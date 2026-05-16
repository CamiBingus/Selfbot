import discord
from discord.ext import commands, tasks
from discord import app_commands
import json
import requests
import re
import asyncio
import aiohttp
import aiosqlite
import pytz
from datetime import datetime, timedelta
from cryptos import Litecoin

# Load configuration
try:
    with open('config.json', 'r') as f:
        config = json.load(f)
except FileNotFoundError:
    print("Error: config.json not found! Please create one using the template.")
    exit()

# Extract config values
TOKEN = config.get("DISCORD_BOT_TOKEN", "")
OWNER_ID = config.get("OWNER_ID", 0)
LTC_PRIVATE_KEY = config.get("LTC_PRIVATE_KEY", "")
LTC_ADDRESS = config.get("LTC_ADDRESS", "")
PAYPAL_EMAIL = config.get("PAYPAL_EMAIL", "")
PAYPAL_TOS = config.get("PAYPAL_TOS", "")

# Initialize Bot
class MyBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=discord.Intents.default())
        self.session = None
        self.db = None

    async def setup_hook(self):
        # Initialize shared aiohttp session for memory efficiency
        self.session = aiohttp.ClientSession()
        
        # Initialize SQLite Database for persistent reminders and new features
        self.db = await aiosqlite.connect("reminders.db")
        await self.db.execute('''
            CREATE TABLE IF NOT EXISTS reminders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                trigger_time INTEGER,
                task TEXT
            )
        ''')
        await self.db.execute('''
            CREATE TABLE IF NOT EXISTS notes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                title TEXT,
                content TEXT
            )
        ''')
        await self.db.execute('''
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        ''')
        await self.db.commit()
        
        # Start the background tasks
        self.check_reminders.start()
        self.ltc_tracker.start()

        # Syncing commands to allow them to be user-installable
        await self.tree.sync()
        print(f"Synced slash commands for {self.user}")
        
    async def close(self):
        # Clean up resources
        if self.session:
            await self.session.close()
        if self.db:
            await self.db.close()
        await super().close()

    @tasks.loop(seconds=60)
    async def check_reminders(self):
        if not self.db:
            return
            
        now = int(datetime.now(pytz.timezone('Europe/Berlin')).timestamp())
        
        # Fetch due reminders
        async with self.db.execute("SELECT id, user_id, task FROM reminders WHERE trigger_time <= ?", (now,)) as cursor:
            rows = await cursor.fetchall()
            
            for row in rows:
                reminder_id, user_id, task = row
                try:
                    user = await self.fetch_user(user_id)
                    if user:
                        await user.send(f"🔔 **Reminder:** {task}")
                except Exception as e:
                    print(f"Failed to send reminder to {user_id}: {e}")
                    
                # Delete the processed reminder
                await self.db.execute("DELETE FROM reminders WHERE id = ?", (reminder_id,))
                await self.db.commit()

    @check_reminders.before_loop
    async def before_check_reminders(self):
        await self.wait_until_ready()

    @tasks.loop(minutes=3)
    async def ltc_tracker(self):
        if not self.db or not LTC_ADDRESS:
            return
            
        try:
            # Check if there is a logging channel set
            async with self.db.execute("SELECT value FROM settings WHERE key = 'ltc_log_channel'") as cursor:
                row = await cursor.fetchone()
                if not row:
                    return
                channel_id = int(row[0])
                
            # Fetch transactions for our address
            url = f"https://api.blockcypher.com/v1/ltc/main/addrs/{LTC_ADDRESS}/full?limit=1"
            async with self.session.get(url) as resp:
                if resp.status != 200:
                    return
                data = await resp.json()
                
            txs = data.get('txs', [])
            if not txs:
                return
                
            latest_tx = txs[0]
            tx_hash = latest_tx['hash']
            
            # Check if we already logged this transaction
            async with self.db.execute("SELECT value FROM settings WHERE key = 'last_ltc_tx'") as cursor:
                last_tx_row = await cursor.fetchone()
                if last_tx_row and last_tx_row[0] == tx_hash:
                    return # Already processed
                    
            # We have a new transaction! Let's analyze it
            # Determine if it's sending or receiving by looking at inputs/outputs
            # A simple heuristic: if our address is in inputs, we sent it.
            is_send = False
            for input_obj in latest_tx.get('inputs', []):
                if LTC_ADDRESS in input_obj.get('addresses', []):
                    is_send = True
                    break
                    
            amount = latest_tx.get('total', 0) / 100000000
            
            # Send notification to channel
            channel = self.get_channel(channel_id)
            if channel:
                embed = discord.Embed(
                    title="📤 Litecoin Sent!" if is_send else "📥 Litecoin Received!",
                    color=discord.Color.red() if is_send else discord.Color.green()
                )
                embed.add_field(name="Amount", value=f"**{amount} LTC**", inline=False)
                embed.add_field(name="TXID", value=f"[{tx_hash}](https://live.blockcypher.com/ltc/tx/{tx_hash})", inline=False)
                await channel.send(embed=embed)
                
            # Update the last seen tx in db
            await self.db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('last_ltc_tx', ?)", (tx_hash,))
            await self.db.commit()
            
        except Exception as e:
            print(f"LTC Tracker Error: {e}")

    @ltc_tracker.before_loop
    async def before_ltc_tracker(self):
        await self.wait_until_ready()

