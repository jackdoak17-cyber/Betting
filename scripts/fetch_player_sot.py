#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Build shots-on-target (SOT) time series for players appearing in our predicted XIs.

Rules:
- League matches only (restricted to the leagues we track)
- Current season only
- For each player: take the LAST 10 league appearances (latest first)
- Only include an appearance if the player played >= 45 minutes
- Output JSON per league + combined JSON
- Also write plain-text summaries, including a team-by-team summary
- Drive the player list from data/predicted_xi/by_league/*.json

Output directory:
  data/player_shots_on_target/
    - by_league/{league_id}.json
    - combined.json
    - summary.txt
    - summary_by_team.txt

ENV:
  SPORTMONKS_TOKEN  (required)
"""

import os
import sys
import time
import json
import glob
import datetime as dt
from typing import Dict, List, Tuple, Optional

import requests

# ---------------- Config ----------------

API_BASE = "https://api.sportmonks.com/v3/football"
API_TOKEN = os.getenv("SPORTMONKS_TOKEN")
if not API_TOKEN:
    print("ERROR: SPORTMONKS_TOKEN is not set.", file=sys.stderr)
    sys.exit(1)

TIMEOUT = 25
RETRIES = 3
BACKOFF = 1.6
GLOBAL_MIN_DELAY = 0.18   # gentle pacing across calls

# Leagues we care about (must match your repo)
LEAGUE_NAMES: Dict[int, str] = {
    8: "Premier League",
    9: "Championship",
    82: "Bundesliga",
    301: "Ligue 1",
    384: "Serie A",
    387: "Serie B",
    564: "La Liga",
    567: "La Liga 2",
    600: "Super Lig",
}
LEAGUE_IDS = sorted(LEAGUE_NAMES.keys())

# Limit
N_LAST = 10
MIN_MINUTES = 45

# Batch pacing
BATCH_SIZE = 12              # players per small batch
SLEEP_BETWEEN_BATCHES = 1.0  # seconds

# ---------------- HTTP helpers ----------------

_MEMO: Dict[str, dict] = {}
_last_call_ts = 0.0

def _pace():
    global _last_call_ts
    now = time.time()
    if now - _last_call_ts < GLOBAL_MIN_DELAY:
        time.sleep(GLOBAL_MIN_DELAY - (now - _last_call_ts))
    _last_call_ts = time.time()

def _key(url: str, params: dict) -> str:
    return url + "?" + "&".join(f"{k}={params[k]}" for k in sorted(params))

def api_get(path: str, params: Optional[dict] = None) -> dict:
    """GET with retries, backoff, light memo, and 429 handling."""
    if params is None:
        params = {}
    params = {**params, "api_token": API_TOKEN}
    url = f"{API_BASE}/{path.lstrip('/')}"
    k = _key(url, params)

    if k in _MEMO:
        return _MEMO[k]

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
            j = r.json()
            _MEMO[k] = j
            return j
        except Exception as e:
            last_exc = e
            if i < RETRIES:
                sleep = BACKOFF ** i
                print(f"[RETRY] {path} (attempt {i}) sleeping {sleep:.1f}s")
                time.sleep(sleep)
            else:
                raise
    raise last_exc  # pragma: no cover

# ---------------- Utilities ----------------

def ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)

def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()

def position_bucket_from_id(position_id: Optional[int]) -> Optional[str]:
    # Broad fallback if we don't have LB/RB/etc.
    return {24: "GK", 25: "DEF", 26: "MID", 27: "FWD"}.get(position_id or 0)

def pick_role_tag(player_obj: dict) -> str:
    # Prefer specific "role" from predicted_xi if present, else "position_label", else bucket
    role = (player_obj.get("role") or "").strip()
    if role:
        return role
    pos_label = (player_obj.get("position_label") or "").strip()
    if pos_label:
        return pos_label
    bucket = position_bucket_from_id(player_obj.get("position_id"))
    return bucket or "?"

# ---------------- Load predicted XI players ----------------

def load_predicted_players() -> Dict[int, Dict[int, Dict[int, dict]]]:
    """
    Returns: nested dict
      by_league[league_id][team_id][player_id] = {
          "name": str,
          "position_tag": "LB/RB/CB/DM/AM/LW/RW/ST/GK/DEF/MID/FWD/?"
      }
    """
    base = "data/predicted_xi/by_league"
    by_league: Dict[int, Dict[int, Dict[int, dict]]] = {}

    paths = sorted(glob.glob(os.path.join(base, "*.json")))
    for path in paths:
        try:
            with open(path, "r", encoding="utf-8") as f:
                j = json.load(f)
        except Exception:
            continue

        lid = int(j.get("league_id") or 0)
        if lid not in LEAGUE_IDS:
            continue

        fixtures = j.get("fixtures") or []
        for fx in fixtures:
            home = fx.get("home") or {}
            away = fx.get("away") or {}
            for side in (home, away):
                tid = int(side.get("team_id") or 0)
                if not tid:
                    continue
                by_league.setdefault(lid, {}).setdefault(tid, {})
                players = side.get("predicted_xi") or []
                for p in players:
                    pid = int(p.get("player_id") or 0)
                    if not pid:
                        continue
                    name = (p.get("name") or "").strip() or f"Player {pid}"
                    tag = pick_role_tag(p)
                    by_league[lid][tid][pid] = {
                        "name": name,
                        "position_tag": tag,
                    }
    return by_league

# ---------------- Season helpers ----------------

def current_season_meta(league_id: int) -> Tuple[Optional[int], Optional[str], Optional[str]]:
    """
    Try to fetch current season id and date bounds for a league.
    Returns (season_id, start_date, end_date) as strings YYYY-MM-DD if known.
    """
    season_id = None
    start_date = None
    end_date = None

    # Primary attempt
    try:
        j = api_get(f"leagues/{league_id}", {"include": "currentSeason"})
        data = j.get("data") or {}
        cur = data.get("currentSeason") or data.get("current_season") or data.get("season") or {}
        season_id = cur.get("id") or cur.get("seas_
