import os
import threading
import time
import random
from http.server import BaseHTTPRequestHandler, HTTPServer
import requests
import discord
import asyncio
import json
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

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
SIGNAL_STRONG_MAX   = 0.01
SIGNAL_MEDIUM_MAX   = 0.03
SIGNAL_WEAK_MAX     = 0.05

TRACKING_DAYS    = 8
TRACKING_SECONDS = TRACKING_DAYS * 86400

STEAM_APP_ID   = 730
STEAM_CURRENCY = 3

MAX_HISTORY_POINTS = 50
MIN_HISTORY_POINTS = 3

# =====================================================================
# MONGODB RETEG
# =====================================================================
#
# Adatbazis: cs2bot
#   price_history : { name, ts, price, date }  index: [name, ts]
#   manual_tracking: { skin, start_price, start_time, channel_id,
#                      reported_8d, used_name }  index: skin (unique)
#
# Mukodes:
#   - record_price() -> in-memory + MongoDB (hatter-szal)
#   - get_history()  -> in-memory ELOSZOR, ha keves adat: MongoDB kiegeszit
#   - Inditaskor: mongo_load_history_into_memory() visszatoltit 7 napot
#   - Ha a MongoDB nem elerheto: bot in-memory modban fut, minden mukodik
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

        # Indexek (ha meg nem leteznek)
        ph = mongo_db["price_history"]
        ph.create_index([("name", ASCENDING), ("ts", ASCENDING)], background=True)
        ph.create_index([("name", ASCENDING), ("date", ASCENDING)], background=True)
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
    """Elmenti az arat MongoDB-be. Duplikat-vedelem: 10 percen belul nem ir ketszer."""
    if not mongo_ok:
        return
    try:
        cutoff = ts - 600
        if mongo_db["price_history"].find_one(
            {"name": name, "ts": {"$gte": cutoff}}, {"_id": 1}
        ):
            return
        mongo_db["price_history"].insert_one({
            "name":  name,
            "ts":    ts,
            "price": price,
            "date":  datetime.fromtimestamp(ts, tz=timezone.utc)
        })
    except Exception as e:
        print(f"[MONGO] insert_price hiba ({name}): {e}")

def mongo_load_history(name, days=30):
    """Betolt egy item arhistoriajat a DB-bol. Visszater: [{"ts":..,"price":..}]"""
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
    """Inditaskor betolti az utolso 7 nap osszes arpontjat az in-memory dict-be."""
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
        print(f"[MONGO] {total} ar-pont betoltve ({len(all_names)} item).")
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
        ph_count = mongo_db["price_history"].count_documents({})
        mt_count = mongo_db["manual_tracking"].count_documents({})
        oldest   = mongo_db["price_history"].find_one(
            {}, {"ts": 1, "_id": 0}, sort=[("ts", ASCENDING)]
        )
        newest   = mongo_db["price_history"].find_one(
            {}, {"ts": 1, "_id": 0}, sort=[("ts", DESCENDING)]
        )
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

# -------------------------
# GEMINI RATE LIMIT MANAGER
# -------------------------

GEMINI_MODEL        = "gemini-2.0-flash"
GEMINI_RPD_LIMIT    = 1400
GEMINI_RPM_LIMIT    = 12
GEMINI_COOLDOWN_SEC = 5

AI_CACHE_TTL      = 1800
ai_response_cache = {}
ai_cache_lock     = threading.Lock()

gemini_stats = {
    "requests_today": 0, "day_reset_ts": 0.0,
    "requests_this_minute": 0, "minute_reset_ts": 0.0,
    "last_request_ts": 0.0, "total_blocked": 0,
}
gemini_lock = threading.Lock()

def _reset_gemini_daily_if_needed():
    today_midnight = datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    ).timestamp()
    with gemini_lock:
        if gemini_stats["day_reset_ts"] < today_midnight:
            gemini_stats["requests_today"] = 0
            gemini_stats["day_reset_ts"]   = today_midnight
            print("[GEMINI] Napi szamlalo resetelve.")

