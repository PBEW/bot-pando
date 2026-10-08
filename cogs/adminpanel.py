"""เมนูแอดมิน: แผงปุ่มค้างในห้อง รวมงานแอดมินที่ใช้บ่อยไว้ที่เดียว ไม่ต้องจำคำสั่ง"""
from __future__ import annotations

import datetime as dt

import discord
from discord import app_commands
from discord.ext import commands

from core.embeds import COLOR_DANGER, COLOR_GOLD, COLOR_INFO, COLOR_MAIN, COLOR_OK, COLOR_WARN, dm_embed, menu_item, panel_embed, rows_text
from core.utils import is_admin, purge_old_panels
from core.vip_logic import GRANT_DURATIONS, duration_label

NOT_ADMIN = "เฉพาะแอดมินเท่านั้นค่ะ"


def _is_admin(interaction: discord.Interaction) -> bool:
    return is_admin(interaction.user, interaction.client.cfg.admin_role_id)


def _member(interaction: discord.Interaction, user: discord.abc.User) -> discord.Member | None:
    if isinstance(user, discord.Member):
        return user
    return interaction.guild.get_member(user.id) if interaction.guild else None


class AdminOnlyView(discord.ui.View):
    """View ชั่วคราว (เห็นคนเดียว) ที่ตรวจสิทธิ์แอดมินทุกครั้งที่กด"""

    def __init__(self) -> None:
        super().__init__(timeout=300)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if _is_admin(interaction):
            return True
        await interaction.response.send_message(NOT_ADMIN, ephemeral=True)
        return False


# ---------------------------------------------------------------- ให้ VIP
class VipGrantView(AdminOnlyView):
    """มอบ VIP: เลือกสมาชิก → ระยะเวลา (1 วัน – 6 เดือน) → (ระดับ ถ้ามีหลายระดับ) → ยืนยัน"""

    def __init__(self, cfg) -> None:
        super().__init__()
        self.cfg = cfg
        self.member: discord.Member | None = None
        tiers = cfg.vip_tiers
        self.tier_key: str | None = tiers[0]["key"] if tiers else None
        self.duration = "month:1"

        self.duration_select.options = [
            discord.SelectOption(
                label=label, value=f"{unit}:{amount}", emoji="📅" if unit == "day" else "🗓️",
                default=f"{unit}:{amount}" == self.duration,
            )
            for unit, amount, label in GRANT_DURATIONS
        ]
        if len(tiers) > 1:
            self.tier_select.options = [
                discord.SelectOption(label=t["name"], value=t["key"], emoji=t.get("emoji") or None, default=i == 0)
                for i, t in enumerate(tiers)
            ]
        else:
            self.remove_item(self.tier_select)

    def embed(self) -> discord.Embed:
        unit, amount = self.duration.split(":")
        return discord.Embed(
            title="💎 มอบ VIP ให้สมาชิก",
            description=rows_text([
                ("👤", "สมาชิก", self.member.mention if self.member else "*ยังไม่เลือก*"),
                ("⏳", "ระยะเวลา", duration_label(unit, int(amount))),
                ("💎", "ระดับ", self.cfg.vip_tier_name(self.tier_key) if self.tier_key else "⚠️ ยังไม่ได้ตั้งค่า VIP"),
            ])
            + "\n\n> ถ้าสมาชิกมี VIP ระดับเดียวกันอยู่แล้ว จะต่อเวลาจากวันหมดอายุเดิม · บอทส่ง DM แจ้งให้อัตโนมัติ",
            color=COLOR_GOLD,
        )

    @discord.ui.select(cls=discord.ui.UserSelect, placeholder="1) เลือกสมาชิก", row=0)
    async def member_select(self, interaction: discord.Interaction, select: discord.ui.UserSelect) -> None:
        self.member = _member(interaction, select.values[0])
        await interaction.response.edit_message(embed=self.embed(), view=self)

    @discord.ui.select(placeholder="2) ระยะเวลา (1 วัน ถึง 6 เดือน)", row=1)
    async def duration_select(self, interaction: discord.Interaction, select: discord.ui.Select) -> None:
        self.duration = select.values[0]
        for opt in select.options:
            opt.default = opt.value == self.duration
        await interaction.response.edit_message(embed=self.embed(), view=self)

    @discord.ui.select(placeholder="3) ระดับ VIP", row=2)
    async def tier_select(self, interaction: discord.Interaction, select: discord.ui.Select) -> None:
        self.tier_key = select.values[0]
        for opt in select.options:
            opt.default = opt.value == self.tier_key
        await interaction.response.edit_message(embed=self.embed(), view=self)

    @discord.ui.button(label="ยืนยันมอบ VIP", emoji="✅", style=discord.ButtonStyle.success, row=3)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if self.member is None or not self.tier_key:
            await interaction.response.send_message("เลือกสมาชิกให้ครบก่อนค่ะ", ephemeral=True)
            return
        self.stop()
        unit, amount = self.duration.split(":")
        await interaction.client.get_cog("VipCog").grant_vip(
            interaction, self.member, self.tier_key, int(amount), unit=unit
        )


