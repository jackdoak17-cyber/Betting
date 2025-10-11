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
    p.add_argument("--leagues", type=str, default=None, help="Comma-separated league IDs to limit processing.")
    p.add_argument("--timeout", type=int, default=25, help="HTTP timeout seconds.")
    p.add_argument("--max-retries", type=int, default=3, help="Max retries for HTTP 5xx/429/404.")
    return p.parse_args()

def _coalesce(*vals, default=None):
    for v in vals:
        if v is not None:
            return v
    return default

def _as_int(val, default=0) -> int:
    try:
        if val is None:
            return default
        if isinstance(val, (int,)):
            return int(val)
        if isinstance(val, float):
            return int(val)
        s = str(val).strip()
        if s == "":
            return default
        # some values might be "2.0" or "2"
        return int(float(s))
    except Exception:
        return default

def _norm_str(x) -> str:
    if x is None:
        return ""
    if isinstance(x, (list, tuple)):
        return " ".join(map(str, x))
    return str(x)

def _finished_state(fix: Dict[str, Any]) -> bool:
    sdata = ((fix.get("state") or {}).get("data")) or {}
    # Consider common finished states
    code = (_coalesce(sdata.get("code"), sdata.get("name"), sdata.get("short_name"), sdata.get("state"), "") or "").lower()
    short = (_coalesce(sdata.get("short_name"), sdata.get("code"), "") or "").upper()
    if code in {"finished", "ft", "after extra time", "aet", "fter", "fter_pen", "pen", "ft_pen"}:
        return True
    if short in {"FT", "AET", "FT_PEN", "PEN"}:
        return True
    return False

def _get(d: Dict[str, Any], path: str, default=None):
    cur = d
    for part in path.split("."):
        if cur is None:
            return default
        if isinstance(cur, dict):
            cur = cur.get(part)
        else:
            return default
    return cur if cur is not None else default

# --- Input: predicted XI / tracked players ----------------------------------

def _possible_input_paths() -> List[str]:
    candidates = []
    # preferred
    for name in ["predicted_players", "predicted_xi"]:
        for ext in [".json", ".ndjson", ".csv"]:
            candidates.append(os.path.join("data", f"{name}{ext}"))
        # allow nested under data/predicted
        for ext in [".json", ".ndjson", ".csv"]:
            candidates.append(os.path.join("data", name, f"latest{ext}"))
    return [p for p in candidates if os.path.exists(p)]

