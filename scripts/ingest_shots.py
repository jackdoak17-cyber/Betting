#!/usr/bin/env python3
"""
ingest_shots.py
----------------
Pulls **league-only** player shot counts for the *last 10 league appearances*
for all players found in a "predicted XI" input file, then prints a league/team
report like:

===== Premier League (LID 8) =====
Arsenal (Team 19)
  Bukayo Saka [FWD] = 4,3,2,4,1,2,3,2,1,0
  Martin Ødegaard [MID] = 2,1,2,1,0,1,2,1
  ...

Notes
- Data source: Sportmonks v3 Football API (fixtures + lineup details).
- We read *player* statistics from `lineups.details`, NOT from `statistics.player`.
- "Total shots" is taken from `shots-total` (type 42) when present; otherwise, we sum
  on-target (86) + off-target (41) + blocked (58, 97).
- Only finished fixtures in the target league are considered.
- For each player, we take their last **10 league appearances** (not team matches).

Input (one of the following, first match wins):
- JSON (.json or .ndjson) at data/predicted_players.* or data/predicted_xi.*
  Each record/object should contain (at least): league_id, team_id, team_name, player_id, player_name, position.
- CSV (.csv) with the same columns.

Env:
- SPORTMONKS_TOKEN must be set.

Run:
  python scripts/ingest_shots.py [--since YYYY-MM-DD] [--days 200] [--leagues 8,9]
"""
from __future__ import annotations

import csv
import json
import os
import sys
import time
import math
import argparse
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Iterable, Tuple, Set

import requests

BASE_URL = "https://api.sportmonks.com/v3/football"
API_TOKEN = os.getenv("SPORTMONKS_TOKEN") or os.getenv("SPORTMONKS_API_TOKEN")

# --- Utility -----------------------------------------------------------------

def eprint(*args, **kwargs):
    print(*args, file=sys.stderr, **kwargs)

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Fetch last-10 league shots per player from Sportmonks.")
    p.add_argument("--since", type=str, default=None, help="Start date (YYYY-MM-DD). Default: 200 days ago.")
    p.add_argument("--days", type=int, default=200, help="Lookback window in days if --since is not provided.")
    p.add_argument("--leagues", type=str, default=None, help="Comma-_
