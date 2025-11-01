# ========================= scripts/team_win_rates.py =========================
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Most Corners — home/away split win-rates + Bet365 price filter.

Reads (local files only):
- Fixtures:                    data/fixtures/{league_id}.json
- Team series (+ H/A flags):   data/team_stats/by_league/{league_id}.json
- Opponent series (+ H/A):     data/team_opponent_stats/by_league/{league_id}.json
- Bet365 odds (per-league):    data/odds/b365/{league_id}.json

What we compute:
For each team we compare its 'corners_last_n' vs 'opp_corners_last_n' match-by-match.
We split into HOME and AWAY using 'locations_last_n'. For each split:
    wins = #games team corners > opp corners
    losses = #games team corners < opp corners
    draws = equal corners
    n = wins + losses (draws excluded)
    win_rate = wins / n  (if n>0)

For each upcoming fixture in WINDOW_DAYS, we pick a side if:
    - Side-specific win_rate >= MIN_SIDE_RATE
    - Gap between sides' rates >= MIN_GAP
    - Bet365 price for the side in a "Most Corners" 3-way market >= MIN_DEC_PRICE

Output (minimal):
  League {lid}: X candidates
  HomeTeam vs AwayTeam — Most Corners: {HOME|AWAY} @ {price}
    Home win rate: {pct}% (home n={nH}) | Away win rate: {pct}% (away n={nA})

