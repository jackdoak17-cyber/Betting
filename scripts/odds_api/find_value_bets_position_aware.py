#!/usr/bin/env python3
"""
Position-Aware V4 Value Bet Finder for Fouls & Tackles

Uses position rankings to filter for favorable matchups:
- Fouls Committed bets: Check if player's position is favorable
- Fouls Drawn bets: Check if opponent positions commit lots of fouls
- Tackles bets: Check if player's position makes lots of tackles

Output: data/odds_api/value_bets_position_aware.json
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
POSITION_RANKINGS_FILE = ROOT / "data" / "odds_api" / "position_rankings.json"
OUTPUT_FILE = ROOT / "data" / "odds_api" / "value_bets_position_aware.json"

# V4 criteria
MIN_ODDS = float(os.getenv("MIN_ODDS", "1.72"))
HIT_RATE_WINDOWS = [
    (7, 7), (7, 8), (8, 9), (7, 10),  # Exact windows requested
    (10, 12), (11, 14), (12, 16), (14, 18), (16, 20)  # Additional V4 windows
]
MIN_HIT_RATE_PERCENT = 70.0  # 70%+ over 10-20 games
POSITION_PERCENTILE_THRESHOLD = 50.0  # Top 50 percentile (more permissive)


def load_latest_odds() -> Optional[dict]:
    """Load the most recent odds file"""
    if not ODDS_DIR.exists():
        return None

    date_folders = sorted([d for d in ODDS_DIR.iterdir() if d.is_dir()], reverse=True)
    if not date_folders:
        return None

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


def load_position_rankings() -> dict:
    """Load position rankings"""
    if not POSITION_RANKINGS_FILE.exists():
        print(f"⚠️  Position rankings file not found: {POSITION_RANKINGS_FILE}")
        print("   Run build_position_rankings.py first!")
        return {}

    return json.loads(POSITION_RANKINGS_FILE.read_text())


def check_hit_rate_windows(history: List[int], threshold: int) -> Optional[Tuple[int, int, int]]:
    """
    Check if player meets any V4 hit rate window.
    Returns: (hits, games, required_hits) or None
    """
    for required_hits, window_size in HIT_RATE_WINDOWS:
        if len(history) < window_size:
            continue

        recent = history[:window_size]
        hits = sum(1 for x in recent if x >= threshold)

        if hits >= required_hits:
            return (hits, window_size, required_hits)

    return None


def check_percentage_hit_rate(history: List[int], threshold: int) -> Optional[Tuple[int, int, float]]:
    """
    Check if player meets 70%+ hit rate over 10-20 games.
    Returns: (hits, games, percentage) or None
    """
    for window_size in range(10, 21):
        if len(history) < window_size:
            continue

        recent = history[:window_size]
        hits = sum(1 for x in recent if x >= threshold)
        hit_rate = (hits / window_size) * 100

        if hit_rate >= MIN_HIT_RATE_PERCENT:
            return (hits, window_size, hit_rate)

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


def is_position_favorable(position: str, market_type: str, rankings: dict) -> Tuple[bool, float, str]:
    """
    Check if position is favorable for the given market type.
    Returns: (is_favorable, percentile, reason)
    """
    if not position or not rankings:
        return False, 0.0, "No position data"

    # Fouls Committed: Check if position commits lots of fouls
    if market_type == "fouls_committed":
        pos_stats = rankings.get("fouls_committed", {}).get(position)
        if not pos_stats:
            return False, 0.0, f"No ranking data for {position}"

        percentile = pos_stats["percentile"]
        # Higher percentile = commits more fouls = favorable
        if percentile >= (100 - POSITION_PERCENTILE_THRESHOLD):
            return True, percentile, f"{position} ranks high in fouls committed"
        else:
            return False, percentile, f"{position} doesn't commit enough fouls (percentile: {percentile})"

    # Tackles: Check if position makes lots of tackles
    elif market_type == "tackles":
        pos_stats = rankings.get("tackles", {}).get(position)
        if not pos_stats:
            return False, 0.0, f"No ranking data for {position}"

        percentile = pos_stats["percentile"]
        # Higher percentile = makes more tackles = favorable
        if percentile >= (100 - POSITION_PERCENTILE_THRESHOLD):
            return True, percentile, f"{position} ranks high in tackles"
        else:
            return False, percentile, f"{position} doesn't make enough tackles (percentile: {percentile})"

    return False, 0.0, "Unknown market type"


def analyze_odds_event(event: dict, fouls_players: dict, tackles_players: dict,
                       rankings: dict, debug: bool = False) -> List[dict]:
    """Analyze a single event for position-aware value bets"""
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

    # Map league slug to league_id
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

        # Check for player prop markets
        if "Player" not in market_name:
            continue

        odds_list = market.get("odds", [])

        for odds_entry in odds_list:
            label = odds_entry.get("label", "")
            over_odds = odds_entry.get("over")
            hdp = odds_entry.get("hdp", 0.5)

            if not over_odds or not label:
                continue

            # Parse odds
            try:
                odds_value = float(over_odds)
            except (ValueError, TypeError):
                continue

            # Filter by min odds
            if odds_value < MIN_ODDS:
                continue

            # Extract player name from label
            player_name = label.split("(")[0].strip() if "(" in label else label

            # Determine market type
            market_lower = market_name.lower()

            # Check for fouls COMMITTED (not fouls drawn/to be fouled)
            if "fouls committed" in market_lower or "foul committed" in market_lower:
                market_type = "fouls_committed"
                players_data = fouls_players
            # Explicitly exclude "to be fouled" (fouls drawn)
            elif "to be foul" in market_lower or "foul drawn" in market_lower:
                continue  # Skip fouls drawn
            # Check for tackles
            elif "tackle" in market_lower:
                market_type = "tackles"
                players_data = tackles_players
            else:
                continue  # Skip unknown markets

            # Find player
            player_id = find_player_id_from_name(player_name, players_data)
            if not player_id:
                if debug:
                    print(f"   ⚠️  Player not found: {player_name}")
                continue

            player = players_data[player_id]
            history = player["history"]
            position = player["position"]

            if not history or not position:
                continue

            # Check position favorability
            is_favorable, percentile, reason = is_position_favorable(
                position, market_type, rankings
            )

            if not is_favorable:
                if debug:
                    print(f"   ⏭️  {player_name} [{position}] - {reason}")
                continue

            # Check hit rate windows
            threshold = math.ceil(hdp)
            window_result = check_hit_rate_windows(history, threshold)
            pct_result = check_percentage_hit_rate(history, threshold)

            # Must pass either window check OR percentage check
            if not window_result and not pct_result:
                if debug:
                    print(f"   ⏭️  {player_name} - Hit rate too low")
                continue

            # Use window result if available, otherwise percentage
            if window_result:
                hits, games, required = window_result
                hit_rate_str = f"{hits}/{games}"
                criteria = f"Window: {required}/{games}"
            else:
                hits, games, pct = pct_result
                hit_rate_str = f"{hits}/{games} ({pct:.1f}%)"
                criteria = f"Percentage: {pct:.1f}%"

            # Calculate average
            avg = sum(history[:games]) / games if games > 0 else 0

            # Build reasoning
            reasoning_parts = []

            # Hit rate explanation
            if window_result:
                hits, games, required = window_result
                reasoning_parts.append(f"Hit {hits}/{games} games (need {required})")
            else:
                hits, games, pct = pct_result
                reasoning_parts.append(f"Hit {pct:.1f}% over {games} games")

            # Average performance
            reasoning_parts.append(f"averages {avg:.2f}")

            # Position advantage
            if market_type == "fouls_committed":
                reasoning_parts.append(f"{position} players commit more fouls than average ({percentile:.1f}th percentile)")
            elif market_type == "tackles":
                reasoning_parts.append(f"{position} players make more tackles than average ({percentile:.1f}th percentile)")

            # Recent form
            recent_3 = history[:3]
            if all(x >= threshold for x in recent_3):
                reasoning_parts.append("hit last 3 games")
            elif sum(1 for x in recent_3 if x >= threshold) >= 2:
                reasoning_parts.append("hit 2 of last 3 games")

            brief_reasoning = "; ".join(reasoning_parts)

            # Found a position-aware value bet!
            bet = {
                "event_id": event_id,
                "home": home_team,
                "away": away_team,
                "date": event_date,
                "league_id": league_id,
                "league": league_slug,
                "player_name": player["name"],
                "player_id": player_id,
                "position": position,
                "position_percentile": round(percentile, 1),
                "position_reason": reason,
                "market": f"O{hdp} {market_type.replace('_', ' ')}",
                "odds": odds_value,
                "hit_rate": hit_rate_str,
                "criteria": criteria,
                "average": round(avg, 2),
                "history": history[:games],
                "bookmaker": "Bet365",
                "reasoning": brief_reasoning
            }

            value_bets.append(bet)

            if debug:
                print(f"   ✅ {player_name} [{position}] - {market_type} @ {odds_value} - {reason}")

    return value_bets


def main():
    print("=" * 70)
    print("POSITION-AWARE V4 VALUE BET FINDER - Fouls & Tackles")
    print("=" * 70)
    print()

    # Load position rankings
    print("Loading position rankings...")
    rankings = load_position_rankings()
    if not rankings:
        print("  ✗ Failed to load position rankings!")
        return
    print(f"  ✓ Loaded rankings for {len(rankings.get('fouls_committed', {}))} positions")

    # Load data
    print("\nLoading player stats...")
    fouls_players = load_player_stats(FOULS_FILE, "fouls_last_n")
    tackles_players = load_player_stats(TACKLES_FILE, "tackles_last_n")
    print(f"  Loaded {len(fouls_players)} fouls players")
    print(f"  Loaded {len(tackles_players)} tackles players")

    print("\nLoading latest odds...")
    odds_data = load_latest_odds()
    if not odds_data:
        print("  No odds data found!")
        return

    events = odds_data.get("odds", [])
    print(f"  Found {len(events)} events")

    # Analyze each event
    print("\nAnalyzing events for position-aware value bets...")
    print(f"  Position threshold: Top {POSITION_PERCENTILE_THRESHOLD}% (percentile >= {100 - POSITION_PERCENTILE_THRESHOLD})")
    print(f"  Hit rate: Windows {HIT_RATE_WINDOWS[:4]} OR {MIN_HIT_RATE_PERCENT}%+ over 10-20 games")
    print(f"  Min odds: {MIN_ODDS}")
    print()

    all_value_bets = []
    debug = os.getenv("DEBUG", "0") == "1"

    for event in events:
        bets = analyze_odds_event(event, fouls_players, tackles_players,
                                  rankings, debug=debug)
        all_value_bets.extend(bets)

    # Save results
    output = {
        "generated_at": datetime.now().isoformat(),
        "min_odds": MIN_ODDS,
        "hit_rate_windows": [f"{h}/{w}" for h, w in HIT_RATE_WINDOWS],
        "min_hit_rate_percent": MIN_HIT_RATE_PERCENT,
        "position_percentile_threshold": POSITION_PERCENTILE_THRESHOLD,
        "total_value_bets": len(all_value_bets),
        "value_bets": all_value_bets
    }

    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_FILE.write_text(json.dumps(output, indent=2))

    print(f"\n{'=' * 70}")
    print(f"✅ Found {len(all_value_bets)} position-aware value bets")
    print(f"📁 Saved to: {OUTPUT_FILE}")
    print(f"{'=' * 70}")

    # Print top value bets
    if all_value_bets:
        print(f"\n🎯 TOP VALUE BETS (showing all {len(all_value_bets)}):")
        print()
        for i, bet in enumerate(all_value_bets[:20], 1):  # Show top 20
            print(f"{i}. {bet['player_name']} [{bet['position']}] - {bet['market']}")
            print(f"   🏟  {bet['home']} vs {bet['away']}")
            print(f"   💰 Odds: {bet['odds']:.2f} | Hit Rate: {bet['hit_rate']} | Avg: {bet['average']}")
            print(f"   💡 REASONING: {bet['reasoning']}")
            print(f"   📋 Recent: {bet['history']}")
            print()

        if len(all_value_bets) > 20:
            print(f"   ... and {len(all_value_bets) - 20} more bets")


if __name__ == "__main__":
    main()
