#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Fetch fixtures for the next 6 days (UTC) for selected leagues.
For each fixture, record:
- basic fixture info
- predicted XI per team:
    - prefer official starters if available
    - else fallback to team's last league fixture with starters

Outputs (under data/YYYY-MM-DD/):
- fixtures.jsonl     (one row per fixture)
- xi_rows.jsonl      (one row per XI player)
"""

from __future__ import annotations

import sys
from typing import Dict, List, Optional

from common import (
    # API/date helpers
    today_utc, days_ahead, daterange_str, DATE_FMT,
    # IO/path helpers
    run_date_dir, write_jsonl,
    # API calls
    fixtures_by_date, pick_home_away, team_last_fixture_with_xi,
    # misc
    pos_id_to_label, LINEUP_TYPE_STARTER, api_get
)

# ---- Target leagues (SportMonks ids) ----
LEAGUES: Dict[int, str] = {
    8:   "Premier League",
    9:   "Championship",
    384: "Serie A",
    387: "Serie B",
    82:  "Bundesliga",
    301: "Ligue 1",
    564: "La Liga",
    567: "La Liga 2",
    600: "Super Lig",
}

def build_predicted_xi(fixture: dict, team_id: int, league_id: int) -> List[dict]:
    """
    Prefer official XI from this fixture; else fallback to last league match with starters.
    Returns a list of lineup rows (as returned by SportMonks) for starters (<= 11).
    """
    fid = fixture.get("id")
    # Try official XI
    try:
        fx_full = api_get(f"fixtures/{fid}", {"include": "lineups;lineups.player"}).get("data", {})
        starters = [
            l for l in (fx_full.get("lineups") or [])
            if l.get("type_id") == LINEUP_TYPE_STARTER and l.get("team_id") == team_id
        ]
        if starters:
            starters.sort(key=lambda x: x.get("formation_position") or 9999)
            return starters[:11]
    except Exception:
        pass

    # Fallback to last league match with starters
    last = team_last_fixture_with_xi(team_id, league_id) or {}
    lineups = last.get("lineups") or []
    starters = [l for l in lineups if l.get("team_id") == team_id and l.get("type_id") == LINEUP_TYPE_STARTER]
    starters.sort(key=lambda x: x.get("formation_position") or 9999)
    return starters[:11]

def main():
    start = today_utc()
    end = days_ahead(start, 5)  # next 6 days inclusive
    dates = daterange_str(start, end)

    out_dir = run_date_dir(base="data", d=start)

    fixtures_out: List[dict] = []
    xi_out: List[dict] = []

    print(f"Fetching fixtures for {start.strftime(DATE_FMT)} → {end.strftime(DATE_FMT)} (UTC)…")

    # gather fixtures by date then filter by league
    for ds in dates:
        day_fixtures = fixtures_by_date(ds, league_filter=set(LEAGUES))
        for fx in day_fixtures:
            fid = fx.get("id")
            lid = fx.get("league_id")
            lname = (fx.get("league") or {}).get("name") or LEAGUES.get(lid, str(lid))
            parts = fx.get("participants") or []
            home, away = pick_home_away(parts)
            if not (home and away):
                continue

            fixtures_out.append({
                "fixture_id": fid,
                "date_utc": fx.get("starting_at"),
                "league_id": lid,
                "league_name": lname,
                "home_id": home.get("id"),
                "home_name": home.get("name"),
                "away_id": away.get("id"),
                "away_name": away.get("name"),
                "state": (fx.get("state") or {}).get("short_name") or fx.get("state_id"),
            })

            # Predicted XI for both sides
            for team in (home, away):
                team_id = team.get("id")
                if not team_id:
                    continue
                xi = build_predicted_xi(fx=fx, team_id=team_id, league_id=lid)
                for lp in xi:
                    xi_out.append({
                        "fixture_id": fid,
                        "league_id": lid,
                        "team_id": team_id,
                        "team_name": team.get("name"),
                        "player_id": lp.get("player_id"),
                        "player_name": (lp.get("player_name") or "").strip(),
                        "jersey_number": lp.get("jersey_number"),
                        "position_id": lp.get("position_id"),
                        "position": pos_id_to_label(lp.get("position_id")),
                        "formation_position": lp.get("formation_position"),
                    })

    # Write outputs
    fixtures_path = f"{out_dir}/fixtures.jsonl"
    xi_path = f"{out_dir}/xi_rows.jsonl"
    write_jsonl(fixtures_path, fixtures_out)
    write_jsonl(xi_path, xi_out)

    print(f"[OK] fixtures={len(fixtures_out)}  xi_rows={len(xi_out)}")
    if not fixtures_out:
        print("[WARN] No fixtures found in the window/leagues – check league IDs or API token.", file=sys.stderr)

if __name__ == "__main__":
    main()
