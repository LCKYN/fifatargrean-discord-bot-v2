import asyncio
import datetime
import random
from typing import Dict, Set

import disnake
from disnake.ext import commands

from core.config import Config
from core.database import db


class GuildWarView(disnake.ui.View):
    def __init__(
        self, war_id: int, team1_name: str, team2_name: str, is_active: bool = True
    ):
        super().__init__(timeout=None)
        self.war_id = war_id

        # Team 1 button
        team1_button = disnake.ui.Button(
            label=f"Join {team1_name}",
            style=disnake.ButtonStyle.primary,
            custom_id=f"guildwar_{war_id}_team1",
            disabled=not is_active,
        )
        team1_button.callback = self.join_team1
        self.add_item(team1_button)

        # Team 2 button
        team2_button = disnake.ui.Button(
            label=f"Join {team2_name}",
            style=disnake.ButtonStyle.danger,
            custom_id=f"guildwar_{war_id}_team2",
            disabled=not is_active,
        )
        team2_button.callback = self.join_team2
        self.add_item(team2_button)

        # Unjoin button
        unjoin_button = disnake.ui.Button(
            label="Leave War",
            style=disnake.ButtonStyle.secondary,
            custom_id=f"guildwar_{war_id}_unjoin",
            disabled=not is_active,
        )
        unjoin_button.callback = self.unjoin_war
        self.add_item(unjoin_button)

    async def join_team1(self, interaction: disnake.MessageInteraction):
        await self.join_team(interaction, 1)

    async def join_team2(self, interaction: disnake.MessageInteraction):
        await self.join_team(interaction, 2)

    async def unjoin_war(self, interaction: disnake.MessageInteraction):
        """Leave the war and get refunded"""
        user_id = interaction.author.id

        async with db.pool.acquire() as conn:
            # Get war details
            war = await conn.fetchrow(
                "SELECT * FROM guild_wars WHERE id = $1", self.war_id
            )

            if not war:
                await interaction.response.send_message(
                    "This war no longer exists.", ephemeral=True
                )
                return

            if war["status"] != "recruiting":
                await interaction.response.send_message(
                    "Cannot leave a war that has already started.", ephemeral=True
                )
                return

            # Check if user is in the war
            member = await conn.fetchrow(
                "SELECT points_bet FROM guild_war_members WHERE war_id = $1 AND user_id = $2",
                self.war_id,
                user_id,
            )

            if not member:
                await interaction.response.send_message(
                    "You are not in this war!", ephemeral=True
                )
                return

            # Refund points
            refund_amount = member["points_bet"]
            await conn.execute(
                "UPDATE users SET points = points + $1 WHERE user_id = $2",
                refund_amount,
                user_id,
            )

            # Remove from war
            await conn.execute(
                "DELETE FROM guild_war_members WHERE war_id = $1 AND user_id = $2",
                self.war_id,
                user_id,
            )

            await interaction.response.send_message(
                f"You left the war! (+{refund_amount} {Config.POINT_NAME} refunded)",
                ephemeral=True,
            )

            # Update embed
            await self.update_war_embed(interaction)

    async def join_team(
        self, interaction: disnake.MessageInteraction, team_number: int
    ):
        user_id = interaction.author.id

        async with db.pool.acquire() as conn:
            # Get war details
            war = await conn.fetchrow(
                "SELECT * FROM guild_wars WHERE id = $1", self.war_id
            )

            if not war:
                await interaction.response.send_message(
                    "This war no longer exists.", ephemeral=True
                )
                return

            if war["status"] != "recruiting":
                await interaction.response.send_message(
                    "This war is not accepting new members.", ephemeral=True
                )
                return

            # Check if user has enough points
            user_points = await conn.fetchval(
                "SELECT points FROM users WHERE user_id = $1", user_id
            )
            user_points = user_points or 0

            entry_cost = war["entry_cost"]
            if user_points < entry_cost:
                await interaction.response.send_message(
                    f"You need at least {entry_cost} {Config.POINT_NAME} to join this war.",
                    ephemeral=True,
                )
                return

            # Check if already in a team
            existing = await conn.fetchrow(
                "SELECT team_number FROM guild_war_members WHERE war_id = $1 AND user_id = $2",
                self.war_id,
                user_id,
            )

            if existing:
                if existing["team_number"] == team_number:
                    await interaction.response.send_message(
                        "You're already in this team!", ephemeral=True
                    )
                    return
                else:
                    # Switch teams
                    await conn.execute(
                        "UPDATE guild_war_members SET team_number = $1 WHERE war_id = $2 AND user_id = $3",
                        team_number,
                        self.war_id,
                        user_id,
                    )
                    await interaction.response.send_message(
                        f"You switched to Team {team_number}!", ephemeral=True
                    )
            else:
                # Join team
                await conn.execute(
                    "INSERT INTO guild_war_members (war_id, user_id, team_number, points_bet) VALUES ($1, $2, $3, $4)",
                    self.war_id,
                    user_id,
                    team_number,
                    entry_cost,
                )

                # Deduct points
                await conn.execute(
                    "UPDATE users SET points = points - $1 WHERE user_id = $2",
                    entry_cost,
                    user_id,
                )

                await interaction.response.send_message(
                    f"You joined Team {team_number}! (-{entry_cost} {Config.POINT_NAME})",
                    ephemeral=True,
                )

            # Update embed
            await self.update_war_embed(interaction)

    async def update_war_embed(self, interaction: disnake.MessageInteraction):
        try:
            async with db.pool.acquire() as conn:
                war = await conn.fetchrow(
                    "SELECT * FROM guild_wars WHERE id = $1", self.war_id
                )

                team1_members = await conn.fetch(
                    "SELECT user_id FROM guild_war_members WHERE war_id = $1 AND team_number = 1",
                    self.war_id,
                )

                team2_members = await conn.fetch(
                    "SELECT user_id FROM guild_war_members WHERE war_id = $1 AND team_number = 2",
                    self.war_id,
                )

                embed = disnake.Embed(
                    title=f"⚔️ Guild War: {war['war_name']}",
                    description=f"Entry Cost: **{war['entry_cost']} {Config.POINT_NAME}**",
                    color=disnake.Color.orange(),
                )

                team1_list = (
                    "\n".join([f"<@{m['user_id']}>" for m in team1_members])
                    or "No members yet"
                )
                team2_list = (
                    "\n".join([f"<@{m['user_id']}>" for m in team2_members])
                    or "No members yet"
                )

                embed.add_field(
                    name=f"🔵 {war['team1_name']} ({len(team1_members)})",
                    value=team1_list,
                    inline=True,
                )

                embed.add_field(
                    name=f"🔴 {war['team2_name']} ({len(team2_members)})",
                    value=team2_list,
                    inline=True,
                )

                embed.set_footer(
                    text=f"War ID: {self.war_id} | Status: {war['status']}"
                )

                await interaction.message.edit(embed=embed)
        except Exception as e:
            print(f"Error updating war embed: {e}")


