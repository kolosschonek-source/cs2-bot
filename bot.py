import os
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
import requests
import discord
import asyncio
import numpy as np
import json
from collections import defaultdict
import certifi

# -----------------------------
# HTTP SZERVER (Render életben tartásához)
# -----------------------------
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")

    def log_message(self, format, *args):
        pass  # Ne logoljon minden pinget

def run_server():
    port = int(os.getenv("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    server.serve_forever()

threading.Thread(target=run_server, daemon=True).start()

# -----------------------------
# JSON BETÖLTÉS
# -----------------------------
def load_cases():
    with open("cases.json", "r", encoding="utf-8") as f:
        return json.load(f)

CASE_SKINS = load_cases()
ALL_CASES = list(CASE_SKINS.keys())

# -----------------------------
# CONFIG
# -----------------------------
TOKEN = os.getenv("DISCORD_TOKEN")
CHANNEL_ID = 1487500804532207699
STEAM_ID = "76561199813237489"

CHECK_INTERVAL = 300       # 5 percenként fut egy teljes ciklus
THRESHOLD = 0.10           # 10% változás kell vételhez
MAX_CASES = 15             # Hány ládát elemezzen egyszerre
REQUEST_DELAY = 2.5        # Másodperc delay minden Steam kérés között

client = discord.Client(intents=discord.Intents.default())

# -----------------------------
# PROFIT TRACKING
# -----------------------------
profit_log = defaultdict(list)
last_heartbeat = 0
buy_signals = {}
previous_prices = {}

# -----------------------------
# SSL SESSION (certifi tanúsítvánnyal - Render kompatibilis)
# -----------------------------
session = requests.Session()
session.verify = certifi.where()
session.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0 Safari/537.36"
})

# -----------------------------
# STEAM ÁRLEKÉRÉS - BLOKKOLÓ (külön szálon fut majd)
# -----------------------------
def _fetch_price_blocking(name):
    """Ez a függvény blokkoló - mindig run_in_executor-ral hívd meg!"""
    url = "https://steamcommunity.com/market/priceoverview/"
    params = {
        "appid": 730,
        "currency": 3,  # EUR
        "market_hash_name": name
    }
    try:
        time.sleep(REQUEST_DELAY)
        res = session.get(url, params=params, timeout=15)

        if res.status_code == 429:
            print(f"RATE LIMIT: {name}, várok 30mp-t...")
            time.sleep(30)
            return None

        if res.status_code != 200:
            print(f"HTTP HIBA {res.status_code}: {name}")
            return None

        data = res.json()
        if not data.get("success"):
            return None

        raw = data.get("lowest_price") or data.get("median_price")
        if not raw:
            return None

        cleaned = raw.replace("€", "").replace("$", "").replace(",", ".").strip()
        return float(cleaned)

    except Exception as e:
        print(f"ÁR HIBA ({name}): {e}")
        return None

# -----------------------------
# CACHE
# -----------------------------
price_cache = {}
cache_timestamp = {}
CACHE_TTL = 240  # 4 perc

async def get_price_async(name, loop):
    """Async wrapper - nem blokkolja a Discord kapcsolatot!"""
    now = time.time()
    if name in price_cache and (now - cache_timestamp.get(name, 0)) < CACHE_TTL:
        return price_cache[name]

    # Külön szálon futtatjuk a blokkoló hívást
    price = await loop.run_in_executor(None, _fetch_price_blocking, name)

    if price is not None:
        price_cache[name] = price
        cache_timestamp[name] = now

    return price

async def get_price_change_async(name, loop):
    """Visszaadja az aktuális árat és a változást."""
    current = await get_price_async(name, loop)
    if current is None:
        return None, None

    if name not in previous_prices:
        previous_prices[name] = current
        return current, None  # Első mérés

    prev = previous_prices[name]
    change = (current - prev) / prev
    previous_prices[name] = current

    return current, change

# -----------------------------
# INVENTORY LEKÉRÉS - BLOKKOLÓ
# -----------------------------
def _fetch_inventory_blocking():
    url = f"https://steamcommunity.com/inventory/{STEAM_ID}/730/2?l=english&count=5000"
    items = set()
    cursor = None

    try:
        while True:
            final_url = url
            if cursor:
                final_url += f"&start_assetid={cursor}"

            time.sleep(REQUEST_DELAY)
            res = session.get(final_url, timeout=15)

            if res.status_code != 200:
                print(f"INVENTORY HTTP HIBA: {res.status_code}")
                break

            data = res.json()

            desc_map = {}
            for d in data.get("descriptions", []):
                key = f"{d.get('classid')}_{d.get('instanceid')}"
                desc_map[key] = d

            for asset in data.get("assets", []):
                key = f"{asset.get('classid')}_{asset.get('instanceid')}"
                if key in desc_map:
                    name = desc_map[key].get("market_hash_name")
                    if name:
                        items.add(name)

            if not data.get("more_items"):
                break

            cursor = data.get("last_assetid")

        print(f"INVENTORY: {len(items)} item")
        return list(items)

    except Exception as e:
        print(f"INVENTORY HIBA: {e}")
        return []

