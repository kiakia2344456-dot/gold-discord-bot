import os
import re
import json
import asyncio
import requests
import math
from html import unescape
from datetime import datetime, timedelta, timezone

import discord
from discord.ext import commands, tasks
from discord import app_commands

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates

from aiohttp import web


# =========================================================
# CONFIG
# =========================================================

TZ = timezone(timedelta(hours=7))

HISTORY_FILE = "gold_history.json"
HISTORY_KEEP_DAYS = 45

# Discord Alert Channel
ALERT_CHANNEL_ID = int(
    os.getenv("ALERT_CHANNEL_ID", "1538158164522827888")
)

# แจ้งเตือนเมื่อราคาทอง Spot เปลี่ยนจากราคาที่แจ้งครั้งล่าสุด
ALERT_THRESHOLD = float(
    os.getenv("ALERT_THRESHOLD", "3.0")
)

# วิเคราะห์ทุกกี่นาที
CHECK_INTERVAL_MINUTES = 1

# สัญลักษณ์ Yahoo Finance
XAU_SYMBOL = "XAUUSD=X"
USDTHB_SYMBOL = "USDTHB=X"

# Timeframes สำหรับการวิเคราะห์
TIMEFRAMES = {
    "5m": {
        "interval": "5m",
        "range": "5d"
    },
    "15m": {
        "interval": "15m",
        "range": "5d"
    },
    "1h": {
        "interval": "1h",
        "range": "30d"
    },
    "4h": {
        "interval": "1h",
        "range": "90d"
    },
    "1d": {
        "interval": "1d",
        "range": "1y"
    }
}


# =========================================================
# GLOBAL
# =========================================================

last_alert_price = None
last_trend_alert = None
last_breakout_state = None


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
# WEB SERVER
# =========================================================

async def handle_health_check(request):
    return web.Response(
        text="Gold Trading Bot is alive!",
        status=200
    )


async def start_web_server():

    app = web.Application()

    app.router.add_get("/", handle_health_check)
    app.router.add_get("/health", handle_health_check)

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
        f"Web server active on port {port}"
    )


# =========================================================
# HTTP SESSION
# =========================================================

HEADERS = {
    "User-Agent":
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 "
        "(KHTML, like Gecko) "
        "Chrome/120.0 Safari/537.36"
}


def yahoo_chart_url(
    symbol,
    interval="5m",
    range_value="5d"
):

    return (
        "https://query1.finance.yahoo.com/v8/finance/chart/"
        f"{symbol}"
        f"?range={range_value}"
        f"&interval={interval}"
        "&includePrePost=true"
        "&events=div%2Csplits"
    )


# =========================================================
# FETCH YAHOO FINANCE
# =========================================================

def fetch_yahoo_data(
    symbol,
    interval="5m",
    range_value="5d"
):

    url = yahoo_chart_url(
        symbol,
        interval,
        range_value
    )

    response = requests.get(
        url,
        headers=HEADERS,
        timeout=15
    )

    response.raise_for_status()

    data = response.json()

    result = data["chart"]["result"]

    if not result:
        raise ValueError(
            f"No Yahoo data for {symbol}"
        )

    result = result[0]

    timestamps = result.get(
        "timestamp",
        []
    )

    indicators = result[
        "indicators"
    ]["quote"][0]

    opens = indicators.get(
        "open",
        []
    )

    highs = indicators.get(
        "high",
        []
    )

    lows = indicators.get(
        "low",
        []
    )

    closes = indicators.get(
        "close",
        []
    )

    volumes = indicators.get(
        "volume",
        []
    )

    rows = []

    for i, timestamp in enumerate(
        timestamps
    ):

        try:

            close = closes[i]

            if close is None:
                continue

            rows.append({
                "timestamp": datetime.fromtimestamp(
                    timestamp,
                    timezone.utc
                ).astimezone(TZ).isoformat(),

                "open": (
                    float(opens[i])
                    if opens[i] is not None
                    else float(close)
                ),

                "high": (
                    float(highs[i])
                    if highs[i] is not None
                    else float(close)
                ),

                "low": (
                    float(lows[i])
                    if lows[i] is not None
                    else float(close)
                ),

                "close": float(close),

                "volume": (
                    float(volumes[i])
                    if i < len(volumes)
                    and volumes[i] is not None
                    else 0
                )
            })

        except Exception:
            continue

    if not rows:
        raise ValueError(
            f"No usable data for {symbol}"
        )

    return rows


