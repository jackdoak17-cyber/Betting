#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Last-10 LEAGUE shots per player from predicted XIs, using only fixture *listing* endpoints
(to avoid 404s from fixtures/{id} on older matches).

Inputs (already produced by your lineup job):
  data/predicted_xi/by_league/*.json
   - we read: league_id, team_id, team_name, player_id, player_name, role/position if present

Outputs:
  data/player_stats/shots/by_league/{league_id}.json
  data/player_stats/shots/summary.txt
  data/player_stats/shots/summary_verbose.txt

Rules:
- League games only (fixture.league_id == our league).
- Appearance counted if minutes >= 45.
- Shots = SHOTS_TOTAL, else (SHOTS_ON_TARGET + SHOTS_OFF_TARGET + SHOTS_BLOCKED/BLOCKED_SHOTS).
- Series is OLDEST → NEWEST, up to last 10.
- No calls to fixtures/{id}; we include player stats in the list endpoints.

Env:
  SPORTMONKS_TOKEN              (required)
  SHOTS_MONTHS_BACK  (int)      default 9  (~season span)
"""

import os
import json
import time
import datetime as dt
from typing import Dict, List, Optional, Tuple, Set

import requests

API_BASE = "https://api.sportmonks.com/v3/football"
TOKEN = os.getenv("SPORTMONKS_TOKEN")
if not TOKEN:
    raise SystemExit("ERROR: SPORTMONKS_TOKEN is not set.")

MONTHS_BACK = int(os.getenv("SHOTS_MONTHS_BACK", "9"))
TIMEOUT = 25
BACKOFF = 1.6
RETRIES_429 = 3
PACE = 0.12  # small spacing between requests

PRED_ROOT = "data/predicted_xi/by_league"
OUT_ROOT = "data/player_stats/shots"
BY_LG_DIR = os.path.join(OUT_ROOT, "by_league")

# -------- memo/cache --------
_MEMO: Dict[str, dict] = {}
_last_ts = 0.0

def _pace():
    global _last_ts
    now = time.time()
    if now - _last_ts < PACE:
        time.sleep(PACE - (now - _last_ts))
    _last_ts = time.time()

def api_get(path: str, params: Optional[dict] = None, ok404: bool = False) -> dict:
    if params is None:
        params = {}
    params = {**params, "api_token": TOKEN}
    url = f"{API_BASE}/{path.lstrip('/')}"
    key = url + "?" + "&".join(f"{k}={params[k]}" for k in sorted(params))
    if key in _MEMO:
        return _MEMO[key]

    last_exc = None
    for attempt in range(1, RETRIES_429 + 1):
        _pace()
        try:
            r = requests.get(url, params=params, timeout=TIMEOUT)
            if r.status_code == 404 and ok404:
                _MEMO[key] = {"data": [], "meta": {}}
                return _MEMO[key]
            if r.status_code == 429:
                sleep = min(60, (BACKOFF ** attempt) * 2.0)
                print(f"[429] {path} — sleeping {sleep:.1f}s")
                time.sleep(sleep)
                continue
            r.raise_for_status()
            j = r.json()
            _MEMO[key] = j
            return j
        except Exception as e:
            last_exc = e
            break
    if ok404:
        return {"data": [], "meta": {}}
    raise last_exc or RuntimeError(f"GET failed for {path}")

# ---------- helpers ----------
def today_utc() -> dt.date:
    return dt.datetime.now(dt.timezone.utc).date()

def dstr(d: dt.date) -> str:
    return d.strftime("%Y-%m-%d")

def ensure_dir(p: str):
    os.makedirs(p, exist_ok=True)

# ---------- load tracked players (from predicted_xi) ----------
def load_tracked() -> Tuple[Dict[int, dict], Dict[int, Set[int]], Set[int]]:
    """
    Returns:
      players: {player_id: {"name","team_id","team_name","league_id","role"}}
      team_to_players: {team_id: set(player_ids)}
      leagues: {league_ids}
    """
    if not os.path.isdir(PRED_ROOT):
        raise SystemExit("No predicted XIs found at data/predicted_xi/by_league/.")

    players: Dict[int, dict] = {}
    team_to_players: Dict[int, Set[int]] = {}
    leagues: Set[int] = set()

    for fn in os.listdir(PRED_ROOT):
        if not fn.endswith(".json"):
            continue
        path = os.path.join(PRED_ROOT, fn)
        try:
            blob = json.loads(open(path, "r", encoding="utf-8").read())
        except Exception:
            continue
        lid = int(blob.get("league_id") or fn.replace(".json", ""))
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
                    role = p.get("role") or p.get("position_label") or p.get("position") or ""
                    players[pid] = {
                        "name": (p.get("name") or p.get("player_name") or "").strip(),
                        "team_id": team_id,
                        "team_name": team_name,
                        "league_id": lid,
                        "role": role,
                    }
                    team_to_players.setdefault(team_id, set()).add(pid)

    return players, team_to_players, leagues

# ---------- fixture discovery with stats on the LIST endpoints ----------
LIST_INCLUDES = (
    "participants;"
    "league;"
    "state;"
    "lineups;lineups.player;lineups.details;lineups.details.type;"
    "statistics;statistics.player"
)

def fixtures_between(start_iso: str, end_iso: str) -> List[dict]:
    """Pull a window with all the stats we need on the listing call."""
    j = api_get(f"fixtures/between/{start_iso}/{end_iso}", {
        "include": LIST_INCLUDES,
        "order": "desc",
        "page": 1
    }, ok404=True)
    data = j.get("data") or []
    meta = j.get("meta") or {}
    last = int(meta.get("last_page") or 1)
    for p in range(2, last + 1):
        jp = api_get(f"fixtures/between/{start_iso}/{end_iso}", {
            "include": LIST_INCLUDES,
            "order": "desc",
            "page": p
        }, ok404=True)
        data.extend(jp.get("data") or [])
    return data

def fixtures_by_day(date_d: dt.date) -> List[dict]:
    j = api_get(f"fixtures/date/{dstr(date_d)}", {
        "include": LIST_INCLUDES,
        "order": "desc",
        "page": 1
    }, ok404=True)
    data = j.get("data") or []
    meta = j.get("meta") or {}
    last = int(meta.get("last_page") or 1)
    for p in range(2, last + 1):
        jp = api_get(f"fixtures/date/{dstr(date_d)}", {
            "include": LIST_INCLUDES,
            "order": "desc",
            "page": p
        }, ok404=True)
        data.extend(jp.get("data") or [])
    return data

def collect_league_team_fixtures(league_id: int, team_ids: Set[int]) -> List[dict]:
    """
    Gather past fixtures for our teams within this league, newest→oldest,
    then return **oldest→newest** after filtering to ones that actually carry
    lineups/statistics (played games).
    """
    out: Dict[int, dict] = {}
    end = today_utc()
    cursor_end = end

    for _ in range(MONTHS_BACK):
        start = cursor_end - dt.timedelta(days=31)
        start_iso = f"{dstr(start)} 00:00:00"
        end_iso   = f"{dstr(cursor_end)} 23:59:59"
        got = fixtures_between(start_iso, end_iso)
        if not got:
            # quick daily fallback (404 -> empty) with same includes
            d = start
            while d <= cursor_end:
                for fx in fixtures_by_day(d):
                    out[int(fx.get("id") or 0)] = fx
                d += dt.timedelta(days=1)
        else:
            for fx in got:
                out[int(fx.get("id") or 0)] = fx

        cursor_end = start - dt.timedelta(days=1)

    # filter: league + our teams + must have stats/lineups (played)
    res: List[dict] = []
    for fx in out.values():
        if int(fx.get("league_id") or 0) != league_id:
            continue
        parts = fx.get("participants") or []
        fx_team_ids = {int(p.get("id") or 0) for p in parts if p}
        if not (fx_team_ids & team_ids):
            continue

        has_stats = False
        # some data arrives as list[team_stat], each with players
        for st in (fx.get("statistics") or []):
            players = st.get("players") or st.get("player") or []
            if players:
                has_stats = True
                break
        # also allow if lineups with details are present (minutes often live here)
        if not has_stats and (fx.get("lineups") or []):
            has_stats = True

        if not has_stats:
            continue
        res.append(fx)

    # oldest → newest for building sequences
    res.sort(key=lambda x: (x.get("starting_at") or "", x.get("id") or 0))
    return res

# ---------- parsing helpers (from *listing* payloads) ----------
SHOT_DEV_TOTAL = {"SHOTS", "SHOTS_TOTAL"}
SHOT_DEV_SOT   = {"SHOTS_ON_TARGET"}
SHOT_DEV_SOFF  = {"SHOTS_OFF_TARGET"}
SHOT_DEV_BLK   = {"SHOTS_BLOCKED", "BLOCKED_SHOTS"}
MINUTES_DEVS   = {"MINUTES_PLAYED", "MINUTES"}
APPEARANCE_MIN = 45

def _intish(v) -> Optional[int]:
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, (int, float)):
        return int(v)
    if isinstance(v, dict):
        # common nested {"total": X}
        if "total" in v:
            return _intish(v.get("total"))
        # some types pack {on_target: x, off_target: y, ...}
        s = 0
        anyp = False
        for k in v:
            if isinstance(v[k], (int, float)):
                s += int(v[k])
                anyp = True
        return s if anyp else None
    try:
        return int(str(v).strip())
    except Exception:
        return None

def shots_from_lineup_details(details: List[dict]) -> Tuple[Optional[int], Optional[int]]:
    """Return (shots_total, minutes) parsed from lineup.details on the listing payload."""
    total = None
    sot = soff = blk = 0
    mins = None
    for det in (details or []):
        t = det.get("type") or {}
        dev = (t.get("developer_name") or t.get("code") or "").upper()
        val = _intish((det.get("data") or {}).get("value"))
        if dev in SHOT_DEV_TOTAL and val is not None:
            total = val
        elif dev in SHOT_DEV_SOT and val is not None:
            sot += val
        elif dev in SHOT_DEV_SOFF and val is not None:
            soff += val
        elif dev in SHOT_DEV_BLK and val is not None:
            blk += val
        elif dev in MINUTES_DEVS and val is not None:
            mins = val if mins is None else max(mins, val)
    if total is None and (sot or soff or blk):
        total = sot + soff + blk
    return total, mins

def shots_minutes_from_listing_fixture(fx: dict) -> Tuple[Dict[int, int], Dict[int, int]]:
    """
    From the LIST fixture (with includes), build:
      shots_map  {player_id: total_shots}
      minutes_map{player_id: minutes}
    """
    shots_map: Dict[int, int] = {}
    minutes_map: Dict[int, int] = {}

    # A) from lineups.details
    for lp in (fx.get("lineups") or []):
        pid = lp.get("player_id")
        if not pid:
            continue
        pid = int(pid)
        total, mins = shots_from_lineup_details(lp.get("details") or [])
        if mins is not None:
            minutes_map[pid] = max(minutes_map.get(pid, 0), mins)
        if total is not None:
            shots_map[pid] = total

    # B) from statistics.player (fallback / complement)
    for st in (fx.get("statistics") or []):
        players = st.get("players") or st.get("player") or []
        if isinstance(players, dict):
            players = [players]
        if not isinstance(players, list):
            continue
        for pr in players:
            pid = pr.get("player_id") or (pr.get("player") or {}).get("id")
            if not pid:
                continue
            pid = int(pid)

            # shots_total or nested dicts
            val = None
            for k in ("shots_total", "total_shots", "shots"):
                if k in pr:
                    val = _intish(pr.get(k))
                    if val is not None:
                        break
                    v = pr.get(k)
                    if isinstance(v, dict) and "total" in v:
                        val = _intish(v.get("total"))
                        break

            # “shots” object with on/off/blocked parts
            if val is None:
                shots_obj = pr.get("shots")
                if isinstance(shots_obj, dict):
                    cnt = 0
                    anyp = False
                    for kk in ("on_target", "off_target", "blocked", "blocked_shots", "shots_blocked"):
                        iv = _intish(shots_obj.get(kk))
                        if iv is not None:
                            cnt += iv
                            anyp = True
                    if anyp:
                        val = cnt

            if val is not None:
                shots_map[pid] = val

            # minutes sometimes surface as a stat as well (rare on this include)
            for k in ("minutes", "minutes_played"):
                mv = _intish(pr.get(k))
                if mv is not None:
                    minutes_map[pid] = max(minutes_map.get(pid, 0), mv)

    return shots_map, minutes_map

# ---------- build last-10 per team ----------
def team_series_for_players(fixtures: List[dict], player_ids: List[int]) -> Dict[int, List[int]]:
    """
    fixtures: list for this team & league, **oldest → newest**
    return pid -> [shots per qualifying appearance], oldest → newest, up to 10
    """
    series: Dict[int, List[int]] = {pid: [] for pid in player_ids}
    for fx in fixtures:
        shots_map, minutes_map = shots_minutes_from_listing_fixture(fx)
        for pid in player_ids:
            if len(series[pid]) >= 10:
                continue
            mins = minutes_map.get(pid)
            if mins is None or mins < APPEARANCE_MIN:
                continue  # not a qualifying appearance
            shots = shots_map.get(pid, 0)  # appeared ≥45' but no stat → 0
            series[pid].append(shots)
    # cap to last-10 (they're already oldest→newest)
    for pid in series:
        if len(series[pid]) > 10:
            series[pid] = series[pid][-10:]
    return series

# ---------- main ----------
def main():
    players, team_to_players, leagues = load_tracked()
    print(f"Leagues (from predicted_xi): {sorted(leagues)}")
    print(f"Tracked players (unique across leagues): {len(players)}\n")

    ensure_dir(BY_LG_DIR)
    ensure_dir(OUT_ROOT)

    # Pre-collect fixtures per league (listing calls only)
    league_fixtures: Dict[int, List[dict]] = {}
    for lid in sorted(leagues):
        our_teams = {tid for tid, pset in team_to_players.items()
                     if any(players[pid]["league_id"] == lid for pid in pset)}
        fxs = collect_league_team_fixtures(lid, our_teams)
        league_fixtures[lid] = fxs

    # Build outputs
    now_iso = dt.datetime.now(dt.timezone.utc).isoformat()
    verbose_lines: List[str] = [
        f"Time (UTC): {now_iso}",
        "Endpoint   : fixtures/between + fixtures/date (listing only, with statistics+lineups included)",
        "Metric     : Total shots per league match (shots_total OR on+off+blocked)",
        "Appearances: >=45 minutes",
        "Order      : oldest → newest",
        ""
    ]

    for lid in sorted(leagues):
        fxs = league_fixtures.get(lid, [])
        # Map team -> fixtures for faster grouping (still oldest→newest)
        team_to_fxs: Dict[int, List[dict]] = {}
        for fx in fxs:
            for p in (fx.get("participants") or []):
                tid = int(p.get("id") or 0)
                team_to_fxs.setdefault(tid, []).append(fx)

        rows: List[dict] = []
        for tid, pid_set in sorted(team_to_players.items()):
            # only players of this league
            pids_here = [pid for pid in pid_set if players[pid]["league_id"] == lid]
            if not pids_here:
                continue
            team_fxs = team_to_fxs.get(tid, [])
            if not team_fxs:
                continue
            series_map = team_series_for_players(team_fxs, pids_here)
            for pid in sorted(pids_here, key=lambda x: (players[x]["name"] or "").lower()):
                meta = players[pid]
                rows.append({
                    "player_id": pid,
                    "player_name": meta["name"],
                    "role": meta.get("role") or "",
                    "team_id": meta["team_id"],
                    "team_name": meta["team_name"],
                    "last10_shots": series_map.get(pid, []),
                })

        rows.sort(key=lambda r: ((r["team_name"] or "").lower(), (r["player_name"] or "").lower(), r["player_id"]))

        payload = {
            "utc_time": now_iso,
            "league_id": lid,
            "players": rows,
        }
        with open(os.path.join(BY_LG_DIR, f"{lid}.json"), "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)

        verbose_lines.append(f"===== League {lid} =====")
        last_team = None
        for r in rows:
            if r["team_name"] != last_team:
                last_team = r["team_name"]
                verbose_lines.append(f"{last_team} (Team {r['team_id']})")
            seq = r["last10_shots"]
            seq_s = ",".join(str(x) for x in seq) if seq else "(no data)"
            role = f" [{r['role']}]" if r.get("role") else ""
            verbose_lines.append(f"  {r['player_name']}{role} = {seq_s}")
        verbose_lines.append("")

    with open(os.path.join(OUT_ROOT, "summary.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join([
            f"Time (UTC): {now_iso}",
            "Endpoint   : listing-only (between/daily) with statistics+lineups included",
            "Metric     : Total shots (shots_total OR on+off+blocked)",
            "Appearances: >=45 minutes",
            "Order      : oldest → newest",
            ""
        ]))

    with open(os.path.join(OUT_ROOT, "summary_verbose.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(verbose_lines).rstrip() + "\n")

    print("Done.")
    print(f"Wrote: {BY_LG_DIR}/*.json and {OUT_ROOT}/summary*.txt")

if __name__ == "__main__":
    try:
        main()
    except requests.HTTPError as e:
        print(f"[HTTPError] {e}")
        raise
