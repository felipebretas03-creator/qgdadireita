#!/usr/bin/env python3
"""
Bot de Notícias Telegram - Governo de Direita
Monitora RSS, X (Twitter) e outras fontes em tempo real
Roda como Web Service no Render (plano gratuito)
"""

import asyncio
import feedparser
import hashlib
import json
import logging
import os
import re
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from datetime import datetime

import httpx
from telegram import Bot
from telegram.constants import ParseMode
from telegram.error import TelegramError

# ─── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()],
)
log = logging.getLogger(__name__)

# ─── Configurações ─────────────────────────────────────────────────────────────
TELEGRAM_TOKEN   = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHANNEL = os.environ["TELEGRAM_CHANNEL"]
SEEN_FILE        = "seen_ids.json"
CHECK_INTERVAL   = 60  # segundos

# ─── Palavras-chave ────────────────────────────────────────────────────────────
KEYWORDS = [
    # Figuras da direita
    "bolsonaro", "eduardo bolsonaro", "flavio bolsonaro", "carlos bolsonaro",
    "tarcísio", "tarcisio", "pablo marçal", "pablo marcal", "nikolas",
    "damares", "marco feliciano", "magno malta", "gustavo gayer",
    "ernesto araújo", "paulo guedes", "sergio moro", "moro",
    # Partidos de direita
    "pl ", "partido liberal", "novo partido", "patriota", "republicanos",
    "união brasil", "progressistas", "pp partido", "podemos partido",
    # Temas da direita
    "direita", "conservador", "conservadora", "liberal conservador",
    "armamento", "porte de arma", "flexibilização de armas",
    "família tradicional", "valores cristãos", "anticomunismo",
    "anti-esquerda", "contra o pt", "privatização", "livre mercado",
    "menos impostos", "redução do estado", "agenda conservadora",
    # Política geral relevante
    "stf", "supremo tribunal", "alexandre de moraes", "moraes",
    "congresso nacional", "senado federal", "câmara dos deputados",
    "impeachment", "cpmi", "cpi", "constituição", "pec",
    "eleição", "urna eletrônica", "voto impresso", "fraude eleitoral",
    "forças armadas", "militares", "exército", "marinha", "aeronáutica",
    "intervenção", "golpe", "democracia", "ditadura",
    # Anti-esquerda / oposição
    "contra lula", "governo lula", "pt partido", "esquerda radical",
    "comunismo", "socialismo", "marxismo", "globalismo",
]

# ─── Fontes RSS ────────────────────────────────────────────────────────────────
RSS_FEEDS = {
    "Jovem Pan":       "https://jovempan.com.br/feed",
    "CNN Brasil":      "https://www.cnnbrasil.com.br/feed/",
    "Folha de S.Paulo":"https://feeds.folha.uol.com.br/poder/rss091.xml",
    "O Globo Política":"https://oglobo.globo.com/rss.xml?secao=politica",
    "Metrópoles":      "https://www.metropoles.com/feed",
    "Veja":            "https://veja.abril.com.br/feed/",
    "UOL Política":    "https://rss.uol.com.br/feed/noticias/politica.xml",
    "R7 Política":     "https://noticias.r7.com/rss.xml",
}

# ─── Contas do X ──────────────────────────────────────────────────────────────
X_ACCOUNTS = [
    "jairbolsonaro", "CarlosEduVereza",
    "flaviobolsonaro", "eduardobolsonaro",
]

NITTER_INSTANCES = [
    "https://nitter.poast.org",
    "https://nitter.privacydev.net",
]

# ─── Persistência ──────────────────────────────────────────────────────────────
def load_seen():
    try:
        with open(SEEN_FILE) as f:
            return set(json.load(f))
    except:
        return set()

def save_seen(seen):
    with open(SEEN_FILE, "w") as f:
        json.dump(list(seen)[-5000:], f)

def make_id(text):
    return hashlib.md5(text.encode()).hexdigest()

def is_relevant(text):
    t = text.lower()
    return any(kw in t for kw in KEYWORDS)

# ─── Formatação ────────────────────────────────────────────────────────────────
def format_message(source, title, summary, url, published=""):
    emoji_map = {
        "Jovem Pan": "📻", "CNN Brasil": "📺",
        "Folha de S.Paulo": "📰", "O Globo Política": "🌐",
        "Metrópoles": "🏙️", "Veja": "📖",
        "UOL Política": "💻", "R7 Política": "📡",
        "X (Twitter)": "🐦",
    }
    emoji = emoji_map.get(source, "📢")
    summary_clean = re.sub(r"<[^>]+>", "", summary).strip()
    summary_short = summary_clean[:300] + "…" if len(summary_clean) > 300 else summary_clean
    lines = [f"{emoji} *{source}*", "", f"*{title}*"]
    if summary_short:
        lines += ["", summary_short]
    if published:
        lines += ["", f"🕐 _{published}_"]
    lines += ["", f"[🔗 Ler notícia completa]({url})"]
    return "\n".join(lines)

# ─── Envio ─────────────────────────────────────────────────────────────────────
async def send_news(bot, message):
    try:
        await bot.send_message(
            chat_id=TELEGRAM_CHANNEL,
            text=message,
            parse_mode=ParseMode.MARKDOWN,
            disable_web_page_preview=False,
        )
        log.info("✅ Notícia enviada.")
    except TelegramError as e:
        log.error(f"❌ Erro Telegram: {e}")

# ─── Coleta RSS ────────────────────────────────────────────────────────────────
async def fetch_rss(source, url, seen, bot):
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
            if uid in seen or not is_relevant(title + " " + summary):
                continue
            seen.add(uid)
            await send_news(bot, format_message(source, title, summary, link, pub))
            await asyncio.sleep(2)
    except Exception as e:
        log.warning(f"⚠️ RSS {source}: {e}")

# ─── Coleta X ──────────────────────────────────────────────────────────────────
async def fetch_x_account(account, seen, bot):
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
                if uid in seen or not is_relevant(title + " " + summary):
                    continue
                seen.add(uid)
                await send_news(bot, format_message("X (Twitter)", f"@{account}: {title}", summary, link, pub))
                await asyncio.sleep(2)
            break
        except Exception as e:
            log.warning(f"⚠️ Nitter {instance} @{account}: {e}")

# ─── Loop principal ────────────────────────────────────────────────────────────
async def main_loop():
    log.info("🤖 Bot iniciado!")
    bot  = Bot(token=TELEGRAM_TOKEN)
    seen = load_seen()
    while True:
        log.info(f"🔍 Verificando... {datetime.now().strftime('%H:%M:%S')}")
        await asyncio.gather(*[fetch_rss(s, u, seen, bot) for s, u in RSS_FEEDS.items()])
        await asyncio.gather(*[fetch_x_account(a, seen, bot) for a in X_ACCOUNTS])
        save_seen(seen)
        await asyncio.sleep(CHECK_INTERVAL)

# ─── Servidor HTTP (necessário para Render free tier) ─────────────────────────
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot rodando!")
    def log_message(self, *args):
        pass

def start_http_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    log.info(f"🌐 Servidor HTTP na porta {port}")
    server.serve_forever()

# ─── Entrada ───────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    threading.Thread(target=start_http_server, daemon=True).start()
    asyncio.run(main_loop())