# =========================================================
# GET CURRENT GOLD
# =========================================================

def get_gold_market():

    data = fetch_yahoo_data(
        XAU_SYMBOL,
        "5m",
        "5d"
    )

    latest = data[-1]

    return {
        "price": latest["close"],
        "open": latest["open"],
        "high": latest["high"],
        "low": latest["low"],
        "timestamp": latest["timestamp"]
    }


def get_usd_thb():

    try:

        data = fetch_yahoo_data(
            USDTHB_SYMBOL,
            "5m",
            "5d"
        )

        return data[-1]["close"]

    except Exception as e:

        print(
            "USDTHB ERROR:",
            e
        )

        return None


# =========================================================
# FORMAT
# =========================================================

def format_gold_price(value):

    if value is None:
        return "ไม่พบข้อมูล"

    return f"${value:,.2f} / oz"


def format_usd_thb(value):

    if value is None:
        return "ไม่พบข้อมูล"

    return f"฿{value:,.2f}"


# =========================================================
# HISTORY
# =========================================================

def load_history():

    if not os.path.exists(
        HISTORY_FILE
    ):
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
            "LOAD HISTORY ERROR:",
            e
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
                ensure_ascii=False
            )

    except Exception as e:

        print(
            "SAVE HISTORY ERROR:",
            e
        )


def append_history(
    market_data,
    usd_thb
):

    history = load_history()

    now = datetime.now(TZ)

    history.append({

        "ts": now.isoformat(),

        "gold_spot":
            market_data["price"],

        "open":
            market_data["open"],

        "high":
            market_data["high"],

        "low":
            market_data["low"],

        "usd_thb":
            usd_thb
    })

    cutoff = (
        now -
        timedelta(
            days=HISTORY_KEEP_DAYS
        )
    )

    cleaned = []

    for h in history:

        try:

            dt = datetime.fromisoformat(
                h["ts"]
            )

            if dt >= cutoff:
                cleaned.append(h)

        except Exception:
            pass

    save_history(cleaned)

    return cleaned


# =========================================================
# MATH
# =========================================================

def sma(values, period):

    if len(values) < period:
        return None

    return sum(
        values[-period:]
    ) / period


def ema(values, period):

    if len(values) < period:
        return None

    multiplier = 2 / (
        period + 1
    )

    result = sum(
        values[:period]
    ) / period

    for price in values[period:]:

        result = (
            (price - result)
            * multiplier
        ) + result

    return result


def calculate_rsi(
    closes,
    period=14
):

    if len(closes) <= period:
        return None

    gains = []
    losses = []

    for i in range(
        1,
        len(closes)
    ):

        change = (
            closes[i] -
            closes[i - 1]
        )

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


def calculate_atr(
    rows,
    period=14
):

    if len(rows) <= period:
        return None

    true_ranges = []

    for i in range(
        1,
        len(rows)
    ):

        high = rows[i]["high"]
        low = rows[i]["low"]
        prev_close = rows[i - 1]["close"]

        tr = max(
            high - low,
            abs(high - prev_close),
            abs(low - prev_close)
        )

        true_ranges.append(tr)

    return sma(
        true_ranges,
        period
    )


