#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Value bets — SHOTS certs (1+ in 100% of last 7, min games=7)
Replaces external odds API with Sportmonks-only calls.

Source data (unchanged):
  data/player_shots/by_league/{league_id}.json
  data/predicted_xi/by_league/{league_id}.json   (for nicer team names)

Bookmaker: Bet365 only (resolved via Sportmonks odds/bookmakers search unless BET365_BOOKMAKER_ID is set)
Markets: Player Shots (NOT SOT, NOT 'outside the box', NOT halves, etc.)
Filters:
  - Player qualifies: last 7 matches all >= 1 shot (len(series) >= 7)
  - Price (Over 0.5) >= MIN_DEC_PRICE
  - Team ML (Bet365) for player's side < TEAM_WIN_MAX

Output:
  data/value_bets/shots_certs.txt + console

ENV (required):
  SPORTMONKS_TOKEN     # per docs, header Authorization: <token>

ENV (optional):
  MIN_DEC_PRICE        # default 1.30
  TEAM_WIN_MAX         # default 3.50
  BET365_BOOKMAKER_ID  # override if you know it; otherwise auto-search

NOTE: This version fixes the 404s by using the correct endpoints:
  - Bookmaker search:   /v3/odds/bookmakers/search/{query}
  - Fixtures (range):   /v3/football/fixtures/between/{start}/{end}
  - Pre-match odds:     /v3/football/odds/pre-match/fixtures/{fixture_id}/bookmakers/{bookmaker_id}
