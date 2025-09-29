#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Common helpers for SportMonks harvesting.

Provides:
- HTTP client with memo + on-disk cache (+ retry/backoff, 429-aware)
- Date helpers
- League helpers
- Fixture discovery (by date, by team, ranges, convenience fixtures_by_date)
- Lineups/minutes/shots extraction for a fixture
- Team recent league fixtures (across seasons, ~2y lookback)
- Player last-N league appearances (>=45') shots series

Environment:
  SPORTMONKS_TOKEN = your v3 token (required)

Cache:
  - In-memory memoization for the current run
  - File cache in ".cache_smonks/" (safe to keep between GitHub Action runs)
"""

from __future__ import annotations

import os
import sys
import json
import time
import hashlib
import datetime as dt
from typing import Dict, List, Optional, Tuple, Iterable, Union

import requests

# ---------------------- configuration ----------------------

API_BASE = "https://api.sportmonks.com/v3"
SPORT = "football"
API_TOKEN = os.getenv("SPORTMONKS_TOKEN", "").strip()

# network
TIMEOUT = 25
RETRIES = 4
BACKOFF = 1.8  # exponential base

# directories (ensure from workflow before calling)
DISK_CACHE_DIR = os.getenv("SMONKS_CACHE_DIR", ".cache_smonks")

# domain constants
LINEUP_TYPE_STARTER = 11
APPEARANCE_MINUTES_THRESHOLD = 45

# developer_names we consider for stats extraction
SHOT_DEVS_TOTAL = {"SHOTS", "SHOTS_TOTAL"}
SHOT_DEVS_SOT = {"SHOTS_ON_TARGET"}
SHOT_DEVS_SOFF = {"SHOTS_OFF_TARGET"}
MINUTES_DEVS = {"MINUTES_PLAYED", "MINUTES"}

# known league names (optional convenience)
LEAGUE_NAMES = {
    8: "Premier League",
    9: "Championship",
    384: "Serie A",
    387: "Serie B",
    82: "Bundesliga",
    301: "Ligue 1",
    564: "La Liga",
    567: "La Liga 2",
    600: "Super Lig",
}

# ---------------------- tiny helpers ----------------------


def _ensure_token():
    if not API_TOKEN:
        print("ERROR: Set SPORTMONKS_TOKEN in env.", file=sys.stderr)
        sys.exit(1)


def md5(s: str) -> str:
    return hashlib.md5(s.encode("utf-8")).hexdigest()


def _cache_key(url: str, params: dict) -> str:
    # canonicalize param order (including token)
    items = sorted((k, "" if v is None else str(v)) for k, v in params.items())
    body = url + "?" + "&".join(f"{k}={v}" for k, v in items)
    return md5(body)


def _disk_cache_path(key: str) -> str:
    os.makedirs(DISK_CACHE_DIR, exist_ok=True)
    return os.path.join(DISK_CACHE_DIR, f"{key}.json")


def _disk_cache_load(key: str) -> Optional[dict]:
    try:
        p = _disk_cache_path(key)
        if os.path.isfile(p):
            with open(p, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return None


def _disk_cache_save(key: str, payload: dict) -> None:
    try:
        p = _disk_cache_path(key)
        with open(p, "w", encoding="utf-8") as f:
            json.dump(payload, f)
    except Exception:
        pass


def safe_int(x, default: int = 0) -> int:
    try:
        return int(x)
    except Exception:
        return default


def pos_id_to_label(position_id: Optional[int]) -> str:
    return {24: "GK", 25: "DEF", 26: "MID", 27: "FWD"}.get(int(position_id or 0), "?")


def league_name(league_id: int) -> str:
    return LEAGUE_NAMES.get(int(league_id), f"League {league_id}")


def today_utc() -> dt.date:
    return dt.datetime.now(dt.timezone.utc).date()


def days_ahead(d: dt.date, n: int) -> dt.date:
    return d + dt.timedelta(days=n)


def daterange_str(start: dt.date, end_inclusive: dt.date, fmt: str = "%Y-%m-%d") -> List[str]:
    out = []
    d = start
    while d <= end_inclusive:
        out.append(d.strftime(fmt))
        d += dt.timedelta(days=1)
    return out


# ---------------------- HTTP client with caching ----------------------


class Memo:
    def __init__(self):
        self.store: Dict[str, dict] = {}

    def get(self, k: str):
        return self.store.get(k)

    def set(self, k: str, v: dict):
        self.store[k] = v


memo = Memo()


def cached_get(url: str, params: Optional[dict] = None) -> dict:
    """
    GET with:
      - token injection
      - in-memory cache
      - on-disk cache
      - retry/backoff (429-aware)
    """
    _ensure_token()
    params = dict(params or {})
    params["api_token"] = API_TOKEN

    key = _cache_key(url, params)

    hit = memo.get(key)
    if hit is not None:
        return hit

    disk_hit = _disk_cache_load(key)
    if disk_hit is not None:
        memo.set(key, disk_hit)
        return disk_hit

    last_exc = None
    for attempt in range(RETRIES):
        try:
            r = requests.get(url, params=params, timeout=TIMEOUT)
            # happy path
            if r.status_code < 400:
                j = r.json()
                memo.set(key, j)
                _disk_cache_save(key, j)
                return j

            # rate limited?
            if r.status_code == 429:
                retry_after = r.headers.get("Retry-After")
                if retry_after:
                    try:
                        sleep_s = float(retry_after)
                    except Exception:
                        sleep_s = None
                else:
                    sleep_s = None
                if sleep_s is None:
                    sleep_s = (BACKOFF ** attempt) + (0.3 * attempt)
                print(f"[429] {url} — sleeping {sleep_s:.1f}s (attempt {attempt+1}/{RETRIES})", file=sys.stderr)
                time.sleep(sleep_s)
                continue

            # other server-ish errors -> retry
            if r.status_code in (500, 502, 503, 504):
                sleep_s = (BACKOFF ** attempt) + (0.3 * attempt)
                print(f"[{r.status_code}] {url} — retrying in {sleep_s:.1f}s", file=sys.stderr)
                time.sleep(sleep_s)
                continue

            # hard error
            try:
                jerr = r.json()
            except Exception:
                jerr = {"message": r.text[:400]}
            raise requests.HTTPError(f"{r.status_code} {r.reason} for {r.url}\nResponse JSON: {jerr}")

        except requests.RequestException as e:
            last_exc = e
            sleep_s = (BACKOFF ** attempt) + 0.2
            print(f"[NET] {url} — {e}. retrying in {sleep_s:.1f}s", file=sys.stderr)
            time.sleep(sleep_s)

    if last_exc:
        raise last_exc
    raise RuntimeError("HTTP fetch failed unexpectedly.")


def api_get(path: str, params: Optional[dict] = None) -> dict:
    url = f"{API_BASE}/{SPORT}/{path.lstrip('/')}"
    return cached_get(url, params)


# ---------------------- fixtures & participants ----------------------


def get_fixtures_for_date(date_str: str, league_filter: Optional[set[int]] = None) -> List[dict]:
    """
    Fetch fixtures for a specific date (UTC), include participants/state/league.
    """
    params = {"include": "participants;state;league", "order": "asc", "page": 1}
    j = api_get(f"fixtures/date/{date_str}", params)
    data = list(j.get("data", []) or [])
    meta = j.get("meta") or {}
    last_page = int(meta.get("last_page") or 1)

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


def fixtures_by_date(
    dates: Union[Iterable[str], dt.date, Tuple[dt.date, dt.date], Tuple[dt.date, int]],
    league_filter: Optional[Iterable[int]] = None,
) -> List[dict]:
    """
    Convenience wrapper expected by scripts/fixtures_lineups.py.

    Accepts:
      - iterable of date strings 'YYYY-MM-DD'
      - (start_date, end_date) as dates
      - (start_date, days) where 'days' is inclusive offset
      - single dt.date -> just that day

    Returns list of fixtures (already filtered by league ids if provided).
    """
    if league_filter is not None:
        league_filter = set(int(x) for x in league_filter)

    date_strings: List[str] = []

    if isinstance(dates, dt.date):
        date_strings = [dates.strftime("%Y-%m-%d")]
    elif isinstance(dates, tuple) and len(dates) == 2:
        a, b = dates
        if isinstance(b, dt.date):
            date_strings = daterange_str(a, b, "%Y-%m-%d")
        else:
            # b is int (days ahead inclusive)
            date_strings = daterange_str(a, days_ahead(a, int(b)), "%Y-%m-%d")
    elif isinstance(dates, (list, tuple)):
        # list/tuple of strings or dates
        tmp = []
        for d in dates:
            if isinstance(d, dt.date):
                tmp.append(d.strftime("%Y-%m-%d"))
            else:
                tmp.append(str(d))
        date_strings = tmp
    else:
        # generic iterable of strings
        date_strings = [str(d) for d in dates]  # type: ignore

    fixtures: List[dict] = []
    for ds in date_strings:
        try:
            fixtures.extend(get_fixtures_for_date(ds, league_filter=set(league_filter) if league_filter else None))
        except Exception as e:
            print(f"[WARN] fixtures_by_date failed for {ds}: {e}", file=sys.stderr)
            continue
    # stable order
    fixtures.sort(key=lambda x: x.get("starting_at") or "")
    return fixtures


def pick_home_away(participants: List[dict]) -> Tuple[Optional[dict], Optional[dict]]:
    home = next((p for p in participants if (p.get("meta") or {}).get("location") == "home"), None)
    away = next((p for p in participants if (p.get("meta") or {}).get("location") == "away"), None)
    return home, away


def _has_starters_for_team(full_fixture: dict, team_id: int) -> bool:
    for l in (full_fixture.get("lineups") or []):
        if l.get("team_id") == team_id and l.get("type_id") == LINEUP_TYPE_STARTER:
            return True
    return False


def get_team_last_fixture_with_xi(team_id: int, league_id: int) -> Optional[dict]:
    """
    Find the team's most recent fixture in this league with recorded starters.
    Strategy:
      1) team's "latest" list filtered by league
      2) walk back by date up to 180 days
    """
    # try "latest" first
    try:
        j = api_get(f"teams/{team_id}", {"include": "latest.league;latest.lineups;latest.lineups.player"})
        latest = j.get("data", {}).get("latest")
        lst = latest if isinstance(latest, list) else ([latest] if latest else [])
        lst.sort(key=lambda x: x.get("starting_at") or "", reverse=True)
        for fx in lst:
            if fx and fx.get("league_id") == league_id and fx.get("id"):
                fid = fx["id"]
                full = api_get(f"fixtures/{fid}", {"include": "lineups;lineups.player"}).get("data", {}) or {}
                if _has_starters_for_team(full, team_id):
                    full["participants"] = fx.get("participants") or []
                    return full
    except Exception:
        pass

    # fallback: date walkback
    start = today_utc()
    for back in range(1, 181):
        d = (start - dt.timedelta(days=back)).strftime("%Y-%m-%d")
        try:
            fxs = get_fixtures_for_date(d, league_filter={league_id})
        except Exception:
            continue
        for fx in fxs:
            if any(p.get("id") == team_id for p in (fx.get("participants") or [])):
                fid = fx.get("id")
                if not fid:
                    continue
                full = api_get(f"fixtures/{fid}", {"include": "lineups;lineups.player"}).get("data", {}) or {}
                if _has_starters_for_team(full, team_id):
                    full["participants"] = fx.get("participants") or []
                    return full
    return None


# ---------------------- fixture stats extraction ----------------------


def _num_from_detail(det: dict) -> int:
    """SportMonks 'details.data.value' can be a number or a dict with totals."""
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


def get_fixture_lineups_minutes_and_shots(
    fixture_id: int,
) -> Tuple[Dict[int, dict], Dict[int, int], Dict[int, int]]:
    """
    fixtures/{id}?include=lineups.details.type
    Returns:
        lineups_map: {player_id: lineup_row}
        shots_map:   {player_id: total_shots}
        minutes_map: {player_id: minutes_played}
    """
    j = api_get(f"fixtures/{fixture_id}", {"include": "lineups.details.type"}).get("data", {}) or {}
    lineups = j.get("lineups") or []

    lineups_map: Dict[int, dict] = {}
    shots_map: Dict[int, int] = {}
    minutes_map: Dict[int, int] = {}

    for lp in lineups:
        pid = lp.get("player_id")
        if not pid:
            continue
        pid = int(pid)
        lineups_map[pid] = lp

        total_from_api: Optional[int] = None
        sot = 0
        soff = 0
        mins: Optional[int] = None

        for det in (lp.get("details") or []):
            t = det.get("type") or {}
            dev = (t.get("developer_name") or "").upper()

            if dev in SHOT_DEVS_TOTAL:
                total_from_api = _num_from_detail(det)
            elif dev in SHOT_DEVS_SOT:
                sot += _num_from_detail(det)
            elif dev in SHOT_DEVS_SOFF:
                soff += _num_from_detail(det)
            elif dev in MINUTES_DEVS:
                mv = _num_from_detail(det)
                mins = mv if mins is None else max(mins, mv)

        if mins is not None:
            minutes_map[pid] = mins
        if (total_from_api is not None) or (sot + soff) > 0:
            shots_map[pid] = total_from_api if total_from_api is not None else (sot + soff)

    return lineups_map, shots_map, minutes_map


# ---------------------- recent fixtures & player series ----------------------


def get_team_recent_league_fixtures(team_id: int, league_id: int, want: int) -> List[dict]:
    """
    Recent fixtures for TEAM in LEAGUE, newest→oldest, scanning up to ~2 years.
    We over-collect (want * 14) to survive minutes filters.
    """
    collected: List[dict] = []
    seen = set()

    # seed with team's 'latest'
    try:
        j = api_get(f"teams/{team_id}", {"include": "latest.league"})
        latest = j.get("data", {}).get("latest")
        lst = latest if isinstance(latest, list) else ([latest] if latest else [])
        for fx in lst:
            if fx and fx.get("league_id") == league_id and fx.get("id") not in seen:
                collected.append(fx)
                seen.add(fx.get("id"))
    except Exception:
        pass

    # walk back by date up to 2 years
    today = today_utc()
    for back in range(1, 731):
        d = (today - dt.timedelta(days=back)).strftime("%Y-%m-%d")
        try:
            fixtures = get_fixtures_for_date(d, league_filter={league_id})
        except Exception:
            continue
        for fx in fixtures:
            fid = fx.get("id")
            if not fid or fid in seen:
                continue
            if any(p.get("id") == team_id for p in (fx.get("participants") or [])):
                collected.append(fx)
                seen.add(fid)
        if len(collected) >= want * 14:
            break

    collected.sort(key=lambda x: x.get("starting_at") or "", reverse=True)
    return collected


def get_player_last_n_shots_series(team_id: int, player_id: int, n: int, league_id: int) -> List[int]:
    """
    Player's last N league APPEARANCES (>=45') across this + last season.
    If minutes missing or < threshold in a fixture, skip that fixture.
    Record 0 if played >=45' but 'shots' stat absent.
    """
    fixtures = get_team_recent_league_fixtures(team_id, league_id, n)
    series: List[Tuple[str, int]] = []

    def consider_fixture(fx: dict) -> Optional[Tuple[str, int]]:
        fid = fx.get("id")
        if not fid:
            return None
        _, shots_map, minutes_map = get_fixture_lineups_minutes_and_shots(int(fid))
        mins = minutes_map.get(int(player_id))
        if mins is None or mins < APPEARANCE_MINUTES_THRESHOLD:
            return None
        shots = shots_map.get(int(player_id), 0)
        return (fx.get("starting_at") or "", shots)

    for fx in fixtures:
        if len(series) >= n:
            break
        try:
            res = consider_fixture(fx)
        except Exception:
            res = None
        if res:
            series.append(res)

    series.sort(key=lambda x: x[0], reverse=True)
    return [s for _, s in series][:n]


def compute_hit_rate(series: List[int]) -> float:
    if not series:
        return 0.0
    hits = sum(1 for x in series if x >= 1)
    return round(100.0 * hits / len(series), 1)


# ---------------------- predicted XI for a fixture ----------------------


def build_predicted_xi_for_team(fixture_id: int, team_id: int) -> List[dict]:
    """
    For a given fixture/team, prefer official starters; else fall back to team’s
    last league fixture with starters.
    """
    # try the fixture's own lineups first
    try:
        fx_full = api_get(f"fixtures/{fixture_id}", {"include": "lineups;lineups.player"}).get("data", {}) or {}
        starters = [
            l for l in (fx_full.get("lineups") or [])
            if l.get("type_id") == LINEUP_TYPE_STARTER and l.get("team_id") == team_id
        ]
        if starters:
            starters.sort(key=lambda x: x.get("formation_position") or 9999)
            return starters[:11]
    except Exception:
        pass

    # fallback: last league fixture with XI
    slim = api_get(f"fixtures/{fixture_id}", {"include": "league"}).get("data", {}) or {}
    lid = slim.get("league_id")
    last = get_team_last_fixture_with_xi(team_id, lid) or {}
    lineups = last.get("lineups") or []
    starters = [l for l in lineups if l.get("team_id") == team_id and l.get("type_id") == LINEUP_TYPE_STARTER]
    starters.sort(key=lambda x: x.get("formation_position") or 9999)
    return starters[:11]


# ---------------------- exportable API ----------------------

__all__ = [
    # config/info
    "API_BASE",
    "SPORT",
    "API_TOKEN",
    "LEAGUE_NAMES",
    "league_name",
    "pos_id_to_label",
    # http
    "api_get",
    "cached_get",
    # dates
    "today_utc",
    "days_ahead",
    "daterange_str",
    # fixtures
    "get_fixtures_for_date",
    "fixtures_by_date",
    "pick_home_away",
    "get_team_last_fixture_with_xi",
    "get_fixture_lineups_minutes_and_shots",
    "get_team_recent_league_fixtures",
    # players
    "get_player_last_n_shots_series",
    "compute_hit_rate",
    # xi
    "build_predicted_xi_for_team",
]
