import os
import json
import math
import time
import asyncio
import logging
import traceback
from datetime import datetime, timedelta, timezone
from logging.handlers import RotatingFileHandler

import requests
import discord
from discord.ext import commands, tasks
from discord import app_commands

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates

from aiohttp import web

import gspread
from google.oauth2.service_account import Credentials
from gspread.exceptions import WorksheetNotFound, APIError


# ============================================================
# GOLD DISCORD BOT V3 - SMART ANALYSIS EDITION (Google Sheets)
# ============================================================
#
# เปลี่ยนจาก V3 เดิม:
# - HISTORY_FILE / SIGNAL_LOG_FILE (local JSON) ถูกแทนที่ด้วย Google Sheets
#   เพราะ Render ใช้ ephemeral disk -> restart ทีไรข้อมูลหายทุกที
# - ข้อมูลราคาย้อนหลัง (price history) และ signal log จะถูกอ่าน/เขียนผ่าน
#   Google Sheets API แทน ทำให้ข้อมูลอยู่ถาวรข้าม deploy / restart
# - ใช้ in-memory cache กัน read เต็มชีทถี่เกินไป (อ่านเต็มชีทแค่ตอน start
#   แล้ว append ทีละแถวหลังจากนั้น)
#
# PATCH (แก้ปัญหา Render แบนเพราะยิง API รัวตอนตลาดปิด/API ล่ม):
# - เพิ่ม CIRCUIT BREAKER: ถ้า external API (xaus.com) fail ติดกันเกิน threshold
#   จะ "เปิดวงจร" หยุดยิง API ไปชั่วคราว (cooldown) ไม่ว่าจะถูกเรียกจาก
#   monitor loop หรือ slash command ไหนก็ตาม
# - เพิ่ม SHARED ANALYSIS CACHE: ผลวิเคราะห์ (get_full_analysis) จะถูกแคชไว้
#   สั้นๆ (ค่าเริ่มต้น 30 วิ) กันหลาย slash command / monitor loop ยิงซ้ำ
#   พร้อมกันในเวลาใกล้เคียงกันโดยไม่จำเป็น
#
# ต้องเพิ่ม ENV VARS ใหม่ (ดูหัวข้อ CONFIG ด้านล่าง):
#   GOOGLE_SERVICE_ACCOUNT_JSON  -> เนื้อหาไฟล์ service account JSON ทั้งไฟล์ (string)
#   GOOGLE_SHEET_ID              -> ID ของ Google Sheet (จาก URL)
#
# ต้อง share Google Sheet ให้ email ของ service account (สิทธิ์ Editor) ด้วย
#
# ต้องเพิ่มใน requirements.txt:
#   gspread
#   google-auth
#
# ============================================================


# ============================================================
# LOGGING SETUP
# ============================================================

LOG_FILE = "gold_bot_v3.log"

logger = logging.getLogger("gold_bot")
logger.setLevel(logging.DEBUG)

_console_handler = logging.StreamHandler()
_console_handler.setLevel(logging.INFO)
_console_handler.setFormatter(
    logging.Formatter("%(asctime)s | %(levelname)-7s | %(message)s")
)

_file_handler = RotatingFileHandler(
    LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
)
_file_handler.setLevel(logging.DEBUG)
_file_handler.setFormatter(
    logging.Formatter("%(asctime)s | %(levelname)-7s | %(message)s")
)

logger.addHandler(_console_handler)
logger.addHandler(_file_handler)


# ============================================================
# CONFIG (ปรับได้ผ่าน ENV VARS ทั้งหมด)
# ============================================================

def env_int(name, default):
    try:
        return int(os.getenv(name, str(default)))
    except Exception:
        return default


def env_float(name, default):
    try:
        return float(os.getenv(name, str(default)))
    except Exception:
        return default


TZ = timezone(timedelta(hours=7))

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "").strip()

ALERT_CHANNEL_ID = env_int("ALERT_CHANNEL_ID", 0)

CHECK_INTERVAL_MINUTES = env_int("CHECK_INTERVAL_MINUTES", 2)

XAU_SPOT_URL = "https://xaus.com/api/v1/spot"
XAU_INTRADAY_URL = "https://xaus.com/api/v1/intraday"
XAU_HISTORY_URL = "https://xaus.com/api/v1/history"

HISTORY_KEEP_DAYS = 14

# จำนวนชั่วโมงย้อนหลังที่ดึงจาก API สำหรับวิเคราะห์ MTF
INTRADAY_HOURS = env_int("INTRADAY_HOURS", 240)

# --- Indicators ---
EMA_FAST = 9
EMA_MID = 21
EMA_SLOW = 50
EMA_LONG = 200
RSI_PERIOD = 14
MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9
ATR_PERIOD = 14

# --- Market structure ---
SWING_LEFT = 2
SWING_RIGHT = 2
SR_LOOKBACK = 60
BREAKOUT_BUFFER_ATR = 0.10
RETEST_TOLERANCE_ATR = 0.30

# --- Divergence ---
DIVERGENCE_LOOKBACK = 20

# --- Score / Confidence (ปรับได้ผ่าน ENV) ---
MAX_RAW_SCORE = 24

MIN_SIGNAL_PERCENT = env_float("MIN_SIGNAL_PERCENT", 45.0)
STRONG_SIGNAL_PERCENT = env_float("STRONG_SIGNAL_PERCENT", 62.0)

MIN_CONFIDENCE = env_float("MIN_CONFIDENCE", 55.0)

DIRECTION_MARGIN_PERCENT = env_float("DIRECTION_MARGIN_PERCENT", 12.0)

# --- Volatility filter ---
ATR_HISTORY_LOOKBACK = env_int("ATR_HISTORY_LOOKBACK", 100)
MIN_ATR_PERCENTILE = env_float("MIN_ATR_PERCENTILE", 20.0)

# --- Risk ---
SL_ATR_MULTIPLIER = env_float("SL_ATR_MULTIPLIER", 1.5)
TP1_RR = env_float("TP1_RR", 1.5)
TP2_RR = env_float("TP2_RR", 2.5)

# --- Alert ---
ALERT_COOLDOWN_MINUTES = env_int("ALERT_COOLDOWN_MINUTES", 20)
RE_ALERT_SCORE_INCREASE = env_float("RE_ALERT_SCORE_INCREASE", 8.0)
HEARTBEAT_MINUTES = env_int("HEARTBEAT_MINUTES", 0)

HTTP_MAX_RETRIES = env_int("HTTP_MAX_RETRIES", 3)
HTTP_BACKOFF_BASE = env_float("HTTP_BACKOFF_BASE", 1.5)

# --- Circuit breaker (กันยิง API รัวตอน API ล่มต่อเนื่อง เช่นตลาดปิด) ---
CIRCUIT_FAIL_THRESHOLD = env_int("CIRCUIT_FAIL_THRESHOLD", 3)
CIRCUIT_COOLDOWN_MINUTES = env_int("CIRCUIT_COOLDOWN_MINUTES", 15)

# --- Shared analysis cache (กันหลายจุดยิง API ซ้ำพร้อมกันโดยไม่จำเป็น) ---
ANALYSIS_CACHE_SECONDS = env_int("ANALYSIS_CACHE_SECONDS", 30)

# --- Google Sheets (แทนที่ local JSON file) ---
GOOGLE_SERVICE_ACCOUNT_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
GOOGLE_SHEET_ID = os.getenv("GOOGLE_SHEET_ID", "").strip()

PRICE_HISTORY_SHEET_NAME = os.getenv("PRICE_HISTORY_SHEET_NAME", "PriceHistory")
SIGNAL_HISTORY_SHEET_NAME = os.getenv("SIGNAL_HISTORY_SHEET_NAME", "SignalHistory")

PRICE_HISTORY_HEADERS = ["time", "price"]
SIGNAL_HISTORY_HEADERS = [
    "time", "price", "direction", "score_percent", "buy_percent",
    "sell_percent", "entry", "stop", "tp1", "tp2", "global_signal", "confidence",
]

SIGNAL_LOG_MAX_ROWS = 500

# ทุกๆ กี่ครั้งที่ append แล้วให้ทำการ trim / rewrite ชีทเพื่อลบข้อมูลเก่าออก
# (ไม่ trim ทุกรอบเพราะ rewrite ทั้งชีทสิ้นเปลือง quota - trim เป็นช่วงๆ พอ)
PRICE_HISTORY_TRIM_EVERY = env_int("PRICE_HISTORY_TRIM_EVERY", 50)

# --- Runtime state ---
last_alert_time = None
last_alert_signal = None
last_alert_score_percent = 0.0
last_price = None
last_analysis_snapshot = None
bot_start_time = datetime.now(TZ)


# ============================================================
# CIRCUIT BREAKER
# ============================================================
#
# แนวคิด:
# - นับจำนวนครั้งที่ยิง external API (xaus.com) แล้ว fail ติดต่อกัน
#   (นับรวมทั้ง spot และ intraday endpoint เป็นตัวเดียวกัน)
# - ถ้า fail ติดกันครบ CIRCUIT_FAIL_THRESHOLD ครั้ง -> "เปิดวงจร"
#   หยุดยิง API ไปจนถึงเวลาที่กำหนด (CIRCUIT_COOLDOWN_MINUTES)
# - ระหว่างวงจรเปิด ทุกจุดที่เรียก get_xau_spot / get_xau_intraday
#   (ไม่ว่าจะจาก monitor loop หรือ slash command ไหน) จะได้ None / []
#   ทันทีโดยไม่ยิง HTTP request ออกไปเลย -> ตัดปัญหายิงรัวตอน API ล่ม
# - พอยิงสำเร็จอีกครั้ง (หลัง cooldown หมด) วงจรจะปิดกลับสู่ปกติทันที
# ============================================================

_circuit_fail_count = 0
_circuit_open_until = None


def circuit_is_open():
    """True ถ้าวงจรเปิดอยู่ (ยังไม่ควรยิง API)"""
    global _circuit_open_until

    if _circuit_open_until is not None and now_thai() < _circuit_open_until:
        return True

    if _circuit_open_until is not None and now_thai() >= _circuit_open_until:
        # หมด cooldown แล้ว -> ปิดวงจร ให้ลองยิงใหม่ได้อีกครั้ง
        logger.info("CIRCUIT: cooldown หมดแล้ว ลองยิง API ใหม่อีกครั้ง")
        _circuit_open_until = None

    return False


def circuit_record_failure():
    """เรียกทุกครั้งที่ยิง API แล้ว fail"""
    global _circuit_fail_count, _circuit_open_until

    _circuit_fail_count += 1
    logger.warning(
        f"CIRCUIT: fail ติดกันครั้งที่ {_circuit_fail_count}/{CIRCUIT_FAIL_THRESHOLD}"
    )

    if _circuit_fail_count >= CIRCUIT_FAIL_THRESHOLD and _circuit_open_until is None:
        _circuit_open_until = now_thai() + timedelta(minutes=CIRCUIT_COOLDOWN_MINUTES)
        logger.warning(
            f"CIRCUIT OPEN: หยุดยิง API ชั่วคราว (อาจเป็นเพราะตลาดปิดหรือ API ล่ม) "
            f"จะลองใหม่หลัง {_circuit_open_until.strftime('%H:%M:%S')}"
        )


def circuit_record_success():
    """เรียกทุกครั้งที่ยิง API สำเร็จ -> reset วงจรกลับปกติ"""
    global _circuit_fail_count, _circuit_open_until

    if _circuit_fail_count > 0 or _circuit_open_until is not None:
        logger.info("CIRCUIT: API กลับมาใช้ได้ปกติ รีเซ็ตวงจร")

    _circuit_fail_count = 0
    _circuit_open_until = None


def circuit_status_text():
    if circuit_is_open():
        remaining = (_circuit_open_until - now_thai()).total_seconds() / 60
        return f"🔴 OPEN (เหลืออีก {remaining:.0f} นาที)"
    return "🟢 CLOSED (ปกติ)"


# ============================================================
# HTTP SESSION + RETRY
# ============================================================

session = requests.Session()
session.headers.update({
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 Chrome/120 Safari/537.36"
    ),
    "Accept": "application/json",
})


def http_get_json(url, params=None, timeout=15, retries=None):
    """GET request พร้อม retry + exponential backoff กัน API ล่มชั่วคราว"""
    if retries is None:
        retries = HTTP_MAX_RETRIES

    last_error = None

    for attempt in range(1, retries + 1):
        try:
            response = session.get(url, params=params, timeout=timeout)

            if response.status_code != 200:
                raise RuntimeError(
                    f"HTTP {response.status_code}: {response.text[:300]}"
                )

            return response.json()

        except Exception as e:
            last_error = e
            logger.warning(
                f"HTTP GET ล้มเหลว (attempt {attempt}/{retries}) {url}: {repr(e)}"
            )

            if attempt < retries:
                wait = HTTP_BACKOFF_BASE ** attempt
                time.sleep(wait)

    raise RuntimeError(f"HTTP GET ล้มเหลวทั้งหมด {retries} ครั้ง: {repr(last_error)}")


# ============================================================
# BASIC HELPERS
# ============================================================

def now_thai():
    return datetime.now(TZ)


def safe_float(value):
    try:
        if value is None:
            return None
        return float(value)
    except Exception:
        return None


