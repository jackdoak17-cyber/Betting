#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
High-probability goals shortlist (Over 2.5 + BTTS + Single-team O1.5 ≥90%)
Reads ONLY local JSONs:

  - data/fixtures/by_league/{league_id}.json
  - data/team_stats/by_league/{league_id}.json          (needs: goals_last_n, fixture_ids)
  - data/team_opponent_stats/by_league/{league_id}.json (needs: opp_goals_last_n, fixture_ids)

Sections:
  1) Combined Over 2.5 — BOTH teams ≥ THRESHOLD (default 70%)
  2) BTTS               — BOTH teams ≥ THRESHOLD (default 70%)
  3) Single-team O1.5   — ANY team ≥ SINGLE_TEAM_O15_THRESHOLD (default 90%)

Env (optional):
  OUTPUT_PATH               (default: posts/over25_matches.md)
  LAST_N                    (default: 10)
  MIN_SAMPLE                (default: 6)
  THRESHOLD                 (default: 70)   # gate for (1) and (2)
  SINGLE_TEAM_O15_THRESHOLD (default: 90)   # gate for (3)
"""

import os
import json
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# ---------------- Config ----------------
OUTPUT_PATH = Path(os.getenv("OUTPUT_PATH", "posts/over25_matches.md"))
LAST_N      = int(os.getenv("LAST_N", "10"))
MIN_SAMPLE  = int(os.getenv("MIN_SAMPLE", "6"))
THRESHOLD   = float(os.getenv("THRESHOLD", "70"))
SINGLE_TEAM_O15_THRESHOLD = float(os.getenv("SINGLE_TEAM_O15_THRESHOLD", "90"))

FIX_DIR     = Path("data/fixtures/by_league")
TEAM_DIR    = Path("data/team_stats/by_league")
OPP_DIR     = Path("data/team_opponent_stats/by_league")

# ---------------- IO helpers ----------------
def _load_json(p: Path) -> Optional[dict]:
    try:
        if not p.exists():
            return None
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None

def _index_team_rows(blob: dict) -> Dict[int, dict]:
    out: Dict[int, dict] = {}
    for r in (blob or {}).get("teams", []) or []:
        tid = r.get("team_id")
        if isinstance(tid, int):
            out[tid] = r
    return out

def load_upcoming_fixtures() -> List[dict]:
    rows: List[dict] = []
    if not FIX_DIR.exists():
        return rows
    for f in sorted(FIX_DIR.glob("*.json")):
        blob = _load_json(f)
        if not blob:
            continue
        for fx in (blob.get("fixtures") or []):
            if fx and fx.get("participants"):
                rows.append(fx)
    return rows

def pick_home_away(parts: List[dict]) -> Tuple[Optional[dict], Optional[dict]]:
    home, away = None, None
    for p in parts or []:
        loc = ((p.get("meta") or {}).get("location") or "").lower()
        if loc == "home": home = p
        elif loc == "away": away = p
    if not home and len(parts or []) >= 1: home = parts[0]
    if not away and len(parts or []) >= 2: away = parts[1]
    return home, away

# ---------------- Caches ----------------
_LEAGUE_CACHE: Dict[int, Tuple[Dict[int, dict], Dict[int, dict]]] = {}
_RATE_CACHE_O25: Dict[Tuple[int,int], Tuple[Optional[float], int]] = {}
_RATE_CACHE_BTTS: Dict[Tuple[int,int], Tuple[Optional[float], int]] = {}
_RATE_CACHE_O15F: Dict[Tuple[int,int], Tuple[Optional[float], int]] = {}

def _league_rows(league_id: int) -> Tuple[Dict[int, dict], Dict[int, dict]]:
    if league_id in _LEAGUE_CACHE:
        return _LEAGUE_CACHE[league_id]
    tfile = TEAM_DIR / f"{league_id}.json"
    ofile = OPP_DIR / f"{league_id}.json"
    trows = _index_team_rows(_load_json(tfile) or {})
    orows = _index_team_rows(_load_json(ofile) or {})
    _LEAGUE_CACHE[league_id] = (trows, orows)
    return trows, orows

# ---------------- Rates ----------------
def _aligned_pairs(league_id: int, team_id: int) -> List[Tuple[int,int]]:
    """(team_goals, opp_goals) pairs, latest->older, up to LAST_N, via fixture_ids."""
    trows, orows = _league_rows(league_id)
    trow, orow = trows.get(team_id), orows.get(team_id)
    if not trow or not orow:
        return []
    t_fids = [int(x) for x in (trow.get("fixture_ids") or [])]
    o_fids = [int(x) for x in (orow.get("fixture_ids") or [])]
    def _ints(seq):
        out=[]
        for v in (seq or []):
            try: out.append(int(float(v)))
            except: pass
        return out
    t_goals = _ints(trow.get("goals_last_n"))
    o_goals = _ints(orow.get("opp_goals_last_n"))
    tg = {fid: g for fid, g in zip(t_fids, t_goals)}
    og = {fid: g for fid, g in zip(o_fids, o_goals)}
    ordered = [fid for fid in t_fids if fid in og][:LAST_N]
    pairs=[]
    for fid in ordered:
        if fid in tg and fid in og:
            pairs.append((tg[fid], og[fid]))
    return pairs

def team_o25_rate(league_id: int, team_id: int) -> Tuple[Optional[float], int]:
    key = (league_id, team_id)
    if key in _RATE_CACHE_O25:
        return _RATE_CACHE_O25[key]
    pairs = _aligned_pairs(league_id, team_id)
    sample = len(pairs)
    if sample < MIN_SAMPLE or sample == 0:
        _RATE_CACHE_O25[key] = (None, sample); return _RATE_CACHE_O25[key]
    overs = sum(1 for gf, ga in pairs if (gf + ga) >= 3)
    _RATE_CACHE_O25[key] = (100.0 * overs / sample, sample)
    return _RATE_CACHE_O25[key]

def team_btts_rate(league_id: int, team_id: int) -> Tuple[Optional[float], int]:
    key = (league_id, team_id)
    if key in _RATE_CACHE_BTTS:
        return _RATE_CACHE_BTTS[key]
    pairs = _aligned_pairs(league_id, team_id)
    sample = len(pairs)
    if sample < MIN_SAMPLE or sample == 0:
        _RATE_CACHE_BTTS[key] = (None, sample); return _RATE_CACHE_BTTS[key]
    hits = sum(1 for gf, ga in pairs if gf > 0 and ga > 0)
    _RATE_CACHE_BTTS[key] = (100.0 * hits / sample, sample)
    return _RATE_CACHE_BTTS[key]

def team_o15_for_rate(league_id: int, team_id: int) -> Tuple[Optional[float], int]:
    """Single-team Over 1.5 (team scored 2+). Uses team goals only; no opponent alignment."""
    key = (league_id, team_id)
    if key in _RATE_CACHE_O15F:
        return _RATE_CACHE_O15F[key]
    trows, _ = _league_rows(league_id)
    trow = trows.get(team_id)
    if not trow:
        _RATE_CACHE_O15F[key] = (None, 0); return _RATE_CACHE_O15F[key]
    # latest->older as written by build_team_series
    def _ints(seq):
        out=[]
        for v in (seq or []):
            try: out.append(int(float(v)))
            except: pass
        return out
    goals = _ints(trow.get("goals_last_n"))[:LAST_N]
    sample = len(goals)
    if sample < MIN_SAMPLE or sample == 0:
        _RATE_CACHE_O15F[key] = (None, sample); return _RATE_CACHE_O15F[key]
    hits = sum(1 for g in goals if g >= 2)
    _RATE_CACHE_O15F[key] = (100.0 * hits / sample, sample)
    return _RATE_CACHE_O15F[key]

# ---------------- Render ----------------
def render_output(o25_entries: List[dict], btts_entries: List[dict], single_o15_teams: List[dict]) -> str:
    lines: List[str] = []
    lines.append("I’ve collated a high-probability goals list based on stats from their last 10 games.")
    lines.append("")
    lines.append("Leave a like if you find these useful.")
    lines.append("")
    # Over 2.5 — both teams gate
    lines.append("📊Combined over 2.5 goals >70%📊")
    lines.append("(Both teams’ matches have had at least 2.5 goals in 70%+ of their last 10)")
    lines.append("")
    if o25_entries:
        for e in o25_entries:
            lines.append(f"{e['home_name']} ({e['hpct']:.0f}%) vs {e['away_name']} ({e['apct']:.0f}%)")
    else:
        lines.append("(No fixtures cleared the threshold based on the latest files.)")
    lines.append("")
    # BTTS — both teams gate
    lines.append("📊Both Teams To Score (BTTS) >70%📊")
    lines.append("(Both teams’ matches have seen goals at both ends in 70%+ of their last 10)")
    lines.append("")
    if btts_entries:
        for e in btts_entries:
            lines.append(f"{e['home_name']} ({e['hpct']:.0f}%) vs {e['away_name']} ({e['apct']:.0f}%)")
    else:
        lines.append("(No fixtures cleared the threshold based on the latest files.)")
    lines.append("")
    # Single-team O1.5
    lines.append(f"📈 Single-team over 1.5 ≥{SINGLE_TEAM_O15_THRESHOLD:.0f}%📈")
    lines.append(f"(Share of last-10 league matches where the team scored 2+ goals)")
    lines.append("")
    if single_o15_teams:
        for t in single_o15_teams:
            lines.append(f"{t['team_name']} ({t['pct']:.0f}%)")
    else:
        lines.append("(No teams cleared the threshold based on the latest files.)")
    lines.append("")
    return "\n".join(lines)

# ---------------- Main ----------------
def main():
    fixtures = load_upcoming_fixtures()

    o25_list: List[dict] = []
    btts_list: List[dict] = []
    single_o15_map: Dict[Tuple[int,int], dict] = {}  # (league_id, team_id) -> row

    for fx in fixtures:
        lid = fx.get("league_id")
        parts = fx.get("participants") or []
        home, away = pick_home_away(parts)
        if not (home and away): 
            continue
        try:
            lid = int(lid); hid = int(home.get("id")); aid = int(away.get("id"))
        except Exception:
            continue
        # Prefer names from team_stats if available
        trows, _ = _league_rows(lid)
        hname = (trows.get(hid, {}).get("team_name") or home.get("name") or "Home").strip()
        aname = (trows.get(aid, {}).get("team_name") or away.get("name") or "Away").strip()

        # Over 2.5 rates
        hpct_o25, _ = team_o25_rate(lid, hid)
        apct_o25, _ = team_o25_rate(lid, aid)
        if hpct_o25 is not None and apct_o25 is not None:
            if hpct_o25 >= THRESHOLD and apct_o25 >= THRESHOLD:
                o25_list.append({
                    "home_name": hname, "away_name": aname,
                    "hpct": hpct_o25, "apct": apct_o25,
                    "combined": (hpct_o25 + apct_o25) / 2.0
                })

        # BTTS rates
        hpct_btts, _ = team_btts_rate(lid, hid)
        apct_btts, _ = team_btts_rate(lid, aid)
        if hpct_btts is not None and apct_btts is not None:
            if hpct_btts >= THRESHOLD and apct_btts >= THRESHOLD:
                btts_list.append({
                    "home_name": hname, "away_name": aname,
                    "hpct": hpct_btts, "apct": apct_btts,
                    "combined": (hpct_btts + apct_btts) / 2.0
                })

        # Single-team O1.5 (team scored 2+)
        for tid, tname in ((hid, hname), (aid, aname)):
            pct, _s = team_o15_for_rate(lid, tid)
            if pct is not None and pct >= SINGLE_TEAM_O15_THRESHOLD:
                key = (lid, tid)
                # keep highest pct if somehow duplicated across multiple fixtures
                prev = single_o15_map.get(key)
                if (not prev) or pct > prev["pct"]:
                    single_o15_map[key] = {"team_name": tname, "pct": pct}

    # Rank
    o25_list.sort(key=lambda x: (-x["combined"], x["home_name"], x["away_name"]))
    btts_list.sort(key=lambda x: (-x["combined"], x["home_name"], x["away_name"]))
    single_o15_teams = sorted(single_o15_map.values(), key=lambda x: (-x["pct"], x["team_name"]))

    # Render + write
    text = render_output(o25_list, btts_list, single_o15_teams)
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(text, encoding="utf-8")
    print(f"Wrote {OUTPUT_PATH} (O2.5-both={len(o25_list)}, BTTS-both={len(btts_list)}, O1.5-single={len(single_o15_teams)})")

if __name__ == "__main__":
    main()
