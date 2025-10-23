#!/usr/bin/env python3
"""
Top scorers for the current season in the top-5 European leagues
(PL, LaLiga, Bundesliga, Serie A, Ligue 1) using Sportmonks v3.

Usage (local):
  export SPORTMONKS_TOKEN=your_token_here
  python scripts/posts/top_scorers_anytime.py -o posts/top_scorers_anytime.md --limit 10
"""

import argparse
import datetime as dt
import json
import os
import sys
from typing import Any, Dict, List, Optional

import requests

API_BASE = "https://api.sportmonks.com/v3/football"

# Your mapping (kept exactly as you provided)
LEAGUE_SLUG_BY_ID = {
    8:   "england-premier-league",
    9:   "england-championship",
    82:  "germany-bundesliga",
    301: "france-ligue-1",
    384: "italy-serie-a",
    387: "italy-serie-b",
    564: "spain-laliga",
    567: "spain-laliga-2",
    72:  "netherlands-eredivisie",
    600: "turkiye-super-lig",
}

TOP5_LEAGUES = [8, 564, 82, 384, 301]  # PL, LaLiga, Bundesliga, Serie A, Ligue 1

LEAGUE_ID_TO_NAME = {
    8: "Premier League",
    564: "LaLiga",
    82: "Bundesliga",
    384: "Serie A",
    301: "Ligue 1",
}

def get_token() -> str:
    token = os.getenv("SPORTMONKS_TOKEN")
    if not token:
        sys.stderr.write(
            "ERROR: SPORTMONKS_TOKEN env var is not set. "
            "Add it as a GitHub secret and pass via the workflow env.\n"
        )
        sys.exit(1)
    return token

def api_get(path: str, token: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    url = f"{API_BASE}/{path.lstrip('/')}"
    p = {"api_token": token}
    if params:
        p.update(params)
    r = requests.get(url, params=p, timeout=30)
    r.raise_for_status()
    return r.json()

def get_current_season_id(league_id: int, token: str) -> Optional[int]:
    data = api_get(f"leagues/{league_id}", token, params={"include": "currentSeason"})
    cs = (data.get("data") or {}).get("currentseason")
    return (cs or {}).get("id")

def _safe(obj: Dict[str, Any], *keys, default=None):
    cur = obj
    for k in keys:
        if isinstance(cur, dict) and k in cur:
            cur = cur[k]
        else:
            return default
    return cur

def _player_name(item: Dict[str, Any]) -> str:
    pd = _safe(item, "player", "data", default={}) or {}
    return (
        pd.get("display_name")
        | pd.get("common_name")
        | pd.get("fullname")
        | pd.get("name")
        if isinstance(pd.get("display_name") or pd.get("common_name") or pd.get("fullname") or pd.get("name"), str)
        else f"Player {item.get('player_id')}"
    )

def _team_name(item: Dict[str, Any]) -> str:
    td = _safe(item, "team", "data", default={}) or {}
    if isinstance(td.get("name"), str) and td.get("name"):
        return td["name"]
    if isinstance(td.get("short_code"), str) and td.get("short_code"):
        return td["short_code"]
    return f"Team {item.get('team_id')}"

def _goals(item: Dict[str, Any]) -> int:
    # Sportmonks top scorers payload typically exposes "goals" and "penalty_goals".
    # Be defensive in case the schema differs.
    for key in ("goals", "total", "scored"):
        v = item.get(key)
        if isinstance(v, (int, float)):
            return int(v)
    stats = item.get("stats") or item.get("statistics") or {}
    for key in ("goals", "total", "scored"):
        v = stats.get(key)
        if isinstance(v, (int, float)):
            return int(v)
    return 0

def _pen_goals(item: Dict[str, Any]) -> int:
    v = item.get("penalty_goals")
    if isinstance(v, (int, float)):
        return int(v)
    return 0

def fetch_top_scorers(season_id: int, token: str, limit: int = 10) -> List[Dict[str, Any]]:
    data = api_get(f"topscorers/seasons/{season_id}", token, params={"include": "player;team"})
    items = data.get("data") or []
    # Sort by goals desc (defensive on missing fields)
    items.sort(key=_goals, reverse=True)
    return items[: max(1, limit)]

def make_markdown(leagues: List[int], token: str, limit: int) -> str:
    ts = dt.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
    lines = [f"# Top Scorers (Anytime) — Updated {ts}", ""]
    for lid in leagues:
        league_name = LEAGUE_ID_TO_NAME.get(lid, f"League {lid}")
        try:
            season_id = get_current_season_id(lid, token)
            if not season_id:
                raise RuntimeError("No current season id found")
            scorers = fetch_top_scorers(season_id, token, limit=limit)
            lines.append(f"## {league_name} — Current Season (season_id: {season_id})")
            lines.append("")
            lines.append("| # | Player | Team | Goals (pens) |")
            lines.append("|---:|---|---|---:|")
            for i, item in enumerate(scorers, start=1):
                lines.append(
                    f"| {i} | {_player_name(item)} | {_team_name(item)} | {_goals(item)} ({_pen_goals(item)}) |"
                )
            lines.append("")
        except Exception as e:
            lines.append(f"## {league_name}")
            lines.append("")
            lines.append(f"> Error fetching data: `{type(e).__name__}: {e}`")
            lines.append("")
    return "\n".join(lines)

def main():
    parser = argparse.ArgumentParser(description="Generate a markdown table of top scorers for the top-5 leagues.")
    parser.add_argument("--limit", type=int, default=10, help="Number of scorers per league.")
    parser.add_argument("--leagues", type=str, default="8,564,82,384,301",
                        help="Comma-separated league IDs to include.")
    parser.add_argument("-o", "--output", type=str, default="",
                        help="Output markdown path (e.g., posts/top_scorers_anytime.md). If empty, prints to stdout.")
    args = parser.parse_args()

    token = get_token()
    leagues = [int(x.strip()) for x in args.leagues.split(",") if x.strip()]
    md = make_markdown(leagues, token, limit=args.limit)

    if args.output:
        os.makedirs(os.path.dirname(args.output), exist_ok=True)
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(md)
        print(f"Wrote: {args.output}")
    else:
        print(md)

if __name__ == "__main__":
    main()
