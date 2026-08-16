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
# GOLD DISCORD BOT V2
# ============================================================
#
# XAU/USD
# EMA
# RSI
# MACD
# ATR
# MULTI TIMEFRAME
# SUPPORT / RESISTANCE
# SWING HIGH / LOW
# BREAKOUT
# RETEST
# CANDLE PATTERNS
# FIBONACCI
# CONFLUENCE SCORE
# RISK / SL / TP
# BUY / SELL BIAS
# NO TRADE
# DISCORD ALERT
#
# FIXED:
# 1. Missing "reasons"
# 2. Missing "buy_score"
# 3. Missing "support"
#
# ============================================================


# ============================================================
# CONFIG
# ============================================================

TZ = timezone(timedelta(hours=7))

DISCORD_TOKEN = os.getenv(
    "DISCORD_TOKEN",
    ""
).strip()

try:
    ALERT_CHANNEL_ID = int(
        os.getenv(
            "ALERT_CHANNEL_ID",
            "0"
        )
    )
except Exception:
    ALERT_CHANNEL_ID = 0


CHECK_INTERVAL_MINUTES = 2

XAU_SPOT_URL = (
    "https://xaus.com/api/v1/spot"
)

XAU_INTRADAY_URL = (
    "https://xaus.com/api/v1/intraday"
)

XAU_HISTORY_URL = (
    "https://xaus.com/api/v1/history"
)

HISTORY_FILE = (
    "xau_history_v2.json"
)

SIGNAL_LOG_FILE = (
    "signal_history_v2.json"
)

HISTORY_KEEP_DAYS = 14


# ============================================================
# INDICATORS
# ============================================================

EMA_FAST = 9
EMA_MID = 21
EMA_SLOW = 50
EMA_LONG = 200

RSI_PERIOD = 14

MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9

ATR_PERIOD = 14


# ============================================================
# MARKET STRUCTURE
# ============================================================

SWING_LEFT = 2
SWING_RIGHT = 2

SR_LOOKBACK = 60

BREAKOUT_BUFFER_ATR = 0.10

RETEST_TOLERANCE_ATR = 0.30


# ============================================================
# SCORE
# ============================================================

MIN_SIGNAL_SCORE = 9

STRONG_SIGNAL_SCORE = 12


# ============================================================
# RISK
# ============================================================

SL_ATR_MULTIPLIER = 1.5

TP1_RR = 1.5

TP2_RR = 2.5


# ============================================================
# ALERT
# ============================================================

ALERT_COOLDOWN_MINUTES = 30

last_alert_time = None

last_alert_signal = None

last_price = None

bot_start_time = datetime.now(TZ)


# ============================================================
# HTTP SESSION
# ============================================================

session = requests.Session()

session.headers.update({
    "User-Agent":
        "Mozilla/5.0 "
        "(Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 "
        "Chrome/120 Safari/537.36",

    "Accept":
        "application/json"
})


# ============================================================
# RENDER HEALTH SERVER
# ============================================================

async def handle_health_check(
    request
):

    return web.json_response({
        "status": "ok",
        "service": "XAU/USD Discord Bot V2",
        "time": datetime.now(
            TZ
        ).isoformat()
    })


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

    runner = web.AppRunner(
        app
    )

    await runner.setup()

    port = int(
        os.getenv(
            "PORT",
            "10000"
        )
    )

    site = web.TCPSite(
        runner,
        "0.0.0.0",
        port
    )

    await site.start()

    print(
        "=" * 70
    )

    print(
        f"WEB SERVER ACTIVE : PORT {port}"
    )

    print(
        "=" * 70
    )


# ============================================================
# DISCORD
# ============================================================

intents = discord.Intents.default()

bot = commands.Bot(
    command_prefix="!",
    intents=intents
)


# ============================================================
# BASIC HELPERS
# ============================================================

def now_thai():

    return datetime.now(TZ)


def safe_float(
    value
):

    try:

        if value is None:
            return None

        return float(value)

    except Exception:

        return None


def fmt_price(
    value
):

    if value is None:

        return "N/A"

    return f"${value:,.2f}"


def fmt_number(
    value
):

    if value is None:

        return "N/A"

    return f"{value:,.2f}"


def clamp(
    value,
    low,
    high
):

    return max(
        low,
        min(high, value)
    )


# ============================================================
# XAU SPOT
# ============================================================

def get_xau_spot():

    try:

        cache_buster = int(
            datetime.now().timestamp()
        )

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
            data.get(
                "spot_usd_oz"
            )
        )

        if price is None:

            raise ValueError(
                f"ไม่พบ spot_usd_oz: {data}"
            )

        return {
            "price": price,

            "updated_at":
                data.get(
                    "updated_at"
                ),

            "price_as_of":
                data.get(
                    "price_as_of"
                ),

            "data_state":
                data.get(
                    "data_state"
                ) or {},

            "source":
                data.get(
                    "price_source"
                )
        }

    except Exception as e:

        print(
            "XAU SPOT ERROR:",
            repr(e)
        )

        return None


# ============================================================
# XAU INTRADAY
# ============================================================

def get_xau_intraday(
    hours=48
):

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

        raw_points = data.get(
            "points",
            []
        )

        points = []

        for item in raw_points:

            try:

                timestamp = item.get(
                    "t"
                )

                price = safe_float(
                    item.get("p")
                )

                if (
                    timestamp is None
                    or price is None
                ):
                    continue

                if isinstance(
                    timestamp,
                    (int, float)
                ):

                    dt = datetime.fromtimestamp(
                        timestamp,
                        timezone.utc
                    ).astimezone(TZ)

                else:

                    dt = datetime.fromisoformat(
                        str(
                            timestamp
                        ).replace(
                            "Z",
                            "+00:00"
                        )
                    )

                    if dt.tzinfo is None:

                        dt = dt.replace(
                            tzinfo=timezone.utc
                        )

                    dt = dt.astimezone(TZ)

                points.append({
                    "time": dt,
                    "price": price
                })

            except Exception:

                continue

        points.sort(
            key=lambda x:
            x["time"]
        )

        return points

    except Exception as e:

        print(
            "XAU INTRADAY ERROR:",
            repr(e)
        )

        return []


# ============================================================
# LOCAL HISTORY
# ============================================================

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
            "HISTORY READ ERROR:",
            repr(e)
        )

        return []


def save_history(
    history
):

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
            "HISTORY SAVE ERROR:",
            repr(e)
        )


def append_history(
    price
):

    history = load_history()

    current = now_thai()

    history.append({
        "time":
            current.isoformat(),

        "price":
            price
    })

    cutoff = (
        current
        - timedelta(
            days=HISTORY_KEEP_DAYS
        )
    )

    cleaned = []

    for item in history:

        try:

            dt = datetime.fromisoformat(
                item["time"]
            )

            if dt >= cutoff:

                cleaned.append(
                    item
                )

        except Exception:

            pass

    save_history(
        cleaned
    )

    return cleaned


# ============================================================
# EMA
# ============================================================

def calculate_ema(
    values,
    period
):

    if len(values) < period:

        return None

    multiplier = (
        2 / (period + 1)
    )

    ema_value = (
        sum(
            values[:period]
        )
        / period
    )

    for price in values[period:]:

        ema_value = (
            (
                price
                - ema_value
            )
            * multiplier
        ) + ema_value

    return ema_value


# ============================================================
# RSI
# ============================================================

