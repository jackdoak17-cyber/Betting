#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Predict XIs for all leagues using Sportmonks v3.

Logic per fixture/team:
- Prefer official starters if the fixture already has a lineup (type_id == 11).
- Else, use the team's last LEAGUE fixture that has a recorded starting XI.
- Try to exclude players who are currently sidelined (injury/suspension) if the API exposes that relation.
- Enrich DEF into LB/RB/CB/WB using the player's stored position when available. Fallback remains DEF.

Inputs (pre-fetched):
  data/fixtures/{league_id}.json  (same format you generated earlier)

Outputs:
  data/predicted/by_league/{league_id}.json
  data/predicted/summary.txt

Env:
  export SPORTMONKS_TOKEN=...
"""

import os
import re
import json
import time
import hashlib
import datetime as dt
from typing import Dict, List, Optional, Tuple

import requests

# ----------------- config -----------------
API_BASE = "https://api.sportmonks.com/v3"
SPORT = "football"
TOKEN = os.getenv("SPORTMONKS_TOKEN")

LEAGUES: Dict[int, str] = {
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

LINEUP_TYPE_STARTER = 11
TIMEOUT = 25
RETRIES = 3
BACKOFF = 1.6
GLOBAL_MIN_DELAY = 0.12  # gentle pacing

DATE_FMT = "%Y-%m-%d"

# caching
CACHE_DIR = ".cache_smonks"
CACHE_TTL_SECS = 24 * 3600  # 1 day
os.makedirs(CACHE_DIR, exist_ok=True)

def _cache_key(url: str, params: dict) -> str:
    base = url + "?" + "&".join(f"{k}={params[k]}" for k in sorted(params))
    return hashlib.sha256(base.encode("utf-8")).hexdigest()

def _cache_path(key: str) -> str:
    return os.path.join(CACHE_DIR, f"{key}.json")

def _cache_load(key: str) -> Optional[dict]:
    p = _cache_path(key)
    if not os.path.isfile(p):
        return None
    try:
        st = os.stat(p)
        if (time.time() - st.st_mtime) > CACHE_TTL_SECS:
            return None
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None

def _cache_save(key: str, payload: dict) -> None:
    p = _cache_path(key)
    try:
        with open(p, "w", encoding="utf-8") as f:
            json.dump(payload, f)
    except Exception:
        pass

MEMO: Dict[str, dict] = {}

def sm_get(path: str, params: Optional[dict] = None) -> dict:
    if params is None:
        params = {}
    params = {**params, "api_token": TOKEN}
    url = f"{API_BASE}/{SPORT}/{path.lstrip('/')}"
    key = _cache_key(url, params)

    if key in MEMO:
        return MEMO[key]

    cached = _cache_load(key)
    if cached is not None:
        MEMO[key] = cached
        return cached

    # soft pacing
    last_ts = getattr(sm_get, "_last_ts", 0.0)
    now = time.time()
    if now - last_ts < GLOBAL_MIN_DELAY:
        time.sleep(GLOBAL_MIN_DELAY - (now - last_ts))

    last_exc = None
    for attempt in range(1, RETRIES + 1):
        try:
            r = requests.get(url, params=params, timeout=TIMEOUT)
            setattr(sm_get, "_last_ts", time.time())
            if r.status_code >= 400:
                # surface JSON error body if present
                try:
                    jerr = r.json()
                except Exception:
                    jerr = {"message": r.text[:300]}
                if r.status_code == 429 and attempt < (RETRIES + 1):
                    sleep = (BACKOFF ** attempt) + 0.3
                    print(f"[429] sleeping {sleep:.1f}s…")
                    time.sleep(sleep)
                    continue
                raise requests.HTTPError(f"{r.status_code} {r.reason} for {r.url}\nResponse JSON: {jerr}")
            j = r.json()
            MEMO[key] = j
            _cache_save(key, j)
            return j
        except Exception as e:
            last_exc = e
            if attempt < RETRIES:
                sleep = (BACKOFF ** attempt) + 0.2
                time.sleep(sleep)
            else:
                raise
    raise last_exc

# ----------------- small helpers -----------------
def today_utc() -> dt.date:
    return dt.datetime.now(dt.timezone.utc).date()

def pos_label(position_id: Optional[int]) -> str:
    return {24: "GK", 25: "DEF", 26: "MID", 27: "FWD"}.get(int(position_id or 0), "?")

# Try to read a player's specific defensive role from player details if available.
# Falls back to DEF.
ROLE_MAP = {
    # common long names -> short role
    "left back": "LB",
    "right back": "RB",
    "centre back": "CB",
    "center back": "CB",
    "central defender": "CB",
    "wing back": "WB",
    "left wing back": "WB",
    "right wing back": "WB",
    # sometimes short or coded forms from providers
    "d(l)": "LB",
    "d(r)": "RB",
    "d(c)": "CB",
    "d(rc)": "RB",
    "d(lc)": "LB",
    "d(lr)": "WB",  # either side
}

def _norm(s: Optional[str]) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())

PLAYER_ROLE_CACHE: Dict[int, str] = {}

def get_player_role(player_id: int, fallback_group_label: str) -> str:
    """
    Attempts:
      - players/{id}?include=position
      - players/{id} (look for 'position' fields or text hints)
    If not resolvable, returns fallback_group_label (e.g., 'DEF').
    """
    if player_id in PLAYER_ROLE_CACHE:
        return PLAYER_ROLE_CACHE[player_id]

    # Try include=position
    try:
        j = sm_get(f"players/{player_id}", {"include": "position"})
        data = j.get("data") or {}
        pos_obj = data.get("position") or {}
        # can be dict or list depending on API—handle strings too
        cand = None
        if isinstance(pos_obj, dict):
            cand = _norm(pos_obj.get("name") or pos_obj.get("short_name"))
        elif isinstance(pos_obj, list) and pos_obj:
            cand = _norm((pos_obj[0] or {}).get("name") or (pos_obj[0] or {}).get("short_name"))
        if cand:
            for k, v in ROLE_MAP.items():
                if k in cand:
                    PLAYER_ROLE_CACHE[player_id] = v
                    return v
            # if it's clearly a defender but not a specific back role, keep DEF
            if "defend" in cand or "defender" in cand or cand == "d":
                PLAYER_ROLE_CACHE[player_id] = "DEF"
                return "DEF"
    except Exception:
        pass

    # Fallback plain player fetch
    try:
        j2 = sm_get(f"players/{player_id}")
        d2 = j2.get("data") or {}
        # scan some string fields for role hints
        for key in ("position", "position_name", "detailed_position", "common_name", "short_name"):
            val = _norm(d2.get(key))
            if val:
                for k, v in ROLE_MAP.items():
                    if k in val:
                        PLAYER_ROLE_CACHE[player_id] = v
                        return v
    except Exception:
        pass

    PLAYER_ROLE_CACHE[player_id] = fallback_group_label
    return fallback_group_label

# sidelined / unavailable
UNAVAILABLE_CACHE: Dict[int, set] = {}

def get_team_unavailable_ids(team_id: int) -> set:
    """
    Best-effort: tries to get team sidelined list and return player_ids that look currently unavailable.
    If the relation isn't present, returns empty set (we won't exclude anyone).
    """
    if team_id in UNAVAILABLE_CACHE:
        return UNAVAILABLE_CACHE[team_id]

    res = set()
    try:
        j = sm_get(f"teams/{team_id}", {"include": "sidelined"})
        sid = (j.get("data") or {}).get("sidelined")
        today = today_utc()
        def parse_date(s):
            try:
                return dt.datetime.fromisoformat(s.replace("Z","")).date()
            except Exception:
                try:
                    return dt.datetime.strptime(s[:10], "%Y-%m-%d").date()
                except Exception:
                    return None
        if isinstance(sid, list):
            for row in sid:
                pid = row.get("player_id")
                if not pid:
                    continue
                from_d = parse_date(row.get("start_at") or row.get("from") or row.get("start_date") or "")
                to_d   = parse_date(row.get("end_at")   or row.get("to")   or row.get("end_date")   or "")
                # If end unknown or in the future, treat as currently out.
                if (from_d and from_d <= today) and (not to_d or to_d >= today):
                    res.add(int(pid))
    except Exception:
        pass

    UNAVAILABLE_CACHE[team_id] = res
    return res

# -------------- core lineup helpers --------------
def pick_home_away(participants: List[dict]) -> Tuple[Optional[dict], Optional[dict]]:
    home = next((p for p in participants if (p.get("meta") or {}).get("location") == "home"), None)
    away = next((p for p in participants if (p.get("meta") or {}).get("location") == "away"), None)
    return home, away

def fixture_official_xi(fixture_id: int, team_id: int) -> List[dict]:
    try:
        j = sm_get(f"fixtures/{fixture_id}", {"include": "lineups;lineups.player;lineups.position"})
        data = j.get("data") or {}
        lineups = data.get("lineups") or []
        starters = [l for l in lineups if l.get("type_id") == LINEUP_TYPE_STARTER and l.get("team_id") == team_id]
        starters.sort(key=lambda x: x.get("formation_position") or 9999)
        return starters[:11]
    except Exception:
        return []

def team_last_league_fixture_with_xi(team_id: int, league_id: int) -> List[dict]:
    """
    Walk the team's 'latest' first, else date-scan back up to ~180 days for this league and take starters.
    """
    # try latest
    try:
        j = sm_get(f"teams/{team_id}", {"include": "latest.league;latest.lineups;latest.lineups.player"})
        latest = (j.get("data") or {}).get("latest")
        lst = latest if isinstance(latest, list) else ([latest] if latest else [])
        candidates = [fx for fx in lst if (fx or {}).get("league_id") == league_id]
        candidates.sort(key=lambda x: x.get("starting_at") or "", reverse=True)
        for fx in candidates:
            fid = fx.get("id")
            if not fid:
                continue
            full = sm_get(f"fixtures/{fid}", {"include": "lineups;lineups.player;lineups.position"}).get("data", {})
            lineups = full.get("lineups") or []
            starters = [l for l in lineups if l.get("team_id") == team_id and l.get("type_id") == LINEUP_TYPE_STARTER]
            if starters:
                starters.sort(key=lambda x: x.get("formation_position") or 9999)
                return starters[:11]
    except Exception:
        pass

    # scan back by date
    start = today_utc()
    for back in range(1, 181):
        d = (start - dt.timedelta(days=back)).strftime(DATE_FMT)
        try:
            # fixtures/date with minimal includes just to find fixtures in this league
            jj = sm_get(f"fixtures/date/{d}", {"include": "participants;league"})
            fxs = (jj.get("data") or [])
        except Exception:
            continue
        # filter this league and team
        for fx in fxs:
            if fx.get("league_id") != league_id:
                continue
            if any(p.get("id") == team_id for p in (fx.get("participants") or [])):
                try:
                    full = sm_get(f"fixtures/{fx['id']}", {"include": "lineups;lineups.player;lineups.position"}).get("data", {})
                    starters = [l for l in (full.get("lineups") or []) if l.get("team_id") == team_id and l.get("type_id") == LINEUP_TYPE_STARTER]
                    if starters:
                        starters.sort(key=lambda x: x.get("formation_position") or 9999)
                        return starters[:11]
                except Exception:
                    continue
    return []

def build_predicted_xi(fixture_id: int, team_id: int, league_id: int, unavailable_ids: set) -> List[dict]:
    # 1) Official
    xi = fixture_official_xi(fixture_id, team_id)
    # 2) Fallback: last league XI
    if not xi:
        xi = team_last_league_fixture_with_xi(team_id, league_id)

    # filter sidelined if possible
    filtered = []
    for lp in xi:
        pid = lp.get("player_id")
        if pid and int(pid) in unavailable_ids:
            continue
        filtered.append(lp)

    # If we lost players due to sidelined and have < 11, just keep what's left (we don't fetch alternates).
    return filtered[:11]

def enrich_player(lp: dict) -> dict:
    pid = int(lp.get("player_id"))
    pnm = (lp.get("player_name") or "").strip()
    jno = lp.get("jersey_number")
    pgroup = pos_label(lp.get("position_id"))
    role = get_player_role(pid, pgroup if pgroup != "?" else "UNK")
    return {
        "player_id": pid,
        "name": pnm,
        "jersey": jno,
        "position_id": lp.get("position_id"),
        "group": pgroup,  # GK/DEF/MID/FWD
        "role": role,     # LB/RB/CB/WB/DEF/...
        "formation_position": lp.get("formation_position"),
    }

# -------------- IO helpers --------------
def read_fixtures_for_league(lid: int) -> List[dict]:
    path = os.path.join("data", "fixtures", f"{lid}.json")
    if not os.path.isfile(path):
        return []
    with open(path, "r", encoding="utf-8") as f:
        j = json.load(f)
    return j.get("fixtures", []) or []

def ensure_dirs():
    os.makedirs(os.path.join("data", "predicted", "by_league"), exist_ok=True)

def write_league_json(lid: int, lname: str, window_start: str, window_end: str, league_rows: List[dict]):
    out = {
        "league_id": lid,
        "league_name": lname,
        "window_start": window_start,
        "window_end": window_end,
        "fixtures_count": len(league_rows),
        "fixtures": league_rows,
    }
    p = os.path.join("data", "predicted", "by_league", f"{lid}.json")
    with open(p, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False)
    return p

def write_summary(summary_lines: List[str]):
    p = os.path.join("data", "predicted", "summary.txt")
    with open(p, "w", encoding="utf-8") as f:
        f.write("\n".join(summary_lines))
    return p

# -------------- main --------------
def main():
    if not TOKEN:
        raise SystemExit("ERROR: SPORTMONKS_TOKEN is not set.")

    ensure_dirs()

    # Discover window from the fixtures files (min/max starting_at among all fixtures)
    window_start = None
    window_end = None

    total_fixtures = 0
    per_league_counts: Dict[int, int] = {}
    summary: List[str] = []
    stamp = dt.datetime.now(dt.timezone.utc).isoformat()

    league_outputs = []

    for lid, lname in LEAGUES.items():
        fixtures = read_fixtures_for_league(lid)
        if not fixtures:
            continue
        per_league_counts[lid] = len(fixtures)
        total_fixtures += len(fixtures)

        # track window
        for fx in fixtures:
            ts = fx.get("starting_at")
            if not ts:
                continue
            d = ts[:10]
            if window_start is None or d < window_start:
                window_start = d
            if window_end is None or d > window_end:
                window_end = d

        league_rows = []
        summary.append(f"\n=== {lname} ===")
        for fx in fixtures:
            fid = fx.get("id")
            parts = fx.get("participants") or []
            home, away = pick_home_away(parts)
            if not (home and away):
                continue
            home_id, away_id = home["id"], away["id"]
            hname, aname = (home.get("name") or "").strip(), (away.get("name") or "").strip()

            # who is unavailable?
            home_out = get_team_unavailable_ids(home_id)
            away_out = get_team_unavailable_ids(away_id)

            # predict XIs
            h_xi = [enrich_player(lp) for lp in build_predicted_xi(fid, home_id, lid, home_out)]
            a_xi = [enrich_player(lp) for lp in build_predicted_xi(fid, away_id, lid, away_out)]

            # add to JSON payload
            league_rows.append({
                "fixture_id": fid,
                "name": fx.get("name"),
                "starting_at": fx.get("starting_at"),
                "home": {
                    "team_id": home_id,
                    "team_name": hname,
                    "predicted_xi": h_xi,
                },
                "away": {
                    "team_id": away_id,
                    "team_name": aname,
                    "predicted_xi": a_xi,
                },
            })

            # add to summary text
            def fmt_team(team, xi):
                summary.append(f"{team} predicted XI:")
                if not xi:
                    summary.append("  (none)")
                    return
                for p in xi:
                    # e.g. "#9 Gabriel (CB) — DEF"
                    jno = f"#{p['jersey']} " if p.get("jersey") not in (None, "", 0) else ""
                    role = p["role"]
                    grp = p["group"]
                    summary.append(f"  - {jno}{p['name']} ({role}) — {grp}")

            summary.append(f"\n{hname} vs {aname} — {fx.get('starting_at')}")
            fmt_team(hname, h_xi)
            fmt_team(aname, a_xi)

        outp = write_league_json(lid, lname, window_start or "", window_end or "", league_rows)
        league_outputs.append((lid, outp))

    # header
    summary_header = [
        f"Time (UTC): {stamp}",
        f"Window    : {window_start or '-'} -> {window_end or '-'}",
        f"Leagues   : {', '.join(str(k) for k in LEAGUES.keys())}",
        f"Fixtures  : {total_fixtures}",
        "",
        "Per league counts:",
    ]
    for lid in sorted(per_league_counts.keys()):
        summary_header.append(f"  - {lid}: {per_league_counts[lid]}")

    # combine and write summary
    write_summary(summary_header + summary)

    print(f"Done. Predicted XIs written for {len(league_outputs)} leagues.")
    for lid, path in league_outputs:
        print(f"  - {LEAGUES[lid]} -> {path}")
    print("Summary -> data/predicted/summary.txt")

if __name__ == "__main__":
    main()
