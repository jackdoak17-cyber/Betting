#!/usr/bin/env python3
"""
Print top scorers for one or more Sportmonks Football seasons.

Usage examples:
  export SPORTMONKS_TOKEN=your_api_token

  # Premier League 2025/26 only
  python top_scorers_multi.py --season-ids 25583

  # The seasons you tried earlier (PL + others)
  python top_scorers_multi.py --season-ids 25583 25659 25533 25646 25651 --limit 10
"""

import os
import sys
import argparse
import requests
from typing import List, Optional

API_URL = "https://api.sportmonks.com/v3/football/topscorers/seasons/{season_id}"

def get_token() -> str:
    token = os.getenv("SPORTMONKS_TOKEN") or os.getenv("SPORTMONKS_API_TOKEN")
    if not token:
        sys.exit("Error: set SPORTMONKS_TOKEN (or SPORTMONKS_API_TOKEN) in your environment.")
    return token

def fetch_top_scorers(season_id: int, token: str, per_page: int = 100) -> Optional[list]:
    """
    Returns a list of topscorer rows or None if 404 (season not in plan / invalid).
    """
    url = API_URL.format(season_id=season_id)
    params = {
        "api_token": token,
        "include": "player.nationality;player.position;participant;type;season.league",
        "filter": "seasonTopscorerTypes:208",  # CORRECT: singular 'filter', proper casing
        "per_page": per_page,                  # 100 is plenty for topscorer lists
    }

    resp = requests.get(url, params=params, timeout=20)
    if resp.status_code == 404:
        print(f"[INFO] season {season_id}: 404 — skipping (invalid/unavailable or not in plan).")
        return None

    try:
        resp.raise_for_status()
    except requests.HTTPError as e:
        try:
            body = resp.json()
        except Exception:
            body = resp.text
        print(f"[ERROR] season {season_id}: HTTP error: {e}\nResponse body: {body}")
        return None

    data = resp.json()
    return data.get("data", data)

def best_name(player: dict) -> str:
    return (
        player.get("display_name")
        or player.get("common_name")
        or player.get("name")
        or f"Player {player.get('id')}"
    )

def print_table(rows: list, limit: Optional[int]) -> None:
    if not rows:
        print("No topscorers found.\n")
        return

    rows_sorted = sorted(rows, key=lambda r: (r.get("position", 10**9), -r.get("total", 0)))
    if isinstance(limit, int) and limit > 0:
        rows_sorted = rows_sorted[:limit]

    # Header tries to show league + season name when present
    league = rows_sorted[0].get("season", {}).get("league", {}).get("name", "")
    season_label = rows_sorted[0].get("season", {}).get("name", "")
    header = (league or "League") + (f" {season_label}" if season_label else "")
    print(f"{header} — Top Scorers")
    for r in rows_sorted:
        pos = r.get("position")
        goals = r.get("total", 0)
        player = r.get("player", {}) or {}
        team = (r.get("participant", {}) or {}).get("name", "N/A")
        name = best_name(player)
        prefix = f"{pos}." if pos is not None else "-"
        print(f"{prefix} {name} ({team}) — {goals}")
    print()  # blank line between seasons

def main():
    parser = argparse.ArgumentParser(description="Print Sportmonks top scorers for multiple seasons.")
    parser.add_argument(
        "--season-ids",
        type=int,
        nargs="+",
        default=[25583],  # Premier League 2025/26
        help="One or more Sportmonks season IDs (space-separated).",
    )
    parser.add_argument("--limit", type=int, default=10, help="Max rows per season (0 = all).")
    parser.add_argument("--per-page", type=int, default=100, help="Items per page (safety margin).")
    args = parser.parse_args()

    token = get_token()
    limit = None if args.limit == 0 else args.limit

    for sid in args.season_ids:
        rows = fetch_top_scorers(sid, token, per_page=args.per_page)
        if rows is None:
            continue
        print_table(rows, limit)

if __name__ == "__main__":
    main()
