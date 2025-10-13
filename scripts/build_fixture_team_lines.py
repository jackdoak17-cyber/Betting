#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Build per-fixture expected 'hit lines' for team stats using team offense
and opponent-allowed (opponent series) with conservative min/max rules.

Clarity goal
------------
For every fixture and team, we print lines like:
  SOT: 80% Team-Offense=2 vs Opp-Allowed=1  →  FINAL 80%: 1–2 (min from Opp-Allowed, max from Team-Offense)
       100% Team-Offense=1 vs Opp-Allowed=0 →  FINAL 100%: 0–1 (min from Opp-Allowed, max from Team-Offense)

So it's obvious the final range blends the team's own series and what the opponent typically concedes.

Definitions
-----------
For a series of non-negative integers S (latest -> older) and a percentage p:
- threshold_at_pct(S, p) = largest integer t such that
    coverage(S, t) = fraction of games with value >= t  >= p.
  Example: S = [4,2,3,2,1], p = 0.8 -> t = 2 (since 4/5 >= 0.8, but 3 has 2/5).

For each fixture (Home vs Away) and each stat:
- team_off_80  = threshold_at_pct(team_offense_series_lastN, 0.80)
- team_off_100 = threshold_at_pct(team_offense_series_lastN, 1.00)
- opp_allw_80  = threshold_at_pct(opponent_allowed_series_lastN, 0.80)
- opp_allw_100 = threshold_at_pct(opponent_allowed_series_lastN, 1.00)

We report per team per fixture:
- 80%:  FINAL range = [min(team_off_80, opp_allw_80), max(team_off_80, opp_allw_80)]
- 100%: FINAL range = [min(team_off_100, opp_allw_100), max(team_off_100, opp_allw_100)]
And we explicitly indicate whether the min/max came from Team-Offense or Opp-Allowed.

Inputs
------
- data/predicted_xi/by_league/{league_id}.json              (fixtures with team ids/names)
- data/team_stats/by_league/{league_id}.json                (team offense series)
- data/team_opponent_stats/by_league/{league_id}.json       (opponent-allowed series)

Outputs
-------
- data/team_lines/by_league/{league_id}.json
- data/team_lines/lines.txt         (HUMAN-READABLE with explicit min/max sources)
- data/team_lines/summary.txt

