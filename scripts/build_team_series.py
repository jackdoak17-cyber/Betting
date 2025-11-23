 #!/usr/bin/env python3
 # -*- coding: utf-8 -*-
 """
 Build BOTH team and opponent series (latest->older) with HOME/AWAY flags.
 
 Captured team stats (per game, integers):
   - shots_total (42)
   - shots_on_target (86)
   - fouls (56)
   - tackles (78)
   - cards_total = yellow (84) + red (83) [+ optional second yellow 85]
   - saves (57)
   - goal_kicks (53)
   - corners (34)
   - offsides (51)
   - goals (52)                 <-- NEW
+  - possession (45)
 
 Captured opponent-allowed series (values for the OPPOSING team in those games):
   - opp_shots_total (42)
   - opp_shots_on_target (86)
   - opp_fouls (56)
   - opp_tackles (78)
   - opp_cards_total (84+83[+85])
   - opp_saves (57)
   - opp_goal_kicks (53)
   - opp_corners (34)
   - opp_offsides (51)
   - opp_goals (52)             <-- NEW
+  - opp_possession (45)
 
 Outputs:
   - data/team_stats/by_league/{league_id}.json
       each team row includes:
         shots_total_last_n, shots_on_target_last_n, fouls_last_n, tackles_last_n,
         cards_total_last_n, saves_last_n, goal_kicks_last_n, corners_last_n,
         offsides_last_n, goals_last_n,                    <-- NEW
+        possession_last_n,
         fixture_ids, locations_last_n
   - data/team_opponent_stats/by_league/{league_id}.json
       each team row includes:
         opp_shots_total_last_n, opp_shots_on_target_last_n, opp_fouls_last_n,
         opp_tackles_last_n, opp_cards_total_last_n, opp_saves_last_n,
         opp_goal_kicks_last_n, opp_corners_last_n,
         opp_offsides_last_n, opp_goals_last_n,             <-- NEW
+        opp_possession_last_n,
         fixture_ids, locations_last_n  (location is for THIS team in that game)
 
 Also writes combined.json + summary.txt in each tree.
 
 Env:
   SPORTMONKS_TOKEN                  (required)
   TEAM_STATS_LAST_N                 (default 10)
   TEAM_OPP_STATS_LAST_N             (default 10)
   INCLUDE_SECOND_YELLOW_IN_CARDS    (0/1, default 0)
   SERIES_MODE                       (both | team | opp, default both)
-  TEAM_STAT_*_ID overrides supported; OFFSIDES=51, GOALS=52
+  TEAM_STAT_*_ID overrides supported; OFFSIDES=51, GOALS=52, POSSESSION=45
 """
 
 import os
 import json
 import time
 import datetime as dt
 from pathlib import Path
 from typing import Dict, List, Tuple, Optional
 import requests
 
 # ----------------- API / pacing -----------------
 API_BASE = "https://api.sportmonks.com/v3/football"
 API_TOKEN = (
     os.getenv("SPORTMONKS_TOKEN")
     or os.getenv("SPORTMONKS_API_TOKEN")
     or os.getenv("SM_TOKEN")
 )
 if not API_TOKEN:
     raise SystemExit("ERROR: SPORTMONKS_TOKEN / SPORTMONKS_API_TOKEN not set.")
 
 TIMEOUT = 25
 RETRIES = 3
 BACKOFF = 1.6
 GLOBAL_MIN_DELAY = 0.18
 
@@ -102,50 +106,51 @@ def api_get(path: str, params: Optional[dict] = None) -> dict:
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
 
 # ----------------- Type IDs -----------------
 SHOTS_TOTAL      = int(os.getenv("TEAM_STAT_SHOTS_TOTAL_ID", "42"))
 SHOTS_ON_TARGET  = int(os.getenv("TEAM_STAT_SHOTS_ON_TARGET_ID", "86"))
 FOULS            = int(os.getenv("TEAM_STAT_FOULS_ID", "56"))
 TACKLES          = int(os.getenv("TEAM_STAT_TACKLES_ID", "78"))
 YELLOW           = int(os.getenv("TEAM_STAT_YELLOW_CARDS_ID", "84"))
 RED              = int(os.getenv("TEAM_STAT_RED_CARDS_ID", "83"))
 SECOND_YELLOW    = int(os.getenv("TEAM_STAT_SECOND_YELLOWS_ID", "85"))
 SAVES            = int(os.getenv("TEAM_STAT_SAVES_ID", "57"))
 GOAL_KICKS       = int(os.getenv("TEAM_STAT_GOAL_KICKS_ID", "53"))
 CORNERS          = int(os.getenv("TEAM_STAT_CORNERS_ID", "34"))
 OFFSIDES         = int(os.getenv("TEAM_STAT_OFFSIDES_ID", "51"))
 GOALS            = int(os.getenv("TEAM_STAT_GOALS_ID", "52"))  # <-- NEW
