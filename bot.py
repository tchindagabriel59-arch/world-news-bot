"""
======================================================
  WORLD NEWS BOT v3 — Canal @infosmondial
  Bot de News Trading Professionnel
======================================================
Fonctionnalités v3 :
  1. Filtre 3 étoiles uniquement (fort impact)
  2. Briefing pré-session Londres (7h00 UTC)
  3. Briefing pré-session New York (12h30 UTC)
  4. Briefing clôture (21h00 UTC)
  5. Surveillance Trump temps réel
  6. Mémoire contextuelle (semaines/mois)
  7. Analyse fondamentale complète
"""

import os
import sys
import json
import time
import hashlib
import logging
import requests
import feedparser
from datetime import datetime, timezone, timedelta
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

CHECK_INTERVAL_TRADING = 30
CHECK_INTERVAL_NORMAL  = 120
SIMILARITY_THRESHOLD   = 0.75
SEEN_FILE              = "seen_news.json"
CONTEXT_FILE           = "market_context.json"
MAX_CONTEXT_EVENTS     = 50  # Garde les 50 derniers événements en mémoire

# Sources RSS
RSS_SOURCES = [
    {"name": "FinancialJuice", "url": "https://www.financialjuice.com/feed.ashx?xy=free", "emoji": "🧃"},
    {"name": "ForexLive",      "url": "https://www.forexlive.com/feed/news",              "emoji": "📡"},
]

# ─── ÉVÉNEMENTS 3 ÉTOILES ──────────────────────────────
HIGH_IMPACT_KEYWORDS = [
    # Banques centrales
    "fed", "federal reserve", "fomc", "powell", "rate decision", "interest rate",
    "bce", "ecb", "lagarde", "rate hike", "rate cut", "monetary policy",
    "quantitative", "QE", "QT", "tapering", "hawkish", "dovish",

    # Données économiques majeures
    "nonfarm payroll", "non-farm", "NFP", "CPI", "consumer price",
    "inflation", "PPI", "producer price", "PCE", "GDP", "gross domestic",
    "unemployment", "jobless claims", "ISM", "PMI", "retail sales",
    "durable goods", "trade balance", "current account",

    # Trump & politique US
    "trump", "tariff", "sanction", "executive order", "white house",
    "treasury", "debt ceiling", "government shutdown", "us budget",

    # Géopolitique & crises
    "war", "guerre", "nuclear", "nucléaire", "iran", "russia", "ukraine",
    "china", "taiwan", "north korea", "missile", "attack", "ceasefire",
    "oil embargo", "opec", "crude oil spike",

    # Or & métaux
    "gold reserve", "central bank gold", "fort knox", "gold standard",
    "silver squeeze", "comex", "precious metals",

    # Bitcoin & crypto majeur
    "bitcoin ETF", "BTC ETF", "SEC bitcoin", "crypto ban", "crypto regulation",
    "coinbase", "binance lawsuit", "crypto hack major",

    # Crises financières
    "bank collapse", "bank failure", "bank run", "systemic risk",
    "recession", "depression", "market crash", "black monday",
    "credit default", "sovereign debt", "IMF", "world bank",
]

# Mots-clés Trump spécifiques
TRUMP_KEYWORDS = [
    "trump", "donald trump", "president trump", "trump says", "trump announces",
    "trump threatens", "trump signs", "trump tariff", "trump tweet", "truth social",
    "mar-a-lago", "white house statement"
]

