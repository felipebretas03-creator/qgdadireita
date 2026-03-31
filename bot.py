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
import redis
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
REDIS_URL        = os.getenv("REDIS_URL", "")
CHECK_INTERVAL   = 60  # segundos

# ─── Redis (anti-duplicatas persistente) ──────────────────────────────────────
rdb = None
if REDIS_URL:
    try:
        rdb = redis.from_url(REDIS_URL, decode_responses=True)
        rdb.ping()
        log.info("✅ Redis conectado!")
    except Exception as e:
        log.warning(f"⚠️ Redis falhou, usando memória local: {e}")
        rdb = None

# ─── Palavras-chave ────────────────────────────────────────────────────────────
KEYWORDS = [
    # Família Bolsonaro
    "bolsonaro", "jair bolsonaro", "eduardo bolsonaro", "flávio bolsonaro",
    "carlos bolsonaro", "michelle bolsonaro", "renan bolsonaro",

    # Líderes da direita nacional
    "tarcísio", "tarcisio de freitas", "nikolas ferreira", "pablo marçal",
    "pablo marcal", "damares alves", "marco feliciano", "magno malta",
    "gustavo gayer", "sergio moro", "paulo guedes", "ernesto araújo",
    "hamilton mourão", "augusto heleno", "braga netto", "walter braga netto",
    "jorge seif", "anderson torres", "filipe martins", "oswaldo eustáquio",
    "joel santana político", "arthur lira", "rodrigo pacheco direita",

    # Candidatos eleições estaduais - direita
    "rogério marinho", "giovani cherini", "coronel meira", "fred costa",
    "coronel ulysses", "julia zanatta", "caroline de toni", "alexandre ramagem",
    "daniel silveira", "carla zambelli", "bia kicis", "chris tonietto",
    "pastor gil", "hélio lopes", "júlia zanatta", "silvio costa filho",
    "capitão alden", "coronel tadeu", "alex manente", "joel santana",
    "roberto pessoa", "capitão wagner", "dr. jaziel", "israel batista",

    # Partidos de direita
    "partido liberal", "pl partido", "partido novo", "partido patriota",
    "republicanos partido", "união brasil", "progressistas", "pp partido",
    "podemos partido", "solidariedade partido", "avante partido",
    "prtb partido", "dc partido", "agir partido",

    # Eleições 2026
    "eleições 2026", "candidato 2026", "pré-candidato", "corrida eleitoral",
    "pesquisa eleitoral", "pesquisa datafolha direita", "pesquisa ipec direita",
    "eleição governador", "eleição senador", "eleição deputado federal",
    "eleição deputado estadual", "eleição prefeito direita",

    # Temas da direita
    "pauta conservadora", "agenda conservadora", "valores conservadores",
    "porte de arma", "flexibilização de armas", "armamento civil",
    "família tradicional", "valores cristãos", "anticomunismo",
    "privatização", "livre mercado", "menos impostos", "estado mínimo",
    "liberalismo econômico", "reforma tributária direita",
    "escola sem partido", "homeschooling", "educação domiciliar",
    "ideologia de gênero", "kit gay", "censura conservadora",

    # Política institucional
    "stf", "alexandre de moraes", "impeachment", "cpmi", "cpi",
    "urna eletrônica", "voto impresso", "fraude eleitoral", "tse",
    "forças armadas", "intervenção federal", "pec", "constituição federal",
    "congresso nacional", "senado federal", "câmara dos deputados",

    # Anti-esquerda / oposição
    "governo lula", "lula errou", "lula mentiu", "pt partido",
    "esquerda radical", "comunismo", "socialismo", "marxismo",
    "mst invasão", "crime organizado esquerda", "censura esquerda",

    # Apoiadores e influenciadores
    "allan dos santos", "bernardo küster", "bernardo kuster",
    "monark político", "arthur do val", "mamãe falei",
    "joel jota", "caio coppolla", "joel santana político",
]

# Palavras que BLOQUEIAM a notícia mesmo que tenha keyword
BLOCKLIST = [
    "mata ", "matou", "assassin", "homicídio",
    "acidente de trânsito", "bateu carro", "morreu atropelado",
    "estupro", "abuso sexual",
    "esporte", "futebol", "campeonato", "copa do mundo", "olimpíadas",
    "celebridade", "novela", "música", "show", "entretenimento",
    "receita culinária", "remédio", "hospital", "doença viral",
]

