#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Value bets — SHOTS certs (1+ in 100% of last 7, min games=7)
Source data: data/player_shots/combined.json (+ fixtures from Sportmonks)
Provider: Sportmonks only

Bookmaker preference: BOOKMAKER_NAME (default "Bet365")
 - ML can fallback to ANY bookmaker when preferred ML missing (ALLOW_ML_FALLBACK_ANY=1)
 - Player Shots can ALSO fallback to ANY bookmaker for debugging (ALLOW_PS_FALLBACK_ANY=0|1)

Markets used:
 - Player Shots (market_id=268, developer_name=PLAYER_TOTAL_SHOTS)
 - Match Winner (market_id=1, developer_name=FULLTIME_RESULT)

Filters:
  - Player qualifies: last 7 matches all >=1 shot (len(series)>=7)
  - Price Over 0.5 >= MIN_DEC_PRICE
  - Team ML (preferred bookmaker; else any bookmaker if enabled) < TEAM_WIN_MAX

Diagnostics (write to data/value_bets/debug):
  DIAG=1 enables:
    - write Player Shots sample rows per fixture (Bet365 + optional any bookmaker)
    - write candidate-level near-miss reasons
    - counters for 0.5-line detection and name/ID match rates
  NAME_MATCH_MODE=strict|relaxed (default strict)
    - strict: require last name, and check initial if present
    - relaxed: last-name only match allowed
  SKIP_ML_FILTER=0|1 (default 0). If 1, ignore ML filter entirely (debugging)