bot = MyBot()
ltc_crypto = Litecoin()

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")
    print("Bot is ready!")

def is_owner():
    def predicate(interaction: discord.Interaction) -> bool:
        return interaction.user.id == OWNER_ID
    return app_commands.check(predicate)

@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.CheckFailure):
        # Fail silently or tell them they lack permission, as requested
        await interaction.response.send_message("❌ You do not have permission to use this command.")
    else:
        # Generic error fallback
        if not interaction.response.is_done():
            await interaction.response.send_message(f"An error occurred: {error}")

# -----------------
# 1. Calculator
# -----------------
def safe_calc(expression: str) -> float:
    # A very basic, safe calculator parser supporting +, -, *, /, % and percentages
    # Examples:
    # "100 + 5%" -> 100 + (100 * 0.05) -> 105
    # "50 * 10%" -> 50 * 0.10 -> 5
    # "100 - 20" -> 80
    
    # First, handle percentages. We'll support the format "X [+-] Y%" and "X * Y%"
    # Remove all spaces for easier parsing
    expr = expression.replace(" ", "")
    
    # Regex to find patterns like `NUMBER + NUMBER%` or `NUMBER - NUMBER%`
    match = re.fullmatch(r"([0-9.]+)([+-])([0-9.]+)%", expr)
    if match:
        base = float(match.group(1))
        op = match.group(2)
        pct_val = float(match.group(3))
        
        amount = base * (pct_val / 100.0)
        if op == "+":
            return base + amount
        elif op == "-":
            return base - amount

    # Regex to find `NUMBER * NUMBER%`
    match = re.fullmatch(r"([0-9.]+)\*([0-9.]+)%", expr)
    if match:
        base = float(match.group(1))
        pct_val = float(match.group(2))
        return base * (pct_val / 100.0)

    # If it's a regular math expression, strictly validate to prevent code injection
    if not re.fullmatch(r"^[0-9.+\-*/() ]+$", expression):
        raise ValueError("Invalid characters in expression.")
    
    # At this point, the string only contains numbers and basic operators.
    # We can safely use eval, but we still disable builtins.
    return float(eval(expression, {"__builtins__": {}}))

@discord.app_commands.allowed_installs(guilds=True, users=True)
@discord.app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@bot.tree.command(name="calc", description="Calculate a math expression (supports basic math and percentages like 100 + 5%)")
@is_owner()
async def calc(interaction: discord.Interaction, expression: str):
    try:
        result = safe_calc(expression)
        await interaction.response.send_message(f"**Expression:** `{expression}`\n**Result:** `{result}`")
    except Exception as e:
        await interaction.response.send_message(f"❌ Error calculating expression. Make sure it's a valid math expression like `100 + 5%` or `50 * 2`.")

# -----------------
# 2. PayPal
# -----------------
@discord.app_commands.allowed_installs(guilds=True, users=True)
@discord.app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@bot.tree.command(name="paypal", description="Get the PayPal email and terms of service")
@is_owner()
async def paypal(interaction: discord.Interaction):
    embed = discord.Embed(title="PayPal Information", color=discord.Color.blue())
    embed.add_field(name="Email", value=f"`{PAYPAL_EMAIL}`\n*(Click to copy)*", inline=False)
    embed.add_field(name="Terms of Service", value=PAYPAL_TOS, inline=False)
    await interaction.response.send_message(embed=embed)

