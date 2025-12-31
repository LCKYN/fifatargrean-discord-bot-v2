import disnake
from disnake.ext import commands

from core.database import db


class Monitoring(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_member_join(self, member):
        print(f"{member} joined. Resetting data.")
        async with db.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO users (user_id, points, daily_claimed_at)
                VALUES ($1, 0, NULL)
                ON CONFLICT (user_id)
                DO UPDATE SET points = 0, daily_claimed_at = NULL
            """,
                member.id,
            )

    @commands.Cog.listener()
    async def on_member_remove(self, member):
        print(f"{member} left. Resetting data.")
        async with db.pool.acquire() as conn:
            # Reset points
            await conn.execute(
                """
                UPDATE users
                SET points = 0, daily_claimed_at = NULL
                WHERE user_id = $1
            """,
                member.id,
            )
            # Remove any temp roles from database
            await conn.execute(
                "DELETE FROM temp_roles WHERE user_id = $1",
                member.id,
            )


def setup(bot):
    bot.add_cog(Monitoring(bot))
