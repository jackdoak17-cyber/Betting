#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Team Lines — OVER only (ranked by combo% then price) using Sportmonks data

Reads (local files only):
- Fixtures:                    data/fixtures/{league_id}.json
- Team offense series:         data/team_stats/by_league/{league_id}.json
- Opponent-allowed series:     data/team_opponent_stats/by_league/{league_id}.json
- Bet365 odds from Sportmonks: data/odds/b365/{league_id}.json

Markets considered (team totals): Team Shots, Team Shots on Target, Team Corners, Team Tackles.

Output:
- data/value_bets/team_lines_over_ranked.txt

Env (optional):
- LEAGUE_IDS     CSV of league IDs to scan (default: auto from fixtures dir)
- MIN_DEC_PRICE  minimum decimal price (default 1.20)
- WINDOW_DAYS    restrict fixtures to next N days (default 7, 0 = no limit)
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

MIN_DEC_PRICE = float(os.getenv("MIN_DEC_PRICE", "1.20"))
WINDOW_DAYS   = int(os.getenv("WINDOW_DAYS", "7"))

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
        dt_utc = dt.datetime.strptime(starting_at, "%Y-%m-%d %H:%M:%S").replace(tzinfo=dt.timezone.utc)
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
    # "total": "Over 4.5" or "Under 8.5"
    t = norm(row.get("total") or "")
    return ("over" in t) and ("under" not in t)

def row_line(row: dict) -> Optional[float]:
    # 1) handicap if present
    h = row.get("handicap")
    v = as_float(h) if h is not None else None
    if v is not None: return v
    # 2) parse number from total or label
    for field in ("total", "label"):
        s = (row.get(field) or "").strip()
        m = re.search(r"([-+]?\d+(?:\.\d+)?)", s)
        if m:
            try: return float(m.group(1))
            except: pass
    return None

# ---------- Rates ----------
def over_rate(seq: List[int], line: float) -> Optional[Tuple[int,int,float]]:
    if not seq: return None
    thr = math.ceil(float(line))
    xs = [x for x in seq if isinstance(x,int)]
    if not xs: return None
    hits = sum(1 for x in xs if x >= thr)
    n = len(xs)
    return hits, n, hits / n

def best_combo(team_over, oppA_over) -> Optional[Tuple[float, Tuple[int,int], Tuple[int,int]]]:
    t_pct = team_over[2] if team_over else None
    a_pct = oppA_over[2] if oppA_over else None
    if t_pct is None and a_pct is None: return None
    if t_pct is None: return a_pct, (0,0), (oppA_over[0], oppA_over[1])
    if a_pct is None: return t_pct, (team_over[0], team_over[1]), (0,0)
    combo = min(t_pct, a_pct)
    return combo, (team_over[0], team_over[1]), (oppA_over[0], oppA_over[1])

# ---------- Main ----------
def main():
    # leagues to scan
    if os.getenv("LEAGUE_IDS"):
        league_ids = [int(x) for x in os.getenv("LEAGUE_IDS").split(",") if x.strip()]
    else:
        league_ids = discover_league_ids()

    rows: List[dict] = []

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

            for row in rows_odds:
                stat = detect_team_market(row)
                if stat is None:
                    continue
                side = label_to_side(row.get("label"))
                if side not in {"home","away"}:
                    # fallback: try text (rare)
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

                # Pull series
                t_rec = ts_idx.get(norm(team_nm))
                a_rec = opp_idx.get(norm(opp_nm))

                def series_for(rec: dict, key: str) -> List[int]:
                    if not rec: return []
                    return [x for x in (rec.get(key) or []) if isinstance(x, int)]

                if stat == "shots_total":
                    off  = series_for(t_rec,  "shots_total_last_n")
                    oppA = series_for(a_rec, "opp_shots_total_last_n")
                    stat_label = "Team Shots"
                elif stat == "shots_on_target":
                    off  = series_for(t_rec,  "shots_on_target_last_n")
                    oppA = series_for(a_rec, "opp_shots_on_target_last_n")
                    stat_label = "Team Shots on Target"
                elif stat == "corners":
                    off  = series_for(t_rec,  "corners_last_n")
                    oppA = series_for(a_rec, "opp_corners_last_n")
                    stat_label = "Team Corners"
                elif stat == "tackles":
                    off  = series_for(t_rec,  "tackles_last_n")
                    oppA = series_for(a_rec, "opp_tackles_last_n")
                    stat_label = "Team Tackles"
                else:
                    continue

                t_over  = over_rate(off, line)
                a_over  = over_rate(oppA, line)
                combo   = best_combo(t_over, a_over)
                if not combo:
                    continue

                combo_pct, t_pair, a_pair = combo
                rows.append({
                    "fixture": name,
                    "kickoff": starting_at,
                    "team": team_nm,
                    "opp": opp_nm,
                    "side": side,
                    "market": f"{stat_label} {'Home' if side=='home' else 'Away'}",
                    "stat_key": stat,
                    "line": float(line),
                    "price": float(price),
                    "team_hits": t_pair[0], "team_n": t_pair[1],
                    "opp_hits": a_pair[0],  "opp_n": a_pair[1],
                    "combo": combo_pct,
                })

    # Rank by combo desc, then price desc
    rows.sort(key=lambda r: (-r["combo"], -r["price"], r["fixture"], r["team"], r["stat_key"], r["line"]))

    # Render
    lines = []
    lines.append(f"Generated at (UTC): {dt.datetime.utcnow().isoformat()}")
    lines.append(f"Window={WINDOW_DAYS} days | Min price={MIN_DEC_PRICE:.2f}")
    lines.append("")
    lines.append("===== TEAM LINES — OVER (ranked by combo % then price) =====")

    if not rows:
        lines.append("  No qualifying team OVER lines found.\n")
    else:
        for r in rows:
            t_pct = (r["team_hits"]/r["team_n"]*100.0) if r["team_n"] else None
            a_pct = (r["opp_hits"]/r["opp_n"]*100.0) if r["opp_n"] else None
            t_str = f"{r['team_hits']}/{r['team_n']} ({t_pct:5.1f}%)" if r["team_n"] else "n/a"
            a_str = f"{r['opp_hits']}/{r['opp_n']} ({a_pct:5.1f}%)"  if r["opp_n"] else "n/a"
            tag = "SOT" if r["stat_key"]=="shots_on_target" else ("Shots" if r["stat_key"]=="shots_total" else r["stat_key"].capitalize())
            lines.append(
                f" • {r['team']} — {tag} Over {r['line']:.1f} @ {r['price']:.3f} | {r['fixture']} | side={r['side']} | "
                f"team {t_str}, oppA {a_str} | combo={r['combo']*100:5.1f}% | {r['market']}"
            )

    OUT_FILE.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    print("\n".join(lines))

if __name__ == "__main__":
    main()
