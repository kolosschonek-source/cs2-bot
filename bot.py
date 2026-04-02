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

# -----------------------------
# HTTP SZERVER (Render + UptimeRobot)
# -----------------------------
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")

    def do_HEAD(self):  # Ezt add hozzá!
        self.send_response(200)
        self.end_headers()

    def log_message(self, format, *args):
        pass

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
TOKEN             = os.getenv("DISCORD_TOKEN")
CSFLOAT_API_KEY   = os.getenv("CSFLOAT_API_KEY")
CHANNEL_ID        = 1487500804532207699

CHECK_INTERVAL    = 600    # 10 perc ciklusonként
REQUEST_DELAY     = 1.5    # Delay kérések között
CACHE_TTL         = 300    # 5 perc cache

# Küszöbök
CASE_RISE_THRESHOLD  = 0.001  # 8%+ láda árnövekedés
SKIN_FOLLOW_MAX      = 0.99   # Skinek max 3% növekedés
SELL_THRESHOLD       = 0.001   # 12%+ = sell alert

MAX_CASES            = 15
MAX_SKINS_PER_CASE   = 8

executor = ThreadPoolExecutor(max_workers=2)
client   = discord.Client(intents=discord.Intents.default())

# -----------------------------
# TRACKING
# -----------------------------
profit_log      = defaultdict(list)
last_heartbeat  = 0
buy_signals     = {}
previous_prices = {}
price_cache     = {}
cache_ts        = {}

# -----------------------------
# CSFLOAT API
# -----------------------------
CSFLOAT_BASE = "https://csfloat.com/api/v1"

def _csfloat_headers():
    return {"Authorization": CSFLOAT_API_KEY, "Content-Type": "application/json"}


def _fetch_price_sync(market_hash_name):
    """Legolcsóbb aktív CSFloat listing ára."""
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
            print(f"RATE LIMIT: {market_hash_name}, várok 60mp-t...")
            time.sleep(60)
            return None
        if res.status_code == 401:
            print("HIBÁS API KULCS!")
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
        print(f"ÁR HIBA ({market_hash_name}): {e}")
        return None


def _fetch_price_history_sync(market_hash_name):
    """CSFloat ártörténet - EUR árak listája."""
    try:
        time.sleep(REQUEST_DELAY)
        url = f"{CSFLOAT_BASE}/market/price-history"
        params = {"market_hash_name": market_hash_name}
        res = requests.get(url, headers=_csfloat_headers(), params=params, timeout=15)

        if res.status_code == 429:
            print(f"RATE LIMIT (history): {market_hash_name}, várok 60mp-t...")
            time.sleep(60)
            return []
        if res.status_code != 200:
            return []

        entries = res.json().get("data", [])
        return [round(e["price"] / 100, 2) for e in entries if "price" in e]

    except Exception as e:
        print(f"ÁR TÖRTÉNET HIBA ({market_hash_name}): {e}")
        return []


# -----------------------------
# TECHNIKAI ELEMZÉS
# -----------------------------
def analyze_trend(prices):
    """Trend elemzés árlista alapján."""
    if len(prices) < 3:
        return None

    prices = np.array(prices)
    current = prices[-1]

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
    """
    Vételi lehetőség pontozása 0-100.
    A láda emelkedik, de a skinek még nem követik = jó vétel.
    """
    score = 0
    if case_a["change_1d"] > 0.05:  score += 20
    if case_a["change_7d"] > 0.08:  score += 20
    if case_a["momentum"]  > 0.05:  score += 15
    if case_a["slope"]     > 0:     score += 10
    if avg_skin_a["change_1d"] < 0.02: score += 20
    if avg_skin_a["change_7d"] < 0.03: score += 15
    return score


# -----------------------------
# CACHE + ASYNC WRAPPEREK
# -----------------------------
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


# -----------------------------
# PRIORITÁS
# -----------------------------
def get_priority(score):
    if score >= 75: return "🔥 ERŐS VÉTEL"
    elif score >= 50: return "⚡ KÖZEPES VÉTEL"
    else: return "ℹ️ GYENGE VÉTEL"

def get_sell_priority(change):
    if change >= 0.25: return "🔥 AZONNALI ELADÁS"
    elif change >= 0.15: return "⚡ ÉRDEMES ELADNI"
    else: return "ℹ️ FIGYELJ RÁ"


