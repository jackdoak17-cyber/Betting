#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Collect fixtures (next 6 days) and predicted XIs per league:
- Prefer official XI, else use team's last league fixture with recorded starters.
Saves:
  data/YYYY-MM-DD/fixtures_lineups.jsonl
  data/latest/fixtures_lineups.jsonl
"""

import os, sys, datetime as dt
from typing import List, Dict, Any, Tuple
from common import (
    daterange_str, today_utc, days_ahead, fixtures_by_date, pick_home_away,
    team_last_fixture_with_xi, LINEUP_TYPE_STARTER, pos_id_to_label,
    run_date_dir, latest_dir, append_jsonl, league_name, LEAGUES, sm_get
)

def starters_for_fixture_team(fixture_id: int, team_id: int) -> List[dict]:
    # Try official XI first
    try:
        full = sm_get(f"fixtures/{fixture_id}", {"include": "lineups;lineups.player"})
        data = full.get("data") or {}
        xs = [l for l in (data.get("lineups") or [])
              if l.get("team_id")==team_id and l.get("type_id")==LINEUP_TYPE_STARTER]
        if xs:
            xs.sort(key=lambda x: x.get("formation_position") or 9999)
            return xs[:11]
    except Exception:
        pass
    # Fallback to last league fixture with XI
    fx = None
    # We need the league_id; fetch minimal fixture to read it:
    meta = sm_get(f"fixtures/{fixture_id}", {"include": ""}).get("data") or {}
    league_id = meta.get("league_id")
    if not league_id:
        return []
    fx = team_last_fixture_with_xi(team_id, league_id)
    if not fx: return []
    lineups = fx.get("lineups") or []
    xs = [l for l in lineups if l.get("team_id")==team_id and l.get("type_id")==LINEUP_TYPE_STARTER]
    xs.sort(key=lambda x: x.get("formation_position") or 9999)
    return xs[:11]

def main():
    start = today_utc()
    end = days_ahead(start, 5)
    dates = daterange_str(start, end)

    out_path_dd = os.path.join(run_date_dir(), "fixtures_lineups.jsonl")
    out_path_latest = os.path.join(latest_dir(), "fixtures_lineups.jsonl")

    # fresh files each run
    for p in (out_path_dd, out_path_latest):
        if os.path.exists(p):
            os.remove(p)

    league_ids = list(LEAGUES.keys())
    print(f"[OK] scanning fixtures {dates[0]} → {dates[-1]} for leagues: {', '.join(str(x) for x in league_ids)}")

    for ds in dates:
        fxs = fixtures_by_date(ds, league_filter=set(league_ids))
        fxs.sort(key=lambda x: x.get("starting_at") or "")
        rows: List[Dict[str,Any]] = []
        for fx in fxs:
            parts = fx.get("participants") or []
            home, away = pick_home_away(parts)
            if not (home and away): continue
            home_id, away_id = home["id"], away["id"]
            # home XI
            hxi = starters_for_fixture_team(fx["id"], home_id)
            axi = starters_for_fixture_team(fx["id"], away_id)

            def serialize_xi(xi):
                out=[]
                for lp in (xi or []):
                    pid = lp.get("player_id")
                    if pid is None: continue
                    out.append({
                        "player_id": int(pid),
                        "player_name": (lp.get("player_name") or "").strip(),
                        "jersey_number": lp.get("jersey_number"),
                        "position": pos_id_to_label(lp.get("position_id")),
                        "team_id": lp.get("team_id"),
                    })
                return out

            rows.append({
                "date": ds,
                "fixture_id": fx.get("id"),
                "league_id": fx.get("league_id"),
                "league_name": league_name(fx.get("league_id")),
                "starting_at": fx.get("starting_at"),
                "name": fx.get("name"),
                "home_id": home_id,
                "away_id": away_id,
                "home_name": home.get("name"),
                "away_name": away.get("name"),
                "home_xi": serialize_xi(hxi),
                "away_xi": serialize_xi(axi),
            })

        if rows:
            append_jsonl(out_path_dd, rows)
            append_jsonl(out_path_latest, rows)

    print(f"[DONE] fixtures+XIs → {out_path_dd} and {out_path_latest}")

if __name__ == "__main__":
    main()