# ------------------------------------------------------- แก้เวลาเข้างาน
class AttendanceFixModal(discord.ui.Modal):
    clock_in = discord.ui.TextInput(
        label="เวลาเข้างาน (เว้นว่าง = ไม่แก้)",
        placeholder="เช่น 18:00 หรือ 05/09 18:00",
        required=False,
        max_length=20,
    )
    clock_out = discord.ui.TextInput(
        label="เวลาออกงาน (เว้นว่าง = ไม่แก้)",
        placeholder="เช่น 02:30 หรือ 06/09 02:30",
        required=False,
        max_length=20,
    )

    def __init__(self, member: discord.Member, new_shift: bool) -> None:
        title = "เพิ่มกะที่ลืมกด" if new_shift else "แก้เวลากะล่าสุด"
        super().__init__(title=f"{title} · {member.display_name}"[:45])
        self.member = member
        self.new_shift = new_shift
        if new_shift:
            self.clock_in.label = "เวลาเข้างาน"
            self.clock_out.label = "เวลาออกงาน"
            self.clock_in.required = self.clock_out.required = True

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not _is_admin(interaction):
            await interaction.response.send_message(NOT_ADMIN, ephemeral=True)
            return
        await interaction.client.get_cog("AttendanceCog").fix_attendance(
            interaction,
            self.member,
            self.clock_in.value.strip() or None,
            self.clock_out.value.strip() or None,
            self.new_shift,
        )


