import os
import time
import logging
from datetime import datetime, time as dt_time
from zoneinfo import ZoneInfo
import re
import urllib.parse
import random
from dotenv import load_dotenv
import discord
from discord.ext import tasks
from openai import AsyncOpenAI
import aiohttp
import db
import discord.ui

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(name)s - %(message)s',
    handlers=[
        logging.FileHandler("ryanbot.log"),
        logging.StreamHandler()
    ]
)

# Load environment variables
load_dotenv()
TOKEN = os.getenv('DISCORD_BOT_TOKEN')

# Initialize Discord client
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.presences = True
client = discord.Client(intents=intents)
tree = discord.app_commands.CommandTree(client)

vibe_group = discord.app_commands.Group(name="vibe", description="Vibe related commands", default_permissions=discord.Permissions(manage_messages=True))
tree.add_command(vibe_group)

# Persona configuration
MODEL_NAME = 'deepseek/deepseek-v4-flash'
PROVIDER_ORDER = ["Alibaba"]
try:
    with open('SYSTEM_PROMPT.md', 'r', encoding='utf-8') as f:
        SYSTEM_PROMPT = f.read().strip()
except FileNotFoundError:
    logging.warning("SYSTEM_PROMPT.md not found. Falling back to default prompt.")
    SYSTEM_PROMPT = "You are a helpful assistant."

try:
    with open('FAVORITE_GAMES.md', 'r', encoding='utf-8') as f:
        FAVORITE_GAMES = f.read().strip()
        SYSTEM_PROMPT += f"\n\nHere is a list of your favorite slot games and their providers. If someone asks what they should play, pick one or two of these and aggressively hype them up:\n{FAVORITE_GAMES}"
except FileNotFoundError:
    logging.warning("FAVORITE_GAMES.md not found. Proceeding without it.")

GIPHY_API_KEY = os.getenv('GIPHY_API_KEY')
gif_api_calls = []

def can_make_gif_call():
    if not GIPHY_API_KEY:
        return False
    current_time = time.time()
    global gif_api_calls
    # Prune calls older than 1 hour (3600 seconds)
    gif_api_calls = [t for t in gif_api_calls if current_time - t < 3600]
    
    # Enforce an hourly limit
    if len(gif_api_calls) >= 80:
        return False
        
    # Enforce a 5-minute cooldown between GIFs
    if gif_api_calls and current_time - gif_api_calls[-1] < 300:
        return False
        
    return True

async def process_gifs_in_reply(text):
    gif_url = None
    local_gif_path = None
    
    # Check for local media trigger (handles LLMs outputting either LOCAL_MEDIA or LOCAL_GIF, multiple times, and optional markdown code blocks)
    def replace_local_media(match):
        nonlocal local_gif_path
        filename = match.group(1).strip().lower()
        if 'yell-ryan' in filename:
            local_gif_path = '/home/vroomanj/Ryan-Bot/yell-ryan.gif'
        return " "
        
    text = re.sub(r'[\`\s]*\[LOCAL_(?:MEDIA|GIF):\s*(.+?)\][\`\s]*', replace_local_media, text, flags=re.IGNORECASE)

    gif_match = re.search(r'[\`\s]*\[GIF:\s*(.+?)\][\`\s]*', text, re.IGNORECASE)
    if gif_match and not local_gif_path:
        text = text.replace(gif_match.group(0), "").strip()
        if can_make_gif_call():
            query = gif_match.group(1)
            global gif_api_calls
            gif_api_calls.append(time.time())
            url = f"https://api.giphy.com/v1/gifs/random?api_key={GIPHY_API_KEY}&tag={urllib.parse.quote(query)}&rating=r"
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(url) as response:
                        if response.status == 200:
                            data = await response.json()
                            gif_url = data.get('data', {}).get('images', {}).get('original', {}).get('url')
            except Exception as e:
                logging.error(f"Error fetching GIF: {e}")
                
    return text.strip(), gif_url, local_gif_path

