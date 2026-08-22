"""
======================================================
  WORLD NEWS BOT v3 PRO — Canal @infosmondial
  Bot de News Trading Professionnel
======================================================
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

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHANNEL   = os.environ.get("TELEGRAM_CHANNEL", "@infosmondial")
GROQ_API_KEY       = os.environ.get("GROQ_API_KEY", "")

CHECK_INTERVAL_TRADING = 30
CHECK_INTERVAL_NORMAL  = 120
SIMILARITY_THRESHOLD   = 0.75
SEEN_FILE              = "seen_news.json"
CONTEXT_FILE           = "market_context.json"
MAX_CONTEXT_EVENTS     = 50
MODEL                  = "openai/gpt-oss-120b"

RSS_SOURCES = [
    {"name": "FinancialJuice", "url": "https://www.financialjuice.com/feed.ashx?xy=free", "emoji": "🧃"},
    {"name": "ForexLive",      "url": "https://www.forexlive.com/feed/news",              "emoji": "📡"},
]

HIGH_IMPACT_KEYWORDS = [
    "fed", "federal reserve", "fomc", "powell", "rate decision", "interest rate",
    "bce", "ecb", "lagarde", "rate hike", "rate cut", "monetary policy",
    "quantitative", "tapering", "hawkish", "dovish",
    "nonfarm payroll", "non-farm", "NFP", "CPI", "consumer price",
    "inflation", "PPI", "producer price", "PCE", "GDP", "gross domestic",
    "unemployment", "jobless claims", "ISM", "PMI", "retail sales",
    "durable goods", "trade balance",
    "trump", "tariff", "sanction", "executive order", "white house",
    "treasury", "debt ceiling", "government shutdown",
    "war", "guerre", "nuclear", "iran", "russia", "ukraine",
    "china", "taiwan", "north korea", "missile", "attack", "ceasefire",
    "opec", "crude oil",
    "gold reserve", "central bank gold", "precious metals",
    "bitcoin ETF", "BTC ETF", "SEC bitcoin", "crypto ban", "crypto regulation",
    "bank collapse", "bank failure", "systemic risk",
    "recession", "market crash", "IMF", "world bank",
]

TRUMP_KEYWORDS = [
    "trump", "donald trump", "president trump", "trump says", "trump announces",
    "trump threatens", "trump signs", "trump tariff", "truth social",
    "mar-a-lago", "white house statement"
]

SESSIONS = {
    "LONDON_OPEN":  {"hour": 7,  "minute": 0,  "label": "OUVERTURE LONDRES 🇬🇧"},
    "NEWYORK_OPEN": {"hour": 12, "minute": 30, "label": "OUVERTURE NEW YORK 🇺🇸"},
    "CLOSE":        {"hour": 21, "minute": 0,  "label": "CLOTURE DES MARCHES 🌙"},
}

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


def clean_json(raw: str) -> str:
    """Nettoie la réponse du modèle pour extraire le JSON valide"""
    if not raw or not raw.strip():
        return ""
    # Supprime les balises <think>...</think>
    if "<think>" in raw:
        raw = raw.split("</think>")[-1].strip()
    # Supprime les backticks markdown
    if "```" in raw:
        parts = raw.split("```")
        for part in parts:
            if part.startswith("json"):
                raw = part[4:].strip()
                break
            elif "{" in part:
                raw = part.strip()
                break
    # Extrait le JSON entre { et }
    start = raw.find("{")
    end = raw.rfind("}") + 1
    if start != -1 and end > start:
        raw = raw[start:end]
    return raw.strip()


def load_context() -> dict:
    if os.path.exists(CONTEXT_FILE):
        with open(CONTEXT_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"events": [], "briefings_sent": {}, "last_briefing": {}}

def save_context(ctx: dict):
    with open(CONTEXT_FILE, "w", encoding="utf-8") as f:
        json.dump(ctx, f, ensure_ascii=False, indent=2)

def add_to_context(ctx: dict, event: dict):
    event["timestamp"] = datetime.now(timezone.utc).isoformat()
    ctx["events"].append(event)
    if len(ctx["events"]) > MAX_CONTEXT_EVENTS:
        ctx["events"] = ctx["events"][-MAX_CONTEXT_EVENTS:]
    save_context(ctx)

def get_context_summary(ctx: dict) -> str:
    events = ctx.get("events", [])
    if not events:
        return "Aucun evenement recent enregistre."
    lines = []
    for e in events[-20:]:
        ts = e.get("timestamp", "")[:10]
        title = e.get("title", "")[:100]
        biais = e.get("biais_summary", "")
        lines.append(f"[{ts}] {title} -> {biais}")
    return "\n".join(lines)


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
    return 7 <= datetime.now(timezone.utc).hour <= 21

def get_check_interval() -> int:
    return CHECK_INTERVAL_TRADING if is_trading_hours() else CHECK_INTERVAL_NORMAL

def is_high_impact(title: str, summary: str) -> bool:
    text = (title + " " + summary).lower()
    return any(kw.lower() in text for kw in HIGH_IMPACT_KEYWORDS)

def is_trump_news(title: str, summary: str) -> bool:
    text = (title + " " + summary).lower()
    return any(kw.lower() in text for kw in TRUMP_KEYWORDS)

def should_send_briefing(ctx: dict, session_key: str) -> bool:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return not ctx.get("briefings_sent", {}).get(f"{today}_{session_key}", False)

def mark_briefing_sent(ctx: dict, session_key: str):
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if "briefings_sent" not in ctx:
        ctx["briefings_sent"] = {}
    ctx["briefings_sent"][f"{today}_{session_key}"] = True
    save_context(ctx)


def fetch_all_news() -> list:
    all_news = []
    for source in RSS_SOURCES:
        try:
            feed = feedparser.parse(source["url"])
            for entry in feed.entries[:20]:
                all_news.append({
                    "title":   entry.get("title", ""),
                    "summary": entry.get("summary", entry.get("title", "")),
                    "link":    entry.get("link", ""),
                    "source":  source["name"],
                    "emoji":   source["emoji"],
                })
            log.info(f"✅ {len(feed.entries[:20])} news depuis {source['name']}")
        except Exception as e:
            log.error(f"❌ Erreur RSS {source['name']}: {e}")
    return all_news


NEWS_PROMPT = """Tu es un analyste financier senior specialise en Gold (XAU), Silver (XAG), Bitcoin (BTC) et Dollar (USD).
Reponds UNIQUEMENT en JSON valide, sans texte avant ou apres, sans backticks, sans balises.

