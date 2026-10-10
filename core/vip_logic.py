"""Life-cycle ของสิทธิ์ VIP: คำนวณวันหมดอายุ, สะสม/อัปเกรดยศ, รอบโควต้ารายเดือน

อ้างอิงจาก "VIP logic.txt":
- รายเดือน = บวก 1 เดือนปฏิทิน (ถ้าวันที่ 29-31 ม.ค. ชนกับ ก.พ. ที่มีวันน้อยกว่า จะเลื่อนไปวันสุดท้ายของ ก.พ. อัตโนมัติ)
- รายปี = บวก 365 วันตรงๆ
- ต่ออายุก่อนหมดอายุ (ระดับเดิม) -> สะสมต่อจากวันหมดอายุเดิม
- หมดอายุไปแล้วค่อยซื้อใหม่ หรือเปลี่ยนระดับ (อัปเกรด) -> เริ่มนับใหม่จาก "วันนี้"
- ตัวนับเดือนสะสม (สำหรับแจ้งอัปเกรดฟรีเมื่อครบ 12 เดือน) รีเซ็ตเป็น 0 ทันทีที่ขาดอายุ
"""
from __future__ import annotations

import calendar
import json
import datetime as dt

from .database import Database
from .utils import to_iso

UTC = dt.timezone.utc


async def active_tier(db: Database, user_id: int, now_local: dt.datetime) -> str | None:
    """ระดับ VIP ปัจจุบันของลูกค้า (None ถ้าไม่มี/หมดอายุแล้ว) — DB คือแหล่งความจริง"""
    row = await db.active_vip_member(user_id, to_iso(now_local))
    return row["tier"] if row else None


def add_calendar_months(base: dt.date, months: int) -> dt.date:
    """บวกจำนวนเดือนปฏิทิน โดย clamp วันที่เกินเดือนปลายทางให้เป็นวันสุดท้ายของเดือนนั้น
    (เช่น 31 ม.ค. + 1 เดือน -> 28/29 ก.พ. ตามที่ระบุใน VIP logic.txt)
    """
    month_index = base.month - 1 + months
    year = base.year + month_index // 12
    month = month_index % 12 + 1
    last_day = calendar.monthrange(year, month)[1]
    return base.replace(year=year, month=month, day=min(base.day, last_day))


def cycle_month_key(now_local: dt.datetime) -> str:
    """คีย์รอบโควต้ารายเดือน เช่น '2026-08' — รีเซ็ตพร้อมกันทุกวันที่ 1 ของเดือน"""
    return f"{now_local.year:04d}-{now_local.month:02d}"


def compute_new_expiry(
    *,
    now_local: dt.datetime,
    unit: str,
    package_months: int,
    previous_expiry: dt.datetime | None,
    same_tier_renewal: bool,
) -> dt.datetime:
    """คำนวณวันหมดอายุใหม่ตาม stacking logic

    - same_tier_renewal=True และยังไม่หมดอายุ -> สะสมต่อจาก previous_expiry
    - นอกนั้น (หมดอายุไปแล้ว หรือเป็นการอัปเกรด/เปลี่ยนระดับ) -> เริ่มนับจาก now_local
    - unit="month" -> บวกเดือนปฏิทิน (รองรับ edge case ม.ค. 29-31 -> ก.พ.)
    - unit="year" -> บวก 365 วันตรงๆ ตามเอกสาร
    """
    still_active = previous_expiry is not None and previous_expiry > now_local
    base = previous_expiry if (same_tier_renewal and still_active) else now_local

    if unit == "month":
        new_date = add_calendar_months(base.date(), package_months)
        return dt.datetime.combine(new_date, base.time(), tzinfo=base.tzinfo)
    if unit == "day":  # แอดมินให้สิทธิ์เป็นวัน (package_months = จำนวนวัน)
        return base + dt.timedelta(days=package_months)

    return base + dt.timedelta(days=365)


# ตัวเลือกระยะเวลาที่แอดมินให้ VIP ได้ (unit, จำนวน, ป้ายชื่อ) — สูงสุด 6 เดือน
GRANT_DURATIONS = [
    ("day", 1, "1 วัน"),
    ("day", 3, "3 วัน"),
    ("day", 7, "7 วัน"),
    ("day", 14, "14 วัน"),
    ("month", 1, "1 เดือน"),
    ("month", 2, "2 เดือน"),
    ("month", 3, "3 เดือน"),
    ("month", 6, "6 เดือน"),
]


