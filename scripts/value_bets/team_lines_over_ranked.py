#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Team Lines — OVER only, grouped into 4 categories:
 A) Overall COMBO ≥ COMBO_OVERALL_MIN
 B) Overall SINGLE ≥ SINGLE_OVERALL_MIN (either team OR opp-allowed)
 C) Home/Away COMBO ≥ COMBO_SPLIT_MIN
 D) Home/Away SINGLE ≥ SINGLE_SPLIT_MIN (either team OR opp-allowed)

Reads (local files only):
- Fixtures:                    data/fixtures/{league_id}.json
- Team offense series:         data/team_stats/by_league/{league_id}.json
- Opponent-allowed series:     data/team_opponent_stats/by_league/{league_id}.json
- Bet365 odds from Sportmonks: data/odds/b365/{league_id}.json

Markets considered (team totals): Team Shots, Team Shots on Target, Team Corners, Team Tackles.

Filters:
- Drop if decimal price < MIN_DEC_PRICE (default 1.20)
- For Shots/SOT/Corners: require team ML (Match Winner) <= TEAM_WIN_MAX (default 3.50). Tackles: no ML filter.
- Optional WINDOW_DAYS to limit fixtures by kickoff.

Overall vs Split:
- "Overall" uses all last-N entries available for team offense / opponent allowed.
- "Split" filters by venue:
    * If betting side = home:
        - Team split uses team locations == "home"
        - Opp-allowed split uses opponent locations == "away"
    * If betting side = away:
        - Team split uses team locations == "away"
        - Opp-allowed split uses opponent locations == "home"

Outputs one file:
- data/value_bets/team_lines_over_ranked.txt
  with 4 sections, ranked within each section by primary metric, then price.

