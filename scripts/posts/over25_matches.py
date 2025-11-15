#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Over 2.5 Goals — Shortlist (Display-only tweak)

Reads ONLY local JSONs:
  - data/fixtures/by_league/{league_id}.json
  - data/team_stats/by_league/{league_id}.json          (needs: goals_last_n, fixture_ids)
  - data/team_opponent_stats/by_league/{league_id}.json (needs: opp_goals_last_n, fixture_ids)

Logic (unchanged):
  - For each team, compute Over 2.5 hit rate across its last LAST_N league games
    by pairing team goals with opponent goals via fixture_ids.
  - For each upcoming fixture: Combined% = mean(Home%, Away%).
  - Keep if Combined% strictly > THRESHOLD (default 70) and each team has ≥ MIN_SAMPLE matches.

Output (formatted like your example):
  Title + short intro + single heading + ranked list lines:
    TeamA (H%) vs TeamB (A%)
  (No bullets, no dates, no leagues shown.)

Env (optional):
  OUTPUT_PATH   (default: posts/over25_matches.md)
  LAST_N        (default: 10)
  MIN_SAMPLE    (default: 6)
  THRESHOLD     (default: 70)
"""

import os
import json
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# ---- Config ----
OUTPUT_PATH = Path(os.getenv("OUTPUT_PATH", "posts/over25_matches.md"))
LAST_N      = int(os.getenv("LAST_N", "10"))
MIN_SAMPLE  = int(os.getenv("MIN_SAMPLE", "6"))
THRESHOLD   = float(os.getenv("THRESHOLD", "70"))

FIX_DIR     = Path("data/fixtures/by_league")
TEAM_DIR    = Path("data/team_stats/by_league")
OPP_DIR     = Path("data/team_opponent_stats/by_league")


# ---- IO helpers ----
def _load_json(p: Path) -> Optional[dict]:
    try:
        if not p.exists():
            return None
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def _index_team_rows(blob: dict) -> Dict[int, dict]:
    """Return {team_id: row} from team_stats/team_opponent_stats payload."""
    out: Dict[int, dict] = {}
    for r in (blob or {}).get("teams", []) or []:
        tid = r.get("team_id")
        if isinstance(tid, int):
            out[tid] = r
    return out


def team_o25_rate(league_id: int, team_id: int) -> Tuple[Optional[float], int]:
    """
    Compute Over 2.5 % for team across LAST_N league fixtures.
    Returns (pct, sample_size). pct is None if insufficient data.
    """
    tfile = TEAM_DIR / f"{league_id}.json"
    ofile = OPP_DIR / f"{league_id}.json"

    tblob = _load_json(tfile) or {}
    oblob = _load_json(ofile) or {}
    if not tblob or not oblob:
        return (None, 0)

    trows = _index_team_rows(tblob)
    orows = _index_team_rows(oblob)
    trow, orow = trows.get(team_id), orows.get(team_id)
    if not trow or not orow:
        return (None, 0)

    # Align latest->older by fixture_id intersection
    t_fids = [int(x) for x in (trow.get("fixture_ids") or [])]
    o_fids = [int(x) for x in (orow.get("fixture_ids") or [])]
    t_goals = list(map(int, (trow.get("goals_last_n") or [])))
    o_goals = list(map(int, (orow.get("opp_goals_last_n") or [])))
    tg = {fid: g for fid, g in zip(t_fids, t_goals)}
    og = {fid: g for fid, g in zip(o_fids, o_goals)}

    ordered_common = [fid for fid in t_fids if fid in og][:LAST_N]

    sample = 0
    overs = 0
    for fid in ordered_common:
        if fid in tg and fid in og:
            total = int(tg[fid]) + int(og[fid])
            sample += 1
            if total >= 3:
                overs += 1

    if sample < MIN_SAMPLE or sample == 0:
        return (None, sample)

    pct = 100.0 * overs / sample
    return (pct, sample)


def pick_home_away(parts: List[dict]) -> Tuple[Optional[dict], Optional[dict]]:
    """Robustly find home/away participants."""
    home, away = None, None
    for p in parts or []:
        loc = ((p.get("meta") or {}).get("location") or "").lower()
        if loc == "home":
            home = p
        elif loc == "away":
            away = p
    if not home and len(parts or []) >= 1:
        home = parts[0]
    if not away and len(parts or []) >= 2:
        away = parts[1]
    return home, away


def load_upcoming_fixtures() -> List[dict]:
    """Flatten fixtures from data/fixtures/by_league/*.json."""
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


def render_output(entries: List[dict]) -> str:
    """
    Exact display style requested:
      Title + two intro lines + heading + parenthetical explainer + list lines:
        TeamA (80%) vs TeamB (90%)
    """
    lines: List[str] = []
    lines.append("I’ve collated high probability goal list based on stats from their last 10 games")
    lines.append("")
    lines.append("Leave a like if you find these useful")
    lines.append("")
    lines.append("📊Combined over 2.5 goals >70%📊")
    lines.append("(Both teams matches have had at least 2.5 goals in 70%+ of their last 10)")
    lines.append("")

    if not entries:
        lines.append("(No fixtures cleared the threshold based on the latest files.)")
        lines.append("")

    for e in entries:
        lines.append(f"{e['home_name']} ({e['hpct']:.0f}%) vs {e['away_name']} ({e['apct']:.0f}%)")

    lines.append("")
    return "\n".join(lines)


def main():
    fixtures = load_upcoming_fixtures()
    shortlist: List[dict] = []

    for fx in fixtures:
        lid = fx.get("league_id")
        parts = fx.get("participants") or []
        home, away = pick_home_away(parts)
        if not (home and away):
            continue

        hid = home.get("id")
        aid = away.get("id")
        hname = (home.get("name") or "Home").strip()
        aname = (away.get("name") or "Away").strip()

        # normalize ids
        try:
            lid = int(lid); hid = int(hid); aid = int(aid)
        except Exception:
            continue

        hpct, hN = team_o25_rate(lid, hid)
        apct, aN = team_o25_rate(lid, aid)
        if hpct is None or apct is None:
            continue

        combined = (hpct + apct) / 2.0
        if combined > THRESHOLD:
            shortlist.append({
                "home_name": hname,
                "away_name": aname,
                "hpct": hpct,
                "apct": apct,
                "combined": combined,
            })

    shortlist.sort(key=lambda x: (-x["combined"], x["home_name"], x["away_name"]))

    text = render_output(shortlist)
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(text, encoding="utf-8")
    print(f"Wrote {OUTPUT_PATH} ({len(shortlist)} fixtures shown)")


if __name__ == "__main__":
    main()
