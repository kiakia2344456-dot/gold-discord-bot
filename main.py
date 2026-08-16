import os
import json
import asyncio
import math
import statistics
from datetime import datetime, timedelta, timezone

import requests
import discord
from discord.ext import commands, tasks
from discord import app_commands

from aiohttp import web


# =========================================================
# DEBUG
# =========================================================

print("🔥🔥🔥 GOLD TRADING BOT - MAIN.PY STARTED 🔥🔥🔥", flush=True)


# =========================================================
# CONFIG
# =========================================================

TZ = timezone(timedelta(hours=7))

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")

ALERT_CHANNEL_ID = int(
    os.getenv("ALERT_CHANNEL_ID", "0")
)

# Yahoo Finance symbol
XAU_SYMBOL = "XAUUSD=X"

# เก็บข้อมูลในไฟล์
HISTORY_FILE = "xau_history.json"

# polling ทุก 5 นาที
CHECK_INTERVAL_MINUTES = 5

# แจ้งเตือนซ้ำอย่างน้อยกี่นาที
ALERT_COOLDOWN_MINUTES = 30

# คะแนนขั้นต่ำสำหรับ Alert
ALERT_SCORE = 6


# =========================================================
# DISCORD
# =========================================================

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(
    command_prefix="!",
    intents=intents
)


# =========================================================
# WEB SERVER FOR RENDER
# =========================================================

async def handle_health_check(request):
    return web.Response(
        text="Gold Trading Bot is alive!",
        status=200
    )


async def start_web_server():

    app = web.Application()

    app.router.add_get(
        "/",
        handle_health_check
    )

    app.router.add_get(
        "/health",
        handle_health_check
    )

    runner = web.AppRunner(app)

    await runner.setup()

    port = int(
        os.getenv("PORT", "10000")
    )

    site = web.TCPSite(
        runner,
        "0.0.0.0",
        port
    )

    await site.start()

    print(
        f"🌐 Web server active on port {port}",
        flush=True
    )


# =========================================================
# HTTP SESSION
# =========================================================

SESSION = requests.Session()

SESSION.headers.update({
    "User-Agent":
        "Mozilla/5.0 "
        "(Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 "
        "Chrome/120 Safari/537.36"
})


# =========================================================
# YAHOO FINANCE DATA
# =========================================================

def yahoo_chart(
    symbol,
    interval="5m",
    range_value="5d"
):

    url = (
        "https://query1.finance.yahoo.com/v8/finance/chart/"
        + symbol
    )

    params = {
        "interval": interval,
        "range": range_value,
        "includePrePost": "true",
        "events": "div,splits"
    }

    response = SESSION.get(
        url,
        params=params,
        timeout=20
    )

    response.raise_for_status()

    data = response.json()

    result = data.get("chart", {}).get("result")

    if not result:
        raise ValueError(
            f"Yahoo ไม่ส่งข้อมูล {symbol}"
        )

    return result[0]


def get_candles(
    interval="5m",
    range_value="5d"
):

    data = yahoo_chart(
        XAU_SYMBOL,
        interval,
        range_value
    )

    timestamps = data.get("timestamp", [])

    quote = (
        data.get("indicators", {})
        .get("quote", [{}])[0]
    )

    opens = quote.get("open", [])
    highs = quote.get("high", [])
    lows = quote.get("low", [])
    closes = quote.get("close", [])
    volumes = quote.get("volume", [])

    candles = []

    for i, ts in enumerate(timestamps):

        try:

            o = opens[i]
            h = highs[i]
            l = lows[i]
            c = closes[i]

            if (
                o is None
                or h is None
                or l is None
                or c is None
            ):
                continue

            volume = (
                volumes[i]
                if i < len(volumes)
                and volumes[i] is not None
                else 0
            )

            candles.append({
                "timestamp": int(ts),
                "datetime": datetime.fromtimestamp(
                    ts,
                    timezone.utc
                ).astimezone(TZ),

                "open": float(o),
                "high": float(h),
                "low": float(l),
                "close": float(c),
                "volume": float(volume)
            })

        except (
            IndexError,
            TypeError,
            ValueError
        ):
            continue

    return candles


# =========================================================
# CURRENT PRICE
# =========================================================

def get_current_price():

    data = yahoo_chart(
        XAU_SYMBOL,
        interval="5m",
        range_value="1d"
    )

    meta = data.get("meta", {})

    price = meta.get(
        "regularMarketPrice"
    )

    if price is None:

        candles = get_candles(
            "5m",
            "1d"
        )

        if not candles:
            raise ValueError(
                "ไม่พบราคาปัจจุบัน"
            )

        price = candles[-1]["close"]

    previous = meta.get(
        "previousClose"
    )

    return {
        "price": float(price),
        "previous": (
            float(previous)
            if previous is not None
            else None
        ),
        "currency": meta.get(
            "currency",
            "USD"
        )
    }


