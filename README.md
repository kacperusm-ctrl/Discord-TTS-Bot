Discord Streaming TTS Bot - Requires discord.py 2.0+ & FFmpeg in path
Low-latency text-to-speech Discord bot using Edge TTS with real-time PCM streaming and caching.

TTS Structure
Discord Message ->
Text Processing (cleanup, acronym expansion) ->
Edge TTS Streaming->
FFmpeg (MP3 → PCM)->
Async Queue (PCM frames)->
Discord Voice Client Playback

Put your discord bot token in .env as DISCORD_TOKEN=your_bot_token_here

Commands
/join	            Join voice channel
/leave	            Leave voice channel
/skip	            Skip current TTS
/language	        Set user voice
/channel_add	    Enable TTS in channel
/channel_remove	    Disable TTS in channel

Max message length: 1000 characters
GIFs are ignored
Only configured channels trigger TTS
Requires user to be in voice channel

