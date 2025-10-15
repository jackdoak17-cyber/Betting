#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Value bets — SHOTS ON TARGET certs (Bet365)
Buckets (priority; player appears once total):
  A) 100% last 5 (>=1 SOT in each of last 5), min n=5
  B) 6 of last 7 (>=1 SOT in at least 6 of last 7), min n=7

Filters:
  - Bookmaker: Bet365 only
  - Market: Player Shots On Target (NOT halves / outside-box / headers, etc.)
  - Team ML (Bet365) for player's side < TEAM_ML_MAX (default 3.50)
  - Price for Over 0.5 SOT >= MIN_DEC_PRICE (default 1.30)
  - Events limited to next WINDOW_DAYS days (default 7)

Output: data/value_bets/sot_certs.txt
Diagnostics printed to console and included in file header.

ENV:
  ODDS_API_KEY (required)
  MIN_DEC_PRICE (default 1.30)
  TEAM_WIN_MAX  (default 3.50)
  WINDOW_DAYS   (default 7)
"""

import os, re, json, math, time, random, unicodedata, datetime as dt
from pathlib import Path
from typing import Dict, List, Tuple, Optional, Any
import requests
from itertools import islice

# ========= CONFIG =========
SPORT = "football"
BOOKMAKERS = "Bet365"
MIN_PRICE = float(os.getenv("MIN_DEC_PRICE", "1.30"))
TEAM_ML_MAX = float(os.getenv("TEAM_WIN_MAX", "3.50"))
WINDOW_DAYS = int(os.getenv("WINDOW_DAYS", "7"))

EVENTS_API_URL = "https://api.odds-api.io/v3/events"
ODDS_MULTI_API_URL = "https://api.odds-api.io/v3/odds/multi"
HTTP_HEADERS = {"accept": "application/json", "user-agent": "odds-sot-certs/1.2"}
TIMEOUT = 25

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

ROOT = Path(".")
SOT_COMBINED = ROOT / "data" / "player_shots_on_target" / "combined.json"
PX_DIR       = ROOT / "data" / "predicted_xi" / "by_league"
OUT_DIR      = ROOT / "data" / "value_bets"; OUT_DIR.mkdir(parents=True, exist_ok=True)
OUT_FILE     = OUT_DIR / "sot_certs.txt"

# ========= MARKET FILTERS =========
NEGATIVE_TERMS = {
    "outside","outside box","outside of box","from outside","outside the box",
    "first half","1st half","2nd half","second half","half",
    "distance","long range",
    "header","headers","left foot","right foot","right-foot","left-foot",
    "goal","goals","to score","assist","assists","ga","g/a",
    "corner","corners","free kick","penalty","penalties",
    "team shots on target","shots conceded","goalkeeper","keeper saves",
}
def market_is_player_sot(name: str) -> bool:
    s = re.sub(r"\s+", " ", (name or "")).strip().lower()
    if not s: return False
    # Accept either "shots on target" OR "sot" and require it's a player market
    if "player" not in s: return False
    if ("shot" in s and "target" in s) or ("sot" in s):
        if any(bad in s for bad in NEGATIVE_TERMS):
            return False
        return True
    return False

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
    if ta == tb or ta.issubset(tb) or tb.issubset(ta): return True
    inter = ta & tb; union = ta | tb
    if len(inter) / max(1, len(union)) >= 0.5: return True
    if len(inter) >= 1 and ("madrid" in inter or "milan" in inter or "inter" in inter): return True
    return False

def cleanup_label(label: str) -> str:
    return re.sub(r"(?:\s*\([^)]*\))+$", "", label or "").strip()

def extract_last_name_initial(name: str):
    if not name: return None, None
    parts = [p for p in strip_accents(name).replace(".", " ").split() if p]
    if not parts: return None, None
    last = norm(parts[-1]); initial = parts[0][0:1].lower() if parts and parts[0] else None
    return last, initial

def player_label_matches(player: str, option_label: str) -> bool:
    if not player or not option_label: return False
    last, initial = extract_last_name_initial(player)
    label = norm(cleanup_label(option_label))
    if not last or last not in label: return False
    if initial:
        first_word_initial = (label.split()[0][0:1] if label.split() else None)
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

SERIES_KEYS = ("shots_on_target_last_n", "sot_last_n", "series_sot", "on_target_last_n", "series")
def series_from_rec(rec: dict) -> List[int]:
    for k in SERIES_KEYS:
        v = rec.get(k)
        if isinstance(v, list):
            return [int(x) if isinstance(x, (int,float)) else 0 for x in v]
    return []

def ensure_latest_first(series: List[int], rec: dict) -> List[int]:
    order = (rec.get("order") or "").lower()
    return list(reversed(series)) if order == "oldest_first" else series

def last_k_counts(series: List[int], k: int) -> Tuple[int, List[int]]:
    sub = series[:k]
    return sum(1 for x in sub if x >= 1), sub

# ========= HTTP / ODDS =========
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
    if not isinstance(data, list): return []
    if WINDOW_DAYS <= 0: return data
    now = dt.datetime.utcnow(); end = now + dt.timedelta(days=WINDOW_DAYS)
    out = []
    for ev in data:
        d = ev.get("date")
        try: when = dt.datetime.fromisoformat(d.replace("Z","+00:00")) if isinstance(d, str) else None
        except: when = None
        if when and now <= when <= end:
            out.append(ev)
    return out

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

# ----- line & price parsing -----
PLUS_PAT = re.compile(r"\b(\d+)\s*\+\b")           # "1+"
OR_MORE_PAT = re.compile(r"\b(\d+)\s*or\s*more\b") # "1 or more"
PAREN_PAT = re.compile(r"\(([-+]?\d+(?:\.\d+)?)\)")

def parse_line_any(opt: dict) -> Optional[float]:
    # direct "hdp"
    if isinstance(opt, dict) and "hdp" in opt:
        try: return float(opt["hdp"])
        except: pass
    # label parsing
    label = (opt or {}).get("label") if isinstance(opt, dict) else None
    if not isinstance(label, str): return None
    m = PAREN_PAT.search(label)
    if m:
        try: return float(m.group(1))
        except: pass
    lab = label.lower()
    m = PLUS_PAT.search(lab) or OR_MORE_PAT.search(lab)
    if m:
        try:
            base = float(m.group(1))
            return base - 0.5  # "1+" -> 0.5, "2+" -> 1.5
        except: return None
    return None

def parse_any_price(opt: dict) -> Optional[float]:
    if not isinstance(opt, dict): return None
    for k in ("over","yes","price","odds","decimal","dec"):
        v = opt.get(k)
        if v is None: continue
        try: return float(v)
        except: continue
    return None

# ----- team win pricing -----
MATCH_WINNER_KEYS = [
    "1x2","match result","match winner","moneyline","full time result","to win",
    "win/draw/win","wdw","ml","match odds","result","3-way","3 way","90 minutes","regular time result"
]
DNB_KEYS = ["draw no bet","dnb"]

def market_is_match_winner(name: str) -> bool:
    s = (name or "").strip().lower()
    return bool(s) and any(k in s for k in MATCH_WINNER_KEYS)

def market_is_dnb(name: str) -> bool:
    s = (name or "").strip().lower()
    return bool(s) and any(k in s for k in DNB_KEYS)

def min_win_prices(ev: dict, allow_dnb_fallback: bool=True) -> Tuple[Optional[float], Optional[float]]:
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
    if allow_dnb_fallback and (best_home is None or best_away is None):
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
    return best_home, best_away

# ========= MAIN =========
def main():
    api_key = os.getenv("ODDS_API_KEY")
    if not api_key:
        raise SystemExit("ERROR: ODDS_API_KEY not set.")

    # 1) Load SOT histories
    blob = _load_json(SOT_COMBINED) or {}
    players = blob.get("players") or blob.get("rows") or blob.get("data") or []
    if not isinstance(players, list): players = []
    total = len(players)

    team_name_cache: Dict[int, Dict[int, str]] = {}

    # Build candidates
    cand_A, cand_B = [], []
    for rec in players:
        series = ensure_latest_first(series_from_rec(rec), rec)
        if not series: continue
        league_id = rec.get("league_id"); team_id = rec.get("team_id")
        if not isinstance(league_id, int) or not isinstance(team_id, int): continue

        team = rec.get("team") or rec.get("team_name") or ""
        if not team:
            if league_id not in team_name_cache:
                team_name_cache[league_id] = _team_name_map(league_id)
            team = team_name_cache[league_id].get(team_id, "")
        if not team: continue

        player = rec.get("name") or rec.get("player_name") or rec.get("player")
        if not player: continue
        pos = rec.get("position_tag") or rec.get("position") or ""

        ints = [x for x in series if isinstance(x, int)]
        n = len(ints)
        c5, w5 = last_k_counts(ints, 5) if n >= 5 else (0, [])
        c7, w7 = last_k_counts(ints, 7) if n >= 7 else (0, [])

        if n >= 5 and c5 == 5:
            cand_A.append({"league_id": league_id, "team_id": team_id, "team": team,
                           "player": player, "position": pos, "series": ints[:10],
                           "w5": w5, "w7": w7, "c5": c5, "c7": c7})
        elif n >= 7 and c7 >= 6:
            cand_B.append({"league_id": league_id, "team_id": team_id, "team": team,
                           "player": player, "position": pos, "series": ints[:10],
                           "w5": w5, "w7": w7, "c5": c5, "c7": c7})

    print(f"[STATS] total_players={total}  bucketA(100% last5)={len(cand_A)}  bucketB(6/7 last7)={len(cand_B)}")
    if not cand_A and not cand_B:
        _render([], [], empty_reason="No players satisfied windows")
        return

    # 2) Events in next WINDOW_DAYS
    need_lids = sorted({c["league_id"] for c in (cand_A + cand_B)})
    slugs = {lid: LEAGUE_SLUG_BY_ID.get(lid) for lid in need_lids if LEAGUE_SLUG_BY_ID.get(lid)}
    events_by_league: Dict[int, List[dict]] = {}
    total_events = 0
    for lid, slug in slugs.items():
        evs = get_events_for_league(slug, api_key)
        events_by_league[lid] = evs
        total_events += len(evs)
        print(f"[EVENTS] {slug}: {len(evs)} (next {WINDOW_DAYS}d)")
    if total_events == 0:
        _render([], [], empty_reason="No events in window")
        return

    # 3) Map candidates → event ids
    def map_events(c_list: List[dict]) -> int:
        mapped = 0
        for c in c_list:
            ids = []
            for ev in events_by_league.get(c["league_id"], []):
                if team_names_match(c["team"], ev.get("home","")) or team_names_match(c["team"], ev.get("away","")):
                    if isinstance(ev.get("id"), int):
                        ids.append(ev["id"])
            if ids:
                mapped += 1
            c["event_ids"] = ids
        return mapped

    mappedA = map_events(cand_A); mappedB = map_events(cand_B)
    print(f"[MAP] bucketA mapped={mappedA}/{len(cand_A)}  bucketB mapped={mappedB}/{len(cand_B)}")

    all_ids = sorted({eid for c in (cand_A + cand_B) for eid in (c.get("event_ids") or [])})
    if not all_ids:
        _render([], [], empty_reason="No event ids after mapping")
        return

    # 4) Fetch odds for those events
    odds_payloads: List[dict] = []
    for i, batch in enumerate(chunked(all_ids, 10), start=1):
        print(f"[ODDS] batch {i} — {len(batch)} ids")
        odds_payloads.extend(get_odds_multi(batch, api_key))
    id_to_ev = {o.get("id"): o for o in odds_payloads if isinstance(o.get("id"), int)}
    if not id_to_ev:
        _render([], [], empty_reason="No odds payloads returned")
        return

    # 5) Evaluate market + line 0.5 + price + ML filter
    def evaluate(c_list: List[dict]) -> Tuple[int, int, int, List[dict]]:
        had_market = 0; priced_meet = 0; kept = []
        for c in c_list:
            best_price = None; best_fixture = ""; best_ml = None; best_market = None
            for ev_id in (c.get("event_ids") or []):
                ev = id_to_ev.get(ev_id)
                if not ev:
                    continue
                home, away = ev.get("home",""), ev.get("away","")
                side = "home" if team_names_match(c["team"], home) else ("away" if team_names_match(c["team"], away) else None)
                if not side:
                    continue
                h_ml, a_ml = min_win_prices(ev, allow_dnb_fallback=True)
                team_ml = h_ml if side == "home" else a_ml
                if team_ml is None or team_ml >= TEAM_ML_MAX:
                    continue
                # scan sot markets
                sot_seen_in_event = False
                for m in bet365_markets(ev):
                    if not market_is_player_sot(m.get("name","")):
                        continue
                    sot_seen_in_event = True
                    odds = m.get("odds")
                    if isinstance(odds, list):
                        for opt in odds:
                            if not player_label_matches(c["player"], opt.get("label")): continue
                            line = parse_line_any(opt)
                            if line is None or not math.isclose(line, 0.5, abs_tol=1e-6): continue
                            price = parse_any_price(opt)
                            if price is None or price < MIN_PRICE: continue
                            if (best_price is None) or (price > best_price + 1e-9):
                                best_price = price
                                best_fixture = f"{home} vs {away}"
                                best_ml = team_ml
                                best_market = m.get("name") or "Player Shots On Target"
                    elif isinstance(odds, dict):
                        for label, opt in odds.items():
                            if not player_label_matches(c["player"], label): continue
                            line = parse_line_any(opt if isinstance(opt, dict) else {})
                            if line is None or not math.isclose(line, 0.5, abs_tol=1e-6): continue
                            price = parse_any_price(opt if isinstance(opt, dict) else {})
                            if price is None or price < MIN_PRICE: continue
                            if (best_price is None) or (price > best_price + 1e-9):
                                best_price = price
                                best_fixture = f"{home} vs {away}"
                                best_ml = team_ml
                                best_market = m.get("name") or "Player Shots On Target"
                if sot_seen_in_event:
                    had_market += 1
            if best_price is not None:
                priced_meet += 1
                kept.append({
                    "player": c["player"], "position": c.get("position",""),
                    "team": c["team"], "league_id": c["league_id"],
                    "fixture": best_fixture,
                    "price": best_price, "team_ml": best_ml, "market": best_market or "Player Shots On Target",
                    "series": c["series"], "w5": c.get("w5") or [], "w7": c.get("w7") or [],
                    "c5": c.get("c5",0), "c7": c.get("c7",0),
                })
        return had_market, priced_meet, len(kept), kept

    a_mkt, a_prc, a_kept, A_hits = evaluate(cand_A)
    b_mkt, b_prc, b_kept, B_hits = evaluate(cand_B)
    print(f"[MKT]  A had_sot_market_in_event={a_mkt}   B had_sot_market_in_event={b_mkt}")
    print(f"[PRICE] A priced_meet={a_prc} kept={a_kept}   B priced_meet={b_prc} kept={b_kept}")

    # Deduplicate by player+team, prefer A over B; sort by price desc
    seen = set(); A_final, B_final = [], []
    for x in sorted(A_hits, key=lambda r: (-r["price"], r["player"])):
        key = norm(x["player"]) + "|" + norm(x["team"])
        if key in seen: continue
        seen.add(key); A_final.append(x)
    for x in sorted(B_hits, key=lambda r: (-r["price"], r["player"])):
        key = norm(x["player"]) + "|" + norm(x["team"])
        if key in seen: continue
        seen.add(key); B_final.append(x)

    _render(A_final, B_final)

def _render(A_final: List[dict], B_final: List[dict], empty_reason: str=""):
    lines = []
    lines.append(f"Generated at (UTC): {dt.datetime.utcnow().isoformat()} | Min price: {MIN_PRICE:.2f} | Team ML < {TEAM_ML_MAX:.2f} | Window: {WINDOW_DAYS}d")
    lines.append("Market: Bet365 — Player Shots On Target Over 0.5 (strict; excludes halves/outside-box).")
    lines.append("")
    def fmt_row(x):
        pos = f"[{x['position']}]" if x.get("position") else ""
        ser5 = ",".join(map(str, (x.get("w5") or [])[:5])); ser7 = ",".join(map(str, (x.get("w7") or [])[:7]))
        return (f" • {x['player']} {pos} — {x['team']} | {x['fixture']} | "
                f"Over 0.5 @ {x['price']:.3f} | Team ML {x['team_ml']:.3f} | "
                f"last5: {x.get('c5',0)}/5 (series5: {ser5}) | last7: {x.get('c7',0)}/7 (series7: {ser7})")
    if A_final:
        lines.append("===== CERTS — SOT 1+ (100% last 5, n≥5) =====")
        for x in A_final: lines.append(fmt_row(x))
        lines.append("")
    if B_final:
        lines.append("===== CERTS — SOT 1+ (6 of last 7, n≥7) =====")
        for x in B_final: lines.append(fmt_row(x))
        lines.append("")
    if not A_final and not B_final:
        lines.append("No SOT candidates matched after price/ML filters." + (f" ({empty_reason})" if empty_reason else ""))
    OUT_FILE.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    print("\n".join(lines))

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
