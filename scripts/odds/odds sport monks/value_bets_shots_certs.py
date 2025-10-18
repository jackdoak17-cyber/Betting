#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Sportmonks — Value Bets (Player Shots "Certs") v1.1
---------------------------------------------------
- Reads predicted XI to discover target players for upcoming fixtures.
- Pulls Sportmonks pre-match odds per fixture.
- Extracts Over 0.5 (1+) lines for:
    • Player Total Shots
    • Player Shots on Target
- Filters by bookmaker(s) (default: Kambi).
- Writes:
    reports/props_latest.csv
    reports/props_latest.json
    reports/digest_latest.md

ENV
---
SPORTMONKS_TOKEN             (required)
SM_BOOKMAKERS                (default: "Kambi")  e.g. "Kambi,Bet365"
SM_MIN_DECIMAL               (default: "1.20")   minimum decimal price to keep
SM_MAX_FIXTURES              (default: "500")    safety cap
SM_INCLUDE_SOT               (default: "1")      include SOT market
SM_INCLUDE_SHOTS             (default: "1")      include Total Shots market

INPUTS (from your pipeline)
---------------------------
data/predicted_xi/by_league/{league_id}.json
  Structure (only fields used):
    {
      "league_id": ...,
      "fixtures": [
        {
          "fixture_id": 123,
          "starting_at": "...",
          "home": { "team_id": ..., "name": "...", "predicted_xi": [ {"player_id":..., "name":"..."} ] },
          "away": { ... }
        },
        ...
      ]
    }

