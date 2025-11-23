#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Compute average possession ranks per league for Player Value Bets V2.

Uses locally stored team possession time series to rank teams within each league
by average possession (higher first). Results are saved per league to
``data/value_bets/Player_value_bets_V2/team_possession_ranks/{league_id}.json``.

Env (optional):
  • LEAGUE_IDS — comma-separated list of league IDs (defaults to standard set)
  • MIN_SAMPLE — minimum games required to compute an average (default 3)
"""
import datetime as dt
import json
import os
import statistics
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
OUT_DIR = Path("data/value_bets/Player_value_bets_V2/team_possession_ranks")
OUT_DIR.mkdir(parents=True, exist_ok=True)


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


def _team_rows(blob: Optional[dict]) -> Dict[int, Tuple[str, Optional[float], int]]:
    out: Dict[int, Tuple[str, Optional[float], int]] = {}
    if not blob:
        return out
    for row in blob.get("teams", []) or []:
        tid = row.get("team_id")
        tname = row.get("team_name")
        series = row.get("possession_last_n") or []
        if not isinstance(tid, int) or not isinstance(tname, str):
            continue
        avg = _avg(series)
        out[tid] = (tname, avg, len(series))
    return out


def compute_league(league_id: int) -> Optional[dict]:
    team_blob = _load_json(TEAM_STATS_DIR / f"{league_id}.json")
    if not team_blob:
        return None

    possession_rows = _team_rows(team_blob)

    ranked = [
        {
            "team_id": tid,
            "team_name": name,
            "avg_possession": avg,
            "sample_size": sample,
        }
        for tid, (name, avg, sample) in possession_rows.items()
        if avg is not None
    ]
    ranked.sort(key=lambda r: (-r["avg_possession"], r["team_name"].lower()))

    return {
        "league_id": league_id,
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "possession_rank": ranked,
        "min_sample": MIN_SAMPLE,
        "team_count": len(possession_rows),
    }


def write_league(payload: dict) -> None:
    out = OUT_DIR / f"{payload['league_id']}.json"
    tmp = out.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(out)


def main() -> None:
    summaries = []
    for lid in LEAGUE_IDS:
        payload = compute_league(lid)
        if not payload:
            print(f"[skip] league {lid}: missing team stats")
            continue
        write_league(payload)
        top5 = payload["possession_rank"][:5]
        summaries.append((lid, top5))
        print(f"[ok] league {lid}: wrote {OUT_DIR / (str(lid) + '.json')}")
    print("\nTop-5 possession leaders per league:")
    for lid, top5 in summaries:
        names = ", ".join(f"{r['team_name']} ({r['avg_possession']:.1f}%)" for r in top5)
        print(f"  L{lid}: {names}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
