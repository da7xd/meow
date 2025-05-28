import discord
from discord.ext import commands, tasks
import os
from dotenv import load_dotenv
import yt_dlp
import asyncio
import traceback

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
    'logtostderr': True, # Keep True for now
    'quiet': False,      # Keep True for now
    'no_warnings': True,
    'default_search': 'auto',
    'source_address': '0.0.0.0',
    'cookiefile': 'cookies.txt'
}

FFMPEG_OPTIONS = {
    'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5',
    'options': '-vn',
}

intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.voice_states = True

bot = commands.Bot(command_prefix=PREFIX, intents=intents, help_command=None)
music_queues = {} # guild_id: state_dict

def get_guild_state(guild_id):
    if guild_id not in music_queues:
        music_queues[guild_id] = {
            "queue": [], "voice_client": None, "current_song": None,
            "is_playing": False, # More explicit playing state
            "loop_song": False, "loop_queue": False, # For future expansion
            "keep_alive_active": False, "is_playing_silence": False,
            "last_channel_id": None,
            "last_ctx": None # Store last context for messages like "Now playing"
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
            if 'entries' in info and info['entries']:
                data_to_use = info['entries'][0]
            elif 'url' in info and 'title' in info:
                data_to_use = info
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

async def play_song(guild_id, song_info):
    """Plays a specific song_info object."""
    state = get_guild_state(guild_id)
    if not state["voice_client"] or not state["voice_client"].is_connected():
        print(f"[{guild_id}] play_song: VC not connected. Cannot play.")
        state["is_playing"] = False
        state["current_song"] = None
        return

    state["current_song"] = song_info
    state["is_playing_silence"] = False
    state["is_playing"] = True # Set playing state
    try:
        player = discord.FFmpegPCMAudio(song_info['source'], **FFMPEG_OPTIONS)
        state["voice_client"].play(player, after=lambda e: asyncio.run_coroutine_threadsafe(play_next_or_finish(e, guild_id), bot.loop))
        print(f"[{guild_id}] Now playing: {song_info['title']}")
        if state["last_ctx"]: # Try to send "Now playing" to the channel
            try:
                await state["last_ctx"].send(f"Now playing: **{song_info['title']}**")
            except Exception as send_error:
                print(f"[{guild_id}] Error sending 'Now playing' message: {send_error}")
    except Exception as e:
        print(f"[{guild_id}] Error in play_song for {song_info.get('title', 'unknown song')}: {e}")
        traceback.print_exc()
        state["current_song"] = None
        state["is_playing"] = False
        await play_next_or_finish(e, guild_id) # Pass error to see if it should play next

async def play_next_or_finish(error, guild_id):
    """Called after a song finishes or errors. Plays next or handles end of queue."""
    state = get_guild_state(guild_id)
    if error:
        print(f'[{guild_id}] Player error: {error}')
        if state["last_ctx"]:
            try:
                await state["last_ctx"].send(f"An error occurred with the last song. Skipping.")
            except: pass


    state["current_song"] = None # Current song finished or errored
    state["is_playing"] = False  # No longer actively playing this song

    if state["queue"]:
        print(f"[{guild_id}] Queue has items, playing next.")
        next_song_info = state["queue"].pop(0)
        await play_song(guild_id, next_song_info)
    else: # Queue is empty
        print(f"[{guild_id}] Queue is empty. Playback finished for now.")
        state["current_song"] = None # Ensure current_song is cleared
        state["is_playing"] = False
        if state["keep_alive_active"]:
            print(f"[{guild_id}] Keep alive active, will check for silent audio.")
            await play_silent_audio_if_needed(guild_id)
        # elif state["voice_client"] and state["voice_client"].is_connected():
            # Optional: Auto-leave if not in stay mode and queue ends
            # print(f"[{guild_id}] Queue ended, not in stay mode. Leaving channel.")
            # await state["voice_client"].disconnect()
            # state["voice_client"] = None


async def play_silent_audio_if_needed(guild_id):
    state = get_guild_state(guild_id)
    if state["keep_alive_active"] and \
       state["voice_client"] and state["voice_client"].is_connected() and \
       not state["is_playing"] and not state["queue"] and not state["current_song"]: # Explicitly check if not already playing anything
        
        print(f"[{guild_id}] Attempting to play conceptual silent audio to keep alive.")
        state["is_playing_silence"] = True
        state["is_playing"] = True # conceptually "playing" silence
        try:
            # This is still a placeholder for a true silent FFmpeg stream.
            # A real silent stream would call play_next_or_finish via its 'after' callback.
            # For now, we simulate a short duration and then re-evaluate.
            await asyncio.sleep(10) # Simulate silence for 10s
            if state["is_playing_silence"]: # Check if still in this specific silent state
                print(f"[{guild_id}] Conceptual silent audio period ended.")
                state["is_playing_silence"] = False
                state["is_playing"] = False
                # This will allow keep_alive_task to re-evaluate if needed
        except Exception as e:
            print(f"[{guild_id}] Error during conceptual silent audio: {e}")
            state["is_playing_silence"] = False
            state["is_playing"] = False


async def attempt_rejoin(guild_id):
    state = get_guild_state(guild_id)
    if state["last_channel_id"]:
        channel = bot.get_channel(state["last_channel_id"])
        if channel and isinstance(channel, discord.VoiceChannel):
            print(f"[{guild_id}] Attempting to rejoin channel: {channel.name}")
            try:
                if state["voice_client"] and state["voice_client"].is_connected(): # Defensive check
                    await state["voice_client"].disconnect(force=True) # Disconnect cleanly first
                state["voice_client"] = await channel.connect(timeout=10.0, reconnect=True)
                print(f"[{guild_id}] Rejoined {channel.name}. Checking queue...")
                # If there was a queue or current song, try to resume
                if state["current_song"]: # If a song was "playing" when disconnected
                    temp_song = state["current_song"]
                    state["current_song"] = None # Clear to allow play_song to set it
                    await play_song(guild_id, temp_song)
                elif state["queue"]:
                    await play_next_or_finish(None, guild_id) # Try playing next from queue
                elif state["keep_alive_active"]:
                    await play_silent_audio_if_needed(guild_id)

            except asyncio.TimeoutError:
                print(f"[{guild_id}] Timeout trying to rejoin {channel.name}.")
                state["voice_client"] = None
            except Exception as e:
                print(f"[{guild_id}] Error during rejoin attempt to {channel.name}: {e}")
                state["voice_client"] = None
        else:
            print(f"[{guild_id}] Could not find or invalid last channel ID for rejoin: {state['last_channel_id']}")
            state["keep_alive_active"] = False # Can't rejoin, disable stay
    else:
        print(f"[{guild_id}] No last channel ID to rejoin.")


# --- Bot Events ---
@bot.event
async def on_ready():
    print(f'{bot.user.name} has connected to Discord!')
    print(f'Bot ID: {bot.user.id}')
    if not keep_alive_task.is_running():
        keep_alive_task.start()

@tasks.loop(seconds=30) # Reduced interval for quicker checks
async def keep_alive_task():
    # print("DEBUG: Keep_alive_task running...")
    for guild_id, state in list(music_queues.items()): # Iterate over a copy
        if state.get("keep_alive_active"): # Check if guild has "stay" mode enabled
            if state.get("voice_client") and state["voice_client"].is_connected():
                if not state.get("is_playing") and not state.get("queue") and not state.get("current_song"):
                    # print(f"Keep-alive: Guild {guild_id} - active, connected, not playing, queue empty.")
                    await play_silent_audio_if_needed(guild_id)
            elif state.get("last_channel_id"): # If keep_alive but disconnected
                print(f"Keep-alive task: Bot for guild {guild_id} disconnected but should be active. Attempting rejoin.")
                await attempt_rejoin(guild_id)

@keep_alive_task.before_loop
async def before_keep_alive_task():
    await bot.wait_until_ready()


# --- Music Commands ---
@bot.command(name='join', help='Tells the bot to join the voice channel you are in.')
async def join(ctx):
    state = get_guild_state(ctx.guild.id)
    state["last_ctx"] = ctx # Store context

    if not ctx.author.voice:
        await ctx.send(f"{ctx.author.mention} is not connected to a voice channel.")
        return

    channel = ctx.author.voice.channel
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
        except Exception as e:
            await ctx.send(f"Could not join voice channel **{channel.name}**: {e}")
            if state["voice_client"]:
                try: await state["voice_client"].disconnect(force=True)
                except: pass
            state["voice_client"] = None


@bot.command(name='stay', help='Tells the bot to join and stay in your current VC 24/7.')
async def stay(ctx):
    state = get_guild_state(ctx.guild.id)
    state["last_ctx"] = ctx
    state["keep_alive_active"] = True # Set keep_alive before join attempt

    if not ctx.author.voice:
        await ctx.send(f"{ctx.author.mention} is not connected to a voice channel.")
        state["keep_alive_active"] = False # Revert if user not in VC
        return
        
    state["last_channel_id"] = ctx.author.voice.channel.id
    await join(ctx) # Let join handle connection logic

    if state["voice_client"] and state["voice_client"].is_connected():
        await ctx.send("Okay, I will try to stay in this channel.")
        # Check if something should play immediately (e.g., if queue wasn't empty or for silence)
        if not state["is_playing"] and not state["queue"] and not state["current_song"]:
            await play_silent_audio_if_needed(ctx.guild.id)
        elif not state["is_playing"] and (state["queue"] or state["current_song"]): # If something to play but not playing
             await play_next_or_finish(None, ctx.guild.id)
    else:
        await ctx.send("Could not join to stay. Please ensure I have permissions.")
        state["keep_alive_active"] = False # Revert if join didn't establish VC

@bot.command(name='leave', help='To make the bot leave the voice channel.')
async def leave(ctx):
    state = get_guild_state(ctx.guild.id)
    state["last_ctx"] = ctx

    if state["voice_client"] and state["voice_client"].is_connected():
        state["keep_alive_active"] = False # Crucial: turn off stay mode
        state["is_playing_silence"] = False
        state["queue"].clear()
        state["current_song"] = None
        state["is_playing"] = False
        if state["voice_client"].is_playing() or state["voice_client"].is_paused(): # check both
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
    state["last_ctx"] = ctx # Update last context

    if not state["voice_client"] or not state["voice_client"].is_connected():
        if ctx.author.voice:
            await ctx.send("Joining your voice channel first...")
            await join(ctx) # join will set state["voice_client"]
            if not state["voice_client"] or not state["voice_client"].is_connected():
                await ctx.send("Could not join your voice channel. Please use `!join` or `!stay` first.")
                return
        else:
            await ctx.send("You are not in a voice channel, and I'm not in one either.")
            return
    
    if ctx.author.voice and state["voice_client"].channel != ctx.author.voice.channel:
        await ctx.send(f"Moving to your channel: **{ctx.author.voice.channel.name}** to play.")
        await state["voice_client"].move_to(ctx.author.voice.channel)
        state["last_channel_id"] = ctx.author.voice.channel.id

    async with ctx.typing():
        song_info = await search_youtube(query)
        if song_info is None:
            await ctx.send(f"Could not find or process the song: `{query}`. Check logs for details.")
            return

        state["queue"].append(song_info)
        await ctx.send(f"Added to queue: **{song_info['title']}**")
        print(f"[{ctx.guild.id}] Added to queue: {song_info['title']}. Current queue length: {len(state['queue'])}")


    # If not already playing anything (including silence), and not just paused
    if state["voice_client"] and state["voice_client"].is_connected() and \
       not state["is_playing"] and not state["voice_client"].is_paused(): # Check if paused too
        print(f"[{ctx.guild.id}] Play command: Not currently playing, attempting to start play_next_or_finish.")
        await play_next_or_finish(None, ctx.guild.id) # Start playing if not already
    elif state["voice_client"] and state["voice_client"].is_paused():
        await ctx.send(f"I'm currently paused. Use `!resume` to continue or `!skip` for the next song.")


@bot.command(name='skip', aliases=['s'], help='Skips the current song.')
async def skip(ctx):
    state = get_guild_state(ctx.guild.id)
    state["last_ctx"] = ctx

    if state["voice_client"] and (state["voice_client"].is_playing() or state["voice_client"].is_paused() or state["current_song"]):
        await ctx.send("Skipping...")
        state["voice_client"].stop() # This triggers 'after' which calls play_next_or_finish
        # play_next_or_finish will handle clearing current_song and playing next if queue has items
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
        # If in stay mode, it might try to play silence after this.
        # Consider if stop should also turn off stay mode or if !leave is for that.
        # For now, stay mode remains, and keep_alive_task might restart silent audio.
    else:
        await ctx.send("Not in a voice channel or not playing anything.")


@bot.command(name='pause', help='Pauses the current song.')
async def pause(ctx):
    state = get_guild_state(ctx.guild.id)
    state["last_ctx"] = ctx
    if state["voice_client"] and state["voice_client"].is_playing():
        state["voice_client"].pause()
        state["is_playing"] = False # Mark as not actively playing
        await ctx.send("Paused music.")
    else:
        await ctx.send("Not playing anything to pause or already paused.")

@bot.command(name='resume', help='Resumes the paused song.')
async def resume(ctx):
    state = get_guild_state(ctx.guild.id)
    state["last_ctx"] = ctx
    if state["voice_client"] and state["voice_client"].is_paused():
        state["voice_client"].resume()
        state["is_playing"] = True # Mark as actively playing again
        await ctx.send("Resumed music.")
    elif state["voice_client"] and not state["voice_client"].is_playing() and state["current_song"]:
        # If it was stopped for some reason but current_song still exists, try to replay it
        await ctx.send("Attempting to resume/restart current song...")
        await play_song(ctx.guild.id, state["current_song"])
    else:
        await ctx.send("Music is not paused or nothing to resume.")


@bot.command(name='queue', aliases=['q'], help='Shows the current music queue.')
async def queue(ctx):
    state = get_guild_state(ctx.guild.id)
    state["last_ctx"] = ctx
    
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
        for i, song in enumerate(state["queue"][:10]):
            song_list += f"{i+1}. {song['title']}\n"
        embed.add_field(name="🎶 Up Next", value=song_list if song_list else "Queue is empty.", inline=False)
        if len(state["queue"]) > 10:
            embed.set_footer(text=f"...and {len(state['queue']) - 10} more song(s).")
    else:
        embed.add_field(name="🎶 Up Next", value="Queue is empty.", inline=False)
        
    if not state["queue"] and not state["current_song"] and not state["is_playing_silence"]: # Truly empty
        await ctx.send("The queue is empty and nothing is currently playing.")
    else:
        await ctx.send(embed=embed)


# --- Basic Error Handling ---
@bot.event
async def on_command_error(ctx, error):
    # Store context in state if available, for error reporting
    if ctx.guild:
        state = get_guild_state(ctx.guild.id)
        state["last_ctx"] = ctx

    if isinstance(error, commands.CommandNotFound):
        print(f"Command not found by {ctx.author}: {ctx.message.content}")
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


# --- Run the Bot ---
if __name__ == "__main__":
    print("DEBUG: Main script execution started...")
    token_preview = "TOKEN_NOT_SET"
    if TOKEN:
        token_preview = f"{TOKEN[:5]}...{TOKEN[-5:]}" if len(TOKEN) > 10 else "TOKEN_TOO_SHORT"
    print(f"DEBUG: Token from env preview: {token_preview}")

    if TOKEN:
        try:
            print("DEBUG: Attempting to run bot with token...")
            bot.run(TOKEN)
        except discord.errors.LoginFailure:
            print("CRITICAL ERROR: Login Failure! Your DISCORD_TOKEN is incorrect or invalid...")
        except Exception as e:
            print(f"CRITICAL ERROR during bot.run (outer try-except): {e}")
            traceback.print_exc()
    else:
        print("CRITICAL ERROR: DISCORD_TOKEN not found in environment variables. Bot cannot start.")

    print("DEBUG: bot.run() has exited or script finished.")
