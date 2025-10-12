#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Render a clean, team-grouped summary from:
- data/player_shots_on_target/by_league/*.json   (expects 'players' with 'on_target_last_n')
and team names from:
- data/predicted_xi/by_league/{league_id}.json

Writes:
- data/player_shots_on_target/summary_by_team.txt
"""

import json, glob
from pathlib import Path
from typing import Dict, List, Any

ROOT = Path(".")
SOT_DIR = ROOT / "data" / "player_shots_on_target" / "by_league"
PX_DIR  = ROOT / "data" / "predicted_xi" / "by_league"
OUT_PATH = ROOT / "data" / "player_shots_on_target" / "summary_by_team.txt"

def _load_json(p: Path) -> Any:
    if not p.is_file(): return None
    with p.open("r", encoding="utf-8") as f:
        try: return json.load(f)
        except Exception: return None

def _team_names_from_predicted_xi(league_id: int) -> Dict[int, str]:
    m: Dict[int, str] = {}
    blob = _load_json(PX_DIR / f"{league_id}.json") or {}
    for fx in (blob.get("fixtures") or []):
        for side in ("home", "away"):
            s = fx.get(side) or {}
            tid, nm = s.get("team_id"), s.get("name")
            if isinstance(tid, int) and isinstance(nm, str) and nm:
                m.setdefault(tid, nm)
    return m

def _players(payload: dict) -> List[dict]:
    return [x for x in (payload.get("players") or []) if isinstance(x, dict)]

def _series(row: dict) -> List[int]:
    seq = row.get("on_target_last_n") or []
    if isinstance(seq, list):
        try: return [int(x) for x in seq]
        except Exception: pass
    return []

def _role(row: dict) -> str:
    tag = row.get("position_tag")
    if isinstance(tag, str) and tag.strip():
        return tag.strip()
    return ""

def main():
    lines: List[str] = []
    for path in sorted(glob.glob(str(SOT_DIR / "*.json")), key=lambda p: int(Path(p).stem)):
        data = _load_json(Path(path)) or {}
        league_id   = int(data.get("league_id") or Path(path).stem)
        league_name = data.get("league_name") or f"League {league_id}"
        players     = _players(data)
        if not players:
            continue

        team_name_map = _team_names_from_predicted_xi(league_id)

        # group by team
        by_team: Dict[int, List[dict]] = {}
        for r in players:
            tid = r.get("team_id")
            if isinstance(tid, int):
                by_team.setdefault(tid, []).append(r)

        lines.append(f"===== {league_name} (LID {league_id}) =====")
        def tname(tid: int) -> str:
            return team_name_map.get(tid, f"Team {tid}")

        for tid in sorted(by_team.keys(), key=lambda x: tname(x).lower()):
            lines.append(tname(tid))
            rows = sorted(by_team[tid], key=lambda r: (r.get("name") or "").lower())
            for r in rows:
                name = (r.get("name") or f"Player {r.get('player_id')}").strip()
                tag  = _role(r)
                seq  = _series(r)
                n    = len(seq)
                series_str = ",".join(str(x) for x in seq)
                if tag:
                    lines.append(f"  {name} ({tag}): {series_str}  [{n}]")
                else:
                    lines.append(f"  {name}: {series_str}  [{n}]")
            lines.append("")  # blank between teams
        lines.append("")      # blank between leagues

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with OUT_PATH.open("w", encoding="utf-8") as f:
        f.write("\n".join(lines).rstrip() + "\n")
    print(f"[OK] wrote {OUT_PATH}")

if __name__ == "__main__":
    main()
