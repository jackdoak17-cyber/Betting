#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Builds 'Top Scorers (Anytime)' post with Bet365 anytime goalscorer odds,
saving to betting/posts/top_scorers_anytime.md

Env:
  SPORTMONKS_TOKEN  -> your API token

Repo files it reads:
  data/fixtures/by_league/{league_id}.json
  data/odds/b365/by_league/{league_id}.json        (preferred)
  data/odds/b365/fixtures/{fixture_id}.json        (fallback per fixture)
"""

import os
import sys
import re
import json
import unicodedata
from pathlib import Path
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import requests

# -----------------------------
# Config
# -----------------------------
LEAGUE_SLUG_BY_ID = {
    8:   "england-premier-league",
    82:  "germany-bundesliga",
    301: "france-ligue-1",
    384: "italy-serie-a",
    564: "spain-laliga",
}
LEAGUE_LABEL = {
    8: "Premier League",
    564: "LaLiga",
    82: "Bundesliga",
    384: "Serie A",
    301: "Ligue 1",
}
TOP_LEAGUES = [8, 564, 82, 384, 301]

API_BASE = "https://api.sportmonks.com/v3/football"
OUTFILE = Path("betting/posts/top_scorers_anytime.md")

FIXTURES_DIR = Path("data/fixtures/by_league")
ODDS_BY_LEAGUE_DIR = Path("data/odds/b365/by_league")
ODDS_BY_FIXTURE_DIR = Path("data/odds/b365/fixtures")
DEBUG_DIR = Path("data/debug"); DEBUG_DIR.mkdir(parents=True, exist_ok=True)

BOOKMAKER_IDENTIFIERS = {"bet365", "b365", "bet 365", "bet-365", "bet_365"}

# -----------------------------
# Utils
# -----------------------------
def now_utc_str() -> str:
    return datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")

def parse_dt(s: Any) -> Optional[datetime]:
    if not s:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(str(s), fmt)
        except Exception:
            pass
    try:
        return datetime.utcfromtimestamp(int(s))
    except Exception:
        return None

def load_json(path: Path) -> Optional[Dict[str, Any]]:
    try:
        if path.exists():
            with path.open("r", encoding="utf-8") as f:
                return json.load(f)
    except Exception as e:
        sys.stderr.write(f"[WARN] Could not parse JSON {path}: {e}\n")
    return None

def ascii_norm(s: str) -> str:
    if s is None:
        return ""
    s = unicodedata.normalize("NFKD", s)
    s = s.encode("ascii", "ignore").decode("ascii")
    s = re.sub(r"[^\w\s-]", "", s)
    s = re.sub(r"\s+", " ", s).strip().lower()
    return s

def name_variants(full_name: str) -> List[str]:
    full = ascii_norm(full_name)
    parts = full.split()
    variants = {full}
    if parts:
        last = parts[-1]
        variants.add(last)
        if len(parts) >= 2:
            first = parts[0]
            variants.add(f"{first} {last}")
            variants.add(f"{first[0]} {last}")
            variants.add(f"{first[0]}. {last}")
    return list(variants)

def looks_like_bet365(name: str) -> bool:
    n = ascii_norm(name)
    return any(k in n for k in BOOKMAKER_IDENTIFIERS)

def num(v: Any) -> Optional[float]:
    try:
        if v is None:
            return None
        if isinstance(v, (int, float)):
            return float(v)
        s = str(v).strip()
        if "/" in s:
            a, b = s.split("/", 1)
            return (float(a) / float(b)) + 1.0
        return float(s)
    except Exception:
        return None

def redact(url: str) -> str:
    return re.sub(r"(api_token=)[^&]+", r"\1***redacted***", url)

# -----------------------------
# SportMonks helpers
# -----------------------------
def req(url: str, params: Dict[str, Any]) -> Dict[str, Any]:
    r = requests.get(url, params=params, timeout=45)
    try:
        r.raise_for_status()
    except Exception as e:
        raise type(e)(f"{e} :: {redact(r.url)}")
    return r.json()

def get_current_season(league_id: int, token: str) -> Tuple[Optional[int], Optional[str]]:
    url = f"{API_BASE}/leagues/{league_id}"
    params = {"include": "currentSeason", "api_token": token}
    data = req(url, params).get("data", {})
    cs = data.get("currentseason") or data.get("currentSeason") or {}
    return cs.get("id"), cs.get("starting_at")

def _items_from_generic(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return items or []

def _items_from_discovery(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Walk an API payload and pick any array that looks like top-scorers:
    items containing a 'player' relation + some integer 'goals/value/scored/count'.
    """
    best: List[Dict[str, Any]] = []
    best_len = 0

    def goals_val(x):
        for k in ("goals", "value", "scored", "count", "total_goals"):
            if k in x:
                try:
                    return int(x[k])
                except Exception:
                    pass
        return None

    def looks_like_item(x):
        if not isinstance(x, dict):
            return False
        has_player = "player" in x or "player_id" in x or "player_name" in x
        has_team = "team" in x or "team_id" in x or "team_name" in x
        has_goals = goals_val(x) is not None
        return has_player and has_goals and has_team

    def walk(obj):
        nonlocal best, best_len
        if isinstance(obj, dict):
            for v in obj.values():
                walk(v)
        elif isinstance(obj, list):
            sample = [x for x in obj if isinstance(x, dict)]
            if sample and all(looks_like_item(x) for x in sample[:5]):
                if len(sample) > best_len:
                    best = sample
                    best_len = len(sample)
            for v in obj:
                walk(v)

    walk(payload)
    return best