# -----------------
# 3. Litecoin Address
# -----------------
@discord.app_commands.allowed_installs(guilds=True, users=True)
@discord.app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@bot.tree.command(name="ltc_address", description="Show the Litecoin address")
@is_owner()
async def ltc_address_cmd(interaction: discord.Interaction):
    embed = discord.Embed(title="Litecoin Address", color=discord.Color.light_gray())
    embed.add_field(name="Address", value=f"`{LTC_ADDRESS}`\n*(Click to copy)*", inline=False)
    await interaction.response.send_message(embed=embed)

# -----------------
# 4. Litecoin TX lookup
# -----------------
def fetch_tx(txid: str):
    url = f"https://api.blockcypher.com/v1/ltc/main/txs/{txid}"
    return requests.get(url)

@discord.app_commands.allowed_installs(guilds=True, users=True)
@discord.app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@bot.tree.command(name="ltc_tx", description="Look up a Litecoin transaction by TXID")
@is_owner()
async def ltc_tx(interaction: discord.Interaction, txid: str):
    await interaction.response.defer()
    try:
        # Run synchronous requests.get in executor to prevent blocking
        loop = asyncio.get_running_loop()
        response = await loop.run_in_executor(None, fetch_tx, txid)
        
        if response.status_code == 200:
            data = response.json()
            confirmations = data.get("confirmations", 0)
            total_sent = data.get("total", 0) / 100000000  # Convert satoshis to LTC
            fees = data.get("fees", 0) / 100000000
            
            embed = discord.Embed(title="Litecoin Transaction", url=f"https://live.blockcypher.com/ltc/tx/{txid}", color=discord.Color.green())
            embed.add_field(name="TXID", value=f"`{txid}`", inline=False)
            embed.add_field(name="Confirmations", value=str(confirmations), inline=True)
            embed.add_field(name="Total Transacted", value=f"{total_sent} LTC", inline=True)
            embed.add_field(name="Fees", value=f"{fees} LTC", inline=True)
            
            await interaction.followup.send(embed=embed)
        else:
            await interaction.followup.send(f"❌ Could not find transaction. BlockCypher returned status {response.status_code}.")
    except Exception as e:
        await interaction.followup.send(f"❌ Error looking up transaction: {e}")

# -----------------
# 5. Stealth Webhook
# -----------------
@discord.app_commands.allowed_installs(guilds=True, users=True)
@discord.app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@bot.tree.command(name="webhook_send", description="Send a stealth webhook message")
@is_owner()
async def webhook_send(interaction: discord.Interaction, url: str, message: str):
    await interaction.response.defer()
    try:
        payload = {"content": message}
        async with bot.session.post(url, json=payload) as response:
            if response.status in (200, 204):
                await interaction.followup.send("✅ Webhook sent successfully.")
            else:
                await interaction.followup.send(f"❌ Failed to send. Status: {response.status}")
    except Exception as e:
        await interaction.followup.send(f"❌ Error sending webhook: {str(e)}")

from discord.app_commands import Choice