def fmt_price(value):
    if value is None:
        return "N/A"
    return f"${value:,.2f}"


def fmt_number(value):
    if value is None:
        return "N/A"
    return f"{value:,.2f}"


def fmt_percent(value):
    if value is None:
        return "N/A"
    return f"{value:.1f}%"


def clamp(value, low, high):
    return max(low, min(high, value))


def percentile(values, pct):
    if not values:
        return None
    data = sorted(values)
    k = (len(data) - 1) * (pct / 100.0)
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return data[int(k)]
    d0 = data[int(f)] * (c - k)
    d1 = data[int(c)] * (k - f)
    return d0 + d1


def linear_regression_slope(values):
    n = len(values)
    if n < 2:
        return None

    xs = list(range(n))
    x_mean = sum(xs) / n
    y_mean = sum(values) / n

    numerator = sum((xs[i] - x_mean) * (values[i] - y_mean) for i in range(n))
    denominator = sum((xs[i] - x_mean) ** 2 for i in range(n))

    if denominator == 0:
        return None

    return numerator / denominator


# ============================================================
# GOOGLE SHEETS PERSISTENCE
# ============================================================
#
# แนวคิด:
# - _gs_spreadsheet / _gs_price_ws / _gs_signal_ws เป็น global handle ที่ต่อ
#   Google Sheets ไว้ครั้งเดียวตอนบอทเริ่มทำงาน
# - _price_history_cache / _signal_history_cache เป็น in-memory cache
#   โหลดจากชีทมาครั้งเดียวตอน start แล้วหลังจากนั้น append ทั้งใน cache
#   และเขียนแถวใหม่ลงชีทไปพร้อมกัน (append_row) - ไม่อ่านเต็มชีทซ้ำอีก
#   เพื่อประหยัด Google Sheets API quota
# - ถ้าต่อ Google Sheets ไม่ได้ (ไม่ได้ตั้ง ENV / เน็ตมีปัญหา) บอทจะยังทำงาน
#   ต่อได้ (log warning) แต่ history/4H accumulation จะไม่ persist ข้าม restart
# ============================================================

_gs_client = None
_gs_spreadsheet = None
_gs_price_ws = None
_gs_signal_ws = None
_gs_enabled = False

_price_history_cache = []   # list of {"time": iso_str, "price": float}
_signal_history_cache = []  # list of dict ตาม SIGNAL_HISTORY_HEADERS

_price_append_counter = 0
_signal_append_counter = 0


def init_google_sheets():
    """ต่อ Google Sheets ครั้งเดียวตอนบอทเริ่มทำงาน (เรียกจาก main() ก่อน bot.start)"""
    global _gs_client, _gs_spreadsheet, _gs_price_ws, _gs_signal_ws, _gs_enabled

    if not GOOGLE_SERVICE_ACCOUNT_JSON or not GOOGLE_SHEET_ID:
        logger.warning(
            "GOOGLE_SERVICE_ACCOUNT_JSON หรือ GOOGLE_SHEET_ID ยังไม่ได้ตั้งค่า "
            "-> ข้อมูล price/signal history จะไม่ persist ข้าม restart"
        )
        _gs_enabled = False
        return

    try:
        creds_dict = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
        scopes = [
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive",
        ]
        credentials = Credentials.from_service_account_info(creds_dict, scopes=scopes)
        _gs_client = gspread.authorize(credentials)
        _gs_spreadsheet = _gs_client.open_by_key(GOOGLE_SHEET_ID)

        _gs_price_ws = _get_or_create_worksheet(
            _gs_spreadsheet, PRICE_HISTORY_SHEET_NAME, PRICE_HISTORY_HEADERS
        )
        _gs_signal_ws = _get_or_create_worksheet(
            _gs_spreadsheet, SIGNAL_HISTORY_SHEET_NAME, SIGNAL_HISTORY_HEADERS
        )

        _gs_enabled = True
        logger.info("=" * 70)
        logger.info(f"GOOGLE SHEETS เชื่อมต่อสำเร็จ: {_gs_spreadsheet.title}")
        logger.info(
            f"  - {PRICE_HISTORY_SHEET_NAME} (price history)"
        )
        logger.info(
            f"  - {SIGNAL_HISTORY_SHEET_NAME} (signal log)"
        )
        logger.info("=" * 70)

    except Exception as e:
        _gs_enabled = False
        logger.error(f"GOOGLE SHEETS INIT ERROR: {repr(e)}")
        logger.error(
            "ตรวจสอบว่า: 1) GOOGLE_SERVICE_ACCOUNT_JSON เป็น JSON ที่ถูกต้อง "
            "2) GOOGLE_SHEET_ID ถูกต้อง 3) แชร์ Sheet ให้ email ของ service account "
            "เป็น Editor แล้ว"
        )


def _get_or_create_worksheet(spreadsheet, name, headers):
    try:
        ws = spreadsheet.worksheet(name)
    except WorksheetNotFound:
        ws = spreadsheet.add_worksheet(title=name, rows=2000, cols=len(headers) + 2)
        ws.append_row(headers, value_input_option="RAW")
        return ws

    # ถ้ามีอยู่แล้วแต่แถวแรกยังไม่ใช่ header ให้ใส่ header ให้
    try:
        first_row = ws.row_values(1)
        if first_row != headers:
            if not first_row:
                ws.append_row(headers, value_input_option="RAW")
    except Exception as e:
        logger.warning(f"ตรวจสอบ header ของ worksheet '{name}' ไม่สำเร็จ: {repr(e)}")

    return ws


def _gs_retry(func, *args, retries=3, **kwargs):
    """เรียกฟังก์ชัน gspread พร้อม retry กัน rate limit / เน็ตสะดุดชั่วคราว"""
    last_error = None
    for attempt in range(1, retries + 1):
        try:
            return func(*args, **kwargs)
        except APIError as e:
            last_error = e
            logger.warning(f"GOOGLE SHEETS API ERROR (attempt {attempt}/{retries}): {repr(e)}")
            time.sleep(1.5 * attempt)
        except Exception as e:
            last_error = e
            logger.warning(f"GOOGLE SHEETS CALL ERROR (attempt {attempt}/{retries}): {repr(e)}")
            time.sleep(1.5 * attempt)

    raise RuntimeError(f"Google Sheets call ล้มเหลวทั้งหมด {retries} ครั้ง: {repr(last_error)}")


# ---------------- Price history (แทนที่ HISTORY_FILE เดิม) ----------------

def load_price_history_cache_from_sheet():
    """โหลด price history ทั้งหมดจากชีทมาไว้ใน in-memory cache (เรียกครั้งเดียวตอน start)"""
    global _price_history_cache

    if not _gs_enabled:
        _price_history_cache = []
        return

    try:
        records = _gs_retry(_gs_price_ws.get_all_records)
        cutoff = now_thai() - timedelta(days=HISTORY_KEEP_DAYS)
        cleaned = []

        for row in records:
            try:
                dt = datetime.fromisoformat(str(row.get("time")))
                price = safe_float(row.get("price"))
                if price is None:
                    continue
                if dt >= cutoff:
                    cleaned.append({"time": row.get("time"), "price": price})
            except Exception:
                continue

        _price_history_cache = cleaned
        logger.info(f"โหลด price history จาก Google Sheets: {len(_price_history_cache)} แถว")

    except Exception as e:
        logger.error(f"โหลด price history จาก Google Sheets ล้มเหลว: {repr(e)}")
        _price_history_cache = []


def append_history(price):
    """เพิ่มราคาปัจจุบันเข้า cache + เขียนลง Google Sheets (แทน append_history เดิม)"""
    global _price_history_cache, _price_append_counter

    current = now_thai()
    entry = {"time": current.isoformat(), "price": price}
    _price_history_cache.append(entry)

    # ตัดข้อมูลเก่าเกิน HISTORY_KEEP_DAYS ออกจาก cache เสมอ (ไม่กระทบชีทจนกว่าจะ trim)
    cutoff = current - timedelta(days=HISTORY_KEEP_DAYS)
    _price_history_cache = [
        x for x in _price_history_cache
        if _safe_parse_iso(x["time"]) is not None and _safe_parse_iso(x["time"]) >= cutoff
    ]

    if not _gs_enabled:
        return _price_history_cache

    try:
        _gs_retry(_gs_price_ws.append_row, [entry["time"], price], value_input_option="RAW")
    except Exception as e:
        logger.error(f"บันทึก price history ลง Google Sheets ล้มเหลว: {repr(e)}")

    _price_append_counter += 1
    if PRICE_HISTORY_TRIM_EVERY > 0 and _price_append_counter % PRICE_HISTORY_TRIM_EVERY == 0:
        trim_price_history_sheet()

    return _price_history_cache


def trim_price_history_sheet():
    """rewrite ทั้งชีท price history ด้วยข้อมูลใน cache (ตัดข้อมูลเก่าเกิน HISTORY_KEEP_DAYS ออก)"""
    if not _gs_enabled:
        return

    try:
        rows = [[x["time"], x["price"]] for x in _price_history_cache]
        _gs_retry(_gs_price_ws.clear)
        _gs_retry(_gs_price_ws.append_row, PRICE_HISTORY_HEADERS, value_input_option="RAW")
        if rows:
            _gs_retry(_gs_price_ws.append_rows, rows, value_input_option="RAW")
        logger.info(f"TRIM price history sheet: เหลือ {len(rows)} แถว")
    except Exception as e:
        logger.error(f"TRIM price history sheet ล้มเหลว: {repr(e)}")


def _safe_parse_iso(value):
    try:
        return datetime.fromisoformat(value)
    except Exception:
        return None


def load_history_as_points():
    """แปลง cache (ที่โหลดจาก Google Sheets) ให้เป็น format เดียวกับ intraday points"""
    points = []

    for item in _price_history_cache:
        dt = _safe_parse_iso(item["time"])
        price = safe_float(item.get("price"))
        if dt is not None and price is not None:
            points.append({"time": dt, "price": price})

    return points


def merge_points(*point_lists):
    """
    รวม points จากหลายแหล่ง (API + Google Sheets history) โดย dedupe ตามเวลา (ปัดวินาที)
    เพื่อให้ 4H timeframe สะสมแท่งเทียนได้ครบ 30 แท่งเร็วขึ้น
    """
    combined = {}

    for points in point_lists:
        for p in points:
            key = p["time"].replace(second=0, microsecond=0).isoformat()
            combined[key] = p

    return sorted(combined.values(), key=lambda x: x["time"])


# ---------------- Signal log (แทนที่ SIGNAL_LOG_FILE เดิม) ----------------

def load_signal_history_cache_from_sheet():
    global _signal_history_cache

    if not _gs_enabled:
        _signal_history_cache = []
        return

    try:
        records = _gs_retry(_gs_signal_ws.get_all_records)
        _signal_history_cache = records[-SIGNAL_LOG_MAX_ROWS:]
        logger.info(f"โหลด signal log จาก Google Sheets: {len(_signal_history_cache)} แถว")
    except Exception as e:
        logger.error(f"โหลด signal log จาก Google Sheets ล้มเหลว: {repr(e)}")
        _signal_history_cache = []


def record_signal(spot, setup, global_signal):
    """บันทึก signal ที่ถูกแจ้งเตือนแล้ว ลง cache + Google Sheets (แทน record_signal เดิม)"""
    global _signal_history_cache, _signal_append_counter

    row = {
        "time": now_thai().isoformat(),
        "price": spot["price"],
        "direction": setup.get("direction", "NO_TRADE"),
        "score_percent": setup.get("score_percent", 0),
        "buy_percent": setup.get("buy_percent", 0),
        "sell_percent": setup.get("sell_percent", 0),
        "entry": setup.get("entry"),
        "stop": setup.get("stop"),
        "tp1": setup.get("tp1"),
        "tp2": setup.get("tp2"),
        "global_signal": global_signal.get("signal", "NEUTRAL"),
        "confidence": global_signal.get("confidence", 0),
    }

    _signal_history_cache.append(row)
    _signal_history_cache = _signal_history_cache[-SIGNAL_LOG_MAX_ROWS:]

    if not _gs_enabled:
        return

    try:
        values = [row.get(col) for col in SIGNAL_HISTORY_HEADERS]
        values = [("" if v is None else v) for v in values]
        _gs_retry(_gs_signal_ws.append_row, values, value_input_option="RAW")
    except Exception as e:
        logger.error(f"บันทึก signal log ลง Google Sheets ล้มเหลว: {repr(e)}")

    _signal_append_counter += 1
    if _signal_append_counter % 20 == 0:
        trim_signal_history_sheet()


def trim_signal_history_sheet():
    """rewrite ทั้งชีท signal log ด้วยข้อมูลใน cache (เก็บแค่ SIGNAL_LOG_MAX_ROWS แถวล่าสุด)"""
    if not _gs_enabled:
        return

    try:
        rows = [
            [("" if row.get(col) is None else row.get(col)) for col in SIGNAL_HISTORY_HEADERS]
            for row in _signal_history_cache
        ]
        _gs_retry(_gs_signal_ws.clear)
        _gs_retry(_gs_signal_ws.append_row, SIGNAL_HISTORY_HEADERS, value_input_option="RAW")
        if rows:
            _gs_retry(_gs_signal_ws.append_rows, rows, value_input_option="RAW")
        logger.info(f"TRIM signal history sheet: เหลือ {len(rows)} แถว")
    except Exception as e:
        logger.error(f"TRIM signal history sheet ล้มเหลว: {repr(e)}")


