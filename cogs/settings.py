"""เมนูตั้งค่าร้านใน Discord (เปิดจากปุ่ม ⚙️ ตั้งค่าร้าน ใน /panel_admin)

แก้ห้อง / บริการ & ราคา / ส่วนแบ่ง / การชำระเงิน / ค่าอื่นๆ แล้วบันทึกลง config.json ทันที
(ไม่ต้องรีสตาร์ตบอท) — ทุกการแก้ไขแจ้งเข้าห้องแอดมิน (และห้อง log ถ้าตั้งไว้)
"""
from __future__ import annotations

import asyncio
import logging
import time

import discord
from discord.ext import commands

from core.embeds import COLOR_DANGER, COLOR_INFO, COLOR_MAIN, COLOR_OK
from core.utils import is_admin

log = logging.getLogger("olp.settings")

NOT_ADMIN = "เฉพาะแอดมินเท่านั้นค่ะ"
NO_CATEGORY = "-"


# ---------------------------------------------------------------- helpers
def _is_admin(interaction: discord.Interaction) -> bool:
    return is_admin(interaction.user, interaction.client.cfg.admin_role_id)


def _num(raw: str, *, lo: float, hi: float, name: str, allow_blank: bool = False) -> float | None:
    """แปลงข้อความเป็นตัวเลข (โยน ValueError พร้อมข้อความภาษาไทยถ้าไม่ถูกต้อง)"""
    text = str(raw or "").replace(",", "").strip()
    if not text:
        if allow_blank:
            return None
        raise ValueError(f"กรุณาใส่ **{name}**")
    try:
        value = float(text)
    except ValueError as exc:
        raise ValueError(f"**{name}** ต้องเป็นตัวเลข") from exc
    if not lo <= value <= hi:
        raise ValueError(f"**{name}** ต้องอยู่ระหว่าง {lo:g} ถึง {hi:g}")
    return int(value) if value == int(value) else value


def _new_key(prefix: str, existing: set[str]) -> str:
    base = f"{prefix}_{int(time.time()) % 100000}"
    key, n = base, 1
    while key in existing:
        key, n = f"{base}_{n}", n + 1
    return key


async def log_change(bot: commands.Bot, user: discord.abc.User, text: str) -> None:
    """บันทึกการแก้ตั้งค่าเข้าห้องแอดมิน (+ ห้อง log ถ้าตั้ง channels.log)

    ส่งแบบเบื้องหลัง ไม่รอ — ผู้เรียกต้องตอบ Discord ภายใน 3 วินาที
    """
    task = asyncio.create_task(_send_change_log(bot, user, text))
    _pending_logs.add(task)  # เก็บอ้างอิงไว้ กัน task ถูกเก็บขยะก่อนส่งเสร็จ
    task.add_done_callback(_on_log_done)


_pending_logs: set[asyncio.Task] = set()


def _on_log_done(task: asyncio.Task) -> None:
    _pending_logs.discard(task)
    if not task.cancelled() and task.exception() is not None:
        log.warning("ส่งบันทึกการตั้งค่าไม่สำเร็จ: %s", task.exception())


async def _send_change_log(bot: commands.Bot, user: discord.abc.User, text: str) -> None:
    embed = discord.Embed(description=f"⚙️ {user.mention} {text}", color=COLOR_INFO)
    payments = bot.get_cog("PaymentsCog")
    if payments is not None:
        await payments.notify_admin(embed=embed)
    log_channel = bot.get_channel(bot.cfg.channel_id("log"))
    if log_channel is not None and log_channel.id != bot.cfg.channel_id("admin"):
        await log_channel.send(embed=embed)
    log.info("ตั้งค่าร้าน: %s %s", user, text)


def _save(interaction: discord.Interaction) -> None:
    interaction.client.cfg.save()


def _category_options(cfg, current: str | None) -> list[discord.SelectOption]:
    opts = [
        discord.SelectOption(label=o["label"], value=o["key"], emoji=o.get("emoji"), default=o["key"] == current)
        for o in (cfg.get("attendance.accept_options") or [])
    ]
    opts.append(discord.SelectOption(label="ไม่ระบุหมวด", value=NO_CATEGORY, default=not current))
    return opts


class AdminView(discord.ui.View):
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if _is_admin(interaction):
            return True
        await interaction.response.send_message(NOT_ADMIN, ephemeral=True)
        return False


class BackButton(discord.ui.Button):
    def __init__(self, row: int = 4) -> None:
        super().__init__(label="กลับ", emoji="⬅️", style=discord.ButtonStyle.secondary, row=row)

    async def callback(self, interaction: discord.Interaction) -> None:
        home = SettingsHome()
        await interaction.response.edit_message(embed=home.embed(interaction.client.cfg), view=home)