# Sessions de trading
SESSIONS = {
    "LONDON_OPEN":  {"hour": 7,  "minute": 0,  "label": "OUVERTURE LONDRES 🇬🇧"},
    "NEWYORK_OPEN": {"hour": 12, "minute": 30, "label": "OUVERTURE NEW YORK 🇺🇸"},
    "CLOSE":        {"hour": 21, "minute": 0,  "label": "CLÔTURE DES MARCHÉS 🌙"},
}

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
# GESTION DE LA MÉMOIRE CONTEXTUELLE
# ══════════════════════════════════════════════════════
def load_context() -> dict:
    if os.path.exists(CONTEXT_FILE):
        with open(CONTEXT_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {
        "events": [],           # Historique des événements importants
        "briefings_sent": {},   # Briefings déjà envoyés aujourd'hui
        "last_briefing": {}     # Dernier briefing envoyé par session
    }

def save_context(ctx: dict):
    with open(CONTEXT_FILE, "w", encoding="utf-8") as f:
        json.dump(ctx, f, ensure_ascii=False, indent=2)

def add_to_context(ctx: dict, event: dict):
    """Ajoute un événement à la mémoire contextuelle"""
    event["timestamp"] = datetime.now(timezone.utc).isoformat()
    ctx["events"].append(event)
    # Garde seulement les MAX_CONTEXT_EVENTS derniers
    if len(ctx["events"]) > MAX_CONTEXT_EVENTS:
        ctx["events"] = ctx["events"][-MAX_CONTEXT_EVENTS:]
    save_context(ctx)

def get_context_summary(ctx: dict) -> str:
    """Génère un résumé du contexte pour l'IA"""
    events = ctx.get("events", [])
    if not events:
        return "Aucun événement récent enregistré."

    lines = []
    for e in events[-20:]:  # 20 derniers événements
        ts = e.get("timestamp", "")[:10]
        title = e.get("title", "")[:100]
        biais = e.get("biais_summary", "")
        lines.append(f"[{ts}] {title} → {biais}")

    return "\n".join(lines)


# ══════════════════════════════════════════════════════
# UTILITAIRES
# ══════════════════════════════════════════════════════
def load_seen() -> set:
    if os.path.exists(SEEN_FILE):
        with open(SEEN_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, list):
                return set(data)
            return set(data.get("seen", []))
    return set()

def save_seen(seen: set):
    with open(SEEN_FILE, "w", encoding="utf-8") as f:
        json.dump(list(seen), f)

def hash_news(title: str) -> str:
    return hashlib.md5(title.encode()).hexdigest()

def is_duplicate(new_title: str, seen_titles: list) -> bool:
    for old_title in seen_titles[-50:]:
        ratio = SequenceMatcher(None, new_title.lower(), old_title.lower()).ratio()
        if ratio >= SIMILARITY_THRESHOLD:
            return True
    return False

def is_trading_hours() -> bool:
    now_hour = datetime.now(timezone.utc).hour
    return 7 <= now_hour <= 21

def get_check_interval() -> int:
    return CHECK_INTERVAL_TRADING if is_trading_hours() else CHECK_INTERVAL_NORMAL

def is_high_impact(title: str, summary: str) -> bool:
    """Vérifie si la news est à fort impact (3 étoiles)"""
    text = (title + " " + summary).lower()
    for kw in HIGH_IMPACT_KEYWORDS:
        if kw.lower() in text:
            return True
    return False

def is_trump_news(title: str, summary: str) -> bool:
    """Détecte si c'est une news Trump"""
    text = (title + " " + summary).lower()
    return any(kw.lower() in text for kw in TRUMP_KEYWORDS)

def should_send_briefing(ctx: dict, session_key: str) -> bool:
    """Vérifie si le briefing de cette session a déjà été envoyé aujourd'hui"""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    briefings = ctx.get("briefings_sent", {})
    key = f"{today}_{session_key}"
    return not briefings.get(key, False)

def mark_briefing_sent(ctx: dict, session_key: str):
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if "briefings_sent" not in ctx:
        ctx["briefings_sent"] = {}
    ctx["briefings_sent"][f"{today}_{session_key}"] = True
    save_context(ctx)


# ══════════════════════════════════════════════════════
# RÉCUPÉRATION DES FLUX RSS
# ══════════════════════════════════════════════════════
def fetch_all_news() -> list[dict]:
    all_news = []
    for source in RSS_SOURCES:
        try:
            feed = feedparser.parse(source["url"])
            for entry in feed.entries[:20]:
                all_news.append({
                    "title":   entry.get("title", ""),
                    "summary": entry.get("summary", entry.get("title", "")),
                    "link":    entry.get("link", ""),
                    "time":    entry.get("published", ""),
                    "source":  source["name"],
                    "emoji":   source["emoji"],
                })
            log.info(f"✅ {len(feed.entries[:20])} news depuis {source['name']}")
        except Exception as e:
            log.error(f"❌ Erreur RSS {source['name']}: {e}")
    return all_news


# ══════════════════════════════════════════════════════
# ANALYSE IA — NEWS INDIVIDUELLE
# ══════════════════════════════════════════════════════
NEWS_ANALYSIS_PROMPT = """Tu es un analyste financier senior spécialisé en Gold (XAU), Silver (XAG), Bitcoin (BTC) et Dollar (USD/DXY).

Réponds UNIQUEMENT en JSON valide, sans texte avant ou après, sans backticks.

Format :
{
  "pertinent": true ou false,
  "impact_stars": 1, 2 ou 3,
  "titre_fr": "Traduction naturelle en français",
  "actifs": ["GOLD", "SILVER", "BTC", "USD"],
  "biais": {
    "GOLD": "FORTEMENT_BULLISH" ou "BULLISH" ou "NEUTRE" ou "BEARISH" ou "FORTEMENT_BEARISH",
    "SILVER": "FORTEMENT_BULLISH" ou "BULLISH" ou "NEUTRE" ou "BEARISH" ou "FORTEMENT_BEARISH",
    "BTC": "FORTEMENT_BULLISH" ou "BULLISH" ou "NEUTRE" ou "BEARISH" ou "FORTEMENT_BEARISH",
    "USD": "FORTEMENT_BULLISH" ou "BULLISH" ou "NEUTRE" ou "BEARISH" ou "FORTEMENT_BEARISH"
  },
  "confiance": 80,
  "explication": "Explication en français (2-3 phrases max)",
  "urgence": "HAUTE" ou "MOYENNE" ou "FAIBLE",
  "trump_related": true ou false
}

Règles :
- impact_stars = 3 uniquement pour : Fed/BCE decisions, NFP, CPI, PIB, guerre, crise majeure, Trump annonces majeures, tariffs
- impact_stars = 2 pour : PMI, ISM, discours Fed/BCE, données économiques secondaires
- impact_stars = 1 pour : tout le reste
- pertinent = true seulement si impact DIRECT sur Gold, Silver, BTC ou USD
- trump_related = true si la news concerne Trump ou ses décisions"""


def analyze_news(title: str, summary: str) -> dict | None:
    try:
        response = groq_client.chat.completions.create(
            model="llama-3.1-8b-instant",
            messages=[
                {"role": "system", "content": NEWS_ANALYSIS_PROMPT},
                {"role": "user", "content": f"Titre: {title}\nContenu: {summary}"}
            ],
            temperature=0.1,
            max_tokens=500,
        )
        raw = response.choices[0].message.content.strip()
        raw = raw.replace("```json", "").replace("```", "").strip()
        return json.loads(raw)
    except Exception as e:
        log.error(f"❌ Erreur analyse IA: {e}")
        return None


# ══════════════════════════════════════════════════════
# GÉNÉRATION DES BRIEFINGS PAR SESSION
# ══════════════════════════════════════════════════════
def generate_briefing(session_label: str, context_summary: str) -> str | None:
    """Génère un briefing pré/post session avec l'IA"""

    now_utc = datetime.now(timezone.utc)
    date_str = now_utc.strftime("%d/%m/%Y")

    if "CLÔTURE" in session_label:
        prompt_type = "clôture — bilan de la journée et orientation pour demain"
    elif "NEW YORK" in session_label:
        prompt_type = "pré-session New York — analyse avant l'ouverture américaine"
    else:
        prompt_type = "pré-session Londres — analyse du matin européen"

    system = """Tu es un analyste financier senior qui rédige des briefings de trading professionnels.
Tu dois rédiger un briefing clair, précis et actionnable pour des traders sur Gold, Silver, Bitcoin et Dollar.
Réponds UNIQUEMENT en JSON valide sans backticks.

Format :
{
  "introduction": "Contexte général du marché en 2-3 phrases",
  "evenements_cles": ["événement 1", "événement 2", "événement 3"],
  "analyse_gold": "Analyse détaillée Gold avec biais directionnel",
  "analyse_dollar": "Analyse détaillée Dollar avec biais directionnel",
  "analyse_btc": "Analyse Bitcoin avec biais",
  "biais_session": {
    "GOLD": "HAUSSIER" ou "BAISSIER" ou "NEUTRE",
    "USD": "HAUSSIER" ou "BAISSIER" ou "NEUTRE",
    "BTC": "HAUSSIER" ou "BAISSIER" ou "NEUTRE"
  },
  "volatilite_attendue": "FORTE" ou "MODÉRÉE" ou "FAIBLE",
  "niveaux_attention": "Niveaux de prix ou événements à surveiller",
  "conclusion": "Phrase de conclusion avec recommandation générale"
}"""

    user = f"""Date : {date_str}
Type de briefing : {prompt_type}

Contexte des événements récents (du plus ancien au plus récent) :
{context_summary}

Rédige le briefing en français, en te basant sur TOUT ce contexte pour donner une analyse cohérente et complète."""

    try:
        response = groq_client.chat.completions.create(
            model="llama-3.1-8b-instant",
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user}
            ],
            temperature=0.3,
            max_tokens=1500,
        )
        raw = response.choices[0].message.content.strip()
        raw = raw.replace("```json", "").replace("```", "").strip()
        return json.loads(raw)
    except Exception as e:
        log.error(f"❌ Erreur génération briefing: {e}")
        return None


