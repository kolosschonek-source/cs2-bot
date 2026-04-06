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

# Skin -> lada mapping (gyors kereseshez)
SKIN_TO_CASE = {}
for case_name, skins in CASE_SKINS.items():
    for skin in skins:
        SKIN_TO_CASE[skin] = case_name

# -------------------------
# CONFIG
# -------------------------

TOKEN      = os.getenv("DISCORD_TOKEN")
CHANNEL_ID = 1487500804532207699

CHECK_INTERVAL      = 600   # 10 perc
STEAM_REQUEST_DELAY = 3.0   # Steam rate limit miatt
CACHE_TTL           = 300   # 5 perc cache

# Lada emelkedes kuszob amitol elkezdjuk nezni a skineket
CASE_RISE_THRESHOLD = 0.08  # 8%

# Vetel ertek hatarok (skin emelkedes alapjan)
SIGNAL_STRONG  = 0.05   # 5%+ emelkedes  -> "JO VETEL LEHET"
SIGNAL_MEDIUM  = 0.02   # 2-5% emelkedes -> "ERDEMES MEGFONTOLNI"
SIGNAL_WEAK    = 0.005  # 0.5-2% emelked -> "FIGYELD"

# 8 napos kovetes ideje masodpercben
TRACKING_DAYS    = 8
TRACKING_SECONDS = TRACKING_DAYS * 86400

STEAM_APP_ID   = 730
STEAM_CURRENCY = 3   # EUR

executor = ThreadPoolExecutor(max_workers=2)

# Discord slash command-okhoz
intents = discord.Intents.default()
intents.message_content = True
client  = discord.Client(intents=intents)
tree    = discord.app_commands.CommandTree(client)

# -------------------------
# TRACKING
# -------------------------

last_heartbeat  = 0.0
previous_prices = {}   # arak az elozo korbol (lada es skin egyutt)
price_cache     = {}   # cache
cache_ts        = {}   # cache timestamp

# Manualis skin kovetes (slash command-bol)
# { "skin_neve": { "start_price": float, "start_time": float, "channel_id": int } }
manual_tracking = {}

loop_started = False

# -------------------------
# STEAM MARKET API
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
            print(f"STEAM RATE LIMIT: {market_hash_name}")
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
# CACHE + ASYNC WRAPPER
# -------------------------

async def get_price(name):
    now = time.time()
    if name in price_cache and (now - cache_ts.get(name, 0)) < CACHE_TTL:
        return price_cache[name]
    loop  = asyncio.get_running_loop()
    price = await loop.run_in_executor(executor, _fetch_steam_price_sync, name)
    if price is not None:
        price_cache[name] = price
        cache_ts[name]    = now
    return price

async def get_price_change(name):
    """Visszaadja az aktualis arat es a valtozast az elozo korhoz kepest."""
    current = await get_price(name)
    if current is None:
        return None, None
    if name not in previous_prices:
        previous_prices[name] = current
        return current, None
    prev = previous_prices[name]
    if prev == 0:
        previous_prices[name] = current
        return current, None
    change = (current - prev) / prev
    previous_prices[name] = current
    return current, change

# -------------------------
# VETEL JELLEMES
# -------------------------

def get_signal_label(change):
    if change >= SIGNAL_STRONG:
        return "VEDD MEG"
    elif change >= SIGNAL_MEDIUM:
        return "JO VETEL LEHET"
    else:
        return "FIGYELD"

# -------------------------
# SLASH COMMAND: /skin
# -------------------------

@tree.command(name="skin", description="8 napig koveti a megadott skin arat es jelzi a valtozast")
@discord.app_commands.describe(nev="A skin neve pontosan, pl: AK-47 | Inheritance")
async def skin_command(interaction: discord.Interaction, nev: str):
    # Ellenorizzuk hogy letezik-e a skin a cases.json-ban
    found = nev in SKIN_TO_CASE
    if not found:
        # Kis- nagybetus elteres kiszurese
        for s in SKIN_TO_CASE.keys():
            if s.lower() == nev.lower():
                nev   = s
                found = True
                break

    if not found:
        await interaction.response.send_message(
            f"Nem talaltam ezt a skint a cases.json-ban: `{nev}`\n"
            f"Ellenorizd a nevet! (pl: `AK-47 | Inheritance`)",
            ephemeral=True
        )
        return

    # Aktualis ar lekeres
    price = await get_price(nev)
    if price is None:
        await interaction.response.send_message(
            f"Nem sikerult az arat lekerni: `{nev}`\nProbald kesobb!",
            ephemeral=True
        )
        return

    # Kovetes inditasa
    manual_tracking[nev] = {
        "start_price":  price,
        "start_time":   time.time(),
        "channel_id":   interaction.channel_id,
        "reported_8d":  False
    }

    await interaction.response.send_message(
        f"Kovetes elindult!\n"
        f"Skin: `{nev}`\n"
        f"Jelenlegi ar: {price} EUR\n"
        f"8 nap mulva kuldok visszajelzest a valtozasrol."
    )

