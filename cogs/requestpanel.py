"""Request Panel สำหรับลูกค้า: สอบถามเจ้าหน้าที่ / ดูเมนู & ราคา / จอง Party Room / Top Donate (+VIP ถ้าเปิดใช้)"""
from __future__ import annotations

import datetime as dt

import discord
from discord import app_commands
from discord.ext import commands

from core.embeds import COLOR_MAIN
from core.utils import is_admin, purge_old_panels


def menu_embed(cfg) -> discord.Embed:
    """เมนูบริการ + ราคา สร้างจาก config.json (แก้ราคาใน config แล้วเมนูเปลี่ยนตาม)"""
    embed = discord.Embed(title=f"📜 เมนูบริการ · {cfg.shop_name}", color=COLOR_MAIN)

    extends = {s["key"]: s for s in cfg.services if s.get("extend_only")}
    for svc in cfg.bookable_services():
        price = svc.get("pricing", {}).get("normal", 0)
        if svc.get("per_unit"):
            head = f"{price:,.0f} บาท / {svc.get('unit_label', 'หน่วย')}"
        elif svc.get("addon_for"):
            head = f"+{price:,.0f} บาท ต่อรอบ (บริการเสริม)"
        else:
            head = f"{price:,.0f} บาท / {svc.get('duration_minutes', 60)} นาที"

        lines = [f"**{head}**"]
        if svc.get("description"):
            lines.append(svc["description"])
        if svc.get("multi_staff"):
            lines.append(
                f"รวมพนักงาน {svc.get('included_staff', 1)} คน · เพิ่มคนละ {svc.get('extra_staff_price', 0):,.0f} บาท"
            )
        ext = next((e for k, e in extends.items() if k.startswith(svc["key"])), None)
        if ext:
            lines.append(
                f"{ext['name']}: {ext['pricing'].get('normal', 0):,.0f} บาท / {ext.get('duration_minutes', 0)} นาที"
            )
        if svc.get("addon_for"):
            bonus = [
                f"{cfg.service_name(k)} +{m} นาที" if m else f"{cfg.service_name(k)} (ไม่บวกเวลา)"
                for k, m in svc["addon_for"].items()
                if not (cfg.service(k) or {}).get("extend_only")
            ]
            if bonus:
                lines.append("ใช้คู่กับ: " + " · ".join(bonus))
        embed.add_field(name=f"{svc.get('emoji', '')} {svc['name']}", value="\n".join(lines)[:1024], inline=False)

    if cfg.top_donate_enabled:
        embed.add_field(
            name="🏆 Top Donate Service",
            value=(
                f"สิ้นเดือน ผู้ที่มียอดรวมสูงสุด (ขั้นต่ำ {cfg.top_donate_min:,.0f} บาท) "
                "พาพนักงานที่คุณโดเนทให้มากที่สุดไปเดทนอกร้านได้ — ขอให้ถามความเห็นของพนักงานก่อนนะคะ"
            ),
            inline=False,
        )
    embed.set_footer(text="เปิดบิล/ชำระเงินผ่านแอดมิน · บริการ 18+ ต้องตกลงกับพนักงานก่อนทุกครั้ง")
    return embed


class PartyBookingModal(discord.ui.Modal, title="จอง Private Party Room"):
    when = discord.ui.TextInput(
        label="วันและเวลาที่ต้องการ",
        placeholder="เช่น 12/10 21:00",
        required=True,
        max_length=40,
    )
    people = discord.ui.TextInput(
        label="จำนวนคนทั้งหมด (รวมเพื่อน+พนักงาน สูงสุด 10)",
        placeholder="เช่น 6",
        required=True,
        max_length=3,
    )
    staff = discord.ui.TextInput(
        label="พนักงานที่อยากได้ (ชื่อ)",
        placeholder="เช่น Mina, Luna, Hikari",
        style=discord.TextStyle.paragraph,
        required=True,
        max_length=300,
    )
    note = discord.ui.TextInput(
        label="หมายเหตุเพิ่มเติม",
        style=discord.TextStyle.paragraph,
        required=False,
        max_length=300,
    )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        topic = (
            "🎉 **คำขอจอง Private Party Room**\n"
            f"วัน/เวลา: {self.when.value}\n"
            f"จำนวนคน: {self.people.value}\n"
            f"พนักงานที่ต้องการ: {self.staff.value}"
            + (f"\nหมายเหตุ: {self.note.value}" if self.note.value else "")
        )
        await interaction.client.get_cog("TicketsCog").open_ticket(interaction, topic=topic)


