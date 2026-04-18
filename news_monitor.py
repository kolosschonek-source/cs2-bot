"""
CS2 Trading Bot - Hírfigyelő modul
===================================
Forrásai:
  1. Reddit  - r/GlobalOffensive, r/csgomarketforum (nyilvános JSON API)
  2. Steam   - CS2 hivatalos hírek (ISteamNews API)
  3. Twitter - Nitter tükör scraping (ingyenes, kulcs nélkül)

Működés:
  - 5 percenként ellenőrzi az összes forrást
  - Gemini AI elemzi a hírt: melyik skin/láda érintett + piaci hatás
  - Discord üzenet azonnal, már látott hírek nem kerülnek újra kiküldésre
"""

import asyncio
import hashlib
import time
import re
import requests
from bs4 import BeautifulSoup

# -----------------------------------------------------------------------
# KULCSSZAVAK - ezekre figyel a bot
# -----------------------------------------------------------------------

# Általános CS2 piaci kulcsszavak
GENERAL_KEYWORDS = [
    "operation", "operáció", "update", "patch", "case", "láda", "skin",
    "valve", "cs2", "csgo", "counter-strike", "major", "tournament",
    "ban", "unban", "new", "release", "drop", "market", "price",
    "sticker", "capsule", "souvenir", "graffiti", "music kit",
    "knife", "gloves", "rare", "limited", "discontinued",
]

# Fegyver nevek a cases.json alapján
WEAPON_KEYWORDS = [
    "ak-47", "ak47", "awp", "m4a4", "m4a1", "usp-s", "glock",
    "desert eagle", "deagle", "karambit", "butterfly", "fade",
    "doppler", "marble fade", "tiger tooth", "asiimov", "redline",
    "howl", "printstream", "pandora", "inheritance",
]

# Profi játékosok akiknek a skinhasználata mozgatja a piacot
PRO_PLAYERS = [
    "s1mple", "niko", "zywoo", "device", "electronic",
    "sh1ro", "ax1le", "hobbit", "b1t", "jame",
    "twistzz", "ropz", "broky", "rain", "karrigan",
]

# Összes kulcsszó
ALL_KEYWORDS = GENERAL_KEYWORDS + WEAPON_KEYWORDS + PRO_PLAYERS

# Magas prioritású kulcsszavak - ezekre azonnal jelez
HIGH_PRIORITY = [
    "operation", "operáció", "new case", "új láda", "major",
    "ban", "valve update", "patch notes", "discontinued",
]

# -----------------------------------------------------------------------
# MÁR LÁTOTT HÍREK (duplikátum szűrő)
# -----------------------------------------------------------------------

seen_news_hashes = set()

def _hash_news(title: str, source: str) -> str:
    return hashlib.md5(f"{source}:{title.lower().strip()}".encode()).hexdigest()

def is_seen(title: str, source: str) -> bool:
    h = _hash_news(title, source)
    if h in seen_news_hashes:
        return True
    seen_news_hashes.add(h)
    # Max 500 hash tárolása memóriában
    if len(seen_news_hashes) > 500:
        oldest = list(seen_news_hashes)[:100]
        for old in oldest:
            seen_news_hashes.discard(old)
    return False

def _contains_keyword(text: str) -> tuple[bool, list[str]]:
    """Visszaadja hogy tartalmaz-e kulcsszót, és melyeket."""
    text_lower = text.lower()
    found = [kw for kw in ALL_KEYWORDS if kw in text_lower]
    return bool(found), found

def _is_high_priority(text: str) -> bool:
    text_lower = text.lower()
    return any(kw in text_lower for kw in HIGH_PRIORITY)

# -----------------------------------------------------------------------
# 1. REDDIT FIGYELŐ
# -----------------------------------------------------------------------

REDDIT_SOURCES = [
    {
        "url": "https://www.reddit.com/r/GlobalOffensive/new.json?limit=10",
        "name": "r/GlobalOffensive",
    },
    {
        "url": "https://www.reddit.com/r/csgomarketforum/new.json?limit=10",
        "name": "r/csgomarketforum",
    },
    {
        "url": "https://www.reddit.com/r/cs2/new.json?limit=10",
        "name": "r/cs2",
    },
]

REDDIT_HEADERS = {
    "User-Agent": "CS2TradingBot/1.0 (by /u/cs2tradingbot)",
}

