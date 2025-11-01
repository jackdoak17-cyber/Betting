# scripts/build_team_series.py
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Build per-team LAST_N series from Sportmonks v3 — single script that outputs:
  1) Team stats (shots, SOT, fouls, tackles, cards_total, saves, goal_kicks, corners)
  2) Opponent-allowed stats (prefixed with 'opp_')
Both include a `locations_last_n` array aligned to the series ("home"/"away"/"unknown").

Outputs (same shapes/paths as your existing collectors):
  - data/team_stats/by_league/{league_id}.json
  - data/team_stats/combined.json
  - data/team_stats/summary.txt
  - data/team_opponent_stats/by_league/{league_id}.json
  - data/team_opponent_stats/combined.json
  - data/team_opponent_stats/summary.txt

Env:
  SPORTMONKS_TOKEN (required)
  TEAM_STATS_LAST_N          (default 10)
  TEAM_OPP_STATS_LAST_N      (default 10)
  INCLUDE_SECOND_YELLOW_IN_CARDS (0/1, default 0)
  # Optional overrides for type IDs if Sportmonks changes them:
  TEAM_STAT_SHOTS_TOTAL_ID, TEAM_STAT_SHOTS_ON_TARGET_ID, TEAM_STAT_FOULS_ID,
  TEAM_STAT_TACKLES_ID, TEAM_STAT_YELLOW_CARDS_ID, TEAM_STAT_RED_CARDS_ID,
  TEAM_STAT_SAVES_ID, TEAM_STAT_GOAL_KICKS_ID, TEAM_STAT_CORNERS_ID

Runtime toggle:
  SERIES_MODE = "both" | "team" | "opp"    (default: both)
