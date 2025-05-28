import discord
from discord.ext import commands, tasks
import os
from dotenv import load_dotenv
import yt_dlp
import asyncio
import traceback # For detailed error printing

# --- Configuration ---
load_dotenv()
TOKEN = os.getenv('DISCORD_TOKEN')
PREFIX = "!" # Or your preferred prefix

# Suppress noise about console usage from errors
yt_dlp.utils.bug_reports_message = lambda: ''

# YTDL options for streaming audio (temporarily verbose for debugging)
YTDL_FORMAT_OPTIONS = {
    'format': 'bestaudio/best',
    'outtmpl': '%(extractor)s-%(id)s-%(title)s.%(ext)s',
    'restrictfilenames': True,
    'noplaylist': True,
    'nocheckcertificate': True,
    'ignoreerrors': False,
    'logtostderr': True,  # VERBOSE FOR DEBUG
    'quiet': False,       # VERBOSE FOR DEBUG
    'no_warnings': True,  # Still keep warnings off unless needed
    'default_search': 'auto',
    'source_address': '0.0.0.0',  # Bind to all IPs (might be needed in some environments)
}

FFMPEG_OPTIONS = {
    'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5',
    'options': '-vn',
}

# --- Bot Setup ---
intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.voice_states = True # Needed for voice client and on_voice_state_update

bot = commands.Bot(command_prefix=PREFIX, intents=intents, help_command=None) # Custom help later if needed

# --- Global State for Music ---
music_queues = {} # guild_id: {"queue": [], "voice_client": None, "current_song": None, "loop": False, "keep_alive_active": False, "is_playing_silence": False, "last_channel_id": None}

def get_guild_state(guild_id):
    if guild_id not in music_queues:
        music_queues[guild_id] = {
            "queue": [],
            "voice_client": None,
            "current_song": None,
            "loop": False,
            "keep_alive_active": False,
            "is_playing_silence": False,
            "last_channel_id": None
        }
    return music_queues[guild_id]

# --- Helper Functions ---
async def search_youtube(query: str):
    """Search YouTube or process a direct URL."""
    try:
        is_url = query.startswith("http://") or query.startswith("https://")
        
        search_target = query
        if not is_url:
            search_target = f"ytsearch:{query}"

        print(f"DEBUG: yt-dlp processing target: {search_target}")

        with yt_dlp.YoutubeDL(YTDL_FORMAT_OPTIONS) as ydl:
            loop = asyncio.get_event_loop()
            
            # Define the blocking function to be run in the executor
            def extract_info_sync():
                return ydl.extract_info(search_target, download=False)

            info = await loop.run_in_executor(None, extract_info_sync) 

            if not info:
                print(f"DEBUG: yt-dlp returned no info for: {search_target}")
                return None

            data_to_use = None
            if 'entries' in info and info['entries']:
                data_to_use = info['entries'][0]
                print(f"DEBUG: yt-dlp found entries, using first one.")
            elif 'url' in info and 'title' in info: 
                data_to_use = info
                print(f"DEBUG: yt-dlp processed direct URL or single result.")
            else:
                print(f"DEBUG: yt-dlp extracted data in unexpected format for '{search_target}'. Full info: {str(info)[:500]}") # Log part of the info
                return None
            
            if not data_to_use or 'url' not in data_to_use or 'title' not in data_to_use:
                print(f"DEBUG: yt-dlp extracted data missing 'url' or 'title'. Data used: {str(data_to_use)[:500]}")
                return None

            print(f"DEBUG: Successfully extracted - Title: {data_to_use['title']}, URL Source: {data_to_use['url'][:70]}...")
            return {"source": data_to_use['url'], "title": data_to_use['title']}

    except Exception as e:
        print(f"CRITICAL Error in search_youtube for '{query}': {e}")
        traceback.print_exc() # Print full traceback to logs
        return None


