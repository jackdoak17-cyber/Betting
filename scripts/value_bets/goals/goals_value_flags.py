```python
#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Goals markets — value flags (LOCAL files only; no API calls)

Selection rules:
  • Form threshold hit (team offense vs opponent-allowed, fixed 70%)
  • Bet365 best decimal price >= MIN_PRICE (default 1.30)
  • H2H gate: market must have LANDED in BOTH of the last 2 H2Hs (strict)

Markets:
  - Over 2.5: Alternative Total Goals > Total Goals > Goals Over/Under (FT only)
    (EXCLUDES "Goal Line")
  - BTTS (Yes): standalone BTTS FT markets

Inputs:
  - data/fixtures/{league_id}.json
  - data/team_stats/by_league/{league_id}.json
  - data/team_opponent_stats/by_league/{league_id}.json
  - data/odds/b365/{league_id}.json
  - data/h2h/by_league/{league_id}.json

Outputs:
  - data/value_bets/goals_value_flags.txt
  - posts/BTTS&O2.5_1.md

Env:
  - MIN_PRICE (default 1.30)
  - WINDOW_DAYS (default 7; 0 = no date filter)
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
OUT_MD    = POSTS_DIR / "BTTS&O2.5_1.md"

MIN_PRICE          = float(os.getenv("MIN_PRICE", "1.30"))
WINDOW_DAYS        = int(os.getenv("WINDOW_DAYS", "7"))  # 0 = no date filter

# Fixed criteria per request
LAST_N             = 10
MIN_GAMES          = LAST_N
THRESH_OVER25      = 0.70
THRESH_BTTS        = 0.70
H2H_LAST2_REQUIRED = True

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

    return False

# ---------- Main ----------
def main():
    league_ids = discover_league_ids()

    # Over 2.5: (fixture, price, home%, away%, combo%, h2h_last2)
    flags_over25 = []
    # BTTS: (fixture, price, home%, away%, combo%, h2h_last2)
    flags_btts   = []

    near_o25 = 0
    near_btts = 0

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

            def clip(xs):
                return as_int_list(xs)[:LAST_N] if LAST_N > 0 else as_int_list(xs)

            H_g   = clip(h_rec.get("goals_last_n"))
            A_g   = clip(a_rec.get("goals_last_n"))
            HogA  = clip(h_opp.get("opp_goals_last_n"))  # AWAY conceded series (used vs home attack)
            AogH  = clip(a_opp.get("opp_goals_last_n"))  # HOME conceded series (used vs away attack)

            def ok_len(x): return len(x) >= MIN_GAMES

            # Form gates
            h_over25 = over_k_from(H_g, HogA, 2.5) if ok_len(H_g) and ok_len(HogA) else (0,0,0.0)
            a_over25 = over_k_from(A_g, AogH, 2.5) if ok_len(A_g) and ok_len(AogH) else (0,0,0.0)

            h_btts = btts_from(H_g, HogA) if ok_len(H_g) and ok_len(HogA) else (0,0,0.0)
            a_btts = btts_from(A_g, AogH) if ok_len(A_g) and ok_len(AogH) else (0,0,0.0)

            pass_over25 = (h_over25[1] >= MIN_GAMES and a_over25[1] >= MIN_GAMES and
                           h_over25[2] >= THRESH_OVER25 and a_over25[2] >= THRESH_OVER25)
            pass_btts   = (h_btts[1]   >= MIN_GAMES and a_btts[1]   >= MIN_GAMES and
                           h_btts[2]   >= THRESH_BTTS   and a_btts[2]   >= THRESH_BTTS)

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

    # --------- Sort (price desc then probability desc, then name) ----------
    flags_over25.sort(key=lambda x: (-x[1], -x[4], x[0]))
    flags_btts.sort(key=lambda x: (-x[1], -x[4], x[0]))

    # --------- Render TEXT ---------
    now_iso = dt.datetime.utcnow().isoformat(timespec="seconds")
    def pct(p): return f"{p*100:.1f}%"

    lines = []
    lines.append(f"Generated at (UTC): {now_iso}")
    lines.append(f"Rules: MIN_PRICE>={MIN_PRICE:.2f}, LAST_N={LAST_N}, MIN_GAMES>={MIN_GAMES}, thresholds: O2.5>={int(THRESH_OVER25*100)}%, BTTS>={int(THRESH_BTTS*100)}%")
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

    # Near misses (passed form gates but below MIN_PRICE)
    nm_parts = []
    if near_o25: nm_parts.append(f"O2.5={near_o25}")
    if near_btts: nm_parts.append(f"BTTS={near_btts}")
    if nm_parts:
        lines.append("")
        lines.append(f"(near-misses: {', '.join(nm_parts)})  # passed form but below MIN_PRICE")

    OUT_TXT.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")

    # --------- Render POST (Markdown) ---------
    def pcts_int(p):
        return f"{round(p*100):d}%"

    def fixture_line(name: str, hp: float, ap: float) -> str:
        home, away = parse_fixture_teams(name)
        hpct = pcts_int(hp)
        apct = pcts_int(ap)
        if home and away:
            return f"{home} ({hpct}) vs {away} ({apct})"
        return f"{name} ({hpct}/{apct})"

    md = []
    md.append("⚽OVER 2.5 + BTTS – List #1")
    md.append("")

    md.append("📊Over 2.5 goals📊")
    md.append("(Both teams have had at least 2.5 goals in 70% of their last 10 & in both of their last 2 h2h)")
    if not flags_over25:
        md.append("No qualifying fixtures")
    for name, price, hp, ap, combo, h2h_tail in flags_over25:
        md.append(f"{fixture_line(name, hp, ap)} @ {price:.2f}")

    md.append("")
    md.append("📊Both teams to score📊")
    md.append("(BTTS has landed in 70% of their last 10 & in both of their last 2 h2h)")
    if not flags_btts:
        md.append("No qualifying fixtures")
    for name, price, hp, ap, combo, h2h_tail in flags_btts:
        md.append(f"{fixture_line(name, hp, ap)} @ {price:.2f}")

    md.append("")
    md.append("*Uses league data only, odds correct time of run")
    md.append("*If you spot any errors let me know so I can investigate and improve the list")
    md.append("*If you think I should add any parameters let me know")
    md.append("*Leave a like if you find these useful :)")

    OUT_MD.write_text("\n".join(md).rstrip() + "\n", encoding="utf-8")

    print("\n".join(lines))
    print(f"\nWrote:\n  • {OUT_TXT}\n  • {OUT_MD}")

if __name__ == "__main__":
    main()
```
