# ================= scripts/value_bets/corners_match_bet.py =================
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Most Corners — global picks (ranked), using team_winrates data.
Draws are treated as LOSSES when computing win rates.

Reads (local only):
- Fixtures:                 data/fixtures/{league_id}.json
- Team winrates:            data/team_winrates/by_league/{league_id}.json
- Bet365 odds (per-league): data/odds/b365/{league_id}.json

Selection logic (per fixture, within WINDOW_DAYS):
- Consider HOME if home_home_rate >= MIN_SIDE_RATE and (home_home_rate - away_away_rate) >= MIN_GAP
- Consider AWAY if away_away_rate >= MIN_SIDE_RATE and (away_away_rate - home_home_rate) >= MIN_GAP
- Price for chosen side must be >= MIN_DEC_PRICE
- Opponent guardrail: opponent overall < MAX_OPP_RATE and opponent split < MAX_OPP_RATE

Ranking (global):
- chosen split win rate (desc), then gap (desc), then price (desc), fixture name (asc)

Env (optional):
- LEAGUE_IDS      CSV (default: discover from fixtures)
- WINDOW_DAYS     default 7
- MIN_DEC_PRICE   default 1.70
- MIN_SIDE_RATE   default 0.55
- MIN_GAP         default 0.15
- MAX_OPP_RATE    default 0.50
- OUT_PATH        file to write (default data/value_bets/corners_match_bet.txt)
"""

import os, re, json, datetime as dt, unicodedata
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# --- IO roots ---
ROOT       = Path(".")
FIX_DIR    = ROOT / "data" / "fixtures"
WINR_DIR   = ROOT / "data" / "team_winrates" / "by_league"
ODDS_DIR   = ROOT / "data" / "odds" / "b365"
OUT_PATH   = Path(os.getenv("OUT_PATH", "data/value_bets/corners_match_bet.txt"))
OUT_PATH.parent.mkdir(parents=True, exist_ok=True)

# --- thresholds ---
WINDOW_DAYS   = int(os.getenv("WINDOW_DAYS", "7"))
MIN_DEC_PRICE = float(os.getenv("MIN_DEC_PRICE", "1.70"))
MIN_SIDE_RATE = float(os.getenv("MIN_SIDE_RATE", "0.55"))
MIN_GAP       = float(os.getenv("MIN_GAP", "0.15"))
MAX_OPP_RATE  = float(os.getenv("MAX_OPP_RATE", "0.50"))

# --- string utils / matching ---
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

# --- helpers ---
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

def index_winrates(blob: dict) -> Dict[str, dict]:
    m: Dict[str, dict] = {}
    for t in (blob.get("teams") or []):
        nm = t.get("team_name")
        if not nm: continue
        m[norm(nm)] = t
    return m

# --- compute win rates from sequences (D counts as LOSS) ---
def _rate_from_seq(cat: dict):
    """
    Returns (wins, losses_incl_draws, n, rate) with draws counted as losses.
    rate = wins / n  with n = W+L+D (i.e., len(sequence))
    """
    seq = [str(s).strip().upper() for s in (cat or {}).get("sequence", [])]
    w = sum(s == "W" for s in seq)
    l = sum(s == "L" for s in seq)
    d = sum(s == "D" for s in seq)
    n = w + l + d
    losses_incl_draws = l + d
    rate = (w / n) if n else 0.0
    return w, losses_incl_draws, n, rate

def _corner_rates_from_team_row(team_row: dict):
    """
    Pull overall/home/away corner win rates from team_winrates JSON,
    recomputing from `sequence` so that draws count as losses.
    """
    cats = (team_row or {}).get("categories", {})
    _, _, n_overall, r_overall = _rate_from_seq(cats.get("corners", {}))
    _, _, n_home,    r_home    = _rate_from_seq(cats.get("corners_home", {}))
    _, _, n_away,    r_away    = _rate_from_seq(cats.get("corners_away", {}))
    return {
        "overall_rate": r_overall,
        "home_rate": r_home,  "home_n": n_home,
        "away_rate": r_away,  "away_n": n_away,
    }

# --- odds parsing ---
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

# --- main ---
def main():
    # league list
    if os.getenv("LEAGUE_IDS"):
        league_ids = [int(x) for x in os.getenv("LEAGUE_IDS").split(",") if x.strip()]
    else:
        league_ids = discover_league_ids()

    picks = []

    for lid in league_ids:
        fx_path   = FIX_DIR  / f"{lid}.json"
        wr_path   = WINR_DIR / f"{lid}.json"
        odds_path = ODDS_DIR / f"{lid}.json"
        if not (fx_path.exists() and wr_path.exists() and odds_path.exists()):
            continue

        fixtures  = load_json(fx_path).get("fixtures") or []
        winr_idx  = index_winrates(load_json(wr_path))
        odds_blob = load_json(odds_path)
        odds_by_fixture = {int(f.get("fixture_id")): f for f in (odds_blob.get("fixtures") or []) if isinstance(f.get("fixture_id"), int)}

        for fx in fixtures:
            if not isinstance(fx, dict): 
                continue
            fid = fx.get("id") or fx.get("fixture_id")
            name = fx.get("name") or ""
            starting_at = fx.get("starting_at") or ""
            if WINDOW_DAYS and not within_window(starting_at, WINDOW_DAYS):
                continue

            home_nm, away_nm = parse_fixture_teams(name)
            if not home_nm or not away_nm:
                continue

            # find team rows
            def find_team_row(team_name: str) -> Optional[dict]:
                row = winr_idx.get(norm(team_name))
                if row:
                    return row
                # fuzzy fallback
                for r in winr_idx.values():
                    if team_names_match(team_name, r.get("team_name","")):
                        return r
                return None

            home_t = find_team_row(home_nm)
            away_t = find_team_row(away_nm)
            if not (home_t and away_t):
                continue

            # compute rates (D as loss)
            home_rates = _corner_rates_from_team_row(home_t)
            away_rates = _corner_rates_from_team_row(away_t)

            # overall (venue-agnostic) for both
            hRate_all = home_rates["overall_rate"]
            aRate_all = away_rates["overall_rate"]

            # split (home team at home, away team away)
            hRate = home_rates["home_rate"]; hN = home_rates["home_n"]
            aRate = away_rates["away_rate"]; aN = away_rates["away_n"]

            # choose side by thresholds/gap
            pick_side = None
            if (hRate >= MIN_SIDE_RATE) and ((hRate - aRate) >= MIN_GAP):
                pick_side = "home"
            if (aRate >= MIN_SIDE_RATE) and ((aRate - hRate) >= MIN_GAP):
                # prefer side with larger advantage
                if (pick_side is None) or ((aRate - hRate) > (hRate - aRate)):
                    pick_side = "away"
            if not pick_side:
                continue

            # opponent guardrail
            other_overall = aRate_all if pick_side == "home" else hRate_all
            other_split   = aRate      if pick_side == "home" else hRate
            if (other_overall >= MAX_OPP_RATE) or (other_split >= MAX_OPP_RATE):
                continue

            # odds
            odds_fx = odds_by_fixture.get(int(fid)) if isinstance(fid, int) else None
            if not odds_fx:
                continue
            home_p, away_p, _draw_p = extract_most_corners_prices(odds_fx.get("odds") or [])
            price = home_p if pick_side == "home" else away_p
            if price is None or price < MIN_DEC_PRICE:
                continue

            chosen_rate = hRate if pick_side=="home" else aRate
            other_rate  = aRate if pick_side=="home" else hRate
            gap = max(0.0, chosen_rate - other_rate)

            picks.append({
                "fixture": name,
                "side": pick_side,
                "price": float(price),
                "chosen_rate": chosen_rate,
                "gap": gap,
                "hRate_all": hRate_all, "hRate": hRate, "hN": hN,
                "aRate_all": aRate_all, "aRate": aRate, "aN": aN,
            })

    # rank globally
    picks.sort(key=lambda r: (-r["chosen_rate"], -r["gap"], -r["price"], r["fixture"]))

    lines = []
    lines.append(f"Generated at (UTC): {dt.datetime.utcnow().isoformat(timespec='seconds')}")
    lines.append("")
    lines.append("Most Corners — candidates (ranked by likelihood)")
    if not picks:
        lines.append("(no candidates)")
    else:
        for r in picks:
            side_txt = "HOME" if r["side"] == "home" else "AWAY"
            lines.append(f"{r['fixture']} — Most Corners: {side_txt} @ {r['price']:.2f}")
            lines.append(
                f"  Home: {r['hRate_all']*100:.1f}% overall | {r['hRate']*100:.1f}% home (n={r['hN']}) | "
                f"Away: {r['aRate_all']*100:.1f}% overall | {r['aRate']*100:.1f}% away (n={r['aN']})"
            )

    out = "\n".join(lines).rstrip() + "\n"
    OUT_PATH.write_text(out, encoding="utf-8")
    print(out, end="")

if __name__ == "__main__":
    main()
