#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Value bets — SHOTS certs (1+ in 100% of last 7, min games=7)
Source data: data/player_shots/combined.json (+ fixtures from Sportmonks)
Provider: Sportmonks only
Bookmaker: resolved by name (default "Bet365"); ML can fallback to any bookmaker
Markets: Player Shots (268), Match Winner (1)

Filters:
  - Player qualifies: last 7 matches all >=1 shot (len(series)>=7)
  - Player Shots Over 0.5 >= MIN_DEC_PRICE
  - Team ML (preferred bookmaker; else any bookmaker if enabled) < TEAM_WIN_MAX

Env toggles (useful while debugging):
  - BOOKMAKER_NAME="Bet365"
  - MIN_DEC_PRICE="1.30"
  - TEAM_WIN_MAX="3.50"
  - ALLOW_ML_FALLBACK_ANY="1"           (allow ML from any bookmaker if preferred missing)
  - ALLOW_PS_FALLBACK_ANY="0"           (allow Player Shots rows from any shop, not just BOOKMAKER_NAME)
  - NAME_MATCH_MODE="strict|relaxed"    (strict uses initial+surname heuristic; relaxed is surname-only)
  - SKIP_ML_FILTER="0|1"                (skip the team moneyline filter entirely)
  - USE_HAS_ODDS="1|0"                  (add filters=havingOdds to fixtures requests)
  - DEBUG_FIXTURE_SAMPLES="4"           (how many fixtures to dump Player Shots samples for)

