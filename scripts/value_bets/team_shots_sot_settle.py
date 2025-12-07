#!/usr/bin/env python3
"""Settle Team Shots & SOT bet sheets for V2–V4 using recorded fixture stats.

This script reloads each version's bet sheet, fills in results/actuals when the
fixture stats are available, and re-saves the CSV. Use alongside the generator
runner so selections and settlement stay in sync.

Cron example (UTC):
  15 1,16 * * * /usr/bin/python /workspace/Betting/scripts/value_bets/team_shots_sot_settle.py >> /workspace/Betting/logs/team_shots_settle.log 2>&1
"""

import csv
import json
import math
from pathlib import Path
from typing import Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[2]
TS_DIR = ROOT / "data" / "team_stats" / "by_league"
SHEET_DIR = ROOT / "data" / "value_bets" / "sheets"
SHEETS = {
    "V2": SHEET_DIR / "team_shots_sot_v2_bets.csv",
    "V3": SHEET_DIR / "team_shots_sot_v3_bets.csv",
    "V4": SHEET_DIR / "team_shots_sot_v4_bets.csv",
}

LEGACY_SHEETS = {
    "V2": ROOT / "data" / "value_bets" / "team_shots_sot_v2_bets.csv",
    "V3": ROOT / "data" / "value_bets" / "team_shots_sot_v3_bets.csv",
    "V4": ROOT / "data" / "value_bets" / "team_shots_sot_v4_bets.csv",
}


# ---------- IO helpers ----------


def load_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def bet_key(row: dict) -> Tuple[str, str, str, str, str, str]:
    """Dedupe using fixture/team details even when IDs are missing."""

    fixture_part = str(row.get("fixture_id") or row.get("fixture") or "").strip()
    kickoff_part = str(row.get("kickoff") or "").strip()
    team_part = str(row.get("team_id") or row.get("team") or "").strip()
    stat_part = str(row.get("stat") or "").strip()
    line_part = str(row.get("line") or "").strip()
    pick_part = (row.get("pick") or "").lower().strip()
    return (fixture_part, kickoff_part, team_part, stat_part, line_part, pick_part)


def load_sheet(path: Path, legacy: Optional[Path] = None) -> List[dict]:
    rows: List[dict] = []
    seen: set = set()

    for candidate in [p for p in (path, legacy) if p is not None]:
        if not candidate.exists():
            continue
        with candidate.open("r", encoding="utf-8", newline="") as f:
            for row in csv.DictReader(f):
                key = bet_key(row)
                if key in seen:
                    continue
                seen.add(key)
                rows.append(row)
    return rows


def save_sheet(path: Path, rows: List[dict]) -> None:
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = list(rows[0].keys())
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


# ---------- Stat helpers ----------


def threshold_from_line(line: float) -> int:
    frac = line - math.floor(line)
    if frac > 1e-6:
        return math.floor(line) + 1
    return int(math.floor(line))


def fixture_stat_value(team_rec: dict, stat: str, fixture_id: int, expected_loc: Optional[str]) -> Optional[int]:
    fids = team_rec.get("fixture_ids") or []
    locs = team_rec.get("locations_last_n") or []
    if stat == "shots_total":
        vals = team_rec.get("shots_total_last_n") or []
    else:
        vals = team_rec.get("shots_on_target_last_n") or []
    for fid, loc, val in zip(fids, locs, vals):
        try:
            if int(fid) != int(fixture_id):
                continue
        except Exception:
            continue
        if expected_loc and loc and loc != expected_loc:
            continue
        if isinstance(val, int):
            return val
    return None


def settle_row(row: dict, ts_index: Dict[int, Dict[int, dict]]) -> str:
    """Try to settle a row. Returns a reason string for diagnostics."""

    try:
        league_id = int(row.get("league_id"))
        team_id = int(row.get("team_id"))
        fixture_id = int(row.get("fixture_id"))
    except Exception:
        return "missing ids"

    stat = row.get("stat") or ""
    pick = (row.get("pick") or "").lower()
    if pick not in {"over", "under"}:
        return "invalid pick"

    ts_by_id = ts_index.get(league_id)
    if not ts_by_id:
        return "league stats missing"
    team_rec = ts_by_id.get(team_id)
    if not team_rec:
        return "team stats missing"

    loc = row.get("venue") if row.get("venue") in {"home", "away"} else None
    val = fixture_stat_value(team_rec, stat, fixture_id, loc)
    if val is None:
        return "fixture stat missing"

    try:
        threshold = threshold_from_line(float(row.get("line")))
    except Exception:
        return "invalid line"

    outcome = None
    line_float = None
    try:
        line_float = float(row.get("line"))
    except Exception:
        pass
    if line_float is not None and line_float.is_integer() and val == int(line_float):
        outcome = "push"
    elif pick == "over":
        outcome = "won" if val >= threshold else "lost"
    else:
        outcome = "won" if val < threshold else "lost"

    row["result"] = outcome
    row["actual"] = str(val)
    row["settled_at"] = row.get("settled_at") or row.get("created_at") or ""
    try:
        stake = float(row.get("stake", 1) or 1)
    except Exception:
        stake = 1.0
    try:
        price = float(row.get("price"))
    except Exception:
        price = None
    profit = None
    if outcome == "won" and price:
        profit = (price - 1.0) * stake
    elif outcome == "lost":
        profit = -stake
    elif outcome == "push":
        profit = 0.0
    if profit is not None:
        row["profit"] = f"{profit:.2f}"
    elif "profit" not in row:
        row["profit"] = ""

    return "settled"


# ---------- Main ----------


def build_ts_index() -> Dict[int, Dict[int, dict]]:
    idx: Dict[int, Dict[int, dict]] = {}
    for path in TS_DIR.glob("*.json"):
        if not path.stem.isdigit():
            continue
        lid = int(path.stem)
        blob = load_json(path)
        teams = blob.get("teams") or []
        idx[lid] = {
            t.get("team_id"): t
            for t in teams
            if isinstance(t, dict) and isinstance(t.get("team_id"), int)
        }
    return idx


def settle_sheet(path: Path, ts_index: Dict[int, Dict[int, dict]], legacy: Optional[Path] = None) -> Tuple[int, int, Dict[str, int]]:
    rows = load_sheet(path, legacy)
    if not rows:
        return 0, 0, {}
    settled = 0
    pending = 0
    skipped: Dict[str, int] = {}
    for row in rows:
        row.setdefault("stake", "1")
        row.setdefault("profit", "")
        if (row.get("result") or "").lower() in {"won", "lost", "push"}:
            continue
        pending += 1
        reason = settle_row(row, ts_index)
        if reason != "settled":
            skipped[reason] = skipped.get(reason, 0) + 1
        if (row.get("result") or "").lower() in {"won", "lost", "push"}:
            settled += 1
    save_sheet(path, rows)
    return settled, pending, skipped


def main() -> None:
    ts_index = build_ts_index()
    for label, path in SHEETS.items():
        settled, pending, skipped = settle_sheet(path, ts_index, LEGACY_SHEETS.get(label))
        print(f"{label}: settled {settled} of {pending} pending selections in {path}")
        for reason, count in skipped.items():
            print(f"  - skipped {count} pending rows ({reason})")


if __name__ == "__main__":
    main()