def get_system_prompt(guild=None, channel=None, user=None, mentioned_users=None):
    current_time_str = datetime.now(ZoneInfo('America/New_York')).strftime('%A, %B %d, %Y at %I:%M %p %Z')
    prompt = f"{SYSTEM_PROMPT}\n\n[System Info: The current date and time is {current_time_str}.]"
    
    if guild:
        total_members = guild.member_count
        online_members = sum(1 for m in guild.members if m.status != discord.Status.offline)
        prompt += f" The server has {total_members} members ({online_members} currently online)."
        
    if channel:
        if hasattr(channel, 'name'):
            prompt += f" You are currently talking in the #{channel.name} channel."
        else:
            prompt += " You are currently talking in a direct message (DM)."
            
        if hasattr(channel, 'topic') and channel.topic:
            prompt += f" The topic/purpose of this channel is: '{channel.topic}'."
        
    if user:
        role_names = [role.name for role in user.roles if role.name != "@everyone"] if hasattr(user, 'roles') else []
        roles_str = ", ".join(role_names) if role_names else "No special roles"
        prompt += f" You are replying to user: {user.display_name} (Their exact ping is <@{user.id}>, Roles: {roles_str}). If you want to ping them inline, use their exact ping string."
        
        # Inject long-term profile memory
        profile = db.get_full_user_profile(user.id)
        if profile:
            prompt += f"\n\n[Long-Term Memory: Here is what you know about {user.display_name} from past interactions: {profile}]"
            
    if mentioned_users:
        prompt += "\n\n[Context: The user mentioned other people in their message. Here are their profiles so you know who they are talking about:]"
        for m_user in mentioned_users:
            m_profile = db.get_full_user_profile(m_user.id)
            if m_profile:
                prompt += f"\n- {m_user.display_name}: {m_profile}"
        
    if can_make_gif_call():
        prompt += " You have the ability to post a GIF by typing exactly `[GIF: search terms]`. However, you must EXTREMELY rarely do this. ONLY use a GIF if a user explicitly asks for one, or if you are reacting to a completely massive jackpot win. Under normal conversational circumstances, NEVER use a GIF."
        
    prompt += "]"
    return prompt

# Initialize OpenRouter client
llm_client = AsyncOpenAI(
    base_url="https://openrouter.ai/api/v1",
    api_key=os.getenv("OPENROUTER_API_KEY"),
)

# CONTEXT_WINDOW_SECONDS is handled by db.prune_conversation_history which defaults to 14400 (4 hours)

def format_channel_links(text, guild):
    """Replaces #channel-name with clickable <#CHANNEL_ID> links."""
    if not guild:
        return text
        
    def replace_channel(match):
        channel_name = match.group(1).lower().replace("_", "-")
        
        # Don't try to link numbers like #1, #2 (these are rules or rankings)
        if channel_name.isdigit():
            return match.group(0)
            
        # Substring match to handle emojis or slightly different names in Discord
        channel = discord.utils.find(lambda c: channel_name in c.name.lower().replace("_", "-"), guild.channels)
        
        if channel:
            return f"<#{channel.id}>"
        return match.group(0) # Return original if not found
        
    return re.sub(r'#([a-zA-Z0-9_-]+)', replace_channel, text)

def split_message(text, limit=2000):
    """Splits a long message into smaller chunks that fit within Discord's limit."""
    if not text:
        return []
    chunks = []
    while len(text) > limit:
        # Find the last newline before the limit to avoid splitting mid-word or mid-sentence if possible
        split_index = text.rfind('\n', 0, limit)
        if split_index == -1:
            # If no newline, find the last space
            split_index = text.rfind(' ', 0, limit)
            if split_index == -1:
                # If no space, just hard split
                split_index = limit
        
        chunks.append(text[:split_index])
        text = text[split_index:].lstrip()
        
    if text:
        chunks.append(text)
    return chunks

@client.event
async def on_ready():
    logging.info(f"Logged in as {client.user}")
    
    # Sync slash commands
    try:
        await tree.sync()
        logging.info("Slash commands synced successfully.")
    except Exception as e:
        logging.error(f"Failed to sync slash commands: {e}")
        
    db.init_db()
    if not chat_reviver.is_running():
        chat_reviver.start()
    if not stream_reminder.is_running():
        stream_reminder.start()
    if not random_engagement.is_running():
        random_engagement.start()
    if not profile_updater.is_running():
        profile_updater.start()
    if not status_changer.is_running():
        status_changer.start()

class LookupPaginationView(discord.ui.View):
    def __init__(self, embeds):
        super().__init__(timeout=300)
        self.embeds = embeds
        self.current_page = 0
        self.update_buttons()

    def update_buttons(self):
        self.prev_button.disabled = self.current_page == 0
        self.next_button.disabled = self.current_page == len(self.embeds) - 1

    @discord.ui.button(label="◀ Previous", style=discord.ButtonStyle.primary)
    async def prev_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.current_page -= 1
        self.update_buttons()
        await interaction.response.edit_message(embed=self.embeds[self.current_page], view=self)

    @discord.ui.button(label="Next ▶", style=discord.ButtonStyle.primary)
    async def next_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.current_page += 1
        self.update_buttons()
        await interaction.response.edit_message(embed=self.embeds[self.current_page], view=self)