# -----------------------------
# PRIORITÁS
# -----------------------------
def get_priority(change):
    if change >= 0.25:
        return "🔥 HIGH"
    elif change >= 0.15:
        return "⚡ MEDIUM"
    else:
        return "ℹ️ LOW"

# -----------------------------
# PERFORMANCE TRACKING
# -----------------------------
async def check_performance(channel, current_time, loop):
    for item, data in list(buy_signals.items()):
        elapsed = current_time - data["timestamp"]
        current_price = await get_price_async(item, loop)

        if current_price is None:
            continue

        initial = data["initial_price"]
        change = (current_price - initial) / initial

        if elapsed > 86400 and not data.get("reported_24h"):
            buy_signals[item]["reported_24h"] = True
            await channel.send(
                f"📊 **24H PERFORMANCE**\n`{item}`\n"
                f"Kezdeti: {round(initial, 2)}€ → Most: {round(current_price, 2)}€\n"
                f"Változás: {round(change * 100, 2)}%"
            )
            del buy_signals[item]

        elif elapsed > 21600 and not data.get("reported_6h"):
            buy_signals[item]["reported_6h"] = True
            await channel.send(
                f"📊 **6H PERFORMANCE**\n`{item}`\n"
                f"Kezdeti: {round(initial, 2)}€ → Most: {round(current_price, 2)}€\n"
                f"Változás: {round(change * 100, 2)}%"
            )

        elif elapsed > 3600 and not data.get("reported_1h"):
            buy_signals[item]["reported_1h"] = True
            await channel.send(
                f"📊 **1H PERFORMANCE**\n`{item}`\n"
                f"Kezdeti: {round(initial, 2)}€ → Most: {round(current_price, 2)}€\n"
                f"Változás: {round(change * 100, 2)}%"
            )

# -----------------------------
# FŐ BOT LOGIKA
# -----------------------------
@client.event
async def on_ready():
    print("Bot elindult!")
    channel = await client.fetch_channel(CHANNEL_ID)
    await channel.send("✅ **Bot online és működik!**")

    loop = asyncio.get_event_loop()
    global last_heartbeat

    while True:
        try:
            current_time = loop.time()
            print("--- ÚJ CIKLUS ---")

            # Heartbeat
            if current_time - last_heartbeat > 3600:
                await channel.send("💓 Bot még online és figyel!")
                last_heartbeat = current_time

            # Performance visszajelzések
            await check_performance(channel, current_time, loop)

            # -------- INVENTORY --------
            inventory = await loop.run_in_executor(None, _fetch_inventory_blocking)
            await channel.send(f"📦 Inventory: **{len(inventory)} item** lekérve")

            sell_alerts = []
            for item in inventory:
                current_price, change = await get_price_change_async(item, loop)
                if change is None:
                    continue
                if change > THRESHOLD:
                    priority = get_priority(change)
                    sell_alerts.append(
                        f"{priority} **SELL ALERT**: `{item}`\n"
                        f"Ár: {round(current_price, 2)}€ | +{round(change * 100, 2)}%"
                    )
                    profit_log[item].append(change)

            if sell_alerts:
                for alert in sell_alerts:
                    await channel.send(alert)
            else:
                print("Nincs sell alert.")

            # -------- LÁDA ELEMZÉS --------
            cases_to_check = ALL_CASES[:MAX_CASES]

            for case in cases_to_check:
                case_price, case_change = await get_price_change_async(case, loop)
                if case_change is None:
                    continue

                skins = CASE_SKINS.get(case, [])
                skin_changes = []

                for skin in skins[:8]:
                    _, skin_change = await get_price_change_async(skin, loop)
                    if skin_change is not None:
                        skin_changes.append(skin_change)

                if not skin_changes:
                    continue

                avg_skin_change = np.mean(skin_changes)

                if case_change >= THRESHOLD and avg_skin_change < 0.03:
                    priority = get_priority(case_change)
                    if case not in buy_signals:
                        buy_signals[case] = {
                            "timestamp": current_time,
                            "initial_price": case_price,
                        }
                    await channel.send(
                        f"{priority} **BUY OPPORTUNITY**\n`{case}`\n"
                        f"Láda: {round(case_price, 2)}€ (+{round(case_change * 100, 2)}%)\n"
                        f"Skinek átlag: {round(avg_skin_change * 100, 2)}% → Nem követik!"
                    )

            print(f"Ciklus kész. Várok {CHECK_INTERVAL}mp-t...")
            await asyncio.sleep(CHECK_INTERVAL)

        except Exception as e:
            print(f"HIBA: {e}")
            await channel.send(f"⚠️ Hiba: `{e}`")
            await asyncio.sleep(60)

client.run(TOKEN)
