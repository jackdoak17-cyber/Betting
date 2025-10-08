#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Predict lineups for all fixtures we have fetched, in *shards* to respect rate limits.

Rules:
- NO official current-match lineup; we *assume* the previous league fixture XI.
- If we can't find a last-league XI fast, we stop early (no deep day-by-day scan).
- Mark players OUT if team sidelined list says injury/suspension (best-effort).
- Process fixtures in batches of 3 (light throttle) + per-request micro-sleep.
- Shardable: use --shards 3 --shard 0/1/2 to split the workload across scheduled runs.

Input:
- Reads fixtures from data/fixtures/by_league/{LEAGUE_ID}.json
  (falls back to data/fixtures/{LEAGUE_ID}.json)

Output:
- data/predicted_xi/{league_id}/{fixture_id}.json
- data/predicted_xi/summary.txt

Env:
  SPORTMONKS_TOKEN
"""

import os
import re
import json
import time
import argparse
import datetime as dt
from typing import Dict, List, Optional, Tuple

import requests

# ---------- Config ----------
API_BASE = "https://api.sportmonks.com/v3/football"
API_TOKEN = os.getenv("SPORTMONKS_TOKEN")
if not API_TOKEN:
    raise SystemExit("ERROR: SPORTMONKS_TOKEN is not set.")

# Throttling / retries
TIMEOUT = 25
RETRIES = 3
BACKOFF = 1.6
GLOBAL_MIN_DELAY = 0.18           # micro-sleep between GETs
BATCH_SIZE = 3                    # fixtures per “batch”
SLEEP_BETWEEN_BATCHES = 1.5       # seconds between batches

# Limits to avoid long scans
MAX_FALLBACK_DAYS = 45            # stop scanning after this many days
LINEUP_TYPE_STARTER = 11

# Known leagues we work with (for tidy output names)
LEAGUE_NAMES = {
    8:   "Premier League",
    9:   "Championship",
    384: "Serie A",
    387: "Serie B",
    82:  "Bundesliga",
    301: "Ligue 1",
    564: "La Liga",
    567: "La Liga 2",
    600: "Super Lig",
}

# ---------- tiny HTTP cache (in-memory only during a run) ----------
_MEMO: Dict[str, dict] = {}

def _k(url: str, params: dict) -> str:
    return url + "?" + "&".join(f"{k}={params[k]}" for k in sorted(params))

def _sleep_for_rate_control(last_ts: float) -> float:
    now = time.time()
    if now - last_ts < GLOBAL_MIN_DELAY:
        time.sleep(GLOBAL_MIN_DELAY - (now - last_ts))
    return time.time()

_last_call_ts = 0.0

def api_get(path: str, params: Optional[dict] = None) -> dict:
    global _last_call_ts
    if params is None:
        params = {}
    params = {**params, "api_token": API_TOKEN}
    url = f"{API_BASE}/{path.lstrip('/')}"
    key = _k(url, params)
    hit = _MEMO.get(key)
    if hit is not None:
        return hit

    last_exc = None
    for attempt in range(1, RETRIES + 1):
        _last_call_ts = _sleep_for_rate_control(_last_call_ts)
        try:
            r = requests.get(url, params=params, timeout=TIMEOUT)
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
            if attempt < RETRIES:
                sleep = (BACKOFF ** attempt)
                print(f"[RETRY] {path} (attempt {attempt}) sleeping {sleep:.1f}s")
                time.sleep(sleep)
            else:
                raise
    raise last_exc  # pragma: no cover

# ---------- helpers ----------
def today_utc() -> dt.date:
    return dt.datetime.now(dt.timezone.utc).date()

def date_str(d: dt.date) -> str:
    return d.strftime("%Y-%m-%d")

def pos_id_to_label(position_id: Optional[int]) -> str:
    return {24: "GK", 25: "DEF", 26: "MID", 27: "FWD"}.get(position_id or 0, "?")

def pick_home_away(participants: List[dict]) -> Tuple[Optional[dict], Optional[dict]]:
    home = next((p for p in participants if (p.get("meta") or {}).get("location") == "home"), None)
    away = next((p for p in participants if (p.get("meta") or {}).get("location") == "away"), None)
    return home, away

# ---------- inputs ----------
def _load_json(path: str) -> Optional[dict]:
    if not os.path.isfile(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        try:
            return json.load(f)
        except Exception:
            return None

def load_all_fixtures() -> List[dict]:
    """
    Load fixtures from data/fixtures/by_league/* (preferred) or top-level files.
    Returns a flat list of fixture dicts (with participants etc.).
    """
    base = "data/fixtures"
    by_league = os.path.join(base, "by_league")
    fixtures: List[dict] = []

    def consume_file(fp: str):
        blob = _load_json(fp)
        if not blob:
            return
        if isinstance(blob, dict) and "fixtures" in blob:
            fixtures.extend(blob["fixtures"])
        elif isinstance(blob, list):
            fixtures.extend(blob)
        else:
            # permissive: some files might just be {count, fixtures:[...]}
            fxs = blob.get("fixtures") if isinstance(blob, dict) else None
            if isinstance(fxs, list):
                fixtures.extend(fxs)

    if os.path.isdir(by_league):
        for name in os.listdir(by_league):
            if name.endswith(".json"):
                consume_file(os.path.join(by_league, name))
    else:
        for name in os.listdir(base):
            if name.endswith(".json") and name not in ("latest.json",):
                consume_file(os.path.join(base, name))

    # Clean minimal shape
    out = []
    for fx in fixtures:
        if fx and fx.get("participants") and fx.get("id"):
            out.append(fx)
    return out

# ---------- Sportmonks helpers ----------
def get_fixtures_for_date(date_s: str, league_filter: Optional[set] = None) -> List[dict]:
    """
    Light fallback scan — limited by MAX_FALLBACK_DAYS. Used only when absolutely needed.
    """
    j = api_get(f"fixtures/date/{date_s}", {"include": "participants;lineups;lineups.player;league;state"})
    data = j.get("data", []) or []
    if league_filter:
        data = [d for d in data if d.get("league_id") in league_filter]
    return data

def last_league_fixture_with_starters(team_id: int, league_id: int) -> Optional[dict]:
    """
    Fast path: use teams/{id}?include=latest.league;latest.lineups;latest.lineups.player
    If that fails, scan backwards by DATE but capped at MAX_FALLBACK_DAYS.
    """
    # 1) fast path via 'latest'
    try:
        j = api_get(f"teams/{team_id}", {
            "include": "latest.league;latest.lineups;latest.lineups.player"
        })
        latest = j.get("data", {}).get("latest")
        lst = latest if isinstance(latest, list) else ([latest] if latest else [])
        lst = [fx for fx in lst if (fx or {}).get("league_id") == league_id]
        lst.sort(key=lambda x: x.get("starting_at") or "", reverse=True)
        for fx in lst:
            lineups = fx.get("lineups") or []
            starters = [l for l in lineups if l.get("type_id") == LINEUP_TYPE_STARTER and l.get("team_id") == team_id]
            if starters:
                return fx
    except Exception:
        pass

    # 2) bounded fallback: scan last MAX_FALLBACK_DAYS dates (same league only)
    start = today_utc()
    for back in range(1, MAX_FALLBACK_DAYS + 1):
        d = date_str(start - dt.timedelta(days=back))
        try:
            fxs = get_fixtures_for_date(d, league_filter={league_id})
        except Exception:
            continue
        for fx in fxs:
            if any(p.get("id") == team_id for p in (fx.get("participants") or [])):
                lineups = fx.get("lineups") or []
                starters = [l for l in lineups if l.get("type_id") == LINEUP_TYPE_STARTER and l.get("team_id") == team_id]
                if starters:
                    return fx
    return None

def team_sidelined_map(team_id: int) -> Dict[int, str]:
    """
    Best-effort: map player_id -> reason ("injury", "suspension", etc.).
    If sidelined endpoint/shape differs, we just return an empty map.
    """
    try:
        j = api_get(f"teams/{team_id}", {"include": "sidelined.player;sidelined.type"})
        data = j.get("data", {})
        sidelined = data.get("sidelined") or []
        out = {}
        for row in sidelined:
            pid = row.get("player_id") or (row.get("player") or {}).get("id")
            if not pid:
                continue
            t = (row.get("type") or {}).get("name") or (row.get("type") or {}).get("code") or "sidelined"
            out[int(pid)] = str(t)
        return out
    except Exception:
        return {}

# ---------- Predict XI ----------
def extract_starters_from_fixture(fx: dict, team_id: int) -> List[dict]:
    li = fx.get("lineups") or []
    starters = [l for l in li if l.get("type_id") == LINEUP_TYPE_STARTER and l.get("team_id") == team_id]
    starters.sort(key=lambda x: x.get("formation_position") or 9999)
    return starters[:11]

def predict_xi_for_team(team_id: int, league_id: int) -> List[dict]:
    """
    Return a list of starter dicts with minimal fields (player_id, player_name, jersey_number, position_id).
    """
    last = last_league_fixture_with_starters(team_id, league_id)
    if not last:
        return []
    return extract_starters_from_fixture(last, team_id)

# ---------- Orchestrate ----------
def ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)

def write_json(path: str, payload: dict) -> None:
    ensure_dir(os.path.dirname(path))
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--shards", type=int, default=1, help="total number of shards")
    parser.add_argument("--shard", type=int, default=0, help="this shard index (0..shards-1)")
    args = parser.parse_args()
    if args.shards < 1 or not (0 <= args.shard < args.shards):
        raise SystemExit("Bad shard config: --shards N --shard i with 0<=i<N")

    all_fixtures = load_all_fixtures()
    if not all_fixtures:
        print("No fixtures found under data/fixtures; nothing to do.")
        return

    # Deterministic ordering
    all_fixtures.sort(key=lambda x: (x.get("league_id"), x.get("starting_at") or "", x.get("id")))

    # Shard
    fixtures = [fx for i, fx in enumerate(all_fixtures) if (i % args.shards) == args.shard]
    print(f"Total fixtures: {len(all_fixtures)} | Processing this shard [{args.shard}/{args.shards}]: {len(fixtures)}")

    out_root = "data/predicted_xi"
    ensure_dir(out_root)

    processed = 0
    by_league_counts: Dict[int, int] = {}
    team_sidelined_cache: Dict[int, Dict[int, str]] = {}
    team_xi_cache: Dict[Tuple[int, int], List[dict]] = {}  # (team_id, league_id) -> starters

    for idx, fx in enumerate(fixtures, 1):
        lid = int(fx.get("league_id"))
        parts = fx.get("participants") or []
        home, away = pick_home_away(parts)
        if not (home and away):
            continue
        hid, aid = int(home["id"]), int(away["id"])
        hname, aname = home.get("name") or "Home", away.get("name") or "Away"
        fid = int(fx["id"])

        if idx % BATCH_SIZE == 1:
            # small marker per batch
            print(f"\n-- Batch starting at item {idx}/{len(fixtures)} --")

        print(f"[{idx:>3}/{len(fixtures)}] L{lid} {LEAGUE_NAMES.get(lid, lid)} | FID {fid} | {hname} vs {aname}")

        # Predicted XIs (with small caching at team level within the run)
        key_h = (hid, lid)
        key_a = (aid, lid)
        if key_h not in team_xi_cache:
            team_xi_cache[key_h] = predict_xi_for_team(hid, lid)
        if key_a not in team_xi_cache:
            team_xi_cache[key_a] = predict_xi_for_team(aid, lid)

        home_xi = team_xi_cache[key_h]
        away_xi = team_xi_cache[key_a]

        # Sidelined maps (fetch once per team)
        if hid not in team_sidelined_cache:
            team_sidelined_cache[hid] = team_sidelined_map(hid)
        if aid not in team_sidelined_cache:
            team_sidelined_cache[aid] = team_sidelined_map(aid)

        h_sidelined = team_sidelined_cache[hid]
        a_sidelined = team_sidelined_cache[aid]

        def pack_player(lp: dict, sidelined_map: Dict[int, str]) -> dict:
            pid = int(lp.get("player_id"))
            status = "OK"
            if pid in sidelined_map:
                status = f"OUT: {sidelined_map[pid]}"
            return {
                "player_id": pid,
                "name": (lp.get("player_name") or "").strip(),
                "jersey": lp.get("jersey_number"),
                "position": pos_id_to_label(lp.get("position_id")),
                "status": status,
            }

        payload = {
            "fixture_id": fid,
            "league_id": lid,
            "league_name": LEAGUE_NAMES.get(lid, f"{lid}"),
            "starting_at": fx.get("starting_at"),
            "home": {
                "team_id": hid,
                "name": hname,
                "predicted_xi": [pack_player(p, h_sidelined) for p in home_xi],
            },
            "away": {
                "team_id": aid,
                "name": aname,
                "predicted_xi": [pack_player(p, a_sidelined) for p in away_xi],
            },
            "assumption": "Last league fixture starters (no current official XI).",
        }

        out_path = os.path.join(out_root, str(lid), f"{fid}.json")
        write_json(out_path, payload)

        by_league_counts[lid] = by_league_counts.get(lid, 0) + 1
        processed += 1

        # batch pause
        if idx % BATCH_SIZE == 0:
            time.sleep(SLEEP_BETWEEN_BATCHES)

    # Summary
    summary_lines = []
    summary_lines.append(f"Time (UTC): {dt.datetime.now(dt.timezone.utc).isoformat()}")
    summary_lines.append(f"Shard      : {args.shard+1}/{args.shards}")
    summary_lines.append(f"Fixtures   : {processed}")
    summary_lines.append("")
    summary_lines.append("Per league counts:")
    for lid in sorted(by_league_counts):
        summary_lines.append(f"  - {lid} ({LEAGUE_NAMES.get(lid, lid)}): {by_league_counts[lid]}")
    summary = "\n".join(summary_lines)

    write_json(os.path.join(out_root, "latest.json"), {
        "utc_time": dt.datetime.now(dt.timezone.utc).isoformat(),
        "shard": args.shard,
        "shards": args.shards,
        "processed": processed,
        "by_league": by_league_counts,
    })
    with open(os.path.join(out_root, "summary.txt"), "w", encoding="utf-8") as f:
        f.write(summary + "\n")

    print("\nDone.")
    print(summary)

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
