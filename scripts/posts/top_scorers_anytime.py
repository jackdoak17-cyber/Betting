#!/usr/bin/env python3
import os
import sys
import datetime
import requests

BASE = "https://api.sportmonks.com/v3/football"
TOKEN = os.getenv("SPORTMONKS_TOKEN")
if not TOKEN:
    sys.exit("Missing SPORTMONKS_TOKEN")

# Top 5 leagues
LEAGUES = {
    8:   "Premier League",
    564: "LaLiga",
    82:  "Bundesliga",
    384: "Serie A",
    301: "Ligue 1",
}

def now_utc():
    return datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")

def get_current_season(league_id: int) -> int:
    url = f"{BASE}/leagues/{league_id}"
    r = requests.get(url, params={"api_token": TOKEN, "include": "currentSeason"}, timeout=30)
    r.raise_for_status()
    data = r.json()["data"]
    season = data.get("currentseason") or data.get("currentSeason")
    if not season:
        raise RuntimeError(f"No currentSeason for league {league_id}")
    return int(season["id"])

def fetch_topscorers_via_endpoint(season_id: int):
    url = f"{BASE}/topscorers/seasons/{season_id}"
    params = {
        "api_token": TOKEN,
        "include": "player;participant;type",     # 'participant' is the team relation
        "filters": "seasonTopscorerTypes:208",   # 208 = Goals (anytime)
        "per_page": 50,
        "order": "asc",
    }
    r = requests.get(url, params=params, timeout=30)
    return r

def fetch_topscorers_via_season_include(season_id: int):
    # Fallback path using seasons endpoint with topscorers include
    url = f"{BASE}/seasons/{season_id}"
    params = {
        "api_token": TOKEN,
        "include": "topscorers.player;topscorers.participant;topscorers.type",
        "filters": "seasonTopscorerTypes:208",
        "per_page": 50,
    }
    r = requests.get(url, params=params, timeout=30)
    return r

def parse_topscorers(payload):
    # Works for both endpoints
    if isinstance(payload.get("data"), dict) and "topscorers" in payload["data"]:
        items = payload["data"]["topscorers"] or []
    else:
        items = payload.get("data", []) or []

    # Aggregate by player across potential multiple stages
    agg = {}
    for row in items:
        player = row.get("player") or (row.get("topscorer") or {}).get("player")
        team = row.get("participant") or (row.get("topscorer") or {}).get("participant")
        total = row.get("total") or (row.get("value") or {}).get("total")
        if not player or total is None:
            continue
        pid = player["id"]
        key = (pid, team["id"] if team else None)
        entry = agg.setdefault(
            key,
            {
                "player": player.get("display_name") or player.get("fullname") or player.get("name"),
                "team": (team or {}).get("name", "—"),
                "total": 0,
            },
        )
        try:
            entry["total"] += int(total)
        except Exception:
            pass

    ranked = sorted(agg.values(), key=lambda x: (-x["total"], x["player"]))[:10]
    return ranked

def main():
    lines = []
    lines.append(f"Top Scorers (Anytime) — Updated {now_utc()}\n")

    for league_id, league_name in LEAGUES.items():
        lines.append(league_name)
        try:
            season_id = get_current_season(league_id)

            r = fetch_topscorers_via_endpoint(season_id)
            if r.status_code == 404:
                # Some seasons/leagues prefer the seasons-include route
                r = fetch_topscorers_via_season_include(season_id)
            r.raise_for_status()

            leaders = parse_topscorers(r.json())
            if not leaders:
                lines.append("No data yet.")
            else:
                for i, row in enumerate(leaders, 1):
                    lines.append(f"{i}. {row['player']} — {row['team']} — {row['total']}")
        except Exception as e:
            lines.append(f"Error fetching data: {type(e).__name__}: {e}")
        lines.append("")  # blank line after each league

    out_path = os.getenv("OUTPUT_PATH", "posts/top_scorers_anytime.md")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print(f"Wrote {out_path}")

if __name__ == "__main__":
    main()
