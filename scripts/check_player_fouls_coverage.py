#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Coverage report for fouls histories vs predicted XI targets.

Reads:
  - data/predicted_xi/by_league/{league_id}.json
  - data/player_fouls/by_league/{league_id}.json

Writes:
  - data/player_fouls/coverage.txt
  - data/player_fouls/coverage.json
"""
import json
from pathlib import Path
from typing import Dict, List, Any

PX_DIR   = Path("data/predicted_xi/by_league")
FOULS_DIR= Path("data/player_fouls/by_league")
OUT_TXT  = Path("data/player_fouls/coverage.txt")
OUT_JSON = Path("data/player_fouls/coverage.json")

def _load_json(p: Path) -> Any:
    if not p.is_file(): return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None

def _predicted_players_by_league() -> Dict[int, List[dict]]:
    out: Dict[int, List[dict]] = {}
    for p in PX_DIR.glob("*.json"):
        blob = _load_json(p) or {}
        lid = int(blob.get("league_id") or p.stem)
        rows = []
        for fx in (blob.get("fixtures") or []):
            for side in ("home", "away"):
                t = fx.get(side) or {}
                for lp in (t.get("predicted_xi") or []):
                    rows.append({
                        "league_id": lid,
                        "team_id": t.get("team_id"),
                        "team_name": t.get("name"),
                        "player_id": lp.get("player_id"),
                        "name": lp.get("name"),
                    })
        out[lid] = rows
    return out

def _fouls_by_league() -> Dict[int, Dict[int, dict]]:
    idx_by_l: Dict[int, Dict[int, dict]] = {}
    for p in FOULS_DIR.glob("*.json"):
        blob = _load_json(p) or {}
        lid = int(blob.get("league_id") or p.stem)
        idx: Dict[int, dict] = {}
        for r in (blob.get("players") or []):
            idx[int(r.get("player_id") or 0)] = r
        idx_by_l[lid] = idx
    return idx_by_l

def main():
    predicted = _predicted_players_by_league()
    fouls_idx = _fouls_by_league()

    txt_lines: List[str] = []
    json_out = {"by_league": {}}

    for lid in sorted(predicted.keys()):
        preds = predicted[lid]
        seen = fouls_idx.get(lid, {})

        total = len(preds)
        present = 0
        non_empty = 0
        missing: List[dict] = []
        empty: List[dict] = []

        for row in preds:
            pid = int(row.get("player_id") or 0)
            data = seen.get(pid)
            if data:
                present += 1
                series = data.get("fouls_last_n") or []
                if isinstance(series, list) and any(int(x or 0) > 0 for x in series):
                    non_empty += 1
                else:
                    empty.append(row)
            else:
                missing.append(row)

        txt_lines.append(f"===== League {lid} =====")
        txt_lines.append(f"Predicted XI players: {total}")
        txt_lines.append(f"Found in fouls file: {present}")
        txt_lines.append(f"Non-empty series:    {non_empty}")
        txt_lines.append(f"Empty series:        {len(empty)}")
        txt_lines.append(f"Missing entirely:    {len(missing)}")

        if missing:
            txt_lines.append(" -- Missing players --")
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
    OUT_TXT.write_text("\n".join(txt_lines).rstrip() + "\n", encoding="utf-8")
    OUT_JSON.write_text(json.dumps(json_out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[OK] wrote {OUT_TXT} and {OUT_JSON}")

if __name__ == "__main__":
    main()
