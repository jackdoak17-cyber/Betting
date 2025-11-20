#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Goals markets — value flags (LOCAL files only; no API calls)

Selection rules:
  • Form threshold hit (team offense vs opponent-allowed, default 70%)
  • Bet365 best decimal price >= MIN_PRICE (default 1.30)
  • H2H gate: market must have LANDED in BOTH of the last 2 H2Hs (strict)
  • Team Over 1.5: show ONLY if that team is the favourite (1X2/Match Result)
    and display team % + opponent conceded % + combined %

Markets:
  - Over 2.5: Alternative Total Goals > Total Goals > Goals Over/Under (FT only)
    (EXCLUDES "Goal Line")
  - BTTS (Yes): standalone BTTS FT markets
  - Team Over 1.5: FT team totals @ 1.5 line (correct side)

Inputs:
  - data/fixtures/{league_id}.json
  - data/team_stats/by_league/{league_id}.json
  - data/team_opponent_stats/by_league/{league_id}.json
  - data/odds/b365/{league_id}.json
  - data/h2h/by_league/{league_id}.json

Outputs:
  - data/value_bets/goals_value_flags.txt
  - posts/value_bets_goals.md

Env:
  - MIN_PRICE (default 1.30)
  - MIN_GAMES (default 6)
  - THRESH_OVER25 / THRESH_BTTS / THRESH_TEAM_O15 (default 0.70)
  - WINDOW_DAYS (default 7; 0 = no date filter)
  - H2H_LAST2_REQUIRED (default "1")  # if "0", disables the H2H gate
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
H2H_DIR   = ROOT / "data" / "h2h" / "by_league"

OUT_DIR   = ROOT / "data" / "value_bets"; OUT_DIR.mkdir(parents=True, exist_ok=True)
OUT_TXT   = OUT_DIR / "goals_value_flags.txt"

POSTS_DIR = ROOT / "posts"; POSTS_DIR.mkdir(parents=True, exist_ok=True)
OUT_MD    = POSTS_DIR / "value_bets_goals.md"

MIN_PRICE        = float(os.getenv("MIN_PRICE", "1.30"))
MIN_GAMES        = int(os.getenv("MIN_GAMES", "6"))
THRESH_OVER25    = float(os.getenv("THRESH_OVER25", "0.70"))
THRESH_BTTS      = float(os.getenv("THRESH_BTTS",   "0.70"))
THRESH_TEAM_O15  = float(os.getenv("THRESH_TEAM_O15","0.70"))
WINDOW_DAYS      = int(os.getenv("WINDOW_DAYS", "7"))  # 0 = no date filter
H2H_LAST2_REQUIRED = (os.getenv("H2H_LAST2_REQUIRED", "1").strip() != "0")

# ---------- String / name utils ----------
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

def team_names_match(a: str, b_norm_key: str) -> bool:
    if not a or not b_norm_key: return False
    ta, tb = team_tokens(a), set(b_norm_key.split())
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
    thr = math.ceil(k + 1e-9)   # 2.5 -> 3
    hits = sum(1 for i in range(n) if (goals[i] + opp_goals[i]) >= thr)
    return hits, n, (hits / n) if n else 0.0

def team_over_from(goals: List[int], thr: int) -> Tuple[int,int,float]:
    xs = as_int_list(goals)
    hits = sum(1 for x in xs if x >= thr)
    n = len(xs)
    return hits, n, (hits / n) if n else 0.0

# ---------- Odds parsing ----------
def price_of(row: dict) -> Optional[float]:
    v = row.get("value")
    try: return float(v)
    except Exception: return None

def numeric_line_from_row(row: dict) -> Optional[float]:
    # Try explicit numeric fields first; then parse inside strings
    for field in ("handicap", "name", "total", "label", "original_label"):
        s = row.get(field)
        if s is None:
            continue
        try:
            return float(s)
        except Exception:
            m = re.search(r"([-+]?\d+(?:\.\d+)?)", str(s))
            if m:
                try: return float(m.group(1))
                except: pass
    return None

