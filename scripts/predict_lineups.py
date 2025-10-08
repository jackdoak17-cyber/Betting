#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Predict lineups for ALL leagues/fixtures previously fetched, then write:
  - data/predicted_xi/by_league/{league_id}.json
  - data/predicted_xi/combined.json            (optional convenience)
  - data/predicted_xi/summary.txt              (counts)
  - data/predicted_xi/summary_verbose.txt      (your human-check file)

Rules:
- Assume NO official matchday XI; copy starters from each team’s LAST league
  match with a recorded XI (bounded 45 days back).
- If a player is sidelined (injury/suspension/etc) we *keep them* in the XI
  but mark `status="OUT: <reason>"` so you can spot them in the summary file.

Requires env:
  SPORTMONKS_TOKEN
"""

import os
import json
import time
import datetime as dt
from typing import Dict, List, Optional, Tuple

import requests

# ---------------- Config ----------------
API_BASE = "https://api.sportmonks.com/v3/football"
API_TOKEN = os.getenv("SPORTMONKS_TOKEN")
if not API_TOKEN:
    raise SystemExit("ERROR: SPORTMONKS_TOKEN is not set.")

# Light throttling / retries
TIMEOUT = 25
RETRIES = 3
BACKOFF = 1.6
GLOBAL_MIN_DELAY = 0.18      # min spacing between GETs
BATCH_SIZE = 3               # fixtures per tiny batch (smooth usage)
SLEEP_BETWEEN_BATCHES = 1.2  # pause between batches

# Limits
LINEUP_TYPE_STARTER = 11
MAX_FALLBACK_DAYS = 45

# For logs only
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

# ---------------- HTTP helper with memo ----------------
_MEMO: Dict[str, dict] = {}
_last_call_ts = 0.0

def _key(url: str, params: dict) -> str:
    return url + "?" + "&".join(f"{k}={params[k]}" for k in sorted(params))

def _pace():
    global _last_call_ts
    now = time.time()
    if now - _last_call_ts < GLOBAL_MIN_DELAY:
        time.sleep(GLOBAL_MIN_DELAY - (now - _last_call_ts))
    _last_call_ts = time.time()

def api_get(path: str, params: Optional[dict] = None) -> dict:
    if params is None:
        params = {}
    params = {**params, "api_token": API_TOKEN}
    url = f"{API_BASE}/{path.lstrip('/')}"
    k = _key(url, params)
    if k in _MEMO:
        return _MEMO[k]

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

# ---------------- Utilities ----------------
def today_utc() -> dt.date:
    return dt.datetime.now(dt.timezone.utc).date()

def dstr(d: dt.date) -> str:
    return d.strftime("%Y-%m-%d")

def pos_id_to_label(position_id: Optional[int]) -> str:
    return {24: "GK", 25: "DEF", 26: "MID", 27: "FWD"}.get(position_id or 0, "?")

def pick_home_away(participants: List[dict]):
    home = next((p for p in participants if (p.get("meta") or {}).get("location") == "home"), None)
    away = next((p for p in participants if (p.get("meta") or {}).get("location") == "away"), None)
    return home, away

def ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)

# ---------------- Load fixtures from repo ----------------
def _load_json(path: str) -> Optional[dict]:
    if not os.path.isfile(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        try:
            return json.load(f)
        except Exception:
            return None

def load_all_fixtures() -> List[dict]:
    """
    Read fixtures from data/fixtures/by_league/*.json (preferred),
    else data/fixtures/*.json. Return flat list with participants present.
    """
    base = "data/fixtures"
    by_league = os.path.join(base, "by_league")
    fixtures: List[dict] = []

    def take(path: str):
        blob = _load_json(path)
        if not blob:
            return
        if isinstance(blob, dict) and "fixtures" in blob:
            fixtures.extend([fx for fx in blob["fixtures"] if fx and fx.get("participants")])
        elif isinstance(blob, list):
            fixtures.extend([fx for fx in blob if fx and fx.get("participants")])

    if os.path.isdir(by_league):
        for name in os.listdir(by_league):
            if name.endswith(".json"):
                take(os.path.join(by_league, name))
    else:
        if os.path.isdir(base):
            for name in os.listdir(base):
                if name.endswith(".json") and name not in ("latest.json",):
                    take(os.path.join(base, name))

    return [fx for fx in fixtures if fx.get("id")]

# ---------------- Sportmonks helpers ----------------
def fixtures_on_date(date_s: str, leagues: Optional[set] = None) -> List[dict]:
    j = api_get(f"fixtures/date/{date_s}", {
        "include": "participants;lineups;lineups.player;league;state"
    })
    data = j.get("data", []) or []
    if leagues:
        data = [d for d in data if d.get("league_id") in leagues]
    return data

def last_league_fixture_with_starters(team_id: int, league_id: int) -> Optional[dict]:
    # fast path via team.latest (+lineups)
    try:
        j = api_get(f"teams/{team_id}", {
            "include": "latest.league;latest.lineups;latest.lineups.player"
        })
        latest = j.get("data", {}).get("latest")
        lst = latest if isinstance(latest, list) else ([latest] if latest else [])
        lst = [fx for fx in lst if (fx or {}).get("league_id") == league_id]
        lst.sort(key=lambda x: x.get("starting_at") or "", reverse=True)
        for fx in lst:
            starters = [l for l in (fx.get("lineups") or [])
                        if l.get("type_id") == LINEUP_TYPE_STARTER and l.get("team_id") == team_id]
            if starters:
                return fx
    except Exception:
        pass

    # bounded fallback scan (≤45 days)
    start = today_utc()
    for back in range(1, MAX_FALLBACK_DAYS + 1):
        day = dstr(start - dt.timedelta(days=back))
        try:
            day_fixtures = fixtures_on_date(day, leagues={league_id})
        except Exception:
            continue
        for fx in day_fixtures:
            if any(p.get("id") == team_id for p in (fx.get("participants") or [])):
                starters = [l for l in (fx.get("lineups") or [])
                            if l.get("type_id") == LINEUP_TYPE_STARTER and l.get("team_id") == team_id]
                if starters:
                    return fx
    return None

def extract_starters(fx: dict, team_id: int) -> List[dict]:
    li = fx.get("lineups") or []
    starters = [l for l in li if l.get("type_id") == LINEUP_TYPE_STARTER and l.get("team_id") == team_id]
    starters.sort(key=lambda x: x.get("formation_position") or 9999)
    return starters[:11]

def sidelined_map(team_id: int) -> Dict[int, str]:
    """
    player_id -> reason string; best-effort (returns {} on any issue).
    """
    try:
        j = api_get(f"teams/{team_id}", {"include": "sidelined.player;sidelined.type"})
        data = j.get("data", {}) or {}
        rows = data.get("sidelined") or []
        out: Dict[int, str] = {}
        for r in rows:
            pid = r.get("player_id") or (r.get("player") or {}).get("id")
            if not pid:
                continue
            t = (r.get("type") or {}).get("name") or (r.get("type") or {}).get("code") or "sidelined"
            try:
                out[int(pid)] = str(t)
            except Exception:
                continue
        return out
    except Exception:
        return {}

# ---------------- Main ----------------
def main():
    fixtures = load_all_fixtures()
    if not fixtures:
        print("No fixtures found. Did the fetch workflow run?")
        return

    fixtures.sort(key=lambda x: (x.get("league_id"), x.get("starting_at") or "", x.get("id")))
    out_root = "data/predicted_xi"
    by_league_root = os.path.join(out_root, "by_league")
    ensure_dir(by_league_root)

    processed = 0
    by_league_counts: Dict[int, int] = {}
    league_payloads: Dict[int, List[dict]] = {}

    # team-level caches so we only call once per team per run
    xi_cache: Dict[Tuple[int, int], List[dict]] = {}       # (team_id, league_id) -> starters rows
    sidelined_cache: Dict[int, Dict[int, str]] = {}        # team_id -> {player_id: reason}

    for idx, fx in enumerate(fixtures, 1):
        if (idx - 1) % BATCH_SIZE == 0:
            print(f"\n-- Batch {((idx - 1)//BATCH_SIZE) + 1} starting (item {idx}/{len(fixtures)}) --")

        lid = int(fx.get("league_id"))
        parts = fx.get("participants") or []
        home, away = pick_home_away(parts)
        if not (home and away):
            continue
        hid, aid = int(home["id"]), int(away["id"])
        hname, aname = (home.get("name") or "Home").strip(), (away.get("name") or "Away").strip()
        fid = int(fx["id"])
        start_at = fx.get("starting_at") or ""

        # predict XI per team with caches
        key_h, key_a = (hid, lid), (aid, lid)
        if key_h not in xi_cache:
            last_h = last_league_fixture_with_starters(hid, lid)
            xi_cache[key_h] = extract_starters(last_h, hid) if last_h else []
        if key_a not in xi_cache:
            last_a = last_league_fixture_with_starters(aid, lid)
            xi_cache[key_a] = extract_starters(last_a, aid) if last_a else []

        if hid not in sidelined_cache:
            sidelined_cache[hid] = sidelined_map(hid)
        if aid not in sidelined_cache:
            sidelined_cache[aid] = sidelined_map(aid)

        def pack(lp: dict, sidemap: Dict[int, str]) -> dict:
            pid = int(lp.get("player_id"))
            status = "OK"
            if pid in sidemap:
                status = f"OUT: {sidemap[pid]}"
            return {
                "player_id": pid,
                "name": (lp.get("player_name") or "").strip(),
                "jersey": lp.get("jersey_number"),
                "position_id": lp.get("position_id"),
                "position_label": pos_id_to_label(lp.get("position_id")),
                "formation_position": lp.get("formation_position"),
                "status": status,
            }

        home_xi = [pack(p, sidelined_cache[hid]) for p in xi_cache[key_h]]
        away_xi = [pack(p, sidelined_cache[aid]) for p in xi_cache[key_a]]

        item = {
            "fixture_id": fid,
            "starting_at": start_at,
            "home": {"team_id": hid, "name": hname, "predicted_xi": home_xi},
            "away": {"team_id": aid, "name": aname, "predicted_xi": away_xi},
            "assumption": "Copied starters from previous league match; OUT tags from team sidelined list.",
        }
        league_payloads.setdefault(lid, []).append(item)

        by_league_counts[lid] = by_league_counts.get(lid, 0) + 1
        processed += 1

        # small pause each batch
        if idx % BATCH_SIZE == 0:
            time.sleep(SLEEP_BETWEEN_BATCHES)

    # Write PER-LEAGUE JSON
    ensure_dir(by_league_root)
    now_iso = dt.datetime.now(dt.timezone.utc).isoformat()
    for lid, rows in league_payloads.items():
        rows.sort(key=lambda r: (r.get("starting_at") or "", r["fixture_id"]))
        payload = {
            "utc_time": now_iso,
            "league_id": lid,
            "league_name": LEAGUE_NAMES.get(lid, str(lid)),
            "fixtures": rows,
        }
        with open(os.path.join(by_league_root, f"{lid}.json"), "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)

    # Optional combined JSON (kept for convenience)
    combined_rows: List[dict] = []
    for lid in sorted(league_payloads):
        combined_rows.extend(league_payloads[lid])
    with open(os.path.join(out_root, "combined.json"), "w", encoding="utf-8") as f:
        json.dump({
            "utc_time": now_iso,
            "processed": processed,
            "by_league": by_league_counts,
            "fixtures": combined_rows,
        }, f, ensure_ascii=False)

    # summaries
    with open(os.path.join(out_root, "summary.txt"), "w", encoding="utf-8") as f:
        f.write(f"Time (UTC): {now_iso}\n")
        f.write(f"Fixtures   : {processed}\n\n")
        f.write("Per league counts:\n")
        for lid in sorted(by_league_counts):
            f.write(f"  - {lid} ({LEAGUE_NAMES.get(lid, lid)}): {by_league_counts[lid]}\n")

    # verbose fixture-by-fixture with XI lines
    lines: List[str] = []
    lines.append(f"Time (UTC): {now_iso}")
    lines.append(f"Fixtures   : {processed}")
    lines.append("")
    for lid in sorted(league_payloads):
        lname = LEAGUE_NAMES.get(lid, str(lid))
        lines.append(f"===== {lname} (LID {lid}) =====")
        for r in league_payloads[lid]:
            dt_str = (r.get("starting_at") or "").replace("T", " ").replace("Z", "")
            lines.append(f"{dt_str}  —  {r['home']['name']} vs {r['away']['name']}  (FID {r['fixture_id']})")

            def xi_line(team: dict) -> str:
                parts = []
                for p in team["predicted_xi"]:
                    nm = p["name"] or f"#{p.get('jersey')}"
                    pos = p.get("position_label") or "?"
                    if p["status"].startswith("OUT"):
                        nm = f"{nm} [{pos}][OUT]"
                    else:
                        nm = f"{nm} [{pos}]"
                    parts.append(nm)
                return ", ".join(parts) if parts else "(no XI found)"

            lines.append(f"  {r['home']['name']} predicted 11 = {xi_line(r['home'])}")
            lines.append(f"  {r['away']['name']} predicted 11 = {xi_line(r['away'])}")
            lines.append("")
    with open(os.path.join(out_root, "summary_verbose.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines).rstrip() + "\n")

    print("\nDone.")
    print(f"Processed fixtures: {processed}")
    for lid in sorted(by_league_counts):
        print(f"  - {LEAGUE_NAMES.get(lid, lid)}: {by_league_counts[lid]}")
    print("Wrote:")
    print("  • data/predicted_xi/by_league/<league_id>.json")
    print("  • data/predicted_xi/combined.json")
    print("  • data/predicted_xi/summary.txt")
    print("  • data/predicted_xi/summary_verbose.txt")

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
