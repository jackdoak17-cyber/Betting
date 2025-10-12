#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Gather last-10 league shots series (≥45' apps) for all players referenced in
data/predicted_xi/by_league/*.json, and output per-league JSON + human summaries.

Key points
- Uses fixtures/{id}?include=lineups.details with filters=lineupDetailTypes:<IDs>.
- Type IDs are loaded from data/refs/type_ids.json; if missing, fetched automatically
  from /v3/core/types and cached to that file.
- Series: newest→oldest, last up to 10 league appearances with ≥45', across this+last season.
- Output:
    data/stats/shots/by_league/<league_id>.json
    data/stats/shots/summary.txt
    data/stats/shots/summary_verbose.txt

Env:
  SPORTMONKS_TOKEN   (required)
"""

import os
import json
import time
import datetime as dt
from typing import Dict, List, Optional, Tuple

import requests

# ---------------- Config ----------------
API_BASE_FOOTBALL = "https://api.sportmonks.com/v3/football"
API_BASE_CORE     = "https://api.sportmonks.com/v3/core"
TOKEN = os.getenv("SPORTMONKS_TOKEN")
if not TOKEN:
    raise SystemExit("ERROR: SPORTMONKS_TOKEN is not set.")

APPEARANCE_MINUTES_THRESHOLD = 45
LAST_N = 10
MONTHS_BACK = 9
GLOBAL_MIN_DELAY = 0.15
TIMEOUT = 25
RETRIES_429 = 3
BACKOFF = 1.6

# Paths
PRED_ROOT     = "data/predicted_xi/by_league"
OUT_ROOT      = "data/stats/shots"
BY_LG_DIR     = os.path.join(OUT_ROOT, "by_league")
REFS_DIR      = "data/refs"
TYPE_MAP_PATH = os.path.join(REFS_DIR, "type_ids.json")

# ---------------- Runtime memo/pacing ----------------
_MEMO: Dict[str, dict] = {}
_last_ts = 0.0

def _pace():
    global _last_ts
    now = time.time()
    if now - _last_ts < GLOBAL_MIN_DELAY:
        time.sleep(GLOBAL_MIN_DELAY - (now - _last_ts))
    _last_ts = time.time()

def api_get(url: str, params: Optional[dict] = None, ok404: bool = False) -> dict:
    if params is None:
        params = {}
    params = {**params, "api_token": TOKEN}
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
            if isinstance(e, requests.HTTPError) and r.status_code != 429:
                break
            if attempt < RETRIES_429:
                time.sleep(BACKOFF ** attempt)
            else:
                break
    if ok404:
        return {"data": []}
    raise last_exc or RuntimeError("api_get failed")

def api_get_fb(path: str, params: Optional[dict] = None, ok404: bool = False) -> dict:
    return api_get(f"{API_BASE_FOOTBALL}/{path.lstrip('/')}", params, ok404)

def api_get_core(path: str, params: Optional[dict] = None) -> dict:
    return api_get(f"{API_BASE_CORE}/{path.lstrip('/')}", params, ok404=False)

# ---------------- Utilities ----------------
def ensure_dir(p: str):
    os.makedirs(p, exist_ok=True)

def today_utc() -> dt.date:
    return dt.datetime.now(dt.timezone.utc).date()

def dstr(d: dt.date) -> str:
    return d.strftime("%Y-%m-%d")

def _num(value):
    """Coerce Sportmonks .data.value or dict payloads to int."""
    v = value
    if isinstance(v, dict):
        # common shapes: {"value": X} or {"total": X} or nested bits
        if "value" in v:
            v = v["value"]
        elif "total" in v:
            v = v["total"]
        else:
            s = 0
            for x in v.values():
                try:
                    s += int(round(float(x)))
                except Exception:
                    pass
            return s
    try:
        return int(round(float(v)))
    except Exception:
        return 0

# ---------------- Types (IDs) ----------------
def fetch_all_types_statistic() -> List[dict]:
    """Page through /v3/core/types and return all model_type=='statistic'."""
    out: List[dict] = []
    page = 1
    per_page = 50
    while True:
        j = api_get_core("types", {"per_page": per_page, "page": page})
        data = j.get("data") or []
        out.extend([t for t in data if (t.get("model_type") == "statistic")])
        meta = j.get("meta") or {}
        has_more = meta.get("has_more")
        # stop if explicit has_more says false, else stop on short page
        if has_more is False or len(data) < per_page or page >= 200:
            break
        page += 1
    return out

def build_type_map(types: List[dict]) -> dict:
    """Return required developer_name -> id mapping (or None if missing)."""
    def find_id(*devnames: str) -> Optional[int]:
        for dn in devnames:
            for t in types:
                if t.get("developer_name") == dn:
                    return t.get("id")
        return None

    shots_total   = find_id("SHOTS", "SHOTS_TOTAL")
    shots_on      = find_id("SHOTS_ON_TARGET", "SHOTS_ON_GOAL")
    shots_off     = find_id("SHOTS_OFF_TARGET", "SHOTS_OFF_GOAL")
    blocked       = find_id("BLOCKED_SHOTS", "SHOTS_BLOCKED")
    minutes       = find_id("MINUTES_PLAYED", "MINUTES")

    want = [x for x in [shots_total, shots_on, shots_off, blocked, minutes] if x]
    return {
        "SHOTS": shots_total,
        "SHOTS_ON_TARGET": shots_on,
        "SHOTS_OFF_TARGET": shots_off,
        "BLOCKED_SHOTS": blocked,
        "MINUTES_PLAYED": minutes,
        "want_ids": want,
    }

def ensure_type_ids() -> dict:
    """Load from file or fetch and cache."""
    ensure_dir(REFS_DIR)
    if os.path.isfile(TYPE_MAP_PATH):
        try:
            m = json.loads(open(TYPE_MAP_PATH, "r", encoding="utf-8").read())
            if isinstance(m, dict) and m.get("want_ids"):
                return m
        except Exception:
            pass
    types = fetch_all_types_statistic()
    m = build_type_map(types)
    with open(TYPE_MAP_PATH, "w", encoding="utf-8") as f:
        json.dump(m, f, ensure_ascii=False)
    return m

TYPE_IDS = ensure_type_ids()

# ---------------- Predicted players ----------------
def read_predicted_players() -> Dict[int, List[dict]]:
    """
    Read all players from predicted_xi/by_league/*.json
    Return: {league_id: [ {player_id, name, team_id, team_name}, ... ] } (deduped)
    """
    leagues: Dict[int, Dict[int, dict]] = {}
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
        try:
            lid = int(blob.get("league_id") or fn.split(".")[0])
        except Exception:
            continue

        arr = blob.get("fixtures") or []
        if lid not in leagues:
            leagues[lid] = {}
        pid_map = leagues[lid]

        for r in arr:
            for side in ("home", "away"):
                team = r.get(side) or {}
                team_id = team.get("team_id")
                team_name = team.get("name") or ""
                for p in (team.get("predicted_xi") or []):
                    try:
                        pid = int(p.get("player_id"))
                        tid = int(team_id)
                    except Exception:
                        continue
                    name = (p.get("name") or p.get("player_name") or "").strip()
                    if pid not in pid_map:
                        pid_map[pid] = {
                            "player_id": pid,
                            "name": name,
                            "team_id": tid,
                            "team_name": team_name,
                        }

    out: Dict[int, List[dict]] = {}
    for lid, m in leagues.items():
        out[lid] = list(m.values())
    return out

# ---------------- Fixture discovery ----------------
_league_window_cache: Dict[Tuple[int, str, str], List[dict]] = {}

def fixtures_between_league(league_id: int, start_iso: str, end_iso: str) -> List[dict]:
    key = (league_id, start_iso, end_iso)
    hit = _league_window_cache.get(key)
    if hit is not None:
        return hit

    j = api_get_fb(f"fixtures/between/{start_iso}/{end_iso}",
                   {"include": "participants;state;league", "order": "desc"},
                   ok404=True)
    data = j.get("data") or []
    data = [fx for fx in data if int(fx.get("league_id") or 0) == league_id]
    _league_window_cache[key] = data
    return data

def fixtures_between_fallback_daily(league_id: int, start_date: dt.date, end_date: dt.date) -> List[dict]:
    res: List[dict] = []
    d = start_date
    while d <= end_date:
        j = api_get_fb(f"fixtures/date/{dstr(d)}",
                       {"include": "participants;state;league", "order": "desc", "page": 1},
                       ok404=True)
        res.extend([fx for fx in (j.get("data") or []) if int(fx.get("league_id") or 0) == league_id])
        d += dt.timedelta(days=1)
    return res

def league_fixtures_windows(league_id: int, months_back: int) -> List[dict]:
    out: Dict[int, dict] = {}
    today = today_utc()
    cursor_end = today
    for _ in range(months_back):
        cursor_start = cursor_end - dt.timedelta(days=31)
        start_iso = f"{dstr(cursor_start)} 00:00:00"
        end_iso   = f"{dstr(cursor_end)} 23:59:59"
        chunk = fixtures_between_league(league_id, start_iso, end_iso)
        if not chunk:
            chunk = fixtures_between_fallback_daily(league_id, cursor_start, cursor_end)
        for fx in chunk:
            try:
                fid = int(fx.get("id"))
                out[fid] = fx
            except Exception:
                pass
        cursor_end = cursor_start - dt.timedelta(days=1)
    return sorted(out.values(), key=lambda x: (x.get("starting_at") or "", x.get("id")), reverse=True)

# ---------------- Per-fixture player minutes + shots ----------------
_fixture_lineups_cache: Dict[int, Tuple[Dict[int, int], Dict[int, int]]] = {}
# cache: fid -> (minutes_map{pid:int}, shots_map{pid:int})

def minutes_and_shots_for_fixture(fid: int) -> Tuple[Dict[int, int], Dict[int, int]]:
    hit = _fixture_lineups_cache.get(fid)
    if hit is not None:
        return hit

    # only the detail types we need
    filt_ids = ",".join(str(x) for x in TYPE_IDS["want_ids"])
    j = api_get_fb(f"fixtures/{fid}",
                   {"include": "lineups.details",
                    "filters": f"lineupDetailTypes:{filt_ids}"})
    data = j.get("data") or {}

    minutes_map: Dict[int, int] = {}
    shots_map: Dict[int, int]   = {}

    for lp in (data.get("lineups") or []):
        pid = lp.get("player_id")
        if not pid:
            continue
        pid = int(pid)

        mins = 0
        total_declared: Optional[int] = None
        sot = soff = blk = 0

        for det in (lp.get("details") or []):
            tid = det.get("type_id")
            if not tid:
                continue
            val = _num((det.get("data") or {}).get("value"))
            if TYPE_IDS.get("MINUTES_PLAYED") and tid == TYPE_IDS["MINUTES_PLAYED"]:
                mins = max(mins, val)
            elif TYPE_IDS.get("SHOTS") and tid == TYPE_IDS["SHOTS"]:
                total_declared = max(total_declared or 0, val)
            elif TYPE_IDS.get("SHOTS_ON_TARGET") and tid == TYPE_IDS["SHOTS_ON_TARGET"]:
                sot += val
            elif TYPE_IDS.get("SHOTS_OFF_TARGET") and tid == TYPE_IDS["SHOTS_OFF_TARGET"]:
                soff += val
            elif TYPE_IDS.get("BLOCKED_SHOTS") and tid == TYPE_IDS["BLOCKED_SHOTS"]:
                blk += val

        if mins:
            minutes_map[pid] = mins
        total = total_declared if total_declared is not None else (sot + soff + blk)
        # record even if zero (for >=45' appearances with no shot we want 0)
        if total_declared is not None or (sot + soff + blk) >= 0:
            shots_map[pid] = max(0, total)

    _fixture_lineups_cache[fid] = (minutes_map, shots_map)
    return minutes_map, shots_map

# ---------------- Build last-10 series per player ----------------
def team_series_for_players(league_id: int, team_id: int, player_ids: List[int]) -> Dict[int, List[int]]:
    league_fxs = league_fixtures_windows(league_id, MONTHS_BACK)
    team_fxs = [fx for fx in league_fxs if any(int(p.get("id") or 0) == team_id for p in (fx.get("participants") or []))]

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
            series[pid].append(int(shots))

    return series  # newest→oldest

# ---------------- Main ----------------
def main():
    players_by_league = read_predicted_players()
    if not players_by_league:
        print("No predicted_xi found under data/predicted_xi/by_league/.")
        return

    ensure_dir(BY_LG_DIR)
    ensure_dir(OUT_ROOT)

    now_iso = dt.datetime.now(dt.timezone.utc).isoformat()

    verbose_lines: List[str] = [f"Time (UTC): {now_iso}", "Metric    : Player total shots per league appearance (>=45')", ""]
    summary_counts: Dict[int, int] = {}

    for lid in sorted(players_by_league):
        plist = players_by_league[lid]
        # group by team
        by_team: Dict[int, List[dict]] = {}
        for p in plist:
            by_team.setdefault(int(p["team_id"]), []).append(p)

        result_rows: List[dict] = []
        for team_id, rows in by_team.items():
            pid_list = [int(r["player_id"]) for r in rows]
            series_map = team_series_for_players(lid, team_id, pid_list)
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

        out_payload = {
            "utc_time": now_iso,
            "league_id": lid,
            "players": sorted(result_rows, key=lambda x: (x["team_name"], x["player_name"] or "")),
        }
        with open(os.path.join(BY_LG_DIR, f"{lid}.json"), "w", encoding="utf-8") as f:
            json.dump(out_payload, f, ensure_ascii=False)

        summary_counts[lid] = len(result_rows)
        verbose_lines.append(f"===== League {lid} =====")
        cur_team = None
        for row in out_payload["players"]:
            if row["team_name"] != cur_team:
                cur_team = row["team_name"]
                verbose_lines.append(f"{cur_team} (Team {row['team_id']})")
            series_txt = ",".join(str(v) for v in row["series"]) if row["series"] else "(no data)"
            verbose_lines.append(f"  {row['player_name']} = {series_txt}")
        verbose_lines.append("")

    with open(os.path.join(OUT_ROOT, "summary.txt"), "w", encoding="utf-8") as f:
        f.write(f"Time (UTC): {now_iso}\n")
        f.write("Endpoint   : fixtures/between monthly (fallback to date scan); lineups.details filtered by type IDs\n\n")
        for lid in sorted(summary_counts):
            f.write(f"League {lid}: {summary_counts[lid]} players\n")

    with open(os.path.join(OUT_ROOT, "summary_verbose.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(verbose_lines).rstrip() + "\n")

    print("Done.")
    print(f"Wrote per-league shots JSON to {BY_LG_DIR}/ and summaries under {OUT_ROOT}/")

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
