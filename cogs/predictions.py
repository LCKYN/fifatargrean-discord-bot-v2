import asyncio
import datetime
from typing import Optional

import disnake
from disnake.ext import commands, tasks

from core.config import Config
from core.database import db


class BetModal(disnake.ui.Modal):
    """Modal for placing a bet"""

    def __init__(self, prediction_id: int, choice_number: int, choice_text: str):
        self.prediction_id = prediction_id
        self.choice_number = choice_number
        components = [
            disnake.ui.TextInput(
                label=f"Bet amount for: {choice_text[:40]}",
                placeholder="Enter amount of points to bet",
                custom_id="bet_amount",
                style=disnake.TextInputStyle.short,
                min_length=1,
                max_length=10,
            )
        ]
        super().__init__(title="Place Your Bet", components=components)

    async def callback(self, inter: disnake.ModalInteraction):
        try:
            amount = int(inter.text_values["bet_amount"])
            if amount <= 0:
                await inter.response.send_message(
                    "Amount must be positive.", ephemeral=True
                )
                return
        except ValueError:
            await inter.response.send_message(
                "Invalid amount. Enter a number.", ephemeral=True
            )
            return

        async with db.pool.acquire() as conn:
            # Check prediction status
            pred = await conn.fetchrow(
                "SELECT status, ends_at, creator_id FROM predictions WHERE id = $1",
                self.prediction_id,
            )
            if not pred or pred["status"] != "betting":
                await inter.response.send_message(
                    "This prediction is no longer accepting bets.", ephemeral=True
                )
                return

            if datetime.datetime.now() > pred["ends_at"]:
                await inter.response.send_message(
                    "Betting time has ended.", ephemeral=True
                )
                return

            # Check if user is the creator (only block non-mods)
            if pred["creator_id"] == inter.author.id:
                mod_role = inter.guild.get_role(Config.MOD_ROLE_ID)
                if not mod_role or mod_role not in inter.author.roles:
                    await inter.response.send_message(
                        "You cannot bet on your own prediction.", ephemeral=True
                    )
                    return

            # Check if user already bet on THIS choice (can add more)
            existing = await conn.fetchrow(
                "SELECT amount FROM prediction_bets WHERE prediction_id = $1 AND user_id = $2 AND choice_number = $3",
                self.prediction_id,
                inter.author.id,
                self.choice_number,
            )

            # Check user has enough points
            user_points = await conn.fetchval(
                "SELECT points FROM users WHERE user_id = $1", inter.author.id
            )
            user_points = user_points or 0

            if user_points < amount:
                await inter.response.send_message(
                    f"Not enough {Config.POINT_NAME}. You have {user_points}.",
                    ephemeral=True,
                )
                return

            # Deduct points and place/add to bet
            await conn.execute(
                "UPDATE users SET points = points - $1 WHERE user_id = $2",
                amount,
                inter.author.id,
            )
            await conn.execute(
                """INSERT INTO prediction_bets (prediction_id, user_id, choice_number, amount) VALUES ($1, $2, $3, $4)
                   ON CONFLICT (prediction_id, user_id, choice_number) DO UPDATE SET amount = prediction_bets.amount + $4""",
                self.prediction_id,
                inter.author.id,
                self.choice_number,
                amount,
            )

        if existing:
            new_total = existing["amount"] + amount
            await inter.response.send_message(
                f"✅ Bet added! You now have **{new_total} {Config.POINT_NAME}** on choice #{self.choice_number}.",
                ephemeral=True,
            )
        else:
            await inter.response.send_message(
                f"✅ Bet placed! You bet **{amount} {Config.POINT_NAME}** on choice #{self.choice_number}.",
                ephemeral=True,
            )


class PredictionView(disnake.ui.View):
    """View with buttons for each prediction choice"""

    def __init__(
        self, prediction_id: int, choices: list[tuple[int, str]], is_active: bool = True
    ):
        super().__init__(timeout=None)
        self.prediction_id = prediction_id

        # Add a button for each choice
        for choice_num, choice_text in choices:
            button = disnake.ui.Button(
                label=f"{choice_num}. {choice_text[:30]}",
                style=disnake.ButtonStyle.primary
                if is_active
                else disnake.ButtonStyle.secondary,
                custom_id=f"pred_bet_{prediction_id}_{choice_num}",
                disabled=not is_active,
                row=choice_num // 3,  # Max 5 buttons per row
            )
            self.add_item(button)


class CreatePredictionModal(disnake.ui.Modal):
    """Modal for creating a prediction"""

    def __init__(self, num_choices: int, duration: int):
        self.num_choices = num_choices
        self.duration = duration

        components = [
            disnake.ui.TextInput(
                label="Prediction Title",
                placeholder="What are you predicting?",
                custom_id="title",
                style=disnake.TextInputStyle.short,
                max_length=100,
            )
        ]

        # Add text inputs for each choice
        for i in range(1, num_choices + 1):
            required = i <= 2  # First 2 are required
            components.append(
                disnake.ui.TextInput(
                    label=f"Choice {i}"
                    + (" (required)" if required else " (optional)"),
                    placeholder=f"Option {i}",
                    custom_id=f"choice_{i}",
                    style=disnake.TextInputStyle.short,
                    max_length=50,
                    required=required,
                )
            )

        super().__init__(
            title="Create Prediction", components=components[:5]
        )  # Max 5 components
        self.extra_components = components[5:] if len(components) > 5 else []

    async def callback(self, inter: disnake.ModalInteraction):
        title = inter.text_values["title"]

        # Collect choices
        choices = []
        for i in range(1, self.num_choices + 1):
            key = f"choice_{i}"
            if key in inter.text_values and inter.text_values[key].strip():
                choices.append((i, inter.text_values[key].strip()))

        if len(choices) < 2:
            await inter.response.send_message(
                "You need at least 2 choices.", ephemeral=True
            )
            return

        # Get prediction cost
        async with db.pool.acquire() as conn:
            cost_row = await conn.fetchval(
                "SELECT value FROM bot_settings WHERE key = 'prediction_cost'"
            )
            cost = int(cost_row) if cost_row else Config.PREDICTION_COST

            # Check if mod (free) or charge cost
            mod_role = inter.guild.get_role(Config.MOD_ROLE_ID)
            is_mod = mod_role and mod_role in inter.author.roles

            if not is_mod:
                user_points = await conn.fetchval(
                    "SELECT points FROM users WHERE user_id = $1", inter.author.id
                )
                user_points = user_points or 0

                if user_points < cost:
                    await inter.response.send_message(
                        f"Not enough {Config.POINT_NAME}. Creating a prediction costs {cost}. You have {user_points}.",
                        ephemeral=True,
                    )
                    return

                # Deduct cost
                await conn.execute(
                    "UPDATE users SET points = points - $1 WHERE user_id = $2",
                    cost,
                    inter.author.id,
                )

            # Check active predictions count
            active_count = await conn.fetchval(
                "SELECT COUNT(*) FROM predictions WHERE status = 'betting'"
            )
            if active_count >= 5:
                # Refund if charged
                if not is_mod:
                    await conn.execute(
                        "UPDATE users SET points = points + $1 WHERE user_id = $2",
                        cost,
                        inter.author.id,
                    )
                await inter.response.send_message(
                    "Maximum 5 active predictions reached. Please wait for one to finish.",
                    ephemeral=True,
                )
                return

            # Create prediction
            ends_at = datetime.datetime.now() + datetime.timedelta(
                minutes=self.duration
            )
            pred_id = await conn.fetchval(
                """INSERT INTO predictions (title, creator_id, status, ends_at, channel_id)
                   VALUES ($1, $2, 'betting', $3, $4) RETURNING id""",
                title,
                inter.author.id,
                ends_at,
                inter.channel.id,
            )

            # Renumber choices sequentially
            for idx, (_, text) in enumerate(choices, 1):
                await conn.execute(
                    "INSERT INTO prediction_choices (prediction_id, choice_number, choice_text) VALUES ($1, $2, $3)",
                    pred_id,
                    idx,
                    text,
                )

            # Update choices list with sequential numbering
            choices = [(idx, text) for idx, (_, text) in enumerate(choices, 1)]

        # Build and send embed
        embed = build_prediction_embed(
            pred_id, title, choices, ends_at, inter.author, {}, {}
        )
        view = PredictionView(pred_id, choices, is_active=True)

        await inter.response.send_message(embed=embed, view=view)
        msg = await inter.original_message()

        # Save message ID
        async with db.pool.acquire() as conn:
            await conn.execute(
                "UPDATE predictions SET message_id = $1 WHERE id = $2", msg.id, pred_id
            )


def build_prediction_embed(
    pred_id: int,
    title: str,
    choices: list[tuple[int, str]],
    ends_at: datetime.datetime,
    creator: disnake.Member | None,
    pool_by_choice: dict[int, int],
    bettors_by_choice: dict[int, int],
    status: str = "betting",
    winning_choice: int | None = None,
) -> disnake.Embed:
    """Build the prediction embed with stats"""

    total_pool = sum(pool_by_choice.values())

    if status == "betting":
        color = disnake.Color.blue()
        time_left = ends_at - datetime.datetime.now()
        minutes_left = max(0, int(time_left.total_seconds() // 60))
        seconds_left = max(0, int(time_left.total_seconds() % 60))
        status_text = f"⏳ Betting ends in **{minutes_left}m {seconds_left}s**"
    elif status == "locked":
        color = disnake.Color.orange()
        status_text = "🔒 Betting closed - Waiting for result"
    elif status == "resolved":
        color = disnake.Color.green()
        status_text = f"✅ Resolved - Winner: Choice #{winning_choice}"
    else:  # cancelled
        color = disnake.Color.red()
        status_text = "❌ Cancelled - Points refunded"

    embed = disnake.Embed(title=f"🔮 Prediction #{pred_id}: {title}", color=color)

    # Add each choice as a field
    for choice_num, choice_text in choices:
        pool = pool_by_choice.get(choice_num, 0)
        bettors = bettors_by_choice.get(choice_num, 0)

        # Calculate odds (ratio) with 10% tax applied
        if pool > 0 and total_pool > 0:
            raw_ratio = total_pool / pool
            after_tax_ratio = raw_ratio * 0.90  # Apply 10% tax
            odds_text = f"1:{after_tax_ratio:.2f}"
        else:
            odds_text = "1:--"

        is_winner = winning_choice == choice_num
        prefix = "🏆 " if is_winner else ""

        embed.add_field(
            name=f"{prefix}{choice_num}. {choice_text}",
            value=f"💰 **{pool:,}** {Config.POINT_NAME}\n👥 {bettors} bettors\n📊 Return: {odds_text} (after 10% tax)",
            inline=True,
        )

    embed.add_field(
        name="📊 Total Pool",
        value=f"**{total_pool:,}** {Config.POINT_NAME}",
        inline=False,
    )

    embed.set_footer(
        text=f"{status_text} | Created by {creator.display_name if creator else 'Unknown'}"
    )

    return embed


async def get_prediction_data(conn, pred_id: int):
    """Fetch all data needed for a prediction"""
    pred = await conn.fetchrow("SELECT * FROM predictions WHERE id = $1", pred_id)
    if not pred:
        return None, None, None, None

    choices = await conn.fetch(
        "SELECT choice_number, choice_text FROM prediction_choices WHERE prediction_id = $1 ORDER BY choice_number",
        pred_id,
    )
    choices = [(r["choice_number"], r["choice_text"]) for r in choices]

    # Pool by choice
    pool_rows = await conn.fetch(
        "SELECT choice_number, SUM(amount) as total FROM prediction_bets WHERE prediction_id = $1 GROUP BY choice_number",
        pred_id,
    )
    pool_by_choice = {r["choice_number"]: r["total"] for r in pool_rows}

    # Bettors by choice
    bettors_rows = await conn.fetch(
        "SELECT choice_number, COUNT(*) as cnt FROM prediction_bets WHERE prediction_id = $1 GROUP BY choice_number",
        pred_id,
    )
    bettors_by_choice = {r["choice_number"]: r["cnt"] for r in bettors_rows}

    return pred, choices, pool_by_choice, bettors_by_choice


class Predictions(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.update_predictions.start()
        self.check_ended_predictions.start()

    def cog_unload(self):
        self.update_predictions.cancel()
        self.check_ended_predictions.cancel()

    @tasks.loop(seconds=10)
    async def update_predictions(self):
        """Update all active prediction embeds every 10 seconds"""
        if db.pool is None:
            return

        async with db.pool.acquire() as conn:
            active = await conn.fetch(
                "SELECT id, message_id, channel_id FROM predictions WHERE status = 'betting'"
            )

            for pred in active:
                try:
                    channel = self.bot.get_channel(pred["channel_id"])
                    if not channel:
                        continue

                    msg = await channel.fetch_message(pred["message_id"])
                    if not msg:
                        continue

                    (
                        pred_data,
                        choices,
                        pool_by_choice,
                        bettors_by_choice,
                    ) = await get_prediction_data(conn, pred["id"])
                    if not pred_data:
                        continue

                    guild = channel.guild
                    creator = guild.get_member(pred_data["creator_id"])

                    embed = build_prediction_embed(
                        pred["id"],
                        pred_data["title"],
                        choices,
                        pred_data["ends_at"],
                        creator,
                        pool_by_choice,
                        bettors_by_choice,
                        pred_data["status"],
                        pred_data["winning_choice"],
                    )

                    view = PredictionView(pred["id"], choices, is_active=True)
                    await msg.edit(embed=embed, view=view)

                except Exception as e:
                    print(f"Error updating prediction {pred['id']}: {e}")

    @tasks.loop(seconds=5)
    async def check_ended_predictions(self):
        """Check for predictions that have ended and lock them"""
        if db.pool is None:
            return

        async with db.pool.acquire() as conn:
            now = datetime.datetime.now()
            ended = await conn.fetch(
                "SELECT id, message_id, channel_id FROM predictions WHERE status = 'betting' AND ends_at < $1",
                now,
            )

            for pred in ended:
                try:
                    await conn.execute(
                        "UPDATE predictions SET status = 'locked' WHERE id = $1",
                        pred["id"],
                    )

                    # Update the message
                    channel = self.bot.get_channel(pred["channel_id"])
                    if not channel:
                        continue

                    msg = await channel.fetch_message(pred["message_id"])
                    if not msg:
                        continue

                    (
                        pred_data,
                        choices,
                        pool_by_choice,
                        bettors_by_choice,
                    ) = await get_prediction_data(conn, pred["id"])
                    guild = channel.guild
                    creator = guild.get_member(pred_data["creator_id"])

                    embed = build_prediction_embed(
                        pred["id"],
                        pred_data["title"],
                        choices,
                        pred_data["ends_at"],
                        creator,
                        pool_by_choice,
                        bettors_by_choice,
                        "locked",
                    )

                    view = PredictionView(pred["id"], choices, is_active=False)
                    await msg.edit(embed=embed, view=view)

                except Exception as e:
                    print(f"Error locking prediction {pred['id']}: {e}")

    @update_predictions.before_loop
    @check_ended_predictions.before_loop
    async def before_tasks(self):
        await self.bot.wait_until_ready()
        # Wait for db connection
        while db.pool is None:
            await asyncio.sleep(1)

    @commands.Cog.listener()
    async def on_button_click(self, inter: disnake.MessageInteraction):
        """Handle prediction bet buttons"""
        if not inter.component.custom_id.startswith("pred_bet_"):
            return

        parts = inter.component.custom_id.split("_")
        if len(parts) != 4:
            return

        pred_id = int(parts[2])
        choice_num = int(parts[3])

        # Get choice text
        async with db.pool.acquire() as conn:
            choice_text = await conn.fetchval(
                "SELECT choice_text FROM prediction_choices WHERE prediction_id = $1 AND choice_number = $2",
                pred_id,
                choice_num,
            )

        if not choice_text:
            await inter.response.send_message("Invalid choice.", ephemeral=True)
            return

        modal = BetModal(pred_id, choice_num, choice_text)
        await inter.response.send_modal(modal)

    @commands.slash_command(description="Create a new prediction")
    async def predict(
        self,
        inter: disnake.ApplicationCommandInteraction,
        duration: int = commands.Param(
            description="Duration in minutes (1-150)", ge=1, le=150
        ),
        choices: int = commands.Param(
            description="Number of choices (2-5)", ge=2, le=5, default=2
        ),
    ):
        """Start the prediction creation process"""
        # Show modal for title and choices
        modal = CreatePredictionModal(choices, duration)
        await inter.response.send_modal(modal)

    @commands.slash_command(description="Force end betting early on a prediction")
    async def predlock(
        self,
        inter: disnake.ApplicationCommandInteraction,
        prediction_id: int = commands.Param(description="Prediction ID"),
    ):
        """Lock a prediction early to stop betting"""
        async with db.pool.acquire() as conn:
            pred = await conn.fetchrow(
                "SELECT * FROM predictions WHERE id = $1", prediction_id
            )

            if not pred:
                await inter.response.send_message(
                    "Prediction not found.", ephemeral=True
                )
                return

            # Check permission (creator or mod)
            mod_role = inter.guild.get_role(Config.MOD_ROLE_ID)
            is_mod = mod_role and mod_role in inter.author.roles
            is_creator = pred["creator_id"] == inter.author.id

            if not is_mod and not is_creator:
                await inter.response.send_message(
                    "Only the creator or a mod can lock this prediction.",
                    ephemeral=True,
                )
                return

            if pred["status"] != "betting":
                await inter.response.send_message(
                    f"This prediction is already {pred['status']}.", ephemeral=True
                )
                return

            # Lock the prediction
            await conn.execute(
                "UPDATE predictions SET status = 'locked' WHERE id = $1", prediction_id
            )

            (
                pred_data,
                choices,
                pool_by_choice,
                bettors_by_choice,
            ) = await get_prediction_data(conn, prediction_id)

        # Update the message
        try:
            channel = self.bot.get_channel(pred["channel_id"])
            if channel:
                msg = await channel.fetch_message(pred["message_id"])
                creator = inter.guild.get_member(pred["creator_id"])

                embed = build_prediction_embed(
                    prediction_id,
                    pred["title"],
                    choices,
                    pred["ends_at"],
                    creator,
                    pool_by_choice,
                    bettors_by_choice,
                    "locked",
                )

                view = PredictionView(prediction_id, choices, is_active=False)
                await msg.edit(embed=embed, view=view)
        except Exception as e:
            print(f"Error updating locked prediction: {e}")

        await inter.response.send_message(
            f"🔒 Prediction #{prediction_id} has been locked. No more bets allowed. Use `/predresult` to set the winner.",
            ephemeral=True,
        )

    @commands.slash_command(description="Resolve a prediction with a winner")
    async def predresult(
        self,
        inter: disnake.ApplicationCommandInteraction,
        prediction_id: int = commands.Param(description="Prediction ID"),
        winner: int = commands.Param(description="Winning choice number"),
    ):
        """Set the winner of a prediction"""
        async with db.pool.acquire() as conn:
            pred = await conn.fetchrow(
                "SELECT * FROM predictions WHERE id = $1", prediction_id
            )

            if not pred:
                await inter.response.send_message(
                    "Prediction not found.", ephemeral=True
                )
                return

            # Check permission (creator or mod)
            mod_role = inter.guild.get_role(Config.MOD_ROLE_ID)
            is_mod = mod_role and mod_role in inter.author.roles
            is_creator = pred["creator_id"] == inter.author.id

            if not is_mod and not is_creator:
                await inter.response.send_message(
                    "Only the creator or a mod can resolve this prediction.",
                    ephemeral=True,
                )
                return

            if pred["status"] not in ("betting", "locked"):
                await inter.response.send_message(
                    "This prediction has already been resolved or cancelled.",
                    ephemeral=True,
                )
                return

            # Validate winner choice exists
            valid_choice = await conn.fetchval(
                "SELECT 1 FROM prediction_choices WHERE prediction_id = $1 AND choice_number = $2",
                prediction_id,
                winner,
            )
            if not valid_choice:
                await inter.response.send_message(
                    "Invalid winning choice.", ephemeral=True
                )
                return

            # Get all bets
            all_bets = await conn.fetch(
                "SELECT user_id, choice_number, amount FROM prediction_bets WHERE prediction_id = $1",
                prediction_id,
            )

            # Calculate pool and distribute
            total_pool = sum(b["amount"] for b in all_bets)
            winner_bets = [b for b in all_bets if b["choice_number"] == winner]
            winner_pool = sum(b["amount"] for b in winner_bets)

            # Distribute winnings (10% tax)
            payouts = []
            if winner_pool > 0 and total_pool > 0:
                for bet in winner_bets:
                    share = bet["amount"] / winner_pool
                    raw_winnings = int(share * total_pool)
                    # Apply 10% tax: floor the received amount
                    winnings = int(raw_winnings * 0.90)

                    await conn.execute(
                        "UPDATE users SET points = points + $1 WHERE user_id = $2",
                        winnings,
                        bet["user_id"],
                    )
                    payouts.append((bet["user_id"], winnings))

            # Update prediction status
            await conn.execute(
                "UPDATE predictions SET status = 'resolved', winning_choice = $1 WHERE id = $2",
                winner,
                prediction_id,
            )

            # Update message
            (
                pred_data,
                choices,
                pool_by_choice,
                bettors_by_choice,
            ) = await get_prediction_data(conn, prediction_id)

        try:
            channel = self.bot.get_channel(pred["channel_id"])
            if channel:
                msg = await channel.fetch_message(pred["message_id"])
                creator = inter.guild.get_member(pred["creator_id"])

                embed = build_prediction_embed(
                    prediction_id,
                    pred["title"],
                    choices,
                    pred["ends_at"],
                    creator,
                    pool_by_choice,
                    bettors_by_choice,
                    "resolved",
                    winner,
                )

                view = PredictionView(prediction_id, choices, is_active=False)
                await msg.edit(embed=embed, view=view)
        except Exception as e:
            print(f"Error updating resolved prediction: {e}")

        # Get winning choice text
        winning_text = next(
            (text for num, text in choices if num == winner), f"Choice #{winner}"
        )

        # Send announcement to bot channel
        bot_channel = self.bot.get_channel(Config.BOT_CHANNEL_ID)
        if bot_channel:
            total_tax = (
                sum(
                    int((b["amount"] / winner_pool * total_pool) * 0.10)
                    for b in winner_bets
                )
                if winner_pool > 0
                else 0
            )
            total_distributed = sum(p[1] for p in payouts)

            announce_embed = disnake.Embed(
                title=f"🎉 Prediction #{prediction_id} Resolved!",
                description=f"**{pred['title']}**",
                color=disnake.Color.green(),
            )
            announce_embed.add_field(
                name="🏆 Winner", value=f"**{winner}. {winning_text}**", inline=True
            )
            announce_embed.add_field(
                name="💰 Total Pool",
                value=f"{total_pool:,} {Config.POINT_NAME}",
                inline=True,
            )
            announce_embed.add_field(
                name="👥 Winners", value=f"{len(winner_bets)} bettors", inline=True
            )
            announce_embed.add_field(
                name="💸 Distributed",
                value=f"{total_distributed:,} {Config.POINT_NAME} (10% tax: {total_tax:,})",
                inline=False,
            )
            announce_embed.set_footer(text=f"Resolved by {inter.author.display_name}")
            await bot_channel.send(embed=announce_embed)

        await inter.response.send_message(
            f"✅ Prediction #{prediction_id} resolved! Winner: Choice #{winner}\n"
            f"💰 Total pool: {total_pool:,} | Winners: {len(winner_bets)} | Distributed with 10% tax.",
            ephemeral=True,
        )

    @commands.slash_command(
        description="[MOD] Undo a prediction result to change winner"
    )
    async def predundo(
        self,
        inter: disnake.ApplicationCommandInteraction,
        prediction_id: int = commands.Param(description="Prediction ID"),
    ):
        """Undo a resolved prediction to change the winner"""
        mod_role = inter.guild.get_role(Config.MOD_ROLE_ID)
        if not mod_role or mod_role not in inter.author.roles:
            await inter.response.send_message(
                "Only mods can undo predictions.", ephemeral=True
            )
            return

        async with db.pool.acquire() as conn:
            pred = await conn.fetchrow(
                "SELECT * FROM predictions WHERE id = $1", prediction_id
            )

            if not pred:
                await inter.response.send_message(
                    "Prediction not found.", ephemeral=True
                )
                return

            if pred["status"] != "resolved":
                await inter.response.send_message(
                    "Only resolved predictions can be undone.", ephemeral=True
                )
                return

            old_winner = pred["winning_choice"]

            # Get all bets to recalculate and revert
            all_bets = await conn.fetch(
                "SELECT user_id, choice_number, amount FROM prediction_bets WHERE prediction_id = $1",
                prediction_id,
            )

            total_pool = sum(b["amount"] for b in all_bets)
            winner_bets = [b for b in all_bets if b["choice_number"] == old_winner]
            winner_pool = sum(b["amount"] for b in winner_bets)

            # Revert winnings
            if winner_pool > 0 and total_pool > 0:
                for bet in winner_bets:
                    share = bet["amount"] / winner_pool
                    raw_winnings = int(share * total_pool)
                    winnings = int(raw_winnings * 0.90)

                    await conn.execute(
                        "UPDATE users SET points = points - $1 WHERE user_id = $2",
                        winnings,
                        bet["user_id"],
                    )

            # Set back to locked
            await conn.execute(
                "UPDATE predictions SET status = 'locked', winning_choice = NULL WHERE id = $1",
                prediction_id,
            )

            # Update message
            (
                pred_data,
                choices,
                pool_by_choice,
                bettors_by_choice,
            ) = await get_prediction_data(conn, prediction_id)

        try:
            channel = self.bot.get_channel(pred["channel_id"])
            if channel:
                msg = await channel.fetch_message(pred["message_id"])
                creator = inter.guild.get_member(pred["creator_id"])

                embed = build_prediction_embed(
                    prediction_id,
                    pred["title"],
                    choices,
                    pred["ends_at"],
                    creator,
                    pool_by_choice,
                    bettors_by_choice,
                    "locked",
                )

                view = PredictionView(prediction_id, choices, is_active=False)
                await msg.edit(embed=embed, view=view)
        except Exception as e:
            print(f"Error updating undone prediction: {e}")

        await inter.response.send_message(
            f"✅ Prediction #{prediction_id} has been undone. Winnings reverted. Use `/predresult` to set a new winner.",
            ephemeral=True,
        )

    @commands.slash_command(description="[MOD] Cancel a prediction and refund all bets")
    async def predcancel(
        self,
        inter: disnake.ApplicationCommandInteraction,
        prediction_id: int = commands.Param(description="Prediction ID"),
    ):
        """Cancel a prediction and refund everyone"""
        mod_role = inter.guild.get_role(Config.MOD_ROLE_ID)
        if not mod_role or mod_role not in inter.author.roles:
            await inter.response.send_message(
                "Only mods can cancel predictions.", ephemeral=True
            )
            return

        async with db.pool.acquire() as conn:
            pred = await conn.fetchrow(
                "SELECT * FROM predictions WHERE id = $1", prediction_id
            )

            if not pred:
                await inter.response.send_message(
                    "Prediction not found.", ephemeral=True
                )
                return

            if pred["status"] == "cancelled":
                await inter.response.send_message(
                    "This prediction is already cancelled.", ephemeral=True
                )
                return

            # If resolved, first undo the winnings
            if pred["status"] == "resolved":
                old_winner = pred["winning_choice"]
                all_bets = await conn.fetch(
                    "SELECT user_id, choice_number, amount FROM prediction_bets WHERE prediction_id = $1",
                    prediction_id,
                )
                total_pool = sum(b["amount"] for b in all_bets)
                winner_bets = [b for b in all_bets if b["choice_number"] == old_winner]
                winner_pool = sum(b["amount"] for b in winner_bets)

                if winner_pool > 0 and total_pool > 0:
                    for bet in winner_bets:
                        share = bet["amount"] / winner_pool
                        raw_winnings = int(share * total_pool)
                        winnings = int(raw_winnings * 0.90)
                        await conn.execute(
                            "UPDATE users SET points = points - $1 WHERE user_id = $2",
                            winnings,
                            bet["user_id"],
                        )

            # Refund all bets
            all_bets = await conn.fetch(
                "SELECT user_id, amount FROM prediction_bets WHERE prediction_id = $1",
                prediction_id,
            )

            for bet in all_bets:
                await conn.execute(
                    "UPDATE users SET points = points + $1 WHERE user_id = $2",
                    bet["amount"],
                    bet["user_id"],
                )

            # Refund creation cost to creator (if not mod)
            cost_row = await conn.fetchval(
                "SELECT value FROM bot_settings WHERE key = 'prediction_cost'"
            )
            cost = int(cost_row) if cost_row else Config.PREDICTION_COST

            creator_member = inter.guild.get_member(pred["creator_id"])
            creator_was_mod = False
            if creator_member:
                creator_was_mod = mod_role and mod_role in creator_member.roles

            if not creator_was_mod:
                await conn.execute(
                    "UPDATE users SET points = points + $1 WHERE user_id = $2",
                    cost,
                    pred["creator_id"],
                )

            # Update status
            await conn.execute(
                "UPDATE predictions SET status = 'cancelled' WHERE id = $1",
                prediction_id,
            )

            (
                pred_data,
                choices,
                pool_by_choice,
                bettors_by_choice,
            ) = await get_prediction_data(conn, prediction_id)

        try:
            channel = self.bot.get_channel(pred["channel_id"])
            if channel:
                msg = await channel.fetch_message(pred["message_id"])
                creator = inter.guild.get_member(pred["creator_id"])

                embed = build_prediction_embed(
                    prediction_id,
                    pred["title"],
                    choices,
                    pred["ends_at"],
                    creator,
                    pool_by_choice,
                    bettors_by_choice,
                    "cancelled",
                )

                view = PredictionView(prediction_id, choices, is_active=False)
                await msg.edit(embed=embed, view=view)
        except Exception as e:
            print(f"Error updating cancelled prediction: {e}")

        await inter.response.send_message(
            f"✅ Prediction #{prediction_id} cancelled. All {len(all_bets)} bets refunded. Creation cost refunded to creator.",
            ephemeral=True,
        )

    @commands.slash_command(description="[MOD] Set the cost to create a prediction")
    async def predcost(
        self,
        inter: disnake.ApplicationCommandInteraction,
        cost: int = commands.Param(description="New cost in points", ge=0),
    ):
        """Set prediction creation cost"""
        mod_role = inter.guild.get_role(Config.MOD_ROLE_ID)
        if not mod_role or mod_role not in inter.author.roles:
            await inter.response.send_message(
                "Only mods can change prediction cost.", ephemeral=True
            )
            return

        async with db.pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO bot_settings (key, value) VALUES ('prediction_cost', $1)
                   ON CONFLICT (key) DO UPDATE SET value = $1""",
                str(cost),
            )

        await inter.response.send_message(
            f"✅ Prediction creation cost set to **{cost} {Config.POINT_NAME}** (free for mods).",
            ephemeral=True,
        )

    @commands.slash_command(description="Show active predictions")
    async def predictions(self, inter: disnake.ApplicationCommandInteraction):
        """List all active predictions"""
        async with db.pool.acquire() as conn:
            active = await conn.fetch(
                "SELECT id, title, status, ends_at FROM predictions WHERE status IN ('betting', 'locked') ORDER BY id"
            )

        if not active:
            await inter.response.send_message("No active predictions.", ephemeral=True)
            return

        embed = disnake.Embed(
            title="🔮 Active Predictions", color=disnake.Color.purple()
        )

        for pred in active:
            status_emoji = "⏳" if pred["status"] == "betting" else "🔒"
            time_info = ""
            if pred["status"] == "betting":
                time_left = pred["ends_at"] - datetime.datetime.now()
                minutes = max(0, int(time_left.total_seconds() // 60))
                time_info = f" ({minutes}m left)"

            embed.add_field(
                name=f"{status_emoji} #{pred['id']}: {pred['title']}",
                value=f"Status: {pred['status'].title()}{time_info}",
                inline=False,
            )

        await inter.response.send_message(embed=embed, ephemeral=True)


def setup(bot):
    bot.add_cog(Predictions(bot))
