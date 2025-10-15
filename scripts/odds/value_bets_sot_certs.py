#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Value bets — SOT certs/singles (1+ on target)
Buckets (priority; player appears once total):
  1) CERTS — 7/7 (most recent 7 all ≥1), min games = 7
  2) CERTS — 100% overall (all games in series ≥1), min games = 5
  3) VALUE — 6/7 (most recent 7 ≥6), min games = 7

Filters:
  - Bookmaker: Bet365 only
  - Market: Player Shots On Target (exclude 'outside the box', halves, headers, etc.)
  - Team ML (Bet365) for player's side < TEAM_ML_MAX (default 3.50)
  - SOT price for Over 0.5 ≥ MIN_DEC_PRICE (default 1.30)
  - Events limited to next WINDOW_DAYS days (default 7; set 0 to disable)
  - Player appears once total (best price, highest-priority bucket)

Input:
  data/player_shots_on_target/combined.json
  data/predicted_xi/by_league/{league_id}.json   (team_id -> team name map)

Output:
  data/value_bets/sot_certs.txt

ENV expected:
  ODDS_API_KEY (required)
  MIN_DEC_PRICE (default 1.30)
  TEAM_WIN_MAX  (default 3.50)
  WINDOW_DAYS   (default 7)
  MIN_CAPTURE_PRICE (default 1.20)  # for diagnostics
  SHOW_NEAR_MISSES  (default "1")
  ALLOW_DNB_FALLBACK (default "1")
