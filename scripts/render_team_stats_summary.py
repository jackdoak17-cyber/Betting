#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Render a human-checkable summary per league:
- data/team_stats/by_league/*.json  -> data/team_stats/summary_by_team.txt
"""

import json, glob
from pathlib import Path
from typing import Any, Dict, List

ROOT = Path(".")
IN_DIR = ROOT / "data" / "team_stats" / "by_league"
OUT_PATH = ROOT / "data" / "team_stats" / "summary_by_team.txt"

KEY_LABELS = {
    "shots_on_target_last_n": "SOT",
    "shots_total_last_n": "Shots",
    "fouls_last_n": "Fouls",
    "tackles_last_n": "Tackles",
    "cards_total_last_n": "Cards",
    "saves_last_n": "Saves",
    "goal_kicks_last_n": "GKicks",
    "corners_last_n": "Corners",
}

def _load_json(p: Path) -> Any:
    if not p.is_file(): return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None

def _teams(payload: dict) -> List[dict]:
    return [x for x in (payload.get("teams") or []) if isinstance(x, dict)]

def _series(row: dict, key: str) -> List[int]:
    seq = row.get(key) or []
    if isinstance(seq, list):
        try: return [int(x) for x in seq]
        except Exception: pass
    return []

def main():
    lines: List[str] = []
    for path in sorted(glob.glob(str(IN_DIR / "*.json")), key=lambda p: int(Path(p).stem)):
        data = _load_json(Path(path)) or {}
        lid = int(data.get("league_id") or Path(path).stem)
        teams = _teams(data)
        if not teams:
            continue

        lines.append(f"===== League {lid} =====")
        for row in sorted(teams, key=lambda r: (r.get('team_name') or '').lower()):
            name = (row.get("team_name") or f"TID {row.get('team_id')}").strip()
            parts = []
            for k, label in KEY_LABELS.items():
                seq = _series(row, k)
                if seq:
                    parts.append(f"{label}: " + ",".join(str(x) for x in seq))
            lines.append(f"{name}: " + " | ".join(parts))
        lines.append("")

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    print(f"[OK] wrote {OUT_PATH}")

if __name__ == "__main__":
    main()