class VibeEditModal(discord.ui.Modal, title="Edit Vibe Profile"):
    def __init__(self, target_user_id: int, current_profile: str):
        super().__init__()
        self.target_user_id = target_user_id
        
        self.profile_input = discord.ui.TextInput(
            label="Profile Summary",
            style=discord.TextStyle.paragraph,
            default=current_profile,
            required=True,
            max_length=2000
        )
        self.add_item(self.profile_input)

    async def on_submit(self, interaction: discord.Interaction):
        new_profile = self.profile_input.value.strip()
        db.update_user_profile(self.target_user_id, new_profile)
        await interaction.response.send_message(f"✅ Vibe profile for User `{self.target_user_id}` has been successfully overwritten in the database!", ephemeral=True)

@vibe_group.command(name="check", description="[Moderator Only] Look up a user's long-term bot profile and recent vibe.")
async def vibe_check(interaction: discord.Interaction, user: discord.Member):
    await interaction.response.defer(ephemeral=True)
    
    embeds = []
    
    # Page 1: Vibe Profile
    profile = db.get_full_user_profile(user.id)
    embed1 = discord.Embed(
        title=f"🧠 Vibe Profile: {user.display_name}",
        description=profile if profile else "No AI profile generated yet.",
        color=discord.Color.blue()
    )
    embed1.set_thumbnail(url=user.display_avatar.url if user.display_avatar else None)
    
    roles_str = ", ".join([r.name for r in user.roles if r.name != "@everyone"])
    embed1.add_field(name="User ID", value=str(user.id), inline=True)
    embed1.add_field(name="Joined Server", value=user.joined_at.strftime("%Y-%m-%d") if user.joined_at else "Unknown", inline=True)
    embed1.add_field(name="Roles", value=roles_str if roles_str else "None", inline=False)
    
    embed1.set_footer(text="Page 1 of 2 • Vibe Profile")
    embeds.append(embed1)
    
    # Page 2: Recent Chat History
    recent_messages = db.get_user_recent_messages(user.id, limit=5)
    embed2 = discord.Embed(
        title=f"📝 Recent Activity: {user.display_name}",
        color=discord.Color.green()
    )
    embed2.set_thumbnail(url=user.display_avatar.url if user.display_avatar else None)
    
    if recent_messages:
        recent_text = "\n".join(recent_messages)
        if len(recent_text) > 4000:
            recent_text = recent_text[:4000] + "..."
        embed2.add_field(name="Last 5 Messages", value=recent_text, inline=False)
    else:
        embed2.description = "No recent messages found in database."
        
    embed2.set_footer(text="Page 2 of 2 • Message History")
    embeds.append(embed2)
    
    view = LookupPaginationView(embeds)
    await interaction.followup.send(embed=embeds[0], view=view)

@vibe_group.command(name="edit", description="[Moderator Only] Manually edit a user's vibe profile.")
async def vibe_edit(interaction: discord.Interaction, user: discord.Member):
    # Fetch existing profile
    profile = db.get_user_profile(user.id)
    if not profile:
        profile = "No profile exists for this user yet. You can write one here!"
        
    # Send the modal popup
    modal = VibeEditModal(target_user_id=user.id, current_profile=profile)
    await interaction.response.send_modal(modal)

