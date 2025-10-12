#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Coverage check for SOT vs predicted XIs.

Writes:
  - data/player_sot/coverage.txt
  - data/player_sot/coverage.json
"""

import json, glob
from pathlib import Path
from typing import Dict, List, Any

ROOT = Path(".")
PX_DIR  = ROOT / "data" / "predicted_xi" / "by_league"
SOT_DIR = ROOT / "data" / "player_sot" / "by_league"
OUT_TXT = ROOT / "data" / "player_sot" / "coverage.txt"
OUT_JSON= ROOT / "data" / "player_sot" / "coverage.json"

def _load_json(p: Path):
    if not p.is_file(): return None
    with p.open("r", encoding="utf-8") as f:
        try: return json.load(f)
        except Exception: return None

def _px_players(lid: int) -> Dict[int, dict]:
    out: Dict[int, dict] = {}
    blob = _load_json(PX_DIR / f"{lid}.json") or {}
    for fx in (blob.get("fixtures") or []):
        for side in ("home","away"):
            s = fx.get(side) or {}
            tid, tname = s.get("team_id"), s.get("name")
            for p in (s.get("predicted_xi") or []):
                pid = p.get("player_id")
                if isinstance(pid, int):
                    out[pid] = {"player_id": pid, "name": p.get("name"), "team_id": tid, "team_name": tname}
    return out

def _sot_index(lid: int) -> Dict[int, dict]:
    idx: Dict[int, dict] = {}
    blob = _load_json(SOT_DIR / f"{lid}.json") or {}
    for r in (blob.get("players") or []):
        pid = r.get("player_id")
        if isinstance(pid, int):
            idx[pid] = r
    return idx

def main():
    leagues = []
    for p in glob.glob(str(SOT_DIR / "*.json")):
        try: leagues.append(int(Path(p).stem))
        except Exception: pass
    leagues = sorted(set(leagues))

    lines: List[str] = []
    out = {"by_league": {}}

    for lid in leagues:
        px = _px_players(lid)
        ix = _sot_index(lid)
        total = len(px)
        present = 0
        non_empty = 0
        missing, empty = [], []

        for pid, meta in px.items():
            row = ix.get(pid)
            if row is None:
                missing.append(meta)
                continue
            present += 1
            seq = row.get("sot_last_n") or []
            if isinstance(seq, list) and len(seq) > 0:
                non_empty += 1
            else:
                empty.append(meta)

        lines.append(f"===== League {lid} =====")
        lines.append(f"Predicted XI players: {total}")
        lines.append(f"Found in SOT file:   {present}")
        lines.append(f"Non-empty series:     {non_empty}")
        lines.append(f"Empty series:         {len(empty)}")
        lines.append(f"Missing entirely:     {len(missing)}")
        lines.append("")

        out["by_league"][str(lid)] = {
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
        f.write("\n".join(lines).rstrip() + "\n")
    with OUT_JSON.open("w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"[OK] wrote {OUT_TXT} and {OUT_JSON}")

if __name__ == "__main__":
    main()
