"""
======================================================
  WORLD NEWS BOT v2 — Canal @infosmondial
  FinancialJuice + ForexLive → Filtre IA → Telegram
======================================================
Améliorations v2 :
  1. Multi-sources (FinancialJuice + ForexLive)
  2. Anti-doublons inter-sources
  3. Vérification rapide (30s) en heures de trading
  4. Score de confiance sur l'analyse
  5. Résumé quotidien à 20h00 UTC
  6. Alertes calendrier économique (NFP, CPI, FOMC...)
  7. Score de force du signal (Fortement/Légèrement)
  8. Boutons interactifs 👍 👎 sous chaque news
"""

import os
import sys
import json
import time
import hashlib
import logging
import requests
import feedparser
from datetime import datetime, timezone
from groq import Groq
from dotenv import load_dotenv
from difflib import SequenceMatcher

load_dotenv()

# Force UTF-8 sur Windows
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

# ─── CONFIG ────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHANNEL   = os.environ.get("TELEGRAM_CHANNEL", "@infosmondial")
GROQ_API_KEY       = os.environ.get("GROQ_API_KEY", "")

CHECK_INTERVAL_NORMAL  = 120   # 2 min hors trading
CHECK_INTERVAL_TRADING = 30    # 30s pendant sessions actives
SEEN_FILE              = "seen_news.json"
DAILY_SUMMARY_HOUR     = 20    # 20h00 UTC
SIMILARITY_THRESHOLD   = 0.75  # Seuil anti-doublons

# Sources RSS
RSS_SOURCES = [
    {
        "name": "FinancialJuice",
        "url": "https://www.financialjuice.com/feed.ashx?xy=free",
        "emoji": "🧃"
    },
    {
        "name": "ForexLive",
        "url": "https://www.forexlive.com/feed/news",
        "emoji": "📡"
    },
]

# Événements calendrier économique à surveiller
ECONOMIC_EVENTS = [
    {"name": "NFP",        "keywords": ["non-farm payroll", "nonfarm", "NFP"],         "impact": "🔴 TRÈS HAUTE"},
    {"name": "CPI",        "keywords": ["CPI", "consumer price", "inflation"],          "impact": "🔴 TRÈS HAUTE"},
    {"name": "FOMC",       "keywords": ["FOMC", "federal reserve", "fed decision"],     "impact": "🔴 TRÈS HAUTE"},
    {"name": "PIB US",     "keywords": ["GDP", "gross domestic product"],               "impact": "🟠 HAUTE"},
    {"name": "PPI",        "keywords": ["PPI", "producer price"],                       "impact": "🟠 HAUTE"},
    {"name": "PCE",        "keywords": ["PCE", "personal consumption expenditure"],     "impact": "🟠 HAUTE"},
    {"name": "Emploi US",  "keywords": ["jobless claims", "unemployment"],              "impact": "🟡 MOYENNE"},
    {"name": "PMI",        "keywords": ["PMI", "purchasing managers"],                  "impact": "🟡 MOYENNE"},
    {"name": "ISM",        "keywords": ["ISM", "institute for supply"],                 "impact": "🟡 MOYENNE"},
]

# ─── LOGGING ───────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("bot.log", encoding="utf-8"),
        logging.StreamHandler()
    ]
)
log = logging.getLogger(__name__)

groq_client = Groq(api_key=GROQ_API_KEY)


