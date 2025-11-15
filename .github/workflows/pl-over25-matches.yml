#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Over 2.5 goals — upcoming fixtures ranked by combined % (avg of each team's last-N O2.5%)

What it does
------------
- Reads upcoming fixtures for a league from local files written by your fixtures job:
    data/fixtures/by_league/{LEAGUE_ID}.json
  (falls back to data/fixtures/{LEAGUE_ID}.json if needed)
- For each team in those fixtures, fetches their last-N *league* matches via Sportmonks v3
  and computes:
    * O2.5 hits (total goals >= 3)
    * O2.5 percentage
    * average total goals
- Ranks fixtures by the average of the two teams' O2.5 percentages (combined %)
- Writes a social-ready post to posts/over25_matches_L{LEAGUE_ID}.md

Env
---
SPORTMONKS_TOKEN          required (or SPORTMONKS_API_TOKEN / SM_TOKEN)
LEAGUE_ID                 int, default "8"
LAST_N                    int, default "10"   (how many recent league matches per team)
MIN_GAMES                 int, default "6"    (each team must have at least this many)
MAX_ROWS                  int, default "20"   (max fixtures in the post)
LOOKBACK_DAYS             int, default "140"  (initial window; script walks back in 100d steps)
OUTPUT_PATH               path, default "posts/over25_matches_L{LEAGUE_ID}.md"

Notes
-----
- Uses /v3/football/fixtures/between/{start}/{end}/{team_id} with filters=fixtureLeagues:{LEAGUE_ID}
- Includes "scores;state" and infers FT totals robustly from the "scores" array
- Finished-only fixtures (state_id == 5) are considered
"""

import os
import re
import json
import time
import datetime as dt
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
import requests

# ----------------- Config / Env -----------------
API_BASE = "https://api.sportmonks.com/v3/football"
TOKEN = (
    os.getenv("SPORTMONKS_TOKEN")
    or os.getenv("SPORTMONKS_API_TOKEN")
    or os.getenv("SM_TOKEN")
)
if not TOKEN:
    raise SystemExit("ERROR: SPORTMONKS_TOKEN / SPORTMONKS_API_TOKEN not set.")

LEAGUE_ID   = int(os.getenv("LEAGUE_ID", "8"))
LAST_N      = int(os.getenv("LAST_N", "10"))
MIN_GAMES   = int(os.getenv("MIN_GAMES", "6"))
MAX_ROWS    = int(os.getenv("MAX_ROWS", "20"))
LOOKBACK_D  = int(os.getenv("LOOKBACK_DAYS", "140"))
OUTPUT_PATH = os.getenv("OUTPUT_PATH", f"posts/over25_matches_L{LEAGUE_ID}.md")

TIMEOUT = 25
RETRIES = 3
BACKOFF = 1.6
PACE_DELAY = 0.18  # gentle global pacing
_last_call = 0.0

ROOT = Path(".")
FIX_FILE     = ROOT / "data" / "fixtures" / "by_league" / f"{LEAGUE_ID}.json"
FIX_FILE_ALT = ROOT / "data" / "fixtures" / f"{LEAGUE_ID}.json"

# Optional pretty map for common long names
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

# ----------------- HTTP helpers -----------------
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

# ----------------- Time helpers -----------------
def today_utc_date() -> dt.date:
    return dt.datetime.now(dt.timezone.utc).date()

def dstr(d: dt.date) -> str:
    return d.strftime("%Y-%m-%d")

def parse_ts_from_str(s: str) -> Optional[int]:
    if not s:
        return None
    try:
        s2 = s.replace("T", " ").replace("Z", "")
        return int(dt.datetime.strptime(s2[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=dt.timezone.utc).timestamp())
    except Exception:
        return None

# ----------------- Local fixtures -----------------
def _load_json(p: Path) -> Any:
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None

def _participants_to_list(parts_any: Any) -> List[dict]:
    if isinstance(parts_any, list):
        return [p for p in parts_any if isinstance(p, dict)]
    if isinstance(parts_any, dict):
        cand = []
        for k in ("home","local","localteam","home_team","away","visitor","visitorteam","away_team"):
            v = parts_any.get(k)
            if isinstance(v, dict):
                cand.append(v)
        return cand
    return []

def upcoming_fixtures_from_local() -> List[dict]:
    blob = _load_json(FIX_FILE) or _load_json(FIX_FILE_ALT) or {}
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

        parts = _participants_to_list(fx.get("participants"))
        teams: List[Tuple[int,str]] = []
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

    up.sort(key=lambda r: (r["starting_at_ts"] or 0, r.get("fixture_id") or 0))
    return up

# ----------------- Goals parsing -----------------
def total_goals_from_scores(fx: dict) -> Optional[int]:
    """
    Read final totals from fx['scores'] robustly:
    - For each participant_id, take the max score seen across entries.
    - Sum the two largest values.
    """
    scores = fx.get("scores") or []
    if not isinstance(scores, list) or not scores:
        return None
    per: Dict[int, int] = {}
    for s in scores:
        try:
            pid = int(s.get("participant_id") or s.get("team_id") or 0)
        except Exception:
            continue
        if pid <= 0:
            continue
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
    top2 = sorted(per.values(), reverse=True)[:2]
    return sum(top2) if len(top2) == 2 else None

# ----------------- Team last-N fetch -----------------
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
    Returns { n, hits, pct, avg_goals } over latest LAST_N finished league matches.
    """
    start_anchor = today_utc_date() - dt.timedelta(days=LOOKBACK_D)
    end = today_utc_date()
    totals: List[int] = []

    def have_enough() -> bool:
        return len(totals) >= LAST_N

    while end >= dt.date(2000, 1, 1) and not have_enough():
        win_start = max(dt.date(2000, 1, 1), end - dt.timedelta(days=99))
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
                    # string fallback like "2-1"
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

