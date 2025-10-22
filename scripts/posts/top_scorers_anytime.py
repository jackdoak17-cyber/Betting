#!/usr/bin/env python3
"""
Print Premier League top scorers (Sportmonks Football API v3).

Usage:
  export SPORTMONKS_TOKEN=your_api_token
  python top_scorers_pl.py --limit 10

Notes:
- Uses the 'season' topscorers endpoint with the correct *singular* 'filter'
  parameter: filter=seasonTopscorerTypes:208
"""

import os
import sys
import argparse
import requests

API_URL = "https://api.sportmonks.com/v3/football/topscorers/seasons/{season_id}"
DEFAULT_SEASON_ID = 25583  # Premier League 2025/26

def get_token() -> str:
    token = os.getenv("SPORTMONKS_TOKEN") or os.getenv("SPORTMONKS_API_TOKEN")
    if not token:
        sys.exit("Error: set SPORTMONKS_TOKEN (or SPORTMONKS_API_TOKEN) in your environment.")
    return token

def fetch_top_scorers(season_id: int, token: str, per_page: int = 50):
    url = API_URL.format(season_id=season_id)
    params = {
        "api_token": token,
        "include": "player.nationality;player.position;participant;type;season.league",
        "filter": "seasonTopscorerTypes:208",  # <-- correct key & camelCase
        "per_page": per_page,
    }

    resp = requests.get(url, params=params, timeout=20)
    if resp.status_code == 404:
        sys.exit(f"404 from {url}. Check your season_id ({season_id}) or plan access.")
    try:
        resp.raise_for_status()
    except requests.HTTPError as e:
        # Show API error body to make debugging easier
        body = ""
        try:
            body = resp.json()
        except Exception:
            body = resp.text
        sys.exit(f"HTTP error: {e}\nResponse body: {body}")

    data = resp.json()
    # Sportmonks usually returns {"data": [...]}; handle raw-list just in case
    return data["data"] if isinstance(data, dict) and "data" in data else data

def best_name(player: dict) -> str:
    return (
        player.get("display_name")
        or player.get("common_name")
        or player.get("name")
        or f"Player {player.get('id')}"
    )

def print_table(rows, limit: int | None):
    if not rows:
        print("No topscorers found.")
        return

    # Sort by API-provided position if present; fallback to goals desc
    rows_sorted = sorted(rows, key=lambda r: (r.get("position", 10**9), -r.get("total", 0)))
    if limit:
        rows_sorted = rows_sorted[:limit]

    # Header
    # Try to read league name if present; otherwise default label
    league_name = "Premier League 2025/26"
    if rows_sorted and rows_sorted[0].get("season", {}).get("league", {}).get("name"):
        league_name = rows_sorted[0]["season"]["league"]["name"] + " 2025/26"

    print(f"{league_name} — Top Scorers")
    for r in rows_sorted:
        pos = r.get("position")
        goals = r.get("total", 0)
        player = r.get("player", {}) or {}
        team = (r.get("participant", {}) or {}).get("name", "N/A")
        name = best_name(player)
        prefix = f"{pos}." if pos is not None else "-"
        print(f"{prefix} {name} ({team}) — {goals}")

def main():
    parser = argparse.ArgumentParser(description="Print Premier League top scorers (Sportmonks).")
    parser.add_argument("--season-id", type=int, default=DEFAULT_SEASON_ID, help="Sportmonks season_id (default: 25583)")
    parser.add_argument("--limit", type=int, default=10, help="Max rows to print (default: 10; use 0 for all)")
    args = parser.parse_args()

    token = get_token()
    rows = fetch_top_scorers(args.season_id, token)
    print_table(rows, None if args.limit == 0 else args.limit)

if __name__ == "__main__":
    main()