def is_full_time(market_desc_norm: str) -> bool:
    s = (market_desc_norm or "")
    return not any(k in s for k in ("1st half", "first half", "2nd half", "second half", "half time", "1st period", "2nd period"))

def nmarket(s: str) -> str:
    return norm(s)

# --- Market whitelists / priorities ---
O25_PRIORITY = [
    "alternative total goals",
    "total goals",
    "goals over/under",
    "goals over under",
]  # excludes "goal line"

BTTS_STANDALONE_MDS = {
    "both teams to score", "both teams to score?",
    "btts", "btts (yes/no)", "both teams to score - yes/no"
}
BTTS_TEAMS_TO_SCORE_MD = "teams to score"  # label/name must be 'Both Teams'

HOME_TTG_MDS = {"home team total goals", "home team over/under", "home team goals", "home team - total goals"}
AWAY_TTG_MDS = {"away team total goals", "away team over/under", "away team goals", "away team - total goals"}
TEAM_TTG_MD  = "team total goals"  # label '1'/'2' for home/away

def text_has_over(row: dict) -> bool:
    txt = " ".join([str(row.get("label") or ""), str(row.get("name") or ""),
                    str(row.get("total") or ""), str(row.get("original_label") or "")]).lower()
    return ("over" in txt) and ("under" not in txt)

def is_btts_yes_row(row: dict) -> bool:
    md = nmarket(row.get("market_description") or "")
    if not is_full_time(md): return False
    if "result/both teams to score" in md or "total goals/both teams to score" in md:
        return False
    if md in BTTS_STANDALONE_MDS:
        txt = " ".join([str(row.get("label") or ""), str(row.get("name") or ""),
                        str(row.get("original_label") or "")]).lower()
        return ("yes" in txt) and ("no" not in txt)
    if md == BTTS_TEAMS_TO_SCORE_MD:
        txt = " ".join([str(row.get("label") or ""), str(row.get("name") or "")]).strip().lower()
        return txt == "both teams"
    return False

def side_from_label(label: Optional[str]) -> Optional[str]:
    s = (label or "").strip().lower()
    if s in {"1","home","local","localteam","home team"}: return "home"
    if s in {"2","away","visitor","visitorteam","away team"}: return "away"
    return None

def is_team_over15_row(row: dict, want_side: str) -> bool:
    md = nmarket(row.get("market_description") or "")
    if not is_full_time(md): return False
    if md == TEAM_TTG_MD:
        side = side_from_label(row.get("label"))
        if side != want_side: return False
        ln = numeric_line_from_row(row)
        return (ln is not None) and abs(ln - 1.5) < 1e-6 and text_has_over(row)
    if want_side == "home" and md in HOME_TTG_MDS:
        ln = numeric_line_from_row(row)
        return (ln is not None) and abs(ln - 1.5) < 1e-6 and text_has_over(row)
    if want_side == "away" and md in AWAY_TTG_MDS:
        ln = numeric_line_from_row(row)
        return (ln is not None) and abs(ln - 1.5) < 1e-6 and text_has_over(row)
    return False

def best_price_o25(rows: List[dict]) -> Optional[float]:
    for md in O25_PRIORITY:
        best = None
        for r in rows:
            rmd = nmarket(r.get("market_description") or "")
            if rmd != md: 
                continue
            if not is_full_time(rmd):
                continue
            ln = numeric_line_from_row(r)
            if (ln is None) or abs(ln - 2.5) > 1e-6:
                continue
            if not text_has_over(r):
                continue
            p = price_of(r)
            if p is None:
                continue
            if (best is None) or (p > best):
                best = p
        if best is not None:
            return best
    return None

def best_price(rows: List[dict], pred) -> Optional[float]:
    best = None
    for r in rows:
        try:
            if not pred(r): 
                continue
            p = price_of(r)
            if p is None:
                continue
            if (best is None) or (p > best):
                best = p
        except Exception:
            continue
    return best

