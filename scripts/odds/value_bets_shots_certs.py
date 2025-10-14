#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Find CERTS for Player Total Shots 1+ (Over 0.5):
- Read data/player_shots/combined.json
- Keep players with 1+ total shot in EACH of last 7 matches (7/7)
- Map each player to their next fixture (from data/fixtures/latest.json)
- Fetch Over 0.5 shots price (best across allowed bookmakers)
- Drop players with price < ODDS_MIN_PRICE
- Print a tidy report and write data/player_shots/certs_shots1plus.json

Backends (auto):
  SPORTMONKS: requires SPORTMONKS_TOKEN and fixture_id → odds endpoint
  ODDS_API:   requires ODDS_API_KEY and player/event mapping (see notes)

ENV (override as you like):
  ODDS_MIN_PRICE=1.30
  ODDS_BOOKMAKERS="bet365,kambi"     # lower-case names
  ODDS_SOURCE="auto"                  # "auto" | "sportmonks" | "oddsapi"

Assumptions about files:
  - data/player_shots/combined.json:
      * Flexible shape. We look for a per-player record that contains either:
        - "series7" (list of last 7 total-shot counts), OR
        - per-match rows with "date" and "shots" / "total_shots" to compute last-7.
      * We try to read player name and team from keys like:
        "player_name"/"name", "team_name"/"team"/"team_short", "player_id"
        Optional: "fixture_id" (next match), "event_id" (odds api), etc.
  - data/fixtures/latest.json:
      * Produced by your fetcher, with "fixtures" list each having "id", "participants"
        and "starting_at" / "starting_at_timestamp".
