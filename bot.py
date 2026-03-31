#!/usr/bin/env python3
"""Bot QG da Direita - com enquetes, alertas urgentes e lock Redis"""

import asyncio
import feedparser
import hashlib
import logging
import os
import re
import random
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from datetime import datetime

import httpx
import redis
from telegram import Bot, Poll
from telegram.constants import ParseMode
from telegram.error import TelegramError

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

TELEGRAM_TOKEN   = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHANNEL = os.environ["TELEGRAM_CHANNEL"]
REDIS_URL        = os.getenv("REDIS_URL", "")
CHECK_INTERVAL   = 120

rdb = None
if REDIS_URL:
    try:
        rdb = redis.from_url(REDIS_URL, decode_responses=True)
        rdb.ping()
        log.info("✅ Redis conectado!")
    except Exception as e:
        log.warning(f"⚠️ Redis falhou: {e}")

def acquire_lock():
    if not rdb:
        return True
    return rdb.set("bot:lock", "1", nx=True, ex=300) is not None

def renew_lock():
    if rdb:
        try:
            rdb.expire("bot:lock", 300)
        except:
            pass

def was_sent(uid):
    if rdb:
        try:
            return rdb.exists(f"sent:{uid}") == 1
        except:
            pass
    return False

def mark_sent(uid):
    if rdb:
        try:
            rdb.set(f"sent:{uid}", "1", ex=60*60*24*7)
        except:
            pass

def make_uid(text):
    return hashlib.md5(text.encode()).hexdigest()

# ─── Palavras urgentes ─────────────────────────────────────────────────────────
URGENT_KEYWORDS = [
    "bolsonaro preso", "bolsonaro solto", "bolsonaro condenado",
    "bolsonaro candidato", "golpe", "impeachment", "intervenção",
    "stf suspende", "stf derruba", "alexandre de moraes preso",
    "lula preso", "lula condenado", "eleição cancelada",
    "forças armadas", "estado de sítio", "cpmi instaurada",
    "nikolas presidente", "tarcísio presidente", "bolsonaro 2026",
]

KEYWORDS = [
    "bolsonaro", "tarcísio", "tarcisio", "nikolas ferreira", "pablo marçal",
    "pablo marcal", "damares", "marco feliciano", "magno malta", "gustavo gayer",
    "sergio moro", "paulo guedes", "hamilton mourão", "braga netto",
    "carla zambelli", "bia kicis", "eduardo bolsonaro", "flávio bolsonaro",
    "carlos bolsonaro", "michelle bolsonaro",
    "partido liberal", "pl partido", "partido novo", "união brasil",
    "progressistas", "republicanos partido", "partido patriota",
    "eleições 2026", "candidato 2026", "corrida eleitoral",
    "stf", "alexandre de moraes", "impeachment", "cpmi",
    "urna eletrônica", "voto impresso", "fraude eleitoral",
    "forças armadas", "intervenção federal",
    "governo lula", "pt partido", "esquerda radical",
    "comunismo", "socialismo", "marxismo",
    "porte de arma", "armamento civil", "família tradicional",
    "privatização", "livre mercado", "estado mínimo",
    "escola sem partido", "homeschooling",
    "bernardo kuster", "caio coppolla", "mamãe falei",
]

BLOCKLIST = [
    "futebol", "campeonato", "copa do mundo",
    "novela", "show", "entretenimento",
    "receita ", "doença viral",
]

def is_urgent(text):
    t = text.lower()
    return any(kw in t for kw in URGENT_KEYWORDS)

def is_relevant(text):
    t = text.lower()
    if any(b in t for b in BLOCKLIST):
        return False
    return any(kw in t for kw in KEYWORDS)

