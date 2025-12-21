# 📱 Telegram Notifications Setup Guide

This guide will help you set up Telegram notifications for your V4 Player Singles betting alerts.

---

## 🚀 Quick Setup (5 minutes)

### **Step 1: Create a Telegram Bot**

1. Open Telegram and search for **@BotFather**
2. Start a chat and send: `/newbot`
3. Choose a name for your bot (e.g., "My Betting Alerts")
4. Choose a username (must end in 'bot', e.g., "mybetting_alerts_bot")
5. **Copy the bot token** - it looks like:
   ```
   123456789:ABCdefGHIjklMNOpqrsTUVwxyz123456789
   ```
6. Save this token - you'll need it in the next steps!

---

### **Step 2: Start a Conversation with Your Bot**

1. Search for your bot in Telegram using the username you created
2. Click **START** or send any message (e.g., "Hello")
3. This activates the bot and allows it to send you messages

---

### **Step 3: Get Your Chat ID**

#### **Option A: Use the provided script (Recommended)**

```bash
# Set your bot token as an environment variable
export TELEGRAM_BOT_TOKEN="your_bot_token_from_step_1"

# Run the script
python get_chat_id.py
```

The script will show you your Chat ID like this:
```
🎯 YOUR CHAT ID(S):
======================================================================

   123456789

✅ Use this in your GitHub secret:

   TELEGRAM_CHAT_ID = 123456789
```

#### **Option B: Use @userinfobot**

1. Search for **@userinfobot** on Telegram
2. Start a chat
3. It will immediately show your user ID (this is your Chat ID)

---

### **Step 4: Add Secrets to GitHub**

1. Go to your repository on GitHub
2. Click **Settings** → **Secrets and variables** → **Actions**
3. Click **New repository secret**

Add these two secrets:

**Secret 1:**
- **Name**: `TELEGRAM_BOT_TOKEN`
- **Value**: Your bot token from Step 1 (e.g., `123456789:ABCdefGHIjklMNOpqrsTUVwxyz123456789`)

**Secret 2:**
- **Name**: `TELEGRAM_CHAT_ID`
- **Value**: Your chat ID from Step 3 (e.g., `123456789`)

---

### **Step 5: Test It!**

#### **Test Locally:**

```bash
# Set environment variables
export TELEGRAM_BOT_TOKEN="your_bot_token"
export TELEGRAM_CHAT_ID="your_chat_id"

# Run the V4 script
python "scripts/value_bets/V4 Bets/V4 player singles/player_stats_v4.py"
```

You should receive a Telegram message if there are any picks!

#### **Test on GitHub Actions:**

1. Go to **Actions** tab
2. Select **"Player Stats V4 with Telegram"**
3. Click **"Run workflow"** → **"Run workflow"**
4. Wait a minute and check your Telegram!

---

## 🔧 Troubleshooting

### **Issue: "No messages found" when running get_chat_id.py**

**Solution:** Make sure you've sent a message to your bot first!
1. Open Telegram
2. Find your bot
3. Click START or send "Hello"
4. Run the script again

---

### **Issue: "Failed to connect to Telegram API"**

**Possible causes:**
1. **Wrong bot token** - Double-check you copied it correctly from BotFather
2. **Network issue** - Check your internet connection
3. **Token not set** - Make sure you exported the environment variable

---

### **Issue: "Telegram notifications disabled" in logs**

**Check:**
1. Secrets are added correctly in GitHub (Settings → Secrets)
2. Secret names are **exactly**:
   - `TELEGRAM_BOT_TOKEN` (not Bot_Token or token)
   - `TELEGRAM_CHAT_ID` (not Chat_Id or chatid)
3. No extra spaces in the secret values

---

### **Issue: Bot sends notifications but they don't arrive**

**Check:**
1. You haven't blocked the bot in Telegram
2. You've started a conversation with the bot (clicked START)
3. The Chat ID matches your account

---