"""

import os, re, json, math, time, random, unicodedata, datetime as dt
from pathlib import Path
from typing import Dict, List, Tuple, Optional, Any
import requests
from itertools import islice

# ========= CONFIG =========
SM_BASE = "https://api.sportmonks.com/v3"

# ✅ Correct endpoints (previous 404s were due to wrong paths)
SM_FXT_RANGE = f"{SM_BASE}/football/fixtures/between"   # /between/{start}/{end}
SM_ODDS_PRE  = f"{SM_BASE}/football/odds/pre-match"
SM_BOOKMAKERS = f"{SM_BASE}/odds/bookmakers"            # /odds/bookmakers/search/{name}

TIMEOUT = 25

MIN_PRICE = float(os.getenv("MIN_DEC_PRICE", "1.30"))
TEAM_ML_MAX = float(os.getenv("TEAM_WIN_MAX", "3.50"))

HTTP_HEADERS = {
    "accept": "application/json",
    "user-agent": "sm-odds-shots-certs/1.1",
    # Per docs: header Authorization just the token (no "Bearer")
    "Authorization": os.getenv("SPORTMONKS_TOKEN", "").strip(),
}

if not HTTP_HEADERS["Authorization"]:
    raise SystemExit("ERROR: SPORTMONKS_TOKEN not set.")

ROOT = Path(".")
PX_DIR    = ROOT / "data" / "predicted_xi" / "by_league"
SHOTS_DIR = ROOT / "data" / "player_shots" / "by_league"
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

MATCH_WINNER_KEYS = {
    "1x2","match result","match results","match winner",
    "moneyline","full time result","to win","win/draw/win","wdw","ml","fulltime result"
}

# ========= STRING NORMALISATION & MATCHERS =========
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

def market_is_player_shots(name: str) -> bool:
    s = norm(name)
    return (bool(s) and "player" in s and "shot" in s and not any(b in s for b in NEGATIVE_SHOTS_TERMS))

# ========= IO HELPERS =========
def _load_json(p: Path) -> Any:
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None

def _team_name_map(league_id: int) -> Dict[int, str]:
    """Map team_id -> team_name from predicted_xi file (for nicer canonical names)."""
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

# ========= HTTP HELPERS (Sportmonks) =========
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

def get_bet365_id() -> Optional[int]:
    override = os.getenv("BET365_BOOKMAKER_ID", "").strip()
    if override.isdigit():
        return int(override)

    # ✅ Correct search endpoint lives under /odds/...
    r = http_get_with_retries(f"{SM_BOOKMAKERS}/search/bet365", params={})
    if r and r.status_code == 200:
        try:
            data = r.json().get("data") or []
            for bk in data:
                name = (bk.get("name") or "").strip().lower()
                if "bet365" == name or "bet 365" == name.replace(" ", "") or "bet365" in name:
                    return int(bk.get("id"))
        except Exception:
            pass

    print("[WARN] Could not resolve Bet365 ID via API; using 2 as a fallback.")
    return 2

# ========= FIXTURES & ODDS =========
def fixtures_for_leagues(leagues: List[int], days_ahead: int = 7) -> List[dict]:
    if not leagues:
        return []
    start = dt.datetime.utcnow().date()
    end = start + dt.timedelta(days=days_ahead)

    params = {
        "include": "participants",
        # Dynamic filter (valid for fixture entity)
        "filters": f"fixtureLeagues:{','.join(map(str, leagues))}",
        "per_page": 50,
    }

    all_fx = []
    # ✅ Correct path: .../fixtures/between/{start}/{end}
    url = f"{SM_FXT_RANGE}/{start:%Y-%m-%d}/{end:%Y-%m-%d}"

    while True:
        r = http_get_with_retries(url, params=params)
        if not (r and r.status_code == 200):
            break
        payload = r.json() or {}
        page = payload.get("data") or []
        all_fx.extend(page)
        meta = payload.get("meta") or {}
        if not meta.get("has_more"):
            break
        params = dict(params, page=int(meta.get("current_page", 1)) + 1)

    return all_fx

def fixture_teams(fx: dict) -> Tuple[str, str]:
    home, away = "", ""
    for p in (fx.get("participants") or []):
        loc = (p.get("meta") or {}).get("location")
        nm = p.get("name") or ""
        if (loc or "").lower() == "home":
            home = nm
        elif (loc or "").lower() == "away":
            away = nm
    return home, away

def get_prematch_odds_for_fixture_bookmaker(fixture_id: int, bookmaker_id: int) -> List[dict]:
    url = f"{SM_ODDS_PRE}/fixtures/{fixture_id}/bookmakers/{bookmaker_id}"
    r = http_get_with_retries(url, params={})
    if not (r and r.status_code == 200): return []
    try:
        data = r.json().get("data") or []
        # normalize numeric strings
        for o in data:
            for k in ("total", "handicap", "value"):
                v = o.get(k)
                if isinstance(v, str) and v.replace(".","",1).replace("-","",1).isdigit():
                    try: o[k] = float(v)
                    except: pass
        return data
    except Exception:
        return []

def min_win_prices(odds_rows: List[dict], home_name: str, away_name: str) -> Tuple[Optional[float], Optional[float]]:
    best_home = None; best_away = None
    for row in odds_rows:
        desc = (row.get("market_description") or "").lower()
        name = (row.get("name") or "").lower()
        label = (row.get("label") or "")
        if not any(k in desc for k in MATCH_WINNER_KEYS):
            continue
        if team_names_match(label, home_name) or name == "home" or label.strip().lower() == "home":
            try:
                v = float(row.get("value"))
                if best_home is None or v < best_home: best_home = v
            except: pass
        elif team_names_match(label, away_name) or name == "away" or label.strip().lower() == "away":
            try:
                v = float(row.get("value"))
                if best_away is None or v < best_away: best_away = v
            except: pass
    return best_home, best_away

def parse_player_over_point5_price(odds_rows: List[dict], player: str) -> Optional[Tuple[float, str]]:
    """
    Return (best_price, market_seen) for Over 0.5 player SHOTS (not SOT).
    """
    best = None; market_seen = None
    for row in odds_rows:
        desc = row.get("market_description") or ""
        if not market_is_player_shots(desc):
            continue
        label = row.get("label") or ""
        if not player_label_matches(player, label):
            continue
        # Accept total/handicap == 0.5 OR "(0.5)" in label
        total = row.get("total")
        hcap = row.get("handicap")
        label_line = None
        m = re.search(r"\(([-+]?\d+(?:\.\d+)?)\)", label)
        if m:
            try: label_line = float(m.group(1))
            except: label_line = None
        line = None
        for x in (total, hcap, label_line):
            if isinstance(x, (int,float)):
                line = float(x); break
        if line is None or not math.isclose(line, 0.5, abs_tol=1e-9):
            continue
        # Must be OVER
        name = (row.get("name") or "").strip().lower()
        if name not in {"over", "o"}:
            continue
        try:
            price = float(row.get("value"))
        except:
            continue
        if price >= MIN_PRICE and (best is None or price > best + 1e-9):
            best = price
            market_seen = desc or "Player Shots"
    if best is None:
        return None
    return best, (market_seen or "Player Shots")

# ========= MAIN =========
def main():
    if not HTTP_HEADERS['Authorization']:
        raise SystemExit("ERROR: SPORTMONKS_TOKEN not set.")

    bet365_id = get_bet365_id()
    if not isinstance(bet365_id, int):
        raise SystemExit("ERROR: Could not resolve Bet365 bookmaker id.")

    # 1) Candidates from local stats
    candidates = collect_candidates()
    if not candidates:
        print("[RESULT] No player candidates with 1+ in each of last 7.")
        OUT_FILE.write_text(
            f"Generated at (UTC): {dt.datetime.utcnow().isoformat()}  |  Min price: {MIN_PRICE:.2f}  |  Team ML < {TEAM_ML_MAX:.2f}\n"
            "Criteria: 1+ shot in 100% of last 7 (n>=7)  |  Market: Bet365 Player Shots Over 0.5\n\n"
            "No matches found.\n", encoding="utf-8"
        )
        return

    # 2) Fixtures (next 7 days) filtered to the leagues present in your local files
    lids_used = sorted({c["league_id"] for c in candidates})
    fixtures = fixtures_for_leagues(lids_used, days_ahead=7)
    print(f"[FIXTURES] Retrieved {len(fixtures)} fixtures across {len(lids_used)} leagues (next 7 days).")

    # Helper: link team to fixture ids
    def find_fixture_ids_for_team(lid: int, team: str) -> List[int]:
        out = []
        for fx in fixtures:
            if int(fx.get("league_id") or 0) != int(lid):
                continue
            home, away = fixture_teams(fx)
            if team_names_match(team, home) or team_names_match(team, away):
                if isinstance(fx.get("id"), int):
                    out.append(fx["id"])
        return out

    for c in candidates:
        c["fixture_ids"] = find_fixture_ids_for_team(c["league_id"], c["team"])

    fixture_ids = sorted({fid for c in candidates for fid in (c.get("fixture_ids") or [])})
    print(f"[ODDS] Unique fixtures to query (Bet365): {len(fixture_ids)}")

    if not fixture_ids:
        lines = []
        lines.append(f"Generated at (UTC): {dt.datetime.utcnow().isoformat()}  |  Min price: {MIN_PRICE:.2f}  |  Team ML < {TEAM_ML_MAX:.2f}")
        lines.append("Criteria: 1+ shot in 100% of last 7 (n>=7)  |  Market: Bet365 Player Shots Over 0.5")
        lines.append("")
        lines.append("No matches found.")
        OUT_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print("\n".join(lines))
        return

    # 3) Fetch odds for each fixture from Bet365
    odds_by_fixture: Dict[int, List[dict]] = {}
    for fid in fixture_ids:
        odds_by_fixture[fid] = get_prematch_odds_for_fixture_bookmaker(fid, bet365_id)

    # Build fixture info for printing
    fx_info = {}
    for fx in fixtures:
        try:
            fid = int(fx["id"])
        except Exception:
            continue
        h, a = fixture_teams(fx)
        fx_info[fid] = {
            "kickoff": (fx.get("starting_at") or "").replace("T"," ").replace("Z",""),
            "home": h,
            "away": a,
        }

    # 4) Apply filters
    flagged: List[dict] = []
    for c in candidates:
        for fid in c.get("fixture_ids") or []:
            rows = odds_by_fixture.get(fid) or []
            if not rows:
                continue
            fxn = fx_info.get(fid) or {}
            home, away = fxn.get("home",""), fxn.get("away","")
            best_home_ml, best_away_ml = min_win_prices(rows, home, away)
            side = "home" if team_names_match(c["team"], home) else ("away" if team_names_match(c["team"], away) else None)
            if not side:
                continue
            team_ml = best_home_ml if side == "home" else best_away_ml
            if team_ml is None or team_ml >= TEAM_ML_MAX:
                continue
            shot = parse_player_over_point5_price(rows, c["player"])
            if not shot:
                continue
            best_price, market_seen = shot
            flagged.append({
                "player": c["player"], "position": c["position"], "team": c["team"],
                "fixture": f"{home} vs {away}",
                "kickoff": fxn.get("kickoff") or "",
                "price": best_price, "team_ml": team_ml,
                "series": c["series"], "league_id": c["league_id"], "market": market_seen,
            })

    # 5) Render
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
