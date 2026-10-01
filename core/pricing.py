"""คำนวณราคา, ระยะเวลา, โควต้าฟรีรายเดือน และส่วนแบ่งรายได้"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from .config import Config
from .database import Database
from .vip_logic import cycle_month_key


@dataclass
class Quote:
    services: list[str]
    total_price: float
    duration_minutes: int
    tier: str | None
    quota_services: list[str] = field(default_factory=list)  # บริการที่ใช้สิทธิ์ฟรีในรอบนี้
    lines: list[tuple[str, float]] = field(default_factory=list)

    @property
    def breakdown(self) -> str:
        return "\n".join(f"• {name} — {price:,.0f} บาท" for name, price in self.lines) or "-"


def staff_count_extra(svc: dict, staff_count: int) -> int:
    """จำนวนพนักงานที่เกินจากที่รวมในราคาแล้ว (เฉพาะบริการ multi_staff)"""
    if not svc.get("multi_staff"):
        return 0
    return max(0, staff_count - int(svc.get("included_staff", 1)))


def validate_selection(cfg: Config, service_keys: list[str], staff_count: int) -> str | None:
    """ตรวจว่าชุดบริการ/จำนวนพนักงานที่เลือกใช้ได้จริง คืนข้อความปัญหา หรือ None ถ้าผ่าน"""
    keys = set(service_keys)
    services = [cfg.service(k) for k in keys]
    services = [s for s in services if s]

    for svc in services:
        addon_for = svc.get("addon_for")
        if addon_for and not keys & set(addon_for):
            bases = ", ".join(cfg.service_name(k) for k in addon_for if cfg.service(k))
            return f"**{svc['name']}** เป็นบริการเสริม ต้องเลือกคู่กับ: {bases}"

    multi = [s for s in services if s.get("multi_staff")]
    if staff_count > 1 and not multi:
        return "เลือกพนักงานหลายคนได้เฉพาะบริการที่รองรับ (เช่น Private Party Room) ค่ะ"
    for svc in multi:
        limit = int(svc.get("max_staff", 10))
        if staff_count > limit:
            return f"**{svc['name']}** รองรับพนักงานสูงสุด {limit} คนค่ะ"
    return None


async def quote_services(
    cfg: Config,
    db: Database,
    service_keys: list[str],
    *,
    customer_id: int,
    tier: str | None,
    staff_count: int = 1,
    now_local: dt.datetime | None = None,
) -> Quote:
    """รวมราคาและระยะเวลาของบริการที่เลือก

    - key ซ้ำ = บริการคิดต่อหน่วย (per_unit เช่น Drink Friend 3 shot) คิดราคาทุกหน่วย แต่นับเวลาครั้งเดียว
    - บริการ multi_staff คิดค่าพนักงานที่เกิน included_staff เพิ่มคนละ extra_staff_price
    - บริการเสริม (addon_for) บวกเวลาเพิ่มตามบริการหลักที่เลือกคู่กัน (ใช้ค่าที่มากที่สุด)

    ไม่ล็อกโควต้าให้ทันที — เป็นแค่การ "ดูตัวอย่าง" (นับสิทธิ์ที่ใช้ไปแล้วในเดือนนี้จริง
    แต่ยังไม่บันทึกเพิ่ม) ต้องเรียก reserve_quota_for_job แยกตอนสร้างบิลจริงเท่านั้น
    """
    now_local = now_local or dt.datetime.now(cfg.tz)
    cycle = cycle_month_key(now_local)
    selected = set(service_keys)

    total = 0.0
    duration = 0
    lines: list[tuple[str, float]] = []
    quota_services: list[str] = []
    seen: set[str] = set()

    for key in service_keys:
        svc = cfg.service(key)
        if svc is None:
            continue
        if key in seen:
            if not svc.get("per_unit"):
                continue
        else:
            seen.add(key)
            duration += int(svc.get("duration_minutes", 60))
            bonus = [int(m) for base, m in (svc.get("addon_for") or {}).items() if base in selected]
            if bonus:
                duration += max(bonus)

        entry = cfg.service_pricing_entry(svc, tier)

        if isinstance(entry, dict):
            if entry.get("unlimited"):
                price = 0.0
                lines.append((f"{svc['name']} (สิทธิ์ไม่จำกัด)", 0.0))
            else:
                free_limit = int(entry.get("free_per_month", 0))
                used = await db.get_quota_used(customer_id, key, cycle)
                if used < free_limit and key not in quota_services:
                    price = 0.0
                    quota_services.append(key)
                    lines.append((f"{svc['name']} (สิทธิ์ฟรี {used + 1}/{free_limit} เดือนนี้)", 0.0))
                else:
                    price = float(entry.get("after_price", 0))
                    lines.append((f"{svc['name']} (ใช้สิทธิ์ฟรีครบแล้ว)", price))
        else:
            price = float(entry)
            lines.append((svc["name"], price))

        extra = staff_count_extra(svc, staff_count)
        if extra:
            extra_price = extra * float(svc.get("extra_staff_price", 0))
            lines.append((f"พนักงานเพิ่ม ({svc['name']}) {extra} คน", extra_price))
            price += extra_price

        total += price

    lines = _merge_unit_lines(lines)
    return Quote(
        services=list(service_keys),
        total_price=total,
        duration_minutes=duration,
        tier=tier,
        quota_services=quota_services,
        lines=lines,
    )


def _merge_unit_lines(lines: list[tuple[str, float]]) -> list[tuple[str, float]]:
    """รวมบรรทัดชื่อซ้ำ (บริการคิดต่อหน่วย) เป็น "ชื่อ ×N" """
    merged: dict[str, list[float]] = {}
    for name, price in lines:
        row = merged.setdefault(name, [0, 0.0])
        row[0] += 1
        row[1] += price
    return [(name + (f" ×{int(n)}" if n > 1 else ""), total) for name, (n, total) in merged.items()]


async def reserve_quota_for_job(db: Database, customer_id: int, quote: Quote, cycle: str) -> None:
    """ล็อกสิทธิ์ฟรีที่ตัดสินใจใช้จริงตอนสร้างบิล (เรียกครั้งเดียวตอน create_job)"""
    for key in quote.quota_services:
        await db.reserve_quota(customer_id, key, cycle)


async def release_quota_for_job(db: Database, customer_id: int, quota_services: list[str], cycle: str) -> None:
    """คืนสิทธิ์ฟรีที่เคยล็อกไว้ (เรียกตอนยกเลิกบิล)"""
    for key in quota_services:
        await db.release_quota(customer_id, key, cycle)


def split_revenue(cfg: Config, staff_ids: int | list[int], total_price: float) -> tuple[float, float]:
    """คืนค่า (ส่วนแบ่งพนักงานรวมทุกคน, รายได้เข้าร้าน)

    บิลที่มีพนักงานหลายคน (Party Room) แบ่งยอดบิลเท่ากันทุกคน แล้วคิดเปอร์เซ็นต์ของแต่ละคน
    """
    ids = [staff_ids] if isinstance(staff_ids, int) else list(staff_ids)
    portion = total_price / max(len(ids), 1)
    staff_share = round(sum(portion * cfg.staff_percent(sid) / 100.0 for sid in ids), 2)
    shop_share = round(total_price - staff_share, 2)
    return staff_share, shop_share


def job_staff_ids(job: dict) -> list[int]:
    """พนักงานทุกคนในบิล (คนแรก = พนักงานหลักที่กดรับงาน)"""
    return [job["staff_id"], *[sid for sid in job.get("co_staff") or [] if sid != job["staff_id"]]]


def job_staff_split(cfg: Config, job: dict) -> list[tuple[int, float, float]]:
    """แยกยอดบิลรายพนักงาน -> [(staff_id, ยอดบิลส่วนของคนนี้, ส่วนแบ่งของคนนี้)]

    ส่วนแบ่งแยกตามสัดส่วนเปอร์เซ็นต์ของแต่ละคน โดยผลรวมเท่ากับ staff_share ที่บันทึกไว้ในบิลเสมอ
    """
    ids = job_staff_ids(job)
    portion = job["total_price"] / len(ids)
    weights = [cfg.staff_percent(sid) for sid in ids]
    weight_sum = sum(weights) or len(ids)
    return [
        (sid, round(portion, 2), round(job["staff_share"] * (w or 1) / weight_sum, 2))
        for sid, w in zip(ids, weights)
    ]


def apply_discount(cfg: Config, code: str | None, price: float) -> tuple[float, float, str | None]:
    """คืนค่า (ยอดสุทธิ, ส่วนลด, โค้ดที่ใช้ได้จริง)"""
    if not code:
        return price, 0.0, None

    rule = cfg.discount(code)
    if rule is None:
        return price, 0.0, None

    if rule.get("type") == "percent":
        discount = round(price * float(rule.get("value", 0)) / 100.0, 2)
    else:
        discount = float(rule.get("value", 0))

    discount = min(discount, price)
    return round(price - discount, 2), discount, code.strip().upper()
