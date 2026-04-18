import os
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
import requests
import discord
import asyncio
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

SKIN_TO_CASE = {}
for case_name, skins in CASE_SKINS.items():
    for skin in skins:
        SKIN_TO_CASE[skin] = case_name

# -------------------------
# CONFIG
# -------------------------

TOKEN        = os.getenv("DISCORD_TOKEN")
GEMINI_KEY   = os.getenv("GEMINI_API_KEY")
MONGO_URI    = os.getenv("MONGO_URI")   # mongodb+srv://user:pass@cluster.mongodb.net/
CHANNEL_ID   = 1487500804532207699

CHECK_INTERVAL      = 600
STEAM_REQUEST_DELAY = 3.0
CACHE_TTL           = 300

CASE_RISE_THRESHOLD = 0.08

SIGNAL_STRONG_MAX = 0.01
SIGNAL_MEDIUM_MAX = 0.03
SIGNAL_WEAK_MAX   = 0.05

TRACKING_DAYS    = 8
TRACKING_SECONDS = TRACKING_DAYS * 86400

STEAM_APP_ID   = 730
STEAM_CURRENCY = 3

MAX_HISTORY_POINTS = 50
MIN_HISTORY_POINTS = 3

# =====================================================================
# MONGODB RETEG
# =====================================================================
mongo_client = None
mongo_db     = None
mongo_ok     = False

def init_mongodb():
    global mongo_client, mongo_db, mongo_ok
    if not MONGO_URI:
        print("[MONGO] MONGO_URI nincs beallitva - in-memory mod.")
        return
    try:
        from pymongo import MongoClient, ASCENDING
        mongo_client = MongoClient(
            MONGO_URI,
            serverSelectionTimeoutMS=5000,
            connectTimeoutMS=5000,
            socketTimeoutMS=10000,
        )
        mongo_client.admin.command("ping")
        mongo_db = mongo_client["cs2bot"]
        ph = mongo_db["price_history"]
        ph.create_index([("name", ASCENDING), ("ts", ASCENDING)], background=True)
        mongo_db["manual_tracking"].create_index(
            [("skin", ASCENDING)], unique=True, background=True
        )
        mongo_ok = True
        print("[MONGO] Kapcsolat OK - cs2bot adatbazis")
    except Exception as e:
        print(f"[MONGO] Kapcsolat SIKERTELEN: {e}")
        print("[MONGO] In-memory modban futunk.")
        mongo_ok = False

def mongo_insert_price(name, price, ts):
    if not mongo_ok:
        return
    try:
        cutoff = ts - 600
        if mongo_db["price_history"].find_one({"name": name, "ts": {"$gte": cutoff}}, {"_id": 1}):
            return
        from datetime import datetime, timezone
        mongo_db["price_history"].insert_one({
            "name": name, "ts": ts, "price": price,
            "date": datetime.fromtimestamp(ts, tz=timezone.utc)
        })
    except Exception as e:
        print(f"[MONGO] insert_price hiba ({name}): {e}")

def mongo_load_history(name, days=7):
    if not mongo_ok:
        return []
    try:
        cutoff = time.time() - days * 86400
        from pymongo import ASCENDING
        return list(mongo_db["price_history"].find(
            {"name": name, "ts": {"$gte": cutoff}},
            {"_id": 0, "ts": 1, "price": 1}
        ).sort("ts", ASCENDING))
    except Exception as e:
        print(f"[MONGO] load_history hiba ({name}): {e}")
        return []

def mongo_load_history_into_memory():
    if not mongo_ok:
        return 0
    total = 0
    try:
        all_names = list(SKIN_TO_CASE.keys()) + ALL_CASES
        for name in all_names:
            rows = mongo_load_history(name, days=7)
            if rows:
                price_history[name] = rows
                total += len(rows)
        print(f"[MONGO] {total} ar-pont betoltve.")
    except Exception as e:
        print(f"[MONGO] load_into_memory hiba: {e}")
    return total

def mongo_save_tracking(skin, data):
    if not mongo_ok:
        return
    try:
        mongo_db["manual_tracking"].update_one(
            {"skin": skin}, {"$set": {**data, "skin": skin}}, upsert=True
        )
    except Exception as e:
        print(f"[MONGO] save_tracking hiba ({skin}): {e}")

