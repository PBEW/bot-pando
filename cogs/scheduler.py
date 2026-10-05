"""ลูปเบื้องหลัง: แจ้งเตือนเวลางาน, ปิด Ticket ค้าง, VIP หมดอายุ, ตัดรอบรายสัปดาห์"""
from __future__ import annotations

import datetime as dt
import logging
from collections import defaultdict

import discord
from discord import app_commands
from discord.ext import commands, tasks

from core.cycle import cycle_start_local, cycle_title, next_cutoff_local
from core.embeds import COLOR_GOLD, COLOR_INFO, COLOR_OK, COLOR_WARN
from core.pricing import job_staff_ids, job_staff_split
from core.utils import (
    discord_ts,
    display_name,
    fmt_datetime,
    from_iso,
    is_admin,
    money,
    now_utc,
    send_dm,
    to_iso,
)

log = logging.getLogger("olp.scheduler")

RUNNING_STATUSES = ["PAID"]  # แจ้งเตือนเวลาเฉพาะบิลที่ชำระเงินแล้วเท่านั้น


class SchedulerCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.cfg = bot.cfg
        self.db = bot.db

    async def cog_load(self) -> None:
        self.tick.change_interval(seconds=self.cfg.loop_seconds)
        self.tick.start()

    async def cog_unload(self) -> None:
        self.tick.cancel()

    # ------------------------------------------------------------- main loop
    @tasks.loop(seconds=35)
    async def tick(self) -> None:
        try:
            await self.check_jobs()
            await self.check_tickets()
            if self.cfg.vip_enabled:
                await self.check_vip_expiry()
            await self.check_cutoff()
            await self.check_top_donate()
            donate = self.bot.get_cog("DonateCog")
            if donate is not None:
                await donate.expire_unpaid()
            await self.bot.get_cog("PaymentsCog").check_stale_bills()
        except Exception:  # noqa: BLE001 - ลูปต้องไม่ตาย
            log.exception("เกิดข้อผิดพลาดใน background loop")

    @tick.before_loop
    async def before_tick(self) -> None:
        await self.bot.wait_until_ready()
        # ตั้งค่ารอบปัจจุบันครั้งแรก (ไม่ส่งสรุปย้อนหลัง)
        if await self.db.get_meta("current_cycle") is None:
            now_local = dt.datetime.now(self.cfg.tz)
            await self.db.set_meta("current_cycle", cycle_title(self.cfg, now_local))
            await self.db.set_meta(
                "last_cutoff", cycle_start_local(now_local, self.cfg).date().isoformat()
            )

    # ------------------------------------------------------- แจ้งเตือนเวลางาน
    async def check_jobs(self) -> None:
        now = now_utc()
        before_start = dt.timedelta(minutes=self.cfg.before_start_minutes)
        before_end = dt.timedelta(minutes=self.cfg.before_end_minutes)

        for job in await self.db.jobs_by_status(RUNNING_STATUSES):
            start, end = from_iso(job["start_time"]), from_iso(job["end_time"])

            if not job["notified_start"] and now >= start - before_start:
                await self.db.update_job(job["id"], notified_start=1)
                if now < end:
                    await self._notify_start(job, start)

            if not job["notified_end"] and now >= end - before_end:
                await self.db.update_job(job["id"], notified_end=1)
                await self._notify_end(job, end)

            if now >= end and not job["review_sent"]:
                await self.db.update_job(job["id"], review_sent=1)
                if job["status"] == "PAID":
                    await self.db.update_job(job["id"], status="COMPLETED")
                    if job["job_type"] == "NORMAL":
                        reviews = self.bot.get_cog("ReviewsCog")
                        await reviews.send_review_invite(job)

    async def _notify_start(self, job: dict, start: dt.datetime) -> None:
        minutes = self.cfg.before_start_minutes
        staff_embed = discord.Embed(
            title=f"⏰ อีก {minutes} นาทีจะถึงเวลางาน",
            description=(
                f"บิล `#{job['id']}` · ลูกค้า <@{job['customer_id']}>\n"
                f"บริการ: {self.cfg.service_names(job['services'])}\n"
                f"ห้อง: {self.cfg.room_name(job.get('room'))}\n"
                f"เริ่ม {discord_ts(start)} — เตรียมตัวได้เลยค่ะ"
            ),
            color=COLOR_WARN,
        )
        for staff_id in job_staff_ids(job):
            await send_dm(self.bot, staff_id, embed=staff_embed)
        await send_dm(
            self.bot,
            job["customer_id"],
            embed=discord.Embed(
                title=f"⏰ อีก {minutes} นาทีจะถึงเวลานัด",
                description=(
                    f"บิล `#{job['id']}` · พนักงาน {' '.join(f'<@{s}>' for s in job_staff_ids(job))}\n"
                    f"ห้อง: {self.cfg.room_name(job.get('room'))}\n"
                    f"เริ่ม {discord_ts(start)} — เตรียมเข้างานได้เลยค่ะ"
                ),
                color=COLOR_WARN,
            ),
        )

    async def _notify_end(self, job: dict, end: dt.datetime) -> None:
        minutes = self.cfg.before_end_minutes
        embed = discord.Embed(
            title=f"⌛ อีก {minutes} นาทีจะหมดเวลา",
            description=(
                f"บิล `#{job['id']}` จะจบเวลา {discord_ts(end)}\n"
                "หากต้องการต่อเวลา แจ้งแอดมินเพื่อเปิดบิลต่อเวลาได้เลยค่ะ"
            ),
            color=COLOR_WARN,
        )
        for staff_id in job_staff_ids(job):
            await send_dm(self.bot, staff_id, embed=embed)
        await send_dm(self.bot, job["customer_id"], embed=embed)

    # ------------------------------------------------------------- Ticket
    async def check_tickets(self) -> None:
        limit = dt.timedelta(minutes=self.cfg.ticket_timeout_minutes)
        now = now_utc()
        tickets = self.bot.get_cog("TicketsCog")

        for ticket in await self.db.tickets_by_status(["OPEN", "ACTIVE"]):
            last = from_iso(ticket["last_activity"])
            if last is None or now - last < limit:
                continue
            reason = (
                f"ไม่มีการสนทนาเกิน {self.cfg.ticket_timeout_minutes} นาที ระบบจึงปิดอัตโนมัติ"
                if ticket["status"] == "ACTIVE"
                else f"ไม่มีแอดมินรับเรื่องภายใน {self.cfg.ticket_timeout_minutes} นาที"
            )
            await tickets.close_ticket(ticket["id"], reason=reason)

    # ---------------------------------------------------------------- VIP
    async def check_vip_expiry(self) -> None:
        now_iso = to_iso(now_utc())
        expired = await self.db.fetchall(
            "SELECT * FROM vip_members WHERE expires_at IS NOT NULL AND expires_at <= ?", (now_iso,)
        )
        if not expired:
            return
        vip = self.bot.get_cog("VipCog")
        for record in expired:
            await vip.expire_pass(record)

    # ------------------------------------------------------------ ตัดรอบ
    async def check_cutoff(self) -> None:
        now_local = dt.datetime.now(self.cfg.tz)
        current_start = cycle_start_local(now_local, self.cfg)
        last = await self.db.get_meta("last_cutoff")
        if last == current_start.date().isoformat():
            return
        await self.run_cutoff(now_local)

    async def run_cutoff(self, now_local: dt.datetime | None = None) -> discord.Embed:
        now_local = now_local or dt.datetime.now(self.cfg.tz)
        current_start = cycle_start_local(now_local, self.cfg)
        previous_start = current_start - dt.timedelta(days=7)

        summary = await self.build_summary(previous_start, current_start)

        new_title = cycle_title(self.cfg, now_local)
        created = await self.bot.sheets.create_cycle_sheet(new_title)
        await self.db.set_meta("current_cycle", new_title)
        await self.db.set_meta("last_cutoff", current_start.date().isoformat())

        summary.add_field(
            name="ชีตรอบใหม่",
            value=f"`{new_title}`" + ("" if created else " *(ยังไม่ได้เปิดใช้ Google Sheets)*"),
            inline=False,
        )
        summary.add_field(
            name="ตัดรอบครั้งถัดไป",
            value=fmt_datetime(next_cutoff_local(now_local, self.cfg), self.cfg.tz),
            inline=False,
        )

        payments = self.bot.get_cog("PaymentsCog")
        await payments.notify_admin(embed=summary)

        attendance = self.bot.get_cog("AttendanceCog")
        if attendance is not None:
            hours_embed = await attendance.build_hours_summary(previous_start, current_start)
            await payments.notify_admin(embed=hours_embed)
        log.info("ตัดรอบเรียบร้อย -> %s", new_title)
        return summary

    async def build_summary(self, start_local: dt.datetime, end_local: dt.datetime) -> discord.Embed:
        jobs = await self.db.jobs_paid_between(to_iso(start_local), to_iso(end_local))
        total_in = sum(j["total_price"] for j in jobs)
        total_out = sum(j["staff_share"] for j in jobs)
        total_shop = sum(j["shop_share"] for j in jobs)

        per_staff: dict[int, list[float]] = defaultdict(lambda: [0.0, 0.0, 0])
        for job in jobs:
            for staff_id, gross, share in job_staff_split(self.cfg, job):
                row = per_staff[staff_id]
                row[0] += gross
                row[1] += share
                row[2] += 1

        embed = discord.Embed(
            title="📊 สรุปยอดรอบบิล",
            description=(
                f"รอบวันที่ **{start_local:%d/%m/%Y}** ถึง **{end_local:%d/%m/%Y}**\n"
                f"จำนวนบิลที่ชำระแล้ว: **{len(jobs)}** ใบ"
            ),
            color=COLOR_OK,
        )
        embed.add_field(name="รายรับรวม (In)", value=money(total_in), inline=True)
        embed.add_field(name="ส่วนแบ่งพนักงาน (Out)", value=money(total_out), inline=True)
        embed.add_field(name="รายได้เข้าร้าน", value=money(total_shop), inline=True)

        guild = self.bot.get_guild(self.cfg.guild_id)
        if per_staff:
            lines = []
            for staff_id, (gross, share, count) in sorted(
                per_staff.items(), key=lambda kv: kv[1][0], reverse=True
            ):
                name = await display_name(self.bot, guild, staff_id)
                lines.append(f"• **{name}** — {count} บิล · In {money(gross)} · แบ่ง {money(share)}")
            embed.add_field(name="แยกตามพนักงาน", value="\n".join(lines)[:1024], inline=False)

        url = await self.bot.sheets.spreadsheet_url()
        if url:
            embed.add_field(name="Google Sheets", value=url, inline=False)
        return embed

    # --------------------------------------------------------- Top Donate
    @staticmethod
    def month_bounds(year: int, month: int, tz) -> tuple[dt.datetime, dt.datetime]:
        start = dt.datetime(year, month, 1, tzinfo=tz)
        end = dt.datetime(year + (month == 12), month % 12 + 1, 1, tzinfo=tz)
        return start, end

    async def donate_ranking(self, year: int, month: int) -> list[dict]:
        """อันดับยอดโดเนทรายลูกค้าของเดือน พร้อมพนักงานที่ลูกค้าคนนั้นโดเนทให้มากที่สุด"""
        start, end = self.month_bounds(year, month, self.cfg.tz)
        rows = await self.db.donation_totals(to_iso(start), to_iso(end))
        if not rows:
            return rows
        jobs = await self.db.jobs_paid_between(to_iso(start), to_iso(end))
        per_pair: dict[tuple[int, int], float] = defaultdict(float)
        for job in jobs:
            for staff_id, gross, _ in job_staff_split(self.cfg, job):
                per_pair[(job["customer_id"], staff_id)] += gross
        for row in rows:
            mine = {sid: amt for (cid, sid), amt in per_pair.items() if cid == row["customer_id"]}
            row["top_staff"] = max(mine, key=mine.get) if mine else None
            row["top_staff_amount"] = mine.get(row["top_staff"], 0.0) if mine else 0.0
        return rows

    async def donate_embed(self, year: int, month: int, *, final: bool) -> discord.Embed:
        rows = await self.donate_ranking(year, month)
        minimum = self.cfg.top_donate_min
        guild = self.bot.get_guild(self.cfg.guild_id)
        title = f"🏆 Top Donate · {month:02d}/{year}"
        embed = discord.Embed(title=title, color=COLOR_GOLD)

        if not rows:
            embed.description = "ยังไม่มียอดโดเนทในเดือนนี้ค่ะ"
            return embed

        medals = ["🥇", "🥈", "🥉"]
        lines = []
        for i, row in enumerate(rows[: self.cfg.top_donate_size]):
            name = await display_name(self.bot, guild, row["customer_id"])
            badge = medals[i] if i < len(medals) else f"`#{i + 1}`"
            lines.append(f"{badge} **{name}** — {money(row['total'])}")
        embed.description = "\n".join(lines)

        top = rows[0]
        if top["total"] >= minimum:
            staff_text = f"<@{top['top_staff']}>" if top.get("top_staff") else "-"
            embed.add_field(
                name="👑 ผู้ชนะประจำเดือน" if final else "👑 อันดับ 1 ตอนนี้",
                value=(
                    f"<@{top['customer_id']}> ยอดรวม **{money(top['total'])}**\n"
                    f"พนักงานที่โดเนทให้มากที่สุด: {staff_text}"
                    + (
                        "\nได้สิทธิ์พาพนักงานคนนี้ไปเดทนอกร้าน (ขอให้ถามความเห็นของพนักงานก่อนนะคะ)"
                        if final
                        else ""
                    )
                ),
                inline=False,
            )
        else:
            embed.add_field(
                name="เงื่อนไข",
                value=f"ผู้ชนะต้องมียอดรวมขั้นต่ำ {money(minimum)} — อันดับ 1 ยังไม่ถึงขั้นต่ำ"
                + (" จึงไม่มีผู้ชนะเดือนนี้" if final else ""),
                inline=False,
            )
        embed.set_footer(text="นับจากยอดบิลที่ชำระแล้วทั้งหมดในเดือนนั้น")
        return embed

    async def check_top_donate(self) -> None:
        """วันที่ 1 ของทุกเดือน ประกาศผู้ชนะ Top Donate ของเดือนก่อน (ครั้งเดียวต่อเดือน)"""
        if not self.cfg.top_donate_enabled:
            return
        now_local = dt.datetime.now(self.cfg.tz)
        this_month = f"{now_local.year:04d}-{now_local.month:02d}"
        last = await self.db.get_meta("last_top_donate")
        if last is None:
            # ครั้งแรกที่รันบอท: เริ่มนับจากเดือนนี้ ไม่ประกาศย้อนหลัง
            await self.db.set_meta("last_top_donate", this_month)
            return
        if last == this_month:
            return
        await self.db.set_meta("last_top_donate", this_month)
        await self.announce_top_donate(now_local)

    async def announce_top_donate(self, now_local: dt.datetime) -> None:
        prev = now_local.replace(day=1) - dt.timedelta(days=1)
        embed = await self.donate_embed(prev.year, prev.month, final=True)
        payments = self.bot.get_cog("PaymentsCog")

        channel = self.bot.get_channel(self.cfg.channel_id("announce"))
        if channel is not None:
            await channel.send(embed=embed)
        await payments.notify_admin(embed=embed)

        rows = await self.donate_ranking(prev.year, prev.month)
        if rows and rows[0]["total"] >= self.cfg.top_donate_min:
            top = rows[0]
            await payments.notify_admin_text(
                f"🏆 ผู้ชนะ Top Donate {prev.month:02d}/{prev.year}: <@{top['customer_id']}> "
                f"({money(top['total'])}) — ติดต่อจัดเดทกับ <@{top['top_staff']}> ได้เลยค่ะ"
            )
            await send_dm(
                self.bot,
                top["customer_id"],
                embed=discord.Embed(
                    title="🏆 ยินดีด้วย! คุณคือ Top Donate ประจำเดือน",
                    description=(
                        f"ยอดรวมเดือน {prev.month:02d}/{prev.year}: **{money(top['total'])}**\n"
                        f"คุณได้สิทธิ์พา <@{top['top_staff']}> ไปเดทนอกร้าน 💜\n"
                        "แอดมินจะติดต่อกลับเพื่อนัดวันเวลา — จะทำอะไรขอให้ถามความเห็นของพนักงานก่อนนะคะ"
                    ),
                    color=COLOR_GOLD,
                ),
            )

    @app_commands.command(name="top_donate", description="ดูอันดับ Top Donate ของเดือนนี้ (หรือเดือนที่ระบุ)")
    @app_commands.describe(month="เดือน (1-12) เว้นว่าง = เดือนนี้", year="ปี ค.ศ. เว้นว่าง = ปีนี้")
    async def top_donate_command(
        self, interaction: discord.Interaction, month: int | None = None, year: int | None = None
    ) -> None:
        now_local = dt.datetime.now(self.cfg.tz)
        month = month or now_local.month
        year = year or now_local.year
        if not 1 <= month <= 12:
            await interaction.response.send_message("เดือนต้องอยู่ระหว่าง 1-12 ค่ะ", ephemeral=True)
            return
        final = (year, month) < (now_local.year, now_local.month)
        await interaction.response.defer(ephemeral=True)
        await interaction.followup.send(embed=await self.donate_embed(year, month, final=final), ephemeral=True)

    # ---------------------------------------------------------- คำสั่ง
    @app_commands.command(name="cutoff", description="ตัดรอบบัญชีทันที (แอดมิน)")
    async def cutoff_command(self, interaction: discord.Interaction) -> None:
        if not is_admin(interaction.user, self.cfg.admin_role_id):
            await interaction.response.send_message("เฉพาะแอดมินเท่านั้นค่ะ", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        await self.run_cutoff()
        await interaction.followup.send("ตัดรอบเรียบร้อย ส่งสรุปเข้าห้องแอดมินแล้วค่ะ", ephemeral=True)

    @app_commands.command(name="summary", description="ดูสรุปยอดของรอบปัจจุบัน (แอดมิน)")
    async def summary_command(self, interaction: discord.Interaction) -> None:
        if not is_admin(interaction.user, self.cfg.admin_role_id):
            await interaction.response.send_message("เฉพาะแอดมินเท่านั้นค่ะ", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        await interaction.followup.send(embed=await self.current_summary(), ephemeral=True)

    async def current_summary(self) -> discord.Embed:
        now_local = dt.datetime.now(self.cfg.tz)
        start = cycle_start_local(now_local, self.cfg)
        embed = await self.build_summary(start, now_local)
        embed.title = "📊 สรุปยอดรอบปัจจุบัน"
        embed.color = COLOR_INFO
        return embed


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(SchedulerCog(bot))