# ============================================================
# RENDER HEALTH SERVER
# ============================================================

async def handle_health_check(request):
    return web.json_response({
        "status": "ok",
        "service": "XAU/USD Discord Bot V3 - Smart Analysis (Google Sheets)",
        "time": now_thai().isoformat(),
        "google_sheets": _gs_enabled,
        "circuit_breaker": circuit_status_text(),
    })


async def handle_diagnostics(request):
    global last_analysis_snapshot, last_alert_time, last_alert_signal

    return web.json_response({
        "last_alert_time": last_alert_time.isoformat() if last_alert_time else None,
        "last_alert_signal": last_alert_signal,
        "last_analysis": last_analysis_snapshot or {},
        "google_sheets_enabled": _gs_enabled,
        "price_history_rows_cached": len(_price_history_cache),
        "signal_history_rows_cached": len(_signal_history_cache),
        "circuit_breaker_status": circuit_status_text(),
        "circuit_fail_count": _circuit_fail_count,
        "config": {
            "MIN_SIGNAL_PERCENT": MIN_SIGNAL_PERCENT,
            "MIN_CONFIDENCE": MIN_CONFIDENCE,
            "DIRECTION_MARGIN_PERCENT": DIRECTION_MARGIN_PERCENT,
            "MIN_ATR_PERCENTILE": MIN_ATR_PERCENTILE,
            "ALERT_COOLDOWN_MINUTES": ALERT_COOLDOWN_MINUTES,
            "RE_ALERT_SCORE_INCREASE": RE_ALERT_SCORE_INCREASE,
            "CIRCUIT_FAIL_THRESHOLD": CIRCUIT_FAIL_THRESHOLD,
            "CIRCUIT_COOLDOWN_MINUTES": CIRCUIT_COOLDOWN_MINUTES,
            "ANALYSIS_CACHE_SECONDS": ANALYSIS_CACHE_SECONDS,
        }
    })


async def start_web_server():
    app = web.Application()
    app.router.add_get("/", handle_health_check)
    app.router.add_get("/health", handle_health_check)
    app.router.add_get("/debug", handle_diagnostics)

    runner = web.AppRunner(app)
    await runner.setup()

    port = env_int("PORT", 10000)
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()

    logger.info("=" * 70)
    logger.info(f"WEB SERVER ACTIVE : PORT {port}  (/debug สำหรับ diagnostics)")
    logger.info("=" * 70)


# ============================================================
# DISCORD
# ============================================================

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)


# ============================================================
# XAU SPOT
# ============================================================

def get_xau_spot():
    if circuit_is_open():
        logger.info("ข้าม SPOT fetch: circuit breaker เปิดอยู่ (API มีปัญหาต่อเนื่อง)")
        return None

    try:
        cache_buster = int(datetime.now().timestamp())

        data = http_get_json(
            XAU_SPOT_URL,
            params={
                "currency": "USD",
                "unit": "oz",
                "compact": "1",
                "fresh": cache_buster,
            },
            timeout=15,
        )

        price = safe_float(data.get("spot_usd_oz"))

        if price is None:
            raise ValueError(f"ไม่พบ spot_usd_oz: {data}")

        circuit_record_success()

        return {
            "price": price,
            "updated_at": data.get("updated_at"),
            "price_as_of": data.get("price_as_of"),
            "data_state": data.get("data_state") or {},
            "source": data.get("price_source"),
        }

    except Exception as e:
        circuit_record_failure()
        logger.error(f"XAU SPOT ERROR: {repr(e)}")
        return None


# ============================================================
# XAU INTRADAY
# ============================================================

def get_xau_intraday(hours=48):
    if circuit_is_open():
        logger.info("ข้าม INTRADAY fetch: circuit breaker เปิดอยู่ (API มีปัญหาต่อเนื่อง)")
        return []

    try:
        data = http_get_json(
            XAU_INTRADAY_URL,
            params={"symbol": "xau", "hours": hours},
            timeout=20,
        )

        raw_points = data.get("points", [])
        points = []

        for item in raw_points:
            try:
                timestamp = item.get("t")
                price = safe_float(item.get("p"))

                if timestamp is None or price is None:
                    continue

                if isinstance(timestamp, (int, float)):
                    dt = datetime.fromtimestamp(timestamp, timezone.utc).astimezone(TZ)
                else:
                    dt = datetime.fromisoformat(str(timestamp).replace("Z", "+00:00"))
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    dt = dt.astimezone(TZ)

                points.append({"time": dt, "price": price})

            except Exception:
                continue

        points.sort(key=lambda x: x["time"])
        circuit_record_success()
        return points

    except Exception as e:
        circuit_record_failure()
        logger.error(f"XAU INTRADAY ERROR: {repr(e)}")
        return []


# ============================================================
# EMA / RSI / MACD / ATR
# ============================================================

def calculate_ema(values, period):
    if len(values) < period:
        return None

    multiplier = 2 / (period + 1)
    ema_value = sum(values[:period]) / period

    for price in values[period:]:
        ema_value = ((price - ema_value) * multiplier) + ema_value

    return ema_value


def calculate_ema_series(values, period):
    if len(values) < period:
        return []

    multiplier = 2 / (period + 1)
    series = [None] * (period - 1)
    ema_value = sum(values[:period]) / period
    series.append(ema_value)

    for price in values[period:]:
        ema_value = ((price - ema_value) * multiplier) + ema_value
        series.append(ema_value)

    return series


def calculate_rsi(values, period=14):
    if len(values) < period + 1:
        return None

    gains, losses = [], []

    for i in range(1, len(values)):
        change = values[i] - values[i - 1]
        if change > 0:
            gains.append(change)
            losses.append(0)
        else:
            gains.append(0)
            losses.append(abs(change))

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    for i in range(period, len(gains)):
        avg_gain = ((avg_gain * (period - 1)) + gains[i]) / period
        avg_loss = ((avg_loss * (period - 1)) + losses[i]) / period

    if avg_loss == 0:
        return 100.0

    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def calculate_rsi_series(values, period=14):
    if len(values) < period + 1:
        return []

    rsi_series = [None] * period
    gains, losses = [], []

    for i in range(1, len(values)):
        change = values[i] - values[i - 1]
        gains.append(max(change, 0))
        losses.append(abs(min(change, 0)))

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    rsi_series.append(
        100.0 if avg_loss == 0 else 100 - (100 / (1 + (avg_gain / avg_loss)))
    )

    for i in range(period, len(gains)):
        avg_gain = ((avg_gain * (period - 1)) + gains[i]) / period
        avg_loss = ((avg_loss * (period - 1)) + losses[i]) / period

        rsi_value = (
            100.0 if avg_loss == 0 else 100 - (100 / (1 + (avg_gain / avg_loss)))
        )
        rsi_series.append(rsi_value)

    return rsi_series


def calculate_macd(values):
    if len(values) < (MACD_SLOW + MACD_SIGNAL):
        return None

    macd_values = []

    for i in range(MACD_SLOW, len(values) + 1):
        subset = values[:i]
        fast = calculate_ema(subset, MACD_FAST)
        slow = calculate_ema(subset, MACD_SLOW)

        if fast is not None and slow is not None:
            macd_values.append(fast - slow)

    if len(macd_values) < MACD_SIGNAL:
        return None

    macd = macd_values[-1]
    signal = calculate_ema(macd_values, MACD_SIGNAL)

    if signal is None:
        return None

    return {"macd": macd, "signal": signal, "histogram": macd - signal}


def calculate_atr(candles, period=14):
    if len(candles) < period + 1:
        return None

    true_ranges = []

    for i in range(1, len(candles)):
        current = candles[i]
        previous = candles[i - 1]

        tr = max(
            current["high"] - current["low"],
            abs(current["high"] - previous["close"]),
            abs(current["low"] - previous["close"]),
        )
        true_ranges.append(tr)

    if len(true_ranges) < period:
        return None

    return sum(true_ranges[-period:]) / period


def calculate_atr_series(candles, period=14):
    if len(candles) < period + 1:
        return []

    true_ranges = []
    for i in range(1, len(candles)):
        current = candles[i]
        previous = candles[i - 1]
        tr = max(
            current["high"] - current["low"],
            abs(current["high"] - previous["close"]),
            abs(current["low"] - previous["close"]),
        )
        true_ranges.append(tr)

    atr_series = []
    for i in range(period, len(true_ranges) + 1):
        window = true_ranges[i - period:i]
        atr_series.append(sum(window) / period)

    return atr_series


def calculate_atr_percentile(candles, period=14, lookback=100):
    atr_series = calculate_atr_series(candles, period)

    if not atr_series:
        return None

    recent = atr_series[-lookback:]
    current_atr = atr_series[-1]

    if len(recent) < 10:
        return None

    rank = sum(1 for v in recent if v <= current_atr)
    return (rank / len(recent)) * 100


# ============================================================
# AGGREGATE CANDLES
# ============================================================