# ===================================================================== หน้าแรก
class SettingsHome(AdminView):
    def __init__(self) -> None:
        super().__init__(timeout=600)

    @staticmethod
    def embed(cfg) -> discord.Embed:
        return discord.Embed(
            title="⚙️ ตั้งค่าร้าน",
            description=(
                "เลือกหมวดที่ต้องการแก้ไข — บันทึกลง `config.json` และใช้ได้ทันที ไม่ต้องรีสตาร์ต\n\n"
                f"🚪 **ห้อง** — {len(cfg.rooms)} ห้อง\n"
                f"🛎️ **บริการ & ราคา** — {len([s for s in cfg.services if not s.get('hidden')])} รายการ\n"
                f"💰 **ส่วนแบ่งพนักงาน** — ค่าเริ่มต้น {cfg.get('revenue_share.default_staff_percent', 60)}%\n"
                "💳 **การชำระเงิน** — พร้อมเพย์ / QR\n"
                "🔧 **อื่นๆ** — โดเนท, Top Donate, บิลค้าง, เวลาตัดยอด\n\n"
                "*ทุกการแก้ไขจะแจ้งเข้าห้องแอดมิน · เพิ่ม/ลบบริการแล้ว แผงเปิดบิลจะอัปเดตเองตอนกดครั้งถัดไป*"
            ),
            color=COLOR_MAIN,
        )

    @discord.ui.select(
        placeholder="เลือกหมวดที่จะแก้ไข",
        options=[
            discord.SelectOption(label="ห้อง", value="rooms", emoji="🚪"),
            discord.SelectOption(label="บริการ & ราคา", value="services", emoji="🛎️"),
            discord.SelectOption(label="ส่วนแบ่งพนักงาน", value="share", emoji="💰"),
            discord.SelectOption(label="การชำระเงิน", value="payment", emoji="💳"),
            discord.SelectOption(label="อื่นๆ", value="other", emoji="🔧"),
        ],
    )
    async def pick(self, interaction: discord.Interaction, select: discord.ui.Select) -> None:
        cfg = interaction.client.cfg
        choice = select.values[0]
        if choice == "rooms":
            view = RoomsView(cfg)
            await interaction.response.edit_message(embed=view.embed(), view=view)
        elif choice == "services":
            view = ServicesView(cfg)
            await interaction.response.edit_message(embed=view.embed(), view=view)
        elif choice == "share":
            view = ShareView(cfg)
            await interaction.response.edit_message(embed=view.embed(), view=view)
        elif choice == "payment":
            await interaction.response.send_modal(PaymentModal(cfg))
        else:
            await interaction.response.send_modal(OtherModal(cfg))


