#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Team Shots & Shots on Target — V3 (model-based shortlist, no baseline shrink)
H2H requires last season's reverse fixture *when available* (same home/away).

Workflow (per upcoming fixture, per team):
1) Build samples with location alignment
   - Team attack: last 10 league games played at the same venue (home/away) as the
     upcoming fixture, using shots and shots on target.
   - Opponent concession: opponent's last 10 league games at their upcoming venue
     (home when they will be home, away when they will be away), using shots and
     shots on target conceded.
   - H2H: up to the last 4 league head-to-head games with the same
     home/away orientation; record the team offense stats.
2) Translate each bookmaker line to an integer threshold (e.g. 8.5 → need 9+).
3) Compute hit rates for the threshold and require conversion: Overs need strong
   hit rates; Unders need weak hit rates. When available, H2H must have landed in
   the reverse fixture last season (same home/away orientation).
   - Overs: team attack ≥70%, opponent concession ≥70%, H2H ≥50% when at least 2
     H2H samples exist, and last season's reverse fixture hit the over.
   - Unders: team attack ≤30%, opponent concession ≤30%, H2H ≤50% when at least 2
     H2H samples exist, and last season's reverse fixture hit the under.
4) Adjust for favourite/underdog using implied 1X2 win probability:
   - Strong favourite (≥60% win prob): +0.03 to model Over.
   - Big underdog (≤40% win prob): −0.03 to model Over.
   Model Under = 1 − model Over.
   Moneyline guards: skip Overs for teams priced 4.0+ (big underdogs) and skip
   Unders for teams priced below 1.80 (big favourites).
5) Keep any Over/Under side whose hit-rate profile passes the gates and provide a
   ranked shortlist (strongest matches first), provided team and opponent samples
   are not tiny (aim for 4+ same-venue games) and the hits are not one-off outliers.

Inputs (local files):
- Fixtures:                  data/fixtures/{league_id}.json
- Bet365 odds:               data/odds/b365/{league_id}.json
- Team offense series:       data/team_stats/by_league/{league_id}.json
- Team opponent-allowed:     data/team_opponent_stats/by_league/{league_id}.json
- Head-to-head histories:    data/h2h/by_league/{league_id}.json

Output:
- data/value_bets/reports/team_shots_sot_v3.txt (printed to console as well)

