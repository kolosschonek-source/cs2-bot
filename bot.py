import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
import requests
import discord
import asyncio
import numpy as np
import json
from collections import defaultdict
def run_server():
    port = int(os.getenv("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), BaseHTTPRequestHandler)
    server.serve_forever()


threading.Thread(target=run_server).start()

# -----------------------------
# JSON LOAD
# -----------------------------
def load_cases():
    with open("cases.json", "r", encoding="utf-8") as f:
        return json.load(f)

CASE_SKINS = load_cases()
ALL_CASES = list(CASE_SKINS.keys())

# -----------------------------
# CONFIG
# -----------------------------
import os
TOKEN = os.getenv("DISCORD_TOKEN")
CHANNEL_ID = 1487500804532207699
STEAM_ID = "76561199813237489"

CHECK_INTERVAL = 300
THRESHOLD = 0.01
MAX_ITEMS = 15

client = discord.Client(intents=discord.Intents.default())

# -----------------------------
# PROFIT TRACKING
# -----------------------------
profit_log = defaultdict(list)
last_heartbeat = 0
buy_signals = {}

# -----------------------------
# STEAM DATA
# -----------------------------
def get_price_history(name):
    url = f"https://steamcommunity.com/market/pricehistory/?appid=730&market_hash_name={name}"
    try:
        res = requests.get(url)
        data = res.json()
        prices = [float(p[1]) for p in data["prices"]]
        return prices
    except:
        return []

# -----------------------------
# CACHE
# -----------------------------
price_cache = {}

def get_price_cached(name):
    if name in price_cache:
        return price_cache[name]

    prices = get_price_history(name)
    price_cache[name] = prices
    return prices

# -----------------------------
# ANALYSIS
# -----------------------------
def analyze(prices):
    if len(prices) < 20:
        return None

    prices = np.array(prices[-90:])

    avg = np.mean(prices)
    current = prices[-1]

    short_avg = np.mean(prices[-5:])
    long_avg = np.mean(prices[-30:])

    momentum = (short_avg - long_avg) / long_avg
    slope = np.polyfit(np.arange(len(prices)), prices, 1)[0]

    change = (current - avg) / avg
    short_change = (current - short_avg) / short_avg

    spike = momentum > 0.10

    return {
        "avg": avg,
        "current": current,
        "change": change,
        "short_change": short_change,
        "momentum": momentum,
        "slope": slope,
        "spike": spike
    }

# -----------------------------
# INVENTORY
# -----------------------------
def get_inventory():
    url = f"https://steamcommunity.com/inventory/{STEAM_ID}/730/2?l=english&count=5000"

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
        "Accept": "application/json",
    }

    items = set()
    cursor = None

    try:
        while True:
            final_url = url
            if cursor:
                final_url += f"&start_assetid={cursor}"

            res = requests.get(final_url, headers=headers, timeout=10)

            if res.status_code != 200:
                print("HTTP ERROR:", res.status_code)
                break

            data = res.json()

            descriptions = data.get("descriptions", [])
            assets = data.get("assets", [])

            desc_map = {}
            for d in descriptions:
                key = f"{d.get('classid')}_{d.get('instanceid')}"
                desc_map[key] = d

            for asset in assets:
                key = f"{asset.get('classid')}_{asset.get('instanceid')}"
                if key in desc_map:
                    name = desc_map[key].get("market_hash_name")
                    if name:
                        items.add(name)

            if not data.get("more_items"):
                break

            cursor = data.get("last_assetid")

        print("TOTAL INVENTORY ITEMS:", len(items))
        return list(items)

    except Exception as e:
        print("INVENTORY ERROR:", e)
        return []
def evaluate_performance(item_name, initial_price, prices):
    if not prices:
        return None

    current_price = prices[-1]

    change = (current_price - initial_price) / initial_price

    return {
        "1h": change,
        "6h": change,
        "24h": change
    }

# -----------------------------
# PRIORITY
# -----------------------------
def get_priority(change, momentum, spike):
    if change > 0.25 or spike:
        return "🔥 HIGH"
    elif change > 0.18:
        return "⚡ MEDIUM"
    else:
        return "ℹ️ LOW"

# -----------------------------
# BOT
# -----------------------------
@client.event
async def on_ready():
    print("Bot elindult!")
    channel = await client.fetch_channel(CHANNEL_ID)

    await channel.send("✅ Bot online és működik!")

    while True:
        try:
            await channel.send("🔁 LOOP ÚJ CIKLUS INDULT")
            print("LOOP RUNNING")
            # ---------------- INVENTORY ----------------
            current_time = asyncio.get_event_loop().time()

            global last_heartbeat
            if current_time - last_heartbeat > 3600:
                await channel.send("💓 Bot még online és figyel!")
                last_heartbeat = current_time

            for item, data in list(buy_signals.items()):
                elapsed = current_time - data["timestamp"]

                if elapsed > 3600:
                    prices = get_price_cached(item)
                    perf = evaluate_performance(item, data["initial_price"], prices)

                    if perf:
                        await channel.send(
                            f"📊 1H PERFORMANCE\n{item}\nChange: {round(perf['1h']*100,2)}%"
                        )

                    del buy_signals[item]

                elif elapsed > 21600:
                    prices = get_price_cached(item)
                    perf = evaluate_performance(item, data["initial_price"], prices)

                    if perf:
                        await channel.send(
                            f"📊 6H PERFORMANCE\n{item}\nChange: {round(perf['6h']*100,2)}%"
                        )

                elif elapsed > 86400:
                    prices = get_price_cached(item)
                    perf = evaluate_performance(item, data["initial_price"], prices)

                    if perf:
                        await channel.send(
                            f"📊 24H PERFORMANCE\n{item}\nChange: {round(perf['24h']*100,2)}%"
                        )
                        
            inventory = get_inventory()
            
            await channel.send(f"📦 Inventory lekérve: {len(inventory)} item")
            print("INVENTORY OK:", len(inventory))

            for item in inventory:
                prices = get_price_cached(item)
                res = analyze(prices)
                print(item, len(prices))
                if not res:
                    continue

                change = res["change"]
                momentum = res["momentum"]
                spike = res["spike"]

                priority = get_priority(change, momentum, spike)

                if change > THRESHOLD:
                    profit_log[item].append(change)

                    await channel.send(
                        f"{priority} SELL ALERT\n{item}\n+{round(change*100,2)}%\nMomentum: {round(momentum,2)}"
                    )

            # ---------------- CASE ANALYSIS ----------------
            cases_to_check = ALL_CASES[:MAX_ITEMS]

            for case in cases_to_check:
                case_prices = get_price_cached(case)
                case_res = analyze(case_prices)

                if not case_res:
                    continue

                case_change = case_res["short_change"]
                case_momentum = case_res["momentum"]

                skins = CASE_SKINS.get(case, [])
                skin_changes = []

                for skin in skins[:10]:
                    prices = get_price_cached(skin)
                    res = analyze(prices)
                    if res:
                        skin_changes.append(res["short_change"])

                if not skin_changes:
                    continue

                avg_skin_change = np.mean(skin_changes)

                priority = get_priority(case_change, case_momentum, case_res["spike"])

                if case_change > THRESHOLD and avg_skin_change < 0.10:
                    buy_signals[case] = {
                        "timestamp": asyncio.get_event_loop().time(),
                        "initial_price": case_res["current"]
                    }
                    
                    await channel.send(
                        f"{priority} BUY OPPORTUNITY\n{case}\nCase: +{round(case_change*100,2)}%\nSkinek nem követik!"
                    )

                elif case_res["spike"]:
                    await channel.send(
                        f"{priority} CASE SPIKE\n{case}"
                    )

            await asyncio.sleep(CHECK_INTERVAL)

        except Exception as e:
            print("Hiba:", e)
            await asyncio.sleep(60)

client.run(TOKEN)

