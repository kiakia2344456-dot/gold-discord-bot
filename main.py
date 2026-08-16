import os
import json
import asyncio
import traceback
from datetime import datetime, timedelta, timezone

import requests
import discord
from discord.ext import commands, tasks
from discord import app_commands

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates

from aiohttp import web


# ============================================================
# GOLD DISCORD BOT
# XAU/USD 24H MONITOR
# EMA / RSI / MACD / ATR / MTF / ALERT
# FREE - NO API KEY
# ============================================================


# ============================================================
# CONFIG
# ============================================================

TZ = timezone(timedelta(hours=7))

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "").strip()

ALERT_CHANNEL_ID = int(
    os.getenv("ALERT_CHANNEL_ID", "0")
)

# ตรวจราคาทุก 2 นาที
CHECK_INTERVAL_MINUTES = 2

# API
XAU_SPOT_URL = "https://xaus.com/api/v1/spot"
XAU_INTRADAY_URL = "https://xaus.com/api/v1/intraday"
XAU_HISTORY_URL = "https://xaus.com/api/v1/history"

# ไฟล์เก็บข้อมูล
HISTORY_FILE = "xau_history.json"

# เก็บข้อมูลใน local bot
HISTORY_KEEP_DAYS = 14

# Alert
ALERT_COOLDOWN_MINUTES = 30

# RSI
RSI_PERIOD = 14

# EMA
EMA_FAST = 9
EMA_MID = 21
EMA_SLOW = 50
EMA_LONG = 200

# MACD
MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9

# ATR
ATR_PERIOD = 14

# ============================================================
# GLOBAL
# ============================================================

last_alert_time = None
last_alert_signal = None
last_price = None

bot_start_time = datetime.now(TZ)


# ============================================================
# WEB SERVER FOR RENDER
# ============================================================

async def handle_health_check(request):
    return web.json_response({
        "status": "ok",
        "bot": "Gold Discord Bot",
        "time": datetime.now(TZ).isoformat()
    })


async def start_web_server():

    app = web.Application()

    app.router.add_get("/", handle_health_check)
    app.router.add_get("/health", handle_health_check)

    runner = web.AppRunner(app)

    await runner.setup()

    port = int(os.getenv("PORT", "10000"))

    site = web.TCPSite(
        runner,
        "0.0.0.0",
        port
    )

    await site.start()

    print("=" * 60)
    print(f"WEB SERVER ACTIVE : PORT {port}")
    print("=" * 60)


# ============================================================
# DISCORD
# ============================================================

intents = discord.Intents.default()

bot = commands.Bot(
    command_prefix="!",
    intents=intents
)


# ============================================================
# HTTP SESSION
# ============================================================

session = requests.Session()

session.headers.update({
    "User-Agent":
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 Chrome/120 Safari/537.36",
    "Accept": "application/json"
})


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


def fmt_change(value):

    if value is None:
        return "N/A"

    return f"{value:+.2f}"


# ============================================================
# XAU/USD SPOT
# ============================================================

def get_xau_spot():

    try:

        cache_buster = int(datetime.now().timestamp())

        response = session.get(
            XAU_SPOT_URL,
            params={
                "currency": "USD",
                "unit": "oz",
                "compact": "1",
                "fresh": cache_buster
            },
            timeout=15
        )

        if response.status_code != 200:

            raise RuntimeError(
                f"HTTP {response.status_code}: "
                f"{response.text[:300]}"
            )

        data = response.json()

        price = safe_float(
            data.get("spot_usd_oz")
        )

        if price is None:

            raise ValueError(
                f"ไม่พบ spot_usd_oz: {data}"
            )

        return {
            "price": price,
            "updated_at": data.get("updated_at"),
            "price_as_of": data.get("price_as_of"),
            "data_state": data.get("data_state"),
            "source": data.get("price_source"),
            "silver": safe_float(
                data.get("silver_usd_oz")
            ),
            "fx_rate": (
                data.get("fx_rates", {}).get("THB")
                if isinstance(data.get("fx_rates"), dict)
                else None
            )
        }

    except Exception as e:

        print("XAU SPOT ERROR:", e)

        return None


# ============================================================
# XAU INTRADAY
# ============================================================