@vibe_group.command(name="refresh", description="[Moderator Only] Force a manual refresh of a user's vibe profile via the AI.")
async def vibe_refresh(interaction: discord.Interaction, user: discord.Member):
    await interaction.response.defer(ephemeral=True)
    
    recent_messages = db.get_user_recent_messages(user.id, limit=100)
    if not recent_messages:
        await interaction.followup.send(f"❌ Cannot refresh profile. User {user.display_name} has no recent messages in the database.", ephemeral=True)
        return
        
    current_profile = db.get_user_profile(user.id)
    
    role_names = [role.name for role in user.roles if role.name != "@everyone"]
    roles_str = ", ".join(role_names) if role_names else "No special roles"
    user_info = f"Display Name: {user.display_name} (Ping: <@{user.id}>) | Roles: {roles_str}"
    
    recent_text = "\n".join(recent_messages)
    prompt = f"Here are the recent messages sent by this user ({user_info}):\n{recent_text}\n\n"
    if current_profile:
        prompt += f"Here is their current profile summary:\n{current_profile}\n\nUpdate their profile summary to incorporate any new vibe, favorite games, win/loss streaks, or behavioral quirks you notice. Keep it to one concise paragraph."
    else:
        prompt += "Write a short, one-paragraph profile summary for this user documenting their general vibe, favorite games, or behavioral quirks based on these messages."
        
    try:
        response = await llm_client.chat.completions.create(
            model=MODEL_NAME,
            messages=[
                {"role": "system", "content": "You are a backend profiling agent. Your output will be saved directly to a database as a user profile. Write ONLY the profile paragraph. Do not include greetings or markdown blocks."},
                {"role": "user", "content": prompt}
            ],
            extra_body={"provider": {"order": PROVIDER_ORDER}}
        )
        new_profile = response.choices[0].message.content.strip()
        db.update_user_profile(user.id, new_profile)
        await interaction.followup.send(f"✅ Successfully refreshed the AI profile for **{user.display_name}**!\n\n**New Profile:**\n{new_profile}", ephemeral=True)
    except Exception as e:
        logging.error(f"Error manually refreshing profile for {user.id}: {e}")
        await interaction.followup.send("❌ An error occurred while communicating with the AI to refresh the profile.", ephemeral=True)

@vibe_group.command(name="add", description="[Moderator Only] Add a permanent, highly-prioritized note to a user's vibe profile.")
@discord.app_commands.describe(note="The permanent note to attach to their profile.")
async def vibe_add(interaction: discord.Interaction, user: discord.Member, note: str):
    db.add_user_note(user.id, note.strip())
    await interaction.response.send_message(f"✅ Successfully attached permanent note to **{user.display_name}**:\n`{note.strip()}`", ephemeral=True)

@vibe_group.command(name="list", description="[Moderator Only] List all permanent notes for a user.")
async def vibe_list(interaction: discord.Interaction, user: discord.Member):
    notes = db.get_user_notes(user.id)
    if not notes:
        await interaction.response.send_message(f"User **{user.display_name}** has no permanent notes.", ephemeral=True)
        return
        
    notes_str = "\n".join(f"**{i+1}.** {note}" for i, note in enumerate(notes))
    await interaction.response.send_message(f"🚨 **Permanent Notes for {user.display_name}:**\n\n{notes_str}", ephemeral=True)

@vibe_group.command(name="delete", description="[Moderator Only] Delete a permanent note from a user.")
@discord.app_commands.describe(index="The note number to delete (use /vibe list to find the number).")
async def vibe_delete(interaction: discord.Interaction, user: discord.Member, index: int):
    success = db.delete_user_note(user.id, index - 1)
    if success:
        await interaction.response.send_message(f"✅ Successfully deleted note #{index} for **{user.display_name}**.", ephemeral=True)
    else:
        await interaction.response.send_message(f"❌ Could not find note #{index} for **{user.display_name}**. Use `/vibe list` to see valid note numbers.", ephemeral=True)

@tasks.loop(minutes=45)
async def random_engagement():
    if not client.is_ready() or random_engagement.current_loop == 0:
        return
        
    db.garbage_collect_user_activity(hours=24)
    active_users = db.get_active_users(minutes=30)
    
    if not active_users:
        return
        
    # Pick a random user
    target_user_id = random.choice(active_users)
    recent_messages_list = db.get_user_recent_messages(target_user_id)
    
    # If they haven't typed any text (e.g. only sent images), skip
    if not recent_messages_list:
        return
        
    recent_messages = "\n".join(f"- {msg}" for msg in recent_messages_list)
    
    for guild in client.guilds:
        channel = discord.utils.find(lambda c: "general" in c.name.lower(), guild.text_channels)
        if channel:
            recent_chat = []
            try:
                async for msg in channel.history(limit=15):
                    dt = msg.created_at.astimezone(ZoneInfo('America/New_York')).strftime('%Y-%m-%d %H:%M:%S')
                    recent_chat.insert(0, f"[{dt}] [{msg.author.display_name} (<@{msg.author.id}>)]: {msg.content}")
            except Exception:
                pass
                
            group_context = "\n".join(recent_chat) if recent_chat else "No recent messages."
            
            prompt = f"You are starting a random conversation with a user out of nowhere. Their exact ping is <@{target_user_id}>. Here are the last few things they talked about recently:\n{recent_messages}\n\nAnd here is the recent conversation happening in the channel right now:\n{group_context}\n\nPick one of these topics, tag them, and make a sarcastic/hyped comment to start a conversation in your Ryan Bot persona. Keep it short!"
            
            messages_for_api = [
                {"role": "system", "content": get_system_prompt(guild=guild, channel=channel)},
                {"role": "user", "content": prompt}
            ]
            
            try:
                response = await llm_client.chat.completions.create(
                    model=MODEL_NAME, 
                    messages=messages_for_api,
                    extra_body={"provider": {"order": PROVIDER_ORDER}}
                )
                bot_reply = response.choices[0].message.content
                bot_reply, gif_url, local_gif_path = await process_gifs_in_reply(bot_reply)
                
                bot_reply = format_channel_links(bot_reply, guild)
                chunks = split_message(bot_reply)
                for i, chunk in enumerate(chunks):
                    await channel.send(chunk)
                    
                if gif_url:
                    await channel.send(gif_url)
                if local_gif_path:
                    await channel.send(file=discord.File(local_gif_path))
            except Exception as e:
                logging.error(f"Error in random_engagement: {e}")
            break # Only do this in one guild (the main one)