class AttendanceFixView(AdminOnlyView):
    def __init__(self) -> None:
        super().__init__()
        self.member: discord.Member | None = None

    @discord.ui.select(cls=discord.ui.UserSelect, placeholder="เลือกพนักงาน", row=0)
    async def member_select(self, interaction: discord.Interaction, select: discord.ui.UserSelect) -> None:
        self.member = _member(interaction, select.values[0])
        await interaction.response.defer()

    async def _open(self, interaction: discord.Interaction, new_shift: bool) -> None:
        if self.member is None:
            await interaction.response.send_message("เลือกพนักงานก่อนค่ะ", ephemeral=True)
            return
        await interaction.response.send_modal(AttendanceFixModal(self.member, new_shift))

    @discord.ui.button(label="แก้กะล่าสุด", emoji="✏️", style=discord.ButtonStyle.primary, row=1)
    async def edit_latest(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._open(interaction, False)

    @discord.ui.button(label="เพิ่มกะที่ลืมกด", emoji="➕", style=discord.ButtonStyle.secondary, row=1)
    async def add_shift(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._open(interaction, True)


# ------------------------------------------------------------- ตัดรอบ
class CutoffConfirmView(AdminOnlyView):
    @discord.ui.button(label="ยืนยันตัดรอบ", emoji="✂️", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.stop()
        await interaction.response.edit_message(
            embed=discord.Embed(description="⏳ กำลังตัดรอบ...", color=COLOR_WARN), view=None
        )
        await interaction.client.get_cog("SchedulerCog").run_cutoff(manual=True)
        await interaction.edit_original_response(
            embed=discord.Embed(description="✅ ตัดรอบเรียบร้อย ส่งสรุปเข้าห้องแอดมินแล้วค่ะ", color=COLOR_OK)
        )

    @discord.ui.button(label="ยกเลิก", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.stop()
        await interaction.response.edit_message(
            embed=discord.Embed(description="ยกเลิกการตัดรอบแล้วค่ะ", color=COLOR_MAIN), view=None
        )


# ---------------------------------------------------------- แผงหลัก
HELP_TEXT = (
    "**🧩 แผงที่โพสต์ได้**\n"
    "`/panel reception` แผงรีเซปชั่น (เปิดบิล / ต่อเวลา / งานที่ดำเนินอยู่)\n"
    "`/panel_request` แผงบริการลูกค้า · `/panel_staff` เมนูพนักงาน · `/panel_attendance` แผงลงเวลา · `/panel_admin` แผงนี้\n\n"
    "**🧾 บิล**\n"
    "`/bill info` ดูบิล · `/bill paid` ยืนยันชำระด้วยมือ · `/bill cancel` ยกเลิกบิล\n\n"
    "**💎 VIP**\n"
    "`/vip_grant member days|months` มอบ VIP 1 วัน ถึง 6 เดือน\n\n"
    "**🧰 อื่น ๆ**\n"
    "`/top_donate` อันดับโดเนท · `/coins give|check|event` เหรียญ Pandora · `/menu` เมนูร้าน · `/attendance_fix` แก้เวลาเข้างาน · `/cutoff` ตัดรอบ · `/summary` สรุปยอด\n"
    "`/attendance_report` ชั่วโมงงาน · `/daily_checkin` เช็คชื่อกิจกรรม/ประชุม · `/staff_today` มาทำงานวันนี้ · `/health` สถานะระบบ · `/sheets_format` จัดรูปแบบชีต · `/reload_config` โหลด config"
)


class AdminPanel(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=None)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if _is_admin(interaction):
            return True
        await interaction.response.send_message(NOT_ADMIN, ephemeral=True)
        return False

    # แถว 1: ดูข้อมูล
    @discord.ui.button(label="สรุปยอดรอบนี้", emoji="📊", style=discord.ButtonStyle.primary, custom_id="olp:admin:summary", row=0)
    async def summary(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        embed = await interaction.client.get_cog("SchedulerCog").current_summary()
        await interaction.followup.send(embed=embed, ephemeral=True)

    @discord.ui.button(label="ชั่วโมงงาน", emoji="🕒", style=discord.ButtonStyle.primary, custom_id="olp:admin:hours", row=0)
    async def hours(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        embed = await interaction.client.get_cog("AttendanceCog").current_hours_embed()
        await interaction.followup.send(embed=embed, ephemeral=True)

    @discord.ui.button(label="มาทำงานวันนี้", emoji="🟢", style=discord.ButtonStyle.primary, custom_id="olp:admin:on_duty", row=0)
    async def on_duty(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        embed = await interaction.client.get_cog("AttendanceCog").today_embed(private=True)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # แถว 2: จัดการ
    @discord.ui.button(label="Top Donate", emoji="🏆", style=discord.ButtonStyle.success, custom_id="olp:admin:top_donate", row=1)
    async def top_donate(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        cfg = interaction.client.cfg
        now_local = dt.datetime.now(cfg.tz)
        await interaction.response.defer(ephemeral=True, thinking=True)
        embed = await interaction.client.get_cog("SchedulerCog").donate_embed(
            now_local.year, now_local.month, final=False
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    @discord.ui.button(label="ตั้งค่าร้าน", emoji="⚙️", style=discord.ButtonStyle.primary, custom_id="olp:admin:settings", row=1)
    async def settings(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.client.get_cog("SettingsCog").open_settings(interaction)

    @discord.ui.button(label="แก้เวลาเข้างาน", emoji="✏️", style=discord.ButtonStyle.success, custom_id="olp:admin:fix", row=1)
    async def fix(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_message(
            embed=discord.Embed(
                title="✏️ แก้เวลาเข้างาน",
                description=(
                    "1️⃣ เลือกพนักงาน　2️⃣ เลือกสิ่งที่ต้องการ\n\n"
                    + menu_item("✏️ แก้กะล่าสุด", "เวลาเข้า/ออกไม่ถูก") + "\n"
                    + menu_item("➕ เพิ่มกะที่ลืมกด", "ลืมกดเข้างานทั้งกะ")
                ),
                color=COLOR_INFO,
            ),
            view=AttendanceFixView(),
            ephemeral=True,
        )

    @discord.ui.button(label="ตัดรอบทันที", emoji="✂️", style=discord.ButtonStyle.danger, custom_id="olp:admin:cutoff", row=1)
    async def cutoff(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_message(
            embed=dm_embed(
                "✂️ ยืนยันตัดรอบ?",
                [
                    ("📊", "สรุปยอด", "ตั้งแต่ตัดครั้งล่าสุด → ตอนนี้ ส่งเข้าห้องแอดมิน"),
                    ("🔄", "เริ่มนับใหม่", "จากตอนนี้ (สรุปอัตโนมัติรอบถัดไปไม่นับซ้ำ)"),
                ],
                note="⚠️ ย้อนกลับไม่ได้",
                color=COLOR_DANGER,
            ),
            view=CutoffConfirmView(),
            ephemeral=True,
        )

    # แถว 3: ระบบ
    @discord.ui.button(label="สถานะระบบ", emoji="🩺", style=discord.ButtonStyle.secondary, custom_id="olp:admin:health", row=2)
    async def health(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        embed = await interaction.client.get_cog("AdminCog").health_embed()
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @discord.ui.button(label="โหลด config ใหม่", emoji="🔄", style=discord.ButtonStyle.secondary, custom_id="olp:admin:reload", row=2)
    async def reload(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        try:
            interaction.client.cfg.reload()
        except ValueError as exc:  # JSON ผิดรูปแบบ
            await interaction.response.send_message(f"❌ อ่าน config.json ไม่ได้: {exc}", ephemeral=True)
            return
        await interaction.response.send_message(
            embed=discord.Embed(description="โหลด config ใหม่เรียบร้อยค่ะ", color=COLOR_OK), ephemeral=True
        )

    @discord.ui.button(label="เหรียญ Pandora", emoji="🪙", style=discord.ButtonStyle.primary, custom_id="olp:admin:coins", row=1)
    async def coins(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.client.get_cog("CoinsCog").open_admin_menu(interaction)

    @discord.ui.button(label="มอบ VIP", emoji="💎", style=discord.ButtonStyle.primary, custom_id="olp:admin:vip_grant", row=2)
    async def vip_grant(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        cfg = interaction.client.cfg
        if not cfg.vip_enabled:
            await interaction.response.send_message(
                "ระบบ VIP ยังปิดอยู่ค่ะ เปิดที่ ⚙️ ตั้งค่าร้าน → 💎 VIP ก่อน", ephemeral=True
            )
            return
        view = VipGrantView(cfg)
        await interaction.response.send_message(embed=view.embed(), view=view, ephemeral=True)

    @discord.ui.button(label="คำสั่งทั้งหมด", emoji="📖", style=discord.ButtonStyle.secondary, custom_id="olp:admin:help", row=2)
    async def help(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_message(
            embed=discord.Embed(title="📖 คำสั่งทั้งหมด", description=HELP_TEXT, color=COLOR_MAIN), ephemeral=True
        )


class AdminPanelCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.cfg = bot.cfg

    @app_commands.command(name="panel_admin", description="โพสต์เมนูแอดมิน (แผงปุ่มรวมงานแอดมิน)")
    async def panel_admin(self, interaction: discord.Interaction) -> None:
        if not is_admin(interaction.user, self.cfg.admin_role_id):
            await interaction.response.send_message(NOT_ADMIN, ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        removed = await purge_old_panels(interaction.channel, self.bot.user.id, "olp:admin:")

        embed = panel_embed(
            "🛠️ Pandora · เมนูแอดมิน",
            "กดปุ่มได้เลย ผลลัพธ์จะเห็นเฉพาะคนกด",
            [
                ("📊 ดูข้อมูล", [
                    ("📊 สรุปยอดรอบนี้", "รายรับ · ส่วนแบ่งพนักงาน · ยอดโอนรายคน"),
                    ("🕒 ชั่วโมงงาน", "ชั่วโมงทำงานของพนักงานรอบนี้"),
                    ("🟢 มาทำงานวันนี้", "ใครเข้างาน รับงานแบบไหน / ไม่รับใคร"),
                ]),
                ("🧰 จัดการ", [
                    ("🏆 Top Donate", "อันดับและประกาศผลประจำเดือน"),
                    ("⚙️ ตั้งค่าร้าน", "ห้อง · บริการ & ราคา · ส่วนแบ่ง · ชำระเงิน · VIP"),
                    ("🪙 เหรียญ Pandora", "ดู/ปรับเหรียญ · คูปอง · อีเวนต์ · รางวัล"),
                    ("✏️ แก้เวลาเข้างาน", "แก้กะล่าสุด หรือเพิ่มกะที่ลืมกด"),
                    ("✂️ ตัดรอบทันที", "สรุปยอดตั้งแต่ตัดครั้งล่าสุดถึงตอนนี้"),
                    ("💎 มอบ VIP", "ให้ VIP สมาชิก 1 วัน ถึง 6 เดือน (ต่อจากวันหมดอายุเดิมได้)"),
                ]),
                ("🩺 ระบบ", [
                    ("🩺 สถานะระบบ", "ห้อง · Role · Google Sheets · งานค้าง"),
                    ("🔄 โหลด config ใหม่", "ใช้ค่าใน config.json ล่าสุดทันที"),
                    ("📖 คำสั่งทั้งหมด", "รายการ slash command ของบอท"),
                ]),
            ],
            footer="ควรโพสต์ในห้องที่เห็นเฉพาะแอดมิน (คนอื่นกดก็ใช้ไม่ได้)",
            guild=interaction.guild,
        )
        await interaction.channel.send(embed=embed, view=AdminPanel())

        note = f" (ลบแผงเก่าออก {removed} อัน)" if removed else ""
        await interaction.followup.send(f"โพสต์เมนูแอดมินแล้วค่ะ{note}", ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    bot.add_view(AdminPanel())
    await bot.add_cog(AdminPanelCog(bot))
