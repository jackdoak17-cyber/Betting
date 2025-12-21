#!/usr/bin/env python3
"""
Simple script to get your Telegram Chat ID

Instructions:
1. Set TELEGRAM_BOT_TOKEN environment variable (or edit TOKEN below)
2. Send a message to your bot in Telegram (e.g., "/start" or "Hello")
3. Run this script: python get_chat_id.py
4. Your chat ID will be displayed in the output

Your chat ID is the number shown in "chat": {"id": YOUR_CHAT_ID}
"""

import requests
import os
import json

# Get token from environment variable or replace with your token
TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN', 'YOUR_TOKEN_HERE')

if TOKEN == 'YOUR_TOKEN_HERE':
    print("=" * 70)
    print("ERROR: No Telegram bot token found!")
    print("=" * 70)
    print("\nPlease either:")
    print("1. Set environment variable:")
    print("   export TELEGRAM_BOT_TOKEN='your_bot_token_here'")
    print("\n2. Or edit this script and replace 'YOUR_TOKEN_HERE' with your token")
    print("\n" + "=" * 70)
    exit(1)

print("=" * 70)
print("TELEGRAM CHAT ID FINDER")
print("=" * 70)
print(f"\nUsing bot token: {TOKEN[:10]}...{TOKEN[-10:]}\n")

url = f"https://api.telegram.org/bot{TOKEN}/getUpdates"

try:
    print("Fetching updates from Telegram API...")
    response = requests.get(url, timeout=10)
    response.raise_for_status()

    data = response.json()

    if not data.get('ok'):
        print(f"\n❌ ERROR: {data.get('description', 'Unknown error')}")
        exit(1)

    updates = data.get('result', [])

    if not updates:
        print("\n⚠️  NO MESSAGES FOUND!")
        print("\nTo get your Chat ID:")
        print("1. Open Telegram")
        print("2. Search for your bot")
        print("3. Click 'START' or send any message to the bot")
        print("4. Run this script again")
        print("\n" + "=" * 70)
        exit(0)

    print(f"\n✅ Found {len(updates)} update(s)\n")
    print("=" * 70)

    # Extract unique chat IDs
    chat_ids = set()

    for update in updates:
        # Check message
        if 'message' in update:
            msg = update['message']
            chat = msg.get('chat', {})
            chat_id = chat.get('id')
            chat_type = chat.get('type')
            first_name = chat.get('first_name', '')
            last_name = chat.get('last_name', '')
            username = chat.get('username', '')

            if chat_id:
                chat_ids.add(chat_id)
                print(f"\n💬 Message from:")
                print(f"   Chat ID: {chat_id}")
                print(f"   Type: {chat_type}")
                if first_name or last_name:
                    print(f"   Name: {first_name} {last_name}".strip())
                if username:
                    print(f"   Username: @{username}")
                print(f"   Text: {msg.get('text', '(no text)')}")

        # Check callback query
        if 'callback_query' in update:
            query = update['callback_query']
            chat = query.get('message', {}).get('chat', {})
            chat_id = chat.get('id')
            if chat_id:
                chat_ids.add(chat_id)

    print("\n" + "=" * 70)
    print("\n🎯 YOUR CHAT ID(S):")
    print("=" * 70)

    for chat_id in sorted(chat_ids):
        print(f"\n   {chat_id}")

    if len(chat_ids) == 1:
        the_id = list(chat_ids)[0]
        print(f"\n✅ Use this in your GitHub secret:")
        print(f"\n   TELEGRAM_CHAT_ID = {the_id}")
    else:
        print(f"\n⚠️  Found multiple chat IDs. Use the one that matches your account.")

    print("\n" + "=" * 70)

    # Show raw JSON if requested
    if os.environ.get('DEBUG', '').lower() in ('1', 'true', 'yes'):
        print("\n📋 RAW JSON:")
        print("=" * 70)
        print(json.dumps(data, indent=2))
        print("=" * 70)

except requests.exceptions.RequestException as e:
    print(f"\n❌ ERROR: Failed to connect to Telegram API")
    print(f"   {str(e)}")
    print("\nPlease check:")
    print("1. Your internet connection")
    print("2. Your bot token is correct")
    print("\n" + "=" * 70)
    exit(1)

except Exception as e:
    print(f"\n❌ UNEXPECTED ERROR: {str(e)}")
    print("\n" + "=" * 70)
    exit(1)