Env (optional):
- LEAGUE_IDS     CSV of league IDs to scan (default: auto from fixtures dir)
- MIN_DEC_PRICE  Minimum decimal price to keep (default: 1.20)
- WINDOW_DAYS    Only consider fixtures within next N days (default: 7, 0 = no limit)
"""

import os, re, json, math, csv, datetime as dt
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Iterable

ROOT = Path(".")
FIX_DIR   = ROOT / "data" / "fixtures"
TS_DIR    = ROOT / "data" / "team_stats" / "by_league"
OPP_DIR   = ROOT / "data" / "team_opponent_stats" / "by_league"
ODDS_DIR  = ROOT / "data" / "odds" / "b365"
H2H_DIR   = ROOT / "data" / "h2h" / "by_league"
BASE_OUT   = ROOT / "data" / "value_bets"
REPORT_DIR = BASE_OUT / "reports"
SHEET_DIR  = BASE_OUT / "sheets"
REPORT_DIR.mkdir(parents=True, exist_ok=True)
SHEET_DIR.mkdir(parents=True, exist_ok=True)
OUT_FILE  = REPORT_DIR / "team_shots_sot_v3.txt"
SHEET_FILE = SHEET_DIR / "team_shots_sot_v3_bets.csv"

MIN_DEC_PRICE = float(os.getenv("MIN_DEC_PRICE", "1.20"))
WINDOW_DAYS   = int(os.getenv("WINDOW_DAYS", "7"))

BET_HEADERS = [
    "fixture_id",
    "league_id",
    "fixture",
    "kickoff",
    "team_id",
    "team",
    "opp_id",
    "opp",
    "venue",
    "market",
    "stat",
    "pick",
    "line",
    "price",
    "model_p",
    "implied_p",
    "threshold",
    "created_at",
    "result",
    "actual",
    "settled_at",
]

# ---------- Helpers ----------

def load_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}

def within_window(starting_at: str, days: int) -> bool:
    if not days:
        return True
    try:
        dt_utc = dt.datetime.strptime(starting_at[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=dt.timezone.utc)
    except Exception:
        return True
    now = dt.datetime.now(dt.timezone.utc)
    return now <= dt_utc <= (now + dt.timedelta(days=days))

def norm(s: str) -> str:
    s = (s or "").lower()
    s = re.sub(r"[^a-z0-9\s\.-]", " ", s)
    return re.sub(r"\s+", " ", s).strip()

def as_float(x) -> Optional[float]:
    try:
        return float(str(x))
    except Exception:
        return None

# ---------- Odds parsing ----------

TEAM_MD_TO_CANON = {
    "team shots": "shots_total",
    "team shots on target": "shots_on_target",
}

MATCH_WINNER_ALIASES = {
    "match winner", "match result", "full time result", "fulltime result",
    "1x2", "result", "win/draw/win", "90 minutes", "3-way", "3 way", "regular time result"
}

def detect_team_market(row: dict) -> Optional[str]:
    md = norm(row.get("market_description") or "")
    return TEAM_MD_TO_CANON.get(md)

def label_to_side(label: Optional[str]) -> Optional[str]:
    s = (label or "").strip().lower()
    if s in {"1","home"}: return "home"
    if s in {"2","away"}: return "away"
    return None

def row_is_over(row: dict) -> Optional[bool]:
    t = norm(row.get("total") or "")
    if "over" in t and "under" not in t:
        return True
    if "under" in t and "over" not in t:
        return False
    return None

def row_line(row: dict) -> Optional[float]:
    h = row.get("handicap")
    v = as_float(h) if h is not None else None
    if v is not None:
        return v
    for field in ("total", "label", "name"):
        s = (row.get(field) or "").strip()
        m = re.search(r"([-+]?\d+(?:\.\d+)?)", s)
        if m:
            try:
                return float(m.group(1))
            except Exception:
                pass
    return None

def is_match_winner_row(row: dict) -> bool:
    md = norm(row.get("market_description") or "")
    return md in MATCH_WINNER_ALIASES

def extract_ml(rows: List[dict]) -> Tuple[Optional[float], Optional[float]]:
    home_ml = None; away_ml = None
    for r in rows:
        if not is_match_winner_row(r):
            continue
        side = label_to_side(r.get("label"))
        price = as_float(r.get("value"))
        if price is None or side not in {"home","away"}:
            continue
        if side == "home":
            home_ml = price if home_ml is None else home_ml
        else:
            away_ml = price if away_ml is None else away_ml
    return home_ml, away_ml

# ---------- Sample builders ----------

def filter_by_location(vals: List[Optional[int]], locs: List[str], target: str, limit: int = 10) -> List[int]:
    out: List[int] = []
    for v, loc in zip(vals, locs):
        if loc != target:
            continue
        if isinstance(v, int):
            out.append(v)
            if len(out) >= limit:
                break
    return out

def team_sample(team_rec: dict, stat: str, side: str) -> List[int]:
    vals = team_rec.get("shots_total_last_n") if stat == "shots_total" else team_rec.get("shots_on_target_last_n")
    locs = team_rec.get("locations_last_n") or []
    vals = vals or []
    return filter_by_location(vals, locs, side)

def opponent_sample(opp_rec: dict, stat: str, opponent_side: str) -> List[int]:
    if stat == "shots_total":
        vals = opp_rec.get("opp_shots_total_last_n")
    else:
        vals = opp_rec.get("opp_shots_on_target_last_n")
    locs = opp_rec.get("locations_last_n") or []
    vals = vals or []
    return filter_by_location(vals, locs, opponent_side)

def h2h_sample(fx_h2h: Optional[dict], stat: str, side: str) -> List[int]:
    if not fx_h2h:
        return []
    vectors = fx_h2h.get("vectors") or {}
    key = "home" if side == "home" else "away"
    series = vectors.get(key, {})
    vals = series.get("shots" if stat == "shots_total" else "sot") or []
    return [v for v in vals if isinstance(v, int)][:4]

def reverse_fixture_value(
    fx_h2h: Optional[dict], stat: str, side: str, kickoff: Optional[dt.datetime]
) -> Optional[int]:
    """Return the stat from the most recent prior same-venue meeting *last season*.

    Limit accepted reverse fixtures to ~200–500 days before kickoff to avoid
    stale multi-season matchups or same-season repeats.
    """
    if not fx_h2h or kickoff is None:
        return None
    metas = fx_h2h.get("lastN_meta") or []
    vectors = fx_h2h.get("vectors") or {}
    key = "home" if side == "home" else "away"
    series = (vectors.get(key, {}) or {}).get("shots" if stat == "shots_total" else "sot") or []
    candidates: List[Tuple[dt.datetime, int]] = []
    for meta, val in zip(metas, series):
        if not isinstance(val, int):
            continue
        try:
            played = dt.datetime.strptime(meta.get("starting_at", "")[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=dt.timezone.utc)
        except Exception:
            continue
        if played >= kickoff:
            continue
        delta_days = (kickoff - played).days
        if delta_days < 200 or delta_days > 500:
            continue
        candidates.append((played, val))
    if not candidates:
        return None
    return max(candidates, key=lambda t: t[0])[1]

# ---------- Rates & modelling ----------

def threshold_from_line(line: float) -> int:
    frac = line - math.floor(line)
    if frac > 1e-6:
        return math.floor(line) + 1
    return int(math.floor(line))

def hit_rate(sample: List[int], threshold: int) -> Tuple[float, int, int]:
    total = len(sample)
    if total == 0:
        return 0.0, 0, 0
    hits = sum(1 for v in sample if v >= threshold)
    return hits / total, hits, total

def implied_win_probs(home_ml: Optional[float], away_ml: Optional[float]) -> Tuple[Optional[float], Optional[float]]:
    if home_ml is None or away_ml is None or home_ml <= 1 or away_ml <= 1:
        return None, None
    inv_h, inv_a = 1 / home_ml, 1 / away_ml
    total = inv_h + inv_a
    return inv_h / total, inv_a / total

def adjust_for_favourite(over_p: float, side: str, home_win: Optional[float], away_win: Optional[float]) -> float:
    win_prob = home_win if side == "home" else away_win
    if win_prob is None:
        return max(0.0, min(1.0, over_p))
    if win_prob >= 0.60:
        over_p += 0.03
    elif win_prob <= 0.40:
        over_p -= 0.03
    return max(0.0, min(1.0, over_p))

# ---------- Spreadsheet helpers ----------

def _normalize_line_key(line: float) -> str:
    try:
        return f"{float(line):.2f}"
    except Exception:
        return str(line)

def load_sheet(path: Path) -> List[dict]:
    if not path.exists():
        return []
    try:
        with path.open("r", newline="", encoding="utf-8") as f:
            return [dict(row) for row in csv.DictReader(f)]
    except Exception:
        return []

def save_sheet(path: Path, rows: List[dict]):
    headers = list(BET_HEADERS)
    extra = sorted({k for r in rows for k in r.keys()} - set(headers))
    headers.extend(extra)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=headers)
        writer.writeheader()
        for r in rows:
            writer.writerow({h: r.get(h, "") for h in headers})

def fixture_stat_value(team_rec: dict, stat: str, fixture_id: int, expected_loc: Optional[str]) -> Optional[int]:
    fids = team_rec.get("fixture_ids") or []
    locs = team_rec.get("locations_last_n") or []
    if stat == "shots_total":
        vals = team_rec.get("shots_total_last_n") or []
    else:
        vals = team_rec.get("shots_on_target_last_n") or []
    for fid, loc, val in zip(fids, locs, vals):
        try:
            if int(fid) != int(fixture_id):
                continue
        except Exception:
            continue
        if expected_loc and loc and loc != expected_loc:
            continue
        if isinstance(val, int):
            return val
    return None

def settle_row(row: dict, ts_index: Dict[int, Dict[int, dict]]):
    try:
        league_id = int(row.get("league_id"))
        team_id = int(row.get("team_id"))
        fixture_id = int(row.get("fixture_id"))
    except Exception:
        return
    stat = row.get("stat") or ""
    pick = (row.get("pick") or "").lower()
    if pick not in {"over", "under"}:
        return
    ts_by_id = ts_index.get(league_id)
    if not ts_by_id:
        return
    team_rec = ts_by_id.get(team_id)
    if not team_rec:
        return
    loc = row.get("venue") if row.get("venue") in {"home", "away"} else None
    val = fixture_stat_value(team_rec, stat, fixture_id, loc)
    if val is None:
        return
    try:
        threshold = threshold_from_line(float(row.get("line")))
    except Exception:
        return
    outcome = None
    line_float = None
    try:
        line_float = float(row.get("line"))
    except Exception:
        pass
    if line_float is not None and line_float.is_integer() and val == int(line_float):
        outcome = "push"
    elif pick == "over":
        outcome = "won" if val >= threshold else "lost"
    else:
        outcome = "won" if val < threshold else "lost"
    now = dt.datetime.utcnow().isoformat(timespec="seconds")
    row["result"] = outcome
    row["actual"] = str(val)
    row["settled_at"] = now

def update_bet_sheet(path: Path, candidates: List[dict], ts_index: Dict[int, Dict[int, dict]]):
    existing = load_sheet(path)
    rows: Dict[Tuple[int, int, str, str, str], dict] = {}
    for r in existing:
        key = (
            int(r.get("fixture_id")) if r.get("fixture_id") else None,
            int(r.get("team_id")) if r.get("team_id") else None,
            r.get("stat") or "",
            _normalize_line_key(r.get("line") or ""),
            (r.get("pick") or "").lower(),
        )
        rows[key] = r

    now = dt.datetime.utcnow().isoformat(timespec="seconds")
    for c in candidates:
        key = (
            int(c["fixture_id"]),
            int(c["team_id"]),
            c["stat"],
            _normalize_line_key(c["line"]),
            c["pick"].lower(),
        )
        if key not in rows:
            rows[key] = {
                "fixture_id": c["fixture_id"],
                "league_id": c["league_id"],
                "fixture": c["fixture"],
                "kickoff": c["kickoff"],
                "team_id": c["team_id"],
                "team": c["team"],
                "opp_id": c["opp_id"],
                "opp": c["opp"],
                "venue": c["venue"],
                "market": c["market"],
                "stat": c["stat"],
                "pick": c["pick"],
                "line": c["line"],
                "price": c.get("price", ""),
                "model_p": f"{c.get('model_p', 0):.3f}",
                "implied_p": f"{c.get('implied_p', 0):.3f}",
                "threshold": c.get("threshold", ""),
                "created_at": now,
                "result": "",
                "actual": "",
                "settled_at": "",
            }
        else:
            row = rows[key]
            row.update({
                "price": c.get("price", row.get("price", "")),
                "model_p": f"{c.get('model_p', 0):.3f}",
                "implied_p": f"{c.get('implied_p', 0):.3f}",
                "threshold": c.get("threshold", row.get("threshold", "")),
                "kickoff": c.get("kickoff", row.get("kickoff", "")),
            })

    for r in rows.values():
        if (r.get("result") or "").lower() in {"won", "lost", "push"}:
            continue
        settle_row(r, ts_index)

    ordered = sorted(rows.values(), key=lambda r: (
        r.get("kickoff") or "",
        r.get("fixture") or "",
        r.get("team") or "",
        r.get("market") or "",
        _normalize_line_key(r.get("line") or ""),
        r.get("pick") or "",
    ))
    save_sheet(path, ordered)

# ---------- Main ----------

def main():
    if os.getenv("LEAGUE_IDS"):
        league_ids = [int(x) for x in os.getenv("LEAGUE_IDS").split(",") if x.strip()]
    else:
        league_ids = sorted({int(p.stem) for p in FIX_DIR.glob("*.json") if p.stem.isdigit()})

    candidates: List[dict] = []
    ts_index: Dict[int, Dict[int, dict]] = {}

    for lid in league_ids:
        fx_path   = FIX_DIR / f"{lid}.json"
        odds_path = ODDS_DIR / f"{lid}.json"
        ts_path   = TS_DIR / f"{lid}.json"
        opp_path  = OPP_DIR / f"{lid}.json"
        h2h_path  = H2H_DIR / f"{lid}.json"
        if not (fx_path.exists() and odds_path.exists() and ts_path.exists() and opp_path.exists() and h2h_path.exists()):
            continue

        odds_blob = load_json(odds_path)
        odds_by_fixture = {int(f.get("fixture_id")): f for f in (odds_blob.get("fixtures") or []) if isinstance(f.get("fixture_id"), int)}
        ts_blob  = load_json(ts_path)
        opp_blob = load_json(opp_path)
        h2h_blob = load_json(h2h_path)
        fixtures = load_json(fx_path).get("fixtures") or []
        ts_by_id   = {t.get("team_id"): t for t in (ts_blob.get("teams") or []) if isinstance(t.get("team_id"), int)}
        opp_by_id  = {t.get("team_id"): t for t in (opp_blob.get("teams") or []) if isinstance(t.get("team_id"), int)}
        h2h_idx    = {fx.get("fixture_id"): fx for fx in (h2h_blob.get("fixtures") or []) if isinstance(fx.get("fixture_id"), int)}

        ts_index[lid] = ts_by_id

        for fx in fixtures:
            if not isinstance(fx, dict):
                continue
            fid = fx.get("id") or fx.get("fixture_id")
            starting_at = fx.get("starting_at") or ""
            if not isinstance(fid, int):
                continue
            kickoff_dt = None
            try:
                kickoff_dt = dt.datetime.strptime(starting_at[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=dt.timezone.utc)
            except Exception:
                pass
            if WINDOW_DAYS and not within_window(starting_at, WINDOW_DAYS):
                continue

            participants = fx.get("participants") or []
            home_id = away_id = None
            home_name = away_name = ""
            for p in participants:
                try:
                    tid = int(p.get("id"))
                except Exception:
                    continue
                loc = (p.get("meta") or {}).get("location")
                if loc == "home":
                    home_id = tid; home_name = p.get("name") or ""
                elif loc == "away":
                    away_id = tid; away_name = p.get("name") or ""
            if not (home_id and away_id):
                continue

            odds_fx = odds_by_fixture.get(int(fid))
            if not odds_fx:
                continue
            rows_odds = odds_fx.get("odds") or []
            if not isinstance(rows_odds, list):
                continue

            home_ml, away_ml = extract_ml(rows_odds)
            home_win, away_win = implied_win_probs(home_ml, away_ml)
            fx_h2h = h2h_idx.get(int(fid))

            # Build odds map keyed by (side, stat, line)
            odds_map: Dict[Tuple[str, str, float], Dict[str, float]] = {}
            for row in rows_odds:
                stat = detect_team_market(row)
                if stat not in {"shots_total", "shots_on_target"}:
                    continue
                over_flag = row_is_over(row)
                if over_flag is None:
                    continue
                side = label_to_side(row.get("label"))
                if side not in {"home","away"}:
                    txt = " ".join([str(row.get("name") or ""), str(row.get("total") or ""), str(row.get("original_label") or "")]).lower()
                    if "home" in txt and "away" not in txt:
                        side = "home"
                    elif "away" in txt and "home" not in txt:
                        side = "away"
                    else:
                        continue
                line = row_line(row)
                price = as_float(row.get("value"))
                if line is None or price is None or price < MIN_DEC_PRICE:
                    continue
                key = (side, stat, line)
                bucket = odds_map.setdefault(key, {})
                bucket["over" if over_flag else "under"] = price

            for (side, stat, line), prices in odds_map.items():
                team_id, opp_id = (home_id, away_id) if side == "home" else (away_id, home_id)
                team_nm, opp_nm = (home_name, away_name) if side == "home" else (away_name, home_name)
                team_rec = ts_by_id.get(team_id)
                opp_rec  = opp_by_id.get(opp_id)
                if not team_rec or not opp_rec:
                    continue

                team_series = team_sample(team_rec, stat, side)
                opp_series  = opponent_sample(opp_rec, stat, "home" if side == "away" else "away")
                h2h_series  = h2h_sample(fx_h2h, stat, side)
                reverse_val = reverse_fixture_value(fx_h2h, stat, side, kickoff_dt)

                if len(team_series) < 4 or len(opp_series) < 4:
                    continue

                threshold = threshold_from_line(line)
                team_rate, team_hits, team_total = hit_rate(team_series, threshold)
                opp_rate, opp_hits, opp_total   = hit_rate(opp_series, threshold)
                h2h_rate, h2h_hits, h2h_total   = hit_rate(h2h_series, threshold)

                if team_total >= 8 and team_hits <= 1:
                    continue  # likely outlier-driven
                if opp_total >= 8 and opp_hits <= 1:
                    continue

                h2h_adj = h2h_rate if h2h_total >= 2 else None

                if h2h_adj is not None:
                    over_p = 0.5 * team_rate + 0.3 * opp_rate + 0.2 * h2h_adj
                else:
                    over_p = 0.6 * team_rate + 0.4 * opp_rate

                over_p = adjust_for_favourite(over_p, side, home_win, away_win)
                under_p = max(0.0, min(1.0, 1 - over_p))

                implied_over = 1 / prices.get("over") if prices.get("over") else None
                implied_under = 1 / prices.get("under") if prices.get("under") else None
                team_ml = home_ml if side == "home" else away_ml

                # Evaluate sides
                for pick_side, model_p, implied_p, price in (
                    ("Over", over_p, implied_over, prices.get("over")),
                    ("Under", under_p, implied_under, prices.get("under")),
                ):
                    if price is None or implied_p is None:
                        continue
                    if pick_side == "Over":
                        if team_rate < 0.70 or opp_rate < 0.70:
                            continue
                        if h2h_total >= 2 and h2h_rate < 0.50:
                            continue
                        if reverse_val is not None and reverse_val < threshold:
                            continue
                    else:  # Under side
                        if team_rate > 0.30 or opp_rate > 0.30:
                            continue
                        if h2h_total >= 2 and h2h_rate > 0.50:
                            continue
                        if reverse_val is not None and reverse_val >= threshold:
                            continue
                    if pick_side == "Over" and team_ml is not None and team_ml >= 4.0:
                        continue  # avoid overs on big underdogs
                    if pick_side == "Under" and team_ml is not None and team_ml < 1.80:
                        continue  # avoid unders on big favourites
                    candidates.append({
                        "fixture_id": fid,
                        "league_id": lid,
                        "fixture": fx.get("name") or "",
                        "kickoff": starting_at,
                        "team_id": team_id,
                        "team": team_nm,
                        "opp_id": opp_id,
                        "opp": opp_nm,
                        "venue": side,
                        "side": side,
                        "market": "Shots" if stat == "shots_total" else "Shots on Target",
                        "stat": stat,
                        "line": line,
                        "pick": pick_side,
                        "price": price,
                        "model_p": model_p,
                        "implied_p": implied_p,
                        "team_hits": team_hits,
                        "team_total": team_total,
                        "opp_hits": opp_hits,
                        "opp_total": opp_total,
                        "h2h_hits": h2h_hits,
                        "h2h_total": h2h_total,
                        "threshold": threshold,
                        "home_win": home_win,
                        "away_win": away_win,
                        "team_series": team_series,
                        "opp_series": opp_series,
                        "reverse_val": reverse_val,
                    })

    candidates.sort(key=lambda r: (-r["model_p"], r["kickoff"], r["fixture"], r["team"], r["market"], r["line"], r["pick"]))

    update_bet_sheet(SHEET_FILE, candidates, ts_index)

    lines = []
    lines.append(f"Generated at (UTC): {dt.datetime.utcnow().isoformat(timespec='seconds')}")
    lines.append(f"Window={WINDOW_DAYS} days | Min price={MIN_DEC_PRICE:.2f}")
    lines.append("Workflow: same-venue last10; line→threshold; hit-rate gates (Over ≥70%/Under ≤30% + reverse fixture hit); favourite tilt + moneyline guards; ranked by strongest model probability (samples ≥4).")
    lines.append("")
    lines.append("===== TEAM SHOTS & SOT (V3) =====")

    if not candidates:
        lines.append("(none)")
    else:
        for r in candidates:
            venue = "home" if r["side"] == "home" else "away"
            stat_label = "shots" if r["market"] == "Shots" else "shots on target"
            lines.append(
                f" • {r['team']} ({venue}) — {r['market']} {r['pick']} {r['line']:.1f} @ {r['price']:.3f} vs {r['opp']} | kickoff {r['kickoff']}"
            )
            lines.append(
                f"    model={r['model_p']:.3f} | implied={r['implied_p']:.3f} | threshold≥{r['threshold']}"
            )

            team_hits = sum(1 for v in r["team_series"] if (v >= r["threshold"] if r["pick"] == "Over" else v < r["threshold"]))
            opp_hits = sum(1 for v in r["opp_series"] if (v >= r["threshold"] if r["pick"] == "Over" else v < r["threshold"]))
            team_vals = ",".join(str(v) for v in r["team_series"])
            opp_vals = ",".join(str(v) for v in r["opp_series"])

            opp_venue = "away" if venue == "home" else "home"
            if r["pick"] == "Over":
                lines.append(
                    f"    {r['team']} have cleared {r['threshold']} {stat_label} in {team_hits}/{r['team_total']} {venue} matches = {team_vals}"
                )
                lines.append(
                    f"    {r['opp']}'s opponents have had {r['threshold']}+ {stat_label} in {opp_hits}/{r['opp_total']} {opp_venue} games = {opp_vals}"
                )
            else:
                lines.append(
                    f"    {r['team']} have had under {r['threshold']} {stat_label} in {team_hits}/{r['team_total']} {venue} matches = {team_vals}"
                )
                lines.append(
                    f"    {r['opp']}'s opponents have had under {r['threshold']} {stat_label} in {opp_hits}/{r['opp_total']} {opp_venue} games = {opp_vals}"
                )

            if r.get("reverse_val") is not None:
                lines.append(
                    f"    This landed in the reverse fixture last season = {r['reverse_val']}"
                )
            lines.append(
                f"    team hits {r['team_hits']}/{r['team_total']} | opp conceded {r['opp_hits']}/{r['opp_total']} | H2H {r['h2h_hits']}/{r['h2h_total']}"
            )
            if r.get("home_win") is not None and r.get("away_win") is not None:
                lines.append(f"    implied win probs: home {r['home_win']:.3f} / away {r['away_win']:.3f}")

    OUT_FILE.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    print("\n".join(lines))

if __name__ == "__main__":
    main()
