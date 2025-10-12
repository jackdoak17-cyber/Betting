#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Verify coverage: for each league, every predicted-XI player should appear
in data/player_shots/by_league/{lid}.json and ideally have a non-empty shots series.

Writes:
- data/player_shots/coverage.txt
- data/player_shots/coverage.json
"""

import json, glob
from pathlib import Path
from typing import Dict, List, Any, Tuple

ROOT = Path(".")
PX_DIR    = ROOT / "data" / "predicted_xi" / "by_league"
SHOTS_DIR = ROOT / "data" / "player_shots" / "by_league"
OUT_TXT   = ROOT / "data" / "player_shots" / "coverage.txt"
OUT_JSON  = ROOT / "data" / "player_shots" / "coverage.json"

def _load_json(p: Path) -> Any:
    if not p.is_file(): return None
    with p.open("r", encoding="utf-8") as f:
        try: return json.load(f)
        except Exception: return None

def _px_players(league_id: int) -> Dict[int, Dict[str, Any]]:
    """player_id -> {name, team_id, team_name} from predicted_xi."""
    out: Dict[int, Dict[str, Any]] = {}
    blob = _load_json(PX_DIR / f"{league_id}.json") or {}
    for fx in (blob.get("fixtures") or []):
        for side in ("home", "away"):
            s = fx.get(side) or {}
            tid, tname = s.get("team_id"), s.get("name")
            for p in (s.get("predicted_xi") or []):
                pid = p.get("player_id")
                if isinstance(pid, int):
                    out[pid] = {
                        "player_id": pid,
                        "name": p.get("name"),
                        "team_id": tid,
                        "team_name": tname,
                    }
    return out

def _shots_index(league_id: int) -> Dict[int, Dict[str, Any]]:
    """player_id -> row from shots file."""
    idx: Dict[int, Dict[str, Any]] = {}
    blob = _load_json(SHOTS_DIR / f"{league_id}.json") or {}
    for r in (blob.get("players") or []):
        pid = r.get("player_id")
        if isinstance(pid, int):
            idx[pid] = r
    return idx

def _shots_series(row: dict) -> List[int]:
    seq = row.get("shots_last_n") or []
    if isinstance(seq, list):
        try: return [int(x) for x in seq]
        except Exception: pass
    return []

def main():
    leagues = []
    for p in glob.glob(str(SHOTS_DIR / "*.json")):
        try: leagues.append(int(Path(p).stem))
        except Exception: pass
    leagues = sorted(set(leagues))

    txt_lines: List[str] = []
    json_out: Dict[str, Any] = {"by_league": {}}

    for lid in leagues:
        px = _px_players(lid)
        ix = _shots_index(lid)
        total = len(px)
        present = 0
        non_empty = 0
        missing: List[Dict[str, Any]] = []
        empty:   List[Dict[str, Any]] = []

        for pid, meta in px.items():
            row = ix.get(pid)
            if row is None:
                missing.append(meta)
                continue
            present += 1
            seq = _shots_series(row)
            if len(seq) > 0:
                non_empty += 1
            else:
                empty.append(meta)

        txt_lines.append(f"===== League {lid} =====")
        txt_lines.append(f"Predicted XI players: {total}")
        txt_lines.append(f"Found in shots file: {present}")
        txt_lines.append(f"Non-empty series:    {non_empty}")
        txt_lines.append(f"Empty series:        {len(empty)}")
        txt_lines.append(f"Missing entirely:    {len(missing)}")

        if missing:
            txt_lines.append(" -- Missing players --")
            # group by team for readability
            by_team: Dict[str, List[str]] = {}
            for m in missing:
                key = f"{m.get('team_name')} (TID {m.get('team_id')})"
                by_team.setdefault(key, []).append(m.get("name") or f"PID {m.get('player_id')}")
            for k in sorted(by_team.keys()):
                txt_lines.append(f"   {k}: " + ", ".join(sorted(by_team[k])))

        if empty:
            txt_lines.append(" -- Present but empty series --")
            by_team: Dict[str, List[str]] = {}
            for m in empty:
                key = f"{m.get('team_name')} (TID {m.get('team_id')})"
                by_team.setdefault(key, []).append(m.get("name") or f"PID {m.get('player_id')}")
            for k in sorted(by_team.keys()):
                txt_lines.append(f"   {k}: " + ", ".join(sorted(by_team[k])))
        txt_lines.append("")

        json_out["by_league"][str(lid)] = {
            "predicted_total": total,
            "found": present,
            "non_empty": non_empty,
            "empty": len(empty),
            "missing": len(missing),
            "missing_list": missing,
            "empty_list": empty,
        }

    OUT_TXT.parent.mkdir(parents=True, exist_ok=True)
    with OUT_TXT.open("w", encoding="utf-8") as f:
        f.write("\n".join(txt_lines).rstrip() + "\n")
    with OUT_JSON.open("w", encoding="utf-8") as f:
        json.dump(json_out, f, ensure_ascii=False, indent=2)
    print(f"[OK] wrote {OUT_TXT} and {OUT_JSON}")

if __name__ == "__main__":
    main()
