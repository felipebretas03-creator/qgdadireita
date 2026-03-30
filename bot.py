#!/usr/bin/env python3
"""
Bot de Notícias Telegram - Governo de Direita
Monitora RSS, X (Twitter) e outras fontes em tempo real
"""

import asyncio
import feedparser
import hashlib
import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
from telegram import Bot
from telegram.constants import ParseMode
from telegram.error import TelegramError

# ─── Configuração de logging ───────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("bot.log"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)

# ─── Configurações (lidas de variáveis de ambiente) ───────────────────────────
TELEGRAM_TOKEN   = os.environ["TELEGRAM_TOKEN"]       # Token do BotFather
TELEGRAM_CHANNEL = os.environ["TELEGRAM_CHANNEL"]     # Ex: @meucanal ou -100123456789
RAPIDAPI_KEY     = os.getenv("RAPIDAPI_KEY", "")      # Para X/Twitter via RapidAPI (opcional)

SEEN_FILE = Path("seen_ids.json")
CHECK_INTERVAL = 60  # segundos entre cada ciclo de verificação

# ─── Palavras-chave de filtro ──────────────────────────────────────────────────
KEYWORDS = [
    "governo", "bolsonaro", "lula", "direita", "conservador",
    "ministério", "congresso", "senado", "câmara", "presidente",
    "militar", "patriota", "liberal", "economia", "tributação",
    "pec", "reforma", "stf", "supremo", "eleição", "partido",
    "pl", "novo", "pp", "republicans", "união brasil",
]

# ─── Fontes RSS ────────────────────────────────────────────────────────────────
RSS_FEEDS = {
    "Jovem Pan": "https://jovempan.com.br/feed",
    "CNN Brasil": "https://www.cnnbrasil.com.br/feed/",
    "Folha de S.Paulo": "https://feeds.folha.uol.com.br/poder/rss091.xml",
    "O Globo Política": "https://oglobo.globo.com/rss.xml?secao=politica",
    "Metrópoles": "https://www.metropoles.com/feed",
    "Veja": "https://veja.abril.com.br/feed/",
    "UOL Política": "https://rss.uol.com.br/feed/noticias/politica.xml",
    "R7 Política": "https://noticias.r7.com/rss.xml",
    "Terra Política": "https://www.terra.com.br/noticias/politica/rss",
    "Gaúcha ZH": "https://gauchazh.clicrbs.com.br/politica/rss",
}

# ─── Contas do X a monitorar (via scraping público) ───────────────────────────
X_ACCOUNTS = [
    "jairbolsonaro", "CarlosEduVereza", "RodrigoConstant",
    "flaviobolsonaro", "eduardobolsonaro", "joaoromaooficial",
]

# ─── Persistência de IDs já vistos ────────────────────────────────────────────

def load_seen() -> set:
    if SEEN_FILE.exists():
        return set(json.loads(SEEN_FILE.read_text()))
    return set()


def save_seen(seen: set):
    SEEN_FILE.write_text(json.dumps(list(seen)[-5000:]))  # mantém os últimos 5000


def make_id(text: str) -> str:
    return hashlib.md5(text.encode()).hexdigest()


# ─── Filtro de relevância ──────────────────────────────────────────────────────

def is_relevant(text: str) -> bool:
    t = text.lower()
    return any(kw in t for kw in KEYWORDS)


# ─── Formatação da mensagem ────────────────────────────────────────────────────

def format_message(source: str, title: str, summary: str, url: str, published: str = "") -> str:
    emoji_map = {
        "Jovem Pan": "📻",
        "CNN Brasil": "📺",
        "Folha de S.Paulo": "📰",
        "O Globo Política": "🌐",
        "Metrópoles": "🏙️",
        "Veja": "📖",
        "UOL Política": "💻",
        "R7 Política": "📡",
        "X (Twitter)": "🐦",
    }
    emoji = emoji_map.get(source, "📢")
    summary_clean = re.sub(r"<[^>]+>", "", summary).strip()
    summary_short = summary_clean[:300] + "…" if len(summary_clean) > 300 else summary_clean

    lines = [
        f"{emoji} *{source}*",
        f"",
        f"*{title}*",
    ]
    if summary_short:
        lines += ["", summary_short]
    if published:
        lines += ["", f"🕐 _{published}_"]
    lines += ["", f"[🔗 Ler notícia completa]({url})"]

    return "\n".join(lines)