def get_xau_intraday(hours=48):

    try:

        response = session.get(
            XAU_INTRADAY_URL,
            params={
                "symbol": "xau",
                "hours": hours
            },
            timeout=20
        )

        if response.status_code != 200:

            raise RuntimeError(
                f"HTTP {response.status_code}: "
                f"{response.text[:300]}"
            )

        data = response.json()

        raw_points = data.get("points", [])

        points = []

        for item in raw_points:

            try:

                timestamp = item.get("t")
                price = safe_float(item.get("p"))

                if timestamp is None or price is None:
                    continue

                # รองรับทั้ง unix timestamp และ ISO
                if isinstance(timestamp, (int, float)):

                    dt = datetime.fromtimestamp(
                        timestamp,
                        timezone.utc
                    ).astimezone(TZ)

                else:

                    dt = datetime.fromisoformat(
                        str(timestamp).replace("Z", "+00:00")
                    )

                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)

                    dt = dt.astimezone(TZ)

                points.append({
                    "time": dt,
                    "price": price
                })

            except Exception:
                continue

        points.sort(
            key=lambda x: x["time"]
        )

        return points

    except Exception as e:

        print("XAU INTRADAY ERROR:", e)

        return []


# ============================================================
# DAILY HISTORY
# ============================================================

def get_xau_daily_history():

    try:

        response = session.get(
            XAU_HISTORY_URL,
            timeout=20
        )

        if response.status_code != 200:

            raise RuntimeError(
                f"HTTP {response.status_code}: "
                f"{response.text[:300]}"
            )

        data = response.json()

        points = []

        for item in data.get("points", []):

            close = safe_float(
                item.get("c")
            )

            high = safe_float(
                item.get("h")
            )

            low = safe_float(
                item.get("l")
            )

            date_value = item.get("d")

            if close is None or not date_value:
                continue

            points.append({
                "date": date_value,
                "close": close,
                "high": high if high is not None else close,
                "low": low if low is not None else close
            })

        return points

    except Exception as e:

        print("XAU DAILY ERROR:", e)

        return []


# ============================================================
# LOCAL HISTORY
# ============================================================

def load_local_history():

    if not os.path.exists(HISTORY_FILE):
        return []

    try:

        with open(
            HISTORY_FILE,
            "r",
            encoding="utf-8"
        ) as f:

            return json.load(f)

    except Exception as e:

        print("LOCAL HISTORY READ ERROR:", e)

        return []


def save_local_history(history):

    try:

        with open(
            HISTORY_FILE,
            "w",
            encoding="utf-8"
        ) as f:

            json.dump(
                history,
                f,
                ensure_ascii=False
            )

    except Exception as e:

        print("LOCAL HISTORY SAVE ERROR:", e)


def append_local_history(price):

    history = load_local_history()

    current_time = now_thai()

    history.append({
        "time": current_time.isoformat(),
        "price": price
    })

    cutoff = current_time - timedelta(
        days=HISTORY_KEEP_DAYS
    )

    cleaned = []

    for item in history:

        try:

            dt = datetime.fromisoformat(
                item["time"]
            )

            if dt >= cutoff:

                cleaned.append(item)

        except Exception:
            continue

    save_local_history(cleaned)

    return cleaned


# ============================================================
# EMA
# ============================================================

def calculate_ema(values, period):

    if len(values) < period:
        return None

    multiplier = 2 / (period + 1)

    ema_value = sum(
        values[:period]
    ) / period

    for price in values[period:]:

        ema_value = (
            (price - ema_value)
            * multiplier
        ) + ema_value

    return ema_value


# ============================================================
# RSI
# ============================================================

def calculate_rsi(values, period=14):

    if len(values) < period + 1:
        return None

    gains = []
    losses = []

    for i in range(1, len(values)):

        change = values[i] - values[i - 1]

        if change > 0:

            gains.append(change)
            losses.append(0)

        else:

            gains.append(0)
            losses.append(abs(change))

    avg_gain = sum(
        gains[:period]
    ) / period

    avg_loss = sum(
        losses[:period]
    ) / period

    for i in range(
        period,
        len(gains)
    ):

        avg_gain = (
            (avg_gain * (period - 1))
            + gains[i]
        ) / period

        avg_loss = (
            (avg_loss * (period - 1))
            + losses[i]
        ) / period

    if avg_loss == 0:

        return 100

    rs = avg_gain / avg_loss

    return 100 - (
        100 / (1 + rs)
    )


# ============================================================
# MACD
# ============================================================

