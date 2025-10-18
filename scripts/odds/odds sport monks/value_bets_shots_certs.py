#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Value bets — SHOTS certs (1+ in 100% of last 7, n>=7)
Source data: data/player_shots/by_league/{league_id}.json OR data/player_shots/combined.json
Predicted XI used for team name maps (by_league/{league_id}.json).
Bookmaker: Bet365 by default (override via BOOKMAKER_NAME or BOOKMAKER_ID).
Markets: Player Shots (attempts) ONLY — exclude SOT/halves/footed/locations.
Filters:
  - Player qualifies: last 7 matches all >= 1 (len(series) >= 7)
  - Price Over 0.5 >= MIN_PRICE (default 1.30)
  - Team ML (Bet365) for player's side < TEAM_ML_MAX (default 3.50)
Output: data/value_bets/shots_certs.txt + console

ENV:
  SPORTMONKS_TOKEN   (required)
  MIN_DEC_PRICE      (default 1.30)
  TEAM_WIN_MAX       (default 3.50)
  BOOKMAKER_NAME     (default "Bet365")
  BOOKMAKER_ID       (optional; overrides name lookup)
"""

import os, re, json, math, time, random, unicodedata, datetime as dt
from pathlib import Path
from typing import Dict, List, Tuple, Optional, Any
import requests
from itertools import islice

# ========= CONFIG =========
MIN_PRICE = float(os.getenv("MIN_DEC_PRICE", "1.30"))
TEAM_ML_MAX = float(os.getenv("TEAM_WIN_MAX", "3.50"))
BOOKMAKER_NAME = os.getenv("BOOKMAKER_NAME", "Bet365").strip()
BOOKMAKER_ID_OVERRIDE = os.getenv("BOOKMAKER_ID")
BET365_FALLBACK_ID = 2  # (Sportmonks docs/examples commonly use 2 for Bet365)

ROOT = Path(".")
PX_DIR       = ROOT / "data" / "predicted_xi" / "by_league"
SHOTS_DIR    = ROOT / "data" / "player_shots" / "by_league"
COMBINED_PTH = ROOT / "data" / "player_shots" / "combined.json"
OUT_DIR      = ROOT / "data" / "value_bets"; OUT_DIR.mkdir(parents=True, exist_ok=True)
OUT_FILE     = OUT_DIR / "shots_certs.txt"

BASE = "https://api.sportmonks.com/v3"
HTTP_HEADERS = {"accept": "application/json", "user-agent": "sm-odds-shots-certs/1.1", "cache-control": "no-cache"}
TIMEOUT = 25

# ========= MARKET FILTERS (shots-only, exclude SOT/outside/halves etc.) =========
NEGATIVE_SHOTS_TERMS = {
    "on target","sot","outside","outside box","outside of box","from outside","outside the box",
    "header","headers","head","left foot","right foot","right-foot","left-foot",
    "first half","1st half","2nd half","second half","half",
    "distance","long range","goal","goals","to score","assist","assists","ga","g/a",
    "shots on target","on-target","from corner","from free kick","penalty","penalties"
}

def market_is_player_shots(desc: str) -> bool:
    """Accept a wide set of phrasings for player attempts markets."""
    s = re.sub(r"\s+", " ", (desc or "")).strip().lower()
    if not s: return False
    # Common phrasings we have seen: "Player Shots", "Player - Shots", "Shots - Player", "Player Attempts"
    if ("player" in s and "shot" in s) or ("attempt" in s and "player" in s):
        if any(b in s for b in NEGATIVE_SHOTS_TERMS):  # exclude SOT/halves/footed/location etc.
            return False
        return True
    return False

MATCH_WINNER_KEYS = {"1x2","match result","match winner","moneyline","full time result","to win","win/draw/win","wdw","ml","fulltime result","result"}

def market_is_match_winner(desc: str) -> bool:
    s = (desc or "").strip().lower()
    return any(k in s for k in MATCH_WINNER_KEYS)

# ========= STRING NORMALISATION =========
def strip_accents(s: str) -> str:
    return ''.join(c for c in unicodedata.normalize('NFD', s or '') if unicodedata.category(c) != 'Mn')

def norm(s: str) -> str:
    s = strip_accents(s or "").lower()
    s = re.sub(r"[^a-z0-9\s\.-]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s

GENERIC_TEAM_TOKENS = {"fc","cf","afc","sc","cd","ud","ac","as","ss","ssc","us","uc","rc","rcd","ca","the","club","de","del","la","las","los","calcio","united","city","saint","st","bk"}
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

def cleanup_label(label: str) -> str:
    return re.sub(r"(?:\s*\([^)]*\))+$", "", label or "").strip()

def extract_last_name_initial(name: str):
    if not name: return None, None
    name2 = strip_accents(name).replace(".", " ").strip()
    parts = [p for p in name2.split() if p]
    if not parts: return None, None
    last = norm(parts[-1]); initial = None
    for p in parts[:-1]:
        ch = p.strip()[0:1]
        if ch: initial = ch.lower(); break
    return last, initial

def player_label_matches(player: str, text: str) -> bool:
    """Match player vs arbitrary label/name/participants text."""
    if not player or not text: return False
    last, initial = extract_last_name_initial(player)
    label = norm(cleanup_label(text))
    if not last or last not in label: return False
    if initial:
        first_word_initial = label.split()[0][0:1] if label.split() else None
        if first_word_initial and first_word_initial == initial: return True
        return bool(re.search(rf"\b{initial}\w*\b.*\b{last}\b", label))
    return True

# ========= IO HELPERS =========
def _load_json(p: Path) -> Any:
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None

def _team_name_map(league_id: int) -> Dict[int, str]:
    """Map team_id -> team_name from predicted_xi file per league."""
    blob = _load_json(PX_DIR / f"{league_id}.json") or {}
    m: Dict[int, str] = {}
    for fx in (blob.get("fixtures") or []):
        for side in ("home", "away"):
            s = fx.get(side) or {}
            tid, nm = s.get("team_id"), s.get("name")
            if isinstance(tid, int) and isinstance(nm, str) and nm:
                m.setdefault(tid, nm)
    return m

def _load_combined_players() -> List[dict]:
    """Read combined.json if present; return list of player rows."""
    blob = _load_json(COMBINED_PTH) or {}
    rows = blob.get("players") or []
    return rows if isinstance(rows, list) else []

def discover_leagues() -> List[int]:
    lids = set()
    for p in SHOTS_DIR.glob("*.json"):
        try: lids.add(int(p.stem))
        except: pass
    # Also gather from combined.json if present
    for rec in _load_combined_players():
        lid = rec.get("league_id")
        if isinstance(lid, int): lids.add(lid)
    return sorted(lids)

def last7_all_one_plus(series: List[int]) -> bool:
    seq = [x for x in series if isinstance(x, int)]
    if len(seq) < 7: return False
    sub = seq[:7]  # assume series is newest -> older
    return all(x >= 1 for x in sub)

def collect_candidates() -> List[dict]:
    """
    Prefer combined.json if present (so we scan *all* players once),
    otherwise fall back to per-league files.
    """
    out = []
    combined_rows = _load_combined_players()
    if combined_rows:
        # Build per-league team name maps because combined lacks team names
        team_map_by_league: Dict[int, Dict[int,str]] = {lid: _team_name_map(lid) for lid in discover_leagues()}
        for rec in combined_rows:
            series = rec.get("shots_last_n") or rec.get("series") or rec.get("shots") or []
            if not isinstance(series, list): continue
            if not last7_all_one_plus(series): continue
            player = rec.get("name") or rec.get("player_name") or rec.get("player")
            if not player: continue
            lid = rec.get("league_id"); tid = rec.get("team_id")
            team = None
            if isinstance(lid, int) and isinstance(tid, int):
                team = (team_map_by_league.get(lid) or {}).get(tid)
            if not team: 
                # As a last resort, keep going — fixture matching below may still succeed.
                team = ""
            pos = rec.get("position_tag") or rec.get("position") or rec.get("pos") or ""
            out.append({
                "league_id": int(lid) if isinstance(lid, int) else None,
                "player": player,
                "team": team,
                "position": pos or "",
                "series": series[:10],
                "team_id": int(tid) if isinstance(tid, int) else None
            })
        return out

    # Fallback: per-league files (original behaviour)
    for lid in discover_leagues():
        shots_blob = _load_json(SHOTS_DIR / f"{lid}.json") or {}
        team_map = _team_name_map(lid)
        players = shots_blob.get("players") or shots_blob.get("rows") or shots_blob.get("data") or []
        for rec in players:
            series = rec.get("series") or rec.get("shots_last_n") or rec.get("shots") or []
            if not isinstance(series, list): continue
            if not last7_all_one_plus(series): continue
            player = rec.get("name") or rec.get("player_name") or rec.get("player")
            if not player: continue
            tid = rec.get("team_id")
            team = rec.get("team") or rec.get("team_name") or (team_map.get(int(tid)) if isinstance(tid, int) else None) or ""
            pos = rec.get("position") or rec.get("position_tag") or rec.get("pos")
            out.append({
                "league_id": lid, "player": player, "team": team,
                "position": pos or "", "series": series[:10],
                "team_id": int(tid) if isinstance(tid, int) else None
            })
    return out

# ========= HTTP =========
def http_get_with_retries(url: str, params: dict, max_retries=6, base_sleep=1.0, factor=1.8):
    attempt = 0; last_text = ""
    while attempt < max_retries:
        try:
            r = requests.get(url, params=params, headers=HTTP_HEADERS, timeout=TIMEOUT)
            if r.status_code == 200:
                return r
            if r.status_code in (429,500,502,503,504):
                last_text = r.text
                sleep = base_sleep * (factor ** attempt) + random.uniform(0, 0.4)
                print(f"[RETRY] {url} {r.status_code}; sleeping {sleep:.1f}s...")
                time.sleep(sleep); attempt += 1; continue
            print(f"[HTTP {r.status_code}] {url} ::    {r.text[:200]}")
            return None
        except requests.exceptions.RequestException as e:
            sleep = base_sleep * (factor ** attempt) + random.uniform(0, 0.4)
            print(f"[NET] {url} exception: {e}; sleeping {sleep:.1f}s...")
            time.sleep(sleep); attempt += 1
    if last_text:
        print(f"[ERROR] Retries exhausted for {url}. Last body: {last_text[:220]}")
    else:
        print(f"[ERROR] Retries exhausted for {url}.")
    return None

# ========= SPORTMONKS HELPERS =========
def get_bookmaker_id(token: str) -> int:
    if BOOKMAKER_ID_OVERRIDE and BOOKMAKER_ID_OVERRIDE.isdigit():
        return int(BOOKMAKER_ID_OVERRIDE)
    # Try search endpoint
    url = f"{BASE}/football/bookmakers"
    r = http_get_with_retries(url, {"api_token": token, "filters": f"bookmakersearch:{BOOKMAKER_NAME}"})
    if r and r.status_code == 200:
        try:
            data = r.json().get("data") or []
            for b in data:
                nm = (b.get("name") or "").strip().lower()
                if nm and BOOKMAKER_NAME.lower() in nm:
                    return int(b.get("id"))
        except Exception:
            pass
    print(f"[WARN] Could not resolve {BOOKMAKER_NAME} ID via API; using {BET365_FALLBACK_ID} as a fallback.")
    return BET365_FALLBACK_ID

def get_league_ids_in_repo() -> List[int]:
    return discover_leagues()

def get_next_7_days_fixtures(token: str, league_ids: List[int]) -> List[dict]:
    if not league_ids: return []
    today = dt.datetime.utcnow().date()
    end = today + dt.timedelta(days=7)
    url = f"{BASE}/football/fixtures/date-range/{today.isoformat()}/{end.isoformat()}"
    params = {"api_token": token, "include": "participants", "filters": f"fixtureLeagues:{','.join(map(str,league_ids))}"}
    r = http_get_with_retries(url, params)
    if not (r and r.status_code == 200):
        return []
    try:
        j = r.json(); rows = j.get("data") or []
        # Keep only fixtures with both participants named
        out = []
        for fx in rows:
            parts = fx.get("participants") or []
            if len(parts) >= 2:
                h = next((p for p in parts if str(p.get("meta","").get("location","")).lower() == "home"), None)
                a = next((p for p in parts if str(p.get("meta","").get("location","")).lower() == "away"), None)
                if h and a:
                    fx["_home_name"] = h.get("name") or ""
                    fx["_away_name"] = a.get("name") or ""
                    out.append(fx)
        return out
    except Exception:
        return []

def get_odds_for_fixture_bookmaker(token: str, fixture_id: int, bookmaker_id: int) -> List[dict]:
    url = f"{BASE}/football/odds/pre-match/fixtures/{fixture_id}/bookmakers/{bookmaker_id}"
    r = http_get_with_retries(url, {"api_token": token})
    if not (r and r.status_code == 200):
        return []
    try:
        return r.json().get("data") or []
    except Exception:
        return []

# ========= ODDS PARSING =========
def parse_decimal(val) -> Optional[float]:
    try:
        if val is None or val == "N/A": return None
        return float(val)
    except Exception:
        return None

def min_win_prices(odds_rows: List[dict]) -> Tuple[Optional[float], Optional[float]]:
    """Find minimum (best) ML for home/away across any 'match winner' market shapes."""
    best_home = None; best_away = None
    for row in odds_rows:
        if not market_is_match_winner(row.get("market_description","")):
            continue
        name = (row.get("name") or "").strip().lower()
        label = (row.get("label") or "").strip().lower()
        price = parse_decimal(row.get("value"))
        if price is None: 
            continue
        # Accept multiple shapes: Home/Away, 1/2, team names, etc.
        if name in ("home","1") or label in ("home","1"):
            best_home = price if (best_home is None or price < best_home) else best_home
        elif name in ("away","2") or label in ("away","2"):
            best_away = price if (best_away is None or price < best_away) else best_away
        else:
            # Sometimes label is the actual team name
            if "home" in name or "home" in label:
                best_home = price if (best_home is None or price < best_home) else best_home
            if "away" in name or "away" in label:
                best_away = price if (best_away is None or price < best_away) else best_away
    return best_home, best_away

def extract_line(row: dict) -> Optional[float]:
    """Try total/handicap or parse from label like '(0.5)'."""
    # Sportmonks puts totals often in 'total' (string)
    t = row.get("total")
    if isinstance(t, (int, float)) or (isinstance(t, str) and t.replace(".","",1).isdigit()):
        try: return float(t)
        except: pass
    h = row.get("handicap")
    if isinstance(h, (int, float)) or (isinstance(h, str) and h.replace(".","",1).isdigit()):
        try: return float(h)
        except: pass
    m = re.search(r"\(([-+]?\d+(?:\.\d+)?)\)", row.get("label") or "")
    if m:
        try: return float(m.group(1))
        except: pass
    return None

def is_over_selection(row: dict) -> bool:
    """Recognise 'Over' selection in multiple shapes."""
    name = (row.get("name") or "").strip().lower()
    label = (row.get("label") or "").strip().lower()
    return ("over" in name) or ("over" in label)

def player_row_matches(player: str, row: dict) -> bool:
    """Try multiple places: participants, name, label."""
    parts = row.get("participants")
    if parts:
        # participants can be string or list/dict depending on feed
        if isinstance(parts, str):
            if player_label_matches(player, parts): return True
        elif isinstance(parts, list):
            concat = " ".join([str(x.get("name") or x) for x in parts])
            if player_label_matches(player, concat): return True
        elif isinstance(parts, dict):
            concat = " ".join(map(str, parts.values()))
            if player_label_matches(player, concat): return True
    # Fallbacks
    if player_label_matches(player, row.get("name") or ""): return True
    if player_label_matches(player, row.get("label") or ""): return True
    return False

# ========= MAIN =========
def main():
    token = os.getenv("SPORTMONKS_TOKEN")
    if not token:
        raise SystemExit("ERROR: SPORTMONKS_TOKEN not set.")

    # 1) Build candidates
    candidates = collect_candidates()
    if not candidates:
        msg = "[RESULT] No player candidates with 1+ in each of last 7."
        OUT_FILE.write_text(msg + "\n", encoding="utf-8")
        print(msg)
        return
    print(f"[CANDIDATES] {len(candidates)} players qualify (series>=7 all >=1).")

    # 2) Fixtures next 7 days for leagues present in data
    league_ids = sorted({c["league_id"] for c in candidates if isinstance(c.get("league_id"), int)})
    fixtures = get_next_7_days_fixtures(token, league_ids)
    print(f"[FIXTURES] Retrieved {len(fixtures)} fixtures across {len(league_ids)} leagues (next 7 days).")

    if not fixtures:
        OUT_FILE.write_text("No fixtures found for next 7 days.\n", encoding="utf-8")
        print("No fixtures found for next 7 days.")
        return

    bookmaker_id = get_bookmaker_id(token)
    # 3) Fetch odds per fixture
    fx_odds: Dict[int, List[dict]] = {}
    market_buckets = {}
    for fx in fixtures:
        fid = int(fx.get("id"))
        rows = get_odds_for_fixture_bookmaker(token, fid, bookmaker_id)
        if rows:
            fx_odds[fid] = rows
            # collect market descriptions for debug
            for r in rows:
                md = r.get("market_description") or ""
                md = md.strip()
                if md:
                    market_buckets[md] = market_buckets.get(md, 0) + 1

    print(f"[ODDS] Unique fixtures to query ({BOOKMAKER_NAME}): {len(fx_odds)}")
    if not fx_odds:
        print("[RESULT] No odds payloads for selected bookmaker; stopping.")
        OUT_FILE.write_text("No odds payloads for selected bookmaker; stopping.\n", encoding="utf-8")
        return

    # Debug: show which markets Bet365 actually returns here
    dbg = sorted(market_buckets.items(), key=lambda kv: (-kv[1], kv[0]))
    show = ", ".join([f"{k}({v})" for k,v in dbg[:20]])
    print(f"[DEBUG] Top markets returned: {show if show else '—'}")

    # 4) Scan for player shots O0.5 + ML filter
    flagged: List[dict] = []
    for c in candidates:
        # find matching fixture(s) by team name
        for fx in fixtures:
            home = fx.get("_home_name",""); away = fx.get("_away_name","")
            if c["team"] and not (team_names_match(c["team"], home) or team_names_match(c["team"], away)):
                continue
            fid = int(fx["id"])
            rows = fx_odds.get(fid) or []
            # Team ML filter first
            best_home_ml, best_away_ml = min_win_prices(rows)
            side = "home" if team_names_match(c["team"], home) else ("away" if team_names_match(c["team"], away) else None)
            if not side:
                # if team name missing (from combined), try team_id by participants (best-effort: skip if unknown)
                continue
            team_ml = best_home_ml if side == "home" else best_away_ml
            if team_ml is None or team_ml >= TEAM_ML_MAX:
                continue

            # Now find Over 0.5 price in any recognized Player Shots market
            best_price = None; market_seen = None
            for row in rows:
                md = row.get("market_description") or ""
                if not market_is_player_shots(md):
                    continue
                if not player_row_matches(c["player"], row):
                    continue
                line = extract_line(row)
                if line is None or not math.isclose(line, 0.5, abs_tol=1e-6):
                    continue
                if not is_over_selection(row):
                    continue
                price = parse_decimal(row.get("value"))
                if price is None or price < MIN_PRICE:
                    continue
                if best_price is None or price > best_price + 1e-9:
                    best_price = price
                    market_seen = md

            if best_price is not None:
                flagged.append({
                    "player": c["player"], "position": c["position"], "team": c["team"] or (home if side=="home" else away),
                    "fixture": f"{home} vs {away}",
                    "kickoff": (fx.get("starting_at") or "").replace("T"," ").replace("Z",""),
                    "price": best_price, "team_ml": team_ml,
                    "series": c["series"], "league_id": c["league_id"], "market": market_seen or "Player Shots",
                })

    # 5) Render
    flagged.sort(key=lambda x: (-x["price"], x["player"]))
    lines = []
    lines.append(f"Generated at (UTC): {dt.datetime.utcnow().isoformat()}  |  Min price: {MIN_PRICE:.2f}  |  Team ML < {TEAM_ML_MAX:.2f}")
    lines.append(f"Criteria: 1+ shot in 100% of last 7 (n>=7)  |  Market: {BOOKMAKER_NAME} Player Shots Over 0.5")
    lines.append("")

    if not flagged:
        # If nothing flagged, drop a tiny diagnostic to help decisions:
        lines.append("No matches found.")
        lines.append("")
        lines.append("Notes:")
        lines.append(" - Likely cause: Player Shots market not available in the Standard odds feed for this bookmaker/fixture.")
        lines.append(" - Check the run logs for '[DEBUG] Top markets returned' to see available markets.")
        lines.append(" - Consider trying a different bookmaker (BOOKMAKER_NAME) or the Premium Odds feed for props.")
    else:
        lines.append("===== CERTS — Player Shots 1+ =====")
        for x in flagged:
            ser = ",".join(map(str, x["series"][:7]))
            pos = f"[{x['position']}]" if x.get("position") else ""
            lines.append(
                f" • {x['player']} {pos} — {x['team']} | {x['fixture']} @ {x['kickoff']} | "
                f"Over 0.5 @ {x['price']:.3f} | Team ML {x['team_ml']:.3f} | series7: {ser}"
            )

    OUT_FILE.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    print("\n".join(lines))

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
