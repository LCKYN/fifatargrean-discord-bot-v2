import datetime

import disnake
from disnake.ext import commands

from core.database import db


class Quests(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.command()
    async def daily(self, ctx):
        async with db.pool.acquire() as conn:
            user = await conn.fetchrow(
                "SELECT * FROM users WHERE user_id = $1", ctx.author.id
            )

            now = datetime.datetime.now()

            if user and user["daily_claimed_at"]:
                last_claim = user["daily_claimed_at"]
                if last_claim.date() == now.date():
                    await ctx.send("You have already claimed your daily reward today!")
                    return

            reward = 100
            if not user:
                await conn.execute(
                    "INSERT INTO users (user_id, points, daily_claimed_at) VALUES ($1, $2, $3)",
                    ctx.author.id,
                    reward,
                    now,
                )
            else:
                await conn.execute(
                    "UPDATE users SET points = points + $1, daily_claimed_at = $2 WHERE user_id = $3",
                    reward,
                    now,
                    ctx.author.id,
                )

            await ctx.send(f"You claimed {reward} points!")


def setup(bot):
    bot.add_cog(Quests(bot))