def aggregate_candles(points, minutes):
    if not points:
        return []

    buckets = {}

    for item in points:
        dt = item["time"]
        total_minutes = dt.hour * 60 + dt.minute
        bucket_start = (total_minutes // minutes) * minutes
        hour = bucket_start // 60
        minute = bucket_start % 60

        bucket_time = dt.replace(hour=hour, minute=minute, second=0, microsecond=0)
        key = bucket_time.isoformat()

        buckets.setdefault(key, []).append(item["price"])

    candles = []
    for key in sorted(buckets.keys()):
        values = buckets[key]
        if not values:
            continue

        candles.append({
            "time": datetime.fromisoformat(key),
            "open": values[0],
            "high": max(values),
            "low": min(values),
            "close": values[-1],
        })

    return candles


# ============================================================
# SWINGS
# ============================================================

def find_swings(candles):
    swing_highs, swing_lows = [], []
    minimum = SWING_LEFT + SWING_RIGHT + 1

    if len(candles) < minimum:
        return swing_highs, swing_lows

    for i in range(SWING_LEFT, len(candles) - SWING_RIGHT):
        current = candles[i]
        left = candles[i - SWING_LEFT:i]
        right = candles[i + 1:i + 1 + SWING_RIGHT]

        is_high = all(current["high"] > x["high"] for x in (left + right))
        is_low = all(current["low"] < x["low"] for x in (left + right))

        if is_high:
            swing_highs.append({"index": i, "price": current["high"], "time": current["time"]})
        if is_low:
            swing_lows.append({"index": i, "price": current["low"], "time": current["time"]})

    return swing_highs, swing_lows


# ============================================================
# SUPPORT / RESISTANCE
# ============================================================

def calculate_support_resistance(candles, current_price):
    empty = {"support": None, "support2": None, "resistance": None, "resistance2": None}

    if not candles:
        return empty

    recent = candles[-SR_LOOKBACK:]
    if not recent:
        return empty

    highs = sorted([x["high"] for x in recent if x["high"] > current_price])
    lows = sorted([x["low"] for x in recent if x["low"] < current_price], reverse=True)

    resistance = highs[0] if highs else max(x["high"] for x in recent)
    resistance2 = highs[1] if len(highs) > 1 else None

    support = lows[0] if lows else min(x["low"] for x in recent)
    support2 = lows[1] if len(lows) > 1 else None

    return {
        "support": support,
        "support2": support2,
        "resistance": resistance,
        "resistance2": resistance2,
    }


# ============================================================
# CANDLE PATTERN
# ============================================================

def detect_candle_pattern(candles):
    default = {"name": "NONE", "direction": "NONE"}

    if len(candles) < 3:
        return default

    c1 = candles[-1]
    c2 = candles[-2]
    c3 = candles[-3]

    body1 = abs(c1["close"] - c1["open"])
    range1 = c1["high"] - c1["low"]

    if range1 <= 0:
        return default

    upper_wick = c1["high"] - max(c1["open"], c1["close"])
    lower_wick = min(c1["open"], c1["close"]) - c1["low"]

    bullish_engulfing = (
        c2["close"] < c2["open"] and c1["close"] > c1["open"]
        and c1["open"] <= c2["close"] and c1["close"] >= c2["open"]
    )
    if bullish_engulfing:
        return {"name": "Bullish Engulfing", "direction": "BULLISH"}

    bearish_engulfing = (
        c2["close"] > c2["open"] and c1["close"] < c1["open"]
        and c1["open"] >= c2["close"] and c1["close"] <= c2["open"]
    )
    if bearish_engulfing:
        return {"name": "Bearish Engulfing", "direction": "BEARISH"}

    body2 = abs(c2["close"] - c2["open"])
    body3 = abs(c3["close"] - c3["open"])
    range3 = c3["high"] - c3["low"]

    if range3 > 0:
        morning_star = (
            c3["close"] < c3["open"]
            and body3 / range3 > 0.5
            and body2 / max(range1, 0.0001) < 0.4
            and c1["close"] > c1["open"]
            and c1["close"] > (c3["open"] + c3["close"]) / 2
        )
        if morning_star:
            return {"name": "Morning Star", "direction": "BULLISH"}

        evening_star = (
            c3["close"] > c3["open"]
            and body3 / range3 > 0.5
            and body2 / max(range1, 0.0001) < 0.4
            and c1["close"] < c1["open"]
            and c1["close"] < (c3["open"] + c3["close"]) / 2
        )
        if evening_star:
            return {"name": "Evening Star", "direction": "BEARISH"}

    hammer = lower_wick >= body1 * 2 and upper_wick <= body1 and (body1 / range1) <= 0.5
    if hammer:
        return {"name": "Hammer", "direction": "BULLISH"}

    shooting_star = upper_wick >= body1 * 2 and lower_wick <= body1 and (body1 / range1) <= 0.5
    if shooting_star:
        return {"name": "Shooting Star", "direction": "BEARISH"}

    doji = (body1 / range1) <= 0.1
    if doji:
        return {"name": "Doji", "direction": "NEUTRAL"}

    inside_bar = c1["high"] <= c2["high"] and c1["low"] >= c2["low"]
    if inside_bar:
        return {"name": "Inside Bar", "direction": "NEUTRAL"}

    return default


# ============================================================
# FIBONACCI
# ============================================================

def calculate_fibonacci(candles):
    if len(candles) < 10:
        return None

    swing_highs, swing_lows = find_swings(candles)
    if not swing_highs or not swing_lows:
        return None

    latest_high = swing_highs[-1]
    latest_low = swing_lows[-1]

    high = latest_high["price"]
    low = latest_low["price"]

    if high <= low:
        return None

    distance = high - low

    return {
        "0.0": high,
        "23.6": high - distance * 0.236,
        "38.2": high - distance * 0.382,
        "50.0": high - distance * 0.500,
        "61.8": high - distance * 0.618,
        "78.6": high - distance * 0.786,
        "100.0": low,
    }


def nearest_fib(price, fib):
    if not fib:
        return None

    best = None
    best_distance = float("inf")

    for level, value in fib.items():
        distance = abs(price - value)
        if distance < best_distance:
            best_distance = distance
            best = {"level": level, "price": value, "distance": distance}

    return best


# ============================================================
# BREAKOUT / RETEST
# ============================================================

def detect_breakout(candles, sr, atr):
    default = {"type": "NONE", "level": None, "confirmed": False}

    if len(candles) < 3:
        return default

    current = candles[-1]
    previous = candles[-2]
    close = current["close"]

    resistance = sr.get("resistance")
    support = sr.get("support")
    buffer = atr * BREAKOUT_BUFFER_ATR if atr else 0

    if resistance is not None:
        if close > resistance + buffer and previous["close"] <= resistance + buffer:
            return {"type": "BULLISH_BREAKOUT", "level": resistance, "confirmed": True}

    if support is not None:
        if close < support - buffer and previous["close"] >= support - buffer:
            return {"type": "BEARISH_BREAKOUT", "level": support, "confirmed": True}

    return default


def detect_retest(candles, level, direction, atr):
    if level is None or len(candles) < 3:
        return False

    tolerance = atr * RETEST_TOLERANCE_ATR if atr else 2.0
    recent = candles[-3:]

    if direction == "BULLISH":
        touched = any(
            abs(c["low"] - level) <= tolerance or (c["low"] <= level <= c["high"])
            for c in recent
        )
        recovered = candles[-1]["close"] > level
        return touched and recovered

    if direction == "BEARISH":
        touched = any(
            abs(c["high"] - level) <= tolerance or (c["low"] <= level <= c["high"])
            for c in recent
        )
        rejected = candles[-1]["close"] < level
        return touched and rejected

    return False


# ============================================================
# RSI DIVERGENCE
# ============================================================

def detect_rsi_divergence(candles, closes):
    default = {"type": "NONE", "strength": 0}

    if len(candles) < DIVERGENCE_LOOKBACK + RSI_PERIOD:
        return default

    rsi_series = calculate_rsi_series(closes, RSI_PERIOD)

    if len(rsi_series) < DIVERGENCE_LOOKBACK:
        return default

    window_candles = candles[-DIVERGENCE_LOOKBACK:]
    window_rsi = rsi_series[-DIVERGENCE_LOOKBACK:]

    valid_pairs = [
        (c, r) for c, r in zip(window_candles, window_rsi) if r is not None
    ]

    if len(valid_pairs) < 10:
        return default

    swing_highs, swing_lows = find_swings(window_candles)

    if len(swing_highs) >= 2:
        h1 = swing_highs[-2]
        h2 = swing_highs[-1]

        rsi_at_h1 = window_rsi[h1["index"]] if h1["index"] < len(window_rsi) else None
        rsi_at_h2 = window_rsi[h2["index"]] if h2["index"] < len(window_rsi) else None

        if (
            rsi_at_h1 is not None and rsi_at_h2 is not None
            and h2["price"] > h1["price"]
            and rsi_at_h2 < rsi_at_h1
        ):
            strength = min(round((rsi_at_h1 - rsi_at_h2)), 10)
            return {"type": "BEARISH_DIVERGENCE", "strength": strength}

    if len(swing_lows) >= 2:
        l1 = swing_lows[-2]
        l2 = swing_lows[-1]

        rsi_at_l1 = window_rsi[l1["index"]] if l1["index"] < len(window_rsi) else None
        rsi_at_l2 = window_rsi[l2["index"]] if l2["index"] < len(window_rsi) else None

        if (
            rsi_at_l1 is not None and rsi_at_l2 is not None
            and l2["price"] < l1["price"]
            and rsi_at_l2 > rsi_at_l1
        ):
            strength = min(round((rsi_at_l2 - rsi_at_l1)), 10)
            return {"type": "BULLISH_DIVERGENCE", "strength": strength}

    return default


# ============================================================
# TREND STRENGTH
# ============================================================

def calculate_trend_strength(closes, atr):
    if len(closes) < 20 or not atr or atr == 0:
        return 0

    recent = closes[-20:]
    slope = linear_regression_slope(recent)

    if slope is None:
        return 0

    normalized = (slope / atr) * 100
    return clamp(normalized, -100, 100)


# ============================================================
# TIMEFRAME ANALYSIS
# ============================================================

def empty_timeframe_result(label, candles):
    current = candles[-1]["close"] if candles else None

    return {
        "label": label,
        "ready": False,
        "candles": candles,
        "current": current,
        "ema9": None, "ema21": None, "ema50": None, "ema200": None,
        "rsi": None, "macd": None, "atr": None, "atr_percentile": None,
        "support": None, "support2": None, "resistance": None, "resistance2": None,
        "pattern": {"name": "DATA NOT READY", "direction": "NONE"},
        "fib": None, "nearest_fib": None,
        "breakout": {"type": "NONE", "level": None, "confirmed": False},
        "divergence": {"type": "NONE", "strength": 0},
        "trend_strength": 0,
        "bullish_score": 0, "bearish_score": 0,
        "trend": "DATA NOT READY",
    }


def analyze_timeframe(candles, label):
    if len(candles) < 30:
        return empty_timeframe_result(label, candles)

    closes = [x["close"] for x in candles]
    current = closes[-1]

    ema9 = calculate_ema(closes, EMA_FAST)
    ema21 = calculate_ema(closes, EMA_MID)
    ema50 = calculate_ema(closes, EMA_SLOW)
    ema200 = calculate_ema(closes, EMA_LONG)

    rsi = calculate_rsi(closes, RSI_PERIOD)
    macd = calculate_macd(closes)
    atr = calculate_atr(candles, ATR_PERIOD)
    atr_pct = calculate_atr_percentile(candles, ATR_PERIOD, ATR_HISTORY_LOOKBACK)

    sr = calculate_support_resistance(candles, current)
    pattern = detect_candle_pattern(candles)
    fib = calculate_fibonacci(candles)
    nearest_fib_level = nearest_fib(current, fib)
    breakout = detect_breakout(candles, sr, atr)
    divergence = detect_rsi_divergence(candles, closes)
    trend_strength = calculate_trend_strength(closes, atr)

    bullish = 0
    bearish = 0

    if ema9 is not None and ema21 is not None:
        if ema9 > ema21:
            bullish += 1
        elif ema9 < ema21:
            bearish += 1

    if ema21 is not None and ema50 is not None:
        if ema21 > ema50:
            bullish += 1
        elif ema21 < ema50:
            bearish += 1

    if ema50 is not None and ema200 is not None:
        if ema50 > ema200:
            bullish += 2
        elif ema50 < ema200:
            bearish += 2

    if ema21 is not None:
        if current > ema21:
            bullish += 1
        elif current < ema21:
            bearish += 1

    if rsi is not None:
        if 50 < rsi < 70:
            bullish += 1
        elif 30 < rsi < 50:
            bearish += 1
        elif rsi >= 70:
            bearish += 0.5
        elif rsi <= 30:
            bullish += 0.5

    if macd:
        if macd["histogram"] > 0:
            bullish += 1
        elif macd["histogram"] < 0:
            bearish += 1

    if pattern["direction"] == "BULLISH":
        bullish += 1
    elif pattern["direction"] == "BEARISH":
        bearish += 1

    if breakout["type"] == "BULLISH_BREAKOUT":
        bullish += 2
    elif breakout["type"] == "BEARISH_BREAKOUT":
        bearish += 2

    if divergence["type"] == "BULLISH_DIVERGENCE":
        bullish += 1 + (divergence["strength"] / 10)
    elif divergence["type"] == "BEARISH_DIVERGENCE":
        bearish += 1 + (divergence["strength"] / 10)

    if trend_strength > 20:
        bullish += 1
    elif trend_strength < -20:
        bearish += 1

    if bullish >= bearish + 3:
        trend = "BULLISH"
    elif bearish >= bullish + 3:
        trend = "BEARISH"
    else:
        trend = "SIDEWAY"

    return {
        "label": label,
        "ready": True,
        "candles": candles,
        "current": current,
        "ema9": ema9, "ema21": ema21, "ema50": ema50, "ema200": ema200,
        "rsi": rsi, "macd": macd, "atr": atr, "atr_percentile": atr_pct,
        "support": sr["support"], "support2": sr["support2"],
        "resistance": sr["resistance"], "resistance2": sr["resistance2"],
        "pattern": pattern, "fib": fib, "nearest_fib": nearest_fib_level,
        "breakout": breakout, "divergence": divergence,
        "trend_strength": round(trend_strength, 1),
        "bullish_score": round(bullish, 1), "bearish_score": round(bearish, 1),
        "trend": trend,
    }


def analyze_mtf(points):
    timeframe_minutes = {"5M": 5, "15M": 15, "1H": 60, "4H": 240}
    result = {}

    for label, minutes in timeframe_minutes.items():
        candles = aggregate_candles(points, minutes)
        result[label] = analyze_timeframe(candles, label)

    return result


# ============================================================
# GLOBAL SIGNAL (Multi-Timeframe Confluence)
# ============================================================

def build_global_signal(mtf):
    bullish_score = 0
    bearish_score = 0
    ready = 0
    aligned_bullish = 0
    aligned_bearish = 0

    for label, weight in [("5M", 1), ("15M", 1), ("1H", 2), ("4H", 3)]:
        data = mtf.get(label)

        if not data or not data.get("ready", False):
            continue

        ready += 1
        trend = data.get("trend", "SIDEWAY")

        if trend == "BULLISH":
            bullish_score += weight + min(data.get("bullish_score", 0), 3)
            aligned_bullish += 1
        elif trend == "BEARISH":
            bearish_score += weight + min(data.get("bearish_score", 0), 3)
            aligned_bearish += 1

    alignment_bonus = 0
    if aligned_bullish >= 3:
        bullish_score += 2
        alignment_bonus = aligned_bullish
    if aligned_bearish >= 3:
        bearish_score += 2
        alignment_bonus = aligned_bearish

    total = bullish_score + bearish_score
    confidence = 0 if total == 0 else round((max(bullish_score, bearish_score) / total) * 100)

    if bullish_score >= bearish_score + 4:
        signal = "BUY_BIAS"
    elif bearish_score >= bullish_score + 4:
        signal = "SELL_BIAS"
    else:
        signal = "NEUTRAL"

    return {
        "signal": signal,
        "bullish": round(bullish_score, 1),
        "bearish": round(bearish_score, 1),
        "confidence": confidence,
        "ready": ready,
        "aligned_timeframes": max(aligned_bullish, aligned_bearish),
        "alignment_bonus": alignment_bonus,
    }


# ============================================================
# TRADE SETUP (Normalized Scoring)
# ============================================================

def empty_trade_setup():
    return {
        "direction": "NO_TRADE",
        "score": 0, "buy_score": 0, "sell_score": 0,
        "score_percent": 0.0, "buy_percent": 0.0, "sell_percent": 0.0,
        "reasons": [],
        "entry": None, "stop": None, "tp1": None, "tp2": None, "atr": None,
        "atr_percentile": None,
        "volatility_ok": True,
        "block_reason": None,
        "info_note": None,
        "h4_ready": False,
    }


def build_trade_setup(spot, mtf, global_signal):
    price = spot["price"]

    h1 = mtf.get("1H")
    h4 = mtf.get("4H")
    m15 = mtf.get("15M")

    setup = empty_trade_setup()

    if not h1 or not h1.get("ready", False):
        setup["block_reason"] = "ข้อมูล 1H ยังไม่พร้อม (ต้องการอย่างน้อย 30 แท่ง)"
        return setup

    h4_ready = bool(h4 and h4.get("ready", False))
    info_note = None

    buy = 0.0
    sell = 0.0
    reasons_buy = []
    reasons_sell = []

    if h4_ready:
        if h4.get("trend") == "BULLISH":
            buy += 3
            reasons_buy.append("4H Bullish")
        elif h4.get("trend") == "BEARISH":
            sell += 3
            reasons_sell.append("4H Bearish")
    else:
        h4_candle_count = len((h4 or {}).get("candles", []) or [])
        info_note = f"⏳ 4H กำลังสะสมข้อมูล ({h4_candle_count}/30 แท่ง) - เทรดจาก 1H/15M/5M ไปก่อน"

    if h1.get("trend") == "BULLISH":
        buy += 2
        reasons_buy.append("1H Bullish")
    elif h1.get("trend") == "BEARISH":
        sell += 2
        reasons_sell.append("1H Bearish")

    if m15 and m15.get("ready", False):
        if m15.get("trend") == "BULLISH":
            buy += 1
            reasons_buy.append("15M Bullish")
        elif m15.get("trend") == "BEARISH":
            sell += 1
            reasons_sell.append("15M Bearish")

    ema9, ema21, ema50 = h1.get("ema9"), h1.get("ema21"), h1.get("ema50")

    if ema9 is not None and ema21 is not None and ema50 is not None:
        if ema9 > ema21 > ema50:
            buy += 2
            reasons_buy.append("EMA Alignment (9>21>50)")
        elif ema9 < ema21 < ema50:
            sell += 2
            reasons_sell.append("EMA Alignment (9<21<50)")

    rsi = h1.get("rsi")
    if rsi is not None:
        if 50 < rsi < 70:
            buy += 1
            reasons_buy.append(f"RSI {rsi:.1f}")
        elif 30 < rsi < 50:
            sell += 1
            reasons_sell.append(f"RSI {rsi:.1f}")

    macd = h1.get("macd")
    if macd:
        histogram = macd.get("histogram")
        if histogram is not None:
            if histogram > 0:
                buy += 1
                reasons_buy.append("MACD Positive")
            elif histogram < 0:
                sell += 1
                reasons_sell.append("MACD Negative")

    breakout = h1.get("breakout") or {}
    breakout_type = breakout.get("type", "NONE")

    if breakout_type == "BULLISH_BREAKOUT":
        buy += 2
        reasons_buy.append("Resistance Breakout")
    elif breakout_type == "BEARISH_BREAKOUT":
        sell += 2
        reasons_sell.append("Support Breakout")

    if breakout.get("confirmed", False):
        level = breakout.get("level")
        atr = h1.get("atr")
        candles = h1.get("candles", [])

        if level is not None and candles:
            if breakout_type == "BULLISH_BREAKOUT" and detect_retest(candles, level, "BULLISH", atr):
                buy += 2
                reasons_buy.append("Breakout Retest")
            elif breakout_type == "BEARISH_BREAKOUT" and detect_retest(candles, level, "BEARISH", atr):
                sell += 2
                reasons_sell.append("Breakout Retest")

    pattern = h1.get("pattern") or {}
    pattern_direction = pattern.get("direction", "NONE")
    pattern_name = pattern.get("name", "NONE")

    if pattern_direction == "BULLISH":
        buy += 1
        reasons_buy.append(pattern_name)
    elif pattern_direction == "BEARISH":
        sell += 1
        reasons_sell.append(pattern_name)

    support = h1.get("support")
    resistance = h1.get("resistance")
    atr = h1.get("atr")

    if support is not None and atr:
        support_distance = price - support
        if 0 <= support_distance <= atr:
            buy += 2
            reasons_buy.append("Near Support")

    if resistance is not None and atr:
        resistance_distance = resistance - price
        if 0 <= resistance_distance <= atr:
            sell += 2
            reasons_sell.append("Near Resistance")

    nf = h1.get("nearest_fib")
    if nf and atr:
        fib_price = nf.get("price")
        fib_distance = nf.get("distance")
        fib_level = nf.get("level")

        if fib_price is not None and fib_distance is not None and fib_distance <= atr * 0.35:
            if price > fib_price:
                buy += 1
                reasons_buy.append(f"Fib {fib_level}%")
            else:
                sell += 1
                reasons_sell.append(f"Fib {fib_level}%")

    divergence = h1.get("divergence") or {}
    if divergence.get("type") == "BULLISH_DIVERGENCE":
        weight = 2 + (divergence.get("strength", 0) / 10)
        buy += weight
        reasons_buy.append(f"RSI Bullish Divergence (strength {divergence.get('strength', 0)})")
    elif divergence.get("type") == "BEARISH_DIVERGENCE":
        weight = 2 + (divergence.get("strength", 0) / 10)
        sell += weight
        reasons_sell.append(f"RSI Bearish Divergence (strength {divergence.get('strength', 0)})")

    trend_strength = h1.get("trend_strength", 0)
    if trend_strength > 30:
        buy += 1
        reasons_buy.append(f"Strong Uptrend ({trend_strength:.0f})")
    elif trend_strength < -30:
        sell += 1
        reasons_sell.append(f"Strong Downtrend ({trend_strength:.0f})")

    if global_signal.get("aligned_timeframes", 0) >= 3:
        if global_signal.get("signal") == "BUY_BIAS":
            buy += 1.5
            reasons_buy.append(f"{global_signal['aligned_timeframes']}/4 Timeframes Aligned")
        elif global_signal.get("signal") == "SELL_BIAS":
            sell += 1.5
            reasons_sell.append(f"{global_signal['aligned_timeframes']}/4 Timeframes Aligned")

    buy_percent = clamp((buy / MAX_RAW_SCORE) * 100, 0, 100)
    sell_percent = clamp((sell / MAX_RAW_SCORE) * 100, 0, 100)

    atr_pct = h1.get("atr_percentile")
    volatility_ok = True
    if atr_pct is not None and atr_pct < MIN_ATR_PERCENTILE:
        volatility_ok = False

    direction = "NO_TRADE"
    score = max(buy, sell)
    score_percent = max(buy_percent, sell_percent)
    reasons = []
    block_reason = None

    margin_ok_buy = buy_percent >= sell_percent + DIRECTION_MARGIN_PERCENT
    margin_ok_sell = sell_percent >= buy_percent + DIRECTION_MARGIN_PERCENT

    if buy_percent >= MIN_SIGNAL_PERCENT and margin_ok_buy:
        direction = "BUY"
        score = buy
        score_percent = buy_percent
        reasons = reasons_buy
    elif sell_percent >= MIN_SIGNAL_PERCENT and margin_ok_sell:
        direction = "SELL"
        score = sell
        score_percent = sell_percent
        reasons = reasons_sell
    else:
        if buy > sell:
            reasons = reasons_buy
        elif sell > buy:
            reasons = reasons_sell

        if max(buy_percent, sell_percent) < MIN_SIGNAL_PERCENT:
            block_reason = (
                f"คะแนนไม่ถึงเกณฑ์ ({max(buy_percent, sell_percent):.1f}% "
                f"< {MIN_SIGNAL_PERCENT}%)"
            )
        else:
            block_reason = (
                f"Buy/Sell ก้ำกึ่งเกินไป (ห่างกันแค่ "
                f"{abs(buy_percent - sell_percent):.1f}% ต้องการ {DIRECTION_MARGIN_PERCENT}%)"
            )

    if direction in ("BUY", "SELL") and not volatility_ok:
        block_reason = (
            f"ความผันผวนต่ำเกินไป (ATR percentile {atr_pct:.0f}% "
            f"< {MIN_ATR_PERCENTILE}%) - อาจเป็นสัญญาณหลอกช่วง sideways"
        )
        direction = "NO_TRADE"

    entry = price
    stop = None
    tp1 = None
    tp2 = None

    if direction in ("BUY", "SELL") and atr:
        if direction == "BUY":
            structural_stop = support if support is not None else entry - atr * 1.5
            atr_stop = entry - atr * SL_ATR_MULTIPLIER
            stop = min(structural_stop, atr_stop)
            risk = entry - stop

            if risk > 0:
                tp1 = entry + risk * TP1_RR
                tp2 = entry + risk * TP2_RR
            else:
                direction = "NO_TRADE"
                block_reason = "คำนวณ Stop Loss ไม่สมเหตุสมผล (risk <= 0)"
        else:
            structural_stop = resistance if resistance is not None else entry + atr * 1.5
            atr_stop = entry + atr * SL_ATR_MULTIPLIER
            stop = max(structural_stop, atr_stop)
            risk = stop - entry

            if risk > 0:
                tp1 = entry - risk * TP1_RR
                tp2 = entry - risk * TP2_RR
            else:
                direction = "NO_TRADE"
                block_reason = "คำนวณ Stop Loss ไม่สมเหตุสมผล (risk <= 0)"

    if direction == "NO_TRADE":
        entry = None
        stop = None
        tp1 = None
        tp2 = None

    return {
        "direction": direction,
        "score": round(score, 1),
        "buy_score": round(buy, 1),
        "sell_score": round(sell, 1),
        "score_percent": round(score_percent, 1),
        "buy_percent": round(buy_percent, 1),
        "sell_percent": round(sell_percent, 1),
        "reasons": reasons,
        "entry": entry, "stop": stop, "tp1": tp1, "tp2": tp2, "atr": atr,
        "atr_percentile": atr_pct,
        "volatility_ok": volatility_ok,
        "block_reason": block_reason,
        "info_note": info_note,
        "h4_ready": h4_ready,
    }


# ============================================================
# SIGNAL QUALITY
# ============================================================

def signal_quality(score_percent):
    if score_percent >= STRONG_SIGNAL_PERCENT:
        return "🔥 STRONG"
    if score_percent >= MIN_SIGNAL_PERCENT:
        return "🟢 GOOD"
    if score_percent >= 25:
        return "🟡 WEAK"
    return "⚪ NO TRADE"


# ============================================================
# FULL ANALYSIS
# ============================================================

def get_full_analysis():
    spot = get_xau_spot()
    if spot is None:
        if circuit_is_open():
            raise RuntimeError(
                "API XAU/USD กำลังมีปัญหาต่อเนื่อง (circuit breaker เปิดอยู่) "
                "ระบบพักการยิง API ชั่วคราวเพื่อไม่ให้โดนแบน ลองใหม่อีกครั้งภายหลัง"
            )
        raise RuntimeError("ไม่สามารถดึง XAU/USD Spot ได้ (API อาจล่มชั่วคราว ลองใหม่รอบถัดไป)")

    api_points = get_xau_intraday(hours=INTRADAY_HOURS)
    local_points = load_history_as_points()

    # รวมข้อมูลจาก API กับข้อมูลที่บอทสะสมไว้เองใน Google Sheets
    points = merge_points(local_points, api_points)

    if len(points) < 30:
        raise RuntimeError(
            f"ข้อมูล Intraday ไม่พอ: {len(points)} จุด (ต้องการอย่างน้อย 30) "
            f"- API ให้มา {len(api_points)} จุด, Google Sheets history มี {len(local_points)} จุด"
        )

    mtf = analyze_mtf(points)
    global_signal = build_global_signal(mtf)
    setup = build_trade_setup(spot, mtf, global_signal)

    return spot, points, mtf, global_signal, setup


# ============================================================
# SHARED ANALYSIS CACHE
# ============================================================
#
# แนวคิด:
# - get_full_analysis() ยิง API จริงทุกครั้งที่ถูกเรียก
# - แต่ทุก slash command (/xau /analysis /signal /trend /levels /pattern)
#   และ monitor loop เรียกฟังก์ชันนี้แยกกัน ถ้ามีคนกดคำสั่งพร้อมกันหลายคน
#   หรือ monitor loop กำลังรันพอดี จะกลายเป็นยิง API ซ้ำซ้อนโดยไม่จำเป็น
#   เพราะข้อมูลในช่วงเวลาสั้นๆ ไม่ต่างกันอยู่แล้ว
# - get_full_analysis_cached() แคชผลลัพธ์ไว้ ANALYSIS_CACHE_SECONDS วินาที
#   ถ้ามีการเรียกซ้ำในช่วงเวลานั้น จะคืนผลจาก cache แทนที่จะยิง API ใหม่
# - ใช้ asyncio.Lock กันกรณีมีหลาย request เข้ามาพร้อมกันตอน cache หมดอายุ
#   พอดี (กันยิง API ซ้อนกันหลายครั้งในจังหวะเดียวกัน)
# ============================================================

_analysis_cache_lock = asyncio.Lock()
_analysis_cache_result = None
_analysis_cache_time = None
_analysis_cache_error = None


async def get_full_analysis_cached():
    """ใช้แทน get_full_analysis() ตรงๆ ในทุก slash command และ monitor loop"""
    global _analysis_cache_result, _analysis_cache_time, _analysis_cache_error

    async with _analysis_cache_lock:
        now = now_thai()

        cache_fresh = (
            _analysis_cache_time is not None
            and (now - _analysis_cache_time).total_seconds() < ANALYSIS_CACHE_SECONDS
        )

        if cache_fresh and _analysis_cache_result is not None:
            return _analysis_cache_result

        if cache_fresh and _analysis_cache_error is not None:
            # เพิ่งยิงพลาดไปเมื่อกี้นี้เอง (ยังอยู่ในช่วง cache) -> ไม่ยิงซ้ำ
            # โยน error เดิมออกไปแทน กันการยิงรัวตอน error ต่อเนื่อง
            raise _analysis_cache_error

        try:
            result = await asyncio.to_thread(get_full_analysis)
            _analysis_cache_result = result
            _analysis_cache_error = None
            _analysis_cache_time = now
            return result
        except Exception as e:
            _analysis_cache_result = None
            _analysis_cache_error = e
            _analysis_cache_time = now
            raise


# ============================================================
# FORMAT HELPERS
# ============================================================

def trend_icon(trend):
    if trend == "BULLISH":
        return "🟢"
    if trend == "BEARISH":
        return "🔴"
    if trend == "DATA NOT READY":
        return "⚠️"
    return "🟡"


def signal_text(signal):
    if signal == "BUY_BIAS":
        return "🟢 BUY BIAS"
    if signal == "SELL_BIAS":
        return "🔴 SELL BIAS"
    return "🟡 NEUTRAL"


def format_tf(data):
    label = data.get("label", "TF")

    if not data.get("ready", False):
        return f"**{label}** ⚠️ DATA NOT READY"

    rsi = data.get("rsi")
    rsi_text = f"{rsi:.1f}" if rsi is not None else "N/A"

    macd_text = "N/A"
    macd = data.get("macd")
    if macd and macd.get("histogram") is not None:
        macd_text = f"{macd['histogram']:.2f}"

    div = data.get("divergence") or {}
    div_text = ""
    if div.get("type") not in (None, "NONE"):
        div_icon = "🔺" if div["type"] == "BULLISH_DIVERGENCE" else "🔻"
        div_text = f" | Div {div_icon}"

    return (
        f"{trend_icon(data.get('trend'))} **{label} {data.get('trend', 'UNKNOWN')}**\n"
        f"Price `{fmt_price(data.get('current'))}` | RSI `{rsi_text}` | "
        f"MACD Hist `{macd_text}`{div_text}\n"
        f"EMA9 `{fmt_price(data.get('ema9'))}` | EMA21 `{fmt_price(data.get('ema21'))}` | "
        f"EMA50 `{fmt_price(data.get('ema50'))}`\n"
        f"Support `{fmt_price(data.get('support'))}` | "
        f"Resistance `{fmt_price(data.get('resistance'))}` | "
        f"Trend Strength `{data.get('trend_strength', 0):.0f}`"
    )


# ============================================================
# ANALYSIS MESSAGE
# ============================================================

def build_analysis_message(spot, mtf, global_signal, setup):
    price = spot["price"]
    setup_direction = setup.get("direction", "NO_TRADE")

    if setup_direction == "BUY":
        setup_text = "🟢 BUY SETUP"
    elif setup_direction == "SELL":
        setup_text = "🔴 SELL SETUP"
    else:
        setup_text = "🟡 NO TRADE"

    lines = [
        "🪙 **XAU/USD V3 SMART ANALYSIS**",
        "━━━━━━━━━━━━━━━━━━━━━━━━",
        "",
        f"💰 Price: **{fmt_price(price)}**",
        f"🎯 Bias: **{signal_text(global_signal.get('signal', 'NEUTRAL'))}**",
        f"📊 Confidence: **{global_signal.get('confidence', 0)}%**",
        f"🔗 Timeframes Aligned: **{global_signal.get('aligned_timeframes', 0)}/4**",
        "",
        f"🧠 Setup: **{setup_text}**",
        f"⭐ Score: **{setup.get('score_percent', 0)}%** "
        f"(Buy {setup.get('buy_percent', 0)}% / Sell {setup.get('sell_percent', 0)}%)",
        f"Quality: **{signal_quality(setup.get('score_percent', 0))}**",
    ]

    if setup.get("block_reason"):
        lines.append(f"ℹ️ เหตุผลที่ไม่เข้าเทรด: {setup['block_reason']}")
    if setup.get("info_note"):
        lines.append(f"ℹ️ {setup['info_note']}")

    lines.extend(["", "━━━━━━━━━━━━━━━━━━━━━━━━", "📊 **MULTI TIMEFRAME**", ""])

    for label in ["5M", "15M", "1H", "4H"]:
        lines.append(format_tf(mtf.get(label, {"label": label, "ready": False})))
        lines.append("")

    h1 = mtf.get("1H", {})

    lines.extend([
        "━━━━━━━━━━━━━━━━━━━━━━━━",
        "🧱 **MARKET STRUCTURE**",
        "",
        f"Support 1: `{fmt_price(h1.get('support'))}`",
        f"Support 2: `{fmt_price(h1.get('support2'))}`",
        f"Resistance 1: `{fmt_price(h1.get('resistance'))}`",
        f"Resistance 2: `{fmt_price(h1.get('resistance2'))}`",
        "",
    ])

    pattern = h1.get("pattern", {}) or {}
    breakout = h1.get("breakout", {}) or {}
    divergence = h1.get("divergence", {}) or {}
    fib = h1.get("nearest_fib")
    atr_pct = h1.get("atr_percentile")

    lines.extend([
        "🕯️ **PATTERN**",
        f"`{pattern.get('name', 'NONE')}`",
        "",
        "🚀 **BREAKOUT**",
        f"`{breakout.get('type', 'NONE')}`",
        "",
        "🔀 **RSI DIVERGENCE**",
        f"`{divergence.get('type', 'NONE')}`"
        + (f" (strength {divergence.get('strength', 0)})" if divergence.get("type") not in (None, "NONE") else ""),
        "",
        "📉 **VOLATILITY (ATR Percentile)**",
        f"`{fmt_percent(atr_pct)}`"
        + (" ⚠️ ต่ำเกินไป อาจเป็นสัญญาณหลอก" if atr_pct is not None and atr_pct < MIN_ATR_PERCENTILE else ""),
        "",
    ])

    if fib:
        lines.extend([
            "📐 **FIBONACCI**",
            f"Nearest: `{fib.get('level', 'N/A')}%` @ `{fmt_price(fib.get('price'))}`",
            "",
        ])

    lines.extend(["━━━━━━━━━━━━━━━━━━━━━━━━", "🛡️ **RISK / SETUP**", ""])

    if setup_direction in ("BUY", "SELL"):
        lines.extend([
            f"Entry: `{fmt_price(setup.get('entry'))}`",
            f"Stop Loss: `{fmt_price(setup.get('stop'))}`",
            f"TP1: `{fmt_price(setup.get('tp1'))}` (1:{TP1_RR})",
            f"TP2: `{fmt_price(setup.get('tp2'))}` (1:{TP2_RR})",
            f"ATR: `{fmt_price(setup.get('atr'))}`",
            "",
        ])
    else:
        lines.extend(["⛔ ไม่มี Setup ที่ผ่านเกณฑ์", "ระบบเลือก **NO TRADE**", ""])

    reasons = setup.get("reasons", []) or []
    if reasons:
        lines.append("🔎 **CONFLUENCE**")
        for reason in reasons[:15]:
            lines.append(f"• {reason}")
        lines.append("")

    lines.extend([
        "━━━━━━━━━━━━━━━━━━━━━━━━",
        "⚠️ Technical analysis only.",
        "ไม่ใช่คำแนะนำการลงทุนและไม่รับประกันผลลัพธ์",
        "ราคา XAU/USD จาก API เป็น indicative spot ไม่ใช่ราคา execution",
    ])

    return "\n".join(lines)


# ============================================================
# ALERT MESSAGE
# ============================================================

def build_alert_message(spot, mtf, global_signal, setup, is_reAlert=False):
    direction = setup.get("direction", "NO_TRADE")

    if direction == "BUY":
        title = "🟢 HIGH QUALITY BUY SETUP"
    elif direction == "SELL":
        title = "🔴 HIGH QUALITY SELL SETUP"
    else:
        return None

    if is_reAlert:
        title += " (สัญญาณแข็งแกร่งขึ้น)"

    h1 = mtf.get("1H", {})

    lines = [
        "🚨 **XAU/USD V3 SMART ALERT**",
        "━━━━━━━━━━━━━━━━━━━━━━━━",
        "",
        f"**{title}**",
        "",
        f"💰 Price: **{fmt_price(spot['price'])}**",
        f"⭐ Score: **{setup.get('score_percent', 0)}%** ({signal_quality(setup.get('score_percent', 0))})",
        f"📊 Confidence: **{global_signal.get('confidence', 0)}%**",
        f"🔗 Timeframes Aligned: **{global_signal.get('aligned_timeframes', 0)}/4**",
        "",
        "📊 **MTF**",
    ]

    for label in ["5M", "15M", "1H", "4H"]:
        data = mtf.get(label)
        if data and data.get("ready", False):
            lines.append(f"{trend_icon(data.get('trend'))} {label}: **{data.get('trend')}**")

    divergence = h1.get("divergence", {}) or {}

    lines.extend([
        "",
        "🧱 **LEVELS**",
        f"Support: `{fmt_price(h1.get('support'))}`",
        f"Resistance: `{fmt_price(h1.get('resistance'))}`",
        "",
        "🕯️ **PATTERN**",
        f"`{(h1.get('pattern') or {}).get('name', 'NONE')}`",
        "",
        "🚀 **STRUCTURE**",
        f"`{(h1.get('breakout') or {}).get('type', 'NONE')}`",
    ])

    if divergence.get("type") not in (None, "NONE"):
        lines.extend(["", "🔀 **DIVERGENCE**", f"`{divergence.get('type')}`"])

    lines.append("")

    if setup.get("entry"):
        lines.extend([
            "🛡️ **RISK PLAN**",
            f"Entry: `{fmt_price(setup.get('entry'))}`",
            f"SL: `{fmt_price(setup.get('stop'))}`",
            f"TP1: `{fmt_price(setup.get('tp1'))}`",
            f"TP2: `{fmt_price(setup.get('tp2'))}`",
            "",
            "🔎 **CONFIRMATIONS**",
        ])

        for reason in (setup.get("reasons", []) or [])[:10]:
            lines.append(f"• {reason}")

    lines.extend([
        "",
        "━━━━━━━━━━━━━━━━━━━━━━━━",
        "⚠️ Alert = technical setup",
        "ไม่ใช่คำสั่งให้เปิดออเดอร์อัตโนมัติ",
    ])

    return "\n".join(lines)


# ============================================================
# CHART
# ============================================================

def make_chart(points):
    if len(points) < 2:
        return None

    recent = points[-500:]
    times = [x["time"] for x in recent]
    prices = [x["price"] for x in recent]

    ema9, ema21 = [], []

    for i in range(len(prices)):
        subset = prices[:i + 1]
        ema9.append(calculate_ema(subset, EMA_FAST))
        ema21.append(calculate_ema(subset, EMA_MID))

    fig, ax = plt.subplots(figsize=(12, 5), dpi=140)

    ax.plot(times, prices, linewidth=2, label="XAU/USD")
    ax.plot(times, [x if x is not None else float("nan") for x in ema9], linewidth=1.2, label="EMA 9")
    ax.plot(times, [x if x is not None else float("nan") for x in ema21], linewidth=1.2, label="EMA 21")

    ax.set_title("XAU/USD V3 Smart Analysis")
    ax.set_ylabel("USD / Troy Ounce")
    ax.grid(True, alpha=0.25)
    ax.legend()
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%d/%m %H:%M"))
    fig.autofmt_xdate()
    fig.tight_layout()

    path = "xau_v3_chart.png"
    fig.savefig(path)
    plt.close(fig)

    return path


# ============================================================
# /GOLD
# ============================================================

@bot.tree.command(name="gold", description="ดูราคา XAU/USD ปัจจุบัน")
async def gold(interaction: discord.Interaction):
    await interaction.response.defer()

    try:
        spot = await asyncio.to_thread(get_xau_spot)

        if spot is None:
            if circuit_is_open():
                await interaction.followup.send(
                    f"❌ API XAU/USD กำลังมีปัญหาต่อเนื่อง ระบบพักการยิง API ชั่วคราว\n"
                    f"สถานะ: {circuit_status_text()}"
                )
            else:
                await interaction.followup.send("❌ ไม่สามารถดึงราคา XAU/USD ได้ (API อาจล่มชั่วคราว)")
            return

        state = spot.get("data_state", {}) or {}

        await interaction.followup.send(
            "🪙 **XAU/USD**\n"
            "━━━━━━━━━━━━━━━━━━\n\n"
            f"💰 Price: **{fmt_price(spot['price'])}**\n"
            f"📡 Status: `{state.get('status', 'unknown')}`\n"
            f"🕐 `{now_thai().strftime('%d/%m/%Y %H:%M:%S')}`\n\n"
            "📌 Global gold spot price"
        )

    except Exception as e:
        logger.error(f"GOLD COMMAND ERROR: {repr(e)}")
        await interaction.followup.send(f"❌ GOLD ERROR\n`{str(e)[:500]}`")


# ============================================================
# /XAU
# ============================================================

@bot.tree.command(name="xau", description="วิเคราะห์ XAU/USD V3 แบบเต็ม (Smart Analysis)")
async def xau(interaction: discord.Interaction):
    await interaction.response.defer()

    try:
        spot, points, mtf, global_signal, setup = await get_full_analysis_cached()
        message = build_analysis_message(spot, mtf, global_signal, setup)
        await interaction.followup.send(message)

    except Exception as e:
        logger.error(f"XAU COMMAND ERROR: {repr(e)}")
        traceback.print_exc()
        await interaction.followup.send(f"❌ XAU ANALYSIS ERROR\n`{str(e)[:700]}`")


# ============================================================
# /ANALYSIS
# ============================================================

@bot.tree.command(name="analysis", description="EMA RSI MACD ATR MTF Structure Divergence")
async def analysis(interaction: discord.Interaction):
    await interaction.response.defer()

    try:
        spot, points, mtf, global_signal, setup = await get_full_analysis_cached()
        await interaction.followup.send(build_analysis_message(spot, mtf, global_signal, setup))

    except Exception as e:
        logger.error(f"ANALYSIS ERROR: {repr(e)}")
        await interaction.followup.send(f"❌ ANALYSIS ERROR\n`{str(e)[:700]}`")


# ============================================================
# /SIGNAL
# ============================================================

@bot.tree.command(name="signal", description="ดู BUY SELL NO TRADE พร้อมคะแนน %")
async def signal(interaction: discord.Interaction):
    await interaction.response.defer()

    try:
        spot, points, mtf, global_signal, setup = await get_full_analysis_cached()
        direction = setup.get("direction", "NO_TRADE")

        if direction == "BUY":
            title = "🟢 BUY"
        elif direction == "SELL":
            title = "🔴 SELL"
        else:
            title = "🟡 NO TRADE"

        message = (
            "🎯 **XAU/USD SIGNAL V3**\n"
            "━━━━━━━━━━━━━━━━━━\n\n"
            f"💰 Price: **{fmt_price(spot['price'])}**\n\n"
            f"Signal: **{title}**\n"
            f"Score: **{setup.get('score_percent', 0)}%**\n"
            f"Quality: **{signal_quality(setup.get('score_percent', 0))}**\n"
            f"Bias: **{signal_text(global_signal.get('signal', 'NEUTRAL'))}**\n"
            f"Confidence: **{global_signal.get('confidence', 0)}%**\n\n"
            f"🟢 Buy: `{setup.get('buy_percent', 0)}%`\n"
            f"🔴 Sell: `{setup.get('sell_percent', 0)}%`"
        )

        if setup.get("block_reason"):
            message += f"\n\nℹ️ {setup['block_reason']}"
        if setup.get("info_note"):
            message += f"\n\nℹ️ {setup['info_note']}"

        if direction in ("BUY", "SELL"):
            message += (
                "\n\n🛡️ **RISK**\n"
                f"Entry: `{fmt_price(setup.get('entry'))}`\n"
                f"SL: `{fmt_price(setup.get('stop'))}`\n"
                f"TP1: `{fmt_price(setup.get('tp1'))}`\n"
                f"TP2: `{fmt_price(setup.get('tp2'))}`"
            )

        reasons = setup.get("reasons", []) or []
        if reasons:
            message += "\n\n🔎 **REASONS**"
            for reason in reasons[:10]:
                message += f"\n• {reason}"

        await interaction.followup.send(message)

    except Exception as e:
        logger.error(f"SIGNAL ERROR: {repr(e)}")
        await interaction.followup.send(f"❌ SIGNAL ERROR\n`{str(e)[:700]}`")


# ============================================================
# /TREND
# ============================================================

@bot.tree.command(name="trend", description="ดูแนวโน้ม 5M 15M 1H 4H")
async def trend(interaction: discord.Interaction):
    await interaction.response.defer()

    try:
        spot, points, mtf, global_signal, setup = await get_full_analysis_cached()

        lines = [
            "📊 **XAU/USD TREND V3**",
            "━━━━━━━━━━━━━━━━━━",
            "",
            f"Price: **{fmt_price(spot['price'])}**",
            "",
        ]

        for label in ["5M", "15M", "1H", "4H"]:
            data = mtf.get(label, {})
            if data.get("ready", False):
                lines.append(
                    f"{trend_icon(data.get('trend'))} **{label}: {data.get('trend')}** "
                    f"(strength {data.get('trend_strength', 0):.0f})"
                )
            else:
                lines.append(f"⚠️ {label}: DATA NOT READY")

        lines.extend([
            "",
            f"🎯 Bias: **{signal_text(global_signal.get('signal', 'NEUTRAL'))}**",
            f"📊 Confidence: **{global_signal.get('confidence', 0)}%**",
            "",
            f"🧠 Setup: **{setup.get('direction', 'NO_TRADE')}**",
            f"⭐ Score: **{setup.get('score_percent', 0)}%**",
        ])

        await interaction.followup.send("\n".join(lines))

    except Exception as e:
        logger.error(f"TREND ERROR: {repr(e)}")
        await interaction.followup.send(f"❌ TREND ERROR\n`{str(e)[:700]}`")


# ============================================================
# /LEVELS
# ============================================================

@bot.tree.command(name="levels", description="ดูแนวรับแนวต้านและ Fibonacci")
async def levels(interaction: discord.Interaction):
    await interaction.response.defer()

    try:
        spot, points, mtf, global_signal, setup = await get_full_analysis_cached()

        h1 = mtf.get("1H", {})

        support = h1.get("support")
        support2 = h1.get("support2")
        resistance = h1.get("resistance")
        resistance2 = h1.get("resistance2")
        breakout = h1.get("breakout", {}) or {}
        fib = h1.get("fib")

        message = (
            "🧱 **XAU/USD MARKET LEVELS**\n"
            "━━━━━━━━━━━━━━━━━━\n\n"
            f"💰 Current: **{fmt_price(spot['price'])}**\n\n"
            f"🟢 Support 1: **{fmt_price(support)}**\n"
            f"🟢 Support 2: **{fmt_price(support2)}**\n\n"
            f"🔴 Resistance 1: **{fmt_price(resistance)}**\n"
            f"🔴 Resistance 2: **{fmt_price(resistance2)}**\n\n"
            f"🚀 Breakout: `{breakout.get('type', 'NONE')}`\n\n"
        )

        if fib:
            message += (
                "📐 **FIBONACCI**\n"
                f"0%: `{fmt_price(fib.get('0.0'))}`\n"
                f"23.6%: `{fmt_price(fib.get('23.6'))}`\n"
                f"38.2%: `{fmt_price(fib.get('38.2'))}`\n"
                f"50%: `{fmt_price(fib.get('50.0'))}`\n"
                f"61.8%: `{fmt_price(fib.get('61.8'))}`\n"
                f"78.6%: `{fmt_price(fib.get('78.6'))}`\n"
                f"100%: `{fmt_price(fib.get('100.0'))}`"
            )
        else:
            message += "📐 **FIBONACCI**\n⚠️ ยังไม่สามารถคำนวณ Fibonacci ได้จากข้อมูลปัจจุบัน"

        await interaction.followup.send(message)

    except Exception as e:
        logger.error(f"LEVELS ERROR: {repr(e)}")
        traceback.print_exc()
        await interaction.followup.send(f"❌ LEVELS ERROR\n`{str(e)[:700]}`")


# ============================================================
# /PATTERN
# ============================================================

@bot.tree.command(name="pattern", description="ดู Candlestick Pattern")
async def pattern(interaction: discord.Interaction):
    await interaction.response.defer()

    try:
        spot, points, mtf, global_signal, setup = await get_full_analysis_cached()

        lines = ["🕯️ **XAU/USD CANDLE PATTERN**", "━━━━━━━━━━━━━━━━━━", ""]

        for label in ["5M", "15M", "1H", "4H"]:
            data = mtf.get(label, {})
            if data.get("ready", False):
                pattern_data = data.get("pattern", {}) or {}
                lines.append(f"{label}: **{pattern_data.get('name', 'NONE')}**")
            else:
                lines.append(f"{label}: DATA NOT READY")

        await interaction.followup.send("\n".join(lines))

    except Exception as e:
        logger.error(f"PATTERN ERROR: {repr(e)}")
        await interaction.followup.send(f"❌ PATTERN ERROR\n`{str(e)[:700]}`")


# ============================================================
# /CHART
# ============================================================

@bot.tree.command(name="chart", description="กราฟ XAU/USD พร้อม EMA")
async def chart(interaction: discord.Interaction):
    await interaction.response.defer()

    try:
        points = await asyncio.to_thread(get_xau_intraday, 48)
        path = await asyncio.to_thread(make_chart, points)

        if path is None:
            await interaction.followup.send("⚠️ ข้อมูลกราฟไม่พอ")
            return

        await interaction.followup.send(file=discord.File(path))

        try:
            os.remove(path)
        except Exception:
            pass

    except Exception as e:
        logger.error(f"CHART ERROR: {repr(e)}")
        await interaction.followup.send(f"❌ CHART ERROR\n`{str(e)[:700]}`")


# ============================================================
# /STATUS
# ============================================================

@bot.tree.command(name="status", description="ตรวจสอบสถานะบอท")
async def status(interaction: discord.Interaction):
    uptime = now_thai() - bot_start_time
    total_seconds = int(uptime.total_seconds())
    hours = total_seconds // 3600
    minutes = (total_seconds % 3600) // 60

    global last_alert_time, last_alert_signal

    last_alert_text = "ยังไม่เคยแจ้งเตือน"
    if last_alert_time is not None:
        elapsed_min = int((now_thai() - last_alert_time).total_seconds() / 60)
        last_alert_text = f"{last_alert_signal} เมื่อ {elapsed_min} นาทีที่แล้ว"

    gs_status = "🟢 เชื่อมต่อแล้ว" if _gs_enabled else "🔴 ไม่ได้เชื่อมต่อ (ตั้งค่า ENV VARS)"

    await interaction.response.send_message(
        "🤖 **GOLD BOT V3 STATUS**\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        "🟢 Discord: ONLINE\n"
        "🟢 Render: RUNNING\n"
        "🟢 XAU API: READY\n"
        "🟢 Smart Analysis Engine: READY\n"
        f"📄 Google Sheets: {gs_status}\n"
        f"   - Price rows (cache): `{len(_price_history_cache)}`\n"
        f"   - Signal rows (cache): `{len(_signal_history_cache)}`\n"
        f"🛡️ Circuit Breaker: {circuit_status_text()}\n\n"
        f"⏱️ Uptime: `{hours}h {minutes}m`\n"
        f"⏰ Monitor: ทุก `{CHECK_INTERVAL_MINUTES}` นาที\n"
        f"🔔 แจ้งเตือนล่าสุด: {last_alert_text}\n\n"
        "ใช้ `/diagnose` เพื่อดูรายละเอียดว่าทำไมรอบล่าสุดถึงแจ้งเตือนหรือไม่"
    )


# ============================================================
# /DIAGNOSE
# ============================================================

@bot.tree.command(name="diagnose", description="ดูรายละเอียดว่าทำไมรอบล่าสุดแจ้งเตือนหรือไม่")
async def diagnose(interaction: discord.Interaction):
    await interaction.response.defer()

    global last_analysis_snapshot, last_alert_time, last_alert_signal

    if not last_analysis_snapshot:
        await interaction.followup.send(
            "⚠️ ยังไม่มีข้อมูลการวิเคราะห์ล่าสุด (รอ monitor loop รอบแรกทำงานก่อน)"
        )
        return

    snap = last_analysis_snapshot

    lines = [
        "🔬 **DIAGNOSE - รอบล่าสุด**",
        "━━━━━━━━━━━━━━━━━━",
        "",
        f"เวลา: `{snap.get('time', 'N/A')}`",
        f"ราคา: **{fmt_price(snap.get('price'))}**",
        "",
        f"Direction: **{snap.get('direction', 'N/A')}**",
        f"Buy Score: `{snap.get('buy_percent', 0)}%`",
        f"Sell Score: `{snap.get('sell_percent', 0)}%`",
        f"Confidence: `{snap.get('confidence', 0)}%`",
        f"ATR Percentile: `{fmt_percent(snap.get('atr_percentile'))}`",
        f"4H Timeframe พร้อมหรือยัง: `{'✅ พร้อม' if snap.get('h4_ready') else '⏳ กำลังสะสมข้อมูล'}`",
        f"Google Sheets: `{'✅ เชื่อมต่อ' if _gs_enabled else '❌ ไม่ได้เชื่อมต่อ'}`",
        f"Circuit Breaker: {circuit_status_text()}",
        "",
        "**เกณฑ์ที่ตั้งไว้ตอนนี้:**",
        f"MIN_SIGNAL_PERCENT = `{MIN_SIGNAL_PERCENT}%`",
        f"MIN_CONFIDENCE = `{MIN_CONFIDENCE}%`",
        f"DIRECTION_MARGIN_PERCENT = `{DIRECTION_MARGIN_PERCENT}%`",
        f"MIN_ATR_PERCENTILE = `{MIN_ATR_PERCENTILE}%`",
        f"ALERT_COOLDOWN_MINUTES = `{ALERT_COOLDOWN_MINUTES}`",
        f"CIRCUIT_FAIL_THRESHOLD = `{CIRCUIT_FAIL_THRESHOLD}` ครั้ง",
        f"CIRCUIT_COOLDOWN_MINUTES = `{CIRCUIT_COOLDOWN_MINUTES}` นาที",
        "",
    ]

    if snap.get("info_note"):
        lines.append(f"ℹ️ {snap['info_note']}")

    if snap.get("block_reason"):
        lines.append(f"⛔ **เหตุผลที่ไม่เข้าเงื่อนไข**: {snap['block_reason']}")
    elif snap.get("confidence", 0) < MIN_CONFIDENCE:
        lines.append(
            f"⛔ **เหตุผล**: Confidence {snap.get('confidence', 0)}% "
            f"ต่ำกว่าเกณฑ์ {MIN_CONFIDENCE}%"
        )
    else:
        lines.append("✅ รอบนี้ผ่านเกณฑ์การวิเคราะห์เบื้องต้น")

    lines.append("")

    if last_alert_time is not None:
        elapsed = (now_thai() - last_alert_time).total_seconds() / 60
        cooldown_left = max(0, ALERT_COOLDOWN_MINUTES - elapsed)
        lines.append(f"🕐 แจ้งเตือนล่าสุด: **{last_alert_signal}** ({elapsed:.0f} นาทีที่แล้ว)")
        if cooldown_left > 0:
            lines.append(f"⏳ Cooldown เหลืออีก: **{cooldown_left:.0f} นาที**")
    else:
        lines.append("🕐 ยังไม่เคยแจ้งเตือนตั้งแต่บอทเริ่มทำงาน")

    await interaction.followup.send("\n".join(lines))


# ============================================================
# /CONFIG
# ============================================================

@bot.tree.command(name="config", description="ดูค่า threshold ปัจจุบันของบอท")
async def config_cmd(interaction: discord.Interaction):
    lines = [
        "⚙️ **BOT CONFIG (ปรับได้ผ่าน ENV VARS บน Render)**",
        "━━━━━━━━━━━━━━━━━━",
        "",
        f"`MIN_SIGNAL_PERCENT` = {MIN_SIGNAL_PERCENT}% "
        "(คะแนนขั้นต่ำที่ถือว่าเป็น setup ที่ใช้ได้)",
        f"`STRONG_SIGNAL_PERCENT` = {STRONG_SIGNAL_PERCENT}% (เกณฑ์สัญญาณแรง)",
        f"`MIN_CONFIDENCE` = {MIN_CONFIDENCE}% (ความมั่นใจขั้นต่ำของ MTF bias)",
        f"`DIRECTION_MARGIN_PERCENT` = {DIRECTION_MARGIN_PERCENT}% "
        "(buy ต้องนำ sell เท่านี้ถึงจะฟันธงทิศทาง)",
        f"`MIN_ATR_PERCENTILE` = {MIN_ATR_PERCENTILE}% (กรองตลาดนิ่งเกินไป)",
        f"`ALERT_COOLDOWN_MINUTES` = {ALERT_COOLDOWN_MINUTES} นาที",
        f"`RE_ALERT_SCORE_INCREASE` = {RE_ALERT_SCORE_INCREASE}% "
        "(คะแนนต้องเพิ่มเท่านี้ถึงจะแจ้งซ้ำทิศทางเดิม)",
        f"`CHECK_INTERVAL_MINUTES` = {CHECK_INTERVAL_MINUTES} นาที",
        "",
        f"`CIRCUIT_FAIL_THRESHOLD` = {CIRCUIT_FAIL_THRESHOLD} ครั้ง "
        "(fail ติดกันกี่ครั้งถึงจะหยุดยิง API ชั่วคราว)",
        f"`CIRCUIT_COOLDOWN_MINUTES` = {CIRCUIT_COOLDOWN_MINUTES} นาที "
        "(หยุดยิง API นานแค่ไหนก่อนลองใหม่)",
        f"`ANALYSIS_CACHE_SECONDS` = {ANALYSIS_CACHE_SECONDS} วิ "
        "(แคชผลวิเคราะห์กันหลายคำสั่ง/monitor ยิงซ้อนกัน)",
        f"Circuit Breaker ปัจจุบัน: {circuit_status_text()}",
        "",
        f"`GOOGLE_SHEET_ID` = `{'ตั้งค่าแล้ว' if GOOGLE_SHEET_ID else 'ยังไม่ได้ตั้งค่า'}`",
        f"`GOOGLE_SERVICE_ACCOUNT_JSON` = `{'ตั้งค่าแล้ว' if GOOGLE_SERVICE_ACCOUNT_JSON else 'ยังไม่ได้ตั้งค่า'}`",
        f"Google Sheets connection: `{'✅ เชื่อมต่อ' if _gs_enabled else '❌ ไม่ได้เชื่อมต่อ'}`",
        "",
        "💡 ถ้าอยากให้บอทแจ้งเตือนถี่ขึ้น ลองลด "
        "`MIN_SIGNAL_PERCENT`, `MIN_CONFIDENCE` หรือ `DIRECTION_MARGIN_PERCENT` "
        "ใน Render Environment Variables แล้ว restart",
    ]

    await interaction.response.send_message("\n".join(lines))


# ============================================================
# /HELP
# ============================================================

@bot.tree.command(name="help", description="ดูคำสั่งทั้งหมด")
async def help_command(interaction: discord.Interaction):
    message = (
        "🪙 **GOLD BOT V3 COMMANDS**\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "`/gold` ราคาปัจจุบัน XAU/USD\n"
        "`/xau` วิเคราะห์เต็มทุกระบบ\n"
        "`/analysis` EMA RSI MACD ATR MTF Divergence\n"
        "`/signal` BUY / SELL / NO TRADE พร้อม %\n"
        "`/trend` 5M / 15M / 1H / 4H\n"
        "`/levels` Support / Resistance / Fibonacci\n"
        "`/pattern` Candlestick Pattern\n"
        "`/chart` กราฟ XAU/USD + EMA\n"
        "`/status` สถานะระบบ (รวม Google Sheets + Circuit Breaker)\n"
        "`/diagnose` ดูว่าทำไมรอบล่าสุดแจ้งเตือนหรือไม่\n"
        "`/config` ดูค่า threshold ปัจจุบัน\n"
        "`/help` คำสั่งทั้งหมด\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "🧠 EMA / RSI / MACD / ATR\n"
        "📊 Multi Timeframe Confluence\n"
        "🧱 Market Structure\n"
        "🚀 Breakout / Retest\n"
        "🕯️ Candlestick (+ Doji, Star)\n"
        "📐 Fibonacci\n"
        "🔀 RSI Divergence\n"
        "📈 Trend Strength Regression\n"
        "📉 Volatility Filter\n"
        "⭐ Normalized Confluence Score %\n"
        "🛡️ SL / TP / R:R\n"
        "🔔 Smart Re-Alert Logic\n"
        "📄 Google Sheets Persistence (ข้อมูลไม่หายเมื่อ Render restart)\n"
        "🧯 Circuit Breaker (กันโดน Render แบนตอน API ล่ม/ตลาดปิด)"
    )

    await interaction.response.send_message(message)


# ============================================================
# MONITOR LOOP
# ============================================================

@tasks.loop(minutes=CHECK_INTERVAL_MINUTES)
async def monitor_gold():
    global last_price, last_alert_time, last_alert_signal
    global last_alert_score_percent, last_analysis_snapshot

    try:
        logger.info("=" * 60)
        logger.info(f"[{now_thai().strftime('%Y-%m-%d %H:%M:%S')}] XAU/USD V3 CHECK เริ่มรอบตรวจสอบ")

        if circuit_is_open():
            logger.info(f"ข้ามรอบนี้: circuit breaker เปิดอยู่ ({circuit_status_text()})")
            return

        spot, points, mtf, global_signal, setup = await get_full_analysis_cached()

        price = spot["price"]
        await asyncio.to_thread(append_history, price)

        direction = setup.get("direction", "NO_TRADE")
        score_percent = setup.get("score_percent", 0)
        confidence = global_signal.get("confidence", 0)
        h1 = mtf.get("1H", {})

        last_analysis_snapshot = {
            "time": now_thai().isoformat(),
            "price": price,
            "direction": direction,
            "buy_percent": setup.get("buy_percent", 0),
            "sell_percent": setup.get("sell_percent", 0),
            "confidence": confidence,
            "atr_percentile": h1.get("atr_percentile"),
            "block_reason": setup.get("block_reason"),
            "info_note": setup.get("info_note"),
            "h4_ready": setup.get("h4_ready"),
        }

        logger.info(f"PRICE: {price:.2f}")
        logger.info(f"BIAS: {global_signal.get('signal')}  | CONFIDENCE: {confidence}%")
        logger.info(
            f"SETUP: {direction} | SCORE: {score_percent}% "
            f"(Buy {setup.get('buy_percent', 0)}% / Sell {setup.get('sell_percent', 0)}%)"
        )
        if setup.get("block_reason"):
            logger.info(f"BLOCK REASON: {setup['block_reason']}")
        if setup.get("info_note"):
            logger.info(f"INFO: {setup['info_note']}")

        if last_price is not None:
            movement = price - last_price
            logger.info(f"MOVE: {movement:+.2f}")

        last_price = price

        if direction not in ("BUY", "SELL"):
            logger.info("ไม่แจ้งเตือน: direction = NO_TRADE")
            return

        if score_percent < MIN_SIGNAL_PERCENT:
            logger.info(f"ไม่แจ้งเตือน: score {score_percent}% < {MIN_SIGNAL_PERCENT}%")
            return

        if confidence < MIN_CONFIDENCE:
            logger.info(f"ไม่แจ้งเตือน: confidence {confidence}% < {MIN_CONFIDENCE}%")
            return

        if not setup.get("volatility_ok", True):
            logger.info("ไม่แจ้งเตือน: volatility ต่ำเกินไป (ตลาด sideways)")
            return

        current_time = now_thai()
        is_re_alert = False

        if last_alert_time is not None:
            elapsed_minutes = (current_time - last_alert_time).total_seconds() / 60

            if elapsed_minutes < ALERT_COOLDOWN_MINUTES:
                logger.info(
                    f"ไม่แจ้งเตือน: อยู่ใน cooldown "
                    f"({elapsed_minutes:.1f}/{ALERT_COOLDOWN_MINUTES} นาที)"
                )
                return

            if direction == last_alert_signal:
                score_increase = score_percent - last_alert_score_percent

                if score_increase < RE_ALERT_SCORE_INCREASE:
                    logger.info(
                        f"ไม่แจ้งเตือน: ทิศทางเดิม ({direction}) และคะแนนเพิ่มแค่ "
                        f"{score_increase:.1f}% (< {RE_ALERT_SCORE_INCREASE}%)"
                    )
                    return

                is_re_alert = True
                logger.info(
                    f"RE-ALERT: ทิศทางเดิมแต่คะแนนแข็งแกร่งขึ้น "
                    f"+{score_increase:.1f}%"
                )

        if not ALERT_CHANNEL_ID:
            logger.warning("ไม่แจ้งเตือน: ALERT_CHANNEL_ID ยังไม่ได้ตั้งค่า (= 0)")
            return

        channel = bot.get_channel(ALERT_CHANNEL_ID)

        if channel is None:
            try:
                channel = await bot.fetch_channel(ALERT_CHANNEL_ID)
            except Exception as e:
                logger.error(f"FETCH CHANNEL ERROR: {repr(e)}")
                return

        message = build_alert_message(spot, mtf, global_signal, setup, is_reAlert=is_re_alert)

        if message:
            await channel.send(message)

            await asyncio.to_thread(record_signal, spot, setup, global_signal)

            last_alert_time = current_time
            last_alert_signal = direction
            last_alert_score_percent = score_percent

            logger.info(f"✅ SMART ALERT SENT: {direction} @ {score_percent}%")

    except Exception as e:
        logger.error(f"MONITOR ERROR: {repr(e)}")
        traceback.print_exc()


@monitor_gold.before_loop
async def before_monitor():
    await bot.wait_until_ready()
    logger.info("XAU/USD V3 MONITOR READY")


# ============================================================
# BOT READY
# ============================================================

@bot.event
async def on_ready():
    logger.info("=" * 70)
    logger.info("DISCORD BOT READY")
    logger.info(f"BOT: {bot.user}")
    logger.info(f"ID: {bot.user.id}")
    logger.info("=" * 70)

    try:
        synced = await bot.tree.sync()
        logger.info(f"SLASH COMMANDS SYNCED: {len(synced)}")
        for command in synced:
            logger.info(f"  /{command.name}")
    except Exception as e:
        logger.error(f"COMMAND SYNC ERROR: {repr(e)}")

    if not monitor_gold.is_running():
        monitor_gold.start()


# ============================================================
# MAIN
# ============================================================

async def main():
    logger.info("=" * 70)
    logger.info("STARTING XAU/USD GOLD DISCORD BOT V3 - SMART ANALYSIS EDITION (Google Sheets)")
    logger.info("=" * 70)

    if not DISCORD_TOKEN:
        raise RuntimeError("DISCORD_TOKEN is not configured")

    # เชื่อมต่อ Google Sheets และโหลด cache ก่อนเริ่ม bot / web server
    await asyncio.to_thread(init_google_sheets)
    await asyncio.to_thread(load_price_history_cache_from_sheet)
    await asyncio.to_thread(load_signal_history_cache_from_sheet)

    await start_web_server()

    logger.info("Connecting to Discord...")
    await bot.start(DISCORD_TOKEN)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("BOT STOPPED")
    except Exception as e:
        logger.error(f"FATAL ERROR: {repr(e)}")
        traceback.print_exc()
