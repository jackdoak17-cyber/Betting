# ================= scripts/value_bets/corners_match_bet.py =================
#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Value — Corners Match Bet (Bet365), ranked by side-specific corner win rate.

Reads:
  - Fixtures:            data/fixtures/{league_id}.json
  - Team winrates JSON:  data/team_winrates/by_league/{league_id}.json  (built by build-team-winrates.yml)
  - Bet365 odds:         data/odds/b365/{league_id}.json  and/or  data/odds/b365/fixtures/{fixture_id}.json

Output:
  - data/value_bets/corners_match_bet.txt   (also printed to stdout)

Selection (per upcoming fixture, within WINDOW_DAYS):
  - For HOME pick: use home team 'corners_home' win_rate vs away team 'corners_away'
  - For AWAY pick: use away team 'corners_away' win_rate vs home team 'corners_home'
  - Require chosen side's split rate >= MIN_SIDE_RATE
  - Require opposing side's split AND overall rates <= MAX_OPP_RATE
  - Require chosen side price >= MIN_DEC_PRICE
  - Optional gap filter: chosen_split - other_split >= MIN_GAP (can set to 0)
  - Minimum samples for chosen split: MIN_SIDE_N (default 3)
  - If split samples are 0, optionally fallback to overall (ALLOW_OVERALL_FALLBACK_FOR_SPLIT=1)

Ranking (global across all leagues):
  1) chosen split win rate (desc)
  2) gap vs other split (desc)
  3) price (desc)
  4) fixture name (asc)