def fetch_top_scorers_any_way(league_id: int, season_id: int, token: str) -> List[Dict[str, Any]]:
    tried = []

    # A) primary documented route on many plans
    try:
        url = f"{API_BASE}/topscorers/seasons/{season_id}"
        params = {"include": "player;team", "api_token": token}
        payload = req(url, params)
        return _items_from_generic(payload.get("data") or [])
    except Exception as e:
        tried.append(str(e))

    # B) query version
    try:
        url = f"{API_BASE}/topscorers"
        params = {"seasons": str(season_id), "include": "player;team", "api_token": token}
        payload = req(url, params)
        return _items_from_generic(payload.get("data") or [])
    except Exception as e:
        tried.append(str(e))

    # C) alt nesting many accounts expose
    for route in (
        f"{API_BASE}/seasons/{season_id}/topscorers",
        f"{API_BASE}/leagues/{league_id}/topscorers",
        f"{API_BASE}/leagues/{league_id}/topscorers/seasons/{season_id}",
    ):
        try:
            payload = req(route, {"api_token": token, "include": "player;team"})
            return _items_from_generic(payload.get("data") or [])
        except Exception as e:
            tried.append(str(e))

    # D) discovery on season payload (never 404s; unknown includes are ignored)
    try:
        season_url = f"{API_BASE}/seasons/{season_id}"
        payload = req(season_url, {
            "api_token": token,
            # throw the kitchen sink; ignored includes don't break
            "include": "statistics;topscorers;players;teams;standings;groups;stages"
        })
        found = _items_from_discovery(payload)
        if found:
            return found
    except Exception as e:
        tried.append(str(e))

    # E) discovery on league payload
    try:
        league_url = f"{API_BASE}/leagues/{league_id}"
        payload = req(league_url, {
            "api_token": token,
            "include": "statistics;currentSeason;topscorers;seasons"
        })
        found = _items_from_discovery(payload)
        if found:
            return found
    except Exception as e:
        tried.append(str(e))

    raise RuntimeError("All top-scorer routes failed:\n  " + "\n  ".join(tried))

def extract_player_fields(item: Dict[str, Any]) -> Tuple[str, Optional[int], str, Optional[int]]:
    goals = (item.get("goals") or item.get("value") or item.get("scored") or item.get("count"))
    try:
        goals = int(goals) if goals is not None else None
    except Exception:
        goals = None

    team_id = item.get("team_id")
    team_name = item.get("team_name")
    team_rel = item.get("team") or {}
    team_data = team_rel.get("data") if isinstance(team_rel, dict) else None
    if isinstance(team_data, dict):
        team_id = team_id or team_data.get("id")
        team_name = team_name or team_data.get("name")

    player_name = item.get("player_name")
    p_rel = item.get("player") or {}
    p_data = p_rel.get("data") if isinstance(p_rel, dict) else None
    if isinstance(p_data, dict):
        player_name = (
            player_name
            or p_data.get("display_name")
            or p_data.get("common_name")
            or p_data.get("fullname")
            or p_data.get("name")
        )
    player_name = player_name or "Unknown"

    return player_name, team_id, (team_name or "Unknown"), goals

