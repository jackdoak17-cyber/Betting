#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Sportmonks — Bet365 ALL odds dump (TXT) v1.2
--------------------------------------------
• Reads fixtures from data/fixtures/*.json and data/fixtures/by_league/*.json
• Fetches ALL pre-match odds from Bet365 for each fixture (no filtering)
• Writes a single human-readable TXT: data/value_bets/bet365_all_odds.txt

ENV
  SPORTMONKS_TOKEN (required)
  SM_MAX_FIXTURES (default "800") safety cap

Endpoints
  - GET /v3/odds/bookmakers
  - GET /v3/football/odds/pre-match/fixtures/{fixture_id}/bookmakers/{bookmaker_id}
    (falls back to /fixtures/{fixture_id} and filters Bet365 rows client-side)
"""
from __future__ import annotations
import os, json, time, re, unicodedata
from pathlib import Path
from typing import Dict, List, Optional, Any, Tuple
from datetime import datetime
import requests

# ---- IO ----
ROOT = Path(".")
FIX_DIR      = ROOT / "data" / "fixtures"
FIX_BY_L_DIR = FIX_DIR / "by_league"
OUT_DIR      = ROOT / "data" / "value_bets"
OUT_DIR.mkdir(parents=True, exist_ok=True)
OUT_TXT      = OUT_DIR / "bet365_all_odds.txt"

# ---- API bases ----
API_BASE_FOOTBALL = "https://api.sportmonks.com/v3/football"
API_BASE_ODDS     = "https://api.sportmonks.com/v3/odds"

TOKEN = (
    os.getenv("SPORTMONKS_TOKEN")
    or os.getenv("SPORTMONKS_API_TOKEN")
    or os.getenv("SM_TOKEN")
)
if not TOKEN:
    raise SystemExit("ERROR: SPORTMONKS_TOKEN / SPORTMONKS_API_TOKEN / SM_TOKEN not set.")

MAX_FIXTURES = int(os.getenv("SM_MAX_FIXTURES", "800"))

TIMEOUT = 25
RETRIES = 3
BACKOFF = 1.7
GLOBAL_MIN_DELAY = 0.18
_last_call = 0.0

# ---- HTTP helpers ----
def _pace():
    global _last_call
    now = time.time()
    if now - _last_call < GLOBAL_MIN_DELAY:
        time.sleep(GLOBAL_MIN_DELAY - (now - _last_call))
    _last_call = time.time()

def api_get(base: str, path: str, params: Optional[dict] = None) -> dict:
    if params is None:
        params = {}
    params = {**params, "api_token": TOKEN}
    url = f"{base}/{path.lstrip('/')}"
    last_e = None
    for i in range(1, RETRIES + 1):
        _pace()
        try:
            r = requests.get(url, params=params, timeout=TIMEOUT)
            if r.status_code == 429:
                sleep = min(60, (BACKOFF ** i) * 2)
                print(f"[429] {path} — sleeping {sleep:.1f}s")
                time.sleep(sleep)
                continue
            r.raise_for_status()
            return r.json()
        except Exception as e:
            last_e = e
            if i < RETRIES:
                sleep = BACKOFF ** i
                print(f"[RETRY] {path} (attempt {i}) sleeping {sleep:.1f}s")
                time.sleep(sleep)
            else:
                raise
    raise last_e

# ---- utils ----
def norm(s: Optional[str]) -> str:
    if not s: return ""
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = s.lower().strip()
    s = re.sub(r"[^a-z0-9+ ]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s

def coerce_list(x: Any) -> List[Any]:
    if isinstance(x, list): return x
    if isinstance(x, dict): return list(x.values())
    return []

# ---- fixtures: read from data/fixtures/* ----
def read_fixtures() -> Tuple[List[int], Dict[int, dict]]:
    """
    Returns (fixture_ids, info_by_fid)
    info_by_fid[fid] = {league_id, starting_at, home, away}
    """
    fixture_ids: List[int] = []
    info: Dict[int, dict] = {}

    def add_from_blob(blob: dict):
        for fx in blob.get("fixtures") or []:
            fid = int(fx.get("id") or fx.get("fixture_id") or 0)
            if not fid: continue
            if fid in info:  # don't overwrite
                continue
            league_id = fx.get("league_id")
            starting_at = fx.get("starting_at") or (fx.get("time") or {}).get("starting_at")
            home, away = None, None
            parts = fx.get("participants") or []
            if parts:
                for p in parts:
                    loc = (p.get("meta") or {}).get("location")
                    if loc == "home": home = p.get("name")
                    elif loc == "away": away = p.get("name")
            # some payloads may have names stored already
            home = home or fx.get("home_name")
            away = away or fx.get("away_name")
            info[fid] = {
                "league_id": league_id,
                "starting_at": starting_at,
                "home": home or "Home",
                "away": away or "Away",
            }
            fixture_ids.append(fid)

    # top-level files like data/fixtures/301.json
    if FIX_DIR.exists():
        for f in FIX_DIR.glob("*.json"):
            if f.name == "latest.json":  # skip the aggregate meta file
                continue
            try:
                blob = json.loads(f.read_text(encoding="utf-8"))
            except Exception:
                continue
            add_from_blob(blob)

    # by_league mirror
    if FIX_BY_L_DIR.exists():
        for f in FIX_BY_L_DIR.glob("*.json"):
            try:
                blob = json.loads(f.read_text(encoding="utf-8"))
            except Exception:
                continue
            add_from_blob(blob)

    # de-dup while keeping order
    seen = set()
    unique_ids: List[int] = []
    for fid in fixture_ids:
        if fid not in seen:
            seen.add(fid)
            unique_ids.append(fid)

    return unique_ids, info

# ---- bookmaker (Bet365) ----
def fetch_bet365_id() -> int:
    """
    Resolve Bet365 bookmaker id. Fallback to 2 if lookup fails.
    """
    try:
        j = api_get(API_BASE_ODDS, "bookmakers")
        for row in j.get("data", []) or []:
            nm = (row.get("name") or "").strip()
            if norm(nm) == "bet365" or "bet365" in norm(nm):
                return int(row.get("id") or 0) or 2
    except Exception as e:
        print(f"[warn] bookmakers fetch failed: {e}")
    return 2

def fetch_fixture_odds_bet365(fid: int, bet365_id: int) -> dict:
    """
    Prefer bookmaker-scoped endpoint; fallback to unscoped and filter.
    """
    try:
        return api_get(API_BASE_FOOTBALL, f"odds/pre-match/fixtures/{fid}/bookmakers/{bet365_id}")
    except Exception as e:
        print(f"  [fallback] fixture {fid} bookmaker-scoped failed: {e}")
        j = api_get(API_BASE_FOOTBALL, f"odds/pre-match/fixtures/{fid}")
        # filter to Bet365 only
        kept = []
        for row in j.get("data", []) or []:
            bm = row.get("bookmaker") or {}
            bid = int(bm.get("id") or row.get("bookmaker_id") or 0)
            bname = (bm.get("name") or row.get("bookmaker_name") or "")
            if bid == bet365_id or "bet365" in norm(bname):
                kept.append(row)
        return {"data": kept}

# ---- pretty printers ----
def outcome_price(o: dict) -> str:
    # show any decimal-like value we can find; else raw value; else "?"
    for k in ("decimal", "price", "odd", "odds", "value"):
        v = o.get(k)
        try:
            if v is not None:
                return f"{float(v):.3f}"
        except Exception:
            pass
    p = o.get("prices") or o.get("bookmaker_price") or {}
    if isinstance(p, dict):
        for k in ("decimal", "dec", "d"):
            if k in p:
                try:
                    return f"{float(p[k]):.3f}"
                except Exception:
                    pass
    v = o.get("price") or o.get("odds") or o.get("value")
    return str(v) if v is not None else "?"

def line_side_suffix(o: dict) -> str:
    parts = []
    for k in ("line", "handicap", "goal", "total", "threshold"):
        if o.get(k) is not None:
            try:
                parts.append(f"{k}={float(o[k])}")
            except Exception:
                parts.append(f"{k}={o[k]}")
    s = o.get("side") or o.get("direction") or o.get("bet_type")
    if s:
        parts.append(f"side={s}")
    return (" [" + ", ".join(parts) + "]") if parts else ""

def extract_participant(o: dict) -> str:
    pv = o.get("participant") or o.get("player") or o.get("competitor")
    if isinstance(pv, dict):
        for kk in ("name", "player_name", "participant_name"):
            if pv.get(kk):
                return str(pv[kk])
    return ""

def market_id_of(m: dict) -> Optional[int]:
    for k in ("id", "market_id"):
        if m.get(k) is not None:
            try:
                return int(m[k])
            except Exception:
                pass
    return None

def dump_fixture(fid: int, head: dict, payload: dict) -> List[str]:
    lines: List[str] = []
    hn, an = head.get("home") or "Home", head.get("away") or "Away"
    ko = head.get("starting_at") or ""
    lid = head.get("league_id") or ""
    lines.append(f"{hn} vs {an}  |  Fixture {fid}  |  League {lid}  |  {ko}")

    rows = payload.get("data") or []
    if isinstance(rows, dict):
        rows = [rows]
    if not rows:
        lines.append("  (no Bet365 markets returned)")
        return lines

    for row in rows:
        bm = row.get("bookmaker") or {}
        bname = bm.get("name") or row.get("bookmaker_name") or "Bet365"
        lines.append(f"  Bookmaker: {bname}")

        markets = coerce_list(row.get("markets") or row.get("odds") or row.get("children") or [])
        if not markets:
            lines.append("    (no markets)")
            continue

        for m in markets:
            mid = market_id_of(m)
            mname = m.get("name") or m.get("market") or m.get("key") or "Market"
            mlabel = m.get("label") or ""
            if mid is not None:
                head_line = f"  - Market [{mid}]: {mname}"
            else:
                head_line = f"  - Market: {mname}"
            if mlabel:
                head_line += f" — {mlabel}"
            lines.append(head_line)

            outs = coerce_list(m.get("outcomes") or m.get("selections") or m.get("runners") or [])
            if not outs:
                lines.append("      (no outcomes)")
                continue

            for o in outs:
                oname = (o.get("name") or o.get("label") or o.get("selection")
                         or o.get("runner_name") or o.get("outcome") or "Outcome")
                price = outcome_price(o)
                extra = line_side_suffix(o)
                part = extract_participant(o)
                pid = o.get("id") or o.get("outcome_id")
                pid_str = f"#{pid} " if pid is not None else ""
                pstr = f" — {part}" if part else ""
                lines.append(f"      * {pid_str}{oname}{pstr} @ {price}{extra}")
    return lines

# ---- main ----
def main():
    generated_at = datetime.utcnow().isoformat()

    fids, info = read_fixtures()
    if len(fids) > MAX_FIXTURES:
        fids = fids[:MAX_FIXTURES]

    bet365_id = fetch_bet365_id()
    header = [
        f"Generated at (UTC): {generated_at}",
        f"Source: Sportmonks pre-match odds",
        f"Bookmaker: Bet365 (id={bet365_id})",
        f"Fixtures: {len(fids)}",
        "",
    ]

    out_lines: List[str] = []
    out_lines.extend(header)

    if not fids:
        out_lines.append("(no fixtures found under data/fixtures — did fetch_fixtures.py run?)")

    for i, fid in enumerate(fids, 1):
        print(f"[{i}/{len(fids)}] Fixture {fid}")
        try:
            payload = fetch_fixture_odds_bet365(fid, bet365_id)
        except Exception as e:
            out_lines.append(f"Fixture {fid}")
            out_lines.append(f"  [error] {e}")
            out_lines.append("")
            continue
        out_lines.extend(dump_fixture(fid, info.get(fid, {}), payload))
        out_lines.append("")

    OUT_TXT.write_text("\n".join(out_lines).rstrip() + "\n", encoding="utf-8")
    print(f"[OK] wrote {OUT_TXT}")

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