Env (optional):
- LEAGUE_IDS      CSV of league IDs to scan (default: discover from fixtures dir)
- WINDOW_DAYS     default 7
- MIN_DEC_PRICE   default 1.70
- MIN_SIDE_RATE   default 0.55
- MIN_GAP         default 0.15
"""

import os, re, json, math, datetime as dt, unicodedata
from pathlib import Path
from typing import Dict, List, Optional, Tuple

ROOT      = Path(".")
FIX_DIR   = ROOT / "data" / "fixtures"
TS_DIR    = ROOT / "data" / "team_stats" / "by_league"
OPP_DIR   = ROOT / "data" / "team_opponent_stats" / "by_league"
ODDS_DIR  = ROOT / "data" / "odds" / "b365"

WINDOW_DAYS   = int(os.getenv("WINDOW_DAYS", "7"))
MIN_DEC_PRICE = float(os.getenv("MIN_DEC_PRICE", "1.70"))
MIN_SIDE_RATE = float(os.getenv("MIN_SIDE_RATE", "0.55"))
MIN_GAP       = float(os.getenv("MIN_GAP", "0.15"))

# ---------- string helpers ----------
def strip_accents(s: str) -> str:
    return ''.join(c for c in unicodedata.normalize('NFD', s or '') if unicodedata.category(c) != 'Mn')

def norm(s: str) -> str:
    s = strip_accents(s or "").lower()
    s = re.sub(r"[^a-z0-9\s\.-]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s

GENERIC_TOK = {"fc","cf","afc","sc","cd","ud","ac","as","ss","ssc","us","uc","rc","rcd","ca",
               "the","club","de","del","la","las","los","calcio","united","city","saint","st","bk"}

def team_tokens(name: str):
    toks = set(norm(name).split())
    return {t for t in toks if t not in GENERIC_TOK}

def team_names_match(a: str, b: str) -> bool:
    if not a or not b: return False
    ta, tb = team_tokens(a), team_tokens(b)
    if not ta or not tb: return False
    if ta == tb or ta.issubset(tb) or tb.issubset(ta): return True
    inter = ta & tb; uni = ta | tb
    if len(inter) / max(1, len(uni)) >= 0.5: return True
    if len(inter) >= 2: return True
    return False

def parse_fixture_teams(fixture_name: str) -> Tuple[str, str]:
    if not fixture_name: return "",""
    for sep in (" vs ", " v ", " - ", " VS ", " Vs "):
        if sep in fixture_name:
            a, b = fixture_name.split(sep, 1)
            return a.strip(), b.strip()
    return "", ""

def within_window(starting_at: str, days: int) -> bool:
    if not starting_at: return True
    try:
        dt_utc = dt.datetime.strptime(starting_at[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=dt.timezone.utc)
    except Exception:
        return True
    now = dt.datetime.now(dt.timezone.utc)
    return now <= dt_utc <= (now + dt.timedelta(days=days))

# ---------- IO ----------
def load_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}

def discover_league_ids() -> List[int]:
    ids = []
    for p in FIX_DIR.glob("*.json"):
        try: ids.append(int(p.stem))
        except: pass
    return sorted(set(ids))

# ---------- indexers ----------
def index_team_rows(blob: dict) -> Dict[str, dict]:
    m: Dict[str, dict] = {}
    for t in (blob.get("teams") or []):
        nm = t.get("team_name")
        if not nm: continue
        m[norm(nm)] = t
    return m

# ---------- win-rate calc (HOME/AWAY split) ----------
def split_win_rates(team_row: dict, opp_row: dict, key_team: str, key_opp: str) -> Tuple[Tuple[int,int,int,int,float], Tuple[int,int,int,int,float]]:
    """
    Returns:
      home_tuple = (wins, losses, draws, n_non_draw, rate)
      away_tuple = (wins, losses, draws, n_non_draw, rate)
    """
    xs  = [x for x in (team_row.get(key_team) or []) if isinstance(x, int)]
    ys  = [y for y in (opp_row.get(key_opp)  or []) if isinstance(y, int)]
    loc = [s for s in (team_row.get("locations_last_n") or [])]

    n = min(len(xs), len(ys), len(loc))
    h_w=h_l=h_d=h_n=0
    a_w=a_l=a_d=a_n=0
    for i in range(n):
        side = (loc[i] or "").lower()
        a_val, b_val = xs[i], ys[i]
        if a_val == b_val:
            if side == "home": h_d += 1
            elif side == "away": a_d += 1
            continue
        win = a_val > b_val
        if side == "home":
            if win: h_w += 1
            else:   h_l += 1
        elif side == "away":
            if win: a_w += 1
            else:   a_l += 1
        # ignore "unknown" location for split rates

    h_n = h_w + h_l
    a_n = a_w + a_l
    h_rate = (h_w / h_n) if h_n > 0 else 0.0
    a_rate = (a_w / a_n) if a_n > 0 else 0.0
    return (h_w, h_l, h_d, h_n, h_rate), (a_w, a_l, a_d, a_n, a_rate)

# ---------- odds parsing ----------
def label_to_side(s: Optional[str]) -> Optional[str]:
    s = (s or "").strip().lower()
    if s in {"1","home","home (1)","team 1"}: return "home"
    if s in {"2","away","away (2)","team 2"}: return "away"
    if s in {"x","draw","tie"}: return "draw"
    return None

def is_most_corners_market(md: str) -> bool:
    s = norm(md)
    if not s: return False
    if "race" in s or "handicap" in s or "total" in s or "over" in s or "under" in s:
        return False
    # Accept broad synonyms
    keys = [
        "most corners", "corner match bet", "corners match bet",
        "team with most corners", "which team will have the most corners",
        "corners - most", "corners most"
    ]
    return any(k in s for k in keys) or (("corner" in s or "corners" in s) and "most" in s)

def extract_most_corners_prices(rows: List[dict]) -> Tuple[Optional[float], Optional[float], Optional[float]]:
    """
    Returns (home_price, away_price, draw_price) from Bet365 rows if present.
    We take the MAX price seen for each side (best for the bettor).
    """
    h, a, d = None, None, None
    for r in rows or []:
        if not is_most_corners_market(r.get("market_description","")):
            continue
        side = label_to_side(r.get("label"))
        try:
            price = float(r.get("value"))
        except Exception:
            continue
        if side == "home":
            h = price if (h is None or price > h) else h
        elif side == "away":
            a = price if (a is None or price > a) else a
        elif side == "draw":
            d = price if (d is None or price > d) else d
    return h, a, d

# ---------- main ----------
def main():
    # leagues to scan
    if os.getenv("LEAGUE_IDS"):
        league_ids = [int(x) for x in os.getenv("LEAGUE_IDS").split(",") if x.strip()]
    else:
        league_ids = discover_league_ids()

    any_out = False

    for lid in league_ids:
        fx_path   = FIX_DIR / f"{lid}.json"
        ts_path   = TS_DIR / f"{lid}.json"
        opp_path  = OPP_DIR / f"{lid}.json"
        odds_path = ODDS_DIR / f"{lid}.json"
        if not (fx_path.exists() and ts_path.exists() and opp_path.exists() and odds_path.exists()):
            # skip quietly if any required file is missing
            continue

        fixtures = load_json(fx_path).get("fixtures") or []
        ts_idx   = index_team_rows(load_json(ts_path))
        opp_idx  = index_team_rows(load_json(opp_path))

        odds_blob = load_json(odds_path)
        odds_by_fixture = {int(f.get("fixture_id")): f for f in (odds_blob.get("fixtures") or []) if isinstance(f.get("fixture_id"), int)}

        candidates = []

        for fx in fixtures:
            if not isinstance(fx, dict): continue
            fid = fx.get("id") or fx.get("fixture_id")
            name = fx.get("name") or ""
            starting_at = fx.get("starting_at") or ""
            if WINDOW_DAYS and not within_window(starting_at, WINDOW_DAYS):
                continue

            home_nm, away_nm = parse_fixture_teams(name)
            if not home_nm or not away_nm: 
                continue

            # find team rows by fuzzy name
            home_rec_t = None; away_rec_t = None
            home_rec_o = None; away_rec_o = None
            for k, row in ts_idx.items():
                if team_names_match(home_nm, row.get("team_name","")): home_rec_t = row
                if team_names_match(away_nm, row.get("team_name","")): away_rec_t = row
            for k, row in opp_idx.items():
                if team_names_match(home_nm, row.get("team_name","")): home_rec_o = row
                if team_names_match(away_nm, row.get("team_name","")): away_rec_o = row
            if not (home_rec_t and away_rec_t and home_rec_o and away_rec_o):
                continue

            # compute split win-rates for corners
            (hW,hL,hD,hN,hRate), _ = split_win_rates(home_rec_t, home_rec_o, "corners_last_n", "opp_corners_last_n")
            _, (aW,aL,aD,aN,aRate) = split_win_rates(away_rec_t, away_rec_o, "corners_last_n", "opp_corners_last_n")

            # choose side by thresholds
            pick_side = None
            if (aRate >= MIN_SIDE_RATE) and ((aRate - hRate) >= MIN_GAP):
                pick_side = "away"
            if (hRate >= MIN_SIDE_RATE) and ((hRate - aRate) >= MIN_GAP):
                # if both satisfy (rare), prefer the bigger gap
                if pick_side is None or (hRate - aRate) > (aRate - hRate):
                    pick_side = "home"
            if not pick_side:
                continue

            # odds
            odds_fx = odds_by_fixture.get(int(fid)) if isinstance(fid, int) else None
            if not odds_fx: 
                continue
            home_p, away_p, _draw = extract_most_corners_prices(odds_fx.get("odds") or [])
            price = home_p if pick_side=="home" else away_p
            if price is None or price < MIN_DEC_PRICE:
                continue

            # pack
            line = {
                "fixture": name,
                "side": pick_side,
                "price": float(price),
                "home_rate": hRate, "home_n": hN,
                "away_rate": aRate, "away_n": aN,
            }
            candidates.append(line)

        # render league block
        if candidates:
            any_out = True
            print(f"League {lid}: {len(candidates)} candidates")
            for r in candidates:
                side_txt = "HOME" if r["side"]=="home" else "AWAY"
                print(f"{r['fixture']} — Most Corners: {side_txt} @ {r['price']:.2f}")
                print(f"  Home win rate: {r['home_rate']*100:.1f}% (home n={r['home_n']}) | Away win rate: {r['away_rate']*100:.1f}% (away n={r['away_n']})")
            print("")
        else:
            print(f"League {lid}: no candidates")

    if not any_out:
        pass  # keep quiet overall (league lines are enough)

if __name__ == "__main__":
    main()
