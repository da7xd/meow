import discord
from discord.ext import commands, tasks
import os
from dotenv import load_dotenv
import yt_dlp
import asyncio
import traceback # For detailed error printing

# --- ATTEMPT TO EXPLICITLY LOAD OPUS ---
# THIS BLOCK IS ADDED AS PER YOUR REQUEST (STEP 2)
OPUS_LIBS = ['libopus.so.0', 'libopus.so', 'opus'] # Common Linux names for the Opus shared library
for lib_name in OPUS_LIBS:
    try:
        discord.opus.load_opus(lib_name)
        print(f"DEBUG: Opus library loaded successfully: {lib_name}")
        break # Exit loop if successfully loaded
    except discord.opus.OpusNotLoaded:
        print(f"DEBUG: Opus library '{lib_name}' not found (OpusNotLoaded).")
    except OSError as e: # Handles cases where file is found but can't be loaded (e.g. wrong architecture)
        print(f"DEBUG: Opus library '{lib_name}' found but encountered an OSError during load: {e}")
else: # This 'else' corresponds to the 'for' loop, executes if 'break' was never hit
    print("CRITICAL DEBUG: FAILED TO LOAD ANY SPECIFIED OPUS LIBRARY. Voice playback will likely fail.")
# --- END ATTEMPT TO EXPLICITLY LOAD OPUS ---


# --- Configuration ---
load_dotenv()
TOKEN = os.getenv('DISCORD_TOKEN')
PREFIX = "!"

YTDL_FORMAT_OPTIONS = {
    'format': 'bestaudio/best',
    'outtmpl': '%(extractor)s-%(id)s-%(title)s.%(ext)s',
    'restrictfilenames': True,
    'noplaylist': True,
    'nocheckcertificate': True,
    'ignoreerrors': False,
    'logtostderr': True,
    'quiet': False,
    'no_warnings': True,
    'default_search': 'auto',
    'source_address': '0.0.0.0',
    'cookiefile': 'cookies.txt'
}

FFMPEG_OPTIONS = {
    'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 -analyzeduration 20M -probesize 20M',
    'options': '-vn -loglevel error',
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
            "is_playing": False,
            "loop_song": False, "loop_queue": False,
            "keep_alive_active": False, "is_playing_silence": False,
            "last_channel_id": None, "last_ctx": None
        }
    return music_queues[guild_id]

# --- Helper Functions ---
async def search_youtube(query: str):
    try:
        is_url = query.startswith("http://") or query.startswith("https://")
        search_target = query if is_url else f"ytsearch:{query}"
        print(f"DEBUG: yt-dlp processing target: {search_target} with cookiefile: {YTDL_FORMAT_OPTIONS.get('cookiefile')}")
        with yt_dlp.YoutubeDL(YTDL_FORMAT_OPTIONS) as ydl:
            loop = asyncio.get_event_loop()
            def extract_info_sync():
                return ydl.extract_info(search_target, download=False)
            info = await loop.run_in_executor(None, extract_info_sync)
            if not info:
                print(f"DEBUG: yt-dlp returned no info for: {search_target}")
                return None
            data_to_use = None
            if 'entries' in info and info['entries']: data_to_use = info['entries'][0]
            elif 'url' in info and 'title' in info: data_to_use = info
            else:
                print(f"DEBUG: yt-dlp extracted data in unexpected format for '{search_target}'.")
                return None
            if not data_to_use or 'url' not in data_to_use or 'title' not in data_to_use:
                print(f"DEBUG: yt-dlp extracted data missing 'url' or 'title'.")
                return None
            print(f"DEBUG: Successfully extracted - Title: {data_to_use['title']}")
            return {"source": data_to_use['url'], "title": data_to_use['title']}
    except Exception as e:
        print(f"CRITICAL Error in search_youtube for '{query}': {e}")
        traceback.print_exc()
        return None

