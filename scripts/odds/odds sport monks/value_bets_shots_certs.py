#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Value bets — SHOTS certs (1+ in 100% of last 7, n>=7)
Sportmonks-only odds.

Sources:
  - data/player_shots/by_league/{league_id}.json   (primary)
  - data/player_shots/combined.json                (optional, auto-used if present)
  - data/predicted_xi/by_league/{league_id}.json   (team_id -> name map)

Bookmaker: Bet365 (resolved via /v3/odds/bookmakers; fallback id=2)
Market: Player Shots (attempts) Over 0.5 (exclude SOT/halves/footed/location markets)

Filters:
  - last 7 all >=1
  - Over 0.5 >= MIN_DEC_PRICE (default 1.30)
  - Team ML (1X2) < TEAM_WIN_MAX (default 3.50)

Output: data/value_bets/shots_certs.txt
"""

import os, re, json, math, time, random, unicodedata, datetime as dt
from pathlib import Path
from typing import Dict, List, Tuple, Optional, Any, Set
import requests

# ---------- CONFIG ----------
BASE = "https://api.sportmonks.com/v3"
TIMEOUT = 25
MIN_PRICE   = float(os.getenv("MIN_DEC_PRICE", "1.30"))
TEAM_ML_MAX = float(os.getenv("TEAM_WIN_MAX",  "3.50"))
BOOKMAKER_NAME = os.getenv("BOOKMAKER_NAME", "bet365").strip()

ROOT = Path(".")
PX_DIR       = ROOT / "data" / "predicted_xi" / "by_league"
SHOTS_DIR    = ROOT / "data" / "player_shots" / "by_league"
COMBINED_PTH = ROOT / "data" / "player_shots" / "combined.json"
OUT_DIR      = ROOT / "data" / "value_bets"; OUT_DIR.mkdir(parents=True, exist_ok=True)
OUT_FILE     = OUT_DIR / "shots_certs.txt"

HEADERS = {"accept": "application/json", "user-agent": "sm-odds-shots-certs/1.3"}

NEGATIVE_SHOTS_TERMS = {
    "on target","sot","outside","outside box","outside of box","from outside","outside the box",
    "header","headers","head","left foot","right foot","right-foot","left-foot",
    "first half","1st half","2nd half","second half","half",
    "distance","long range","goal","goals","to score","assist","assists","ga","g/a",
    "shots on target","on-target","from corner","from free kick","penalty","penalties"
}
MATCH_WINNER_KEYS = {"1x2","match result","match winner","moneyline","full time result","to win","win/draw/win","wdw","ml","fulltime result","result"}

# ---------- STRING HELPERS ----------
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
    if ta == tb or ta.issubset(tb) or tb.issubset(ta): return True
    inter = ta & tb; union = ta | tb
    return (len(inter) / max(1, len(union)) >= 0.5) or (len(inter) >= 2)

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
    if not player or not text: return False
    last, initial = extract_last_name_initial(player)
    label = norm(cleanup_label(text))
    if not last or last not in label: return False
    if initial:
        fw = label.split()[0][0:1] if label.split() else None
        if fw and fw == initial: return True
        return bool(re.search(rf"\b{initial}\w*\b.*\b{last}\b", label))
    return True

def market_is_player_shots(desc: str) -> bool:
    s = norm(desc)
    if not s: return False
    if ("player" in s and "shot" in s) or ("attempt" in s and "player" in s):
        if any(b in s for b in NEGATIVE_SHOTS_TERMS): return False
        return True
    return False

# ---------- IO ----------
def _load_json(p: Path) -> Any:
    try: return json.loads(p.read_text(encoding="utf-8"))
    except Exception: return None

def _team_name_map(league_id: int) -> Dict[int, str]:
    blob = _load_json(PX_DIR / f"{league_id}.json") or {}
    m: Dict[int, str] = {}
    for fx in (blob.get("fixtures") or []):
        for side in ("home","away"):
            s = fx.get(side) or {}
            tid, nm = s.get("team_id"), s.get("name")
            if isinstance(tid, int) and isinstance(nm, str) and nm:
                m.setdefault(tid, nm)
    return m

def _load_combined_players() -> List[dict]:
    blob = _load_json(COMBINED_PTH) or {}
    rows = blob.get("players") or []
    return rows if isinstance(rows, list) else []

def discover_leagues() -> List[int]:
    lids = set()
    for p in SHOTS_DIR.glob("*.json"):
        try: lids.add(int(p.stem))
        except: pass
    for rec in _load_combined_players():
        lid = rec.get("league_id")
        if isinstance(lid, int): lids.add(lid)
    return sorted(lids)

def last7_all_one_plus(series: List[int]) -> bool:
    seq = [x for x in series if isinstance(x, int)]
    return len(seq) >= 7 and all(x >= 1 for x in seq[:7])

def collect_candidates() -> List[dict]:
    out = []
    combined_rows = _load_combined_players()
    if combined_rows:
        team_map_by_league: Dict[int, Dict[int,str]] = {lid: _team_name_map(lid) for lid in discover_leagues()}
        for rec in combined_rows:
            series = rec.get("shots_last_n") or rec.get("series") or []
            if not isinstance(series, list) or not last7_all_one_plus(series): continue
            player = rec.get("name") or rec.get("player_name") or rec.get("player")
            if not player: continue
            lid = rec.get("league_id"); tid = rec.get("team_id")
            team = (team_map_by_league.get(int(lid), {}) or {}).get(int(tid), "") if isinstance(lid,int) and isinstance(tid,int) else ""
            pos = rec.get("position_tag") or rec.get("position") or rec.get("pos") or ""
            out.append({"league_id": int(lid) if isinstance(lid,int) else None, "player": player, "team": team,
                        "position": pos, "series": series[:10], "team_id": int(tid) if isinstance(tid,int) else None})
        return out

    # per-league files (original behaviour)
    for lid in discover_leagues():
        shots_blob = _load_json(SHOTS_DIR / f"{lid}.json") or {}
        team_map = _team_name_map(lid)
        players = shots_blob.get("players") or shots_blob.get("rows") or shots_blob.get("data") or []
        for rec in players:
            series = rec.get("series") or rec.get("shots_last_n") or rec.get("shots") or []
            if not isinstance(series, list) or not last7_all_one_plus(series): continue
            player = rec.get("name") or rec.get("player_name") or rec.get("player")
            if not player: continue
            tid = rec.get("team_id")
            team = rec.get("team") or rec.get("team_name") or (team_map.get(int(tid)) if isinstance(tid,int) else "") or ""
            pos = rec.get("position") or rec.get("position_tag") or rec.get("pos") or ""
            out.append({"league_id": lid, "player": player, "team": team, "position": pos, "series": series[:10],
                        "team_id": int(tid) if isinstance(tid,int) else None})
    return out

# ---------- HTTP ----------
def http_get(url: str, params: dict) -> Optional[requests.Response]:
    token = os.getenv("SPORTMONKS_TOKEN", "").strip()
    if not token: raise SystemExit("ERROR: SPORTMONKS_TOKEN not set.")
    p = dict(params or {})
    p.setdefault("api_token", token)
    try:
        r = requests.get(url, params=p, headers=HEADERS, timeout=TIMEOUT)
        if r.status_code == 200:
            return r
        if r.status_code in (429,500,502,503,504):
            # simple backoff
            for i in range(5):
                sleep = (1.4 ** i) + random.uniform(0,0.3)
                print(f"[RETRY] {url} {r.status_code}; sleeping {sleep:.1f}s...")
                time.sleep(sleep)
                r = requests.get(url, params=p, headers=HEADERS, timeout=TIMEOUT)
                if r.status_code == 200: return r
        print(f"[HTTP {r.status_code}] {url} :: {r.text[:200]}")
        return None
    except requests.exceptions.RequestException as e:
        print(f"[NET] {url} exception: {e}")
        return None

# ---------- SPORTMONKS CALLS ----------
def get_bookmaker_id() -> int:
    # GET all bookmakers, find bet365 (or BOOKMAKER_NAME)
    url = f"{BASE}/odds/bookmakers"
    r = http_get(url, {})
    if r:
        try:
            for b in (r.json().get("data") or []):
                if (b.get("name") or "").strip().lower() == BOOKMAKER_NAME.lower():
                    return int(b.get("id"))
        except Exception:
            pass
    print("[WARN] Could not resolve bookmaker via API; using fallback id=2 for bet365.")
    return 2

def fixtures_between(leagues: List[int], days_ahead: int = 7) -> List[dict]:
    """Use the CORRECT endpoint: /football/fixtures/between/{start}/{end} (NOT /date-range)."""
    if not leagues: return []
    start = dt.datetime.utcnow().date()
    end = start + dt.timedelta(days=days_ahead)
    url = f"{BASE}/football/fixtures/between/{start:%Y-%m-%d}/{end:%Y-%m-%d}"
    params = {"include": "participants", "filters": f"fixtureLeagues:{','.join(map(str,leagues))}", "per_page": 50}
    out = []
    page = 1
    while True:
        r = http_get(url, dict(params, page=page))
        if not r: break
        j = r.json() or {}
        out.extend(j.get("data") or [])
        meta = j.get("meta") or {}
        if not meta.get("has_more"): break
        page = int(meta.get("current_page", 1)) + 1
    return out

def fixture_teams(fx: dict) -> Tuple[str,str]:
    home, away = "", ""
    for p in (fx.get("participants") or []):
        loc = ((p.get("meta") or {}).get("location") or "").lower()
        if loc == "home": home = p.get("name") or ""
        elif loc == "away": away = p.get("name") or ""
    return home, away

def prematch_odds_by_fixture_bookmaker(fixture_id: int, bookmaker_id: int) -> List[dict]:
    url = f"{BASE}/football/odds/pre-match/fixtures/{fixture_id}/bookmakers/{bookmaker_id}"
    r = http_get(url, {})
    if not r: return []
    try: return r.json().get("data") or []
    except Exception: return []

# ---------- ODDS PARSERS ----------
def parse_decimal(v) -> Optional[float]:
    try:
        if v is None or v == "N/A": return None
        return float(v)
    except Exception:
        return None

def market_is_match_winner(desc: str) -> bool:
    s = (desc or "").strip().lower()
    return any(k in s for k in MATCH_WINNER_KEYS)

def min_win_prices(rows: List[dict]) -> Tuple[Optional[float], Optional[float]]:
    best_home = None; best_away = None
    for row in rows:
        if not market_is_match_winner(row.get("market_description","")): continue
        name = (row.get("name") or "").strip().lower()
        label = (row.get("label") or "").strip().lower()
        price = parse_decimal(row.get("value"))
        if price is None: continue
        if name in ("home","1") or label in ("home","1"): best_home = price if (best_home is None or price < best_home) else best_home
        if name in ("away","2") or label in ("away","2"): best_away = price if (best_away is None or price < best_away) else best_away
    return best_home, best_away

def extract_line(row: dict) -> Optional[float]:
    t = row.get("total"); h = row.get("handicap")
    for x in (t, h):
        if isinstance(x, (int,float)) or (isinstance(x,str) and re.fullmatch(r"-?\d+(?:\.\d+)?", x or "")):
            try: return float(x)
            except: pass
    m = re.search(r"\(([-+]?\d+(?:\.\d+)?)\)", row.get("label") or "")
    if m:
        try: return float(m.group(1))
        except: pass
    return None

def is_over_selection(row: dict) -> bool:
    name = (row.get("name") or "").strip().lower()
    label = (row.get("label") or "").strip().lower()
    return ("over" in name) or ("over" in label)

def player_row_matches(player: str, row: dict) -> bool:
    parts = row.get("participants")
    if parts:
        if isinstance(parts, str):
            if player_label_matches(player, parts): return True
        elif isinstance(parts, list):
            concat = " ".join([str(x.get("name") if isinstance(x, dict) else x) for x in parts])
            if player_label_matches(player, concat): return True
        elif isinstance(parts, dict):
            concat = " ".join(map(str, parts.values()))
            if player_label_matches(player, concat): return True
    if player_label_matches(player, row.get("name") or ""): return True
    if player_label_matches(player, row.get("label") or ""): return True
    return False

# ---------- MAIN ----------
def main():
    # 1) Candidates
    candidates = collect_candidates()
    if not candidates:
        OUT_FILE.write_text("No player candidates with 1+ in last 7.\n", encoding="utf-8"); print("No candidates."); return
    print(f"[CANDIDATES] {len(candidates)} players qualify (series>=7 all >=1).")

    # 2) Fixtures (CORRECT endpoint)
    lids_used = sorted({int(c["league_id"]) for c in candidates if isinstance(c.get("league_id"), int)})
    fixtures = fixtures_between(lids_used, days_ahead=7)
    print(f"[FIXTURES] Retrieved {len(fixtures)} fixtures across {len(lids_used)} leagues (next 7 days).")
    if not fixtures:
        OUT_FILE.write_text("No fixtures found for next 7 days.\n", encoding="utf-8"); print("No fixtures."); return

    # 3) Bookmaker id + odds pull
    bookmaker_id = get_bookmaker_id()
    fx_odds: Dict[int, List[dict]] = {}
    market_counts: Dict[str,int] = {}
    for fx in fixtures:
        fid = int(fx.get("id"))
        rows = prematch_odds_by_fixture_bookmaker(fid, bookmaker_id)
        if rows:
            fx_odds[fid] = rows
            for r in rows:
                md = (r.get("market_description") or "").strip()
                if md: market_counts[md] = market_counts.get(md, 0) + 1

    print(f"[ODDS] Unique fixtures with odds ({BOOKMAKER_NAME}): {len(fx_odds)}")
    top_markets = ", ".join([f"{k}({v})" for k,v in sorted(market_counts.items(), key=lambda kv:(-kv[1],kv[0]))[:20]])
    print(f"[DEBUG] Top markets returned: {top_markets or '—'}")

    # 4) Scan for Player Shots O0.5 + ML filter
    flagged: List[dict] = []
    for c in candidates:
        for fx in fixtures:
            home, away = fixture_teams(fx)
            if c["team"] and not (team_names_match(c["team"], home) or team_names_match(c["team"], away)):
                continue
            fid = int(fx["id"])
            rows = fx_odds.get(fid) or []
            if not rows: continue
            best_home_ml, best_away_ml = min_win_prices(rows)
            side = "home" if team_names_match(c["team"], home) else ("away" if team_names_match(c["team"], away) else None)
            if not side: continue
            team_ml = best_home_ml if side == "home" else best_away_ml
            if team_ml is None or team_ml >= TEAM_ML_MAX: continue

            best_price = None; market_seen = None
            for row in rows:
                md = row.get("market_description") or ""
                if not market_is_player_shots(md): continue
                if not player_row_matches(c["player"], row): continue
                line = extract_line(row)
                if line is None or not math.isclose(line, 0.5, abs_tol=1e-9): continue
                if not is_over_selection(row): continue
                price = parse_decimal(row.get("value"))
                if price is None or price < MIN_PRICE: continue
                if best_price is None or price > best_price + 1e-9:
                    best_price = price; market_seen = md

            if best_price is not None:
                flagged.append({
                    "player": c["player"], "position": c["position"], "team": c["team"] or (home if side=="home" else away),
                    "fixture": f"{home} vs {away}",
                    "kickoff": (fx.get("starting_at") or "").replace("T"," ").replace("Z",""),
                    "price": best_price, "team_ml": team_ml,
                    "series": c["series"], "league_id": c["league_id"], "market": market_seen or "Player Shots",
                })

    # 5) Output
    flagged.sort(key=lambda x: (-x["price"], x["player"]))
    lines = []
    lines.append(f"Generated at (UTC): {dt.datetime.utcnow().isoformat()}  |  Min price: {MIN_PRICE:.2f}  |  Team ML < {TEAM_ML_MAX:.2f}")
    lines.append(f"Criteria: 1+ shot in 100% of last 7 (n>=7)  |  Market: {BOOKMAKER_NAME} Player Shots Over 0.5")
    lines.append("")
    if not flagged:
        lines.append("No matches found.")
        lines.append("")
        lines.append("Notes:")
        lines.append(" - If '[DEBUG] Top markets returned' does not include a Player Shots market, it is not present in your current feed/bookmaker.")
        lines.append(" - Standard pre-match odds exist at /football/odds/pre-match; Player Shots coverage may require different bookmakers or the Premium feed. ")
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