# -----------------------------
# Fixtures + Odds
# -----------------------------
def next_fixture_for_team(fixtures_doc: Dict[str, Any], team_id: int) -> Optional[Dict[str, Any]]:
    fixtures = fixtures_doc.get("fixtures") or fixtures_doc.get("data") or []
    now = datetime.utcnow()
    candidates = []
    for fx in fixtures:
        parts = fx.get("participants") or []
        if any((p.get("id") == team_id) for p in parts):
            dt = parse_dt(fx.get("starting_at")) or parse_dt(fx.get("starting_at_timestamp"))
            if dt and dt >= now:
                candidates.append((dt, fx))
    if not candidates:
        return None
    candidates.sort(key=lambda t: t[0])
    return candidates[0][1]

def opponent_and_home_away(fixture: Dict[str, Any], team_id: int) -> Tuple[str, str]:
    parts = fixture.get("participants") or []
    opp, ha = "?", "?"
    for p in parts:
        if p.get("id") == team_id:
            meta = p.get("meta") or {}
            loc = (meta.get("location") or "").lower()
            ha = "H" if loc == "home" else ("A" if loc == "away" else "?")
        else:
            opp = p.get("name") or "?"
    return opp, ha

def find_fixture_odds_node(league_odds_doc: Dict[str, Any], fixture_id: int) -> Optional[Dict[str, Any]]:
    if not league_odds_doc:
        return None
    key = str(fixture_id)
    if key in league_odds_doc and isinstance(league_odds_doc[key], dict):
        return league_odds_doc[key]
    for top_key in ("fixtures", "data", "events", "matches"):
        arr = league_odds_doc.get(top_key)
        if isinstance(arr, list):
            for node in arr:
                if not isinstance(node, dict):
                    continue
                if node.get("id") == fixture_id or node.get("fixture_id") == fixture_id:
                    return node

    def walk(obj):
        if isinstance(obj, dict):
            if (obj.get("id") == fixture_id or obj.get("fixture_id") == fixture_id) and (
                "bookmakers" in obj or "markets" in obj
            ):
                return obj
            for v in obj.values():
                found = walk(v)
                if found:
                    return found
        elif isinstance(obj, list):
            for v in obj:
                found = walk(v)
                if found:
                    return found
        return None

    return walk(league_odds_doc)

def select_bet365_block(odds_node: Dict[str, Any]) -> Dict[str, Any]:
    books = odds_node.get("bookmakers")
    if isinstance(books, list) and books:
        for b in books:
            name = ascii_norm(b.get("name") or b.get("key") or b.get("id") or "")
            if looks_like_bet365(name):
                return b
        return books[0]
    return odds_node

def collect_anytime_selections(book_block: Dict[str, Any]) -> List[Dict[str, Any]]:
    results = []

    def accept_market_name(n: str) -> bool:
        n0 = ascii_norm(n)
        return ("anytime" in n0 and ("scorer" in n0 or "score" in n0)) or ("to score" in n0)

    def visit(node, ctx_market_name: Optional[str] = None):
        if isinstance(node, dict):
            market_name = ctx_market_name
            n = node.get("name") or node.get("key") or node.get("market") or ""
            if n:
                if any(k in ascii_norm(n) for k in ["goalscorer", "scorer", "score", "market"]):
                    market_name = n
            sels = node.get("selections") or node.get("runners") or node.get("outcomes")
            if isinstance(sels, list) and accept_market_name(market_name or n or ""):
                for s in sels:
                    nm = s.get("name") or s.get("runner") or s.get("label")
                    if not nm:
                        continue
                    price = (
                        num(s.get("price"))
                        or num(s.get("odds_decimal"))
                        or num(s.get("decimal"))
                        or num(s.get("odds"))
                        or num(s.get("true_odds"))
                    )
                    if price:
                        results.append({"name": nm, "price": price})
            for v in node.values():
                visit(v, market_name)
        elif isinstance(node, list):
            for v in node:
                visit(v, ctx_market_name)

    visit(book_block, None)
    return results

def match_runner_price(runners: List[Dict[str, Any]], player_name: str) -> Optional[float]:
    if not runners:
        return None
    pvars = name_variants(player_name)
    p_last = ascii_norm(player_name).split()[-1] if player_name else ""
    best_price = None
    best_score = -1
    pvars_norm = [ascii_norm(v) for v in pvars]

    for r in runners:
        nm = r.get("name") or ""
        price = r.get("price")
        if not nm or not price:
            continue
        nm_norm = ascii_norm(nm)
        score = 0
        if nm_norm in pvars_norm:
            score = 100
        elif p_last and p_last in nm_norm:
            score = 70
        if ascii_norm(player_name) in nm_norm:
            score += 20
        if score > best_score:
            best_score = score
            best_price = price
    return best_price