# =========================================================
# HISTORY
# =========================================================

def load_history():

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

        print(
            "❌ อ่าน history ไม่สำเร็จ:",
            e,
            flush=True
        )

        return []


def save_history(history):

    try:

        with open(
            HISTORY_FILE,
            "w",
            encoding="utf-8"
        ) as f:

            json.dump(
                history,
                f,
                ensure_ascii=False,
                indent=2
            )

    except Exception as e:

        print(
            "❌ บันทึก history ไม่สำเร็จ:",
            e,
            flush=True
        )


def append_price_history():

    try:

        current = get_current_price()

        history = load_history()

        now = datetime.now(TZ)

        history.append({
            "ts": now.isoformat(),
            "price": current["price"]
        })

        cutoff = now - timedelta(days=30)

        new_history = []

        for item in history:

            try:

                dt = datetime.fromisoformat(
                    item["ts"]
                )

                if dt >= cutoff:
                    new_history.append(item)

            except Exception:
                pass

        save_history(new_history)

        return current["price"]

    except Exception as e:

        print(
            "❌ HISTORY ERROR:",
            e,
            flush=True
        )

        return None


# =========================================================
# MATH HELPERS
# =========================================================

def clean(values):

    return [
        float(x)
        for x in values
        if x is not None
        and not math.isnan(float(x))
    ]


def ema(values, period):

    values = clean(values)

    if len(values) < period:
        return None

    multiplier = 2 / (period + 1)

    result = (
        sum(values[:period])
        / period
    )

    for price in values[period:]:

        result = (
            (price - result)
            * multiplier
            + result
        )

    return result


def ema_series(values, period):

    values = clean(values)

    if len(values) < period:
        return []

    multiplier = 2 / (period + 1)

    current = (
        sum(values[:period])
        / period
    )

    result = [current]

    for price in values[period:]:

        current = (
            (price - current)
            * multiplier
            + current
        )

        result.append(current)

    return result


# =========================================================
# RSI
# =========================================================

def rsi(values, period=14):

    values = clean(values)

    if len(values) < period + 1:
        return None

    gains = []
    losses = []

    for i in range(1, len(values)):

        change = (
            values[i]
            - values[i - 1]
        )

        if change >= 0:
            gains.append(change)
            losses.append(0)
        else:
            gains.append(0)
            losses.append(abs(change))

    avg_gain = (
        sum(gains[:period])
        / period
    )

    avg_loss = (
        sum(losses[:period])
        / period
    )

    for i in range(
        period,
        len(gains)
    ):

        avg_gain = (
            (
                avg_gain
                * (period - 1)
            )
            + gains[i]
        ) / period

        avg_loss = (
            (
                avg_loss
                * (period - 1)
            )
            + losses[i]
        ) / period

    if avg_loss == 0:
        return 100

    rs = avg_gain / avg_loss

    return 100 - (
        100 / (1 + rs)
    )


# =========================================================
# MACD
# =========================================================

def macd(values):

    values = clean(values)

    if len(values) < 35:
        return None

    ema12 = ema_series(
        values,
        12
    )

    ema26 = ema_series(
        values,
        26
    )

    if not ema12 or not ema26:
        return None

    # ทำให้ series อยู่ตำแหน่งเดียวกัน
    macd_values = []

    offset = len(ema12) - len(ema26)

    for i in range(
        len(ema26)
    ):

        macd_values.append(
            ema12[i + offset]
            - ema26[i]
        )

    signal_series = ema_series(
        macd_values,
        9
    )

    if not signal_series:
        return None

    macd_current = (
        macd_values[-1]
    )

    signal_current = (
        signal_series[-1]
    )

    histogram = (
        macd_current
        - signal_current
    )

    return {
        "macd": macd_current,
        "signal": signal_current,
        "histogram": histogram
    }


# =========================================================
# ATR
# =========================================================

def atr(candles, period=14):

    if len(candles) < period + 1:
        return None

    trs = []

    for i in range(
        1,
        len(candles)
    ):

        current = candles[i]
        previous = candles[i - 1]

        tr = max(
            current["high"]
            - current["low"],

            abs(
                current["high"]
                - previous["close"]
            ),

            abs(
                current["low"]
                - previous["close"]
            )
        )

        trs.append(tr)

    if len(trs) < period:
        return None

    return (
        sum(trs[-period:])
        / period
    )


# =========================================================
# SUPPORT / RESISTANCE
# =========================================================

