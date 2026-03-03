import discord
from discord import app_commands
from discord.ext import commands
import asyncio
import edge_tts
from dotenv import load_dotenv
import sqlite3
import io
import re
import os
from datetime import datetime, timezone
from collections import OrderedDict

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")

# Config

TEXT_CHANNEL_IDS = [
    1473527027280773120,
    1471319865817169921
]

TTS_PITCH = "+8Hz"
TTS_RATE = "+5%"


# Cache

TTS_CACHE_LIMIT = 100
tts_cache = OrderedDict()


def make_cache_key(text, voice):
    return (text, voice)


def get_cached_tts(key):
    if key in tts_cache:
        # Move to end to mark as recently used
        tts_cache.move_to_end(key)
        return tts_cache[key]
    return None


def set_cached_tts(key, value):
    # Insert and evict oldest if limit exceeded
    tts_cache[key] = value
    tts_cache.move_to_end(key)

    if len(tts_cache) > TTS_CACHE_LIMIT:
        tts_cache.popitem(last=False)


# Language


VOICE_MAP = {
    "en": "en-US-EricNeural",
    "nl": "nl-NL-FennaNeural",
    "pl": "pl-PL-MarekNeural",
    "fr": "fr-FR-HenriNeural",
    "it": "it-IT-ElsaNeural",
    "es": "es-MX-JorgeNeural",
    "de": "de-DE-ConradNeural",
    "ru": "ru-RU-DmitryNeural",
}