NOTE
----
Sportmonks odds payloads vary per bookmaker/market. This script uses tolerant parsing:
- market name fuzzy match (e.g., "Player Shots", "Player - Total Shots", etc.)
- outcome detection for Over 0.5 (also catches "1+" / "1 or more")
- participant (player) name pulled from common fields; falls back to label parsing
"""

from __future__ import annotations
import os, json, re, csv, time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
import unicodedata
import requests
from datetime import datetime

# ----------------- Config / IO -----------------
ROOT = Path(".")
PX_DIR = ROOT / "data" / "predicted_xi" / "by_league"
REPORTS_DIR = ROOT / "reports"
REPORTS_DIR.mkdir(parents=True, exist_ok=True)

API_BASE = "https://api.sportmonks.com/v3/football"
TOKEN = (
    os.getenv("SPORTMONKS_TOKEN")
    or os.getenv("SPORTMONKS_API_TOKEN")
    or os.getenv("SM_TOKEN")
)
if not TOKEN:
    raise SystemExit("ERROR: SPORTMONKS_TOKEN / SPORTMONKS_API_TOKEN / SM_TOKEN not set.")

BOOKMAKERS_IN = os.getenv("SM_BOOKMAKERS", "Kambi")
MIN_DEC = float(os.getenv("SM_MIN_DECIMAL", "1.20"))
MAX_FIXTURES = int(os.getenv("SM_MAX_FIXTURES", "500"))
INCLUDE_SOT = os.getenv("SM_INCLUDE_SOT", "1") == "1"
INCLUDE_SHOTS = os.getenv("SM_INCLUDE_SHOTS", "1") == "1"

TIMEOUT = 25
RETRIES = 3
BACKOFF = 1.7
GLOBAL_MIN_DELAY = 0.18  # gentle pacing across calls
_last_call = 0.0

# Market name heuristics
SHOTS_MARKET_HINTS = [
    "player shots", "player - total shots", "total shots - player", "total shots player",
    "shots - player", "shots player", "shots taken - player", "shots (player)",
]
SOT_MARKET_HINTS = [
    "shots on target - player", "player shots on target", "shots on target player",
    "sot - player", "total shots on target - player", "shots on target (player)",
]

OVER05_HINTS = ["over 0.5", "over0.5", "1+", "1 or more", "1 or-more", "1 plus"]

# ----------------- HTTP helpers -----------------
def _pace():
    global _last_call
    now = time.time()
    if now - _last_call < GLOBAL_MIN_DELAY:
        time.sleep(GLOBAL_MIN_DELAY - (now - _last_call))
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
                sleep = min(60, (BACKOFF ** i) * 2)
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

# ----------------- Text / matching helpers -----------------
def norm(s: Optional[str]) -> str:
    if not s: return ""
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = s.lower().strip()
    s = re.sub(r"[^a-z0-9+ ]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s

def looks_like_market(name: str, hints: List[str]) -> bool:
    n = norm(name)
    return any(h in n for h in hints)

def outcome_is_over05(name: str, line: Optional[float] = None, side: Optional[str] = None) -> bool:
    n = norm(name)
    if any(h in n for h in OVER05_HINTS):
        return True
    if line is not None:
        # Some payloads have explicit line/handicap + side
        if abs(line - 0.5) < 1e-9 and (side or "").lower() in {"over", "o", "ov"}:
            return True
    return False

def pick_decimal(out: dict) -> Optional[float]:
    # Common decimal price fields across feeds
    for k in ("decimal", "price", "odd", "odds", "value"):
        v = out.get(k)
        try:
            if v is None: continue
            return float(v)
        except Exception:
            continue
    # Some use nested objects { "decimal": 1.83 } or {"american":...,"decimal":...}
    v = out.get("prices") or out.get("bookmaker_price") or {}
    if isinstance(v, dict):
        for k in ("decimal", "dec", "d"):
            if k in v:
                try: return float(v[k])
                except Exception: pass
    return None

def extract_player_name(outcome: dict) -> str:
    # Try a variety of fields commonly seen
    for k in ("participant", "player", "competitor", "runner_name", "selection", "label", "name", "outcome"):
        v = outcome.get(k)
        if isinstance(v, dict):
            for kk in ("name", "player_name", "participant_name"):
                if v.get(kk):
                    return str(v[kk])
        elif isinstance(v, str) and v.strip():
            # Beware strings like "Over 0.5 - Bukayo Saka" — strip "Over 0.5 - "
            s = v.strip()
            # heuristics to peel line prefix/suffix
            s = re.sub(r"^(over|under)\s*[\d.]+\s*[-:–]\s*", "", s, flags=re.I)
            s = re.sub(r"\b(over|under)\s*[\d.]+\b", "", s, flags=re.I).strip(" -:–")
            if len(s.split()) >= 2:  # don't return just "Over 0.5"
                return s
    # Some markets attach participant in 'description' / 'meta'
    meta = outcome.get("meta") or outcome.get("extra") or {}
    if isinstance(meta, dict):
        for kk in ("participant_name", "player_name", "name"):
            if meta.get(kk):
                return str(meta[kk])
    return ""

def extract_line_info(outcome: dict) -> Tuple[Optional[float], Optional[str]]:
    # (line/handicap, side)
    line = None; side = None
    for k in ("line", "handicap", "goal", "total", "threshold"):
        v = outcome.get(k)
        try:
            if v is not None:
                line = float(v)
                break
        except Exception:
            continue
    # side:
    side = (outcome.get("side") or outcome.get("direction") or outcome.get("bet_type") or "").lower() or None
    return (line, side)

# ----------------- Bookmakers -----------------
def fetch_bookmaker_index() -> Dict[int, str]:
    idx: Dict[int, str] = {}
    try:
        j = api_get("bookmakers")
        for row in j.get("data", []):
            bid = int(row.get("id") or 0)
            nm = row.get("name") or ""
            if bid:
                idx[bid] = nm
    except Exception as e:
        print(f"[warn] bookmakers fetch failed: {e}")
    return idx

def resolve_bookmaker_ids(want_names: List[str], idx: Dict[int, str]) -> List[int]:
    want_norm = [norm(x) for x in want_names if x.strip()]
    out: List[int] = []
    for bid, nm in idx.items():
        n = norm(nm)
        if any(w in n for w in want_norm):
            out.append(bid)
    # Special case: user historically just uses "kambi"
    # If no match but "kambi" was requested, include any bookmaker containing "kambi".
    if not out and any("kambi" == w for w in want_norm):
        for bid, nm in idx.items():
            if "kambi" in norm(nm):
                out.append(bid)
    return sorted(set(out))

# ----------------- Inputs: predicted XI -----------------
def load_targets_from_predicted_xi() -> Dict[int, Dict[str, dict]]:
    """
    Returns:
      targets_by_fixture[fixture_id][norm_player_name] = {
        "player_id": int|None,
        "display_name": str,
        "team_name": str,
        "league_id": int|None,
        "starting_at": str|None
      }
    """
    out: Dict[int, Dict[str, dict]] = {}
    if not PX_DIR.exists():
        return out
    for f in PX_DIR.glob("*.json"):
        try:
            blob = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        lid = int(blob.get("league_id") or 0)
        for fx in (blob.get("fixtures") or []):
            fid = int(fx.get("fixture_id") or fx.get("id") or 0)
            if not fid:
                continue
            start = (fx.get("time") or {}).get("starting_at") or fx.get("starting_at")
            bucket = out.setdefault(fid, {})
            for side_key in ("home", "away"):
                side = fx.get(side_key) or {}
                tname = side.get("name") or ""
                for p in side.get("predicted_xi") or []:
                    nm = p.get("name") or ""
                    pid = p.get("player_id")
                    if not nm: 
                        continue
                    bucket[norm(nm)] = {
                        "player_id": int(pid) if pid else None,
                        "display_name": nm,
                        "team_name": tname,
                        "league_id": lid or None,
                        "starting_at": start,
                    }
    return out

# ----------------- Sportmonks odds per fixture -----------------
def fetch_fixture_odds(fid: int) -> dict:
    # Keep includes minimal; many markets already in base payload.
    path = f"odds/pre-match/fixtures/{fid}"
    return api_get(path, params={})

def iter_player_over05_prices(odds_payload: dict,
                              keep_bookmaker_ids: List[int]) -> List[dict]:
    """
    Returns list of dicts:
      {
        "fixture_id", "bookmaker_id", "bookmaker_name",
        "market_name", "market_kind"("shots"|"sot"),
        "player_name", "outcome_name", "decimal"
      }
    """
    out: List[dict] = []
    data = odds_payload.get("data") or []
    # Payload can be either a list (bookmakers) or list of 'odds' items with nested bookmaker
    for row in data:
        # bookmaker
        bm = row.get("bookmaker") or {}
        bookmaker_id = int(bm.get("id") or row.get("bookmaker_id") or 0)
        bookmaker_name = bm.get("name") or row.get("bookmaker_name") or ""
        if keep_bookmaker_ids and bookmaker_id not in keep_bookmaker_ids:
            continue

        # markets may be under 'markets', or the row itself is a market
        markets = row.get("markets") or row.get("odds") or row.get("children") or []
        if isinstance(markets, dict):
            markets = list(markets.values())
        for m in markets:
            mname = m.get("name") or m.get("market") or m.get("key") or ""
            n = norm(mname)
            market_kind = None
            if INCLUDE_SHOTS and looks_like_market(n, SHOTS_MARKET_HINTS):
                market_kind = "shots"
            elif INCLUDE_SOT and looks_like_market(n, SOT_MARKET_HINTS):
                market_kind = "sot"
            else:
                continue

            # outcomes
            outs = m.get("outcomes") or m.get("selections") or m.get("runners") or []
            if isinstance(outs, dict):
                outs = list(outs.values())
            for o in outs:
                dec = pick_decimal(o)
                if dec is None or dec < MIN_DEC:
                    continue
                line, side = extract_line_info(o)
                oname = o.get("name") or o.get("label") or o.get("bet") or ""
                if not outcome_is_over05(oname, line, side):
                    # Also check if the outcome group has "Over 0.5" in parent label
                    parent_label = m.get("label") or m.get("name") or ""
                    if not outcome_is_over05(parent_label, line, side):
                        continue
                pname = extract_player_name(o)
                if not pname:
                    # sometimes the player is tucked inside selection name like "Over 0.5 - Bukayo Saka"
                    # already trimmed in extract_player_name(), but one more fallback:
                    s = (o.get("name") or o.get("label") or "")
                    m2 = re.search(r"[-:–]\s*([A-Za-z ].+)$", s)
                    if m2:
                        pname = m2.group(1).strip()
                if not pname:
                    continue

                out.append({
                    "bookmaker_id": bookmaker_id,
                    "bookmaker_name": bookmaker_name,
                    "market_name": mname,
                    "market_kind": market_kind,
                    "player_name": pname,
                    "outcome_name": oname,
                    "decimal": float(dec),
                })
    return out

# ----------------- Main -----------------
def main():
    generated_at = datetime.utcnow().isoformat()

    # 1) predicted XI targets
    targets_by_fixture = load_targets_from_predicted_xi()
    all_fixture_ids = list(targets_by_fixture.keys())
    if not all_fixture_ids:
        print("[warn] No predicted XI targets found in data/predicted_xi/by_league/*.json")
    if len(all_fixture_ids) > MAX_FIXTURES:
        all_fixture_ids = all_fixture_ids[:MAX_FIXTURES]

    # 2) bookmakers
    bm_index = fetch_bookmaker_index()
    bm_ids = resolve_bookmaker_ids([x.strip() for x in BOOKMAKERS_IN.split(",")], bm_index)
    bm_names = [bm_index.get(bid, f"Bookmaker {bid}") for bid in bm_ids]
    print(f"Bookmakers filter: {', '.join(bm_names) if bm_ids else '(none — keeping all)'}")

    # 3) Walk fixtures & pull odds
    rows: List[dict] = []
    for i, fid in enumerate(all_fixture_ids, 1):
        print(f"[{i}/{len(all_fixture_ids)}] Fixture {fid}")
        try:
            j = fetch_fixture_odds(fid)
        except Exception as e:
            print(f"  [error] fixture {fid}: {e}")
            continue

        lines = iter_player_over05_prices(j, bm_ids)
        if not lines:
            continue

        # 4) tie to predicted XI player list (name-normalized)
        tgt_index = targets_by_fixture.get(fid, {})
        for ln in lines:
            pname_n = norm(ln["player_name"])
            tgt = tgt_index.get(pname_n)
            if not tgt:
                # allow relaxed match: drop middle names if needed
                # match on last name if unique
                tokens = [t for t in pname_n.split() if len(t) >= 3]
                matched = None
                for k, v in tgt_index.items():
                    if any(tok in k for tok in tokens[-1:]):  # prefer last token
                        matched = v
                        break
                tgt = matched

            row = {
                "fixture_id": fid,
                "starting_at": (tgt or {}).get("starting_at"),
                "league_id": (tgt or {}).get("league_id"),
                "team_name": (tgt or {}).get("team_name"),
                "player_name": ln["player_name"],
                "player_id": (tgt or {}).get("player_id"),
                "market": ln["market_kind"],  # "shots" or "sot"
                "market_name_raw": ln["market_name"],
                "bookmaker": ln["bookmaker_name"],
                "decimal": ln["decimal"],
                "outcome_raw": ln["outcome_name"],
                "matched_predicted_xi": bool(tgt),
            }
            rows.append(row)

    # 5) outputs
    rows.sort(key=lambda r: (r["starting_at"] or "", r["league_id"] or 0, r["team_name"] or "", r["player_name"], r["market"], -r["decimal"]))

    out_json = {
        "generated_at": generated_at,
        "min_decimal": MIN_DEC,
        "bookmakers_requested": BOOKMAKERS_IN,
        "count": len(rows),
        "rows": rows,
    }
    (REPORTS_DIR / "props_latest.json").write_text(json.dumps(out_json, ensure_ascii=False, indent=2), encoding="utf-8")

    # CSV
    csv_path = REPORTS_DIR / "props_latest.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow([
            "starting_at","fixture_id","league_id","team_name","player_name","player_id",
            "market","bookmaker","decimal","market_name_raw","outcome_raw","matched_predicted_xi"
        ])
        for r in rows:
            w.writerow([
                r.get("starting_at"), r.get("fixture_id"), r.get("league_id"), r.get("team_name"),
                r.get("player_name"), r.get("player_id"),
                r.get("market"), r.get("bookmaker"), r.get("decimal"),
                r.get("market_name_raw"), r.get("outcome_raw"), "Y" if r.get("matched_predicted_xi") else "N"
            ])

    # Digest
    md_lines = []
    md_lines.append(f"Generated at (UTC): {generated_at}")
    md_lines.append(f"Min price: {MIN_DEC:.2f}  |  Bookmakers: {BOOKMAKERS_IN}  |  Fixtures: {len(all_fixture_ids)}")
    md_lines.append("")
    if rows:
        # Group by market then bookmaker
        def keygrp(r): return (r["market"], r["bookmaker"])
        from itertools import groupby
        for (mk, bm), grp in groupby(rows, key=keygrp):
            md_lines.append(f"===== {('SOT' if mk=='sot' else 'Total Shots')} — {bm} =====")
            for r in grp:
                team = r.get("team_name") or ""
                kickoff = r.get("starting_at") or ""
                line = f" • {r['player_name']} — {team} | {kickoff} | 1+ @{r['decimal']:.3f}"
                if not r.get("matched_predicted_xi"):
                    line += "  (note: not matched to predicted XI)"
                md_lines.append(line)
            md_lines.append("")
    else:
        md_lines.append("No matches found (no prices ≥ threshold for your targets).")

    (REPORTS_DIR / "digest_latest.md").write_text("\n".join(md_lines).rstrip() + "\n", encoding="utf-8")

    # Console echo
    print("\n".join(md_lines))

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