async def play_song_in_vc(guild_id, song_info):
    state = get_guild_state(guild_id)
    if not state["voice_client"] or not state["voice_client"].is_connected():
        state["is_playing"] = False
        state["current_song"] = None
        print(f"[{guild_id}] play_song_in_vc: VC not connected. Aborting play.")
        return

    state["current_song"] = song_info
    state["is_playing_silence"] = False
    state["is_playing"] = True
    print(f"[{guild_id}] Attempting to play: {song_info['title']}")
    try:
        player = discord.FFmpegPCMAudio(song_info['source'], **FFMPEG_OPTIONS)
        state["voice_client"].play(player, after=lambda e: asyncio.run_coroutine_threadsafe(song_finished_callback(e, guild_id), bot.loop))
        print(f"[{guild_id}] DISCORD.PY .play() CALLED for: {song_info['title']}")
        if state["last_ctx"]:
            try: await state["last_ctx"].send(f"Now playing: **{song_info['title']}**")
            except Exception as send_err: print(f"[{guild_id}] Error sending 'Now playing' msg: {send_err}")
    except Exception as e:
        print(f"[{guild_id}] Error in play_song_in_vc for {song_info.get('title', 'unknown')}: {e}")
        traceback.print_exc()
        state["is_playing"] = False
        state["current_song"] = None
        asyncio.run_coroutine_threadsafe(song_finished_callback(e, guild_id), bot.loop)

async def song_finished_callback(error, guild_id):
    state = get_guild_state(guild_id)
    print(f"[{guild_id}] song_finished_callback triggered. Error: {error}")
    current_title = state["current_song"]["title"] if state["current_song"] else "N/A (already cleared or never set)"
    print(f"[{guild_id}] Song '{current_title}' finished or was stopped.")

    state["current_song"] = None
    state["is_playing"] = False

    if state["queue"]:
        next_song_info = state["queue"].pop(0)
        print(f"[{guild_id}] Playing next from queue: {next_song_info['title']}")
        await play_song_in_vc(guild_id, next_song_info)
    else:
        print(f"[{guild_id}] Queue is empty. Playback ended.")
        if state["keep_alive_active"]:
            print(f"[{guild_id}] Stay mode active, checking for silent audio.")
            await play_silent_audio_if_needed(guild_id)

async def play_silent_audio_if_needed(guild_id):
    state = get_guild_state(guild_id)
    if state["keep_alive_active"] and \
       state["voice_client"] and state["voice_client"].is_connected() and \
       not state["is_playing"] and not state["queue"] and not state["current_song"]:
        print(f"[{guild_id}] Playing conceptual silent audio.")
        state["is_playing_silence"] = True
        state["is_playing"] = True
        # Placeholder - real silent audio stream would have an 'after' callback
        # For now, keep_alive_task will re-evaluate
    # else:
    #    state["is_playing_silence"] = False # ensure it's reset

async def attempt_rejoin(guild_id):
    state = get_guild_state(guild_id)
    if state["last_channel_id"]:
        channel = bot.get_channel(state["last_channel_id"])
        if channel and isinstance(channel, discord.VoiceChannel):
            print(f"[{guild_id}] Attempting to rejoin channel: {channel.name}")
            try:
                if state["voice_client"] and state["voice_client"].is_connected():
                    await state["voice_client"].disconnect(force=True)
                state["voice_client"] = await channel.connect(timeout=10.0, reconnect=True)
                print(f"[{guild_id}] Rejoined {channel.name}. Checking playback status.")
                if state["current_song"]:
                    temp_song = state["current_song"]
                    state["current_song"] = None; state["is_playing"] = False
                    await play_song_in_vc(guild_id, temp_song)
                elif state["queue"]:
                    await song_finished_callback(None, guild_id)
                elif state["keep_alive_active"]:
                    await play_silent_audio_if_needed(guild_id)
            except Exception as e:
                print(f"[{guild_id}] Error during rejoin: {e}")
                state["voice_client"] = None
        else:
            state["keep_alive_active"] = False
    else:
        state["keep_alive_active"] = False

# --- Bot Events ---
@bot.event
async def on_ready():
    print(f'{bot.user.name} has connected to Discord!')
    if not keep_alive_task.is_running():
        keep_alive_task.start()

