import os

import disnake
from disnake.ext import commands

from core.config import Config
from core.database import db

intents = disnake.Intents.default()
intents.message_content = True
intents.members = True

# Set guild for instant slash command sync (0 = global, takes up to 1hr)
test_guilds = [Config.GUILD_ID] if Config.GUILD_ID else None

bot = commands.Bot(command_prefix="!", intents=intents, test_guilds=test_guilds)


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")
    print(f"Slash commands synced: {len(bot.slash_commands)}")
    await db.connect()
    print("Database connected")


@bot.event
async def on_close():
    await db.close()
    print("Database connection closed")


# Load Cogs
for filename in os.listdir("./cogs"):
    if filename.endswith(".py") and filename != "__init__.py":
        bot.load_extension(f"cogs.{filename[:-3]}")

if __name__ == "__main__":
    if not Config.DISCORD_TOKEN or Config.DISCORD_TOKEN == "your_token_here":
        print("CRITICAL ERROR: DISCORD_TOKEN is missing or set to default in .env")
        exit(1)

    if "." not in Config.DISCORD_TOKEN:
        print(
            "CRITICAL ERROR: DISCORD_TOKEN looks invalid. Ensure you are using the 'Bot Token' (contains dots), not the 'Client Secret'."
        )
        exit(1)

    bot.run(Config.DISCORD_TOKEN)
