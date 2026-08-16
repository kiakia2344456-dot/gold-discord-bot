import os
import re
import json
import asyncio
import requests
from html import unescape
from datetime import datetime, timedelta, timezone

import discord
from discord.ext import commands, tasks
from discord import app_commands

import matplotlib
matplotlib.use("Agg")  # รันกราฟใน Background Server
import matplotlib.pyplot as plt
import matplotlib.dates as mdates

from aiohttp import web

# ==========================================
# WEB SERVER FOR KEEP ALIVE (HEALTH CHECK)
# ==========================================

async def handle_health_check(request):
    return web.Response(text="Bot is alive!", status=200)

async def start_web_server():
    app = web.Application()
    app.router.add_get('/', handle_health_check)
    app.router.add_get('/health', handle_health_check)
    
    runner = web.AppRunner(app)
    await runner.setup()
    
    # Render จะกำหนด PORT ผ่าน Environment Variable อัตโนมัติ (Default: 10000)
    port = int(os.getenv("PORT", 10000))
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
    print(f"Web server active on port {port}")

# ==========================================
# ตั้งค่า Discord
# ==========================================

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(
    command_prefix="!",
    intents=intents
)

# ==========================================
# ตั้งค่าห้องแจ้งเตือน
# ==========================================

ALERT_CHANNEL_ID = int(os.getenv("ALERT_CHANNEL_ID", "1538158164522827888"))
ALERT_THRESHOLD = 50
TZ = timezone(timedelta(hours=7))
HISTORY_FILE = "gold_history.json"
HISTORY_KEEP_DAYS = 45

last_alert_price = None

# ==========================================
# ดึงราคาทองจากสมาคมค้าทองคำ
# ==========================================

def extract_price_from_element(page, element_id):
    pattern = rf'id="{re.escape(element_id)}"[^>]*>(.*?)</span>'
    match = re.search(pattern, page, flags=re.IGNORECASE | re.DOTALL)

    if not match:
        raise ValueError(f"ไม่พบข้อมูลช่อง {element_id}")

    value = re.sub(r"<[^>]+>", "", match.group(1))
    value = unescape(value).strip()

    if not value or value.lower() == "n/a":
        raise ValueError(f"ข้อมูลช่อง {element_id} ยังไม่พร้อมใช้งาน")

    return float(value.replace(",", ""))

def get_usd_thb():
    try:
        response = requests.get(
            "https://api.frankfurter.app/latest?from=USD&to=THB",
            timeout=10,
        )
        response.raise_for_status()
        return float(response.json()["rates"]["THB"])
    except (KeyError, TypeError, ValueError, requests.RequestException):
        return None

def format_usd_thb(value):
    return f"฿{value:,.2f}" if value is not None else "ไม่พบข้อมูล"

def format_gold_spot(value):
    return f"${value:,.2f} / oz" if value is not None else "ไม่พบข้อมูล"

def get_gold_price():
    url = "https://classic.goldtraders.or.th/"

    response = requests.get(
        url,
        headers={"User-Agent": "Mozilla/5.0"},
        timeout=15,
    )
    response.raise_for_status()
    response.encoding = response.apparent_encoding
    page = response.text

    gold_sell = extract_price_from_element(
        page,
        "DetailPlace_uc_goldprices1_lblBLSell",
    )
    gold_buy = extract_price_from_element(
        page,
        "DetailPlace_uc_goldprices1_lblBLBuy",
    )
    jewelry_sell = extract_price_from_element(
        page,
        "DetailPlace_uc_goldprices1_lblOMSell",
    )
    jewelry_buy = extract_price_from_element(
        page,
        "DetailPlace_uc_goldprices1_lblOMBuy",
    )

    gold_spot = None
    for element_id in (
        "DetailPlace_uc_pricesinfo_PriceInfoTabs_TabPanel1_lblLDPClose",
        "DetailPlace_uc_pricesinfo_PriceInfoTabs_TabPanel1_lblNYClose",
    ):
        try:
            gold_spot = extract_price_from_element(page, element_id)
            break
        except ValueError:
            continue

    usd_thb = get_usd_thb()

    return (
        gold_buy,
        gold_sell,
        jewelry_buy,
        jewelry_sell,
        gold_spot,
        usd_thb
    )

# ==========================================
# ระบบเก็บประวัติราคา (วิเคราะห์แนวโน้ม)
# ==========================================

def load_history():
    if not os.path.exists(HISTORY_FILE):
        return []
    try:
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print("อ่านไฟล์ประวัติราคาไม่สำเร็จ:", e)
        return []

