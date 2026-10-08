"""คำสั่งดูแลระบบ: ตรวจสถานะ, โหลด config ใหม่"""
from __future__ import annotations

import datetime as dt

import discord
from discord import app_commands
from discord.ext import commands

from core.cycle import cycle_title, next_cutoff_local
from core.embeds import COLOR_INFO, COLOR_OK
from core.utils import fmt_datetime, is_admin


class AdminCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.cfg = bot.cfg
        self.db = bot.db

    def _guard(self, interaction: discord.Interaction) -> bool:
        return is_admin(interaction.user, self.cfg.admin_role_id)

    @app_commands.command(name="health", description="ตรวจสถานะบอทและการตั้งค่า (แอดมิน)")
    async def health(self, interaction: discord.Interaction) -> None:
        if not self._guard(interaction):
            await interaction.response.send_message("เฉพาะแอดมินเท่านั้นค่ะ", ephemeral=True)
            return
        await interaction.response.send_message(embed=await self.health_embed(), ephemeral=True)

    async def health_embed(self) -> discord.Embed:
        now_local = dt.datetime.now(self.cfg.tz)
        active = await self.db.active_jobs()
        tickets = await self.db.tickets_by_status(["OPEN", "ACTIVE"])

        def channel_line(key: str) -> str:
            cid = self.cfg.channel_id(key)
            channel = self.bot.get_channel(cid) if cid else None
            return channel.mention if channel else f"⚠️ ยังไม่ได้ตั้งค่า (`channels.{key}`)"

        embed = discord.Embed(title="🩺 สถานะระบบ Pandora", color=COLOR_INFO)
        embed.add_field(name="📶 Latency", value=f"{self.bot.latency * 1000:.0f} ms", inline=True)
        embed.add_field(name="🕒 เวลาปัจจุบัน", value=fmt_datetime(now_local, self.cfg.tz), inline=True)
        embed.add_field(name="📋 งานที่ยังไม่จบ", value=str(len(active)), inline=True)
        embed.add_field(name="🎫 Ticket ที่เปิดอยู่", value=str(len(tickets)), inline=True)
        embed.add_field(
            name="📍 ห้อง",
            value=(
                f"🛠️ แอดมิน　{channel_line('admin')}\n"
                f"💖 รีวิว　{channel_line('review')}\n"
                f"📣 ประกาศ　{channel_line('announce')}"
            ),
            inline=False,
        )
        adult = self.cfg.adult_role_ids
        embed.add_field(
            name="🔞 Role ยืนยันอายุ 18+",
            value=" ".join(f"<@&{r}>" for r in adult) if adult else "⚠️ ยังไม่ตั้ง (`roles.adult_verified`) · บริการ 18+ เปิดบิลได้ทุกคน",
            inline=False,
        )
        embed.add_field(
            name="Google Sheets",
            value=(
                f"✅ เชื่อมต่อแล้ว · ชีตรอบปัจจุบัน `{await self.db.get_meta('current_cycle') or cycle_title(self.cfg)}`"
                if self.bot.sheets.ready
                else ("⚠️ เปิดใช้งานแต่เชื่อมต่อไม่สำเร็จ" if self.bot.sheets.enabled else "ปิดใช้งาน")
            ),
            inline=False,
        )
        embed.add_field(
            name="ตัดรอบครั้งถัดไป",
            value=fmt_datetime(next_cutoff_local(now_local, self.cfg), self.cfg.tz),
            inline=False,
        )
        if not self.cfg.vip_enabled:
            return embed
        tier_lines = [
            f"{t.get('emoji', '')} **{t['name']}** — "
            + (f"<@&{t['role_id']}>" if t.get("role_id") else "⚠️ ยังไม่ตั้ง role_id")
            for t in self.cfg.vip_tiers
        ]
        embed.add_field(
            name="ระดับ VIP", value="\n".join(tier_lines) or "⚠️ ยังไม่ได้ตั้งค่า", inline=False
        )
        return embed

    @app_commands.command(name="sheets_format", description="จัดรูปแบบ Google Sheets ใหม่ (สี/หัวตาราง/สรุป) · ข้อมูลเดิมไม่หาย (แอดมิน)")
    async def sheets_format(self, interaction: discord.Interaction) -> None:
        if not self._guard(interaction):
            await interaction.response.send_message("เฉพาะแอดมินเท่านั้นค่ะ", ephemeral=True)
            return
        if not self.bot.sheets.ready:
            await interaction.response.send_message("ยังไม่ได้เชื่อมต่อ Google Sheets ค่ะ (ดู `/health`)", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        title = await self.db.get_meta("current_cycle") or cycle_title(self.cfg)
        try:
            done = await self.bot.sheets.restyle(title)
        except Exception as exc:  # noqa: BLE001 - แจ้งแอดมินแทนการเงียบ
            await interaction.followup.send(f"❌ จัดรูปแบบไม่สำเร็จ: `{exc}`", ephemeral=True)
            return
        backfilled = await self.bot.get_cog("PaymentsCog").backfill_sheet()
        url = await self.bot.sheets.spreadsheet_url()
        extra = f"\n📥 เติมบิลที่ตกหล่นลงชีต {backfilled} ใบ" if backfilled else ""
        await interaction.followup.send(
            embed=discord.Embed(
                description=f"✅ จัดรูปแบบแล้ว: {', '.join(f'`{t}`' for t in done)}{extra}\n{url}",
                color=COLOR_OK,
            ),
            ephemeral=True,
        )

    @app_commands.command(name="reload_config", description="โหลดไฟล์ config.json ใหม่ (แอดมิน)")
    async def reload_config(self, interaction: discord.Interaction) -> None:
        if not self._guard(interaction):
            await interaction.response.send_message("เฉพาะแอดมินเท่านั้นค่ะ", ephemeral=True)
            return
        self.cfg.reload()
        await interaction.response.send_message(
            embed=discord.Embed(description="โหลด config ใหม่เรียบร้อยค่ะ", color=COLOR_OK),
            ephemeral=True,
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(AdminCog(bot))
