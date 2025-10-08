#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Predict XIs for all saved fixtures (rate-limit friendly, batches of 3).

Rules:
- Do NOT rely on official XIs. Predict from each team's most recent LEAGUE match
  that has a recorded starting XI (starters only).
- Mark players as OUT if Sportmonks reports an active injury/suspension.
- Otherwise mark as EXPECTED (we're not picking replacements).
- Write one JSON per fixture plus a summary.

Inputs (already created by your fetch script):
  data/fixtures/by_league/<league_id>.json   # preferred
  or
  data/fixtures/<league_id>.json             # fallback

Outputs:
  data/predicted_xi/<league_id>/<fixture_id>.json
  data/predicted_xi/summary.txt

Env:
  export SPORTMONKS_TOKEN=...   (uses repository secret in Actions)
"""

import os
import re
import json
import time
import math
import datetime as dt
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import requests

# ---------- config ----------
SM_API_BASE = "https://api.sportmonks.com/v3"
SM_SPORT = "football"
API_TOKEN = os.getenv("SPORTMONKS_TOKEN") or os.getenv("SPORTMONKS_API_TOKEN") or os.getenv("SPORTMONKS_TOKEN".lower())
if not API_TOKEN:
    raise SystemExit("ERROR: SPORTMONKS_TOKEN is not set.")

DATE_FMT = "%Y-%m-%d"
LINEUP_TYPE_STARTER = 11
APPEARANCE_MINUTES_THRESHOLD = 45

# Batching / rate limiting
BATCH_SIZE = 3
GLOBAL_MIN_DELAY = 0.15  # small delay between Sportmonks calls
TIMEOUT = 25
RETRIES = 3
BACKOFF = 1.8

# On-disk cache (24h TTL)
CACHE_DIR = Path(".cache_smonks")
CACHE_DIR.mkdir(parents=True, exist_ok=True)
CACHE_TTL_SECS = 24 * 3600

# I/O paths
FIXT_ROOT = Path("data/fixtures")
PRED_ROOT = Path("data/predicted_xi")
PRED_ROOT.mkdir(parents=True, exist_ok=True)

# ---------- tiny cache ----------
import hashlib
MEMO: Dict[str, dict] = {}

def _cache_key(url: str, params: dict) -> str:
    base = url + "?" + "&".join(f"{k}={params[k]}" for k in sorted(params))
    return hashlib.sha256(base.encode("utf-8")).hexdigest()

def _cache_path(key: str) -> Path:
    return CACHE_DIR / f"{key}.json"

def _cache_load(key: str) -> Optional[dict]:
    p = _cache_path(key)
    if not p.is_file():
        return None
    try:
        if (time.time() - p.stat().st_mtime) > CACHE_TTL_SECS:
            return None
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None

def _cache_save(key: str, payload: dict) -> None:
    try:
        _cache_path(key).write_text(json.dumps(payload), encoding="utf-8")
    except Exception:
        pass

def sm_get(path: str, params: Optional[dict] = None) -> dict:
    if params is None:
        params = {}
    params = {**params, "api_token": API_TOKEN}
    url = f"{SM_API_BASE}/{SM_SPORT}/{path.lstrip('/')}"
    key = _cache_key(url, params)

    if key in MEMO:
        return MEMO[key]
    cached = _cache_load(key)
    if cached is not None:
        MEMO[key] = cached
        return cached

    last_exc = None
    for attempt in range(1, RETRIES + 1):
        try:
            # smooth out calls
            prev = getattr(sm_get, "_last_ts", 0.0)
            now = time.time()
            if now - prev < GLOBAL_MIN_DELAY:
                time.sleep(GLOBAL_MIN_DELAY - (now - prev))

            r = requests.get(url, params=params, timeout=TIMEOUT)
            setattr(sm_get, "_last_ts", time.time())
            if r.status_code == 429:
                sleep = min(90, (BACKOFF ** attempt) * 2.0)
                time.sleep(sleep)
                continue
            r.raise_for_status()
            j = r.json()
            MEMO[key] = j
            _cache_save(key, j)
            return j
        except Exception as e:
            last_exc = e
            if attempt < RETRIES:
                time.sleep((BACKOFF ** attempt))
            else:
                raise
    raise last_exc

# ---------- helpers ----------
def today_utc() -> dt.date:
    return dt.datetime.now(dt.timezone.utc).date()

def pos_id_to_label(position_id: Optional[int]) -> str:
    return {24: "GK", 25: "DEF", 26: "MID", 27: "FWD"}.get(position_id or 0, "?")

def pick_home_away(participants: List[dict]) -> Tuple[Optional[dict], Optional[dict]]:
    home = next((p for p in participants if (p.get("meta") or {}).get("location") == "home"), None)
    away = next((p for p in participants if (p.get("meta") or {}).get("location") == "away"), None)
    return home, away

def league_ids_present() -> List[int]:
    ids = []
    # prefer by_league/
    by_lg = FIXT_ROOT / "by_league"
    if by_lg.is_dir():
        for p in sorted(by_lg.glob("*.json")):
            try:
                ids.append(int(p.stem))
            except Exception:
                continue
        return ids
    # fallback to flat
    for p in sorted(FIXT_ROOT.glob("*.json")):
        try:
            ids.append(int(p.stem))
        except Exception:
            continue
    return ids

def load_all_fixtures() -> List[dict]:
    fixtures = []
    by_lg = FIXT_ROOT / "by_league"
    if by_lg.is_dir():
        files = sorted(by_lg.glob("*.json"))
        for f in files:
            try:
                obj = json.loads(f.read_text(encoding="utf-8"))
                arr = obj.get("fixtures") or []
                fixtures.extend(arr)
            except Exception:
                continue
        return fixtures
    # fallback flat
    for f in sorted(FIXT_ROOT.glob("*.json")):
        try:
            obj = json.loads(f.read_text(encoding="utf-8"))
            arr = obj.get("fixtures") or obj  # in case a flat file is raw array
            if isinstance(arr, list):
                fixtures.extend(arr)
            elif isinstance(obj, dict) and "fixtures" in obj:
                fixtures.extend(obj["fixtures"])
        except Exception:
            continue
    return fixtures

def get_fixtures_for_date(date_str: str, league_filter: Optional[set] = None) -> List[dict]:
    params = {"include": "participants;state;league", "order": "asc", "page": 1}
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

def get_team_last_fixture_with_xi(team_id: int, league_id: int) -> Optional[dict]:
    """Team's most recent league fixture (this/last season) with recorded starters."""
    try:
        j = sm_get(f"teams/{team_id}", {"include": "latest.league;latest.lineups;latest.lineups.player"})
        latest = j.get("data", {}).get("latest")
        lst = latest if isinstance(latest, list) else ([latest] if latest else [])
        cand = [fx for fx in lst if (fx or {}).get("league_id") == league_id]
        cand.sort(key=lambda x: x.get("starting_at") or "", reverse=True)
        for fx in cand:
            fid = fx.get("id")
            if not fid:
                continue
            full = sm_get(f"fixtures/{fid}", {"include": "lineups;lineups.player"}).get("data", {})
            if any(l.get("type_id") == LINEUP_TYPE_STARTER and l.get("team_id") == team_id for l in (full.get("lineups") or [])):
                full["participants"] = fx.get("participants") or []
                return full
    except Exception:
        pass

    # Fallback small date-walk (≤180 days)
    start = today_utc()
    for back in range(1, 181):
        d = (start - dt.timedelta(days=back)).strftime(DATE_FMT)
        try:
            fxs = get_fixtures_for_date(d, league_filter={league_id})
        except Exception:
            continue
        for fx in fxs:
            if any(p.get("id") == team_id for p in (fx.get("participants") or [])):
                full = sm_get(f"fixtures/{fx['id']}", {"include": "lineups;lineups.player"}).get("data", {})
                if any(l.get("type_id") == LINEUP_TYPE_STARTER and l.get("team_id") == team_id for l in (full.get("lineups") or [])):
                    full["participants"] = fx.get("participants") or []
                    return full
    return None

# ---- Unavailability (injuries/suspensions) ----
def _parse_sidelined_records(container) -> Dict[int, dict]:
    """
    Parse various shapes Sportmonks might return for 'sidelined'.
    Returns {player_id: record}
    """
    res: Dict[int, dict] = {}
    if not container:
        return res

    def consider(rec):
        if not isinstance(rec, dict):
            return
        pid = rec.get("player_id")
        if not pid and isinstance(rec.get("player"), dict):
            pid = rec["player"].get("id")
        try:
            pid = int(pid)
        except Exception:
            return
        status = (rec.get("status") or rec.get("type") or "").lower()
        reason = rec.get("reason") or rec.get("description") or rec.get("type") or ""
        # dates are often like 'YYYY-MM-DD' or full ISO
        end = rec.get("end") or rec.get("until") or rec.get("to")
        start = rec.get("start") or rec.get("from")
        res[pid] = {"status": status, "reason": reason, "start": start, "end": end}

    if isinstance(container, list):
        for r in container:
            consider(r)
    elif isinstance(container, dict):
        # sometimes { data: [...] } or keyed by id
        if "data" in container and isinstance(container["data"], list):
            for r in container["data"]:
                consider(r)
        else:
            for r in container.values():
                consider(r)
    return res

def _is_active_sidelined(rec: dict, today: dt.date) -> bool:
    """Return True if injury/suspension is likely active now."""
    end = rec.get("end")
    if end:
        # tolerate multiple formats
        try:
            if len(end) == 10:
                end_date = dt.date.fromisoformat(end[:10])
            else:
                end_date = dt.datetime.fromisoformat(end.replace("Z","")).date()
            if end_date < today:
                return False
        except Exception:
            # unknown format -> assume active
            pass
    status = (rec.get("status") or "").lower()
    # If status explicitly says recovered/returned etc -> not active
    if any(k in status for k in ("recovered", "returned", "fit", "available")):
        return False
    return True

def get_team_unavailable_player_ids(team_id: int) -> Dict[int, dict]:
    """
    Try a few shapes to find injuries/suspensions for a team.
    Best-effort; if the API doesn't expose it for this plan, returns {}.
    """
    today = today_utc()
    out: Dict[int, dict] = {}

    # Attempt 1: team include sidelined
