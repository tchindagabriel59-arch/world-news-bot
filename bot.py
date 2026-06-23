"""
======================================================
  WORLD NEWS BOT — Telegram Canal de Gabriel
  FinancialJuice → Filtre IA → Canal Telegram
======================================================
Filtre les news qui impactent : Gold, Silver, BTC, USD
Analyse le biais : Bullish / Bearish / Neutre
"""

import os
import sys
import json
import time
import hashlib
import logging
import requests
import feedparser
from datetime import datetime
from groq import Groq
from dotenv import load_dotenv

# Charge les variables du fichier .env dans l'environnement
load_dotenv()

# Force l'UTF-8 sur la console Windows pour que les emojis s'affichent sans erreur
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

# ─── CONFIG ────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "COLLE_TON_TOKEN_ICI")
TELEGRAM_CHANNEL    = os.environ.get("TELEGRAM_CHANNEL", "@WorldNewsGabriel")  # Remplace par ton vrai @username
GROQ_API_KEY        = os.environ.get("GROQ_API_KEY", "COLLE_TA_CLE_GROQ_ICI")
CHECK_INTERVAL      = 120   # Vérification toutes les 2 minutes
SEEN_FILE           = "seen_news.json"
RSS_URL             = "https://www.financialjuice.com/feed.ashx?xy=free"

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

# ─── CLIENTS ───────────────────────────────────────────
groq_client = Groq(api_key=GROQ_API_KEY)


# ══════════════════════════════════════════════════════
# 1. CHARGEMENT / SAUVEGARDE DES NEWS DÉJÀ VUES
# ══════════════════════════════════════════════════════
def load_seen() -> set:
    if os.path.exists(SEEN_FILE):
        with open(SEEN_FILE, "r") as f:
            return set(json.load(f))
    return set()

def save_seen(seen: set):
    with open(SEEN_FILE, "w") as f:
        json.dump(list(seen), f)

def hash_news(title: str) -> str:
    return hashlib.md5(title.encode()).hexdigest()


# ══════════════════════════════════════════════════════
# 2. RÉCUPÉRATION DU FLUX RSS FINANCIALJUICE
# ══════════════════════════════════════════════════════
def fetch_news() -> list[dict]:
    try:
        feed = feedparser.parse(RSS_URL)
        news_list = []
        for entry in feed.entries[:20]:  # On prend les 20 dernières
            news_list.append({
                "title":   entry.get("title", ""),
                "summary": entry.get("summary", entry.get("title", "")),
                "link":    entry.get("link", ""),
                "time":    entry.get("published", ""),
            })
        log.info(f"✅ {len(news_list)} news récupérées depuis FinancialJuice")
        return news_list
    except Exception as e:
        log.error(f"❌ Erreur RSS FinancialJuice: {e}")
        return []


# ══════════════════════════════════════════════════════
# 3. ANALYSE IA AVEC GROQ / LLAMA
# ══════════════════════════════════════════════════════
SYSTEM_PROMPT = """Tu es un analyste financier expert en Gold (XAU), Silver (XAG), Bitcoin (BTC) et Dollar (USD/DXY).

Ta mission : analyser une news économique/politique et répondre UNIQUEMENT en JSON valide, sans aucun texte avant ou après.

Format de réponse :
{
  "pertinent": true ou false,
  "actifs": ["GOLD", "SILVER", "BTC", "USD"],
  "biais": {
    "GOLD": "BULLISH" ou "BEARISH" ou "NEUTRE",
    "SILVER": "BULLISH" ou "BEARISH" ou "NEUTRE",
    "BTC": "BULLISH" ou "BEARISH" ou "NEUTRE",
    "USD": "BULLISH" ou "BEARISH" ou "NEUTRE"
  },
  "explication": "Explication courte en français (max 2 phrases) du pourquoi de l'impact",
  "urgence": "HAUTE" ou "MOYENNE" ou "FAIBLE"
}

Règles :
- pertinent = true seulement si la news impacte DIRECTEMENT Gold, Silver, BTC ou USD
- Exemples de news pertinentes : Fed, taux d'intérêt, inflation, CPI, NFP, guerre, crise bancaire, ETF BTC, réglementation crypto, DXY, dollar...
- Exemples de news NON pertinentes : résultats d'entreprises tech, sport, météo...
- N'inclure dans "actifs" que ceux réellement impactés
- Si pertinent = false, les autres champs peuvent être vides"""


