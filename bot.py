import discord
from discord.ext import commands, tasks
import os
from dotenv import load_dotenv
import yt_dlp
import asyncio

# --- Configuration ---
load_dotenv()
TOKEN = os.getenv('DISCORD_TOKEN')
PREFIX = "!" # Or your preferred prefix

# Suppress noise about console usage from errors
yt_dlp.utils.bug_reports_message = lambda: ''

# YTDL options for streaming audio
YTDL_FORMAT_OPTIONS = {
    'format': 'bestaudio/best',
    'outtmpl': '%(extractor)s-%(id)s-%(title)s.%(ext)s',
    'restrictfilenames': True,
    'noplaylist': True,
    'nocheckcertificate': True,
    'ignoreerrors': False,
    'logtostderr': False,
    'quiet': False,
    'no_warnings': True,
    'default_search': 'auto',
    'source_address': '0.0.0.0',
}

FFMPEG_OPTIONS = {
    'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5',
    'options': '-vn',
}

# --- Bot Setup ---
intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.voice_states = True

bot = commands.Bot(command_prefix=PREFIX, intents=intents, help_command=None)

# --- Global State for Music ---
music_queues = {}

def get_guild_state(guild_id):
    if guild_id not in music_queues:
        music_queues[guild_id] = {
            "queue": [], "voice_client": None, "current_song": None,
            "loop": False, "keep_alive_active": False, "is_playing_silence": False,
            "last_channel_id": None # For trying to rejoin
        }
    return music_queues[guild_id]

# --- Helper Functions ---
async def search_youtube(query: str):
    try:
        with yt_dlp.YoutubeDL(YTDL_FORMAT_OPTIONS) as ydl:
            loop = asyncio.get_event_loop()
            data = await loop.run_in_executor(None, lambda: ydl.extract_info(f"ytsearch:{query}", download=False)['entries'][0])
        return {"source": data['url'], "title": data['title']}
    except Exception as e:
        print(f"Error searching YouTube for '{query}': {e}")
        return None

async def play_next(guild_id):
    state = get_guild_state(guild_id)
    if state["voice_client"] is None or not state["voice_client"].is_connected():
        print(f"[{guild_id}] Play_next: VC not connected or None.")
        state["current_song"] = None
        if state["keep_alive_active"]: # Attempt to rejoin if we were supposed to stay
            await attempt_rejoin(guild_id)
        return

    if state["queue"]:
        state["is_playing_silence"] = False
        song_info = state["queue"].pop(0)
        state["current_song"] = song_info
        try:
            player = discord.FFmpegPCMAudio(song_info['source'], **FFMPEG_OPTIONS)
            state["voice_client"].play(player, after=lambda e: asyncio.run_coroutine_threadsafe(play_next_after_error(e, guild_id), bot.loop))
            # Optionally send "Now playing" message here if you have a ctx object or store last ctx
            print(f"[{guild_id}] Now playing: {song_info['title']}")
        except Exception as e:
            print(f"[{guild_id}] Error playing {song_info.get('title', 'unknown song')}: {e}")
            state["current_song"] = None
            await play_next(guild_id) # Try next song
    else:
        state["current_song"] = None
        print(f"[{guild_id}] Queue empty.")
        if state["keep_alive_active"]:
            await play_silent_audio_if_needed(guild_id)

async def play_next_after_error(error, guild_id):
    if error:
        print(f'[{guild_id}] Player error: {error}')
    await play_next(guild_id)

async def play_silent_audio_if_needed(guild_id):
    # This function is more conceptual for now. A true silent stream is better.
    # The keep_alive_task handles the "staying active" part more directly.
    state = get_guild_state(guild_id)
    if state["keep_alive_active"] and not state["current_song"] and \
       state["voice_client"] and state["voice_client"].is_connected() and \
       not state["voice_client"].is_playing() and not state["is_playing_silence"]:
        print(f"[{guild_id}] (Conceptual) Playing silent audio to keep alive.")
        state["is_playing_silence"] = True
        # In a real scenario, you'd play an actual silent FFmpeg stream here.
        # For now, we just mark it and let the keep_alive_task manage checks.
        # After a short conceptual "silent play", reset:
        await asyncio.sleep(5) # Simulate a short silent segment being "played"
        if state["is_playing_silence"]: # check if still in this state
             state["is_playing_silence"] = False
             # This ensures if nothing else happens, next check might try again or play_next handles empty queue.
    else:
        state["is_playing_silence"] = False


