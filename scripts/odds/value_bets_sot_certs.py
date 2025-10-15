#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Value bets — SOT certs (1+ SOT)
Source data: data/player_shots_on_target/combined.json (+ predicted XI for team names)
Bookmaker: Bet365 only
Market: Player Shots On Target (STRICT) — Over 0.5 only

Buckets (player appears once total; 7/7 has priority):
  A) 7/7  -> last 7 all >=1 SOT       (min games = 7)
  B) 6/7  -> last 7 >=1 SOT in >= 6   (min games = 7)

Filters:
  - Price for 1+ SOT >= MIN_DEC_PRICE (default 1.30)
  - Team ML (Bet365) for player's side < TEAM_ML_MAX (default 3.50)
  - Events limited to next WINDOW_DAYS days (default 7; set 0 to disable)
  - Player listed once overall (best price, highest-priority bucket)

Output: data/value_bets/sot_certs.txt
Diagnostics printed to console and included in file header.

ENV expected:
  ODDS_API_KEY (required)
  MIN_DEC_PRICE (default 1.30)
  TEAM_WIN_MAX  (default 3.50)
  WINDOW_DAYS   (default 7)
  MIN_CAPTURE_PRICE (default 1.10)    # capture for diagnostics/near misses
  SHOW_NEAR_MISSES  (default "1")
  ALLOW_DNB_FALLBACK (default "1")    # use Draw No Bet if ML is missing
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
MIN_CAPTURE_PRICE = float(os.getenv("MIN_CAPTURE_PRICE", "1.10"))  # capture for diagnostics
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
HTTP_HEADERS = {"accept": "application/json", "user-agent": "odds-sot-certs/1.1"}
TIMEOUT = 25

ROOT = Path(".")
PX_DIR    = ROOT / "data" / "predicted_xi" / "by_league"   # team_id -> name map
COMBINED  = ROOT / "data" / "player_shots_on_target" / "combined.json"
OUT_DIR   = ROOT / "data" / "value_bets"; OUT_DIR.mkdir(parents=True, exist_ok=True)
OUT_FILE  = OUT_DIR / "sot_certs.txt"

# ========= MARKET FILTERS (STRICT “Player Shots On Target” @ 0.5 only) =========
NEGATIVE_SOT_TERMS = {
    "outside","outside box","outside of box","from outside","outside the box",
    "header","headers","headed",
    "left foot","right foot","right-foot","left-foot",
    "first half","1st half","2nd half","second half","half",
    "distance","long range","goal","goals","to score","assist","assists","ga","g/a",
    "team shots on target","conceded","keeper","goalkeeper","saves","shots conceded"
}

def market_is_player_sot_strict(name: str) -> bool:
    """
    Accept ONLY the pure 'Player Shots On Target' market.
    Must contain 'player' AND 'on target'. Reject if any negative term occurs.
    """
    s = re.sub(r"\s+", " ", (name or "")).strip().lower()
    if not s:
        return False
    if "player" not in s:
        return False
    if "on target" not in s:
        return False
    if any(b in s for b in NEGATIVE_SOT_TERMS):
        return False
    # Strong allow-list pattern to avoid variants like 'headed' or 'outside the box'
    # Typical books use names like 'Player Shots On Target' or 'Player - Shots On Target'
    return True

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
    """Map team_id -> team_name from predicted_xi file."""
    blob = _load_json(PX_DIR / f"{league_id}.json") or {}
    m: Dict[int, str] = {}
    for fx in (blob.get("fixtures") or []):
        for side in ("home", "away"):
            s = fx.get(side) or {}
            tid, nm = s.get("team_id"), s.get("name")
            if isinstance(tid, int) and isinstance(nm, str) and nm:
                m.setdefault(tid, nm)
    return m

# ========= SERIES / HITS (STRICT: MOST RECENT WINDOW) =========
def hits_recent(seq: List[int], k: int, thresh: int = 1) -> int:
    seq = [x for x in seq if isinstance(x, int)]
    if len(seq) < k: return 0
    return sum(1 for x in seq[:k] if x >= thresh)

def qualify_buckets(on_target: List[int]) -> Tuple[bool, bool, int]:
    hits7 = hits_recent(on_target, 7, 1)
    return (hits7 == 7, (len(on_target) >= 7 and hits7 >= 6), hits7)

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
            if r.status_code == 200: return r
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
        nm = m.get("name","")
        if not market_is_match_winner(nm): continue
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
            if not market_is_dnb(m.get("name","")):
                continue
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

def price_for_over_point5_sot(opt: dict) -> Optional[float]:
    """
    STRICT: Only accept Over 0.5 line. Do NOT accept 1.5/2.5 or generic YES/price.
    Odds API usually encodes price at opt['over'] when line/hdp == 0.5.
    """
    if not isinstance(opt, dict):
        return None
    line = parse_line(opt)
    if line is None or not math.isclose(line, 0.5, abs_tol=1e-6):
        return None
    val = opt.get("over")
    try:
        return float(val) if val is not None else None
    except:
        return None

