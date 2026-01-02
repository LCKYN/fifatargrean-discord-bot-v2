import asyncio
import datetime

import disnake
from disnake.ext import commands, tasks

from core.config import Config
from core.database import db


class Roles(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.check_temp_roles.start()

    def cog_unload(self):
        self.check_temp_roles.cancel()

    @tasks.loop(seconds=30)  # Check every 30 seconds for testing
    async def check_temp_roles(self):
        if db.pool is None:
            return

        async with db.pool.acquire() as conn:
            now = datetime.datetime.now()
            expired = await conn.fetch(
                "SELECT * FROM temp_roles WHERE expires_at < $1", now
            )

            for record in expired:
                guild = self.bot.guilds[0]
                member = guild.get_member(record["user_id"])
                role = guild.get_role(record["role_id"])

                # Always delete the record from database
                await conn.execute(
                    "DELETE FROM temp_roles WHERE user_id = $1 AND role_id = $2",
                    record["user_id"],
                    record["role_id"],
                )

                if member and role:
                    # Only remove and notify if user still has the role
                    if role in member.roles:
                        await member.remove_roles(role)
                        print(f"Removed expired role {role.name} from {member.name}")

                        # Notify in bot channel
                        channel = self.bot.get_channel(Config.BOT_CHANNEL_ID)
                        if channel:
                            embed = disnake.Embed(
                                title="⏰ Role Expired",
                                description=f"The **{role.name}** role has expired for {member.mention}",
                                color=disnake.Color.orange(),
                            )
                            await channel.send(embed=embed)
                    else:
                        print(
                            f"Role {role.name} already removed from {member.name} (possibly by mod)"
                        )
                else:
                    print(
                        f"Cleaned up expired role record: user={record['user_id']}, role={record['role_id']}"
                    )

    @check_temp_roles.before_loop
    async def before_check(self):
        await self.bot.wait_until_ready()
        while db.pool is None:
            await asyncio.sleep(1)


def setup(bot):
    bot.add_cog(Roles(bot))