"""

import os, re, json, math, time, random, unicodedata, datetime as dt
from pathlib import Path
from typing import Dict, List, Tuple, Optional, Any, Iterable
import requests
from collections import Counter, defaultdict

# ========= CONFIG =========
BOOKMAKER_NAME = os.getenv("BOOKMAKER_NAME", "Bet365").strip().lower()
MARKET_PLAYER_SHOTS = 268  # PLAYER_TOTAL_SHOTS
MARKET_MATCH_WINNER  = 1   # FULLTIME_RESULT

MIN_PRICE = float(os.getenv("MIN_DEC_PRICE", "1.30"))
TEAM_ML_MAX = float(os.getenv("TEAM_WIN_MAX", "3.50"))

# Fallbacks/toggles
ALLOW_ML_FALLBACK_ANY  = os.getenv("ALLOW_ML_FALLBACK_ANY",  "1").lower() not in ("0","false","no")
ALLOW_PS_FALLBACK_ANY  = os.getenv("ALLOW_PS_FALLBACK_ANY",  "0").lower() not in ("0","false","no")
NAME_MATCH_MODE        = os.getenv("NAME_MATCH_MODE",        "strict").strip().lower()  # strict|relaxed
SKIP_ML_FILTER         = os.getenv("SKIP_ML_FILTER",         "0").lower() not in ("0","false","no")
DIAG                   = os.getenv("DIAG",                   "1").lower() not in ("0","false","no")
DIAG_SAMPLE_FIXTURES   = int(os.getenv("DIAG_SAMPLE_FIXTURES", "4"))

# Leagues (Sportmonks league IDs)
LEAGUE_IDS = [8,9,82,301,384,387,564,567,72,600]

BASE = "https://api.sportmonks.com/v3"
TIMEOUT = 25
HTTP_HEADERS = {"accept": "application/json", "user-agent": "sm-odds-shots-certs/diag-1.3"}

ROOT = Path(".")
DATA_DIR   = ROOT / "data"
OUT_DIR    = DATA_DIR / "value_bets"; OUT_DIR.mkdir(parents=True, exist_ok=True)
OUT_FILE   = OUT_DIR / "shots_certs.txt"
COMBINED   = DATA_DIR / "player_shots" / "combined.json"
DEBUG_DIR  = OUT_DIR / "debug"; DEBUG_DIR.mkdir(parents=True, exist_ok=True)

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

def split_name(player: str) -> Tuple[Optional[str], Optional[str]]:
    if not player: return None, None
    name2 = strip_accents(player).replace(".", " ").strip()
    parts = [p for p in name2.split() if p]
    if not parts: return None, None
    last = norm(parts[-1]); initial = None
    for p in parts[:-1]:
        ch = p.strip()[0:1]
        if ch: initial = ch.lower(); break
    return last, initial

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
    q = {"api_token": token, "include": "participants", "per_page": 50, "filters": f"leagues:{','.join(map(str, league_ids))}"}
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
    return {"fixture_id": fid, "home_id": home_id, "away_id": away_id,
            "home_name": home_name, "away_name": away_name,
            "kickoff": str(kickoff).replace("T"," ").replace("Z","")}

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

# ========= CANDIDATES =========
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

# ========= PRICE PARSERS =========
def is_over_label(odd: dict) -> bool:
    s1 = (odd.get("label") or "").strip().lower()
    s2 = (odd.get("name")  or "").strip().lower()
    s3 = (odd.get("original_label") or "").strip().lower()
    return ("over" in s1) or ("over" in s2) or ("over" in s3)

def line_is_half(odd: dict) -> bool:
    for k in ("total", "handicap"):
        v = odd.get(k)
        if v is None: continue
        try:
            if math.isclose(float(v), 0.5, abs_tol=1e-9):
                return True
        except:
            pass
    for k in ("total","handicap","original_label","label","name"):
        t = odd.get(k)
        if isinstance(t, str) and re.search(r"\b0\.5\s*\+?\b", t):
            return True
    return False

def decimals(odd: dict) -> Optional[float]:
    for k in ("dp3", "value"):
        v = odd.get(k)
        if v is None: continue
        try: return float(v)
        except: pass
    return None

# --- participants helpers (try hard) ---
def _participants_text(participants: Any) -> str:
    if isinstance(participants, str):
        return participants
    if isinstance(participants, dict):
        return participants.get("name") or participants.get("label") or ""
    if isinstance(participants, list):
        names: List[str] = []
        for it in participants:
            if isinstance(it, dict):
                nm = it.get("name") or it.get("label") or ""
                if nm: names.append(nm)
            elif isinstance(it, str):
                names.append(it)
        return ", ".join(names)
    return ""

def _participants_ids(participants: Any) -> List[int]:
    out: List[int] = []
    if isinstance(participants, dict):
        for k in ("id","participant_id","player_id"):
            v = participants.get(k)
            if isinstance(v, int): out.append(v)
    elif isinstance(participants, list):
        for it in participants:
            if isinstance(it, dict):
                for k in ("id","participant_id","player_id"):
                    v = it.get(k)
                    if isinstance(v, int): out.append(v)
    return out

def player_text_from_odd(odd: dict) -> str:
    # prefer participants if textual
    who = _participants_text(odd.get("participants"))
    if who: return who
    # try label/original_label/name as last resort
    for k in ("original_label","label","name"):
        t = odd.get(k)
        if isinstance(t, str) and t.strip():
            return t
    return ""

def player_ids_from_odd(odd: dict) -> List[int]:
    ids: List[int] = []
    # direct fields sometimes exist
    for k in ("participant_id","player_id"):
        v = odd.get(k)
        if isinstance(v, int): ids.append(v)
    # from participants array/dict
    ids.extend(_participants_ids(odd.get("participants")))
    # unique
    return sorted({i for i in ids if isinstance(i, int)})

# ========= MATCHING =========
def name_matches(player_name: str, label_text: str, mode: str) -> bool:
    last, initial = split_name(player_name)
    if not last: return False
    s = norm(cleanup_label(label_text))
    if last not in s: 
        return False
    if mode == "relaxed":
        return True
    if initial:
        # allow any word starting with initial before last name
        return bool(re.search(rf"\b{initial}\w*\b.*\b{last}\b", s)) or (s.split()[0][:1] == initial)
    return True

def odd_mentions_candidate(c: dict, odd: dict) -> Tuple[bool, str]:
    # 1) ID match (strongest)
    ids = player_ids_from_odd(odd)
    if ids and isinstance(c.get("player_id"), int) and c["player_id"] in ids:
        return True, "id_match"
    # 2) name strict
    txt = player_text_from_odd(odd)
    if txt and name_matches(c["player"], txt, "strict"):
        return True, "name_strict"
    # 3) name relaxed if enabled
    if NAME_MATCH_MODE == "relaxed" and txt and name_matches(c["player"], txt, "relaxed"):
        return True, "name_relaxed"
    return False, ""

# ========= ML HELPERS =========
def is_home_label(label: str) -> bool:
    s = (label or "").strip().lower()
    return s in ("home","1","1 (home)") or "home" in s

def is_away_label(label: str) -> bool:
    s = (label or "").strip().lower()
    return s in ("away","2","2 (away)") or "away" in s

def team_side_for_fixture(team_id: int, meta: dict) -> Optional[str]:
    if team_id == meta.get("home_id"): return "home"
    if team_id == meta.get("away_id"): return "away"
    return None

def best_ml_from_rows(rows: Iterable[dict]) -> Tuple[Optional[float], Optional[float]]:
    best_home = None; best_away = None
    for odd in rows:
        label = (odd.get("label") or odd.get("name") or "").strip()
        price = decimals(odd)
        if price is None: continue
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
        OUT_FILE.write_text("No player candidates with 1+ in each of last 7.\n", encoding="utf-8"); return

    # 2) Fixtures (next 7 days)
    today = dt.datetime.utcnow().date()
    start_date = today.strftime("%Y-%m-%d")
    end_date   = (today + dt.timedelta(days=7)).strftime("%Y-%m-%d")

    fixtures = get_fixtures_between(token, start_date, end_date, sorted(set(LEAGUE_IDS)))
    if not fixtures:
        print("No fixtures found for next 7 days.")
        OUT_FILE.write_text("No fixtures found for next 7 days.\n", encoding="utf-8"); return

    team_to_fixtures: Dict[int, List[dict]] = {}
    for fx in fixtures:
        meta = parse_fixture_participants(fx)
        fid = meta.get("fixture_id")
        if not isinstance(fid, int): continue
        for side_id in (meta.get("home_id"), meta.get("away_id")):
            if isinstance(side_id, int):
                team_to_fixtures.setdefault(side_id, []).append(meta)

    print(f"[FIXTURES] Retrieved {len(fixtures)} fixtures across {len(set([c['league_id'] for c in candidates]))} leagues (next 7 days).")

    # 3) Fetch odds for relevant fixtures
    relevant_fixture_ids = {meta["fixture_id"] for c in candidates for meta in (team_to_fixtures.get(c["team_id"], []) or [])}
    if not relevant_fixture_ids:
        print("[ODDS] No relevant fixtures for candidate teams.")
        OUT_FILE.write_text("No relevant fixtures for candidate teams.\n", encoding="utf-8"); return

    odds_by_fixture: Dict[int, List[dict]] = {}
    for i, fid in enumerate(sorted(relevant_fixture_ids), start=1):
        odds_by_fixture[fid] = fetch_odds_for_fixture(token, fid) or []
        time.sleep(0.20)  # polite

    fixtures_with_odds = sum(1 for fid in relevant_fixture_ids if odds_by_fixture.get(fid))
    print(f"[ODDS] Fixtures with odds payloads: {fixtures_with_odds} / {len(relevant_fixture_ids)}")

    # 3a) DEBUG: Inspect bookmakers / markets
    bm_counter = Counter(); bm_name_by_id: Dict[int,str] = {}
    mkt_counter = Counter()
    for fid in relevant_fixture_ids:
        for odd in odds_by_fixture.get(fid, []):
            bid = odd.get("bookmaker_id")
            if isinstance(bid, int): bm_counter[bid] += 1
            bobj = odd.get("bookmaker")
            if isinstance(bobj, dict):
                nm = (bobj.get("name") or "").strip()
                if nm: bm_name_by_id[bid] = nm
            mid = odd.get("market_id")
            if isinstance(mid, int): mkt_counter[mid] += 1
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
        top_mkts = ", ".join(f"{mkt_name_by_id.get(mid, mid)}({cnt})" for mid, cnt in mkt_counter.most_common(12))
        print(f"[DEBUG] Top markets returned: {top_mkts}")

    # Resolve bookmaker_id by name (default Bet365)
    resolved_bm_id: Optional[int] = None
    for bid, cnt in bm_counter.items():
        name = (bm_name_by_id.get(bid, "") or "").lower()
        if name and BOOKMAKER_NAME in name:
            resolved_bm_id = bid; break
    if not resolved_bm_id and bm_counter:
        resolved_bm_id = bm_counter.most_common(1)[0][0]
        fallback_name = bm_name_by_id.get(resolved_bm_id, str(resolved_bm_id))
        print(f"[WARN] Could not find bookmaker '{BOOKMAKER_NAME}'. Using most common: {fallback_name} (id {resolved_bm_id}).")

    # 4) Scan odds for Player Shots Over 0.5 and apply ML
    flagged: List[dict] = []
    ml_debug_seen = {"preferred_ml_rows": 0, "fallback_ml_rows": 0}
    diag_counts = Counter()
    diag_near_miss = defaultdict(lambda: {"had_ps_rows":0,"had_over_rows":0,"had_half_rows":0,"had_name_string":0,"had_id_in_row":0,"strict_name_hits":0,"relaxed_name_hits":0})
    diag_fixture_samples_done = 0

    for c in candidates:
        metas = team_to_fixtures.get(c["team_id"], []) or []
        for meta in metas:
            fid = meta["fixture_id"]; ev_odds = odds_by_fixture.get(fid, [])
            if not ev_odds: 
                continue

            # --- ML (Match Winner) ---
            best_home_ml = best_away_ml = None
            if not SKIP_ML_FILTER:
                preferred_rows = [o for o in ev_odds if o.get("market_id") == MARKET_MATCH_WINNER
                                  and (resolved_bm_id is None or o.get("bookmaker_id") == resolved_bm_id)]
                fallback_rows  = [o for o in ev_odds if o.get("market_id") == MARKET_MATCH_WINNER]
                if preferred_rows:
                    ml_debug_seen["preferred_ml_rows"] += len(preferred_rows)
                    h, a = best_ml_from_rows(preferred_rows); best_home_ml, best_away_ml = h, a
                if (best_home_ml is None or best_away_ml is None) and ALLOW_ML_FALLBACK_ANY and fallback_rows:
                    ml_debug_seen["fallback_ml_rows"] += len(fallback_rows)
                    h2, a2 = best_ml_from_rows(fallback_rows)
                    if best_home_ml is None: best_home_ml = h2
                    if best_away_ml is None: best_away_ml = a2

                side = team_side_for_fixture(c["team_id"], meta)
                if not side: 
                    continue
                team_ml = best_home_ml if side == "home" else best_away_ml
                if team_ml is None or team_ml >= TEAM_ML_MAX:
                    continue
            else:
                team_ml = 1.01  # neutral for debugging

            # --- Player Shots (Over 0.5) ---
            if ALLOW_PS_FALLBACK_ANY:
                player_rows = [o for o in ev_odds if o.get("market_id") == MARKET_PLAYER_SHOTS]
            else:
                player_rows = [o for o in ev_odds if o.get("market_id") == MARKET_PLAYER_SHOTS
                               and (resolved_bm_id is None or o.get("bookmaker_id") == resolved_bm_id)]

            diag_near_miss[c["player"]]["had_ps_rows"] += len(player_rows)

            best_price = None; market_seen = None; why_hit = ""
            for odd in player_rows:
                # gather diagnostics
                if is_over_label(odd): diag_near_miss[c["player"]]["had_over_rows"] += 1
                if line_is_half(odd): diag_near_miss[c["player"]]["had_half_rows"] += 1
                txt = player_text_from_odd(odd)
                if txt: diag_near_miss[c["player"]]["had_name_string"] += 1
                if player_ids_from_odd(odd): diag_near_miss[c["player"]]["had_id_in_row"] += 1

                # apply filters
                if not is_over_label(odd) or not line_is_half(odd):
                    continue
                ok, hit_why = odd_mentions_candidate(c, odd)
                if not ok:
                    continue
                if hit_why == "name_strict": diag_near_miss[c["player"]]["strict_name_hits"] += 1
                if hit_why == "name_relaxed": diag_near_miss[c["player"]]["relaxed_name_hits"] += 1

                price = decimals(odd)
                if price is None: 
                    continue
                if price >= MIN_PRICE and (best_price is None or price > best_price + 1e-9):
                    best_price = price
                    market_seen = (odd.get("market") or {}).get("name") or "Player Shots"
                    why_hit = hit_why

            if best_price is not None:
                home = meta.get("home_name") or "Home"
                away = meta.get("away_name") or "Away"
                flagged.append({
                    "player": c["player"], "position": c["position"], "team_id": c["team_id"],
                    "fixture": f"{home} vs {away}",
                    "kickoff": meta.get("kickoff") or "",
                    "price": best_price, "team_ml": team_ml,
                    "series": c["series"], "league_id": c["league_id"], "market": market_seen or "Player Shots",
                    "why": why_hit
                })

            # --- write a small sample file of PS rows per a few fixtures ---
            if DIAG and diag_fixture_samples_done < DIAG_SAMPLE_FIXTURES:
                # collect PS rows for this fixture once
                diag_fixture_samples_done += 1
                rows = []
                for odd in player_rows[:80]:  # keep sample size reasonable
                    rows.append({
                        "id": odd.get("id"),
                        "bookmaker_id": odd.get("bookmaker_id"),
                        "bookmaker": (odd.get("bookmaker") or {}).get("name"),
                        "market_id": odd.get("market_id"),
                        "market": (odd.get("market") or {}).get("name") or (odd.get("market") or {}).get("developer_name"),
                        "label": odd.get("label"),
                        "name": odd.get("name"),
                        "original_label": odd.get("original_label"),
                        "participants": odd.get("participants"),
                        "participant_id": odd.get("participant_id"),
                        "player_id": odd.get("player_id"),
                        "total": odd.get("total"),
                        "handicap": odd.get("handicap"),
                        "value": odd.get("value"),
                        "dp3": odd.get("dp3"),
                    })
                sample_path = DEBUG_DIR / f"fixture_{fid}_playershots_sample.json"
                sample_path.write_text(json.dumps({"fixture_id": fid, "home": meta.get("home_name"), "away": meta.get("away_name"), "rows": rows}, ensure_ascii=False, indent=2), encoding="utf-8")

    # 5) Render + diagnostics
    flagged.sort(key=lambda x: (-x["price"], x["player"]))
    lines = []
    lines.append(f"Generated at (UTC): {dt.datetime.utcnow().isoformat()}  |  Min price: {MIN_PRICE:.2f}  |  Team ML < {TEAM_ML_MAX:.2f}")
    bm_title = BOOKMAKER_NAME + (" (PS any bookmaker)" if ALLOW_PS_FALLBACK_ANY else "")
    lines.append(f"Criteria: 1+ shot in 100% of last 7 (n>=7)  |  Market: {bm_title} Player Shots Over 0.5")
    if SKIP_ML_FILTER: lines.append("[DEBUG] ML filter SKIPPED by SKIP_ML_FILTER=1")
    lines.append("")

    if not flagged:
        lines.append("No matches found.")
        lines.append("")
        lines.append("[Notes]")
        # quick global diagnostics
        # Count PS rows, 0.5 lines, over labels, name strings
        ps_total = ps_half = ps_over = ps_text = ps_ids = 0
        for fid in relevant_fixture_ids:
            for o in odds_by_fixture.get(fid, []):
                if o.get("market_id") != MARKET_PLAYER_SHOTS: continue
                ps_total += 1
                if line_is_half(o): ps_half += 1
                if is_over_label(o): ps_over += 1
                if player_text_from_odd(o): ps_text += 1
                if player_ids_from_odd(o): ps_ids += 1

        bm_names = sorted({v for v in bm_name_by_id.values() if v})
        if bm_names: lines.append(f"- Bookmakers seen: {', '.join(bm_names)}")
        lines.append(f"- Player Shots rows: total={ps_total}, over0.5_like={ps_half}, label_has_over={ps_over}, has_player_text={ps_text}, has_player_ids={ps_ids}")
        lines.append(f"- ML rows used (preferred): {ml_debug_seen['preferred_ml_rows']}  |  ML rows used (fallback any): {ml_debug_seen['fallback_ml_rows']}")
        lines.append(f"- Name match mode: {NAME_MATCH_MODE}  |  PS bookmaker fallback: {int(ALLOW_PS_FALLBACK_ANY)}")

        # Write candidate near-miss summary JSON for deeper inspection
        diag_json = [{"player": k, **v} for k, v in sorted(diag_near_miss.items())]
        (DEBUG_DIR / "candidate_near_miss.json").write_text(json.dumps(diag_json, ensure_ascii=False, indent=2), encoding="utf-8")
        lines.append("- Wrote candidate diagnostics: data/value_bets/debug/candidate_near_miss.json")
        lines.append(f"- Wrote up to {DIAG_SAMPLE_FIXTURES} fixture samples to data/value_bets/debug/fixture_*_playershots_sample.json")
        if not ALLOW_PS_FALLBACK_ANY:
            lines.append("- Tip: set ALLOW_PS_FALLBACK_ANY=1 to test name/ID matching with any bookmaker, not just Bet365.")
        if NAME_MATCH_MODE != "relaxed":
            lines.append("- Tip: set NAME_MATCH_MODE=relaxed to test last-name only matching.")
        if not SKIP_ML_FILTER:
            lines.append("- Tip: set SKIP_ML_FILTER=1 to confirm ML filter is not the blocker.")
    else:
        lines.append("===== CERTS — Player Shots 1+ =====")
        for x in flagged:
            ser = ",".join(map(str, x["series"][:7]))
            pos = f"[{x['position']}]" if x.get("position") else ""
            why = f" | match_by={x.get('why')}" if x.get("why") else ""
            lines.append(
                f" • {x['player']} {pos} — {x['fixture']} | {x['kickoff']} | "
                f"Over 0.5 @ {x['price']:.3f} | Team ML {x['team_ml']:.3f} | series7: {ser}{why}"
            )

    OUT_FILE.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    print("\n".join(lines))

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