# SQlite Database
def init_db():
    with sqlite3.connect("bot.db") as conn:
        cursor = conn.cursor()
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS user_languages (
            user_id INTEGER PRIMARY KEY,
            language TEXT NOT NULL
        )
        """)
        conn.commit()


init_db()


def get_user_language(user_id):
    with sqlite3.connect("bot.db") as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT language FROM user_languages WHERE user_id = ?",
                       (user_id,))
        row = cursor.fetchone()
        return row[0] if row else "en"


async def language_autocomplete(
    interaction: discord.Interaction,
    current: str
):
    return [
        discord.app_commands.Choice(name=code, value=code)
        for code in VOICE_MAP.keys()
        if current.lower() in code.lower()
    ][:25]


def set_user_language(user_id, code):
    with sqlite3.connect("bot.db") as conn:
        cursor = conn.cursor()
        cursor.execute("""
        INSERT INTO user_languages (user_id, language)
        VALUES (?, ?)
        ON CONFLICT(user_id)
        DO UPDATE SET language=excluded.language
        """, (user_id, code))
        conn.commit()


# Acronyms
CUSTOM_REPLACEMENTS = {
    "rn": "right now",
    "gtg": "got to go",
    "cya": "see you",
    "brb": "be right back",
    "idk": "I don't know",
    "omw": "on my way",
    "smh": "shaking my head",
    "ty": "thanks",
    "ig": "i guess",
    "rq": "real quick",
    "tysm": "thank you so much",
    "wdym": "what do you mean",
    "ngl": "not gonna lie",
    "plz": "please",
    "abt": "about",
    "asap": "as soon as possible",
    "rw": "red wood",
    "cita": "citadel",
    "hc": "high castle",
    "mon": "monastary",
    "regi": "regiment",
    "engi": "engineer",
    "ft": "fast travel",
    "sgt": "sergeant",
    "ikr": "I know right",
    "atp": "at this point",
    "ts": "this shit",
    "pls": "please",
    "prob": "probably",
    "gng": "gang",
    "ur": "your",
    "smth": "something",
    "ik": "i know",
}

# FFmpeg


FFMPEG_OPTIONS = {
    "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
    "options": "-vn -loglevel quiet"
}
# Bot Setup

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)

voice_clients = {}
guild_queues = {}
guild_workers = {}
user_voice_lang = {}

# Text Processing


def replace_acronyms(text: str) -> str:
    words = text.split()
    for i, word in enumerate(words):
        key = word.lower()
        if key in CUSTOM_REPLACEMENTS:
            words[i] = CUSTOM_REPLACEMENTS[key]
    return " ".join(words)


async def process_message_text(message: discord.Message) -> str:
    text = message.clean_content

    timestamp_pattern = r"<t:(\d+):[a-zA-Z]>"

    def replace_timestamp(match):
        unix_time = int(match.group(1))
        dt = datetime.fromtimestamp(unix_time, tz=timezone.utc)
        return dt.strftime("%B %d %Y at %I:%M %p UTC")

    text = re.sub(timestamp_pattern, replace_timestamp, text)
    text = replace_acronyms(text)

    # Remove @ From Names
    text = re.sub(r"@(\w+)", r"\1", text)

    return text.strip()

# TTS Streaming


async def generate_tts_stream(text: str, voice: str) -> bytes:
    key = make_cache_key(text, voice)
    cached = get_cached_tts(key)
    if cached is not None:
        return cached

    communicate = edge_tts.Communicate(
        text=text,
        voice=voice,
        pitch=TTS_PITCH,
        rate=TTS_RATE
    )

    audio_bytes = bytearray()

    async for chunk in communicate.stream():
        if chunk["type"] == "audio":
            audio_bytes.extend(chunk["data"])

    if not audio_bytes:
        raise RuntimeError("Edge Returned Empty Audio Stream.")

    data = bytes(audio_bytes)
    set_cached_tts(key, data)
    return bytes(audio_bytes)


async def tts_worker(guild_id: int):
    queue = guild_queues[guild_id]

    while True:
        try:
            voice_client = voice_clients.get(guild_id)

            if not voice_client or not voice_client.is_connected():
                await asyncio.sleep(0.1)
                continue

            # Batch same-author rapid messages
            message = await queue.get()
            messages = [message]
            author_id = message.author.id

            await asyncio.sleep(0.05)

            while True:
                try:
                    next_msg = queue.get_nowait()
                except asyncio.QueueEmpty:
                    break

                if next_msg.author.id == author_id:
                    messages.append(next_msg)
                else:
                    await queue.put(next_msg)
                    break

            # Message processing
            processed_parts = []

            for msg in messages:
                processed = await process_message_text(msg)
                if processed:
                    processed_parts.append(processed)

            for _ in messages:
                queue.task_done()

            if not processed_parts:
                continue

            combined_text = " ".join(processed_parts)

            # Length guard
            if len(combined_text) > 1500:
                combined_text = combined_text[:1500]

            lang_code = get_user_language(author_id)

            voice_name = VOICE_MAP.get(lang_code, VOICE_MAP["en"])

            try:
                audio_bytes = await generate_tts_stream(combined_text, voice_name)
            except Exception as e:
                print(f"[EDGE FAIL - Guild {guild_id}] {e}")
                continue

            source = discord.FFmpegPCMAudio(
                io.BytesIO(audio_bytes),
                pipe=True)

            voice_client.play(source)

            while voice_client.is_playing():
                await asyncio.sleep(0.5)
        except asyncio.CancelledError:
            break

        except Exception as e:
            print(f"[TTS ERROR - Guild {guild_id}] {e}")

# Events


@bot.event
async def on_ready():
    await bot.tree.sync()
    print(f"Successfully Logged In As {bot.user}")


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    if (
        message.guild
        and message.channel.id in TEXT_CHANNEL_IDS
        and message.guild.id in voice_clients
    ):
        guild_id = message.guild.id

        if guild_id not in guild_queues:
            guild_queues[guild_id] = asyncio.Queue()
            guild_workers[guild_id] = asyncio.create_task(
                tts_worker(guild_id)
            )

        await guild_queues[guild_id].put(message)

    await bot.process_commands(message)

# Slash Commands


@bot.tree.command(name="join", description="Join Your Voice Channel")
async def join(interaction: discord.Interaction):
    if not interaction.user.voice:
        await interaction.response.send_message(
            "You must be in a voice channel.",
            ephemeral=True
        )
        return

    channel = interaction.user.voice.channel
    guild = interaction.guild

    if guild.voice_client:
        if guild.voice_client.channel != channel:
            await guild.voice_client.move_to(channel)
        await interaction.response.send_message("Connected.")
        return

    voice_client = await channel.connect()
    voice_clients[guild.id] = voice_client

    await interaction.response.send_message("Joined voice channel.")
    print(
        f"[JOIN] Bot joined channel '{channel}' In '{guild.name}' By '{interaction.user}'")


@bot.tree.command(name="leave", description="Leave Voice Channel")
async def leave(interaction: discord.Interaction):
    guild_id = interaction.guild.id
    channel = interaction.user.voice.channel
    voice_client = voice_clients.get(guild_id)

    if voice_client and voice_client.is_connected():
        await voice_client.disconnect()
        voice_clients.pop(guild_id, None)

        if guild_id in guild_workers:
            guild_workers[guild_id].cancel()
            guild_workers.pop(guild_id, None)

        guild_queues.pop(guild_id, None)

        await interaction.response.send_message("Disconnected.")
        temp_path = f"temp_{guild_id}.mp3"
        try:
            os.remove(temp_path)
        except FileNotFoundError:
            print("[ERROR] Temp File Couldn't Be Found & Deleted")
            pass

        # Logging
        print(
            f"[LEAVE] Bot left channel '{channel}' In '{interaction.guild.name} By '{interaction.user}'")
    else:
        await interaction.response.send_message(
            "Not in a voice channel.",
            ephemeral=True
        )


@bot.tree.command(name="skip", description="Skip Current TTS")
async def skip(interaction: discord.Interaction):
    guild_id = interaction.guild.id
    voice_client = voice_clients.get(guild_id)

    if not voice_client or not voice_client.is_connected():
        await interaction.response.send_message(
            "Not connected.",
            ephemeral=True
        )
        return

    if voice_client.is_playing():
        voice_client.stop()
        await interaction.response.send_message("Skipped.")
        print(
            f"[SKIP] Bot skipped TTS in channel '{voice_client.channel}' In '{interaction.guild.name}' By '{interaction.user}'")
    else:
        await interaction.response.send_message(
            "Nothing playing.",
            ephemeral=True
        )


@bot.tree.command(name="language", description="Set TTS Language")
@app_commands.autocomplete(code=language_autocomplete)
async def language(interaction: discord.Interaction, code: str):
    code = code.lower().strip()

    if code not in VOICE_MAP:
        await interaction.response.send_message(
            f"Invalid language. Options: {', '.join(VOICE_MAP)}",
            ephemeral=True
        )
        return
    # DB writing
    set_user_language(interaction.user.id, code)

    await interaction.response.send_message(
        f"Language set to `{code}`.",
        ephemeral=False
    )


@bot.tree.command(name="react", description="React To Previous Message")
async def react_previous(interaction: discord.Interaction, emoji: str):
    channel = interaction.channel

    messages = [msg async for msg in channel.history(limit=2)]
    if len(messages) < 2:
        await interaction.response.send_message(
            "No previous message found.",
            ephemeral=True
        )
        return
    previous_message = messages[0]

    try:
        await previous_message.add_reaction(emoji)
        await interaction.response.send_message(
            f"Reacted with {emoji}",
            ephemeral=True
        )
    except discord.HTTPException:
        await interaction.response.send_message(
            "Invalid emoji or missing permissions.",
            ephemeral=True)
        print(
            f"[ERROR] API Call Rejected, '{channel}' In '{interaction.guild.name}' By '{interaction.user}' ")


# Run

bot.run(TOKEN)