def support_resistance(candles):

    if len(candles) < 20:
        return None, None

    recent = candles[-50:]

    highs = [
        c["high"]
        for c in recent
    ]

    lows = [
        c["low"]
        for c in recent
    ]

    resistance = max(highs)

    support = min(lows)

    return support, resistance


# =========================================================
# CANDLE PATTERN
# =========================================================

def candle_pattern(candles):

    if len(candles) < 3:
        return "ไม่มีข้อมูล"

    a = candles[-2]
    b = candles[-1]

    body_a = abs(
        a["close"]
        - a["open"]
    )

    body_b = abs(
        b["close"]
        - b["open"]
    )

    range_b = (
        b["high"]
        - b["low"]
    )

    if range_b <= 0:
        return "ไม่มี Pattern ชัดเจน"

    upper = (
        b["high"]
        - max(
            b["open"],
            b["close"]
        )
    )

    lower = (
        min(
            b["open"],
            b["close"]
        )
        - b["low"]
    )

    # Bullish engulfing
    if (
        a["close"] < a["open"]
        and b["close"] > b["open"]
        and b["open"] <= a["close"]
        and b["close"] >= a["open"]
    ):
        return "Bullish Engulfing 🟢"

    # Bearish engulfing
    if (
        a["close"] > a["open"]
        and b["close"] < b["open"]
        and b["open"] >= a["close"]
        and b["close"] <= a["open"]
    ):
        return "Bearish Engulfing 🔴"

    # Hammer
    if (
        lower > body_b * 2
        and upper < body_b
    ):
        return "Hammer 🟢"

    # Shooting star
    if (
        upper > body_b * 2
        and lower < body_b
    ):
        return "Shooting Star 🔴"

    # Doji
    if (
        body_b <= range_b * 0.1
    ):
        return "Doji ⚠️"

    return "ไม่มี Pattern เด่น"


# =========================================================
# ANALYSIS
# =========================================================

def analyze_candles(
    candles,
    timeframe
):

    if len(candles) < 50:

        return {
            "ok": False,
            "message":
                "ข้อมูลยังไม่พอสำหรับวิเคราะห์"
        }

    closes = [
        c["close"]
        for c in candles
    ]

    current = closes[-1]

    ema20 = ema(
        closes,
        20
    )

    ema50 = ema(
        closes,
        50
    )

    ema200 = ema(
        closes,
        200
    )

    rsi14 = rsi(
        closes,
        14
    )

    macd_data = macd(
        closes
    )

    atr14 = atr(
        candles,
        14
    )

    support, resistance = (
        support_resistance(
            candles
        )
    )

    pattern = candle_pattern(
        candles
    )

    score = 0
    reasons = []

    # -----------------------------------------
    # EMA
    # -----------------------------------------

    if ema20 is not None:

        if current > ema20:

            score += 1
            reasons.append(
                "ราคาอยู่เหนือ EMA20"
            )

        else:

            score -= 1
            reasons.append(
                "ราคาอยู่ต่ำกว่า EMA20"
            )

    if (
        ema20 is not None
        and ema50 is not None
    ):

        if ema20 > ema50:

            score += 2
            reasons.append(
                "EMA20 > EMA50"
            )

        else:

            score -= 2
            reasons.append(
                "EMA20 < EMA50"
            )

    if ema200 is not None:

        if current > ema200:

            score += 2
            reasons.append(
                "ราคาเหนือ EMA200"
            )

        else:

            score -= 2
            reasons.append(
                "ราคาต่ำกว่า EMA200"
            )

    # -----------------------------------------
    # RSI
    # -----------------------------------------

    if rsi14 is not None:

        if 50 <= rsi14 <= 70:

            score += 1
            reasons.append(
                "RSI สนับสนุนโมเมนตัมขาขึ้น"
            )

        elif 30 <= rsi14 < 50:

            score -= 1
            reasons.append(
                "RSI ยังไม่แข็งแรงฝั่งขึ้น"
            )

        elif rsi14 > 70:

            reasons.append(
                "RSI อยู่เขต Overbought"
            )

        elif rsi14 < 30:

            reasons.append(
                "RSI อยู่เขต Oversold"
            )

    # -----------------------------------------
    # MACD
    # -----------------------------------------

    if macd_data:

        if (
            macd_data["macd"]
            > macd_data["signal"]
        ):

            score += 2
            reasons.append(
                "MACD อยู่เหนือ Signal"
            )

        else:

            score -= 2
            reasons.append(
                "MACD อยู่ต่ำกว่า Signal"
            )

    # -----------------------------------------
    # Candle
    # -----------------------------------------

    if "Bullish" in pattern:

        score += 1

    elif (
        "Bearish" in pattern
        or "Shooting" in pattern
    ):

        score -= 1

    # -----------------------------------------
    # SIGNAL
    # -----------------------------------------

    if score >= 6:

        signal = "🟢 BUY BIAS"
        trend = "ขาขึ้น"

    elif score >= 3:

        signal = "🟢 Bullish"
        trend = "เอนเอียงขาขึ้น"

    elif score <= -6:

        signal = "🔴 SELL BIAS"
        trend = "ขาลง"

    elif score <= -3:

        signal = "🔴 Bearish"
        trend = "เอนเอียงขาลง"

    else:

        signal = "🟡 WAIT"
        trend = "Sideway / ไม่ชัดเจน"

    return {
        "ok": True,

        "timeframe": timeframe,

        "current": current,

        "ema20": ema20,
        "ema50": ema50,
        "ema200": ema200,

        "rsi": rsi14,

        "macd": macd_data,

        "atr": atr14,

        "support": support,
        "resistance": resistance,

        "pattern": pattern,

        "score": score,

        "signal": signal,

        "trend": trend,

        "reasons": reasons
    }


