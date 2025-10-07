# scripts/common.py
import os
import json
import csv
from pathlib import Path

# ---- API config ----
BASE_URL = os.getenv("SPORTMONKS_BASE_URL", "https://api.sportmonks.com/v3")
API_TOKEN = os.getenv("SPORTMONKS_TOKEN")  # REQUIRED

if not API_TOKEN:
    raise RuntimeError("Missing SPORTMONKS_TOKEN env var")

# The only leagues we care about
ALLOWED_LEAGUES = [
    8,    # Premier League
    9,    # Championship
    384,  # Serie A
    387,  # Serie B
    82,   # Bundesliga
    301,  # Ligue 1
    564,  # La Liga
    567,  # La Liga 2
    600,  # Süper Lig
    72,   # Eredivisie
    271,  # Superliga (Denmark)
]

# ---- IO helpers ----
def _ensure_parent(path: str) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)

def write_json(path: str, obj) -> None:
    _ensure_parent(path)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)

def write_csv(path: str, fixtures: list) -> None:
    """
    Write a flat CSV of fixtures. Pulls home/away team names from participants.
    """
    _ensure_parent(path)
    fields = [
        "id",
        "league_id",
        "starting_at",
        "home_team",
        "away_team",
        "venue_id",
        "state",
    ]
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for fx in fixtures:
            home, away = None, None
            for p in fx.get("participants", []) or []:
                loc = (p.get("meta") or {}).get("location")
                if loc == "home":
                    home = p.get("name")
                elif loc == "away":
                    away = p.get("name")
            state_obj = fx.get("state") or {}
            w.writerow({
                "id": fx.get("id"),
                "league_id": fx.get("league_id"),
                "starting_at": fx.get("starting_at"),
                "home_team": home,
                "away_team": away,
                "venue_id": fx.get("venue_id"),
                "state": state_obj.get("short_name") or state_obj.get("name"),
            })