def calculate_macd(
    closes
):

    if len(closes) < 35:
        return None

    ema12_values = []

    ema26_values = []

    for i in range(
        len(closes)
    ):

        subset = closes[:i + 1]

        e12 = ema(
            subset,
            12
        )

        e26 = ema(
            subset,
            26
        )

        if e12 is not None:
            ema12_values.append(e12)

        if e26 is not None:
            ema26_values.append(e26)

    macd_values = []

    for i in range(
        len(closes)
    ):

        e12 = ema(
            closes[:i + 1],
            12
        )

        e26 = ema(
            closes[:i + 1],
            26
        )

        if e12 is not None and e26 is not None:

            macd_values.append(
                e12 - e26
            )

    if len(macd_values) < 9:
        return None

    signal = ema(
        macd_values,
        9
    )

    if signal is None:
        return None

    macd = macd_values[-1]

    histogram = (
        macd - signal
    )

    return {
        "macd": macd,
        "signal": signal,
        "histogram": histogram
    }


# =========================================================
# SUPPORT / RESISTANCE
# =========================================================

def support_resistance(
    rows,
    lookback=50
):

    rows = rows[-lookback:]

    if len(rows) < 10:
        return None, None

    resistance = max(
        r["high"]
        for r in rows
    )

    support = min(
        r["low"]
        for r in rows
    )

    return support, resistance


# =========================================================
# CANDLE PATTERNS
# =========================================================

def detect_candle_pattern(
    rows
):

    if len(rows) < 3:
        return "ไม่มีข้อมูล"

    a = rows[-3]
    b = rows[-2]
    c = rows[-1]

    body = abs(
        c["close"] -
        c["open"]
    )

    candle_range = (
        c["high"] -
        c["low"]
    )

    if candle_range <= 0:
        return "ปกติ"

    upper_wick = (
        c["high"] -
        max(
            c["open"],
            c["close"]
        )
    )

    lower_wick = (
        min(
            c["open"],
            c["close"]
        ) -
        c["low"]
    )

    # Doji
    if body <= candle_range * 0.1:

        return "Doji — ตลาดลังเล"

    # Hammer
    if (
        lower_wick >= body * 2
        and upper_wick <= body
    ):

        return "Hammer — มีแรงซื้อกลับ"

    # Shooting Star
    if (
        upper_wick >= body * 2
        and lower_wick <= body
    ):

        return "Shooting Star — มีแรงขายกด"

    # Bullish engulfing
    if (
        b["close"] < b["open"]
        and c["close"] > c["open"]
        and c["close"] >= b["open"]
        and c["open"] <= b["close"]
    ):

        return "Bullish Engulfing — สัญญาณซื้อ"

    # Bearish engulfing
    if (
        b["close"] > b["open"]
        and c["close"] < c["open"]
        and c["open"] >= b["close"]
        and c["close"] <= b["open"]
    ):

        return "Bearish Engulfing — สัญญาณขาย"

    return "ปกติ"


# =========================================================
# MARKET ANALYSIS
# =========================================================

