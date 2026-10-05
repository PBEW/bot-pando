"""เหรียญ Pandora: ได้เหรียญจากบิล/รีวิว/Top Donate, แลกรางวัลเป็นคูปอง, อันดับนักสะสม, หมดอายุ"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging

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
        embed = discord.Embed(title=f"🪙 จัดการ{coins.opt(cfg, 'name')}", color=COLOR_GOLD)
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
        await self.cog.use_voucher(self.voucher_id, 0)
        await self.cog._notify_admin(
            f"🎟️ {interaction.user.mention} บันทึกว่า {self.member.mention} ใช้คูปอง `V{self.voucher_id}` แล้ว"
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
    def __init__(self, parent: CoinAdminView) -> None:
        super().__init__(timeout=600)
        self.parent = parent
        cfg = parent.cog.cfg
        self.select = discord.ui.Select(
            placeholder="เลือกรางวัลที่จะแก้",
            options=[
                discord.SelectOption(label=f"{r['name']} — {r['cost']} เหรียญ"[:100], value=r["key"], emoji=r.get("emoji") or None)
                for r in coins.rewards(cfg)[:25]
            ],
        )
        self.select.callback = self._on_pick
        self.add_item(self.select)

    def embed(self) -> discord.Embed:
        cfg = self.parent.cog.cfg
        return discord.Embed(
            title="🎁 แก้รางวัล",
            description="\n".join(f"{r.get('emoji', '')} **{r['name']}** — {r['cost']} เหรียญ" for r in coins.rewards(cfg))
            + "\n\nเลือกรางวัลเพื่อแก้ชื่อ / อีโมจิ / ราคา",
            color=COLOR_GOLD,
        )

    async def _on_pick(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(RewardModal(self, self.select.values[0]))

    @discord.ui.button(label="กลับ", emoji="⬅️", style=discord.ButtonStyle.secondary, row=1)
    async def back(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.parent.rerender(interaction, self.parent.member)


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
        section = cfg.data.setdefault("coins", {})
        if "rewards" not in section:  # ยังใช้ค่าเริ่มต้นอยู่ → คัดลอกลง config ก่อนแก้
            section["rewards"] = [dict(r) for r in coins.DEFAULT_REWARDS]
        item = next(r for r in section["rewards"] if r["key"] == self.key)
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
                "SELECT COUNT(*) AS n FROM jobs WHERE customer_id = ? AND id != ? AND job_type != 'DONATE' "
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
    async def use_voucher(self, voucher_id: int, job_id: int) -> None:
        await self.db.execute(
            "UPDATE coin_vouchers SET status = 'USED', used_job_id = ?, used_at = ? WHERE id = ?",
            (job_id, to_iso(now_utc()), voucher_id),
        )

    # ------------------------------------------------------------ ลูกค้า
    async def my_coins(self, interaction: discord.Interaction) -> None:
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
        bal = await coins.balance(self.db, interaction.user.id)
        lines = [
            f"{r.get('emoji', '')} **{r['name']}** — {r['cost']} เหรียญ" + (" ✅" if bal >= int(r["cost"]) else "")
            for r in coins.rewards(self.cfg)
        ]
        embed = discord.Embed(
            title="🎁 แลกรางวัล",
            description=f"คุณมี **{bal:,}** เหรียญ\n\n" + "\n".join(lines)
            + f"\n\n*รางวัลบริการจะได้เป็นคูปอง ใช้ได้ {coins.opt(self.cfg, 'voucher_days')} วัน — แจ้งแอดมินตอนจองได้เลย*",
            color=COLOR_GOLD,
        )
        await interaction.response.send_message(embed=embed, view=RedeemView(self, interaction.user, bal), ephemeral=True)

    async def redeem(self, interaction: discord.Interaction, reward_key: str) -> None:
        item = coins.reward(self.cfg, reward_key)
        uid = interaction.user.id
        if item is None:
            await interaction.response.edit_message(content="ไม่พบรางวัลนี้แล้วค่ะ", embed=None, view=None)
            return
        cost = int(item["cost"])
        if await coins.balance(self.db, uid) < cost:
            await interaction.response.edit_message(content="เหรียญไม่พอค่ะ", embed=None, view=None)
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
        return f"ได้คูปอง `V{voucher_id}` ใช้ได้ถึง {expires.astimezone(self.cfg.tz):%d/%m/%Y} — แจ้งแอดมินตอนจองได้เลยค่ะ"

    async def show_leaderboard(self, interaction: discord.Interaction) -> None:
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
        """เรียกจากลูป scheduler: คูปองหมดอายุ (ถอด Role) + เหรียญหมดอายุเมื่อไม่มาใช้บริการนาน (วันละครั้ง)"""
        if not coins.enabled(self.cfg):
            return
        now = now_utc()
        guild = self.bot.get_guild(self.cfg.guild_id)
        expired = await self.db.fetchall(
            "SELECT * FROM coin_vouchers WHERE status = 'ACTIVE' AND expires_at <= ?", (to_iso(now),)
        )
        for v in expired:
            await self.db.execute("UPDATE coin_vouchers SET status = 'EXPIRED' WHERE id = ?", (v["id"],))
            item = coins.reward(self.cfg, v["reward_key"]) or {}
            if item.get("type") == "role" and guild:
                role = guild.get_role(int(item.get("role_id") or 0))
                member = guild.get_member(v["user_id"])
                if role and member and role in member.roles:
                    try:
                        await member.remove_roles(role, reason="Role รางวัลหมดอายุ")
                    except discord.HTTPException as exc:
                        log.warning("ถอด Role รางวัลไม่สำเร็จ: %s", exc)

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
    await bot.add_cog(CoinsCog(bot))
