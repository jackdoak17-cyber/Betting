#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
League-centric shot histories for tracked players (oldest → newest).

Key change vs previous attempt:
- We IGNORE team assignment when gathering history.
- For each target league, we scan finished fixtures (newest → oldest),
  fetch details once per fixture, and update sequences for ANY tracked player
  who appears (minutes ≥ threshold). This handles transfers/promotions cleanly.

Rules
- League-only (fixture.league_id must match).
- Count appearance if minutes >= APPEARANCE_MINUTES (default 45).
- Shots = SHOTS_TOTAL OR (ON_TARGET + OFF_TARGET + BLOCKED), accepting multiple dev-name variants.
- If minutes are missing but the player is an official **starter** and the fixture is finished,
  assume 90' (best-effort to avoid undercount when minutes absent).

Inputs (from your lineup job):
  data/predicted_xi/by_league/*.json  → used ONLY to get (league_id, tracked player_ids, role/label, names)

Outputs:
  data/player_stats/shots/by_league/<league_id>.json
  data/player_stats/shots/summary.txt
  data/player_stats/shots/summary_verbose.txt

Env:
  SPORTMONKS_TOKEN         (required)
  SHOTS_BACK_MONTHS        (optional, default 14)  # how far back to discover fixtures
  APPEARANCE_MINUTES       (optional, default 45)
  MAX_FIXTURE_DETAILS      (optional, default 5000)  # safety ceiling per league
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

SHOTS_BACK_MONTHS = int(os.getenv("SHOTS_BACK_MONTHS", "14"))
APPEARANCE_MINUTES = int(os.getenv("APPEARANCE_MINUTES", "45"))
MAX_FIXTURE_DETAILS = int(os.getenv("MAX_FIXTURE_DETAILS", "5000"))

# Pacing / retries
TIMEOUT = 25
GLOBAL_MIN_DELAY = 0.15
RETRIES_429 = 3
BACKOFF = 1.6

# Folders
PRED_ROOT = "data/predicted_xi/by_league"
OUT_ROOT = "data/player_stats/shots"
BY_LEAGUE_DIR = os.path.join(OUT_ROOT, "by_league")

# -------- HTTP helper with tiny memo --------
_MEMO: Dict[str, dict] = {}
_last_ts = 0.0

def _pace():
    global _last_ts
    now = time.time()
    if now - _last_ts < GLOBAL_MIN_DELAY:
        time.sleep(GLOBAL_MIN_DELAY - (now - _last_ts))
    _last_ts = time.time()

def _key(url: str, params: dict) -> str:
    return url + "?" + "&".join(f"{k}={params[k]}" for k in sorted(params))

def api_get(path: str, params: Optional[dict] = None, ok404: bool = False) -> dict:
    if params is None:
        params = {}
    params = {**params, "api_token": TOKEN}
    url = f"{API_BASE}/{path.lstrip('/')}"
    key = _key(url, params)
    if key in _MEMO:
        return _MEMO[key]

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
        return {"data": []}
    raise last_exc or RuntimeError(f"GET failed for {path}")

# -------- utilities --------
def ensure_dir(p: str): os.makedirs(p, exist_ok=True)
def today_utc() -> dt.date: return dt.datetime.now(dt.timezone.utc).date()
def dstr(d: dt.date) -> str: return d.strftime("%Y-%m-%d")

# -------- read tracked players (ids + labels) --------
def read_tracked() -> Tuple[Dict[int, Dict[int, dict]], Dict[int, str]]:
    """
    Returns:
      players_by_league: {league_id: {player_id: {player_id, player_name, role/pos_label}}}
      league_names:      {league_id: name}
    """
    players_by_league: Dict[int, Dict[int, dict]] = {}
    league_names: Dict[int, str] = {}

    if not os.path.isdir(PRED_ROOT):
        raise SystemExit("No predicted_xi found under data/predicted_xi/by_league/. Run the lineup workflow first.")

    for fn in os.listdir(PRED_ROOT):
        if not fn.endswith(".json"):
            continue
        path = os.path.join(PRED_ROOT, fn)
        try:
            blob = json.loads(open(path, "r", encoding="utf-8").read())
        except Exception:
            continue

        lid = int(blob.get("league_id") or fn.split(".")[0])
        league_names[lid] = blob.get("league_name") or str(lid)
        bucket = players_by_league.setdefault(lid, {})

        for fx in (blob.get("fixtures") or []):
            for side in ("home", "away"):
                t = fx.get(side) or {}
                for p in (t.get("predicted_xi") or []):
                    pid = int(p.get("player_id") or 0)
                    if not pid: continue
                    name = (p.get("name") or p.get("player_name") or "").strip()
                    role = (p.get("role") or p.get("position_label") or "").strip()
                    bucket[pid] = {"player_id": pid, "player_name": name, "role": role}

    return players_by_league, league_names

# -------- discover fixtures in a window (for a league) --------
def list_between(a: dt.date, b: dt.date) -> List[dict]:
    start_iso = f"{dstr(a)} 00:00:00"; end_iso = f"{dstr(b)} 23:59:59"
    j = api_get(f"fixtures/between/{start_iso}/{end_iso}", {
        "include": "league;state",
        "order": "desc",
        "page": 1
    }, ok404=True)
    data = j.get("data") or []
    meta = j.get("meta") or {}
    last = int(meta.get("last_page", 1) or 1)
    out = list(data)
    for p in range(2, last + 1):
        jp = api_get(f"fixtures/between/{start_iso}/{end_iso}", {
            "include": "league;state",
            "order": "desc",
            "page": p
        }, ok404=True)
        out.extend(jp.get("data") or [])
    return out

def list_by_day(a: dt.date, b: dt.date) -> List[dict]:
    out: List[dict] = []
    d = b
    while d >= a:
        j = api_get(f"fixtures/date/{dstr(d)}", {"include": "league;state", "order": "desc", "page": 1}, ok404=True)
        data = j.get("data") or []
        meta = j.get("meta") or {}
        last = int(meta.get("last_page", 1) or 1)
        out.extend(data)
        for p in range(2, last + 1):
            jp = api_get(f"fixtures/date/{dstr(d)}", {"include": "league;state", "order": "desc", "page": p}, ok404=True)
            out.extend(jp.get("data") or [])
        d -= dt.timedelta(days=1)
    return out

def discover_league_fixtures(lid: int) -> List[dict]:
    """
    Return newest → oldest finished fixtures for this league within SHOTS_BACK_MONTHS.
    """
    end = today_utc()
    start = end - dt.timedelta(days=31 * SHOTS_BACK_MONTHS)
    collected: Dict[int, dict] = {}

    cursor_end = end
    for _ in range(SHOTS_BACK_MONTHS):
        cursor_start = max(start, cursor_end - dt.timedelta(days=31))
        chunk = list_between(cursor_start, cursor_end)
        if not chunk:
            chunk = list_by_day(cursor_start, cursor_end)
        for fx in chunk:
            try:
                if int(fx.get("league_id") or 0) != lid:
                    continue
            except Exception:
                continue
            st = (fx.get("state") or {})
            sn = (st.get("short_name") or "").upper()
            name = (st.get("name") or "").upper()
            # keep only finished fixtures at discovery stage
            if not ("FT" in sn or "FULL" in name or st.get("id") in (5, 45, 490)):
                continue
            fid = int(fx.get("id") or 0)
            if fid:
                collected[fid] = fx
        cursor_end = cursor_start - dt.timedelta(days=1)

    out = sorted(collected.values(), key=lambda x: (x.get("starting_at") or "", x.get("id")), reverse=True)
    return out

# -------- parse fixture details (lineups/statistics) --------
SHOT_DEV_TOTAL = {"SHOTS", "SHOTS_TOTAL"}
SHOT_DEV_SOT   = {"SHOTS_ON_TARGET"}
SHOT_DEV_SOFF  = {"SHOTS_OFF_TARGET"}
SHOT_DEV_BLOCK = {"BLOCKED_SHOTS", "SHOTS_BLOCKED"}
MIN_DEV        = {"MINUTES_PLAYED", "MINUTES"}

def _num_from_val(v):
    if isinstance(v, dict):
        if "total" in v:
            try: return int(v["total"] or 0)
            except Exception: return 0
        s = 0
        for x in v.values():
            if isinstance(x, (int, float)):
                s += int(x)
        return s
    try: return int(v or 0)
    except Exception: return 0

def _num_from_detail(det: dict) -> int:
    return _num_from_val((det.get("data") or {}).get("value"))

def fetch_fixture_detail(fid: int) -> Optional[dict]:
    j = api_get(f"fixtures/{fid}", {
        "include": "league;state;lineups.details.type;statistics;statistics.player"
    }, ok404=False)
    return j.get("data") or {}

def parse_fixture_minutes_shots(fx: dict) -> Tuple[Dict[int, int], Dict[int, int], Set[int], bool, str]:
    """
    Returns:
      minutes_map {pid: minutes}, shots_map {pid: shots}, starters {pid}, finished?, when_str
    """
    st = fx.get("state") or {}
    sn = (st.get("short_name") or "").upper()
    name = (st.get("name") or "").upper()
    finished = ("FT" in sn) or ("FULL" in name) or (st.get("id") in (5, 45, 490))
    when = (fx.get("starting_at") or "").replace("T", " ").replace("Z", "")

    minutes_map: Dict[int, int] = {}
    shots_map: Dict[int, int] = {}
    starters: Set[int] = set()

    # lineups branch
    for lp in (fx.get("lineups") or []):
        pid = lp.get("player_id")
        if not pid: continue
        pid = int(pid)
        if int(lp.get("type_id") or 0) == 11:
            starters.add(pid)

        mins = None
        total = None
        sot = soff = blk = 0

        for det in (lp.get("details") or []):
            dev = (det.get("type") or {}).get("developer_name") or ""
            dev = dev.upper()
            if dev in MIN_DEV:
                val = _num_from_detail(det)
                mins = val if mins is None else max(mins, val)
            elif dev in SHOT_DEV_TOTAL:
                total = _num_from_detail(det)
            elif dev in SHOT_DEV_SOT:
                sot += _num_from_detail(det)
            elif dev in SHOT_DEV_SOFF:
                soff += _num_from_detail(det)
            elif dev in SHOT_DEV_BLOCK:
                blk += _num_from_detail(det)

        if mins is not None:
            minutes_map[pid] = mins
        if total is not None:
            shots_map[pid] = total
        else:
            tot = sot + soff + blk
            if tot > 0:
                shots_map[pid] = tot

    # statistics backup (shape varies by feed)
    stats = fx.get("statistics") or []
    if isinstance(stats, dict):
        stats = stats.get("data") or []
    def try_row(row: dict):
        pid = row.get("player_id") or (row.get("player") or {}).get("id")
        if not pid: return
        pid = int(pid)
        # minutes
        for k in ("minutes_played", "MINUTES_PLAYED", "minutes"):
            if k in row:
                minutes_map[pid] = max(minutes_map.get(pid, 0), _num_from_val(row[k]))
        # totals / parts
        total = None
        for k in ("shots_total", "total_shots", "shots", "SHOTS_TOTAL", "SHOTS"):
            if k in row:
                total = _num_from_val(row[k])
        if total is not None:
            shots_map[pid] = total
        else:
            parts = 0
            for k in ("shots_on_target", "shots_off_target", "blocked_shots", "shots_blocked",
                      "SHOTS_ON_TARGET", "SHOTS_OFF_TARGET", "BLOCKED_SHOTS", "SHOTS_BLOCKED"):
                if k in row:
                    parts += _num_from_val(row[k])
            if parts > 0:
                shots_map[pid] = max(shots_map.get(pid, 0), parts)

    for r in stats:
        # nested players
        for key in ("players", "player_stats", "player", "statistics"):
            nested = r.get(key)
            if isinstance(nested, list):
                for pr in nested:
                    if isinstance(pr, dict):
                        try_row(pr)
            elif isinstance(nested, dict):
                try_row(nested)
        if isinstance(r, dict):
            try_row(r)

    return minutes_map, shots_map, starters, finished, when

# -------- main league engine --------
def build_league(players_meta: Dict[int, dict], league_id: int, league_name: str) -> Dict[int, List[int]]:
    """
    For the league, iterate finished fixtures newest→oldest and accumulate last-10 sequences
    for tracked players in this league. Returns {player_id: [shots... oldest→newest]}.
    """
    tracked_pids: Set[int] = set(players_meta.keys())
    sequences: Dict[int, List[Tuple[str, int]]] = {pid: [] for pid in tracked_pids}
    need: Dict[int, int] = {pid: 10 for pid in tracked_pids}
    done_count = 0

    fixtures = discover_league_fixtures(league_id)
    print(f"  Fixtures discovered (finished): {len(fixtures)}")

    detail_calls = 0
    for fx in fixtures:   # newest → oldest
        if detail_calls >= MAX_FIXTURE_DETAILS:
            print(f"  Hit MAX_FIXTURE_DETAILS={MAX_FIXTURE_DETAILS}, stopping early.")
            break
        if done_count >= len(tracked_pids):
            break

        fid = int(fx.get("id") or 0)
        if not fid:
            continue

        # fetch details
        try:
            data = fetch_fixture_detail(fid)
        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code == 404:
                print(f"[SKIP 404] fixture {fid} — no details available")
                continue
            print(f"[WARN] fixture {fid} — {e}")
            continue
        except Exception as e:
            print(f"[WARN] fixture {fid} — {e}")
            continue
        detail_calls += 1

        if int(data.get("league_id") or 0) != league_id:
            continue

        minutes_map, shots_map, starters, finished, when = parse_fixture_minutes_shots(data)
        if not finished:
            continue

        # intersect with tracked
        present = set(minutes_map.keys()) & tracked_pids
        if not present:
            # No tracked players played 45'+ here anyway; quick pass
            continue

        for pid in present:
            if len(sequences[pid]) >= need[pid]:
                continue
            mins = minutes_map.get(pid)
            if mins is None:
                if pid in starters and finished:
                    mins = 90
            if mins is None or mins < APPEARANCE_MINUTES:
                continue
            shots = shots_map.get(pid, 0)
            sequences[pid].append((when, shots))
            if len(sequences[pid]) >= need[pid]:
                done_count += 1

    # convert to oldest→newest ints
    out: Dict[int, List[int]] = {}
    for pid, arr in sequences.items():
        arr.sort(key=lambda x: x[0])     # oldest first
        out[pid] = [v for (_d, v) in arr][-10:]
    print(f"  Detail calls: {detail_calls}")
    have_any = sum(1 for pid in tracked_pids if out[pid])
    print(f"  Players with ≥1 appearance found: {have_any}/{len(tracked_pids)}")
    return out

# -------- MAIN --------
def main():
    ensure_dir(BY_LEAGUE_DIR)
    ensure_dir(OUT_ROOT)

    players_by_league, league_names = read_tracked()
    leagues = sorted(players_by_league.keys())

    uniq_players = set()
    for lid in leagues:
        uniq_players |= set(players_by_league[lid].keys())
    print(f"Leagues (from predicted_xi): {leagues}")
    print(f"Tracked players (unique across leagues): {len(uniq_players)}")

    verbose: List[str] = []
    ts = dt.datetime.now(dt.timezone.utc).isoformat()
    verbose.append(f"Time (UTC): {ts}")
    verbose.append("Endpoint   : fixtures/between + fixtures/date (listing only) + fixtures/{id} (details)")
    verbose.append("Metric     : Total shots per LEAGUE match (shots_total OR on+off+blocked)")
    verbose.append(f"Appearances: ≥{APPEARANCE_MINUTES} minutes (90' assumed for starters if minutes missing)")
    verbose.append("Order      : oldest → newest")
    verbose.append("")

    for lid in leagues:
        lname = league_names.get(lid, str(lid))
        print(f"\n=== League {lid} — {lname} ===")

        series_map = build_league(players_by_league[lid], lid, lname)

        # pack JSON rows
        rows = []
        for pid, meta in sorted(players_by_league[lid].items(), key=lambda kv: (kv[1]["player_name"], kv[0])):
            seq = series_map.get(pid, [])
            rows.append({
                "player_id": pid,
                "player_name": meta.get("player_name") or "",
                "role": meta.get("role") or "",
                "series": seq,
                "apps": len(seq),
            })

        payload = {
            "utc_time": dt.datetime.now(dt.timezone.utc).isoformat(),
            "league_id": lid,
            "league_name": lname,
            "players": rows,
        }
        with open(os.path.join(BY_LEAGUE_DIR, f"{lid}.json"), "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)

        # verbose block
        verbose.append(f"===== League {lid} =====")
        for r in rows:
            tag = f"[{r['role']}]" if r.get("role") else ""
            seq_txt = ",".join(str(x) for x in r["series"]) if r["series"] else "(no data)"
            verbose.append(f"  {r['player_name']} {tag} = {seq_txt}")
        verbose.append("")

    with open(os.path.join(OUT_ROOT, "summary_verbose.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(verbose).rstrip() + "\n")

    with open(os.path.join(OUT_ROOT, "summary.txt"), "w", encoding="utf-8") as f:
        f.write(f"Time (UTC): {dt.datetime.now(dt.timezone.utc).isoformat()}\n")
        f.write("Built per-league JSON under data/player_stats/shots/by_league/\n")
        f.write(f"Minutes threshold: {APPEARANCE_MINUTES}\n")
        f.write(f"Scan window: last {SHOTS_BACK_MONTHS} months (finished fixtures only)\n")
        f.write(f"Max fixture detail calls per league: {MAX_FIXTURE_DETAILS}\n")

    print("\nDone.")
    print(f"Wrote: {BY_LEAGUE_DIR}/*.json and {OUT_ROOT}/summary*.txt")

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
