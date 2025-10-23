#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os, sys, json, math, re, time, datetime as dt
from pathlib import Path
from typing import List, Dict, Optional, Tuple
import requests

# ---------- config ----------
ROOT = Path(".")
# fixtures can be in either of these
FIX_DIRS = [
    ROOT / "data" / "fixtures" / "by_league",
    ROOT / "data" / "fixtures",
]
# odds primary location
ODDS_FIX_DIRS = [
    ROOT / "data" / "odds" / "b365" / "fixtures",
    ROOT / "data" / "odds" / "b365" / "by_fixture",  # optional alt
    ROOT / "data" / "odds" / "b365",                 # optional alt
]
DEBUG_DIR = ROOT / "data" / "debug"
DEBUG_DIR.mkdir(parents=True, exist_ok=True)

SPORTMONKS_TOKEN = os.getenv("SPORTMONKS_TOKEN", "").strip()
if not SPORTMONKS_TOKEN:
    print("ERROR: SPORTMONKS_TOKEN env not set", file=sys.stderr)
    sys.exit(1)

LEAGUE_IDS = [int(x) for x in os.getenv("LEAGUE_IDS", "8,564,82,384,301").split(",") if x.strip()]
LIMIT_PER_LEAGUE = int(os.getenv("LIMIT_PER_LEAGUE", "10"))
WINDOW_DAYS = int(os.getenv("WINDOW_DAYS", "14"))
OUTPUT_PATH = Path(os.getenv("OUTPUT_PATH", "betting/posts/top_scorers_anytime.md"))

API = "https://api.sportmonks.com/v3/football"

# ---------- utils ----------
def now_utc() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)

def parse_dt_utc(s: str) -> Optional[dt.datetime]:
    if not s:
        return None
    try:
        if "T" in s:
            return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
        return dt.datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=dt.timezone.utc)
    except Exception:
        return None

def upcoming_within_window(starting_at: str, days: int) -> bool:
    d = parse_dt_utc(starting_at)
    if not d:
        return False
    now = now_utc()
    return now <= d <= (now + dt.timedelta(days=days))

def strip_accents(s: str) -> str:
    import unicodedata
    return "".join(c for c in unicodedata.normalize("NFD", s or "") if unicodedata.category(c) != "Mn")

