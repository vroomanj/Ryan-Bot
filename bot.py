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

def get_system_prompt(guild=None, channel=None, user=None):
    current_time_str = datetime.now(ZoneInfo('America/New_York')).strftime('%A, %B %d, %Y at %I:%M %p EST')
    prompt = f"{SYSTEM_PROMPT}\n\n[System Info: The current date and time is {current_time_str}. Always refer to time in EST."
    
    if guild:
        total_members = guild.member_count
        online_members = sum(1 for m in guild.members if m.status != discord.Status.offline)
        prompt += f" The server has {total_members} members ({online_members} currently online)."
        
    if channel:
        prompt += f" You are currently talking in the #{channel.name} channel."
        
    if user and hasattr(user, 'roles'):
        role_names = [role.name for role in user.roles if role.name != "@everyone"]
        roles_str = ", ".join(role_names) if role_names else "No special roles"
        prompt += f" You are replying to user: {user.display_name} (Their exact ping is <@{user.id}>, Roles: {roles_str}). If you want to ping them inline, use their exact ping string."
        
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
    logging.info(f'Logged in as {client.user}')
    db.init_db()
    if not chat_reviver.is_running():
        chat_reviver.start()
    if not stream_reminder.is_running():
        stream_reminder.start()
    if not random_engagement.is_running():
        random_engagement.start()

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
            prompt = f"You are starting a random conversation with a user out of nowhere. Their exact ping is <@{target_user_id}>. Here are the last few things they talked about recently:\n{recent_messages}\n\nPick one of these topics, tag them, and make a sarcastic/hyped comment to start a conversation in your Ryan Bot persona. Keep it short!"
            
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
            prompt = "It is 7:55 AM EST! Announce to the chat that Ryan's stream is starting in 5 minutes! Make it extremely hyped, and explicitly tell them to go to MyPrize.us/BigJackpots to watch. Make sure you include the link directly in your message!"
            
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
                bot_reply = f"@here\n\n{bot_reply}"
                
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
                        await target_channel.send(chunk)
                        
                    if gif_url:
                        await target_channel.send(gif_url)
                    if local_gif_path:
                        await target_channel.send(file=discord.File(local_gif_path))
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

    # 2. Check if the bot should respond (mentioned OR static image in wins channel)
    is_mentioned = client.user in message.mentions
    is_wins_channel = "share-your-wins" in message.channel.name.lower()
    
    if not is_mentioned and not (is_wins_channel and has_static_image):
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
                chain.insert(0, f"[{fetched_msg.author.display_name}]: {fetched_msg.content}")
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
    db.add_conversation_message(channel_id, "user", user_content)

    messages_for_api = [{"role": "system", "content": get_system_prompt(guild=message.guild, channel=message.channel, user=message.author)}]
    
    # Load history from DB
    history = db.get_conversation_history(channel_id)
    messages_for_api.extend(history)
    
    # If the bot was explicitly pinged, insert the user's recent messages as context before their current ping
    if is_mentioned:
        recent_messages_list = db.get_user_recent_messages(message.author.id)
        if len(recent_messages_list) > 1:
            # Use :-1 to exclude the message they just sent right now
            recent_context = "\n".join(f"- {msg}" for msg in recent_messages_list[:-1])
            context_prompt = f"For extra context, the user tagging you recently said the following things across the server:\n{recent_context}\n\nYou can casually reference this if it seems relevant to their current message, but ignore it if it's completely unrelated."
            messages_for_api.insert(-1, {"role": "system", "content": context_prompt})

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