# ══════════════════════════════════════════════════════
# UTILITAIRES
# ══════════════════════════════════════════════════════
def load_seen() -> dict:
    if os.path.exists(SEEN_FILE):
        with open(SEEN_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            # Compatibilité avec l'ancien format (set simple)
            if isinstance(data, list):
                return {"seen": set(data), "daily_titles": [], "last_summary_date": ""}
            data["seen"] = set(data.get("seen", []))
            return data
    return {"seen": set(), "daily_titles": [], "last_summary_date": ""}

def save_seen(data: dict):
    to_save = {
        "seen": list(data["seen"]),
        "daily_titles": data.get("daily_titles", []),
        "last_summary_date": data.get("last_summary_date", "")
    }
    with open(SEEN_FILE, "w", encoding="utf-8") as f:
        json.dump(to_save, f, ensure_ascii=False)

def hash_news(title: str) -> str:
    return hashlib.md5(title.encode()).hexdigest()

def is_duplicate(new_title: str, seen_titles: list) -> bool:
    for old_title in seen_titles[-50:]:  # Compare avec les 50 dernières
        ratio = SequenceMatcher(None, new_title.lower(), old_title.lower()).ratio()
        if ratio >= SIMILARITY_THRESHOLD:
            return True
    return False

def is_trading_hours() -> bool:
    """Retourne True si on est en session Londres ou New York (7h-21h UTC)"""
    now_hour = datetime.now(timezone.utc).hour
    return 7 <= now_hour <= 21

def get_check_interval() -> int:
    if is_trading_hours():
        return CHECK_INTERVAL_TRADING
    return CHECK_INTERVAL_NORMAL

def is_economic_event(title: str, summary: str) -> dict | None:
    """Détecte si la news mentionne un événement calendrier économique"""
    text = (title + " " + summary).lower()
    for event in ECONOMIC_EVENTS:
        for kw in event["keywords"]:
            if kw.lower() in text:
                return event
    return None


# ══════════════════════════════════════════════════════
# RÉCUPÉRATION DES FLUX RSS
# ══════════════════════════════════════════════════════
def fetch_all_news() -> list[dict]:
    all_news = []
    for source in RSS_SOURCES:
        try:
            feed = feedparser.parse(source["url"])
            for entry in feed.entries[:15]:
                all_news.append({
                    "title":   entry.get("title", ""),
                    "summary": entry.get("summary", entry.get("title", "")),
                    "link":    entry.get("link", ""),
                    "time":    entry.get("published", ""),
                    "source":  source["name"],
                    "emoji":   source["emoji"],
                })
            log.info(f"✅ {len(feed.entries[:15])} news depuis {source['name']}")
        except Exception as e:
            log.error(f"❌ Erreur RSS {source['name']}: {e}")
    return all_news


# ══════════════════════════════════════════════════════
# ANALYSE IA AVEC GROQ
# ══════════════════════════════════════════════════════
SYSTEM_PROMPT = """Tu es un analyste financier senior expert en Gold (XAU), Silver (XAG), Bitcoin (BTC) et Dollar (USD/DXY).

Analyse la news et réponds UNIQUEMENT en JSON valide, sans texte avant ou après, sans backticks.

Format :
{
  "pertinent": true ou false,
  "titre_fr": "Traduction fidèle et naturelle du titre en français",
  "actifs": ["GOLD", "SILVER", "BTC", "USD"],
  "biais": {
    "GOLD": "FORTEMENT_BULLISH" ou "BULLISH" ou "NEUTRE" ou "BEARISH" ou "FORTEMENT_BEARISH",
    "SILVER": "FORTEMENT_BULLISH" ou "BULLISH" ou "NEUTRE" ou "BEARISH" ou "FORTEMENT_BEARISH",
    "BTC": "FORTEMENT_BULLISH" ou "BULLISH" ou "NEUTRE" ou "BEARISH" ou "FORTEMENT_BEARISH",
    "USD": "FORTEMENT_BULLISH" ou "BULLISH" ou "NEUTRE" ou "BEARISH" ou "FORTEMENT_BEARISH"
  },
  "confiance": 70,
  "explication": "Explication courte en français (max 2 phrases)",
  "urgence": "HAUTE" ou "MOYENNE" ou "FAIBLE"
}

Règles :
- pertinent = true seulement si impact DIRECT sur Gold, Silver, BTC ou USD
- titre_fr = toujours traduit en français, naturellement, même si pertinent = false
- confiance = nombre entier entre 50 et 95 (ta certitude sur l'analyse)
- FORTEMENT_BULLISH/BEARISH = impact majeur confirmé (Fed, guerre, crise...)
- BULLISH/BEARISH = impact modéré probable
- N'inclure dans actifs que ceux réellement impactés
- Si pertinent = false, les autres champs peuvent être vides"""


def analyze_news(title: str, summary: str) -> dict | None:
    try:
        response = groq_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": f"Titre: {title}\nContenu: {summary}"}
            ],
            temperature=0.1,
            max_tokens=400,
        )
        raw = response.choices[0].message.content.strip()
        raw = raw.replace("```json", "").replace("```", "").strip()
        return json.loads(raw)
    except Exception as e:
        log.error(f"❌ Erreur analyse IA: {e}")
        return None


