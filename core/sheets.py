"""บันทึกบัญชีลง Google Sheets (ทำงานแบบ optional — ปิดได้ใน config)

ชีตรอบบิล (Cycle_YYYY-MM-DD): ตาราง A:P + บล็อกสรุปด้านขวา Q:S
ชีต Attendance: บันทึกเข้างานรายวัน
ทุกชีตจัดรูปแบบให้อ่านง่าย (หัวตารางสีม่วง, แยกสีคอลัมน์เงิน, ไฮไลต์โดเนท/ต่อเวลา) — สั่งจัดใหม่ได้ด้วย /sheets_format
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
from pathlib import Path

from .config import Config

log = logging.getLogger("olp.sheets")

# ลำดับคอลัมน์ต้องตรงกับแถวที่ PaymentsCog.log_job_to_sheet เขียน (A..P)
HEADERS = [
    "เลขบิล",                   # A
    "วันที่",                    # B
    "เริ่ม",                     # C
    "จบ",                       # D
    "ลูกค้า",                    # E
    "ID ลูกค้า",                 # F (ซ่อน)
    "พนักงาน",                  # G
    "ID พนักงาน",               # H (ซ่อน)
    "บริการ",                    # I
    "ห้อง",                      # J
    "💰 ยอดบิล (In)",            # K
    "💃 ส่วนแบ่งพนักงาน (Out)",   # L
    "🏠 รายได้ร้าน",              # M
    "ประเภท",                   # N
    "ระดับ VIP",                 # O (ซ่อน — ร้านไม่ใช้ VIP)
    "หมายเหตุ",                 # P
]

# บล็อกสรุปด้านขวา (คอลัมน์ Q:S)
COIN_SHEET = "เหรียญ Pandora"
COIN_HEADERS = ["เวลา", "ลูกค้า", "ID", "+/- เหรียญ", "ประเภท", "รายละเอียด", "คงเหลือ"]

PAYOUT_SHEET = "บัญชีพนักงาน"
PAYOUT_HEADERS = ["พนักงาน", "ID พนักงาน", "ธนาคาร / ช่องทาง", "เลขบัญชี / พร้อมเพย์", "ชื่อบัญชี", "อัปเดตล่าสุด"]

# บล็อกสรุปด้านขวา (คอลัมน์ Q:T) — คอลัมน์ T ดึงบัญชีรับเงินจากแท็บบัญชีพนักงาน (จับคู่ด้วยชื่อพนักงาน)
_PAYOUT_LOOKUP = (
    "=ARRAYFORMULA(IF(Q8:Q=\"\",,IFERROR(VLOOKUP(Q8:Q,{"
    f"'{PAYOUT_SHEET}'!A2:A,'{PAYOUT_SHEET}'!C2:C&\" \"&'{PAYOUT_SHEET}'!D2:D&\" · \"&'{PAYOUT_SHEET}'!E2:E"
    "},2,FALSE),\"⚠️ ยังไม่ได้ให้ข้อมูล\")))"
)
SUMMARY_BLOCK = [
    ["📊 สรุปรอบนี้", "", "", ""],
    ["💰 ยอดบิลรวม (In)", "=SUM(K2:K)", "", ""],
    ["💃 จ่ายพนักงาน (Out)", "=SUM(L2:L)", "", ""],
    ["🏠 รายได้เข้าร้าน", "=SUM(M2:M)", "", ""],
    # บิลที่มีพนักงานหลายคนเขียนแถวละคน (เลขบิลซ้ำ) จึงต้องนับแบบไม่ซ้ำ
    ["🧾 จำนวนบิล", "=COUNTUNIQUE(A2:A)", "", ""],
    ["", "", "", ""],
    ["พนักงาน", "ยอดบิล", "ส่วนแบ่ง (ต้องโอน)", "💳 บัญชีรับเงิน"],
    [
        "=IFERROR(UNIQUE(FILTER(G2:G,G2:G<>\"\")),\"\")",
        "=ARRAYFORMULA(IF(Q8:Q=\"\",,SUMIF(G:G,Q8:Q,K:K)))",
        "=ARRAYFORMULA(IF(Q8:Q=\"\",,SUMIF(G:G,Q8:Q,L:L)))",
        _PAYOUT_LOOKUP,
    ],
]

ATTENDANCE_SHEET = "Attendance"
ATTENDANCE_HEADERS = [
    "เลขที่",        # A
    "วันที่",        # B
    "เข้างาน",       # C
    "ออกงาน",       # D
    "พนักงาน",      # E
    "ID พนักงาน",   # F (ซ่อน)
    "ชั่วโมง",       # G
    "หมายเหตุ",     # H
]

# ---------------------------------------------------------------- สี
PURPLE = "#6A1B9A"
PURPLE_DARK = "#4A148C"
PURPLE_LIGHT = "#F3E5F5"
GREEN, GREEN_LIGHT = "#2E7D32", "#E8F5E9"
ORANGE, ORANGE_LIGHT = "#EF6C00", "#FFF3E0"
BLUE, BLUE_LIGHT = "#1565C0", "#E3F2FD"
PINK_LIGHT = "#FCE4EC"
YELLOW_LIGHT = "#FFF8E1"
WHITE = "#FFFFFF"
MONEY = "#,##0.00"


def _rgb(hex_color: str) -> dict:
    h = hex_color.lstrip("#")
    return {"red": int(h[0:2], 16) / 255, "green": int(h[2:4], 16) / 255, "blue": int(h[4:6], 16) / 255}


def _range(sheet_id: int, r1: int, r2: int | None, c1: int, c2: int) -> dict:
    """ช่วงเซลล์แบบ 0-based (r2=None = ถึงแถวสุดท้าย)"""
    rng = {"sheetId": sheet_id, "startRowIndex": r1, "startColumnIndex": c1, "endColumnIndex": c2}
    if r2 is not None:
        rng["endRowIndex"] = r2
    return rng


def _cell(sheet_id, r1, r2, c1, c2, fmt: dict) -> dict:
    fields = ",".join(f"userEnteredFormat.{k}" for k in fmt)
    return {"repeatCell": {"range": _range(sheet_id, r1, r2, c1, c2), "cell": {"userEnteredFormat": fmt}, "fields": fields}}


def _header_fmt(bg: str, *, size: int = 10) -> dict:
    return {
        "backgroundColor": _rgb(bg),
        "textFormat": {"bold": True, "foregroundColor": _rgb(WHITE), "fontSize": size},
        "horizontalAlignment": "CENTER",
        "verticalAlignment": "MIDDLE",
        "wrapStrategy": "WRAP",
    }


def _width(sheet_id: int, col: int, px: int) -> dict:
    return {
        "updateDimensionProperties": {
            "range": {"sheetId": sheet_id, "dimension": "COLUMNS", "startIndex": col, "endIndex": col + 1},
            "properties": {"pixelSize": px},
            "fields": "pixelSize",
        }
    }


def _hide(sheet_id: int, col: int) -> dict:
    return {
        "updateDimensionProperties": {
            "range": {"sheetId": sheet_id, "dimension": "COLUMNS", "startIndex": col, "endIndex": col + 1},
            "properties": {"hiddenByUser": True},
            "fields": "hiddenByUser",
        }
    }


def _row_height(sheet_id: int, row: int, px: int) -> dict:
    return {
        "updateDimensionProperties": {
            "range": {"sheetId": sheet_id, "dimension": "ROWS", "startIndex": row, "endIndex": row + 1},
            "properties": {"pixelSize": px},
            "fields": "pixelSize",
        }
    }


def _row_rule(sheet_id: int, formula: str, bg: str, index: int) -> dict:
    """ไฮไลต์ทั้งแถวของตาราง (A2:P) เมื่อสูตรเป็นจริง"""
    return {
        "addConditionalFormatRule": {
            "index": index,
            "rule": {
                "ranges": [_range(sheet_id, 1, None, 0, 16)],
                "booleanRule": {
                    "condition": {"type": "CUSTOM_FORMULA", "values": [{"userEnteredValue": formula}]},
                    "format": {"backgroundColor": _rgb(bg)},
                },
            },
        }
    }


def _box(sheet_id, r1, r2, c1, c2) -> dict:
    line = {"style": "SOLID", "color": _rgb(PURPLE)}
    return {
        "updateBorders": {
            "range": _range(sheet_id, r1, r2, c1, c2),
            "top": line, "bottom": line, "left": line, "right": line,
            "innerHorizontal": {"style": "SOLID", "color": _rgb("#E1BEE7")},
        }
    }


def cycle_style_requests(sheet_id: int) -> list[dict]:
    """คำสั่งจัดรูปแบบชีตรอบบิล (ส่งด้วย spreadsheet.batch_update)"""
    K, L, M, N = 10, 11, 12, 13
    Q, R, S, T = 16, 17, 18, 19
    reqs: list[dict] = [
        {"updateSheetProperties": {
            "properties": {"sheetId": sheet_id, "gridProperties": {"frozenRowCount": 1}},
            "fields": "gridProperties.frozenRowCount",
        }},
        _row_height(sheet_id, 0, 40),
        # หัวตาราง: ม่วง ตัวขาว — คอลัมน์เงินแยกสี เขียว (เข้า) / ส้ม (จ่ายพนักงาน) / น้ำเงิน (ร้าน)
        _cell(sheet_id, 0, 1, 0, 16, _header_fmt(PURPLE)),
        _cell(sheet_id, 0, 1, K, K + 1, _header_fmt(GREEN)),
        _cell(sheet_id, 0, 1, L, L + 1, _header_fmt(ORANGE)),
        _cell(sheet_id, 0, 1, M, M + 1, _header_fmt(BLUE)),
        # เนื้อตาราง
        _cell(sheet_id, 1, None, 0, 16, {"verticalAlignment": "MIDDLE"}),
        _cell(sheet_id, 1, None, 0, 1, {"horizontalAlignment": "CENTER", "textFormat": {"bold": True}}),
        _cell(sheet_id, 1, None, 6, 7, {"textFormat": {"bold": True}}),  # ชื่อพนักงาน
        _cell(sheet_id, 1, None, 8, 9, {"wrapStrategy": "WRAP"}),        # บริการ
        _cell(sheet_id, 1, None, K, K + 1, {"backgroundColor": _rgb(GREEN_LIGHT), "numberFormat": {"type": "NUMBER", "pattern": MONEY}}),
        _cell(sheet_id, 1, None, L, L + 1, {"backgroundColor": _rgb(ORANGE_LIGHT), "numberFormat": {"type": "NUMBER", "pattern": MONEY}}),
        _cell(sheet_id, 1, None, M, M + 1, {"backgroundColor": _rgb(BLUE_LIGHT), "numberFormat": {"type": "NUMBER", "pattern": MONEY}, "textFormat": {"bold": True}}),
        _cell(sheet_id, 1, None, N, N + 1, {"horizontalAlignment": "CENTER"}),
        # บล็อกสรุป
        {"unmergeCells": {"range": _range(sheet_id, 0, 1, Q, S + 1)}},
        {"mergeCells": {"range": _range(sheet_id, 0, 1, Q, S + 1), "mergeType": "MERGE_ALL"}},
        _cell(sheet_id, 0, 1, Q, S + 1, _header_fmt(PURPLE_DARK, size=12)),
        _cell(sheet_id, 1, 5, Q, Q + 1, {"backgroundColor": _rgb(PURPLE_LIGHT), "textFormat": {"bold": True}}),
        _cell(sheet_id, 1, 5, R, R + 1, {"numberFormat": {"type": "NUMBER", "pattern": MONEY}, "textFormat": {"bold": True, "fontSize": 11}, "horizontalAlignment": "RIGHT"}),
        _cell(sheet_id, 1, 2, R, R + 1, {"backgroundColor": _rgb(GREEN_LIGHT)}),
        _cell(sheet_id, 2, 3, R, R + 1, {"backgroundColor": _rgb(ORANGE_LIGHT)}),
        _cell(sheet_id, 3, 4, R, R + 1, {"backgroundColor": _rgb(BLUE_LIGHT), "textFormat": {"bold": True, "fontSize": 12, "foregroundColor": _rgb(BLUE)}}),
        _cell(sheet_id, 4, 5, R, R + 1, {"numberFormat": {"type": "NUMBER", "pattern": "#,##0"}}),
        _box(sheet_id, 0, 5, Q, S + 1),
        _cell(sheet_id, 6, 7, Q, T + 1, _header_fmt(PURPLE)),
        _cell(sheet_id, 7, None, R, S + 1, {"numberFormat": {"type": "NUMBER", "pattern": MONEY}}),
        _cell(sheet_id, 7, None, S, S + 1, {"backgroundColor": _rgb(ORANGE_LIGHT), "textFormat": {"bold": True}}),
        _cell(sheet_id, 7, None, T, T + 1, {"wrapStrategy": "WRAP"}),
        _cell(sheet_id, 7, None, Q, Q + 1, {"textFormat": {"bold": True}}),
        # ไฮไลต์ทั้งแถว: โดเนท = ชมพู, ต่อเวลา = เหลือง
        _row_rule(sheet_id, '=$N2="โดเนท"', PINK_LIGHT, 0),
        _row_rule(sheet_id, '=$N2="ต่อเวลา"', YELLOW_LIGHT, 1),
        # ปุ่มตัวกรอง/เรียงลำดับบนหัวตาราง
        {"setBasicFilter": {"filter": {"range": _range(sheet_id, 0, None, 0, 16)}}},
    ]
    widths = {0: 70, 1: 95, 2: 65, 3: 65, 4: 150, 6: 150, 8: 230, 9: 140, K: 120, L: 130, M: 120, N: 80, 15: 240, Q: 200, R: 130, S: 140, T: 300}
    reqs += [_width(sheet_id, col, px) for col, px in widths.items()]
    reqs += [_hide(sheet_id, col) for col in (5, 7, 14)]  # ID ลูกค้า, ID พนักงาน, ระดับ VIP
    return reqs


def attendance_style_requests(sheet_id: int) -> list[dict]:
    reqs: list[dict] = [
        {"updateSheetProperties": {
            "properties": {"sheetId": sheet_id, "gridProperties": {"frozenRowCount": 1}},
            "fields": "gridProperties.frozenRowCount",
        }},
        _row_height(sheet_id, 0, 36),
        _cell(sheet_id, 0, 1, 0, 8, _header_fmt(PURPLE)),
        _cell(sheet_id, 1, None, 0, 1, {"horizontalAlignment": "CENTER"}),
        _cell(sheet_id, 1, None, 4, 5, {"textFormat": {"bold": True}}),
        _cell(sheet_id, 1, None, 6, 7, {
            "numberFormat": {"type": "NUMBER", "pattern": '0.00 "ชม."'},
            "backgroundColor": _rgb(GREEN_LIGHT),
            "textFormat": {"bold": True},
            "horizontalAlignment": "CENTER",
        }),
        {"setBasicFilter": {"filter": {"range": _range(sheet_id, 0, None, 0, 8)}}},
    ]
    widths = {0: 70, 1: 100, 2: 140, 3: 140, 4: 160, 6: 100, 7: 260}
    reqs += [_width(sheet_id, col, px) for col, px in widths.items()]
    reqs.append(_hide(sheet_id, 5))
    return reqs


def payout_style_requests(sheet_id: int) -> list[dict]:
    reqs: list[dict] = [
        {"updateSheetProperties": {
            "properties": {"sheetId": sheet_id, "gridProperties": {"frozenRowCount": 1}},
            "fields": "gridProperties.frozenRowCount",
        }},
        _row_height(sheet_id, 0, 36),
        _cell(sheet_id, 0, 1, 0, 6, _header_fmt(PURPLE)),
        _cell(sheet_id, 1, None, 0, 1, {"textFormat": {"bold": True}}),
        # เลขบัญชีเป็นข้อความเสมอ (กันเลข 0 นำหน้าหาย) ตัวหนาให้อ่านง่ายตอนโอน
        _cell(sheet_id, 1, None, 3, 4, {
            "numberFormat": {"type": "TEXT"},
            "backgroundColor": _rgb(ORANGE_LIGHT),
            "textFormat": {"bold": True, "fontSize": 11},
        }),
    ]
    widths = {0: 160, 2: 150, 3: 190, 4: 200, 5: 140}
    reqs += [_width(sheet_id, col, px) for col, px in widths.items()]
    reqs.append(_hide(sheet_id, 1))
    return reqs


def coin_style_requests(sheet_id: int) -> list[dict]:
    reqs: list[dict] = [
        {"updateSheetProperties": {
            "properties": {"sheetId": sheet_id, "gridProperties": {"frozenRowCount": 1}},
            "fields": "gridProperties.frozenRowCount",
        }},
        _row_height(sheet_id, 0, 36),
        _cell(sheet_id, 0, 1, 0, 7, _header_fmt("#B8860B")),
        _cell(sheet_id, 1, None, 1, 2, {"textFormat": {"bold": True}}),
        _cell(sheet_id, 1, None, 3, 4, {"numberFormat": {"type": "NUMBER", "pattern": "+#,##0;-#,##0;0"},
                                         "textFormat": {"bold": True}, "horizontalAlignment": "CENTER"}),
        _cell(sheet_id, 1, None, 6, 7, {"numberFormat": {"type": "NUMBER", "pattern": "#,##0"},
                                         "backgroundColor": _rgb("#FFF8E1"), "horizontalAlignment": "CENTER"}),
        # ได้เหรียญ = เขียว / ใช้-ดึงคืน = แดง
        {"addConditionalFormatRule": {"index": 0, "rule": {
            "ranges": [_range(sheet_id, 1, None, 3, 4)],
            "booleanRule": {"condition": {"type": "NUMBER_GREATER", "values": [{"userEnteredValue": "0"}]},
                            "format": {"textFormat": {"foregroundColor": _rgb(GREEN)}}}}}},
        {"addConditionalFormatRule": {"index": 1, "rule": {
            "ranges": [_range(sheet_id, 1, None, 3, 4)],
            "booleanRule": {"condition": {"type": "NUMBER_LESS", "values": [{"userEnteredValue": "0"}]},
                            "format": {"textFormat": {"foregroundColor": _rgb("#C62828")}}}}}},
        {"setBasicFilter": {"filter": {"range": _range(sheet_id, 0, None, 0, 7)}}},
    ]
    widths = {0: 130, 1: 150, 3: 90, 4: 90, 5: 260, 6: 90}
    reqs += [_width(sheet_id, col, px) for col, px in widths.items()]
    reqs.append(_hide(sheet_id, 2))
    return reqs


class SheetsClient:
    """ห่อ gspread ให้เรียกใช้แบบ async ได้ (gspread เป็น sync ล้วน)"""

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self._client = None
        self._spreadsheet = None
        self._lock = asyncio.Lock()

    @property
    def enabled(self) -> bool:
        return bool(self.cfg.get("google_sheets.enabled", False))

    @property
    def ready(self) -> bool:
        return self._spreadsheet is not None

    # ------------------------------------------------------------- startup
    async def start(self) -> None:
        if not self.enabled:
            log.info("ปิดการใช้งาน Google Sheets (google_sheets.enabled = false)")
            return
        try:
            await asyncio.to_thread(self._connect)
            log.info("เชื่อมต่อ Google Sheets สำเร็จ")
        except Exception:  # noqa: BLE001 - ไม่ให้บอทล่มเพราะ Sheets
            log.exception("เชื่อมต่อ Google Sheets ไม่สำเร็จ บอทจะทำงานต่อโดยไม่บันทึกชีต")

    def _connect(self) -> None:
        import gspread
        from google.oauth2.service_account import Credentials

        creds_file = Path(self.cfg.get("google_sheets.credentials_file", "credentials.json"))
        if not creds_file.exists():
            raise FileNotFoundError(f"ไม่พบไฟล์ credential: {creds_file}")

        scopes = [
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive",
        ]
        creds = Credentials.from_service_account_file(str(creds_file), scopes=scopes)
        self._client = gspread.authorize(creds)
        self._spreadsheet = self._client.open_by_key(self.cfg.get("google_sheets.spreadsheet_id"))

    # ------------------------------------------------------------- helpers
    @staticmethod
    def cycle_name(day: dt.date) -> str:
        return f"Cycle_{day.isoformat()}"

    def _get_or_create_ws(self, title: str):
        import gspread

        assert self._spreadsheet is not None
        try:
            return self._spreadsheet.worksheet(title)
        except gspread.WorksheetNotFound:
            ws = self._spreadsheet.add_worksheet(title=title, rows=500, cols=26)
            self._init_ws(ws)
            return ws

    def _clear_conditional_rules(self, sheet_id: int) -> list[dict]:
        """คำสั่งลบกฎไฮไลต์เดิมของชีต (กันกฎซ้ำเวลาจัดรูปแบบใหม่)"""
        assert self._spreadsheet is not None
        meta = self._spreadsheet.fetch_sheet_metadata(
            {"fields": "sheets(properties.sheetId,conditionalFormats)"}
        )
        sheet = next((s for s in meta.get("sheets", []) if s["properties"]["sheetId"] == sheet_id), {})
        count = len(sheet.get("conditionalFormats", []))
        return [{"deleteConditionalFormatRule": {"sheetId": sheet_id, "index": 0}} for _ in range(count)]

    def _init_ws(self, ws) -> None:
        assert self._spreadsheet is not None
        ws.update(values=[HEADERS], range_name="A1")
        ws.update(values=SUMMARY_BLOCK, range_name="Q1", value_input_option="USER_ENTERED")
        self._spreadsheet.batch_update(
            {"requests": self._clear_conditional_rules(ws.id) + cycle_style_requests(ws.id)}
        )

    def _init_attendance_ws(self, ws) -> None:
        assert self._spreadsheet is not None
        ws.update(values=[ATTENDANCE_HEADERS], range_name="A1")
        self._spreadsheet.batch_update({"requests": attendance_style_requests(ws.id)})

    # -------------------------------------------------------------- public
    async def append_job_row(self, sheet_title: str, row: list) -> bool:
        if not self.ready:
            return False
        async with self._lock:
            try:
                await asyncio.to_thread(self._append_sync, sheet_title, row)
                return True
            except Exception:  # noqa: BLE001
                log.exception("บันทึกแถวลง Google Sheets ไม่สำเร็จ")
                return False

    def _append_sync(self, sheet_title: str, row: list) -> None:
        ws = self._get_or_create_ws(sheet_title)
        # ไม่ใช้ append_row: Google จะนับบล็อกสรุปด้านขวา (Q:T) เป็นส่วนของตาราง
        # แล้วเขียนแถวใหม่ไว้ใต้บล็อกสรุป ทำให้มีแถวว่างแทรก — เขียนต่อจากแถวสุดท้ายของคอลัมน์ A แทน
        line = len(ws.col_values(1)) + 1
        if line > ws.row_count:
            ws.add_rows(200)
        ws.update(values=[row], range_name=f"A{line}:P{line}", value_input_option="USER_ENTERED")

    async def append_attendance_row(self, row: list) -> bool:
        if not self.ready:
            return False
        async with self._lock:
            try:
                await asyncio.to_thread(self._append_attendance_sync, row)
                return True
            except Exception:  # noqa: BLE001
                log.exception("บันทึกเวลาเข้างานลง Google Sheets ไม่สำเร็จ")
                return False

    def _attendance_ws(self):
        import gspread

        assert self._spreadsheet is not None
        try:
            return self._spreadsheet.worksheet(ATTENDANCE_SHEET)
        except gspread.WorksheetNotFound:
            ws = self._spreadsheet.add_worksheet(title=ATTENDANCE_SHEET, rows=1000, cols=8)
            self._init_attendance_ws(ws)
            return ws

    def _append_attendance_sync(self, row: list) -> None:
        """เขียนกะลงชีต — ถ้ากะนี้ (คอลัมน์ A = เลขกะ) เคยลงแล้วให้แก้แถวเดิม ไม่เพิ่มแถวซ้ำ (เช่น แอดมินแก้เวลา)"""
        ws = self._attendance_ws()
        ids = ws.col_values(1)
        key = str(row[0])
        if key in ids[1:]:
            line = ids.index(key, 1) + 1
            ws.update(values=[row], range_name=f"A{line}:H{line}", value_input_option="USER_ENTERED")
        else:
            ws.append_row(row, value_input_option="USER_ENTERED", table_range="A1")

    async def create_cycle_sheet(self, title: str) -> bool:
        if not self.ready:
            return False
        async with self._lock:
            try:
                await asyncio.to_thread(self._get_or_create_ws, title)
                return True
            except Exception:  # noqa: BLE001
                log.exception("สร้างชีตรอบใหม่ (%s) ไม่สำเร็จ", title)
                return False

    async def restyle(self, cycle_title: str) -> list[str]:
        """จัดรูปแบบชีตรอบปัจจุบัน + Attendance ใหม่ (หัวตาราง/สูตรสรุป/สี) — ข้อมูลเดิมไม่หาย"""
        if not self.ready:
            return []
        async with self._lock:
            return await asyncio.to_thread(self._restyle_sync, cycle_title)

    # ---------------------------------------------------------- เหรียญ
    def _coin_ws(self):
        import gspread

        assert self._spreadsheet is not None
        try:
            return self._spreadsheet.worksheet(COIN_SHEET)
        except gspread.WorksheetNotFound:
            ws = self._spreadsheet.add_worksheet(title=COIN_SHEET, rows=1000, cols=7)
            ws.update(values=[COIN_HEADERS], range_name="A1")
            self._spreadsheet.batch_update(
                {"requests": self._clear_conditional_rules(ws.id) + coin_style_requests(ws.id)}
            )
            return ws

    async def append_coin_row(self, row: list) -> bool:
        if not self.ready:
            return False
        async with self._lock:
            try:
                await asyncio.to_thread(
                    lambda: self._coin_ws().append_row(row, value_input_option="RAW", table_range="A1")
                )
                return True
            except Exception:  # noqa: BLE001
                log.exception("บันทึกเหรียญลง Google Sheets ไม่สำเร็จ")
                return False

    # ------------------------------------------------------ บัญชีพนักงาน
    def _payout_ws(self):
        import gspread

        assert self._spreadsheet is not None
        try:
            return self._spreadsheet.worksheet(PAYOUT_SHEET)
        except gspread.WorksheetNotFound:
            ws = self._spreadsheet.add_worksheet(title=PAYOUT_SHEET, rows=200, cols=6)
            self._init_payout_ws(ws)
            return ws

    def _init_payout_ws(self, ws) -> None:
        assert self._spreadsheet is not None
        ws.update(values=[PAYOUT_HEADERS], range_name="A1")
        self._spreadsheet.batch_update({"requests": payout_style_requests(ws.id)})

    async def upsert_payout_row(self, row: list[str]) -> bool:
        """เขียน/อัปเดตบัญชีรับเงินของพนักงาน 1 คน (row ตาม PAYOUT_HEADERS, คอลัมน์ B = ID)"""
        if not self.ready:
            return False
        async with self._lock:
            try:
                await asyncio.to_thread(self._upsert_payout_sync, row)
                return True
            except Exception:  # noqa: BLE001
                log.exception("บันทึกบัญชีพนักงานลง Google Sheets ไม่สำเร็จ")
                return False

    def _upsert_payout_sync(self, row: list[str]) -> None:
        ws = self._payout_ws()
        ids = ws.col_values(2)  # RAW: ID เก็บเป็นข้อความ
        if row[1] in ids:
            line = ids.index(row[1]) + 1
            ws.update(values=[row], range_name=f"A{line}:F{line}", value_input_option="RAW")
        else:
            ws.append_row(row, value_input_option="RAW", table_range="A1")

    def _restyle_sync(self, cycle_title: str) -> list[str]:
        done = []
        self._init_payout_ws(self._payout_ws())
        done.append(PAYOUT_SHEET)
        self._init_ws(self._get_or_create_ws(cycle_title))
        done.append(cycle_title)
        self._init_attendance_ws(self._attendance_ws())
        done.append(ATTENDANCE_SHEET)
        return done

    async def spreadsheet_url(self) -> str | None:
        if not self.ready:
            return None
        return f"https://docs.google.com/spreadsheets/d/{self.cfg.get('google_sheets.spreadsheet_id')}"
