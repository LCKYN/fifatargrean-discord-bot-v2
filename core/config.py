import os

from dotenv import load_dotenv

# Load secrets first, then config (config can override)
load_dotenv(".env.secret")
load_dotenv(".env")


class Config:
    DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
    DB_USER = os.getenv("DB_USER")
    DB_PASSWORD = os.getenv("DB_PASSWORD")
    DB_NAME = os.getenv("DB_NAME")
    DB_HOST = os.getenv("DB_HOST", "localhost")
    BOT_CHANNEL_ID = int(os.getenv("BOT_CHANNEL_ID", 0))
    GUILD_ID = int(os.getenv("GUILD_ID", 0))
    POINT_NAME = os.getenv("POINT_NAME", "point")
    MOD_ROLE_ID = int(os.getenv("MOD_ROLE_ID", 0))
    ROLE_DURATION_MINUTES = int(
        os.getenv("ROLE_DURATION_MINUTES", 1440)
    )  # Default 24 hours

    # Parse role prices: {role_id: price}
    ROLE_PRICES = {}
    for item in os.getenv("ROLE_PRICES", "").split(","):
        if ":" in item:
            role_id, price = item.split(":")
            ROLE_PRICES[int(role_id)] = int(price)

    # Points System
    BASE_POINTS = 10
    DIMINISHING_FACTOR = 0.8
    COOLDOWN_SECONDS = 15
    PREDICTION_COST = int(os.getenv("PREDICTION_COST", 100))

    # Role IDs (Replace with actual IDs)
    ROLE_MEMBER = 123456789012345678
    ROLE_VIP = 123456789012345679
