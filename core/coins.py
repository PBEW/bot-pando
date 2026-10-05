"""เหรียญ Pandora: บัญชีเหรียญ, อัตราได้เหรียญ, รางวัล และส่วนลดจากคูปอง

กติกา
- ได้เหรียญจากบิลที่ชำระแล้ว (1 เหรียญ / baht_per_coin บาท) · โดเนทใช้ donate_baht_per_coin
- เหรียญซื้อไม่ได้ แลกเป็นเงินไม่ได้ โอนไม่ได้ · บิลที่จ่ายด้วยคูปองฟรีไม่ได้เหรียญจากส่วนที่ฟรี
- รางวัลห้ามผูกกับบริการ 18+ (การรับงานขึ้นกับความสบายใจของพนักงานเสมอ)
- ยอดสะสมตลอดชีพ (lifetime) นับเฉพาะ EARN / REVOKE / ADMIN — การแลกของไม่ทำให้อันดับลด
"""
from __future__ import annotations

from .config import Config
from .database import Database
from .utils import now_utc, to_iso

LIFETIME_KINDS = ("EARN", "REVOKE", "ADMIN")

DEFAULTS = {
    "enabled": True,
    "name": "เหรียญ Pandora",
    "emoji": "🪙",
    "baht_per_coin": 10,
    "donate_baht_per_coin": 20,
    "first_visit_bonus": 20,
    "review_bonus": 5,
    "top_donate_bonus": 100,
    "event_multiplier": 1,
    "voucher_days": 30,
    "expire_inactive_days": 180,
    "collector_lifetime": 1000,
    "collector_role_id": 0,
}

DEFAULT_REWARDS = [
    {"key": "shoutout", "name": "ประกาศขอบคุณในห้องประกาศ", "emoji": "📣", "cost": 20, "type": "shoutout"},
    {"key": "color_role", "name": "Role สีพิเศษ 30 วัน", "emoji": "🎨", "cost": 40, "type": "role", "role_id": 0, "days": 30},
    {"key": "free_shot", "name": "Drink Friend ฟรี 1 shot", "emoji": "🥃", "cost": 60, "type": "service_free", "service": "drink_friend", "qty": 1},
    {"key": "priority", "name": "จองพนักงานคนโปรดก่อน 1 ครั้ง", "emoji": "⭐", "cost": 80, "type": "manual"},
    {"key": "free_short_date", "name": "Short Date ฟรี 20 นาที", "emoji": "☕", "cost": 100, "type": "service_free", "service": "short_date", "qty": 1},
    {"key": "karaoke_half", "name": "Karaoke ลด 50%", "emoji": "🎤", "cost": 120, "type": "service_discount", "service": "karaoke", "percent": 50},
]

# ประเภทคูปองที่แอดมินเลือกใช้ตอนเปิดบิล (role / shoutout ใช้ทันทีตอนแลก)
BILL_VOUCHER_TYPES = ("service_free", "service_discount", "manual")


def opt(cfg: Config, key: str):
    return cfg.get(f"coins.{key}", DEFAULTS[key])


def enabled(cfg: Config) -> bool:
    return bool(opt(cfg, "enabled"))


def label(cfg: Config) -> str:
    return f"{opt(cfg, 'emoji')} {opt(cfg, 'name')}"


def rewards(cfg: Config) -> list[dict]:
    items = cfg.get("coins.rewards") or DEFAULT_REWARDS
    return sorted(items, key=lambda r: int(r.get("cost", 0)))


def reward(cfg: Config, key: str) -> dict | None:
    return next((r for r in rewards(cfg) if r["key"] == key), None)


# ------------------------------------------------------------------ ยอด
async def balance(db: Database, user_id: int) -> int:
    row = await db.fetchone("SELECT COALESCE(SUM(delta), 0) AS b FROM coin_ledger WHERE user_id = ?", (user_id,))
    return int(row["b"]) if row else 0


async def lifetime(db: Database, user_id: int) -> int:
    holders = ",".join("?" for _ in LIFETIME_KINDS)
    row = await db.fetchone(
        f"SELECT COALESCE(SUM(delta), 0) AS b FROM coin_ledger WHERE user_id = ? AND kind IN ({holders})",
        (user_id, *LIFETIME_KINDS),
    )
    return max(int(row["b"]) if row else 0, 0)


