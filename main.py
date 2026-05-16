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
import subprocess
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
        # We use a callable prefix to allow dynamic prefixes from the database
        super().__init__(command_prefix=self.get_dynamic_prefix, intents=discord.Intents.default())
        self.session = None
        self.db = None
        self.current_prefix = ","

    async def get_dynamic_prefix(self, bot, message):
        return self.current_prefix

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
        
        # Load the custom prefix
        async with self.db.execute("SELECT value FROM settings WHERE key = 'prefix'") as cursor:
            row = await cursor.fetchone()
            if row:
                self.current_prefix = row[0]

        # Start the background tasks
        self.check_reminders.start()
        self.ltc_tracker.start()

        # Syncing commands to allow them to be user-installable
        await self.tree.sync()
        print(f"Synced slash commands for {self.user} with prefix '{self.current_prefix}'")
        
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

def check_prefix_owner():
    async def predicate(ctx: commands.Context):
        return ctx.author.id == OWNER_ID
    return commands.check(predicate)

@bot.check
async def global_check(ctx: commands.Context):
    # Ensure all prefix commands are strictly restricted to the owner
    return ctx.author.id == OWNER_ID

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
# Prefix Commands & Media/Downloads
# -----------------
@bot.command(name="ytdl")
async def ytdl(ctx, url: str):
    msg = await ctx.send("⏳ Downloading YouTube video...")
    try:
        # Use yt-dlp to download and output the filename
        process = await asyncio.create_subprocess_shell(
            f'yt-dlp -f "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best" --max-filesize 24M -o "media_%(id)s.%(ext)s" --print filename {url}',
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await process.communicate()
        if process.returncode == 0:
            filename = stdout.decode().strip()
            await ctx.send(file=discord.File(filename))
            import os
            os.remove(filename)
            await msg.delete()
        else:
            await msg.edit(content=f"❌ Failed to download. Ensure the file is under 24MB.")
    except Exception as e:
        await msg.edit(content=f"❌ Error: {e}")

@bot.command(name="tiktok")
async def tiktok_dl(ctx, url: str):
    msg = await ctx.send("⏳ Downloading TikTok...")
    try:
        # yt-dlp automatically downloads watermark-free for TikTok
        process = await asyncio.create_subprocess_shell(
            f'yt-dlp --max-filesize 24M -o "media_%(id)s.%(ext)s" --print filename {url}',
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await process.communicate()
        if process.returncode == 0:
            filename = stdout.decode().strip()
            await ctx.send(file=discord.File(filename))
            import os
            os.remove(filename)
            await msg.delete()
        else:
            await msg.edit(content=f"❌ Failed to download TikTok.")
    except Exception as e:
        await msg.edit(content=f"❌ Error: {e}")

@bot.command(name="spotify")
async def spotify_dl(ctx, url: str):
    # Due to Spotify's DRM, standard yt-dlp doesn't download direct tracks.
    # We will use a public API or simply search yt-dlp via youtube
    msg = await ctx.send("⏳ Downloading Spotify Track...")
    try:
        # Search the spotify title on youtube and download audio
        process = await asyncio.create_subprocess_shell(
            f'yt-dlp -x --audio-format mp3 --max-filesize 24M -o "media_%(id)s.%(ext)s" --print filename "ytsearch1:{url}"',
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await process.communicate()
        if process.returncode == 0:
            filename = stdout.decode().strip().split('\n')[-1] # Sometimes prints multiple lines, grab the last one
            
            # Since we extract to mp3, the extension changes
            filename = filename.rsplit('.', 1)[0] + '.mp3'
            
            await ctx.send(file=discord.File(filename))
            import os
            os.remove(filename)
            await msg.delete()
        else:
            await msg.edit(content=f"❌ Failed to download Spotify track.")
    except Exception as e:
        await msg.edit(content=f"❌ Error: {e}")
bot.remove_command('help')

@bot.command(name="prefix")
async def change_prefix(ctx, new_prefix: str):
    bot.current_prefix = new_prefix
    await bot.db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('prefix', ?)", (new_prefix,))
    await bot.db.commit()
    await ctx.send(f"✅ Prefix changed to `{new_prefix}`")

@bot.command(name="paypal")
async def prefix_paypal(ctx):
    embed = discord.Embed(title="PayPal Information", color=discord.Color.blue())
    embed.add_field(name="Email", value=f"`{PAYPAL_EMAIL}`\n*(Click to copy)*", inline=False)
    embed.add_field(name="Terms of Service", value=PAYPAL_TOS, inline=False)
    await ctx.send(embed=embed)

@bot.command(name="ltc")
async def prefix_ltc(ctx):
    embed = discord.Embed(title="Litecoin Address", color=discord.Color.light_gray())
    embed.add_field(name="Address", value=f"`{LTC_ADDRESS}`\n*(Click to copy)*", inline=False)
    await ctx.send(embed=embed)

@bot.command(name="help")
async def prefix_help(ctx):
    embed = discord.Embed(title="🤖 Selfbot Command Menu", description=f"Current Prefix: `{bot.current_prefix}`\nAll commands are restricted to the bot owner.", color=discord.Color.purple())
    cat_crypto = "`portfolio`, `set_ltc_log`, `cv`, `calc`, `paypal`, `ltc_address`, `ltc_tx`, `ltc_send`, `tx_fee_calc`"
    cat_util = "`remind`, `notes`, `tempmail`, `webhook_send`, `steam_lookup`, `social_scan`, `name_check`, `metadata`, `speedtest`, `obfuscate`, `discord_token`, `weather`, `timezones`"
    cat_media = "`nitro_gen`, `fake_message`, `deepfry`, `tts_mp3`, `audio_extract`, `uwuify`, `zalgo`, `hack_screen`"
    cat_discord = "`server_clone`, `fake_activity`, `avatar`, `id_decode`, `banner_steal`, `guild_icon`, `whois_discord`, `embed_builder`, `role_color`, `server_stats`"
    cat_prefix = f"`{bot.current_prefix}ytdl`, `{bot.current_prefix}tiktok`, `{bot.current_prefix}spotify`, `{bot.current_prefix}steal`, `{bot.current_prefix}lock`, `{bot.current_prefix}prefix`"

    embed.add_field(name="💳 Finance & Crypto", value=cat_crypto, inline=False)
    embed.add_field(name="🛠 Utilities & OSINT", value=cat_util, inline=False)
    embed.add_field(name="🎨 Media & Trolling", value=cat_media, inline=False)
    embed.add_field(name="👑 Discord Power-User", value=cat_discord, inline=False)
    embed.add_field(name="⚡ Prefix Fast-Commands", value=cat_prefix, inline=False)
    await ctx.send(embed=embed)

@bot.command(name="txid")
async def prefix_txid(ctx, txid: str):
    try:
        loop = asyncio.get_running_loop()
        url = f"https://api.blockcypher.com/v1/ltc/main/txs/{txid}"
        response = await loop.run_in_executor(None, requests.get, url)
        
        if response.status_code == 200:
            data = response.json()
            confirmations = data.get("confirmations", 0)
            total_sent = data.get("total", 0) / 100000000
            fees = data.get("fees", 0) / 100000000
            
            embed = discord.Embed(title="Litecoin Transaction", url=f"https://live.blockcypher.com/ltc/tx/{txid}", color=discord.Color.green())
            embed.add_field(name="TXID", value=f"`{txid}`", inline=False)
            embed.add_field(name="Confirmations", value=str(confirmations), inline=True)
            embed.add_field(name="Total Transacted", value=f"{total_sent} LTC", inline=True)
            embed.add_field(name="Fees", value=f"{fees} LTC", inline=True)
            
            await ctx.send(embed=embed)
        else:
            await ctx.send(f"❌ Could not find transaction. BlockCypher returned status {response.status_code}.")
    except Exception as e:
        await ctx.send(f"❌ Error looking up transaction: {e}")

# -----------------
# Discord Power-User Features
# -----------------
@discord.app_commands.allowed_installs(guilds=True, users=True)
@discord.app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@bot.tree.command(name="server_clone", description="Clone server layout to Notes")
@is_owner()
async def server_clone(interaction: discord.Interaction, target_server_id: str):
    await interaction.response.defer()
    try:
        target = bot.get_guild(int(target_server_id))
        if not target:
            return await interaction.followup.send("❌ Server not found. Make sure I'm in it!")
            
        layout = {"name": target.name, "categories": {}}
        for category in target.categories:
            layout["categories"][category.name] = [c.name for c in category.channels]
            
        json_dump = json.dumps(layout, indent=2)
        if len(json_dump) > 3900:
            json_dump = json_dump[:3900] + "..."
            
        # Save to Notes
        await bot.db.execute("INSERT INTO notes (user_id, title, content) VALUES (?, ?, ?)", (interaction.user.id, f"Clone: {target.name}", json_dump))
        await bot.db.commit()
        await interaction.followup.send(f"✅ Successfully scraped layout for **{target.name}** and saved to `/notes`!")
    except Exception as e:
        await interaction.followup.send(f"❌ Error: {e}")

@bot.command(name="steal")
async def steal_emoji(ctx, emoji: discord.PartialEmoji, name: str = None):
    try:
        emoji_bytes = await emoji.read()
        new_emoji = await ctx.guild.create_custom_emoji(name=name or emoji.name, image=emoji_bytes)
        await ctx.send(f"✅ Stolen successfully: {new_emoji}")
    except Exception as e:
        await ctx.send(f"❌ Failed to steal emoji: {e}")

@bot.command(name="lock")
async def lock_channel(ctx):
    try:
        await ctx.channel.set_permissions(ctx.guild.default_role, send_messages=False)
        await ctx.send("🔒 Channel Locked.")
    except Exception as e:
        await ctx.send(f"❌ Failed to lock: {e}")

@discord.app_commands.allowed_installs(guilds=True, users=True)
@discord.app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@bot.tree.command(name="fake_activity", description="Set a custom rich presence activity")
@is_owner()
async def fake_activity(interaction: discord.Interaction, text: str):
    await bot.change_presence(activity=discord.Game(name=text))
    await interaction.response.send_message(f"✅ Activity set to: Playing **{text}**")

@discord.app_commands.allowed_installs(guilds=True, users=True)
@discord.app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@bot.tree.command(name="avatar", description="Get a user's avatar")
@is_owner()
async def grab_avatar(interaction: discord.Interaction, user_id: str):
    try:
        user = await bot.fetch_user(int(user_id))
        embed = discord.Embed(title=f"{user.name}'s Avatar", color=discord.Color.purple())
        embed.set_image(url=user.avatar.url if user.avatar else user.default_avatar.url)
        await interaction.response.send_message(embed=embed)
    except:
        await interaction.response.send_message("❌ User not found.")

@discord.app_commands.allowed_installs(guilds=True, users=True)
@discord.app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@bot.tree.command(name="id_decode", description="Decode a Discord Snowflake ID")
@is_owner()
async def id_decode(interaction: discord.Interaction, snowflake: str):
    try:
        discord_epoch = 1420070400000
        timestamp = (int(snowflake) >> 22) + discord_epoch
        dt = datetime.fromtimestamp(timestamp / 1000.0, tz=pytz.timezone('UTC'))
        await interaction.response.send_message(f"✅ ID Created: **{dt.strftime('%Y-%m-%d %H:%M:%S')} UTC**")
    except:
        await interaction.response.send_message("❌ Invalid Snowflake ID.")

@discord.app_commands.allowed_installs(guilds=True, users=True)
@discord.app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@bot.tree.command(name="banner_steal", description="Grab a user's or server's high-res banner")
@is_owner()
async def banner_steal(interaction: discord.Interaction, user_id: str = None):
    await interaction.response.defer()
    try:
        if user_id:
            user = await bot.fetch_user(int(user_id))
            if user.banner:
                embed = discord.Embed(title=f"{user.name}'s Banner", color=discord.Color.purple())
                embed.set_image(url=user.banner.url)
                return await interaction.followup.send(embed=embed)
            else:
                return await interaction.followup.send("❌ This user does not have a banner.")
        else:
            if interaction.guild and interaction.guild.banner:
                embed = discord.Embed(title=f"{interaction.guild.name}'s Banner", color=discord.Color.purple())
                embed.set_image(url=interaction.guild.banner.url)
                return await interaction.followup.send(embed=embed)
            else:
                return await interaction.followup.send("❌ This server does not have a banner.")
    except Exception as e:
        await interaction.followup.send(f"❌ Error: {e}")

@discord.app_commands.allowed_installs(guilds=True, users=True)
@discord.app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@bot.tree.command(name="guild_icon", description="Steal the current server's high-res icon")
@is_owner()
async def guild_icon(interaction: discord.Interaction):
    if interaction.guild and interaction.guild.icon:
        embed = discord.Embed(title=f"{interaction.guild.name} Icon", color=discord.Color.gold())
        embed.set_image(url=interaction.guild.icon.url)
        await interaction.response.send_message(embed=embed)
    else:
        await interaction.response.send_message("❌ This is not a server, or it has no icon.")

@discord.app_commands.allowed_installs(guilds=True, users=True)
@discord.app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@bot.tree.command(name="whois_discord", description="Get detailed Discord user info")
@is_owner()
async def whois_discord(interaction: discord.Interaction, user_id: str):
    await interaction.response.defer()
    try:
        user = await bot.fetch_user(int(user_id))
        embed = discord.Embed(title=f"User Info: {user.name}", color=discord.Color.blue())
        embed.set_thumbnail(url=user.avatar.url if user.avatar else user.default_avatar.url)
        embed.add_field(name="ID", value=user.id, inline=False)
        embed.add_field(name="Created At", value=user.created_at.strftime("%Y-%m-%d %H:%M:%S UTC"), inline=False)
        embed.add_field(name="Bot?", value="Yes" if user.bot else "No", inline=True)
        if user.public_flags:
            flags = [flag[0] for flag in user.public_flags if flag[1]]
            embed.add_field(name="Badges", value=", ".join(flags) if flags else "None", inline=True)
        await interaction.followup.send(embed=embed)
    except Exception as e:
        await interaction.followup.send(f"❌ User not found: {e}")

@discord.app_commands.allowed_installs(guilds=True, users=True)
@discord.app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@bot.tree.command(name="role_color", description="Instantly change a role's hex color")
@is_owner()
async def role_color(interaction: discord.Interaction, role: discord.Role, hex_code: str):
    try:
        hex_code = hex_code.lstrip("#")
        color = discord.Color(int(hex_code, 16))
        await role.edit(color=color)
        await interaction.response.send_message(f"✅ Role `{role.name}` color changed to `#{hex_code}`")
    except Exception as e:
        await interaction.response.send_message(f"❌ Failed to edit role: {e}")

@discord.app_commands.allowed_installs(guilds=True, users=True)
@discord.app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@bot.tree.command(name="server_stats", description="Display clean server stats")
@is_owner()
async def server_stats(interaction: discord.Interaction):
    if not interaction.guild:
        return await interaction.response.send_message("❌ You must run this in a server.")
    g = interaction.guild
    embed = discord.Embed(title=f"📊 {g.name} Stats", color=discord.Color.dark_theme())
    embed.add_field(name="Members", value=g.member_count, inline=True)
    embed.add_field(name="Channels", value=len(g.channels), inline=True)
    embed.add_field(name="Roles", value=len(g.roles), inline=True)
    embed.add_field(name="Boost Level", value=g.premium_tier, inline=True)
    embed.add_field(name="Created", value=g.created_at.strftime("%Y-%m-%d"), inline=False)
    if g.icon:
        embed.set_thumbnail(url=g.icon.url)
    await interaction.response.send_message(embed=embed)

@discord.app_commands.allowed_installs(guilds=True, users=True)
@discord.app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@bot.tree.command(name="embed_builder", description="Craft a custom rich embed")
@is_owner()
async def embed_builder(interaction: discord.Interaction, title: str, description: str, color_hex: str = "000000", image_url: str = None):
    try:
        color = discord.Color(int(color_hex.lstrip("#"), 16))
        embed = discord.Embed(title=title, description=description, color=color)
        if image_url:
            embed.set_image(url=image_url)
        await interaction.response.send_message(embed=embed)
    except Exception as e:
        await interaction.response.send_message(f"❌ Error building embed: {e}")

# -----------------
# Fun & Trolling Features
# -----------------
import random
import string

@discord.app_commands.allowed_installs(guilds=True, users=True)
@discord.app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@bot.tree.command(name="nitro_gen", description="Generate fake Discord Nitro links")
@is_owner()
async def nitro_gen(interaction: discord.Interaction, count: int = 5):
    links = []
    for _ in range(count):
        code = ''.join(random.choices(string.ascii_letters + string.digits, k=16))
        links.append(f"https://discord.gift/{code}")
    await interaction.response.send_message("\n".join(links))

@discord.app_commands.allowed_installs(guilds=True, users=True)
@discord.app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@bot.tree.command(name="fake_message", description="Create a fake message via Webhook")
@is_owner()
async def fake_message(interaction: discord.Interaction, user_id: str, text: str):
    await interaction.response.defer()
    try:
        user = await bot.fetch_user(int(user_id))
        webhook = await interaction.channel.create_webhook(name=user.name)
        avatar_url = user.avatar.url if user.avatar else user.default_avatar.url
        await webhook.send(text, username=user.name, avatar_url=avatar_url)
        await webhook.delete()
        await interaction.followup.send("✅ Fake message sent.", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"❌ Failed to fake message. (Do I have webhook perms?): {e}")

@discord.app_commands.allowed_installs(guilds=True, users=True)
@discord.app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@bot.tree.command(name="audio_extract", description="Strip the MP3 audio track from an MP4 video")
@is_owner()
async def audio_extract(interaction: discord.Interaction, video: discord.Attachment):
    await interaction.response.defer()
    try:
        if not video.content_type.startswith('video/'):
            return await interaction.followup.send("❌ Must be a video file.")
            
        video_bytes = await video.read()
        import tempfile
        import os
        
        with tempfile.NamedTemporaryFile(delete=False, suffix=".mp4") as temp_vid:
            temp_vid.write(video_bytes)
            temp_vid_path = temp_vid.name
            
        out_path = temp_vid_path.replace(".mp4", ".mp3")
        
        process = await asyncio.create_subprocess_shell(
            f'ffmpeg -i "{temp_vid_path}" -q:a 0 -map a "{out_path}" -y',
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        await process.communicate()
        
        if process.returncode == 0:
            await interaction.followup.send(file=discord.File(out_path, filename="extracted.mp3"))
        else:
            await interaction.followup.send("❌ FFmpeg failed to extract audio.")
            
        # Cleanup
        if os.path.exists(temp_vid_path): os.remove(temp_vid_path)
        if os.path.exists(out_path): os.remove(out_path)
        
    except Exception as e:
        await interaction.followup.send(f"❌ Error: {e}")

@discord.app_commands.allowed_installs(guilds=True, users=True)
@discord.app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@bot.tree.command(name="uwuify", description="Translate text into uwu speak")
@is_owner()
async def uwuify(interaction: discord.Interaction, text: str):
    text = text.replace('r', 'w').replace('R', 'W').replace('l', 'w').replace('L', 'W')
    text = text.replace('you', 'uwu').replace('You', 'Uwu')
    await interaction.response.send_message(f"{text} uwu")

@discord.app_commands.allowed_installs(guilds=True, users=True)
@discord.app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@bot.tree.command(name="zalgo", description="Corrupt text with demonic markings")
@is_owner()
async def zalgo(interaction: discord.Interaction, text: str):
    zalgo_chars = [chr(i) for i in range(0x0300, 0x036F + 1)]
    out = ""
    for char in text:
        out += char
        for _ in range(random.randint(2, 6)):
            out += random.choice(zalgo_chars)
    await interaction.response.send_message(out[:2000])

@discord.app_commands.allowed_installs(guilds=True, users=True)
@discord.app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@bot.tree.command(name="hack_screen", description="Output scrolling movie-hacker text")
@is_owner()
async def hack_screen(interaction: discord.Interaction):
    lines = [
        "[+] Bypassing mainframe firewall...",
        "[!] Accessing secure root directory...",
        "[-] Injecting payload to memory sector 0x4B3A",
        "[+] Decrypting RSA-4096 hash...",
        "[!] Privilege escalation successful (root/UID 0)",
        "[-] Scrubbing proxy logs...",
        "[+] Establishing persistent reverse shell...",
        "[!] Target compromised. Connection established."
    ]
    await interaction.response.send_message("```yaml\n" + "\n".join(lines) + "\n```")


from PIL import Image, ImageEnhance
import io

@discord.app_commands.allowed_installs(guilds=True, users=True)
@discord.app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@bot.tree.command(name="deepfry", description="Deepfry an attached image")
@is_owner()
async def deepfry(interaction: discord.Interaction, attachment: discord.Attachment):
    await interaction.response.defer()
    try:
        if not attachment.content_type.startswith('image/'):
            return await interaction.followup.send("❌ Must be an image.")
            
        image_bytes = await attachment.read()
        img = Image.open(io.BytesIO(image_bytes))
        img = img.convert('RGB')
        
        # Deepfry magic
        img = ImageEnhance.Color(img).enhance(3.0)
        img = ImageEnhance.Contrast(img).enhance(3.0)
        img = ImageEnhance.Sharpness(img).enhance(5.0)
        
        output = io.BytesIO()
        img.save(output, format="JPEG", quality=10)
        output.seek(0)
        
        await interaction.followup.send(file=discord.File(output, filename="deepfry.jpg"))
    except Exception as e:
        await interaction.followup.send(f"❌ Error: {e}")

from gtts import gTTS

@discord.app_commands.allowed_installs(guilds=True, users=True)
@discord.app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@bot.tree.command(name="tts_mp3", description="Generate a TTS audio file")
@is_owner()
async def tts_mp3(interaction: discord.Interaction, text: str):
    await interaction.response.defer()
    try:
        tts = gTTS(text=text, lang='en')
        output = io.BytesIO()
        tts.write_to_fp(output)
        output.seek(0)
        await interaction.followup.send(file=discord.File(output, filename="tts.mp3"))
    except Exception as e:
        await interaction.followup.send(f"❌ Error: {e}")

# -----------------
# OSINT & Advanced Utilities
# -----------------
import base64
from PIL.ExifTags import TAGS
from datetime import datetime

@discord.app_commands.allowed_installs(guilds=True, users=True)
@discord.app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@bot.tree.command(name="discord_token", description="Decode a Discord Token safely offline")
@is_owner()
async def discord_token(interaction: discord.Interaction, token: str):
    try:
        parts = token.split('.')
        if len(parts) < 2:
            return await interaction.response.send_message("❌ Invalid token format.")
            
        # Parse User ID
        user_id_b64 = parts[0]
        # Pad base64 if needed
        user_id_b64 += '=' * (-len(user_id_b64) % 4)
        user_id = base64.b64decode(user_id_b64).decode()
        
        # Parse Creation Timestamp
        timestamp_b64 = parts[1]
        timestamp_b64 += '=' * (-len(timestamp_b64) % 4)
        timestamp_bytes = base64.urlsafe_b64decode(timestamp_b64)
        timestamp_int = int.from_bytes(timestamp_bytes, byteorder='big')
        discord_epoch = 1293840000
        timestamp = timestamp_int + discord_epoch
        dt = datetime.fromtimestamp(timestamp, tz=pytz.timezone('UTC'))
        
        embed = discord.Embed(title="Token Decoded", color=discord.Color.red())
        embed.add_field(name="User ID", value=user_id, inline=False)
        embed.add_field(name="Token Created At", value=dt.strftime("%Y-%m-%d %H:%M:%S UTC"), inline=False)
        embed.set_footer(text="Decoded entirely offline. No API requests were made.")
        await interaction.response.send_message(embed=embed)
    except Exception as e:
        await interaction.response.send_message(f"❌ Failed to decode token: {e}")

@discord.app_commands.allowed_installs(guilds=True, users=True)
@discord.app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@bot.tree.command(name="weather", description="Check global live weather")
@is_owner()
async def weather(interaction: discord.Interaction, city: str):
    await interaction.response.defer()
    try:
        url = f"https://wttr.in/{city}?format=3"
        async with bot.session.get(url) as resp:
            data = await resp.text()
            await interaction.followup.send(f"**Weather:** {data.strip()}")
    except Exception as e:
        await interaction.followup.send(f"❌ Error: {e}")

@discord.app_commands.allowed_installs(guilds=True, users=True)
@discord.app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@bot.tree.command(name="timezones", description="Compare current times across global cities")
@is_owner()
async def timezones(interaction: discord.Interaction):
    cities = {
        "New York": "America/New_York",
        "London": "Europe/London",
        "Berlin": "Europe/Berlin",
        "Tokyo": "Asia/Tokyo",
        "Sydney": "Australia/Sydney"
    }
    out = []
    for city, tz_str in cities.items():
        tz = pytz.timezone(tz_str)
        t = datetime.now(tz).strftime("%I:%M %p")
        out.append(f"**{city}**: `{t}`")
    await interaction.response.send_message("\n".join(out))

@discord.app_commands.allowed_installs(guilds=True, users=True)
@discord.app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@bot.tree.command(name="tx_fee_calc", description="Calculate average LTC network fee in Fiat")
@is_owner()
async def tx_fee_calc(interaction: discord.Interaction):
    await interaction.response.defer()
    try:
        url = "https://api.blockcypher.com/v1/ltc/main"
        async with bot.session.get(url) as resp:
            data = await resp.json()
            high_fee_kb = data.get("high_fee_per_kb", 0) / 100000000
            
        price_url = "https://api.coingecko.com/api/v3/simple/price?ids=litecoin&vs_currencies=usd,eur"
        async with bot.session.get(price_url) as resp:
            price_data = await resp.json()
            ltc_usd = price_data["litecoin"]["usd"]
            ltc_eur = price_data["litecoin"]["eur"]
            
        avg_tx_size = 0.25 # KB
        fee_ltc = high_fee_kb * avg_tx_size
        fee_usd = fee_ltc * ltc_usd
        fee_eur = fee_ltc * ltc_eur
        
        embed = discord.Embed(title="LTC High Priority Fee", color=discord.Color.light_gray())
        embed.add_field(name="LTC", value=f"{fee_ltc:,.6f}", inline=False)
        embed.add_field(name="Fiat", value=f"${fee_usd:,.3f} USD | €{fee_eur:,.3f} EUR", inline=False)
        await interaction.followup.send(embed=embed)
    except Exception as e:
        await interaction.followup.send(f"❌ Error: {e}")

@discord.app_commands.allowed_installs(guilds=True, users=True)
@discord.app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@bot.tree.command(name="social_scan", description="Check if an email is registered to a major service")
@is_owner()
async def social_scan(interaction: discord.Interaction, email: str):
    await interaction.response.defer()
    try:
        # A simple check for MX records via a public API 
        url = f"https://api.eva.pingutil.com/email?email={email}"
        async with bot.session.get(url) as resp:
            data = await resp.json()
            if data.get("status") == "success":
                info = data["data"]
                embed = discord.Embed(title=f"Email Scan: {email}", color=discord.Color.blue())
                embed.add_field(name="Deliverable", value="✅ Yes" if info.get("deliverable") else "❌ No")
                embed.add_field(name="Disposable", value="✅ Yes" if info.get("disposable") else "❌ No")
                embed.add_field(name="Spam Trap", value="✅ Yes" if info.get("spam") else "❌ No")
                await interaction.followup.send(embed=embed)
            else:
                await interaction.followup.send("❌ Error parsing email.")
    except Exception as e:
        await interaction.followup.send(f"❌ Error: {e}")

@discord.app_commands.allowed_installs(guilds=True, users=True)
@discord.app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@bot.tree.command(name="name_check", description="Check username availability on major sites")
@is_owner()
async def name_check(interaction: discord.Interaction, username: str):
    await interaction.response.defer()
    sites = {
        "GitHub": f"https://github.com/{username}",
        "Twitter/X": f"https://nitter.net/{username}",
        "Reddit": f"https://www.reddit.com/user/{username}"
    }
    results = []
    for site, url in sites.items():
        try:
            async with bot.session.get(url, timeout=3) as resp:
                if resp.status == 404:
                    results.append(f"✅ **{site}**: Available (or suspended)")
                else:
                    results.append(f"❌ **{site}**: Taken")
        except:
            results.append(f"⚠️ **{site}**: Error checking")
            
    await interaction.followup.send("\n".join(results))

@discord.app_commands.allowed_installs(guilds=True, users=True)
@discord.app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@bot.tree.command(name="metadata", description="Read or strip EXIF metadata from an image")
@app_commands.choices(action=[
    Choice(name="Read Metadata", value="read"),
    Choice(name="Strip Metadata", value="strip"),
])
@is_owner()
async def metadata(interaction: discord.Interaction, action: str, attachment: discord.Attachment):
    await interaction.response.defer()
    try:
        image_bytes = await attachment.read()
        img = Image.open(io.BytesIO(image_bytes))
        
        if action == "read":
            exifdata = img.getexif()
            if not exifdata:
                return await interaction.followup.send("📭 No metadata found in this image.")
                
            out = []
            for tag_id in exifdata:
                tag = TAGS.get(tag_id, tag_id)
                data = exifdata.get(tag_id)
                if isinstance(data, bytes):
                    data = data.decode(errors='replace')
                out.append(f"**{tag}**: {data}")
                
            text = "\n".join(out)
            if len(text) > 2000:
                text = text[:1995] + "..."
            await interaction.followup.send(text)
            
        elif action == "strip":
            # Removing EXIF is as simple as saving without the exif param
            data = list(img.getdata())
            img_without_exif = Image.new(img.mode, img.size)
            img_without_exif.putdata(data)
            
            output = io.BytesIO()
            img_without_exif.save(output, format="PNG")
            output.seek(0)
            await interaction.followup.send(file=discord.File(output, filename="stripped.png"))
            
    except Exception as e:
        await interaction.followup.send(f"❌ Error: {e}")

@discord.app_commands.allowed_installs(guilds=True, users=True)
@discord.app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@bot.tree.command(name="speedtest", description="Run a speedtest on the Zap-Hosting server")
@is_owner()
async def speedtest_cmd(interaction: discord.Interaction):
    await interaction.response.defer()
    try:
        process = await asyncio.create_subprocess_shell(
            'speedtest-cli --simple',
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await process.communicate()
        if process.returncode == 0:
            await interaction.followup.send(f"```yaml\n{stdout.decode().strip()}\n```")
        else:
            await interaction.followup.send(f"❌ Speedtest failed: {stderr.decode()}")
    except Exception as e:
        await interaction.followup.send(f"❌ Error: {e}")

@discord.app_commands.allowed_installs(guilds=True, users=True)
@discord.app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@bot.tree.command(name="obfuscate", description="Scramble code to make it harder to read")
@is_owner()
async def obfuscate(interaction: discord.Interaction, code: str):
    await interaction.response.defer()
    try:
        # Simple base64 layer
        encoded = base64.b64encode(code.encode()).decode()
        obf = f"import base64;exec(base64.b64decode('{encoded}'))"
        await interaction.followup.send(f"```python\n{obf}\n```")
    except Exception as e:
        await interaction.followup.send(f"❌ Error: {e}")

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
# Help Command
# -----------------
@discord.app_commands.allowed_installs(guilds=True, users=True)
@discord.app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@bot.tree.command(name="help", description="Show all available commands and what they do")
@is_owner()
async def help_cmd(interaction: discord.Interaction):
    embed = discord.Embed(title="🤖 Selfbot Command Menu", description=f"Current Prefix: `{bot.current_prefix}`\nAll commands are restricted to the bot owner.", color=discord.Color.purple())
    
    cat_crypto = "`portfolio`, `set_ltc_log`, `cv`, `calc`, `paypal`, `ltc_address`, `ltc_tx`, `ltc_send`, `tx_fee_calc`"
    cat_util = "`remind`, `notes`, `tempmail`, `webhook_send`, `steam_lookup`, `social_scan`, `name_check`, `metadata`, `speedtest`, `obfuscate`, `discord_token`, `weather`, `timezones`"
    cat_media = "`nitro_gen`, `fake_message`, `deepfry`, `tts_mp3`, `audio_extract`, `uwuify`, `zalgo`, `hack_screen`"
    cat_discord = "`server_clone`, `fake_activity`, `avatar`, `id_decode`, `banner_steal`, `guild_icon`, `whois_discord`, `embed_builder`, `role_color`, `server_stats`"
    cat_prefix = f"`{bot.current_prefix}ytdl`, `{bot.current_prefix}tiktok`, `{bot.current_prefix}spotify`, `{bot.current_prefix}steal`, `{bot.current_prefix}lock`, `{bot.current_prefix}prefix`"

    embed.add_field(name="💳 Finance & Crypto", value=cat_crypto, inline=False)
    embed.add_field(name="🛠 Utilities & OSINT", value=cat_util, inline=False)
    embed.add_field(name="🎨 Media & Trolling", value=cat_media, inline=False)
    embed.add_field(name="👑 Discord Power-User", value=cat_discord, inline=False)
    embed.add_field(name="⚡ Prefix Fast-Commands", value=cat_prefix, inline=False)
        
    await interaction.response.send_message(embed=embed)


# -----------------
# End of File
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
