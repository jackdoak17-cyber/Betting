#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os, re, json, time, hashlib, datetime as dt
from typing import Any, Dict, Optional, Tuple, List
import requests

API_BASE = "https://api.sportmonks.com/v3"
SPORT = "football"
SM_TOKEN = os.getenv("SPORTMONKS_TOKEN", "YOUR_SPORTMONKS_TOKEN")
ODDS_API_KEY = os.getenv("ODDS_API_KEY", "YOUR_ODDS_API_KEY")

# ---- window: next 6 days ONLY (today + 5) ----
DATE_FMT = "%Y-%m-%d"
def today_utc() -> dt.date:
    return dt.datetime.now(dt.timezone.utc).date()
def next6_dates() -> List[str]:
    d0 = today_utc()
    return [(d0 + dt.timedelta(days=i)).strftime(DATE_FMT) for i in range(0, 6)]

# leagues
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

# odds leagues (slugs)
LEAGUE_SLUGS = {
    8:   "england-premier-league",
    9:   "england-championship",
    384: "italy-serie-a",
    387: "italy-serie-b",
    82:  "germany-bundesliga",
    301: "france-ligue-1",
    564: "spain-laliga",
    567: "spain-laliga-2",
    600: "turkiye-super-lig",
}

# IO
DATA_DIR = os.environ.get("DATA_DIR", "data")
CACHE_SM = os.environ.get("CACHE_SMONKS_DIR", ".cache_smonks")
CACHE_ODDS = os.environ.get("CACHE_ODDS_DIR", ".cache_odds")
os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(CACHE_SM, exist_ok=True)
os.makedirs(CACHE_ODDS, exist_ok=True)

# http with cache/backoff
def _cache_path(base_dir: str, key: str) -> str:
    h = hashlib.sha1(key.encode("utf-8")).hexdigest()
    return os.path.join(base_dir, f"{h}.json")

def _read_json(path: str) -> Optional[Any]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None

def _write_json(path: str, obj: Any) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)
    os.replace(tmp, path)

def get_json_cached(url: str, params: Dict[str, Any], cache_dir: str, ttl_sec: int) -> Any:
    q = url + "?" + "&".join(f"{k}={params[k]}" for k in sorted(params))
    p = _cache_path(cache_dir, q)
    now = time.time()
    if os.path.isfile(p):
        if now - os.path.getmtime(p) < ttl_sec:
            obj = _read_json(p)
            if obj is not None:
                return obj
    # backoff
    tries, base = 0, 1.4
    last_err = None
    while tries < 6:
        try:
            r = requests.get(url, params=params, timeout=25)
            if r.status_code == 200:
                j = r.json()
                _write_json(p, j)
                return j
            if r.status_code in (429, 500, 502, 503, 504):
                last_err = r.text
                sleep = (base ** tries) + (0.25 * (tries + 1))
                time.sleep(sleep)
                tries += 1
                continue
            # hard error (store minimal to avoid thrash)
            _write_json(p, {"_err": r.status_code, "_text": r.text[:200]})
            raise requests.HTTPError(f"{r.status_code} {r.reason} for {r.url}\n{r.text[:200]}")
        except requests.RequestException as e:
            last_err = str(e)
            sleep = (base ** tries) + (0.25 * (tries + 1))
            time.sleep(sleep)
            tries += 1
    raise RuntimeError(f"GET failed after retries: {url} :: {last_err}")

def sm_get(path: str, params: Optional[Dict[str, Any]] = None, ttl_sec: int = 1800) -> Any:
    if params is None: params = {}
    params = {**params, "api_token": SM_TOKEN}
    url = f"{API_BASE}/{SPORT}/{path.lstrip('/')}"
    return get_json_cached(url, params, CACHE_SM, ttl_sec)

# odds api (the same odds-api.io style you used)
EVENTS_API_URL = "https://api.odds-api.io/v3/events"
ODDS_MULTI_API_URL = "https://api.odds-api.io/v3/odds/multi"
BOOKMAKERS = "Bet365"

def odds_get(url: str, params: Dict[str, Any], ttl_sec: int = 900) -> Any:
    params = {**params, "apiKey": ODDS_API_KEY}
    return get_json_cached(url, params, CACHE_ODDS, ttl_sec)

# name normalization helpers (brief)
import unicodedata
def strip_accents(s: str) -> str:
    return ''.join(c for c in unicodedata.normalize('NFD', s or '') if unicodedata.category(c) != 'Mn')
def norm(s: str) -> str:
    s = strip_accents(s or "").lower()
    s = re.sub(r"[^a-z0-9\s-]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s

GENERIC_TEAM_TOKENS = {
    "fc","cf","afc","sc","cd","ud","ac","as","ss","ssc","us","uc","rc","rcd","ca",
    "the","club","de","del","la","las","los","calcio","united","city","saint","st"
}
def team_tokens(name: str):
    toks = set(norm(name).split())
    return {t for t in toks if t not in GENERIC_TEAM_TOKENS}
def team_names_match(a: str, b: str) -> bool:
    if not a or not b: return False
    ta, tb = team_tokens(a), team_tokens(b)
    if not ta or not tb: return False
    if ta == tb: return True
    if ta.issubset(tb) or tb.issubset(ta): return True
    inter = ta & tb
    union = ta | tb
    if len(inter) / max(1, len(union)) >= 0.5: return True
    if len(inter) >= 2: return True
    return False

def pos_label(position_id: Optional[int]) -> str:
    return {24:"GK", 25:"DEF", 26:"MID", 27:"FWD"}.get(position_id or 0, "?")