def calculate_macd(values):

    if len(values) < MACD_SLOW + MACD_SIGNAL:

        return None

    fast = calculate_ema(
        values,
        MACD_FAST
    )

    slow = calculate_ema(
        values,
        MACD_SLOW
    )

    if fast is None or slow is None:

        return None

    # สร้าง MACD series
    macd_values = []

    for i in range(
        MACD_SLOW,
        len(values) + 1
    ):

        subset = values[:i]

        fast_i = calculate_ema(
            subset,
            MACD_FAST
        )

        slow_i = calculate_ema(
            subset,
            MACD_SLOW
        )

        if fast_i is not None and slow_i is not None:

            macd_values.append(
                fast_i - slow_i
            )

    if len(macd_values) < MACD_SIGNAL:

        return None

    signal = calculate_ema(
        macd_values,
        MACD_SIGNAL
    )

    macd = macd_values[-1]

    histogram = (
        macd - signal
        if signal is not None
        else None
    )

    return {
        "macd": macd,
        "signal": signal,
        "histogram": histogram
    }


# ============================================================
# ATR
# ============================================================

def calculate_atr(
    prices,
    period=14
):

    if len(prices) < period + 1:

        return None

    # Intraday dataset ของเรามี price อย่างเดียว
    # จึงใช้ absolute movement เป็น proxy
    true_ranges = []

    for i in range(1, len(prices)):

        true_ranges.append(
            abs(
                prices[i]
                - prices[i - 1]
            )
        )

    if len(true_ranges) < period:

        return None

    return sum(
        true_ranges[-period:]
    ) / period


# ============================================================
# SUPPORT / RESISTANCE
# ============================================================

def calculate_support_resistance(
    prices,
    window=30
):

    if not prices:
        return None, None

    recent = prices[-window:]

    support = min(recent)

    resistance = max(recent)

    return support, resistance


# ============================================================
# TIMEFRAME AGGREGATION
# ============================================================

def aggregate_prices(
    points,
    minutes
):

    if not points:
        return []

    buckets = {}

    for item in points:

        dt = item["time"]

        minute_bucket = (
            dt.hour * 60
            + dt.minute
        )

        bucket_index = (
            minute_bucket // minutes
        ) * minutes

        bucket_hour = bucket_index // 60
        bucket_minute = bucket_index % 60

        bucket_time = dt.replace(
            hour=bucket_hour,
            minute=bucket_minute,
            second=0,
            microsecond=0
        )

        key = bucket_time.isoformat()

        buckets.setdefault(
            key,
            []
        ).append(
            item["price"]
        )

    result = []

    for key in sorted(buckets):

        values = buckets[key]

        result.append({
            "time": datetime.fromisoformat(key),
            "price": values[-1]
        })

    return result


# ============================================================
# TIMEFRAME ANALYSIS
# ============================================================

def analyze_timeframe(
    points,
    label
):

    if len(points) < 30:

        return {
            "label": label,
            "ready": False
        }

    prices = [
        x["price"]
        for x in points
    ]

    current = prices[-1]

    ema9 = calculate_ema(
        prices,
        EMA_FAST
    )

    ema21 = calculate_ema(
        prices,
        EMA_MID
    )

    ema50 = calculate_ema(
        prices,
        EMA_SLOW
    )

    ema200 = calculate_ema(
        prices,
        EMA_LONG
    )

    rsi = calculate_rsi(
        prices,
        RSI_PERIOD
    )

    macd = calculate_macd(
        prices
    )

    atr = calculate_atr(
        prices,
        ATR_PERIOD
    )

    bullish_score = 0
    bearish_score = 0

    # EMA structure
    if ema9 and ema21:

        if ema9 > ema21:
            bullish_score += 1

        elif ema9 < ema21:
            bearish_score += 1

    if ema21 and ema50:

        if ema21 > ema50:
            bullish_score += 1

        elif ema21 < ema50:
            bearish_score += 1

    if ema50 and ema200:

        if ema50 > ema200:
            bullish_score += 2

        elif ema50 < ema200:
            bearish_score += 2

    # Price position
    if ema21:

        if current > ema21:
            bullish_score += 1

        elif current < ema21:
            bearish_score += 1

    # RSI
    if rsi is not None:

        if 50 < rsi < 70:

            bullish_score += 1

        elif 30 < rsi < 50:

            bearish_score += 1

        elif rsi >= 70:

            # overbought ไม่ใช่ sell อัตโนมัติ
            pass

        elif rsi <= 30:

            # oversold ไม่ใช่ buy อัตโนมัติ
            pass

    # MACD
    if macd:

        if macd["histogram"] > 0:
            bullish_score += 1

        elif macd["histogram"] < 0:
            bearish_score += 1

    if bullish_score >= bearish_score + 3:

        trend = "BULLISH"

    elif bearish_score >= bullish_score + 3:

        trend = "BEARISH"

    else:

        trend = "SIDEWAY"

    support, resistance = (
        calculate_support_resistance(
            prices
        )
    )

    return {
        "label": label,
        "ready": True,
        "current": current,
        "ema9": ema9,
        "ema21": ema21,
        "ema50": ema50,
        "ema200": ema200,
        "rsi": rsi,
        "macd": macd,
        "atr": atr,
        "support": support,
        "resistance": resistance,
        "bullish_score": bullish_score,
        "bearish_score": bearish_score,
        "trend": trend,
        "count": len(prices)
    }


# ============================================================
# MULTI TIMEFRAME ANALYSIS
# ============================================================

def analyze_mtf(points):

    timeframes = {
        "5M": 5,
        "15M": 15,
        "1H": 60,
        "4H": 240
    }

    result = {}

    for label, minutes in timeframes.items():

        aggregated = aggregate_prices(
            points,
            minutes
        )

        result[label] = analyze_timeframe(
            aggregated,
            label
        )

    return result


# ============================================================
# GLOBAL SIGNAL
# ============================================================

def build_global_signal(
    mtf
):

    bullish = 0
    bearish = 0

    ready_count = 0

    for label in [
        "5M",
        "15M",
        "1H",
        "4H"
    ]:

        data = mtf.get(label)

        if not data or not data["ready"]:
            continue

        ready_count += 1

        if data["trend"] == "BULLISH":

            if label == "4H":
                bullish += 3

            elif label == "1H":
                bullish += 2

            else:
                bullish += 1

        elif data["trend"] == "BEARISH":

            if label == "4H":
                bearish += 3

            elif label == "1H":
                bearish += 2

            else:
                bearish += 1

    if bullish >= bearish + 3:

        signal = "BUY_BIAS"

    elif bearish >= bullish + 3:

        signal = "SELL_BIAS"

    else:

        signal = "NEUTRAL"

    total = bullish + bearish

    if total == 0:

        confidence = 0

    else:

        confidence = round(
            max(
                bullish,
                bearish
            )
            / total
            * 100
        )

    return {
        "signal": signal,
        "bullish": bullish,
        "bearish": bearish,
        "confidence": confidence,
        "ready": ready_count
    }


# ============================================================
# ANALYSIS MESSAGE
# ============================================================

def trend_icon(trend):

    if trend == "BULLISH":
        return "🟢"

    if trend == "BEARISH":
        return "🔴"

    return "🟡"


def signal_text(signal):

    if signal == "BUY_BIAS":
        return "🟢 BUY BIAS"

    if signal == "SELL_BIAS":
        return "🔴 SELL BIAS"

    return "🟡 NEUTRAL"


def format_timeframe(data):

    if not data["ready"]:

        return (
            f"**{data['label']}** "
            "⚠️ ข้อมูลยังไม่พอ"
        )

    macd = data["macd"]

    if macd:

        macd_text = (
            f"{macd['macd']:.2f} / "
            f"{macd['signal']:.2f}"
        )

    else:

        macd_text = "N/A"

    return (
        f"{trend_icon(data['trend'])} "
        f"**{data['label']} — {data['trend']}**\n"
        f"Price: `{fmt_price(data['current'])}`\n"
        f"EMA9: `{fmt_price(data['ema9'])}`\n"
        f"EMA21: `{fmt_price(data['ema21'])}`\n"
        f"EMA50: `{fmt_price(data['ema50'])}`\n"
        f"EMA200: `{fmt_price(data['ema200'])}`\n"
        f"RSI: `{data['rsi']:.2f}`\n"
        f"MACD/Signal: `{macd_text}`\n"
        f"ATR: `{fmt_price(data['atr'])}`"
    )


def build_analysis_message(
    spot,
    mtf,
    global_signal
):

    price = spot["price"]

    signal = global_signal["signal"]

    confidence = global_signal["confidence"]

    state = spot.get("data_state") or {}

    state_text = state.get(
        "status",
        "unknown"
    )

    message = (
        "🪙 **XAU/USD MARKET ANALYSIS**\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"💰 ราคา XAU/USD: **{fmt_price(price)}**\n"
        f"📡 Data state: `{state_text}`\n"
        f"🕐 เวลาไทย: `{now_thai().strftime('%d/%m/%Y %H:%M:%S')}`\n\n"
        f"🎯 ภาพรวม: **{signal_text(signal)}**\n"
        f"📊 Confidence: **{confidence}%**\n"
        f"🟢 Bullish score: `{global_signal['bullish']}`\n"
        f"🔴 Bearish score: `{global_signal['bearish']}`\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "📊 **MULTI TIMEFRAME**\n\n"
    )

    for label in [
        "5M",
        "15M",
        "1H",
        "4H"
    ]:

        message += (
            format_timeframe(
                mtf[label]
            )
            + "\n\n"
        )

    message += (
        "━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "⚠️ **หมายเหตุ**\n"
        "ระบบนี้เป็นการวิเคราะห์เชิงเทคนิค "
        "ไม่ใช่การรับประกันว่าราคาจะขึ้นหรือลง\n"
        "ไม่ควรใช้สัญญาณเดียวในการเปิดออเดอร์"
    )

    return message


