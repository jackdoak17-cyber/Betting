#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Team Form Signals — Corners / Cards / Shots / SOT (Sportmonks v3)

Pure stats builder (no odds). Produces:
- data/team_stats/by_league/{league_id}.json        -> per-team last-N sequences + win-rates
- data/team_stats/mismatches/{league_id}.json       -> upcoming fixtures with W/L 'mismatch' flags
- data/team_stats/summary.txt                       -> human-readable summary

Inputs (env):
  SPORTMONKS_TOKEN  (required)
  LEAGUE_IDS        default "8"            # e.g. "8,564,82,384,301"
  LAST_N            default 10             # last-N finished league games used for form
  MIN_MATCHES       default 6              # min (W+L) samples to evaluate a team
  EDGE_MIN_WIN      default 0.60           # strong side threshold
  EDGE_MAX_WIN      default 0.40           # weak side threshold
  CARD_RED_WEIGHT   default 1              # treat 1 red = N yellows (1=just count as 1 card)
  OUT_BASE          default "data/team_stats"   # root output folder

Depends on your stored upcoming fixtures at:
  data/fixtures/by_league/{league_id}.json
"""

import os, re, json, time, datetime as dt
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Any
import requests

# ---------------- Config / ENV ----------------
API_BASE = "https://api.sportmonks.com/v3/football"
API_TOKEN = (
    os.getenv("SPORTMONKS_TOKEN")
    or os.getenv("SPORTMONKS_API_TOKEN")
    or os.getenv("SM_TOKEN")
)
if not API_TOKEN:
    raise SystemExit("ERROR: SPORTMONKS_TOKEN / SPORTMONKS_API_TOKEN not set.")

LEAGUE_IDS = [int(x) for x in (os.getenv("LEAGUE_IDS", "8").strip() or "8").split(",") if x.strip()]
LAST_N = int(os.getenv("LAST_N", "10"))
MIN_MATCHES = int(os.getenv("MIN_MATCHES", "6"))
EDGE_MIN_WIN = float(os.getenv("EDGE_MIN_WIN", "0.60"))
EDGE_MAX_WIN = float(os.getenv("EDGE_MAX_WIN", "0.40"))
CARD_RED_WEIGHT = float(os.getenv("CARD_RED_WEIGHT", "1"))
OUT_BASE = Path(os.getenv("OUT_BASE", "data/team_stats"))

FORM_DIR = OUT_BASE / "by_league"
MM_DIR = OUT_BASE / "mismatches"
FORM_DIR.mkdir(parents=True, exist_ok=True)
MM_DIR.mkdir(parents=True, exist_ok=True)

LOCAL_FIX_BY_LEAGUE = Path("data/fixtures/by_league")

# pacing / retry
TIMEOUT = 25
RETRIES = 3
BACKOFF = 1.6
GLOBAL_MIN_DELAY = 0.18
_last = 0.0

def _pace():
    global _last
    now = time.time()
    if now - _last < GLOBAL_MIN_DELAY:
        time.sleep(GLOBAL_MIN_DELAY - (now - _last))
    _last = time.time()

def api_get(path: str, params: Optional[dict] = None) -> dict:
    if params is None:
        params = {}
    params = {**params, "api_token": API_TOKEN}
    url = f"{API_BASE}/{path.lstrip('/')}"
    last_exc: Optional[Exception] = None
    for i in range(1, RETRIES + 1):
        _pace()
        try:
            r = requests.get(url, params=params, timeout=TIMEOUT)
            if r.status_code == 429:
                sleep = min(60, (BACKOFF ** i) * 2.0)
                print(f"[429] {path} — sleeping {sleep:.1f}s")
                time.sleep(sleep); continue
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
    raise last_exc  # type: ignore

def today_utc_date() -> dt.date:
    return dt.datetime.now(dt.timezone.utc).date()

def get_current_season(league_id: int) -> int:
    j = api_get(f"leagues/{league_id}", params={"include": "currentSeason"})
    data = j.get("data") or {}
    s = data.get("currentseason") or data.get("currentSeason") or {}
    sid = int(s.get("id") or 0)
    if not sid:
        raise RuntimeError(f"No currentSeason for league {league_id}")
    return sid

def get_season_bounds(season_id: int) -> Tuple[dt.date, dt.date]:
    j = api_get(f"seasons/{season_id}")
    d = j.get("data") or {}
    start_s = (d.get("starting_at") or "").split("T")[0] or (d.get("starting_at") or "").split(" ")[0]
    end_s   = (d.get("ending_at") or "").split("T")[0] or (d.get("ending_at") or "").split(" ")[0]
    start = dt.datetime.strptime(start_s, "%Y-%m-%d").date() if start_s else today_utc_date().replace(month=8, day=1)
    end   = min(today_utc_date(), dt.datetime.strptime(end_s, "%Y-%m-%d").date() if end_s else today_utc_date())
    return start, end

# ---------------- Season fixtures with stats ----------------
def fetch_fixtures_for_season(season_id: int, page: int = 1) -> dict:
    """
    Season fixtures, include participants + state + statistics.
    """
    params = {
        "include": "participants;state;statistics.types",
        "order": "desc",
        "per_page": 50,
        "page": page,
    }
    return api_get(f"fixtures/seasons/{season_id}", params)

# ---------------- Stat parsing ----------------
def _lower(s: Optional[str]) -> str:
    return (s or "").strip().lower()

STAT_TOKENS = {
    "corners": {"corner", "corners", "corner kicks", "cornerkicks"},
    "shots":   {"shots", "shots total", "total shots"},
    "sot":     {"shots on target", "shots on goal", "on target", "on-target"},
    "yc":      {"yellowcards", "yellow cards", "yellow card"},
    "rc":      {"redcards", "red cards", "red card"},
}

def _extract_team_stats_from_fx(fx: dict) -> Dict[int, Dict[str, float]]:
    """
    Returns: { team_id: {"corners": x, "shots": y, "sot": z, "cards": c} }
    cards = yc + rc * CARD_RED_WEIGHT
    """
    out: Dict[int, Dict[str, float]] = {}
    stats = fx.get("statistics") or []
    if isinstance(stats, dict):
        stats = [stats]

    for block in stats:
        try:
            tid = block.get("team_id") or block.get("participant_id") or block.get("id")
            tid = int(tid)
        except Exception:
            continue

        entries = block.get("types") or block.get("stats") or block.get("details") or []
        tmap = {"corners": 0.0, "shots": 0.0, "sot": 0.0, "cards": 0.0}
        have = {"corners": False, "shots": False, "sot": False, "yc": False, "rc": False}
        tmp_yc, tmp_rc = 0.0, 0.0

        for e in entries or []:
            name = _lower(e.get("name") or e.get("type") or e.get("description"))

            # normalize value
            v = e.get("value")
            if isinstance(v, dict):
                v = v.get("value")
            if v is None and isinstance(e.get("data"), dict):
                v = e["data"].get("value")
            try:
                val = float(v)
            except Exception:
                continue

            if any(tok in name for tok in STAT_TOKENS["corners"]):
                tmap["corners"] = val; have["corners"] = True
            elif any(tok in name for tok in STAT_TOKENS["sot"]):
                tmap["sot"] = val; have["sot"] = True
            elif any(tok in name for tok in STAT_TOKENS["shots"]):
                tmap["shots"] = val; have["shots"] = True
            elif any(tok in name for tok in STAT_TOKENS["yc"]):
                tmp_yc = val; have["yc"] = True
            elif any(tok in name for tok in STAT_TOKENS["rc"]):
                tmp_rc = val; have["rc"] = True

        if have["yc"] or have["rc"]:
            tmap["cards"] = float(tmp_yc) + float(tmp_rc) * float(CARD_RED_WEIGHT)

        out[tid] = tmap
    return out

# ---------------- W/L/D helpers ----------------
def cmp_code(a: Optional[float], b: Optional[float]) -> str:
    try:
        fa = float(a); fb = float(b)
    except Exception:
        return "D"
    if fa > fb: return "W"
    if fa < fb: return "L"
    return "D"

def push_sequence(seqs: Dict[str, List[str]], key: str, code: str):
    arr = seqs.setdefault(key, [])
    arr.append(code)

def winrate(seq: List[str]) -> Tuple[float, int, int]:
    w = sum(1 for x in seq if x == "W")
    l = sum(1 for x in seq if x == "L")
    n = w + l
    return (w / n if n > 0 else 0.0, w, n)

# ---------------- Upcoming fixtures (local) ----------------
def load_upcoming_fixtures(league_id: int) -> List[dict]:
    p = LOCAL_FIX_BY_LEAGUE / f"{league_id}.json"
    try:
        blob = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return []
    fixtures = blob.get("fixtures") or (blob.get("data") or {}).get("fixtures") or []
    out = []
    now_ts = dt.datetime.now(dt.timezone.utc).timestamp()
    for fx in fixtures:
        ko_ts = fx.get("starting_at_timestamp")
        if isinstance(ko_ts, (int, float)) and ko_ts >= now_ts:
            out.append(fx)
            continue
        # fallback parse "YYYY-MM-DD HH:MM:SS"
        s = fx.get("starting_at")
        if isinstance(s, str) and "T" not in s:
            try:
                ts = dt.datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=dt.timezone.utc).timestamp()
                if ts >= now_ts:
                    out.append(fx)
            except Exception:
                pass
    return out

# ---------------- Per-league processing ----------------
def process_league(league_id: int) -> Tuple[dict, dict]:
    season_id = get_current_season(league_id)

    # Pull finished fixtures with stats
    finished_fx: List[dict] = []
    page = 1
    while True:
        j = fetch_fixtures_for_season(season_id, page=page)
        data = j.get("data") or []
        meta = j.get("meta") or {}
        for fx in data:
            st = int(fx.get("state_id") or fx.get("state", {}).get("id") or 0)
            if st != 5:    # finished
                continue
            if int(fx.get("league_id") or 0) != league_id:
                continue
            finished_fx.append(fx)
        if not meta.get("has_more"):
            break
        page += 1

    # Build sequences per team (latest->older order preserved by 'order=desc')
    per_team_seq: Dict[int, Dict[str, Any]] = {}
    for fx in finished_fx:
        parts = fx.get("participants") or fx.get("teams") or []
        if isinstance(parts, dict): parts = list(parts.values())
        if not isinstance(parts, list) or len(parts) < 2:
            continue
        try:
            t1 = int(parts[0].get("id") or parts[0].get("team_id")); n1 = parts[0].get("name") or ""
            t2 = int(parts[1].get("id") or parts[1].get("team_id")); n2 = parts[1].get("name") or ""
        except Exception:
            continue

        stats_by_team = _extract_team_stats_from_fx(fx)
        s1 = stats_by_team.get(t1) or {}
        s2 = stats_by_team.get(t2) or {}

        if t1 not in per_team_seq:
            per_team_seq[t1] = {"team_id": t1, "team_name": n1, "seq": {"corners": [], "cards": [], "shots": [], "sot": []}}
        if t2 not in per_team_seq:
            per_team_seq[t2] = {"team_id": t2, "team_name": n2, "seq": {"corners": [], "cards": [], "shots": [], "sot": []}}

        for key in ("corners", "cards", "shots", "sot"):
            c12 = cmp_code(s1.get(key), s2.get(key))
            c21 = "W" if c12 == "L" else "L" if c12 == "W" else "D"
            push_sequence(per_team_seq[t1]["seq"], key, c12)
            push_sequence(per_team_seq[t2]["seq"], key, c21)

    # Trim to LAST_N & compute win-rates
    team_rows: List[dict] = []
    for tid, row in per_team_seq.items():
        seqs = row["seq"]
        out_seq = {}
        rates = {}
        for k, arr in seqs.items():
            arr = arr[:LAST_N]
            out_seq[k] = arr
            wr, w, n = winrate(arr)
            rates[k] = {"win_rate": round(wr, 4), "wins": w, "n": n}
        team_rows.append({
            "team_id": tid,
            "team_name": row["team_name"],
            "last_n": LAST_N,
            "sequences": out_seq,       # e.g. {"corners":["W","L",...]}
            "win_rates": rates,         # e.g. {"corners":{"win_rate":0.6,"wins":6,"n":10}}
        })

    team_rows.sort(key=lambda r: r["team_name"])
    team_form_payload = {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "league_id": league_id,
        "season_id": season_id,
        "last_n": LAST_N,
        "min_matches": MIN_MATCHES,
        "card_red_weight": CARD_RED_WEIGHT,
        "teams": team_rows,
        "count": len(team_rows),
    }

    # Mismatch flags for upcoming fixtures (from local file)
    idx = {r["team_id"]: r for r in team_rows}
    mm_rows: List[dict] = []
    for fx in load_upcoming_fixtures(league_id):
        parts = fx.get("participants") or []
        if isinstance(parts, dict): parts = list(parts.values())
        if not isinstance(parts, list) or len(parts) < 2:
            continue
        try:
            t1 = int(parts[0].get("id") or parts[0].get("team_id")); n1 = parts[0].get("name") or ""
            t2 = int(parts[1].get("id") or parts[1].get("team_id")); n2 = parts[1].get("name") or ""
        except Exception:
            continue

        r1 = idx.get(t1); r2 = idx.get(t2)
        if not r1 or not r2:
            continue

        flags = {}
        details = {}

        def maybe_flag(stat_key: str):
            a = r1["win_rates"][stat_key]; b = r2["win_rates"][stat_key]
            wr_a, n_a = a["win_rate"], a["n"]
            wr_b, n_b = b["win_rate"], b["n"]
            if n_a >= MIN_MATCHES and n_b >= MIN_MATCHES:
                if wr_a >= EDGE_MIN_WIN and wr_b <= EDGE_MAX_WIN:
                    flags[stat_key] = {"fav": t1, "und": t2}
                if wr_b >= EDGE_MIN_WIN and wr_a <= EDGE_MAX_WIN:
                    flags[stat_key] = {"fav": t2, "und": t1}
                if stat_key in flags:
                    details[stat_key] = {
                        "team_a": {"team_id": t1, "name": n1, "wr": wr_a, "n": n_a},
                        "team_b": {"team_id": t2, "name": n2, "wr": wr_b, "n": n_b},
                    }

        for k in ("corners", "cards", "shots", "sot"):
            maybe_flag(k)

        if flags:
            mm_rows.append({
                "fixture_id": int(fx.get("id") or 0),
                "name": fx.get("name") or f"{n1} vs {n2}",
                "starting_at": fx.get("starting_at") or "",
                "starting_at_timestamp": fx.get("starting_at_timestamp"),
                "flags": flags,
                "details": details,
            })

    mismatches_payload = {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "league_id": league_id,
        "season_id": season_id,
        "params": {
            "last_n": LAST_N, "min_matches": MIN_MATCHES,
            "edge_min_win": EDGE_MIN_WIN, "edge_max_win": EDGE_MAX_WIN,
            "card_red_weight": CARD_RED_WEIGHT,
        },
        "fixtures_flagged": mm_rows,
        "count": len(mm_rows),
    }

    return team_form_payload, mismatches_payload

# ---------------- Main ----------------
def main():
    OUT_BASE.mkdir(parents=True, exist_ok=True)
    summary_lines: List[str] = [f"Generated at (UTC): {dt.datetime.now(dt.timezone.utc).isoformat()}", ""]
    for lid in LEAGUE_IDS:
        try:
            form, mism = process_league(lid)
        except Exception as e:
            print(f"[ERROR] League {lid}: {type(e).__name__}: {e}")
            continue

        p_form = FORM_DIR / f"{lid}.json"
        p_mism = MM_DIR / f"{lid}.json"
        p_form.write_text(json.dumps(form, ensure_ascii=False, indent=2), encoding="utf-8")
        p_mism.write_text(json.dumps(mism, ensure_ascii=False, indent=2), encoding="utf-8")

        summary_lines.append(f"League {lid}: {form['count']} teams | Mismatches: {mism['count']}")
        for row in (mism["fixtures_flagged"][:6] if mism["fixtures_flagged"] else []):
            labs = ", ".join(sorted(row["flags"].keys()))
            summary_lines.append(f"  • {row['name']} — {labs}")
        summary_lines.append("")

    (OUT_BASE / "summary.txt").write_text("\n".join(summary_lines).rstrip() + "\n", encoding="utf-8")
    print("\n".join(summary_lines))

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
