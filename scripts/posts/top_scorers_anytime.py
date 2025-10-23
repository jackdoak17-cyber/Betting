#!/usr/bin/env python3
"""
Fetch season IDs from Sportmonks (no guessing) and print Premier League (or any league) top scorers.

Examples:
  export SPORTMONKS_TOKEN=your_api_token

  # Premier League current season only (default)
  python topscorers_resolved_seasons.py

  # Current season for multiple leagues (PL=8, La Liga=564, Serie A=384, Ligue 1=301, Bundesliga=82)
  python topscorers_resolved_seasons.py --league-ids 8 564 384 301 82

  # Last 2 seasons for Premier League
  python topscorers_resolved_seasons.py --league-ids 8 --last-n 2

  # All seasons for Premier League (careful: long)
  python topscorers_resolved_seasons.py --league-ids 8 --all
"""

import os
import sys
import argparse
import requests
from datetime import datetime
from typing import Dict, List, Optional, Tuple

BASE = "https://api.sportmonks.com/v3/football"

def get_token() -> str:
    token = os.getenv("SPORTMONKS_TOKEN") or os.getenv("SPORTMONKS_API_TOKEN")
    if not token:
        sys.exit("Error: set SPORTMONKS_TOKEN (or SPORTMONKS_API_TOKEN) in your environment.")
    return token

def api_get(path: str, token: str, params: Optional[dict] = None, timeout: int = 20) -> dict:
    params = params.copy() if params else {}
    params.setdefault("api_token", token)
    url = f"{BASE}/{path.lstrip('/')}"
    r = requests.get(url, params=params, timeout=timeout)
    try:
        r.raise_for_status()
    except requests.HTTPError as e:
        # return structured info for caller
        raise RuntimeError(f"HTTP {r.status_code} for GET {url}\nBody: {r.text}") from e
    try:
        return r.json()
    except Exception as e:
        raise RuntimeError(f"Non-JSON response from {url}: {r.text[:500]}") from e

def parse_date(s: Optional[str]) -> Tuple[int, int, int]:
    """Parse YYYY-MM-DD safely; return tuple for sorting; unknown -> very old date."""
    if not s:
        return (1900, 1, 1)
    try:
        d = datetime.fromisoformat(s)
        return (d.year, d.month, d.day)
    except Exception:
        return (1900, 1, 1)

def get_current_season_id(league_id: int, token: str) -> Optional[int]:
    """
    GET /leagues/{league_id}?include=currentSeason
    Returns currentSeason.id or None.
    """
    try:
        data = api_get(f"leagues/{league_id}", token, params={"include": "currentSeason"})
    except Exception as e:
        print(f"[ERROR] league {league_id}: failed to fetch currentSeason: {e}")
        return None
    league = data.get("data") or {}
    current = league.get("currentSeason")
    if isinstance(current, dict):
        return current.get("id")
    return None

def get_all_seasons_for_league(league_id: int, token: str) -> List[dict]:
    """
    GET /leagues/{league_id}?include=seasons
    Returns list of season dicts with id, name, starting_at, ending_at.
    """
    try:
        data = api_get(f"leagues/{league_id}", token, params={"include": "seasons"})
    except Exception as e:
        print(f"[ERROR] league {league_id}: failed to fetch seasons: {e}")
        return []
    league = data.get("data") or {}
    seasons = league.get("seasons") or []
    # normalize shape (some SDKs wrap in 'data')
    if isinstance(seasons, dict) and "data" in seasons:
        seasons = seasons["data"]
    # ensure list of dicts
    if not isinstance(seasons, list):
        return []
    return seasons

def choose_season_ids(league_id: int, token: str, current_only: bool, last_n: Optional[int], all_flag: bool) -> List[int]:
    if current_only and not last_n and not all_flag:
        sid = get_current_season_id(league_id, token)
        return [sid] if sid else []

    seasons = get_all_seasons_for_league(league_id, token)
    if not seasons:
        # fallback to current if available
        sid = get_current_season_id(league_id, token)
        return [sid] if sid else []

    # sort by starting_at descending (newest first)
    seasons_sorted = sorted(
        seasons,
        key=lambda s: parse_date(s.get("starting_at")),
        reverse=True,
    )

    if all_flag:
        return [s.get("id") for s in seasons_sorted if s.get("id")]

    if last_n and last_n > 0:
        seasons_sorted = seasons_sorted[:last_n]
        return [s.get("id") for s in seasons_sorted if s.get("id")]

    # default fallback: just current (top of sorted is usually current)
    return [seasons_sorted[0].get("id")] if seasons_sorted and seasons_sorted[0].get("id") else []

