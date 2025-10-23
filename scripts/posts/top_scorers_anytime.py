#!/usr/bin/env python3
"""
Print Sportmonks season IDs for one or more leagues (no guessing).

Usage:
  export SPORTMONKS_TOKEN=your_api_token

  # Premier League (league_id=8) seasons (default)
  python scripts/util/print_season_ids.py

  # Multiple leagues (PL=8, La Liga=564, Serie A=384, Ligue 1=301, Bundesliga=82)
  python scripts/util/print_season_ids.py --league-ids 8 564 384 301 82

  # Only the current season for each league
  python scripts/util/print_season_ids.py --current-only

  # IDs only (comma-separated) — handy for piping
  python scripts/util/print_season_ids.py --league-ids 8 --ids-only
"""
import os
import sys
import argparse
import requests
from datetime import datetime
from typing import List, Dict, Any, Optional, Tuple

BASE = "https://api.sportmonks.com/v3/football"

def token() -> str:
    t = os.getenv("SPORTMONKS_TOKEN") or os.getenv("SPORTMONKS_API_TOKEN")
    if not t:
        print("Error: set SPORTMONKS_TOKEN (or SPORTMONKS_API_TOKEN).", file=sys.stderr)
        sys.exit(0)  # CI-friendly: don't fail the job
    return t

def api_get(path: str, params: Optional[dict] = None, timeout: int = 20) -> Dict[str, Any]:
    params = dict(params or {})
    params.setdefault("api_token", token())
    url = f"{BASE}/{path.lstrip('/')}"
    r = requests.get(url, params=params, timeout=timeout)
    if r.status_code == 404:
        return {"data": None, "_status": 404, "_body": r.text}
    if r.status_code == 403:
        return {"data": None, "_status": 403, "_body": r.text}
    try:
        r.raise_for_status()
    except requests.HTTPError:
        # Return best-effort info without crashing CI
        return {"data": None, "_status": r.status_code, "_body": r.text}
    try:
        js = r.json()
    except Exception:
        js = {"data": None, "_status": "non-json", "_body": r.text[:500]}
    return js

def parse_date(s: Optional[str]) -> Tuple[int, int, int]:
    if not s:
        return (1900, 1, 1)
    try:
        d = datetime.fromisoformat(s)
        return (d.year, d.month, d.day)
    except Exception:
        return (1900, 1, 1)

def fetch_league_with_seasons(league_id: int) -> Dict[str, Any]:
    # Try preferred include (fast, scoped)
    data = api_get(f"leagues/{league_id}", params={"include": "seasons;currentSeason"})
    if data.get("data"):
        return data

    # Fallback: query seasons endpoint filtered by league_id (in case include is limited by plan)
    seasons = api_get("seasons", params={"filter": f"league_id:{league_id}", "per_page": 200})
    league = api_get(f"leagues/{league_id}")
    return {
        "data": {
            **(league.get("data") or {}),
            "seasons": (seasons.get("data") or []),
            "currentSeason": (api_get(f"leagues/{league_id}", params={"include": "currentSeason"}).get("data") or {}).get("currentSeason")
        }
    }

def normalize_seasons(obj: Dict[str, Any]) -> Tuple[str, List[Dict[str, Any]], Optional[int]]:
    league_name = ""
    current_id = None
    if not obj or not obj.get("data"):
        return league_name, [], current_id

    d = obj["data"]
    league_name = d.get("name") or f"League {d.get('id')}"
    current = d.get("currentSeason")
    if isinstance(current, dict):
        current_id = current.get("id")

    seasons = d.get("seasons") or []
    if isinstance(seasons, dict) and "data" in seasons:
        seasons = seasons["data"]
    if not isinstance(seasons, list):
        seasons = []

    # sort newest first by starting_at
    seasons.sort(key=lambda s: parse_date(s.get("starting_at")), reverse=True)
    return league_name, seasons, current_id

def main():
    ap = argparse.ArgumentParser(description="Print season IDs for one or more leagues.")
    ap.add_argument("--league-ids", type=int, nargs="+", default=[8],
                    help="League IDs (default: 8 = Premier League).")
    ap.add_argument("--current-only", action="store_true",
                    help="Print only the current season ID for each league.")
    ap.add_argument("--ids-only", action="store_true",
                    help="Print only numeric IDs (comma-separated per league).")
    args = ap.parse_args()

    any_output = False

    for lid in args.league_ids:
        data = fetch_league_with_seasons(lid)
        league_name, seasons, current_id = normalize_seasons(data)

        if args.current-only:
            # Only current season
            if current_id:
                if args.ids_only:
                    print(f"{lid}:{current_id}")
                else:
                    # Try to find label for current
                    cur = next((s for s in seasons if s.get("id") == current_id), None)
                    label = cur.get("name") if cur else ""
                    print(f"{league_name} (league_id={lid}) — current season: {current_id} {f'[{label}]' if label else ''}")
                any_output = True
            else:
                print(f"{league_name or f'League {lid}'} (league_id={lid}) — current season: not found / not in plan.")
            continue

        # All seasons for that league
        if not seasons:
            print(f"{league_name or f'League {lid}'} (league_id={lid}) — no seasons found.")
            continue

        any_output = True
        if args.ids_only:
            ids = ",".join(str(s.get("id")) for s in seasons if s.get("id"))
            print(f"{lid}:{ids}")
        else:
            print(f"{league_name} (league_id={lid}) — seasons (newest first):")
            for s in seasons:
                sid = s.get("id")
                name = s.get("name", "")
                start = s.get("starting_at", "")
                end = s.get("ending_at", "")
                star = " (current)" if current_id and sid == current_id else ""
                print(f"  - {sid}: {name}  [{start} → {end}]{star}")
            print()

    # CI-friendly: never fail the job just because nothing returned
    if not any_output:
        print("[INFO] No seasons printed (check token/plan/league IDs).")

if __name__ == "__main__":
    main()
