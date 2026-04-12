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

# -------------------------
# GEMINI RATE LIMIT MANAGER
# -------------------------
# Gemini 2.0 Flash free tier: 1500 RPD, 15 RPM
# Biztonsagi hatarokat hasznalunk hogy ne futunk ki

GEMINI_MODEL        = "gemini-2.0-flash"
GEMINI_RPD_LIMIT    = 1400   # 1500-bol 100-at tartalekban tartunk
GEMINI_RPM_LIMIT    = 12     # 15-bol 3-at tartalekban tartunk
GEMINI_COOLDOWN_SEC = 5      # keres kozotti minimum varakozas (smooth UX)

# AI valasz cache - 30 percig cacheli ugyanazt a skint
AI_CACHE_TTL        = 1800   # masodpercben
ai_response_cache   = {}     # {"skin_name": {"ts": ..., "response": ...}}
ai_cache_lock       = threading.Lock()

# RPD es RPM szamlalok
gemini_stats = {
    "requests_today": 0,
    "day_reset_ts":   0.0,     # mikor resetelodott utoljara
    "requests_this_minute": 0,
    "minute_reset_ts": 0.0,
    "last_request_ts": 0.0,
    "total_blocked":  0,       # hany keres lett blokkolva limit miatt
}
gemini_lock = threading.Lock()

def _reset_gemini_daily_if_needed():
    """Napi szamlalo resetelese ha uj nap van (UTC ejfel)."""
    now = time.time()
    today_midnight = datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    ).timestamp()
    with gemini_lock:
        if gemini_stats["day_reset_ts"] < today_midnight:
            gemini_stats["requests_today"]  = 0
            gemini_stats["day_reset_ts"]    = today_midnight
            print(f"[GEMINI] Napi szamlalo resetelve. Uj nap: {datetime.now(timezone.utc).strftime('%Y-%m-%d')}")

