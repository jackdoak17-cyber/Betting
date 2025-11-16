#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Goals markets — value flags (LOCAL files only; no API calls)

Flags a fixture/market when BOTH are true:
  • Form threshold hit (default 70% on last-10 window)
  • Bet365 decimal price >= MIN_PRICE (default 1.80)

Form signals from your series:
  - Over 2.5 (team totals = goals + opp_goals)
  - BTTS (goals > 0 AND opp_goals > 0)
  - Team Over 1.5 (home team, away team separately)

Inputs (must already exist):
  - data/fixtures/{league_id}.json
  - data/team_stats/by_league/{league_id}.json
  - data/team_opponent_stats/by_league/{league_id}.json
  - data/odds/b365/{league_id}.json   # Bet365-only dump

Outputs:
  - data/value_bets/goals_value_flags.txt
  - posts/value_bets_goals.md

Env (optional):
  - MIN_PRICE (default 1.80)
  - MIN_GAMES (default 6)
  - THRESH_OVER25 (default 0.70)
  - THRESH_BTTS (default 0.70)
  - THRESH_TEAM_O15 (default 0.70)
  - WINDOW_DAYS (default 7; 0 = no limit)
  - BET365_ID (default 2)
  - DEBUG_MARKETS=1 (print market-id tallies per fixture)
  - DEBUG_UNDERPRICE=1 (log near-misses with found price)