def fetch_top_scorers(season_id: int, token: str, per_page: int = 100) -> Optional[List[dict]]:
    """
    GET /topscorers/seasons/{season_id}?filter=seasonTopscorerTypes:208
    Returns rows or None on 404/unavailable.
    """
    path = f"topscorers/seasons/{season_id}"
    params = {
        "include": "player.nationality;player.position;participant;type;season.league",
        "filter": "seasonTopscorerTypes:208",  # CORRECT: singular 'filter', correct key
        "per_page": per_page,
    }
    try:
        data = api_get(path, token, params=params)
    except RuntimeError as e:
        msg = str(e)
        if "HTTP 404" in msg:
            print(f"[INFO] season {season_id}: topscorers 404 — not available / not in plan.")
            return None
        print(f"[ERROR] season {season_id}: {e}")
        return None
    return data.get("data", data)

def best_player_name(p: dict) -> str:
    return p.get("display_name") or p.get("common_name") or p.get("name") or f"Player {p.get('id')}"

def print_table(rows: List[dict], limit: Optional[int]) -> None:
    if not rows:
        print("No topscorers found.\n")
        return
    rows_sorted = sorted(rows, key=lambda r: (r.get("position", 10**9), -r.get("total", 0)))
    if isinstance(limit, int) and limit > 0:
        rows_sorted = rows_sorted[:limit]

    league = rows_sorted[0].get("season", {}).get("league", {}).get("name", "")
    season_label = rows_sorted[0].get("season", {}).get("name", "")
    header = (league or "League") + (f" {season_label}" if season_label else "")
    print(f"{header} — Top Scorers")
    for r in rows_sorted:
        pos = r.get("position")
        goals = r.get("total", 0)
        team = (r.get("participant", {}) or {}).get("name", "N/A")
        player = r.get("player", {}) or {}
        name = best_player_name(player)
        prefix = f"{pos}." if pos is not None else "-"
        print(f"{prefix} {name} ({team}) — {goals}")
    print()

def main():
    parser = argparse.ArgumentParser(description="Resolve Sportmonks season IDs via the API and print topscorers.")
    parser.add_argument("--league-ids", type=int, nargs="+", default=[8],
                        help="One or more league IDs (default: 8 = Premier League).")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--current", action="store_true", help="Use current season only (default).")
    group.add_argument("--all", dest="all_flag", action="store_true", help="Use all seasons for each league.")
    group.add_argument("--last-n", type=int, help="Use last N seasons (by starting_at) for each league.")
    parser.add_argument("--limit", type=int, default=10, help="Max rows per season (0 = all).")
    parser.add_argument("--per-page", type=int, default=100, help="Items per page for topscorers.")
    args = parser.parse_args()

    token = get_token()
    current_only = not args.all_flag and not args.last_n
    limit = None if args.limit == 0 else args.limit

    # Resolve seasons per league (no guessing)
    season_ids: List[int] = []
    for lid in args.league_ids:
        sids = choose_season_ids(lid, token, current_only=current_only, last_n=args.last_n, all_flag=args.all_flag)
        if not sids:
            print(f"[WARN] league {lid}: no season IDs resolved.")
        else:
            print(f"[INFO] league {lid}: seasons resolved -> {sids}")
            season_ids.extend(sids)

    # De-dup & keep order
    seen = set()
    ordered_sids = []
    for sid in season_ids:
        if sid and sid not in seen:
            seen.add(sid)
            ordered_sids.append(sid)

    if not ordered_sids:
        sys.exit("[RESULT] No season IDs resolved. Nothing to do.")

    # Fetch & print topscorers for each season
    any_rows = False
    for sid in ordered_sids:
        rows = fetch_top_scorers(sid, token, per_page=args.per_page)
        if rows:
            any_rows = True
            print_table(rows, limit)
    if not any_rows:
        print("[RESULT] No topscorer rows found across selected seasons.")

if __name__ == "__main__":
    main()
