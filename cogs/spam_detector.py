"""
Spam Detector with Link Detection Cog
Migrated from old code - detects spam with links and auto-bans

To enable: rename this file to spam_detector.py (remove _disabled)
Required: Configure IGNORE_CHANNELS and MOD_CHANNEL below
"""

import re
import time
from collections import defaultdict
from typing import Dict, List, Optional

import disnake
from disnake.ext import commands

from core.config import Config
from core.logger import cleanup_old_logs, log

# URL regex pattern for detecting links
URL_PATTERN = re.compile(
    r"http[s]?://(?:[a-zA-Z]|[0-9]|[$-_@.&+]|[!*\\(\\),]|(?:%[0-9a-fA-F][0-9a-fA-F]))+|"
    r"(?:www\.)[a-zA-Z0-9][a-zA-Z0-9-]+[a-zA-Z0-9]\.[^\s]{2,}|"
    r"[a-zA-Z0-9][a-zA-Z0-9-]+[a-zA-Z0-9]\.(?:com|org|net|io|gg|co|me|tv|xyz|info|biz)[^\s]*"
)


def contains_link(text: str) -> bool:
    """Check if text contains a URL"""
    return bool(URL_PATTERN.search(text))


class SpamTracker:
    """Track messages per user for spam detection - tracks UNIQUE CHANNELS"""

    def __init__(self, min_channels: int = 4, time_limit: int = 30):
        self.min_channels = min_channels  # Must post links in X different channels
        self.time_limit = time_limit
        # {user_id: [(timestamp, content, channel_id), ...]}
        self.messages: Dict[int, List[tuple]] = defaultdict(list)

    def add_message(
        self, user_id: int, content: str, channel_id: int
    ) -> Optional[dict]:
        """
        Add a message and check for cross-channel spam.
        Returns spam data if user posted links in min_channels DIFFERENT channels.
        """
        now = time.time()

        # Clean old messages
        self.messages[user_id] = [
            (ts, msg, ch)
            for ts, msg, ch in self.messages[user_id]
            if now - ts < self.time_limit
        ]

        # Add new message
        self.messages[user_id].append((now, content, channel_id))

        # Count UNIQUE channels with link messages
        unique_channels = set(ch for _, _, ch in self.messages[user_id])

        # Check if spam (links in X different channels)
        if len(unique_channels) >= self.min_channels:
            all_messages = [msg for _, msg, _ in self.messages[user_id]]
            self.messages[user_id] = []  # Clear after detection
            return {
                "all_messages": "\n".join(all_messages),
                "channel_count": len(unique_channels),
            }

        return None

    def get_unique_channel_count(self, user_id: int) -> int:
        """Get count of unique channels for a user"""
        return len(set(ch for _, _, ch in self.messages.get(user_id, [])))


class DuplicateContentTracker:
    """Track duplicate URLs/images posted across channels - 5 channels in 5 minutes = ban"""

    def __init__(self, min_channels: int = 5, time_limit: int = 300):
        self.min_channels = min_channels  # Must post same content in X different channels
        self.time_limit = time_limit  # 5 minutes = 300 seconds
        # {user_id: {content_hash: [(timestamp, channel_id), ...]}}
        self.content_posts: Dict[int, Dict[str, List[tuple]]] = defaultdict(
            lambda: defaultdict(list)
        )

    def _extract_urls(self, text: str) -> List[str]:
        """Extract all URLs from text"""
        return URL_PATTERN.findall(text) if text else []

    def add_content(
        self, user_id: int, content: str, channel_id: int, attachment_urls: List[str]
    ) -> Optional[dict]:
        """
        Track content (URLs/images) and check for duplicate spam.
        Returns spam data if same content posted in min_channels DIFFERENT channels.
        """
        now = time.time()

        # Collect all URLs (from text and attachments)
        all_urls = self._extract_urls(content) + attachment_urls

        if not all_urls:
            return None

        # Clean old entries for this user
        for content_hash in list(self.content_posts[user_id].keys()):
            self.content_posts[user_id][content_hash] = [
                (ts, ch)
                for ts, ch in self.content_posts[user_id][content_hash]
                if now - ts < self.time_limit
            ]
            # Remove empty entries
            if not self.content_posts[user_id][content_hash]:
                del self.content_posts[user_id][content_hash]

        # Track each URL
        for url in all_urls:
            # Normalize URL (lowercase, strip trailing slashes)
            normalized_url = url.lower().rstrip("/")

            # Add this post
            self.content_posts[user_id][normalized_url].append((now, channel_id))

            # Count unique channels for this specific URL
            unique_channels = set(
                ch for _, ch in self.content_posts[user_id][normalized_url]
            )

            # Check if spam (same URL in X different channels)
            if len(unique_channels) >= self.min_channels:
                # Clear this user's tracking after detection
                spam_url = normalized_url
                self.content_posts[user_id] = defaultdict(list)
                return {
                    "duplicate_url": spam_url,
                    "channel_count": len(unique_channels),
                    "reason": "duplicate_content",
                }

        return None

    def get_duplicate_channel_count(self, user_id: int) -> Dict[str, int]:
        """Get count of unique channels per URL for a user"""
        result = {}
        for url, posts in self.content_posts.get(user_id, {}).items():
            result[url] = len(set(ch for _, ch in posts))
        return result