"""

import os, re, json, math, datetime as dt, unicodedata
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# ---------- Config ----------
ROOT = Path(".")
FIX_DIR   = ROOT / "data" / "fixtures"
TS_DIR    = ROOT / "data" / "team_stats" / "by_league"
OPP_DIR   = ROOT / "data" / "team_opponent_stats" / "by_league"
ODDS_DIR  = ROOT / "data" / "odds" / "b365"

OUT_DIR   = ROOT / "data" / "value_bets"; OUT_DIR.mkdir(parents=True, exist_ok=True)
OUT_TXT   = OUT_DIR / "goals_value_flags.txt"
POSTS_DIR = ROOT / "posts"; POSTS_DIR.mkdir(parents=True, exist_ok=True)
OUT_MD    = POSTS_DIR / "value_bets_goals.md"

MIN_PRICE        = float(os.getenv("MIN_PRICE", "1.80"))
MIN_GAMES        = int(os.getenv("MIN_GAMES", "6"))
THRESH_OVER25    = float(os.getenv("THRESH_OVER25", "0.70"))
THRESH_BTTS      = float(os.getenv("THRESH_BTTS",   "0.70"))
THRESH_TEAM_O15  = float(os.getenv("THRESH_TEAM_O15","0.70"))
WINDOW_DAYS      = int(os.getenv("WINDOW_DAYS", "7"))
BET365_ID        = int(os.getenv("BET365_ID", "2"))
DEBUG_MARKETS    = bool(int(os.getenv("DEBUG_MARKETS", "0")))
DEBUG_UNDERPRICE = bool(int(os.getenv("DEBUG_UNDERPRICE", "0")))

# Hard market IDs (Sportmonks)
MID_BTTS          = 14         # Both Teams To Score
MID_OU_CANDIDATES = {80, 5}    # Goals O/U + Alternative Match Goals
MID_HOME_GOALS    = 20         # Home Team Goals
MID_AWAY_GOALS    = 21         # Away Team Goals

# ---------- String utils ----------
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
    if not days: return True
    try:
        dt_utc = dt.datetime.strptime(starting_at[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=dt.timezone.utc)
    except Exception:
        return True
    now = dt.datetime.now(dt.timezone.utc)
    return now <= dt_utc <= (now + dt.timedelta(days=days))

# ---------- Loaders ----------
def discover_league_ids() -> List[int]:
    ids = []
    for p in FIX_DIR.glob("*.json"):
        try: ids.append(int(p.stem))
        except: pass
    return sorted(set(ids))

def load_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}

def index_team(blob: dict) -> Dict[str, dict]:
    m: Dict[str, dict] = {}
    for t in (blob.get("teams") or []):
        nm = t.get("team_name")
        if not nm: continue
        m[norm(nm)] = t
    return m

# ---------- Series helpers ----------
def as_int_list(xs) -> List[int]:
    out = []
    for v in (xs or []):
        try: out.append(int(v))
        except: pass
    return out

def btts_from(goals: List[int], opp_goals: List[int]) -> Tuple[int,int,float]:
    n = min(len(goals), len(opp_goals))
    hits = sum(1 for i in range(n) if goals[i] > 0 and opp_goals[i] > 0)
    return hits, n, (hits / n) if n else 0.0

def over_k_from(goals: List[int], opp_goals: List[int], k: float) -> Tuple[int,int,float]:
    n = min(len(goals), len(opp_goals))
    thr = math.ceil(k + 1e-9)   # e.g., 2.5 -> 3
    hits = sum(1 for i in range(n) if (goals[i] + opp_goals[i]) >= thr)
    return hits, n, (hits / n) if n else 0.0

def team_over_from(goals: List[int], thr: int) -> Tuple[int,int,float]:
    xs = as_int_list(goals)
    hits = sum(1 for x in xs if x >= thr)
    n = len(xs)
    return hits, n, (hits / n) if n else 0.0

# ---------- Odds helpers ----------
def f_price(v) -> Optional[float]:
    try: return float(v)
    except Exception: return None

def f_total(row) -> Optional[float]:
    """Return numeric line from handicap/total (supports 2.5, '2.5', '2.50')."""
    # numeric handicap first
    if row.get("handicap") is not None:
        try: return float(row["handicap"])
        except Exception: pass
    # then string total
    t = row.get("total")
    if t is None: return None
    try:
        return float(str(t).strip())
    except Exception:
        # last resort: dig first number
        m = re.search(r"([-+]?\d+(?:\.\d+)?)", str(t))
        if m:
            try: return float(m.group(1))
            except Exception: return None
    return None

def same_line(row, target: float, eps: float = 1e-6) -> bool:
    x = f_total(row)
    return (x is not None) and (abs(x - target) <= max(eps, 1e-3))

def row_active(r: dict) -> bool:
    # some rows are suspended/closed
    return not bool(r.get("stopped"))

def best_price_over25(rows: List[dict]) -> Optional[float]:
    """Best O2.5 @ Bet365 from market ids {80,5}, label Over, line ~ 2.5"""
    best = None
    for r in rows:
        if r.get("bookmaker_id") != BET365_ID: continue
        if not row_active(r): continue
        if r.get("market_id") not in MID_OU_CANDIDATES: continue
        label = norm(str(r.get("label") or ""))
        if ("over" not in label) or ("under" in label): continue
        if not same_line(r, 2.5): continue
        p = f_price(r.get("value"))
        if p is None: continue
        if (best is None) or (p > best): best = p
    return best

def best_price_btts_yes(rows: List[dict]) -> Optional[float]:
    """Best BTTS Yes strictly from market id 14 (avoid halves/variants)."""
    best = None
    for r in rows:
        if r.get("bookmaker_id") != BET365_ID: continue
        if not row_active(r): continue
        if r.get("market_id") != MID_BTTS: continue
        label = norm(str(r.get("label") or ""))
        name  = norm(str(r.get("name") or ""))
        if ("yes" not in label) and ("yes" not in name): continue
        p = f_price(r.get("value"))
        if p is None: continue
        if (best is None) or (p > best): best = p
    return best

def best_price_team_o15(rows: List[dict], home_side: bool) -> Optional[float]:
    """Home uses market 20, away uses 21 (label Over, line ~1.5)."""
    want_mid = MID_HOME_GOALS if home_side else MID_AWAY_GOALS
    best = None
    for r in rows:
        if r.get("bookmaker_id") != BET365_ID: continue
        if not row_active(r): continue
        if r.get("market_id") != want_mid: continue
        label = norm(str(r.get("label") or ""))
        if ("over" not in label) or ("under" in label): continue
        if not same_line(r, 1.5): continue
        p = f_price(r.get("value"))
        if p is None: continue
        if (best is None) or (p > best): best = p
    return best

# ---------- Debug ----------
def tally_markets(rows: List[dict]) -> Dict[int, int]:
    d: Dict[int,int] = {}
    for r in rows:
        mid = r.get("market_id")
        if isinstance(mid, int):
            d[mid] = d.get(mid, 0) + 1
    return dict(sorted(d.items()))

# ---------- Main ----------
def main():
    league_ids = discover_league_ids()

    flags_over25 = []  # (fixture, price, H%, A%, combo%)
    flags_btts   = []  # (fixture, price, H%, A%, combo%)
    flags_team15 = []  # (fixture, side, team, price, team%)

    near_o25 = 0; near_btts = 0; near_t15 = 0

    for lid in league_ids:
        fx_path   = FIX_DIR / f"{lid}.json"
        ts_path   = TS_DIR  / f"{lid}.json"
        opp_path  = OPP_DIR / f"{lid}.json"
        odds_path = ODDS_DIR/ f"{lid}.json"
        if not (fx_path.exists() and ts_path.exists() and opp_path.exists() and odds_path.exists()):
            continue

        fixtures = load_json(fx_path).get("fixtures") or []
        ts_idx   = index_team(load_json(ts_path))
        opp_idx  = index_team(load_json(opp_path))
        odds_blob = load_json(odds_path)
        odds_by_fixture = {int(f.get("fixture_id")): f for f in (odds_blob.get("fixtures") or []) if isinstance(f.get("fixture_id"), int)}

        for fx in fixtures:
            fid = int(fx.get("id") or fx.get("fixture_id") or 0)
            if not fid: continue
            name = fx.get("name") or ""
            starting_at = fx.get("starting_at") or ""
            if WINDOW_DAYS and not within_window(starting_at, WINDOW_DAYS):
                continue

            home, away = parse_fixture_teams(name)
            if not (home and away): 
                continue

            h_rec = next((ts_idx[k] for k in ts_idx if team_names_match(home, k)), None)
            a_rec = next((ts_idx[k] for k in ts_idx if team_names_match(away, k)), None)
            h_opp = next((opp_idx[k] for k in opp_idx if team_names_match(away, k)), None)  # away conceded vs home
            a_opp = next((opp_idx[k] for k in opp_idx if team_names_match(home, k)), None)  # home conceded vs away
            if not (h_rec and a_rec and h_opp and a_opp):
                continue

            H_g  = as_int_list(h_rec.get("goals_last_n"))
            A_g  = as_int_list(a_rec.get("goals_last_n"))
            HogA = as_int_list(a_opp.get("opp_goals_last_n"))
            AogH = as_int_list(h_opp.get("opp_goals_last_n"))

            def ok_len(x): return len(x) >= MIN_GAMES

            h_over25 = over_k_from(H_g, HogA, 2.5) if ok_len(H_g) and ok_len(HogA) else (0,0,0.0)
            a_over25 = over_k_from(A_g, AogH, 2.5) if ok_len(A_g) and ok_len(AogH) else (0,0,0.0)

            h_btts = btts_from(H_g, HogA) if ok_len(H_g) and ok_len(HogA) else (0,0,0.0)
            a_btts = btts_from(A_g, AogH) if ok_len(A_g) and ok_len(AogH) else (0,0,0.0)

            h_o15 = team_over_from(H_g, 2) if ok_len(H_g) else (0,0,0.0)
            a_o15 = team_over_from(A_g, 2) if ok_len(A_g) else (0,0,0.0)

            pass_over25 = (h_over25[1] >= MIN_GAMES and a_over25[1] >= MIN_GAMES and
                           h_over25[2] >= THRESH_OVER25 and a_over25[2] >= THRESH_OVER25)
            pass_btts   = (h_btts[1]   >= MIN_GAMES and a_btts[1]   >= MIN_GAMES and
                           h_btts[2]   >= THRESH_BTTS   and a_btts[2]   >= THRESH_BTTS)
            pass_h15    = (h_o15[1]    >= MIN_GAMES and h_o15[2]    >= THRESH_TEAM_O15)
            pass_a15    = (a_o15[1]    >= MIN_GAMES and a_o15[2]    >= THRESH_TEAM_O15)

            odds_fx = odds_by_fixture.get(fid) or {}
            rows = odds_fx.get("odds") or []

            if DEBUG_MARKETS and rows:
                tall = tally_markets(rows)
                print(f"[DEBUG] {name} markets present: {tall}")

            # ---- OVER 2.5 ----
            if pass_over25:
                p = best_price_over25(rows)
                if p and p >= MIN_PRICE:
                    combo = (h_over25[2] + a_over25[2]) / 2.0
                    flags_over25.append((name, p, h_over25[2], a_over25[2], combo))
                elif p:
                    near_o25 += 1
                    if DEBUG_UNDERPRICE:
                        print(f"[UNDER] O2.5 {name} best={p:.2f} < {MIN_PRICE:.2f}")

            # ---- BTTS YES ----
            if pass_btts:
                p = best_price_btts_yes(rows)
                if p and p >= MIN_PRICE:
                    combo = (h_btts[2] + a_btts[2]) / 2.0
                    flags_btts.append((name, p, h_btts[2], a_btts[2], combo))
                elif p:
                    near_btts += 1
                    if DEBUG_UNDERPRICE:
                        print(f"[UNDER] BTTS {name} best={p:.2f} < {MIN_PRICE:.2f}")

            # ---- TEAM OVER 1.5 ----
            if pass_h15:
                p = best_price_team_o15(rows, home_side=True)
                if p and p >= MIN_PRICE:
                    flags_team15.append((name, "home", home, p, h_o15[2]))
                elif p:
                    near_t15 += 1
                    if DEBUG_UNDERPRICE:
                        print(f"[UNDER] TeamO1.5 {home} (home) {name} best={p:.2f} < {MIN_PRICE:.2f}")
            if pass_a15:
                p = best_price_team_o15(rows, home_side=False)
                if p and p >= MIN_PRICE:
                    flags_team15.append((name, "away", away, p, a_o15[2]))
                elif p:
                    near_t15 += 1
                    if DEBUG_UNDERPRICE:
                        print(f"[UNDER] TeamO1.5 {away} (away) {name} best={p:.2f} < {MIN_PRICE:.2f}")

    # --------- Sort (by price desc then a simple edge proxy) ----------
    def edge_proxy(p, q):  # q = probability estimate (combo/team)
        return (q * p) - 1.0

    flags_over25.sort(key=lambda x: (-x[1], -edge_proxy(x[1], x[4]), x[0]))
    flags_btts.sort(key=lambda x: (-x[1], -edge_proxy(x[1], x[4]), x[0]))
    flags_team15.sort(key=lambda x: (-x[3], -edge_proxy(x[3], x[4]), x[0], x[1]))

    # --------- Render TEXT ---------
    now_iso = dt.datetime.utcnow().isoformat(timespec="seconds")
    lines = []
    lines.append(f"Generated at (UTC): {now_iso}")
    lines.append(f"Rules: MIN_PRICE>={MIN_PRICE:.2f}, MIN_GAMES>={MIN_GAMES}, thresholds: O2.5>={int(THRESH_OVER25*100)}%, BTTS>={int(THRESH_BTTS*100)}%, TeamO1.5>={int(THRESH_TEAM_O15*100)}%")
    lines.append("")
    def pct(p): return f"{p*100:.1f}%"

    lines.append("=== Over 2.5 — value flags ===")
    if not flags_over25: lines.append("  (none)")
    for name, price, hp, ap, combo in flags_over25:
        edge = edge_proxy(price, combo)
        lines.append(f" • {name} — Over 2.5 @ {price:.2f} | H {pct(hp)} / A {pct(ap)} | combo {pct(combo)} | edge {edge*100:+.1f}%")

    lines.append("")
    lines.append("=== BTTS (Yes) — value flags ===")
    if not flags_btts: lines.append("  (none)")
    for name, price, hp, ap, combo in flags_btts:
        edge = edge_proxy(price, combo)
        lines.append(f" • {name} — BTTS Yes @ {price:.2f} | H {pct(hp)} / A {pct(ap)} | combo {pct(combo)} | edge {edge*100:+.1f}%")

    lines.append("")
    lines.append("=== Team Over 1.5 — value flags ===")
    if not flags_team15: lines.append("  (none)")
    for name, side, team, price, tp in flags_team15:
        edge = edge_proxy(price, tp)
        lines.append(f" • {team} — Team Over 1.5 ({side}) @ {price:.2f} | team {pct(tp)} | edge {edge*100:+.1f}% | {name}")

    nm = []
    if near_o25: nm.append(f"O2.5={near_o25}")
    if near_btts: nm.append(f"BTTS={near_btts}")
    if near_t15: nm.append(f"TeamO1.5={near_t15}")
    if nm:
        lines.append("")
        lines.append(f"(near-misses: {', '.join(nm)})  # passed form but below MIN_PRICE")

    OUT_TXT.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")

    # --------- Render POST (Markdown) ---------
    md = []
    md.append("I’ve collated a high-probability goals shortlist from teams’ last 10 league games and flagged **potential value** where the market is pricing at **≥ {:.2f}**.".format(MIN_PRICE))
    md.append("")
    md.append(f"_Form gates:_ Over 2.5 ≥ {int(THRESH_OVER25*100)}%, BTTS ≥ {int(THRESH_BTTS*100)}%, Team Over 1.5 ≥ {int(THRESH_TEAM_O15*100)}% (≥{MIN_GAMES} games).")
    md.append("")
    md.append("### Over 2.5 — value flags")
    if not flags_over25: md.append("- (none)")
    for name, price, hp, ap, combo in flags_over25:
        md.append(f"- **{name}** — **Over 2.5 @ {price:.2f}** (H {pct(hp)} / A {pct(ap)}; combo {pct(combo)})")

    md.append("")
    md.append("### BTTS (Yes) — value flags")
    if not flags_btts: md.append("- (none)")
    for name, price, hp, ap, combo in flags_btts:
        md.append(f"- **{name}** — **BTTS Yes @ {price:.2f}** (H {pct(hp)} / A {pct(ap)}; combo {pct(combo)})")

    md.append("")
    md.append("### Team Over 1.5 — value flags")
    if not flags_team15: md.append("- (none)")
    for name, side, team, price, tp in flags_team15:
        md.append(f"- **{team}** — **Team Over 1.5 @ {price:.2f}** ({side}; {pct(tp)}) — {name}")

    OUT_MD.write_text("\n".join(md).rstrip() + "\n", encoding="utf-8")

    print("\n".join(lines))
    print(f"\nWrote:\n  • {OUT_TXT}\n  • {OUT_MD}")

if __name__ == "__main__":
    main()