class CreateWarModal(disnake.ui.Modal):
    def __init__(self, bot):
        self.bot = bot
        components = [
            disnake.ui.TextInput(
                label="War Name",
                placeholder="Enter war name",
                custom_id="war_name",
                style=disnake.TextInputStyle.short,
                max_length=100,
            ),
            disnake.ui.TextInput(
                label="Team 1 Name",
                placeholder="Enter team 1 name",
                custom_id="team1_name",
                style=disnake.TextInputStyle.short,
                max_length=50,
            ),
            disnake.ui.TextInput(
                label="Team 2 Name",
                placeholder="Enter team 2 name",
                custom_id="team2_name",
                style=disnake.TextInputStyle.short,
                max_length=50,
            ),
            disnake.ui.TextInput(
                label="Entry Cost (10-500)",
                placeholder="Enter points required to join (10-500)",
                custom_id="entry_cost",
                style=disnake.TextInputStyle.short,
                min_length=2,
                max_length=3,
            ),
        ]
        super().__init__(title="Create Guild War", components=components)

    async def callback(self, inter: disnake.ModalInteraction):
        war_name = inter.text_values["war_name"]
        team1_name = inter.text_values["team1_name"]
        team2_name = inter.text_values["team2_name"]

        try:
            entry_cost = int(inter.text_values["entry_cost"])
            if entry_cost < 10 or entry_cost > 500:
                await inter.response.send_message(
                    "Entry cost must be between 10 and 500.", ephemeral=True
                )
                return
        except ValueError:
            await inter.response.send_message(
                "Entry cost must be a number.", ephemeral=True
            )
            return

        await inter.response.defer()

        async with db.pool.acquire() as conn:
            # Create war in database
            war_id = await conn.fetchval(
                """INSERT INTO guild_wars
                   (creator_id, war_name, team1_name, team2_name, entry_cost, status, created_at)
                   VALUES ($1, $2, $3, $4, $5, 'recruiting', $6)
                   RETURNING id""",
                inter.author.id,
                war_name,
                team1_name,
                team2_name,
                entry_cost,
                datetime.datetime.now(),
            )

        # Create thread in guild war channel
        guild_war_channel = self.bot.get_channel(1456851588239986762)
        if not guild_war_channel:
            await inter.followup.send("Guild war channel not found.", ephemeral=True)
            return

        thread = await guild_war_channel.create_thread(
            name=f"⚔️ War #{war_id}: {war_name}",
            type=disnake.ChannelType.public_thread,
            auto_archive_duration=1440,  # 24 hours
        )

        # Create embed
        embed = disnake.Embed(
            title=f"⚔️ Guild War: {war_name}",
            description=f"Entry Cost: **{entry_cost} {Config.POINT_NAME}**\n\nJoin a team to participate!",
            color=disnake.Color.orange(),
        )

        embed.add_field(
            name=f"🔵 {team1_name} (0)", value="No members yet", inline=True
        )

        embed.add_field(
            name=f"🔴 {team2_name} (0)", value="No members yet", inline=True
        )

        embed.set_footer(
            text=f"War ID: {war_id} | Created by {inter.author.display_name}"
        )

        view = GuildWarView(war_id, team1_name, team2_name, is_active=True)
        message = await thread.send(embed=embed, view=view)

        # Update database with thread and message IDs
        async with db.pool.acquire() as conn:
            await conn.execute(
                "UPDATE guild_wars SET thread_id = $1, message_id = $2 WHERE id = $3",
                thread.id,
                message.id,
                war_id,
            )

        await inter.followup.send(
            f"✅ Guild War created! Check {thread.mention}", ephemeral=True
        )