# ---------- Favourite (1X2) ----------
FTR_MDS = {
    "match winner","match result","full time result","1x2","1x2 full time",
    "win/draw/win","to win"
}

def favourite_side(rows: List[dict]) -> Optional[str]:
    home_p = None; away_p = None
    for r in rows:
        md = nmarket(r.get("market_description") or "")
        if md not in FTR_MDS: 
            continue
        if not is_full_time(md):
            continue
        lbl = (r.get("label") or r.get("name") or "").strip().lower()
        p = price_of(r)
        if p is None:
            continue
        if lbl in {"1","home","local","localteam","home team"}:
            home_p = p if home_p is None else min(home_p, p)
        elif lbl in {"2","away","visitor","visitorteam","away team"}:
            away_p = p if away_p is None else min(away_p, p)
        else:
            pass  # ignore draw
    if home_p is None and away_p is None:
        return None
    if home_p is None: return "away"
    if away_p is None: return "home"
    return "home" if home_p < away_p else "away"

# ---------- H2H helpers ----------
def h2h_index_for_league(lid: int) -> Dict[int, dict]:
    p = H2H_DIR / f"{lid}.json"
    blob = load_json(p)
    idx = {}
    for fx in (blob.get("fixtures") or []):
        fid = fx.get("fixture_id")
        if isinstance(fid, int):
            idx[fid] = fx
    return idx

def _last2_pairs(fx_h2h: Optional[dict]) -> List[Tuple[str,int,int]]:
    if not fx_h2h:
        return []
    meta = (fx_h2h.get("lastN_meta") or [])[:2]  # newest -> older
    out = []
    for m in meta:
        try:
            dtstr = (m.get("starting_at") or "")[:10]
            hg = int(m.get("home_goals"))
            ag = int(m.get("away_goals"))
            out.append((dtstr, hg, ag))
        except Exception:
            pass
    return out

def h2h_last2_str(fx_h2h: Optional[dict]) -> str:
    pairs = _last2_pairs(fx_h2h)
    if not pairs:
        return "n/a"
    return ", ".join(f"{d} {h}–{a}" for d,h,a in pairs)

def _clean_last2_ints(ints: List[Optional[int]]) -> List[Optional[int]]:
    return (ints or [])[:2]

def h2h_gate_pass_strict(fx_h2h: Optional[dict], market: str, side: Optional[str]) -> bool:
    """
    STRICT: require BOTH of the last two H2Hs (if two exist) to satisfy the market.
    If fewer than 2 known, FAIL the gate (strict as requested).
    """
    if not H2H_LAST2_REQUIRED:
        return True
    if not fx_h2h:
        return False  # strict

    Vh = ((fx_h2h.get("vectors") or {}).get("home") or {})
    Va = ((fx_h2h.get("vectors") or {}).get("away") or {})
    Hg2 = _clean_last2_ints(Vh.get("goals") or [])
    Ag2 = _clean_last2_ints(Va.get("goals") or [])

    pairs = []
    for i in range(min(len(Hg2), len(Ag2))):
        h, a = Hg2[i], Ag2[i]
        if isinstance(h, int) and isinstance(a, int):
            pairs.append((h, a))

    if len(pairs) < 2:
        return False  # strict: need 2 known

    if market == "o25":
        return all((h + a) >= 3 for h,a in pairs)

    if market == "btts":
        return all((h > 0 and a > 0) for h,a in pairs)

    if market == "team_o15":
        if side == "home":
            g2 = [g for g in Hg2 if isinstance(g, int)]
            if len(g2) < 2: return False
            return all(g >= 2 for g in g2[:2])
        if side == "away":
            g2 = [g for g in Ag2 if isinstance(g, int)]
            if len(g2) < 2: return False
            return all(g >= 2 for g in g2[:2])
        return False

    return False