# ─── Enquetes ──────────────────────────────────────────────────────────────────
POLLS = [
    {
        "question": "🗳️ Você apoia Bolsonaro como candidato em 2026?",
        "options": ["✅ Sim, com certeza!", "🤔 Talvez, depende", "❌ Não apoio"],
    },
    {
        "question": "🗳️ Tarcísio de Freitas deve ser candidato à Presidente em 2026?",
        "options": ["✅ Sim!", "🤔 Prefiro outro candidato", "❌ Não"],
    },
    {
        "question": "🗳️ Você confia no sistema eleitoral brasileiro?",
        "options": ["✅ Confio", "🤔 Confio parcialmente", "❌ Não confio"],
    },
    {
        "question": "🗳️ O STF está extrapolando seus poderes?",
        "options": ["✅ Sim, com certeza", "🤔 Em alguns casos", "❌ Não, age corretamente"],
    },
    {
        "question": "🗳️ Qual pauta é mais importante para a direita?",
        "options": ["🔫 Armamento", "💰 Economia", "👨‍👩‍👧 Família", "🗳️ Eleições 2026"],
    },
    {
        "question": "🗳️ Você acredita que houve fraude nas eleições de 2022?",
        "options": ["✅ Sim", "🤔 Tenho dúvidas", "❌ Não"],
    },
    {
        "question": "🗳️ Nikolas Ferreira deve ser candidato a presidente?",
        "options": ["✅ Sim!", "🤔 Ainda não é hora", "❌ Prefiro outro"],
    },
]

# ─── RSS ───────────────────────────────────────────────────────────────────────
RSS_FEEDS = {
    "Jovem Pan":        "https://jovempan.com.br/feed",
    "CNN Brasil":       "https://www.cnnbrasil.com.br/politica/feed/",
    "Folha Poder":      "https://feeds.folha.uol.com.br/poder/rss091.xml",
    "O Globo Política": "https://oglobo.globo.com/rss.xml?secao=politica",
    "Metrópoles":       "https://www.metropoles.com/brasil/politica/feed",
    "Veja Política":    "https://veja.abril.com.br/categoria/politica/feed/",
    "UOL Política":     "https://rss.uol.com.br/feed/noticias/politica.xml",
    "Gazeta do Povo":   "https://www.gazetadopovo.com.br/feed/republica/",
    "O Antagonista":    "https://oantagonista.com.br/feed/",
    "Oeste":            "https://revistaoeste.com/feed/",
    "Crusoé":           "https://crusoe.com.br/feed/",
}

X_ACCOUNTS = [
    "jairbolsonaro", "flaviobolsonaro", "eduardobolsonaro",
    "carlosbolsonaro", "michellebolsonaro",
    "nikolas_foto", "pablomarcal", "tarcisiogdf", "sergiomoro",
    "damaresalves", "MarcoFeliciano", "magnomalta", "GustavoGayer",
    "CarlaZambelli", "biakicis", "caiocoppolla", "bernardokuster",
]

NITTER_INSTANCES = [
    "https://nitter.poast.org",
    "https://nitter.privacydev.net",
]

EMOJI_MAP = {
    "Jovem Pan": "📻", "CNN Brasil": "📺", "Folha Poder": "📰",
    "O Globo Política": "🌐", "Metrópoles": "🏙️", "Veja Política": "📖",
    "UOL Política": "💻", "Gazeta do Povo": "🗞️",
    "O Antagonista": "⚡", "Oeste": "🔵", "Crusoé": "📝",
    "X (Twitter)": "🐦",
}

def format_normal(source, title, summary, url, pub=""):
    emoji = EMOJI_MAP.get(source, "📢")
    clean = re.sub(r"<[^>]+>", "", summary).strip()
    short = clean[:280] + "…" if len(clean) > 280 else clean
    parts = [f"{emoji} *{source}*", "", f"*{title}*"]
    if short:
        parts += ["", short]
    if pub:
        parts += ["", f"🕐 _{pub}_"]
    parts += ["", f"[🔗 Ler completo]({url})"]
    return "\n".join(parts)

def format_urgent(source, title, summary, url, pub=""):
    clean = re.sub(r"<[^>]+>", "", summary).strip()
    short = clean[:280] + "…" if len(clean) > 280 else clean
    parts = [
        "🚨🚨🚨 *URGENTE* 🚨🚨🚨",
        "",
        f"📢 *{source}*",
        "",
        f"*{title}*",
    ]
    if short:
        parts += ["", short]
    if pub:
        parts += ["", f"🕐 _{pub}_"]
    parts += ["", f"[🔗 Ler completo]({url})"]
    return "\n".join(parts)