Output: data/value_bets/shots_certs.txt + console
Also writes debug files to data/value_bets/debug/
"""

import os, re, json, math, time, random, unicodedata, datetime as dt
from pathlib import Path
from typing import Dict, List, Tuple, Optional, Any, Iterable
import requests
from collections import Counter

# ========= CONFIG =========
BOOKMAKER_NAME = os.getenv("BOOKMAKER_NAME", "Bet365").strip().lower()
MARKET_PLAYER_SHOTS = 268  # PLAYER_TOTAL_SHOTS
MARKET_MATCH_WINNER  = 1   # FULLTIME_RESULT

MIN_PRICE = float(os.getenv("MIN_DEC_PRICE", "1.30"))
TEAM_ML_MAX = float(os.getenv("TEAM_WIN_MAX", "3.50"))
ALLOW_ML_FALLBACK_ANY = os.getenv("ALLOW_ML_FALLBACK_ANY", "1") not in ("0", "false", "False")
ALLOW_PS_FALLBACK_ANY = os.getenv("ALLOW_PS_FALLBACK_ANY", "0") not in ("0", "false", "False")
NAME_MATCH_MODE = (os.getenv("NAME_MATCH_MODE", "strict") or "strict").strip().lower()
SKIP_ML_FILTER = os.getenv("SKIP_ML_FILTER", "0") in ("1", "true", "True")
USE_HAS_ODDS = os.getenv("USE_HAS_ODDS", "1") not in ("0", "false", "False")
DEBUG_FIXTURE_SAMPLES = int(os.getenv("DEBUG_FIXTURE_SAMPLES", "4"))

# Limit to the leagues you care about (Sportmonks league IDs)
LEAGUE_IDS = [
    8,   # Premier League
    9,   # Championship
    82,  # Bundesliga
    301, # Ligue 1
    384, # Serie A
    387, # Serie B
    564, # LaLiga
    567, # LaLiga 2
    72,  # Eredivisie
    600, # Süper Lig
]

BASE = "https://api.sportmonks.com/v3"
TIMEOUT = 25
HTTP_HEADERS = {"accept": "application/json", "user-agent": "sm-odds-shots-certs/1.3"}

ROOT = Path(".")
DATA_DIR   = ROOT / "data"
OUT_DIR    = DATA_DIR / "value_bets"; OUT_DIR.mkdir(parents=True, exist_ok=True)
OUT_FILE   = OUT_DIR / "shots_certs.txt"
COMBINED   = DATA_DIR / "player_shots" / "combined.json"
DEBUG_DIR  = OUT_DIR / "debug"; DEBUG_DIR.mkdir(parents=True, exist_ok=True)

# ========= SMALL UTILS =========
def save_json(path: Path, obj: Any):
    try:
        path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        print(f"[WARN] Could not write {path}: {e}")

def pretty_now_iso() -> str:
    return dt.datetime.utcnow().isoformat()

def numeric_is_half(x: Optional[float]) -> bool:
    try:
        return x is not None and math.isclose(float(x), 0.5, abs_tol=1e-9)
    except:
        return False

# ========= STRING NORMALISATION =========
def strip_accents(s: str) -> str:
    return ''.join(c for c in unicodedata.normalize('NFD', s or '') if unicodedata.category(c) != 'Mn')

def norm(s: str) -> str:
    s = strip_accents(s or "").lower()
    s = re.sub(r"[^a-z0-9\s\.-']", " ", s)
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

def player_label_matches_strict(player: str, option_text: str) -> bool:
    """
    Match "A. Smith" vs “... A. Smith ...”.
    We look for last name and (if present) a token starting with the initial before the last name.
    """
    if not player or not option_text: return False
    last, initial = extract_last_name_initial(player)
    label = norm(cleanup_label(option_text))
    if not last or last not in label: 
        return False
    if initial:
        # initial appears before last name or first token starts with initial
        return bool(re.search(rf"\b{initial}\w*\b.*\b{last}\b", label)) or label.split()[0][:1] == initial
    return True

def player_label_matches_relaxed(player: str, option_text: str) -> bool:
    """Surname-only containment."""
    if not player or not option_text: return False
    last, _ = extract_last_name_initial(player)
    label = norm(cleanup_label(option_text))
    return bool(last and last in label)

def player_label_matches(player: str, option_text: str, mode: str = "strict") -> bool:
    if mode == "relaxed":
        return player_label_matches_relaxed(player, option_text)
    return player_label_matches_strict(player, option_text)

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
    # Combine filters: leagues + (optional) havingOdds
    filters_parts = [f"leagues:{','.join(map(str, league_ids))}"]
    if USE_HAS_ODDS:
        filters_parts.append("havingOdds")
    q = {
        "api_token": token,
        "include": "participants",
        "per_page": 50,
        "filters": ",".join(filters_parts)
    }
    url = f"{BASE}/football/fixtures/between/{start_date}/{end_date}"

    r = http_get_with_retries(url, q)
    data: Dict[str, Any] = {}
    if r and r.status_code == 200:
        try: data = r.json() or {}
        except: data = {}

    if not data.get("data"):
        print(f"[WARN] No fixtures via 'between'; trying per-day fallback.")
        out = []
        day = dt.datetime.fromisoformat(start_date)
        stop = dt.datetime.fromisoformat(end_date)
        while day <= stop:
            dstr = day.strftime("%Y-%m-%d")
            url2 = f"{BASE}/football/fixtures/date/{dstr}"
            q2 = dict(q)
            r2 = http_get_with_retries(url2, q2)
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
    fid = fx.get("id")
    kickoff = fx.get("starting_at") or fx.get("starting_at_date") or fx.get("date") or ""
    home_id = away_id = None
    home_name = away_name = None

    parts = fx.get("participants") or []
    if isinstance(parts, list) and parts:
        for p in parts:
            pid = p.get("id") or p.get("participant_id")
            nm  = p.get("name") or p.get("short_code")
            loc = (p.get("meta") or {}).get("location") or (p.get("meta") or {}).get("is_home")
            if loc in ("home", True, "localteam"):
                home_id, home_name = pid, nm
            elif loc in ("away", False, "visitorteam"):
                away_id, away_name = pid, nm

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
    params = {"api_token": token, "per_page": 200, "include": "bookmaker;market"}
    for path in (f"{BASE}/football/odds/pre-match/fixtures/{fixture_id}",
                 f"{BASE}/football/odds/pre-match/fixture/{fixture_id}"):
        r = http_get_with_retries(path, params)
        if r and r.status_code == 200:
            try:
                j = r.json() or {}
                return j.get("data") or []
            except:
                return []
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

# ========= PRICE + TEXT PARSERS =========
def decimals(odd: dict) -> Optional[float]:
    for k in ("dp3", "value"):
        v = odd.get(k)
        if v is None: continue
        try: return float(v)
        except: pass
    return None

def any_text(odd: dict) -> str:
    fields = []
    for k in ("original_label", "label", "name"):
        t = odd.get(k)
        if isinstance(t, str) and t.strip():
            fields.append(t.strip())
    p = odd.get("participants")
    if isinstance(p, str) and p.strip():
        fields.append(p.strip())
    elif isinstance(p, dict):
        fields.append((p.get("name") or p.get("label") or "").strip())
    elif isinstance(p, list):
        for it in p:
            if isinstance(it, dict):
                nm = (it.get("name") or it.get("label") or "").strip()
                if nm: fields.append(nm)
            elif isinstance(it, str) and it.strip():
                fields.append(it.strip())
    return " | ".join([x for x in fields if x])

def player_text_from_odd(odd: dict) -> str:
    # For Player Shots feeds, the player is usually in `name`
    nm = odd.get("name")
    if isinstance(nm, str) and nm.strip():
        return nm
    # Then try participants and other textual fields
    p = odd.get("participants")
    if isinstance(p, str): return p
    if isinstance(p, dict): return p.get("name") or p.get("label") or ""
    if isinstance(p, list):
        for it in p:
            if isinstance(it, dict):
                nm2 = it.get("name") or it.get("label") or ""
                if nm2: return nm2
            elif isinstance(it, str):
                return it
    return any_text(odd)

def parse_side_and_line(odd: dict) -> Tuple[Optional[str], Optional[float], str]:
    """
    Return ('over'|'under'|None, line(float)|None, debug_json)

    Sportmonks/Bet365 Player Shots often encode:
      - line in `label` (e.g., "0.5", "1.5", ...)
      - player in `name`
      - no explicit "Over"/"Under" tokens

    Heuristics:
      1) If "under" appears anywhere -> under
      2) Else if "over" appears -> over
      3) Else if market_id == 268 (Player Shots) -> assume over
      4) Else if line==0.5 and no "under" -> over
    """
    dbg = {"src": []}

    # --- line extraction ---
    line = None
    for k in ("total", "handicap"):
        v = odd.get(k)
        if v is None: continue
        try:
            line = float(v)
            dbg["src"].append(f"num:{k}")
            break
        except:
            pass
    if line is None:
        lab = odd.get("label")
        if isinstance(lab, (int, float, str)):
            try:
                line = float(str(lab).strip())
                dbg["src"].append("num:label")
            except:
                pass

    # textual blob for side detection
    fields = []
    for k in ("label", "name", "original_label"):
        t = odd.get(k)
        if isinstance(t, str) and t.strip():
            fields.append(t)
    p = odd.get("participants")
    if isinstance(p, str) and p.strip():
        fields.append(p)
    elif isinstance(p, dict):
        fields.append(p.get("name") or p.get("label") or "")
    elif isinstance(p, list):
        for it in p:
            if isinstance(it, dict):
                fields.append(it.get("name") or it.get("label") or "")
            elif isinstance(it, str):
                fields.append(it)

    blob = " | ".join(s.lower() for s in fields if s)
    dbg["blob"] = blob[:180]

    # --- side detection ---
    side = None
    if " under " in f" {blob} ":
        side = "under"; dbg["side"] = "under_token"
    elif " over " in f" {blob} " or " or more" in blob or " more than" in blob:
        side = "over"; dbg["side"] = "over_token"

    # Assume OVER for Player Shots market when side not explicit
    if side is None and odd.get("market_id") == MARKET_PLAYER_SHOTS:
        side = "over"; dbg["side"] = "default_ps_over"

    # Fallback: if line==0.5 and no 'under' token, default to over
    if side is None and line is not None and numeric_is_half(line) and " under " not in f" {blob} ":
        side = "over"; dbg["side"] = "default_over_at_half"

    # Last-resort numeric from any text if line still None
    if line is None:
        m = re.search(r"(\d+(?:\.\d+)?)", blob)
        if m:
            try:
                line = float(m.group(1)); dbg["src"].append("num:anytext")
            except:
                pass

    return side, line, json.dumps(dbg, ensure_ascii=False)

# ========= ML HELPERS =========
def is_home_label(label: str) -> bool:
    s = (label or "").strip().lower()
    return s in ("home", "1", "1 (home)") or "home" in s

def is_away_label(label: str) -> bool:
    s = (label or "").strip().lower()
    return s in ("away", "2", "2 (away)") or "away" in s

def team_side_for_fixture(team_id: int, meta: dict) -> Optional[str]:
    if team_id == meta.get("home_id"): return "home"
    if team_id == meta.get("away_id"): return "away"
    return None

def best_ml_from_rows(rows: Iterable[dict]) -> Tuple[Optional[float], Optional[float]]:
    best_home = None; best_away = None
    for odd in rows:
        label = (odd.get("label") or odd.get("name") or "").strip()
        price = decimals(odd)
        if price is None:
            continue
        if is_home_label(label):
            best_home = price if (best_home is None or price < best_home) else best_home
        elif is_away_label(label):
            best_away = price if (best_away is None or price < best_away) else best_away
    return best_home, best_away

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
        print("No fixtures found for next 7 days.")
        OUT_FILE.write_text("No fixtures found for next 7 days.\n", encoding="utf-8")
        return

    team_to_fixtures: Dict[int, List[dict]] = {}
    fixture_info_by_id: Dict[int, dict] = {}
    for fx in fixtures:
        meta = parse_fixture_participants(fx)
        fid = meta.get("fixture_id")
        if not isinstance(fid, int): continue
        fixture_info_by_id[fid] = meta
        for side_id in (meta.get("home_id"), meta.get("away_id")):
            if isinstance(side_id, int):
                team_to_fixtures.setdefault(side_id, []).append(meta)

    print(f"[FIXTURES] Retrieved {len(fixtures)} fixtures across {len(set([c['league_id'] for c in candidates]))} leagues (next 7 days).")

    # 3) Fetch odds per relevant fixture
    relevant_fixture_ids = set()
    for c in candidates:
        for meta in team_to_fixtures.get(c["team_id"], []) or []:
            relevant_fixture_ids.add(meta["fixture_id"])
    if not relevant_fixture_ids:
        print("[ODDS] No relevant fixtures for candidate teams.")
        OUT_FILE.write_text("No relevant fixtures for candidate teams.\n", encoding="utf-8")
        return

    odds_by_fixture: Dict[int, List[dict]] = {}
    for i, fid in enumerate(sorted(relevant_fixture_ids), start=1):
        odds = fetch_odds_for_fixture(token, fid)
        odds_by_fixture[fid] = odds or []
        time.sleep(0.20)  # be nice

    fixtures_with_odds = sum(1 for fid in relevant_fixture_ids if odds_by_fixture.get(fid))
    print(f"[ODDS] Fixtures with odds payloads: {fixtures_with_odds} / {len(relevant_fixture_ids)}")

    # 3a) DEBUG: Inspect bookmakers / markets
    bm_counter = Counter()
    bm_name_by_id: Dict[int, str] = {}
    mkt_counter = Counter()
    for fid in relevant_fixture_ids:
        for odd in odds_by_fixture.get(fid, []):
            bid = odd.get("bookmaker_id")
            if isinstance(bid, int):
                bm_counter[bid] += 1
            bobj = odd.get("bookmaker")
            # Sportmonks sometimes returns dict or str here; normalise to str
            if isinstance(bobj, dict):
                name = (bobj.get("name") or bobj.get("alias") or "").strip()
                if name:
                    bm_name_by_id[bid] = name
            elif isinstance(bobj, str):
                bm_name_by_id[bid] = bobj
            mid = odd.get("market_id")
            if isinstance(mid, int):
                mkt_counter[mid] += 1

    if bm_counter:
        top_bm = ", ".join(f"{bm_name_by_id.get(bid, str(bid))}({cnt})" for bid, cnt in bm_counter.most_common(6))
        print(f"[DEBUG] Bookmakers seen (top): {top_bm}")
    if mkt_counter:
        mkt_name_by_id: Dict[int, str] = {}
        for fid in relevant_fixture_ids:
            for odd in odds_by_fixture.get(fid, []):
                mid = odd.get("market_id"); m = odd.get("market")
                if isinstance(mid, int) and isinstance(m, dict) and (m.get("name") or m.get("developer_name")):
                    mkt_name_by_id[mid] = m.get("name") or m.get("developer_name")
                elif isinstance(mid, int) and isinstance(m, str) and m.strip():
                    mkt_name_by_id[mid] = m.strip()
        top_mkts = ", ".join(f"{mkt_name_by_id.get(mid, mid)}({cnt})" for mid, cnt in mkt_counter.most_common(12))
        print(f"[DEBUG] Top markets returned: {top_mkts}")

    # Resolve bookmaker_id by name (default "Bet365")
    resolved_bm_id: Optional[int] = None
    for bid, cnt in bm_counter.items():
        name = (bm_name_by_id.get(bid, "") or "").lower()
        if name and BOOKMAKER_NAME in name:
            resolved_bm_id = bid
            break
    if not resolved_bm_id and bm_counter:
        resolved_bm_id = bm_counter.most_common(1)[0][0]
        fallback_name = bm_name_by_id.get(resolved_bm_id, str(resolved_bm_id))
        print(f"[WARN] Could not find bookmaker '{BOOKMAKER_NAME}'. Using most common: {fallback_name} (id {resolved_bm_id}).")

    # 4) Scan odds for Player Shots Over 0.5 and apply ML filter
    flagged: List[dict] = []
    ml_debug_seen = {"preferred_ml_rows": 0, "fallback_ml_rows": 0}

    # Pre-count diagnostics
    total_ps_rows = 0
    over0_5_by_label = 0
    label_has_over = 0
    label_has_under = 0
    has_player_text = 0
    has_player_ids = 0
    fixture_samples_written = 0

    # Optional: write a few raw PS rows for inspection
    for fid in sorted(relevant_fixture_ids):
        ev_odds = odds_by_fixture.get(fid, [])
        # diagnostics counting
        for o in ev_odds:
            if o.get("market_id") != MARKET_PLAYER_SHOTS:
                continue
            total_ps_rows += 1
            if player_text_from_odd(o):
                has_player_text += 1
            # detect IDs if present anywhere commonly used
            if (isinstance(o.get("participants"), dict) and ("id" in (o.get("participants") or {}))) \
               or (o.get("participant_id") is not None) or (o.get("player_id") is not None):
                has_player_ids += 1

            side, ln, _dbg = parse_side_and_line(o)
            if side == "over" and numeric_is_half(ln):
                over0_5_by_label += 1
            blob = f"{o.get('label','')} {o.get('name','')} {o.get('original_label','')}".lower()
            if " over " in f" {blob} ":
                label_has_over += 1
            if " under " in f" {blob} ":
                label_has_under += 1

        # write small sample of PS rows for a few fixtures
        if fixture_samples_written < DEBUG_FIXTURE_SAMPLES:
            ps_rows = [o for o in ev_odds if o.get("market_id") == MARKET_PLAYER_SHOTS]
            if ps_rows:
                meta = fixture_info_by_id.get(fid, {})
                sample_path = DEBUG_DIR / f"fixture_{fid}_playershots_sample.json"
                save_json(sample_path, {
                    "fixture_id": fid,
                    "home": meta.get("home_name"),
                    "away": meta.get("away_name"),
                    "rows": ps_rows[:120]  # cap
                })
                fixture_samples_written += 1

    # Main selection loop
    for c in candidates:
        metas = team_to_fixtures.get(c["team_id"], []) or []
        for meta in metas:
            fid = meta["fixture_id"]
            ev_odds = odds_by_fixture.get(fid, [])
            if not ev_odds:
                continue

            # --- Team ML (Match Winner) ---
            best_home_ml = best_away_ml = None
            if not SKIP_ML_FILTER:
                # Preferred bookmaker rows for ML
                preferred_rows = [o for o in ev_odds if o.get("market_id") == MARKET_MATCH_WINNER
                                  and (resolved_bm_id is None or o.get("bookmaker_id") == resolved_bm_id)]
                # Fallback rows (any bookmaker) for ML if preferred not found
                fallback_rows  = [o for o in ev_odds if o.get("market_id") == MARKET_MATCH_WINNER]

                if preferred_rows:
                    ml_debug_seen["preferred_ml_rows"] += len(preferred_rows)
                    h, a = best_ml_from_rows(preferred_rows)
                    best_home_ml, best_away_ml = h, a
                if (best_home_ml is None or best_away_ml is None) and ALLOW_ML_FALLBACK_ANY and fallback_rows:
                    ml_debug_seen["fallback_ml_rows"] += len(fallback_rows)
                    h2, a2 = best_ml_from_rows(fallback_rows)
                    if best_home_ml is None: best_home_ml = h2
                    if best_away_ml is None: best_away_ml = a2

            side = team_side_for_fixture(c["team_id"], meta)
            if not side:
                continue

            team_ml = None
            if not SKIP_ML_FILTER:
                team_ml = best_home_ml if side == "home" else best_away_ml
                if team_ml is None or team_ml >= TEAM_ML_MAX:
                    continue

            # --- Player Shots Over 0.5 ---
            best_price = None
            market_seen = None

            # Respect bookmaker preference unless ALLOW_PS_FALLBACK_ANY
            if ALLOW_PS_FALLBACK_ANY:
                player_rows = [o for o in ev_odds if o.get("market_id") == MARKET_PLAYER_SHOTS]
            else:
                player_rows = [o for o in ev_odds if o.get("market_id") == MARKET_PLAYER_SHOTS
                               and (resolved_bm_id is None or o.get("bookmaker_id") == resolved_bm_id)]

            for odd in player_rows:
                who = player_text_from_odd(odd)
                if not player_label_matches(c["player"], who, NAME_MATCH_MODE):
                    continue

                side_txt, line_val, _dbg = parse_side_and_line(odd)
                if side_txt != "over":
                    continue
                if not numeric_is_half(line_val):
                    continue

                price = decimals(odd)
                if price is None:
                    continue
                if price >= MIN_PRICE and (best_price is None or price > best_price + 1e-9):
                    best_price = price
                    market_seen = (odd.get("market") or {}).get("name") or odd.get("market") or "Player Shots"

            if best_price is not None:
                home = meta.get("home_name") or "Home"
                away = meta.get("away_name") or "Away"
                flagged.append({
                    "player": c["player"], "position": c["position"], "team_id": c["team_id"],
                    "fixture": f"{home} vs {away}",
                    "kickoff": meta.get("kickoff") or "",
                    "price": best_price, "team_ml": team_ml,
                    "series": c["series"], "league_id": c["league_id"], "market": market_seen or "Player Shots",
                })

    # 5) Render
    flagged.sort(key=lambda x: (-x["price"], x["player"]))
    lines = []
    lines.append(f"Generated at (UTC): {pretty_now_iso()}  |  Min price: {MIN_PRICE:.2f}  |  Team ML < {TEAM_ML_MAX:.2f}")
    lines.append(f"Criteria: 1+ shot in 100% of last 7 (n>=7)  |  Market: {BOOKMAKER_NAME} Player Shots Over 0.5")
    lines.append("")

    # Write a small 'first_hits.json' to debug dir
    save_json(DEBUG_DIR / "first_hits.json", flagged[:25])

    if not flagged:
        lines.append("No matches found.")
        lines.append("")
        lines.append("[Notes]")
        # Bookmakers and Player Shots presence
        bm_names = sorted(set(v for v in (bm_name_by_id.values())))
        if bm_names:
            lines.append(f"- Bookmakers seen: {', '.join(bm_names)}")
        ps_present = mkt_counter.get(MARKET_PLAYER_SHOTS, 0)
        lines.append(f"- Player Shots rows: total={total_ps_rows}, over0.5_like={over0_5_by_label}, label_has_over={label_has_over}, label_has_under={label_has_under}, has_player_text={has_player_text}, has_player_ids={has_player_ids}")
        lines.append(f"- ML rows used (preferred): {ml_debug_seen['preferred_ml_rows']}  |  ML rows used (fallback any): {ml_debug_seen['fallback_ml_rows']}")
        lines.append(f"- Name match mode: {NAME_MATCH_MODE}  |  PS bookmaker fallback: {1 if ALLOW_PS_FALLBACK_ANY else 0}")
        if not ALLOW_ML_FALLBACK_ANY:
            lines.append("- Tip: Set ALLOW_ML_FALLBACK_ANY=1 to accept ML from any bookmaker when preferred ML is missing.")
        if not ALLOW_PS_FALLBACK_ANY:
            lines.append("- Tip: set ALLOW_PS_FALLBACK_ANY=1 to test Player Shots matching against any bookmaker.")
        if NAME_MATCH_MODE != "relaxed":
            lines.append("- Tip: set NAME_MATCH_MODE=relaxed to test surname-only matches.")
        if not SKIP_ML_FILTER:
            lines.append("- ML filter active (preferred bookmaker unless fallback enabled).")
        else:
            lines.append("- ML filter is DISABLED (SKIP_ML_FILTER=1).")
        if USE_HAS_ODDS:
            lines.append("- Fixtures were filtered with filters=havingOdds.")
        # dump some samples info
        lines.append(f"- Wrote up to {fixture_samples_written} fixture samples to {DEBUG_DIR}/fixture_*_playershots_sample.json")
    else:
        lines.append("===== CERTS — Player Shots 1+ =====")
        for x in flagged:
            ser = ",".join(map(str, x["series"][:7]))
            pos = f"[{x['position']}]" if x.get("position") else ""
            ml_txt = f"{x['team_ml']:.3f}" if isinstance(x.get("team_ml"), (int, float)) else "n/a"
            lines.append(
                f" • {x['player']} {pos} — {x['fixture']} | {x['kickoff']} | "
                f"Over 0.5 @ {x['price']:.3f} | Team ML {ml_txt} | series7: {ser}"
            )

    OUT_FILE.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    print("\n".join(lines))

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
