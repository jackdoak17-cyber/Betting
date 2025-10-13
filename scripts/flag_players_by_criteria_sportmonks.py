#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Flags players from data/player_filters/players_by_criteria.{json,txt}
if Bet365 (Sportmonks) prices for the exact stat+threshold are >= MIN_PRICE.
Targets:
  - SHOTS 1+  (market_id=268, line ~0.5)
  - SHOTS 2+  (market_id=268, line ~1.5)
  - SOT   1+  (market_id=267, line ~0.5)

Output:
  data/player_filters/criteria_odds_flags.txt

ENV:
  SPORTMONKS_TOKEN=...        # required (Sportmonks v3)
  MIN_PRICE=1.80              # optional
"""

import os, re, json, time, unicodedata
import requests
from pathlib import Path
from itertools import islice

# ================== CONFIG ==================
API_TOKEN = os.getenv("SPORTMONKS_TOKEN", "").strip()
if not API_TOKEN:
    raise SystemExit("Set SPORTMONKS_TOKEN env var.")

MIN_PRICE = float(os.getenv("MIN_PRICE", "1.80"))

# Bet365 = bookmaker_id 2 (from your docs dump)
BET365_ID = 2

# Player markets (from your docs dump)
MARKET_SOT = 267  # Player Shots On Target
MARKET_SHOTS = 268  # Player Shots

# Endpoints (pre-match odds sit under football)
PREMATCH_FX_MARKET = "https://api.sportmonks.com/v3/football/odds/pre-match/fixtures/{fid}/markets/{mid}"

HTTP_HEADERS = {"accept": "application/json"}

FILTERS_JSON = Path("data/player_filters/players_by_criteria.json")
FILTERS_TXT  = Path("data/player_filters/players_by_criteria.txt")
PX_DIR = Path("data/predicted_xi/by_league")
OUT_TXT = Path("data/player_filters/criteria_odds_flags.txt")

# ================== UTILS ==================
def chunked(it, n):
    it = iter(it)
    while True:
        ch = list(islice(it, n))
        if not ch: return
        yield ch

def http_get(url, params, retries=4):
    backoff = 1.0
    for i in range(retries):
        try:
            r = requests.get(url, params=params, headers=HTTP_HEADERS, timeout=25)
            if r.status_code == 200:
                return r.json()
            if r.status_code in (429, 500, 502, 503, 504):
                time.sleep(backoff); backoff *= 1.8; continue
            print(f"[HTTP] {url} -> {r.status_code}: {r.text[:180]}")
            return None
        except requests.exceptions.RequestException as e:
            time.sleep(backoff); backoff *= 1.8
    return None

def strip_accents(s: str) -> str:
    return ''.join(c for c in unicodedata.normalize('NFD', s or '') if unicodedata.category(c) != 'Mn')

def norm(s):
    import re as _re
    s = strip_accents(s or "").lower()
    s = _re.sub(r"[^a-z0-9\s-]", " ", s)
    s = _re.sub(r"\s+", " ", s).strip()
    return s

GENERIC_TEAM_TOKENS = {
    "fc","cf","afc","sc","cd","ud","ac","as","ss","ssc","us","uc","rc","rcd","ca",
    "the","club","de","del","la","las","los","calcio","united","city","saint","st"
}
def team_tokens(name: str):
    toks = set(norm(name).split())
    return {t for t in toks if t not in GENERIC_TEAM_TOKENS}

def team_names_match(a: str, b: str) -> bool:
    if not a or not b: return False
    ta, tb = team_tokens(a), team_tokens(b)
    if not ta or not tb: return False
    if ta == tb or ta.issubset(tb) or tb.issubset(ta): return True
    inter = ta & tb; union = ta | tb
    if len(inter) / max(1, len(union)) >= 0.5: return True
    return len(inter) >= 2

def strip_paren_trail(label: str) -> str:
    import re as _re
    return _re.sub(r"(?:\s*\([^)]*\))+$", "", label or "").strip()

def extract_last_init(name):
    name = strip_accents(name).replace(".", " ").strip()
    parts = [p for p in name.split() if p]
    if not parts: return None, None
    last = norm(parts[-1]); initial = None
    for p in parts[:-1]:
        ch = p.strip()[0:1]
        if ch: initial = ch.lower(); break
    return last, initial

def player_label_matches(target_name, option_label):
    # tolerant: last name must match; if first initial present, enforce
    if not target_name or not option_label: return False
    last, initial = extract_last_init(target_name)
    label = norm(strip_paren_trail(option_label))
    if not last or last not in label: return False
    if initial:
        first_word_initial = label.split()[0][0:1] if label.split() else None
        if first_word_initial and first_word_initial == initial: return True
        import re as _re
        return bool(_re.search(rf"\b{initial}\w*\b.*\b{last}\b", label))
    return True

_label_num_pat   = re.compile(r"\((\d+)\)")         # "Name (2)"
_label_plus_pat  = re.compile(r"\b(\d+)\s*\+\b")    # "Name 2+"
_label_over_pat  = re.compile(r"\bover\s*(\d+(?:\.\d+)?)")  # "Name Over 1.5"

def threshold_from_entry(e: dict):
    label = e.get("label") or e.get("name") or ""
    m=_label_num_pat.search(label)
    if m: return int(m.group(1))
    m=_label_plus_pat.search(label)
    if m: return int(m.group(1))
    m=_label_over_pat.search(label)
    if m:
        try:
            line=float(m.group(1)); return int(round(line+0.5))
        except: pass
    for key in ("handicap","total"):
        v = e.get(key)
        try:
            if v is None: continue
            line=float(v)
            return int(round(line+0.5))
        except: pass
    return None

def price_from_entry(e: dict):
    for k in ("value","dp3"):
        v=e.get(k)
        try:
            if v is None: continue
            return float(v)
        except: pass
    return None

# ================== LOAD TARGETS ==================
def load_targets():
    """
    Return: list of {player, team, stat('shots'|'sot'), threshold(1|2)}
    """
    targets=[]
    if FILTERS_JSON.exists():
        j=json.loads(FILTERS_JSON.read_text(encoding="utf-8"))
        stats=j.get("stats") or {}
        # SHOTS thresholds 1 & 2
        if "shots" in stats:
            tmap=(stats["shots"] or {}).get("thresholds") or {}
            for thr in (1,2):
                buckets = tmap.get(str(thr)) or tmap.get(thr) or {}
                for b in ("100pct_all","90pct_all","100pct_last5","4of5_last5"):
                    for r in buckets.get(b, []):
                        targets.append({"player": r.get("name","").strip(),
                                        "team": r.get("team"),
                                        "stat": "shots",
                                        "threshold": thr})
        # SOT threshold 1
        if "shots_on_target" in stats:
            tmap=(stats["shots_on_target"] or {}).get("thresholds") or {}
            thr=1
            buckets = tmap.get(str(thr)) or tmap.get(thr) or {}
            for b in ("100pct_all","90pct_all","100pct_last5","4of5_last5"):
                for r in buckets.get(b, []):
                    targets.append({"player": r.get("name","").strip(),
                                    "team": r.get("team"),
                                    "stat": "sot",
                                    "threshold": 1})
    elif FILTERS_TXT.exists():
        cur_stat=None; cur_thr=None
        for line in FILTERS_TXT.read_text(encoding="utf-8", errors="ignore").splitlines():
            m_stat = re.match(r"^\s*===== (SHOTS|SHOTS_ON_TARGET) =====\s*$", line, re.I)
            if m_stat:
                cur_stat = "shots" if m_stat.group(1).lower()=="shots" else "sot"
                cur_thr=None; continue
            m_thr = re.match(r"^\s*-- Threshold:\s*(\d+)\+\s*$", line, re.I)
            if m_thr:
                cur_thr=int(m_thr.group(1)); continue
            if cur_stat and cur_thr and line.strip().startswith("•"):
                nm = re.match(r"^\s*•\s*([^()]+)\s*\(([^)]+)\):", line)
                if nm:
                    name = nm.group(1).strip()
                    team = nm.group(2).split(",")[0].strip()
                    if cur_stat=="shots" and cur_thr in (1,2):
                        targets.append({"player": name, "team": team, "stat":"shots", "threshold": cur_thr})
                    if cur_stat=="sot" and cur_thr==1:
                        targets.append({"player": name, "team": team, "stat":"sot", "threshold": 1})
    else:
        raise FileNotFoundError("players_by_criteria.{json,txt} not found.")

    # de-dupe
    seen=set(); uniq=[]
    for t in targets:
        key=(t["player"], t.get("team"), t["stat"], t["threshold"])
        if t["player"] and key not in seen:
            uniq.append(t); seen.add(key)
    return uniq

# ================== FIXTURE MAP FROM predicted_xi ==================
def _load_json(p: Path):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None

def _predicted_fixtures():
    out=[]
    for p in PX_DIR.glob("*.json"):
        blob=_load_json(p) or {}
        for fx in (blob.get("fixtures") or []):
            fid = fx.get("fixture_id") or (fx.get("fixture", {}) or {}).get("id")
            if not fid: continue
            try: fid=int(fid)
            except: continue
            h=(fx.get("home") or {})
            a=(fx.get("away") or {})
            home_name = h.get("name") or h.get("team_name") or ""
            away_name = a.get("name") or a.get("team_name") or ""
            out.append({"fixture_id": fid, "home": home_name, "away": away_name})
    return out

def map_team_to_fixture(fixtures):
    m={}
    for fx in fixtures:
        for nm in (fx["home"], fx["away"]):
            if nm and nm not in m:
                m[nm]=fx
    return m

# ================== SPORTMONKS CALLS ==================
def fetch_by_fixture_market(fid: int, market_id: int):
    url = PREMATCH_FX_MARKET.format(fid=fid, mid=market_id)
    j = http_get(url, params={"api_token": API_TOKEN})
    if not j: return []
    return j.get("data") or []

# ================== MAIN ==================
def main():
    targets = load_targets()
    if not targets:
        print("No targets from players_by_criteria files."); return

    fixtures = _predicted_fixtures()
    team_to_fx = map_team_to_fixture(fixtures)
    print(f"[INFO] predicted_xi fixtures loaded: {len(fixtures)}")

    # bucket targets by fixture id
    buckets={}
    unmatched=[]
    for t in targets:
        team=t.get("team") or ""
        fx=None
        if team in team_to_fx:
            fx=team_to_fx[team]
        else:
            for nm, rec in team_to_fx.items():
                if team_names_match(team, nm):
                    fx=rec; break
        if fx:
            fid=fx["fixture_id"]
            buckets.setdefault(fid, []).append({**t, "home": fx["home"], "away": fx["away"]})
        else:
            unmatched.append(t)

    if unmatched:
        print(f"[WARN] {len(unmatched)} targets not mapped to a fixture via predicted_xi.")

    flagged=[]

    for fid, tlist in buckets.items():
        sot_rows   = [e for e in fetch_by_fixture_market(fid, MARKET_SOT)   if e.get("bookmaker_id")==BET365_ID]
        shots_rows = [e for e in fetch_by_fixture_market(fid, MARKET_SHOTS) if e.get("bookmaker_id")==BET365_ID]

        for t in tlist:
            rows = sot_rows if t["stat"]=="sot" else shots_rows
            best=None
            for e in rows:
                label = e.get("label") or ""
                if not player_label_matches(t["player"], label): 
                    continue
                thr = threshold_from_entry(e)
                if thr is None or thr != t["threshold"]:
                    continue
                price = price_from_entry(e)
                if price is None or price < MIN_PRICE:
                    continue
                if (best is None) or (price > best["price"] + 1e-9):
                    best={"price": price, "market": e.get("market_description",""), "label": label}
            if best:
                flagged.append({
                    "player": t["player"],
                    "team": t.get("team") or "",
                    "stat": t["stat"],
                    "threshold": t["threshold"],
                    "price": best["price"],
                    "market": best["market"] or ("Player Shots On Target" if t["stat"]=="sot" else "Player Shots"),
                    "label": best["label"],
                    "home": t["home"], "away": t["away"],
                })

    # keep best per (player,stat,thr)
    best_map={}
    for r in flagged:
        key=(r["player"], r["stat"], r["threshold"])
        cur=best_map.get(key)
        if (cur is None) or (r["price"] > cur["price"] + 1e-9):
            best_map[key]=r
    rows=list(best_map.values())

    def bucket_key(r):
        if r["stat"]=="sot" and r["threshold"]==1: return 0
        if r["stat"]=="shots" and r["threshold"]==2: return 1
        if r["stat"]=="shots" and r["threshold"]==1: return 2
        return 9
    rows.sort(key=lambda r: (bucket_key(r), -r["price"], r["player"]))

    # render clean text
    lines=[]
    lines.append(f"Generated at (UTC): {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}")
    lines.append(f"Min price: {MIN_PRICE:.2f}  |  Bookmaker: Bet365 (id={BET365_ID})  |  Fixtures: {len(buckets)}")
    lines.append("")

    def group_title(stat, thr):
        if stat=="sot" and thr==1: return "SOT 1+"
        if stat=="shots" and thr==2: return "SHOTS 2+"
        if stat=="shots" and thr==1: return "SHOTS 1+"
        return f"{stat.upper()} {thr}+"

    current=None
    if not rows:
        lines.append("No matches found (no Bet365 prices ≥ threshold for your targets).")
    for r in rows:
        title = group_title(r["stat"], r["threshold"])
        if title != current:
            if current is not None:
                lines.append("")
            lines.append(f"===== {title} — Bet365 — ≥ {MIN_PRICE:.2f} =====")
            header = f"{'Player':22} {'Team':22} {'Fixture':40} {'Price':>7} {'Market':28}"
            lines.append(header); lines.append("-"*len(header))
            current=title
        fixture = f"{r['home']} vs {r['away']}"
        lines.append(f"{r['player'][:22]:22} {r['team'][:22]:22} {fixture[:40]:40} {r['price']:>7.3f} {r['market'][:28]:28}")

    OUT_TXT.parent.mkdir(parents=True, exist_ok=True)
    OUT_TXT.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    print(f"[OK] wrote {OUT_TXT}")

if __name__ == "__main__":
    main()
