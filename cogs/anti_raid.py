"""
cogs/anti_raid.py — Module chống raid.
Cơ chế khi member join:
  1. Mass join: 10 người / 20 giây → lockdown toàn server
  2. Account age: mới hơn 1 ngày → kick
  3. Pattern name: user123, member123... → quarantine
  4. Default avatar → quarantine
  5. Captcha: gán Unverified role → verify trong 60s, không thì kick
- Owner/Co-owner + whitelist bypass mọi check
- Invite link từ server lạ do anti_content xử lý
"""

import re
import time
import asyncio
import logging
from datetime import datetime, timezone
from collections import deque

import discord
from discord.ext import commands

import config
from utils import captcha as captcha_util

log = logging.getLogger("bot.anti_raid")

# Pattern tên nghi vấn: user1234, member99, newuser5, acc123, test01...
SUSPICIOUS_NAME_RE = re.compile(
    r"^(?:user|member|newuser|acc(?:ount)?|test|bot|raid)[\s_\-.]*\d+$",
    re.IGNORECASE,
)


class AntiRaid(commands.Cog):
    """Cog chống raid — xử lý chuỗi check khi member join."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.db = bot.db
        # Lịch sử join per guild: {guild_id: deque[timestamp]}
        self._joins: dict = {}
        # Đang lockdown: set[guild_id] — tránh lockdown lặp
        self._lockdown_guilds: set[int] = set()
        # Task captcha đang chờ: {(guild_id, user_id): asyncio.Task}
        self._pending_captcha: dict = {}

    def cog_unload(self):
        # Hủy mọi task captcha đang chờ
        for task in self._pending_captcha.values():
            task.cancel()

    # ========================================================
    # 🔧 HELPERS
    # ========================================================
    async def _is_bypassed(self, guild_id: int, user_id: int) -> bool:
        if config.is_owner_or_coowner(user_id):
            return True
        return await self.db.is_whitelisted(guild_id, user_id)

    async def _log(self, guild, title, desc, color=discord.Color.red(), **kw):
        logger = self.bot.get_cog("Logger")
        if logger:
            await logger.send_log(guild, title, desc, color, **kw)

    async def _get_or_create_role(
        self, guild: discord.Guild, config_key: str, name: str,
        deny_send: bool = True,
    ) -> discord.Role | None:
        """Lấy role từ config; chưa có → tạo + chặn quyền gửi tin."""
        try:
            role_id = await self.db.get_config(guild.id, config_key)
            role = guild.get_role(role_id) if role_id else None
            if role:
                return role
            role = await guild.create_role(
                name=name, reason="Auto-created by anti-raid",
                color=discord.Color.dark_gray(),
            )
            await self.db.set_config(guild.id, config_key, role.id)
            if deny_send:
                for channel in guild.channels:
                    try:
                        await channel.set_permissions(
                            role, send_messages=False, add_reactions=False,
                            speak=False, connect=False,
                            reason=f"Setup {name} role",
                        )
                    except discord.HTTPException:
                        continue
            return role
        except discord.Forbidden:
            log.warning("No permission to create role %s in guild %s", name, guild.id)
        except Exception:
            log.exception("Error creating role %s", name)
        return None

    async def _quarantine(self, member: discord.Member, reason: str):
        """Gán quarantine role + log."""
        try:
            role = await self._get_or_create_role(
                member.guild, "role_quarantine", "Quarantine")
            if role:
                await member.add_roles(role, reason=reason)
            await self._log(
                member.guild, "🔒 Quarantine",
                f"{member.mention} bị cách ly: **{reason}**",
                discord.Color.orange(), user=member,
            )
            try:
                await member.send(
                    f"🔒 Bạn bị cách ly tại **{member.guild.name}**: {reason}\n"
                    f"Liên hệ mod để được mở.")
            except (discord.Forbidden, discord.HTTPException):
                pass
        except Exception:
            log.exception("Error quarantining member")

    # ========================================================
    # 👋 MAIN — chuỗi check khi member join
    # ========================================================
    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        try:
            guild = member.guild
            if member.bot:
                return  # bot do anti_nuke xử lý
            if not await self.db.is_module_enabled(guild.id, "antiraid"):
                return
            # 🔐 Bypass TRƯỚC mọi check
            if await self._is_bypassed(guild.id, member.id):
                return

            # 1️⃣ Mass join detect (chạy trước — kể cả member hợp lệ vẫn đếm)
            await self._check_mass_join(guild)

            # 2️⃣ Account age — kick nếu account quá mới
            if await self._check_account_age(member):
                return  # đã kick

            # 3️⃣ Pattern name → quarantine
            if await self.db.get_config(guild.id, "antiraid_pattern_detect"):
                if SUSPICIOUS_NAME_RE.match(member.name):
                    await self._quarantine(member, f"Tên nghi vấn: `{member.name}`")
                    return

            # 4️⃣ Default avatar → quarantine
            if await self.db.get_config(guild.id, "antiraid_default_avatar_check"):
                if member.avatar is None:
                    await self._quarantine(member, "Không có avatar (default)")
                    return

            # 5️⃣ Captcha verify — gán Unverified, chờ verify
            await self._start_captcha(member)
        except Exception:
            log.exception("Error in anti-raid on_member_join")

    # ========================================================
    # 1️⃣ MASS JOIN → LOCKDOWN
    # ========================================================
    async def _check_mass_join(self, guild: discord.Guild):
        threshold = await self.db.get_config(guild.id, "antiraid_join_threshold")
        window = await self.db.get_config(guild.id, "antiraid_join_window")

        now = time.time()
        joins = self._joins.setdefault(guild.id, deque(maxlen=100))
        joins.append(now)

        recent = sum(1 for ts in joins if now - ts <= window)
        if recent >= threshold and guild.id not in self._lockdown_guilds:
            self._lockdown_guilds.add(guild.id)
            await self.lockdown_guild(
                guild, f"Mass join: {recent} người/{window}s")

    async def lockdown_guild(self, guild: discord.Guild, reason: str):
        """Khóa send_messages của @everyone toàn server (cũng dùng cho /lockdown)."""
        locked = 0
        try:
            for channel in guild.text_channels:
                try:
                    overwrite = channel.overwrites_for(guild.default_role)
                    if overwrite.send_messages is False:
                        continue
                    overwrite.send_messages = False
                    await channel.set_permissions(
                        guild.default_role, overwrite=overwrite, reason=reason)
                    locked += 1
                except discord.HTTPException:
                    continue
            await self._log(
                guild, "🚨 LOCKDOWN TOÀN SERVER",
                f"**Lý do:** {reason}\nĐã khóa {locked} channel. "
                f"Mod dùng `/unlockdown` để mở.",
                discord.Color.red(),
            )
        except Exception:
            log.exception("Error during guild lockdown")

    async def unlockdown_guild(self, guild: discord.Guild, reason: str = "Mod mở khóa"):
        """Mở khóa toàn server (dùng cho /unlockdown)."""
        unlocked = 0
        try:
            for channel in guild.text_channels:
                try:
                    overwrite = channel.overwrites_for(guild.default_role)
                    if overwrite.send_messages is not False:
                        continue
                    overwrite.send_messages = None  # trả về mặc định
                    await channel.set_permissions(
                        guild.default_role, overwrite=overwrite, reason=reason)
                    unlocked += 1
                except discord.HTTPException:
                    continue
            self._lockdown_guilds.discard(guild.id)
            await self._log(
                guild, "🔓 Mở lockdown",
                f"Đã mở khóa {unlocked} channel. ({reason})",
                discord.Color.green(),
            )
        except Exception:
            log.exception("Error during guild unlockdown")

    # ========================================================
    # 2️⃣ ACCOUNT AGE
    # ========================================================
    async def _check_account_age(self, member: discord.Member) -> bool:
        min_age = await self.db.get_config(member.guild.id, "antiraid_min_account_age")
        age = (datetime.now(timezone.utc) - member.created_at).total_seconds()
        if age >= min_age:
            return False
        try:
            try:
                await member.send(
                    f"❌ Account của bạn quá mới để vào **{member.guild.name}** "
                    f"(yêu cầu tối thiểu {min_age // 3600} giờ tuổi). Thử lại sau!")
            except (discord.Forbidden, discord.HTTPException):
                pass
            await member.kick(reason=f"Account quá mới ({int(age // 60)} phút tuổi)")
            await self._log(
                member.guild, "👢 Kick: account quá mới",
                f"**{member}** bị kick — account mới tạo "
                f"{int(age // 60)} phút trước (yêu cầu ≥ {min_age // 3600}h).",
                discord.Color.red(), user=member,
            )
            return True
        except discord.Forbidden:
            log.warning("No permission to kick %s", member)
        except Exception:
            log.exception("Error kicking young account")
        return False

    # ========================================================
    # 5️⃣ CAPTCHA — Unverified role + timeout kick
    # Gửi vào KÊNH VERIFY (set qua /setup-verify); chưa set → DM fallback
    # ========================================================
    async def _start_captcha(self, member: discord.Member):
        guild = member.guild
        timeout = await self.db.get_config(guild.id, "antiraid_captcha_timeout")

        # Gán Unverified role (tự tạo nếu chưa có)
        unverified = await self._get_or_create_role(
            guild, "role_unverified", "Unverified")
        if unverified:
            try:
                await member.add_roles(unverified, reason="Chờ verify captcha")
            except discord.HTTPException:
                pass

        # Callback khi verify đúng mã
        async def on_success(interaction: discord.Interaction):
            try:
                # Hủy task kick
                task = self._pending_captcha.pop((guild.id, member.id), None)
                if task:
                    task.cancel()
                m = guild.get_member(member.id)
                if m:
                    # Xóa Unverified role
                    if unverified and unverified in m.roles:
                        await m.remove_roles(unverified, reason="Verify thành công")
                    # ✅ Cấp Verified role (nếu đã set qua /setup-verify)
                    verified_id = await self.db.get_config(guild.id, "role_verified")
                    verified = guild.get_role(verified_id) if verified_id else None
                    if verified:
                        try:
                            await m.add_roles(verified, reason="Verify thành công")
                        except discord.HTTPException:
                            pass
                await interaction.response.send_message(
                    f"✅ Verify thành công! Chào mừng đến **{guild.name}** 🎉",
                    ephemeral=True,
                )
                await self._log(
                    guild, "✅ Verify thành công",
                    f"{member.mention} đã vượt captcha.",
                    discord.Color.green(), user=member,
                )
            except Exception:
                log.exception("Error in captcha success callback")

        # Kênh verify đã set qua /setup-verify → gửi vào đó (ưu tiên #1)
        verify_channel_id = await self.db.get_config(guild.id, "channel_verify")
        verify_channel = guild.get_channel(verify_channel_id) if verify_channel_id else None

        sent = await captcha_util.send_captcha(
            member, on_success, timeout=timeout,
            verify_channel=verify_channel,           # kênh verify (ưu tiên)
            fallback_channel=guild.system_channel,   # cuối cùng mới tới system channel
        )
        if not sent:
            # Không gửi được captcha → quarantine thay vì kick oan
            await self._quarantine(member, "Không thể gửi captcha (DM đóng)")
            return

        # Task kick sau timeout nếu chưa verify
        async def kick_after_timeout():
            try:
                await asyncio.sleep(timeout)
                m = guild.get_member(member.id)
                if m is None:
                    return  # đã rời server
                # Vẫn còn Unverified role → chưa verify → kick
                if unverified and unverified in m.roles:
                    try:
                        await m.send(
                            f"⏰ Bạn bị kick khỏi **{guild.name}** vì không verify "
                            f"trong {timeout} giây. Có thể vào lại để thử lần nữa.")
                    except (discord.Forbidden, discord.HTTPException):
                        pass
                    await m.kick(reason=f"Không verify captcha trong {timeout}s")
                    await self._log(
                        guild, "👢 Kick: không verify",
                        f"**{m}** không verify captcha trong {timeout}s.",
                        discord.Color.red(), user=m,
                    )
            except asyncio.CancelledError:
                pass  # verify thành công → task bị hủy, bình thường
            except discord.Forbidden:
                log.warning("No permission to kick unverified %s", member)
            except Exception:
                log.exception("Error in captcha timeout kick")
            finally:
                self._pending_captcha.pop((guild.id, member.id), None)

        self._pending_captcha[(guild.id, member.id)] = asyncio.create_task(
            kick_after_timeout())

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        """Member rời server → hủy task captcha đang chờ."""
        try:
            task = self._pending_captcha.pop((member.guild.id, member.id), None)
            if task:
                task.cancel()
        except Exception:
            log.exception("Error cleaning captcha task")


async def setup(bot: commands.Bot):
    await bot.add_cog(AntiRaid(bot))

# ✅ Done: cogs/anti_raid.py — Tiếp theo: cogs/anti_nuke.py
