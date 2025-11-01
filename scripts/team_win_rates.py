# ========================= scripts/team_win_rates.py =========================
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Most Corners — overall win rate + home/away-adjusted rates, ranked by likelihood.

Reads (local files only):
- Fixtures:                    data/fixtures/{league_id}.json
- Team series (+ H/A flags):   data/team_stats/by_league/{league_id}.json
- Opponent series (+ H/A):     data/team_opponent_stats/by_league/{league_id}.json
- Bet365 odds (per-league):    data/odds/b365/{league_id}.json

We compute for CORNERS:
  Overall win-rate (venue-agnostic) and split win-rates (HOME-only, AWAY-only).
  Draws are excluded from the denominator.

Selection (for fixtures within WINDOW_DAYS):
  - Pick HOME if its home-only win_rate >= MIN_SIDE_RATE and (home - away) >= MIN_GAP
  - Pick AWAY if its away-only win_rate >= MIN_SIDE_RATE and (away - home) >= MIN_GAP
  - Require Bet365 "Most Corners" price for the picked side >= MIN_DEC_PRICE

Ranking:
  Sort candidates by:
    1) chosen side split win rate (desc)
    2) gap vs other side (desc)
    3) price (desc)
    4) fixture name (asc)

Output (minimal, 2 lines per pick):
  Fixture — Most Corners: {HOME|AWAY} @ {price}
    Home win rate: {overall%} overall | {home%} home (n={nH}) | Away win rate: {overall%} overall | {away%} away (n={nA})

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

# ---------- win-rate calcs ----------
def overall_win_rate(team_row: dict, opp_row: dict, key_team: str, key_opp: str) -> Tuple[int,int,int,int,float]:
    xs = [x for x in (team_row.get(key_team) or []) if isinstance(x, int)]
    ys = [y for y in (opp_row.get(key_opp)  or []) if isinstance(y, int)]
    n = min(len(xs), len(ys))
    w=l=d=0
    for i in range(n):
        if xs[i] == ys[i]:
            d += 1
        elif xs[i] > ys[i]:
            w += 1
        else:
            l += 1
    nd = w + l
    rate = (w / nd) if nd > 0 else 0.0
    return w, l, d, nd, rate

def split_win_rates(team_row: dict, opp_row: dict, key_team: str, key_opp: str) -> Tuple[Tuple[int,int,int,int,float], Tuple[int,int,int,int,float]]:
    xs  = [x for x in (team_row.get(key_team) or []) if isinstance(x, int)]
    ys  = [y for y in (opp_row.get(key_opp)  or []) if isinstance(y, int)]
    loc = [s for s in (team_row.get("locations_last_n") or [])]
    n = min(len(xs), len(ys), len(loc))
    hW=hL=hD=hN=0
    aW=aL=aD=aN=0
    for i in range(n):
        side = (loc[i] or "").lower()
        a_val, b_val = xs[i], ys[i]
        if a_val == b_val:
            if side == "home": hD += 1
            elif side == "away": aD += 1
            continue
        win = a_val > b_val
        if side == "home":
            if win: hW += 1
            else:   hL += 1
        elif side == "away":
            if win: aW += 1
            else:   aL += 1
    hN = hW + hL
    aN = aW + aL
    hRate = (hW / hN) if hN > 0 else 0.0
    aRate = (aW / aN) if aN > 0 else 0.0
    return (hW,hL,hD,hN,hRate), (aW,aL,aD,aN,aRate)

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
    keys = [
        "most corners", "corner match bet", "corners match bet",
        "team with most corners", "which team will have the most corners",
        "corners - most", "corners most"
    ]
    return any(k in s for k in keys) or (("corner" in s or "corners" in s) and "most" in s)

def extract_most_corners_prices(rows: List[dict]) -> Tuple[Optional[float], Optional[float], Optional[float]]:
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

            # find rows
            home_t = away_t = home_o = away_o = None
            for row in ts_idx.values():
                if team_names_match(home_nm, row.get("team_name","")): home_t = row
                if team_names_match(away_nm, row.get("team_name","")): away_t = row
            for row in opp_idx.values():
                if team_names_match(home_nm, row.get("team_name","")): home_o = row
                if team_names_match(away_nm, row.get("team_name","")): away_o = row
            if not (home_t and away_t and home_o and away_o):
                continue

            # overall rates
            _,_,_,hN_all,hRate_all = overall_win_rate(home_t, home_o, "corners_last_n", "opp_corners_last_n")
            _,_,_,aN_all,aRate_all = overall_win_rate(away_t, away_o, "corners_last_n", "opp_corners_last_n")

            # split rates (used for decision)
            (hW,hL,hD,hN,hRate), _ = split_win_rates(home_t, home_o, "corners_last_n", "opp_corners_last_n")
            _, (aW,aL,aD,aN,aRate) = split_win_rates(away_t, away_o, "corners_last_n", "opp_corners_last_n")

            # choose side by thresholds (split)
            pick_side = None
            if (aRate >= MIN_SIDE_RATE) and ((aRate - hRate) >= MIN_GAP):
                pick_side = "away"
            if (hRate >= MIN_SIDE_RATE) and ((hRate - aRate) >= MIN_GAP):
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

            chosen_rate = hRate if pick_side=="home" else aRate
            other_rate  = aRate if pick_side=="home" else hRate
            gap = max(0.0, chosen_rate - other_rate)

            candidates.append({
                "fixture": name,
                "side": pick_side,
                "price": float(price),
                "chosen_rate": chosen_rate,
                "gap": gap,
                "home_rate_all": hRate_all, "home_n_all": hN_all,
                "home_rate_split": hRate,   "home_n_split": hN,
                "away_rate_all": aRate_all, "away_n_all": aN_all,
                "away_rate_split": aRate,   "away_n_split": aN,
            })

        # ---- sort by likelihood
        if candidates:
            candidates.sort(
                key=lambda r: (-r["chosen_rate"], -r["gap"], -r["price"], r["fixture"])
            )

            any_out = True
            print(f"League {lid}: {len(candidates)} candidates (ranked by likelihood)")
            for r in candidates:
                side_txt = "HOME" if r["side"]=="home" else "AWAY"
                print(f"{r['fixture']} — Most Corners: {side_txt} @ {r['price']:.2f}")
                print(
                    "  Home win rate: "
                    f"{r['home_rate_all']*100:.1f}% overall | {r['home_rate_split']*100:.1f}% home (n={r['home_n_split']})"
                    " | Away win rate: "
                    f"{r['away_rate_all']*100:.1f}% overall | {r['away_rate_split']*100:.1f}% away (n={r['away_n_split']})"
                )
            print("")
        else:
            print(f"League {lid}: no candidates")

    if not any_out:
        pass

if __name__ == "__main__":
    main()