def mongo_delete_tracking(skin):
    if not mongo_ok:
        return
    try:
        mongo_db["manual_tracking"].delete_one({"skin": skin})
    except Exception as e:
        print(f"[MONGO] delete_tracking hiba ({skin}): {e}")

def mongo_load_all_tracking():
    if not mongo_ok:
        return {}
    try:
        result = {}
        for doc in mongo_db["manual_tracking"].find({}, {"_id": 0}):
            skin = doc.pop("skin")
            result[skin] = doc
        return result
    except Exception as e:
        print(f"[MONGO] load_all_tracking hiba: {e}")
        return {}

def mongo_get_stats():
    if not mongo_ok:
        return None
    try:
        from pymongo import ASCENDING, DESCENDING
        ph_count     = mongo_db["price_history"].count_documents({})
        mt_count     = mongo_db["manual_tracking"].count_documents({})
        oldest       = mongo_db["price_history"].find_one({}, {"ts": 1, "_id": 0}, sort=[("ts", ASCENDING)])
        newest       = mongo_db["price_history"].find_one({}, {"ts": 1, "_id": 0}, sort=[("ts", DESCENDING)])
        unique_items = len(mongo_db["price_history"].distinct("name"))
        return {
            "total_points":  ph_count,
            "unique_items":  unique_items,
            "tracked_skins": mt_count,
            "oldest_ts":     oldest["ts"] if oldest else None,
            "newest_ts":     newest["ts"] if newest else None,
        }
    except Exception as e:
        print(f"[MONGO] get_stats hiba: {e}")
        return None

# =====================================================================

executor = ThreadPoolExecutor(max_workers=2)

intents = discord.Intents.default()
intents.message_content = True
client  = discord.Client(intents=intents)
tree    = discord.app_commands.CommandTree(client)

# -------------------------
# TRACKING
# -------------------------

last_heartbeat  = 0.0
previous_prices = {}
price_cache     = {}
cache_ts        = {}
manual_tracking = {}
loop_started    = False
price_history   = defaultdict(list)

# -------------------------
# AR HISTORIA
# -------------------------

def record_price(name, price):
    ts = time.time()
    price_history[name].append({"ts": ts, "price": price})
    if len(price_history[name]) > MAX_HISTORY_POINTS * 2:
        price_history[name] = price_history[name][-MAX_HISTORY_POINTS:]
    # MongoDB-be is menti hatter-szalon (nem lassitja a fo ciklust)
    threading.Thread(target=mongo_insert_price, args=(name, price, ts), daemon=True).start()

