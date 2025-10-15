#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Value bets — SHOTS ON TARGET certs (Bet365)
Buckets (priority):
  A) 100% last 5 (>=1 SOT in each of most-recent 5), min n=5
  B) 6 of last 7 (>=1 SOT in at least 6 of most-recent 7), min n=7

Filters:
  - Bookmaker: Bet365 only
  - Market: Player Shots On Target Over 0.5 (NOT halves / outside box / etc.)
  - Team ML (Bet365) for player's side < TEAM_ML_MAX (default 3.50)
  - Price for Over 0.5 >= MIN_DEC_PRICE (default 1.30)
  - Events limited to next WINDOW_DAYS days (default 7)
  - Player appears ONCE total (prefer bucket A, then B; within bucket pick highest price)

Output: data/value_bets/sot_certs.txt
Diagnostics printed to console and included in file header.

ENV expected:
  ODDS_API_KEY (required)
  MIN_DEC_PRICE (default 1.30)
  TEAM_WIN_MAX  (default 3.50)
  WINDOW_DAYS   (default 7)
  ALLOW_DNB_FALLBACK (default "0")  # if "1", use Draw No Bet prices when ML missing
"""

import os, re, json, math, time, random, unicodedata, datetime as dt
from pathlib import Path
from typing import Dict, List, Tuple, Optional, Any
from itertools import islice
import requests

# ========= CONFIG =========
SPORT = "football"
BOOKMAKERS = "Bet365"
MIN_PRICE = float(os.getenv("MIN_DEC_PRICE", "1.30"))
TEAM_ML_MAX = float(os.getenv("TEAM_WIN_MAX", "3.50"))
WINDOW_DAYS = int(os.getenv("WINDOW_DAYS", "7"))
ALLOW_DNB_FALLBACK = os.getenv("ALLOW_DNB_FALLBACK", "0") == "1"

EVENTS_API_URL = "https://api.odds-api.io/v3/events"
ODDS_MULTI_API_URL = "https://api.odds-api.io/v3/odds/multi"
HTTP_HEADERS = {"accept": "application/json", "user-agent": "odds-sot-certs/1.2"}
TIMEOUT = 25

# SportMonks league ids -> odds-api slugs
LEAGUE_SLUG_BY_ID = {
    8:   "england-premier-league",
    9:   "england-championship",
    82:  "germany-bundesliga",
    301: "france-ligue-1",
    384: "italy-serie-a",
    387: "italy-serie-b",
    564: "spain-laliga",
    567: "spain-laliga-2",
    72:  "netherlands-eredivisie",
    600: "turkiye-super-lig",
}

ROOT = Path(".")
SOT_COMBINED = ROOT / "data" / "player_shots_on_target" / "combined.json"
PX_DIR       = ROOT / "data" / "predicted_xi" / "by_league"
OUT_DIR      = ROOT / "data" / "value_bets"; OUT_DIR.mkdir(parents=True, exist_ok=True)
OUT_FILE     = OUT_DIR / "sot_certs.txt"

# ========= MARKET FILTERS =========
NEGATIVE_TERMS = {
    "outside","outside box","outside of box","from outside","outside the box",
    "first half","1st half","second half","2nd half","half",
    "distance","long range",
    "header","headers","headed",
    "left foot","right foot","right-foot","left-foot",
    "goal","goals","to score","assist","assists","ga","g/a",
    "corner","corners","free kick","penalty","penalties",
    "team shots on target","shots conceded","goalkeeper","keeper saves","saves",
}
def market_is_player_sot(name: str) -> bool:
    s = re.sub(r"\s+", " ", (name or "")).strip().lower()
    if not s:
        return False
    # require it's a player market and refers to SOT
    if "player" not in s:
        return False
    if ("on target" not in s) and ("sot" not in s) and not ("shot" in s and "target" in s):
        return False
    if any(bad in s for bad in NEGATIVE_TERMS):
        return False
    return True

# ========= TEXT / MATCH HELPERS =========
def strip_accents(s: str) -> str:
    return ''.join(c for c in unicodedata.normalize('NFD', s or '') if unicodedata.category(c) != 'Mn')

def norm(s: str) -> str:
    s = strip_accents(s or "").lower()
    s = re.sub(r"[^a-z0-9\s\.-]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s

TEAM_ALIAS = {
    "man city": "manchester city",
    "man utd": "manchester united",
    "man united": "manchester united",
    "spurs": "tottenham hotspur",
    "wolves": "wolverhampton wanderers",
    "west brom": "west bromwich albion",
    "psv": "psv eindhoven",
    "st pauli": "fc st. pauli",
    "inter milan": "inter milano",
    "bayern munich": "bayern münchen",
    "newcastle": "newcastle united",
    "forest": "nottingham forest",
    "betis": "real betis",
    "sociedad": "real sociedad",
    "celta": "rc celta de vigo",
    "deportivo alaves": "deportivo alavés",
}
def expand_alias(name: str) -> str:
    s = norm(name)
    for k, v in TEAM_ALIAS.items():
        if k in s:
            return v
    return s

GENERIC_TEAM_TOKENS = {
    "fc","cf","afc","sc","cd","ud","ac","as","ss","ssc","us","uc","rc","rcd","ca",
    "the","club","de","del","la","las","los","calcio","united","city","saint","st","bk"
}
def team_tokens(name: str):
    s = expand_alias(name)
    toks = set(s.split())
    return {t for t in toks if t not in GENERIC_TEAM_TOKENS}

def team_names_match(a: str, b: str) -> bool:
    if not a or not b: 
        return False
    ta, tb = team_tokens(a), team_tokens(b)
    if not ta or not tb: 
        return False
    if ta == tb or ta.issubset(tb) or tb.issubset(ta):
        return True
    inter = ta & tb; union = ta | tb
    if len(inter) / max(1, len(union)) >= 0.5:
        return True
    if len(inter) >= 1 and ("madrid" in inter or "milan" in inter or "inter" in inter):
        return True
    return False

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

def player_label_matches(player: str, option_label: str) -> bool:
    if not player or not option_label: return False
    last, initial = extract_last_name_initial(player)
    label = norm(cleanup_label(option_label))
    if not last or last not in label: return False
    if initial:
        first_word_initial = label.split()[0][0:1] if label.split() else None
        if first_word_initial and first_word_initial == initial: return True
        return bool(re.search(rf"\b{initial}\w*\b.*\b{last}\b", label))
    return True

# ========= IO HELPERS =========
def _load_json(p: Path) -> Any:
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None

def _team_name_map(league_id: int) -> Dict[int, str]:
    blob = _load_json(PX_DIR / f"{league_id}.json") or {}
    m: Dict[int, str] = {}
    for fx in (blob.get("fixtures") or []):
        for side in ("home", "away"):
            s = fx.get(side) or {}
            tid, nm = s.get("team_id"), s.get("name")
            if isinstance(tid, int) and isinstance(nm, str) and nm:
                m.setdefault(tid, nm)
    return m

SERIES_KEYS = ("shots_on_target_last_n","sot_last_n","series_sot","on_target_last_n","series")
def series_from_rec(rec: dict) -> List[int]:
    for k in SERIES_KEYS:
        v = rec.get(k)
        if isinstance(v, list):
            return [int(x) if isinstance(x, (int,float)) else 0 for x in v]
    return []

def ensure_latest_first(series: List[int], rec: dict) -> List[int]:
    order = (rec.get("order") or "").lower()
    return list(reversed(series)) if order == "oldest_first" else series

def last_k_counts(series: List[int], k: int) -> Tuple[int, List[int]]:
    seq = [x for x in series if isinstance(x, int)]
    if len(seq) < k:
        return 0, []
    sub = seq[:k]
    return sum(1 for x in sub if x >= 1), sub

# ========= HTTP / ODDS =========
def chunked(it, n):
    it = iter(it)
    while True:
        batch = list(islice(it, n))
        if not batch: return
        yield batch

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

def get_events_for_league(slug: str, api_key: str) -> List[dict]:
    r = http_get_with_retries(EVENTS_API_URL, {"apiKey": api_key, "sport": SPORT, "league": slug})
    if not (r and r.status_code == 200): return []
    try: data = r.json()
    except: data = None
    if not isinstance(data, list): return []
    now = dt.datetime.utcnow(); end = now + dt.timedelta(days=WINDOW_DAYS)
    out = []
    for ev in data:
        d = ev.get("date")
        try:
            when = dt.datetime.fromisoformat(d.replace("Z","+00:00")) if isinstance(d, str) else None
        except:
            when = None
        if when and now <= when <= end:
            out.append(ev)
    return out

def get_odds_multi(event_ids: List[int], api_key: str) -> List[dict]:
    if not event_ids: return []
    r = http_get_with_retries(ODDS_MULTI_API_URL, {
        "apiKey": api_key, "eventIds": ",".join(map(str, event_ids)), "bookmakers": BOOKMAKERS
    })
    if not (r and r.status_code == 200): return []
    try: data = r.json()
    except: return []
    return data if isinstance(data, list) else []

def bet365_markets(ev: dict):
    for bm_name, markets in (ev.get("bookmakers") or {}).items():
        if "bet365" not in (bm_name or "").lower(): continue
        for m in markets or []:
            yield m

MATCH_WINNER_KEYS = [
    "1x2","match result","match winner","moneyline","full time result","to win",
    "win/draw/win","wdw","ml","match odds","result","3-way","3 way",
    "90 minutes","regular time result"
]
DNB_KEYS = ["draw no bet","dnb"]

def market_is_match_winner(name: str) -> bool:
    s = (name or "").strip().lower()
    return bool(s) and any(k in s for k in MATCH_WINNER_KEYS)

def market_is_dnb(name: str) -> bool:
    s = (name or "").strip().lower()
    return bool(s) and any(k in s for k in DNB_KEYS)

def min_win_prices(ev: dict) -> Tuple[Optional[float], Optional[float], bool]:
    best_home = None; best_away = None
    for m in bet365_markets(ev):
        nm = m.get("name","")
        if not market_is_match_winner(nm):
            continue
        odds = m.get("odds") or []
        for row in odds:
            try:
                h = float(row.get("home")) if row.get("home") not in (None, "N/A") else None
                a = float(row.get("away")) if row.get("away") not in (None, "N/A") else None
            except: h = a = None
            if isinstance(h, float): best_home = h if (best_home is None or h < best_home) else best_home
            if isinstance(a, float): best_away = a if (best_away is None or a < best_away) else best_away
    used_fb = False
    if (best_home is None or best_away is None) and ALLOW_DNB_FALLBACK:
        for m in bet365_markets(ev):
            if not market_is_dnb(m.get("name","")): continue
            odds = m.get("odds") or []
            for row in odds:
                try:
                    h = float(row.get("home")) if row.get("home") not in (None, "N/A") else None
                    a = float(row.get("away")) if row.get("away") not in (None, "N/A") else None
                except: h = a = None
                if isinstance(h, float) and best_home is None: best_home = h
                if isinstance(a, float) and best_away is None: best_away = a
        if best_home is not None or best_away is not None:
            used_fb = True
    return best_home, best_away, used_fb

# ----- Over 0.5 SOT parsing -----
PLUS_PAT     = re.compile(r"\b(\d+)\s*\+\b")            # "1+" / "2+"
OR_MORE_PAT  = re.compile(r"\b(\d+)\s*or\s*more\b")     # "1 or more"
PAREN_PAT    = re.compile(r"\(([-+]?\d+(?:\.\d+)?)\)")
def parse_line_any(opt: dict) -> Optional[float]:
    if isinstance(opt, dict) and "hdp" in opt:
        try: return float(opt["hdp"])
        except: pass
    label = (opt.get("label") if isinstance(opt, dict) else None) or ""
    m = PAREN_PAT.search(label)
    if m:
        try: return float(m.group(1))
        except: pass
    lab = label.lower()
    m = PLUS_PAT.search(lab) or OR_MORE_PAT.search(lab)
    if m:
        try:
            base = float(m.group(1))
            return base - 0.5  # map "1+" -> 0.5, "2+" -> 1.5
        except: return None
    return None

def parse_over_price(opt: dict) -> Optional[float]:
    try:
        return float(opt.get("over")) if isinstance(opt, dict) else None
    except:
        return None

# ========= MAIN =========
def main():
    api_key = os.getenv("ODDS_API_KEY")
    if not api_key:
        raise SystemExit("ERROR: ODDS_API_KEY not set.")

    # 1) Load SOT histories
    blob = _load_json(SOT_COMBINED) or {}
    players = blob.get("players") or blob.get("rows") or blob.get("data") or []
    if not isinstance(players, list):
        players = []
    total_players = len(players)

    # 2) Build candidates (Bucket A then B)
    team_name_cache: Dict[int, Dict[int, str]] = {}
    cand_A, cand_B = [], []
    for rec in players:
        series = ensure_latest_first(series_from_rec(rec), rec)
        if not series:
            continue
        league_id = rec.get("league_id"); team_id = rec.get("team_id")
        if not isinstance(league_id, int) or not isinstance(team_id, int):
            continue

        team = rec.get("team") or rec.get("team_name") or ""
        if not team:
            if league_id not in team_name_cache:
                team_name_cache[league_id] = _team_name_map(league_id)
            team = team_name_cache[league_id].get(team_id, "")
        if not team:
            continue

        player = rec.get("name") or rec.get("player_name") or rec.get("player")
        if not player:
            continue
        pos = rec.get("position_tag") or rec.get("position") or ""

        n_all = len([x for x in series if isinstance(x, int)])
        c5, w5 = last_k_counts(series, 5) if n_all >= 5 else (0, [])
        c7, w7 = last_k_counts(series, 7) if n_all >= 7 else (0, [])

        if n_all >= 5 and c5 == 5:
            cand_A.append({"league_id": league_id, "team_id": team_id, "team": team,
                           "player": player, "position": pos, "series": series[:10],
                           "c5": c5, "w5": w5, "c7": c7, "w7": w7})
        elif n_all >= 7 and c7 >= 6:
            cand_B.append({"league_id": league_id, "team_id": team_id, "team": team,
                           "player": player, "position": pos, "series": series[:10],
                           "c5": c5, "w5": w5, "c7": c7, "w7": w7})

    print(f"[STATS] total_players={total_players}  bucketA(100% last5)={len(cand_A)}  bucketB(6/7 last7)={len(cand_B)}")

    if not cand_A and not cand_B:
        _render([], [], reason="No players satisfied SOT windows")
        return

    # 3) Fetch events (next WINDOW_DAYS)
    need_lids = sorted({c["league_id"] for c in (cand_A + cand_B)})
    slugs = {lid: LEAGUE_SLUG_BY_ID.get(lid) for lid in need_lids if LEAGUE_SLUG_BY_ID.get(lid)}
    events_by_league: Dict[int, List[dict]] = {}
    total_events = 0
    for lid, slug in slugs.items():
        evs = get_events_for_league(slug, api_key)
        events_by_league[lid] = evs
        total_events += len(evs)
        print(f"[EVENTS] {slug}: {len(evs)} (next {WINDOW_DAYS}d)")
    if total_events == 0:
        _render([], [], reason="No events in window")
        return

    # 4) Map candidates → event ids
    def map_events(c
