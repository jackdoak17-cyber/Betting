#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Over 2.5 Goals — Shortlist (Combined% > THRESHOLD)
Reads ONLY local JSONs:
  - data/fixtures/by_league/{league_id}.json
  - data/team_stats/by_league/{league_id}.json          (needs: goals_last_n, fixture_ids)
  - data/team_opponent_stats/by_league/{league_id}.json (needs: opp_goals_last_n, fixture_ids)

Method:
  - For each team, join goals_last_n with opp_goals_last_n by fixture_id (latest->older),
    compute % of matches with total_goals >= 3 across the last LAST_N (default 10).
  - For each upcoming fixture, Combined% = mean(HomeTeam%, AwayTeam%).
  - Keep fixtures only if Combined% >= THRESHOLD (default 70) and both teams have at least
    MIN_SAMPLE (default 6) valid recent matches.

Output:
  posts/over25_matches.md (title + concise intro + ranked shortlist, no dates/league sections).

Env (optional):
  OUTPUT_PATH   (default: posts/over25_matches.md)
  LAST_N        (default: 10)
  MIN_SAMPLE    (default: 6)
  THRESHOLD     (default: 70)   # Combined% cutoff
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


# ---- Core calculations ----
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
    Compute Over 2.5 % for a given team using last LAST_N league fixtures.
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

    # Build fixture -> goals maps, then align via intersection, ordered latest->older
    t_fids = [int(x) for x in (trow.get("fixture_ids") or [])]
    o_fids = [int(x) for x in (orow.get("fixture_ids") or [])]
    t_goals = list(map(int, (trow.get("goals_last_n") or [])))
    o_goals = list(map(int, (orow.get("opp_goals_last_n") or [])))

    tg = {fid: g for fid, g in zip(t_fids, t_goals)}
    og = {fid: g for fid, g in zip(o_fids, o_goals)}

    # Keep the order from team list (latest_first), but only where both sides exist
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
    """
    Try to find home/away participants robustly.
    """
    home, away = None, None
    for p in parts or []:
        loc = ((p.get("meta") or {}).get("location") or "").lower()
        if loc == "home":
            home = p
        elif loc == "away":
            away = p
    # Fallback: assume first=home, second=away
    if not home and len(parts or []) >= 1:
        home = parts[0]
    if not away and len(parts or []) >= 2:
        away = parts[1]
    return home, away


def load_upcoming_fixtures() -> List[dict]:
    """
    Read fixtures from data/fixtures/by_league/*.json and return a flat list.
    """
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


# ---- Render ----
def render_shortlist(entries: List[dict]) -> str:
    """
    Build final markdown with title, concise intro and ranked shortlist.
    Expect entries with keys: home_name, away_name, combined, hpct, apct.
    """
    header = [
        "# Over 2.5 Goals — Shortlist (Last-10 Form, >70% Combined)",
        "",
        "Using each team’s **last 10 league games**, I calculated their Over 2.5 hit rates "
        "(share of matches with **3+ goals**). For each upcoming fixture, **Combined% = mean(Home%, Away%)**. "
        f"Shown only if **Combined% > {THRESHOLD:.0f}%** and both teams have at least **{MIN_SAMPLE}** recent league games. Cups excluded.",
        "",
    ]

    if not entries:
        body = ["(No fixtures cleared the threshold based on the latest files.)", ""]
    else:
        lines = []
        for e in entries:
            lines.append(f"• **{e['home_name']} vs {e['away_name']}** — **{e['combined']:.1f}%** "
                         f"(H {e['hpct']:.1f}%, A {e['apct']:.1f}%)")
        body = lines + [""]

    footer = []
    return "\n".join(header + body + footer).rstrip() + "\n"


# ---- Main ----
def main():
    fixtures = load_upcoming_fixtures()
    if not fixtures:
        OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
        OUTPUT_PATH.write_text("# Over 2.5 Goals — Shortlist (Last-10 Form, >70% Combined)\n\n(No fixtures found.)\n", encoding="utf-8")
        print(f"Wrote {OUTPUT_PATH}")
        return

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

        if not isinstance(hid, int) or not isinstance(aid, int) or not isinstance(lid, int):
            # try coerce to int
            try:
                hid = int(hid); aid = int(aid); lid = int(lid)
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

    text = render_shortlist(shortlist)
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(text, encoding="utf-8")
    print(f"Wrote {OUTPUT_PATH} ({len(shortlist)} fixtures shown)")


if __name__ == "__main__":
    main()