"""

import os, json, time, datetime as dt
from pathlib import Path
from typing import Dict, List, Tuple, Optional
import requests

# -------- API config --------
API_BASE = "https://api.sportmonks.com/v3/football"
API_TOKEN = (
    os.getenv("SPORTMONKS_TOKEN")
    or os.getenv("SPORTMONKS_API_TOKEN")
    or os.getenv("SM_TOKEN")
)
if not API_TOKEN:
    raise SystemExit("ERROR: SPORTMONKS_TOKEN / SPORTMONKS_API_TOKEN not set.")

TIMEOUT = 25
RETRIES = 3
BACKOFF = 1.6
GLOBAL_MIN_DELAY = 0.18  # gentle pacing

# -------- Type IDs (override via env if needed) --------
def _env_int(key:str, default:str) -> int:
    try:
        return int(os.getenv(key, default))
    except Exception:
        return int(default)

SHOTS_TOTAL      = _env_int("TEAM_STAT_SHOTS_TOTAL_ID", "42")
SHOTS_ON_TARGET  = _env_int("TEAM_STAT_SHOTS_ON_TARGET_ID", "86")
FOULS            = _env_int("TEAM_STAT_FOULS_ID", "56")
TACKLES          = _env_int("TEAM_STAT_TACKLES_ID", "78")
YELLOW           = _env_int("TEAM_STAT_YELLOW_CARDS_ID", "84")
RED              = _env_int("TEAM_STAT_RED_CARDS_ID", "83")
SECOND_YELLOW    = _env_int("TEAM_STAT_SECOND_YELLOWS_ID", "85")  # optional, opponent cards sum
SAVES            = _env_int("TEAM_STAT_SAVES_ID", "57")
GOAL_KICKS       = _env_int("TEAM_STAT_GOAL_KICKS_ID", "53")
CORNERS          = _env_int("TEAM_STAT_CORNERS_ID", "34")

TEAM_LAST_N = _env_int("TEAM_STATS_LAST_N", "10")
OPP_LAST_N  = _env_int("TEAM_OPP_STATS_LAST_N", "10")
INCLUDE_SECOND_YELLOW_IN_CARDS = os.getenv("INCLUDE_SECOND_YELLOW_IN_CARDS", "0") in ("1","true","TRUE","yes","YES")
SERIES_MODE = os.getenv("SERIES_MODE", "both").strip().lower() or "both"  # both|team|opp

# -------- IO paths --------
PX_DIR        = Path("data/predicted_xi/by_league")
TS_ROOT       = Path("data/team_stats");            TS_BY_LEAGUE  = TS_ROOT / "by_league"
OPP_ROOT      = Path("data/team_opponent_stats");   OPP_BY_LEAGUE = OPP_ROOT / "by_league"
for p in (TS_BY_LEAGUE, OPP_BY_LEAGUE):
    p.mkdir(parents=True, exist_ok=True)

# -------- HTTP helpers --------
_last_call = 0.0
def _pace():
    global _last_call
    now = time.time()
    if now - _last_call < GLOBAL_MIN_DELAY:
        time.sleep(GLOBAL_MIN_DELAY - (now - _last_call))
    _last_call = time.time()

def api_get(path: str, params: Optional[dict] = None) -> dict:
    if params is None:
        params = {}
    params = {**params, "api_token": API_TOKEN}
    url = f"{API_BASE}/{path.lstrip('/')}"
    last_exc = None
    for i in range(1, RETRIES + 1):
        _pace()
        try:
            r = requests.get(url, params=params, timeout=TIMEOUT)
            if r.status_code == 429:
                sleep = min(60, (BACKOFF ** i) * 2.0)
                print(f"[429] {path} — sleeping {sleep:.1f}s")
                time.sleep(sleep)
                continue
            r.raise_for_status()
            return r.json()
        except Exception as e:
            last_exc = e
            if i < RETRIES:
                sleep = BACKOFF ** i
                print(f"[RETRY] {path} (attempt {i}) sleeping {sleep:.1f}s")
                time.sleep(sleep)
            else:
                raise
    raise last_exc

# -------- small helpers --------
def today_utc_date() -> dt.date:
    return dt.datetime.now(dt.timezone.utc).date()

def dstr(d: dt.date) -> str:
    return d.strftime("%Y-%m-%d")

def ensure_dir(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)

def load_target_teams() -> Dict[Tuple[int,int], Dict[int,str]]:
    """
    Find target (league, season) -> {team_id: team_name} from predicted_xi files.
    Falls back to fixture lookup for missing season_id.
    """
    teams: Dict[Tuple[int,int], Dict[int,str]] = {}
    if not PX_DIR.exists():
        return teams

    for f in PX_DIR.glob("*.json"):
        try:
            blob = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        lid = int(blob.get("league_id") or 0)
        for row in (blob.get("fixtures") or []):
            fid = row.get("fixture_id")
            season_id = row.get("season_id") or None
            if season_id is None and isinstance(fid, int):
                try:
                    fx = api_get(f"fixtures/{fid}").get("data") or {}
                    season_id = int(fx.get("season_id") or 0) or None
                except Exception:
                    pass
            if not (lid and season_id):
                continue
            key = (int(lid), int(season_id))
            mm = teams.setdefault(key, {})
            for side in ("home","away"):
                s = row.get(side) or {}
                tid, nm = s.get("team_id"), (s.get("name") or "").strip()
                if isinstance(tid, int) and nm:
                    mm.setdefault(int(tid), nm)
    return teams

def get_season_bounds(season_id: int) -> Tuple[dt.date, dt.date]:
    """Fetch season start/end (clamp end to today)."""
    j = api_get(f"seasons/{season_id}")
    data = j.get("data") or {}
    def _take(s: Optional[str]) -> Optional[str]:
        if not s: return None
        return s.split("T")[0].split(" ")[0]
    start_s = _take(data.get("starting_at"))
    end_s   = _take(data.get("ending_at"))
    start = dt.datetime.strptime(start_s, "%Y-%m-%d").date() if start_s else today_utc_date().replace(month=8, day=1)
    end   = min(today_utc_date(), dt.datetime.strptime(end_s, "%Y-%m-%d").date() if end_s else today_utc_date())
    return start, end

# -------- home/away inference --------
def _infer_location_from_part(part: dict, default: Optional[str] = None) -> Optional[str]:
    meta = (part.get("meta") or {}) if isinstance(part, dict) else {}
    loc = (meta.get("location") or part.get("location") or "").strip().lower()
    if loc.startswith("home") or loc.startswith("local"):
        return "home"
    if loc.startswith("away") or loc.startswith("visitor") or loc.startswith("visit"):
        return "away"
    return default

def infer_location(fx: dict, team_id: int) -> Optional[str]:
    parts = fx.get("participants")
    # list form
    if isinstance(parts, list) and parts:
        for idx, p in enumerate(parts):
            pid = p.get("id") or p.get("team_id")
            try:
                pid = int(pid)
            except Exception:
                continue
            if pid == team_id:
                loc = _infer_location_from_part(p)
                if loc: return loc
                if idx == 0: return "home"
                if idx == 1: return "away"
        return None
    # dict form
    if isinstance(parts, dict):
        for hk in ("home","localteam","local","home_team"):
            d = parts.get(hk)
            if isinstance(d, dict):
                pid = d.get("id") or d.get("team_id")
                try:
                    if int(pid) == team_id: return "home"
                except Exception:
                    pass
        for ak in ("away","visitorteam","visitor","away_team"):
            d = parts.get(ak)
            if isinstance(d, dict):
                pid = d.get("id") or d.get("team_id")
                try:
                    if int(pid) == team_id: return "away"
                except Exception:
                    pass
    return None

# -------- API fetchers --------
def fetch_team_fixtures_window(team_id: int, start: dt.date, end: dt.date, league_id: int, type_ids: List[int], page: int = 1) -> dict:
    """
    GET fixtures for team in [start,end] with league & statistic type filters, ordered desc.
    Includes participants to infer home/away.
    """
    path = f"fixtures/between/{dstr(start)}/{dstr(end)}/{team_id}"
    params = {
        "include": "participants;statistics;state",
        "filters": f"fixtureStatisticTypes:{','.join(str(x) for x in type_ids)};fixtureLeagues:{league_id}",
        "order": "desc",
        "per_page": 50,
        "page": page,
    }
    return api_get(path, params)

# -------- Team stats collector --------
def collect_team_series(league_id: int, season_id: int, team_id: int, last_n: int) -> dict:
    type_ids = list({SHOTS_TOTAL, SHOTS_ON_TARGET, FOULS, TACKLES, YELLOW, RED, SAVES, GOAL_KICKS, CORNERS})
    start_season, end_today = get_season_bounds(season_id)
    end = end_today

    series = {
        "shots_total": [], "shots_on_target": [], "fouls": [], "tackles": [],
        "cards_total": [], "saves": [], "goal_kicks": [], "corners": []
    }
    fixture_ids: List[int] = []
    locations: List[str] = []

    def have_enough() -> bool:
        return all(len(series[k]) >= last_n for k in series.keys())

    while end >= start_season and not have_enough():
        win_start = max(start_season, end - dt.timedelta(days=99))
        page = 1
        has_more = True
        while has_more and not have_enough():
            j = fetch_team_fixtures_window(team_id, win_start, end, league_id, type_ids, page=page)
            data = j.get("data") or []
            meta = j.get("meta") or {}
            has_more = bool(meta.get("has_more"))
            page += 1

            for fx in data:
                if int(fx.get("league_id") or 0) != league_id:  continue
                if int(fx.get("season_id") or 0) != season_id:   continue
                if int(fx.get("state_id") or 0) not in (5,):     continue  # finished

                fid = int(fx.get("id") or 0)
                by_type: Dict[int,int] = {}
                for s in (fx.get("statistics") or []):
                    try:
                        if int(s.get("participant_id") or 0) != team_id:
                            continue
                        t = int(s.get("type_id") or 0)
                        vobj = s.get("data") or s.get("value") or {}
                        val = vobj.get("value") if isinstance(vobj, dict) else None
                        if val is None: continue
                        by_type[t] = int(float(val))
                    except Exception:
                        continue

                cards_total = int(by_type.get(YELLOW, 0)) + int(by_type.get(RED, 0))

                series["shots_total"].append(int(by_type.get(SHOTS_TOTAL, 0)))
                series["shots_on_target"].append(int(by_type.get(SHOTS_ON_TARGET, 0)))
                series["fouls"].append(int(by_type.get(FOULS, 0)))
                series["tackles"].append(int(by_type.get(TACKLES, 0)))
                series["cards_total"].append(cards_total)
                series["saves"].append(int(by_type.get(SAVES, 0)))
                series["goal_kicks"].append(int(by_type.get(GOAL_KICKS, 0)))
                series["corners"].append(int(by_type.get(CORNERS, 0)))
                fixture_ids.append(fid)

                loc = infer_location(fx, team_id) or "unknown"
                locations.append(loc)

                if have_enough(): break
        end = win_start - dt.timedelta(days=1)

    # clamp latest->older
    for k in series: series[k] = series[k][:last_n]
    fixture_ids = fixture_ids[:last_n]
    locations   = locations[:last_n]
    return {"stats": series, "fixtures": fixture_ids, "locations": locations}

# -------- Opponent-allowed collector --------
def _parse_start_ts(fx: dict) -> int:
    st = fx.get("starting_at")
    if isinstance(st, dict):
        ts = st.get("timestamp") or st.get("ts")
        if isinstance(ts, (int,float)): return int(ts)
        dt_str = st.get("date_time") or st.get("date") or st.get("starting_at")
        if isinstance(dt_str, str):
            try:
                s = dt_str.replace("Z","").replace("T"," ")
                return int(dt.datetime.strptime(s[:19], "%Y-%m-%d %H:%M:%S").timestamp())
            except Exception:
                pass
    tm = fx.get("time")
    if isinstance(tm, dict):
        ts = tm.get("timestamp") or (tm.get("starting_at") or {}).get("timestamp")
        if isinstance(ts, (int,float)): return int(ts)
        dt_str = (tm.get("starting_at") or {}).get("date_time") or tm.get("starting_at")
        if isinstance(dt_str, str):
            try:
                s = dt_str.replace("Z","").replace("T"," ")
                return int(dt.datetime.strptime(s[:19], "%Y-%m-%d %H:%M:%S").timestamp())
            except Exception:
                pass
    return 0

def _opponent_id_from_fixture(fx: dict, team_id: int) -> Optional[int]:
    parts = fx.get("participants")
    if isinstance(parts, list) and len(parts) >= 2:
        ids = [int(p.get("id")) for p in parts if p.get("id") is not None]
        others = [i for i in ids if i != team_id]
        if others: return others[0]
    stats = fx.get("statistics") or []
    participants = {int(s.get("participant_id")) for s in stats if s.get("participant_id") is not None}
    others = [i for i in participants if i != team_id]
    if others: return others[0]
    return None

def collect_opponent_series(league_id: int, season_i