def norm(s: str) -> str:
    s = strip_accents(s or "").lower()
    s = re.sub(r"[^a-z0-9\s\.-]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s

GENERIC_TEAM_TOKENS = {
    "fc","cf","afc","sc","cd","ud","ac","as","ss","ssc","us","uc","rc","rcd",
    "ca","the","club","de","del","la","las","los","calcio","united","city",
    "saint","st","bk","athletic","foot","football"
}
def team_tokens(name: str):
    toks = set(norm(name).split())
    return {t for t in toks if t and t not in GENERIC_TEAM_TOKENS}
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

def ensure_parent(p: Path):
    p.parent.mkdir(parents=True, exist_ok=True)

# ---------- IO ----------
def read_json(path: Path) -> Optional[dict]:
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None

def load_fixtures_for_league(lid: int) -> List[dict]:
    for base in FIX_DIRS:
        p = base / f"{lid}.json"
        blob = read_json(p) or {}
        fixtures = blob.get("fixtures")
        if isinstance(fixtures, list):
            return fixtures
        # sometimes stored directly as a list
        if isinstance(blob, list):
            return blob
    return []

def find_fixture_odds_file(fid: int) -> Optional[Path]:
    candidates = []
    for base in ODDS_FIX_DIRS:
        candidates.append(base / f"{fid}.json")
        # in some repos people save fixture odds as <league>_<fid>.json; try that too
        candidates.extend(base.glob(f"*{fid}*.json"))
    for c in candidates:
        if c.exists():
            return c
    return None

# ---------- Odds parsing ----------
MATCH_WINNER_KEYS = [
    "match winner","full time result","win/draw/win","wdw","1x2","match odds","result","3 way","90 minutes","regular time result"
]
def is_match_winner(desc: str) -> bool:
    s = norm(desc)
    return bool(s) and any(k in s for k in MATCH_WINNER_KEYS)

ANYTIME_KEYS = [
    "anytime goalscorer","to score anytime","player to score","score at any time","to score (anytime)"
]
BAD_VARIANTS = ["first goalscorer","last goalscorer","2 or more","two or more","hat-trick","hattrick"]
def is_anytime_goals_market(desc: str) -> bool:
    s = norm(desc)
    if not s: return False
    if any(b in s for b in BAD_VARIANTS):  # exclude wrong variants
        return False
    # accept looser phrasing too
    return ("anytime" in s and "score" in s) or any(k in s for k in ANYTIME_KEYS) or ("to score" in s and "any" in s)

def to_float(v) -> Optional[float]:
    try:
        if v in (None, "", "N/A"): return None
        return float(v)
    except Exception:
        return None

def extract_anytime_price(rows: List[dict], player_name: str) -> Optional[float]:
    want = norm(player_name)
    best = None
    for r in rows or []:
        try:
            if r.get("bookmaker_id") not in (2, "2"):  # bet365
                continue
            if r.get("stopped"):
                continue
            if not is_anytime_goals_market(r.get("market_description","")):
                continue

            # player name can be in "name", "total", "participant", or nested key
            cand = r.get("name") or r.get("total") or r.get("participant") or ""
            if norm(cand) != want:
                # Some feeds store like "E. Haaland" – try startswith/contains fallback if exact fails
                cn = norm(cand)
                if not cn or (want not in cn and cn not in want):
                    continue

            price = to_float(r.get("value"))
            if price is None:
                continue
            if (best is None) or (price > best + 1e-12):
                best = price
        except Exception:
            continue
    return best

def extract_team_ml(rows: List[dict], side: str) -> Optional[float]:
    home_vals, away_vals = [], []
    for r in rows or []:
        if r.get("bookmaker_id") != 2:  # Bet365
            continue
        if not is_match_winner(r.get("market_description","")):
            continue
        lab = (r.get("label") or "").strip().lower()
        val = to_float(r.get("value"))
        if val is None: continue
        if lab in ("1", "home", "1 (home)"):
            home_vals.append(val)
        elif lab in ("2", "away", "2 (away)"):
            away_vals.append(val)
    h = min(home_vals) if home_vals else None
    a = min(away_vals) if away_vals else None
    return h if side == "home" else a

# ---------- API helpers ----------
def _get(url: str, params: Dict[str, str]) -> requests.Response:
    # retry a couple times on 5xx
    for i in range(3):
        r = requests.get(url, params=params, timeout=25)
        if r.status_code >= 500:
            time.sleep(1.2 * (i+1))
            continue
        r.raise_for_status()
        return r
    r.raise_for_status()
    return r  # never actually here

def get_current_season_id(league_id: int) -> int:
    r = _get(f"{API}/leagues/{league_id}", {
        "include": "currentSeason",
        "api_token": SPORTMONKS_TOKEN,
    })
    data = r.json().get("data") or {}
    cs = data.get("currentseason") or data.get("currentSeason") or {}
    sid = int(cs.get("id") or 0)
    if not sid:
        raise RuntimeError(f"no current season for league {league_id}")
    return sid

def parse_scorer_item(it: dict) -> Tuple[str, str, int]:
    """
    Return (player_name, team_name, goals)
    Accepts multiple possible shapes.
    """
    player_name = (
        (it.get("player") or {}).get("name")
        or it.get("player_name")
        or it.get("name")
        or ""
    )
    team_name = (
        (it.get("team") or {}).get("name")
        or it.get("team_name")
        or (it.get("participant") or {}).get("name")
        or ""
    )
    goals = None
    # common fields
    for k in ("goals","total","scored","value","statistics_goals"):
        v = it.get(k)
        if isinstance(v, dict):
            # sometimes {"total": N}
            v = v.get("total") or v.get("count")
        g = to_float(v)
        if g is not None:
            goals = int(g)
            break
    # sometimes the key is nested in statistics
    if goals is None:
        stats = it.get("statistics") or {}
        for k in ("goals", "goals_scored"):
            v = stats.get(k)
            g = to_float(v)
            if g is not None:
                goals = int(g)
                break
    if goals is None:
        # last resort: position-based list that includes "count"
        g = to_float(it.get("count"))
        if g is not None:
            goals = int(g)
    return (player_name, team_name, goals or 0)

def fetch_top_scorers(season_id: int) -> List[dict]:
    """
    Try official + fallback routes. Returns list of normalized items:
      { "player": str, "team": str, "goals": int }
    """
    tried = []
    def attempt(url: str, params: Dict[str, str]) -> Optional[List[dict]]:
        tried.append((url, params.copy()))
        r = requests.get(url, params=params, timeout=25)
        if r.status_code >= 500:
            r.raise_for_status()
        if r.status_code >= 400:
            return None
        payload = r.json()
        raw = payload.get("data") or payload.get("topscorers") or []
        if not isinstance(raw, list):
            # some endpoints return {"data":{"scorers":[...]}}
            if isinstance(raw, dict):
                for k in ("scorers","topscorers","items"):
                    if isinstance(raw.get(k), list):
                        raw = raw.get(k)
                        break
        out = []
        for it in raw:
            p, t, g = parse_scorer_item(it)
            if p and t:
                out.append({"player": p, "team": t, "goals": int(g)})
        return out or None

    # 1) primary (documented) route
    out = attempt(f"{API}/players/topscorers/seasons/{season_id}", {
        "include": "player;team",
        "api_token": SPORTMONKS_TOKEN,
    })
    # 2) fallback alias
    if not out:
        out = attempt(f"{API}/topscorers/seasons/{season_id}", {
            "include": "player;team",
            "api_token": SPORTMONKS_TOKEN,
    })
    # 3) old query form
    if not out:
        out = attempt(f"{API}/topscorers", {
            "seasons": str(season_id),
            "include": "player;team",
            "api_token": SPORTMONKS_TOKEN,
        })
    # write a debug snapshot of the *last* response attempt (even if none)
    try:
        dbg = {
            "season_id": season_id,
            "attempts": [{"url": u, "params": p} for (u,p) in tried],
            "result_count": len(out or []),
            "generated_at": now_utc().isoformat()
        }
        ensure_parent(DEBUG_DIR / "x")
        with (DEBUG_DIR / f"topscorers_raw_{season_id}.json").open("w", encoding="utf-8") as f:
            json.dump(dbg, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

    if not out:
        raise RuntimeError(
            "All top-scorer routes failed: " +
            " ; ".join([f"{u} (tried)" for (u, _) in tried])
        )
    # Sort desc by goals
    out.sort(key=lambda r: (-int(r.get("goals",0)), norm(r.get("player",""))))
    return out

# ---------- core ----------
def next_fixture_for_team(team_name: str, fixtures: List[dict]) -> Tuple[Optional[dict], Optional[str]]:
    """
    Return (fixture, side) for team_name, side in {"home","away"}.
    Only fixtures within WINDOW_DAYS.
    """
    for fx in fixtures:
        if not upcoming_within_window(fx.get("starting_at"), WINDOW_DAYS):
            continue
        parts = fx.get("participants") or []
        if len(parts) < 2:
            continue
        home = (parts[0] or {}).get("name") or ""
        away = (parts[1] or {}).get("name") or ""
        if team_names_match(team_name, home):
            return fx, "home"
        if team_names_match(team_name, away):
            return fx, "away"
    return None, None

def load_fixture_odds_rows(fid: int) -> List[dict]:
    p = find_fixture_odds_file(fid)
    if not p: return []
    blob = read_json(p) or {}
    # try various shapes
    if isinstance(blob, dict):
        rows = blob.get("odds") or blob.get("markets") or blob.get("rows") or []
        if isinstance(rows, list):
            return rows
    if isinstance(blob, list):
        return blob
    return []

def league_label(lid: int) -> str:
    return {
        8: "Premier League",
        564: "LaLiga",
        82: "Bundesliga",
        384: "Serie A",
        301: "Ligue 1",
    }.get(lid, f"League {lid}")

def main():
    sections: List[str] = []
    header = f"Top Scorers (Anytime) — Updated {now_utc().strftime('%Y-%m-%d %H:%M:%S')} UTC\n"
    sections.append(header)

    for lid in LEAGUE_IDS:
        title = league_label(lid)
        sections.append("")
        sections.append(title)

        # 1) current season
        try:
            season_id = get_current_season_id(lid)
        except Exception as e:
            sections.append(f"Error: could not get current season for league {lid} — {e}")
            continue

        # 2) top scorers
        try:
            scorers = fetch_top_scorers(season_id)[:LIMIT_PER_LEAGUE]
        except Exception as e:
            sections.append(f"Error fetching scorers: {e}")
            continue

        # 3) fixtures
        fixtures = load_fixtures_for_league(lid)

        # 4) build lines
        if not scorers:
            sections.append("No scorers returned.")
            continue

        rank = 1
        for sc in scorers:
            player = sc["player"]; team = sc["team"]; goals = sc.get("goals", 0)

            fx, side = next_fixture_for_team(team, fixtures)
            if not fx:
                sections.append(f"{rank}. {player} — {team} — {goals} (no upcoming fixture in {WINDOW_DAYS}d)")
                rank += 1
                continue

            fid = int(fx.get("id") or 0)
            opponent = ""
            try:
                parts = fx.get("participants") or []
                home = (parts[0] or {}).get("name") or ""
                away = (parts[1] or {}).get("name") or ""
                opponent = away if side == "home" else home
            except Exception:
                pass

            rows = load_fixture_odds_rows(fid)
            price = extract_anytime_price(rows, player)
            # also helpful (optional): team ML if you want to filter later
            team_ml = extract_team_ml(rows, side) if rows else None

            price_str = f" @ {price:.3f}" if price is not None else " — Odds N/A"
            ml_str = f" | Team ML {team_ml:.3f}" if team_ml is not None else ""

            kickoff = (fx.get("starting_at") or "").replace("T"," ").replace("Z","")
            vs = f"{team} vs {opponent}" if side == "home" else f"{opponent} vs {team}"

            sections.append(f"{rank}. {player} — {team} — {goals} — {vs} — {kickoff}{price_str}{ml_str}")
            rank += 1

    # write file
    ensure_parent(OUTPUT_PATH)
    with OUTPUT_PATH.open("w", encoding="utf-8") as f:
        f.write("\n".join(sections).rstrip() + "\n")

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