# -----------------------------
# PERFORMANCE TRACKING
# -----------------------------
async def check_performance(channel, current_time):
    for item in list(buy_signals.keys()):
        data    = buy_signals[item]
        elapsed = current_time - data["timestamp"]
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
                f"📊 **24H VISSZAJELZÉS** {emoji}\n`{item}`\n"
                f"Vétel: {round(initial,2)}€ → Most: {round(current_price,2)}€\n"
                f"Eredmény: {sign}{round(change*100,2)}%"
            )
            del buy_signals[item]

        elif elapsed > 21600 and not data.get("reported_6h"):
            buy_signals[item]["reported_6h"] = True
            await channel.send(
                f"📊 **6H VISSZAJELZÉS** {emoji}\n`{item}`\n"
                f"Vétel: {round(initial,2)}€ → Most: {round(current_price,2)}€\n"
                f"Változás: {sign}{round(change*100,2)}%"
            )

        elif elapsed > 3600 and not data.get("reported_1h"):
            buy_signals[item]["reported_1h"] = True
            await channel.send(
                f"📊 **1H VISSZAJELZÉS** {emoji}\n`{item}`\n"
                f"Vétel: {round(initial,2)}€ → Most: {round(current_price,2)}€\n"
                f"Változás: {sign}{round(change*100,2)}%"
            )


# -----------------------------
# FŐ BOT LOGIKA
# -----------------------------
@client.event
async def on_ready():
    print("Bot elindult!")
    channel = await client.fetch_channel(CHANNEL_ID)
    await channel.send(
        "✅ **CS2 Trading Bot online!**\n"
        "📡 CSFloat API kapcsolódva\n"
        f"🔍 {len(ALL_CASES)} láda figyelése aktív"
    )

    global last_heartbeat

    while True:
        try:
            current_time = asyncio.get_event_loop().time()
            print("--- ÚJ CIKLUS ---")

            # Heartbeat óránként
            if current_time - last_heartbeat > 3600:
                await channel.send("💓 Bot online és figyel!")
                last_heartbeat = current_time

            # Performance visszajelzések
            await check_performance(channel, current_time)

            buy_alerts  = []
            sell_alerts = []

            for case in ALL_CASES[:MAX_CASES]:
                # Láda ártörténet
                case_history  = await get_price_history(case)
                case_analysis = analyze_trend(case_history)

                if not case_analysis:
                    print(f"Nincs elég adat: {case}")
                    continue

                case_price = case_analysis["current"]
                print(
                    f"Láda: {case} | {case_price}€ | "
                    f"1d: {round(case_analysis['change_1d']*100,1)}% | "
                    f"7d: {round(case_analysis['change_7d']*100,1)}%"
                )

                # Live sell alert a ládára
                _, case_live_change = await get_price_change(case)
                if case_live_change is not None and case_live_change >= SELL_THRESHOLD:
                    sell_alerts.append(
                        f"{get_sell_priority(case_live_change)} **LÁDA SELL**\n`{case}`\n"
                        f"Ár: {case_price}€ | +{round(case_live_change*100,2)}%"
                    )

                # Skinek elemzése
                skins         = CASE_SKINS.get(case, [])
                skin_analyses = []

                for skin in skins[:MAX_SKINS_PER_CASE]:
                    history  = await get_price_history(skin)
                    analysis = analyze_trend(history)
                    if analysis:
                        skin_analyses.append((skin, analysis))

                        # Skin sell alert
                        _, skin_live = await get_price_change(skin)
                        if skin_live is not None and skin_live >= SELL_THRESHOLD:
                            sell_alerts.append(
                                f"{get_sell_priority(skin_live)} **SKIN SELL**\n`{skin}`\n"
                                f"Ár: {analysis['current']}€ | +{round(skin_live*100,2)}%"
                            )

                if not skin_analyses:
                    continue

                avg_skin = {
                    "change_1d": np.mean([a["change_1d"] for _, a in skin_analyses]),
                    "change_7d": np.mean([a["change_7d"] for _, a in skin_analyses]),
                }

                # Vétel pontozás
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

            # Sell alertek
            for alert in sell_alerts:
                await channel.send(alert)

            # Vétel alertek - legjobb először
            buy_alerts.sort(key=lambda x: x[0], reverse=True)
            for score, case, price, ca, sa in buy_alerts:
                await channel.send(
                    f"{get_priority(score)}\n`{case}`\n"
                    f"💰 Ár: **{price}€**\n"
                    f"📈 Láda: 1 nap +{round(ca['change_1d']*100,1)}% | 7 nap +{round(ca['change_7d']*100,1)}%\n"
                    f"📉 Skinek: 1 nap {round(sa['change_1d']*100,1)}% | 7 nap {round(sa['change_7d']*100,1)}%\n"
                    f"⭐ Pontszám: {score}/100"
                )

            if not sell_alerts and not buy_alerts:
                print("Nincs alert ebben a ciklusban.")

            print(f"Ciklus kész. Várok {CHECK_INTERVAL}mp-t...")
            await asyncio.sleep(CHECK_INTERVAL)

        except Exception as e:
            print(f"HIBA: {e}")
            await channel.send(f"⚠️ Hiba: `{e}`")
            await asyncio.sleep(60)

client.run(TOKEN)
