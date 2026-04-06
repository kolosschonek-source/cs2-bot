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

CHECK_INTERVAL        = 600   # 10 perc
STEAM_REQUEST_DELAY   = 3.0   # Steam rate limit miatt
CSFLOAT_REQUEST_DELAY = 1.5
CACHE_TTL             = 300   # 5 perc

CASE_RISE_THRESHOLD = 0.06
SKIN_FOLLOW_MAX     = 0.04
SELL_THRESHOLD      = 0.001

MAX_CASES          = 15
MAX_SKINS_PER_CASE = 8

STEAM_APP_ID    = 730
STEAM_CURRENCY  = 3   # EUR

executor     = ThreadPoolExecutor(max_workers=2)
client       = discord.Client(intents=discord.Intents.default())

# -------------------------
# TRACKING
# -------------------------

last_heartbeat  = 0.0
buy_signals     = {}
previous_prices = {}
price_cache     = {}
cache_ts        = {}
loop_started    = False

# -------------------------
# STEAM MARKET API (ladakhoz - ingyenes, nem kell API kulcs)
# -------------------------

STEAM_BASE = "https://steamcommunity.com/market/priceoverview/"

def _fetch_steam_price_sync(market_hash_name):
    try:
        time.sleep(STEAM_REQUEST_DELAY)
        params = {
            "appid": STEAM_APP_ID,
            "currency": STEAM_CURRENCY,
            "market_hash_name": market_hash_name
        }
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        }
        res = requests.get(STEAM_BASE, params=params, headers=headers, timeout=15)

        if res.status_code == 429:
            print(f"STEAM RATE LIMIT: {market_hash_name}, varok 60mp-t...")
            time.sleep(60)
            return None
        if res.status_code != 200:
            print(f"STEAM HIBA {res.status_code}: {market_hash_name}")
            return None

        data = res.json()
        if not data.get("success"):
            return None

        lowest = data.get("lowest_price", "") or data.get("median_price", "")
        if not lowest:
            return None

        # Ar kinyerese a stringbol (pl. "1,23 EUR" vagy "1.23EUR")
        cleaned = ""
        for ch in lowest.replace(",", "."):
            if ch.isdigit() or ch == ".":
                cleaned += ch

        parts = cleaned.split(".")
        if len(parts) > 2:
            cleaned = parts[0] + "." + parts[1]

        return round(float(cleaned), 2) if cleaned else None

    except Exception as e:
        print(f"STEAM AR HIBA ({market_hash_name}): {e}")
        return None

# -------------------------
# CSFLOAT API (skinekhez)
# -------------------------

CSFLOAT_BASE = "https://csfloat.com/api/v1"

def _csfloat_headers():
    return {"Authorization": CSFLOAT_API_KEY, "Content-Type": "application/json"}

def _fetch_csfloat_price_sync(market_hash_name):
    try:
        time.sleep(CSFLOAT_REQUEST_DELAY)
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
            time.sleep(60)
            return None
        if res.status_code == 401:
            print("HIBAS CSFLOAT API KULCS!")
            return None
        if res.status_code != 200:
            return None

        listings = res.json().get("data", [])
        if not listings:
            return None

        price_cents = listings[0].get("price")
        return round(price_cents / 100, 2) if price_cents else None

    except Exception as e:
        print(f"CSFLOAT HIBA ({market_hash_name}): {e}")
        return None

# -------------------------
# CACHE + ASYNC WRAPPEREK
# -------------------------

async def get_case_price(name):
    """Lada arat Steam Market-rol kerunk."""
    now = time.time()
    cache_key = f"case_{name}"
    if cache_key in price_cache and (now - cache_ts.get(cache_key, 0)) < CACHE_TTL:
        return price_cache[cache_key]
    loop  = asyncio.get_running_loop()
    price = await loop.run_in_executor(executor, _fetch_steam_price_sync, name)
    if price is not None:
        price_cache[cache_key] = price
        cache_ts[cache_key]    = now
    return price

