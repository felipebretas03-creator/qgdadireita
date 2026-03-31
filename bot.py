#!/usr/bin/env python3
"""Bot QG da Direita - com lock Redis para evitar instâncias duplicadas"""

import asyncio
import feedparser
import hashlib
import logging
import os
import re
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from datetime import datetime

import httpx
import redis
from telegram import Bot
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
    """Garante que só uma instância do bot roda por vez."""
    if not rdb:
        return True
    result = rdb.set("bot:lock", "1", nx=True, ex=300)
    return result is not None

def renew_lock():
    """Renova o lock a cada ciclo."""
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

def is_relevant(text):
    t = text.lower()
    if any(b in t for b in BLOCKLIST):
        return False
    return any(kw in t for kw in KEYWORDS)

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

def format_msg(source, title, summary, url, pub=""):
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

async def send(bot, msg):
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
            await send(bot, format_msg(source, title, summary, link, pub))
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
                await send(bot, format_msg("X (Twitter)", f"@{account}: {title}", summary, link, pub))
                await asyncio.sleep(6)
            break
        except Exception as e:
            log.warning(f"⚠️ X @{account}: {e}")

async def main_loop():
    log.info("🤖 QG da Direita - Bot iniciado!")
    bot = Bot(token=TELEGRAM_TOKEN)

    # Aguarda um pouco para deixar a instância antiga morrer
    await asyncio.sleep(10)

    if not acquire_lock():
        log.warning("⚠️ Outra instância já está rodando. Aguardando...")
        while not acquire_lock():
            await asyncio.sleep(30)
        log.info("✅ Lock adquirido! Iniciando...")

    log.info("🔒 Lock Redis adquirido — sou a única instância ativa.")

    while True:
        renew_lock()
        log.info(f"🔍 {datetime.now().strftime('%H:%M:%S')}")
        for source, url in RSS_FEEDS.items():
            await fetch_rss(source, url, bot)
            await asyncio.sleep(2)
        for account in X_ACCOUNTS:
            await fetch_x(account, bot)
            await asyncio.sleep(2)
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