# ----------------- Main -----------------
def main():
    upcoming = upcoming_fixtures_from_local()

    outp = Path(OUTPUT_PATH)
    outp.parent.mkdir(parents=True, exist_ok=True)

    if not upcoming:
        outp.write_text("No upcoming fixtures found for this league.\n", encoding="utf-8")
        print(f"Wrote {outp}")
        return

    team_cache: Dict[int, Dict[str, Any]] = {}
    def team_stats(tid: int) -> Dict[str, Any]:
        if tid not in team_cache:
            team_cache[tid] = collect_team_over25(LEAGUE_ID, tid)
        return team_cache[tid]

    rows: List[dict] = []
    for fx in upcoming:
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
        outp.write_text("No eligible fixtures (insufficient last-N data).\n", encoding="utf-8")
        print(f"Wrote {outp}")
        return

    rows.sort(key=lambda r: (-r["combined"], -r["avg_mix"], r["ts"]))

    def fmt_kick(ts: int) -> str:
        # Keep UTC time for reproducibility in CI
        return dt.datetime.utcfromtimestamp(ts).strftime("%a %d %b, %H:%M UTC")

    header = [
        f"Upcoming fixtures ranked by **Over 2.5 Goals** (combined % = avg of each team's last {LAST_N} league games).",
        f"Minimum data gate: each team must have ≥{MIN_GAMES} games collected.",
        "",
        "Like & follow if this helps your picks.",
        "",
        "**Top candidates**",
        "",
    ]

    lines: List[str] = []
    lines.extend(header)

    for r in rows[:MAX_ROWS]:
        hn, an = pretty_team(r["home_name"]), pretty_team(r["away_name"])
        lines.append(
            f"• {hn} vs {an} — {fmt_kick(r['ts'])} — "
            f"Combined **{r['combined']:.0f}%** "
            f"(H {r['home_pct']:.0f}% [{r['home_hits']}/{r['home_n']}], "
            f"A {r['away_pct']:.0f}% [{r['away_hits']}/{r['away_n']}]) "
            f"Avg goals: H {r['home_avg']:.2f} | A {r['away_avg']:.2f}"
        )

    lines.append("")
    lines.append("_Method: team last-N league matches, finished only; totals inferred from fixture scores._")

    outp.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    print(f"Wrote {outp}")

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
