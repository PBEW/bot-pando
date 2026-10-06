"""ระบบโดเนทด้วยตัวเอง: ลูกค้าเลือกพนักงาน → ใส่ยอด/จำนวน shot → รับ QR ทาง DM → ส่งสลิป → แอดมินยืนยัน

บันทึกเป็นบิลประเภท DONATE ในตาราง jobs เดิม จึงใช้ระบบสลิป, ส่วนแบ่งพนักงาน, Google Sheets,
สรุปตัดรอบ และ Top Donate ร่วมกับบิลปกติได้ทันที (ไม่มีแจ้งเตือนเวลา และไม่มีแบบประเมินรีวิว)
"""
from __future__ import annotations

import datetime as dt
import logging

import discord
from discord.ext import commands

from core.embeds import COLOR_DANGER, COLOR_GOLD, COLOR_MAIN
from core.pricing import split_revenue
from core.utils import from_iso, money, now_utc, send_dm, staff_members, to_iso

log = logging.getLogger("olp.donate")

DONATE_KEY = "donate"


class DonateAmountModal(discord.ui.Modal):
    amount = discord.ui.TextInput(label="จำนวน", required=True, max_length=7)
    message = discord.ui.TextInput(
        label="ข้อความถึงพนักงาน (ไม่บังคับ)",
        style=discord.TextStyle.paragraph,
        required=False,
        max_length=300,
    )

    def __init__(self, view: "DonateView") -> None:
        shot = view.kind == "shot"
        super().__init__(title="🥃 Drink Friend" if shot else "💜 โดเนท")
        self.view = view
        if shot:
            svc = view.cfg.service(view.shot_key) or {}
            price = svc.get("pricing", {}).get("normal", 0)
            self.amount.label = f"จำนวน shot ({price:,.0f} บาท/shot)"
            self.amount.default = "1"
        else:
            self.amount.label = f"ยอดโดเนท (บาท) ขั้นต่ำ {view.cog.min_amount:,.0f}"
            self.amount.placeholder = "เช่น 100"

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            value = int(str(self.amount.value).replace(",", "").strip())
        except ValueError:
            value = 0
        await self.view.cog.create_donation(
            interaction, self.view.staff_id, self.view.kind, value, str(self.message.value).strip()
        )