async def get_skin_price(name):
    """Skin arat CSFloat-rol kerunk, ha nem megy akkor Steam fallback."""
    now = time.time()
    cache_key = f"skin_{name}"
    if cache_key in price_cache and (now - cache_ts.get(cache_key, 0)) < CACHE_TTL:
        return price_cache[cache_key]
    loop  = asyncio.get_running_loop()
    price = await loop.run_in_executor(executor, _fetch_csfloat_price_sync, name)
    if price is None:
        price = await loop.run_in_executor(executor, _fetch_steam_price_sync, name)
    if price is not None:
        price_cache[cache_key] = price
        cache_ts[cache_key]    = now
    return price

async def get_price_change(name, is_case=False):
    current = await get_case_price(name) if is_case else await get_skin_price(name)
    if current is None:
        return None, None
    key = f"{'case' if is_case else 'skin'}_{name}"
    if key not in previous_prices:
        previous_prices[key] = current
        return current, None
    prev = previous_prices[key]
    if prev == 0:
        previous_prices[key] = current
        return current, None
    change = (current - prev) / prev
    previous_prices[key] = current
    return current, change

# -------------------------
# TECHNIKAI ELEMZES
# -------------------------

def analyze_from_single_price(current_price, name, is_case=False):
    key = f"{'case' if is_case else 'skin'}_{name}"
    prev = previous_prices.get(key)
    if prev is None or prev == 0:
        return None
    change = (current_price - prev) / prev
    return {
        "current":   current_price,
        "change_1d": change,
        "change_7d": change,
        "momentum":  0.0,
        "slope":     0.0,
        "spike":     change > 0.15
    }

def score_buy_opportunity(case_a, avg_skin_a):
    score = 0
    if case_a["change_1d"] > 0.05:     score += 20
    if case_a["change_7d"] > 0.08:     score += 20
    if case_a["momentum"]  > 0.05:     score += 15
    if case_a["slope"]     > 0:        score += 10
    if avg_skin_a["change_1d"] < 0.02: score += 20
    if avg_skin_a["change_7d"] < 0.03: score += 15
    return score

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
    to_delete = []
    for item, data in list(buy_signals.items()):
        elapsed       = current_time - data["timestamp"]
        is_case       = data.get("is_case", False)
        current_price = await get_case_price(item) if is_case else await get_skin_price(item)

        if current_price is None:
            continue

        initial = data["initial_price"]
        if initial == 0:
            continue

        change = (current_price - initial) / initial
        irany  = "fel" if change > 0 else "le"
        sign   = "+" if change > 0 else ""

        if elapsed > 86400 and not data.get("reported_24h"):
            buy_signals[item]["reported_24h"] = True
            await channel.send(
                f"24H VISSZAJELZES ({irany})\n`{item}`\n"
                f"Vetel: {round(initial, 2)} EUR -> Most: {round(current_price, 2)} EUR\n"
                f"Eredmeny: {sign}{round(change * 100, 2)}%"
            )
            to_delete.append(item)
        elif elapsed > 21600 and not data.get("reported_6h"):
            buy_signals[item]["reported_6h"] = True
            await channel.send(
                f"6H VISSZAJELZES ({irany})\n`{item}`\n"
                f"Vetel: {round(initial, 2)} EUR -> Most: {round(current_price, 2)} EUR\n"
                f"Valtozas: {sign}{round(change * 100, 2)}%"
            )
        elif elapsed > 3600 and not data.get("reported_1h"):
            buy_signals[item]["reported_1h"] = True
            await channel.send(
                f"1H VISSZAJELZES ({irany})\n`{item}`\n"
                f"Vetel: {round(initial, 2)} EUR -> Most: {round(current_price, 2)} EUR\n"
                f"Valtozas: {sign}{round(change * 100, 2)}%"
            )

    for item in to_delete:
        buy_signals.pop(item, None)

