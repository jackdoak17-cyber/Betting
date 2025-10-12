#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Build shots-on-target series for the SAME players/fixtures captured by player_shots.

Reads:
  data/player_shots/by_league/{league_id}.json   (expects player objects with fixture_ids, minutes_last_n)

Calls:
  GET /v3/football/fixtures/{fixture_id}?include=statistics;statistics.player

Writes:
  data/player_sot/by_league/{league_id}.json
  data/player_sot/combined.json
  data/player_sot/summary.txt

Notes:
  - Current season, league-only, ≥45' is already enforced by your player_shots job.
  - We reuse those exact fixture_ids to pull SOT -> perfectly aligned sequences.
"""

import os, sys, time, json, glob
import datetime as dt
from pathlib import Path
from typing import Dict, List, Any, Optional

API_BASE = "https://api.sportmonks.com/v3/football"
API_TOKEN = os.getenv("SPORTMONKS_TOKEN") or os.getenv("SPORTMONKS_API_TOKEN") or os.getenv("SM_TOKEN")
if not API_TOKEN:
    print("ERROR: SPORTMONKS_TOKEN / SPORTMONKS_API_TOKEN not set.", file=sys.stderr)
    sys.exit(1)

TIMEOUT = 25
RETRIES = 3
BACKOFF = 1.6
GLOBAL_MIN_DELAY = 0.18

ROOT = Path(".")
SHOTS_DIR = ROOT / "data" / "player_shots" / "by_league"
OUT_DIR   = ROOT / "data" / "player_sot"
BY_LG_DIR = OUT_DIR / "by_league"
OUT_DIR.mkdir(parents=True, exist_ok=True)
BY_LG_DIR.mkdir(parents=True, exist_ok=True)

# ----- HTTP helper with memo -----
import requests
_MEMO: Dict[str, dict] = {}
_last_call_ts = 0.0

def _pace():
    global _last_call_ts
    now = time.time()
    if now - _last_call_ts < GLOBAL_MIN_DELAY:
        time.sleep(GLOBAL_MIN_DELAY - (now - _last_call_ts))
    _last_call_ts = time.time()

def _key(url: str, params: dict) -> str:
    return url + "?" + "&".join(f"{k}={params[k]}" for k in sorted(params))

def api_get(path: str, params: Optional[dict] = None) -> dict:
    if params is None: params = {}
    params = {**params, "api_token": API_TOKEN}
    url = f"{API_BASE}/{path.lstrip('/')}"
    k = _key(url, params)
    if k in _MEMO: return _MEMO[k]
    last_exc = None
    for i in range(1, RETRIES + 1):
        _pace()
        try:
            r = requests.get(url, params=params, timeout=TIMEOUT)
            if r.status_code == 429:
                sleep = min(60, (BACKOFF ** i) * 2.0)
                print(f"[429] {path} — sleeping {sleep:.1f}s")
                time.sleep(sleep)
                continue
            r.raise_for_status()
            j = r.json()
            _MEMO[k] = j
            return j
        except Exception as e:
            last_exc = e
            if i < RETRIES:
                sleep = BACKOFF ** i
                print(f"[RETRY] {path} (attempt {i}) sleeping {sleep:.1f}s")
                time.sleep(sleep)
            else:
                raise
    raise last_exc  # pragma: no cover

# ----- IO helpers -----
def _load_json(p: Path) -> Any:
    if not p.is_file(): return None
    with p.open("r", encoding="utf-8") as f:
        try: return json.load(f)
        except Exception: return None

def _write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, separators=(",", ":"), sort_keys=False)
    tmp.replace(path)

def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        f.write(text)
    tmp.replace(path)

# ----- parsing helpers -----
def _sot_from_stat_row(row: dict) -> Optional[int]:
    """
    Be liberal in what we accept:
      - row.get('shots', {}).get('on_target' or 'onTarget')
      - row.get('shots_on_target') or variants
    Return 0 if present but falsy, else None if truly missing.
    """
    if not isinstance(row, dict):
        return None

    shots = row.get("shots")
    if isinstance(shots, dict):
        for k in ("on_target", "onTarget", "on_target_shots", "onTargetShots"):
            if k in shots:
                try: return int(shots[k] or 0)
                except Exception: return 0

        # sometimes shots might be list of typed stats
        if isinstance(shots, list):
            for it in shots:
                if not isinstance(it, dict): continue
                name = (it.get("name") or it.get("type") or "").lower()
                if "on target" in name or "on_target" in name:
                    try: return int(it.get("value") or 0)
                    except Exception: return 0

    # flat fields
    for k in ("shots_on_target", "shotsOnTarget", "on_target", "onTarget"):
        if k in row:
            try: return int(row[k] or 0)
            except Exception: return 0

    return None

def _fixture_player_sot_map(fixture: dict) -> Dict[int, int]:
    """
    Return {player_id: sot} for a single fixture payload with statistics included.
    """
    out: Dict[int, int] = {}
    if not isinstance(fixture, dict): return out
    stats = fixture.get("statistics") or fixture.get("stats") or []
    if isinstance(stats, dict):
        # some responses wrap at 'statistics' -> {'data': [...]}
        stats = stats.get("data") or []
    if not isinstance(stats, list): stats = []

    for row in stats:
        if not isinstance(row, dict): continue
        pid = row.get("player_id") or row.get("playerId") or (row.get("player") or {}).get("id")
        if not isinstance(pid, int): continue
        val = _sot_from_stat_row(row)
        if val is None:
            # sometimes the SOT sits under nested 'details' or similar:
            details = row.get("details") or {}
            if isinstance(details, dict):
                val = _sot_from_stat_row(details)
        if val is None:
            val = 0
        out[pid] = int(val)
    return out

# ----- main -----
def build_for_league(league_path: Path) -> Optional[dict]:
    src = _load_json(league_path) or {}
    players = src.get("players") or []
    if not players:
        return None

    # Collect unique fixture ids we need to inspect
    fid_set = set()
    for p in players:
        for fid in (p.get("fixture_ids") or []):
            try: fid_set.add(int(fid))
            except Exception: pass

    # Build one map per fixture, then emit SOT per player in the same order
    maps: Dict[int, Dict[int, int]] = {}
    for fid in sorted(fid_set):
        try:
            j = api_get(f"fixtures/{fid}", {"include": "statistics;statistics.player"})
            fixture = j.get("data") or j  # tolerate either style
            maps[fid] = _fixture_player_sot_map(fixture)
        except Exception as e:
            print(f"[WARN] fixture {fid}: {e}")
            maps[fid] = {}

    # Top-level meta
    generated_at = dt.datetime.now(dt.timezone.utc).isoformat()
    league_id = int(Path(league_path).stem)
    league_name = src.get("league_name") or f"League {league_id}"
    out_players: List[dict] = []

    for p in players:
        fixture_ids = [int(x) for x in (p.get("fixture_ids") or [])]
        sot_series: List[int] = []
        for fid in fixture_ids:
            sot_series.append(int(maps.get(fid, {}).get(int(p.get("player_id")), 0)))
        out_players.append({
            "league_id": p.get("league_id") or league_id,
            "season_id": p.get("season_id"),  # copied from shots file if present
            "team_id": p.get("team_id"),
            "player_id": p.get("player_id"),
            "name": p.get("name"),
            "position_tag": p.get("position_tag"),
            "sot_last_n": sot_series,               # <-- key for SOT
            "minutes_last_n": p.get("minutes_last_n"),
            "fixture_ids": fixture_ids,
            "n": len(sot_series),
            "n_requested": p.get("n_requested") or 10,
            "min_minutes": p.get("min_minutes") or 45,
            "order": "latest_first",
        })

    payload = {
        "generated_at": generated_at,
        "league_id": league_id,
        "league_name": league_name,
        "count": len(out_players),
        "players": out_players,
    }
    return payload

def main():
    league_files = sorted(glob.glob(str(SHOTS_DIR / "*.json")), key=lambda p: int(Path(p).stem))
    if not league_files:
        print("No player_shots league files found. Run the Player shots workflow first.", file=sys.stderr)
        sys.exit(2)

    per_league_written = 0
    combined_players: List[dict] = []
    per_l_counts: Dict[int, int] = {}

    for lp in league_files:
        payload = build_for_league(Path(lp))
        if not payload:
            continue
        lid = int(payload["league_id"])
        _write_json(BY_LG_DIR / f"{lid}.json", payload)
        per_league_written += 1
        per_l_counts[lid] = payload["count"]
        combined_players.extend(payload["players"])

    # combined + summary
    now_iso = dt.datetime.now(dt.timezone.utc).isoformat()
    _write_json(OUT_DIR / "combined.json", {
        "generated_at": now_iso,
        "count": len(combined_players),
        "players": combined_players,
    })

    lines = [f"Time (UTC): {now_iso}", f"Players : {len(combined_players)}", ""]
    for lid in sorted(per_l_counts):
        lines.append(f"League {lid}: {per_l_counts[lid]} players")
    _write_text(OUT_DIR / "summary.txt", "\n".join(lines) + "\n")

    print(f"[OK] Wrote SOT for {per_league_written} leagues")

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
