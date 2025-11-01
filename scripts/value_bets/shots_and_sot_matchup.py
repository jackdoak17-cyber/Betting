#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Flag high-likelihood matchups for:
  • Most Shots (shots_total)
  • Most Shots on Target (shots_on_target)
using team_winrates (overall + home/away sequences). Draws count as LOSSES.

Reads (local):
- Fixtures:                 data/fixtures/{league_id}.json
- Team winrates:            data/team_winrates/by_league/{league_id}.json
- OPTIONAL 1X2 odds:        data/odds/b365/{league_id}.json  (to filter big favourites/underdogs)

Selection per fixture (within WINDOW_DAYS):
- Consider HOME if: home_split >= MIN_SIDE_RATE and (home_split - away_split) >= MIN_GAP
- Consider AWAY if: away_split >= MIN_SIDE_RATE and (away_split - home_split) >= MIN_GAP
- If 1X2 odds exist, DROP pick if chosen team's 1X2 price:
    - <= FAVORITE_MAX_DECIMAL  (too short; likely poor price)
    - >= UNDERDOG_MAX_DECIMAL  (too large; user asked to drop > 4.0)
Ranking (global within each stat):
- chosen split rate (desc), then gap (desc), then fixture name (asc)

Env (optional):
- LEAGUE_IDS            CSV (default: discover from fixtures dir)
- WINDOW_DAYS           default 7
- MIN_SIDE_RATE         default 0.60
- MIN_GAP               default 0.15
- FAVORITE_MAX_DECIMAL  default 1.60
- UNDERDOG_MAX_DECIMAL  default 4.00
- OUT_PATH              default data/value_bets/shots_and_sot_matchup.txt
"""

import os, re, json, datetime as dt, unicodedata
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# --- IO roots ---
ROOT       = Path(".")
FIX_DIR    = ROOT / "data" / "fixtures"
WINR_DIR   = ROOT / "data" / "team_winrates" / "by_league"
ODDS_DIR   = ROOT / "data" / "odds" / "b365"
OUT_PATH   = Path(os.getenv("OUT_PATH", "data/value_bets/shots_and_sot_matchup.txt"))
OUT_PATH.parent.mkdir(parents=True, exist_ok=True)

# --- thresholds ---
WINDOW_DAYS           = int(os.getenv("WINDOW_DAYS", "7"))
MIN_SIDE_RATE         = float(os.getenv("MIN_SIDE_RATE", "0.60"))
MIN_GAP               = float(os.getenv("MIN_GAP", "0.15"))
FAVORITE_MAX_DECIMAL  = float(os.getenv("FAVORITE_MAX_DECIMAL", "1.60"))  # drop if <= this (too short fav)
UNDERDOG_MAX_DECIMAL  = float(os.getenv("UNDERDOG_MAX_DECIMAL", "4.00"))  # drop if >= this (too big a dog)

# --- text utils ---
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

def parse_fixture_teams(name: str) -> Tuple[str,str]:
    if not name: return "",""
    for sep in (" vs ", " v ", " - ", " VS ", " Vs "):
        if sep in name:
            a, b = name.split(sep, 1)
            return a.strip(), b.strip()
    return "",""

def within_window(starting_at: str, days: int) -> bool:
    if not starting_at: return True
    try:
        dt_utc = dt.datetime.strptime(starting_at[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=dt.timezone.utc)
    except Exception:
        return True
    now = dt.datetime.now(dt.timezone.utc)
    return now <= dt_utc <= (now + dt.timedelta(days=days))

# --- IO helpers ---
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
        if nm:
            m[norm(nm)] = t
    return m

# --- compute rate from sequence, counting draws as LOSSES ---
def _rate_from_seq(cat: dict):
    seq = [str(s).strip().upper() for s in (cat or {}).get("sequence", [])]
    w = sum(s == "W" for s in seq)
    l = sum(s == "L" for s in seq)
    d = sum(s == "D" for s in seq)
    n = w + l + d
    rate = (w / n) if n else 0.0
    return n, rate

def _get_rates(team_row: dict, key_base: str):
    """
    key_base ∈ {"shots_total", "shots_on_target"}
    returns dict with overall/home/away rates and sample sizes.
    """
    cats = (team_row or {}).get("categories", {})
    n_all,  r_all  = _rate_from_seq(cats.get(key_base, {}))
    n_home, r_home = _rate_from_seq(cats.get(f"{key_base}_home", {}))
    n_away, r_away = _rate_from_seq(cats.get(f"{key_base}_away", {}))
    return {
        "overall": r_all, "n_overall": n_all,
        "home":    r_home, "n_home": n_home,
        "away":    r_away, "n_away": n_away,
    }

# --- odds: parse 1X2 if present (optional filter) ---
def label_to_side(s: Optional[str]) -> Optional[str]:
    s = (s or "").strip().lower()
    if s in {"1","home","home (1)","team 1"}: return "home"
    if s in {"2","away","away (2)","team 2"}: return "away"
    if s in {"x","draw","tie"}: return "draw"
    return None

def is_match_result_market(md: str) -> bool:
    s = norm(md)
    if not s: return False
    keys = ["match result", "full time result", "result - 3-way", "1x2", "ft result"]
    return any(k in s for k in keys)

def extract_1x2_prices(odds_rows: List[dict]) -> Tuple[Optional[float], Optional[float], Optional[float]]:
    """Return (home_win_price, draw_price, away_win_price) if found; else Nones."""
    h = d = a = None
    for r in odds_rows or []:
        if not is_match_result_market(r.get("market_description","")):
            continue
        side = label_to_side(r.get("label"))
        try: price = float(r.get("value"))
        except Exception: continue
        if side == "home":
            h = price if (h is None or price > h) else h
        elif side == "away":
            a = price if (a is None or price > a) else a
        elif side == "draw":
            d = price if (d is None or price > d) else d
    return h, d, a

# --- core: evaluate one stat type across leagues ---
def evaluate_stat(stat_key: str, league_ids: List[int]) -> List[dict]:
    """
    stat_key: "shots_total" or "shots_on_target"
    Returns list of picks with:
      fixture, side, chosen_rate, gap, h_overall, h_split, h_n, a_overall, a_split, a_n
    """
    results = []

    for lid in league_ids:
        fx_path   = FIX_DIR  / f"{lid}.json"
        wr_path   = WINR_DIR / f"{lid}.json"
        odds_path = ODDS_DIR / f"{lid}.json"
        if not (fx_path.exists() and wr_path.exists()):
            continue

        fixtures   = load_json(fx_path).get("fixtures") or []
        winr_idx   = index_winrates(load_json(wr_path))
        odds_blob  = load_json(odds_path) if odds_path.exists() else {}
        odds_by_fx = {int(f.get("fixture_id")): f for f in (odds_blob.get("fixtures") or []) if isinstance(f.get("fixture_id"), int)}

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

            def find_row(team_name: str) -> Optional[dict]:
                row = winr_idx.get(norm(team_name))
                if row: return row
                for r in winr_idx.values():
                    if team_names_match(team_name, r.get("team_name","")):
                        return r
                return None

            home_t = find_row(home_nm)
            away_t = find_row(away_nm)
            if not (home_t and away_t):
                continue

            # rates (draws -> losses)
            H = _get_rates(home_t, stat_key)
            A = _get_rates(away_t, stat_key)

            # venue-split head-to-head proxy: home at HOME vs away AWAY
            hRate, hN = H["home"], H["n_home"]
            aRate, aN = A["away"], A["n_away"]

            # thresholds
            pick_side = None
            if (hRate >= MIN_SIDE_RATE) and ((hRate - aRate) >= MIN_GAP):
                pick_side = "home"
            if (aRate >= MIN_SIDE_RATE) and ((aRate - hRate) >= MIN_GAP):
                if (pick_side is None) or ((aRate - hRate) > (hRate - aRate)):
                    pick_side = "away"
            if not pick_side:
                continue

            # OPTIONAL 1X2 filter if odds exist
            fx_odds = odds_by_fx.get(int(fid)) if isinstance(fid, int) else None
            if fx_odds:
                h1x2, d1x2, a1x2 = extract_1x2_prices(fx_odds.get("odds") or [])
                chosen_price = h1x2 if pick_side == "home" else a1x2
                if chosen_price is not None:
                    # drop big favourites (too short)
                    if chosen_price <= FAVORITE_MAX_DECIMAL:
                        continue
                    # drop too large underdogs (user asked to drop > 4.0)
                    if chosen_price >= UNDERDOG_MAX_DECIMAL:
                        continue

            chosen_rate = hRate if pick_side=="home" else aRate
            other_rate  = aRate if pick_side=="home" else hRate
            gap = max(0.0, chosen_rate - other_rate)

            results.append({
                "fixture": name,
                "side": pick_side,
                "chosen_rate": chosen_rate,
                "gap": gap,
                "h_overall": H["overall"], "h_split": hRate, "h_n": hN,
                "a_overall": A["overall"], "a_split": aRate, "a_n": aN,
            })

    # rank
    results.sort(key=lambda r: (-r["chosen_rate"], -r["gap"], r["fixture"]))
    return results

# --- main ---
def main():
    # leagues
    if os.getenv("LEAGUE_IDS"):
        league_ids = [int(x) for x in os.getenv("LEAGUE_IDS").split(",") if x.strip()]
    else:
        league_ids = discover_league_ids()

    shots = evaluate_stat("shots_total", league_ids)
    sots  = evaluate_stat("shots_on_target", league_ids)

    lines = []
    lines.append(f"Generated at (UTC): {dt.datetime.utcnow().isoformat(timespec='seconds')}")
    lines.append("")

    lines.append("Most Shots — candidates (ranked by likelihood)")
    if not shots:
        lines.append("(no candidates)")
    else:
        for r in shots:
            side_txt = "HOME" if r["side"]=="home" else "AWAY"
            lines.append(f"{r['fixture']} — Most Shots: {side_txt}")
            lines.append(
                f"  Home: {r['h_overall']*100:.1f}% overall | {r['h_split']*100:.1f}% home (n={r['h_n']}) | "
                f"Away: {r['a_overall']*100:.1f}% overall | {r['a_split']*100:.1f}% away (n={r['a_n']})"
            )

    lines.append("")
    lines.append("Most Shots on Target — candidates (ranked by likelihood)")
    if not sots:
        lines.append("(no candidates)")
    else:
        for r in sots:
            side_txt = "HOME" if r["side"]=="home" else "AWAY"
            lines.append(f"{r['fixture']} — Most Shots on Target: {side_txt}")
            lines.append(
                f"  Home: {r['h_overall']*100:.1f}% overall | {r['h_split']*100:.1f}% home (n={r['h_n']}) | "
                f"Away: {r['a_overall']*100:.1f}% overall | {r['a_split']*100:.1f}% away (n={r['a_n']})"
            )

    out = "\n".join(lines).rstrip() + "\n"
    OUT_PATH.write_text(out, encoding="utf-8")
    print(out, end="")

if __name__ == "__main__":
    main()
