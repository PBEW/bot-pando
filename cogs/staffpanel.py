"""เมนูพนักงาน: แผงปุ่มค้างในห้อง รวมเข้างาน ชั่วโมง รายได้ และงานของฉันไว้ที่เดียว"""
from __future__ import annotations

import datetime as dt

import discord
from discord import app_commands
from discord.ext import commands

from core.cycle import cycle_start_local
from core.embeds import COLOR_MAIN, COLOR_OK, STATUS_LABEL, panel_embed
from core.pricing import job_staff_ids, job_staff_split
from core.utils import discord_ts, fmt_datetime, from_iso, is_admin, money, now_utc, purge_old_panels, to_iso


class StaffPanel(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=None)

    @staticmethod
    def _attendance(interaction: discord.Interaction):
        return interaction.client.get_cog("AttendanceCog")

    # แถว 1: ลงเวลา (ตรวจสิทธิ์พนักงานในแต่ละฟังก์ชันอยู่แล้ว)
    @discord.ui.button(label="เข้างาน", emoji="🟢", style=discord.ButtonStyle.success, custom_id="olp:staff:in", row=0)
    async def clock_in(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._attendance(interaction).clock_in(interaction)

    @discord.ui.button(label="ยกเลิกเข้างาน", emoji="↩️", style=discord.ButtonStyle.secondary, custom_id="olp:staff:cancel", row=0)
    async def cancel_clock_in(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._attendance(interaction).cancel_clock_in(interaction)

    # แถว 2: ข้อมูลของฉัน
    @discord.ui.button(label="ชั่วโมงของฉัน", emoji="🕒", style=discord.ButtonStyle.primary, custom_id="olp:staff:hours", row=1)
    async def my_hours(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._attendance(interaction).send_my_hours(interaction)

    @discord.ui.button(label="รายได้รอบนี้", emoji="💰", style=discord.ButtonStyle.primary, custom_id="olp:staff:income", row=1)
    async def my_income(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        cog: StaffPanelCog = interaction.client.get_cog("StaffPanelCog")  # type: ignore[assignment]
        await cog.send_my_income(interaction)

    @discord.ui.button(label="งานของฉัน", emoji="📋", style=discord.ButtonStyle.primary, custom_id="olp:staff:jobs", row=1)
    async def my_jobs(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        cog: StaffPanelCog = interaction.client.get_cog("StaffPanelCog")  # type: ignore[assignment]
        await cog.send_my_jobs(interaction)

    # แถว 3: ทีม
    @discord.ui.button(label="มาทำงานวันนี้", emoji="👥", style=discord.ButtonStyle.secondary, custom_id="olp:staff:on_duty", row=2)
    async def on_duty(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        cog = self._attendance(interaction)
        if await cog._deny_if_not_staff(interaction):
            return
        await interaction.response.send_message(embed=await cog.today_embed(), ephemeral=True)

    @discord.ui.button(label="บัญชีรับเงิน", emoji="💳", style=discord.ButtonStyle.secondary, custom_id="olp:staff:payout", row=2)
    async def payout(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        cog: StaffPanelCog = interaction.client.get_cog("StaffPanelCog")  # type: ignore[assignment]
        await cog.open_payout(interaction)


class PayoutModal(discord.ui.Modal, title="บัญชีรับเงินของฉัน"):
    bank = discord.ui.TextInput(label="ธนาคาร หรือ พร้อมเพย์", placeholder="เช่น กสิกรไทย / พร้อมเพย์", max_length=40)
    account_no = discord.ui.TextInput(
        label="เลขบัญชี / เบอร์หรือเลขบัตรพร้อมเพย์", placeholder="เช่น 123-4-56789-0", max_length=25
    )
    account_name = discord.ui.TextInput(label="ชื่อบัญชี (ตามหน้าสมุด)", max_length=80)

    def __init__(self, current: dict | None) -> None:
        super().__init__()
        if current:
            self.bank.default = current["bank"]
            self.account_no.default = current["account_no"]
            self.account_name.default = current["account_name"]

    async def on_submit(self, interaction: discord.Interaction) -> None:
        cog: StaffPanelCog = interaction.client.get_cog("StaffPanelCog")  # type: ignore[assignment]
        await cog.save_payout(
            interaction, str(self.bank.value).strip(), str(self.account_no.value).strip(), str(self.account_name.value).strip()
        )


def mask_account(no: str) -> str:
    """แสดงเลขบัญชีแบบซ่อนบางส่วน (ใช้ในข้อความที่คนอื่นอาจเห็น)"""
    digits = [c for c in no if c.isdigit()]
    return f"xxx-{''.join(digits[-4:])}" if len(digits) > 4 else no


class StaffPanelCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.cfg = bot.cfg
        self.db = bot.db

    async def _deny(self, interaction: discord.Interaction) -> bool:
        return await self.bot.get_cog("AttendanceCog")._deny_if_not_staff(interaction)

    # -------------------------------------------------------- บัญชีรับเงิน
    async def open_payout(self, interaction: discord.Interaction) -> None:
        if await self._deny(interaction):
            return
        await interaction.response.send_modal(PayoutModal(await self.db.get_payout(interaction.user.id)))

    async def save_payout(self, interaction: discord.Interaction, bank: str, account_no: str, account_name: str) -> None:
        cleaned = account_no.replace(" ", "")
        digits = sum(c.isdigit() for c in cleaned)
        if not all(c.isdigit() or c == "-" for c in cleaned) or not 9 <= digits <= 15:
            await interaction.response.send_message(
                "⚠️ เลขบัญชี/พร้อมเพย์ต้องเป็นตัวเลข 9–15 หลัก (ใส่ขีด - ได้) ค่ะ", ephemeral=True
            )
            return

        # ตอบ Discord ก่อน (ต้องภายใน 3 วินาที) — การเขียน Google Sheets อาจช้ากว่านั้น
        await interaction.response.defer(ephemeral=True, thinking=True)
        now = dt.datetime.now(self.cfg.tz)
        await self.db.set_payout(interaction.user.id, bank, cleaned, account_name, now.isoformat())
        synced = await self.bot.sheets.upsert_payout_row(
            [
                interaction.user.display_name,
                str(interaction.user.id),
                bank,
                cleaned,
                account_name,
                now.strftime("%d/%m/%Y %H:%M"),
            ]
        )
        embed = discord.Embed(title="💳 บันทึกบัญชีรับเงินแล้ว", color=COLOR_OK)
        embed.add_field(name="ธนาคาร / ช่องทาง", value=bank, inline=True)
        embed.add_field(name="เลขบัญชี", value=f"`{cleaned}`", inline=True)
        embed.add_field(name="ชื่อบัญชี", value=account_name, inline=False)
        embed.set_footer(
            text="ข้อมูลนี้เห็นเฉพาะคุณกับแอดมิน"
            + (" · อัปเดตใน Google Sheets แล้ว" if synced else "")
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

        payments = self.bot.get_cog("PaymentsCog")
        if payments is not None:
            await payments.notify_admin_text(
                f"💳 {interaction.user.mention} อัปเดตบัญชีรับเงิน: {bank} {mask_account(cleaned)}"
            )

    async def send_my_income(self, interaction: discord.Interaction) -> None:
        if await self._deny(interaction):
            return
        now_local = dt.datetime.now(self.cfg.tz)
        start = cycle_start_local(now_local, self.cfg)
        me = interaction.user.id
        mine: list[tuple[dict, float, float]] = []  # (บิล, ยอดส่วนของฉัน, ส่วนแบ่งของฉัน)
        for j in await self.db.jobs_paid_between(to_iso(start), to_iso(now_local)):
            for staff_id, part, part_share in job_staff_split(self.cfg, j):
                if staff_id == me:
                    mine.append((j, part, part_share))
        jobs = [j for j, _, _ in mine]
        gross = sum(part for _, part, _ in mine)
        share = sum(part_share for _, _, part_share in mine)

        embed = discord.Embed(title="💰 รายได้ของคุณ (รอบปัจจุบัน)", color=COLOR_OK)
        embed.add_field(name="ตั้งแต่", value=fmt_datetime(start, self.cfg.tz), inline=True)
        embed.add_field(name="บิลที่ชำระแล้ว", value=f"{len(jobs)} ใบ", inline=True)
        embed.add_field(name="ยอดบิลรวม", value=money(gross), inline=True)
        embed.add_field(name="ส่วนแบ่งของคุณ", value=f"**{money(share)}**", inline=False)
        if jobs:
            lines = [
                f"`#{j['id']}` {self.cfg.service_names(j['services'])} · แบ่ง {money(part_share)}"
                for j, _, part_share in mine[-10:]
            ]
            embed.add_field(name="บิลล่าสุด (สูงสุด 10 ใบ)", value="\n".join(lines)[:1024], inline=False)
        embed.set_footer(text="นับเฉพาะบิลที่ชำระแล้ว · ยอดสุดท้ายยึดตามสรุปตัดรอบของแอดมิน")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    async def send_my_jobs(self, interaction: discord.Interaction) -> None:
        if await self._deny(interaction):
            return
        now = now_utc()
        jobs = [
            j
            for j in await self.db.active_jobs()
            if interaction.user.id in job_staff_ids(j) and from_iso(j["end_time"]) > now
        ]
        embed = discord.Embed(title="📋 งานของคุณที่ยังไม่จบ", color=COLOR_MAIN)
        if not jobs:
            embed.description = "ตอนนี้ไม่มีงานค้างค่ะ"
        else:
            lines = []
            for j in sorted(jobs, key=lambda j: j["start_time"])[:15]:
                start, end = from_iso(j["start_time"]), from_iso(j["end_time"])
                lines.append(
                    f"`#{j['id']}` <@{j['customer_id']}> · {self.cfg.service_names(j['services'])}\n"
                    f"　{discord_ts(start)}–{discord_ts(end)} · ห้อง {self.cfg.room_name(j.get('room'))} · "
                    f"{STATUS_LABEL.get(j['status'], j['status'])}"
                )
            embed.description = "\n".join(lines)[:4000]
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="panel_staff", description="โพสต์เมนูพนักงาน (เข้างาน, ชั่วโมง, รายได้, งานของฉัน)")
    async def panel_staff(self, interaction: discord.Interaction) -> None:
        if not is_admin(interaction.user, self.cfg.admin_role_id):
            await interaction.response.send_message("เฉพาะแอดมินเท่านั้นค่ะ", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        removed = await purge_old_panels(interaction.channel, self.bot.user.id, "olp:staff:")

        attendance = self.bot.get_cog("AttendanceCog")
        cutoff = attendance.cutoff_label() if attendance else "ตี 1"
        embed = panel_embed(
            "🧑‍💼 Pandora · เมนูพนักงาน",
            "กดปุ่มได้เลย ผลลัพธ์จะเห็นเฉพาะคุณ",
            [
                ("🕒 ลงเวลา", [
                    ("🟢 เข้างาน", "เลือกงานที่รับวันนี้ แล้วรับ Role On Duty"),
                    ("↩️ ยกเลิกเข้างาน", "กดผิด กดยกเลิกได้ (ไม่นับชั่วโมง)"),
                ]),
                ("👤 ของฉัน", [
                    ("🕒 ชั่วโมงของฉัน", "ชั่วโมงสะสมรอบนี้"),
                    ("💰 รายได้รอบนี้", "ส่วนแบ่งที่จะได้รับในรอบนี้"),
                    ("📋 งานของฉัน", "บิลที่ยังไม่จบเวลา"),
                ]),
                ("👥 ทีม", [
                    ("👥 มาทำงานวันนี้", "ใครเข้างานบ้างวันนี้"),
                    ("💳 บัญชีรับเงิน", "ใส่บัญชีไว้ให้แอดมินโอนส่วนแบ่ง"),
                ]),
            ],
            footer=f"ไม่ต้องกดออกงาน — บอทตัดยอดให้อัตโนมัติทุก {cutoff}",
            guild=interaction.guild,
        )
        await interaction.channel.send(embed=embed, view=StaffPanel())

        note = f" (ลบแผงเก่าออก {removed} อัน)" if removed else ""
        await interaction.followup.send(f"โพสต์เมนูพนักงานแล้วค่ะ{note}", ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    bot.add_view(StaffPanel())
    await bot.add_cog(StaffPanelCog(bot))