# =========================================================
# TIMEFRAME DATA
# =========================================================

def get_timeframe_candles(
    timeframe
):

    if timeframe == "5m":

        return get_candles(
            "5m",
            "5d"
        )

    if timeframe == "15m":

        return get_candles(
            "15m",
            "60d"
        )

    if timeframe == "1h":

        return get_candles(
            "1h",
            "730d"
        )

    if timeframe == "4h":

        # Yahoo มี 1h จึงนำมารวมเป็น 4h
        candles = get_candles(
            "1h",
            "730d"
        )

        return resample_4h(
            candles
        )

    if timeframe == "1d":

        return get_candles(
            "1d",
            "5y"
        )

    raise ValueError(
        "Timeframe ไม่ถูกต้อง"
    )


# =========================================================
# 4H RESAMPLE
# =========================================================

def resample_4h(candles):

    if not candles:
        return []

    result = []

    bucket = []

    current_bucket = None

    for candle in candles:

        dt = candle["datetime"]

        # แบ่งตามทุก 4 ชั่วโมง
        hour = (
            dt.hour // 4
        ) * 4

        bucket_key = (
            dt.date(),
            hour
        )

        if (
            current_bucket is not None
            and bucket_key
            != current_bucket
        ):

            if bucket:

                result.append({
                    "timestamp":
                        bucket[0]["timestamp"],

                    "datetime":
                        bucket[0]["datetime"],

                    "open":
                        bucket[0]["open"],

                    "high":
                        max(
                            x["high"]
                            for x in bucket
                        ),

                    "low":
                        min(
                            x["low"]
                            for x in bucket
                        ),

                    "close":
                        bucket[-1]["close"],

                    "volume":
                        sum(
                            x["volume"]
                            for x in bucket
                        )
                })

            bucket = []

        current_bucket = bucket_key

        bucket.append(candle)

    if bucket:

        result.append({
            "timestamp":
                bucket[0]["timestamp"],

            "datetime":
                bucket[0]["datetime"],

            "open":
                bucket[0]["open"],

            "high":
                max(
                    x["high"]
                    for x in bucket
                ),

            "low":
                min(
                    x["low"]
                    for x in bucket
                ),

            "close":
                bucket[-1]["close"],

            "volume":
                sum(
                    x["volume"]
                    for x in bucket
                )
        })

    return result


# =========================================================
# FORMAT ANALYSIS
# =========================================================

def fmt(value, digits=2):

    if value is None:
        return "N/A"

    return f"{value:,.{digits}f}"


