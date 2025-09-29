#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os, json
from typing import List, Dict, Optional, Tuple
from common import sm_get, next6_dates, LEAGUES, DATA_DIR, pos_label

LINEUP_TYPE_STARTER = 11

def get_fixtures_for_date(date_str: str, league_filter: Optional[set]) -> List[dict]:
    params = {"include": "participants;state;league", "order": "asc", "page": 1}
    j = sm_get(f"fixtures/date/{date_str}", params, ttl_sec=1200)
    data = j.get("data", []) or []
    meta = j.get("meta") or {}
    last_page = meta.get("last_page", 1)
    for p in range(2, last_page + 1):
        params["page"] = p
        jp = sm_get(f"fixtures/date/{date_str}", params, ttl_sec=1200)
        data.extend(jp.get("data", []) or [])
    out = []
    for fx in data:
        lid = fx.get("league_id")
        if league_filter and lid not in league_filter:
            continue
        if not fx.get("participants"):
            continue
        out.append(fx)
    return out

def pick_home_away(parts: List[dict]) -> Tuple[Optional[dict], Optional[dict]]:
    home = next((p for p in parts if (p.get("meta") or {}).get("location") == "home"), None)
    away = next((p for p in parts if (p.get("meta") or {}).get("location") == "away"), None)
    return home, away

def last_fixture_with_starters(team_id: int, league_id: int) -> Optional[dict]:
    # try team latest first
    try:
        j = sm_get(f"teams/{team_id}", {"include": "latest.league;latest.lineups;latest.lineups.player"}, ttl_sec=1800)
        latest = j.get("data", {}).get("latest")
        lst = latest if isinstance(latest, list) else ([latest] if latest else [])
        lst = [fx for fx in lst if fx and fx.get("league_id") == league_id]
        lst.sort(key=lambda x: x.get("starting_at") or "", reverse=True)
        for fx in lst:
            fid = fx.get("id")
            if not fid: continue
            full = sm_get(f"fixtures/{fid}", {"include": "lineups;lineups.player"}, ttl_sec=1800).get("data", {})
            if any(l.get("type_id") == LINEUP_TYPE_STARTER and l.get("team_id") == team_id for l in (full.get("lineups") or [])):
                full["participants"] = fx.get("participants") or []
                return full
    except Exception:
        pass
    # fallback: walk back ~120 days by date but league-filtered (cheap)
    import datetime as dt
    from common import DATE_FMT, today_utc
    today = today_utc()
    for back in range(1, 121):
        ds = (today - dt.timedelta(days=back)).strftime(DATE_FMT)
        try:
            fxs = get_fixtures_for_date(ds, league_filter={league_id})
        except Exception:
            continue
        for fx in fxs:
            if any(p.get("id") == team_id for p in (fx.get("participants") or [])):
                full = sm_get(f"fixtures/{fx['id']}", {"include": "lineups;lineups.player"}, ttl_sec=3600).get("data", {})
                if any(l.get("type_id") == LINEUP_TYPE_STARTER and l.get("team_id") == team_id for l in (full.get("lineups") or [])):
                    full["participants"] = fx.get("participants") or []
                    return full
    return None

def build_predicted_xi(fx: dict, team_id: int, league_id: int) -> List[dict]:
    # try official XI
    try:
        full = sm_get(f"fixtures/{fx['id']}", {"include": "lineups;lineups.player"}, ttl_sec=900).get("data", {})
        starters = [l for l in (full.get("lineups") or []) if l.get("type_id") == LINEUP_TYPE_STARTER and l.get("team_id") == team_id]
        if starters:
            starters.sort(key=lambda x: x.get("formation_position") or 9999)
            return starters[:11]
    except Exception:
        pass
    # fallback
    last = last_fixture_with_starters(team_id, league_id) or {}
    lineups = last.get("lineups") or []
    starters = [l for l in lineups if l.get("team_id") == team_id and l.get("type_id") == LINEUP_TYPE_STARTER]
    starters.sort(key=lambda x: x.get("formation_position") or 9999)
    return starters[:11]

def main():
    leagues = set(LEAGUES.keys())
    dates = next6_dates()
    fixtures: List[dict] = []
    for ds in dates:
        try:
            fixtures.extend(get_fixtures_for_date(ds, league_filter=leagues))
        except Exception as e:
            print(f"[WARN] fixtures {ds}: {e}")
    fixtures.sort(key=lambda x: x.get("starting_at") or "")

    # write fixtures
    with open(os.path.join(DATA_DIR, "fixtures.jsonl"), "w", encoding="utf-8") as f:
        for fx in fixtures:
            f.write(json.dumps(fx, ensure_ascii=False) + "\n")

    # build XIs
    xi_rows = []
    for fx in fixtures:
        parts = fx.get("participants") or []
        home, away = pick_home_away(parts)
        if not (home and away): continue
        lid = fx.get("league_id")
        for team in (home, away):
            xi = build_predicted_xi(fx, team["id"], lid)
            for lp in xi:
                pid = lp.get("player_id")
                if pid is None:  # guard against None IDs
                    continue
                xi_rows.append({
                    "fixture_id": fx.get("id"),
                    "league_id": lid,
                    "team_id": team["id"],
                    "team_name": team.get("name"),
                    "player_id": int(pid),
                    "player_name": (lp.get("player_name") or "").strip(),
                    "jersey": lp.get("jersey_number"),
                    "position_id": lp.get("position_id"),
                    "position": pos_label(lp.get("position_id")),
                    "source": "official" if "lineups" in (fx.keys()) else "last_league"
                })

    with open(os.path.join(DATA_DIR, "lineups.jsonl"), "w", encoding="utf-8") as f:
        for row in xi_rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    print(f"[OK] fixtures={len(fixtures)}  xi_rows={len(xi_rows)}")

if __name__ == "__main__":
    main()