+POSSESSION       = int(os.getenv("TEAM_STAT_POSSESSION_ID", "45"))
 
 INCLUDE_SECOND_YELLOW_IN_CARDS = os.getenv("INCLUDE_SECOND_YELLOW_IN_CARDS", "0") in ("1","true","TRUE","yes","YES")
 
 # ----------------- Config -----------------
 LAST_N_TEAM = int(os.getenv("TEAM_STATS_LAST_N", "10"))
 LAST_N_OPP  = int(os.getenv("TEAM_OPP_STATS_LAST_N", "10"))
 SERIES_MODE = (os.getenv("SERIES_MODE") or "both").strip().lower()
 if SERIES_MODE not in {"both","team","opp"}:
     SERIES_MODE = "both"
 
 # ----------------- IO -----------------
 PX_DIR = Path("data/predicted_xi/by_league")
 
 OUT_TEAM_ROOT = Path("data/team_stats")
 OUT_TEAM_BY   = OUT_TEAM_ROOT / "by_league"
 OUT_TEAM_BY.mkdir(parents=True, exist_ok=True)
 
 OUT_OPP_ROOT  = Path("data/team_opponent_stats")
 OUT_OPP_BY    = OUT_OPP_ROOT / "by_league"
 OUT_OPP_BY.mkdir(parents=True, exist_ok=True)
 
 def ensure_dir(path: Path) -> None:
     path.parent.mkdir(parents=True, exist_ok=True)
 
 # ----------------- Helpers -----------------