def _check_gemini_rate_limit():
    """
    Megvizsgalja hogy lehet-e most Gemini kerest kuldeni.
    Visszater: (mehet: bool, uzenet: str | None)
    """
    _reset_gemini_daily_if_needed()
    now = time.time()

    with gemini_lock:
        # Napi limit
        if gemini_stats["requests_today"] >= GEMINI_RPD_LIMIT:
            remaining = 86400 - (now - gemini_stats["day_reset_ts"])
            h = int(remaining // 3600)
            m = int((remaining % 3600) // 60)
            gemini_stats["total_blocked"] += 1
            return False, (
                f"Az AI napi limitet elerte ({GEMINI_RPD_LIMIT} keres/nap).\n"
                f"Reset: kb. {h} ora {m} perc mulva (UTC ejfel).\n"
                f"Holnap elolrol indul a szamlalo!"
            )

        # Percenkenti limit - ablak reset
        if now - gemini_stats["minute_reset_ts"] >= 60:
            gemini_stats["requests_this_minute"] = 0
            gemini_stats["minute_reset_ts"]      = now

        if gemini_stats["requests_this_minute"] >= GEMINI_RPM_LIMIT:
            wait = 60 - (now - gemini_stats["minute_reset_ts"])
            gemini_stats["total_blocked"] += 1
            return False, (
                f"Tul sok keres egyszerre (max {GEMINI_RPM_LIMIT}/perc).\n"
                f"Varj meg ~{int(wait)+1} masodpercet, utana probald ujra!"
            )

        # Minimum varakozas keres kozott (smooth, nem spammelunk)
        since_last = now - gemini_stats["last_request_ts"]
        if since_last < GEMINI_COOLDOWN_SEC:
            wait = GEMINI_COOLDOWN_SEC - since_last
            gemini_stats["total_blocked"] += 1
            return False, (
                f"Kicsit gyors vagy! Varj meg {wait:.1f} masodpercet es probald ujra."
            )

        return True, None

def _increment_gemini_counter():
    """Sikeres keres utan noveli a szamlalokat."""
    now = time.time()
    with gemini_lock:
        gemini_stats["requests_today"]      += 1
        gemini_stats["requests_this_minute"] += 1
        gemini_stats["last_request_ts"]      = now

def get_gemini_status():
    """Visszaadja az aktualis Gemini hasznalati statisztikakat (szovegesen)."""
    _reset_gemini_daily_if_needed()
    with gemini_lock:
        remaining_today = GEMINI_RPD_LIMIT - gemini_stats["requests_today"]
        used_today      = gemini_stats["requests_today"]
        blocked         = gemini_stats["total_blocked"]
    return (
        f"**Gemini API statisztika:**\n"
        f"• Felhasznalt keres ma: {used_today} / {GEMINI_RPD_LIMIT}\n"
        f"• Maradek keres ma: {remaining_today}\n"
        f"• Blokkolt keres: {blocked}\n"
        f"• Reset: UTC ejfelkor"
    )

# -------------------------
# AI VALASZ CACHE
# -------------------------

def get_cached_ai_response(key):
    """Visszaadja a cachelt valaszt ha meg friss, egyebkent None."""
    with ai_cache_lock:
        entry = ai_response_cache.get(key)
        if entry and (time.time() - entry["ts"]) < AI_CACHE_TTL:
            return entry["response"], entry["ts"]
    return None, None

def set_cached_ai_response(key, response):
    """Elmenti az AI valaszt a cache-be."""
    with ai_cache_lock:
        ai_response_cache[key] = {"ts": time.time(), "response": response}

# -------------------------
# EXECUTOR ES DISCORD SETUP
# -------------------------

executor = ThreadPoolExecutor(max_workers=3)

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
    price_history[name].append({"ts": time.time(), "price": price})
    if len(price_history[name]) > MAX_HISTORY_POINTS * 2:
        price_history[name] = price_history[name][-MAX_HISTORY_POINTS:]

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
        step        = max(1, len(mid_part) // keep_mid)
        mid_sampled = mid_part[::step][:keep_mid]
    else:
        mid_sampled = []

    return first_part + mid_sampled + last_part

def format_history_for_ai(name):
    """
    Visszater egy (summary_str, price_log_str, n_points) tuplevval,
    vagy None-nal ha nincs adat.
    """
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
        f"Meresek szama: {len(history)}\n"
        f"Legalacsonyabb ar: {min_p:.2f} EUR\n"
        f"Legmagasabb ar: {max_p:.2f} EUR\n"
        f"Atlacar: {avg_p:.2f} EUR\n"
        f"Elso mert ar: {first:.2f} EUR\n"
        f"Jelenlegi ar: {last:.2f} EUR\n"
        f"Teljes valtozas: {total_change:+.2f}%\n"
        f"Volatilitas (max-min/avg): {volatility:.2f}%\n"
    )

    return summary, "\n".join(lines), len(history)

# -------------------------
# PIACI OSSZEFOGLALO (general-hoz)
# -------------------------

def build_market_snapshot():
    case_summaries  = []
    skin_candidates = []

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
            f"LADA: {case} | Valtozas: {total_change:+.2f}% | "
            f"Jelenlegi ar: {last:.2f} EUR | Meresek: {n}"
        )

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

    skin_candidates.sort(key=lambda x: (x[2], x[3]))
    return case_summaries, skin_candidates

def format_general_prompt(case_summaries, skin_candidates):
    if case_summaries:
        case_section = "LADAK PIACI ATTEKINTESE:\n" + "\n".join(case_summaries)
    else:
        case_section = "LADAK: Meg nincs eleg adat."

    if skin_candidates:
        skin_lines = []
        for skin, case, change, vol, price, n in skin_candidates[:10]:
            skin_lines.append(
                f"  - {skin} (ladabol: {case}) | "
                f"Valtozas: {change:+.2f}% | "
                f"Volatilitas: {vol:.1f}% | "
                f"Jelenlegi ar: {price:.2f} EUR | "
                f"Meresek: {n}"
            )
        skin_section = "SKIN JELOLTEK (valtozas szerint rendezve, legkisebb elol):\n" + "\n".join(skin_lines)
    else:
        skin_section = "SKINEK: Meg nincs eleg adat."

    prompt = (
        f"Az alabbiakban egy CS2 trading bot altal gyujtott VALOS piaci adatok lathatoak.\n"
        f"Kerlek vegezz teljes piaci elemzest!\n\n"
        f"{case_section}\n\n"
        f"{skin_section}\n\n"
        f"FELADATOD:\n"
        f"1. PIACI OSSZKEPE: Rovid osszefoglalas a jelenlegi CS2 piac allapatarol.\n"
        f"2. TOP 3 VETEL: Melyik 3 skin a legjobb vetel MOST es miert? "
        f"(Ha a lada emelkedett de a skin meg nem, az a legjobb jel!)\n"
        f"3. KOCKAZATOS SKINEK: Melyiket keruluk most es miert?\n"
        f"4. ALTALANOS TANACS: Mit erdemes figyelni a kovetkezo napokban?\n\n"
        f"Legyel konkret, adatalapu es tomorl! Max 400 szo."
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
            "appid":            STEAM_APP_ID,
            "currency":         STEAM_CURRENCY,
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
# GOOGLE GEMINI AI - ALAP HIVAS
# -------------------------

GEMINI_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"

SYSTEM_PROMPT = (
    "Te egy CS2 Steam Market befektetesi elemzo bot vagy. "
    "Kizarolag CS2 skinekre es ladakra adsz tacsacot. "
    "Adatalapu, tomor, magyar nyelvu valaszokat adsz. "
    "Soha nem adsz penzugyi garantiat, mindig kiemeled hogy a piac kiszamithatatlan. "
    "Valaszaid strukturaltak, emojikkal olvashatok es tomorek."
)

def _call_gemini_sync(prompt, max_tokens=1200):
    """
    Alacsony szintu Gemini hivas.
    Visszater a szoveges valasszal, vagy egy HIBA:... prefixu hibauzenettel.
    Nem kezeli a rate limiteket - azt a hivo felnek kell.
    """
    if not GEMINI_KEY:
        return "HIBA: GEMINI_API_KEY nincs beallitva a kornyezeti valtozokban!"

    try:
        url  = f"{GEMINI_URL}?key={GEMINI_KEY}"
        body = {
            "system_instruction": {
                "parts": [{"text": SYSTEM_PROMPT}]
            },
            "contents": [
                {
                    "role":  "user",
                    "parts": [{"text": prompt}]
                }
            ],
            "generationConfig": {
                "maxOutputTokens": max_tokens,
                "temperature":     0.75
            }
        }

        for attempt in range(3):
            res = requests.post(url, json=body, timeout=45)

            if res.status_code == 200:
                data       = res.json()
                candidates = data.get("candidates", [])
                if not candidates:
                    # Lehet hogy a tartalom blokkolt (safety filter)
                    block_reason = data.get("promptFeedback", {}).get("blockReason", "ismeretlen")
                    msg = f"HIBA: Gemini nem generalt valaszt. Ok: {block_reason}. Teljes valasz: {str(data)[:200]}"
                    print(f"[GEMINI] {msg}")
                    return msg
                return candidates[0]["content"]["parts"][0]["text"]

            elif res.status_code == 429:
                wait = 20 * (attempt + 1)
                print(f"[GEMINI] 429 Rate limit! Varakozas {wait}s... ({attempt+1}/3)")
                time.sleep(wait)
                continue

            elif res.status_code == 503:
                wait = 10 * (attempt + 1)
                print(f"[GEMINI] 503 Szerver tulterhelt. Varakozas {wait}s...")
                time.sleep(wait)
                continue

            else:
                err_text = res.text[:400]
                msg = f"HIBA: Gemini API {res.status_code} hibat adott. Valasz: {err_text}"
                print(f"[GEMINI] {msg}")
                return msg

        return "HIBA: Gemini 3 probalkozas utan sem valaszolt (429/503 loop)."

    except requests.exceptions.Timeout:
        msg = "HIBA: Gemini API timeout (45s). Lehet hogy a prompt tul hosszu vagy a szerver terhelt."
        print(f"[GEMINI] {msg}")
        return msg
    except Exception as e:
        msg = f"HIBA: Gemini hivas kivetel: {type(e).__name__}: {e}"
        print(f"[GEMINI] {msg}")
        return msg

# =====================================================================
# 3 TRADER SZEMÉLYISÉG + SCORE RENDSZER
# =====================================================================
#
# A bot harom kulonbozo trader "szemuvegen" at elemzi a skint:
#
#  1. HODOR  (hosszutavu, ovatos)  - csak akkor vesz ha biztosan jo
#  2. FLIPPER (agressziv, gyors)   - gyors ar-mozgasra jatszik
#  3. ANALYST (technikai elemzes)  - volatilitas, trend, pattern
#
# Mindegyik ad egy velemeny (Vetel / Varj / Elad) es egy
# indoklast. A vegso score 0-100 kozott van:
#  80-100 : Eros vetel
#  60-79  : Jo vetel lehet
#  40-59  : Semleges / figyelj
#  20-39  : Varj
#   0-19  : Kerulend
#
# =====================================================================

HODOR_PROMPT = """Te HODOR vagy, egy ultra-ovatos, hosszutavu CS2 befekteto.
Csak akkor javasolsz vetelre ha:
- Az ar legalabb 3+ merest mutat stabil vagy emelkedo trendet
- A volatilitas alacsony (kiszamithato mozgas)
- A lada mar emelkedett de a skin meg nem kovetele azt (divergencia)
- A jelenlegi ar a tortenet atlag ALATT van (alul-ertekelt)

Valaszod PONTOSAN ebben a formatumban legyen (semmi mas):
HODOR_VELEMENYE: [VETEL / VARJ / ELAD]
HODOR_INDOKLAS: [max 2 mondat miert]
HODOR_BIZALMI_SZINT: [1-10]
"""

FLIPPER_PROMPT = """Te FLIPPER vagy, egy agressziv, gyors CS2 skin flipper.
Mindig a rovid tavu ar-mozgasra jatszol:
- Ha az ar az utolso mereseken emelkedik: vetel (momentum)
- Ha a lada emelkedett es a skin elmaradt: azonnali vetel (divergencia play)
- Ha az ar atlag felett van es emelkedik: lehet elad (profit take)
- Nem erdekel a hosszu tavon, csak a kovetkezo 1-3 nap

Valaszod PONTOSAN ebben a formatumban legyen (semmi mas):
FLIPPER_VELEMENYE: [VETEL / VARJ / ELAD]
FLIPPER_INDOKLAS: [max 2 mondat miert]
FLIPPER_BIZALMI_SZINT: [1-10]
"""

ANALYST_PROMPT = """Te ANALYST vagy, egy hidegeveru technikai elemzo.
Szamok alapjan dolgozol:
- Volatilitas: ha > 30% akkor kockazatos, ha < 10% akkor stabil
- Trend: elso vs utolso ar osszehasonlitasa (emelkedo / csokeno / oldalaz)
- Support szint: a meresek minimuma = tamasz ar
- Divergencia ertek: (lada_valtozas - skin_valtozas) -> minnel nagyobb, annal jobb lehetoseg

Valaszod PONTOSAN ebben a formatumban legyen (semmi mas):
ANALYST_VELEMENYE: [VETEL / VARJ / ELAD]
ANALYST_INDOKLAS: [max 2 mondat miert]
ANALYST_SCORE: [0-100, ahol 100 = tokeletes vetel]
"""

def _build_skin_data_block(skin_name, current_price):
    """Osszeallitja az adatblokkot amit mindegyik trader megkap."""
    hist_result = format_history_for_ai(skin_name)
    case_name   = SKIN_TO_CASE.get(skin_name, "Ismeretlen")
    case_hist   = format_history_for_ai(case_name) if case_name != "Ismeretlen" else None

    if hist_result is None:
        return None, 0

    summary, price_log, n_points = hist_result

    # Lada adatok
    case_block = ""
    case_change_pct = 0.0
    if case_hist and case_hist[2] >= MIN_HISTORY_POINTS:
        c_summary, c_log, c_n = case_hist
        c_prices    = [e["price"] for e in get_dynamic_history(case_name)]
        c_first     = c_prices[0]
        c_last      = c_prices[-1]
        case_change_pct = ((c_last - c_first) / c_first * 100) if c_first > 0 else 0
        case_block = (
            f"\nA LADA ADATAI ({case_name}):\n"
            f"{c_summary}\n"
            f"Lada ar-valtozas: {case_change_pct:+.2f}%"
        )

    # Skin ar-valtozas szamolas
    skin_prices = [e["price"] for e in get_dynamic_history(skin_name)]
    s_first     = skin_prices[0] if skin_prices else current_price
    skin_change_pct = ((current_price - s_first) / s_first * 100) if s_first > 0 else 0

    divergencia = case_change_pct - skin_change_pct

    data_block = (
        f"SKIN: {skin_name}\n"
        f"Lada: {case_name}\n"
        f"Jelenlegi ar: {current_price:.2f} EUR\n"
        f"Skin ar-valtozas (osszes meres): {skin_change_pct:+.2f}%\n"
        f"Divergencia (lada minus skin valtozas): {divergencia:+.2f}% "
        f"{'(pozitiv = lada megelozi a skint = JO JEL)' if divergencia > 0 else ''}\n\n"
        f"SKIN ARTORTENETE ({n_points} meres):\n"
        f"{summary}\n"
        f"Arfolyam log:\n{price_log}"
        f"{case_block}"
    )

    return data_block, n_points

def _parse_trader_response(text, trader_name):
    """
    Kiolvassa a trader valaszabol a veleményt es a bizalmi szintet.
    Visszater: (velemeny: str, indoklas: str, score: int)
    """
    lines      = text.strip().split("\n")
    velemeny   = "VARJ"
    indoklas   = "Nem sikerult elemezni."
    raw_score  = 5

    prefix_map = {
        "HODOR":   ("HODOR_VELEMENYE:", "HODOR_INDOKLAS:", "HODOR_BIZALMI_SZINT:"),
        "FLIPPER": ("FLIPPER_VELEMENYE:", "FLIPPER_INDOKLAS:", "FLIPPER_BIZALMI_SZINT:"),
        "ANALYST": ("ANALYST_VELEMENYE:", "ANALYST_INDOKLAS:", "ANALYST_SCORE:"),
    }

    pv, pi, ps = prefix_map.get(trader_name, ("", "", ""))

    for line in lines:
        line = line.strip()
        if line.startswith(pv):
            val = line[len(pv):].strip().upper()
            if "VETEL" in val:
                velemeny = "VETEL"
            elif "ELAD" in val:
                velemeny = "ELAD"
            else:
                velemeny = "VARJ"
        elif line.startswith(pi):
            indoklas = line[len(pi):].strip()
        elif line.startswith(ps):
            try:
                raw_score = int("".join(c for c in line[len(ps):] if c.isdigit())[:3])
                raw_score = max(1, min(10 if trader_name != "ANALYST" else 100, raw_score))
            except Exception:
                raw_score = 5

    return velemeny, indoklas, raw_score

def _velemeny_to_score(velemeny, raw_score, trader_name):
    """
    Atvalositja a veleményt egy 0-100 skalaira.
    Analyst mar 0-100-at ad, Hodor/Flipper 1-10-et.
    """
    if trader_name == "ANALYST":
        return max(0, min(100, raw_score))

    # Hodor / Flipper: 1-10 bizalmi szint -> velemeny alapjan skalalas
    if velemeny == "VETEL":
        base = 60
        bonus = (raw_score - 1) * (40 / 9)   # 60-100 kozotti skala
    elif velemeny == "VARJ":
        base = 30
        bonus = (raw_score - 1) * (20 / 9)   # 30-50 kozotti skala
    else:  # ELAD
        base = 0
        bonus = (raw_score - 1) * (20 / 9)   # 0-20 kozotti skala

    return max(0, min(100, int(base + bonus)))

def _score_to_label(score):
    if score >= 80:
        return "🟢 EROS VETEL"
    elif score >= 60:
        return "🟡 JO VETEL LEHET"
    elif score >= 40:
        return "🟠 SEMLEGES / FIGYELJ"
    elif score >= 20:
        return "🔴 VARJ"
    else:
        return "⛔ KERULEND"

def _call_gemini_trader(data_block, trader_prompt, trader_name, max_tokens=300):
    """Egy trader szemelyre szolo Gemini hivas."""
    prompt = f"{trader_prompt}\n\nPIACI ADATOK:\n{data_block}"
    result = _call_gemini_sync(prompt, max_tokens=max_tokens)
    return result

async def get_ai_tip_full(skin_name, current_price):
    """
    A fo AI elemzesi fuggveny.
    Harom Gemini hivas (3 trader), majd osszevont score es megjelenitcs.
    AI cache-t hasznal - 30 percig ugyanazt adja vissza.
    """
    # Cache ellenorzese
    cache_key      = f"tip_{skin_name}"
    cached, cached_ts = get_cached_ai_response(cache_key)
    if cached:
        age_min = int((time.time() - cached_ts) / 60)
        return cached + f"\n\n*(Cache-elt valasz, {age_min} perce)*"

    # Rate limit ellenorzese - harom hivas kell, ellenorizzuk elore
    can_go, limit_msg = _check_gemini_rate_limit()
    if not can_go:
        return f"⏳ **Rate limit:**\n{limit_msg}"

    # Adatblokk osszeallitasa
    data_block, n_points = _build_skin_data_block(skin_name, current_price)
    if data_block is None or n_points < MIN_HISTORY_POINTS:
        dp = n_points if data_block else 0
        return (
            f"Meg nincs eleg piaci adat ehhez a skinhez.\n"
            f"Jelenlegi meresek szama: {dp} (minimum: {MIN_HISTORY_POINTS})\n"
            f"Probald meg ujra kesobb!"
        )

    loop = asyncio.get_running_loop()

    # --- HODOR hivas ---
    can_go, limit_msg = _check_gemini_rate_limit()
    if not can_go:
        return f"⏳ **Rate limit (HODOR keres):**\n{limit_msg}"
    _increment_gemini_counter()
    hodor_raw = await loop.run_in_executor(
        executor, _call_gemini_trader, data_block, HODOR_PROMPT, "HODOR", 250
    )
    await asyncio.sleep(GEMINI_COOLDOWN_SEC)

    # --- FLIPPER hivas ---
    can_go, limit_msg = _check_gemini_rate_limit()
    if not can_go:
        return f"⏳ **Rate limit (FLIPPER keres):**\n{limit_msg}"
    _increment_gemini_counter()
    flipper_raw = await loop.run_in_executor(
        executor, _call_gemini_trader, data_block, FLIPPER_PROMPT, "FLIPPER", 250
    )
    await asyncio.sleep(GEMINI_COOLDOWN_SEC)

    # --- ANALYST hivas ---
    can_go, limit_msg = _check_gemini_rate_limit()
    if not can_go:
        return f"⏳ **Rate limit (ANALYST keres):**\n{limit_msg}"
    _increment_gemini_counter()
    analyst_raw = await loop.run_in_executor(
        executor, _call_gemini_trader, data_block, ANALYST_PROMPT, "ANALYST", 300
    )

    # Ha barmelyik HIBA: prefixszel tert vissza, megmutatjuk
    for trader_name, raw in [("HODOR", hodor_raw), ("FLIPPER", flipper_raw), ("ANALYST", analyst_raw)]:
        if raw and raw.startswith("HIBA:"):
            return f"❌ **Gemini API hiba ({trader_name} keres):**\n```\n{raw}\n```"

    # Parseolas
    hodor_v,   hodor_i,   hodor_s   = _parse_trader_response(hodor_raw or "",   "HODOR")
    flipper_v, flipper_i, flipper_s = _parse_trader_response(flipper_raw or "", "FLIPPER")
    analyst_v, analyst_i, analyst_s = _parse_trader_response(analyst_raw or "", "ANALYST")

    # Score szamitas
    hodor_score   = _velemeny_to_score(hodor_v,   hodor_s,   "HODOR")
    flipper_score = _velemeny_to_score(flipper_v, flipper_s, "FLIPPER")
    analyst_score = _velemeny_to_score(analyst_v, analyst_s, "ANALYST")

    # Vegso score: Analyst 40%, Hodor 30%, Flipper 30%
    final_score = int(
        analyst_score * 0.40 +
        hodor_score   * 0.30 +
        flipper_score * 0.30
    )
    label = _score_to_label(final_score)

    # Konszenzus szamlalasa
    votes        = [hodor_v, flipper_v, analyst_v]
    vetel_count  = votes.count("VETEL")
    elad_count   = votes.count("ELAD")
    varj_count   = votes.count("VARJ")

    # Emojik a velemenyekhez
    def v_emoji(v):
        return "✅" if v == "VETEL" else ("❌" if v == "ELAD" else "⏸️")

    response = (
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🎯 **VEGSO SCORE: {final_score}/100** — {label}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"

        f"📊 **KONSZENZUS:** "
        f"{'✅ ' * vetel_count}{'⏸️ ' * varj_count}{'❌ ' * elad_count}"
        f"({vetel_count} Vetel / {varj_count} Varj / {elad_count} Elad)\n\n"

        f"🐂 **HODOR** *(hosszutavu, ovatos)* — {v_emoji(hodor_v)} {hodor_v} | Bizalom: {hodor_s}/10\n"
        f"> {hodor_i}\n\n"

        f"⚡ **FLIPPER** *(gyors, agressziv)* — {v_emoji(flipper_v)} {flipper_v} | Bizalom: {flipper_s}/10\n"
        f"> {flipper_i}\n\n"

        f"📈 **ANALYST** *(technikai)* — {v_emoji(analyst_v)} {analyst_v} | Score: {analyst_s}/100\n"
        f"> {analyst_i}\n\n"

        f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"⚠️ *Ez nem penzugyi tanacsadas. A piac kiszamithatatlan!*"
    )

    # Cache-be mentjuk
    set_cached_ai_response(cache_key, response)
    return response

async def get_ai_general():
    """Teljes piaci elemzes az osszes gyujtott adatbol. 1 Gemini hivas."""
    # Cache ellenorzese
    cache_key      = "general_market"
    cached, cached_ts = get_cached_ai_response(cache_key)
    if cached:
        age_min = int((time.time() - cached_ts) / 60)
        return cached + f"\n\n*(Cache-elt valasz, {age_min} perce)*"

    can_go, limit_msg = _check_gemini_rate_limit()
    if not can_go:
        return f"⏳ **Rate limit:**\n{limit_msg}"

    case_summaries, skin_candidates = build_market_snapshot()
    total_items = len(case_summaries) + len(skin_candidates)

    # Ha nincs eleg history-adat, de van price_cache-unk, abbol epitunk egy alap promptot
    if total_items == 0:
        if not price_cache:
            return (
                "⏳ Még nincs egyetlen ár sem a cache-ben.\n"
                f"Kérlek várj ~2-3 percet az indítás után, amíg a bot lekéri az árakat!"
            )
        # Alap prompt csak a jelenlegi cachelt arakbol
        cache_lines = []
        for name, price in list(price_cache.items())[:30]:
            cache_lines.append(f"  {name}: {price:.2f} EUR")
        fallback_prompt = (
            "Az alabbiakban egy CS2 trading bot altal gyujtott JELENLEGI PIACI ARAK lathatoak.\n"
            "Meg nincs eleg tortenet-adat a trend elemzeshez, csak az aktualis arak.\n\n"
            "JELENLEGI ARAK:\n" + "\n".join(cache_lines) + "\n\n"
            "FELADATOD:\n"
            "1. Rövid altalanos megjegyzes a CS2 piac jelenlegi arszintjeirol.\n"
            "2. Melyik 2-3 item tunik erdekes arszinten lenni?\n"
            "3. Mire figyeljen a trader a kovetkezo napokban?\n"
            "Megjegyzes: ez egy korai elemzes, meg nincs trend-adat. Max 200 szo."
        )
        loop   = asyncio.get_running_loop()
        _increment_gemini_counter()
        answer = await loop.run_in_executor(executor, _call_gemini_sync, fallback_prompt, 800)
        if not answer:
            return "❌ Az AI nem adott vissza választ. Ellenőrizd a Render.com logokat!"
        if answer.startswith("HIBA:"):
            return f"❌ **Gemini API hiba:**\n```\n{answer}\n```"
        return "⚠️ *Korai elemzés - trend adatok nélkül*\n\n" + answer

    prompt = format_general_prompt(case_summaries, skin_candidates)
    loop   = asyncio.get_running_loop()
    _increment_gemini_counter()
    answer = await loop.run_in_executor(executor, _call_gemini_sync, prompt, 1500)

    # Ha a valasz HIBA: prefixszel kezdodik, megmutatjuk a felhasznalonak
    if not answer:
        return "❌ Az AI nem adott vissza választ. Ellenőrizd a Render.com logokat!"
    if answer.startswith("HIBA:"):
        return f"❌ **Gemini API hiba:**\n```\n{answer}\n```"

    set_cached_ai_response(cache_key, answer)
    return answer

# -------------------------
# SLASH COMMAND: /tip
# -------------------------

@tree.command(
    name="tip",
    description="AI elemzes: /tip [skin neve] | /tip general | /tip status"
)
@discord.app_commands.describe(
    nev="Skin neve (pl: AK-47 | Redline (Field-Tested)) VAGY 'general' VAGY 'status'"
)
async def tip_command(interaction: discord.Interaction, nev: str):
    await interaction.response.defer()

    if not GEMINI_KEY:
        await interaction.followup.send(
            "Az AI funkció nincs konfigurálva.\n"
            "Be kell állítani a `GEMINI_API_KEY` környezeti változót!\n"
            "API kulcs: https://aistudio.google.com"
        )
        return

    nev_clean = nev.strip().lower()

    # ----------------------------------
    # /tip status - API statisztika
    # ----------------------------------
    if nev_clean == "status":
        await interaction.followup.send(get_gemini_status())
        return

    # ----------------------------------
    # /tip general - teljes piaci elemzés
    # ----------------------------------
    if nev_clean == "general":
        await interaction.followup.send(
            "🔍 **Teljes piaci elemzés folyamatban...**\n"
            "Az AI most átnézi az összes gyűjtött ládát és skint.\n"
            "Ez 10-20 másodpercet vehet igénybe..."
        )
        result   = await get_ai_general()
        header   = f"**🌍 ÁLTALÁNOS PIACI ELEMZÉS**\n{'━'*30}\n"
        full_msg = header + result

        await _send_long_message(interaction.channel, full_msg)
        return

    # ----------------------------------
    # /tip [skin neve] - egyedi 3-trader elemzés
    # ----------------------------------
    COND_LIST = [" (Factory New)", " (Minimal Wear)", " (Field-Tested)", " (Well-Worn)", " (Battle-Scarred)"]
    base_nev  = nev.strip()
    for c in COND_LIST:
        if base_nev.endswith(c):
            base_nev = base_nev[:-len(c)]
            break

    found = base_nev in SKIN_TO_CASE
    if not found:
        for s in SKIN_TO_CASE.keys():
            if s.lower() == base_nev.lower():
                base_nev = s
                found    = True
                break

    if not found:
        await interaction.followup.send(
            f"❌ Nem találtam ezt a skint: `{nev}`\n"
            f"Ellenőrizd a nevet, vagy írd: `/tip general` az általános elemzéshez!\n"
            f"Tipp: a skin nevét pontosan add meg, pl: `AK-47 | Redline`"
        )
        return

    # Ar lekeres kondicioval
    CONDITIONS = ["", " (Factory New)", " (Minimal Wear)", " (Field-Tested)", " (Well-Worn)", " (Battle-Scarred)"]
    price     = None
    used_name = nev.strip()

    if any(cond.strip() in nev for cond in COND_LIST):
        price     = await get_price(nev.strip())
        used_name = nev.strip()
    else:
        for cond in CONDITIONS:
            test_name = base_nev + cond
            p         = await get_price(test_name)
            if p is not None:
                price     = p
                used_name = test_name
                break

    if price is None:
        await interaction.followup.send(
            f"❌ Nem sikerült az árat lekérni: `{nev}`\n"
            f"Próbáld meg a pontos Steam névvel + kondícióval!\n"
            f"Pl: `AK-47 | Inheritance (Field-Tested)`"
        )
        return

    # Cache ellenorzese es elore jelzese
    cache_key      = f"tip_{used_name}"
    cached, cached_ts = get_cached_ai_response(cache_key)
    if cached:
        age_min = int((time.time() - cached_ts) / 60)
        await interaction.followup.send(
            f"💾 **Cache-ből töltöm** ({age_min} perce mentett elemzés)..."
        )
    else:
        await interaction.followup.send(
            f"🤖 **3 Trader AI elemzés indul...**\n"
            f"Skin: `{used_name}` | Ár: `{price:.2f} EUR`\n"
            f"HODOR → FLIPPER → ANALYST ... (~15-30 mp)"
        )

    tip    = await get_ai_tip_full(used_name, price)
    header = f"**🎮 AI ELEMZÉS: {used_name}**\n**💰 Jelenlegi ár: {price:.2f} EUR**\n{'━'*30}\n"

    await _send_long_message(interaction.channel, header + tip)

# -------------------------
# SLASH COMMAND: /skin
# -------------------------

@tree.command(name="skin", description="8 napig követi a megadott skin árát és jelzi a változást")
@discord.app_commands.describe(nev="A skin neve pontosan, pl: AK-47 | Inheritance (Field-Tested)")
async def skin_command(interaction: discord.Interaction, nev: str):
    await interaction.response.defer()

    found    = nev in SKIN_TO_CASE
    nev_work = nev.strip()
    if not found:
        for s in SKIN_TO_CASE.keys():
            if s.lower() == nev_work.lower():
                nev_work = s
                found    = True
                break

    if not found:
        await interaction.followup.send(
            f"❌ Nem találtam ezt a skint a cases.json-ban: `{nev}`\n"
            f"Ellenőrizd a nevet! (pl: `AK-47 | Inheritance`)"
        )
        return

    CONDITIONS = ["", " (Factory New)", " (Minimal Wear)", " (Field-Tested)", " (Well-Worn)", " (Battle-Scarred)"]
    price      = None
    used_name  = nev_work

    if any(cond.strip() in nev_work for cond in CONDITIONS[1:]):
        price     = await get_price(nev_work)
        used_name = nev_work
    else:
        for cond in CONDITIONS:
            test_name = nev_work + cond
            p         = await get_price(test_name)
            if p is not None:
                price     = p
                used_name = test_name
                break

    if price is None:
        await interaction.followup.send(
            f"❌ Nem sikerült az árat lekérni: `{nev}`\n"
            f"Próbáld meg a pontos Steam névvel kondícióval, pl:\n"
            f"`AK-47 | Inheritance (Field-Tested)`"
        )
        return

    manual_tracking[nev_work] = {
        "start_price": price,
        "start_time":  time.time(),
        "channel_id":  interaction.channel_id,
        "reported_8d": False,
        "used_name":   used_name
    }

    await interaction.followup.send(
        f"✅ **Követés elindult!**\n"
        f"Skin: `{used_name}`\n"
        f"Jelenlegi ár: **{price:.2f} EUR**\n"
        f"📅 8 nap múlva küldök visszajelzést a változásról."
    )

# -------------------------
# HOSSZU UZENET KULDES SEGITFUGGVENY
# -------------------------

async def _send_long_message(channel, text):
    """Discord 2000 karakteres limit intelligens kezelese."""
    if len(text) <= 1990:
        await channel.send(text)
        return

    # Feldarabolas soronkent (nem vagunk el szo kozott)
    chunks  = []
    current = ""
    for line in text.split("\n"):
        test = current + line + "\n"
        if len(test) > 1980:
            if current:
                chunks.append(current.rstrip())
            current = line + "\n"
        else:
            current = test
    if current.strip():
        chunks.append(current.rstrip())

    for chunk in chunks:
        await channel.send(chunk)
        await asyncio.sleep(0.5)  # kis delay hogy ne flood-olja a Discord API-t

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
            irany  = "📈 emelkedett" if change >= 0 else "📉 csökkent"

            try:
                target_channel = await client.fetch_channel(data["channel_id"])
            except Exception:
                target_channel = channel

            await target_channel.send(
                f"📅 **8 NAPOS VISSZAJELZÉS**\n"
                f"Skin: `{query_name}`\n"
                f"Indulási ár: **{round(initial, 2)} EUR**\n"
                f"Jelenlegi ár: **{round(current_price, 2)} EUR**\n"
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
        return "🟢 VEDD MEG"
    elif skin_change <= SIGNAL_MEDIUM_MAX:
        return "🟡 JO VETEL LEHET"
    elif skin_change <= SIGNAL_WEAK_MAX:
        return "🟠 FIGYELD"
    else:
        return None

async def main_loop():
    global last_heartbeat

    channel = await client.fetch_channel(CHANNEL_ID)
    await channel.send("✅ **Bot online!** Elemzem a ládákat...")

    # Ládák árai
    msg = "**📦 Ládák aktuális árai:**\n"
    for case in ALL_CASES:
        price = await get_price(case)
        line  = f"• {case}: **{price:.2f} EUR**\n" if price else f"• {case}: nem elérhető\n"
        if len(msg) + len(line) > 1900:
            await channel.send(msg)
            msg = ""
        msg += line
    if msg.strip():
        await channel.send(msg)

    # 5 random skin
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
                skin_lines.append(f"• {skin}{cond}: **{p:.2f} EUR**")
                break
    if skin_lines:
        await channel.send("**🎮 5 random skin ára:**\n" + "\n".join(skin_lines))

    last_heartbeat = time.time()

    while True:
        try:
            current_time = time.time()

            if current_time - last_heartbeat > 3600:
                await channel.send("💓 Bot online és figyel!")
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
                        f"📣 **JELZÉS: {label}**\n"
                        f"Láda: `{case}` (+{round(case_change * 100, 2)}%)\n"
                        f"Skin: `{skin}`\n"
                        f"Jelenlegi ár: **{skin_price} EUR** "
                        f"({sign}{round(skin_change * 100, 2)}%)\n"
                        f"💡 Tipp: `/tip {skin}` az AI elemzésért!"
                    )

            await asyncio.sleep(CHECK_INTERVAL)

        except Exception as e:
            print(f"HIBA a fo ciklusban: {e}")
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
    try:
        synced = await tree.sync()
        print(f"Slash commandok szinkronizalva: {len(synced)} db")
    except Exception as e:
        print(f"Slash command sync hiba: {e}")
    if not loop_started:
        loop_started = True
        asyncio.ensure_future(main_loop())

client.run(TOKEN)
