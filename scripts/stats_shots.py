#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Read fixtures_lineups.jsonl → compute shots hit-rates for each (player, team, league).
- last up to 10 league appearances (>=45'), across this + last season.
Saves per-league JSON:
  data/YYYY-MM-DD/shots_stats_<LEAGUE_ID>.json
  data/latest/shots_stats_<LEAGUE_ID>.json
"""

import os, sys, json
from typing import Dict, Any, List, Tuple, Set
from common import (
    run_date_dir, latest_dir, league_name, player_last_n_shots_series, compute_hit_rate,
    APPEARANCE_MINUTES_THRESHOLD
)

def load_jsonl(path: str) -> List[dict]:
    rows=[]
    if not os.path.isfile(path):
        return rows
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line=line.strip()
            if not line: continue
            try:
                rows.append(json.loads(line))
            except Exception:
                pass
    return rows

def main():
    fixtures_path = os.path.join(latest_dir(), "fixtures_lineups.jsonl")
    rows = load_jsonl(fixtures_path)
    if not rows:
        print(f"[ERR] no fixtures jsonl at {fixtures_path}")
        sys.exit(2)

    # league -> { (player_id, team_id): player_row }
    by_league: Dict[int, Dict[Tuple[int,int], dict]] = {}

    for fx in rows:
        lid = int(fx["league_id"])
        for side in ("home_xi", "away_xi"):
            for lp in fx.get(side) or []:
                pid = lp.get("player_id"); tid = lp.get("team_id")
                if pid is None or tid is None: continue
                key = (int(pid), int(tid))
                by_league.setdefault(lid, {})
                if key not in by_league[lid]:
                    by_league[lid][key] = {
                        "player_id": int(pid),
                        "team_id": int(tid),
                        "player_name": lp.get("player_name"),
                        "position": lp.get("position") or "?",
                    }

    # compute stats
    for lid, players in by_league.items():
        out: List[dict] = []
        for (pid, tid), meta in players.items():
            s10 = player_last_n_shots_series(tid, pid, 10, lid)
            apps10 = len(s10); hit10 = compute_hit_rate(s10) if apps10>0 else 0.0
            s5 = s10[:5] if len(s10)>=5 else s10
            apps5 = len(s5); hit5 = compute_hit_rate(s5) if apps5>0 else 0.0
            out.append({
                "player_id": pid,
                "team_id": tid,
                "player_name": meta["player_name"],
                "pos": meta["position"],
                "apps10": apps10,
                "hit10": hit10,
                "apps5": apps5,
                "hit5": hit5,
            })
        # save
        dd = run_date_dir()
        latest = latest_dir()
        p1 = os.path.join(dd, f"shots_stats_{lid}.json")
        p2 = os.path.join(latest, f"shots_stats_{lid}.json")
        with open(p1, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
        with open(p2, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
        print(f"[OK] {league_name(lid)} → {p1}")

if __name__ == "__main__":
    main()
