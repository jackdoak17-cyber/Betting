#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Last-10 league shots per tracked player from predicted_xi.

Inputs:
  data/predicted_xi/by_league/*.json
    (we read league_id, team_id, team_name, player_id, player_name, position if present)

Outputs:
  data/player_stats/shots/by_league/{league_id}.json
  data/player_stats/shots/summary.txt
  data/player_stats/shots/summary_verbose.txt

Rules:
- League matches only (fixture.league_id == our league).
- Sequence is OLDEST → NEWEST.
- Total shots = SHOTS_TOTAL if present, else (SHOTS_ON_TARGET + SHOTS_OFF_TARGET + SHOTS_BLOCKED/BLOCKED_SHOTS).
- If a player appeared but no shot stat exists, record 0.
- Up to last 10 fixtures per player.

Env:
  SPORTMONKS_TOKEN  (required)
  SHOTS_MONTHS_BACK (optional, default 9)
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
PACE = 0.12  # tiny delay between requests

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
            # non-429: bail (unless ok404 handled above)
            break
    if ok404:
        return {"data": [], "meta": {}}
    raise last_exc or RuntimeError(f"GET failed for {path}")

def today_utc() -> dt.date:
    return dt.datetime.now(dt.timezone.utc).date()

def dstr(d: dt.date) -> str:
    return d.strftime("%Y-%m-%d")

# -------- input: predicted_xi --------
def load_tracked() -> Tuple[Dict[int, dict], Dict[int, Set[int]], Set[int]]:
    """
    Returns:
      players: {player_id: {"name","team_id","team_name","league_id","position"}}
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
                    pos = p.get("position") or p.get("pos") or p.get("role") or ""
                    players[pid] = {
                        "name": (p.get("name") or p.get("player_name") or "").strip(),
                        "team_id": team_id,
                        "team_name": team_name,
                        "league_id": lid,
                        "position": pos,
                    }
                    team_to_players.setdefault(team_id, set()).add(pid)
    return players, team_to_players, leagues

# -------- fixture discovery (league windows) --------
def fixtures_between(start_iso: str, end_iso: str) -> List[dict]:
    j = api_get(f"fixtures/between/{start_iso}/{end_iso}", {
        "include": "participants;league;state",
        "order": "desc",
        "page": 1
    }, ok404=True)
    data = j.get("data") or []
    meta = j.get("meta") or {}
    last = int(meta.get("last_page") or 1)
    for p in range(2, last + 1):
        jp = api_get(f"fixtures/between/{start_iso}/{end_iso}", {
            "include": "participants;league;state",
            "order": "desc",
            "page": p
        }, ok404=True)
        data.extend(jp.get("data") or [])
    return data

def fixtures_by_day(date_d: dt.date) -> List[dict]:
    j = api_get(f"fixtures/date/{dstr(date_d)}", {
        "include": "participants;league;state",
        "order": "desc",
        "page": 1
    }, ok404=True)
    data = j.get("data") or []
    meta = j.get("meta") or {}
    last = int(meta.get("last_page") or 1)
    for p in range(2, last + 1):
        jp = api_get(f"fixtures/date/{dstr(date_d)}", {
            "include": "participants;league;state",
            "order": "desc",
            "page": p
        }, ok404=True)
        data.extend(jp.get("data") or [])
    return data

def collect_league_team_fixtures(league_id: int, team_ids: Set[int]) -> List[dict]:
    """Newest → oldest scan in month chunks, with daily fallback. Then filter to our league+teams and sort oldest → newest."""
    out: Dict[int, dict] = {}
    end = today_utc()
    cursor_end = end
    for _ in range(MONTHS_BACK):
        start = cursor_end - dt.timedelta(days=31)
        start_iso = f"{dstr(start)} 00:00:00"
        end_iso   = f"{dstr(cursor_end)} 23:59:59"
        got = fixtures_between(start_iso, end_iso)
        if not got:
            # daily fallback
            d = start
            while d <= cursor_end:
                for fx in fixtures_by_day(d):
                    out[int(fx.get("id") or 0)] = fx
                d += dt.timedelta(days=1)
        else:
            for fx in got:
                out[int(fx.get("id") or 0)] = fx
        cursor_end = start - dt.timedelta(days=1)

    res = []
    for fx in out.values():
        if int(fx.get("league_id") or 0) != league_id:
            continue
        parts = fx.get("participants") or []
        fx_team_ids = {int(p.get("id") or 0) for p in parts if p}
        if not (fx_team_ids & team_ids):
            continue
        res.append(fx)

    res.sort(key=lambda x: (x.get("starting_at") or "", x.get("id") or 0))  # oldest → newest
    return res

# -------- parse per-fixture stats --------
SHOT_DEV_TOTAL = {"SHOTS", "SHOTS_TOTAL"}
SHOT_DEV_SOT   = {"SHOTS_ON_TARGET"}
SHOT_DEV_SOFF  = {"SHOTS_OFF_TARGET"}
SHOT_DEV_BLK   = {"SHOTS_BLOCKED", "BLOCKED_SHOTS"}

def _intish(v) -> Optional[int]:
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, (int, float)):
        return int(v)
    if isinstance(v, dict):
        t = v.get("total")
        return _intish(t)
    try:
        return int(str(v).strip())
    except Exception:
        return None

def _shots_from_details(details: List[dict]) -> Optional[int]:
    total = None
    sot = soff = blk = 0
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
    if total is not None:
        return total
    tot = sot + soff + blk
    return tot if (sot or soff or blk) else None

def _shots_from_flat_player_row(pr: dict) -> Optional[int]:
    for k in ("shots_total", "total_shots", "shots"):
        if k in pr:
            v = pr.get(k)
            iv = _intish(v)
            if iv is not None:
                return iv
            if isinstance(v, dict):
                iv = _intish(v.get("total"))
                if iv is not None:
                    return iv
    shots = pr.get("shots")
    if isinstance(shots, dict):
        x = 0
        any_part = False
        for kk in ("on_target", "off_target", "blocked", "blocked_shots", "shots_blocked"):
            iv = _intish(shots.get(kk))
            if iv is not None:
                x += iv
                any_part = True
        if any_part:
            return x
    details = pr.get("details")
    if isinstance(details, list):
        return _shots_from_details(details)
    return None

def fetch_fixture_full(fid: int) -> dict:
    # IMPORTANT: ok404=True so we skip non-existent/forbidden fixture details gracefully
    j = api_get(f"fixtures/{fid}", {
        "include": "participants;state;lineups.details.type;statistics;statistics.player",
    }, ok404=True)
    return j.get("data") or {}

def build_shots_for_fixture(fid: int) -> Tuple[Dict[int, int], Set[int]]:
    """
    Returns:
      shots_map {player_id: shots_int}
      appeared  set(player_ids)  (anyone we can say 'played' -> 0 fallback)
    """
    data = fetch_fixture_full(fid)
    if not data:
        print(f"[SKIP 404] fixture {fid} — no details available")
        return {}, set()

    shots_map: Dict[int, int] = {}
    appeared: Set[int] = set()

    # A) lineups.details.type
    for lp in (data.get("lineups") or []):
        pid = lp.get("player_id")
        if not pid:
            continue
        pid = int(pid)
        appeared.add(pid)  # listed in lineups → appeared
        details = lp.get("details") or []
        s = _shots_from_details(details)
        if s is not None:
            shots_map[pid] = s

    # B) statistics.player
    for statrow in (data.get("statistics") or []):
        players = statrow.get("players") or statrow.get("player") or []
        if isinstance(players, dict):
            players = [players]
        if not isinstance(players, list):
            continue
        for pr in players:
            pid = pr.get("player_id") or (pr.get("player") or {}).get("id")
            if not pid:
                continue
            pid = int(pid)
            appeared.add(pid)
            s = _shots_from_flat_player_row(pr)
            if s is not None:
                shots_map[pid] = s

    return shots_map, appeared

# -------- assemble last-10 per player --------
def main():
    players, team_to_players, leagues = load_tracked()
    print(f"Leagues (from predicted_xi): {sorted(leagues)}")
    print(f"Tracked players (unique across leagues): {len(players)}\n")

    os.makedirs(BY_LG_DIR, exist_ok=True)
    os.makedirs(OUT_ROOT, exist_ok=True)

    verbose_lines: List[str] = []
    verbose_lines.append(f"Time (UTC): {dt.datetime.now(dt.timezone.utc).isoformat()}")
    verbose_lines.append("Endpoint   : between-monthly (fallback to daily), lineups.details + statistics.player")
    verbose_lines.append("Metric     : Total shots per league match (shots_total OR on+off+blocked)")
    verbose_lines.append("Order      : oldest → newest\n")

    # pre-collect fixtures per league (filtered to our teams)
    league_fixtures: Dict[int, List[dict]] = {}
    for lid in sorted(leagues):
        our_teams = {tid for tid, pset in team_to_players.items()
                     if any(players[pid]["league_id"] == lid for pid in pset)}
        fxs = collect_league_team_fixtures(lid, our_teams)
        league_fixtures[lid] = fxs

    # per league output
    for lid in sorted(leagues):
        fxs = league_fixtures.get(lid, [])

        team_to_fids: Dict[int, List[int]] = {}
        for fx in fxs:
            fid = int(fx.get("id") or 0)
            for p in (fx.get("participants") or []):
                tid = int(p.get("id") or 0)
                team_to_fids.setdefault(tid, []).append(fid)

        fixture_cache: Dict[int, Tuple[Dict[int, int], Set[int]]] = {}

        out_rows: List[dict] = []
        for tid in sorted(team_to_players.keys()):
            pids_here = [pid for pid in team_to_players[tid] if players[pid]["league_id"] == lid]
            if not pids_here:
                continue
            fids = team_to_fids.get(tid, [])
            series: Dict[int, List[int]] = {pid: [] for pid in pids_here}

            for fid in fids:
                if all(len(series[pid]) >= 10 for pid in pids_here):
                    break
                if fid not in fixture_cache:
                    fixture_cache[fid] = build_shots_for_fixture(fid)
                shots_map, appeared = fixture_cache[fid]
                if not shots_map and not appeared:
                    # fixture unavailable (404) → skip
                    continue
                for pid in pids_here:
                    if len(series[pid]) >= 10:
                        continue
                    if pid in appeared:
                        val = shots_map.get(pid, 0)
                        series[pid].append(val)

            for pid in sorted(pids_here, key=lambda x: (players[x]["name"] or "").lower()):
                meta = players[pid]
                out_rows.append({
                    "player_id": pid,
                    "player_name": meta["name"],
                    "position": meta.get("position") or "",
                    "team_id": meta["team_id"],
                    "team_name": meta["team_name"],
                    "last10_shots": series[pid][-10:],  # oldest→newest
                })

        out_rows.sort(key=lambda r: ((r["team_name"] or "").lower(), (r["player_name"] or "").lower(), r["player_id"]))

        payload = {
            "utc_time": dt.datetime.now(dt.timezone.utc).isoformat(),
            "league_id": lid,
            "players": out_rows,
        }
        with open(os.path.join(BY_LG_DIR, f"{lid}.json"), "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)

        verbose_lines.append(f"===== League {lid} =====")
        last_team = None
        for r in out_rows:
            if r["team_name"] != last_team:
                last_team = r["team_name"]
                verbose_lines.append(f"{last_team} (Team {r['team_id']})")
            seq = r["last10_shots"]
            seq_s = ",".join(str(x) for x in seq) if seq else "(no data)"
            pos_txt = f" [{r['position']}]" if r.get("position") else ""
            verbose_lines.append(f"  {r['player_name']}{pos_txt} = {seq_s}")
        verbose_lines.append("")

    with open(os.path.join(OUT_ROOT, "summary.txt"), "w", encoding="utf-8") as f:
        f.write(f"Time (UTC): {dt.datetime.now(dt.timezone.utc).isoformat()}\n")
        f.write("Endpoint   : between-monthly (fallback to daily)\n")
        f.write("Metric     : Total shots per match (shots_total OR on+off+blocked)\n")
        f.write("Order      : oldest → newest\n")

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
