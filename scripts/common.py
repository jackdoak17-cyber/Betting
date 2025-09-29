#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os, re, json, time, hashlib, datetime as dt, random
from typing import Optional, Dict, Any, List, Tuple
import requests

# ------------------ ENV / CONSTANTS ------------------
API_BASE_SM = "https://api.sportmonks.com/v3/football"
SPORTMONKS_TOKEN = os.getenv("SPORTMONKS_TOKEN") or os.getenv("SPORTMONKS_API_KEY") or "MISSING"
ODDS_API_KEY = os.getenv("ODDS_API_KEY") or "MISSING"

CACHE_DIR_SM = os.getenv("SM_CACHE_DIR", ".cache_smonks")
CACHE_DIR_ODDS = os.getenv("ODDS_CACHE_DIR", ".cache_odds")
DATA_DIR = "data"

os.makedirs(CACHE_DIR_SM, exist_ok=True)
os.makedirs(CACHE_DIR_ODDS, exist_ok=True)
os.makedirs(DATA_DIR, exist_ok=True)

DATE_FMT = "%Y-%m-%d"
LINEUP_TYPE_STARTER = 11
APPEARANCE_MINUTES_THRESHOLD = 45

# League map (id -> name + odds slug)
LEAGUES = {
    8:   ("Premier League", "england-premier-league"),
    9:   ("Championship", "england-championship"),
    384: ("Serie A", "italy-serie-a"),
    387: ("Serie B", "italy-serie-b"),
    82:  ("Bundesliga", "germany-bundesliga"),
    301: ("Ligue 1", "france-ligue-1"),
    564: ("La Liga", "spain-laliga"),
    567: ("La Liga 2", "spain-laliga-2"),
    600: ("Super Lig", "turkiye-super-lig"),
}

def league_name(league_id: int) -> str:
    return LEAGUES.get(league_id, (f"League {league_id}", ""))[0]

def league_slug(league_id: int) -> str:
    return LEAGUES.get(league_id, ("", ""))[1]

def today_utc() -> dt.date:
    return dt.datetime.now(dt.timezone.utc).date()

def days_ahead(d: dt.date, n: int) -> dt.date:
    return d + dt.timedelta(days=n)

def daterange_str(start: dt.date, end_inclusive: dt.date) -> List[str]:
    out = []
    d = start
    while d <= end_inclusive:
        out.append(d.strftime(DATE_FMT))
        d += dt.timedelta(days=1)
    return out

def pos_id_to_label(position_id: Optional[int]) -> str:
    return {24:"GK",25:"DEF",26:"MID",27:"FWD"}.get(position_id or 0, "?")

# ------------------ FILE IO ------------------
def run_date_dir() -> str:
    d = today_utc().strftime(DATE_FMT)
    p = os.path.join(DATA_DIR, d)
    os.makedirs(p, exist_ok=True)
    return p

def latest_dir() -> str:
    p = os.path.join(DATA_DIR, "latest")
    os.makedirs(p, exist_ok=True)
    return p

def save_json(obj, path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)

def append_jsonl(path, rows: List[Dict[str, Any]]):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False))
            f.write("\n")

# ------------------ CACHING HTTP ------------------
def _cache_path(dir_: str, key: str) -> str:
    h = hashlib.sha1(key.encode("utf-8")).hexdigest()
    return os.path.join(dir_, f"{h}.json")

def _load_cache(path: str) -> Optional[dict]:
    if not os.path.isfile(path): return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None

def _save_cache(path: str, obj: dict):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(obj, f)
    except Exception:
        pass

