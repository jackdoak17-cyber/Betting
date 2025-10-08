#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Fetch upcoming fixtures from Sportmonks for specific leagues and write:
- data/fixtures/latest.json        (with generated_at + metadata + all fixtures)
- data/fixtures/{league}.json      (per-league fixtures, stable content)
- data/fixtures/by_league/{id}.json (same as above; kept for compatibility)
- data/fixtures/fixtures.txt       (human summary)

Env:
  SPORTMONKS_TOKEN or SPORTMONKS_API_TOKEN
  DAYS_AHEAD  (default: 21)

Leagues:
  8,9,82,301,384,387,564,567,600
"""

import os, sys, json, time, datetime as dt
from typing import List, Dict, Optional, Tuple
import requests
from pathlib import Path

API_BASE = "https://api.sportmonks.com/v3"
SPORT = "football"
API_TOKEN = (
    os.getenv("SPORTMONKS_TOKEN")
    or os.getenv("SPORTMONKS_API_TOKEN")
    or os.getenv("SM_TOKEN")
)

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
LEAGUE_IDS = sorted(LEAGUES.keys(), key=int)  # 8,9,82,301,384,387,564,567,600

DAYS_AHEAD = int(os.getenv("DAYS_AHEAD", "21"))
DATE_FMT = "%Y-%m-%d"
TIMEOUT = 25

OUT_DIR = Path("data/fixtures")
BY_LEAGUE_DIR = OUT_DIR / "by_league"
OUT_DIR.mkdir(parents=True, exist_ok=True)
BY_LEAGUE_DIR.mkdir(parents=True, exist_ok=True)

def today_utc_date() -> dt.date:
    return dt.datetime.now(dt.timezone.utc).date()

def daterange_str(start: dt.date, end_inclusive: dt.date) -> List[str]:
    out = []
    d = start
    while d <= end_inclusive:
        out.append(d.strftime(DATE_FMT))
        d += dt.timedelta(days=1)
    return out

def api_get(path: str, params: Optional[dict] = None) -> dict:
    if params is None:
        params = {}
    params = {**params, "api_token": API_TOKEN}
    url = f"{API_BASE}/{SPORT}/{path.lstrip('/')}"
    r = requests.get(url, params=params, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()

def get_fixtures_for_date(date_str: str, league_filter: Optional[set] = None) -> List[dict]:
    params = {"include": "participants;state;league", "order": "asc", "page": 1}
    j = api_get(f"fixtures/date/{date_str}", params)
    data = (j.get("data") or [])
    meta = (j.get("meta") or {})
    last_page = int(meta.get("last_page", 1))

    for p in range(2, last_page + 1):
        params["page"] = p
        jp = api_get(f"fixtures/date/{date_str}", params)
        data.extend(jp.get("data") or [])

    # Filter and normalise minimal fields we care about
    out = []
    for fx in data:
        lid = fx.get("league_id")
        if league_filter and lid not in league_filter:
            continue
        parts = fx.get("participants") or []
        if not parts:
            continue
        # keep only safe subset + a few helpful fields
        out.append({
            "id": fx.get("id"),
            "league_id": lid,
            "season_id": fx.get("season_id"),
            "stage_id": fx.get("stage_id"),
            "round_id": fx.get("round_id"),
            "name": fx.get("name"),
            "starting_at": fx.get("starting_at"),
            "starting_at_timestamp": fx.get("starting_at_timestamp"),
            "state_id": fx.get("state_id"),
            "venue_id": fx.get("venue_id"),
            "participants": [
                {
                    "id": p.get("id"),
                    "name": p.get("name"),
                    "short_code": p.get("short_code"),
                    "meta": p.get("meta"),  # contains home/away
                }
                for p in parts
            ],
        })
    return out

def write_json(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    tmp.replace(path)

def write_tex_
