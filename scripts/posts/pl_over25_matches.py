#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Over 2.5 goals — upcoming fixtures ranked by combined % (avg of each team's last-N O2.5%)

Inputs:
  - Local fixtures for the league:
      data/fixtures/by_league/{LEAGUE_ID}.json
    (written by your fetch_fixtures.py)

Env:
  SPORTMONKS_TOKEN    (required)
  LEAGUE_ID           (int, default "8")
  LAST_N              (int, default "10")   # last N league games per team
  MIN_GAMES           (int, default "6")    # require at least this many games collected
  MAX_ROWS            (int, default "20")   # how many fixtures to print
  OUTPUT_PATH         (default "posts/over25_matches_L{LEAGUE_ID}.md")
  LOOKBACK_DAYS       (int, default "140")  # window to start with; script walks back in 100-day steps

Notes:
- Uses /v3/football/fixtures/between/{start}/{end}/{team_id}?filters=fixtureLeagues:{LEAGUE_ID}
- Includes "scores;state" and computes total goals from fixture.scores (robust parsing).
- Finished-only fixtures (state_id == 5).
"""

import os
import re
import json
import time
import math
import datetime as dt
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
import requests

API_BASE = "https://api.sportmonks.com/v3/football"
TOKEN = (
    os.getenv("SPORTMONKS_TOKEN")
    or os.getenv("SPORTMONKS_API_TOKEN")
    or os.getenv("SM_TOKEN")
)
if not TOKEN:
    raise SystemExit("ERROR: SPORTMONKS_TOKEN / SPORTMONKS_API_TOKEN not set.")

LEAGUE_ID  = int(os.getenv("LEAGUE_ID", "8"))
LAST_N     = int(os.getenv("LAST_N", "10"))
MIN_GAMES  = int(os.getenv("MIN_GAMES", "6"))
MAX_ROWS   = int(os.getenv("MAX_ROWS", "20"))
LOOKBACK_D = int(os.getenv("LOOKBACK_DAYS", "140"))
OUTPUT_PATH = os.getenv("OUTPUT_PATH", f"posts/over25_matches_L{LEAGUE_ID}.md")

TIMEOUT = 25
RETRIES = 3
BACKOFF = 1.6
PACE_DELAY = 0.18  # gentle global pacing
_last_call = 0.0

ROOT = Path(".")
FIX_FILE = ROOT / "data" / "fixtures" / "by_league" / f"{LEAGUE_ID}.json"

# -------- Pretty team names (extend if you like) --------
PRETTY_MAP = {
    "Manchester City": "Man City",
    "Manchester United": "Man Utd",
    "Newcastle United": "Newcastle",
    "Nottingham Forest": "Nottm Forest",
    "Brighton & Hove Albion": "Brighton",
    "Tottenham Hotspur": "Spurs",
    "Wolverhampton Wanderers": "Wolves",
    "West Ham United": "West Ham",
    "AFC Bournemouth": "Bournemouth",
    "Sheffield United": "Sheff Utd",
}

def pretty_team(name: Optional[str]) -> str:
    if not name:
        return "TBC"
    return PRETTY_MAP.get(name, name)

# ---------------- HTTP helpers ----------------
def _pace():
    global _last_call
    now = time.time()
    if now - _last_call < PACE_DELAY:
        time.sleep(PACE_DELAY - (now - _last_call))
    _last_call = time.time()

def api_get(path: str, params: Optional[dict] = None) -> dict:
    if params is None:
        params = {}
    params = {**params, "api_token": TOKEN}
    url = f"{API_BASE}/{path.lstrip('/')}"
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
            return r.json()
        except Exception as e:
            last_exc = e
            if i < RETRIES:
                sleep = BACKOFF ** i
                print(f"[RETRY] {path} (attempt {i}) sleeping {sleep:.1f}s")
                time.sleep(sleep)
            else:
                raise
    raise last_exc

# ---------------- time helpers ----------------
def today_utc_date() -> dt.date:
    return dt.datetime.now(dt.timezone.utc).date()

def dstr(d: dt.date) -> str:
    return d.strftime("%Y-%m-%d")

def parse_ts_from_str(s: str) -> Optional[int]:
    # common shapes: "YYYY-MM-DD HH:MM:SS", "YYYY-MM-DDTHH:MM:SSZ"
    if not s:
        return None
    try:
        s2 = s.replace("T", " ").replace("Z", "")
        return int(dt.datetime.strptime(s2[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=dt.timezone.utc).timestamp())
    except Exception:
        return None

# ---------------- fixtures (local upcoming) ----------------
def _load_json(p: Path) -> Any:
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None

def upcoming_fixtures_from_local() -> List[dict]:
    blob = _load_json(FIX_FILE) or {}
    fixtures = blob.get("fixtures") or []
    now_ts = int(time.time())
    up: List[dict] = []
    for fx in fixtures:
        st_ts = fx.get("starting_at_timestamp")
        if st_ts is None:
            st_ts = parse_ts_from_str(fx.get("starting_at") or "")
        try:
            st_ts = int(st_ts) if st_ts is not None else None
        except Exception:
            st_ts = None
        if st_ts is None or st_ts <= now_ts:
            continue
        parts = fx.get("participants") or []
        # normalize to [(id,name), ...]
        teams = []
        for p in parts:
            tid = p.get("id") or p.get("team_id")
            nm = p.get("name") or ""
            try:
                tid = int(tid)
            except Exception:
                continue
            teams.append((tid, nm))
        if len(teams) >= 2:
            up.append({
                "fixture_id": fx.get("id"),
                "starting_at_ts": st_ts,
                "home_id": teams[0][0],
                "home_name": teams[0][1],
                "away_id": teams[1][0],
                "away_name": teams[1][1],
            })
    # sort by kickoff
    up.sort(key=lambda r: (r["starting_at_ts"] or 0, r.get("fixture_id") or 0))
    return up

# ---------------- parse goals from a fixture ----------------
def total_goals_from_scores(fx: dict) -> Optional[int]:
    """
    Robustly read FT score from fx['scores'].
    Strategy: take the MAX score seen per participant_id among all entries.
    This usually captures the final (ft) score even if multiple score snapshots exist.
    """
    scores = fx.get("scores") or []
    if not isinstance(scores, list) or not scores:
        return None
    per = {}
    for s in scores:
        try:
            pid = int(s.get("participant_id") or s.get("team_id") or 0)
        except Exception:
            continue
        if pid <= 0:
            continue
        # Value candidates
        val = s.get("score")
        if val is None:
            vobj = s.get("data") or s.get("value") or {}
            if isinstance(vobj, dict):
                val = vobj.get("score") or vobj.get("value")
        try:
            val = int(float(val))
        except Exception:
            continue
        per[pid] = max(per.get(pid, 0), val)
    if len(per) < 2:
        return None
    # sum two largest scores (in case more than 2 appeared)
    top2 = sorted(per.values(), reverse=True)[:2]
    return sum(top2) if len(top2) == 2 else None

# ---------------- pull last-N for a team ----------------
def fetch_team_fixtures_window(team_id: int, start: dt.date, end: dt.date, league_id: int, page: int = 1) -> dict:
    path = f"fixtures/between/{dstr(start)}/{dstr(end)}/{team_id}"
    params = {
        "include": "scores;state",
        "filters": f"fixtureLeagues:{league_id}",
        "order": "desc",
        "per_page": 50,
        "page": page,
    }
    return api_get(path, params)

def collect_team_over25(league_id: int, team_id: int) -> Dict[str, Any]:
    """
    Returns { n, hits, pct, avg_goals }
      - n: number of finished league matches collected (latest-first)
      - hits: count of matches with total_goals >= 3
      - pct: hits/n * 100
      - avg_goals: mean total goals over collected matches
    """
    start_anchor = today_utc_date() - dt.timedelta(days=LOOKBACK_D)
    end = today_utc_date()
    totals: List[int] = []

    def have_enough() -> bool:
        return len(totals) >= LAST_N

    while end >= dt.date(2000, 1, 1) and not have_enough():
        win_start = max(dt.date(2000, 1, 1), end - dt.timedelta(days=99))
        # ensure our first window starts earlier if LOOKBACK_DAYS small
        if win_start > start_anchor:
            win_start = start_anchor
        page = 1
        has_more = True
        while has_more and not have_enough():
            j = fetch_team_fixtures_window(team_id, win_start, end, league_id, page=page)
            data = j.get("data") or []
            meta = j.get("meta") or {}
            per_page = 50
            has_more = bool(meta.get("has_more")) or (len(data) == per_page)
            page += 1

            for fx in data:
                # finished only
                try:
                    if int(fx.get("state_id") or 0) != 5:
                        continue
                except Exception:
                    continue

                tg = total_goals_from_scores(fx)
                if tg is None:
                    # fallback: sometimes top-level might have quick values
                    # try 'result' like "2-1"
                    res = fx.get("result") or fx.get("name") or ""
                    m = re.search(r"(\d+)\s*[-:]\s*(\d+)$", str(res).strip())
                    if m:
                        tg = int(m.group(1)) + int(m.group(2))
                if tg is None:
                    continue

                totals.append(int(tg))
                if have_enough():
                    break

        end = win_start - dt.timedelta(days=1)

    if not totals:
        return {"n": 0, "hits": 0, "pct": 0.0, "avg_goals": 0.0}

    totals = totals[:LAST_N]
    n = len(totals)
    hits = sum(1 for g in totals if g >= 3)
    pct = (hits / n) * 100.0
    avg_goals = sum(totals) / n
    return {"n": n, "hits": hits, "pct": pct, "avg_goals": avg_goals}

# ---------------- main ----------------
def main():
    up = upcoming_fixtures_from_local()
    if not up:
        # write a small "no fixtures" note so workflow still commits cleanly
        outp = Path(OUTPUT_PATH)
        outp.parent.mkdir(parents=True, exist_ok=True)
        outp.write_text("No upcoming fixtures found for this league.\n", encoding="utf-8")
        print(f"Wrote {outp}")
        return

    team_cache: Dict[int, Dict[str, Any]] = {}

    def team_stats(tid: int) -> Dict[str, Any]:
        if tid not in team_cache:
            team_cache[tid] = collect_team_over25(LEAGUE_ID, tid)
        return team_cache[tid]

    rows: List[dict] = []
    for fx in up:
        h = team_stats(fx["home_id"])
        a = team_stats(fx["away_id"])
        if h["n"] < MIN_GAMES or a["n"] < MIN_GAMES:
            continue
        combined = (h["pct"] + a["pct"]) / 2.0
        avg_mix = (h["avg_goals"] + a["avg_goals"]) / 2.0
        rows.append({
            "fixture_id": fx["fixture_id"],
            "ts": fx["starting_at_ts"],
            "home_name": fx["home_name"], "away_name": fx["away_name"],
            "home_pct": h["pct"], "away_pct": a["pct"],
            "home_hits": h["hits"], "home_n": h["n"], "home_avg": h["avg_goals"],
            "away_hits": a["hits"], "away_n": a["n"], "away_avg": a["avg_goals"],
            "combined": combined,
            "avg_mix": avg_mix,
        })

    if not rows:
        outp = Path(OUTPUT_PATH)
        outp.parent.mkdir(parents=True, exist_ok=True)
        outp.write_text("No eligible fixtures (insufficient last-N data).\n", encoding="utf-8")
        print(f"Wrote {outp}")
        return

    rows.sort(key=lambda r: (-r["combined"], -r["avg_mix"], r["ts"]))

    # ----- format post -----
    def fmt_kick(ts: int) -> str:
        # Keep UTC clock time for consistency in CI
        return dt.datetime.utcfromtimestamp(ts).strftime("%a %d %b, %H:%M UTC")

    header = [
        f"Upcoming fixtures ranked by **Over 2.5 Goals** (combined % = avg of each team's last {LAST_N} league games).",
        f"Minimum data gate: each team must have ≥{MIN_GAMES} games collected.",
        "",
        "Like & follow if this helps your picks.",
        "",
    ]

    lines: List[str] = []
    lines.extend(header)
    lines.append("**Top candidates**")
    lines.append("")
    take = rows[:MAX_ROWS]
    for r in take:
        hn, an = pretty_team(r["home_name"]), pretty_team(r["away_name"])
        lines.append(
            f"• {hn} vs {an} — {fmt_kick(r['ts'])} — "
            f"Combined **{r['combined']:.0f}%** "
            f"(H {r['home_pct']:.0f}% [{r['home_hits']}/{r['home_n']}], "
            f"A {r['away_pct']:.0f}% [{r['away_hits']}/{r['away_n']}]) "
            f"Avg goals: H {r['home_avg']:.2f} | A {r['away_avg']:.2f}"
        )

    lines.append("")
    lines.append("_Method: team last-N league matches, finished only; totals from fixture scores._")

    outp = Path(OUTPUT_PATH)
    outp.parent.mkdir(parents=True, exist_ok=True)
    outp.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    print(f"Wrote {outp}")

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
