#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Build last-10 LEAGUE shot sequences (oldest → newest) for all players that appear
in data/predicted_xi/by_league/*.json (your lineup job output).

Accuracy rules
- League-only (filter fixtures by league_id).
- Count an appearance if minutes >= APPEARANCE_MINUTES (default 45).
- Shots = SHOTS_TOTAL  OR  (SHOTS_ON_TARGET + SHOTS_OFF_TARGET + BLOCKED_SHOTS/SHOTS_BLOCKED).
- If fixture detail lacks minutes but the player is in the official lineup (starter)
  and the fixture is finished, assume 90' to avoid undercounting due to missing minute data.
- Robust to plan limits:
  - Listing endpoints are used to discover fixture IDs.
  - We only call fixtures/{id} for as many recent fixtures as needed (early stop).
  - Any 404 on fixtures/{id} is logged and skipped without failing the run.

Inputs (must already exist from your lineup workflow)
  data/predicted_xi/by_league/*.json
    {
      "league_id": 8,
      "fixtures": [
         {
           "fixture_id": 123,
           "home": { "team_id": 19, "name": "Arsenal", "predicted_xi": [ { "player_id": ..., "name": ..., "role": "LW", "position_label": "FWD" }, ... ] },
           "away": { ... }
         },
         ...
      ]
    }

Outputs
  data/player_stats/shots/by_league/<league_id>.json
  data/player_stats/shots/summary.txt
  data/player_stats/shots/summary_verbose.txt

Env
  SPORTMONKS_TOKEN         (required)
  SHOTS_BACK_MONTHS        (optional, default 14)   # how far back we scan for fixtures
  APPEARANCE_MINUTES       (optional, default 45)
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

# pacing / retries
TIMEOUT = 25
RETRIES_429 = 3
BACKOFF = 1.6
GLOBAL_MIN_DELAY = 0.15

# where lineups live (from your lineup job)
PRED_ROOT = "data/predicted_xi/by_league"

# outputs
OUT_ROOT = "data/player_stats/shots"
BY_LEAGUE_DIR = os.path.join(OUT_ROOT, "by_league")

# ---- tiny in-run memo ----
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
    """GET with light memo; treat 404 as empty when ok404=True; retry only 429."""
    if params is None:
        params = {}
    params = {**params, "api_token": TOKEN}
    url = f"{API_BASE}/{path.lstrip('/')}"
    key = _key(url, params)
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
                print(f"[429] {path} — sleeping {sleep:.1f}s")
                time.sleep(sleep)
                continue
            r.raise_for_status()
            j = r.json()
            _MEMO[key] = j
            return j
        except Exception as e:
            last_exc = e
            # on non-429 errors (incl. 404 when ok404=False), don't spin
            break
    if ok404:
        return {"data": []}
    raise last_exc or RuntimeError(f"GET failed for {path}")

# ---- helpers ----
def ensure_dir(p: str): os.makedirs(p, exist_ok=True)
def today_utc() -> dt.date: return dt.datetime.now(dt.timezone.utc).date()
def dstr(d: dt.date) -> str: return d.strftime("%Y-%m-%d")

# ---- read tracked players from predicted_xi ----
def read_tracked() -> Tuple[Dict[int, Dict[int, dict]], Dict[int, Set[int]], Dict[int, str]]:
    """
    Returns:
      players_by_league: {league_id: {player_id: {player_id, name, team_id, team_name, role, pos_label}}}
      team_ids_by_league: {league_id: set(team_ids)}
      league_names: {league_id: "Premier League" | ...} if present (optional)
    """
    players_by_league: Dict[int, Dict[int, dict]] = {}
    team_ids_by_league: Dict[int, Set[int]] = {}
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
        lname = blob.get("league_name") or str(lid)
        league_names[lid] = lname
        players_by_league.setdefault(lid, {})
        team_ids_by_league.setdefault(lid, set())

        for fx in (blob.get("fixtures") or []):
            for side in ("home", "away"):
                t = fx.get(side) or {}
                tid = int(t.get("team_id") or 0)
                tname = (t.get("name") or "").strip()
                if not tid:
                    continue
                team_ids_by_league[lid].add(tid)

                for p in (t.get("predicted_xi") or []):
                    pid = int(p.get("player_id") or 0)
                    if not pid:
                        continue
                    name = (p.get("name") or p.get("player_name") or "").strip()
                    role = (p.get("role") or "").strip() or (p.get("position_label") or "").strip()
                    pos_label = (p.get("position_label") or "").strip()
                    players_by_league[lid][pid] = {
                        "player_id": pid, "player_name": name,
                        "team_id": tid, "team_name": tname,
                        "role": role or pos_label or "",
                        "pos_label": pos_label or "",
                    }

    return players_by_league, team_ids_by_league, league_names

# ---- fixture discovery (windows) ----
def list_fixtures_between(a: dt.date, b: dt.date) -> List[dict]:
    """Try fixtures/between once (no paging docs on some plans)."""
    start_iso = f"{dstr(a)} 00:00:00"
    end_iso   = f"{dstr(b)} 23:59:59"
    j = api_get(f"fixtures/between/{start_iso}/{end_iso}", {
        "include": "participants;league;state",
        "order": "desc",
        "page": 1,
    }, ok404=True)
    data = j.get("data") or []
    meta = j.get("meta") or {}
    last = int(meta.get("last_page", 1) or 1)
    out = list(data)
    for p in range(2, last + 1):
        jp = api_get(f"fixtures/between/{start_iso}/{end_iso}", {
            "include": "participants;league;state",
            "order": "desc",
            "page": p,
        }, ok404=True)
        out.extend(jp.get("data") or [])
    return out

def list_fixtures_by_day(a: dt.date, b: dt.date) -> List[dict]:
    out: List[dict] = []
    d = b
    while d >= a:
        j = api_get(f"fixtures/date/{dstr(d)}", {
            "include": "participants;league;state",
            "order": "desc", "page": 1
        }, ok404=True)
        data = j.get("data") or []
        meta = j.get("meta") or {}
        last = int(meta.get("last_page", 1) or 1)
        out.extend(data)
        for p in range(2, last + 1):
            jp = api_get(f"fixtures/date/{dstr(d)}", {
                "include": "participants;league;state",
                "order": "desc", "page": p
            }, ok404=True)
            out.extend(jp.get("data") or [])
        d -= dt.timedelta(days=1)
    return out

def discover_league_fixtures(lid: int, team_ids: Set[int]) -> List[dict]:
    """
    Newest → oldest fixtures for this league within SHOTS_BACK_MONTHS, filtered to games with our teams.
    """
    end = today_utc()
    start = end - dt.timedelta(days=31 * SHOTS_BACK_MONTHS)

    # Try monthly chunks via /between; fallback to daily if needed.
    collected: Dict[int, dict] = {}
    cursor_end = end
    for _ in range(SHOTS_BACK_MONTHS):
        cursor_start = max(start, cursor_end - dt.timedelta(days=31))
        chunk = list_fixtures_between(cursor_start, cursor_end)
        if not chunk:  # fallback: day scan for this month
            chunk = list_fixtures_by_day(cursor_start, cursor_end)
        for fx in chunk:
            try:
                if int(fx.get("league_id") or 0) != lid:
                    continue
            except Exception:
                continue
            parts = fx.get("participants") or []
            if not parts:
                continue
            pids = {int(p.get("id") or 0) for p in parts if p and p.get("id")}
            if not (pids & team_ids):
                continue
            fid = int(fx.get("id") or 0)
            if fid:
                collected[fid] = fx
        cursor_end = cursor_start - dt.timedelta(days=1)

    # newest first
    out = sorted(collected.values(), key=lambda x: (x.get("starting_at") or "", x.get("id")), reverse=True)
    return out

# ---- detail parsing (lineups + player statistics) ----
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
    v = (det.get("data") or {}).get("value")
    return _num_from_val(v)

def parse_lineups_minutes_shots(fx_data: dict) -> Tuple[Dict[int, int], Dict[int, int], Set[int]]:
    """
    Returns:
      minutes_map {pid: minutes}
      shots_map   {pid: total_shots}
      starters    {pid}
    """
    minutes_map: Dict[int, int] = {}
    shots_map: Dict[int, int] = {}
    starters: Set[int] = set()

    for lp in (fx_data.get("lineups") or []):
        pid = lp.get("player_id")
        if not pid:
            continue
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

    return minutes_map, shots_map, starters

def parse_statistics_minutes_shots(fx_data: dict) -> Tuple[Dict[int, int], Dict[int, int]]:
    """
    Backup parser from fixture 'statistics' (with nested players).
    Returns minutes_map, shots_map (fills only if we can parse something).
    """
    minutes_map: Dict[int, int] = {}
    shots_map: Dict[int, int] = {}

    stats = fx_data.get("statistics") or []
    if isinstance(stats, dict):
        stats = stats.get("data") or []

    def try_row(row: dict):
        pid = row.get("player_id") or (row.get("player") or {}).get("id")
        if not pid:
            return
        pid = int(pid)

        # shots_total variants
        total = None
        for key in ("shots_total", "total_shots", "shots"):
            v = row.get(key)
            if v is not None:
                if isinstance(v, dict):
                    total = _num_from_val(v)
                else:
                    try: total = int(v)
                    except Exception: pass
        # dev-name variants in a flattened row
        for key in ("SHOTS_TOTAL", "SHOTS", "SHOTS_ON_TARGET", "SHOTS_OFF_TARGET", "BLOCKED_SHOTS", "SHOTS_BLOCKED",
                    "shots_on_target", "shots_off_target", "blocked_shots", "shots_blocked"):
            v = row.get(key)
            if v is not None:
                if key in ("SHOTS_TOTAL", "SHOTS", "shots_total"):
                    total = _num_from_val(v)
                else:
                    # sum parts if present
                    cur = shots_map.get(pid, 0)
                    shots_map[pid] = cur + _num_from_val(v)

        # minutes
        for key in ("minutes_played", "MINUTES_PLAYED", "minutes"):
            v = row.get(key)
            if v is not None:
                minutes_map[pid] = max(minutes_map.get(pid, 0), _num_from_val(v))

        if total is not None:
            shots_map[pid] = total

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

        # row itself could be a player row
        if isinstance(r, dict):
            try_row(r)

    return minutes_map, shots_map

def fetch_fixture_detail(fid: int) -> Optional[dict]:
    j = api_get(f"fixtures/{fid}", {
        "include": "participants;league;state;lineups.details.type;statistics;statistics.player"
    }, ok404=False)
    return j.get("data") or {}

def is_finished(fx_data: dict) -> bool:
    st = fx_data.get("state") or {}
    sn = (st.get("short_name") or "").upper()
    name = (st.get("name") or "").upper()
    # common finals: FT, AET, PEN, FULL TIME etc.
    return "FT" in sn or "FULL" in name or st.get("id") in (5, 45, 490)

# ---- per-team series builder ----
def build_team_series(lid: int, team_id: int, tracked_pids: List[int], fixtures_newest_first: List[dict]) -> Dict[int, List[int]]:
    """
    For a single team, walk recent league fixtures newest→oldest, fetch details only as
    needed, and build per-player last-10 sequences (oldest→newest in the returned list).
    """
    want = {pid: 10 for pid in tracked_pids}
    seqs: Dict[int, List[Tuple[str, int]]] = {pid: [] for pid in tracked_pids}

    for fx in fixtures_newest_first:
        # early stop if everyone is done
        if all(len(seqs[pid]) >= want[pid] for pid in tracked_pids):
            break

        fid = int(fx.get("id") or 0)
        if not fid:
            continue

        # Only bother if at least one player still needs data
        try:
            data = fetch_fixture_detail(fid)
        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code == 404:
                print(f"[SKIP 404] fixture {fid} — no details available")
                continue
            else:
                # soft-skip other hard API errors
                print(f"[WARN] fixture {fid} — {e}")
                continue
        except Exception as e:
            print(f"[WARN] fixture {fid} — {e}")
            continue

        # sanity: league filter (some teams play cups)
        if int(data.get("league_id") or 0) != lid:
            continue

        when = (data.get("starting_at") or "").replace("T", " ").replace("Z", "")
        mins_map_L, shots_map_L, starters = parse_lineups_minutes_shots(data)
        mins_map_S, shots_map_S = parse_statistics_minutes_shots(data)

        # merge minutes (prefer explicit numbers)
        minutes_map: Dict[int, int] = dict(mins_map_S)
        for pid, m in mins_map_L.items():
            minutes_map[pid] = max(minutes_map.get(pid, 0), m)

        # merge shots (prefer total if present in either; else sum parts already done above)
        shots_map: Dict[int, int] = dict(shots_map_S)
        for pid, s in shots_map_L.items():
            # keep the larger "confidence" (if one source had explicit total use it; our logic already sets totals directly)
            shots_map[pid] = max(shots_map.get(pid, 0), s)

        finished = is_finished(data)

        # record for tracked players of this team only
        for pid in tracked_pids:
            if len(seqs[pid]) >= want[pid]:
                continue
            # appearance minutes
            mins = minutes_map.get(pid)
            if mins is None:
                # fallback: if starter & finished fixture, assume 90
                if pid in starters and finished:
                    mins = 90
            if mins is None or mins < APPEARANCE_MINUTES:
                continue
            shots = shots_map.get(pid, 0)
            seqs[pid].append((when, shots))

    # convert to oldest→newest lists and cap at 10
    out: Dict[int, List[int]] = {}
    for pid, arr in seqs.items():
        arr.sort(key=lambda x: x[0])             # oldest first
        vals = [v for (_d, v) in arr][-10:]
        out[pid] = vals
    return out

# ---- MAIN ----
def main():
    players_by_league, team_ids_by_league, league_names = read_tracked()
    leagues = sorted(players_by_league.keys())
    # cosmetic: show total unique tracked players
    unique_pids = set()
    for lid in leagues:
        unique_pids |= set(players_by_league[lid].keys())
    print(f"Leagues (from predicted_xi): {leagues}")
    print(f"Tracked players (unique across leagues): {len(unique_pids)}")

    ensure_dir(BY_LEAGUE_DIR)
    ensure_dir(OUT_ROOT)

    verbose_lines: List[str] = []
    verbose_lines.append(f"Time (UTC): {dt.datetime.now(dt.timezone.utc).isoformat()}")
    verbose_lines.append("Endpoint   : fixtures/between + fixtures/date (listing only, detail per needed fixture)")
    verbose_lines.append("Metric     : Total shots per LEAGUE match (shots_total OR on+off+blocked)")
    verbose_lines.append(f"Appearances: ≥{APPEARANCE_MINUTES} minutes")
    verbose_lines.append("Order      : oldest → newest")
    verbose_lines.append("")

    # league loop
    for lid in leagues:
        lname = league_names.get(lid, str(lid))
        print(f"\n=== League {lid} — {lname} ===")
        league_players: Dict[int, dict] = players_by_league[lid]
        team_ids = team_ids_by_league[lid]

        # group tracked players by team
        by_team: Dict[int, List[int]] = {}
        for pid, meta in league_players.items():
            by_team.setdefault(int(meta["team_id"]), []).append(pid)

        # discover fixtures in window for this league (newest→oldest)
        league_fxs = discover_league_fixtures(lid, team_ids)

        # Pre-split fixtures per team to avoid scanning the whole list every time
        fxs_by_team: Dict[int, List[dict]] = {}
        for fx in league_fxs:
            parts = fx.get("participants") or []
            tid_set = {int(p.get("id") or 0) for p in parts if p and p.get("id")}
            for tid in (tid_set & team_ids):
                fxs_by_team.setdefault(tid, []).append(fx)

        # Build series team-by-team
        per_league_rows: List[dict] = []
        for tid, pids in sorted(by_team.items()):
            rows = [league_players[pid] for pid in pids if pid in league_players]
            fixtures_for_team = fxs_by_team.get(tid, [])
            series_map = build_team_series(lid, tid, pids, fixtures_for_team)

            # pack output rows
            for r in rows:
                pid = int(r["player_id"])
                seq = series_map.get(pid, [])
                per_league_rows.append({
                    "player_id": pid,
                    "player_name": r["player_name"],
                    "team_id": r["team_id"],
                    "team_name": r["team_name"],
                    "role": r.get("role") or r.get("pos_label") or "",
                    "series": seq,          # oldest → newest, up to 10
                    "apps": len(seq),
                })

        # write JSON per league
        payload = {
            "utc_time": dt.datetime.now(dt.timezone.utc).isoformat(),
            "league_id": lid,
            "league_name": lname,
            "players": sorted(per_league_rows, key=lambda x: (x["team_name"], x["player_name"], x["player_id"])),
        }
        ensure_dir(BY_LEAGUE_DIR)
        with open(os.path.join(BY_LEAGUE_DIR, f"{lid}.json"), "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)

        # verbose summary: team blocks
        verbose_lines.append(f"===== League {lid} =====")
        cur_team = None
        for row in payload["players"]:
            if row["team_name"] != cur_team:
                cur_team = row["team_name"]
                verbose_lines.append(f"{cur_team} (Team {row['team_id']})")
            seq_txt = ",".join(str(v) for v in row["series"]) if row["series"] else "(no data)"
            tag = f"[{row['role']}]" if row.get("role") else ""
            verbose_lines.append(f"  {row['player_name']} {tag} = {seq_txt}")
        verbose_lines.append("")

    # summaries
    with open(os.path.join(OUT_ROOT, "summary_verbose.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(verbose_lines).rstrip() + "\n")

    with open(os.path.join(OUT_ROOT, "summary.txt"), "w", encoding="utf-8") as f:
        f.write(f"Time (UTC): {dt.datetime.now(dt.timezone.utc).isoformat()}\n")
        f.write("Built per-league JSON under data/player_stats/shots/by_league/\n")
        f.write(f"Minutes threshold: {APPEARANCE_MINUTES}\n")
        f.write(f"Scan window: last {SHOTS_BACK_MONTHS} months (monthly between + daily fallback)\n")

    print("\nDone.")
    print(f"Wrote: {BY_LEAGUE_DIR}/*.json and {OUT_ROOT}/summary*.txt")

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
