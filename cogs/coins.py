"""เหรียญ Pandora: ได้เหรียญจากบิล/รีวิว/Top Donate, แลกรางวัลเป็นคูปอง, อันดับนักสะสม, หมดอายุ"""
from __future__ import annotations

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


# ======================================================================== cog
class CoinsCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.cfg = bot.cfg
        self.db = bot.db

    # -------------------------------------------------------- helpers
    async def _notify_admin(self, text: str) -> None:
        payments = self.bot.get_cog("PaymentsCog")
        if payments is not None:
            await payments.notify_admin_text(text)

    async def _change(self, user_id: int, delta: int, kind: str, reason: str, *, ref: str | None = None, by: int | None = None) -> int:
        new_balance = await coins.add(self.db, user_id, delta, kind, reason, ref=ref, by=by)
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
        if delta > 0 and kind in coins.LIFETIME_KINDS:
            await self._check_collector(user_id)
        return new_balance

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
        self.cfg.data.setdefault("coins", {})["event_multiplier"] = multiplier
        self.cfg.save()
        state = "ปิดอีเวนต์ (ได้เหรียญปกติ)" if multiplier == 1 else f"เปิดอีเวนต์ ได้เหรียญ ×{multiplier:g}"
        await interaction.response.send_message(f"✅ {state}", ephemeral=True)
        await self._notify_admin(f"🪙 {interaction.user.mention} {state}")
        channel = self.bot.get_channel(self.cfg.channel_id("announce"))
        if channel is not None and multiplier > 1:
            await channel.send(
                embed=discord.Embed(
                    description=f"🎉 วันนี้ได้ {coins.label(self.cfg)} **×{multiplier:g}** ทุกบิล!",
                    color=COLOR_GOLD,
                )
            )

    @app_commands.command(name="my_coins", description="ดูเหรียญ Pandora ของฉัน")
    async def my_coins_command(self, interaction: discord.Interaction) -> None:
        await self.my_coins(interaction)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(CoinsCog(bot))
