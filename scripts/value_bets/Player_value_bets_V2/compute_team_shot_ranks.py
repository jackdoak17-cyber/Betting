#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Compute team shot rankings for Player Value Bets V2.

Uses locally stored team stats (shots for) and opponent stats (shots conceded)
to rank teams within each league. Results are saved per league to
``data/value_bets/Player_value_bets_V2/team_shot_ranks/{league_id}.json``.

Metrics:
  • Shots conceded: average opp_shots_total_last_n (ascending = stingiest)
  • Shots for     : average shots_total_last_n (descending = highest volume)

Env (optional):
  • LEAGUE_IDS   — comma-separated list of league IDs (defaults to standard set)
  • MIN_SAMPLE   — minimum games required to compute an average (default 3)

Outputs include sample sizes and are sorted for convenience; you can take the
first 5 of shots_conceded_rank as the avoid list and the first 3 of
shots_for_rank as high-volume attackers.
"""
import json
import os
import statistics
import datetime as dt
from pathlib import Path
from typing import Dict, List, Optional, Tuple

DEFAULT_LEAGUES = [301, 384, 387, 564, 567, 600, 8, 82, 9]
LEAGUE_IDS: List[int] = [
    int(x)
    for x in (os.getenv("LEAGUE_IDS") or ",".join(map(str, DEFAULT_LEAGUES))).split(",")
    if x.strip()
]
MIN_SAMPLE = int(os.getenv("MIN_SAMPLE", "3"))

TEAM_STATS_DIR = Path("data/team_stats/by_league")
OPP_STATS_DIR = Path("data/team_opponent_stats/by_league")
OUT_DIR = Path("data/value_bets/Player_value_bets_V2/team_shot_ranks")
OUT_DIR.mkdir(parents=True, exist_ok=True)


# ---------- helpers ----------
def _load_json(path: Path) -> Optional[dict]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _avg(seq: List[int]) -> Optional[float]:
    vals = [int(x) for x in seq if isinstance(x, (int, float))]
    if len(vals) < MIN_SAMPLE:
        return None
    return statistics.mean(vals)


def _team_rows(blob: Optional[dict], shots_key: str) -> Dict[int, Tuple[str, Optional[float], int]]:
    """
    Returns mapping team_id -> (team_name, average, sample_size)
    for the given shots key.
    """
    out: Dict[int, Tuple[str, Optional[float], int]] = {}
    if not blob:
        return out
    for row in blob.get("teams", []) or []:
        tid = row.get("team_id")
        tname = row.get("team_name")
        series = row.get(shots_key) or []
        if not isinstance(tid, int) or not isinstance(tname, str):
            continue
        avg = _avg(series)
        out[tid] = (tname, avg, len(series))
    return out


def compute_league(league_id: int) -> Optional[dict]:
    team_blob = _load_json(TEAM_STATS_DIR / f"{league_id}.json")
    opp_blob = _load_json(OPP_STATS_DIR / f"{league_id}.json")
    if not team_blob and not opp_blob:
        return None

    shots_for = _team_rows(team_blob, "shots_total_last_n")
    shots_conc = _team_rows(opp_blob, "opp_shots_total_last_n")

    # Sort: conceded ascending (stingiest first), for descending
    conc_rank = [
        {
            "team_id": tid,
            "team_name": name,
            "avg_shots_conceded": avg,
            "sample_size": sample,
        }
        for tid, (name, avg, sample) in shots_conc.items()
        if avg is not None
    ]
    conc_rank.sort(key=lambda r: (r["avg_shots_conceded"], r["team_name"].lower()))

    for_rank = [
        {
            "team_id": tid,
            "team_name": name,
            "avg_shots_for": avg,
            "sample_size": sample,
        }
        for tid, (name, avg, sample) in shots_for.items()
        if avg is not None
    ]
    for_rank.sort(key=lambda r: (-r["avg_shots_for"], r["team_name"].lower()))

    return {
        "league_id": league_id,
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "shots_conceded_rank": conc_rank,
        "shots_for_rank": for_rank,
        "min_sample": MIN_SAMPLE,
        "team_count": len(shots_for) or len(shots_conc),
    }


def write_league(payload: dict) -> None:
    out = OUT_DIR / f"{payload['league_id']}.json"
    tmp = out.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(out)


# ---------- main ----------
def main() -> None:
    summaries = []
    for lid in LEAGUE_IDS:
        payload = compute_league(lid)
        if not payload:
            print(f"[skip] league {lid}: missing team/opponent stats")
            continue
        write_league(payload)
        conc = payload["shots_conceded_rank"][:5]
        top_for = payload["shots_for_rank"][:3]
        summaries.append((lid, conc, top_for))
        print(f"[ok] league {lid}: wrote {OUT_DIR / (str(lid) + '.json')}")
    print("\nSummary (top lists):")
    for lid, conc, top_for in summaries:
        conc_names = ", ".join(f"{r['team_name']} ({r['avg_shots_conceded']:.2f})" for r in conc)
        for_names = ", ".join(f"{r['team_name']} ({r['avg_shots_for']:.2f})" for r in top_for)
        print(f"  L{lid} — stingiest: {conc_names} | top shooters: {for_names}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