def format_briefing_message(session_label: str, briefing: dict) -> str:
    """Formate le briefing pour Telegram"""

    now = datetime.now(timezone.utc).strftime("%d/%m/%Y %H:%M UTC")

    biais_session = briefing.get("biais_session", {})
    volatilite = briefing.get("volatilite_attendue", "MODÉRÉE")

    BIAIS_EMOJI = {
        "HAUSSIER": "🟢 HAUSSIER",
        "BAISSIER": "🔴 BAISSIER",
        "NEUTRE":   "🟡 NEUTRE"
    }

    VOLATILITE_EMOJI = {
        "FORTE":    "🔴 FORTE",
        "MODÉRÉE":  "🟠 MODÉRÉE",
        "FAIBLE":   "🟢 FAIBLE"
    }

    events = briefing.get("evenements_cles", [])
    events_text = "\n".join([f"  • {e}" for e in events[:5]])

    lines = [
        f"━━━━━━━━━━━━━━━━━━━━━━━━",
        f"📊 *BRIEFING {session_label}*",
        f"🕐 _{now}_",
        f"━━━━━━━━━━━━━━━━━━━━━━━━",
        f"",
        f"🌍 *Contexte général :*",
        f"_{briefing.get('introduction', '')}_",
        f"",
        f"📌 *Événements clés :*",
        events_text,
        f"",
        f"🥇 *Gold :*",
        f"_{briefing.get('analyse_gold', '')}_",
        f"",
        f"💵 *Dollar (USD) :*",
        f"_{briefing.get('analyse_dollar', '')}_",
        f"",
        f"₿ *Bitcoin :*",
        f"_{briefing.get('analyse_btc', '')}_",
        f"",
        f"📊 *Biais pour cette session :*",
        f"  • 🥇 Gold → {BIAIS_EMOJI.get(biais_session.get('GOLD', 'NEUTRE'), '🟡 NEUTRE')}",
        f"  • 💵 Dollar → {BIAIS_EMOJI.get(biais_session.get('USD', 'NEUTRE'), '🟡 NEUTRE')}",
        f"  • ₿ Bitcoin → {BIAIS_EMOJI.get(biais_session.get('BTC', 'NEUTRE'), '🟡 NEUTRE')}",
        f"",
        f"⚡ *Volatilité attendue :* {VOLATILITE_EMOJI.get(volatilite, '🟠 MODÉRÉE')}",
        f"",
        f"👁️ *À surveiller :*",
        f"_{briefing.get('niveaux_attention', '')}_",
        f"",
        f"💡 *Conclusion :*",
        f"_{briefing.get('conclusion', '')}_",
        f"",
        f"━━━━━━━━━━━━━━━━━━━━━━━━",
    ]

    return "\n".join(lines)


