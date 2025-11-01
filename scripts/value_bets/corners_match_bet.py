#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Value — Corners Match Bet (Bet365)
- Uses fixture-id aligned corner WIN RATES computed from:
    data/team_stats/by_league/{league_id}.json
    data/team_opponent_stats/by_league/{league_id}.json
  (expects new fields: `locations_last_n` aligned to each stat series)

- For each upcoming fixture (within WINDOW_DAYS), we compute:
    * Home team's corner win rates: overall and home-only
    * Away team's corner win rates: overall and away-only
  Win = team_corners > opp_corners; Draws are EXCLUDED from n.

- Candidate rules:
    HOME bet if:  home_home_rate ≥ MIN_SIDE_RATE  AND away_away_rate ≤ MAX_OPP_RATE
    AWAY bet if:  away_away_rate ≥ MIN_SIDE_RATE  AND home_home_rate ≤ MAX_OPP_RATE
  (Require at least MIN_SIDE_N samples for the side-specific rate; else skip)

- Odds:
    Reads Bet365 "Most Corners"/"Corners Match Bet" prices from:
      data/odds/b365/fixtures/{fixture_id}.json  (preferred)
      or data/odds/b365/{league_id}.json         (fallback)
    Keeps only sides with price ≥ MIN_DEC_PRICE.

Output (ranked by estimated likelihood = chosen_side_rate):
    <Fixture> — Most Corners: <HOME|AWAY> @ <price>
      Home: <overall%> overall | <home%> home (n=<Hn>) | Away: <overall%> overall | <away%> away (n=<An>)
