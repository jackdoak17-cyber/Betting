#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Common helpers used by fixtures_lineups.py, stats_shots.py, and odds scripts.

Exports:
- API constants:
    API_BASE, SPORT, API_TOKEN
    DATE_FMT, LINEUP_TYPE_STARTER, APPEARANCE_MINUTES_THRESHOLD
- HTTP layer with retry/backoff + tiny memo:
    api_get()
- Date & path helpers:
    today_utc(), days_ahead(d, n), daterange_str(start, end_inclusive)
    ensure_dir(path), run_date_dir(base="data", d=None)
- General helpers:
    pos_id_to_label(), safe_int(), write_jsonl(path, rows), append_jsonl(path, row)
- Fixture/day helpers:
    fixtures_by_date(date_str, league_filter=None) -> List[dict]
    pick_home_away(participants) -> (home, away)
- XI helpers:
    team_last_fixture_with_xi(team_id, league_id) -> dict|None
    fixture_lineups_minutes_shots(fixture_id) -> (lineups_map, shots_map, minutes_map)
- Recent fixtures + player series:
    team_recent_league_fixtures(team_id, league_id, want) -> List[dict]
    player_last_n_shots_series(team_id, player_id, n, league_id) -> List[int]
- Stats:
    compute_hit_rate(series) -> float
