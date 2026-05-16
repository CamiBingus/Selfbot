# User-Installable Discord Bot

This bot provides features to perform basic calculations, display PayPal and Litecoin addresses, lookup LTC transactions, and send LTC securely. It is designed as a **User-Installable App**, meaning you can install it to your personal Discord account and use its slash commands anywhere.

## Prerequisites

- **Python 3.10+**
- A Discord Developer Account (to create the bot)

## Discord Developer Portal Setup (User-Installable App)

1. Go to the [Discord Developer Portal](https://discord.com/developers/applications).
2. Click **New Application** and give it a name.
3. Go to the **Bot** tab on the left.
4. Uncheck **Public Bot** (since only you should be using this for your wallet).
5. Scroll down to **Privileged Gateway Intents** and enable **Message Content Intent**.
6. Click **Reset Token** and copy your token. Keep this safe!
7. **To make it User-Installable**:
   - Go to the **Installation** tab on the left.
   - Under **Installation Contexts**, check **User Install**.
   - Under **Default Install Settings**, select **User Install** and add the scope `applications.commands`.
   - Copy the "Discord Provided Link" and open it in your browser to authorize the bot to your personal account.

## Zap-Hosting Setup Instructions

1. Upload the following files to your Zap-Hosting server:
<<<<<<< HEAD
   - `bot.py`
=======
   - `main.py`
>>>>>>> 8e197fa (Fix Zap-Hosting entrypoint error by renaming bot.py to main.py)
   - `config.json`
   - `requirements.txt`

2. Open the `config.json` file and fill in your details:
   - `DISCORD_BOT_TOKEN`: The token you copied from the Developer Portal.
   - `OWNER_ID`: Your personal Discord User ID. (This ensures only YOU can run the bot commands).
   - `LTC_PRIVATE_KEY`: Your Litecoin wallet's private key (WIF format). **Never share this!**
   - `LTC_ADDRESS`: Your public Litecoin address.
   - `PAYPAL_EMAIL`: The email you want displayed.
   - `PAYPAL_TOS`: Your Terms of Service text.

3. Access your Zap-Hosting terminal/console and run the following commands to install dependencies:
   ```bash
   pip install -r requirements.txt
   ```

4. Start the bot! Depending on Zap-Hosting's setup, you might just click "Start" or run:
   ```bash
<<<<<<< HEAD
   python3 bot.py
=======
   python3 main.py
>>>>>>> 8e197fa (Fix Zap-Hosting entrypoint error by renaming bot.py to main.py)
   ```

## Usage

Once running, you can use the following slash commands anywhere on Discord:

**💳 Finance & Crypto**
- `/portfolio`: Check your live Litecoin balance and its Fiat value in USD and EUR.
- `/set_ltc_log <channel_id>`: Set a channel to receive automatic logs when your LTC address sends or receives money.
- `/cv <amount> <from> <to>`: Convert between Fiat (EUR/USD) and Crypto (LTC).
- `/calc <expression>`: Safely calculate math expressions and percentages.
- `/paypal`: Retrieve the configured PayPal email and Terms of Service.
- `/ltc_address`: Output your public Litecoin address.
- `/ltc_tx <txid>`: Look up an LTC transaction status manually.
- `/ltc_send <address> <amount>`: Send Litecoin with a safe confirmation prompt.

**🛠 Productivity & Utility**
- `/help`: Display a help menu showing all these commands inside Discord.
- `/remind <time_string> <task>`: Set a persistent reminder (e.g. `10m`, `2h`, `1d`).
- `/notes <action>`: Securely add, list, or delete text snippets.
- `/tempmail <action>`: Generate a disposable email address and read its inbox.
- `/webhook_send <url> <message>`: Send a stealth POST message to a webhook.
- `/steam_lookup <query>`: Fetch public profile details of a Steam user.

**Security Warning:** Because this bot has access to your private key, ensure your Zap-Hosting account is secure. Do not share the config.json file with anyone.
