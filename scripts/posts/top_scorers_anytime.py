#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Post 1 — Goal scorer odds
(AKA "Boot & Book — Top Scorers × Anytime Odds")

What it does
------------
For each league (defaults to the Big 5: EPL, LaLiga, Serie A, Bundesliga, Ligue 1):
1) Infers the current season_id from your local fixtures file:
     data/fixtures/by_league/{league_id}.json
2) Fetches Top Scorers for that season (Season endpoint first; falls back to Stage on 404).
   Filters to goal-scorer rows (type_id == 208), keeps Top N (default 10).
3) Finds each player’s NEXT fixture from the fixtures file.
4) Pulls odds for that fixture with include=odds and extracts an "Anytime Goalscorer" price
   for the player (by market name matching, unless you set MARKET_IDS).
5) Writes:
   - Markdown table per league to: reports/social/top_scorers_anytime_YYYYMMDD.md
   - Plain text post to:          data/social_media_posts/top_scorers_anytime_YYYYMMDD.txt

Env vars (optional)
-------------------
SPORTMONKS_TOKEN   : your API token (required)
LEAGUE_IDS         : comma-separated league IDs (default "8,564,384,82,301")
TOP_N              : number of scorers to list (default "10")
BOOKMAKER_IDS      : e.g. "2" for Bet365; comma-separated (optional)
MARKET_IDS         : Anytime market id(s), comma-separated (optional)
ODDS_FALLBACK_ALL  : "1" to scan market names for Anytime when MARKET_IDS unset (default "1")
"""

import os
import re
import json
import time
import datetime as dt
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Any

import requests

# ---------- API + pacing ----------
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

class NotFound(Exception):
    pass

def api_get(path: str, params: Optional[dict] = None) -> dict:
    """Normal GET with retry/backoff; raises for non-2xx."""
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
    """GET that throws NotFound on 404 (for clean stage fallback)."""
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

# ---------- IO ----------
FIX_DIR = Path("data/fixtures/by_league")
OUT_MD_DIR = Path("reports/social")
OUT_TXT_DIR = Path("data/social_media_posts")
OUT_MD_DIR.mkdir(parents=True, exist_ok=True)
OUT_TXT_DIR.mkdir(parents=True, exist_ok=True)

# Optional nice names (used when fixtures blob lacks league_name)
BIG5_LABELS = {
    8:   "Premier League",
    564: "La Liga",
    384: "Serie A",
    82:  "Bundesliga",
    301: "Ligue 1",
}

# ---------- Fixtures helpers ----------
def _parse_ko(s: Any) -> Optional[dt.datetime]:
    if s is None:
        return None
    if isinstance(s, (int, float)):  # starting_at_timestamp (if present)
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
    if not p.is_file():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}

def infer_season_id_from_fixtures(blob: dict) -> Optional[int]:
    ctr: Dict[int, int] = {}
    for fx in (blob.get("fixtures") or []):
        sid = fx.get("season_id")
        if isinstance(sid, int) and sid > 0:
            ctr[sid] = ctr.get(sid, 0) + 1
    if not ctr:
        return None
    return max(ctr.items(), key=lambda kv: kv[1])[0]

def team_next_fixture_map(blob: dict) -> Dict[int, dict]:
    """
    From fixtures blob, map team_id -> earliest upcoming fixture row with:
      {fixture_id, home_id, home, away_id, away, kickoff (UTC)}
    """
    now = dt.datetime.utcnow()
    best: Dict[int, dict] = {}

    for fx in (blob.get("fixtures") or []):
        fid = int(fx.get("id") or fx.get("fixture_id") or 0)
        ko = _parse_ko(fx.get("starting_at") or fx.get("starting_at_timestamp")) or now

        # participants: [{id, name, meta: {location: home/away}}]
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

# ---------- Topscorers (Season first, Stage fallback) ----------
def preferred_stage_id_from_fixtures(blob: dict) -> Optional[int]:
    counts: Dict[int, int] = {}
    for fx in (blob.get("fixtures") or []):
        sid = fx.get("stage_id") or (fx.get("stage") or {}).get("id")
        if isinstance(sid, int) and sid > 0:
            counts[sid] = counts.get(sid, 0) + 1
    if counts:
        return max(counts.items(), key=lambda kv: kv[1])[0]
    return None

def regular_stage_id_from_api(season_id: int) -> Optional[int]:
    j = api_get(f"stages/seasons/{season_id}", params={"per_page": 50})
    rows = j.get("data") or []
    if not rows:
        return None
    def key(r):
        nm = (r.get("name") or "").lower()
        tp = (r.get("type") or "").lower()
        score = 0
        if "regular" in nm or "regular" in tp: score += 3
        if "league" in nm or "league" in tp:   score += 2
        if r.get("has_standings"):             score += 1
        return -score
    rows.sort(key=key)
    return int(rows[0].get("id") or 0) or None

def _parse_topscorer_rows(payload: dict) -> List[dict]:
    """
    Normalize various shapes into:
      [{player_id, player_name, team_id, team_name, goals, type_id}]
    """
    data = payload.get("data") or {}
    rows = data.get("topscorers") or data.get("goalscorers") or data.get("data") or []
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

def fetch_topscorers_goal_topN(season_id: int, fixtures_blob: dict, top_n: int) -> List[dict]:
    """Return Top N goal scorers (type_id == 208). Tries Season (with filter) then Stage."""
    try:
        j = api_get_or_404(
            f"topscorers/seasons/{season_id}",
            params={
                "include": "player;team;type",
                "per_page": max(25, top_n),
                "filters": "seasonTopscorerTypes:208",  # explicit Goals filter
            },
        )
        rows = _parse_topscorer_rows(j)
        if rows:
            return rows[:top_n]
    except NotFound:
        pass
    except Exception as e:
        print(f"[WARN] Season topscorers failed for season {season_id}: {e}")

    # Stage fallback (prefer stage_id from fixtures; else pick a 'regular' stage)
    stage_id = preferred_stage_id_from_fixtures(fixtures_blob) or regular_stage_id_from_api(season_id)
    if stage_id:
        try:
            j = api_get_or_404(
                f"topscorers/stages/{stage_id}",
                params={
                    "include": "player;team;type",
                    "per_page": max(25, top_n),
                    "filters": "stageTopscorerTypes:208",  # goals on stage
                },
            )
            rows = _parse_topscorer_rows(j)
            if rows:
                return rows[:top_n]
        except Exception as e:
            print(f"[WARN] Stage topscorers failed for stage {stage_id}: {e}")

    return []

# ---------- Odds extraction (Anytime market) ----------
ANYTIME_KEYS = ["anytime", "to score", "player to score", "goalscorer", "score at anytime"]
EXCLUDE_KEYS = ["first", "last", "2 or more", "two or more", "hat-trick", "hat trick"]

def looks_like_anytime_market(name: str) -> bool:
    s = (name or "").lower()
    if not s:
        return False
    if any(k in s for k in EXCLUDE_KEYS):
        return False
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
            if isinstance(v, (int, float)):
                return float(v)
            if isinstance(v, str):
                return float(v)
        except Exception:
            pass
    return None

def extract_anytime_price(markets: List[dict], player_name: str) -> Optional[Tuple[str, float]]:
    for m in markets or []:
        mname = m.get("name") or ""
        if not looks_like_anytime_market(mname):
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
    if BOOKMAKER_IDS:
        fltrs.append(f"bookmakers:{BOOKMAKER_IDS}")
    if MARKET_IDS:
        fltrs.append(f"markets:{MARKET_IDS}")
    if fltrs:
        params["filters"] = ";".join(fltrs)

    j = api_get(f"fixtures/{fixture_id}", params=params)
    fx = j.get("data") or {}
    markets = fx.get("odds") or []

    # ensure bookmaker name present
    ms = []
    for m in markets:
        m = dict(m)
        if not m.get("bookmaker_name"):
            m["bookmaker_name"] = (m.get("bookmaker") or {}).get("name") or m.get("bookmaker_name") or "Book"
        ms.append(m)

    res = extract_anytime_price(ms, player_name)
    if res:
        return res

    if not MARKET_IDS and ODDS_FALLBACK_ALL:
        return extract_anytime_price(ms, player_name)

    return None

# ---------- Main ----------
def league_label(league_id: int, fixtures_blob: dict) -> str:
    nm = fixtures_blob.get("league_name")
    if isinstance(nm, str) and nm.strip():
        return nm.strip()
    return BIG5_LABELS.get(league_id, f"League {league_id}")

def render_markdown(leagues_blocks: List[str]) -> str:
    return "\n".join(["## Boot & Book — Top Scorers × Anytime Odds", ""] + leagues_blocks) + "\n"

def render_text(leagues_blocks: List[str]) -> str:
    # Convert the markdown-style tables into a clean text post
    lines = ["Boot & Book — Top Scorers × Anytime Odds", ""]
    for block in leagues_blocks:
        # keep headings, transform table lines
        for ln in block.splitlines():
            if ln.startswith("|---"):
                continue
            if ln.startswith("|"):
                cols = [c.strip() for c in ln.strip("|").split("|")]
                if len(cols) >= 7:
                    lines.append(f"{cols[0]}. {cols[1]} — {cols[2]} — Goals: {cols[3]} — Anytime: {cols[4]} — {cols[5]} — KO {cols[6]}")
            else:
                lines.append(ln)
        lines.append("")
    return "\n".join(lines) + "\n"

def main():
    today = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d")
    leagues_blocks_md: List[str] = []

    for league_id in LEAGUE_IDS:
        blob = load_fixtures_blob(league_id)
        label = league_label(league_id, blob) if blob else BIG5_LABELS.get(league_id, f"League {league_id}")

        if not blob or not (blob.get("fixtures")):
            block = "\n".join([
                f"### {label} (LID {league_id})", "",
                "_No fixtures file found or empty_; skipped.", ""
            ])
            leagues_blocks_md.append(block)
            continue

        season_id = infer_season_id_from_fixtures(blob)
        if not season_id:
            block = "\n".join([
                f"### {label} (LID {league_id})", "",
                "_Could not infer season_id from fixtures_; skipped.", ""
            ])
            leagues_blocks_md.append(block)
            continue

        team_fx = team_next_fixture_map(blob)
        top_rows = fetch_topscorers_goal_topN(season_id, blob, TOP_N)

        lines = [f"### {label} (LID {league_id})", "", "| # | Player | Team | Goals | Anytime | Fixture | KO (UTC) |", "|---:|:------|:-----|------:|:-------:|:-------|:---------|"]
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
        leagues_blocks_md.append("\n".join(lines))

        # Helpful log
        print(f"[INFO] {label}: top_rows={len(top_rows)}")

    # Write Markdown
    md = render_markdown(leagues_blocks_md)
    out_md = OUT_MD_DIR / f"top_scorers_anytime_{today}.md"
    out_md.write_text(md, encoding="utf-8")
    print(f"[OK] wrote {out_md}")

    # Write plain TXT for your /data/ folder
    txt = render_text(leagues_blocks_md)
    out_txt = OUT_TXT_DIR / f"top_scorers_anytime_{today}.txt"
    out_txt.write_text(txt, encoding="utf-8")
    print(f"[OK] wrote {out_txt}")

if __name__ == "__main__":
    main()