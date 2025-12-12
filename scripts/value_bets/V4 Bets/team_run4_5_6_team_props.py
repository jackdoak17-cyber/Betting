#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Team Bets V4 — BET RUN 4/5/6 (Team cards, team corners, team total goals overs)

Logic mirrors team shots runs:
 - Overs only, price gate >= 1.72.
 - Same-venue samples for team attack; opponent conceded samples at upcoming venue.
 - Strong team hit gates: any of {8/9, 8/10, 10/12, 11/13, 12/14, 13/15, 15/19, 16/20}.
 - Opponent allow gates: any of {4/5, 5/5, 6/8, 7/10} or >=70% if n>10.
 - Must have landed in the most recent same-venue H2H at or above the line when available.
 - Drop if team has been at or below half the line in 2 of last 10.
 - Outputs three files:
     BET RUN 4 (cards):   data/value_bets/V4 Bets/team_run4_cards.txt
     BET RUN 5 (corners): data/value_bets/V4 Bets/team_run5_corners.txt
     BET RUN 6 (goals):   data/value_bets/V4 Bets/team_run6_goals.txt

Inputs:
  data/fixtures/{league_id}.json
  data/odds/b365/{league_id}.json
  data/team_stats/by_league/{league_id}.json
  data/team_opponent_stats/by_league/{league_id}.json
  data/h2h/by_league/{league_id}.json

Env:
  LEAGUE_IDS   CSV of league ids (default: auto from fixtures dir)
  MIN_PRICE    Decimal price gate (default 1.72)
  WINDOW_DAYS  Only fixtures within next N days (default 7, 0=all)
  DEBUG_DROPS  1 to print drop reasons
