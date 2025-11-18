#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
H2H cache & league bundle — Sportmonks v3

What it does
------------
1) Reads upcoming fixtures from: data/fixtures/by_league/{league_id}.json
   (expects participants with meta.location "home"/"away")
2) For each unique pair, fetches last N H2H fixtures using v3:
      /v3/football/fixtures/head-to-head/{A}/{B}
   Includes fallbacks so 'statistics.type' / 'events.type' 404s don’t kill it.
3) Builds per-pair caches: data/h2h/{lo}_{hi}.json
   - Stores lastN_meta (date + FT home/away goals)
   - Stores sequences per team (goals, shots, sot, corners, fouls, offsides, yellow, red, poss)
   - **Never writes an empty cache** if the API returned 0 rows.
4) Builds per-league JSON bundles: data/h2h/by_league/{league_id}.json
   - One object per upcoming fixture with `vectors.home` and `vectors.away`
   - Uses the sequences from the pair cache
5) Writes a human summary to data/h2h/summary.txt (optional quick check)

Env
---
SPORTMONKS_TOKEN (required)
H2H_MATCHES        default 5
H2H_CACHE_HOURS    default 24 (don’t refetch fresh caches)
H2H_OVERWRITE_EMPTY default 0 (ignore empty fetches if a cache exists)
SM_SLEEP           default 0.05
SM_TIMEOUT         default 20
LEAGUE_IDS         optional "8,82,..." to limit which by_league files are read
"""

from __future__ import annotations
import os, sys, re, json, time, datetime as dt
from pathlib import Path
from typing import List, Tuple, Dict, Optional, Any
import requests

# ---------- Config ----------
API_BASE = "https://api.sportmonks.com/v3"
SPORT = "football"

TOKEN = (
    os.getenv("SPORTMONKS_TOKEN")
    or os.getenv("SPORTMONKS_API_TOKEN")
    or os.getenv("SM_TOKEN")
)
if not TOKEN:
    print("ERROR: SPORTMONKS_TOKEN not set.", file=sys.stderr)
    sys.exit(1)

H2H_MATCHES       = int(os.getenv("H2H_MATCHES", "5"))
CACHE_HOURS       = int(os.getenv("H2H_CACHE_HOURS", "24"))
SM_SLEEP          = float(os.getenv("SM_SLEEP", "0.05"))
SM_TIMEOUT        = int(os.getenv("SM_TIMEOUT", "20"))
OVERWRITE_EMPTY   = os.getenv("H2H_OVERWRITE_EMPTY", "0") == "1"

ROOT      = Path(".")
FIX_DIR   = ROOT / "data" / "fixtures" / "by_league"   # <-- per your repo
PAIR_DIR  = ROOT / "data" / "h2h"
LG_DIR    = PAIR_DIR / "by_league"
PAIR_DIR.mkdir(parents=True, exist_ok=True)
LG_DIR.mkdir(parents=True, exist_ok=True)
OUT_SUM_TXT = PAIR_DIR / "summary.txt"

# ---------- Utils ----------
def now_utc_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")

def to_int(x) -> Optional[int]:
    try:
        return int(x)
    except Exception:
        try:
            s = str(x).strip().rstrip("%")
            if not s:
                return None
            return int(round(float(s)))
        except Exception:
            return None

def nrm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()

def write_json(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, separators=(",", ":"), sort_keys=True), encoding="utf-8")
    tmp.replace(path)

def load_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}

def get_list_or_data(obj: dict, key: str):
    v = obj.get(key)
    if isinstance(v, list): return v
    if isinstance(v, dict) and "data" in v and isinstance(v["data"], list): return v["data"]
    return []

def is_final_score_desc(desc: str) -> bool:
    d = (desc or "").upper()
    return d in {"CURRENT", "FT", "FULLTIME", "SECOND_HALF", "2ND_HALF"}

def cache_path_for_pair(a: int, b: int) -> Path:
    lo, hi = (a, b) if a <= b else (b, a)
    return PAIR_DIR / f"{lo}_{hi}.json"

def cache_fresh(p: Path) -> bool:
    if not p.exists(): return False
    try:
        j = json.loads(p.read_text(encoding="utf-8"))
        ts = j.get("fetched_at")
        if not ts: return False
        t = dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return (dt.datetime.now(dt.timezone.utc) - t) < dt.timedelta(hours=CACHE_HOURS)
    except Exception:
        return False

# ---------- Fixture discovery ----------
def discover_league_ids() -> List[int]:
    out = []
    for p in FIX_DIR.glob("*.json"):
        try: out.append(int(p.stem))
        except Exception: pass
    return sorted(set(out))

def extract_fixture_home_away(fx: dict) -> Tuple[Optional[int], Optional[str], Optional[int], Optional[str]]:
    home_id = away_id = None
    home_name = away_name = None
    parts = fx.get("participants") or []
    for p in parts:
        try:
            pid = int(p.get("id"))
        except Exception:
            continue
        loc = ((p.get("meta") or {}).get("location") or "").lower()
        if loc == "home":
            home_id = pid; home_name = p.get("name")
        elif loc == "away":
            away_id = pid; away_name = p.get("name")
    return home_id, home_name, away_id, away_name

# ---------- Stats parsing (identical logic to your local working script) ----------
ALIASES = {
    # explicit total shots
    "total shots": "shots_total", "shots total": "shots_total", "shots": "shots_total",
    "total shots (incl blocks)": "shots_total", "shots (total)": "shots_total",
    # on target / off target
    "shots on target": "sot", "on target": "sot", "shots on": "sot", "shots on goal": "sot",
    "shots off target": "soff", "off target": "soff",
    # blocked + attempts
    "blocked shots": "sblk", "shots blocked": "sblk", "blocked": "sblk",
    "goal attempts": "attempts", "attempts": "attempts", "attempts on goal": "attempts",
    "attempts at goal": "attempts", "shots attempts": "attempts", "shots attempted": "attempts",
    # others
    "corners": "corners", "corner": "corners", "corner kicks": "corners",
    "fouls": "fouls",
    "offsides": "offsides", "offside": "offsides",
    "possession": "poss", "ball possession": "poss", "ball possession %": "poss", "possession %": "poss",
    # sometimes in statistics
    "yellow cards": "yellow", "yellow": "yellow",
    "red cards": "red", "red": "red",
}

def extract_numeric_from_statdata(d: Any) -> Optional[int]:
    if not isinstance(d, dict):
        return to_int(d)
    for k in ("value", "total", "count", "number"):
        if k in d:
            v = to_int(d.get(k))
            if v is not None: return v
    for v in d.values():
        got = to_int(v)
        if got is not None: return got
    return None

def extract_stats_for_team(stats: List[dict], tid: int) -> Dict[str, Optional[int]]:
    out: Dict[str, Optional[int]] = {}
    for row in stats:
        try:
            if int(row.get("participant_id") or -1) != tid:
                continue
        except Exception:
            continue
        keyname = nrm((row.get("type") or {}).get("name"))
        normkey = ALIASES.get(keyname)
        if not normkey:
            continue
        val = extract_numeric_from_statdata(row.get("data") or {})
        if val is not None:
            out[normkey] = int(val)

    # Robust total shots synthesis (never equate attempts==shots)
    shots_total = out.get("shots_total")
    sot  = out.get("sot")
    soff = out.get("soff")
    sblk = out.get("sblk")
    att  = out.get("attempts")

    if shots_total is not None:
        out["shots"] = shots_total
    elif (sot is not None) or (soff is not None) or (sblk is not None):
        out["shots"] = (sot or 0) + (soff or 0) + (sblk or 0)
    elif (att is not None) and (sblk is not None):
        out["shots"] = att + sblk
    elif att is not None:
        out["shots"] = att
    elif (sot is not None) or (soff is not None):
        out["shots"] = (sot or 0) + (soff or 0)

    if "poss" in out:
        out["poss"] = to_int(out["poss"])
    return out

def extract_ft_goals(scores: List[dict], tid: int) -> Optional[int]:
    val = None
    for s in scores:
        if not is_final_score_desc(s.get("description")):
            continue
        try:
            if int(s.get("participant_id") or -1) != tid:
                continue
        except Exception:
            continue
        g = to_int((s.get("score") or {}).get("goals"))
        if g is not None:
            val = g
    return val

def count_cards_from_events(events: List[dict], tid: int) -> Tuple[int, int]:
    y_count = 0
    r_count = 0
    for ev in events:
        try:
            if int(ev.get("participant_id") or -1) != tid:
                continue
        except Exception:
            continue
        tname = nrm((ev.get("type") or {}).get("name"))
        if tname in {"yellowcard", "yellow card"}:
            y_count += 1
        elif tname in {"redcard", "red card"}:
            r_count += 1
        elif tname in {"yellowredcard", "yellow red card", "second yellow"}:
            r_count += 1
    return y_count, r_count

# ---------- API ----------
def _fetch_h2h_once(a: int, b: int, want: int, include: str) -> List[dict]:
    url = f"{API_BASE}/{SPORT}/fixtures/head-to-head/{a}/{b}"
    params = {
        "api_token": TOKEN,
        "include": include,
        "per_page": 30,
        "sort": "-starting_at",
        "page": 1,
    }
    out = []
    while True:
        r = requests.get(url, params=params, timeout=SM_TIMEOUT)
        if r.status_code == 404 and "include" in (r.text or "").lower():
            return []
        if r.status_code != 200:
            return []
        j = r.json(); rows = j.get("data") or []
        out.extend(rows)
        if len(out) >= want: break

        meta = j.get("meta") or {}
        last_page = meta.get("last_page")
        has_more = (meta.get("pagination") or {}).get("has_more", None)
        if last_page:
            try: lp = int(last_page)
            except: lp = params["page"]
            if params["page"] >= lp: break
        elif has_more is not None:
            if not has_more: break
        else:
            if len(rows) < params["per_page"]: break
        params["page"] += 1
        time.sleep(SM_SLEEP)
    return out

def fetch_h2h(a: int, b: int, want: int) -> List[dict]:
    include_attempts = [
        "participants;scores;statistics.type;events.type",
        "participants;scores;statistics;events.type",
        "participants;scores;statistics;events",
        "participants;scores"
    ]
    collected: List[dict] = []
    for x,y in ((a,b),(b,a)):  # both orders
        got = []
        for inc in include_attempts:
            got = _fetch_h2h_once(x, y, want, inc)
            if got:
                break
        collected.extend(got)
        if len(collected) >= want:
            break

    seen = set()
    dedup: List[dict] = []
    for it in collected:
        fid = it.get("id")
        if fid in seen: continue
        seen.add(fid); dedup.append(it)

    dedup.sort(key=lambda x: (x.get("starting_at") or ""), reverse=True)
    return dedup[:want]

# ---------- Pair cache build ----------
def build_pair_cache(a: int, b: int, lastN: int) -> Optional[dict]:
    """Fetch and write pair cache. Returns payload if written, None if skipped."""
    items = fetch_h2h(a, b, lastN)

    # Never create/overwrite a cache with empty results
    if not items:
        # If an old cache exists and OVERWRITE_EMPTY=0, keep it; otherwise do nothing
        if cache_path_for_pair(a,b).exists() and not OVERWRITE_EMPTY:
            return None
        else:
            return None  # do not write empty files

    seqs: Dict[int, Dict[str, List[Optional[int]]]] = {
        a: {"goals":[], "shots":[], "sot":[], "corners":[], "fouls":[], "offsides":[], "yellow":[], "red":[], "poss":[]},
        b: {"goals":[], "shots":[], "sot":[], "corners":[], "fouls":[], "offsides":[], "yellow":[], "red":[], "poss":[]},
    }
    meta_list: List[dict] = []

    for it in items:
        start = it.get("starting_at")

        parts  = get_list_or_data(it, "participants")
        scores = get_list_or_data(it, "scores")
        stats  = get_list_or_data(it, "statistics")
        events = get_list_or_data(it, "events")

        # Identify fixture's home/away (for meta only)
        hid = aid = None
        for p in parts:
            try:
                pid = int(p.get("id"))
            except Exception:
                continue
            loc = ((p.get("meta") or {}).get("location") or "").lower()
            if loc == "home": hid = pid
            elif loc == "away": aid = pid

        Ag = extract_ft_goals(scores, a)
        Bg = extract_ft_goals(scores, b)

        Astat = extract_stats_for_team(stats, a)
        Bstat = extract_stats_for_team(stats, b)

        Ay, Ar = count_cards_from_events(events, a)
        By, Br = count_cards_from_events(events, b)

        if Ay == 0 and Ar == 0 and (Astat.get("yellow") or Astat.get("red")):
            Ay = int(Astat.get("yellow") or 0); Ar = int(Astat.get("red") or 0)
        if By == 0 and Br == 0 and (Bstat.get("yellow") or Bstat.get("red")):
            By = int(Bstat.get("yellow") or 0); Br = int(Bstat.get("red") or 0)

        for tid, g, st, y, r in [
            (a, Ag, Astat, Ay, Ar),
            (b, Bg, Bstat, By, Br),
        ]:
            seqs[tid]["goals"].append( None if g is None else int(g) )
            seqs[tid]["shots"].append( st.get("shots") )
            seqs[tid]["sot"].append(   st.get("sot") )
            seqs[tid]["corners"].append(st.get("corners"))
            seqs[tid]["fouls"].append( st.get("fouls") )
            seqs[tid]["offsides"].append(st.get("offsides"))
            seqs[tid]["yellow"].append( y )
            seqs[tid]["red"].append(    r )
            seqs[tid]["poss"].append(   st.get("poss") )

        meta_list.append({
            "starting_at": start,
            "home_goals": extract_ft_goals(scores, hid) if isinstance(hid, int) else None,
            "away_goals": extract_ft_goals(scores, aid) if isinstance(aid, int) else None
        })

    lo, hi = (a, b) if a <= b else (b, a)
    payload = {
        "pair": [a, b],
        "sorted_key": f"{lo}_{hi}",
        "fetched_at": now_utc_iso(),
        "lastN": lastN,
        "lastN_meta": meta_list,
        "teams": {
            str(a): seqs[a],
            str(b): seqs[b],
        }
    }
    write_json(cache_path_for_pair(a, b), payload)
    return payload

# ---------- League bundle build ----------
def seq_for_team(cache: dict, tid: int, key: str) -> List[Optional[int]]:
    return list((cache.get("teams") or {}).get(str(tid), {}).get(key) or [])

def build_league_bundle(lid: int, fx_blob: dict) -> dict:
    fixtures = []
    for fx in (fx_blob.get("fixtures") or []):
        hid, hname, aid, aname = extract_fixture_home_away(fx)
        if not (isinstance(hid, int) and isinstance(aid, int)):
            continue

        pairp = cache_path_for_pair(hid, aid)
        cache_present = pairp.exists()
        cache = load_json(pairp) if cache_present else {}

        home_vec = {
            "goals":      seq_for_team(cache, hid, "goals"),
            "shots":      seq_for_team(cache, hid, "shots"),
            "sot":        seq_for_team(cache, hid, "sot"),
            "corners":    seq_for_team(cache, hid, "corners"),
            "fouls":      seq_for_team(cache, hid, "fouls"),
            "offsides":   seq_for_team(cache, hid, "offsides"),
            "yellow":     seq_for_team(cache, hid, "yellow"),
            "red":        seq_for_team(cache, hid, "red"),
            "possession": seq_for_team(cache, hid, "poss"),
        }
        away_vec = {
            "goals":      seq_for_team(cache, aid, "goals"),
            "shots":      seq_for_team(cache, aid, "shots"),
            "sot":        seq_for_team(cache, aid, "sot"),
            "corners":    seq_for_team(cache, aid, "corners"),
            "fouls":      seq_for_team(cache, aid, "fouls"),
            "offsides":   seq_for_team(cache, aid, "offsides"),
            "yellow":     seq_for_team(cache, aid, "yellow"),
            "red":        seq_for_team(cache, aid, "red"),
            "possession": seq_for_team(cache, aid, "poss"),
        }

        fixtures.append({
            "fixture_id": fx.get("id") or fx.get("fixture_id"),
            "starting_at": fx.get("starting_at"),
            "home_id": hid, "home_name": hname,
            "away_id": aid, "away_name": aname,
            "pair_key": f"{min(hid,aid)}_{max(hid,aid)}",
            "fetched_at": (cache.get("fetched_at") or None),
            "lastN_meta": cache.get("lastN_meta") or [],
            "vectors": {"home": home_vec, "away": away_vec},
            "cache_present": bool(cache_present),
        })

    out = {
        "generated_at": now_utc_iso(),
        "league_id": lid,
        "count": len(fixtures),
        "fixtures": fixtures,
    }
    write_json(LG_DIR / f"{lid}.json", out)
    return out

# ---------- Summary (TXT) ----------
def _seq_str(seq: List[Optional[int]]) -> str:
    return ",".join("—" if v is None else str(int(v)) for v in (seq or []))

def _pairwise_sum(a: List[Optional[int]], b: List[Optional[int]]) -> List[Optional[int]]:
    n = min(len(a or []), len(b or []))
    out: List[Optional[int]] = []
    for i in range(n):
        if a[i] is None or b[i] is None:
            out.append(None)
        else:
            out.append(int(a[i]) + int(b[i]))
    return out

def build_summary_from_bundles() -> str:
    bundles = []
    for p in sorted(LG_DIR.glob("*.json")):
        try:
            bundles.append(json.loads(p.read_text(encoding="utf-8")))
        except Exception:
            pass

    if not bundles:
        msg = "No league bundles found in data/h2h/by_league/"
        OUT_SUM_TXT.write_text(msg + "\n", encoding="utf-8")
        return msg

    lines: List[str] = [f"Generated at (UTC): {now_utc_iso()}\n"]
    for bundle in bundles:
        lid = bundle.get("league_id")
        fixtures = bundle.get("fixtures") or []
        if not fixtures:
            continue

        lines.append(f"===== League {lid} =====")
        for fx in fixtures:
            hn = fx.get("home_name") or f"H{fx.get('home_id')}"
            an = fx.get("away_name") or f"A{fx.get('away_id')}"
            fid = fx.get("fixture_id")
            vecH = (fx.get("vectors") or {}).get("home") or {}
            vecA = (fx.get("vectors") or {}).get("away") or {}

            lines.append(f"{hn} vs {an} — last {max(len(vecH.get('goals') or []), len(vecA.get('goals') or []))} H2H (fixture {fid})")
            meta = fx.get("lastN_meta") or []
            for i, row in enumerate(meta, start=1):
                d = (row.get("starting_at") or "")[:10]
                hg = row.get("home_goals"); ag = row.get("away_goals")
                lines.append(f" {i}) {d} | {hn} {hg}–{ag} {an}")
            if meta: lines.append("")

            Hg = vecH.get("goals") or []
            Ag = vecA.get("goals") or []
            lines.append(f"{hn} Goals = {_seq_str(Hg)}")
            lines.append(f"{an} Goals = {_seq_str(Ag)}")
            lines.append(f"Total Goals = {_seq_str(_pairwise_sum(Hg, Ag))}")

            def dump(label, key, pretty=None):
                Hs = vecH.get(key) or []
                As = vecA.get(key) or []
                lab = (pretty or label)
                lines.append(f"{hn} {lab} = {_seq_str(Hs)}")
                lines.append(f"{an} {lab} = {_seq_str(As)}")

            dump("shots", "shots", "Shots")
            dump("sot",   "sot",   "SOT")
            dump("corners","corners","Corners")
            dump("fouls","fouls","Fouls")
            dump("offsides","offsides","Offsides")
            dump("yellow","yellow","Yellow")
            dump("red","red","Red")
            dump("possession","possession","Possession%")
            lines.append("")
        lines.append("")

    OUT_SUM_TXT.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return f"Wrote: {OUT_SUM_TXT}"

# ---------- Main ----------
def main():
    # leagues to process
    if os.getenv("LEAGUE_IDS"):
        league_ids = [int(x) for x in os.getenv("LEAGUE_IDS").split(",") if x.strip()]
    else:
        league_ids = discover_league_ids()
    if not league_ids:
        print("No leagues discovered under data/fixtures/by_league/*.json", file=sys.stderr)
        sys.exit(1)

    # read fixtures & pairs
    pairs: List[Tuple[int,int]] = []
    fixtures_by_league: Dict[int, dict] = {}
    for lid in league_ids:
        blob = load_json(FIX_DIR / f"{lid}.json")
        fixtures_by_league[lid] = blob
        for fx in (blob.get("fixtures") or []):
            hid, _, aid, _ = extract_fixture_home_away(fx)
            if isinstance(hid, int) and isinstance(aid, int):
                pairs.append((hid, aid))

    # unique pairs (order-insensitive)
    seen = set()
    uniq_pairs: List[Tuple[int,int]] = []
    for a, b in pairs:
        key = (a, b) if a < b else (b, a)
        if key not in seen:
            seen.add(key)
            uniq_pairs.append((a, b))

    # update caches (respect TTL; never write empty)
    updated = 0
    for a, b in uniq_pairs:
        p = cache_path_for_pair(a, b)
        if cache_fresh(p):
            continue
        res = build_pair_cache(a, b, H2H_MATCHES)
        if res is not None:
            updated += 1
        time.sleep(SM_SLEEP)

    print(f"Pairs discovered: {len(uniq_pairs)}")
    print(f"Updated caches: {updated}/{len(uniq_pairs)} (TTL={CACHE_HOURS}h, N={H2H_MATCHES})")

    # build league bundles
    count = 0
    for lid in league_ids:
        build_league_bundle(lid, fixtures_by_league[lid])
        count += 1
    print(f"Wrote league bundles: {count} -> data/h2h/by_league/<league_id>.json")

    # summary
    msg = build_summary_from_bundles()
    print(msg)

if __name__ == "__main__":
    main()
