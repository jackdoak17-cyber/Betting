#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Value bets — SHOTS certs (1+ in 100% of last 7, min games=7)
Source data: data/player_shots/combined.json (+ fixtures from Sportmonks)
Provider: Sportmonks only
Bookmaker: Bet365 only (bookmaker_id=2)
Markets: Player Shots (market_id=268), Match Winner (market_id=1)
Filters:
  - Player qualifies: last 7 matches all >=1 shot (len(series)>=7)
  - Price Over 0.5 >= MIN_DEC_PRICE
  - Team ML (Bet365) for player's side < TEAM_WIN_MAX
Output: data/value_bets/shots_certs.txt + console

ENV:
  SPORTMONKS_TOKEN (required)
  MIN_DEC_PRICE (default=1.30)
  TEAM_WIN_MAX  (default=3.50)

Notes:
  - Fixtures fetched for the next 7 days across configured leagues.
  - Odds fetched per fixture (with retries & tolerant parsing).
  - Player/label matching is tolerant to small naming diffs.
"""

import os, re, json, math, time, random, unicodedata, datetime as dt
from pathlib import Path
from typing import Dict, List, Tuple, Optional, Any
import requests
from itertools import islice

# ========= CONFIG =========
BOOKMAKER_ID = 2  # Bet365
MARKET_PLAYER_SHOTS = 268  # PLAYER_TOTAL_SHOTS
MARKET_MATCH_WINNER  = 1   # FULLTIME_RESULT

MIN_PRICE = float(os.getenv("MIN_DEC_PRICE", "1.30"))
TEAM_ML_MAX = float(os.getenv("TEAM_WIN_MAX", "3.50"))

# Limit to the leagues you care about (Sportmonks league IDs)
LEAGUE_IDS = [
    8,   # England - Premier League
    9,   # England - Championship
    82,  # Germany - Bundesliga
    301, # France - Ligue 1
    384, # Italy - Serie A
    387, # Italy - Serie B
    564, # Spain - LaLiga
    567, # Spain - LaLiga 2
    72,  # Netherlands - Eredivisie
    600, # Türkiye - Süper Lig
]

BASE = "https://api.sportmonks.com/v3"
TIMEOUT = 25
MAX_ODDS_PAGES = 1  # not used now; we fetch odds per fixture to stay precise
HTTP_HEADERS = {"accept": "application/json", "user-agent": "sm-odds-shots-certs/1.0"}

ROOT = Path(".")
DATA_DIR   = ROOT / "data"
OUT_DIR    = DATA_DIR / "value_bets"; OUT_DIR.mkdir(parents=True, exist_ok=True)
OUT_FILE   = OUT_DIR / "shots_certs.txt"
COMBINED   = DATA_DIR / "player_shots" / "combined.json"

# ========= STRING NORMALISATION =========
def strip_accents(s: str) -> str:
    return ''.join(c for c in unicodedata.normalize('NFD', s or '') if unicodedata.category(c) != 'Mn')

def norm(s: str) -> str:
    s = strip_accents(s or "").lower()
    s = re.sub(r"[^a-z0-9\s\.-]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s

def cleanup_label(label: str) -> str:
    return re.sub(r"(?:\s*\([^)]*\))+$", "", label or "").strip()

def extract_last_name_initial(name: str):
    if not name: return None, None
    name2 = strip_accents(name).replace(".", " ").strip()
    parts = [p for p in name2.split() if p]
    if not parts: return None, None
    last = norm(parts[-1]); initial = None
    for p in parts[:-1]:
        ch = p.strip()[0:1]
        if ch: initial = ch.lower(); break
    return last, initial

def player_label_matches(player: str, option_text: str) -> bool:
    """
    Match "A. Smith" against “A. Smith (Over 0.5)” or similar free text.
    We look for last name and, if present, first initial before the last name.
    """
    if not player or not option_text: return False
    last, initial = extract_last_name_initial(player)
    label = norm(cleanup_label(option_text))
    if not last or last not in label: return False
    if initial:
        first_word_initial = label.split()[0][0:1] if label.split() else None
        if first_word_initial and first_word_initial == initial: return True
        return bool(re.search(rf"\b{initial}\w*\b.*\b{last}\b", label))
    return True

# ========= SERIES FILTER =========
def last7_all_one_plus(series: List[int]) -> bool:
    seq = [x for x in series if isinstance(x, int)]
    if len(seq) < 7: return False
    sub = seq[:7]  # newest -> older
    return all(x >= 1 for x in sub)

# ========= HTTP HELPERS =========
def http_get_with_retries(url: str, params: dict, max_retries=6, base_sleep=1.0, factor=1.8):
    attempt = 0; last_text = ""
    while attempt < max_retries:
        try:
            r = requests.get(url, params=params, headers=HTTP_HEADERS, timeout=TIMEOUT)
            if r.status_code == 200:
                return r
            if r.status_code in (429,500,502,503,504):
                last_text = r.text
                sleep = base_sleep * (factor ** attempt) + random.uniform(0, 0.4)
                print(f"[RETRY] {url} {r.status_code}; sleeping {sleep:.1f}s...")
                time.sleep(sleep); attempt += 1; continue
            print(f"[HTTP {r.status_code}] {url} :: {r.text[:200]}")
            return None
        except requests.exceptions.RequestException as e:
            sleep = base_sleep * (factor ** attempt) + random.uniform(0, 0.4)
            print(f"[NET] {url} exception: {e}; sleeping {sleep:.1f}s...")
            time.sleep(sleep); attempt += 1
    if last_text:
        print(f"[ERROR] Retries exhausted for {url}. Last body: {last_text[:220]}")
    else:
        print(f"[ERROR] Retries exhausted for {url}.")
    return None

# ========= SPORTMONKS: FIXTURES =========
def get_fixtures_between(token: str, start_date: str, end_date: str, league_ids: List[int]) -> List[dict]:
    """
    Try the canonical 'between' endpoint first, then fall back to per-day if needed.
    Includes participants so we can map team_id -> fixture.
    """
    q = {
        "api_token": token,
        "include": "participants",
        "per_page": 50,
        "filters": f"leagues:{','.join(map(str, league_ids))}"  # tolerated by fixtures API
    }
    url = f"{BASE}/football/fixtures/between/{start_date}/{end_date}"

    r = http_get_with_retries(url, q)
    data: Dict[str, Any] = {}
    if r and r.status_code == 200:
        try: data = r.json() or {}
        except: data = {}

    # If the endpoint name differs on your plan, fall back to daily pulls (today .. end_date)
    if not data.get("data"):
        print(f"[WARN] No fixtures via 'between'; trying per-day fallback.")
        out = []
        day = dt.datetime.fromisoformat(start_date)
        stop = dt.datetime.fromisoformat(end_date)
        while day <= stop:
            dstr = day.strftime("%Y-%m-%d")
            url2 = f"{BASE}/football/fixtures/date/{dstr}"
            r2 = http_get_with_retries(url2, q)
            if r2 and r2.status_code == 200:
                try:
                    jd = r2.json() or {}
                    out.extend(jd.get("data") or [])
                except:
                    pass
            day += dt.timedelta(days=1)
        return out
    return data.get("data") or []

def parse_fixture_participants(fx: dict) -> Dict[str, Any]:
    """
    Return {
      'fixture_id': int,
      'home_id': int | None,
      'away_id': int | None,
      'home_name': str | None,
      'away_name': str | None,
      'kickoff': str (UTC ISO) | ""
    }
    Tolerant to shape differences.
    """
    fid = fx.get("id")
    kickoff = fx.get("starting_at") or fx.get("starting_at_date") or fx.get("date") or ""
    home_id = away_id = None
    home_name = away_name = None

    parts = fx.get("participants") or []
    if isinstance(parts, list) and parts:
        # Common v3 shape: each has id/name/meta.location ('home'/'away')
        for p in parts:
            pid = p.get("id") or p.get("participant_id")
            nm  = p.get("name") or p.get("short_code")
            loc = (p.get("meta") or {}).get("location") or (p.get("meta") or {}).get("is_home")
            if loc in ("home", True, "localteam"):
                home_id, home_name = pid, nm
            elif loc in ("away", False, "visitorteam"):
                away_id, away_name = pid, nm

    # Fallback: some feeds still expose local/visitor ids on fixture
    home_id = home_id or fx.get("localteam_id") or fx.get("home_team_id")
    away_id = away_id or fx.get("visitorteam_id") or fx.get("away_team_id")

    return {
        "fixture_id": fid,
        "home_id": home_id, "away_id": away_id,
        "home_name": home_name, "away_name": away_name,
        "kickoff": str(kickoff).replace("T", " ").replace("Z", ""),
    }

# ========= SPORTMONKS: ODDS (BY FIXTURE) =========
def fetch_odds_for_fixture(token: str, fixture_id: int) -> List[dict]:
    """
    Try plural and singular fixture paths to be safe:
      /football/odds/pre-match/fixtures/{id}
      /football/odds/pre-match/fixture/{id}
    Filter later in code by bookmaker & market ids.
    """
    for path in (f"{BASE}/football/odds/pre-match/fixtures/{fixture_id}",
                 f"{BASE}/football/odds/pre-match/fixture/{fixture_id}"):
        r = http_get_with_retries(path, {"api_token": token, "per_page": 50})
        if r and r.status_code == 200:
            try:
                j = r.json() or {}
                return j.get("data") or []
            except:
                return []
        # continue loop on 404/other
    return []

# ========= CANDIDATES FROM combined.json =========
def load_candidates() -> List[dict]:
    if not COMBINED.exists():
        raise SystemExit(f"ERROR: shots data file missing: {COMBINED}")
    try:
        blob = json.loads(COMBINED.read_text(encoding="utf-8"))
    except Exception as e:
        raise SystemExit(f"ERROR: cannot parse {COMBINED}: {e}")

    players = blob.get("players") or []
    out = []
    for rec in players:
        # Expect latest_first order
        series = rec.get("shots_last_n") or rec.get("series") or []
        if not isinstance(series, list): continue
        if not last7_all_one_plus(series): continue

        lid = rec.get("league_id"); tid = rec.get("team_id")
        if not isinstance(lid, int) or not isinstance(tid, int): continue
        if LEAGUE_IDS and lid not in LEAGUE_IDS: continue

        out.append({
            "league_id": lid,
            "team_id": tid,
            "player_id": rec.get("player_id"),
            "player": rec.get("name") or rec.get("player_name") or "",
            "position": rec.get("position_tag") or "",
            "series": series[:10],
        })
    return out

# ========= PRICE PARSERS =========
def is_over_label(odd: dict) -> bool:
    s1 = (odd.get("label") or "").strip().lower()
    s2 = (odd.get("name")  or "").strip().lower()
    return "over" in s1 or "over" in s2

def total_is_half(odd: dict) -> bool:
    tot = odd.get("total")
    if tot is None: return False
    try: return math.isclose(float(tot), 0.5, abs_tol=1e-9)
    except: return False

def decimals(odd: dict) -> Optional[float]:
    # prefer dp3 if present, else value
    for k in ("dp3", "value"):
        v = odd.get(k)
        if v is None: continue
        try: return float(v)
        except: pass
    return None

# ========= WORKFLOW =========
def main():
    token = os.getenv("SPORTMONKS_TOKEN")
    if not token:
        raise SystemExit("ERROR: SPORTMONKS_TOKEN not set.")

    # 1) Candidates
    candidates = load_candidates()
    print(f"[CANDIDATES] {len(candidates)} players qualify (series>=7 all >=1).")
    if not candidates:
        OUT_FILE.write_text("No player candidates with 1+ in each of last 7.\n", encoding="utf-8")
        print("No player candidates with 1+ in each of last 7.")
        return

    # 2) Fixtures (next 7 days)
    today = dt.datetime.utcnow().date()
    start_date = today.strftime("%Y-%m-%d")
    end_date   = (today + dt.timedelta(days=7)).strftime("%Y-%m-%d")

    fixtures = get_fixtures_between(token, start_date, end_date, sorted(set(LEAGUE_IDS)))
    if not fixtures:
