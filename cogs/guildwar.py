import datetime
import random
import asyncio
from typing import Dict, Set

import disnake
from disnake.ext import commands

from core.config import Config
from core.database import db


class GuildWarView(disnake.ui.View):
    def __init__(self, war_id: int, team1_name: str, team2_name: str, is_active: bool = True):
        super().__init__(timeout=None)
        self.war_id = war_id
        
        # Team 1 button
        team1_button = disnake.ui.Button(
            label=f"Join {team1_name}",
            style=disnake.ButtonStyle.primary,
            custom_id=f"guildwar_{war_id}_team1",
            disabled=not is_active
        )
        team1_button.callback = self.join_team1
        self.add_item(team1_button)
        
        # Team 2 button
        team2_button = disnake.ui.Button(
            label=f"Join {team2_name}",
            style=disnake.ButtonStyle.danger,
            custom_id=f"guildwar_{war_id}_team2",
            disabled=not is_active
        )
        team2_button.callback = self.join_team2
        self.add_item(team2_button)

    async def join_team1(self, interaction: disnake.MessageInteraction):
        await self.join_team(interaction, 1)

    async def join_team2(self, interaction: disnake.MessageInteraction):
        await self.join_team(interaction, 2)

    async def join_team(self, interaction: disnake.MessageInteraction, team_number: int):
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
                    ephemeral=True
                )
                return
            
            # Check if already in a team
            existing = await conn.fetchrow(
                "SELECT team_number FROM guild_war_members WHERE war_id = $1 AND user_id = $2",
                self.war_id, user_id
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
                        team_number, self.war_id, user_id
                    )
                    await interaction.response.send_message(
                        f"You switched to Team {team_number}!", ephemeral=True
                    )
            else:
                # Join team
                await conn.execute(
                    "INSERT INTO guild_war_members (war_id, user_id, team_number, points_bet) VALUES ($1, $2, $3, $4)",
                    self.war_id, user_id, team_number, entry_cost
                )
                
                # Deduct points
                await conn.execute(
                    "UPDATE users SET points = points - $1 WHERE user_id = $2",
                    entry_cost, user_id
                )
                
                await interaction.response.send_message(
                    f"You joined Team {team_number}! (-{entry_cost} {Config.POINT_NAME})", ephemeral=True
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
                    self.war_id
                )
                
                team2_members = await conn.fetch(
                    "SELECT user_id FROM guild_war_members WHERE war_id = $1 AND team_number = 2",
                    self.war_id
                )
                
                embed = disnake.Embed(
                    title=f"⚔️ Guild War: {war['war_name']}",
                    description=f"Entry Cost: **{war['entry_cost']} {Config.POINT_NAME}**",
                    color=disnake.Color.orange()
                )
                
                team1_list = "\n".join([f"<@{m['user_id']}>" for m in team1_members]) or "No members yet"
                team2_list = "\n".join([f"<@{m['user_id']}>" for m in team2_members]) or "No members yet"
                
                embed.add_field(
                    name=f"🔵 {war['team1_name']} ({len(team1_members)})",
                    value=team1_list,
                    inline=True
                )
                
                embed.add_field(
                    name=f"🔴 {war['team2_name']} ({len(team2_members)})",
                    value=team2_list,
                    inline=True
                )
                
                embed.set_footer(text=f"War ID: {self.war_id} | Status: {war['status']}")
                
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
                label="Entry Cost (50-500)",
                placeholder="Enter points required to join (50-500)",
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
            if entry_cost < 50 or entry_cost > 500:
                await inter.response.send_message(
                    "Entry cost must be between 50 and 500.", ephemeral=True
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
                inter.author.id, war_name, team1_name, team2_name, entry_cost,
                datetime.datetime.now()
            )

        # Create thread in guild war channel
        guild_war_channel = self.bot.get_channel(1456851588239986762)
        if not guild_war_channel:
            await inter.followup.send(
                "Guild war channel not found.", ephemeral=True
            )
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
            color=disnake.Color.orange()
        )
        
        embed.add_field(
            name=f"🔵 {team1_name} (0)",
            value="No members yet",
            inline=True
        )
        
        embed.add_field(
            name=f"🔴 {team2_name} (0)",
            value="No members yet",
            inline=True
        )
        
        embed.set_footer(text=f"War ID: {war_id} | Created by {inter.author.display_name}")

        view = GuildWarView(war_id, team1_name, team2_name, is_active=True)
        message = await thread.send(embed=embed, view=view)

        # Update database with thread and message IDs
        async with db.pool.acquire() as conn:
            await conn.execute(
                "UPDATE guild_wars SET thread_id = $1, message_id = $2 WHERE id = $3",
                thread.id, message.id, war_id
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
            war = await conn.fetchrow(
                "SELECT * FROM guild_wars WHERE id = $1", war_id
            )
            
            if not war:
                await inter.response.send_message(
                    "War not found.", ephemeral=True
                )
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
                war_id
            )
            
            team2_members = await conn.fetch(
                "SELECT user_id, points_bet FROM guild_war_members WHERE war_id = $1 AND team_number = 2",
                war_id
            )
            
            if len(team1_members) == 0 or len(team2_members) == 0:
                await inter.response.send_message(
                    "Both teams need at least 1 member to start.", ephemeral=True
                )
                return
            
            # Update status
            await conn.execute(
                "UPDATE guild_wars SET status = 'in_progress' WHERE id = $1",
                war_id
            )

        await inter.response.send_message("⚔️ Starting war...", ephemeral=True)

        # Get thread
        thread = self.bot.get_channel(war["thread_id"])
        if not thread:
            await inter.followup.send("Thread not found.", ephemeral=True)
            return

        # Battle simulation
        await self.simulate_battle(thread, war, team1_members, team2_members)

    async def simulate_battle(self, thread, war, team1_members, team2_members):
        """Simulate the guild war battle"""
        team1_alive = [m["user_id"] for m in team1_members]
        team2_alive = [m["user_id"] for m in team2_members]
        
        battle_events = [
            "{user} charges forward!",
            "{user} defends their position!",
            "{user} launches a fierce attack!",
            "{user} retreats to recover!",
            "{user} runs away from the battle!",
            "{user} strikes {target} with a critical hit!",
            "{user} defeats {target}!",
            "{user} overwhelms {target}!",
            "{user} outmaneuvers {target}!",
        ]
        
        await thread.send("⚔️ **THE BATTLE BEGINS!**\n━━━━━━━━━━━━━━━━━━━━")
        await asyncio.sleep(2)
        
        round_num = 1
        while len(team1_alive) > 0 and len(team2_alive) > 0:
            await thread.send(f"\n**Round {round_num}**")
            
            # Random events
            num_events = random.randint(2, 4)
            for _ in range(num_events):
                event_type = random.choice(["kill", "action"])
                
                if event_type == "kill" and len(team1_alive) > 0 and len(team2_alive) > 0:
                    # Someone gets eliminated
                    attacker_team = random.choice([1, 2])
                    if attacker_team == 1 and len(team1_alive) > 0 and len(team2_alive) > 0:
                        attacker = random.choice(team1_alive)
                        victim = random.choice(team2_alive)
                        team2_alive.remove(victim)
                        await thread.send(f"💀 <@{attacker}> defeats <@{victim}>!")
                    elif len(team2_alive) > 0 and len(team1_alive) > 0:
                        attacker = random.choice(team2_alive)
                        victim = random.choice(team1_alive)
                        team1_alive.remove(victim)
                        await thread.send(f"💀 <@{attacker}> defeats <@{victim}>!")
                else:
                    # Random action
                    all_alive = team1_alive + team2_alive
                    if all_alive:
                        actor = random.choice(all_alive)
                        event = random.choice(battle_events[:5])  # Non-kill events
                        await thread.send(event.format(user=f"<@{actor}>"))
                
                await asyncio.sleep(1.5)
            
            round_num += 1
            await asyncio.sleep(2)
            
            # Safety limit
            if round_num > 20:
                break

        # Determine winner
        if len(team1_alive) > len(team2_alive):
            winning_team = 1
            winning_name = war["team1_name"]
            winners = team1_members
        else:
            winning_team = 2
            winning_name = war["team2_name"]
            winners = team2_members

        await thread.send("\n━━━━━━━━━━━━━━━━━━━━")
        await thread.send(f"🏆 **{winning_name} WINS!**")
        
        # Calculate and distribute rewards
        total_pool = sum(m["points_bet"] for m in team1_members) + sum(m["points_bet"] for m in team2_members)
        tax = int(total_pool * 0.05)
        prize_pool = total_pool - tax
        
        winner_count = len(winners)
        if winner_count > 0:
            reward_per_winner = prize_pool // winner_count
            
            async with db.pool.acquire() as conn:
                for winner in winners:
                    await conn.execute(
                        "UPDATE users SET points = points + $1 WHERE user_id = $2",
                        reward_per_winner, winner["user_id"]
                    )
                
                # Update war status
                await conn.execute(
                    "UPDATE guild_wars SET status = 'finished', winning_team = $1 WHERE id = $2",
                    winning_team, war["id"]
                )
            
            await thread.send(
                f"\n💰 **Prize Distribution**\n"
                f"Total Pool: {total_pool:,} {Config.POINT_NAME}\n"
                f"Tax (5%): {tax:,} {Config.POINT_NAME}\n"
                f"Each winner receives: **{reward_per_winner:,} {Config.POINT_NAME}**"
            )


def setup(bot):
    bot.add_cog(GuildWar(bot))