def duration_label(unit: str, amount: int) -> str:
    return f"{amount} วัน" if unit == "day" else f"{amount} เดือน"


def free_upgrade_expiry(previous_expiry: dt.datetime, now_local: dt.datetime) -> dt.datetime:
    """วันหมดอายุใหม่เมื่อแอดมินอนุมัติอัปเกรดฟรี 1 เดือน (ต่อจากวันหมดอายุเดิม)"""
    base = previous_expiry if previous_expiry > now_local else now_local
    new_date = add_calendar_months(base.date(), 1)
    return dt.datetime.combine(new_date, base.time(), tzinfo=base.tzinfo)


def next_streak_months(*, current_streak: int, same_tier_renewal: bool, still_active: bool, months_added: int) -> int:
    """ตัวนับเดือนสะสมของระดับปัจจุบัน (ใช้แจ้งอัปเกรดฟรีเมื่อครบ 12 เดือน)

    - ต่ออายุระดับเดิมก่อนหมดอายุ -> สะสมบวกเพิ่ม
    - หมดอายุไปแล้ว หรือเปลี่ยนระดับ -> เริ่มนับใหม่จากจำนวนเดือนที่เพิ่งซื้อ
    """
    if same_tier_renewal and still_active:
        return current_streak + months_added
    return months_added


# ------------------------------------------------------------ Pandora VIP
# ค่าเริ่มต้นตามโปสเตอร์ Pandora VIP — บอทเติมลง config.json ให้ตอนเปิดระบบ VIP (แก้ได้ที่ ⚙️ ตั้งค่าร้าน → VIP)
DEFAULT_VIP_TIERS = [
    {"key": "pandora", "name": "Pandora VIP", "emoji": "💎", "rank": 1, "role_id": 0, "purchasable": True},
]
DEFAULT_VIP_PACKAGES = [
    {"key": "pandora_1m", "tier": "pandora", "name": "Pandora VIP 1 เดือน", "emoji": "💎",
     "price": 149, "unit": "month", "months": 1},
    {"key": "pandora_3m", "tier": "pandora", "name": "Pandora VIP 3 เดือน", "emoji": "💎",
     "price": 299, "unit": "month", "months": 3},
]
VIP_DATE_KEY = "vip_date"
DEFAULT_VIP_BENEFITS = {
    # เพิ่มเวลาห้องให้ลูกค้า VIP (นาที)
    "bonus_minutes": {"party_room": 10, "bedroom": 10, "karaoke": 10},
    # ห้องที่ VIP หลายคนในบิลเดียวบวกเวลาซ้อนกันได้ (VIP 2 คน = +20 นาที) — ห้องอื่นบวกครั้งเดียวต่อบิล
    "stack_services": ["party_room"],
    # Free Date กับพนักงานวันละครั้ง (นับตามวันทำงานของร้าน ตัดยอดตามเวลาตัดยอดเข้างาน)
    "free_date_per_day": 1,
    # สิทธิ์อื่นที่บอทไม่ได้คำนวณให้ (แสดงให้ลูกค้าเห็นอย่างเดียว) — สิทธิ์เพิ่มเวลา/Free Date บอทเขียนข้อความเองจากค่าด้านบน
    "extra_perks": ["Avatar ไม่จำกัด Polygon"],
}
DEFAULT_VIP_DATE_SERVICE = {
    "key": VIP_DATE_KEY, "name": "VIP Free Date", "emoji": "💎", "duration_minutes": 10, "require_room": False,
    "vip_only": True, "pricing": {"normal": 0}, "staff_percent": 70, "category": "chill",
    "description": "สิทธิ์สมาชิก VIP: Free Date กับพนักงาน 10 นาที วันละ 1 ครั้ง (เปลี่ยนพนักงานกลางคันไม่ได้)",
}


def vip_benefit(cfg, key: str):
    return (cfg.get("vip_benefits") or {}).get(key, DEFAULT_VIP_BENEFITS[key])


def shop_day_start(cfg, now_local: dt.datetime) -> dt.datetime:
    """จุดเริ่ม "วันทำงาน" ของร้าน = เวลาตัดยอดเข้างาน (attendance.day_cutoff_hour, ค่าเริ่มต้นตี 1) ล่าสุด"""
    hour = int(cfg.get("attendance.day_cutoff_hour", 1))
    start = now_local.replace(hour=hour, minute=0, second=0, microsecond=0)
    if start > now_local:
        start -= dt.timedelta(days=1)
    return start


