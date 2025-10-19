#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Team Line Beat Rates — Conservative (Sportmonks / Bet365)

What it does
------------
- Scans team lines (shots, SOT, corners, tackles) from data/team_lines/by_league/*.json
- Finds the corresponding upcoming fixtures in data/odds/b365/{league_id}.json
- Reads Bet365 prices from Sportmonks odds payload (per-fixture odds array)
- Computes team 'over' and 'under' hit rates from your series:
    • team offense vs line
    • opponent allowed vs line
  -> uses combo% = min(team_rate, opp_allowed_rate)
- Filters by price and moneyline as before:
    • Over (shots/SOT/corners): team ML ≤ TEAM_WIN_MAX
    • Under (shots/SOT/corners): team ML  > UNDERDOG_MIN
    • Tackles: no ML filter
    • Keep only odds ≥ MIN_DEC_PRICE
- Drops fixtures outside the next WINDOW_DAYS (0 disables date filter)

Writes
------
data/value_bets/team_line_beat_conservative.txt

Env
---
MIN_DEC_PRICE  (default "1.20")
TEAM_WIN_MAX   (default "3.50")
UNDERDOG_MIN   (default "3.50")
COMBO_MIN      (default "0.50")
WINDOW_DAYS    (default "7")
LEAGUE_IDS     (optional, comma-separated; default autodetect from data/team_lines/by_league)
"""

import os, re, json, math, datetime as dt, unicodedata
from pathlib import Path
from typing import Dict, List, Tuple, Optional, Any

# ====== Config ======
MIN_DEC_PRICE = float(os.getenv("MIN_DEC_PRICE", "1.20"))
TEAM_WIN_MAX  = float(os.getenv("TEAM_WIN_MAX",  "3.50"))
UNDERDOG_MIN  = float(os.getenv("UNDERDOG_MIN",  "3.50"))
COMBO_MIN     = float(os.getenv("COMBO_MIN",     "0.50"))
WINDOW_DAYS   = int(os.getenv("WINDOW_DAYS",     "7"))

ROOT         = Path(".")
LINES_DIR    = ROOT / "data" / "team_lines" / "by_league"
ODDS_DIR     = ROOT / "data" / "odds" / "b365"
OUT_DIR      = ROOT / "data" / "value_bets"; OUT_DIR.mkdir(parents=True, exist_ok=True)
OUT_FILE     = OUT_DIR / "team_line_beat_conservative.txt"

MARKET_MATCH_WINNER = 1  # 1X2 / ML

# ====== Market normalisation (Sportmonks) ======
TEAM_MARKET_KEYS = {
    "team shots": "shots",
    "team shots on target": "shots_on_target",
    "team sot": "shots_on_target",
    "team corners": "corners",
    "team tackles": "tackles",
}

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
    inter = ta & tb; union = ta | tb
    if len(inter) / max(1, len(union)) >= 0.5: return True
    if len(inter) >= 2: return True
    return False

def parse_fixture_teams(fixture_name: str) -> Tuple[str, str]:
    if not fixture_name: return "",""
    for sep in (" vs ", " v ", " VS ", " Vs ", " - "):
        if sep in fixture_name:
            a, b = fixture_name.split(sep, 1)
            return a.strip(), b.strip()
    return "", ""

def within_window(starting_at: str, days: int) -> bool:
    if not days: return True
    try:
        # Sportmonks format: "YYYY-MM-DD HH:MM:SS" (UTC)
        dt_utc = dt.datetime.strptime(starting_at, "%Y-%m-%d %H:%M:%S").replace(tzinfo=dt.timezone.utc)
    except Exception:
        return True  # be permissive if missing / unparsable
    now = dt.datetime.now(dt.timezone.utc)
    return now <= dt_utc <= (now + dt.timedelta(days=days))

# ====== Read team_lines contexts ======
def iter_series(lines_blob: dict, league_id: int):
    for fx in (lines_blob.get("fixtures") or []):
        meta = {
            "fixture_id": fx.get("fixture_id"),
            "league_id": fx.get("league_id") or league_id,
            "starting_at": fx.get("starting_at"),
            "home_name": fx.get("home_name"),
            "away_name": fx.get("away_name"),
        }
        teams = fx.get("teams") or {}
        for side in ("home","away"):
            t = (teams.get(side) or {})
            opp_side = "away" if side=="home" else "home"
            opp = (teams.get(opp_side) or {})
            team_name = t.get("name") or t.get("team_name") or (meta["home_name"] if side=="home" else meta["away_name"])
            opp_name  = opp.get("name") or opp.get("team_name") or (meta["away_name"] if side=="home" else meta["home_name"])
            stats = (t.get("stats") or {})
            for stat_key, m in stats.items():
                if stat_key not in ("shots","shots_on_target","corners","tackles"): continue
                ser = (m.get("series_used") or {})
                off = [x for x in (ser.get("offense_lastN") or []) if isinstance(x,int)]
                oppA= [x for x in (ser.get("opponent_allowed_lastN") or []) if isinstance(x,int)]
                if not off and not oppA: continue
                yield {
                    "league_id": league_id,
                    "fixture_id": meta["fixture_id"],
                    "starting_at": meta["starting_at"],
                    "home_name": meta["home_name"],
                    "away_name": meta["away_name"],
                    "side": side,
                    "team_name": team_name or "",
                    "opp_name": opp_name or "",
                    "stat": stat_key,
                    "offense_lastN": off,
                    "opp_allowed_lastN": oppA,
                }

# ====== Odds helpers (Sportmonks JSON) ======
def as_float(x) -> Optional[float]:
    try:
        return float(str(x))
    except Exception:
        return None

def extract_team_ml_prices(odds_rows: List[dict], home_name: str, away_name: str) -> Tuple[Optional[float], Optional[float]]:
    """Return (home_ml, away_ml) from market_id=1-ish rows."""
    home_price = None; away_price = None
    for row in odds_rows:
        if int(row.get("market_id", 0)) != MARKET_MATCH_WINNER:
            continue
        label = norm(row.get("label") or "")
        name  = norm(row.get("name") or "")
        val   = as_float(row.get("value"))
        if val is None: 
            continue
        if label in {"1","home"} or team_names_match(home_name, label) or team_names_match(home_name, name):
            home_price = val if (home_price is None or val < home_price) else home_price
        elif label in {"2","away"} or team_names_match(away_name, label) or team_names_match(away_name, name):
            away_price = val if (away_price is None or val < away_price) else away_price
        elif team_names_match(home_name, row.get("name","")):
            home_price = val if (home_price is None or val < home_price) else home_price
        elif team_names_match(away_name, row.get("name","")):
            away_price = val if (away_price is None or val < away_price) else away_price
    return home_price, away_price

def detect_team_market(row: dict) -> Optional[str]:
    """
    Map Sportmonks row -> 'shots'|'shots_on_target'|'corners'|'tackles' if it is a team market.
    Uses market_description primarily.
    """
    desc = norm(row.get("market_description") or "")
    if not desc:  # fall back to any hint on name
        desc = norm(row.get("name") or "")
    for key, canon in TEAM_MARKET_KEYS.items():
        if key in desc:
            return canon
    return None

def row_side_for_team(row: dict, home_name: str, away_name: str) -> Optional[str]:
    """Infer whether the row applies to the home or away team."""
    # team name is often in 'name' or 'total' (Sportmonks variants)
    cand = row.get("name") or row.get("total") or row.get("original_label") or ""
    if team_names_match(cand, home_name): return "home"
    if team_names_match(cand, away_name): return "away"
    # some books put 'Home/Away' in label
    lab = norm(row.get("label") or "")
    if "home" in lab and "away" not in lab: return "home"
    if "away" in lab and "home" not in lab: return "away"
    return None

def row_pick_over_under(row: dict) -> Optional[str]:
    """Detect 'Over' or 'Under' selection for this row."""
    for f in ("label", "original_label", "name"):
        s = norm(row.get(f) or "")
        if "over" in s and "under" not in s: return "Over"
        if "under" in s and "over" not in s: return "Under"
    return None

def row_line(row: dict) -> Optional[float]:
    # try explicit number in label/handicap
    h = row.get("handicap")
    if h is not None:
        v = as_float(h)
        if v is not None: return v
    # label can be "Over 8.5" or just "8.5" or "(8.5)"
    lab = (row.get("label") or "").strip()
    m = re.search(r"([-+]?\d+(?:\.\d+)?)", lab)
    if m:
        try: return float(m.group(1))
        except: pass
    # sometimes 'total' can be the line, but we already used for team name; be conservative
    return None

# ====== rates ======
def over_hits(seq: List[int], line: float) -> float:
    if not seq: return 0.0
    thr = math.ceil(float(line))
    return sum(1 for x in seq if x >= thr) / len(seq)

def under_hits(seq: List[int], line: float) -> float:
    if not seq: return 0.0
    thr = math.floor(float(line))
    return sum(1 for x in seq if x <= thr) / len(seq)

# ====== Main ======
def main():
    # Discover leagues to scan
    env_leagues = os.getenv("LEAGUE_IDS")
    if env_leagues:
        league_ids = [int(x) for x in env_leagues.split(",") if x.strip()]
    else:
        league_ids = []
        for p in sorted(LINES_DIR.glob("*.json")):
            try:
                blob = json.loads(p.read_text(encoding="utf-8"))
                lid = int(blob.get("league_id") or re.findall(r"(\d+)", p.stem)[0])
                league_ids.append(lid)
            except Exception:
                pass
        league_ids = sorted(set(league_ids))

    # Load team_lines contexts
    contexts: List[dict] = []
    for lid in league_ids:
        p = LINES_DIR / f"{lid}.json"
        if not p.exists(): continue
        try:
            blob = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        contexts.extend(iter_series(blob, lid))

    if not contexts:
        print("No team_lines contexts found.")
        return

    # Load odds per league (Sportmonks Bet365)
    odds_by_league: Dict[int, dict] = {lid: (json.loads((ODDS_DIR / f"{lid}.json").read_text(encoding="utf-8"))
                                            if (ODDS_DIR / f"{lid}.json").exists() else {})
                                       for lid in league_ids}

    rows_over: List[dict] = []
    rows_under: List[dict] = []

    for r in contexts:
        lid = r["league_id"]
        blob = odds_by_league.get(lid) or {}
        fixtures = blob.get("fixtures") or []
        if not fixtures: continue

        # Find matching fixture by team vs opp
        match = None
        ev_side = r["side"]
        for fx in fixtures:
            fname = fx.get("name") or ""
            if not fname: continue
            if not within_window(fx.get("starting_at") or "", WINDOW_DAYS): 
                continue
            home, away = parse_fixture_teams(fname)
            if not home or not away: 
                continue
            if team_names_match(r["team_name"], home) and team_names_match(r["opp_name"], away):
                ev_side = "home"; match = fx; break
            if team_names_match(r["team_name"], away) and team_names_match(r["opp_name"], home):
                ev_side = "away"; match = fx; break
        if not match:
            continue

        odds_rows = match.get("odds") or []
        home_ml, away_ml = extract_team_ml_prices(odds_rows, match.get("name","").split(" vs ")[0] if match.get("name") else r["home_name"], 
                                                             match.get("name","").split(" vs ")[-1] if match.get("name") else r["away_name"])
        team_ml = home_ml if ev_side == "home" else away_ml

        # Walk odds rows and collect Over/Under for the right team/stat
        for row in odds_rows:
            stat_canon = detect_team_market(row)
            if stat_canon != r["stat"]:
                continue
            side = row_side_for_team(row, r["home_name"], r["away_name"])
            if side and side != ev_side:
                continue
            pick = row_pick_over_under(row)
            line = row_line(row)
            price = as_float(row.get("value"))
            if (pick not in {"Over","Under"}) or (line is None) or (price is None): 
                continue
            if price < MIN_DEC_PRICE:
                continue

            # Compute rates
            if pick == "Over":
                rate_t = over_hits(r["offense_lastN"], line)
                rate_a = over_hits(r["opp_allowed_lastN"], line)
            else:
                rate_t = under_hits(r["offense_lastN"], line)
                rate_a = under_hits(r["opp_allowed_lastN"], line)
            combo = min(rate_t, rate_a)
            if combo < COMBO_MIN:
                continue

            # ML filters for shots/SOT/corners (tackles exempt)
            if r["stat"] in ("shots","shots_on_target","corners"):
                if team_ml is None:
                    continue  # conservative drop if ML missing
                if pick == "Over" and not (team_ml <= TEAM_WIN_MAX):
                    continue
                if pick == "Under" and not (team_ml > UNDERDOG_MIN):
                    continue

            row_out = {
                "fixture": match.get("name",""),
                "kickoff": match.get("starting_at",""),
                "team": r["team_name"],
                "opp": r["opp_name"],
                "side": ev_side,
                "stat": r["stat"],
                "hdp": float(line),
                "price": float(price),
                "pick": pick,
                "team_rate": rate_t,
                "opp_allowed_rate": rate_a,
                "combo_rate": combo,
                "team_ml": float(team_ml) if isinstance(team_ml, float) else None,
                "market": row.get("market_description") or "",
            }
            if pick == "Over":
                rows_over.append(row_out)
            else:
                rows_under.append(row_out)

    # Rank & render
    rows_over.sort(key=lambda r: (-r["combo_rate"], -r["price"], r["fixture"], r["team"], r["stat"], r["hdp"]))
    rows_under.sort(key=lambda r: (-r["combo_rate"], -r["price"], r["fixture"], r["team"], r["stat"], r["hdp"]))

    def lab_stat(k: str) -> str:
        return {"shots":"Shots","shots_on_target":"SOT","corners":"Corners","tackles":"Tackles"}.get(k,k)
    def pct(x: float) -> str:
        return f"{x*100:5.1f}%"

    lines = []
    lines.append(f"Generated at (UTC): {dt.datetime.utcnow().isoformat()}")
    lines.append(f"Min price: {MIN_DEC_PRICE:.2f} | Combo≥{COMBO_MIN:.2f} | "
                 f"Shots/SOT/Corners OVER ML≤{TEAM_WIN_MAX:.2f} | Shots/SOT/Corners UNDER ML>{UNDERDOG_MIN:.2f} | "
                 f"Window={WINDOW_DAYS} days")
    lines.append("")
    def dump(title: str, rows: List[dict], limit: int = 120):
        lines.append(title)
        if not rows:
            lines.append("  No qualifying lines after conservative filters.\n"); return
        for r in rows[:limit]:
            ml = f" | ML={r['team_ml']:.3f}" if isinstance(r.get("team_ml"), float) else ""
            lines.append(
                f" • {r['team']} — {lab_stat(r['stat'])} {r['pick']} {r['hdp']:.1f} @ {r['price']:.3f} | "
                f"{r['fixture']} @ {r['kickoff']} | side={r['side']} | "
                f"team={pct(r['team_rate'])} oppA={pct(r['opp_allowed_rate'])} combo={pct(r['combo_rate'])}"
                f"{ml} | {r['market']}"
            )
        lines.append("")
    dump("===== TEAM LINES — OVER (Conservative) =====", rows_over)
    dump("===== TEAM LINES — UNDER (Conservative; underdogs only for Shots/SOT/Corners) =====", rows_under)

    OUT_FILE.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    print("\n".join(lines))

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