def format_analysis(data):

    if not data["ok"]:
        return (
            "⚠️ "
            + data["message"]
        )

    macd_data = data["macd"]

    lines = [

        "🧠 **XAU/USD TECHNICAL ANALYSIS**",

        "━━━━━━━━━━━━━━━━━━━━",

        f"⏱️ Timeframe: **{data['timeframe']}**",

        f"💰 ราคา: **${fmt(data['current'])}**",

        "",

        f"🎯 Signal: **{data['signal']}**",

        f"📈 Trend: **{data['trend']}**",

        f"⭐ Score: **{data['score']:+d} / 10**",

        "",

        "📐 **EMA**",

        f"EMA20: **{fmt(data['ema20'])}**",

        f"EMA50: **{fmt(data['ema50'])}**",

        f"EMA200: **{fmt(data['ema200'])}**",

        "",

        "📊 **RSI**",

        f"RSI14: **{fmt(data['rsi'])}**",

        "",

        "📉 **MACD**",

        (
            f"MACD: **{fmt(macd_data['macd'])}**\n"
            f"Signal: **{fmt(macd_data['signal'])}**\n"
            f"Histogram: **{fmt(macd_data['histogram'])}**"
            if macd_data
            else "N/A"
        ),

        "",

        "🌊 **ATR**",

        f"ATR14: **{fmt(data['atr'])}**",

        "",

        "🎯 **Support / Resistance**",

        f"Support: **${fmt(data['support'])}**",

        f"Resistance: **${fmt(data['resistance'])}**",

        "",

        f"🕯️ Pattern: **{data['pattern']}**",

        "",

        "🔎 **เหตุผล**"
    ]

    for reason in data["reasons"]:

        lines.append(
            f"• {reason}"
        )

    lines.extend([

        "",

        "⚠️ **หมายเหตุ**",

        "Signal เป็นการประเมินทางเทคนิค "
        "ไม่ใช่คำสั่งซื้อขายอัตโนมัติ",

    ])

    return "\n".join(lines)


# =========================================================
# MTF ANALYSIS
# =========================================================

def get_mtf():

    timeframes = [
        "5m",
        "15m",
        "1h",
        "4h",
        "1d"
    ]

    results = []

    for tf in timeframes:

        try:

            candles = (
                get_timeframe_candles(
                    tf
                )
            )

            analysis = (
                analyze_candles(
                    candles,
                    tf
                )
            )

            results.append(
                analysis
            )

        except Exception as e:

            results.append({
                "ok": False,
                "timeframe": tf,
                "message": str(e)
            })

    return results


def format_mtf(results):

    lines = [

        "🧠 **XAU/USD MULTI-TIMEFRAME**",

        "━━━━━━━━━━━━━━━━━━━━",

        "ดูภาพใหญ่ก่อนใช้ Timeframe เล็ก",

        ""
    ]

    total = 0

    valid = 0

    for data in results:

        tf = data["timeframe"]

        if not data["ok"]:

            lines.append(
                f"⏱️ **{tf}** → ⚠️ ไม่มีข้อมูล"
            )

            continue

        valid += 1

        score = data["score"]

        total += score

        lines.append(
            f"⏱️ **{tf}** → "
            f"{data['signal']} "
            f"({score:+d})"
        )

    lines.append("")

    if valid:

        average = total / valid

        if average >= 3:

            overall = (
                "🟢 **ภาพรวมเอนเอียงขาขึ้น**"
            )

        elif average <= -3:

            overall = (
                "🔴 **ภาพรวมเอนเอียงขาลง**"
            )

        else:

            overall = (
                "🟡 **ภาพรวมยังไม่ชัดเจน**"
            )

        lines.extend([

            f"📊 Average Score: "
            f"**{average:+.2f}**",

            "",

            overall,

            "",

            "💡 แนวคิด:",

            "• 1D / 4H = ดูทิศทางใหญ่",

            "• 1H = ดูโครงสร้างกลาง",

            "• 15M / 5M = ใช้หาจังหวะ",

            "",

            "⚠️ อย่าใช้ Timeframe เดียว "
            "ตัดสินใจซื้อขาย"
        ])

    return "\n".join(lines)


# =========================================================
# ALERT
# =========================================================

last_alert_time = None
last_alert_signal = None


async def send_alert(message):

    if ALERT_CHANNEL_ID == 0:

        print(
            "⚠️ ALERT_CHANNEL_ID = 0",
            flush=True
        )

        return

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
                "❌ ไม่พบ Alert Channel:",
                e,
                flush=True
            )

            return

    await channel.send(
        message
    )


def should_alert(analysis):

    global last_alert_time
    global last_alert_signal

    if not analysis["ok"]:
        return False

    score = analysis["score"]

    if abs(score) < ALERT_SCORE:
        return False

    if score >= ALERT_SCORE:

        signal = "BUY"

    else:

        signal = "SELL"

    now = datetime.now(TZ)

    if last_alert_time is not None:

        elapsed = (
            now - last_alert_time
        ).total_seconds() / 60

        if (
            elapsed
            < ALERT_COOLDOWN_MINUTES
            and signal
            == last_alert_signal
        ):

            return False

    last_alert_time = now
    last_alert_signal = signal

    return True


