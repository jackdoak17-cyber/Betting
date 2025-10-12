#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Render a team-grouped summary for shots on target (SOT).

Reads:
  - data/player_sot/by_league/{league_id}.json   (expects 'sot_last_n')
  - data/predicted_xi/by_league/{league_id}.json (for team names)

Writes:
  - data/player_sot/summary_by_team.txt
"""

import json, glob
from pathlib import Path
from typing import Dict, List, Any

ROOT = Path(".")
SOT_DIR = ROOT / "data" / "player_sot" / "by_league"
PX_DIR  = ROOT / "data" / "predicted_xi" / "by_league"
OUT     = ROOT / "data" / "player_sot" / "summary_by_team.txt"

def _load_json(p: Path) -> Any:
    if not p.is_file(): return None
    with p.open("r", encoding="utf-8") as f:
        try: return json.load(f)
        except Exception: return None

def _team_names(lid: int) -> Dict[int, str]:
    m: Dict[int, str] = {}
    blob = _load_json(PX_DIR / f"{lid}.json") or {}
    for fx in (blob.get("fixtures") or []):
        for side in ("home", "away"):
            s = fx.get(side) or {}
            tid, nm = s.get("team_id"), s.get("name")
            if isinstance(tid, int) and isinstance(nm, str) and nm:
                m.setdefault(tid, nm)
    return m

def main():
    lines: List[str] = []
    for path in sorted(glob.glob(str(SOT_DIR / "*.json")), key=lambda p: int(Path(p).stem)):
        data = _load_json(Path(path)) or {}
        lid  = int(data.get("league_id") or Path(path).stem)
        lname = data.get("league_name") or f"League {lid}"
        players = [x for x in (data.get("players") or []) if isinstance(x, dict)]
        if not players: 
            continue

        tmap = _team_names(lid)

        # group by team
        by_team: Dict[int, List[dict]] = {}
        for r in players:
            tid = r.get("team_id")
            if isinstance(tid, int):
                by_team.setdefault(tid, []).append(r)

        lines.append(f"===== {lname} (LID {lid}) =====")
        def tname(tid: int) -> str: return tmap.get(tid, f"Team {tid}")

        for tid in sorted(by_team.keys(), key=lambda x: tname(x).lower()):
            lines.append(tname(tid))
            for r in sorted(by_team[tid], key=lambda r: (r.get("name") or "").lower()):
                nm  = r.get("name") or f"PID {r.get('player_id')}"
                tag = r.get("position_tag") or ""
                seq = r.get("sot_last_n") or []
                series_str = ",".join(str(int(x)) for x in seq)
                n = len(seq)
                if tag:
                    lines.append(f"  {nm} ({tag}): {series_str}  [{n}]")
                else:
                    lines.append(f"  {nm}: {series_str}  [{n}]")
            lines.append("")
        lines.append("")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("w", encoding="utf-8") as f:
        f.write("\n".join(lines).rstrip() + "\n")
    print(f"[OK] wrote {OUT}")

if __name__ == "__main__":
    main()