# ============================================================
# ALERT LOGIC
# ============================================================

def should_alert(
    signal,
    confidence
):

    global last_alert_time
    global last_alert_signal

    if signal == "NEUTRAL":

        return False

    if confidence < 70:

        return False

    current = now_thai()

    if last_alert_time is not None:

        elapsed = (
            current
            - last_alert_time
        ).total_seconds() / 60

        if elapsed < ALERT_COOLDOWN_MINUTES:

            return False

    # ไม่ยิง signal เดิมซ้ำ
    if signal == last_alert_signal:

        return False

    return True


# ============================================================
# ALERT MESSAGE
# ============================================================

def build_alert_message(
    spot,
    mtf,
    global_signal
):

    signal = global_signal["signal"]

    confidence = global_signal["confidence"]

    price = spot["price"]

    if signal == "BUY_BIAS":

        title = "🟢 XAU/USD BUY BIAS"

    else:

        title = "🔴 XAU/USD SELL BIAS"

    lines = [
        "🔔 **GOLD SIGNAL ALERT**",
        "━━━━━━━━━━━━━━━━━━━━━━━━",
        "",
        f"{title}",
        "",
        f"💰 ราคา: **{fmt_price(price)}**",
        f"📊 Confidence: **{confidence}%**",
        "",
        "📈 **MTF CONFIRMATION**"
    ]

    for label in [
        "5M",
        "15M",
        "1H",
        "4H"
    ]:

        data = mtf[label]

        if not data["ready"]:

            lines.append(
                f"{label}: ⚠️ ไม่พอ"
            )

            continue

        rsi = data["rsi"]

        lines.append(
            f"{trend_icon(data['trend'])} "
            f"{label}: **{data['trend']}** "
            f"| RSI {rsi:.1f}"
        )

    lines.extend([
        "",
        "⚠️ **ระบบแจ้งเตือน ไม่ใช่คำสั่งซื้อขาย**",
        "ควรตรวจแนวรับ/แนวต้าน, ข่าว และ Risk Management ก่อนเทรด"
    ])

    return "\n".join(lines)


# ============================================================
# CHART
# ============================================================

def make_chart(points):

    if len(points) < 2:

        return None

    recent = points[-360:]

    times = [
        p["time"]
        for p in recent
    ]

    prices = [
        p["price"]
        for p in recent
    ]

    ema9_values = []

    ema21_values = []

    for i in range(len(prices)):

        subset = prices[:i + 1]

        ema9_values.append(
            calculate_ema(
                subset,
                EMA_FAST
            )
        )

        ema21_values.append(
            calculate_ema(
                subset,
                EMA_MID
            )
        )

    fig, ax = plt.subplots(
        figsize=(12, 5),
        dpi=140
    )

    ax.plot(
        times,
        prices,
        linewidth=2,
        label="XAU/USD"
    )

    valid9 = [
        x is not None
        for x in ema9_values
    ]

    if any(valid9):

        ax.plot(
            times,
            [
                x if x is not None else float("nan")
                for x in ema9_values
            ],
            linewidth=1.2,
            label="EMA 9"
        )

    valid21 = [
        x is not None
        for x in ema21_values
    ]

    if any(valid21):

        ax.plot(
            times,
            [
                x if x is not None else float("nan")
                for x in ema21_values
            ],
            linewidth=1.2,
            label="EMA 21"
        )

    ax.set_title(
        "XAU/USD - Intraday"
    )

    ax.set_ylabel(
        "USD / Troy Ounce"
    )

    ax.grid(
        True,
        alpha=0.25
    )

    ax.legend()

    ax.xaxis.set_major_formatter(
        mdates.DateFormatter(
            "%d/%m %H:%M"
        )
    )

    fig.autofmt_xdate()

    fig.tight_layout()

    path = "xau_chart.png"

    fig.savefig(path)

    plt.close(fig)

    return path


# ============================================================
# FULL ANALYSIS
# ============================================================