def _normalize_record(rec: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    # Try multiple key spellings
    lid = _as_int(_coalesce(rec.get("league_id"), rec.get("lid"), rec.get("league"), rec.get("leagueId")))
    team_id = _as_int(_coalesce(rec.get("team_id"), rec.get("tid"), rec.get("teamId"), rec.get("teamID")))
    player_id = _as_int(_coalesce(rec.get("player_id"), rec.get("pid"), rec.get("playerId"), rec.get("playerID")))
    if not (lid and team_id and player_id):
        return None
    team_name = _coalesce(rec.get("team_name"), rec.get("team"), rec.get("teamName"), "")
    player_name = _coalesce(rec.get("player_name"), rec.get("name"), rec.get("player"), "")
    pos = _coalesce(rec.get("position"), rec.get("pos"), rec.get("role"), rec.get("lineup_position"), "")
    return {
        "league_id": lid,
        "team_id": team_id,
        "team_name": team_name,
        "player_id": player_id,
        "player_name": player_name,
        "position": pos,
    }

def read_tracked_players() -> Tuple[Dict[int, Dict[int, Dict[str, Any]]], Set[int]]:
    """
    Returns:
      leagues_map:
        { league_id: { team_id: {"team_name": str, "players": { player_id: {"name": str, "pos": str} } } } }
      league_ids set
    """
    paths = _possible_input_paths()
    if not paths:
        raise SystemExit(
            "No input found. Please create one of:\n"
            "  data/predicted_players.json|.ndjson|.csv\n"
            "  data/predicted_xi.json|.ndjson|.csv\n"
            "with columns/keys: league_id, team_id, team_name, player_id, player_name, position"
        )
    # Pick first existing
    path = paths[0]
    leagues: Dict[int, Dict[int, Dict[str, Any]]] = {}
    def add_row(row: Dict[str, Any]):
        norm = _normalize_record(row or {})
        if not norm:
            return
        lid = norm["league_id"]
        tid = norm["team_id"]
        tname = norm["team_name"]
        pid = norm["player_id"]
        pname = norm["player_name"]
        ppos = norm["position"]
        if lid not in leagues:
            leagues[lid] = {}
        if tid not in leagues[lid]:
            leagues[lid][tid] = {"team_name": tname, "players": {}}
        leagues[lid][tid]["team_name"] = leagues[lid][tid]["team_name"] or tname
        leagues[lid][tid]["players"][pid] = {"name": pname, "pos": ppos}
    if path.endswith(".json"):
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, list):
                for rec in data:
                    add_row(rec)
            elif isinstance(data, dict) and "records" in data and isinstance(data["records"], list):
                for rec in data["records"]:
                    add_row(rec)
            elif isinstance(data, dict):
                # maybe keyed by league/team
                # try to enumerate nested dicts
                for v in data.values():
                    if isinstance(v, list):
                        for rec in v:
                            add_row(rec)
                    elif isinstance(v, dict):
                        add_row(v)
            else:
                eprint(f"[WARN] Unrecognized JSON structure in {path}")
    elif path.endswith(".ndjson"):
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                add_row(rec)
    elif path.endswith(".csv"):
        with open(path, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                add_row(row)
    else:
        raise SystemExit(f"Unsupported input: {path}")
    # Return plus league id set
    lids = set(leagues.keys())
    return leagues, lids

# --- HTTP helpers ------------------------------------------------------------

def api_get(path: str, *, params: Optional[Dict[str, Any]] = None, timeout: int = 25, max_retries: int = 3) -> Dict[str, Any]:
    if not API_TOKEN:
        raise SystemExit("Missing SPORTMONKS_TOKEN env var")
    url = f"{BASE_URL}/{path.lstrip('/')}"
    params = dict(params or {})
    params["api_token"] = API_TOKEN

    backoff = 1.6
    for attempt in range(max_retries + 1):
        try:
            resp = requests.get(url, params=params, timeout=timeout)
            if resp.status_code == 404:
                # Treat as empty page
                return {"data": [], "meta": {"pagination": {"total": 0, "count": 0, "per_page": 0, "current_page": 1, "total_pages": 1}}}
            resp.raise_for_status()
            return resp.json()
        except requests.HTTPError as e:
            code = getattr(e.response, "status_code", None)
            if code in (429, 500, 502, 503, 504) and attempt < max_retries:
                sleep = backoff * (attempt + 1)
                eprint(f"[RETRY] {path} (attempt {attempt+1}) sleeping {sleep:.1f}s")
                time.sleep(sleep)
                continue
            raise

def paged_get(path: str, *, params: Optional[Dict[str, Any]] = None, timeout: int = 25, max_retries: int = 3) -> Iterable[Dict[str, Any]]:
    page = 1
    while True:
        p = dict(params or {})
        p["page"] = page
        j = api_get(path, params=p, timeout=timeout, max_retries=max_retries)
        data = j.get("data") or []
        for item in data:
            yield item
        meta = j.get("meta") or {}
        pagination = meta.get("pagination") or {}
        cur = pagination.get("current_page") or page
        tot = pagination.get("total_pages") or page
        if cur >= tot:
            break
        page += 1

# --- Shots extraction --------------------------------------------------------

DETAIL_TYPE_CODES = {
    "shots_total": {"shots-total", "shots_total", "shots total", "shots"},
    "shots_on_target": {"shots-on-target", "shots_on_target"},
    "shots_off_target": {"shots-off-target", "shots_off_target"},
    # sportmonks has two codes for blocked
    "shots_blocked": {"shots-blocked", "shots_blocked", "blocked-shots", "blocked_shots"},
}

LINEUP_INCLUDE = "participants;league;state;lineups.player;lineups.details"
LINEUP_TYPES = "42,41,86,58,97"  # total, off target, on target, shots-blocked, blocked-shots

def _codes_map_from_details(details: List[Dict[str, Any]]) -> Dict[str, int]:
    m: Dict[str, int] = {}
    for d in details or []:
        # v3: detail has type (object with code), value
        code = ""
        t = d.get("type")
        if isinstance(t, dict):
            code = (t.get("code") or t.get("name") or t.get("short_code") or "").strip().lower()
        if not code:
            # Fallbacks seen in some payloads
            code = (d.get("code") or d.get("type_code") or "").strip().lower()
        val = _as_int(d.get("value"))
        if code:
            m[code] = val
    return m

def extract_total_shots_from_details(details: List[Dict[str, Any]]) -> int:
    codes = _codes_map_from_details(details)
    # Prefer explicit total
    for alias in DETAIL_TYPE_CODES["shots_total"]:
        if alias in codes:
            return _as_int(codes[alias], 0)
    # Else sum components
    total = 0
    for alias in DETAIL_TYPE_CODES["shots_on_target"]:
        total += _as_int(codes.get(alias), 0)
    for alias in DETAIL_TYPE_CODES["shots_off_target"]:
        total += _as_int(codes.get(alias), 0)
    for alias in DETAIL_TYPE_CODES["shots_blocked"]:
        total += _as_int(codes.get(alias), 0)
    return total

def shots_for_players_in_fixture(fix: Dict[str, Any], team_id: int, target_player_ids: Set[int]) -> Dict[int, int]:
    """Return {player_id: shots_total} for players of team_id who appeared in this fixture."""
    out: Dict[int, int] = {}
    lineups = _get(fix, "lineups.data") or []
    for lu in lineups:
        if (lu.get("team_id") or lu.get("teamId")) != team_id:
            continue
        player = _get(lu, "player.data") or {}
        pid = _as_int(player.get("id"))
        if pid not in target_player_ids:
            continue
        details = _get(lu, "details.data") or []
        out[pid] = extract_total_shots_from_details(details)
    return out

# --- Fetch fixtures for a team ----------------------------------------------

def fetch_team_fixtures_between(team_id: int, start: str, end: str, *, timeout: int, max_retries: int) -> List[Dict[str, Any]]:
    params = dict(
        teams=team_id,
        order="desc",
        include=LINEUP_INCLUDE,
        lineupDetailTypes=LINEUP_TYPES,
    )
    fixtures = list(paged_get(f"fixtures/between/{start}/{end}", params=params, timeout=timeout, max_retries=max_retries))
    return fixtures

# --- Main processing ---------------------------------------------------------

def main():
    args = parse_args()
    if args.since:
        start_date = args.since
    else:
        start_date = (datetime.now(timezone.utc) - timedelta(days=args.days)).strftime("%Y-%m-%d")
    end_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    leagues_map, lids_from_input = read_tracked_players()

    # optional filter
    if args.leagues:
        only = {int(x) for x in args.leagues.split(",") if x.strip().isdigit()}
        # intersect
        lids = list(sorted(lids_from_input.intersection(only)))
    else:
        lids = list(sorted(lids_from_input))

    print(f"Time (UTC): {datetime.now(timezone.utc).isoformat()}")
    print(f"Endpoint   : fixtures/between  (league-only; last 10 appearances)")
    print()
    print(f"Leagues (from input): {lids}")
    # Count players
    total_players = sum(len(team["players"]) for lid in lids for team in leagues_map.get(lid, {}).values())
    print(f"Tracked players (unique across leagues): {total_players}")
    print()

    for lid in lids:
        print(f"===== League {lid} =====")
        teams = leagues_map.get(lid, {})
        # stable team order by name
        team_items = sorted(teams.items(), key=lambda kv: _norm_str(_get(kv[1], "team_name")).lower())
        for team_id, tinfo in team_items:
            team_name = tinfo.get("team_name") or f"Team {team_id}"
            print(f"{team_name} (Team {team_id})")
            players = tinfo.get("players") or {}
            # fetch fixtures once per team
            try:
                fixtures = fetch_team_fixtures_between(team_id, start_date, end_date, timeout=args.timeout, max_retries=args.max_retries)
            except requests.HTTPError as e:
                eprint(f"[ERROR] fetch fixtures team {team_id}: {e}")
                fixtures = []
            # Filter league & finished, newest first (API already order=desc)
            league_fixtures = []
            for fix in fixtures:
                # league filter
                ldata = _get(fix, "league.data") or {}
                fix_lid = _as_int(ldata.get("id"))
                if fix_lid != lid:
                    continue
                if not _finished_state(fix):
                    continue
                league_fixtures.append(fix)

            # Build per-player last-10 sequence (appearances)
            player_ids_set: Set[int] = set(players.keys())
            per_player_series: Dict[int, List[int]] = {pid: [] for pid in player_ids_set}

            for fix in league_fixtures:
                shots_map = shots_for_players_in_fixture(fix, team_id, player_ids_set)
                # Append if the player appeared (present in lineup details); if not in lineup, skip this fixture for that player.
                for pid in list(player_ids_set):
                    if pid in shots_map:
                        series = per_player_series[pid]
                        if len(series) < 10:
                            series.append(_as_int(shots_map[pid], 0))
                # early exit if everyone reached 10
                if all(len(per_player_series[pid]) >= 10 for pid in player_ids_set):
                    break

            # print players sorted
            items = [(info.get("name") or f"Player {pid}", pid, info.get("pos") or "") for pid, info in players.items()]
            items.sort(key=lambda x: _norm_str(x[0]).lower())

            for name, pid, pos in items:
                seq = per_player_series.get(pid) or []
                if not seq:
                    print(f"  {name} [{pos}] = (no data)")
                else:
                    # print most-recent-first, already in that order
                    print(f"  {name} [{pos}] = {','.join(str(x) for x in seq)}")
            print()
        print()

if __name__ == "__main__":
    main()
