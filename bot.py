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
# -----------------------------
# HTTP SZERVER (Render életben tartásához)
# -----------------------------
class HealthHandler(BaseHTTPRequestHandler):
def do_GET(self):
self.send_response(200)
self.end_headers()
self.wfile.write(b"OK")
def log_message(self, format, *args):
pass # Ne logoljon minden pinget
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
CHECK_INTERVAL = 300 # 5 percenként fut egy teljes ciklus
THRESHOLD = 0.10 # 10% változás kell vételhez
MAX_CASES = 15 # Hány ládát elemezzen egyszerre
REQUEST_DELAY = 2.0 # Másodperc delay minden Steam kérés között (rate limit elkerülés)
client = discord.Client(intents=discord.Intents.default())
# -----------------------------
# PROFIT TRACKING
# -----------------------------
profit_log = defaultdict(list)
last_heartbeat = 0
buy_signals = {}
# -----------------------------
# STEAM ÁRLEKÉRÉS (priceoverview - nyilvános, nem kell cookie)
# -----------------------------
def get_current_price(name):
"""Jelenlegi árat kér le a Steam piactérről."""
url = "https://steamcommunity.com/market/priceoverview/"
params = {
"appid": 730,
"currency": 3, # 3 = EUR, 1 = USD
"market_hash_name": name
}
try:
time.sleep(REQUEST_DELAY) # Rate limit elkerülés!
res = requests.get(url, params=params, timeout=10)
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
# Legolcsóbb ár feldolgozása (pl. "1,23€" -> 1.23)

raw = data.get("lowest_price") or data.get("median_price")
if not raw:
return None
cleaned = raw.replace("€", "").replace("$", "").replace(",", ".").strip()
return float(cleaned)
except Exception as e:
print(f"ÁR HIBA ({name}): {e}")
return None
# -----------------------------
# CACHE (ne kérje le kétszer ugyanazt egy cikluson belül)
# -----------------------------
price_cache = {}
cache_timestamp = {}
CACHE_TTL = 240 # 4 perc cache élettartam
def get_price_cached(name):
now = time.time()
if name in price_cache and (now - cache_timestamp.get(name, 0)) < CACHE_TTL:
return price_cache[name]
price = get_current_price(name)
if price is not None:
price_cache[name] = price
cache_timestamp[name] = now
return price
# -----------------------------
# KORÁBBI ÁR TÁROLÁS (összevetéshez)
# -----------------------------
previous_prices = {}
def get_price_change(name):
"""
Visszaadja az aktuális árat és a változást az előző ismert árhoz képest.
Ha még nincs előző ár, elmenti és None-t ad vissza.
"""
current = get_price_cached(name)
if current is None:
return None, None
if name not in previous_prices:
previous_prices[name] = current
return current, None # Első mérés, nincs mivel összehasonlítani

prev = previous_prices[name]
change = (current - prev) / prev
previous_prices[name] = current # Frissítés
return current, change
# -----------------------------
# INVENTORY LEKÉRÉS
# -----------------------------
def get_inventory():
url = f"https://steamcommunity.com/inventory/{STEAM_ID}/730/2?l=english&count=5000"
headers = {
"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0 Safari/537.36",
"Accept": "application/json",
}
items = set()
cursor = None
try:
while True:
final_url = url
if cursor:
final_url += f"&start_assetid={cursor}"
time.sleep(REQUEST_DELAY)
res = requests.get(final_url, headers=headers, timeout=10)
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
# PRIORITÁS MEGHATÁROZÁS
# -----------------------------
def get_priority(change):
if change >= 0.25:
return " HIGH"
elif change >= 0.15:
return " MEDIUM"
else:
return " LOW"
# -----------------------------
# PERFORMANCE TRACKING (helyes sorrendben!)
# -----------------------------
async def check_performance(channel, current_time):
for item, data in list(buy_signals.items()):
elapsed = current_time - data["timestamp"]
current_price = get_price_cached(item)
if current_price is None:
continue
initial = data["initial_price"]
change = (current_price - initial) / initial
# FONTOS: nagyobbtól kisebb felé kell ellenőrizni!
if elapsed > 86400 and not data.get("reported_24h"):
buy_signals[item]["reported_24h"] = True
await channel.send(
f" **24H PERFORMANCE**\n`{item}`\n"
f"Kezdeti ár: {round(initial, 2)}€ → Most: {round(current_price, 2)}€\n"
f"Változás: {round(change * 100, 2)}%"
)
elif elapsed > 21600 and not data.get("reported_6h"):

