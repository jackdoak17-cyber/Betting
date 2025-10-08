#!/usr/bin/env python3
"""
Fetch upcoming fixtures (Sportmonks v3) for a fixed set of leagues, within a rolling window.

What it does
------------
- Queries **per league** to avoid cross-league leakage.
- Paginates defensively.
- Hard-filters results by ALLOWED_LEAGUES (double safety).
- Dedupes by fixture id.
- Sorts output by (starting_at, league_id, id).
- Writes:
    data/fixtures/latest.json
    data/fixtures/summary.txt
    data/fixtures/by_league/<league_id>.json
  These files are **overwritten** each run.

Env vars
--------
SPORTMONKS_API_TOKEN  (required)
DAYS_AHEAD            (optional, default 21)
"""

import os
import sys
import json
from pathlib import Path
from datetime import datetime, date, timedelta
from typing import Dict, List, Any
import urllib.request
import urllib.parse

# ----- config -----
ALLOWED_LEAGUES = [8, 9, 82, 301, 384, 387, 564, 567, 600]  # keep sorted
DAYS_AHEAD = int(os.getenv("DAYS_AHEAD", "21"))
API_TOKEN = os.getenv("SPORTMONKS_API_TOKEN", "").strip()

BASE_URL = "https://api.sportmonks.com/v3/football/fixtures/between/{start}/{end}"
INCLUDE = ",".join([
    "participants",
    "league",
    "season",
    "venue",
    "round",
])

OUT_ROOT = Path("data/fixtures")
OUT_BY_LEAGUE = OUT_ROOT / "by_league"
OUT_LATEST_JSON = OUT_ROOT / "latest.json"
OUT_SUMMARY_TXT = OUT_ROOT / "summary.txt"


# ----- http helper -----
def api_get(url: str, params: Dict[str, Any]) -> Dict[str, Any]:
    q = urllib.parse.urlencode(params, doseq=True)
    full = f"{url}?{q}"
    req = urllib.request.Request(full, headers={"User-Agent": "fixtures-bot/1.0"})
    with urllib.request.urlopen(req, timeout=60) as r:
        body = r.read()
    try:
        return json.loads(body.decode("utf-8"))
    except Exception as e:
        raise RuntimeError(f"Failed to decode JSON from: {full}\nError: {e}\nBody: {body[:500]!r}") from e


def paginate_between(start_iso: str, end_iso: str, league_id: int) -> List[Dict[str, Any]]:
    """
    Fetch fixtures between start/end for a single league, following pagination.
    Works against typical Sportmonks v3 pagination shapes:
      - meta.pagination.{current_page,total_pages,next_page,per_page}
      - or 'pagination' at top-level (defensive).
    """
    url = BASE_URL.format(start=start_iso, end=end_iso)
    page = 1
    collected: List[Dict[str, Any]] = []

    while True:
        params = {
            "api_token": API_TOKEN,
            "include": INCLUDE,
            "filters": f"league_id:{league_id}",
            "per_page": 200,
            "page": page,
        }
        payload = api_get(url, params)

        # Most v3 responses are shaped as {"data": [...], "meta": {...}}
        data = payload.get("data")
        if isinstance(data, list):
            collected.extend(data)
        elif isinstance(payload, dict) and all(k in payload for k in ("id", "starting_at", "league_id")):
            # extremely defensive: a single object response
            collected.append(payload)
        else:
            # nothing more (or unexpected)
            break

        # detect next page
        meta = payload.get("meta", {}) or payload.get("pagination", {}) or {}
        pg = meta.get("pagination", meta)
        cur = pg.get("current_page")
        tot = pg.get("total_pages")
        nxt = pg.get("next_page")
        if isinstance(cur, int) and isinstance(tot, int):
            if cur < tot:
                page += 1
                continue
            break
        if nxt:
            page = int(nxt)
            continue
        # if no pagination signals, assume single page
        break

    return collected