"""

import os, sys, json, re, unicodedata, time, math
from pathlib import Path
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
import requests

# -------------------- CONFIG --------------------
ROOT = Path(".")
COMBINED = ROOT / "data" / "player_shots" / "combined.json"
FIXTURES = ROOT / "data" / "fixtures" / "latest.json"
OUT_JSON = ROOT / "data" / "player_shots" / "certs_shots1plus.json"

SPORTMONKS_TOKEN = (
    os.getenv("SPORTMONKS_TOKEN")
    or os.getenv("SPORTMONKS_API_TOKEN")
    or os.getenv("SM_TOKEN")
)
ODDS_API_KEY = os.getenv("ODDS_API_KEY")

ODDS_MIN_PRICE = float(os.getenv("ODDS_MIN_PRICE", "1.30"))
ALLOWED_BOOKMAKERS = {
    b.strip().lower() for b in os.getenv("ODDS_BOOKMAKERS", "bet365,kambi").split(",") if b.strip()
}
ODDS_SOURCE = os.getenv("ODDS_SOURCE", "auto").lower()  # auto | sportmonks | oddsapi

# Market name heuristics for “player total shots”
PLAYER_SHOTS_MARKET_KEYS = [
    "player shots",
    "player total shots",
    "total shots by player",
    "shots by player",
    "player to have 1+ shot",  # yes/no style
]

# -------------------- UTILS ---------------------
def norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    return re.sub(r"\s+", " ", s).strip().lower()

def safe_get(d: dict, *keys, default=None):
    for k in keys:
        if isinstance(d, dict) and k in d and d[k] is not None:
            return d[k]
    return default

def read_json(path: Path) -> Any:
    if not path.is_file():
        return None
    with path.open("r", encoding="utf-8", errors="ignore") as f:
        return json.load(f)

def parse_series7_from_player(p: dict) -> Optional[List[int]]:
    """
    Try several shapes to get last-7 total shot counts.
    Returns a list of 7 ints if available, else None.
    """
    # direct keys
    for k in ("series7", "shots_series7", "last7", "shots_last7"):
        arr = p.get(k)
        if isinstance(arr, list) and len(arr) >= 7:
            try:
                last7 = [int(round(float(x))) for x in arr[-7:]]
                return last7
            except Exception:
                pass

    # from match rows
    rows = p.get("matches") or p.get("last_matches") or p.get("games") or []
    # try to sort by date if present
    def row_key(r):
        d = r.get("date") or r.get("match_date") or r.get("starting_at")
        if d:
            try:
                return datetime.fromisoformat(str(d).replace("Z", "+00:00"))
            except Exception:
                pass
        return datetime.min.replace(tzinfo=timezone.utc)
    if isinstance(rows, list) and rows:
        rows_sorted = sorted(rows, key=row_key)[-7:]
        vals = []
        for r in rows_sorted:
            v = safe_get(r, "shots", "total_shots", "shots_total", default=None)
            if v is None:
                return None
            try:
                vals.append(int(round(float(v))))
            except Exception:
                return None
        if len(vals) == 7:
            return vals
    return None

def is_7of7_oneplus(series7: List[int]) -> bool:
    return len(series7) >= 7 and all((x or 0) >= 1 for x in series7[-7:])

def load_players_from_combined() -> List[dict]:
    data = read_json(COMBINED)
    if data is None:
        print(f"ERROR: missing {COMBINED}", file=sys.stderr)
        sys.exit(2)

    # Combined may be dict or list
    players = []
    if isinstance(data, dict):
        # common shapes: {"players":[...]} or {"data":[...]} or {id: {...}}
        if "players" in data and isinstance(data["players"], list):
            players = data["players"]
        elif "data" in data and isinstance(data["data"], list):
            players = data["data"]
        else:
            # dict of id -> player
            players = [v for v in data.values() if isinstance(v, dict)]
    elif isinstance(data, list):
        players = data
    else:
        print("ERROR: combined.json unknown shape", file=sys.stderr)
        sys.exit(2)

    out = []
    for p in players:
        series7 = parse_series7_from_player(p)
        if series7 is None:
            continue
        name = safe_get(p, "player_name", "name", "player", default="").strip()
        team = safe_get(p, "team_name", "team", "team_short", "club", default="").strip()
        team_id = safe_get(p, "team_id", "club_id", default=None)
        player_id = safe_get(p, "player_id", "id", default=None)
        out.append({
            "raw": p,
            "player_name": name,
            "team_name": team,
            "team_id": team_id,
            "player_id": player_id,
            "series7": series7[-7:]
        })
    return out

def load_fixtures() -> List[dict]:
    j = read_json(FIXTURES) or {}
    return j.get("fixtures") or []

def find_next_fixture_for_team(team_name: str, fixtures: List[dict]) -> Optional[dict]:
    tkey = norm(team_name)
    now_ts = int(time.time())
    best = None
    for fx in fixtures:
        ts = int(safe_get(fx, "starting_at_timestamp", default=0) or 0)
        if ts < now_ts:
            continue
        parts = fx.get("participants") or []
        names = [norm(safe_get(p, "name", default="")) for p in parts]
        if tkey in names:
            if best is None or ts < int(safe_get(best, "starting_at_timestamp", default=1<<60)):
                best = fx
    return best

# -------------------- ODDS: SPORTMONKS --------------------
def sm_get_fixture_odds(fixture_id: int) -> Optional[dict]:
    """
    Fetch all odds for a fixture (Sportmonks).
    NOTE: include path/shape can vary by plan; we attempt a few.
    Return a generic dict with bookmakers -> markets -> options.
    """
    if not SPORTMONKS_TOKEN:
        return None

    base = "https://api.sportmonks.com/v3/football/fixtures"
    params_list = [
        # Try pre-expanded odds include (most common)
        {"include": "odds.bookmakers.markets.options"},
        {"include": "odds;odds.bookmakers;odds.bookmakers.markets;odds.bookmakers.markets.options"},
        # Fallback: dedicated odds endpoint if available (some tenants use /v3/odds/fixtures/{id})
        {},
    ]
    for params in params_list:
        try:
            if params:
                r = requests.get(f"{base}/{fixture_id}", params={**params, "api_token": SPORTMONKS_TOKEN}, timeout=25)
                r.raise_for_status()
                j = r.json()
                # try common paths
                odds = safe_get(j, "data", default={}).get("odds") or j.get("odds")
            else:
                # possible dedicated odds endpoint
                r = requests.get(f"https://api.sportmonks.com/v3/odds/fixtures/{fixture_id}",
                                 params={"api_token": SPORTMONKS_TOKEN}, timeout=25)
                r.raise_for_status()
                j = r.json()
                odds = j.get("data") or j
            if odds:
                return odds  # raw; parser below will normalize
        except Exception:
            continue
    return None

def sm_extract_over05_for_player(odds_obj: dict, player_name: str) -> List[Tuple[str, float]]:
    """
    From Sportmonks odds structure, extract best Over 0.5 price for player across allowed bookmakers.
    Returns list of (bookmaker_name, price) found.
    """
    want = norm(player_name)
    found = []

    # Possible shapes: a list of {bookmaker: {...}, markets:[...]} or nested dicts
    books = []
    if isinstance(odds_obj, list):
        books = odds_obj
    elif isinstance(odds_obj, dict):
        # sometimes odds_obj = {"bookmakers":[...]}
        if isinstance(odds_obj.get("bookmakers"), list):
            books = odds_obj["bookmakers"]
        else:
            # maybe already a bookmaker item
            books = [odds_obj]

    for b in books:
        bname = norm(safe_get(b, "name", "bookmaker_name", default=""))
        if bname not in ALLOWED_BOOKMAKERS:
            continue
        markets = b.get("markets") or safe_get(b, "data", "markets", default=[])
        if not markets:
            continue
        for m in markets:
            mname = norm(safe_get(m, "name", "market_name", default=""))
            if not any(key in mname for key in PLAYER_SHOTS_MARKET_KEYS):
                continue
            # options/selections can vary
            opts = m.get("options") or m.get("selections") or []
            for opt in opts:
                # Try to detect player field
                pname = norm(
                    safe_get(opt, "player_name", "player", "participant", "name", default="")
                )
                if pname and want not in pname:
                    # Some books put "Over 0.5 - John Smith"; we still need the player to be present
                    if want not in pname:
                        continue

                # Look for Over 0.5 framing
                label = norm(safe_get(opt, "label", "name", "outcome", default=""))
                line = None
                for lk in ("line", "handicap", "value", "hdp"):
                    v = opt.get(lk)
                    if v is None:
                        continue
                    try:
                        line = float(v)
                        break
                    except Exception:
                        pass

                price = None
                # Explicit "over" price
                if line is not None and abs(line - 0.5) < 1e-9:
                    ov = safe_get(opt, "over", "price_over", "odds_over", default=None)
                    if ov is not None:
                        try:
                            price = float(ov)
                        except Exception:
                            pass

                # Or label contains "over 0.5"
                if price is None and "over 0.5" in label:
                    pv = safe_get(opt, "price", "odds", "decimal", "decimal_odds", default=None)
                    if pv is not None:
                        try:
                            price = float(pv)
                        except Exception:
                            pass

                # Or yes/no: "To have 1+ shot – Yes"
                if price is None and ("1+ shot" in label or "to have 1+ shot" in label or "1+ shots" in label):
                    yn = norm(safe_get(opt, "selection", "choice", "result", "label", default=""))
                    if "yes" in yn or "over" in yn or "to score" in yn:  # allow "Yes"
                        pv = safe_get(opt, "price", "odds", "decimal", "decimal_odds", default=None)
                        if pv is not None:
                            try:
                                price = float(pv)
                            except Exception:
                                pass

                if price is not None:
                    found.append((bname, price))
    return found

# -------------------- ODDS: ODDS-API.IO --------------------
def oddsapi_get_event_odds(event_id: int, bookmakers_csv: str) -> Optional[dict]:
    if not ODDS_API_KEY:
        return None
    url = "https://api.odds-api.io/v3/odds"
    try:
        r = requests.get(url, params={
            "apiKey": ODDS_API_KEY,
            "eventId": int(event_id),
            "bookmakers": bookmakers_csv
        }, timeout=25)
        r.raise_for_status()
        return r.json()
    except Exception:
        return None

def oddsapi_extract_over05_for_player(j: dict, player_name: str) -> List[Tuple[str, float]]:
    want = norm(player_name)
    out = []
    if not isinstance(j, dict):
        return out
    # Typical shape (simplified): {"bookmakers":[{"name":"Bet365","markets":[{"name":"Player Shots","options":[...]}]}]}
    books = j.get("bookmakers") or []
    for b in books:
        bname = norm(safe_get(b, "name", default=""))
        if bname not in ALLOWED_BOOKMAKERS:
            continue
        markets = b.get("markets") or []
        for m in markets:
            mname = norm(safe_get(m, "name", default=""))
            if not any(key in mname for key in PLAYER_SHOTS_MARKET_KEYS):
                continue
            opts = m.get("options") or []
            for opt in opts:
                pname = norm(safe_get(opt, "player", "player_name", "name", default=""))
                if want and want not in pname:
                    continue
                line = None
                for lk in ("line", "handicap", "value", "hdp"):
                    v = opt.get(lk)
                    if v is None: continue
                    try:
                        line = float(v); break
                    except Exception: pass

                price = None
                if line is not None and abs(line - 0.5) < 1e-9:
                    ov = safe_get(opt, "over", "price_over", default=None)
                    if ov is not None:
                        try: price = float(ov)
                        except Exception: pass
                if price is None:
                    label = norm(safe_get(opt, "label", "name", default=""))
                    if "over 0.5" in label or "1+ shot" in label or "1+ shots" in label:
                        pv = safe_get(opt, "price", "odds", "decimal", default=None)
                        if pv is not None:
                            try: price = float(pv)
                            except Exception: pass
                if price is not None:
                    out.append((bname, price))
    return out

# -------------------- MAIN --------------------
def main():
    players_all = load_players_from_combined()
    fixtures = load_fixtures()

    # 1) Only players who are 7/7 for 1+ shot
    eligible = [p for p in players_all if is_7of7_oneplus(p["series7"])]
    if not eligible:
        print("No players are 7/7 for 1+ shots based on combined.json.")
        sys.exit(0)

    # 2) Attach next fixture and odds
    results = []
    for p in eligible:
        name = p["player_name"]
        team = p["team_name"]
        if not name or not team:
            continue

        fx = find_next_fixture_for_team(team, fixtures)
        if not fx:
            # No upcoming match in the window → skip
            continue

        fixture_id = safe_get(fx, "id", default=None)
        home = None; away = None
        for pp in fx.get("participants") or []:
            loc = ((pp.get("meta") or {}).get("location") or "").lower()
            if loc == "home": home = pp.get("name")
            if loc == "away": away = pp.get("name")

        # --- Try to fetch odds ---
        prices: List[Tuple[str, float]] = []

        backend_order = []
        if ODDS_SOURCE == "sportmonks":
            backend_order = ["sportmonks"]
        elif ODDS_SOURCE == "oddsapi":
            backend_order = ["oddsapi"]
        else:
            backend_order = ["sportmonks", "oddsapi"]

        for be in backend_order:
            if be == "sportmonks" and SPORTMONKS_TOKEN and fixture_id:
                j = sm_get_fixture_odds(int(fixture_id))
                if j:
                    prices = sm_extract_over05_for_player(j, name)
            elif be == "oddsapi" and ODDS_API_KEY:
                # Need eventId in player object or raw combined row
                event_id = safe_get(p["raw"], "event_id", "odds_event_id", default=None)
                if event_id is not None:
                    jj = oddsapi_get_event_odds(int(event_id), ",".join(sorted(ALLOWED_BOOKMAKERS)))
                    if jj:
                        prices = oddsapi_extract_over05_for_player(jj, name)

            if prices:
                break  # got something

        if not prices:
            # Couldn't find a valid price; skip
            continue

        # Keep best price (max)
        best_bk, best_price = max(prices, key=lambda x: x[1] or 0.0)
        if best_price is None or best_price < ODDS_MIN_PRICE:
            continue

        # Optional: try to pull a Match Winner price (nice to have; best-effort)
        team_ml = None
        # We’ll leave ML extraction as best-effort in Sportmonks parse; can be extended later.

        results.append({
            "player": name,
            "team": team,
            "fixture_id": fixture_id,
            "home": home, "away": away,
            "kickoff": safe_get(fx, "starting_at", default=None),
            "series7": p["series7"],
            "bookmaker": best_bk,
            "price_over05": round(float(best_price), 3),
            "team_ml": team_ml,
        })

    # Sort by price desc
    results.sort(key=lambda r: (r.get("price_over05") or 0.0), reverse=True)

    # Write JSON and print a pretty report
    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    with OUT_JSON.open("w", encoding="utf-8") as f:
        json.dump({"generated_at": datetime.now(timezone.utc).isoformat(), "items": results}, f, ensure_ascii=False, indent=2)

    # Console
    print("===== CERTS — Player Shots 1+ =====")
    for r in results:
        when = r.get("kickoff") or ""
        print(f" • {r['player']}  — {r['team']} | {r['home']} vs {r['away']} @ {when} | Over 0.5 @ {r['price_over05']:.3f} | series7: {','.join(str(x) for x in r['series7'])}")

if __name__ == "__main__":
    main()