async def play_next(guild_id):
    state = get_guild_state(guild_id)
    if state["voice_client"] is None or not state["voice_client"].is_connected():
        print(f"[{guild_id}] Play_next: VC not connected or None.")
        state["current_song"] = None
        if state["keep_alive_active"]:
            await attempt_rejoin(guild_id)
        return

    if state["queue"]:
        state["is_playing_silence"] = False
        song_info = state["queue"].pop(0)
        state["current_song"] = song_info
        try:
            # Ensure FFmpeg path is correctly found or specify it if needed
            # executable_path = "ffmpeg" # Or full path if not in PATH on server
            player = discord.FFmpegPCMAudio(song_info['source'], **FFMPEG_OPTIONS) # executable=executable_path
            state["voice_client"].play(player, after=lambda e: asyncio.run_coroutine_threadsafe(play_next_after_error(e, guild_id), bot.loop))
            print(f"[{guild_id}] Now playing: {song_info['title']}")
        except Exception as e:
            print(f"[{guild_id}] Error instantiating player or playing {song_info.get('title', 'unknown song')}: {e}")
            traceback.print_exc()
            state["current_song"] = None
            await play_next(guild_id)
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
    state = get_guild_state(guild_id)
    if state["keep_alive_active"] and not state["current_song"] and \
       state["voice_client"] and state["voice_client"].is_connected() and \
       not state["voice_client"].is_playing() and not state["is_playing_silence"]:
        print(f"[{guild_id}] Attempting to play conceptual silent audio to keep alive.")
        state["is_playing_silence"] = True
        try:
            # This is a placeholder. A real silent stream is needed for robust 24/7.
            # For example, using anullsrc with FFmpeg.
            # FFmpegPCMAudio("-f lavfi -i anullsrc=channel_layout=stereo:sample_rate=48000 -t 5 -c:a pcm_s16le -f s16le pipe:1", **FFMPEG_OPTIONS)
            # This is complex to get right without testing the FFmpeg command.
            # For now, the task loop is the main "keep active" mechanism for the VC presence.
            # We can simulate a short activity here just to ensure the logic path is tested.
            await asyncio.sleep(1) # Minimal activity
            if state["is_playing_silence"]: # If state hasn't changed (e.g. new song added)
                state["is_playing_silence"] = False # Reset after conceptual play
                print(f"[{guild_id}] Conceptual silent audio period ended.")
        except Exception as e:
            print(f"[{guild_id}] Error during conceptual silent audio: {e}")
            state["is_playing_silence"] = False
    # else:
    #     state["is_playing_silence"] = False # Ensure it's false if conditions not met


async def attempt_rejoin(guild_id):
    state = get_guild_state(guild_id)
    if state["last_channel_id"]:
        channel = bot.get_channel(state["last_channel_id"])
        if channel and isinstance(channel, discord.VoiceChannel):
            print(f"[{guild_id}] Attempting to rejoin channel: {channel.name}")
            try:
                state["voice_client"] = await channel.connect(timeout=10.0, reconnect=True)
                await play_next(guild_id)
            except asyncio.TimeoutError:
                print(f"[{guild_id}] Timeout trying to rejoin {channel.name}.")
                state["voice_client"] = None
            except Exception as e:
                print(f"[{guild_id}] Error during rejoin attempt to {channel.name}: {e}")
                state["voice_client"] = None
        else:
            print(f"[{guild_id}] Could not find or invalid last channel ID: {state['last_channel_id']}")
    else:
        print(f"[{guild_id}] No last channel ID to rejoin.")


# --- Bot Events ---
@bot.event
async def on_ready():
    print(f'{bot.user.name} has connected to Discord!')
    print(f'Bot ID: {bot.user.id}')
    if not keep_alive_task.is_running():
        keep_alive_task.start()

