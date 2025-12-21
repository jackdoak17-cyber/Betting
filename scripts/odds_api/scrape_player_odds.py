"""
Hourly scraper for player prop odds from Odds-API.io
Saves raw data for later analysis
"""
import requests
import json
import os
from datetime import datetime
from pathlib import Path

API_KEY = os.environ.get('ODDS_API_KEY', 'b523f23279aa237311037661958934cd7199f5a0ead583d1746ca5e0ade451ec')
BASE_URL = 'https://api2.odds-api.io/v3'

# Target leagues
LEAGUES = ['england-premier-league', 'spain-laliga', 'italy-serie-a']
BOOKMAKER = 'Bet365'

def get_pending_events(league_slug):
    """Get pending events for a league"""
    response = requests.get(f"{BASE_URL}/events", params={
        'apiKey': API_KEY,
        'sport': 'football',
        'league': league_slug,
        'status': 'pending'
    })
    response.raise_for_status()
    return response.json()

def get_odds_batch(event_ids):
    """Get odds for up to 10 events (1 request)"""
    response = requests.get(f"{BASE_URL}/odds/multi", params={
        'apiKey': API_KEY,
        'eventIds': ','.join(map(str, event_ids)),
        'bookmakers': BOOKMAKER
    })
    response.raise_for_status()
    return response.json()

def main():
    timestamp = datetime.now()
    print(f"🕐 {timestamp.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"📊 Scraping player odds from {BOOKMAKER}\n")

    all_odds = []
    requests_used = 0

    # Process each league
    for league in LEAGUES:
        print(f"⚽ {league}...")

        # Get events
        events = get_pending_events(league)
        requests_used += 1

        if not events:
            print(f"   No pending events")
            continue

        print(f"   {len(events)} events found")

        # Batch fetch odds (10 events per request)
        event_ids = [e['id'] for e in events]
        for i in range(0, len(event_ids), 10):
            batch = event_ids[i:i+10]
            odds = get_odds_batch(batch)
            requests_used += 1
            all_odds.extend(odds)
            print(f"   Fetched odds for {len(batch)} events")

    # Save raw odds data
    date_str = timestamp.strftime('%Y%m%d')
    time_str = timestamp.strftime('%H%M')
    output_dir = Path(f'data/odds_api/{date_str}')
    output_dir.mkdir(parents=True, exist_ok=True)

    output_file = output_dir / f'odds_{time_str}.json'

    with open(output_file, 'w') as f:
        json.dump({
            'timestamp': timestamp.isoformat(),
            'bookmaker': BOOKMAKER,
            'leagues': LEAGUES,
            'requests_used': requests_used,
            'events_count': len(all_odds),
            'odds': all_odds
        }, f, indent=2)

    print(f"\n✅ Saved {len(all_odds)} events")
    print(f"📁 {output_file}")
    print(f"🔢 API requests: {requests_used}/100")

if __name__ == '__main__':
    main()