"""

from __future__ import annotations

import os
import json
import time
import datetime as dt
from typing import Dict, List, Optional, Tuple, Iterable

import requests

# ===================== API / CONFIG =====================

API_BASE = "https://api.sportmonks.com/v3"
SPORT = "football"
API_TOKEN = os.getenv("SPORTMONKS_TOKEN", os.getenv("SPORTMONKS_API_TOKEN", "YOUR_TOKEN_HERE"))

DATE_FMT = "%Y-%m-%d"
LINEUP_TYPE_STARTER = 11
APPEARANCE_MINUTES_THRESHOLD = 45

TIMEOUT = 25
RETRIES = 4
BACKOFF = 1.9
WAIT_ON_RATE_LIMIT = True  # gentle sleep inside a single job if we hit 429s

# ===================== tiny memo cache =====================

class _Memo:
    def __init__(self):
        self.store: Dict[str, dict] = {}
    def get(self, key: str):
        return self.store.get(key)
    def set(self, key: str, value: dict):
        self.store[key] = value

_memo = _Memo()

def _cached_get(url: str, params: Optional[dict] = None) -> dict:
    if params is None:
        params = {}
    params = {**params, "api_token": API_TOKEN}

    key = url + "?" + "&".join(f"{k}={params[k]}" for k in sorted(params))
    hit = _memo.get(key)
    if hit is not None:
        return hit

    last_exc: Optional[Exception] = None
    sleep_for = 0.0

    for attempt in range(1, RETRIES + 1):
        if sleep_for > 0:
            time.sleep(sleep_for)
        try:
            r = requests.get(url, params=params, timeout=TIMEOUT)

            if r.status_code == 429 and WAIT_ON_RATE_LIMIT:
                reset_hdr = r.headers.get("x-ratelimit-reset") or r.headers.get("X-RateLimit-Reset")
                if reset_hdr:
                    try:
                        reset_val = float(reset_hdr)
                        now = time.time()
                        # Some APIs send a unix ts; some send seconds-until-reset. We clamp to [3, 120]s.
                        wait = reset_val - (now if reset_val > 1e9 else 0)
                        sleep_for = max(3.0, min(wait, 120.0))
                    except Exception:
                        sleep_for = min(90.0, (BACKOFF ** attempt) + 2.0)
                else:
                    sleep_for = min(90.0, (BACKOFF ** attempt) + 2.0)
                last_exc = requests.HTTPError(f"429 rate limited for {r.url}")
                continue

            if r.status_code >= 400:
                try:
                    jerr = r.json()
                except Exception:
                    jerr = {"message": r.text[:300]}
                raise requests.HTTPError(f"{r.status_code} {r.reason} for {r.url}\nResponse JSON: {jerr}")

            j = r.json()
            _memo.set(key, j)
            return j

        except Exception as e:
            last_exc = e
            sleep_for = (BACKOFF ** attempt) + (0.15 * attempt)
            if attempt >= RETRIES:
                break

    if last_exc:
        raise last_exc
    raise RuntimeError("Unknown error in _cached_get")

def api_get(path: str, params: Optional[dict] = None) -> dict:
    if API_TOKEN == "YOUR_TOKEN_HERE" or not API_TOKEN:
        raise RuntimeError("SPORTMONKS_TOKEN not set. Add repository secret or env var.")
    if params is None:
        params = {}
    url = f"{API_BASE}/{SPORT}/{path.lstrip('/')}"
    return _cached_get(url, params)

# ===================== Utilities =====================

def pos_id_to_label(position_id: Optional[int]) -> str:
    return {24: "GK", 25: "DEF", 26: "MID", 27: "FWD"}.get(int(position_id or 0), "?")

def safe_int(x, default: Optional[int] = None) -> Optional[int]:
    try:
        return int(x)
    except Exception:
        return default

def ensure_dir(path: str) -> str:
    os.makedirs(path, exist_ok=True)
    return path

def run_date_dir(base: str = "data", d: Optional[dt.date] = None) -> str:
    """
    Returns a path like data/YYYY-MM-DD and creates it.
    """
    if d is None:
        d = today_utc()
    p = os.path.join(base, d.strftime(DATE_FMT))
    ensure_dir(p)
    return p

def write_jsonl(path: str, rows: Iterable[dict]) -> None:
    ensure_dir(os.path.dirname(path) or ".")
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

def append_jsonl(path: str, row: dict) -> None:
    ensure_dir(os.path.dirname(path) or ".")
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")

# ===================== Date helpers =====================

def today_utc() -> dt.date:
    return dt.datetime.now(dt.timezone.utc).date()

def days_ahead(d: dt.date, n: int) -> dt.date:
    return d + dt.timedelta(days=n)

def daterange_str(start: dt.date, end_inclusive: dt.date) -> List[str]:
    out: List[str] = []
    d = start
    while d <= end_inclusive:
        out.append(d.strftime(DATE_FMT))
        d += dt.timedelta(days=1)
    return out

# ===================== Fixtures by day =====================

def fixtures_by_date(date_str: str, league_filter: Optional[set] = None) -> List[dict]:
    """
    Fetch all fixtures for a date. Optionally filter by league id set.
    Includes participants; state; league (for labeling).
    """
    params = {
        "include": "participants;state;league",
        "order": "asc",
        "page": 1,
    }
    j = api_get(f"fixtures/date/{date_str}", params)
    data = j.get("data", []) or []
    meta = j.get("meta") or {}
    last_page = int(meta.get("last_page", 1) or 1)

    for p in range(2, last_page + 1):
        params["page"] = p
        jp = api_get(f"fixtures/date/{date_str}", params)
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

def pick_home_away(participants: List[dict]) -> Tuple[Optional[dict], Optional[dict]]:
    home = next((p for p in participants if (p.get("meta") or {}).get("location") == "home"), None)
    away = next((p for p in participants if (p.get("meta") or {}).get("location") == "away"), None)
    return home, away

# ===================== XI helpers =====================

def team_last_fixture_with_xi(team_id: int, league_id: int) -> Optional[dict]:
    """
    Return the team's most recent fixture in this LEAGUE that has recorded starters.
    Scans 'latest' first, then walks back by date (≤180 days).
    Allows last-season because we don't restrict by season_id.
    """
    # Try team latest first
    try:
        j = api_get(f"teams/{team_id}", {"include": "latest.league;latest.lineups;latest.lineups.player"})
        latest = j.get("data", {}).get("latest")
        lst = latest if isinstance(latest, list) else ([latest] if latest else [])
        candidates = [fx for fx in lst if (fx or {}).get("league_id") == league_id]
        candidates.sort(key=lambda x: x.get("starting_at") or "", reverse=True)

        for fx in candidates:
            fid = fx.get("id")
            if not fid:
                continue
            full = api_get(f"fixtures/{fid}", {"include": "lineups;lineups.player"}).get("data", {})
            if any(l.get("type_id") == LINEUP_TYPE_STARTER and l.get("team_id") == team_id
                   for l in (full.get("lineups") or [])):
                full["participants"] = fx.get("participants") or []
                return full
    except Exception:
        pass

    # Walk back by date
    start = today_utc()
    for back in range(1, 181):
        d = (start - dt.timedelta(days=back)).strftime(DATE_FMT)
        try:
            fxs = fixtures_by_date(d, league_filter={league_id})
        except Exception:
            continue
        for fx in fxs:
            parts = fx.get("participants") or []
            if not any(p.get("id") == team_id for p in parts):
                continue
            fid = fx.get("id")
            if not fid:
                continue
            full = api_get(f"fixtures/{fid}", {"include": "lineups;lineups.player"}).get("data", {})
            if any(l.get("type_id") == LINEUP_TYPE_STARTER and l.get("team_id") == team_id
                   for l in (full.get("lineups") or [])):
                full["participants"] = parts
                return full

    return None

# ===================== Lineups + stats for a fixture =====================

_SHOT_DEVS_TOTAL = {"SHOTS", "SHOTS_TOTAL"}
_SHOT_DEVS_SOT   = {"SHOTS_ON_TARGET"}
_SHOT_DEVS_SOFF  = {"SHOTS_OFF_TARGET"}
_MINUTES_DEVS    = {"MINUTES_PLAYED", "MINUTES"}

def _num_from_detail(det: dict) -> int:
    v = (det.get("data") or {}).get("value")
    if isinstance(v, dict):
        if "total" in v:
            try:
                return int(v["total"] or 0)
            except Exception:
                return 0
        s = 0
        for x in v.values():
            if isinstance(x, (int, float)):
                s += int(x)
        return s
    try:
        return int(v or 0)
    except Exception:
        return 0

def fixture_lineups_minutes_shots(fixture_id: int) -> Tuple[Dict[int, dict], Dict[int, int], Dict[int, int]]:
    """
    GET fixtures/{id}?include=lineups.details.type

    Returns:
        lineups_map: {player_id: lineup_row}
        shots_map:   {player_id: total_shots}
        minutes_map: {player_id: minutes_played}
    """
    j = api_get(f"fixtures/{fixture_id}", {"include": "lineups.details.type"}).get("data", {})
    lineups = j.get("lineups") or []

    lineups_map: Dict[int, dict] = {}
    shots_map: Dict[int, int] = {}
    minutes_map: Dict[int, int] = {}

    for lp in lineups:
        pid = safe_int(lp.get("player_id"))
        if pid is None:
            continue
        lineups_map[pid] = lp

        total_from_api: Optional[int] = None
        sot = soff = 0
        mins: Optional[int] = None

        for det in (lp.get("details") or []):
            t = det.get("type") or {}
            dev = (t.get("developer_name") or "").upper()

            if dev in _SHOT_DEVS_TOTAL:
                total_from_api = _num_from_detail(det)
            elif dev in _SHOT_DEVS_SOT:
                sot += _num_from_detail(det)
            elif dev in _SHOT_DEVS_SOFF:
                soff += _num_from_detail(det)
            elif dev in _MINUTES_DEVS:
                mins_val = _num_from_detail(det)
                mins = mins_val if mins is None else max(mins, mins_val)

        if mins is not None:
            minutes_map[pid] = mins
        if (total_from_api is not None) or (sot + soff) > 0:
            shots_map[pid] = total_from_api if total_from_api is not None else (sot + soff)

    return lineups_map, shots_map, minutes_map

# ===================== Recent fixtures & player series =====================

def team_recent_league_fixtures(team_id: int, league_id: int, want: int) -> List[dict]:
    """
    Recent fixtures for TEAM in LEAGUE, newest→oldest, scanning up to ~2 years.
    """
    collected: List[dict] = []
    seen = set()

    # Seed using 'latest'
    try:
        j = api_get(f"teams/{team_id}", {"include": "latest.league"})
        latest = j.get("data", {}).get("latest")
        lst = latest if isinstance(latest, list) else ([latest] if latest else [])
        for fx in lst:
            if fx and fx.get("league_id") == league_id and fx.get("id") not in seen:
                collected.append(fx); seen.add(fx.get("id"))
    except Exception:
        pass

    today = today_utc()
    for back in range(1, 731):
        d = (today - dt.timedelta(days=back)).strftime(DATE_FMT)
        try:
            fixtures = fixtures_by_date(d, league_filter={league_id})
        except Exception:
            continue
        for fx in fixtures:
            fid = fx.get("id")
            if not fid or fid in seen:
                continue
            if any(p.get("id") == team_id for p in (fx.get("participants") or [])):
                collected.append(fx); seen.add(fid)
        if len(collected) >= want * 14:
            break

    collected.sort(key=lambda x: x.get("starting_at") or "", reverse=True)
    return collected

def player_last_n_shots_series(team_id: int, player_id: int, n: int, league_id: int) -> List[int]:
    """
    Player's last n LEAGUE APPEARANCES (>=45'), across this+last season.
    If minutes missing, skip fixture. Record 0 if played >=45' but no shots stat.
    """
    fixtures = team_recent_league_fixtures(team_id, league_id, n)
    series: List[Tuple[str, int]] = []

    def consider(fx: dict) -> Optional[Tuple[str, int]]:
        fid = fx.get("id")
        if not fid:
            return None
        _, shots_map, minutes_map = fixture_lineups_minutes_shots(fid)
        mins = minutes_map.get(int(player_id))
        if mins is None or mins < APPEARANCE_MINUTES_THRESHOLD:
            return None
        shots = shots_map.get(int(player_id), 0)
        return (fx.get("starting_at") or "", shots)

    for fx in fixtures:
        if len(series) >= n:
            break
        try:
            res = consider(fx)
        except Exception:
            res = None
        if res:
            series.append(res)

    series.sort(key=lambda x: x[0], reverse=True)
    return [s for _, s in series][:n]

# ===================== Simple stat helper =====================

def compute_hit_rate(series: List[int]) -> float:
    if not series:
        return 0.0
    hits = sum(1 for x in series if x >= 1)
    return 100.0 * hits / len(series)
