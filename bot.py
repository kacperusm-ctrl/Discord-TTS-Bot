import os
import discord
from discord import app_commands
from discord.ext import commands
import asyncio
import edge_tts
from dotenv import load_dotenv
import sqlite3
import io
import re
from datetime import datetime, timezone

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")

# Config

TEXT_CHANNEL_IDS = [
    1473527027280773120,
    1471319865817169921
]

TTS_PITCH = "+8Hz"
TTS_RATE = "+5%"

# Language


VOICE_MAP = {
    "en": "en-US-EricNeural",
    "nl": "nl-NL-FennaNeural",
    "pl": "pl-PL-MarekNeural",
    "fr": "fr-FR-HenriNeural",
    "it": "it-IT-ElsaNeural",
    "es": "es-ES-LuisNeural",
}


conn = sqlite3.connect("bot.db")
cursor = conn.cursor()

cursor.execute("""
CREATE TABLE IF NOT EXISTS user_languages (
    user_id INTEGER PRIMARY KEY,
    language TEXT NOT NULL
)
""")
conn.commit()


async def language_autocomplete(
    interaction: discord.Interaction,
    current: str
):
    return [
        discord.app_commands.Choice(name=code, value=code)
        for code in VOICE_MAP.keys()
        if current.lower() in code.lower()
    ][:25]

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
    "ft": "fast travel",
    "sgt": "sergeant",
    "stfu": "shut up",
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

    return text.strip()

# TTS Streaming


async def generate_tts_stream(text: str, voice: str) -> bytes:
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
        raise RuntimeError("Edge returned empty audio stream.")

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

            await asyncio.sleep(0.02)

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

            # Combine raw clean_content first
            raw_text_parts = [
                msg.clean_content.strip()
                for msg in messages
                if msg.clean_content.strip()
            ]

            for _ in messages:
                queue.task_done()

            if not raw_text_parts:
                continue

            combined_text = " ".join(raw_text_parts)

            # Timestamp
            timestamp_pattern = r"<t:(\d+):[a-zA-Z]>"

            def replace_timestamp(match):
                unix_time = int(match.group(1))
                dt = datetime.fromtimestamp(unix_time, tz=timezone.utc)
                return dt.strftime("%B %d %Y at %I:%M %p UTC")

            combined_text = re.sub(
                timestamp_pattern, replace_timestamp, combined_text)

            def acronym_replacer(match):
                word = match.group(0)
                lower = word.lower()
                return CUSTOM_REPLACEMENTS.get(lower, word)

            combined_text = re.sub(
                r"\b\w+\b",
                acronym_replacer,
                combined_text
            )

            combined_text = combined_text.strip()

            if not combined_text:
                continue

            # Length guard
            if len(combined_text) > 2500:
                combined_text = combined_text[:2500]

            cursor.execute(
                "SELECT language FROM user_languages WHERE user_id = ?",
                (author_id,)
            )
            row = cursor.fetchone()
            lang_code = row[0] if row else "en"
            voice_name = VOICE_MAP.get(lang_code, VOICE_MAP["en"])

            try:
                audio_bytes = await generate_tts_stream(combined_text, voice_name)
            except Exception as e:
                print(f"[EDGE FAIL - Guild {guild_id}] {e}")
                continue

            temp_path = f"temp_{guild_id}.mp3"

            with open(temp_path, "wb") as f:
                f.write(audio_bytes)

            source = discord.FFmpegPCMAudio(temp_path)

            voice_client.play(source)

            while voice_client.is_playing():
                await asyncio.sleep(0.5)
            try:
                os.remove(temp_path)
            except FileNotFoundError:
                pass
        except asyncio.CancelledError:
            break

        except Exception as e:
            print(f"[TTS ERROR - Guild {guild_id}] {e}")

# Events


@bot.event
async def on_ready():
    await bot.tree.sync()
    print(f"Logged in as {bot.user}")


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
        f"[JOIN] Bot joined channel '{channel}' in server '{guild.name}' triggered by user '{interaction.user}'")


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
            pass

        # Logging
        print(
            f"[LEAVE] Bot left channel '{channel}' in server '{interaction.guild.name} triggered by user '{interaction.user}'")
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
            f"[SKIP] Bot skipped TTS in channel '{voice_client.channel}'in server '{interaction.guild.name}' triggered by user '{interaction.user}'")
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

    cursor.execute("""
    INSERT INTO user_languages (user_id, language)
    VALUES (?, ?)
    ON CONFLICT(user_id)
    DO UPDATE SET language=excluded.language
    """, (interaction.user.id, code))

    conn.commit()

    await interaction.response.send_message(
        f"Language set to `{code}`.",
        ephemeral=False
    )

# Run

bot.run(TOKEN)
