"""เมนูตั้งค่าร้านใน Discord (เปิดจากปุ่ม ⚙️ ตั้งค่าร้าน ใน /panel_admin)

แก้ห้อง / บริการ & ราคา / ส่วนแบ่ง / การชำระเงิน / ค่าอื่นๆ แล้วบันทึกลง config.json ทันที
(ไม่ต้องรีสตาร์ตบอท) — ทุกการแก้ไขแจ้งเข้าห้องแอดมิน (และห้อง log ถ้าตั้งไว้)
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import time

import discord
from discord.ext import commands

from core.embeds import COLOR_DANGER, COLOR_GOLD, COLOR_INFO, COLOR_MAIN, COLOR_OK, panel_embed, rows_text
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
    """บันทึกการแก้ตั้งค่าเข้าห้อง Log (ถ้าตั้ง channels.log) ไม่งั้นเข้าห้องแอดมิน

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
    # ตั้งห้อง Log ไว้ = ส่งไปห้อง Log อย่างเดียว (ห้องแอดมินไม่รก) · ไม่ตั้ง = ห้องแอดมิน
    payments = bot.get_cog("PaymentsCog")
    if payments is not None:
        await payments.notify_admin(embed=embed, topic="log")
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


def _setup_status(cfg) -> str:
    missing = sum(1 for _, _, _, p, _, _ in BOT_SLOTS if not _slot_ids(cfg, p) and p not in OPTIONAL_SLOTS)
    return "✅ ครบ" if not missing else f"⚠️ ขาด {missing}"


