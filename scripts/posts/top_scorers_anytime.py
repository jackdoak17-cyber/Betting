#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Boot & Book — Top Scorers × Anytime Odds
Path: scripts/posts/top_scorers_anytime.py

Inputs
------
- data/fixtures/by_league/{league_id}.json  (upcoming fixtures with season_id & participants)
- SPORTMONKS_TOKEN env var

Env (optional)
--------------
LEAGUE_IDS="8,82"           # comma-separated list of league IDs
TOP_N="10"                  # number of scorers to list
BOOKMAKER_IDS=""            # e.g. "2" for Bet365; comma sep for multiple
MARKET_IDS=""               # if you know the Anytime market id(s); else leave blank
ODDS_FALLBACK_ALL="1"       # scan all markets by name if MARKET_IDS is blank

Output
------
- reports/social/top_scorers_anytime_{YYYYMMDD}.md
"""

import os, json, re, time, datetime as dt
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Any
import requests

# ---------- API ----------
API_BASE = "https://api.sportmonks.com/v3/football"
API_TOKEN = (
    os.getenv("SPORTMONKS_TOKEN")
    or os.getenv("SPORTMONKS_API_TOKEN")
    or os.getenv("SM_TOKEN")
)
if not API_TOKEN:
    raise SystemExit("ERROR: SPORTMONKS_TOKEN not set")

LEAGUE_IDS = [int(x) for x in os.getenv("LEAGUE_IDS", "8").split(",") if x.strip()]
TOP_N = int(os.getenv("TOP_N", "10"))
BOOKMAKER_IDS = (os.getenv("BOOKMAKER_IDS", "").strip() or None)
MARKET_IDS = (os.getenv("MARKET_IDS", "").strip() or None)
ODDS_FALLBACK_ALL = (os.getenv("ODDS_FALLBACK_ALL", "1").strip() == "1")

TIMEOUT = 25
RETRIES = 3
BACKOFF = 1.7
_last_call = 0.0
PACE = 0.18  # gentle pacing

def _pace():
    global _last_call
    now = time.time()
    if now - _last_call < PACE:
        time.sleep(PACE - (now - _last_call))
    _last_call = time.time()

def api_get(path: str, params: Optional[dict] = None) -> dict:
    params = params or {}
    params["api_token"] = API_TOKEN
    url = f"{API_BASE}/{path.lstrip('/')}"
    err = None
    for i in range(RETRIES):
        _pace()
        try:
            r = requests.get(url, params=params, timeout=TIMEOUT)
            if r.status_code == 429:
                time.sleep(min(60, (BACKOFF ** (i+1)) * 2.0))
                continue
            r.raise_for_status()
            return r.json()
        except Exception as e:
            err = e
            if i + 1 < RETRIES:
                time.sleep(BACKOFF ** (i+1))
    raise err

# ---------- IO ----------
FIX_DIR = Path("data/fixtures/by_league")
OUT_DIR = Path("reports/social")
OUT_DIR.mkdir(parents=True, exist_ok=True)

# ---------- Fixtures helpers ----------
def _parse_dt(s: str) -> Optional[dt.datetime]:
    if not s:
        return None
    # Accept "YYYY-MM-DD HH:MM:SS" or ISO-like
    s = s.replace("T", " ").replace("Z", "")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return dt.datetime.strptime(s, fmt)
        except Exception:
            continue
    return None

def load_fixtures_blob(league_id: int) -> dict:
    p = FIX_DIR / f"{league_id}.json"
    if not p.is_file():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}

def infer_season_id_from_fixtures(blob: dict) -> Optional[int]:
    """Pick the most common season_id from the fixtures payload."""
    ctr: Dict[int, int] = {}
    for fx in (blob.get("fixtures") or []):
        sid = fx.get("season_id")
        if isinstance(sid, int) and sid > 0:
            ctr[sid] = ctr.get(sid, 0) + 1
    if not ctr:
        return None
    return max(ctr.items(), key=lambda kv: kv[1])[0]

def team_next_fixture_map(blob: dict) -> Dict[int, dict]:
    """
    Map team_id -> earliest upcoming fixture record for that team.
    Assumes blob contains upcoming fixtures.
    """
    now = dt.datetime.utcnow()
    best: Dict[int, dict] = {}

    for fx in (blob.get("fixtures") or []):
        fid = int(fx.get("id") or fx.get("fixture_id") or 0)
        ko = _parse_dt(str(fx.get("starting_at") or fx.get("starting_at_timestamp") or "")) or now
        # Build home/away/team mapping from 'participants'
        teams = []
        for p in (fx.get("participants") or []):
            try:
                tid = int(p.get("id"))
            except Exception:
                continue
            loc = ((p.get("meta") or {}).get("location") or "").lower()
            nm = p.get("name") or f"Team {tid}"
            teams.append({"team_id": tid, "name": nm, "loc": loc})
        if len(teams) != 2:
            continue
        home = next((t for t in teams if t["loc"] == "home"), teams[0])
        away = next((t for t in teams if t["loc"] == "away"), teams[-1])

        row = {
            "fixture_id": fid,
            "home_id": home["team_id"], "home": home["name"],
            "away_id": away["team_id"], "away": away["name"],
            "kickoff": ko,
        }
        for t in teams:
            prev = best.get(t["team_id"])
            if (prev is None) or (row["kickoff"] < prev["kickoff"]):
                best[t["team_id"]] = row
    return best

# ---------- Top scorers ----------
def fetch_topscorers_for_season(season_id: int) -> List[dict]:
    """
    Use Sportmonks topscorers endpoint; returns list of dicts:
      {player_id, player_name, team_id, team_name, goals}
    """
    j = api_get(f"topscorers/seasons/{season_id}",
                params={"include": "goalscorers.player;goalscorers.team", "per_page": 50})
    data = j.get("data") or {}
    rows = []
    for g in (data.get("goalscorers") or []):
        pl = (g.get("player") or {})
        tm = (g.get("team") or {})
        rows.append({
            "player_id": int(pl.get("id") or 0),
            "player_name": pl.get("name") or "",
            "team_id": int(tm.get("id") or 0),
            "team_name": tm.get("name") or "",
            "goals": int(g.get("goals") or g.get("total") or g.get("scored") or 0),
        })
    rows.sort(key=lambda r: (-r["goals"], r["player_name"].lower()))
    return rows

# ---------- Odds helpers ----------
ANYTIME_KEYS = ["anytime", "to score", "player to score", "goalscorer", "score at anytime"]
EXCLUDE_KEYS = ["first", "last", "2 or more", "two or more", "hat-trick", "hat trick"]

def looks_like_anytime_market(name: str) -> bool:
    s = (name or "").lower()
    if not s:
        return False
    if any(k in s for k in EXCLUDE_KEYS):
        return False
    return any(k in s for k in ANYTIME_KEYS)

def normalize(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())

def names_match(a: str, b: str) -> bool:
    na, nb = normalize(a), normalize(b)
    return na == nb or na in nb or nb in na

def pick_decimal_price(obj: dict) -> Optional[float]:
    for k in ("decimal", "price", "odd", "over", "yes", "value"):
        v = obj.get(k)
        try:
            if isinstance(v, (int, float)):
                return float(v)
            if isinstance(v, str):
                return float(v)
        except Exception:
            pass
    return None

def extract_anytime_price(markets: List[dict], player_name: str) -> Optional[Tuple[str, float]]:
    pn = normalize(player_name)
    for m in markets or []:
        mname = m.get("name") or ""
        if not looks_like_anytime_market(mname):
            continue
        odds = m.get("odds")
        # try list shape
        if isinstance(odds, list):
            for opt in odds:
                label = opt.get("label") or opt.get("name") or ""
                if names_match(pn, label):
                    price = pick_decimal_price(opt if isinstance(opt, dict) else {})
                    if price is not None:
                        bm = m.get("bookmaker_name") or m.get("bookmaker") or "Book"
                        return bm, price
        # try dict shape
        if isinstance(odds, dict):
            for label, opt in odds.items():
                if names_match(pn, str(label)):
                    price = pick_decimal_price(opt if isinstance(opt, dict) else {})
                    if price is not None:
                        bm = m.get("bookmaker_name") or m.get("bookmaker") or "Book"
                        return bm, price
    return None

def odds_for_fixture_anytime(fixture_id: int, player_name: str) -> Optional[Tuple[str, float]]:
    params = {"include": "odds"}
    fltrs = []
    if BOOKMAKER_IDS:
        fltrs.append(f"bookmakers:{BOOKMAKER_IDS}")
    if MARKET_IDS:
        fltrs.append(f"markets:{MARKET_IDS}")
    if fltrs:
        params["filters"] = ";".join(fltrs)

    j = api_get(f"fixtures/{fixture_id}", params=params)
    fx = j.get("data") or {}
    markets = fx.get("odds") or []

    # attach bookmaker_name if nested elsewhere (defensive)
    ms = []
    for m in markets:
        m = dict(m)
        if not m.get("bookmaker_name"):
            m["bookmaker_name"] = (m.get("bookmaker") or {}).get("name") or m.get("bookmaker_name") or "Book"
        ms.append(m)

    # direct attempt
    res = extract_anytime_price(ms, player_name)
    if res:
        return res

    # fallback: if MARKET_IDS unknown, scan all by name
    if not MARKET_IDS and ODDS_FALLBACK_ALL:
        return extract_anytime_price(ms, player_name)

    return None

# ---------- Main ----------
def main():
    today = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d")
    lines: List[str] = []
    lines.append("## Boot & Book — Top Scorers × Anytime Odds")
    lines.append("")

    for league_id in LEAGUE_IDS:
        blob = load_fixtures_blob(league_id)
        if not blob or not (blob.get("fixtures")):
            print(f"[WARN] No fixtures for league {league_id}; skipping.")
            continue

        season_id = infer_season_id_from_fixtures(blob)
        if not season_id:
            print(f"[WARN] Could not infer season_id for league {league_id}; skipping.")
            continue

        team_fx = team_next_fixture_map(blob)
        top_rows = fetch_topscorers_for_season(season_id)[:TOP_N]
        if not top_rows:
            print(f"[WARN] No top scorers returned for season {season_id}; league {league_id}.")
            continue

        lines.append(f"### League {league_id}")
        lines.append("")
        lines.append("| # | Player | Team | Goals | Anytime | Fixture | KO |")
        lines.append("|---:|:------|:-----|------:|:-------:|:-------|:---|")

        for rank, r in enumerate(top_rows, start=1):
            team_id = r["team_id"]
            fx = team_fx.get(team_id)
            price_txt = "—"
            fixture_txt = "—"
            ko_txt = "—"

            if fx:
                fixture_txt = f"{fx['home']} vs {fx['away']}"
                ko_txt = fx["kickoff"].strftime("%Y-%m-%d %H:%M")
                res = odds_for_fixture_anytime(fx["fixture_id"], r["player_name"])
                if res:
                    bm, price = res
                    price_txt = f"{price:.2f} ({bm})"

            lines.append(
                f"| {rank} | {r['player_name']} | {r['team_name']} | {r['goals']} | {price_txt} | {fixture_txt} | {ko_txt} |"
            )

        lines.append("")

    out = OUT_DIR / f"top_scorers_anytime_{today}.md"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"[OK] wrote {out}")

if __name__ == "__main__":
    main()