Format exact :
{"pertinent":true,"impact_stars":3,"titre_fr":"Traduction en francais","actifs":["GOLD","USD"],"biais":{"GOLD":"BULLISH","USD":"BEARISH"},"confiance":85,"explication":"Explication en francais en 2 phrases.","urgence":"HAUTE","trump_related":false}

Regles :
- impact_stars = 3 pour Fed/BCE/NFP/CPI/PIB/guerre/Trump majeur/tariffs
- impact_stars = 2 pour PMI/ISM/discours/donnees secondaires
- impact_stars = 1 pour le reste
- pertinent = true seulement si impact DIRECT sur Gold/Silver/BTC/USD
- Valeurs biais : FORTEMENT_BULLISH, BULLISH, NEUTRE, BEARISH, FORTEMENT_BEARISH"""


def analyze_news(title: str, summary: str) -> dict:
    try:
        response = groq_client.chat.completions.create(
            model=MODEL,
            messages=[
                {"role": "system", "content": NEWS_PROMPT},
                {"role": "user", "content": f"Titre: {title}\nContenu: {summary}"}
            ],
            temperature=0.1,
            max_tokens=400,
        )
        raw = response.choices[0].message.content.strip()
                raw = clean_json(raw)
        if not raw:
            return None
        return json.loads(raw)
    except Exception as e:
        log.error(f"❌ Erreur analyse IA: {e}")
        return None


BRIEFING_PROMPT = """Tu es un analyste financier senior qui redige des briefings de trading professionnels.
Reponds UNIQUEMENT en JSON valide, sans texte avant ou apres, sans backticks, sans balises.

