"""ส่วนงานรีเซปชั่น: แผงควบคุมแอดมิน, เปิดบิล, ต่อเวลา, พนักงานรับงาน"""
from __future__ import annotations

import datetime as dt
import logging
import re

import discord
from discord import app_commands
from discord.ext import commands

from core.embeds import COLOR_DANGER, COLOR_INFO, COLOR_MAIN, COLOR_OK, COLOR_WARN, job_embed
from core.pricing import (
    job_staff_ids,
    job_staff_split,
    quote_services,
    reserve_quota_for_job,
    split_revenue,
    validate_selection,
)
from core.utils import (
    TimeParseError,
    discord_ts,
    from_iso,
    is_admin,
    money,
    now_utc,
    parse_start_time,
    purge_old_panels,
    send_dm,
    to_iso,
)
from core.vip_logic import active_tier, cycle_month_key
from cogs.attendance import accept_label

log = logging.getLogger("olp.reception")

ACTIVE_STATUSES = ("PENDING_STAFF", "ACCEPTED", "SLIP_PENDING", "PAID")


# ------------------------------------------------------------ ปุ่มรับงาน
class JobAcceptButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"olp:job_accept:(?P<job_id>\d+)",
):
    def __init__(self, job_id: int) -> None:
        self.job_id = job_id
        super().__init__(
            discord.ui.Button(
                label="รับงาน",
                emoji="✅",
                style=discord.ButtonStyle.success,
                custom_id=f"olp:job_accept:{job_id}",
            )
        )

    @classmethod
    async def from_custom_id(cls, interaction, item, match: re.Match[str]):
        return cls(int(match["job_id"]))

    async def callback(self, interaction: discord.Interaction) -> None:  # type: ignore[override]
        cog: ReceptionCog = interaction.client.get_cog("ReceptionCog")  # type: ignore[assignment]
        await cog.staff_accept(interaction, self.job_id)


class JobRejectButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"olp:job_reject:(?P<job_id>\d+)",
):
    """ปุ่มให้พนักงานปฏิเสธงาน เผื่อกรณีแอดมินคีย์บิลผิด หรือไม่สะดวกรับบริการเสริม (ใช้ได้เฉพาะก่อนกดรับงาน)"""

    def __init__(self, job_id: int) -> None:
        self.job_id = job_id
        super().__init__(
            discord.ui.Button(
                label="ปฏิเสธงาน",
                emoji="❌",
                style=discord.ButtonStyle.danger,
                custom_id=f"olp:job_reject:{job_id}",
            )
        )

    @classmethod
    async def from_custom_id(cls, interaction, item, match: re.Match[str]):
        return cls(int(match["job_id"]))

    async def callback(self, interaction: discord.Interaction) -> None:  # type: ignore[override]
        cog: ReceptionCog = interaction.client.get_cog("ReceptionCog")  # type: ignore[assignment]
        await cog.staff_reject_prompt(interaction, self.job_id)


class JobRejectReasonModal(discord.ui.Modal, title="ปฏิเสธงาน"):
    reason = discord.ui.TextInput(
        label="เหตุผล (ถ้ามี)",
        placeholder="เช่น คีย์ผิดคน / ผิดบริการ / ผิดเวลา / ไม่สะดวกรับบริการนี้",
        style=discord.TextStyle.paragraph,
        required=False,
        max_length=300,
    )

    def __init__(self, job_id: int) -> None:
        super().__init__()
        self.job_id = job_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        cog: ReceptionCog = interaction.client.get_cog("ReceptionCog")  # type: ignore[assignment]
        await cog.staff_reject(interaction, self.job_id, str(self.reason).strip())


def accept_view(job_id: int) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(JobAcceptButton(job_id))
    view.add_item(JobRejectButton(job_id))
    return view


# ---------------------------------------------------------------- helpers
def _service_hint(svc: dict) -> str:
    """คำอธิบายสั้นใต้ตัวเลือกบริการ (สูงสุด 100 ตัวอักษรตามข้อจำกัด Discord)"""
    price = svc.get("pricing", {}).get("normal", 0)
    if svc.get("addon_for"):
        text = f"+{price:,.0f} บาท · บริการเสริม (เลือกคู่กับบริการหลัก)"
    elif svc.get("per_unit"):
        text = f"{price:,.0f} บาท / {svc.get('unit_label', 'หน่วย')} · กรอกจำนวนตอนยืนยัน"
    else:
        text = f"{price:,.0f} บาท / {svc.get('duration_minutes', 60)} นาที"
        if svc.get("multi_staff"):
            text += (
                f" · รวมพนักงาน {svc.get('included_staff', 1)} คน"
                f" (+{svc.get('extra_staff_price', 0):,.0f}/คน)"
            )
    return text[:100]


def _plain(text: str) -> str:
    return text.replace("**", "")


def adult_problem(cfg, customer: discord.abc.User | None, service_keys: list[str]) -> str | None:
    """บริการ adult_only ต้องให้ลูกค้ามี Role ยืนยันอายุ (roles.adult_verified) ถ้าตั้งค่าไว้"""
    adult = [k for k in dict.fromkeys(service_keys) if (cfg.service(k) or {}).get("adult_only")]
    role_ids = set(cfg.adult_role_ids)
    if not adult or not role_ids:
        return None
    if isinstance(customer, discord.Member) and role_ids & {r.id for r in customer.roles}:
        return None
    return (
        f"**{cfg.service_names(adult)}** เปิดได้เฉพาะลูกค้าที่มี Role ยืนยันอายุ 18+ ค่ะ"
    )


def has_adult_service(cfg, service_keys: list[str]) -> bool:
    return any((cfg.service(k) or {}).get("adult_only") for k in service_keys)


