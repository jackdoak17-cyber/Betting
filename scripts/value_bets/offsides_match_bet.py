#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Team Offsides — OVER only (ranked by combo% then price) using Sportmonks + your series

Reads (local files only):
- Fixtures:                    data/fixtures/{league_id}.json
- Team offense series:         data/team_stats/by_league/{league_id}.json         (offsides_last_n, locations_last_n)
- Opponent-allowed series:     data/team_opponent_stats/by_league/{league_id}.json (opp_offsides_last_n, locations_last_n)
- Bet365 odds from Sportmonks: data/odds/b365/{league_id}.json

Market targeted:
- Team Offsides (Bet365) — market_id 286 — labels “Home/Away” (or “1/2”) with Over/Under lines.

Value logic (OVER only):
- Compute team OVER% vs line, opponent-allowed OVER% vs line (overall).
- combo% = average of the two (if only one side exists, use that one).
- Rank picks by combo% desc, then price desc.
- (Optional) ML filter: drop team if Match Winner price > OFFSIDES_ML_MAX (default 4.00).

Output:
- data/value_bets/offsides_match_bet.txt

Env (optional):
- LEAGUE_IDS         CSV of league IDs to scan (default: discover from fixtures dir)
- WINDOW_DAYS        restrict fixtures to next N days (default 7, 0 = no limit)
- MIN_DEC_PRICE      min decimal price to keep (default 1.20)
- OFFSIDES_ML_MAX    drop teams with ML > this (default 4.00; set to 99 to disable)
"""

import os, re, json, math, datetime as dt, unicodedata
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# ---------- Config ----------
ROOT       = Path(".")
FIX_DIR    = ROOT / "data" / "fixtures"
TS_DIR     = ROOT / "data" / "team_stats" / "by_league"
OPP_DIR    = ROOT / "data" / "team_opponent_stats" / "by_league"
ODDS_DIR   = ROOT / "data" / "odds" / "b365"
OUT_PATH   = Path("data/value_bets/offsides_match_bet.txt"); OUT_PATH.parent.mkdir(parents=True, exist_ok=True)

WINDOW_DAYS     = int(os.getenv("WINDOW_DAYS", "7"))
MIN_DEC_PRICE   = float(os.getenv("MIN_DEC_PRICE", "1.20"))
OFFSIDES_ML_MAX = float(os.getenv("OFFSIDES_ML_MAX", "4.00"))

TEAM_OFFSIDES_MARKET_ID = 286  # Bet365 "Team Offsides"

# ---------- String utils / matching ----------
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
    if not starting_at: return True
    try:
        dt_utc = dt.datetime.strptime(starting_at[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=dt.timezone.utc)
    except Exception:
        return True
    now = dt.datetime.now(dt.timezone.utc)
    return now <= dt_utc <= (now + dt.timedelta(days=days))

# ---------- IO ----------
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

# ---------- Odds helpers ----------
def is_team_offsides_row(row: dict) -> bool:
    md = norm(row.get("market_description") or "")
    mid = row.get("market_id")
    if isinstance(mid, int) and mid == TEAM_OFFSIDES_MARKET_ID:
        return True
    # tolerate minor naming variants
    if "offsides" in md and "team" in md:
        return True
    return False

def label_to_side(label: Optional[str]) -> Optional[str]:
    s = (label or "").strip().lower()
    if s in {"1","home","home (1)","team 1"}: return "home"
    if s in {"2","away","away (2)","team 2"}: return "away"
    return None

def row_is_over(row: dict) -> bool:
    # Many feeds stick "Over" inside total/label fields.
    text = " ".join(str(row.get(k) or "") for k in ("total","label","original_label","name")).lower()
    return "over" in text and ("under" not in text)

def parse_line(row: dict) -> Optional[float]:
    # Prefer explicit handicap/line field if numeric
    h = row.get("handicap")
    try:
        if h is not None:
            return float(h)
    except Exception:
        pass
    # Fallback: extract first number from total/label
    for k in ("total","label","original_label","name"):
        s = str(row.get(k) or "")
        m = re.search(r"([-+]?\d+(?:\.\d+)?)", s)
        if m:
            try: return float(m.group(1))
            except Exception: pass
    return None

def parse_price(row: dict) -> Optional[float]:
    try:
        return float(row.get("value"))
    except Exception:
        return None

# Match Winner (ML) for ML filter
MATCH_WINNER_ALIASES = {
    "match winner","match result","full time result","fulltime result",
    "1x2","result","win/draw/win","90 minutes","3-way","3 way","regular time result"
}
def is_ml_row(r: dict) -> bool:
    return norm(r.get("market_description") or "") in MATCH_WINNER_ALIASES

def extract_ml(rows: List[dict]) -> Tuple[Optional[float], Optional[float]]:
    home_ml = None; away_ml = None
    for r in rows:
        if not is_ml_row(r): continue
        side = label_to_side(r.get("label"))
        price = parse_price(r)
        if price is None or side not in {"home","away"}: continue
        if side == "home":
            home_ml = price if (home_ml is None or price < home_ml) else home_ml
        else:
            away_ml = price if (away_ml is None or price < away_ml) else away_ml
    return home_ml, away_ml

# ---------- Rates ----------
def over_threshold(line: float) -> int:
    """
    Convert a book line into an integer threshold for '>= hits'.
    - For half lines (e.g., 2.5) -> ceil(2.5)=3
    - For integer lines (e.g., 2.0 or 2) -> require strictly over (>= 3)
    """
    if float(line).is_integer():
        return int(line) + 1
    return math.ceil(float(line))

def over_rate(seq: List[int], line: float) -> Optional[Tuple[int,int,float]]:
    if seq is None: return None
    xs = [x for x in seq if isinstance(x, int)]
    if not xs: return None
    thr = over_threshold(line)
    hits = sum(1 for x in xs if x >= thr)
    n = len(xs)
    return hits, n, (hits / n) if n else None

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
    return (t_pct + a_pct) / 2.0, (team_over[0], team_over[1]), (oppA_over[0], oppA_over[1])

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

            # Compute ML once per fixture (for optional filter)
            home_ml, away_ml = extract_ml(rows_odds)

            for row in rows_odds:
                if not is_team_offsides_row(row):
                    continue
                if not row_is_over(row):
                    continue

                side = label_to_side(row.get("label"))
                if side not in {"home","away"}:
                    # fallback: try to infer from text
                    text = " ".join([str(row.get("name") or ""), str(row.get("total") or ""), str(row.get("original_label") or "")]).lower()
                    if "home" in text and "away" not in text: side = "home"
                    elif "away" in text and "home" not in text: side = "away"
                    else:
                        continue

                line  = parse_line(row)
                price = parse_price(row)
                if line is None or price is None or price < MIN_DEC_PRICE:
                    continue

                team_nm = home if side=="home" else away
                opp_nm  = away if side=="home" else home

                # Optional ML filter (drop big underdogs)
                team_ml = home_ml if side=="home" else away_ml
                if (team_ml is None) or (team_ml > OFFSIDES_ML_MAX):
                    continue

                # Pull series
                t_rec = ts_idx.get(norm(team_nm))
                a_rec = opp_idx.get(norm(opp_nm))
                if not t_rec or not a_rec:
                    # fuzzy fallback
                    for r in ts_idx.values():
                        if not t_rec and team_names_match(team_nm, r.get("team_name","")):
                            t_rec = r
                    for r in opp_idx.values():
                        if not a_rec and team_names_match(opp_nm, r.get("team_name","")):
                            a_rec = r
                    if not t_rec or not a_rec:
                        continue

                team_seq = [x for x in (t_rec.get("offsides_last_n") or []) if isinstance(x, int)]
                oppA_seq = [x for x in (a_rec.get("opp_offsides_last_n") or []) if isinstance(x, int)]

                t_over = over_rate(team_seq, line)
                a_over = over_rate(oppA_seq, line)
                combo  = combo_avg(t_over, a_over)
                if not combo:
                    continue

                combo_pct, (t_hits, t_n), (a_hits, a_n) = combo

                rows_out.append({
                    "fixture": name,
                    "kickoff": starting_at,
                    "team": team_nm,
                    "opp": opp_nm,
                    "side": side,
                    "line": float(line),
                    "price": float(price),
                    "team_hits": t_hits, "team_n": t_n,
                    "opp_hits": a_hits,  "opp_n": a_n,
                    "combo": combo_pct,
                    "team_ml": float(team_ml) if team_ml is not None else None,
                })

    # Rank by combo desc, then price desc
    rows_out.sort(key=lambda r: (-r["combo"], -r["price"], r["fixture"], r["team"], r["line"]))

    # Render
    lines = []
    lines.append(f"Generated at (UTC): {dt.datetime.utcnow().isoformat(timespec='seconds')}")
    lines.append("")
    lines.append("Team Offsides — OVER candidates (ranked by combo% then price)")
    if not rows_out:
        lines.append("(no candidates)")
    else:
        for r in rows_out:
            t_pct = (r["team_hits"]/r["team_n"]*100.0) if r["team_n"] else None
            a_pct = (r["opp_hits"]/r["opp_n"]*100.0) if r["opp_n"] else None
            t_str = f"{r['team_hits']}/{r['team_n']} ({t_pct:5.1f}%)" if r["team_n"] else "n/a"
            a_str = f"{r['opp_hits']}/{r['opp_n']} ({a_pct:5.1f}%)"  if r["opp_n"] else "n/a"
            ml_str = f" | ML={r['team_ml']:.3f}" if isinstance(r.get("team_ml"), float) else ""
            lines.append(
                f" • {r['team']} — Offsides Over {r['line']:.1f} @ {r['price']:.3f} | {r['fixture']} | side={r['side']} | "
                f"team {t_str}, oppA {a_str} | combo={(r['combo']*100):5.1f}%{ml_str}"
            )

    OUT_PATH.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    print("\n".join(lines))

if __name__ == "__main__":
    main()