@tasks.loop(seconds=30)
async def keep_alive_task():
    for guild_id, state in list(music_queues.items()):
        if state.get("keep_alive_active"):
            if state.get("voice_client") and state["voice_client"].is_connected():
                if not state.get("is_playing") and not state.get("queue") and not state.get("current_song"):
                    await play_silent_audio_if_needed(guild_id)
            elif state.get("last_channel_id"): # Disconnected but should be active
                await attempt_rejoin(guild_id)

@keep_alive_task.before_loop
async def before_keep_alive_task():
    await bot.wait_until_ready()

# --- Music Commands (join, stay, leave, play, skip, stop, pause, resume, queue) ---
# These commands will remain largely the same as the previous "full code" version,
# but ensure they use `get_guild_state(ctx.guild.id)` correctly.
# I will paste them for completeness.

@bot.command(name='join', help='Tells the bot to join the voice channel you are in.')
async def join(ctx):
    state = get_guild_state(ctx.guild.id)
    state["last_ctx"] = ctx 
    if not ctx.author.voice:
        await ctx.send(f"{ctx.author.mention} is not connected to a voice channel.")
        return
    channel = ctx.author.voice.channel
    state["last_channel_id"] = channel.id
    if state["voice_client"] and state["voice_client"].is_connected():
        if state["voice_client"].channel == channel: await ctx.send("I'm already in this voice channel!")
        else:
            await state["voice_client"].move_to(channel)
            await ctx.send(f"Moved to **{channel.name}**.")
    else:
        try:
            state["voice_client"] = await channel.connect(timeout=10.0, reconnect=True)
            await ctx.send(f"Joined **{channel.name}**.")
        except Exception as e:
            await ctx.send(f"Could not join **{channel.name}**: {e}")
            if state["voice_client"]:
                try: await state["voice_client"].disconnect(force=True)
                except: pass
            state["voice_client"] = None

@bot.command(name='stay', help='Tells the bot to join and stay in your current VC 24/7.')
async def stay(ctx):
    state = get_guild_state(ctx.guild.id)
    state["last_ctx"] = ctx
    state["keep_alive_active"] = True
    if not ctx.author.voice:
        await ctx.send(f"{ctx.author.mention} is not connected to a voice channel.")
        state["keep_alive_active"] = False
        return
    state["last_channel_id"] = ctx.author.voice.channel.id
    await join(ctx) # Let join handle connection logic
    if state["voice_client"] and state["voice_client"].is_connected():
        await ctx.send("Okay, I will try to stay in this channel.")
        if not state["is_playing"] and not state["queue"] and not state["current_song"]: # Check if something should start
            await play_silent_audio_if_needed(ctx.guild.id)
        elif not state["is_playing"] and (state["queue"] or state["current_song"]):
             await song_finished_callback(None, ctx.guild.id) # Trigger play if queue has items
    else:
        state["keep_alive_active"] = False # Join failed

@bot.command(name='leave', help='To make the bot leave the voice channel.')
async def leave(ctx):
    state = get_guild_state(ctx.guild.id)
    state["last_ctx"] = ctx
    if state["voice_client"] and state["voice_client"].is_connected():
        state["keep_alive_active"] = False
        state["is_playing_silence"] = False
        state["queue"].clear()
        state["current_song"] = None
        state["is_playing"] = False
        if state["voice_client"].is_playing() or state["voice_client"].is_paused():
            state["voice_client"].stop()
        await state["voice_client"].disconnect()
        state["voice_client"] = None
        state["last_channel_id"] = None
        await ctx.send("Left the voice channel.")
    else:
        await ctx.send("I am not in a voice channel.")

