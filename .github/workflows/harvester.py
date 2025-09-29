#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
SportMonks-first shots harvester — scheduled, chunked, and checkpointed.

What it does (incrementally, across many small runs):
  1) Collect fixtures for the next 6 days (per target leagues).
  2) For a small batch of fixtures each run:
       - Build predicted XIs (prefer official XI; else use the team's last league fixture with starters)
       - Enqueue XI players as "work items"
  3) For a small batch of players each run:
       - Compute last up to 10 league appearances (>=45') across this+last season
       - Compute hit rates for last10 and last5 (1+ shot = hit)
       - Save per-player stats to data/stats/*.json (and to a tiny index)
  4) Rebuild exclusive shortlists per league from all computed players so far
  5) Persist state to data/state.json so the next run continues from where it left off

Configure via env vars or CLI flags:
  SPORTMONKS_TOKEN      (required)
  MAX_FIXTURES_PER_RUN  (default 6)
  MAX_PLAYERS_PER_RUN   (default 40)
  DAYS_LOOKAHEAD        (default 5 -> next 6 days inclusive)

Run locally:
  export SPORTMONKS_TOKEN=...
  python scripts/harvester.py

Schedule in GitHub Actions (see harvest.yml).
"""

import os
import sys
import re
import json
import time
import math
import random
import datetime as dt
from typing import Dict, List, Optional, Tuple

import requests

# ------------------------- CONFIG -------------------------
API_BASE = "https://api.sportmonks.com/v3"
SPORT = "football"
TOKEN = os.getenv("SPORTMONKS_TOKEN", "")

# Leagues (SportMonks IDs)
LEAGUES = {
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

# Windows / limits
DAYS_LOOKAHEAD = int(os.getenv("DAYS_LOOKAHEAD", "5"))  # + today = 6 days inclusive
MAX_FIXTURES_PER_RUN = int(os.getenv("MAX_FIXTURES_PER_RUN", "6"))
MAX_PLAYERS_PER_RUN  = int(os.getenv("MAX_PLAYERS_PER_RUN", "40"))

# Shots logic
PLAYER_LOOKBACK = 10
APPEARANCE_MINUTES_THRESHOLD = 45
REQ_APPS_LAST10 = 10
REQ_APPS_LAST5  = 5
REQ_APPS_MIN100 = 3  # for "streakers"

# I/O
DATA_DIR = "data"
STATS_DIR = os.path.join(DATA_DIR, "stats")
STATE_PATH = os.path.join(DATA_DIR, "state.json")
INDEX_PATH = os.path.join(DATA_DIR, "stats_index.json")
SHORT_TXT = os.path.join(DATA_DIR, "shortlists.txt")
SHORT_JSON = os.path.join(DATA_DIR, "shortlists.json")
os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(STATS_DIR, exist_ok=True)

# HTTP
TIMEOUT = 25
RETRIES = 3
BACKOFF = 1.7

DATE_FMT = "%Y-%m-%d"

# ------------------------- HTTP + CACHE -------------------------
class Memo:
    def __init__(self): self.store = {}
    def get(self, k): return self.store.get(k)
    def set(self, k, v): self.store[k]=v

memo = Memo()

def cached_get(url: str, params: dict) -> dict:
    key = url + "?" + "&".join(f"{k}={params[k]}" for k in sorted(params))
    hit = memo.get(key)
    if hit is not None:
        return hit
    last_exc = None
    for attempt in range(1, RETRIES+1):
        try:
            r = requests.get(url, params=params, timeout=TIMEOUT)
            if r.status_code >= 400:
                try:
                    jerr = r.json()
                except Exception:
                    jerr = {"message": r.text[:300]}
                # Surface 429 so the scheduler can just run again later
                raise requests.HTTPError(f"{r.status_code} {r.reason} for {r.url}\nResponse JSON: {jerr}")
            j = r.json()
            memo.set(key, j)
            return j
        except Exception as e:
            last_exc = e
            if attempt < RETRIES:
                sleep = (BACKOFF ** attempt) + random.uniform(0, 0.3)
                time.sleep(sleep)
            else:
                raise
    raise last_exc

def api_get(path: str, params: Optional[dict]=None) -> dict:
    if params is None: params = {}
    params = {**params, "api_token": TOKEN}
    url = f"{API_BASE}/{SPORT}/{path.lstrip('/')}"
    return cached_get(url, params)

# ------------------------- HELPERS -------------------------
def today_utc() -> dt.date:
    return dt.datetime.now(dt.timezone.utc).date()

def date_window(days_ahead: int) -> List[str]:
    start = today_utc()
    end = start + dt.timedelta(days=days_ahead)
    out = []
    d = start
    while d <= end:
        out.append(d.strftime(DATE_FMT))
        d += dt.timedelta(days=1)
    return out

def pos_id_to_label(position_id: Optional[int]) -> str:
    return {24:"GK",25:"DEF",26:"MID",27:"FWD"}.get(position_id or 0, "?")

def pick_home_away(parts: List[dict]):
    h = next((p for p in parts if (p.get("meta") or {}).get("location") == "home"), None)
    a = next((p for p in parts if (p.get("meta") or {}).get("location") == "away"), None)
    return h, a

def compute_hit_rate(series: List[int]) -> float:
    if not series: return 0.0
    hits = sum(1 for x in series if x >= 1)
    return 100.0 * hits / len(series)

# ------------------------- FIXTURES -------------------------
def get_fixtures_for_date(date_str: str, league_filter: Optional[set]=None) -> List[dict]:
    params = {"include": "participants;state;league", "order": "asc", "page": 1}
    j = api_get(f"fixtures/date/{date_str}", params)
    data = j.get("data", []) or []
    meta = j.get("meta") or {}
    last_page = meta.get("last_page", 1)
    for p in range(2, last_page+1):
        params["page"] = p
        jp = api_get(f"fixtures/date/{date_str}", params)
        data.extend(jp.get("data", []) or [])
    out = []
    for fx in data:
        if not fx.get("participants"): continue
        lid = fx.get("league_id")
        if league_filter and lid not in league_filter: continue
        out.append({
            "id": fx.get("id"),
            "league_id": lid,
            "starting_at": fx.get("starting_at"),
            "name": fx.get("name"),
            "participants": fx.get("participants"),
            "state": fx.get("state"),
        })
    return out

# ------------------------- LINEUPS + PER-FIXTURE STATS -------------------------
SHOT_DEVS_TOTAL = {"SHOTS", "SHOTS_TOTAL"}
SHOT_DEVS_SOT   = {"SHOTS_ON_TARGET"}
SHOT_DEVS_SOFF  = {"SHOTS_OFF_TARGET"}
MINUTES_DEVS    = {"MINUTES_PLAYED", "MINUTES"}

def _num_from_detail(det: dict) -> int:
    v = (det.get("data") or {}).get("value")
    if isinstance(v, dict):
        if "total" in v:
            try: return int(v["total"] or 0)
            except: return 0
        s = 0
        for x in v.values():
            if isinstance(x, (int, float)): s += int(x)
        return s
    try: return int(v or 0)
    except: return 0

def get_fixture_lineups_minutes_and_shots(fixture_id: int) -> Tuple[Dict[int, dict], Dict[int, int], Dict[int, int]]:
    j = api_get(f"fixtures/{fixture_id}", {"include": "lineups.details.type"}).get("data", {})
    lineups = j.get("lineups") or []
    lineups_map: Dict[int, dict] = {}
    shots_map: Dict[int, int] = {}
    minutes_map: Dict[int, int] = {}

    for lp in lineups:
        pid = lp.get("player_id")
        if pid is None:
            continue
        pid = int(pid)
        lineups_map[pid] = lp

        total_from_api = None
        sot = soff = 0
        mins = None

        for det in (lp.get("details") or []):
            t = det.get("type") or {}
            dev = (t.get("developer_name") or "").upper()

            if dev in SHOT_DEVS_TOTAL:
                total_from_api = _num_from_detail(det)
            elif dev in SHOT_DEVS_SOT:
                sot += _num_from_detail(det)
            elif dev in SHOT_DEVS_SOFF:
                soff += _num_from_detail(det)
            elif dev in MINUTES_DEVS:
                mv = _num_from_detail(det)
                mins = mv if mins is None else max(mins, mv)

        if mins is not None:
            minutes_map[pid] = mins
        if (total_from_api is not None) or (sot + soff) > 0:
            shots_map[pid] = total_from_api if total_from_api is not None else (sot + soff)

    return lineups_map, shots_map, minutes_map

def get_team_last_fixture_with_xi(team_id: int, league_id: int) -> Optional[dict]:
    # Try team latest first
    try:
        j = api_get(f"teams/{team_id}", {"include": "latest.league;latest.lineups;latest.lineups.player"})
        latest = j.get("data", {}).get("latest")
        lst = latest if isinstance(latest, list) else ([latest] if latest else [])
        cands = [fx for fx in lst if (fx or {}).get("league_id") == league_id]
        cands.sort(key=lambda x: x.get("starting_at") or "", reverse=True)
        for fx in cands:
            fid = fx.get("id")
            if not fid: continue
            full = api_get(f"fixtures/{fid}", {"include": "lineups;lineups.player"}).get("data", {})
            if any(l.get("type_id")==11 and l.get("team_id")==team_id for l in (full.get("lineups") or [])):
                full["participants"] = fx.get("participants") or []
                return full
    except Exception:
        pass

    # Fallback: scan ≤180 days
    start = today_utc()
    for back in range(1, 181):
        d = (start - dt.timedelta(days=back)).strftime(DATE_FMT)
        try:
            fxs = get_fixtures_for_date(d, league_filter={league_id})
        except Exception:
            continue
        for fx in fxs:
            if any(p.get("id")==team_id for p in (fx.get("participants") or [])):
                full = api_get(f"fixtures/{fx['id']}", {"include":"lineups;lineups.player"}).get("data", {})
                if any(l.get("type_id")==11 and l.get("team_id")==team_id for l in (full.get("lineups") or [])):
                    full["participants"] = fx.get("participants") or []
                    return full
    return None

def build_predicted_xi(fx: dict, team_id: int, league_id: int) -> List[dict]:
    # Prefer official XI
    try:
        fx_full = api_get(f"fixtures/{fx['id']}", {"include": "lineups;lineups.player"}).get("data", {})
        starters = [l for l in (fx_full.get("lineups") or []) if l.get("type_id")==11 and l.get("team_id")==team_id]
        if starters:
            starters.sort(key=lambda x: x.get("formation_position") or 9999)
            return starters[:11]
    except Exception:
        pass
    # Fallback: last league fixture with starters
    last = get_team_last_fixture_with_xi(team_id, league_id) or {}
    lineups = last.get("lineups") or []
    starters = [l for l in lineups if l.get("team_id")==team_id and l.get("type_id")==11]
    starters.sort(key=lambda x: x.get("formation_position") or 9999)
    return starters[:11]

# ------------------------- PLAYER HISTORY -------------------------
def get_team_recent_league_fixtures(team_id: int, league_id: int, want: int) -> List[dict]:
    collected: List[dict] = []
    seen = set()
    # seed with latest
    try:
        j = api_get(f"teams/{team_id}", {"include":"latest.league"})
        latest = j.get("data", {}).get("latest")
        lst = latest if isinstance(latest, list) else ([latest] if latest else [])
        for fx in lst:
            if fx and fx.get("league_id")==league_id and fx.get("id") not in seen:
                collected.append(fx); seen.add(fx.get("id"))
    except Exception:
        pass
    # date scan ≤ 2 years
    today = today_utc()
    for back in range(1, 731):
        d = (today - dt.timedelta(days=back)).strftime(DATE_FMT)
        try:
            fixtures = get_fixtures_for_date(d, league_filter={league_id})
        except Exception:
            continue
        for fx in fixtures:
            fid = fx.get("id")
            if not fid or fid in seen: continue
            if any(p.get("id")==team_id for p in (fx.get("participants") or [])):
                collected.append(fx); seen.add(fid)
        if len(collected) >= want * 14:
            break
    collected.sort(key=lambda x: x.get("starting_at") or "", reverse=True)
    return collected

def get_player_last_n_shots_series(team_id: int, player_id: int, n: int, league_id: int) -> List[int]:
    fixtures = get_team_recent_league_fixtures(team_id, league_id, n)
    series: List[Tuple[str, int]] = []
    for fx in fixtures:
        fid = fx.get("id")
        if not fid: continue
        _, shots_map, minutes_map = get_fixture_lineups_minutes_and_shots(fid)
        mins = minutes_map.get(int(player_id))
        if mins is None or mins < APPEARANCE_MINUTES_THRESHOLD:
            continue
        shots = shots_map.get(int(player_id), 0)
        series.append((fx.get("starting_at") or "", shots))
        if len(series) >= n:
            break
    series.sort(key=lambda x: x[0], reverse=True)
    return [s for _, s in series][:n]

# ------------------------- STATE + STATS I/O -------------------------
def read_json(path: str, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default

def write_json_atomic(path: str, obj) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)

def stats_key(league_id: int, team_id: int, player_id: int) -> str:
    return f"{league_id}:{team_id}:{player_id}"

def stats_path_for(key: str) -> str:
    return os.path.join(STATS_DIR, key.replace(":", "_") + ".json")

# ------------------------- SHORTLISTS -------------------------
def build_shortlists(all_stats: List[dict]) -> Dict[str, Dict[str, List[dict]]]:
    """
    Returns: { league_name: { bucket_name: [entries...] } }
    buckets (exclusive, in order):
      - last10_100 (apps10==10 && hit10==100%)
      - last10_80  (apps10==10 && hit10>=80%)
      - last5_100  (apps5==5 && hit5==100%)
      - last5_80   (apps5==5 && hit5>=80%)
      - streak3    (apps_any>=3 && hit_any==100%)
    """
    by_league: Dict[int, List[dict]] = {}
    for s in all_stats:
        by_league.setdefault(s["league_id"], []).append(s)

    out: Dict[str, Dict[str, List[dict]]] = {}
    for lid, rows in by_league.items():
        used = set()
        b10_100 = []
        b10_80 = []
        b5_100 = []
        b5_80 = []
        streak3 = []

        # Priority 1
        for r in rows:
            if r["apps10"] == REQ_APPS_LAST10 and r["hit10"] == 100.0:
                b10_100.append(r); used.add(r["key"])
        # Priority 2
        for r in rows:
            if r["key"] in used: continue
            if r["apps10"] == REQ_APPS_LAST10 and r["hit10"] >= 80.0:
                b10_80.append(r); used.add(r["key"])
        # Priority 3
        for r in rows:
            if r["key"] in used: continue
            if r["apps5"] == REQ_APPS_LAST5 and r["hit5"] == 100.0:
                b5_100.append(r); used.add(r["key"])
        # Priority 4
        for r in rows:
            if r["key"] in used: continue
            if r["apps5"] == REQ_APPS_LAST5 and r["hit5"] >= 80.0:
                b5_80.append(r); used.add(r["key"])
        # Priority 5
        for r in rows:
            if r["key"] in used: continue
            apps_any = max(r["apps5"], r["apps10"])
            hit_any = r["hit5"] if r["apps5"] >= 3 else (r["hit10"] if r["apps10"] >= 3 else 0.0)
            if apps_any >= REQ_APPS_MIN100 and hit_any == 100.0:
                streak3.append(r); used.add(r["key"])

        def tidy(items: List[dict]) -> List[dict]:
            items.sort(key=lambda x: (-x["hit10"], -x["hit5"], x["pos"], x["player_id"]))
            return items

        out[LEAGUES.get(lid, str(lid))] = {
            "last10_100": tidy(b10_100),
            "last10_80":  tidy(b10_80),
            "last5_100":  tidy(b5_100),
            "last5_80":   tidy(b5_80),
            "streak3":    tidy(streak3),
        }
    return out

def print_shortlists(sl: Dict[str, Dict[str, List[dict]]]) -> str:
    lines = []
    for lname in sorted(sl.keys()):
        buckets = sl[lname]
        def dump(tag, key):
            rows = buckets.get(key, [])
            lines.append(f"\n▶ {lname} — {tag}:")
            if not rows:
                lines.append("  (none)")
                return
            for r in rows:
                lines.append(f"  #{r['jersey'] or '-'} {r['player_name']} (pos={r['pos']}) — apps10:{r['apps10']}, hit10:{r['hit10']:.1f}%  | last5:{r['apps5']} apps, hit5:{r['hit5']:.1f}%")
        dump("Last 10 apps: 100% hit rate", "last10_100")
        dump("Last 10 apps: ≥80% hit rate", "last10_80")
        dump("Last 5 apps: 100% hit rate", "last5_100")
        dump("Last 5 apps: ≥80% hit rate", "last5_80")
        dump("Streakers: 100% hit rate in ≥3 apps", "streak3")
    return "\n".join(lines)

# ------------------------- MAIN WORKFLOW -------------------------
def main():
    if not TOKEN or TOKEN == "YOUR_TOKEN_HERE":
        print("Please set SPORTMONKS_TOKEN", file=sys.stderr)
        sys.exit(1)

    print(f"Fixtures window: next {DAYS_LOOKAHEAD+1} days (UTC). Chunked processing…\n")

    # Load state and index
    state = read_json(STATE_PATH, {})
    index = read_json(INDEX_PATH, {})

    # If window missing or stale, refresh fixtures list
    need_fixtures = False
    dates = date_window(DAYS_LOOKAHEAD)
    if not state.get("window") or state["window"] != [dates[0], dates[-1]]:
        need_fixtures = True

    if need_fixtures:
        fixtures_by_league: Dict[int, List[dict]] = {lid: [] for lid in LEAGUES}
        for ds in dates:
            try:
                fxs = get_fixtures_for_date(ds, league_filter=set(LEAGUES))
            except requests.HTTPError as e:
                print(f"[WARN] fixtures for {ds} failed: {e}")
                break
            for fx in fxs:
                lid = fx.get("league_id")
                if lid in fixtures_by_league:
                    fixtures_by_league[lid].append(fx)
        # sort fixtures for deterministic traversal
        for lid in fixtures_by_league:
            fixtures_by_league[lid].sort(key=lambda x: x.get("starting_at") or "")
        state = {
            "window": [dates[0], dates[-1]],
            "fixtures_by_league": fixtures_by_league,
            "fixture_cursors": {str(lid): 0 for lid in LEAGUES},
            "player_queue": [],
            "last_updated": dt.datetime.utcnow().isoformat() + "Z",
        }
        write_json_atomic(STATE_PATH, state)
        print(f"[INIT] Loaded fixtures for leagues. Total fixtures:",
              sum(len(v) for v in fixtures_by_league.values()))

    # 1) Expand queue from fixtures (limited per run)
    added_players = 0
    expanded_fixtures = 0
    for lid in LEAGUES.keys():
        lid_s = str(lid)
        fxs = state["fixtures_by_league"].get(lid_s if lid_s in state["fixtures_by_league"] else lid, [])
        if not fxs:
            fxs = state["fixtures_by_league"].get(lid, [])
        cur = state["fixture_cursors"].get(lid_s, 0)
        while cur < len(fxs) and expanded_fixtures < MAX_FIXTURES_PER_RUN:
            fx = fxs[cur]
            parts = fx.get("participants") or []
            home, away = pick_home_away(parts)
            if not (home and away):
                cur += 1
                continue
            # predicted XI for both sides
            try:
                home_xi = build_predicted_xi(fx, home["id"], lid)
                away_xi = build_predicted_xi(fx, away["id"], lid)
            except requests.HTTPError as e:
                # hit rate limit; stop expanding now, keep cursor for next run
                print(f"[RATE] stopping expansion: {e}")
                state["fixture_cursors"][lid_s] = cur
                write_json_atomic(STATE_PATH, state)
                # jump to processing what we already have
                expanded_fixtures = MAX_FIXTURES_PER_RUN
                break

            def enqueue(xi, team_id):
                nonlocal added_players
                for lp in xi:
                    pid = lp.get("player_id")
                    if pid is None:
                        continue
                    work = {
                        "league_id": lid,
                        "fixture_id": fx["id"],
                        "team_id": team_id,
                        "player_id": int(pid),
                        "player_name": (lp.get("player_name") or "").strip(),
                        "jersey": lp.get("jersey_number"),
                        "pos_id": lp.get("position_id"),
                    }
                    key = stats_key(lid, team_id, int(pid))
                    if key in index:
                        continue  # already computed
                    state["player_queue"].append(work); added_players += 1

            enqueue(home_xi, home["id"])
            enqueue(away_xi, away["id"])

            cur += 1
            expanded_fixtures += 1
            state["fixture_cursors"][lid_s] = cur

        if expanded_fixtures >= MAX_FIXTURES_PER_RUN:
            break

    # Persist state after expansion
    state["last_updated"] = dt.datetime.utcnow().isoformat() + "Z"
    write_json_atomic(STATE_PATH, state)
    if expanded_fixtures:
        print(f"[EXPAND] fixtures processed this run: {expanded_fixtures}  | players enqueued: +{added_players}")

    # 2) Process a batch of players -> compute stats
    queue = state.get("player_queue", [])
    processed = 0
    kept_queue = []
    for work in queue:
        if processed >= MAX_PLAYERS_PER_RUN:
            kept_queue.append(work)
            continue

        lid = work["league_id"]; team_id = work["team_id"]; pid = work["player_id"]
        key = stats_key(lid, team_id, pid)
        if key in index:
            # already computed (from previous run) — keep going
            continue

        try:
            s10 = get_player_last_n_shots_series(team_id, pid, PLAYER_LOOKBACK, lid)
        except requests.HTTPError as e:
            print(f"[RATE] stopping stats (pid={pid}): {e}")
            # put this and the rest back onto the queue
            kept_queue.append(work)
            kept_queue.extend([w for w in queue[queue.index(work)+1:]])
            break

        apps10 = len(s10)
        hit10 = compute_hit_rate(s10) if apps10 > 0 else 0.0
        s5 = s10[:5] if len(s10) >= 5 else s10
        apps5 = len(s5)
        hit5 = compute_hit_rate(s5) if apps5 > 0 else 0.0

        record = {
            "key": key,
            "league_id": lid,
            "league": LEAGUES.get(lid, str(lid)),
            "team_id": team_id,
            "player_id": pid,
            "player_name": work["player_name"],
            "jersey": work.get("jersey"),
            "pos": pos_id_to_label(work.get("pos_id")),
            "apps10": apps10, "hit10": round(hit10, 1),
            "apps5": apps5, "hit5": round(hit5, 1),
            "series10": s10,
            "updated_at": dt.datetime.utcnow().isoformat() + "Z",
        }

        # write per-player file + update index
        path = stats_path_for(key)
        write_json_atomic(path, record)
        index[key] = {"path": path, "league_id": lid, "team_id": team_id, "player_id": pid}
        processed += 1

    # Save queue and index
    state["player_queue"] = kept_queue
    state["last_updated"] = dt.datetime.utcnow().isoformat() + "Z"
    write_json_atomic(STATE_PATH, state)
    write_json_atomic(INDEX_PATH, index)
    print(f"[STATS] players processed this run: {processed}  | remaining in queue: {len(kept_queue)}")

    # 3) Rebuild shortlists from all computed stats so far
    all_stats = []
    for k, meta in index.items():
        try:
            with open(meta["path"], "r", encoding="utf-8") as f:
                rec = json.load(f)
                all_stats.append(rec)
        except Exception:
            pass

    short = build_shortlists(all_stats)
    # write files
    write_json_atomic(SHORT_JSON, {"generated_at": dt.datetime.utcnow().isoformat()+"Z", "shortlists": short})
    txt = print_shortlists(short)
    with open(SHORT_TXT, "w", encoding="utf-8") as f:
        f.write(f"Fixtures window: {dates[0]} → {dates[-1]}\n")
        f.write(txt + "\n")

    print("\n[OUTPUT]")
    print(f"  - {SHORT_JSON}")
    print(f"  - {SHORT_TXT}")

if __name__ == "__main__":
    try:
        main()
    except requests.HTTPError as e:
        # Don’t crash the job on a 429; exit cleanly so the next scheduled run resumes.
        print(f"\nHTTPError: {e}\n", file=sys.stderr)
        sys.exit(0)
    except KeyboardInterrupt:
        pass
