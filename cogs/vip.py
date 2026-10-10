"""ระบบ VIP Self-Service: ซื้อ / ต่ออายุ / อัปเกรด / ตรวจสอบสิทธิ์ (multi-tier: Lace, Desire, Obsession)"""
from __future__ import annotations

import datetime as dt
import logging
import json
import re

import discord
from discord import app_commands
from discord.ext import commands

from core.embeds import COLOR_DANGER, COLOR_GOLD, COLOR_INFO, COLOR_OK, COLOR_WARN, dm_embed
from core.pricing import apply_discount
from core.utils import fmt_datetime, from_iso, is_admin, money, now_utc, send_dm, to_iso
from core.vip_logic import (
    DEFAULT_VIP_BENEFITS,
    DEFAULT_VIP_DATE_SERVICE,
    DEFAULT_VIP_PACKAGES,
    DEFAULT_VIP_TIERS,
    VIP_DATE_KEY,
    active_tier,
    perks_lines,
    shop_day_start,
    vip_benefit,
    compute_new_expiry,
    cycle_month_key,
    duration_label,
    free_upgrade_expiry,
    next_streak_months,
)

log = logging.getLogger("olp.vip")


def _unit_label(pkg: dict) -> str:
    if pkg["unit"] == "month":
        return f"{int(pkg.get('months', 1))} เดือน"
    return "1 ปี"


def ensure_vip_defaults(cfg) -> bool:
    """เติมค่า Pandora VIP (ระดับ/แพ็กเกจ/สิทธิ์/บริการ Free Date) ลง config ถ้ายังไม่มี — คืน True ถ้ามีการเติม"""
    changed = False
    for key, default in (
        ("vip_tiers", DEFAULT_VIP_TIERS),
        ("vip_packages", DEFAULT_VIP_PACKAGES),
        ("vip_benefits", DEFAULT_VIP_BENEFITS),
    ):
        if not cfg.data.get(key):
            cfg.data[key] = json.loads(json.dumps(default))
            changed = True
    # ย้ายแพ็กเกจเดิม (6 เดือน 365 บาท ที่ยังไม่เคยแก้) ไปเป็น 1 เดือน 149 / 3 เดือน 299
    pkgs = cfg.data.get("vip_packages") or []
    if len(pkgs) == 1 and pkgs[0].get("key") == "pandora_6m" and float(pkgs[0].get("price", 0)) == 365:
        cfg.data["vip_packages"] = json.loads(json.dumps(DEFAULT_VIP_PACKAGES))
        changed = True
    if cfg.service(VIP_DATE_KEY) is None:
        cfg.data.setdefault("services", []).append(dict(DEFAULT_VIP_DATE_SERVICE))
        changed = True
    if changed:
        cfg.save()
    return changed


def perks_text(cfg) -> str:
    return "\n".join(f"• {p}" for p in perks_lines(cfg))


class DiscountModal(discord.ui.Modal, title="ยืนยันการสั่งซื้อ"):
    code = discord.ui.TextInput(
        label="โค้ดส่วนลด (ถ้ามี)",
        placeholder="เว้นว่างได้ถ้าไม่มีโค้ด",
        required=False,
        max_length=32,
    )

    def __init__(self, shop: "VipShopView") -> None:
        super().__init__()
        self.shop = shop

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.shop.checkout(interaction, str(self.code).strip())


