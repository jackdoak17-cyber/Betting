#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Goals markets — value flags (LOCAL files only)

Flags when BOTH hold:
  • Form gate hits (default 70% using last-10 series)
  • Bet365 price >= MIN_PRICE (default 1.30)

Markets:
  - Over 2.5 (full-time totals only; exact 2.5 line; accepts 'alternative totals')
  - BTTS Yes (90 mins only)
  - Team Over 1.5 (home/away) — only if that team is favourite on Match Winner

Enrichment:
  - Appends H2H last-2 summary (O2.5 x/y, BTTS x/y) if data/h2h/{minId}_{maxId}.json exists.

Inputs:
  data/fixtures/{league_id}.json
  data/team_stats/by_league/{league_id}.json
  data/team_opponent_stats/by_league/{league_id}.json
  data/odds/b365/{league_id}.json
  data/h2h/{minId}_{maxId}.json  (optional; produced by scripts/h2h_last2_fetch.py)

Outputs:
  data/value_bets/goals_value_flags.txt
  posts/value_bets_goals.md
"""

import os, re, json, math, datetime as dt, unicodedata
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# -------- Config --------
ROOT = Path(".")
FIX_DIR   = ROOT / "data" / "fixtures"
TS_DIR    = ROOT / "data" / "team_stats" / "by_league"
OPP_DIR   = ROOT / "data" / "team_opponent_stats" / "by_league"
ODDS_DIR  = ROOT / "data" / "odds" / "b365"
H2H_DIR   = ROOT / "data" / "h2h"
OUT_DIR   = ROOT / "data" / "value_bets"; OUT_DIR.mkdir(parents=True, exist_ok=True)
OUT_TXT   = OUT_DIR / "goals_value_flags.txt"
POSTS_DIR = ROOT / "posts"; POSTS_DIR.mkdir(parents=True, exist_ok=True)
OUT_MD    = POSTS_DIR / "value_bets_goals.md"

MIN_PRICE        = float(os.getenv("MIN_PRICE", "1.30"))
MIN_GAMES        = int(os.getenv("MIN_GAMES", "6"))
THRESH_OVER25    = float(os.getenv("THRESH_OVER25", "0.70"))
THRESH_BTTS      = float(os.getenv("THRESH_BTTS",   "0.70"))
THRESH_TEAM_O15  = float(os.getenv("THRESH_TEAM_O15","0.70"))
WINDOW_DAYS      = int(os.getenv("WINDOW_DAYS", "7"))

# -------- String utils --------
def strip_accents(s: str) -> str:
    return ''.join(c for c in unicodedata.normalize('NFD', s or '') if unicodedata.category(c) != 'Mn')

def norm(s: str) -> str:
    s = strip_accents(s or "").lower()
    s = re.sub(r"[^a-z0-9\s\.-]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s

GENERIC_TOK = {"fc","cf","afc","sc","cd","ud","ac","as","ss","ssc","us","uc","rc","rcd","ca","the","club","de","del","la","las","los","calcio","united","city","saint","st","bk"}

def team_tokens(name: str):
    return {t for t in set(norm(name).split()) if t not in GENERIC_TOK}

def team_names_match(a: str, b: str) -> bool:
    if not a or not b: return False
    ta, tb = team_tokens(a), team_tokens(b)
    if not ta or not tb: return False
    if ta == tb or ta.issubset(tb) or tb.issubset(ta): return True
    inter = ta & tb; uni = ta | tb
    return (len(inter) / max(1, len(uni)) >= 0.5) or (len(inter) >= 2)

def parse_fixture_teams(name: str) -> Tuple[str,str]:
    if not name: return "",""
    for sep in (" vs ", " v ", " - ", " VS ", " Vs "):
        if sep in name:
            a, b = name.split(sep, 1)
            return a.strip(), b.strip()
    return "",""

def within_window(starting_at: str, days: int) -> bool:
    if not days: return True
    try:
        t = dt.datetime.strptime(starting_at[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=dt.timezone.utc)
    except Exception:
        return True
    now = dt.datetime.now(dt.timezone.utc)
    return now <= t <= (now + dt.timedelta(days=days))

# -------- IO helpers --------
def discover_league_ids() -> List[int]:
    out = []
    for p in FIX_DIR.glob("*.json"):
        try: out.append(int(p.stem))
        except: pass
    return sorted(set(out))

def load_json(p: Path) -> dict:
    try: return json.loads(p.read_text(encoding="utf-8"))
    except Exception: return {}

def index_team(blob: dict) -> Dict[str, dict]:
    m: Dict[str, dict] = {}
    for t in (blob.get("teams") or []):
        nm = t.get("team_name")
        if nm: m[norm(nm)] = t
    return m

# -------- Series helpers --------
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
    thr = math.ceil(k + 1e-9)
    hits = sum(1 for i in range(n) if (goals[i] + opp_goals[i]) >= thr)
    return hits, n, (hits / n) if n else 0.0

def team_over_from(goals: List[int], thr: int) -> Tuple[int,int,float]:
    xs = as_int_list(goals)
    hits = sum(1 for x in xs if x >= thr)
    n = len(xs)
    return hits, n, (hits / n) if n else 0.0

# -------- Odds parsing --------
def row_line(row: dict) -> Optional[float]:
    h = row.get("handicap")
    try:
        return float(h) if h is not None else None
    except Exception:
        for field in ("total","label","name","original_label"):
            s = str(row.get(field) or "")
            m = re.search(r"([-+]?\d+(?:\.\d+)?)", s)
            if m:
                try: return float(m.group(1))
                except: pass
    return None

def price_of(row: dict) -> Optional[float]:
    v = row.get("value")
    try: return float(v)
    except Exception: return None

# Allow any market_description; filter by context/line instead (Bet365 strings vary).
BANNED_SUBSTRINGS_FT = {"first half","1st half","2nd half","second half","half time","ht","1h","2h",
                        "home team","away team","team corners","corners","cards","bookings","offsides",
                        "penalties","extra time","overtime","both halves","win to nil","clean sheet"}

def is_fulltime_totals_context(text: str) -> bool:
    t = text.lower()
    return not any(b in t for b in BANNED_SUBSTRINGS_FT)

def is_over25_row(row: dict) -> bool:
    blob = " ".join(str(row.get(k) or "") for k in ("market_description","label","name","total","original_label"))
    if not is_fulltime_totals_context(blob): 
        return False
    ln = row_line(row)
    if ln is None or abs(ln - 2.5) > 1e-6:
        return False
    t = blob.lower()
    return ("over" in t) and ("under" not in t)

def is_btts_yes_row(row: dict) -> bool:
    blob = " ".join(str(row.get(k) or "") for k in ("market_description","label","name","original_label")).lower()
    if any(x in blob for x in ("first half","1st half","2nd half","both halves","no goal")):
        return False
    return ("both teams to score" in blob or "btts" in blob) and ("yes" in blob) and ("no" not in blob)

HOME_MD = {"home team over/under","home team total goals","home team goals","home team total",
           "alternative home team total goals","alternative home team goals"}
AWAY_MD = {"away team over/under","away team total goals","away team goals","away team total",
           "alternative away team total goals","alternative away team goals"}

def is_team_over15_row(row: dict, want_side: str) -> bool:
    blob = " ".join(str(row.get(k) or "") for k in ("market_description","label","name","total","original_label")).lower()
    if any(x in blob for x in ("first half","1st half","2nd half","both halves")):
        return False
    ln = row_line(row)
    if ln is None or abs(ln - 1.5) > 1e-6:
        return False
    if ("over" not in blob) or ("under" in blob):
        return False
    md = norm(row.get("market_description") or "")
    if want_side == "home":
        return (md in HOME_MD) or ("home team" in blob)
    else:
        return (md in AWAY_MD) or ("away team" in blob)

# Match Winner parsing (more synonyms)
MATCH_WINNER_MD = {"match winner","match result","full time result","fulltime result","1x2","result",
                   "win/draw/win","90 minutes","3-way","3 way","regular time result","match odds",
                   "to win","moneyline"}

def extract_match_winner_prices(rows: List[dict]) -> Tuple[Optional[float], Optional[float], Optional[float]]:
    home = draw = away = None
    def upd(side: str, p: Optional[float]):
        nonlocal home, draw, away
        if p is None: return
        if side == "home": home = p if (home is None or p < home) else home
        elif side == "draw": draw = p if (draw is None or p < draw) else draw
        elif side == "away": away = p if (away is None or p < away) else away
    for r in rows:
        md = norm(r.get("market_description") or "")
        if md not in MATCH_WINNER_MD: 
            continue
        p = price_of(r)
        if p is None: 
            continue
        fields = " ".join(str(r.get(k) or "") for k in ("label","name","original_label")).lower()
        fields = fields.replace("home team","home").replace("away team","away")
        if re.search(r"\b(home|1)\b", fields) and "away" not in fields: upd("home", p); continue
        if re.search(r"\b(away|2)\b", fields) and "home" not in fields: upd("away", p); continue
        if re.search(r"\b(draw|x)\b", fields): upd("draw", p); continue
    return home, draw, away

# -------- H2H helpers --------
def extract_team_ids_from_fixture(fx: dict) -> Tuple[Optional[int], Optional[int]]:
    home_id = away_id = None
    for p in (fx.get("participants") or []):
        try: tid = int(p.get("id"))
        except Exception: continue
        loc = ((p.get("meta") or {}).get("location") or (p.get("meta") or {}).get("venue") or "").lower()
        if loc == "home": home_id = tid
        elif loc == "away": away_id = tid
    if home_id is None:
        for k in ("home_team_id","localteam_id","home_id","localteamid"):
            v = fx.get(k); 
            if isinstance(v,(int,str)) and str(v).isdigit(): home_id = int(v); break
    if away_id is None:
        for k in ("away_team_id","visitorteam_id","away_id","visitorteamid"):
            v = fx.get(k); 
            if isinstance(v,(int,str)) and str(v).isdigit(): away_id = int(v); break
    return home_id, away_id

def h2h_last2_summary(h_id: Optional[int], a_id: Optional[int]) -> Optional[str]:
    if not (isinstance(h_id,int) and isinstance(a_id,int)): return None
    lo, hi = (h_id, a_id) if h_id <= a_id else (a_id, h_id)
    p = H2H_DIR / f"{lo}_{hi}.json"
    if not p.exists(): return None
    try: j = json.loads(p.read_text(encoding="utf-8"))
    except Exception: return None
    s = (j.get("summary") or {})
    o25h, o25n = int(s.get("o25_hits",0)), int(s.get("o25_n",0))
    bttsh, bttsn = int(s.get("btts_hits",0)), int(s.get("btts_n",0))
    return f"H2H last2: O2.5 {o25h}/{o25n}, BTTS {bttsh}/{bttsn}"

# -------- Main --------
def main():
    leagues = discover_league_ids()

    flags_over25 = []  # (fixture, price, h_pct, a_pct, combo, h2h)
    flags_btts   = []  # (fixture, price, h_pct, a_pct, combo, h2h)
    flags_team15 = []  # (fixture, side, team, price, team_pct, ml_price)

    near_o25 = near_btts = near_o15 = 0

    for lid in leagues:
        fx_path, ts_path, opp_path, odds_path = (
            FIX_DIR/f"{lid}.json", TS_DIR/f"{lid}.json", OPP_DIR/f"{lid}.json", ODDS_DIR/f"{lid}.json"
        )
        if not all(p.exists() for p in (fx_path, ts_path, opp_path, odds_path)): 
            continue

        fixtures = load_json(fx_path).get("fixtures") or []
        ts_idx   = index_team(load_json(ts_path))
        opp_idx  = index_team(load_json(opp_path))
        odds     = load_json(odds_path)
        odds_by_fixture = {int(f.get("fixture_id")): f for f in (odds.get("fixtures") or []) if isinstance(f.get("fixture_id"), int)}

        for fx in fixtures:
            fid = int(fx.get("id") or fx.get("fixture_id") or 0)
            if not fid: continue
            name = fx.get("name") or ""
            starting_at = fx.get("starting_at") or ""
            if WINDOW_DAYS and not within_window(starting_at, WINDOW_DAYS): 
                continue

            home, away = parse_fixture_teams(name)
            if not (home and away): continue

            # team indices
            h_rec = next((ts_idx[k] for k in ts_idx if team_names_match(home, k)), None)
            a_rec = next((ts_idx[k] for k in ts_idx if team_names_match(away, k)), None)
            h_opp = next((opp_idx[k] for k in opp_idx if team_names_match(away, k)), None)
            a_opp = next((opp_idx[k] for k in opp_idx if team_names_match(home, k)), None)
            if not (h_rec and a_rec and h_opp and a_opp): 
                continue

            H_g  = as_int_list(h_rec.get("goals_last_n"));   HogA = as_int_list(a_opp.get("opp_goals_last_n"))
            A_g  = as_int_list(a_rec.get("goals_last_n"));   AogH = as_int_list(h_opp.get("opp_goals_last_n"))

            def ok_len(x): return len(x) >= MIN_GAMES

            h_over25 = over_k_from(H_g, HogA, 2.5) if ok_len(H_g) and ok_len(HogA) else (0,0,0.0)
            a_over25 = over_k_from(A_g, AogH, 2.5) if ok_len(A_g) and ok_len(AogH) else (0,0,0.0)

            h_btts = btts_from(H_g, HogA) if ok_len(H_g) and ok_len(HogA) else (0,0,0.0)
            a_btts = btts_from(A_g, AogH) if ok_len(A_g) and ok_len(AogH) else (0,0,0.0)

            h_o15 = team_over_from(H_g, 2) if ok_len(H_g) else (0,0,0.0)
            a_o15 = team_over_from(A_g, 2) if ok_len(A_g) else (0,0,0.0)

            pass_over25 = (h_over25[1] >= MIN_GAMES and a_over25[1] >= MIN_GAMES and h_over25[2] >= THRESH_OVER25 and a_over25[2] >= THRESH_OVER25)
            pass_btts   = (h_btts[1]   >= MIN_GAMES and a_btts[1]   >= MIN_GAMES and h_btts[2]   >= THRESH_BTTS   and a_btts[2]   >= THRESH_BTTS)
            pass_h15    = (h_o15[1]    >= MIN_GAMES and h_o15[2]    >= THRESH_TEAM_O15)
            pass_a15    = (a_o15[1]    >= MIN_GAMES and a_o15[2]    >= THRESH_TEAM_O15)

            odds_fx = odds_by_fixture.get(fid) or {}
            rows = odds_fx.get("odds") or []

            # Match winner for favourite check
            home_ml, draw_ml, away_ml = extract_match_winner_prices(rows)

            # Over 2.5 (full-time totals, any label set)
            if pass_over25:
                best = None
                for r in rows:
                    if is_over25_row(r):
                        p = price_of(r)
                        if p is None: 
                            continue
                        if (best is None) or (p > best): 
                            best = p
                combo = (h_over25[2] + a_over25[2]) / 2.0
                if best is None or best < MIN_PRICE:
                    near_o25 += 1
                else:
                    hid, aid = extract_team_ids_from_fixture(fx)
                    h2h = h2h_last2_summary(hid, aid)
                    flags_over25.append((name, best, h_over25[2], a_over25[2], combo, h2h))

            # BTTS Yes
            if pass_btts:
                best = None
                for r in rows:
                    if is_btts_yes_row(r):
                        p = price_of(r)
                        if p is None: 
                            continue
                        if (best is None) or (p > best): 
                            best = p
                combo = (h_btts[2] + a_btts[2]) / 2.0
                if best is None or best < MIN_PRICE:
                    near_btts += 1
                else:
                    hid, aid = extract_team_ids_from_fixture(fx)
                    h2h = h2h_last2_summary(hid, aid)
                    flags_btts.append((name, best, h_btts[2], a_btts[2], combo, h2h))

            # Team Over 1.5 — favourites only
            # Home fav
            if pass_h15 and isinstance(home_ml, float) and isinstance(away_ml, float) and home_ml < away_ml:
                best = None
                for r in rows:
                    if is_team_over15_row(r, "home"):
                        p = price_of(r)
                        if p is None: 
                            continue
                        if (best is None) or (p > best): 
                            best = p
                if best is None or best < MIN_PRICE:
                    near_o15 += 1
                else:
                    flags_team15.append((name, "home", home, best, h_o15[2], home_ml))
            # Away fav
            if pass_a15 and isinstance(home_ml, float) and isinstance(away_ml, float) and away_ml < home_ml:
                best = None
                for r in rows:
                    if is_team_over15_row(r, "away"):
                        p = price_of(r)
                        if p is None: 
                            continue
                        if (best is None) or (p > best): 
                            best = p
                if best is None or best < MIN_PRICE:
                    near_o15 += 1
                else:
                    flags_team15.append((name, "away", away, best, a_o15[2], away_ml))

    # Sorting
    flags_over25.sort(key=lambda x: (-x[1], -x[4], x[0]))
    flags_btts.sort(  key=lambda x: (-x[1], -x[4], x[0]))
    flags_team15.sort(key=lambda x: (-x[3], -x[4], x[0], x[1]))

    # Render
    now_iso = dt.datetime.utcnow().isoformat(timespec="seconds")
    pct = lambda p: f"{p*100:.1f}%"

    lines = []
    lines.append(f"Generated at (UTC): {now_iso}")
    lines.append(f"Rules: MIN_PRICE>={MIN_PRICE:.2f}, MIN_GAMES>={MIN_GAMES}, thresholds: O2.5>={int(THRESH_OVER25*100)}%, BTTS>={int(THRESH_BTTS*100)}%, TeamO1.5>={int(THRESH_TEAM_O15*100)}%")
    lines.append("Notes: Team Over 1.5 is only flagged if that team is favourite on Match Winner (strictly shorter price).")
    lines.append("")
    lines.append("=== Over 2.5 — value flags ===")
    if not flags_over25: lines.append("  (none)")
    for name, price, hp, ap, combo, h2h in flags_over25:
        tail = f" | {h2h}" if h2h else ""
        lines.append(f" • {name} — Over 2.5 @ {price:.2f} | H {pct(hp)} / A {pct(ap)} | combo {pct(combo)}{tail}")

    lines.append("\n=== BTTS (Yes) — value flags ===")
    if not flags_btts: lines.append("  (none)")
    for name, price, hp, ap, combo, h2h in flags_btts:
        tail = f" | {h2h}" if h2h else ""
        lines.append(f" • {name} — BTTS Yes @ {price:.2f} | H {pct(hp)} / A {pct(ap)} | combo {pct(combo)}{tail}")

    lines.append("\n=== Team Over 1.5 — value flags (favourites only) ===")
    if not flags_team15: lines.append("  (none)")
    for name, side, team, price, tp, mlp in flags_team15:
        ml_str = f" | ML {mlp:.2f}" if isinstance(mlp, float) else ""
        lines.append(f" • {team} — Team Over 1.5 ({side}) @ {price:.2f} | team {pct(tp)}{ml_str} | {name}")

    if any((near_o25, near_btts, near_o15)):
        bits = []
        if near_o25: bits.append(f"O2.5={near_o25}")
        if near_btts: bits.append(f"BTTS={near_btts}")
        if near_o15: bits.append(f"TeamO1.5={near_o15}")
        lines.append(f"\n(near-misses: {', '.join(bits)})  # passed form but below MIN_PRICE")

    OUT_TXT.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")

    md = []
    md.append("I’ve collated a high-probability goals shortlist from teams’ last 10 league games and flagged **potential value** based on local Bet365 prices.\n")
    md.append(f"_Form gates:_ Over 2.5 ≥ {int(THRESH_OVER25*100)}%, BTTS ≥ {int(THRESH_BTTS*100)}%, Team Over 1.5 ≥ {int(THRESH_TEAM_O15*100)}% (≥{MIN_GAMES} games).")
    md.append("_Notes:_ Team Over 1.5 only when that team is favourite on the Match Winner market.\n")
    md.append("### Over 2.5 — value flags")
    if not flags_over25: md.append("- (none)")
    for name, price, hp, ap, combo, h2h in flags_over25:
        tail = f" — _{h2h}_" if h2h else ""
        md.append(f"- **{name}** — **Over 2.5 @ {price:.2f}** (H {pct(hp)} / A {pct(ap)}; combo {pct(combo)}){tail}")
    md.append("\n### BTTS (Yes) — value flags")
    if not flags_btts: md.append("- (none)")
    for name, price, hp, ap, combo, h2h in flags_btts:
        tail = f" — _{h2h}_" if h2h else ""
        md.append(f"- **{name}** — **BTTS Yes @ {price:.2f}** (H {pct(hp)} / A {pct(ap)}; combo {pct(combo)}){tail}")
    md.append("\n### Team Over 1.5 — value flags (favourites only)")
    if not flags_team15: md.append("- (none)")
    for name, side, team, price, tp, mlp in flags_team15:
        ml_str = f"; ML {mlp:.2f}" if isinstance(mlp, float) else ""
        md.append(f"- **{team}** — **Team Over 1.5 @ {price:.2f}** ({side}; {pct(tp)}{ml_str}) — {name}")

    OUT_MD.write_text("\n".join(md).rstrip() + "\n", encoding="utf-8")
    print("\n".join(lines))
    print(f"\nWrote:\n  • {OUT_TXT}\n  • {OUT_MD}")

if __name__ == "__main__":
    main()