Env (optional):
- LEAGUE_IDS         CSV of league IDs to scan (default: auto from fixtures dir)
- MIN_DEC_PRICE      minimum decimal price (default 1.20)
- TEAM_WIN_MAX       max ML to keep for Shots/SOT/Corners (default 3.50)
- WINDOW_DAYS        restrict fixtures to next N days (default 7, 0 = no limit)
- COMBO_OVERALL_MIN  default 0.70
- SINGLE_OVERALL_MIN default 0.75
- COMBO_SPLIT_MIN    default 0.70
- SINGLE_SPLIT_MIN   default 0.75
"""

import os, re, json, math, datetime as dt, unicodedata
from pathlib import Path
from typing import Dict, List, Optional, Tuple

ROOT = Path(".")
FIX_DIR   = ROOT / "data" / "fixtures"
TS_DIR    = ROOT / "data" / "team_stats" / "by_league"
OPP_DIR   = ROOT / "data" / "team_opponent_stats" / "by_league"
ODDS_DIR  = ROOT / "data" / "odds" / "b365"
OUT_DIR   = ROOT / "data" / "value_bets"; OUT_DIR.mkdir(parents=True, exist_ok=True)
OUT_FILE  = OUT_DIR / "team_lines_over_ranked.txt"

MIN_DEC_PRICE     = float(os.getenv("MIN_DEC_PRICE", "1.20"))
TEAM_WIN_MAX      = float(os.getenv("TEAM_WIN_MAX",  "3.50"))
WINDOW_DAYS       = int(os.getenv("WINDOW_DAYS",     "7"))
COMBO_OVERALL_MIN = float(os.getenv("COMBO_OVERALL_MIN",  "0.70"))
SINGLE_OVERALL_MIN= float(os.getenv("SINGLE_OVERALL_MIN", "0.75"))
COMBO_SPLIT_MIN   = float(os.getenv("COMBO_SPLIT_MIN",    "0.70"))
SINGLE_SPLIT_MIN  = float(os.getenv("SINGLE_SPLIT_MIN",   "0.75"))

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

def index_team_stats(blob: dict) -> Dict[str, dict]:
    m: Dict[str, dict] = {}
    for t in (blob.get("teams") or []):
        nm = t.get("team_name")
        if not nm: continue
        m[norm(nm)] = t
    return m

def index_team_opp_stats(blob: dict) -> Dict[str, dict]:
    m: Dict[str, dict] = {}
    for t in (blob.get("teams") or []):
        nm = t.get("team_name")
        if not nm: continue
        m[norm(nm)] = t
    return m

def as_float(x) -> Optional[float]:
    try: return float(str(x))
    except Exception: return None

# ---------- Market detection (Sportmonks Bet365 rows) ----------
TEAM_MD_TO_CANON = {
    "team shots": "shots_total",
    "team shots on target": "shots_on_target",
    "team corners": "corners",
    "team tackles": "tackles",
}

def detect_team_market(row: dict) -> Optional[str]:
    md = norm(row.get("market_description") or "")
    return TEAM_MD_TO_CANON.get(md)

def label_to_side(label: Optional[str]) -> Optional[str]:
    s = (label or "").strip().lower()
    if s in {"1","home"}: return "home"
    if s in {"2","away"}: return "away"
    return None

def row_pick_over(row: dict) -> bool:
    t = norm(row.get("total") or "")
    return ("over" in t) and ("under" not in t)

def row_line(row: dict) -> Optional[float]:
    h = row.get("handicap")
    v = as_float(h) if h is not None else None
    if v is not None: return v
    for field in ("total", "label", "name"):
        s = (row.get(field) or "").strip()
        m = re.search(r"([-+]?\d+(?:\.\d+)?)", s)
        if m:
            try: return float(m.group(1))
            except: pass
    return None

# ---------- Match Winner (ML) parsing ----------
MATCH_WINNER_ALIASES = {
    "match winner", "match result", "full time result", "fulltime result",
    "1x2", "result", "win/draw/win", "90 minutes", "3-way", "3 way", "regular time result"
}

def is_match_winner_row(row: dict) -> bool:
    md = norm(row.get("market_description") or "")
    return md in MATCH_WINNER_ALIASES

def extract_ml(rows: List[dict]) -> Tuple[Optional[float], Optional[float]]:
    """Return (home_ml, away_ml) from Bet365 rows for this fixture."""
    home_ml = None; away_ml = None
    for r in rows:
        if not is_match_winner_row(r): 
            continue
        side = label_to_side(r.get("label"))
        price = as_float(r.get("value"))
        if price is None or side not in {"home","away"}:
            continue
        if side == "home":
            home_ml = price if (home_ml is None or price < home_ml) else home_ml
        else:
            away_ml = price if (away_ml is None or price < away_ml) else away_ml
    return home_ml, away_ml

# ---------- Rates ----------
def pct(hits: int, n: int) -> Optional[float]:
    return (hits / n) if n else None

def over_rate(seq: List[int], line: float) -> Optional[Tuple[int,int,float]]:
    if not seq: return None
    thr = math.ceil(float(line))
    xs = [x for x in seq if isinstance(x,int)]
    if not xs: return None
    hits = sum(1 for x in xs if x >= thr)
    n = len(xs)
    return hits, n, hits / n

def filter_by_loc(seq: List[int], locs: List[str], want: str) -> List[int]:
    """Return values where locs[i] == want. Locs length assumed >= len(seq) or equal; guard with min()."""
    n = min(len(seq), len(locs))
    out = []
    for i in range(n):
        if (locs[i] or "").lower() == want:
            out.append(seq[i])
    return out

def combo_avg(team_over, oppA_over) -> Optional[Tuple[float, Tuple[int,int], Tuple[int,int]]]:
    """
    Average of team% and oppA% if both available.
    If only one side has data, use that %. If neither, return None.
    Returns (combo_pct, (team_hits, team_n), (opp_hits, opp_n))
    """
    t_pct = team_over[2] if team_over else None
    a_pct = oppA_over[2] if oppA_over else None
    if t_pct is None and a_pct is None:
        return None
    if t_pct is None:
        return a_pct, (0,0), (oppA_over[0], oppA_over[1])
    if a_pct is None:
        return t_pct, (team_over[0], team_over[1]), (0,0)
    combo = (t_pct + a_pct) / 2.0
    return combo, (team_over[0], team_over[1]), (oppA_over[0], oppA_over[1])

# ---------- Helpers to pull series ----------
def series_for(rec: dict, key: str) -> List[int]:
    if not rec: return []
    return [x for x in (rec.get(key) or []) if isinstance(x, int)]

def locs_for(rec: dict) -> List[str]:
    if not rec: return []
    return [str(x).lower() for x in (rec.get("locations_last_n") or [])]

# ---------- Main ----------
def main():
    # leagues to scan
    if os.getenv("LEAGUE_IDS"):
        league_ids = [int(x) for x in os.getenv("LEAGUE_IDS").split(",") if x.strip()]
    else:
        league_ids = discover_league_ids()

    # Buckets
    cat_A = []  # Overall COMBO ≥ min
    cat_B = []  # Overall SINGLE ≥ min
    cat_C = []  # H/A COMBO ≥ min
    cat_D = []  # H/A SINGLE ≥ min

    for lid in league_ids:
        fx_path   = FIX_DIR / f"{lid}.json"
        odds_path = ODDS_DIR / f"{lid}.json"
        ts_path   = TS_DIR / f"{lid}.json"
        opp_path  = OPP_DIR / f"{lid}.json"
        if not (fx_path.exists() and odds_path.exists() and ts_path.exists() and opp_path.exists()):
            continue

        fixtures = load_json(fx_path).get("fixtures") or []
        odds_blob = load_json(odds_path)
        odds_by_fixture = {int(f.get("fixture_id")): f for f in (odds_blob.get("fixtures") or []) if isinstance(f.get("fixture_id"), int)}

        ts_idx   = index_team_stats(load_json(ts_path))
        opp_idx  = index_team_opp_stats(load_json(opp_path))

        for fx in fixtures:
            if not isinstance(fx, dict): continue
            fid = fx.get("id") or fx.get("fixture_id")
            name = fx.get("name") or ""
            starting_at = fx.get("starting_at") or ""
            if WINDOW_DAYS and not within_window(starting_at, WINDOW_DAYS):
                continue

            home, away = parse_fixture_teams(name)
            if not home or not away: 
                continue

            odds_fx = odds_by_fixture.get(int(fid)) if isinstance(fid, int) else None
            if not odds_fx: 
                continue
            rows_odds = odds_fx.get("odds") or []
            if not isinstance(rows_odds, list): 
                continue

            # ML once per fixture
            home_ml, away_ml = extract_ml(rows_odds)

            for row in rows_odds:
                stat = detect_team_market(row)
                if stat is None:
                    continue
                side = label_to_side(row.get("label"))
                if side not in {"home","away"}:
                    txt = " ".join([str(row.get("name") or ""), str(row.get("total") or ""), str(row.get("original_label") or "")]).lower()
                    if "home" in txt and "away" not in txt: side = "home"
                    elif "away" in txt and "home" not in txt: side = "away"
                    else: 
                        continue
                if not row_pick_over(row):
                    continue

                line  = row_line(row)
                price = as_float(row.get("value"))
                if line is None or price is None or price < MIN_DEC_PRICE:
                    continue

                team_nm = home if side=="home" else away
                opp_nm  = away if side=="home" else home

                # ---- ML filter for Shots/SOT/Corners (drop big underdogs)
                team_ml = home_ml if side == "home" else away_ml
                if stat in {"shots_total","shots_on_target","corners"}:
                    if (team_ml is None) or (team_ml > TEAM_WIN_MAX):
                        continue  # drop big underdogs

                # Pull series + locs
                t_rec  = ts_idx.get(norm(team_nm))
                a_rec  = opp_idx.get(norm(opp_nm))
                t_locs = locs_for(t_rec)
                a_locs = locs_for(a_rec)

                # offense sequences
                if stat == "shots_total":
                    off_all  = series_for(t_rec,  "shots_total_last_n")
                    oppA_all = series_for(a_rec, "opp_shots_total_last_n")
                    tag = "Shots"
                    label = "Team Shots"
                elif stat == "shots_on_target":
                    off_all  = series_for(t_rec,  "shots_on_target_last_n")
                    oppA_all = series_for(a_rec, "opp_shots_on_target_last_n")
                    tag = "SOT"
                    label = "Team Shots on Target"
                elif stat == "corners":
                    off_all  = series_for(t_rec,  "corners_last_n")
                    oppA_all = series_for(a_rec, "opp_corners_last_n")
                    tag = "Corners"
                    label = "Team Corners"
                elif stat == "tackles":
                    off_all  = series_for(t_rec,  "tackles_last_n")
                    oppA_all = series_for(a_rec, "opp_tackles_last_n")
                    tag = "Tackles"
                    label = "Team Tackles"
                else:
                    continue

                # Overall metrics
                t_overall = over_rate(off_all, line)
                a_overall = over_rate(oppA_all, line)
                overall_combo = combo_avg(t_overall, a_overall)

                # Split metrics (use locations)
                want_team = "home" if side=="home" else "away"
                want_opp  = "away" if side=="home" else "home"  # opponent's venue in the fixture
                off_split  = filter_by_loc(off_all, t_locs, want_team)
                oppA_split = filter_by_loc(oppA_all, a_locs, want_opp)

                t_split = over_rate(off_split, line) if off_split else None
                a_split = over_rate(oppA_split, line) if oppA_split else None
                split_combo = combo_avg(t_split, a_split)

                # Determine categories hit
                hit_A = False; hit_B = False; hit_C = False; hit_D = False
                if overall_combo and overall_combo[0] is not None:
                    hit_A = overall_combo[0] >= COMBO_OVERALL_MIN
                if (t_overall and t_overall[2] is not None) or (a_overall and a_overall[2] is not None):
                    top = max([x for x in [t_overall[2] if t_overall else None, a_overall[2] if a_overall else None] if x is not None], default=None)
                    if top is not None:
                        hit_B = top >= SINGLE_OVERALL_MIN
                if split_combo and split_combo[0] is not None:
                    hit_C = split_combo[0] >= COMBO_SPLIT_MIN
                if (t_split and t_split[2] is not None) or (a_split and a_split[2] is not None):
                    top_s = max([x for x in [t_split[2] if t_split else None, a_split[2] if a_split else None] if x is not None], default=None)
                    if top_s is not None:
                        hit_D = top_s >= SINGLE_SPLIT_MIN

                if not (hit_A or hit_B or hit_C or hit_D):
                    continue

                def pack_rate(r):
                    if not r: return (0,0,None)
                    return (r[0], r[1], r[2])

                # Build record
                rec = {
                    "fixture": name,
                    "kickoff": starting_at,
                    "team": team_nm,
                    "opp": opp_nm,
                    "side": side,
                    "market": f"{label} {'Home' if side=='home' else 'Away'}",
                    "tag": tag,
                    "line": float(line),
                    "price": float(price),
                    "team_ml": float(team_ml) if team_ml is not None else None,

                    # overall
                    "t_overall": pack_rate(t_overall),
                    "a_overall": pack_rate(a_overall),
                    "overall_combo": overall_combo[0] if overall_combo else None,

                    # split
                    "t_split": pack_rate(t_split),
                    "a_split": pack_rate(a_split),
                    "split_combo": split_combo[0] if split_combo else None,
                }

                # Place into buckets (duplicates allowed across sections)
                if hit_A: cat_A.append(rec)
                if hit_B: cat_B.append(rec)
                if hit_C: cat_C.append(rec)
                if hit_D: cat_D.append(rec)

    # --------- ranking & render helpers ----------
    def pct_str(p: Optional[float]) -> str:
        return f"{(p*100):5.1f}%" if isinstance(p, float) else "  n/a "

    def rate_str(r: Tuple[int,int,Optional[float]]) -> str:
        hits, n, p = r
        if n == 0 or p is None:
            return "n/a"
        return f"{hits}/{n} ({pct_str(p)})"

    def rank_cat_A(r):  # Overall COMBO desc, price desc
        return (-(r["overall_combo"] if r["overall_combo"] is not None else -1), -r["price"], r["fixture"], r["team"], r["tag"], r["line"])
    def rank_cat_B(r):  # Overall SINGLE (max of two) desc, price desc
        top = max([x for x in [r["t_overall"][2], r["a_overall"][2]] if x is not None], default=-1)
        return (-top, -r["price"], r["fixture"], r["team"], r["tag"], r["line"])
    def rank_cat_C(r):  # Split COMBO desc, price desc
        return (-(r["split_combo"] if r["split_combo"] is not None else -1), -r["price"], r["fixture"], r["team"], r["tag"], r["line"])
    def rank_cat_D(r):  # Split SINGLE desc, price desc
        top = max([x for x in [r["t_split"][2], r["a_split"][2]] if x is not None], default=-1)
        return (-top, -r["price"], r["fixture"], r["team"], r["tag"], r["line"])

    cat_A.sort(key=rank_cat_A)
    cat_B.sort(key=rank_cat_B)
    cat_C.sort(key=rank_cat_C)
    cat_D.sort(key=rank_cat_D)

    # --------- render ---------
    lines = []
    lines.append(f"Generated at (UTC): {dt.datetime.utcnow().isoformat(timespec='seconds')}")
    lines.append(
        "Rules: "
        f"COMBO_OVERALL≥{int(COMBO_OVERALL_MIN*100)}% OR "
        f"SINGLE_OVERALL≥{int(SINGLE_OVERALL_MIN*100)}% OR "
        f"H/A_COMBO≥{int(COMBO_SPLIT_MIN*100)}% OR "
        f"H/A_SINGLE≥{int(SINGLE_SPLIT_MIN*100)}%"
    )
    lines.append(f"Window={WINDOW_DAYS} days | Min price={MIN_DEC_PRICE:.2f} | ML filter (Shots/SOT/Corners): ≤ {TEAM_WIN_MAX:.2f}")
    lines.append("")
    lines.append("===== TEAM LINES — OVER (grouped) =====")

    def section(title: str, rows: List[dict], mode: str):
        lines.append("")
        lines.append(title)
        if not rows:
            lines.append("  (none)")
            return
        for r in rows:
            ml_str = f" | ML={r['team_ml']:.3f}" if isinstance(r.get("team_ml"), float) else ""
            lines.append(
                f" • {r['team']} — {r['tag']} Over {r['line']:.1f} @ {r['price']:.3f} | {r['fixture']} | side={r['side']}{ml_str}"
            )
            if mode == "A":
                lines.append(
                    f"    Overall: team {rate_str(r['t_overall'])} | oppA {rate_str(r['a_overall'])} | combo={pct_str(r['overall_combo'])}"
                )
            elif mode == "B":
                t = r['t_overall'][2]; a = r['a_overall'][2]
                top_side = "team" if (t or -1) >= (a or -1) else "oppA"
                top_pct = max([x for x in [t, a] if x is not None], default=None)
                lines.append(
                    f"    Overall: team {rate_str(r['t_overall'])} | oppA {rate_str(r['a_overall'])} | strongest={top_side} {pct_str(top_pct)}"
                )
            elif mode == "C":
                lines.append(
                    f"    H/A:     team {rate_str(r['t_split'])} | oppA {rate_str(r['a_split'])} | combo={pct_str(r['split_combo'])}"
                )
            elif mode == "D":
                t = r['t_split'][2]; a = r['a_split'][2]
                top_side = "team" if (t or -1) >= (a or -1) else "oppA"
                top_pct = max([x for x in [t, a] if x is not None], default=None)
                lines.append(
                    f"    H/A:     team {rate_str(r['t_split'])} | oppA {rate_str(r['a_split'])} | strongest={top_side} {pct_str(top_pct)}"
                )

    section("A) OVERALL — COMBO ≥ threshold", cat_A, "A")
    section("B) OVERALL — SINGLE side ≥ threshold", cat_B, "B")
    section("C) HOME/AWAY — COMBO ≥ threshold", cat_C, "C")
    section("D) HOME/AWAY — SINGLE side ≥ threshold", cat_D, "D")

    OUT_FILE.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    print("\n".join(lines))

if __name__ == "__main__":
    main()