# ------------------------------------------------------------ Modal เวลา
class BillDetailModal(discord.ui.Modal, title="รายละเอียดบิล"):
    start_time = discord.ui.TextInput(
        label="เวลาเริ่มงาน",
        placeholder="ตอนนี้ / +15 / 20:30 / 05/09 20:30",
        default="ตอนนี้",
        required=True,
        max_length=32,
    )
    note = discord.ui.TextInput(
        label="หมายเหตุ",
        style=discord.TextStyle.paragraph,
        required=False,
        max_length=400,
    )

    def __init__(self, wizard: "OpenBillWizard") -> None:
        super().__init__()
        self.wizard = wizard
        self.quantities: dict[str, discord.ui.TextInput] = {}
        # บริการคิดต่อหน่วย (เช่น Drink Friend) ให้กรอกจำนวนเพิ่ม — Modal ใส่ช่องได้สูงสุด 5 ช่อง
        for key in wizard.unit_service_keys()[:3]:
            svc = wizard.cfg.service(key) or {}
            field = discord.ui.TextInput(
                label=f"จำนวน {svc.get('name', key)} ({svc.get('unit_label', 'หน่วย')})"[:45],
                default="1",
                required=True,
                max_length=2,
            )
            self.quantities[key] = field
            self.add_item(field)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        quantities: dict[str, int] = {}
        for key, field in self.quantities.items():
            try:
                qty = int(str(field.value).strip())
            except ValueError:
                qty = 0
            if not 1 <= qty <= 50:
                await interaction.response.send_message(
                    f"⚠️ จำนวน **{self.wizard.cfg.service_name(key)}** ต้องเป็นตัวเลข 1-50 ค่ะ",
                    ephemeral=True,
                )
                return
            quantities[key] = qty
        await self.wizard.submit(interaction, str(self.start_time), str(self.note), quantities)