@bot.command(name='play', aliases=['p'], help='Plays a song from YouTube (URL or search query)')
async def play(ctx, *, query: str):
    state = get_guild_state(ctx.guild.id)
    state["last_ctx"] = ctx

    if not state["voice_client"] or not state["voice_client"].is_connected():
        if ctx.author.voice:
            await ctx.send("Joining your voice channel first...")
            await join(ctx) # join will set state["voice_client"]
            if not state["voice_client"] or not state["voice_client"].is_connected(): # Check again
                await ctx.send("Could not join your voice channel.")
                return
        else:
            await ctx.send("You're not in a VC, and I'm not in one. Join a VC first.")
            return
    
    if ctx.author.voice and state["voice_client"].channel != ctx.author.voice.channel:
        await ctx.send(f"Moving to your channel: **{ctx.author.voice.channel.name}**.")
        await state["voice_client"].move_to(ctx.author.voice.channel)
        state["last_channel_id"] = ctx.author.voice.channel.id

    async with ctx.typing():
        song_info = await search_youtube(query)
        if song_info is None:
            await ctx.send(f"Could not find or process: `{query}`.")
            return

        state["queue"].append(song_info)
        await ctx.send(f"Added to queue: **{song_info['title']}**")
        print(f"[{ctx.guild.id}] Added to queue: {song_info['title']}. Q len: {len(state['queue'])}. Playing: {state['is_playing']}")

    vc = state["voice_client"]
    if vc and vc.is_connected() and not state["is_playing"]:
        if not vc.is_playing() and not vc.is_paused(): 
            print(f"[{ctx.guild.id}] Play cmd: Not currently playing anything, starting queue via song_finished_callback.")
            await song_finished_callback(None, ctx.guild.id) # This will pop from queue and play
        elif vc.is_paused():
             await ctx.send(f"I'm paused. Use `!resume` or `!skip` for this new song.")
    elif not (vc and vc.is_connected()):
        print(f"[{ctx.guild.id}] Play cmd: VC became disconnected before trying to play.")

@bot.command(name='skip', aliases=['s'], help='Skips the current song.')
async def skip(ctx):
    state = get_guild_state(ctx.guild.id)
    state["last_ctx"] = ctx
    if state["voice_client"] and (state["voice_client"].is_playing() or state["voice_client"].is_paused() or state["current_song"]):
        await ctx.send("Skipping...")
        state["voice_client"].stop() # This triggers song_finished_callback
    else:
        await ctx.send("Not playing anything to skip.")

@bot.command(name='stop', help='Stops the music and clears the queue.')
async def stop(ctx):
    state = get_guild_state(ctx.guild.id)
    state["last_ctx"] = ctx
    if state["voice_client"] and state["voice_client"].is_connected():
        state["queue"].clear()
        state["current_song"] = None
        state["is_playing_silence"] = False
        state["is_playing"] = False
        if state["voice_client"].is_playing() or state["voice_client"].is_paused():
            state["voice_client"].stop()
        await ctx.send("Music stopped and queue cleared.")
    else:
        await ctx.send("Not in a voice channel or not playing anything.")

@bot.command(name='pause', help='Pauses the current song.')
async def pause(ctx):
    state = get_guild_state(ctx.guild.id)
    state["last_ctx"] = ctx
    if state["voice_client"] and state["voice_client"].is_playing(): # Only pause if actively playing
        state["voice_client"].pause()
        state["is_playing"] = False # No longer actively outputting sound
        await ctx.send("Paused music.")
    else:
        await ctx.send("Not playing anything to pause.")

@bot.command(name='resume', help='Resumes the paused song.')
async def resume(ctx):
    state = get_guild_state(ctx.guild.id)
    state["last_ctx"] = ctx
    if state["voice_client"] and state["voice_client"].is_paused():
        state["voice_client"].resume()
        state["is_playing"] = True # Actively outputting sound again
        await ctx.send("Resumed music.")
    elif state["voice_client"] and not state["voice_client"].is_playing() and state["current_song"] and not state["is_playing"]:
        # If we have a current_song but bot thinks it's not playing (e.g. after pause then stop, or an error)
        await ctx.send("Attempting to restart current song...")
        await play_song_in_vc(ctx.guild.id, state["current_song"])
    elif state["voice_client"] and not state["voice_client"].is_playing() and state["queue"] and not state["is_playing"]:
        # If queue has songs but nothing is playing
        await ctx.send("Queue has songs, attempting to play next...")
        await song_finished_callback(None, ctx.guild.id)
    else:
        await ctx.send("Music is not paused or nothing to resume.")

