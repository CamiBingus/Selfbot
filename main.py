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
        
        # Initialize SQLite Database for persistent reminders
        self.db = await aiosqlite.connect("reminders.db")
        await self.db.execute('''
            CREATE TABLE IF NOT EXISTS reminders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                trigger_time INTEGER,
                task TEXT
            )
        ''')
        await self.db.commit()
        
        # Start the background task for reminders
        self.check_reminders.start()

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
# 8. Litecoin Send
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
