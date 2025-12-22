"""
V4 Value Bet Finder for Fouls & Tackles
Analyzes player stats against Odds-API.io data
"""
import json
import math
import os
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# Paths
ROOT = Path(".")
ODDS_DIR = ROOT / "data" / "odds_api"
FOULS_FILE = ROOT / "data" / "player_fouls" / "combined.json"
TACKLES_FILE = ROOT / "data" / "player_tackles" / "combined.json"
TEAM_STATS_FILE = ROOT / "data" / "team_stats" / "combined.json"
OUTPUT_FILE = ROOT / "data" / "odds_api" / "value_bets_v4.json"

# V4 criteria
MIN_ODDS = float(os.getenv("MIN_ODDS", "1.72"))
HIT_RATE_WINDOWS = [
    (7, 8), (7, 9), (8, 10), (9, 10),
    (10, 12), (11, 14), (12, 16), (14, 18), (16, 20)
]
TOP_N_STINGY = 5  # Skip top-5 stingiest defenses


def load_latest_odds() -> Optional[dict]:
    """Load the most recent odds file"""
    if not ODDS_DIR.exists():
        return None

    # Find latest date folder
    date_folders = sorted([d for d in ODDS_DIR.iterdir() if d.is_dir()], reverse=True)
    if not date_folders:
        return None

    # Find latest time file in that folder
    latest_folder = date_folders[0]
    odds_files = sorted(latest_folder.glob("odds_*.json"), reverse=True)
    if not odds_files:
        return None

    print(f"Loading odds from: {odds_files[0]}")
    return json.loads(odds_files[0].read_text())


def load_player_stats(stat_file: Path, stat_key: str) -> Dict[int, dict]:
    """Load player stats indexed by player_id"""
    data = json.loads(stat_file.read_text())
    players = {}

    for player in data.get("players", []):
        player_id = player.get("player_id")
        if player_id:
            players[player_id] = {
                "name": player.get("name"),
                "team_id": player.get("team_id"),
                "league_id": player.get("league_id"),
                "position": player.get("position_tag"),
                "history": player.get(stat_key, []),
                "fixture_ids": player.get("fixture_ids", [])
            }

    return players


def calculate_opponent_rankings(team_stats: dict, stat_key: str) -> Dict[int, Dict[int, int]]:
    """
    Calculate team rankings for conceding stats (fouls/tackles) by league.
    Returns: {league_id: {team_id: rank}}
    Lower rank = stingier (concedes fewer fouls/tackles)
    """
    rankings_by_league = defaultdict(dict)

    # Group teams by league
    teams_by_league = defaultdict(list)
    for team in team_stats.get("teams", []):
        league_id = team.get("league_id")
        team_id = team.get("team_id")
        stat_history = team.get(stat_key, [])

        if league_id and team_id and stat_history:
            avg_stat = sum(stat_history[:10]) / min(10, len(stat_history))
            teams_by_league[league_id].append({
                "team_id": team_id,
                "avg": avg_stat
            })

    # Rank teams within each league (ascending = stingy)
    for league_id, teams in teams_by_league.items():
        sorted_teams = sorted(teams, key=lambda x: x["avg"])
        for rank, team in enumerate(sorted_teams, start=1):
            rankings_by_league[league_id][team["team_id"]] = rank

    return rankings_by_league


def check_hit_rate_windows(history: List[int], threshold: int) -> Optional[Tuple[int, int, int]]:
    """
    Check if player meets any V4 hit rate window.
    Returns: (hits, games, window_size) or None
    """
    for required_hits, window_size in HIT_RATE_WINDOWS:
        if len(history) < window_size:
            continue

        recent = history[:window_size]
        hits = sum(1 for x in recent if x >= threshold)

        if hits >= required_hits:
            return (hits, window_size, required_hits)

    return None


def find_player_id_from_name(name: str, players: Dict[int, dict]) -> Optional[int]:
    """Fuzzy match player name to player_id"""
    name_lower = name.lower().strip()

    # Try exact match first
    for player_id, data in players.items():
        if data["name"].lower().strip() == name_lower:
            return player_id

    # Try partial match
    for player_id, data in players.items():
        player_name = data["name"].lower().strip()
        if name_lower in player_name or player_name in name_lower:
            return player_id

    return None


