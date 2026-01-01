import datetime
import random

import disnake
from disnake.ext import commands

from core.config import Config
from core.database import db


class Points(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_message(self, message):
        if message.author.bot:
            return

        async with db.pool.acquire() as conn:
            user = await conn.fetchrow(
                "SELECT * FROM users WHERE user_id = $1", message.author.id
            )
            now = datetime.datetime.now()

            points_to_add = 1

            if not user:
                # First time user ever
                points_to_add = 10
                await conn.execute(
                    "INSERT INTO users (user_id, points, last_message_at) VALUES ($1, $2, $3)",
                    message.author.id,
                    points_to_add,
                    now,
                )
            else:
                last_msg = user["last_message_at"]

                # Check if first message of the day
                if last_msg is None or last_msg.date() < now.date():
                    points_to_add = 10
                else:
                    # Regular message, check cooldown
                    if (now - last_msg).total_seconds() < Config.COOLDOWN_SECONDS:
                        return
                    points_to_add = 1

                await conn.execute(
                    "UPDATE users SET points = points + $1, last_message_at = $2 WHERE user_id = $3",
                    points_to_add,
                    now,
                    message.author.id,
                )

            # Check if user has server booster role for 2x points
            if message.guild:
                booster_role = message.guild.get_role(939954575216107540)
                if booster_role and booster_role in message.author.roles:
                    # Give bonus points (same amount again for 2x total)
                    await conn.execute(
                        "UPDATE users SET points = points + $1 WHERE user_id = $2",
                        points_to_add,
                        message.author.id,
                    )

    @commands.slash_command(description="Check your current points")
    async def point(self, inter: disnake.ApplicationCommandInteraction):
        async with db.pool.acquire() as conn:
            points = await conn.fetchval(
                "SELECT points FROM users WHERE user_id = $1", inter.author.id
            )
            points = points if points else 0
            await inter.response.send_message(
                f"You have {points} {Config.POINT_NAME}.", ephemeral=True
            )

    @commands.slash_command(description="Check points for a specific user")
    async def checkpoints(
        self,
        inter: disnake.ApplicationCommandInteraction,
        user: disnake.User = commands.Param(description="User to check points for"),
    ):
        async with db.pool.acquire() as conn:
            points = await conn.fetchval(
                "SELECT points FROM users WHERE user_id = $1", user.id
            )
            points = points if points else 0
            await inter.response.send_message(
                f"{user.mention} has {points} {Config.POINT_NAME}.", ephemeral=True
            )

    @commands.slash_command(description="Attack another user to steal points")
    async def attack(
        self,
        inter: disnake.ApplicationCommandInteraction,
        target: disnake.User = commands.Param(description="User to attack"),
    ):
        # Can't attack yourself
        if target.id == inter.author.id:
            await inter.response.send_message(
                "You cannot attack yourself.", ephemeral=True
            )
            return

        # Can't attack bots
        if target.bot:
            await inter.response.send_message("You cannot attack bots.", ephemeral=True)
            return

        async with db.pool.acquire() as conn:
            # Get both users' points
            attacker_points = await conn.fetchval(
                "SELECT points FROM users WHERE user_id = $1", inter.author.id
            )
            target_points = await conn.fetchval(
                "SELECT points FROM users WHERE user_id = $1", target.id
            )

            attacker_points = attacker_points or 0
            target_points = target_points or 0

            # Check both have at least 10 points
            if attacker_points < 10:
                await inter.response.send_message(
                    f"You need at least 10 {Config.POINT_NAME} to attack.",
                    ephemeral=True,
                )
                return

            if target_points < 10:
                await inter.response.send_message(
                    f"{target.mention} doesn't have enough {Config.POINT_NAME} to attack.",
                    ephemeral=True,
                )
                return

            # 45% success, 55% fail
            success = random.random() < 0.45

            if success:
                # Attacker steals 10 points from target
                await conn.execute(
                    "UPDATE users SET points = points + 10 WHERE user_id = $1",
                    inter.author.id,
                )
                await conn.execute(
                    "UPDATE users SET points = points - 10 WHERE user_id = $1",
                    target.id,
                )
                await inter.response.send_message(
                    f"💥 **Attack successful!** You stole 10 {Config.POINT_NAME} from {target.mention}!"
                )
            else:
                # Attacker loses 10 points to target
                await conn.execute(
                    "UPDATE users SET points = points - 10 WHERE user_id = $1",
                    inter.author.id,
                )
                await conn.execute(
                    "UPDATE users SET points = points + 10 WHERE user_id = $1",
                    target.id,
                )
                await inter.response.send_message(
                    f"💔 **Attack failed!** You lost 10 {Config.POINT_NAME} to {target.mention}!"
                )

    async def get_shop_roles(self):
        """Get role prices from database"""
        async with db.pool.acquire() as conn:
            rows = await conn.fetch("SELECT role_id, price FROM shop_roles")
            return {row["role_id"]: row["price"] for row in rows}

    @commands.slash_command(description="Show all available roles and prices")
    async def shop(self, inter: disnake.ApplicationCommandInteraction):
        channel = inter.channel

        # Build role list from database
        role_prices = await self.get_shop_roles()
        role_items = []
        for role_id, price in role_prices.items():
            role = inter.guild.get_role(role_id)
            if role:
                role_items.append(f"• **{role.name}** - `{price} {Config.POINT_NAME}`")
            else:
                role_items.append(
                    f"• Role ID {role_id} (not found) - `{price} {Config.POINT_NAME}`"
                )

        if not role_items:
            await inter.response.send_message(
                "No roles available for purchase.", ephemeral=True
            )
            return

        # Split into chunks of 10 roles per embed
        chunk_size = 10
        chunks = [
            role_items[i : i + chunk_size]
            for i in range(0, len(role_items), chunk_size)
        ]

        await inter.response.defer()

        for idx, chunk in enumerate(chunks):
            embed = disnake.Embed(
                title=f"🛒 Role Shop"
                + (f" (Page {idx + 1}/{len(chunks)})" if len(chunks) > 1 else ""),
                description="\n".join(chunk),
                color=disnake.Color.blue(),
            )
            embed.set_footer(
                text=f"Use /buyrole @role to purchase • Duration: {Config.ROLE_DURATION_MINUTES} minute(s)"
            )
            await inter.followup.send(embed=embed)

    @commands.slash_command(description="Show top 10 leaderboard")
    async def leaderboard(self, inter: disnake.ApplicationCommandInteraction):
        async with db.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT user_id, points FROM users ORDER BY points DESC"
            )

            embed = disnake.Embed(
                title="🏆 Top 10 Leaderboard", color=disnake.Color.gold()
            )
            description = ""

            # Get mod role
            mod_role = inter.guild.get_role(Config.MOD_ROLE_ID)

            # Filter out mods and build leaderboard
            count = 0
            for row in rows:
                if count >= 10:
                    break

                user_id = row["user_id"]
                points = row["points"]

                # Get member object
                member = inter.guild.get_member(user_id)

                # Skip if user has mod role
                if member and mod_role and mod_role in member.roles:
                    continue

                # Add to leaderboard
                name = member.display_name if member else f"User {user_id}"
                count += 1
                description += f"**{count}.** {name} - `{points} {Config.POINT_NAME}`\n"

            embed.description = description or "No data yet."

            await inter.response.send_message(embed=embed, ephemeral=True)

    @commands.slash_command(description="Show top 10 senders and receivers")
    async def transfers(self, inter: disnake.ApplicationCommandInteraction):
        async with db.pool.acquire() as conn:
            # Top 10 senders
            senders = await conn.fetch(
                "SELECT user_id, total_sent FROM users WHERE total_sent > 0 ORDER BY total_sent DESC LIMIT 10"
            )
            # Top 10 receivers
            receivers = await conn.fetch(
                "SELECT user_id, total_received FROM users WHERE total_received > 0 ORDER BY total_received DESC LIMIT 10"
            )

        # Build senders list
        sender_lines = []
        for idx, row in enumerate(senders, 1):
            member = inter.guild.get_member(row["user_id"])
            name = member.display_name if member else f"User {row['user_id']}"
            sender_lines.append(
                f"**{idx}.** {name} - `{row['total_sent']} {Config.POINT_NAME}`"
            )

        # Build receivers list
        receiver_lines = []
        for idx, row in enumerate(receivers, 1):
            member = inter.guild.get_member(row["user_id"])
            name = member.display_name if member else f"User {row['user_id']}"
            receiver_lines.append(
                f"**{idx}.** {name} - `{row['total_received']} {Config.POINT_NAME}`"
            )

        embed = disnake.Embed(
            title=f"💸 Transfer Leaderboard", color=disnake.Color.green()
        )
        embed.add_field(
            name="📤 Top Senders",
            value="\n".join(sender_lines) if sender_lines else "No data yet.",
            inline=True,
        )
        embed.add_field(
            name="📥 Top Receivers",
            value="\n".join(receiver_lines) if receiver_lines else "No data yet.",
            inline=True,
        )

        await inter.response.send_message(embed=embed, ephemeral=True)

    @commands.slash_command(description="[MOD] Add points to a user")
    async def addpoint(
        self,
        inter: disnake.ApplicationCommandInteraction,
        user: disnake.Member,
        amount: int,
    ):
        # Check if user has mod role
        mod_role = inter.guild.get_role(Config.MOD_ROLE_ID)
        if not mod_role or mod_role not in inter.author.roles:
            await inter.response.send_message(
                "You don't have permission to use this command.", ephemeral=True
            )
            return

        if amount <= 0:
            await inter.response.send_message(
                "Amount must be positive.", ephemeral=True
            )
            return

        async with db.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO users (user_id, points) VALUES ($1, $2)
                ON CONFLICT (user_id) DO UPDATE SET points = users.points + $2
            """,
                user.id,
                amount,
            )

        channel = self.bot.get_channel(Config.BOT_CHANNEL_ID)
        if channel:
            embed = disnake.Embed(
                title="💰 Points Added",
                description=f"{inter.author.mention} added **{amount} {Config.POINT_NAME}** to {user.mention}",
                color=disnake.Color.green(),
            )
            await channel.send(embed=embed)

        await inter.response.send_message(
            f"Added {amount} {Config.POINT_NAME} to {user.display_name}", ephemeral=True
        )

    @commands.slash_command(description="Send points to another user (10% tax)")
    async def sendpoint(
        self,
        inter: disnake.ApplicationCommandInteraction,
        user: disnake.Member,
        amount: int,
        reason: str,
    ):
        if user.id == inter.author.id:
            await inter.response.send_message(
                "You cannot send points to yourself.", ephemeral=True
            )
            return

        if amount <= 0:
            await inter.response.send_message(
                "Amount must be positive.", ephemeral=True
            )
            return

        # Calculate received (90% of amount, floored), tax is the remainder
        received = int(amount * 0.90)
        tax = amount - received

        async with db.pool.acquire() as conn:
            # Check sender has enough points
            sender_points = await conn.fetchval(
                "SELECT points FROM users WHERE user_id = $1", inter.author.id
            )
            sender_points = sender_points or 0

            if sender_points < amount:
                await inter.response.send_message(
                    f"Not enough {Config.POINT_NAME}. You have {sender_points}, need {amount}.",
                    ephemeral=True,
                )
                return

            # Deduct from sender and track sent amount
            await conn.execute(
                "UPDATE users SET points = points - $1, total_sent = total_sent + $1 WHERE user_id = $2",
                amount,
                inter.author.id,
            )

            # Add to receiver and track received amount
            await conn.execute(
                """
                INSERT INTO users (user_id, points, total_received) VALUES ($1, $2, $2)
                ON CONFLICT (user_id) DO UPDATE SET points = users.points + $2, total_received = users.total_received + $2
            """,
                user.id,
                received,
            )

        channel = self.bot.get_channel(Config.BOT_CHANNEL_ID)
        if channel:
            embed = disnake.Embed(
                title="💸 Points Sent!",
                description=f"{inter.author.mention} sent **{amount} {Config.POINT_NAME}** to {user.mention}\n"
                f"📥 Received: **{received} {Config.POINT_NAME}** (10% tax: {tax})\n"
                f"📝 Reason: {reason}",
                color=disnake.Color.gold(),
            )
            await channel.send(embed=embed)

        await inter.response.send_message(
            f"Sent {amount} {Config.POINT_NAME} to {user.display_name} (they received {received} after 10% tax)",
            ephemeral=True,
        )

    @commands.slash_command(description="[MOD] Add a role to the shop")
    async def shopadd(
        self,
        inter: disnake.ApplicationCommandInteraction,
        role: disnake.Role,
        price: int,
    ):
        mod_role = inter.guild.get_role(Config.MOD_ROLE_ID)
        if not mod_role or mod_role not in inter.author.roles:
            await inter.response.send_message(
                "You don't have permission to use this command.", ephemeral=True
            )
            return

        if price <= 0:
            await inter.response.send_message("Price must be positive.", ephemeral=True)
            return

        async with db.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO shop_roles (role_id, price) VALUES ($1, $2)
                ON CONFLICT (role_id) DO UPDATE SET price = $2
            """,
                role.id,
                price,
            )

        channel = self.bot.get_channel(Config.BOT_CHANNEL_ID)
        if channel:
            embed = disnake.Embed(
                title="🛒 Role Added to Shop",
                description=f"{inter.author.mention} added **{role.name}** to the shop for **{price} {Config.POINT_NAME}**",
                color=disnake.Color.green(),
            )
            await channel.send(embed=embed)

        await inter.response.send_message(
            f"Added **{role.name}** to shop with price {price} {Config.POINT_NAME}",
            ephemeral=True,
        )

    @commands.slash_command(description="[MOD] Remove a role from the shop")
    async def shopremove(
        self, inter: disnake.ApplicationCommandInteraction, role: disnake.Role
    ):
        mod_role = inter.guild.get_role(Config.MOD_ROLE_ID)
        if not mod_role or mod_role not in inter.author.roles:
            await inter.response.send_message(
                "You don't have permission to use this command.", ephemeral=True
            )
            return

        async with db.pool.acquire() as conn:
            result = await conn.execute(
                "DELETE FROM shop_roles WHERE role_id = $1", role.id
            )

        channel = self.bot.get_channel(Config.BOT_CHANNEL_ID)
        if channel:
            embed = disnake.Embed(
                title="🛒 Role Removed from Shop",
                description=f"{inter.author.mention} removed **{role.name}** from the shop",
                color=disnake.Color.red(),
            )
            await channel.send(embed=embed)

        await inter.response.send_message(
            f"Removed **{role.name}** from shop", ephemeral=True
        )

    @commands.slash_command(description="[MOD] Change the price of a role in the shop")
    async def shopprice(
        self,
        inter: disnake.ApplicationCommandInteraction,
        role: disnake.Role,
        price: int,
    ):
        mod_role = inter.guild.get_role(Config.MOD_ROLE_ID)
        if not mod_role or mod_role not in inter.author.roles:
            await inter.response.send_message(
                "You don't have permission to use this command.", ephemeral=True
            )
            return

        if price <= 0:
            await inter.response.send_message("Price must be positive.", ephemeral=True)
            return

        async with db.pool.acquire() as conn:
            existing = await conn.fetchval(
                "SELECT role_id FROM shop_roles WHERE role_id = $1", role.id
            )
            if not existing:
                await inter.response.send_message(
                    f"**{role.name}** is not in the shop. Use /shopadd first.",
                    ephemeral=True,
                )
                return

            await conn.execute(
                "UPDATE shop_roles SET price = $1 WHERE role_id = $2", price, role.id
            )

        channel = self.bot.get_channel(Config.BOT_CHANNEL_ID)
        if channel:
            embed = disnake.Embed(
                title="🛒 Role Price Updated",
                description=f"{inter.author.mention} changed **{role.name}** price to **{price} {Config.POINT_NAME}**",
                color=disnake.Color.blue(),
            )
            await channel.send(embed=embed)

        await inter.response.send_message(
            f"Updated **{role.name}** price to {price} {Config.POINT_NAME}",
            ephemeral=True,
        )

    async def autocomplete_roles(
        self, inter: disnake.ApplicationCommandInteraction, string: str
    ):
        """Autocomplete for purchasable roles only"""
        role_prices = await self.get_shop_roles()
        roles = []
        for role_id in role_prices.keys():
            role = inter.guild.get_role(role_id)
            if role and (string.lower() in role.name.lower() or not string):
                roles.append(role.name)
        return roles[:25]  # Discord limit

    @commands.slash_command(
        description="Use points to add a role to yourself or another user"
    )
    async def buyrole(
        self,
        inter: disnake.ApplicationCommandInteraction,
        role: str = commands.Param(autocomplete=autocomplete_roles),
        target: disnake.Member = None,
    ):
        target = target or inter.author

        # Get role prices from database
        role_prices = await self.get_shop_roles()

        # Find the role by name from purchasable roles
        selected_role = None
        for role_id in role_prices.keys():
            r = inter.guild.get_role(role_id)
            if r and r.name.lower() == role.lower():
                selected_role = r
                break

        if not selected_role:
            await inter.response.send_message(
                "This role is not available for purchase. Use `/shop` to see available roles.",
                ephemeral=True,
            )
            return

        role_cost = role_prices[selected_role.id]

        if selected_role in target.roles:
            await inter.response.send_message(
                f"{target.display_name} already has this role.", ephemeral=True
            )
            return

        async with db.pool.acquire() as conn:
            points = await conn.fetchval(
                "SELECT points FROM users WHERE user_id = $1", inter.author.id
            )
            points = points or 0

            if points < role_cost:
                await inter.response.send_message(
                    f"Not enough {Config.POINT_NAME}. You have {points}, need {role_cost}.",
                    ephemeral=True,
                )
                return

            # Deduct points
            await conn.execute(
                "UPDATE users SET points = points - $1 WHERE user_id = $2",
                role_cost,
                inter.author.id,
            )

            # Add to temp_roles table with expiration
            import datetime

            expires_at = datetime.datetime.now() + datetime.timedelta(
                minutes=Config.ROLE_DURATION_MINUTES
            )
            await conn.execute(
                """
                INSERT INTO temp_roles (user_id, role_id, expires_at) VALUES ($1, $2, $3)
                ON CONFLICT (user_id, role_id) DO UPDATE SET expires_at = $3
            """,
                target.id,
                selected_role.id,
                expires_at,
            )

        # Add role
        await target.add_roles(selected_role)

        channel = self.bot.get_channel(Config.BOT_CHANNEL_ID)
        if channel:
            duration_text = f"{Config.ROLE_DURATION_MINUTES} minute(s)"
            if target == inter.author:
                desc = f"{inter.author.mention} purchased the **{selected_role.name}** role for **{role_cost} {Config.POINT_NAME}** (expires in {duration_text})"
            else:
                desc = f"{inter.author.mention} gifted the **{selected_role.name}** role to {target.mention} for **{role_cost} {Config.POINT_NAME}** (expires in {duration_text})"

            embed = disnake.Embed(
                title="🎁 Role Purchased",
                description=desc,
                color=disnake.Color.purple(),
            )
            await channel.send(embed=embed)

        await inter.response.send_message(
            f"Role **{selected_role.name}** added to {target.display_name} for {Config.ROLE_DURATION_MINUTES} minute(s)!",
            ephemeral=True,
        )


def setup(bot):
    bot.add_cog(Points(bot))