# -------------------------
# FO CIKLUS
# -------------------------

async def main_loop():
    global last_heartbeat

    channel = await client.fetch_channel(CHANNEL_ID)
    await channel.send(
        "CS2 Trading Bot online!\n"
        "Ladak: Steam Market API\n"
        "Skinek: CSFloat API + Steam fallback\n"
        f"{len(ALL_CASES)} lada figyelese aktiv"
    )

    # --- INDULASKORI AR ELLENORZES (csak egyszer) ---
    await channel.send("Ladak aktualis arainak lekerdezese indul (Steam Market)...")
    ar_uzenet = ""
    sikeres = 0
    sikertelen = 0
    for case in ALL_CASES:
        price = await get_case_price(case)
        if price is not None:
            ar_uzenet += f"{case}: {price} EUR\n"
            sikeres += 1
        else:
            ar_uzenet += f"{case}: nem elerheto\n"
            sikertelen += 1
        if len(ar_uzenet) > 1700:
            await channel.send(ar_uzenet)
            ar_uzenet = ""
    if ar_uzenet:
        await channel.send(ar_uzenet)
    await channel.send(
        f"Ar lekerdezes kesz: {sikeres} sikeres, {sikertelen} sikertelen"
    )
    # -------------------------------------------------

    while True:
        try:
            current_time = time.time()

            if current_time - last_heartbeat > 3600:
                await channel.send("Bot online es figyel!")
                last_heartbeat = current_time

            await check_performance(channel, current_time)

            buy_alerts  = []
            sell_alerts = []

            for case in ALL_CASES[:MAX_CASES]:
                case_price, case_live_change = await get_price_change(case, is_case=True)

                if case_price is None:
                    continue

                case_analysis = analyze_from_single_price(case_price, case, is_case=True)
                if not case_analysis:
                    continue

                if case_live_change is not None and case_live_change >= SELL_THRESHOLD:
                    sell_alerts.append(
                        f"{get_sell_priority(case_live_change)} LADA SELL\n`{case}`\n"
                        f"Ar: {case_price} EUR | +{round(case_live_change * 100, 2)}%"
                    )

                skins         = CASE_SKINS.get(case, [])
                skin_analyses = []

                for skin in skins[:MAX_SKINS_PER_CASE]:
                    skin_price, skin_live = await get_price_change(skin, is_case=False)

                    if skin_price is not None:
                        analysis = analyze_from_single_price(skin_price, skin, is_case=False)
                        if analysis:
                            skin_analyses.append((skin, analysis))
                        if skin_live is not None and skin_live >= SELL_THRESHOLD:
                            sell_alerts.append(
                                f"{get_sell_priority(skin_live)} SKIN SELL\n`{skin}`\n"
                                f"Ar: {skin_price} EUR | +{round(skin_live * 100, 2)}%"
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
                            "is_case":       True,
                        }
                    buy_alerts.append((score, case, case_price, case_analysis, avg_skin))

            for alert in sell_alerts:
                await channel.send(alert)

            buy_alerts.sort(key=lambda x: x[0], reverse=True)
            for score, case, price, ca, sa in buy_alerts:
                await channel.send(
                    f"{get_priority(score)}\n`{case}`\n"
                    f"Ar: {price} EUR\n"
                    f"Lada: 1 nap +{round(ca['change_1d'] * 100, 1)}%\n"
                    f"Skinek: 1 nap {round(sa['change_1d'] * 100, 1)}%\n"
                    f"Pontszam: {score}/100"
                )

            if not sell_alerts and not buy_alerts:
                print("Nincs alert ebben a ciklusban.")

            await asyncio.sleep(CHECK_INTERVAL)

        except Exception as e:
            print(f"HIBA: {e}")
            try:
                await channel.send(f"Hiba: `{e}`")
            except Exception:
                pass
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
        asyncio.ensure_future(main_loop())

client.run(TOKEN)