async def check_market():

    try:

        current = get_current_price()

        price = current["price"]

        print(
            f"💰 XAU/USD: ${price:,.2f}",
            flush=True
        )

        append_price_history()

        # วิเคราะห์ 15m สำหรับ Alert
        candles = get_timeframe_candles(
            "15m"
        )

        analysis = analyze_candles(
            candles,
            "15m"
        )

        if should_alert(analysis):

            direction = (
                "📈 BUY BIAS"
                if analysis["score"] > 0
                else "📉 SELL BIAS"
            )

            message = (

                "🚨 **XAU/USD TRADING ALERT**\n"

                "━━━━━━━━━━━━━━━━━━━━\n\n"

                f"💰 ราคา: "
                f"**${price:,.2f}**\n\n"

                f"{direction}\n"

                f"⭐ Score: "
                f"**{analysis['score']:+d}**\n"

                f"📊 Trend: "
                f"**{analysis['trend']}**\n\n"

                f"📐 EMA20: "
                f"**{fmt(analysis['ema20'])}**\n"

                f"📐 EMA50: "
                f"**{fmt(analysis['ema50'])}**\n"

                f"📐 EMA200: "
                f"**{fmt(analysis['ema200'])}**\n\n"

                f"RSI14: "
                f"**{fmt(analysis['rsi'])}**\n"

                f"ATR14: "
                f"**{fmt(analysis['atr'])}**\n\n"

                f"🕯️ Pattern: "
                f"**{analysis['pattern']}**\n\n"

                "⚠️ เป็น Alert เชิงเทคนิค "
                "ไม่ใช่คำสั่งซื้อขาย"
            )

            await send_alert(
                message
            )

    except Exception as e:

        print(
            "❌ MARKET CHECK ERROR:",
            repr(e),
            flush=True
        )


@tasks.loop(
    minutes=CHECK_INTERVAL_MINUTES
)
async def market_loop():

    await check_market()


# =========================================================
# DISCORD READY
# =========================================================

@bot.event
async def on_ready():

    print(
        "==============================",
        flush=True
    )

    print(
        f"🤖 Discord Bot ONLINE: "
        f"{bot.user}",
        flush=True
    )

    print(
        "==============================",
        flush=True
    )

    try:

        synced = await bot.tree.sync()

        print(
            f"🔧 Slash Commands synced: "
            f"{len(synced)}",
            flush=True
        )

    except Exception as e:

        print(
            "❌ Slash Sync Error:",
            repr(e),
            flush=True
        )

    if not market_loop.is_running():

        market_loop.start()

        print(
            "🚀 Market monitoring STARTED",
            flush=True
        )


# =========================================================
# /gold
# =========================================================

@bot.tree.command(
    name="gold",
    description="ดูราคาทอง XAU/USD ปัจจุบัน"
)
async def gold(
    interaction: discord.Interaction
):

    await interaction.response.defer()

    try:

        current = get_current_price()

        price = current["price"]

        previous = current["previous"]

        if previous:

            change = (
                price - previous
            )

            pct = (
                change
                / previous
                * 100
            )

        else:

            change = None
            pct = None

        direction = (
            "📈"
            if change is not None
            and change > 0
            else
            "📉"
            if change is not None
            and change < 0
            else
            "➖"
        )

        message = (

            "🥇 **XAU/USD GOLD**\n"

            "━━━━━━━━━━━━━━━━━━━━\n\n"

            f"💰 ราคา: "
            f"**${price:,.2f} / oz**\n\n"

            f"{direction} Daily Change: "
            f"**{fmt(change)}**\n"

            f"📊 Change %: "
            f"**{fmt(pct)}%**\n\n"

            "🌎 ตลาด: **XAU/USD**\n"

            "📡 Source: Yahoo Finance\n\n"

            "⚠️ ราคาตลาดโลกมีช่วงเปิด/ปิด "
            "จึงไม่ใช่ข้อมูล 24/7 ทุกนาที"
        )

        await interaction.followup.send(
            message
        )

    except Exception as e:

        print(
            "❌ GOLD COMMAND ERROR:",
            repr(e),
            flush=True
        )

        await interaction.followup.send(
            "❌ ไม่สามารถดึงราคา XAU/USD ได้\n"
            f"Error: `{str(e)[:500]}`"
        )


# =========================================================
# /analyze
# =========================================================

@bot.tree.command(
    name="analyze",
    description="วิเคราะห์ XAU/USD ด้วย Technical Analysis"
)
@app_commands.describe(
    timeframe="เลือก Timeframe"
)
@app_commands.choices(
    timeframe=[

        app_commands.Choice(
            name="5 นาที",
            value="5m"
        ),

        app_commands.Choice(
            name="15 นาที",
            value="15m"
        ),

        app_commands.Choice(
            name="1 ชั่วโมง",
            value="1h"
        ),

        app_commands.Choice(
            name="4 ชั่วโมง",
            value="4h"
        ),

        app_commands.Choice(
            name="1 วัน",
            value="1d"
        )
    ]
)
async def analyze(
    interaction: discord.Interaction,
    timeframe: app_commands.Choice[str]
):

    await interaction.response.defer()

    try:

        candles = (
            get_timeframe_candles(
                timeframe.value
            )
        )

        result = analyze_candles(
            candles,
            timeframe.value
        )

        await interaction.followup.send(
            format_analysis(
                result
            )
        )

    except Exception as e:

        print(
            "❌ ANALYZE ERROR:",
            repr(e),
            flush=True
        )

        await interaction.followup.send(
            "❌ วิเคราะห์ไม่ได้\n"
            f"Error: `{str(e)[:500]}`"
        )