# ----- utilities -----
def coerce_starting_at(s: Any) -> str:
    """Return a safe 'YYYY-MM-DD HH:MM:SS' string or empty string."""
    if not s:
        return ""
    # API often returns 'YYYY-MM-DD HH:MM:SS'
    return str(s)


def hard_filter_and_dedupe(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Keep only allowed leagues; dedupe by id."""
    by_id: Dict[int, Dict[str, Any]] = {}
    for it in items:
        try:
            lid = int(it.get("league_id"))
        except Exception:
            continue
        if lid not in ALLOWED_LEAGUES:
            continue
        fid = int(it.get("id"))
        by_id[fid] = it
    # sort
    def sort_key(it: Dict[str, Any]):
        ts = coerce_starting_at(it.get("starting_at"))
        lid = int(it.get("league_id", 0))
        fid = int(it.get("id", 0))
        return (ts, lid, fid)
    return sorted(by_id.values(), key=sort_key)


def write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, separators=(",", ":"), indent=2)


def write_summary(path: Path, header: str, rows: List[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        f.write(header.rstrip() + "\n")
        if rows:
            f.write("\n".join(rows) + "\n")


def main() -> int:
    if not API_TOKEN:
        print("ERROR: SPORTMONKS_API_TOKEN is not set.", file=sys.stderr)
        return 1

    # dates
    start = date.today()
    end = start + timedelta(days=DAYS_AHEAD)
    start_iso = start.isoformat()
    end_iso = end.isoformat()

    # ensure folders exist
    OUT_BY_LEAGUE.mkdir(parents=True, exist_ok=True)
    OUT_ROOT.mkdir(parents=True, exist_ok=True)

    # fetch per league
    all_raw: List[Dict[str, Any]] = []
    per_league_raw: Dict[int, List[Dict[str, Any]]] = {}

    for lid in ALLOWED_LEAGUES:
        try:
            items = paginate_between(start_iso, end_iso, lid)
        except Exception as e:
            print(f"[WARN] League {lid}: fetch error: {e}", file=sys.stderr)
            items = []
        per_league_raw[lid] = items
        all_raw.extend(items)

    # hard filter + dedupe globally
    all_clean = hard_filter_and_dedupe(all_raw)

    # also prepare per-league cleaned files
    per_league_clean: Dict[int, List[Dict[str, Any]]] = {lid: [] for lid in ALLOWED_LEAGUES}
    for it in all_clean:
        per_league_clean[int(it["league_id"])].append(it)

    # sort each league subset by date then id (already mostly sorted)
    for lid in ALLOWED_LEAGUES:
        per_league_clean[lid] = sorted(
            per_league_clean[lid],
            key=lambda it: (coerce_starting_at(it.get("starting_at")), int(it.get("id", 0))),
        )

    # write by-league json (overwrite)
    for lid in ALLOWED_LEAGUES:
        write_json(OUT_BY_LEAGUE / f"{lid}.json", per_league_clean[lid])

    # write latest.json (overwrite)
    meta = {
        "generated_at_utc": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "start_date": start_iso,
        "end_date": end_iso,
        "league_ids": [str(l) for l in ALLOWED_LEAGUES],
        "count": len(all_clean),
        "data": all_clean,
    }
    write_json(OUT_LATEST_JSON, meta)

    # write summary.txt (overwrite)
    lines: List[str] = []
    for it in all_clean:
        fid = it.get("id")
        kick = coerce_starting_at(it.get("starting_at"))
        name = it.get("name") or ""
        lines.append(f"{fid} | {kick} | {name}")

    header = (
        "fixtures = "
        f"Time (UTC): {datetime.utcnow().isoformat(timespec='seconds')}Z\n"
        f"Window    : {start_iso} -> {end_iso}\n"
        f"Leagues   : {','.join(str(x) for x in ALLOWED_LEAGUES)}\n"
        f"Fixtures  : {len(all_clean)} (written {len(all_clean)})"
    )
    write_summary(OUT_SUMMARY_TXT, header, lines)

    # console log (useful in Actions log)
    print(header)
    return 0


if __name__ == "__main__":
    sys.exit(main())