async def send_msg(bot, msg):
    try:
        await bot.send_message(
            chat_id=TELEGRAM_CHANNEL,
            text=msg,
            parse_mode=ParseMode.MARKDOWN,
            disable_web_page_preview=False,
        )
        log.info("✅ Enviado.")
    except TelegramError as e:
        log.error(f"❌ Telegram: {e}")

async def send_poll(bot):
    poll = random.choice(POLLS)
    try:
        await bot.send_poll(
            chat_id=TELEGRAM_CHANNEL,
            question=poll["question"],
            options=poll["options"],
            is_anonymous=True,
        )
        log.info("🗳️ Enquete enviada.")
    except TelegramError as e:
        log.error(f"❌ Enquete: {e}")

async def fetch_rss(source, url, bot):
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(url, headers={"User-Agent": "Mozilla/5.0"})
        feed = feedparser.parse(resp.text)
        for entry in feed.entries[:5]:
            title   = entry.get("title", "").strip()
            link    = entry.get("link", "").strip()
            summary = entry.get("summary", entry.get("description", ""))
            pub     = entry.get("published", "")
            if not title or not link:
                continue
            uid = make_uid(link)
            if was_sent(uid):
                continue
            if not is_relevant(title + " " + summary):
                continue
            mark_sent(uid)
            if is_urgent(title + " " + summary):
                msg = format_urgent(source, title, summary, link, pub)
            else:
                msg = format_normal(source, title, summary, link, pub)
            await send_msg(bot, msg)
            await asyncio.sleep(6)
    except Exception as e:
        log.warning(f"⚠️ RSS {source}: {e}")

async def fetch_x(account, bot):
    for instance in NITTER_INSTANCES:
        try:
            url = f"{instance}/{account}/rss"
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(url, headers={"User-Agent": "Mozilla/5.0"})
            if resp.status_code != 200:
                continue
            feed = feedparser.parse(resp.text)
            for entry in feed.entries[:3]:
                title   = entry.get("title", "").strip()
                link    = entry.get("link", "").replace(instance, "https://x.com")
                summary = entry.get("description", "")
                pub     = entry.get("published", "")
                if not title or not link:
                    continue
                uid = make_uid(link)
                if was_sent(uid):
                    continue
                if not is_relevant(title + " " + summary):
                    continue
                mark_sent(uid)
                if is_urgent(title + " " + summary):
                    msg = format_urgent("X (Twitter)", f"@{account}: {title}", summary, link, pub)
                else:
                    msg = format_normal("X (Twitter)", f"@{account}: {title}", summary, link, pub)
                await send_msg(bot, msg)
                await asyncio.sleep(6)
            break
        except Exception as e:
            log.warning(f"⚠️ X @{account}: {e}")

async def main_loop():
    log.info("🤖 QG da Direita - Bot iniciado!")
    bot = Bot(token=TELEGRAM_TOKEN)

    await asyncio.sleep(10)

    if not acquire_lock():
        log.warning("⚠️ Outra instância já está rodando. Aguardando...")
        while not acquire_lock():
            await asyncio.sleep(30)

    log.info("🔒 Lock adquirido — única instância ativa.")

    cycle = 0
    while True:
        renew_lock()
        cycle += 1
        log.info(f"🔍 Ciclo {cycle} — {datetime.now().strftime('%H:%M:%S')}")

        for source, url in RSS_FEEDS.items():
            await fetch_rss(source, url, bot)
            await asyncio.sleep(2)

        for account in X_ACCOUNTS:
            await fetch_x(account, bot)
            await asyncio.sleep(2)

        # Envia enquete a cada 5 ciclos (~10 minutos)
        if cycle % 5 == 0:
            await send_poll(bot)

        log.info(f"💤 Aguardando {CHECK_INTERVAL}s")
        await asyncio.sleep(CHECK_INTERVAL)

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"QG da Direita - Bot ativo!")
    def log_message(self, *a): pass

def start_server():
    port = int(os.environ.get("PORT", 10000))
    HTTPServer(("0.0.0.0", port), Handler).serve_forever()

if __name__ == "__main__":
    threading.Thread(target=start_server, daemon=True).start()
    asyncio.run(main_loop())
