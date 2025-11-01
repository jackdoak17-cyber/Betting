#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Team Lines — OVER only (ranked with overall & H/A rules)

Reads (local files only):
- Fixtures:                    data/fixtures/{league_id}.json
- Team offense series:         data/team_stats/by_league/{league_id}.json  (has locations_last_n)
- Opponent-allowed series:     data/team_opponent_stats/by_league/{league_id}.json  (has locations_last_n)
- Bet365 odds from Sportmonks: data/odds/b365/{league_id}.json

Markets considered (team totals): Team Shots, Team Shots on Target, Team Corners, Team Tackles.

Filters (as before):
- Drop if decimal price < MIN_DEC_PRICE (default 1.20)
- For Shots/SOT/Corners: require chosen team ML (Match Winner) <= TEAM_WIN_MAX (default 3.50). Tackles: no ML filter.
- Optional WINDOW_DAYS to limit fixtures by kickoff.

NEW selection rules (any of the following qualifies a pick):
1) Overall combined% (avg of team-over% and opp-allowed-over%) >= COMBO_OVERALL_MIN (default 0.70).
2) Overall single-side% (team OR opp-allowed) >= SINGLE_OVERALL_MIN (default 0.75).
3) Home/Away combined% (using venue-filtered samples for both sides) >= COMBO_SPLIT_MIN (default 0.70).
4) Home/Away single-side% (team OR opp-allowed, venue-filtered) >= SINGLE_SPLIT_MIN (default 0.75).

Notes:
- "Overall" uses all samples (draws irrelevant here as this is an OVER line).
- "Home/Away" filters the team offense by the team’s venue in those past matches,
  and the opponent-allowed by the opponent’s venue (opposite of the team’s side in the upcoming match).

Output:
- data/value_bets/team_lines_over_ranked.txt  (sorted by the strongest qualifying metric desc, then price desc)

Env (optional):
- LEAGUE_IDS           CSV of league IDs to scan (default: auto from fixtures dir)
- MIN_DEC_PRICE        minimum decimal price (default 1.20)
- TEAM_WIN_MAX         max ML to keep for Shots/SOT/Corners (default 3.50)
- WINDOW_DAYS          restrict fixtures to next N days (default 7, 0 = no limit)
- COMBO_OVERALL_MIN    default 0.70
- SINGLE_OVERALL_MIN   default 0.75
- COMBO_SPLIT_MIN      default 0.70
- SINGLE_SPLIT_MIN     default 0.75
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

