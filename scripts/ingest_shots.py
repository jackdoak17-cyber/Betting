#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Build last-10 shots per tracked player (from predicted_xi) using Sportmonks.

Outputs:
  data/player_stats/shots/by_league/{league_id}.json
  data/player_stats/shots/summary.txt        (compact)
  data/player_stats/shots/summary_verbose.txt (league → team → player lines)

Rules / Notes:
- We ONLY track players found in data/predicted_xi/by_league/*.json.
- We scan a date window (default 150 days back) and read fixtures with
  include=participants;statistics;statistics.player.
- Shots are parsed best-effort from player statistics (various shapes handled).
- Sequences are oldest → newest, capped at 10 entries per player.

Env:
  SPORTMONKS_TOKEN (required)
  SHOTS_BACK_DAYS  (optional, default 150)
"""

import os
import json
import time
import datetime as dt
from typing import Dict, List, Optional, Tuple, Set

import requests

API_BASE = "https://api.sportmonks.com/v3/football"
API_TOKEN = os.getenv("SPORTMONKS_TOKEN")
if not API_TOKEN:
    raise SystemExit("ERROR: SPORTMONKS_TOKEN is not set.")

# Window
BACK_DAYS = int(os.getenv("SHOTS_BACK_DAYS", "150"))

# HTTP pacing/retry
TIMEOUT = 25
RETRIES = 2             # small retry budget
BACKOFF = 1.6
GLOBAL_MIN_DELAY = 0.12
_last_call_ts = 0.0
_MEMO: Dict[str, dict] = {}

def _pace():
    global _last_call_ts
    now = time.time()
    if now - _last_call_ts < GLOBAL_MIN_DELAY:
        time.sleep(GLOBAL_MIN_DELAY - (now - _last_call_ts))
    _last_call_ts = time.time()

def _key(url: str, params: dict) -> str:
    return url + "?" + "&".join(f"{k}={params[k]}" for k in sorted(params))

def sm_get(path: str, params: Optional[dict] = None, treat_404_as_none: bool = False) -> Optional[dict]:
    if params is None:
        params = {}
    params = {**params, "api_token": API_TOKEN}
    url = f"{API_BASE}/{path.lstrip('/')}"
    k = _key(url, params)
    if k in _MEMO:
        return _MEMO[k]

    last_exc = None
    for attempt in range(1, RETRIES + 2):
        _pace()
        try:
            r = requests.get(url, params=params, timeout=TIMEOUT)
            if r.status_code == 404 and treat_404_as_none:
                return None
            if r.status_code == 429:
                sleep = min(45, (BACKOFF ** attempt) * 2.0)
                print(f"[429] {path} — sleeping {sleep:.1f}s")
                time.sleep(sleep)
                continue
            r.raise_for_status()
            j = r.json()
            _MEMO[k] = j
            return j
        except Exception as e:
            last_exc = e
            # On 404 with treat_404_as_none=False, don't retry pointlessly
            if isinstance(e, requests.HTTPError) and e.response is not None and e.response.status_code == 404:
                if treat_404_as_none:
                    return None
                # still bail quickly on 404
                break
            if attempt < (RETRIES + 1):
                time.sleep(BACKOFF ** attempt)
            else:
                break
    # Final failure
    if treat_404_as_none:
        return None
    if last_exc:
        raise last_exc
    return None

def today_utc() -> dt.date:
    return dt.datetime.now(dt.timezone.utc).date()

def dstr(d: dt.date) -> str:
    return d.strftime("%Y-%m-%d")

# ---------- load tracked players from predicted_xi ----------
def load_tracked() -> Tuple[Dict[int, dict], Dict[int, Set[int]], Set[int]]:
    """
    Returns:
      players: {player_id: {"name","team_id","team_name","league_id"}}
      team_to_players: {team_id: set(player_ids)}
      leagues: {league_ids seen}
    """
    root = "data/predicted_xi/by_league"
    players: Dict[int, dict] = {}
    team_to_players: Dict[int, Set[int]] = {}
    leagues: Set[int] = set()

    if not os.path.isdir(root):
        raise SystemExit("No predicted XIs found. Run predicted lineups first.")

    for name in os.listdir(root):
        if not name.endswith(".json"):
            continue
        path = os.path.join(root, name)
        try:
            blob = json.loads(open(path, "r", encoding="utf-8").read())
        except Exception:
            continue
        lid = int(blob.get("league_id") or name.replace(".json", ""))
        leagues.add(lid)
        for fx in (blob.get("fixtures") or []):
            for side in ("home", "away"):
                t = fx.get(side) or {}
                team_id = int(t.get("team_id") or 0)
                team_name = t.get("name") or ""
                if not team_id:
                    continue
                for p in (t.get("predicted_xi") or []):
                    pid = int(p.get("player_id") or 0)
                    if not pid:
                        continue
                    players[pid] = {
                        "name": (p.get("name") or "").strip(),
                        "team_id": team_id,
                        "team_name": team_name,
                        "league_id": lid,
                    }
                    team_to_players.setdefault(team_id, set()).add(pid)

    return players, team_to_players, leagues

# ---------- extract shots from various stat shapes ----------
def _try_get(d: dict, *keys, default=None):
    cur = d
    for k in keys:
        if not isinstance(cur, dict) or k not in cur:
            return default
        cur = cur[k]
    return cur

def extract_player_shots(stat_row: dict) -> Optional[int]:
    """
    Accept multiple Sportmonks shapes. Return int shots or None.
    Common shapes seen:
      { "shots": { "total": 3, ... } }
      { "shots_total": 3, ... }
      { "shots": { "shots_total": 3 } }
    """
    # nested
    v = _try_get(stat_row, "shots", "total")
    if isinstance(v, (int, float)):
        return int(v)
    v = _try_get(stat_row, "shots", "shots_total")
    if isinstance(v, (int, float)):
        return int(v)
    # flat
    for k in ("shots_total", "total_shots", "shots"):
        v = stat_row.get(k)
        if isinstance(v, (int, float)):
            return int(v)
        # sometimes it's dict with "total"
        if isinstance(v, dict):
            t = v.get("total")
            if isinstance(t, (int, float)):
                return int(t)
    return None

# ---------- fetch fixtures with stats ----------
def fetch_between(from_d: dt.date, to_d: dt.date) -> Optional[List[dict]]:
    """
    Try the /fixtures/between/{from}/{to} endpoint once.
    Returns list or None if unavailable/404.
    """
    path = f"fixtures/between/{dstr(from_d)}/{dstr(to_d)}"
    j = sm_get(path, {
        "include": "participants;statistics;statistics.player;league;state",
        "order": "asc",
        "page": 1
    }, treat_404_as_none=True)
    if not j:
        return None
    out = j.get("data", []) or []
    meta = j.get("meta") or {}
    last = meta.get("last_page", 1)
    for p in range(2, last + 1):
        jp = sm_get(path, {
            "include": "participants;statistics;statistics.player;league;state",
            "order": "asc",
            "page": p
        }, treat_404_as_none=True)
        if not jp:
            break
        out.extend(jp.get("data", []) or [])
    return out

def fetch_by_day(date_d: dt.date) -> List[dict]:
    """
    /fixtures/date/{yyyy-mm-dd}; returns [] when 404 (treated as none).
    """
    j = sm_get(f"fixtures/date/{dstr(date_d)}", {
        "include": "participants;statistics;statistics.player;league;state",
        "order": "asc",
        "page": 1
    }, treat_404_as_none=True)
    if not j:
        return []
    out = j.get("data", []) or []
    meta = j.get("meta") or {}
    last = meta.get("last_page", 1)
    for p in range(2, last + 1):
        jp = sm_get(f"fixtures/date/{dstr(date_d)}", {
            "include": "participants;statistics;statistics.player;league;state",
            "order": "asc",
            "page": p
        }, treat_404_as_none=True)
        if not jp:
            break
        out.extend(jp.get("data", []) or [])
    return out

# ---------- main aggregation ----------
def main():
    players, team_to_players, leagues = load_tracked()
    print(f"Leagues (from predicted_xi): {sorted(leagues)}")
    print(f"Tracked players: {len(players)}")

    # Build reverse maps
    league_set = set(leagues)
    team_set = set(team_to_players.keys())

    # Collect fixtures across a window; prefer BETWEEN in big chunks
    end = today_utc()
    start = end - dt.timedelta(days=BACK_DAYS)

    all_fixtures: List[dict] = []
    used_between = False

    # Try chunked between (3 chunks) to reduce calls
    between_ok = True
    chunks: List[Tuple[dt.date, dt.date]] = []
    span = (end - start).days
    if span <= 30:
        chunks = [(start, end)]
    else:
        # split into ~monthly chunks
        cur = start
        while cur <= end:
            nxt = min(cur + dt.timedelta(days=29), end)
            chunks.append((cur, nxt))
            cur = nxt + dt.timedelta(days=1)

    for (a, b) in chunks:
        got = fetch_between(a, b)
        if got is None:
            between_ok = False
            break
        all_fixtures.extend(got)

    if between_ok:
        used_between = True
    else:
        # Fallback: per-day but FAST — 404 => skip (no retries)
        print("[INFO] Falling back to /fixtures/date per-day (fast 404-skip).")
        consecutive_404s = 0
        for d in (start + dt.timedelta(days=i) for i in range((end - start).days + 1)):
            day_fixtures = fetch_by_day(d)
            if not day_fixtures:
                consecutive_404s += 1
                # if we hit a long off-period, don't waste time
                if consecutive_404s >= 10:
                    # skip ahead a few days to speed up
                    continue
            else:
                consecutive_404s = 0
                all_fixtures.extend(day_fixtures)

    # Filter to only leagues we care about and fixtures that involve our tracked teams
    filt: List[dict] = []
    for fx in all_fixtures:
        lid = fx.get("league_id")
        if lid not in league_set:
            continue
        parts = fx.get("participants") or []
        if not parts:
            continue
        team_ids = {p.get("id") for p in parts if p and p.get("id")}
        if not (team_ids & team_set):
            continue
        # keep
        filt.append(fx)

    # Sort oldest → newest (so we can pop last-10 naturally)
    def _ts(fx):
        ts = fx.get("starting_at") or ""
        return ts
    filt.sort(key=_ts)

    # Accumulators
    shots_by_player: Dict[int, List[Tuple[str, int]]] = {}  # pid -> list[(date, shots)]
    seen_in_fixture: Set[Tuple[int, int]] = set()  # (pid, fixture_id) to avoid dup

    def iter_player_stats(fx: dict):
        """
        Yield (player_id, shots) for all player-stat rows we can parse.
        """
        # some shapes: fx["statistics"] is a list where each element may contain
        # a "players" or "player" field, or be directly player rows.
        stats = fx.get("statistics") or []

        # If structure is dict-like (some APIs nest under "data")
        if isinstance(stats, dict):
            stats = stats.get("data") or []

        for row in stats:
            # Case 1: team-level rows containing nested player stats
            for key in ("players", "player", "statistics", "player_stats"):
                nested = row.get(key)
                if isinstance(nested, list):
                    for pr in nested:
                        pid = pr.get("player_id") or (pr.get("player") or {}).get("id")
                        if not pid:
                            continue
                        shots = extract_player_shots(pr)
                        if shots is not None:
                            yield int(pid), int(shots)
                elif isinstance(nested, dict):
                    pid = nested.get("player_id") or (nested.get("player") or {}).get("id")
                    if pid:
                        shots = extract_player_shots(nested)
                        if shots is not None:
                            yield int(pid), int(shots)

            # Case 2: row itself is a player-stat row
            pid = row.get("player_id") or (row.get("player") or {}).get("id")
            if pid:
                shots = extract_player_shots(row)
                if shots is not None:
                    yield int(pid), int(shots)

    # Walk fixtures (oldest → newest) and collect shots per tracked player
    for fx in filt:
        fid = int(fx.get("id") or 0)
        when = (fx.get("starting_at") or "").replace("T", " ").replace("Z", "")
        if not fid:
            continue

        per_fx: Dict[int, int] = {}
        for pid, shots in iter_player_stats(fx):
            if pid in players:
                per_fx[pid] = shots

        # Record once per player per fixture
        for pid, shots in per_fx.items():
            key = (pid, fid)
            if key in seen_in_fixture:
                continue
            seen_in_fixture.add(key)
            shots_by_player.setdefault(pid, []).append((when, shots))

    # Trim to last 10 (keep oldest→newest in output)
    for pid, seq in shots_by_player.items():
        if len(seq) > 10:
            # keep the last 10 (most recent), but still order oldest→newest
            seq[:] = seq[-10:]

    # Build per-league payloads
    out_root = "data/player_stats/shots"
    by_league_dir = os.path.join(out_root, "by_league")
    os.makedirs(by_league_dir, exist_ok=True)

    per_league: Dict[int, dict] = {}
    for pid, meta in players.items():
        lid = meta["league_id"]
        team_id = meta["team_id"]
        team_name = meta["team_name"]
        name = meta["name"]

        # sequence (oldest → newest)
        seq = shots_by_player.get(pid, [])
        seq_vals = [v for (_d, v) in seq]
        seq_dates = [d for (d, _v) in seq]

        per_league.setdefault(lid, {
            "utc_time": dt.datetime.now(dt.timezone.utc).isoformat(),
            "league_id": lid,
            "players": []
        })
        per_league[lid]["players"].append({
            "player_id": pid,
            "player_name": name,
            "team_id": team_id,
            "team_name": team_name,
            "last10_shots": seq_vals,
            "last10_dates": seq_dates,
        })

    # Write per-league JSON
    for lid, payload in per_league.items():
        # sort players by team then name for stable diffs
        payload["players"].sort(key=lambda x: (x["team_name"], x["player_name"], x["player_id"]))
        with open(os.path.join(by_league_dir, f"{lid}.json"), "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)

    # Summaries
    # Compact
    with open(os.path.join(out_root, "summary.txt"), "w", encoding="utf-8") as f:
        f.write(f"Time (UTC): {dt.datetime.now(dt.timezone.utc).isoformat()}\n")
        f.write(f"Used endpoint: {'between' if used_between else 'date'}\n")
        f.write(f"Leagues: {','.join(map(str, sorted(leagues)))}\n")
        f.write(f"Tracked players: {len(players)}\n")

    # Verbose: league → team → player "1,2,1,2,..."
    lines: List[str] = []
    lines.append(f"Time (UTC): {dt.datetime.now(dt.timezone.utc).isoformat()}")
    lines.append(f"Endpoint   : {'between' if used_between else 'date-per-day'}")
    lines.append("")
    # Group players by league->team
    by_lg_team: Dict[int, Dict[Tuple[int, str], List[dict]]] = {}
    for lid, payload in per_league.items():
        for p in payload["players"]:
            by_lg_team.setdefault(lid, {}).setdefault((p["team_id"], p["team_name"]), []).append(p)
    for lid in sorted(by_lg_team):
        lines.append(f"===== League {lid} =====")
        for (tid, tname) in sorted(by_lg_team[lid], key=lambda t: t[1]):
            lines.append(f"{tname} (Team {tid})")
            for p in sorted(by_lg_team[lid][(tid, tname)], key=lambda x: (x["player_name"], x["player_id"])):
                seq = p["last10_shots"]
                seq_s = ",".join(str(x) for x in seq) if seq else "(no data)"
                lines.append(f"  {p['player_name']} = {seq_s}")
            lines.append("")
    with open(os.path.join(out_root, "summary_verbose.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines).rstrip() + "\n")

    print("Done.")
    print(f"Wrote: {by_league_dir}/*.json, {out_root}/summary*.txt")

if __name__ == "__main__":
    try:
        main()
    except requests.HTTPError as e:
        print(f"[HTTPError] {e}")
        raise