# ══════════════════════════════════════════════════════
# FORMATAGE DES NEWS INDIVIDUELLES
# ══════════════════════════════════════════════════════
BIAIS_FORMAT = {
    "FORTEMENT_BULLISH": "🟢🟢 FORTEMENT HAUSSIER",
    "BULLISH":           "🟢 HAUSSIER",
    "NEUTRE":            "🟡 NEUTRE",
    "BEARISH":           "🔴 BAISSIER",
    "FORTEMENT_BEARISH": "🔴🔴 FORTEMENT BAISSIER",
}

ACTIF_EMOJI = {
    "GOLD":   "🥇 Gold",
    "SILVER": "🥈 Silver",
    "BTC":    "₿ Bitcoin",
    "USD":    "💵 Dollar",
}

def format_news_message(news: dict, analysis: dict) -> str:
    urgence      = analysis.get("urgence", "HAUTE")
    actifs       = analysis.get("actifs", [])
    biais        = analysis.get("biais", {})
    explication  = analysis.get("explication", "")
    confiance    = analysis.get("confiance", 80)
    trump        = analysis.get("trump_related", False)
    source_emoji = news.get("emoji", "📰")
    source_name  = news.get("source", "News")

    now = datetime.now(timezone.utc).strftime("%d/%m/%Y %H:%M UTC")
    filled = round(confiance / 10)
    bar = "█" * filled + "░" * (10 - filled)

    trump_tag = "🇺🇸 *TRUMP ALERT* | " if trump else ""

    lines = [
        f"🚨 {trump_tag}*IMPACT FORT — ⭐⭐⭐*",
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

    lines += [
        "",
        f"🎯 Confiance : {bar} {confiance}%",
    ]

    if news.get("link"):
        lines += ["", f"🔗 [Source {source_name}]({news['link']})"]

    lines += ["", f"🕐 _{now}_", "", "━━━━━━━━━━━━━━━━━━━━"]

    return "\n".join(lines)


# ══════════════════════════════════════════════════════
# ENVOI TELEGRAM
# ══════════════════════════════════════════════════════
def send_message(text: str) -> bool:
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id":    TELEGRAM_CHANNEL,
        "text":       text,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True,
    }
    try:
        resp = requests.post(url, json=payload, timeout=15)
        if resp.status_code == 200:
            log.info("✅ Message envoyé sur Telegram")
            return True
        else:
            log.error(f"❌ Telegram erreur {resp.status_code}: {resp.text[:200]}")
            return False
    except Exception as e:
        log.error(f"❌ Erreur envoi Telegram: {e}")
        return False