## 📋 Environment Variables Reference

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `TELEGRAM_BOT_TOKEN` | Yes | None | Your bot token from BotFather |
| `TELEGRAM_CHAT_ID` | Yes | None | Your Telegram chat/user ID |
| `ENABLE_TELEGRAM` | No | `1` | Set to `0` to disable notifications |
| `MIN_DEC_PRICE` | No | `1.72` | Minimum odds threshold |

---

## 🧪 Testing Without Real Data

If you want to test the Telegram functionality without running the full analysis:

```python
# test_telegram.py
import os
import requests

TOKEN = os.environ['TELEGRAM_BOT_TOKEN']
CHAT_ID = os.environ['TELEGRAM_CHAT_ID']

url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
payload = {
    "chat_id": CHAT_ID,
    "text": "🎯 Test message from V4 Player Singles!\n\nIf you see this, your setup is working! ✅",
    "parse_mode": "HTML"
}

response = requests.post(url, json=payload, timeout=10)
print(f"Status: {response.status_code}")
print(f"Response: {response.json()}")
```

Run it:
```bash
export TELEGRAM_BOT_TOKEN="your_token"
export TELEGRAM_CHAT_ID="your_chat_id"
python test_telegram.py
```

---

## 📱 Advanced: Group Chat Notifications

To send notifications to a **group chat** instead of a personal chat:

1. Create a Telegram group
2. Add your bot to the group
3. Make the bot an admin (or allow it to post messages)
4. Send a message in the group
5. Run `get_chat_id.py` - it will show the group's Chat ID (usually a negative number like `-123456789`)
6. Use that negative number as your `TELEGRAM_CHAT_ID`

---

## 🔐 Security Best Practices

1. **Never commit tokens to Git** - Always use environment variables or GitHub Secrets
2. **Regenerate tokens** if accidentally exposed (talk to @BotFather, use `/revoke`)
3. **Use separate bots** for testing vs production
4. **Restrict bot permissions** - only give what's needed (send messages)

---

## 📊 Notification Format

Here's what you'll receive:

```
🎯 V4 Player Singles

⚽ Mohamed Salah [RW]
🏟 Liverpool vs Manchester United

📊 O1.5 shots
💰 Odds: 1.85

📈 V4 HR 11/13 (gate 11/13)
📊 Hit Rate: 11/13
📉 Average: 2.31
📋 Sequence: 3,2,4,1,3,2,3,2,4,1,2,3,1

🏆 Team ML: 1.75
🎲 Opp ML: 4.20
🔝 Opponent rank (shots conceded): 18

📅 2025-12-22 @ 17:30:00
🆔 Fixture: 23456
```

---

## 🎯 What Gets Notified?

- **Only NEW picks** - Each unique player/market/fixture combo is notified once
- **All qualifying picks** - Any bet meeting the V4 criteria (≥1.72 odds, high hit rates)
- **Tracking persists** - The `notified_players.json` file remembers what was sent

---

## ❓ FAQ

**Q: Will I get spammed with notifications?**
A: No! Each unique pick is only notified once. The system tracks what's been sent.

**Q: Can I get notifications for multiple bots/strategies?**
A: Yes! Use different bots for different strategies (V3, V4, etc.)

**Q: Can I disable notifications temporarily?**
A: Yes, set `ENABLE_TELEGRAM=0` in the workflow file

**Q: How do I clear the tracking file to get notifications again?**
A: Delete `data/value_bets/V4 Bets/V4 player singles/notified_players.json`

**Q: Can I customize the message format?**
A: Yes! Edit the `format_telegram_message()` function in `player_stats_v4.py`

---

## 📞 Need Help?

If you're still having issues:

1. Check the workflow logs in GitHub Actions
2. Look for error messages in the console output
3. Verify your secrets are set correctly
4. Test locally with environment variables first
5. Make sure you've started a conversation with your bot

---

**Happy betting! 🎰📊**
