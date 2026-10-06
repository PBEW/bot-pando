"""ระบบเข้างานพนักงาน: กดเข้างาน/ยกเลิกเข้างาน, Role On Duty, ตัดยอดอัตโนมัติทุกตี 1, สรุปชั่วโมง

ร้านเปิดวันละ 3-4 ชม. จึงไม่มีปุ่มออกงาน — บอทปิดการลงเวลาของทุกคนให้เองตอน day_cutoff_hour (ค่าเริ่มต้นตี 1)
"""
from __future__ import annotations

import datetime as dt
import json
import logging
from collections import defaultdict

import discord
from discord import app_commands
from discord.ext import commands, tasks

from core.cycle import cycle_start_local
from core.embeds import COLOR_DANGER, COLOR_INFO, COLOR_MAIN, COLOR_OK, COLOR_WARN
from core.utils import (
    TimeParseError,
    discord_ts,
    display_name,
    fmt_datetime,
    fmt_time,
    from_iso,
    is_admin,
    now_utc,
    parse_start_time,
    purge_old_panels,
    send_dm,
    to_iso,
)

log = logging.getLogger("olp.attendance")


def fmt_hours(seconds: float) -> str:
    minutes = int(seconds // 60)
    return f"{minutes // 60} ชม. {minutes % 60:02d} นาที"


def parse_past_time(raw: str, tz) -> dt.datetime:
    """อ่านเวลาที่ผ่านมาแล้ว (ใช้ตอนแอดมินแก้เวลา) — เวลาแบบไม่ระบุวันที่อยู่ในอนาคตจะถือเป็นของเมื่อวาน"""
    value = parse_start_time(raw, tz, past=True)
    now = now_utc()
    if value > now + dt.timedelta(minutes=1):
        value -= dt.timedelta(days=1)
    if value > now + dt.timedelta(minutes=1):
        raise TimeParseError("เวลาที่ระบุอยู่ในอนาคต")
    return value


# ------------------------------------------------- งานที่รับวันนี้ (prefs)
DEFAULT_ACCEPT_OPTIONS = [
    {"key": "vip_room", "label": "ห้องบริการ VIP", "emoji": "🔥"},
    {"key": "normal_room", "label": "ห้องปกติ", "emoji": "🚪"},
    {"key": "chill", "label": "เล่นชิวๆ", "emoji": "☕"},
]


def accept_options(cfg) -> list[dict]:
    return list(cfg.get("attendance.accept_options") or DEFAULT_ACCEPT_OPTIONS)


def accept_label(cfg, key: str) -> str:
    opt = next((o for o in accept_options(cfg) if o["key"] == key), None)
    return f"{opt.get('emoji', '')} {opt['label']}".strip() if opt else key


def load_prefs(row: dict | None) -> dict:
    """prefs ของการเข้างาน 1 รายการ: {accepts: [key], avoid_ids: [user_id], avoid_text: str}"""
    raw = (row or {}).get("prefs")
    data = json.loads(raw) if raw else {}
    return {
        "accepts": list(data.get("accepts") or []),
        "avoid_ids": [int(x) for x in data.get("avoid_ids") or []],
        "avoid_text": str(data.get("avoid_text") or ""),
    }


def format_prefs(cfg, prefs: dict, *, private: bool) -> str:
    """ข้อความสรุปงานที่รับ — private=True แสดงรายชื่อคนที่ไม่รับด้วย (เฉพาะแอดมิน/คนกดเอง)"""
    accepts = " · ".join(accept_label(cfg, k) for k in prefs["accepts"]) or "-"
    text = f"รับ: {accepts}"
    if private and (prefs["avoid_ids"] or prefs["avoid_text"]):
        avoid = " ".join(f"<@{uid}>" for uid in prefs["avoid_ids"])
        if prefs["avoid_text"]:
            avoid = f"{avoid} {prefs['avoid_text']}".strip()
        text += f"\n　⛔ ไม่รับ: {avoid}"
    return text


class AvoidNoteModal(discord.ui.Modal, title="ยืนยันเข้างาน"):
    avoid_text = discord.ui.TextInput(
        label="คนที่ไม่เข้าห้องด้วย (ไม่บังคับ)",
        placeholder="พิมพ์ชื่อคนที่ไม่รับวันนี้ หรือหมายเหตุถึงแอดมิน",
        style=discord.TextStyle.paragraph,
        required=False,
        max_length=300,
    )

    def __init__(self, view: "ClockInView") -> None:
        super().__init__()
        self.view = view
        self.avoid_text.default = view.prefs["avoid_text"] or None

    async def on_submit(self, interaction: discord.Interaction) -> None:
        self.view.prefs["avoid_text"] = str(self.avoid_text.value).strip()
        self.view.stop()
        await self.view.cog.save_clock_in(interaction, self.view.prefs)


class ClockInView(discord.ui.View):
    """ฟอร์มตอนกดเข้างาน: เลือกงานที่รับวันนี้ + คนที่ไม่รับเข้าห้อง (เห็นเฉพาะพนักงานคนกด)"""

    def __init__(self, cog: "AttendanceCog", user: discord.abc.User, prefs: dict) -> None:
        super().__init__(timeout=300)
        self.cog = cog
        self.user = user
        self.prefs = prefs

        options = accept_options(cog.cfg)
        selected = set(prefs["accepts"]) or {o["key"] for o in options if o.get("default", True)}
        self.accept_select = discord.ui.Select(
            placeholder="วันนี้รับงานแบบไหนบ้าง (เลือกได้หลายข้อ)",
            min_values=1,
            max_values=len(options),
            row=0,
            options=[
                discord.SelectOption(
                    label=o["label"], value=o["key"], emoji=o.get("emoji"), default=o["key"] in selected
                )
                for o in options
            ],
        )
        self.accept_select.callback = self._on_accept
        self.add_item(self.accept_select)
        self.prefs["accepts"] = [o["key"] for o in options if o["key"] in selected]

        self.avoid_select = discord.ui.UserSelect(
            placeholder="⛔ คนที่ไม่รับเข้าห้องด้วยวันนี้ (ไม่บังคับ)",
            min_values=0,
            max_values=10,
            row=1,
            default_values=[discord.Object(id=uid) for uid in prefs["avoid_ids"]],
        )
        self.avoid_select.callback = self._on_avoid
        self.add_item(self.avoid_select)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.user.id

    async def _on_accept(self, interaction: discord.Interaction) -> None:
        self.prefs["accepts"] = list(self.accept_select.values)
        await interaction.response.defer()

    async def _on_avoid(self, interaction: discord.Interaction) -> None:
        self.prefs["avoid_ids"] = [u.id for u in self.avoid_select.values]
        await interaction.response.defer()

    @discord.ui.button(label="ถัดไป: ยืนยันเข้างาน", emoji="✅", style=discord.ButtonStyle.success, row=2)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_modal(AvoidNoteModal(self))


class AttendancePanel(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(
        label="เข้างาน",
        emoji="🟢",
        style=discord.ButtonStyle.success,
        custom_id="olp:attendance:in",
    )
    async def clock_in(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        cog = interaction.client.get_cog("AttendanceCog")
        await cog.clock_in(interaction)

    @discord.ui.button(
        label="ยกเลิกเข้างาน",
        emoji="↩️",
        style=discord.ButtonStyle.secondary,
        custom_id="olp:attendance:cancel",
    )
    async def cancel_clock_in(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        cog = interaction.client.get_cog("AttendanceCog")
        await cog.cancel_clock_in(interaction)

    @discord.ui.button(
        label="ชั่วโมงของฉัน",
        emoji="🕒",
        style=discord.ButtonStyle.secondary,
        custom_id="olp:attendance:hours",
    )
    async def my_hours(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        cog = interaction.client.get_cog("AttendanceCog")
        await cog.send_my_hours(interaction)


class AttendanceCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.cfg = bot.cfg
        self.db = bot.db

    async def cog_load(self) -> None:
        self.watch_open_shifts.start()

    async def cog_unload(self) -> None:
        self.watch_open_shifts.cancel()

    # -------------------------------------------------------------- สิทธิ์
    def _is_staff(self, member: discord.abc.User) -> bool:
        if is_admin(member, self.cfg.admin_role_id):
            return True
        role_ids = set(self.cfg.staff_role_ids)
        return isinstance(member, discord.Member) and any(r.id in role_ids for r in member.roles)

    async def _deny_if_not_staff(self, interaction: discord.Interaction) -> bool:
        if self._is_staff(interaction.user):
            return False
        if not self.cfg.staff_role_id:
            msg = "ยังไม่ได้ตั้งค่า `roles.staff` ใน config.json แจ้งแอดมินก่อนนะคะ"
        else:
            msg = "ปุ่มนี้สำหรับพนักงานเท่านั้นค่ะ"
        await interaction.response.send_message(msg, ephemeral=True)
        return True

    # ---------------------------------------------------------- Role On Duty
    async def _set_on_duty(self, guild: discord.Guild | None, user_id: int, on: bool) -> None:
        role_id = self.cfg.on_duty_role_id
        if guild is None or not role_id:
            return
        role = guild.get_role(role_id)
        member = guild.get_member(user_id)
        if role is None or member is None:
            return
        try:
            if on and role not in member.roles:
                await member.add_roles(role, reason="เข้างาน")
            elif not on and role in member.roles:
                await member.remove_roles(role, reason="ออกงาน")
        except discord.HTTPException as exc:
            log.warning("ปรับ Role On Duty ของ %s ไม่สำเร็จ: %s", user_id, exc)

    # ------------------------------------------------------------ เข้า/ออก
    async def clock_in(self, interaction: discord.Interaction) -> None:
        if await self._deny_if_not_staff(interaction):
            return
        current = await self.db.open_attendance(interaction.user.id)
        embed = discord.Embed(
            title="✏️ แก้ไขงานที่รับวันนี้" if current else "🟢 เข้างาน — วันนี้รับงานแบบไหน?",
            description=(
                "1) เลือกงานที่รับวันนี้ (เลือกได้หลายข้อ) — 👥 รับลูกค้าหลายคนในห้อง VIP ต้องติ๊กเองเท่านั้น\n"
                "2) เลือกคนที่ไม่รับเข้าห้องด้วย (ไม่บังคับ — แอดมินจะเปิดบิลคู่กับคนนี้ไม่ได้)\n"
                "3) กด **ยืนยันเข้างาน** แล้วพิมพ์ชื่อ/หมายเหตุเพิ่มได้\n\n"
                "*รายชื่อคนที่ไม่รับ เห็นเฉพาะคุณกับแอดมินเท่านั้น*"
            ),
            color=COLOR_MAIN,
        )
        if current:
            embed.set_footer(text="วันนี้คุณเข้างานแล้ว — ยืนยันเพื่ออัปเดตข้อมูล (เวลาเข้างานไม่เปลี่ยน)")
        await interaction.response.send_message(
            embed=embed, view=ClockInView(self, interaction.user, load_prefs(current)), ephemeral=True
        )

    async def save_clock_in(self, interaction: discord.Interaction, prefs: dict) -> None:
        """บันทึกเข้างาน (หรืออัปเดตงานที่รับ ถ้าวันนี้เข้างานแล้ว) — เรียกจาก AvoidNoteModal"""
        user = interaction.user
        prefs_json = json.dumps(prefs, ensure_ascii=False)
        current = await self.db.open_attendance(user.id)
        summary = format_prefs(self.cfg, prefs, private=True)

        if current is not None:
            await self.db.update_attendance(current["id"], prefs=prefs_json)
            await interaction.response.edit_message(
                embed=discord.Embed(title="✏️ อัปเดตงานที่รับวันนี้แล้ว", description=summary, color=COLOR_OK),
                view=None,
            )
            await self._notify_admin(f"✏️ <@{user.id}> แก้ไขงานที่รับวันนี้ · {summary}")
            return

        now = now_utc()
        row_id = await self.db.create_attendance(interaction.guild_id or self.cfg.guild_id, user.id, to_iso(now))
        await self.db.update_attendance(row_id, prefs=prefs_json)
        await interaction.response.edit_message(
            embed=discord.Embed(
                title=f"🟢 บันทึกเข้างานแล้ว {fmt_time(now, self.cfg.tz)} น.",
                description=(
                    f"{summary}\n\n"
                    f"บอทตัดยอดให้อัตโนมัติตอน {self.cutoff_label()} · กดผิด กด ↩️ ยกเลิกเข้างานได้\n"
                    "อยากเปลี่ยนงานที่รับ กด 🟢 เข้างาน อีกครั้งได้เลยค่ะ"
                ),
                color=COLOR_OK,
            ),
            view=None,
        )
        await self._set_on_duty(interaction.guild, user.id, True)
        await self._notify_admin(f"🟢 <@{user.id}> เข้างาน {discord_ts(now)} · {summary}")

    async def today_prefs(self, *, on_duty_only: bool = False) -> dict[int, dict]:
        """{user_id: prefs} ของพนักงานที่เข้างานวันนี้ (ใช้ตอนแอดมินเปิดบิล) — on_duty_only = เฉพาะคนที่ยังไม่ออกงาน"""
        rows = await self.db.fetchall(
            "SELECT * FROM attendance WHERE clock_in >= ?" + (" AND clock_out IS NULL" if on_duty_only else "")
            + " ORDER BY clock_in",
            (to_iso(self.workday_start()),),
        )
        return {row["user_id"]: load_prefs(row) for row in rows}

    async def cancel_clock_in(self, interaction: discord.Interaction) -> None:
        """ยกเลิกการกดเข้างานของวันนี้ (กรณีกดผิด) — ลบรายการทิ้ง ไม่นับชั่วโมง"""
        if await self._deny_if_not_staff(interaction):
            return
        user = interaction.user
        current = await self.db.open_attendance(user.id)
        if current is None:
            await interaction.response.send_message("วันนี้คุณยังไม่ได้กดเข้างานค่ะ", ephemeral=True)
            return

        await self.db.execute("DELETE FROM attendance WHERE id = ?", (current["id"],))
        await interaction.response.send_message("↩️ ยกเลิกการเข้างานแล้วค่ะ", ephemeral=True)
        await self._set_on_duty(interaction.guild, user.id, False)
        await self._notify_admin(f"↩️ <@{user.id}> ยกเลิกเข้างาน (เข้างานเมื่อ {discord_ts(from_iso(current['clock_in']))})")

    # ----------------------------------------------------------- วันทำงาน
    @property
    def cutoff_hour(self) -> int:
        return int(self.cfg.get("attendance.day_cutoff_hour", 1))

    def cutoff_label(self) -> str:
        return f"{self.cutoff_hour:02d}:00 น."

    def workday_start(self, now_local: dt.datetime | None = None) -> dt.datetime:
        """จุดเริ่มวันทำงานปัจจุบัน = เวลาตัดยอด (ตี 1) ครั้งล่าสุดที่ผ่านมา"""
        now_local = now_local or dt.datetime.now(self.cfg.tz)
        start = now_local.replace(hour=self.cutoff_hour, minute=0, second=0, microsecond=0)
        if start > now_local:
            start -= dt.timedelta(days=1)
        return start

    # ------------------------------------------------------------ ชั่วโมง
    async def hours_by_user(
        self, start: dt.datetime, end: dt.datetime, user_id: int | None = None
    ) -> dict[int, list[float]]:
        """{user_id: [วินาทีรวม, จำนวนกะ]} โดยตัดกะให้อยู่ในช่วงเวลา (กะที่ยังเปิดนับถึงตอนนี้)"""
        rows = await self.db.attendance_overlapping(to_iso(start), to_iso(end), user_id)
        now = now_utc()
        result: dict[int, list[float]] = defaultdict(lambda: [0.0, 0])
        for row in rows:
            s = max(from_iso(row["clock_in"]), start)
            e = min(from_iso(row["clock_out"]) or now, end)
            if e > s:
                result[row["user_id"]][0] += (e - s).total_seconds()
                result[row["user_id"]][1] += 1
        return result

    async def build_hours_summary(
        self, start_local: dt.datetime, end_local: dt.datetime, *, title: str = "🕒 สรุปชั่วโมงงานพนักงาน"
    ) -> discord.Embed:
        totals = await self.hours_by_user(start_local, end_local)
        embed = discord.Embed(
            title=title,
            description=f"ช่วง **{fmt_datetime(start_local, self.cfg.tz)}** ถึง **{fmt_datetime(end_local, self.cfg.tz)}**",
            color=COLOR_INFO,
        )
        if not totals:
            embed.add_field(name="ผลรวม", value="ไม่มีบันทึกเข้างานในช่วงนี้", inline=False)
            return embed

        guild = self.bot.get_guild(self.cfg.guild_id)
        lines = []
        for user_id, (seconds, count) in sorted(totals.items(), key=lambda kv: kv[1][0], reverse=True):
            name = await display_name(self.bot, guild, user_id)
            lines.append(f"• **{name}** — {fmt_hours(seconds)} ({int(count)} วัน)")
        embed.add_field(name="แยกตามพนักงาน", value="\n".join(lines)[:1024], inline=False)
        embed.add_field(
            name="รวมทั้งหมด", value=fmt_hours(sum(v[0] for v in totals.values())), inline=False
        )
        return embed

    async def send_my_hours(self, interaction: discord.Interaction) -> None:
        if await self._deny_if_not_staff(interaction):
            return
        now_local = dt.datetime.now(self.cfg.tz)
        start = cycle_start_local(now_local, self.cfg)
        totals = await self.hours_by_user(start, now_local, interaction.user.id)
        seconds, count = totals.get(interaction.user.id, [0.0, 0])

        embed = discord.Embed(title="🕒 ชั่วโมงงานของคุณ (รอบปัจจุบัน)", color=COLOR_MAIN)
        embed.add_field(name="ตั้งแต่", value=fmt_datetime(start, self.cfg.tz), inline=True)
        embed.add_field(name="รวม", value=f"**{fmt_hours(seconds)}** ({int(count)} วัน)", inline=True)
        current = await self.db.open_attendance(interaction.user.id)
        embed.add_field(
            name="สถานะ",
            value=(
                f"🟢 วันนี้เข้างานแล้ว ({discord_ts(from_iso(current['clock_in']))})"
                if current
                else "⚪ วันนี้ยังไม่ได้เข้างาน"
            ),
            inline=False,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ------------------------------------------------- ตัดยอดอัตโนมัติตี 1
    @tasks.loop(minutes=1)
    async def watch_open_shifts(self) -> None:
        try:
            await self._check_open_shifts()
        except Exception:  # noqa: BLE001 - ไม่ให้ลูปตาย
            log.exception("ตัดยอดเข้างานอัตโนมัติไม่สำเร็จ")

    @watch_open_shifts.before_loop
    async def before_watch(self) -> None:
        await self.bot.wait_until_ready()

    async def _check_open_shifts(self) -> None:
        """ปิดการลงเวลาที่เข้างานก่อนเวลาตัดยอดล่าสุด โดยบันทึกเวลาออก = เวลาตัดยอด (ไม่ส่ง DM)"""
        cutoff = self.workday_start()
        guild = self.bot.get_guild(self.cfg.guild_id)
        closed = []
        for row in await self.db.all_open_attendance():
            clock_in = from_iso(row["clock_in"])
            if clock_in >= cutoff:
                continue
            # ปิดที่ "ตี 1 แรกหลังเข้างาน" ไม่ใช่ตี 1 ล่าสุด — ถ้าบอทดับข้ามวัน จะได้ไม่นับชั่วโมงเกินเป็นวันๆ
            close_at = self.workday_start(clock_in.astimezone(self.cfg.tz)) + dt.timedelta(days=1)
            row["close_at"] = close_at
            await self.db.update_attendance(
                row["id"], clock_out=to_iso(close_at), auto_closed=1, note=f"ตัดยอดอัตโนมัติ {self.cutoff_label()}"
            )
            await self._set_on_duty(guild, row["user_id"], False)
            await self._log_to_sheet(await self.db.get_attendance(row["id"]))
            closed.append(row)

        if closed:
            lines = [
                f"• <@{r['user_id']}> — {fmt_hours((r['close_at'] - from_iso(r['clock_in'])).total_seconds())}"
                for r in sorted(closed, key=lambda r: r["clock_in"])
            ]
            await self._notify_admin(
                embed=discord.Embed(
                    title=f"✂️ ตัดยอดเข้างานประจำวัน ({self.cutoff_label()})",
                    description="\n".join(lines)[:4000],
                    color=COLOR_INFO,
                )
            )

    # ------------------------------------------------------------ helpers
    async def _notify_admin(self, content: str | None = None, *, embed: discord.Embed | None = None) -> None:
        payments = self.bot.get_cog("PaymentsCog")
        if payments is not None:
            await payments.notify_admin(content=content, embed=embed)

    async def _log_to_sheet(self, row: dict | None) -> None:
        if row is None or not row["clock_out"]:
            return
        tz = self.cfg.tz
        start, end = from_iso(row["clock_in"]), from_iso(row["clock_out"])
        guild = self.bot.get_guild(self.cfg.guild_id)
        await self.bot.sheets.append_attendance_row(
            [
                row["id"],
                start.astimezone(tz).strftime("%d/%m/%Y"),
                fmt_datetime(start, tz),
                fmt_datetime(end, tz),
                await display_name(self.bot, guild, row["user_id"]),
                f"'{row['user_id']}",
                round((end - start).total_seconds() / 3600, 2),
                row.get("note") or "",
            ]
        )

    # ---------------------------------------------------------- คำสั่ง
    @app_commands.command(name="panel_attendance", description="โพสต์แผงลงเวลาสำหรับพนักงาน (แอดมิน)")
    async def panel_attendance(self, interaction: discord.Interaction) -> None:
        if not is_admin(interaction.user, self.cfg.admin_role_id):
            await interaction.response.send_message("เฉพาะแอดมินเท่านั้นค่ะ", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        removed = await purge_old_panels(interaction.channel, self.bot.user.id, "olp:attendance:")

        embed = discord.Embed(
            title="🕒 Pandora · ลงเวลาทำงาน",
            description=(
                "🟢 **เข้างาน** — กดเมื่อมาทำงาน\n"
                "↩️ **ยกเลิกเข้างาน** — กดผิด กดยกเลิกได้\n"
                "🕒 **ชั่วโมงของฉัน** — ดูชั่วโมงสะสมของรอบนี้\n\n"
                f"*ไม่ต้องกดออกงาน บอทตัดยอดให้อัตโนมัติทุก {self.cutoff_label()}*"
            ),
            color=COLOR_MAIN,
        )
        await interaction.channel.send(embed=embed, view=AttendancePanel())

        note = f" (ลบแผงเก่าออก {removed} อัน)" if removed else ""
        await interaction.followup.send(f"โพสต์แผงเข้างานแล้วค่ะ{note}", ephemeral=True)

    @app_commands.command(name="my_hours", description="ดูชั่วโมงงานของคุณในรอบปัจจุบัน")
    async def my_hours_command(self, interaction: discord.Interaction) -> None:
        await self.send_my_hours(interaction)

    async def today_embed(self, *, private: bool = False) -> discord.Embed:
        """คนที่มาทำงานวันนี้ (กดเข้างานตั้งแต่ตัดยอดครั้งล่าสุด) — private=True แสดงคนที่ไม่รับด้วย (แอดมิน)"""
        start = self.workday_start()
        rows = await self.db.fetchall(
            "SELECT * FROM attendance WHERE clock_in >= ? ORDER BY clock_in", (to_iso(start),)
        )
        if not rows:
            return discord.Embed(description="วันนี้ยังไม่มีพนักงานกดเข้างานค่ะ", color=COLOR_MAIN)
        lines = [
            f"• <@{r['user_id']}> — เข้างาน {discord_ts(from_iso(r['clock_in']))}\n"
            f"　{format_prefs(self.cfg, load_prefs(r), private=private)}"
            for r in rows
        ]
        embed = discord.Embed(
            title=f"🟢 มาทำงานวันนี้ ({len(rows)} คน)", description="\n".join(lines)[:4000], color=COLOR_OK
        )
        embed.set_footer(text=f"นับตั้งแต่ {fmt_datetime(start, self.cfg.tz)} · ตัดยอดทุก {self.cutoff_label()}")
        return embed

    @app_commands.command(name="staff_today", description="ดูว่าวันนี้พนักงานคนไหนมาทำงานบ้าง")
    async def staff_today_command(self, interaction: discord.Interaction) -> None:
        private = is_admin(interaction.user, self.cfg.admin_role_id)
        await interaction.response.send_message(embed=await self.today_embed(private=private), ephemeral=True)

    @app_commands.command(name="attendance_report", description="สรุปชั่วโมงงานพนักงานของรอบปัจจุบัน (แอดมิน)")
    async def attendance_report(self, interaction: discord.Interaction) -> None:
        if not is_admin(interaction.user, self.cfg.admin_role_id):
            await interaction.response.send_message("เฉพาะแอดมินเท่านั้นค่ะ", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        await interaction.followup.send(embed=await self.current_hours_embed(), ephemeral=True)

    async def current_hours_embed(self) -> discord.Embed:
        now_local = dt.datetime.now(self.cfg.tz)
        return await self.build_hours_summary(
            cycle_start_local(now_local, self.cfg), now_local, title="🕒 ชั่วโมงงานรอบปัจจุบัน"
        )

    @app_commands.command(name="attendance_fix", description="แก้เวลาเข้า/ออกงานของกะล่าสุดของพนักงาน (แอดมิน)")
    @app_commands.describe(
        member="พนักงานที่ต้องการแก้",
        clock_in="เวลาเข้างานใหม่ เช่น 18:00 หรือ 05/09 18:00 (เว้นว่าง = ไม่แก้)",
        clock_out="เวลาออกงานใหม่ เช่น 02:30 หรือ 06/09 02:30 (เว้นว่าง = ไม่แก้)",
        new_shift="สร้างกะใหม่แทนการแก้กะล่าสุด (กรณีลืมกดเข้างานทั้งกะ)",
    )
    async def attendance_fix(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        clock_in: str | None = None,
        clock_out: str | None = None,
        new_shift: bool = False,
    ) -> None:
        if not is_admin(interaction.user, self.cfg.admin_role_id):
            await interaction.response.send_message("เฉพาะแอดมินเท่านั้นค่ะ", ephemeral=True)
            return
        await self.fix_attendance(interaction, member, clock_in, clock_out, new_shift)

    async def fix_attendance(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        clock_in: str | None,
        clock_out: str | None,
        new_shift: bool = False,
    ) -> None:
        """แก้เวลาเข้างาน (ใช้ทั้งจาก /attendance_fix และเมนูแอดมิน) — ผู้เรียกต้องตรวจสิทธิ์แอดมินก่อน"""
        if not clock_in and not clock_out:
            await interaction.response.send_message("ระบุ clock_in หรือ clock_out อย่างน้อย 1 ค่าค่ะ", ephemeral=True)
            return

        tz = self.cfg.tz
        try:
            new_in = parse_past_time(clock_in, tz) if clock_in else None
            new_out = parse_past_time(clock_out, tz) if clock_out else None
        except TimeParseError as exc:
            await interaction.response.send_message(f"❌ {exc}", ephemeral=True)
            return

        if new_shift:
            if new_in is None or new_out is None:
                await interaction.response.send_message("สร้างกะใหม่ต้องระบุทั้ง clock_in และ clock_out ค่ะ", ephemeral=True)
                return
            row_id = await self.db.create_attendance(member.guild.id, member.id, to_iso(new_in))
            row = await self.db.get_attendance(row_id)
        else:
            row = await self.db.latest_attendance(member.id)
            if row is None:
                await interaction.response.send_message(
                    "พนักงานคนนี้ยังไม่มีบันทึกเข้างาน ใช้ `new_shift: True` เพื่อสร้างกะใหม่ค่ะ", ephemeral=True
                )
                return

        final_in = new_in or from_iso(row["clock_in"])
        final_out = new_out or from_iso(row["clock_out"])
        if final_out is not None and final_out <= final_in:
            if new_shift:
                await self.db.execute("DELETE FROM attendance WHERE id = ?", (row["id"],))
            await interaction.response.send_message("❌ เวลาออกงานต้องอยู่หลังเวลาเข้างานค่ะ", ephemeral=True)
            return

        await self.db.update_attendance(
            row["id"],
            clock_in=to_iso(final_in),
            clock_out=to_iso(final_out) if final_out else None,
            edited_by=interaction.user.id,
            note=f"แอดมิน {interaction.user.display_name} แก้เวลา",
        )
        if final_out is not None:
            await self._set_on_duty(member.guild, member.id, False)

        summary = (
            f"กะ `#{row['id']}` ของ {member.mention}\n"
            f"เข้างาน {discord_ts(final_in, 'f')} → "
            + (f"ออกงาน {discord_ts(final_out, 'f')} ({fmt_hours((final_out - final_in).total_seconds())})" if final_out else "ยังไม่ออกงาน")
        )
        await interaction.response.send_message(f"✅ แก้เวลาแล้ว\n{summary}", ephemeral=True)
        await self._notify_admin(
            embed=discord.Embed(
                title="✏️ แก้เวลาเข้างาน",
                description=f"{summary}\nโดย {interaction.user.mention}",
                color=COLOR_WARN,
            )
        )
        if final_out is not None:
            await self._log_to_sheet(await self.db.get_attendance(row["id"]))


async def setup(bot: commands.Bot) -> None:
    bot.add_view(AttendancePanel())
    await bot.add_cog(AttendanceCog(bot))