# -----------------
# 6. Master Converter
# -----------------
@discord.app_commands.allowed_installs(guilds=True, users=True)
@discord.app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@bot.tree.command(name="cv", description="Convert between Fiat (EUR/USD) and Crypto (LTC)")
@app_commands.choices(from_curr=[
    Choice(name="EUR", value="EUR"),
    Choice(name="USD", value="USD"),
    Choice(name="LTC", value="LTC")
], to_curr=[
    Choice(name="EUR", value="EUR"),
    Choice(name="USD", value="USD"),
    Choice(name="LTC", value="LTC")
])
@is_owner()
async def cv(interaction: discord.Interaction, amount: float, from_curr: str, to_curr: str):
    if from_curr == to_curr:
        return await interaction.response.send_message(f"Amount: {amount} {to_curr}")

    await interaction.response.defer()
    
    try:
        # We need to get exchange rates
        # EUR to USD or USD to EUR
        fiat_rate = None
        if from_curr in ["EUR", "USD"] and to_curr in ["EUR", "USD"]:
            async with bot.session.get(f"https://api.frankfurter.app/latest?amount={amount}&from={from_curr}&to={to_curr}") as resp:
                data = await resp.json()
                converted = data["rates"][to_curr]
                
        # If LTC is involved, we fetch LTC price in EUR and USD
        elif "LTC" in [from_curr, to_curr]:
            async with bot.session.get("https://api.coingecko.com/api/v3/simple/price?ids=litecoin&vs_currencies=usd,eur") as resp:
                data = await resp.json()
                ltc_usd = data["litecoin"]["usd"]
                ltc_eur = data["litecoin"]["eur"]
                
                if from_curr == "LTC":
                    rate = ltc_usd if to_curr == "USD" else ltc_eur
                    converted = amount * rate
                else:
                    rate = ltc_usd if from_curr == "USD" else ltc_eur
                    converted = amount / rate

        embed = discord.Embed(title="Currency Conversion", color=discord.Color.blue())
        embed.add_field(name="From", value=f"{amount:,.2f} **{from_curr}**" if from_curr != "LTC" else f"{amount:,.4f} **{from_curr}**", inline=True)
        embed.add_field(name="To", value=f"{converted:,.2f} **{to_curr}**" if to_curr != "LTC" else f"{converted:,.4f} **{to_curr}**", inline=True)
        
        await interaction.followup.send(embed=embed)

    except Exception as e:
        await interaction.followup.send(f"❌ Error during conversion: {e}")

# -----------------
# 7. Persistent Reminders
# -----------------
@discord.app_commands.allowed_installs(guilds=True, users=True)
@discord.app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@bot.tree.command(name="remind", description="Set a persistent reminder")
@is_owner()
async def remind(interaction: discord.Interaction, time_string: str, task: str):
    # Parse time_string like '10m', '2h', '1d'
    match = re.fullmatch(r"(\d+)([mhd])", time_string.lower())
    if not match:
        return await interaction.response.send_message("❌ Invalid format. Use `10m`, `2h`, `1d`.")
    
    amount = int(match.group(1))
    unit = match.group(2)
    
    delta = timedelta()
    if unit == 'm':
        delta = timedelta(minutes=amount)
    elif unit == 'h':
        delta = timedelta(hours=amount)
    elif unit == 'd':
        delta = timedelta(days=amount)
        
    tz = pytz.timezone('Europe/Berlin')
    trigger_time = int((datetime.now(tz) + delta).timestamp())
    
    # Save to db
    try:
        await bot.db.execute("INSERT INTO reminders (user_id, trigger_time, task) VALUES (?, ?, ?)", 
                             (interaction.user.id, trigger_time, task))
        await bot.db.commit()
        await interaction.response.send_message(f"✅ Reminder set for {time_string} from now: `{task}`")
    except Exception as e:
        await interaction.response.send_message(f"❌ Failed to set reminder: {e}")

# -----------------
# 8. Portfolio Tracker
# -----------------
@discord.app_commands.allowed_installs(guilds=True, users=True)
@discord.app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@bot.tree.command(name="portfolio", description="Check your live Litecoin balance and its Fiat value")
@is_owner()
async def portfolio(interaction: discord.Interaction):
    await interaction.response.defer()
    try:
        # Get live balance from Blockcypher
        balance_url = f"https://api.blockcypher.com/v1/ltc/main/addrs/{LTC_ADDRESS}/balance"
        async with bot.session.get(balance_url) as resp:
            if resp.status != 200:
                return await interaction.followup.send("❌ Could not fetch balance from Blockcypher.")
            data = await resp.json()
            ltc_balance = data.get("balance", 0) / 100000000

        # Get live price from CoinGecko
        price_url = "https://api.coingecko.com/api/v3/simple/price?ids=litecoin&vs_currencies=usd,eur"
        async with bot.session.get(price_url) as resp:
            if resp.status != 200:
                return await interaction.followup.send("❌ Could not fetch price from CoinGecko.")
            price_data = await resp.json()
            ltc_usd = price_data["litecoin"]["usd"]
            ltc_eur = price_data["litecoin"]["eur"]

        val_usd = ltc_balance * ltc_usd
        val_eur = ltc_balance * ltc_eur

        embed = discord.Embed(title="Crypto Portfolio", color=discord.Color.green())
        embed.add_field(name="LTC Balance", value=f"**{ltc_balance:,.6f} LTC**", inline=False)
        embed.add_field(name="Fiat Value", value=f"💵 ${val_usd:,.2f} USD\n💶 €{val_eur:,.2f} EUR", inline=False)
        embed.set_footer(text=f"Address: {LTC_ADDRESS}")

        await interaction.followup.send(embed=embed)
    except Exception as e:
        await interaction.followup.send(f"❌ Error fetching portfolio: {e}")

