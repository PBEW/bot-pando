"""เหรียญ Pandora: ได้เหรียญจากบิล/รีวิว/Top Donate, แลกรางวัลเป็นคูปอง, อันดับนักสะสม, หมดอายุ"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import re

import discord
from discord import app_commands
from discord.ext import commands

from core import coins
from core.embeds import COLOR_DANGER, COLOR_GOLD, COLOR_MAIN, COLOR_OK
from core.utils import display_name, from_iso, is_admin, money, now_utc, send_dm, to_iso

log = logging.getLogger("olp.coins")


def _fmt_date(value: str, tz) -> str:
    return from_iso(value).astimezone(tz).strftime("%d/%m/%Y")


# ===================================================================== แลกรางวัล
class RedeemView(discord.ui.View):
    def __init__(self, cog: "CoinsCog", user: discord.abc.User, bal: int) -> None:
        super().__init__(timeout=300)
        self.cog = cog
        self.user = user
        self.choice: str | None = None
        cfg = cog.cfg
        self.select = discord.ui.Select(
            placeholder="เลือกรางวัลที่จะแลก",
            options=[
                discord.SelectOption(
                    label=f"{r['name']} — {r['cost']} เหรียญ"[:100],
                    value=r["key"],
                    emoji=r.get("emoji") or None,
                    description=("✅ แลกได้" if bal >= int(r["cost"]) else f"ขาดอีก {int(r['cost']) - bal} เหรียญ")[:100],
                )
                for r in coins.rewards(cfg)[:25]
            ],
        )
        self.select.callback = self._on_pick
        self.add_item(self.select)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.user.id

    async def _on_pick(self, interaction: discord.Interaction) -> None:
        self.choice = self.select.values[0]
        await interaction.response.defer()

    @discord.ui.button(label="ยืนยันแลก", emoji="🎁", style=discord.ButtonStyle.success, row=1)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not self.choice:
            await interaction.response.send_message("เลือกรางวัลก่อนนะคะ", ephemeral=True)
            return
        self.stop()
        item = coins.reward(self.cog.cfg, self.choice) or {}
        if item.get("type") == "prank":
            await self.cog.open_prank_setup(interaction, item)
            return
        await self.cog.redeem(interaction, self.choice)


# ================================================================ เมนูแอดมิน
class AdminOnly(discord.ui.View):
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if is_admin(interaction.user, interaction.client.cfg.admin_role_id):
            return True
        await interaction.response.send_message("เฉพาะแอดมินเท่านั้นค่ะ", ephemeral=True)
        return False


class CoinAdminView(AdminOnly):
    """เมนูเหรียญ Pandora สำหรับแอดมิน (เปิดจาก /panel_admin)"""

    def __init__(self, cog: "CoinsCog", member: discord.abc.User | None = None, vouchers: list[dict] | None = None) -> None:
        super().__init__(timeout=600)
        self.cog = cog
        self.member = member
        self.vouchers = vouchers or []
        self.voucher_id: int | None = None
        cfg = cog.cfg

        self.member_select = discord.ui.UserSelect(
            placeholder="👤 เลือกลูกค้าเพื่อดู/ปรับเหรียญ",
            row=0,
            default_values=[discord.Object(id=member.id)] if member else [],
        )
        self.member_select.callback = self._on_member
        self.add_item(self.member_select)
        self.toggle.label = "ปิดระบบเหรียญ" if coins.enabled(cfg) else "เปิดระบบเหรียญ"
        self.toggle.style = discord.ButtonStyle.danger if coins.enabled(cfg) else discord.ButtonStyle.success

        self.voucher_select = discord.ui.Select(
            placeholder="🎟️ คูปองของลูกค้า (เลือกเพื่อกดว่าใช้แล้ว)",
            row=1,
            disabled=not self.vouchers,
            options=[
                discord.SelectOption(
                    label=f"V{v['id']} {(coins.reward(cfg, v['reward_key']) or {}).get('name', v['reward_key'])}"[:100],
                    value=str(v["id"]),
                    description=f"หมดอายุ {_fmt_date(v['expires_at'], cfg.tz)}",
                )
                for v in self.vouchers[:25]
            ]
            or [discord.SelectOption(label="ไม่มีคูปอง", value="-")],
        )
        self.voucher_select.callback = self._on_voucher
        self.add_item(self.voucher_select)

    async def embed(self) -> discord.Embed:
        cfg = self.cog.cfg
        mult = float(coins.opt(cfg, "event_multiplier"))
        on = coins.enabled(cfg)
        embed = discord.Embed(
            title=f"🪙 จัดการ{coins.opt(cfg, 'name')} — " + ("🟢 เปิดใช้งาน" if on else "🔴 ปิดใช้งาน"),
            color=COLOR_GOLD if on else COLOR_DANGER,
        )
        embed.add_field(
            name="ตั้งค่าปัจจุบัน",
            value=(
                f"1 เหรียญ / {coins.opt(cfg, 'baht_per_coin')} บาท · โดเนท 1 / {coins.opt(cfg, 'donate_baht_per_coin')} บาท\n"
                f"มาครั้งแรก +{coins.opt(cfg, 'first_visit_bonus')} · รีวิว +{coins.opt(cfg, 'review_bonus')} · "
                f"Top Donate +{coins.opt(cfg, 'top_donate_bonus')}\n"
                f"หมดอายุเมื่อไม่มา {coins.opt(cfg, 'expire_inactive_days')} วัน · "
                + ("อีเวนต์: ปิด" if mult <= 1 else f"🎉 อีเวนต์ ×{mult:g} อยู่")
            ),
            inline=False,
        )
        if self.member:
            uid = self.member.id
            embed.add_field(name=f"👤 {self.member.display_name}", value=(
                f"คงเหลือ **{await coins.balance(self.cog.db, uid):,}** · สะสมตลอดชีพ {await coins.lifetime(self.cog.db, uid):,}\n"
                f"คูปองที่ใช้ได้ {len(self.vouchers)} ใบ"
            ), inline=False)
            history = await self.cog.db.fetchall("SELECT * FROM coin_ledger WHERE user_id = ? ORDER BY id DESC LIMIT 5", (uid,))
            if history:
                embed.add_field(
                    name="ล่าสุด", value="\n".join(f"`{h['delta']:+d}` {h['reason']}" for h in history)[:1024], inline=False
                )
        else:
            embed.set_footer(text="เลือกลูกค้าด้านล่างเพื่อดูยอด ปรับเหรียญ หรือกดใช้คูปอง")
        return embed

    async def rerender(self, interaction: discord.Interaction, member: discord.abc.User | None) -> None:
        vouchers = await coins.active_vouchers(self.cog.db, member.id) if member else []
        view = CoinAdminView(self.cog, member, vouchers)
        await interaction.response.edit_message(embed=await view.embed(), view=view)

    async def _on_member(self, interaction: discord.Interaction) -> None:
        await self.rerender(interaction, self.member_select.values[0])

    async def _on_voucher(self, interaction: discord.Interaction) -> None:
        value = self.voucher_select.values[0]
        self.voucher_id = None if value == "-" else int(value)
        await interaction.response.defer()

    @discord.ui.button(label="ปรับเหรียญ", emoji="➕", style=discord.ButtonStyle.primary, row=2)
    async def adjust(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if self.member is None:
            await interaction.response.send_message("เลือกลูกค้าก่อนค่ะ", ephemeral=True)
            return
        await interaction.response.send_modal(AdjustModal(self))

    @discord.ui.button(label="ใช้คูปองที่เลือกแล้ว", emoji="✅", style=discord.ButtonStyle.success, row=2)
    async def use_voucher(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not self.voucher_id:
            await interaction.response.send_message("เลือกคูปองก่อนค่ะ", ephemeral=True)
            return
        result = await self.cog.mark_voucher_used(self.voucher_id)
        await self.cog._notify_admin(
            f"🎟️ {interaction.user.mention} บันทึกว่า {self.member.mention} ใช้คูปอง `V{self.voucher_id}` แล้ว — {result}"
        )
        await self.rerender(interaction, self.member)

    @discord.ui.button(label="เปิด/ปิดระบบเหรียญ", emoji="🔌", style=discord.ButtonStyle.danger, row=2)
    async def toggle(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        cfg = self.cog.cfg
        on = not coins.enabled(cfg)
        cfg.data.setdefault("coins", {})["enabled"] = on
        cfg.save()
        await self.cog._notify_admin(
            f"🔌 {interaction.user.mention} {'เปิด' if on else 'ปิด'}ระบบ{coins.opt(cfg, 'name')}"
            + ("" if on else " — ลูกค้าจะไม่ได้เหรียญเพิ่ม และแลก/ดูเหรียญไม่ได้จนกว่าจะเปิดใหม่")
        )
        await self.rerender(interaction, self.member)

    @discord.ui.button(label="อีเวนต์เหรียญ", emoji="🎉", style=discord.ButtonStyle.secondary, row=3)
    async def event(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_modal(EventModal(self))

    @discord.ui.button(label="ตั้งค่าเหรียญ", emoji="⚙️", style=discord.ButtonStyle.secondary, row=3)
    async def settings(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_modal(CoinSettingsModal(self))

    @discord.ui.button(label="แก้รางวัล", emoji="🎁", style=discord.ButtonStyle.secondary, row=3)
    async def rewards(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        view = RewardEditView(self)
        await interaction.response.edit_message(embed=view.embed(), view=view)

    @discord.ui.button(label="อันดับนักสะสม", emoji="🏅", style=discord.ButtonStyle.secondary, row=3)
    async def top(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.cog.show_leaderboard(interaction)


class AdjustModal(discord.ui.Modal, title="ปรับเหรียญ"):
    amount = discord.ui.TextInput(label="จำนวน (ใส่ - เพื่อลด เช่น -20)", max_length=7)
    reason = discord.ui.TextInput(label="เหตุผล", max_length=200)

    def __init__(self, view: CoinAdminView) -> None:
        super().__init__()
        self.view = view

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            amount = int(str(self.amount.value).replace(",", "").strip())
        except ValueError:
            amount = 0
        if amount == 0 or abs(amount) > 100_000:
            await interaction.response.send_message("⚠️ จำนวนต้องเป็นตัวเลข ไม่เป็น 0 และไม่เกิน 100,000 ค่ะ", ephemeral=True)
            return
        member, cog = self.view.member, self.view.cog
        reason = str(self.reason.value).strip()
        await cog._change(member.id, amount, "ADMIN", reason, by=interaction.user.id)
        await cog._notify_admin(f"🪙 {interaction.user.mention} ปรับเหรียญ {member.mention} {amount:+,} — {reason}")
        await self.view.rerender(interaction, member)


class EventModal(discord.ui.Modal, title="อีเวนต์เหรียญ"):
    multiplier = discord.ui.TextInput(label="ตัวคูณ (1 = ปิด, 2 = ได้ 2 เท่า, สูงสุด 5)", max_length=4)

    def __init__(self, view: CoinAdminView) -> None:
        super().__init__()
        self.view = view
        self.multiplier.default = f"{float(coins.opt(view.cog.cfg, 'event_multiplier')):g}"

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            value = float(str(self.multiplier.value).strip())
        except ValueError:
            value = 0
        if not 1 <= value <= 5:
            await interaction.response.send_message("⚠️ ตัวคูณต้องอยู่ระหว่าง 1 ถึง 5 ค่ะ", ephemeral=True)
            return
        await self.view.cog.set_event(interaction.user, value)
        await self.view.rerender(interaction, self.view.member)


COIN_SETTING_FIELDS = [
    # (key, label, min, max)
    ("baht_per_coin", "กี่บาทได้ 1 เหรียญ", 1, 10_000),
    ("donate_baht_per_coin", "โดเนทกี่บาทได้ 1 เหรียญ", 1, 10_000),
    ("first_visit_bonus", "โบนัสมาครั้งแรก (เหรียญ)", 0, 10_000),
    ("review_bonus", "โบนัสรีวิว (เหรียญ)", 0, 10_000),
    ("expire_inactive_days", "เหรียญหมดอายุถ้าไม่มากี่วัน (0 = ไม่หมด)", 0, 3650),
]


class CoinSettingsModal(discord.ui.Modal, title="ตั้งค่าเหรียญ"):
    def __init__(self, view: CoinAdminView) -> None:
        super().__init__()
        self.view = view
        self.inputs = []
        for key, text, lo, hi in COIN_SETTING_FIELDS:
            field = discord.ui.TextInput(label=text, default=str(coins.opt(view.cog.cfg, key)), max_length=6)
            self.inputs.append((key, text, lo, hi, field))
            self.add_item(field)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        values = {}
        for key, text, lo, hi, field in self.inputs:
            try:
                value = int(str(field.value).replace(",", "").strip())
            except ValueError:
                value = None
            if value is None or not lo <= value <= hi:
                await interaction.response.send_message(f"⚠️ **{text}** ต้องเป็นตัวเลข {lo}–{hi} ค่ะ", ephemeral=True)
                return
            values[key] = value
        cfg = self.view.cog.cfg
        section = cfg.data.setdefault("coins", {})
        changed = [f"{t}: {coins.opt(cfg, k)} → {values[k]}" for k, t, *_ in COIN_SETTING_FIELDS if coins.opt(cfg, k) != values[k]]
        section.update(values)
        cfg.save()
        if changed:
            await self.view.cog._notify_admin(
                f"⚙️ {interaction.user.mention} แก้ตั้งค่าเหรียญ\n" + "\n".join(f"• {c}" for c in changed)
            )
        await self.view.rerender(interaction, self.view.member)


class RewardEditView(AdminOnly):
    def __init__(self, parent: CoinAdminView, selected: str | None = None) -> None:
        super().__init__(timeout=600)
        self.parent = parent
        self.selected = selected
        cfg = parent.cog.cfg
        self.select = discord.ui.Select(
            placeholder="เลือกรางวัลที่จะแก้/เลิกแลก",
            options=[
                discord.SelectOption(
                    label=f"{r['name']} — {r['cost']} เหรียญ"[:100], value=r["key"], emoji=r.get("emoji") or None,
                    default=r["key"] == selected,
                )
                for r in coins.rewards(cfg)[:25]
            ] or [discord.SelectOption(label="ยังไม่มีรางวัล", value="-")],
        )
        self.select.callback = self._on_pick
        self.add_item(self.select)

    def embed(self) -> discord.Embed:
        cfg = self.parent.cog.cfg
        return discord.Embed(
            title="🎁 จัดการรางวัล",
            description="\n".join(
                f"{r.get('emoji', '')} **{r['name']}** — {r['cost']} เหรียญ · {coins.REWARD_TYPES.get(r.get('type'), r.get('type'))}"
                for r in coins.rewards(cfg)
            )
            + "\n\nเลือกรางวัล → ✏️ แก้ / 🗑️ เลิกแลก · ➕ เพิ่มรางวัลใหม่ · 🃏 แก้รายการท่าแกล้ง\n"
            "*เลิกแลกแล้ว คูปองที่ลูกค้าแลกไปก่อนหน้ายังใช้ได้จนหมดอายุ*",
            color=COLOR_GOLD,
        )

    async def _on_pick(self, interaction: discord.Interaction) -> None:
        value = self.select.values[0]
        self.selected = None if value == "-" else value
        await interaction.response.defer()

    @discord.ui.button(label="แก้", emoji="✏️", style=discord.ButtonStyle.primary, row=1)
    async def edit(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not self.selected:
            await interaction.response.send_message("เลือกรางวัลก่อนค่ะ", ephemeral=True)
            return
        await interaction.response.send_modal(RewardModal(self, self.selected))

    @discord.ui.button(label="เลิกแลก", emoji="🗑️", style=discord.ButtonStyle.danger, row=1)
    async def retire(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not self.selected:
            await interaction.response.send_message("เลือกรางวัลก่อนค่ะ", ephemeral=True)
            return
        item = _editable_reward(self.parent.cog.cfg, self.selected)
        item["retired"] = True
        self.parent.cog.cfg.save()
        await self.parent.cog._notify_admin(f"🗑️ {interaction.user.mention} เลิกแลกรางวัล **{item['name']}**")
        view = RewardEditView(self.parent)
        await interaction.response.edit_message(embed=view.embed(), view=view)

    @discord.ui.button(label="เพิ่มรางวัล", emoji="➕", style=discord.ButtonStyle.success, row=1)
    async def add(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        view = AddRewardView(self.parent)
        await interaction.response.edit_message(embed=view.embed(), view=view)

    @discord.ui.button(label="แก้ท่าแกล้ง", emoji="🃏", style=discord.ButtonStyle.secondary, row=1)
    async def pranks(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_modal(PrankListModal(self))

    @discord.ui.button(label="กลับ", emoji="⬅️", style=discord.ButtonStyle.secondary, row=2)
    async def back(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.parent.rerender(interaction, self.parent.member)


def _editable_reward(cfg, key: str) -> dict:
    """คืน dict รางวัลที่อยู่ใน config (คัดลอกค่าเริ่มต้นลง config ก่อนถ้ายังไม่เคยแก้)"""
    section = cfg.data.setdefault("coins", {})
    if "rewards" not in section:
        section["rewards"] = [dict(r) for r in coins.DEFAULT_REWARDS]
    return next(r for r in section["rewards"] if r["key"] == key)


ADD_REWARD_EXTRA = {
    # type: [(field, label, default)]
    "discount_amount": [("amount", "ลดกี่บาท", "20"), ("min_bill", "ใช้ได้เมื่อบิลถึงกี่บาท", "150")],
    "role": [("role_id", "ID ของ Role (0 = แอดมินให้เอง)", "0"), ("days", "ได้ Role กี่วัน", "30")],
    "manual": [("announce", "ข้อความประกาศ ({user} = ชื่อลูกค้า, ว่าง = ไม่ประกาศ)", "")],
    "prank": [("staff_bonus", "โบนัสพนักงานต่อครั้ง (บาท)", "10")],
    "shoutout": [],
}


class AddRewardView(AdminOnly):
    def __init__(self, parent: CoinAdminView) -> None:
        super().__init__(timeout=600)
        self.parent = parent
        self.kind = "manual"
        self.type_select = discord.ui.Select(
            options=[
                discord.SelectOption(label=label[:100], value=key, default=key == "manual")
                for key, label in coins.REWARD_TYPES.items()
            ]
        )
        self.type_select.callback = self._on_type
        self.add_item(self.type_select)

    @staticmethod
    def embed() -> discord.Embed:
        return discord.Embed(
            title="➕ เพิ่มรางวัลใหม่",
            description="1) เลือกประเภทรางวัล  2) กด **กรอกรายละเอียด**\n*ห้ามตั้งรางวัลที่เกี่ยวกับบริการ 18+*",
            color=COLOR_GOLD,
        )

    async def _on_type(self, interaction: discord.Interaction) -> None:
        self.kind = self.type_select.values[0]
        for o in self.type_select.options:
            o.default = o.value == self.kind
        await interaction.response.defer()

    @discord.ui.button(label="กรอกรายละเอียด", emoji="📝", style=discord.ButtonStyle.success, row=1)
    async def next(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_modal(AddRewardModal(self.parent, self.kind))

    @discord.ui.button(label="กลับ", emoji="⬅️", style=discord.ButtonStyle.secondary, row=1)
    async def back(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        view = RewardEditView(self.parent)
        await interaction.response.edit_message(embed=view.embed(), view=view)


class AddRewardModal(discord.ui.Modal, title="เพิ่มรางวัล"):
    name = discord.ui.TextInput(label="ชื่อรางวัล", max_length=60)
    emoji = discord.ui.TextInput(label="อีโมจิ", required=False, max_length=8)
    cost = discord.ui.TextInput(label="ราคา (เหรียญ)", max_length=6)

    def __init__(self, parent: CoinAdminView, kind: str) -> None:
        super().__init__()
        self.parent = parent
        self.kind = kind
        self.extra: dict[str, discord.ui.TextInput] = {}
        for field, text, default in ADD_REWARD_EXTRA.get(kind, []):
            inp = discord.ui.TextInput(label=text[:45], default=default or None, required=field != "announce", max_length=200)
            self.extra[field] = inp
            self.add_item(inp)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            cost = int(str(self.cost.value).replace(",", "").strip())
            if not 1 <= cost <= 100_000:
                raise ValueError
            extra = {}
            for field, inp in self.extra.items():
                raw = str(inp.value).strip()
                extra[field] = raw if field == "announce" else int(raw.replace(",", ""))
        except ValueError:
            await interaction.response.send_message("⚠️ ราคา/ตัวเลขต้องเป็นจำนวนเต็ม (ราคา 1–100,000) ค่ะ", ephemeral=True)
            return
        cfg = self.parent.cog.cfg
        section = cfg.data.setdefault("coins", {})
        if "rewards" not in section:
            section["rewards"] = [dict(r) for r in coins.DEFAULT_REWARDS]
        existing = {r["key"] for r in section["rewards"]}
        key, n = f"reward_{len(existing) + 1}", len(existing) + 1
        while key in existing:
            n += 1
            key = f"reward_{n}"
        item = {"key": key, "name": str(self.name.value).strip(), "cost": cost, "type": self.kind, **extra}
        if str(self.emoji.value).strip():
            item["emoji"] = str(self.emoji.value).strip()
        if self.kind == "manual":
            item["days"] = 90
            if not item.get("announce"):
                item.pop("announce", None)
        section["rewards"].append(item)
        cfg.save()
        await self.parent.cog._notify_admin(
            f"➕ {interaction.user.mention} เพิ่มรางวัล **{item['name']}** {cost} เหรียญ ({coins.REWARD_TYPES[self.kind]})"
        )
        view = RewardEditView(self.parent)
        await interaction.response.edit_message(embed=view.embed(), view=view)


class PrankListModal(discord.ui.Modal, title="รายการท่าแกล้ง"):
    items = discord.ui.TextInput(
        label="ท่าแกล้ง (บรรทัดละ 1 ท่า สูงสุด 25)",
        style=discord.TextStyle.paragraph,
        max_length=3000,
    )

    def __init__(self, view: RewardEditView) -> None:
        super().__init__()
        self.view = view
        self.items.default = "\n".join(coins.pranks(view.parent.cog.cfg))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        lines = [ln.strip() for ln in str(self.items.value).splitlines() if ln.strip()]
        if not 1 <= len(lines) <= 25:
            await interaction.response.send_message("⚠️ ต้องมี 1–25 ท่าค่ะ", ephemeral=True)
            return
        cfg = self.view.parent.cog.cfg
        cfg.data.setdefault("coins", {})["pranks"] = [ln[:100] for ln in lines]
        cfg.save()
        await self.view.parent.cog._notify_admin(f"🃏 {interaction.user.mention} แก้รายการท่าแกล้ง ({len(lines)} ท่า)")
        view = RewardEditView(self.view.parent)
        await interaction.response.edit_message(embed=view.embed(), view=view)


# ============================================================== การ์ดแกล้ง
class PrankSetupView(discord.ui.View):
    """ลูกค้าเลือกท่าแกล้ง / เป้าหมาย / พนักงานที่จะไปแกล้ง (ยังไม่หักเหรียญจนกดยืนยัน)"""

    def __init__(self, cog: "CoinsCog", user, item: dict, performers: list[int], today: dict[int, dict]) -> None:
        super().__init__(timeout=300)
        self.cog, self.user, self.item, self.today = cog, user, item, today
        self.prank: str | None = None
        self.target = None
        self.performer: int | None = None
        guild = cog.bot.get_guild(cog.cfg.guild_id)

        self.prank_select = discord.ui.Select(
            placeholder="1) เลือกท่าแกล้ง", row=0,
            options=[discord.SelectOption(label=p[:100], value=str(i)) for i, p in enumerate(coins.pranks(cog.cfg)[:25])],
        )
        self.prank_select.callback = self._on_prank
        self.add_item(self.prank_select)

        self.target_select = discord.ui.UserSelect(placeholder="2) แกล้งใคร (เพื่อนที่มาด้วย / CEO / พนักงานที่เปิดรับ)", row=1)
        self.target_select.callback = self._on_target
        self.add_item(self.target_select)

        def staff_name(uid: int) -> str:
            m = guild.get_member(uid) if guild else None
            return m.display_name if m else str(uid)

        self.performer_select = discord.ui.Select(
            placeholder="3) ให้พนักงานคนไหนไปแกล้ง", row=2,
            options=[discord.SelectOption(label=staff_name(uid)[:100], value=str(uid)) for uid in performers[:25]],
        )
        self.performer_select.callback = self._on_performer
        self.add_item(self.performer_select)

    def embed(self) -> discord.Embed:
        return discord.Embed(
            title=f"🃏 {self.item['name']} — {self.item['cost']} เหรียญ",
            description=(
                "เลือกท่าแกล้ง → คนที่จะโดนแกล้ง → พนักงานที่จะไปแกล้ง แล้วกด **ส่งการ์ด**\n"
                "• แกล้งได้: เพื่อนที่มาด้วยกัน / CEO / พนักงานที่เปิดรับให้แกล้งวันนี้\n"
                "• พนักงานปฏิเสธได้ — ถ้าปฏิเสธหรือไม่ตอบใน 2 ชม. คืนเหรียญอัตโนมัติ\n"
                "*ห้ามแกล้งลูกค้าคนอื่นที่ไม่รู้เรื่อง · ไม่ใช่เรื่อง 18+ หรือทำให้อับอายจริง*"
            ),
            color=COLOR_GOLD,
        )

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.user.id

    async def _on_prank(self, interaction: discord.Interaction) -> None:
        self.prank = coins.pranks(self.cog.cfg)[int(self.prank_select.values[0])]
        await interaction.response.defer()

    async def _on_target(self, interaction: discord.Interaction) -> None:
        self.target = self.target_select.values[0]
        await interaction.response.defer()

    async def _on_performer(self, interaction: discord.Interaction) -> None:
        self.performer = int(self.performer_select.values[0])
        await interaction.response.defer()

    @discord.ui.button(label="ส่งการ์ด", emoji="🃏", style=discord.ButtonStyle.success, row=3)
    async def send(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not (self.prank and self.target and self.performer):
            await interaction.response.send_message("เลือกให้ครบ 3 ข้อก่อนนะคะ", ephemeral=True)
            return
        self.stop()
        await self.cog.start_prank(interaction, self.item, self.prank, self.target, self.performer, self.today)


class PrankDecisionButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"olp:prank_(?P<action>ok|no):(?P<vid>\d+)",
):
    def __init__(self, action: str, voucher_id: int) -> None:
        self.action, self.voucher_id = action, voucher_id
        ok = action == "ok"
        super().__init__(
            discord.ui.Button(
                label="รับการ์ด" if ok else "ปฏิเสธ",
                emoji="✅" if ok else "❌",
                style=discord.ButtonStyle.success if ok else discord.ButtonStyle.danger,
                custom_id=f"olp:prank_{action}:{voucher_id}",
            )
        )

    @classmethod
    async def from_custom_id(cls, interaction, item, match: re.Match[str]):
        return cls(match["action"], int(match["vid"]))

    async def callback(self, interaction: discord.Interaction) -> None:  # type: ignore[override]
        await interaction.client.get_cog("CoinsCog").decide_prank(interaction, self.voucher_id, self.action == "ok")


class RewardModal(discord.ui.Modal, title="แก้รางวัล"):
    name = discord.ui.TextInput(label="ชื่อรางวัล", max_length=60)
    emoji = discord.ui.TextInput(label="อีโมจิ", required=False, max_length=8)
    cost = discord.ui.TextInput(label="ราคา (เหรียญ)", max_length=6)

    def __init__(self, view: RewardEditView, key: str) -> None:
        super().__init__()
        self.view = view
        self.key = key
        item = coins.reward(view.parent.cog.cfg, key) or {}
        self.name.default = item.get("name")
        self.emoji.default = item.get("emoji") or None
        self.cost.default = str(item.get("cost", 0))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            cost = int(str(self.cost.value).replace(",", "").strip())
        except ValueError:
            cost = 0
        if not 1 <= cost <= 100_000:
            await interaction.response.send_message("⚠️ ราคาต้องเป็นตัวเลข 1–100,000 ค่ะ", ephemeral=True)
            return
        cfg = self.view.parent.cog.cfg
        item = _editable_reward(cfg, self.key)
        old = f"{item['name']} {item['cost']}"
        item.update(name=str(self.name.value).strip(), cost=cost)
        if str(self.emoji.value).strip():
            item["emoji"] = str(self.emoji.value).strip()
        cfg.save()
        await self.view.parent.cog._notify_admin(
            f"🎁 {interaction.user.mention} แก้รางวัล {old} → **{item['name']}** {cost} เหรียญ"
        )
        view = RewardEditView(self.view.parent)
        await interaction.response.edit_message(embed=view.embed(), view=view)


# ======================================================================== cog
class CoinsCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.cfg = bot.cfg
        self.db = bot.db
        self._sheet_tasks: set[asyncio.Task] = set()

    # -------------------------------------------------------- helpers
    async def _notify_admin(self, text: str) -> None:
        payments = self.bot.get_cog("PaymentsCog")
        if payments is not None:
            await payments.notify_admin_text(text)

    async def _change(self, user_id: int, delta: int, kind: str, reason: str, *, ref: str | None = None, by: int | None = None) -> int:
        new_balance = await coins.add(self.db, user_id, delta, kind, reason, ref=ref, by=by)
        # ลงชีตแบบเบื้องหลัง — ผู้เรียกบางจุด (ฟอร์มแอดมิน) ต้องตอบ Discord ภายใน 3 วินาที
        task = asyncio.create_task(self._log_sheet(user_id, delta, kind, reason, new_balance))
        self._sheet_tasks.add(task)
        task.add_done_callback(self._sheet_tasks.discard)
        if delta > 0 and kind in coins.LIFETIME_KINDS:
            await self._check_collector(user_id)
        return new_balance

    async def _log_sheet(self, user_id: int, delta: int, kind: str, reason: str, new_balance: int) -> None:
        try:
            guild = self.bot.get_guild(self.cfg.guild_id)
            await self.bot.sheets.append_coin_row([
                dt.datetime.now(self.cfg.tz).strftime("%d/%m/%Y %H:%M"),
                await display_name(self.bot, guild, user_id),
                str(user_id),
                delta,
                kind,
                reason,
                new_balance,
            ])
        except Exception:  # noqa: BLE001 - การลงชีตพลาดต้องไม่กระทบเหรียญ
            log.exception("ลงชีตเหรียญไม่สำเร็จ")

    async def _check_collector(self, user_id: int) -> None:
        """สะสมตลอดชีพครบ collector_lifetime → ให้ Role นักสะสม (ครั้งเดียว)"""
        need = int(coins.opt(self.cfg, "collector_lifetime"))
        if await coins.lifetime(self.db, user_id) < need:
            return
        key = f"coins:collector:{user_id}"
        if await self.db.get_meta(key):
            return
        await self.db.set_meta(key, to_iso(now_utc()))
        role_id = int(coins.opt(self.cfg, "collector_role_id") or 0)
        guild = self.bot.get_guild(self.cfg.guild_id)
        member = guild.get_member(user_id) if guild else None
        role = guild.get_role(role_id) if guild and role_id else None
        if member and role:
            try:
                await member.add_roles(role, reason="สะสมเหรียญ Pandora ครบ")
            except discord.HTTPException as exc:
                log.warning("ให้ Role นักสะสมไม่สำเร็จ: %s", exc)
        await send_dm(
            self.bot,
            user_id,
            embed=discord.Embed(
                title="💎 คุณคือ Pandora Collector!",
                description=f"สะสม {coins.label(self.cfg)} ครบ {need:,} เหรียญแล้ว ขอบคุณที่อยู่กับเรานะคะ 💜",
                color=COLOR_GOLD,
            ),
        )
        await self._notify_admin(
            f"💎 <@{user_id}> สะสมเหรียญครบ {need:,} — " + ("ให้ Role แล้ว" if role else "ตั้ง `coins.collector_role_id` เพื่อให้ Role อัตโนมัติ")
        )

    # --------------------------------------------------- ได้/ดึงคืนเหรียญ
    async def on_job_paid(self, job: dict) -> None:
        if not coins.enabled(self.cfg):
            return
        uid = job["customer_id"]
        earned = coins.bill_coins(self.cfg, job)
        lines = []
        if earned:
            kind = "โดเนท" if job["job_type"] == "DONATE" else "บิล"
            mult = float(coins.opt(self.cfg, "event_multiplier"))
            note = f" (อีเวนต์ ×{mult:g})" if mult > 1 and job["job_type"] != "DONATE" else ""
            await self._change(uid, earned, "EARN", f"{kind} #{job['id']}{note}", ref=f"job:{job['id']}")
            lines.append(f"+{earned} จาก{kind} `#{job['id']}`{note}")

        # โบนัสมาครั้งแรก: บิลชำระแล้วใบแรกของลูกค้า (ไม่นับโดเนท / บิลที่ฟรีทั้งใบจากคูปอง)
        if job["job_type"] != "DONATE" and float(job.get("total_price") or 0) > 0:
            bonus = int(coins.opt(self.cfg, "first_visit_bonus"))
            prior = await self.db.fetchone(
                "SELECT COUNT(*) AS n FROM jobs WHERE customer_id = ? AND id != ? AND job_type NOT IN ('DONATE', 'BONUS') "
                "AND status IN ('PAID','COMPLETED')",
                (uid, job["id"]),
            )
            given = await self.db.fetchone("SELECT 1 FROM coin_ledger WHERE user_id = ? AND ref = 'first_visit'", (uid,))
            if bonus and prior and prior["n"] == 0 and not given:
                await self._change(uid, bonus, "EARN", "โบนัสมาครั้งแรก", ref="first_visit")
                lines.append(f"+{bonus} โบนัสมาครั้งแรก 🎉")

        if lines:
            await send_dm(
                self.bot,
                uid,
                embed=discord.Embed(
                    title=f"{coins.opt(self.cfg, 'emoji')} ได้รับ{coins.opt(self.cfg, 'name')}",
                    description="\n".join(lines) + f"\nคงเหลือ **{await coins.balance(self.db, uid):,}** เหรียญ",
                    color=COLOR_GOLD,
                ),
            )

    async def on_job_cancelled(self, job: dict) -> None:
        """บิลถูกยกเลิก: ดึงเหรียญที่ได้จากบิลนี้คืน + คืนคูปองที่ใช้ไป"""
        for row in await coins.earned_for_ref(self.db, f"job:{job['id']}"):
            await self._change(row["user_id"], -int(row["total"]), "REVOKE", f"บิล #{job['id']} ถูกยกเลิก", ref=f"job:{job['id']}")
        if job.get("voucher_id"):
            await self.db.execute(
                "UPDATE coin_vouchers SET status = 'ACTIVE', used_job_id = NULL, used_at = NULL WHERE id = ? AND status = 'USED'",
                (job["voucher_id"],),
            )

    async def on_review_approved(self, review: dict) -> None:
        bonus = int(coins.opt(self.cfg, "review_bonus"))
        if not coins.enabled(self.cfg) or not bonus:
            return
        await self._change(review["customer_id"], bonus, "EARN", "รีวิวผ่านการอนุมัติ", ref=f"review:{review['id']}")

    async def on_top_donate(self, user_id: int, month_label: str) -> None:
        bonus = int(coins.opt(self.cfg, "top_donate_bonus"))
        if not coins.enabled(self.cfg) or not bonus:
            return
        await self._change(user_id, bonus, "EARN", f"ชนะ Top Donate {month_label}", ref=f"topdonate:{month_label}")

    # ------------------------------------------------------- คูปองในบิล
    async def use_voucher(self, voucher_id: int, job_id: int) -> bool:
        """ตัดคูปองเป็น USED เฉพาะเมื่อยังใช้ได้ (ACTIVE และไม่หมดอายุ) — คืน False ถ้ามีคนใช้ไปก่อนแล้ว"""
        now = to_iso(now_utc())
        return (
            await self.db.execute_count(
                "UPDATE coin_vouchers SET status = 'USED', used_job_id = ?, used_at = ? "
                "WHERE id = ? AND status = 'ACTIVE' AND expires_at > ?",
                (job_id, now, voucher_id, now),
            )
            > 0
        )

    # ------------------------------------------------------------ ลูกค้า
    async def my_coins(self, interaction: discord.Interaction) -> None:
        if not coins.enabled(self.cfg):
            await interaction.response.send_message("ระบบเหรียญปิดใช้งานชั่วคราวค่ะ", ephemeral=True)
            return
        uid = interaction.user.id
        bal = await coins.balance(self.db, uid)
        life = await coins.lifetime(self.db, uid)
        embed = discord.Embed(title=f"{coins.label(self.cfg)} ของฉัน", color=COLOR_GOLD)
        embed.add_field(name="คงเหลือ", value=f"**{bal:,}** เหรียญ", inline=True)
        embed.add_field(name="สะสมตลอดชีพ", value=f"{life:,} เหรียญ", inline=True)

        nxt = next((r for r in coins.rewards(self.cfg) if int(r["cost"]) > bal), None)
        if nxt:
            need = int(nxt["cost"]) - bal
            rate = int(coins.opt(self.cfg, "baht_per_coin"))
            embed.add_field(
                name="รางวัลถัดไป",
                value=f"{nxt.get('emoji', '')} {nxt['name']} — อีก **{need}** เหรียญ (ใช้บริการอีกประมาณ {need * rate:,} บาท)",
                inline=False,
            )
        collector = int(coins.opt(self.cfg, "collector_lifetime"))
        if life < collector:
            embed.add_field(name="💎 Pandora Collector", value=f"สะสมตลอดชีพอีก {collector - life:,} เหรียญ", inline=False)

        vouchers = await coins.active_vouchers(self.db, uid)
        if vouchers:
            embed.add_field(
                name="🎟️ คูปองที่ใช้ได้",
                value="\n".join(
                    f"`V{v['id']}` {(coins.reward(self.cfg, v['reward_key']) or {}).get('name', v['reward_key'])} — "
                    f"หมดอายุ {_fmt_date(v['expires_at'], self.cfg.tz)}"
                    for v in vouchers[:10]
                ),
                inline=False,
            )
        history = await self.db.fetchall(
            "SELECT * FROM coin_ledger WHERE user_id = ? ORDER BY id DESC LIMIT 8", (uid,)
        )
        if history:
            embed.add_field(
                name="ล่าสุด",
                value="\n".join(f"`{h['delta']:+d}` {h['reason']}" for h in history)[:1024],
                inline=False,
            )
        embed.set_footer(text=self.rules_text())
        await interaction.response.send_message(embed=embed, ephemeral=True)

    def rules_text(self) -> str:
        return (
            f"ได้ 1 เหรียญทุก {coins.opt(self.cfg, 'baht_per_coin')} บาท · เหรียญซื้อ/แลกเงิน/โอนไม่ได้ · "
            f"ไม่มาใช้บริการ {int(coins.opt(self.cfg, 'expire_inactive_days')) // 30} เดือน เหรียญหมดอายุ"
        )

    async def open_redeem(self, interaction: discord.Interaction) -> None:
        if not coins.enabled(self.cfg):
            await interaction.response.send_message("ระบบเหรียญปิดใช้งานชั่วคราวค่ะ", ephemeral=True)
            return
        if not coins.rewards(self.cfg):
            # Discord ไม่ยอมให้ส่งเมนูเลือกที่ไม่มีตัวเลือก — แจ้งลูกค้าแทน
            await interaction.response.send_message("ตอนนี้ยังไม่มีรางวัลให้แลกค่ะ รอแอดมินเพิ่มรางวัลนะคะ", ephemeral=True)
            return
        bal = await coins.balance(self.db, interaction.user.id)
        lines = [
            f"{r.get('emoji', '')} **{r['name']}** — {r['cost']} เหรียญ" + (" ✅" if bal >= int(r["cost"]) else "")
            for r in coins.rewards(self.cfg)
        ]
        embed = discord.Embed(
            title="🎁 แลกรางวัล",
            description=f"คุณมี **{bal:,}** เหรียญ\n\n" + "\n".join(lines)
            + "\n\n*รางวัลที่ต้องนัดวัน (CEO / Hall of Fame / Host Night) แอดมินจะติดต่อกลับ · ส่วนลดแจ้งแอดมินตอนจอง*",
            color=COLOR_GOLD,
        )
        await interaction.response.send_message(embed=embed, view=RedeemView(self, interaction.user, bal), ephemeral=True)

    async def redeem(self, interaction: discord.Interaction, reward_key: str) -> None:
        item = coins.reward(self.cfg, reward_key)
        uid = interaction.user.id
        if item is None or item.get("retired"):
            await interaction.response.edit_message(content="รางวัลนี้เลิกแลกแล้วค่ะ", embed=None, view=None)
            return
        cost = int(item["cost"])
        if await coins.balance(self.db, uid) < cost:
            await interaction.response.edit_message(content="เหรียญไม่พอค่ะ", embed=None, view=None)
            return

        if not coins.enabled(self.cfg):
            await interaction.response.edit_message(content="ระบบเหรียญปิดใช้งานชั่วคราวค่ะ", embed=None, view=None)
            return
        await interaction.response.defer()
        days = int(item.get("days") or coins.opt(self.cfg, "voucher_days"))
        expires = now_utc() + dt.timedelta(days=days)
        voucher_id = await self.db.execute(
            "INSERT INTO coin_vouchers (user_id, reward_key, cost, status, created_at, expires_at) VALUES (?, ?, ?, 'ACTIVE', ?, ?)",
            (uid, reward_key, cost, to_iso(now_utc()), to_iso(expires)),
        )
        new_balance = await self._change(uid, -cost, "REDEEM", f"แลก {item['name']}", ref=f"voucher:{voucher_id}")

        result = await self._deliver(interaction.user, item, voucher_id, expires)
        await interaction.edit_original_response(
            content=None,
            embed=discord.Embed(
                title=f"🎁 แลก {item['name']} แล้ว",
                description=f"{result}\nคงเหลือ **{new_balance:,}** เหรียญ",
                color=COLOR_OK,
            ),
            view=None,
        )
        await self._notify_admin(f"🎁 {interaction.user.mention} แลก **{item['name']}** ({cost} เหรียญ) · คูปอง `V{voucher_id}`")

    async def _deliver(self, user: discord.abc.User, item: dict, voucher_id: int, expires: dt.datetime) -> str:
        """ส่งมอบรางวัลที่ทำได้ทันที — ที่เหลือเป็นคูปองให้แอดมินใช้ตอนเปิดบิล"""
        kind = item.get("type")
        if kind == "shoutout":
            channel = self.bot.get_channel(self.cfg.channel_id("announce"))
            if channel is not None:
                await channel.send(
                    embed=discord.Embed(
                        description=f"📣 ขอบคุณ {user.mention} ที่สนับสนุน {self.cfg.shop_name} เสมอมานะคะ 💜",
                        color=COLOR_GOLD,
                    )
                )
            await self.use_voucher(voucher_id, 0)
            return "ประกาศขอบคุณในห้องประกาศเรียบร้อยค่ะ"
        if kind == "role":
            guild = self.bot.get_guild(self.cfg.guild_id)
            role = guild.get_role(int(item.get("role_id") or 0)) if guild else None
            member = guild.get_member(user.id) if guild else None
            if role and member:
                try:
                    await member.add_roles(role, reason="แลกเหรียญ Pandora")
                    return f"ได้รับ Role {role.mention} ถึงวันที่ {expires.astimezone(self.cfg.tz):%d/%m/%Y} ค่ะ"
                except discord.HTTPException as exc:
                    log.warning("ให้ Role รางวัลไม่สำเร็จ: %s", exc)
            await self._notify_admin(f"🎨 กรุณาให้ Role รางวัลกับ {user.mention} ด้วยมือ (คูปอง `V{voucher_id}`)")
            return "แจ้งแอดมินให้มอบ Role ให้แล้วค่ะ"
        if kind == "manual":
            channel = self.bot.get_channel(self.cfg.channel_id("announce"))
            if channel is not None and item.get("announce"):
                await channel.send(
                    embed=discord.Embed(description=item["announce"].replace("{user}", user.mention), color=COLOR_GOLD)
                )
            note = f"\n📌 {item['note']}" if item.get("note") else ""
            return (
                f"ได้สิทธิ์ `V{voucher_id}` ใช้ได้ถึง {expires.astimezone(self.cfg.tz):%d/%m/%Y} — "
                f"แอดมินจะติดต่อนัดวันกับคุณค่ะ{note}"
            )
        return f"ได้คูปอง `V{voucher_id}` ใช้ได้ถึง {expires.astimezone(self.cfg.tz):%d/%m/%Y} — แจ้งแอดมินตอนจองได้เลยค่ะ"

    # ------------------------------------------------- ใช้คูปอง (แอดมิน)
    async def mark_voucher_used(self, voucher_id: int) -> str:
        """แอดมินกดว่าใช้คูปองแล้ว — รางวัลที่มี role_id + role_hours (เช่น Host Night) ให้ Role ชั่วคราวตอนนี้"""
        v = await self.db.fetchone("SELECT * FROM coin_vouchers WHERE id = ?", (voucher_id,))
        if v is None:
            return "ไม่พบคูปองนี้"
        if v["status"] != "ACTIVE":
            return "คูปองนี้ถูกใช้/หมดอายุไปแล้ว"
        item = coins.reward(self.cfg, v["reward_key"]) or {}
        if item.get("type") == "role":
            # Role รางวัลให้ไปตั้งแต่ตอนแลก — ต้องปล่อยให้ระบบถอดเองตอนหมดอายุ ถ้ากดใช้ Role จะติดถาวร
            return "คูปอง Role ไม่ต้องกดใช้ — บอทจะถอด Role ให้อัตโนมัติเมื่อหมดอายุค่ะ"
        hours = int(item.get("role_hours") or 0)
        guild = self.bot.get_guild(self.cfg.guild_id)
        role = guild.get_role(int(item.get("role_id") or 0)) if guild and hours else None
        member = guild.get_member(v["user_id"]) if guild else None
        if role and member:
            try:
                await member.add_roles(role, reason=f"ใช้รางวัล {item.get('name')}")
                until = now_utc() + dt.timedelta(hours=hours)
                await self.db.execute(
                    "UPDATE coin_vouchers SET status = 'ROLE', used_at = ?, expires_at = ? WHERE id = ? AND status = 'ACTIVE'",
                    (to_iso(now_utc()), to_iso(until), voucher_id),
                )
                return f"ให้ Role {role.mention} {hours} ชั่วโมงแล้ว"
            except discord.HTTPException as exc:
                log.warning("ให้ Role รางวัลไม่สำเร็จ: %s", exc)
        if not await self.use_voucher(voucher_id, 0):
            return "คูปองนี้ถูกใช้/หมดอายุไปแล้ว"
        return "บันทึกว่าใช้แล้ว"

    # ------------------------------------------------------- การ์ดแกล้ง
    async def open_prank_setup(self, interaction: discord.Interaction, item: dict) -> None:
        if await coins.balance(self.db, interaction.user.id) < int(item["cost"]):
            await interaction.response.edit_message(content="เหรียญไม่พอค่ะ", embed=None, view=None)
            return
        attendance = self.bot.get_cog("AttendanceCog")
        # เฉพาะคนที่ยังอยู่ในร้าน — คนที่ออกงานแล้วรับการ์ดไม่ได้ และไม่นับเป็นเป้าหมายที่เปิดรับ
        today = await attendance.today_prefs(on_duty_only=True) if attendance else {}
        performers = [uid for uid, prefs in today.items() if "prank_ok" in prefs.get("accepts", [])]
        if not performers:
            await interaction.response.edit_message(
                content="ตอนนี้ยังไม่มีพนักงานที่รับการ์ดแกล้งวันนี้ ลองใหม่ตอนร้านเปิดนะคะ (ยังไม่หักเหรียญ)",
                embed=None,
                view=None,
            )
            return
        view = PrankSetupView(self, interaction.user, item, performers, today)
        await interaction.response.edit_message(content=None, embed=view.embed(), view=view)

    async def start_prank(
        self, interaction: discord.Interaction, item: dict, prank: str, target: discord.abc.User,
        performer_id: int, today: dict[int, dict],
    ) -> None:
        uid = interaction.user.id
        cost = int(item["cost"])
        problem = None
        if getattr(target, "bot", False):
            problem = "เลือกบอทเป็นเป้าหมายไม่ได้ค่ะ"
        elif performer_id == uid:
            # กันพนักงานส่งการ์ดให้ตัวเองเพื่อรับโบนัส (เท่ากับแลกเหรียญเป็นเงิน)
            problem = "ส่งการ์ดแกล้งให้ตัวเองไม่ได้ค่ะ — เลือกพนักงานคนอื่นนะคะ"
        elif target.id == performer_id:
            problem = "พนักงานที่ไปแกล้งกับเป้าหมายต้องเป็นคนละคนกันค่ะ"
        elif target.id in today and "prank_ok" not in today[target.id].get("accepts", []) and target.id != uid:
            problem = "พนักงานคนนี้ไม่ได้เปิดรับให้แกล้งวันนี้ค่ะ — เลือกเป้าหมายอื่นนะคะ"
        elif self._is_staff(target) and target.id not in today and target.id != uid:
            problem = "พนักงานคนนี้ยังไม่ได้เข้างาน/ไม่ได้เปิดรับให้แกล้งค่ะ"
        elif await coins.balance(self.db, uid) < cost:
            problem = "เหรียญไม่พอค่ะ"
        if problem:
            await interaction.response.send_message(problem, ephemeral=True)
            return

        await interaction.response.defer()
        expires = now_utc() + dt.timedelta(hours=2)
        voucher_id = await self.db.execute(
            "INSERT INTO coin_vouchers (user_id, reward_key, cost, status, created_at, expires_at) VALUES (?, ?, ?, 'PENDING', ?, ?)",
            (uid, item["key"], cost, to_iso(now_utc()), to_iso(expires)),
        )
        await self.db.set_meta(
            f"prank:{voucher_id}",
            json.dumps({"prank": prank, "target": target.id, "performer": performer_id, "customer": uid}, ensure_ascii=False),
        )
        balance_left = await self._change(uid, -cost, "REDEEM", f"การ์ดแกล้ง: {prank[:60]}", ref=f"voucher:{voucher_id}")

        bonus = float(item.get("staff_bonus", 0))
        embed = discord.Embed(
            title="🃏 มีการ์ดแกล้งมาถึงคุณ!",
            description=(
                f"ลูกค้า <@{uid}> ขอให้คุณไปแกล้ง <@{target.id}>\n"
                f"**ท่าแกล้ง:** {prank}\n\n"
                f"กด **รับ** ถ้าสะดวก (ได้โบนัส {bonus:,.0f} บาท) หรือ **ปฏิเสธ** ได้เลย ลูกค้าจะได้เหรียญคืน\n"
                "*ห้ามทำเกินขอบเขต: ไม่ทำให้อับอายจริง ไม่ใช่เรื่องส่วนตัว ไม่ใช่ 18+*"
            ),
            color=COLOR_GOLD,
        )
        view = discord.ui.View(timeout=None)
        view.add_item(PrankDecisionButton("ok", voucher_id))
        view.add_item(PrankDecisionButton("no", voucher_id))
        sent = await send_dm(self.bot, performer_id, embed=embed, view=view)
        if sent is None:
            await self._settle_prank(voucher_id, accepted=False, reason="ส่ง DM ถึงพนักงานไม่ได้")
            await interaction.edit_original_response(
                content="ส่งการ์ดถึงพนักงานไม่ได้ คืนเหรียญให้แล้วค่ะ", embed=None, view=None
            )
            return
        await interaction.edit_original_response(
            content=None,
            embed=discord.Embed(
                title="🃏 ส่งการ์ดแกล้งแล้ว",
                description=(
                    f"รอ <@{performer_id}> กดรับ — ถ้าปฏิเสธหรือไม่ตอบภายใน 2 ชั่วโมง คืนเหรียญอัตโนมัติ\n"
                    f"คงเหลือ **{balance_left:,}** เหรียญ"
                ),
                color=COLOR_OK,
            ),
            view=None,
        )
        await self._notify_admin(f"🃏 <@{uid}> ส่งการ์ดแกล้งให้ <@{performer_id}> ไปแกล้ง <@{target.id}>: {prank}")

    async def _settle_prank(self, voucher_id: int, *, accepted: bool, reason: str = "") -> dict | None:
        v = await self.db.fetchone("SELECT * FROM coin_vouchers WHERE id = ? AND status = 'PENDING'", (voucher_id,))
        if v is None:
            return None
        # ล็อกการ์ดด้วย UPDATE แบบมีเงื่อนไข — กดรับซ้ำ/กดพร้อมหมดเวลา จะผ่านได้ครั้งเดียว (กันโบนัส/คืนเหรียญซ้ำ)
        new_status = "USED" if accepted else "CANCELLED"
        if not await self.db.execute_count(
            "UPDATE coin_vouchers SET status = ?, used_at = ? WHERE id = ? AND status = 'PENDING'",
            (new_status, to_iso(now_utc()), voucher_id),
        ):
            return None
        info = json.loads(await self.db.get_meta(f"prank:{voucher_id}") or "{}")
        item = coins.reward(self.cfg, v["reward_key"]) or {}
        if accepted:
            bonus = float(item.get("staff_bonus", 0))
            if bonus:
                now = to_iso(now_utc())
                job_id = await self.db.create_job(
                    guild_id=self.cfg.guild_id, job_type="BONUS", customer_id=v["user_id"], staff_id=info["performer"],
                    services=[], note=f"โบนัสการ์ดแกล้ง V{voucher_id}", start_time=now, end_time=now,
                    duration_minutes=0, total_price=0, staff_share=bonus, shop_share=-bonus, status="COMPLETED",
                    created_at=now, accepted_at=now, paid_at=now, notified_start=1, notified_end=1, review_sent=1,
                )
                payments = self.bot.get_cog("PaymentsCog")
                if payments is not None:
                    await payments.log_job_to_sheet(await self.db.get_job(job_id))
        else:
            await self._change(v["user_id"], int(v["cost"]), "REFUND", f"คืนเหรียญการ์ดแกล้ง ({reason})", ref=f"voucher:{voucher_id}")
        return info

    async def decide_prank(self, interaction: discord.Interaction, voucher_id: int, accepted: bool) -> None:
        info = json.loads(await self.db.get_meta(f"prank:{voucher_id}") or "{}")
        if interaction.user.id != info.get("performer"):
            await interaction.response.send_message("ปุ่มนี้สำหรับพนักงานที่ได้รับการ์ดค่ะ", ephemeral=True)
            return
        await interaction.response.defer()
        info = await self._settle_prank(voucher_id, accepted=accepted, reason="พนักงานไม่สะดวก")
        if info is None:
            await interaction.edit_original_response(content="การ์ดนี้ถูกดำเนินการไปแล้วค่ะ", embed=None, view=None)
            return
        if accepted:
            text = f"✅ รับการ์ดแกล้งแล้ว — ไปแกล้ง <@{info['target']}> ได้เลย: {info['prank']}"
            customer_msg = f"🃏 <@{info['performer']}> รับการ์ดแกล้งแล้ว! เตรียมดูได้เลย 😆"
        else:
            text = "❌ ปฏิเสธการ์ดแล้ว — คืนเหรียญให้ลูกค้าเรียบร้อย"
            customer_msg = "🃏 พนักงานไม่สะดวกรับการ์ดแกล้งนี้ คืนเหรียญให้แล้วนะคะ"
        await interaction.edit_original_response(content=text, embed=None, view=None)
        await send_dm(self.bot, info["customer"], embed=discord.Embed(description=customer_msg, color=COLOR_GOLD))
        await self._notify_admin(f"🃏 การ์ดแกล้ง `V{voucher_id}`: {'รับแล้ว' if accepted else 'ปฏิเสธ (คืนเหรียญ)'}")

    def _is_staff(self, user: discord.abc.User) -> bool:
        roles = set(self.cfg.staff_role_ids)
        return isinstance(user, discord.Member) and any(r.id in roles for r in user.roles)

    async def show_leaderboard(self, interaction: discord.Interaction) -> None:
        if not coins.enabled(self.cfg):
            await interaction.response.send_message("ระบบเหรียญปิดใช้งานชั่วคราวค่ะ", ephemeral=True)
            return
        rows = await coins.leaderboard(self.db, 10)
        guild = interaction.guild
        medals = ["🥇", "🥈", "🥉"]
        lines = []
        for i, row in enumerate(rows):
            name = await display_name(self.bot, guild, row["user_id"])
            lines.append(f"{medals[i] if i < 3 else f'`#{i + 1}`'} **{name}** — {int(row['total']):,} เหรียญ")
        embed = discord.Embed(
            title=f"🏅 อันดับนักสะสม{coins.opt(self.cfg, 'name')}",
            description="\n".join(lines) or "ยังไม่มีใครสะสมเหรียญค่ะ",
            color=COLOR_GOLD,
        )
        embed.set_footer(text="นับยอดสะสมตลอดชีพ — แลกของแล้วอันดับไม่ลด")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ---------------------------------------------------- ดูแลรายวัน
    async def maintenance(self) -> None:
        """เรียกจากลูป scheduler: คูปองหมดอายุ (ถอด Role) + การ์ดแกล้งค้าง + เหรียญหมดอายุเมื่อไม่มาใช้บริการนาน"""
        now = now_utc()
        guild = self.bot.get_guild(self.cfg.guild_id)

        for v in await self.db.fetchall(
            "SELECT * FROM coin_vouchers WHERE status = 'PENDING' AND expires_at <= ?", (to_iso(now),)
        ):
            info = await self._settle_prank(v["id"], accepted=False, reason="พนักงานไม่ได้ตอบภายใน 2 ชั่วโมง")
            if info:
                await send_dm(
                    self.bot, info["customer"],
                    embed=discord.Embed(description="🃏 การ์ดแกล้งไม่มีพนักงานรับภายในเวลา คืนเหรียญให้แล้วนะคะ", color=COLOR_GOLD),
                )

        expired = await self.db.fetchall(
            "SELECT * FROM coin_vouchers WHERE status IN ('ACTIVE', 'ROLE') AND expires_at <= ?", (to_iso(now),)
        )
        for v in expired:
            new_status = "USED" if v["status"] == "ROLE" else "EXPIRED"
            await self.db.execute("UPDATE coin_vouchers SET status = ? WHERE id = ?", (new_status, v["id"]))
            item = coins.reward(self.cfg, v["reward_key"]) or {}
            if (item.get("type") == "role" or v["status"] == "ROLE") and guild:
                role = guild.get_role(int(item.get("role_id") or 0))
                member = guild.get_member(v["user_id"])
                if role and member and role in member.roles:
                    try:
                        await member.remove_roles(role, reason="Role รางวัลหมดอายุ")
                    except discord.HTTPException as exc:
                        log.warning("ถอด Role รางวัลไม่สำเร็จ: %s", exc)

        if not coins.enabled(self.cfg):
            return
        today = dt.datetime.now(self.cfg.tz).date().isoformat()
        if await self.db.get_meta("coins:expire_day") == today:
            return
        await self.db.set_meta("coins:expire_day", today)
        days = int(coins.opt(self.cfg, "expire_inactive_days"))
        if days <= 0:
            return
        cutoff = to_iso(now - dt.timedelta(days=days))
        rows = await self.db.fetchall(
            "SELECT user_id, SUM(delta) AS bal, MAX(CASE WHEN kind = 'EARN' THEN created_at END) AS last_earn "
            "FROM coin_ledger GROUP BY user_id HAVING SUM(delta) > 0"
        )
        for row in rows:
            if row["last_earn"] and row["last_earn"] < cutoff:
                await self._change(row["user_id"], -int(row["bal"]), "EXPIRE", f"ไม่ได้ใช้บริการเกิน {days} วัน")
                await send_dm(
                    self.bot,
                    row["user_id"],
                    embed=discord.Embed(
                        description=f"⌛ {coins.label(self.cfg)} {int(row['bal']):,} เหรียญของคุณหมดอายุ เพราะไม่ได้ใช้บริการเกิน {days} วันค่ะ",
                        color=COLOR_DANGER,
                    ),
                )

    # ------------------------------------------------------------ แอดมิน
    coins_group = app_commands.Group(name="coins", description="จัดการเหรียญ Pandora (แอดมิน)")

    def _guard(self, interaction: discord.Interaction) -> bool:
        return is_admin(interaction.user, self.cfg.admin_role_id)

    @coins_group.command(name="give", description="เพิ่ม/ลดเหรียญให้สมาชิก (ใส่ติดลบเพื่อลด)")
    @app_commands.describe(member="สมาชิก", amount="จำนวน (ติดลบ = ลด)", reason="เหตุผล")
    async def give(self, interaction: discord.Interaction, member: discord.Member, amount: int, reason: str) -> None:
        if not self._guard(interaction):
            await interaction.response.send_message("เฉพาะแอดมินเท่านั้นค่ะ", ephemeral=True)
            return
        if amount == 0 or abs(amount) > 100_000:
            await interaction.response.send_message("จำนวนต้องไม่เป็น 0 และไม่เกิน 100,000 ค่ะ", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        bal = await self._change(member.id, amount, "ADMIN", reason[:200], by=interaction.user.id)
        await interaction.followup.send(f"✅ {member.mention} {amount:+,} เหรียญ · คงเหลือ {bal:,}", ephemeral=True)
        await self._notify_admin(f"🪙 {interaction.user.mention} ปรับเหรียญ {member.mention} {amount:+,} — {reason[:200]}")

    @coins_group.command(name="check", description="ดูเหรียญและคูปองของสมาชิก")
    async def check(self, interaction: discord.Interaction, member: discord.Member) -> None:
        if not self._guard(interaction):
            await interaction.response.send_message("เฉพาะแอดมินเท่านั้นค่ะ", ephemeral=True)
            return
        bal = await coins.balance(self.db, member.id)
        life = await coins.lifetime(self.db, member.id)
        vouchers = await coins.active_vouchers(self.db, member.id)
        text = "\n".join(
            f"`V{v['id']}` {(coins.reward(self.cfg, v['reward_key']) or {}).get('name', v['reward_key'])}" for v in vouchers
        ) or "-"
        embed = discord.Embed(title=f"🪙 {member.display_name}", color=COLOR_MAIN)
        embed.add_field(name="คงเหลือ", value=f"{bal:,}", inline=True)
        embed.add_field(name="สะสมตลอดชีพ", value=f"{life:,}", inline=True)
        embed.add_field(name="คูปองที่ใช้ได้", value=text, inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @coins_group.command(name="event", description="ตั้งตัวคูณเหรียญวันอีเวนต์ (1 = ปกติ, 2 = ได้ 2 เท่า)")
    async def event(self, interaction: discord.Interaction, multiplier: app_commands.Range[float, 1, 5]) -> None:
        if not self._guard(interaction):
            await interaction.response.send_message("เฉพาะแอดมินเท่านั้นค่ะ", ephemeral=True)
            return
        await interaction.response.send_message(f"✅ {self.event_state(multiplier)}", ephemeral=True)
        await self.set_event(interaction.user, multiplier)

    @staticmethod
    def event_state(multiplier: float) -> str:
        return "ปิดอีเวนต์ (ได้เหรียญปกติ)" if multiplier == 1 else f"เปิดอีเวนต์ ได้เหรียญ ×{multiplier:g}"

    async def set_event(self, user: discord.abc.User, multiplier: float) -> None:
        """ตั้งตัวคูณเหรียญ + แจ้งแอดมิน + ประกาศลูกค้า (เมื่อเปิดอีเวนต์)"""
        self.cfg.data.setdefault("coins", {})["event_multiplier"] = multiplier
        self.cfg.save()
        await self._notify_admin(f"🪙 {user.mention} {self.event_state(multiplier)}")
        channel = self.bot.get_channel(self.cfg.channel_id("announce"))
        if channel is not None and multiplier > 1:
            await channel.send(
                embed=discord.Embed(
                    description=f"🎉 วันนี้ได้ {coins.label(self.cfg)} **×{multiplier:g}** ทุกบิล!",
                    color=COLOR_GOLD,
                )
            )

    async def open_admin_menu(self, interaction: discord.Interaction) -> None:
        if not self._guard(interaction):
            await interaction.response.send_message("เฉพาะแอดมินเท่านั้นค่ะ", ephemeral=True)
            return
        view = CoinAdminView(self)
        await interaction.response.send_message(embed=await view.embed(), view=view, ephemeral=True)

    @app_commands.command(name="my_coins", description="ดูเหรียญ Pandora ของฉัน")
    async def my_coins_command(self, interaction: discord.Interaction) -> None:
        await self.my_coins(interaction)


async def setup(bot: commands.Bot) -> None:
    bot.add_dynamic_items(PrankDecisionButton)
    await bot.add_cog(CoinsCog(bot))
