#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Select players who satisfy coverage criteria over their last-N series
(already collected in your data/ files) and render clean lists.

Reads (by league):
  - data/player_shots/by_league/{lid}.json            -> key: shots_last_n
  - data/player_shots_on_target/by_league/{lid}.json  -> key: on_target_last_n
  - data/player_fouls/by_league/{lid}.json            -> key: fouls_last_n
  - data/player_tackles/by_league/{lid}.json          -> key: tackles_last_n
And team names from:
  - data/predicted_xi/by_league/{lid}.json

Writes:
  - data/player_filters/players_by_criteria.txt
  - data/player_filters/players_by_criteria.json

Rules (per stat & threshold):
  1) 100% of last games (min n >= 8)
  2) >=90% of last games (min n >= 8)
  3) 100% of last 5 (n >= 5)
  4) >=4 of last 5 (n >= 5)
Players appear at most once per (stat, threshold) — first matching bucket wins.
"""

import json, glob
from pathlib import Path
from typing import Dict, List, Any, Tuple
from datetime import datetime

ROOT = Path(".")
OUT_DIR = ROOT / "data" / "player_filters"
OUT_DIR.mkdir(parents=True, exist_ok=True)
OUT_TXT  = OUT_DIR / "players_by_criteria.txt"
OUT_JSON = OUT_DIR / "players_by_criteria.json"

# Where the inputs live
PX_DIR      = ROOT / "data" / "predicted_xi" / "by_league"
SHOTS_DIR   = ROOT / "data" / "player_shots" / "by_league"
SOT_DIR     = ROOT / "data" / "player_shots_on_target" / "by_league"
FOULS_DIR   = ROOT / "data" / "player_fouls" / "by_league"
TACK_DIR    = ROOT / "data" / "player_tackles" / "by_league"

# Config
MIN_GAMES_ALL = 8       # for "last games" buckets
LAST_K = 5              # for "last 5" buckets
ALL_THRESHOLDS = [1, 2, 3]

# ---- utils ----
def _load_json(p: Path) -> Any:
    if not p.is_file(): return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None

def _discover_leagues(paths: List[Path]) -> List[int]:
    lids = set()
    for base in paths:
        for p in base.glob("*.json"):
            try:
                lids.add(int(p.stem))
            except Exception:
                pass
    return sorted(lids)

def _team_name_map(league_id: int) -> Dict[int, str]:
    """Map team_id -> team_name from predicted_xi file."""
    blob = _load_json(PX_DIR / f"{league_id}.json") or {}
    m: Dict[int, str] = {}
    for fx in (blob.get("fixtures") or []):
        for side in ("home", "away"):
            s = fx.get(side) or {}
            tid, nm = s.get("team_id"), s.get("name")
            if isinstance(tid, int) and isinstance(nm, str) and nm:
                m.setdefault(tid, nm)
    return m

def _series(row: dict, key: str) -> List[int]:
    seq = row.get(key) or []
    if not isinstance(seq, list): return []
    out = []
    for x in seq:
        try: out.append(int(x))
        except Exception:
            try: out.append(int(float(x)))
            except Exception: out.append(0)
    return out

def _pos_tag(row: dict) -> str:
    tag = row.get("position_tag")
    return tag.strip() if isinstance(tag, str) else ""

def _coverage(seq: List[int], threshold: int) -> float:
    if not seq: return 0.0
    hits = sum(1 for v in seq if v >= threshold)
    return hits / len(seq)

def _last_k(seq: List[int], k: int) -> List[int]:
    return (seq or [])[:k]

def _meets_all(seq: List[int], threshold: int, min_games: int, pct: float) -> bool:
    if len(seq) < min_games: return False
    return _coverage(seq, threshold) >= pct

def _meets_lastk_all(seq: List[int], threshold: int, k: int) -> bool:
    s = _last_k(seq, k)
    return len(s) == k and all(v >= threshold for v in s)

def _meets_lastk_atleast(seq: List[int], threshold: int, k: int, atleast: int) -> bool:
    s = _last_k(seq, k)
    return len(s) == k and sum(1 for v in s if v >= threshold) >= atleast

# ---- core selection ----
def _read_players_by_league(base: Path, key: str) -> Dict[int, List[dict]]:
    """
    Returns: by_league[lid] = list of player rows with 'series', 'team_id', 'name', 'position_tag'
    """
    out: Dict[int, List[dict]] = {}
    for p in base.glob("*.json"):
        data = _load_json(p) or {}
        try:
            lid = int(data.get("league_id") or p.stem)
        except Exception:
            continue
        rows = []
        for r in (data.get("players") or []):
            seq = _series(r, key)
            rows.append({
                "league_id": lid,
                "team_id": r.get("team_id"),
                "player_id": r.get("player_id"),
                "name": r.get("name") or f"Player {r.get('player_id')}",
                "position": _pos_tag(r),
                "series": seq,           # latest -> older
            })
        out[lid] = rows
    return out

def select_for_stat(stat_label: str, base_dir: Path, series_key: str) -> dict:
    """
    Build buckets per threshold for one stat.
    Returns a structured dict ready for JSON emission.
    """
    by_league = _read_players_by_league(base_dir, series_key)
    leagues = sorted(by_league.keys())
    result = {
        "stat": stat_label,
        "thresholds": {}
    }

    for thr in ALL_THRESHOLDS:
        # buckets with de-dup per (league, player_id)
        placed: Dict[int, set] = {lid: set() for lid in leagues}
        buckets = {
            "100pct_all": [],     # n>=8 & coverage == 1.0
            "90pct_all": [],      # n>=8 & coverage >= 0.9 (but not 100)
            "100pct_last5": [],   # n>=5 & last5 all >= thr
            "4of5_last5": [],     # n>=5 & last5 >= thr in >=4 matches
        }

        for lid in leagues:
            team_names = _team_name_map(lid)
            rows = by_league.get(lid, [])
            # iterate players, place into first matching bucket only
            for r in rows:
                pid = r["player_id"]
                if pid in placed[lid]:
                    continue
                seq = r["series"]

                # 1) 100% of last games (min 8)
                if _meets_all(seq, thr, MIN_GAMES_ALL, 1.0):
                    buckets["100pct_all"].append({
                        **r,
                        "team": team_names.get(r["team_id"], f"Team {r['team_id']}"),
                        "n": len(seq),
                        "hit_rate": 1.0,
                        "threshold": thr,
                    })
                    placed[lid].add(pid)
                    continue

                # 2) >=90% of last games (min 8)
                if _meets_all(seq, thr, MIN_GAMES_ALL, 0.9):
                    buckets["90pct_all"].append({
                        **r,
                        "team": team_names.get(r["team_id"], f"Team {r['team_id']}"),
                        "n": len(seq),
                        "hit_rate": round(_coverage(seq, thr), 3),
                        "threshold": thr,
                    })
                    placed[lid].add(pid)
                    continue

                # 3) 100% of last 5
                if _meets_lastk_all(seq, thr, LAST_K):
                    buckets["100pct_last5"].append({
                        **r,
                        "team": team_names.get(r["team_id"], f"Team {r['team_id']}"),
                        "n": len(seq),
                        "hit_last5": LAST_K,
                        "threshold": thr,
                    })
                    placed[lid].add(pid)
                    continue

                # 4) >=4 of last 5
                if _meets_lastk_atleast(seq, thr, LAST_K, 4):
                    buckets["4of5_last5"].append({
                        **r,
                        "team": team_names.get(r["team_id"], f"Team {r['team_id']}"),
                        "n": len(seq),
                        "hit_last5": sum(1 for v in seq[:LAST_K] if v >= thr),
                        "threshold": thr,
                    })
                    placed[lid].add(pid)
                    continue

        # Sort nicely within each bucket (league, team name, player)
        def sort_key(x):
            return (x["league_id"], x.get("team","").lower(), x.get("name","").lower())

        for b in buckets:
            buckets[b].sort(key=sort_key)

        result["thresholds"][thr] = buckets

    return result

# ---- main ----
def format_txt(all_stats_result: Dict[str, dict]) -> str:
    lines: List[str] = []
    lines.append(f"Generated at (UTC): {datetime.utcnow().isoformat()}")
    lines.append("Criteria order: 100% (n>=8) -> 90% (n>=8) -> 100% of last 5 -> 4 of last 5")
    lines.append("Series shown latest → older.\n")

    for stat_label, payload in all_stats_result.items():
        lines.append(f"===== {stat_label.upper()} =====")
        for thr in ALL_THRESHOLDS:
            lines.append(f"-- Threshold: {thr}+")

            for bucket_label in ("100pct_all", "90pct_all", "100pct_last5", "4of5_last5"):
                rows = payload["thresholds"][thr][bucket_label]
                if not rows:
                    continue
                nice = {
                    "100pct_all": "100% of last games (min 8)",
                    "90pct_all":  "≥90% of last games (min 8)",
                    "100pct_last5": "100% of last 5",
                    "4of5_last5":  "≥4 of last 5",
                }[bucket_label]
                lines.append(f"  {nice}:")
                for r in rows:
                    seq = r["series"]
                    series_str = ",".join(str(v) for v in seq)
                    pos = f", {r['position']}" if r.get("position") else ""
                    lines.append(f"    • {r['name']} ({r['team']}{pos}): {series_str}  [n={len(seq)}]")
                lines.append("")  # blank after bucket
            lines.append("")      # blank after threshold
        lines.append("")          # blank after stat
    return "\n".join(lines).rstrip() + "\n"

def main():
    # Build results for each stat
    results_by_stat: Dict[str, dict] = {}

    results_by_stat["shots"] = select_for_stat(
        "shots", SHOTS_DIR, "shots_last_n"
    )
    results_by_stat["shots_on_target"] = select_for_stat(
        "shots_on_target", SOT_DIR, "on_target_last_n"
    )
    results_by_stat["fouls"] = select_for_stat(
        "fouls", FOULS_DIR, "fouls_last_n"
    )
    results_by_stat["tackles"] = select_for_stat(
        "tackles", TACK_DIR, "tackles_last_n"
    )

    # Write JSON
    json_payload = {
        "generated_at": datetime.utcnow().isoformat(),
        "criteria": {
            "order": ["100pct_all (n>=8)", "90pct_all (n>=8)", "100pct_last5", "4of5_last5"],
            "thresholds": ALL_THRESHOLDS,
            "last_k": LAST_K,
            "min_games_all": MIN_GAMES_ALL,
        },
        "stats": results_by_stat,
    }
    OUT_JSON.write_text(json.dumps(json_payload, ensure_ascii=False, indent=2), encoding="utf-8")

    # Write TXT
    OUT_TXT.write_text(format_txt(results_by_stat), encoding="utf-8")

    print(f"[OK] wrote {OUT_TXT} and {OUT_JSON}")

if __name__ == "__main__":
    main()
