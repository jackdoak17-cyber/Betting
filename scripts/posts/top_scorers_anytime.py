#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Post 1 — Goal scorer odds
("Boot & Book — Top Scorers × Anytime Odds")

What’s new (per API Coach):
- Use `filter=` (not `filters=`) with seasonTopscorerTypes:208 / stageTopscorerTypes:208.
- Resolve stage via /seasons/{season_id}?include=stages (pick 'Regular Season').

Outputs:
- reports/social/top_scorers_anytime_YYYYMMDD.md
- data/social_media_posts/top_scorers_anytime_YYYYMMDD.txt
"""

import os
import re
import json
import time
import datetime as dt
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Any

import requests

# ---------------- Config & env ----------------
API_BASE = "https://api.sportmonks.com/v3/football"
API_TOKEN = (
    os.getenv("SPORTMONKS_TOKEN")
    or os.getenv("SPORTMONKS_API_TOKEN")
    or os.getenv("SM_TOKEN")
)
if not API_TOKEN:
    raise SystemExit("ERROR: SPORTMONKS_TOKEN not set")

LEAGUE_IDS = [int(x) for x in os.getenv("LEAGUE_IDS", "8,564,384,82,301").split(",") if x.strip()]
TOP_N = int(os.getenv("TOP_N", "10"))
BOOKMAKER_IDS = (os.getenv("BOOKMAKER_IDS", "").strip() or None)
MARKET_IDS = (os.getenv("MARKET_IDS", "").strip() or None)
ODDS_FALLBACK_ALL = (os.getenv("ODDS_FALLBACK_ALL", "1").strip() == "1")

# ---------------- HTTP helpers ----------------
TIMEOUT = 25
RETRIES = 3
BACKOFF = 1.7
PACE = 0.18
_last_call = 0.0

def _pace():
    global _last_call
    now = time.time()
    if now - _last_call < PACE:
        time.sleep(PACE - (now - _last_call))
    _last_call = time.time()

class NotFound(Exception): pass

def api_get(path: str, params: Optional[dict] = None) -> dict:
    """Standard GET with retry/backoff (raises on non-2xx)."""
    params = params or {}
    params["api_token"] = API_TOKEN
    url = f"{API_BASE}/{path.lstrip('/')}"
    err = None
    for i in range(RETRIES):
        _pace()
        try:
            r = requests.get(url, params=params, timeout=TIMEOUT)
            if r.status_code == 429:
                time.sleep(min(60, (BACKOFF ** (i+1)) * 2.0))
                continue
            r.raise_for_status()
            return r.json()
        except Exception as e:
            err = e
            if i + 1 < RETRIES:
                time.sleep(BACKOFF ** (i+1))
    raise err

def api_get_or_404(path: str, params: Optional[dict] = None) -> dict:
    """GET that raises NotFound on 404, so we can branch cleanly."""
    params = params or {}
    params["api_token"] = API_TOKEN
    url = f"{API_BASE}/{path.lstrip('/')}"
    err = None
    for i in range(RETRIES):
        _pace()
        try:
            r = requests.get(url, params=params, timeout=TIMEOUT)
            if r.status_code == 404:
                raise NotFound(f"404 for {url}")
            if r.status_code == 429:
                time.sleep(min(60, (BACKOFF ** (i+1)) * 2.0))
                continue
            r.raise_for_status()
            return r.json()
        except NotFound:
            raise
        except Exception as e:
            err = e
            if i + 1 < RETRIES:
                time.sleep(BACKOFF ** (i+1))
    raise err

# ---------------- IO paths ----------------
FIX_DIR = Path("data/fixtures/by_league")
OUT_MD_DIR = Path("reports/social")
OUT_TXT_DIR = Path("data/social_media_posts")
OUT_MD_DIR.mkdir(parents=True, exist_ok=True)
OUT_TXT_DIR.mkdir(parents=True, exist_ok=True)

BIG5_LABELS = {
    8:   "Premier League",
    564: "La Liga",
    384: "Serie A",
    82:  "Bundesliga",
    301: "Ligue 1",
}

# ---------------- Fixtures helpers ----------------
def _parse_ko(s: Any) -> Optional[dt.datetime]:
    if s is None: return None
    if isinstance(s, (int, float)):
        try:
            return dt.datetime.utcfromtimestamp(int(s))
        except Exception:
            return None
    s = str(s).replace("T", " ").replace("Z", "")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return dt.datetime.strptime(s, fmt)
        except Exception:
            continue
    return None

def load_fixtures_blob(league_id: int) -> dict:
    p = FIX_DIR / f"{league_id}.json"
    if not p.is_file(): return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}

def team_next_fixture_map(blob: dict) -> Dict[int, dict]:
    """Map team_id -> earliest upcoming fixture (from local fixtures)."""
    now = dt.datetime.utcnow()
    best: Dict[int, dict] = {}

    for fx in (blob.get("fixtures") or []):
        fid = int(fx.get("id") or fx.get("fixture_id") or 0)
        ko = _parse_ko(fx.get("starting_at") or fx.get("starting_at_timestamp")) or now
        parts = fx.get("participants") or []
        teams = []
        for p in parts:
            try:
                tid = int(p.get("id"))
            except Exception:
                continue
            name = p.get("name") or f"Team {tid}"
            loc = ((p.get("meta") or {}).get("location") or "").lower()
            teams.append({"team_id": tid, "name": name, "loc": loc})
        if len(teams) != 2:
            continue
        home = next((t for t in teams if t["loc"] == "home"), teams[0])
        away = next((t for t in teams if t["loc"] == "away"), teams[-1])
        row = {
            "fixture_id": fid,
            "home_id": home["team_id"], "home": home["name"],
            "away_id": away["team_id"], "away": away["name"],
            "kickoff": ko,
        }
        for t in teams:
            prev = best.get(t["team_id"])
            if (prev is None) or (row["kickoff"] < prev["kickoff"]):
                best[t["team_id"]] = row
    return best

# ---------------- League -> current season ----------------
def league_current_season_id(league_id: int) -> Optional[int]:
    try:
        j = api_get_or_404(f"leagues/{league_id}", params={"include": "currentSeason"})
    except NotFound:
        print(f"[WARN] League {league_id}: not found")
        return None
    except Exception as e:
        print(f"[WARN] League {league_id}: error {e}")
        return None

    d = j.get("data") or {}
    cs = d.get("currentSeason") or d.get("current_season") or d.get("currentseason") or {}
    sid = cs.get("id") or d.get("current_season_id")
    try:
        return int(sid) if sid else None
    except Exception:
        return None

# ---------------- Stage discovery via /seasons/{id}?include=stages ----------------
def regular_stage_id_from_season(season_id: int) -> Optional[int]:
    try:
        j = api_get(f"seasons/{season_id}", params={"include": "stages", "per_page": 100})
    except Exception:
        return None
    data = j.get("data") or {}
    stages = data.get("stages") or []
    if not stages:
        return None

    def score(s: dict) -> int:
        # Prefer "Regular Season", generic "League", or anything with standings
        nm = (s.get("name") or "").lower()
        tp = (s.get("type") or "").lower()
        sc = 0
        if "regular" in nm or "regular" in tp: sc += 3
        if "league" in nm or "league" in tp:   sc += 2
        if s.get("has_standings"):             sc += 1
        return sc

    stages.sort(key=lambda s: (-score(s), s.get("id") or 0))
    sid = stages[0].get("id")
    try:
        return int(sid) if sid else None
    except Exception:
        return None

# ---------------- Topscorers parsing ----------------
GOAL_TYPE_ID = 208  # per API Coach

def _parse_topscorer_rows(payload: dict) -> List[dict]:
    """
    Normalize to: [{player_id, player_name, team_id, team_name, goals, type_id}]
    Shapes differ by endpoint; support both:
      - {"data":{"topscorers":[...]}} or {"data":{"goalscorers":[...]}}
      - {"data":[...]}
    """
    data = payload.get("data")
    if isinstance(data, dict):
        rows = data.get("topscorers") or data.get("goalscorers") or []
    else:
        rows = data or []
    out: List[dict] = []
    for t in rows:
        pl = (t.get("player") or {})
        tm = (t.get("team") or {})
        tp = (t.get("type") or {})
        goals = t.get("goals") if t.get("goals") is not None else t.get("total") or t.get("score") or 0
        out.append({
            "player_id": int(pl.get("id") or 0),
            "player_name": pl.get("name") or "",
            "team_id": int(tm.get("id") or 0),
            "team_name": tm.get("name") or "",
            "goals": int(goals),
            "type_id": int(tp.get("id") or 0),
        })
    out.sort(key=lambda r: (-r["goals"], r["player_name"].lower()))
    return out

def fetch_topscorers_goal_topN(season_id: int, top_n: int) -> List[dict]:
    """
    Season -> Goal topscorers with robust fallbacks:
    1) /topscorers/seasons/{sid}?filter=seasonTopscorerTypes:208
    2) If 404/empty: /topscorers/seasons/{sid} (no filter) then local filter to type_id==208
    3) If still empty: discover stage via /seasons/{sid}?include=stages, then
       /topscorers/stages/{stage}?filter=stageTopscorerTypes:208 (then unfiltered+local as last resort)
    """
    # 1) Season with goals filter
    try:
        j = api_get_or_404(
            f"topscorers/seasons/{season_id}",
            params={"filter": "seasonTopscorerTypes:208", "include": "player;team;type", "per_page": max(25, top_n)}
        )
        rows = [r for r in _parse_topscorer_rows(j) if r["type_id"] == GOAL_TYPE_ID]
        if rows:
            return rows[:top_n]
        print(f"[INFO] season {season_id}: goals filter returned 0; trying unfiltered")
    except NotFound:
        print(f"[WARN] season {season_id}: season topscorers 404; will try stage")
    except Exception as e:
        print(f"[WARN] season {season_id}: error {e}; trying unfiltered")

    # 2) Season unfiltered -> local filter
    try:
        j2 = api_get_or_404(
            f"topscorers/seasons/{season_id}",
            params={"include": "player;team;type", "per_page": max(50, top_n)}
        )
        rows2 = [r for r in _parse_topscorer_rows(j2) if r["type_id"] == GOAL_TYPE_ID]
        if rows2:
            return rows2[:top_n]
    except NotFound:
        pass
    except Exception as e:
        print(f"[WARN] season {season_id}: unfiltered error {e}")

    # 3) Stage fallback
    stage_id = regular_stage_id_from_season(season_id)
    if stage_id:
        print(f"[INFO] season {season_id}: using stage {stage_id}")
        try:
            j3 = api_get_or_404(
                f"topscorers/stages/{stage_id}",
                params={"filter": "stageTopscorerTypes:208", "include": "player;team;type", "per_page": max(50, top_n)}
            )
            rows3 = [r for r in _parse_topscorer_rows(j3) if r["type_id"] == GOAL_TYPE_ID]
            if rows3:
                return rows3[:top_n]
            print(f"[INFO] stage {stage_id}: goals filter returned 0; trying unfiltered")
            j4 = api_get_or_404(
                f"topscorers/stages/{stage_id}",
                params={"include": "player;team;type", "per_page": max(50, top_n)}
            )
            rows4 = [r for r in _parse_topscorer_rows(j4) if r["type_id"] == GOAL_TYPE_ID]
            if rows4:
                return rows4[:top_n]
        except NotFound:
            print(f"[WARN] stage {stage_id}: topscorers 404")
        except Exception as e:
            print(f"[WARN] stage {stage_id}: error {e}")

    return []

# ---------------- Odds (Anytime market) ----------------
ANYTIME_KEYS = ["anytime", "to score", "player to score", "goalscorer", "score at anytime"]
EXCLUDE_KEYS = ["first", "last", "2 or more", "two or more", "hat-trick", "hat trick"]

def looks_like_anytime_market(name: str) -> bool:
    s = (name or "").lower()
    if not s: return False
    if any(k in s for k in EXCLUDE_KEYS): return False
    return any(k in s for k in ANYTIME_KEYS)

def normalize(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())

def names_match(player_name: str, label: str) -> bool:
    a, b = normalize(player_name), normalize(label)
    return a == b or a in b or b in a

def pick_decimal_price(obj: dict) -> Optional[float]:
    for k in ("decimal", "price", "odd", "over", "yes", "value"):
        v = obj.get(k)
        try:
            if isinstance(v, (int, float)): return float(v)
            if isinstance(v, str): return float(v)
        except Exception:
            pass
    return None

def extract_anytime_price(markets: List[dict], player_name: str) -> Optional[Tuple[str, float]]:
    for m in markets or []:
        if not looks_like_anytime_market(m.get("name") or ""):
            continue
        odds = m.get("odds")
        if isinstance(odds, list):
            for opt in odds:
                label = opt.get("label") or opt.get("name") or ""
                if names_match(player_name, label):
                    price = pick_decimal_price(opt if isinstance(opt, dict) else {})
                    if price is not None:
                        bm = m.get("bookmaker_name") or m.get("bookmaker") or "Book"
                        return bm, price
        elif isinstance(odds, dict):
            for label, opt in odds.items():
                if names_match(player_name, str(label)):
                    price = pick_decimal_price(opt if isinstance(opt, dict) else {})
                    if price is not None:
                        bm = m.get("bookmaker_name") or m.get("bookmaker") or "Book"
                        return bm, price
    return None

def odds_for_fixture_anytime(fixture_id: int, player_name: str) -> Optional[Tuple[str, float]]:
    params = {"include": "odds"}
    fltrs = []
    if BOOKMAKER_IDS: fltrs.append(f"bookmakers:{BOOKMAKER_IDS}")
    if MARKET_IDS:    fltrs.append(f"markets:{MARKET_IDS}")
    if fltrs: params["filters"] = ";".join(fltrs)

    j = api_get(f"fixtures/{fixture_id}", params=params)
    fx = j.get("data") or {}
    markets = fx.get("odds") or []

    ms = []
    for m in markets:
        m = dict(m)
        if not m.get("bookmaker_name"):
            m["bookmaker_name"] = (m.get("bookmaker") or {}).get("name") or m.get("bookmaker_name") or "Book"
        ms.append(m)

    res = extract_anytime_price(ms, player_name)
    if res: return res
    if not MARKET_IDS and ODDS_FALLBACK_ALL:
        return extract_anytime_price(ms, player_name)
    return None

# ---------------- Rendering ----------------
def league_label(league_id: int) -> str:
    return BIG5_LABELS.get(league_id, f"League {league_id}")

def render_markdown(blocks: List[str]) -> str:
    return "\n".join(["## Boot & Book — Top Scorers × Anytime Odds", ""] + blocks) + "\n"

def render_text(blocks: List[str]) -> str:
    lines = ["Boot & Book — Top Scorers × Anytime Odds", ""]
    for block in blocks:
        for ln in block.splitlines():
            if ln.startswith("|---"): continue
            if ln.startswith("|"):
                cols = [c.strip() for c in ln.strip("|").split("|")]
                if len(cols) >= 7:
                    lines.append(f"{cols[0]}. {cols[1]} — {cols[2]} — Goals: {cols[3]} — Anytime: {cols[4]} — {cols[5]} — KO {cols[6]}")
            else:
                lines.append(ln)
        lines.append("")
    return "\n".join(lines) + "\n"

# ---------------- Main ----------------
def main():
    today = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d")
    blocks: List[str] = []

    for league_id in LEAGUE_IDS:
        label = league_label(league_id)

        # Current season via League include
        season_id = league_current_season_id(league_id)
        if not season_id:
            print(f"[WARN] {label}: could not resolve current season; skipping")
            blocks.append("\n".join([f"### {label} (LID {league_id})", "", "_Could not resolve current season_; skipped.", ""]))
            continue

        # Local fixtures for odds/join
        fx_blob = load_fixtures_blob(league_id)
        team_fx = team_next_fixture_map(fx_blob) if fx_blob.get("fixtures") else {}
        if not team_fx:
            print(f"[INFO] {label}: no local fixtures found; odds may be blank")

        # Fetch topscorers
        top_rows = fetch_topscorers_goal_topN(season_id, TOP_N)
        print(f"[INFO] {label}: season_id={season_id} top_rows={len(top_rows)}")

        # Build table block
        lines = [
            f"### {label} (LID {league_id})",
            "",
            "| # | Player | Team | Goals | Anytime | Fixture | KO (UTC) |",
            "|---:|:------|:-----|------:|:-------:|:-------|:---------|",
        ]
        if not top_rows:
            lines.append("| — | — | — | — | — | — | — |")
        else:
            for rank, r in enumerate(top_rows, start=1):
                team_id = r["team_id"]
                fx = team_fx.get(team_id)
                price_txt = "—"
                fixture_txt = "—"
                ko_txt = "—"
                if fx:
                    fixture_txt = f"{fx['home']} vs {fx['away']}"
                    ko_txt = fx["kickoff"].strftime("%Y-%m-%d %H:%M")
                    res = odds_for_fixture_anytime(fx["fixture_id"], r["player_name"])
                    if res:
                        bm, price = res
                        price_txt = f"{price:.2f} ({bm})"
                lines.append(f"| {rank} | {r['player_name']} | {r['team_name']} | {r['goals']} | {price_txt} | {fixture_txt} | {ko_txt} |")
        lines.append("")
        blocks.append("\n".join(lines))

    # Write outputs
    md = render_markdown(blocks)
    out_md = OUT_MD_DIR / f"top_scorers_anytime_{today}.md"
    out_md.write_text(md, encoding="utf-8")
    print(f"[OK] wrote {out_md}")

    txt = render_text(blocks)
    out_txt = OUT_TXT_DIR / f"top_scorers_anytime_{today}.txt"
    out_txt.write_text(txt, encoding="utf-8")
    print(f"[OK] wrote {out_txt}")

if __name__ == "__main__":
    main()
