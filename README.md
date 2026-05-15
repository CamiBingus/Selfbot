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

Once running, you can use the following slash commands in any server or DM:
- `/calc <expression>`: Evaluate math expressions.
- `/paypal`: Shows your PayPal email and TOS in a copyable embed.
- `/ltc_address`: Shows your Litecoin address in a copyable embed.
- `/ltc_tx <txid>`: Checks the status of an LTC transaction via BlockCypher.
- `/ltc_send <address> <amount>`: Prompts a confirmation to send LTC from your wallet.

**Security Warning:** Because this bot has access to your private key, ensure your Zap-Hosting account is secure. Do not share the config.json file with anyone.
