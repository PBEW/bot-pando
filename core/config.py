"""โหลด/เข้าถึงค่าตั้งค่าของบอทจากไฟล์ config.json"""
from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


class ConfigError(RuntimeError):
    pass


class Config:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        if not self.path.exists():
            example = self.path.with_name("config.example.json")
            if example.exists():
                shutil.copy(example, self.path)
            else:
                raise ConfigError(f"ไม่พบไฟล์ config: {self.path}")
        self.reload()

    # ------------------------------------------------------------------ base
    def reload(self) -> None:
        with self.path.open(encoding="utf-8") as fp:
            self.data: dict[str, Any] = json.load(fp)

    def save(self) -> None:
        with self.path.open("w", encoding="utf-8") as fp:
            json.dump(self.data, fp, ensure_ascii=False, indent=2)

    def get(self, dotted: str, default: Any = None) -> Any:
        node: Any = self.data
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    # --------------------------------------------------------------- general
    @property
    def guild_id(self) -> int:
        return int(self.get("guild_id", 0) or 0)

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.get("timezone", "Asia/Bangkok"))

    def channel_id(self, name: str) -> int:
        return int(self.get(f"channels.{name}", 0) or 0)

    @property
    def admin_role_id(self) -> int:
        return int(self.get("roles.admin", 0) or 0)

    @property
    def staff_role_ids(self) -> list[int]:
        """roles.staff ใส่ได้ทั้งตัวเลขเดียว หรือ list หลาย Role (เช่น แยกกลุ่มโฮสต์)"""
        raw = self.get("roles.staff", 0) or 0
        values = raw if isinstance(raw, list) else [raw]
        return [int(v) for v in values if int(v or 0)]

    @property
    def staff_role_id(self) -> int:
        ids = self.staff_role_ids
        return ids[0] if ids else 0

    @property
    def on_duty_role_id(self) -> int:
        return int(self.get("roles.on_duty", 0) or 0)

    @property
    def adult_role_ids(self) -> list[int]:
        raw = self.get("roles.adult_verified", 0) or 0
        values = raw if isinstance(raw, list) else [raw]
        return [int(v) for v in values if int(v or 0)]

    @property
    def shop_name(self) -> str:
        return str(self.get("shop_name", "Pandora's Night Heaven"))

    @property
    def vip_enabled(self) -> bool:
        return bool(self.get("features.vip", False))

    # ------------------------------------------------------------ attendance
    @property
    def attendance_warn_hours(self) -> float:
        return float(self.get("attendance.warn_hours", 12))

    @property
    def attendance_auto_close_hours(self) -> float:
        return float(self.get("attendance.auto_close_hours", 16))

    # -------------------------------------------------------------- services
    @property
    def services(self) -> list[dict]:
        return list(self.get("services", []) or [])

    def bookable_services(self) -> list[dict]:
        return [s for s in self.services if not s.get("extend_only") and not s.get("hidden")]

    def extend_services(self) -> list[dict]:
        """ตัวเลือกในเมนูต่อเวลา: แพ็กเกจต่อเวลา + บริการเสริมที่ต่อคู่กับแพ็กเกจต่อเวลาได้"""
        extend_keys = {s["key"] for s in self.services if s.get("extend_only")}
        return [
            s
            for s in self.services
            if s.get("extend_only") or extend_keys & set((s.get("addon_for") or {}).keys())
        ]

    def service(self, key: str) -> dict | None:
        return next((s for s in self.services if s["key"] == key), None)

    def service_name(self, key: str) -> str:
        svc = self.service(key)
        return svc["name"] if svc else key

    def service_names(self, keys: list[str]) -> str:
        """ชื่อบริการคั่นด้วยจุลภาค — key ที่ซ้ำกัน (บริการคิดต่อหน่วย) แสดงเป็น "ชื่อ ×N" """
        counts: dict[str, int] = {}
        for key in keys:
            counts[key] = counts.get(key, 0) + 1
        return ", ".join(
            self.service_name(k) + (f" ×{n}" if n > 1 else "") for k, n in counts.items()
        ) or "-"

    def service_pricing_entry(self, service: dict, tier_key: str | None) -> float | dict:
        """ค่า pricing ดิบของบริการตามระดับ VIP (ตัวเลข = ราคาคงที่, dict = มีเงื่อนไขโควต้า)"""
        pricing = service.get("pricing", {})
        key = tier_key or "normal"
        return pricing.get(key, pricing.get("normal", 0))

    @property
    def rooms(self) -> list[dict]:
        return list(self.get("rooms", []) or [])

    def rooms_for_services(self, keys: list[str]) -> list[dict]:
        """ห้องที่ใช้ได้กับบริการที่เลือก (ห้องที่ไม่ได้ระบุ services ใช้ได้ทุกบริการ)"""
        need = {k for k in keys if (self.service(k) or {}).get("require_room")}
        return [r for r in self.rooms if not r.get("services") or need <= set(r["services"])]

    def room_name(self, key: str | None) -> str:
        if not key:
            return "-"
        room = next((r for r in self.rooms if r["key"] == key), None)
        return room["name"] if room else key

    # ------------------------------------------------------------------- VIP
    @property
    def vip_tiers(self) -> list[dict]:
        """เรียงจากระดับต่ำไปสูงตาม rank เสมอ"""
        return sorted(self.get("vip_tiers", []) or [], key=lambda t: t.get("rank", 0))

    def vip_tier(self, key: str | None) -> dict | None:
        if not key:
            return None
        return next((t for t in self.vip_tiers if t["key"] == key), None)

    def vip_tier_rank(self, key: str | None) -> int:
        tier = self.vip_tier(key)
        return int(tier["rank"]) if tier else 0

    def vip_tier_name(self, key: str | None) -> str:
        tier = self.vip_tier(key)
        return tier["name"] if tier else "ลูกค้าทั่วไป"

    def purchasable_vip_tiers(self) -> list[dict]:
        return [t for t in self.vip_tiers if t.get("purchasable", True)]

    @property
    def vip_role_ids(self) -> list[int]:
        return [int(t["role_id"]) for t in self.vip_tiers if int(t.get("role_id") or 0)]

    @property
    def vip_packages(self) -> list[dict]:
        return list(self.get("vip_packages", []) or [])

    def vip_package(self, key: str) -> dict | None:
        return next((p for p in self.vip_packages if p["key"] == key), None)

    def vip_packages_for_tier(self, tier_key: str) -> list[dict]:
        return [p for p in self.vip_packages if p["tier"] == tier_key]

    def discount(self, code: str) -> dict | None:
        codes = self.get("discount_codes", {}) or {}
        return codes.get(code.strip().upper())

    # ------------------------------------------------------------ top donate
    @property
    def top_donate_enabled(self) -> bool:
        return bool(self.get("top_donate.enabled", True))

    @property
    def top_donate_min(self) -> float:
        return float(self.get("top_donate.min_amount", 1000))

    @property
    def top_donate_size(self) -> int:
        return int(self.get("top_donate.leaderboard_size", 5))

    # ---------------------------------------------------------------- shares
    def staff_percent(self, staff_id: int) -> float:
        table = self.get("revenue_share.staff_percent", {}) or {}
        if str(staff_id) in table:
            return float(table[str(staff_id)])
        return float(self.get("revenue_share.default_staff_percent", 60))

    # --------------------------------------------------------------- numbers
    @property
    def loop_seconds(self) -> int:
        return int(self.get("notify.loop_seconds", 35))

    @property
    def before_start_minutes(self) -> int:
        return int(self.get("notify.before_start_minutes", 5))

    @property
    def before_end_minutes(self) -> int:
        return int(self.get("notify.before_end_minutes", 5))

    @property
    def ticket_timeout_minutes(self) -> int:
        return int(self.get("ticket.timeout_minutes", 10))

    @property
    def review_window_hours(self) -> int:
        return int(self.get("review.window_hours", 24))

    @property
    def review_color(self) -> int:
        raw = str(self.get("review.embed_color", "#FF6FA5")).lstrip("#")
        try:
            return int(raw, 16)
        except ValueError:
            return 0xFF6FA5

    @property
    def cutoff_weekday(self) -> int:
        """0 = จันทร์ ... 5 = เสาร์ (ตาม datetime.weekday())"""
        return int(self.get("cutoff.weekday", 5))

    @property
    def cutoff_hour(self) -> int:
        return int(self.get("cutoff.hour", 8))

    @property
    def cutoff_minute(self) -> int:
        return int(self.get("cutoff.minute", 0))
