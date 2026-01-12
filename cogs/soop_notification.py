"""
SOOP Live Stream Notification Cog
Migrated from old code - checks if streamer is live and sends notification

To enable: rename this file to soop_notification.py (remove _disabled)
Required ENV variables:
- NOTIFICATION_CHANNEL_ID: Channel ID to send notifications

Optional:
- SOOP_COOKIES: Cookie string (defaults to: AbroadChk=OK; AbroadVod=OK)
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

    @tasks.loop(minutes=6)
    async def check_streams(self):
        """Check all monitored streams every 6 minutes"""
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

    async def get_stream_info(self, username: str):
        """Get stream info from SOOP API"""
        try:
            session = await self.get_session()
            url = f"https://api-channel.sooplive.co.kr/v1.1/channel/{username}/home/section/broad"

            # Get cookies from config, default to minimal required cookies
            cookies_str = getattr(Config, "SOOP_COOKIES", "AbroadChk=OK; AbroadVod=OK")

            headers = {
                "accept": "application/json, text/plain, */*",
                "accept-language": "en-US,en;q=0.9",
                "Cookie": cookies_str,
            }

            async with session.get(url, headers=headers) as response:
                if response.status == 200:
                    data = await response.json()
                    # If broadNo exists and is not 0, streamer is live
                    is_live = data.get("broadNo", 0) != 0
                    return {"isStream": is_live, "data": data if is_live else None}
                else:
                    return {"isStream": False}
        except Exception as e:
            print(f"Error fetching stream info for {username}: {e}")
            return {"isStream": False}

    async def get_stream_data(self, username: str, stream_data: dict):
        """Parse stream data from SOOP API response"""
        # Parse start time from ISO format: "2026-01-12T13:35:56.000Z"
        broad_start = stream_data.get("broadStart", "")

        if broad_start:
            try:
                # Parse ISO format timestamp (UTC)
                start_dt = datetime.strptime(broad_start, "%Y-%m-%dT%H:%M:%S.%fZ")
                utc_zone = pytz.utc
                thailand_zone = pytz.timezone("Asia/Bangkok")
                start_dt = utc_zone.localize(start_dt)
                thailand_time = start_dt.astimezone(thailand_zone)
                start_time = thailand_time.timestamp()
            except Exception as e:
                print(f"Error parsing start time: {e}")
                start_time = time.time()
        else:
            start_time = time.time()

        # Get thumbnail URL - construct from broadNo
        broad_no = stream_data.get("broadNo", "")
        thumbnail = f"https://liveimg.sooplive.co.kr/m/{broad_no}" if broad_no else ""

        # Get language from langTags or default to Thai
        lang_tags = stream_data.get("langTags", [])
        language = lang_tags[0] if lang_tags else "th"

        return {
            "display_name": stream_data.get("userId", username),
            "category": stream_data.get("categoryName", "Live"),
            "language": language,
            "start_time": start_time,
            "thumbnail": thumbnail,
            "title": stream_data.get("broadTitle", "Live Stream"),
            "viewer_count": stream_data.get("currentSumViewer", 0),
            "broad_no": broad_no,
        }

    async def send_notification(
        self, username: str, stream_data: dict, channel_id: int
    ):
        """Send live notification to Discord channel"""
        channel = self.bot.get_channel(channel_id)
        if not channel:
            print(f"Channel {channel_id} not found for SOOP notification")
            return

        # Use play.sooplive.co.kr with broadNo
        stream_url = f"https://play.sooplive.co.kr/{username}/{stream_data['broad_no']}"

        embed = disnake.Embed(
            title=f"🔴 {stream_data['display_name']} is now live on SOOP!",
            description=f"**{stream_data['title']}**",
            url=stream_url,
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
                url=stream_url,
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

    @commands.slash_command(description="SOOP stream commands")
    async def soop(self, inter: disnake.ApplicationCommandInteraction):
        """Base command for SOOP stream commands"""
        pass

    @soop.sub_command(description="[MOD] Manually send live notification")
    async def notify(
        self,
        inter: disnake.ApplicationCommandInteraction,
    ):
        """Manually send a live notification with @everyone tag"""
        mod_role = inter.guild.get_role(Config.MOD_ROLE_ID)
        if not mod_role or mod_role not in inter.author.roles:
            await inter.response.send_message(
                "Only mods can send notifications.", ephemeral=True
            )
            return

        await inter.response.defer(ephemeral=True)
        username = "fifatargrean"

        try:
            stream_info = await self.get_stream_info(username)

            if not stream_info.get("isStream"):
                await inter.followup.send(
                    f"❌ **{username}** is not currently live.", ephemeral=True
                )
                return

            stream_data = await self.get_stream_data(username, stream_info["data"])

            # Use configured channel or notification channel
            channel_id = self.username_mapping.get(username, {}).get(
                "target_channel", Config.NOTIFICATION_CHANNEL_ID
            )

            await self.send_notification(username, stream_data, channel_id)

            await inter.followup.send(
                f"✅ Sent notification for **{username}**!", ephemeral=True
            )
        except Exception as e:
            await inter.followup.send(
                f"❌ Error sending notification: {e}", ephemeral=True
            )

    @soop.sub_command(description="Check if SOOP streamer is live")
    async def check(
        self,
        inter: disnake.ApplicationCommandInteraction,
    ):
        """Check live status without sending notification"""
        await inter.response.defer()
        username = "fifatargrean"

        try:
            stream_info = await self.get_stream_info(username)

            if not stream_info.get("isStream"):
                embed = disnake.Embed(
                    title="📴 Offline",
                    description=f"**{username}** is not currently live.",
                    color=0x808080,
                )
                embed.set_footer(text="SOOP Live")
                await inter.followup.send(embed=embed)
                return

            stream_data = await self.get_stream_data(username, stream_info["data"])

            # Use play.sooplive.co.kr with broadNo
            stream_url = (
                f"https://play.sooplive.co.kr/{username}/{stream_data['broad_no']}"
            )

            embed = disnake.Embed(
                title=f"🔴 {stream_data['display_name']} is LIVE!",
                description=f"**{stream_data['title']}**",
                url=stream_url,
                color=0xD2FE2C,
            )
            embed.add_field(
                name="📂 Category", value=stream_data["category"], inline=True
            )
            embed.add_field(
                name="👥 Viewers", value=str(stream_data["viewer_count"]), inline=True
            )

            # Add start time
            start_dt = datetime.fromtimestamp(stream_data["start_time"])
            thailand_zone = pytz.timezone("Asia/Bangkok")
            start_dt = thailand_zone.localize(start_dt)
            embed.add_field(
                name="🕒 Started",
                value=f"<t:{int(stream_data['start_time'])}:R>",
                inline=True,
            )

            embed.set_image(url=stream_data["thumbnail"])
            embed.set_footer(text="SOOP Live")

            # Create watch button
            view = disnake.ui.View()
            view.add_item(
                disnake.ui.Button(
                    style=disnake.ButtonStyle.link,
                    label="Watch Stream",
                    url=stream_url,
                )
            )

            await inter.followup.send(embed=embed, view=view)

        except Exception as e:
            await inter.followup.send(f"❌ Error checking stream: {e}")


def setup(bot):
    bot.add_cog(SoopNotification(bot))