class VipShopView(discord.ui.View):
    def __init__(self, cog: "VipCog", buyer_id: int) -> None:
        super().__init__(timeout=600)
        self.cog = cog
        self.cfg = cog.cfg
        self.buyer_id = buyer_id
        self.package_key: str | None = None

        purchasable_tiers = {t["key"] for t in self.cfg.purchasable_vip_tiers()}
        packages = [p for p in self.cfg.vip_packages if p["tier"] in purchasable_tiers]
        self.package_select = discord.ui.Select(
            placeholder="💎 เลือกแพ็กเกจ VIP",
            row=0,
            options=[
                discord.SelectOption(
                    label=pkg["name"],
                    value=pkg["key"],
                    emoji=pkg.get("emoji"),
                    description=f"{pkg['price']:,.0f} บาท / {_unit_label(pkg)}",
                )
                for pkg in packages[:25]
            ]
            or [discord.SelectOption(label="ยังไม่ได้ตั้งค่าแพ็กเกจ", value="none")],
        )
        self.package_select.callback = self._on_package
        self.add_item(self.package_select)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.buyer_id

    async def _on_package(self, interaction: discord.Interaction) -> None:
        value = self.package_select.values[0]
        self.package_key = None if value == "none" else value
        await interaction.response.edit_message(embed=self.embed(), view=self)

    def embed(self) -> discord.Embed:
        embed = discord.Embed(
            title="💎 สมัคร / ต่ออายุ VIP",
            description=(
                "1️⃣ เลือกแพ็กเกจ　2️⃣ กด **ยืนยัน & ใส่โค้ดส่วนลด**　3️⃣ ชำระผ่าน QR ใน DM"
            ),
            color=COLOR_GOLD,
        )
        embed.set_footer(text="ต่ออายุก่อนหมดอายุ = สะสมต่อจากวันเดิม · เปลี่ยนระดับ = เริ่มนับใหม่")
        perks = perks_text(self.cfg)
        if perks:
            embed.add_field(name="สิทธิ์สมาชิก VIP", value=perks[:1024], inline=False)
        if self.package_key:
            pkg = self.cfg.vip_package(self.package_key)
            embed.add_field(
                name="แพ็กเกจที่เลือก",
                value=f"**{pkg['name']}**\nราคา {money(pkg['price'])} · อายุ {_unit_label(pkg)}",
                inline=False,
            )
        else:
            embed.add_field(name="แพ็กเกจที่เลือก", value="*ยังไม่เลือก*", inline=False)
        embed.set_footer(text="ระบบจะส่ง QR ชำระเงินไปที่ DM ของคุณ")
        return embed

    @discord.ui.button(
        label="ยืนยัน & ใส่โค้ดส่วนลด", emoji="🏷️", style=discord.ButtonStyle.primary, row=1
    )
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not self.package_key:
            await interaction.response.send_message("ยังไม่ได้เลือกแพ็กเกจค่ะ", ephemeral=True)
            return
        await interaction.response.send_modal(DiscountModal(self))

    async def checkout(self, interaction: discord.Interaction, code: str) -> None:
        pkg = self.cfg.vip_package(self.package_key)
        if pkg is None:
            await interaction.response.send_message("ไม่พบแพ็กเกจนี้ค่ะ", ephemeral=True)
            return

        await interaction.response.defer()
        base = float(pkg["price"])
        total, discount, used_code = apply_discount(self.cfg, code, base)

        order_id = await self.cog.db.create_vip_order(
            guild_id=interaction.guild_id or self.cfg.guild_id,
            customer_id=interaction.user.id,
            package_key=pkg["key"],
            base_price=base,
            discount_code=used_code,
            discount_amount=discount,
            total_price=total,
            status="AWAITING_PAYMENT",
            created_at=to_iso(now_utc()),
        )
        order = await self.cog.db.get_vip_order(order_id)

        payments = self.cog.bot.get_cog("PaymentsCog")
        await payments.start_vip_payment(order)

        note = ""
        if code and used_code is None:
            note = "\n⚠️ โค้ดส่วนลดที่กรอกไม่ถูกต้อง ระบบจึงคิดราคาปกติค่ะ"

        self.stop()
        await interaction.edit_original_response(
            embed=dm_embed(
                "📨 ส่งรายละเอียดการชำระเงินให้แล้ว",
                [
                    ("💎", "คำสั่งซื้อ", f"`V#{order_id}` · {pkg['name']}"),
                    ("💰", "ยอดชำระ", f"**{money(total)}**"),
                ],
                note="ตรวจสอบ DM ของบอท สแกน QR แล้วส่งภาพสลิปกลับมาได้เลยค่ะ" + note,
                color=COLOR_OK,
            ),
            view=None,
        )


