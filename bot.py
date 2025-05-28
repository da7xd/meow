print("DEBUG: Script starting...") # <-- ADD THIS

import discord
from discord.ext import commands
import os
from dotenv import load_dotenv

print("DEBUG: Imports successful.") # <-- ADD THIS

# Load the secret token from a file called .env
load_dotenv()
print("DEBUG: load_dotenv() called.") # <-- ADD THIS
TOKEN = os.getenv('DISCORD_TOKEN')
print(f"DEBUG: Token is: {TOKEN}") # <-- ADD THIS (This will show if the token loaded)

# This tells Discord what our bot is allowed to "see" or "do"
intents = discord.Intents.default()
intents.message_content = True # We turned this on in the Discord Developer Portal!
print("DEBUG: Intents set.") # <-- ADD THIS

# This creates our bot. The "!" means commands will start with ! (e.g., !hello)
bot = commands.Bot(command_prefix="!", intents=intents)
print("DEBUG: Bot object created.") # <-- ADD THIS

# This event happens once the bot successfully connects to Discord
@bot.event
async def on_ready():
    print("DEBUG: on_ready event triggered!") # <-- ADD THIS
    print(f"Yay! {bot.user.name} is online and ready!")
    print(f"My ID is: {bot.user.id}")

# This is our first command!
@bot.command(name='ping')
async def ping_command(ctx):
    await ctx.send("Pong!")

# Another command!
@bot.command(name='hello')
async def hello_command(ctx):
    await ctx.send(f"Hello there, {ctx.author.mention}!")

print("DEBUG: About to check TOKEN and run bot.") # <-- ADD THIS
# This is how we actually start the bot using its secret token
if TOKEN:
    print("DEBUG: TOKEN exists, trying to run bot...") # <-- ADD THIS
    try:
        bot.run(TOKEN)
    except discord.errors.LoginFailure:
        print("CRITICAL ERROR: Login Failure! Your DISCORD_TOKEN is incorrect or invalid. Double-check it in your .env file and on the Discord Developer Portal. Also, make sure 'MESSAGE CONTENT INTENT' is enabled on the portal.")
    except Exception as e:
        print(f"CRITICAL ERROR: An unexpected error occurred when trying to run the bot: {e}")
else:
    print("CRITICAL ERROR: DISCORD_TOKEN not found in .env file or environment! Make sure your .env file is in the same folder as bot.py and contains 'DISCORD_TOKEN=YOUR_ACTUAL_TOKEN'.")

print("DEBUG: Script has finished or bot.run() is blocking (which is normal if it connected).") # <-- This line might not always print if bot.run() takes over.