def get_full_analysis():

    spot = get_xau_spot()

    if spot is None:

        raise RuntimeError(
            "ไม่สามารถดึง XAU/USD Spot ได้"
        )

    points = get_xau_intraday(
        hours=48
    )

    if len(points) < 30:

        raise RuntimeError(
            f"ข้อมูล Intraday ไม่พอ: {len(points)} จุด"
        )

    mtf = analyze_mtf(
        points
    )

    global_signal = build_global_signal(
        mtf
    )

    return (
        spot,
        points,
        mtf,
        global_signal
    )


# ============================================================
# BOT READY
# ============================================================

@bot.event
async def on_ready():

    print("=" * 60)
    print("DISCORD BOT READY")
    print(f"BOT: {bot.user}")
    print(f"ID: {bot.user.id}")
    print("=" * 60)

    try:

        synced = await bot.tree.sync()

        print(
            f"Slash commands synced: {len(synced)}"
        )

        for command in synced:

            print(
                f"  /{command.name}"
            )

    except Exception as e:

        print(
            "COMMAND SYNC ERROR:",
            repr(e)
        )

    if not monitor_gold.is_running():

        monitor_gold.start()


# ============================================================
# /GOLD
# ============================================================

@bot.tree.command(
    name="gold",
    description="ดูราคา XAU/USD ปัจจุบัน"
)
async def gold(
    interaction: discord.Interaction
):

    await interaction.response.defer()

    try:

        spot = await asyncio.to_thread(
            get_xau_spot
        )

        if spot is None:

            await interaction.followup.send(
                "❌ ไม่สามารถดึงราคา XAU/USD ได้"
            )

            return

        price = spot["price"]

        state = (
            spot.get("data_state")
            or {}
        )

        fx = spot.get("fx_rate")

        message = (
            "🪙 **XAU/USD LIVE PRICE**\n"
            "━━━━━━━━━━━━━━━━━━\n\n"
            f"💰 Gold Spot: **{fmt_price(price)} / oz**\n"
            f"💵 USD/THB: **{fmt_price(fx) if fx else 'N/A'}**\n\n"
            f"📡 Data: `{state.get('status', 'unknown')}`\n"
            f"🕐 `{now_thai().strftime('%d/%m/%Y %H:%M:%S')}`\n\n"
            "📌 ข้อมูล XAU/USD เป็นราคา Spot "
            "ไม่ใช่ราคาทองคำแท่งไทย"
        )

        await interaction.followup.send(
            message
        )

    except Exception as e:

        print(
            "GOLD COMMAND ERROR:",
            repr(e)
        )

        await interaction.followup.send(
            f"❌ ดึงราคาทองไม่สำเร็จ\n"
            f"`{str(e)[:500]}`"
        )


# ============================================================
# /XAU
# ============================================================

@bot.tree.command(
    name="xau",
    description="ดู XAU/USD พร้อมสถานะตลาด"
)
async def xau(
    interaction: discord.Interaction
):

    await interaction.response.defer()

    try:

        result = await asyncio.to_thread(
            get_full_analysis
        )

        spot, points, mtf, global_signal = result

        message = build_analysis_message(
            spot,
            mtf,
            global_signal
        )

        await interaction.followup.send(
            message
        )

    except Exception as e:

        print(
            "XAU COMMAND ERROR:",
            repr(e)
        )

        traceback.print_exc()

        await interaction.followup.send(
            f"❌ XAU Analysis Error\n"
            f"`{str(e)[:700]}`"
        )


# ============================================================
# /ANALYSIS
# ============================================================

@bot.tree.command(
    name="analysis",
    description="วิเคราะห์ EMA RSI MACD ATR และ MTF"
)
async def analysis(
    interaction: discord.Interaction
):

    await interaction.response.defer()

    try:

        spot, points, mtf, global_signal = (
            await asyncio.to_thread(
                get_full_analysis
            )
        )

        message = build_analysis_message(
            spot,
            mtf,
            global_signal
        )

        await interaction.followup.send(
            message
        )

    except Exception as e:

        print(
            "ANALYSIS ERROR:",
            repr(e)
        )

        await interaction.followup.send(
            f"❌ วิเคราะห์ไม่ได้\n"
            f"`{str(e)[:700]}`"
        )


# ============================================================
# /SIGNAL
# ============================================================

