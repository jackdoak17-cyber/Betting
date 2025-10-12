#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Verify opponent-series coverage against predicted_xi teams.

Reads:
  - data/predicted_xi/by_league/{league_id}.json
  - data/team_opponent_stats/by_league/{league_id}.json

Writes:
  - data/team_opponent_stats/coverage.txt
  - data/team_opponent_stats/coverage.json
"""

import json, glob
from pathlib import Path
from typing import Dict, List, Any

ROOT = Path(".")
PX_DIR   = ROOT / "data" / "predicted_xi" / "by_league"
TS_DIR   = ROOT / "data" / "team_opponent_stats" / "by_league"
OUT_TXT  = ROOT / "data" / "team_opponent_stats" / "coverage.txt"
OUT_JSON = ROOT / "data" / "team_opponent_stats" / "coverage.json"

STAT_KEYS = [
    "opp_shots_total_last_n",
    "opp_shots_on_target_last_n",
    "opp_fouls_last_n",
    "opp_tackles_last_n",
    "opp_cards_total_last_n",
    "opp_saves_last_n",
    "opp_goal_kicks_last_n",
    "opp_corners_last_n",
]

def _load_json(p: Path) -> Any:
    if not p.is_file(): return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None

def _px_teams(league_id: int) -> Dict[int, str]:
    out: Dict[int, str] = {}
    blob = _load_json(PX_DIR / f"{league_id}.json") or {}
    for fx in (blob.get("fixtures") or []):
        for side in ("home", "away"):
            s = fx.get(side) or {}
            tid, name = s.get("team_id"), s.get("name")
            if isinstance(tid, int) and isinstance(name, str) and name:
                out[tid] = name
    return out

def _team_index(league_id: int) -> Dict[int, dict]:
    idx: Dict[int, dict] = {}
    blob = _load_json(TS_DIR / f"{league_id}.json") or {}
    for r in (blob.get("teams") or []):
        tid = r.get("team_id")
        if isinstance(tid, int):
            idx[tid] = r
    return idx

def _len(row: dict, key: str) -> int:
    v = row.get(key) or []
    return len(v) if isinstance(v, list) else 0

def main():
    leagues = []
    for p in PX_DIR.glob("*.json"):
        try: leagues.append(int(p.stem))
        except Exception: pass
    leagues = sorted(set(leagues))

    txt_lines: List[str] = []
    json_out: Dict[str, Any] = {"by_league": {}}

    for lid in leagues:
        teams = _px_teams(lid)
        idx   = _team_index(lid)

        total = len(teams)
        present = sum(1 for tid in teams if tid in idx)

        per_stat_non_empty = {k: 0 for k in STAT_KEYS}
        missing: List[dict] = []
        empty_by_stat: Dict[str, List[dict]] = {k: [] for k in STAT_KEYS}

        for tid, tname in teams.items():
            row = idx.get(tid)
            if row is None:
                missing.append({"team_id": tid, "team_name": tname})
                continue
            for k in STAT_KEYS:
                if _len(row, k) > 0:
                    per_stat_non_empty[k] += 1
                else:
                    empty_by_stat[k].append({"team_id": tid, "team_name": tname})

        txt_lines.append(f"===== League {lid} =====")
        txt_lines.append(f"Teams in predicted_xi      : {total}")
        txt_lines.append(f"Present in opponent_stats  : {present}")
        for k in STAT_KEYS:
            label = k.replace("_last_n", "")
            txt_lines.append(f"Non-empty {label:<24}: {per_stat_non_empty[k]}")
        if missing:
            txt_lines.append(" -- Missing teams --")
            for m in sorted(missing, key=lambda x: x["team_name"].lower()):
                txt_lines.append(f"   {m['team_name']} (TID {m['team_id']})")
        for k in STAT_KEYS:
            if empty_by_stat[k]:
                label = k.replace("_last_n", "")
                txt_lines.append(f" -- Present but empty series ({label}) --")
                for m in sorted(empty_by_stat[k], key=lambda x: x["team_name"].lower()):
                    txt_lines.append(f"   {m['team_name']} (TID {m['team_id']})")
        txt_lines.append("")

        json_out["by_league"][str(lid)] = {
            "predicted_total": total,
            "present": present,
            "per_stat_non_empty": per_stat_non_empty,
            "missing": missing,
            "empty_by_stat": empty_by_stat,
        }

    OUT_TXT.parent.mkdir(parents=True, exist_ok=True)
    OUT_TXT.write_text("\n".join(txt_lines).rstrip() + "\n", encoding="utf-8")
    OUT_JSON.write_text(json.dumps(json_out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[OK] wrote {OUT_TXT} and {OUT_JSON}")

if __name__ == "__main__":
    main()