stream_time = dt_time(hour=7, minute=55, tzinfo=ZoneInfo('America/New_York'))

@tasks.loop(time=stream_time)
async def stream_reminder():
    now = datetime.now(ZoneInfo('America/New_York'))
    # Only run Monday to Friday (0 = Mon, 4 = Fri)
    if now.weekday() > 4:
        return
        
    for guild in client.guilds:
        channel = discord.utils.find(lambda c: "slots-with-ryan" in c.name.lower(), guild.text_channels)
        if channel:
            prompt = "It is 7:55 AM Eastern Time! Announce to the chat that Ryan's stream is starting in 5 minutes! Make it extremely hyped, and explicitly tell them to go to https://MyPrize.us/BigJackpots to watch. Make sure you include the exact link `https://MyPrize.us/BigJackpots` directly in your message!"
            
            messages_for_api = [
                {"role": "system", "content": get_system_prompt(guild=guild, channel=channel)},
                {"role": "user", "content": prompt}
            ]
            
            try:
                response = await llm_client.chat.completions.create(
                    model=MODEL_NAME, 
                    messages=messages_for_api,
                    extra_body={"provider": {"order": PROVIDER_ORDER}}
                )
                bot_reply = response.choices[0].message.content
                bot_reply, gif_url, local_gif_path = await process_gifs_in_reply(bot_reply)
                
                # Format channel links if there are any
                bot_reply = format_channel_links(bot_reply, guild)
                
                # Add @here ping
                bot_reply = f"{bot_reply} @here"
                
                chunks = split_message(bot_reply)
                for i, chunk in enumerate(chunks):
                    await channel.send(chunk)
                    
                if gif_url:
                    await channel.send(gif_url)
                if local_gif_path:
                    await channel.send(file=discord.File(local_gif_path))
            except Exception as e:
                logging.error(f"Error in stream_reminder: {e}")

@stream_reminder.before_loop
async def before_stream_reminder():
    await client.wait_until_ready()

@tasks.loop(minutes=30)
async def profile_updater():
    if not client.is_ready():
        return
        
    users_to_update = db.get_users_needing_profile_update(limit=5)
    for user_id in users_to_update:
        recent_messages = db.get_user_recent_messages(user_id, limit=100)
        if not recent_messages:
            continue
            
        current_profile = db.get_user_profile(user_id)
        
        # Try to fetch their Discord profile and roles
        member = None
        for guild in client.guilds:
            member = guild.get_member(int(user_id))
            if member:
                break
                
        user_info = f"User ID: {user_id}"
        if member:
            role_names = [role.name for role in member.roles if role.name != "@everyone"]
            roles_str = ", ".join(role_names) if role_names else "No special roles"
            user_info = f"Display Name: {member.display_name} (Ping: <@{user_id}>) | Roles: {roles_str}"
        
        recent_text = "\n".join(recent_messages)
        prompt = f"Here are the recent messages sent by this user ({user_info}):\n{recent_text}\n\n"
        if current_profile:
            prompt += f"Here is their current profile summary:\n{current_profile}\n\nUpdate their profile summary to incorporate any new vibe, favorite games, win/loss streaks, or behavioral quirks you notice. Keep it to one concise paragraph."
        else:
            prompt += "Write a short, one-paragraph profile summary for this user documenting their general vibe, favorite games, or behavioral quirks based on these messages."
            
        try:
            response = await llm_client.chat.completions.create(
                model=MODEL_NAME,
                messages=[
                    {"role": "system", "content": "You are a backend profiling agent. Your output will be saved directly to a database as a user profile. Write ONLY the profile paragraph. Do not include greetings or markdown blocks."},
                    {"role": "user", "content": prompt}
                ],
                extra_body={"provider": {"order": PROVIDER_ORDER}}
            )
            new_profile = response.choices[0].message.content.strip()
            db.update_user_profile(user_id, new_profile)
        except Exception as e:
            logging.error(f"Error updating profile for {user_id}: {e}")