# ---------- Main ----------
def main():
    league_ids = discover_league_ids()

    # Over 2.5: (fixture, price, home%, away%, combo%, h2h_last2)
    flags_over25 = []
    # BTTS: (fixture, price, home%, away%, combo%, h2h_last2)
    flags_btts   = []
    # Team O1.5: (fixture, side, team, price, team%, opp_conc%, combo%, h2h_last2)
    flags_team15 = []

    near_o25 = 0
    near_btts = 0
    near_t15 = 0

    for lid in league_ids:
        fx_path   = FIX_DIR / f"{lid}.json"
        ts_path   = TS_DIR  / f"{lid}.json"
        opp_path  = OPP_DIR / f"{lid}.json"
        odds_path = ODDS_DIR/ f"{lid}.json"
        if not (fx_path.exists() and ts_path.exists() and opp_path.exists() and odds_path.exists()):
            continue

        fixtures  = load_json(fx_path).get("fixtures") or []
        ts_idx    = index_team(load_json(ts_path))
        opp_idx   = index_team(load_json(opp_path))
        odds_blob = load_json(odds_path)
        odds_by_fixture = {int(f.get("fixture_id")): f for f in (odds_blob.get("fixtures") or []) if isinstance(f.get("fixture_id"), int)}
        h2h_idx   = h2h_index_for_league(lid)

        for fx in fixtures:
            fid = int(fx.get("id") or fx.get("fixture_id") or 0)
            if not fid:
                continue
            name = fx.get("name") or ""
            starting_at = fx.get("starting_at") or ""
            if WINDOW_DAYS and not within_window(starting_at, WINDOW_DAYS):
                continue

            home, away = parse_fixture_teams(name)
            if not (home and away):
                continue

            # find team records (offense + opponent-allowed)
            h_rec = next((ts_idx[k] for k in ts_idx if team_names_match(home, k)), None)
            a_rec = next((ts_idx[k] for k in ts_idx if team_names_match(away, k)), None)
            h_opp = next((opp_idx[k] for k in opp_idx if team_names_match(away, k)), None)  # away conceded (for home)
            a_opp = next((opp_idx[k] for k in opp_idx if team_names_match(home, k)), None)  # home conceded (for away)
            if not (h_rec and a_rec and h_opp and a_opp):
                continue

            H_g   = as_int_list(h_rec.get("goals_last_n"))
            A_g   = as_int_list(a_rec.get("goals_last_n"))
            HogA  = as_int_list(h_opp.get("opp_goals_last_n"))  # AWAY conceded series (used vs home attack)
            AogH  = as_int_list(a_opp.get("opp_goals_last_n"))  # HOME conceded series (used vs away attack)

            def ok_len(x): return len(x) >= MIN_GAMES

            # Form gates
            h_over25 = over_k_from(H_g, HogA, 2.5) if ok_len(H_g) and ok_len(HogA) else (0,0,0.0)
            a_over25 = over_k_from(A_g, AogH, 2.5) if ok_len(A_g) and ok_len(AogH) else (0,0,0.0)

            h_btts = btts_from(H_g, HogA) if ok_len(H_g) and ok_len(HogA) else (0,0,0.0)
            a_btts = btts_from(A_g, AogH) if ok_len(A_g) and ok_len(AogH) else (0,0,0.0)

            h_o15_team = team_over_from(H_g, 2) if ok_len(H_g) else (0,0,0.0)
            a_o15_team = team_over_from(A_g, 2) if ok_len(A_g) else (0,0,0.0)
            # Opponent conceded >=2 (might have fewer than MIN_GAMES; still report)
            h_o15_opp  = team_over_from(HogA, 2) if HogA else (0,0,0.0)
            a_o15_opp  = team_over_from(AogH, 2) if AogH else (0,0,0.0)

            pass_over25 = (h_over25[1] >= MIN_GAMES and a_over25[1] >= MIN_GAMES and
                           h_over25[2] >= THRESH_OVER25 and a_over25[2] >= THRESH_OVER25)
            pass_btts   = (h_btts[1]   >= MIN_GAMES and a_btts[1]   >= MIN_GAMES and
                           h_btts[2]   >= THRESH_BTTS   and a_btts[2]   >= THRESH_BTTS)
            pass_h15    = (h_o15_team[1] >= MIN_GAMES and h_o15_team[2] >= THRESH_TEAM_O15)
            pass_a15    = (a_o15_team[1] >= MIN_GAMES and a_o15_team[2] >= THRESH_TEAM_O15)

            odds_fx = odds_by_fixture.get(fid) or {}
            rows = odds_fx.get("odds") or []
            fx_h2h = h2h_idx.get(fid)
            h2h_tail = h2h_last2_str(fx_h2h)

            # ---- Over 2.5 ----
            if pass_over25:
                p = best_price_o25(rows)
                if p is not None and p >= MIN_PRICE and h2h_gate_pass_strict(fx_h2h, "o25", None):
                    combo = (h_over25[2] + a_over25[2]) / 2.0
                    flags_over25.append((name, p, h_over25[2], a_over25[2], combo, h2h_tail))
                elif p is not None:
                    near_o25 += 1

            # ---- BTTS Yes ----
            if pass_btts:
                p = best_price(rows, is_btts_yes_row)
                if p is not None and p >= MIN_PRICE and h2h_gate_pass_strict(fx_h2h, "btts", None):
                    combo = (h_btts[2] + a_btts[2]) / 2.0
                    flags_btts.append((name, p, h_btts[2], a_btts[2], combo, h2h_tail))
                elif p is not None:
                    near_btts += 1

            # ---- Team Over 1.5 (Home) — only if favourite ----
            fav = favourite_side(rows)
            if pass_h15 and fav == "home":
                p = best_price(rows, lambda r: is_team_over15_row(r, "home"))
                if p is not None and p >= MIN_PRICE and h2h_gate_pass_strict(fx_h2h, "team_o15", "home"):
                    team_p = h_o15_team[2]
                    opp_p  = h_o15_opp[2]
                    combo  = (team_p + opp_p) / 2.0
                    flags_team15.append((name, "home", home, p, team_p, opp_p, combo, h2h_tail))
                elif p is not None:
                    near_t15 += 1

            # ---- Team Over 1.5 (Away) — only if favourite ----
            if pass_a15 and fav == "away":
                p = best_price(rows, lambda r: is_team_over15_row(r, "away"))
                if p is not None and p >= MIN_PRICE and h2h_gate_pass_strict(fx_h2h, "team_o15", "away"):
                    team_p = a_o15_team[2]
                    opp_p  = a_o15_opp[2]
                    combo  = (team_p + opp_p) / 2.0
                    flags_team15.append((name, "away", away, p, team_p, opp_p, combo, h2h_tail))
                elif p is not None:
                    near_t15 += 1

    # --------- Sort (price desc then probability desc, then name) ----------
    flags_over25.sort(key=lambda x: (-x[1], -x[4], x[0]))
    flags_btts.sort(key=lambda x: (-x[1], -x[4], x[0]))
    flags_team15.sort(key=lambda x: (-x[3], -x[6], x[0], x[1]))

    # --------- Render TEXT ---------
    now_iso = dt.datetime.utcnow().isoformat(timespec="seconds")
    def pct(p): return f"{p*100:.1f}%"

    lines = []
    lines.append(f"Generated at (UTC): {now_iso}")
    lines.append(f"Rules: MIN_PRICE>={MIN_PRICE:.2f}, MIN_GAMES>={MIN_GAMES}, thresholds: O2.5>={int(THRESH_OVER25*100)}%, BTTS>={int(THRESH_BTTS*100)}%, TeamO1.5>={int(THRESH_TEAM_O15*100)}%")
    if H2H_LAST2_REQUIRED:
        lines.append("H2H gate: must land in BOTH of the last 2 H2Hs.")
    lines.append("")

    lines.append("=== Over 2.5 — value flags ===")
    if not flags_over25: lines.append("  (none)")
    for name, price, hp, ap, combo, h2h_tail in flags_over25:
        lines.append(f" • {name} — Over 2.5 @ {price:.2f} | H {pct(hp)} / A {pct(ap)} | combo {pct(combo)} | H2H last2: {h2h_tail}")

    lines.append("")
    lines.append("=== BTTS (Yes) — value flags ===")
    if not flags_btts: lines.append("  (none)")
    for name, price, hp, ap, combo, h2h_tail in flags_btts:
        lines.append(f" • {name} — BTTS Yes @ {price:.2f} | H {pct(hp)} / A {pct(ap)} | combo {pct(combo)} | H2H last2: {h2h_tail}")

    lines.append("")
    lines.append("=== Team Over 1.5 — value flags (only favourites) ===")
    if not flags_team15: lines.append("  (none)")
    for name, side, team, price, team_p, opp_p, combo, h2h_tail in flags_team15:
        lines.append(f" • {team} — Team Over 1.5 ({side}) @ {price:.2f} | team {pct(team_p)} / opp conceded {pct(opp_p)} | combo {pct(combo)} | {name} | H2H last2: {h2h_tail}")

    # Near misses (passed form gates but below MIN_PRICE)
    nm_parts = []
    if near_o25: nm_parts.append(f"O2.5={near_o25}")
    if near_btts: nm_parts.append(f"BTTS={near_btts}")
    if near_t15: nm_parts.append(f"TeamO1.5={near_t15}")
    if nm_parts:
        lines.append("")
        lines.append(f"(near-misses: {', '.join(nm_parts)})  # passed form but below MIN_PRICE")

    OUT_TXT.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")

    # --------- Render POST (Markdown) ---------
    md = []
    md.append("Shortlist from recent team form (offense vs conceded), priced at or above the configured minimum.")
    if H2H_LAST2_REQUIRED:
        md.append("\n_Strict H2H gate: market must have landed in **both** of the last 2 H2Hs._\n")
    md.append(f"_Form gates:_ Over 2.5 ≥ {int(THRESH_OVER25*100)}%, BTTS ≥ {int(THRESH_BTTS*100)}%, Team Over 1.5 ≥ {int(THRESH_TEAM_O15*100)}% (≥{MIN_GAMES} games). Min price = **{MIN_PRICE:.2f}**.\n")
    def pcts(p): return f"{p*100:.1f}%"

    md.append("### Over 2.5 — value flags")
    if not flags_over25: md.append("- (none)")
    for name, price, hp, ap, combo, h2h_tail in flags_over25:
        md.append(f"- **{name}** — **Over 2.5 @ {price:.2f}** (H {pcts(hp)} / A {pcts(ap)}; combo {pcts(combo)}; H2H: {h2h_tail})")

    md.append("\n### BTTS (Yes) — value flags")
    if not flags_btts: md.append("- (none)")
    for name, price, hp, ap, combo, h2h_tail in flags_btts:
        md.append(f"- **{name}** — **BTTS Yes @ {price:.2f}** (H {pcts(hp)} / A {pcts(ap)}; combo {pcts(combo)}; H2H: {h2h_tail})")

    md.append("\n### Team Over 1.5 — value flags (only favourites)")
    if not flags_team15: md.append("- (none)")
    for name, side, team, price, team_p, opp_p, combo, h2h_tail in flags_team15:
        md.append(f"- **{team}** — **Team Over 1.5 @ {price:.2f}** ({side}; team {pcts(team_p)} / opp conceded {pcts(opp_p)}; combo {pcts(combo)}; H2H: {h2h_tail}) — {name}")

    OUT_MD.write_text("\n".join(md).rstrip() + "\n", encoding="utf-8")

    print("\n".join(lines))
    print(f"\nWrote:\n  • {OUT_TXT}\n  • {OUT_MD}")

if __name__ == "__main__":
    main()
