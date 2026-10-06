"""ระบบชำระเงิน: ส่ง QR ให้ลูกค้า, รับสลิป, ให้แอดมินตรวจสอบ, บันทึกบัญชี"""
from __future__ import annotations

import datetime as dt
import logging
import re

import discord
from discord.ext import commands

from core.cycle import cycle_title
from core.pricing import job_staff_ids, job_staff_split, release_quota_for_job
from core.embeds import (
    COLOR_DANGER,
    COLOR_GOLD,
    COLOR_INFO,
    COLOR_OK,
    dm_embed,
    job_embed,
    payment_embed,
    rows_text,
)
from core.utils import (
    display_name,
    fmt_date,
    fmt_time,
    from_iso,
    is_admin,
    money,
    now_utc,
    send_dm,
    to_iso,
)

log = logging.getLogger("olp.payments")


# --------------------------------------------------------------------- ปุ่ม
class SlipConfirmButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"olp:slip_confirm:(?P<kind>JOB|VIP):(?P<ref>\d+)",
):
    """ปุ่มให้ลูกค้ายืนยันว่าจะส่งสลิปภาพนี้ให้แอดมินตรวจ"""

    def __init__(self, kind: str, ref_id: int) -> None:
        self.kind = kind
        self.ref_id = ref_id
        super().__init__(
            discord.ui.Button(
                label="ยืนยันส่งสลิป",
                emoji="📤",
                style=discord.ButtonStyle.success,
                custom_id=f"olp:slip_confirm:{kind}:{ref_id}",
            )
        )

    @classmethod
    async def from_custom_id(cls, interaction, item, match: re.Match[str]):
        return cls(match["kind"], int(match["ref"]))

    async def callback(self, interaction: discord.Interaction) -> None:  # type: ignore[override]
        cog: PaymentsCog = interaction.client.get_cog("PaymentsCog")  # type: ignore[assignment]
        await cog.submit_slip_to_admin(interaction, self.kind, self.ref_id)


class SlipDecisionButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"olp:slip_(?P<action>ok|no):(?P<kind>JOB|VIP):(?P<ref>\d+)",
):
    """ปุ่มฝั่งแอดมิน: ยืนยันสลิปถูกต้อง / ยกเลิกบิล"""

    def __init__(self, action: str, kind: str, ref_id: int) -> None:
        self.action = action
        self.kind = kind
        self.ref_id = ref_id
        approve = action == "ok"
        reject_label = "ปฏิเสธสลิป" if kind == "JOB" else "ปฏิเสธสลิป VIP"
        super().__init__(
            discord.ui.Button(
                label="ยืนยันสลิปถูกต้อง" if approve else reject_label,
                emoji="✅" if approve else "❌",
                style=discord.ButtonStyle.success if approve else discord.ButtonStyle.danger,
                custom_id=f"olp:slip_{action}:{kind}:{ref_id}",
            )
        )

    @classmethod
    async def from_custom_id(cls, interaction, item, match: re.Match[str]):
        return cls(match["action"], match["kind"], int(match["ref"]))

    async def callback(self, interaction: discord.Interaction) -> None:  # type: ignore[override]
        cog: PaymentsCog = interaction.client.get_cog("PaymentsCog")  # type: ignore[assignment]
        if not is_admin(interaction.user, cog.cfg.admin_role_id):
            await interaction.response.send_message("เฉพาะแอดมินเท่านั้นค่ะ", ephemeral=True)
            return
        if self.action == "ok":
            await cog.approve_slip(interaction, self.kind, self.ref_id)
        else:
            await interaction.response.send_modal(SlipRejectReasonModal(self.kind, self.ref_id))


class SlipRejectReasonModal(discord.ui.Modal, title="ปฏิเสธสลิป"):
    reason = discord.ui.TextInput(
        label="หมายเหตุ (ถ้ามี)",
        placeholder="เช่น โอนไม่ครบยอด / สลิปไม่ใช่ของบิลนี้ / สลิปปลอม",
        style=discord.TextStyle.paragraph,
        required=False,
        max_length=300,
    )

    def __init__(self, kind: str, ref_id: int) -> None:
        super().__init__()
        self.kind = kind
        self.ref_id = ref_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        cog: PaymentsCog = interaction.client.get_cog("PaymentsCog")  # type: ignore[assignment]
        await cog.reject_slip(interaction, self.kind, self.ref_id, str(self.reason).strip())


def slip_view(kind: str, ref_id: int) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(SlipConfirmButton(kind, ref_id))
    return view


