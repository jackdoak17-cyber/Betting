#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Gather per-player SHOTS for players in predicted XIs — LEAGUE MATCHES ONLY.

For each league we track (from data/predicted_xi/by_league/*.json):
  • Build the set of (team, players) from predicted XIs (with position info).
  • For each team, fetch fixtures via /v3/football/fixtures/between/{from}/{to}?teams=<team_id>
    and include participants;statistics;statistics.player;state, then
    **filter to fixtures with league_id == <that league>**.
  • For each tracked player, collect the shot count for their **last 10 league appearances**
    (newest → older). If a stats row exists but no explicit shots field, treat as 0.

Outputs:
  data/player_stats/shots/by_league/<league_id>.json
     {
       "utc_time": "...",
       "league_id": 8,
       "players": [
         {
           "player_id": 123,
           "name": "Bukayo Saka",
           "team": "Arsenal",
           "position_label": "FWD",
           "position_id": 27,
           "last10_shots": [3,2,1, ...]   # newest → older
         },
         ...
       ]
     }

  data/player_stats/shots/summary_shots.txt
     Human-readable lines grouped by league -> team:
       Arsenal
         Bukayo Saka [FWD] = 3,2,1,...

Env:
  SPORTMONKS_TOKEN (required)
"""

import os
import sys
import json
import time
import datetime as dt
from typing import Dict, List, Optional, Tuple, Set

import requests

API_BASE = "https://api.sportmonks.com/v3/football"
API_TOKEN = os.getenv("SPORTMONKS_TOKEN")
if not API_TOKEN:
    raise SystemExit("ERROR: SPORTMONKS_TOKEN is not set.")

# pacing / retries
TIMEOUT = 25
RETRIES = 3
BACKOFF = 1.6
GLOBAL_MIN_DELAY = 0.15

# rolling windows to walk back in time until we have last10 per player
WINDOW_DAYS = 35
MAX_ROLLING_WINDOWS = 14           # ~16 months worst case
PER_TEAM_MAX_FIXTURES = 400        # guardrail

# IO
PRED_XI_DIR = "data/predicted_xi/by_league"
OUT_BASE = "data/player_stats/shots"
OUT_BY_LEAGUE = os.path.join(OUT_BASE, "by_league")
os.makedirs(OUT_BY_LEAGUE, exist_ok=True)

# memoize GETs per run
_MEMO: Dict[str, dict] = {}
_last_call = 0.0


def _throttle():
    global _last_call
    now = time.time()
    if now - _last_call < GLOBAL_MIN_DELAY:
        time.sleep(GLOBAL_MIN_DELAY - (now - _last_call))
    _last_call = time.time()


def api_get(path: str, params: Optional[dict] = None) -> dict:
    if params is None:
        params = {}
    params = {**params, "api_token": API_TOKEN}
    url = f"{API_BASE}/{path.lstrip('/')}"
    key = url + "?" + "&".join(f"{k}={params[k]}" for k in sorted(params))
    if key in _MEMO:
        return _MEMO[key]

    last_exc = None
    for i in range(1, RETRIES + 1):
        _throttle()
        try:
            r = requests.get(url, params=params, timeout=TIMEOUT)
            if r.status_code == 429:
                sleep = min(60, (BACKOFF ** i) * 2.0)
                print(f"[429] {path} — sleeping {sleep:.1f}s")
                time.sleep(sleep)
                continue
            r.raise_for_status()
            j = r.json()
            _MEMO[key] = j
            return j
        except Exception as e:
            last_exc = e
            if i < RETRIES:
                sleep = BACKOFF ** i
                print(f"[RETRY] {path} (attempt {i}) sleeping {sleep:.1f}s")
                time.sleep(sleep)
            else:
                raise
    raise last_exc  # pragma: no cover


def _load_json(path: str) -> Optional[dict]:
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def load_from_predicted_xi():
    """
    Read data/predicted_xi/by_league/*.json and return:

    leagues -> {
      lid: {
        'teams': { team_id: set(player_ids), ... },
        'players': {
           player_id: {
              'name': 'Player',
              'team': 'Team Name',
              'position_label': 'GK/DEF/MID/FWD',
              'position_id': 24/25/26/27
           }, ...
        }
      }, ...
    }
    """
    if not os.path.isdir(PRED_XI_DIR):
        raise SystemExit("No predicted XI folder found. Run the predicted lineups job first.")

    leagues: Dict[int, dict] = {}

    for name in os.listdir(PRED_XI_DIR):
        if not name.endswith(".json"):
            continue
        lid = int(name[:-5])
        blob = _load_json(os.path.join(PRED_XI_DIR, name)) or {}
        fixtures = blob.get("fixtures") or []
        info = leagues.setdefault(lid, {"teams": {}, "players": {}})

        for item in fixtures:
            for side in ("home", "away"):
                team = (item.get(side) or {})
                tname = (team.get("name") or "").strip()
                tid = team.get("team_id")
                if not tid:
                    continue
                players = team.get("predicted_xi") or []
                info["teams"].setdefault(int(tid), set())
                for p in players:
                    pid = p.get("player_id")
                    if not pid:
                        continue
                    pid = int(pid)
                    info["teams"][int(tid)].add(pid)
                    # carry position info from predicted_xi
                    info["players"][pid] = {
                        "name": (p.get("name") or "").strip(),
                        "team": tname,
                        "position_label": p.get("position_label"),
                        "position_id": p.get("position_id"),
                    }
    return leagues


def iso(d: dt.date) -> str:
    return d.strftime("%Y-%m-%d")


def fetch_team_fixtures_between(team_id: int, d_from: dt.date, d_to: dt.date) -> List[dict]:
    """
    One window fetch with pagination; includes per-player statistics.
    """
    all_rows: List[dict] = []
    page = 1
    while True:
        j = api_get(
            f"fixtures/between/{iso(d_from)}/{iso(d_to)}",
            {
                "teams": team_id,
                "order": "desc",
                "page": page,
                "include": "participants;statistics;statistics.player;state;league"
            },
        )
        data = j.get("data") or []
        all_rows.extend(data)
        meta = j.get("meta") or {}
        last_page = meta.get("last_page", page)
        if page >= last_page:
            break
        page += 1
        if len(all_rows) >= PER_TEAM_MAX_FIXTURES:
            break
    return all_rows


def extract_shots_from_statrow(row: dict) -> Optional[int]:
    """
    Be generous with v3 shapes:
      - direct numeric fields: shots_total / total_shots / shots
      - nested dict: shots = { total|attempts|total_attempts: N }
      - nested *-shots*: try common numeric keys
      - details[] / stats[]: look for entries with 'shots' & ('total' or plain)
    """
    for k in ("shots_total", "total_shots", "shots"):
        v = row.get(k)
        if isinstance(v, (int, float)):
            return int(v)

    shots = row.get("shots")
    if isinstance(shots, dict):
        for k in ("total", "attempts", "total_attempts", "value", "value_int"):
            v = shots.get(k)
            if isinstance(v, (int, float)):
                return int(v)

    for k, v in row.items():
        if isinstance(v, dict) and "shots" in k.lower():
            for sub in ("total", "attempts", "total_attempts", "value", "value_int"):
                sv = v.get(sub)
                if isinstance(sv, (int, float)):
                    return int(sv)

    details = row.get("details") or row.get("stats") or []
    if isinstance(details, list):
        for d in details:
            t = (d.get("type") or d.get("code") or d.get("name") or d.get("identifier") or "")
            t = str(t).lower()
            if "shots" in t and ("total" in t or t.strip() == "shots"):
                for vk in ("value", "value_int", "number", "amount", "total"):
                    val = d.get(vk)
                    if isinstance(val, (int, float)):
                        return int(val)
    return None


def collect_last10_for_team_players_in_league(team_id: int, league_id: int, player_ids: Set[int]) -> Dict[int, List[Tuple[int, str]]]:
    """
    Return {player_id: [(shots, starting_at_iso), ...]} for LEAGUE fixtures only (matching league_id).
    Newest → older. Only counts fixtures where the player's statistics row exists for this team.
    """
    res: Dict[int, List[Tuple[int, str]]] = {pid: [] for pid in player_ids}

    def all_done() -> bool:
        return all(len(v) >= 10 for v in res.values())

    end = dt.datetime.now(dt.timezone.utc).date()
    windows_done = 0

    while windows_done < MAX_ROLLING_WINDOWS and not all_done():
        start = end - dt.timedelta(days=WINDOW_DAYS)
        fixtures = fetch_team_fixtures_between(team_id, start, end)
        # **league-only filter**
        fixtures = [fx for fx in fixtures if int(fx.get("league_id") or 0) == int(league_id)]
        fixtures.sort(key=lambda x: x.get("starting_at") or "", reverse=True)

        for fx in fixtures:
            st = (fx.get("state") or {}).get("state") or (fx.get("state") or {}).get("short_name") or ""
            st = str(st).lower()
            if st in {"scheduled", "postponed", "cancelled"}:
                continue

            stats = fx.get("statistics") or []
            if not stats:
                continue

            when = (fx.get("starting_at") or "").replace("T", " ").replace("Z", "")

            for row in stats:
                pid = row.get("player_id") or ((row.get("player") or {}).get("id") if isinstance(row.get("player"), dict) else None)
                try:
                    pid = int(pid) if pid is not None else None
                except Exception:
                    pid = None
                if pid is None or pid not in player_ids:
                    continue

                # ensure row is for this team (ignore if player's row belongs to another club)
                team_in_row = row.get("team_id") or (row.get("participant") or {}).get("id")
                try:
                    if team_in_row is not None and int(team_in_row) != int(team_id):
                        continue
                except Exception:
                    pass

                shots = extract_shots_from_statrow(row)
                if shots is None:
                    shots = 0

                if len(res[pid]) < 10:
                    res[pid].append((int(shots), when))

            if all_done():
                break

        end = start
        windows_done += 1

    # newest → older & cap
    for pid in res:
        res[pid].sort(key=lambda x: x[1], reverse=True)
        res[pid] = res[pid][:10]
    return res


def main():
    leagues = load_from_predicted_xi()

    now_iso = dt.datetime.now(dt.timezone.utc).isoformat()
    print(f"Leagues (from predicted_xi): {sorted(leagues.keys())}")
    total_tracked = sum(len(info["players"]) for info in leagues.values())
    print(f"Tracked players (unique across leagues): {total_tracked}")

    # For each league, gather stats team-by-team
    per_league_player_series: Dict[int, Dict[int, List[Tuple[int, str]]]] = {}

    for lid in sorted(leagues.keys()):
        info = leagues[lid]
        teams = info["teams"]
        print(f"\n=== League {lid} ===")
        league_series: Dict[int, List[Tuple[int, str]]] = {}
        tcount = len(teams)
        for idx, (team_id, player_ids) in enumerate(teams.items(), 1):
            print(f"[{idx}/{tcount}] Team {team_id} — players: {len(player_ids)} (league {lid})")
            series_map = collect_last10_for_team_players_in_league(team_id, lid, player_ids)
            league_series.update(series_map)
        per_league_player_series[lid] = league_series

        # write JSON per league
        pack = {
            "utc_time": now_iso,
            "league_id": lid,
            "players": []
        }
        pmeta = info["players"]  # {pid: {name, team, position_label, position_id}}
        for pid, meta in pmeta.items():
            seq = [s for (s, _ts) in league_series.get(pid, [])]  # newest → older
            pack["players"].append({
                "player_id": pid,
                "name": meta.get("name"),
                "team": meta.get("team"),
                "position_label": meta.get("position_label"),
                "position_id": meta.get("position_id"),
                "last10_shots": seq,
            })
        # Sort by team then name for stable diffs
        pack["players"].sort(key=lambda x: (x["team"] or "", x["name"] or "").lower())

        with open(os.path.join(OUT_BY_LEAGUE, f"{lid}.json"), "w", encoding="utf-8") as f:
            json.dump(pack, f, ensure_ascii=False)

    # Human summary
    lines: List[str] = []
    lines.append(f"Time (UTC): {now_iso}")
    lines.append("Endpoint   : fixtures-between (LEAGUE ONLY)")
    lines.append("Order      : most recent → older")
    lines.append("")
    for lid in sorted(leagues.keys()):
        lines.append(f"===== League {lid} =====")
        info = leagues[lid]
        pmeta = info["players"]
        # group by team name
        team_groups: Dict[str, List[int]] = {}
        for pid, meta in pmeta.items():
            team_groups.setdefault(meta.get("team") or "?", []).append(pid)

        for team_name in sorted(team_groups.keys()):
            lines.append(f"{team_name}")
            for pid in sorted(team_groups[team_name], key=lambda p: (pmeta[p].get("name") or "").lower()):
                meta = pmeta[pid]
                seq = [s for (s, _ts) in per_league_player_series[lid].get(pid, [])]
                seq_str = ",".join(str(x) for x in seq) if seq else "(no data)"
                pos = meta.get("position_label") or "?"
                lines.append(f"  {meta.get('name')} [{pos}] = {seq_str}")
            lines.append("")  # blank line between teams

    os.makedirs(OUT_BASE, exist_ok=True)
    with open(os.path.join(OUT_BASE, "summary_shots.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines).rstrip() + "\n")

    print("\nDone.")
    print(f"Wrote: {OUT_BY_LEAGUE}/<league_id>.json and {OUT_BASE}/summary_shots.txt")


if __name__ == "__main__":
    try:
        main()
    except requests.HTTPError as e:
        print(f"[HTTPError] {e}", file=sys.stderr)
        raise
