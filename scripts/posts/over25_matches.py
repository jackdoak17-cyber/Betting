#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Over 2.5 Goals — Ranked upcoming fixtures (pure local)

Reads:
  - data/fixtures/by_league/{league_id}.json
  - data/team_stats/by_league/{league_id}.json
  - data/team_opponent_stats/by_league/{league_id}.json

Computes for each TEAM (last N, default 10):
  - overall % of matches with total_goals >= 3 using:
      total_goals = goals_last_n + opp_goals_last_n (aligned by fixture_id)
  - optional home% and away% (venue-aware, if enabled)

For each UPCOMING fixture:
  - Combined% = mean(home_overall%, away_overall%)  [default]
  - (Optionally venue-aware: mean(home_home%, away_away%) if USE_HOME_AWAY=1)

Outputs a single social-ready markdown file (see OUTPUT_PATH env).
Skips a league if required local files are missing.

Env (optional):
  OUTPUT_PATH       (default: posts/over25_all.md)
  LAST_N            (default: 10)
  MIN_GAMES         (default: 6)   # min samples per team to include a fixture
  TOP_K_PER_LEAGUE  (default: 8)   # top rows per league
  USE_HOME_AWAY     (default: 0)   # 1 => use home(only) + away(only) for combined
"""

import os
import json
from pathlib import Path
from typing import Dict, List, Optional, Tuple

ROOT = Path(".")
FIX_DIR   = ROOT / "data" / "fixtures" / "by_league"
TEAM_DIR  = ROOT / "data" / "team_stats" / "by_league"
OPP_DIR   = ROOT / "data" / "team_opponent_stats" / "by_league"

OUTPUT_PATH       = Path(os.getenv("OUTPUT_PATH", "posts/over25_all.md"))
LAST_N            = int(os.getenv("LAST_N", "10"))
MIN_GAMES         = int(os.getenv("MIN_GAMES", "6"))
TOP_K_PER_LEAGUE  = int(os.getenv("TOP_K_PER_LEAGUE", "8"))
USE_HOME_AWAY     = os.getenv("USE_HOME_AWAY", "0").strip() in ("1","true","TRUE","yes","YES")

def _load_json(p: Path) -> Optional[dict]:
    try:
        if p.is_file():
            return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        pass
    return None

def _idx_by_team(rows: List[dict]) -> Dict[int, dict]:
    out = {}
    for r in rows or []:
        try:
            tid = int(r.get("team_id"))
            out[tid] = r
        except Exception:
            continue
    return out

def _series_by_fixture(entry: dict, key_prefix: str) -> Tuple[Dict[int, int], Dict[int, str]]:
    """
    Build {fixture_id: goals} for either team or opponent entry.
    Also return {fixture_id: location} from locations_last_n (when present).
    """
    goals = entry.get(f"{key_prefix}goals_last_n")
    fids  = entry.get("fixture_ids")
    locs  = entry.get("locations_last_n") or []
    m_goals: Dict[int, int] = {}
    m_locs: Dict[int, str] = {}
    if isinstance(goals, list) and isinstance(fids, list):
        for i, fid in enumerate(fids[:LAST_N]):
            try:
                g = int(goals[i])
                m_goals[int(fid)] = g
                if i < len(locs):
                    m_locs[int(fid)] = (locs[i] or "unknown")
            except Exception:
                continue
    return m_goals, m_locs

def _o25_rates(team_entry: dict, opp_entry: dict) -> dict:
    """
    Compute Over 2.5 rates for a single team using aligned fixtures:
      total = goals_last_n + opp_goals_last_n
    Returns dict with overall/home/away pct and sample sizes.
    """
    if not (team_entry and opp_entry):
        return {"overall_pct": None, "overall_n": 0,
                "home_pct": None, "home_n": 0, "away_pct": None, "away_n": 0}

    team_goals, team_locs = _series_by_fixture(team_entry, "")
    opp_goals , _         = _series_by_fixture(opp_entry , "opp_")

    # Intersect by fixture_id to guarantee alignment
    fids = [fid for fid in team_goals.keys() if fid in opp_goals]
    fids = fids[:LAST_N]

    totals = [(team_goals[f] + opp_goals[f], team_locs.get(f, "unknown")) for f in fids]

    def pct(rows: List[Tuple[int,str]]) -> Tuple[Optional[float], int]:
        if not rows:
            return (None, 0)
        hits = sum(1 for (t, _) in rows if t >= 3)
        return (round(100.0 * hits / len(rows), 1), len(rows))

    overall_pct, overall_n = pct(totals)
    home_pct  , home_n     = pct([r for r in totals if r[1] == "home"])
    away_pct  , away_n     = pct([r for r in totals if r[1] == "away"])

    return {
        "overall_pct": overall_pct, "overall_n": overall_n,
        "home_pct": home_pct, "home_n": home_n,
        "away_pct": away_pct, "away_n": away_n,
    }

def _pick_home_away(parts: List[dict]) -> Tuple[Optional[dict], Optional[dict]]:
    if not isinstance(parts, list) or len(parts) < 2:
        return None, None
    home = next((p for p in parts if ((p.get("meta") or {}).get("location") or "").lower()=="home"), None)
    away = next((p for p in parts if ((p.get("meta") or {}).get("location") or "").lower()=="away"), None)
    if not home or not away:
        # fallback to list order
        home = parts[0] if parts else None
        away = parts[1] if len(parts) > 1 else None
    return home, away

def fmt_pct(x: Optional[float]) -> str:
    return f"{x:.1f}%" if isinstance(x, (int, float)) else "—"

def main():
    sections: List[str] = []
    header = [
        "I’ve ranked upcoming fixtures for **Over 2.5 Goals** using each team’s last 10 league games.",
        "",
        f"Method: combined% = mean(Home team %, Away team %){' (venue-aware)' if USE_HOME_AWAY else ''}.",
        f"Filters: require ≥{MIN_GAMES} recent games per team.",
        "",
    ]
    sections.extend(header)

    by_league_files = sorted([p for p in FIX_DIR.glob("*.json") if p.is_file()])
    if not by_league_files:
        sections.append("_No upcoming fixtures found. Did fetch_fixtures run?_")
        OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
        OUTPUT_PATH.write_text("\n".join(sections) + "\n", encoding="utf-8")
        print(f"Wrote {OUTPUT_PATH}")
        return

    any_rows = 0

    for fx_path in by_league_files:
        fx_blob = _load_json(fx_path) or {}
        fixtures = fx_blob.get("fixtures") or []
        if not fixtures:
            continue
        league_id = fx_blob.get("league_id") or None
        league_name = fx_blob.get("league_name") or f"League {fx_path.stem}"

        team_path = TEAM_DIR / f"{league_id}.json"
        opp_path  = OPP_DIR  / f"{league_id}.json"
        team_blob = _load_json(team_path) or {}
        opp_blob  = _load_json(opp_path) or {}
        team_rows = team_blob.get("teams") or []
        opp_rows  = opp_blob.get("teams") or []
        if not team_rows or not opp_rows:
            # Skip leagues without built series (keep it silent but traceable in logs)
            print(f"[skip] Missing team/opponent series for league {league_id} ({league_name})")
            continue

        team_idx = _idx_by_team(team_rows)
        opp_idx  = _idx_by_team(opp_rows)

        table = []

        for fx in fixtures:
            parts = fx.get("participants") or []
            home, away = _pick_home_away(parts)
            if not (home and away):
                continue
            try:
                hid, aid = int(home.get("id")), int(away.get("id"))
            except Exception:
                continue

            hname = (home.get("name") or "Home").strip()
            aname = (away.get("name") or "Away").strip()

            te_h, te_a = team_idx.get(hid), team_idx.get(aid)
            oe_h, oe_a = opp_idx.get(hid),  opp_idx.get(aid)
            if not (te_h and te_a and oe_h and oe_a):
                # Missing data for either side -> skip fixture
                continue

            r_h = _o25_rates(te_h, oe_h)
            r_a = _o25_rates(te_a, oe_a)

            # Ensure sample size gate
            if r_h["overall_n"] < MIN_GAMES or r_a["overall_n"] < MIN_GAMES:
                continue

            if USE_HOME_AWAY:
                # Use home-only for home side, away-only for away side; if missing, fall back to overall.
                h_pct = r_h["home_pct"] if (r_h["home_n"] >= MIN_GAMES//2 and r_h["home_pct"] is not None) else r_h["overall_pct"]
                a_pct = r_a["away_pct"] if (r_a["away_n"] >= MIN_GAMES//2 and r_a["away_pct"] is not None) else r_a["overall_pct"]
            else:
                h_pct = r_h["overall_pct"]
                a_pct = r_a["overall_pct"]

            if h_pct is None or a_pct is None:
                continue

            combined = round((h_pct + a_pct) / 2.0, 1)
            date_str = (fx.get("starting_at") or "").split("T")[0] or ""

            table.append({
                "combined": combined,
                "h_pct": h_pct, "a_pct": a_pct,
                "h_n": r_h["overall_n"], "a_n": r_a["overall_n"],
                "h": hname, "a": aname, "date": date_str,
            })

        if not table:
            continue

        table.sort(key=lambda r: (-r["combined"], -(r["h_pct"] or 0), -(r["a_pct"] or 0), r["h"], r["a"]))
        keep = table[:TOP_K_PER_LEAGUE]

        sections.append(f"### {league_name}")
        sections.append("")
        for row in keep:
            line = f"• {row['h']} vs {row['a']} — **{row['combined']:.1f}%** combined (H {fmt_pct(row['h_pct'])}, A {fmt_pct(row['a_pct'])}) — {row['date']}"
            sections.append(line)
        sections.append("")

        any_rows += len(keep)

    if not any_rows:
        sections.append("_No qualified fixtures found (check that team series are built for these leagues)._")
    else:
        sections.append("Good luck with your bets today. Any value here?")

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text("\n".join(sections).rstrip() + "\n", encoding="utf-8")
    print(f"Wrote {OUTPUT_PATH}")
    print(f"Total rows: {any_rows}")

if __name__ == "__main__":
    main()