def fetch_reddit_news() -> list[dict]:
    """Lekéri a legújabb Reddit posztokat és szűri kulcsszóra."""
    results = []
    for source in REDDIT_SOURCES:
        try:
            res = requests.get(
                source["url"],
                headers=REDDIT_HEADERS,
                timeout=10
            )
            if res.status_code != 200:
                print(f"[REDDIT] {source['name']} hiba: {res.status_code}")
                continue

            data = res.json()
            posts = data.get("data", {}).get("children", [])

            for post in posts:
                p = post.get("data", {})
                title    = p.get("title", "")
                selftext = p.get("selftext", "")
                url      = f"https://reddit.com{p.get('permalink', '')}"
                score    = p.get("score", 0)
                created  = p.get("created_utc", 0)

                # Csak friss posztok (max 2 óra)
                if time.time() - created > 7200:
                    continue

                full_text = f"{title} {selftext}"
                has_kw, keywords = _contains_keyword(full_text)

                if not has_kw:
                    continue
                if is_seen(title, source["name"]):
                    continue

                results.append({
                    "source":      source["name"],
                    "title":       title,
                    "text":        selftext[:500] if selftext else "",
                    "url":         url,
                    "score":       score,
                    "keywords":    keywords,
                    "priority":    "HIGH" if _is_high_priority(full_text) else "NORMAL",
                    "ts":          created,
                    "type":        "reddit",
                })

        except Exception as e:
            print(f"[REDDIT] {source['name']} kivétel: {e}")

    return results

# -----------------------------------------------------------------------
# 2. STEAM HÍREK FIGYELŐ
# -----------------------------------------------------------------------

STEAM_NEWS_URL = "https://api.steampowered.com/ISteamNews/GetNewsForApp/v2/"
CS2_APP_ID     = 730

def fetch_steam_news() -> list[dict]:
    """Lekéri a CS2 hivatalos Steam híreit."""
    results = []
    try:
        res = requests.get(
            STEAM_NEWS_URL,
            params={
                "appid":   CS2_APP_ID,
                "count":   5,
                "maxlength": 500,
                "format":  "json",
            },
            timeout=10
        )
        if res.status_code != 200:
            print(f"[STEAM NEWS] Hiba: {res.status_code}")
            return []

        items = res.json().get("appnews", {}).get("newsitems", [])
        for item in items:
            title   = item.get("title", "")
            url     = item.get("url", "")
            content = item.get("contents", "")
            date    = item.get("date", 0)

            # Csak friss hírek (max 24 óra)
            if time.time() - date > 86400:
                continue
            if is_seen(title, "Steam"):
                continue

            # Steam hivatalos hírek mindig HIGH priority
            results.append({
                "source":   "Steam Official",
                "title":    title,
                "text":     content[:500],
                "url":      url,
                "keywords": ["official", "valve", "cs2"],
                "priority": "HIGH",
                "ts":       date,
                "type":     "steam",
            })

    except Exception as e:
        print(f"[STEAM NEWS] Kivétel: {e}")

    return results

# -----------------------------------------------------------------------
# 3. TWITTER/X FIGYELŐ (Nitter scraping)
# -----------------------------------------------------------------------

# Több Nitter tükör, ha az egyik nem elérhető
NITTER_INSTANCES = [
    "https://nitter.privacydev.net",
    "https://nitter.poast.org",
    "https://nitter.1d4.us",
]

TWITTER_ACCOUNTS = [
    "csgo",          # Hivatalos CS2/CSGO Twitter
    "CounterStrike", # Valve CS2
    "s1mplecsgo",    # s1mple
    "niko_cs",       # NiKo
    "ZywOo",         # ZywOo
]

def _get_working_nitter() -> str | None:
    """Megkeresi az első működő Nitter példányt."""
    for instance in NITTER_INSTANCES:
        try:
            res = requests.get(f"{instance}/csgo", timeout=8)
            if res.status_code == 200:
                return instance
        except Exception:
            continue
    return None

def fetch_twitter_news() -> list[dict]:
    """Lekéri a Twitter/X posztokat Nitter-en keresztül."""
    results = []
    nitter  = _get_working_nitter()

    if not nitter:
        print("[TWITTER] Nincs elérhető Nitter példány.")
        return []

    for account in TWITTER_ACCOUNTS:
        try:
            url = f"{nitter}/{account}"
            res = requests.get(url, timeout=10, headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
            })
            if res.status_code != 200:
                continue

            soup   = BeautifulSoup(res.text, "html.parser")
            tweets = soup.find_all("div", class_="tweet-content")

            for tweet in tweets[:5]:
                text = tweet.get_text(strip=True)
                if not text:
                    continue

                has_kw, keywords = _contains_keyword(text)
                # Hivatalos CS2 fiók minden tweetje releváns
                if account in ("csgo", "CounterStrike"):
                    has_kw  = True
                    keywords = keywords or ["official"]

                if not has_kw:
                    continue
                if is_seen(text[:100], f"Twitter/{account}"):
                    continue

                results.append({
                    "source":   f"Twitter @{account}",
                    "title":    text[:120],
                    "text":     text[:500],
                    "url":      url,
                    "keywords": keywords,
                    "priority": "HIGH" if (
                        account in ("csgo", "CounterStrike") or
                        _is_high_priority(text)
                    ) else "NORMAL",
                    "ts":       time.time(),
                    "type":     "twitter",
                })

        except Exception as e:
            print(f"[TWITTER] @{account} kivétel: {e}")

    return results