# -------------------------
# MANUALIS KOVETES ELLENORZESE
# -------------------------

async def check_manual_tracking(channel):
    now = time.time()
    to_delete = []

    for skin, data in list(manual_tracking.items()):
        elapsed = now - data["start_time"]

        if elapsed >= TRACKING_SECONDS and not data.get("reported_8d"):
            current_price = await get_price(skin)
            if current_price is None:
                continue

            initial = data["start_price"]
            if initial == 0:
                continue

            change = (current_price - initial) / initial
            sign   = "+" if change >= 0 else ""
            irany  = "emelkedett" if change >= 0 else "csokkent"

            # Uzenet kuldese arra a csatornara ahol a commandot hasznaltak
            try:
                target_channel = await client.fetch_channel(data["channel_id"])
            except Exception:
                target_channel = channel

            await target_channel.send(
                f"8 NAPOS VISSZAJELZES\n"
                f"Skin: `{skin}`\n"
                f"Indulasi ar: {round(initial, 2)} EUR\n"
                f"Jelenlegi ar: {round(current_price, 2)} EUR\n"
                f"Az ar {sign}{round(change * 100, 2)}% {irany} a kovetesi idoszak alatt."
            )
            manual_tracking[skin]["reported_8d"] = True
            to_delete.append(skin)

    for skin in to_delete:
        manual_tracking.pop(skin, None)

# -------------------------
# FO CIKLUS
# -------------------------

async def main_loop():
    global last_heartbeat

    channel = await client.fetch_channel(CHANNEL_ID)
    await channel.send(
        "CS2 Trading Bot online!\n"
        "Ladak es skinek: Steam Market API\n"
        f"{len(ALL_CASES)} lada figyelese aktiv\n"
        "Slash command: /skin [nev] - 8 napos kovetes"
    )

    # --- INDULASKORI AR ELLENORZES ---
    await channel.send("Ladak aktualis arainak lekerdezese indul...")
    ar_uzenet = ""
    sikeres   = 0
    sikertelen = 0
    for case in ALL_CASES:
        price = await get_price(case)
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

            # Heartbeat
            if current_time - last_heartbeat > 3600:
                await channel.send("Bot online es figyel!")
                last_heartbeat = current_time

            # Manualis kovetes ellenorzese
            await check_manual_tracking(channel)

            # ----------------------------------
            # FO LOGIKA: Lada + Skin figyelese
            # ----------------------------------
            for case in ALL_CASES:
                case_price, case_change = await get_price_change(case)

                if case_price is None or case_change is None:
                    # Elso kor, meg nincs mihez hasonlitani
                    continue

                # Ha a lada ara NEM emelkedett 8%-ot, kihagyjuk
                if case_change < CASE_RISE_THRESHOLD:
                    continue

                # Lada 8%+ emelkedett -> megnezzuk a skineket
                skins = CASE_SKINS.get(case, [])
                for skin in skins:
                    skin_price, skin_change = await get_price_change(skin)

                    if skin_price is None or skin_change is None:
                        continue

                    # Csak ha a skin ara is emelkedett (barmennyit)
                    if skin_change <= 0:
                        continue

                    label = get_signal_label(skin_change)
                    await channel.send(
                        f"A \"{case}\"-ban a \"{skin}\" - {label}\n"
                        f"Lada emelkedes: +{round(case_change * 100, 2)}%\n"
                        f"Skin jelenlegi ar: {skin_price} EUR "
                        f"(+{round(skin_change * 100, 2)}%)"
                    )

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
    # Slash commandok szinkronizalasa
    try:
        synced = await tree.sync()
        print(f"Slash commandok szinkronizalva: {len(synced)} db")
    except Exception as e:
        print(f"Slash command sync hiba: {e}")
    if not loop_started:
        loop_started = True
        asyncio.ensure_future(main_loop())

client.run(TOKEN)