"""

import os, json, re, unicodedata, datetime as dt
from pathlib import Path
from typing import Dict, List, Tuple, Optional

# --------- IO locations ---------
ROOT = Path(".")
FIX_DIR   = ROOT / "data" / "fixtures"
WR_DIR    = ROOT / "data" / "team_winrates" / "by_league"
ODDS_LDIR = ROOT / "data" / "odds" / "b365"
ODDS_FDIR = ODDS_LDIR / "fixtures"
OUT_DIR   = ROOT / "data" / "value_bets"; OUT_DIR.mkdir(parents=True, exist_ok=True)
OUT_FILE  = OUT_DIR / "corners_match_bet.txt"

# --------- Tuning via ENV ---------
LEAGUE_IDS  = [int(x) for x in os.getenv("LEAGUE_IDS", "").split(",") if x.strip()] or None
WINDOW_DAYS = int(os.getenv("WINDOW_DAYS", "7"))

MIN_DEC_PRICE = float(os.getenv("MIN_DEC_PRICE", "1.70"))
MIN_SIDE_RATE = float(os.getenv("MIN_SIDE_RATE", "0.55"))
MAX_OPP_RATE  = float(os.getenv("MAX_OPP_RATE", "0.50"))
MIN_GAP       = float(os.getenv("MIN_GAP", "0.15"))
MIN_SIDE_N    = int(os.getenv("MIN_SIDE_N", "3"))

# If a split sequence is empty (n=0), allow falling back to the team's overall rate?
ALLOW_OVERALL_FALLBACK_FOR_SPLIT = os.getenv("ALLOW_OVERALL_FALLBACK_FOR_SPLIT", "1") in ("1","true","TRUE","yes","YES")

# --------- String helpers ---------
def strip_accents(s: str) -> str:
    return ''.join(c for c in unicodedata.normalize('NFD', s or '') if unicodedata.category(c) != 'Mn')

def norm(s: str) -> str:
    s = strip_accents(s or "").lower()
    s = re.sub(r"[^a-z0-9\s\.-]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s

def fmt_pct(x: Optional[float]) -> str:
    return f"{x*100:.1f}%" if isinstance(x, float) else "n/a"

# --------- Time window ---------
def within_window(starting_at: str, days: int) -> bool:
    if not days: return True
    try:
        t = dt.datetime.strptime(starting_at[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=dt.timezone.utc)
    except Exception:
        return True
    now = dt.datetime.now(dt.timezone.utc)
    return now <= t <= (now + dt.timedelta(days=days))

# --------- Odds parsing ---------
BOOKMAKER_B365 = 2

CORNER_MATCH_ALIASES = {
    "most corners", "corners match bet", "corner match bet", "team with most corners",
    "corners - match bet", "corners - match betting", "which team will have more corners",
    "most corners (match)", "corners match result"
}

def is_corner_match_market(desc: str) -> bool:
    s = norm(desc)
    if not s: return False
    if any(x in s for x in ("handicap", "asian", "race", "total", "over", "under")):
        return False
    return any(alias in s for alias in CORNER_MATCH_ALIASES) or ("most" in s and "corner" in s)

def label_to_side(label: str) -> Optional[str]:
    s = (label or "").strip().lower()
    if s in {"1","home","home team"}: return "home"
    if s in {"2","away","away team"}: return "away"
    return None

def best_corner_price_for_side(rows: List[dict], side: str) -> Optional[float]:
    best = None
    for r in rows or []:
        if int(r.get("bookmaker_id") or 0) != BOOKMAKER_B365:
            continue
        if not is_corner_match_market(r.get("market_description") or ""):
            continue
        if label_to_side(r.get("label") or "") != side:
            continue
        try:
            price = float(r.get("value"))
        except Exception:
            continue
        if best is None or price > best:
            best = price
    return best

def odds_rows_for_fixture(lid: int, fid: int) -> List[dict]:
    # Prefer per-fixture odds if present
    pf = ODDS_FDIR / f"{fid}.json"
    if pf.exists():
        try:
            blob = json.loads(pf.read_text(encoding="utf-8"))
            return blob.get("odds") or []
        except Exception:
            pass
    # Fallback to per-league odds bundle
    pl = ODDS_LDIR / f"{lid}.json"
    if pl.exists():
        try:
            blob = json.loads(pl.read_text(encoding="utf-8"))
            for f in (blob.get("fixtures") or []):
                if int(f.get("fixture_id") or 0) == int(fid):
                    return f.get("odds") or []
        except Exception:
            pass
    return []

# --------- Winrate accessors ---------
def read_json(p: Path) -> dict:
    try: return json.loads(p.read_text(encoding="utf-8"))
    except Exception: return {}

def discover_league_ids() -> List[int]:
    ids = []
    for p in FIX_DIR.glob("*.json"):
        try: ids.append(int(p.stem))
        except: pass
    return sorted(set(ids))

def parse_fixture(fx: dict) -> Tuple[Optional[int], Optional[int], str, str, Optional[int]]:
    """Return (home_id, away_id, home_name, away_name, fixture_id)."""
    fid = fx.get("id") or fx.get("fixture_id")
    try: fid = int(fid)
    except Exception: fid = None

    home_nm = away_nm = None
    home_id = away_id = None

    parts = fx.get("participants")
    if isinstance(parts, list):
        for p in parts:
            nm = (p.get("name") or "").strip()
            pid = p.get("id") or p.get("team_id")
            try: pid = int(pid)
            except Exception: pid = None
            loc = ((p.get("meta") or {}).get("location") or p.get("location") or "").lower()
            if "home" in loc:
                home_nm, home_id = nm, pid
            elif "away" in loc:
                away_nm, away_id = nm, pid

    if not (home_nm and away_nm):
        name = fx.get("name") or ""
        for sep in (" vs ", " v ", " - "):
            if sep in name:
                a, b = name.split(sep, 1)
                home_nm = home_nm or a.strip()
                away_nm = away_nm or b.strip()
                break

    return home_id, away_id, home_nm or "", away_nm or "", fid

def wr_index_for_league(lid: int) -> Tuple[Dict[int, dict], Dict[str, dict]]:
    blob = read_json(WR_DIR / f"{lid}.json")
    by_id: Dict[int, dict] = {}
    by_name: Dict[str, dict] = {}
    for t in (blob.get("teams") or []):
        tid = t.get("team_id")
        nm  = t.get("team_name") or ""
        if isinstance(tid, int):
            by_id[tid] = t
        if nm:
            by_name[norm(nm)] = t
    return by_id, by_name

def get_cat_rates(team_row: dict, cat: str) -> Tuple[Optional[float], int]:
    """Return (win_rate, n) for a category name under team_row['categories']."""
    cats = (team_row or {}).get("categories") or {}
    c = cats.get(cat) or {}
    r = (c.get("rates") or {})
    wr = r.get("win_rate")
    n  = int(r.get("n") or 0)
    try:
        wr = float(wr) if wr is not None else None
    except Exception:
        wr = None
    return wr, n

def side_rates(team_row: dict, is_home: bool) -> Tuple[Optional[float], int, Optional[float], int]:
    """
    Returns:
      (overall_wr, overall_n, split_wr, split_n)
      where split is corners_home if is_home else corners_away.
    """
    overall_wr, overall_n = get_cat_rates(team_row, "corners")
    split_cat = "corners_home" if is_home else "corners_away"
    split_wr, split_n = get_cat_rates(team_row, split_cat)

    if split_n == 0 and ALLOW_OVERALL_FALLBACK_FOR_SPLIT:
        # Fall back to overall rate/samples if split is empty
        split_wr, split_n = overall_wr, overall_n

    return overall_wr, overall_n, split_wr, split_n

# --------- Main ---------
def main():
    generated_at = dt.datetime.utcnow().isoformat()
    league_ids = LEAGUE_IDS or discover_league_ids()

    picks: List[dict] = []

    for lid in league_ids:
        fx_blob = read_json(FIX_DIR / f"{lid}.json")
        if not fx_blob: 
            continue
        wr_by_id, wr_by_name = wr_index_for_league(lid)
        fixtures = fx_blob.get("fixtures") or []

        for fx in fixtures:
            starting_at = fx.get("starting_at") or ""
            if WINDOW_DAYS and not within_window(starting_at, WINDOW_DAYS):
                continue

            home_id, away_id, home_nm, away_nm, fid = parse_fixture(fx)
            if not fid: 
                continue

            # Find team rows (prefer by id, fallback by name)
            h = wr_by_id.get(home_id) or wr_by_name.get(norm(home_nm))
            a = wr_by_id.get(away_id) or wr_by_name.get(norm(away_nm))
            if not (h and a):
                continue

            # Rates
            h_all, h_all_n, h_split, h_split_n = side_rates(h, is_home=True)
            a_all, a_all_n, a_split, a_split_n = side_rates(a, is_home=False)

            # Chosen side checks (HOME)
            def consider_home() -> Optional[dict]:
                if h_split is None or a_split is None: 
                    return None
                if h_split_n < MIN_SIDE_N: 
                    return None
                if h_split < MIN_SIDE_RATE: 
                    return None
                if (a_split is not None and a_split > MAX_OPP_RATE): 
                    return None
                if (a_all is not None and a_all > MAX_OPP_RATE): 
                    return None
                if (h_split - a_split) < MIN_GAP:
                    return None
                rows = odds_rows_for_fixture(lid, fid)
                price = best_corner_price_for_side(rows, "home")
                if price is None or price < MIN_DEC_PRICE:
                    return None
                return {
                    "fixture": fx.get("name") or f"{home_nm} vs {away_nm}",
                    "side": "HOME",
                    "price": float(price),
                    "chosen_rate": float(h_split),
                    "gap": float(max(0.0, h_split - a_split)),
                    "home_overall": h_all, "home_split": h_split, "home_n": h_split_n,
                    "away_overall": a_all, "away_split": a_split, "away_n": a_split_n,
                }

            # Chosen side checks (AWAY)
            def consider_away() -> Optional[dict]:
                if a_split is None or h_split is None: 
                    return None
                if a_split_n < MIN_SIDE_N: 
                    return None
                if a_split < MIN_SIDE_RATE: 
                    return None
                if (h_split is not None and h_split > MAX_OPP_RATE): 
                    return None
                if (h_all is not None and h_all > MAX_OPP_RATE): 
                    return None
                if (a_split - h_split) < MIN_GAP:
                    return None
                rows = odds_rows_for_fixture(lid, fid)
                price = best_corner_price_for_side(rows, "away")
                if price is None or price < MIN_DEC_PRICE:
                    return None
                return {
                    "fixture": fx.get("name") or f"{home_nm} vs {away_nm}",
                    "side": "AWAY",
                    "price": float(price),
                    "chosen_rate": float(a_split),
                    "gap": float(max(0.0, a_split - h_split)),
                    "home_overall": h_all, "home_split": h_split, "home_n": h_split_n,
                    "away_overall": a_all, "away_split": a_split, "away_n": a_split_n,
                }

            cand_h = consider_home()
            cand_a = consider_away()
            if cand_h: picks.append(cand_h)
            if cand_a: picks.append(cand_a)

    # Rank globally
    picks.sort(key=lambda r: (-r["chosen_rate"], -r["gap"], -r["price"], r["fixture"]))

    # Render
    lines = []
    lines.append(f"Generated at (UTC): {generated_at}")
    lines.append("")
    lines.append("Most Corners — candidates (ranked by likelihood)")
    lines.append("")
    if not picks:
        lines.append("No candidates passed the filters.")
    else:
        for r in picks:
            lines.append(f"{r['fixture']} — Most Corners: {r['side']} @ {r['price']:.2f}")
            lines.append(
                f"  Home: {fmt_pct(r['home_overall'])} overall | {fmt_pct(r['home_split'])} home (n={r['home_n']}) | "
                f"Away: {fmt_pct(r['away_overall'])} overall | {fmt_pct(r['away_split'])} away (n={r['away_n']})"
            )

    OUT_FILE.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
