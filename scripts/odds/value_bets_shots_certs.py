#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Value bets — SHOTS certs (1+ in 100% of last 7, min games=7)
Source data: data/player_shots/by_league/{league_id}.json (+ predicted XI for team names)
Bookmaker: Bet365 only
Markets: Player Shots (NOT SOT, NOT 'outside the box', NOT halves, etc.)
Filters:
  - Player qualifies: last 7 matches all >=1 shot (len(series)>=7)
  - Price Over 0.5 >= 1.30
  - Team ML (Bet365) for player's side < 3.50
Output: data/value_bets/shots_certs.txt + console

ENV:
  ODDS_API_KEY (required)

Notes:
  - Events fetched by league slug; odds fetched in batches of 10 (odds/multi).
  - Team/event and player/label matching is tolerant to small naming diffs.
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

# Limit to the leagues you care about (expand if needed)
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
HTTP_HEADERS = {"accept": "application/json", "user-agent": "odds-shots-certs/1.0"}
TIMEOUT = 25

ROOT = Path(".")
PX_DIR    = ROOT / "data" / "predicted_xi" / "by_league"   # team_id -> name map
SHOTS_DIR = ROOT / "data" / "player_shots" / "by_league"   # per-league player shots histories
OUT_DIR   = ROOT / "data" / "value_bets"; OUT_DIR.mkdir(parents=True, exist_ok=True)
OUT_FILE  = OUT_DIR / "shots_certs.txt"

# ========= MARKET FILTERS (shots-only, exclude SOT/outside/halves etc.) =========
NEGATIVE_SHOTS_TERMS = {
    "on target","sot","outside","outside box","outside of box","from outside","outside the box",
    "header","headers","head","left foot","right foot","right-foot","left-foot",
    "first half","1st half","2nd half","second half","half",
    "distance","long range","goal","goals","to score","assist","assists","ga","g/a",
    "shots on target","on-target","from corner","from free kick","penalty","penalties"
}

def market_is_player_shots(name: str) -> bool:
    s = re.sub(r"\s+", " ", (name or "")).strip().lower()
    return (bool(s) and "player" in s and "shot" in s and not any(b in s for b in NEGATIVE_SHOTS_TERMS))

# ========= STRING NORMALISATION =========
def strip_accents(s: str) -> str:
    return ''.join(c for c in unicodedata.normalize('NFD', s or '') if unicodedata.category(c) != 'Mn')

