#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Premier League — Best upcoming Over 2.5 matches
Uses each team's last N games (default 10) to compute O2.5 hit rates, then
ranks upcoming fixtures by the average (Combined%) of both teams’ hit rates.

Inputs (ENV):
  LEAGUE_ID     (default: 8)
  LAST_N        (default: 10)
  MIN_GAMES     (default: 6)   # skip teams with too few recent games
  OUTPUT_PATH   (default: posts/pl_over25_matches.md)
  O25_SPLIT_MODE (default: "mixed")  # "mixed" = all games; "split" = home-only for home team, away-only for away team

Requires:
  - data/fixtures/by_league/{LEAGUE_ID}.json
  - data/team_stats/by_league/{LEAGUE_ID}.json          (needs goals_last_n)
  - data/team_opponent_stats/by_league/{LEAGUE_ID}.json (needs opp_goals_last_n)
"""

import os
import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT        = Path(".")
LEAGUE_ID   = int(os.getenv("LEAGUE_ID", "8"))
LAST_N      = int(os.getenv("LAST_N", "10"))
MIN_GAMES   = int(os.getenv("MIN_GAMES", "6"))
OUT_PATH    = Path(os.getenv("OUTPUT_PATH", "posts/pl_over25_matches.md"))
SPLIT_MODE  = os.getenv("O25_SPLIT_MODE", "mixed").strip().lower()  # "mixed" | "split"

FIX_FILE    = ROOT / "data" / "fixtures" / "by_league" / f"{LEAGUE_ID}.json"
TEAM_FILE   = ROOT / "data" / "team_stats" / "by_league" / f"{LEAGUE_ID}.json"
OPP_FILE    = ROOT / "data" / "team_opponent_stats" / "by_league" / f"{LEAGUE_ID}.json"

def _load(p: Path) -> Any:
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None

def _participants_to_pair(fx: dict) -> Optional[Tuple[Tuple[int,str], Tuple[int,str]]]:
    """Return ((home_id,home_name),(away_id,away_name)) if we can infer; else None."""
    parts = fx.get("participants")
    if isinstance(parts, list) and len(parts) >= 2:
        home = None
        away = None
        for idx, p in enumerate(parts[:2]):
            pid = p.get("id") or p.get("team_id")
            name = p.get("name") or ""
            meta = p.get("meta") or {}
            loc = (meta.get("location") or "").lower()
            try:
                pid = int(pid)
            except Exception:
                return None
            if "home" in loc or "local" in loc:
                home = (pid, name)
            elif "away" in loc or "visitor" in loc:
                away = (pid, name)
            elif idx == 0 and home is None:
                home = (pid, name)
            elif idx == 1 and away is None:
                away = (pid, name)
        if home and away:
            return home, away
    elif isinstance(parts, dict):
        h = parts.get("home") or parts.get("localteam") or parts.get("home_team")
        a = parts.get("away") or parts.get("visitorteam") or parts.get("away_team")
        try:
            if h and a:
                return (int(h.get("id") or h.get("team_id")), h.get("name") or ""), \
                       (int(a.get("id") or a.get("team_id")), a.get("name") or "")
        except Exception:
            return None
    return None

def take_last_n(arr: List[int], n: int) -> List[int]:
    if not isinstance(arr, list):
        return []
    return [int(x) for x in arr[:n]]

def filter_by_location(row: dict, want: str, arr_key: str) -> Tuple[List[int], List[int]]:
    """Return values and fixture_ids filtered by location ('home' or 'away')."""
    vals = row.get(arr_key) or []
    fids = row.get("fixture_ids") or []
    locs = row.get("locations_last_n") or []
    out_v, out_f = [], []
    for v, fid, loc in zip(vals, fids, locs):
        if (want == "home" and loc == "home") or (want == "away" and loc == "away"):
            try:
                out_v.append(int(v)); out_f.append(int(fid))
            except Exception:
                continue
    return out_v, out_f

def aligned_totals(team_row: dict, opp_row: dict, last_n: int, split_side: Optional[str] = None) -> List[int]:
    """
    Build totals = GF + oppGA aligned by fixture_id.
    If split_side is "home"/"away", only use those fixtures from team_row; otherwise use mixed last_N.
    """
    if split_side in {"home", "away"}:
        gf_vals, gf_fids = filter_by_location(team_row, split_side, "goals_last_n")
    else:
        gf_vals = take_last_n(team_row.get("goals_last_n") or [], last_n)
        gf_fids = (team_row.get("fixture_ids") or [])[:last_n]

    ga_vals = take_last_n(opp_row.get("opp_goals_last_n") or [], last_n)
    ga_fids = (opp_row.get("fixture_ids") or [])[:last_n]

    idx_ga = {fid: i for i, fid in enumerate(ga_fids)}
    totals = []
    for i, fid in enumerate(gf_fids):
        j = idx_ga.get(fid)
        if j is None:
            continue
        try:
            totals.append(int(gf_vals[i]) + int(ga_vals[j]))
        except Exception:
            continue
    return totals

def build_team_o25_map() -> Dict[int, dict]:
    team_blob = _load(TEAM_FILE) or {}
    opp_blob  = _load(OPP_FILE)  or {}

    rows_t = {int(r["team_id"]): r for r in (team_blob.get("teams") or []) if isinstance(r.get("team_id"), int)}
    rows_o = {int(r["team_id"]): r for r in (opp_blob.get("teams") or [])  if isinstance(r.get("team_id"), int)}

    out: Dict[int, dict] = {}
    for tid, tr in rows_t.items():
        orow = rows_o.get(tid)
        if not orow:
            continue

        side = None
        if SPLIT_MODE == "split":
            # We'll decide per use: 'home' for home team, 'away' for away team at fixture time.
            pass

        totals_mixed = aligned_totals(tr, orow, LAST_N, split_side=None)
        n_mixed = len(totals_mixed)
        if n_mixed >= MIN_GAMES:
            hits = sum(1 for x in totals_mixed if x >= 3)
            pct  = round((hits / n_mixed) * 100)
            out[tid] = {
                "team_name": tr.get("team_name") or "",
                "mixed": {"hits": hits, "games": n_mixed, "pct": pct},
            }
        else:
            out[tid] = {"team_name": tr.get("team_name") or "", "mixed": None}

        if SPLIT_MODE == "split":
            totals_home = aligned_totals(tr, orow, LAST_N, split_side="home")
            totals_away = aligned_totals(tr, orow, LAST_N, split_side="away")
            out[tid]["home"] = None
            out[tid]["away"] = None
            if len(totals_home) >= max(3, MIN_GAMES//2):
                h_hits = sum(1 for x in totals_home if x >= 3)
                h_pct  = round((h_hits / len(totals_home)) * 100)
                out[tid]["home"] = {"hits": h_hits, "games": len(totals_home), "pct": h_pct}
            if len(totals_away) >= max(3, MIN_GAMES//2):
                a_hits = sum(1 for x in totals_away if x >= 3)
                a_pct  = round((a_hits / len(totals_away)) * 100)
                out[tid]["away"] = {"hits": a_hits, "games": len(totals_away), "pct": a_pct}

    return out

def collect_upcoming_pairs() -> List[dict]:
    blob = _load(FIX_FILE) or {}
    fixtures = blob.get("fixtures") or []
    pairs = []
    for fx in fixtures:
        pa = _participants_to_pair(fx)
        if not pa:
            continue
        (hid, hname), (aid, aname) = pa
        pairs.append({
            "fixture_id": fx.get("id"),
            "starting_at": fx.get("starting_at"),
            "home_id": hid, "home_name": hname,
            "away_id": aid, "away_name": aname,
        })
    return pairs

def main():
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)

    o25_by_team = build_team_o25_map()
    pairs = collect_upcoming_pairs()

    rows = []
    for fx in pairs:
        tH = o25_by_team.get(fx["home_id"])
        tA = o25_by_team.get(fx["away_id"])
        if not tH or not tA:
            continue

        if SPLIT_MODE == "split":
            home = tH.get("home") or tH.get("mixed")
            away = tA.get("away") or tA.get("mixed")
        else:
            home = tH.get("mixed")
            away = tA.get("mixed")

        if not home or not away:
            continue

        combined = round((home["pct"] + away["pct"]) / 2)
        rows.append({
            **fx,
            "home_pct": home["pct"], "home_hits": home["hits"], "home_games": home["games"],
            "away_pct": away["pct"], "away_hits": away["hits"], "away_games": away["games"],
            "combined": combined,
        })

    rows.sort(key=lambda r: (-r["combined"], r["starting_at"] or "", r["home_name"]))

    header = [
        f"Premier League — Best upcoming matches for **Over 2.5 Goals**",
        f"Basis: each team’s last {LAST_N} games (O2.5 = total goals ≥ 3).",
        "Combined% = average of both teams’ hit rates.",
        "",
    ]

    lines: List[str] = []
    if not rows:
        lines.append("_No qualifying fixtures (missing goals arrays or not enough games)._")
    else:
        lines.append("📊 **Top Over 2.5 candidates (sorted by Combined%)**")
        lines.append("")
        for r in rows[:12]:
            lines.append(
                f"• {r['home_name']} vs {r['away_name']} — Combined **{r['combined']}%** "
                f"(H: {r['home_pct']}% {r['home_hits']}/{r['home_games']}, "
                f"A: {r['away_pct']}% {r['away_hits']}/{r['away_games']})"
            )
        lines.append("")
        lines.append("_Note: uses last-N form only (no odds). Interpret as context, not a pick._")

    text = "\n".join(header + lines) + "\n"
    OUT_PATH.write_text(text, encoding="utf-8")
    print(f"Wrote {OUT_PATH}")

if __name__ == "__main__":
    main()