@tasks.loop(minutes=15)
async def status_changer():
    if not client.is_ready():
        return
        
    statuses = [
        discord.Activity(type=discord.ActivityType.watching, name="Ryan tilt on Plinko"),
        discord.Activity(type=discord.ActivityType.playing, name="Gates of Olympus"),
        discord.Activity(type=discord.ActivityType.watching, name="a 0x Hacksaw bonus"),
        discord.Activity(type=discord.ActivityType.playing, name="with the server's database"),
        discord.Activity(type=discord.ActivityType.listening, name="people beg for sweeps"),
        discord.Activity(type=discord.ActivityType.playing, name="Blackjack against the dealer"),
        discord.Activity(type=discord.ActivityType.watching, name="the crypto charts crash"),
        discord.Activity(type=discord.ActivityType.playing, name="a $1,000 bonus buy"),
        discord.CustomActivity(name="Currently down $5,000 SC"),
        discord.Activity(type=discord.ActivityType.listening, name="slot machine noises"),
        discord.Activity(type=discord.ActivityType.watching, name="the roulette wheel spin"),
        discord.CustomActivity(name="Chasing the Grand Jackpot"),
        discord.Activity(type=discord.ActivityType.listening, name="Ryan yell at the screen"),
        discord.Activity(type=discord.ActivityType.playing, name="hide and seek with my balance"),
        discord.CustomActivity(name="Banned from the live dealer tables"),
        discord.Activity(type=discord.ActivityType.watching, name="my crypto wallet drain"),
        discord.Activity(type=discord.ActivityType.playing, name="Sweet Bonanza on auto-spin"),
        discord.Activity(type=discord.ActivityType.listening, name="the sweet sound of a max win"),
        discord.CustomActivity(name="RTP is definitely a myth"),
        discord.Activity(type=discord.ActivityType.watching, name="someone hit a 10,000x")
    ]
    
    new_status = random.choice(statuses)
    await client.change_presence(activity=new_status)

@tasks.loop(minutes=5)
async def chat_reviver():
    # Only run if client is ready
    if not client.is_ready():
        return
        
    for guild in client.guilds:
        channel = discord.utils.find(lambda c: "general" in c.name.lower(), guild.text_channels)
        if not channel:
            continue
            
        try:
            # Get the very last message in the channel
            last_message = None
            async for msg in channel.history(limit=1):
                last_message = msg
                
            if not last_message:
                continue
                
            # Check if last message is older than 30 minutes (1800 seconds)
            now = discord.utils.utcnow()
            time_since_last = (now - last_message.created_at).total_seconds()
            
            if time_since_last > 1800:
                # Don't let the bot talk to itself infinitely
                if last_message.author == client.user:
                    continue
                    
                # It's been 30 mins and the last message wasn't the bot! Send a hype message.
                prompt = "The chat has been dead for 30 minutes. Send a highly engaging, hyped up message to revive the chat! Talk about something random but on topic: the Discord server, gambling in general, slot machines, recent wins, a specific game, or a hot take. Keep it short and in your Ryan Bot persona."
                
                messages_for_api = [
                    {"role": "system", "content": get_system_prompt(guild=guild, channel=channel)},
                    {"role": "user", "content": prompt}
                ]
                
                async with channel.typing():
                    response = await llm_client.chat.completions.create(
                        model=MODEL_NAME, 
                        messages=messages_for_api,
                        extra_body={"provider": {"order": PROVIDER_ORDER}}
                    )
                    bot_reply = response.choices[0].message.content
                    bot_reply, gif_url, local_gif_path = await process_gifs_in_reply(bot_reply)
                    
                    bot_reply = format_channel_links(bot_reply, guild)
                    
                    chunks = split_message(bot_reply)
                    for i, chunk in enumerate(chunks):
                        await channel.send(chunk)
                        
                    if gif_url:
                        await channel.send(gif_url)
                    if local_gif_path:
                        await channel.send(file=discord.File(local_gif_path))
        except Exception as e:
            logging.error(f"Error in chat_reviver: {e}")