# ---------------------------------------------------------- Wizard เปิดบิล
class OpenBillWizard(discord.ui.View):
    """แผงเลือกข้อมูลเปิดบิลแบบขั้นตอนเดียว (ephemeral)"""

    def __init__(
        self, cog: "ReceptionCog", opener: discord.Member, today: dict[int, dict] | None = None
    ) -> None:
        super().__init__(timeout=600)
        self.cog = cog
        self.cfg = cog.cfg
        self.opener = opener
        self.today = today or {}  # {staff_id: prefs} ของคนที่กดเข้างานวันนี้
        self.customer: discord.abc.User | None = None
        self.customer_id: int | None = None
        self.staff_ids: list[int] = []
        self.service_keys: list[str] = []
        self.room_key: str | None = None

        self.customer_select = discord.ui.UserSelect(
            placeholder="👤 เลือกลูกค้า", min_values=1, max_values=1, row=0
        )
        self.customer_select.callback = self._on_customer
        self.add_item(self.customer_select)

        # บริการที่รองรับหลายพนักงาน (Party Room) ทำให้เมนูพนักงานเลือกได้หลายคน
        max_staff = max(
            [int(s.get("max_staff", 10)) for s in self.cfg.bookable_services() if s.get("multi_staff")]
            or [1]
        )
        staff_hint = " (Party Room เลือกได้หลายคน)" if max_staff > 1 else ""

        # ถ้าตั้ง roles.staff ไว้ ให้แสดงรายชื่อพนักงานเป็นเมนู (ไม่ต้องพิมพ์ค้นหา ชื่อฟอนต์พิเศษก็เลือกได้)
        # คนที่เข้างานวันนี้ขึ้นก่อน พร้อมบอกงานที่รับ
        staff = sorted(self._staff_members(opener), key=lambda m: m.id not in self.today)
        if 0 < len(staff) <= 25:
            self.staff_select = discord.ui.Select(
                placeholder=f"💃 เลือกพนักงาน{staff_hint}",
                min_values=1,
                max_values=min(max_staff, len(staff)),
                row=1,
                options=[
                    discord.SelectOption(
                        label=m.display_name[:100],
                        value=str(m.id),
                        emoji="🟢" if m.id in self.today else "⚪",
                        description=self._staff_hint(m.id),
                    )
                    for m in staff
                ],
            )
        else:
            self.staff_select = discord.ui.UserSelect(
                placeholder=f"💃 เลือกพนักงาน{staff_hint}",
                min_values=1,
                max_values=min(max_staff, 25),
                row=1,
            )
        self.staff_select.callback = self._on_staff
        self.add_item(self.staff_select)

        services = self.cfg.bookable_services()
        self.service_select = discord.ui.Select(
            placeholder="🛎️ เลือกบริการ (เลือกได้มากกว่า 1)",
            min_values=1,
            max_values=max(1, min(len(services), 25)),
            row=2,
            options=[
                discord.SelectOption(
                    label=svc["name"],
                    value=svc["key"],
                    emoji=svc.get("emoji"),
                    description=_service_hint(svc),
                )
                for svc in services[:25]
            ],
        )
        self.service_select.callback = self._on_services
        self.add_item(self.service_select)

        self.room_select = discord.ui.Select(
            placeholder="🚪 เลือกห้อง (เฉพาะบริการที่ต้องใช้ห้อง)",
            min_values=1,
            max_values=1,
            row=3,
            options=self._room_options(),
        )
        self.room_select.callback = self._on_room
        self.add_item(self.room_select)

    # ------------------------------------------------------------ callbacks
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.opener.id:
            await interaction.response.send_message("แผงนี้ของแอดมินคนอื่นค่ะ", ephemeral=True)
            return False
        return True

    async def _on_customer(self, interaction: discord.Interaction) -> None:
        self.customer = self.customer_select.values[0]
        self.customer_id = self.customer.id
        await self._refresh(interaction)

    def _staff_members(self, opener: discord.Member) -> list[discord.Member]:
        guild = getattr(opener, "guild", None)
        role_ids = set(self.cfg.staff_role_ids)
        if guild is None or not role_ids:
            return []
        members = {
            m.id: m
            for role_id in role_ids
            if (role := guild.get_role(role_id)) is not None
            for m in role.members
            if not m.bot
        }
        return sorted(members.values(), key=lambda m: m.display_name.lower())

    def _room_options(self) -> list[discord.SelectOption]:
        rooms = self.cfg.rooms_for_services(self.service_keys) if self.service_keys else self.cfg.rooms
        options = [
            discord.SelectOption(label=room["name"], value=room["key"], default=room["key"] == self.room_key)
            for room in rooms[:25]
        ]
        return options or [discord.SelectOption(label="ไม่มีห้องที่ใช้กับบริการนี้ใน config", value="none")]

    def _staff_hint(self, staff_id: int) -> str:
        prefs = self.today.get(staff_id)
        if prefs is None:
            return "ยังไม่กดเข้างานวันนี้"
        accepts = ", ".join(accept_label(self.cfg, k) for k in prefs["accepts"]) or "-"
        return f"รับ: {accepts}"[:100]

    def _needed_categories(self) -> set[str]:
        return {
            cat
            for key in self.service_keys
            if (cat := (self.cfg.service(key) or {}).get("category"))
        }

    def _avoid_problem(self) -> str | None:
        """พนักงานที่เลือกระบุไว้ว่าไม่รับลูกค้าคนนี้วันนี้ -> ห้ามเปิดบิล"""
        for sid in self.staff_ids:
            if self.customer_id in self.today.get(sid, {}).get("avoid_ids", []):
                return f"<@{sid}> แจ้งไว้ว่าวันนี้ไม่รับลูกค้าคนนี้ค่ะ — เลือกพนักงานคนอื่นนะคะ"
        return None

    def staff_warnings(self) -> list[str]:
        """คำเตือน (ไม่บล็อก): ยังไม่เข้างาน / ไม่รับงานประเภทนี้ / มีหมายเหตุ"""
        need = self._needed_categories()
        lines = []
        for sid in self.staff_ids:
            prefs = self.today.get(sid)
            if prefs is None:
                lines.append(f"⚪ <@{sid}> ยังไม่กดเข้างานวันนี้")
                continue
            missing = need - set(prefs["accepts"])
            if missing:
                labels = ", ".join(accept_label(self.cfg, k) for k in sorted(missing))
                lines.append(f"⚠️ <@{sid}> วันนี้ไม่รับ: {labels}")
            if prefs["avoid_text"]:
                lines.append(f"📝 <@{sid}> หมายเหตุ: {prefs['avoid_text'][:150]}")
        return lines

    def unit_service_keys(self) -> list[str]:
        return [k for k in self.service_keys if (self.cfg.service(k) or {}).get("per_unit")]

    async def _on_staff(self, interaction: discord.Interaction) -> None:
        self.staff_ids = [int(v) if isinstance(v, str) else v.id for v in self.staff_select.values]
        await self._refresh(interaction)

    async def _on_services(self, interaction: discord.Interaction) -> None:
        self.service_keys = list(self.service_select.values)
        allowed = {r["key"] for r in self.cfg.rooms_for_services(self.service_keys)}
        if self.room_key not in allowed:
            self.room_key = None
        self.room_select.options = self._room_options()
        await self._refresh(interaction)

    async def _on_room(self, interaction: discord.Interaction) -> None:
        value = self.room_select.values[0]
        self.room_key = None if value == "none" else value
        self.room_select.options = self._room_options()
        await self._refresh(interaction)

    @discord.ui.button(
        label="กรอกเวลา & ยืนยันเปิดบิล", emoji="🧾", style=discord.ButtonStyle.primary, row=4
    )
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        problem = self._validate()
        if problem:
            await interaction.response.send_message(problem, ephemeral=True)
            return
        await interaction.response.send_modal(BillDetailModal(self))

    # -------------------------------------------------------------- helpers
    def _requires_room(self) -> bool:
        return any(
            (self.cfg.service(key) or {}).get("require_room") for key in self.service_keys
        )

    def _validate(self) -> str | None:
        if not self.customer_id:
            return "ยังไม่ได้เลือก **ลูกค้า** ค่ะ"
        if not self.staff_ids:
            return "ยังไม่ได้เลือก **พนักงาน** ค่ะ"
        if self.customer_id in self.staff_ids:
            return "ลูกค้ากับพนักงานเป็นคนเดียวกันไม่ได้ค่ะ"
        if not self.service_keys:
            return "ยังไม่ได้เลือก **บริการ** ค่ะ"
        if self._requires_room() and not self.room_key:
            return "บริการที่เลือกต้องระบุ **ห้อง** ด้วยค่ะ"
        return (
            self._avoid_problem()
            or validate_selection(self.cfg, self.service_keys, len(self.staff_ids))
            or adult_problem(self.cfg, self.customer, self.service_keys)
        )

    async def _customer_tier(self) -> str | None:
        if not self.customer_id:
            return None
        return await active_tier(self.cog.db, self.customer_id, dt.datetime.now(self.cfg.tz))

    async def summary_embed(self) -> discord.Embed:
        embed = discord.Embed(
            title="🧾 เปิดบิลใหม่",
            description="เลือกข้อมูลให้ครบ แล้วกดปุ่ม **กรอกเวลา & ยืนยันเปิดบิล**",
            color=COLOR_MAIN,
        )
        embed.add_field(
            name="ลูกค้า",
            value=f"<@{self.customer_id}>" if self.customer_id else "*ยังไม่เลือก*",
            inline=True,
        )
        embed.add_field(
            name="พนักงาน" + (f" ({len(self.staff_ids)} คน)" if len(self.staff_ids) > 1 else ""),
            value=" ".join(f"<@{s}>" for s in self.staff_ids) if self.staff_ids else "*ยังไม่เลือก*",
            inline=True,
        )
        embed.add_field(
            name="ห้อง",
            value=self.cfg.room_name(self.room_key) if self.room_key else "*ยังไม่เลือก*",
            inline=True,
        )
        embed.add_field(
            name="บริการ",
            value=self.cfg.service_names(self.service_keys) if self.service_keys else "*ยังไม่เลือก*",
            inline=False,
        )

        if self.service_keys and self.customer_id:
            tier = await self._customer_tier()
            quote = await quote_services(
                self.cfg,
                self.cog.db,
                self.service_keys,
                customer_id=self.customer_id,
                tier=tier,
                staff_count=max(len(self.staff_ids), 1),
            )
            unit_note = (
                "\n*บริการคิดต่อหน่วย: กรอกจำนวนในขั้นถัดไป (ราคานี้คิด 1 หน่วย)*"
                if self.unit_service_keys()
                else ""
            )
            embed.add_field(
                name="ราคาโดยประมาณ",
                value=(
                    f"{quote.breakdown}\n"
                    f"**รวม {money(quote.total_price)}** · {quote.duration_minutes} นาที"
                    + (f"\n{self.cfg.vip_tier_name(tier)} — คิดราคา/สิทธิ์ตามระดับอัตโนมัติ" if tier else "")
                    + unit_note
                ),
                inline=False,
            )

        if self.room_key and self._requires_room():
            busy = await self.cog.room_busy_jobs(self.room_key)
            if busy:
                embed.add_field(
                    name="⚠️ ห้องนี้มีบิลที่ยังไม่จบ",
                    value="\n".join(
                        f"บิล `#{j['id']}` ถึง {discord_ts(from_iso(j['end_time']))}" for j in busy[:5]
                    ),
                    inline=False,
                )

        warnings = self.staff_warnings()
        if warnings:
            embed.add_field(name="เช็คพนักงาน", value="\n".join(warnings)[:1024], inline=False)

        ready = self.customer_id and self.staff_ids and self.service_keys
        problem = self._validate() if ready else None
        if problem:
            embed.set_footer(text=f"⚠️ {_plain(problem)}")
        elif self._requires_room() and not self.room_key:
            embed.set_footer(text="⚠️ บริการที่เลือกต้องระบุห้อง")
        elif has_adult_service(self.cfg, self.service_keys):
            embed.set_footer(text="🔞 มีบริการ 18+ — พนักงานต้องตกลงกับลูกค้าก่อนกดรับงาน")
        return embed

    async def _refresh(self, interaction: discord.Interaction) -> None:
        await interaction.response.edit_message(embed=await self.summary_embed(), view=self)

    # --------------------------------------------------------------- submit
    async def submit(
        self, interaction: discord.Interaction, raw_time: str, note: str, quantities: dict[str, int]
    ) -> None:
        problem = self._validate()
        if problem:
            await interaction.response.send_message(problem, ephemeral=True)
            return

        try:
            start = parse_start_time(raw_time, self.cfg.tz)
        except TimeParseError as exc:
            await interaction.response.send_message(f"⚠️ {exc}", ephemeral=True)
            return

        # บริการคิดต่อหน่วยเก็บเป็น key ซ้ำตามจำนวน (เช่น Drink Friend 3 shot = key 3 ตัว)
        service_keys = [key for key in self.service_keys for _ in range(quantities.get(key, 1))]

        await interaction.response.defer()
        job_id = await self.cog.create_job(
            guild=interaction.guild,
            opener=interaction.user,
            customer_id=self.customer_id,
            staff_ids=self.staff_ids,
            service_keys=service_keys,
            room_key=self.room_key if self._requires_room() else None,
            note=note.strip(),
            start=start,
        )
        job = await self.cog.db.get_job(job_id)

        self.stop()
        await interaction.edit_original_response(
            embed=job_embed(self.cfg, job, title="✅ เปิดบิลเรียบร้อย", color=COLOR_OK),
            view=None,
        )