def analyze_odds_event(event: dict, fouls_players: dict, tackles_players: dict,
                      fouls_rankings: dict, tackles_rankings: dict) -> List[dict]:
    """Analyze a single event for value bets"""
    value_bets = []

    # Get Bet365 bookmaker data
    bookmakers = event.get("bookmakers", {})
    bet365 = bookmakers.get("Bet365", [])

    if not bet365:
        return value_bets

    # Extract event info
    event_id = event.get("id")
    home_team = event.get("home", "")
    away_team = event.get("away", "")
    event_date = event.get("date", "")
    league_slug = event.get("league", {}).get("slug", "")

    # Map league slug to league_id (approximate)
    league_map = {
        "england-premier-league": 8,
        "spain-laliga": 564,
        "italy-serie-a": 384
    }
    league_id = league_map.get(league_slug)

    if not league_id:
        return value_bets

    # Find fouls and tackles markets
    for market in bet365:
        market_name = market.get("name", "")

        # Check for fouls/tackles markets (these would need to be in the odds data)
        # For now, we'll process any player prop markets
        if "Player" not in market_name:
            continue

        odds_list = market.get("odds", [])

        for odds_entry in odds_list:
            label = odds_entry.get("label", "")
            over_odds = odds_entry.get("over")
            hdp = odds_entry.get("hdp", 0.5)

            if not over_odds or not label:
                continue

            # Parse odds (could be string)
            try:
                odds_value = float(over_odds)
            except (ValueError, TypeError):
                continue

            # Filter by min odds
            if odds_value < MIN_ODDS:
                continue

            # Extract player name from label
            # Format: "Player Name (X)" where X is some number
            player_name = label.split("(")[0].strip() if "(" in label else label

            # Try to match with fouls/tackles stats
            # Determine if it's fouls or tackles based on market name
            # (This is a heuristic - adjust based on actual market names)
            if "foul" in market_name.lower():
                stat_type = "fouls"
                players_data = fouls_players
                rankings = fouls_rankings
            elif "tackle" in market_name.lower():
                stat_type = "tackles"
                players_data = tackles_players
                rankings = tackles_rankings
            else:
                continue  # Skip unknown markets

            # Find player
            player_id = find_player_id_from_name(player_name, players_data)
            if not player_id:
                continue

            player = players_data[player_id]
            history = player["history"]
            team_id = player["team_id"]

            if not history:
                continue

            # Check hit rate windows
            # For "over" lines, need to round up: O0.5 needs ≥1, O1.5 needs ≥2, etc.
            threshold = math.ceil(hdp)
            window_result = check_hit_rate_windows(history, threshold)

            if not window_result:
                continue

            hits, games, required = window_result

            # Check opponent ranking (skip if opponent is too stingy)
            # We need to figure out which team is the opponent
            # This requires mapping team names to team IDs
            # For now, skip this check (TODO: improve)

            # Calculate average
            avg = sum(history[:games]) / games if games > 0 else 0

            # Found a value bet!
            value_bets.append({
                "event_id": event_id,
                "home": home_team,
                "away": away_team,
                "date": event_date,
                "league_id": league_id,
                "league": league_slug,
                "player_name": player["name"],
                "player_id": player_id,
                "position": player["position"],
                "market": f"O{hdp} {stat_type}",
                "odds": odds_value,
                "hit_rate": f"{hits}/{games}",
                "required": f"{required}/{games}",
                "average": round(avg, 2),
                "history": history[:games],
                "bookmaker": "Bet365"
            })

    return value_bets


def main():
    print("=" * 70)
    print("V4 VALUE BET FINDER - Fouls & Tackles")
    print("=" * 70)
    print()

    # Load data
    print("Loading player stats...")
    fouls_players = load_player_stats(FOULS_FILE, "fouls_last_n")
    tackles_players = load_player_stats(TACKLES_FILE, "tackles_last_n")
    print(f"  Loaded {len(fouls_players)} fouls players")
    print(f"  Loaded {len(tackles_players)} tackles players")

    print("\nLoading team stats...")
    team_stats = json.loads(TEAM_STATS_FILE.read_text())
    fouls_rankings = calculate_opponent_rankings(team_stats, "fouls_last_n")
    tackles_rankings = calculate_opponent_rankings(team_stats, "tackles_last_n")
    print(f"  Calculated rankings for {len(fouls_rankings)} leagues")

    print("\nLoading latest odds...")
    odds_data = load_latest_odds()
    if not odds_data:
        print("  No odds data found!")
        return

    events = odds_data.get("odds", [])
    print(f"  Found {len(events)} events")

    # Analyze each event
    print("\nAnalyzing events for value bets...")
    all_value_bets = []

    for event in events:
        bets = analyze_odds_event(event, fouls_players, tackles_players,
                                  fouls_rankings, tackles_rankings)
        all_value_bets.extend(bets)

    # Save results
    output = {
        "generated_at": datetime.now().isoformat(),
        "min_odds": MIN_ODDS,
        "hit_rate_windows": [f"{h}/{w}" for h, w in HIT_RATE_WINDOWS],
        "total_value_bets": len(all_value_bets),
        "value_bets": all_value_bets
    }

    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_FILE.write_text(json.dumps(output, indent=2))

    print(f"\n{'=' * 70}")
    print(f"✅ Found {len(all_value_bets)} value bets")
    print(f"📁 Saved to: {OUTPUT_FILE}")
    print(f"{'=' * 70}")

    # Print sample bets
    if all_value_bets:
        print(f"\nSample bets:")
        for bet in all_value_bets[:5]:
            print(f"  • {bet['player_name']} - {bet['market']} @ {bet['odds']}")
            print(f"    {bet['home']} vs {bet['away']}")
            print(f"    Hit rate: {bet['hit_rate']} | Avg: {bet['average']}")
            print()


if __name__ == "__main__":
    main()