def _check_gemini_rate_limit():
    _reset_gemini_daily_if_needed()
    now = time.time()
    with gemini_lock:
        if gemini_stats["requests_today"] >= GEMINI_RPD_LIMIT:
            remaining = 86400 - (now - gemini_stats["day_reset_ts"])
            h = int(remaining // 3600)
            m = int((remaining % 3600) // 60)
            gemini_stats["total_blocked"] += 1
            return False, f"Napi limit elert! Reset: {h}h {m}m mulva."
        if now - gemini_stats["minute_reset_ts"] >= 60:
            gemini_stats["requests_this_minute"] = 0
            gemini_stats["minute_reset_ts"]      = now
        if gemini_stats["requests_this_minute"] >= GEMINI_RPM_LIMIT:
            wait = 60 - (now - gemini_stats["minute_reset_ts"])
            gemini_stats["total_blocked"] += 1
            return False, f"Tul sok keres/perc. Varj ~{int(wait)+1}s-et!"
        since_last = now - gemini_stats["last_request_ts"]
        if since_last < GEMINI_COOLDOWN_SEC:
            gemini_stats["total_blocked"] += 1
            return False, f"Kicsit gyors vagy! Varj {GEMINI_COOLDOWN_SEC - since_last:.1f}s-et."
        return True, None

def _increment_gemini_counter():
    now = time.time()
    with gemini_lock:
        gemini_stats["requests_today"]      += 1
        gemini_stats["requests_this_minute"] += 1
        gemini_stats["last_request_ts"]      = now

def get_gemini_status():
    _reset_gemini_daily_if_needed()
    with gemini_lock:
        used    = gemini_stats["requests_today"]
        blocked = gemini_stats["total_blocked"]
    return (
        f"**Gemini API statisztika:**\n"
        f"• Felhasznalt ma: `{used}` / `{GEMINI_RPD_LIMIT}`\n"
        f"• Maradek: `{GEMINI_RPD_LIMIT - used}`\n"
        f"• Blokkolt: `{blocked}`\n"
        f"• Reset: UTC ejfelkor"
    )

def get_cached_ai_response(key):
    with ai_cache_lock:
        entry = ai_response_cache.get(key)
        if entry and (time.time() - entry["ts"]) < AI_CACHE_TTL:
            return entry["response"], entry["ts"]
    return None, None

def set_cached_ai_response(key, response):
    with ai_cache_lock:
        ai_response_cache[key] = {"ts": time.time(), "response": response}

# -------------------------
# EXECUTOR + DISCORD
# -------------------------

executor = ThreadPoolExecutor(max_workers=3)
intents  = discord.Intents.default()
intents.message_content = True
client   = discord.Client(intents=intents)
tree     = discord.app_commands.CommandTree(client)

# -------------------------
# IN-MEMORY ALLAPOT
# -------------------------

last_heartbeat  = 0.0
previous_prices = {}
price_cache     = {}
cache_ts        = {}
manual_tracking = {}
loop_started    = False
price_history   = defaultdict(list)

# =====================================================================
# AR HISTORIA - HIBRID RETEG (memory + MongoDB)
# =====================================================================

def record_price(name, price):
    """
    Rogzit egy ar-pontot.
    In-memory: azonnal, szinkron.
    MongoDB: hatter-szalon, nem lassitja a fo ciklust.
    """
    ts    = time.time()
    entry = {"ts": ts, "price": price}

    price_history[name].append(entry)
    if len(price_history[name]) > MAX_HISTORY_POINTS * 2:
        price_history[name] = price_history[name][-MAX_HISTORY_POINTS:]

    threading.Thread(
        target=mongo_insert_price, args=(name, price, ts), daemon=True
    ).start()

def get_history(name, days=30):
    """
    Visszaadja az ar-tortenetet.
    Eloszor in-memory-t nezi, ha keves adat van hosszabb tavhoz -> MongoDB kiegeszit.
    """
    mem_data = list(price_history.get(name, []))

    # Rovid lekeres es van eleg memory adat
    if days <= 1 and len(mem_data) >= MIN_HISTORY_POINTS:
        return sorted(mem_data, key=lambda x: x["ts"])

    if mongo_ok:
        db_data = mongo_load_history(name, days=days)
        if db_data:
            # Duplikat-szures 5 perces blokkokban
            db_blocks = {round(e["ts"] / 300) for e in db_data}
            extra     = [e for e in mem_data if round(e["ts"] / 300) not in db_blocks]
            merged    = db_data + extra
            merged.sort(key=lambda x: x["ts"])
            return merged

    return sorted(mem_data, key=lambda x: x["ts"])

def get_dynamic_history(name, days=14):
    """Max MAX_HISTORY_POINTS pontot ad vissza, egyenletesen eloszolva az idotengely menten."""
    history = get_history(name, days=days)
    if len(history) <= MAX_HISTORY_POINTS:
        return history

    keep_first = 5
    keep_last  = 20
    keep_mid   = MAX_HISTORY_POINTS - keep_first - keep_last
    mid_part   = history[keep_first:-keep_last]
    mid_sampled = (
        mid_part[::max(1, len(mid_part) // keep_mid)][:keep_mid]
        if keep_mid > 0 and mid_part else []
    )
    return history[:keep_first] + mid_sampled + history[-keep_last:]

def format_history_for_ai(name, days=14):
    """Osszeallitja az AI-nak szant szoveges osszefoglalot."""
    history = get_dynamic_history(name, days=days)
    if not history:
        return None

    prices       = [e["price"] for e in history]
    min_p, max_p = min(prices), max(prices)
    avg_p        = sum(prices) / len(prices)
    first, last  = prices[0], prices[-1]
    total_change = ((last - first) / first * 100) if first > 0 else 0
    volatility   = (max_p - min_p) / avg_p * 100 if avg_p > 0 else 0
    span_days    = (history[-1]["ts"] - history[0]["ts"]) / 86400

    lines   = [
        f"  {time.strftime('%Y-%m-%d %H:%M', time.localtime(e['ts']))} -> {e['price']:.2f} EUR"
        for e in history
    ]
    summary = (
        f"Meresek: {len(history)} ({span_days:.1f} nap alatt)\n"
        f"Min: {min_p:.2f} EUR | Max: {max_p:.2f} EUR | Atlag: {avg_p:.2f} EUR\n"
        f"Elso: {first:.2f} EUR | Jelenlegi: {last:.2f} EUR\n"
        f"Valtozas: {total_change:+.2f}% | Volatilitas: {volatility:.2f}%\n"
    )
    return summary, "\n".join(lines), len(history)

# -------------------------
# PIACI OSSZEFOGLALO
# -------------------------

def build_market_snapshot():
    case_summaries  = []
    skin_candidates = []

    for case in ALL_CASES:
        hist = format_history_for_ai(case)
        if hist is None or hist[2] < MIN_HISTORY_POINTS:
            continue
        _, _, n  = hist
        prices   = [e["price"] for e in get_dynamic_history(case)]
        first, last = prices[0], prices[-1]
        change   = ((last - first) / first * 100) if first > 0 else 0
        case_summaries.append(
            f"LADA: {case} | Valtozas: {change:+.2f}% | Ar: {last:.2f} EUR | Meresek: {n}"
        )
        for skin in CASE_SKINS.get(case, []):
            sh = format_history_for_ai(skin)
            if sh is None or sh[2] < MIN_HISTORY_POINTS:
                continue
            _, _, s_n = sh
            sp        = [e["price"] for e in get_dynamic_history(skin)]
            s_change  = ((sp[-1] - sp[0]) / sp[0] * 100) if sp[0] > 0 else 0
            s_avg     = sum(sp) / len(sp)
            s_vol     = (max(sp) - min(sp)) / s_avg * 100 if s_avg > 0 else 0
            skin_candidates.append((skin, case, s_change, s_vol, sp[-1], s_n))

    skin_candidates.sort(key=lambda x: (x[2], x[3]))
    return case_summaries, skin_candidates

def format_general_prompt(case_summaries, skin_candidates):
    case_sec = ("LADAK:\n" + "\n".join(case_summaries)) if case_summaries else "LADAK: Nincs eleg adat."
    if skin_candidates:
        skin_sec = "SKIN JELOLTEK (valtozas szerint):\n" + "\n".join(
            f"  - {s} ({c}) | Valtozas: {ch:+.2f}% | Vol: {v:.1f}% | Ar: {p:.2f} EUR | n={n}"
            for s, c, ch, v, p, n in skin_candidates[:10]
        )
    else:
        skin_sec = "SKINEK: Nincs eleg adat."
    return (
        f"CS2 trading bot valós piaci adatai:\n\n{case_sec}\n\n{skin_sec}\n\n"
        f"FELADATOD:\n"
        f"1. PIACI OSSZKEPE: Rovid osszefoglalas.\n"
        f"2. TOP 3 VETEL: Legjobb vetel MOST es miert?\n"
        f"3. KOCKAZATOS SKINEK: Melyiket keruluk?\n"
        f"4. ALTALANOS TANACS: Mit figyelj a kovetkezo napokban?\n"
        f"Legyel konkret, adatalapu, tomor! Max 400 szo."
    )

# -------------------------
# STEAM MARKET API
# -------------------------

STEAM_BASE = "https://steamcommunity.com/market/priceoverview/"

def _fetch_steam_price_sync(market_hash_name):
    try:
        time.sleep(STEAM_REQUEST_DELAY)
        res = requests.get(
            STEAM_BASE,
            params={"appid": STEAM_APP_ID, "currency": STEAM_CURRENCY,
                    "market_hash_name": market_hash_name},
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
            timeout=15
        )
        if res.status_code == 429:
            time.sleep(60)
            return None
        if res.status_code != 200:
            return None
        data   = res.json()
        if not data.get("success"):
            return None
        lowest = data.get("lowest_price", "") or data.get("median_price", "")
        if not lowest:
            return None
        cleaned = "".join(ch for ch in lowest.replace(",", ".") if ch.isdigit() or ch == ".")
        parts   = cleaned.split(".")
        if len(parts) > 2:
            cleaned = parts[0] + "." + parts[1]
        return round(float(cleaned), 2) if cleaned else None
    except Exception as e:
        print(f"STEAM AR HIBA ({market_hash_name}): {e}")
        return None

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
    if name not in previous_prices or previous_prices[name] == 0:
        previous_prices[name] = current
        return current, None
    change = (current - previous_prices[name]) / previous_prices[name]
    previous_prices[name] = current
    return current, change

# -------------------------
# GEMINI AI - ALAP HIVAS
# -------------------------

GEMINI_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
SYSTEM_PROMPT = (
    "Te egy CS2 Steam Market befektetesi elemzo bot vagy. "
    "Kizarolag CS2 skinekre es ladakra adsz tancsacot. "
    "Adatalapu, tomor, magyar nyelvu valaszokat adsz. "
    "Soha nem adsz penzugyi garantiat, mindig kiemeled hogy a piac kiszamithatatlan. "
    "Valaszaid strukturaltak, emojikkal olvashatok es tomorek."
)

def _call_gemini_sync(prompt, max_tokens=1200):
    if not GEMINI_KEY:
        return "HIBA: GEMINI_API_KEY nincs beallitva!"
    try:
        url  = f"{GEMINI_URL}?key={GEMINI_KEY}"
        body = {
            "system_instruction": {"parts": [{"text": SYSTEM_PROMPT}]},
            "contents":           [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig":   {"maxOutputTokens": max_tokens, "temperature": 0.75}
        }
        for attempt in range(3):
            res = requests.post(url, json=body, timeout=45)
            if res.status_code == 200:
                data = res.json()
                cands = data.get("candidates", [])
                if not cands:
                    reason = data.get("promptFeedback", {}).get("blockReason", "ismeretlen")
                    return f"HIBA: Ures candidates. Ok: {reason}. Valasz: {str(data)[:200]}"
                return cands[0]["content"]["parts"][0]["text"]
            elif res.status_code in (429, 503):
                time.sleep(20 * (attempt + 1) if res.status_code == 429 else 10 * (attempt + 1))
            else:
                return f"HIBA: Gemini {res.status_code}. Valasz: {res.text[:400]}"
        return "HIBA: 3 probalkozas utan sem valaszolt (429/503 loop)."
    except requests.exceptions.Timeout:
        return "HIBA: Gemini timeout (45s)."
    except Exception as e:
        return f"HIBA: {type(e).__name__}: {e}"

# =====================================================================
# 3 TRADER SZEMÉLYISÉG + SCORE
# =====================================================================

HODOR_PROMPT = """Te HODOR vagy, egy ultra-ovatos, hosszutavu CS2 befekteto.
Csak akkor javasolsz vetelre ha:
- Az ar legalabb 3+ merest mutat stabil/emelkedo trendet
- Volatilitas alacsony (kiszamithato mozgas)
- Lada mar emelkedett de a skin meg nem (divergencia)
- Jelenlegi ar a tortenet atlag ALATT van (alul-ertekelt)

Valaszod PONTOSAN ebben a formatumban (semmi mas):
HODOR_VELEMENYE: [VETEL / VARJ / ELAD]
HODOR_INDOKLAS: [max 2 mondat]
HODOR_BIZALMI_SZINT: [1-10]"""

FLIPPER_PROMPT = """Te FLIPPER vagy, egy agressziv, gyors CS2 skin flipper.
Rovid tavu ar-mozgasra jatszol:
- Emelkedo utolso meresek = vetel (momentum)
- Lada emelkedett, skin elmaradt = azonnali vetel
- Ar atlag felett es emelkedik = elad (profit take)

Valaszod PONTOSAN ebben a formatumban (semmi mas):
FLIPPER_VELEMENYE: [VETEL / VARJ / ELAD]
FLIPPER_INDOKLAS: [max 2 mondat]
FLIPPER_BIZALMI_SZINT: [1-10]"""

ANALYST_PROMPT = """Te ANALYST vagy, egy hidegeveru technikai elemzo.
Szamok alapjan dolgozol:
- Volatilitas: >30% kockazatos, <10% stabil
- Trend: elso vs utolso ar
- Support: meresek minimuma = tamasz
- Divergencia: (lada_valtozas - skin_valtozas) -> nagyobb = jobb

Valaszod PONTOSAN ebben a formatumban (semmi mas):
ANALYST_VELEMENYE: [VETEL / VARJ / ELAD]
ANALYST_INDOKLAS: [max 2 mondat]
ANALYST_SCORE: [0-100, ahol 100=tokeletes vetel]"""

def _build_skin_data_block(skin_name, current_price):
    hist = format_history_for_ai(skin_name, days=14)
    if hist is None:
        return None, 0
    summary, price_log, n_points = hist

    case_name   = SKIN_TO_CASE.get(skin_name, "Ismeretlen")
    case_hist   = format_history_for_ai(case_name, days=14) if case_name != "Ismeretlen" else None
    case_block  = ""
    case_change = 0.0

    if case_hist and case_hist[2] >= MIN_HISTORY_POINTS:
        c_summary, _, _ = case_hist
        cp = [e["price"] for e in get_dynamic_history(case_name)]
        case_change = ((cp[-1] - cp[0]) / cp[0] * 100) if cp[0] > 0 else 0
        case_block  = f"\nLADA ({case_name}):\n{c_summary}Lada valtozas: {case_change:+.2f}%"

    sp           = [e["price"] for e in get_dynamic_history(skin_name)]
    skin_change  = ((current_price - sp[0]) / sp[0] * 100) if sp and sp[0] > 0 else 0
    divergencia  = case_change - skin_change

    data_block = (
        f"SKIN: {skin_name} | Lada: {case_name}\n"
        f"Jelenlegi ar: {current_price:.2f} EUR\n"
        f"Skin valtozas: {skin_change:+.2f}% | Divergencia: {divergencia:+.2f}%"
        f"{' (JO JEL: lada megelozi a skint)' if divergencia > 5 else ''}\n\n"
        f"ARTORTENET ({n_points} pont):\n{summary}\nLog:\n{price_log}"
        f"{case_block}"
    )
    return data_block, n_points

def _parse_trader_response(text, trader_name):
    velemeny, indoklas, raw_score = "VARJ", "Nem sikerult elemezni.", 5
    pmap = {
        "HODOR":   ("HODOR_VELEMENYE:", "HODOR_INDOKLAS:", "HODOR_BIZALMI_SZINT:"),
        "FLIPPER": ("FLIPPER_VELEMENYE:", "FLIPPER_INDOKLAS:", "FLIPPER_BIZALMI_SZINT:"),
        "ANALYST": ("ANALYST_VELEMENYE:", "ANALYST_INDOKLAS:", "ANALYST_SCORE:"),
    }
    pv, pi, ps = pmap.get(trader_name, ("", "", ""))
    for line in text.strip().split("\n"):
        line = line.strip()
        if line.startswith(pv):
            val = line[len(pv):].strip().upper()
            velemeny = "VETEL" if "VETEL" in val else ("ELAD" if "ELAD" in val else "VARJ")
        elif line.startswith(pi):
            indoklas = line[len(pi):].strip()
        elif line.startswith(ps):
            try:
                raw_score = int("".join(c for c in line[len(ps):] if c.isdigit())[:3])
                raw_score = max(1, min(10 if trader_name != "ANALYST" else 100, raw_score))
            except Exception:
                pass
    return velemeny, indoklas, raw_score

def _velemeny_to_score(velemeny, raw_score, trader_name):
    if trader_name == "ANALYST":
        return max(0, min(100, raw_score))
    base  = {"VETEL": 60, "VARJ": 30, "ELAD": 0}[velemeny]
    scale = {"VETEL": 40, "VARJ": 20, "ELAD": 20}[velemeny]
    return max(0, min(100, int(base + (raw_score - 1) * scale / 9)))

def _score_to_label(s):
    if s >= 80: return "🟢 EROS VETEL"
    if s >= 60: return "🟡 JO VETEL LEHET"
    if s >= 40: return "🟠 SEMLEGES"
    if s >= 20: return "🔴 VARJ"
    return "⛔ KERULEND"

async def get_ai_tip_full(skin_name, current_price):
    cache_key         = f"tip_{skin_name}"
    cached, cached_ts = get_cached_ai_response(cache_key)
    if cached:
        age_min = int((time.time() - cached_ts) / 60)
        return cached + f"\n\n*(Cache-elt, {age_min} perce)*"

    can_go, msg = _check_gemini_rate_limit()
    if not can_go:
        return f"⏳ **Rate limit:** {msg}"

    data_block, n_points = _build_skin_data_block(skin_name, current_price)
    if data_block is None or n_points < MIN_HISTORY_POINTS:
        return f"⏳ Nincs eleg adat ({n_points}/{MIN_HISTORY_POINTS} meres). Probald kesobb!"

    loop    = asyncio.get_running_loop()
    results = {}

    for trader, prompt, tname, tokens in [
        ("hodor",   HODOR_PROMPT,   "HODOR",   250),
        ("flipper", FLIPPER_PROMPT, "FLIPPER", 250),
        ("analyst", ANALYST_PROMPT, "ANALYST", 300),
    ]:
        can_go, msg = _check_gemini_rate_limit()
        if not can_go:
            return f"⏳ **Rate limit ({tname}):** {msg}"
        _increment_gemini_counter()
        raw = await loop.run_in_executor(
            executor, _call_gemini_sync,
            f"{prompt}\n\nPIACI ADATOK:\n{data_block}", tokens
        )
        if raw and raw.startswith("HIBA:"):
            return f"❌ **Gemini hiba ({tname}):**\n```\n{raw}\n```"
        results[trader] = _parse_trader_response(raw or "", tname)
        await asyncio.sleep(GEMINI_COOLDOWN_SEC)

    hv, hi, hs = results["hodor"]
    fv, fi, fs = results["flipper"]
    av, ai_, as_ = results["analyst"]

    h_score = _velemeny_to_score(hv, hs, "HODOR")
    f_score = _velemeny_to_score(fv, fs, "FLIPPER")
    a_score = _velemeny_to_score(av, as_, "ANALYST")
    final   = int(a_score * 0.40 + h_score * 0.30 + f_score * 0.30)
    label   = _score_to_label(final)

    votes       = [hv, fv, av]
    v_count     = votes.count("VETEL")
    e_count     = votes.count("ELAD")
    w_count     = votes.count("VARJ")
    vemoji      = lambda v: "✅" if v == "VETEL" else ("❌" if v == "ELAD" else "⏸️")

    # MongoDB extra kontextus
    db_note = ""
    if mongo_ok:
        db_pts = len(mongo_load_history(skin_name, days=30))
        if db_pts > n_points:
            db_note = f"\n📊 *MongoDB: {db_pts} pont / 30 nap*"

    response = (
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🎯 **VEGSO SCORE: {final}/100** — {label}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"📊 **KONSZENZUS:** {'✅ '*v_count}{'⏸️ '*w_count}{'❌ '*e_count}"
        f"({v_count}V / {w_count}W / {e_count}E)\n\n"
        f"🐂 **HODOR** *(ovatos)* — {vemoji(hv)} {hv} | Biz: {hs}/10\n> {hi}\n\n"
        f"⚡ **FLIPPER** *(agressziv)* — {vemoji(fv)} {fv} | Biz: {fs}/10\n> {fi}\n\n"
        f"📈 **ANALYST** *(technikai)* — {vemoji(av)} {av} | Score: {as_}/100\n> {ai_}"
        f"{db_note}\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"⚠️ *Ez nem penzugyi tanacsadas!*"
    )
    set_cached_ai_response(cache_key, response)
    return response

async def get_ai_general():
    cache_key         = "general_market"
    cached, cached_ts = get_cached_ai_response(cache_key)
    if cached:
        age_min = int((time.time() - cached_ts) / 60)
        return cached + f"\n\n*(Cache-elt, {age_min} perce)*"

    can_go, msg = _check_gemini_rate_limit()
    if not can_go:
        return f"⏳ **Rate limit:** {msg}"

    case_summaries, skin_candidates = build_market_snapshot()

    if not case_summaries and not skin_candidates:
        if not price_cache:
            return "⏳ Még nincs ár a cache-ben. Várj ~2-3 percet!"
        lines = [f"  {n}: {p:.2f} EUR" for n, p in list(price_cache.items())[:30]]
        prompt = (
            "Jelenlegi CS2 arak (trend-adat meg nincs):\n" + "\n".join(lines) +
            "\n\nRovid altalanos megjegyzes ezekrol az arszintekrol. Max 200 szo."
        )
        loop = asyncio.get_running_loop()
        _increment_gemini_counter()
        answer = await loop.run_in_executor(executor, _call_gemini_sync, prompt, 800)
        if not answer:
            return "❌ Az AI nem adott vissza választ."
        if answer.startswith("HIBA:"):
            return f"❌ **Gemini hiba:**\n```\n{answer}\n```"
        return "⚠️ *Korai elemzés - trend adat nelkul*\n\n" + answer

    loop = asyncio.get_running_loop()
    _increment_gemini_counter()
    answer = await loop.run_in_executor(
        executor, _call_gemini_sync,
        format_general_prompt(case_summaries, skin_candidates), 1500
    )
    if not answer:
        return "❌ Az AI nem adott vissza választ. Ellenőrizd a Render.com logokat!"
    if answer.startswith("HIBA:"):
        return f"❌ **Gemini hiba:**\n```\n{answer}\n```"
    set_cached_ai_response(cache_key, answer)
    return answer

# -------------------------
# SLASH COMMANDOK
# -------------------------

@tree.command(name="tip", description="AI elemzes: /tip [skin] | /tip general | /tip status")
@discord.app_commands.describe(nev="Skin neve VAGY 'general' VAGY 'status'")
async def tip_command(interaction: discord.Interaction, nev: str):
    await interaction.response.defer()
    if not GEMINI_KEY:
        await interaction.followup.send("❌ `GEMINI_API_KEY` nincs beallitva!")
        return

    nev_clean = nev.strip().lower()

    # /tip status
    if nev_clean == "status":
        db_info = ""
        if mongo_ok:
            stats = mongo_get_stats()
            if stats:
                oldest_str = (
                    datetime.fromtimestamp(stats["oldest_ts"]).strftime("%Y-%m-%d")
                    if stats["oldest_ts"] else "n/a"
                )
                db_info = (
                    f"\n\n**MongoDB:**\n"
                    f"• Ar-pontok: `{stats['total_points']:,}`\n"
                    f"• Itemek: `{stats['unique_items']}`\n"
                    f"• Kovetesek: `{stats['tracked_skins']}`\n"
                    f"• Legregebb adat: `{oldest_str}`"
                )
        else:
            db_info = "\n\n**MongoDB:** ❌ Nincs kapcsolat (in-memory mod)"
        await interaction.followup.send(get_gemini_status() + db_info)
        return

    # /tip general
    if nev_clean == "general":
        await interaction.followup.send(
            "🔍 **Teljes piaci elemzés...**\nEz 10-20 mp lehet..."
        )
        result = await get_ai_general()
        await _send_long_message(
            interaction.channel,
            f"**🌍 PIACI ELEMZÉS**\n{'━'*30}\n" + result
        )
        return

    # /tip [skin neve]
    COND_LIST = [" (Factory New)", " (Minimal Wear)", " (Field-Tested)", " (Well-Worn)", " (Battle-Scarred)"]
    base_nev  = nev.strip()
    for c in COND_LIST:
        if base_nev.endswith(c):
            base_nev = base_nev[:-len(c)]
            break

    found = base_nev in SKIN_TO_CASE
    if not found:
        for s in SKIN_TO_CASE:
            if s.lower() == base_nev.lower():
                base_nev = s
                found    = True
                break

    if not found:
        await interaction.followup.send(f"❌ Nem találtam: `{nev}`\nPróbáld: `/tip general`")
        return

    price, used_name = None, nev.strip()
    if any(c.strip() in nev for c in COND_LIST):
        price = await get_price(nev.strip())
    else:
        for cond in ["", *COND_LIST]:
            p = await get_price(base_nev + cond)
            if p is not None:
                price, used_name = p, base_nev + cond
                break

    if price is None:
        await interaction.followup.send(
            f"❌ Nem sikerült az árat lekérni: `{nev}`\n"
            f"Próbáld kondícióval: `AK-47 | Redline (Field-Tested)`"
        )
        return

    cached, cached_ts = get_cached_ai_response(f"tip_{used_name}")
    if cached:
        age_min = int((time.time() - cached_ts) / 60)
        await interaction.followup.send(f"💾 Cache-ből töltöm ({age_min} perce mentett)...")
    else:
        mem_pts = len(price_history.get(used_name, []))
        db_pts  = len(mongo_load_history(used_name, days=30)) if mongo_ok else 0
        await interaction.followup.send(
            f"🤖 **3 Trader AI elemzés...**\n"
            f"Skin: `{used_name}` | Ár: `{price:.2f} EUR`\n"
            f"📊 Memory: {mem_pts}pt" + (f" | DB: {db_pts}pt" if mongo_ok else "") + "\n"
            f"HODOR → FLIPPER → ANALYST (~15-30 mp)"
        )

    tip    = await get_ai_tip_full(used_name, price)
    header = f"**🎮 AI ELEMZÉS: {used_name}**\n**💰 {price:.2f} EUR**\n{'━'*30}\n"
    await _send_long_message(interaction.channel, header + tip)


@tree.command(name="skin", description="8 napig követi a skin árát")
@discord.app_commands.describe(nev="Skin neve, pl: AK-47 | Inheritance (Field-Tested)")
async def skin_command(interaction: discord.Interaction, nev: str):
    await interaction.response.defer()
    nev_work = nev.strip()
    found    = nev_work in SKIN_TO_CASE
    if not found:
        for s in SKIN_TO_CASE:
            if s.lower() == nev_work.lower():
                nev_work = s
                found    = True
                break
    if not found:
        await interaction.followup.send(f"❌ Nem találtam: `{nev}`")
        return

    COND_LIST = [" (Factory New)", " (Minimal Wear)", " (Field-Tested)", " (Well-Worn)", " (Battle-Scarred)"]
    price, used_name = None, nev_work
    if any(c.strip() in nev_work for c in COND_LIST):
        price = await get_price(nev_work)
    else:
        for cond in ["", *COND_LIST]:
            p = await get_price(nev_work + cond)
            if p is not None:
                price, used_name = p, nev_work + cond
                break
    if price is None:
        await interaction.followup.send(f"❌ Nem sikerült az árat lekérni: `{nev}`")
        return

    data = {"start_price": price, "start_time": time.time(),
            "channel_id": interaction.channel_id, "reported_8d": False, "used_name": used_name}
    manual_tracking[nev_work] = data
    mongo_save_tracking(nev_work, data)

    await interaction.followup.send(
        f"✅ **Követés elindult!**\nSkin: `{used_name}`\n"
        f"Ár: **{price:.2f} EUR** | 📅 8 nap múlva küldök visszajelzést."
    )


@tree.command(name="dbstatus", description="MongoDB adatbázis státusza")
async def dbstatus_command(interaction: discord.Interaction):
    await interaction.response.defer()
    if not mongo_ok:
        await interaction.followup.send(
            "**🗄️ MongoDB: ❌ Nincs kapcsolat**\n"
            "In-memory módban fut — adatok elvesznek újraindításkor!\n\n"
            "**Beállítás:** Add hozzá `MONGO_URI` env változót Render.com-on.\n"
            "Ingyenes: https://www.mongodb.com/atlas"
        )
        return

    stats = mongo_get_stats()
    if not stats:
        await interaction.followup.send("❌ Statisztika lekérési hiba.")
        return

    oldest_str = (datetime.fromtimestamp(stats["oldest_ts"]).strftime("%Y-%m-%d %H:%M")
                  if stats["oldest_ts"] else "n/a")
    newest_str = (datetime.fromtimestamp(stats["newest_ts"]).strftime("%Y-%m-%d %H:%M")
                  if stats["newest_ts"] else "n/a")
    days_of_data = (
        (stats["newest_ts"] - stats["oldest_ts"]) / 86400
        if stats["oldest_ts"] and stats["newest_ts"] else 0
    )
    mem_total = sum(len(v) for v in price_history.values())

    await interaction.followup.send(
        f"**🗄️ MongoDB: ✅ Csatlakoztatva**\n{'━'*30}\n"
        f"• Összes ár-pont: `{stats['total_points']:,}`\n"
        f"• Különböző itemek: `{stats['unique_items']}`\n"
        f"• Aktív követések: `{stats['tracked_skins']}`\n"
        f"• Legrégebbi adat: `{oldest_str}`\n"
        f"• Legújabb adat: `{newest_str}`\n"
        f"• Adatok kora: `{days_of_data:.1f} nap`\n"
        f"• Memory pontok: `{mem_total:,}`\n"
        f"• Price cache: `{len(price_cache)}` item\n\n"
        f"💡 Minél több nap telik el, annál pontosabb az AI elemzés!"
    )

# -------------------------
# SEGITFUGGVENYEK
# -------------------------

async def _send_long_message(channel, text):
    if len(text) <= 1990:
        await channel.send(text)
        return
    chunks, current = [], ""
    for line in text.split("\n"):
        if len(current) + len(line) + 1 > 1980:
            if current:
                chunks.append(current.rstrip())
            current = line + "\n"
        else:
            current += line + "\n"
    if current.strip():
        chunks.append(current.rstrip())
    for chunk in chunks:
        await channel.send(chunk)
        await asyncio.sleep(0.5)

async def check_manual_tracking(channel):
    now, to_delete = time.time(), []
    for skin, data in list(manual_tracking.items()):
        if time.time() - data["start_time"] >= TRACKING_SECONDS and not data.get("reported_8d"):
            q     = data.get("used_name", skin)
            price = await get_price(q)
            if price is None or data["start_price"] == 0:
                continue
            change = (price - data["start_price"]) / data["start_price"]
            sign   = "+" if change >= 0 else ""
            irany  = "📈 emelkedett" if change >= 0 else "📉 csökkent"
            try:
                ch = await client.fetch_channel(data["channel_id"])
            except Exception:
                ch = channel
            await ch.send(
                f"📅 **8 NAPOS VISSZAJELZÉS**\nSkin: `{q}`\n"
                f"Start: **{data['start_price']:.2f} EUR** → Most: **{price:.2f} EUR**\n"
                f"Az ár {sign}{round(change*100,2)}% {irany}."
            )
            manual_tracking[skin]["reported_8d"] = True
            to_delete.append(skin)
            mongo_save_tracking(skin, manual_tracking[skin])
    for skin in to_delete:
        manual_tracking.pop(skin, None)
        mongo_delete_tracking(skin)

# -------------------------
# FO CIKLUS
# -------------------------

def get_signal_label(change):
    if change <= SIGNAL_STRONG_MAX:   return "🟢 VEDD MEG"
    if change <= SIGNAL_MEDIUM_MAX:   return "🟡 JO VETEL LEHET"
    if change <= SIGNAL_WEAK_MAX:     return "🟠 FIGYELD"
    return None

async def main_loop():
    global last_heartbeat
    channel = await client.fetch_channel(CHANNEL_ID)

    # MongoDB betoltes inditaskor
    if mongo_ok:
        await channel.send("🗄️ **MongoDB history betöltése...**")
        loop    = asyncio.get_running_loop()
        pts     = await loop.run_in_executor(executor, mongo_load_history_into_memory)
        await channel.send(f"✅ **{pts:,} ár-pont betöltve** a MongoDB-ből!")

        saved = mongo_load_all_tracking()
        if saved:
            manual_tracking.update(saved)
            await channel.send(f"📋 **{len(saved)} skin-követés** visszaállítva.")

    await channel.send("✅ **Bot online!**")

    # Ládák árai
    msg = "**📦 Ládák árai:**\n"
    for case in ALL_CASES:
        p    = await get_price(case)
        line = f"• {case}: **{p:.2f} EUR**\n" if p else f"• {case}: nem elérhető\n"
        if len(msg) + len(line) > 1900:
            await channel.send(msg)
            msg = ""
        msg += line
    if msg.strip():
        await channel.send(msg)

    # 5 random skin
    CONDS    = [" (Factory New)", " (Minimal Wear)", " (Field-Tested)", " (Well-Worn)", " (Battle-Scarred)"]
    jeloltek = random.sample(list(SKIN_TO_CASE.keys()), min(20, len(SKIN_TO_CASE)))
    lines    = []
    for skin in jeloltek:
        if len(lines) >= 5:
            break
        for cond in CONDS:
            p = await get_price(skin + cond)
            if p is not None:
                lines.append(f"• {skin}{cond}: **{p:.2f} EUR**")
                break
    if lines:
        await channel.send("**🎮 5 random skin:**\n" + "\n".join(lines))

    last_heartbeat = time.time()

    while True:
        try:
            if time.time() - last_heartbeat > 3600:
                db_str = "✅" if mongo_ok else "❌"
                await channel.send(f"💓 Bot online! DB: {db_str}")
                last_heartbeat = time.time()

            await check_manual_tracking(channel)

            for case in ALL_CASES:
                case_price, case_change = await get_price_change(case)
                if case_price is None or case_change is None:
                    continue
                if case_change < CASE_RISE_THRESHOLD:
                    continue
                for skin in CASE_SKINS.get(case, []):
                    skin_price, skin_change = await get_price_change(skin)
                    if skin_price is None or skin_change is None:
                        continue
                    label = get_signal_label(skin_change)
                    if label is None:
                        continue
                    sign = "+" if skin_change >= 0 else ""
                    await channel.send(
                        f"📣 **{label}**\n"
                        f"Láda: `{case}` (+{round(case_change*100,2)}%)\n"
                        f"Skin: `{skin}` — **{skin_price} EUR** ({sign}{round(skin_change*100,2)}%)\n"
                        f"💡 `/tip {skin}` az AI elemzésért!"
                    )
            await asyncio.sleep(CHECK_INTERVAL)

        except Exception as e:
            print(f"HIBA fo ciklus: {e}")
            try:
                await channel.send(f"⚠️ Hiba: `{e}`")
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
    init_mongodb()   # szinkron, 5mp timeout
    try:
        synced = await tree.sync()
        print(f"Slash commandok szinkronizalva: {len(synced)} db")
    except Exception as e:
        print(f"Slash sync hiba: {e}")
    if not loop_started:
        loop_started = True
        asyncio.ensure_future(main_loop())

client.run(TOKEN)