# ---------------------------------------------------------- Wizard ต่อเวลา
class ExtendWizard(discord.ui.View):
    def __init__(self, cog: "ReceptionCog", opener: discord.Member, jobs: list[dict]) -> None:
        super().__init__(timeout=600)
        self.cog = cog
        self.cfg = cog.cfg
        self.opener = opener
        self.guild = getattr(opener, "guild", None)
        self.jobs = {job["id"]: job for job in jobs}
        self.job_id: int | None = None
        self.service_keys: list[str] = []

        self.job_select = discord.ui.Select(
            placeholder="🧾 เลือกบิลที่ต้องการต่อเวลา",
            row=0,
            options=[
                discord.SelectOption(
                    label=f"บิล #{job['id']} · {self.cfg.service_names(job['services'])[:60]}",
                    value=str(job["id"]),
                    description=(
                        f"จบ {from_iso(job['end_time']).astimezone(self.cfg.tz):%d/%m %H:%M}"
                    ),
                )
                for job in jobs[:25]
            ],
        )
        self.job_select.callback = self._on_job
        self.add_item(self.job_select)

        extends = self.cfg.extend_services()
        self.service_select = discord.ui.Select(
            placeholder="⏱️ เลือกแพ็กเกจต่อเวลา (+บริการเสริมได้)",
            min_values=1,
            max_values=max(1, min(len(extends), 25)),
            row=1,
            options=[
                discord.SelectOption(
                    label=svc["name"],
                    value=svc["key"],
                    emoji=svc.get("emoji"),
                    description=_service_hint(svc),
                )
                for svc in extends[:25]
            ]
            or [discord.SelectOption(label="ยังไม่ได้ตั้งค่าแพ็กเกจต่อเวลา", value="none")],
        )
        self.service_select.callback = self._on_services
        self.add_item(self.service_select)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.opener.id:
            await interaction.response.send_message("แผงนี้ของแอดมินคนอื่นค่ะ", ephemeral=True)
            return False
        return True

    async def _on_job(self, interaction: discord.Interaction) -> None:
        self.job_id = int(self.job_select.values[0])
        await self._refresh(interaction)

    async def _on_services(self, interaction: discord.Interaction) -> None:
        self.service_keys = [v for v in self.service_select.values if v != "none"]
        await self._refresh(interaction)

    def _validate(self) -> str | None:
        if not self.job_id:
            return "ยังไม่ได้เลือกบิลค่ะ"
        if not self.service_keys:
            return "ยังไม่ได้เลือกแพ็กเกจต่อเวลาค่ะ"
        if not any((self.cfg.service(k) or {}).get("extend_only") for k in self.service_keys):
            return "ต้องเลือกแพ็กเกจต่อเวลาอย่างน้อย 1 รายการ (บริการเสริมต่อเดี่ยวๆ ไม่ได้) ค่ะ"
        customer_id = self.jobs[self.job_id]["customer_id"]
        customer = self.guild.get_member(customer_id) if self.guild else None
        return validate_selection(self.cfg, self.service_keys, 1) or adult_problem(
            self.cfg, customer, self.service_keys
        )

    async def summary_embed(self) -> discord.Embed:
        embed = discord.Embed(title="⏱️ ต่อเวลา / เพิ่มรอบ (EXTEND)", color=COLOR_MAIN)
        if self.job_id:
            job = self.jobs[self.job_id]
            end = from_iso(job["end_time"])
            staff = " ".join(f"<@{s}>" for s in job_staff_ids(job))
            embed.add_field(
                name="บิลเดิม",
                value=(
                    f"`#{job['id']}` · ลูกค้า <@{job['customer_id']}> · พนักงาน {staff}\n"
                    f"{self.cfg.service_names(job['services'])}\n"
                    f"เวลาจบปัจจุบัน: {discord_ts(end)}"
                ),
                inline=False,
            )
        else:
            embed.add_field(name="บิลเดิม", value="*ยังไม่เลือก*", inline=False)

        if self.service_keys and self.job_id:
            job = self.jobs[self.job_id]
            tier = await active_tier(self.cog.db, job["customer_id"], dt.datetime.now(self.cfg.tz))
            quote = await quote_services(
                self.cfg, self.cog.db, self.service_keys, customer_id=job["customer_id"], tier=tier
            )
            embed.add_field(
                name="แพ็กเกจต่อเวลา",
                value=f"{quote.breakdown}\n**รวม {money(quote.total_price)}** · +{quote.duration_minutes} นาที",
                inline=False,
            )
            problem = self._validate()
            if problem:
                embed.set_footer(text=f"⚠️ {_plain(problem)}")
        return embed

    async def _refresh(self, interaction: discord.Interaction) -> None:
        await interaction.response.edit_message(embed=await self.summary_embed(), view=self)

    @discord.ui.button(label="ยืนยันต่อเวลา", emoji="✅", style=discord.ButtonStyle.primary, row=2)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        problem = self._validate()
        if problem:
            await interaction.response.send_message(problem, ephemeral=True)
            return

        await interaction.response.defer()
        extend_id = await self.cog.create_extend(
            interaction.guild, interaction.user, self.job_id, self.service_keys
        )
        extend_job = await self.cog.db.get_job(extend_id)
        parent = await self.cog.db.get_job(self.job_id)

        self.stop()
        embed = job_embed(self.cfg, extend_job, title="✅ เปิดบิลต่อเวลาแล้ว", color=COLOR_OK)
        embed.add_field(
            name="เวลาจบใหม่ของบิลเดิม",
            value=f"บิล `#{parent['id']}` → {discord_ts(from_iso(parent['end_time']))}",
            inline=False,
        )
        await interaction.edit_original_response(embed=embed, view=None)


