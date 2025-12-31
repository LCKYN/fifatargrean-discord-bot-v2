import asyncpg

from core.config import Config


class Database:
    def __init__(self):
        self.pool = None

    async def connect(self):
        self.pool = await asyncpg.create_pool(
            user=Config.DB_USER,
            password=Config.DB_PASSWORD,
            database=Config.DB_NAME,
            host=Config.DB_HOST,
        )
        await self.create_tables()

    async def create_tables(self):
        async with self.pool.acquire() as conn:
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id BIGINT PRIMARY KEY,
                    points INTEGER DEFAULT 0,
                    total_sent INTEGER DEFAULT 0,
                    total_received INTEGER DEFAULT 0,
                    last_message_at TIMESTAMP,
                    daily_claimed_at TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS temp_roles (
                    user_id BIGINT,
                    role_id BIGINT,
                    expires_at TIMESTAMP,
                    PRIMARY KEY (user_id, role_id)
                );
                CREATE TABLE IF NOT EXISTS shop_roles (
                    role_id BIGINT PRIMARY KEY,
                    price INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS predictions (
                    id SERIAL PRIMARY KEY,
                    title TEXT NOT NULL,
                    creator_id BIGINT NOT NULL,
                    status TEXT DEFAULT 'betting',
                    winning_choice INTEGER,
                    created_at TIMESTAMP DEFAULT NOW(),
                    ends_at TIMESTAMP NOT NULL,
                    message_id BIGINT,
                    channel_id BIGINT
                );
                CREATE TABLE IF NOT EXISTS prediction_choices (
                    prediction_id INTEGER REFERENCES predictions(id) ON DELETE CASCADE,
                    choice_number INTEGER NOT NULL,
                    choice_text TEXT NOT NULL,
                    PRIMARY KEY (prediction_id, choice_number)
                );
                CREATE TABLE IF NOT EXISTS prediction_bets (
                    prediction_id INTEGER REFERENCES predictions(id) ON DELETE CASCADE,
                    user_id BIGINT NOT NULL,
                    choice_number INTEGER NOT NULL,
                    amount INTEGER NOT NULL,
                    PRIMARY KEY (prediction_id, user_id)
                );
                CREATE TABLE IF NOT EXISTS bot_settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
            """)

            # Add new columns if they don't exist (migration for existing databases)
            await conn.execute("""
                ALTER TABLE users ADD COLUMN IF NOT EXISTS total_sent INTEGER DEFAULT 0;
                ALTER TABLE users ADD COLUMN IF NOT EXISTS total_received INTEGER DEFAULT 0;
            """)
            
            # Migration: Add new prediction columns if they don't exist
            await conn.execute("""
                ALTER TABLE predictions ADD COLUMN IF NOT EXISTS creator_id BIGINT;
                ALTER TABLE predictions ADD COLUMN IF NOT EXISTS winning_choice INTEGER;
                ALTER TABLE predictions ADD COLUMN IF NOT EXISTS created_at TIMESTAMP DEFAULT NOW();
                ALTER TABLE predictions ADD COLUMN IF NOT EXISTS ends_at TIMESTAMP;
                ALTER TABLE predictions ADD COLUMN IF NOT EXISTS message_id BIGINT;
                ALTER TABLE predictions ADD COLUMN IF NOT EXISTS channel_id BIGINT;
            """)

            # Sync initial role prices from ENV if table is empty
            count = await conn.fetchval("SELECT COUNT(*) FROM shop_roles")
            if count == 0 and Config.ROLE_PRICES:
                for role_id, price in Config.ROLE_PRICES.items():
                    await conn.execute(
                        "INSERT INTO shop_roles (role_id, price) VALUES ($1, $2) ON CONFLICT DO NOTHING",
                        role_id,
                        price,
                    )

    async def close(self):
        await self.pool.close()


db = Database()
