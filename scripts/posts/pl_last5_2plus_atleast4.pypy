#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Premier League — Last 5 (≥4/5 hits at 2+)
Categories: Shots, Fouls, Fouls Drawn

Finds:
  • 2+ in ≥4 of last 5

Requires prebuilt JSONs from your extractors:
  - data/player_shots/by_league/{LEAGUE_ID}.json            (.. "shots_last_n": [...])
  - data/player_fouls/by_league/{LEAGUE_ID}.json            (.. "fouls_last_n": [...])
  - data/player_fouls_drawn/by_league/{LEAGUE_ID}.json      (.. "fouls_drawn_last_n": [...])

ENV (optional):
  OUTPUT_PATH    (default: posts/pl_last5_2plus_atleast4.md)
  LEAGUE_ID      (default: 8  — Premier League)
  MIN_MINUTES    (default: 45 — require ≥45' in ≥4 of last 5)
  BULLET         (default: "• ")
"""

import os
import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# ----- Config -----
ROOT        = Path(".")
OUT_PATH    = Path(os.getenv("OUTPUT_PATH", "posts/pl_last5_2plus_atleast4.md"))
LEAGUE_ID   = int(os.getenv("LEAGUE_ID", "8"))
MIN_MINUTES = int(os.getenv("MIN_MINUTES", "45"))
BULLET      = os.getenv("BULLET", "• ")

SHOTS_FILE      = ROOT / "data" / "player_shots" / "by_league" / f"{LEAGUE_ID}.json"
FOULS_FILE      = ROOT / "data" / "player_fouls" / "by_league" / f"{LEAGUE_ID}.json"
F_DRAWN_FILE    = ROOT / "data" / "player_fouls_drawn" / "by_league" / f"{LEAGUE_ID}.json"

# fixtures path can be stored in either location depending on your other jobs
FIX_FILE        = ROOT / "data" / "fixtures" / "by_league" / f"{LEAGUE_ID}.json"
FIX_FILE_ALT    = ROOT / "data" / "fixtures" / f"{LEAGUE_ID}.json"

# ----- IO helpers -----
def _load_json(p: Path) -> Any:
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None

# ----- Name helpers -----
SUFFIXES = {"jr", "junior", "sr", "senior", "ii", "iii", "iv", "filho", "neto"}

def short_player(name: str) -> str:
    """Return 'F. Last' with suffixes removed."""
    raw = (name or "").strip()
    if not raw:
        return ""
    parts = [p for p in re.split(r"\s+", raw) if p]
    # remove suffixes like "Jr", "III", etc
    while parts and re.sub(r"[\W_]+", "", parts[-1]).lower() in SUFFIXES:
        parts = parts[:-1]
    last = parts[-1] if parts else raw
    first_initial = parts[0][0:1] if parts else ""
    return f"{first_initial}. {last}"

# ----- Team names -----
PRETTY_MAP = {
    "Manchester City": "Man City",
    "Manchester United": "Man Utd",
    "Newcastle United": "Newcastle",
    "Nottingham Forest": "Nottm Forest",
    "Brighton & Hove Albion": "Brighton",
    "Tottenham Hotspur": "Spurs",
    "Wolverhampton Wanderers": "Wolves",
    "West Ham United": "West Ham",
    "AFC Bournemouth": "Bournemouth",
    "Sheffield United": "Sheff Utd",
}

def pretty_team(name: Optional[str]) -> str:
    if not name:
        return "TBC"
    return PRETTY_MAP.get(name, name)

def build_team_map_from_fixtures() -> Dict[int, str]:
    """
    Builds {team_id: team_name} from fixtures JSON.
    Supports a few shapes:
      - top-level: {"fixtures":[{"participants":[{"id":..,"name":..},...]},...]}
      - top-level: {"data":{"fixtures":[...]}}
      - participants may use keys {"id","name"} or {"team_id","name"}
    """
    m: Dict[int, str] = {}
    blob = _load_json(FIX_FILE) or _load_json(FIX_FILE_ALT) or {}
    fixtures = blob.get("fixtures") or (blob.get("data") or {}).get("fixtures") or []

    for fx in fixtures:
        participants = fx.get("participants") or fx.get("teams") or []
        # Sometimes home/away nested as dicts — normalize to list of dicts
        if isinstance(participants, dict):
            participants = list(participants.values())

        for p in (participants or []):
            tid = p.get("id") or p.get("team_id")
            nm = p.get("name")
            try:
                tid = int(tid)
            except Exception:
                continue
            if isinstance(nm, str) and nm:
                m.setdefault(tid, nm)

        # Fallback: some shapes store explicit home/away dicts
        for key in ("home", "away", "localteam", "visitorteam"):
            t = fx.get(key)
            if isinstance(t, dict):
                tid = t.get("id") or t.get("team_id")
                nm = t.get("name")
                try:
                    tid = int(tid)
                except Exception:
                    continue
                if isinstance(nm, str) and nm:
                    m.setdefault(tid, nm)

    return m

# ----- Series helpers -----
def take_last5(series: List[int]) -> Optional[List[int]]:
    if not isinstance(series, list) or len(series) < 5:
        return None
    return [int(x) for x in series[:5]]

def count_ge(series5: List[int], threshold: int) -> int:
    return sum(1 for x in series5 if isinstance(x, (int, float)) and x >= threshold)

def minutes_ok(minutes_last_n: Optional[List[int]]) -> bool:
    """
    True if at least 4 of the most recent 5 (or fewer if <5 available) entries
    meet MIN_MINUTES. When MIN_MINUTES==0, always True.
    """
    if MIN_MINUTES <= 0:
        return True
    if not minutes_last_n or not isinstance(minutes_last_n, list):
        return False
    recent5 = minutes_last_n[:5] if len(minutes_last_n) >= 5 else minutes_last_n
    return sum(1 for m in recent5 if isinstance(m, (int, float)) and m >= MIN_MINUTES) >= 4

# ----- Core -----
def collect_category_lines() -> List[str]:
    team_map = build_team_map_from_fixtures()

    shots_blob  = _load_json(SHOTS_FILE)   or {}
    fouls_blob  = _load_json(FOULS_FILE)   or {}
    fdrawn_blob = _load_json(F_DRAWN_FILE) or {}

    shots_players  = shots_blob.get("players")  or []
    fouls_players  = fouls_blob.get("players")  or []
    fdrawn_players = fdrawn_blob.get("players") or []

    sections: List[str] = []

    def build_block(players: List[dict], series_key: str, title: str, threshold: int = 2, need_hits: int = 4) -> None:
        rows: List[Tuple[str, str, List[int], int]] = []
        for rec in players:
            series = take_last5(rec.get(series_key) or [])
            if not series:
                continue
            if not minutes_ok(rec.get("minutes_last_n")):
                continue
            hits = count_ge(series, threshold)
            if hits >= need_hits:
                team_name = team_map.get(int(rec.get("team_id", -1)), "") or ""
                rows.append((rec.get("name", ""), team_name, series, hits))
        # Sort: more 2+ hits first, then higher sum of recent 5, then name
        rows.sort(key=lambda t: (-t[3], -sum(t[2]), t[0]))
        if rows:
            sections.append(title)
            sections.append("")
            for name, team, series, hits in rows:
                sections.append(f"{BULLET}{short_player(name)} ({pretty_team(team)}) = {', '.join(map(str, series))}  — {hits}/5 @2+")
            sections.append("")

    # Order: Shots, Fouls, Fouls Drawn
    build_block(shots_players, "shots_last_n",        "📊2+ Shots in ≥4/5")
    build_block(fouls_players, "fouls_last_n",        "📊2+ Fouls Committed in ≥4/5")
    build_block(fdrawn_players,"fouls_drawn_last_n",  "📊2+ Fouls Drawn in ≥4/5")

    return sections

def main():
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)

    header = [
        "Premier League: players hitting **2+** in **≥4 of their last 5** (Shots, Fouls, Fouls Drawn).",
        f"Minutes gate: ≥{MIN_MINUTES}' in ≥4/5.",
        "",
    ]

    sections = collect_category_lines()
    if not sections:
        sections = ["(No qualifying players found based on current files.)", ""]

    footer = [
        "Use this as a shortlist, not advice. Shop around & play responsibly.",
    ]

    text = "\n".join(header + sections + [""] + footer).rstrip() + "\n"
    OUT_PATH.write_text(text, encoding="utf-8")
    print(f"Wrote {OUT_PATH}")

if __name__ == "__main__":
    main()