def http_get_json(url: str, params: dict = None, headers: dict = None, use_cache: bool = True,
                  cache_dir: str = CACHE_DIR_SM, max_retries: int = 6) -> dict:
    params = dict(params or {})
    headers = dict(headers or {})
    key = url + "?" + "&".join(f"{k}={params[k]}" for k in sorted(params))
    cpath = _cache_path(cache_dir, key)

    if use_cache:
        hit = _load_cache(cpath)
        if hit is not None:
            return hit

    attempt = 0
    last_err = None
    while attempt < max_retries:
        try:
            r = requests.get(url, params=params, headers=headers, timeout=30)
            if r.status_code == 200:
                j = r.json()
                _save_cache(cpath, j)
                return j
            if r.status_code == 429:
                # respect reset header if available; otherwise backoff
                reset_after = r.headers.get("Retry-After")
                sleep_s = None
                if reset_after:
                    try:
                        sleep_s = float(reset_after)
                    except:
                        sleep_s = None
                if sleep_s is None:
                    sleep_s = 2.0 * (1.8 ** attempt) + random.uniform(0, 0.5)
                print(f"[429] {url} — sleeping {sleep_s:.1f}s")
                time.sleep(sleep_s)
                attempt += 1
                continue
            if r.status_code in (500,502,503,504):
                sleep_s = 1.0 * (1.8 ** attempt) + random.uniform(0, 0.5)
                print(f"[{r.status_code}] {url} — retry in {sleep_s:.1f}s")
                time.sleep(sleep_s)
                attempt += 1
                continue
            # hard error
            try:
                jerr = r.json()
            except Exception:
                jerr = {"message": r.text[:300]}
            raise requests.HTTPError(f"{r.status_code} {r.reason} for {r.url}\nResponse JSON: {jerr}")
        except Exception as e:
            last_err = e
            sleep_s = 1.2 * (1.8 ** attempt) + random.uniform(0, 0.5)
            print(f"[NET] {url} exception {e} — retry {sleep_s:.1f}s")
            time.sleep(sleep_s)
            attempt += 1
    if last_err:
        raise last_err
    raise RuntimeError("http_get_json failed")

# ------------------ SPORTMONKS HELPERS ------------------
def sm_get(path: str, params: dict = None, use_cache: bool = True) -> dict:
    if SPORTMONKS_TOKEN == "MISSING":
        raise RuntimeError("SPORTMONKS_TOKEN missing (set repo secret)")
    url = f"{API_BASE_SM}/{path.lstrip('/')}"
    params = dict(params or {})
    params["api_token"] = SPORTMONKS_TOKEN
    return http_get_json(url, params=params, use_cache=use_cache, cache_dir=CACHE_DIR_SM)

def fixtures_by_date(date_str: str, league_filter: Optional[set] = None) -> List[dict]:
    params = {"include": "participants;state;league", "order": "asc", "page": 1}
    j = sm_get(f"fixtures/date/{date_str}", params)
    data = list(j.get("data") or [])
    meta = j.get("meta") or {}
    last_page = meta.get("last_page", 1)
    for p in range(2, last_page + 1):
        params["page"] = p
        jp = sm_get(f"fixtures/date/{date_str}", params)
        data.extend(jp.get("data") or [])
    out = []
    for fx in data:
        if league_filter and fx.get("league_id") not in league_filter: continue
        if not fx.get("participants"): continue
        out.append(fx)
    return out

def pick_home_away(parts: List[dict]):
    home = next((p for p in parts if (p.get("meta") or {}).get("location") == "home"), None)
    away = next((p for p in parts if (p.get("meta") or {}).get("location") == "away"), None)
    return home, away

def team_last_fixture_with_xi(team_id: int, league_id: int) -> Optional[dict]:
    # quick path: team's latest
    try:
        j = sm_get(f"teams/{team_id}", {"include": "latest.league;latest.lineups;latest.lineups.player"})
        latest = j.get("data", {}).get("latest")
        lst = latest if isinstance(latest, list) else ([latest] if latest else [])
        cands = [fx for fx in lst if (fx or {}).get("league_id") == league_id]
        cands.sort(key=lambda x: x.get("starting_at") or "", reverse=True)
        for fx in cands:
            fid = fx.get("id")
            if not fid: continue
            full = sm_get(f"fixtures/{fid}", {"include": "lineups;lineups.player"})
            fd = full.get("data") or {}
            if any(l.get("type_id") == LINEUP_TYPE_STARTER and l.get("team_id") == team_id for l in (fd.get("lineups") or [])):
                return fd
    except Exception:
        pass
    # fallback: scan back 180 days
    start = today_utc()
    for back in range(1, 181):
        d = (start - dt.timedelta(days=back)).strftime(DATE_FMT)
        try:
            fxs = fixtures_by_date(d, league_filter={league_id})
        except Exception:
            continue
        for fx in fxs:
            if any(p.get("id") == team_id for p in (fx.get("participants") or [])):
                full = sm_get(f"fixtures/{fx['id']}", {"include": "lineups;lineups.player"})
                fd = full.get("data") or {}
                if any(l.get("type_id") == LINEUP_TYPE_STARTER and l.get("team_id") == team_id for l in (fd.get("lineups") or [])):
                    return fd
    return None

