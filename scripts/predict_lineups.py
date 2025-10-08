#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Predict lineups per league (using each team’s last league fixture with an official XI)
and write:
  - data/predicted/by_league/{league_id}.json
  - data/predicted/summary.txt

Enhancement: uses Sportmonks "detailed position" to label defenders as RB/LB/CB (and WB etc.)
Falls back to coarse position (GK/DEF/MID/FWD) if detailed position is missing.

Env:
  export SPORTMONKS_TOKEN=YOUR_TOKEN

Rate-friendly:
- Reuses the fixture window we already fetched from disk.
- Only looks up each team once (last fixture with starters in this league).
"""

import os
import json
import time
import datetime as dt
from typing import Dict, List, Optional, Tuple

import requests

SM_API_BASE = "https://api.sportmonks.com/v3"
SM_SPORT = "football"
API_TOKEN = os.getenv("SPORTMONKS_TOKEN", "")

# Target leagues
LEAGUES: Dict[int, str] = {
    8:   "Premier League",
    9:   "Championship",
    82:  "Bundesliga",
    301: "Ligue 1",
    384: "Serie A",
    387: "Serie B",
    564: "La Liga",
    567: "La Liga 2",
    600: "Super Lig",
}

DATA_ROOT = "data"
FIX_ROOT_MAIN = os.path.join(DATA_ROOT, "fixtures")              # fallback
FIX_ROOT_BY_LEAGUE = os.path.join(DATA_ROOT, "fixtures", "by_league")
OUT_ROOT = os.path.join(DATA_ROOT, "predicted", "by_league")
SUMMARY_PATH = os.path.join(DATA_ROOT, "predicted", "summary.txt")

os.makedirs(OUT_ROOT, exist_ok=True)
os.makedirs(os.path.dirname(SUMMARY_PATH), exist_ok=True)

TIMEOUT = 25
RETRIES = 3
BACKOFF = 1.6
GLOBAL_MIN_DELAY = 0.12

LINEUP_TYPE_STARTER = 11  # Sportmonks: starters id

# ---------------------- HTTP helpers + tiny memo ----------------------
MEMO: Dict[str, dict] = {}

def _get(url: str, params: Optional[dict] = None) -> dict:
    if params is None:
        params = {}
    params = {**params, "api_token": API_TOKEN}
    key = url + "?" + "&".join(f"{k}={params[k]}" for k in sorted(params))

    if key in MEMO:
        return MEMO[key]

    last = getattr(_get, "_last_ts", 0.0)
    now = time.time()
    if now - last < GLOBAL_MIN_DELAY:
        time.sleep(GLOBAL_MIN_DELAY - (now - last))

    err = None
    for attempt in range(1, RETRIES + 1):
        try:
            r = requests.get(url, params=params, timeout=TIMEOUT)
            setattr(_get, "_last_ts", time.time())
            if r.status_code >= 400:
                try:
                    jerr = r.json()
                except Exception:
                    jerr = {"message": r.text[:300]}
                raise requests.HTTPError(f"{r.status_code} {r.reason} for {r.url}\n{jerr}")
            j = r.json()
            MEMO[key] = j
            return j
        except Exception as e:
            err = e
            if attempt < RETRIES:
                time.sleep(BACKOFF ** attempt)
            else:
                raise
    raise err

def sm_get(path: str, params: Optional[dict] = None) -> dict:
    return _get(f"{SM_API_BASE}/{SM_SPORT}/{path.lstrip('/')}", params)

# ---------------------- small utils ----------------------
def pos_group_from_id(pid: Optional[int]) -> str:
    return {24: "GK", 25: "DEF", 26: "MID", 27: "FWD"}.get(pid or 0, "?")

def norm_lower(s: Optional[str]) -> str:
    return (s or "").strip().lower()

def first_present(d: dict, *keys):
    for k in keys:
        if d.get(k) is not None:
            return d.get(k)
    return None

def code_from_detailed_position_name(name: Optional[str]) -> Optional[str]:
    """
    Map Sportmonks detailedPosition name/code to a short code.
    Primary goal: distinguish RB/LB/CB (+ wing-backs). Also a few mids/fwds niceties.
    """
    if not name:
        return None
    n = norm_lower(name)

    # defenders
    if "wing" in n and "back" in n:
        if "right" in n: return "RWB"
        if "left"  in n: return "LWB"
        return "WB"
    if "back" in n:
        if "right" in n: return "RB"
        if "left"  in n: return "LB"
        if "centre" in n or "center" in n or "central" in n: return "CB"
        return "CB"

    # optional: mids/fwds
    if "defensive" in n and "mid" in n: return "DM"
    if "attacking" in n and "mid" in n: return "AM"
    if "central" in n and "mid" in n:   return "CM"
    if "right" in n and "mid" in n:     return "RM"
    if "left"  in n and "mid" in n:     return "LM"
    if "right" in n and ("wing" in n or "forward" in n): return "RW"
    if "left"  in n and ("wing" in n or "forward" in n): return "LW"
    if "centre" in n and ("forward" in n or "striker" in n) or "striker" in n: return "ST"

    return None

def extract_detailed_position(lp: dict) -> Tuple[Optional[str], Optional[str]]:
    """
    Returns (code, name) from the lineup row using the included detailedPosition entity when available.
    """
    ent = first_present(lp, "detailedPosition", "detailedposition", "detailed_position")
    name = None
    code = None
    if isinstance(ent, dict):
        code = ent.get("code")
        name = ent.get("name") or ent.get("name_short") or ent.get("short_name") or code
    if not code:
        code = code_from_detailed_position_name(name)
    else:
        short = code_from_detailed_position_name(code)
        if short:
            code = short
    if not code:
        code = code_from_detailed_position_name(name)
    return code, name

# ---------------------- fixtures helpers ----------------------
def get_fixtures_for_date(date_str: str, league_filter: Optional[set] = None) -> List[dict]:
    params = {
        "include": "participants;state;league",
        "order": "asc",
        "page": 1,
    }
    j = sm_get(f"fixtures/date/{date_str}", params)
    data = j.get("data", []) or []
    meta = j.get("meta") or {}
    last_page = meta.get("last_page", 1)
    for p in range(2, last_page + 1):
        params["page"] = p
        jp = sm_get(f"fixtures/date/{date_str}", params)
        data.extend(jp.get("data", []) or [])
    out = []
    for fx in data:
        lid = fx.get("league_id")
        if league_filter and lid not in league_filter:
            continue
        if not fx.get("participants"):
            continue
        out.append(fx)
    return out

def pick_home_away(parts: List[dict]) -> Tuple[Optional[dict], Optional[dict]]:
    home = next((p for p in parts if (p.get("meta") or {}).get("location") == "home"), None)
    away = next((p for p in parts if (p.get("meta") or {}).get("location") == "away"), None)
    return home, away

def get_team_last_fixture_with_xi(team_id: int, league_id: int) -> Optional[dict]:
    """
    Try team/latest, else scan backward by date in this league to find a fixture with starters.
    IMPORTANT: when we fetch the full fixture, include detailedPosition & position on lineups.
    """
    try:
        j = sm_get(
            f"teams/{team_id}",
            {"include": "latest.league;latest.lineups;latest.lineups.player;latest.lineups.detailedPosition;latest.lineups.position"}
        )
        latest = j.get("data", {}).get("latest")
        lst = latest if isinstance(latest, list) else ([latest] if latest else [])
        candidates = [fx for fx in lst if (fx or {}).get("league_id") == league_id]
        candidates.sort(key=lambda x: x.get("starting_at") or "", reverse=True)
        for fx in candidates:
            fid = fx.get("id")
            if not fid:
                continue
            full = sm_get(
                f"fixtures/{fid}",
                {"include": "lineups;lineups.player;lineups.detailedPosition;lineups.position"}
            ).get("data", {})
            if any(l.get("type_id") == LINEUP_TYPE_STARTER and l.get("team_id") == team_id for l in (full.get("lineups") or [])):
                full["participants"] = fx.get("participants") or []
                return full
    except Exception:
        pass

    # Walk back by date (≤180 days)
    start = dt.datetime.now(dt.timezone.utc).date()
    for back in range(1, 181):
        d = (start - dt.timedelta(days=back)).strftime("%Y-%m-%d")
        try:
            fxs = get_fixtures_for_date(d, league_filter={league_id})
        except Exception:
            continue
        for fx in fxs:
            if any(p.get("id") == team_id for p in (fx.get("participants") or [])):
                full = sm_get(
                    f"fixtures/{fx['id']}",
                    {"include": "lineups;lineups.player;lineups.detailedPosition;lineups.position"}
                ).get("data", {})
                if any(l.get("type_id") == LINEUP_TYPE_STARTER and l.get("team_id") == team_id for l in (full.get("lineups") or [])):
                    full["participants"] = fx.get("participants") or []
                    return full
    return None

# ---------------------- main predict ----------------------
def build_predicted_xi_from_fixture(fixture: dict, team_id: int) -> List[dict]:
    """
    From a FULL fixture (already fetched with lineups.* includes), return sorted starters (<=11)
    with detailed and group positions resolved.
    """
    lineups = fixture.get("lineups") or []
    starters = [l for l in lineups if l.get("team_id") == team_id and l.get("type_id") == LINEUP_TYPE_STARTER]
    starters.sort(key=lambda x: x.get("formation_position") or 9999)
    out = []
    for lp in starters[:11]:
        pos_group = pos_group_from_id(lp.get("position_id"))
        code, name = extract_detailed_position(lp)
        out.append({
            "player_id": lp.get("player_id"),
            "player_name": (lp.get("player_name") or "").strip(),
            "jersey_number": lp.get("jersey_number"),
            "pos_group": pos_group,                # GK/DEF/MID/FWD
            "pos_detailed_code": code,             # e.g., RB/LB/CB/RWB/LWB when available
            "pos_detailed_name": name,             # e.g., "Right-Back"
            "formation_position": lp.get("formation_position"),
        })
    return out

def load_fixtures_league_file(league_id: int) -> Optional[dict]:
    """
    Try both locations so we’re robust no matter where fixtures were written.
    - data/fixtures/by_league/{lid}.json
    - data/fixtures/{lid}.json
    """
    p1 = os.path.join(FIX_ROOT_BY_LEAGUE, f"{league_id}.json")
    p2 = os.path.join(FIX_ROOT_MAIN, f"{league_id}.json")
    for path in (p1, p2):
        if os.path.isfile(path):
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
    return None

def predict_for_league(league_id: int, league_name: str) -> dict:
    fx_doc = load_fixtures_league_file(league_id) or {}
    fixtures = fx_doc.get("fixtures") or []
    window_start = fx_doc.get("window_start")
    window_end = fx_doc.get("window_end")

    # Collect unique teams from fixtures
    teams = []
    for fx in fixtures:
        parts = fx.get("participants") or []
        h, a = pick_home_away(parts)
        if h: teams.append(h["id"])
        if a: teams.append(a["id"])
    team_ids = sorted({t for t in teams if t})

    # If fixtures are empty, we still do nothing gracefully
    team_xi: Dict[int, List[dict]] = {}
    team_name: Dict[int, str] = {}

    for tid in team_ids:
        last = get_team_last_fixture_with_xi(tid, league_id)
        if not last:
            continue
        parts = last.get("participants") or []
        h, a = pick_home_away(parts)
        if h and h["id"] == tid:
            team_name[tid] = h.get("name")
        elif a and a["id"] == tid:
            team_name[tid] = a.get("name")

        xi = build_predicted_xi_from_fixture(last, tid)
        team_xi[tid] = xi
        time.sleep(0.05)

    # Build per-fixture predictions inside this league file (output remains per-league)
    out_fixtures = []
    for fx in fixtures:
        parts = fx.get("participants") or []
        h, a = pick_home_away(parts)
        if not (h and a):
            continue
        home_id, away_id = h["id"], a["id"]
        out_fixtures.append({
            "fixture_id": fx.get("id"),
            "starting_at": fx.get("starting_at"),
            "home": {
                "team_id": home_id,
                "team_name": h.get("name"),
                "predicted_xi": team_xi.get(home_id, []),
            },
            "away": {
                "team_id": away_id,
                "team_name": a.get("name"),
                "predicted_xi": team_xi.get(away_id, []),
            },
        })

    return {
        "league_id": league_id,
        "league_name": league_name,
        "window_start": window_start,
        "window_end": window_end,
        "fixtures": out_fixtures,
    }

def write_json(path: str, payload: dict) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)

def write_summary(leagues_payload: List[dict]) -> None:
    lines = []
    ts = dt.datetime.now(dt.timezone.utc).isoformat()
    lines.append(f"Time (UTC): {ts}")
    for lp in leagues_payload:
        lines.append(f"\n===== {lp['league_name']} ({lp['league_id']}) =====")
        for fx in lp.get("fixtures", []):
            lines.append(f"\n{fx['starting_at']} — {fx['home']['team_name']} vs {fx['away']['team_name']}")
            for side in ("home", "away"):
                team = fx[side]
                lines.append(f"  {team['team_name']} predicted XI:")
                xi = team.get("predicted_xi") or []
                if not xi:
                    lines.append("    (no data)")
                    continue
                for p in xi:
                    tag = p.get("pos_detailed_code") or p.get("pos_group") or "?"
                    jno = p.get("jersey_number")
                    nm = p.get("player_name")
                    lines.append(f"    - #{jno} {nm} [{tag}]")
    with open(SUMMARY_PATH, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

def main():
    if not API_TOKEN:
        raise SystemExit("SPORTMONKS_TOKEN is not set.")

    all_payloads = []
    for lid, lname in LEAGUES.items():
        path = os.path.join(OUT_ROOT, f"{lid}.json")
        try:
            payload = predict_for_league(lid, lname)
            write_json(path, payload)
            all_payloads.append(payload)
        except Exception as e:
            print(f"[WARN] league {lid}: {e}")

    write_summary(all_payloads)
    print(f"Wrote {len(all_payloads)} league files to {OUT_ROOT}")
    print(f"Summary: {SUMMARY_PATH}")

if __name__ == "__main__":
    main()
