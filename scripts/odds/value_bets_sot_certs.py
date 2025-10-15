#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Value bets — SOT certs (1+ SOT)
Reads: data/player_shots_on_target/combined.json (+ predicted XI for team names)
Bookmaker: Bet365
Markets: Player Shots On Target (strictly 1+ / Over 0.5 only)

Buckets (priority; player appears once total):
  A) 7/7  -> last 7 all >=1 SOT (min n=7)
  B) 6/7  -> last 7 >=1 SOT in >=6 (min n=7, and not 7/7)

Filters:
  - Price (Over 0.5) >= MIN_DEC_PRICE (default 1.30)
  - Team ML (Bet365) < TEAM_ML_MAX (default 3.50)
  - Fixtures limited to next WINDOW_DAYS days (default 7; 0 disables)
  - Player shown once overall (best price, highest-priority bucket)

Output: data/value_bets/sot_certs.txt
Diagnostics printed to console and included in header.

ENV:
  ODDS_API_KEY (required)
  MIN_DEC_PRICE (default 1.30)
  TEAM_WIN_MAX  (default 3.50)
  WINDOW_DAYS   (default 7)
  MIN_CAPTURE_PRICE (default 1.10)
  SHOW_NEAR_MISSES  (default "1")
  ALLOW_DNB_FALLBACK (default "1")
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
MIN_CAPTURE_PRICE = float(os.getenv("MIN_CAPTURE_PRICE", "1.10"))
SHOW_NEAR_MISSES  = os.getenv("SHOW_NEAR_MISSES", "1") == "1"
ALLOW_DNB_FALLBACK = os.getenv("ALLOW_DNB_FALLBACK", "1") == "1"

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

EVENTS_API_URL = "https://api.odds-api.io/v3/events"
ODDS_MULTI_API_URL = "https://api.odds-api.io/v3/odds/multi"
HTTP_HEADERS = {"accept": "application/json", "user-agent": "odds-sot-certs/1.1"}
TIMEOUT = 25

ROOT = Path(".")
PX_DIR    = ROOT / "data" / "predicted_xi" / "by_league"
COMBINED  = ROOT / "data" / "player_shots_on_target" / "combined.json"
OUT_DIR   = ROOT / "data" / "value_bets"; OUT_DIR.mkdir(parents=True, exist_ok=True)
OUT_FILE  = OUT_DIR / "sot_certs.txt"

# ========= MARKET FILTERS (SOT-only, strict) =========
NEGATIVE_SOT_TERMS = {
    "outside", "outside box", "outside of box", "from outside", "outside the box",
    "header", "headers", "headed",
    "left foot", "right foot", "right-foot", "left-foot",
    "first half", "1st half", "2nd half", "second half", "half",
    "distance", "long range", "goal", "goals", "to score", "assist", "assists", "ga", "g/a",
    "team shots on target", "conceded", "keeper", "goalkeeper", "saves", "shots conceded",
}
def market_is_player_sot(name: str) -> bool:
    """Allow generic Player Shots On Target market only; exclude variants."""
    s = re.sub(r"\s+", " ", (name or "")).strip().lower()
    if not s:
        return False
    # must clearly be shots on target market
    if ("shots on target" not in s) and ("sot" not in s):
        return False
    # exclude variants
    if any(b in s for b in NEGATIVE_SOT_TERMS):
        return False
    # Prefer names that explicitly contain "player"
    if "player" in s:
        return True
    # Some feeds may omit "player" but still be per-player SOT
    return "shots on target" in s or "sot" in s

# ========= STRING / MATCH HELPERS =========
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
    if not a or not b: return False
    ta, tb = team_tokens(a), team_tokens(b)
    if not ta or not tb: return False
    if ta == tb: return True
    if ta.issubset(tb) or tb.issubset(ta): return True
    inter = ta & tb; union = ta | tb
    if len(inter) / max(1, len(union)) >= 0.5: return True
    if len(inter) >= 1 and ("madrid" in inter or "milan" in inter or "inter" in inter): return True
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

# ========= SERIES / HITS (STRICT RECENT WINDOW) =========
def hits_recent(seq: List[int], k: int, thresh: int = 1) -> int:
    seq = [x for x in seq if isinstance(x, int)]
    if len(seq) < k: return 0
    return sum(1 for x in seq[:k] if x >= thresh)

def qualify_buckets(on_target: List[int]) -> Tuple[bool, bool, int]:
    hits7 = hits_recent(on_target, 7, 1)
    return (hits7 == 7, (len(on_target) >= 7 and hits7 >= 6), hits7)

# ========= ODDS API =========
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
            if r.status_code == 200: return r
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
    return data if isinstance(data, list) else []

def within_next_days(ev: dict, days: int) -> bool:
    if not days: return True
    ds = ev.get("date") or ""
    try:
        dt_utc = dt.datetime.fromisoformat(ds.replace("Z", "+00:00"))
        now = dt.datetime.utcnow().replace(tzinfo=dt.timezone.utc)
        return now <= dt_utc <= (now + dt.timedelta(days=days))
    except Exception:
        return False

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
    "1x2","match result","match winner","moneyline","full time result",
    "to win","win/draw/win","wdw","ml","match odds","result","3-way","3 way",
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
        if not market_is_match_winner(nm): continue
        odds = m.get("odds") or []
        for row in odds:
            try:
                h = float(row.get("home")) if row.get("home") not in (None, "N/A") else None
                a = float(row.get("away")) if row.get("away") not in (None, "N/A") else None
            except: h = a = None
            if isinstance(h, float): best_home = h if (best_home is None or h < best_home) else best_home
            if isinstance(a, float): best_away = a if (best_away is None or a < best_away) else best_away
    used_fallback = False
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
            used_fallback = True
    return best_home, best_away, used_fallback

def parse_line(opt: dict, label: Optional[str] = None) -> Optional[float]:
    """Return the line (e.g., 0.5 / 1.5) from hdp/line or from label '(x.y)'."""
    if isinstance(opt, dict):
        for key in ("line","hdp"):
            if key in opt:
                try: return float(opt[key])
                except: pass
    lbl = label or (opt.get("label") if isinstance(opt, dict) else "")
    m = re.search(r"\(([-+]?\d+(?:\.\d+)?)\)", (lbl or ""))
    if m:
        try: return float(m.group(1))
        except: return None
    return None

# ========= MAIN =========
def main():
    api_key = os.getenv("ODDS_API_KEY")
    if not api_key:
        raise SystemExit("ERROR: ODDS_API_KEY not set.")

    # 0) Load SOT combined JSON
    blob = _load_json(COMBINED) or {}
    players = blob.get("players") or []
    if not players:
        OUT_FILE.write_text("No matches found.\n", encoding="utf-8")
        print("[RESULT] No players in combined SOT file.")
        return

    # 1) Build candidates (strict recent windows)
    cands: List[dict] = []
    for rec in players:
        seq = rec.get("on_target_last_n") or rec.get("sot_last_n") or rec.get("shots_on_target") or []
        if not isinstance(seq, list): continue
        order = (rec.get("order") or "").lower()
        series = seq if order in ("", "latest_first") else seq  # default latest_first
        is7, is6, hits7 = qualify_buckets(series)
        if not (is7 or is6): continue

        lid, tid = rec.get("league_id"), rec.get("team_id")
        if not isinstance(lid, int) or not isinstance(tid, int): continue

        team_map = _team_name_map(lid)
        team = team_map.get(tid) or rec.get("team") or rec.get("team_name")
        if not team: continue

        cands.append({
            "league_id": lid,
            "team_id": tid,