Env
---
TEAM_LINES_USE_LAST_N (int, default 10): trim series to last N items (or fewer if unavailable).
"""

from __future__ import annotations
import os, json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from datetime import datetime

ROOT = Path(".")
PX_DIR   = ROOT / "data" / "predicted_xi" / "by_league"
TS_DIR   = ROOT / "data" / "team_stats" / "by_league"
OPS_DIR  = ROOT / "data" / "team_opponent_stats" / "by_league"
OUT_DIR  = ROOT / "data" / "team_lines"
OL_DIR   = OUT_DIR / "by_league"
OL_DIR.mkdir(parents=True, exist_ok=True)

# Use last N from each series (or fewer if not available)
USE_LAST_N = int(os.getenv("TEAM_LINES_USE_LAST_N", "10"))

# Stat map: offense key, opponent-allowed key, short label
STAT_MAP = {
    "shots":          ("shots_total_last_n",       "opp_shots_total_last_n",       "Shots"),
    "shots_on_target":("shots_on_target_last_n",   "opp_shots_on_target_last_n",   "SOT"),
    "fouls":          ("fouls_last_n",             "opp_fouls_last_n",             "Fouls"),
    "tackles":        ("tackles_last_n",           "opp_tackles_last_n",           "Tackles"),
    "cards_total":    ("cards_total_last_n",       "opp_cards_total_last_n",       "Cards"),
    "saves":          ("saves_last_n",             "opp_saves_last_n",             "Saves"),
    "goal_kicks":     ("goal_kicks_last_n",        "opp_goal_kicks_last_n",        "GoalKicks"),
    "corners":        ("corners_last_n",           "opp_corners_last_n",           "Corners"),
}

# ---------- IO helpers ----------
def _load_json(p: Path) -> Any:
    if not p.is_file(): return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None

def _series(row: dict, key: str) -> List[int]:
    arr = row.get(key) or []
    if not isinstance(arr, list):
        return []
    out: List[int] = []
    for x in arr[:USE_LAST_N]:
        try: out.append(int(x))
        except Exception:
            try: out.append(int(float(x)))
            except Exception: out.append(0)
    return out

def _team_index(blob: dict) -> Dict[int, dict]:
    """Build team_id -> row dict for quick lookup."""
    out: Dict[int, dict] = {}
    for r in (blob.get("teams") or []):
        tid = r.get("team_id")
        if isinstance(tid, int):
            out[tid] = r
    return out

def _fixtures_from_px(lid: int) -> List[dict]:
    px = _load_json(PX_DIR / f"{lid}.json") or {}
    fx_list = []
    for fx in (px.get("fixtures") or []):
        row = {
            "fixture_id": fx.get("fixture_id") or fx.get("id"),
            "season_id": fx.get("season_id"),
            "home_id": (fx.get("home") or {}).get("team_id"),
            "home_name": (fx.get("home") or {}).get("name"),
            "away_id": (fx.get("away") or {}).get("team_id"),
            "away_name": (fx.get("away") or {}).get("name"),
            "starting_at": (fx.get("time") or {}).get("starting_at") or fx.get("starting_at"),
        }
        if isinstance(row["home_id"], int) and isinstance(row["away_id"], int):
            fx_list.append(row)
    return fx_list

# ---------- math ----------
def _coverage(seq: List[int], threshold: int) -> float:
    if not seq: return 0.0
    hits = sum(1 for v in seq if v >= threshold)
    return hits / len(seq)

def threshold_at_pct(seq: List[int], pct: float) -> int:
    """Return largest integer t such that coverage(seq, t) >= pct. Empty seq -> 0."""
    if not seq:
        return 0
    hi = max(seq)
    best = 0
    for t in range(0, hi + 1):
        if _coverage(seq, t) >= pct:
            best = t
    return best

def _minmax_with_sources(off: int, opp: int) -> Dict[str, Any]:
    """
    Return min/max and which side supplied them. If equal, mark as 'both'.
    """
    if off < opp:
        return {"min": off, "min_src": "Team-Offense", "max": opp, "max_src": "Opp-Allowed"}
    if opp < off:
        return {"min": opp, "min_src": "Opp-Allowed", "max": off, "max_src": "Team-Offense"}
    # equal
    return {"min": off, "min_src": "both", "max": off, "max_src": "both"}

def _pair_lines(off_seq: List[int], allw_seq: List[int]) -> Dict[str, Dict[str, Any]]:
    """
    For p in {0.8, 1.0} compute team/offense and opponent/allowed thresholds, then final min/max with sources.
    Returns:
      {
        "p80":  {"off": int, "opp": int, **minmax_with_sources},
        "p100": {...}
      }
    """
    off80  = threshold_at_pct(off_seq, 0.80)
    opp80  = threshold_at_pct(allw_seq, 0.80)
    off100 = threshold_at_pct(off_seq, 1.00)
    opp100 = threshold_at_pct(allw_seq, 1.00)
    res80  = _minmax_with_sources(off80,  opp80)
    res100 = _minmax_with_sources(off100, opp100)
    return {
        "p80":  {"off": off80,  "opp": opp80,  **res80},
        "p100": {"off": off100, "opp": opp100, **res100},
    }

# ---------- core ----------
def build_for_league(lid: int) -> Optional[dict]:
    ts = _load_json(TS_DIR  / f"{lid}.json") or {}
    ops = _load_json(OPS_DIR / f"{lid}.json") or {}
    if not (_load_json(PX_DIR / f"{lid}.json") or {}).get("fixtures"):
        print(f"[warn] No fixtures in predicted_xi/by_league/{lid}.json")
        return None

    ts_idx  = _team_index(ts)
    ops_idx = _team_index(ops)

    out_fixtures: List[dict] = []
    missing_off = set()
    missing_opp = set()

    for fx in _fixtures_from_px(lid):
        hid, aid = fx["home_id"], fx["away_id"]

        def side(team_id: int, opp_id: int) -> Dict[str, Any]:
            tres = ts_idx.get(team_id)
            ores = ops_idx.get(opp_id)   # opponent-allowed keyed by the *opponent* team id
            side_obj: Dict[str, Any] = {"team_id": team_id, "opponent_id": opp_id, "stats": {}}

            for key, (off_key, opp_key, label) in STAT_MAP.items():
                off_seq = _series(tres or {}, off_key) if tres else []
                allw_seq = _series(ores or {}, opp_key) if ores else []
                if not tres: missing_off.add(team_id)
                if not ores: missing_opp.add(opp_id)
                lines = _pair_lines(off_seq, allw_seq)
                side_obj["stats"][key] = {
                    "label": label,
                    "series_used": {
                        "offense_lastN": off_seq,
                        "opponent_allowed_lastN": allw_seq
                    },
                    "p80":  lines["p80"],
                    "p100": lines["p100"],
                }
            return side_obj

        home_block = side(hid, aid)
        away_block = side(aid, hid)

        out_fixtures.append({
            "fixture_id": fx["fixture_id"],
            "league_id": lid,
            "season_id": fx.get("season_id"),
            "home_id": hid,
            "home_name": fx.get("home_name"),
            "away_id": aid,
            "away_name": fx.get("away_name"),
            "starting_at": fx.get("starting_at"),
            "use_last_n": USE_LAST_N,
            "teams": { "home": home_block, "away": away_block }
        })

    payload = {
        "generated_at": datetime.utcnow().isoformat(),
        "league_id": lid,
        "use_last_n": USE_LAST_N,
        "fixtures": out_fixtures,
        "missing": {
            "team_offense_missing_for_team_ids": sorted(missing_off),
            "opponent_allowed_missing_for_team_ids": sorted(missing_opp),
        }
    }
    return payload

def write_outputs(all_payloads: List[dict]) -> None:
    # per-league JSON
    for pl in all_payloads:
        lid = pl["league_id"]
        outp = OL_DIR / f"{lid}.json"
        outp.parent.mkdir(parents=True, exist_ok=True)
        tmp = outp.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(pl, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(outp)

    # human-readable TXT with explicit sources for min/max
    lines: List[str] = []
    lines.append(f"Generated at (UTC): {datetime.utcnow().isoformat()}")
    lines.append(f"USE_LAST_N = {USE_LAST_N}")
    lines.append("Legend: Team-Offense = team’s own series; Opp-Allowed = opponent’s opponents series (what they concede).")
    lines.append("FINAL ranges blend both: min/max show which source supplied that bound.\n")

    for pl in all_payloads:
        lid = pl["league_id"]
        lines.append(f"===== League {lid} =====")
        for fx in pl["fixtures"]:
            hn = fx.get("home_name") or f"H{fx['home_id']}"
            an = fx.get("away_name") or f"A{fx['away_id']}"
            fid = fx.get("fixture_id")
            lines.append(f"{hn} vs {an}  [Fixture {fid}]  (using last {fx.get('use_last_n', USE_LAST_N)})")

            for side_name in ("home", "away"):
                side = fx["teams"][side_name]
                tname = hn if side_name == "home" else an
                lines.append(f"  {tname} — FINAL expected lines (blend of Team-Offense and {an if side_name=='home' else hn} Opp-Allowed):")
                for key, (_, _, label) in STAT_MAP.items():
                    st = side["stats"][key]
                    p80, p100 = st["p80"], st["p100"]

                    # Build explicit source notes
                    def src_note(p):
                        if p["min_src"] == "both" and p["max_src"] == "both":
                            return "(both sides equal)"
                        min_note = "min from {}".format(p["min_src"])
                        max_note = "max from {}".format(p["max_src"])
                        return f"({min_note}, {max_note})"

                    lines.append(
                        f"    - {label}: "
                        f"80% Team-Offense={p80['off']} vs Opp-Allowed={p80['opp']} → FINAL 80%: {p80['min']}–{p80['max']} {src_note(p80)} | "
                        f"100% Team-Offense={p100['off']} vs Opp-Allowed={p100['opp']} → FINAL 100%: {p100['min']}–{p100['max']} {src_note(p100)}"
                    )
            lines.append("")  # blank after fixture
        lines.append("")      # blank after league

    out_txt = OUT_DIR / "lines.txt"
    out_txt.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")

    # tiny summary
    total_fx = sum(len(pl["fixtures"]) for pl in all_payloads)
    sum_lines = [
        f"Generated at (UTC): {datetime.utcnow().isoformat()}",
        f"Leagues: {len(all_payloads)}",
        f"Fixtures: {total_fx}",
        ""
    ]
    (OUT_DIR / "summary.txt").write_text("\n".join(sum_lines), encoding="utf-8")
    print(f"[OK] wrote {out_txt} and {OUT_DIR/'summary.txt'}")

def main():
    leagues = []
    for p in PX_DIR.glob("*.json"):
        try: leagues.append(int(p.stem))
        except Exception: pass
    leagues = sorted(leagues)

    all_payloads: List[dict] = []
    for lid in leagues:
        pl = build_for_league(lid)
        if pl:
            all_payloads.append(pl)

    if not all_payloads:
        print("[warn] no payloads built (no fixtures?)")
        return
    write_outputs(all_payloads)

if __name__ == "__main__":
    main()
