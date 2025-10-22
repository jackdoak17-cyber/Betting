#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Post 1 — Top scorers + Anytime odds (Big 5)

Flow
----
1) Determine season_id per league from your saved fixtures (or LEAGUE_SEASON_OVERRIDES).
2) Try:    GET /v3/football/topscorers/seasons/{season_id}?filter=seasonTopscorerTypes:208&include=player;team;type
   Fallback:
           GET /v3/football/seasons/{season_id}?include=stages
           -> pick likely "Regular/League/Main" stage(s)
           -> GET /v3/football/topscorers/stages/{stage_id}?filter=stageTopscorerTypes:208&include=player;team;type
3) Merge all leagues, rank by goals desc, take Top-N (default 10).
4) Join each player to their next fixture from data/fixtures/by_league/<lid>.json.
5) Read Bet365 odds from data/odds/b365/by_league/<lid>.json:
   - market_id=90 ("Goalscorers")
   - label "Anytime"
6) Outputs:
   - reports/social/top_scorers_anytime_YYYYMMDD.md
   - data/social_media_posts/top_scorers_anytime_YYYYMMDD.txt
"""

import os, sys, json, time, re, unicodedata, datetime as dt
from pathlib import Path
from typing import Dict, List, Optional, Any, Tuple

import requests

API_BASE = "https://api.sportmonks.com/v3/football"
API_TOKEN = os.getenv("SPORTMONKS_TOKEN") or os.getenv("SPORTMONKS_API_TOKEN")
TIMEOUT = 20
RETRIES = 3
BACKOFF = 1.6

# Big 5 default (EPL, LaLiga, Serie A, Bundesliga, Ligue 1)
DEFAULT_LEAGUES = [8, 564, 384, 82, 301]
LEAGUE_IDS = [int(x) for x in (os.getenv("LEAGUE_IDS") or ",".join(map(str, DEFAULT_LEAGUES))).split(",") if x.strip()]
TOP_N = int(os.getenv("TOP_N", "10"))

# Optional manual override: "8:25583,564:25659,384:25533,82:25646,301:25651"
OVERRIDES_RAW = os.getenv("LEAGUE_SEASON_OVERRIDES", "").strip()
SEASON_OVERRIDES: Dict[int, int] = {}
if OVERRIDES_RAW:
    for pair in OVERRIDES_RAW.split(","):
        pair = pair.strip()
        if ":" in pair:
            lid, sid = pair.split(":", 1)
            try:
                SEASON_OVERRIDES[int(lid)] = int(sid)
            except Exception:
                pass

# Local data
ROOT = Path(".")
FIX_DIR = ROOT / "data" / "fixtures" / "by_league"
ODDS_DIR = ROOT / "data" / "odds" / "b365" / "by_league"
REPORTS_DIR = ROOT / "reports" / "social"
POSTS_DIR = ROOT / "data" / "social_media_posts"
REPORTS_DIR.mkdir(parents=True, exist_ok=True)
POSTS_DIR.mkdir(parents=True, exist_ok=True)

# Odds parsing constants (match your local odds dump)
MARKET_GOALSCORERS = int(os.getenv("B365_GOALSCORERS_MARKET_ID", "90"))
ANYTIME_LABEL = os.getenv("B365_ANYTIME_LABEL", "Anytime")

# ---------------- string helpers ----------------
def strip_accents(s: str) -> str:
    return ''.join(c for c in unicodedata.normalize('NFD', s or '') if unicodedata.category(c) != 'Mn')

def norm(s: str) -> str:
    s = strip_accents(s or "").lower()
    s = re.sub(r"[^a-z0-9\s\.-]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s

GENERIC_TEAM_TOKENS = {"fc","cf","afc","sc","cd","ud","ac","as","ss","ssc","us","uc","rc","rcd","ca","the",
                       "club","de","del","la","las","los","calcio","united","city","saint","st"}
def team_tokens(name: str):
    toks = set(norm(name).split())
    return {t for t in toks if t not in GENERIC_TEAM_TOKENS}

def team_names_match(a: str, b: str) -> bool:
    if not a or not b: return False
    ta, tb = team_tokens(a), team_tokens(b)
    if not ta or not tb: return False
    if ta == tb or ta.issubset(tb) or tb.issubset(ta): return True
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

def player_label_matches(player: str, option: str) -> bool:
    # Match "J. Bowen" / "Jarrod Bowen" / "Bowen" etc.
    if not player or not option: return False
    last, initial = extract_last_name_initial(player)
    label = norm(option)
    if not last or last not in label: return False
    if initial:
        fw = label.split()[0][0:1] if label.split() else None
        if fw and fw == initial: return True
        return bool(re.search(rf"\b{initial}\w*\b.*\b{last}\b", label))
    return True

def as_float(x) -> Optional[float]:
    try: return float(str(x))
    except Exception: return None

def now_yyyymmdd() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d")

# ---------------- HTTP ----------------
def api_get(path: str, params: Optional[dict] = None) -> dict:
    if not API_TOKEN:
        raise RuntimeError("SPORTMONKS_TOKEN not set.")
    params = {**(params or {}), "api_token": API_TOKEN}
    url = f"{API_BASE}/{path.lstrip('/')}"
    last = None
    for i in range(1, RETRIES+1):
        try:
            r = requests.get(url, params=params, timeout=TIMEOUT)
            if r.status_code == 429:
                time.sleep(min(60, (BACKOFF ** i) * 2.0))
                continue
            r.raise_for_status()
            return r.json()
        except Exception as e:
            last = e
            if i < RETRIES:
                time.sleep(BACKOFF ** i)
            else:
                raise
    raise last

# ---------------- fixtures ----------------
def load_league_fixtures(league_id: int) -> List[dict]:
    p = FIX_DIR / f"{league_id}.json"
    if not p.is_file(): return []
    try:
        blob = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return []
    return blob.get("fixtures") or []

def infer_season_from_fixtures(league_id: int) -> Optional[int]:
    fx = load_league_fixtures(league_id)
    if not fx: return None
    counts: Dict[int,int] = {}
    for row in fx:
        sid = row.get("season_id")
        if isinstance(sid, int) and sid > 0:
            counts[sid] = counts.get(sid, 0) + 1
    if not counts: return None
    best_sid = max(counts.items(), key=lambda kv: kv[1])[0]
    return best_sid

def next_fixture_for_team(fixtures: List[dict], team_name: str) -> Optional[dict]:
    # earliest upcoming by starting_at_timestamp; if all past, first occurrence
    now_ts = int(dt.datetime.now(dt.timezone.utc).timestamp())
    best = None
    for fx in fixtures:
        parts = fx.get("participants") or []
        teams = [p.get("name") for p in parts if p.get("name")]
        if any(team_names_match(team_name, t) for t in teams):
            ts = int(fx.get("starting_at_timestamp") or 0)
            if ts >= now_ts and (best is None or ts < int(best.get("starting_at_timestamp") or 1e12)):
                best = fx
            if best is None:
                best = fx
    return best

def home_away_names(fx: dict) -> Tuple[str,str]:
    home = next((p for p in (fx.get("participants") or []) if (p.get("meta") or {}).get("location")=="home"), {})
    away = next((p for p in (fx.get("participants") or []) if (p.get("meta") or {}).get("location")=="away"), {})
    return home.get("name",""), away.get("name","")

# ---------------- odds (local Bet365) ----------------
def load_odds_by_fixture(league_id: int) -> Dict[int, List[dict]]:
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
        rows = blob.get("odds") or []
        for row in rows:
            fid = row.get("fixture_id")
            if isinstance(fid, int):
                idx.setdefault(fid, []).append(row)
    return idx

def best_anytime_price(odds_rows: List[dict], player_name: str) -> Optional[float]:
    best = None
    for r in odds_rows or []:
        if int(r.get("market_id", 0)) != MARKET_GOALSCORERS:
            continue
        if (r.get("label") or "").strip().lower() != "anytime":
            continue
        name = r.get("name") or r.get("original_label") or ""
        # Match "O. Watkins" / "Ollie Watkins" / "Watkins"
        last, initial = extract_last_name_initial(player_name)
        label = norm(name)
        if not last or last not in label:
            continue
        if initial:
            fw = label.split()[0][0:1] if label.split() else None
            if not (fw and fw == initial) and not re.search(rf"\b{initial}\w*\b.*\b{last}\b", label):
                continue
        price = as_float(r.get("value") or r.get("dp3"))
        if price is None:
            continue
        if best is None or price > best + 1e-12:
            best = price
    return best

# ---------------- topscorers (season -> stage fallback) ----------------
def parse_top_rows(payload: dict) -> List[dict]:
    """
    Normalize to [{player, team, goals}]
    """
    data = payload.get("data") or []
    out = []
    for r in data:
        pl = r.get("player") or {}
        tm = r.get("team") or r.get("participant") or {}
        player = pl.get("name") or pl.get("display_name")
        team = tm.get("name") or tm.get("short_code")
        goals = None
        for key in ("goals", "total", "value", "count"):
            v = r.get(key)
            if isinstance(v, (int, float)):
                goals = int(v); break
        if player and team and goals is not None:
            out.append({"player": player, "team": team, "goals": goals})
    out.sort(key=lambda x: (-x["goals"], norm(x["player"])))
    return out

def season_topscorers(season_id: int) -> List[dict]:
    try:
        j = api_get(
            f"topscorers/seasons/{season_id}",
            params={"filter": "seasonTopscorerTypes:208", "include": "player;team;type", "per_page": 50}
        )
        return parse_top_rows(j)
    except requests.HTTPError as e:
        if getattr(e, "response", None) is not None and e.response.status_code == 404:
            print(f"[INFO] season {season_id}: topscorers 404 — will try stages")
            return []
        print(f"[WARN] season {season_id}: topscorers error {e}")
        return []
    except Exception as e:
        print(f"[WARN] season {season_id}: topscorers error {e}")
        return []

def candidate_stage_ids(season_id: int) -> List[int]:
    """
    Discover stages, score likely 'Regular/League/Main' stages first.
    """
    try:
        j = api_get(f"seasons/{season_id}", params={"include": "stages", "per_page": 100})
    except Exception as e:
        print(f"[WARN] seasons/{season_id}?include=stages error {e}")
        return []
    data = j.get("data") or {}
    stages = data.get("stages") or []
    if not stages:
        return []
    def score(s: dict) -> int:
        nm = (s.get("name") or "").lower()
        tp = (s.get("type") or "").lower()
        sc = 0
        if "regular" in nm or "regular" in tp: sc += 4
        if "league" in nm or "league" in tp:   sc += 3
        if "main" in nm:                       sc += 2
        if s.get("has_standings"):             sc += 1
        return sc
    stages.sort(key=lambda s: (-score(s), s.get("id") or 0))
    return [int(s.get("id")) for s in stages if isinstance(s.get("id"), int)]

def stage_topscorers(stage_id: int) -> List[dict]:
    try:
        j = api_get(
            f"topscorers/stages/{stage_id}",
            params={"filter": "stageTopscorerTypes:208", "include": "player;team;type", "per_page": 50}
        )
        return parse_top_rows(j)
    except requests.HTTPError as e:
        if getattr(e, "response", None) is not None and e.response.status_code == 404:
            print(f"[INFO] stage {stage_id}: 404")
            return []
        print(f"[WARN] stage {stage_id}: error {e}")
        return []
    except Exception as e:
        print(f"[WARN] stage {stage_id}: error {e}")
        return []

def top_scorers_for_season_with_fallback(season_id: int) -> List[dict]:
    rows = season_topscorers(season_id)
    if rows:
        return rows
    # try candidate stages
    for sid in candidate_stage_ids(season_id)[:5]:  # try a few best candidates
        rows = stage_topscorers(sid)
        if rows:
            print(f"[INFO] season {season_id}: using stage {sid}")
            return rows
    return []

# ---------------- main ----------------
def main():
    if not API_TOKEN:
        print("ERROR: SPORTMONKS_TOKEN not set.", file=sys.stderr)
        sys.exit(1)

    all_rows: List[dict] = []

    # 1) per-league topscorers
    for lid in LEAGUE_IDS:
        season_id = SEASON_OVERRIDES.get(lid) or infer_season_from_fixtures(lid)
        if not season_id:
            print(f"[WARN] League {lid}: could not resolve season_id from fixtures/overrides — skipping")
            continue
        rows = top_scorers_for_season_with_fallback(season_id)
        print(f"[INFO] League {lid}: season {season_id} -> rows {len(rows)}")
        for r in rows:
            r["league_id"] = lid
        all_rows.extend(rows)

    if not all_rows:
        stamp = now_yyyymmdd()
        md_out = REPORTS_DIR / f"top_scorers_anytime_{stamp}.md"
        txt_out = POSTS_DIR / f"top_scorers_anytime_{stamp}.txt"
        md_out.write_text("# Top Scorers + Anytime odds\n\n_No data available_\n", encoding="utf-8")
        txt_out.write_text("No data available\n", encoding="utf-8")
        print("[RESULT] No topscorer rows found across selected leagues.")
        print(f"[OK] wrote {md_out}")
        print(f"[OK] wrote {txt_out}")
        return

    # 2) merge + rank Top N
    for r in all_rows:
        try: r["goals"] = int(r.get("goals") or 0)
        except Exception: r["goals"] = 0
    all_rows.sort(key=lambda r: (-r["goals"], norm(r["player"]), norm(r["team"])))
    top = all_rows[:TOP_N]

    # 3) join next fixture + odds
    def load_odds_idx(lid: int) -> Dict[int, List[dict]]:
        try: return load_odds_by_fixture(lid)
        except Exception: return {}

    odds_cache: Dict[int, Dict[int, List[dict]]] = {}
    results = []
    for r in top:
        lid = r["league_id"]
        fixtures = load_league_fixtures(lid)
        fx = next_fixture_for_team(fixtures, r["team"])
        fixture_text = "—"
        price = None
        if fx:
            fid = fx.get("id") or fx.get("fixture_id")
            h, a = home_away_names(fx)
            kickoff = fx.get("starting_at") or ""
            fixture_text = f"{h} vs {a} @ {kickoff}".strip()
            if lid not in odds_cache:
                odds_cache[lid] = load_odds_idx(lid)
            rows = odds_cache[lid].get(int(fid), [])
            price = best_anytime_price(rows, r["player"])
        results.append({
            "player": r["player"], "team": r["team"], "goals": r["goals"],
            "league_id": lid, "fixture_text": fixture_text,
            "anytime": price
        })

    # 4) write outputs
    stamp = now_yyyymmdd()
    md_lines = [f"# Top Scorers (Big 5) + Anytime odds — {stamp}", ""]
    md_lines.append("_Data: Sportmonks v3 Topscorers (type_id=208). Odds: Bet365 Goalscorers→Anytime (local)._")
    md_lines.append("")
    for i, x in enumerate(results, 1):
        price_txt = f"{x['anytime']:.2f}" if isinstance(x["anytime"], (int,float)) else "N/A"
        md_lines.append(f"**{i}. {x['player']} — {x['team']}**  \nGoals: **{x['goals']}**  |  Fixture: {x['fixture_text']}  |  Anytime: **{price_txt}**")

    txt_lines = [f"Top Scorers + Anytime (Big 5) — {stamp}"]
    for i, x in enumerate(results, 1):
        price_txt = f"{x['anytime']:.2f}" if isinstance(x["anytime"], (int,float)) else "N/A"
        txt_lines.append(f"{i}. {x['player']} ({x['team']}) — {x['goals']} goals — {x['fixture_text']} — Anytime {price_txt}")

    md_out = REPORTS_DIR / f"top_scorers_anytime_{stamp}.md"
    txt_out = POSTS_DIR / f"top_scorers_anytime_{stamp}.txt"
    md_out.write_text("\n".join(md_lines).rstrip() + "\n", encoding="utf-8")
    txt_out.write_text("\n".join(txt_lines).rstrip() + "\n", encoding="utf-8")

    print(f"[OK] wrote {md_out}")
    print(f"[OK] wrote {txt_out}")

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