def get_dynamic_history(name):
    history = price_history.get(name, [])
    if len(history) == 0:
        return []
    if len(history) <= MAX_HISTORY_POINTS:
        return history

    keep_first = 5
    keep_last  = 20
    keep_mid   = MAX_HISTORY_POINTS - keep_first - keep_last

    first_part = history[:keep_first]
    last_part  = history[-keep_last:]
    mid_part   = history[keep_first:-keep_last]

    if keep_mid > 0 and len(mid_part) > 0:
        step = max(1, len(mid_part) // keep_mid)
        mid_sampled = mid_part[::step][:keep_mid]
    else:
        mid_sampled = []

    return first_part + mid_sampled + last_part

def format_history_for_ai(name):
    history = get_dynamic_history(name)
    if not history:
        return None

    lines = []
    for entry in history:
        ts_str = time.strftime("%Y-%m-%d %H:%M", time.localtime(entry["ts"]))
        lines.append(f"  {ts_str} -> {entry['price']:.2f} EUR")

    prices       = [e["price"] for e in history]
    min_p        = min(prices)
    max_p        = max(prices)
    avg_p        = sum(prices) / len(prices)
    first        = prices[0]
    last         = prices[-1]
    total_change = ((last - first) / first * 100) if first > 0 else 0
    volatility   = (max_p - min_p) / avg_p * 100 if avg_p > 0 else 0

    summary = (
        f"Mérések száma: {len(history)}\n"
        f"Legalacsonyabb ár: {min_p:.2f} EUR\n"
        f"Legmagasabb ár: {max_p:.2f} EUR\n"
        f"Átlagár: {avg_p:.2f} EUR\n"
        f"Első mért ár: {first:.2f} EUR\n"
        f"Jelenlegi ár: {last:.2f} EUR\n"
        f"Teljes változás: {total_change:+.2f}%\n"
        f"Volatilitás (max-min/avg): {volatility:.2f}%\n"
    )

    return summary, "\n".join(lines), len(history)

# -------------------------
# PIACI OSSZEFOGLALO (general-hoz)
# -------------------------

def build_market_snapshot():
    """
    Összegyűjti az összes ládáról és skinről elérhető trend adatot.
    Visszaad egy szöveges összefoglalót az AI számára,
    és egy rendezett listát a legjobb vételi lehetőségekről.
    """
    case_summaries  = []
    skin_candidates = []   # (skin_neve, lada_neve, total_change, volatility, current_price, n_points)

    for case in ALL_CASES:
        hist = format_history_for_ai(case)
        if hist is None or hist[2] < MIN_HISTORY_POINTS:
            continue

        summary, _, n = hist
        prices       = [e["price"] for e in get_dynamic_history(case)]
        first        = prices[0]
        last         = prices[-1]
        total_change = ((last - first) / first * 100) if first > 0 else 0

        case_summaries.append(
            f"LÁDA: {case} | Változás: {total_change:+.2f}% | "
            f"Jelenlegi ár: {last:.2f} EUR | Mérések: {n}"
        )

        # Skineket is megvizsgáljuk
        for skin in CASE_SKINS.get(case, []):
            sh = format_history_for_ai(skin)
            if sh is None or sh[2] < MIN_HISTORY_POINTS:
                continue

            s_summary, _, s_n = sh
            s_prices     = [e["price"] for e in get_dynamic_history(skin)]
            s_first      = s_prices[0]
            s_last       = s_prices[-1]
            s_change     = ((s_last - s_first) / s_first * 100) if s_first > 0 else 0
            s_min        = min(s_prices)
            s_max        = max(s_prices)
            s_avg        = sum(s_prices) / len(s_prices)
            s_volatility = (s_max - s_min) / s_avg * 100 if s_avg > 0 else 0

            skin_candidates.append((skin, case, s_change, s_volatility, s_last, s_n))

    # Rendezés: legjobb vételi lehetőség = láda emelkedett, skin még nem
    # Prioritás: alacsony skin_change (még nem követte a ládát) + alacsony volatilitás
    skin_candidates.sort(key=lambda x: (x[2], x[3]))

    return case_summaries, skin_candidates

def format_general_prompt(case_summaries, skin_candidates):
    """Összeállítja az AI promptot a general elemzéshez."""

    # Láda összefoglaló szekció
    if case_summaries:
        case_section = "LÁDÁK PIACI ÁTTEKINTÉSE:\n" + "\n".join(case_summaries)
    else:
        case_section = "LÁDÁK: Még nincs elég adat."

    # Top 10 skin jelölt az AI-nak (hogy ne legyen túl hosszú a prompt)
    if skin_candidates:
        skin_lines = []
        for skin, case, change, vol, price, n in skin_candidates[:10]:
            skin_lines.append(
                f"  - {skin} (ládából: {case}) | "
                f"Változás: {change:+.2f}% | "
                f"Volatilitás: {vol:.1f}% | "
                f"Jelenlegi ár: {price:.2f} EUR | "
                f"Mérések: {n}"
            )
        skin_section = "SKIN JELÖLTEK (változás szerint rendezve, legkisebb elől):\n" + "\n".join(skin_lines)
    else:
        skin_section = "SKINEK: Még nincs elég adat."

    prompt = (
        f"Az alábbiakban egy CS2 trading bot által gyűjtött VALÓS piaci adatok láthatók.\n"
        f"Kérlek végezz teljes piaci elemzést!\n\n"
        f"{case_section}\n\n"
        f"{skin_section}\n\n"
        f"FELADATOD:\n"
        f"1. PIACI ÖSSZKÉPE: Rövid összefoglalás a jelenlegi CS2 piac állapotáról a fenti adatok alapján.\n"
        f"2. TOP 3 VÉTEL: Melyik 3 skin a legjobb vételi lehetőség MOST és miért? "
        f"(Figyelj a láda vs skin ár különbségre - ha a láda emelkedett de a skin még nem, az a legjobb jel!)\n"
        f"3. KOCKÁZATOS SKINEK: Melyiket kerüljük most és miért?\n"
        f"4. ÁLTALÁNOS TANÁCS: Mit érdemes figyelni a következő napokban?\n\n"
        f"Legyél konkrét, adatalapú és tömör!"
    )

    return prompt

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
        record_price(name, price)
    return price

async def get_price_change(name):
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
# GOOGLE GEMINI AI
# -------------------------

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent"

SYSTEM_PROMPT = (
    "Te egy CS2 Steam Market befektetési elemző bot vagy. "
    "Kizárólag CS2 skinekre és ládákra adsz tanácsot. "
    "Adatalapú, tömör, magyar nyelvű válaszokat adsz. "
    "Soha nem adsz pénzügyi garanciát, mindig kiemeled hogy a piac kiszámíthatatlan."
)

def _call_gemini_sync(prompt, max_tokens=1000):
    try:
        url  = f"{GEMINI_URL}?key={GEMINI_KEY}"
        body = {
            "system_instruction": {
                "parts": [{"text": SYSTEM_PROMPT}]
            },
            "contents": [
                {
                    "role": "user",
                    "parts": [{"text": prompt}]
                }
            ],
            "generationConfig": {
                "maxOutputTokens": max_tokens,
                "temperature": 0.7
            }
        }

        # Exponential backoff: 20s, 40s, 70s
        wait_times = [20, 40, 70]
        for attempt in range(3):
            try:
                res = requests.post(url, json=body, timeout=50)
            except requests.exceptions.Timeout:
                return "⏳ Gemini timeout. Probald ujra!"
            except requests.exceptions.ConnectionError:
                return "❌ Gemini kapcsolat hiba."

            if res.status_code == 200:
                data = res.json()
                candidates = data.get("candidates", [])
                if not candidates:
                    reason = data.get("promptFeedback", {}).get("blockReason", "ismeretlen")
                    print(f"[GEMINI] Ures candidates, ok: {reason}. Valasz: {data}")
                    return f"⚠️ Gemini nem generalt valaszt (ok: {reason}). Probald kesobb!"
                return candidates[0]["content"]["parts"][0]["text"]

            elif res.status_code in (429, 503):
                wait = wait_times[attempt]
                print(f"[GEMINI] {res.status_code} - varakozas {wait}s (proba {attempt+1}/3)")
                time.sleep(wait)
                continue

            elif res.status_code == 400:
                print(f"[GEMINI] 400 bad request: {res.text[:300]}")
                return "❌ Gemini 400 hiba (tul hosszu prompt)."

            else:
                print(f"[GEMINI] {res.status_code}: {res.text[:200]}")
                return f"❌ Gemini {res.status_code} hiba. Probald kesobb!"

        return "⏳ Gemini tulterhelt (429/503). Varj 2-3 percet es probald ujra!"

    except Exception as e:
        print(f"[GEMINI] Kivatel: {type(e).__name__}: {e}")
        return f"❌ Hiba: {type(e).__name__}: {str(e)[:100]}"

async def get_ai_tip(skin_name, current_price):
    hist_result = format_history_for_ai(skin_name)

    case_name            = SKIN_TO_CASE.get(skin_name, "Ismeretlen")
    case_price           = price_cache.get(case_name)
    case_history_result  = format_history_for_ai(case_name) if case_name != "Ismeretlen" else None

    if hist_result is None or hist_result[2] < MIN_HISTORY_POINTS:
        data_points = hist_result[2] if hist_result else 0
        return (
            f"Még nincs elég piaci adat ehhez a skinhez.\n"
            f"Jelenlegi mérések száma: {data_points} (minimum: {MIN_HISTORY_POINTS})\n"
            f"Próbáld meg újra később!"
        )

    summary, price_log, n_points = hist_result

    case_context = ""
    if case_history_result and case_history_result[2] >= MIN_HISTORY_POINTS:
        c_summary, c_log, c_n = case_history_result
        case_context = (
            f"\n\nA LÁDA ADATAI ({case_name}):\n"
            f"{c_summary}\n"
            f"Láda árfolyam log:\n{c_log}"
        )
        if case_price:
            case_context += f"\nLáda jelenlegi ára: {case_price:.2f} EUR"

    prompt = (
        f"Elemezd ezt a CS2 skint befektetési szempontból:\n\n"
        f"SKIN: {skin_name}\n"
        f"Jelenlegi ár: {current_price:.2f} EUR\n"
        f"Ebből a ládából: {case_name}\n\n"
        f"A SKIN ÁRTÖRTÉNETE ({n_points} mérés):\n"
        f"{summary}\n"
        f"Árfolyam log:\n{price_log}"
        f"{case_context}\n\n"
        f"Add meg: Trend elemzés, Kockázat (1-5), Ajánlás (Vétel/Várakozás/Eladás), Indoklás. Max 300 szó."
    )

    loop   = asyncio.get_running_loop()
    answer = await loop.run_in_executor(executor, _call_gemini_sync, prompt, 1000)

    return answer if answer else "Az AI elemzés jelenleg nem elérhető. Próbáld meg később!"

async def get_ai_general():
    """Teljes piaci elemzés az összes gyűjtött adatból."""
    case_summaries, skin_candidates = build_market_snapshot()

    total_items = len(case_summaries) + len(skin_candidates)
    if total_items == 0:
        return (
            "Még nincs elég adat az általános elemzéshez.\n"
            f"A bot minimum {MIN_HISTORY_POINTS} mérést igényel minden egyes tételnél.\n"
            f"Próbáld meg ~30 perc múlva, amikor több adat gyűlt össze!"
        )

    prompt = format_general_prompt(case_summaries, skin_candidates)
    loop   = asyncio.get_running_loop()
    # General elemzéshez több token kell
    answer = await loop.run_in_executor(executor, _call_gemini_sync, prompt, 1500)

    return answer if answer else "Az AI elemzés jelenleg nem elérhető. Próbáld meg később!"

# -------------------------
# SLASH COMMAND: /tip
# -------------------------

@tree.command(name="tip", description="AI elemzés: /tip [skin neve] VAGY /tip general - teljes piaci áttekintés")
@discord.app_commands.describe(nev="Skin neve (pl: AK-47 | Redline (Field-Tested)) VAGY 'general' az általános elemzéshez")
async def tip_command(interaction: discord.Interaction, nev: str):
    await interaction.response.defer()

    if not GEMINI_KEY:
        await interaction.followup.send(
            "Az AI funkció nincs konfigurálva.\n"
            "Be kell állítani a `GEMINI_API_KEY` környezeti változót Render.com-on!\n"
            "API kulcs: https://aistudio.google.com"
        )
        return

    # ----------------------------------
    # /tip general - teljes piaci elemzés
    # ----------------------------------
    if nev.strip().lower() == "general":
        await interaction.followup.send(
            "Teljes piaci elemzés folyamatban...\n"
            "Az AI most átnézi az összes gyűjtött ládát és skint.\n"
            "Ez 10-20 másodpercet vehet igénybe..."
        )

        result = await get_ai_general()

        header   = f"**ÁLTALÁNOS PIACI ELEMZÉS**\n{'─'*40}\n"
        full_msg = header + result

        # Discord 2000 karakter limit kezelése - több üzenetbe darabolás
        if len(full_msg) <= 2000:
            await interaction.channel.send(full_msg)
        else:
            # Első rész header-rel
            await interaction.channel.send(header + result[:1800] + "...")
            # Maradék darabolva
            remaining = result[1800:]
            while remaining:
                chunk    = remaining[:1950]
                remaining = remaining[1950:]
                await interaction.channel.send(chunk)
        return

    # ----------------------------------
    # /tip [skin neve] - egyedi elemzés
    # ----------------------------------
    # Kondíciót levágjuk a kereséshez (pl "AK-47 | Redline (Field-Tested)" -> "AK-47 | Redline")
    COND_LIST = [" (Factory New)", " (Minimal Wear)", " (Field-Tested)", " (Well-Worn)", " (Battle-Scarred)"]
    base_nev = nev
    for c in COND_LIST:
        if nev.endswith(c):
            base_nev = nev[:-len(c)]
            break

    found = base_nev in SKIN_TO_CASE
    if not found:
        for s in SKIN_TO_CASE.keys():
            if s.lower() == base_nev.lower():
                base_nev = s
                found    = True
                break

    # Ha kondícióval adta meg, visszarakjuk
    if found and base_nev != nev:
        nev = nev  # megtartjuk az eredetit kondícióval
    elif found:
        nev = base_nev

    if not found:
        await interaction.followup.send(
            f"Nem találtam ezt a skint: `{nev}`\n"
            f"Ellenőrizd a nevet, vagy írd: `/tip general` az általános elemzéshez!"
        )
        return

    CONDITIONS = [
        "",
        " (Factory New)",
        " (Minimal Wear)",
        " (Field-Tested)",
        " (Well-Worn)",
        " (Battle-Scarred)",
    ]

    price     = None
    used_name = nev

    if any(cond.strip() in nev for cond in CONDITIONS[1:]):
        price     = await get_price(nev)
        used_name = nev
    else:
        for cond in CONDITIONS:
            test_name = nev + cond
            p = await get_price(test_name)
            if p is not None:
                price     = p
                used_name = test_name
                break

    if price is None:
        await interaction.followup.send(
            f"Nem sikerült az árat lekérni: `{nev}`\n"
            f"Próbáld meg a pontos Steam névvel + kondícióval!"
        )
        return

    await interaction.followup.send(
        f"Elemzés folyamatban...\n"
        f"Skin: `{used_name}` | Jelenlegi ár: `{price:.2f} EUR`\n"
        f"Az AI feldolgozza a gyűjtött piaci adatokat..."
    )

    tip      = await get_ai_tip(used_name, price)
    header   = f"**AI ELEMZÉS: {used_name}**\n{'─'*40}\n"
    full_msg = header + tip

    if len(full_msg) <= 2000:
        await interaction.channel.send(full_msg)
    else:
        await interaction.channel.send(header + tip[:1800] + "...")
        await interaction.channel.send("..." + tip[1800:])

# -------------------------
# SLASH COMMAND: /skin
# -------------------------

@tree.command(name="skin", description="8 napig követi a megadott skin árát és jelzi a változást")
@discord.app_commands.describe(nev="A skin neve pontosan, pl: AK-47 | Inheritance (Field-Tested)")
async def skin_command(interaction: discord.Interaction, nev: str):
    await interaction.response.defer()

    found = nev in SKIN_TO_CASE
    if not found:
        for s in SKIN_TO_CASE.keys():
            if s.lower() == nev.lower():
                nev   = s
                found = True
                break

    if not found:
        await interaction.followup.send(
            f"Nem találtam ezt a skint a cases.json-ban: `{nev}`\n"
            f"Ellenőrizd a nevet! (pl: `AK-47 | Inheritance`)",
        )
        return

    CONDITIONS = [
        "",
        " (Factory New)",
        " (Minimal Wear)",
        " (Field-Tested)",
        " (Well-Worn)",
        " (Battle-Scarred)",
    ]

    price     = None
    used_name = nev

    if any(cond.strip() in nev for cond in CONDITIONS[1:]):
        price     = await get_price(nev)
        used_name = nev
    else:
        for cond in CONDITIONS:
            test_name = nev + cond
            p = await get_price(test_name)
            if p is not None:
                price     = p
                used_name = test_name
                break

    if price is None:
        await interaction.followup.send(
            f"Nem sikerült az árat lekérni: `{nev}`\n"
            f"Próbáld meg a pontos Steam névvel kondícióval, pl:\n"
            f"`AK-47 | Inheritance (Field-Tested)`"
        )
        return

    manual_tracking[nev] = {
        "start_price": price,
        "start_time":  time.time(),
        "channel_id":  interaction.channel_id,
        "reported_8d": False,
        "used_name":   used_name
    }

    await interaction.followup.send(
        f"Követés elindult!\n"
        f"Skin: `{used_name}`\n"
        f"Jelenlegi ár: {price} EUR\n"
        f"8 nap múlva küldök visszajelzést a változásról."
    )

# -------------------------
# MANUALIS KOVETES
# -------------------------

async def check_manual_tracking(channel):
    now       = time.time()
    to_delete = []

    for skin, data in list(manual_tracking.items()):
        elapsed = now - data["start_time"]

        if elapsed >= TRACKING_SECONDS and not data.get("reported_8d"):
            query_name    = data.get("used_name", skin)
            current_price = await get_price(query_name)
            if current_price is None:
                continue

            initial = data["start_price"]
            if initial == 0:
                continue

            change = (current_price - initial) / initial
            sign   = "+" if change >= 0 else ""
            irany  = "emelkedett" if change >= 0 else "csökkent"

            try:
                target_channel = await client.fetch_channel(data["channel_id"])
            except Exception:
                target_channel = channel

            await target_channel.send(
                f"8 NAPOS VISSZAJELZÉS\n"
                f"Skin: `{query_name}`\n"
                f"Indulási ár: {round(initial, 2)} EUR\n"
                f"Jelenlegi ár: {round(current_price, 2)} EUR\n"
                f"Az ár {sign}{round(change * 100, 2)}% {irany} a követési időszak alatt."
            )
            manual_tracking[skin]["reported_8d"] = True
            to_delete.append(skin)

    for skin in to_delete:
        manual_tracking.pop(skin, None)

# -------------------------
# FO CIKLUS
# -------------------------

def get_signal_label(skin_change):
    if skin_change <= SIGNAL_STRONG_MAX:
        return "VEDD MEG"
    elif skin_change <= SIGNAL_MEDIUM_MAX:
        return "JO VETEL LEHET"
    elif skin_change <= SIGNAL_WEAK_MAX:
        return "FIGYELD"
    else:
        return None

async def main_loop():
    global last_heartbeat

    channel = await client.fetch_channel(CHANNEL_ID)

    # MongoDB history + tracking visszatöltése
    if mongo_ok:
        await channel.send("🗄️ MongoDB history betöltése...")
        loop_ref = asyncio.get_running_loop()
        pts      = await loop_ref.run_in_executor(executor, mongo_load_history_into_memory)
        await channel.send(f"✅ {pts:,} ár-pont betöltve a MongoDB-ből!")
        saved = mongo_load_all_tracking()
        if saved:
            manual_tracking.update(saved)
            await channel.send(f"📋 {len(saved)} skin-követés visszaállítva.")

    await channel.send("Online! Elemzem a ládákat...")

    # Ládák árai - darabolva ha kell
    msg = "**Ládák aktuális árai:**\n"
    for case in ALL_CASES:
        price = await get_price(case)
        line  = f"{case}: {price:.2f} EUR\n" if price else f"{case}: nem elérhető\n"
        if len(msg) + len(line) > 1900:
            await channel.send(msg)
            msg = ""
        msg += line
    if msg:
        await channel.send(msg)

    # Random 5 skin - kondícióval keres
    import random
    CONDS     = [" (Factory New)", " (Minimal Wear)", " (Field-Tested)", " (Well-Worn)", " (Battle-Scarred)"]
    all_skins = list(SKIN_TO_CASE.keys())
    jeloltek  = random.sample(all_skins, min(20, len(all_skins)))
    skin_lines = []
    for skin in jeloltek:
        if len(skin_lines) >= 5:
            break
        for cond in CONDS:
            p = await get_price(skin + cond)
            if p is not None:
                skin_lines.append(f"{skin}{cond}: {p:.2f} EUR")
                break
    if skin_lines:
        await channel.send("**5 random skin ára:**\n" + "\n".join(skin_lines))

    # Heartbeat beállítása hogy ne szóljon azonnal
    last_heartbeat = time.time()

    while True:
        try:
            current_time = time.time()

            if current_time - last_heartbeat > 3600:
                db_str = "✅ Csatlakozva" if mongo_ok else "❌ In-memory mod (adatok elvesznek ujraindításkor!)"
                await channel.send(f"💓 Bot online! | MongoDB: {db_str}")
                last_heartbeat = current_time

            await check_manual_tracking(channel)

            for case in ALL_CASES:
                case_price, case_change = await get_price_change(case)

                if case_price is None or case_change is None:
                    continue
                if case_change < CASE_RISE_THRESHOLD:
                    continue

                skins = CASE_SKINS.get(case, [])
                for skin in skins:
                    skin_price, skin_change = await get_price_change(skin)

                    if skin_price is None or skin_change is None:
                        continue

                    label = get_signal_label(skin_change)
                    if label is None:
                        continue

                    sign = "+" if skin_change >= 0 else ""
                    await channel.send(
                        f"A \"{case}\"-ban a \"{skin}\" - {label}\n"
                        f"Lada emelkedes: +{round(case_change * 100, 2)}%\n"
                        f"Skin jelenlegi ar: {skin_price} EUR "
                        f"({sign}{round(skin_change * 100, 2)}%)"
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
    init_mongodb()   # MongoDB kapcsolat (szinkron, max 5mp)
    try:
        synced = await tree.sync()
        print(f"Slash commandok szinkronizalva: {len(synced)} db")
    except Exception as e:
        print(f"Slash command sync hiba: {e}")
    if not loop_started:
        loop_started = True
        asyncio.ensure_future(main_loop())
        asyncio.ensure_future(
            news_monitor_loop(
                channel      = await client.fetch_channel(CHANNEL_ID),
                gemini_key   = GEMINI_KEY,
                all_cases    = ALL_CASES,
                skin_to_case = SKIN_TO_CASE,
                executor     = executor,
            )
        )

client.run(TOKEN)