async def attempt_rejoin(guild_id):
    state = get_guild_state(guild_id)
    if state["last_channel_id"]:
        try:
            channel = bot.get_channel(state["last_channel_id"])
            if channel and isinstance(channel, discord.VoiceChannel):
                print(f"[{guild_id}] Attempting to rejoin channel: {channel.name}")
                state["voice_client"] = await channel.connect()
                await play_next(guild_id) # Try playing again if queue had items
            else:
                print(f"[{guild_id}] Could not find or connect to last channel ID: {state['last_channel_id']}")
        except Exception as e:
            print(f"[{guild_id}] Error during rejoin attempt: {e}")
            state["voice_client"] = None # Ensure VC is None if rejoin fails
    else:
        print(f"[{guild_id}] No last channel ID to rejoin.")

# --- Bot Events ---
@bot.event
async def on_ready():
    print(f'{bot.user.name} has connected to Discord!')
    print(f'Bot ID: {bot.user.id}')
    if not keep_alive_task.is_running():
        keep_alive_task.start()

@tasks.loop(seconds=60)  # Check less frequently to be less noisy
async def keep_alive_task():
    for guild_id, state in list(music_queues.items()): # list() for safe iteration if modified
        if state["voice_client"] and state["voice_client"].is_connected():
            if state["keep_alive_active"] and not state["voice_client"].is_playing() and not state["queue"] and not state["current_song"]:
                if not state["is_playing_silence"]:
                    print(f"Keep-alive task: Triggering silent audio for guild {guild_id}")
                    await play_silent_audio_if_needed(guild_id)
        elif state["keep_alive_active"] and state["last_channel_id"]: # If keep_alive but disconnected
            print(f"Keep-alive task: Bot for guild {guild_id} disconnected but should be active. Attempting rejoin.")
            await attempt_rejoin(guild_id)


# --- Music Commands ---
@bot.command(name='join', help='Tells the bot to join the voice channel you are in.')
async def join(ctx):
    if not ctx.author.voice:
        await ctx.send(f"{ctx.author.mention} is not connected to a voice channel.")
        return

    channel = ctx.author.voice.channel
    guild_id = ctx.guild.id
    state = get_guild_state(guild_id)
    state["last_channel_id"] = channel.id # Store for potential rejoin

    if state["voice_client"] and state["voice_client"].is_connected():
        if state["voice_client"].channel == channel:
            await ctx.send("I'm already in this voice channel!")
        else:
            await state["voice_client"].move_to(channel)
            await ctx.send(f"Moved to **{channel.name}**.")
    else:
        try:
            state["voice_client"] = await channel.connect()
            await ctx.send(f"Joined **{channel.name}**.")
        except Exception as e:
            await ctx.send(f"Could not join voice channel: {e}")
            if state["voice_client"]:
                await state["voice_client"].disconnect(force=True)
            state["voice_client"] = None


@bot.command(name='stay', help='Tells the bot to join and stay in your current VC 24/7.')
async def stay(ctx):
    if not ctx.author.voice:
        await ctx.send(f"{ctx.author.mention} is not connected to a voice channel.")
        return

    guild_id = ctx.guild.id
    state = get_guild_state(guild_id)
    state["keep_alive_active"] = True
    state["last_channel_id"] = ctx.author.voice.channel.id

    await join(ctx)

    if state["voice_client"] and state["voice_client"].is_connected():
        await ctx.send("Okay, I will try to stay in this channel.")
        await play_silent_audio_if_needed(guild_id)
    else:
        await ctx.send("Could not join to stay. Please try `!join` first.")
        state["keep_alive_active"] = False


@bot.command(name='leave', help='To make the bot leave the voice channel.')
async def leave(ctx):
    guild_id = ctx.guild.id
    state = get_guild_state(guild_id)

    if state["voice_client"] and state["voice_client"].is_connected():
        state["keep_alive_active"] = False
        state["is_playing_silence"] = False
        state["queue"].clear()
        state["current_song"] = None
        if state["voice_client"].is_playing():
            state["voice_client"].stop()
        await state["voice_client"].disconnect()
        state["voice_client"] = None
        state["last_channel_id"] = None # Clear last channel on explicit leave
        await ctx.send("Left the voice channel.")
    else:
        await ctx.send("I am not in a voice channel.")

@bot.command(name='play', aliases=['p'], help='Plays a song from YouTube (URL or search query)')
async def play(ctx, *, query: str):
    guild_id = ctx.guild.id
    state = get_guild_state(guild_id)

    if not state["voice_client"] or not state["voice_client"].is_connected():
        if ctx.author.voice:
            await ctx.send("Joining your voice channel first...")
            await join(ctx)
            if not state["voice_client"] or not state["voice_client"].is_connected():
                await ctx.send("Could not join your voice channel. Please use `!join` or `!stay` first.")
                return
        else:
            await ctx.send("You are not in a voice channel, and I'm not in one either.")
            return

    async with ctx.typing():
        song_info = await search_youtube(query)

        if song_info is None:
            await ctx.send(f"Could not find or process the song: `{query}`.")
            return

        state["queue"].append(song_info)
        await ctx.send(f"Added to queue: **{song_info['title']}**")

    if not state["voice_client"].is_playing() and not state["current_song"] and not state["is_playing_silence"]:
        await play_next(guild_id)