@tasks.loop(seconds=60)
async def keep_alive_task():
    # print("DEBUG: Keep_alive_task running...") # Can be very noisy
    for guild_id, state in list(music_queues.items()):
        if state["voice_client"] and state["voice_client"].is_connected():
            if state["keep_alive_active"] and not state["voice_client"].is_playing() and \
               not state["queue"] and not state["current_song"] and not state["is_playing_silence"]:
                # print(f"Keep-alive: Guild {guild_id} is active, connected, not playing, queue empty, no current song, not playing silence.")
                await play_silent_audio_if_needed(guild_id)
        elif state["keep_alive_active"] and state["last_channel_id"]:
            print(f"Keep-alive task: Bot for guild {guild_id} (last known channel {state['last_channel_id']}) is not connected but should be active. Attempting rejoin.")
            await attempt_rejoin(guild_id)

@keep_alive_task.before_loop
async def before_keep_alive_task():
    await bot.wait_until_ready()


# --- Music Commands ---
@bot.command(name='join', help='Tells the bot to join the voice channel you are in.')
async def join(ctx):
    if not ctx.author.voice:
        await ctx.send(f"{ctx.author.mention} is not connected to a voice channel.")
        return

    channel = ctx.author.voice.channel
    guild_id = ctx.guild.id
    state = get_guild_state(guild_id)
    state["last_channel_id"] = channel.id

    if state["voice_client"] and state["voice_client"].is_connected():
        if state["voice_client"].channel == channel:
            await ctx.send("I'm already in this voice channel!")
        else:
            await state["voice_client"].move_to(channel)
            await ctx.send(f"Moved to **{channel.name}**.")
    else:
        try:
            state["voice_client"] = await channel.connect(timeout=10.0, reconnect=True)
            await ctx.send(f"Joined **{channel.name}**.")
        except asyncio.TimeoutError:
            await ctx.send(f"Timed out trying to join **{channel.name}**.")
            state["voice_client"] = None # Ensure it's None
        except Exception as e:
            await ctx.send(f"Could not join voice channel **{channel.name}**: {e}")
            if state["voice_client"]: # Defensive disconnect
                try: await state["voice_client"].disconnect(force=True)
                except: pass
            state["voice_client"] = None


@bot.command(name='stay', help='Tells the bot to join and stay in your current VC 24/7.')
async def stay(ctx):
    if not ctx.author.voice:
        await ctx.send(f"{ctx.author.mention} is not connected to a voice channel.")
        return

    guild_id = ctx.guild.id
    state = get_guild_state(guild_id)
    state["keep_alive_active"] = True
    state["last_channel_id"] = ctx.author.voice.channel.id # Set it before calling join

    await join(ctx) # join command handles connecting and updating state["voice_client"]

    if state["voice_client"] and state["voice_client"].is_connected():
        await ctx.send("Okay, I will try to stay in this channel.")
        await play_silent_audio_if_needed(guild_id) # Initial check
    else:
        # Join command would have sent a message if it failed
        print(f"[{guild_id}] Stay command: Join failed or VC not established.")
        state["keep_alive_active"] = False # Revert if join didn't establish VC


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
        state["last_channel_id"] = None
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
            await join(ctx) # This will set state["voice_client"]
            if not state["voice_client"] or not state["voice_client"].is_connected(): # Check again
                await ctx.send("Could not join your voice channel. Please use `!join` or `!stay` first.")
                return
        else:
            await ctx.send("You are not in a voice channel, and I'm not in one either. Use `!join` or `!stay` first.")
            return
    
    # Ensure we are in the same channel as the user if they are in one
    if ctx.author.voice and state["voice_client"].channel != ctx.author.voice.channel:
        await ctx.send(f"Moving to your channel: **{ctx.author.voice.channel.name}** to play.")
        await state["voice_client"].move_to(ctx.author.voice.channel)
        state["last_channel_id"] = ctx.author.voice.channel.id


    async with ctx.typing():
        song_info = await search_youtube(query)

        if song_info is None:
            await ctx.send(f"Could not find or process the song: `{query}`.")
            return

        state["queue"].append(song_info)
        await ctx.send(f"Added to queue: **{song_info['title']}**")

    if state["voice_client"] and state["voice_client"].is_connected() and \
       not state["voice_client"].is_playing() and not state["current_song"] and \
       not state["is_playing_silence"]:
        await play_next(guild_id)

