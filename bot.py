"""Pandora — บอทบริหารร้าน Pandora's Night Heaven บน Discord

รันด้วย:  python bot.py
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

import discord
from discord.ext import commands
from dotenv import load_dotenv

from core.config import Config, ConfigError
from core.database import Database
from core.lockfile import SingleInstanceLock
from core.sheets import SheetsClient

BASE_DIR = Path(__file__).resolve().parent

EXTENSIONS = [
    "cogs.payments",
    "cogs.reception",
    "cogs.tickets",
    "cogs.reviews",
    "cogs.requestpanel",
    "cogs.scheduler",
    "cogs.attendance",
    "cogs.dmrouter",
    "cogs.admin",
    "cogs.adminpanel",
    "cogs.staffpanel",
    "cogs.dailycheck",
    "cogs.donate",
    "cogs.settings",
    "cogs.coins",
]


def setup_logging() -> None:
    log_dir = BASE_DIR / "logs"
    log_dir.mkdir(exist_ok=True)

    fmt = logging.Formatter("[{asctime}] [{levelname:<7}] {name}: {message}", "%Y-%m-%d %H:%M:%S", "{")

    stream = logging.StreamHandler()
    stream.setFormatter(fmt)

    file_handler = RotatingFileHandler(
        log_dir / "pandora.log", maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(stream)
    root.addHandler(file_handler)
    logging.getLogger("discord.http").setLevel(logging.WARNING)


log = logging.getLogger("olp")


class PandoraBot(commands.Bot):
    def __init__(self, cfg: Config, db_path: str) -> None:
        intents = discord.Intents.default()
        intents.members = True           # ต้องเปิด Server Members Intent ใน Developer Portal
        intents.message_content = True   # ต้องเปิด Message Content Intent (ใช้กับสะพานแชท DM)

        super().__init__(
            command_prefix=commands.when_mentioned,
            intents=intents,
            help_command=None,
            activity=discord.Activity(type=discord.ActivityType.watching, name="Pandora's Night Heaven 💜"),
        )
        self.cfg = cfg
        self.db = Database(db_path)
        self.sheets = SheetsClient(cfg)

    async def setup_hook(self) -> None:
        await self.db.connect()
        await self.sheets.start()

        extensions = list(EXTENSIONS)
        # โหลด VIP เสมอ — เปิด/ปิดได้ที่ ⚙️ ตั้งค่าร้าน → VIP โดยไม่ต้องรีสตาร์ต (ปุ่มตอบว่าปิดใช้งานเองตอนปิด)
        extensions.insert(3, "cogs.vip")
        for ext in extensions:
            await self.load_extension(ext)
            log.info("โหลด extension: %s", ext)

        await self.sync_commands()

    async def sync_commands(self) -> None:
        """ซิงก์ slash command เฉพาะตอนคำสั่งเปลี่ยน — รีสตาร์ตบ่อยจะไม่ยิง API ซ้ำทุกครั้ง (ลดโอกาสโดน rate limit)"""
        guild = discord.Object(id=self.cfg.guild_id) if self.cfg.guild_id else None
        if guild is not None:
            self.tree.copy_global_to(guild=guild)
        payload = sorted(
            (json.dumps(cmd.to_dict(self.tree), sort_keys=True, ensure_ascii=False)
             for cmd in self.tree.get_commands(guild=guild)),
        )
        digest = hashlib.sha256(f"{self.cfg.guild_id}|{payload}".encode()).hexdigest()
        if await self.db.get_meta("command_hash") == digest:
            log.info("slash command ไม่เปลี่ยน ข้ามการซิงก์")
            return
        try:
            synced = await self.tree.sync(guild=guild)
        except discord.HTTPException as exc:
            # ซิงก์ไม่ได้ (เช่นโดน rate limit) ไม่ต้องให้บอทล่ม — คำสั่งเดิมยังใช้ได้ รอบหน้าค่อยซิงก์ใหม่
            log.warning("ซิงก์ slash command ไม่สำเร็จ (%s) จะลองใหม่ตอนเปิดบอทครั้งหน้า", exc)
            return
        await self.db.set_meta("command_hash", digest)
        where = f"กิลด์ {self.cfg.guild_id}" if guild else "แบบ global (อาจใช้เวลาถึง 1 ชม.)"
        log.info("ซิงก์ slash command %d รายการ %s", len(synced), where)

    async def on_ready(self) -> None:
        log.info("เข้าสู่ระบบในชื่อ %s (id=%s)", self.user, self.user.id)
        if not self.cfg.guild_id:
            log.warning("ยังไม่ได้ตั้งค่า guild_id ใน config.json")
        if not self.cfg.channel_id("admin"):
            log.warning("ยังไม่ได้ตั้งค่า channels.admin ใน config.json")

    async def close(self) -> None:
        await self.db.close()
        await super().close()


async def main() -> None:
    setup_logging()
    load_dotenv(BASE_DIR / ".env")

    token = os.getenv("DISCORD_TOKEN", "").strip()
    if not token:
        log.error("ไม่พบ DISCORD_TOKEN — คัดลอก .env.example เป็น .env แล้วใส่โทเคนก่อนค่ะ")
        sys.exit(1)

    try:
        cfg = Config(BASE_DIR / os.getenv("PANDORA_CONFIG", "config.json"))
    except ConfigError as exc:
        log.error("%s", exc)
        sys.exit(1)

    db_path = str(BASE_DIR / os.getenv("PANDORA_DB", "data/pandora.sqlite3"))

    # กันรันซ้อน: ถ้ามีบอทตัวอื่นอยู่แล้ว ทุกอย่างจะทำงานซ้ำ (DM ซ้ำ, ลงบัญชีซ้ำ, ให้ Role ซ้ำ)
    lock = SingleInstanceLock(BASE_DIR / "data" / "bot.lock")
    if not lock.acquire():
        log.error(
            "มีบอทตัวอื่นรันอยู่แล้ว จึงไม่เปิดตัวใหม่ซ้ำ\n"
            "  หากรันซ้อนกัน ลูกค้าจะได้ DM ซ้ำ และบัญชีจะถูกบันทึกซ้ำแถว\n"
            "  ให้ปิดหน้าต่าง terminal เดิมที่รันบอทอยู่ก่อน (กด Ctrl+C ในหน้าต่างนั้น)\n"
            "  ตรวจสอบว่ามีตัวไหนรันอยู่บ้างด้วยคำสั่ง PowerShell:\n"
            "    Get-CimInstance Win32_Process -Filter \"Name like '%python%'\" |"
            " Select-Object ProcessId, CommandLine"
        )
        sys.exit(1)

    # โดน Discord บล็อกชั่วคราว (429) ตอนเข้าสู่ระบบ: รอแล้วค่อยลองใหม่ในตัว
    # ไม่ปล่อยให้โปรแกรมปิดแล้วโฮสต์เปิดใหม่ทันทีซ้ำๆ ซึ่งทำให้โดนบล็อกนานขึ้น
    waits = [300, 600, 1200, 1800]  # วินาที
    try:
        for attempt in range(len(waits) + 1):
            bot = PandoraBot(cfg, db_path)
            try:
                async with bot:
                    await bot.start(token)
                return
            except discord.HTTPException as exc:
                if exc.status != 429 or attempt == len(waits):
                    raise
                wait = waits[attempt]
                log.error(
                    "Discord บล็อกการเชื่อมต่อชั่วคราว (429 rate limit) — รอ %d นาทีแล้วลองใหม่ (ครั้งที่ %d)\n"
                    "  อย่ากดรีสตาร์ตบอทระหว่างนี้ ยิ่งรีสตาร์ตยิ่งโดนบล็อกนาน",
                    wait // 60, attempt + 1,
                )
                await asyncio.sleep(wait)
    finally:
        lock.release()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("ปิดบอทแล้ว")
