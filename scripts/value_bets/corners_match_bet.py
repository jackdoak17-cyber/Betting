#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Corners — Match Bet (Most Corners) value finder (Bet365)

Inputs (local only):
  Fixtures:
    - data/fixtures/by_league/{league_id}.json
    - (fallback) data/fixtures/{league_id}.json
  Team win rates (built by scripts/team_win_rates.py):
    - data/team_winrates/by_league/{league_id}.json
  Odds (Bet365 via SportMonks JSON you already store):
    - data/odds/b365/fixtures/{fixture_id}.json
    - (fallback) data/odds/b365/{league_id}.json

Outputs:
  - data/value_bets/corners_match_bet/{league_id}.json
  - data/value_bets/corners_match_bet_summary.txt

ENV (optional):
  LEAGUE_IDS     CSV of league IDs (default: auto-discover from team_winrates dir)
  WINDOW_DAYS    consider fixtures within next N days (default 7; 0 = all upcoming)
  MIN_MATCHES    require at least this many W/L samples per team (default 6)
  EDGE_MIN_WIN   min win_rate for fav team (default 0.60)
  EDGE_MAX_WIN   max win_rate for opponent (default 0.40)
  MIN_DEC_PRICE  minimum decimal price to keep (default 1.70)
  ENABLE_EV      "1" to enforce EV check, else "0" (default "1")
  EV_MIN_EDGE    est_prob - implied_prob must exceed this (default 0.03)

