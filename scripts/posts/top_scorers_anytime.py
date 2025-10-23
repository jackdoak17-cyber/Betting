#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Top Scorers (Anytime) — Bet365 odds, clear output

Reads:
  fixtures:                  data/fixtures/by_league/{league_id}.json
  odds (preferred, league):  data/odds/b365/by_league/{league_id}.json
  odds (fallback, fixture):  data/odds/b365/fixtures/{fixture_id}.json

Calls:
  SportMonks (only for current season + flexible scorer discovery)
    /v3/football/leagues/{league_id}?include=currentSeason
    plus a set of tolerant discovery routes for top scorers

Env:
  SPORTMONKS_TOKEN   (required)
  LEAGUE_IDS         (comma-sep; default top-5: 8,564,82,384,301)
  LIMIT_PER_LEAGUE   (default 10)
  WINDOW_DAYS        (default 14) — only consider upcoming fixtures in this window
"""

import os, re, json, math, unicodedata
import datetime as dt
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Any
import requests

ROOT = Path(".")

# ---------- config ----------
LEAGUE_LABEL = {
    8:   "Premier League",
    564: "LaLiga",
    82:  "Bundesliga",
    384: "Serie A",
    301: "Ligue 1",
}
DEFAULT_LEAGUES = [8, 564, 82, 384, 301]
LIMIT_PER_LEAGUE = int(os.getenv("LIMIT_PER_LEAGUE", "10"))
WINDOW_DAYS = int(os.getenv("WINDOW_DAYS", "14"))

OUTFILE = ROOT / "betting" / "posts" / "top_scorers_anytime.md"
FIX_BY_LEAGUE = ROOT / "data" / "fixtures" / "by_league"
ODDS_BY_LEAGUE = ROOT / "data" / "odds" / "b365" / "by_league"
ODDS_BY_FIXTURE = ROOT / "data" / "odds" / "b365" / "fixtures"
DEBUG_DIR = ROOT / "data" / "debug"; DEBUG_DIR.mkdir(parents=True, exist_ok=True)

API = "https://api.sportmonks.com/v3/football"

# ---------- time ----------
def now_utc() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)

def now_utc_str() -> str:
    return now_utc().strftime("%Y-%m-%d %H:%M:%S UTC")

def parse_dt_utc(s: Any) -> Optional[dt.datetime]:
    if not s: return None
    try:
        if isinstance(s, (int, float)):
            return dt.datetime.fromtimestamp(int(s), tz=dt.timezone.utc)
        st = str(s)
        if "T" in st:
            return dt.datetime.fromisoformat(st.replace("Z","+00:00"))
        return dt.datetime.strptime(st, "%Y-%m-%d %H:%M:%S").replace(tzinfo=dt.timezone.utc)
    except Exception:
        return None

# ---------- string helpers ----------
def strip_accents(s: str) -> str:
    return ''.join(c for c in unicodedata.normalize('NFD', s or '') if unicodedata.category(c) != 'Mn')

def norm(s: str) -> str:
    s = strip_accents(s or "").lower()
    s = re.sub(r"[^a-z0-9\s\.-]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s

def redact(url: str) -> str:
    return re.sub(r"(api_token=)[^&]+", r"\1***redacted***", url or "")

# ---------- IO ----------
def read_json(path: Path) -> Optional[dict]:
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None

def write_text(path: Path, content: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")

# ---------- SportMonks calls (safe) ----------
def sm_get(url: str, token: str, **params) -> dict:
    p = dict(params or {})
    p["api_token"] = token
    r = requests.get(url, params=p, timeout=45)
    try:
        r.raise_for_status()
    except Exception as e:
        # redact token in error
        raise type(e)(f"{e} :: {redact(r.url)}")
    return r.json()

def get_current_season_id(league_id: int, token: str) -> Optional[int]:
    data = sm_get(f"{API}/leagues/{league_id}", token, include="currentSeason").get("data", {})
    cs = data.get("currentseason") or data.get("currentSeason") or {}
    return cs.get("id")

def discover_scorers_from_payload(payload: Any) -> List[dict]:
    """
    Find a 'top scorers' looking array anywhere in the payload.
    Accept items having player{}/player_id AND goals-ish key AND team-ish info.
    """
    def goals_val(x: dict) -> Optional[int]:
        for k in ("goals","value","scored","count","total_goals","goals_overall","goals_league"):
            if k in x:
                try: return int(x[k])
                except: pass
        return None

    def has_player(x: dict) -> bool:
        return ("player" in x) or ("player_id" in x) or ("player_name" in x)

    def has_teamish(x: dict) -> bool:
        return ("team" in x) or ("team_id" in x) or ("team_name" in x)

    best, best_len = [], 0

    def walk(o: Any):
        nonlocal best, best_len
        if isinstance(o, dict):
            for v in o.values(): walk(v)
        elif isinstance(o, list) and o:
            sample = [x for x in o if isinstance(x, dict)]
            if sample:
                ok = 0
                for x in sample[: min(5, len(sample))]:
                    if has_player(x) and has_teamish(x) and goals_val(x) is not None:
                        ok += 1
                if ok >= max(1, len(sample[:5])//2):  # looks like scorer rows
                    if len(sample) > best_len:
                        best, best_len = sample, len(sample)
            for v in o: walk(v)

    walk(payload)
    return best

def fetch_top_scorers_any_way(league_id: int, season_id: int, token: str) -> List[dict]:
    tried = []

    # 1) canonical on many accounts
    try:
        j = sm_get(f"{API}/topscorers/seasons/{season_id}", token, include="player;team")
        if j.get("data"): return j["data"]
    except Exception as e: tried.append(str(e))

    # 2) query variant
    try:
        j = sm_get(f"{API}/topscorers", token, seasons=str(season_id), include="player;team")
        if j.get("data"): return j["data"]
    except Exception as e: tried.append(str(e))

    # 3) more variants often enabled
    for url in (
        f"{API}/seasons/{season_id}/topscorers",
        f"{API}/leagues/{league_id}/topscorers",
        f"{API}/leagues/{league_id}/topscorers/seasons/{season_id}",
    ):
        try:
            j = sm_get(url, token, include="player;team")
            if j.get("data"): return j["data"]
        except Exception as e:
            tried.append(str(e))

    # 4) discovery on season payload (unknown includes are ignored gracefully)
    try:
        j = sm_get(f"{API}/seasons/{season_id}", token,
                   include="statistics;topscorers;players;teams;standings;stages")
        found = discover_scorers_from_payload(j)
        if found: return found
    except Exception as e: tried.append(str(e))

    # 5) discovery on league payload
    try:
        j = sm_get(f"{API}/leagues/{league_id}", token,
                   include="statistics;currentSeason;topscorers;seasons")
        found = discover_scorers_from_payload(j)
        if found: return found
    except Exception as e: tried.append(str(e))

    raise RuntimeError("All top-scorer routes failed:\n  " + "\n  ".join(tried))

def extract_row(item: dict) -> Tuple[str, Optional[int], str, Optional[int]]:
    # goals
    goals = item.get("goals") or item.get("value") or item.get("scored") or item.get("count") \
            or item.get("total_goals")
    try: goals = int(goals) if goals is not None else None
    except: goals = None

    # team
    team_id = item.get("team_id")
    team_name = item.get("team_name")
    t = item.get("team") or {}
    t = t.get("data", t) if isinstance(t, dict) else t
    if isinstance(t, dict):
        team_id = team_id or t.get("id")
        team_name = team_name or t.get("name")

    # player name
    player_name = item.get("player_name")
    p = item.get("player") or {}
    p = p.get("data", p) if isinstance(p, dict) else p
    if isinstance(p, dict):
        player_name = player_name or p.get("display_name") or p.get("common_name") \
                      or p.get("fullname") or p.get("name")
    return (player_name or "Unknown"), team_id, (team_name or "Unknown"), goals

# ---------- fixtures ----------
def load_fixtures(league_id: int) -> List[dict]:
    blob = read_json(FIX_BY_LEAGUE / f"{league_id}.json") or {}
    return blob.get("fixtures") or blob.get("data") or []

def next_fixture(fixtures: List[dict], team_id: Optional[int], team_name: str) -> Optional[dict]:
    if not fixtures: return None
    now = now_utc()
    best = None
    for fx in fixtures:
        dt_k = parse_dt_utc(fx.get("starting_at") or fx.get("starting_at_timestamp"))
        if not dt_k or dt_k < now or dt_k > now + dt.timedelta(days=WINDOW_DAYS):
            continue
        parts = fx.get("participants") or []
        for p in parts:
            if team_id and p.get("id") == team_id:
                best = min([best, fx], key=lambda x: parse_dt_utc(x.get("starting_at")) or dt.datetime.max.replace(tzinfo=dt.timezone.utc)) if best else fx
                break
            # fallback by name token overlap
            name = (p.get("name") or "").lower()
            if team_name and team_name.lower() in name:
                best = min([best, fx], key=lambda x: parse_dt_utc(x.get("starting_at")) or dt.datetime.max.replace(tzinfo=dt.timezone.utc)) if best else fx
                break
    return best

def opponent_and_side(fx: dict, team_id: Optional[int], team_name: str) -> Tuple[str, str]:
    opp, side = "?", "?"
    parts = fx.get("participants") or []
    for p in parts:
        pid = p.get("id")
        pname = p.get("name") or ""
        if (team_id and pid == team_id) or (not team_id and team_name.lower() in pname.lower()):
            loc = ((p.get("meta") or {}).get("location") or "").lower()
            side = "H" if loc == "home" else ("A" if loc == "away" else "?")
        else:
            opp = pname or "?"
    return opp, side

# ---------- odds ----------
ANYTIME_KEYS = ["anytime", "to score", "goalscorer", "goal scorer", "player to score", "score anytime"]
EXCLUDE_FIRST_LAST = ["first goalscorer","last goalscorer","first to score","last to score"]

def looks_anytime_market(name: str) -> bool:
    s = norm(name)
    if any(k in s for k in EXCLUDE_FIRST_LAST):  # exclude first/last
        return False
    return any(k in s for k in ANYTIME_KEYS)

def to_float(v) -> Optional[float]:
    try:
        if v in (None, "", "N/A"): return None
        s = str(v).strip()
        if "/" in s:
            a,b=s.split("/",1); return (float(a)/float(b))+1.0  # frac to dec
        return float(s)
    except Exception:
        return None

def find_fixture_odds_node(league_doc: dict, fixture_id: int) -> Optional[dict]:
    if not league_doc: return None
    key = str(fixture_id)
    if key in league_doc and isinstance(league_doc[key], dict):
        return league_doc[key]
    for tk in ("fixtures","data","events","matches"):
        arr = league_doc.get(tk)
        if isinstance(arr, list):
            for n in arr:
                if isinstance(n, dict) and (n.get("id")==fixture_id or n.get("fixture_id")==fixture_id):
                    return n
    # deep walk
    def walk(o: Any):
        if isinstance(o, dict):
            if o.get("fixture_id")==fixture_id or o.get("id")==fixture_id:
                if "bookmakers" in o or "markets" in o or "odds" in o:
                    return o
            for v in o.values():
                r = walk(v); 
                if r: return r
        elif isinstance(o, list):
            for v in o:
                r = walk(v)
                if r: return r
        return None
    return walk(league_doc)

def select_bet365_block(node: dict) -> dict:
    # prefer explicit bookmaker lists
    books = node.get("bookmakers")
    if isinstance(books, list) and books:
        for b in books:
            n = norm(b.get("name") or b.get("key") or "")
            if "365" in n or "bet365" in n:
                return b
        return books[0]
    return node

def collect_anytime_runners(block: dict) -> List[dict]:
    runners = []
    def visit(o: Any, current_name: Optional[str]=None):
        if isinstance(o, dict):
            market_name = current_name
            nm = o.get("name") or o.get("key") or o.get("market")
            if nm: market_name = nm
            sels = o.get("selections") or o.get("runners") or o.get("outcomes")
            if isinstance(sels, list) and looks_anytime_market(market_name or ""):
                for s in sels:
                    label = s.get("name") or s.get("runner") or s.get("label")
                    price = (to_float(s.get("odds_decimal")) or to_float(s.get("decimal")) or
                             to_float(s.get("price")) or to_float(s.get("odds")) or to_float(s.get("value")))
                    if label and price:
                        runners.append({"name": label, "price": price})
            for v in o.values(): visit(v, market_name)
        elif isinstance(o, list):
            for v in o: visit(v, current_name)
    visit(block, None)
    return runners

def name_match_score(player: str, label: str) -> int:
    p = norm(player); l = norm(label)
    if p == l: return 100
    parts = p.split()
    if parts and parts[-1] in l: return 75  # last name match
    if p in l: return 60
    return 0

def find_anytime_price_for_player(league_id: int, fixture_id: int, player_name: str) -> Optional[float]:
    # try league blob first
    league_doc = read_json(ODDS_BY_LEAGUE / f"{league_id}.json") or {}
    node = find_fixture_odds_node(league_doc, fixture_id)
    if not node:
        # fall back to per-fixture file (SportMonks normalized odds rows style)
        fx_doc = read_json(ODDS_BY_FIXTURE / f"{fixture_id}.json") or {}
        # if this file is already flattened rows style {"odds":[...]} try to parse
        if fx_doc.get("bookmakers") or fx_doc.get("markets"):
            runners = collect_anytime_runners(select_bet365_block(fx_doc))
            best = None; best_score = -1
            for r in runners:
                sc = name_match_score(player_name, r["name"])
                if sc > best_score:
                    best_score, best = sc, r.get("price")
            return best
        rows = fx_doc.get("odds") or []
        # rows-style (SportMonks odds rows) — anytime detection by market_description
        best, best_score = None, -1
        for r in rows:
            if int(r.get("bookmaker_id") or 0) != 2:  # Bet365 only
                continue
            if r.get("stopped"):                      # market closed
                continue
            if not looks_anytime_market(r.get("market_description","")):
                continue
            sc = name_match_score(player_name, r.get("name") or r.get("total") or "")
            if sc <= best_score: 
                continue
            price = to_float(r.get("value"))
            if price is not None:
                best, best_score = price, sc
        return best

    # parse nested bookmaker structure
    block = select_bet365_block(node)
    runners = collect_anytime_runners(block)
    best, best_score = None, -1
    for r in runners:
        sc = name_match_score(player_name, r["name"])
        if sc > best_score:
            best_score, best = sc, r.get("price")
    return best

# ---------- render ----------
def league_block(title: str, rows: List[dict], err: Optional[str]) -> str:
    out = [f"## {title}"]
    if err:
        out.append(f"> Error fetching top scorers: {err}")
        out.append("")
    if not rows:
        out.append("_No data._")
        out.append("")
        return "\n".join(out)
    for i, r in enumerate(rows, 1):
        base = f"{i}. {r['player']} — {r['team']} — {r.get('goals','?')}"
        if r.get("odds"):
            extra = f" — **Bet365 Anytime:** {r['odds']:.2f}"
            if r.get("opponent"): extra += f" (vs {r['opponent']}, {r.get('side','?')})"
            out.append(base + extra)
        else:
            out.append(base + " — Odds: N/A")
    out.append("")
    return "\n".join(out)

# ---------- main ----------
def main():
    token = os.getenv("SPORTMONKS_TOKEN")
    if not token:
        raise SystemExit("ERROR: SPORTMONKS_TOKEN is required")

    # leagues
    env = os.getenv("LEAGUE_IDS","").strip()
    if env:
        leagues = [int(x) for x in env.split(",") if x.strip()]
    else:
        leagues = DEFAULT_LEAGUES

    header = f"# Top Scorers (Anytime) — Updated {now_utc_str()}\n\n"
    blocks: List[str] = [header]

    for lid in leagues:
        title = LEAGUE_LABEL.get(lid, f"League {lid}")
        err = None; items: List[dict] = []
        try:
            season_id = get_current_season_id(lid, token)
            if not season_id:
                raise RuntimeError("No current season found")
            items = fetch_top_scorers_any_way(lid, season_id, token)
            # keep a debug snapshot
            try:
                (DEBUG_DIR / f"topscorers_raw_{lid}_{season_id}.json").write_text(
                    json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8"
                )
            except Exception:
                pass
        except Exception as e:
            err = str(e)

        fixtures = load_fixtures(lid)
        rows = []
        for it in (items[:LIMIT_PER_LEAGUE] if items else []):
            player, team_id, team_name, goals = extract_row(it)
            fx = next_fixture(fixtures, team_id, team_name)
            opp = side = None; fixture_id = None
            if fx:
                opp, side = opponent_and_side(fx, team_id, team_name)
                fixture_id = int(fx.get("id") or 0)
            odds = None
            if fixture_id:
                try:
                    odds = find_anytime_price_for_player(lid, fixture_id, player)
                except Exception:
                    odds = None
            rows.append({
                "player": player,
                "team": team_name or "?",
                "goals": goals if isinstance(goals,int) else "?",
                "opponent": opp,
                "side": side,
                "odds": odds,
            })

        blocks.append(league_block(title, rows, err))

    content = "\n".join(blocks).rstrip() + "\n"
    write_text(OUTFILE, content)
    print(f"Wrote {OUTFILE} ({len(content)} bytes)")

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