@bot.command(name='skip', aliases=['s'], help='Skips the current song.')
async def skip(ctx):
    guild_id = ctx.guild.id
    state = get_guild_state(guild_id)

    if state["voice_client"] and state["voice_client"].is_playing():
        state["voice_client"].stop()
        await ctx.send("Skipped the current song.")
    elif state["current_song"]:
        state["current_song"] = None
        await play_next(guild_id)
        await ctx.send("Skipped. Trying next song.")
    else:
        await ctx.send("Not playing anything to skip.")


@bot.command(name='stop', help='Stops the music and clears the queue.')
async def stop(ctx):
    guild_id = ctx.guild.id
    state = get_guild_state(guild_id)

    if state["voice_client"] and state["voice_client"].is_connected():
        state["queue"].clear()
        state["current_song"] = None
        state["is_playing_silence"] = False
        if state["voice_client"].is_playing():
            state["voice_client"].stop()
        await ctx.send("Music stopped and queue cleared.")
    else:
        await ctx.send("Not in a voice channel or not playing anything.")

@bot.command(name='pause', help='Pauses the current song.')
async def pause(ctx):
    guild_id = ctx.guild.id
    state = get_guild_state(guild_id)
    if state["voice_client"] and state["voice_client"].is_playing():
        state["voice_client"].pause()
        await ctx.send("Paused music.")
    else:
        await ctx.send("Not playing anything to pause.")

@bot.command(name='resume', help='Resumes the paused song.')
async def resume(ctx):
    guild_id = ctx.guild.id
    state = get_guild_state(guild_id)
    if state["voice_client"] and state["voice_client"].is_paused():
        state["voice_client"].resume()
        await ctx.send("Resumed music.")
    else:
        await ctx.send("Music is not paused or nothing to resume.")


@bot.command(name='queue', aliases=['q'], help='Shows the current music queue.')
async def queue(ctx):
    guild_id = ctx.guild.id
    state = get_guild_state(guild_id)
    
    if not state["queue"] and not state["current_song"]:
        await ctx.send("The queue is empty and nothing is currently playing.")
        return

    embed = discord.Embed(title="Music Queue", color=discord.Color.blue())
    
    if state["current_song"]:
        embed.add_field(name="Now Playing", value=f"**{state['current_song']['title']}**", inline=False)
    else:
        embed.add_field(name="Now Playing", value="Nothing is currently playing.", inline=False)

    if state["queue"]:
        song_list = ""
        for i, song in enumerate(state["queue"][:10]):
            song_list += f"{i+1}. {song['title']}\n"
        embed.add_field(name="Up Next", value=song_list if song_list else "Queue is empty.", inline=False)
        if len(state["queue"]) > 10:
            embed.set_footer(text=f"...and {len(state['queue']) - 10} more song(s).")
    else:
        embed.add_field(name="Up Next", value="Queue is empty.", inline=False)
        
    await ctx.send(embed=embed)

# --- Basic Error Handling ---
@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandNotFound):
        # await ctx.send("Invalid command. (No general help command enabled yet)")
        print(f"Command not found: {ctx.message.content}") # Log instead of sending for now
    elif isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(f"Missing argument: `{error.param.name}`. Please provide all required arguments.")
    elif isinstance(error, commands.CommandInvokeError):
        print(f"Error invoking command {ctx.command}: {error.original}")
        await ctx.send(f"An error occurred while running the command: {error.original}")
    else:
        print(f"An unhandled error occurred: {error}")
        # await ctx.send("An unexpected error occurred.") # Can be spammy

# --- Run the Bot ---
if __name__ == "__main__":
    print("DEBUG: Script starting...")
    print(f"DEBUG: Token from env is: {TOKEN[:5]}...{TOKEN[-5:] if TOKEN and len(TOKEN) > 10 else 'TOKEN_TOO_SHORT_OR_NONE'}") # Print partial token for check

    if TOKEN:
        try:
            print("DEBUG: Attempting to run bot with token...")
            bot.run(TOKEN)
        except discord.errors.LoginFailure:
            print("CRITICAL ERROR: Login Failure! Your DISCORD_TOKEN is incorrect or invalid.")
        except Exception as e:
            print(f"CRITICAL ERROR during bot.run: {e}")
    else:
        print("CRITICAL ERROR: DISCORD_TOKEN not found in environment variables.")

    print("DEBUG: bot.run() has exited or script finished.") # Should not be reached if bot runs successfully