@bot.tree.command(
    name="signal",
    description="ดูสัญญาณภาพรวม XAU/USD"
)
async def signal(
    interaction: discord.Interaction
):

    await interaction.response.defer()

    try:

        (
            spot,
            points,
            mtf,
            global_signal
        ) = await asyncio.to_thread(
            get_full_analysis
        )

        signal_name = (
            global_signal["signal"]
        )

        confidence = (
            global_signal["confidence"]
        )

        if signal_name == "BUY_BIAS":

            title = "🟢 BUY BIAS"

        elif signal_name == "SELL_BIAS":

            title = "🔴 SELL BIAS"

        else:

            title = "🟡 NEUTRAL"

        message = (
            "🎯 **XAU/USD SIGNAL**\n"
            "━━━━━━━━━━━━━━━━━━\n\n"
            f"💰 Price: **{fmt_price(spot['price'])}**\n\n"
            f"Signal: **{title}**\n"
            f"Confidence: **{confidence}%**\n\n"
            f"🟢 Bullish score: "
            f"`{global_signal['bullish']}`\n"
            f"🔴 Bearish score: "
            f"`{global_signal['bearish']}`\n\n"
            "MTF:\n"
        )

        for label in [
            "5M",
            "15M",
            "1H",
            "4H"
        ]:

            data = mtf[label]

            if data["ready"]:

                message += (
                    f"{trend_icon(data['trend'])} "
                    f"{label}: **{data['trend']}**\n"
                )

            else:

                message += (
                    f"⚠️ {label}: DATA NOT READY\n"
                )

        message += (
            "\n⚠️ Signal เป็น Bias "
            "ไม่ใช่คำสั่ง Buy/Sell อัตโนมัติ"
        )

        await interaction.followup.send(
            message
        )

    except Exception as e:

        print(
            "SIGNAL ERROR:",
            repr(e)
        )

        await interaction.followup.send(
            f"❌ Signal Error\n"
            f"`{str(e)[:700]}`"
        )


# ============================================================
# /TREND
# ============================================================

@bot.tree.command(
    name="trend",
    description="ดูแนวโน้ม XAU/USD หลาย Timeframe"
)
async def trend(
    interaction: discord.Interaction
):

    await interaction.response.defer()

    try:

        (
            spot,
            points,
            mtf,
            global_signal
        ) = await asyncio.to_thread(
            get_full_analysis
        )

        lines = [
            "📊 **XAU/USD TREND**",
            "━━━━━━━━━━━━━━━━━━",
            "",
            f"💰 Price: **{fmt_price(spot['price'])}**",
            ""
        ]

        for label in [
            "5M",
            "15M",
            "1H",
            "4H"
        ]:

            data = mtf[label]

            if not data["ready"]:

                lines.append(
                    f"⚠️ {label}: ข้อมูลยังไม่พอ"
                )

            else:

                lines.append(
                    f"{trend_icon(data['trend'])} "
                    f"**{label}: {data['trend']}**"
                )

        lines.extend([
            "",
            f"🎯 Overall: "
            f"**{signal_text(global_signal['signal'])}**",
            f"📊 Confidence: "
            f"**{global_signal['confidence']}%**"
        ])

        await interaction.followup.send(
            "\n".join(lines)
        )

    except Exception as e:

        print(
            "TREND ERROR:",
            repr(e)
        )

        await interaction.followup.send(
            f"❌ Trend Error\n"
            f"`{str(e)[:700]}`"
        )


# ============================================================
# /CHART
# ============================================================

@bot.tree.command(
    name="chart",
    description="ดูกราฟ XAU/USD พร้อม EMA"
)
async def chart(
    interaction: discord.Interaction
):

    await interaction.response.defer()

    try:

        points = await asyncio.to_thread(
            get_xau_intraday,
            48
        )

        path = await asyncio.to_thread(
            make_chart,
            points
        )

        if path is None:

            await interaction.followup.send(
                "⚠️ ข้อมูลกราฟยังไม่พอ"
            )

            return

        await interaction.followup.send(
            file=discord.File(path)
        )

        try:
            os.remove(path)
        except Exception:
            pass

    except Exception as e:

        print(
            "CHART ERROR:",
            repr(e)
        )

        await interaction.followup.send(
            f"❌ สร้างกราฟไม่ได้\n"
            f"`{str(e)[:700]}`"
        )


# ============================================================
# /STATUS
# ============================================================

@bot.tree.command(
    name="status",
    description="ตรวจสอบสถานะบอท"
)
async def status(
    interaction: discord.Interaction
):

    uptime = (
        now_thai()
        - bot_start_time
    )

    hours = int(
        uptime.total_seconds() // 3600
    )

    minutes = int(
        (uptime.total_seconds() % 3600)
        // 60
    )

    await interaction.response.send_message(
        "🤖 **BOT STATUS**\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        "🟢 Discord: ONLINE\n"
        "🟢 Render: RUNNING\n"
        "🟢 XAU API: READY\n"
        f"⏱️ Uptime: "
        f"`{hours}h {minutes}m`\n\n"
        f"⏰ ตรวจราคา: ทุก "
        f"`{CHECK_INTERVAL_MINUTES}` นาที"
    )


