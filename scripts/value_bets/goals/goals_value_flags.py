#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Goals markets — value flags (LOCAL files only; no API calls)

Flags a fixture/market when BOTH are true:
  • Form threshold hit (default 70% on last-10 window)
  • Bet365 decimal price >= MIN_PRICE (default 1.80)

Form signals computed from your series:
  - Over 2.5 (team totals = goals + opp_goals)
  - BTTS (goals >0 AND opp_goals >0)
  - Team Over 1.5 (home & away sides separately)

Inputs (must already exist):
  - data/fixtures/{league_id}.json
  - data/team_stats/by_league/{league_id}.json
  - data/team_opponent_stats/by_league/{league_id}.json
  - data/odds/b365/{league_id}.json

Outputs:
  - data/value_bets/goals_value_flags.txt
  - posts/value_bets_goals.md

Env (optional):
  MIN_PRICE        (default 1.80)
  MIN_GAMES        (default 6)
  THRESH_OVER25    (default 0.70)
  THRESH_BTTS      (default 0.70)
  THRESH_TEAM_O15  (default 0.70)
  WINDOW_DAYS      (default 7, 0 = no date filter)
  DEBUG_NEARMISS   (default 1 to print counters)
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
DEBUG_NEARMISS   = int(os.getenv("DEBUG_NEARMISS", "1")) != 0

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

# ---------- Odds parsing (robust) ----------
def nmarket(s: str) -> str:
    return norm(s)

# Accept multiple synonyms (all normalized)
OVER25_MDS = {
    "total goals",
    "alternative total goals",
    "goals over/under",
    "goals over under",
    "goal line",
    "alternative goal line",
}
BTTS_MDS = {"both teams to score", "btts"}
HOME_MDS = {"home team total goals", "home team total", "home team over/under", "home team goals"}
AWAY_MDS = {"away team total goals", "away team total", "away team over/under", "away team goals"}
TEAM_TOTAL_MDS = {"team total goals"}  # label decides Home/Away

def extract_line_any(row: dict) -> Optional[float]:
    # prefer numeric 'handicap'
    h = row.get("handicap")
    try:
        if h is not None:
            return float(h)
    except Exception:
        pass
    # else scan common fields
    for field in ("total","name","original_label","label"):
        s = str(row.get(field) or "")
        m = re.search(r"([-+]?\d+(?:\.\d+)?)", s)
        if m:
            try: return float(m.group(1))
            except Exception: pass
    return None

def is_over_label(row: dict) -> bool:
    txt = " ".join([str(row.get(k) or "") for k in ("label","name","original_label")]).lower()
    return ("over" in txt) and ("under" not in txt)

def is_over25_row(row: dict) -> bool:
    if not is_over_label(row): return False
    ln = extract_line_any(row)
    return (ln is not None) and (abs(ln - 2.5) < 1e-6)

def is_btts_yes_row(row: dict) -> bool:
    # exact BTTS market only
    lab = (row.get("label") or "").strip().lower()
    nm  = (row.get("name") or "").strip().lower()
    orig= (row.get("original_label") or "").strip().lower()
    val = {lab, nm, orig}
    return ("yes" in val)

def is_team_over15_row_home_md(row: dict) -> bool:
    # Home/Away-specific markets where the "Over 1.5" is in label/name
    if not is_over_label(row): return False
    ln = extract_line_any(row)
    return (ln is not None) and (abs(ln - 1.5) < 1e-6)

def is_team_over15_row_teamtotal(row: dict) -> Optional[str]:
    # "Team Total Goals" (label tells side, name tells Over/Under)
    side = (row.get("label") or "").strip().lower()  # "home" / "away"
    if side not in {"home","away"}:
        return None
    name = (row.get("name") or "").lower()
    if ("over" in name) and ("under" not in name):
        ln = extract_line_any(row)
        if (ln is not None) and (abs(ln - 1.5) < 1e-6):
            return side
    return None

def price_of(row: dict) -> Optional[float]:
    v = row.get("value")
    try: return float(v)
    except Exception: return None

