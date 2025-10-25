#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Top Scorers (Anytime) — Bet365 odds (strict)
v1.6.0 — compact X-friendly format + strict 'Anytime' + robust name matching

Output ONLY:
  posts/top_scorers_anytime_social.md

ENV:
  SPORTMONKS_TOKEN      (required)
  OUTPUT_PATH           (default posts/top_scorers_anytime_social.md)
  DEBUG                 (optional; "1" to print extra logs)
  EMIT_VERSION_COMMENT  (optional; default "0")
"""

import os, sys, json, datetime as dt, re, unicodedata
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
import requests

VERSION = "top_scorers_anytime.py v1.6.0"

BASE  = "https://api.sportmonks.com/v3/football"
TOKEN = os.getenv("SPORTMONKS_TOKEN")
if not TOKEN:
    sys.exit("Missing SPORTMONKS_TOKEN")

DEBUG = os.getenv("DEBUG") == "1"
EMIT_VERSION_COMMENT = os.getenv("EMIT_VERSION_COMMENT", "0") == "1"

# Top 5 leagues
LEAGUES = {8:"Premier League",564:"LaLiga",82:"Bundesliga",384:"Serie A",301:"Ligue 1"}

# Local cache paths
ROOT          = Path(".")
FIX_DIR       = ROOT / "data" / "fixtures" / "by_league"
ODDS_DIR_MAIN = ROOT / "data" / "odds" / "b365" / "by_league"
ODDS_DIR_ALT  = ROOT / "data" / "odds" / "b365"

# Bet365 identifiers
BOOKMAKER_B365     = 2
MARKET_GOALSCORERS = 90

# ---------------- string helpers ----------------
SUFFIXES = {"jr","junior","sr","senior","ii","iii","iv","filho","neto"}

def strip_accents(s: str) -> str:
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

# Known aliases / nicknames
ALIASES: Dict[str, set] = {
    "vinicius junior": {"vini", "vinicius jr", "vini jr"},
    "cucho hernandez": {"cucho", "juan camilo hernandez", "juan hernandez", "j camilo hernandez"},
}

def player_label_matches(player: str, option_name_or_label: str) -> bool:
    if not player or not option_name_or_label: return False
    label = norm(cleanup_label_end_parens(option_name_or_label))
    p_norm = norm(player)

    first, last = extract_first_last(player)

    if last and len(last) >= 3 and last in label:
        if first:
            ini = first[0:1]
            if re.search(rf"\b{ini}\w*\b.*\b{last}\b", label) or first in label:
                return True
        else:
            return True

    if p_norm in ALIASES:
        for a in ALIASES[p_norm]:
            if norm(a) in label:
                return True

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

GENERIC_TEAM_TOKENS = {
    "fc","cf","afc","sc","cd","ud","ac","as","ss","ssc","us","uc","rc","rcd","ca",
    "the","club","de","del","la","las","los","calcio","united","city","saint","st",
    "bk","saint-germain","saintgermain","psg"
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

# ---------------- time/io ----------------
def now_utc_str() -> str:
    return dt.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")

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

# ---------------- Sportmonks (leaders) ----------------
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
        "api_token": TOKEN, "include": "player;participant;type",
        "filters": "seasonTopscorerTypes:208", "per_page": 50, "order": "asc",
    }
    return requests.get(url, params=params, timeout=30)

def fetch_topscorers_via_season_include(season_id: int):
    url = f"{BASE}/seasons/{season_id}"
    params = {
        "api_token": TOKEN,
        "include": "topscorers.player;topscorers.participant;topscorers.type",
        "filters": "seasonTopscorerTypes:208", "per_page": 50,
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
        team = row.get("participant") or (row.get("topscorer") or {}).get("participant")
        total = row.get("total") or (row.get("value") or {}).get("total")
        if not player or total is None:
            continue
        pid = player["id"]; key = (pid, (team or {}).get("id"))
        entry = agg.setdefault(key, {
            "player": player.get("display_name") or player.get("fullname") or player.get("name"),
            "team": (team or {}).get("name", "—"),
            "total": 0,
        })
        try: entry["total"] += int(total)
        except Exception: pass

    return sorted(agg.values(), key=lambda x: (-x["total"], x["player"]))[:10]

# ---------------- fixtures ----------------
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
        if not home or not away: continue
        if not (team_names_match(team_name, home) or team_names_match(team_name, away)): continue
        ko = parse_dt_utc(fx.get("starting_at"))
        if not ko:
            ts = fx.get("starting_at_timestamp")
            if isinstance(ts,(int,float)):
                try: ko = dt.datetime.utcfromtimestamp(int(ts))
                except Exception: pass
        if ko and ko >= now:
            if best is None or ko < best[0]: best = (ko, fx)
    return best[1] if best else None

# --------- team abbreviations (use short_code if present, else smart fallback)
def build_team_abbrev_map(fixtures: List[dict]) -> Dict[str, str]:
    m: Dict[str, str] = {}
    for fx in fixtures:
        for p in (fx.get("participants") or []):
            nm = p.get("name")
            sc = p.get("short_code") or ""
            if not nm: continue
            key = norm(nm)
            if sc:
                m.setdefault(key, sc.upper())
            else:
                m.setdefault(key, smart_abbrev(nm))
    return m

GENERIC_WORDS = {"the","club","de","del","la","las","los","and","cf","fc","ac","as","ud","cd","rc","rcd",
                 "atletico","athletic","real","inter","union","bayern","paris","saint","saint-germain","psg",
                 "football","calcio","sv","ssc","us","uc","bk"}

def smart_abbrev(name: str) -> str:
    toks = [t for t in re.split(r"[^a-zA-Z]+", strip_accents(name)) if t]
    core = [t for t in toks if t.lower() not in GENERIC_WORDS]
    if not core:
        core = toks
    if not core:
        return (name[:3] or "???").upper()
    if len(core) == 1:
        return core[0][:3].upper()
    # take first letters of first 3 core tokens
    return "".join(t[0] for t in core[:3]).upper()

def abbr_for(team_name: Optional[str], m: Dict[str,str]) -> str:
    if not team_name: return "TBC"
    return m.get(norm(team_name)) or smart_abbrev(team_name)

# ---------------- odds ----------------
ANYTIME_EXACT = {"anytime","any time","to score","to score (anytime)"}
ANYTIME_BLOCK = {
    "first","last","2 or more","two or more","brace","hat trick","hat-trick",
    "treble","header","left foot","right foot","penalty","free kick",
    "assist","to assist","card","yellow","red","shots","shot","on target",
    "sot","score 2+","score two+","score two or more","to score 2","to score two"
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
    if isinstance(odds_blob, list): return odds_blob
    if isinstance(odds_blob, dict):
        if isinstance(odds_blob.get("fixtures"), list): return odds_blob["fixtures"]
        data = odds_blob.get("data")
        if isinstance(data, dict) and isinstance(data.get("fixtures"), list): return data["fixtures"]
    return []

def load_odds_for_league(league_id: int) -> dict:
    p1 = ODDS_DIR_MAIN / f"{league_id}.json"
    if p1.exists(): return _read_json(p1) or {}
    p2 = ODDS_DIR_ALT / f"{league_id}.json"
    if p2.exists(): return _read_json(p2) or {}
    return {}

def _parse_latest_ts(s: Optional[str]) -> Optional[dt.datetime]:
    if not s: return None
    try: return dt.datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
    except Exception: return None

def _choose_price(cands: List[Tuple[float, Optional[dt.datetime], str]]) -> Optional[float]:
    if not cands: return None
    latest = None
    for _, ts, _ in cands:
        if ts and (latest is None or ts > latest): latest = ts
    if latest:
        cands = [c for c in cands if c[1] == latest]
    return min(cands, key=lambda x: x[0])[0]

def best_anytime_goalscorer_price(odds_rows: List[dict], player: str) -> Optional[float]:
    strict_cands: List[Tuple[float, Optional[dt.datetime], str]] = []
    lenient_cands: List[Tuple[float, Optional[dt.datetime], str]] = []
    for o in odds_rows or []:
        try:
            if int(o.get("bookmaker_id", 0)) != BOOKMAKER_B365: continue
            if int(o.get("market_id", 0))    != MARKET_GOALSCORERS: continue
        except Exception:
            continue
        if o.get("stopped"): continue
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
        tup = (price, ts, raw_label)
        (strict_cands if label_is_anytime_strict(raw_label) else lenient_cands).append(tup)
    best = _choose_price(strict_cands) or _choose_price(lenient_cands)
    if DEBUG and best is None:
        seen = [norm_ws_lower(str(x.get("label"))) for x in (odds_rows or [])]
        print(f"[DEBUG] NO ANYTIME MATCH for '{player}'. Labels seen: {sorted(set(seen))[:8]}")
    return best

def find_fixture_odds_entry(odds_blob: dict, fixture_id: Optional[int], fixture_name: Optional[str]) -> Optional[dict]:
    fixtures = iter_odds_fixtures(odds_blob)
    if fixture_id is not None:
        for fx in fixtures:
            try:
                if int(fx.get("id", -1)) == int(fixture_id): return fx
            except Exception: pass
    if fixture_name:
        tgt_home, tgt_away = parse_fixture_teams(fixture_name)
        for fx in fixtures:
            home, away = parse_fixture_teams(fx.get("name") or "")
            if home and away and team_names_match(tgt_home, home) and team_names_match(tgt_away, away):
                return fx
    return None

def scan_team_fixtures_for_anytime(odds_blob: dict, team_name: str, player: str) -> Optional[Tuple[str, str, float]]:
    for fx in iter_odds_fixtures(odds_blob):
        fname = fx.get("name") or ""
        home, away = parse_fixture_teams(fname)
        if not home or not away: continue
        if not (team_names_match(team_name, home) or team_names_match(team_name, away)): continue
        price = best_anytime_goalscorer_price(fx.get("odds") or [], player)
        if price is not None:
            kickoff = fx.get("starting_at") or ""
            return fname, kickoff, price
    return None

# ---------------- social output (compact X style) ----------------
def header_lines() -> List[str]:
    lines = []
    if EMIT_VERSION_COMMENT:
        lines.append(f"<!-- {VERSION} -->")
    lines.append("Top Scorers in each of the top 5 leagues — Anytime (Bet365)")
    lines.append("")  # spacer
    return lines

def compact_line(rank: int, player: str, goals: int, team_abbr: str, price: Optional[float], opp_abbr: Optional[str]) -> str:
    price_txt = f"@{price:.2f}" if price is not None else "—"
    opp_txt = opp_abbr or "TBC"
    return f"{rank}) {player} ({goals}, {team_abbr}) {price_txt} v {opp_txt}"

def main():
    out_path = os.getenv("OUTPUT_PATH", "posts/top_scorers_anytime_social.md")
    Path(os.path.dirname(out_path)).mkdir(parents=True, exist_ok=True)

    lines: List[str] = header_lines()

    for league_id, league_name in LEAGUES.items():
        lines.append("")
        lines.append(league_name)

        try:
            season_id = get_current_season(league_id)
            r = fetch_topscorers_via_endpoint(season_id)
            if r.status_code == 404:
                r = fetch_topscorers_via_season_include(season_id)
            r.raise_for_status()
            leaders = parse_topscorers(r.json())

            fixtures = load_fixtures_for_league(league_id)
            odds_blob = load_odds_for_league(league_id)
            abbr_map = build_team_abbrev_map(fixtures)

            if DEBUG:
                print(f"[DEBUG] L{league_id} {league_name}: leaders={len(leaders)} fixtures={len(fixtures)} abbrs={len(abbr_map)}")

            if not leaders:
                lines.append("(no data)")
                continue

            for i, row in enumerate(leaders, 1):
                player = row["player"]; team = row["team"]; total = int(row["total"])
                fx = find_next_fixture_for_team(team, fixtures)

                opp_name = None
                price = None
                if fx:
                    fx_name = fx.get("name") or ""
                    home, away = parse_fixture_teams(fx_name)
                    if home and away:
                        if team_names_match(team, home):   opp_name = away
                        elif team_names_match(team, away): opp_name = home
                        else: opp_name = away if team in home else home

                    fx_odds = find_fixture_odds_entry(odds_blob, fx.get("id"), fx_name)
                    if fx_odds:
                        price = best_anytime_goalscorer_price(fx_odds.get("odds") or [], player)
                    if price is None:
                        alt = scan_team_fixtures_for_anytime(odds_blob, team, player)
                        if alt: _, _, price = alt

                team_abbr = abbr_for(team, abbr_map)
                opp_abbr  = abbr_for(opp_name, abbr_map) if opp_name else None
                lines.append(compact_line(i, player, total, team_abbr, price, opp_abbr))

        except Exception as e:
            if DEBUG:
                import traceback; traceback.print_exc()
            lines.append(f"(Error fetching {league_name}: {type(e).__name__}: {e})")

    Path(out_path).write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    print(f"Wrote {out_path}")

if __name__ == "__main__":
    main()