# ─── Fontes RSS ────────────────────────────────────────────────────────────────
RSS_FEEDS = {
    "Jovem Pan":         "https://jovempan.com.br/feed",
    "Jovem Pan Política":"https://jovempan.com.br/category/noticias/politica/feed",
    "CNN Brasil":        "https://www.cnnbrasil.com.br/politica/feed/",
    "Folha Poder":       "https://feeds.folha.uol.com.br/poder/rss091.xml",
    "O Globo Política":  "https://oglobo.globo.com/rss.xml?secao=politica",
    "Metrópoles Política":"https://www.metropoles.com/brasil/politica/feed",
    "Veja":              "https://veja.abril.com.br/feed/",
    "Veja Política":     "https://veja.abril.com.br/categoria/politica/feed/",
    "UOL Política":      "https://rss.uol.com.br/feed/noticias/politica.xml",
    "R7 Política":       "https://noticias.r7.com/politica/feed.xml",
    "Gazeta do Povo":    "https://www.gazetadopovo.com.br/feed/republica/",
    "O Antagonista":     "https://oantagonista.com.br/feed/",
    "Oeste":             "https://revistaoeste.com/feed/",
    "Crusoé":            "https://crusoe.com.br/feed/",
}

# ─── Contas do X (bancada da direita) ─────────────────────────────────────────
X_ACCOUNTS = [
    # Família Bolsonaro
    "jairbolsonaro", "flaviobolsonaro", "eduardobolsonaro",
    "carlosbolsonaro", "michellebolsonaro",
    # Líderes nacionais
    "nikolas_foto", "pablomarcal", "damasceno_mdb",
    "tarcisiogdf", "sergiomoro", "damaresalves",
    "MarcoFeliciano", "magnomalta", "GustavoGayer",
    # Deputados federais - direita
    "CarlaZambelli", "biakicis", "ChrisTonietto",
    "JuliaMZanatta", "CarolineDeToni", "AlexRamagem",
    "DanielSilveira", "HelioCopter", "JoelJota",
    # Influenciadores conservadores
    "caiocoppolla", "bernardokuster", "opropriolucas",
    "renanoproprio", "olavodecarvalho",
]

# ─── Persistência ──────────────────────────────────────────────────────────────
SEEN_LOCAL = set()

def load_seen():
    global SEEN_LOCAL
    if rdb:
        try:
            SEEN_LOCAL = set(rdb.smembers("seen_ids"))
            log.info(f"✅ Carregados {len(SEEN_LOCAL)} IDs do Redis")
            return SEEN_LOCAL
        except Exception as e:
            log.warning(f"⚠️ Redis load falhou: {e}")
    return SEEN_LOCAL

def add_seen(uid):
    """Adiciona ID à memória local e ao Redis imediatamente."""
    global SEEN_LOCAL
    SEEN_LOCAL.add(uid)
    if rdb:
        try:
            rdb.sadd("seen_ids", uid)
            rdb.expire("seen_ids", 60 * 60 * 24 * 30)
        except Exception as e:
            log.warning(f"⚠️ Redis save falhou: {e}")

def save_seen(seen):
    pass  # não usado mais, substituído por add_seen()

def make_id(text):
    return hashlib.md5(text.encode()).hexdigest()

def is_relevant(text):
    t = text.lower()
    # Bloqueia se tiver palavra da blocklist
    if any(b in t for b in BLOCKLIST):
        return False
    # Exige pelo menos 1 keyword
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
        for entry in feed.entries[:5]:
            title   = entry.get("title", "")
            link    = entry.get("link", "")
            summary = entry.get("summary", entry.get("description", ""))
            pub     = entry.get("published", "")
            uid = make_id(link or title)
            if uid in seen or not is_relevant(title + " " + summary):
                continue
            add_seen(uid)
            seen.add(uid)
            await send_news(bot, format_message(source, title, summary, link, pub))
            await asyncio.sleep(5)
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
            for entry in feed.entries[:3]:  # máx 3 por conta
                title   = entry.get("title", "")
                link    = entry.get("link", "").replace(instance, "https://x.com")
                summary = entry.get("description", "")
                pub     = entry.get("published", "")
                uid = make_id(link or title)
                if uid in seen or not is_relevant(title + " " + summary):
                    continue
                add_seen(uid)
                seen.add(uid)
                await send_news(bot, format_message("X (Twitter)", f"@{account}: {title}", summary, link, pub))
                await asyncio.sleep(5)  # 5s entre mensagens anti-flood
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
        # Roda fontes sequencialmente para evitar flood
        for s, u in RSS_FEEDS.items():
            await fetch_rss(s, u, seen, bot)
            await asyncio.sleep(3)
        for a in X_ACCOUNTS:
            await fetch_x_account(a, seen, bot)
            await asyncio.sleep(3)
        log.info(f"💤 Aguardando {CHECK_INTERVAL}s...")
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
