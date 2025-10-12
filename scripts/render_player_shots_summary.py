#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Render a clean, human-readable summary grouped by team from
data/player_shots/by_league/*.json

Writes:
- data/player_shots/summary_by_team.txt

Assumptions:
- Each by_league JSON contains a list of player rows under "players".
- Each player row has: player_id, name, team_id, team_name (or we will try
  to resolve team_name from predicted_xi), and either "role" or "position_label",
  plus "shots" (list of ints).
"""

import os, json, glob
from pathlib import Path
from typing import Dict, List, Any

ROOT = Path(".")
SHOTS_DIR = ROOT / "data" / "player_shots" / "by_league"
PRED_XI_DIR = ROOT / "data" / "predicted_xi" / "by_league"
OUT_PATH = ROOT / "data" / "player_shots" / "summary_by_team.txt"


def _load_json(path: Path) -> Any:
    if not path.is_file():
        return None
    with path.open("r", encoding="utf-8") as f:
        try:
            return json.load(f)
        except Exception:
            return None


def _team_names_from_predicted_xi(league_id: int) -> Dict[int, str]:
    """Fallback mapping team_id -> name from predicted XI."""
    m: Dict[int, str] = {}
    px_path = PRED_XI_DIR / f"{league_id}.json"
    blob = _load_json(px_path) or {}
    for fx in (blob.get("fixtures") or []):
        for side in ("home", "away"):
            side_obj = fx.get(side) or {}
            tid = side_obj.get("team_id")
            nm = side_obj.get("name")
            if isinstance(tid, int) and nm:
                m.setdefault(tid, str(nm))
    return m


def _players_list(payload: dict) -> List[dict]:
    # primary key
    if isinstance(payload, dict):
        for key in ("players", "items", "rows"):
            if isinstance(payload.get(key), list):
                return [x for x in payload[key] if isinstance(x, dict)]
    # last resort: scan dict values for a list of dicts with player_id
    if isinstance(payload, dict):
        for v in payload.values():
            if isinstance(v, list) and v and isinstance(v[0], dict) and "player_id" in v[0]:
                return v
    return []


def format_shots(seq: List[Any]) -> str:
    # coerce to ints/strings and join with commas, no spaces
    try:
        return ",".join(str(int(x)) for x in seq)
    except Exception:
        return ",".join(str(x) for x in seq)


def main():
    SHOTS_DIR.mkdir(parents=True, exist_ok=True)
    lines: List[str] = []
    league_files = sorted(glob.glob(str(SHOTS_DIR / "*.json")), key=lambda p: int(Path(p).stem))

    total_players = 0
    for path in league_files:
        data = _load_json(Path(path)) or {}
        league_id = int(data.get("league_id") or Path(path).stem)
        league_name = data.get("league_name") or f"League {league_id}"

        players = _players_list(data)
        if not players:
            # skip empty league files gracefully
            continue

        # Try to ensure we have team names
        team_name_map: Dict[int, str] = {}
        for r in players:
            tid = r.get("team_id")
            tname = r.get("team_name")
            if isinstance(tid, int) and isinstance(tname, str) and tname.strip():
                team_name_map[tid] = tname.strip()
        if not team_name_map:
            team_name_map = _team_names_from_predicted_xi(league_id)

        # Group players by team_id
        by_team: Dict[int, List[dict]] = {}
        for r in players:
            tid = r.get("team_id")
            if not isinstance(tid, int):
                continue
            by_team.setdefault(tid, []).append(r)

        # Header per league
        lines.append(f"===== {league_name} (LID {league_id}) =====")

        # Sort teams by readable name
        def team_display_name(tid: int) -> str:
            return team_name_map.get(tid, f"Team {tid}")

        for tid in sorted(by_team.keys(), key=lambda x: team_display_name(x).lower()):
            tname = team_display_name(tid)
            lines.append(tname)

            # Sort players by name
            rows = sorted(by_team[tid], key=lambda r: (r.get("name") or "").lower())
            for r in rows:
                name = (r.get("name") or "").strip() or f"Player {r.get('player_id')}"
                tag = (r.get("role") or r.get("position_tag") or r.get("position_label") or "").strip()
                seq = r.get("shots") or []
                seq_s = format_shots(seq)
                n = len(seq)
                # Example: "  Jarrod Bowen (RW): 1,0,2,1,0  [5]"
                if tag:
                    lines.append(f"  {name} ({tag}): {seq_s}  [{n}]")
                else:
                    lines.append(f"  {name}: {seq_s}  [{n}]")
            lines.append("")  # blank line between teams

        total_players += len(players)
        lines.append("")  # blank line between leagues

    # Write the output
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with OUT_PATH.open("w", encoding="utf-8") as f:
        f.write("\n".join(lines).rstrip() + "\n")

    print(f"[OK] wrote {OUT_PATH} (covering ~{total_players} players)")


if __name__ == "__main__":
    main()