class GuildWar(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.slash_command(description="Create a guild war")
    async def guildwar(self, inter: disnake.ApplicationCommandInteraction):
        """Create a new guild war"""
        modal = CreateWarModal(self.bot)
        await inter.response.send_modal(modal)

    @commands.slash_command(description="[MOD/Creator] Start the guild war")
    async def startwar(
        self,
        inter: disnake.ApplicationCommandInteraction,
        war_id: int = commands.Param(description="War ID to start"),
    ):
        """Start a guild war battle simulation"""
        async with db.pool.acquire() as conn:
            war = await conn.fetchrow("SELECT * FROM guild_wars WHERE id = $1", war_id)

            if not war:
                await inter.response.send_message("War not found.", ephemeral=True)
                return

            # Check permission
            mod_role = inter.guild.get_role(Config.MOD_ROLE_ID)
            is_mod = mod_role and mod_role in inter.author.roles
            is_creator = war["creator_id"] == inter.author.id

            if not is_mod and not is_creator:
                await inter.response.send_message(
                    "Only the creator or a mod can start this war.", ephemeral=True
                )
                return

            if war["status"] != "recruiting":
                await inter.response.send_message(
                    "This war has already been started or finished.", ephemeral=True
                )
                return

            # Get teams
            team1_members = await conn.fetch(
                "SELECT user_id, points_bet FROM guild_war_members WHERE war_id = $1 AND team_number = 1",
                war_id,
            )

            team2_members = await conn.fetch(
                "SELECT user_id, points_bet FROM guild_war_members WHERE war_id = $1 AND team_number = 2",
                war_id,
            )

            if len(team1_members) == 0 or len(team2_members) == 0:
                await inter.response.send_message(
                    "Both teams need at least 1 member to start.", ephemeral=True
                )
                return

            # Update status
            await conn.execute(
                "UPDATE guild_wars SET status = 'in_progress' WHERE id = $1", war_id
            )

        await inter.response.send_message("⚔️ Starting war...", ephemeral=True)

        # Get thread
        thread = self.bot.get_channel(war["thread_id"])
        if not thread:
            await inter.followup.send("Thread not found.", ephemeral=True)
            return

        # Battle simulation
        await self.simulate_battle(thread, war, team1_members, team2_members)

    @commands.slash_command(description="[MOD/Creator] Cancel the guild war and refund")
    async def cancelwar(
        self,
        inter: disnake.ApplicationCommandInteraction,
        war_id: int = commands.Param(description="War ID to cancel"),
    ):
        """Cancel a guild war and refund all participants"""
        # Defer response first
        await inter.response.defer(ephemeral=True)

        # Unarchive thread if needed
        async with db.pool.acquire() as conn:
            war = await conn.fetchrow("SELECT * FROM guild_wars WHERE id = $1", war_id)

            if not war:
                await inter.followup.send("War not found.", ephemeral=True)
                return

        # Unarchive thread if it's archived
        thread = self.bot.get_channel(war["thread_id"])
        if thread and isinstance(thread, disnake.Thread) and thread.archived:
            await thread.edit(archived=False)

        async with db.pool.acquire() as conn:
            # Check permission
            mod_role = inter.guild.get_role(Config.MOD_ROLE_ID)
            is_mod = mod_role and mod_role in inter.author.roles
            is_creator = war["creator_id"] == inter.author.id

            if not is_mod and not is_creator:
                await inter.followup.send(
                    "Only the creator or a mod can cancel this war.", ephemeral=True
                )
                return

            if war["status"] == "finished":
                await inter.followup.send(
                    "Cannot cancel a finished war.", ephemeral=True
                )
                return

            if war["status"] == "cancelled":
                await inter.followup.send(
                    "This war is already cancelled.", ephemeral=True
                )
                return

            # Get all members
            all_members = await conn.fetch(
                "SELECT user_id, points_bet FROM guild_war_members WHERE war_id = $1",
                war_id,
            )

            # Refund all members
            for member in all_members:
                await conn.execute(
                    "UPDATE users SET points = points + $1 WHERE user_id = $2",
                    member["points_bet"],
                    member["user_id"],
                )

            # Update war status
            await conn.execute(
                "UPDATE guild_wars SET status = 'cancelled' WHERE id = $1", war_id
            )

        # Update thread message
        try:
            if thread:
                message = await thread.fetch_message(war["message_id"])

                embed = disnake.Embed(
                    title=f"⚔️ Guild War: {war['war_name']} [CANCELLED]",
                    description=f"This war has been cancelled. All {len(all_members)} participants have been refunded.",
                    color=disnake.Color.red(),
                )

                embed.set_footer(
                    text=f"War ID: {war_id} | Cancelled by {inter.author.display_name}"
                )

                view = GuildWarView(
                    war_id, war["team1_name"], war["team2_name"], is_active=False
                )
                await message.edit(embed=embed, view=view)

                await thread.send(
                    f"❌ **War Cancelled!**\n{len(all_members)} participants have been refunded their entry cost."
                )

                # Archive thread
                await thread.edit(archived=True, locked=True)
        except Exception as e:
            print(f"Error updating cancelled war: {e}")

        await inter.followup.send(
            f"✅ War #{war_id} cancelled. All {len(all_members)} participants refunded.",
            ephemeral=True,
        )

    async def simulate_battle(self, thread, war, team1_members, team2_members):
        """Simulate the guild war battle with HP and attack/defense mechanics"""
        # Initialize player stats
        players = {}
        team1_ids = []
        team2_ids = []

        for member in team1_members:
            user_id = member["user_id"]
            players[user_id] = {
                "hp": 100,
                "team": 1,
                "forced_attack": False,
                "dodge_active": False,
                "power_boost": 0,  # Extra damage
                "shield": 0,  # Damage reduction %
                "crit_boost": 0,  # Extra crit chance
                "vulnerable": 0,  # Extra damage taken %
                "stunned": False,  # Skip turn
            }
            team1_ids.append(user_id)

        for member in team2_members:
            user_id = member["user_id"]
            players[user_id] = {
                "hp": 100,
                "team": 2,
                "forced_attack": False,
                "dodge_active": False,
                "power_boost": 0,
                "shield": 0,
                "crit_boost": 0,
                "vulnerable": 0,
                "stunned": False,
            }
            team2_ids.append(user_id)

        def format_user(user_id):
            team = (
                players[user_id]["team"]
                if user_id in players
                else (1 if user_id in team1_ids else 2)
            )
            color = "🔵" if team == 1 else "🔴"
            hp = players[user_id]["hp"] if user_id in players else 0
            return f"<@{user_id}> {color} ({hp} HP)"

        def apply_damage_variance(damage):
            """Apply ±20% variance to damage as integer"""
            variance = random.uniform(-0.20, 0.20)
            return int(damage * (1 + variance))

        def get_alive():
            return [uid for uid, data in players.items() if data["hp"] > 0]

        def get_team_alive(team_num):
            return [
                uid
                for uid, data in players.items()
                if data["hp"] > 0 and data["team"] == team_num
            ]

        await thread.send("⚔️ **THE BATTLE BEGINS!**\n━━━━━━━━━━━━━━━━━━━━")
        await asyncio.sleep(2)

        round_num = 1
        while len(get_team_alive(1)) > 0 and len(get_team_alive(2)) > 0:
            await thread.send(f"\n**━━━ Round {round_num} ━━━**")

            alive = get_alive()
            round_actions = {}

            # BUFF/DEBUFF PHASE - Show before combat
            await thread.send("**⚡ Status Phase**")

            # Determine actions for each player
            for user_id in alive:
                player = players[user_id]

                # Clear one-turn effects from previous round
                if player.get("stunned"):
                    player["stunned"] = False

                # 30% chance for buff/debuff event
                event_type = None  # Initialize event_type
                if random.random() < 0.30:
                    event_type = random.choice(
                        [
                            "retreat",
                            "run_away",
                            "dodge",
                            "power_up",
                            "shield",
                            "focus",
                            "heal",
                            "berserk",
                            "weaken",
                            "vulnerable",
                            "stun",
                        ]
                    )

                    # DEBUFFS
                    if event_type == "retreat":
                        old_hp = player["hp"]
                        player["hp"] = player["hp"] // 2
                        await thread.send(
                            f"🏃 {format_user(user_id)} retreats in fear! HP reduced by half ({old_hp} → {player['hp']})!"
                        )
                        round_actions[user_id] = "retreat"
                    elif event_type == "run_away":
                        old_hp = player["hp"]
                        player["hp"] = player["hp"] // 2
                        await thread.send(
                            f"😱 {format_user(user_id)} runs away! HP reduced by half ({old_hp} → {player['hp']})!"
                        )
                        round_actions[user_id] = "run_away"
                    elif event_type == "weaken":
                        player["power_boost"] = -10
                        await thread.send(
                            f"💔 {format_user(user_id)} is weakened! -10 damage this round!"
                        )
                        round_actions[user_id] = "weakened"
                    elif event_type == "vulnerable":
                        player["vulnerable"] = 30
                        await thread.send(
                            f"🩸 {format_user(user_id)} becomes vulnerable! +30% damage taken this round!"
                        )
                        round_actions[user_id] = "vulnerable_state"
                    elif event_type == "stun":
                        player["stunned"] = True
                        await thread.send(
                            f"💫 {format_user(user_id)} is stunned! Cannot act this round!"
                        )
                        round_actions[user_id] = "stunned"

                    # BUFFS
                    elif event_type == "dodge":
                        player["dodge_active"] = True
                        await thread.send(
                            f"✨ {format_user(user_id)} activates dodge! (50% evasion)"
                        )
                        round_actions[user_id] = "dodge_prep"
                    elif event_type == "power_up":
                        player["power_boost"] = 20
                        await thread.send(
                            f"💪 {format_user(user_id)} powers up! +20 damage this round!"
                        )
                        round_actions[user_id] = "powered_up"
                    elif event_type == "shield":
                        player["shield"] = 40
                        await thread.send(
                            f"🛡️ {format_user(user_id)} raises a shield! -40% damage taken this round!"
                        )
                        round_actions[user_id] = "shielded"
                    elif event_type == "focus":
                        player["crit_boost"] = 20
                        await thread.send(
                            f"🎯 {format_user(user_id)} focuses intensely! +20% critical chance!"
                        )
                        round_actions[user_id] = "focused"
                    elif event_type == "heal":
                        heal_amount = 25
                        player["hp"] = min(100, player["hp"] + heal_amount)
                        await thread.send(
                            f"💚 {format_user(user_id)} recovers health! +{heal_amount} HP!"
                        )
                        round_actions[user_id] = "healed"
                    elif event_type == "berserk":
                        player["power_boost"] = 25
                        player["vulnerable"] = 25
                        await thread.send(
                            f"😤 {format_user(user_id)} goes berserk! +25 damage but +25% damage taken!"
                        )
                        round_actions[user_id] = "berserk"

                    await asyncio.sleep(0.8)
                    continue

                # Reset temporary buffs/debuffs if no event
                if event_type not in [
                    "power_up",
                    "shield",
                    "focus",
                    "weaken",
                    "vulnerable",
                    "berserk",
                ]:
                    player["power_boost"] = 0
                    player["shield"] = 0
                    player["crit_boost"] = 0
                    player["vulnerable"] = 0

                # Determine attack or defense
                if player.get("stunned"):
                    round_actions[user_id] = "stunned"
                elif player["forced_attack"]:
                    round_actions[user_id] = "attack"
                    player["forced_attack"] = False
                else:
                    # 60% attack, 40% defense
                    round_actions[user_id] = (
                        "attack" if random.random() < 0.6 else "defense"
                    )

                    if round_actions[user_id] == "defense":
                        player["forced_attack"] = True  # Next round must attack

            await asyncio.sleep(1)
            await thread.send("**⚔️ Combat Phase**")

            # Process combat actions
            processed = set()

            for user_id in alive:
                skip_actions = [
                    "retreat",
                    "run_away",
                    "dodge_prep",
                    "weakened",
                    "vulnerable_state",
                    "stunned",
                    "powered_up",
                    "shielded",
                    "focused",
                    "healed",
                    "berserk",
                ]
                if user_id in processed or round_actions.get(user_id) in skip_actions:
                    continue

                if players[user_id]["hp"] <= 0:
                    continue

                action = round_actions[user_id]

                if action == "attack":
                    # Find target from opposite team
                    enemy_team = 2 if players[user_id]["team"] == 1 else 1
                    enemies = [
                        e for e in get_team_alive(enemy_team) if e not in processed
                    ]

                    if not enemies:
                        continue

                    target = random.choice(enemies)
                    target_action = round_actions.get(target, "attack")

                    # Check for perfect strike (2% chance)
                    if random.random() < 0.02:
                        players[target]["hp"] = 0
                        await thread.send(
                            f"🌟 **PERFECT STRIKE!** {format_user(user_id)} instantly defeats {format_user(target)}!"
                        )
                        processed.add(user_id)
                        processed.add(target)
                        await asyncio.sleep(1.5)
                        continue

                    # Check dodge
                    if players[target].get("dodge_active") and random.random() < 0.5:
                        await thread.send(
                            f"✨ {format_user(target)} dodges {format_user(user_id)}'s attack!"
                        )
                        players[target]["dodge_active"] = False
                        processed.add(user_id)
                        processed.add(target)
                        await asyncio.sleep(1)
                        continue

                    players[target]["dodge_active"] = (
                        False  # Remove dodge after being hit
                    )

                    # Calculate base damage with buffs
                    attacker = players[user_id]
                    defender = players[target]

                    base_damage = 50 + attacker.get("power_boost", 0)

                    # Calculate critical hit chance for attacker
                    crit_chance = 0.10 + (attacker.get("crit_boost", 0) / 100)
                    is_crit = random.random() < crit_chance
                    if is_crit:
                        base_damage = int(base_damage * 1.5)

                    # Check if target is stunned
                    if target_action == "stunned":
                        # Stunned player always takes damage
                        stunned_damage = base_damage

                        # If attacker is in defense mode, stunned takes half damage
                        if action == "defense":
                            stunned_damage = int(base_damage * 0.5)

                        # Apply damage variance
                        stunned_damage = apply_damage_variance(stunned_damage)

                        if action == "defense":
                            await thread.send(
                                f"🛡️ {format_user(user_id)} attacks stunned {format_user(target)}! {format_user(target)} takes {stunned_damage} damage (reduced)!"
                            )
                        else:
                            await thread.send(
                                f"⚔️ {format_user(user_id)} attacks stunned {format_user(target)}! {format_user(target)} takes {stunned_damage} damage!"
                            )

                        defender["hp"] -= stunned_damage

                        if defender["hp"] <= 0:
                            await thread.send(f"💀 {format_user(target)} has been defeated!")

                        processed.add(user_id)
                        processed.add(target)
                        await asyncio.sleep(1.5)
                        continue

                    if target_action == "defense":
                        # Attack vs Defense: defender takes 10 damage, attacker takes 30 damage
                        defender_damage = 10
                        attacker_damage = 30

                        # Apply shield to defender
                        if defender.get("shield", 0) > 0:
                            defender_damage = int(
                                defender_damage * (1 - defender["shield"] / 100)
                            )

                        # Apply vulnerability to defender
                        if defender.get("vulnerable", 0) > 0:
                            defender_damage = int(
                                defender_damage * (1 + defender["vulnerable"] / 100)
                            )

                        # Apply vulnerability to attacker
                        if attacker.get("vulnerable", 0) > 0:
                            attacker_damage = int(
                                attacker_damage * (1 + attacker["vulnerable"] / 100)
                            )

                        # Apply damage variance
                        defender_damage = apply_damage_variance(defender_damage)
                        attacker_damage = apply_damage_variance(attacker_damage)

                        defender["hp"] -= defender_damage
                        attacker["hp"] -= attacker_damage

                        if is_crit:
                            await thread.send(
                                f"💥 **CRITICAL!** {format_user(user_id)} attacks {format_user(target)} who defends! {format_user(target)} takes {defender_damage} damage, {format_user(user_id)} takes {attacker_damage} damage!"
                            )
                        else:
                            await thread.send(
                                f"⚔️ {format_user(user_id)} attacks {format_user(target)} who defends! {format_user(target)} takes {defender_damage} damage, {format_user(user_id)} takes {attacker_damage} damage!"
                            )

                    elif target_action == "attack":
                        # Attack vs Attack: both take full damage with separate crit rolls
                        # Calculate attacker's damage (already has crit applied to base_damage)
                        attacker_base_damage = base_damage

                        # Calculate defender's damage with separate crit roll
                        defender_base_damage = 50 + defender.get("power_boost", 0)
                        defender_crit_chance = 0.10 + (defender.get("crit_boost", 0) / 100)
                        is_defender_crit = random.random() < defender_crit_chance
                        if is_defender_crit:
                            defender_base_damage = int(defender_base_damage * 1.5)

                        attacker_final_damage = defender_base_damage
                        defender_final_damage = attacker_base_damage

                        # Apply shields
                        if defender.get("shield", 0) > 0:
                            defender_final_damage = int(
                                defender_final_damage * (1 - defender["shield"] / 100)
                            )

                        if attacker.get("shield", 0) > 0:
                            attacker_final_damage = int(
                                attacker_final_damage * (1 - attacker["shield"] / 100)
                            )

                        # Apply vulnerability
                        if defender.get("vulnerable", 0) > 0:
                            defender_final_damage = int(
                                defender_final_damage
                                * (1 + defender["vulnerable"] / 100)
                            )

                        if attacker.get("vulnerable", 0) > 0:
                            attacker_final_damage = int(
                                attacker_final_damage
                                * (1 + attacker["vulnerable"] / 100)
                            )

                        # Apply damage variance
                        defender_final_damage = apply_damage_variance(defender_final_damage)
                        attacker_final_damage = apply_damage_variance(attacker_final_damage)

                        defender["hp"] -= defender_final_damage
                        attacker["hp"] -= attacker_final_damage

                        # Show appropriate message based on who got crits
                        if is_crit and is_defender_crit:
                            await thread.send(
                                f"💥💥 **DOUBLE CRITICAL!** {format_user(user_id)} and {format_user(target)} both land critical hits! {format_user(target)} takes {defender_final_damage} damage, {format_user(user_id)} takes {attacker_final_damage} damage!"
                            )
                        elif is_crit:
                            await thread.send(
                                f"💥 **CRITICAL!** {format_user(user_id)} lands a critical hit! {format_user(target)} takes {defender_final_damage} damage, {format_user(user_id)} takes {attacker_final_damage} damage!"
                            )
                        elif is_defender_crit:
                            await thread.send(
                                f"💥 **CRITICAL!** {format_user(target)} lands a critical hit! {format_user(target)} takes {defender_final_damage} damage, {format_user(user_id)} takes {attacker_final_damage} damage!"
                            )
                        else:
                            await thread.send(
                                f"🔥 {format_user(user_id)} and {format_user(target)} clash! {format_user(target)} takes {defender_final_damage} damage, {format_user(user_id)} takes {attacker_final_damage} damage!"
                            )

                    # Check for deaths
                    if defender["hp"] <= 0:
                        await thread.send(
                            f"💀 {format_user(target)} has been defeated!"
                        )
                    if attacker["hp"] <= 0:
                        await thread.send(
                            f"💀 {format_user(user_id)} has been defeated!"
                        )

                    # Clear one-round buffs/debuffs after combat
                    attacker["power_boost"] = 0
                    attacker["shield"] = 0
                    attacker["crit_boost"] = 0
                    attacker["vulnerable"] = 0
                    defender["power_boost"] = 0
                    defender["shield"] = 0
                    defender["crit_boost"] = 0
                    defender["vulnerable"] = 0

                    processed.add(user_id)
                    processed.add(target)
                    await asyncio.sleep(1.5)

                elif action == "defense":
                    # Defense with no attacker - just announce
                    await thread.send(
                        f"🛡️ {format_user(user_id)} takes a defensive stance!"
                    )
                    processed.add(user_id)
                    await asyncio.sleep(0.8)

            round_num += 1
            await asyncio.sleep(1.5)

            # Safety limit
            if round_num > 30:
                break

        # Determine winner
        team1_alive = get_team_alive(1)
        team2_alive = get_team_alive(2)

        # Handle draw - both teams eliminated
        if len(team1_alive) == 0 and len(team2_alive) == 0:
            await thread.send("\n━━━━━━━━━━━━━━━━━━━━")
            await thread.send("⚔️ **IT'S A DRAW! SUDDEN DEATH!**")
            await asyncio.sleep(2)

            # Revive one random player from each team with 1 HP
            team1_fighter = random.choice(team1_ids)
            team2_fighter = random.choice(team2_ids)

            players[team1_fighter]["hp"] = 1
            players[team2_fighter]["hp"] = 1

            await thread.send(f"\n{format_user(team1_fighter)} vs {format_user(team2_fighter)}")
            await asyncio.sleep(2)

            # 50/50 roll to determine winner (not shown)
            if random.random() < 0.5:
                winning_team = 1
                winning_name = war["team1_name"]
                winners = team1_members
                winner_fighter = team1_fighter
                loser_fighter = team2_fighter
            else:
                winning_team = 2
                winning_name = war["team2_name"]
                winners = team2_members
                winner_fighter = team2_fighter
                loser_fighter = team1_fighter

            await thread.send(f"⚡ {format_user(winner_fighter)} strikes first!")
            await asyncio.sleep(1.5)
            players[loser_fighter]["hp"] = 0
            await thread.send(f"💀 {format_user(loser_fighter)} is defeated!")
            await asyncio.sleep(1.5)

            # Recalculate alive lists after sudden death
            team1_alive = get_team_alive(1)
            team2_alive = get_team_alive(2)

        elif len(team1_alive) > len(team2_alive):
            winning_team = 1
            winning_name = war["team1_name"]
            winners = team1_members
        else:
            winning_team = 2
            winning_name = war["team2_name"]
            winners = team2_members

        await thread.send("\n━━━━━━━━━━━━━━━━━━━━")
        await thread.send(f"🏆 **{winning_name} WINS!**")

        # Show survivors
        survivors = team1_alive if winning_team == 1 else team2_alive
        if survivors:
            survivor_list = "\n".join(
                [
                    f"{format_user(uid)} - {players[uid]['hp']} HP remaining"
                    for uid in survivors
                ]
            )
            await thread.send(f"\n**Survivors:**\n{survivor_list}")

        # Calculate and distribute rewards
        total_pool = sum(m["points_bet"] for m in team1_members) + sum(
            m["points_bet"] for m in team2_members
        )
        tax = int(total_pool * 0.05)
        prize_pool = total_pool - tax

        winner_count = len(winners)
        if winner_count > 0:
            reward_per_winner = prize_pool // winner_count

            async with db.pool.acquire() as conn:
                for winner in winners:
                    await conn.execute(
                        "UPDATE users SET points = points + $1 WHERE user_id = $2",
                        reward_per_winner,
                        winner["user_id"],
                    )

                # Update war status
                await conn.execute(
                    "UPDATE guild_wars SET status = 'finished', winning_team = $1 WHERE id = $2",
                    winning_team,
                    war["id"],
                )

            await thread.send(
                f"\n💰 **Prize Distribution**\n"
                f"Total Pool: {total_pool:,} {Config.POINT_NAME}\n"
                f"Tax (5%): {tax:,} {Config.POINT_NAME}\n"
                f"Each winner receives: **{reward_per_winner:,} {Config.POINT_NAME}**"
            )


def setup(bot):
    bot.add_cog(GuildWar(bot))
