#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Top 10 Scorers (Combined) — Bet365 Anytime
Outputs a single, concise list across the Top 5 leagues + optional table.

List line format:
"1) H. Kane — 12 — Bayern — @ 1.50 — v Leverkusen."

ENV:
  SPORTMONKS_TOKEN    (required)
  OUTPUT_PATH         (default: posts/top10_scorers_anytime.md)
  OUTPUT_TABLE_PATH   (optional; if set, also writes a table MD)
  DEBUG               (optional, "1" for verbose)
"""

import os, re, json, unicodedata, datetime as dt
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
import requests

# ---------------- Config ----------------
BASE   = "https://api.sportmonks.com/v3/football"
TOKEN  = os.getenv("SPORTMONKS_TOKEN")
if not TOKEN:
    raise SystemExit("Missing SPORTMONKS_TOKEN")

DEBUG  = os.getenv("DEBUG") == "1"
OUT_LIST  = Path(os.getenv("OUTPUT_PATH", "posts/top10_scorers_anytime.md"))
OUT_TABLE = os.getenv("OUTPUT_TABLE_PATH")  # optional path; when set we write a table too
MAX_ROWS = 10

# Top 5 leagues
LEAGUES = {
    8:   ("Premier League", "PL"),
    564: ("LaLiga",         "LAL"),
    82:  ("Bundesliga",     "BUN"),
    384: ("Serie A",        "SA"),
    301: ("Ligue 1",        "L1"),
}

# Local data (optional, for fixture/odds match)
ROOT         = Path(".")
FIX_DIR      = ROOT / "data" / "fixtures" / "by_league"
ODDS_DIR_MAIN= ROOT / "data" / "odds" / "b365" / "by_league"
ODDS_DIR_ALT = ROOT / "data" / "odds" / "b365"

# Bet365 identifiers
BOOKMAKER_B365        = 2
MARKET_GOALSCORERS    = 90

# ---------------- String helpers ----------------
SUFFIXES = {"jr","junior","sr","senior","ii","iii","iv","filho","neto"}

def strip_accents(s: str) -> str:
    import unicodedata
    return ''.join(c for c in unicodedata.normalize('NFD', s or '') if unicodedata.category(c) != 'Mn')

def norm_ws_lower(s: str) -> str:
    s = strip_accents(s or "").lower()
    s = re.sub(r"\s+", " ", s).strip()
    return s

def norm(s: str) -> str:
    s = strip_accents(s or "").lower()
    s = re.sub(r"[^a-z0-9\s\.-]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s

def cleanup_label_end_parens(label: str) -> str:
    return re.sub(r"(?:\s*\([^)]*\))+$", "", label or "").strip()

def tokenize_name(name: str) -> List[str]:
    return [t for t in re.split(r"[\s\-]+", norm(name)) if t]

def core_tokens(name: str) -> List[str]:
    toks = tokenize_name(name)
    while toks and toks[-1] in SUFFIXES:
        toks = toks[:-1]
    return toks

def extract_first_last(name: str) -> Tuple[Optional[str], Optional[str]]:
    toks = core_tokens(name)
    if not toks: return None, None
    first = toks[0]
    last  = toks[-1] if len(toks) > 1 else toks[0]
    return first, last

def short_player_display(name: str) -> str:
    """E. Haaland — keep last token from original, first initial from original."""
    raw = (name or "").strip()
    if not raw:
        return ""
    parts = [p for p in re.split(r"\s+", raw) if p]
    while parts and norm(parts[-1]) in SUFFIXES:
        parts.pop()
    last = parts[-1] if parts else raw
    first_initial = parts[0][0:1] if parts else ""
    return f"{first_initial}. {last}"

# ---------------- Pretty team names ----------------
GENERIC_TEAM_TOKENS = {
    "fc","cf","afc","sc","cd","ud","ac","as","ss","ssc","us","uc","rc","rcd","ca",
    "the","club","de","del","la","las","los","calcio","saint","st","bk",
    "saint-germain","saintgermain","psg","united","city"
}
def team_tokens(name: str):
    toks = set(norm(name).split())
    return {t for t in toks if t not in GENERIC_TEAM_TOKENS}

def team_names_match(a: str, b: str) -> bool:
    if not a or not b: return False
    ta, tb = team_tokens(a), team_tokens(b)
    if not ta or not tb: return False
    if ta == tb or ta.issubset(tb) or tb.issubset(ta): return True
    inter = ta & tb; union = ta | tb
    return (len(inter) / max(1,len(union)) >= 0.5) or (len(inter) >= 2)

def pretty_map() -> Dict[str, str]:
    M: Dict[str, str] = {}
    def add(names, pretty):
        for n in names:
            M[norm_ws_lower(n)] = pretty
    # PL
    add(["Manchester City"], "Man City")
    add(["Manchester United"], "Man Utd")
    add(["Newcastle United"], "Newcastle")
    add(["Nottingham Forest"], "Nottm Forest")
    add(["Brighton & Hove Albion"], "Brighton")
    add(["Tottenham Hotspur"], "Spurs")
    add(["Wolverhampton Wanderers"], "Wolves")
    add(["West Ham United"], "West Ham")
    add(["AFC Bournemouth"], "Bournemouth")
    add(["Leeds United"], "Leeds")
    add(["Leicester City"], "Leicester")
    # LaLiga
    add(["Atlético Madrid","Atletico Madrid"], "Atleti")
    add(["FC Barcelona","Barcelona"], "Barcelona")
    add(["Real Sociedad"], "Sociedad")
    add(["Rayo Vallecano"], "Rayo")
    add(["Deportivo Alavés","Alaves"], "Alaves")
    # Bundesliga
    add(["FC Bayern München","Bayern Munchen","Bayern München","FC Bayern Munchen"], "Bayern")
    add(["Borussia Dortmund"], "Dortmund")
    add(["Borussia Mönchengladbach","Borussia Monchengladbach"], "Gladbach")
    add(["Bayer 04 Leverkusen"], "Leverkusen")
    add(["Eintracht Frankfurt"], "Frankfurt")
    add(["Werder Bremen","SV Werder Bremen"], "Bremen")
    add(["Union Berlin","1. FC Union Berlin","FC Union Berlin"], "Union Berlin")
    # Serie A
    add(["AC Milan","Milan"], "Milan")
    add(["Internazionale","Inter"], "Inter")
    add(["AS Roma","Roma"], "Roma")
    add(["SS Lazio","Lazio"], "Lazio")
    # Ligue 1
    add(["Paris Saint-Germain","Paris Saint Germain","PSG"], "PSG")
    add(["Olympique Marseille","Marseille"], "Marseille")
    add(["Olympique Lyonnais","Lyon"], "Lyon")
    return M

PRETTY = pretty_map()

def pretty_team(name: Optional[str]) -> str:
    if not name:
        return "TBC"
    key = norm_ws_lower(name)
    if key in PRETTY:
        return PRETTY[key]
    base = strip_accents(name).replace("&", " ")
    base = re.sub(r"\b(?:FC|CF|AC|AS|UD|CD|RC|RCD|US|UC|BK)\b\.?", "", base, flags=re.I)
    base = re.sub(r"\s+", " ", base).strip()
    base = re.sub(r"\bUnited\b", "", base).strip()
    return base

# ---------------- Time / IO ----------------
def parse_dt_utc(s: Optional[str]) -> Optional[dt.datetime]:
    if not s: return None
    s2 = s.replace(" UTC", "")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return dt.datetime.strptime(s2, fmt)
        except Exception:
            pass
    return None

def _read_json(p: Path) -> Any:
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None

# ---------------- Sportmonks: leaders ----------------
def get_current_season(league_id: int) -> int:
    url = f"{BASE}/leagues/{league_id}"
    r = requests.get(url, params={"api_token": TOKEN, "include": "currentSeason"}, timeout=30)
    r.raise_for_status()
    data = r.json()["data"]
    season = data.get("currentseason") or data.get("currentSeason")
    if not season:
        raise RuntimeError(f"No currentSeason for league {league_id}")
    return int(season["id"])

def fetch_topscorers_via_endpoint(season_id: int):
    url = f"{BASE}/topscorers/seasons/{season_id}"
    params = {
        "api_token": TOKEN,
        "include": "player;participant;type",
        "filters": "seasonTopscorerTypes:208",
        "per_page": 50,
        "order": "asc",
    }
    return requests.get(url, params=params, timeout=30)

def fetch_topscorers_via_season_include(season_id: int):
    url = f"{BASE}/seasons/{season_id}"
    params = {
        "api_token": TOKEN,
        "include": "topscorers.player;topscorers.participant;topscorers.type",
        "filters": "seasonTopscorerTypes:208",
        "per_page": 50,
    }
    return requests.get(url, params=params, timeout=30)

def parse_topscorers(payload):
    if isinstance(payload.get("data"), dict) and "topscorers" in payload["data"]:
        items = payload["data"]["topscorers"] or []
    else:
        items = payload.get("data", []) or []
    agg = {}
    for row in items:
        player = row.get("player") or (row.get("topscorer") or {}).get("player")
        team   = row.get("participant") or (row.get("topscorer") or {}).get("participant")
        total  = row.get("total") or (row.get("value") or {}).get("total")
        if not player or total is None:
            continue
        pid = player.get("id"); tid = (team or {}).get("id")
        key = (pid, tid)
        entry = agg.setdefault(key, {
            "player": player.get("display_name") or player.get("fullname") or player.get("name"),
            "team":   (team or {}).get("name", "—"),
            "total":  0
        })
        try:
            entry["total"] += int(total)
        except Exception:
            pass
    return sorted(agg.values(), key=lambda x: (-x["total"], x["player"]))[:10]

# ---------------- Fixtures ----------------
def parse_fixture_teams(fixture_name: str) -> Tuple[str, str]:
    if not fixture_name: return "", ""
    for sep in [" vs ", " v ", " VS ", " Vs "]:
        if sep in fixture_name:
            a,b = fixture_name.split(sep,1); return a.strip(), b.strip()
    if " - " in fixture_name:
        a,b = fixture_name.split(" - ",1); return a.strip(), b.strip()
    return "", ""

def load_fixtures_for_league(league_id: int) -> List[dict]:
    blob = _read_json(FIX_DIR / f"{league_id}.json") or {}
    return blob.get("fixtures") or (blob.get("data") or {}).get("fixtures") or []

def find_next_fixture_for_team(team_name: str, fixtures: List[dict]) -> Optional[dict]:
    now = dt.datetime.utcnow(); best = None
    for fx in fixtures:
        name = fx.get("name") or ""
        home, away = parse_fixture_teams(name)
        if not home or not away: 
            continue
        if not (team_names_match(team_name, home) or team_names_match(team_name, away)):
            continue
        ko = parse_dt_utc(fx.get("starting_at"))
        if not ko:
            ts = fx.get("starting_at_timestamp")
            if isinstance(ts,(int,float)):
                try: ko = dt.datetime.utcfromtimestamp(int(ts))
                except Exception: pass
        if ko and ko >= now:
            if best is None or ko < best[0]:
                best = (ko, fx)
    return best[1] if best else None

# ---------------- Odds: Bet365 Anytime ----------------
ANYTIME_EXACT = {"anytime","any time","to score","to score (anytime)"}
ANYTIME_BLOCK = {
    "first","last","2 or more","two or more","brace","hat trick","hat-trick","treble",
    "header","left foot","right foot","penalty","free kick","assist","to assist","card",
    "yellow","red","shots","shot","on target","sot","score 2+","score two+",
    "score two or more","to score 2","to score two"
}

def label_is_anytime_strict(raw_label: str) -> bool:
    if not raw_label: return False
    l = norm_ws_lower(raw_label)
    for bad in ANYTIME_BLOCK:
        if bad in l: return False
    return l in ANYTIME_EXACT

def label_is_anytime_lenient(raw_label: str) -> bool:
    if not raw_label: return False
    l = norm_ws_lower(raw_label)
    for bad in ANYTIME_BLOCK:
        if bad in l: return False
    return ("anytime" in l or "any time" in l or l.startswith("to score"))

def iter_odds_fixtures(odds_blob: dict) -> List[dict]:
    if isinstance(odds_blob, list): 
        return odds_blob
    if isinstance(odds_blob, dict):
        if isinstance(odds_blob.get("fixtures"), list):
            return odds_blob["fixtures"]
        data = odds_blob.get("data")
        if isinstance(data, dict) and isinstance(data.get("fixtures"), list):
            return data["fixtures"]
    return []

def load_odds_for_league(league_id: int) -> dict:
    p1 = ODDS_DIR_MAIN / f"{league_id}.json"
    if p1.exists(): return _read_json(p1) or {}
    p2 = ODDS_DIR_ALT / f"{league_id}.json"
    if p2.exists(): return _read_json(p2) or {}
    return {}

def _parse_latest_ts(s: Optional[str]) -> Optional[dt.datetime]:
    if not s: return None
    try:
        return dt.datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
    except Exception:
        return None

def _choose_price(cands: List[Tuple[float, Optional[dt.datetime], str]]) -> Optional[float]:
    if not cands: 
        return None
    latest = None
    for _, ts, _ in cands:
        if ts and (latest is None or ts > latest):
            latest = ts
    if latest:
        cands = [c for c in cands if c[1] == latest]
    return min(cands, key=lambda x: x[0])[0]

def player_label_matches(player: str, option_name_or_label: str) -> bool:
    if not player or not option_name_or_label: 
        return False
    label  = norm(cleanup_label_end_parens(option_name_or_label))
    p_norm = norm(player)
    first, last = extract_first_last(player)
    if last and len(last) >= 3 and last in label:
        if first:
            ini = first[0:1]
            if re.search(rf"\b{ini}\w*\b.*\b{last}\b", label) or first in label:
                return True
        else:
            return True
    # fallback heuristics
    parts = tokenize_name(player)
    for p in parts:
        if len(p) >= 5 and p in label:
            if (last and last in label) or sum(1 for t in parts if len(t) >= 5 and t in label) >= 2:
                return True
    core = [t for t in core_tokens(player) if len(t) >= 4]
    if sum(1 for t in core if t in label) >= 2: 
        return True
    if first and len(first) >= 6 and first in label: 
        return True
    return False

def best_anytime_goalscorer_price(odds_rows: List[dict], player: str) -> Optional[float]:
    strict_cands: List[Tuple[float, Optional[dt.datetime], str]] = []
    lenient_cands: List[Tuple[float, Optional[dt.datetime], str]] = []
    for o in odds_rows or []:
        try:
            if int(o.get("bookmaker_id", 0)) != BOOKMAKER_B365: 
                continue
            if int(o.get("market_id", 0)) != MARKET_GOALSCORERS: 
                continue
        except Exception:
            continue
        if o.get("stopped"):
            continue
        raw_label = str(o.get("label") or "")
        if not (label_is_anytime_strict(raw_label) or label_is_anytime_lenient(raw_label)):
            continue
        candidate = o.get("name") or o.get("original_label") or o.get("total") or ""
        if not player_label_matches(player, candidate):
            continue
        try:
            price = float(str(o.get("value")))
        except Exception:
            continue
        ts = _parse_latest_ts(o.get("latest_bookmaker_update"))
        (strict_cands if label_is_anytime_strict(raw_label) else lenient_cands).append((price, ts, raw_label))
    best = _choose_price(strict_cands) or _choose_price(lenient_cands)
    if DEBUG and best is None:
        seen = [norm_ws_lower(str(x.get("label"))) for x in (odds_rows or [])]
        print(f"[DEBUG] NO ANYTIME MATCH for '{player}'. Labels seen: {sorted(set(seen))[:8]}")
    return best

# ---------------- Output helpers ----------------
def header_lines() -> List[str]:
    return ["Top 10 Scorers — Bet365 Anytime", ""]

# ---------------- Main ----------------
def main():
    OUT_LIST.parent.mkdir(parents=True, exist_ok=True)
    if OUT_TABLE:
        Path(OUT_TABLE).parent.mkdir(parents=True, exist_ok=True)

    # Gather leaders across all leagues
    combined: List[dict] = []
    fixtures_by_league: Dict[int, List[dict]] = {}
    odds_by_league: Dict[int, dict] = {}

    for league_id, (league_name, abbr) in LEAGUES.items():
        try:
            # leaders
            url = f"{BASE}/leagues/{league_id}"
            r = requests.get(url, params={"api_token": TOKEN, "include": "currentSeason"}, timeout=30)
            r.raise_for_status()
            season = (r.json()["data"].get("currentseason") or r.json()["data"].get("currentSeason"))["id"]
            sid = int(season)

            q = requests.get(
                f"{BASE}/topscorers/seasons/{sid}",
                params={"api_token": TOKEN, "include":"player;participant;type", "filters":"seasonTopscorerTypes:208", "per_page":50, "order":"asc"},
                timeout=30
            )
            if q.status_code == 404:
                q = requests.get(
                    f"{BASE}/seasons/{sid}",
                    params={"api_token": TOKEN, "include":"topscorers.player;topscorers.participant;topscorers.type", "filters":"seasonTopscorerTypes:208", "per_page":50},
                    timeout=30
                )
            q.raise_for_status()
            payload = q.json()
            # parse
            if isinstance(payload.get("data"), dict) and "topscorers" in payload["data"]:
                items = payload["data"]["topscorers"] or []
            else:
                items = payload.get("data", []) or []
            agg = {}
            for row in items:
                player = row.get("player") or (row.get("topscorer") or {}).get("player")
                team   = row.get("participant") or (row.get("topscorer") or {}).get("participant")
                total  = row.get("total") or (row.get("value") or {}).get("total")
                if not player or total is None:
                    continue
                pid = player.get("id"); tid = (team or {}).get("id")
                key = (pid, tid)
                e = agg.setdefault(key, {
                    "player": player.get("display_name") or player.get("fullname") or player.get("name"),
                    "team":   (team or {}).get("name", "—"),
                    "total":  0,
                    "league_id": league_id
                })
                try: e["total"] += int(total)
                except Exception: pass
            leaders = sorted(agg.values(), key=lambda x: (-x["total"], x["player"]))[:10]
        except Exception as e:
            if DEBUG:
                print(f"[WARN] leaders {league_id}: {e}")
            leaders = []

        fixtures = (lambda lid=league_id: ( _read_json(FIX_DIR / f"{lid}.json") or {} ))()
        fixtures = fixtures.get("fixtures") or (fixtures.get("data") or {}).get("fixtures") or []
        odds_blob = _read_json(ODDS_DIR_MAIN / f"{league_id}.json") or _read_json(ODDS_DIR_ALT / f"{league_id}.json") or {}

        fixtures_by_league[league_id] = fixtures
        odds_by_league[league_id] = odds_blob

        for row in leaders:
            combined.append({
                "league_id": row["league_id"],
                "player": row["player"],
                "player_disp": short_player_display(row["player"]),
                "team": row["team"],
                "team_disp": pretty_team(row["team"]),
                "goals": int(row["total"]),
            })

    # Rank by goals desc, then player name; take Top 10 overall
    combined.sort(key=lambda r: (-r["goals"], r["player"]))
    topN = combined[:MAX_ROWS]

    # Enrich with opponent + price
    def parse_fixture_teams(fixture_name: str) -> Tuple[str, str]:
        if not fixture_name: return "", ""
        for sep in [" vs ", " v ", " VS ", " Vs "]:
            if sep in fixture_name:
                a,b = fixture_name.split(sep,1); return a.strip(), b.strip()
        if " - " in fixture_name:
            a,b = fixture_name.split(" - ",1); return a.strip(), b.strip()
        return "", ""

    def parse_dt_utc(s: Optional[str]) -> Optional[dt.datetime]:
        if not s: return None
        s2 = s.replace(" UTC", "")
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
            try: return dt.datetime.strptime(s2, fmt)
            except Exception: pass
        return None

    for rec in topN:
        league_id = rec["league_id"]
        fixtures   = fixtures_by_league.get(league_id, [])
        odds_blob  = odds_by_league.get(league_id, {})

        # Opponent
        now = dt.datetime.utcnow(); best = None
        for fx in fixtures:
            name = fx.get("name") or ""
            home, away = parse_fixture_teams(name)
            if not home or not away: 
                continue
            # match team
            tname = rec["team"]
            ta, tb = team_tokens(tname), team_tokens(home)
            tc, td = team_tokens(tname), team_tokens(away)
            if not (team_names_match(tname, home) or team_names_match(tname, away)):
                continue
            ko = fx.get("starting_at")
            dt_obj = parse_dt_utc(ko) if isinstance(ko,str) else None
            if not dt_obj:
                ts = fx.get("starting_at_timestamp")
                if isinstance(ts,(int,float)):
                    try: dt_obj = dt.datetime.utcfromtimestamp(int(ts))
                    except Exception: pass
            if dt_obj and dt_obj >= now:
                if best is None or dt_obj < best[0]:
                    best = (dt_obj, fx)
        fx = best[1] if best else None

        opp_disp = None
        if fx:
            home, away = parse_fixture_teams(fx.get("name") or "")
            if home and away:
                if team_names_match(rec["team"], home):
                    opp_disp = pretty_team(away)
                elif team_names_match(rec["team"], away):
                    opp_disp = pretty_team(home)
        rec["opp_disp"] = opp_disp or "TBC"

        # Odds
        price = None
        def iter_odds_fixtures(odds_blob: dict) -> List[dict]:
            if isinstance(odds_blob, list): return odds_blob
            if isinstance(odds_blob, dict):
                if isinstance(odds_blob.get("fixtures"), list):
                    return odds_blob["fixtures"]
                data = odds_blob.get("data")
                if isinstance(data, dict) and isinstance(data.get("fixtures"), list):
                    return data["fixtures"]
            return []
        fixtures_list = iter_odds_fixtures(odds_blob)
        fx_odds = None
        if fx:
            for ofx in fixtures_list:
                try:
                    if int(ofx.get("id", -1)) == int(fx.get("id")):
                        fx_odds = ofx; break
                except Exception:
                    pass
        if not fx_odds:
            for ofx in fixtures_list:
                h, a = parse_fixture_teams(ofx.get("name") or "")
                if not h or not a: 
                    continue
                if (team_names_match(rec["team"], h) and team_names_match(rec["opp_disp"], a)) or \
                   (team_names_match(rec["team"], a) and team_names_match(rec["opp_disp"], h)):
                    fx_odds = ofx; break

        def _parse_latest_ts(s: Optional[str]) -> Optional[dt.datetime]:
            if not s: return None
            try: return dt.datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
            except Exception: return None

        def label_is_anytime_strict(raw_label: str) -> bool:
            if not raw_label: return False
            l = norm_ws_lower(raw_label)
            for bad in {"first","last","2 or more","two or more","brace","hat trick","hat-trick","treble",
                        "header","left foot","right foot","penalty","free kick","assist","to assist","card",
                        "yellow","red","shots","shot","on target","sot","score 2+","score two+",
                        "score two or more","to score 2","to score two"}:
                if bad in l: return False
            return l in {"anytime","any time","to score","to score (anytime)"}

        def label_is_anytime_lenient(raw_label: str) -> bool:
            if not raw_label: return False
            l = norm_ws_lower(raw_label)
            for bad in {"first","last","2 or more","two or more","brace","hat trick","hat-trick","treble",
                        "header","left foot","right foot","penalty","free kick","assist","to assist","card",
                        "yellow","red","shots","shot","on target","sot","score 2+","score two+",
                        "score two or more","to score 2","to score two"}:
                if bad in l: return False
            return ("anytime" in l or "any time" in l or l.startswith("to score"))

        def best_anytime_goalscorer_price(odds_rows: List[dict], player: str) -> Optional[float]:
            strict_cands = []; lenient_cands = []
            for o in odds_rows or []:
                try:
                    if int(o.get("bookmaker_id", 0)) != 2: 
                        continue
                    if int(o.get("market_id", 0)) != 90: 
                        continue
                except Exception:
                    continue
                if o.get("stopped"):
                    continue
                raw_label = str(o.get("label") or "")
                if not (label_is_anytime_strict(raw_label) or label_is_anytime_lenient(raw_label)):
                    continue
                candidate = o.get("name") or o.get("original_label") or o.get("total") or ""
                # name match
                lab = norm(cleanup_label_end_parens(candidate))
                first, last = extract_first_last(rec["player"])
                ok = False
                if last and len(last) >= 3 and last in lab:
                    if first:
                        ini = first[0:1]
                        if re.search(rf"\b{ini}\w*\b.*\b{last}\b", lab) or first in lab:
                            ok = True
                    else:
                        ok = True
                if not ok:
                    parts = tokenize_name(rec["player"])
                    if sum(1 for p in parts if len(p) >= 5 and p in lab) >= 2:
                        ok = True
                if not ok:
                    continue
                try:
                    price = float(str(o.get("value")))
                except Exception:
                    continue
                ts = _parse_latest_ts(o.get("latest_bookmaker_update"))
                (strict_cands if label_is_anytime_strict(raw_label) else lenient_cands).append((price, ts, raw_label))
            # choose latest timestamp then lowest price
            def _choose(cands):
                if not cands: return None
                latest = None
                for _, ts, _ in cands:
                    if ts and (latest is None or ts > latest): latest = ts
                if latest:
                    cands = [c for c in cands if c[1] == latest]
                return min(cands, key=lambda x: x[0])[0]
            return _choose(strict_cands) or _choose(lenient_cands)

        price = None
        if fx_odds:
            price = best_anytime_goalscorer_price(fx_odds.get("odds") or [], rec["player"])
        if price is None:
            for ofx in fixtures_list:
                h, a = parse_fixture_teams(ofx.get("name") or "")
                if not h or not a: 
                    continue
                if not (team_names_match(rec["team"], h) or team_names_match(rec["team"], a)):
                    continue
                p2 = best_anytime_goalscorer_price(ofx.get("odds") or [], rec["player"])
                if p2 is not None:
                    price = p2; break
        rec["price"] = price

    # ---------- Write LIST file (scan-friendly) ----------
    lines: List[str] = []
    lines += header_lines()
    for i, r in enumerate(topN, 1):
        price_txt = f"@ {r['price']:.2f}" if isinstance(r["price"], (int,float)) else "@ —"
        lines.append(f"{i}) {r['player_disp']} — {r['goals']} — {r['team_disp']} — {price_txt} — v {r['opp_disp']}.")

    OUT_LIST.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    print(f"Wrote {OUT_LIST}")

    # ---------- Optional: write TABLE file ----------
    if OUT_TABLE:
        rows = []
        rows.append("| Rank | Player | Goals | Odds | Opponent |")
        rows.append("| ---: | :----- | :---: | :--: | :------- |")
        for i, r in enumerate(topN, 1):
            odds = f"{r['price']:.2f}" if isinstance(r["price"], (int,float)) else "—"
            rows.append(f"| {i} | {r['player_disp']} | {r['goals']} | {odds} | {r['opp_disp']} |")
        Path(OUT_TABLE).write_text("\n".join(rows).rstrip() + "\n", encoding="utf-8")
        print(f"Wrote {OUT_TABLE}")

if __name__ == "__main__":
    main()
