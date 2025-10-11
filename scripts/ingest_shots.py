#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Gather per-player SHOTS for players in predicted XIs — LEAGUE MATCHES ONLY.

Strategy
- Read teams/players (with positions) from data/predicted_xi/by_league/*.json
- For each team:
    1) Try /v3/football/fixtures/between/{from}/{to}?teams=<team_id>
    2) If that 404s, FALL BACK to per-day: /fixtures/date/YYYY-MM-DD (same window)
       and keep only fixtures that (a) match league_id and (b) include the team.
- From each matching fixture, take per-player statistics and extract "shots".
- Build each player's last 10 league appearances' shots (newest → older).

Outputs
- data/player_stats/shots/by_league/<league_id>.json
- data/player_stats/shots/summary_shots.txt

Env
- SPORTMONKS_TOKEN
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
PER_TEAM_MAX_FIXTURES = 500        # guardrail

# IO
PRED_XI_DIR = "data/predicted_xi/by_league"
OUT_BASE = "data/player_stats/shots"
OUT_BY_LEAGUE = os.path.join(OUT_BASE, "by_league")
os.makedirs(OUT_BY_LEAGUE, exist_ok=True)

# memo per run
_MEMO: Dict[str, dict] = {}
_last_call = 0.0


def _throttle():
    global _last_call
    now = time.time()
    if now - _last_call < GLOBAL_MIN_DELAY:
        time.sleep(GLOBAL_MIN_DELAY - (now - _last_call))
    _last_call = time.time()


def api_get(path: str, params: Optional[dict] = None, allow_404: bool = False) -> Optional[dict]:
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
            if r.status_code == 404 and allow_404:
                _MEMO[key] = None
                return None
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
                if allow_404 and isinstance(e, requests.HTTPError) and e.response is not None and e.response.status_code == 404:
                    _MEMO[key] = None
                    return None
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
      { league_id: {
           "teams": { team_id: set(player_ids) },
           "players": { player_id: {name, team, position_label, position_id} }
        }, ... }
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
    Try the /fixtures/between fast path; if it returns None (404), fall back to per-day.
    Returns a flat list of fixtures with participants/statistics/league included.
    """
    all_rows: List[dict] = []

    # --- fast path ---
    page = 1
    while True:
        j = api_get(
            f"fixtures/between/{iso(d_from)}/{iso(d_to)}",
            {
                "teams": team_id,
                "order": "desc",
                "page": page,
                "include": "participants;statistics;statistics.player;state;league",
            },
            allow_404=True,
        )
        if j is None:
            break  # use fallback
        data = j.get("data") or []
        all_rows.extend(data)
        meta = j.get("meta") or {}
        last_page = meta.get("last_page", page)
        if page >= last_page:
            return all_rows
        page += 1
        if len(all_rows) >= PER_TEAM_MAX_FIXTURES:
            return all_rows

    # --- fallback: per-day ---
    seen_ids: Set[int] = set()
    day = d_to
    while day >= d_from:
        page = 1
        while True:
            j = api_get(
                f"fixtures/date/{iso(day)}",
                {
                    "order": "desc",
                    "page": page,
                    "include": "participants;statistics;statistics.player;state;league",
                },
                allow_404=True,
            )
            if j is None:
                break  # no fixtures that date
            data = j.get("data") or []
            for fx in data:
                fid = fx.get("id")
                if fid and fid not in seen_ids:
                    seen_ids.add(fid)
                    all_rows.append(fx)
            meta = j.get("meta") or {}
            last_page = meta.get("last_page", page)
            if page >= last_page:
                break
            page += 1
        day -= dt.timedelta(days=1)
        if len(all_rows) >= PER_TEAM_MAX_FIXTURES:
            break

    return all_rows


def extract_shots_from_statrow(row: dict) -> Optional[int]:
    """
    Heuristic extractor for per-player shot totals across common v3 shapes.
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
    Return {player_id: [(shots, starting_at_iso), ...]} for LEAGUE fixtures only.
    Newest → older. Only count fixtures that contain a statistics row for the player
    and belong to the given team_id.
    """
    res: Dict[int, List[Tuple[int, str]]] = {pid: [] for pid in player_ids}

    def all_done() -> bool:
        return all(len(v) >= 10 for v in res.values())

    end = dt.datetime.now(dt.timezone.utc).date()
    windows_done = 0

    while windows_done < MAX_ROLLING_WINDOWS and not all_done():
        start = end - dt.timedelta(days=WINDOW_DAYS)
        fixtures = fetch_team_fixtures_between(team_id, start, end)

        # League-only filter + basic cleanup
        fixtures = [fx for fx in fixtures if int(fx.get("league_id") or 0) == int(league_id)]
        fixtures.sort(key=lambda x: x.get("starting_at") or "", reverse=True)

        for fx in fixtures:
            st = (fx.get("state") or {}).get("state") or (fx.get("state") or {}).get("short_name") or ""
            st = str(st).lower()
            if st in {"scheduled", "postponed", "cancelled"}:
                continue

            # ensure team participated
            parts = fx.get("participants") or []
            if not any(int(p.get("id") or -1) == int(team_id) for p in parts):
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

                # ensure stat row is recorded for THIS team
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
    lids_sorted = sorted(leagues.keys())
    print(f"Leagues (from predicted_xi): {lids_sorted}")
    total_tracked = sum(len(info["players"]) for info in leagues.values())
    print(f"Tracked players (unique across leagues): {total_tracked}")

    per_league_player_series: Dict[int, Dict[int, List[Tuple[int, str]]]] = {}

    for lid in lids_sorted:
        info = leagues[lid]
        teams = info["teams"]
        print(f"\n=== League {lid} ===")
        league_series: Dict[int, List[Tuple[int, str]]] = {}
        tcount = len(teams)
        for idx, (team_id, player_ids) in enumerate(teams.items(), 1):
            print(f"[{idx}/{tcount}] Team {team_id} — players: {len(player_ids)} (league {lid})")
            try:
                series_map = collect_last10_for_team_players_in_league(team_id, lid, player_ids)
            except requests.HTTPError as e:
                print(f"[WARN] team {team_id} league {lid}: {e}", file=sys.stderr)
                series_map = {pid: [] for pid in player_ids}
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

        # ✅ FIX: sort by team then name (case-insensitive) — lower() per element, not on the tuple
        pack["players"].sort(key=lambda x: ((x.get("team") or "").lower(), (x.get("name") or "").lower()))

        with open(os.path.join(OUT_BY_LEAGUE, f"{lid}.json"), "w", encoding="utf-8") as f:
            json.dump(pack, f, ensure_ascii=False)

    # Human summary
    lines: List[str] = []
    lines.append(f"Time (UTC): {now_iso}")
    lines.append("Endpoint   : between (fast) → date fallback (on 404)")
    lines.append("Filter     : league-only; newest → older")
    lines.append("")
    for lid in lids_sorted:
        lines.append(f"===== League {lid} =====")
        info = leagues[lid]
        pmeta = info["players"]
        # group by team name
        team_groups: Dict[str, List[int]] = {}
        for pid, meta in pmeta.items():
            team_groups.setdefault(meta.get("team") or "?", []).append(pid)

        for team_name in sorted(team_groups.keys(), key=lambda s: (s or "").lower()):
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