# ══════════════════════════════════════════════════════
# FORMATAGE DES MESSAGES
# ══════════════════════════════════════════════════════
BIAIS_FORMAT = {
    "FORTEMENT_BULLISH": "🟢🟢 FORTEMENT BULLISH",
    "BULLISH":           "🟢 BULLISH",
    "NEUTRE":            "🟡 NEUTRE",
    "BEARISH":           "🔴 BEARISH",
    "FORTEMENT_BEARISH": "🔴🔴 FORTEMENT BEARISH",
}

ACTIF_EMOJI = {
    "GOLD":   "🥇 Gold",
    "SILVER": "🥈 Silver",
    "BTC":    "₿ Bitcoin",
    "USD":    "💵 Dollar",
}

URGENCE_EMOJI = {
    "HAUTE":   "🚨",
    "MOYENNE": "⚡",
    "FAIBLE":  "📌",
}

def format_message(news: dict, analysis: dict) -> str:
    urgence     = analysis.get("urgence", "FAIBLE")
    actifs      = analysis.get("actifs", [])
    biais       = analysis.get("biais", {})
    explication = analysis.get("explication", "")
    confiance   = analysis.get("confiance", 70)
    source_emoji = news.get("emoji", "📰")
    source_name  = news.get("source", "News")

    emoji_urg = URGENCE_EMOJI.get(urgence, "📌")
    now = datetime.now(timezone.utc).strftime("%d/%m/%Y %H:%M UTC")

    # Barre de confiance visuelle
    filled = round(confiance / 10)
    bar = "█" * filled + "░" * (10 - filled)
    conf_line = f"🎯 Confiance : {bar} {confiance}%"

    lines = [
        f"{emoji_urg} *MARKET NEWS — {urgence}*",
        f"",
        f"{source_emoji} *{analysis.get('titre_fr', news['title'])}*",
        f"",
        f"📊 *Impact sur les marchés :*",
    ]

    for actif in actifs:
        b = biais.get(actif, "NEUTRE")
        lines.append(f"  • {ACTIF_EMOJI.get(actif, actif)} → {BIAIS_FORMAT.get(b, b)}")

    if explication:
        lines += ["", f"💡 _{explication}_"]

    lines += ["", conf_line]

    if news.get("link"):
        lines += ["", f"🔗 [Source {source_name}]({news['link']})"]

    lines += ["", f"🕐 _{now}_", "", "━━━━━━━━━━━━━━━━━━━━"]

    return "\n".join(lines)


def format_daily_summary(daily_titles: list) -> str:
    now = datetime.now(timezone.utc).strftime("%d/%m/%Y")
    count = len(daily_titles)

    lines = [
        f"📋 *RÉSUMÉ QUOTIDIEN — {now}*",
        f"",
        f"Aujourd'hui, *{count} news* pertinentes ont été publiées sur Gold, Silver, Bitcoin et Dollar.",
        f"",
        f"📌 *Dernières headlines :*",
    ]

    for title in daily_titles[-5:]:
        lines.append(f"  • {title}")

    lines += [
        "",
        "━━━━━━━━━━━━━━━━━━━━",
        "🌙 _Bonne nuit — le bot reprend sa veille demain !_",
        "━━━━━━━━━━━━━━━━━━━━"
    ]
    return "\n".join(lines)


def format_event_alert(event: dict, minutes_before: int = 30) -> str:
    return (
        f"⏰ *ALERTE CALENDRIER ÉCONOMIQUE*\n\n"
        f"📅 Dans ~{minutes_before} minutes : *{event['name']}*\n\n"
        f"Impact attendu : {event['impact']}\n\n"
        f"💡 _Restez vigilants — fort mouvement possible sur Gold et Dollar !_\n\n"
        f"━━━━━━━━━━━━━━━━━━━━"
    )


