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

    @commands.slash_command(
        name="fakeairdrop", description="Start a fake airdrop event"
    )
    @commands.has_permissions(administrator=True)
    async def fake_airdrop(self, inter: disnake.ApplicationCommandInteraction):
        """Send an airdrop message that users can react to"""
        # Define the role IDs
        FIRST_ROLE_ID = 1458785318541987992
        SECOND_ROLE_ID = 1458791580436664446
        DURATION_MINUTES = 1140

        guild = inter.guild
        first_role = guild.get_role(FIRST_ROLE_ID)
        second_role = guild.get_role(SECOND_ROLE_ID)

        if not first_role or not second_role:
            await inter.response.send_message(
                "❌ Error: Airdrop roles not found on this server.", ephemeral=True
            )
            return

        # Create the airdrop embed
        embed = disnake.Embed(
            title="🤏 AIRDROP!",
            description=(
                "React with 🤏 to claim 250 Greanpoint!\n\n"
                "• First 10 users only!\n"
                "• < 100 Greanpoint: Always 2x!\n"
                "• < 500 Greanpoint: 50% chance for 2x\n"
                "• > 3000 Greanpoint: 50% nothing, 50% half"
            ),
            color=disnake.Color.gold(),
        )
        embed.set_footer(text="10/10 claimed - AIRDROP ENDED")

        await inter.response.send_message(embed=embed)
        message = await inter.original_message()
        await message.add_reaction("🤏")

    @commands.Cog.listener()
    async def on_reaction_add(self, reaction, user):
        """Handle airdrop claims via reactions"""
        if user.bot:
            return

        # Only process 🤏 reactions
        if str(reaction.emoji) != "🤏":
            return

        # Check if this is an airdrop message
        if not reaction.message.embeds:
            return

        embed = reaction.message.embeds[0]
        if not embed.title or "FAKE AIRDROP" not in embed.title:
            return

        # Define the role IDs
        FIRST_ROLE_ID = 1458785318541987992
        SECOND_ROLE_ID = 1458791580436664446
        DURATION_MINUTES = 1140

        guild = reaction.message.guild
        member = guild.get_member(user.id)
        first_role = guild.get_role(FIRST_ROLE_ID)
        second_role = guild.get_role(SECOND_ROLE_ID)

        if not first_role or not second_role or not member:
            return

        # Check what roles the user has
        has_first_role = first_role in member.roles
        has_second_role = second_role in member.roles

        if has_first_role and has_second_role:
            try:
                await user.send("❌ You already have both airdrop roles!")
            except:
                pass
            return

        # Calculate expiration time
        expires_at = datetime.datetime.now() + datetime.timedelta(
            minutes=DURATION_MINUTES
        )

        if not has_first_role:
            # Give the first role
            await member.add_roles(first_role)

            # Add to database
            async with db.pool.acquire() as conn:
                await conn.execute(
                    "INSERT INTO temp_roles (user_id, role_id, expires_at) VALUES ($1, $2, $3) "
                    "ON CONFLICT (user_id, role_id) DO UPDATE SET expires_at = $3",
                    member.id,
                    FIRST_ROLE_ID,
                    expires_at,
                )

            try:
                await user.send(
                    f"🤏 You received the **{first_role.name}** role for {DURATION_MINUTES} minutes!"
                )
            except:
                pass

        elif not has_second_role:
            # Give the second role
            await member.add_roles(second_role)

            # Add to database
            async with db.pool.acquire() as conn:
                await conn.execute(
                    "INSERT INTO temp_roles (user_id, role_id, expires_at) VALUES ($1, $2, $3) "
                    "ON CONFLICT (user_id, role_id) DO UPDATE SET expires_at = $3",
                    member.id,
                    SECOND_ROLE_ID,
                    expires_at,
                )

            try:
                await user.send(
                    f"🤏 You received the **{second_role.name}** role for {DURATION_MINUTES} minutes!"
                )
            except:
                pass


def setup(bot):
    bot.add_cog(Roles(bot))