# Stats details
SHOT_DEVS_TOTAL = {"SHOTS","SHOTS_TOTAL"}
SHOT_DEVS_SOT = {"SHOTS_ON_TARGET"}
SHOT_DEVS_SOFF = {"SHOTS_OFF_TARGET"}
MINUTES_DEVS = {"MINUTES_PLAYED","MINUTES"}

def _num_from_detail(det: dict) -> int:
    v = (det.get("data") or {}).get("value")
    if isinstance(v, dict):
        if "total" in v:
            try: return int(v["total"] or 0)
            except: return 0
        s=0
        for x in v.values():
            if isinstance(x,(int,float)): s += int(x)
        return s
    try: return int(v or 0)
    except: return 0

def fixture_lineups_minutes_shots(fixture_id: int) -> Tuple[Dict[int, dict], Dict[int, int], Dict[int, int]]:
    j = sm_get(f"fixtures/{fixture_id}", {"include": "lineups.details.type"})
    data = j.get("data") or {}
    lineups = data.get("lineups") or []
    lineups_map, shots_map, minutes_map = {}, {}, {}
    for lp in lineups:
        pid = lp.get("player_id")
        if not pid: continue
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
        if (total_from_api is not None) or (sot+soff)>0:
            shots_map[pid] = total_from_api if total_from_api is not None else (sot+soff)
    return lineups_map, shots_map, minutes_map

def team_recent_league_fixtures(team_id: int, league_id: int, want: int) -> List[dict]:
    """Newest -> oldest, scan back ~2 years."""
    collected, seen = [], set()
    try:
        j = sm_get(f"teams/{team_id}", {"include": "latest.league"})
        latest = j.get("data", {}).get("latest")
        lst = latest if isinstance(latest, list) else ([latest] if latest else [])
        for fx in lst:
            if fx and fx.get("league_id")==league_id and fx.get("id") not in seen:
                collected.append(fx); seen.add(fx.get("id"))
    except Exception:
        pass
    start = today_utc()
    for back in range(1, 731):
        d = (start - dt.timedelta(days=back)).strftime(DATE_FMT)
        try:
            fxs = fixtures_by_date(d, league_filter={league_id})
        except Exception:
            continue
        for fx in fxs:
            fid = fx.get("id")
            if not fid or fid in seen: continue
            if any(p.get("id")==team_id for p in (fx.get("participants") or [])):
                collected.append(fx); seen.add(fid)
        if len(collected) >= want*14:
            break
    collected.sort(key=lambda x: x.get("starting_at") or "", reverse=True)
    return collected

def player_last_n_shots_series(team_id: int, player_id: int, n: int, league_id: int) -> List[int]:
    fixtures = team_recent_league_fixtures(team_id, league_id, n)
    series: List[Tuple[str,int]] = []
    def consider(fx: dict) -> Optional[Tuple[str,int]]:
        fid = fx.get("id"); if not fid: return None
        _, shots_map, minutes_map = fixture_lineups_minutes_shots(int(fid))
        mins = minutes_map.get(int(player_id))
        if mins is None or mins < APPEARANCE_MINUTES_THRESHOLD:
            return None
        shots = shots_map.get(int(player_id), 0)
        return (fx.get("starting_at") or "", shots)
    for fx in fixtures:
        if len(series) >= n: break
        try:
            res = consider(fx)
        except Exception:
            res = None
        if res: series.append(res)
    series.sort(key=lambda x: x[0], reverse=True)
    return [s for _, s in series][:n]

def compute_hit_rate(series: List[int]) -> float:
    if not series: return 0.0
    hits = sum(1 for x in series if x >= 1)
    return 100.0 * hits / len(series)