# -----------------
# 9. TempMail
# -----------------
@discord.app_commands.allowed_installs(guilds=True, users=True)
@discord.app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@bot.tree.command(name="tempmail", description="Generate or check a disposable email")
@app_commands.choices(action=[
    Choice(name="Generate New Email", value="gen"),
    Choice(name="Check Inbox", value="inbox"),
])
@is_owner()
async def tempmail(interaction: discord.Interaction, action: str, email: str = None, message_id: str = None):
    await interaction.response.defer()
    
    try:
        if action == "gen":
            async with bot.session.get("https://www.1secmail.com/api/v1/?action=genRandomMailbox&count=1") as resp:
                data = await resp.json()
                new_email = data[0]
                
            embed = discord.Embed(title="TempMail Generated", description=f"**Email:** `{new_email}`\n\nTo check the inbox, use `/tempmail action:Check Inbox email:{new_email}`", color=discord.Color.blue())
            await interaction.followup.send(embed=embed)
            
        elif action == "inbox":
            if not email or "@" not in email:
                return await interaction.followup.send("❌ You must provide the generated `email` to check its inbox.")
                
            user, domain = email.split("@")
            async with bot.session.get(f"https://www.1secmail.com/api/v1/?action=getMessages&login={user}&domain={domain}") as resp:
                messages = await resp.json()
                
            if not messages:
                return await interaction.followup.send("📭 Inbox is empty.")
                
            embed = discord.Embed(title=f"Inbox for {email}", color=discord.Color.orange())
            for msg in messages[:5]:  # Show top 5
                # The API allows reading by passing the ID
                embed.add_field(name=f"ID: {msg['id']} | From: {msg['from']}", value=f"**Subject:** {msg['subject']}\n*Date: {msg['date']}*", inline=False)
            
            embed.set_footer(text="To read a message, run /tempmail_read <email> <id>")
            await interaction.followup.send(embed=embed)

    except Exception as e:
        await interaction.followup.send(f"❌ TempMail Error: {e}")

@discord.app_commands.allowed_installs(guilds=True, users=True)
@discord.app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@bot.tree.command(name="tempmail_read", description="Read a specific message from your TempMail")
@is_owner()
async def tempmail_read(interaction: discord.Interaction, email: str, message_id: str):
    await interaction.response.defer()
    try:
        if "@" not in email:
            return await interaction.followup.send("❌ Invalid email format.")
            
        user, domain = email.split("@")
        async with bot.session.get(f"https://www.1secmail.com/api/v1/?action=readMessage&login={user}&domain={domain}&id={message_id}") as resp:
            data = await resp.json()
            
        if "body" not in data:
            return await interaction.followup.send("❌ Message not found.")
            
        # Clean the body and truncate if necessary
        content = data['textBody'] if data['textBody'] else data['body']
        if len(content) > 1024:
            content = content[:1020] + "..."
            
        embed = discord.Embed(title=f"Message ID: {message_id}", description=content, color=discord.Color.green())
        embed.set_author(name=f"From: {data['from']}")
        embed.add_field(name="Subject", value=data['subject'], inline=False)
        await interaction.followup.send(embed=embed)
    except Exception as e:
        await interaction.followup.send(f"❌ Error reading message: {e}")