"""

import os, re, json, math, time, random, unicodedata, datetime as dt
from pathlib import Path
from typing import Dict, List, Tuple, Optional, Any
from itertools import islice
import requests

# ========= CONFIG =========
SPORT = "football"
BOOKMAKERS = "Bet365"
MIN_PRICE = float(os.getenv("MIN_DEC_PRICE", "1.30"))
TEAM_ML_MAX = float(os.getenv("TEAM_WIN_MAX", "3.50"))
WINDOW_DAYS = int(os.getenv("WINDOW_DAYS", "7"))  # 0 disables time filter
MIN_CAPTURE_PRICE = float(os.getenv("MIN_CAPTURE_PRICE", "1.20"))  # capture for diagnostics
SHOW_NEAR_MISSES  = os.getenv("SHOW_NEAR_MISSES", "1") == "1"
ALLOW_DNB_FALLBACK = os.getenv("ALLOW_DNB_FALLBACK", "1") == "1"

# Leagues to include (SportMonks -> odds-api slugs)
LEAGUE_SLUG_BY_ID = {
    8:   "england-premier-league",
    9:   "england-championship",
    82:  "germany-bundesliga",
    301: "france-ligue-1",
    384: "italy-serie-a",
    387: "italy-serie-b",
    564: "spain-laliga",
    567: "spain-laliga-2",
    72:  "netherlands-eredivisie",
    600: "turkiye-super-lig",
}

EVENTS_API_URL = "https://api.odds-api.io/v3/events"
ODDS_MULTI_API_URL = "https://api.odds-api.io/v3/odds/multi"
HTTP_HEADERS = {"accept": "application/json", "user-agent": "odds-sot-certs/1.0"}
TIMEOUT = 25

ROOT = Path(".")
SOT_FILE = ROOT / "data" / "player_shots_on_target" / "combined.json"
PX_DIR    = ROOT / "data" / "predicted_xi" / "by_league"
OUT_DIR   = ROOT / "data" / "value_bets"; OUT_DIR.mkdir(parents=True, exist_ok=True)
OUT_FILE  = OUT_DIR / "sot_certs.txt"

# ========= MARKET FILTERS: SOT only =========
NEGATIVE_SOT_TERMS = {
    "outside", "outside box", "outside of box", "from outside", "outside the box",
    "first half","1st half","2nd half","second half","half",
    "header","headers","head","left foot","right foot","right-foot","left-foot",
    "distance","long range","goal","goals","to score","assist","assists","ga","g/a",
    "from corner","from free kick","penalty","penalties"
}

def market_is_player_sot(name: str) -> bool:
    s = re.sub(r"\s+", " ", (name or "")).strip().lower()
    if not s: return False
    if any(b in s for b in NEGATIVE_SOT_TERMS): return False
    needed = (("on target" in s) or ("shots on target" in s) or ("sot" in s))
    return ("player" in s) and needed

# ========= STRING / MATCH HELPERS =========
def strip_accents(s: str) -> str:
    return ''.join(c for c in unicodedata.normalize('NFD', s or '') if unicodedata.category(c) != 'Mn')

def norm(s: str) -> str:
    s = strip_accents(s or "").lower()
    s = re.sub(r"[^a-z0-9\s\.-]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s

TEAM_ALIAS = {
    "man city": "manchester city",
    "man utd": "manchester united",
    "man united": "manchester united",
    "spurs": "tottenham hotspur",
    "wolves": "wolverhampton wanderers",
    "west brom": "west bromwich albion",
    "psv": "psv eindhoven",
    "st pauli": "fc st. pauli",
    "inter milan": "inter milano",
    "bayern munich": "bayern münchen",
    "newcastle": "newcastle united",
    "forest": "nottingham forest",
    "betis": "real betis",
    "sociedad": "real sociedad",
    "celta": "rc celta de vigo",
    "deportivo alaves": "deportivo alavés",
}
def expand_alias(name: str) -> str:
    s = norm(name)
    for k, v in TEAM_ALIAS.items():
        if k in s:
            return v
    return s

GENERIC_TEAM_TOKENS = {
    "fc","cf","afc","sc","cd","ud","ac","as","ss","ssc","us","uc","rc","rcd","ca",
    "the","club","de","del","la","las","los","calcio","united","city","saint","st","bk"
}
def team_tokens(name: str):
    s = expand_alias(name)
    toks = set(s.split())
    return {t for t in toks if t not in GENERIC_TEAM_TOKENS}

def team_names_match(a: str, b: str) -> bool:
    if not a or not b: return False
    ta, tb = team_tokens(a), team_tokens(b)
    if not ta or not tb: return False
    if ta == tb: return True
    if ta.issubset(tb) or tb.issubset(ta): return True
    inter = ta & tb; union = ta | tb
    if len(inter) / max(1, len(union)) >= 0.5: return True
    if len(inter) >= 1 and ("madrid" in inter or "milan" in inter or "inter" in inter): return True
    return False

def cleanup_label(label: str) -> str:
    return re.sub(r"(?:\s*\([^)]*\))+$", "", label or "").strip()

def extract_last_name_initial(name: str):
    if not name: return None, None
    name2 = strip_accents(name).replace(".", " ").strip()
    parts = [p for p in name2.split() if p]
    if not parts: return None, None
    last = norm(parts[-1]); initial = None
    for p in parts[:-1]:
        ch = p.strip()[0:1]
        if ch: initial = ch.lower(); break
    return last, initial

def player_label_matches(player: str, option_label: str) -> bool:
    if not player or not option_label: return False
    last, initial = extract_last_name_initial(player)
    label = norm(cleanup_label(option_label))
    if not last or last not in label: return False
    if initial:
        first_word_initial = label.split()[0][0:1] if label.split() else None
        if first_word_initial and first_word_initial == initial: return True
        return bool(re.search(rf"\b{initial}\w*\b.*\b{last}\b", label))
    return True

# ========= IO HELPERS =========
def _load_json(p: Path) -> Any:
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None

def _team_name_map(league_id: int) -> Dict[int, str]:
    blob = _load_json(PX_DIR / f"{league_id}.json") or {}
    m: Dict[int, str] = {}
    for fx in (blob.get("fixtures") or []):
        for side in ("home", "away"):
            s = fx.get(side) or {}
            tid, nm = s.get("team_id"), s.get("name")
            if isinstance(tid, int) and isinstance(nm, str) and nm:
                m.setdefault(tid, nm)
    return m

# ========= READ CANDIDATES FROM combined.json =========
def load_sot_players() -> List[dict]:
    js = _load_json(SOT_FILE) or {}
    players = js.get("players") or js.get("rows") or js.get("data") or []
    return players if isinstance(players, list) else []

def hits_recent(seq: List[int], k: int, thresh: int = 1) -> int:
    seq = [x for x in seq if isinstance(x, int)]
    if len(seq) < k: return 0
    return sum(1 for x in seq[:k] if x >= thresh)

def collect_candidates() -> List[dict]:
    out = []
    # Build team name maps per league on demand
    team_maps: Dict[int, Dict[int, str]] = {}

    for rec in load_sot_players():
        series = (rec.get("sot_last_n") or rec.get("series") or rec.get("sot") or rec.get("shots_on_target_last_n") or [])
        if not isinstance(series, list) or not series:
            continue
        league_id = rec.get("league_id")
        team_id = rec.get("team_id")
        if not isinstance(league_id, int) or league_id not in LEAGUE_SLUG_BY_ID:
            continue

        # Resolve team name
        team = rec.get("team") or rec.get("team_name")
        if not team:
            team_maps.setdefault(league_id, _team_name_map(league_id))
            if isinstance(team_id, int):
                team = team_maps[league_id].get(team_id)
        if not team:
            continue

        player = rec.get("name") or rec.get("player_name") or rec.get("player")
        if not player:
            continue

        pos = rec.get("position_tag") or rec.get("position") or rec.get("pos") or ""

        n = len([x for x in series if isinstance(x, int)])
        # Buckets
        q_7_7 = n >= 7 and all(x >= 1 for x in series[:7])     # 7/7
        q_all_100 = n >= 5 and all(x >= 1 for x in series)     # 100% overall, n>=5
        q_6_7 = n >= 7 and (hits_recent(series, 7, 1) >= 6)    # 6/7

        if not (q_7_7 or q_all_100 or q_6_7):
            continue

        out.append({
            "league_id": league_id, "player": player, "team": team, "position": pos,
            "series": series[:10], "n": n, "q_7_7": q_7_7, "q_all_100": q_all_100, "q_6_7": q_6_7,
        })
    return out

# ========= ODDS API =========
def chunked(it, n):
    it = iter(it)
    while True:
        batch = list(islice(it, n))
        if not batch: return
        yield batch

def http_get_with_retries(url: str, params: dict, max_retries=6, base_sleep=1.0, factor=1.8):
    attempt = 0; last_text = ""
    while attempt < max_retries:
        try:
            r = requests.get(url, params=params, headers=HTTP_HEADERS, timeout=TIMEOUT)
            if r.status_code == 200:
                return r
            if r.status_code in (429,500,502,503,504):
                last_text = r.text
                sleep = base_sleep * (factor ** attempt) + random.uniform(0, 0.4)
                print(f"[RETRY] {url} {r.status_code}; sleeping {sleep:.1f}s...")
                time.sleep(sleep); attempt += 1; continue
            print(f"[HTTP {r.status_code}] {url} :: {r.text[:200]}")
            return None
        except requests.exceptions.RequestException as e:
            sleep = base_sleep * (factor ** attempt) + random.uniform(0, 0.4)
            print(f"[NET] {url} exception: {e}; sleeping {sleep:.1f}s...")
            time.sleep(sleep); attempt += 1
    if last_text:
        print(f"[ERROR] Retries exhausted for {url}. Last body: {last_text[:220]}")
    else:
        print(f"[ERROR] Retries exhausted for {url}.")
    return None

def get_events_for_league(slug: str, api_key: str) -> List[dict]:
    r = http_get_with_retries(EVENTS_API_URL, {"apiKey": api_key, "sport": SPORT, "league": slug})
    if not (r and r.status_code == 200): return []
    try: data = r.json()
    except: data = None
    return data if isinstance(data, list) else []

def within_next_days(ev: dict, days: int) -> bool:
    if not days: return True
    ds = ev.get("date") or ""
    try:
        dt_utc = dt.datetime.fromisoformat(ds.replace("Z", "+00:00"))
        now = dt.datetime.utcnow().replace(tzinfo=dt.timezone.utc)
        return now <= dt_utc <= (now + dt.timedelta(days=days))
    except Exception:
        return False

def get_odds_multi(event_ids: List[int], api_key: str) -> List[dict]:
    if not event_ids: return []
    r = http_get_with_retries(ODDS_MULTI_API_URL, {
        "apiKey": api_key, "eventIds": ",".join(map(str, event_ids)), "bookmakers": BOOKMAKERS
    })
    if not (r and r.status_code == 200): return []
    try: data = r.json()
    except: return []
    return data if isinstance(data, list) else []

def bet365_markets(ev: dict):
    for bm_name, markets in (ev.get("bookmakers") or {}).items():
        if "bet365" not in (bm_name or "").lower(): continue
        for m in markets or []:
            yield m

MATCH_WINNER_KEYS = [
    "1x2","match result","match winner","moneyline","full time result",
    "to win","win/draw/win","wdw","ml","match odds","result","3-way","3 way",
    "90 minutes","regular time result"
]
DNB_KEYS = ["draw no bet","dnb"]

def market_is_match_winner(name: str) -> bool:
    s = (name or "").strip().lower()
    return bool(s) and any(k in s for k in MATCH_WINNER_KEYS)

def market_is_dnb(name: str) -> bool:
    s = (name or "").strip().lower()
    return bool(s) and any(k in s for k in DNB_KEYS)

def min_win_prices(ev: dict) -> Tuple[Optional[float], Optional[float], bool]:
    best_home = None; best_away = None
    for m in bet365_markets(ev):
        if not market_is_match_winner(m.get("name","")): continue
        odds = m.get("odds") or []
        for row in odds:
            try:
                h = float(row.get("home")) if row.get("home") not in (None, "N/A") else None
                a = float(row.get("away")) if row.get("away") not in (None, "N/A") else None
            except: h = a = None
            if isinstance(h, float): best_home = h if (best_home is None or h < best_home) else best_home
            if isinstance(a, float): best_away = a if (best_away is None or a < best_away) else best_away
    used_fallback = False
    if (best_home is None or best_away is None) and ALLOW_DNB_FALLBACK:
        for m in bet365_markets(ev):
            if not market_is_dnb(m.get("name","")): continue
            odds = m.get("odds") or []
            for row in odds:
                try:
                    h = float(row.get("home")) if row.get("home") not in (None, "N/A") else None
                    a = float(row.get("away")) if row.get("away") not in (None, "N/A") else None
                except: h = a = None
                if isinstance(h, float) and best_home is None: best_home = h
                if isinstance(a, float) and best_away is None: best_away = a
        if best_home is not None or best_away is not None:
            used_fallback = True
    return best_home, best_away, used_fallback

def parse_line(opt: dict) -> Optional[float]:
    if isinstance(opt, dict) and "hdp" in opt:
        try: return float(opt["hdp"])
        except: pass
    label = (opt.get("label") if isinstance(opt, dict) else None) or ""
    m = re.search(r"\(([-+]?\d+(?:\.\d+)?)\)", label)
    if m:
        try: return float(m.group(1))
        except: return None
    return None

def parse_over_price(opt: dict) -> Optional[float]:
    try:
        val = opt.get("over") if isinstance(opt, dict) else None
        return float(val) if val is not None else None
    except:
        return None

# ========= MAIN =========
def main():
    api_key = os.getenv("ODDS_API_KEY")
    if not api_key:
        raise SystemExit("ERROR: ODDS_API_KEY not set.")

    # 1) Candidates from combined SOT histories
    candidates = collect_candidates()
    if not candidates:
        print("[RESULT] No SOT candidates meeting stat windows.")
        OUT_FILE.write_text("No matches found.\n", encoding="utf-8")
        return

    # 2) Events per league (optional time window)
    lids_used = sorted({c["league_id"] for c in candidates})
    slug_used = {lid: LEAGUE_SLUG_BY_ID.get(lid) for lid in lids_used if LEAGUE_SLUG_BY_ID.get(lid)}
    events_by_league: Dict[int, List[dict]] = {}
    for lid, slug in slug_used.items():
        evs = get_events_for_league(slug, api_key)
        if WINDOW_DAYS:
            evs = [e for e in evs if within_next_days(e, WINDOW_DAYS)]
        events_by_league[lid] = evs
        print(f"[EVENTS] {slug}: {len(evs)} (next {WINDOW_DAYS}d)")

    # 3) Map team -> event ids
    def find_event_ids(lid: int, team: str) -> List[int]:
        evs = events_by_league.get(lid, [])
        out = []
        for ev in evs:
            if team_names_match(team, ev.get("home","")) or team_names_match(team, ev.get("away","")):
                if isinstance(ev.get("id"), int): out.append(ev["id"])
        return out

    for c in candidates:
        c["event_ids"] = find_event_ids(c["league_id"], c["team"])

    # 4) Fetch odds (batches of 10)
    event_ids = sorted({eid for c in candidates for eid in (c.get("event_ids") or [])})
    print(f"[ODDS] Unique events to query: {len(event_ids)}")
    odds_payloads: List[dict] = []
    for i, batch in enumerate(chunked(event_ids, 10), start=1):
        print(f"[ODDS] batch {i} — {len(batch)} ids")
        odds_payloads.extend(get_odds_multi(batch, api_key))
    if not odds_payloads:
        print("[RESULT] No odds payloads; stopping.")
        OUT_FILE.write_text("No matches found.\n", encoding="utf-8")
        return
    id_to_ev = {o.get("id"): o for o in odds_payloads if isinstance(o.get("id"), int)}

    # 5) Scan markets — capture best SOT price per player (>= MIN_CAPTURE_PRICE), team ML filter
    best_by_player_all: Dict[str, dict] = {}
    def pkey(name: str) -> str: return norm(name)

    had_market = 0
    passed_team_ml = 0

    for c in candidates:
        pk = pkey(c["player"])
        for ev_id in (c.get("event_ids") or []):
            ev = id_to_ev.get(ev_id)
            if not ev: continue
            home, away = ev.get("home",""), ev.get("away","")

            # Team ML
            home_ml, away_ml, _fb = min_win_prices(ev)
            side = "home" if team_names_match(c["team"], home) else ("away" if team_names_match(c["team"], away) else None)
            if not side: continue
            team_ml = home_ml if side == "home" else away_ml
            if team_ml is None or team_ml >= TEAM_ML_MAX:
                continue
            passed_team_ml += 1

            # Player SOT Over 0.5
            best_price_here = None; mkt_name = None
            found_market_here = False
            for m in bet365_markets(ev):
                name = m.get("name","")
                if not market_is_player_sot(name): 
                    continue
                found_market_here = True
                odds = m.get("odds")
                iterable = (odds.items() if isinstance(odds, dict)
                            else ((opt.get("label"), opt) for opt in (odds or [])))
                for label, opt in iterable:
                    if not player_label_matches(c["player"], label):
                        continue
                    line = parse_line(opt if isinstance(opt, dict) else {})
                    if line is None or not math.isclose(line, 0.5, abs_tol=1e-6):
                        continue
                    price = parse_over_price(opt if isinstance(opt, dict) else {})
                    if price is None or price < MIN_CAPTURE_PRICE:
                        continue
                    if (best_price_here is None) or (price > best_price_here + 1e-9):
                        best_price_here = price; mkt_name = name
            if found_market_here:
                had_market += 1
            if best_price_here is None:
                continue

            row = {
                "player": c["player"], "position": c["position"] or "",
                "team": c["team"], "fixture": f"{home} vs {away}",
                "price": float(best_price_here), "team_ml": float(team_ml),
                "series": c["series"], "n": c["n"],
                "q_7_7": c["q_7_7"], "q_all_100": c["q_all_100"], "q_6_7": c["q_6_7"],
                "league_id": c["league_id"], "market": mkt_name or "Player Shots On Target",
            }
            cur = best_by_player_all.get(pk)
            if (cur is None) or (row["price"] > cur["price"] + 1e-9):
                best_by_player_all[pk] = row

    # 6) Apply ≥ MIN_PRICE and allocate to buckets (priority: 7/7 → 100% all → 6/7)
    eligible = {k:v for k,v in best_by_player_all.items() if v["price"] >= MIN_PRICE}
    buckets = {"7of7": [], "all100": [], "6of7": []}
    for pk, row in eligible.items():
        if row["q_7_7"]:
            buckets["7of7"].append(row)
        elif row["q_all_100"]:
            buckets["all100"].append(row)
        elif row["q_6_7"]:
            buckets["6of7"].append(row)

    # 7) Render
    header = (
        f"Generated at (UTC): {dt.datetime.utcnow().isoformat()} | "
        f"Min price: {MIN_PRICE:.2f} | Capture≥{MIN_CAPTURE_PRICE:.2f} | "
        f"Team ML < {TEAM_ML_MAX:.2f} | Window: {WINDOW_DAYS}d"
    )
    sub = "Market: Bet365 — Player Shots On Target Over 0.5 only (NOT outside box / halves). Most recent windows strictly used. Player appears once (priority: 7/7 → 100% all → 6/7)."
    lines = [header, sub, ""]
    print(f"[PIPE] candidates={len(candidates)} mapped_events={len({eid for c in candidates for eid in (c.get('event_ids') or [])})} passed_team_ml={passed_team_ml} had_sot_market={had_market} captured={len(best_by_player_all)} eligible(≥{MIN_PRICE})={len(eligible)}")

    def fmt_row(x: dict) -> str:
        ser7 = ",".join(map(str, x["series"][:7])) if x["series"] else ""
        pos = f"[{x['position']}]" if x.get("position") else ""
        return (f" • {x['player']} {pos} — {x['team']} | {x['fixture']} | "
                f"Over 0.5 SOT @ {x['price']:.3f} | Team ML {x['team_ml']:.3f} | "
                f"series7: {ser7} | n: {x['n']}")

    sections = [
        ("7of7",  "===== CERTS — 1+ SOT (7/7) ====="),
        ("all100","===== CERTS — 1+ SOT (100% overall; n≥5) ====="),
        ("6of7",  "===== VALUE — 1+ SOT (6/7) ====="),
    ]
    something = False
    for key, title in sections:
        arr = buckets[key]
        if not arr: continue
        something = True
        arr.sort(key=lambda r: (-r["price"], r["player"]))
        lines.append(title)
        for x in arr:
            lines.append(fmt_row(x))
        lines.append("")

    if not something:
        lines.append("No matches found.")

    OUT_FILE.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    print("\n".join(lines))

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