def find_anytime_price(league_id: int, fixture_id: int, player_name: str) -> Optional[float]:
    league_path = ODDS_BY_LEAGUE_DIR / f"{league_id}.json"
    league_doc = load_json(league_path)
    node = find_fixture_odds_node(league_doc or {}, fixture_id)
    if node:
        book = select_bet365_block(node)
        runners = collect_anytime_selections(book)
        price = match_runner_price(runners, player_name)
        if price:
            return price

    fx_path = ODDS_BY_FIXTURE_DIR / f"{fixture_id}.json"
    fx_doc = load_json(fx_path)
    if fx_doc:
        book = select_bet365_block(fx_doc)
        runners = collect_anytime_selections(book)
        price = match_runner_price(runners, player_name)
        if price:
            return price

    return None

# -----------------------------
# Render
# -----------------------------
def render_league_block(league_id: int, rows: List[Dict[str, Any]], error: Optional[str]) -> str:
    lines = []
    lines.append(f"## {LEAGUE_LABEL.get(league_id, f'League {league_id}')}")
    if error:
        lines.append(f"> Error fetching top scorers: {error}")
        lines.append("")
    if not rows:
        lines.append("_No data._")
        lines.append("")
        return "\n".join(lines)

    for i, r in enumerate(rows, 1):
        base = f"{i}. {r['player']} — {r['team']} — {r.get('goals','?')}"
        if r.get("odds"):
            extra = f" — **Bet365 Anytime:** {r['odds']:.2f}"
            if r.get("opponent"):
                extra += f" (vs {r['opponent']}, {r.get('ha','?')})"
            lines.append(base + extra)
        else:
            lines.append(base + " — Odds: N/A")
    lines.append("")
    return "\n".join(lines)

# -----------------------------
# Main
# -----------------------------
def main():
    token = os.environ.get("SPORTMONKS_TOKEN")
    if not token:
        sys.stderr.write("ERROR: SPORTMONKS_TOKEN env var is required\n")
        sys.exit(1)

    OUTFILE.parent.mkdir(parents=True, exist_ok=True)

    header = f"# Top Scorers (Anytime) — Updated {now_utc_str()}\n"
    blocks: List[str] = [header, ""]

    for league_id in TOP_LEAGUES:
        title = LEAGUE_LABEL.get(league_id, f"League {league_id}")
        rows = []
        err_text = None
        try:
            season_id, _season_start = get_current_season(league_id, token)
            if not season_id:
                raise RuntimeError(f"No current season for league {league_id}")

            items = fetch_top_scorers_any_way(league_id, season_id, token)
            # Debug dump raw for inspection
            try:
                with (DEBUG_DIR / f"topscorers_{league_id}_{season_id}.json").open("w", encoding="utf-8") as f:
                    json.dump(items, f, ensure_ascii=False, indent=2)
            except Exception:
                pass

        except Exception as e:
            err_text = str(e)
            items = []

        fixtures_doc = load_json(FIXTURES_DIR / f"{league_id}.json") or {}
        for item in items[:10]:
            player, team_id, team_name, goals = extract_player_fields(item)
            if not team_id:
                team_rel = item.get("team") or {}
                team_data = team_rel.get("data") if isinstance(team_rel, dict) else {}
                team_id = team_id or (team_data.get("id") if isinstance(team_data, dict) else None)
                team_name = team_name or (team_data.get("name") if isinstance(team_data, dict) else None)
            fx = next_fixture_for_team(fixtures_doc, team_id) if team_id else None
            opponent = ha = None
            fixture_id = None
            if fx:
                opponent, ha = opponent_and_home_away(fx, team_id)
                fixture_id = fx.get("id")
            price = None
            if fixture_id:
                try:
                    price = find_anytime_price(league_id, fixture_id, player)
                except Exception as oe:
                    sys.stderr.write(f"[WARN] Odds parse failed L{league_id} F{fixture_id} {player}: {oe}\n")

            rows.append({
                "player": player,
                "team": team_name or "?",
                "goals": goals if goals is not None else "?",
                "opponent": opponent,
                "ha": ha,
                "odds": price,
            })

        blocks.append(render_league_block(league_id, rows, err_text))

    content = "\n".join(blocks).rstrip() + "\n"
    OUTFILE.write_text(content, encoding="utf-8")
    print(f"Wrote {OUTFILE} ({len(content)} bytes)")

if __name__ == "__main__":
    main()