# -------------------------------------------------------------- แผงควบคุม
class ReceptionPanel(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(
        label="เปิดบิลใหม่",
        emoji="🧾",
        style=discord.ButtonStyle.primary,
        custom_id="olp:panel:open_bill",
    )
    async def open_bill(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        cog: ReceptionCog = interaction.client.get_cog("ReceptionCog")  # type: ignore[assignment]
        await cog.open_bill_panel(interaction)

    @discord.ui.button(
        label="ต่อเวลา / เพิ่มรอบ",
        emoji="⏱️",
        style=discord.ButtonStyle.secondary,
        custom_id="olp:panel:extend",
    )
    async def extend(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        cog: ReceptionCog = interaction.client.get_cog("ReceptionCog")  # type: ignore[assignment]
        await cog.open_extend_panel(interaction)

    @discord.ui.button(
        label="งานที่กำลังดำเนินอยู่",
        emoji="📋",
        style=discord.ButtonStyle.secondary,
        custom_id="olp:panel:active",
    )
    async def active(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        cog: ReceptionCog = interaction.client.get_cog("ReceptionCog")  # type: ignore[assignment]
        await cog.show_active_jobs(interaction)


# --------------------------------------------------------------------- cog
class ReceptionCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.cfg = bot.cfg
        self.db = bot.db

    # ------------------------------------------------------------- panels
    async def open_bill_panel(self, interaction: discord.Interaction) -> None:
        if not self._admin_guard(interaction):
            await interaction.response.send_message("เฉพาะแอดมินเท่านั้นค่ะ", ephemeral=True)
            return
        attendance = self.bot.get_cog("AttendanceCog")
        today = await attendance.today_prefs() if attendance else {}
        wizard = OpenBillWizard(self, interaction.user, today)
        await interaction.response.send_message(
            embed=await wizard.summary_embed(), view=wizard, ephemeral=True
        )

    async def open_extend_panel(self, interaction: discord.Interaction) -> None:
        if not self._admin_guard(interaction):
            await interaction.response.send_message("เฉพาะแอดมินเท่านั้นค่ะ", ephemeral=True)
            return
        now = now_utc()
        jobs = [
            job
            for job in await self.db.jobs_by_status(["ACCEPTED", "SLIP_PENDING", "PAID"])
            if job["job_type"] == "NORMAL" and from_iso(job["end_time"]) > now
        ]
        if not jobs:
            await interaction.response.send_message(
                "ตอนนี้ยังไม่มีบิลที่กำลังดำเนินอยู่ค่ะ", ephemeral=True
            )
            return
        wizard = ExtendWizard(self, interaction.user, jobs)
        await interaction.response.send_message(
            embed=await wizard.summary_embed(), view=wizard, ephemeral=True
        )

    async def show_active_jobs(self, interaction: discord.Interaction) -> None:
        if not self._admin_guard(interaction):
            await interaction.response.send_message("เฉพาะแอดมินเท่านั้นค่ะ", ephemeral=True)
            return
        now = now_utc()
        jobs = [
            job
            for job in await self.db.jobs_by_status(list(ACTIVE_STATUSES))
            if from_iso(job["end_time"]) > now
        ]
        if not jobs:
            await interaction.response.send_message("ยังไม่มีงานค้างอยู่ค่ะ", ephemeral=True)
            return

        embed = discord.Embed(title="📋 งานที่กำลังดำเนินอยู่", color=COLOR_INFO)
        for job in jobs[:20]:
            staff = " ".join(f"<@{s}>" for s in job_staff_ids(job))
            embed.add_field(
                name=f"บิล #{job['id']} · {job['status']}",
                value=(
                    f"<@{job['customer_id']}> ↔ {staff}\n"
                    f"{self.cfg.service_names(job['services'])} · {money(job['total_price'])}\n"
                    f"ห้อง {self.cfg.room_name(job.get('room'))} · จบ {discord_ts(from_iso(job['end_time']))}"
                ),
                inline=False,
            )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    def _admin_guard(self, interaction: discord.Interaction) -> bool:
        return is_admin(interaction.user, self.cfg.admin_role_id)

    async def room_busy_jobs(self, room_key: str) -> list[dict]:
        """บิลที่ยังไม่จบและใช้ห้องนี้อยู่ (ไว้เตือนตอนเปิดบิลซ้อนห้อง)"""
        now = now_utc()
        return [
            job
            for job in await self.db.jobs_by_status(list(ACTIVE_STATUSES))
            if job.get("room") == room_key
            and job["job_type"] == "NORMAL"
            and from_iso(job["end_time"]) > now
        ]

    # ---------------------------------------------------------- สร้างบิล
    async def create_job(
        self,
        *,
        guild: discord.Guild,
        opener: discord.abc.User,
        customer_id: int,
        staff_ids: list[int],
        service_keys: list[str],
        room_key: str | None,
        note: str,
        start: dt.datetime,
        job_type: str = "NORMAL",
        parent_job_id: int | None = None,
    ) -> int:
        """staff_ids[0] = พนักงานหลัก (คนกดรับงาน) ที่เหลือ = พนักงานร่วม (Party Room)"""
        now_local = dt.datetime.now(self.cfg.tz)
        tier = await active_tier(self.db, customer_id, now_local)
        staff_id, co_staff = staff_ids[0], list(staff_ids[1:])
        quote = await quote_services(
            self.cfg,
            self.db,
            service_keys,
            customer_id=customer_id,
            tier=tier,
            staff_count=len(staff_ids),
            now_local=now_local,
        )
        staff_share, shop_share = split_revenue(self.cfg, staff_ids, quote.total_price, quote.amounts)
        end = start + dt.timedelta(minutes=quote.duration_minutes)
        cycle = cycle_month_key(now_local)

        job_id = await self.db.create_job(
            guild_id=guild.id,
            job_type=job_type,
            parent_job_id=parent_job_id,
            customer_id=customer_id,
            staff_id=staff_id,
            co_staff=co_staff,
            services=service_keys,
            room=room_key,
            note=note or None,
            start_time=to_iso(start),
            end_time=to_iso(end),
            duration_minutes=quote.duration_minutes,
            vip_tier=tier,
            quota_services=quote.quota_services,
            quota_cycle=cycle if quote.quota_services else None,
            total_price=quote.total_price,
            staff_share=staff_share,
            shop_share=shop_share,
            status="PENDING_STAFF",
            opened_by=opener.id,
            created_at=to_iso(now_utc()),
        )
        await reserve_quota_for_job(self.db, customer_id, quote, cycle)

        job = await self.db.get_job(job_id)
        shares = {sid: share for sid, _, share in job_staff_split(self.cfg, job)}
        payments = self.bot.get_cog("PaymentsCog")

        embed = job_embed(self.cfg, job, title="🔔 มีงานใหม่เข้ามา", color=COLOR_WARN)
        embed.add_field(name="ส่วนแบ่งของคุณ", value=money(shares.get(staff_id, staff_share)), inline=True)
        if has_adult_service(self.cfg, service_keys):
            embed.add_field(
                name="🔞 มีบริการ 18+",
                value="กดรับงานเฉพาะเมื่อคุณตกลงกับลูกค้าเรียบร้อยแล้ว ถ้าไม่สะดวกกด **ปฏิเสธงาน** ได้เลย",
                inline=False,
            )
        if co_staff:
            embed.add_field(
                name="👑 คุณเป็นพนักงานหลักของบิลนี้",
                value="กดรับงานแทนทั้งทีม — ลูกค้าจะได้ยอดชำระหลังคุณกดรับงาน",
                inline=False,
            )
        sent = await send_dm(self.bot, staff_id, embed=embed, view=accept_view(job_id))
        if sent is None:
            await payments.notify_admin_text(
                f"⚠️ ส่ง DM แจ้งงานบิล `#{job_id}` ถึงพนักงาน <@{staff_id}> ไม่สำเร็จ (ปิด DM อยู่)"
            )

        for sid in co_staff:
            info = job_embed(self.cfg, job, title="🔔 คุณถูกเพิ่มเข้าบิลนี้", color=COLOR_INFO)
            info.add_field(name="ส่วนแบ่งของคุณ", value=money(shares.get(sid, 0)), inline=True)
            info.add_field(
                name="พนักงานหลัก", value=f"<@{staff_id}> เป็นคนกดรับงานแทนทีม", inline=False
            )
            if await send_dm(self.bot, sid, embed=info) is None:
                await payments.notify_admin_text(
                    f"⚠️ ส่ง DM แจ้งบิล `#{job_id}` ถึงพนักงานร่วม <@{sid}> ไม่สำเร็จ (ปิด DM อยู่)"
                )
        return job_id

    async def create_extend(
        self,
        guild: discord.Guild,
        opener: discord.abc.User,
        parent_id: int,
        service_keys: list[str],
    ) -> int:
        parent = await self.db.get_job(parent_id)
        now_local = dt.datetime.now(self.cfg.tz)
        tier = await active_tier(self.db, parent["customer_id"], now_local)
        quote = await quote_services(
            self.cfg,
            self.db,
            service_keys,
            customer_id=parent["customer_id"],
            tier=tier,
            now_local=now_local,
        )
        staff_ids = job_staff_ids(parent)
        staff_share, shop_share = split_revenue(self.cfg, staff_ids, quote.total_price, quote.amounts)
        cycle = cycle_month_key(now_local)

        old_end = from_iso(parent["end_time"])
        new_end = old_end + dt.timedelta(minutes=quote.duration_minutes)

        extend_id = await self.db.create_job(
            guild_id=guild.id,
            job_type="EXTEND",
            parent_job_id=parent_id,
            customer_id=parent["customer_id"],
            staff_id=parent["staff_id"],
            co_staff=parent.get("co_staff") or [],
            services=service_keys,
            room=parent.get("room"),
            note=f"ต่อเวลาจากบิล #{parent_id}",
            start_time=parent["end_time"],
            end_time=to_iso(new_end),
            duration_minutes=quote.duration_minutes,
            vip_tier=tier,
            quota_services=quote.quota_services,
            quota_cycle=cycle if quote.quota_services else None,
            total_price=quote.total_price,
            staff_share=staff_share,
            shop_share=shop_share,
            status="ACCEPTED",
            opened_by=opener.id,
            created_at=to_iso(now_utc()),
            accepted_at=to_iso(now_utc()),
        )
        await reserve_quota_for_job(self.db, parent["customer_id"], quote, cycle)

        # ขยายเวลาจบของบิลเดิม และรีเซ็ตแจ้งเตือนก่อนจบงานให้คำนวณใหม่
        await self.db.update_job(
            parent_id,
            end_time=to_iso(new_end),
            duration_minutes=parent["duration_minutes"] + quote.duration_minutes,
            notified_end=0,
            review_sent=0,
        )

        payments = self.bot.get_cog("PaymentsCog")
        extend_job = await self.db.get_job(extend_id)
        await payments.start_job_payment(extend_job)
        for sid in staff_ids:
            await send_dm(
                self.bot,
                sid,
                embed=discord.Embed(
                    title="⏱️ ลูกค้าต่อเวลา",
                    description=(
                        f"บิล `#{parent_id}` ต่อเวลา +{quote.duration_minutes} นาที "
                        f"({self.cfg.service_names(service_keys)})\n"
                        f"เวลาจบใหม่: {discord_ts(new_end)}"
                    ),
                    color=COLOR_INFO,
                ),
            )
        return extend_id

    # ------------------------------------------------------- พนักงานรับงาน
    async def staff_accept(self, interaction: discord.Interaction, job_id: int) -> None:
        job = await self.db.get_job(job_id)
        if job is None:
            await interaction.response.send_message("ไม่พบบิลนี้ค่ะ", ephemeral=True)
            return
        if interaction.user.id != job["staff_id"]:
            await interaction.response.send_message("ปุ่มนี้สำหรับพนักงานที่ถูกจ่ายงานค่ะ", ephemeral=True)
            return
        if job["status"] != "PENDING_STAFF":
            await interaction.response.send_message("บิลนี้ถูกรับงานไปแล้วค่ะ", ephemeral=True)
            return

        await interaction.response.defer()
        await self.db.update_job(job_id, status="ACCEPTED", accepted_at=to_iso(now_utc()))
        job = await self.db.get_job(job_id)

        await interaction.edit_original_response(
            embed=job_embed(self.cfg, job, title="✅ รับงานแล้ว", color=COLOR_OK), view=None
        )

        payments = self.bot.get_cog("PaymentsCog")
        await payments.start_job_payment(job)
        await payments.notify_admin_text(
            f"✅ <@{job['staff_id']}> รับงานบิล `#{job_id}` แล้ว — ส่งยอดชำระให้ลูกค้าเรียบร้อย"
        )

    async def staff_reject_prompt(self, interaction: discord.Interaction, job_id: int) -> None:
        job = await self.db.get_job(job_id)
        if job is None:
            await interaction.response.send_message("ไม่พบบิลนี้ค่ะ", ephemeral=True)
            return
        if interaction.user.id != job["staff_id"]:
            await interaction.response.send_message("ปุ่มนี้สำหรับพนักงานที่ถูกจ่ายงานค่ะ", ephemeral=True)
            return
        if job["status"] != "PENDING_STAFF":
            await interaction.response.send_message("บิลนี้ถูกดำเนินการไปแล้วค่ะ", ephemeral=True)
            return
        await interaction.response.send_modal(JobRejectReasonModal(job_id))

    async def staff_reject(self, interaction: discord.Interaction, job_id: int, reason: str) -> None:
        job = await self.db.get_job(job_id)
        if job is None or interaction.user.id != job["staff_id"] or job["status"] != "PENDING_STAFF":
            await interaction.response.send_message("บิลนี้ถูกดำเนินการไปแล้วค่ะ", ephemeral=True)
            return

        await interaction.response.defer()
        payments = self.bot.get_cog("PaymentsCog")
        await payments.reject_job_by_staff(job, interaction.user, reason)

        job = await self.db.get_job(job_id)
        embed = job_embed(self.cfg, job, title="❌ ปฏิเสธงานแล้ว", color=COLOR_DANGER)
        if reason:
            embed.add_field(name="เหตุผล", value=reason, inline=False)
        await interaction.edit_original_response(embed=embed, view=None)

    # ------------------------------------------------------ คำสั่ง slash
    panel_group = app_commands.Group(name="panel", description="โพสต์แผงควบคุมของบอท")

    @panel_group.command(name="reception", description="โพสต์แผงควบคุมรีเซปชั่น (สำหรับแอดมิน)")
    async def panel_reception(self, interaction: discord.Interaction) -> None:
        if not self._admin_guard(interaction):
            await interaction.response.send_message("เฉพาะแอดมินเท่านั้นค่ะ", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        removed = await purge_old_panels(interaction.channel, self.bot.user.id, "olp:panel:")

        embed = discord.Embed(
            title="🎛️ Reception Control Panel",
            description=(
                f"แผงควบคุมสำหรับแอดมิน / พนักงานต้อนรับ · {self.cfg.shop_name}\n\n"
                "🧾 **เปิดบิลใหม่** — เลือกลูกค้า พนักงาน บริการ ห้อง แล้วคำนวณราคาอัตโนมัติ\n"
                "　• Party Room เลือกพนักงานได้หลายคน (เกินที่รวมในราคาคิดเพิ่มต่อคน)\n"
                "　• Drink Friend กรอกจำนวน shot ตอนยืนยัน\n"
                "　• Erotic Service เลือกคู่กับ Short Date / Bed Room / Karaoke (บวกเวลาให้อัตโนมัติ)\n"
                "⏱️ **ต่อเวลา / เพิ่มรอบ** — ต่อ Short Date หรือเพิ่มรอบห้อง (+Erotic ได้) ขยายเวลาจบของบิลเดิม\n"
                "📋 **งานที่กำลังดำเนินอยู่** — ดูงานที่ยังไม่จบเวลา"
            ),
            color=COLOR_MAIN,
        )
        await interaction.channel.send(embed=embed, view=ReceptionPanel())

        note = f" (ลบแผงเก่าออก {removed} อัน)" if removed else ""
        await interaction.followup.send(f"โพสต์แผงควบคุมแล้วค่ะ{note}", ephemeral=True)

    bill_group = app_commands.Group(name="bill", description="จัดการบิล")

    @bill_group.command(name="info", description="ดูรายละเอียดบิลตามเลขที่")
    @app_commands.describe(job_id="เลขที่บิล")
    async def bill_info(self, interaction: discord.Interaction, job_id: int) -> None:
        if not self._admin_guard(interaction):
            await interaction.response.send_message("เฉพาะแอดมินเท่านั้นค่ะ", ephemeral=True)
            return
        job = await self.db.get_job(job_id)
        if job is None:
            await interaction.response.send_message("ไม่พบบิลนี้ค่ะ", ephemeral=True)
            return
        await interaction.response.send_message(
            embed=job_embed(self.cfg, job, title=f"🧾 บิล #{job_id}"), ephemeral=True
        )

    @bill_group.command(name="cancel", description="ยกเลิกบิล (ไม่บันทึกลง Google Sheets)")
    @app_commands.describe(job_id="เลขที่บิล", reason="เหตุผล (ถ้ามี)")
    async def bill_cancel(
        self, interaction: discord.Interaction, job_id: int, reason: str | None = None
    ) -> None:
        if not self._admin_guard(interaction):
            await interaction.response.send_message("เฉพาะแอดมินเท่านั้นค่ะ", ephemeral=True)
            return
        payments = self.bot.get_cog("PaymentsCog")
        ok, msg = await payments.cancel_job(job_id, interaction.user, reason)
        await interaction.response.send_message(
            embed=discord.Embed(description=msg, color=COLOR_OK if ok else COLOR_DANGER),
            ephemeral=True,
        )

    @bill_group.command(name="paid", description="ทำเครื่องหมายว่าชำระเงินแล้วด้วยมือ")
    @app_commands.describe(job_id="เลขที่บิล")
    async def bill_paid(self, interaction: discord.Interaction, job_id: int) -> None:
        if not self._admin_guard(interaction):
            await interaction.response.send_message("เฉพาะแอดมินเท่านั้นค่ะ", ephemeral=True)
            return
        payments = self.bot.get_cog("PaymentsCog")
        ok, msg = await payments.mark_job_paid(job_id, interaction.user)
        await interaction.response.send_message(
            embed=discord.Embed(description=msg, color=COLOR_OK if ok else COLOR_DANGER),
            ephemeral=True,
        )


async def setup(bot: commands.Bot) -> None:
    bot.add_dynamic_items(JobAcceptButton, JobRejectButton)
    bot.add_view(ReceptionPanel())
    await bot.add_cog(ReceptionCog(bot))