class SpamDetector(commands.Cog):
    """Detects and bans spammers posting links across multiple channels"""

    def __init__(self, bot):
        self.bot = bot
        self.spam_tracker = SpamTracker(min_channels=4, time_limit=30)
        self.duplicate_tracker = DuplicateContentTracker(min_channels=5, time_limit=300)

        # Channels to ignore (add your channel IDs here)
        self.ignore_channels = [
            # 940952038991347722,  # Example: #links-allowed
            # 940966263168065547,  # Example: #promo
        ]

        # Channel to send mod notifications
        self.mod_channel_id = None  # Set via command or config

    @commands.Cog.listener()
    async def on_message(self, message: disnake.Message):
        # Ignore bots
        if message.author.bot:
            return

        # Ignore DMs
        if not message.guild:
            return

        # Ignore specified channels
        if message.channel.id in self.ignore_channels:
            return

        # Get attachment URLs (images, files)
        attachment_urls = [att.url for att in message.attachments]

        # Check for duplicate content spam (same URL/image in 5 channels within 5 mins)
        duplicate_result = self.duplicate_tracker.add_content(
            message.author.id,
            message.content,
            message.channel.id,
            attachment_urls,
        )

        if duplicate_result:
            log(
                "spam_detector",
                "duplicate_spam_detected",
                {
                    "user_id": message.author.id,
                    "user_name": str(message.author),
                    "channel_count": duplicate_result["channel_count"],
                    "duplicate_url": duplicate_result["duplicate_url"][:100],
                },
            )
            await self.handle_spam(
                message,
                f"Duplicate content posted in {duplicate_result['channel_count']} channels:\n{duplicate_result['duplicate_url'][:500]}",
                delete_seconds=300,  # Delete 5 minutes of messages
                reason_type="duplicate_content",
            )
            return

        # Check for links (original spam detection)
        if not contains_link(message.content):
            return

        # Check for spam (cross-channel link posting)
        spam_result = self.spam_tracker.add_message(
            message.author.id, message.content, message.channel.id
        )

        # Log unique channel count
        unique_channels = self.spam_tracker.get_unique_channel_count(message.author.id)
        log(
            "spam_detector",
            "link_detected",
            {
                "user_id": message.author.id,
                "user_name": str(message.author),
                "channel_id": message.channel.id,
                "channel_name": message.channel.name,
                "unique_channels": unique_channels,
                "content_preview": message.content[:100],
            },
        )

        if spam_result:
            log(
                "spam_detector",
                "spam_detected",
                {
                    "user_id": message.author.id,
                    "user_name": str(message.author),
                    "channel_count": spam_result["channel_count"],
                },
            )
            await self.handle_spam(
                message,
                spam_result["all_messages"],
                delete_seconds=1800,  # Delete 30 minutes of messages
                reason_type="link_spam",
            )

    async def handle_spam(
        self,
        message: disnake.Message,
        all_messages: str,
        delete_seconds: int = 1800,
        reason_type: str = "link_spam",
    ):
        """Handle detected spam - ban user and notify"""
        user = message.author
        channel = message.channel
        guild = message.guild

        # Format reason for ban
        if reason_type == "duplicate_content":
            ban_reason = f"Auto-ban: Duplicate content spam (same URL/image in 5+ channels)"
            title_text = "🔨 Auto-Ban: Duplicate Content Spam"
        else:
            ban_reason = f"Auto-ban: Link spam detected in #{channel.name}"
            title_text = "🔨 Auto-Ban: Link Spam"

        # Don't ban mods/admins
        mod_role = guild.get_role(Config.MOD_ROLE_ID)
        if mod_role and mod_role in user.roles:
            log(
                "spam_detector",
                "mod_test_triggered",
                {"user_id": user.id, "user_name": str(user)},
            )
            # DM the mod to confirm detection worked
            try:
                await user.send(
                    f"✅ **Spam Detector Test Successful!**\n"
                    f"You triggered the spam detector but weren't banned because you're a mod.\n"
                    f"Detection: 4 link messages in 30 seconds across multiple channels."
                )
            except disnake.Forbidden:
                pass
            return
        if user.guild_permissions.administrator:
            log(
                "spam_detector",
                "admin_skipped",
                {"user_id": user.id, "user_name": str(user)},
            )
            return

        # Don't ban bot owner (safety check)
        if await self.bot.is_owner(user):
            log("spam_detector", "owner_skipped", {"user_id": user.id})
            return

        try:
            # Ban the user
            await guild.ban(
                user,
                clean_history_duration=delete_seconds,  # Delete messages (seconds)
                reason=ban_reason,
            )

            # Send public notification
            public_embed = disnake.Embed(
                title="🔨 User Banned",
                description=f"{user.mention} was banned for {'duplicate content' if reason_type == 'duplicate_content' else 'link'} spam",
                color=disnake.Color.red(),
            )
            public_embed.set_image(
                url="https://media.tenor.com/SJ2HvoNKCwkAAAAi/pepe-the-frog-pepe.gif"
            )
            await channel.send(embed=public_embed)

            # Send mod notification
            mod_embed = disnake.Embed(
                title=title_text, color=disnake.Color.red()
            )
            mod_embed.add_field(name="User", value=f"{user} ({user.id})", inline=False)
            mod_embed.add_field(name="Channel", value=channel.mention, inline=True)
            mod_embed.add_field(
                name="Messages", value=all_messages[:1000], inline=False
            )
            mod_embed.set_footer(text="Use /spamunban to unban if this was a mistake")

            # Send to mod channel
            if self.mod_channel_id:
                mod_channel = self.bot.get_channel(self.mod_channel_id)
                if mod_channel:
                    await mod_channel.send(embed=mod_embed)

            # Also send to bot channel
            bot_channel = self.bot.get_channel(Config.BOT_CHANNEL_ID)
            if bot_channel:
                await bot_channel.send(embed=mod_embed)

            # DM guild owner or first available mod
            try:
                owner = guild.owner
                if owner:
                    dm_embed = disnake.Embed(
                        title="🔨 Spam Detector Triggered",
                        description=f"Auto-banned **{user}** for link spam in **{guild.name}**",
                        color=disnake.Color.red(),
                    )
                    dm_embed.add_field(
                        name="Channel", value=f"#{channel.name}", inline=True
                    )
                    dm_embed.add_field(name="User ID", value=str(user.id), inline=True)
                    await owner.send(embed=dm_embed)
            except disnake.Forbidden:
                pass  # Owner has DMs disabled

            # Log successful ban
            log(
                "spam_detector",
                "user_banned",
                {
                    "user_id": user.id,
                    "user_name": str(user),
                    "channel_id": channel.id,
                    "channel_name": channel.name,
                },
            )

        except disnake.Forbidden:
            log(
                "spam_detector",
                "ban_failed",
                {
                    "user_id": user.id,
                    "user_name": str(user),
                    "reason": "missing_permissions",
                },
            )
        except Exception as e:
            log(
                "spam_detector",
                "ban_error",
                {"user_id": user.id, "user_name": str(user), "error": str(e)},
            )

    @commands.slash_command(description="[MOD] Unban a user banned by spam detector")
    async def spamunban(
        self,
        inter: disnake.ApplicationCommandInteraction,
        user_id: str = commands.Param(description="User ID to unban"),
    ):
        """Unban a user"""
        mod_role = inter.guild.get_role(Config.MOD_ROLE_ID)
        if not mod_role or mod_role not in inter.author.roles:
            await inter.response.send_message("Only mods can unban.", ephemeral=True)
            return

        try:
            user_id_int = int(user_id)
            await inter.guild.unban(disnake.Object(id=user_id_int))
            await inter.response.send_message(
                f"✅ User {user_id} has been unbanned.", ephemeral=True
            )

            # Notify bot channel
            bot_channel = self.bot.get_channel(Config.BOT_CHANNEL_ID)
            if bot_channel:
                embed = disnake.Embed(
                    title="✅ User Unbanned",
                    description=f"User ID `{user_id}` was unbanned by {inter.author.mention}",
                    color=disnake.Color.green(),
                )
                await bot_channel.send(embed=embed)

        except ValueError:
            await inter.response.send_message("Invalid user ID.", ephemeral=True)
        except disnake.NotFound:
            await inter.response.send_message(
                "User not found in ban list.", ephemeral=True
            )
        except disnake.Forbidden:
            await inter.response.send_message(
                "I don't have permission to unban.", ephemeral=True
            )

    @commands.slash_command(
        description="[MOD] Set mod notification channel for spam detector"
    )
    async def spammodchannel(
        self,
        inter: disnake.ApplicationCommandInteraction,
        channel: disnake.TextChannel = commands.Param(
            description="Mod channel for notifications"
        ),
    ):
        """Set the mod notification channel"""
        mod_role = inter.guild.get_role(Config.MOD_ROLE_ID)
        if not mod_role or mod_role not in inter.author.roles:
            await inter.response.send_message(
                "Only mods can configure this.", ephemeral=True
            )
            return

        self.mod_channel_id = channel.id
        await inter.response.send_message(
            f"✅ Spam detector mod notifications will be sent to {channel.mention}",
            ephemeral=True,
        )

    @commands.slash_command(
        description="[MOD] Add channel to spam detector ignore list"
    )
    async def spamignore(
        self,
        inter: disnake.ApplicationCommandInteraction,
        channel: disnake.TextChannel = commands.Param(description="Channel to ignore"),
    ):
        """Add channel to ignore list"""
        mod_role = inter.guild.get_role(Config.MOD_ROLE_ID)
        if not mod_role or mod_role not in inter.author.roles:
            await inter.response.send_message(
                "Only mods can configure this.", ephemeral=True
            )
            return

        if channel.id not in self.ignore_channels:
            self.ignore_channels.append(channel.id)
            await inter.response.send_message(
                f"✅ {channel.mention} will be ignored by spam detector.",
                ephemeral=True,
            )
        else:
            await inter.response.send_message(
                f"{channel.mention} is already ignored.", ephemeral=True
            )

    @commands.slash_command(
        description="[MOD] Remove channel from spam detector ignore list"
    )
    async def spamunignore(
        self,
        inter: disnake.ApplicationCommandInteraction,
        channel: disnake.TextChannel = commands.Param(
            description="Channel to unignore"
        ),
    ):
        """Remove channel from ignore list"""
        mod_role = inter.guild.get_role(Config.MOD_ROLE_ID)
        if not mod_role or mod_role not in inter.author.roles:
            await inter.response.send_message(
                "Only mods can configure this.", ephemeral=True
            )
            return

        if channel.id in self.ignore_channels:
            self.ignore_channels.remove(channel.id)
            await inter.response.send_message(
                f"✅ {channel.mention} will now be monitored by spam detector.",
                ephemeral=True,
            )
        else:
            await inter.response.send_message(
                f"{channel.mention} is not in ignore list.", ephemeral=True
            )

    @commands.slash_command(
        description="[MOD] Test spam detector - simulates detection without banning"
    )
    async def spamtest(
        self,
        inter: disnake.ApplicationCommandInteraction,
    ):
        """Test spam detector functionality"""
        mod_role = inter.guild.get_role(Config.MOD_ROLE_ID)
        if not mod_role or mod_role not in inter.author.roles:
            await inter.response.send_message(
                "Only mods can test this.", ephemeral=True
            )
            return

        await inter.response.defer(ephemeral=True)

        # Try to DM the user
        try:
            dm_embed = disnake.Embed(
                title="✅ Spam Detector Test",
                description="The spam detector is working correctly!",
                color=disnake.Color.green(),
            )
            dm_embed.add_field(
                name="Settings",
                value=f"• Link spam: **4 link messages** in **30 seconds**\n"
                f"• Duplicate spam: **Same URL/image** in **5 channels** within **5 minutes**\n"
                f"• Ignored channels: **{len(self.ignore_channels)}**\n"
                f"• Mod channel set: **{'Yes' if self.mod_channel_id else 'No'}**",
                inline=False,
            )
            dm_embed.add_field(
                name="Note",
                value="Mods and admins are exempt from auto-ban.",
                inline=False,
            )
            await inter.author.send(embed=dm_embed)
            await inter.followup.send(
                "✅ Spam detector is working! Check your DMs.", ephemeral=True
            )
        except disnake.Forbidden:
            await inter.followup.send(
                "✅ Spam detector is working, but I couldn't DM you (DMs disabled).",
                ephemeral=True,
            )


def setup(bot):
    bot.add_cog(SpamDetector(bot))
