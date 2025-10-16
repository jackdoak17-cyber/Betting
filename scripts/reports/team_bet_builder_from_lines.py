#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Format team 'FINAL' expected lines into bet-builder style text.

Source: data/team_lines/by_league/*.json
  - Each fixture has teams.home/away.stats[stat].p80.min / p100.min

Output:
  - Sections grouped by League ID, then fixtures, for 80% and/or 100%.
  - Lines like:  Team — SOT 3+, Corners 2+, Shots 10+, Cards 1+

ENV:
  STATS         (default: "shots_on_target,corners,shots,cards_total")
  MODE          (default: "both")  # both | p80 | p100
  INCLUDE_ZERO  (default: "0")      # "1" to allow 0+ lines; default hides 0
  OUT_FILE      (default: "data/bet_builders/team_bet_builders.txt")
"""
import os, json, re, datetime as dt
from pathlib import Path
from typing import Dict, Any, List, Tuple, Optional

ROOT = Path(".")
LINES_DIR = ROOT / "data" / "team_lines" / "by_league"

# ---- Config from ENV
STATS = [s.strip().lower() for s in os.getenv(
    "STATS", "shots_on_target,corners,shots,cards_total"
).split(",") if s.strip()]
MODE = os.getenv("MODE", "both").strip().lower()  # both | p80 | p100
INCLUDE_ZERO = os.getenv("INCLUDE_ZERO", "0") == "1"
OUT_FILE = os.getenv("OUT_FILE", "data/bet_builders/team_bet_builders.txt")

# ---- Human labels & order
LABELS = {
    "shots": "Shots",
    "shots_on_target": "SOT",
    "corners": "Corners",
    "cards_total": "Cards",
    "fouls": "Fouls",
    "tackles": "Tackles",
    "saves": "Saves",
    "goal_kicks": "Goal Kicks",
}
ORDER = ["shots_on_target", "corners", "shots", "cards_total", "tackles", "fouls", "saves", "goal_kicks"]

def safe_int(x) -> Optional[int]:
    try: return int(x)
    except Exception: return None

def parse_dt(s: Optional[str]) -> Optional[dt.datetime]:
    if not s: return None
    try:
        # Accept "YYYY-MM-DD HH:MM:SS"
        return dt.datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
    except Exception:
        return None

def pick_team_name(side_block: Dict[str, Any], meta: Dict[str, Any], side: str) -> str:
    nm = side_block.get("name") or side_block.get("team_name")
    if nm: return nm
    return meta.get("home_name") if side == "home" else meta.get("away_name")

def collect_fixture_rows(blob: Dict[str, Any]) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for fx in (blob.get("fixtures") or []):
        meta = {
            "fixture_id": fx.get("fixture_id"),
            "league_id": fx.get("league_id") or blob.get("league_id"),
            "season_id": fx.get("season_id") or blob.get("season_id"),
            "starting_at": fx.get("starting_at"),
            "home_name": fx.get("home_name"),
            "away_name": fx.get("away_name"),
            "use_last_n": fx.get("use_last_n") or blob.get("use_last_n"),
        }
        teams = fx.get("teams") or {}
        for side in ("home", "away"):
            tb = teams.get(side) or {}
            name = pick_team_name(tb, meta, side) or (meta["home_name"] if side == "home" else meta["away_name"])
            stats = tb.get("stats") or {}
            rows.append({
                "meta": meta,
                "side": side,
                "team": name,
                "opp": (meta["away_name"] if side == "home" else meta["home_name"]),
                "stats": stats
            })
    return rows

def format_team_line(stats_block: Dict[str, Any], pct_key: str, chosen_stats: List[str], include_zero: bool) -> str:
    parts: List[str] = []
    for k in sorted(chosen_stats, key=lambda s: ORDER.index(s) if s in ORDER else 999):
        sb = stats_block.get(k) or {}
        pk = sb.get(pct_key) or {}
        mn = safe_int(pk.get("min"))
        if mn is None: 
            continue
        if not include_zero and mn <= 0:
            continue
        lab = LABELS.get(k, k)
        parts.append(f"{lab} {mn}+")
    return ", ".join(parts)

def main():
    league_files = sorted(LINES_DIR.glob("*.json"))
    if not league_files:
        print("No team_lines league files found.")
        return

    all_rows: List[Dict[str, Any]] = []
    for p in league_files:
        try:
            blob = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        all_rows.extend(collect_fixture_rows(blob))

    if not all_rows:
        print("No fixtures found in team_lines files.")
        return

    # Group by league -> fixture
    leagues: Dict[int, Dict[Tuple[int, str, str, int], Dict[str, Any]]] = {}
    for r in all_rows:
        meta = r["meta"]
        lid = int(meta.get("league_id") or -1)
        fid = meta.get("fixture_id")
        key = (int(fid) if isinstance(fid, int) else -1,
               meta.get("home_name") or "",
               meta.get("away_name") or "",
               int(meta.get("use_last_n") or 0))
        grp = leagues.setdefault(lid, {}).setdefault(key, {"meta": meta, "teams": []})
        grp["teams"].append(r)

    lines: List[str] = []
    lines.append("Legend: X+ means the team’s minimum expected line at the selected certainty (80% or 100%) using the blend of Team-Offense and Opp-Allowed (we take the MIN bound).")
    lines.append("Stats shown (in order): " + ", ".join(LABELS.get(s, s) for s in STATS))
    lines.append("")

    def dump_section(pct_key: str, title: str):
        lines.append(f"===== {title} =====")
        # sort leagues numerically; then fixtures by kickoff (if available), else by home/away
        for lid in sorted(leagues.keys()):
            lines.append(f"--- League {lid} ---")
            fixmap = leagues[lid]
            # sort fixtures by starting_at (asc), fallback to home/away alphabetical
            def sort_key(item):
                (_, home, away, _useN), grp = item
                dt_obj = parse_dt(grp["meta"].get("starting_at"))
                return (dt_obj or dt.datetime.max, home.lower(), away.lower())
            for (fid, home, away, useN), grp in sorted(fixmap.items(), key=sort_key):
                suffix = f"  (using last {useN})" if useN else ""
                hdr = f"{home} vs {away}" + (f"  [Fixture {fid}]" if fid != -1 else "")
                lines.append(hdr + suffix)
                # ensure home printed first
                team_rows = sorted(grp["teams"], key=lambda r: 0 if r["side"]=="home" else 1)
                for tr in team_rows:
                    items = format_team_line(tr["stats"], pct_key, STATS, INCLUDE_ZERO)
                    if items:
                        lines.append(f"  - {tr['team']}: {items}")
                    else:
                        lines.append(f"  - {tr['team']}: (no qualifying lines with current settings)")
                lines.append("")
            lines.append("")
        lines.append("")

    if MODE in ("both", "p80"):
        dump_section("p80", "Bet Builder — 80%")
    if MODE in ("both", "p100"):
        dump_section("p100", "Bet Builder — 100%")

    out = "\n".join(lines).rstrip() + "\n"
    print(out)

    # Write file (and ensure folder exists)
    out_path = ROOT / OUT_FILE
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(out, encoding="utf-8")

if __name__ == "__main__":
    main()