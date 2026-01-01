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

    # Points System
    BASE_POINTS = 10
    DIMINISHING_FACTOR = 0.8
    COOLDOWN_SECONDS = 15
    PREDICTION_COST = int(os.getenv("PREDICTION_COST", 30))

    # SOOP Notification (disabled by default)
    SOOP_CLIENT_ID = os.getenv("SOOP_CLIENT_ID", "")
    NOTIFICATION_CHANNEL_ID = int(os.getenv("NOTIFICATION_CHANNEL_ID", 0))

    # Role IDs (Replace with actual IDs)
    ROLE_MEMBER = 123456789012345678
    ROLE_VIP = 123456789012345679
