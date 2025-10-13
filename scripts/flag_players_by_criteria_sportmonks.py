#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Flag players (from data/player_filters/players_by_criteria.{json,txt})
when **Bet365** (Sportmonks) prices for the **specific stat+threshold** are >= MIN_PRICE.

Targets:
  - SHOTS 1+ (line 0.5)
  - SHOTS 2+ (line 1.5)
  - SOT   1+ (line 0.5)

Output:
  data/player_filters/criteria_odds_flags.txt

ENV:
  SPORTMONKS_TOKEN=...        # required (Sportmonks v3 uses ?api_token=...)
  MIN_PRICE=1.80              # optional
  BET365_BOOKMAKER_ID=2       # optional override
"""

import os, re, json, time, math, random, unicodedata
import requests
from pathlib import Path
from itertools import islice

# ================== CONFIG ==================
API_TOKEN = os.getenv("SPORTMONKS_TOKEN", "").strip()
if not API_TOKEN:
    raise SystemExit("Set SPORTMONKS_TOKEN env var.")

MIN_PRICE = float(os.getenv("MIN_PRICE", "1.80"))
BET365_ID = int(os.getenv("BET365_BOOKMAKER_ID", "2"))  # Bet365 is usually id=2

# Sportmonks pre-match odds, filtered by fixture + bookmaker
# e.g. /v3/football/odds/pre-match/fixtures/{fixture_id}/bookmakers/2?api_token=...
PREMATCH_FX_BM = "https://api.sportmonks.com/v3/football/odds/pre-match/fixtures/{fid}/bookmakers/{bm}"

# Read targets from your selector output
FILTERS_JSON = Path("data/player_filters/players_by_criteria.json")
FILTERS_TXT  = Path("data/player_filters/players_by_criteria.txt")

# Read upcoming fixtures from your predicted_xi files (we only need team ↔ fixture mapping)
PX_DIR = Path("data/predicted_xi/by_league")  # these files list fixtures with home/away team names/ids
OUT_TXT = Path("data/player_filters/criteria_odds_flags.txt")

HTTP_HEADERS = {"accept": "application/json"}

# ================== UTILS (same spirit as your existing odds scripts) ==================
def chunked(it, n):
    it = iter(it)
    while True:
        ch = list(islice(it, n))
        if not ch: return
        yield ch

def http_get_with_retries(url, params=None, max_retries=5, base_sleep=1.0, factor=1.8):
    attempt = 0; last_text = ""
    while attempt < max_retries:
        try:
            r = requests.get(url, params=params, headers=HTTP_HEADERS, timeout=25)
            if r.status_code == 200:
                return r
            if r.status_code in (429,500,502,503,504):
                last_text = r.text
                sleep = base_sleep*(factor**attempt)+random.uniform(0,0.4)
                print(f"[RETRY] {url} status {r.status_code}. Sleep {sleep:.1f}s")
                time.sleep(sleep); attempt += 1; continue
            print(f"[ERROR] {url} -> {r.status_code}: {r.text[:180]}"); return None
        except requests.exceptions.RequestException as e:
            sleep = base_sleep*(factor**attempt)+random.uniform(0,0.4)
            print(f"[NET] {url} exception: {e}. Sleep {sleep:.1f}s")
            time.sleep(sleep); attempt += 1
    if last_text:
        print(f"[ERROR] Exhausted retries for {url}. Last: {last_text[:200]}")
    else:
        print(f"[ERROR] Exhausted retries for {url}.")
    return None

def strip_accents(s: str) -> str:
    return ''.join(c for c in unicodedata.normalize('NFD', s or '') if unicodedata.category(c) != 'Mn')

def norm(s):
    s = strip_accents(s or "").lower()
    s = re.sub(r"[^a-z0-9\s-]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s

GENERIC_TEAM_TOKENS = {
    "fc","cf","afc","sc","cd","ud","ac","as","ss","ssc","us","uc","rc","rcd","ca",
    "the","club","de","del","la","las","los","calcio","united","city","saint","st"
}
def team_tokens(name: str):
    toks = set(norm(name).split())
    return {t for t in toks if t not in GENERIC_TEAM_TOKENS}

def team_names_match(a: str, b: str) -> bool:
    # mirrors your helper used across the project
    if not a or not b: return False
    ta, tb = team_tokens(a), team_tokens(b)
    if not ta or not tb: return False
    if ta == tb: return True
    if ta.issubset(tb) or tb.issubset(ta): return True
    inter = ta & tb; union = ta | tb
    if len(inter) / max(1, len(union)) >= 0.5: return True
    if len(inter) >= 2: return True
    return False

def strip_paren_trail(label: str) -> str:
    return re.sub(r"(?:\s*\([^)]*\))+$", "", label or "").strip()

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
    # last-name + initial tolerant match (works with labels like "J. Alvarez (2)" or "Alvarez Over 1.5")
    if not target_name or not option_label: return False
    last, initial = extract_last_init(target_name)
    label = norm(strip_paren_trail(option_label))
    if not last or last not in label: return False
    if initial:
        first_word_initial = label.split()[0][0:1] if label.split() else None
        if first_word_initial and first_word_initial == initial: return True
        return bool(re.search(rf"\b{initial}\w*\b.*\b{last}\b", label))
    return True

# ================== LOAD TARGETS (your selector outputs) ==================
def load_targets():
    """
    Returns list of dicts:
      { 'player': str, 'team': Optional[str], 'stat': 'shots'|'sot', 'threshold': int }
    We only keep: shots thr in {1,2}; sot thr in {1}.
    """
    targets = []
    if FILTERS_JSON.exists():
        j = json.loads(FILTERS_JSON.read_text(encoding="utf-8"))
        stats = j.get("stats") or {}
        # SHOTS
        if "shots" in stats:
            tmap = (stats["shots"] or {}).get("thresholds") or {}
            for thr in (1,2):
                buckets = tmap.get(str(thr)) or tmap.get(thr) or {}
                for b in ("100pct_all","90pct_all","100pct_last5","4of5_last5"):
                    for r in buckets.get(b, []):
                        targets.append({"player": r.get("name","").strip(),
                                        "team": r.get("team"),
                                        "stat": "shots",
                                        "threshold": thr})
        # SOT
        if "shots_on_target" in stats:
            tmap = (stats["shots_on_target"] or {}).get("thresholds") or {}
            thr = 1
            buckets = tmap.get(str(thr)) or tmap.get(thr) or {}
            for b in ("100pct_all","90pct_all","100pct_last5","4of5_last5"):
                for r in buckets.get(b, []):
                    targets.append({"player": r.get("name","").strip(),
                                    "team": r.get("team"),
                                    "stat": "sot",
                                    "threshold": 1})
    elif FILTERS_TXT.exists():
        # TXT fallback: parses the clear blocks your script writes
        cur_stat = None; cur_thr = None
        for line in FILTERS_TXT.read_text(encoding="utf-8", errors="ignore").splitlines():
            m_stat = re.match(r"^\s*===== (SHOTS|SHOTS_ON_TARGET) =====\s*$", line, re.I)
            if m_stat:
                cur_stat = "shots" if m_stat.group(1).lower()=="shots" else "sot"
                cur_thr = None; continue
            m_thr = re.match(r"^\s*-- Threshold:\s*(\d+)\+\s*$", line, re.I)
            if m_thr:
                cur_thr = int(m_thr.group(1)); continue
            if cur_stat and cur_thr and line.strip().startswith("•"):
                nm = re.match(r"^\s*•\s*([^()]+)\s*\(([^)]+)\):", line)
                if nm:
                    name = nm.group(1).strip(); team = nm.group(2).split(",")[0].strip()
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

# ================== UPCOMING FIXTURES FROM predicted_xi ==================
def _load_json(p: Path):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None

def _predicted_fixtures():
    """
    Returns list of {fixture_id, home_name, away_name}
    pulled from data/predicted_xi/by_league/*.json
    """
    out=[]
    for p in PX_DIR.glob("*.json"):
        blob=_load_json(p) or {}
        for fx in (blob.get("fixtures") or []):
            h = (fx.get("home") or {})
            a = (fx.get("away") or {})
            fid = fx.get("fixture_id") or fx.get("id") or fx.get("fixtureId") or fx.get("fixtureID")
            # some of your pred_xi JSONs embed the whole fixture; handle both
            if not fid and isinstance(fx.get("fixture"), dict):
                fid = fx["fixture"].get("id")
            if not fid: continue
            try: fid=int(fid)
            except: continue
            home_name = h.get("name") or h.get("team_name") or ""
            away_name = a.get("name") or a.get("team_name") or ""
            if home_name or away_name:
                out.append({"fixture_id": fid, "home": home_name, "away": away_name})
    return out

def map_team_to_fixture(fixtures):
    """
    Map team name -> a fixture record {fixture_id, home, away}
    If a team appears in multiple fixtures, we keep the first (predicted_xi usually lists the next matchday).
    """
    m={}
    for fx in fixtures:
        for nm in (fx["home"], fx["away"]):
            if nm and nm not in m:
                m[nm]=fx
    return m

# ================== SPORTMONKS: fetch odds for (fixture, bet365) ==================
def fetch_bet365_prematch_for_fixture(fid: int):
    url = PREMATCH_FX_BM.format(fid=fid, bm=BET365_ID)
    r = http_get_with_retries(url, params={"api_token": API_TOKEN})
    if not r: return []
    try:
        j = r.json()
    except Exception:
        return []
    return j.get("data") or []

# ================== MARKET FILTERS / PRICE PARSING ==================
def is_player_shots_market(desc: str) -> bool:
    s = (desc or "").lower()
    return "player" in s and "shot" in s and "on target" not in s

def is_player_sot_market(desc: str) -> bool:
    s = (desc or "").lower()
    return ("on target" in s) or ("sot" in s)

_label_num_pat = re.compile(r"\((\d+)\)")       # "Name (2)"
_label_plus_pat = re.compile(r"\b(\d+)\s*\+\b") # "Name 2+"
_label_over_pat = re.compile(r"\bover\s*(\d+(?:\.\d+)?)")  # "Name Over 1.5"

def threshold_from_entry(e: dict) -> int|None:
    """
    Try to infer the integer threshold from the entry using:
      - label: "(1)" or "1+"
      - label: "Over 1.5" => 2
      - total/handicap ~ 0.5 => 1, 1.5 => 2
    """
    label = e.get("label") or e.get("name") or ""
    m=_label_num_pat.search(label)
    if m: return int(m.group(1))
    m=_label_plus_pat.search(label)
    if m: return int(m.group(1))
    m=_label_over_pat.search(label)
    if m:
        try:
            line=float(m.group(1))
            return int(round(line+0.5))
        except: pass
    for key in ("handicap","total"):
        v = e.get(key)
        try:
            if v is None: continue
            line=float(v)
            return int(round(line+0.5))
        except: pass
    return None

def price_from_entry(e: dict) -> float|None:
    # Sportmonks names the decimal price "value" and also "dp3" (3-decimal string)
    for k in ("value","dp3"):
        v=e.get(k)
        try:
            if v is None: continue
            return float(v)
        except: pass
    return None

# ================== MAIN ==================
def main():
    targets = load_targets()
    if not targets:
        print("No targets from players_by_criteria files."); return

    # Build team -> fixture map from predicted_xi (your repo already generates these files)
    fixtures = _predicted_fixtures()
    team_to_fx = map_team_to_fixture(fixtures)
    print(f"[INFO] predicted_xi fixtures loaded: {len(fixtures)}")

    # Group targets by the fixture id their team is in
    buckets = {}  # fid -> list of targets (augmented with fixture names)
    unmatched=[]
    for t in targets:
        team = t.get("team") or ""
        fx=None
        # fast path: direct key hit
        if team in team_to_fx:
            fx=team_to_fx[team]
        else:
            # tolerant match
            for nm, rec in team_to_fx.items():
                if team_names_match(team, nm):
                    fx=rec; break
        if fx:
            eid=fx["fixture_id"]
            buckets.setdefault(eid, []).append({**t, "home": fx["home"], "away": fx["away"]})
        else:
            unmatched.append(t)

    if unmatched:
        print(f"[WARN] {len(unmatched)} targets not mapped to a fixture via predicted_xi.")

    flagged=[]

    # Call Sportmonks for each fixture (Bet365 only)
    for fid, tlist in buckets.items():
        data = fetch_bet365_prematch_for_fixture(fid)
        if not data: 
            continue

        # Split markets
        shots_rows   = [e for e in data if is_player_shots_market(e.get("market_description",""))]
        sot_rows     = [e for e in data if is_player_sot_market(e.get("market_description",""))]

        for t in tlist:
            best=None
            rows = shots_rows if t["stat"]=="shots" else sot_rows
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
                    "market": best["market"],
                    "label": best["label"],
                    "home": t["home"], "away": t["away"],
                })

    # de-dupe to best per (player,stat,thr)
    best_map={}
    for r in flagged:
        key=(r["player"], r["stat"], r["threshold"])
        cur=best_map.get(key)
        if (cur is None) or (r["price"] > cur["price"] + 1e-9):
            best_map[key]=r
    rows=list(best_map.values())

    # order: SOT 1+, SHOTS 2+, SHOTS 1+
    def bucket_key(r):
        if r["stat"]=="sot" and r["threshold"]==1: return 0
        if r["stat"]=="shots" and r["threshold"]==2: return 1
        if r["stat"]=="shots" and r["threshold"]==1: return 2
        return 9
    rows.sort(key=lambda r: (bucket_key(r), -r["price"], r["player"]))

    # Render clean TXT
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
