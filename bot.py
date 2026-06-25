import os
import time
from datetime import datetime, time as dt_time
from zoneinfo import ZoneInfo
import re
import urllib.parse
from dotenv import load_dotenv
import discord
from discord.ext import tasks
from openai import AsyncOpenAI
import aiohttp

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
    print("Warning: SYSTEM_PROMPT.md not found. Falling back to default prompt.")
    SYSTEM_PROMPT = "You are a helpful assistant."

try:
    with open('FAVORITE_GAMES.md', 'r', encoding='utf-8') as f:
        FAVORITE_GAMES = f.read().strip()
        SYSTEM_PROMPT += f"\n\nHere is a list of your favorite slot games and their providers. If someone asks what they should play, pick one or two of these and aggressively hype them up:\n{FAVORITE_GAMES}"
except FileNotFoundError:
    print("Warning: FAVORITE_GAMES.md not found. Proceeding without it.")

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
    gif_match = re.search(r'\[GIF:\s*(.+?)\]', text, re.IGNORECASE)
    if not gif_match:
        return text, None
    
    text = text.replace(gif_match.group(0), "").strip()
    if not can_make_gif_call():
        return text, None
        
    query = gif_match.group(1)
    gif_api_calls.append(time.time())
    url = f"https://api.giphy.com/v1/gifs/random?api_key={GIPHY_API_KEY}&tag={urllib.parse.quote(query)}&rating=r"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as response:
                if response.status == 200:
                    data = await response.json()
                    gif_url = data.get('data', {}).get('images', {}).get('original', {}).get('url')
                    return text, gif_url
    except Exception as e:
        print(f"Error fetching GIF: {e}")
    return text, None

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

# Conversation history storage
# Format: {channel_id: [{"timestamp": float, "role": "user"|"assistant", "content": str}, ...]}
conversation_history = {}
CONTEXT_WINDOW_SECONDS = 3600  # 1 hour

def prune_history(channel_id):
    """Remove messages older than CONTEXT_WINDOW_SECONDS"""
    if channel_id not in conversation_history:
        return
    current_time = time.time()
    conversation_history[channel_id] = [
        msg for msg in conversation_history[channel_id] 
        if current_time - msg["timestamp"] < CONTEXT_WINDOW_SECONDS
    ]

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
    print(f'Logged in as {client.user}')
    if not chat_reviver.is_running():
        chat_reviver.start()
    if not stream_reminder.is_running():
        stream_reminder.start()

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
                bot_reply, gif_url = await process_gifs_in_reply(bot_reply)
                
                # Format channel links if there are any
                bot_reply = format_channel_links(bot_reply, guild)
                
                # Add @here ping
                bot_reply = f"@here\n\n{bot_reply}"
                
                chunks = split_message(bot_reply)
                for i, chunk in enumerate(chunks):
                    await channel.send(chunk)
                    
                if gif_url:
                    await channel.send(gif_url)
            except Exception as e:
                print(f"Error in stream_reminder: {e}")

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
                    bot_reply, gif_url = await process_gifs_in_reply(bot_reply)
                    bot_reply = format_channel_links(bot_reply, guild)
                    
                    chunks = split_message(bot_reply)
                    for i, chunk in enumerate(chunks):
                        await channel.send(chunk)
                    
                    if gif_url:
                        await channel.send(gif_url)
        except Exception as e:
            print(f"Error in chat_reviver: {e}")

@client.event
async def on_member_join(member):
    # Find the #general channel (fuzzy match to handle emojis)
    channel = discord.utils.find(lambda c: "general" in c.name.lower(), member.guild.text_channels)
    if not channel:
        print(f"Could not find a 'general' channel for welcoming {member.display_name}")
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
        bot_reply, gif_url = await process_gifs_in_reply(bot_reply)
        
        # Format channel links if there are any
        bot_reply = format_channel_links(bot_reply, member.guild)
        
        chunks = split_message(bot_reply)
        for i, chunk in enumerate(chunks):
            await channel.send(chunk)
            
        if gif_url:
            await channel.send(gif_url)
    except Exception as e:
        print(f"Error welcoming new member: {e}")

@client.event
async def on_message(message):
    # Ignore messages from the bot itself
    if message.author == client.user:
        return

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
    prune_history(channel_id)
    
    # Initialize history for this channel if it doesn't exist
    if channel_id not in conversation_history:
        conversation_history[channel_id] = []

    # Remove the bot mention from the user's message
    user_content = message.content.replace(f'<@{client.user.id}>', '').replace(f'<@!{client.user.id}>', '').strip()
    
    # If the user just tagged the bot (or posted an image) with no message
    if not user_content:
        user_content = "Hi"

    # Add user's message to history (text only for context window)
    conversation_history[channel_id].append({
        "timestamp": time.time(),
        "role": "user",
        "content": user_content
    })

    messages_for_api = [{"role": "system", "content": get_system_prompt(guild=message.guild, channel=message.channel, user=message.author)}]
    for msg in conversation_history[channel_id]:
        messages_for_api.append({
            "role": msg["role"],
            "content": msg["content"]
        })

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
            bot_reply, gif_url = await process_gifs_in_reply(bot_reply)

            # Format channel links
            if message.guild:
                bot_reply = format_channel_links(bot_reply, message.guild)

            # Add bot's reply to history
            conversation_history[channel_id].append({
                "timestamp": time.time(),
                "role": "assistant",
                "content": bot_reply
            })

            # Split and send the reply
            reply_chunks = split_message(bot_reply)
            for i, chunk in enumerate(reply_chunks):
                if i == 0:
                    await message.reply(chunk)
                else:
                    await message.channel.send(chunk)
                    
            if gif_url:
                await message.channel.send(gif_url)
                
        except Exception as e:
            print(f"Error calling OpenRouter: {e}")
            await message.reply(f"Sorry, I had an issue connecting to my brain! Please check your OpenRouter API key and connection.")

if __name__ == '__main__':
    if not TOKEN or TOKEN == 'your_discord_bot_token_here':
        print("Please update the .env file with your Discord bot token.")
    else:
        client.run(TOKEN)