@bot.command(name='skip', aliases=['s'], help='Skips the current song.')
async def skip(ctx):
    guild_id = ctx.guild.id
    state = get_guild_state(guild_id)

    if state["voice_client"] and state["voice_client"].is_playing():
        state["voice_client"].stop() # Triggers 'after' in play() which calls play_next()
        await ctx.send("Skipped the current song.")
    elif state["current_song"]: # If something is loaded but not "playing" (e.g. error state)
        print(f"[{guild_id}] Skipping non-playing current song: {state['current_song']['title']}")
        state["current_song"] = None # Clear it
        await play_next(guild_id) # Attempt to play next from queue
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
        await ctx.send("Not playing anything to pause or already paused.")

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
        embed.add_field(name="💿 Now Playing", value=f"**{state['current_song']['title']}**", inline=False)
    # elif state["is_playing_silence"]:
    #     embed.add_field(name="💿 Now Playing", value="Playing silence to stay connected...", inline=False)
    else:
        embed.add_field(name="💿 Now Playing", value="Nothing specific is currently playing.", inline=False)

    if state["queue"]:
        song_list = ""
        for i, song in enumerate(state["queue"][:10]): # Show up to 10 songs
            song_list += f"{i+1}. {song['title']}\n"
        embed.add_field(name="🎶 Up Next", value=song_list if song_list else "Queue is empty.", inline=False)
        if len(state["queue"]) > 10:
            embed.set_footer(text=f"...and {len(state['queue']) - 10} more song(s).")
    else:
        embed.add_field(name="🎶 Up Next", value="Queue is empty.", inline=False)
        
    await ctx.send(embed=embed)

# --- Basic Error Handling ---
@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandNotFound):
        # To avoid spamming, only print to console or do nothing
        print(f"Command not found by {ctx.author}: {ctx.message.content}")
        # await ctx.send("Invalid command.") # Optional: send message to user
    elif isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(f"Missing argument: `{error.param.name}`. Please provide all required arguments for `!{ctx.command.name}`.")
    elif isinstance(error, commands.CommandInvokeError):
        print(f"Error invoking command '{ctx.command}'. Invoked by: {ctx.author}. Full message: '{ctx.message.content}'. Error: {error.original}")
        traceback.print_exception(type(error.original), error.original, error.original.__traceback__)
        await ctx.send(f"An error occurred while running the command `!{ctx.command.name}`. Please check the logs or try again.")
    elif isinstance(error, commands.CheckFailure):
        await ctx.send("You do not have the necessary permissions to use this command.")
    else:
        print(f"An unhandled error occurred type {type(error)}: {error}")
        traceback.print_exception(type(error), error, error.__traceback__)
        # await ctx.send("An unexpected error occurred.")

# --- Run the Bot ---
if __name__ == "__main__":
    print("DEBUG: Main script execution started...")
    # Simple check for token for security, print only partial
    token_preview = "TOKEN_NOT_SET"
    if TOKEN:
        token_preview = f"{TOKEN[:5]}...{TOKEN[-5:]}" if len(TOKEN) > 10 else "TOKEN_TOO_SHORT"
    print(f"DEBUG: Token from env preview: {token_preview}")

    if TOKEN:
        try:
            print("DEBUG: Attempting to run bot with token...")
            bot.run(TOKEN)
        except discord.errors.LoginFailure:
            print("CRITICAL ERROR: Login Failure! Your DISCORD_TOKEN is incorrect or invalid. Double-check it in your .env file (locally) or environment variables (on server) and on the Discord Developer Portal. Also, make sure all necessary intents are enabled on the portal.")
        except Exception as e:
            print(f"CRITICAL ERROR during bot.run (outer try-except): {e}")
            traceback.print_exc()
    else:
        print("CRITICAL ERROR: DISCORD_TOKEN not found in environment variables. Bot cannot start.")

    print("DEBUG: bot.run() has exited or script finished (this line should ideally not be reached if bot is running correctly).")