buy_signals[item]["reported_6h"] = True
await channel.send(
f" **6H PERFORMANCE**\n`{item}`\n"
f"Kezdeti ár: {round(initial, 2)}€ → Most: {round(current_price, 2)}€\n"
f"Változás: {round(change * 100, 2)}%"
)
elif elapsed > 3600 and not data.get("reported_1h"):
buy_signals[item]["reported_1h"] = True
await channel.send(
f" **1H PERFORMANCE**\n`{item}`\n"
f"Kezdeti ár: {round(initial, 2)}€ → Most: {round(current_price, 2)}€\n"
f"Változás: {round(change * 100, 2)}%"
)
# Ha már 24h eltelt, töröljük a tracking-ből
if elapsed > 86400 and data.get("reported_24h"):
del buy_signals[item]
# -----------------------------
# FŐ BOT LOGIKA
# -----------------------------
@client.event
async def on_ready():
print("Bot elindult!")
channel = await client.fetch_channel(CHANNEL_ID)
await channel.send(" **Bot online és működik!**")
global last_heartbeat
while True:
try:
current_time = asyncio.get_event_loop().time()
print("--- ÚJ CIKLUS ---")
# Heartbeat minden órában
if current_time - last_heartbeat > 3600:
await channel.send(" Bot még online és figyel!")
last_heartbeat = current_time
# Performance visszajelzések ellenőrzése
await check_performance(channel, current_time)
# -------- INVENTORY ELEMZÉS --------
inventory = get_inventory()
await channel.send(f" Inventory: **{len(inventory)} item** lekérve")

sell_alerts = []
for item in inventory:
current_price, change = get_price_change(item)
if change is None:
continue # Első mérés, nincs összehasonlítási alap
if change > THRESHOLD:
priority = get_priority(change)
sell_alerts.append(
f"{priority} **SELL ALERT**: `{item}`\n"
f"Ár: {round(current_price, 2)}€ | Változás: +{round(change * 100, 2)}%"
)
profit_log[item].append(change)
if sell_alerts:
for alert in sell_alerts:
await channel.send(alert)
else:
print("Nincs sell alert ebben a ciklusban.")
# -------- LÁDA ELEMZÉS --------
cases_to_check = ALL_CASES[:MAX_CASES]
buy_alerts = []
for case in cases_to_check:
case_price, case_change = get_price_change(case)
if case_change is None:
continue
skins = CASE_SKINS.get(case, [])
skin_changes = []
for skin in skins[:8]: # Max 8 skin per láda
skin_price, skin_change = get_price_change(skin)
if skin_change is not None:
skin_changes.append(skin_change)
if not skin_changes:
continue
avg_skin_change = np.mean(skin_changes)
# Vétel logika: láda nőtt 10%+, de a skinek nem követik (max 3%)
if case_change >= THRESHOLD and avg_skin_change < 0.03:

priority = get_priority(case_change)
if case not in buy_signals:
buy_signals[case] = {
"timestamp": current_time,
"initial_price": case_price,
}
buy_alerts.append(
f"{priority} **BUY OPPORTUNITY**\n`{case}`\n"
f"Láda ár: {round(case_price, 2)}€ (+{round(case_change * 100, 2)}%)\n"
f"Skinek átlag változás: {round(avg_skin_change * 100, 2)}% → A skinek NEM követik!"
)
if buy_alerts:
for alert in buy_alerts:
await channel.send(alert)
else:
print("Nincs buy alert ebben a ciklusban.")
print(f"Ciklus kész. Várok {CHECK_INTERVAL} másodpercet...")
await asyncio.sleep(CHECK_INTERVAL)
except Exception as e:
print(f"HIBA A CIKLUSBAN: {e}")
await channel.send(f" Hiba történt: `{e}`")
await asyncio.sleep(60)
client.run(TOKEN)