# -----------------------------------------------------------------------
# GEMINI AI ELEMZÉS - Hír hatás becslése
# -----------------------------------------------------------------------

def analyze_news_with_ai(news_item: dict, gemini_key: str,
                          all_cases: list, skin_to_case: dict) -> str | None:
    """
    Gemini elemzi a hírt:
    - Melyik skin/láda lehet érintett?
    - Várható piaci hatás (emelkedés/csökkenés)?
    - Mennyire sürgős a cselekvés?
    """
    if not gemini_key:
        return None

    # Összes láda neve röviden az AI kontextusához
    case_list = ", ".join(all_cases[:20]) + "..."

    prompt = (
        f"Egy CS2 trading bot hírérzékelője ezt találta:\n\n"
        f"Forrás: {news_item['source']}\n"
        f"Cím: {news_item['title']}\n"
        f"Tartalom: {news_item['text'][:300]}\n"
        f"Kulcsszavak: {', '.join(news_item['keywords'][:10])}\n\n"
        f"Elérhető ládák: {case_list}\n\n"
        f"Elemezd PONTOSAN ebben a formátumban (semmi más):\n"
        f"HATÁS: [POZITÍV / NEGATÍV / SEMLEGES / BIZONYTALAN]\n"
        f"ÉRINTETT: [konkrét skin vagy láda neve, vagy 'általános piac']\n"
        f"SÜRGESSÉG: [AZONNALI / MA / FIGYELD / NEM FONTOS]\n"
        f"INDOKLÁS: [max 2 mondat, miért]\n"
        f"TEENDŐ: [max 1 mondat, mit tegyünk most]"
    )

    try:
        url  = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={gemini_key}"
        body = {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"maxOutputTokens": 300, "temperature": 0.3},
        }
        res = requests.post(url, json=body, timeout=20)
        if res.status_code == 200:
            cands = res.json().get("candidates", [])
            if cands:
                return cands[0]["content"]["parts"][0]["text"]
        elif res.status_code == 429:
            print("[NEWS AI] Rate limit - kihagyva")
    except Exception as e:
        print(f"[NEWS AI] Kivétel: {e}")

    return None

def parse_ai_analysis(text: str) -> dict:
    """Kibontja az AI strukturált válaszát."""
    result = {
        "hatas":    "BIZONYTALAN",
        "erintett": "általános piac",
        "surgeseg": "FIGYELD",
        "indoklas": "",
        "teendo":   "",
    }
    if not text:
        return result

    mapping = {
        "HATÁS:":     "hatas",
        "ÉRINTETT:":  "erintett",
        "SÜRGESSÉG:": "surgeseg",
        "INDOKLÁS:":  "indoklas",
        "TEENDŐ:":    "teendo",
    }
    for line in text.strip().split("\n"):
        line = line.strip()
        for key, field in mapping.items():
            if line.upper().startswith(key.upper()):
                result[field] = line[len(key):].strip()
    return result

# -----------------------------------------------------------------------
# DISCORD ÜZENET FORMÁZÁS
# -----------------------------------------------------------------------

HATAS_EMOJI = {
    "POZITÍV":    "📈",
    "NEGATÍV":    "📉",
    "SEMLEGES":   "➡️",
    "BIZONYTALAN": "❓",
}
SURGESEG_EMOJI = {
    "AZONNALI": "🚨",
    "MA":       "⚡",
    "FIGYELD":  "👀",
    "NEM FONTOS": "💤",
}
SOURCE_EMOJI = {
    "reddit":  "🟠",
    "steam":   "🎮",
    "twitter": "🐦",
}