# ========= MAIN =========
def main():
    api_key = os.getenv("ODDS_API_KEY")
    if not api_key:
        raise SystemExit("ERROR: ODDS_API_KEY not set.")

    # 0) Load SOT combined JSON
    blob = _load_json(COMBINED) or {}
    players = blob.get("players") or []
    if not players:
        print("[RESULT] No players in combined SOT file.")
        OUT_FILE.write_text("No matches found.\n", encoding="utf-8")
        return

    # 1) Collect candidates by buckets using STRICT recent windows
    cands: List[dict] = []
    for rec in players:
        on_target = rec.get("on_target_last_n") or rec.get("sot_last_n") or rec.get("shots_on_target") or []
        if not isinstance(on_target, list):
            continue
        order = (rec.get("order") or "").lower()
        seq = on_target if order == "latest_first" or not order else on_target

        is_7of7, is_6of7, hits7 = qualify_buckets(seq)
        if not (is_7of7 or is_6of7):
            continue

        lid = rec.get("league_id"); tid = rec.get("team_id")
        if not isinstance(lid, int) or not isinstance(tid, int):
            continue

        team_map = _team_name_map(lid)
        team = team_map.get(tid) or rec.get("team") or rec.get("team_name")
        if not team:
            continue

        cands.append({
            "league_id": lid,
            "team_id": tid,
            "team": team,
            "player": rec.get("name") or rec.get("player_name") or "",
            "position": rec.get("position_tag") or rec.get("position") or rec.get("pos") or "",
            "on_target": seq[:10],
            "is_7of7": is_7of7,
            "is_6of7": (not is_7of7) and is_6of7,
            "hits7": hits7,
        })

    stats_A = sum(1 for c in cands if c["is_7of7"])
    stats_B = sum(1 for c in cands if c["is_6of7"])
    print(f"[STATS] total_players={len(players)}  bucketA(7/7)={stats_A}  bucketB(6/7)={stats_B}")

    if not cands:
        OUT_FILE.write_text("No matches found.\n", encoding="utf-8")
        return

    # 2) Fetch events per league (optionally time-windowed)
    lids_used = sorted({c["league_id"] for c in cands if LEAGUE_SLUG_BY_ID.get(c["league_id"])})
    events_by_league: Dict[int, List[dict]] = {}
    for lid in lids_used:
        slug = LEAGUE_SLUG_BY_ID[lid]
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
                if isinstance(ev.get("id"), int):
                    out.append(ev["id"])
        return out

    for c in cands:
        c["event_ids"] = find_event_ids(c["league_id"], c["team"])

    mapped = [c for c in cands if c.get("event_ids")]
    print(f"[MAP] candidates_mapped={len(mapped)}/{len(cands)}")

    # 4) Fetch odds (batches of 10)
    event_ids = sorted({eid for c in mapped for eid in c["event_ids"]})
    print(f"[ODDS] Unique events to query: {len(event_ids)}")
    odds_payloads: List[dict] = []
    for i, batch in enumerate(chunked(event_ids, 10), start=1):
        print(f"[ODDS] batch {i} — {len(batch)} ids")
        odds_payloads.extend(get_odds_multi(batch, api_key))
    if not odds_payloads:
        OUT_FILE.write_text("No matches found.\n", encoding="utf-8")
        print("[RESULT] No odds payloads; stopping.")
        return
    id_to_ev = {o.get("id"): o for o in odds_payloads if isinstance(o.get("id"), int)}

    # 5) Capture best SOT 1+ price per player (>= MIN_CAPTURE_PRICE) and pass ML filter
    best_by_player: Dict[str, dict] = {}
    def pkey(name: str) -> str: return norm(name)

    ml_pass = 0
    had_sot_market = 0

    for c in mapped:
        pk = pkey(c["player"])
        for ev_id in c["event_ids"]:
            ev = id_to_ev.get(ev_id)
            if not ev:
                continue
            home, away = ev.get("home",""), ev.get("away","")

            # Team ML (with optional DNB fallback)
            home_ml, away_ml, _used_fb = min_win_prices(ev)
            side = "home" if team_names_match(c["team"], home) else ("away" if team_names_match(c["team"], away) else None)
            if not side:
                continue
            team_ml = home_ml if side == "home" else away_ml
            if team_ml is None or team_ml >= TEAM_ML_MAX:
                continue
            ml_pass += 1

            # STRICT market + STRICT line (0.5 only)
            found_market_here = False
            best_price_here = None; mkt_name = None

            for m in bet365_markets(ev):
                name = m.get("name","")
                if not market_is_player_sot_strict(name):
                    continue
                found_market_here = True
                odds = m.get("odds")

                if isinstance(odds, dict):
                    iterator = list(odds.items())
                else:
                    iterator = [(opt.get("label"), opt) for opt in (odds or [])]

                for label, opt in iterator:
                    if not player_label_matches(c["player"], label):
                        continue
                    price = price_for_over_point5_sot(opt if isinstance(opt, dict) else {})
                    if price is None or price < MIN_CAPTURE_PRICE:
                        continue
                    if (best_price_here is None) or (price > best_price_here + 1e-9):
                        best_price_here = price; mkt_name = name

            if found_market_here:
                had_sot_market += 1
            if best_price_here is None:
                continue

            row = {
                "player": c["player"], "position": c["position"] or "",
                "team": c["team"], "fixture": f"{home} vs {away}",
                "price": float(best_price_here), "team_ml": float(team_ml),
                "series7": c["on_target"][:7], "hits7": c["hits7"],
                "bucket": "7/7" if c["is_7of7"] else ("6/7" if c["is_6of7"] else ""),
                "league_id": c["league_id"], "market": mkt_name or "Player Shots On Target",
            }
            cur = best_by_player.get(pk)
            if (cur is None) or (row["price"] > cur["price"] + 1e-9):
                best_by_player[pk] = row

    if not best_by_player:
        OUT_FILE.write_text("No matches found.\n", encoding="utf-8")
        print("[RESULT] No prices captured at all (even below MIN_DEC_PRICE).")
        print(f"[PIPE] ml_pass={ml_pass} had_sot_market={had_sot_market}")
        return

    # 6) Apply ≥ MIN_PRICE and bucket priority (7/7 first, then 6/7). Player only once.
    eligible = [v for v in best_by_player.values() if v["price"] >= MIN_PRICE]
    bA = [r for r in eligible if r["bucket"] == "7/7"]
    bB = [r for r in eligible if r["bucket"] == "6/7"]

    bA.sort(key=lambda r: (-r["price"], r["player"]))
    bB.sort(key=lambda r: (-r["price"], r["player"]))

    # 7) Render
    header = (
        f"Generated at (UTC): {dt.datetime.utcnow().isoformat()} | "
        f"Min price: {MIN_PRICE:.2f} | Capture≥{MIN_CAPTURE_PRICE:.2f} | "
        f"Team ML < {TEAM_ML_MAX:.2f} | Window: {WINDOW_DAYS}d | DNB fallback: {int(ALLOW_DNB_FALLBACK)}"
    )
    sub = "Market: Bet365 — Player Shots On Target (Over 0.5) STRICT. Excludes headed/outside/halves. Most recent windows strictly used."
    out_lines = [header, sub, ""]
    def fmt_row(x: dict) -> str:
        ser7 = ",".join(map(str, x["series7"])) if x.get("series7") else ""
        pos = f"[{x['position']}]" if x.get("position") else ""
        return (f" • {x['player']} {pos} — {x['team']} | {x['fixture']} | "
                f"1+ SOT @ {x['price']:.3f} | Team Win (ML) {x['team_ml']:.3f} | "
                f"{x['bucket']} (last7: {x['hits7']}/7; series7: {ser7})")

    if bA:
        out_lines.append("===== CERTS — 1+ SOT (7/7) =====")
        for x in bA: out_lines.append(fmt_row(x))
        out_lines.append("")
    if bB:
        out_lines.append("===== CERTS — 1+ SOT (6/7) =====")
        for x in bB: out_lines.append(fmt_row(x))
        out_lines.append("")

    if (not bA) and (not bB) and SHOW_NEAR_MISSES:
        near = sorted(best_by_player.values(), key=lambda r: (-r["price"], r["player"]))
        out_lines.append("No matches found.")
        out_lines.append("")
        out_lines.append("----- NEAR MISSES (captured but < MIN_DEC_PRICE) -----")
        for x in near[:30]:
            out_lines.append(fmt_row(x))

    OUT_FILE.write_text("\n".join(out_lines).rstrip() + "\n", encoding="utf-8")

    # Diagnostics
    print(header); print(sub)
    print(f"[PIPE] mapped={len(mapped)}/{len(cands)}  ml_pass={ml_pass}  had_sot_market={had_sot_market}")
    print(f"[PRICE] captured_players(>= {MIN_CAPTURE_PRICE})={len(best_by_player)}  eligible(>= {MIN_PRICE})={len(bA)+len(bB)}")

    if bA:
        print("\n===== CERTS — 1+ SOT (7/7) =====")
        for x in bA: print(fmt_row(x))
    if bB:
        print("\n===== CERTS — 1+ SOT (6/7) =====")
        for x in bB: print(fmt_row(x))

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