@@ -258,107 +263,108 @@ def fetch_team_fixtures_window(team_id: int, start: dt.date, end: dt.date, leagu
     GET fixtures for team in [start,end] with league & statistic type filters, ordered desc.
     Includes participants to infer home/away.
     """
     path = f"fixtures/between/{dstr(start)}/{dstr(end)}/{team_id}"
     params = {
         "include": "participants;statistics;state",
         "filters": f"fixtureStatisticTypes:{','.join(str(x) for x in type_ids)};fixtureLeagues:{league_id}",
         "order": "desc",
         "per_page": 50,
         "page": page,
     }
     return api_get(path, params)
 
 # ----------------- TEAM series -----------------
 def collect_team_series(league_id: int, season_id: int, team_id: int, last_n: int) -> dict:
     """
     Returns:
       {
         'stats': { shots_total:[...], shots_on_target:[...], fouls:[...], tackles:[...],
                    cards_total:[...], saves:[...], goal_kicks:[...], corners:[...],
                    offsides:[...], goals:[...] },
         'fixtures': [ids],  # aligned latest->older
         'locations': ["home"/"away"/"unknown", ...]
       }
     """
-    type_ids = list({SHOTS_TOTAL, SHOTS_ON_TARGET, FOULS, TACKLES, YELLOW, RED, SAVES, GOAL_KICKS, CORNERS, OFFSIDES, GOALS})
+    type_ids = list({SHOTS_TOTAL, SHOTS_ON_TARGET, FOULS, TACKLES, YELLOW, RED, SAVES, GOAL_KICKS, CORNERS, OFFSIDES, GOALS, POSSESSION})
     start_season, end_today = get_season_bounds(season_id)
     end = end_today
 
     series = {
         "shots_total": [], "shots_on_target": [], "fouls": [], "tackles": [],
         "cards_total": [], "saves": [], "goal_kicks": [], "corners": [],
-        "offsides": [], "goals": []  # NEW
+        "offsides": [], "goals": [], "possession": []  # NEW possession
     }
     fixture_ids: List[int] = []
     locations: List[str] = []
 
     def have_enough() -> bool:
         return all(len(series[k]) >= last_n for k in series.keys())
 
     while end >= start_season and not have_enough():
         win_start = max(start_season, end - dt.timedelta(days=99))
         page = 1
         has_more = True
         while has_more and not have_enough():
             j = fetch_team_fixtures_window(team_id, win_start, end, league_id, type_ids, page=page)
             data = j.get("data") or []
             meta = j.get("meta") or {}
             has_more = bool(meta.get("has_more"))
             page += 1
 
             for fx in data:
                 if int(fx.get("league_id") or 0) != league_id:  continue
                 if int(fx.get("season_id") or 0) != season_id:  continue
                 if int(fx.get("state_id") or 0) not in (5,):    continue  # finished
 
                 fid = int(fx.get("id") or 0)
                 by_type: Dict[int, int] = {}
                 for s in (fx.get("statistics") or []):
                     try:
                         if int(s.get("participant_id") or 0) != team_id:
                             continue
                         t = int(s.get("type_id") or 0)
                         vobj = s.get("data") or s.get("value") or {}
                         val = vobj.get("value") if isinstance(vobj, dict) else None
                         if val is None:
                             continue
                         by_type[t] = int(float(val))
                     except Exception:
                         continue
 
                 cards_total = int(by_type.get(YELLOW, 0)) + int(by_type.get(RED, 0))
                 series["shots_total"].append(int(by_type.get(SHOTS_TOTAL, 0)))
                 series["shots_on_target"].append(int(by_type.get(SHOTS_ON_TARGET, 0)))
                 series["fouls"].append(int(by_type.get(FOULS, 0)))
                 series["tackles"].append(int(by_type.get(TACKLES, 0)))
                 series["cards_total"].append(cards_total)
                 series["saves"].append(int(by_type.get(SAVES, 0)))
                 series["goal_kicks"].append(int(by_type.get(GOAL_KICKS, 0)))
                 series["corners"].append(int(by_type.get(CORNERS, 0)))
                 series["offsides"].append(int(by_type.get(OFFSIDES, 0)))
                 series["goals"].append(int(by_type.get(GOALS, 0)))  # NEW
+                series["possession"].append(int(by_type.get(POSSESSION, 0)))
                 fixture_ids.append(fid)
 
                 loc = infer_location(fx, team_id) or "unknown"
                 locations.append(loc)
 
                 if have_enough():
                     break
 
         end = win_start - dt.timedelta(days=1)
 
     # clamp
     for k in series:
         series[k] = series[k][:last_n]
     fixture_ids = fixture_ids[:last_n]
     locations   = locations[:last_n]
     return {"stats": series, "fixtures": fixture_ids, "locations": locations}
 
 # ----------------- OPP series -----------------
 def _parse_start_ts(fx: dict) -> int:
     st = fx.get("starting_at")
     if isinstance(st, dict):
         ts = st.get("timestamp") or st.get("ts")
         if isinstance(ts, (int, float)):
             return int(ts)
         dt_str = st.get("date_time") or st.get("date") or st.get("starting_at")
@@ -379,51 +385,51 @@ def _parse_start_ts(fx: dict) -> int:
                 s = dt_str.replace("Z", "").replace("T", " ")
                 return int(dt.datetime.strptime(s[:19], "%Y-%m-%d %H:%M:%S").timestamp())
             except Exception:
                 pass
     return 0
 
 def _opponent_id_from_fixture(fx: dict, team_id: int) -> Optional[int]:
     parts = fx.get("participants")
     if isinstance(parts, list) and len(parts) >= 2:
         ids = [int(p.get("id")) for p in parts if p.get("id") is not None]
         others = [i for i in ids if i != team_id]
         if others:
             return others[0]
     stats = fx.get("statistics") or []
     participants = {int(s.get("participant_id")) for s in stats if s.get("participant_id") is not None}
     others = [i for i in participants if i != team_id]
     if others:
         return others[0]
     return None
 
 def collect_opponent_series(league_id: int, season_id: int, team_id: int, last_n: int) -> dict:
     """
     Build opponent stat series for the given team.
     Locations reflect THIS TEAM's home/away in each match.
     """
-    needed_type_ids = {SHOTS_TOTAL, SHOTS_ON_TARGET, FOULS, TACKLES, YELLOW, RED, SAVES, GOAL_KICKS, CORNERS, OFFSIDES, GOALS}
+    needed_type_ids = {SHOTS_TOTAL, SHOTS_ON_TARGET, FOULS, TACKLES, YELLOW, RED, SAVES, GOAL_KICKS, CORNERS, OFFSIDES, GOALS, POSSESSION}
     if INCLUDE_SECOND_YELLOW_IN_CARDS:
         needed_type_ids.add(SECOND_YELLOW)
 
     start_season, end_today = get_season_bounds(season_id)
     end = end_today
 
     recs: List[dict] = []
     seen_fids: set[int] = set()
 
     def got_enough_unique() -> bool:
         return len(recs) >= last_n
 
     while end >= start_season and not got_enough_unique():
         win_start = max(start_season, end - dt.timedelta(days=99))
         page = 1
         has_more = True
         while has_more and not got_enough_unique():
             j = fetch_team_fixtures_window(team_id, win_start, end, league_id, sorted(needed_type_ids), page=page)
             data = j.get("data") or []
             meta = j.get("meta") or {}
             per_page = 50
             has_more = bool(meta.get("has_more")) or (len(data) == per_page)
             page += 1
 
             for fx in data:
@@ -457,66 +463,68 @@ def collect_opponent_series(league_id: int, season_id: int, team_id: int, last_n
                 loc = infer_location(fx, team_id) or "unknown"
                 recs.append({"fid": fid, "ts": start_ts, "by_type": by_type_opp, "loc": loc})
                 seen_fids.add(fid)
 
         end = win_start - dt.timedelta(days=1)
 
     recs.sort(key=lambda r: r["ts"], reverse=True)
 
     def card_sum(bt: Dict[int, int]) -> int:
         base = int(bt.get(YELLOW, 0)) + int(bt.get(RED, 0))
         if INCLUDE_SECOND_YELLOW_IN_CARDS:
             base += int(bt.get(SECOND_YELLOW, 0))
         return base
 
     series = {
         "opp_shots_total": [],
         "opp_shots_on_target": [],
         "opp_fouls": [],
         "opp_tackles": [],
         "opp_cards_total": [],
         "opp_saves": [],
         "opp_goal_kicks": [],
         "opp_corners": [],
         "opp_offsides": [],
         "opp_goals": [],  # NEW
+        "opp_possession": [],
     }
     fixture_ids: List[int] = []
     locations: List[str] = []
 
     for r in recs[:last_n]:
         bt = r["by_type"]
         series["opp_shots_total"].append(int(bt.get(SHOTS_TOTAL, 0)))
         series["opp_shots_on_target"].append(int(bt.get(SHOTS_ON_TARGET, 0)))
         series["opp_fouls"].append(int(bt.get(FOULS, 0)))
         series["opp_tackles"].append(int(bt.get(TACKLES, 0)))
         series["opp_cards_total"].append(card_sum(bt))
         series["opp_saves"].append(int(bt.get(SAVES, 0)))
         series["opp_goal_kicks"].append(int(bt.get(GOAL_KICKS, 0)))
         series["opp_corners"].append(int(bt.get(CORNERS, 0)))
         series["opp_offsides"].append(int(bt.get(OFFSIDES, 0)))
         series["opp_goals"].append(int(bt.get(GOALS, 0)))  # NEW
+        series["opp_possession"].append(int(bt.get(POSSESSION, 0)))
         fixture_ids.append(int(r["fid"]))
         locations.append(r.get("loc", "unknown"))
 
     return {"stats": series, "fixtures": fixture_ids, "locations": locations}
 
 # ----------------- MAIN -----------------
 def main():
     teams_by_ls = load_target_teams()
     if not teams_by_ls:
         print("No targets found. Did predicted_xi run?")
         return
 
     gen_at = dt.datetime.now(dt.timezone.utc).isoformat()
 
     # Prepare aggregates
     per_league_team: Dict[int, List[dict]] = {}
     per_league_opp : Dict[int, List[dict]] = {}
     combined_team_rows: List[dict] = []
     combined_opp_rows : List[dict] = []
 
     items = []
     for (lid, sid), tm in teams_by_ls.items():
         for tid, tname in tm.items():
             items.append((lid, sid, tid, tname))
     items.sort()