# -----------------
# 10. Notes
# -----------------
@discord.app_commands.allowed_installs(guilds=True, users=True)
@discord.app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@bot.tree.command(name="notes", description="Manage personal notes")
@app_commands.choices(action=[
    Choice(name="Add Note", value="add"),
    Choice(name="List Notes", value="list"),
    Choice(name="Delete Note", value="delete"),
])
@is_owner()
async def notes(interaction: discord.Interaction, action: str, title: str = None, content: str = None, note_id: int = None):
    try:
        if action == "add":
            if not title or not content:
                return await interaction.response.send_message("❌ You must provide a `title` and `content` to add a note.")
            await bot.db.execute("INSERT INTO notes (user_id, title, content) VALUES (?, ?, ?)", (interaction.user.id, title, content))
            await bot.db.commit()
            await interaction.response.send_message(f"✅ Note **{title}** saved!")
            
        elif action == "list":
            async with bot.db.execute("SELECT id, title, content FROM notes WHERE user_id = ?", (interaction.user.id,)) as cursor:
                rows = await cursor.fetchall()
                
            if not rows:
                return await interaction.response.send_message("📭 You have no notes saved.")
                
            embed = discord.Embed(title="Your Notes", color=discord.Color.gold())
            for row in rows:
                n_id, n_title, n_content = row
                if len(n_content) > 100:
                    n_content = n_content[:97] + "..."
                embed.add_field(name=f"[{n_id}] {n_title}", value=n_content, inline=False)
            await interaction.response.send_message(embed=embed)
            
        elif action == "delete":
            if not note_id:
                return await interaction.response.send_message("❌ You must provide the `note_id` to delete a note.")
            await bot.db.execute("DELETE FROM notes WHERE id = ? AND user_id = ?", (note_id, interaction.user.id))
            await bot.db.commit()
            await interaction.response.send_message(f"🗑️ Note [{note_id}] deleted.")
            
    except Exception as e:
        if not interaction.response.is_done():
            await interaction.response.send_message(f"❌ Error managing notes: {e}")
        else:
            await interaction.followup.send(f"❌ Error managing notes: {e}")

# -----------------
# 11. Steam Lookup
# -----------------
@discord.app_commands.allowed_installs(guilds=True, users=True)
@discord.app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@bot.tree.command(name="steam_lookup", description="Look up a Steam user by their vanity URL or Steam ID")
@is_owner()
async def steam_lookup(interaction: discord.Interaction, query: str):
    await interaction.response.defer()
    try:
        url = f"https://playerdb.co/api/player/steam/{query}"
        async with bot.session.get(url) as resp:
            if resp.status != 200:
                return await interaction.followup.send("❌ Could not find Steam user. Ensure the ID or username is correct.")
            
            data = await resp.json()
            if not data.get("success"):
                return await interaction.followup.send("❌ Steam user not found.")
                
            player = data["data"]["player"]
            meta = player.get("meta", {})
            
            embed = discord.Embed(title=f"Steam Profile: {player.get('username')}", url=f"https://steamcommunity.com/profiles/{player.get('id')}", color=discord.Color.dark_blue())
            
            if player.get('avatar'):
                embed.set_thumbnail(url=player.get('avatar'))
                
            if meta.get('realname'):
                embed.add_field(name="Real Name", value=meta.get('realname'), inline=True)
                
            if meta.get('loccountrycode'):
                embed.add_field(name="Country", value=meta.get('loccountrycode'), inline=True)
                
            embed.add_field(name="Steam ID", value=player.get('id'), inline=False)
            
            await interaction.followup.send(embed=embed)
    except Exception as e:
        await interaction.followup.send(f"❌ Error looking up Steam profile: {e}")


# -----------------
# 12. LTC Tracker Setup
# -----------------
@discord.app_commands.allowed_installs(guilds=True, users=True)
@discord.app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@bot.tree.command(name="set_ltc_log", description="Set the channel where LTC Send/Receive notifications will be sent")
@is_owner()
async def set_ltc_log(interaction: discord.Interaction, channel_id: str):
    if not channel_id.isdigit():
        return await interaction.response.send_message("❌ Please provide a valid numeric Channel ID.")
        
    try:
        await bot.db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('ltc_log_channel', ?)", (channel_id,))
        await bot.db.commit()
        await interaction.response.send_message(f"✅ LTC tracking logs will now be sent to <#{channel_id}>.")
    except Exception as e:
        await interaction.response.send_message(f"❌ Error setting log channel: {e}")