def perks_lines(cfg, *, compact: bool = False) -> list[str]:
    """ข้อความสิทธิ์ VIP สำหรับแสดงลูกค้า (สร้างจากค่าตั้งค่าจริง แก้ค่าแล้วข้อความเปลี่ยนตาม)

    compact=True = ข้อความสั้นสำหรับแผงลูกค้า
    """
    lines = list(vip_benefit(cfg, "extra_perks") or [])
    bonus = {k: int(m) for k, m in (vip_benefit(cfg, "bonus_minutes") or {}).items() if int(m) > 0}
    stack = [k for k in vip_benefit(cfg, "stack_services") or [] if k in bonus]
    date_svc = cfg.service(VIP_DATE_KEY)
    per_day = int(vip_benefit(cfg, "free_date_per_day"))
    if compact:
        by_minutes: dict[int, list[str]] = {}
        for k, m in bonus.items():
            by_minutes.setdefault(m, []).append(cfg.service_name(k))
        lines += [f"เพิ่มเวลา **+{m} นาที/บิล** · {' · '.join(names)}" for m, names in by_minutes.items()]
        lines += [f"{cfg.service_name(k)} ซ้อนเวลาได้ (VIP 2 ท่าน = +{bonus[k] * 2} นาที)" for k in stack]
        if date_svc and per_day > 0:
            lines.append(
                f"**Free Date {int(date_svc.get('duration_minutes', 10))} นาที** วันละ {per_day} ครั้ง ทุกวันที่ร้านเปิด"
            )
        return lines
    if bonus:
        names = ", ".join(f"{cfg.service_name(k)} +{m} นาที" for k, m in bonus.items())
        lines.append(f"เพิ่มเวลาเข้าห้อง {names} ต่อบิล")
    for key in stack:
        lines.append(
            f"เพิ่มเวลาซ้อนกันได้เฉพาะ {cfg.service_name(key)} "
            f"(ลูกค้า VIP 2 ท่านเข้าห้องด้วยกัน = +{bonus[key] * 2} นาที)"
        )
    if date_svc and per_day > 0:
        lines.append(
            f"Free Date กับพนักงาน {int(date_svc.get('duration_minutes', 10))} นาที ได้ {per_day} ครั้ง "
            "ทุกวันที่ร้านเปิดทำการ (ขอเปลี่ยนพนักงานกลางคันไม่ได้)"
        )
    return lines


async def vip_customer_count(db: Database, customer_ids: list[int], now_local: dt.datetime) -> int:
    """จำนวนลูกค้าในบิลที่มี VIP ใช้งานอยู่"""
    count = 0
    for uid in dict.fromkeys(customer_ids):
        if await db.active_vip_member(uid, to_iso(now_local)):
            count += 1
    return count


async def vip_only_problem(cfg, db: Database, service_keys: list[str], customer_id: int, now_local: dt.datetime) -> str | None:
    """ตรวจบริการเฉพาะ VIP (Free Date): ต้องเป็น VIP และใช้ได้ไม่เกินวันละ free_date_per_day ครั้ง"""
    vip_keys = [k for k in dict.fromkeys(service_keys) if (cfg.service(k) or {}).get("vip_only")]
    if not vip_keys:
        return None
    if not cfg.vip_enabled or not await db.active_vip_member(customer_id, to_iso(now_local)):
        return f"**{cfg.service_names(vip_keys)}** ใช้ได้เฉพาะลูกค้าที่เป็นสมาชิก VIP (คนจ่ายบิล) ค่ะ"
    limit = int(vip_benefit(cfg, "free_date_per_day"))
    if limit <= 0:
        return "ตอนนี้ร้านปิดสิทธิ์ Free Date ของ VIP อยู่ค่ะ"
    since = to_iso(shop_day_start(cfg, now_local))
    rows = await db.fetchall(
        "SELECT services FROM jobs WHERE customer_id = ? AND status != 'CANCELLED' AND created_at >= ?",
        (customer_id, since),
    )
    for key in vip_keys:
        used = sum(json.loads(r["services"] or "[]").count(key) for r in rows)
        if used + service_keys.count(key) > limit:
            return f"วันนี้ลูกค้าใช้สิทธิ์ **{cfg.service_name(key)}** ครบ {limit} ครั้งแล้วค่ะ (รีเซ็ตทุกวันทำการ)"
    return None
