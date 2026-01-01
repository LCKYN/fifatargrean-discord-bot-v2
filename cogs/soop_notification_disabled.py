"""
SOOP Live Stream Notification Cog
Migrated from old code - checks if streamer is live and sends notification

To enable: rename this file to soop_notification.py (remove _disabled)
Required ENV variables:
- SOOP_CLIENT_ID: Your SOOP API client ID
- NOTIFICATION_CHANNEL_ID: Channel ID to send notifications
"""

import asyncio
import time
from datetime import datetime

import aiohttp
import disnake
import pytz
from disnake.ext import commands, tasks

from core.config import Config


class SoopNotification(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.session = None
        # Configurable streamers to monitor
        self.username_mapping = {
            "fifatargrean": {
                "target_channel": Config.NOTIFICATION_CHANNEL_ID,
                "last_send_notification": time.time(),
            },
        }
        self.check_streams.start()

    def cog_unload(self):
        self.check_streams.cancel()
        if self.session:
            asyncio.create_task(self.session.close())

    async def get_session(self):
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession()
        return self.session

    @tasks.loop(minutes=4)
    async def check_streams(self):
        """Check all monitored streams every 4 minutes"""
        for username, data in self.username_mapping.items():
            try:
                stream_info = await self.get_stream_info(username)
                if stream_info.get("isStream"):
                    stream_data = await self.get_stream_data(
                        username, stream_info["data"]
                    )
                    if data["last_send_notification"] < stream_data["start_time"]:
                        await self.send_notification(
                            username, stream_data, data["target_channel"]
                        )
                        self.username_mapping[username]["last_send_notification"] = (
                            time.time()
                        )
            except Exception as e:
                print(f"Error checking stream {username}: {e}")

            await asyncio.sleep(1)

    @check_streams.before_loop
    async def before_check(self):
        await self.bot.wait_until_ready()

    async def fetch_json(self, url: str):
        """Fetch JSON from URL using aiohttp"""
        session = await self.get_session()
        headers = {
            "accept": "application/json",
            "client-id": Config.SOOP_CLIENT_ID,
        }
        async with session.get(url, headers=headers) as response:
            return await response.json()

    async def get_stream_info(self, username: str):
        """Get basic stream info (is live, title, etc.)"""
        url = f"https://api.sooplive.com/stream/info/{username}"
        return await self.fetch_json(url)

    async def get_stream_data(self, username: str, stream_data: dict):
        """Get detailed stream data including viewer count"""
        url_viewer = f"https://api.sooplive.com/stream/info/{username}/live"
        url_info = f"https://api.sooplive.com/channel/info/{username}"

        response_viewer = await self.fetch_json(url_viewer)
        response_info = await self.fetch_json(url_info)

        display_name = response_info["streamerChannelInfo"]["nickname"]

        # Parse start time
        start_time = datetime.strptime(
            stream_data["streamStartDate"], "%Y-%m-%dT%H:%M:%S.%fZ"
        )
        utc_zone = pytz.utc
        thailand_zone = pytz.timezone("Asia/Bangkok")
        start_time = utc_zone.localize(start_time)
        thailand_time = start_time.astimezone(thailand_zone)

        return {
            "display_name": display_name,
            "category": stream_data.get("categoryName", "Just Chatting"),
            "language": stream_data.get("languageCode", "th"),
            "start_time": thailand_time.timestamp(),
            "thumbnail": f"https://api.sooplive.com/media/live/{username}/thumbnail.jpg",
            "title": stream_data["title"],
            "viewer_count": response_viewer.get("viewer", 0),
        }

    async def send_notification(
        self, username: str, stream_data: dict, channel_id: int
    ):
        """Send live notification to Discord channel"""
        channel = self.bot.get_channel(channel_id)
        if not channel:
            print(f"Channel {channel_id} not found for SOOP notification")
            return

        embed = disnake.Embed(
            title=f"🔴 {stream_data['display_name']} is now live on SOOP!",
            description=f"**{stream_data['title']}**",
            url=f"https://www.sooplive.com/{username}",
            color=0xD2FE2C,
        )
        embed.add_field(name="📂 Category", value=stream_data["category"], inline=True)
        embed.add_field(
            name="👥 Viewers", value=str(stream_data["viewer_count"]), inline=True
        )
        embed.set_image(url=stream_data["thumbnail"])
        embed.set_footer(text="SOOP Live")

        # Create watch button
        view = disnake.ui.View()
        view.add_item(
            disnake.ui.Button(
                style=disnake.ButtonStyle.link,
                label="Watch Stream",
                url=f"https://www.sooplive.com/{username}",
            )
        )

        await channel.send(
            content=f"@everyone {stream_data['display_name']} is now live!",
            embed=embed,
            view=view,
        )

    @commands.slash_command(description="[MOD] Add a streamer to monitor")
    async def soopadd(
        self,
        inter: disnake.ApplicationCommandInteraction,
        username: str = commands.Param(description="SOOP username"),
        channel: disnake.TextChannel = commands.Param(
            description="Notification channel"
        ),
    ):
        """Add a streamer to monitor"""
        mod_role = inter.guild.get_role(Config.MOD_ROLE_ID)
        if not mod_role or mod_role not in inter.author.roles:
            await inter.response.send_message(
                "Only mods can add streamers.", ephemeral=True
            )
            return

        self.username_mapping[username] = {
            "target_channel": channel.id,
            "last_send_notification": time.time(),
        }

        await inter.response.send_message(
            f"✅ Now monitoring **{username}** on SOOP. Notifications will go to {channel.mention}",
            ephemeral=True,
        )

    @commands.slash_command(description="[MOD] Remove a streamer from monitoring")
    async def soopremove(
        self,
        inter: disnake.ApplicationCommandInteraction,
        username: str = commands.Param(description="SOOP username to remove"),
    ):
        """Remove a streamer from monitoring"""
        mod_role = inter.guild.get_role(Config.MOD_ROLE_ID)
        if not mod_role or mod_role not in inter.author.roles:
            await inter.response.send_message(
                "Only mods can remove streamers.", ephemeral=True
            )
            return

        if username in self.username_mapping:
            del self.username_mapping[username]
            await inter.response.send_message(
                f"✅ Stopped monitoring **{username}**", ephemeral=True
            )
        else:
            await inter.response.send_message(
                f"**{username}** is not being monitored.", ephemeral=True
            )

    @commands.slash_command(description="Show monitored SOOP streamers")
    async def sooplist(self, inter: disnake.ApplicationCommandInteraction):
        """List all monitored streamers"""
        if not self.username_mapping:
            await inter.response.send_message(
                "No streamers being monitored.", ephemeral=True
            )
            return

        embed = disnake.Embed(title="📺 Monitored SOOP Streamers", color=0xD2FE2C)
        for username, data in self.username_mapping.items():
            channel = self.bot.get_channel(data["target_channel"])
            embed.add_field(
                name=username,
                value=f"Channel: {channel.mention if channel else 'Unknown'}",
                inline=False,
            )

        await inter.response.send_message(embed=embed, ephemeral=True)


def setup(bot):
    bot.add_cog(SoopNotification(bot))