@client.event
async def on_member_join(member):
    # Find the #general channel (fuzzy match to handle emojis)
    channel = discord.utils.find(lambda c: "general" in c.name.lower(), member.guild.text_channels)
    if not channel:
        logging.warning(f"Could not find a 'general' channel for welcoming {member.display_name}")
        return
        
    # Calculate account age
    created_at = member.created_at
    now = discord.utils.utcnow()
    age_days = (now - created_at).days
    
    if age_days == 0:
        age_str = "today"
    elif age_days == 1:
        age_str = "yesterday"
    elif age_days < 30:
        age_str = f"{age_days} days ago"
    elif age_days < 365:
        months = age_days // 30
        age_str = f"about {months} month{'s' if months > 1 else ''} ago"
    else:
        years = age_days // 365
        age_str = f"over {years} year{'s' if years > 1 else ''} ago"

    # Prepare prompt for LLM
    prompt = f"A new user just joined the server! Their exact Discord ping is <@{member.id}> and their account was created {age_str} ({created_at.strftime('%Y-%m-%d')}). Give them a short, hyped, personalized welcome message in your Ryan Bot persona! IMPORTANT: You MUST greet them using their exact ping `<@{member.id}>` directly in your message instead of their name. Do not mention badges or anything else."
    
    messages_for_api = [
        {"role": "system", "content": get_system_prompt(guild=member.guild, channel=channel, user=member)},
        {"role": "user", "content": prompt}
    ]

    try:
        response = await llm_client.chat.completions.create(
            model=MODEL_NAME, 
            messages=messages_for_api,
            extra_body={"provider": {"order": PROVIDER_ORDER}}
        )
        bot_reply = response.choices[0].message.content
        bot_reply, gif_url, local_gif_path = await process_gifs_in_reply(bot_reply)
        
        # Format channel links if there are any
        bot_reply = format_channel_links(bot_reply, member.guild)
        
        chunks = split_message(bot_reply)
        for chunk in chunks:
            await channel.send(chunk)
            
        if gif_url:
            await channel.send(gif_url)
        if local_gif_path:
            await channel.send(file=discord.File(local_gif_path))
    except Exception as e:
        logging.error(f"Error welcoming new member: {e}")

