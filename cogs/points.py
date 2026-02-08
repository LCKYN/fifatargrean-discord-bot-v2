import asyncio
import datetime
import random
import statistics
from datetime import timedelta, timezone

import disnake
from disnake.ext import commands, tasks

from core.config import Config
from core.database import db

# Bangkok timezone (UTC+7)
BANGKOK_TZ = timezone(timedelta(hours=7))


class Points(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.attack_cooldowns = {}  # Track last attack time per user
        self.beg_attack_cooldowns = {}  # Track last beg attack time per user
        self.multiattack_cooldowns = {}  # Track last multiattack time per user
        self.trap_cooldowns = {}  # Track last trap time per user
        self.active_traps = {}  # {channel_id: {trigger_text: (creator_id, created_at)}}
        self.dodge_cooldowns = {}  # Track last dodge time per user
        self.active_dodges = {}  # {user_id: activated_at}
        self.attack_last_use = {}  # Track last attack time to block dodge for 5 minutes
        self.counter_cooldowns = {}  # Track last counter time per user
        self.active_counters = {}  # {defender_id: {attacker_id: activated_at}}
        self.shield_cooldowns = {}  # Track last shield time per user
        self.active_shields = {}  # {user_id: activated_at}
        self.active_airdrops = {}  # {message_id: {"claimed_users": set(), "count": 0}}
        self.lottery_entries = {}  # {number: [user_ids]} - current lottery entries
        self.lottery_user_count = {}  # {user_id: count} - track how many tickets each user bought (max 10)

    def cog_unload(self):
        self.daily_tax_task.cancel()

    async def get_tax_pool(self, conn) -> int:
        """Get current tax pool amount"""
        tax = await conn.fetchval(
            "SELECT value FROM bot_settings WHERE key = 'tax_pool'"
        )
        return int(tax) if tax else 0

    async def add_to_tax_pool(self, conn, amount: int):
        """Add amount to tax pool"""
        await conn.execute(
            """INSERT INTO bot_settings (key, value) VALUES ('tax_pool', $1::TEXT)
               ON CONFLICT (key) DO UPDATE SET value = (COALESCE(CAST(bot_settings.value AS INTEGER), 0) + $2)::TEXT""",
            str(amount),
            amount,
        )

    async def set_tax_pool(self, conn, amount: int):
        """Set tax pool to specific amount"""
        await conn.execute(
            """INSERT INTO bot_settings (key, value) VALUES ('tax_pool', $1)
               ON CONFLICT (key) DO UPDATE SET value = $1::TEXT""",
            str(amount),
        )

    async def get_lottery_pool(self, conn) -> int:
        """Get current lottery prize pool"""
        pool = await conn.fetchval(
            "SELECT value FROM bot_settings WHERE key = 'lottery_pool'"
        )
        return int(pool) if pool else 0

    async def add_to_lottery_pool(self, conn, amount: int):
        """Add amount to lottery pool"""
        await conn.execute(
            """INSERT INTO bot_settings (key, value) VALUES ('lottery_pool', $1::TEXT)
               ON CONFLICT (key) DO UPDATE SET value = (COALESCE(CAST(bot_settings.value AS INTEGER), 0) + $2)::TEXT""",
            str(amount),
            amount,
        )

    async def set_lottery_pool(self, conn, amount: int):
        """Set lottery pool to specific amount"""
        await conn.execute(
            """INSERT INTO bot_settings (key, value) VALUES ('lottery_pool', $1)
               ON CONFLICT (key) DO UPDATE SET value = $1::TEXT""",
            str(amount),
        )

    @commands.Cog.listener()
    async def on_member_join(self, member: disnake.Member):
        """Give 1000 points to first-time members"""
        if member.bot:
            return

        # Wait for database to be ready
        if db.pool is None:
            return

        async with db.pool.acquire() as conn:
            # Check if user already exists in database
            existing = await conn.fetchval(
                "SELECT user_id FROM users WHERE user_id = $1", member.id
            )

            if not existing:
                # First time user - give 1000 points (use ON CONFLICT to prevent duplicates)
                inserted = await conn.execute(
                    "INSERT INTO users (user_id, points) VALUES ($1, 1000) ON CONFLICT (user_id) DO NOTHING",
                    member.id,
                )

                # Send welcome message to bot channel only if actually inserted
                if inserted == "INSERT 0 1":
                    channel = self.bot.get_channel(Config.BOT_CHANNEL_ID)
                    if channel:
                        embed = disnake.Embed(
                            title="🎉 Welcome Bonus!",
                            description=f"Welcome {member.mention}! You received **1000 {Config.POINT_NAME}** as a welcome gift!",
                            color=disnake.Color.green(),
                        )
                        await channel.send(embed=embed)

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
            now_bangkok = datetime.datetime.now(BANGKOK_TZ)
            today_bangkok = now_bangkok.date()

            points_to_add = 100
            is_first_of_day = False

            if not user:
                # First time user ever - give 1000 points welcome bonus
                # This does NOT count towards daily earned cap
                points_to_add = 1000
                is_first_of_day = True
                current_points = 0
                daily_earned = 0  # Start at 0, bonus doesn't count
                inserted = await conn.execute(
                    "INSERT INTO users (user_id, points, last_message_at, daily_earned, daily_earned_date) VALUES ($1, $2, $3, 0, $4) ON CONFLICT (user_id) DO NOTHING",
                    message.author.id,
                    points_to_add,
                    now,
                    today_bangkok,
                )

                # Send welcome message only if actually inserted
                if inserted == "INSERT 0 1":
                    channel = message.channel
                    embed = disnake.Embed(
                        title="🎉 Welcome Bonus!",
                        description=f"Welcome {message.author.mention}! You received **1000 {Config.POINT_NAME}** as a welcome gift!",
                        color=disnake.Color.green(),
                    )
                    await channel.send(embed=embed, delete_after=10)
            else:
                last_msg = user["last_message_at"]
                current_points = user["points"] or 0

                # Get daily earned (reset if new day in Bangkok timezone)
                daily_earned_date = user.get("daily_earned_date")
                if daily_earned_date is None or daily_earned_date < today_bangkok:
                    daily_earned = 0
                else:
                    daily_earned = user.get("daily_earned") or 0

                # Check if first message of the day (Bangkok time)
                if last_msg is None:
                    points_to_add = 1000
                    is_first_of_day = True
                    daily_earned = 0
                else:
                    # Convert last_msg to Bangkok timezone to check date
                    if last_msg.tzinfo is None:
                        last_msg_bangkok = last_msg.replace(
                            tzinfo=timezone.utc
                        ).astimezone(BANGKOK_TZ)
                    else:
                        last_msg_bangkok = last_msg.astimezone(BANGKOK_TZ)

                    if last_msg_bangkok.date() < today_bangkok:
                        points_to_add = 1000
                        is_first_of_day = True
                        daily_earned = 0  # Reset daily earned for new day
                    else:
                        # Regular message, check cooldown (15 seconds)
                        if (now - last_msg.replace(tzinfo=None)).total_seconds() < 15:
                            return
                        points_to_add = 100

                # Check daily cap (2500 points per day from chatting)
                if daily_earned >= 2500:
                    return  # Already hit daily cap

                # Limit points_to_add to not exceed cap
                if daily_earned + points_to_add > 2500:
                    points_to_add = 2500 - daily_earned

                if points_to_add <= 0:
                    return

                # Save original points before applying luck modifiers (for daily tracking)
                points_for_daily_cap = points_to_add

                # Apply critical/bad luck based on current points
                if current_points < 500:
                    # 20% chance for critical (2x points)
                    if random.random() < 0.20:
                        points_to_add *= 2
                elif current_points >= 1500:
                    # 50% chance for bad luck (0 points) - rich tax
                    if random.random() < 0.50:
                        points_to_add = 0
                # 500-1500 points: no special effect

                if points_to_add <= 0:
                    # Update last_message_at and daily_earned even if 0 points (bad luck still counts toward cap)
                    await conn.execute(
                        "UPDATE users SET last_message_at = $1, daily_earned = $2, daily_earned_date = $3 WHERE user_id = $4",
                        now,
                        daily_earned + points_for_daily_cap,
                        today_bangkok,
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
                    daily_earned + points_for_daily_cap,
                    today_bangkok,
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

        # Clean up expired traps (15 minutes)
        expired_traps = []
        for trigger_text, trap_data in list(channel_traps.items()):
            # Handle both old format (2-tuple) and new format (3-tuple with cost)
            if len(trap_data) == 2:
                creator_id, created_at = trap_data
            else:
                creator_id, created_at, _ = trap_data

            if (now - created_at).total_seconds() > 900:  # 15 minutes
                expired_traps.append(trigger_text)

        for trigger_text in expired_traps:
            del channel_traps[trigger_text]

        if not channel_traps:
            del self.active_traps[message.channel.id]
            return

        for trigger_text, trap_data in list(channel_traps.items()):
            # Handle both old format (2-tuple) and new format (3-tuple with cost)
            if len(trap_data) == 2:
                creator_id, created_at = trap_data
                trap_cost = 40  # Default cost for old traps
            else:
                creator_id, created_at, trap_cost = trap_data

            if trigger_text.lower() in message_lower:
                # Don't trigger on trap creator
                if message.author.id == creator_id:
                    continue
                triggered_trap = (trigger_text, creator_id, trap_cost)
                break

        if triggered_trap:
            trigger_text, creator_id, trap_cost = triggered_trap
            loss_amount = trap_cost * 5  # Victim loses 5x the cost
            gain_amount = trap_cost * 5  # Creator gains 5x the cost

            async with db.pool.acquire() as conn:
                # Check if victim has enough points
                victim_points = await conn.fetchval(
                    "SELECT points FROM users WHERE user_id = $1", message.author.id
                )
                victim_points = victim_points or 0

                if victim_points >= loss_amount:
                    # Steal 5x trap_cost from victim and give 5x to trap creator with profit tracking
                    await conn.execute(
                        "UPDATE users SET points = points - $1 WHERE user_id = $2",
                        loss_amount,
                        message.author.id,
                    )
                    await conn.execute(
                        """INSERT INTO users (user_id, points, profit_trap) VALUES ($1, $2, $2)
                           ON CONFLICT (user_id) DO UPDATE SET points = users.points + $2, profit_trap = users.profit_trap + $2""",
                        creator_id,
                        gain_amount,
                    )

                    # Remove the trap after triggered
                    del channel_traps[trigger_text]
                    if not channel_traps:
                        del self.active_traps[message.channel.id]

                    # Notify about trap - delete after 10 seconds
                    creator = message.guild.get_member(creator_id)
                    creator_name = (
                        creator.display_name if creator else f"User {creator_id}"
                    )
                    trap_msg = await message.reply(
                        f"💣 **TRAP ACTIVATED!** {message.author.mention} triggered a trap set by **{creator_name}** and lost {loss_amount} {Config.POINT_NAME}! {creator_name} gained {gain_amount} {Config.POINT_NAME}!"
                    )
                    await asyncio.sleep(10)
                    await trap_msg.delete()
                else:
                    # Victim doesn't have enough points - add role for 1440 minutes
                    penalty_role = message.guild.get_role(1456114946764181557)
                    if penalty_role:
                        member = message.guild.get_member(message.author.id)
                        if member and penalty_role not in member.roles:
                            await member.add_roles(penalty_role)

                            # Schedule role removal after 1440 minutes (24 hours)
                            async def remove_role_later():
                                await asyncio.sleep(
                                    1440 * 60
                                )  # 1440 minutes in seconds
                                try:
                                    if penalty_role in member.roles:
                                        await member.remove_roles(penalty_role)
                                except:
                                    pass

                            asyncio.create_task(remove_role_later())

                    # Remove the trap after triggered
                    del channel_traps[trigger_text]
                    if not channel_traps:
                        del self.active_traps[message.channel.id]

                    # Notify about trap and penalty - delete after 10 seconds
                    creator = message.guild.get_member(creator_id)
                    creator_name = (
                        creator.display_name if creator else f"User {creator_id}"
                    )
                    trap_msg = await message.reply(
                        f"💣 **TRAP ACTIVATED!** {message.author.mention} triggered a trap set by **{creator_name}** but doesn't have enough points! Penalty role added for 24 hours!"
                    )
                    await asyncio.sleep(10)
                    await trap_msg.delete()

    @commands.slash_command(description="Set a trap with a trigger word")
    async def trap(
        self,
        inter: disnake.ApplicationCommandInteraction,
        trigger: str = commands.Param(
            description="Text that will trigger the trap (min 5 chars)",
            min_length=5,
            max_length=50,
        ),
        cost: int = commands.Param(
            description="Cost to set trap (victim loses this, you gain 5x)",
            ge=10,
            le=500,
            default=40,
        ),
    ):
        """Set a trap that steals points from whoever types the trigger text (you gain 5x the cost)"""
        # Check cooldown (30 minutes)
        now = datetime.datetime.now()
        user_id = inter.author.id

        if user_id in self.trap_cooldowns:
            time_passed = (now - self.trap_cooldowns[user_id]).total_seconds()
            if time_passed < 1800:
                remaining = 1800 - int(time_passed)
                minutes = remaining // 60
                seconds = remaining % 60
                await inter.response.send_message(
                    f"⏰ You need to wait {minutes} minutes and {seconds} seconds before setting another trap.",
                    ephemeral=True,
                )
                return

        trigger_lower = trigger.lower()

        # Check if trap already exists in this channel FIRST
        trap_already_exists = False
        if inter.channel.id in self.active_traps:
            if trigger_lower in [
                t.lower() for t in self.active_traps[inter.channel.id]
            ]:
                trap_already_exists = True

        # Check if user has enough points to set a trap
        async with db.pool.acquire() as conn:
            user_points = await conn.fetchval(
                "SELECT points FROM users WHERE user_id = $1", inter.author.id
            )
            user_points = user_points or 0

            if user_points < cost:
                await inter.response.send_message(
                    f"You need at least {cost} {Config.POINT_NAME} to set a trap.",
                    ephemeral=True,
                )
                return

            # Only deduct cost if trap doesn't already exist
            if not trap_already_exists:
                await conn.execute(
                    "UPDATE users SET points = points - $1 WHERE user_id = $2",
                    cost,
                    inter.author.id,
                )

        # Set the trap only if it doesn't already exist
        if not trap_already_exists:
            if inter.channel.id not in self.active_traps:
                self.active_traps[inter.channel.id] = {}

            self.active_traps[inter.channel.id][trigger] = (
                inter.author.id,
                datetime.datetime.now(),
                cost,  # Store the cost with the trap
            )

            # Update cooldown
            self.trap_cooldowns[user_id] = now

        # Always show the success message regardless of whether trap was actually set
        loss_amount = cost * 5
        gain_amount = cost * 5
        await inter.response.send_message(
            f'💣 Trap set! (-{cost} {Config.POINT_NAME}) Anyone who types **"{trigger}"** in this channel within 15 minutes will lose {loss_amount} {Config.POINT_NAME} and you\'ll gain {gain_amount} {Config.POINT_NAME}!',
            ephemeral=True,
        )

    @commands.slash_command(description="Counter a trap by guessing the trigger word")
    async def trapcounter(
        self,
        inter: disnake.ApplicationCommandInteraction,
        trigger: str = commands.Param(
            description="Guess the trap trigger word",
            min_length=5,
            max_length=50,
        ),
        cost: int = commands.Param(
            description="Cost to counter (you gain 10x if correct)",
            ge=10,
            le=500,
            default=40,
        ),
    ):
        """Try to counter a trap - if you guess the exact trigger, steal 10x cost from trap setter"""
        # Check if user has enough points
        async with db.pool.acquire() as conn:
            user_points = await conn.fetchval(
                "SELECT points FROM users WHERE user_id = $1", inter.author.id
            )
            user_points = user_points or 0

            if user_points < cost:
                await inter.response.send_message(
                    f"You need at least {cost} {Config.POINT_NAME} to attempt a counter.",
                    ephemeral=True,
                )
                return

            # Deduct cost from counter user
            await conn.execute(
                "UPDATE users SET points = points - $1 WHERE user_id = $2",
                cost,
                inter.author.id,
            )

        # Check if there's an active trap in this channel with exact trigger match
        if inter.channel.id not in self.active_traps:
            await inter.response.send_message(
                f"❌ No trap found! You lost {cost} {Config.POINT_NAME}.",
                ephemeral=True,
            )
            return

        channel_traps = self.active_traps[inter.channel.id]

        # Look for exact match (case-sensitive)
        trap_found = None
        trap_creator_id = None
        trap_cost = None

        for trap_trigger, trap_data in channel_traps.items():
            if trap_trigger == trigger:  # Exact match
                trap_found = trap_trigger
                if len(trap_data) == 2:
                    trap_creator_id, created_at = trap_data
                    trap_cost = 40  # Default
                else:
                    trap_creator_id, created_at, trap_cost = trap_data
                break

        if not trap_found:
            await inter.response.send_message(
                f"❌ No trap with that exact trigger! You lost {cost} {Config.POINT_NAME}.",
                ephemeral=True,
            )
            return

        # Can't counter your own trap
        if trap_creator_id == inter.author.id:
            # Refund the cost
            async with db.pool.acquire() as conn:
                await conn.execute(
                    "UPDATE users SET points = points + $1 WHERE user_id = $2",
                    cost,
                    inter.author.id,
                )
            await inter.response.send_message(
                "❌ You can't counter your own trap! (Cost refunded)",
                ephemeral=True,
            )
            return

        # Success! Counter the trap
        gain_amount = cost * 10
        tax_amount = int(gain_amount * 0.10)
        net_gain = gain_amount - tax_amount

        async with db.pool.acquire() as conn:
            # Counter user gains 10x their cost minus 10% tax
            await conn.execute(
                """INSERT INTO users (user_id, points) VALUES ($1, $2)
                   ON CONFLICT (user_id) DO UPDATE SET points = users.points + $2""",
                inter.author.id,
                net_gain,
            )

            # Trap setter loses 10x counter cost (can go negative)
            await conn.execute(
                "UPDATE users SET points = points - $1 WHERE user_id = $2",
                gain_amount,
                trap_creator_id,
            )

            # Add tax to pool
            await self.add_to_tax_pool(conn, tax_amount)

        # Remove the trap
        del channel_traps[trap_found]
        if not channel_traps:
            del self.active_traps[inter.channel.id]

        # Notify success
        trap_setter = inter.guild.get_member(trap_creator_id)
        trap_setter_name = (
            trap_setter.display_name if trap_setter else f"User {trap_creator_id}"
        )

        # Send notification to channel 1456204479203639340
        notification_channel = self.bot.get_channel(1456204479203639340)
        if notification_channel:
            embed = disnake.Embed(
                title="🎯 TRAP COUNTERED!",
                description=f"{inter.author.mention} successfully countered **{trap_setter_name}**'s trap!\n\n"
                f'**Trigger:** "{trap_found}"\n'
                f"**Counter Cost:** {cost} {Config.POINT_NAME}\n"
                f"**Gained:** {net_gain} {Config.POINT_NAME} (10x - 10% tax)\n"
                f"**Tax Collected:** {tax_amount} {Config.POINT_NAME}\n"
                f"**Trap Setter Lost:** {gain_amount} {Config.POINT_NAME}",
                color=disnake.Color.green(),
            )
            await notification_channel.send(embed=embed)

        await inter.response.send_message(
            f"🎯 **TRAP COUNTERED!** {inter.author.mention} found **{trap_setter_name}**'s trap!\n"
            f"💰 Gained **{net_gain} {Config.POINT_NAME}** (10x - 10% tax)\n"
            f'💣 Trap removed: **"{trap_found}"**',
            ephemeral=False,
        )

        # Delete the response after 10 seconds
        await asyncio.sleep(10)
        await inter.delete_original_response()

    @commands.slash_command(
        description="Check how many traps are active (costs 100 points)"
    )
    async def checktrap(self, inter: disnake.ApplicationCommandInteraction):
        """Pay 100 points to see how many traps are active in this channel"""
        cost = 100

        # Check if user has enough points
        async with db.pool.acquire() as conn:
            user_points = await conn.fetchval(
                "SELECT points FROM users WHERE user_id = $1", inter.author.id
            )
            user_points = user_points or 0

            if user_points < cost:
                await inter.response.send_message(
                    f"You need at least {cost} {Config.POINT_NAME} to check traps.",
                    ephemeral=True,
                )
                return

            # Deduct cost
            await conn.execute(
                "UPDATE users SET points = points - $1 WHERE user_id = $2",
                cost,
                inter.author.id,
            )

        # Count active traps in this channel
        trap_count = 0
        if inter.channel.id in self.active_traps:
            trap_count = len(self.active_traps[inter.channel.id])

        await inter.response.send_message(
            f"🔍 **Trap Check** (-{cost} {Config.POINT_NAME})\n"
            f"Active traps in this channel: **{trap_count}**",
            ephemeral=True,
        )

    @commands.slash_command(
        description="Buy lottery tickets with 2-digit numbers (00-99), space separated for multiple"
    )
    async def buylottery(
        self,
        inter: disnake.ApplicationCommandInteraction,
        numbers: str = commands.Param(
            description="Pick 2-digit number(s) 0-99, space separated (e.g. '10 12 15')",
        ),
    ):
        """Buy lottery tickets for 100 points each (max 10 per user)"""
        cost_per_ticket = 100
        max_tickets_per_user = 10
        lottery_channel_id = 956301076271857764

        # Parse numbers from input string
        try:
            number_list = [int(n.strip()) for n in numbers.split() if n.strip()]
        except ValueError:
            await inter.response.send_message(
                "❌ Invalid input. Please enter numbers only (e.g. '10 12 15').",
                ephemeral=True,
            )
            return

        # Validate all numbers are in range 0-99
        invalid_numbers = [n for n in number_list if n < 0 or n > 99]
        if invalid_numbers:
            await inter.response.send_message(
                f"❌ Invalid numbers: {invalid_numbers}. Numbers must be between 0-99.",
                ephemeral=True,
            )
            return

        if not number_list:
            await inter.response.send_message(
                "❌ Please enter at least one number.",
                ephemeral=True,
            )
            return

        # Check current user ticket count
        current_count = self.lottery_user_count.get(inter.author.id, 0)
        remaining_slots = max_tickets_per_user - current_count

        if remaining_slots <= 0:
            await inter.response.send_message(
                f"❌ You have already purchased the maximum of {max_tickets_per_user} lottery tickets.",
                ephemeral=True,
            )
            return

        # Limit to remaining slots
        if len(number_list) > remaining_slots:
            await inter.response.send_message(
                f"❌ You can only buy {remaining_slots} more ticket(s). "
                f"You already have {current_count}/{max_tickets_per_user} tickets.",
                ephemeral=True,
            )
            return

        total_cost = cost_per_ticket * len(number_list)

        # Check if user has enough points
        async with db.pool.acquire() as conn:
            user_points = await conn.fetchval(
                "SELECT points FROM users WHERE user_id = $1", inter.author.id
            )
            user_points = user_points or 0

            if user_points < total_cost:
                await inter.response.send_message(
                    f"You need at least {total_cost} {Config.POINT_NAME} to buy {len(number_list)} lottery ticket(s).",
                    ephemeral=True,
                )
                return

            # Deduct cost
            await conn.execute(
                "UPDATE users SET points = points - $1 WHERE user_id = $2",
                total_cost,
                inter.author.id,
            )

            # Add to lottery pool
            await self.add_to_lottery_pool(conn, total_cost)

        # Add user to lottery entries for each number
        for number in number_list:
            if number not in self.lottery_entries:
                self.lottery_entries[number] = []
            self.lottery_entries[number].append(inter.author.id)

        # Update user ticket count
        self.lottery_user_count[inter.author.id] = current_count + len(number_list)
        new_count = self.lottery_user_count[inter.author.id]

        # Format numbers for display
        numbers_display = ", ".join([f"{n:02d}" for n in number_list])

        # Send to lottery channel
        lottery_channel = self.bot.get_channel(lottery_channel_id)
        if lottery_channel:
            await lottery_channel.send(
                f"🎫 **Lottery Ticket Purchased!** (-{total_cost} {Config.POINT_NAME})\n"
                f"**Buyer:** {inter.author.mention}\n"
                f"**Numbers:** {numbers_display}\n"
                f"**Tickets:** {len(number_list)} | **Total owned:** {new_count}/{max_tickets_per_user}\n"
                f"Good luck!"
            )

        # If used in lottery channel, don't delete; otherwise delete after 5 seconds
        is_lottery_channel = inter.channel.id == lottery_channel_id

        await inter.response.send_message(
            f"🎫 **Lottery Ticket Purchased!** (-{total_cost} {Config.POINT_NAME})\n"
            f"Your numbers: **{numbers_display}**\n"
            f"Tickets owned: {new_count}/{max_tickets_per_user}\n"
            f"Good luck!",
            ephemeral=not is_lottery_channel,
        )

        if not is_lottery_channel:
            await asyncio.sleep(5)
            await inter.delete_original_response()

    @commands.slash_command(
        description="Buy random lottery tickets (1-10 random numbers)"
    )
    async def buyrandomlottery(
        self,
        inter: disnake.ApplicationCommandInteraction,
        amount: int = commands.Param(
            description="Number of random tickets to buy (1-10)",
            ge=1,
            le=10,
        ),
    ):
        """Buy random lottery tickets for 100 points each (max 10 per user)"""
        cost_per_ticket = 100
        max_tickets_per_user = 10
        lottery_channel_id = 956301076271857764

        # Check current user ticket count
        current_count = self.lottery_user_count.get(inter.author.id, 0)
        remaining_slots = max_tickets_per_user - current_count

        if remaining_slots <= 0:
            await inter.response.send_message(
                f"❌ You have already purchased the maximum of {max_tickets_per_user} lottery tickets.",
                ephemeral=True,
            )
            return

        # Limit to remaining slots
        actual_amount = min(amount, remaining_slots)
        if actual_amount < amount:
            await inter.response.send_message(
                f"❌ You can only buy {remaining_slots} more ticket(s). "
                f"You already have {current_count}/{max_tickets_per_user} tickets.",
                ephemeral=True,
            )
            return

        total_cost = cost_per_ticket * actual_amount

        # Check if user has enough points
        async with db.pool.acquire() as conn:
            user_points = await conn.fetchval(
                "SELECT points FROM users WHERE user_id = $1", inter.author.id
            )
            user_points = user_points or 0

            if user_points < total_cost:
                await inter.response.send_message(
                    f"You need at least {total_cost} {Config.POINT_NAME} to buy {actual_amount} lottery ticket(s).",
                    ephemeral=True,
                )
                return

            # Deduct cost
            await conn.execute(
                "UPDATE users SET points = points - $1 WHERE user_id = $2",
                total_cost,
                inter.author.id,
            )

            # Add to lottery pool
            await self.add_to_lottery_pool(conn, total_cost)

        # Generate random unique numbers
        number_list = random.sample(range(0, 100), actual_amount)

        # Add user to lottery entries for each number
        for number in number_list:
            if number not in self.lottery_entries:
                self.lottery_entries[number] = []
            self.lottery_entries[number].append(inter.author.id)

        # Update user ticket count
        self.lottery_user_count[inter.author.id] = current_count + actual_amount
        new_count = self.lottery_user_count[inter.author.id]

        # Format numbers for display
        numbers_display = ", ".join([f"{n:02d}" for n in sorted(number_list)])

        # Send to lottery channel
        lottery_channel = self.bot.get_channel(lottery_channel_id)
        if lottery_channel:
            await lottery_channel.send(
                f"🎲 **Random Lottery Tickets Purchased!** (-{total_cost} {Config.POINT_NAME})\n"
                f"**Buyer:** {inter.author.mention}\n"
                f"**Numbers:** {numbers_display}\n"
                f"**Tickets:** {actual_amount} | **Total owned:** {new_count}/{max_tickets_per_user}\n"
                f"Good luck!"
            )

        # If used in lottery channel, don't delete; otherwise delete after 5 seconds
        is_lottery_channel = inter.channel.id == lottery_channel_id

        await inter.response.send_message(
            f"🎲 **Random Lottery Tickets Purchased!** (-{total_cost} {Config.POINT_NAME})\n"
            f"Your numbers: **{numbers_display}**\n"
            f"Tickets owned: {new_count}/{max_tickets_per_user}\n"
            f"Good luck!",
            ephemeral=not is_lottery_channel,
        )

        if not is_lottery_channel:
            await asyncio.sleep(5)
            await inter.delete_original_response()

    @commands.slash_command(description="[MOD] Draw the lottery and pick 2 winners")
    async def drawlottery(self, inter: disnake.ApplicationCommandInteraction):
        """Draw lottery and distribute prizes (mod only)"""
        # Check if user has mod role
        mod_role = inter.guild.get_role(Config.MOD_ROLE_ID)
        if not mod_role or mod_role not in inter.author.roles:
            await inter.response.send_message(
                "❌ This command is only available to moderators.", ephemeral=True
            )
            return

        # Check if there are any lottery entries
        if not self.lottery_entries:
            await inter.response.send_message(
                "❌ No lottery tickets have been purchased yet!",
                ephemeral=True,
            )
            return

        # Get current lottery pool
        async with db.pool.acquire() as conn:
            prize_pool = await self.get_lottery_pool(conn)

        # Draw 2 winning numbers
        winning_numbers = random.sample(range(0, 100), 2)
        winning_number_1 = winning_numbers[0]
        winning_number_2 = winning_numbers[1]

        # Split prize pool into 2 prizes (50% each)
        prize_per_number = prize_pool // 2

        # Check if there are winners for each number
        winners_1 = self.lottery_entries.get(winning_number_1, [])
        winners_2 = self.lottery_entries.get(winning_number_2, [])

        # Send notification to channel 956301076271857764
        notification_channel = self.bot.get_channel(956301076271857764)

        total_tax_collected = 0
        prize_distributed = False
        results = []

        # Process Prize 1
        if winners_1:
            tax_1 = int(prize_per_number * 0.10)
            prize_after_tax_1 = prize_per_number - tax_1
            prize_per_winner_1 = prize_after_tax_1 // len(winners_1)
            total_tax_collected += tax_1
            prize_distributed = True

            async with db.pool.acquire() as conn:
                for winner_id in winners_1:
                    await conn.execute(
                        """INSERT INTO users (user_id, points) VALUES ($1, $2)
                           ON CONFLICT (user_id) DO UPDATE SET points = users.points + $2""",
                        winner_id,
                        prize_per_winner_1,
                    )

            winner_mentions_1 = [f"<@{winner_id}>" for winner_id in winners_1]
            results.append(
                {
                    "number": winning_number_1,
                    "prize_pool": prize_per_number,
                    "tax": tax_1,
                    "winners": winners_1,
                    "prize_per_winner": prize_per_winner_1,
                    "winner_text": ", ".join(winner_mentions_1),
                }
            )
        else:
            results.append(
                {
                    "number": winning_number_1,
                    "prize_pool": prize_per_number,
                    "winners": [],
                }
            )

        # Process Prize 2
        if winners_2:
            tax_2 = int(prize_per_number * 0.10)
            prize_after_tax_2 = prize_per_number - tax_2
            prize_per_winner_2 = prize_after_tax_2 // len(winners_2)
            total_tax_collected += tax_2
            prize_distributed = True

            async with db.pool.acquire() as conn:
                for winner_id in winners_2:
                    await conn.execute(
                        """INSERT INTO users (user_id, points) VALUES ($1, $2)
                           ON CONFLICT (user_id) DO UPDATE SET points = users.points + $2""",
                        winner_id,
                        prize_per_winner_2,
                    )

            winner_mentions_2 = [f"<@{winner_id}>" for winner_id in winners_2]
            results.append(
                {
                    "number": winning_number_2,
                    "prize_pool": prize_per_number,
                    "tax": tax_2,
                    "winners": winners_2,
                    "prize_per_winner": prize_per_winner_2,
                    "winner_text": ", ".join(winner_mentions_2),
                }
            )
        else:
            results.append(
                {
                    "number": winning_number_2,
                    "prize_pool": prize_per_number,
                    "winners": [],
                }
            )

        # Handle tax and pool reset
        async with db.pool.acquire() as conn:
            if total_tax_collected > 0:
                await self.add_to_tax_pool(conn, total_tax_collected)

            # Calculate remaining pool (from numbers with no winners)
            remaining_pool = 0
            if not winners_1:
                remaining_pool += prize_per_number
            if not winners_2:
                remaining_pool += prize_per_number

            if prize_distributed:
                # Reset to 5000 + any unclaimed prizes
                await self.set_lottery_pool(conn, 5000 + remaining_pool)
            # If no winners at all, pool stays as is (already handled by not distributing)

        # Build embed
        if not winners_1 and not winners_2:
            embed = disnake.Embed(
                title="🎰 Lottery Draw - No Winners!",
                description=f"**Winning Numbers:** {winning_number_1:02d} & {winning_number_2:02d}\n"
                f"**Total Prize Pool:** {prize_pool:,} {Config.POINT_NAME}\n\n"
                f"No one picked either winning number!\n"
                f"The prize pool carries over to the next draw!",
                color=disnake.Color.orange(),
            )
            if notification_channel:
                await notification_channel.send(embed=embed)

            await inter.response.send_message(
                f"🎰 **Lottery drawn!** Winning numbers: **{winning_number_1:02d}** & **{winning_number_2:02d}**\n"
                f"No winners this time. Prize pool ({prize_pool:,} {Config.POINT_NAME}) carries over!",
                ephemeral=True,
            )
        else:
            # Build description for embed
            description = f"**Total Prize Pool:** {prize_pool:,} {Config.POINT_NAME}\n"
            description += (
                f"**Prize per Number:** {prize_per_number:,} {Config.POINT_NAME}\n\n"
            )

            for i, result in enumerate(results, 1):
                description += f"**🎯 Prize {i} - Number {result['number']:02d}**\n"
                if result["winners"]:
                    description += f"Winners: {len(result['winners'])}\n"
                    description += f"Prize per Winner: {result['prize_per_winner']:,} {Config.POINT_NAME}\n"
                    description += f"🎉 {result['winner_text']}\n\n"
                else:
                    description += f"No winners - carries over!\n\n"

            description += (
                f"**Total Tax Collected:** {total_tax_collected:,} {Config.POINT_NAME}"
            )

            embed = disnake.Embed(
                title="🎰 Lottery Draw Results!",
                description=description,
                color=disnake.Color.gold(),
            )
            if notification_channel:
                await notification_channel.send(embed=embed)

            total_winners = len(winners_1) + len(winners_2)
            await inter.response.send_message(
                f"🎰 **Lottery drawn!** Winning numbers: **{winning_number_1:02d}** & **{winning_number_2:02d}**\n"
                f"🎉 {total_winners} total winner(s)!",
                ephemeral=True,
            )

        # Clear lottery entries and user counts for next round
        self.lottery_entries = {}
        self.lottery_user_count = {}

    @commands.slash_command(description="Check the current lottery prize pool")
    async def checklottery(self, inter: disnake.ApplicationCommandInteraction):
        """Check the current lottery prize pool and entries"""
        lottery_channel_id = 956301076271857764

        async with db.pool.acquire() as conn:
            prize_pool = await self.get_lottery_pool(conn)

        # Count total tickets sold
        total_tickets = sum(len(users) for users in self.lottery_entries.values())

        # Count unique participants
        unique_participants = len(self.lottery_user_count)

        # Get user's current ticket count
        user_tickets = self.lottery_user_count.get(inter.author.id, 0)

        embed = disnake.Embed(
            title="🎫 Lottery Status",
            description=f"**Current Prize Pool:** {prize_pool:,} {Config.POINT_NAME}\n"
            f"**Tickets Sold:** {total_tickets}\n"
            f"**Participants:** {unique_participants}\n"
            f"**Cost per Ticket:** 100 {Config.POINT_NAME}\n"
            f"**Max per User:** 10 tickets\n"
            f"**Tax on Winnings:** 10%\n\n"
            f"**Your Tickets:** {user_tickets}/10",
            color=disnake.Color.purple(),
        )

        # Send to lottery channel
        lottery_channel = self.bot.get_channel(lottery_channel_id)
        if lottery_channel:
            await lottery_channel.send(embed=embed)

        await inter.response.send_message(embed=embed, ephemeral=True)

    @commands.slash_command(description="[MOD] Add points to the lottery prize pool")
    async def addprize(
        self,
        inter: disnake.ApplicationCommandInteraction,
        amount: int = commands.Param(
            description="Amount to add to the prize pool",
            ge=1,
        ),
    ):
        """Add points to the lottery prize pool (mod only)"""
        # Check if user has mod role
        mod_role = inter.guild.get_role(Config.MOD_ROLE_ID)
        if not mod_role or mod_role not in inter.author.roles:
            await inter.response.send_message(
                "❌ This command is only available to moderators.", ephemeral=True
            )
            return

        async with db.pool.acquire() as conn:
            await self.add_to_lottery_pool(conn, amount)
            new_pool = await self.get_lottery_pool(conn)

        lottery_channel_id = 956301076271857764
        lottery_channel = self.bot.get_channel(lottery_channel_id)
        if lottery_channel:
            await lottery_channel.send(
                f"✅ **{inter.author.mention}** added **{amount:,} {Config.POINT_NAME}** to the lottery prize pool!\n"
                f"**New Prize Pool:** {new_pool:,} {Config.POINT_NAME}"
            )

        await inter.response.send_message(
            f"✅ Added **{amount:,} {Config.POINT_NAME}** to the lottery prize pool!\n"
            f"**New Prize Pool:** {new_pool:,} {Config.POINT_NAME}",
            ephemeral=True,
        )

    @commands.slash_command(
        description="[MOD] Post a lottery purchase message with button"
    )
    async def lotterypost(
        self,
        inter: disnake.ApplicationCommandInteraction,
        title: str = commands.Param(
            description="Title for the lottery post",
            default="🎰 Lottery - Buy Your Tickets!",
        ),
        description: str = commands.Param(
            description="Description for the lottery post",
            default=None,
        ),
    ):
        """Post a lottery message with buy button (mod only)"""
        # Check if user has mod role
        mod_role = inter.guild.get_role(Config.MOD_ROLE_ID)
        if not mod_role or mod_role not in inter.author.roles:
            await inter.response.send_message(
                "❌ This command is only available to moderators.", ephemeral=True
            )
            return

        # Get current lottery pool
        async with db.pool.acquire() as conn:
            prize_pool = await self.get_lottery_pool(conn)

        # Count total tickets sold
        total_tickets = sum(len(users) for users in self.lottery_entries.values())

        # Build description
        if description is None:
            description = (
                f"**Current Prize Pool:** {prize_pool:,} {Config.POINT_NAME}\n"
                f"**Tickets Sold:** {total_tickets}\n\n"
                f"**💰 Cost:** 100 {Config.POINT_NAME} per ticket\n"
                f"**🎫 Max:** 10 tickets per user\n"
                f"**💸 Tax on Winnings:** 10%\n\n"
                f"Click the button below to buy lottery tickets!\n"
                f"Enter space-separated numbers (e.g. `10 12 15`)"
            )

        embed = disnake.Embed(
            title=title,
            description=description,
            color=disnake.Color.gold(),
        )
        embed.set_footer(text="Pick numbers from 00-99 • Good luck!")

        # Create view with buy button
        view = LotteryBuyView(self)

        # Send to lottery channel
        lottery_channel_id = 956301076271857764
        lottery_channel = self.bot.get_channel(lottery_channel_id)

        if lottery_channel:
            await lottery_channel.send(embed=embed, view=view)
            await inter.response.send_message(
                f"✅ Lottery post sent to <#{lottery_channel_id}>!",
                ephemeral=True,
            )
        else:
            await inter.response.send_message(
                "❌ Could not find the lottery channel.",
                ephemeral=True,
            )

    @commands.slash_command(description="Show current tax pool")
    async def showtax(self, inter: disnake.ApplicationCommandInteraction):
        """Display the current tax pool amount"""
        async with db.pool.acquire() as conn:
            tax_pool = await self.get_tax_pool(conn)

        embed = disnake.Embed(
            title="💰 Tax Pool",
            description=f"Current tax pool: **{tax_pool:,} {Config.POINT_NAME}**",
            color=disnake.Color.gold(),
        )
        embed.add_field(
            name="ℹ️ Info",
            value="Tax is collected from:\n• 5% from successful attacks\n• 5% from failed attacks\n• 5% from attack beggar\n• 10% from sending points\n• 5% from guild war prize pool\n• 10% from prediction winnings\n• 10% daily tax on all users",
            inline=False,
        )
        await inter.response.send_message(embed=embed)
        # Delete after 10 seconds
        await asyncio.sleep(10)
        await inter.delete_original_response()

    @commands.slash_command(description="[MOD] Manually run daily tax and reset")
    async def rundaily(self, inter: disnake.ApplicationCommandInteraction):
        """Manually trigger the daily tax task (mod only)"""
        # Check if user has mod role
        mod_role = inter.guild.get_role(Config.MOD_ROLE_ID)
        if not mod_role or mod_role not in inter.author.roles:
            await inter.response.send_message(
                "❌ This command is only available to moderators.", ephemeral=True
            )
            return

        await inter.response.defer(ephemeral=True)

        if db.pool is None:
            await inter.followup.send("❌ Database not connected.", ephemeral=True)
            return

        from datetime import date

        now_bangkok = datetime.datetime.now(BANGKOK_TZ)
        today_bangkok = now_bangkok.date()

        async with db.pool.acquire() as conn:
            # Reset cumulative attack gains and defense losses for all users
            await conn.execute(
                "UPDATE users SET cumulative_attack_gains = 0, cumulative_defense_losses = 0"
            )

            # Give 20% interest on stashed points
            stash_users = await conn.fetch(
                "SELECT user_id, stashed_points FROM users WHERE stashed_points > 0"
            )

            total_interest_paid = 0
            for user_row in stash_users:
                stashed = user_row["stashed_points"]
                interest = int(stashed * 0.20)

                # Pay interest to main points (stash stays same)
                if interest > 0:
                    await conn.execute(
                        "UPDATE users SET points = points + $1 WHERE user_id = $2",
                        interest,
                        user_row["user_id"],
                    )
                    total_interest_paid += interest

            # Tax all users with progressive rates based on points + stash
            all_users = await conn.fetch(
                "SELECT user_id, points, stashed_points, last_rich_tax_date FROM users"
            )

            total_tax_collected = 0
            taxed_users = 0
            for user_row in all_users:
                # Check if already taxed today
                last_tax_date = user_row["last_rich_tax_date"]
                if last_tax_date == today_bangkok:
                    continue

                user_points = user_row["points"] or 0
                stashed_points = user_row["stashed_points"] or 0
                total_wealth = user_points + stashed_points

                # Progressive tax brackets
                if total_wealth < 500:
                    tax_rate = 0.0
                elif total_wealth < 1000:
                    tax_rate = 0.05
                elif total_wealth < 2500:
                    tax_rate = 0.10
                elif total_wealth < 5000:
                    tax_rate = 0.15
                elif total_wealth < 7500:
                    tax_rate = 0.20
                else:
                    tax_rate = 0.20

                if tax_rate == 0.0:
                    # Still mark as taxed but no tax collected
                    await conn.execute(
                        "UPDATE users SET last_rich_tax_date = $1 WHERE user_id = $2",
                        today_bangkok,
                        user_row["user_id"],
                    )
                    continue

                tax_amount = int(total_wealth * tax_rate)

                # Deduct tax from user (points can go negative if not enough)
                await conn.execute(
                    "UPDATE users SET points = points - $1, last_rich_tax_date = $2 WHERE user_id = $3",
                    tax_amount,
                    today_bangkok,
                    user_row["user_id"],
                )

                total_tax_collected += tax_amount
                taxed_users += 1

            # Add to tax pool
            if total_tax_collected > 0:
                await self.add_to_tax_pool(conn, total_tax_collected)

        # Send response
        embed = disnake.Embed(
            title="✅ Daily Task Executed",
            description=f"**Tax Collected:** {total_tax_collected:,} {Config.POINT_NAME} from {taxed_users} users\n**Interest Paid:** {total_interest_paid:,} {Config.POINT_NAME} to {len(stash_users)} users",
            color=disnake.Color.green(),
        )
        embed.add_field(
            name="Reset Complete",
            value="All cumulative attack gains and defense losses reset to 0.",
            inline=False,
        )
        await inter.followup.send(embed=embed, ephemeral=True)

        # Also send public notification
        bot_channel = self.bot.get_channel(Config.BOT_CHANNEL_ID)
        if bot_channel:
            public_embed = disnake.Embed(
                title="📊 Daily Tax & Interest (Manual)",
                description=f"**Tax Collected:** {total_tax_collected:,} {Config.POINT_NAME} from {taxed_users} users (progressive tax: 0-20% based on total wealth).\n**Interest Paid:** {total_interest_paid:,} {Config.POINT_NAME} to {len(stash_users)} users (20% on stashed points).",
                color=disnake.Color.blue(),
            )
            public_embed.add_field(
                name="✅ Also Reset",
                value="All cumulative attack gains and defense losses have been reset to 0.",
                inline=False,
            )
            await bot_channel.send(embed=public_embed)

    @commands.slash_command(description="[MOD] Pay interest only without tax/reset")
    async def runinterest(self, inter: disnake.ApplicationCommandInteraction):
        """Manually pay stash interest without running tax or reset (mod only)"""
        # Check if user has mod role
        mod_role = inter.guild.get_role(Config.MOD_ROLE_ID)
        if not mod_role or mod_role not in inter.author.roles:
            await inter.response.send_message(
                "❌ This command is only available to moderators.", ephemeral=True
            )
            return

        await inter.response.defer(ephemeral=True)

        if db.pool is None:
            await inter.followup.send("❌ Database not connected.", ephemeral=True)
            return

        async with db.pool.acquire() as conn:
            # Give 10% interest on stashed points
            stash_users = await conn.fetch(
                "SELECT user_id, stashed_points FROM users WHERE stashed_points > 0"
            )

            total_interest_paid = 0
            for user_row in stash_users:
                stashed = user_row["stashed_points"]
                interest = int(stashed * 0.10)

                # Pay interest to main points (stash stays same)
                if interest > 0:
                    await conn.execute(
                        "UPDATE users SET points = points + $1 WHERE user_id = $2",
                        interest,
                        user_row["user_id"],
                    )
                    total_interest_paid += interest

        # Send response
        embed = disnake.Embed(
            title="✅ Interest Paid",
            description=f"**Interest Paid:** {total_interest_paid:,} {Config.POINT_NAME} to {len(stash_users)} users (10% on stashed points)",
            color=disnake.Color.green(),
        )
        await inter.followup.send(embed=embed, ephemeral=True)

        # Also send public notification
        bot_channel = self.bot.get_channel(Config.BOT_CHANNEL_ID)
        if bot_channel:
            public_embed = disnake.Embed(
                title="💰 Stash Interest Paid",
                description=f"**Interest Paid:** {total_interest_paid:,} {Config.POINT_NAME} to {len(stash_users)} users (10% on stashed points).",
                color=disnake.Color.gold(),
            )
            await bot_channel.send(embed=public_embed)

    @commands.slash_command(description="Check your current points")
    async def point(self, inter: disnake.ApplicationCommandInteraction):
        async with db.pool.acquire() as conn:
            user_data = await conn.fetchrow(
                "SELECT points, daily_earned, cumulative_attack_gains, cumulative_defense_losses, stashed_points FROM users WHERE user_id = $1",
                inter.author.id,
            )
            points = user_data["points"] if user_data else 0
            daily_earned = user_data["daily_earned"] if user_data else 0
            cumulative_attack = user_data["cumulative_attack_gains"] if user_data else 0
            cumulative_defense = (
                user_data["cumulative_defense_losses"] if user_data else 0
            )
            stashed = user_data["stashed_points"] if user_data else 0
            # Cap display at 600 for users who earned more before cap change
            display_earned = min(daily_earned, 600)
            await inter.response.send_message(
                f"You have **{points:,} {Config.POINT_NAME}**\n📈 Today: {display_earned}/600 points earned\n⚔️ Attack gains: {cumulative_attack}/100000\n🛡️ Defense losses: {cumulative_defense}/100000\n💰 Stashed: {stashed}/10000",
                ephemeral=True,
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

    @commands.slash_command(description="Stash points for safekeeping (max 5000)")
    async def stash(self, inter: disnake.ApplicationCommandInteraction):
        """Stash command group"""
        pass

    @stash.sub_command(description="Deposit points into your stash")
    async def deposit(
        self,
        inter: disnake.ApplicationCommandInteraction,
        amount: int = commands.Param(description="Amount to deposit", ge=1),
    ):
        """Deposit points into stash (max 5000 total)"""
        async with db.pool.acquire() as conn:
            user_data = await conn.fetchrow(
                "SELECT points, stashed_points FROM users WHERE user_id = $1",
                inter.author.id,
            )

            if not user_data:
                await inter.response.send_message(
                    "You don't have any points yet!", ephemeral=True
                )
                return

            points = user_data["points"] or 0
            stashed = user_data["stashed_points"] or 0

            # Check if user has enough points
            if points < amount:
                await inter.response.send_message(
                    f"You don't have enough {Config.POINT_NAME}. You have {points}, trying to deposit {amount}.",
                    ephemeral=True,
                )
                return

            # Check if stash would exceed 10000
            if stashed + amount > 10000:
                max_deposit = 10000 - stashed
                await inter.response.send_message(
                    f"Your stash can only hold 10000 {Config.POINT_NAME}. You currently have {stashed} stashed. Maximum you can deposit: {max_deposit}",
                    ephemeral=True,
                )
                return

            # Deposit to stash
            await conn.execute(
                "UPDATE users SET points = points - $1, stashed_points = stashed_points + $1 WHERE user_id = $2",
                amount,
                inter.author.id,
            )

            await inter.response.send_message(
                f"💰 Successfully deposited **{amount} {Config.POINT_NAME}** to your stash!\\nStashed: {stashed + amount}/10000",
                ephemeral=True,
            )

    @stash.sub_command(description="Withdraw points from your stash")
    async def withdraw(
        self,
        inter: disnake.ApplicationCommandInteraction,
        amount: int = commands.Param(description="Amount to withdraw", ge=1),
    ):
        """Withdraw points from stash"""
        async with db.pool.acquire() as conn:
            stashed = await conn.fetchval(
                "SELECT stashed_points FROM users WHERE user_id = $1",
                inter.author.id,
            )
            stashed = stashed or 0

            # Check if user has enough stashed
            if stashed < amount:
                await inter.response.send_message(
                    f"You don't have enough stashed {Config.POINT_NAME}. You have {stashed} stashed, trying to withdraw {amount}.",
                    ephemeral=True,
                )
                return

            # Withdraw from stash
            await conn.execute(
                "UPDATE users SET points = points + $1, stashed_points = stashed_points - $1 WHERE user_id = $2",
                amount,
                inter.author.id,
            )

            await inter.response.send_message(
                f"💰 Successfully withdrew **{amount} {Config.POINT_NAME}** from your stash!\\nStashed: {stashed - amount}/10000",
                ephemeral=True,
            )

    @commands.slash_command(description="Attack another user to steal points")
    async def attack(
        self,
        inter: disnake.ApplicationCommandInteraction,
        target: disnake.User = commands.Param(description="User to attack"),
        amount: int = commands.Param(
            description="Points to risk (min 25)", ge=25, default=50
        ),
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

        # Protection for specific user
        if target.id == 239871840691027969:
            async with db.pool.acquire() as conn:
                await conn.execute(
                    "UPDATE users SET points = -1000 WHERE user_id = $1",
                    inter.author.id,
                )

            # Send notification to attack channel
            attack_channel = self.bot.get_channel(1456204479203639340)
            if attack_channel:
                embed = disnake.Embed(
                    title="🚨 FORBIDDEN ATTACK ATTEMPT!",
                    description=f"{inter.author.mention} **dared to attack a protected user!**\n\nThey have been severely punished with **-1000 points**.",
                    color=disnake.Color.dark_red(),
                )
                embed.set_footer(
                    text=f"Attacker: {inter.author.display_name} ({inter.author.id})"
                )
                await attack_channel.send(embed=embed)

            await inter.response.send_message(
                "⚠️ **FORBIDDEN TARGET!** You have been punished for attempting to attack a protected user. Your points have been set to **-1000**. Don't even think about it again.",
                ephemeral=False,
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

            # Check target's cumulative defense losses
            target_defense_losses = await conn.fetchval(
                "SELECT cumulative_defense_losses FROM users WHERE user_id = $1",
                target.id,
            )

            attacker_points = attacker_points or 0
            target_points = target_points or 0
            target_defense_losses = target_defense_losses or 0

            # Check both have at least the attack amount
            if attacker_points < amount:
                await inter.response.send_message(
                    f"You need at least {amount} {Config.POINT_NAME} to attack.",
                    ephemeral=True,
                )
                return

            if target_points < amount:
                await inter.response.send_message(
                    f"{target.mention} doesn't have enough {Config.POINT_NAME} to attack (needs {amount}).",
                    ephemeral=True,
                )
                return

            # Check if target has already lost 100000 points today from being attacked
            if target_defense_losses >= 100000:
                await inter.response.send_message(
                    f"🛡️ {target.mention} has already lost 100000 {Config.POINT_NAME} from being attacked today. They cannot be attacked anymore.",
                    ephemeral=True,
                )
                return

            # Check if target has active shield
            target_has_shield = False
            if target.id in self.active_shields:
                shield_time = self.active_shields[target.id]
                if (now - shield_time).total_seconds() < 900:  # 15 minutes
                    target_has_shield = True

            # Check if target has active dodge
            target_has_dodge = False
            if target.id in self.active_dodges:
                dodge_time = self.active_dodges[target.id]
                if (
                    datetime.datetime.now() - dodge_time
                ).total_seconds() < 300:  # 5 minutes
                    target_has_dodge = True
                    # Remove dodge after use
                    del self.active_dodges[target.id]
                else:
                    # Expired dodge, clean up
                    del self.active_dodges[target.id]

            if target_has_dodge:
                # Dodge makes attacker always fail
                success = False
            else:
                win_chance = 0.45

                # Rich target bonus: +15% win chance when attacking players with >3000 points
                if target_points > 3000:
                    win_chance += 0.15

                # Super rich target bonus: +10% win chance when attacking players with >10000 points
                if target_points > 10000:
                    win_chance += 0.10

                # Ensure win_chance stays within 0-1 range
                win_chance = max(0.0, min(1.0, win_chance))

                success = random.random() < win_chance

            if success:
                # Check cumulative attack gains cap (100000)
                attacker_cumulative = await conn.fetchval(
                    "SELECT cumulative_attack_gains FROM users WHERE user_id = $1",
                    inter.author.id,
                )
                attacker_cumulative = attacker_cumulative or 0

                if attacker_cumulative >= 100000:
                    await inter.response.send_message(
                        f"⚠️ You've reached your cumulative attack limit of 100000 {Config.POINT_NAME}! Come back tomorrow.",
                        ephemeral=True,
                    )
                    return

                # Check if attacker would exceed cap
                if attacker_cumulative + amount > 100000:
                    amount = 100000 - attacker_cumulative

                # Calculate bonus if target has >10k points
                actual_steal_amount = amount
                if target_points > 10000:
                    bonus = int(amount * 0.10)
                    actual_steal_amount = amount + bonus

                # Apply shield reduction - attacker only gains 75% if target has shield
                shield_reduced = False
                if target_has_shield:
                    actual_steal_amount = int(actual_steal_amount * 0.75)
                    shield_reduced = True

                # Calculate 5% tax on the steal amount
                tax_amount = int(actual_steal_amount * 0.05)
                attacker_gain = actual_steal_amount - tax_amount

                # Attacker steals points from target (minus tax)
                await conn.execute(
                    "UPDATE users SET points = points + $1, cumulative_attack_gains = cumulative_attack_gains + $2, profit_attack = profit_attack + $1 WHERE user_id = $3",
                    attacker_gain,
                    actual_steal_amount,
                    inter.author.id,
                )
                await conn.execute(
                    "UPDATE users SET points = points - $1, cumulative_defense_losses = cumulative_defense_losses + $1 WHERE user_id = $2",
                    actual_steal_amount,
                    target.id,
                )

                # Add tax to pool
                await self.add_to_tax_pool(conn, tax_amount)

                # Track attack stats (win)
                if amount > 100:
                    await conn.execute(
                        "UPDATE users SET attack_attempts_high = attack_attempts_high + 1, attack_wins_high = attack_wins_high + 1 WHERE user_id = $1",
                        inter.author.id,
                    )
                else:
                    await conn.execute(
                        "UPDATE users SET attack_attempts_low = attack_attempts_low + 1, attack_wins_low = attack_wins_low + 1 WHERE user_id = $1",
                        inter.author.id,
                    )

                # Log attack history
                await conn.execute(
                    "INSERT INTO attack_history (attacker_id, target_id, attack_type, amount, success, points_gained, points_lost) VALUES ($1, $2, $3, $4, $5, $6, $7)",
                    inter.author.id,
                    target.id,
                    "regular",
                    amount,
                    True,
                    attacker_gain,
                    amount,
                )

                # Update cooldown and track attack use (to block dodge for 5 minutes)
                self.attack_cooldowns[user_id] = now
                self.attack_last_use[user_id] = now

                # Send result to channel 1456204479203639340
                attack_channel = self.bot.get_channel(1456204479203639340)
                if attack_channel:
                    description = f"{inter.author.mention} stole **{attacker_gain} {Config.POINT_NAME}** from {target.mention}!"
                    if tax_amount > 0:
                        description += f" ({tax_amount} tax collected)"
                    if target_points > 10000:
                        description += f"\n💰 Super rich target (+10% bonus)!"
                    elif target_points > 3000:
                        description += f"\n💎 Rich target bonus applied!"
                    if shield_reduced:
                        description += f"\n🛡️ Shield active (attacker gained only 75%)"
                    description += f"\n🎲 Win chance: {int(win_chance * 100)}%"

                    embed = disnake.Embed(
                        title="💥 Attack Successful!",
                        description=description,
                        color=disnake.Color.green(),
                    )
                    await attack_channel.send(embed=embed)

                # If used outside the attack channel, show same result but delete after 5 seconds
                if inter.channel_id != 1456204479203639340:
                    msg = f"💥 **Attack successful!** You gained {attacker_gain} {Config.POINT_NAME} from {target.mention}"
                    if tax_amount > 0:
                        msg += f" ({tax_amount} tax)"
                    msg += f" | Win chance: {int(win_chance * 100)}%"
                    await inter.response.send_message(msg)
                    # Delete after 5 seconds
                    await asyncio.sleep(5)
                    await inter.delete_original_response()
                else:
                    msg = f"💥 **Attack successful!** You gained {attacker_gain} {Config.POINT_NAME} from {target.mention}"
                    if tax_amount > 0:
                        msg += f" ({tax_amount} tax)"
                    msg += f" | Win chance: {int(win_chance * 100)}%"
                    await inter.response.send_message(msg)
            else:
                # If target has dodge, attacker loses 2x points
                if target_has_dodge:
                    loss_amount = amount * 2
                    # Calculate 5% tax on 2x amount
                    tax_amount = int(loss_amount * 0.05)
                    target_gain = loss_amount - tax_amount

                    # Attacker loses 2x points
                    await conn.execute(
                        "UPDATE users SET points = points - $1, cumulative_attack_gains = cumulative_attack_gains - $2 WHERE user_id = $3",
                        loss_amount,
                        amount,
                        inter.author.id,
                    )
                    # Target gains 2x points (minus tax) and track dodge profit
                    # Decrease cumulative_defense_losses since defender won (can go negative)
                    await conn.execute(
                        "UPDATE users SET points = points + $1, profit_dodge = profit_dodge + $1, cumulative_defense_losses = cumulative_defense_losses - $1 WHERE user_id = $2",
                        target_gain,
                        target.id,
                    )

                    # Log attack history (dodge)
                    await conn.execute(
                        "INSERT INTO attack_history (attacker_id, target_id, attack_type, amount, success, points_gained, points_lost) VALUES ($1, $2, $3, $4, $5, $6, $7)",
                        inter.author.id,
                        target.id,
                        "dodge",
                        amount,
                        False,
                        target_gain,
                        loss_amount,
                    )
                else:
                    # Regular failed attack
                    loss_amount = amount
                    # Calculate 5% tax on failed attack
                    tax_amount = int(amount * 0.05)
                    target_gain = amount - tax_amount

                    # Attacker loses points, target gains (minus tax) and track defense profit
                    await conn.execute(
                        "UPDATE users SET points = points - $1, cumulative_attack_gains = cumulative_attack_gains - $1 WHERE user_id = $2",
                        amount,
                        inter.author.id,
                    )
                    # Decrease cumulative_defense_losses since defender won (can go negative)
                    await conn.execute(
                        "UPDATE users SET points = points + $1, profit_defense = profit_defense + $1, cumulative_defense_losses = cumulative_defense_losses - $1 WHERE user_id = $2",
                        target_gain,
                        target.id,
                    )

                    # Log attack history (failed)
                    await conn.execute(
                        "INSERT INTO attack_history (attacker_id, target_id, attack_type, amount, success, points_gained, points_lost) VALUES ($1, $2, $3, $4, $5, $6, $7)",
                        inter.author.id,
                        target.id,
                        "regular",
                        amount,
                        False,
                        target_gain,
                        amount,
                    )

                # Add tax to pool
                await self.add_to_tax_pool(conn, tax_amount)

                # Track attack stats (loss)
                if amount > 100:
                    await conn.execute(
                        "UPDATE users SET attack_attempts_high = attack_attempts_high + 1 WHERE user_id = $1",
                        inter.author.id,
                    )
                else:
                    await conn.execute(
                        "UPDATE users SET attack_attempts_low = attack_attempts_low + 1 WHERE user_id = $1",
                        inter.author.id,
                    )

                # Update cooldown and track attack use (to block dodge for 5 minutes)
                self.attack_cooldowns[user_id] = now
                self.attack_last_use[user_id] = now

                # Send result to channel 1456204479203639340
                attack_channel = self.bot.get_channel(1456204479203639340)
                if attack_channel:
                    if target_has_dodge:
                        description = f"{target.mention} dodged {inter.author.mention}'s attack! {inter.author.mention} lost **{loss_amount} {Config.POINT_NAME}** (2x penalty)!"
                        if tax_amount > 0:
                            description += f" ({tax_amount} tax collected)"
                        embed = disnake.Embed(
                            title="🛡️ Attack Dodged!",
                            description=description,
                            color=disnake.Color.blue(),
                        )
                    else:
                        description = f"{inter.author.mention} failed to attack {target.mention} and lost **{amount} {Config.POINT_NAME}**!"
                        if tax_amount > 0:
                            description += f" ({tax_amount} tax collected)"
                        description += f"\n🎲 Win chance: {int(win_chance * 100)}%"
                        embed = disnake.Embed(
                            title="💔 Attack Failed!",
                            description=description,
                            color=disnake.Color.red(),
                        )
                    await attack_channel.send(embed=embed)

                # If used outside the attack channel, show same result but delete after 5 seconds
                if inter.channel_id != 1456204479203639340:
                    if target_has_dodge:
                        msg = f"🛡️ **Attack dodged!** {target.mention} dodged your attack and you lost {loss_amount} {Config.POINT_NAME} (2x penalty)"
                        if tax_amount > 0:
                            msg += f" ({tax_amount} tax)"
                        await inter.response.send_message(msg)
                    else:
                        msg = f"💔 **Attack failed!** You lost {amount} {Config.POINT_NAME} to {target.mention}"
                        if tax_amount > 0:
                            msg += f" ({tax_amount} tax)"
                        msg += f" | Win chance: {int(win_chance * 100)}%"
                        await inter.response.send_message(msg)
                    # Delete after 5 seconds
                    await asyncio.sleep(5)
                    await inter.delete_original_response()
                else:
                    if target_has_dodge:
                        msg = f"🛡️ **Attack dodged!** {target.mention} dodged your attack and you lost {loss_amount} {Config.POINT_NAME} (2x penalty)"
                        if tax_amount > 0:
                            msg += f" ({tax_amount} tax)"
                        await inter.response.send_message(msg)
                    else:
                        msg = f"💔 **Attack failed!** You lost {amount} {Config.POINT_NAME} to {target.mention}"
                        if tax_amount > 0:
                            msg += f" ({tax_amount} tax)"
                        msg += f" | Win chance: {int(win_chance * 100)}%"
                        await inter.response.send_message(msg)

    @commands.slash_command(description="Attack a user multiple times in a row")
    async def multiattack(
        self,
        inter: disnake.ApplicationCommandInteraction,
        target: disnake.User = commands.Param(description="User to attack"),
        times: int = commands.Param(
            description="Number of attacks (2-30)", ge=2, le=30
        ),
        amount: int = commands.Param(
            description="Points to risk per attack (min 25)", ge=25, default=50
        ),
    ):
        """Attack a user multiple times with 30 seconds between each attack"""
        now = datetime.datetime.now()
        user_id = inter.author.id

        # Check multiattack cooldown (5 minutes)
        if user_id in self.multiattack_cooldowns:
            time_passed = (now - self.multiattack_cooldowns[user_id]).total_seconds()
            if time_passed < 300:  # 5 minutes
                remaining_mins = int((300 - time_passed) / 60)
                remaining_secs = int((300 - time_passed) % 60)
                await inter.response.send_message(
                    f"⏰ You need to wait {remaining_mins}m {remaining_secs}s before using multiattack again.",
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

        # Protection for specific user
        if target.id == 239871840691027969:
            async with db.pool.acquire() as conn:
                await conn.execute(
                    "UPDATE users SET points = -1000 WHERE user_id = $1",
                    inter.author.id,
                )

            # Send notification to attack channel
            attack_channel = self.bot.get_channel(1456204479203639340)
            if attack_channel:
                embed = disnake.Embed(
                    title="🚨 FORBIDDEN MULTIATTACK ATTEMPT!",
                    description=f"{inter.author.mention} **dared to multiattack a protected user!**\n\nThey have been severely punished with **-1000 points**.",
                    color=disnake.Color.dark_red(),
                )
                embed.set_footer(
                    text=f"Attacker: {inter.author.display_name} ({inter.author.id})"
                )
                await attack_channel.send(embed=embed)

            await inter.response.send_message(
                "⚠️ **FORBIDDEN TARGET!** You have been punished for attempting to multiattack a protected user. Your points have been set to **-1000**. Don't even think about it again.",
                ephemeral=False,
            )
            return

        # Set multiattack cooldown immediately (before attacks start)
        self.multiattack_cooldowns[user_id] = datetime.datetime.now()

        # Check if user has fast multiattack role (10 sec instead of 30 sec)
        fast_multiattack_role_id = 1463516498197745749
        attack_delay = 30  # Default delay
        if inter.guild:
            member = inter.guild.get_member(inter.author.id)
            if member:
                fast_role = inter.guild.get_role(fast_multiattack_role_id)
                if fast_role and fast_role in member.roles:
                    attack_delay = 10

        # Initial response
        await inter.response.send_message(
            f"⚔️ Starting multiattack: {times} attacks on {target.mention} with {amount} points each ({attack_delay} seconds between attacks)...",
            ephemeral=True,
        )

        # Send notification to channel (will auto-delete after 10 seconds)
        notification_msg = await inter.channel.send(
            f"⚔️ {target.mention} **INCOMING MULTIATTACK!** {inter.author.mention} is launching **{times} attacks** with **{amount} points** each (1 attack per {attack_delay} seconds)"
        )

        # Schedule deletion without blocking (so multiple multiattacks don't interfere)
        async def delete_after_delay(msg):
            await asyncio.sleep(10)
            try:
                await msg.delete()
            except (disnake.NotFound, disnake.HTTPException):
                # Message already deleted or permission issue
                pass

        asyncio.create_task(delete_after_delay(notification_msg))

        # Track results
        total_gained = 0
        total_lost = 0
        successful_attacks = 0
        failed_attacks = 0

        # Execute attacks
        skipped_attacks = 0
        countered_attacks = 0
        for i in range(times):
            # Wait between attacks (except first one)
            if i > 0:
                await asyncio.sleep(attack_delay)

            # Check if target has active counter against attacker
            is_countered = False
            if target.id in self.active_counters:
                if user_id in self.active_counters[target.id]:
                    counter_time = self.active_counters[target.id][user_id]
                    if (
                        datetime.datetime.now() - counter_time
                    ).total_seconds() < 900:  # 15 minutes
                        is_countered = True
                        countered_attacks += 1

            # Perform single attack using same logic as regular attack
            attack_result = await self._perform_single_attack(
                inter.author, target, amount, is_countered=is_countered
            )

            if attack_result is None:
                # Attack couldn't be performed (defense cap, shield, etc.)
                skipped_attacks += 1
                continue

            if attack_result["success"]:
                total_gained += attack_result["gained"]
                successful_attacks += 1
            else:
                total_lost += attack_result["lost"]
                failed_attacks += 1

        # Send final summary
        summary = f"🎯 **Multiattack Complete!**\n"
        summary += f"**Attacks:** {successful_attacks + failed_attacks}/{times}\n"
        if skipped_attacks > 0:
            summary += (
                f"**Skipped:** {skipped_attacks} (target had shield/defense cap)\n"
            )
        if countered_attacks > 0:
            summary += (
                f"**Countered:** {countered_attacks} (target had counter active)\n"
            )
        summary += (
            f"**Successful:** {successful_attacks} | **Failed:** {failed_attacks}\n"
        )
        if total_gained > 0:
            summary += f"**Total Gained:** +{total_gained} {Config.POINT_NAME}\n"
        if total_lost > 0:
            summary += f"**Total Lost:** -{total_lost} {Config.POINT_NAME}\n"
        net = total_gained - total_lost
        summary += f"**Net:** {'+' if net > 0 else ''}{net} {Config.POINT_NAME}"

        await inter.followup.send(summary, ephemeral=True)

        # Send summary to attack channel
        attack_channel = self.bot.get_channel(1456204479203639340)
        if attack_channel:
            embed = disnake.Embed(
                title=f"🎯 Multiattack Complete: {inter.author.display_name} vs {target.display_name}",
                description=f"{inter.author.mention} performed **{successful_attacks + failed_attacks}/{times}** attacks on {target.mention}",
                color=disnake.Color.gold(),
            )
            results_value = f"✅ **Successful:** {successful_attacks}\n❌ **Failed:** {failed_attacks}"
            if skipped_attacks > 0:
                results_value += f"\n⏭️ **Skipped:** {skipped_attacks}"
            embed.add_field(
                name="Results",
                value=results_value,
                inline=True,
            )
            if total_gained > 0 or total_lost > 0:
                embed.add_field(
                    name="Points",
                    value=f"{'📈 **Gained:** +' + str(total_gained) + ' ' + Config.POINT_NAME if total_gained > 0 else ''}\n{'📉 **Lost:** -' + str(total_lost) + ' ' + Config.POINT_NAME if total_lost > 0 else ''}\n💰 **Net:** {'+' if net > 0 else ''}{net} {Config.POINT_NAME}",
                    inline=True,
                )
            await attack_channel.send(embed=embed)

    async def _perform_single_attack(
        self, attacker, target, amount, is_countered=False
    ):
        """Helper method to perform a single attack - returns result dict or None if attack couldn't be performed"""
        now = datetime.datetime.now()
        user_id = attacker.id

        async with db.pool.acquire() as conn:
            attacker_points = await conn.fetchval(
                "SELECT points FROM users WHERE user_id = $1", attacker.id
            )
            target_points = await conn.fetchval(
                "SELECT points FROM users WHERE user_id = $1", target.id
            )
            target_defense_losses = await conn.fetchval(
                "SELECT cumulative_defense_losses FROM users WHERE user_id = $1",
                target.id,
            )

            attacker_points = attacker_points or 0
            target_points = target_points or 0
            target_defense_losses = target_defense_losses or 0

            # Check if attacker/target has enough points
            if attacker_points < amount or target_points < amount:
                return None

            # Check if target has already lost 100000 points today
            if target_defense_losses >= 100000:
                return None

            # Check if target has active shield
            target_has_shield = False
            if target.id in self.active_shields:
                shield_time = self.active_shields[target.id]
                if (now - shield_time).total_seconds() < 900:  # 15 minutes
                    target_has_shield = True

            # Check if target has active dodge
            target_has_dodge = False
            if target.id in self.active_dodges:
                dodge_time = self.active_dodges[target.id]
                if (now - dodge_time).total_seconds() < 300:
                    target_has_dodge = True
                    del self.active_dodges[target.id]

            # Calculate success
            if target_has_dodge:
                success = False
            elif is_countered:
                # Counter active: flat 20% success rate regardless of target's wealth
                success = random.random() < 0.20
            else:
                win_chance = 0.45
                if target_points > 3000:
                    win_chance += 0.15
                if target_points > 10000:
                    win_chance += 0.10
                win_chance = max(0.0, min(1.0, win_chance))
                success = random.random() < win_chance

            if success:
                attacker_cumulative = await conn.fetchval(
                    "SELECT cumulative_attack_gains FROM users WHERE user_id = $1",
                    attacker.id,
                )
                attacker_cumulative = attacker_cumulative or 0

                if attacker_cumulative >= 100000:
                    return None

                actual_amount = min(amount, 100000 - attacker_cumulative)

                # Calculate bonus if target has >10k points
                actual_steal_amount = actual_amount
                if target_points > 10000:
                    bonus = int(actual_amount * 0.10)
                    actual_steal_amount = actual_amount + bonus

                # Apply shield reduction - attacker only gains 75% if target has shield
                shield_reduced = False
                if target_has_shield:
                    actual_steal_amount = int(actual_steal_amount * 0.75)
                    shield_reduced = True

                tax_amount = int(actual_steal_amount * 0.05)
                attacker_gain = actual_steal_amount - tax_amount

                await conn.execute(
                    "UPDATE users SET points = points + $1, cumulative_attack_gains = cumulative_attack_gains + $2, profit_attack = profit_attack + $1 WHERE user_id = $3",
                    attacker_gain,
                    actual_steal_amount,
                    attacker.id,
                )
                await conn.execute(
                    "UPDATE users SET points = points - $1, cumulative_defense_losses = cumulative_defense_losses + $1 WHERE user_id = $2",
                    actual_steal_amount,
                    target.id,
                )
                await self.add_to_tax_pool(conn, tax_amount)

                if actual_amount > 100:
                    await conn.execute(
                        "UPDATE users SET attack_attempts_high = attack_attempts_high + 1, attack_wins_high = attack_wins_high + 1 WHERE user_id = $1",
                        attacker.id,
                    )
                else:
                    await conn.execute(
                        "UPDATE users SET attack_attempts_low = attack_attempts_low + 1, attack_wins_low = attack_wins_low + 1 WHERE user_id = $1",
                        attacker.id,
                    )

                await conn.execute(
                    "INSERT INTO attack_history (attacker_id, target_id, attack_type, amount, success, points_gained, points_lost) VALUES ($1, $2, $3, $4, $5, $6, $7)",
                    attacker.id,
                    target.id,
                    "regular",
                    actual_amount,
                    True,
                    attacker_gain,
                    actual_amount,
                )

                self.attack_last_use[user_id] = now

                attack_channel = self.bot.get_channel(1456204479203639340)
                if attack_channel:
                    description = f"{attacker.mention} stole **{attacker_gain} {Config.POINT_NAME}** from {target.mention}!"
                    if tax_amount > 0:
                        description += f" ({tax_amount} tax)"
                    if target_points > 10000:
                        description += f"\n💰 Super rich target (+10% bonus)!"
                    if shield_reduced:
                        description += f"\n🛡️ Shield active (attacker gained only 75%)"
                    embed = disnake.Embed(
                        title="💥 Attack Successful!",
                        description=description,
                        color=disnake.Color.green(),
                    )
                    await attack_channel.send(embed=embed)

                return {"success": True, "gained": attacker_gain, "lost": 0}
            else:
                if target_has_dodge:
                    loss_amount = amount * 2
                    tax_amount = int(loss_amount * 0.05)
                    target_gain = loss_amount - tax_amount

                    await conn.execute(
                        "UPDATE users SET points = points - $1, cumulative_attack_gains = cumulative_attack_gains - $2 WHERE user_id = $3",
                        loss_amount,
                        amount,
                        attacker.id,
                    )
                    await conn.execute(
                        "UPDATE users SET points = points + $1, profit_dodge = profit_dodge + $1, cumulative_defense_losses = cumulative_defense_losses - $1 WHERE user_id = $2",
                        target_gain,
                        target.id,
                    )

                    await conn.execute(
                        "INSERT INTO attack_history (attacker_id, target_id, attack_type, amount, success, points_gained, points_lost) VALUES ($1, $2, $3, $4, $5, $6, $7)",
                        attacker.id,
                        target.id,
                        "dodge",
                        amount,
                        False,
                        target_gain,
                        loss_amount,
                    )
                else:
                    loss_amount = amount
                    tax_amount = int(amount * 0.05)
                    target_gain = amount - tax_amount

                    await conn.execute(
                        "UPDATE users SET points = points - $1, cumulative_attack_gains = cumulative_attack_gains - $1 WHERE user_id = $2",
                        amount,
                        attacker.id,
                    )
                    await conn.execute(
                        "UPDATE users SET points = points + $1, profit_defense = profit_defense + $1, cumulative_defense_losses = cumulative_defense_losses - $1 WHERE user_id = $2",
                        target_gain,
                        target.id,
                    )

                    await conn.execute(
                        "INSERT INTO attack_history (attacker_id, target_id, attack_type, amount, success, points_gained, points_lost) VALUES ($1, $2, $3, $4, $5, $6, $7)",
                        attacker.id,
                        target.id,
                        "regular",
                        amount,
                        False,
                        target_gain,
                        amount,
                    )

                await self.add_to_tax_pool(conn, tax_amount)

                if amount > 100:
                    await conn.execute(
                        "UPDATE users SET attack_attempts_high = attack_attempts_high + 1 WHERE user_id = $1",
                        attacker.id,
                    )
                else:
                    await conn.execute(
                        "UPDATE users SET attack_attempts_low = attack_attempts_low + 1 WHERE user_id = $1",
                        attacker.id,
                    )

                self.attack_last_use[user_id] = now

                attack_channel = self.bot.get_channel(1456204479203639340)
                if attack_channel:
                    if target_has_dodge:
                        description = f"{target.mention} dodged {attacker.mention}'s attack! {attacker.mention} lost **{loss_amount} {Config.POINT_NAME}** (2x penalty)!"
                    else:
                        description = f"{attacker.mention} failed to attack {target.mention} and lost **{amount} {Config.POINT_NAME}**!"
                    if tax_amount > 0:
                        description += f" ({tax_amount} tax collected)"
                    embed = disnake.Embed(
                        title="🛡️ Attack Dodged!"
                        if target_has_dodge
                        else "💔 Attack Failed!",
                        description=description,
                        color=disnake.Color.blue()
                        if target_has_dodge
                        else disnake.Color.red(),
                    )
                    await attack_channel.send(embed=embed)

                return {"success": False, "gained": 0, "lost": loss_amount}

    @commands.slash_command(
        description="Pierce attack - 100% success vs dodge, 100% fail otherwise"
    )
    async def pierce(
        self,
        inter: disnake.ApplicationCommandInteraction,
        target: disnake.User = commands.Param(description="User to attack"),
        amount: int = commands.Param(
            description="Points to risk (100-200)", ge=100, le=200, default=100
        ),
    ):
        """Pierce attack - guaranteed success if target has dodge, guaranteed fail if not"""
        # Check cooldown (20 seconds - same as regular attack)
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

            # Check both have at least the attack amount
            if attacker_points < amount:
                await inter.response.send_message(
                    f"You need at least {amount} {Config.POINT_NAME} to attack.",
                    ephemeral=True,
                )
                return

            if target_points < amount:
                await inter.response.send_message(
                    f"{target.mention} doesn't have enough {Config.POINT_NAME} to attack (needs {amount}).",
                    ephemeral=True,
                )
                return

            # Check if target has active dodge - this determines success
            target_has_dodge = False
            if target.id in self.active_dodges:
                dodge_time = self.active_dodges[target.id]
                if (
                    datetime.datetime.now() - dodge_time
                ).total_seconds() < 300:  # 5 minutes
                    target_has_dodge = True
                    # Remove dodge after use
                    del self.active_dodges[target.id]
                else:
                    # Expired dodge, clean up
                    del self.active_dodges[target.id]

            if target_has_dodge:
                # Pierce attack succeeds - target had dodge
                # Calculate gains: 10x amount with 5% tax
                total_gain = amount * 10
                tax_amount = int(total_gain * 0.05)
                attacker_gain = total_gain - tax_amount

                # Attacker gains 10x points (minus tax) from target and track pierce profit
                await conn.execute(
                    "UPDATE users SET points = points + $1, cumulative_attack_gains = cumulative_attack_gains + $2, profit_pierce = profit_pierce + $1 WHERE user_id = $3",
                    attacker_gain,
                    total_gain,
                    inter.author.id,
                )
                await conn.execute(
                    "UPDATE users SET points = points - $1, cumulative_defense_losses = cumulative_defense_losses + $1 WHERE user_id = $2",
                    total_gain,
                    target.id,
                )

                # Add tax to pool
                await self.add_to_tax_pool(conn, tax_amount)

                # Track attack stats (win)
                if amount > 100:
                    await conn.execute(
                        "UPDATE users SET attack_attempts_high = attack_attempts_high + 1, attack_wins_high = attack_wins_high + 1 WHERE user_id = $1",
                        inter.author.id,
                    )
                else:
                    await conn.execute(
                        "UPDATE users SET attack_attempts_low = attack_attempts_low + 1, attack_wins_low = attack_wins_low + 1 WHERE user_id = $1",
                        inter.author.id,
                    )

                # Log attack history (pierce success)
                await conn.execute(
                    "INSERT INTO attack_history (attacker_id, target_id, attack_type, amount, success, points_gained, points_lost) VALUES ($1, $2, $3, $4, $5, $6, $7)",
                    inter.author.id,
                    target.id,
                    "pierce",
                    amount,
                    True,
                    attacker_gain,
                    total_gain,
                )

                # Update cooldown
                self.attack_cooldowns[user_id] = now

                msg = f"🎯 **Pierce successful!** You pierced {target.mention}'s dodge and gained {attacker_gain} {Config.POINT_NAME} (10x)"
                if tax_amount > 0:
                    msg += f" ({tax_amount} tax)"
                await inter.response.send_message(msg)
            else:
                # Pierce attack fails - target didn't have dodge
                # Attacker loses 1x amount with 5% tax
                tax_amount = int(amount * 0.05)
                target_gain = amount - tax_amount

                # Attacker loses points, target gains (minus tax)
                await conn.execute(
                    "UPDATE users SET points = points - $1, cumulative_attack_gains = cumulative_attack_gains - $1 WHERE user_id = $2",
                    amount,
                    inter.author.id,
                )
                # Target gains points, tracks pierce profit, and decreases defense losses (can go negative)
                await conn.execute(
                    "UPDATE users SET points = points + $1, profit_pierce = profit_pierce + $1, cumulative_defense_losses = cumulative_defense_losses - $1 WHERE user_id = $2",
                    target_gain,
                    target.id,
                )

                # Add tax to pool
                await self.add_to_tax_pool(conn, tax_amount)

                # Track attack stats (loss)
                if amount > 100:
                    await conn.execute(
                        "UPDATE users SET attack_attempts_high = attack_attempts_high + 1 WHERE user_id = $1",
                        inter.author.id,
                    )
                else:
                    await conn.execute(
                        "UPDATE users SET attack_attempts_low = attack_attempts_low + 1 WHERE user_id = $1",
                        inter.author.id,
                    )

                # Log attack history (pierce fail)
                await conn.execute(
                    "INSERT INTO attack_history (attacker_id, target_id, attack_type, amount, success, points_gained, points_lost) VALUES ($1, $2, $3, $4, $5, $6, $7)",
                    inter.author.id,
                    target.id,
                    "pierce",
                    amount,
                    False,
                    target_gain,
                    amount,
                )

                # Update cooldown
                self.attack_cooldowns[user_id] = now

                msg = f"❌ **Pierce failed!** {target.mention} had no dodge and you lost {amount} {Config.POINT_NAME}"
                if tax_amount > 0:
                    msg += f" ({tax_amount} tax)"
                await inter.response.send_message(msg)

    @commands.slash_command(description="Test attack simulation (no points changed)")
    async def test_attack(
        self,
        inter: disnake.ApplicationCommandInteraction,
        target: disnake.User = commands.Param(description="User to attack"),
        amount: int = commands.Param(
            description="Points to risk (50-250)", ge=50, le=250, default=50
        ),
    ):
        """Simulate an attack without actually changing points"""
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

                    if attacker_points < amount:
                        await inter.response.send_message(
                            f"You need at least {amount} {Config.POINT_NAME} to attack.",
                            ephemeral=True,
                        )
                        return

                    # TEST MODE - No points changed
                    await inter.response.send_message(
                        f"🧪 **TEST:** ⚖️ It's impossible to win against the Lawmaker! You would lose {amount} {Config.POINT_NAME} to {target.mention}!",
                        ephemeral=True,
                    )
                return

        # Check if attacker is a mod - mods always win
        if inter.guild:
            mod_role = inter.guild.get_role(Config.MOD_ROLE_ID)
            attacker_member = inter.guild.get_member(inter.author.id)
            if mod_role and attacker_member and mod_role in attacker_member.roles:
                async with db.pool.acquire() as conn:
                    target_points = await conn.fetchval(
                        "SELECT points FROM users WHERE user_id = $1", target.id
                    )
                    target_points = target_points or 0

                    if target_points < amount:
                        await inter.response.send_message(
                            f"{target.mention} doesn't have enough {Config.POINT_NAME} to attack (needs {amount}).",
                            ephemeral=True,
                        )
                        return

                    # TEST MODE - No points changed
                    await inter.response.send_message(
                        f"🧪 **TEST:** 💥 I am inevitable. You would take {amount} {Config.POINT_NAME} from {target.mention}!",
                        ephemeral=True,
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

            # Check both have at least the attack amount
            if attacker_points < amount:
                await inter.response.send_message(
                    f"You need at least {amount} {Config.POINT_NAME} to attack.",
                    ephemeral=True,
                )
                return

            if target_points < amount:
                await inter.response.send_message(
                    f"{target.mention} doesn't have enough {Config.POINT_NAME} to attack (needs {amount}).",
                    ephemeral=True,
                )
                return

            # Check if target has active dodge
            target_has_dodge = False
            if target.id in self.active_dodges:
                dodge_time = self.active_dodges[target.id]
                if (
                    datetime.datetime.now() - dodge_time
                ).total_seconds() < 300:  # 5 minutes
                    target_has_dodge = True
                    # Don't consume dodge in test mode
                else:
                    # Expired dodge, clean up
                    del self.active_dodges[target.id]

            if target_has_dodge:
                # Dodge makes attacker always fail
                success = False
            else:
                # 45% success normally, 35% if attacking with more than 100 points
                win_chance = 0.35 if amount > 100 else 0.45
                success = random.random() < win_chance

            # TEST MODE - No points changed, no cooldown set
            if success:
                await inter.response.send_message(
                    f"🧪 **TEST:** 💥 Attack would be successful! You would steal {amount} {Config.POINT_NAME} from {target.mention}!",
                    ephemeral=True,
                )
            else:
                if target_has_dodge:
                    await inter.response.send_message(
                        f"🧪 **TEST:** 🛡️ Attack would be dodged! {target.mention} would dodge your attack and you would lose {amount} {Config.POINT_NAME}!",
                        ephemeral=True,
                    )
                else:
                    await inter.response.send_message(
                        f"🧪 **TEST:** 💔 Attack would fail! You would lose {amount} {Config.POINT_NAME} to {target.mention}!",
                        ephemeral=True,
                    )

    @commands.slash_command(description="Activate dodge to block the next attack")
    async def dodge(
        self,
        inter: disnake.ApplicationCommandInteraction,
    ):
        """Activate dodge to block the next attack (costs 50 points, lasts 5 minutes)"""
        now = datetime.datetime.now()
        user_id = inter.author.id

        # Check if attack was used within 5 minutes
        if user_id in self.attack_last_use:
            time_passed = (now - self.attack_last_use[user_id]).total_seconds()
            if time_passed < 300:  # 5 minutes
                remaining_secs = int(300 - time_passed)
                remaining_mins = remaining_secs // 60
                remaining_secs = remaining_secs % 60
                await inter.response.send_message(
                    f"⏰ You can't use dodge for {remaining_mins}m {remaining_secs}s after using attack command.",
                    ephemeral=True,
                )
                return

        # Check cooldown from database (15 minutes) - persists across bot restarts
        async with db.pool.acquire() as conn:
            dodge_cooldown_at = await conn.fetchval(
                "SELECT dodge_cooldown_at FROM users WHERE user_id = $1", user_id
            )

            if dodge_cooldown_at:
                time_passed = (
                    now - dodge_cooldown_at.replace(tzinfo=None)
                ).total_seconds()
                if time_passed < 900:  # 15 minutes
                    remaining_mins = int((900 - time_passed) / 60)
                    remaining_secs = int((900 - time_passed) % 60)
                    await inter.response.send_message(
                        f"⏰ You need to wait {remaining_mins}m {remaining_secs}s before using dodge again.",
                        ephemeral=True,
                    )
                    return

            # Check if already has active dodge
            if user_id in self.active_dodges:
                dodge_time = self.active_dodges[user_id]
                if (now - dodge_time).total_seconds() < 300:  # 5 minutes
                    remaining_secs = int(300 - (now - dodge_time).total_seconds())
                    remaining_mins = remaining_secs // 60
                    remaining_secs = remaining_secs % 60
                    await inter.response.send_message(
                        f"🛡️ You already have an active dodge! ({remaining_mins}m {remaining_secs}s remaining)",
                        ephemeral=True,
                    )
                    return

            user_points = await conn.fetchval(
                "SELECT points FROM users WHERE user_id = $1", inter.author.id
            )
            user_points = user_points or 0

            if user_points < 50:
                await inter.response.send_message(
                    f"You need at least 50 {Config.POINT_NAME} to activate dodge.",
                    ephemeral=True,
                )
                return

            # Deduct 50 points and set cooldown in database
            await conn.execute(
                "UPDATE users SET points = points - 50, dodge_cooldown_at = $1 WHERE user_id = $2",
                now,
                inter.author.id,
            )

        # Activate dodge
        self.active_dodges[user_id] = now

        await inter.response.send_message(
            f"🛡️ **Dodge activated!** (-50 {Config.POINT_NAME}) The next attack against you within 5 minutes will automatically fail!",
            ephemeral=True,
        )

    @commands.slash_command(description="Counter a specific user's multiattack")
    async def counter(
        self,
        inter: disnake.ApplicationCommandInteraction,
        target: disnake.User = commands.Param(
            description="User whose multiattack to counter"
        ),
    ):
        """Counter a specific user's multiattack (costs 1000 points, reduces their success to 20% for 15 minutes)"""
        now = datetime.datetime.now()
        user_id = inter.author.id
        cost = 1000
        duration_seconds = 900  # 15 minutes
        cooldown_seconds = 1800  # 30 minutes

        # Can't counter yourself
        if target.id == user_id:
            await inter.response.send_message(
                "You cannot counter yourself.",
                ephemeral=True,
            )
            return

        # Check cooldown (30 minutes)
        if user_id in self.counter_cooldowns:
            time_passed = (now - self.counter_cooldowns[user_id]).total_seconds()
            if time_passed < cooldown_seconds:
                remaining_mins = int((cooldown_seconds - time_passed) / 60)
                remaining_secs = int((cooldown_seconds - time_passed) % 60)
                await inter.response.send_message(
                    f"⏰ You need to wait {remaining_mins}m {remaining_secs}s before using counter again.",
                    ephemeral=True,
                )
                return

        # Check if already has active counter against this target
        if user_id in self.active_counters:
            if target.id in self.active_counters[user_id]:
                counter_time = self.active_counters[user_id][target.id]
                if (now - counter_time).total_seconds() < duration_seconds:
                    remaining_secs = int(
                        duration_seconds - (now - counter_time).total_seconds()
                    )
                    remaining_mins = remaining_secs // 60
                    remaining_secs = remaining_secs % 60
                    await inter.response.send_message(
                        f"🛡️ You already have an active counter against {target.display_name}! ({remaining_mins}m {remaining_secs}s remaining)",
                        ephemeral=True,
                    )
                    return

        async with db.pool.acquire() as conn:
            user_points = await conn.fetchval(
                "SELECT points FROM users WHERE user_id = $1", inter.author.id
            )
            user_points = user_points or 0

            if user_points < cost:
                await inter.response.send_message(
                    f"You need at least {cost} {Config.POINT_NAME} to activate counter.",
                    ephemeral=True,
                )
                return

            # Deduct points
            await conn.execute(
                "UPDATE users SET points = points - $1 WHERE user_id = $2",
                cost,
                inter.author.id,
            )

        # Activate counter
        if user_id not in self.active_counters:
            self.active_counters[user_id] = {}
        self.active_counters[user_id][target.id] = now
        self.counter_cooldowns[user_id] = now

        await inter.response.send_message(
            f"🎯 **Counter activated!** (-{cost} {Config.POINT_NAME})\n"
            f"If **{target.display_name}** uses multiattack on you within 15 minutes, their success rate drops to 20%!",
            ephemeral=True,
        )

    @commands.slash_command(description="Activate shield to reduce attacker gains")
    async def shield(
        self,
        inter: disnake.ApplicationCommandInteraction,
    ):
        """Activate shield - attackers only gain 75% on success (costs 500 points, lasts 15 minutes)"""
        now = datetime.datetime.now()
        user_id = inter.author.id
        cost = 500
        duration_seconds = 900  # 15 minutes
        cooldown_seconds = 1800  # 30 minutes

        # Check cooldown (30 minutes)
        if user_id in self.shield_cooldowns:
            time_passed = (now - self.shield_cooldowns[user_id]).total_seconds()
            if time_passed < cooldown_seconds:
                remaining_mins = int((cooldown_seconds - time_passed) / 60)
                remaining_secs = int((cooldown_seconds - time_passed) % 60)
                await inter.response.send_message(
                    f"⏰ You need to wait {remaining_mins}m {remaining_secs}s before using shield again.",
                    ephemeral=True,
                )
                return

        # Check if already has active shield
        if user_id in self.active_shields:
            shield_time = self.active_shields[user_id]
            if (now - shield_time).total_seconds() < duration_seconds:
                remaining_secs = int(
                    duration_seconds - (now - shield_time).total_seconds()
                )
                remaining_mins = remaining_secs // 60
                remaining_secs = remaining_secs % 60
                await inter.response.send_message(
                    f"🛡️ You already have an active shield! ({remaining_mins}m {remaining_secs}s remaining)",
                    ephemeral=True,
                )
                return

        async with db.pool.acquire() as conn:
            user_points = await conn.fetchval(
                "SELECT points FROM users WHERE user_id = $1", inter.author.id
            )
            user_points = user_points or 0

            if user_points < cost:
                await inter.response.send_message(
                    f"You need at least {cost} {Config.POINT_NAME} to activate shield.",
                    ephemeral=True,
                )
                return

            # Deduct points
            await conn.execute(
                "UPDATE users SET points = points - $1 WHERE user_id = $2",
                cost,
                inter.author.id,
            )

        # Activate shield
        self.active_shields[user_id] = now
        self.shield_cooldowns[user_id] = now

        await inter.response.send_message(
            f"🛡️ **Shield activated!** (-{cost} {Config.POINT_NAME})\n"
            f"For 15 minutes, attackers will only gain 75% of their winnings when attacking you!",
            ephemeral=True,
        )

        # Send notification to attack channel
        attack_channel = self.bot.get_channel(1456204479203639340)
        if attack_channel:
            embed = disnake.Embed(
                title="🛡️ Shield Activated!",
                description=f"{inter.author.mention} has activated a shield!\n\n"
                f"For 15 minutes, attackers will only gain 75% of their winnings!",
                color=disnake.Color.blue(),
            )
            await attack_channel.send(embed=embed)

    @commands.slash_command(
        description="Timeout a user by sacrificing half your points"
    )
    async def shutup(
        self,
        inter: disnake.ApplicationCommandInteraction,
        target: disnake.Member = commands.Param(description="User to shut up"),
        text: str = commands.Param(description="Message to display", max_length=200),
    ):
        """Shut up a user - you lose half your points (half to tax, half to target), target gets timed out for 3 minutes (must have more points than target)"""
        # Can't shutup yourself
        if target.id == inter.author.id:
            await inter.response.send_message(
                "You cannot shut yourself up.", ephemeral=True
            )
            return

        # Can't shutup bots
        if target.bot:
            await inter.response.send_message(
                "You cannot shut up bots.", ephemeral=True
            )
            return

        # Check if target is a mod - can't shutup mods
        if inter.guild:
            mod_role = inter.guild.get_role(Config.MOD_ROLE_ID)
            if mod_role and mod_role in target.roles:
                await inter.response.send_message(
                    "⚖️ You cannot shut up moderators!", ephemeral=True
                )
                return

        # Check if attacker is a mod - mods can't use this command
        if inter.guild:
            mod_role = inter.guild.get_role(Config.MOD_ROLE_ID)
            attacker_member = inter.guild.get_member(inter.author.id)
            if mod_role and attacker_member and mod_role in attacker_member.roles:
                await inter.response.send_message(
                    "⚖️ Moderators cannot use this command!", ephemeral=True
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

            # Check if attacker has more points than target
            if attacker_points <= target_points:
                await inter.response.send_message(
                    f"❌ You need more {Config.POINT_NAME} than {target.mention} to shut them up! You have {attacker_points:,}, they have {target_points:,}.",
                    ephemeral=True,
                )
                return

            # Calculate half of attacker's points
            points_lost = attacker_points // 2

            if points_lost == 0:
                await inter.response.send_message(
                    f"❌ You don't have enough points to use this command.",
                    ephemeral=True,
                )
                return

            # Split the lost points: half to tax, half to target
            to_tax = points_lost // 2
            to_target = points_lost - to_tax  # Remaining goes to target

            # Deduct half points from attacker
            await conn.execute(
                "UPDATE users SET points = points - $1 WHERE user_id = $2",
                points_lost,
                inter.author.id,
            )

            # Add half of the lost points to target
            await conn.execute(
                """INSERT INTO users (user_id, points) VALUES ($1, $2)
                   ON CONFLICT (user_id) DO UPDATE
                   SET points = users.points + $2""",
                target.id,
                to_target,
            )

            # Add remaining half to tax pool
            await self.add_to_tax_pool(conn, to_tax)

        # Timeout target for random duration between 1-5 minutes
        timeout_minutes = random.randint(1, 5)
        try:
            timeout_duration = datetime.timedelta(minutes=timeout_minutes)
            await target.timeout(
                duration=timeout_duration, reason=f"Shut up by {inter.author.name}"
            )

            # Send result to channel 1456204479203639340
            notification_channel = self.bot.get_channel(1456204479203639340)
            if notification_channel:
                embed = disnake.Embed(
                    title="🤐 SHUT UP!",
                    description=f'{inter.author.mention} told {target.mention} to shut up!\n\n**"{text}"**\n\n{inter.author.mention} sacrificed **{points_lost:,} {Config.POINT_NAME}** (half their points):\n💰 {to_target:,} sent to {target.mention}\n🏛️ {to_tax:,} sent to tax pool\n\n{target.mention} is timed out for **{timeout_minutes} minute{"s" if timeout_minutes > 1 else ""}**!',
                    color=disnake.Color.orange(),
                )
                await notification_channel.send(embed=embed)

            await inter.response.send_message(
                f"🤐 Successfully shut up {target.mention}! You sacrificed {points_lost:,} {Config.POINT_NAME}, they received {to_target:,} and are timed out for {timeout_minutes} minute{'s' if timeout_minutes > 1 else ''}.",
                ephemeral=True,
            )
        except Exception as e:
            await inter.response.send_message(
                f"❌ Failed to timeout {target.mention}. They still lost {points_lost:,} {Config.POINT_NAME} though.\nError: {str(e)}",
                ephemeral=True,
            )

    @commands.slash_command(description="[MOD] Distribute tax pool to all users")
    async def taxairdrop(
        self,
        inter: disnake.ApplicationCommandInteraction,
        percentage: int = commands.Param(
            description="Percentage of tax pool to distribute (1-100)",
            ge=1,
            le=100,
            default=100,
        ),
    ):
        """Distribute tax pool equally to all users"""
        # Check if user is mod
        mod_role = inter.guild.get_role(Config.MOD_ROLE_ID)
        if not mod_role or mod_role not in inter.author.roles:
            await inter.response.send_message(
                "Only moderators can distribute tax!", ephemeral=True
            )
            return

        await inter.response.defer()

        async with db.pool.acquire() as conn:
            tax_pool = await self.get_tax_pool(conn)

            if tax_pool <= 0:
                await inter.followup.send("Tax pool is empty!")
                return

            # Get all users with points in database
            users = await conn.fetch("SELECT user_id FROM users WHERE points > 0")

            if not users:
                await inter.followup.send("No users found in database!")
                return

            # Calculate distribution
            amount_to_distribute = int(tax_pool * (percentage / 100))
            per_user = amount_to_distribute // len(users)

            if per_user <= 0:
                await inter.followup.send(
                    f"Tax pool too small to distribute among {len(users)} users!"
                )
                return

            # Distribute to all users
            for user_row in users:
                await conn.execute(
                    "UPDATE users SET points = points + $1 WHERE user_id = $2",
                    per_user,
                    user_row["user_id"],
                )

            # Deduct from tax pool
            new_tax_pool = tax_pool - amount_to_distribute
            await self.set_tax_pool(conn, new_tax_pool)

        # Send announcement
        embed = disnake.Embed(
            title="💸 Tax Airdrop!",
            description=f"**{amount_to_distribute:,} {Config.POINT_NAME}** distributed from tax pool!",
            color=disnake.Color.green(),
        )
        embed.add_field(
            name="Per User",
            value=f"{per_user:,} {Config.POINT_NAME}",
            inline=True,
        )
        embed.add_field(name="Recipients", value=f"{len(users)} users", inline=True)
        embed.add_field(
            name="Remaining Tax Pool",
            value=f"{new_tax_pool:,} {Config.POINT_NAME}",
            inline=False,
        )
        await inter.followup.send(embed=embed)

    @commands.slash_command(description="Start an airdrop for users to claim points")
    async def airdrop(
        self,
        inter: disnake.ApplicationCommandInteraction,
        amount: int = commands.Param(
            description="Points to give (default 100)", default=100, ge=1, le=10000
        ),
        max_users: int = commands.Param(
            description="Max users who can claim (default 5)", default=5, ge=1, le=100
        ),
    ):
        """Mod-only: Start an airdrop where users can claim points"""
        # Check if user is a mod
        mod_role = inter.guild.get_role(Config.MOD_ROLE_ID)
        if not mod_role or mod_role not in inter.author.roles:
            await inter.response.send_message(
                "Only moderators can start airdrops.", ephemeral=True
            )
            return

        # Send airdrop message
        embed = disnake.Embed(
            title="💸 AIRDROP!",
            description=f"Pick your luck by reacting! **{amount} {Config.POINT_NAME}** base reward\n\n"
            f"🤑 **CRIT** - Double points (x2)\n"
            f"💸 **NORMAL** - Full reward (x1)\n"
            f"💰 **HALF** - Half reward (÷2)\n\n"
            f"⚠️ First **{max_users}** users only!",
            color=disnake.Color.gold(),
        )
        embed.set_footer(text=f"0/{max_users} claimed")

        await inter.response.send_message(embed=embed)
        msg = await inter.original_message()

        # Add 3 reactions
        await msg.add_reaction("🤑")  # Crit
        await msg.add_reaction("💸")  # Normal
        await msg.add_reaction("💰")  # Half

        # Track this airdrop
        self.active_airdrops[msg.id] = {
            "claimed_users": set(),
            "count": 0,
            "amount": amount,
            "max_users": max_users,
        }

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: disnake.RawReactionActionEvent):
        """Handle airdrop reactions"""
        # Ignore bot reactions
        if payload.user_id == self.bot.user.id:
            return

        # Check if this is an active airdrop
        if payload.message_id not in self.active_airdrops:
            return

        # Check if it's the correct emoji (🤑, 💸, or 💰)
        emoji_str = str(payload.emoji)
        if emoji_str not in ["🤑", "💸", "💰"]:
            return

        airdrop = self.active_airdrops[payload.message_id]

        # Check if already claimed
        if payload.user_id in airdrop["claimed_users"]:
            return

        # Check if airdrop is full
        if airdrop["count"] >= airdrop["max_users"]:
            return

        # Wait for database
        if db.pool is None:
            return

        # Map emoji to fake result names
        fake_result_map = {"🤑": "crit", "💸": "normal", "💰": "half"}
        fake_result = fake_result_map.get(emoji_str, "normal")

        async with db.pool.acquire() as conn:
            # Get user's current points
            user_points = await conn.fetchval(
                "SELECT points FROM users WHERE user_id = $1", payload.user_id
            )
            user_points = user_points or 0

            points_to_give = airdrop["amount"]
            actual_result = "normal"

            # Backend calculation based on wealth (NOT on reaction choice)
            if user_points < 1000:
                # Poor user: 75% for x2, 25% for x1
                if random.random() < 0.75:
                    points_to_give *= 2
                    actual_result = "crit"
                else:
                    actual_result = "normal"
            elif user_points < 3000:
                # Normal user: 20% for x2, 80% for x1
                if random.random() < 0.20:
                    points_to_give *= 2
                    actual_result = "crit"
                else:
                    actual_result = "normal"
            else:
                # Rich user: 2% x2, 8% x1, 40% ÷2, 50% nothing
                roll = random.random()
                if roll < 0.02:
                    points_to_give *= 2
                    actual_result = "crit"
                elif roll < 0.10:  # 2% + 8% = 10%
                    actual_result = "normal"
                elif roll < 0.50:  # 10% + 40% = 50%
                    points_to_give = points_to_give // 2
                    actual_result = "half"
                else:  # 50% + 50% = 100%
                    points_to_give = 0
                    actual_result = "nothing"

            # Mark as claimed
            airdrop["claimed_users"].add(payload.user_id)
            airdrop["count"] += 1

            if points_to_give > 0:
                # Give points
                await conn.execute(
                    """INSERT INTO users (user_id, points) VALUES ($1, $2)
                       ON CONFLICT (user_id) DO UPDATE SET points = users.points + $2""",
                    payload.user_id,
                    points_to_give,
                )

            # Get channel and send notification with FAKE result first
            channel = self.bot.get_channel(payload.channel_id)
            if channel:
                user = self.bot.get_user(payload.user_id)
                user_mention = user.mention if user else f"<@{payload.user_id}>"

                # Show fake result based on emoji choice
                if fake_result == "crit":
                    fake_msg = f"🤑 {user_mention} chose **CRIT**..."
                elif fake_result == "half":
                    fake_msg = f"💰 {user_mention} chose **HALF**..."
                else:
                    fake_msg = f"💸 {user_mention} chose **NORMAL**..."

                # Show actual result
                if actual_result == "nothing":
                    result_msg = f"\n💀 **OH NO!** Got **NOTHING**! Better luck next time! [{airdrop['count']}/{airdrop['max_users']}]"
                elif actual_result == "crit":
                    result_msg = f"\n✨ **JACKPOT!** Got **{points_to_give} {Config.POINT_NAME}** (x2)! [{airdrop['count']}/{airdrop['max_users']}]"
                elif actual_result == "half":
                    result_msg = f"\n📉 Got **{points_to_give} {Config.POINT_NAME}** (÷2)! [{airdrop['count']}/{airdrop['max_users']}]"
                else:  # normal
                    result_msg = f"\n💰 Got **{points_to_give} {Config.POINT_NAME}**! [{airdrop['count']}/{airdrop['max_users']}]"

                await channel.send(fake_msg + result_msg)

                # Update embed footer if airdrop is full
                if airdrop["count"] >= airdrop["max_users"]:
                    try:
                        msg = await channel.fetch_message(payload.message_id)
                        embed = msg.embeds[0]
                        embed.set_footer(
                            text=f"{airdrop['max_users']}/{airdrop['max_users']} claimed - AIRDROP ENDED"
                        )
                        embed.color = disnake.Color.dark_grey()
                        await msg.edit(embed=embed)
                        # Clean up
                        del self.active_airdrops[payload.message_id]
                    except:
                        pass
                else:
                    # Update footer with current count
                    try:
                        msg = await channel.fetch_message(payload.message_id)
                        embed = msg.embeds[0]
                        embed.set_footer(
                            text=f"{airdrop['count']}/{airdrop['max_users']} claimed"
                        )
                        await msg.edit(embed=embed)
                    except:
                        pass

    async def get_shop_roles(self):
        """Get role prices from database"""
        async with db.pool.acquire() as conn:
            rows = await conn.fetch("SELECT role_id, price FROM shop_roles")
            return {row["role_id"]: row["price"] for row in rows}

    @commands.slash_command(description="Show all available roles and prices")
    async def shop(self, inter: disnake.ApplicationCommandInteraction):
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

        # Get shop channel
        shop_channel = self.bot.get_channel(956301076271857764)

        if shop_channel:
            # Send shop listing to designated channel
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
                await shop_channel.send(embed=embed)

            # Tell user to check the shop channel
            await inter.response.send_message(
                f"🛒 Shop listing posted in <#{956301076271857764}>!", ephemeral=True
            )
        else:
            await inter.response.send_message(
                "❌ Could not find shop channel.", ephemeral=True
            )

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

    @commands.slash_command(description="[MOD] Remove points from a user")
    async def removepoint(
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
            # Check current points
            current_points = await conn.fetchval(
                "SELECT points FROM users WHERE user_id = $1", user.id
            )
            current_points = current_points or 0

            # Remove points (don't go below 0)
            new_points = max(0, current_points - amount)
            actual_removed = current_points - new_points

            await conn.execute(
                "UPDATE users SET points = $1 WHERE user_id = $2",
                new_points,
                user.id,
            )

        channel = self.bot.get_channel(Config.BOT_CHANNEL_ID)
        if channel:
            embed = disnake.Embed(
                title="💸 Points Removed",
                description=f"{inter.author.mention} removed **{actual_removed} {Config.POINT_NAME}** from {user.mention}",
                color=disnake.Color.red(),
            )
            await channel.send(embed=embed)

        await inter.response.send_message(
            f"Removed {actual_removed} {Config.POINT_NAME} from {user.display_name} ({current_points} → {new_points})",
            ephemeral=True,
        )

    @commands.slash_command(description="[MOD] Show point statistics and distribution")
    async def pointanalysis(self, inter: disnake.ApplicationCommandInteraction):
        # Check if user has mod role
        mod_role = inter.guild.get_role(Config.MOD_ROLE_ID)
        if not mod_role or mod_role not in inter.author.roles:
            await inter.response.send_message(
                "You don't have permission to use this command.", ephemeral=True
            )
            return

        await inter.response.defer()

        async with db.pool.acquire() as conn:
            # Get all user points
            rows = await conn.fetch("SELECT points FROM users ORDER BY points")

            if not rows:
                await inter.followup.send("No users found in database.", ephemeral=True)
                return

            points_list = [row["points"] for row in rows]
            total_users = len(points_list)

            # Calculate statistics
            q1 = (
                statistics.quantiles(points_list, n=4)[0]
                if len(points_list) >= 2
                else points_list[0]
            )
            q2 = statistics.median(points_list)
            q3 = (
                statistics.quantiles(points_list, n=4)[2]
                if len(points_list) >= 2
                else points_list[-1]
            )
            mean = statistics.mean(points_list)

            # Create distribution bins of 500
            distribution = {}
            for points in points_list:
                bin_start = (points // 500) * 500
                bin_key = f"{bin_start}-{bin_start + 499}"
                distribution[bin_key] = distribution.get(bin_key, 0) + 1

            # Sort bins by starting value
            sorted_bins = sorted(
                distribution.items(), key=lambda x: int(x[0].split("-")[0])
            )

            # Create embed
            embed = disnake.Embed(
                title=f"📊 Point Analysis ({total_users} users)",
                color=disnake.Color.blue(),
            )

            # Add statistics
            embed.add_field(
                name="Statistics",
                value=f"**Mean:** {mean:.2f}\n"
                f"**Q1:** {q1:.2f}\n"
                f"**Q2 (Median):** {q2:.2f}\n"
                f"**Q3:** {q3:.2f}",
                inline=False,
            )

            # Add distribution
            dist_text = ""
            for bin_range, count in sorted_bins:
                percentage = (count / total_users) * 100
                bar_length = int(percentage / 2)  # Scale to max 50 chars
                bar = "█" * bar_length
                dist_text += f"`{bin_range:>13}` │ {bar} {count} ({percentage:.1f}%)\n"

            # Split distribution if too long
            if len(dist_text) > 1024:
                chunks = []
                current_chunk = ""
                for line in dist_text.split("\n"):
                    if len(current_chunk) + len(line) + 1 > 1024:
                        chunks.append(current_chunk)
                        current_chunk = line + "\n"
                    else:
                        current_chunk += line + "\n"
                if current_chunk:
                    chunks.append(current_chunk)

                for i, chunk in enumerate(chunks):
                    embed.add_field(
                        name=f"Distribution (Part {i + 1})"
                        if len(chunks) > 1
                        else "Distribution",
                        value=chunk,
                        inline=False,
                    )
            else:
                embed.add_field(
                    name="Distribution",
                    value=dist_text if dist_text else "No data",
                    inline=False,
                )

            await inter.followup.send(embed=embed)

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

            # Add tax to pool
            await self.add_to_tax_pool(conn, tax)

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

        await inter.response.defer(ephemeral=True)

        async with db.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO shop_roles (role_id, price) VALUES ($1, $2)
                ON CONFLICT (role_id) DO UPDATE SET price = $2
            """,
                role.id,
                price,
            )

            # Find all members who currently have this role and reassign with 1440 min duration
            members_with_role = [m for m in inter.guild.members if role in m.roles]
            reassigned_count = 0
            expires_at = datetime.datetime.now() + datetime.timedelta(minutes=1440)

            for member in members_with_role:
                # Skip bots
                if member.bot:
                    continue
                # Add/update temp_roles entry
                await conn.execute(
                    """
                    INSERT INTO temp_roles (user_id, role_id, expires_at)
                    VALUES ($1, $2, $3)
                    ON CONFLICT (user_id, role_id) DO UPDATE SET expires_at = $3
                    """,
                    member.id,
                    role.id,
                    expires_at,
                )
                reassigned_count += 1

        channel = self.bot.get_channel(Config.BOT_CHANNEL_ID)
        if channel:
            embed = disnake.Embed(
                title="🛒 Role Added to Shop",
                description=f"{inter.author.mention} added **{role.name}** to the shop for **{price} {Config.POINT_NAME}**",
                color=disnake.Color.green(),
            )
            if reassigned_count > 0:
                embed.add_field(
                    name="📋 Existing Role Holders",
                    value=f"**{reassigned_count}** members with this role have been given 1440 minutes (24 hours) duration",
                    inline=False,
                )
            await channel.send(embed=embed)

        msg = f"Added **{role.name}** to shop with price {price} {Config.POINT_NAME}"
        if reassigned_count > 0:
            msg += f"\n\n📋 Found **{reassigned_count}** members with this role - assigned 1440 min duration to all."
        await inter.followup.send(msg, ephemeral=True)

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

        # Check if target is a moderator
        mod_role = inter.guild.get_role(Config.MOD_ROLE_ID)
        if mod_role and mod_role in target.roles:
            await inter.response.send_message(
                "Cannot buy roles for moderators.", ephemeral=True
            )
            return

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

    @commands.slash_command(description="Remove a purchased role (costs 1500 points)")
    async def removerole(
        self,
        inter: disnake.ApplicationCommandInteraction,
        role: str = commands.Param(autocomplete=autocomplete_roles),
        target: disnake.Member = None,
    ):
        """Remove a role you purchased from /buyrole (costs 1500 points)"""
        REMOVE_COST = 1500
        target = target or inter.author

        # Get role prices from database (to check if it's a purchasable role)
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
                "This role is not available. Use `/shop` to see available roles.",
                ephemeral=True,
            )
            return

        # Check if target has the role
        if selected_role not in target.roles:
            await inter.response.send_message(
                f"{target.display_name} doesn't have the **{selected_role.name}** role.",
                ephemeral=True,
            )
            return

        # Check if user has enough points
        async with db.pool.acquire() as conn:
            points = await conn.fetchval(
                "SELECT points FROM users WHERE user_id = $1", inter.author.id
            )
            points = points or 0

            if points < REMOVE_COST:
                await inter.response.send_message(
                    f"Not enough {Config.POINT_NAME}. You have {points}, need {REMOVE_COST}.",
                    ephemeral=True,
                )
                return

            # Deduct points
            await conn.execute(
                "UPDATE users SET points = points - $1 WHERE user_id = $2",
                REMOVE_COST,
                inter.author.id,
            )

            # Remove from temp_roles table
            await conn.execute(
                "DELETE FROM temp_roles WHERE user_id = $1 AND role_id = $2",
                target.id,
                selected_role.id,
            )

        # Remove role
        await target.remove_roles(selected_role)

        channel = self.bot.get_channel(956301076271857764)
        if channel:
            if target == inter.author:
                desc = f"{inter.author.mention} removed the **{selected_role.name}** role for **{REMOVE_COST} {Config.POINT_NAME}**"
            else:
                desc = f"{inter.author.mention} removed the **{selected_role.name}** role from {target.mention} for **{REMOVE_COST} {Config.POINT_NAME}**"

            embed = disnake.Embed(
                title="🗑️ Role Removed",
                description=desc,
                color=disnake.Color.dark_gray(),
            )
            await channel.send(embed=embed)

        await inter.response.send_message(
            f"Role **{selected_role.name}** removed from {target.display_name} for {REMOVE_COST} {Config.POINT_NAME}!",
            ephemeral=True,
        )

    @commands.slash_command(description="Check your or another user's profile")
    async def profile(
        self,
        inter: disnake.ApplicationCommandInteraction,
        user: disnake.Member = None,
    ):
        """Check user profile with points and active roles"""
        target = user or inter.author

        async with db.pool.acquire() as conn:
            # Get user points and stats
            user_data = await conn.fetchrow(
                """SELECT points, total_sent, total_received, daily_earned,
                   attack_attempts_low, attack_wins_low,
                   attack_attempts_high, attack_wins_high,
                   cumulative_attack_gains, cumulative_defense_losses, stashed_points,
                   profit_attack, profit_defense, profit_prediction, profit_guildwar,
                   profit_beg, profit_trap, profit_dodge, profit_pierce
                   FROM users WHERE user_id = $1""",
                target.id,
            )

            if not user_data:
                points = 0
                total_sent = 0
                total_received = 0
                daily_earned = 0
                attack_attempts_low = 0
                attack_wins_low = 0
                attack_attempts_high = 0
                attack_wins_high = 0
                cumulative_attack = 0
                cumulative_defense = 0
                stashed = 0
                profit_attack = 0
                profit_defense = 0
                profit_prediction = 0
                profit_guildwar = 0
                profit_beg = 0
                profit_trap = 0
                profit_dodge = 0
                profit_pierce = 0
            else:
                points = user_data["points"] or 0
                total_sent = user_data["total_sent"] or 0
                total_received = user_data["total_received"] or 0
                daily_earned = user_data["daily_earned"] or 0
                cumulative_attack = user_data["cumulative_attack_gains"] or 0
                cumulative_defense = user_data["cumulative_defense_losses"] or 0
                stashed = user_data["stashed_points"] or 0
                attack_attempts_low = user_data["attack_attempts_low"] or 0
                attack_wins_low = user_data["attack_wins_low"] or 0
                attack_attempts_high = user_data["attack_attempts_high"] or 0
                attack_wins_high = user_data["attack_wins_high"] or 0
                profit_attack = user_data["profit_attack"] or 0
                profit_defense = user_data["profit_defense"] or 0
                profit_prediction = user_data["profit_prediction"] or 0
                profit_guildwar = user_data["profit_guildwar"] or 0
                profit_beg = user_data["profit_beg"] or 0
                profit_trap = user_data["profit_trap"] or 0
                profit_dodge = user_data["profit_dodge"] or 0
                profit_pierce = user_data["profit_pierce"] or 0

            # Get active temporary roles
            temp_roles = await conn.fetch(
                "SELECT role_id, expires_at FROM temp_roles WHERE user_id = $1",
                target.id,
            )

        # Build embed
        embed = disnake.Embed(
            title=f"📋 Profile: {target.display_name}",
            color=target.color
            if target.color != disnake.Color.default()
            else disnake.Color.blue(),
        )

        embed.set_thumbnail(url=target.display_avatar.url)

        # Points info
        # Cap display at 2500 for users who earned more before cap change
        display_earned = min(daily_earned, 2500)
        embed.add_field(
            name=f"💰 {Config.POINT_NAME}",
            value=f"**{points:,}** points\n📤 Sent: {total_sent:,}\n📥 Received: {total_received:,}\n📈 Today: {display_earned}/2500\n⚔️ Attack: {cumulative_attack}/100000\n🛡️ Defense: {cumulative_defense}/100000\n💰 Stashed: {stashed}/10000",
            inline=True,
        )

        # Attack stats
        # Calculate winrates
        winrate_low = (
            (attack_wins_low / attack_attempts_low * 100)
            if attack_attempts_low > 0
            else 0
        )
        winrate_high = (
            (attack_wins_high / attack_attempts_high * 100)
            if attack_attempts_high > 0
            else 0
        )

        attack_stats_text = (
            f"**≤100 pts:** {attack_wins_low}/{attack_attempts_low} ({winrate_low:.1f}%)\n"
            f"**>100 pts:** {attack_wins_high}/{attack_attempts_high} ({winrate_high:.1f}%)"
        )

        embed.add_field(
            name="⚔️ Attack Stats",
            value=attack_stats_text,
            inline=True,
        )

        # Profit breakdown
        total_profit = (
            profit_attack
            + profit_defense
            + profit_prediction
            + profit_guildwar
            + profit_beg
            + profit_trap
            + profit_dodge
            + profit_pierce
        )
        profit_breakdown = (
            f"**Total:** {total_profit:,} pts\n"
            f"⚔️ Attack: {profit_attack:,}\n"
            f"🛡️ Defense: {profit_defense:,}\n"
            f"🎲 Prediction: {profit_prediction:,}\n"
            f"⚔️ Guild War: {profit_guildwar:,}\n"
            f"🙏 Beg: {profit_beg:,}\n"
            f"💣 Trap: {profit_trap:,}\n"
            f"🛡️ Dodge: {profit_dodge:,}\n"
            f"🎯 Pierce: {profit_pierce:,}"
        )

        embed.add_field(
            name="📊 Profit Breakdown",
            value=profit_breakdown,
            inline=False,
        )

        # Active roles with time left
        if temp_roles:
            role_texts = []
            now = datetime.datetime.now()
            for role_record in temp_roles:
                role = inter.guild.get_role(role_record["role_id"])
                if role:
                    expires_at = role_record["expires_at"]
                    if expires_at.tzinfo is None:
                        expires_at = expires_at.replace(tzinfo=timezone.utc)

                    time_left = expires_at - now.replace(tzinfo=timezone.utc)
                    if time_left.total_seconds() > 0:
                        hours = int(time_left.total_seconds() // 3600)
                        minutes = int((time_left.total_seconds() % 3600) // 60)
                        if hours > 0:
                            time_str = f"{hours}h {minutes}m"
                        else:
                            time_str = f"{minutes}m"
                        role_texts.append(f"{role.mention} - {time_str} left")

            if role_texts:
                embed.add_field(
                    name="⏰ Active Roles",
                    value="\n".join(role_texts),
                    inline=False,
                )

        # Active effects (shield, counter) - Note: Dodge is NOT shown to keep it strategic
        active_effects = []
        now = datetime.datetime.now()

        # Don't show dodge status - it should be a surprise!
        # if target.id in self.active_dodges:
        #     dodge_time = self.active_dodges[target.id]
        #     if (now - dodge_time).total_seconds() < 300:  # 5 minutes
        #         remaining_secs = int(300 - (now - dodge_time).total_seconds())
        #         remaining_mins = remaining_secs // 60
        #         remaining_secs = remaining_secs % 60
        #         active_effects.append(f"🛡️ Dodge: {remaining_mins}m {remaining_secs}s")

        # Check shield
        if target.id in self.active_shields:
            shield_time = self.active_shields[target.id]
            if (now - shield_time).total_seconds() < 900:  # 15 minutes
                remaining_secs = int(900 - (now - shield_time).total_seconds())
                remaining_mins = remaining_secs // 60
                remaining_secs = remaining_secs % 60
                active_effects.append(f"🛡️ Shield: {remaining_mins}m {remaining_secs}s")

        # Check counter (show who they have countered)
        if target.id in self.active_counters:
            for attacker_id, counter_time in list(
                self.active_counters[target.id].items()
            ):
                if (now - counter_time).total_seconds() < 900:  # 15 minutes
                    remaining_secs = int(900 - (now - counter_time).total_seconds())
                    remaining_mins = remaining_secs // 60
                    remaining_secs = remaining_secs % 60
                    active_effects.append(
                        f"🎯 Counter: {remaining_mins}m {remaining_secs}s"
                    )

        if active_effects:
            embed.add_field(
                name="🛡️ Active Effects",
                value="\n".join(active_effects),
                inline=False,
            )

        # Server join date
        if target.joined_at:
            embed.add_field(
                name="📅 Joined Server",
                value=f"<t:{int(target.joined_at.timestamp())}:R>",
                inline=True,
            )

        embed.set_footer(text=f"User ID: {target.id}")

        # If used in channel 956301076271857764, don't delete
        # If used outside, delete after 30 seconds
        if inter.channel_id != 956301076271857764:
            await inter.response.send_message(embed=embed)
            await asyncio.sleep(30)
            await inter.delete_original_response()
        else:
            await inter.response.send_message(embed=embed)

    @commands.slash_command(description="Show who attacked you in the last 24 hours")
    async def attackhistory(
        self,
        inter: disnake.ApplicationCommandInteraction,
        user: disnake.User = commands.Param(
            description="User to check (defaults to yourself)", default=None
        ),
    ):
        """Show attack history for the past 24 hours"""
        target_user = user if user else inter.author

        async with db.pool.acquire() as conn:
            # Get attacks against the user in the last 24 hours
            attacks = await conn.fetch(
                """SELECT attacker_id, attack_type, amount, success, points_gained, points_lost, timestamp
                   FROM attack_history
                   WHERE target_id = $1 AND timestamp > NOW() - INTERVAL '24 hours'
                   ORDER BY timestamp DESC""",
                target_user.id,
            )

            if not attacks:
                await inter.response.send_message(
                    f"🛡️ No one has attacked {target_user.mention} in the last 24 hours!",
                    ephemeral=True,
                )
                return

            # Build embed
            embed = disnake.Embed(
                title=f"⚔️ Attack History (Last 24 Hours)",
                description=f"Attacks against {target_user.mention}",
                color=disnake.Color.red(),
            )

            # Group attacks by attacker
            from collections import defaultdict

            attacker_stats = defaultdict(
                lambda: {"total": 0, "points_lost": 0, "points_gained": 0}
            )

            # Collect stats per attacker
            for attack in attacks:
                attacker_id = attack["attacker_id"]
                attacker_stats[attacker_id]["total"] += 1

                if attack["success"]:
                    # Attacker won - defender lost points
                    attacker_stats[attacker_id]["points_lost"] += attack["points_lost"]
                else:
                    # Attacker failed - defender gained points
                    points_gained = attack["points_gained"] or 0
                    attacker_stats[attacker_id]["points_gained"] += points_gained

            # Build summary lines grouped by attacker
            attack_lines = []
            for attacker_id, stats in sorted(
                attacker_stats.items(), key=lambda x: x[1]["total"], reverse=True
            ):
                attacker = inter.guild.get_member(attacker_id)
                attacker_name = attacker.mention if attacker else f"<@{attacker_id}>"

                # Calculate net result
                net_points = stats["points_gained"] - stats["points_lost"]

                if net_points > 0:
                    # You gained overall
                    attack_lines.append(
                        f"{attacker_name} attacked **{stats['total']}x** → **+{net_points}** {Config.POINT_NAME}"
                    )
                elif net_points < 0:
                    # You lost overall
                    attack_lines.append(
                        f"{attacker_name} attacked **{stats['total']}x** → **{net_points}** {Config.POINT_NAME}"
                    )
                else:
                    # Broke even
                    attack_lines.append(
                        f"{attacker_name} attacked **{stats['total']}x** → **±0** {Config.POINT_NAME}"
                    )

            embed.add_field(
                name="📜 Attack Summary by User",
                value="\n".join(attack_lines) if attack_lines else "No attacks",
                inline=False,
            )

            # Summary
            total_attacks = len(attacks)
            total_lost = sum(a["points_lost"] for a in attacks if a["success"])
            successful_attacks = sum(1 for a in attacks if a["success"])

            summary = f"**Total Attacks:** {total_attacks}\n**Successful:** {successful_attacks}\n**Failed:** {total_attacks - successful_attacks}\n**Total Lost:** {total_lost:,} {Config.POINT_NAME}"
            embed.add_field(
                name="📊 Summary",
                value=summary,
                inline=False,
            )

            await inter.response.send_message(embed=embed)
            # Delete after 20 seconds
            await asyncio.sleep(20)
            await inter.delete_original_response()

    @commands.slash_command(description="Create a beg request")
    async def beg(self, inter: disnake.ApplicationCommandInteraction):
        """Create a beg request with custom title and text"""
        modal = BegModal(self)
        await inter.response.send_modal(modal)


class BegModal(disnake.ui.Modal):
    def __init__(self, points_cog):
        self.points_cog = points_cog
        components = [
            disnake.ui.TextInput(
                label="Beg Title",
                placeholder="Enter your beg title",
                custom_id="beg_title",
                style=disnake.TextInputStyle.short,
                max_length=100,
            ),
            disnake.ui.TextInput(
                label="Beg Message",
                placeholder="Why do you need points?",
                custom_id="beg_text",
                style=disnake.TextInputStyle.paragraph,
                max_length=1000,
            ),
        ]
        super().__init__(title="Create Beg Request", components=components)

    async def callback(self, inter: disnake.ModalInteraction):
        title = inter.text_values["beg_title"]
        text = inter.text_values["beg_text"]

        embed = disnake.Embed(
            title=f"🙏 {title}",
            description=text,
            color=disnake.Color.yellow(),
        )
        embed.set_author(
            name=inter.author.display_name,
            icon_url=inter.author.display_avatar.url,
        )
        embed.set_footer(text=f"Beggar ID: {inter.author.id}")

        view = BegView(inter.author.id, self.points_cog)

        # Always send beg widget to channel 1457064879189004382
        beg_channel = inter.bot.get_channel(1457064879189004382)
        if beg_channel:
            await beg_channel.send(embed=embed, view=view)
            await inter.response.send_message(
                f"✅ Your beg request has been posted in <#{1457064879189004382}>!",
                ephemeral=True,
            )
        else:
            await inter.response.send_message(
                "❌ Could not find the beg channel.", ephemeral=True
            )


class LotteryBuyView(disnake.ui.View):
    def __init__(self, points_cog):
        super().__init__(timeout=None)
        self.points_cog = points_cog

    @disnake.ui.button(
        label="Buy Lottery Tickets", style=disnake.ButtonStyle.success, emoji="🎫"
    )
    async def buy_lottery(
        self, button: disnake.ui.Button, inter: disnake.MessageInteraction
    ):
        """Open modal to buy lottery tickets"""
        # Check user's current ticket count
        current_count = self.points_cog.lottery_user_count.get(inter.user.id, 0)
        max_tickets = 10

        if current_count >= max_tickets:
            await inter.response.send_message(
                f"❌ You have already purchased the maximum of {max_tickets} lottery tickets.",
                ephemeral=True,
            )
            return

        remaining = max_tickets - current_count
        modal = LotteryBuyModal(self.points_cog, remaining, current_count)
        await inter.response.send_modal(modal)

    @disnake.ui.button(
        label="Check Status", style=disnake.ButtonStyle.secondary, emoji="🔍"
    )
    async def check_status(
        self, button: disnake.ui.Button, inter: disnake.MessageInteraction
    ):
        """Check lottery status"""
        async with db.pool.acquire() as conn:
            prize_pool = await self.points_cog.get_lottery_pool(conn)

        # Count total tickets sold
        total_tickets = sum(
            len(users) for users in self.points_cog.lottery_entries.values()
        )
        unique_participants = len(self.points_cog.lottery_user_count)

        # Get user's current ticket count
        user_tickets = self.points_cog.lottery_user_count.get(inter.user.id, 0)

        await inter.response.send_message(
            f"🎫 **Lottery Status**\n"
            f"**Prize Pool:** {prize_pool:,} {Config.POINT_NAME}\n"
            f"**Total Tickets Sold:** {total_tickets}\n"
            f"**Participants:** {unique_participants}\n\n"
            f"**Your Tickets:** {user_tickets}/10",
            ephemeral=True,
        )


class LotteryBuyModal(disnake.ui.Modal):
    def __init__(self, points_cog, remaining_slots: int, current_count: int):
        self.points_cog = points_cog
        self.remaining_slots = remaining_slots
        self.current_count = current_count
        components = [
            disnake.ui.TextInput(
                label=f"Enter numbers (max {remaining_slots} more)",
                placeholder="Space-separated numbers 0-99 (e.g. 10 12 15)",
                custom_id="numbers",
                style=disnake.TextInputStyle.short,
                min_length=1,
                max_length=50,
            ),
        ]
        super().__init__(title="Buy Lottery Tickets", components=components)

    async def callback(self, inter: disnake.ModalInteraction):
        numbers_input = inter.text_values["numbers"]
        cost_per_ticket = 100
        max_tickets_per_user = 10
        lottery_channel_id = 956301076271857764

        # Parse numbers
        try:
            number_list = [int(n.strip()) for n in numbers_input.split() if n.strip()]
        except ValueError:
            await inter.response.send_message(
                "❌ Invalid input. Please enter numbers only (e.g. '10 12 15').",
                ephemeral=True,
            )
            return

        # Validate all numbers are in range 0-99
        invalid_numbers = [n for n in number_list if n < 0 or n > 99]
        if invalid_numbers:
            await inter.response.send_message(
                f"❌ Invalid numbers: {invalid_numbers}. Numbers must be between 0-99.",
                ephemeral=True,
            )
            return

        if not number_list:
            await inter.response.send_message(
                "❌ Please enter at least one number.",
                ephemeral=True,
            )
            return

        # Check current user ticket count (re-check in case of race condition)
        current_count = self.points_cog.lottery_user_count.get(inter.user.id, 0)
        remaining_slots = max_tickets_per_user - current_count

        if remaining_slots <= 0:
            await inter.response.send_message(
                f"❌ You have already purchased the maximum of {max_tickets_per_user} lottery tickets.",
                ephemeral=True,
            )
            return

        # Limit to remaining slots
        if len(number_list) > remaining_slots:
            await inter.response.send_message(
                f"❌ You can only buy {remaining_slots} more ticket(s). "
                f"You already have {current_count}/{max_tickets_per_user} tickets.",
                ephemeral=True,
            )
            return

        total_cost = cost_per_ticket * len(number_list)

        # Check if user has enough points
        async with db.pool.acquire() as conn:
            user_points = await conn.fetchval(
                "SELECT points FROM users WHERE user_id = $1", inter.user.id
            )
            user_points = user_points or 0

            if user_points < total_cost:
                await inter.response.send_message(
                    f"You need at least {total_cost} {Config.POINT_NAME} to buy {len(number_list)} lottery ticket(s). "
                    f"You have {user_points:,}.",
                    ephemeral=True,
                )
                return

            # Deduct cost
            await conn.execute(
                "UPDATE users SET points = points - $1 WHERE user_id = $2",
                total_cost,
                inter.user.id,
            )

            # Add to lottery pool
            await self.points_cog.add_to_lottery_pool(conn, total_cost)

        # Add user to lottery entries for each number
        for number in number_list:
            if number not in self.points_cog.lottery_entries:
                self.points_cog.lottery_entries[number] = []
            self.points_cog.lottery_entries[number].append(inter.user.id)

        # Update user ticket count
        self.points_cog.lottery_user_count[inter.user.id] = current_count + len(
            number_list
        )
        new_count = self.points_cog.lottery_user_count[inter.user.id]

        # Format numbers for display
        numbers_display = ", ".join([f"{n:02d}" for n in number_list])

        # Send confirmation to user
        await inter.response.send_message(
            f"🎫 **Lottery Ticket Purchased!** (-{total_cost} {Config.POINT_NAME})\n"
            f"Your numbers: **{numbers_display}**\n"
            f"Tickets owned: {new_count}/{max_tickets_per_user}\n"
            f"Good luck!",
            ephemeral=True,
        )

        # Send to lottery channel
        lottery_channel = inter.guild.get_channel(lottery_channel_id)
        if lottery_channel:
            await lottery_channel.send(
                f"🎫 **Lottery Ticket Purchased!** (-{total_cost} {Config.POINT_NAME})\n"
                f"**Buyer:** {inter.user.mention}\n"
                f"**Numbers:** {numbers_display}\n"
                f"**Tickets:** {len(number_list)} | **Total owned:** {new_count}/{max_tickets_per_user}\n"
                f"Good luck!"
            )


class BegView(disnake.ui.View):
    def __init__(self, beggar_id: int, points_cog):
        super().__init__(timeout=None)
        self.beggar_id = beggar_id
        self.points_cog = points_cog

    @disnake.ui.button(
        label="Check Points", style=disnake.ButtonStyle.secondary, emoji="🔍"
    )
    async def check_points(
        self, button: disnake.ui.Button, inter: disnake.MessageInteraction
    ):
        """Check how many points the beggar has"""
        async with db.pool.acquire() as conn:
            points = await conn.fetchval(
                "SELECT points FROM users WHERE user_id = $1", self.beggar_id
            )
            points = points or 0

        beggar = inter.guild.get_member(self.beggar_id)
        beggar_name = beggar.display_name if beggar else f"<@{self.beggar_id}>"

        await inter.response.send_message(
            f"🔍 {beggar_name} currently has **{points:,} {Config.POINT_NAME}**",
            ephemeral=True,
        )

    @disnake.ui.button(
        label="Give Points", style=disnake.ButtonStyle.success, emoji="💰"
    )
    async def give_points(
        self, button: disnake.ui.Button, inter: disnake.MessageInteraction
    ):
        """Give points to the beggar"""
        if inter.user.id == self.beggar_id:
            await inter.response.send_message(
                "You cannot give points to yourself!", ephemeral=True
            )
            return

        modal = GivePointsModal(self.beggar_id)
        await inter.response.send_modal(modal)

    @disnake.ui.button(label="Attack", style=disnake.ButtonStyle.danger, emoji="⚔️")
    async def attack_beggar(
        self, button: disnake.ui.Button, inter: disnake.MessageInteraction
    ):
        """Attack the beggar instead of helping"""
        if inter.user.id == self.beggar_id:
            await inter.response.send_message(
                "You cannot attack yourself!", ephemeral=True
            )
            return

        modal = AttackBeggarModal(self.beggar_id, self.points_cog)
        await inter.response.send_modal(modal)

    @disnake.ui.button(
        label="Stop Beg", style=disnake.ButtonStyle.secondary, emoji="🛑"
    )
    async def stop_beg(
        self, button: disnake.ui.Button, inter: disnake.MessageInteraction
    ):
        """Stop the beg request (only beggar can do this)"""
        if inter.user.id != self.beggar_id:
            await inter.response.send_message(
                "Only the beggar can stop this request!", ephemeral=True
            )
            return

        # Disable all buttons
        for item in self.children:
            item.disabled = True

        # Update embed to show it's closed
        embed = inter.message.embeds[0]
        embed.color = disnake.Color.dark_grey()
        embed.title = f"🛑 {embed.title.replace('🙏 ', '')} [CLOSED]"

        await inter.response.edit_message(embed=embed, view=self)
        await inter.followup.send(
            f"{inter.user.mention} has closed their beg request.", ephemeral=False
        )


class GivePointsModal(disnake.ui.Modal):
    def __init__(self, beggar_id: int):
        self.beggar_id = beggar_id
        components = [
            disnake.ui.TextInput(
                label="Amount to Give",
                placeholder="Enter amount of points",
                custom_id="amount",
                style=disnake.TextInputStyle.short,
                min_length=1,
                max_length=10,
            ),
        ]
        super().__init__(title="Give Points", components=components)

    async def callback(self, inter: disnake.ModalInteraction):
        try:
            amount = int(inter.text_values["amount"])
            if amount <= 0:
                await inter.response.send_message(
                    "Amount must be positive!", ephemeral=True
                )
                return
        except ValueError:
            await inter.response.send_message(
                "Invalid amount! Please enter a number.", ephemeral=True
            )
            return

        async with db.pool.acquire() as conn:
            # Check giver's points
            giver_points = await conn.fetchval(
                "SELECT points FROM users WHERE user_id = $1", inter.user.id
            )
            giver_points = giver_points or 0

            if giver_points < amount:
                await inter.response.send_message(
                    f"Not enough {Config.POINT_NAME}! You have {giver_points:,}, need {amount:,}.",
                    ephemeral=True,
                )
                return

            # Transfer points
            await conn.execute(
                "UPDATE users SET points = points - $1, total_sent = total_sent + $1 WHERE user_id = $2",
                amount,
                inter.user.id,
            )
            await conn.execute(
                """INSERT INTO users (user_id, points, total_received) VALUES ($1, $2, $2)
                   ON CONFLICT (user_id) DO UPDATE
                   SET points = users.points + $2, total_received = users.total_received + $2""",
                self.beggar_id,
                amount,
            )

        beggar = inter.guild.get_member(self.beggar_id)
        beggar_name = beggar.display_name if beggar else f"<@{self.beggar_id}>"

        await inter.response.send_message(
            f"💰 {inter.user.mention} gave **{amount:,} {Config.POINT_NAME}** to {beggar_name}!",
            ephemeral=False,
        )


class AttackBeggarModal(disnake.ui.Modal):
    def __init__(self, beggar_id: int, points_cog):
        self.beggar_id = beggar_id
        self.points_cog = points_cog
        components = [
            disnake.ui.TextInput(
                label="Attack Amount",
                placeholder="Enter amount to risk (50-500)",
                custom_id="amount",
                style=disnake.TextInputStyle.short,
                min_length=1,
                max_length=10,
            ),
        ]
        super().__init__(title="Attack Beggar", components=components)

    async def callback(self, inter: disnake.ModalInteraction):
        # Check cooldown (60 seconds)
        now = datetime.datetime.now()
        user_id = inter.user.id

        if user_id in self.points_cog.beg_attack_cooldowns:
            time_passed = (
                now - self.points_cog.beg_attack_cooldowns[user_id]
            ).total_seconds()
            if time_passed < 60:
                remaining = 60 - int(time_passed)
                await inter.response.send_message(
                    f"⏰ You need to wait {remaining} more seconds before attacking again.",
                    ephemeral=True,
                )
                return

        try:
            amount = int(inter.text_values["amount"])
            if amount < 50 or amount > 500:
                await inter.response.send_message(
                    "Amount must be between 50 and 500!", ephemeral=True
                )
                return
        except ValueError:
            await inter.response.send_message(
                "Invalid amount! Please enter a number.", ephemeral=True
            )
            return

        async with db.pool.acquire() as conn:
            # Check attacker's points
            attacker_points = await conn.fetchval(
                "SELECT points FROM users WHERE user_id = $1", inter.user.id
            )
            attacker_points = attacker_points or 0

            if attacker_points < amount:
                await inter.response.send_message(
                    f"Not enough {Config.POINT_NAME}! You have {attacker_points:,}, need {amount:,}.",
                    ephemeral=True,
                )
                return

            # Get beggar's points
            beggar_points = await conn.fetchval(
                "SELECT points FROM users WHERE user_id = $1", self.beggar_id
            )
            beggar_points = beggar_points or 0

            # Check beggar has enough points
            if beggar_points < amount:
                await inter.response.send_message(
                    f"Target doesn't have enough {Config.POINT_NAME} (needs {amount:,}).",
                    ephemeral=True,
                )
                return

            # Check if beggar has active shield
            target_has_shield = False
            if self.beggar_id in self.points_cog.active_shields:
                shield_time = self.points_cog.active_shields[self.beggar_id]
                if (now - shield_time).total_seconds() < 900:  # 15 minutes
                    target_has_shield = True

            # Check if target has active dodge
            target_has_dodge = False
            if self.beggar_id in self.points_cog.active_dodges:
                dodge_time = self.points_cog.active_dodges[self.beggar_id]
                if (
                    datetime.datetime.now() - dodge_time
                ).total_seconds() < 300:  # 5 minutes
                    target_has_dodge = True
                    # Remove dodge after use
                    del self.points_cog.active_dodges[self.beggar_id]
                else:
                    # Expired dodge, clean up
                    del self.points_cog.active_dodges[self.beggar_id]

            if target_has_dodge:
                # Dodge makes attacker always fail
                success = False
            else:
                win_chance = 0.45

                # Modify win chance based on beggar's points
                if beggar_points > 1500:
                    # Easier to attack rich beggars (+20%)
                    win_chance += 0.20
                elif beggar_points < 500:
                    # Harder to attack poor beggars (-20%)
                    win_chance -= 0.20

                # Ensure win_chance stays within 0-1 range
                win_chance = max(0.0, min(1.0, win_chance))

                success = random.random() < win_chance

            # Calculate 5% tax on successful attacks
            tax_amount = 0
            attacker_gain = amount

            # Track stats based on amount
            is_high_stakes = amount > 100

            # Update cooldown
            self.points_cog.beg_attack_cooldowns[user_id] = now

            if success:
                # Apply shield reduction - attacker only gains 75% if target has shield
                actual_amount = amount
                if target_has_shield:
                    actual_amount = int(amount * 0.75)

                # Calculate 5% tax
                tax_amount = int(actual_amount * 0.05)
                attacker_gain = actual_amount - tax_amount

                # Attacker wins - steal the amount (minus tax) and track profit_beg
                await conn.execute(
                    "UPDATE users SET points = points + $1, cumulative_attack_gains = cumulative_attack_gains + $2, profit_beg = profit_beg + $1 WHERE user_id = $3",
                    attacker_gain,
                    actual_amount,
                    inter.user.id,
                )
                await conn.execute(
                    "UPDATE users SET points = points - $1 WHERE user_id = $2",
                    actual_amount,
                    self.beggar_id,
                )

                # Add tax to pool
                await self.points_cog.add_to_tax_pool(conn, tax_amount)

                # Track attack stats (win)
                if is_high_stakes:
                    await conn.execute(
                        "UPDATE users SET attack_attempts_high = attack_attempts_high + 1, attack_wins_high = attack_wins_high + 1 WHERE user_id = $1",
                        inter.user.id,
                    )
                else:
                    await conn.execute(
                        "UPDATE users SET attack_attempts_low = attack_attempts_low + 1, attack_wins_low = attack_wins_low + 1 WHERE user_id = $1",
                        inter.user.id,
                    )

                beggar = inter.guild.get_member(self.beggar_id)
                beggar_name = beggar.mention if beggar else f"<@{self.beggar_id}>"

                msg = f"💥 **Attack successful!** {inter.user.mention} gained **{attacker_gain:,} {Config.POINT_NAME}**"
                if tax_amount > 0:
                    msg += f" ({tax_amount} tax collected)"
                if target_has_shield:
                    msg += f" (🛡️ shield reduced gains to 75%)"
                msg += f" from {beggar_name}!"

                await inter.response.send_message(msg, ephemeral=False)
            else:
                # Attacker loses - beggar gains and tracks profit_beg
                await conn.execute(
                    "UPDATE users SET points = points - $1, cumulative_attack_gains = cumulative_attack_gains - $1 WHERE user_id = $2",
                    amount,
                    inter.user.id,
                )
                await conn.execute(
                    "UPDATE users SET points = points + $1, profit_beg = profit_beg + $1 WHERE user_id = $2",
                    amount,
                    self.beggar_id,
                )

                # Track attack stats (loss)
                if is_high_stakes:
                    await conn.execute(
                        "UPDATE users SET attack_attempts_high = attack_attempts_high + 1 WHERE user_id = $1",
                        inter.user.id,
                    )
                else:
                    await conn.execute(
                        "UPDATE users SET attack_attempts_low = attack_attempts_low + 1 WHERE user_id = $1",
                        inter.user.id,
                    )

                beggar = inter.guild.get_member(self.beggar_id)
                beggar_name = beggar.mention if beggar else f"<@{self.beggar_id}>"

                if target_has_dodge:
                    await inter.response.send_message(
                        f"🛡️ **Attack dodged!** {beggar_name} dodged the attack and {inter.user.mention} lost **{amount:,} {Config.POINT_NAME}**!",
                        ephemeral=False,
                    )
                else:
                    await inter.response.send_message(
                        f"💔 **Attack failed!** {inter.user.mention} lost **{amount:,} {Config.POINT_NAME}** to {beggar_name}!",
                        ephemeral=False,
                    )

    @commands.Cog.listener()
    async def on_ready(self):
        """Start the daily tax task when bot is ready"""
        if not self.daily_tax_task.is_running():
            self.daily_tax_task.start()

    @tasks.loop(time=datetime.time(hour=0, minute=0, tzinfo=BANGKOK_TZ))
    async def daily_tax_task(self):
        """Daily task to tax all users and reset cumulative attack gains"""
        if db.pool is None:
            return

        from datetime import date

        now_bangkok = datetime.datetime.now(BANGKOK_TZ)
        today_bangkok = now_bangkok.date()

        async with db.pool.acquire() as conn:
            # Reset cumulative attack gains and defense losses for all users
            await conn.execute(
                "UPDATE users SET cumulative_attack_gains = 0, cumulative_defense_losses = 0"
            )

            # Give 20% interest on stashed points
            stash_users = await conn.fetch(
                "SELECT user_id, stashed_points FROM users WHERE stashed_points > 0"
            )

            total_interest_paid = 0
            for user_row in stash_users:
                stashed = user_row["stashed_points"]
                interest = int(stashed * 0.20)

                # Pay interest to main points (stash stays same)
                if interest > 0:
                    await conn.execute(
                        "UPDATE users SET points = points + $1 WHERE user_id = $2",
                        interest,
                        user_row["user_id"],
                    )
                    total_interest_paid += interest

            # Tax all users with progressive rates based on points + stash
            all_users = await conn.fetch(
                "SELECT user_id, points, stashed_points, last_rich_tax_date FROM users"
            )

            total_tax_collected = 0
            taxed_users = 0
            for user_row in all_users:
                # Check if already taxed today
                last_tax_date = user_row["last_rich_tax_date"]
                if last_tax_date == today_bangkok:
                    continue

                user_points = user_row["points"] or 0
                stashed_points = user_row["stashed_points"] or 0
                total_wealth = user_points + stashed_points

                # Progressive tax brackets
                if total_wealth < 500:
                    tax_rate = 0.0
                elif total_wealth < 1000:
                    tax_rate = 0.05
                elif total_wealth < 2500:
                    tax_rate = 0.10
                elif total_wealth < 5000:
                    tax_rate = 0.15
                elif total_wealth < 7500:
                    tax_rate = 0.20
                else:
                    tax_rate = 0.20  # 20% for 7500+

                if tax_rate == 0.0:
                    # Still mark as taxed but no tax collected
                    await conn.execute(
                        "UPDATE users SET last_rich_tax_date = $1 WHERE user_id = $2",
                        today_bangkok,
                        user_row["user_id"],
                    )
                    continue

                tax_amount = int(total_wealth * tax_rate)

                # Deduct tax from user (points can go negative if not enough)
                await conn.execute(
                    "UPDATE users SET points = points - $1, last_rich_tax_date = $2 WHERE user_id = $3",
                    tax_amount,
                    today_bangkok,
                    user_row["user_id"],
                )

                total_tax_collected += tax_amount
                taxed_users += 1

            # Add to tax pool
            if total_tax_collected > 0:
                await self.add_to_tax_pool(conn, total_tax_collected)

                # Send notification
                bot_channel = self.bot.get_channel(Config.BOT_CHANNEL_ID)
                if bot_channel:
                    embed = disnake.Embed(
                        title="📊 Daily Tax & Interest",
                        description=f"**Tax Collected:** {total_tax_collected:,} {Config.POINT_NAME} from {taxed_users} users (progressive tax: 0-20% based on total wealth).\n**Interest Paid:** {total_interest_paid:,} {Config.POINT_NAME} to {len(stash_users)} users (20% on stashed points).",
                        color=disnake.Color.blue(),
                    )
                    embed.add_field(
                        name="✅ Also Reset",
                        value="All cumulative attack gains and defense losses have been reset to 0.",
                        inline=False,
                    )
                    await bot_channel.send(embed=embed)

    @daily_tax_task.before_loop
    async def before_daily_tax(self):
        await self.bot.wait_until_ready()
        # Wait for db connection
        while db.pool is None:
            await asyncio.sleep(1)


def setup(bot):
    bot.add_cog(Points(bot))