# ======================================================================= ห้อง
class RoomsView(AdminView):
    def __init__(self, cfg, room_key: str | None = None) -> None:
        super().__init__(timeout=600)
        self.cfg = cfg
        self.room_key = room_key

        rooms = cfg.rooms
        self.room_select = discord.ui.Select(
            placeholder="เลือกห้องที่จะแก้ไข",
            row=0,
            options=[
                discord.SelectOption(label=r["name"][:100], value=r["key"], default=r["key"] == room_key)
                for r in rooms[:25]
            ]
            or [discord.SelectOption(label="ยังไม่มีห้อง — กด ➕ เพิ่มห้อง", value="-")],
        )
        self.room_select.callback = self._on_room
        self.add_item(self.room_select)

        room_services = [s for s in cfg.services if s.get("require_room") and not s.get("extend_only")]
        room = self._room()
        allowed = set((room or {}).get("services") or [])
        self.service_select = discord.ui.Select(
            placeholder="บริการที่ใช้ห้องนี้ได้ (ไม่เลือก = ใช้ได้ทุกบริการ)",
            row=1,
            min_values=0,
            max_values=max(1, len(room_services[:25])),
            disabled=room is None or not room_services,
            options=[
                discord.SelectOption(label=s["name"][:100], value=s["key"], emoji=s.get("emoji"), default=s["key"] in allowed)
                for s in room_services[:25]
            ]
            or [discord.SelectOption(label="ไม่มีบริการที่ต้องใช้ห้อง", value="-")],
        )
        self.service_select.callback = self._on_services
        self.add_item(self.service_select)
        self.add_item(BackButton())

    def _room(self) -> dict | None:
        return next((r for r in self.cfg.rooms if r["key"] == self.room_key), None)

    def embed(self) -> discord.Embed:
        embed = discord.Embed(title="🚪 ตั้งค่าห้อง", color=COLOR_MAIN)
        lines = []
        for r in self.cfg.rooms:
            used = ", ".join(self.cfg.service_name(k) for k in r.get("services") or []) or "ทุกบริการที่ต้องใช้ห้อง"
            mark = "▶️ " if r["key"] == self.room_key else "• "
            lines.append(f"{mark}**{r['name']}** — {used}")
        embed.description = "\n".join(lines) or "*ยังไม่มีห้อง*"
        embed.set_footer(text="เลือกห้อง → แก้ชื่อ / เลือกบริการที่ใช้ได้ / ลบ · หรือกด ➕ เพิ่มห้อง")
        return embed

    async def _rerender(self, interaction: discord.Interaction) -> None:
        view = RoomsView(self.cfg, self.room_key)
        await interaction.response.edit_message(embed=view.embed(), view=view)

    async def _on_room(self, interaction: discord.Interaction) -> None:
        value = self.room_select.values[0]
        self.room_key = None if value == "-" else value
        await self._rerender(interaction)

    async def _on_services(self, interaction: discord.Interaction) -> None:
        room = self._room()
        if room is None:
            await interaction.response.defer()
            return
        room["services"] = [v for v in self.service_select.values if v != "-"]
        _save(interaction)
        used = self.cfg.service_names(room["services"]) if room["services"] else "ทุกบริการที่ต้องใช้ห้อง"
        await log_change(interaction.client, interaction.user, f"ตั้งห้อง **{room['name']}** ให้ใช้กับ: {used}")
        await self._rerender(interaction)

    @discord.ui.button(label="เพิ่มห้อง", emoji="➕", style=discord.ButtonStyle.success, row=2)
    async def add(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_modal(RoomNameModal(self, None))

    @discord.ui.button(label="แก้ชื่อห้อง", emoji="✏️", style=discord.ButtonStyle.primary, row=2)
    async def rename(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        room = self._room()
        if room is None:
            await interaction.response.send_message("เลือกห้องก่อนค่ะ", ephemeral=True)
            return
        await interaction.response.send_modal(RoomNameModal(self, room))

    @discord.ui.button(label="ลบห้อง", emoji="🗑️", style=discord.ButtonStyle.danger, row=2)
    async def delete(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        room = self._room()
        if room is None:
            await interaction.response.send_message("เลือกห้องก่อนค่ะ", ephemeral=True)
            return

        async def do_delete(i: discord.Interaction) -> None:
            self.cfg.data["rooms"] = [r for r in self.cfg.rooms if r["key"] != room["key"]]
            _save(i)
            await log_change(i.client, i.user, f"ลบห้อง **{room['name']}**")
            view = RoomsView(self.cfg)
            await i.response.edit_message(embed=view.embed(), view=view)

        await interaction.response.edit_message(
            embed=discord.Embed(
                title=f"🗑️ ลบห้อง {room['name']}?",
                description="บิลเก่าที่ใช้ห้องนี้ยังอยู่ แต่จะแสดงเป็นรหัสห้องแทนชื่อ",
                color=COLOR_DANGER,
            ),
            view=ConfirmView(do_delete, back=lambda: RoomsView(self.cfg, self.room_key)),
        )


class RoomNameModal(discord.ui.Modal):
    name = discord.ui.TextInput(label="ชื่อห้อง", max_length=80)

    def __init__(self, view: RoomsView, room: dict | None) -> None:
        super().__init__(title="แก้ชื่อห้อง" if room else "เพิ่มห้องใหม่")
        self.view = view
        self.room = room
        if room:
            self.name.default = room["name"]

    async def on_submit(self, interaction: discord.Interaction) -> None:
        cfg = self.view.cfg
        name = str(self.name.value).strip()
        if self.room:
            old = self.room["name"]
            self.room["name"] = name
            text = f"เปลี่ยนชื่อห้อง **{old}** → **{name}**"
        else:
            key = _new_key("room", {r["key"] for r in cfg.rooms})
            cfg.data.setdefault("rooms", []).append({"key": key, "name": name})
            self.view.room_key = key
            text = f"เพิ่มห้อง **{name}** (ใช้ได้ทุกบริการที่ต้องใช้ห้อง — เลือกจำกัดบริการได้ในเมนูห้อง)"
        _save(interaction)
        await log_change(interaction.client, interaction.user, text)
        view = RoomsView(cfg, self.view.room_key)
        await interaction.response.edit_message(embed=view.embed(), view=view)


# ==================================================================== บริการ
SERVICE_TYPES = {
    "normal": ("บริการปกติ", "ไม่ต้องใช้ห้อง"),
    "room": ("ต้องใช้ห้อง", "บังคับเลือกห้องตอนเปิดบิล"),
    "extend": ("แพ็กเกจต่อเวลา", "ใช้ในเมนูต่อเวลา/เพิ่มรอบ"),
    "unit": ("คิดต่อหน่วย", "เช่น shot — กรอกจำนวนตอนเปิดบิล"),
}


def service_type(svc: dict) -> str:
    if svc.get("extend_only"):
        return "extend"
    if svc.get("per_unit"):
        return "unit"
    if svc.get("require_room"):
        return "room"
    return "normal"


def service_line(cfg, svc: dict) -> str:
    price = svc.get("pricing", {}).get("normal", 0)
    pct = svc.get("staff_percent")
    share = f"{pct}%" if pct is not None else f"{cfg.get('revenue_share.default_staff_percent', 60)}% (ค่าเริ่มต้น)"
    if svc.get("per_unit"):
        head = f"{price:,.0f} บาท/{svc.get('unit_label', 'หน่วย')}"
    elif svc.get("addon_for"):
        head = f"+{price:,.0f} บาท (บริการเสริม)"
    else:
        head = f"{price:,.0f} บาท / {svc.get('duration_minutes', 0)} นาที"
    return f"{head} · พนักงานได้ {share}"


class ServicesView(AdminView):
    def __init__(self, cfg, service_key: str | None = None) -> None:
        super().__init__(timeout=600)
        self.cfg = cfg
        self.service_key = service_key
        services = [s for s in cfg.services if not s.get("hidden")]

        self.service_select = discord.ui.Select(
            placeholder="เลือกบริการที่จะแก้ไข",
            row=0,
            options=[
                discord.SelectOption(
                    label=s["name"][:100],
                    value=s["key"],
                    emoji=s.get("emoji") or None,
                    description=service_line(cfg, s)[:100],
                    default=s["key"] == service_key,
                )
                for s in services[:25]
            ]
            or [discord.SelectOption(label="ยังไม่มีบริการ", value="-")],
        )
        self.service_select.callback = self._on_service
        self.add_item(self.service_select)

        svc = self._svc()
        self.category_select = discord.ui.Select(
            placeholder="หมวดงาน (ใช้จับคู่กับงานที่พนักงานรับตอนเข้างาน)",
            row=1,
            disabled=svc is None,
            options=_category_options(cfg, (svc or {}).get("category")),
        )
        self.category_select.callback = self._on_category
        self.add_item(self.category_select)
        self.add_item(BackButton())

    def _svc(self) -> dict | None:
        return self.cfg.service(self.service_key) if self.service_key else None

    def embed(self) -> discord.Embed:
        svc = self._svc()
        if svc is None:
            embed = discord.Embed(title="🛎️ บริการ & ราคา", color=COLOR_MAIN)
            embed.description = "\n".join(
                f"{s.get('emoji', '')} **{s['name']}** — {service_line(self.cfg, s)}"
                for s in self.cfg.services
                if not s.get("hidden")
            )[:4000] or "*ยังไม่มีบริการ*"
            embed.set_footer(text="เลือกบริการเพื่อแก้ไข/ลบ · หรือกด ➕ เพิ่มบริการ")
            return embed

        embed = discord.Embed(title=f"{svc.get('emoji', '')} {svc['name']}", color=COLOR_MAIN)
        embed.add_field(name="ราคา / ส่วนแบ่ง", value=service_line(self.cfg, svc), inline=False)
        embed.add_field(name="ประเภท", value=SERVICE_TYPES[service_type(svc)][0], inline=True)
        cat = svc.get("category")
        cat_label = next((o["label"] for o in self.cfg.get("attendance.accept_options") or [] if o["key"] == cat), "ไม่ระบุ")
        embed.add_field(name="หมวดงาน", value=cat_label, inline=True)
        if svc.get("multi_staff"):
            embed.add_field(
                name="หลายพนักงาน",
                value=(
                    f"รวม {svc.get('included_staff', 1)} คน · เพิ่มคนละ {svc.get('extra_staff_price', 0):,.0f} บาท "
                    f"(พนักงานได้ {svc.get('extra_staff_percent', 100):g}%) · สูงสุด {svc.get('max_staff', 10)} คน"
                ),
                inline=False,
            )
        if int(svc.get("max_customers", 1)) > 1:
            embed.add_field(
                name="ลูกค้าหลายคน",
                value=(
                    f"สูงสุด {svc['max_customers']} คน · เพิ่มคนละ {svc.get('extra_customer_price', 0):,.0f} บาท "
                    f"(พนักงานได้ {svc.get('extra_customer_percent', 100):g}%)"
                    + (" · 👥 ต้องให้พนักงานยินยอมตอนเข้างาน" if svc.get("group_consent") else "")
                ),
                inline=False,
            )
        if svc.get("description"):
            embed.add_field(name="คำอธิบาย (แสดงในเมนูลูกค้า)", value=svc["description"][:1024], inline=False)
        embed.set_footer(text=f"รหัส: {svc['key']}")
        return embed

    async def _rerender(self, interaction: discord.Interaction, key: str | None = None) -> None:
        view = ServicesView(self.cfg, key if key is not None else self.service_key)
        await interaction.response.edit_message(embed=view.embed(), view=view)

    async def _on_service(self, interaction: discord.Interaction) -> None:
        value = self.service_select.values[0]
        self.service_key = None if value == "-" else value
        await self._rerender(interaction)

    async def _on_category(self, interaction: discord.Interaction) -> None:
        svc = self._svc()
        if svc is None:
            await interaction.response.defer()
            return
        value = self.category_select.values[0]
        if value == NO_CATEGORY:
            svc.pop("category", None)
        else:
            svc["category"] = value
        _save(interaction)
        await log_change(interaction.client, interaction.user, f"ตั้งหมวดงานของ **{svc['name']}** เป็น `{value}`")
        await self._rerender(interaction)

    @discord.ui.button(label="แก้ไข", emoji="✏️", style=discord.ButtonStyle.primary, row=2)
    async def edit(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        svc = self._svc()
        if svc is None:
            await interaction.response.send_message("เลือกบริการก่อนค่ะ", ephemeral=True)
            return
        await interaction.response.send_modal(ServiceModal(self, svc))

    @discord.ui.button(label="รายละเอียดเพิ่มเติม", emoji="📝", style=discord.ButtonStyle.secondary, row=2)
    async def extra(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        svc = self._svc()
        if svc is None:
            await interaction.response.send_message("เลือกบริการก่อนค่ะ", ephemeral=True)
            return
        await interaction.response.send_modal(ServiceExtraModal(self, svc))

    @discord.ui.button(label="ลูกค้าหลายคน", emoji="👤", style=discord.ButtonStyle.secondary, row=2)
    async def customers(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        svc = self._svc()
        if svc is None:
            await interaction.response.send_message("เลือกบริการก่อนค่ะ", ephemeral=True)
            return
        if svc.get("per_unit"):
            await interaction.response.send_message("บริการคิดต่อหน่วยไม่จำกัดจำนวนลูกค้าอยู่แล้วค่ะ", ephemeral=True)
            return
        await interaction.response.send_modal(CustomerModal(self, svc))

    @discord.ui.button(label="เพิ่มบริการ", emoji="➕", style=discord.ButtonStyle.success, row=3)
    async def add(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        view = AddServiceView(self.cfg)
        await interaction.response.edit_message(embed=view.embed(), view=view)

    @discord.ui.button(label="ลบบริการ", emoji="🗑️", style=discord.ButtonStyle.danger, row=3)
    async def delete(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        svc = self._svc()
        if svc is None:
            await interaction.response.send_message("เลือกบริการก่อนค่ะ", ephemeral=True)
            return
        used_by = [s["name"] for s in self.cfg.services if svc["key"] in (s.get("addon_for") or {})]

        async def do_delete(i: discord.Interaction) -> None:
            self.cfg.data["services"] = [s for s in self.cfg.services if s["key"] != svc["key"]]
            for s in self.cfg.services:  # ถอดออกจากบริการเสริมและห้องที่อ้างถึง
                (s.get("addon_for") or {}).pop(svc["key"], None)
            for r in self.cfg.rooms:
                if svc["key"] in (r.get("services") or []):
                    r["services"].remove(svc["key"])
            _save(i)
            await log_change(i.client, i.user, f"ลบบริการ **{svc['name']}**")
            view = ServicesView(self.cfg)
            await i.response.edit_message(embed=view.embed(), view=view)

        note = "บิลเก่ายังอยู่ แต่จะแสดงเป็นรหัสบริการแทนชื่อ"
        if used_by:
            note += f"\n⚠️ บริการเสริมที่ใช้คู่กับบริการนี้จะถูกถอดออกด้วย: {', '.join(used_by)}"
        await interaction.response.edit_message(
            embed=discord.Embed(title=f"🗑️ ลบบริการ {svc['name']}?", description=note, color=COLOR_DANGER),
            view=ConfirmView(do_delete, back=lambda: ServicesView(self.cfg, self.service_key)),
        )


class ServiceModal(discord.ui.Modal):
    """แก้ (หรือสร้าง) ชื่อ / อีโมจิ / ราคา / เวลา / ส่วนแบ่ง"""

    name = discord.ui.TextInput(label="ชื่อบริการ", max_length=80)
    emoji = discord.ui.TextInput(label="อีโมจิ (ไม่บังคับ)", required=False, max_length=8)
    price = discord.ui.TextInput(label="ราคา (บาท)", max_length=8)
    duration = discord.ui.TextInput(label="เวลา (นาที)", max_length=4)
    staff_percent = discord.ui.TextInput(
        label="พนักงานได้กี่ % (ว่าง = ใช้ค่าเริ่มต้น)", required=False, max_length=5
    )

    def __init__(self, view: ServicesView | None, svc: dict | None, *, new_type: str = "normal", new_category: str | None = None) -> None:
        super().__init__(title="แก้ไขบริการ" if svc else "เพิ่มบริการใหม่")
        self.view = view
        self.svc = svc
        self.new_type = new_type
        self.new_category = new_category
        if svc:
            self.name.default = svc["name"]
            self.emoji.default = svc.get("emoji") or None
            self.price.default = f"{svc.get('pricing', {}).get('normal', 0):g}"
            self.duration.default = str(svc.get("duration_minutes", 0))
            pct = svc.get("staff_percent")
            self.staff_percent.default = f"{pct:g}" if pct is not None else None
        elif new_type == "unit":
            self.duration.default = "10"

    async def on_submit(self, interaction: discord.Interaction) -> None:
        cfg = interaction.client.cfg
        try:
            price = _num(self.price.value, lo=0, hi=1_000_000, name="ราคา")
            duration = int(_num(self.duration.value, lo=0, hi=1440, name="เวลา"))
            pct = _num(self.staff_percent.value, lo=0, hi=100, name="ส่วนแบ่ง %", allow_blank=True)
        except ValueError as exc:
            await interaction.response.send_message(f"⚠️ {exc}", ephemeral=True)
            return

        svc = self.svc
        if svc is None:
            svc = {"key": _new_key("svc", {s["key"] for s in cfg.services}), "pricing": {}}
            if self.new_type == "room":
                svc["require_room"] = True
            elif self.new_type == "extend":
                svc["extend_only"] = True
            elif self.new_type == "unit":
                svc.update(per_unit=True, unit_label="หน่วย")
            if self.new_category:
                svc["category"] = self.new_category
            cfg.data.setdefault("services", []).append(svc)
            action = "เพิ่มบริการ"
        else:
            action = "แก้บริการ"

        svc["name"] = str(self.name.value).strip()
        emoji = str(self.emoji.value).strip()
        if emoji:
            svc["emoji"] = emoji
        else:
            svc.pop("emoji", None)
        svc.setdefault("pricing", {})["normal"] = price
        svc["duration_minutes"] = duration
        if pct is None:
            svc.pop("staff_percent", None)
        else:
            svc["staff_percent"] = pct
        _save(interaction)
        await log_change(interaction.client, interaction.user, f"{action} **{svc['name']}** — {service_line(cfg, svc)}")

        view = ServicesView(cfg, svc["key"])
        await interaction.response.edit_message(embed=view.embed(), view=view)


class ServiceExtraModal(discord.ui.Modal):
    """คำอธิบาย + ค่าเฉพาะของบริการหลายพนักงาน/คิดต่อหน่วย"""

    def __init__(self, view: ServicesView, svc: dict) -> None:
        super().__init__(title="รายละเอียดเพิ่มเติม")
        self.view = view
        self.svc = svc
        self.description = discord.ui.TextInput(
            label="คำอธิบาย (แสดงในเมนูลูกค้า)",
            style=discord.TextStyle.paragraph,
            required=False,
            max_length=300,
            default=svc.get("description") or None,
        )
        self.add_item(self.description)
        self.fields: dict[str, discord.ui.TextInput] = {}
        if not svc.get("per_unit"):
            # พนักงานสูงสุด 1 = ปิดหลายพนักงาน · มากกว่า 1 = เปิด
            multi = bool(svc.get("multi_staff"))
            for key, label, default in (
                ("max_staff", "พนักงานสูงสุดต่อบิล (1 = คนเดียว)", svc.get("max_staff", 10) if multi else 1),
                ("included_staff", "จำนวนพนักงานที่รวมในราคาแล้ว", svc.get("included_staff", 1)),
                ("extra_staff_price", "ค่าพนักงานเพิ่ม (บาท/คน)", svc.get("extra_staff_price", 0)),
                ("extra_staff_percent", "พนักงานได้กี่ % ของค่าพนักงานเพิ่ม", svc.get("extra_staff_percent", 100)),
            ):
                self.fields[key] = discord.ui.TextInput(label=label, default=f"{default:g}", max_length=6)
                self.add_item(self.fields[key])
        if svc.get("per_unit"):
            self.fields["unit_label"] = discord.ui.TextInput(
                label="ชื่อหน่วย (เช่น shot)", default=svc.get("unit_label", "หน่วย"), max_length=20
            )
            self.add_item(self.fields["unit_label"])

    async def on_submit(self, interaction: discord.Interaction) -> None:
        svc = self.svc
        try:
            values = {}
            for key, field in self.fields.items():
                if key == "unit_label":
                    values[key] = str(field.value).strip() or "หน่วย"
                elif key == "extra_staff_price":
                    values[key] = _num(field.value, lo=0, hi=100_000, name="ค่าพนักงานเพิ่ม")
                elif key == "extra_staff_percent":
                    values[key] = _num(field.value, lo=0, hi=100, name="% ของค่าพนักงานเพิ่ม")
                else:
                    values[key] = int(_num(field.value, lo=1, hi=25, name="จำนวนพนักงาน"))
        except ValueError as exc:
            await interaction.response.send_message(f"⚠️ {exc}", ephemeral=True)
            return
        desc = str(self.description.value).strip()
        if desc:
            svc["description"] = desc
        else:
            svc.pop("description", None)
        if "max_staff" in values:
            if values["included_staff"] > values["max_staff"]:
                await interaction.response.send_message(
                    "⚠️ จำนวนที่รวมในราคาต้องไม่มากกว่าพนักงานสูงสุดค่ะ", ephemeral=True
                )
                return
            values["multi_staff"] = values["max_staff"] > 1
        svc.update(values)
        _save(interaction)
        note = ""
        if svc.get("multi_staff"):
            note = (
                f" — หลายพนักงาน สูงสุด {svc['max_staff']} คน · เพิ่มคนละ {svc['extra_staff_price']:,.0f} บาท "
                f"(พนักงานได้ {svc.get('extra_staff_percent', 100):g}%)"
            )
        await log_change(interaction.client, interaction.user, f"แก้รายละเอียดบริการ **{svc['name']}**{note}")
        view = ServicesView(self.view.cfg, svc["key"])
        await interaction.response.edit_message(embed=view.embed(), view=view)


class CustomerModal(discord.ui.Modal, title="ลูกค้าหลายคน"):
    """ตั้งจำนวนลูกค้าสูงสุด / ค่าลูกค้าเพิ่ม / ต้องให้พนักงานยินยอมหรือไม่"""

    max_customers = discord.ui.TextInput(label="ลูกค้าสูงสุดต่อบิล (1 = คนเดียว)", max_length=2)
    price = discord.ui.TextInput(label="ค่าลูกค้าเพิ่ม (บาท/คน, 0 = ฟรี)", max_length=8)
    percent = discord.ui.TextInput(label="พนักงานได้กี่ % ของค่าลูกค้าเพิ่ม", max_length=5)
    consent = discord.ui.TextInput(label="ต้องให้พนักงานติ๊กยินยอม? (ใช่/ไม่)", max_length=4)

    def __init__(self, view: ServicesView, svc: dict) -> None:
        super().__init__()
        self.view = view
        self.svc = svc
        self.max_customers.default = str(svc.get("max_customers", 1))
        self.price.default = f"{svc.get('extra_customer_price', 0):g}"
        self.percent.default = f"{svc.get('extra_customer_percent', 100):g}"
        self.consent.default = "ใช่" if svc.get("group_consent") else "ไม่"

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            mx = int(_num(self.max_customers.value, lo=1, hi=25, name="ลูกค้าสูงสุด"))
            price = _num(self.price.value, lo=0, hi=100_000, name="ค่าลูกค้าเพิ่ม")
            pct = _num(self.percent.value, lo=0, hi=100, name="% ของค่าลูกค้าเพิ่ม")
        except ValueError as exc:
            await interaction.response.send_message(f"⚠️ {exc}", ephemeral=True)
            return
        answer = str(self.consent.value).strip().lower()
        if answer not in ("ใช่", "ไม่", "yes", "no", "y", "n"):
            await interaction.response.send_message("⚠️ ช่องยินยอมให้ตอบ **ใช่** หรือ **ไม่** ค่ะ", ephemeral=True)
            return
        svc = self.svc
        svc.update(max_customers=mx, extra_customer_price=price, extra_customer_percent=pct)
        if answer in ("ใช่", "yes", "y"):
            svc["group_consent"] = True
        else:
            svc.pop("group_consent", None)
        _save(interaction)
        text = (
            f"ตั้งลูกค้าหลายคนของ **{svc['name']}** — สูงสุด {mx} คน · เพิ่มคนละ {price:,.0f} บาท "
            f"(พนักงานได้ {pct:g}%)" + (" · ต้องให้พนักงานยินยอม" if svc.get("group_consent") else "")
            if mx > 1
            else f"ตั้ง **{svc['name']}** ให้รับลูกค้าได้คนเดียว"
        )
        await log_change(interaction.client, interaction.user, text)
        view = ServicesView(self.view.cfg, svc["key"])
        await interaction.response.edit_message(embed=view.embed(), view=view)


class AddServiceView(AdminView):
    """ขั้นแรกของการเพิ่มบริการ: เลือกประเภทและหมวดงาน แล้วค่อยกรอกชื่อ/ราคา"""

    def __init__(self, cfg) -> None:
        super().__init__(timeout=600)
        self.cfg = cfg
        self.type_key = "normal"
        self.category: str | None = None

        self.type_select = discord.ui.Select(
            row=0,
            options=[
                discord.SelectOption(label=label, value=key, description=desc, default=key == "normal")
                for key, (label, desc) in SERVICE_TYPES.items()
            ],
        )
        self.type_select.callback = self._on_type
        self.add_item(self.type_select)

        self.category_select = discord.ui.Select(
            placeholder="หมวดงาน (ไม่บังคับ)", row=1, options=_category_options(cfg, None)
        )
        self.category_select.callback = self._on_category
        self.add_item(self.category_select)
        self.add_item(BackButton())

    @staticmethod
    def embed() -> discord.Embed:
        return discord.Embed(
            title="➕ เพิ่มบริการใหม่",
            description="1) เลือกประเภท  2) เลือกหมวดงาน (ไม่บังคับ)  3) กด **กรอกรายละเอียด**",
            color=COLOR_MAIN,
        )

    async def _on_type(self, interaction: discord.Interaction) -> None:
        self.type_key = self.type_select.values[0]
        for opt in self.type_select.options:
            opt.default = opt.value == self.type_key
        await interaction.response.defer()

    async def _on_category(self, interaction: discord.Interaction) -> None:
        value = self.category_select.values[0]
        self.category = None if value == NO_CATEGORY else value
        await interaction.response.defer()

    @discord.ui.button(label="กรอกรายละเอียด", emoji="📝", style=discord.ButtonStyle.success, row=2)
    async def next(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_modal(
            ServiceModal(None, None, new_type=self.type_key, new_category=self.category)
        )


# ================================================================== ส่วนแบ่ง
class ShareView(AdminView):
    def __init__(self, cfg) -> None:
        super().__init__(timeout=600)
        self.cfg = cfg
        self.member: discord.abc.User | None = None
        self.add_item(BackButton())

    def embed(self) -> discord.Embed:
        share = self.cfg.get("revenue_share", {}) or {}
        embed = discord.Embed(title="💰 ส่วนแบ่งพนักงาน", color=COLOR_MAIN)
        embed.add_field(
            name="ค่าเริ่มต้น",
            value=f"**{share.get('default_staff_percent', 60)}%** (ใช้กับบริการที่ไม่ได้ตั้ง % เอง)",
            inline=False,
        )
        fixed = [
            f"{s.get('emoji', '')} {s['name']} — {s['staff_percent']}%"
            for s in self.cfg.services
            if s.get("staff_percent") is not None and not s.get("hidden")
        ]
        embed.add_field(name="บริการที่ตั้ง % เอง", value="\n".join(fixed)[:1024] or "-", inline=False)
        per = share.get("staff_percent") or {}
        embed.add_field(
            name="ตั้งรายคน (ใช้กับบริการที่ไม่ได้ตั้ง % เอง)",
            value="\n".join(f"<@{uid}> — {pct}%" for uid, pct in per.items())[:1024] or "-",
            inline=False,
        )
        embed.set_footer(text="แก้ % ของแต่ละบริการได้ที่เมนู บริการ & ราคา")
        return embed

    @discord.ui.button(label="แก้ % เริ่มต้น", emoji="✏️", style=discord.ButtonStyle.primary, row=1)
    async def default(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_modal(PercentModal(self, None))

    @discord.ui.select(cls=discord.ui.UserSelect, placeholder="ตั้ง % รายคน: เลือกพนักงาน", row=0)
    async def pick_member(self, interaction: discord.Interaction, select: discord.ui.UserSelect) -> None:
        await interaction.response.send_modal(PercentModal(self, select.values[0]))


class PercentModal(discord.ui.Modal):
    percent = discord.ui.TextInput(label="พนักงานได้กี่ %", max_length=5)

    def __init__(self, view: ShareView, member: discord.abc.User | None) -> None:
        super().__init__(title=f"% ของ {member.display_name}"[:45] if member else "% เริ่มต้น")
        self.view = view
        self.member = member
        share = view.cfg.data.setdefault("revenue_share", {})
        if member:
            self.percent.label = "พนักงานได้กี่ % (ว่าง = ใช้ค่าเริ่มต้น)"
            self.percent.required = False
            current = (share.get("staff_percent") or {}).get(str(member.id))
        else:
            current = share.get("default_staff_percent", 60)
        self.percent.default = f"{current:g}" if current is not None else None

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            pct = _num(self.percent.value, lo=0, hi=100, name="%", allow_blank=self.member is not None)
        except ValueError as exc:
            await interaction.response.send_message(f"⚠️ {exc}", ephemeral=True)
            return
        share = self.view.cfg.data.setdefault("revenue_share", {})
        if self.member is None:
            share["default_staff_percent"] = pct
            text = f"ตั้งส่วนแบ่งเริ่มต้นเป็น **{pct}%**"
        else:
            per = share.setdefault("staff_percent", {})
            if pct is None:
                per.pop(str(self.member.id), None)
                text = f"ลบ % รายคนของ {self.member.mention} (กลับไปใช้ค่าเริ่มต้น)"
            else:
                per[str(self.member.id)] = pct
                text = f"ตั้งส่วนแบ่งของ {self.member.mention} เป็น **{pct}%**"
        _save(interaction)
        await log_change(interaction.client, interaction.user, text)
        view = ShareView(self.view.cfg)
        await interaction.response.edit_message(embed=view.embed(), view=view)


# ============================================================ ชำระเงิน / อื่นๆ
class PaymentModal(discord.ui.Modal, title="การชำระเงิน"):
    account = discord.ui.TextInput(label="ชื่อบัญชี", max_length=80)
    promptpay = discord.ui.TextInput(label="เลขพร้อมเพย์", max_length=30)
    qr = discord.ui.TextInput(label="ลิงก์รูป QR (ไม่บังคับ)", required=False, max_length=400)
    note = discord.ui.TextInput(label="ข้อความท้ายบิล", required=False, max_length=200)

    def __init__(self, cfg) -> None:
        super().__init__()
        pay = cfg.get("payment", {}) or {}
        self.account.default = pay.get("account_name") or None
        self.promptpay.default = pay.get("promptpay") or None
        self.qr.default = pay.get("qr_image_url") or None
        self.note.default = pay.get("note") or None

    async def on_submit(self, interaction: discord.Interaction) -> None:
        qr = str(self.qr.value).strip()
        if qr and not qr.startswith(("http://", "https://")):
            await interaction.response.send_message("⚠️ ลิงก์รูป QR ต้องขึ้นต้นด้วย https://", ephemeral=True)
            return
        cfg = interaction.client.cfg
        cfg.data["payment"] = {
            "account_name": str(self.account.value).strip(),
            "promptpay": str(self.promptpay.value).strip(),
            "qr_image_url": qr,
            "note": str(self.note.value).strip(),
        }
        _save(interaction)
        await log_change(interaction.client, interaction.user, "แก้ข้อมูลการชำระเงิน (พร้อมเพย์/QR)")
        await interaction.response.send_message(
            embed=discord.Embed(description="✅ บันทึกข้อมูลการชำระเงินแล้วค่ะ", color=COLOR_OK), ephemeral=True
        )


OTHER_FIELDS = [
    # (path ใน config, label, ค่าเริ่มต้น, ต่ำสุด, สูงสุด)
    ("donate.min_amount", "โดเนทขั้นต่ำ (บาท)", 20, 1, 1_000_000),
    ("donate.max_amount", "โดเนทสูงสุด (บาท)", 50000, 1, 10_000_000),
    ("top_donate.min_amount", "Top Donate ยอดขั้นต่ำ (บาท)", 1000, 0, 10_000_000),
    ("bill_timeout.payment_cancel_minutes", "ยกเลิกบิลที่ไม่จ่ายหลังกี่นาที", 45, 5, 1440),
    ("attendance.day_cutoff_hour", "ตัดยอดเข้างานกี่โมง (0-23)", 1, 0, 23),
]


class OtherModal(discord.ui.Modal, title="ตั้งค่าอื่นๆ"):
    def __init__(self, cfg) -> None:
        super().__init__()
        self.inputs: list[tuple[tuple, discord.ui.TextInput]] = []
        for spec in OTHER_FIELDS:
            path, label, default, _, _ = spec
            field = discord.ui.TextInput(label=label, default=f"{cfg.get(path, default):g}", max_length=9)
            self.inputs.append((spec, field))
            self.add_item(field)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        cfg = interaction.client.cfg
        try:
            values = {spec[0]: _num(f.value, lo=spec[3], hi=spec[4], name=spec[1]) for spec, f in self.inputs}
        except ValueError as exc:
            await interaction.response.send_message(f"⚠️ {exc}", ephemeral=True)
            return
        if values["donate.min_amount"] > values["donate.max_amount"]:
            await interaction.response.send_message("⚠️ โดเนทขั้นต่ำต้องไม่มากกว่ายอดสูงสุดค่ะ", ephemeral=True)
            return
        values["attendance.day_cutoff_hour"] = int(values["attendance.day_cutoff_hour"])

        changed = []
        for path, value in values.items():
            section, key = path.split(".")
            node = cfg.data.setdefault(section, {})
            if node.get(key) != value:
                changed.append(f"{dict((s[0], s[1]) for s in OTHER_FIELDS)[path]}: {node.get(key)} → {value}")
                node[key] = value
        if not changed:
            await interaction.response.send_message("ไม่มีค่าที่เปลี่ยนค่ะ", ephemeral=True)
            return
        _save(interaction)
        await log_change(interaction.client, interaction.user, "แก้ตั้งค่าอื่นๆ\n" + "\n".join(f"• {c}" for c in changed))
        await interaction.response.send_message(
            embed=discord.Embed(description="✅ บันทึกแล้ว\n" + "\n".join(f"• {c}" for c in changed), color=COLOR_OK),
            ephemeral=True,
        )


# ==================================================================== ยืนยัน
class ConfirmView(AdminView):
    def __init__(self, on_confirm, back) -> None:
        super().__init__(timeout=120)
        self.on_confirm = on_confirm
        self.back = back

    @discord.ui.button(label="ยืนยันลบ", emoji="🗑️", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.stop()
        await self.on_confirm(interaction)

    @discord.ui.button(label="ยกเลิก", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.stop()
        view = self.back()
        await interaction.response.edit_message(embed=view.embed(), view=view)


class SettingsCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def open_settings(self, interaction: discord.Interaction) -> None:
        if not _is_admin(interaction):
            await interaction.response.send_message(NOT_ADMIN, ephemeral=True)
            return
        home = SettingsHome()
        await interaction.response.send_message(embed=home.embed(self.bot.cfg), view=home, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(SettingsCog(bot))