# ---------- Main ----------
def main():
    league_ids = discover_league_ids()

    flags_over25 = []  # (fixture, price, home%, away%, combo%)
    flags_btts   = []  # (fixture, price, home%, away%, combo%)
    flags_team15 = []  # (fixture, side, team, price, team%)

    # near-miss counters (passed form but failed price)
    nm_over25 = nm_btts = nm_team15 = 0

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

            # find team records (team stats & opponent allowed)
            h_rec = next((ts_idx[k] for k in ts_idx if team_names_match(home, k)), None)
            a_rec = next((ts_idx[k] for k in ts_idx if team_names_match(away, k)), None)
            h_opp = next((opp_idx[k] for k in opp_idx if team_names_match(away, k)), None)  # opp allowed vs home
            a_opp = next((opp_idx[k] for k in opp_idx if team_names_match(home, k)), None)  # opp allowed vs away
            if not (h_rec and a_rec and h_opp and a_opp):
                continue

            # series (latest->older)
            H_g  = as_int_list(h_rec.get("goals_last_n"))
            A_g  = as_int_list(a_rec.get("goals_last_n"))
            HogA = as_int_list(a_opp.get("opp_goals_last_n"))
            AogH = as_int_list(h_opp.get("opp_goals_last_n"))

            ok = lambda xs: len(xs) >= MIN_GAMES

            # Over 2.5 last-10 (team totals)
            h_over25 = over_k_from(H_g, HogA, 2.5) if ok(H_g) and ok(HogA) else (0,0,0.0)
            a_over25 = over_k_from(A_g, AogH, 2.5) if ok(A_g) and ok(AogH) else (0,0,0.0)
            pass_over25 = (h_over25[1] >= MIN_GAMES and a_over25[1] >= MIN_GAMES and
                           h_over25[2] >= THRESH_OVER25 and a_over25[2] >= THRESH_OVER25)

            # BTTS last-10
            h_btts = btts_from(H_g, HogA) if ok(H_g) and ok(HogA) else (0,0,0.0)
            a_btts = btts_from(A_g, AogH) if ok(A_g) and ok(AogH) else (0,0,0.0)
            pass_btts = (h_btts[1] >= MIN_GAMES and a_btts[1] >= MIN_GAMES and
                         h_btts[2] >= THRESH_BTTS and a_btts[2] >= THRESH_BTTS)

            # Team Over 1.5 last-10
            h_o15 = team_over_from(H_g, 2) if ok(H_g) else (0,0,0.0)
            a_o15 = team_over_from(A_g, 2) if ok(A_g) else (0,0,0.0)
            pass_h15 = (h_o15[1] >= MIN_GAMES and h_o15[2] >= THRESH_TEAM_O15)
            pass_a15 = (a_o15[1] >= MIN_GAMES and a_o15[2] >= THRESH_TEAM_O15)

            if not (pass_over25 or pass_btts or pass_h15 or pass_a15):
                continue

            odds_fx = odds_by_fixture.get(fid) or {}
            rows = odds_fx.get("odds") or []

            # ---- OVER 2.5 (best price) ----
            if pass_over25:
                best = None
                for r in rows:
                    md = nmarket(r.get("market_description") or "")
                    if md not in OVER25_MDS: 
                        continue
                    if not is_over25_row(r):
                        continue
                    p = price_of(r)
                    if p is None: 
                        continue
                    if (best is None) or (p > best): best = p
                if best is not None:
                    if best >= MIN_PRICE:
                        combo = (h_over25[2] + a_over25[2]) / 2.0
                        flags_over25.append((name, best, h_over25[2], a_over25[2], combo))
                    else:
                        nm_over25 += 1

            # ---- BTTS YES (best price) ----
            if pass_btts:
                best = None
                for r in rows:
                    md = nmarket(r.get("market_description") or "")
                    if md not in BTTS_MDS:
                        continue
                    if not is_btts_yes_row(r):
                        continue
                    p = price_of(r)
                    if p is None:
                        continue
                    if (best is None) or (p > best): best = p
                if best is not None:
                    if best >= MIN_PRICE:
                        combo = (h_btts[2] + a_btts[2]) / 2.0
                        flags_btts.append((name, best, h_btts[2], a_btts[2], combo))
                    else:
                        nm_btts += 1

            # ---- TEAM OVER 1.5 (Home) ----
            if pass_h15:
                best = None
                for r in rows:
                    md = nmarket(r.get("market_description") or "")
                    # Home-specific markets
                    if md in HOME_MDS:
                        if not is_team_over15_row_home_md(r): 
                            continue
                        p = price_of(r)
                        if p is None: continue
                        best = p if (best is None or p > best) else best
                    # Unified team total goals
                    elif md in TEAM_TOTAL_MDS:
                        side = is_team_over15_row_teamtotal(r)
                        if side == "home":
                            p = price_of(r)
                            if p is None: continue
                            best = p if (best is None or p > best) else best
                if best is not None:
                    if best >= MIN_PRICE:
                        flags_team15.append((name, "home", home, best, h_o15[2]))
                    else:
                        nm_team15 += 1

            # ---- TEAM OVER 1.5 (Away) ----
            if pass_a15:
                best = None
                for r in rows:
                    md = nmarket(r.get("market_description") or "")
                    if md in AWAY_MDS:
                        if not is_team_over15_row_home_md(r):
                            continue
                        p = price_of(r)
                        if p is None: continue
                        best = p if (best is None or p > best) else best
                    elif md in TEAM_TOTAL_MDS:
                        side = is_team_over15_row_teamtotal(r)
                        if side == "away":
                            p = price_of(r)
                            if p is None: continue
                            best = p if (best is None or p > best) else best
                if best is not None:
                    if best >= MIN_PRICE:
                        flags_team15.append((name, "away", away, best, a_o15[2]))
                    else:
                        nm_team15 += 1

    # --------- Sort (by price desc then edge proxy) ----------
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

    if DEBUG_NEARMISS:
        lines.append("")
        lines.append(f"(near-misses: O2.5={nm_over25}, BTTS={nm_btts}, TeamO1.5={nm_team15})  # passed form but below MIN_PRICE")

    OUT_TXT.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")

    # --------- Render POST (Markdown) ---------
    md = []
    md.append("I’ve collated a high-probability goals shortlist from teams’ last 10 league games and flagged **potential value** where the Bet365 market is **≥ MIN_PRICE**.")
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
