#!/usr/bin/env python3
"""
Test Telegram notification setup

This script sends a test message to verify your Telegram bot is configured correctly.

Usage:
    export TELEGRAM_BOT_TOKEN="your_token"
    export TELEGRAM_CHAT_ID="your_chat_id"
    python test_telegram.py
"""

import os
import requests
import json

def test_telegram_setup():
    """Test Telegram bot configuration"""

    print("=" * 70)
    print("TELEGRAM NOTIFICATION TEST")
    print("=" * 70)

    # Get credentials
    token = os.environ.get('TELEGRAM_BOT_TOKEN', '')
    chat_id = os.environ.get('TELEGRAM_CHAT_ID', '')

    # Validate credentials
    if not token:
        print("\n❌ ERROR: TELEGRAM_BOT_TOKEN not set")
        print("\nPlease run:")
        print('  export TELEGRAM_BOT_TOKEN="your_bot_token_here"')
        return False

    if not chat_id:
        print("\n❌ ERROR: TELEGRAM_CHAT_ID not set")
        print("\nPlease run:")
        print('  export TELEGRAM_CHAT_ID="your_chat_id_here"')
        print("\nOr use get_chat_id.py to find your Chat ID")
        return False

    print(f"\n✅ Bot Token: {token[:10]}...{token[-10:]}")
    print(f"✅ Chat ID: {chat_id}")

    # Test bot info
    print("\n" + "-" * 70)
    print("Step 1: Testing bot connection...")
    print("-" * 70)

    try:
        url = f"https://api.telegram.org/bot{token}/getMe"
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        data = response.json()

        if not data.get('ok'):
            print(f"\n❌ Bot connection failed: {data.get('description')}")
            return False

        bot_info = data.get('result', {})
        print(f"\n✅ Connected to bot: @{bot_info.get('username', 'unknown')}")
        print(f"   Name: {bot_info.get('first_name', 'unknown')}")
        print(f"   ID: {bot_info.get('id', 'unknown')}")

    except requests.exceptions.RequestException as e:
        print(f"\n❌ Failed to connect to Telegram API: {str(e)}")
        return False

    # Test sending message
    print("\n" + "-" * 70)
    print("Step 2: Sending test message...")
    print("-" * 70)

    test_message = """🧪 <b>Telegram Test Message</b>

✅ Your V4 Player Singles setup is working!

<b>Configuration:</b>
• Bot Token: Configured ✓
• Chat ID: Configured ✓
• Connection: Active ✓

You're all set to receive betting notifications! 🎯

<i>This is a test message from test_telegram.py</i>"""

    try:
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        payload = {
            "chat_id": chat_id,
            "text": test_message,
            "parse_mode": "HTML",
            "disable_web_page_preview": True
        }

        response = requests.post(url, json=payload, timeout=10)
        response.raise_for_status()
        data = response.json()

        if not data.get('ok'):
            print(f"\n❌ Failed to send message: {data.get('description')}")
            return False

        print("\n✅ Test message sent successfully!")
        print("\n📱 Check your Telegram app - you should see the test message")

        # Show message details
        result = data.get('result', {})
        print(f"\n📋 Message Details:")
        print(f"   Message ID: {result.get('message_id')}")
        print(f"   Date: {result.get('date')}")

    except requests.exceptions.RequestException as e:
        print(f"\n❌ Failed to send message: {str(e)}")
        return False

    # Success!
    print("\n" + "=" * 70)
    print("🎉 SUCCESS - Your Telegram setup is working perfectly!")
    print("=" * 70)
    print("\nNext steps:")
    print("1. Add these secrets to GitHub:")
    print(f"   TELEGRAM_BOT_TOKEN = {token}")
    print(f"   TELEGRAM_CHAT_ID = {chat_id}")
    print("\n2. Run the V4 workflow to start receiving real betting alerts")
    print("\n" + "=" * 70)

    return True


if __name__ == "__main__":
    try:
        success = test_telegram_setup()
        exit(0 if success else 1)
    except KeyboardInterrupt:
        print("\n\n⚠️  Test cancelled by user")
        exit(1)
    except Exception as e:
        print(f"\n❌ Unexpected error: {str(e)}")
        exit(1)
