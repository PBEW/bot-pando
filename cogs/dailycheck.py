"""เช็คชื่อกิจกรรม / ประชุม: แอดมินใช้ /daily_checkin ตั้งหัวข้อ แล้วให้คนในเซิร์ฟเวอร์กด ✅ ไป / 🤔 ยังไม่แน่ใจ / ❌ ไม่ไป

ไม่เกี่ยวกับการมาทำงาน (การเข้างานอยู่ที่ /panel_staff) · ทุกกระดานเก็บคำตอบแยกกัน เปลี่ยนคำตอบได้จนกว่าแอดมินจะปิด
"""
from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands

from core.embeds import COLOR_MAIN, COLOR_OK
from core.utils import discord_ts, from_iso, is_admin, now_utc, staff_members, to_iso

log = logging.getLogger("olp.dailycheck")

STATUSES = {
    "YES": ("✅", "ไป"),
    "MAYBE": ("🤔", "ยังไม่แน่ใจ"),
    "NO": ("❌", "ไม่ไป"),
}


class RollCallView(discord.ui.View):
    """ปุ่มของกระดาน (ลงทะเบียนถาวร ใช้ได้กับทุกกระดาน เพราะอ้างกระดานจากข้อความที่กด)"""

    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(label="ไป", emoji="✅", style=discord.ButtonStyle.success, custom_id="olp:roll:yes")
    async def yes(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.client.get_cog("DailyCheckCog").answer(interaction, "YES")

    @discord.ui.button(label="ยังไม่แน่ใจ", emoji="🤔", style=discord.ButtonStyle.secondary, custom_id="olp:roll:maybe")
    async def maybe(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.client.get_cog("DailyCheckCog").answer(interaction, "MAYBE")

    @discord.ui.button(label="ไม่ไป", emoji="❌", style=discord.ButtonStyle.danger, custom_id="olp:roll:no")
    async def no(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.client.get_cog("DailyCheckCog").answer(interaction, "NO")

    @discord.ui.button(label="ปิดเช็คชื่อ", emoji="🔒", style=discord.ButtonStyle.secondary, custom_id="olp:roll:close", row=1)
    async def close(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.client.get_cog("DailyCheckCog").close_board(interaction)


class LegacyDailyView(discord.ui.View):
    """ปุ่มของกระดานเช็คชื่อแบบเก่า (มาทำงาน / หยุด) ที่ยังค้างอยู่ในห้อง"""

    def __init__(self) -> None:
        super().__init__(timeout=None)

    async def _gone(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(
            "กระดานนี้เลิกใช้แล้วค่ะ เข้างานใช้ปุ่ม 🟢 เข้างาน ใน /panel_staff แทน", ephemeral=True
        )

    @discord.ui.button(label="มาทำงานวันนี้", emoji="✅", style=discord.ButtonStyle.success, custom_id="olp:daily:in")
    async def old_in(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._gone(interaction)

    @discord.ui.button(label="หยุดวันนี้", emoji="🛌", style=discord.ButtonStyle.secondary, custom_id="olp:daily:off")
    async def old_off(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._gone(interaction)


class DailyCheckCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.cfg = bot.cfg
        self.db = bot.db

    # ------------------------------------------------------------ ข้อมูล
    async def _board(self, message_id: int) -> dict | None:
        return await self.db.fetchone("SELECT * FROM rollcall_boards WHERE message_id = ?", (message_id,))

    async def board_embed(self, guild: discord.Guild | None, board: dict) -> discord.Embed:
        rows = await self.db.fetchall(
            "SELECT * FROM rollcall_answers WHERE message_id = ? ORDER BY answered_at", (board["message_id"],)
        )
        closed = bool(board["closed"])
        desc = []
        if board.get("when_text"):
            desc.append(f"🗓️ **{board['when_text']}**")
        desc.append(
            "🔒 ปิดเช็คชื่อแล้ว" if closed else "กดปุ่มด้านล่างเพื่อตอบ เปลี่ยนคำตอบได้จนกว่าแอดมินจะปิดค่ะ"
        )
        if board["staff_only"]:
            desc.append("👥 เฉพาะพนักงาน")
        embed = discord.Embed(
            title=f"📋 เช็คชื่อ · {board['topic']}",
            description="\n".join(desc),
            color=COLOR_OK if closed else COLOR_MAIN,
        )
        for key, (emoji, label) in STATUSES.items():
            people = [r for r in rows if r["status"] == key]
            value = "\n".join(f"<@{r['user_id']}> · {discord_ts(from_iso(r['answered_at']))}" for r in people)
            embed.add_field(name=f"{emoji} {label} ({len(people)})", value=value[:1024] or "-", inline=True)
        if board["staff_only"]:
            answered = {r["user_id"] for r in rows}
            waiting = [m for m in staff_members(guild, self.cfg.staff_role_ids) if m.id not in answered]
            if waiting:
                embed.add_field(
                    name=f"⏳ ยังไม่ตอบ ({len(waiting)})",
                    value=", ".join(m.mention for m in waiting)[:1024],
                    inline=False,
                )
        embed.set_footer(text=f"ตั้งโดยแอดมิน · {len(rows)} คนตอบแล้ว")
        return embed

    def _is_staff(self, member: discord.abc.User) -> bool:
        roles = set(self.cfg.staff_role_ids)
        return isinstance(member, discord.Member) and any(r.id in roles for r in member.roles)

    # ------------------------------------------------------------ ปุ่ม
    async def answer(self, interaction: discord.Interaction, status: str) -> None:
        board = await self._board(interaction.message.id)
        if board is None:
            await interaction.response.send_message("ไม่พบกระดานนี้ในระบบค่ะ", ephemeral=True)
            return
        if board["closed"]:
            await interaction.response.send_message("กระดานนี้ปิดเช็คชื่อแล้วค่ะ", ephemeral=True)
            return
        if board["staff_only"] and not self._is_staff(interaction.user):
            await interaction.response.send_message("กระดานนี้สำหรับพนักงานเท่านั้นค่ะ", ephemeral=True)
            return
        await self.db.execute(
            "INSERT INTO rollcall_answers (message_id, user_id, status, answered_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(message_id, user_id) DO UPDATE SET status = excluded.status, answered_at = excluded.answered_at",
            (board["message_id"], interaction.user.id, status, to_iso(now_utc())),
        )
        await interaction.response.edit_message(embed=await self.board_embed(interaction.guild, board))
        emoji, label = STATUSES[status]
        await interaction.followup.send(f"{emoji} บันทึกว่า **{label}** แล้วค่ะ", ephemeral=True)

    async def close_board(self, interaction: discord.Interaction) -> None:
        if not is_admin(interaction.user, self.cfg.admin_role_id):
            await interaction.response.send_message("เฉพาะแอดมินเท่านั้นค่ะ", ephemeral=True)
            return
        board = await self._board(interaction.message.id)
        if board is None:
            await interaction.response.send_message("ไม่พบกระดานนี้ในระบบค่ะ", ephemeral=True)
            return
        await self.db.execute("UPDATE rollcall_boards SET closed = 1 WHERE message_id = ?", (board["message_id"],))
        board["closed"] = 1
        await interaction.response.edit_message(embed=await self.board_embed(interaction.guild, board), view=None)

    # ---------------------------------------------------------- คำสั่ง
    @app_commands.command(name="daily_checkin", description="โพสต์กระดานเช็คชื่อกิจกรรม / ประชุม (แอดมิน)")
    @app_commands.describe(
        topic="หัวข้อ เช่น ประชุมทีม, อีเวนต์ฮาโลวีน",
        when="วันเวลา เช่น เสาร์ 12/10 21:00 (ไม่บังคับ)",
        staff_only="ให้ตอบได้เฉพาะพนักงาน และแสดงรายชื่อคนที่ยังไม่ตอบ",
    )
    async def daily_checkin(
        self,
        interaction: discord.Interaction,
        topic: app_commands.Range[str, 1, 100],
        when: app_commands.Range[str, 1, 100] | None = None,
        staff_only: bool = False,
    ) -> None:
        if not is_admin(interaction.user, self.cfg.admin_role_id):
            await interaction.response.send_message("เฉพาะแอดมินเท่านั้นค่ะ", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        # โพสต์ก่อนเพื่อได้เลขข้อความ แล้วค่อยบันทึกกระดานและใส่ข้อมูลจริง
        message = await interaction.channel.send(
            embed=discord.Embed(title=f"📋 เช็คชื่อ · {topic}", color=COLOR_MAIN), view=RollCallView()
        )
        await self.db.execute(
            "INSERT INTO rollcall_boards (message_id, channel_id, topic, when_text, staff_only, created_by, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (message.id, message.channel.id, topic, when, int(staff_only), interaction.user.id, to_iso(now_utc())),
        )
        board = await self._board(message.id)
        await message.edit(embed=await self.board_embed(interaction.guild, board))
        await interaction.followup.send("โพสต์กระดานเช็คชื่อแล้วค่ะ · ปิดได้ด้วยปุ่ม 🔒 ปิดเช็คชื่อ", ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    bot.add_view(RollCallView())
    bot.add_view(LegacyDailyView())
    await bot.add_cog(DailyCheckCog(bot))
