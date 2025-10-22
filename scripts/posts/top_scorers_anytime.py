#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Post 1 — Top scorers + Anytime odds (Big 5)

What it does
------------
1) For each Big 5 league, fetch currentSeason and the league topscorers (type_id=208).
2) Merge across leagues, rank by goals (desc), take the global Top N (default 10).
3) For each player, find the next saved fixture from data/fixtures/by_league/<lid>.json.
4) From data/odds/b365/by_league/<lid>.json, read Bet365 "Goalscorers" (market_id=90),
   label "Anytime" to grab the decimal price for that player in that fixture.
5) Write:
   - reports/social/top_scorers_anytime_YYYYMMDD.md
   - data/social_media_posts/top_scorers_anytime_YYYYMMDD.txt

ENV
---
SPORTMONKS_TOKEN     (required)
LEAGUE_IDS           comma list (default: 8,564,384,82,301  -> EPL, LaLiga, Serie A, Bundesliga, Ligue 1)
TOP_N                integer (default: 10)
"""

import os, sys, time, json, math, datetime as dt, re, unicodedata
from pathlib import Path
from typing import Dict, List, Optional, Any, Tuple
import requests

# ---------- Config ----------
API_BASE = "https://api.sportmonks.com/v3/football"
API_TOKEN = os.getenv("SPORTMONKS_TOKEN") or os.getenv("SPORTMONKS_API_TOKEN")
TIMEOUT = 20
RETRIES = 3
BACKOFF = 1.6

# Big 5 default
DEFAULT_LEAGUES = [8, 564, 384, 82, 301]  # EPL, LaLiga, Serie A, Bundesliga, Ligue 1
LEAGUE_IDS = [int(x) for x in (os.getenv("LEAGUE_IDS") or ",".join(map(str, DEFAULT_LEAGUES))).split(",") if x.strip()]

TOP_N = int(os.getenv("TOP_N", "10"))

# Local data roots
ROOT = Path(".")
FIX_DIR = ROOT / "data" / "fixtures" / "by_league"
ODDS_DIR = ROOT / "data" / "odds" / "b365" / "by_league"
REPORTS_DIR = ROOT / "reports" / "social"
POSTS_DIR = ROOT / "data" / "social_media_posts"
REPORTS_DIR.mkdir(parents=True, exist_ok=True)
POSTS_DIR.mkdir(parents=True, exist_ok=True)

# Market constants – as seen in your EPL odds dump
MARKET_GOALSCORERS = 90       # "Goalscorers" market
ANYTIME_LABEL = "Anytime"     # we only want anytime scorer options

# ---------- small helpers ----------
def strip_accents(s: str) -> str:
    return ''.join(c for c in unicodedata.normalize('NFD', s or '') if unicodedata.category(c) != 'Mn')

def norm(s: str) -> str:
    s = strip_accents(s or "").lower()
    s = re.sub(r"[^a-z0-9\s\.-]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s

GENERIC_TEAM_TOKENS = {"fc","cf","afc","sc","cd","ud","ac","as","ss","ssc","us","uc","rc","rcd","ca","the","club","de","del","la","las","los","calcio","united","city","saint","st"}
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

def extract_last_name_initial(name: str) -> Tuple[Optional[str], Optional[str]]:
    if not name: return None, None
    name2 = strip_accents(name).replace(".", " ").strip()
    parts = [p for p in name2.split() if p]
    if not parts: return None, None
    last = norm(parts[-1]); initial = None
    for p in parts[:-1]:
        ch = p.strip()[0:1]
        if ch: initial = ch.lower(); break
    return last, initial

def player_label_matches(player: str, option_name_or_label: str) -> bool:
    """
    Match "J. Bowen" / "Jarrod Bowen" / "Bowen" to Bet365 player strings.
    """
    if not player or not option_name_or_label: return False
    last, initial = extract_last_name_initial(player)
    label = norm(option_name_or_label)
    if not last or last not in label: return False
    if initial:
        first_word_initial = label.split()[0][0:1] if label.split() else None
        if first_word_initial and first_word_initial == initial: return True
        return bool(re.search(rf"\b{initial}\w*\b.*\b{last}\b", label))
    return True

def as_float(x) -> Optional[float]:
    try: return float(str(x))
    except Exception: return None

def today_yyyymmdd() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d")

def dmy(dts: Optional[str]) -> str:
    if not dts: return ""
    # accepts "YYYY-MM-DD HH:MM:SS" or ISO
    s = dts.replace("T", " ").replace("Z", "")
    return s

# ---------- HTTP (Sportmonks) ----------
_MEMO: Dict[str, Any] = {}

def _key(url: str, params: dict) -> str:
    return url + "?" + "&".join(sorted(f"{k}={v}" for k,v in params.items()))

def api_get(path: str, params: Optional[dict] = None) -> dict:
    if not API_TOKEN:
        raise RuntimeError("SPORTMONKS_TOKEN not set.")
    if params is None: params = {}
    params = {**params, "api_token": API_TOKEN}
    url = f"{API_BASE}/{path.lstrip('/')}"
    k = _key(url, params)
    if k in _MEMO: return _MEMO[k]
    last = None
    for i in range(1, RETRIES+1):
        try:
            r = requests.get(url, params=params, timeout=TIMEOUT)
            if r.status_code == 429:
                time.sleep(min(60, (BACKOFF ** i) * 2.0))
                continue
            r.raise_for_status()
            j = r.json()
            _MEMO[k] = j
            return j
        except Exception as e:
            last = e
            if i < RETRIES:
                time.sleep(BACKOFF ** i)
            else:
                raise
    raise last  # pragma: no cover

# ---------- Sportmonks => seasons / topscorers ----------
def current_season_id(league_id: int) -> Optional[int]:
    try:
        j = api_get(f"leagues/{league_id}", params={"include": "currentSeason"})
        data = j.get("data") or {}
        cur = data.get("currentSeason") or {}
        sid = cur.get("id") or cur.get("season_id")
        return int(sid) if sid else None
    except Exception as e:
        print(f"[WARN] league {league_id}: could not read currentSeason ({e})")
        return None

def fetch_topscorers_for_season(season_id: int) -> List[dict]:
    """
    Use filter=seasonTopscorerTypes:208 and include player;team;type.
    If this returns 404 or empty, just return [].
    """
    try:
        j = api_get(
            f"topscorers/seasons/{season_id}",
            params={"filter": "seasonTopscorerTypes:208", "include": "player;team;type", "per_page": 50}
        )
        rows = j.get("data") or []
    except requests.HTTPError as e:
        # Graceful: some seasons don’t expose season-level topscorers
        print(f"[WARN] season {season_id}: topscorers error {e}")
        return []
    except Exception as e:
        print(f"[WARN] season {season_id}: topscorers error {e}")
        return []

    out = []
    for r in rows:
        # common shapes: r['player'], r['team'], r['value'] or r['goals']
        player = None; team = None
        if isinstance(r.get("player"), dict):
            player = r["player"].get("name") or r["player"].get("display_name")
        if isinstance(r.get("team"), dict):
            team = r["team"].get("name") or r["team"].get("short_code")
        goals = None
        for key in ("goals", "value", "count", "statistics", "total"):
            v = r.get(key)
            if isinstance(v, (int, float)) and v >= 0:
                goals = int(v); break
            if isinstance(v, dict) and "goals" in v:
                try:
                    goals = int(v["goals"]); break
                except Exception:
                    pass
        if player and team and goals is not None:
            out.append({"player": player, "team": team, "goals": goals})
    return out

# ---------- Local fixtures ----------
def load_fixtures_for_league(league_id: int) -> List[dict]:
    p = FIX_DIR / f"{league_id}.json"
    if not p.is_file(): return []
    try:
        blob = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return []
    fixtures = blob.get("fixtures") or []
    # keep only entries with participants list
    return [fx for fx in fixtures if fx and fx.get("participants")]

def find_next_fixture_for_team(fixtures: List[dict], team_name: str) -> Optional[dict]:
    # choose earliest starting_at >= now among the saved fixtures where team appears
    now_ts = int(dt.datetime.now(dt.timezone.utc).timestamp())
    best = None
    for fx in fixtures:
        parts = fx.get("participants") or []
        teams = [p.get("name") for p in parts]
        if any(team_names_match(team_name, t) for t in teams if t):
            ts = fx.get("starting_at_timestamp") or 0
            # if timestamps missing, still consider but with ts=0
            if ts >= now_ts and (best is None or ts < (best.get("starting_at_timestamp") or 10**12)):
                best = fx
            if best is None:
                best = fx  # fallback: take first if all are in the past
    return best

def fixture_home_away_names(fx: dict) -> Tuple[str, str]:
    home = next((p for p in (fx.get("participants") or []) if (p.get("meta") or {}).get("location") == "home"), {})
    away = next((p for p in (fx.get("participants") or []) if (p.get("meta") or {}).get("location") == "away"), {})
    return home.get("name",""), away.get("name","")

# ---------- Local odds (Bet365) ----------
def load_odds_blob(league_id: int) -> dict:
    """
    Supports two shapes:
      A) { "fixtures":[ { "id":..., "name":..., "odds":[ rows... ] }, ... ] }
      B) flat list of odds rows grouped by 'fixture_id'
    We normalize into: idx[fixture_id] -> list[rows]
    """
    p = ODDS_DIR / f"{league_id}.json"
    if not p.is_file(): return {}
    try:
        blob = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}

    idx: Dict[int, List[dict]] = {}
    if isinstance(blob, dict) and isinstance(blob.get("fixtures"), list):
        for fx in blob["fixtures"]:
            fid = fx.get("id") or fx.get("fixture_id")
            if not isinstance(fid, int): continue
            rows = fx.get("odds") or []
            if isinstance(rows, list):
                idx.setdefault(fid, []).extend(rows)
    elif isinstance(blob, list):
        for row in blob:
            fid = row.get("fixture_id")
            if isinstance(fid, int):
                idx.setdefault(fid, []).append(row)
    else:
        # try another common dump shape: {"odds":[...]}
        rows = blob.get("odds") or []
        if isinstance(rows, list):
            for row in rows:
                fid = row.get("fixture_id")
                if isinstance(fid, int):
                    idx.setdefault(fid, []).append(row)

    return {"by_fixture": idx}

def best_anytime_price(odds_rows: List[dict], player_name: str) -> Optional[float]:
    """
    Scan rows for market_id=90 "Goalscorers" and label "Anytime", pick the best (highest) decimal.
    (Based on your odds file that shows Goalscorers market 90 and Anytime label).  # see citations in chat
    """
    best = None
    for r in odds_rows:
        mid = int(r.get("market_id", 0))
        if mid != MARKET_GOALSCORERS:
            continue
        if (r.get("label") or "").strip().lower() != ANYTIME_LABEL.lower():
            continue
        name = r.get("name") or r.get("original_label") or ""
        if not player_label_matches(player_name, name):
            continue
        price = as_float(r.get("value") or r.get("dp3"))
        if price is None:
            continue
        if best is None or price > best + 1e-12:
            best = price
    return best

# ---------- Main ----------
def main():
    # 0) Guard
    if not API_TOKEN:
        print("ERROR: SPORTMONKS_TOKEN not set.", file=sys.stderr)
        sys.exit(1)

    # 1) Build per-league topscorers
    league_payloads: Dict[int, dict] = {}
    all_rows: List[dict] = []

    for lid in LEAGUE_IDS:
        season_id = current_season_id(lid)
        if not season_id:
            print(f"[WARN] League {lid}: no currentSeason — skipping")
            continue

        top_rows = fetch_topscorers_for_season(season_id)
        if not top_rows:
            print(f"[INFO] League {lid}: 0 topscorer rows")
            continue

        # stash with league id
        for r in top_rows:
            all_rows.append({**r, "league_id": lid})

        league_payloads[lid] = {
            "league_id": lid,
            "season_id": season_id,
            "count": len(top_rows),
        }

    if not all_rows:
        print("[RESULT] No topscorer rows found across selected leagues.")
        # still write empty files for traceability
        stamp = today_yyyymmdd()
        md_out = REPORTS_DIR / f"top_scorers_anytime_{stamp}.md"
        txt_out = POSTS_DIR / f"top_scorers_anytime_{stamp}.txt"
        md_out.write_text("# Top Scorers + Anytime odds\n\n_No data available_\n", encoding="utf-8")
        txt_out.write_text("No data available\n", encoding="utf-8")
        return

    # 2) Merge & rank global Top-N
    # normalize goals to int
    for r in all_rows:
        try: r["goals"] = int(r.get("goals") or 0)
        except Exception: r["goals"] = 0

    all_rows.sort(key=lambda r: (-r["goals"], norm(r["player"]), norm(r["team"])))
    top = all_rows[:TOP_N]

    # 3) For each player, find next fixture + anytime price
    results = []
    for r in top:
        lid = r["league_id"]
        fixtures = load_fixtures_for_league(lid)
        fx = find_next_fixture_for_team(fixtures, r["team"])
        fx_text = ""
        anytime = None
        if fx:
            fid = fx.get("id") or fx.get("fixture_id")
            h, a = fixture_home_away_names(fx)
            fx_text = f"{h} vs {a} @ {dmy(fx.get('starting_at'))}"
            # odds lookup
            odds_blob = load_odds_blob(lid)
            idx = (odds_blob.get("by_fixture") or {})
            rows = idx.get(int(fid), [])
            anytime = best_anytime_price(rows, r["player"])
        else:
            fx_text = "(fixture not found in saved files)"

        results.append({
            "league_id": lid,
            "player": r["player"],
            "team": r["team"],
            "goals": r["goals"],
            "fixture_text": fx_text,
            "anytime": anytime
        })

    # 4) Render outputs
    stamp = today_yyyymmdd()
    md_lines = []
    md_lines.append(f"# Top Scorers (Big 5) + Anytime odds — {stamp}")
    md_lines.append("")
    md_lines.append("_Data: Sportmonks v3 topscorers (type_id=208). Odds: Bet365 Goalscorers→Anytime from local dumps._")
    md_lines.append("")
    pos = 1
    for x in results:
        price = f"{x['anytime']:.2f}" if isinstance(x["anytime"], (int,float)) else "N/A"
        md_lines.append(f"**{pos}. {x['player']} — {x['team']}**  \nGoals: **{x['goals']}**  |  Fixture: {x['fixture_text']}  |  Anytime: **{price}**")
        pos += 1

    text_lines = []
    text_lines.append(f"Top Scorers + Anytime (Big 5) — {stamp}")
    for i, x in enumerate(results, 1):
        price = f"{x['anytime']:.2f}" if isinstance(x["anytime"], (int,float)) else "N/A"
        text_lines.append(f"{i}. {x['player']} ({x['team']}) — {x['goals']} goals — {x['fixture_text']} — Anytime {price}")

    md_out = REPORTS_DIR / f"top_scorers_anytime_{stamp}.md"
    txt_out = POSTS_DIR / f"top_scorers_anytime_{stamp}.txt"
    md_out.write_text("\n".join(md_lines).rstrip() + "\n", encoding="utf-8")
    txt_out.write_text("\n".join(text_lines).rstrip() + "\n", encoding="utf-8")

    print(f"[OK] wrote {md_out}")
    print(f"[OK] wrote {txt_out}")

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