def calculate_rsi(
    values,
    period=14
):

    if len(values) < period + 1:

        return None

    gains = []

    losses = []

    for i in range(
        1,
        len(values)
    ):

        change = (
            values[i]
            - values[i - 1]
        )

        if change > 0:

            gains.append(
                change
            )

            losses.append(
                0
            )

        else:

            gains.append(
                0
            )

            losses.append(
                abs(change)
            )

    avg_gain = (
        sum(
            gains[:period]
        )
        / period
    )

    avg_loss = (
        sum(
            losses[:period]
        )
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

        return 100.0

    rs = (
        avg_gain
        / avg_loss
    )

    return 100 - (
        100 / (1 + rs)
    )


# ============================================================
# MACD
# ============================================================

def calculate_macd(
    values
):

    if len(values) < (
        MACD_SLOW
        + MACD_SIGNAL
    ):

        return None

    macd_values = []

    for i in range(
        MACD_SLOW,
        len(values) + 1
    ):

        subset = values[:i]

        fast = calculate_ema(
            subset,
            MACD_FAST
        )

        slow = calculate_ema(
            subset,
            MACD_SLOW
        )

        if (
            fast is not None
            and slow is not None
        ):

            macd_values.append(
                fast - slow
            )

    if len(macd_values) < MACD_SIGNAL:

        return None

    macd = macd_values[-1]

    signal = calculate_ema(
        macd_values,
        MACD_SIGNAL
    )

    if signal is None:

        return None

    histogram = (
        macd - signal
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
    candles,
    period=14
):

    if len(candles) < period + 1:

        return None

    true_ranges = []

    for i in range(
        1,
        len(candles)
    ):

        current = candles[i]

        previous = candles[i - 1]

        high = current["high"]

        low = current["low"]

        previous_close = (
            previous["close"]
        )

        tr = max(
            high - low,

            abs(
                high
                - previous_close
            ),

            abs(
                low
                - previous_close
            )
        )

        true_ranges.append(
            tr
        )

    if len(true_ranges) < period:

        return None

    return (
        sum(
            true_ranges[-period:]
        )
        / period
    )


# ============================================================
# AGGREGATE DATA
# ============================================================

def aggregate_candles(
    points,
    minutes
):

    if not points:

        return []

    buckets = {}

    for item in points:

        dt = item["time"]

        total_minutes = (
            dt.hour * 60
            + dt.minute
        )

        bucket_start = (
            total_minutes // minutes
        ) * minutes

        hour = bucket_start // 60

        minute = (
            bucket_start % 60
        )

        bucket_time = dt.replace(
            hour=hour,
            minute=minute,
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

    candles = []

    for key in sorted(
        buckets.keys()
    ):

        values = buckets[key]

        if not values:

            continue

        candles.append({
            "time":
                datetime.fromisoformat(
                    key
                ),

            "open":
                values[0],

            "high":
                max(values),

            "low":
                min(values),

            "close":
                values[-1]
        })

    return candles


# ============================================================
# SWINGS
# ============================================================

def find_swings(
    candles
):

    swing_highs = []

    swing_lows = []

    minimum = (
        SWING_LEFT
        + SWING_RIGHT
        + 1
    )

    if len(candles) < minimum:

        return (
            swing_highs,
            swing_lows
        )

    for i in range(
        SWING_LEFT,
        len(candles)
        - SWING_RIGHT
    ):

        current = candles[i]

        left = candles[
            i - SWING_LEFT:i
        ]

        right = candles[
            i + 1:
            i + 1 + SWING_RIGHT
        ]

        is_high = all(
            current["high"]
            > x["high"]
            for x in (
                left + right
            )
        )

        is_low = all(
            current["low"]
            < x["low"]
            for x in (
                left + right
            )
        )

        if is_high:

            swing_highs.append({
                "index": i,
                "price":
                    current["high"],
                "time":
                    current["time"]
            })

        if is_low:

            swing_lows.append({
                "index": i,
                "price":
                    current["low"],
                "time":
                    current["time"]
            })

    return (
        swing_highs,
        swing_lows
    )


# ============================================================
# SUPPORT / RESISTANCE
# ============================================================

def calculate_support_resistance(
    candles,
    current_price
):

    # FIX:
    # return ทุก key เสมอ
    empty = {
        "support": None,
        "support2": None,
        "resistance": None,
        "resistance2": None
    }

    if not candles:

        return empty

    recent = candles[
        -SR_LOOKBACK:
    ]

    if not recent:

        return empty

    highs = sorted(
        [
            x["high"]
            for x in recent
            if x["high"] > current_price
        ]
    )

    lows = sorted(
        [
            x["low"]
            for x in recent
            if x["low"] < current_price
        ],
        reverse=True
    )

    resistance = (
        highs[0]
        if highs
        else max(
            x["high"]
            for x in recent
        )
    )

    resistance2 = (
        highs[1]
        if len(highs) > 1
        else None
    )

    support = (
        lows[0]
        if lows
        else min(
            x["low"]
            for x in recent
        )
    )

    support2 = (
        lows[1]
        if len(lows) > 1
        else None
    )

    return {
        "support":
            support,

        "support2":
            support2,

        "resistance":
            resistance,

        "resistance2":
            resistance2
    }


# ============================================================
# CANDLE PATTERN
# ============================================================

def detect_candle_pattern(
    candles
):

    default = {
        "name": "NONE",
        "direction": "NONE"
    }

    if len(candles) < 3:

        return default

    c1 = candles[-1]

    c2 = candles[-2]

    body1 = abs(
        c1["close"]
        - c1["open"]
    )

    range1 = (
        c1["high"]
        - c1["low"]
    )

    if range1 <= 0:

        return default

    upper_wick = (
        c1["high"]
        - max(
            c1["open"],
            c1["close"]
        )
    )

    lower_wick = (
        min(
            c1["open"],
            c1["close"]
        )
        - c1["low"]
    )

    bullish_engulfing = (
        c2["close"]
        < c2["open"]
        and
        c1["close"]
        > c1["open"]
        and
        c1["open"]
        <= c2["close"]
        and
        c1["close"]
        >= c2["open"]
    )

    if bullish_engulfing:

        return {
            "name":
                "Bullish Engulfing",

            "direction":
                "BULLISH"
        }

    bearish_engulfing = (
        c2["close"]
        > c2["open"]
        and
        c1["close"]
        < c1["open"]
        and
        c1["open"]
        >= c2["close"]
        and
        c1["close"]
        <= c2["open"]
    )

    if bearish_engulfing:

        return {
            "name":
                "Bearish Engulfing",

            "direction":
                "BEARISH"
        }

    hammer = (
        lower_wick >= body1 * 2
        and
        upper_wick <= body1
        and
        (
            body1 / range1
        ) <= 0.5
    )

    if hammer:

        return {
            "name":
                "Hammer",

            "direction":
                "BULLISH"
        }

    shooting_star = (
        upper_wick >= body1 * 2
        and
        lower_wick <= body1
        and
        (
            body1 / range1
        ) <= 0.5
    )

    if shooting_star:

        return {
            "name":
                "Shooting Star",

            "direction":
                "BEARISH"
        }

    inside_bar = (
        c1["high"]
        <= c2["high"]
        and
        c1["low"]
        >= c2["low"]
    )

    if inside_bar:

        return {
            "name":
                "Inside Bar",

            "direction":
                "NEUTRAL"
        }

    return default


# ============================================================
# FIBONACCI
# ============================================================

def calculate_fibonacci(
    candles
):

    if len(candles) < 10:

        return None

    swing_highs, swing_lows = (
        find_swings(candles)
    )

    if (
        not swing_highs
        or not swing_lows
    ):

        return None

    latest_high = (
        swing_highs[-1]
    )

    latest_low = (
        swing_lows[-1]
    )

    high = latest_high["price"]

    low = latest_low["price"]

    if high <= low:

        return None

    distance = high - low

    return {
        "0.0":
            high,

        "23.6":
            high - distance * 0.236,

        "38.2":
            high - distance * 0.382,

        "50.0":
            high - distance * 0.500,

        "61.8":
            high - distance * 0.618,

        "78.6":
            high - distance * 0.786,

        "100.0":
            low
    }


# ============================================================
# NEAREST FIB
# ============================================================

def nearest_fib(
    price,
    fib
):

    if not fib:

        return None

    best = None

    best_distance = float(
        "inf"
    )

    for level, value in fib.items():

        distance = abs(
            price - value
        )

        if distance < best_distance:

            best_distance = distance

            best = {
                "level":
                    level,

                "price":
                    value,

                "distance":
                    distance
            }

    return best


# ============================================================
# BREAKOUT
# ============================================================

def detect_breakout(
    candles,
    sr,
    atr
):

    default = {
        "type":
            "NONE",

        "level":
            None,

        "confirmed":
            False
    }

    if len(candles) < 3:

        return default

    current = candles[-1]

    previous = candles[-2]

    close = current["close"]

    resistance = sr.get(
        "resistance"
    )

    support = sr.get(
        "support"
    )

    buffer = (
        atr * BREAKOUT_BUFFER_ATR
        if atr
        else 0
    )

    if resistance is not None:

        if (
            close
            > resistance + buffer
            and
            previous["close"]
            <= resistance + buffer
        ):

            return {
                "type":
                    "BULLISH_BREAKOUT",

                "level":
                    resistance,

                "confirmed":
                    True
            }

    if support is not None:

        if (
            close
            < support - buffer
            and
            previous["close"]
            >= support - buffer
        ):

            return {
                "type":
                    "BEARISH_BREAKOUT",

                "level":
                    support,

                "confirmed":
                    True
            }

    return default


# ============================================================
# RETEST
# ============================================================

def detect_retest(
    candles,
    level,
    direction,
    atr
):

    if (
        level is None
        or len(candles) < 3
    ):

        return False

    tolerance = (
        atr * RETEST_TOLERANCE_ATR
        if atr
        else 2.0
    )

    recent = candles[-3:]

    if direction == "BULLISH":

        touched = any(
            abs(
                c["low"]
                - level
            ) <= tolerance
            or
            (
                c["low"]
                <= level
                <= c["high"]
            )
            for c in recent
        )

        recovered = (
            candles[-1]["close"]
            > level
        )

        return (
            touched
            and recovered
        )

    if direction == "BEARISH":

        touched = any(
            abs(
                c["high"]
                - level
            ) <= tolerance
            or
            (
                c["low"]
                <= level
                <= c["high"]
            )
            for c in recent
        )

        rejected = (
            candles[-1]["close"]
            < level
        )

        return (
            touched
            and rejected
        )

    return False


# ============================================================
# TIMEFRAME ANALYSIS
# ============================================================

def analyze_timeframe(
    candles,
    label
):

    # ========================================================
    # FIX #1
    # แม้ข้อมูลไม่พอ ต้อง return key ครบ
    # ========================================================

    if len(candles) < 30:

        current = (
            candles[-1]["close"]
            if candles
            else None
        )

        return {
            "label":
                label,

            "ready":
                False,

            "candles":
                candles,

            "current":
                current,

            "ema9":
                None,

            "ema21":
                None,

            "ema50":
                None,

            "ema200":
                None,

            "rsi":
                None,

            "macd":
                None,

            "atr":
                None,

            "support":
                None,

            "support2":
                None,

            "resistance":
                None,

            "resistance2":
                None,

            "pattern": {
                "name":
                    "DATA NOT READY",

                "direction":
                    "NONE"
            },

            "fib":
                None,

            "nearest_fib":
                None,

            "breakout": {
                "type":
                    "NONE",

                "level":
                    None,

                "confirmed":
                    False
            },

            "bullish_score":
                0,

            "bearish_score":
                0,

            "trend":
                "DATA NOT READY"
        }

    closes = [
        x["close"]
        for x in candles
    ]

    current = closes[-1]

    ema9 = calculate_ema(
        closes,
        EMA_FAST
    )

    ema21 = calculate_ema(
        closes,
        EMA_MID
    )

    ema50 = calculate_ema(
        closes,
        EMA_SLOW
    )

    ema200 = calculate_ema(
        closes,
        EMA_LONG
    )

    rsi = calculate_rsi(
        closes,
        RSI_PERIOD
    )

    macd = calculate_macd(
        closes
    )

    atr = calculate_atr(
        candles,
        ATR_PERIOD
    )

    sr = calculate_support_resistance(
        candles,
        current
    )

    pattern = detect_candle_pattern(
        candles
    )

    fib = calculate_fibonacci(
        candles
    )

    nearest_fib_level = nearest_fib(
        current,
        fib
    )

    breakout = detect_breakout(
        candles,
        sr,
        atr
    )

    bullish = 0

    bearish = 0

    # EMA

    if (
        ema9 is not None
        and ema21 is not None
    ):

        if ema9 > ema21:

            bullish += 1

        elif ema9 < ema21:

            bearish += 1

    if (
        ema21 is not None
        and ema50 is not None
    ):

        if ema21 > ema50:

            bullish += 1

        elif ema21 < ema50:

            bearish += 1

    if (
        ema50 is not None
        and ema200 is not None
    ):

        if ema50 > ema200:

            bullish += 2

        elif ema50 < ema200:

            bearish += 2

    # Price vs EMA

    if ema21 is not None:

        if current > ema21:

            bullish += 1

        elif current < ema21:

            bearish += 1

    # RSI

    if rsi is not None:

        if 50 < rsi < 70:

            bullish += 1

        elif 30 < rsi < 50:

            bearish += 1

    # MACD

    if macd:

        if macd["histogram"] > 0:

            bullish += 1

        elif macd["histogram"] < 0:

            bearish += 1

    # Candle

    if pattern["direction"] == "BULLISH":

        bullish += 1

    elif pattern["direction"] == "BEARISH":

        bearish += 1

    # Breakout

    if (
        breakout["type"]
        == "BULLISH_BREAKOUT"
    ):

        bullish += 2

    elif (
        breakout["type"]
        == "BEARISH_BREAKOUT"
    ):

        bearish += 2

    # Trend

    if bullish >= bearish + 3:

        trend = "BULLISH"

    elif bearish >= bullish + 3:

        trend = "BEARISH"

    else:

        trend = "SIDEWAY"

    return {
        "label":
            label,

        "ready":
            True,

        "candles":
            candles,

        "current":
            current,

        "ema9":
            ema9,

        "ema21":
            ema21,

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
            sr["support"],

        "support2":
            sr["support2"],

        "resistance":
            sr["resistance"],

        "resistance2":
            sr["resistance2"],

        "pattern":
            pattern,

        "fib":
            fib,

        "nearest_fib":
            nearest_fib_level,

        "breakout":
            breakout,

        "bullish_score":
            bullish,

        "bearish_score":
            bearish,

        "trend":
            trend
    }


# ============================================================
# MULTI TIMEFRAME
# ============================================================

def analyze_mtf(
    points
):

    timeframe_minutes = {
        "5M": 5,
        "15M": 15,
        "1H": 60,
        "4H": 240
    }

    result = {}

    for label, minutes in (
        timeframe_minutes.items()
    ):

        candles = aggregate_candles(
            points,
            minutes
        )

        result[label] = (
            analyze_timeframe(
                candles,
                label
            )
        )

    return result


# ============================================================
# GLOBAL SIGNAL
# ============================================================

def build_global_signal(
    mtf
):

    bullish_score = 0

    bearish_score = 0

    ready = 0

    for label, weight in [
        ("5M", 1),
        ("15M", 1),
        ("1H", 2),
        ("4H", 3)
    ]:

        data = mtf.get(
            label
        )

        if (
            not data
            or not data.get(
                "ready",
                False
            )
        ):

            continue

        ready += 1

        trend = data.get(
            "trend",
            "SIDEWAY"
        )

        if trend == "BULLISH":

            bullish_score += (
                weight
                + min(
                    data.get(
                        "bullish_score",
                        0
                    ),
                    3
                )
            )

        elif trend == "BEARISH":

            bearish_score += (
                weight
                + min(
                    data.get(
                        "bearish_score",
                        0
                    ),
                    3
                )
            )

    total = (
        bullish_score
        + bearish_score
    )

    if total == 0:

        confidence = 0

    else:

        confidence = round(
            (
                max(
                    bullish_score,
                    bearish_score
                )
                / total
            ) * 100
        )

    if (
        bullish_score
        >= bearish_score + 4
    ):

        signal = "BUY_BIAS"

    elif (
        bearish_score
        >= bullish_score + 4
    ):

        signal = "SELL_BIAS"

    else:

        signal = "NEUTRAL"

    return {
        "signal":
            signal,

        "bullish":
            bullish_score,

        "bearish":
            bearish_score,

        "confidence":
            confidence,

        "ready":
            ready
    }


# ============================================================
# TRADE SETUP
# ============================================================

def build_trade_setup(
    spot,
    mtf,
    global_signal
):

    price = spot["price"]

    # ========================================================
    # FIX #2
    # ค่า default ครบทุก key
    # ดังนั้นไม่มี KeyError:
    # reasons
    # buy_score
    # sell_score
    # ========================================================

    empty_setup = {
        "direction":
            "NO_TRADE",

        "score":
            0,

        "buy_score":
            0,

        "sell_score":
            0,

        "reasons":
            [],

        "entry":
            None,

        "stop":
            None,

        "tp1":
            None,

        "tp2":
            None,

        "atr":
            None
    }

    h1 = mtf.get(
        "1H"
    )

    h4 = mtf.get(
        "4H"
    )

    m15 = mtf.get(
        "15M"
    )

    if not h1 or not h4:

        return empty_setup

    if (
        not h1.get(
            "ready",
            False
        )
        or
        not h4.get(
            "ready",
            False
        )
    ):

        return empty_setup

    buy = 0

    sell = 0

    reasons_buy = []

    reasons_sell = []

    # ========================================================
    # MTF
    # ========================================================

    if h4.get(
        "trend"
    ) == "BULLISH":

        buy += 3

        reasons_buy.append(
            "4H Bullish"
        )

    elif h4.get(
        "trend"
    ) == "BEARISH":

        sell += 3

        reasons_sell.append(
            "4H Bearish"
        )

    if h1.get(
        "trend"
    ) == "BULLISH":

        buy += 2

        reasons_buy.append(
            "1H Bullish"
        )

    elif h1.get(
        "trend"
    ) == "BEARISH":

        sell += 2

        reasons_sell.append(
            "1H Bearish"
        )

    if (
        m15
        and m15.get(
            "ready",
            False
        )
    ):

        if m15.get(
            "trend"
        ) == "BULLISH":

            buy += 1

            reasons_buy.append(
                "15M Bullish"
            )

        elif m15.get(
            "trend"
        ) == "BEARISH":

            sell += 1

            reasons_sell.append(
                "15M Bearish"
            )

    # ========================================================
    # EMA
    # ========================================================

    ema9 = h1.get(
        "ema9"
    )

    ema21 = h1.get(
        "ema21"
    )

    ema50 = h1.get(
        "ema50"
    )

    if (
        ema9 is not None
        and
        ema21 is not None
        and
        ema50 is not None
    ):

        if (
            ema9
            > ema21
            > ema50
        ):

            buy += 2

            reasons_buy.append(
                "EMA Alignment"
            )

        elif (
            ema9
            < ema21
            < ema50
        ):

            sell += 2

            reasons_sell.append(
                "EMA Alignment"
            )

    # ========================================================
    # RSI
    # ========================================================

    rsi = h1.get(
        "rsi"
    )

    if rsi is not None:

        if (
            50
            < rsi
            < 70
        ):

            buy += 1

            reasons_buy.append(
                f"RSI {rsi:.1f}"
            )

        elif (
            30
            < rsi
            < 50
        ):

            sell += 1

            reasons_sell.append(
                f"RSI {rsi:.1f}"
            )

    # ========================================================
    # MACD
    # ========================================================

    macd = h1.get(
        "macd"
    )

    if macd:

        histogram = macd.get(
            "histogram"
        )

        if histogram is not None:

            if histogram > 0:

                buy += 1

                reasons_buy.append(
                    "MACD Positive"
                )

            elif histogram < 0:

                sell += 1

                reasons_sell.append(
                    "MACD Negative"
                )

    # ========================================================
    # BREAKOUT
    # ========================================================

    breakout = h1.get(
        "breakout"
    ) or {}

    breakout_type = breakout.get(
        "type",
        "NONE"
    )

    if (
        breakout_type
        == "BULLISH_BREAKOUT"
    ):

        buy += 2

        reasons_buy.append(
            "Resistance Breakout"
        )

    elif (
        breakout_type
        == "BEARISH_BREAKOUT"
    ):

        sell += 2

        reasons_sell.append(
            "Support Breakout"
        )

    # ========================================================
    # RETEST
    # ========================================================

    if breakout.get(
        "confirmed",
        False
    ):

        level = breakout.get(
            "level"
        )

        atr = h1.get(
            "atr"
        )

        candles = h1.get(
            "candles",
            []
        )

        if (
            level is not None
            and candles
        ):

            if (
                breakout_type
                == "BULLISH_BREAKOUT"
            ):

                if detect_retest(
                    candles,
                    level,
                    "BULLISH",
                    atr
                ):

                    buy += 2

                    reasons_buy.append(
                        "Breakout Retest"
                    )

            elif (
                breakout_type
                == "BEARISH_BREAKOUT"
            ):

                if detect_retest(
                    candles,
                    level,
                    "BEARISH",
                    atr
                ):

                    sell += 2

                    reasons_sell.append(
                        "Breakout Retest"
                    )

    # ========================================================
    # CANDLE PATTERN
    # ========================================================

    pattern = h1.get(
        "pattern"
    ) or {}

    pattern_direction = pattern.get(
        "direction",
        "NONE"
    )

    pattern_name = pattern.get(
        "name",
        "NONE"
    )

    if (
        pattern_direction
        == "BULLISH"
    ):

        buy += 1

        reasons_buy.append(
            pattern_name
        )

    elif (
        pattern_direction
        == "BEARISH"
    ):

        sell += 1

        reasons_sell.append(
            pattern_name
        )

    # ========================================================
    # SUPPORT / RESISTANCE
    # ========================================================

    support = h1.get(
        "support"
    )

    resistance = h1.get(
        "resistance"
    )

    atr = h1.get(
        "atr"
    )

    if (
        support is not None
        and atr
    ):

        support_distance = (
            price
            - support
        )

        if (
            0
            <= support_distance
            <= atr
        ):

            buy += 2

            reasons_buy.append(
                "Near Support"
            )

    if (
        resistance is not None
        and atr
    ):

        resistance_distance = (
            resistance
            - price
        )

        if (
            0
            <= resistance_distance
            <= atr
        ):

            sell += 2

            reasons_sell.append(
                "Near Resistance"
            )

    # ========================================================
    # FIBONACCI
    # ========================================================

    nearest_fib = h1.get(
        "nearest_fib"
    )

    if (
        nearest_fib
        and atr
    ):

        fib_price = nearest_fib.get(
            "price"
        )

        fib_distance = nearest_fib.get(
            "distance"
        )

        fib_level = nearest_fib.get(
            "level"
        )

        if (
            fib_price is not None
            and
            fib_distance is not None
            and
            fib_distance
            <= atr * 0.35
        ):

            if price > fib_price:

                buy += 1

                reasons_buy.append(
                    f"Fib {fib_level}%"
                )

            else:

                sell += 1

                reasons_sell.append(
                    f"Fib {fib_level}%"
                )

    # ========================================================
    # FINAL DIRECTION
    # ========================================================

    if (
        buy >= MIN_SIGNAL_SCORE
        and
        buy >= sell + 3
    ):

        direction = "BUY"

        score = buy

        reasons = reasons_buy

    elif (
        sell >= MIN_SIGNAL_SCORE
        and
        sell >= buy + 3
    ):

        direction = "SELL"

        score = sell

        reasons = reasons_sell

    else:

        direction = "NO_TRADE"

        score = max(
            buy,
            sell
        )

        if buy > sell:

            reasons = reasons_buy

        elif sell > buy:

            reasons = reasons_sell

        else:

            reasons = []

    # ========================================================
    # RISK
    # ========================================================

    entry = price

    stop = None

    tp1 = None

    tp2 = None

    if (
        direction
        in (
            "BUY",
            "SELL"
        )
        and atr
    ):

        if direction == "BUY":

            if support is not None:

                structural_stop = (
                    support
                )

            else:

                structural_stop = (
                    entry
                    - atr * 1.5
                )

            atr_stop = (
                entry
                - atr
                * SL_ATR_MULTIPLIER
            )

            stop = min(
                structural_stop,
                atr_stop
            )

            risk = (
                entry
                - stop
            )

            if risk > 0:

                tp1 = (
                    entry
                    + risk * TP1_RR
                )

                tp2 = (
                    entry
                    + risk * TP2_RR
                )

            else:

                direction = "NO_TRADE"

        else:

            if resistance is not None:

                structural_stop = (
                    resistance
                )

            else:

                structural_stop = (
                    entry
                    + atr * 1.5
                )

            atr_stop = (
                entry
                + atr
                * SL_ATR_MULTIPLIER
            )

            stop = max(
                structural_stop,
                atr_stop
            )

            risk = (
                stop
                - entry
            )

            if risk > 0:

                tp1 = (
                    entry
                    - risk * TP1_RR
                )

                tp2 = (
                    entry
                    - risk * TP2_RR
                )

            else:

                direction = "NO_TRADE"

    if direction == "NO_TRADE":

        entry = None

        stop = None

        tp1 = None

        tp2 = None

    return {
        "direction":
            direction,

        "score":
            score,

        "buy_score":
            buy,

        "sell_score":
            sell,

        "reasons":
            reasons,

        "entry":
            entry,

        "stop":
            stop,

        "tp1":
            tp1,

        "tp2":
            tp2,

        "atr":
            atr
    }


# ============================================================
# SIGNAL QUALITY
# ============================================================

def signal_quality(
    score
):

    if score >= STRONG_SIGNAL_SCORE:

        return "🔥 STRONG"

    if score >= MIN_SIGNAL_SCORE:

        return "🟢 GOOD"

    if score >= 6:

        return "🟡 WEAK"

    return "⚪ NO TRADE"


# ============================================================
# SIGNAL LOG
# ============================================================

def load_signal_log():

    if not os.path.exists(
        SIGNAL_LOG_FILE
    ):

        return []

    try:

        with open(
            SIGNAL_LOG_FILE,
            "r",
            encoding="utf-8"
        ) as f:

            return json.load(f)

    except Exception:

        return []


def save_signal_log(
    data
):

    try:

        with open(
            SIGNAL_LOG_FILE,
            "w",
            encoding="utf-8"
        ) as f:

            json.dump(
                data,
                f,
                ensure_ascii=False,
                indent=2
            )

    except Exception as e:

        print(
            "SIGNAL LOG ERROR:",
            repr(e)
        )


def record_signal(
    spot,
    setup,
    global_signal
):

    log = load_signal_log()

    log.append({
        "time":
            now_thai().isoformat(),

        "price":
            spot["price"],

        "direction":
            setup.get(
                "direction",
                "NO_TRADE"
            ),

        "score":
            setup.get(
                "score",
                0
            ),

        "buy_score":
            setup.get(
                "buy_score",
                0
            ),

        "sell_score":
            setup.get(
                "sell_score",
                0
            ),

        "entry":
            setup.get(
                "entry"
            ),

        "stop":
            setup.get(
                "stop"
            ),

        "tp1":
            setup.get(
                "tp1"
            ),

        "tp2":
            setup.get(
                "tp2"
            ),

        "global_signal":
            global_signal.get(
                "signal",
                "NEUTRAL"
            ),

        "confidence":
            global_signal.get(
                "confidence",
                0
            )
    })

    log = log[-500:]

    save_signal_log(
        log
    )


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
            f"ข้อมูล Intraday ไม่พอ: "
            f"{len(points)} จุด"
        )

    mtf = analyze_mtf(
        points
    )

    global_signal = (
        build_global_signal(
            mtf
        )
    )

    setup = build_trade_setup(
        spot,
        mtf,
        global_signal
    )

    return (
        spot,
        points,
        mtf,
        global_signal,
        setup
    )


# ============================================================
# FORMAT
# ============================================================

def trend_icon(
    trend
):

    if trend == "BULLISH":

        return "🟢"

    if trend == "BEARISH":

        return "🔴"

    if trend == "DATA NOT READY":

        return "⚠️"

    return "🟡"


def signal_text(
    signal
):

    if signal == "BUY_BIAS":

        return "🟢 BUY BIAS"

    if signal == "SELL_BIAS":

        return "🔴 SELL BIAS"

    return "🟡 NEUTRAL"


def format_tf(
    data
):

    label = data.get(
        "label",
        "TF"
    )

    if not data.get(
        "ready",
        False
    ):

        return (
            f"**{label}** "
            "⚠️ DATA NOT READY"
        )

    rsi = data.get(
        "rsi"
    )

    if rsi is not None:

        rsi_text = f"{rsi:.1f}"

    else:

        rsi_text = "N/A"

    macd_text = "N/A"

    macd = data.get(
        "macd"
    )

    if macd:

        histogram = macd.get(
            "histogram"
        )

        if histogram is not None:

            macd_text = (
                f"{histogram:.2f}"
            )

    return (
        f"{trend_icon(data.get('trend'))} "
        f"**{label} "
        f"{data.get('trend', 'UNKNOWN')}**\n"

        f"Price "
        f"`{fmt_price(data.get('current'))}` | "

        f"RSI `{rsi_text}` | "

        f"MACD Hist "
        f"`{macd_text}`\n"

        f"EMA9 "
        f"`{fmt_price(data.get('ema9'))}` | "

        f"EMA21 "
        f"`{fmt_price(data.get('ema21'))}` | "

        f"EMA50 "
        f"`{fmt_price(data.get('ema50'))}`\n"

        f"Support "
        f"`{fmt_price(data.get('support'))}` | "

        f"Resistance "
        f"`{fmt_price(data.get('resistance'))}`"
    )


# ============================================================
# ANALYSIS MESSAGE
# ============================================================

def build_analysis_message(
    spot,
    mtf,
    global_signal,
    setup
):

    price = spot["price"]

    setup_direction = setup.get(
        "direction",
        "NO_TRADE"
    )

    if setup_direction == "BUY":

        setup_text = (
            "🟢 BUY SETUP"
        )

    elif setup_direction == "SELL":

        setup_text = (
            "🔴 SELL SETUP"
        )

    else:

        setup_text = (
            "🟡 NO TRADE"
        )

    lines = [
        "🪙 **XAU/USD V2 ANALYSIS**",
        "━━━━━━━━━━━━━━━━━━━━━━━━",
        "",
        f"💰 Price: **{fmt_price(price)}**",
        f"🎯 Bias: **{signal_text(global_signal.get('signal', 'NEUTRAL'))}**",
        f"📊 Confidence: **{global_signal.get('confidence', 0)}%**",
        "",
        f"🧠 Setup: **{setup_text}**",
        f"⭐ Score: **{setup.get('score', 0)}**",
        f"Quality: **{signal_quality(setup.get('score', 0))}**",
        "",
        "━━━━━━━━━━━━━━━━━━━━━━━━",
        "📊 **MULTI TIMEFRAME**",
        ""
    ]

    for label in [
        "5M",
        "15M",
        "1H",
        "4H"
    ]:

        lines.append(
            format_tf(
                mtf.get(
                    label,
                    {
                        "label":
                            label,

                        "ready":
                            False
                    }
                )
            )
        )

        lines.append("")

    h1 = mtf.get(
        "1H",
        {}
    )

    lines.extend([
        "━━━━━━━━━━━━━━━━━━━━━━━━",
        "🧱 **MARKET STRUCTURE**",
        "",
        f"Support 1: "
        f"`{fmt_price(h1.get('support'))}`",

        f"Support 2: "
        f"`{fmt_price(h1.get('support2'))}`",

        f"Resistance 1: "
        f"`{fmt_price(h1.get('resistance'))}`",

        f"Resistance 2: "
        f"`{fmt_price(h1.get('resistance2'))}`",

        ""
    ])

    pattern = h1.get(
        "pattern",
        {}
    ) or {}

    breakout = h1.get(
        "breakout",
        {}
    ) or {}

    fib = h1.get(
        "nearest_fib"
    )

    lines.extend([
        "🕯️ **PATTERN**",
        f"`{pattern.get('name', 'NONE')}`",
        "",
        "🚀 **BREAKOUT**",
        f"`{breakout.get('type', 'NONE')}`",
        ""
    ])

    if fib:

        lines.extend([
            "📐 **FIBONACCI**",

            f"Nearest: "
            f"`{fib.get('level', 'N/A')}%` "
            f"@ "
            f"`{fmt_price(fib.get('price'))}`",

            ""
        ])

    lines.extend([
        "━━━━━━━━━━━━━━━━━━━━━━━━",
        "🛡️ **RISK / SETUP**",
        ""
    ])

    if setup_direction in (
        "BUY",
        "SELL"
    ):

        lines.extend([
            f"Entry: "
            f"`{fmt_price(setup.get('entry'))}`",

            f"Stop Loss: "
            f"`{fmt_price(setup.get('stop'))}`",

            f"TP1: "
            f"`{fmt_price(setup.get('tp1'))}` "
            f"(1:{TP1_RR})",

            f"TP2: "
            f"`{fmt_price(setup.get('tp2'))}` "
            f"(1:{TP2_RR})",

            f"ATR: "
            f"`{fmt_price(setup.get('atr'))}`",

            ""
        ])

    else:

        lines.extend([
            "⛔ ไม่มี Setup ที่ผ่านเกณฑ์",
            "ระบบเลือก **NO TRADE**",
            ""
        ])

    reasons = setup.get(
        "reasons",
        []
    ) or []

    if reasons:

        lines.append(
            "🔎 **CONFLUENCE**"
        )

        for reason in reasons[:12]:

            lines.append(
                f"• {reason}"
            )

        lines.append("")

    lines.extend([
        "━━━━━━━━━━━━━━━━━━━━━━━━",
        "⚠️ Technical analysis only.",
        "ไม่ใช่คำแนะนำการลงทุนและไม่รับประกันผลลัพธ์",
        "ราคา XAU/USD จาก API เป็น indicative spot ไม่ใช่ราคา execution"
    ])

    return "\n".join(
        lines
    )


# ============================================================
# ALERT MESSAGE
# ============================================================

def build_alert_message(
    spot,
    mtf,
    global_signal,
    setup
):

    direction = setup.get(
        "direction",
        "NO_TRADE"
    )

    if direction == "BUY":

        title = (
            "🟢 HIGH QUALITY BUY SETUP"
        )

    elif direction == "SELL":

        title = (
            "🔴 HIGH QUALITY SELL SETUP"
        )

    else:

        return None

    h1 = mtf.get(
        "1H",
        {}
    )

    lines = [
        "🚨 **XAU/USD V2 ALERT**",
        "━━━━━━━━━━━━━━━━━━━━━━━━",
        "",
        f"**{title}**",
        "",
        f"💰 Price: **{fmt_price(spot['price'])}**",
        f"⭐ Score: **{setup.get('score', 0)}**",
        f"📊 Confidence: **{global_signal.get('confidence', 0)}%**",
        "",
        "📊 **MTF**"
    ]

    for label in [
        "5M",
        "15M",
        "1H",
        "4H"
    ]:

        data = mtf.get(
            label
        )

        if data and data.get(
            "ready",
            False
        ):

            lines.append(
                f"{trend_icon(data.get('trend'))} "
                f"{label}: "
                f"**{data.get('trend')}**"
            )

    lines.extend([
        "",
        "🧱 **LEVELS**",
        f"Support: "
        f"`{fmt_price(h1.get('support'))}`",

        f"Resistance: "
        f"`{fmt_price(h1.get('resistance'))}`",

        "",
        "🕯️ **PATTERN**",
        f"`{(h1.get('pattern') or {}).get('name', 'NONE')}`",

        "",
        "🚀 **STRUCTURE**",
        f"`{(h1.get('breakout') or {}).get('type', 'NONE')}`",

        ""
    ])

    if setup.get(
        "entry"
    ):

        lines.extend([
            "🛡️ **RISK PLAN**",

            f"Entry: "
            f"`{fmt_price(setup.get('entry'))}`",

            f"SL: "
            f"`{fmt_price(setup.get('stop'))}`",

            f"TP1: "
            f"`{fmt_price(setup.get('tp1'))}`",

            f"TP2: "
            f"`{fmt_price(setup.get('tp2'))}`",

            "",

            "🔎 **CONFIRMATIONS**"
        ])

        for reason in (
            setup.get(
                "reasons",
                []
            ) or []
        )[:10]:

            lines.append(
                f"• {reason}"
            )

    lines.extend([
        "",
        "━━━━━━━━━━━━━━━━━━━━━━━━",
        "⚠️ Alert = technical setup",
        "ไม่ใช่คำสั่งให้เปิดออเดอร์อัตโนมัติ"
    ])

    return "\n".join(
        lines
    )


# ============================================================
# CHART
# ============================================================

def make_chart(
    points
):

    if len(points) < 2:

        return None

    recent = points[-500:]

    times = [
        x["time"]
        for x in recent
    ]

    prices = [
        x["price"]
        for x in recent
    ]

    ema9 = []

    ema21 = []

    for i in range(
        len(prices)
    ):

        subset = prices[
            :i + 1
        ]

        ema9.append(
            calculate_ema(
                subset,
                EMA_FAST
            )
        )

        ema21.append(
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

    ax.plot(
        times,
        [
            x
            if x is not None
            else float("nan")
            for x in ema9
        ],
        linewidth=1.2,
        label="EMA 9"
    )

    ax.plot(
        times,
        [
            x
            if x is not None
            else float("nan")
            for x in ema21
        ],
        linewidth=1.2,
        label="EMA 21"
    )

    ax.set_title(
        "XAU/USD V2"
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

    path = (
        "xau_v2_chart.png"
    )

    fig.savefig(
        path
    )

    plt.close(fig)

    return path


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

        state = (
            spot.get(
                "data_state",
                {}
            )
            or {}
        )

        await interaction.followup.send(
            "🪙 **XAU/USD**\n"
            "━━━━━━━━━━━━━━━━━━\n\n"

            f"💰 Price: "
            f"**{fmt_price(spot['price'])}**\n"

            f"📡 Status: "
            f"`{state.get('status', 'unknown')}`\n"

            f"🕐 "
            f"`{now_thai().strftime('%d/%m/%Y %H:%M:%S')}`\n\n"

            "📌 Global gold spot price"
        )

    except Exception as e:

        print(
            "GOLD COMMAND ERROR:",
            repr(e)
        )

        await interaction.followup.send(
            f"❌ GOLD ERROR\n"
            f"`{str(e)[:500]}`"
        )


# ============================================================
# /XAU
# ============================================================

@bot.tree.command(
    name="xau",
    description="วิเคราะห์ XAU/USD V2 แบบเต็ม"
)
async def xau(
    interaction: discord.Interaction
):

    await interaction.response.defer()

    try:

        result = await asyncio.to_thread(
            get_full_analysis
        )

        (
            spot,
            points,
            mtf,
            global_signal,
            setup
        ) = result

        message = (
            build_analysis_message(
                spot,
                mtf,
                global_signal,
                setup
            )
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
            f"❌ XAU ANALYSIS ERROR\n"
            f"`{str(e)[:700]}`"
        )


# ============================================================
# /ANALYSIS
# ============================================================

@bot.tree.command(
    name="analysis",
    description="EMA RSI MACD ATR MTF Structure"
)
async def analysis(
    interaction: discord.Interaction
):

    await interaction.response.defer()

    try:

        (
            spot,
            points,
            mtf,
            global_signal,
            setup
        ) = await asyncio.to_thread(
            get_full_analysis
        )

        await interaction.followup.send(
            build_analysis_message(
                spot,
                mtf,
                global_signal,
                setup
            )
        )

    except Exception as e:

        print(
            "ANALYSIS ERROR:",
            repr(e)
        )

        await interaction.followup.send(
            f"❌ ANALYSIS ERROR\n"
            f"`{str(e)[:700]}`"
        )


# ============================================================
# /SIGNAL
# ============================================================

@bot.tree.command(
    name="signal",
    description="ดู BUY SELL NO TRADE"
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
            global_signal,
            setup
        ) = await asyncio.to_thread(
            get_full_analysis
        )

        direction = setup.get(
            "direction",
            "NO_TRADE"
        )

        if direction == "BUY":

            title = "🟢 BUY"

        elif direction == "SELL":

            title = "🔴 SELL"

        else:

            title = "🟡 NO TRADE"

        message = (
            "🎯 **XAU/USD SIGNAL V2**\n"
            "━━━━━━━━━━━━━━━━━━\n\n"

            f"💰 Price: "
            f"**{fmt_price(spot['price'])}**\n\n"

            f"Signal: **{title}**\n"

            f"Score: "
            f"**{setup.get('score', 0)}**\n"

            f"Quality: "
            f"**{signal_quality(setup.get('score', 0))}**\n"

            f"Bias: "
            f"**{signal_text(global_signal.get('signal', 'NEUTRAL'))}**\n"

            f"Confidence: "
            f"**{global_signal.get('confidence', 0)}%**\n\n"

            f"🟢 Buy Score: "
            f"`{setup.get('buy_score', 0)}`\n"

            f"🔴 Sell Score: "
            f"`{setup.get('sell_score', 0)}`"
        )

        if direction in (
            "BUY",
            "SELL"
        ):

            message += (
                "\n\n🛡️ **RISK**\n"

                f"Entry: "
                f"`{fmt_price(setup.get('entry'))}`\n"

                f"SL: "
                f"`{fmt_price(setup.get('stop'))}`\n"

                f"TP1: "
                f"`{fmt_price(setup.get('tp1'))}`\n"

                f"TP2: "
                f"`{fmt_price(setup.get('tp2'))}`"
            )

        reasons = setup.get(
            "reasons",
            []
        ) or []

        if reasons:

            message += (
                "\n\n🔎 **REASONS**"
            )

            for reason in reasons[:10]:

                message += (
                    f"\n• {reason}"
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
            f"❌ SIGNAL ERROR\n"
            f"`{str(e)[:700]}`"
        )


# ============================================================
# /TREND
# ============================================================

@bot.tree.command(
    name="trend",
    description="ดูแนวโน้ม 5M 15M 1H 4H"
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
            global_signal,
            setup
        ) = await asyncio.to_thread(
            get_full_analysis
        )

        lines = [
            "📊 **XAU/USD TREND V2**",
            "━━━━━━━━━━━━━━━━━━",
            "",
            f"Price: "
            f"**{fmt_price(spot['price'])}**",
            ""
        ]

        for label in [
            "5M",
            "15M",
            "1H",
            "4H"
        ]:

            data = mtf.get(
                label,
                {}
            )

            if data.get(
                "ready",
                False
            ):

                lines.append(
                    f"{trend_icon(data.get('trend'))} "
                    f"**{label}: "
                    f"{data.get('trend')}**"
                )

            else:

                lines.append(
                    f"⚠️ {label}: "
                    "DATA NOT READY"
                )

        lines.extend([
            "",
            f"🎯 Bias: "
            f"**{signal_text(global_signal.get('signal', 'NEUTRAL'))}**",

            f"📊 Confidence: "
            f"**{global_signal.get('confidence', 0)}%**",

            "",

            f"🧠 Setup: "
            f"**{setup.get('direction', 'NO_TRADE')}**",

            f"⭐ Score: "
            f"**{setup.get('score', 0)}**"
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
            f"❌ TREND ERROR\n"
            f"`{str(e)[:700]}`"
        )


# ============================================================
# /LEVELS
# ============================================================

@bot.tree.command(
    name="levels",
    description="ดูแนวรับแนวต้านและ Fibonacci"
)
async def levels(
    interaction: discord.Interaction
):

    await interaction.response.defer()

    try:

        (
            spot,
            points,
            mtf,
            global_signal,
            setup
        ) = await asyncio.to_thread(
            get_full_analysis
        )

        # ====================================================
        # FIX #3
        # ใช้ .get() ป้องกัน KeyError: support
        # ====================================================

        h1 = mtf.get(
            "1H",
            {}
        )

        support = h1.get(
            "support"
        )

        support2 = h1.get(
            "support2"
        )

        resistance = h1.get(
            "resistance"
        )

        resistance2 = h1.get(
            "resistance2"
        )

        breakout = h1.get(
            "breakout",
            {}
        ) or {}

        fib = h1.get(
            "fib"
        )

        message = (
            "🧱 **XAU/USD MARKET LEVELS**\n"
            "━━━━━━━━━━━━━━━━━━\n\n"

            f"💰 Current: "
            f"**{fmt_price(spot['price'])}**\n\n"

            f"🟢 Support 1: "
            f"**{fmt_price(support)}**\n"

            f"🟢 Support 2: "
            f"**{fmt_price(support2)}**\n\n"

            f"🔴 Resistance 1: "
            f"**{fmt_price(resistance)}**\n"

            f"🔴 Resistance 2: "
            f"**{fmt_price(resistance2)}**\n\n"

            f"🚀 Breakout: "
            f"`{breakout.get('type', 'NONE')}`\n\n"
        )

        if fib:

            message += (
                "📐 **FIBONACCI**\n"

                f"0%: "
                f"`{fmt_price(fib.get('0.0'))}`\n"

                f"23.6%: "
                f"`{fmt_price(fib.get('23.6'))}`\n"

                f"38.2%: "
                f"`{fmt_price(fib.get('38.2'))}`\n"

                f"50%: "
                f"`{fmt_price(fib.get('50.0'))}`\n"

                f"61.8%: "
                f"`{fmt_price(fib.get('61.8'))}`\n"

                f"78.6%: "
                f"`{fmt_price(fib.get('78.6'))}`\n"

                f"100%: "
                f"`{fmt_price(fib.get('100.0'))}`"
            )

        else:

            message += (
                "📐 **FIBONACCI**\n"
                "⚠️ ยังไม่สามารถคำนวณ "
                "Fibonacci ได้จากข้อมูลปัจจุบัน"
            )

        await interaction.followup.send(
            message
        )

    except Exception as e:

        print(
            "LEVELS ERROR:",
            repr(e)
        )

        traceback.print_exc()

        await interaction.followup.send(
            f"❌ LEVELS ERROR\n"
            f"`{str(e)[:700]}`"
        )


# ============================================================
# /PATTERN
# ============================================================

@bot.tree.command(
    name="pattern",
    description="ดู Candlestick Pattern"
)
async def pattern(
    interaction: discord.Interaction
):

    await interaction.response.defer()

    try:

        (
            spot,
            points,
            mtf,
            global_signal,
            setup
        ) = await asyncio.to_thread(
            get_full_analysis
        )

        lines = [
            "🕯️ **XAU/USD CANDLE PATTERN**",
            "━━━━━━━━━━━━━━━━━━",
            ""
        ]

        for label in [
            "5M",
            "15M",
            "1H",
            "4H"
        ]:

            data = mtf.get(
                label,
                {}
            )

            if data.get(
                "ready",
                False
            ):

                pattern_data = (
                    data.get(
                        "pattern",
                        {}
                    )
                    or {}
                )

                lines.append(
                    f"{label}: "
                    f"**{pattern_data.get('name', 'NONE')}**"
                )

            else:

                lines.append(
                    f"{label}: "
                    "DATA NOT READY"
                )

        await interaction.followup.send(
            "\n".join(lines)
        )

    except Exception as e:

        print(
            "PATTERN ERROR:",
            repr(e)
        )

        await interaction.followup.send(
            f"❌ PATTERN ERROR\n"
            f"`{str(e)[:700]}`"
        )


# ============================================================
# /CHART
# ============================================================

@bot.tree.command(
    name="chart",
    description="กราฟ XAU/USD พร้อม EMA"
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
                "⚠️ ข้อมูลกราฟไม่พอ"
            )

            return

        await interaction.followup.send(
            file=discord.File(
                path
            )
        )

        try:

            os.remove(
                path
            )

        except Exception:

            pass

    except Exception as e:

        print(
            "CHART ERROR:",
            repr(e)
        )

        await interaction.followup.send(
            f"❌ CHART ERROR\n"
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

    total_seconds = int(
        uptime.total_seconds()
    )

    hours = (
        total_seconds
        // 3600
    )

    minutes = (
        total_seconds
        % 3600
    ) // 60

    await interaction.response.send_message(
        "🤖 **GOLD BOT V2 STATUS**\n"
        "━━━━━━━━━━━━━━━━━━\n\n"

        "🟢 Discord: ONLINE\n"
        "🟢 Render: RUNNING\n"
        "🟢 XAU API: READY\n"
        "🟢 Technical Engine: READY\n"

        f"⏱️ Uptime: "
        f"`{hours}h {minutes}m`\n"

        f"⏰ Monitor: ทุก "
        f"`{CHECK_INTERVAL_MINUTES}` นาที"
    )


# ============================================================
# /HELP
# ============================================================

@bot.tree.command(
    name="help",
    description="ดูคำสั่งทั้งหมด"
)
async def help_command(
    interaction: discord.Interaction
):

    message = (
        "🪙 **GOLD BOT V2 COMMANDS**\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━\n\n"

        "`/gold`\n"
        "ราคาปัจจุบัน XAU/USD\n\n"

        "`/xau`\n"
        "วิเคราะห์เต็มทุกระบบ\n\n"

        "`/analysis`\n"
        "EMA RSI MACD ATR MTF\n\n"

        "`/signal`\n"
        "BUY / SELL / NO TRADE\n\n"

        "`/trend`\n"
        "5M / 15M / 1H / 4H\n\n"

        "`/levels`\n"
        "Support / Resistance / Fibonacci\n\n"

        "`/pattern`\n"
        "Candlestick Pattern\n\n"

        "`/chart`\n"
        "กราฟ XAU/USD + EMA\n\n"

        "`/status`\n"
        "สถานะระบบ\n\n"

        "`/help`\n"
        "คำสั่งทั้งหมด\n\n"

        "━━━━━━━━━━━━━━━━━━━━━━━━\n"

        "🧠 EMA / RSI / MACD / ATR\n"
        "📊 Multi Timeframe\n"
        "🧱 Market Structure\n"
        "🚀 Breakout / Retest\n"
        "🕯️ Candlestick\n"
        "📐 Fibonacci\n"
        "⭐ Confluence Score\n"
        "🛡️ SL / TP / R:R\n"
        "🔔 Smart Alert"
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
            "=" * 60
        )

        print(
            f"[{now_thai().strftime('%Y-%m-%d %H:%M:%S')}] "
            "XAU/USD V2 CHECK"
        )

        (
            spot,
            points,
            mtf,
            global_signal,
            setup
        ) = await asyncio.to_thread(
            get_full_analysis
        )

        price = spot["price"]

        append_history(
            price
        )

        print(
            f"PRICE: {price:.2f}"
        )

        print(
            f"BIAS: "
            f"{global_signal.get('signal')}"
        )

        print(
            f"CONFIDENCE: "
            f"{global_signal.get('confidence', 0)}%"
        )

        print(
            f"SETUP: "
            f"{setup.get('direction', 'NO_TRADE')}"
        )

        print(
            f"SCORE: "
            f"{setup.get('score', 0)}"
        )

        if last_price is not None:

            movement = (
                price
                - last_price
            )

            print(
                f"MOVE: "
                f"{movement:+.2f}"
            )

        last_price = price

        # ----------------------------------------------------
        # Only high-quality setups
        # ----------------------------------------------------

        if setup.get(
            "direction"
        ) not in (
            "BUY",
            "SELL"
        ):

            return

        if setup.get(
            "score",
            0
        ) < MIN_SIGNAL_SCORE:

            return

        if global_signal.get(
            "confidence",
            0
        ) < 65:

            return

        current = now_thai()

        if last_alert_time is not None:

            elapsed = (
                current
                - last_alert_time
            ).total_seconds() / 60

            if (
                elapsed
                < ALERT_COOLDOWN_MINUTES
            ):

                print(
                    "ALERT COOLDOWN"
                )

                return

        if (
            setup.get(
                "direction"
            )
            == last_alert_signal
        ):

            print(
                "SAME SIGNAL - SKIP"
            )

            return

        if not ALERT_CHANNEL_ID:

            print(
                "ALERT_CHANNEL_ID = 0"
            )

            return

        channel = bot.get_channel(
            ALERT_CHANNEL_ID
        )

        if channel is None:

            try:

                channel = (
                    await bot.fetch_channel(
                        ALERT_CHANNEL_ID
                    )
                )

            except Exception as e:

                print(
                    "FETCH CHANNEL ERROR:",
                    repr(e)
                )

                return

        message = (
            build_alert_message(
                spot,
                mtf,
                global_signal,
                setup
            )
        )

        if message:

            await channel.send(
                message
            )

            record_signal(
                spot,
                setup,
                global_signal
            )

            last_alert_time = (
                now_thai()
            )

            last_alert_signal = (
                setup.get(
                    "direction"
                )
            )

            print(
                "SMART ALERT SENT"
            )

    except Exception as e:

        print(
            "MONITOR ERROR:",
            repr(e)
        )

        traceback.print_exc()


# ============================================================
# BEFORE MONITOR
# ============================================================

@monitor_gold.before_loop
async def before_monitor():

    await bot.wait_until_ready()

    print(
        "XAU/USD V2 MONITOR READY"
    )


# ============================================================
# BOT READY
# ============================================================

@bot.event
async def on_ready():

    print(
        "=" * 70
    )

    print(
        "DISCORD BOT READY"
    )

    print(
        f"BOT: {bot.user}"
    )

    print(
        f"ID: {bot.user.id}"
    )

    print(
        "=" * 70
    )

    try:

        synced = await bot.tree.sync()

        print(
            f"SLASH COMMANDS SYNCED: "
            f"{len(synced)}"
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
# MAIN
# ============================================================

async def main():

    print(
        "=" * 70
    )

    print(
        "STARTING XAU/USD GOLD DISCORD BOT V2"
    )

    print(
        "=" * 70
    )

    if not DISCORD_TOKEN:

        raise RuntimeError(
            "DISCORD_TOKEN is not configured"
        )

    await start_web_server()

    print(
        "Connecting to Discord..."
    )

    await bot.start(
        DISCORD_TOKEN
    )


# ============================================================
# RUN
# ============================================================

if __name__ == "__main__":

    try:

        asyncio.run(
            main()
        )

    except KeyboardInterrupt:

        print(
            "BOT STOPPED"
        )

    except Exception as e:

        print(
            "FATAL ERROR:",
            repr(e)
        )

        traceback.print_exc()