def norm(s: str) -> str:
    s = strip_accents(s or "").lower()
    s = re.sub(r"[^a-z0-9\s\.-]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s

GENERIC_TEAM_TOKENS = {"fc","cf","afc","sc","cd","ud","ac","as","ss","ssc","us","uc","rc","rcd","ca","the","club","de","del","la","las","los","calcio","united","city","saint","st","bk"}
def team_tokens(name: str):
    toks = set(norm(name).split())
    return {t for t in toks if t not in GENERIC_TEAM_TOKENS}

def team_names_match(a: str, b: str) -> bool:
    if not a or not b: return False
    ta, tb = team_tokens(a), team_tokens(b)
    if not ta or not tb: return False
    if ta == tb: return True
    if ta.issubset(tb) or tb.issubset(ta): return True
    inter = ta & tb; union = ta | tb
    if len(inter) / max(1, len(union)) >= 0.5: return True
    if len(inter) >= 2: return True
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

def discover_leagues() -> List[int]:
    lids = set()
    for p in SHOTS_DIR.glob("*.json"):
        try: lids.add(int(p.stem))
        except: pass
    return sorted(lids)

def last7_all_one_plus(series: List[int]) -> bool:
    seq = [x for x in series if isinstance(x, int)]
    if len(seq) < 7: return False
    sub = seq[:7]  # assume series is newest -> older
    return all(x >= 1 for x in sub)

def collect_candidates() -> List[dict]:
    """
    Expect per-league shots files to include per-player 'series' (or 'shots_last_n').
    Fallback keys supported: 'series', 'shots_last_n', 'shots'.
    """
    out = []
    for lid in discover_leagues():
        shots_blob = _load_json(SHOTS_DIR / f"{lid}.json") or {}
        team_map = _team_name_map(lid)
        players = shots_blob.get("players") or shots_blob.get("rows") or shots_blob.get("data") or []
        for rec in players:
            series = rec.get("series") or rec.get("shots_last_n") or rec.get("shots") or []
            if not isinstance(series, list): continue
            if not last7_all_one_plus(series): continue
            player = rec.get("name") or rec.get("player_name") or rec.get("player")
            if not player: continue
            tid = rec.get("team_id")
            team = rec.get("team") or rec.get("team_name") or (team_map.get(int(tid)) if isinstance(tid, int) else None)
            if not team: continue
            pos = rec.get("position") or rec.get("pos")
            out.append({
                "league_id": lid, "player": player, "team": team,
                "position": pos or "", "series": series[:10]
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

def parse_line(opt: dict) -> Optional[float]:
    if "hdp" in opt:
        try: return float(opt["hdp"])
        except: pass
    m = re.search(r"\(([-+]?\d+(?:\.\d+)?)\)", opt.get("label") or "")
    if m:
        try: return float(m.group(1))
        except: return None
    return None

def parse_over_price(opt: dict) -> Optional[float]:
    val = opt.get("over") if isinstance(opt, dict) else None
    try: return float(val)
    except: return None

MATCH_WINNER_KEYS = ["1x2","match result","match winner","moneyline","full time result","to win","win/draw/win","wdw","ml"]
def market_is_match_winner(name: str) -> bool:
    s = (name or "").strip().lower()
    return bool(s) and any(k in s for k in MATCH_WINNER_KEYS)

def min_win_prices(ev: dict) -> Tuple[Optional[float], Optional[float]]:
    best_home = None; best_away = None
    for m in bet365_markets(ev):
        if not market_is_match_winner(m.get("name","")): continue
        odds = m.get("odds") or []
        for row in odds:
            # row may be {"home": "...", "draw": "...", "away": "..."}
            try:
                h = float(row.get("home")) if row.get("home") not in (None, "N/A") else None
                a = float(row.get("away")) if row.get("away") not in (None, "N/A") else None
            except: h = a = None
            if isinstance(h, float):
                best_home = h if (best_home is None or h < best_home) else best_home
            if isinstance(a, float):
                best_away = a if (best_away is None or a < best_away) else best_away
    return best_home, best_away

# ========= MAIN =========
def main():
    api_key = os.getenv("ODDS_API_KEY")
    if not api_key:
        raise SystemExit("ERROR: ODDS_API_KEY not set.")

    # 1) Build candidate list from your player_shots data
    candidates = collect_candidates()
    if not candidates:
        print("[RESULT] No player candidates with 1+ in each of last 7.")
        return

    # 2) Fetch events per league
    lids_used = sorted({c["league_id"] for c in candidates})
    slug_used = {lid: LEAGUE_SLUG_BY_ID.get(lid) for lid in lids_used if LEAGUE_SLUG_BY_ID.get(lid)}
    events_by_league: Dict[int, List[dict]] = {}
    for lid, slug in slug_used.items():
        evs = get_events_for_league(slug, api_key)
        events_by_league[lid] = evs
        print(f"[EVENTS] {slug}: {len(evs)}")

    # 3) Map each candidate to its event id(s)
    def find_event_id_for_team(lid: int, team: str) -> List[int]:
        evs = events_by_league.get(lid, [])
        out = []
        for ev in evs:
            if team_names_match(team, ev.get("home","")) or team_names_match(team, ev.get("away","")):
                if isinstance(ev.get("id"), int):
                    out.append(ev["id"])
        return out

    for c in candidates:
        c["event_ids"] = find_event_id_for_team(c["league_id"], c["team"])

    event_ids = sorted({eid for c in candidates for eid in (c.get("event_ids") or [])})
    print(f"[ODDS] Unique events to query: {len(event_ids)}")

    # 4) Fetch odds for all events (batches of 10)
    odds_payloads: List[dict] = []
    for i, batch in enumerate(chunked(event_ids, 10), start=1):
        print(f"[ODDS] batch {i} — {len(batch)} ids")
        odds_payloads.extend(get_odds_multi(batch, api_key))
    if not odds_payloads:
        print("[RESULT] No odds payloads; stopping.")
        return
    id_to_ev = {o.get("id"): o for o in odds_payloads if isinstance(o.get("id"), int)}

    # 5) Scan markets for Player Shots Over 0.5 and apply ML filter
    flagged: List[dict] = []
    for c in candidates:
        for ev_id in c.get("event_ids") or []:
            ev = id_to_ev.get(ev_id)
            if not ev: continue
            home, away = ev.get("home",""), ev.get("away","")
            # Team ML filter first
            best_home_ml, best_away_ml = min_win_prices(ev)
            side = "home" if team_names_match(c["team"], home) else ("away" if team_names_match(c["team"], away) else None)
            if not side: 
                continue
            team_ml = best_home_ml if side == "home" else best_away_ml
            if team_ml is None or team_ml >= TEAM_ML_MAX:
                continue

            # Now find best Over 0.5 price in Player Shots (exclude SOT/Outside etc.)
            best_price = None
            market_seen = None
            for m in bet365_markets(ev):
                name = m.get("name","")
                if not market_is_player_shots(name): 
                    continue
                odds = m.get("odds")
                if isinstance(odds, list):
                    for opt in odds:
                        label = opt.get("label")
                        if not player_label_matches(c["player"], label):
                            continue
                        line = parse_line(opt)
                        if line is None or not math.isclose(line, 0.5, abs_tol=1e-6):
                            continue
                        price = parse_over_price(opt)
                        if price is None: 
                            continue
                        if price >= MIN_PRICE and (best_price is None or price > best_price + 1e-9):
                            best_price = price
                            market_seen = name
                elif isinstance(odds, dict):
                    # per-player dict format
                    for label, opt in odds.items():
                        if not player_label_matches(c["player"], label):
                            continue
                        line = parse_line(opt if isinstance(opt, dict) else {})
                        if line is None or not math.isclose(line, 0.5, abs_tol=1e-6):
                            continue
                        price = parse_over_price(opt if isinstance(opt, dict) else {})
                        if price is None:
                            continue
                        if price >= MIN_PRICE and (best_price is None or price > best_price + 1e-9):
                            best_price = price
                            market_seen = name

            if best_price is not None:
                flagged.append({
                    "player": c["player"], "position": c["position"], "team": c["team"],
                    "fixture": f"{home} vs {away}",
                    "kickoff": (ev.get("date") or "").replace("T"," ").replace("Z",""),
                    "price": best_price, "team_ml": team_ml,
                    "series": c["series"], "league_id": c["league_id"], "market": market_seen or "Player Shots",
                })

    # 6) Render
    flagged.sort(key=lambda x: (-x["price"], x["player"]))
    lines = []
    lines.append(f"Generated at (UTC): {dt.datetime.utcnow().isoformat()}  |  Min price: {MIN_PRICE:.2f}  |  Team ML < {TEAM_ML_MAX:.2f}")
    lines.append("Criteria: 1+ shot in 100% of last 7 (n>=7)  |  Market: Bet365 Player Shots Over 0.5")
    lines.append("")

    if not flagged:
        lines.append("No matches found.")
    else:
        lines.append("===== CERTS — Player Shots 1+ =====")
        for x in flagged:
            ser = ",".join(map(str, x["series"][:7]))
            pos = f"[{x['position']}]" if x.get("position") else ""
            lines.append(
                f" • {x['player']} {pos} — {x['team']} | {x['fixture']} @ {x['kickoff']} | "
                f"Over 0.5 @ {x['price']:.3f} | Team ML {x['team_ml']:.3f} | series7: {ser}"
            )

    OUT_FILE.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    print("\n".join(lines))

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