# ══════════════════════════════════════════════════════
# VÉRIFICATION DES SESSIONS
# ══════════════════════════════════════════════════════
def check_sessions(ctx: dict):
    """Vérifie si c'est l'heure d'envoyer un briefing de session"""
    now = datetime.now(timezone.utc)

    for session_key, session in SESSIONS.items():
        if now.hour == session["hour"] and now.minute == session["minute"]:
            if should_send_briefing(ctx, session_key):
                log.info(f"📊 Génération du briefing {session['label']}...")
                context_summary = get_context_summary(ctx)
                briefing = generate_briefing(session["label"], context_summary)
                if briefing:
                    message = format_briefing_message(session["label"], briefing)
                    if send_message(message):
                        mark_briefing_sent(ctx, session_key)
                        log.info(f"✅ Briefing {session['label']} envoyé !")


# ══════════════════════════════════════════════════════
# BOUCLE PRINCIPALE
# ══════════════════════════════════════════════════════
def run():
    log.info(f"🚀 World News Bot v3 PRO démarré — Canal: {TELEGRAM_CHANNEL}")
    seen = load_seen()
    ctx = load_context()
    recent_titles = []

    while True:
        now = datetime.now(timezone.utc)

        # ── Vérification des briefings de session ──
        check_sessions(ctx)

        # ── Récupération des news ──
        log.info("🔍 Vérification des nouvelles news...")
        all_news = fetch_all_news()

        analyses_this_cycle = 0
        MAX_ANALYSES_PER_CYCLE = 5

        for news in all_news:
            if analyses_this_cycle >= MAX_ANALYSES_PER_CYCLE:
                log.info("⏸️ Limite d'analyses atteinte pour ce cycle — pause au prochain")
                break
            news_id = hash_news(news["title"])

            if news_id in seen:
                continue

            # Anti-doublons
            if is_duplicate(news["title"], recent_titles):
                log.info(f"⏭️ Doublon — ignoré")
                seen.add(news_id)
                continue

            title   = news["title"]
            summary = news["summary"]

            # Filtre pré-IA : uniquement news à fort impact potentiel
            trump_flag = is_trump_news(title, summary)
            high_flag  = is_high_impact(title, summary)

            if not high_flag and not trump_flag:
                log.info(f"⏭️ Impact faible — ignorée : {title[:60]}")
                seen.add(news_id)
                recent_titles.append(title)
                continue

            log.info(f"⭐ News fort impact détectée : {title[:80]}")

            # Analyse IA complète
            time.sleep(3)  # Délai pour éviter le rate limit Groq
            analysis = analyze_news(title, summary)
            analyses_this_cycle += 1

            if not analysis:
                seen.add(news_id)
                continue

            # Filtre final : seulement 3 étoiles ET pertinent pour nos actifs
            stars = analysis.get("impact_stars", 1)
            pertinent = analysis.get("pertinent", False)

            if stars < 3 or not pertinent:
                log.info(f"⏭️ {stars} étoile(s) — sous le seuil 3 étoiles, ignorée")
                seen.add(news_id)
                recent_titles.append(title)
                continue

            # Ajouter à la mémoire contextuelle
            biais_summary = " | ".join([
                f"{k}:{v}" for k, v in analysis.get("biais", {}).items()
                if k in analysis.get("actifs", [])
            ])
            add_to_context(ctx, {
                "title": analysis.get("titre_fr", title),
                "biais_summary": biais_summary,
                "urgence": analysis.get("urgence", ""),
                "trump": trump_flag
            })

            # Formatage et envoi
            message = format_news_message(news, analysis)
            send_message(message)

            recent_titles.append(title)
            if len(recent_titles) > 100:
                recent_titles = recent_titles[-100:]

            seen.add(news_id)
            save_seen(seen)
            time.sleep(2)

        save_seen(seen)
        interval = get_check_interval()
        session_status = "TRADING ACTIF 🔥" if is_trading_hours() else "hors trading"
        log.info(f"⏳ [{session_status}] Prochaine vérification dans {interval}s...")
        time.sleep(interval)


if __name__ == "__main__":
    run()