"""

import os, json, math, datetime as dt, unicodedata, re
from pathlib import Path
from typing import Dict, List, Optional, Tuple

ROOT = Path(".")
FIX_DIR   = ROOT / "data" / "fixtures"
TS_DIR    = ROOT / "data" / "team_stats" / "by_league"
OPP_DIR   = ROOT / "data" / "team_opponent_stats" / "by_league"
ODDS_FIX  = ROOT / "data" / "odds" / "b365" / "fixtures"
ODDS_LIG  = ROOT / "data" / "odds" / "b365"
OUT_DIR   = ROOT / "data" / "value_bets"; OUT_DIR.mkdir(parents=True, exist_ok=True)
OUT_FILE  = OUT_DIR / "corners_match_bet.txt"

# --------- ENV TUNING ----------
LEAGUE_IDS     = [int(x) for x in os.getenv("LEAGUE_IDS", "").split(",") if x.strip()] or None
WINDOW_DAYS    = int(os.getenv("WINDOW_DAYS", "7"))
MIN_DEC_PRICE  = float(os.getenv("MIN_DEC_PRICE", "1.70"))
MIN_SIDE_RATE  = float(os.getenv("MIN_SIDE_RATE", "0.55"))   # e.g., 55%+
MAX_OPP_RATE   = float(os.getenv("MAX_OPP_RATE", "0.45"))    # e.g., ≤45%
MIN_SIDE_N     = int(os.getenv("MIN_SIDE_N", "4"))           # min samples for H/A split

# --------- UTILS ----------
def read_json(p: Path) -> dict:
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}

def strip_accents(s: str) -> str:
    return ''.join(c for c in unicodedata.normalize('NFD', s or '') if unicodedata.category(c) != 'Mn')
def norm(s: str) -> str:
    s = strip_accents(s or "").lower()
    s = re.sub(r"[^a-z0-9\s\.-]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s

def within_window(starting_at: str, days: int) -> bool:
    if not days: return True
    try:
        dt_utc = dt.datetime.strptime(starting_at, "%Y-%m-%d %H:%M:%S").replace(tzinfo=dt.timezone.utc)
    except Exception:
        return True
    now = dt.datetime.now(dt.timezone.utc)
    return now <= dt_utc <= (now + dt.timedelta(days=days))

def parse_fixture_teams(fx: dict) -> Tuple[Optional[str], Optional[str], Optional[int], Optional[int]]:
    """
    Return (home_name, away_name, home_id, away_id) using participants meta.location when present.
    """
    parts = fx.get("participants") or []
    home_nm = away_nm = None
    home_id = away_id = None
    if isinstance(parts, list):
        for p in parts:
            nm = p.get("name") or ""
            pid = p.get("id") or p.get("team_id")
            loc = ((p.get("meta") or {}).get("location") or "").lower()
            if "home" in loc:
                home_nm, home_id = nm, (int(pid) if isinstance(pid, int) or (isinstance(pid, str) and pid.isdigit()) else None)
            elif "away" in loc:
                away_nm, away_id = nm, (int(pid) if isinstance(pid, int) or (isinstance(pid, str) and pid.isdigit()) else None)
    # Fallback to name splitting if needed
    if not (home_nm and away_nm):
        name = fx.get("name") or ""
        for sep in (" vs ", " v ", " - ", " VS ", " Vs "):
            if sep in name:
                a, b = name.split(sep, 1)
                # heuristically treat left as home if locations missing
                home_nm = home_nm or a.strip()
                away_nm = away_nm or b.strip()
                break
    return home_nm, away_nm, home_id, away_id

# --------- LOADERS ----------
def discover_league_ids() -> List[int]:
    ids = []
    for p in FIX_DIR.glob("*.json"):
        try: ids.append(int(p.stem))
        except: pass
    return sorted(set(ids))

def index_by_team_id(blob: dict) -> Dict[int, dict]:
    m = {}
    for t in (blob.get("teams") or []):
        tid = t.get("team_id")
        if isinstance(tid, int):
            m[tid] = t
    return m

def build_corner_maps(team_row: dict, opp_row: dict) -> Tuple[Dict[int,int], Dict[int,int], Dict[int,str]]:
    """
    Returns:
      tc[fid] = team corners
      oc[fid] = opponent corners (from same fixture)
      loc[fid] = "home"/"away"/"unknown" (where THIS team played)
    Only fixtures present in BOTH rows are meaningful; we will intersect on fid.
    """
    tc, oc, loc = {}, {}, {}

    fids_t = list(map(int, team_row.get("fixture_ids") or []))
    vals_t = list(map(int, team_row.get("corners_last_n") or []))
    locs_t = list(map(str, team_row.get("locations_last_n") or []))

    fids_o = list(map(int, opp_row.get("fixture_ids") or []))
    vals_o = list(map(int, opp_row.get("opp_corners_last_n") or []))

    for i, fid in enumerate(fids_t):
        if i < len(vals_t):
            tc[fid] = int(vals_t[i])
        if i < len(locs_t):
            loc[fid] = locs_t[i] if locs_t[i] in ("home","away") else "unknown"

    for j, fid in enumerate(fids_o):
        if j < len(vals_o):
            oc[fid] = int(vals_o[j])

    return tc, oc, loc

def corner_win_rates(team_row: dict, opp_row: dict) -> dict:
    """
    Compute win rates by comparing team corners vs opp corners per fixture_id (draws excluded).
    Returns dict:
      {
        "overall": (wins, n),
        "home":    (wins, n),
        "away":    (wins, n)
      }
    """
    tc, oc, loc = build_corner_maps(team_row, opp_row)
    inter = [fid for fid in tc.keys() if fid in oc]

    w_all = n_all = 0
    w_h = n_h = 0
    w_a = n_a = 0

    for fid in inter:
        a = tc[fid]; b = oc[fid]
        if a == b:
            continue  # exclude draws from n
        win = 1 if a > b else 0
        w_all += win; n_all += 1
        where = loc.get(fid, "unknown")
        if where == "home":
            w_h += win; n_h += 1
        elif where == "away":
            w_a += win; n_a += 1
        else:
            # unknown location contributes to overall only
            pass

    return {
        "overall": (w_all, n_all),
        "home":    (w_h,  n_h),
        "away":    (w_a,  n_a),
    }

# --------- ODDS PARSING ----------
CORNER_MATCH_ALIASES = {
    "most corners", "corners match bet", "corner match bet", "most corner", "team with most corners"
}

def is_corner_match_market(desc: str) -> bool:
    s = norm(desc)
    return any(alias in s for alias in CORNER_MATCH_ALIASES)

def label_to_side(label: str) -> Optional[str]:
    s = (label or "").strip().lower()
    if s in {"1","home","home team"}: return "home"
    if s in {"2","away","away team"}: return "away"
    return None

def best_corner_price_for_side(rows: List[dict], side: str) -> Optional[float]:
    best = None
    for r in rows or []:
        if int(r.get("bookmaker_id") or 0) != 2:  # Bet365 only
            continue
        if not is_corner_match_market(r.get("market_description") or ""):
            continue
        lab = label_to_side(r.get("label") or "")
        if lab != side:
            continue
        v = r.get("value")
        try:
            price = float(v)
        except Exception:
            continue
        if (best is None) or (price > best):
            best = price
    return best

def odds_rows_for_fixture(lid: int, fid: int) -> List[dict]:
    # 1) fixture-level
    fpath = ODDS_FIX / f"{fid}.json"
    if fpath.exists():
        j = read_json(fpath)
        return j.get("odds") or []
    # 2) league-level
    lpath = ODDS_LIG / f"{lid}.json"
    if lpath.exists():
        blob = read_json(lpath)
        for f in (blob.get("fixtures") or []):
            if int(f.get("fixture_id") or 0) == int(fid):
                return f.get("odds") or []
    return []

# --------- MAIN ----------
def pct(w, n) -> Optional[float]:
    if not n: return None
    return w / n

def fmt_pct(x: Optional[float]) -> str:
    return f"{x*100:.1f}%" if isinstance(x, float) else "n/a"

def main():
    generated_at = dt.datetime.utcnow().isoformat()
    league_ids = LEAGUE_IDS or discover_league_ids()

    lines = []
    lines.append(f"Generated at (UTC): {generated_at}")
    lines.append("")
    lines.append("Most Corners — candidates (ranked by likelihood)")
    lines.append("")

    all_candidates = []

    for lid in league_ids:
        fx_blob = read_json(FIX_DIR / f"{lid}.json")
        fixtures = fx_blob.get("fixtures") or []
        ts_blob  = read_json(TS_DIR / f"{lid}.json")
        opp_blob = read_json(OPP_DIR / f"{lid}.json")
        if not ((ts_blob.get("teams")) and (opp_blob.get("teams"))):
            continue

        ts_idx  = index_by_team_id(ts_blob)
        opp_idx = index_by_team_id(opp_blob)

        # build quick access by team_id
        def rates_for_team(tid: int) -> Optional[dict]:
            ts = ts_idx.get(tid); op = opp_idx.get(tid)
            if not (ts and op): return None
            return corner_win_rates(ts, op)

        for fx in fixtures:
            starting_at = fx.get("starting_at") or ""
            if WINDOW_DAYS and not within_window(starting_at, WINDOW_DAYS):
                continue

            home_nm, away_nm, home_id, away_id = parse_fixture_teams(fx)
            if not (home_id and away_id):
                # fall back: try to resolve by matching names to ts_idx entries
                continue

            r_home = rates_for_team(home_id)
            r_away = rates_for_team(away_id)
            if not (r_home and r_away):
                continue

            # Extract overall + split rates
            h_all = pct(*r_home["overall"]); h_h = pct(*r_home["home"]); h_h_n = r_home["home"][1]
            a_all = pct(*r_away["overall"]); a_a = pct(*r_away["away"]); a_a_n = r_away["away"][1]

            # Require side-specific samples
            h_ok = (h_h is not None) and (h_h_n >= MIN_SIDE_N)
            a_ok = (a_a is not None) and (a_a_n >= MIN_SIDE_N)

            # Evaluate HOME candidate
            if h_ok and a_ok and (h_h >= MIN_SIDE_RATE) and (a_a <= MAX_OPP_RATE):
                rows = odds_rows_for_fixture(lid, int(fx.get("id") or fx.get("fixture_id") or 0))
                price = best_corner_price_for_side(rows, "home")
                if (price is not None) and (price >= MIN_DEC_PRICE):
                    all_candidates.append({
                        "likelihood": float(h_h),
                        "lid": lid,
                        "fixture": fx.get("name") or f"{home_nm} vs {away_nm}",
                        "side": "HOME",
                        "price": float(price),
                        "home_overall": h_all, "home_home": h_h, "home_home_n": h_h_n,
                        "away_overall": a_all, "away_away": a_a, "away_away_n": a_a_n,
                    })

            # Evaluate AWAY candidate
            if h_ok and a_ok and (a_a >= MIN_SIDE_RATE) and (h_h <= MAX_OPP_RATE):
                rows = odds_rows_for_fixture(lid, int(fx.get("id") or fx.get("fixture_id") or 0))
                price = best_corner_price_for_side(rows, "away")
                if (price is not None) and (price >= MIN_DEC_PRICE):
                    all_candidates.append({
                        "likelihood": float(a_a),
                        "lid": lid,
                        "fixture": fx.get("name") or f"{home_nm} vs {away_nm}",
                        "side": "AWAY",
                        "price": float(price),
                        "home_overall": h_all, "home_home": h_h, "home_home_n": h_h_n,
                        "away_overall": a_all, "away_away": a_a, "away_away_n": a_a_n,
                    })

    # Rank globally (not grouped by league)
    all_candidates.sort(key=lambda r: (-r["likelihood"], r["fixture"], r["side"]))

    if not all_candidates:
        lines.append("No candidates passed the filters.\n")
    else:
        for r in all_candidates:
            lines.append(f"{r['fixture']} — Most Corners: {r['side']} @ {r['price']:.2f}")
            lines.append(f"  Home: {fmt_pct(r['home_overall'])} overall | {fmt_pct(r['home_home'])} home (n={r['home_home_n']}) | "
                         f"Away: {fmt_pct(r['away_overall'])} overall | {fmt_pct(r['away_away'])} away (n={r['away_away_n']})")

    OUT_FILE.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    print("\n".join(lines))

if __name__ == "__main__":
    main()
