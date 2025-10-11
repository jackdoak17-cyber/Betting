#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Gather last-10 league shots series (≥45' apps) for all players referenced in
data/predicted_xi/by_league/*.json, and output per-league JSON + human summary.

Key points
- No more slow date-per-day retries: try fixtures/between (monthly chunks); if 404,
  fall back to day-scan but **treat 404 as empty** (no retries).
- Per-fixture stats come from fixtures/{id}?include=lineups.details.type
  We derive minutes and total shots per player.
- Series definition: newest→oldest, last up to **10 league appearances** with ≥45',
  across this + last season.
- Output:
    data/stats/shots/by_league/<league_id>.json
    data/stats/shots/summary.txt
    data/stats/shots/summary_verbose.txt   (team blocks with lines like: 1,2,0,1,3,...)

Requires env:
  SPORTMONKS_TOKEN   (GitHub Secret in workflow)
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

# **** Tuning ****
APPEARANCE_MINUTES_THRESHOLD = 45
LAST_N = 10
MONTHS_BACK = 9            # ~ 9 months back should easily cover 10 league apps
GLOBAL_MIN_DELAY = 0.15    # tiny pacing between HTTP GETs
TIMEOUT = 25
RETRIES_429 = 3
BACKOFF = 1.6

# Output roots
PRED_ROOT = "data/predicted_xi/by_league"
OUT_ROOT  = "data/stats/shots"
BY_LG_DIR = os.path.join(OUT_ROOT, "by_league")

# Shot/minutes keys in lineups.details.type
SHOT_DEVS_TOTAL = {"SHOTS", "SHOTS_TOTAL"}
SHOT_DEVS_SOT   = {"SHOTS_ON_TARGET"}
SHOT_DEVS_SOFF  = {"SHOTS_OFF_TARGET"}
MINUTES_DEVS    = {"MINUTES_PLAYED", "MINUTES"}

# ---------- tiny memo (in-run) ----------
_MEMO: Dict[str, dict] = {}
_last_ts = 0.0

def _pace():
    global _last_ts
    now = time.time()
    if now - _last_ts < GLOBAL_MIN_DELAY:
        time.sleep(GLOBAL_MIN_DELAY - (now - _last_ts))
    _last_ts = time.time()

def api_get(path: str, params: Optional[dict] = None, ok404: bool = False) -> dict:
    """GET with small memo; treat 404 as empty when ok404=True; only retry 429."""
    if params is None:
        params = {}
    params = {**params, "api_token": TOKEN}
    url = f"{API_BASE}/{path.lstrip('/')}"
    key = url + "?" + "&".join(f"{k}={params[k]}" for k in sorted(params))
    hit = _MEMO.get(key)
    if hit is not None:
        return hit

    last_exc = None
    for attempt in range(1, RETRIES_429 + 1):
        _pace()
        try:
            r = requests.get(url, params=params, timeout=TIMEOUT)
            if r.status_code == 404 and ok404:
                _MEMO[key] = {"data": []}
                return _MEMO[key]
            if r.status_code == 429:
                sleep = min(60, (BACKOFF ** attempt) * 2.0)
                time.sleep(sleep)
                continue
            r.raise_for_status()
            j = r.json()
            _MEMO[key] = j
            return j
        except Exception as e:
            last_exc = e
            if isinstance(e, requests.HTTPError):
                # non-429 errors: don't retry (unless ok404 handled above)
                break
            if attempt < RETRIES_429:
                time.sleep(BACKOFF ** attempt)
            else:
                break
    if ok404:
        return {"data": []}
    raise last_exc or RuntimeError("api_get failed")

# ---------- helpers ----------
def ensure_dir(p: str):
    os.makedirs(p, exist_ok=True)

def today_utc() -> dt.date:
    return dt.datetime.now(dt.timezone.utc).date()

def dstr(d: dt.date) -> str:
    return d.strftime("%Y-%m-%d")

def read_predicted_players() -> Dict[int, List[dict]]:
    """
    Read all players from predicted_xi/by_league/*.json
    Return: {league_id: [ {player_id, name, team_id, team_name}, ... ] } (deduped)
    """
    leagues: Dict[int, Dict[int, dict]] = {}  # league -> pid -> row
    if not os.path.isdir(PRED_ROOT):
        return {}

    for fn in os.listdir(PRED_ROOT):
        if not fn.endswith(".json"):
            continue
        path = os.path.join(PRED_ROOT, fn)
        try:
            blob = json.loads(open(path, "r", encoding="utf-8").read())
        except Exception:
            continue
        lid = int(blob.get("league_id") or fn.split(".")[0])
        arr = blob.get("fixtures") or []
        if lid not in leagues:
            leagues[lid] = {}
        pid_map = leagues[lid]

        for r in arr:
            for side in ("home","away"):
                team = r.get(side) or {}
                team_id = team.get("team_id")
                team_name = team.get("name") or ""
                for p in (team.get("predicted_xi") or []):
                    try:
                        pid = int(p.get("player_id"))
                    except Exception:
                        continue
                    name = (p.get("name") or p.get("player_name") or "").strip()
                    if pid not in pid_map:
                        pid_map[pid] = {
                            "player_id": pid,
                            "name": name,
                            "team_id": int(team_id),
                            "team_name": team_name,
                        }
    # flatten
    out: Dict[int, List[dict]] = {}
    for lid, m in leagues.items():
        out[lid] = list(m.values())
    return out

# ---------- fixtures discovery (league windows) ----------
_league_window_cache: Dict[Tuple[int, str, str], List[dict]] = {}

def fixtures_between_league(league_id: int, start_iso: str, end_iso: str) -> List[dict]:
    """
    Try fixtures/between/{start}/{end}?include=participants;state;league
    If 404, return [] (caller will optionally fall back to day scan).
    """
    key = (league_id, start_iso, end_iso)
    hit = _league_window_cache.get(key)
    if hit is not None:
        return hit

    j = api_get(f"fixtures/between/{start_iso}/{end_iso}", {
        "include": "participants;state;league",
        "order": "desc"
    }, ok404=True)
    data = j.get("data") or []
    data = [fx for fx in data if int(fx.get("league_id") or 0) == league_id]
    _league_window_cache[key] = data
    return data

def fixtures_between_fallback_daily(league_id: int, start_date: dt.date, end_date: dt.date) -> List[dict]:
    """Fallback: scan per-day quickly (404 -> empty, no retries)."""
    res: List[dict] = []
    d = start_date
    while d <= end_date:
        j = api_get(f"fixtures/date/{dstr(d)}", {
            "include": "participants;state;league",
            "order": "desc",
            "page": 1
        }, ok404=True)
        res.extend([fx for fx in (j.get("data") or []) if int(fx.get("league_id") or 0) == league_id])
        d += dt.timedelta(days=1)
    return res

def league_fixtures_windows(league_id: int, months_back: int) -> List[dict]:
    """
    Collect league fixtures in ~1-month chunks (newest→oldest).
    Use 'between' if available; otherwise day-scan fallback per chunk.
    """
    out: Dict[int, dict] = {}  # fid -> fx
    today = today_utc()
    # windows like [today-0m, today], [today-1m, today-0m), ...
    cursor_end = today
    for _ in range(months_back):
        cursor_start = cursor_end - dt.timedelta(days=31)
        start_iso = f"{dstr(cursor_start)} 00:00:00"
        end_iso   = f"{dstr(cursor_end)} 23:59:59"
        # try between
        chunk = fixtures_between_league(league_id, start_iso, end_iso)
        if not chunk:
            # gentle fallback
            chunk = fixtures_between_fallback_daily(league_id, cursor_start, cursor_end)
        for fx in chunk:
            fid = int(fx.get("id"))
            out[fid] = fx
        cursor_end = cursor_start - dt.timedelta(days=1)
    # newest first
    return sorted(out.values(), key=lambda x: (x.get("starting_at") or "", x.get("id")), reverse=True)

# ---------- per-fixture minutes + shots ----------
_fixture_lineups_cache: Dict[int, Tuple[Dict[int, int], Dict[int, int]]] = {}
# maps: fixture_id -> (minutes_map{pid:mins}, shots_map{pid:shots})

def minutes_and_shots_for_fixture(fid: int) -> Tuple[Dict[int, int], Dict[int, int]]:
    hit = _fixture_lineups_cache.get(fid)
    if hit is not None:
        return hit
    j = api_get(f"fixtures/{fid}", {"include": "lineups.details.type"})
    data = j.get("data") or {}
    minutes_map: Dict[int, int] = {}
    shots_map: Dict[int, int] = {}
    for lp in (data.get("lineups") or []):
        pid = lp.get("player_id")
        if not pid:
            continue
        pid = int(pid)
        total_from_api: Optional[int] = None
        sot = soff = 0
        mins: Optional[int] = None
        for det in (lp.get("details") or []):
            t = det.get("type") or {}
            dev = (t.get("developer_name") or "").upper()
            # minutes
            if dev in MINUTES_DEVS:
                v = (det.get("data") or {}).get("value")
                try:
                    v = int(v if not isinstance(v, dict) else v.get("total", 0))
                except Exception:
                    v = 0
                mins = max(mins or 0, v)
            # shots
            elif dev in SHOT_DEVS_TOTAL:
                v = (det.get("data") or {}).get("value")
                if isinstance(v, dict):
                    v = v.get("total", 0)
                try:
                    total_from_api = int(v or 0)
                except Exception:
                    total_from_api = 0
            elif dev in SHOT_DEVS_SOT:
                v = (det.get("data") or {}).get("value")
                if isinstance(v, dict): v = v.get("total", 0)
                try: sot += int(v or 0)
                except Exception: pass
            elif dev in SHOT_DEVS_SOFF:
                v = (det.get("data") or {}).get("value")
                if isinstance(v, dict): v = v.get("total", 0)
                try: soff += int(v or 0)
                except Exception: pass
        if mins is not None:
            minutes_map[pid] = mins
        if total_from_api is not None:
            shots_map[pid] = total_from_api
        else:
            tot = sot + soff
            if tot > 0:
                shots_map[pid] = tot
    _fixture_lineups_cache[fid] = (minutes_map, shots_map)
    return minutes_map, shots_map

# ---------- build last-10 series per player ----------
def team_series_for_players(league_id: int, team_id: int, player_ids: List[int]) -> Dict[int, List[int]]:
    """
    Build last-10 series for given players of a team within league fixtures.
    """
    # collect league fixtures, newest→oldest
    league_fxs = league_fixtures_windows(league_id, MONTHS_BACK)
    # keep only fixtures containing this team
    team_fxs = [fx for fx in league_fxs if any(int(p.get("id") or 0) == team_id for p in (fx.get("participants") or []))]
    # newest first → iterate
    series: Dict[int, List[int]] = {pid: [] for pid in player_ids}
    if not team_fxs:
        return series

    for fx in team_fxs:
        if all(len(series[pid]) >= LAST_N for pid in series):
            break
        fid = int(fx.get("id"))
        mins_map, shots_map = minutes_and_shots_for_fixture(fid)
        for pid in player_ids:
            if len(series[pid]) >= LAST_N:
                continue
            mins = mins_map.get(pid)
            if mins is None or mins < APPEARANCE_MINUTES_THRESHOLD:
                continue
            shots = shots_map.get(pid, 0)
            series[pid].append(shots)

    # series are newest→oldest; we want that order in output; pad nothing
    return series

# ---------- main ----------
def main():
    players_by_league = read_predicted_players()
    if not players_by_league:
        print("No predicted_xi found under data/predicted_xi/by_league/.")
        return

    ensure_dir(BY_LG_DIR)
    ensure_dir(OUT_ROOT)

    now_iso = dt.datetime.now(dt.timezone.utc).isoformat()

    # For summary files
    verbose_lines: List[str] = [f"Time (UTC): {now_iso}", "Metric    : Player total shots per league appearance (>=45')", ""]
    summary_counts: Dict[int, int] = {}

    for lid in sorted(players_by_league):
        plist = players_by_league[lid]
        # group by team for efficient reuse
        by_team: Dict[int, List[dict]] = {}
        for p in plist:
            by_team.setdefault(int(p["team_id"]), []).append(p)

        # compute series team-by-team
        result_rows: List[dict] = []
        for team_id, rows in by_team.items():
            pid_list = [int(r["player_id"]) for r in rows]
            series_map = team_series_for_players(lid, team_id, pid_list)
            # pack output rows
            for r in rows:
                pid = int(r["player_id"])
                s   = series_map.get(pid, [])
                result_rows.append({
                    "player_id": pid,
                    "player_name": r["name"],
                    "team_id": team_id,
                    "team_name": r["team_name"],
                    "series": s,                 # newest→oldest, up to 10
                    "apps": len(s),
                })

        # write per-league JSON
        out_payload = {
            "utc_time": now_iso,
            "league_id": lid,
            "players": sorted(result_rows, key=lambda x: (x["team_name"], x["player_name"])),
        }
        with open(os.path.join(BY_LG_DIR, f"{lid}.json"), "w", encoding="utf-8") as f:
            json.dump(out_payload, f, ensure_ascii=False)

        # add to summaries
        summary_counts[lid] = len(result_rows)
        verbose_lines.append(f"===== League {lid} =====")
        # pretty: team blocks with "1,2,1,2,3,0,1,2,3,2"
        cur_team = None
        for row in out_payload["players"]:
            if row["team_name"] != cur_team:
                cur_team = row["team_name"]
                verbose_lines.append(f"{cur_team} (Team {row['team_id']})")
            series_txt = ",".join(str(v) for v in row["series"]) if row["series"] else "(no data)"
            verbose_lines.append(f"  {row['player_name']} = {series_txt}")
        verbose_lines.append("")

    # compact summary.txt
    with open(os.path.join(OUT_ROOT, "summary.txt"), "w", encoding="utf-8") as f:
        f.write(f"Time (UTC): {now_iso}\n")
        f.write("Endpoint   : between-monthly (fallback to daily no-retry on 404)\n\n")
        for lid in sorted(summary_counts):
            f.write(f"League {lid}: {summary_counts[lid]} players\n")

    # verbose file
    with open(os.path.join(OUT_ROOT, "summary_verbose.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(verbose_lines).rstrip() + "\n")

    print("Done.")
    print(f"Wrote per-league shots JSON to {BY_LG_DIR}/ and summaries under {OUT_ROOT}/")

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