def save_history(history):
    try:
        with open(HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(history, f, ensure_ascii=False)
    except Exception as e:
        print("บันทึกไฟล์ประวัติราคาไม่สำเร็จ:", e)

def append_history(gold_buy, gold_sell, jewelry_buy, jewelry_sell, gold_spot, usd_thb):
    history = load_history()
    now = datetime.now(TZ)

    history.append({
        "ts": now.isoformat(),
        "gold_buy": gold_buy,
        "gold_sell": gold_sell,
        "jewelry_buy": jewelry_buy,
        "jewelry_sell": jewelry_sell,
        "gold_spot": gold_spot,
        "usd_thb": usd_thb,
    })

    cutoff = now - timedelta(days=HISTORY_KEEP_DAYS)
    history = [
        h for h in history
        if datetime.fromisoformat(h["ts"]) >= cutoff
    ]

    save_history(history)
    return history

def get_trend(history, since: datetime):
    points = [
        h for h in history
        if datetime.fromisoformat(h["ts"]) >= since
    ]

    if len(points) < 2:
        return None

    points.sort(key=lambda h: h["ts"])

    open_price = points[0]["gold_sell"]
    current_price = points[-1]["gold_sell"]

    high_price = max(p["gold_sell"] for p in points)
    low_price = min(p["gold_sell"] for p in points)

    change = current_price - open_price
    pct = (change / open_price * 100) if open_price else 0

    return {
        "open": open_price,
        "current": current_price,
        "high": high_price,
        "low": low_price,
        "change": change,
        "pct": pct,
        "count": len(points),
    }

def get_daily_closes(history):
    if not history:
        return []

    by_date = {}
    for h in sorted(history, key=lambda x: x["ts"]):
        date_key = h["ts"][:10]
        by_date[date_key] = h["gold_sell"]

    return [
        {"date": d, "close": c}
        for d, c in sorted(by_date.items())
    ]

def sma(values, period):
    if len(values) < period:
        return None
    return sum(values[-period:]) / period

def format_ma_message(daily_closes) -> str:
    closes = [d["close"] for d in daily_closes]
    current = closes[-1]

    ma7 = sma(closes, 7)
    ma25 = sma(closes, 25)

    lines = [
        "📐 **MOVING AVERAGE (เส้นค่าเฉลี่ยราคาทองแท่งขายออก)**",
        "━━━━━━━━━━━━━━━━━━\n",
        f"💰 ราคาปัจจุบัน: **฿{current:,.2f}**\n",
    ]

    if ma7 is not None:
        status7 = "อยู่ **เหนือ** MA7 📈" if current > ma7 else (
            "อยู่ **ต่ำกว่า** MA7 📉" if current < ma7 else "อยู่ที่ MA7 พอดี ➖"
        )
        lines.append(f"MA7 (ค่าเฉลี่ย 7 วัน): **฿{ma7:,.2f}** — ราคาปัจจุบัน{status7}")
    else:
        lines.append(f"MA7: ⚠️ ข้อมูลยังไม่ครบ 7 วัน (ตอนนี้มี {len(closes)} วัน)")

    if ma25 is not None:
        status25 = "อยู่ **เหนือ** MA25 📈" if current > ma25 else (
            "อยู่ **ต่ำกว่า** MA25 📉" if current < ma25 else "อยู่ที่ MA25 พอดี ➖"
        )
        lines.append(f"MA25 (ค่าเฉลี่ย 25 วัน): **฿{ma25:,.2f}** — ราคาปัจจุบัน{status25}")
    else:
        lines.append(f"MA25: ⚠️ ข้อมูลยังไม่ครบ 25 วัน (ตอนนี้มี {len(closes)} วัน)")

    lines.append(
        "\n📌 ข้อมูลนี้เป็นสถิติเชิงพรรณนาเพื่อประกอบการตัดสินใจเท่านั้น "
        "ไม่ใช่คำแนะนำให้ซื้อหรือขาย"
    )

    return "\n".join(lines)

def make_price_chart(history, since: datetime, title: str):
    points = [
        h for h in history
        if datetime.fromisoformat(h["ts"]) >= since
    ]

    if len(points) < 2:
        return None

    points.sort(key=lambda h: h["ts"])

    times = [datetime.fromisoformat(p["ts"]) for p in points]
    prices = [p["gold_sell"] for p in points]

    fig, ax = plt.subplots(figsize=(9, 4.5), dpi=140)

    ax.plot(times, prices, color="#FFB400", linewidth=2)
    ax.fill_between(times, prices, min(prices), color="#FFB400", alpha=0.1)

    ax.set_title(title, fontsize=13, fontweight="bold")
    ax.set_ylabel("ราคาทองแท่งขายออก (บาท)")
    ax.grid(True, alpha=0.25)

    ax.xaxis.set_major_formatter(mdates.DateFormatter("%d/%m %H:%M"))
    fig.autofmt_xdate()
    fig.tight_layout()

    chart_path = "gold_chart.png"
    fig.savefig(chart_path)
    plt.close(fig)

    return chart_path

def format_trend_message(title: str, trend: dict) -> str:
    if trend["change"] > 0:
        icon = "📈"
        direction = "ขาขึ้น"
    elif trend["change"] < 0:
        icon = "📉"
        direction = "ขาลง"
    else:
        icon = "➖"
        direction = "ทรงตัว"

    return (
        f"{icon} **{title}**\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        f"แนวโน้ม: **{direction}**\n\n"
        f"💰 ราคาเปิดช่วง: **฿{trend['open']:,.2f}**\n"
        f"💰 ราคาปัจจุบัน: **฿{trend['current']:,.2f}**\n\n"
        f"🔺 สูงสุด: **฿{trend['high']:,.2f}**\n"
        f"🔻 ต่ำสุด: **฿{trend['low']:,.2f}**\n\n"
        f"📊 เปลี่ยนแปลง: **{trend['change']:+,.2f} บาท "
        f"({trend['pct']:+.2f}%)**\n\n"
        f"🧮 จำนวนจุดข้อมูล: {trend['count']} จุด\n"
        "📌 อ้างอิงราคาทองแท่งขายออก (96.5%)"
    )

# ==========================================
# บอทออนไลน์
# ==========================================

@bot.event
async def on_ready():
    await bot.tree.sync()
    print("==============================")
    print("บอทออนไลน์แล้ว:", bot.user)
    print("==============================")

    if not check_gold.is_running():
        check_gold.start()

# ==========================================
# คำสั่ง SLASH COMMANDS (/gold, /trend, /ma, /chart)
# ==========================================

@bot.tree.command(name="gold", description="ดูราคาทองไทยและราคาทองโลก")
async def gold(interaction: discord.Interaction):
    await interaction.response.defer()
    try:
        (
            gold_buy, gold_sell, jewelry_buy, jewelry_sell, gold_spot, usd_thb
        ) = get_gold_price()

        message = (
            "🪙 **GOLD MARKET**\n"
            "━━━━━━━━━━━━━━━━━━\n\n"
            "🇹🇭 **ทองไทย 96.5%**\n"
            f"🟢 รับซื้อทองแท่ง: **฿{gold_buy:,.2f}**\n"
            f"🔴 ขายออกทองแท่ง: **฿{gold_sell:,.2f}**\n\n"
            "💍 **ทองรูปพรรณ 96.5%**\n"
            f"🟢 รับซื้อ: **฿{jewelry_buy:,.2f}**\n"
            f"🔴 ขายออก: **฿{jewelry_sell:,.2f}**\n\n"
            "🌎 **Gold Spot**\n"
            f"XAU/USD: **{format_gold_spot(gold_spot)}**\n\n"
            "💵 **Baht / USD**\n"
            f"**{format_usd_thb(usd_thb)}**\n\n"
            "📌 แหล่งข้อมูล: สมาคมค้าทองคำ"
        )
        await interaction.followup.send(message)
    except Exception as e:
        print("GOLD ERROR:", e)
        await interaction.followup.send("❌ ดึงราคาทองไม่สำเร็จ")

@bot.tree.command(name="trend", description="ดูแนวโน้มราคาทอง (รายวัน / ระยะสั้น / ระยะยาว)")
@app_commands.describe(range="เลือกช่วงเวลาที่ต้องการวิเคราะห์")
@app_commands.choices(range=[
    app_commands.Choice(name="รายวัน (Day Trade - วันนี้)", value="day"),
    app_commands.Choice(name="ระยะสั้น (7 วันล่าสุด)", value="short"),
    app_commands.Choice(name="ระยะยาว (30 วันล่าสุด)", value="long"),
])
async def trend(interaction: discord.Interaction, range: app_commands.Choice[str]):
    await interaction.response.defer()
    history = load_history()
    now = datetime.now(TZ)

    if range.value == "day":
        since = now.replace(hour=0, minute=0, second=0, microsecond=0)
        title = "แนวโน้มรายวัน (Day Trade)"
    elif range.value == "short":
        since = now - timedelta(days=7)
        title = "แนวโน้มระยะสั้น (7 วันล่าสุด)"
    else:
        since = now - timedelta(days=30)
        title = "แนวโน้มระยะยาว (30 วันล่าสุด)"

    trend_data = get_trend(history, since)

    if trend_data is None:
        await interaction.followup.send("⚠️ ข้อมูลย้อนหลังยังไม่พอสำหรับช่วงเวลานี้")
        return

    message = format_trend_message(title, trend_data)
    await interaction.followup.send(message)

@bot.tree.command(name="ma", description="ดูเส้นค่าเฉลี่ยราคาทอง MA7 / MA25")
async def ma(interaction: discord.Interaction):
    await interaction.response.defer()
    history = load_history()
    daily_closes = get_daily_closes(history)

    if len(daily_closes) < 2:
        await interaction.followup.send("⚠️ ข้อมูลย้อนหลังยังไม่พอสำหรับคำนวณ MA")
        return

    message = format_ma_message(daily_closes)
    await interaction.followup.send(message)

@bot.tree.command(name="chart", description="ดูกราฟราคาทอง")
@app_commands.describe(range="เลือกช่วงเวลาที่ต้องการดูกราฟ")
@app_commands.choices(range=[
    app_commands.Choice(name="รายวัน (Day Trade - วันนี้)", value="day"),
    app_commands.Choice(name="ระยะสั้น (7 วันล่าสุด)", value="short"),
    app_commands.Choice(name="ระยะยาว (30 วันล่าสุด)", value="long"),
])
async def chart(interaction: discord.Interaction, range: app_commands.Choice[str]):
    await interaction.response.defer()
    history = load_history()
    now = datetime.now(TZ)

    if range.value == "day":
        since = now.replace(hour=0, minute=0, second=0, microsecond=0)
        title = "กราฟราคาทองแท่งขายออก - รายวัน"
    elif range.value == "short":
        since = now - timedelta(days=7)
        title = "กราฟราคาทองแท่งขายออก - 7 วันล่าสุด"
    else:
        since = now - timedelta(days=30)
        title = "กราฟราคาทองแท่งขายออก - 30 วันล่าสุด"

    chart_path = make_price_chart(history, since, title)

    if chart_path is None:
        await interaction.followup.send("⚠️ ข้อมูลย้อนหลังยังไม่พอสำหรับช่วงเวลานี้")
        return

    await interaction.followup.send(file=discord.File(chart_path))

    try:
        os.remove(chart_path)
    except Exception:
        pass

# ==========================================
# ระบบตรวจราคาทุก 1 นาที + แจ้งเตือนสะสมทุก 50 บาท
# ==========================================

@tasks.loop(minutes=1)
async def check_gold():
    global last_alert_price

    try:
        (
            gold_buy, gold_sell, jewelry_buy, jewelry_sell, gold_spot, usd_thb
        ) = get_gold_price()

        print("ราคาทองแท่งขายออก:", gold_sell)

        append_history(
            gold_buy, gold_sell, jewelry_buy, jewelry_sell, gold_spot, usd_thb
        )

        if last_alert_price is None:
            last_alert_price = gold_sell
            print("บันทึกราคาเริ่มต้น:", gold_sell)
            return

        difference = gold_sell - last_alert_price

        if abs(difference) >= ALERT_THRESHOLD:
            icon = "📈" if difference > 0 else "📉"
            direction = "เพิ่มขึ้น" if difference > 0 else "ลดลง"

            message = (
                "🔔 **GOLD ALERT**\n"
                "━━━━━━━━━━━━━━━━━━\n\n"
                f"{icon} ราคาทองแท่งขายออก **{direction}** เกิน {ALERT_THRESHOLD} บาท\n\n"
                f"💰 ราคาก่อนหน้า: **฿{last_alert_price:,.2f}**\n"
                f"💰 ราคาปัจจุบัน: **฿{gold_sell:,.2f}**\n"
                f"📊 เปลี่ยนแปลงสะสม: **{difference:+,.2f} บาท**\n\n"
                f"🌎 Gold Spot: **{format_gold_spot(gold_spot)}**\n"
                f"💵 Baht/USD: **{format_usd_thb(usd_thb)}**\n\n"
                "📌 สมาคมค้าทองคำ"
            )

            if ALERT_CHANNEL_ID != 0:
                channel = bot.get_channel(ALERT_CHANNEL_ID)
                if channel is None:
                    try:
                        channel = await bot.fetch_channel(ALERT_CHANNEL_ID)
                    except Exception as e:
                        print("ไม่พบห้องแจ้งเตือน:", e)

                if channel:
                    await channel.send(message)
                    print("ส่งแจ้งเตือนเรียบร้อย")

            last_alert_price = gold_sell

    except Exception as e:
        print("CHECK ERROR:", e)

# ==========================================
# MAIN EXECUTION
# ==========================================

async def main():
    discord_token = os.getenv("DISCORD_TOKEN")
    if not discord_token:
        raise RuntimeError("DISCORD_TOKEN is not configured")

    # รัน Web Server แบบ Async ควบคู่กับ Discord Bot
    await start_web_server()
    await bot.start(discord_token)

if __name__ == "__main__":
    asyncio.run(main())