"""

import json
import math
import os
import re
import datetime as dt
from pathlib import Path
from typing import Dict, List, Optional, Tuple

ROOT = Path(".")
FIX_DIR = ROOT / "data" / "fixtures"
ODDS_DIR = ROOT / "data" / "odds" / "b365"
TS_DIR = ROOT / "data" / "team_stats" / "by_league"
OPP_DIR = ROOT / "data" / "team_opponent_stats" / "by_league"
H2H_DIR = ROOT / "data" / "h2h" / "by_league"
OUT_BASE = ROOT / "data" / "value_bets" / "V4 Bets"
OUT_BASE.mkdir(parents=True, exist_ok=True)

MIN_PRICE = float(os.getenv("MIN_PRICE", "1.72"))
WINDOW_DAYS = int(os.getenv("WINDOW_DAYS", "7"))
DEBUG_DROPS = bool(int(os.getenv("DEBUG_DROPS", "0")))

TEAM_WINDOWS = [(9, 8), (10, 8), (12, 10), (13, 11), (14, 12), (15, 13), (19, 15), (20, 16)]
OPP_WINDOWS = [(5, 4), (5, 5), (8, 6), (10, 7)]

STAT_CONFIGS = [
    {
        "name": "cards",
        "market_descs": ["team cards"],
        "team_key": "cards_total_last_n",
        "opp_key": "opp_cards_total_last_n",
        "stat_label": "cards",
        "outfile": OUT_BASE / "team_run4_cards.txt",
        "h2h_stat": "cards",
    },
    {
        "name": "corners",
        "market_descs": ["team corners"],
        "team_key": "corners_last_n",
        "opp_key": "opp_corners_last_n",
        "stat_label": "corners",
        "outfile": OUT_BASE / "team_run5_corners.txt",
        "h2h_stat": "corners",
    },
    {
        "name": "goals",
        "market_descs": ["team total goals", "team goals"],
        "team_key": "goals_last_n",
        "opp_key": "opp_goals_last_n",
        "stat_label": "goals",
        "outfile": OUT_BASE / "team_run6_goals.txt",
        "h2h_stat": "goals",
    },
]


def load_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def within_window(starting_at: str, days: int) -> bool:
    if not days:
        return True
    try:
        dt_obj = dt.datetime.fromisoformat(starting_at.replace("Z", "+00:00"))
        if dt_obj.tzinfo is None:
            dt_obj = dt_obj.replace(tzinfo=dt.timezone.utc)
    except Exception:
        return True
    now = dt.datetime.now(dt.timezone.utc)
    return now <= dt_obj <= now + dt.timedelta(days=days)


def norm(s: str) -> str:
    s = (s or "").lower()
    s = re.sub(r"[^\w\s\.-]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def label_to_side(label: Optional[str]) -> Optional[str]:
    s = (label or "").strip()
    low = s.lower()
    if low in {"1", "home", "home team"}:
        return "home"
    if low in {"2", "away", "away team"}:
        return "away"
    return None


def parse_line(total: Optional[str]) -> Optional[float]:
    if not total:
        return None
    m = re.search(r"([0-9]+(?:\.[0-9]+)?)", total.replace(",", "."))
    if not m:
        return None
    try:
        return float(m.group(1))
    except Exception:
        return None


def threshold_from_line(line: float) -> int:
    # Overs need strictly greater than the line (e.g., over 2.0 requires 3).
    return math.floor(line + 1e-9) + 1


def best_gate(seq: List[int], threshold: int, gates: List[Tuple[int, int]]) -> Tuple[bool, float, int, int]:
    ok = False
    best_ratio = -1.0
    best_hits = 0
    best_win = 0
    for win, req in gates:
        if len(seq) < win:
            continue
        hits = sum(1 for v in seq[:win] if v >= threshold)
        ratio = hits / win
        if hits >= req and ratio > best_ratio:
            ok = True
            best_ratio = ratio
            best_hits = hits
            best_win = win
    return ok, best_ratio, best_hits, best_win


def best_opp_gate(seq: List[int], threshold: int) -> Tuple[bool, float, int, int]:
    ok, best_ratio, best_hits, best_win = best_gate(seq, threshold, OPP_WINDOWS)
    if len(seq) > 10:
        n = min(len(seq), 20)
        hits = sum(1 for v in seq[:n] if v >= threshold)
        ratio = hits / n if n else 0.0
        if n >= 11 and ratio >= 0.7 and ratio > best_ratio:
            ok = True
            best_ratio = ratio
            best_hits = hits
            best_win = n
    return ok, best_ratio, best_hits, best_win


def under_half_line_recent(seq: List[int], threshold: int) -> bool:
    if not seq:
        return False
    limit = max(0, int(math.floor(threshold / 2)))
    recent = seq[:10]
    under_hits = sum(1 for v in recent if v <= limit)
    return under_hits >= 2


def avg(seq: List[int], n: int = 10) -> float:
    sub = seq[:n]
    return sum(sub) / len(sub) if sub else 0.0


def build_fixture_map(league_id: int) -> Dict[int, dict]:
    blob = load_json(FIX_DIR / f"{league_id}.json")
    fmap = {}
    for fx in blob.get("fixtures") or []:
        fid = int(fx.get("id") or 0)
        parts = fx.get("participants") or []
        home = next((p for p in parts if (p.get("meta") or {}).get("location") == "home"), None)
        away = next((p for p in parts if (p.get("meta") or {}).get("location") == "away"), None)
        if not home or not away:
            continue
        fmap[fid] = {
            "id": fid,
            "home_id": int(home.get("id") or 0),
            "away_id": int(away.get("id") or 0),
            "home_name": home.get("name") or "Home",
            "away_name": away.get("name") or "Away",
            "starting_at": fx.get("starting_at") or "",
            "name": fx.get("name") or f"{home.get('name','Home')} vs {away.get('name','Away')}",
        }
    return fmap


def load_team_stats(league_id: int) -> Dict[int, dict]:
    blob = load_json(TS_DIR / f"{league_id}.json")
    return {int(t.get("team_id") or 0): t for t in blob.get("teams") or []}


def load_opp_stats(league_id: int) -> Dict[int, dict]:
    blob = load_json(OPP_DIR / f"{league_id}.json")
    return {int(t.get("team_id") or 0): t for t in blob.get("teams") or []}


def load_h2h_map(league_id: int) -> Dict[Tuple[int, int], List[dict]]:
    blob = load_json(H2H_DIR / f"{league_id}.json")
    out: Dict[Tuple[int, int], List[dict]] = {}
    for fx in blob.get("fixtures") or []:
        home = int(fx.get("home_id") or 0)
        away = int(fx.get("away_id") or 0)
        pair = tuple(sorted((home, away)))
        out.setdefault(pair, []).append(fx)
    for k in out:
        out[k].sort(key=lambda r: r.get("starting_at") or "", reverse=True)
    return out


def same_venue_series(team_rec: dict, stat_key: str, target_loc: Optional[str]) -> List[int]:
    vals = team_rec.get(stat_key) or []
    locs = team_rec.get("locations_last_n") or []
    out = []
    for v, loc in zip(vals, locs):
        if target_loc and loc != target_loc:
            continue
        try:
            out.append(int(v))
        except Exception:
            continue
        if len(out) >= 20:
            break
    return out


def h2h_last_same_venue(
    h2h_list: List[dict],
    target_home: int,
    target_away: int,
    team_id: int,
    stat: str,
) -> Optional[int]:
    for fx in h2h_list:
        if int(fx.get("home_id") or 0) != target_home or int(fx.get("away_id") or 0) != target_away:
            continue
        metas = fx.get("lastN_meta") or []
        side = "home" if team_id == target_home else "away"
        vecs = (fx.get("vectors") or {}).get(side) or {}
        for idx, meta in enumerate(metas):
            actual_home = meta.get("actual_home_id") or meta.get("home_id")
            actual_away = meta.get("actual_away_id") or meta.get("away_id")
            if int(actual_home or 0) != target_home or int(actual_away or 0) != target_away:
                continue
            if stat == "cards":
                yell = vecs.get("yellow") or []
                reds = vecs.get("red") or []
                y = yell[idx] if idx < len(yell) else None
                r = reds[idx] if idx < len(reds) else None
                if y is None and r is None:
                    return None
                try:
                    return int((y or 0) + (r or 0))
                except Exception:
                    return None
            else:
                key = "corners" if stat == "corners" else ("goals" if stat == "goals" else stat)
                vec = vecs.get(key) or []
                if idx < len(vec):
                    try:
                        return int(vec[idx])
                    except Exception:
                        return None
            break
    return None


def best_over_rows(rows: List[dict], market_descs: List[str], side: str, price_gate: float) -> List[Tuple[float, float, dict]]:
    md_norms = {norm(m) for m in market_descs}
    out = []
    for r in rows:
        if norm(r.get("market_description")) not in md_norms:
            continue
        if label_to_side(r.get("label")) != side:
            continue
        total = r.get("total") or ""
        if "over" not in (total or "").lower():
            continue
        line = parse_line(total)
        if line is None:
            continue
        try:
            price = float(r.get("value"))
        except Exception:
            continue
        if price < price_gate:
            continue
        out.append((line, price, r))
    return out


def sample_label(tag: Optional[str]) -> str:
    if tag == "home":
        return "home"
    if tag == "away":
        return "away"
    return "overall"


def extract_team_ml(odds_rows: List[dict], home_name: str, away_name: str) -> Tuple[Optional[float], Optional[float]]:
    home_ml: Optional[float] = None
    away_ml: Optional[float] = None
    for r in odds_rows or []:
        try:
            mid = int(r.get("market_id") or 0)
        except Exception:
            continue
        if mid != 1:
            continue
        try:
            price = float(r.get("value"))
        except Exception:
            continue
        label = (r.get("label") or "").strip()
        name = (r.get("name") or "").strip()
        side = label_to_side(label)
        if side == "home" or norm(label) == norm(home_name) or norm(name) == norm(home_name):
            home_ml = price if home_ml is None or price < home_ml else home_ml
        elif side == "away" or norm(label) == norm(away_name) or norm(name) == norm(away_name):
            away_ml = price if away_ml is None or price < away_ml else away_ml
    return home_ml, away_ml


def process_fixture(
    fx_meta: dict,
    odds_rows: List[dict],
    team_stats: Dict[int, dict],
    opp_stats: Dict[int, dict],
    h2h_map: Dict[Tuple[int, int], List[dict]],
    drops: Dict[str, int],
    picks: List[dict],
    home_ml: Optional[float],
    away_ml: Optional[float],
    stat_conf: dict,
) -> None:
    def drop(reason: str, ctx: Optional[dict] = None):
        drops[reason] = drops.get(reason, 0) + 1
        if DEBUG_DROPS:
            print(f"[drop] {reason} :: {ctx or fx_meta.get('id')}")

    if not within_window(fx_meta.get("starting_at", ""), WINDOW_DAYS):
        return

    pair = tuple(sorted((fx_meta["home_id"], fx_meta["away_id"])))
    h2h_list = h2h_map.get(pair, [])
    fixture_id = fx_meta.get("id")

    for side, team_id, opp_id in [
        ("home", fx_meta["home_id"], fx_meta["away_id"]),
        ("away", fx_meta["away_id"], fx_meta["home_id"]),
    ]:
        team_rec = team_stats.get(team_id)
        opp_rec = opp_stats.get(opp_id)
        if not team_rec or not opp_rec:
            drop("missing team/opp stats")
            continue

        stat_key = stat_conf["team_key"]
        opp_key = stat_conf["opp_key"]
        stat_label = stat_conf["stat_label"]

        team_series = same_venue_series(team_rec, stat_key, side)
        team_sample = side
        if len(team_series) < 9:
            team_series = same_venue_series(team_rec, stat_key, None)
            team_sample = "overall"
        opp_series = same_venue_series(
            opp_rec,
            opp_key,
            "home" if side == "away" else "away",
        )
        opp_sample = "away" if side == "home" else "home"
        if len(opp_series) < 9:
            opp_series = same_venue_series(opp_rec, opp_key, None)
            opp_sample = "overall"
        if len(team_series) < 6:
            drop("team sample <6")
            continue

        over_rows = best_over_rows(odds_rows, stat_conf["market_descs"], side, MIN_PRICE)
        if not over_rows:
            drop("no priced over rows")
            continue

        for line, price, row in over_rows:
            threshold = threshold_from_line(line)
            ok_team, team_ratio, team_hits, team_win = best_gate(team_series, threshold, TEAM_WINDOWS)
            if not ok_team:
                continue
            if under_half_line_recent(team_series, threshold):
                drop(
                    "team under half-line twice in last10",
                    {"fixture": fx_meta.get("name"), "team": team_id, "line": line, "stat": stat_label},
                )
                continue
            ok_opp, opp_ratio, opp_hits, opp_win = best_opp_gate(opp_series, threshold)
            if not ok_opp:
                continue
            h_val = h2h_last_same_venue(
                h2h_list,
                fx_meta["home_id"],
                fx_meta["away_id"],
                team_id,
                stat_conf["h2h_stat"],
            )
            if h_val is not None and h_val < threshold:
                continue

            team_avg = avg(team_series, 10)
            opp_avg = avg(opp_series, 10)
            team_seq = team_series[: team_win or len(team_series)]
            opp_seq = opp_series[: opp_win or len(opp_series)]
            picks.append(
                {
                    "fixture_id": fixture_id,
                    "fixture": fx_meta.get("name") or "",
                    "team_id": team_id,
                    "team_name": fx_meta["home_name"] if side == "home" else fx_meta["away_name"],
                    "opp_name": fx_meta["away_name"] if side == "home" else fx_meta["home_name"],
                    "stat": stat_label.upper(),
                    "line": line,
                    "threshold": threshold,
                    "price": price,
                    "team_hits": team_hits,
                    "team_win": team_win,
                    "team_ratio": team_ratio,
                    "team_sample": team_sample,
                    "team_n": len(team_series),
                    "opp_hits": opp_hits,
                    "opp_win": opp_win,
                    "opp_ratio": opp_ratio,
                    "opp_sample": opp_sample,
                    "opp_n": len(opp_series),
                    "team_avg": team_avg,
                    "opp_avg": opp_avg,
                    "team_ml": home_ml if side == "home" else away_ml,
                    "opp_ml": away_ml if side == "home" else home_ml,
                    "h2h_val": h_val,
                    "side": side,
                    "team_series": team_seq,
                    "opp_series": opp_seq,
                    "stat_conf": stat_conf["name"],
                }
            )


def dedupe_best(picks: List[dict]) -> List[dict]:
    best: Dict[Tuple[int, int, str], dict] = {}

    def score(p: dict) -> Tuple[float, float, float, float]:
        base = min(p.get("team_ratio", 0.0), p.get("opp_ratio", 0.0))
        return (
            base,
            p.get("team_ratio", 0.0),
            p.get("opp_ratio", 0.0),
            -(p.get("price") or 0.0),
        )

    for p in picks:
        key = (p.get("fixture_id"), p.get("team_id"), p.get("stat_conf"))
        cur = best.get(key)
        if cur is None or score(p) > score(cur):
            best[key] = p
    return list(best.values())


def format_pick(p: dict) -> str:
    stat_label = p.get("stat_label_override") or (
        "cards" if p.get("stat") == "CARDS" else ("corners" if p.get("stat") == "CORNERS" else "goals")
    )
    threshold = p.get("threshold")
    team_seq = p.get("team_series") or []
    opp_seq = p.get("opp_series") or []
    team_win = p.get("team_win") or len(team_seq)
    opp_win = p.get("opp_win") or len(opp_seq)
    opp_sample = sample_label(p.get("opp_sample"))
    opp_phrase = {"home": "at home", "away": "away from home", "overall": "overall"}.get(opp_sample, "overall")
    lines: List[str] = []
    lines.append(f"{p['fixture']} — {p['team_name']} {threshold}+ {stat_label} @ {p['price']:.2f}")
    lines.append(
        f"{p['team_name']} have had {threshold}+ {stat_label} in {p['team_hits']}/{p['team_win']} = "
        f"{','.join(str(x) for x in team_seq[:team_win])}"
    )
    lines.append(
        f"{p['opp_name']} have conceded {threshold}+ {stat_label} in {p['opp_hits']}/{p['opp_win']} {opp_phrase} = "
        f"{','.join(str(x) for x in opp_seq[:opp_win])}"
    )
    if p.get("h2h_val") is not None:
        lines.append(f"{p['team_name']} had {p['h2h_val']} {stat_label} in this fixture last season.")
    else:
        lines.append("No same-venue H2H stat available.")
    lines.append(
        f"Over their last 10 games: {p['team_name']} average {p['team_avg']:.1f} {stat_label}, "
        f"{p['opp_name']} average {p['opp_avg']:.1f} {stat_label} conceded."
    )
    return "\n".join(lines)


def run_config(stat_conf: dict) -> str:
    leagues_env = os.getenv("LEAGUE_IDS")
    if leagues_env:
        league_ids = [int(x) for x in leagues_env.split(",") if x.strip()]
    else:
        league_ids = [int(p.stem) for p in FIX_DIR.glob("*.json") if p.stem.isdigit()]

    drops: Dict[str, int] = {}
    picks: List[dict] = []

    for lid in league_ids:
        fmap = build_fixture_map(lid)
        odds_blob = load_json(ODDS_DIR / f"{lid}.json")
        team_stats = load_team_stats(lid)
        opp_stats = load_opp_stats(lid)
        h2h_map = load_h2h_map(lid)

        for fx in odds_blob.get("fixtures") or []:
            fid = int(fx.get("fixture_id") or fx.get("id") or 0)
            meta = fmap.get(fid)
            if not meta:
                drops["fixture missing"] = drops.get("fixture missing", 0) + 1
                continue
            odds_rows = fx.get("odds") or []
            if not odds_rows:
                drops["missing odds rows"] = drops.get("missing odds rows", 0) + 1
                continue
            home_ml, away_ml = extract_team_ml(odds_rows, meta["home_name"], meta["away_name"])
            process_fixture(meta, odds_rows, team_stats, opp_stats, h2h_map, drops, picks, home_ml, away_ml, stat_conf)

    picks = [p for p in picks if p.get("stat_conf") == stat_conf["name"]]
    picks = dedupe_best(picks)
    picks.sort(key=lambda p: (p.get("fixture", ""), p.get("team_name", "")))

    lines: List[str] = []
    ts = dt.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%SZ")
    lines.append(f"Generated at (UTC): {ts}")
    lines.append(f"Price gate: >= {MIN_PRICE:.2f} (overs only)")
    lines.append(f"Team windows: {', '.join([f'{req}/{win}' for win, req in TEAM_WINDOWS])}")
    lines.append(f"Opponent allow: {', '.join([f'{req}/{win}' for win, req in OPP_WINDOWS])} or >=70% if n>10")
    lines.append("")
    for p in picks:
        lines.append(format_pick(p))
        lines.append("")
    if drops:
        lines.append("")
        lines.append("Drop summary:")
        for k, v in sorted(drops.items(), key=lambda kv: (-kv[1], kv[0])):
            lines.append(f"  {k}: {v}")

    out_text = "\n".join(lines).strip() + "\n"
    stat_conf["outfile"].write_text(out_text, encoding="utf-8")
    return out_text


def main():
    outputs = []
    for conf in STAT_CONFIGS:
        outputs.append(run_config(conf))
    print("\n\n".join(outputs))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