@client.event
async def on_message(message):
    # Ignore messages from the bot itself
    if message.author == client.user:
        return

    # Track user activity in SQLite DB
    user_id = message.author.id
    # Only track actual text messages that aren't bot commands
    if message.content and not message.content.startswith('!'):
        channel_name = message.channel.name if hasattr(message.channel, 'name') else "unknown"
        db.log_user_activity(user_id, channel_name, message.content)

    # 1. Check for a static image attachment
    has_static_image = False
    image_url = None
    
    if message.attachments:
        for att in message.attachments:
            if att.content_type and att.content_type.startswith('image/') and not 'gif' in att.content_type.lower():
                has_static_image = True
                image_url = att.url
                break

    # 2. Check if the bot should respond (mentioned, static image in wins channel, or a DM)
    is_dm = message.guild is None
    is_mentioned = client.user in message.mentions
    is_wins_channel = hasattr(message.channel, 'name') and "share-your-wins" in message.channel.name.lower()
    
    if not is_dm and not is_mentioned and not (is_wins_channel and has_static_image):
        return

    channel_id = message.channel.id
    
    # Prune old history for this channel
    db.prune_conversation_history(channel_id)

    # Remove the bot mention from the user's message
    user_content = message.content.replace(f'<@{client.user.id}>', '').replace(f'<@!{client.user.id}>', '').strip()
    
    # If they are replying to a message, trace the reply chain up to 3 messages back
    if message.reference and message.reference.message_id:
        try:
            chain = []
            current_ref = message.reference
            depth = 0
            
            while current_ref and current_ref.message_id and depth < 3:
                fetched_msg = await message.channel.fetch_message(current_ref.message_id)
                dt = fetched_msg.created_at.astimezone(ZoneInfo('America/New_York')).strftime('%Y-%m-%d %H:%M:%S')
                chain.insert(0, f"[{dt}] [{fetched_msg.author.display_name} (<@{fetched_msg.author.id}>)]: {fetched_msg.content}")
                current_ref = fetched_msg.reference
                depth += 1
                
            if chain:
                chain_text = "\n".join(chain)
                quote_text = f"The user is replying to this conversation chain:\n{chain_text}\n\nTheir reply: "
                user_content = f"{quote_text}{user_content}"
        except Exception as e:
            logging.warning(f"Could not fetch referenced message chain: {e}")
    
    # If the user just tagged the bot (or posted an image) with no message
    if not user_content:
        user_content = "Hi"

    # Add user's message to history (text only for context window)
    db.add_conversation_message(channel_id, "user", f"[{message.author.display_name} (<@{message.author.id}>)]: {user_content}")

    other_mentions = [m for m in message.mentions if m.id != client.user.id]
    messages_for_api = [{"role": "system", "content": get_system_prompt(guild=message.guild, channel=message.channel, user=message.author, mentioned_users=other_mentions)}]
    
    # Load history from DB
    history = db.get_conversation_history(channel_id)
    messages_for_api.extend(history)
    
    # If the bot was explicitly pinged (or DM'd), insert the user's recent messages and channel context before their current ping
    if is_mentioned or is_dm:
        # User's recent messages
        recent_messages_list = db.get_user_recent_messages(message.author.id)
        if len(recent_messages_list) > 1:
            # Use :-1 to exclude the message they just sent right now
            recent_context = "\n".join(f"- {msg}" for msg in recent_messages_list[:-1])
            context_prompt = f"For extra context, the user tagging you recently said the following things across the server:\n{recent_context}\n\nYou can casually reference this if it seems relevant to their current message, but ignore it if it's completely unrelated."
            messages_for_api.insert(-1, {"role": "system", "content": context_prompt})
            
        # Channel's recent messages (Reading the room)
        recent_chat = []
        try:
            async for msg in message.channel.history(limit=15, before=message):
                dt = msg.created_at.astimezone(ZoneInfo('America/New_York')).strftime('%Y-%m-%d %H:%M:%S')
                recent_chat.insert(0, f"[{dt}] [{msg.author.display_name} (<@{msg.author.id}>)]: {msg.content}")
        except Exception as e:
            logging.error(f"Error fetching channel history: {e}")
            
        if recent_chat:
            group_context = "\n".join(recent_chat)
            room_read_prompt = f"For extra context, here are the last few messages sent in this channel right before you were pinged. Use this to 'read the room' and understand what the group is currently discussing:\n{group_context}"
            messages_for_api.insert(-1, {"role": "system", "content": room_read_prompt})

    api_model = MODEL_NAME
    
    if has_static_image:
        api_model = "google/gemini-3.1-flash-lite"
        
        if is_wins_channel and not is_mentioned:
            if user_content == "Hi":
                vision_prompt = "React to this massive slot win screenshot! Read the multiplier or win amount if you can see it, and act like a chaotic casino streamer hyping them up."
            else:
                vision_prompt = f"React to this slot win screenshot! The user also said: {user_content}. Hype them up!"
        else:
            if user_content == "Hi":
                vision_prompt = "React to this image the user just showed you. Keep it brief and in your Ryan Bot persona."
            else:
                vision_prompt = f"React to this image! The user said: {user_content}. Keep it in your Ryan Bot persona."
                
        # Override the last user message with the vision payload
        messages_for_api[-1]["content"] = [
            {"type": "text", "text": vision_prompt},
            {"type": "image_url", "image_url": {"url": image_url}}
        ]

    async with message.channel.typing():
        try:
            # Call OpenRouter API with specific provider routing
            response = await llm_client.chat.completions.create(
                model=api_model, 
                messages=messages_for_api,
                extra_body={
                    "provider": {
                        "order": PROVIDER_ORDER
                    }
                }
            )
            bot_reply = response.choices[0].message.content
            bot_reply, gif_url, local_gif_path = await process_gifs_in_reply(bot_reply)

            # Format channel links
            if message.guild:
                bot_reply = format_channel_links(bot_reply, message.guild)

            # Add bot's reply to history
            db.add_conversation_message(channel_id, "assistant", bot_reply)

            # Split and send the reply
            reply_chunks = split_message(bot_reply)
            for i, chunk in enumerate(reply_chunks):
                if i == 0:
                    await message.reply(chunk)
                else:
                    await message.channel.send(chunk)
                    
            if gif_url:
                await message.channel.send(gif_url)
            if local_gif_path:
                await message.channel.send(file=discord.File(local_gif_path))
                
        except Exception as e:
            logging.error(f"Error calling OpenRouter: {e}")
            await message.reply(f"Sorry, I had an issue connecting to my brain! Please check your OpenRouter API key and connection.")

if __name__ == '__main__':
    if not TOKEN or TOKEN == 'your_discord_bot_token_here':
        logging.error("Please update the .env file with your Discord bot token.")
    else:
        client.run(TOKEN)
