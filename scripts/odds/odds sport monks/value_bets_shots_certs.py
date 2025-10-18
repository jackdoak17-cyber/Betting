#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Sportmonks — Bet365 ALL odds dump (TXT) v1.0
--------------------------------------------
- Uses predicted_xi fixtures list to know which fixture_ids to query.
- Fetches *all* pre-match odds for **Bet365** for each fixture.
- No filtering by market or price. Everything that Bet365 provides gets printed.
- Writes a single human-readable TXT to: data/value_bets/bet365_all_odds.txt

ENV
---
SPORTMONKS_TOKEN   (required)
SM_MAX_FIXTURES    (default: "500") — safety cap

Inputs
------
data/predicted_xi/by_league/*.json   (your existing pipeline output)

Endpoints
---------
- Bookmakers list:     https://api.sportmonks.com/v3/odds/bookmakers
- Fixture pre-match:   https://api.sportmonks.com/v3/football/odds/pre-match/fixtures/{fixture_id}/bookmakers/{bookmaker_id}
  (fallback to /fixtures/{fixture_id} and filter to Bet365 if needed)
"""
from __future__ import annotations
import os, json, time, re, unicodedata
from pathlib import Path
from typing import Dict, List, Optional, Any
from datetime import datetime
import requests

# ---------- Config / IO ----------
ROOT = Path(".")
PX_DIR = ROOT / "data" / "predicted_xi" / "by_league"
OUT_DIR = ROOT / "data" / "value_bets"
OUT_DIR.mkdir(parents=True, exist_ok=True)
OUT_TXT = OUT_DIR / "bet365_all_odds.txt"

API_BASE_FOOTBALL = "https://api.sportmonks.com/v3/football"
API_BASE_ODDS = "https://api.sportmonks.com/v3/odds"

TOKEN = (
    os.getenv("SPORTMONKS_TOKEN")
    or os.getenv("SPORTMONKS_API_TOKEN")
    or os.getenv("SM_TOKEN")
)
if not TOKEN:
    raise SystemExit("ERROR: SPORTMONKS_TOKEN / SPORTMONKS_API_TOKEN / SM_TOKEN not set.")

MAX_FIXTURES = int(os.getenv("SM_MAX_FIXTURES", "500"))

TIMEOUT = 25
RETRIES = 3
BACKOFF = 1.7
GLOBAL_MIN_DELAY = 0.18
_last_call = 0.0


# ---------- HTTP ----------
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


# ---------- helpers ----------
def norm(s: Optional[str]) -> str:
    if not s: return ""
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = s.lower().strip()
    s = re.sub(r"[^a-z0-9+ ]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s

def load_fixture_ids_from_predicted_xi() -> List[int]:
    fids: List[int] = []
    if not PX_DIR.exists():
        return fids
    for f in PX_DIR.glob("*.json"):
        try:
            blob = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        for fx in (blob.get("fixtures") or []):
            fid = int(fx.get("fixture_id") or fx.get("id") or 0)
            if fid:
                fids.append(fid)
    # unique + stable order
    seen = set()
    out = []
    for fid in fids:
        if fid not in seen:
            seen.add(fid); out.append(fid)
    return out

def fetch_bet365_id() -> int:
    """
    Resolve Bet365 bookmaker id. Fallback to 2 (commonly Bet365) if not found.
    """
    try:
        j = api_get(API_BASE_ODDS, "bookmakers")
        for row in j.get("data", []) or []:
            nm = (row.get("name") or "").strip()
            if norm(nm) == "bet365" or "bet365" in norm(nm):
                return int(row.get("id") or 0) or 2
    except Exception as e:
        print(f"[warn] bookmakers fetch failed: {e}")
    return 2  # pragmatic fallback

def fetch_fixture_odds_bet365(fid: int, bet365_id: int) -> dict:
    """
    Prefer bookmaker-scoped endpoint; fallback to unscoped and filter.
    """
    try:
        return api_get(API_BASE_FOOTBALL, f"odds/pre-match/fixtures/{fid}/bookmakers/{bet365_id}")
    except Exception as e:
        print(f"  [fallback] fixture {fid} bookmaker-scoped failed: {e}")
        j = api_get(API_BASE_FOOTBALL, f"odds/pre-match/fixtures/{fid}")
        # filter to Bet365 rows only
        data = []
        for row in j.get("data", []) or []:
            bm = row.get("bookmaker") or {}
            bid = int(bm.get("id") or row.get("bookmaker_id") or 0)
            bname = (bm.get("name") or row.get("bookmaker_name") or "").strip()
            if bid == bet365_id or "bet365" in norm(bname):
                data.append(row)
        return {"data": data}

def coerce_list(x: Any) -> List[Any]:
    if isinstance(x, list):
        return x
    if isinstance(x, dict):
        return list(x.values())
    return []

def fmt_price(outcome: dict) -> str:
    # print any decimal we can find; if none, try any number-like field
    for k in ("decimal", "price", "odd", "odds", "value"):
        v = outcome.get(k)
        try:
            return f"{float(v):.3f}"
        except Exception:
            pass
    p = outcome.get("prices") or outcome.get("bookmaker_price") or {}
    if isinstance(p, dict):
        for k in ("decimal", "dec", "d"):
            if k in p:
                try:
                    return f"{float(p[k]):.3f}"
                except Exception:
                    pass
    # last resort: show raw if exists
    v = outcome.get("price") or outcome.get("odds") or outcome.get("value")
    return str(v) if v is not None else "?"

def extract_outcome_name(o: dict) -> str:
    return (o.get("name")
            or o.get("label")
            or o.get("selection")
            or o.get("runner_name")
            or o.get("outcome")
            or "Outcome")

def extract_line_side(o: dict) -> str:
    parts = []
    for k in ("line", "handicap", "goal", "total", "threshold"):
        if o.get(k) is not None:
            try:
                parts.append(f"{k}={float(o[k])}")
            except Exception:
                parts.append(f"{k}={o[k]}")
    side = o.get("side") or o.get("direction") or o.get("bet_type")
    if side:
        parts.append(f"side={side}")
    return (" [" + ", ".join(parts) + "]") if parts else ""

def dump_fixture_block(fid: int, payload: dict) -> List[str]:
    lines: List[str] = []
    rows = payload.get("data") or []
    if not rows:
        lines.append(f"Fixture {fid}")
        lines.append("  (no Bet365 markets returned)")
        return lines

    # Some bookmaker-scoped responses return a single object; normalize to list
    if isinstance(rows, dict):
        rows = [rows]

    # There may still be multiple rows (e.g., same bookmaker split by category)
    lines.append(f"Fixture {fid}")
    for row in rows:
        bm = row.get("bookmaker") or {}
        bname = bm.get("name") or row.get("bookmaker_name") or "Bet365"
        lines.append(f"  Bookmaker: {bname}")

        markets = coerce_list(row.get("markets") or row.get("odds") or row.get("children") or [])
        if not markets:
            lines.append("    (no markets)")
            continue

        for m in markets:
            mname = m.get("name") or m.get("market") or m.get("key") or "Market"
            mlabel = m.get("label") or ""
            lines.append(f"  - Market: {mname}{(' — ' + mlabel) if mlabel else ''}")

            outs = coerce_list(m.get("outcomes") or m.get("selections") or m.get("runners") or [])
            if not outs:
                lines.append("      (no outcomes)")
                continue

            for o in outs:
                oname = extract_outcome_name(o)
                price = fmt_price(o)
                extra = extract_line_side(o)
                # If a player/participant field exists, append it
                participant = ""
                pv = o.get("participant") or o.get("player") or o.get("competitor")
                if isinstance(pv, dict):
                    pname = pv.get("name") or pv.get("player_name") or pv.get("participant_name")
                    if pname:
                        participant = f" — {pname}"
                lines.append(f"      * {oname}{participant} @ {price}{extra}")
    return lines


# ---------- main ----------
def main():
    generated_at = datetime.utcnow().isoformat()

    fixture_ids = load_fixture_ids_from_predicted_xi()
    if not fixture_ids:
        print("[warn] No fixtures found in data/predicted_xi/by_league/*.json")
        # still proceed (nothing to fetch)
    if len(fixture_ids) > MAX_FIXTURES:
        fixture_ids = fixture_ids[:MAX_FIXTURES]

    bet365_id = fetch_bet365_id()
    hdr = [
        f"Generated at (UTC): {generated_at}",
        f"Source: Sportmonks pre-match odds",
        f"Bookmaker: Bet365 (id={bet365_id})",
        f"Fixtures: {len(fixture_ids)}",
        ""
    ]

    out_lines: List[str] = []
    out_lines.extend(hdr)

    for i, fid in enumerate(fixture_ids, 1):
        print(f"[{i}/{len(fixture_ids)}] Fixture {fid}")
        try:
            payload = fetch_fixture_odds_bet365(fid, bet365_id)
        except Exception as e:
            out_lines.append(f"Fixture {fid}")
            out_lines.append(f"  [error] {e}")
            continue
        out_lines.extend(dump_fixture_block(fid, payload))
        out_lines.append("")  # blank line between fixtures

    # Write TXT
    OUT_TXT.write_text("\n".join(out_lines).rstrip() + "\n", encoding="utf-8")
    print(f"[OK] wrote {OUT_TXT}")

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
