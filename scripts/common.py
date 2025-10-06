#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os, re, json, time, hashlib, datetime as dt
from typing import Dict, List, Optional, Tuple
import requests

API_BASE = "https://api.sportmonks.com/v3"
SPORT = "football"
API_TOKEN = os.getenv("SPORTMONKS_TOKEN", "")
if not API_TOKEN:
    raise SystemExit("Missing env SPORTMONKS_TOKEN")

# Leagues you care about (you can add more later)
LEAGUES = {
    8:   "Premier League",
    9:   "Championship",
    384: "Serie A",
    387: "Serie B",
    82:  "Bundesliga",
    301: "Ligue 1",
    564: "La Liga",
    567: "La Liga 2",
    600: "Süper Lig",
    72:  "Eredivisie",   # added
    271: "Superliga",    # added
}

DATE_FMT = "%Y-%m-%d"
LINEUP_TYPE_STARTER = 11
APPEARANCE_MINUTES_THRESHOLD = 45

TIMEOUT = 20
RETRIES = 3
BACKOFF = 1.7

# Simple disk cache (persists across workflow runs if saved by actions/cache)
CACHE_DIR = os.environ.get("SMONKS_CACHE_DIR", ".cache_smonks")
os.makedirs(CACHE_DIR, exist_ok=True)

def _cache_key(url: str, params: Dict) -> str:
    q = "&".join(f"{k}={params[k]}" for k in sorted(params))
    h = hashlib.md5((url + "?" + q).encode("utf-8")).hexdigest()
    return os.path.join(CACHE_DIR, f"{h}.json")

def cached_get(url: str, params: Optional[dict] = None) -> dict:
    if params is None:
        params = {}
    p = {**params, "api_token": API_TOKEN}
    path = _cache_key(url, p)
    if os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    last_exc = None
    for attempt in range(1, RETRIES+1):
        try:
            r = requests.get(url, params=p, timeout=TIMEOUT)
            if r.status_code >= 400:
                # surface rate-limit body to logs
                try:
                    body = r.json()
                except Exception:
                    body = {"message": r.text[:400]}
                raise requests.HTTPError(f"{r.status_code} {r.reason} for {r.url}\nResponse JSON: {body}")
            j = r.json()
            try:
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(j, f, ensure_ascii=False)
            except Exception:
                pass
            return j
        except Exception as e:
            last_exc = e
            if attempt < RETRIES:
                time.sleep(BACKOFF ** attempt)
            else:
                raise
    raise last_exc  # pragma: no cover

def api_get(path: str, params: Optional[dict] = None) -> dict:
    url = f"{API_BASE}/{SPORT}/{path.lstrip('/')}"
    return cached_get(url, params)

# ---------- dates ----------
def today_utc() -> dt.date:
    return dt.datetime.now(dt.timezone.utc).date()

def days_ahead(d: dt.date, n: int) -> dt.date:
    return d + dt.timedelta(days=n)

def daterange_str(start: dt.date, end_inclusive: dt.date) -> List[str]:
    out = []
    d = start
    while d <= end_inclusive:
        out.append(d.strftime(DATE_FMT))
        d += dt.timedelta(days=1)
    return out

# ---------- fixtures ----------
def fixtures_by_date(date_str: str, league_filter: Optional[set] = None) -> List[dict]:
    params = {"include": "participants;state;league", "order": "asc", "page": 1}
    j = api_get(f"fixtures/date/{date_str}", params)
    data = j.get("data", []) or []
    meta = j.get("meta") or {}
    last_page = int(meta.get("last_page", 1))
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

# ---------- stats from lineups.details ----------
SHOT_DEVS_TOTAL = {"SHOTS", "SHOTS_TOTAL"}
SHOT_DEVS_SOT   = {"SHOTS_ON_TARGET"}
SHOT_DEVS_SOFF  = {"SHOTS_OFF_TARGET"}
MINUTES_DEVS    = {"MINUTES_PLAYED", "MINUTES"}

def _num_from_detail(det: dict) -> int:
    v = (det.get("data") or {}).get("value")
    if isinstance(v, dict):
        if "total" in v:
            try: return int(v["total"] or 0)
            except Exception: return 0
        s = 0
        for x in v.values():
            if isinstance(x, (int, float)): s += int(x)
        return s
    try: return int(v or 0)
    except Exception: return 0

def fixture_lineups_minutes_and_shots(fixture_id: int):
    """Return (lineups_map, shots_map, minutes_map) for a fixture id."""
    j = api_get(f"fixtures/{fixture_id}", {"include": "lineups.details.type"})
    data = j.get("data", {}) or {}
    lineups = data.get("lineups") or []
    lineups_map, shots_map, minutes_map = {}, {}, {}
    for lp in lineups:
        pid = lp.get("player_id")
        if not pid: 
            continue
        pid = int(pid)
        lineups_map[pid] = lp
        total_from_api = None
        sot = soff = 0
        mins = None
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

def pos_id_to_label(position_id: Optional[int]) -> str:
    return {24: "GK", 25: "DEF", 26: "MID", 27: "FWD"}.get(position_id or 0, "?")

def league_name(league_id: int) -> str:
    return LEAGUES.get(league_id, f"League {league_id}")

def run_date_dir(root: str, date_iso: str) -> str:
    path = os.path.join(root, date_iso)
    os.makedirs(path, exist_ok=True)
    return path