Format exact :
{"introduction":"Contexte general en 2-3 phrases","evenements_cles":["event1","event2","event3"],"analyse_gold":"Analyse Gold avec biais","analyse_dollar":"Analyse Dollar avec biais","analyse_btc":"Analyse Bitcoin","biais_session":{"GOLD":"HAUSSIER","USD":"BAISSIER","BTC":"NEUTRE"},"volatilite_attendue":"FORTE","niveaux_attention":"Niveaux a surveiller","conclusion":"Conclusion avec recommandation"}

Valeurs biais_session : HAUSSIER, BAISSIER, NEUTRE
Valeurs volatilite_attendue : FORTE, MODEREE, FAIBLE"""


def generate_briefing(session_label: str, context_summary: str) -> dict:
    try:
        date_str = datetime.now(timezone.utc).strftime("%d/%m/%Y")
        if "CLOTURE" in session_label:
            prompt_type = "cloture — bilan du jour et orientation pour demain"
        elif "NEW YORK" in session_label:
            prompt_type = "pre-session New York — analyse avant ouverture americaine"
        else:
            prompt_type = "pre-session Londres — analyse du matin europeen"

        user = f"""Date : {date_str}
Type : {prompt_type}
Evenements recents :
{context_summary}
Redige le briefing en francais."""

        response = groq_client.chat.completions.create(
            model=MODEL,
            messages=[
                {"role": "system", "content": BRIEFING_PROMPT},
                {"role": "user", "content": user}
            ],
            temperature=0.3,
            max_tokens=1500,
        )
        raw = response.choices[0].message.content.strip()
                raw = clean_json(raw)
        if not raw:
            return None
        return json.loads(raw)
    except Exception as e:
        log.error(f"❌ Erreur generation briefing: {e}")
        return None


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

BIAIS_SESSION = {
    "HAUSSIER": "🟢 HAUSSIER",
    "BAISSIER": "🔴 BAISSIER",
    "NEUTRE":   "🟡 NEUTRE",
}

VOLATILITE = {
    "FORTE":    "🔴 FORTE",
    "MODEREE":  "🟠 MODEREE",
    "FAIBLE":   "🟢 FAIBLE",
}


def format_news_message(news: dict, analysis: dict) -> str:
    actifs      = analysis.get("actifs", [])
    biais       = analysis.get("biais", {})
    explication = analysis.get("explication", "")
    confiance   = analysis.get("confiance", 80)
    trump       = analysis.get("trump_related", False)
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
        f"📊 *Impact sur les marches :*",
    ]
    for actif in actifs:
        b = biais.get(actif, "NEUTRE")
        lines.append(f"  • {ACTIF_EMOJI.get(actif, actif)} → {BIAIS_FORMAT.get(b, b)}")
    if explication:
        lines += ["", f"💡 _{explication}_"]
    lines += ["", f"🎯 Confiance : {bar} {confiance}%"]
    if news.get("link"):
        lines += ["", f"🔗 [Source {source_name}]({news['link']})"]
    lines += ["", f"🕐 _{now}_", "", "━━━━━━━━━━━━━━━━━━━━"]
    return "\n".join(lines)


def format_briefing_message(session_label: str, briefing: dict) -> str:
    now = datetime.now(timezone.utc).strftime("%d/%m/%Y %H:%M UTC")
    biais_s = briefing.get("biais_session", {})
    vol = briefing.get("volatilite_attendue", "MODEREE")
    events = "\n".join([f"  • {e}" for e in briefing.get("evenements_cles", [])[:5]])

    lines = [
        f"━━━━━━━━━━━━━━━━━━━━━━━━",
        f"📊 *BRIEFING {session_label}*",
        f"🕐 _{now}_",
        f"━━━━━━━━━━━━━━━━━━━━━━━━",
        f"",
        f"🌍 *Contexte general :*",
        f"_{briefing.get('introduction', '')}_",
        f"",
        f"📌 *Evenements cles :*",
        events,
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
        f"📊 *Biais session :*",
        f"  • 🥇 Gold → {BIAIS_SESSION.get(biais_s.get('GOLD', 'NEUTRE'), '🟡 NEUTRE')}",
        f"  • 💵 Dollar → {BIAIS_SESSION.get(biais_s.get('USD', 'NEUTRE'), '🟡 NEUTRE')}",
        f"  • ₿ Bitcoin → {BIAIS_SESSION.get(biais_s.get('BTC', 'NEUTRE'), '🟡 NEUTRE')}",
        f"",
        f"⚡ *Volatilite attendue :* {VOLATILITE.get(vol, '🟠 MODEREE')}",
        f"",
        f"👁️ *A surveiller :*",
        f"_{briefing.get('niveaux_attention', '')}_",
        f"",
        f"💡 *Conclusion :*",
        f"_{briefing.get('conclusion', '')}_",
        f"",
        f"━━━━━━━━━━━━━━━━━━━━━━━━",
    ]
    return "\n".join(lines)


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
            log.info("✅ Message envoye sur Telegram")
            return True
        else:
            log.error(f"❌ Telegram erreur {resp.status_code}: {resp.text[:200]}")
            return False
    except Exception as e:
        log.error(f"❌ Erreur envoi Telegram: {e}")
        return False


def check_sessions(ctx: dict):
    now = datetime.now(timezone.utc)
    for session_key, session in SESSIONS.items():
        if now.hour == session["hour"] and now.minute == session["minute"]:
            if should_send_briefing(ctx, session_key):
                log.info(f"📊 Generation briefing {session['label']}...")
                context_summary = get_context_summary(ctx)
                briefing = generate_briefing(session["label"], context_summary)
                if briefing:
                    message = format_briefing_message(session["label"], briefing)
                    if send_message(message):
                        mark_briefing_sent(ctx, session_key)
                        log.info(f"✅ Briefing {session['label']} envoye!")


def run():
    log.info(f"🚀 World News Bot v3 PRO demarre — Canal: {TELEGRAM_CHANNEL}")
    seen = load_seen()
    ctx = load_context()
    recent_titles = []

    while True:
        check_sessions(ctx)
        log.info("🔍 Verification des nouvelles news...")
        all_news = fetch_all_news()

        analyses_this_cycle = 0
        MAX_ANALYSES_PER_CYCLE = 5

        for news in all_news:
            if analyses_this_cycle >= MAX_ANALYSES_PER_CYCLE:
                log.info("⏸️ Limite analyses atteinte — pause au prochain cycle")
                break

            news_id = hash_news(news["title"])
            if news_id in seen:
                continue

            if is_duplicate(news["title"], recent_titles):
                log.info("⏭️ Doublon — ignore")
                seen.add(news_id)
                continue

            title   = news["title"]
            summary = news["summary"]
            trump_flag = is_trump_news(title, summary)
            high_flag  = is_high_impact(title, summary)

            if not high_flag and not trump_flag:
                log.info(f"⏭️ Impact faible — ignoree : {title[:60]}")
                seen.add(news_id)
                recent_titles.append(title)
                continue

            log.info(f"⭐ News fort impact : {title[:80]}")
            time.sleep(3)
            analysis = analyze_news(title, summary)
            analyses_this_cycle += 1

            if not analysis:
                seen.add(news_id)
                continue

            stars     = analysis.get("impact_stars", 1)
            pertinent = analysis.get("pertinent", False)

            if stars < 3 or not pertinent:
                log.info(f"⏭️ {stars} etoile(s) — sous seuil 3 etoiles, ignoree")
                seen.add(news_id)
                recent_titles.append(title)
                continue

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
        status = "TRADING ACTIF 🔥" if is_trading_hours() else "hors trading"
        log.info(f"⏳ [{status}] Prochaine verification dans {interval}s...")
        time.sleep(interval)


if __name__ == "__main__":
    run()
