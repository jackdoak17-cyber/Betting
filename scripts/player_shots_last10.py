#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Build last-10 *shots* sequences for all players appearing in the predicted XIs.

Reads:
  - data/predicted_xi/by_league/*.json   (players to track)

Fetches (Sportmonks):
  - fixtures/date/YYYY-MM-DD?include=participants;statistics;statistics.player;league;state
    (scans backwards a configurable number of days; cached on disk)

Writes:
  - data/player_stats/shots/by_league/{league_id}.json
  - data/player_stats/shots/combined.json
  - data/player_stats/shots/summary.txt
  - data/player_stats/shots/summary_verbose.txt

Env:
  SPORTMONKS_TOKEN   (required)
  SHOTS_BACK_DAYS    (optional, default 150)  # how many days back to scan
"""

import os
import json
import time
import datetime as dt
from pathlib import Path
from typing import Dict, List, Tuple, Optional

import requests

# ---------------- Config ----------------
API_BASE = "https://api.sportmonks.com/v3/football"
API_TOKEN = os.getenv("SPORTMONKS_TOKEN")
if not API_TOKEN:
    raise SystemExit("ERROR: SPORTMONKS_TOKEN is not set.")

BACK_DAYS = int(os.getenv("SHOTS_BACK_DAYS", "150"))  # look back ~5 months by default

# Throttling / retries
TIMEOUT = 25
RETRIES = 3
BACKOFF = 1.6
GLOBAL_MIN_DELAY = 0.18

# Caching
CACHE_DIR = Path(".cache_smonks")
CACHE_DIR.mkdir(parents=True, exist_ok=True)
CACHE_TTL_SECS = 24 * 3600  # 24h

# Output roots
OUT_ROOT = Path("data/player_stats/shots")
BY_LEAGUE_DIR = OUT_ROOT / "by_league"
for p in (OUT_ROOT, BY_LEAGUE_DIR):
    p.mkdir(parents=True, exist_ok=True)

LEAGUE_NAMES = {
    8:   "Premier League",
    9:   "Championship",
    384: "Serie A",
    387: "Serie B",
    82:  "Bundesliga",
    301: "Ligue 1",
    564: "La Liga",
    567: "La Liga 2",
    600: "Super Lig",
}

# ---------------- Cache helpers ----------------
import hashlib

_MEMO: Dict[str, dict] = {}
_last_call_ts = 0.0

def _cache_key(url: str, params: dict) -> str:
    s = url + "?" + "&".join(f"{k}={params[k]}" for k in sorted(params))
    return hashlib.sha256(s.encode("utf-8")).hexdigest()

def _cache_path(key: str) -> Path:
    return CACHE_DIR / f"{key}.json"

def _cache_load(key: str) -> Optional[dict]:
    p = _cache_path(key)
    if not p.is_file():
        return None
    try:
        if (time.time() - p.stat().st_mtime) > CACHE_TTL_SECS:
            return None
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None

def _cache_save(key: str, payload: dict) -> None:
    try:
        _cache_path(key).write_text(json.dumps(payload), encoding="utf-8")
    except Exception:
        pass

def api_get(path: str, params: Optional[dict] = None) -> dict:
    """GET with small memo + retry + light pacing + on-disk cache."""
    global _last_call_ts
    if params is None:
        params = {}
    params = {**params, "api_token": API_TOKEN}
    url = f"{API_BASE}/{path.lstrip('/')}"
    key = _cache_key(url, params)

    if key in _MEMO:
        return _MEMO[key]
    cached = _cache_load(key)
    if cached is not None:
        _MEMO[key] = cached
        return cached

    last_exc = None
    for attempt in range(1, RETRIES + 1):
        now = time.time()
        if now - _last_call_ts < GLOBAL_MIN_DELAY:
            time.sleep(GLOBAL_MIN_DELAY - (now - _last_call_ts))
        try:
            r = requests.get(url, params=params, timeout=TIMEOUT)
            _last_call_ts = time.time()
            if r.status_code == 429:
                sleep = min(60, (BACKOFF ** attempt) * 2.0)
                print(f"[429] {path} — sleeping {sleep:.1f}s")
                time.sleep(sleep)
                continue
            r.raise_for_status()
            j = r.json()
            _MEMO[key] = j
            _cache_save(key, j)
            return j
        except Exception as e:
            last_exc = e
            if attempt < RETRIES:
                sleep = (BACKOFF ** attempt)
                print(f"[RETRY] {path} (attempt {attempt}) sleeping {sleep:.1f}s")
                time.sleep(sleep)
            else:
                raise
    raise last_exc  # pragma: no cover

# ---------------- Utils ----------------
DATE_FMT = "%Y-%m-%d"

def today_utc_date() -> dt.date:
    return dt.datetime.now(dt.timezone.utc).date()

def daterange_str(start: dt.date, end_exclusive: dt.date) -> List[str]:
    d = start
    out = []
    while d < end_exclusive:
        out.append(d.strftime(DATE_FMT))
        d += dt.timedelta(days=1)
    return list(out)

def parse_iso_date(s: str) -> dt.datetime:
    # Sportmonks returns "YYYY-MM-DD HH:MM:SS" (no TZ) or ISO; we normalize
    try:
        if "T" in s:
            return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
        return dt.datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=dt.timezone.utc)
    except Exception:
        return dt.datetime.now(dt.timezone.utc)

def safe_int(x, default: int = 0) -> int:
    try:
        return int(x)
    except Exception:
        return default

# ---------------- Load players from predicted XIs ----------------
def load_tracked_players() -> Tuple[Dict[int, dict], Dict[int, set]]:
    """
    Returns:
      players: {player_id: {"name":..., "team_id":..., "team_name":..., "league_id":...}}
      league_to_players: {league_id: set(player_id)}
    """
    players: Dict[int, dict] = {}
    league_to_players: Dict[int, set] = {}

    base = Path("data/predicted_xi/by_league")
    if not base.is_dir():
        raise SystemExit("ERROR: data/predicted_xi/by_league not found. Run the lineups job first.")

    for f in sorted(base.glob("*.json")):
        try:
            obj = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        lid = int(obj.get("league_id") or f.stem)
        fixtures = obj.get("fixtures") or []
        for fx in fixtures:
            for side in ("home", "away"):
                team = fx.get(side) or {}
                tid = team.get("team_id")
                tname = (team.get("name") or "").strip()
                for p in (team.get("predicted_xi") or []):
                    pid = p.get("player_id")
                    if not pid:
                        continue
                    pid = int(pid)
                    if pid not in players:
                        players[pid] = {
                            "name": (p.get("name") or "").strip(),
                            "team_id": tid,
                            "team_name": tname,
                            "league_id": lid,
                        }
                    league_to_players.setdefault(lid, set()).add(pid)
    return players, league_to_players

# ---------------- Extract shots from a fixture blob ----------------
def _iter_stats_rows(stats_container) -> List[dict]:
    """
    Normalize different shapes: list, {"data":[...]}, dict-of-rows, etc.
    Return a flat list of stat rows.
    """
    rows = []
    if not stats_container:
        return rows
    if isinstance(stats_container, list):
        rows = stats_container
    elif isinstance(stats_container, dict):
        if "data" in stats_container and isinstance(stats_container["data"], list):
            rows = stats_container["data"]
        else:
            # dict keyed by something -> take dict values if they look like rows
            vals = list(stats_container.values())
            if vals and isinstance(vals[0], (dict, list)):
                # flatten one level if lists
                for v in vals:
                    if isinstance(v, list):
                        rows.extend(v)
                    elif isinstance(v, dict):
                        rows.append(v)
            else:
                rows = vals
    return [r for r in rows if isinstance(r, dict)]

def _extract_player_shots_from_row(row: dict) -> Optional[int]:
    """
    Try several common shapes to fetch 'shots total' for a player-stat row.
    """
    # If shots nested
    shots = row.get("shots")
    if isinstance(shots, dict):
        for k in ("total", "shots_total", "attempts", "all"):
            if k in shots and shots[k] is not None:
                return safe_int(shots[k])
        # sometimes it's {"on":X, "off":Y, "total":X+Y} -> already handled above
    # Flat keys
    for k in ("shots_total", "total_shots", "shotsTotal", "shotsAttempted", "attempts"):
        if k in row and row[k] is not None:
            return safe_int(row[k])

    # Some providers pack stats in "details" or "statistics"
    for container_key in ("details", "statistics"):
        cont = row.get(container_key)
        if isinstance(cont, dict):
            for k in ("shots_total", "total_shots", "shotsTotal", "attempts", "shots"):
                v = cont.get(k)
                if isinstance(v, dict):
                    if "total" in v:
                        return safe_int(v["total"])
                elif v is not None:
                    return safe_int(v)
    return None

def extract_player_shots_from_fixture(fx: dict) -> Dict[int, int]:
    """
    Return {player_id: shots_total} for this fixture (only if per-player stats exist).
    """
    out: Dict[int, int] = {}
    stats = fx.get("statistics") or fx.get("stats") or {}
    rows = _iter_stats_rows(stats)
    if not rows:
        return out
    for row in rows:
        pid = row.get("player_id") or (row.get("player") or {}).get("id")
        if not pid:
            continue
        shots = _extract_player_shots_from_row(row)
        if shots is None:
            continue
        out[int(pid)] = safe_int(shots, 0)
    return out

# ---------------- Fetch fixtures by date (with stats) ----------------
def fetch_fixtures_for_date(date_str: str) -> List[dict]:
    """
    fixtures/date/YYYY-MM-DD with statistics (player-level) and participants.
    """
    j = api_get(f"fixtures/date/{date_str}", {
        "include": "participants;statistics;statistics.player;league;state",
        "order": "asc",
        "page": 1
    })
    data = j.get("data", []) or []
    meta = j.get("meta") or {}
    last_page = int(meta.get("last_page", 1) or 1)
    for p in range(2, last_page + 1):
        jp = api_get(f"fixtures/date/{date_str}", {
            "include": "participants;statistics;statistics.player;league;state",
            "order": "asc",
            "page": p
        })
        data.extend(jp.get("data", []) or [])
    return data

# ---------------- Main ----------------
def main():
    players, league_to_players = load_tracked_players()
    if not players:
        raise SystemExit("No players found in predicted XIs.")

    league_ids = sorted(league_to_players.keys())
    print(f"Leagues (from predicted_xi): {league_ids}")
    print(f"Tracked players: {len(players)}")

    # Accumulate per-player shot logs: {player_id: [(ts, shots), ...]}
    per_player: Dict[int, List[Tuple[int, int]]] = {pid: [] for pid in players}

    end = today_utc_date()
    start = end - dt.timedelta(days=BACK_DAYS)
    dates = daterange_str(start, end)  # [start, end)

    # Walk date-by-date once; filter to our leagues
    for ds in dates:
        try:
            day = fetch_fixtures_for_date(ds)
        except requests.HTTPError as e:
            print(f"[WARN] {ds}: {e}")
            continue
        # Filter to leagues we care about only
        day = [fx for fx in day if int(fx.get("league_id") or 0) in league_ids]
        if not day:
            continue
        for fx in day:
            # Only finished or with stats present (some future games may leak in)
            if not fx.get("statistics"):
                continue
            ts = fx.get("starting_at") or fx.get("time") or fx.get("startingAt") or ""
            t = int(parse_iso_date(str(ts)).timestamp())
            pshots = extract_player_shots_from_fixture(fx)
            if not pshots:
                continue
            for pid, shots in pshots.items():
                if pid in per_player:
                    per_player[pid].append((t, shots))

    # Build last-10 sequences
    combined_players = []
    by_league_payload: Dict[int, List[dict]] = {lid: [] for lid in league_ids}

    for pid, log in per_player.items():
        if not log:
            seq = []
        else:
            log.sort(key=lambda x: x[0])          # ascending by time
            seq = [s for _, s in log][-10:]       # last 10 values

        meta = players[pid]
        row = {
            "player_id": pid,
            "name": meta["name"],
            "team_id": meta["team_id"],
            "team_name": meta["team_name"],
            "league_id": meta["league_id"],
            "shots_last10": seq,
        }
        combined_players.append(row)
        by_league_payload[meta["league_id"]].append(row)

    # Write by-league JSON
    now_iso = dt.datetime.now(dt.timezone.utc).isoformat()
    for lid in league_ids:
        payload = {
            "utc_time": now_iso,
            "league_id": lid,
            "league_name": LEAGUE_NAMES.get(lid, str(lid)),
            "players": sorted(by_league_payload[lid], key=lambda r: (r["team_name"], r["name"])),
        }
        (BY_LEAGUE_DIR / f"{lid}.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    # Combined
    (OUT_ROOT / "combined.json").write_text(json.dumps({
        "utc_time": now_iso,
        "players": combined_players,
    }, ensure_ascii=False), encoding="utf-8")

    # Summary (compact)
    summary = []
    summary.append(f"Time (UTC): {now_iso}")
    summary.append(f"Players    : {len(combined_players)}")
    summary.append("")
    summary.append("Per league player counts:")
    for lid in league_ids:
        summary.append(f"  - {lid} ({LEAGUE_NAMES.get(lid, lid)}): {len(by_league_payload[lid])}")
    (OUT_ROOT / "summary.txt").write_text("\n".join(summary) + "\n", encoding="utf-8")

    # Summary verbose (validation lines in the requested format)
    lines = []
    lines.append(f"Time (UTC): {now_iso}")
    lines.append("")
    for lid in league_ids:
        lines.append(f"===== {LEAGUE_NAMES.get(lid, str(lid))} (LID {lid}) =====")
        for row in sorted(by_league_payload[lid], key=lambda r: (r["team_name"], r["name"])):
            seq = row["shots_last10"]
            seq_str = ",".join(str(x) for x in seq) if seq else "(no data)"
            lines.append(f"{row['team_name']} — {row['name']}: {seq_str}")
        lines.append("")  # blank between leagues
    (OUT_ROOT / "summary_verbose.txt").write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")

    print("\nDone.")
    print(f"Wrote {len(league_ids)} league files to {BY_LEAGUE_DIR}/")
    print("Also wrote:")
    print("  • data/player_stats/shots/combined.json")
    print("  • data/player_stats/shots/summary.txt")
    print("  • data/player_stats/shots/summary_verbose.txt")

if __name__ == "__main__":
    main()