def admin_slip_view(kind: str, ref_id: int) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(SlipDecisionButton("ok", kind, ref_id))
    view.add_item(SlipDecisionButton("no", kind, ref_id))
    return view


def _minutes(delta: dt.timedelta) -> int:
    # ใช้ total_seconds — .seconds ไม่นับส่วนวัน (1440 นาทีจะกลายเป็น 0)
    return int(delta.total_seconds() // 60)


# --------------------------------------------------------------------- cog
class PaymentsCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.cfg = bot.cfg
        self.db = bot.db

    # ------------------------------------------------------- เริ่มเก็บเงิน
    async def start_job_payment(self, job: dict) -> bool:
        """ส่งสรุปยอด + QR ให้ลูกค้าใน DM และเปิดสถานะรอสลิป (คืน False ถ้าส่ง DM ไม่ได้)"""
        embed = payment_embed(
            self.cfg,
            title="💳 สรุปยอดชำระเงิน",
            description=(
                f"บิล `#{job['id']}` — {self.cfg.service_names(job['services'])}\n"
                f"พนักงาน: {' '.join(f'<@{s}>' for s in job_staff_ids(job))}"
            ),
            amount=job["total_price"],
        )
        await self.db.set_pending_slip(job["customer_id"], "JOB", job["id"], to_iso(now_utc()))
        sent = await send_dm(self.bot, job["customer_id"], embed=embed)
        if sent is None:
            await self.notify_admin_text(
                f"⚠️ ส่ง DM แจ้งยอดชำระบิล `#{job['id']}` ถึง <@{job['customer_id']}> ไม่สำเร็จ "
                f"(ลูกค้าปิด DM)",
                topic="slip",
            )
        return sent is not None

    async def release_pending_slip(self, customer_id: int, kind: str, ref_id: int) -> None:
        """ปิดช่องรอสลิปของรายการนี้ (ถ้ายังชี้อยู่) แล้วชี้ไปที่รายการอื่นที่ลูกค้ายังค้างจ่าย

        ลูกค้ามีได้ช่องเดียว — ถ้ามีบิลค้างจ่าย 2 ใบ (เช่น บิลหลัก + บิลต่อเวลา) ใบหลังจะทับใบแรก
        พอใบหลังจบ ต้องย้ายช่องกลับไปที่ใบแรก ไม่งั้นสลิปของใบแรกไม่มีทางเข้าระบบ
        """
        if not await self.db.clear_pending_slip(customer_id, kind, ref_id):
            return
        job = await self.db.fetchone(
            "SELECT id, total_price FROM jobs WHERE customer_id = ? AND status = 'ACCEPTED' AND total_price > 0 "
            "ORDER BY id DESC LIMIT 1",
            (customer_id,),
        )
        if job is not None:
            await self.db.set_pending_slip(customer_id, "JOB", job["id"], to_iso(now_utc()))
            await send_dm(
                self.bot,
                customer_id,
                embed=dm_embed(
                    "🧾 ยังมีบิลรอชำระ",
                    [("🧾", "บิล", f"`#{job['id']}`"), ("💰", "ยอด", f"**{money(job['total_price'])}**")],
                    note="ส่งภาพสลิปของบิลนี้ใน DM นี้ได้เลยค่ะ",
                    color=COLOR_INFO,
                ),
            )
            return
        order = await self.db.fetchone(
            "SELECT id FROM vip_orders WHERE customer_id = ? AND status = 'AWAITING_PAYMENT' ORDER BY id DESC LIMIT 1",
            (customer_id,),
        )
        if order is not None:
            await self.db.set_pending_slip(customer_id, "VIP", order["id"], to_iso(now_utc()))

    async def start_vip_payment(self, order: dict) -> None:
        package = self.cfg.vip_package(order["package_key"])
        name = package["name"] if package else order["package_key"]
        detail = f"แพ็กเกจ: **{name}**\nราคาปกติ: {money(order['base_price'])}"
        if order["discount_amount"]:
            detail += f"\nส่วนลด ({order['discount_code']}): -{money(order['discount_amount'])}"

        embed = payment_embed(
            self.cfg,
            title="💎 สรุปยอดชำระ VIP",
            description=f"คำสั่งซื้อ `V#{order['id']}`\n{detail}",
            amount=order["total_price"],
        )
        await self.db.set_pending_slip(order["customer_id"], "VIP", order["id"], to_iso(now_utc()))
        await send_dm(self.bot, order["customer_id"], embed=embed)

    # ------------------------------------------------------------ รับสลิป
    async def receive_slip_image(self, message: discord.Message, pending: dict) -> None:
        """ลูกค้าส่งภาพสลิปเข้ามาใน DM"""
        attachment = next(
            (a for a in message.attachments if (a.content_type or "").startswith("image")), None
        )
        if attachment is None:
            return

        kind, ref_id = pending["kind"], pending["ref_id"]
        if kind == "JOB":
            await self.db.update_job(ref_id, slip_url=attachment.url)
        else:
            await self.db.update_vip_order(ref_id, slip_url=attachment.url)

        embed = dm_embed(
            "📎 ได้รับภาพสลิปแล้ว",
            [("🧾", "รายการ", f"`#{ref_id}`" if kind == "JOB" else f"`V#{ref_id}`")],
            note="ตรวจภาพให้ถูกต้อง แล้วกด **ยืนยันส่งสลิป** · ส่งภาพผิด ส่งภาพใหม่ได้เลยค่ะ",
            color=COLOR_GOLD,
        )
        embed.set_image(url=attachment.url)
        await message.reply(embed=embed, view=slip_view(kind, ref_id))

    async def submit_slip_to_admin(
        self, interaction: discord.Interaction, kind: str, ref_id: int
    ) -> None:
        record = (
            await self.db.get_job(ref_id) if kind == "JOB" else await self.db.get_vip_order(ref_id)
        )
        if record is None:
            await interaction.response.send_message("ไม่พบรายการนี้ในระบบค่ะ", ephemeral=True)
            return
        if not record.get("slip_url"):
            await interaction.response.send_message(
                "ยังไม่พบภาพสลิป กรุณาส่งภาพสลิปเข้ามาใน DM ก่อนค่ะ", ephemeral=True
            )
            return
        if record["status"] not in ("ACCEPTED", "AWAITING_PAYMENT", "SLIP_PENDING"):
            await interaction.response.send_message(
                "รายการนี้ไม่อยู่ในสถานะรอชำระเงินแล้วค่ะ", ephemeral=True
            )
            return

        await interaction.response.defer()

        if kind == "JOB":
            await self.db.update_job(ref_id, status="SLIP_PENDING")
            title = f"🔎 สลิปรอตรวจสอบ · บิล #{ref_id}"
            desc = rows_text([
                ("👤", "ลูกค้า", f"<@{record['customer_id']}>"),
                ("💃", "พนักงาน", " ".join(f"<@{s}>" for s in job_staff_ids(record))),
                ("🛎️", "บริการ", self.cfg.service_names(record["services"])),
                ("💰", "ยอด", f"**{money(record['total_price'])}**"),
            ])
        else:
            await self.db.update_vip_order(ref_id, status="SLIP_PENDING")
            package = self.cfg.vip_package(record["package_key"])
            title = f"🔎 สลิปรอตรวจสอบ · VIP #{ref_id}"
            desc = rows_text([
                ("👤", "ลูกค้า", f"<@{record['customer_id']}>"),
                ("💎", "แพ็กเกจ", package["name"] if package else record["package_key"]),
                ("💰", "ยอด", f"**{money(record['total_price'])}**"),
            ])

        embed = discord.Embed(title=title, description=desc, color=COLOR_GOLD)
        embed.set_image(url=record["slip_url"])
        await self.notify_admin(embed=embed, view=admin_slip_view(kind, ref_id), topic="slip")

        await interaction.edit_original_response(
            embed=dm_embed(
                "📤 ส่งสลิปให้แอดมินแล้ว",
                lead="รอแอดมินตรวจสอบสักครู่นะคะ ⏳",
                note="ผลการตรวจจะแจ้งกลับมาทาง DM นี้ค่ะ",
                color=COLOR_INFO,
            ),
            view=None,
        )

    # -------------------------------------------------------- ตัดสินใจสลิป
    async def approve_slip(self, interaction: discord.Interaction, kind: str, ref_id: int) -> None:
        await interaction.response.defer()
        if kind == "JOB":
            ok, msg = await self.mark_job_paid(ref_id, interaction.user)
        else:
            vip = self.bot.get_cog("VipCog")
            ok, msg = await vip.activate_order(ref_id, interaction.user)

        await self._finish_admin_message(interaction, msg, COLOR_OK if ok else COLOR_DANGER)

    async def reject_slip(
        self, interaction: discord.Interaction, kind: str, ref_id: int, reason: str = ""
    ) -> None:
        await interaction.response.defer()
        if kind == "JOB":
            ok, msg = await self.cancel_job(ref_id, interaction.user, reason or None)
        else:
            order = await self.db.get_vip_order(ref_id)
            if order is None:
                ok, msg = False, "ไม่พบคำสั่งซื้อนี้"
            else:
                cancelled = await self.db.execute_count(
                    "UPDATE vip_orders SET status = 'CANCELLED' WHERE id = ? "
                    "AND status IN ('AWAITING_PAYMENT', 'SLIP_PENDING')",
                    (ref_id,),
                )
                if not cancelled:
                    await self._finish_admin_message(
                        interaction, "คำสั่งซื้อนี้ถูกดำเนินการไปแล้ว (ยืนยัน/ยกเลิกแล้ว)", COLOR_DANGER
                    )
                    return
                await self.release_pending_slip(order["customer_id"], "VIP", ref_id)
                note = dm_embed(
                    "❌ คำสั่งซื้อ VIP ถูกยกเลิก",
                    [("💎", "คำสั่งซื้อ", f"`V#{ref_id}`"), *([("📝", "เหตุผล", reason)] if reason else [])],
                    note="หากมีข้อสงสัย ติดต่อแอดมินผ่าน 💬 สอบถามเจ้าหน้าที่ได้เลยค่ะ",
                    color=COLOR_DANGER,
                )
                await send_dm(self.bot, order["customer_id"], embed=note)
                reason_suffix = f"\nเหตุผล: {reason}" if reason else ""
                ok, msg = True, (
                    f"ยกเลิกคำสั่งซื้อ VIP `#{ref_id}` แล้ว โดย {interaction.user.mention}{reason_suffix}"
                )

        await self._finish_admin_message(interaction, msg, COLOR_OK if ok else COLOR_DANGER)

    async def _finish_admin_message(
        self, interaction: discord.Interaction, text: str, color: int
    ) -> None:
        message = interaction.message
        if message is None:
            await interaction.followup.send(text, ephemeral=True)
            return
        embed = message.embeds[0] if message.embeds else discord.Embed()
        embed.color = color
        embed.add_field(name="📋 ผลการตรวจสอบ", value=text, inline=False)
        await message.edit(embed=embed, view=None)

    # ------------------------------------------------------------ สถานะบิล
    async def mark_job_paid(self, job_id: int, admin: discord.abc.User) -> tuple[bool, str]:
        job = await self.db.get_job(job_id)
        if job is None:
            return False, "ไม่พบบิลนี้ในระบบ"
        if job["status"] in ("PAID", "COMPLETED"):
            return False, "บิลนี้ชำระเงินเรียบร้อยแล้ว"
        if job["status"] == "CANCELLED":
            return False, "บิลนี้ถูกยกเลิกไปแล้ว"

        # เปลี่ยนสถานะแบบมีเงื่อนไข — แอดมิน 2 คนกดพร้อมกัน จะมีแค่คนเดียวที่ผ่าน (กันเหรียญ/Sheets ซ้ำ)
        if not await self.db.claim_job(
            job_id, ["PENDING_STAFF", "ACCEPTED", "SLIP_PENDING"], status="PAID", paid_at=to_iso(now_utc())
        ):
            return False, "บิลนี้ถูกดำเนินการไปแล้ว (ชำระแล้ว/ยกเลิกแล้ว)"
        await self.release_pending_slip(job["customer_id"], "JOB", job_id)
        await self.clear_job_meta(job_id)
        job = await self.db.get_job(job_id)
        coins_cog = self.bot.get_cog("CoinsCog")
        if coins_cog is not None:
            await coins_cog.on_job_paid(job)

        paid_embed = job_embed(self.cfg, job, title="✅ ชำระเงินสำเร็จ — ขอบคุณค่ะ 💜", color=COLOR_OK)
        paid_embed.set_footer(text="บอทจะแจ้งเตือนก่อนถึงเวลาและก่อนหมดเวลาให้อัตโนมัติ")
        await send_dm(self.bot, job["customer_id"], embed=paid_embed)
        donate = self.bot.get_cog("DonateCog")
        if job["job_type"] == "DONATE" and donate is not None:
            await donate.on_donation_paid(job)
        for staff_id, _, share in ([] if job["job_type"] == "DONATE" else job_staff_split(self.cfg, job)):
            await send_dm(
                self.bot,
                staff_id,
                embed=dm_embed(
                    "💰 ลูกค้าชำระเงินแล้ว",
                    [
                        ("🧾", "บิล", f"`#{job_id}`"),
                        ("💵", "ยอดบิล", money(job["total_price"])),
                        ("💜", "ส่วนแบ่งของคุณ", f"**{money(share)}**"),
                    ],
                    color=COLOR_OK,
                ),
            )

        await self.log_job_to_sheet(job)
        return True, f"ยืนยันสลิปแล้ว โดย {admin.mention} — บิล `#{job_id}` สถานะ **PAID**"

    async def cancel_core(self, job: dict, from_statuses: list[str]) -> bool:
        """งานยกเลิกบิลที่ทุกทางต้องทำเหมือนกัน (แอดมินยกเลิก / พนักงานปฏิเสธ / โดเนทหมดเวลา)

        เปลี่ยนสถานะแบบมีเงื่อนไข → ปลดช่องรอสลิป → ดึงเหรียญ/คืนคูปอง → คืนสิทธิ์ฟรี
        → ถอนเวลาต่อออกจากบิลแม่ → ลบตัวกันแจ้งเตือนซ้ำ · คืน False ถ้ามีคนดำเนินการบิลนี้ไปก่อนแล้ว
        (ส่วนการแจ้งเตือนแต่ละทางต่างกัน ให้ผู้เรียกทำเอง)
        """
        job_id = job["id"]
        if not await self.db.claim_job(job_id, from_statuses, status="CANCELLED", cancelled_at=to_iso(now_utc())):
            return False
        await self.release_pending_slip(job["customer_id"], "JOB", job_id)
        coins_cog = self.bot.get_cog("CoinsCog")
        if coins_cog is not None:
            await coins_cog.on_job_cancelled(job)

        # คืนสิทธิ์ฟรีที่เคยล็อกไว้ตอนเปิดบิล (ถ้ามี)
        if job.get("quota_services") and job.get("quota_cycle"):
            await release_quota_for_job(self.db, job["customer_id"], job["quota_services"], job["quota_cycle"])

        # ถ้าเป็นบิลต่อเวลา ให้ถอนเวลาที่ต่อออกจากบิลแม่
        if job["job_type"] == "EXTEND" and job.get("parent_job_id"):
            parent = await self.db.get_job(job["parent_job_id"])
            if parent is not None:
                new_end = from_iso(parent["end_time"]) - dt.timedelta(minutes=job["duration_minutes"])
                await self.db.update_job(
                    parent["id"],
                    end_time=to_iso(new_end),
                    duration_minutes=max(parent["duration_minutes"] - job["duration_minutes"], 0),
                )
        await self.clear_job_meta(job_id)
        return True

    async def clear_job_meta(self, job_id: int) -> None:
        """ลบตัวกันแจ้งเตือนซ้ำของบิลที่จบแล้ว (ไม่ให้ตาราง meta โตขึ้นเรื่อยๆ)"""
        await self.db.execute(
            "DELETE FROM meta WHERE key IN (?, ?, ?, ?)",
            tuple(f"stale:{kind}:{job_id}" for kind in ("staff", "pay", "slip", "slipseen")),
        )

    async def purge_finished_meta(self) -> int:
        """ลบตัวกันแจ้งเตือนของบิล/การ์ดแกล้งที่จบไปแล้ว (เก็บกวาดข้อมูลเก่าที่ค้างจากเวอร์ชันก่อน)"""
        suffix_id = "CAST(substr(key, length(rtrim(key, '0123456789')) + 1) AS INTEGER)"
        removed = await self.db.execute_count(
            f"DELETE FROM meta WHERE key LIKE 'stale:%' AND {suffix_id} IN "
            "(SELECT id FROM jobs WHERE status IN ('PAID', 'COMPLETED', 'CANCELLED'))"
        )
        removed += await self.db.execute_count(
            f"DELETE FROM meta WHERE key LIKE 'prank:%' AND {suffix_id} IN "
            "(SELECT id FROM coin_vouchers WHERE status != 'PENDING')"
        )
        return removed

    async def cancel_job(
        self, job_id: int, admin: discord.abc.User, reason: str | None = None
    ) -> tuple[bool, str]:
        job = await self.db.get_job(job_id)
        if job is None:
            return False, "ไม่พบบิลนี้ในระบบ"
        if job["status"] == "CANCELLED":
            return False, "บิลนี้ถูกยกเลิกไปแล้ว"

        if not await self.cancel_core(job, ["PENDING_STAFF", "ACCEPTED", "SLIP_PENDING", "PAID", "COMPLETED"]):
            return False, "บิลนี้ถูกยกเลิกไปแล้ว"

        note = dm_embed(
            "❌ บิลถูกยกเลิก",
            [("🧾", "บิล", f"`#{job_id}`"), *([("📝", "เหตุผล", reason)] if reason else [])],
            note="แอดมินยกเลิกบิลนี้แล้ว ยอดเงินจะไม่ถูกบันทึกลงบัญชีค่ะ",
            color=COLOR_DANGER,
        )
        await send_dm(self.bot, job["customer_id"], embed=note)
        for staff_id in job_staff_ids(job):
            await send_dm(self.bot, staff_id, embed=note)

        # ยกเลิกบิลแม่ = ยกเลิกบิลต่อเวลาที่ยังไม่จ่ายด้วย (ไม่งั้นลูกค้ายังถูกเก็บเงินค่าต่อเวลา)
        child_notes = []
        if job["job_type"] == "NORMAL":
            children = await self.db.fetchall(
                "SELECT id, status FROM jobs WHERE parent_job_id = ? AND job_type = 'EXTEND' AND status != 'CANCELLED'",
                (job_id,),
            )
            for child in children:
                if child["status"] in ("PAID", "COMPLETED"):
                    child_notes.append(f"⚠️ บิลต่อเวลา `#{child['id']}` ชำระแล้ว — ตรวจสอบ/ยกเลิกแยกเองถ้าต้องคืนเงิน")
                    continue
                await self.cancel_job(child["id"], admin, f"ยกเลิกตามบิลหลัก #{job_id}")
                child_notes.append(f"ยกเลิกบิลต่อเวลา `#{child['id']}` ด้วย")

        if job.get("sheet_logged"):
            sheet_note = (
                f"⚠️ บิลนี้ลง Google Sheets ไปแล้ว — กรุณาลบแถวบิล `#{job_id}` ในชีตด้วยมือ "
                "ไม่งั้นยอดในชีตจะไม่ตรงกับสรุปของบอท"
            )
        else:
            sheet_note = "(ไม่บันทึกลง Google Sheets)"
        reason_suffix = f"\nเหตุผล: {reason}" if reason else ""
        extra = "".join(f"\n{n}" for n in child_notes)
        return True, f"ยกเลิกบิล `#{job_id}` แล้ว โดย {admin.mention} {sheet_note}{reason_suffix}{extra}"

    async def reject_job_by_staff(self, job: dict, staff: discord.abc.User, reason: str) -> bool:
        """พนักงานปฏิเสธงานเพราะแอดมินคีย์บิลผิด — ยกเลิกบิลเงียบๆ (ลูกค้ายังไม่เคยรู้เรื่องบิลนี้)"""
        if not await self.cancel_core(job, ["PENDING_STAFF"]):
            return False

        for sid in job_staff_ids(job):
            if sid != staff.id:
                await send_dm(
                    self.bot,
                    sid,
                    embed=dm_embed(
                        "❌ บิลถูกยกเลิก",
                        [("🧾", "บิล", f"`#{job['id']}`")],
                        note="มีพนักงานในทีมไม่สะดวกรับงานนี้ แอดมินจะเปิดบิลใหม่ให้ถ้าจำเป็นค่ะ",
                        color=COLOR_DANGER,
                    ),
                )

        await self.notify_admin(
            embed=dm_embed(
                "❌ พนักงานปฏิเสธงาน",
                [
                    ("🧾", "บิล", f"`#{job['id']}`"),
                    ("💃", "ปฏิเสธโดย", staff.mention),
                    *([("📝", "เหตุผล", reason)] if reason else []),
                    ("👤", "ลูกค้า", f"<@{job['customer_id']}>"),
                    ("🛎️", "บริการ", self.cfg.service_names(job["services"])),
                ],
                note="ตรวจสอบ แล้วคีย์บิลใหม่ให้ถูกต้อง หรือเปลี่ยนพนักงานค่ะ",
                color=COLOR_DANGER,
            )
        )
        return True

    # ----------------------------------------------------- Google Sheets
    async def log_job_to_sheet(self, job: dict) -> None:
        if not self.bot.sheets.ready:
            return
        if job.get("sheet_logged"):
            return

        tz = self.cfg.tz
        guild = self.bot.get_guild(job["guild_id"])
        start, end = from_iso(job["start_time"]), from_iso(job["end_time"])
        customer_name = await display_name(self.bot, guild, job["customer_id"])
        split = job_staff_split(self.cfg, job)
        group_note = f"ทีม {len(split)} คน · " if len(split) > 1 else ""
        if job.get("co_customers"):
            group_note += f"ลูกค้า {1 + len(job['co_customers'])} คน · "
        if job.get("coin_cost"):
            group_note += f"🪙 คูปอง -{job['coin_cost']:,.0f} (ร้านออก) · "

        # บิลที่มีพนักงานหลายคน (Party Room) แยกเป็นแถวละคน เพื่อให้สูตรสรุปรายพนักงานในชีตถูกต้อง
        rows = []
        for staff_id, gross, share in split:
            rows.append([
                job["id"],
                fmt_date(start, tz),
                fmt_time(start, tz),
                fmt_time(end, tz),
                customer_name,
                f"'{job['customer_id']}",
                await display_name(self.bot, guild, staff_id),
                f"'{staff_id}",
                self.cfg.service_names(job["services"]),
                self.cfg.room_name(job.get("room")),
                gross,
                share,
                round(gross - share, 2),
                {"EXTEND": "ต่อเวลา", "DONATE": "โดเนท", "BONUS": "โบนัส"}.get(job["job_type"], "ปกติ"),
                self.cfg.vip_tier_name(job.get("vip_tier")) if job.get("vip_tier") else "ลูกค้าทั่วไป",
                group_note + (job.get("note") or ""),
            ])
        # ลงแท็บของรอบที่ชำระเงินจริง (บิลที่เติมย้อนหลังจะไม่ไปปนรอบปัจจุบัน)
        paid = from_iso(job.get("paid_at"))
        title = cycle_title(self.cfg, paid.astimezone(tz)) if paid else cycle_title(self.cfg)
        # จำว่าลงไปแล้วกี่แถว — ถ้าล้มกลางทาง รอบหน้าลงต่อจากแถวที่ค้าง ไม่ลงซ้ำแถวที่สำเร็จแล้ว
        progress_key = f"sheet_rows:{job['id']}"
        done = int(await self.db.get_meta(progress_key) or 0)
        for index, row in enumerate(rows):
            if index < done:
                continue
            if not await self.bot.sheets.append_job_row(title, row):
                await self.db.set_meta(progress_key, str(done))
                return
            done += 1
        await self.db.update_job(job["id"], sheet_logged=1)
        await self.db.execute("DELETE FROM meta WHERE key = ?", (progress_key,))

    async def backfill_sheet(self) -> int:
        """ลงชีตให้บิลที่ชำระแล้วแต่ยังไม่เคยลง (เช่น ตอน Sheets ยังไม่มีสิทธิ์เขียน) — คืนจำนวนบิลที่ลงได้"""
        if not self.bot.sheets.ready:
            return 0
        done = 0
        for job in await self.db.jobs_by_status(["PAID", "COMPLETED"]):
            if job.get("sheet_logged"):
                continue
            await self.log_job_to_sheet(job)
            if (await self.db.get_job(job["id"]) or {}).get("sheet_logged"):
                done += 1
        if done:
            log.info("เติมบิลที่ตกหล่นลง Google Sheets %d ใบ", done)
        return done

    # -------------------------------------------------- บิลค้าง (เรียกจากลูป)
    def _bill_minutes(self, key: str, default: int) -> int:
        return int(self.cfg.get(f"bill_timeout.{key}", default))

    async def _once(self, key: str) -> bool:
        """คืน True ครั้งแรกที่เจอ key นี้ (กันแจ้งเตือนซ้ำทุกรอบลูป)"""
        if await self.db.get_meta(key):
            return False
        await self.db.set_meta(key, to_iso(now_utc()))
        return True

    async def check_stale_bills(self) -> None:
        """จัดการบิลปกติ/ต่อเวลาที่ค้าง (โดเนทมีระบบหมดอายุของตัวเองใน DonateCog)

        - รอพนักงานรับงานนาน → แจ้งแอดมิน 1 ครั้ง
        - ลูกค้ายังไม่ส่งสลิป → DM เตือน 1 ครั้ง แล้วยกเลิกอัตโนมัติเมื่อเลยกำหนด
          (กำหนด = รับงาน + payment_cancel_minutes แต่ไม่ก่อนเวลาเริ่มงาน เผื่อจองล่วงหน้า)
        - สลิปรอแอดมินตรวจนาน → แจ้งแอดมิน 1 ครั้ง
        """
        now = now_utc()
        staff_wait = dt.timedelta(minutes=self._bill_minutes("staff_accept_minutes", 15))
        pay_remind = dt.timedelta(minutes=self._bill_minutes("payment_remind_minutes", 15))
        pay_cancel = dt.timedelta(minutes=self._bill_minutes("payment_cancel_minutes", 45))
        slip_wait = dt.timedelta(minutes=self._bill_minutes("slip_review_minutes", 10))

        for job in await self.db.jobs_by_status(["PENDING_STAFF", "ACCEPTED", "SLIP_PENDING"]):
            if job["job_type"] == "DONATE":
                continue
            jid = job["id"]

            if job["status"] == "PENDING_STAFF":
                if now - from_iso(job["created_at"]) >= staff_wait and await self._once(f"stale:staff:{jid}"):
                    waiting = [s for s in job_staff_ids(job) if s not in (job.get("accepted_by") or [])]
                    await self.notify_admin_text(
                        f"⏳ บิล `#{jid}` รอ {' '.join(f'<@{s}>' for s in waiting)} กดรับงานมาเกิน {_minutes(staff_wait)} นาทีแล้ว "
                        "— ทักพนักงาน หรือยกเลิกแล้วเปิดบิลใหม่ด้วย `/bill cancel`"
                    )

            elif job["status"] == "ACCEPTED" and not job.get("slip_url"):
                accepted = from_iso(job["accepted_at"] or job["created_at"])
                deadline = max(accepted + pay_cancel, from_iso(job["start_time"]))
                if now >= deadline:
                    ok, _ = await self.cancel_job(
                        jid, self.bot.user, f"ไม่ได้ชำระเงินภายในเวลาที่กำหนด ({_minutes(pay_cancel)} นาที)"
                    )
                    if ok:
                        await self.notify_admin_text(
                            f"⌛ ยกเลิกบิล `#{jid}` อัตโนมัติ — <@{job['customer_id']}> ไม่ได้ส่งสลิปตามกำหนด"
                        )
                elif now - accepted >= pay_remind and await self._once(f"stale:pay:{jid}"):
                    left = int((deadline - now).total_seconds() // 60)
                    await send_dm(
                        self.bot,
                        job["customer_id"],
                        embed=dm_embed(
                            "💳 อย่าลืมชำระเงินนะคะ",
                            [
                                ("🧾", "บิล", f"`#{jid}`"),
                                ("💰", "ยอด", f"**{money(job['total_price'])}**"),
                                ("⏳", "เหลือเวลา", f"**{max(left, 1)} นาที**"),
                            ],
                            note="ส่งภาพสลิปใน DM นี้ได้เลยค่ะ — เลยเวลาแล้วระบบจะยกเลิกบิลให้อัตโนมัติ",
                            color=COLOR_GOLD,
                        ),
                    )

            elif job["status"] == "SLIP_PENDING":
                seen_key = f"stale:slipseen:{jid}"
                first_seen = await self.db.get_meta(seen_key)
                if first_seen is None:
                    await self.db.set_meta(seen_key, to_iso(now))
                elif now - from_iso(first_seen) >= slip_wait and await self._once(f"stale:slip:{jid}"):
                    await self.notify_admin_text(
                        f"🔎 สลิปบิล `#{jid}` รอแอดมินตรวจมาเกิน {_minutes(slip_wait)} นาทีแล้ว "
                        f"(ลูกค้า <@{job['customer_id']}> ยอด {money(job['total_price'])})",
                        topic="slip",
                    )

    # -------------------------------------------------------------- utils
    async def notify_admin(
        self,
        *,
        content: str | None = None,
        embed: discord.Embed | None = None,
        view: discord.ui.View | None = None,
        topic: str | None = None,
    ) -> discord.Message | None:
        """ส่งเข้าห้องแอดมิน — topic = ห้องแยกตามเรื่อง (ticket / slip / attendance) ถ้าตั้งไว้ ไม่ตั้ง = ห้องแอดมิน"""
        channel = self.bot.get_channel(self.cfg.channel_id(topic)) if topic else None
        if channel is None:
            channel = self.bot.get_channel(self.cfg.channel_id("admin"))
        if channel is None:
            log.warning("ไม่พบห้องแอดมิน (channels.admin) ใน config")
            return None
        kwargs: dict = {}
        if content is not None:
            kwargs["content"] = content
        if embed is not None:
            kwargs["embed"] = embed
        if view is not None:
            kwargs["view"] = view
        return await channel.send(**kwargs)

    async def notify_admin_text(self, text: str, *, topic: str | None = None) -> None:
        await self.notify_admin(content=text, topic=topic)


async def setup(bot: commands.Bot) -> None:
    bot.add_dynamic_items(SlipConfirmButton, SlipDecisionButton)
    await bot.add_cog(PaymentsCog(bot))
