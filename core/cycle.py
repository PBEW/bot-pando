"""คำนวณรอบตัดยอด/จ่ายพนักงาน

- รายวัน (cutoff.daily = true, ค่าเริ่มต้น): รอบ = วันทำงานของร้าน ตัดที่เวลาตัดยอดเข้างาน
  (attendance.day_cutoff_hour, ค่าเริ่มต้นตี 1) · Google Sheets มีแต่แท็บรายวัน Day_
- รายสัปดาห์ (cutoff.daily = false): ตัดตาม cutoff.weekday/hour/minute · แท็บ Cycle_ + Day_
"""
from __future__ import annotations

import datetime as dt

from .config import Config


def is_daily(cfg: Config) -> bool:
    return bool(cfg.get("cutoff.daily", True))


def cycle_length(cfg: Config) -> dt.timedelta:
    return dt.timedelta(days=1 if is_daily(cfg) else 7)


def shop_day_start_local(cfg: Config, now_local: dt.datetime) -> dt.datetime:
    """จุดเริ่มวันทำงานของร้านล่าสุด (เวลาตัดยอดเข้างาน)"""
    hour = int(cfg.get("attendance.day_cutoff_hour", 1))
    start = now_local.replace(hour=hour, minute=0, second=0, microsecond=0)
    if start > now_local:
        start -= dt.timedelta(days=1)
    return start


def cycle_start_local(now_local: dt.datetime, cfg: Config) -> dt.datetime:
    """เวลาเริ่มต้นของรอบปัจจุบัน (จุดตัดยอดล่าสุดที่ผ่านมาแล้ว)"""
    if is_daily(cfg):
        return shop_day_start_local(cfg, now_local)
    candidate = now_local.replace(
        hour=cfg.cutoff_hour, minute=cfg.cutoff_minute, second=0, microsecond=0
    )
    candidate -= dt.timedelta(days=(candidate.weekday() - cfg.cutoff_weekday) % 7)
    if candidate > now_local:
        candidate -= dt.timedelta(days=7)
    return candidate


def next_cutoff_local(now_local: dt.datetime, cfg: Config) -> dt.datetime:
    return cycle_start_local(now_local, cfg) + cycle_length(cfg)


def cycle_title(cfg: Config, now_local: dt.datetime | None = None) -> str:
    """แท็บสรุปรอบจ่ายพนักงาน — รายวันใช้แท็บ Day_ เดียวกับแท็บรายวัน (ไม่มี Cycle_ แยก)"""
    now_local = now_local or dt.datetime.now(cfg.tz)
    if is_daily(cfg):
        return day_title(cfg, now_local)
    return f"Cycle_{cycle_start_local(now_local, cfg).date().isoformat()}"


def sheet_titles(cfg: Config, now_local: dt.datetime | None = None) -> list[str]:
    """แท็บที่บิลหนึ่งต้องลง (ไม่ซ้ำกัน): รายวันอย่างเดียว หรือ สัปดาห์ + รายวัน"""
    return list(dict.fromkeys((cycle_title(cfg, now_local), day_title(cfg, now_local))))


def shop_day_local(cfg: Config, now_local: dt.datetime | None = None) -> dt.date:
    """วันทำงานของร้าน — ก่อนเวลาตัดยอดเข้างาน (attendance.day_cutoff_hour, ค่าเริ่มต้นตี 1) นับเป็นของเมื่อวาน"""
    now_local = now_local or dt.datetime.now(cfg.tz)
    return shop_day_start_local(cfg, now_local).date()


def day_title(cfg: Config, now_local: dt.datetime | None = None) -> str:
    """ชื่อแท็บรายวันใน Google Sheets (แท็บ Cycle_ = สรุปทั้งสัปดาห์)"""
    return f"Day_{shop_day_local(cfg, now_local).isoformat()}"