Note: The summary text is intentionally minimal: no est/implied/edge/KO lines.
"""

import os, re, json, math, datetime as dt, unicodedata
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Any

ROOT = Path(".")
FIX_BY_LG = ROOT / "data" / "fixtures" / "by_league"
FIX_FLAT  = ROOT / "data" / "fixtures"
WR_DIR    = ROOT / "data" / "team_winrates" / "by_league"
ODDS_FIX  = ROOT / "data" / "odds" / "b365" / "fixtures"
ODDS_LG   = ROOT / "data" / "odds" / "b365"
OUT_DIR   = ROOT / "data" / "value_bets" / "corners_match_bet"
OUT_DIR.mkdir(parents=True, exist_ok=True)

WINDOW_DAYS   = int(os.getenv("WINDOW_DAYS", "7"))
MIN_MATCHES   = int(os.getenv("MIN_MATCHES", "6"))
EDGE_MIN_WIN  = float(os.getenv("EDGE_MIN_WIN", "0.60"))
EDGE_MAX_WIN  = float(os.getenv("EDGE_MAX_WIN", "0.40"))
MIN_DEC_PRICE = float(os.getenv("MIN_DEC_PRICE", "1.70"))
ENABLE_EV     = os.getenv("ENABLE_EV", "1") == "1"
EV_MIN_EDGE   = float(os.getenv("EV_MIN_EDGE", "0.03"))
BOOKMAKER_B365 = 2

def now_utc() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)

# ---------- string & name helpers ----------
def strip_accents(s: str) -> str:
    return ''.join(c for c in unicodedata.normalize('NFD', s or '') if unicodedata.category(c) != 'Mn')

def norm(s: str) -> str:
    s = strip_accents(s or "").lower()
    s = re.sub(r"[^a-z0-9\s\.-]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s

GENERIC_TEAM_TOKENS = {
    "fc","cf","afc","sc","cd","ud","ac","as","ss","ssc","us","uc",
    "rc","rcd","ca","the","club","de","del","la","las","los","calcio",
    "united","city","saint","st","bk"
}

def team_tokens(name: str):
    t = set(norm(name).split())
    return {x for x in t if x not in GENERIC_TEAM_TOKENS}

def team_names_match(a: str, b: str) -> bool:
    if not a or not b: return False
    ta, tb = team_tokens(a), team_tokens(b)
    if not ta or not tb: return False
    if ta == tb or ta.issubset(tb) or tb.issubset(ta): return True
    inter = ta & tb; union = ta | tb
    return (len(inter) / max(1,len(union)) >= 0.5) or (len(inter) >= 2)

def parse_fixture_teams(name: str) -> Tuple[str, str]:
    if not name: return "", ""
    for sep in (" vs ", " v ", " - ", " VS ", " Vs "):
        if sep in name:
            a,b = name.split(sep, 1)
            return a.strip(), b.strip()
    return "", ""

def within_window(starting_at: str, days: int) -> bool:
    if not days: return True
    try:
        ko = dt.datetime.strptime(starting_at, "%Y-%m-%d %H:%M:%S").replace(tzinfo=dt.timezone.utc)
    except Exception:
        return True
    return now_utc() <= ko <= (now_utc() + dt.timedelta(days=days))

# ---------- IO ----------
def load_json(p: Path) -> dict:
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}

def discover_league_ids() -> List[int]:
    ids = []
    for p in WR_DIR.glob("*.json"):
        try: ids.append(int(p.stem))
        except: pass
    return sorted(set(ids))

def read_fixtures(league_id: int) -> List[dict]:
    p1 = FIX_BY_LG / f"{league_id}.json"
    blob = load_json(p1)
    if not blob:
        p2 = FIX_FLAT / f"{league_id}.json"
        blob = load_json(p2)
    fixtures = blob.get("fixtures") or (blob.get("data") or {}).get("fixtures") or []
    return fixtures

def read_winrates(league_id: int) -> dict:
    return load_json(WR_DIR / f"{league_id}.json")

# ---------- Odds helpers ----------
def iter_fixture_odds_from_league_blob(league_blob: dict, fid: int) -> Optional[List[dict]]:
    for fx in (league_blob.get("fixtures") or []):
        try:
            if int(fx.get("fixture_id") or -1) == int(fid):
                return fx.get("odds") or []
        except Exception:
            pass
    return None

def load_odds_rows_for_fixture(league_id: int, fid: int) -> List[dict]:
    p = ODDS_FIX / f"{fid}.json"
    blob = load_json(p)
    rows = blob.get("odds") or (blob.get("data") or {}).get("odds") or None
    if isinstance(rows, list) and rows:
        return rows
    lg = load_json(ODDS_LG / f"{league_id}.json")
    rows = iter_fixture_odds_from_league_blob(lg, fid)
    return rows or []

# ----- Detect "Corner Match Bet / Most Corners" market -----
CORNER_MATCH_KEYS = {
    "most corners", "corner match bet", "corners match bet",
    "team with most corners", "corners winner", "corners - match bet",
    "corners - most", "most corner", "team to have most corners"
}

def is_corners_match_market(desc: str) -> bool:
    s = norm(desc)
    return any(k in s for k in CORNER_MATCH_KEYS)

def label_side(label: str, home: str, away: str) -> Optional[str]:
    s = (label or "").strip().lower()
    if s in {"1","home","1 (home)"}: return "home"
    if s in {"2","away","2 (away)"}: return "away"
    if s in {"x","draw","tie"}: return "draw"
    if team_names_match(label, home): return "home"
    if team_names_match(label, away): return "away"
    return None

def best_price_per_side(rows: List[dict], home: str, away: str) -> Dict[str, float]:
    best = {"home": None, "away": None, "draw": None}
    for r in rows:
        try:
            if int(r.get("bookmaker_id") or 0) != BOOKMAKER_B365:
                continue
        except Exception:
            continue
        if not is_corners_match_market(r.get("market_description","")):
            continue
        if r.get("stopped"):
            continue
        side = label_side(str(r.get("label") or ""), home, away)
        if side not in {"home","away","draw"}:
            continue
        try:
            price = float(str(r.get("value")))
        except Exception:
            continue
        if best[side] is None or price > best[side]:
            best[side] = price
    return {k: v for k,v in best.items() if v is not None}

# ---------- math ----------
def implied_prob(price: float) -> float:
    try:
        return 1.0 / float(price)
    except Exception:
        return 1.0

def est_prob_from_wr(fav_wr: float, opp_wr: float) -> float:
    fav_wr = max(0.0, min(1.0, fav_wr))
    opp_wr = max(0.0, min(1.0, opp_wr))
    return (fav_wr + (1.0 - opp_wr)) / 2.0

def fmt_pct(x: float) -> str:
    try:
        return f"{100.0*float(x):.1f}%"
    except Exception:
        return "n/a"

# ---------- main ----------
def main():
    env = os.getenv("LEAGUE_IDS", "").strip()
    league_ids = [int(x) for x in env.split(",") if x.strip()] if env else discover_league_ids()

    combined_lines: List[str] = []
    combined_lines.append(f"Generated at (UTC): {now_utc().isoformat()}")
    combined_lines.append("")

    for lid in league_ids:
        fixtures = read_fixtures(lid)
        wr_blob  = read_winrates(lid)
        teams_wr = { (t.get("team_name") or "").strip(): t for t in wr_blob.get("teams") or [] }

        def corners_rates(team_name: str) -> Optional[dict]:
            rec = teams_wr.get(team_name)
            if not rec:
                for k, v in teams_wr.items():
                    if team_names_match(k, team_name):
                        rec = v; break
            if not rec: return None
            cat = (rec.get("categories") or {}).get("corners")
            return cat.get("rates") if cat else None

        picks: List[dict] = []

        for fx in fixtures:
            name = fx.get("name") or ""
            home, away = parse_fixture_teams(name)
            if not home or not away:
                continue

            sat = fx.get("starting_at") or ""
            ts  = fx.get("starting_at_timestamp")
            if WINDOW_DAYS:
                if isinstance(ts, (int, float)):
                    ko = dt.datetime.utcfromtimestamp(int(ts)).replace(tzinfo=dt.timezone.utc)
                    if not (now_utc() <= ko <= now_utc() + dt.timedelta(days=WINDOW_DAYS)):
                        continue
                else:
                    if not within_window(sat, WINDOW_DAYS):
                        continue

            r_home = corners_rates(home)
            r_away = corners_rates(away)
            if not r_home or not r_away:
                continue
            if r_home.get("n", 0) < MIN_MATCHES or r_away.get("n", 0) < MIN_MATCHES:
                continue

            fid = int(fx.get("id") or fx.get("fixture_id") or 0)
            if not fid:
                continue

            rows = load_odds_rows_for_fixture(lid, fid)
            if not rows:
                continue

            best = best_price_per_side(rows, home, away)
            if not best:
                continue

            def maybe_add(side: str, fav_wr: float, opp_wr: float, price: Optional[float]):
                if price is None: return
                if fav_wr < EDGE_MIN_WIN or opp_wr > EDGE_MAX_WIN: return
                if price < MIN_DEC_PRICE: return
                est = est_prob_from_wr(fav_wr, opp_wr)
                imp = implied_prob(price)
                if ENABLE_EV and not (est - imp > EV_MIN_EDGE): return
                picks.append({
                    "fixture_id": fid,
                    "fixture": name,
                    "kickoff": sat,
                    "side": side,
                    "team": home if side=="home" else away,
                    "opponent": away if side=="home" else home,
                    "price": float(price),
                    "est_prob": round(est, 4),
                    "implied_prob": round(imp, 4),
                    "edge": round(est - imp, 4),
                    "home_wr": round(float(r_home["win_rate"]), 4),
                    "home_n": int(r_home["n"]),
                    "away_wr": round(float(r_away["win_rate"]), 4),
                    "away_n": int(r_away["n"]),
                })

            maybe_add("home", float(r_home["win_rate"]), float(r_away["win_rate"]), best.get("home"))
            maybe_add("away", float(r_away["win_rate"]), float(r_home["win_rate"]), best.get("away"))

        # sort but keep summary minimal
        picks.sort(key=lambda r: (-r["edge"], -r["price"], r["fixture"], r["team"]))

        out_json = {
            "generated_at": now_utc().isoformat(),
            "league_id": lid,
            "params": {
                "WINDOW_DAYS": WINDOW_DAYS,
                "MIN_MATCHES": MIN_MATCHES,
                "EDGE_MIN_WIN": EDGE_MIN_WIN,
                "EDGE_MAX_WIN": EDGE_MAX_WIN,
                "MIN_DEC_PRICE": MIN_DEC_PRICE,
                "ENABLE_EV": ENABLE_EV,
                "EV_MIN_EDGE": EV_MIN_EDGE,
            },
            "count": len(picks),
            "picks": picks,
        }
        (OUT_DIR / f"{lid}.json").write_text(json.dumps(out_json, ensure_ascii=False, indent=2), encoding="utf-8")

        # ---- Minimal summary lines (no est/implied/edge/KO)
        if picks:
            combined_lines.append(f"League {lid}: {len(picks)} candidates")
            for r in picks[:80]:
                combined_lines.append(
                    f"{r['fixture']} — Most Corners: {r['side'].upper()} @ {r['price']:.2f}"
                )
                combined_lines.append(
                    f"  Home win rate: {100.0*r['home_wr']:.1f}% (n={r['home_n']}) | Away win rate: {100.0*r['away_wr']:.1f}% (n={r['away_n']})"
                )
            combined_lines.append("")
        else:
            combined_lines.append(f"League {lid}: no candidates\n")

    (OUT_DIR / "corners_match_bet_summary.txt").write_text("\n".join(combined_lines).rstrip() + "\n", encoding="utf-8")
    print("\n".join(combined_lines))

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