async def add(
    db: Database, user_id: int, delta: int, kind: str, reason: str, *, ref: str | None = None, by: int | None = None
) -> int:
    """บันทึกการเปลี่ยนแปลงเหรียญ แล้วคืนยอดคงเหลือใหม่"""
    if delta:
        await db.execute(
            "INSERT INTO coin_ledger (user_id, delta, kind, reason, ref, by_user, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (user_id, int(delta), kind, reason, ref, by, to_iso(now_utc())),
        )
    return await balance(db, user_id)


async def earned_for_ref(db: Database, ref_prefix: str) -> list[dict]:
    """ยอดสุทธิที่ได้ไปต่อคนจากรายการอ้างอิง (เช่น job:12) — ใช้ตอนดึงเหรียญคืน"""
    return await db.fetchall(
        "SELECT user_id, SUM(delta) AS total FROM coin_ledger WHERE ref LIKE ? AND kind IN ('EARN','REVOKE') "
        "GROUP BY user_id HAVING SUM(delta) != 0",
        (f"{ref_prefix}%",),
    )


async def leaderboard(db: Database, limit: int = 10) -> list[dict]:
    holders = ",".join("?" for _ in LIFETIME_KINDS)
    return await db.fetchall(
        f"SELECT user_id, SUM(delta) AS total FROM coin_ledger WHERE kind IN ({holders}) "
        "GROUP BY user_id HAVING SUM(delta) > 0 ORDER BY total DESC LIMIT ?",
        (*LIFETIME_KINDS, limit),
    )


# ------------------------------------------------------------ ได้เหรียญ
def bill_coins(cfg: Config, job: dict) -> int:
    """เหรียญจากบิลที่ชำระแล้ว (คิดจากยอดที่จ่ายจริง หลังหักคูปอง)"""
    paid = float(job.get("total_price") or 0)
    if paid <= 0:
        return 0
    if job.get("job_type") == "DONATE":
        return int(paid // float(opt(cfg, "donate_baht_per_coin")))
    multiplier = max(float(opt(cfg, "event_multiplier")), 1.0)
    return int(paid // float(opt(cfg, "baht_per_coin")) * multiplier)


# --------------------------------------------------------------- คูปอง
def voucher_discount(cfg: Config, reward_item: dict | None, service_keys: list[str], amounts: dict[str, float]) -> tuple[float, str | None]:
    """คืน (ส่วนลด, ปัญหา) — ปัญหาไม่ใช่ None แปลว่าคูปองนี้ใช้กับบิลนี้ไม่ได้"""
    if reward_item is None:
        return 0.0, "ไม่พบรางวัลของคูปองนี้ในระบบแล้ว"
    kind = reward_item.get("type")
    if kind == "manual":
        return 0.0, None
    key = reward_item.get("service")
    svc = cfg.service(key) if key else None
    if svc is None:
        return 0.0, "บริการของคูปองนี้ถูกลบไปแล้ว"
    if svc.get("adult_only"):
        return 0.0, "คูปองใช้กับบริการ 18+ ไม่ได้"
    if key not in service_keys:
        return 0.0, f"คูปองนี้ใช้กับ **{svc['name']}** — ต้องเลือกบริการนี้ในบิลด้วย"
    if kind == "service_free":
        unit = float(svc.get("pricing", {}).get("normal", 0))
        qty = min(int(reward_item.get("qty", 1)), service_keys.count(key))
        return round(unit * qty, 2), None
    if kind == "service_discount":
        base = float(amounts.get(key, 0))
        return round(base * float(reward_item.get("percent", 0)) / 100.0, 2), None
    return 0.0, "คูปองนี้ใช้ตอนเปิดบิลไม่ได้"


async def active_vouchers(db: Database, user_id: int, *, bill_only: bool = False, cfg: Config | None = None) -> list[dict]:
    rows = await db.fetchall(
        "SELECT * FROM coin_vouchers WHERE user_id = ? AND status = 'ACTIVE' AND expires_at > ? ORDER BY expires_at",
        (user_id, to_iso(now_utc())),
    )
    if bill_only and cfg is not None:
        rows = [r for r in rows if (reward(cfg, r["reward_key"]) or {}).get("type") in BILL_VOUCHER_TYPES]
    return rows