@bot.command(name='queue', aliases=['q'], help='Shows the current music queue.')
async def queue(ctx):
    state = get_guild_state(ctx.guild.id)
    state["last_ctx"] = ctx
    print(f"DEBUG [!q]: Guild ID: {ctx.guild.id}")
    print(f"DEBUG [!q]: Current Song: {state.get('current_song')}")
    print(f"DEBUG [!q]: Queue Contents: {state.get('queue')}")
    print(f"DEBUG [!q]: Is Playing Silence: {state.get('is_playing_silence')}")
    print(f"DEBUG [!q]: Is Playing (general): {state.get('is_playing')}")
    
    embed = discord.Embed(title="Music Queue", color=discord.Color.blue())
    now_playing_value = "Nothing specific is currently playing."
    if state["current_song"]:
        now_playing_value = f"**{state['current_song']['title']}**"
        if state["voice_client"] and state["voice_client"].is_paused():
            now_playing_value += " (Paused)"
    elif state["is_playing_silence"]:
         now_playing_value = "Playing silence to stay connected..."
    embed.add_field(name="💿 Now Playing", value=now_playing_value, inline=False)

    if state["queue"]:
        song_list = ""
        for i, song in enumerate(state["queue"][:10]): song_list += f"{i+1}. {song['title']}\n"
        embed.add_field(name="🎶 Up Next", value=song_list if song_list else "Queue is empty.", inline=False)
        if len(state["queue"]) > 10: embed.set_footer(text=f"...and {len(state['queue']) - 10} more song(s).")
    else:
        embed.add_field(name="🎶 Up Next", value="Queue is empty.", inline=False)
        
    if not state["queue"] and not state["current_song"] and not state["is_playing_silence"]:
        await ctx.send("The queue is empty and nothing is currently playing.")
    else:
        await ctx.send(embed=embed)

# --- Error Handling & Run ---
@bot.event
async def on_command_error(ctx, error):
    if ctx.guild: get_guild_state(ctx.guild.id)["last_ctx"] = ctx # Store context
    if isinstance(error, commands.CommandNotFound): print(f"Cmd not found by {ctx.author}: {ctx.message.content}")
    elif isinstance(error, commands.MissingRequiredArgument): await ctx.send(f"Missing arg: `{error.param.name}` for `!{ctx.command.name}`.")
    elif isinstance(error, commands.CommandInvokeError):
        print(f"Error in cmd '{ctx.command}'. Invoker: {ctx.author}. Msg: '{ctx.message.content}'. Err: {error.original}")
        traceback.print_exception(type(error.original), error.original, error.original.__traceback__)
        await ctx.send(f"Error in `!{ctx.command.name}`. Check logs.")
    elif isinstance(error, commands.CheckFailure): await ctx.send("No permission.")
    else:
        print(f"Unhandled error {type(error)}: {error}")
        traceback.print_exception(type(error), error, error.__traceback__)

if __name__ == "__main__":
    print("DEBUG: Main script start...")
    token_preview = "TOKEN_NOT_SET"
    if TOKEN: token_preview = f"{TOKEN[:5]}...{TOKEN[-5:]}" if len(TOKEN) > 10 else "TOKEN_TOO_SHORT"
    print(f"DEBUG: Token preview: {token_preview}")
    if TOKEN:
        try:
            print("DEBUG: Attempting to run bot with token...")
            bot.run(TOKEN)
        except discord.errors.LoginFailure:
            print("CRITICAL ERROR: Login Failure! Check token & intents.")
        except Exception as e:
            print(f"CRITICAL ERROR during bot.run: {e}")
            traceback.print_exc()
    else:
        print("CRITICAL ERROR: DISCORD_TOKEN not found.")
    print("DEBUG: bot.run() has exited or script finished.")