def format_news_discord(news_item: dict, ai_analysis: dict | None) -> str:
    """Szépen formázott Discord üzenetet állít össze."""
    src_emoji  = SOURCE_EMOJI.get(news_item.get("type", ""), "📰")
    priority   = news_item.get("priority", "NORMAL")
    prio_str   = "🔴 **FONTOS HÍR**" if priority == "HIGH" else "📰 Piaci hír"

    msg = (
        f"{prio_str}\n"
        f"{'━'*32}\n"
        f"{src_emoji} **Forrás:** {news_item['source']}\n"
        f"📌 **{news_item['title'][:120]}**\n"
    )

    if news_item.get("text") and len(news_item["text"]) > 10:
        msg += f"> {news_item['text'][:200]}...\n"

    if news_item.get("url"):
        msg += f"🔗 {news_item['url']}\n"

    if ai_analysis:
        hatas_e  = HATAS_EMOJI.get(ai_analysis["hatas"].upper(), "❓")
        surg_e   = SURGESEG_EMOJI.get(ai_analysis["surgeseg"].upper(), "👀")
        msg += (
            f"\n**🤖 AI Elemzés:**\n"
            f"{hatas_e} Hatás: **{ai_analysis['hatas']}**\n"
            f"🎯 Érintett: `{ai_analysis['erintett']}`\n"
            f"{surg_e} Sürgősség: **{ai_analysis['surgeseg']}**\n"
        )
        if ai_analysis.get("indoklas"):
            msg += f"💡 {ai_analysis['indoklas']}\n"
        if ai_analysis.get("teendo"):
            msg += f"✅ **Teendő:** {ai_analysis['teendo']}\n"

    msg += f"\n{'━'*32}"
    return msg

# -----------------------------------------------------------------------
# FŐ FIGYELŐ LOOP - ezt kell a botba integrálni
# -----------------------------------------------------------------------

NEWS_CHECK_INTERVAL = 300   # 5 perc

async def news_monitor_loop(channel, gemini_key: str,
                             all_cases: list, skin_to_case: dict,
                             executor):
    """
    Főloop - ezt kell asyncio.ensure_future()-rel indítani a on_ready-ben.
    Paraméterek:
      channel     - Discord csatorna ahol küld
      gemini_key  - Gemini API kulcs
      all_cases   - ALL_CASES lista a főbotból
      skin_to_case- SKIN_TO_CASE dict a főbotból
      executor    - ThreadPoolExecutor a főbotból
    """
    print("[NEWS] Hírfigyelő elindult.")
    await channel.send(
        "📡 **Hírfigyelő aktív!**\n"
        "Forrásai: Reddit (r/GlobalOffensive, r/cs2, r/csgomarketforum) "
        "| Steam Official | Twitter\n"
        "Frissítés: 5 percenként"
    )

    # Induláskor betöltjük a már látott híreket (hogy ne küldje újra)
    _preload_seen(all_cases)

    while True:
        try:
            loop        = asyncio.get_running_loop()
            all_news    = []

            # Párhuzamos lekérés threadpool-ban
            reddit_news  = await loop.run_in_executor(executor, fetch_reddit_news)
            steam_news   = await loop.run_in_executor(executor, fetch_steam_news)
            twitter_news = await loop.run_in_executor(executor, fetch_twitter_news)

            all_news = reddit_news + steam_news + twitter_news

            # Rendezés: HIGH priority előre, utána timestamp szerint
            all_news.sort(key=lambda x: (
                0 if x["priority"] == "HIGH" else 1,
                -x.get("ts", 0)
            ))

            for news_item in all_news:
                # AI elemzés (csak ha van Gemini kulcs)
                ai_raw      = None
                ai_analysis = None
                if gemini_key:
                    ai_raw = await loop.run_in_executor(
                        executor,
                        analyze_news_with_ai,
                        news_item, gemini_key, all_cases, skin_to_case
                    )
                    if ai_raw:
                        ai_analysis = parse_ai_analysis(ai_raw)
                        # NEM FONTOS hírek kihagyása
                        if ai_analysis.get("surgeseg") == "NEM FONTOS":
                            continue
                        await asyncio.sleep(3)   # Gemini rate limit védelem

                # Discord üzenet küldése
                msg = format_news_discord(news_item, ai_analysis)
                if len(msg) <= 2000:
                    await channel.send(msg)
                else:
                    await channel.send(msg[:1990] + "...")

                await asyncio.sleep(1)   # Ne spammeljük a Discord-ot

        except Exception as e:
            print(f"[NEWS] Hiba a főloopban: {e}")

        await asyncio.sleep(NEWS_CHECK_INTERVAL)

def _preload_seen(all_cases: list):
    """
    Induláskor előtölti a seen_hashes-t a legújabb hírekből,
    hogy ne küldjön duplikátumot újraindítás után.
    """
    try:
        for source in REDDIT_SOURCES:
            res = requests.get(source["url"], headers=REDDIT_HEADERS, timeout=8)
            if res.status_code == 200:
                posts = res.json().get("data", {}).get("children", [])
                for post in posts:
                    title = post.get("data", {}).get("title", "")
                    if title:
                        seen_news_hashes.add(_hash_news(title, source["name"]))
    except Exception:
        pass