# -----------------
# 13. Help Command
# -----------------
@discord.app_commands.allowed_installs(guilds=True, users=True)
@discord.app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@bot.tree.command(name="help", description="Show all available commands and what they do")
@is_owner()
async def help_cmd(interaction: discord.Interaction):
    embed = discord.Embed(title="🤖 Selfbot Command Menu", description="Here are all the utility and crypto commands currently loaded:", color=discord.Color.purple())
    
    commands_list = [
        ("`/calc <expr>`", "Calculate math expressions safely, including percentages (e.g. `100 + 5%`)."),
        ("`/paypal`", "Show your configured PayPal email and Terms of Service."),
        ("`/ltc_address`", "Show your static public Litecoin address."),
        ("`/ltc_tx <txid>`", "Look up a Litecoin transaction status manually."),
        ("`/ltc_send <address> <amount>`", "Prompt confirmation to send LTC directly from your wallet via private key."),
        ("`/portfolio`", "View your live LTC wallet balance converted to USD and EUR."),
        ("`/set_ltc_log <channel_id>`", "Set a Discord channel to receive automatic notifications whenever your LTC address sends or receives money."),
        ("`/cv <amount> <from> <to>`", "Convert seamlessly between EUR, USD, and LTC."),
        ("`/webhook_send <url> <msg>`", "Send a stealth payload message to a webhook without logging the endpoint."),
        ("`/remind <time> <task>`", "Set a persistent reminder using timeframes like `10m`, `2h`, `1d`."),
        ("`/notes <action> <title> <content>`", "Manage secure, private text snippets and notes across devices."),
        ("`/tempmail <action>`", "Generate a disposable email and read its inbox directly on Discord."),
        ("`/steam_lookup <query>`", "Lookup Steam ID, real name, and avatar by providing a vanity URL or ID.")
    ]
    
    for cmd, desc in commands_list:
        embed.add_field(name=cmd, value=desc, inline=False)
        
    embed.set_footer(text="All commands are restricted to the bot owner.")
    await interaction.response.send_message(embed=embed)


# -----------------
# 14. Litecoin Send
# -----------------
class ConfirmSendView(discord.ui.View):
    def __init__(self, to_address: str, amount_ltc: float):
        super().__init__(timeout=60)
        self.to_address = to_address
        self.amount_ltc = amount_ltc
        self.value = None

    @discord.ui.button(label="Confirm Send", style=discord.ButtonStyle.green)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != OWNER_ID:
            return await interaction.response.send_message("❌ You are not the owner.")
        self.value = True
        self.stop()
        await interaction.response.edit_message(content="Processing transaction... please wait.", embed=None, view=None)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.red)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != OWNER_ID:
            return await interaction.response.send_message("❌ You are not the owner.")
        self.value = False
        self.stop()
        await interaction.response.edit_message(content="Transaction cancelled.", embed=None, view=None)

def send_tx(private_key, from_address, to_address, amount_satoshis):
    return ltc_crypto.send(private_key, from_address, to_address, amount_satoshis)

@discord.app_commands.allowed_installs(guilds=True, users=True)
@discord.app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@bot.tree.command(name="ltc_send", description="Send Litecoin to an address")
@is_owner()
async def ltc_send(interaction: discord.Interaction, to_address: str, amount: float):
    embed = discord.Embed(title="Confirm Transaction", color=discord.Color.orange())
    embed.add_field(name="Recipient", value=f"`{to_address}`", inline=False)
    embed.add_field(name="Amount", value=f"**{amount} LTC**", inline=False)
    embed.set_footer(text="Please verify the address. Transactions cannot be reversed.")
    
    view = ConfirmSendView(to_address, amount)
    await interaction.response.send_message(embed=embed, view=view)
    
    await view.wait()
    
    if view.value is True:
        try:
            amount_satoshis = int(amount * 100000000)
            
            # Run synchronous ltc_crypto.send in executor to prevent blocking
            loop = asyncio.get_running_loop()
            tx_hash = await loop.run_in_executor(
                None, 
                send_tx, 
                LTC_PRIVATE_KEY, 
                LTC_ADDRESS, 
                to_address, 
                amount_satoshis
            )
            
            if tx_hash:
                success_embed = discord.Embed(title="Transaction Sent!", color=discord.Color.green())
                success_embed.add_field(name="TXID", value=f"`{tx_hash['data']['tx']['hash']}`" if isinstance(tx_hash, dict) else f"`{tx_hash}`", inline=False)
                await interaction.edit_original_response(content=None, embed=success_embed)
            else:
                await interaction.edit_original_response(content="❌ Failed to broadcast transaction.")
        except Exception as e:
            await interaction.edit_original_response(content=f"❌ Error sending LTC: {e}")

if __name__ == "__main__":
    if not TOKEN or TOKEN == "YOUR_DISCORD_BOT_TOKEN_HERE":
        print("Please configure your DISCORD_BOT_TOKEN in config.json")
    else:
        bot.run(TOKEN)