def analyze_news(title: str, summary: str) -> dict | None:
    try:
        prompt = f"News à analyser :\nTitre: {title}\nContenu: {summary}"
        response = groq_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user",   "content": prompt}
            ],
            temperature=0.1,
            max_tokens=400,
        )
        raw = response.choices[0].message.content.strip()
        # Nettoyage au cas où le modèle ajoute des backticks
        raw = raw.replace("```json", "").replace("```", "").strip()
        return json.loads(raw)
    except Exception as e:
        log.error(f"❌ Erreur analyse IA: {e}")
        return None


# ══════════════════════════════════════════════════════
# 4. FORMATAGE DU MESSAGE TELEGRAM
# ══════════════════════════════════════════════════════
EMOJI_BIAIS = {
    "BULLISH": "🟢 BULLISH",
    "BEARISH": "🔴 BEARISH",
    "NEUTRE":  "🟡 NEUTRE",
}

EMOJI_ACTIF = {
    "GOLD":   "🥇 Gold",
    "SILVER": "🥈 Silver",
    "BTC":    "₿ Bitcoin",
    "USD":    "💵 Dollar (USD)",
}

EMOJI_URGENCE = {
    "HAUTE":   "🚨",
    "MOYENNE": "⚡",
    "FAIBLE":  "📌",
}

def format_message(news: dict, analysis: dict) -> str:
    urgence     = analysis.get("urgence", "FAIBLE")
    actifs      = analysis.get("actifs", [])
    biais       = analysis.get("biais", {})
    explication = analysis.get("explication", "")
    
    emoji_urg = EMOJI_URGENCE.get(urgence, "📌")
    now = datetime.utcnow().strftime("%d/%m/%Y %H:%M UTC")

    lines = [
        f"{emoji_urg} *MARKET NEWS — {urgence}*",
        f"",
        f"📰 *{news['title']}*",
        f"",
        f"📊 *Impact sur les marchés :*",
    ]

    for actif in actifs:
        b = biais.get(actif, "NEUTRE")
        lines.append(f"  • {EMOJI_ACTIF.get(actif, actif)} → {EMOJI_BIAIS.get(b, b)}")

    if explication:
        lines += ["", f"💡 _{explication}_"]

    if news.get("link"):
        lines += ["", f"🔗 [Source FinancialJuice]({news['link']})"]

    lines += ["", f"🕐 _{now}_", "", "━━━━━━━━━━━━━━━━━━━━"]

    return "\n".join(lines)


# ══════════════════════════════════════════════════════
# 5. ENVOI VERS TELEGRAM
# ══════════════════════════════════════════════════════
def send_to_telegram(message: str) -> bool:
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id":    TELEGRAM_CHANNEL,
        "text":       message,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True,
    }
    try:
        resp = requests.post(url, json=payload, timeout=10)
        if resp.status_code == 200:
            log.info("✅ Message envoyé sur Telegram")
            return True
        else:
            log.error(f"❌ Telegram API error {resp.status_code}: {resp.text}")
            return False
    except Exception as e:
        log.error(f"❌ Erreur envoi Telegram: {e}")
        return False


# ══════════════════════════════════════════════════════
# 6. BOUCLE PRINCIPALE
# ══════════════════════════════════════════════════════
def run():
    log.info("🚀 World News Bot démarré — Canal: " + TELEGRAM_CHANNEL)
    seen = load_seen()

    while True:
        log.info("🔍 Vérification des nouvelles news...")
        news_list = fetch_news()

        for news in news_list:
            news_id = hash_news(news["title"])

            # Déjà traitée ?
            if news_id in seen:
                continue

            log.info(f"📌 Nouvelle news détectée : {news['title'][:80]}...")

            # Analyse IA
            analysis = analyze_news(news["title"], news["summary"])
            if not analysis:
                seen.add(news_id)
                continue

            # Pertinente pour nos marchés ?
            if not analysis.get("pertinent", False):
                log.info("⏭️  Non pertinente pour Gold/Silver/BTC/USD — ignorée")
                seen.add(news_id)
                continue

            # Formatage et envoi
            message = format_message(news, analysis)
            success = send_to_telegram(message)

            if success:
                log.info(f"📤 Publiée : {news['title'][:60]}...")

            seen.add(news_id)
            save_seen(seen)

            # Petit délai entre chaque envoi pour éviter le spam
            time.sleep(2)

        save_seen(seen)
        log.info(f"⏳ Prochaine vérification dans {CHECK_INTERVAL} secondes...\n")
        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    run()
