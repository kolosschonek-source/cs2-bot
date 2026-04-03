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
from concurrent.futures import ThreadPoolExecutor

# -------------------------
# HTTP SZERVER (Render + UptimeRobot)
# -------------------------

class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")

    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()

    def log_message(self, format, *args):
        pass

def run_server():
    port = int(os.getenv("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    server.serve_forever()

threading.Thread(target=run_server, daemon=True).start()

# -------------------------
# JSON BETOLTES
# -------------------------

def load_cases():
    with open("cases.json", "r", encoding="utf-8") as f:
        return json.load(f)

CASE_SKINS = load_cases()
ALL_CASES  = list(CASE_SKINS.keys())

# -------------------------
# CONFIG
# -------------------------

TOKEN           = os.getenv("DISCORD_TOKEN")
CSFLOAT_API_KEY = os.getenv("CSFLOAT_API_KEY")
CHANNEL_ID      = 1487500804532207699

CHECK_INTERVAL       = 600    # 10 perc
REQUEST_DELAY        = 1.5
CACHE_TTL            = 300    # 5 perc

CASE_RISE_THRESHOLD  = 0.001  # TESZT: 0.1% - visszaallitani 0.08-ra elesben!
SKIN_FOLLOW_MAX      = 0.99   # TESZT - visszaallitani 0.03-ra elesben!
SELL_THRESHOLD       = 0.001  # TESZT: 0.1% - visszaallitani 0.12-re elesben!

MAX_CASES          = 15
MAX_SKINS_PER_CASE = 8

executor = ThreadPoolExecutor(max_workers=2)
client   = discord.Client(intents=discord.Intents.default())

# -------------------------
# TRACKING
# -------------------------

profit_log      = defaultdict(list)
last_heartbeat  = 0
buy_signals     = {}
previous_prices = {}
price_cache     = {}
cache_ts        = {}
loop_started    = False

# -------------------------
# CSFLOAT API
# -------------------------

CSFLOAT_BASE = "https://csfloat.com/api/v1"

def _csfloat_headers():
    return {"Authorization": CSFLOAT_API_KEY, "Content-Type": "application/json"}

def _fetch_price_sync(market_hash_name):
    try:
        time.sleep(REQUEST_DELAY)
        url = f"{CSFLOAT_BASE}/listings"
        params = {
            "market_hash_name": market_hash_name,
            "sort_by": "price",
            "order": "asc",
            "limit": 5,
            "category": 0
        }
        res = requests.get(url, headers=_csfloat_headers(), params=params, timeout=15)

        if res.status_code == 429:
            print(f"RATE LIMIT: {market_hash_name}, varok 60mp-t...")
            time.sleep(60)
            return None
        if res.status_code == 401:
            print("HIBAS API KULCS!")
            return None
        if res.status_code != 200:
            print(f"HTTP HIBA {res.status_code}: {market_hash_name}")
            return None

        listings = res.json().get("data", [])
        if not listings:
            return None

        price_cents = listings[0].get("price")
        return round(price_cents / 100, 2) if price_cents else None

    except Exception as e:
        print(f"AR HIBA ({market_hash_name}): {e}")
        return None

def _fetch_price_history_sync(market_hash_name):
    try:
        time.sleep(REQUEST_DELAY)
        url = f"{CSFLOAT_BASE}/market/price-history"
        params = {"market_hash_name": market_hash_name}
        res = requests.get(url, headers=_csfloat_headers(), params=params, timeout=15)

        if res.status_code == 429:
            print(f"RATE LIMIT (history): {market_hash_name}, varok 60mp-t...")
            time.sleep(60)
            return []
        if res.status_code != 200:
            return []

        entries = res.json().get("data", [])
        return [round(e["price"] / 100, 2) for e in entries if "price" in e]

    except Exception as e:
        print(f"AR TORTENET HIBA ({market_hash_name}): {e}")
        return []

# -------------------------
# TECHNIKAI ELEMZES
# -------------------------

def analyze_trend(prices):
    if len(prices) < 3:
        return None

    prices    = np.array(prices)
    current   = prices[-1]
    change_1d = (current - prices[-2]) / prices[-2] if len(prices) >= 2 else 0
    change_7d = (current - prices[-7]) / prices[-7] if len(prices) >= 7 else (current - prices[0]) / prices[0]
    short_avg = np.mean(prices[-3:])
    long_avg  = np.mean(prices[-min(14, len(prices)):])
    momentum  = (short_avg - long_avg) / long_avg if long_avg > 0 else 0
    slope     = np.polyfit(np.arange(len(prices)), prices, 1)[0]
    spike     = change_1d > 0.15 or momentum > 0.12

    return {
        "current":   current,
        "change_1d": change_1d,
        "change_7d": change_7d,
        "momentum":  momentum,
        "slope":     slope,
        "spike":     spike
    }

def score_buy_opportunity(case_a, avg_skin_a):
    score = 0
    if case_a["change_1d"] > 0.05:        score += 20
    if case_a["change_7d"] > 0.08:        score += 20
    if case_a["momentum"]  > 0.05:        score += 15
    if case_a["slope"]     > 0:           score += 10
    if avg_skin_a["change_1d"] < 0.02:    score += 20
    if avg_skin_a["change_7d"] < 0.03:    score += 15
    return score

# -------------------------
# CACHE + ASYNC WRAPPEREK
# -------------------------

async def get_price(name):
    now = time.time()
    if name in price_cache and (now - cache_ts.get(name, 0)) < CACHE_TTL:
        return price_cache[name]
    loop  = asyncio.get_event_loop()
    price = await loop.run_in_executor(executor, _fetch_price_sync, name)
    if price is not None:
        price_cache[name] = price
        cache_ts[name]    = now
    return price

async def get_price_history(name):
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(executor, _fetch_price_history_sync, name)

async def get_price_change(name):
    current = await get_price(name)
    if current is None:
        return None, None
    if name not in previous_prices:
        previous_prices[name] = current
        return current, None
    prev   = previous_prices[name]
    change = (current - prev) / prev
    previous_prices[name] = current
    return current, change

# -------------------------
# PRIORITAS
# -------------------------

def get_priority(score):
    if score >= 75:   return "EROS VETEL"
    elif score >= 50: return "KOZEPES VETEL"
    else:             return "GYENGE VETEL"

def get_sell_priority(change):
    if change >= 0.25:   return "AZONNALI ELADAS"
    elif change >= 0.15: return "ERDEMES ELADNI"
    else:                return "FIGYELJ RA"

# -------------------------
# PERFORMANCE TRACKING
# -------------------------

async def check_performance(channel, current_time):
    for item in list(buy_signals.keys()):
        data          = buy_signals[item]
        elapsed       = current_time - data["timestamp"]
        current_price = await get_price(item)

        if current_price is None:
            continue

        initial = data["initial_price"]
        change  = (current_price - initial) / initial
        emoji   = "📈" if change > 0 else "📉"
        sign    = "+" if change > 0 else ""

        if elapsed > 86400 and not data.get("reported_24h"):
            buy_signals[item]["reported_24h"] = True
            await channel.send(
                f"📊 **24H VISSZAJELZES** {emoji}\n`{item}`\n"
                f"Vetel: {round(initial,2)}EUR -> Most: {round(current_price,2)}EUR\n"
                f"Eredmeny: {sign}{round(change*100,2)}%"
            )
            del buy_signals[item]

        elif elapsed > 21600 and not data.get("reported_6h"):
            buy_signals[item]["reported_6h"] = True
            await channel.send(
                f"📊 **6H VISSZAJELZES** {emoji}\n`{item}`\n"
                f"Vetel: {round(initial,2)}EUR -> Most: {round(current_price,2)}EUR\n"
                f"Valtozas: {sign}{round(change*100,2)}%"
            )

        elif elapsed > 3600 and not data.get("reported_1h"):
            buy_signals[item]["reported_1h"] = True
            await channel.send(
                f"📊 **1H VISSZAJELZES** {emoji}\n`{item}`\n"
                f"Vetel: {round(initial,2)}EUR -> Most: {round(current_price,2)}EUR\n"
                f"Valtozas: {sign}{round(change*100,2)}%"
            )

# -------------------------
# FO CIKLUS
# -------------------------

async def main_loop():
    global last_heartbeat

    channel = await client.fetch_channel(CHANNEL_ID)
    await channel.send(
        "✅ **CS2 Trading Bot online!**\n"
        "📡 CSFloat API kapcsolodva\n"
        f"🔍 {len(ALL_CASES)} lada figyelese aktiv"
    )

    while True:
        try:
            current_time = asyncio.get_event_loop().time()
            print("--- UJ CIKLUS ---")

            if current_time - last_heartbeat > 3600:
                await channel.send("💓 Bot online es figyel!")
                last_heartbeat = current_time

            await check_performance(channel, current_time)

            buy_alerts  = []
            sell_alerts = []

            for case in ALL_CASES[:MAX_CASES]:
                case_history  = await get_price_history(case)
                case_analysis = analyze_trend(case_history)

                if not case_analysis:
                    print(f"Nincs eleg adat: {case}")
                    continue

                case_price = case_analysis["current"]
                print(
                    f"Lada: {case} | {case_price}EUR | "
                    f"1d: {round(case_analysis['change_1d']*100,1)}% | "
                    f"7d: {round(case_analysis['change_7d']*100,1)}%"
                )

                _, case_live_change = await get_price_change(case)
                if case_live_change is not None and case_live_change >= SELL_THRESHOLD:
                    sell_alerts.append(
                        f"{get_sell_priority(case_live_change)} **LADA SELL**\n`{case}`\n"
                        f"Ar: {case_price}EUR | +{round(case_live_change*100,2)}%"
                    )

                skins         = CASE_SKINS.get(case, [])
                skin_analyses = []

                for skin in skins[:MAX_SKINS_PER_CASE]:
                    history  = await get_price_history(skin)
                    analysis = analyze_trend(history)
                    if analysis:
                        skin_analyses.append((skin, analysis))
                        _, skin_live = await get_price_change(skin)
                        if skin_live is not None and skin_live >= SELL_THRESHOLD:
                            sell_alerts.append(
                                f"{get_sell_priority(skin_live)} **SKIN SELL**\n`{skin}`\n"
                                f"Ar: {analysis['current']}EUR | +{round(skin_live*100,2)}%"
                            )

                if not skin_analyses:
                    continue

                avg_skin = {
                    "change_1d": np.mean([a["change_1d"] for _, a in skin_analyses]),
                    "change_7d": np.mean([a["change_7d"] for _, a in skin_analyses]),
                }

                score = score_buy_opportunity(case_analysis, avg_skin)

                if (
                    (case_analysis["change_1d"] >= CASE_RISE_THRESHOLD or
                     case_analysis["change_7d"] >= CASE_RISE_THRESHOLD) and
                    avg_skin["change_1d"] < SKIN_FOLLOW_MAX and
                    score >= 40
                ):
                    if case not in buy_signals:
                        buy_signals[case] = {
                            "timestamp":     current_time,
                            "initial_price": case_price,
                        }
                    buy_alerts.append((score, case, case_price, case_analysis, avg_skin))

            for alert in sell_alerts:
                await channel.send(alert)

            buy_alerts.sort(key=lambda x: x[0], reverse=True)
            for score, case, price, ca, sa in buy_alerts:
                await channel.send(
                    f"{get_priority(score)}\n`{case}`\n"
                    f"💰 Ar: **{price}EUR**\n"
                    f"📈 Lada: 1 nap +{round(ca['change_1d']*100,1)}% | 7 nap +{round(ca['change_7d']*100,1)}%\n"
                    f"📉 Skinek: 1 nap {round(sa['change_1d']*100,1)}% | 7 nap {round(sa['change_7d']*100,1)}%\n"
                    f"⭐ Pontszam: {score}/100"
                )

            if not sell_alerts and not buy_alerts:
                print("Nincs alert ebben a ciklusban.")

            print(f"Ciklus kesz. Varok {CHECK_INTERVAL}mp-t...")
            await asyncio.sleep(CHECK_INTERVAL)

        except Exception as e:
            print(f"HIBA: {e}")
            await channel.send(f"⚠️ Hiba: `{e}`")
            await asyncio.sleep(60)

# -------------------------
# BOT EVENTS
# -------------------------

@client.event
async def on_ready():
    global loop_started
    print(f"Bot bejelentkezve: {client.user}")
    if not loop_started:
        loop_started = True
        asyncio.get_event_loop().create_task(main_loop())

client.run(TOKEN)
