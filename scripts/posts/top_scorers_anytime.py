#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Top Scorers (Anytime) + Bet365 Anytime Goalscorer odds

- Fetch top 10 scorers for each of the top 5 leagues from Sportmonks (type 208).
- Find each player's NEXT fixture from local fixtures cache:
    data/fixtures/by_league/{league_id}.json
- Load league odds file:
    data/odds/b365/by_league/{league_id}.json
- Extract Bet365 "Goalscorers" (market_id=90) "Anytime" price for the player.
  - Only uses bookmaker_id=2 (Bet365) and rows with stopped=False.
  - Robust name matching (accents/initials).
- Output to markdown (default: posts/top_scorers_anytime.md)

ENV:
  SPORTMONKS_TOKEN  (required)
  OUTPUT_PATH       (optional; default posts/top_scorers_anytime.md)
"""

import os
import sys
import json
import datetime as dt
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
import requests
import re
import unicodedata

# ====== CONFIG ======
BASE  = "https://api.sportmonks.com/v3/football"
TOKEN = os.getenv("SPORTMONKS_TOKEN")
if not TOKEN:
    sys.exit("Missing SPORTMONKS_TOKEN")

LEAGUES = {
    8:   "Premier League",
    564: "LaLiga",
    82:  "Bundesliga",
    384: "Serie A",
    301: "Ligue 1",
}

ROOT     = Path(".")
FIX_DIR  = ROOT / "data" / "fixtures" / "by_league"
ODDS_DIR = ROOT / "data" / "odds" / "b365" / "by_league"

BOOKMAKER_B365     = 2
MARKET_GOALSCORERS = 90

ANYTIME_LABELS_CANON   = {"anytime", "to score", "to score at any time"}
ANYTIME_SUBSTRINGS     = {"anytime", "to score"}
ANYTIME_LABEL_FALLBACK = {"any time"}  # super-safe catch

# ====== STRING/NAMES ======
def strip_accents(s: str) -> str:
    return ''.join(c for c in unicodedata.normalize('NFD', s or '') if unicodedata.category(c) != 'Mn')

def norm(s: str) -> str:
    s = strip_accents(s or "").lower()
    s = re.sub(r"[^a-z0-9\s\.-]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s

def cleanup_label(label: str) -> str:
    return re.sub(r"(?:\s*\([^)]*\))+$", "", label or "").strip()

def extract_last_name_initial(name: str) -> Tuple[Optional[str], Optional[str]]:
    if not name:
        return None, None
    name2 = strip_accents(name).replace(".", " ").strip()
    parts = [p for p in name2.split() if p]
    if not parts:
        return None, None
    last = norm(parts[-1])
    initial = None
    for p in parts[:-1]:
        ch = p.strip()[0:1]
        if ch:
            initial = ch.lower()
            break
    return last, initial

def player_label_matches(player: str, option_name_or_label: str) -> bool:
    """Match 'O. Watkins' / 'Ollie Watkins' / 'Watkins' to Bet365 player strings."""
    if not player or not option_name_or_label:
        return False
    last, initial = extract_last_name_initial(player)
    label = norm(cleanup_label(option_name_or_label))
    if not last or last not in label:
        return False
    if initial:
        first_word_initial = label.split()[0][0:1] if label.split() else None
        if first_word_initial and first_word_initial == initial:
            return True
        return bool(re.search(rf"\b{initial}\w*\b.*\b{last}\b", label))
    return True

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
    if ta == tb: return True
    if ta.issubset(tb) or tb.issubset(ta): return True
    inter = ta & tb; union = ta | tb
    if len(inter) / max(1, len(union)) >= 0.5: return True
    if len(inter) >= 2: return True
    return False

# ====== TIME/IO ======
def now_utc() -> str:
    return dt.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")

def parse_dt_utc(s: Optional[str]) -> Optional[dt.datetime]:
    if not s:
        return None
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

# ====== SPORTMONKS ======
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
        "filters": "seasonTopscorerTypes:208",  # 208 = Goals (anytime)
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
    # Works for both endpoints
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
        pid = player["id"]
        key = (pid, team["id"] if team else None)
        entry = agg.setdefault(
            key,
            {
                "player": player.get("display_name") or player.get("fullname") or player.get("name"),
                "team": (team or {}).get("name", "—"),
                "total": 0,
            },
        )
        try:
            entry["total"] += int(total)
        except Exception:
            pass

    ranked = sorted(agg.values(), key=lambda x: (-x["total"], x["player"]))[:10]
    return ranked

# ====== FIXTURES (LOCAL) ======
def parse_fixture_teams(fixture_name: str) -> Tuple[str, str]:
    if not fixture_name:
        return "", ""
    for sep in [" vs ", " v ", " VS ", " Vs "]:
        if sep in fixture_name:
            home, away = fixture_name.split(sep, 1)
            return home.strip(), away.strip()
    if " - " in fixture_name:
        home, away = fixture_name.split(" - ", 1)
        return home.strip(), away.strip()
    return "", ""

def load_fixtures_for_league(league_id: int) -> List[dict]:
    blob = _read_json(FIX_DIR / f"{league_id}.json") or {}
    return blob.get("fixtures") or []

def find_next_fixture_for_team(team_name: str, fixtures: List[dict]) -> Optional[dict]:
    now = dt.datetime.utcnow()
    candidates = []
    for fx in fixtures:
        name = fx.get("name") or ""
        home, away = parse_fixture_teams(name)
        if not home or not away:
            continue
        if not (team_names_match(team_name, home) or team_names_match(team_name, away)):
            continue
        kickoff = parse_dt_utc(fx.get("starting_at"))
        if kickoff is None:
            ts = fx.get("starting_at_timestamp")
            if isinstance(ts, (int, float)):
                try:
                    kickoff = dt.datetime.utcfromtimestamp(int(ts))
                except Exception:
                    kickoff = None
        if kickoff and kickoff >= now:
            candidates.append((kickoff, fx))
    if not candidates:
        return None
    candidates.sort(key=lambda t: t[0])
    return candidates[0][1]

# ====== ODDS (LOCAL) ======
def is_anytime_label(label: str) -> bool:
    if not label:
        return False
    l = norm(label)
    if l in ANYTIME_LABELS_CANON:
        return True
    if l in ANYTIME_LABEL_FALLBACK:
        return True
    for sub in ANYTIME_SUBSTRINGS:
        if sub in l:
            return True
    return False

def best_anytime_goalscorer_price(odds_rows: List[dict], player: str) -> Optional[float]:
    best = None
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

        if not is_anytime_label(o.get("label") or ""):
            continue

        candidate = o.get("name") or o.get("original_label") or o.get("total") or ""
        if not player_label_matches(player, candidate):
            continue

        try:
            price = float(str(o.get("value")))
        except Exception:
            continue

        if best is None or price > best + 1e-12:
            best = price
    return best

def load_odds_for_league(league_id: int) -> dict:
    return _read_json(ODDS_DIR / f"{league_id}.json") or {}

def find_fixture_odds_entry(odds_blob: dict, fixture_id: Optional[int], fixture_name: Optional[str]) -> Optional[dict]:
    fixtures = odds_blob.get("fixtures") or []
    # Try by fixture id first
    if fixture_id is not None:
        for fx in fixtures:
            if int(fx.get("id", -1)) == int(fixture_id):
                return fx
    # Fallback by name match
    if fixture_name:
        tgt_home, tgt_away = parse_fixture_teams(fixture_name)
        for fx in fixtures:
            home, away = parse_fixture_teams(fx.get("name") or "")
            if home and away and team_names_match(tgt_home, home) and team_names_match(tgt_away, away):
                return fx
    return None

def scan_team_fixtures_for_anytime(odds_blob: dict, team_name: str, player: str) -> Optional[Tuple[str, str, float]]:
    """
    Fallback: look across all fixtures in odds blob where this team appears,
    and return (fixture_name, kickoff_str, price) for the first with a price.
    """
    best = None
    for fx in odds_blob.get("fixtures") or []:
        name = fx.get("name") or ""
        home, away = parse_fixture_teams(name)
        if not home or not away:
            continue
        if not (team_names_match(team_name, home) or team_names_match(team_name, away)):
            continue
        price = best_anytime_goalscorer_price(fx.get("odds") or [], player)
        if price is not None:
            kickoff = fx.get("starting_at") or ""
            best = (name, kickoff, price)
            break
    return best

# ====== MAIN ======
def main():
    out_path = os.getenv("OUTPUT_PATH", "posts/top_scorers_anytime.md")
    Path(os.path.dirname(out_path)).mkdir(parents=True, exist_ok=True)

    lines: List[str] = []
    lines.append(f"# Top Scorers (Anytime) — Updated {now_utc()}")
    lines.append("")
    lines.append("_Bet365 anytime prices from local cache (bookmaker_id=2, market_id=90). If missing, no active line was found._")
    lines.append("")

    for league_id, league_name in LEAGUES.items():
        lines.append(f"## {league_name}")
        try:
            season_id = get_current_season(league_id)

            r = fetch_topscorers_via_endpoint(season_id)
            if r.status_code == 404:
                r = fetch_topscorers_via_season_include(season_id)
            r.raise_for_status()
            leaders = parse_topscorers(r.json())

            if not leaders:
                lines.append("No data yet.")
                lines.append("")
                continue

            fixtures = load_fixtures_for_league(league_id)
            odds_blob = load_odds_for_league(league_id)

            for i, row in enumerate(leaders, 1):
                player = row["player"]
                team   = row["team"]
                total  = row["total"]

                extra = ""
                # next fixture first
                fx = find_next_fixture_for_team(team, fixtures)
                odds_price = None
                fx_name = None
                kickoff = None

                if fx:
                    fx_name = fx.get("name") or ""
                    kickoff = fx.get("starting_at") or ""
                    fx_id   = fx.get("id")
                    odds_entry = find_fixture_odds_entry(odds_blob, fx_id, fx_name)
                    if odds_entry:
                        odds_price = best_anytime_goalscorer_price(odds_entry.get("odds") or [], player)

                # fallback: scan league odds fixtures where team appears
                if odds_price is None:
                    alt = scan_team_fixtures_for_anytime(odds_blob, team, player)
                    if alt:
                        fx_name, kickoff, odds_price = alt

                if odds_price is not None and fx_name:
                    extra = f" — Anytime @ **{odds_price:.2f}** ({fx_name} @ {kickoff} UTC)"
                elif fx_name:
                    extra = f" — ({fx_name} @ {kickoff} UTC) — _no open Bet365 anytime price_"
                else:
                    extra = " — _no upcoming fixture / odds found_"

                lines.append(f"{i}. {player} — {team} — {total}{extra}")

        except Exception as e:
            lines.append(f"_Error: {type(e).__name__}: {e}_")

        lines.append("")

    Path(out_path).write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    print(f"Wrote {out_path}")

if __name__ == "__main__":
    main()
