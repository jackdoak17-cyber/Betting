#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Sportmonks — Bet365 ALL odds dump (TXT) v2.0
--------------------------------------------
• Reads fixtures from:
    - data/fixtures/*.json
    - data/fixtures/by_league/*.json
• Fetches ALL pre-match odds from **Bet365** for each fixture (no filtering).
• Writes a single human-readable TXT to:
    - data/value_bets/bet365_all_odds.txt

ENV
  SPORTMONKS_TOKEN    (required)
  SM_MAX_FIXTURES     (default "800")  safety cap
  SM_SAVE_RAW         (optional "1")   also save raw JSON per fixture to data/value_bets/raw/

Endpoints used
  - GET /v3/odds/bookmakers
  - GET /v3/football/odds/pre-match/fixtures/{fixture_id}/bookmakers/{bookmaker_id}
    (falls back to /fixtures/{fixture_id} and filters Bet365 rows client-side if needed)

Notes
  - The standard pre-match odds response is commonly **FLAT**: one object per odd
    with fields like market_id, market_description, name/label, value/dp3, total/handicap, etc.
  - This script detects that flat shape and prints every odd; if we ever see a nested shape
    (markets -> outcomes), it falls back to a nested printer.
"""

from __future__ import annotations
import os, json, time, re, unicodedata
from pathlib import Path
from typing import Dict, List, Optional, Any, Tuple
from datetime import datetime
from itertools import groupby
import requests

# ---------- IO ----------
ROOT = Path(".")
FIX_DIR       = ROOT / "data" / "fixtures"
FIX_BYL_DIR   = FIX_DIR / "by_league"
OUT_DIR       = ROOT / "data" / "value_bets"
RAW_DIR       = OUT_DIR / "raw"
OUT_DIR.mkdir(parents=True, exist_ok=True)

OUT_TXT       = OUT_DIR / "bet365_all_odds.txt"

# ---------- API ----------
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
SAVE_RAW     = os.getenv("SM_SAVE_RAW", "0") == "1"

TIMEOUT = 25
RETRIES = 3
BACKOFF = 1.7
GLOBAL_MIN_DELAY = 0.18
_last_call = 0.0

# ---------- HTTP helpers ----------
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

# ---------- utils ----------
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

def _as_float(x):
    try:
        return float(x)
    except Exception:
        return None

# ---------- fixtures ----------
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
            if not fid:
                continue
            if fid in info:
                continue
            league_id = fx.get("league_id")
            starting_at = fx.get("starting_at") or (fx.get("time") or {}).get("starting_at")
            parts = fx.get("participants") or []
            home, away = None, None
            if parts:
                for p in parts:
                    loc = (p.get("meta") or {}).get("location")
                    if loc == "home": home = p.get("name")
                    elif loc == "away": away = p.get("name")
            home = home or fx.get("home_name") or "Home"
            away = away or fx.get("away_name") or "Away"
            info[fid] = {
                "league_id": league_id,
                "starting_at": starting_at,
                "home": home,
                "away": away,
            }
            fixture_ids.append(fid)

    if FIX_DIR.exists():
        for f in FIX_DIR.glob("*.json"):
            if f.name == "latest.json":
                try:
                    blob = json.loads(f.read_text(encoding="utf-8"))
                    for fx in blob.get("fixtures") or []:
                        add_from_blob({"fixtures": [fx]})
                except Exception:
                    pass
                continue
            try:
                blob = json.loads(f.read_text(encoding="utf-8"))
            except Exception:
                continue
            add_from_blob(blob)

    if FIX_BYL_DIR.exists():
        for f in FIX_BYL_DIR.glob("*.json"):
            try:
                blob = json.loads(f.read_text(encoding="utf-8"))
            except Exception:
                continue
            add_from_blob(blob)

    # de-dup keep order
    seen = set()
    uniq = []
    for fid in fixture_ids:
        if fid not in seen:
            seen.add(fid)
            uniq.append(fid)

    return uniq, info

# ---------- bookmaker (Bet365) ----------
def fetch_bet365_id() -> int:
    """
    Resolve Bet365 bookmaker id via /v3/odds/bookmakers. Fallback to 2.
    """
    try:
        j = api_get(API_BASE_ODDS, "bookmakers")
        for row in j.get("data", []) or []:
            nm = (row.get("name") or "").strip()
            if norm(nm) == "bet365" or "bet365" in norm(nm):
                rid = int(row.get("id") or 0)
                return rid if rid else 2
    except Exception as e:
        print(f"[warn] bookmakers fetch failed: {e}")
    return 2

# ---------- odds fetch ----------
def fetch_fixture_odds_bet365(fid: int, bet365_id: int) -> dict:
    """
    Prefer bookmaker-scoped endpoint; fallback to unscoped and filter.
    """
    # try bookmaker-scoped
    try:
        payload = api_get(API_BASE_FOOTBALL, f"odds/pre-match/fixtures/{fid}/bookmakers/{bet365_id}")
        if SAVE_RAW:
            RAW_DIR.mkdir(parents=True, exist_ok=True)
            (RAW_DIR / f"fixture_{fid}_bm{bet365_id}.json").write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        return payload
    except Exception as e:
        print(f"  [fallback] fixture {fid} bookmaker-scoped failed: {e}")

    # fallback: unscoped, filter rows to Bet365
    j = api_get(API_BASE_FOOTBALL, f"odds/pre-match/fixtures/{fid}")
    data = []
    for row in j.get("data", []) or []:
        bm = row.get("bookmaker") or {}
        bid = int(bm.get("id") or row.get("bookmaker_id") or 0)
        bname = (bm.get("name") or row.get("bookmaker_name") or "")
        if bid == bet365_id or "bet365" in norm(bname):
            data.append(row)
    payload = {"data": data}
    if SAVE_RAW:
        RAW_DIR.mkdir(parents=True, exist_ok=True)
        (RAW_DIR / f"fixture_{fid}_filtered_bm{bet365_id}.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    return payload

# ---------- pretty printers ----------
def outcome_price_from_odd(odd: dict) -> str:
    # preferred decimal price field on flat rows is often "dp3", else "value"
    for k in ("dp3", "value", "decimal", "price", "odd", "odds"):
        v = odd.get(k)
        try:
            if v is None:
                continue
            return f"{float(v):.3f}"
        except Exception:
            continue
    return str(odd.get("value") or "?")

def flat_dump(rows: List[dict]) -> List[str]:
    """
    Dump "flat" odds rows. Group by market_id + market_description for readability.
    """
    out: List[str] = []
    # sort for stable grouping
    rows_sorted = sorted(
        rows,
        key=lambda r: (
            int(r.get("market_id") or 0),
            str(r.get("market_description") or ""),
            str(r.get("name") or r.get("label") or ""),
            str(r.get("total") or r.get("handicap") or ""),
            str(r.get("value") or r.get("dp3") or "")
        )
    )

    def keyer(r):
        return (int(r.get("market_id") or 0), r.get("market_description") or "")

    for (mid, mdesc), grp in groupby(rows_sorted, key=keyer):
        head = f"  - Market [{mid}]: {mdesc if mdesc else 'Unknown market'}"
        out.append(head)
        for odd in grp:
            label = odd.get("name") or odd.get("label") or "Odd"
            price = outcome_price_from_odd(odd)
            extras = []
            if odd.get("total") is not None:
                f = _as_float(odd.get("total")); extras.append(f"total={f:.3f}" if f is not None else f"total={odd.get('total')}")
            if odd.get("handicap") is not None:
                f = _as_float(odd.get("handicap")); extras.append(f"handicap={f:.3f}" if f is not None else f"handicap={odd.get('handicap')}")
            if odd.get("stopped") is True:
                extras.append("stopped=true")
            extra_str = f" [{', '.join(extras)}]" if extras else ""

            participants = odd.get("participants")
            part_str = f" — participants={participants}" if participants else ""

            out.append(f"      * {label} @ {price}{extra_str}{part_str}")
    return out

def nested_dump(rows: List[dict]) -> List[str]:
    """
    Fallback printer for nested bookmaker->markets->outcomes shapes (uncommon on standard feed).
    """
    out: List[str] = []
    for row in rows:
        bm = row.get("bookmaker") or {}
        bname = bm.get("name") or row.get("bookmaker_name") or "Bet365"
        out.append(f"  Bookmaker: {bname}")

        markets = coerce_list(row.get("markets") or row.get("odds") or row.get("children") or [])
        if not markets:
            out.append("    (no markets)")
            continue

        for m in markets:
            mid = m.get("id") or m.get("market_id")
            mname = m.get("name") or m.get("market") or m.get("key") or "Market"
            mlabel = m.get("label") or ""
            head = f"  - Market [{mid}]: {mname}" if mid is not None else f"  - Market: {mname}"
            if mlabel:
                head += f" — {mlabel}"
            out.append(head)

            outs = coerce_list(m.get("outcomes") or m.get("selections") or m.get("runners") or [])
            if not outs:
                out.append("      (no outcomes)")
                continue

            for o in outs:
                # try common fields
                name = (o.get("name") or o.get("label") or o.get("selection")
                        or o.get("runner_name") or o.get("outcome") or "Outcome")
                price = None
                for k in ("decimal", "price", "odd", "odds", "value"):
                    v = o.get(k)
                    try:
                        if v is not None:
                            price = f"{float(v):.3f}"
                            break
                    except Exception:
                        pass
                if price is None:
                    price = str(o.get("value") or "?")

                # line/side decorations
                parts = []
                for k in ("line", "handicap", "goal", "total", "threshold"):
                    if o.get(k) is not None:
                        f = _as_float(o.get(k))
                        parts.append(f"{k}={f:.3f}" if f is not None else f"{k}={o.get(k)}")
                s = o.get("side") or o.get("direction") or o.get("bet_type")
                if s:
                    parts.append(f"side={s}")
                extra = f" [{', '.join(parts)}]" if parts else ""

                # participant
                pv = o.get("participant") or o.get("player") or o.get("competitor")
                pstr = ""
                if isinstance(pv, dict):
                    for kk in ("name", "player_name", "participant_name"):
                        if pv.get(kk):
                            pstr = f" — {pv[kk]}"
                            break

                out.append(f"      * {name} @ {price}{extra}{pstr}")
    return out

def dump_fixture_block(fid: int, head: dict, payload: dict) -> List[str]:
    """
    Decide which printer to use and return formatted lines for this fixture.
    """
    lines: List[str] = []
    hn, an = head.get("home") or "Home", head.get("away") or "Away"
    ko = head.get("starting_at") or ""
    lid = head.get("league_id") or ""
    lines.append(f"{hn} vs {an}  |  Fixture {fid}  |  League {lid}  |  {ko}")

    data = payload.get("data") or []
    if not data:
        lines.append("  (no Bet365 odds returned)")
        return lines

    # Detect FLAT shape (one object per odd)
    is_flat = any(("market_id" in r) or ("market_description" in r) for r in data)
    if is_flat:
        lines.extend(flat_dump(data))
        return lines

    # Fallback nested
    rows = data if isinstance(data, list) else [data]
    lines.extend(nested_dump(rows))
    return lines

# ---------- main ----------
def main():
    generated_at = datetime.utcnow().isoformat()

    fids, info = read_fixtures()
    if not fids:
        OUT_TXT.write_text(
            "\n".join([
                f"Generated at (UTC): {generated_at}",
                "Source: Sportmonks pre-match odds",
                "Bookmaker: Bet365 (id=? — no lookup done)",
                "Fixtures: 0",
                "",
                "(no fixtures found under data/fixtures — did fetch_fixtures.py run?)",
                ""
            ]),
            encoding="utf-8"
        )
        print("[warn] No fixtures found in data/fixtures/.")
        print(f"[OK] wrote {OUT_TXT}")
        return

    if len(fids) > MAX_FIXTURES:
        fids = fids[:MAX_FIXTURES]

    bet365_id = fetch_bet365_id()

    header = [
        f"Generated at (UTC): {generated_at}",
        "Source: Sportmonks pre-match odds",
        f"Bookmaker: Bet365 (id={bet365_id})",
        f"Fixtures: {len(fids)}",
        "",
    ]
    out_lines: List[str] = []
    out_lines.extend(header)

    for i, fid in enumerate(fids, 1):
        print(f"[{i}/{len(fids)}] Fixture {fid}")
        try:
            payload = fetch_fixture_odds_bet365(fid, bet365_id)
        except Exception as e:
            out_lines.append(f"{info.get(fid, {}).get('home','Home')} vs {info.get(fid, {}).get('away','Away')}  |  Fixture {fid}")
            out_lines.append(f"  [error] {e}")
            out_lines.append("")
            continue
        out_lines.extend(dump_fixture_block(fid, info.get(fid, {}), payload))
        out_lines.append("")

    OUT_TXT.write_text("\n".join(out_lines).rstrip() + "\n", encoding="utf-8")
    print(f"[OK] wrote {OUT_TXT}")

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
