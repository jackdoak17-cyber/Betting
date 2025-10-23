#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Top Scorers (Anytime) — pulls live top scorers from SportMonks, then
matches Bet365 'Anytime Goalscorer' odds from your saved JSON.

Writes: betting/posts/top_scorers_anytime.md

Reads local:
  data/fixtures/by_league/{league_id}.json
  data/odds/b365/by_league/{league_id}.json   (preferred)
  data/odds/b365/fixtures/{fixture_id}.json   (fallback)

Env (required):
  SPORTMONKS_TOKEN

Env (optional):
  LEAGUE_IDS        default "8,564,82,384,301" (EPL, LaLiga, Bundesliga, Serie A, Ligue 1)
  LIMIT_PER_LEAGUE  default 10
  WINDOW_DAYS       default 14
"""

import os, re, json, unicodedata, math, datetime as dt
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
import requests

ROOT = Path(".")
POST_OUT = ROOT / "betting" / "posts" / "top_scorers_anytime.md"
FIX_DIR  = ROOT / "data" / "fixtures" / "by_league"
ODDS_LEAGUE = ROOT / "data" / "odds" / "b365" / "by_league"
ODDS_FIX    = ROOT / "data" / "odds" / "b365" / "fixtures"
DEBUG_DIR   = ROOT / "data" / "debug"; DEBUG_DIR.mkdir(parents=True, exist_ok=True)

API = "https://api.sportmonks.com/v3/football"
LEAGUE_LABEL = {8:"Premier League", 564:"LaLiga", 82:"Bundesliga", 384:"Serie A", 301:"Ligue 1"}

LIMIT_PER_LEAGUE = int(os.getenv("LIMIT_PER_LEAGUE","10"))
WINDOW_DAYS = int(os.getenv("WINDOW_DAYS","14"))

# ---------- tiny utils ----------
def now_utc() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)

def now_str() -> str:
    return now_utc().strftime("%Y-%m-%d %H:%M:%S UTC")

def parse_dt_utc(s: Any) -> Optional[dt.datetime]:
    if not s: return None
    try:
        if isinstance(s,(int,float)): return dt.datetime.fromtimestamp(int(s), tz=dt.timezone.utc)
        st = str(s)
        if "T" in st: return dt.datetime.fromisoformat(st.replace("Z","+00:00"))
        return dt.datetime.strptime(st, "%Y-%m-%d %H:%M:%S").replace(tzinfo=dt.timezone.utc)
    except Exception:
        return None

def read_json(p: Path) -> Optional[dict]:
    try:
        with p.open("r", encoding="utf-8") as f: return json.load(f)
    except Exception:
        return None

def write_text(p: Path, s: str):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(s, encoding="utf-8")

def strip_accents(s: str) -> str:
    return ''.join(c for c in unicodedata.normalize('NFD', s or '') if unicodedata.category(c) != 'Mn')

def norm(s: str) -> str:
    s = strip_accents(s or "").lower()
    s = re.sub(r"[^a-z0-9\s\.-]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s

def redact(url: str) -> str:
    return re.sub(r"(api_token=)[^&]+", r"\1***redacted***", url or "")

# ---------- SportMonks ----------
def sm_get(path: str, token: str, **params) -> dict:
    params = dict(params or {})
    params["api_token"] = token
    r = requests.get(path, params=params, timeout=45)
    try:
        r.raise_for_status()
    except Exception as e:
        raise type(e)(f"{e} :: {redact(r.url)}")
    return r.json()

def get_current_season_id(league_id: int, token: str) -> Optional[int]:
    j = sm_get(f"{API}/leagues/{league_id}", token, include="currentSeason").get("data", {})
    cs = j.get("currentseason") or j.get("currentSeason") or {}
    return cs.get("id")

def _discover_scorer_rows(payload: Any) -> List[dict]:
    """
    Heuristic: find the biggest array of dicts that look like {player..., team..., goals...}
    """
    def has_goals(d: dict) -> bool:
        for k in ("goals","value","scored","count","total_goals","goals_overall","goals_league"):
            if k in d:
                try: int(d[k]); return True
                except: pass
        return False
    def has_player(d: dict) -> bool:
        return any(k in d for k in ("player","player_id","player_name"))
    def has_team(d: dict) -> bool:
        return any(k in d for k in ("team","team_id","team_name"))

    best, best_len = [], 0
    def walk(o: Any):
        nonlocal best, best_len
        if isinstance(o, dict):
            for v in o.values(): walk(v)
        elif isinstance(o, list) and o:
            cand = [x for x in o if isinstance(x, dict)]
            if cand:
                sample = cand[: min(6, len(cand))]
                ok = sum(1 for x in sample if has_player(x) and has_team(x) and has_goals(x))
                if ok >= max(1, len(sample)//2) and len(cand) > best_len:
                    best, best_len = cand, len(cand)
            for v in o: walk(v)
    walk(payload)
    return best

def fetch_top_scorers(league_id: int, season_id: int, token: str) -> Tuple[List[dict], str]:
    """
    Returns (rows, source_used). Tries multiple routes; last resort: discover in league/season payloads.
    """
    tried = []

    # A) official — may be disabled on some plans
    url = f"{API}/topscorers/seasons/{season_id}"
    try:
        j = sm_get(url, token, include="player;team")
        if j.get("data"): return j["data"], url
    except Exception as e: tried.append(str(e))

    # B) query form
    url = f"{API}/topscorers"
    try:
        j = sm_get(url, token, seasons=str(season_id), include="player;team")
        if j.get("data"): return j["data"], url+"?seasons="
    except Exception as e: tried.append(str(e))

    # C) other plausible forms people use
    for url in (
        f"{API}/seasons/{season_id}/topscorers",
        f"{API}/leagues/{league_id}/topscorers",
        f"{API}/leagues/{league_id}/topscorers/seasons/{season_id}",
        f"{API}/statistics/seasons/{season_id}/topscorers",
    ):
        try:
            j = sm_get(url, token, include="player;team")
            if j.get("data"): return j["data"], url
        except Exception as e:
            tried.append(str(e))

    # D) discover inside season payload
    url = f"{API}/seasons/{season_id}"
    try:
        j = sm_get(url, token, include="statistics;topscorers;players;teams;standings;stages")
        rows = _discover_scorer_rows(j)
        if rows: return rows, url+"?include=*discover*"
    except Exception as e: tried.append(str(e))

    # E) discover inside league payload
    url = f"{API}/leagues/{league_id}"
    try:
        j = sm_get(url, token, include="statistics;currentSeason;topscorers;seasons")
        rows = _discover_scorer_rows(j)
        if rows: return rows, url+"?include=*discover*"
    except Exception as e: tried.append(str(e))

    raise RuntimeError("All top-scorer routes failed:\n  " + "\n  ".join(tried))

def extract_row(item: dict) -> Tuple[str, Optional[int], str, Optional[int]]:
    goals = None
    for k in ("goals","value","scored","count","total_goals","goals_overall","goals_league"):
        if k in item:
            try: goals = int(item[k]); break
            except: pass

    team_id = item.get("team_id")
    team_name = item.get("team_name")
    t = item.get("team") or {}
    t = t.get("data", t) if isinstance(t, dict) else t
    if isinstance(t, dict):
        team_id = team_id or t.get("id")
        team_name = team_name or t.get("name")

    player_name = item.get("player_name")
    p = item.get("player") or {}
    p = p.get("data", p) if isinstance(p, dict) else p
    if isinstance(p, dict):
        player_name = (player_name or p.get("display_name") or p.get("common_name")
                       or p.get("fullname") or p.get("name"))
    return (player_name or "Unknown"), team_id, (team_name or "Unknown"), goals

# ---------- fixtures & odds ----------
def load_fixtures(league_id: int) -> List[dict]:
    blob = read_json(FIX_DIR / f"{league_id}.json") or {}
    return blob.get("fixtures") or blob.get("data") or []

def next_fixture(fixtures: List[dict], team_id: Optional[int], team_name: str) -> Optional[dict]:
    now = now_utc()
    best = None
    for fx in fixtures:
        dt_k = parse_dt_utc(fx.get("starting_at") or fx.get("starting_at_timestamp"))
        if not dt_k or dt_k < now or dt_k > now + dt.timedelta(days=WINDOW_DAYS):
            continue
        for p in (fx.get("participants") or []):
            if (team_id and p.get("id")==team_id) or (team_name and team_name.lower() in (p.get("name") or "").lower()):
                best = fx if not best or dt_k < parse_dt_utc(best.get("starting_at")) else best
                break
    return best

def opponent_and_side(fx: dict, team_id: Optional[int], team_name: str) -> Tuple[str,str]:
    opp, side = "?", "?"
    parts = fx.get("participants") or []
    for p in parts:
        pid = p.get("id"); pname = p.get("name") or ""
        if (team_id and pid==team_id) or (not team_id and team_name.lower() in pname.lower()):
            loc = ((p.get("meta") or {}).get("location") or "").lower()
            side = "H" if loc=="home" else ("A" if loc=="away" else "?")
        else:
            opp = pname or "?"
    return opp, side

ANYTIME_KEYS = ["anytime","to score","goalscorer","goal scorer","player to score","score anytime"]
EXCLUDE_FIRST_LAST = ["first goalscorer","last goalscorer","first to score","last to score"]

def looks_anytime_market(name: str) -> bool:
    s = norm(name)
    if any(k in s for k in EXCLUDE_FIRST_LAST): return False
    return any(k in s for k in ANYTIME_KEYS)

def to_float(v) -> Optional[float]:
    try:
        if v in (None,"","N/A"): return None
        s = str(v).strip()
        if "/" in s:
            a,b = s.split("/",1)
            return (float(a)/float(b))+1.0
        return float(s)
    except Exception:
        return None

def find_fixture_odds_node(league_doc: dict, fixture_id: int) -> Optional[dict]:
    if not league_doc: return None
    key = str(fixture_id)
    if key in league_doc and isinstance(league_doc[key], dict):
        return league_doc[key]
    for tk in ("fixtures","data","events","matches","items"):
        arr = league_doc.get(tk)
        if isinstance(arr, list):
            for n in arr:
                if isinstance(n, dict) and (n.get("id")==fixture_id or n.get("fixture_id")==fixture_id):
                    return n
    def walk(o: Any):
        if isinstance(o, dict):
            if (o.get("fixture_id")==fixture_id or o.get("id")==fixture_id) and any(k in o for k in ("bookmakers","markets","odds")):
                return o
            for v in o.values():
                r = walk(v)
                if r: return r
        elif isinstance(o, list):
            for v in o:
                r = walk(v)
                if r: return r
        return None
    return walk(league_doc)

def select_bet365_block(node: dict) -> dict:
    books = node.get("bookmakers")
    if isinstance(books, list) and books:
        for b in books:
            nm = norm(b.get("name") or b.get("key") or "")
            if "365" in nm or "bet365" in nm:
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
    if parts and parts[-1] in l: return 75
    if p in l: return 60
    return 0

def find_anytime_price_for_player(league_id: int, fixture_id: int, player_name: str) -> Optional[float]:
    # league-level odds JSON
    league_doc = read_json(ODDS_LEAGUE / f"{league_id}.json") or {}
    node = find_fixture_odds_node(league_doc, fixture_id)
    if node:
        block = select_bet365_block(node)
        runners = collect_anytime_runners(block)
        best, best_score = None, -1
        for r in runners:
            sc = name_match_score(player_name, r["name"])
            if sc > best_score:
                best_score, best = sc, r.get("price")
        if best: return best

    # fallback: per-fixture odds file — two forms supported
    fx_doc = read_json(ODDS_FIX / f"{fixture_id}.json") or {}
    if fx_doc.get("bookmakers") or fx_doc.get("markets"):
        block = select_bet365_block(fx_doc)
        runners = collect_anytime_runners(block)
        best, best_score = None, -1
        for r in runners:
            sc = name_match_score(player_name, r["name"])
            if sc > best_score:
                best_score, best = sc, r.get("price")
        return best

    # SportMonks rows-style fallback
    rows = fx_doc.get("odds") or []
    best, best_score = None, -1
    for r in rows:
        if int(r.get("bookmaker_id") or 0) != 2:   # Bet365 only
            continue
        if r.get("stopped"):                       # market closed
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

# ---------- render ----------
def league_block(title: str, rows: List[dict], err: Optional[str], source: Optional[str]) -> str:
    out = [f"## {title}"]
    if err:
        out.append(f"> Error fetching top scorers: {err}")
    elif source:
        out.append(f"> Source: {source}")
    out.append("")
    if not rows:
        out.append("_No data._\n")
        return "\n".join(out)

    for i, r in enumerate(rows, 1):
        base = f"{i}. {r['player']} — {r['team']} — {r.get('goals','?')}"
        if r.get("odds") is not None:
            extra = f" — **Bet365 Anytime:** {r['odds']:.2f}"
            if r.get("opponent"):
                extra += f" (vs {r['opponent']}, {r.get('side','?')})"
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

    env = os.getenv("LEAGUE_IDS","").strip()
    leagues = [int(x) for x in env.split(",") if x.strip()] if env else [8,564,82,384,301]

    blocks = [f"# Top Scorers (Anytime) — Updated {now_str()}\n"]
    for lid in leagues:
        title = LEAGUE_LABEL.get(lid, f"League {lid}")
        rows, err, source_used = [], None, None
        try:
            season_id = get_current_season_id(lid, token)
            if not season_id:
                raise RuntimeError("No current season found")
            items, source_used = fetch_top_scorers(lid, season_id, token)
            # keep a raw snapshot for troubleshooting
            try:
                (DEBUG_DIR / f"topscorers_raw_{lid}_{season_id}.json").write_text(
                    json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8"
                )
            except Exception:
                pass
        except Exception as e:
            err = str(e)
            items = []

        fixtures = load_fixtures(lid)
        for it in items[:LIMIT_PER_LEAGUE]:
            player, team_id, team_name, goals = extract_row(it)
            fx = next_fixture(fixtures, team_id, team_name)
            opp = side = None; fixture_id = None
            if fx:
                opp, side = opponent_and_side(fx, team_id, team_name)
                fixture_id = int(fx.get("id") or 0)
            odds = find_anytime_price_for_player(lid, fixture_id, player) if fixture_id else None
            rows.append({
                "player": player, "team": team_name or "?", "goals": goals if isinstance(goals,int) else "?",
                "opponent": opp, "side": side, "odds": odds
            })

        blocks.append(league_block(title, rows, err, source_used))

    content = "\n".join(blocks).rstrip() + "\n"
    write_text(POST_OUT, content)
    print(f"Wrote {POST_OUT} ({len(content)} bytes)")

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
