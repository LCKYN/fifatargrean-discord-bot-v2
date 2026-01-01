import asyncio
import datetime
from typing import Dict, Tuple

import disnake
from disnake.ext import commands, tasks

from core.config import Config
from core.database import db

DEFAULT_AUTOREPLY_COST = 1000
DEFAULT_AUTOREPLY_DURATION = 5  # minutes


class AutoReply(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        # Store active auto-replies: {(channel_id, user_id): (message_text, expires_at, creator_id)}
        # user_id can be None to target all users in channel
        self.active_replies: Dict[
            Tuple[int, int | None], Tuple[str, datetime.datetime, int]
        ] = {}
        self.cleanup_expired.start()

    def cog_unload(self):
        self.cleanup_expired.cancel()

    @tasks.loop(seconds=10)
    async def cleanup_expired(self):
        """Remove expired auto-replies and notify"""
        now = datetime.datetime.now()
        expired = [
            (key, data) for key, data in self.active_replies.items() if now > data[1]
        ]

        for key, (msg_text, expires_at, creator_id) in expired:
            del self.active_replies[key]
            ch_id, user_id = key

            # Send expiration notification to bot channel
            bot_channel = self.bot.get_channel(Config.BOT_CHANNEL_ID)
            if bot_channel:
                channel = self.bot.get_channel(ch_id)
                guild = bot_channel.guild
                target = guild.get_member(user_id) if user_id else None
                creator = guild.get_member(creator_id)

                embed = disnake.Embed(
                    title="⏰ Auto-Reply Expired", color=disnake.Color.orange()
                )
                embed.add_field(
                    name="📍 Channel",
                    value=channel.mention if channel else f"#{ch_id}",
                    inline=True,
                )
                embed.add_field(
                    name="👤 Target",
                    value=target.mention if target else "Unknown",
                    inline=True,
                )
                embed.add_field(
                    name="👤 Creator",
                    value=creator.display_name if creator else "Unknown",
                    inline=True,
                )
                await bot_channel.send(embed=embed)

            print(f"Auto-reply expired for {key}")

    @cleanup_expired.before_loop
    async def before_cleanup(self):
        await self.bot.wait_until_ready()

    @commands.Cog.listener()
    async def on_message(self, message: disnake.Message):
        # Ignore bots
        if message.author.bot:
            return

        # Check for user-specific auto-reply first, then channel-wide
        key = (message.channel.id, message.author.id)
        key_all = (message.channel.id, None)

        active_key = None
        if key in self.active_replies:
            active_key = key
        elif key_all in self.active_replies:
            active_key = key_all

        if not active_key:
            return

        reply_text, expires_at, creator_id = self.active_replies[active_key]

        # Check if expired
        if datetime.datetime.now() > expires_at:
            del self.active_replies[active_key]
            return

        # Send auto-reply
        await message.reply(reply_text, mention_author=True)

    @commands.slash_command(description="Set auto-reply for a user in a channel")
    async def autoreply(
        self,
        inter: disnake.ApplicationCommandInteraction,
        user: disnake.Member = commands.Param(
            description="Target user to auto-reply to"
        ),
        channel: disnake.TextChannel = commands.Param(
            description="Channel to auto-reply in"
        ),
        message: str = commands.Param(
            description="Auto-reply message text", max_length=500
        ),
    ):
        """Set up auto-reply for a user in a channel"""
        mod_role = inter.guild.get_role(Config.MOD_ROLE_ID)
        is_mod = mod_role and mod_role in inter.author.roles
        is_admin = inter.author.guild_permissions.administrator

        # Check max 3 active auto-replies
        if len(self.active_replies) >= 3:
            await inter.response.send_message(
                "Maximum 3 auto-replies can be active at the same time. Please wait for one to expire.",
                ephemeral=True,
            )
            return

        # Check if this user already has an active auto-reply (as creator)
        for (ch_id, target_id), (_, _, creator_id) in self.active_replies.items():
            if creator_id == inter.author.id:
                target_member = inter.guild.get_member(target_id)
                target_channel = self.bot.get_channel(ch_id)
                await inter.response.send_message(
                    f"You already have an active auto-reply targeting {target_member.mention if target_member else 'someone'} in {target_channel.mention if target_channel else 'a channel'}. Wait for it to expire first.",
                    ephemeral=True,
                )
                return

        # Check if target user is already being targeted by another auto-reply
        for (ch_id, target_id), (_, _, creator_id) in self.active_replies.items():
            if target_id == user.id:
                creator_member = inter.guild.get_member(creator_id)
                await inter.response.send_message(
                    f"{user.mention} is already being targeted by another auto-reply. Wait for it to expire first.",
                    ephemeral=True,
                )
                return

        # Get cost and duration from database
        async with db.pool.acquire() as conn:
            cost_row = await conn.fetchval(
                "SELECT value FROM bot_settings WHERE key = 'autoreply_cost'"
            )
            cost = int(cost_row) if cost_row else DEFAULT_AUTOREPLY_COST

            duration_row = await conn.fetchval(
                "SELECT value FROM bot_settings WHERE key = 'autoreply_duration'"
            )
            duration = int(duration_row) if duration_row else DEFAULT_AUTOREPLY_DURATION

            # Mods/admins are free
            if not is_mod and not is_admin:
                user_points = await conn.fetchval(
                    "SELECT points FROM users WHERE user_id = $1", inter.author.id
                )
                user_points = user_points or 0

                if user_points < cost:
                    await inter.response.send_message(
                        f"Not enough {Config.POINT_NAME}. Cost: {cost}, you have: {user_points}",
                        ephemeral=True,
                    )
                    return

                # Deduct cost
                await conn.execute(
                    "UPDATE users SET points = points - $1 WHERE user_id = $2",
                    cost,
                    inter.author.id,
                )

        # Set auto-reply
        expires_at = datetime.datetime.now() + datetime.timedelta(minutes=duration)
        key = (channel.id, user.id)
        self.active_replies[key] = (message, expires_at, inter.author.id)

        embed = disnake.Embed(
            title="🤖 Auto-Reply Activated", color=disnake.Color.green()
        )
        embed.add_field(name="📍 Channel", value=channel.mention, inline=True)
        embed.add_field(name="👤 Target", value=user.mention, inline=True)
        embed.add_field(name="⏱️ Duration", value=f"{duration} minutes", inline=True)
        if not is_mod and not is_admin:
            embed.add_field(
                name="💰 Cost", value=f"{cost} {Config.POINT_NAME}", inline=True
            )
        embed.add_field(
            name="💬 Message",
            value=message[:100] + ("..." if len(message) > 100 else ""),
            inline=False,
        )
        embed.add_field(
            name="📊 Active", value=f"{len(self.active_replies)}/3", inline=True
        )
        embed.set_footer(
            text=f"Set by {inter.author.display_name} • Expires at {expires_at.strftime('%H:%M:%S')}"
        )

        await inter.response.send_message(embed=embed, ephemeral=True)

        # Send notification to bot channel
        bot_channel = self.bot.get_channel(Config.BOT_CHANNEL_ID)
        if bot_channel:
            notify_embed = disnake.Embed(
                title="🤖 New Auto-Reply Started", color=disnake.Color.green()
            )
            notify_embed.add_field(
                name="📍 Channel", value=channel.mention, inline=True
            )
            notify_embed.add_field(name="👤 Target", value=user.mention, inline=True)
            notify_embed.add_field(
                name="⏱️ Duration", value=f"{duration} minutes", inline=True
            )
            notify_embed.add_field(
                name="💬 Message",
                value=message[:100] + ("..." if len(message) > 100 else ""),
                inline=False,
            )
            notify_embed.set_footer(text=f"Created by {inter.author.display_name}")
            await bot_channel.send(embed=notify_embed)

    @commands.slash_command(description="Stop auto-reply for a user")
    async def autoreplystop(
        self,
        inter: disnake.ApplicationCommandInteraction,
        user: disnake.Member = commands.Param(description="Target user"),
        channel: disnake.TextChannel = commands.Param(description="Channel"),
    ):
        """Stop auto-reply for a user in a channel"""
        mod_role = inter.guild.get_role(Config.MOD_ROLE_ID)
        is_mod = mod_role and mod_role in inter.author.roles
        is_admin = inter.author.guild_permissions.administrator

        key = (channel.id, user.id)
        if key not in self.active_replies:
            await inter.response.send_message(
                f"No active auto-reply for {user.mention} in {channel.mention}.",
                ephemeral=True,
            )
            return

        _, _, creator_id = self.active_replies[key]

        # Mods/admins can stop for free
        # Creator can stop for free
        # Anyone else pays 1.5x cost
        stop_cost = 0
        if not is_mod and not is_admin and inter.author.id != creator_id:
            # Get base cost and calculate 1.5x
            async with db.pool.acquire() as conn:
                cost_row = await conn.fetchval(
                    "SELECT value FROM bot_settings WHERE key = 'autoreply_cost'"
                )
                base_cost = int(cost_row) if cost_row else DEFAULT_AUTOREPLY_COST
                stop_cost = int(base_cost * 1.5)

                user_points = await conn.fetchval(
                    "SELECT points FROM users WHERE user_id = $1", inter.author.id
                )
                user_points = user_points or 0

                if user_points < stop_cost:
                    await inter.response.send_message(
                        f"Not enough {Config.POINT_NAME}. Stopping someone else's auto-reply costs {stop_cost} (1.5x). You have: {user_points}",
                        ephemeral=True,
                    )
                    return

                # Deduct cost
                await conn.execute(
                    "UPDATE users SET points = points - $1 WHERE user_id = $2",
                    stop_cost,
                    inter.author.id,
                )

        del self.active_replies[key]

        cost_text = f" (Cost: {stop_cost} {Config.POINT_NAME})" if stop_cost > 0 else ""
        await inter.response.send_message(
            f"✅ Auto-reply stopped for {user.mention} in {channel.mention}.{cost_text}",
            ephemeral=True,
        )

        # Notify bot channel
        bot_channel = self.bot.get_channel(Config.BOT_CHANNEL_ID)
        if bot_channel:
            embed = disnake.Embed(
                title="🛑 Auto-Reply Stopped", color=disnake.Color.orange()
            )
            embed.add_field(name="📍 Channel", value=channel.mention, inline=True)
            embed.add_field(name="👤 Target", value=user.mention, inline=True)
            if stop_cost > 0:
                embed.add_field(
                    name="💰 Cost",
                    value=f"{stop_cost} {Config.POINT_NAME}",
                    inline=True,
                )
            embed.set_footer(text=f"Stopped by {inter.author.display_name}")
            await bot_channel.send(embed=embed)

    @commands.slash_command(description="Show active auto-replies")
    async def autoreplylist(self, inter: disnake.ApplicationCommandInteraction):
        """List all active auto-replies"""
        mod_role = inter.guild.get_role(Config.MOD_ROLE_ID)
        is_mod = mod_role and mod_role in inter.author.roles
        is_admin = inter.author.guild_permissions.administrator

        if not is_mod and not is_admin:
            await inter.response.send_message(
                "You need mod role or administrator permission to use this.",
                ephemeral=True,
            )
            return

        if not self.active_replies:
            await inter.response.send_message("No active auto-replies.", ephemeral=True)
            return

        embed = disnake.Embed(
            title="🤖 Active Auto-Replies", color=disnake.Color.blue()
        )

        now = datetime.datetime.now()
        for (ch_id, user_id), (
            msg_text,
            expires_at,
            creator_id,
        ) in self.active_replies.items():
            channel = self.bot.get_channel(ch_id)
            time_left = expires_at - now
            minutes_left = max(0, int(time_left.total_seconds() // 60))
            seconds_left = max(0, int(time_left.total_seconds() % 60))

            target = "Everyone"
            if user_id:
                member = inter.guild.get_member(user_id)
                target = member.display_name if member else f"User {user_id}"

            embed.add_field(
                name=f"#{channel.name if channel else ch_id}",
                value=f"👤 {target}\n⏱️ {minutes_left}m {seconds_left}s left\n💬 {msg_text[:50]}...",
                inline=False,
            )

        await inter.response.send_message(embed=embed, ephemeral=True)

    @commands.slash_command(description="[MOD] Set the cost for auto-reply")
    async def autoreplycost(
        self,
        inter: disnake.ApplicationCommandInteraction,
        cost: int = commands.Param(description="New cost in points", ge=0),
    ):
        """Set auto-reply cost"""
        mod_role = inter.guild.get_role(Config.MOD_ROLE_ID)
        if not mod_role or mod_role not in inter.author.roles:
            await inter.response.send_message(
                "Only mods can change auto-reply cost.", ephemeral=True
            )
            return

        async with db.pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO bot_settings (key, value) VALUES ('autoreply_cost', $1)
                   ON CONFLICT (key) DO UPDATE SET value = $1""",
                str(cost),
            )

        await inter.response.send_message(
            f"✅ Auto-reply cost set to **{cost} {Config.POINT_NAME}** (free for mods).",
            ephemeral=True,
        )

        # Notify bot channel
        bot_channel = self.bot.get_channel(Config.BOT_CHANNEL_ID)
        if bot_channel:
            embed = disnake.Embed(
                title="⚙️ Auto-Reply Cost Updated",
                description=f"New cost: **{cost} {Config.POINT_NAME}** (free for mods)",
                color=disnake.Color.blurple(),
            )
            embed.set_footer(text=f"Changed by {inter.author.display_name}")
            await bot_channel.send(embed=embed)

    @commands.slash_command(description="[MOD] Set the duration for auto-reply")
    async def autoreplyduration(
        self,
        inter: disnake.ApplicationCommandInteraction,
        minutes: int = commands.Param(description="Duration in minutes", ge=1, le=60),
    ):
        """Set auto-reply duration"""
        mod_role = inter.guild.get_role(Config.MOD_ROLE_ID)
        if not mod_role or mod_role not in inter.author.roles:
            await inter.response.send_message(
                "Only mods can change auto-reply duration.", ephemeral=True
            )
            return

        async with db.pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO bot_settings (key, value) VALUES ('autoreply_duration', $1)
                   ON CONFLICT (key) DO UPDATE SET value = $1""",
                str(minutes),
            )

        await inter.response.send_message(
            f"✅ Auto-reply duration set to **{minutes} minutes**.", ephemeral=True
        )

        # Notify bot channel
        bot_channel = self.bot.get_channel(Config.BOT_CHANNEL_ID)
        if bot_channel:
            embed = disnake.Embed(
                title="⚙️ Auto-Reply Duration Updated",
                description=f"New duration: **{minutes} minutes**",
                color=disnake.Color.blurple(),
            )
            embed.set_footer(text=f"Changed by {inter.author.display_name}")
            await bot_channel.send(embed=embed)


def setup(bot):
    bot.add_cog(AutoReply(bot))