# =========================================================
# /mtf
# =========================================================

@bot.tree.command(
    name="mtf",
    description="วิเคราะห์ XAU/USD หลาย Timeframe"
)
async def mtf(
    interaction: discord.Interaction
):

    await interaction.response.defer()

    try:

        results = get_mtf()

        await interaction.followup.send(
            format_mtf(
                results
            )
        )

    except Exception as e:

        print(
            "❌ MTF ERROR:",
            repr(e),
            flush=True
        )

        await interaction.followup.send(
            "❌ MTF วิเคราะห์ไม่ได้\n"
            f"Error: `{str(e)[:500]}`"
        )


# =========================================================
# /ema
# =========================================================

@bot.tree.command(
    name="ema",
    description="ดู EMA20 EMA50 EMA200"
)
@app_commands.describe(
    timeframe="เลือก Timeframe"
)
@app_commands.choices(
    timeframe=[

        app_commands.Choice(
            name="5m",
            value="5m"
        ),

        app_commands.Choice(
            name="15m",
            value="15m"
        ),

        app_commands.Choice(
            name="1h",
            value="1h"
        ),

        app_commands.Choice(
            name="4h",
            value="4h"
        ),

        app_commands.Choice(
            name="1d",
            value="1d"
        )
    ]
)
async def ema_command(
    interaction: discord.Interaction,
    timeframe: app_commands.Choice[str]
):

    await interaction.response.defer()

    try:

        candles = (
            get_timeframe_candles(
                timeframe.value
            )
        )

        closes = [
            c["close"]
            for c in candles
        ]

        current = closes[-1]

        e20 = ema(
            closes,
            20
        )

        e50 = ema(
            closes,
            50
        )

        e200 = ema(
            closes,
            200
        )

        message = (

            "📐 **XAU/USD EMA**\n"

            "━━━━━━━━━━━━━━━━━━━━\n\n"

            f"⏱️ Timeframe: "
            f"**{timeframe.value}**\n\n"

            f"💰 Price: "
            f"**${current:,.2f}**\n\n"

            f"EMA20: **{fmt(e20)}**\n"

            f"EMA50: **{fmt(e50)}**\n"

            f"EMA200: **{fmt(e200)}**"
        )

        await interaction.followup.send(
            message
        )

    except Exception as e:

        await interaction.followup.send(
            "❌ EMA Error: "
            f"`{str(e)[:500]}`"
        )


# =========================================================
# /rsi
# =========================================================

@bot.tree.command(
    name="rsi",
    description="ดู RSI14"
)
@app_commands.describe(
    timeframe="เลือก Timeframe"
)
@app_commands.choices(
    timeframe=[

        app_commands.Choice(
            name="5m",
            value="5m"
        ),

        app_commands.Choice(
            name="15m",
            value="15m"
        ),

        app_commands.Choice(
            name="1h",
            value="1h"
        ),

        app_commands.Choice(
            name="4h",
            value="4h"
        ),

        app_commands.Choice(
            name="1d",
            value="1d"
        )
    ]
)
async def rsi_command(
    interaction: discord.Interaction,
    timeframe: app_commands.Choice[str]
):

    await interaction.response.defer()

    try:

        candles = (
            get_timeframe_candles(
                timeframe.value
            )
        )

        closes = [
            c["close"]
            for c in candles
        ]

        value = rsi(
            closes,
            14
        )

        if value is None:

            status = "ข้อมูลไม่พอ"

        elif value >= 70:

            status = "🔴 Overbought"

        elif value <= 30:

            status = "🟢 Oversold"

        else:

            status = "🟡 Neutral"

        message = (

            "📊 **XAU/USD RSI14**\n"

            "━━━━━━━━━━━━━━━━━━━━\n\n"

            f"Timeframe: "
            f"**{timeframe.value}**\n\n"

            f"RSI14: **{fmt(value)}**\n\n"

            f"สถานะ: **{status}**"
        )

        await interaction.followup.send(
            message
        )

    except Exception as e:

        await interaction.followup.send(
            "❌ RSI Error: "
            f"`{str(e)[:500]}`"
        )


