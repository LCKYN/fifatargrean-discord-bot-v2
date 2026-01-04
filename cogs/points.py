import datetime
import random
import statistics
from datetime import timedelta, timezone

import disnake
from disnake.ext import commands

from core.config import Config
from core.database import db

# Bangkok timezone (UTC+7)
BANGKOK_TZ = timezone(timedelta(hours=7))


class Points(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.attack_cooldowns = {}  # Track last attack time per user
        self.beg_attack_cooldowns = {}  # Track last beg attack time per user
        self.trap_cooldowns = {}  # Track last trap time per user
        self.active_traps = {}  # {channel_id: {trigger_text: (creator_id, created_at)}}
        self.dodge_cooldowns = {}  # Track last dodge time per user
        self.active_dodges = {}  # {user_id: activated_at}
        self.ceasefire_cooldowns = {}  # Track last ceasefire time per user
        self.active_ceasefires = {}  # {user_id: activated_at}
        self.ceasefire_breakers = {}  # {user_id: debuffed_at} - 10 min debuff for breaking ceasefire
        self.active_airdrops = {}  # {message_id: {"claimed_users": set(), "count": 0}}

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
                # First time user - give 1000 points
                await conn.execute(
                    "INSERT INTO users (user_id, points) VALUES ($1, 1000)",
                    member.id,
                )

                # Send welcome message to bot channel
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

            points_to_add = 10
            is_first_of_day = False

            if not user:
                # First time user ever - give 1000 points welcome bonus
                # This does NOT count towards daily earned cap
                points_to_add = 1000
                is_first_of_day = True
                current_points = 0
                daily_earned = 0  # Start at 0, bonus doesn't count
                await conn.execute(
                    "INSERT INTO users (user_id, points, last_message_at, daily_earned, daily_earned_date) VALUES ($1, $2, $3, 0, $4)",
                    message.author.id,
                    points_to_add,
                    now,
                    today_bangkok,
                )

                # Send welcome message
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
                    points_to_add = 100
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
                        points_to_add = 100
                        is_first_of_day = True
                        daily_earned = 0  # Reset daily earned for new day
                    else:
                        # Regular message, check cooldown (15 seconds)
                        if (now - last_msg.replace(tzinfo=None)).total_seconds() < 15:
                            return
                        points_to_add = 10

                # Check daily cap (600 points per day from chatting)
                if daily_earned >= 600:
                    return  # Already hit daily cap

                # Limit points_to_add to not exceed cap
                if daily_earned + points_to_add > 600:
                    points_to_add = 600 - daily_earned

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
        for trigger_text, (creator_id, created_at) in list(channel_traps.items()):
            if (now - created_at).total_seconds() > 900:  # 15 minutes
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
                # Check if victim has at least 200 points
                victim_points = await conn.fetchval(
                    "SELECT points FROM users WHERE user_id = $1", message.author.id
                )
                victim_points = victim_points or 0

                if victim_points >= 200:
                    # Steal 200 points from victim and give to trap creator
                    await conn.execute(
                        "UPDATE users SET points = points - 200 WHERE user_id = $1",
                        message.author.id,
                    )
                    await conn.execute(
                        """INSERT INTO users (user_id, points) VALUES ($1, 200)
                           ON CONFLICT (user_id) DO UPDATE SET points = users.points + 200""",
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
                        f"💣 **TRAP ACTIVATED!** {message.author.mention} triggered a trap set by **{creator_name}** and lost 200 {Config.POINT_NAME}!"
                    )
                else:
                    # Victim doesn't have enough points - add role for 1440 minutes
                    penalty_role = message.guild.get_role(1456114946764181557)
                    if penalty_role:
                        member = message.guild.get_member(message.author.id)
                        if member and penalty_role not in member.roles:
                            await member.add_roles(penalty_role)

                            # Schedule role removal after 1440 minutes (24 hours)
                            import asyncio

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

                    # Notify about trap and penalty
                    creator = message.guild.get_member(creator_id)
                    creator_name = (
                        creator.display_name if creator else f"User {creator_id}"
                    )
                    await message.reply(
                        f"💣 **TRAP ACTIVATED!** {message.author.mention} triggered a trap set by **{creator_name}** but doesn't have enough points! Penalty role added for 24 hours!"
                    )

    @commands.slash_command(description="Set a trap with a trigger word")
    async def trap(
        self,
        inter: disnake.ApplicationCommandInteraction,
        trigger: str = commands.Param(
            description="Text that will trigger the trap (min 5 chars)",
            min_length=5,
            max_length=50,
        ),
    ):
        """Set a trap that steals 50 points from whoever types the trigger text (costs 10 points)"""
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

        # Check if user has at least 40 points to set a trap (cost)
        async with db.pool.acquire() as conn:
            user_points = await conn.fetchval(
                "SELECT points FROM users WHERE user_id = $1", inter.author.id
            )
            user_points = user_points or 0

            if user_points < 40:
                await inter.response.send_message(
                    f"You need at least 40 {Config.POINT_NAME} to set a trap.",
                    ephemeral=True,
                )
                return

            # Deduct 40 points for setting trap
            await conn.execute(
                "UPDATE users SET points = points - 40 WHERE user_id = $1",
                inter.author.id,
            )

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
            f'💣 Trap set! (-40 {Config.POINT_NAME}) Anyone who types **"{trigger}"** in this channel within 15 minutes will lose 200 {Config.POINT_NAME} to you!',
            ephemeral=True,
        )

    @commands.slash_command(description="Check your current points")
    async def point(self, inter: disnake.ApplicationCommandInteraction):
        async with db.pool.acquire() as conn:
            user_data = await conn.fetchrow(
                "SELECT points, daily_earned FROM users WHERE user_id = $1",
                inter.author.id,
            )
            points = user_data["points"] if user_data else 0
            daily_earned = user_data["daily_earned"] if user_data else 0
            # Cap display at 600 for users who earned more before cap change
            display_earned = min(daily_earned, 600)
            await inter.response.send_message(
                f"You have **{points:,} {Config.POINT_NAME}**\n📈 Today: {display_earned}/600 points earned",
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

    @commands.slash_command(description="Attack another user to steal points")
    async def attack(
        self,
        inter: disnake.ApplicationCommandInteraction,
        target: disnake.User = commands.Param(description="User to attack"),
        amount: int = commands.Param(
            description="Points to risk (50-250)", ge=50, le=250, default=50
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

        # Check if target is a mod - can't attack mods
        if inter.guild:
            mod_role = inter.guild.get_role(Config.MOD_ROLE_ID)
            target_member = inter.guild.get_member(target.id)
            if mod_role and target_member and mod_role in target_member.roles:
                await inter.response.send_message(
                    "⚖️ You cannot attack moderators!", ephemeral=True
                )
                return

        # Check if attacker is a mod - mods can't attack
        if inter.guild:
            mod_role = inter.guild.get_role(Config.MOD_ROLE_ID)
            attacker_member = inter.guild.get_member(inter.author.id)
            if mod_role and attacker_member and mod_role in attacker_member.roles:
                await inter.response.send_message(
                    "⚖️ Moderators cannot attack users!", ephemeral=True
                )
                return

        # Check if attacker has active ceasefire - break it if so
        attacker_broke_ceasefire = False
        if user_id in self.active_ceasefires:
            ceasefire_time = self.active_ceasefires[user_id]
            if (now - ceasefire_time).total_seconds() < 900:  # 15 minutes
                # Remove ceasefire
                del self.active_ceasefires[user_id]
                attacker_broke_ceasefire = True

                # Apply 10-minute debuff
                self.ceasefire_breakers[user_id] = now

                # Send notification to channel 1456204479203639340
                notification_channel = self.bot.get_channel(1456204479203639340)
                if notification_channel:
                    embed = disnake.Embed(
                        title="⚠️ Ceasefire Broken!",
                        description=f"{inter.author.mention} has broken their ceasefire by attacking! They now have a 10-minute debuff (easier to attack).",
                        color=disnake.Color.red(),
                    )
                    await notification_channel.send(embed=embed)
            else:
                # Expired ceasefire, clean up
                del self.active_ceasefires[user_id]

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
                    # Remove dodge after use
                    del self.active_dodges[target.id]
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

            if success:
                # Attacker steals points from target
                await conn.execute(
                    "UPDATE users SET points = points + $1 WHERE user_id = $2",
                    amount,
                    inter.author.id,
                )
                await conn.execute(
                    "UPDATE users SET points = points - $1 WHERE user_id = $2",
                    amount,
                    target.id,
                )

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

                # Update cooldown
                self.attack_cooldowns[user_id] = now
                
                # Send result to channel 1456204479203639340
                attack_channel = self.bot.get_channel(1456204479203639340)
                if attack_channel:
                    embed = disnake.Embed(
                        title="💥 Attack Successful!",
                        description=f"{inter.author.mention} stole **{amount} {Config.POINT_NAME}** from {target.mention}!",
                        color=disnake.Color.green(),
                    )
                    await attack_channel.send(embed=embed)
                
                # If used outside the attack channel, show ephemeral message
                if inter.channel_id != 1456204479203639340:
                    await inter.response.send_message(
                        f"💥 Attack successful! Check <#{1456204479203639340}> for result.",
                        ephemeral=True,
                        delete_after=5
                    )
                else:
                    await inter.response.send_message(
                        f"💥 **Attack successful!** You stole {amount} {Config.POINT_NAME} from {target.mention}!",
                        delete_after=5
                    )
            else:
                # Attacker loses points to target
                await conn.execute(
                    "UPDATE users SET points = points - $1 WHERE user_id = $2",
                    amount,
                    inter.author.id,
                )
                await conn.execute(
                    "UPDATE users SET points = points + $1 WHERE user_id = $2",
                    amount,
                    target.id,
                )

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

                # Update cooldown
                self.attack_cooldowns[user_id] = now
                
                # Send result to channel 1456204479203639340
                attack_channel = self.bot.get_channel(1456204479203639340)
                if attack_channel:
                    if target_has_dodge:
                        embed = disnake.Embed(
                            title="🛡️ Attack Dodged!",
                            description=f"{target.mention} dodged {inter.author.mention}'s attack! {inter.author.mention} lost **{amount} {Config.POINT_NAME}**!",
                            color=disnake.Color.blue(),
                        )
                    else:
                        embed = disnake.Embed(
                            title="💔 Attack Failed!",
                            description=f"{inter.author.mention} failed to attack {target.mention} and lost **{amount} {Config.POINT_NAME}**!",
                            color=disnake.Color.red(),
                        )
                    await attack_channel.send(embed=embed)
                
                # If used outside the attack channel, show ephemeral message
                if inter.channel_id != 1456204479203639340:
                    if target_has_dodge:
                        await inter.response.send_message(
                            f"🛡️ Attack dodged! Check <#{1456204479203639340}> for result.",
                            ephemeral=True,
                            delete_after=5
                        )
                    else:
                        await inter.response.send_message(
                            f"💔 Attack failed! Check <#{1456204479203639340}> for result.",
                            ephemeral=True,
                            delete_after=5
                        )
                else:
                    if target_has_dodge:
                        await inter.response.send_message(
                            f"🛡️ **Attack dodged!** {target.mention} dodged your attack and you lost {amount} {Config.POINT_NAME}!",
                            delete_after=5
                        )
                    else:
                        await inter.response.send_message(
                            f"💔 **Attack failed!** You lost {amount} {Config.POINT_NAME} to {target.mention}!",
                            delete_after=5
                        )

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

        # Check if target is a mod - can't attack mods
        if inter.guild:
            mod_role = inter.guild.get_role(Config.MOD_ROLE_ID)
            target_member = inter.guild.get_member(target.id)
            if mod_role and target_member and mod_role in target_member.roles:
                await inter.response.send_message(
                    "⚖️ You cannot attack moderators!", ephemeral=True
                )
                return

        # Check if attacker is a mod - mods can't attack
        if inter.guild:
            mod_role = inter.guild.get_role(Config.MOD_ROLE_ID)
            attacker_member = inter.guild.get_member(inter.author.id)
            if mod_role and attacker_member and mod_role in attacker_member.roles:
                await inter.response.send_message(
                    "⚖️ Moderators cannot attack users!", ephemeral=True
                )
                return

        # Check if attacker has active ceasefire - break it if so
        attacker_broke_ceasefire = False
        if user_id in self.active_ceasefires:
            ceasefire_time = self.active_ceasefires[user_id]
            if (now - ceasefire_time).total_seconds() < 900:  # 15 minutes
                # Remove ceasefire
                del self.active_ceasefires[user_id]
                attacker_broke_ceasefire = True

                # Apply 10-minute debuff
                self.ceasefire_breakers[user_id] = now

                # Send notification to channel 1456204479203639340
                notification_channel = self.bot.get_channel(1456204479203639340)
                if notification_channel:
                    embed = disnake.Embed(
                        title="⚠️ Ceasefire Broken!",
                        description=f"{inter.author.mention} has broken their ceasefire by attacking! They now have a 10-minute debuff (easier to attack).",
                        color=disnake.Color.red(),
                    )
                    await notification_channel.send(embed=embed)
            else:
                # Expired ceasefire, clean up
                del self.active_ceasefires[user_id]

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

            # Check if target has active ceasefire
            target_has_ceasefire = False
            if target.id in self.active_ceasefires:
                ceasefire_time = self.active_ceasefires[target.id]
                if (
                    datetime.datetime.now() - ceasefire_time
                ).total_seconds() < 900:  # 15 minutes
                    target_has_ceasefire = True
                else:
                    # Expired ceasefire, clean up
                    del self.active_ceasefires[target.id]

            if target_has_ceasefire:
                await inter.response.send_message(
                    f"☮️ {target.mention} has an active ceasefire! They cannot be attacked right now.",
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
                # Attacker steals points from target
                await conn.execute(
                    "UPDATE users SET points = points + $1 WHERE user_id = $2",
                    amount,
                    inter.author.id,
                )
                await conn.execute(
                    "UPDATE users SET points = points - $1 WHERE user_id = $2",
                    amount,
                    target.id,
                )

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

                # Update cooldown
                self.attack_cooldowns[user_id] = now
                await inter.response.send_message(
                    f"🎯 **Pierce successful!** You pierced {target.mention}'s dodge and stole {amount} {Config.POINT_NAME}!"
                )
            else:
                # Pierce attack fails - target didn't have dodge
                # Attacker loses points to target
                await conn.execute(
                    "UPDATE users SET points = points - $1 WHERE user_id = $2",
                    amount,
                    inter.author.id,
                )
                await conn.execute(
                    "UPDATE users SET points = points + $1 WHERE user_id = $2",
                    amount,
                    target.id,
                )

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

                # Update cooldown
                self.attack_cooldowns[user_id] = now
                await inter.response.send_message(
                    f"❌ **Pierce failed!** {target.mention} had no dodge and you lost {amount} {Config.POINT_NAME}!"
                )

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
        """Activate dodge to block the next attack (costs 50 points, lasts 30 minutes)"""
        now = datetime.datetime.now()
        user_id = inter.author.id

        # Check cooldown (30 minutes)
        if user_id in self.dodge_cooldowns:
            time_passed = (now - self.dodge_cooldowns[user_id]).total_seconds()
            if time_passed < 1800:  # 30 minutes
                remaining_mins = int((1800 - time_passed) / 60)
                remaining_secs = int((1800 - time_passed) % 60)
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

        async with db.pool.acquire() as conn:
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

            # Deduct 50 points
            await conn.execute(
                "UPDATE users SET points = points - 50 WHERE user_id = $1",
                inter.author.id,
            )

        # Activate dodge
        self.active_dodges[user_id] = now
        self.dodge_cooldowns[user_id] = now

        await inter.response.send_message(
            f"🛡️ **Dodge activated!** (-50 {Config.POINT_NAME}) The next attack against you within 5 minutes will automatically fail!",
            ephemeral=True,
        )

    @commands.slash_command(description="Activate ceasefire to prevent all attacks")
    async def ceasefire(
        self,
        inter: disnake.ApplicationCommandInteraction,
    ):
        """Activate ceasefire to be immune from attacks (costs 50 points, lasts 15 minutes)"""
        now = datetime.datetime.now()
        user_id = inter.author.id

        # Check cooldown (30 minutes)
        if user_id in self.ceasefire_cooldowns:
            time_passed = (now - self.ceasefire_cooldowns[user_id]).total_seconds()
            if time_passed < 1800:  # 30 minutes
                remaining_mins = int((1800 - time_passed) / 60)
                remaining_secs = int((1800 - time_passed) % 60)
                await inter.response.send_message(
                    f"⏰ You need to wait {remaining_mins}m {remaining_secs}s before using ceasefire again.",
                    ephemeral=True,
                )
                return

        # Check if already has active ceasefire
        if user_id in self.active_ceasefires:
            ceasefire_time = self.active_ceasefires[user_id]
            if (now - ceasefire_time).total_seconds() < 900:  # 15 minutes
                remaining_secs = int(900 - (now - ceasefire_time).total_seconds())
                remaining_mins = remaining_secs // 60
                remaining_secs = remaining_secs % 60
                await inter.response.send_message(
                    f"☮️ You already have an active ceasefire! ({remaining_mins}m {remaining_secs}s remaining)",
                    ephemeral=True,
                )
                return

        async with db.pool.acquire() as conn:
            user_points = await conn.fetchval(
                "SELECT points FROM users WHERE user_id = $1", inter.author.id
            )
            user_points = user_points or 0

            if user_points < 50:
                await inter.response.send_message(
                    f"You need at least 50 {Config.POINT_NAME} to activate ceasefire.",
                    ephemeral=True,
                )
                return

            # Deduct 50 points
            await conn.execute(
                "UPDATE users SET points = points - 50 WHERE user_id = $1",
                inter.author.id,
            )

        # Activate ceasefire
        self.active_ceasefires[user_id] = now
        self.ceasefire_cooldowns[user_id] = now

        await inter.response.send_message(
            f"☮️ **Ceasefire activated!** (-50 {Config.POINT_NAME}) You are immune from all attacks for 15 minutes!",
            ephemeral=True,
        )

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
            description=f"React with 💸 to claim **{amount} {Config.POINT_NAME}**!\n\n"
            f"• First **{max_users}** users only!\n"
            f"• < 500 {Config.POINT_NAME}: 50% chance for 2x\n"
            f"• > 1500 {Config.POINT_NAME}: 70% chance for nothing",
            color=disnake.Color.gold(),
        )
        embed.set_footer(text=f"0/{max_users} claimed")

        await inter.response.send_message(embed=embed)
        msg = await inter.original_message()

        # Add reaction
        await msg.add_reaction("💸")

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

        # Check if it's the correct emoji
        if str(payload.emoji) != "💸":
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

        async with db.pool.acquire() as conn:
            # Get user's current points
            user_points = await conn.fetchval(
                "SELECT points FROM users WHERE user_id = $1", payload.user_id
            )
            user_points = user_points or 0

            points_to_give = airdrop["amount"]
            result_type = "normal"

            # Apply luck mechanics
            if user_points > 1500:
                # 70% chance for bad luck (get nothing)
                if random.random() < 0.70:
                    points_to_give = 0
                    result_type = "bad_luck"
            elif user_points < 500:
                # 50% chance for crit (2x)
                if random.random() < 0.50:
                    points_to_give *= 2
                    result_type = "crit"

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

            # Get channel and send notification
            channel = self.bot.get_channel(payload.channel_id)
            if channel:
                user = self.bot.get_user(payload.user_id)
                user_mention = user.mention if user else f"<@{payload.user_id}>"

                if result_type == "bad_luck":
                    await channel.send(
                        f"💸 {user_mention} claimed the airdrop but got **nothing** (bad luck)! [{airdrop['count']}/{airdrop['max_users']}]"
                    )
                elif result_type == "crit":
                    await channel.send(
                        f"💸✨ {user_mention} claimed the airdrop with **CRIT** and got **{points_to_give} {Config.POINT_NAME}**! [{airdrop['count']}/{airdrop['max_users']}]"
                    )
                else:
                    await channel.send(
                        f"💸 {user_mention} claimed the airdrop and got **{points_to_give} {Config.POINT_NAME}**! [{airdrop['count']}/{airdrop['max_users']}]"
                    )

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
                   attack_attempts_high, attack_wins_high
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
            else:
                points = user_data["points"] or 0
                total_sent = user_data["total_sent"] or 0
                total_received = user_data["total_received"] or 0
                daily_earned = user_data["daily_earned"] or 0
                attack_attempts_low = user_data["attack_attempts_low"] or 0
                attack_wins_low = user_data["attack_wins_low"] or 0
                attack_attempts_high = user_data["attack_attempts_high"] or 0
                attack_wins_high = user_data["attack_wins_high"] or 0

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
        # Cap display at 600 for users who earned more before cap change
        display_earned = min(daily_earned, 600)
        embed.add_field(
            name=f"💰 {Config.POINT_NAME}",
            value=f"**{points:,}** points\n📤 Sent: {total_sent:,}\n📥 Received: {total_received:,}\n📈 Today: {display_earned}/600",
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

        # Active effects (ceasefire, debuff) - Note: Dodge is NOT shown to keep it strategic
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

        # Check ceasefire
        if target.id in self.active_ceasefires:
            ceasefire_time = self.active_ceasefires[target.id]
            if (now - ceasefire_time).total_seconds() < 900:  # 15 minutes
                remaining_secs = int(900 - (now - ceasefire_time).total_seconds())
                remaining_mins = remaining_secs // 60
                remaining_secs = remaining_secs % 60
                active_effects.append(
                    f"☮️ Ceasefire: {remaining_mins}m {remaining_secs}s"
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
        await inter.response.send_message(embed=embed)

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
                ephemeral=True
            )
        else:
            await inter.response.send_message(
                "❌ Could not find the beg channel.",
                ephemeral=True
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
                # 45% success for ≤100 points, 35% for >100 points
                win_chance = 0.35 if amount > 100 else 0.45

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

            # Track stats based on amount
            is_high_stakes = amount > 100

            # Update cooldown
            self.points_cog.beg_attack_cooldowns[user_id] = now

            if success:
                # Attacker wins - steal the amount
                await conn.execute(
                    "UPDATE users SET points = points + $1 WHERE user_id = $2",
                    amount,
                    inter.user.id,
                )
                await conn.execute(
                    "UPDATE users SET points = points - $1 WHERE user_id = $2",
                    amount,
                    self.beggar_id,
                )

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

                await inter.response.send_message(
                    f"💥 **Attack successful!** {inter.user.mention} stole **{amount:,} {Config.POINT_NAME}** from {beggar_name}!",
                    ephemeral=False,
                )
            else:
                # Attacker loses - lose the amount to target
                await conn.execute(
                    "UPDATE users SET points = points - $1 WHERE user_id = $2",
                    amount,
                    inter.user.id,
                )
                await conn.execute(
                    "UPDATE users SET points = points + $1 WHERE user_id = $2",
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


def setup(bot):
    bot.add_cog(Points(bot))
