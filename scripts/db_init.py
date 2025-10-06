#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os, sqlite3

DB_PATH = os.environ.get("DB_PATH", "data/data.db")
os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)

SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS teams(
  team_id INTEGER PRIMARY KEY,
  name TEXT,
  country_id INTEGER,
  updated_at TEXT
);
CREATE TABLE IF NOT EXISTS players(
  player_id INTEGER PRIMARY KEY,
  name TEXT,
  position_id INTEGER,
  updated_at TEXT
);
CREATE TABLE IF NOT EXISTS fixtures(
  fixture_id INTEGER PRIMARY KEY,
  league_id INTEGER,
  starting_at TEXT,
  home_team_id INTEGER,
  away_team_id INTEGER,
  state TEXT,
  updated_at TEXT
);
CREATE TABLE IF NOT EXISTS lineups(
  fixture_id INTEGER,
  team_id INTEGER,
  player_id INTEGER,
  type_id INTEGER,
  formation_position INTEGER,
  jersey_number INTEGER,
  PRIMARY KEY(fixture_id, team_id, player_id)
);
CREATE TABLE IF NOT EXISTS appearances(
  fixture_id INTEGER,
  team_id INTEGER,
  player_id INTEGER,
  minutes_played INTEGER,
  started INTEGER,
  PRIMARY KEY(fixture_id, player_id)
);
CREATE TABLE IF NOT EXISTS player_match_stats(
  fixture_id INTEGER,
  team_id INTEGER,
  player_id INTEGER,
  shots_total INTEGER,
  shots_on_target INTEGER,
  fouls INTEGER,
  tackles INTEGER,
  PRIMARY KEY(fixture_id, player_id)
);
CREATE TABLE IF NOT EXISTS team_match_stats(
  fixture_id INTEGER,
  team_id INTEGER,
  shots_total INTEGER,
  shots_on_target INTEGER,
  fouls INTEGER,
  tackles INTEGER,
  PRIMARY KEY(fixture_id, team_id)
);
"""

def main():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.executescript(SCHEMA)
    conn.commit()
    conn.close()
    print(f"[OK] schema ready at {DB_PATH}")

if __name__ == "__main__":
    main()