# ============================================================
# /HELP
# ============================================================

@bot.tree.command(
    name="help",
    description="ดูคำสั่งทั้งหมดของ Gold Bot"
)
async def help_command(
    interaction: discord.Interaction
):

    message = (
        "🪙 **GOLD BOT COMMANDS**\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        "`/gold`\n"
        "ดู XAU/USD ปัจจุบัน\n\n"
        "`/xau`\n"
        "วิเคราะห์เต็ม EMA RSI MACD ATR MTF\n\n"
        "`/analysis`\n"
        "วิเคราะห์ตลาดแบบละเอียด\n\n"
        "`/signal`\n"
        "ดู BUY / SELL BIAS\n\n"
        "`/trend`\n"
        "ดูแนวโน้ม 5M / 15M / 1H / 4H\n\n"
        "`/chart`\n"
        "กราฟ XAU/USD + EMA\n\n"
        "`/status`\n"
        "ตรวจสถานะบอท\n\n"
        "`/help`\n"
        "ดูคำสั่งทั้งหมด\n\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "📡 XAU/USD: 24H ช่วงวันทำการ\n"
        "🧠 Technical: EMA / RSI / MACD / ATR\n"
        "📊 MTF: 5M / 15M / 1H / 4H\n"
        "🔔 Signal Alert: เปิดใช้งาน"
    )

    await interaction.response.send_message(
        message
    )


# ============================================================
# MONITOR
# ============================================================

@tasks.loop(
    minutes=CHECK_INTERVAL_MINUTES
)
async def monitor_gold():

    global last_price
    global last_alert_time
    global last_alert_signal

    try:

        print(
            f"[{now_thai().strftime('%Y-%m-%d %H:%M:%S')}] "
            "Checking XAU/USD..."
        )

        result = await asyncio.to_thread(
            get_full_analysis
        )

        (
            spot,
            points,
            mtf,
            global_signal
        ) = result

        price = spot["price"]

        append_local_history(
            price
        )

        print(
            f"XAU/USD = {price:.2f} | "
            f"Signal = {global_signal['signal']} | "
            f"Confidence = {global_signal['confidence']}%"
        )

        if last_price is not None:

            movement = (
                price - last_price
            )

            if abs(movement) >= 1:

                print(
                    f"Price movement: "
                    f"{movement:+.2f}"
                )

        last_price = price

        signal = (
            global_signal["signal"]
        )

        confidence = (
            global_signal["confidence"]
        )

        if should_alert(
            signal,
            confidence
        ):

            channel = None

            if ALERT_CHANNEL_ID:

                channel = bot.get_channel(
                    ALERT_CHANNEL_ID
                )

                if channel is None:

                    try:

                        channel = await bot.fetch_channel(
                            ALERT_CHANNEL_ID
                        )

                    except Exception as e:

                        print(
                            "ALERT CHANNEL ERROR:",
                            e
                        )

            if channel:

                message = build_alert_message(
                    spot,
                    mtf,
                    global_signal
                )

                await channel.send(
                    message
                )

                last_alert_time = now_thai()

                last_alert_signal = signal

                print(
                    "SIGNAL ALERT SENT"
                )

    except Exception as e:

        print(
            "MONITOR ERROR:",
            repr(e)
        )

        traceback.print_exc()


# ============================================================
# LOOP ERROR HANDLING
# ============================================================

@monitor_gold.before_loop
async def before_monitor():

    await bot.wait_until_ready()

    print(
        "XAU monitor is ready."
    )


# ============================================================
# MAIN
# ============================================================

async def main():

    print("=" * 60)
    print("STARTING GOLD DISCORD BOT")
    print("=" * 60)

    if not DISCORD_TOKEN:

        raise RuntimeError(
            "DISCORD_TOKEN is not configured"
        )

    await start_web_server()

    print(
        "Starting Discord connection..."
    )

    await bot.start(
        DISCORD_TOKEN
    )


if __name__ == "__main__":

    try:

        asyncio.run(
            main()
        )

    except KeyboardInterrupt:

        print(
            "Bot stopped."
        )

    except Exception as e:

        print(
            "FATAL ERROR:",
            repr(e)
        )

        traceback.print_exc()
