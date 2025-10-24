#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Top Scorers (Anytime) — with Bet365 'Anytime goalscorer' prices.

Key differences vs prior:
- Emits a version comment at the top of the markdown (toggle with EMIT_VERSION_COMMENT=0)
- DEBUG logs to Actions
- Robust odds file loader (tries multiple paths + shapes)
- Always appends an "extra" suffix per player so you can tell it's running

ENV:
  SPORTMONKS_TOKEN  (required)
  OUTPUT_PATH       (optional; default posts/top_scorers_anytime.md)
  DEBUG             (optional; "1" to print extra logs)
  EMIT_VERSION_COMMENT (optional; default "1")
"""

import os, sys, json, datetime as dt, re, unicodedata
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
import requests

VERSION = "top_scorers_anytime.py v1.3.0"

BASE  = "https://api.sportmonks.com/v3/football"
TOKEN = os.getenv("SPORTMONKS_TOKEN")
if not TOKEN:
    sys.exit("Missing SPORTMONKS_TOKEN")

DEBUG = os.getenv("DEBUG") == "1"
EMIT_VERSION_COMMENT = os.getenv("EMIT_VERSION_COMMENT", "1") == "1"

# Top 5 leagues
LEAGUES = {
    8:   "Premier League",
    564: "LaLiga",
    82:  "Bundesliga",
    384: "Serie A",
    301: "Ligue 1",
}

ROOT = Path(".")
FIX_DIR = ROOT / "data" / "fixtures" / "by_league"
# We'll try both of these (many repos differ)
ODDS_DIR_MAIN = ROOT / "data" / "odds" / "b365" / "by_league"
ODDS_DIR_ALT  = ROOT / "data" / "odds" / "b365"

BOOKMAKER_B365     = 2
MARKET_GOALSCORERS = 90

# ---------- string helpers ----------
def strip_accents(s: str) -> str:
    import unicodedata
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
    parts = strip_accents(name).replace(".", " ").strip().split()
    if not parts: return None, None
    last = norm(parts[-1]); initial = None
    for p in parts[:-1]:
        if p: initial = p[0].lower(); break
    return last, initial

def player_label_matches(player: str, option_name_or_label: str) -> bool:
    """Match 'O. Watkins' / 'Ollie Watkins' / 'Watkins' to Bet365 strings."""
    if not player or not option_name_or_label: return False
    last, initial = extract_last_name_initial(player)
    label = norm(cleanup_label(option_name_or_label))
    if not last or last not in label: return False
    if initial:
        first = label.split()[0][0:1] if label.split() else None
        if first and first == initial: return True
        return bool(re.search(rf"\b{initial}\w*\b.*\b{last}\b", label))
    return True

GENERIC_TEAM_TOKENS = {
    "fc","cf","afc","sc","cd","ud","ac","as","ss","ssc","us","uc","rc","rcd","ca",
    "the","club","de","del","la","las","los","calcio","united","city","saint","st",
    "bk","saint-germain","saintgermain","psg"
}
def team_tokens(name: str):
    toks = set(norm(name).split()); return {t for t in toks if t not in GENERIC_TEAM_TOKENS}
def team_names_match(a: str, b: str) -> bool:
    if not a or not b: return False
    ta, tb = team_tokens(a), team_tokens(b)
    if not ta or not tb: return False
    if ta == tb or ta.issubset(tb) or tb.issubset(ta): return True
    inter = ta & tb; union = ta | tb
    return len(inter) / max(1, len(union)) >= 0.5 or len(inter) >= 2

# ---------- time/io ----------
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

# ---------- sportmonks ----------
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
        team = row.get("participant") or (row.get("topscorer") or {}).get("participant")
        total = row.get("total") or (row.get("value") or {}).get("total")
        if not player or total is None: continue
        pid = player["id"]; key = (pid, (team or {}).get("id"))
        entry = agg.setdefault(key, {
            "player": player.get("display_name") or player.get("fullname") or player.get("name"),
            "team": (team or {}).get("name", "—"),
            "total": 0,
        })
        try: entry["total"] += int(total)
        except Exception: pass
    return sorted(agg.values(), key=lambda x: (-x["total"], x["player"]))[:10]

# ---------- fixtures ----------
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
    # some repos store fixtures under "data"
    fx = blob.get("fixtures") or (blob.get("data") or {}).get("fixtures") or []
    return fx

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

# ---------- odds ----------
ANYTIME_LABELS = {"anytime","to score","to score at any time","any time"}
def is_anytime_label(label: str) -> bool:
    if not label: return False
    l = norm(label)
    if l in ANYTIME_LABELS: return True
    return ("anytime" in l) or ("any time" in l) or ("to score" in l)

def iter_odds_fixtures(odds_blob: dict) -> List[dict]:
    """
    Support a few shapes:
      { fixtures: [...] }
      { data: { fixtures: [...] } }
      [ {id,name,odds}, ... ]
    """
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
    # Try main path
    p1 = ODDS_DIR_MAIN / f"{league_id}.json"
    if p1.exists(): return _read_json(p1) or {}
    # Try alt path (no by_league)
    p2 = ODDS_DIR_ALT / f"{league_id}.json"
    if p2.exists(): return _read_json(p2) or {}
    return {}

def find_fixture_odds_entry(odds_blob: dict, fixture_id: Optional[int], fixture_name: Optional[str]) -> Optional[dict]:
    fixtures = iter_odds_fixtures(odds_blob)
    # by id
    if fixture_id is not None:
        for fx in fixtures:
            try:
                if int(fx.get("id", -1)) == int(fixture_id):
                    return fx
            except Exception:
                continue
    # by fuzzy name
    if fixture_name:
        tgt_home, tgt_away = parse_fixture_teams(fixture_name)
        for fx in fixtures:
            home, away = parse_fixture_teams(fx.get("name") or "")
            if home and away and team_names_match(tgt_home, home) and team_names_match(tgt_away, away):
                return fx
    return None

def best_anytime_goalscorer_price(odds_rows: List[dict], player: str) -> Optional[float]:
    best = None
    for o in odds_rows or []:
        try:
            if int(o.get("bookmaker_id", 0)) != BOOKMAKER_B365: continue
            if int(o.get("market_id", 0))    != MARKET_GOALSCORERS: continue
        except Exception:
            continue
        if o.get("stopped"):  # closed line
            continue
        if not is_anytime_label(o.get("label") or ""):  # we only want "Anytime"
            continue
        candidate = o.get("name") or o.get("original_label") or o.get("total") or ""
        if not player_label_matches(player, candidate):
            continue
        try: price = float(str(o.get("value")))
        except Exception: continue
        if best is None or price > best + 1e-12:
            best = price
    return best

def scan_team_fixtures_for_anytime(odds_blob: dict, team_name: str, player: str) -> Optional[Tuple[str, str, float]]:
    for fx in iter_odds_fixtures(odds_blob):
        fname = fx.get("name") or ""
        home, away = parse_fixture_teams(fname)
        if not home or not away: continue
        if not (team_names_match(team_name, home) or team_names_match(team_name, away)): continue
        price = best_anytime_goalscorer_price(fx.get("odds") or [], player)
        if price is not None:
            return fname, (fx.get("starting_at") or ""), price
    return None

# ---------- main ----------
def main():
    out_path = os.getenv("OUTPUT_PATH", "posts/top_scorers_anytime.md")
    Path(os.path.dirname(out_path)).mkdirs(exist_ok=True) if hasattr(Path, "mkdirs") else Path(os.path.dirname(out_path)).mkdir(parents=True, exist_ok=True)

    if DEBUG:
        print(f"[DEBUG] {VERSION}")
        print(f"[DEBUG] FIX_DIR={FIX_DIR}")
        print(f"[DEBUG] ODDS_DIR_MAIN={ODDS_DIR_MAIN}")
        print(f"[DEBUG] ODDS_DIR_ALT={ODDS_DIR_ALT}")

    lines: List[str] = []
    if EMIT_VERSION_COMMENT:
        lines.append(f"<!-- {VERSION} -->")
    lines.append(f"Top Scorers (Anytime) — Updated {now_utc_str()}\n")

    for league_id, league_name in LEAGUES.items():
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
            odds_fixtures = iter_odds_fixtures(odds_blob)

            if DEBUG:
                print(f"[DEBUG] L{league_id} {league_name}: leaders={len(leaders)} fixtures={len(fixtures)} odds.fixtures={len(odds_fixtures)}")

            if not leaders:
                lines.append("No data yet.")
            else:
                for i, row in enumerate(leaders, 1):
                    player = row["player"]; team = row["team"]; total = row["total"]
                    extra = ""

                    fx = find_next_fixture_for_team(team, fixtures)
                    odds_price = None; fx_name = None; kickoff = None

                    if fx:
                        fx_name = fx.get("name") or ""
                        kickoff = fx.get("starting_at") or ""
                        fx_id   = fx.get("id")
                        fx_odds = find_fixture_odds_entry(odds_blob, fx_id, fx_name)
                        if fx_odds:
                            odds_price = best_anytime_goalscorer_price(fx_odds.get("odds") or [], player)

                    if odds_price is None:
                        alt = scan_team_fixtures_for_anytime(odds_blob, team, player)
                        if alt:
                            fx_name, kickoff, odds_price = alt

                    if odds_price is not None and fx_name:
                        extra = f" — Anytime @ {odds_price:.2f} ({fx_name} @ {kickoff} UTC)"
                    elif fx_name:
                        extra = f" — ({fx_name} @ {kickoff} UTC) — no Bet365 anytime price"
                    else:
                        extra = " — no upcoming fixture / odds found"

                    lines.append(f"{i}. {player} — {team} — {total}{extra}")

        except Exception as e:
            if DEBUG:
                import traceback; traceback.print_exc()
            lines.append(f"Error fetching data: {type(e).__name__}: {e}")

        lines.append("")

    Path(out_path).write_text("\n".join(lines), encoding="utf-8")
    print(f"Wrote {out_path}")

if __name__ == "__main__":
    main()