def analyze_market(
    rows
):

    if len(rows) < 50:
        return None

    closes = [
        r["close"]
        for r in rows
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

    rsi = calculate_rsi(
        closes,
        14
    )

    macd = calculate_macd(
        closes
    )

    atr = calculate_atr(
        rows,
        14
    )

    support, resistance = (
        support_resistance(
            rows,
            50
        )
    )

    pattern = detect_candle_pattern(
        rows
    )

    score = 0
    reasons = []

    # EMA20
    if ema20:

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

    # EMA50
    if ema50:

        if current > ema50:

            score += 2

            reasons.append(
                "ราคาอยู่เหนือ EMA50"
            )

        else:

            score -= 2

            reasons.append(
                "ราคาอยู่ต่ำกว่า EMA50"
            )

    # EMA200
    if ema200:

        if current > ema200:

            score += 3

            reasons.append(
                "ราคาอยู่เหนือ EMA200"
            )

        else:

            score -= 3

            reasons.append(
                "ราคาอยู่ต่ำกว่า EMA200"
            )

    # RSI
    if rsi is not None:

        if rsi >= 70:

            reasons.append(
                "RSI สูง — ระวัง Overbought"
            )

        elif rsi <= 30:

            reasons.append(
                "RSI ต่ำ — ระวัง Oversold"
            )

        elif rsi > 50:

            score += 1

            reasons.append(
                "RSI > 50"
            )

        else:

            score -= 1

            reasons.append(
                "RSI < 50"
            )

    # MACD
    if macd:

        if macd["histogram"] > 0:

            score += 2

            reasons.append(
                "MACD เป็นบวก"
            )

        else:

            score -= 2

            reasons.append(
                "MACD เป็นลบ"
            )

    # Trend
    if score >= 5:

        trend = "ขาขึ้นแรง"

    elif score >= 2:

        trend = "ขาขึ้น"

    elif score <= -5:

        trend = "ขาลงแรง"

    elif score <= -2:

        trend = "ขาลง"

    else:

        trend = "Sideway / รอความชัดเจน"

    # Signal
    if (
        score >= 6
        and rsi is not None
        and rsi < 70
    ):

        signal = "🟢 BUY BIAS"

    elif (
        score <= -6
        and rsi is not None
        and rsi > 30
    ):

        signal = "🔴 SELL BIAS"

    else:

        signal = "🟡 WAIT"

    return {

        "price":
            current,

        "ema20":
            ema20,

        "ema50":
            ema50,

        "ema200":
            ema200,

        "rsi":
            rsi,

        "macd":
            macd,

        "atr":
            atr,

        "support":
            support,

        "resistance":
            resistance,

        "pattern":
            pattern,

        "score":
            score,

        "trend":
            trend,

        "signal":
            signal,

        "reasons":
            reasons
    }


# =========================================================
# MULTI TIMEFRAME
# =========================================================

def get_multi_timeframe():

    result = {}

    for tf, config in TIMEFRAMES.items():

        try:

            rows = fetch_yahoo_data(
                XAU_SYMBOL,
                config["interval"],
                config["range"]
            )

            analysis = analyze_market(
                rows
            )

            if analysis:

                result[tf] = analysis

        except Exception as e:

            print(
                f"{tf} ERROR:",
                e
            )

    return result


# =========================================================
# TREND MESSAGE
# =========================================================

def trend_emoji(trend):

    if "ขาขึ้นแรง" in trend:
        return "🚀"

    if "ขาขึ้น" in trend:
        return "📈"

    if "ขาลงแรง" in trend:
        return "🔻"

    if "ขาลง" in trend:
        return "📉"

    return "⏸️"


def format_analysis(
    analysis,
    timeframe="5m"
):

    if not analysis:

        return "⚠️ ข้อมูลไม่เพียงพอ"

    rsi_text = (
        f"{analysis['rsi']:.2f}"
        if analysis["rsi"] is not None
        else "-"
    )

    macd_text = "-"

    if analysis["macd"]:

        macd_text = (
            f"{analysis['macd']['macd']:.3f}"
        )

    ema20_text = (
        f"${analysis['ema20']:,.2f}"
        if analysis["ema20"]
        else "-"
    )

    ema50_text = (
        f"${analysis['ema50']:,.2f}"
        if analysis["ema50"]
        else "-"
    )

    ema200_text = (
        f"${analysis['ema200']:,.2f}"
        if analysis["ema200"]
        else "-"
    )

    support_text = (
        f"${analysis['support']:,.2f}"
        if analysis["support"]
        else "-"
    )

    resistance_text = (
        f"${analysis['resistance']:,.2f}"
        if analysis["resistance"]
        else "-"
    )

    return (
        f"{trend_emoji(analysis['trend'])} "
        f"**GOLD ANALYSIS — {timeframe}**\n"
        "━━━━━━━━━━━━━━━━━━\n\n"

        f"💰 XAU/USD: "
        f"**${analysis['price']:,.2f}**\n\n"

        f"📊 Trend: "
        f"**{analysis['trend']}**\n"

        f"🎯 Score: "
        f"**{analysis['score']}**\n"

        f"🧭 Signal: "
        f"**{analysis['signal']}**\n\n"

        f"📐 EMA20: **{ema20_text}**\n"
        f"📐 EMA50: **{ema50_text}**\n"
        f"📐 EMA200: **{ema200_text}**\n\n"

        f"📊 RSI14: **{rsi_text}**\n"
        f"📉 MACD: **{macd_text}**\n"
        f"🌊 ATR14: "
        f"**{analysis['atr']:.2f}**\n"
        if analysis["atr"] is not None
        else
        f"📊 RSI14: **{rsi_text}**\n"
        f"📉 MACD: **{macd_text}**\n"
        f"🌊 ATR14: **-**\n"
    ) + (

        f"\n🟢 Support: "
        f"**{support_text}**\n"

        f"🔴 Resistance: "
        f"**{resistance_text}**\n\n"

        f"🕯️ Pattern: "
        f"**{analysis['pattern']}**\n\n"

        "🔎 **เหตุผลหลัก**\n"
        +
        "\n".join(
            f"• {r}"
            for r in analysis["reasons"][:6]
        )
    )


# =========================================================
# MARKET STATUS
# =========================================================

def market_status():

    try:

        data = get_gold_market()

        usd_thb = get_usd_thb()

        return data, usd_thb

    except Exception as e:

        print(
            "MARKET STATUS ERROR:",
            e
        )

        return None, None


# =========================================================
# PRICE CHART
# =========================================================

def make_price_chart(
    rows,
    title
):

    if len(rows) < 2:
        return None

    rows = rows[-500:]

    times = []

    prices = []

    for r in rows:

        try:

            times.append(
                datetime.fromisoformat(
                    r["timestamp"]
                )
            )

            prices.append(
                r["close"]
            )

        except Exception:
            pass

    if len(prices) < 2:
        return None

    fig, ax = plt.subplots(
        figsize=(10, 5),
        dpi=140
    )

    ax.plot(
        times,
        prices,
        linewidth=2
    )

    ax.axhline(
        max(prices),
        linestyle="--",
        alpha=0.4
    )

    ax.axhline(
        min(prices),
        linestyle="--",
        alpha=0.4
    )

    ax.set_title(
        title,
        fontsize=14,
        fontweight="bold"
    )

    ax.set_ylabel(
        "XAU/USD"
    )

    ax.grid(
        True,
        alpha=0.25
    )

    ax.xaxis.set_major_formatter(
        mdates.DateFormatter(
            "%d/%m %H:%M"
        )
    )

    fig.autofmt_xdate()

    fig.tight_layout()

    path = "gold_chart.png"

    fig.savefig(path)

    plt.close(fig)

    return path


# =========================================================
# GOLD COMMAND
# =========================================================

@bot.tree.command(
    name="gold",
    description="ดูราคาทอง Spot และตลาดทอง"
)
async def gold(
    interaction: discord.Interaction
):

    await interaction.response.defer()

    try:

        market, usd_thb = (
            market_status()
        )

        if market is None:

            await interaction.followup.send(
                "❌ ไม่สามารถดึงราคาทองได้"
            )

            return

        message = (

            "🪙 **GOLD MARKET**\n"
            "━━━━━━━━━━━━━━━━━━\n\n"

            f"🌎 XAU/USD\n"
            f"💰 **${market['price']:,.2f} / oz**\n\n"

            f"📌 Open: "
            f"**${market['open']:,.2f}**\n"

            f"🔺 High: "
            f"**${market['high']:,.2f}**\n"

            f"🔻 Low: "
            f"**${market['low']:,.2f}**\n\n"

            f"💵 USD/THB: "
            f"**{format_usd_thb(usd_thb)}**\n\n"

            f"🕒 Update: "
            f"{market['timestamp']}\n\n"

            "📡 Data: Yahoo Finance\n"
            "🆓 ระบบใช้แหล่งข้อมูลฟรี"
        )

        await interaction.followup.send(
            message
        )

    except Exception as e:

        print(
            "GOLD COMMAND ERROR:",
            e
        )

        await interaction.followup.send(
            "❌ เกิดข้อผิดพลาดในการดึงข้อมูล"
        )


# =========================================================
# ANALYZE COMMAND
# =========================================================

@bot.tree.command(
    name="analyze",
    description="วิเคราะห์ทองด้วย Technical Analysis"
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

        config = TIMEFRAMES[
            timeframe.value
        ]

        rows = fetch_yahoo_data(
            XAU_SYMBOL,
            config["interval"],
            config["range"]
        )

        result = analyze_market(
            rows
        )

        message = format_analysis(
            result,
            timeframe.value
        )

        await interaction.followup.send(
            message
        )

    except Exception as e:

        print(
            "ANALYZE ERROR:",
            e
        )

        await interaction.followup.send(
            "❌ วิเคราะห์ราคาไม่สำเร็จ"
        )


# =========================================================
# MULTI TIMEFRAME COMMAND
# =========================================================

@bot.tree.command(
    name="mtf",
    description="วิเคราะห์ทองหลาย Timeframe"
)
async def mtf(
    interaction: discord.Interaction
):

    await interaction.response.defer()

    try:

        results = get_multi_timeframe()

        if not results:

            await interaction.followup.send(
                "⚠️ ไม่มีข้อมูลสำหรับวิเคราะห์"
            )

            return

        lines = [
            "🧠 **GOLD MULTI-TIMEFRAME ANALYSIS**",
            "━━━━━━━━━━━━━━━━━━",
            ""
        ]

        for tf in [
            "5m",
            "15m",
            "1h",
            "4h",
            "1d"
        ]:

            if tf not in results:
                continue

            a = results[tf]

            lines.append(
                f"{trend_emoji(a['trend'])} "
                f"**{tf}** — "
                f"{a['trend']} | "
                f"Score **{a['score']}** | "
                f"{a['signal']}"
            )

        lines.append("")
        lines.append(
            "📌 ใช้หลาย Timeframe เพื่อลดการตัดสินใจจากกราฟเดียว"
        )

        await interaction.followup.send(
            "\n".join(lines)
        )

    except Exception as e:

        print(
            "MTF ERROR:",
            e
        )

        await interaction.followup.send(
            "❌ วิเคราะห์ MTF ไม่สำเร็จ"
        )


# =========================================================
# TREND COMMAND
# =========================================================

@bot.tree.command(
    name="trend",
    description="ดูแนวโน้มทอง"
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
async def trend(
    interaction: discord.Interaction,
    timeframe: app_commands.Choice[str]
):

    await interaction.response.defer()

    try:

        config = TIMEFRAMES[
            timeframe.value
        ]

        rows = fetch_yahoo_data(
            XAU_SYMBOL,
            config["interval"],
            config["range"]
        )

        analysis = analyze_market(
            rows
        )

        if not analysis:

            await interaction.followup.send(
                "⚠️ ข้อมูลไม่เพียงพอ"
            )

            return

        message = (

            f"{trend_emoji(analysis['trend'])} "
            f"**GOLD TREND — {timeframe.value}**\n"
            "━━━━━━━━━━━━━━━━━━\n\n"

            f"💰 ราคา: "
            f"**${analysis['price']:,.2f}**\n\n"

            f"📈 แนวโน้ม: "
            f"**{analysis['trend']}**\n"

            f"🎯 Score: "
            f"**{analysis['score']}**\n"

            f"🧭 Signal: "
            f"**{analysis['signal']}**\n\n"

            f"🟢 Support: "
            f"**${analysis['support']:,.2f}**\n"

            f"🔴 Resistance: "
            f"**${analysis['resistance']:,.2f}**\n\n"

            f"📊 RSI: "
            f"**{analysis['rsi']:.2f}**\n"
            if analysis["rsi"] is not None
            else
            f"📊 RSI: **-**\n"
        )

        message += (
            f"🕯️ Pattern: "
            f"**{analysis['pattern']}**\n\n"

            "📌 ระบบนี้เป็น Technical Analysis "
            "ไม่ใช่การรับประกันผลกำไร"
        )

        await interaction.followup.send(
            message
        )

    except Exception as e:

        print(
            "TREND ERROR:",
            e
        )

        await interaction.followup.send(
            "❌ วิเคราะห์ Trend ไม่สำเร็จ"
        )


# =========================================================
# MA COMMAND
# =========================================================

@bot.tree.command(
    name="ma",
    description="ดู EMA20 EMA50 EMA200"
)
async def ma(
    interaction: discord.Interaction
):

    await interaction.response.defer()

    try:

        rows = fetch_yahoo_data(
            XAU_SYMBOL,
            "1h",
            "90d"
        )

        closes = [
            r["close"]
            for r in rows
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

        message = (

            "📐 **GOLD MOVING AVERAGE**\n"
            "━━━━━━━━━━━━━━━━━━\n\n"

            f"💰 ราคา: "
            f"**${current:,.2f}**\n\n"

            f"EMA20: "
            f"**${ema20:,.2f}**\n"

            f"EMA50: "
            f"**${ema50:,.2f}**\n"

            f"EMA200: "
            f"**${ema200:,.2f}**\n\n"
        )

        if (
            ema20
            and ema50
            and ema200
        ):

            if (
                ema20 >
                ema50 >
                ema200
            ):

                message += (
                    "🚀 **Bullish Structure**\n"
                    "EMA20 > EMA50 > EMA200"
                )

            elif (
                ema20 <
                ema50 <
                ema200
            ):

                message += (
                    "🔻 **Bearish Structure**\n"
                    "EMA20 < EMA50 < EMA200"
                )

            else:

                message += (
                    "⏸️ **Mixed Structure**"
                )

        await interaction.followup.send(
            message
        )

    except Exception as e:

        print(
            "MA ERROR:",
            e
        )

        await interaction.followup.send(
            "❌ ไม่สามารถคำนวณ MA ได้"
        )


# =========================================================
# CHART COMMAND
# =========================================================

@bot.tree.command(
    name="chart",
    description="ดูกราฟ XAU/USD"
)
@app_commands.describe(
    timeframe="เลือกช่วงกราฟ"
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
            name="1 วัน",
            value="1d"
        )
    ]
)
async def chart(
    interaction: discord.Interaction,
    timeframe: app_commands.Choice[str]
):

    await interaction.response.defer()

    try:

        config = TIMEFRAMES[
            timeframe.value
        ]

        rows = fetch_yahoo_data(
            XAU_SYMBOL,
            config["interval"],
            config["range"]
        )

        path = make_price_chart(
            rows,
            f"XAU/USD — {timeframe.value}"
        )

        if not path:

            await interaction.followup.send(
                "⚠️ สร้างกราฟไม่ได้"
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
            e
        )

        await interaction.followup.send(
            "❌ สร้างกราฟไม่สำเร็จ"
        )


# =========================================================
# AUTOMATIC ALERT
# =========================================================

@tasks.loop(
    minutes=CHECK_INTERVAL_MINUTES
)
async def check_gold():

    global last_alert_price
    global last_trend_alert
    global last_breakout_state

    try:

        market = get_gold_market()

        price = market["price"]

        usd_thb = get_usd_thb()

        print(
            datetime.now(TZ).strftime(
                "%Y-%m-%d %H:%M:%S"
            ),
            "XAU/USD:",
            price
        )

        append_history(
            market,
            usd_thb
        )

        # =================================================
        # PRICE ALERT
        # =================================================

        if last_alert_price is None:

            last_alert_price = price

        else:

            difference = (
                price -
                last_alert_price
            )

            if (
                abs(difference)
                >= ALERT_THRESHOLD
            ):

                icon = (
                    "📈"
                    if difference > 0
                    else "📉"
                )

                direction = (
                    "เพิ่มขึ้น"
                    if difference > 0
                    else "ลดลง"
                )

                message = (

                    "🔔 **GOLD PRICE ALERT**\n"
                    "━━━━━━━━━━━━━━━━━━\n\n"

                    f"{icon} XAU/USD "
                    f"**{direction}**\n\n"

                    f"💰 ก่อนหน้า: "
                    f"**${last_alert_price:,.2f}**\n"

                    f"💰 ปัจจุบัน: "
                    f"**${price:,.2f}**\n"

                    f"📊 เปลี่ยนแปลง: "
                    f"**{difference:+,.2f}**\n\n"

                    f"💵 USD/THB: "
                    f"**{format_usd_thb(usd_thb)}**"
                )

                await send_alert(
                    message
                )

                last_alert_price = price

        # =================================================
        # TECHNICAL ANALYSIS
        # =================================================

        try:

            config = TIMEFRAMES["15m"]

            rows = fetch_yahoo_data(
                XAU_SYMBOL,
                config["interval"],
                config["range"]
            )

            analysis = analyze_market(
                rows
            )

            if analysis:

                trend = analysis[
                    "trend"
                ]

                signal = analysis[
                    "signal"
                ]

                trend_key = (
                    f"{trend}|{signal}"
                )

                if (
                    last_trend_alert
                    != trend_key
                ):

                    # แจ้งเฉพาะเมื่อเกิด signal ชัด
                    if (
                        "BUY" in signal
                        or "SELL" in signal
                    ):

                        message = (

                            "🧠 **GOLD TECHNICAL ALERT**\n"
                            "━━━━━━━━━━━━━━━━━━\n\n"

                            f"💰 XAU/USD: "
                            f"**${price:,.2f}**\n\n"

                            f"{trend_emoji(trend)} "
                            f"Trend: **{trend}**\n"

                            f"🎯 Score: "
                            f"**{analysis['score']}**\n"

                            f"🧭 Signal: "
                            f"**{signal}**\n\n"

                            f"📊 RSI: "
                            f"**{analysis['rsi']:.2f}**\n"
                            if analysis["rsi"]
                            is not None
                            else
                            "📊 RSI: **-**\n"
                        )

                        message += (

                            f"📐 EMA20: "
                            f"**${analysis['ema20']:,.2f}**\n"

                            f"📐 EMA50: "
                            f"**${analysis['ema50']:,.2f}**\n"

                            f"📐 EMA200: "
                            f"**${analysis['ema200']:,.2f}**\n\n"

                            f"🟢 Support: "
                            f"**${analysis['support']:,.2f}**\n"

                            f"🔴 Resistance: "
                            f"**${analysis['resistance']:,.2f}**\n\n"

                            f"🕯️ Pattern: "
                            f"**{analysis['pattern']}**\n\n"

                            "⚠️ เป็นสัญญาณจาก Technical Analysis "
                            "ไม่ใช่คำสั่งซื้อขาย"
                        )

                        await send_alert(
                            message
                        )

                    last_trend_alert = trend_key

        except Exception as e:

            print(
                "TECHNICAL ALERT ERROR:",
                e
            )

    except Exception as e:

        print(
            "CHECK GOLD ERROR:",
            e
        )


# =========================================================
# SEND ALERT
# =========================================================

async def send_alert(
    message
):

    if ALERT_CHANNEL_ID == 0:
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
                "FETCH CHANNEL ERROR:",
                e
            )

            return

    try:

        await channel.send(
            message
        )

        print(
            "ส่ง Alert สำเร็จ"
        )

    except Exception as e:

        print(
            "SEND ALERT ERROR:",
            e
        )


# =========================================================
# BOT READY
# =========================================================

@bot.event
async def on_ready():

    try:

        await bot.tree.sync()

    except Exception as e:

        print(
            "SYNC ERROR:",
            e
        )

    print(
        "=============================="
    )

    print(
        "GOLD TRADING BOT ONLINE"
    )

    print(
        "Bot:",
        bot.user
    )

    print(
        "=============================="
    )

    if not check_gold.is_running():

        check_gold.start()


# =========================================================
# ERROR HANDLING
# =========================================================

@bot.event
async def on_command_error(
    ctx,
    error
):

    print(
        "COMMAND ERROR:",
        error
    )


# =========================================================
# MAIN
# =========================================================

async def main():

    discord_token = os.getenv(
        "DISCORD_TOKEN"
    )

    if not discord_token:

        raise RuntimeError(
            "DISCORD_TOKEN is not configured"
        )

    await start_web_server()

    await bot.start(
        discord_token
    )


if __name__ == "__main__":

    asyncio.run(
        main()
    )