# ===================================================================== หน้าแรก
class SettingsHome(AdminView):
    def __init__(self) -> None:
        super().__init__(timeout=600)

    @staticmethod
    def embed(cfg) -> discord.Embed:
        services = len([s for s in cfg.services if not s.get("hidden")])
        return panel_embed(
            "⚙️ ตั้งค่าร้าน",
            "เลือกหมวดจากเมนูด้านล่าง บันทึกลง `config.json` และใช้ได้ทันที ไม่ต้องรีสตาร์ต",
            [
                ("🏪 ร้าน & บริการ", [
                    (f"🚪 ห้อง · {len(cfg.rooms)} ห้อง", "ชื่อห้อง และบริการที่ใช้ห้องได้"),
                    (f"🛎️ บริการ & ราคา · {services} รายการ", "ราคา · เวลา · หลายพนักงาน/ลูกค้า · หมวดงาน"),
                    (
                        f"💰 ส่วนแบ่งพนักงาน · {cfg.get('revenue_share.default_staff_percent', 60)}%",
                        "ค่าเริ่มต้น และตั้งรายคน",
                    ),
                ]),
                ("💳 เงิน & ระบบ", [
                    ("💳 การชำระเงิน", "พร้อมเพย์ · ชื่อบัญชี · รูป QR"),
                    ("🔧 อื่นๆ", "โดเนท · Top Donate · บิลค้าง · เวลาตัดยอด"),
                ]),
                ("🧭 ระบบบอท", [
                    (
                        f"🧭 ห้อง · Role · เวลา · ตัดรอบ · {_setup_status(cfg)}",
                        "ห้องแอดมิน/รีวิว/ประกาศ · Role ต่างๆ · เวลาแจ้งเตือน · บิลค้าง · Google Sheets",
                    ),
                ]),
                ("👥 สมาชิก & สิทธิ์", [
                    (f"💎 VIP · {'🟢 เปิด' if cfg.vip_enabled else '⚪ ปิด'}", "ราคา/อายุแพ็กเกจ · Role · สิทธิ์"),
                    (f"🔑 Role รีเซปชั่น · {len(cfg.reception_role_ids)} Role", "ใช้แผง /panel reception ได้โดยไม่ต้องเป็นแอดมิน"),
                ]),
            ],
            footer="ทุกการแก้ไขแจ้งเข้าห้องแอดมิน · เพิ่ม/ลบบริการแล้ว เมนูเปิดบิลอัปเดตเองตอนกดครั้งถัดไป",
        )

    @discord.ui.select(
        placeholder="เลือกหมวดที่จะแก้ไข",
        options=[
            discord.SelectOption(label="ห้อง", value="rooms", emoji="🚪"),
            discord.SelectOption(label="บริการ & ราคา", value="services", emoji="🛎️"),
            discord.SelectOption(label="ส่วนแบ่งพนักงาน", value="share", emoji="💰"),
            discord.SelectOption(label="การชำระเงิน", value="payment", emoji="💳"),
            discord.SelectOption(label="อื่นๆ", value="other", emoji="🔧"),
            discord.SelectOption(label="VIP", value="vip", emoji="💎"),
            discord.SelectOption(label="Role รีเซปชั่น", value="reception", emoji="🔑"),
            discord.SelectOption(label="ระบบบอท (ห้อง · Role · เวลา · ตัดรอบ · Sheets)", value="bot", emoji="🧭"),
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
        elif choice == "bot":
            if not cfg.guild_id and interaction.guild_id:
                # ยังไม่ได้ใส่ guild_id — ใช้เซิร์ฟเวอร์นี้เลย (คำสั่ง slash จะซิงก์เร็วขึ้นหลังรีสตาร์ตครั้งถัดไป)
                cfg.data["guild_id"] = interaction.guild_id
                _save(interaction)
            view = BotSetupView(cfg, sheets_ready=interaction.client.sheets.ready)
            await interaction.response.edit_message(embed=view.embed(), view=view)
        elif choice == "reception":
            await interaction.response.send_modal(ReceptionRoleModal(cfg))
        elif choice == "vip":
            view = VipSettingsView(cfg)
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
            or [discord.SelectOption(label="ยังไม่มีห้อง กด ➕ เพิ่มห้อง", value="-")],
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
        embed = discord.Embed(title=f"🚪 ตั้งค่าห้อง · {len(self.cfg.rooms)} ห้อง", color=COLOR_MAIN)
        lines = []
        for r in self.cfg.rooms:
            used = ", ".join(self.cfg.service_name(k) for k in r.get("services") or []) or "ทุกบริการที่ต้องใช้ห้อง"
            mark = "▶️" if r["key"] == self.room_key else "🚪"
            lines.append(f"{mark} **{r['name']}**\n┗ {used}")
        embed.description = "\n".join(lines)[:4000] or "*ยังไม่มีห้อง กด ➕ เพิ่มห้อง*"
        embed.set_footer(text="เลือกห้อง → ✏️ แก้ชื่อ / เลือกบริการที่ใช้ได้ / 🗑️ ลบ · หรือกด ➕ เพิ่มห้อง")
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
            text = f"เพิ่มห้อง **{name}** (ใช้ได้ทุกบริการที่ต้องใช้ห้อง เลือกจำกัดบริการได้ในเมนูห้อง)"
        _save(interaction)
        await log_change(interaction.client, interaction.user, text)
        view = RoomsView(cfg, self.view.room_key)
        await interaction.response.edit_message(embed=view.embed(), view=view)


# ==================================================================== บริการ
SERVICE_TYPES = {
    "normal": ("บริการปกติ", "ไม่ต้องใช้ห้อง"),
    "room": ("ต้องใช้ห้อง", "บังคับเลือกห้องตอนเปิดบิล"),
    "extend": ("แพ็กเกจต่อเวลา", "ใช้ในเมนูต่อเวลา/เพิ่มรอบ"),
    "unit": ("คิดต่อหน่วย", "เช่น shot · กรอกจำนวนตอนเปิดบิล"),
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
            visible = [s for s in self.cfg.services if not s.get("hidden")]
            embed = discord.Embed(title=f"🛎️ บริการ & ราคา · {len(visible)} รายการ", color=COLOR_MAIN)
            embed.description = "\n".join(
                f"{s.get('emoji', '') or '•'} **{s['name']}**\n┗ {service_line(self.cfg, s)}" for s in visible
            )[:4000] or "*ยังไม่มีบริการ กด ➕ เพิ่มบริการ*"
            embed.set_footer(text="เลือกบริการเพื่อแก้ไข/ลบ · หรือกด ➕ เพิ่มบริการ")
            return embed

        embed = discord.Embed(title=f"{svc.get('emoji', '')} {svc['name']}", color=COLOR_MAIN)
        embed.add_field(name="💰 ราคา / ส่วนแบ่ง", value=service_line(self.cfg, svc), inline=False)
        embed.add_field(name="🏷️ ประเภท", value=SERVICE_TYPES[service_type(svc)][0], inline=True)
        cat = svc.get("category")
        cat_label = next((o["label"] for o in self.cfg.get("attendance.accept_options") or [] if o["key"] == cat), "ไม่ระบุ")
        embed.add_field(name="📂 หมวดงาน", value=cat_label, inline=True)
        if svc.get("vip_only"):
            embed.add_field(name="💎 เฉพาะ VIP", value="ใช้ได้เฉพาะสมาชิก VIP", inline=True)
        if svc.get("multi_staff"):
            embed.add_field(
                name="👥 หลายพนักงาน",
                value=(
                    f"รวม {svc.get('included_staff', 1)} คน · เพิ่มคนละ {svc.get('extra_staff_price', 0):,.0f} บาท "
                    f"(พนักงานได้ {svc.get('extra_staff_percent', 100):g}%) · สูงสุด {svc.get('max_staff', 10)} คน"
                ),
                inline=False,
            )
        if int(svc.get("max_customers", 1)) > 1:
            embed.add_field(
                name="👫 ลูกค้าหลายคน",
                value=(
                    f"สูงสุด {svc['max_customers']} คน · เพิ่มคนละ {svc.get('extra_customer_price', 0):,.0f} บาท "
                    f"(พนักงานได้ {svc.get('extra_customer_percent', 100):g}%)"
                    + (" · 👥 ต้องให้พนักงานยินยอมตอนเข้างาน" if svc.get("group_consent") else "")
                ),
                inline=False,
            )
        if svc.get("description"):
            embed.add_field(name="📝 คำอธิบาย (แสดงในเมนูลูกค้า)", value=svc["description"][:1024], inline=False)
        embed.set_footer(text=f"รหัส: {svc['key']} · ✏️ แก้ / 🗑️ ลบ ด้วยปุ่มด้านล่าง")
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
        # บริการเสริมที่มีบริการหลักนี้เป็นตัวเดียว ต้องลบตามไปด้วย ไม่งั้นจะกลายเป็นบริการเดี่ยวที่เปิดบิลได้เอง
        orphans = [
            s for s in self.cfg.services
            if s.get("addon_for") and set(s["addon_for"]) <= {svc["key"]} and s["key"] != svc["key"]
        ]

        async def do_delete(i: discord.Interaction) -> None:
            removed = {svc["key"], *(o["key"] for o in orphans)}
            self.cfg.data["services"] = [s for s in self.cfg.services if s["key"] not in removed]
            for s in self.cfg.services:  # ถอดออกจากบริการเสริมและห้องที่อ้างถึง
                for key in removed:
                    (s.get("addon_for") or {}).pop(key, None)
            for r in self.cfg.rooms:
                if r.get("services"):
                    r["services"] = [k for k in r["services"] if k not in removed]
            _save(i)
            extra = f" (และบริการเสริม {', '.join(o['name'] for o in orphans)})" if orphans else ""
            await log_change(i.client, i.user, f"ลบบริการ **{svc['name']}**{extra}")
            view = ServicesView(self.cfg)
            await i.response.edit_message(embed=view.embed(), view=view)

        note = "บิลเก่ายังอยู่ แต่จะแสดงเป็นรหัสบริการแทนชื่อ"
        if used_by:
            note += f"\n⚠️ บริการเสริมที่ใช้คู่กับบริการนี้จะถูกถอดออกด้วย: {', '.join(used_by)}"
        if orphans:
            note += (
                f"\n⚠️ บริการเสริมที่ใช้ได้กับบริการนี้อย่างเดียวจะถูก**ลบ**ด้วย: {', '.join(o['name'] for o in orphans)}"
            )
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
                f" · หลายพนักงาน สูงสุด {svc['max_staff']} คน · เพิ่มคนละ {svc['extra_staff_price']:,.0f} บาท "
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
            f"ตั้งลูกค้าหลายคนของ **{svc['name']}** · สูงสุด {mx} คน · เพิ่มคนละ {price:,.0f} บาท "
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
            description=(
                "1️⃣ เลือก**ประเภท**บริการ\n"
                "2️⃣ เลือก**หมวดงาน** (ไม่บังคับ ใช้จับคู่กับงานที่พนักงานรับตอนเข้างาน)\n"
                "3️⃣ กด 📝 **กรอกรายละเอียด** (ชื่อ · ราคา · เวลา · ส่วนแบ่ง)"
            ),
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
            name="⭐ ค่าเริ่มต้น",
            value=f"**{share.get('default_staff_percent', 60)}%** (ใช้กับบริการที่ไม่ได้ตั้ง % เอง)",
            inline=False,
        )
        fixed = [
            f"{s.get('emoji', '')} {s['name']} — {s['staff_percent']}%"
            for s in self.cfg.services
            if s.get("staff_percent") is not None and not s.get("hidden")
        ]
        embed.add_field(name="🛎️ บริการที่ตั้ง % เอง", value="\n".join(fixed)[:1024] or "-", inline=False)
        per = share.get("staff_percent") or {}
        embed.add_field(
            name="👤 ตั้งรายคน (ใช้กับบริการที่ไม่ได้ตั้ง % เอง)",
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


# ============================================================ Role รีเซปชั่น
class ReceptionRoleModal(discord.ui.Modal, title="Role รีเซปชั่น"):
    """Role ที่ใช้แผง /panel reception ได้ (เปิดบิล / ต่อเวลา / ดูงาน) — แยกจากแอดมิน"""

    def __init__(self, cfg) -> None:
        super().__init__()
        self.roles = discord.ui.TextInput(
            label="Role ID (หลาย Role คั่นด้วย , · ว่าง = ปิด)",
            default=", ".join(str(r) for r in cfg.reception_role_ids),
            required=False,
            max_length=200,
            placeholder="คลิกขวาที่ Role → Copy Role ID",
        )
        self.add_item(self.roles)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        cfg = interaction.client.cfg
        parts = [p.strip() for p in self.roles.value.replace(" ", ",").split(",") if p.strip()]
        if any(not p.isdigit() for p in parts):
            await interaction.response.send_message("⚠️ Role ID ต้องเป็นตัวเลขเท่านั้นค่ะ", ephemeral=True)
            return
        ids = list(dict.fromkeys(int(p) for p in parts))
        guild = interaction.guild
        missing = [i for i in ids if guild is None or guild.get_role(i) is None]
        if missing:
            await interaction.response.send_message(
                "⚠️ ไม่พบ Role ID นี้ในเซิร์ฟเวอร์: " + ", ".join(map(str, missing)), ephemeral=True
            )
            return
        cfg.data.setdefault("roles", {})["reception"] = ids
        _save(interaction)
        text = " ".join(f"<@&{i}>" for i in ids) or "ไม่มี (เฉพาะแอดมิน)"
        await log_change(interaction.client, interaction.user, f"ตั้ง Role รีเซปชั่น: {text}")
        await interaction.response.send_message(
            embed=discord.Embed(
                description=f"✅ บันทึกแล้ว Role รีเซปชั่น: {text}\nใช้ `/panel reception` และปุ่มในแผงได้ทันที",
                color=COLOR_OK,
            ),
            ephemeral=True,
        )


# ======================================================================= VIP
class VipSettingsView(AdminView):
    """เปิด/ปิดระบบ VIP และแก้แพ็กเกจหลัก (ระดับแรก + แพ็กเกจแรก) / Role / สิทธิ์เพิ่มเวลา / Free Date"""

    def __init__(self, cfg) -> None:
        super().__init__(timeout=600)
        self.cfg = cfg
        self.toggle.label = "ปิดระบบ VIP" if cfg.vip_enabled else "เปิดระบบ VIP"
        self.toggle.style = discord.ButtonStyle.danger if cfg.vip_enabled else discord.ButtonStyle.success
        self.add_item(BackButton(row=1))

    def embed(self) -> discord.Embed:
        from cogs.vip import perks_text
        from core.vip_logic import vip_benefit

        cfg = self.cfg
        tier = (cfg.vip_tiers or [{}])[0]
        pkgs = cfg.vip_packages
        role_id = int(tier.get("role_id") or 0)
        bonus = vip_benefit(cfg, "bonus_minutes") or {}
        stack = vip_benefit(cfg, "stack_services") or []
        embed = discord.Embed(
            title="💎 ตั้งค่า VIP",
            description=rows_text([
                ("🔌", "สถานะ", "🟢 เปิดใช้งาน" if cfg.vip_enabled else "⚪ ปิดอยู่"),
                (
                    "📦",
                    "แพ็กเกจ",
                    "\n".join(f"{p.get('name', '-')} · {p.get('price', 0):,.0f} บาท" for p in pkgs) or "-",
                ),
                ("🏷️", "Role VIP", f"<@&{role_id}>" if role_id else "⚠️ ยังไม่ตั้ง (ใส่ Role ID ที่ปุ่ม ✏️)"),
                (
                    "⏱️",
                    "เพิ่มเวลาห้อง",
                    ", ".join(
                        f"{cfg.service_name(k)} +{m}" + (" (ซ้อนได้)" if k in stack else "") for k, m in bonus.items()
                    ) or "-",
                ),
                ("💎", "Free Date", f"วันละ {vip_benefit(cfg, 'free_date_per_day')} ครั้ง"),
            ]),
            color=COLOR_GOLD if cfg.vip_enabled else COLOR_MAIN,
        )
        embed.set_footer(
            text="เปิดครั้งแรก บอทเติมแพ็กเกจ 1 เดือน 149 บาท / 3 เดือน 299 บาท + VIP Free Date ให้เอง · แล้วโพสต์ /panel_request ใหม่"
        )
        perks = perks_text(cfg)
        if perks:
            embed.add_field(name="✨ สิทธิ์ที่แสดงให้ลูกค้าเห็น", value=perks[:1024], inline=False)
        return embed

    @discord.ui.button(label="เปิดระบบ VIP", emoji="🔌", row=0)
    async def toggle(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        from cogs.vip import ensure_vip_defaults

        on = not self.cfg.vip_enabled
        self.cfg.data.setdefault("features", {})["vip"] = on
        if on:
            ensure_vip_defaults(self.cfg)
        _save(interaction)
        await log_change(interaction.client, interaction.user, f"{'เปิด' if on else 'ปิด'}ระบบ VIP")
        view = VipSettingsView(self.cfg)
        await interaction.response.edit_message(embed=view.embed(), view=view)

    @discord.ui.button(label="แก้แพ็กเกจ / Role / สิทธิ์", emoji="✏️", style=discord.ButtonStyle.primary, row=0)
    async def edit(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        from cogs.vip import ensure_vip_defaults

        ensure_vip_defaults(self.cfg)
        await interaction.response.send_modal(VipModal(self.cfg))


class VipModal(discord.ui.Modal, title="แก้แพ็กเกจ VIP"):
    def __init__(self, cfg) -> None:
        super().__init__()
        from core.vip_logic import vip_benefit

        tier = cfg.vip_tiers[0]
        pkg1, pkg3 = (cfg.vip_package(k) or {} for k in ("pandora_1m", "pandora_3m"))
        bonus = vip_benefit(cfg, "bonus_minutes") or {}
        self.price = discord.ui.TextInput(label="ราคา 1 เดือน (บาท)", default=f"{float(pkg1.get('price', 149)):g}", max_length=7)
        self.price3 = discord.ui.TextInput(label="ราคา 3 เดือน (บาท)", default=f"{float(pkg3.get('price', 299)):g}", max_length=7)
        self.role = discord.ui.TextInput(
            label="Role ID ของ VIP (0 = ไม่ให้ Role)", default=str(tier.get("role_id") or 0), max_length=22
        )
        self.bonus = discord.ui.TextInput(
            label="เพิ่มเวลาห้อง (นาที, 0 = ปิด)", default=str(max(bonus.values(), default=10)), max_length=3
        )
        self.free = discord.ui.TextInput(
            label="Free Date ต่อวัน (ครั้ง, 0 = ปิด)",
            default=str(vip_benefit(cfg, "free_date_per_day")),
            max_length=2,
        )
        for item in (self.price, self.price3, self.role, self.bonus, self.free):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        cfg = interaction.client.cfg
        try:
            price = _num(self.price.value, lo=0, hi=100000, name="ราคา 1 เดือน")
            price3 = _num(self.price3.value, lo=0, hi=100000, name="ราคา 3 เดือน")
            bonus = int(_num(self.bonus.value, lo=0, hi=240, name="เพิ่มเวลาห้อง"))
            free = int(_num(self.free.value, lo=0, hi=10, name="Free Date ต่อวัน"))
            role_text = self.role.value.strip() or "0"
            if not role_text.isdigit():
                raise ValueError("**Role ID** ต้องเป็นตัวเลข (คลิกขวาที่ Role → Copy Role ID)")
            role_id = int(role_text)
        except ValueError as exc:
            await interaction.response.send_message(f"⚠️ {exc}", ephemeral=True)
            return
        if role_id and (interaction.guild is None or interaction.guild.get_role(role_id) is None):
            await interaction.response.send_message("⚠️ ไม่พบ Role ID นี้ในเซิร์ฟเวอร์ค่ะ", ephemeral=True)
            return

        tier = cfg.data["vip_tiers"][0]
        for key, value in (("pandora_1m", price), ("pandora_3m", price3)):
            for pkg in cfg.data["vip_packages"]:
                if pkg.get("key") == key:
                    pkg["price"] = value
        tier["role_id"] = role_id
        benefits = cfg.data.setdefault("vip_benefits", {})
        keys = list((benefits.get("bonus_minutes") or {}).keys()) or ["party_room", "bedroom", "karaoke"]
        benefits["bonus_minutes"] = {k: bonus for k in keys}
        benefits["free_date_per_day"] = free
        _save(interaction)
        summary = f"1 เดือน {price:,.0f} / 3 เดือน {price3:,.0f} บาท · Role {role_id or '-'} · เพิ่มเวลา {bonus} นาที · Free Date {free}/วัน"
        await log_change(interaction.client, interaction.user, f"แก้ VIP: {summary}")
        view = VipSettingsView(cfg)
        await interaction.response.edit_message(embed=view.embed(), view=view)


# ================================================================ ระบบบอท
# ช่อง/Role ที่บอทใช้ (key, ชนิด, ชื่อ, path ใน config, เลือกได้หลายอัน, คำอธิบาย)
BOT_SLOTS = [
    ("ch_admin", "channel", "🛠️ ห้องแอดมิน", "channels.admin", False, "บิล · สลิป · สรุปยอด · แจ้งเตือนแอดมิน"),
    ("ch_review", "channel", "💖 ห้องรีวิว", "channels.review", False, "รีวิวที่อนุมัติแล้ว"),
    ("ch_announce", "channel", "📣 ห้องประกาศ", "channels.announce", False, "Top Donate · โดเนท · ขอบคุณ · อีเวนต์"),
    ("ch_ticket", "channel", "💬 ห้องตั๋วสอบถาม", "channels.ticket", False, "ลูกค้าทัก DM / รับเรื่อง (ไม่ตั้ง = ห้องแอดมิน)"),
    ("ch_slip", "channel", "🧾 ห้องตรวจสลิป", "channels.slip", False, "สลิปรอยืนยัน (ไม่ตั้ง = ห้องแอดมิน)"),
    ("ch_review_check", "channel", "📝 ห้องตรวจรีวิว", "channels.review_check", False, "รีวิวรออนุมัติ ✅/❌ (ไม่ตั้ง = ห้องแอดมิน)"),
    ("ch_attendance", "channel", "🕒 ห้องเข้างาน", "channels.attendance", False, "พนักงานเข้างาน/ตัดยอด (ไม่ตั้ง = ห้องแอดมิน)"),
    ("ch_log", "channel", "📝 ห้อง Log", "channels.log", False, "บันทึกการแก้ตั้งค่า (ไม่ตั้ง = ห้องแอดมิน)"),
    ("role_admin", "role", "🛠️ Role แอดมิน", "roles.admin", False, "ใช้เมนูแอดมิน/ตั้งค่า/ยืนยันสลิป"),
    ("role_reception", "role", "🔑 Role รีเซปชั่น", "roles.reception", True, "ใช้แผง /panel reception"),
    ("role_staff", "role", "💃 Role พนักงาน", "roles.staff", True, "รับงาน · เข้างาน · เมนูพนักงาน"),
    ("role_on_duty", "role", "🟢 Role On Duty", "roles.on_duty", False, "บอทให้ตอนเข้างาน ถอดตอนตัดยอด"),
    ("role_adult", "role", "🔞 Role ยืนยันอายุ 18+", "roles.adult_verified", True, "ลูกค้าต้องมีถึงเปิดบิล 18+ ได้"),
]

# ห้องที่ไม่ตั้งก็ได้ (ไม่ตั้ง = ส่งเข้าห้องแอดมิน) — ไม่นับเป็น "ยังขาด"
OPTIONAL_SLOTS = {"channels.log", "channels.ticket", "channels.slip", "channels.attendance", "channels.review_check"}

BOT_FEATURES = [
    ("donate.enabled", "💜 ระบบโดเนท", True),
    ("donate.announce", "📣 ประกาศโดเนทในห้องประกาศ", True),
    ("top_donate.enabled", "🏆 Top Donate รายเดือน", True),
]

WEEKDAYS = ["จันทร์", "อังคาร", "พุธ", "พฤหัสบดี", "ศุกร์", "เสาร์", "อาทิตย์"]


def _slot_ids(cfg, path: str) -> list[int]:
    raw = cfg.get(path, 0) or 0
    values = raw if isinstance(raw, list) else [raw]
    return [int(v) for v in values if int(v or 0)]


def _slot_text(kind: str, ids: list[int], path: str = "") -> str:
    if not ids:
        return "↪️ ใช้ห้องแอดมิน" if path in OPTIONAL_SLOTS else "⚠️ ยังไม่ตั้ง"
    return " ".join(f"<#{i}>" if kind == "channel" else f"<@&{i}>" for i in ids)


def _set_path(cfg, path: str, value) -> None:
    section, key = path.split(".")
    cfg.data.setdefault(section, {})[key] = value


class BotSetupView(AdminView):
    """ตั้งค่าที่บอทต้องใช้: ห้อง / Role / เวลาแจ้งเตือน / ตัดรอบ / ร้าน & Sheets / เปิด-ปิดฟีเจอร์"""

    def __init__(self, cfg, slot: str | None = None, *, sheets_ready: bool = False) -> None:
        super().__init__(timeout=600)
        self.cfg = cfg
        self.slot = slot
        self.sheets_ready = sheets_ready

        self.slot_select = discord.ui.Select(
            placeholder="1) เลือกห้อง / Role ที่จะตั้ง",
            row=0,
            options=[
                discord.SelectOption(
                    label=name.split(" ", 1)[1][:100],
                    value=key,
                    emoji=name.split(" ", 1)[0],
                    description=(
                        ("✅ ตั้งแล้ว · " if _slot_ids(cfg, path) else "▫️ " if path in OPTIONAL_SLOTS else "⚠️ ยังไม่ตั้ง · ")
                        + detail
                    )[:100],
                    default=key == slot,
                )
                for key, _, name, path, _, detail in BOT_SLOTS
            ],
        )
        self.slot_select.callback = self._on_slot
        self.add_item(self.slot_select)

        spec = self._spec()
        if spec is not None:
            _, kind, name, _, multi, _ = spec
            if kind == "channel":
                # ห้อง: ใส่ Channel ID เอง (กดปุ่ม ✏️) — เมนูเลือกห้องของ Discord หาห้องยากเมื่อห้องเยอะ
                self.id_button.label = "2) ใส่ Channel ID"
            else:
                self.value_select = discord.ui.RoleSelect(
                    placeholder=f"2) เลือก{name.split(' ', 1)[1]}" + (" (เลือกได้หลายอัน)" if multi else ""),
                    row=1,
                    min_values=1,
                    max_values=10 if multi else 1,
                )
                self.value_select.callback = self._on_value
                self.add_item(self.value_select)
                self.id_button.label = "หรือใส่ Role ID"
        else:
            self.clear_slot.disabled = True
            self.id_button.disabled = True
        self.add_item(BackButton(row=4))

    def _spec(self):
        return next((s for s in BOT_SLOTS if s[0] == self.slot), None)

    def embed(self) -> discord.Embed:
        cfg = self.cfg
        channels = rows_text(
            [(n.split(" ", 1)[0], n.split(" ", 1)[1], _slot_text(k, _slot_ids(cfg, p), p)) for _, k, n, p, _, _ in BOT_SLOTS if k == "channel"]
        )
        roles = rows_text(
            [(n.split(" ", 1)[0], n.split(" ", 1)[1], _slot_text(k, _slot_ids(cfg, p))) for _, k, n, p, _, _ in BOT_SLOTS if k == "role"]
        )
        missing = sum(1 for _, _, _, p, _, _ in BOT_SLOTS if not _slot_ids(cfg, p) and p not in OPTIONAL_SLOTS)
        embed = discord.Embed(
            title="🧭 ตั้งค่าระบบบอท",
            description=(
                "1️⃣ เลือกห้อง/Role จากเมนูแรก　2️⃣ กด ✏️ ใส่ Channel ID (Role เลือกจากเมนูหรือใส่ ID ก็ได้) · บันทึกทันที\n"
                + (f"⚠️ ยังไม่ได้ตั้ง **{missing}** รายการ" if missing else "✅ ตั้งห้องและ Role ครบแล้ว")
            ),
            color=COLOR_OK if not missing else COLOR_MAIN,
        )
        embed.add_field(name="📍 ห้อง", value=channels[:1024], inline=False)
        embed.add_field(name="🏷️ Role", value=roles[:1024], inline=False)
        embed.add_field(
            name="⏰ เวลา & แจ้งเตือน",
            value=rows_text([
                ("🔔", "เตือนก่อนเริ่มงาน", f"{cfg.before_start_minutes} นาที"),
                ("⌛", "เตือนก่อนหมดเวลา", f"{cfg.before_end_minutes} นาที"),
                ("🎫", "ปิดตั๋วเงียบ", f"{cfg.ticket_timeout_minutes} นาที"),
                ("💖", "เขียนรีวิวได้ภายใน", f"{cfg.review_window_hours} ชม."),
            ]),
            inline=True,
        )
        embed.add_field(
            name="🧾 บิลค้าง",
            value=rows_text([
                ("💃", "รอพนักงานรับ", f"{cfg.get('bill_timeout.staff_accept_minutes', 15)} นาที"),
                ("💳", "เตือนจ่ายเงิน", f"{cfg.get('bill_timeout.payment_remind_minutes', 15)} นาที"),
                ("❌", "ยกเลิกไม่จ่าย", f"{cfg.get('bill_timeout.payment_cancel_minutes', 45)} นาที"),
                ("🔎", "สลิปรอตรวจ", f"{cfg.get('bill_timeout.slip_review_minutes', 10)} นาที"),
            ]),
            inline=True,
        )
        sheets = self.cfg.get("google_sheets.enabled", False)
        embed.add_field(
            name="🏪 ร้าน & ระบบ",
            value=rows_text([
                ("🏷️", "ชื่อร้าน", cfg.shop_name),
                (
                    "✂️",
                    "ตัดรอบ",
                    f"ทุกวัน{WEEKDAYS[cfg.cutoff_weekday % 7]} {cfg.cutoff_hour:02d}:{cfg.cutoff_minute:02d}",
                ),
                ("📊", "Google Sheets", ("🟢 เปิด" if sheets else "⚪ ปิด") + (" · เชื่อมต่อแล้ว" if self.sheets_ready else "")),
                *[(n.split(" ", 1)[0], n.split(" ", 1)[1], "🟢 เปิด" if cfg.get(p, d) else "⚪ ปิด") for p, n, d in BOT_FEATURES],
            ]),
            inline=False,
        )
        embed.set_footer(text="Guild ID / Token / ไฟล์ credentials ยังแก้ใน config.json และ .env (ต้องรีสตาร์ต)")
        return embed

    async def _rerender(self, interaction: discord.Interaction, slot: str | None = None) -> None:
        view = BotSetupView(self.cfg, slot, sheets_ready=interaction.client.sheets.ready)
        await interaction.response.edit_message(embed=view.embed(), view=view)

    async def _on_slot(self, interaction: discord.Interaction) -> None:
        await self._rerender(interaction, self.slot_select.values[0])

    async def _on_value(self, interaction: discord.Interaction) -> None:
        await self.apply_ids(interaction, [int(v.id) for v in self.value_select.values])

    async def apply_ids(self, interaction: discord.Interaction, ids: list[int]) -> None:
        """บันทึกห้อง/Role ของช่องที่เลือก (ใช้ทั้งเมนูเลือกและฟอร์มใส่ ID)"""
        key, kind, name, path, multi, _ = self._spec()
        if path == "roles.admin":
            member = interaction.user
            perms = getattr(member, "guild_permissions", None)
            has_role = any(r.id == ids[0] for r in getattr(member, "roles", []))
            if not has_role and not (perms and (perms.administrator or perms.manage_guild)):
                await interaction.response.send_message(
                    "⚠️ ตั้ง Role แอดมินเป็น Role ที่คุณไม่มีไม่ได้ (จะล็อกตัวเองออกจากเมนูแอดมิน) "
                    "— ใส่ Role นี้ให้ตัวเองก่อน หรือให้เจ้าของเซิร์ฟเวอร์ตั้งค่ะ",
                    ephemeral=True,
                )
                return
        _set_path(self.cfg, path, ids if multi else ids[0])
        _save(interaction)
        await log_change(interaction.client, interaction.user, f"ตั้ง{name}: {_slot_text(kind, ids)}")
        await self._rerender(interaction, key)

    @discord.ui.button(label="ใส่ ID", emoji="✏️", style=discord.ButtonStyle.primary, row=2)
    async def id_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if self._spec() is None:
            await interaction.response.send_message("เลือกห้อง / Role จากเมนูแรกก่อนค่ะ", ephemeral=True)
            return
        await interaction.response.send_modal(SlotIdModal(self))

    @discord.ui.button(label="ล้างค่าที่เลือก", emoji="🗑️", style=discord.ButtonStyle.danger, row=2)
    async def clear_slot(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        spec = self._spec()
        if spec is None:
            await interaction.response.defer()
            return
        key, _, name, path, multi, _ = spec
        if path == "roles.admin":
            await interaction.response.send_message("ล้าง Role แอดมินไม่ได้ค่ะ (ให้เลือก Role ใหม่แทน)", ephemeral=True)
            return
        _set_path(self.cfg, path, [] if multi else 0)
        _save(interaction)
        await log_change(interaction.client, interaction.user, f"ล้างค่า{name}")
        await self._rerender(interaction, key)

    @discord.ui.button(label="เวลา & แจ้งเตือน", emoji="⏰", style=discord.ButtonStyle.primary, row=3)
    async def times(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_modal(NumberFieldsModal("⏰ เวลา & แจ้งเตือน", TIME_FIELDS, self.cfg))

    @discord.ui.button(label="บิลค้าง", emoji="🧾", style=discord.ButtonStyle.primary, row=3)
    async def bills(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_modal(NumberFieldsModal("🧾 บิลค้าง", BILL_FIELDS, self.cfg))

    @discord.ui.button(label="ตัดรอบ", emoji="✂️", style=discord.ButtonStyle.primary, row=3)
    async def cutoff(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_modal(CutoffModal(self.cfg))

    @discord.ui.button(label="ร้าน & Sheets", emoji="🏪", style=discord.ButtonStyle.secondary, row=3)
    async def shop(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_modal(ShopModal(self.cfg))

    @discord.ui.button(label="เปิด/ปิดฟีเจอร์", emoji="🔌", style=discord.ButtonStyle.secondary, row=2)
    async def features(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        view = FeatureView(self.cfg)
        await interaction.response.edit_message(embed=view.embed(), view=view)


class SlotIdModal(discord.ui.Modal):
    """ใส่ Channel ID / Role ID เอง (คลิกขวาที่ห้อง/Role → Copy ID · ต้องเปิด Developer Mode)"""

    def __init__(self, view: "BotSetupView") -> None:
        _, kind, name, path, multi, _ = view._spec()
        super().__init__(title=f"✏️ {name.split(' ', 1)[1]}"[:45])
        self.view = view
        self.kind = kind
        current = ", ".join(str(i) for i in _slot_ids(view.cfg, path))
        self.ids = discord.ui.TextInput(
            label=("Channel ID" if kind == "channel" else "Role ID") + (" (หลายอันคั่นด้วย ,)" if multi else ""),
            placeholder="คลิกขวาที่" + ("ห้อง" if kind == "channel" else " Role") + " → Copy ID เช่น 1234567890123456789",
            default=current or None,
            max_length=200,
        )
        self.add_item(self.ids)
        self.multi = multi

    async def on_submit(self, interaction: discord.Interaction) -> None:
        parts = [p.strip().strip("<>#@&") for p in self.ids.value.replace(" ", ",").split(",") if p.strip()]
        if not parts or any(not p.isdigit() for p in parts):
            await interaction.response.send_message("⚠️ ID ต้องเป็นตัวเลขเท่านั้นค่ะ (เช่น 1234567890123456789)", ephemeral=True)
            return
        if not self.multi and len(parts) > 1:
            await interaction.response.send_message("⚠️ ช่องนี้ใส่ได้ ID เดียวค่ะ", ephemeral=True)
            return
        ids = list(dict.fromkeys(int(p) for p in parts))
        guild = interaction.guild
        if self.kind == "channel":
            bad = [i for i in ids if guild is None or not hasattr(guild.get_channel(i), "send")]
            what = "ห้องข้อความ"
        else:
            bad = [i for i in ids if guild is None or guild.get_role(i) is None]
            what = "Role"
        if bad:
            await interaction.response.send_message(
                f"⚠️ ไม่พบ{what}นี้ในเซิร์ฟเวอร์: " + ", ".join(map(str, bad))
                + ("\n(บอทต้องมองเห็นห้องนั้นด้วย)" if self.kind == "channel" else ""),
                ephemeral=True,
            )
            return
        await self.view.apply_ids(interaction, ids)


class FeatureView(AdminView):
    def __init__(self, cfg) -> None:
        super().__init__(timeout=600)
        self.cfg = cfg
        select = discord.ui.Select(
            placeholder="ติ๊กฟีเจอร์ที่ต้องการเปิด (ที่ไม่ติ๊ก = ปิด)",
            min_values=0,
            max_values=len(BOT_FEATURES),
            row=0,
            options=[
                discord.SelectOption(label=n.split(" ", 1)[1], value=p, emoji=n.split(" ", 1)[0], default=bool(cfg.get(p, d)))
                for p, n, d in BOT_FEATURES
            ],
        )
        select.callback = self._on_pick
        self.select = select
        self.add_item(select)
        back = discord.ui.Button(label="กลับ", emoji="⬅️", style=discord.ButtonStyle.secondary, row=1)
        back.callback = self._back
        self.add_item(back)

    def embed(self) -> discord.Embed:
        return discord.Embed(
            title="🔌 เปิด/ปิดฟีเจอร์",
            description=rows_text(
                [(n.split(" ", 1)[0], n.split(" ", 1)[1], "🟢 เปิด" if self.cfg.get(p, d) else "⚪ ปิด") for p, n, d in BOT_FEATURES]
            )
            + "\n\n> VIP และเหรียญ Pandora เปิด/ปิดได้ที่เมนูของตัวเอง",
            color=COLOR_MAIN,
        )

    async def _on_pick(self, interaction: discord.Interaction) -> None:
        on = set(self.select.values)
        changed = []
        for path, name, default in BOT_FEATURES:
            value = path in on
            if bool(self.cfg.get(path, default)) != value:
                _set_path(self.cfg, path, value)
                changed.append(f"{name}: {'เปิด' if value else 'ปิด'}")
        if changed:
            _save(interaction)
            await log_change(interaction.client, interaction.user, "เปิด/ปิดฟีเจอร์\n" + "\n".join(f"• {c}" for c in changed))
        view = FeatureView(self.cfg)
        await interaction.response.edit_message(embed=view.embed(), view=view)

    async def _back(self, interaction: discord.Interaction) -> None:
        view = BotSetupView(self.cfg, sheets_ready=interaction.client.sheets.ready)
        await interaction.response.edit_message(embed=view.embed(), view=view)


TIME_FIELDS = [
    ("notify.before_start_minutes", "เตือนก่อนเริ่มงาน (นาที)", 5, 0, 120),
    ("notify.before_end_minutes", "เตือนก่อนหมดเวลา (นาที)", 5, 0, 120),
    ("ticket.timeout_minutes", "ปิดตั๋วสอบถามเมื่อเงียบ (นาที)", 10, 1, 1440),
    ("review.window_hours", "เขียนรีวิวได้ภายใน (ชั่วโมง)", 24, 1, 720),
    ("donate.expire_minutes", "ยกเลิกโดเนทที่ไม่จ่าย (นาที)", 60, 5, 1440),
]
BILL_FIELDS = [
    ("bill_timeout.staff_accept_minutes", "แจ้งแอดมินเมื่อพนักงานไม่รับ (นาที)", 15, 1, 1440),
    ("bill_timeout.payment_remind_minutes", "DM เตือนลูกค้าจ่ายเงิน (นาที)", 15, 1, 1440),
    ("bill_timeout.payment_cancel_minutes", "ยกเลิกบิลที่ไม่จ่าย (นาที)", 45, 5, 1440),
    ("bill_timeout.slip_review_minutes", "แจ้งแอดมินเมื่อสลิปค้างตรวจ (นาที)", 10, 1, 1440),
]


class NumberFieldsModal(discord.ui.Modal):
    """ฟอร์มตัวเลขทั่วไป (สูงสุด 5 ช่อง) — fields = [(path, label, ค่าเริ่มต้น, ต่ำสุด, สูงสุด)]"""

    def __init__(self, title: str, fields: list[tuple], cfg) -> None:
        super().__init__(title=title[:45])
        self.fields = fields
        self.inputs: list[tuple[tuple, discord.ui.TextInput]] = []
        for spec in fields[:5]:
            path, label, default, _, _ = spec
            item = discord.ui.TextInput(label=label[:45], default=f"{float(cfg.get(path, default)):g}", max_length=9)
            self.inputs.append((spec, item))
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        cfg = interaction.client.cfg
        try:
            values = {spec[0]: int(_num(f.value, lo=spec[3], hi=spec[4], name=spec[1])) for spec, f in self.inputs}
        except ValueError as exc:
            await interaction.response.send_message(f"⚠️ {exc}", ephemeral=True)
            return
        labels = {spec[0]: spec[1] for spec, _ in self.inputs}
        changed = []
        for path, value in values.items():
            if cfg.get(path) != value:
                changed.append(f"{labels[path]}: {cfg.get(path)} → {value}")
                _set_path(cfg, path, value)
        if changed:
            _save(interaction)
            await log_change(interaction.client, interaction.user, f"แก้{self.title}\n" + "\n".join(f"• {c}" for c in changed))
        view = BotSetupView(cfg, sheets_ready=interaction.client.sheets.ready)
        await interaction.response.edit_message(embed=view.embed(), view=view)


class CutoffModal(discord.ui.Modal, title="✂️ วันเวลาตัดรอบบัญชี"):
    def __init__(self, cfg) -> None:
        super().__init__()
        self.weekday = discord.ui.TextInput(
            label="วัน (1=จันทร์ … 6=เสาร์, 7=อาทิตย์)", default=str(cfg.cutoff_weekday % 7 + 1), max_length=1
        )
        self.time = discord.ui.TextInput(
            label="เวลา (HH:MM)", default=f"{cfg.cutoff_hour:02d}:{cfg.cutoff_minute:02d}", max_length=5
        )
        self.add_item(self.weekday)
        self.add_item(self.time)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        cfg = interaction.client.cfg
        try:
            weekday = int(_num(self.weekday.value, lo=1, hi=7, name="วัน")) - 1
            hour_text, _, minute_text = self.time.value.strip().replace(".", ":").partition(":")
            hour = int(_num(hour_text, lo=0, hi=23, name="ชั่วโมง"))
            minute = int(_num(minute_text or "0", lo=0, hi=59, name="นาที"))
        except ValueError as exc:
            await interaction.response.send_message(f"⚠️ {exc}", ephemeral=True)
            return
        cfg.data["cutoff"] = {**(cfg.get("cutoff") or {}), "weekday": weekday, "hour": hour, "minute": minute}
        _save(interaction)
        # นับรอบปัจจุบันตามเวลาใหม่ทันที — กันบอทส่งสรุปรอบทันทีเพราะวันเริ่มรอบเปลี่ยน
        from core.cycle import cycle_start_local

        now_local = dt.datetime.now(cfg.tz)
        await interaction.client.db.set_meta("last_cutoff", cycle_start_local(now_local, cfg).date().isoformat())
        text = f"ทุกวัน{WEEKDAYS[weekday]} {hour:02d}:{minute:02d}"
        await log_change(interaction.client, interaction.user, f"ตั้งเวลาตัดรอบ: {text}")
        view = BotSetupView(cfg, sheets_ready=interaction.client.sheets.ready)
        await interaction.response.edit_message(embed=view.embed(), view=view)


class ShopModal(discord.ui.Modal, title="🏪 ร้าน & Google Sheets"):
    def __init__(self, cfg) -> None:
        super().__init__()
        self.name = discord.ui.TextInput(label="ชื่อร้าน", default=cfg.shop_name, max_length=80)
        self.sheets_on = discord.ui.TextInput(
            label="เปิดใช้ Google Sheets (ใช่ / ไม่)",
            default="ใช่" if cfg.get("google_sheets.enabled", False) else "ไม่",
            max_length=5,
        )
        self.sheet_id = discord.ui.TextInput(
            label="Spreadsheet ID (จากลิงก์ /d/<ID>/edit)",
            default=str(cfg.get("google_sheets.spreadsheet_id", "") or ""),
            required=False,
            max_length=120,
        )
        self.color = discord.ui.TextInput(
            label="สีกรอบรีวิว (#RRGGBB)", default=str(cfg.get("review.embed_color", "#B026FF")), max_length=7
        )
        for item in (self.name, self.sheets_on, self.sheet_id, self.color):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        cfg = interaction.client.cfg
        on_text = self.sheets_on.value.strip().lower()
        if on_text not in ("ใช่", "ไม่", "yes", "no", "on", "off", "1", "0"):
            await interaction.response.send_message("⚠️ ช่องเปิดใช้ Google Sheets ให้ตอบ **ใช่** หรือ **ไม่** ค่ะ", ephemeral=True)
            return
        color = self.color.value.strip()
        if not color.startswith("#"):
            color = "#" + color
        try:
            int(color[1:], 16)
            assert len(color) == 7
        except (ValueError, AssertionError):
            await interaction.response.send_message("⚠️ สีต้องเป็นรูปแบบ #RRGGBB เช่น #B026FF ค่ะ", ephemeral=True)
            return
        sheet_id = self.sheet_id.value.strip()
        if "/d/" in sheet_id:  # วางทั้งลิงก์มาก็ได้
            sheet_id = sheet_id.split("/d/", 1)[1].split("/", 1)[0]
        enable = on_text in ("ใช่", "yes", "on", "1")
        if enable and not sheet_id:
            await interaction.response.send_message("⚠️ เปิดใช้ Google Sheets ต้องใส่ Spreadsheet ID ด้วยค่ะ", ephemeral=True)
            return

        old_sheets = (cfg.get("google_sheets.enabled", False), cfg.get("google_sheets.spreadsheet_id", ""))
        cfg.data["shop_name"] = self.name.value.strip() or cfg.shop_name
        _set_path(cfg, "review.embed_color", color.upper())
        _set_path(cfg, "google_sheets.enabled", enable)
        _set_path(cfg, "google_sheets.spreadsheet_id", sheet_id)
        _save(interaction)
        await interaction.response.defer()

        note = ""
        if (enable, sheet_id) != old_sheets:
            sheets = interaction.client.sheets
            if enable:
                await sheets.start()  # เชื่อมต่อใหม่ทันที ไม่ต้องรีสตาร์ต
                note = "\n📊 เชื่อมต่อ Google Sheets สำเร็จ" if sheets.ready else (
                    "\n⚠️ เชื่อมต่อ Google Sheets ไม่สำเร็จ ตรวจไฟล์ credentials และแชร์ชีตให้ service account"
                )
            else:
                sheets._spreadsheet = None
                note = "\n📊 ปิด Google Sheets แล้ว"
        await log_change(
            interaction.client,
            interaction.user,
            f"แก้ร้าน & Sheets: ชื่อ {cfg.shop_name} · Sheets {'เปิด' if enable else 'ปิด'} · สีรีวิว {color.upper()}",
        )
        view = BotSetupView(cfg, sheets_ready=interaction.client.sheets.ready)
        await interaction.edit_original_response(embed=view.embed(), view=view)
        if note:
            await interaction.followup.send(note.strip(), ephemeral=True)


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
