#!/usr/bin/env python3
"""
Goal: For every player in your predicted XI input, print their last 10 *league-only*
shot counts as a simple sequence like: 1,2,1,2,3,0,1,2,3,2 — plus their position.

Input (one of):
  data/predicted_players.json|.ndjson|.csv
  data/predicted_xi.json|.ndjson|.csv
Or pass --input path.

Env:
  SPORTMONKS_TOKEN (or SPORTMONKS_API_TOKEN)

Usage example:
  python scripts/ingest_shots.py --input data/predicted_xi.csv --leagues 8
"""
from __future__ import annotations
import argparse, csv, json, os, sys, time, glob
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

import requests

BASE_URL = "https://api.sportmonks.com/v3/football"
API_TOKEN = os.getenv("SPORTMONKS_TOKEN") or os.getenv("SPORTMONKS_API_TOKEN")

# --- helpers -----------------------------------------------------------------
def eprint(*a, **k): print(*a, file=sys.stderr, **k)

def _coalesce(*vals, default=None):
    for v in vals:
        if v is not None and v != "":
            return v
    return default

def _as_int(v, default=0):
    try:
        if v is None: return default
        if isinstance(v, (int,)): return int(v)
        if isinstance(v, float): return int(v)
        s = str(v).strip()
        if s == "": return default
        return int(float(s))
    except Exception:
        return default

def _get(d: Dict[str, Any], path: str, default=None):
    cur = d
    for part in path.split("."):
        if isinstance(cur, dict):
            cur = cur.get(part)
        else:
            return default
        if cur is None: return default
    return cur

def _norm_str(x) -> str:
    if x is None: return ""
    return str(x)

# --- args --------------------------------------------------------------------
def parse_args():
    p = argparse.ArgumentParser(description="Print last 10 league-only shots per player (from predicted XI input).")
    p.add_argument("--input", type=str, default=None, help="Path to predicted_xi/predicted_players file")
    p.add_argument("--since", type=str, default=None, help="Start date YYYY-MM-DD (default: now-240d)")
    p.add_argument("--days", type=int, default=240, help="Lookback days if --since omitted")
    p.add_argument("--leagues", type=str, default=None, help="Comma-separated league IDs to process (default: all in input)")
    p.add_argument("--timeout", type=int, default=25)
    p.add_argument("--max-retries", type=int, default=3)
    return p.parse_args()

# --- input discovery/reading --------------------------------------------------
def _candidate_patterns() -> List[str]:
    explicit = [
        "data/predicted_players.json", "data/predicted_players.ndjson", "data/predicted_players.csv",
        "data/predicted_xi.json", "data/predicted_xi.ndjson", "data/predicted_xi.csv",
    ]
    broad = []
    for base in ["data", "outputs", "output", "artifacts", "."]:
        for pat in ["*predicted*.*", "*xi*.*", "*lineup*.*", "*players*.*"]:
            broad.append(os.path.join(base, pat))
    # deep search fallback
    broad += ["data/**/*.csv", "data/**/*.json", "data/**/*.ndjson"]
    return explicit + broad

def _possible_input_paths(cli_path: Optional[str]) -> List[str]:
    if cli_path and os.path.isfile(cli_path):
        return [cli_path]
    paths: List[str] = []
    for pat in _candidate_patterns():
        matches = glob.glob(pat, recursive=True)
        for m in matches:
            if os.path.isfile(m): paths.append(m)
    # de-dup keep order
    seen = set(); out = []
    for p in paths:
        if p not in seen:
            seen.add(p); out.append(p)
    return out