# ------------------------------------------------------- ปุ่มอนุมัติอัปเกรดฟรี
class VipUpgradeDecisionButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"olp:vipup_(?P<action>ok|no):(?P<request_id>\d+)",
):
    def __init__(self, action: str, request_id: int) -> None:
        self.action = action
        self.request_id = request_id
        approve = action == "ok"
        super().__init__(
            discord.ui.Button(
                label="อนุมัติอัปเกรดฟรี" if approve else "ปฏิเสธ",
                emoji="✅" if approve else "❌",
                style=discord.ButtonStyle.success if approve else discord.ButtonStyle.danger,
                custom_id=f"olp:vipup_{action}:{request_id}",
            )
        )

    @classmethod
    async def from_custom_id(cls, interaction, item, match: re.Match[str]):
        return cls(match["action"], int(match["request_id"]))

    async def callback(self, interaction: discord.Interaction) -> None:  # type: ignore[override]
        cog: VipCog = interaction.client.get_cog("VipCog")  # type: ignore[assignment]
        if not is_admin(interaction.user, cog.cfg.admin_role_id):
            await interaction.response.send_message("เฉพาะแอดมินเท่านั้นค่ะ", ephemeral=True)
            return
        await interaction.response.defer()
        if self.action == "ok":
            ok, msg = await cog.approve_upgrade_request(self.request_id, interaction.user)
        else:
            ok, msg = await cog.reject_upgrade_request(self.request_id, interaction.user)

        message = interaction.message
        if message is not None:
            embed = message.embeds[0] if message.embeds else discord.Embed()
            embed.color = COLOR_OK if ok else COLOR_DANGER
            embed.add_field(name="ผลการตรวจสอบ", value=msg, inline=False)
            await message.edit(embed=embed, view=None)


def upgrade_request_view(request_id: int) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(VipUpgradeDecisionButton("ok", request_id))
    view.add_item(VipUpgradeDecisionButton("no", request_id))
    return view


class VipCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.cfg = bot.cfg
        self.db = bot.db

    # --------------------------------------------------------------- ร้าน
    async def cog_load(self) -> None:
        if self.cfg.vip_enabled:
            ensure_vip_defaults(self.cfg)

    async def _vip_off(self, interaction: discord.Interaction) -> bool:
        if self.cfg.vip_enabled:
            return False
        await interaction.response.send_message("ระบบ VIP ปิดใช้งานชั่วคราวค่ะ", ephemeral=True)
        return True

    async def open_vip_shop(self, interaction: discord.Interaction) -> None:
        if await self._vip_off(interaction):
            return
        view = VipShopView(self, interaction.user.id)
        await interaction.response.send_message(embed=view.embed(), view=view, ephemeral=True)

    def _welcome_embed(self, title: str, tier_cfg: dict, expires: dt.datetime) -> discord.Embed:
        embed = dm_embed(
            title,
            [
                ("💎", "ระดับ", f"**{tier_cfg.get('emoji', '')} {tier_cfg['name']}**"),
                ("📅", "หมดอายุ", f"**{fmt_datetime(expires, self.cfg.tz)}**"),
            ],
            note="ใช้บริการครั้งต่อไป บอทให้สิทธิ์ VIP อัตโนมัติ · ดูสิทธิ์ได้ที่ 🔍 ตรวจสอบสิทธิ์ VIP",
            color=COLOR_OK,
        )
        perks = perks_text(self.cfg)
        if perks:
            embed.add_field(name="✨ สิทธิ์ของคุณ", value=perks[:1024], inline=False)
        return embed

    async def check_vip(self, interaction: discord.Interaction) -> None:
        if await self._vip_off(interaction):
            return
        now_local = dt.datetime.now(self.cfg.tz)
        tier = await active_tier(self.db, interaction.user.id, now_local)

        if tier is None:
            embed = dm_embed(
                "🔍 สถานะสิทธิ์ VIP",
                lead="ตอนนี้คุณยังไม่มีสิทธิ์ VIP ค่ะ",
                note="กดปุ่ม 💎 สมัคร VIP / ต่ออายุ เพื่อเลือกแพ็กเกจได้เลย",
                color=COLOR_INFO,
            )
            perks = perks_text(self.cfg)
            if perks:
                embed.add_field(name="✨ สิทธิ์สมาชิก VIP", value=perks[:1024], inline=False)
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        record = await self.db.get_vip_member(interaction.user.id)
        expires = from_iso(record["expires_at"])
        tier_cfg = self.cfg.vip_tier(tier)

        days_left = max((expires - now_local).days, 0)
        embed = discord.Embed(
            title="🔍 สถานะสิทธิ์ VIP",
            description=f"## {tier_cfg.get('emoji', '')} {tier_cfg['name']}\n┗ เหลืออีก **{days_left}** วัน",
            color=COLOR_GOLD,
        )
        embed.add_field(name="📅 หมดอายุ", value=fmt_datetime(expires, self.cfg.tz), inline=True)
        embed.add_field(name="🏅 เดือนสะสม", value=f"{record['streak_months']} เดือน", inline=True)

        if tier_cfg.get("upgrade_to"):
            need = int(tier_cfg["upgrade_streak_months"])
            remain = max(need - int(record["streak_months"]), 0)
            embed.add_field(
                name="เงื่อนไขอัปเกรดฟรี",
                value=(
                    f"สะสมครบ {need} เดือน จะได้อัปเกรดเป็น {self.cfg.vip_tier_name(tier_cfg['upgrade_to'])} ฟรี 1 เดือน\n"
                    + (f"อีก {remain} เดือน" if remain else "ครบแล้ว รอแอดมินอนุมัติ")
                ),
                inline=False,
            )

        if self.cfg.service(VIP_DATE_KEY) and int(vip_benefit(self.cfg, "free_date_per_day")) > 0:
            limit = int(vip_benefit(self.cfg, "free_date_per_day"))
            rows = await self.db.fetchall(
                "SELECT services FROM jobs WHERE customer_id = ? AND status != 'CANCELLED' AND created_at >= ?",
                (interaction.user.id, to_iso(shop_day_start(self.cfg, now_local))),
            )
            used = sum(json.loads(r["services"] or "[]").count(VIP_DATE_KEY) for r in rows)
            embed.add_field(
                name="💎 Free Date วันนี้",
                value=f"ใช้ไป {min(used, limit)}/{limit} ครั้ง แจ้งแอดมินตอนจองได้เลยค่ะ" if used < limit
                else f"ใช้ครบ {limit}/{limit} ครั้งแล้ว (รีเซ็ตวันทำการถัดไป)",
                inline=False,
            )
        perks = perks_text(self.cfg)
        if perks:
            embed.add_field(name="สิทธิ์ของคุณ", value=perks[:1024], inline=False)

        quota_lines = await self._quota_status_lines(interaction.user.id, tier, now_local)
        if quota_lines:
            embed.add_field(name="สิทธิ์ฟรีเดือนนี้", value="\n".join(quota_lines), inline=False)

        await interaction.response.send_message(embed=embed, ephemeral=True)

    async def _quota_status_lines(self, user_id: int, tier: str, now_local: dt.datetime) -> list[str]:
        cycle = cycle_month_key(now_local)
        lines: list[str] = []
        for svc in self.cfg.services:
            entry = self.cfg.service_pricing_entry(svc, tier)
            if not isinstance(entry, dict):
                continue
            if entry.get("unlimited"):
                lines.append(f"• {svc['name']} · ไม่จำกัด")
                continue
            limit = int(entry.get("free_per_month", 0))
            used = await self.db.get_quota_used(user_id, svc["key"], cycle)
            lines.append(f"• {svc['name']} · ใช้ไป {min(used, limit)}/{limit} ครั้ง")
        return lines

    # ------------------------------------------------- คำนวณ+บันทึกสิทธิ์ (ใช้ร่วมกัน)
    async def _apply_grant(
        self,
        *,
        guild_id: int,
        customer_id: int,
        tier_key: str,
        unit: str,
        months: int,
        package_key: str | None = None,
    ) -> tuple[dt.datetime, int, str, dict]:
        """คำนวณวันหมดอายุ/เดือนสะสมตาม stacking logic แล้วบันทึก + สลับ Role ให้

        คืนค่า (วันหมดอายุใหม่ (tz ไทย), เดือนสะสมใหม่, หมายเหตุเรื่อง role, tier_cfg)
        """
        tier_cfg = self.cfg.vip_tier(tier_key)
        if tier_cfg is None:
            raise ValueError(f"ไม่พบระดับ VIP `{tier_key}` ใน config")

        now_local = dt.datetime.now(self.cfg.tz)
        existing = await self.db.get_vip_member(customer_id)
        previous_expiry_utc = from_iso(existing["expires_at"]) if existing and existing["expires_at"] else None
        previous_expiry_local = previous_expiry_utc.astimezone(self.cfg.tz) if previous_expiry_utc else None
        still_active = bool(previous_expiry_utc and previous_expiry_utc > now_utc())
        same_tier = bool(existing and existing["tier"] == tier_key)

        new_expiry_local = compute_new_expiry(
            now_local=now_local,
            unit=unit,
            package_months=months,
            previous_expiry=previous_expiry_local,
            same_tier_renewal=same_tier,
        )
        new_streak = next_streak_months(
            current_streak=int(existing["streak_months"]) if existing else 0,
            same_tier_renewal=same_tier,
            still_active=still_active,
            # ให้เป็นวัน: นับเดือนสะสมเฉพาะส่วนที่ครบ 30 วัน
            months_added=months // 30 if unit == "day" else months,
        )

        role_note = await self._swap_role(guild_id, customer_id, existing, tier_cfg)

        now_iso = to_iso(now_utc())
        expires_iso = to_iso(new_expiry_local)
        await self.db.upsert_vip_member(
            customer_id,
            tier_key,
            package_key,
            int(tier_cfg.get("role_id") or 0),
            new_streak,
            expires_iso,
            now_iso,
        )
        return new_expiry_local, new_streak, role_note, tier_cfg

    # ---------------------------------------------------- ยืนยันสลิป -> role
    async def activate_order(self, order_id: int, admin: discord.abc.User) -> tuple[bool, str]:
        order = await self.db.get_vip_order(order_id)
        if order is None:
            return False, "ไม่พบคำสั่งซื้อนี้"
        if order["status"] == "ACTIVE":
            return False, "คำสั่งซื้อนี้ถูกยืนยันไปแล้ว"
        if order["status"] not in ("AWAITING_PAYMENT", "SLIP_PENDING"):
            return False, "คำสั่งซื้อนี้ถูกยกเลิกไปแล้ว ยืนยันไม่ได้"

        pkg = self.cfg.vip_package(order["package_key"])
        if pkg is None:
            return False, "ไม่พบแพ็กเกจนี้ใน config"
        if self.cfg.vip_tier(pkg["tier"]) is None:
            return False, f"ไม่พบระดับ VIP `{pkg['tier']}` ใน config"

        # ล็อกคำสั่งซื้อก่อนให้สิทธิ์ — กดยืนยันซ้ำ/กดยืนยันพร้อมยกเลิก จะผ่านได้ครั้งเดียว
        if not await self.db.execute_count(
            "UPDATE vip_orders SET status = 'ACTIVATING' WHERE id = ? AND status IN ('AWAITING_PAYMENT', 'SLIP_PENDING')",
            (order_id,),
        ):
            return False, "คำสั่งซื้อนี้ถูกดำเนินการไปแล้ว"
        try:
            new_expiry_local, new_streak, role_note, tier_cfg = await self._apply_grant(
                guild_id=order["guild_id"],
                customer_id=order["customer_id"],
                tier_key=pkg["tier"],
                unit=pkg["unit"],
                months=int(pkg["months"]),
                package_key=order["package_key"],
            )
        except Exception as exc:
            await self.db.update_vip_order(order_id, status=order["status"])  # ปลดล็อกให้ลองใหม่ได้
            if isinstance(exc, ValueError):
                return False, str(exc)
            raise

        now_iso = to_iso(now_utc())
        expires_iso = to_iso(new_expiry_local)
        await self.db.update_vip_order(order_id, status="ACTIVE", paid_at=now_iso, expires_at=expires_iso)
        payments = self.bot.get_cog("PaymentsCog")
        if payments is not None:
            await payments.release_pending_slip(order["customer_id"], "VIP", order_id)
            await payments.log_vip_to_sheet(await self.db.get_vip_order(order_id))
        else:
            await self.db.clear_pending_slip(order["customer_id"], "VIP", order_id)

        await send_dm(
            self.bot,
            order["customer_id"],
            embed=self._welcome_embed("🎉 ยินดีต้อนรับสู่ VIP!", tier_cfg, new_expiry_local),
        )

        await self._check_streak_upgrade(order["guild_id"], order["customer_id"], tier_cfg, new_streak)

        return True, (
            f"ยืนยันสลิป VIP `V#{order_id}` แล้ว โดย {admin.mention}\n"
            f"ตั้งระดับ {tier_cfg['name']} หมดอายุ {fmt_datetime(new_expiry_local, self.cfg.tz)}{role_note}"
        )

    async def _swap_role(
        self, guild_id: int, customer_id: int, existing: dict | None, new_tier_cfg: dict
    ) -> str:
        guild = self.bot.get_guild(guild_id) or self.bot.get_guild(self.cfg.guild_id)
        if guild is None:
            return "\n⚠️ ไม่พบเซิร์ฟเวอร์ (ตรวจสอบ guild_id ใน config)"
        member = guild.get_member(customer_id)
        if member is None:
            return "\n⚠️ ไม่พบสมาชิกในเซิร์ฟเวอร์"

        note = ""
        if existing and existing.get("role_id") and existing["role_id"] != new_tier_cfg.get("role_id"):
            old_role = guild.get_role(int(existing["role_id"]))
            if old_role and old_role in member.roles:
                try:
                    await member.remove_roles(old_role, reason="เปลี่ยนระดับ VIP")
                except discord.Forbidden:
                    pass

        new_role = guild.get_role(int(new_tier_cfg.get("role_id") or 0))
        if new_role is None:
            return note + "\n⚠️ ไม่พบ Role ของระดับนี้ (ตรวจสอบ role_id ใน config)"
        try:
            await member.add_roles(new_role, reason="ยืนยัน VIP")
        except discord.Forbidden:
            note += "\n⚠️ บอทไม่มีสิทธิ์ให้ Role (ตรวจสอบลำดับ Role ของบอท)"
        return note

    # ------------------------------------------------- สะสมครบ 12 เดือน -> แจ้งแอดมิน
    async def _check_streak_upgrade(
        self, guild_id: int, customer_id: int, tier_cfg: dict, streak_months: int
    ) -> None:
        upgrade_to = tier_cfg.get("upgrade_to")
        threshold = tier_cfg.get("upgrade_streak_months")
        if not upgrade_to or not threshold or streak_months < int(threshold):
            return
        if await self.db.pending_upgrade_request(customer_id, upgrade_to):
            return

        request_id = await self.db.create_upgrade_request(
            guild_id=guild_id,
            user_id=customer_id,
            from_tier=tier_cfg["key"],
            to_tier=upgrade_to,
            streak_months=streak_months,
            status="PENDING",
            created_at=to_iso(now_utc()),
        )
        to_name = self.cfg.vip_tier_name(upgrade_to)
        embed = dm_embed(
            "🎁 ลูกค้าสะสมครบ รออนุมัติอัปเกรดฟรี",
            [
                ("👤", "ลูกค้า", f"<@{customer_id}>"),
                ("🏅", "สะสม", f"**{tier_cfg['name']}** ต่อเนื่อง **{streak_months} เดือน**"),
                ("⬆️", "อัปเกรดเป็น", f"**{to_name}** (ฟรี 1 เดือน)"),
            ],
            color=COLOR_WARN,
        )
        payments = self.bot.get_cog("PaymentsCog")
        msg = await payments.notify_admin(embed=embed, view=upgrade_request_view(request_id))
        if msg is not None:
            await self.db.update_upgrade_request(request_id, admin_msg_id=msg.id)

    async def approve_upgrade_request(
        self, request_id: int, admin: discord.abc.User
    ) -> tuple[bool, str]:
        req = await self.db.get_upgrade_request(request_id)
        if req is None:
            return False, "ไม่พบคำขอนี้"
        if req["status"] != "PENDING":
            return False, "คำขอนี้ถูกดำเนินการไปแล้วค่ะ"

        to_tier_cfg = self.cfg.vip_tier(req["to_tier"])
        if to_tier_cfg is None:
            return False, f"ไม่พบระดับ `{req['to_tier']}` ใน config"

        now_local = dt.datetime.now(self.cfg.tz)
        existing = await self.db.get_vip_member(req["user_id"])
        previous_expiry_local = (
            from_iso(existing["expires_at"]).astimezone(self.cfg.tz)
            if existing and existing["expires_at"]
            else now_local
        )
        new_expiry_local = free_upgrade_expiry(previous_expiry_local, now_local)

        role_note = await self._swap_role(req["guild_id"], req["user_id"], existing, to_tier_cfg)

        now_iso = to_iso(now_utc())
        expires_iso = to_iso(new_expiry_local)
        await self.db.upsert_vip_member(
            req["user_id"],
            req["to_tier"],
            None,
            int(to_tier_cfg.get("role_id") or 0),
            0,  # เริ่มนับเดือนสะสมของระดับใหม่ใหม่ตั้งแต่ 0
            expires_iso,
            now_iso,
        )
        await self.db.update_upgrade_request(
            request_id, status="APPROVED", handled_at=now_iso, handled_by=admin.id
        )

        await send_dm(
            self.bot,
            req["user_id"],
            embed=dm_embed(
                "🎁 อัปเกรด VIP ฟรีเรียบร้อย!",
                [
                    ("💎", "ระดับใหม่", f"**{to_tier_cfg['name']}** (ฟรี 1 เดือน)"),
                    ("📅", "หมดอายุ", f"**{fmt_datetime(new_expiry_local, self.cfg.tz)}**"),
                ],
                lead=f"ยินดีด้วยค่ะ คุณสะสมครบ {req['streak_months']} เดือน 🎉",
                color=COLOR_OK,
            ),
        )
        return True, f"อนุมัติอัปเกรดเป็น {to_tier_cfg['name']} แล้ว โดย {admin.mention}{role_note}"

    async def reject_upgrade_request(
        self, request_id: int, admin: discord.abc.User
    ) -> tuple[bool, str]:
        req = await self.db.get_upgrade_request(request_id)
        if req is None:
            return False, "ไม่พบคำขอนี้"
        if req["status"] != "PENDING":
            return False, "คำขอนี้ถูกดำเนินการไปแล้วค่ะ"
        await self.db.update_upgrade_request(
            request_id, status="REJECTED", handled_at=to_iso(now_utc()), handled_by=admin.id
        )
        return True, f"ปฏิเสธคำขออัปเกรดแล้ว โดย {admin.mention}"

    # ------------------------------------------------------ ตรวจสิทธิ์หมดอายุ
    async def expire_pass(self, record: dict) -> None:
        guild = self.bot.get_guild(self.cfg.guild_id)
        if guild is not None:
            member = guild.get_member(record["user_id"])
            role = guild.get_role(int(record["role_id"] or 0))
            if member and role and role in member.roles:
                try:
                    await member.remove_roles(role, reason="VIP หมดอายุ")
                except discord.Forbidden:
                    log.warning("ถอด Role VIP ของ %s ไม่สำเร็จ", record["user_id"])

        # เคลียร์ expires_at เพื่อไม่ให้ loop ตรวจซ้ำ แต่เก็บ tier ไว้อ้างอิงประวัติ และรีเซ็ตเดือนสะสม
        await self.db.execute(
            "UPDATE vip_members SET expires_at = NULL, streak_months = 0, updated_at = ? "
            "WHERE user_id = ?",
            (to_iso(now_utc()), record["user_id"]),
        )
        await send_dm(
            self.bot,
            record["user_id"],
            embed=dm_embed(
                "⌛ สิทธิ์ VIP หมดอายุแล้ว",
                lead="ขอบคุณที่เป็นสมาชิก VIP กับเรานะคะ 💜",
                note="ต่ออายุได้ที่ปุ่ม 💎 สมัคร VIP / ต่ออายุ ในแผงบริการค่ะ",
                color=COLOR_DANGER,
            ),
        )

    # ----------------------------------------------------------- คำสั่ง
    @app_commands.command(name="vip_grant", description="มอบ VIP ให้สมาชิก (แอดมิน) · 1 วัน ถึง 6 เดือน")
    @app_commands.describe(
        member="สมาชิกที่จะมอบ VIP",
        days="จำนวนวัน (1-180) · ใส่อย่างใดอย่างหนึ่งกับ months",
        months="จำนวนเดือน (1-6)",
        tier="ระดับ VIP (เว้นว่าง = ระดับแรก)",
    )
    async def vip_grant(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        days: app_commands.Range[int, 1, 180] | None = None,
        months: app_commands.Range[int, 1, 6] | None = None,
        tier: str | None = None,
    ) -> None:
        if not is_admin(interaction.user, self.cfg.admin_role_id):
            await interaction.response.send_message("เฉพาะแอดมินเท่านั้นค่ะ", ephemeral=True)
            return
        if days and months:
            await interaction.response.send_message("ใส่ **days** หรือ **months** อย่างใดอย่างหนึ่งค่ะ", ephemeral=True)
            return
        unit, amount = ("day", days) if days else ("month", months or 1)
        tier_key = tier or ((self.cfg.vip_tiers or [{}])[0].get("key") or "")
        await self.grant_vip(interaction, member, tier_key, amount, unit=unit)

    @vip_grant.autocomplete("tier")
    async def _tier_autocomplete(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        return [
            app_commands.Choice(name=t["name"][:100], value=t["key"])
            for t in self.cfg.vip_tiers
            if current.lower() in t["name"].lower()
        ][:25]

    async def grant_vip(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        tier_key: str,
        months: int,
        *,
        unit: str = "month",
    ) -> None:
        """มอบ VIP ด้วยมือ (ใช้ทั้งจาก /vip_grant และเมนูแอดมิน) — unit = "day" | "month" · ผู้เรียกต้องตรวจสิทธิ์แอดมินก่อน"""
        if not self.cfg.vip_enabled:
            await interaction.response.send_message(
                "ระบบ VIP ยังปิดอยู่ค่ะ เปิดที่ ⚙️ ตั้งค่าร้าน → 💎 VIP ก่อน", ephemeral=True
            )
            return
        if self.cfg.vip_tier(tier_key) is None:
            await interaction.response.send_message(
                f"ยังไม่ได้ตั้งค่าระดับ `{tier_key}` ใน config (vip_tiers)", ephemeral=True
            )
            return
        if member.bot:
            await interaction.response.send_message("มอบ VIP ให้บอทไม่ได้ค่ะ", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        try:
            new_expiry_local, new_streak, role_note, tier_cfg = await self._apply_grant(
                guild_id=interaction.guild_id,
                customer_id=member.id,
                tier_key=tier_key,
                unit=unit,
                months=months,
            )
        except ValueError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        duration = duration_label(unit, months)

        await send_dm(
            self.bot,
            member.id,
            embed=self._welcome_embed(f"🎁 คุณได้รับ VIP {duration}!", tier_cfg, new_expiry_local),
        )
        payments = self.bot.get_cog("PaymentsCog")
        if payments is not None:
            await payments.notify_admin_text(
                f"💎 {interaction.user.mention} มอบ **{tier_cfg['name']}** {duration} ให้ {member.mention} "
                f"— หมดอายุ {fmt_datetime(new_expiry_local, self.cfg.tz)}"
            )
        await self._check_streak_upgrade(interaction.guild_id, member.id, tier_cfg, new_streak)

        await interaction.followup.send(
            embed=dm_embed(
                "✅ ให้สิทธิ์ VIP แล้ว",
                [
                    ("👤", "สมาชิก", member.mention),
                    ("💎", "ระดับ", tier_cfg["name"]),
                    ("⏳", "ระยะเวลา", f"+{duration}"),
                    ("📅", "หมดอายุ", fmt_datetime(new_expiry_local, self.cfg.tz)),
                ],
                note=role_note.strip() or None,
                color=COLOR_OK,
            ),
            ephemeral=True,
        )


async def setup(bot: commands.Bot) -> None:
    bot.add_dynamic_items(VipUpgradeDecisionButton)
    await bot.add_cog(VipCog(bot))