# ─── Envio ao Telegram ─────────────────────────────────────────────────────────

async def send_news(bot: Bot, message: str):
    try:
        await bot.send_message(
            chat_id=TELEGRAM_CHANNEL,
            text=message,
            parse_mode=ParseMode.MARKDOWN,
            disable_web_page_preview=False,
        )
        log.info("✅ Notícia enviada ao canal.")
    except TelegramError as e:
        log.error(f"❌ Erro ao enviar mensagem: {e}")


# ─── Coleta RSS ────────────────────────────────────────────────────────────────

async def fetch_rss(source: str, url: str, seen: set, bot: Bot):
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(url, headers={"User-Agent": "Mozilla/5.0"})
            feed = feedparser.parse(resp.text)

        for entry in feed.entries[:10]:
            title   = entry.get("title", "")
            link    = entry.get("link", "")
            summary = entry.get("summary", entry.get("description", ""))
            pub     = entry.get("published", "")

            uid = make_id(link or title)
            if uid in seen:
                continue
            if not is_relevant(title + " " + summary):
                continue

            seen.add(uid)
            msg = format_message(source, title, summary, link, pub)
            await send_news(bot, msg)
            await asyncio.sleep(2)  # anti-flood

    except Exception as e:
        log.warning(f"⚠️ Erro no feed {source}: {e}")


# ─── Coleta X/Twitter via Nitter (scraping livre) ─────────────────────────────

NITTER_INSTANCES = [
    "https://nitter.poast.org",
    "https://nitter.privacydev.net",
    "https://nitter.1d4.us",
]

async def fetch_x_account(account: str, seen: set, bot: Bot):
    for instance in NITTER_INSTANCES:
        try:
            url = f"{instance}/{account}/rss"
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(url, headers={"User-Agent": "Mozilla/5.0"})
            if resp.status_code != 200:
                continue
            feed = feedparser.parse(resp.text)

            for entry in feed.entries[:5]:
                title   = entry.get("title", "")
                link    = entry.get("link", "").replace(instance, "https://x.com")
                summary = entry.get("description", "")
                pub     = entry.get("published", "")

                uid = make_id(link or title)
                if uid in seen:
                    continue
                if not is_relevant(title + " " + summary):
                    continue

                seen.add(uid)
                msg = format_message("X (Twitter)", f"@{account}: {title}", summary, link, pub)
                await send_news(bot, msg)
                await asyncio.sleep(2)

            break  # instância funcionou, não tenta a próxima

        except Exception as e:
            log.warning(f"⚠️ Nitter {instance} falhou para @{account}: {e}")
            continue


# ─── Loop principal ────────────────────────────────────────────────────────────

async def main():
    log.info("🤖 Bot de notícias iniciado!")
    bot  = Bot(token=TELEGRAM_TOKEN)
    seen = load_seen()

    while True:
        log.info(f"🔍 Verificando fontes... ({datetime.now().strftime('%H:%M:%S')})")

        # RSS
        rss_tasks = [fetch_rss(src, url, seen, bot) for src, url in RSS_FEEDS.items()]
        await asyncio.gather(*rss_tasks)

        # X/Twitter
        x_tasks = [fetch_x_account(acc, seen, bot) for acc in X_ACCOUNTS]
        await asyncio.gather(*x_tasks)

        save_seen(seen)
        log.info(f"💤 Aguardando {CHECK_INTERVAL}s até próxima verificação...")
        await asyncio.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    asyncio.run(main())