def _normalize_record(rec: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    lid = _as_int(_coalesce(rec.get("league_id"), rec.get("lid"), rec.get("league"), rec.get("leagueId")))
    tid = _as_int(_coalesce(rec.get("team_id"), rec.get("tid"), rec.get("teamId")))
    pid = _as_int(_coalesce(rec.get("player_id"), rec.get("pid"), rec.get("playerId"), rec.get("id")))
    if not (lid and tid and pid): return None
    tname = _coalesce(rec.get("team_name"), rec.get("team"), "")
    pname = _coalesce(rec.get("player_name"), rec.get("name"), "")
    ppos = _coalesce(rec.get("position"), rec.get("pos"), rec.get("role"), "")
    return {"league_id": lid, "team_id": tid, "team_name": tname, "player_id": pid, "player_name": pname, "position": ppos}

def read_tracked_players(cli_input: Optional[str]) -> Tuple[Dict[int, Dict[int, Dict[str, Any]]], Set[int], str]:
    cands = _possible_input_paths(cli_input)
    if not cands:
        raise SystemExit(
            "No input found. Please create one of:\n"
            "  data/predicted_players.json|.ndjson|.csv\n"
            "  data/predicted_xi.json|.ndjson|.csv\n"
            "with columns/keys: league_id, team_id, team_name, player_id, player_name, position\n"
            "…or pass --input path/to/file"
        )
    used = cands[0]
    eprint(f"[INFO] Using input file: {used}")

    leagues: Dict[int, Dict[int, Dict[str, Any]]] = {}

    def add_row(row: Dict[str, Any]):
        norm = _normalize_record(row or {})
        if not norm: return
        lid, tid, pid = norm["league_id"], norm["team_id"], norm["player_id"]
        leagues.setdefault(lid, {}).setdefault(tid, {"team_name": norm["team_name"], "players": {}})
        leagues[lid][tid]["team_name"] = leagues[lid][tid]["team_name"] or norm["team_name"]
        leagues[lid][tid]["players"][pid] = {"name": norm["player_name"], "pos": norm["position"]}

    if used.endswith(".csv"):
        with open(used, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader: add_row(row)
    elif used.endswith(".ndjson"):
        with open(used, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line: continue
                try: add_row(json.loads(line))
                except json.JSONDecodeError: continue
    else:  # .json
        with open(used, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            for rec in data: add_row(rec)
        elif isinstance(data, dict):
            # try common shapes
            for v in data.values():
                if isinstance(v, list):
                    for rec in v: add_row(rec)

    lids = set(leagues.keys())
    return leagues, lids, used

# --- HTTP --------------------------------------------------------------------
def api_get(path: str, *, params=None, timeout=25, max_retries=3) -> Dict[str, Any]:
    if not API_TOKEN:
        raise SystemExit("Missing SPORTMONKS_TOKEN (or SPORTMONKS_API_TOKEN)")
    url = f"{BASE_URL}/{path.lstrip('/')}"
    p = dict(params or {}); p["api_token"] = API_TOKEN
    backoff = 1.6
    for attempt in range(max_retries + 1):
        try:
            r = requests.get(url, params=p, timeout=timeout)
            if r.status_code == 404:
                # Treat 404 as empty page so we don't blow up.
                return {"data": [], "meta": {"pagination": {"current_page": 1, "total_pages": 1}}}
            r.raise_for_status()
            return r.json()
        except requests.HTTPError as e:
            code = getattr(e.response, "status_code", None)
            if code in (429, 500, 502, 503, 504) and attempt < max_retries:
                sleep = backoff * (attempt + 1)
                eprint(f"[RETRY] {path} (attempt {attempt+1}) sleeping {sleep:.1f}s")
                time.sleep(sleep)
                continue
            raise

def paged_get(path: str, *, params=None, timeout=25, max_retries=3) -> Iterable[Dict[str, Any]]:
    page = 1
    while True:
        p = dict(params or {}); p["page"] = page
        j = api_get(path, params=p, timeout=timeout, max_retries=max_retries)
        for item in (j.get("data") or []):
            yield item
        meta = j.get("meta") or {}; pg = meta.get("pagination") or {}
        cur = pg.get("current_page") or page
        tot = pg.get("total_pages") or page
        if cur >= tot: break
        page += 1

# --- shots extraction ---------------------------------------------------------
# We request only the stats we need via lineupDetailTypes:
# 42=shots-total, 41=off, 86=on, 58=shots-blocked, 97=blocked-shots (provider uses both)
LINEUP_INCLUDE = "participants;league;state;lineups.player;lineups.details"
LINEUP_TYPES = "41,42,58,86,97"

DETAIL_ALIASES = {
    "shots_total": {"shots-total", "shots_total", "shots total", "shots"},
    "on": {"shots-on-target", "shots_on_target"},
    "off": {"shots-off-target", "shots_off_target"},
    "blocked": {"shots-blocked", "shots_blocked", "blocked-shots", "blocked_shots"},
}

FINISHED_CODES = {"finished", "ft", "after extra time", "aet", "fter", "fter_pen", "ft_pen", "pen"}
FINISHED_SHORT = {"FT", "AET", "FT_PEN", "PEN"}

def _finished(fix: Dict[str, Any]) -> bool:
    sdata = (_get(fix, "state.data") or {})
    code = _norm_str(_coalesce(sdata.get("code"), sdata.get("name"), sdata.get("state"), "")).lower()
    short = _norm_str(_coalesce(sdata.get("short_name"), sdata.get("code"), "")).upper()
    return (code in FINISHED_CODES) or (short in FINISHED_SHORT)

def _codes_map(details: List[Dict[str, Any]]) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for d in details or []:
        t = d.get("type") or {}
        code = _norm_str(_coalesce(
            (t or {}).get("code"),
            (t or {}).get("name"),
            d.get("code"),
            d.get("type_code"),
            ""
        )).strip().lower()
        if not code: continue
        out[code] = _as_int(d.get("value"), 0)
    return out

def shots_from_details(details: List[Dict[str, Any]]) -> int:
    """Prefer shots-total; otherwise on + off + blocked."""
    codes = _codes_map(details)
    for alias in DETAIL_ALIASES["shots_total"]:
        if alias in codes:
            return _as_int(codes[alias], 0)
    total = 0
    for alias in DETAIL_ALIASES["on"]:      total += _as_int(codes.get(alias), 0)
    for alias in DETAIL_ALIASES["off"]:     total += _as_int(codes.get(alias), 0)
    for alias in DETAIL_ALIASES["blocked"]: total += _as_int(codes.get(alias), 0)
    return total

def shots_for_players_in_fixture(fix: Dict[str, Any], team_id: int, pids: Set[int]) -> Dict[int, int]:
    out: Dict[int, int] = {}
    for lu in (_get(fix, "lineups.data") or []):
        if (lu.get("team_id") or lu.get("teamId")) != team_id:
            continue
        player = _get(lu, "player.data") or {}
        pid = _as_int(player.get("id"))
        if pid not in pids: continue
        details = _get(lu, "details.data") or []
        out[pid] = shots_from_details(details)
    return out

def fetch_team_fixtures_between(team_id: int, start: str, end: str, *, timeout: int, max_retries: int) -> List[Dict[str, Any]]:
    params = dict(
        teams=team_id,
        order="desc",
        include=LINEUP_INCLUDE,
        lineupDetailTypes=LINEUP_TYPES
    )
    return list(paged_get(f"fixtures/between/{start}/{end}", params=params, timeout=timeout, max_retries=max_retries))

# --- main --------------------------------------------------------------------
def main():
    args = parse_args()
    if args.since:
        start_date = args.since
    else:
        start_date = (datetime.now(timezone.utc) - timedelta(days=args.days)).strftime("%Y-%m-%d")
    end_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    leagues_map, lids_from_input, used_path = read_tracked_players(args.input)

    if args.leagues:
        only = {int(x) for x in args.leagues.split(",") if x.strip().isdigit()}
        lids = [lid for lid in sorted(lids_from_input) if lid in only]
    else:
        lids = sorted(lids_from_input)

    print(f"Time (UTC): {datetime.now(timezone.utc).isoformat()}")
    print("Endpoint   : fixtures/between  (league-only; last 10 appearances)")
    print(f"Input file : {used_path}\n")
    print(f"Leagues (from input): {lids}")
    total_players = sum(len(team["players"]) for lid in lids for team in leagues_map.get(lid, {}).values())
    print(f"Tracked players (unique across leagues): {total_players}\n")

    for lid in lids:
        print(f"===== League {lid} =====")
        teams = leagues_map.get(lid, {})
        # sort teams by name
        team_items = sorted(teams.items(), key=lambda kv: _norm_str(kv[1].get("team_name") or f"Team {kv[0]}").lower())

        for team_id, tinfo in team_items:
            team_name = tinfo.get("team_name") or f"Team {team_id}"
            print(f"{team_name} (Team {team_id})")
            players = tinfo.get("players") or {}
            if not players:
                print("  (no tracked players)")
                continue

            # fetch fixtures once per team (wide lookback), then filter to league + finished
            try:
                fixtures = fetch_team_fixtures_between(team_id, start_date, end_date, timeout=args.timeout, max_retries=args.max_retries)
            except requests.HTTPError as e:
                eprint(f"[ERROR] fetch fixtures team {team_id}: {e}")
                fixtures = []

            league_fixtures: List[Dict[str, Any]] = []
            for fix in fixtures:
                if _as_int(_get(fix, "league.data.id")) != lid:      # league-only
                    continue
                if not _finished(fix):                                # finished games only
                    continue
                league_fixtures.append(fix)

            # iterate fixtures newest->oldest (API order=desc already, but keep safe)
            league_fixtures.sort(key=lambda f: _norm_str(f.get("starting_at") or _get(f, "time.starting_at")) , reverse=True)

            pids: Set[int] = set(players.keys())
            per_player: Dict[int, List[int]] = {pid: [] for pid in pids}

            for fix in league_fixtures:
                shots_map = shots_for_players_in_fixture(fix, team_id, pids)
                # append only when present, keep appearance order
                for pid in pids:
                    if pid in shots_map and len(per_player[pid]) < 10:
                        per_player[pid].append(_as_int(shots_map[pid], 0))
                # stop early if everyone has 10
                if all(len(seq) >= 10 for seq in per_player.values()):
                    break

            # stable player order by name
            items = [(info.get("name") or f"Player {pid}", pid, info.get("pos") or "") for pid, info in players.items()]
            items.sort(key=lambda x: _norm_str(x[0]).lower())

            for name, pid, pos in items:
                seq = per_player.get(pid) or []
                if not seq:
                    print(f"  {name} [{pos}] = (no data)")
                else:
                    print(f"  {name} [{pos}] = {','.join(str(x) for x in seq)}")
        print()  # blank line between leagues

if __name__ == "__main__":
    main()