# =========================================================
# /trend
# =========================================================

@bot.tree.command(
    name="trend",
    description="ดูแนวโน้ม XAU/USD"
)
@app_commands.describe(
    timeframe="เลือก Timeframe"
)
@app_commands.choices(
    timeframe=[

        app_commands.Choice(
            name="15m",
            value="15m"
        ),

        app_commands.Choice(
            name="1h",
            value="1h"
        ),

        app_commands.Choice(
            name="4h",
            value="4h"
        ),

        app_commands.Choice(
            name="1d",
            value="1d"
        )
    ]
)
async def trend_command(
    interaction: discord.Interaction,
    timeframe: app_commands.Choice[str]
):

    await interaction.response.defer()

    try:

        candles = (
            get_timeframe_candles(
                timeframe.value
            )
        )

        result = analyze_candles(
            candles,
            timeframe.value
        )

        if not result["ok"]:

            await interaction.followup.send(
                "⚠️ "
                + result["message"]
            )

            return

        message = (

            "📈 **XAU/USD TREND**\n"

            "━━━━━━━━━━━━━━━━━━━━\n\n"

            f"Timeframe: "
            f"**{timeframe.value}**\n\n"

            f"Trend: "
            f"**{result['trend']}**\n"

            f"Signal: "
            f"**{result['signal']}**\n"

            f"Score: "
            f"**{result['score']:+d}**\n\n"

            f"Price: "
            f"**${fmt(result['current'])}**\n\n"

            f"Support: "
            f"**${fmt(result['support'])}**\n"

            f"Resistance: "
            f"**${fmt(result['resistance'])}**"
        )

        await interaction.followup.send(
            message
        )

    except Exception as e:

        await interaction.followup.send(
            "❌ Trend Error: "
            f"`{str(e)[:500]}`"
        )


# =========================================================
# /levels
# =========================================================

@bot.tree.command(
    name="levels",
    description="หาแนวรับและแนวต้าน XAU/USD"
)
@app_commands.describe(
    timeframe="เลือก Timeframe"
)
@app_commands.choices(
    timeframe=[

        app_commands.Choice(
            name="15m",
            value="15m"
        ),

        app_commands.Choice(
            name="1h",
            value="1h"
        ),

        app_commands.Choice(
            name="4h",
            value="4h"
        ),

        app_commands.Choice(
            name="1d",
            value="1d"
        )
    ]
)
async def levels(
    interaction: discord.Interaction,
    timeframe: app_commands.Choice[str]
):

    await interaction.response.defer()

    try:

        candles = (
            get_timeframe_candles(
                timeframe.value
            )
        )

        support, resistance = (
            support_resistance(
                candles
            )
        )

        price = candles[-1]["close"]

        await interaction.followup.send(

            "🎯 **XAU/USD LEVELS**\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"

            f"⏱️ Timeframe: "
            f"**{timeframe.value}**\n\n"

            f"💰 Price: "
            f"**${price:,.2f}**\n\n"

            f"🟢 Support: "
            f"**${fmt(support)}**\n\n"

            f"🔴 Resistance: "
            f"**${fmt(resistance)}**"
        )

    except Exception as e:

        await interaction.followup.send(
            "❌ Levels Error: "
            f"`{str(e)[:500]}`"
        )


# =========================================================
# ERROR HANDLER
# =========================================================

@bot.event
async def on_command_error(
    ctx,
    error
):

    print(
        "COMMAND ERROR:",
        repr(error),
        flush=True
    )


# =========================================================
# MAIN
# =========================================================

async def main():

    print(
        "DEBUG 1: เข้า main()",
        flush=True
    )

    if not DISCORD_TOKEN:

        print(
            "❌ DISCORD_TOKEN NOT FOUND",
            flush=True
        )

        raise RuntimeError(
            "DISCORD_TOKEN is not configured"
        )

    print(
        "DEBUG 2: พบ DISCORD_TOKEN = True",
        flush=True
    )

    print(
        "DEBUG 3: กำลังเปิด Web Server",
        flush=True
    )

    await start_web_server()

    print(
        "DEBUG 4: กำลังเชื่อมต่อ Discord",
        flush=True
    )

    await bot.start(
        DISCORD_TOKEN
    )


# =========================================================
# RUN
# =========================================================

if __name__ == "__main__":

    try:

        asyncio.run(
            main()
        )

    except KeyboardInterrupt:

        print(
            "Bot stopped",
            flush=True
        )

    except Exception as e:

        print(
            "🔥 FATAL ERROR:",
            repr(e),
            flush=True
        )

        raise
