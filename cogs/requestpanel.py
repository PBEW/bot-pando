"""Request Panel สำหรับลูกค้า: สอบถามเจ้าหน้าที่ / ดูเมนู & ราคา / พนักงานวันนี้ / โดเนท / Top Donate (+VIP ถ้าเปิดใช้)"""
from __future__ import annotations

import datetime as dt

import discord
from discord import app_commands
from discord.ext import commands

from core.coins import enabled as coin_enabled, label as coin_label, opt as coin_opt
from core.embeds import COLOR_MAIN
from core.utils import is_admin, purge_old_panels
from core.vip_logic import perks_lines


COMFORT_NOTE = (
    "ทุกการเข้าห้องบริการขึ้นอยู่กับ**ความสบายใจของพนักงานเป็นหลัก** "
    "พนักงานมีสิทธิ์ปฏิเสธหรือขอหยุดได้ทุกเมื่อ ขอบคุณที่เคารพกันนะคะ"
)


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
            inc = int(svc.get("included_staff", 1))
            lines.append(
                (f"รวมพนักงาน {inc} คน · " if inc > 1 else "")
                + f"พนักงานเพิ่มคนละ {svc.get('extra_staff_price', 0):,.0f} บาท (สูงสุด {svc.get('max_staff', 10)} คน)"
            )
        if int(svc.get("max_customers", 1)) > 1:
            price = float(svc.get("extra_customer_price", 0))
            lines.append(
                f"มากับเพื่อนได้ถึง {svc['max_customers']} คน · "
                + (f"เพิ่มคนละ {price:,.0f} บาท" if price else "ไม่คิดเพิ่ม")
                + (" (ขึ้นอยู่กับพนักงานยินยอม)" if svc.get("group_consent") else "")
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
    embed.add_field(name="💜 สำคัญ", value=COMFORT_NOTE, inline=False)
    embed.set_footer(text="เปิดบิล/ชำระเงินผ่านแอดมิน · บริการ 18+ ต้องตกลงกับพนักงานก่อนทุกครั้ง")
    return embed


class RequestPanel(discord.ui.View):
    def __init__(self, vip_enabled: bool = False, coins_enabled: bool = True) -> None:
        super().__init__(timeout=None)
        if not coins_enabled:
            for item in (self.coins_mine, self.coins_redeem, self.coins_top):
                self.remove_item(item)
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
        label="พนักงานวันนี้",
        emoji="👥",
        style=discord.ButtonStyle.success,
        custom_id="olp:request:staff_today",
        row=0,
    )
    async def staff_today(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        # private=False: ลูกค้าเห็นแค่ใครเข้างานและรับงานอะไร ไม่เห็นรายชื่อคนที่พนักงานไม่รับ
        embed = await interaction.client.get_cog("AttendanceCog").today_embed(private=False)
        embed.add_field(name="💜 สำคัญ", value=COMFORT_NOTE, inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)

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

    @discord.ui.button(label="เหรียญของฉัน", emoji="🪙", style=discord.ButtonStyle.primary, custom_id="olp:request:coins", row=2)
    async def coins_mine(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.client.get_cog("CoinsCog").my_coins(interaction)

    @discord.ui.button(label="แลกรางวัล", emoji="🎁", style=discord.ButtonStyle.success, custom_id="olp:request:redeem", row=2)
    async def coins_redeem(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.client.get_cog("CoinsCog").open_redeem(interaction)

    @discord.ui.button(label="อันดับนักสะสม", emoji="🏅", style=discord.ButtonStyle.secondary, custom_id="olp:request:coins_top", row=2)
    async def coins_top(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.client.get_cog("CoinsCog").show_leaderboard(interaction)

    @discord.ui.button(
        label="สมัคร VIP / ต่ออายุ",
        emoji="💎",
        style=discord.ButtonStyle.success,
        custom_id="olp:request:vip",
        row=3,
    )
    async def vip(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        cog = interaction.client.get_cog("VipCog")
        await cog.open_vip_shop(interaction)

    @discord.ui.button(
        label="ตรวจสอบสิทธิ์ VIP",
        emoji="🔍",
        style=discord.ButtonStyle.secondary,
        custom_id="olp:request:vip_check",
        row=3,
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

        lines = [
            "เลือกรายการที่ต้องการได้เลยค่ะ ระบบจะติดต่อกลับทาง **DM** ของบอท\n",
            "💬 **สอบถามเจ้าหน้าที่** — คุยกับแอดมินแบบตัวต่อตัวผ่าน DM (จองพนักงาน / สั่งบริการ)",
            "📜 **เมนู & ราคา** — ดูบริการทั้งหมดของร้าน",
            "👥 **พนักงานวันนี้** — ดูว่าวันนี้ใครเข้างาน และรับงานแบบไหนบ้าง",
            "💜 **โดเนทให้พนักงาน** — เลือกพนักงาน ใส่ยอด (หรือซื้อ Drink Friend) รับ QR แล้วส่งสลิปได้เอง",
            "🏆 **Top Donate** — ดูอันดับยอดโดเนทของเดือนนี้",
        ]
        if coin_enabled(self.cfg):
            lines += [
                f"\n{coin_label(self.cfg)} — ได้ 1 เหรียญทุก {coin_opt(self.cfg, 'baht_per_coin')} บาท "
                "สะสมแลกรางวัล (การ์ดแกล้ง 🃏, ส่วนลด, สั่ง CEO 👑, Host Night 🏰 ฯลฯ)",
                "🪙 **เหรียญของฉัน** · 🎁 **แลกรางวัล** · 🏅 **อันดับนักสะสม**",
            ]
        if self.cfg.vip_enabled:
            lines += [
                "💎 **สมัคร VIP / ต่ออายุ** — สมัครเองได้เลย เลือกแพ็กเกจ ใส่โค้ดส่วนลด แล้วชำระเงินผ่าน QR ใน DM",
                "🔍 **ตรวจสอบสิทธิ์ VIP** — ดูแพ็กเกจและวันหมดอายุของคุณ",
            ]
            pkg = (self.cfg.vip_packages or [None])[0]
            if pkg:
                lines.append(f"　💎 **{pkg['name']}** เพียง **{float(pkg['price']):,.0f} บาท** — สิทธิ์:")
            lines += [f"　• {p}" for p in perks_lines(self.cfg)]
        lines.append(f"\n> 💜 **สำคัญ:** {COMFORT_NOTE}")
        lines.append("\n*กรุณาเปิดรับข้อความ DM จากสมาชิกในเซิร์ฟเวอร์ก่อนใช้งานนะคะ*")

        embed = discord.Embed(
            title=f"✨ {self.cfg.shop_name} · บริการลูกค้า",
            description="\n".join(lines),
            color=COLOR_MAIN,
        )
        await interaction.channel.send(embed=embed, view=RequestPanel(self.cfg.vip_enabled, coin_enabled(self.cfg)))

        note = f" (ลบแผงเก่าออก {removed} อัน)" if removed else ""
        await interaction.followup.send(f"โพสต์ Request Panel แล้วค่ะ{note}", ephemeral=True)

    @app_commands.command(name="menu", description="ดูเมนูบริการและราคาของร้าน")
    async def menu(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(embed=menu_embed(self.cfg), ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    # ลงทะเบียนปุ่มเหรียญ/VIP เสมอ — เปิด/ปิดระบบระหว่างบอทรันอยู่ ปุ่มในแผงที่โพสต์ใหม่ก็ยังกดได้
    # (ตอนระบบปิด ปุ่มจะตอบว่าปิดใช้งานเอง)
    bot.add_view(RequestPanel(vip_enabled=True, coins_enabled=True))
    await bot.add_cog(RequestPanelCog(bot))