# ══════════════════════════════════════════════════════
# ENVOI TELEGRAM
# ══════════════════════════════════════════════════════
def send_message(text: str, reply_markup: dict = None) -> int | None:
    """Envoie un message et retourne le message_id"""
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id":    TELEGRAM_CHANNEL,
        "text":       text,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True,
    }
    if reply_markup:
        payload["reply_markup"] = json.dumps(reply_markup)
    try:
        resp = requests.post(url, json=payload, timeout=10)
        if resp.status_code == 200:
            log.info("✅ Message envoyé sur Telegram")
            return resp.json().get("result", {}).get("message_id")
        else:
            log.error(f"❌ Telegram erreur {resp.status_code}: {resp.text}")
            return None
    except Exception as e:
        log.error(f"❌ Erreur envoi Telegram: {e}")
        return None


def get_feedback_buttons() -> dict:
    """Boutons 👍 👎 sous chaque news"""
    return {
        "inline_keyboard": [[
            {"text": "👍 Utile", "callback_data": "feedback_utile"},
            {"text": "👎 Pas utile", "callback_data": "feedback_inutile"}
        ]]
    }


# ══════════════════════════════════════════════════════
# RÉSUMÉ QUOTIDIEN
# ══════════════════════════════════════════════════════
def should_send_summary(data: dict) -> bool:
    now = datetime.now(timezone.utc)
    today = now.strftime("%Y-%m-%d")
    if now.hour == DAILY_SUMMARY_HOUR and data.get("last_summary_date") != today:
        return True
    return False


# ══════════════════════════════════════════════════════
# BOUCLE PRINCIPALE
# ══════════════════════════════════════════════════════
def run():
    log.info(f"🚀 World News Bot v2 démarré — Canal: {TELEGRAM_CHANNEL}")
    data = load_seen()
    recent_titles = []  # Pour anti-doublons inter-sources

    while True:
        now = datetime.now(timezone.utc)

        # ── Résumé quotidien ──
        if should_send_summary(data):
            if data.get("daily_titles"):
                summary = format_daily_summary(data["daily_titles"])
                send_message(summary)
            data["last_summary_date"] = now.strftime("%Y-%m-%d")
            data["daily_titles"] = []
            save_seen(data)

        # ── Récupération des news ──
        log.info("🔍 Vérification des nouvelles news...")
        all_news = fetch_all_news()

        for news in all_news:
            news_id = hash_news(news["title"])

            if news_id in data["seen"]:
                continue

            # Anti-doublons inter-sources
            if is_duplicate(news["title"], recent_titles):
                log.info(f"⏭️ Doublon détecté — ignoré : {news['title'][:60]}")
                data["seen"].add(news_id)
                continue

            log.info(f"📌 Nouvelle news : {news['title'][:80]}...")

            # Détection événement calendrier
            event = is_economic_event(news["title"], news["summary"])
            if event:
                alert = format_event_alert(event, minutes_before=0)
                send_message(alert)

            # Analyse IA
            analysis = analyze_news(news["title"], news["summary"])
            if not analysis or not analysis.get("pertinent", False):
                log.info("⏭️ Non pertinente — ignorée")
                data["seen"].add(news_id)
                recent_titles.append(news["title"])
                continue

            # Formatage + envoi avec boutons
            message = format_message(news, analysis)
            buttons = get_feedback_buttons()
            send_message(message, reply_markup=buttons)

            # Ajout au résumé du jour
            data["daily_titles"].append(news["title"])
            recent_titles.append(news["title"])
            if len(recent_titles) > 100:
                recent_titles = recent_titles[-100:]

            data["seen"].add(news_id)
            save_seen(data)
            time.sleep(2)

        save_seen(data)
        interval = get_check_interval()
        session = "TRADING ACTIF 🔥" if is_trading_hours() else "hors trading"
        log.info(f"⏳ [{session}] Prochaine vérification dans {interval}s...")
        time.sleep(interval)


if __name__ == "__main__":
    run()