MIN_DEC_PRICE      = float(os.getenv("MIN_DEC_PRICE", "1.20"))
TEAM_WIN_MAX       = float(os.getenv("TEAM_WIN_MAX",  "3.50"))
WINDOW_DAYS        = int(os.getenv("WINDOW_DAYS",     "7"))
COMBO_OVERALL_MIN  = float(os.getenv("COMBO_OVERALL_MIN",  "0.70"))
SINGLE_OVERALL_MIN = float(os.getenv("SINGLE_OVERALL_MIN", "0.75"))
COMBO_SPLIT_MIN    = float(os.getenv("COMBO_SPLIT_MIN",    "0.70"))
SINGLE_SPLIT_MIN   = float(os.getenv("SINGLE_SPLIT_MIN",   "0.75"))

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
        # tolerate ISO or "YYYY-MM-DD HH:MM:SS"
        s = (starting_at or "").replace("T", " ").replace("Z", "")
        dt_utc = dt.datetime.strptime(s[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=dt.timezone.utc)
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
    for field in ("total", "label"):
        s = (row.get(field) or "").strip()
        m = re.search(r"([-+]?\d+(?:\.\d+)?)", s)
        if m:
            try: return float(m.group(1))
            except: pass
    return None

# ---------- Match Winner (ML) parsing from Sportmonks rows ----------
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

# ---------- Rate helpers ----------
def over_rate(seq: List[int], line: float) -> Optional[Tuple[int,int,float]]:
    """Return (hits, n, pct) where pct is hits/n; uses ceil(line) threshold (>=)."""
    if not seq: return None
    thr = math.ceil(float(line))
    xs = [x for x in seq if isinstance(x, int)]
    if not xs: return None
    hits = sum(1 for x in xs if x >= thr)
    n = len(xs)
    return hits, n, (hits / n) if n else None

def filter_series_by_loc(values: List[int], locs: List[str], want: str) -> List[int]:
    """Filter aligned series by location ('home'|'away'). Unknowns are ignored."""
    out = []
    m = min(len(values), len(locs))
    want = (want or "").lower()
    for i in range(m):
        if (locs[i] or "").lower() == want and isinstance(values[i], int):
            out.append(values[i])
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

# ---------- Main ----------
def main():
    # leagues to scan
    if os.getenv("LEAGUE_IDS"):
        league_ids = [int(x) for x in os.getenv("LEAGUE_IDS").split(",") if x.strip()]
    else:
        league_ids = discover_league_ids()

    rows_out: List[dict] = []

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

            # --- compute ML once per fixture
            home_ml, away_ml = extract_ml(rows_odds)

            # --- lookup records once per fixture
            home_t = ts_idx.get(norm(home)); away_t = ts_idx.get(norm(away))
            home_o = opp_idx.get(norm(home)); away_o = opp_idx.get(norm(away))
            if not (home_t and away_t and home_o and away_o):
                continue

            # convenience: pull location arrays
            home_locs = (home_t.get("locations_last_n") or []) if isinstance(home_t, dict) else []
            away_locs = (away_t.get("locations_last_n") or []) if isinstance(away_t, dict) else []
            homeopp_locs = (home_o.get("locations_last_n") or []) if isinstance(home_o, dict) else []
            awayopp_locs = (away_o.get("locations_last_n") or []) if isinstance(away_o, dict) else []

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

                # ML filter for Shots/SOT/Corners (drop big underdogs). Tackles exempt.
                if stat in {"shots_total","shots_on_target","corners"}:
                    team_ml = home_ml if side == "home" else away_ml
                    if (team_ml is None) or (team_ml > TEAM_WIN_MAX):
                        continue
                else:
                    team_ml = None  # Tackles (or other future stats)

                # pull offense/allowed series (overall)
                def series_for(rec: dict, key: str) -> List[int]:
                    if not rec: return []
                    return [x for x in (rec.get(key) or []) if isinstance(x, int)]

                if stat == "shots_total":
                    label = "Team Shots"
                    team_off_key = "shots_total_last_n"
                    opp_allow_key = "opp_shots_total_last_n"
                elif stat == "shots_on_target":
                    label = "Team Shots on Target"
                    team_off_key = "shots_on_target_last_n"
                    opp_allow_key = "opp_shots_on_target_last_n"
                elif stat == "corners":
                    label = "Team Corners"
                    team_off_key = "corners_last_n"
                    opp_allow_key = "opp_corners_last_n"
                elif stat == "tackles":
                    label = "Team Tackles"
                    team_off_key = "tackles_last_n"
                    opp_allow_key = "opp_tackles_last_n"
                else:
                    continue

                # choose which team's offense/allowed to use according to 'side'
                if side == "home":
                    team_nm, opp_nm = home, away
                    team_rec, opp_rec = home_t, away_o
                    team_locs_use, opp_locs_use = home_locs, awayopp_locs
                    opp_side_for_split = "away"  # opponent will be away
                else:
                    team_nm, opp_nm = away, home
                    team_rec, opp_rec = away_t, home_o
                    team_locs_use, opp_locs_use = away_locs, homeopp_locs
                    opp_side_for_split = "home"  # opponent will be home

                team_series_all = series_for(team_rec, team_off_key)
                oppA_series_all = series_for(opp_rec,  opp_allow_key)

                # venue-filtered (split)
                team_series_split = filter_series_by_loc(
                    series_for(team_rec, team_off_key),
                    team_locs_use,
                    side  # the team's own venue in the upcoming game
                )
                oppA_series_split = filter_series_by_loc(
                    series_for(opp_rec, opp_allow_key),
                    opp_locs_use,
                    opp_side_for_split  # opponent’s own venue in the upcoming game
                )

                # compute rates
                t_over_all   = over_rate(team_series_all, line)
                a_over_all   = over_rate(oppA_series_all, line)
                t_over_split = over_rate(team_series_split, line)
                a_over_split = over_rate(oppA_series_split, line)

                combo_all = combo_avg(t_over_all, a_over_all)
                combo_split = combo_avg(t_over_split, a_over_split)

                # pull pct helpers
                def pct(ofr): return ofr[2] if ofr else None

                t_pct_all = pct(t_over_all)
                a_pct_all = pct(a_over_all)
                c_pct_all = combo_all[0] if combo_all else None

                t_pct_split = pct(t_over_split)
                a_pct_split = pct(a_over_split)
                c_pct_split = combo_split[0] if combo_split else None

                # selection triggers (collect reasons)
                triggers = []

                if c_pct_all is not None and c_pct_all >= COMBO_OVERALL_MIN:
                    triggers.append("OVR:combo>=70")
                if (t_pct_all is not None and t_pct_all >= SINGLE_OVERALL_MIN) or \
                   (a_pct_all is not None and a_pct_all >= SINGLE_OVERALL_MIN):
                    triggers.append("OVR:one>=75")
                if c_pct_split is not None and c_pct_split >= COMBO_SPLIT_MIN:
                    triggers.append("H/A:combo>=70")
                if (t_pct_split is not None and t_pct_split >= SINGLE_SPLIT_MIN) or \
                   (a_pct_split is not None and a_pct_split >= SINGLE_SPLIT_MIN):
                    triggers.append("H/A:one>=75")

                if not triggers:
                    continue

                # choose rank metric preference by trigger strength
                if "H/A:combo>=70" in triggers and c_pct_split is not None:
                    rank_metric = c_pct_split
                elif "OVR:combo>=70" in triggers and c_pct_all is not None:
                    rank_metric = c_pct_all
                elif "H/A:one>=75" in triggers:
                    rank_metric = max(x for x in [t_pct_split, a_pct_split] if x is not None)
                else:
                    rank_metric = max(x for x in [t_pct_all, a_pct_all] if x is not None)

                rows_out.append({
                    "fixture": name,
                    "kickoff": starting_at,
                    "team": team_nm,
                    "opp": opp_nm,
                    "side": side,
                    "market": f"{label} {'Home' if side=='home' else 'Away'}",
                    "stat_key": stat,
                    "line": float(line),
                    "price": float(price),
                    "team_ml": float(team_ml) if team_ml is not None else None,
                    # metrics
                    "t_hits_all": t_over_all[0] if t_over_all else None,
                    "t_n_all":    t_over_all[1] if t_over_all else None,
                    "t_pct_all":  t_pct_all,
                    "a_hits_all": a_over_all[0] if a_over_all else None,
                    "a_n_all":    a_over_all[1] if a_over_all else None,
                    "a_pct_all":  a_pct_all,
                    "c_pct_all":  c_pct_all,
                    "t_hits_split": t_over_split[0] if t_over_split else None,
                    "t_n_split":    t_over_split[1] if t_over_split else None,
                    "t_pct_split":  t_pct_split,
                    "a_hits_split": a_over_split[0] if a_over_split else None,
                    "a_n_split":    a_over_split[1] if a_over_split else None,
                    "a_pct_split":  a_pct_split,
                    "c_pct_split":  c_pct_split,
                    "triggers": triggers,
                    "rank_metric": rank_metric,
                })

    # Rank by strongest qualifying metric, then price
    rows_out.sort(key=lambda r: (-r["rank_metric"], -r["price"], r["fixture"], r["team"], r["stat_key"], r["line"]))

    # Render
    lines = []
    lines.append(f"Generated at (UTC): {dt.datetime.utcnow().isoformat(timespec='seconds')}")
    lines.append(
        "Rules: COMBO_OVERALL≥{:.0f}% OR SINGLE_OVERALL≥{:.0f}% OR H/A_COMBO≥{:.0f}% OR H/A_SINGLE≥{:.0f}%"
        .format(COMBO_OVERALL_MIN*100, SINGLE_OVERALL_MIN*100, COMBO_SPLIT_MIN*100, SINGLE_SPLIT_MIN*100)
    )
    lines.append(f"Window={WINDOW_DAYS} days | Min price={MIN_DEC_PRICE:.2f} | ML filter (Shots/SOT/Corners): ≤ {TEAM_WIN_MAX:.2f}")
    lines.append("")
    lines.append("===== TEAM LINES — OVER (ranked by strongest metric then price) =====")

    if not rows_out:
        lines.append("  No qualifying team OVER lines found.\n")
    else:
        for r in rows_out:
            def fmt_pair(hits, n):
                if hits is None or n in (None, 0): return "n/a"
                pct = (hits / n) * 100.0
                return f"{hits}/{n} ({pct:5.1f}%)"

            ml_str = f" | ML={r['team_ml']:.3f}" if isinstance(r.get("team_ml"), float) else ""

            lines.append(
                f" • {r['team']} — {r['market']} Over {r['line']:.1f} @ {r['price']:.3f} | {r['fixture']} | side={r['side']}{ml_str}"
            )
            lines.append(
                "    Overall: team {} | oppA {} | combo={:.1f}%   |   H/A: team {} | oppA {} | combo={:.1f}%   |   triggers: {}"
                .format(
                    fmt_pair(r["t_hits_all"], r["t_n_all"]), fmt_pair(r["a_hits_all"], r["a_n_all"]),
                    (r["c_pct_all"]*100) if r["c_pct_all"] is not None else float("nan"),
                    fmt_pair(r["t_hits_split"], r["t_n_split"]), fmt_pair(r["a_hits_split"], r["a_n_split"]),
                    (r["c_pct_split"]*100) if r["c_pct_split"] is not None else float("nan"),
                    ",".join(r["triggers"])
                )
            )

    OUT_FILE.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    print("\n".join(lines))

if __name__ == "__main__":
    main()
