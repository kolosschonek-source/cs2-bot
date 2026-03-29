import requests
import discord
import asyncio
import numpy as np
import json
from collections import defaultdict

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
TOKEN = os.getenv("MTQ4NzUwNTM1OTE1NjU0Nzc5NA.GY2K_D.zqohOs_J3m8ZPQQLe5CQyrWVdOyQxKG5Z7zZew")
CHANNEL_ID = 1487500804532207699
STEAM_ID = "76561199813237489"

CHECK_INTERVAL = 300
THRESHOLD = 0.18
MAX_ITEMS = 15

client = discord.Client(intents=discord.Intents.default())

# -----------------------------
# PROFIT TRACKING
# -----------------------------
profit_log = defaultdict(list)

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
    try:
        data = requests.get(url).json()
        items = set()

        for item in data.get("descriptions", []):
            if "market_hash_name" in item:
                items.add(item["market_hash_name"])

        return list(items)
    except:
        return []

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
    channel = client.get_channel(CHANNEL_ID)

    await channel.send("✅ Bot online és működik!")

    while True:
        try:
            # ---------------- INVENTORY ----------------
            inventory = get_inventory()

            for item in inventory:
                prices = get_price_cached(item)
                res = analyze(prices)
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

                if case_change > THRESHOLD and avg_skin_change < 0.05:
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
