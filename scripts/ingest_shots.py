#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os, json, sqlite3, datetime as dt
from typing import Optional
from common import (
    fixture_lineups_minutes_and_shots, pick_home_away,
    pos_id_to_label, APPEARANCE_MINUTES_THRESHOLD
)

DATA_DIR = os.environ.get("DATA_DIR", "data")
DB_PATH = os.environ.get("DB_PATH", "data/data.db")
FIX_PATH = os.path.join(DATA_DIR, "fixtures.json")

def upsert_fixture(cur, fx: dict):
    fid = int(fx["id"])
    st  = fx.get("state") or {}
    parts = fx.get("participants") or []
    home, away = pick_home_away(parts)
    home_id = int(home["id"]) if home else None
    away_id = int(away["id"]) if away else None

    cur.execute("""
    INSERT INTO fixtures(fixture_id, league_id, starting_at, home_team_id, away_team_id, state, updated_at)
    VALUES(?,?,?,?,?,?,?)
    ON CONFLICT(fixture_id) DO UPDATE SET
        league_id=excluded.league_id,
        starting_at=excluded.starting_at,
        home_team_id=excluded.home_team_id,
        away_team_id=excluded.away_team_id,
        state=excluded.state,
        updated_at=excluded.updated_at
    """, (fid, fx.get("league_id"), fx.get("starting_at"),
          home_id, away_id, (st.get("short_name") or st.get("name") or ""), dt.datetime.utcnow().isoformat()+"Z"))

def upsert_lineups_and_stats(cur, fixture_id: int):
    lineups_map, shots_map, minutes_map = fixture_lineups_minutes_and_shots(fixture_id)
    # Write appearances + per-player stats + lineups
    for pid, lp in lineups_map.items():
        team_id = int(lp.get("team_id")) if lp.get("team_id") else None
        pos_id  = lp.get("position_id")
        jersey  = lp.get("jersey_number")
        started = 1 if (lp.get("type_id") == 11) else 0
        mins    = int(minutes_map.get(pid, 0))
        shots   = int(shots_map.get(pid, 0))

        # players table (best-effort)
        pname = (lp.get("player_name") or "").strip()
        cur.execute("""
        INSERT INTO players(player_id, name, position_id, updated_at)
        VALUES(?,?,?,?)
        ON CONFLICT(player_id) DO UPDATE SET
            name=excluded.name, position_id=excluded.position_id, updated_at=excluded.updated_at
        """, (pid, pname, pos_id, dt.datetime.utcnow().isoformat()+"Z"))

        # lineups table
        if team_id is not None:
            cur.execute("""
            INSERT INTO lineups(fixture_id, team_id, player_id, type_id, formation_position, jersey_number)
            VALUES(?,?,?,?,?,?)
            ON CONFLICT(fixture_id, team_id, player_id) DO UPDATE SET
                type_id=excluded.type_id,
                formation_position=excluded.formation_position,
                jersey_number=excluded.jersey_number
            """, (fixture_id, team_id, pid, lp.get("type_id"),
                  lp.get("formation_position"), jersey))

            # appearances (store all minutes; your >=45' rule applies when *querying*)
            cur.execute("""
            INSERT INTO appearances(fixture_id, team_id, player_id, minutes_played, started)
            VALUES(?,?,?,?,?)
            ON CONFLICT(fixture_id, player_id) DO UPDATE SET
                team_id=excluded.team_id,
                minutes_played=excluded.minutes_played,
                started=excluded.started
            """, (fixture_id, team_id, pid, mins, started))

            # player_match_stats (shots now; SOT/fouls/tackles later)
            cur.execute("""
            INSERT INTO player_match_stats(fixture_id, team_id, player_id, shots_total, shots_on_target, fouls, tackles)
            VALUES(?,?,?,?,?,?,?)
            ON CONFLICT(fixture_id, player_id) DO UPDATE SET
                team_id=excluded.team_id,
                shots_total=excluded.shots_total
            """, (fixture_id, team_id, pid, shots, None, None, None))

def main():
    if not os.path.isfile(FIX_PATH):
        raise SystemExit(f"fixtures file missing: {FIX_PATH}")
    with open(FIX_PATH, "r", encoding="utf-8") as f:
        payload = json.load(f)
    fixtures = payload.get("fixtures", []) or []
    if not fixtures:
        print("[WARN] no fixtures to ingest")
        return

    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    cur  = conn.cursor()

    done = 0
    for fx in fixtures:
        try:
            upsert_fixture(cur, fx)
            fid = int(fx["id"])
            upsert_lineups_and_stats(cur, fid)
            conn.commit()
            done += 1
        except Exception as e:
            conn.rollback()
            print(f"[WARN] fixture {fx.get('id')} ingest failed: {e}")

    conn.close()
    print(f"[OK] ingested {done}/{len(fixtures)} fixtures into {DB_PATH}")

if __name__ == "__main__":
    main()