class RequestPanel(discord.ui.View):
    def __init__(self, vip_enabled: bool = False) -> None:
        super().__init__(timeout=None)
        if not vip_enabled:
            self.remove_item(self.vip)
            self.remove_item(self.vip_check)

    @discord.ui.button(
        label="สอบถามเจ้าหน้าที่",
        emoji="💬",
        style=discord.ButtonStyle.primary,
        custom_id="olp:request:ticket",
        row=0,
    )
    async def ticket(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        cog = interaction.client.get_cog("TicketsCog")
        await cog.open_ticket(interaction)

    @discord.ui.button(
        label="เมนู & ราคา",
        emoji="📜",
        style=discord.ButtonStyle.secondary,
        custom_id="olp:request:menu",
        row=0,
    )
    async def menu(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_message(embed=menu_embed(interaction.client.cfg), ephemeral=True)

    @discord.ui.button(
        label="จอง Party Room",
        emoji="🎉",
        style=discord.ButtonStyle.success,
        custom_id="olp:request:party",
        row=0,
    )
    async def party(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_modal(PartyBookingModal())

    @discord.ui.button(
        label="โดเนทให้พนักงาน",
        emoji="💜",
        style=discord.ButtonStyle.success,
        custom_id="olp:request:donate",
        row=1,
    )
    async def donate(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.client.get_cog("DonateCog").open_donate(interaction)

    @discord.ui.button(
        label="Top Donate",
        emoji="🏆",
        style=discord.ButtonStyle.secondary,
        custom_id="olp:request:top_donate",
        row=1,
    )
    async def top_donate(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        scheduler = interaction.client.get_cog("SchedulerCog")
        now_local = dt.datetime.now(interaction.client.cfg.tz)
        await interaction.response.defer(ephemeral=True, thinking=True)
        embed = await scheduler.donate_embed(now_local.year, now_local.month, final=False)
        await interaction.followup.send(embed=embed, ephemeral=True)

    @discord.ui.button(
        label="ซื้อ VIP / ต่ออายุ",
        emoji="💎",
        style=discord.ButtonStyle.success,
        custom_id="olp:request:vip",
        row=1,
    )
    async def vip(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        cog = interaction.client.get_cog("VipCog")
        await cog.open_vip_shop(interaction)

    @discord.ui.button(
        label="ตรวจสอบสิทธิ์ VIP",
        emoji="🔍",
        style=discord.ButtonStyle.secondary,
        custom_id="olp:request:vip_check",
        row=1,
    )
    async def vip_check(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        cog = interaction.client.get_cog("VipCog")
        await cog.check_vip(interaction)


class RequestPanelCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.cfg = bot.cfg

    @app_commands.command(name="panel_request", description="โพสต์ Request Panel สำหรับลูกค้า")
    async def panel_request(self, interaction: discord.Interaction) -> None:
        if not is_admin(interaction.user, self.cfg.admin_role_id):
            await interaction.response.send_message("เฉพาะแอดมินเท่านั้นค่ะ", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        removed = await purge_old_panels(interaction.channel, self.bot.user.id, "olp:request:")

        advance = int(self.cfg.get("party_booking.advance_days", 2))
        lines = [
            "เลือกรายการที่ต้องการได้เลยค่ะ ระบบจะติดต่อกลับทาง **DM** ของบอท\n",
            "💬 **สอบถามเจ้าหน้าที่** — คุยกับแอดมินแบบตัวต่อตัวผ่าน DM (จองพนักงาน / สั่งบริการ)",
            "📜 **เมนู & ราคา** — ดูบริการทั้งหมดของร้าน",
            f"🎉 **จอง Party Room** — กรอกวันเวลา จำนวนคน และพนักงานที่ต้องการ (แจ้งล่วงหน้า {advance} วันก่อนร้านเปิด)",
            "💜 **โดเนทให้พนักงาน** — เลือกพนักงาน ใส่ยอด (หรือซื้อ Drink Friend) รับ QR แล้วส่งสลิปได้เอง",
            "🏆 **Top Donate** — ดูอันดับยอดโดเนทของเดือนนี้",
        ]
        if self.cfg.vip_enabled:
            lines += [
                "💎 **ซื้อ VIP / ต่ออายุ** — เลือกแพ็กเกจ ใส่โค้ดส่วนลด และชำระเงินได้เอง",
                "🔍 **ตรวจสอบสิทธิ์ VIP** — ดูแพ็กเกจและวันหมดอายุของคุณ",
            ]
        lines.append("\n*กรุณาเปิดรับข้อความ DM จากสมาชิกในเซิร์ฟเวอร์ก่อนใช้งานนะคะ*")

        embed = discord.Embed(
            title=f"✨ {self.cfg.shop_name} · บริการลูกค้า",
            description="\n".join(lines),
            color=COLOR_MAIN,
        )
        await interaction.channel.send(embed=embed, view=RequestPanel(self.cfg.vip_enabled))

        note = f" (ลบแผงเก่าออก {removed} อัน)" if removed else ""
        await interaction.followup.send(f"โพสต์ Request Panel แล้วค่ะ{note}", ephemeral=True)

    @app_commands.command(name="menu", description="ดูเมนูบริการและราคาของร้าน")
    async def menu(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(embed=menu_embed(self.cfg), ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    bot.add_view(RequestPanel(bot.cfg.vip_enabled))
    await bot.add_cog(RequestPanelCog(bot))