class DonateView(discord.ui.View):
    """แผงเลือกพนักงาน + ประเภทโดเนท (เห็นเฉพาะลูกค้าคนกด)"""

    def __init__(self, cog: "DonateCog", user: discord.abc.User, staff: list[discord.Member]) -> None:
        super().__init__(timeout=300)
        self.cog = cog
        self.cfg = cog.cfg
        self.user = user
        self.staff_id: int | None = None
        self.kind = "money"
        self.shot_key = cog.shot_key

        if 0 < len(staff) <= 25:
            self.staff_select = discord.ui.Select(
                placeholder="💃 เลือกพนักงานที่ต้องการโดเนทให้",
                options=[discord.SelectOption(label=m.display_name[:100], value=str(m.id)) for m in staff],
                row=0,
            )
        else:
            self.staff_select = discord.ui.UserSelect(placeholder="💃 เลือกพนักงานที่ต้องการโดเนทให้", row=0)
        self.staff_select.callback = self._on_staff
        self.add_item(self.staff_select)

        kinds = [discord.SelectOption(label="โดเนทเงิน (ระบุยอดเอง)", value="money", emoji="💜", default=True)]
        if self.cfg.service(self.shot_key):
            kinds.append(discord.SelectOption(label="Drink Friend (ซื้อ shot ให้พนักงาน)", value="shot", emoji="🥃"))
        self.kind_select = discord.ui.Select(options=kinds, row=1)
        self.kind_select.callback = self._on_kind
        self.add_item(self.kind_select)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.user.id

    async def _on_staff(self, interaction: discord.Interaction) -> None:
        value = self.staff_select.values[0]
        self.staff_id = int(value) if isinstance(value, str) else value.id
        await interaction.response.defer()

    async def _on_kind(self, interaction: discord.Interaction) -> None:
        self.kind = self.kind_select.values[0]
        for option in self.kind_select.options:
            option.default = option.value == self.kind
        await interaction.response.defer()

    @discord.ui.button(label="ถัดไป: ใส่ยอด", emoji="➡️", style=discord.ButtonStyle.success, row=2)
    async def next(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not self.staff_id:
            await interaction.response.send_message("เลือกพนักงานก่อนนะคะ", ephemeral=True)
            return
        if self.staff_id == interaction.user.id:
            await interaction.response.send_message("โดเนทให้ตัวเองไม่ได้ค่ะ", ephemeral=True)
            return
        await interaction.response.send_modal(DonateAmountModal(self))


class DonateCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.cfg = bot.cfg
        self.db = bot.db

    @property
    def min_amount(self) -> float:
        return float(self.cfg.get("donate.min_amount", 20))

    @property
    def max_amount(self) -> float:
        return float(self.cfg.get("donate.max_amount", 50000))

    @property
    def shot_key(self) -> str:
        return str(self.cfg.get("donate.shot_service", "drink_friend"))

    @property
    def expire_minutes(self) -> int:
        return int(self.cfg.get("donate.expire_minutes", 60))

    # ----------------------------------------------------------- เปิดแผง
    async def open_donate(self, interaction: discord.Interaction) -> None:
        if not self.cfg.get("donate.enabled", True):
            await interaction.response.send_message("ตอนนี้ปิดรับโดเนทอยู่ค่ะ", ephemeral=True)
            return
        pending = await self._pending_donation(interaction.user.id)
        if pending:
            await interaction.response.send_message(
                f"คุณมีโดเนท `#{pending['id']}` ที่ยังรอชำระอยู่ ({money(pending['total_price'])}) "
                "กรุณาส่งสลิปใน DM ของบอทให้เรียบร้อยก่อนนะคะ "
                f"(ถ้าไม่ชำระภายใน {self.expire_minutes} นาที ระบบจะยกเลิกให้อัตโนมัติ)",
                ephemeral=True,
            )
            return

        staff = [m for m in staff_members(interaction.guild, self.cfg.staff_role_ids) if m.id != interaction.user.id]
        embed = discord.Embed(
            title="💜 โดเนทให้พนักงาน",
            description=(
                "1) เลือกพนักงาน\n2) เลือกประเภท — โดเนทเงิน หรือ Drink Friend\n"
                "3) กด **ถัดไป** ใส่ยอด แล้วรับ QR ชำระเงินทาง DM\n\n"
                "ยอดโดเนทนับรวมใน 🏆 **Top Donate** ของเดือนนี้ด้วยนะคะ"
            ),
            color=COLOR_MAIN,
        )
        await interaction.response.send_message(embed=embed, view=DonateView(self, interaction.user, staff), ephemeral=True)

    async def _pending_donation(self, customer_id: int) -> dict | None:
        for job in await self.db.jobs_by_status(["ACCEPTED", "SLIP_PENDING"]):
            if job["job_type"] == "DONATE" and job["customer_id"] == customer_id:
                return job
        return None

    # --------------------------------------------------------- สร้างบิล
    async def create_donation(
        self, interaction: discord.Interaction, staff_id: int, kind: str, value: int, message: str
    ) -> None:
        guild = interaction.guild
        staff_ids = set(self.cfg.staff_role_ids)
        member = guild.get_member(staff_id) if guild else None
        if member is None or (staff_ids and not staff_ids & {r.id for r in member.roles}):
            await interaction.response.send_message("คนที่เลือกไม่ใช่พนักงานของร้านค่ะ", ephemeral=True)
            return

        if kind == "shot":
            if not 1 <= value <= 50:
                await interaction.response.send_message("จำนวน shot ต้องเป็น 1-50 ค่ะ", ephemeral=True)
                return
            price = float((self.cfg.service(self.shot_key) or {}).get("pricing", {}).get("normal", 0))
            services = [self.shot_key] * value
            total = price * value
        else:
            if not self.min_amount <= value <= self.max_amount:
                await interaction.response.send_message(
                    f"ยอดโดเนทต้องอยู่ระหว่าง {money(self.min_amount)} ถึง {money(self.max_amount)} ค่ะ",
                    ephemeral=True,
                )
                return
            services = [DONATE_KEY]
            total = float(value)

        await interaction.response.defer(ephemeral=True)
        now = now_utc()
        if kind == "shot":
            staff_share, shop_share = split_revenue(self.cfg, staff_id, total, {self.shot_key: total})
        else:
            # โดเนทเงิน: ร้านไม่หัก ให้พนักงานตาม donate.staff_percent (ค่าเริ่มต้น 100%)
            percent = float(self.cfg.get("donate.staff_percent", 100))
            staff_share = round(total * percent / 100.0, 2)
            shop_share = round(total - staff_share, 2)
        job_id = await self.db.create_job(
            guild_id=guild.id,
            job_type="DONATE",
            customer_id=interaction.user.id,
            staff_id=staff_id,
            services=services,
            note=message or None,
            start_time=to_iso(now),
            end_time=to_iso(now),
            duration_minutes=0,
            total_price=total,
            staff_share=staff_share,
            shop_share=shop_share,
            status="ACCEPTED",  # ไม่ต้องรอพนักงานรับงาน ข้ามไปขั้นชำระเงินเลย
            opened_by=interaction.user.id,
            created_at=to_iso(now),
            accepted_at=to_iso(now),
            notified_start=1,
            notified_end=1,
        )
        job = await self.db.get_job(job_id)
        payments = self.bot.get_cog("PaymentsCog")
        sent = await payments.start_job_payment(job)

        if not sent:
            await payments.cancel_job(job_id, self.bot.user, "ส่ง DM หาลูกค้าไม่ได้")
            await interaction.followup.send(
                "ส่ง DM ไม่ได้ค่ะ กรุณาเปิดรับข้อความ DM จากสมาชิกในเซิร์ฟเวอร์ แล้วลองใหม่นะคะ", ephemeral=True
            )
            return

        await interaction.followup.send(
            f"สร้างรายการโดเนท `#{job_id}` ให้ {member.mention} ยอด **{money(total)}** แล้วค่ะ\n"
            "ตรวจสอบ DM ของบอท สแกน QR แล้วส่งภาพสลิปกลับมาได้เลย 💜",
            ephemeral=True,
        )

    async def on_donation_paid(self, job: dict) -> None:
        """เรียกจาก PaymentsCog หลังแอดมินยืนยันสลิปของบิล DONATE"""
        embed = discord.Embed(
            title="💜 มีคนโดเนทให้คุณ!",
            description=(
                f"<@{job['customer_id']}> โดเนท **{self.cfg.service_names(job['services'])}** "
                f"ยอด {money(job['total_price'])}\nส่วนแบ่งของคุณ: **{money(job['staff_share'])}**"
            ),
            color=COLOR_GOLD,
        )
        if job.get("note"):
            embed.add_field(name="ข้อความจากลูกค้า", value=job["note"][:1024], inline=False)
        await send_dm(self.bot, job["staff_id"], embed=embed)

        channel = self.bot.get_channel(self.cfg.channel_id("announce"))
        if channel is not None and self.cfg.get("donate.announce", True):
            await channel.send(
                embed=discord.Embed(
                    description=(
                        f"💜 <@{job['customer_id']}> โดเนทให้ <@{job['staff_id']}> "
                        f"**{money(job['total_price'])}** ({self.cfg.service_names(job['services'])}) ขอบคุณค่ะ!"
                    ),
                    color=COLOR_GOLD,
                ),
                allowed_mentions=discord.AllowedMentions(users=False),
            )

    async def expire_unpaid(self) -> None:
        """ยกเลิกโดเนทที่ลูกค้าไม่ส่งสลิปภายในเวลาที่กำหนด (เรียกจากลูป scheduler)"""
        limit = dt.timedelta(minutes=self.expire_minutes)
        now = now_utc()
        for job in await self.db.jobs_by_status(["ACCEPTED"]):
            if job["job_type"] != "DONATE" or job.get("slip_url"):
                continue
            if now - from_iso(job["created_at"]) < limit:
                continue
            payments = self.bot.get_cog("PaymentsCog")
            if payments is None or not await payments.cancel_core(job, ["ACCEPTED"]):
                continue
            await send_dm(
                self.bot,
                job["customer_id"],
                embed=discord.Embed(
                    title="⌛ ยกเลิกรายการโดเนทแล้ว",
                    description=f"โดเนท `#{job['id']}` ไม่ได้ชำระภายใน {self.expire_minutes} นาที ระบบจึงยกเลิกให้ค่ะ",
                    color=COLOR_DANGER,
                ),
            )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(DonateCog(bot))
