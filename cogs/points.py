import datetime
import random

import disnake
from disnake.ext import commands

from core.config import Config
from core.database import db


class Points(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.attack_cooldowns = {}  # Track last attack time per user
        self.trap_cooldowns = {}  # Track last trap time per user
        self.active_traps = {}  # {channel_id: {trigger_text: (creator_id, created_at)}}

    @commands.Cog.listener()
    async def on_message(self, message):
        if message.author.bot:
            return

        # Wait for database to be ready
        if db.pool is None:
            return

        # Check for traps first
        await self.check_traps(message)

        async with db.pool.acquire() as conn:
            user = await conn.fetchrow(
                "SELECT * FROM users WHERE user_id = $1", message.author.id
            )
            now = datetime.datetime.now()

            points_to_add = 5
            is_first_of_day = False

            if not user:
                # First time user ever - give 20 points
                points_to_add = 20
                is_first_of_day = True
                current_points = 0
                daily_earned = 0
                await conn.execute(
                    "INSERT INTO users (user_id, points, last_message_at, daily_earned, daily_earned_date) VALUES ($1, $2, $3, $2, $4)",
                    message.author.id,
                    points_to_add,
                    now,
                    now.date(),
                )
            else:
                last_msg = user["last_message_at"]
                current_points = user["points"] or 0

                # Get daily earned (reset if new day)
                daily_earned_date = user.get("daily_earned_date")
                if daily_earned_date is None or daily_earned_date < now.date():
                    daily_earned = 0
                else:
                    daily_earned = user.get("daily_earned") or 0

                # Check if first message of the day
                if last_msg is None or last_msg.date() < now.date():
                    points_to_add = 20
                    is_first_of_day = True
                    daily_earned = 0  # Reset daily earned for new day
                else:
                    # Regular message, check cooldown (15 seconds)
                    if (now - last_msg).total_seconds() < 15:
                        return
                    points_to_add = 5

                # Check daily cap (400 points per day from chatting)
                if daily_earned >= 400:
                    return  # Already hit daily cap

                # Limit points_to_add to not exceed cap
                if daily_earned + points_to_add > 400:
                    points_to_add = 400 - daily_earned

                if points_to_add <= 0:
                    return

                # Apply critical/bad luck based on current points
                if current_points < 500:
                    # 20% chance for critical (2x points)
                    if random.random() < 0.20:
                        points_to_add *= 2
                elif current_points < 1500:
                    # 40% chance for bad luck (0 points)
                    if random.random() < 0.40:
                        points_to_add = 0

                if points_to_add <= 0:
                    # Update last_message_at even if 0 points
                    await conn.execute(
                        "UPDATE users SET last_message_at = $1 WHERE user_id = $2",
                        now,
                        message.author.id,
                    )
                    return

                # Check if user has server booster role for 1.5x bonus
                if message.guild:
                    booster_role = message.guild.get_role(939954575216107540)
                    if booster_role and booster_role in message.author.roles:
                        # 50% chance for extra 0.5x (so either 1x or 1.5x total)
                        if random.random() < 0.50:
                            bonus = int(points_to_add * 0.5)
                            points_to_add += bonus

                await conn.execute(
                    "UPDATE users SET points = points + $1, last_message_at = $2, daily_earned = $3, daily_earned_date = $4 WHERE user_id = $5",
                    points_to_add,
                    now,
                    daily_earned + points_to_add,
                    now.date(),
                    message.author.id,
                )

    async def check_traps(self, message):
        """Check if message triggers any active traps"""
        if db.pool is None:
            return

        if message.channel.id not in self.active_traps:
            return

        channel_traps = self.active_traps[message.channel.id]
        message_lower = message.content.lower()
        triggered_trap = None
        now = datetime.datetime.now()

        # Clean up expired traps (2 minutes)
        expired_traps = []
        for trigger_text, (creator_id, created_at) in list(channel_traps.items()):
            if (now - created_at).total_seconds() > 120:  # 2 minutes
                expired_traps.append(trigger_text)

        for trigger_text in expired_traps:
            del channel_traps[trigger_text]

        if not channel_traps:
            del self.active_traps[message.channel.id]
            return

        for trigger_text, (creator_id, created_at) in list(channel_traps.items()):
            if trigger_text.lower() in message_lower:
                # Don't trigger on trap creator
                if message.author.id == creator_id:
                    continue
                triggered_trap = (trigger_text, creator_id)
                break

        if triggered_trap:
            trigger_text, creator_id = triggered_trap

            async with db.pool.acquire() as conn:
                # Check if victim has at least 10 points
                victim_points = await conn.fetchval(
                    "SELECT points FROM users WHERE user_id = $1", message.author.id
                )
                victim_points = victim_points or 0

                if victim_points >= 10:
                    # Steal 10 points from victim and give to trap creator
                    await conn.execute(
                        "UPDATE users SET points = points - 10 WHERE user_id = $1",
                        message.author.id,
                    )
                    await conn.execute(
                        """INSERT INTO users (user_id, points) VALUES ($1, 10)
                           ON CONFLICT (user_id) DO UPDATE SET points = users.points + 10""",
                        creator_id,
                    )

                    # Remove the trap after triggered
                    del channel_traps[trigger_text]
                    if not channel_traps:
                        del self.active_traps[message.channel.id]

                    # Notify about trap
                    creator = message.guild.get_member(creator_id)
                    creator_name = (
                        creator.display_name if creator else f"User {creator_id}"
                    )
                    await message.reply(
                        f"💣 **TRAP ACTIVATED!** {message.author.mention} triggered a trap set by **{creator_name}** and lost 10 {Config.POINT_NAME}!"
                    )

    @commands.slash_command(description="Set a trap with a trigger word")
    async def trap(
        self,
        inter: disnake.ApplicationCommandInteraction,
        trigger: str = commands.Param(
            description="Text that will trigger the trap (min 3 chars)",
            min_length=3,
            max_length=50,
        ),
    ):
        """Set a trap that steals 10 points from whoever types the trigger text"""
        # Check cooldown (1 minute)
        now = datetime.datetime.now()
        user_id = inter.author.id

        if user_id in self.trap_cooldowns:
            time_passed = (now - self.trap_cooldowns[user_id]).total_seconds()
            if time_passed < 60:
                remaining = 60 - int(time_passed)
                await inter.response.send_message(
                    f"⏰ You need to wait {remaining} more seconds before setting another trap.",
                    ephemeral=True,
                )
                return

        trigger_lower = trigger.lower()

        # Check if user has at least 10 points to set a trap
        async with db.pool.acquire() as conn:
            user_points = await conn.fetchval(
                "SELECT points FROM users WHERE user_id = $1", inter.author.id
            )
            user_points = user_points or 0

            if user_points < 10:
                await inter.response.send_message(
                    f"You need at least 10 {Config.POINT_NAME} to set a trap.",
                    ephemeral=True,
                )
                return

        # Check if trap already exists in this channel
        if inter.channel.id in self.active_traps:
            if trigger_lower in [
                t.lower() for t in self.active_traps[inter.channel.id]
            ]:
                await inter.response.send_message(
                    "A trap with this trigger already exists in this channel.",
                    ephemeral=True,
                )
                return

        # Set the trap
        if inter.channel.id not in self.active_traps:
            self.active_traps[inter.channel.id] = {}

        self.active_traps[inter.channel.id][trigger] = (
            inter.author.id,
            datetime.datetime.now(),
        )

        # Update cooldown
        self.trap_cooldowns[user_id] = now

        await inter.response.send_message(
            f'💣 Trap set! Anyone who types **"{trigger}"** in this channel within 2 minutes will lose 10 {Config.POINT_NAME} to you!',
            ephemeral=True,
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
        # Check cooldown (20 seconds)
        now = datetime.datetime.now()
        user_id = inter.author.id

        if user_id in self.attack_cooldowns:
            time_passed = (now - self.attack_cooldowns[user_id]).total_seconds()
            if time_passed < 20:
                remaining = 20 - int(time_passed)
                await inter.response.send_message(
                    f"⏰ You need to wait {remaining} more seconds before attacking again.",
                    ephemeral=True,
                )
                return

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

        # Check if target is a mod - mods always win
        if inter.guild:
            mod_role = inter.guild.get_role(Config.MOD_ROLE_ID)
            target_member = inter.guild.get_member(target.id)
            if mod_role and target_member and mod_role in target_member.roles:
                async with db.pool.acquire() as conn:
                    attacker_points = await conn.fetchval(
                        "SELECT points FROM users WHERE user_id = $1", inter.author.id
                    )
                    attacker_points = attacker_points or 0

                    if attacker_points < 10:
                        await inter.response.send_message(
                            f"You need at least 10 {Config.POINT_NAME} to attack.",
                            ephemeral=True,
                        )
                        return

                    # Attacker automatically loses against mod
                    await conn.execute(
                        "UPDATE users SET points = points - 10 WHERE user_id = $1",
                        inter.author.id,
                    )
                    await conn.execute(
                        "UPDATE users SET points = points + 10 WHERE user_id = $1",
                        target.id,
                    )
                    # Update cooldown
                    self.attack_cooldowns[user_id] = now
                    await inter.response.send_message(
                        f"⚖️ **It's impossible to win against the Lawmaker!** You lost 10 {Config.POINT_NAME} to {target.mention}!"
                    )
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
                # Update cooldown
                self.attack_cooldowns[user_id] = now
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
                # Update cooldown
                self.attack_cooldowns[user_id] = now
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
