#!/usr/bin/env python3
"""
mail2tg — пересылает письма от выбранных отправителей из IMAP-ящика в Telegram.

Настроен на Gmail (imap.gmail.com, вход по паролю приложения),
но работает с любым IMAP-сервером (Яндекс, Mail.ru, свой сервер).

Запуск:
    python3 mail2tg.py               # основной режим (бесконечный цикл)
    python3 mail2tg.py --check       # проверить подключение к почте и Telegram
    python3 mail2tg.py --get-chat-id # узнать свой chat_id (сначала напишите боту /start)
    python3 mail2tg.py --once        # один проход и выход
    python3 mail2tg.py --resend-last # переслать последнее письмо автора (проверить, как выглядит)
"""

from __future__ import annotations

import argparse
import base64
import email
import html
import imaplib
import json
import logging
import os
import re
import signal
import socket
import ssl
import sys
import time
from dataclasses import dataclass
from email.header import decode_header, make_header
from email.message import Message
from email.utils import getaddresses, parseaddr, parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path

import requests
from bs4 import BeautifulSoup, NavigableString

BASE_DIR = Path(__file__).resolve().parent
log = logging.getLogger("mail2tg")

TG_TEXT_LIMIT = 4096
TG_CAPTION_LIMIT = 1024
TG_BOT_FILE_LIMIT_MB = 50


# ───────────────────────────── конфигурация ─────────────────────────────

def load_env_file(path: Path) -> None:
    """Минимальный парсер .env: KEY=VALUE, комментарии #, кавычки. Не перезаписывает уже заданные переменные."""
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):]
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        else:
            value = re.sub(r"\s+#.*$", "", value)  # комментарий в конце строки
        os.environ.setdefault(key, value)


def env_str(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def env_int(name: str, default: int) -> int:
    value = env_str(name)
    if not value:
        return default
    try:
        return int(value)
    except ValueError:
        raise SystemExit(f"Ошибка в .env: {name} должно быть числом, а не {value!r}")


def env_bool(name: str, default: bool) -> bool:
    value = env_str(name).lower()
    if not value:
        return default
    return value in ("1", "true", "yes", "on", "да")


@dataclass
class Config:
    tg_token: str
    tg_chat_id: str
    tg_proxy: str
    imap_host: str
    imap_port: int
    imap_security: str
    imap_verify_tls: bool
    imap_user: str
    imap_password: str
    imap_folder: str
    from_filter: list[str]
    poll_interval: int
    send_attachments: bool
    max_attachment_mb: int
    max_body_chars: int
    show_links: bool
    delivery: str
    translate: bool
    translate_from: str
    translate_to: str
    translate_proxy: str
    translate_providers: list[str]
    mymemory_email: str
    backfill: int
    state_file: Path

    @classmethod
    def from_env(cls) -> "Config":
        security = env_str("IMAP_SECURITY", "ssl").lower()
        if security not in ("starttls", "ssl", "plain"):
            raise SystemExit("Ошибка в .env: IMAP_SECURITY должно быть starttls, ssl или plain")
        delivery = env_str("DELIVERY", "html").lower()
        if delivery not in ("html", "text", "both"):
            raise SystemExit("Ошибка в .env: DELIVERY должно быть html, text или both")
        state_file = Path(env_str("STATE_FILE", "state.json"))
        if not state_file.is_absolute():
            state_file = BASE_DIR / state_file
        filters = [f.strip().lower() for f in re.split(r"[,;\s]+", env_str("FROM_FILTER")) if f.strip()]
        imap_host = env_str("IMAP_HOST", "imap.gmail.com")
        imap_password = env_str("IMAP_PASSWORD")
        if imap_host.endswith("gmail.com"):
            # Google показывает пароль приложения группами через пробел: "abcd efgh ijkl mnop"
            imap_password = imap_password.replace(" ", "")
        return cls(
            tg_token=env_str("TELEGRAM_BOT_TOKEN"),
            tg_chat_id=env_str("TELEGRAM_CHAT_ID"),
            tg_proxy=env_str("TELEGRAM_PROXY"),
            imap_host=imap_host,
            imap_port=env_int("IMAP_PORT", 993),
            imap_security=security,
            imap_verify_tls=env_bool("IMAP_VERIFY_TLS", True),
            imap_user=env_str("IMAP_USER"),
            imap_password=imap_password,
            imap_folder=env_str("IMAP_FOLDER", "INBOX"),
            from_filter=filters,
            poll_interval=max(10, env_int("POLL_INTERVAL", 60)),
            send_attachments=env_bool("SEND_ATTACHMENTS", True),
            max_attachment_mb=min(TG_BOT_FILE_LIMIT_MB, env_int("MAX_ATTACHMENT_MB", 20)),
            max_body_chars=env_int("MAX_BODY_CHARS", 12000),
            show_links=env_bool("SHOW_LINKS", False),
            delivery=delivery,
            translate=env_bool("TRANSLATE", True),
            translate_from=env_str("TRANSLATE_FROM", "en") or "auto",
            translate_to=env_str("TRANSLATE_TO", "ru") or "ru",
            translate_proxy=env_str("TRANSLATE_PROXY"),
            translate_providers=[x.strip().lower() for x in env_str("TRANSLATE_PROVIDERS", "google,mymemory").split(",") if x.strip()],
            mymemory_email=env_str("MYMEMORY_EMAIL"),
            backfill=max(0, env_int("BACKFILL_ON_FIRST_RUN", 0)),
            state_file=state_file,
        )

    def require(self, *names: str) -> None:
        mapping = {
            "TELEGRAM_BOT_TOKEN": self.tg_token,
            "TELEGRAM_CHAT_ID": self.tg_chat_id,
            "IMAP_USER": self.imap_user,
            "IMAP_PASSWORD": self.imap_password,
        }
        missing = [n for n in names if not mapping.get(n)]
        if missing:
            raise SystemExit("Не заполнены параметры в .env: " + ", ".join(missing))


# ───────────────────────────── Telegram ─────────────────────────────

class TelegramPermanentError(Exception):
    """Telegram отклонил запрос (ошибка 4xx) — повтор не поможет."""


class TelegramTemporaryError(Exception):
    """Сеть или Telegram недоступны — стоит повторить позже."""


class Telegram:
    def __init__(self, token: str, chat_id: str = "", proxy: str = ""):
        self.token = token
        self.chat_id = chat_id
        self.session = requests.Session()
        if proxy:
            self.session.proxies = {"http": proxy, "https": proxy}

    def _mask(self, text: str) -> str:
        return text.replace(self.token, "***") if self.token else text

    def call(self, method: str, data: dict | None = None, files: dict | None = None, attempts: int = 5):
        url = f"https://api.telegram.org/bot{self.token}/{method}"
        delay = 2
        last_error = ""
        for _ in range(attempts):
            try:
                resp = self.session.post(url, data=data, files=files, timeout=(15, 120))
            except requests.RequestException as exc:
                last_error = self._mask(str(exc))
                log.warning("Telegram недоступен (%s), повтор через %s с", last_error, delay)
                time.sleep(delay)
                delay = min(delay * 2, 60)
                continue
            try:
                payload = resp.json()
            except ValueError:
                payload = {}
            if payload.get("ok"):
                return payload["result"]
            description = payload.get("description", resp.text[:200])
            if resp.status_code == 429:
                wait = int(payload.get("parameters", {}).get("retry_after", 5)) + 1
                log.warning("Telegram просит подождать %s с (лимит сообщений)", wait)
                time.sleep(wait)
                continue
            if resp.status_code >= 500:
                last_error = f"{resp.status_code} {description}"
                log.warning("Ошибка на стороне Telegram (%s), повтор через %s с", last_error, delay)
                time.sleep(delay)
                delay = min(delay * 2, 60)
                continue
            raise TelegramPermanentError(f"{method}: {resp.status_code} {description}")
        raise TelegramTemporaryError(f"{method}: {last_error or 'не удалось отправить'}")

    def send_text(self, text: str, html_mode: bool = False, reply_to: int | None = None) -> int:
        data = {
            "chat_id": self.chat_id,
            "text": text,
            "link_preview_options": json.dumps({"is_disabled": True}),
        }
        if html_mode:
            data["parse_mode"] = "HTML"
        if reply_to:
            data["reply_parameters"] = json.dumps({"message_id": reply_to, "allow_sending_without_reply": True})
        return self.call("sendMessage", data)["message_id"]

    def send_document(self, filename: str, content: bytes, caption: str = "", reply_to: int | None = None,
                      html_mode: bool = False) -> int:
        data = {"chat_id": self.chat_id}
        if caption:
            data["caption"] = caption[:TG_CAPTION_LIMIT]
            if html_mode:
                data["parse_mode"] = "HTML"
        if reply_to:
            data["reply_parameters"] = json.dumps({"message_id": reply_to, "allow_sending_without_reply": True})
        files = {"document": (filename, content)}
        return self.call("sendDocument", data, files)["message_id"]


# ───────────────────────────── разбор писем ─────────────────────────────

def decode_mime(value: str | None) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value))).strip()
    except Exception:
        return str(value).strip()


class _HTMLToText(HTMLParser):
    BLOCK_TAGS = {
        "p", "div", "tr", "table", "ul", "ol", "li", "blockquote", "section", "article",
        "header", "footer", "h1", "h2", "h3", "h4", "h5", "h6", "hr", "pre",
    }
    SKIP_TAGS = {"script", "style", "head", "title", "noscript"}

    def __init__(self, show_links: bool):
        super().__init__(convert_charrefs=True)
        self.show_links = show_links
        self.parts: list[str] = []
        self.skip = 0
        self.href_stack: list[tuple[str | None, int]] = []

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP_TAGS:
            self.skip += 1
        elif tag == "br":
            self.parts.append("\n")
        elif tag in self.BLOCK_TAGS:
            self.parts.append("\n")
            if tag == "li":
                self.parts.append("• ")
        elif tag == "td":
            self.parts.append(" ")
        elif tag == "a":
            href = dict(attrs).get("href")
            self.href_stack.append((href, len(self.parts)))

    def handle_startendtag(self, tag, attrs):
        if tag in ("br", "hr"):
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP_TAGS:
            self.skip = max(0, self.skip - 1)
        elif tag in self.BLOCK_TAGS and tag != "li":
            self.parts.append("\n")
        elif tag == "a" and self.href_stack:
            href, start = self.href_stack.pop()
            if self.show_links and href and href.startswith(("http://", "https://")):
                link_text = "".join(self.parts[start:]).strip()
                if href not in link_text:
                    self.parts.append(f" ({href})")

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)

    def text(self) -> str:
        return "".join(self.parts)


def html_to_text(raw_html: str, show_links: bool = False) -> str:
    parser = _HTMLToText(show_links)
    try:
        parser.feed(raw_html)
        parser.close()
    except Exception:
        return re.sub(r"<[^>]+>", " ", raw_html)
    return parser.text()


def normalize_text(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\xa0", " ").replace("‌", "")
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.split("\n")]
    text = "\n".join(lines)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def part_text(part: Message) -> str:
    payload = part.get_payload(decode=True)
    if payload is None:
        payload = part.get_payload()
        return payload if isinstance(payload, str) else ""
    charset = part.get_content_charset() or "utf-8"
    for enc in (charset, "utf-8", "cp1251", "latin-1"):
        try:
            return payload.decode(enc)
        except (LookupError, UnicodeDecodeError):
            continue
    return payload.decode("utf-8", errors="replace")


def is_attachment(part: Message) -> bool:
    if part.is_multipart():
        return False
    disposition = (part.get("Content-Disposition") or "").lower()
    if disposition.startswith("attachment"):
        return True
    # inline-картинки с именем файла и прочие файлы, которые не являются телом письма
    return bool(part.get_filename()) and part.get_content_type() not in ("text/plain", "text/html")


def extract_body(msg: Message, show_links: bool) -> str:
    plain, html_parts = [], []
    for part in msg.walk():
        if part.is_multipart() or is_attachment(part):
            continue
        ctype = part.get_content_type()
        if ctype == "text/plain":
            plain.append(part_text(part))
        elif ctype == "text/html":
            html_parts.append(part_text(part))
    if plain and any(p.strip() for p in plain):
        return normalize_text("\n\n".join(plain))
    if html_parts:
        return normalize_text("\n\n".join(html_to_text(h, show_links) for h in html_parts))
    return ""


def content_id(part: Message) -> str:
    return (part.get("Content-ID") or "").strip().strip("<>").strip().lower()


def extract_attachments(msg: Message, skip_cids: frozenset[str] | set[str] = frozenset()) -> list[tuple[str, bytes]]:
    result = []
    for index, part in enumerate(msg.walk(), start=1):
        if not is_attachment(part):
            continue
        if skip_cids and content_id(part) in skip_cids:
            continue  # картинка уже встроена в HTML-файл
        content = part.get_payload(decode=True)
        if not content:
            continue
        name = decode_mime(part.get_filename()) or f"attachment_{index}"
        name = re.sub(r'[\\/:*?"<>|\r\n]+', "_", name)
        result.append((name, content))
    return result


def sender_matches(from_header: str, filters: list[str]) -> bool:
    if not filters:
        return True
    addresses = [addr.lower() for _, addr in getaddresses([from_header]) if addr]
    for addr in addresses:
        for f in filters:
            if f.startswith("@"):
                if addr.endswith(f):
                    return True
            elif addr == f:
                return True
    return False


def chunk_escaped(text: str, limit: int) -> list[str]:
    """Режет текст на куски, каждый из которых после html.escape не длиннее limit."""
    chunks: list[str] = []
    current = ""
    for line in text.split("\n"):
        candidate = f"{current}\n{line}" if current else line
        if len(html.escape(candidate, quote=False)) <= limit:
            current = candidate
            continue
        if current:
            chunks.append(current)
            current = ""
        # строка сама по себе слишком длинная — режем по символам
        while len(html.escape(line, quote=False)) > limit:
            cut = limit
            while cut > 1 and len(html.escape(line[:cut], quote=False)) > limit:
                cut = max(1, cut - 50)
            space = line.rfind(" ", 0, cut)
            if space > cut // 2:
                cut = space
            chunks.append(line[:cut])
            line = line[cut:].lstrip()
        current = line
    if current:
        chunks.append(current)
    return [html.escape(c, quote=False) for c in chunks if c.strip()]


# ───────────────────────────── перевод ─────────────────────────────

class TranslationError(Exception):
    pass


def mostly_cyrillic(text: str) -> bool:
    letters = [ch for ch in text if ch.isalpha()]
    if not letters:
        return True
    cyr = sum(1 for ch in letters if "Ѐ" <= ch <= "ӿ")
    return cyr / len(letters) > 0.5


def split_long(text: str, limit: int, size=len) -> list[str]:
    """Режет длинную строку на куски ≤ limit (по предложениям, затем по словам). Куски склеиваются пробелом."""
    if size(text) <= limit:
        return [text]
    pieces: list[str] = []
    current = ""
    for sentence in re.split(r"(?<=[.!?…])\s+", text):
        while size(sentence) > limit:
            if current:
                pieces.append(current)
                current = ""
            cut = limit
            while cut > 1 and size(sentence[:cut]) > limit:
                cut -= 10
            space = sentence.rfind(" ", 0, cut)
            cut = space if space > cut // 2 else cut
            pieces.append(sentence[:cut])
            sentence = sentence[cut:].lstrip()
        if current and size(f"{current} {sentence}") > limit:
            pieces.append(current)
            current = sentence
        else:
            current = f"{current} {sentence}" if current else sentence
    if current:
        pieces.append(current)
    return pieces


BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


class TranslateProvider:
    name = "base"
    limit = 1000          # максимальный размер одного куска текста
    size = staticmethod(len)

    def __init__(self, source: str, target: str, proxy: str = ""):
        self.source, self.target = source, target
        self.session = requests.Session()
        self.session.headers["User-Agent"] = BROWSER_UA
        if proxy:
            self.session.proxies = {"http": proxy, "https": proxy}

    def _get(self, url: str, params) -> requests.Response:
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                resp = self.session.get(url, params=params, timeout=(10, 30))
                if resp.status_code == 429:
                    # обычно это блокировка IP, повторы не помогут
                    raise TranslationError(f"{self.name}: сервис ограничил запросы (429)")
                resp.raise_for_status()
                return resp
            except requests.RequestException as exc:
                last_error = exc
                time.sleep(3 * (attempt + 1))
        raise TranslationError(f"{self.name}: {last_error}")

    def translate_lines(self, lines: list[str]) -> list[str]:
        """Переводит список строк, сохраняя их количество и порядок."""
        result = []
        for line in lines:
            parts = split_long(line, self.limit, self.size)
            result.append(" ".join(self.translate_batch(parts)))
        return result

    def translate_batch(self, texts: list[str]) -> list[str]:
        return [self.translate_one(t) for t in texts]

    def translate_one(self, text: str) -> str:
        raise NotImplementedError


class GoogleClients5(TranslateProvider):
    """Google Translate через эндпоинт расширения Chrome. Без ключа."""
    name = "google"
    limit = 1500
    URL = "https://clients5.google.com/translate_a/t"

    def _query(self, texts: list[str]) -> list[str]:
        params = [("client", "dict-chrome-ex"), ("sl", self.source), ("tl", self.target)]
        params += [("q", t) for t in texts]
        try:
            data = self._get(self.URL, params).json()
        except ValueError as exc:
            raise TranslationError(f"{self.name}: непонятный ответ ({exc})")
        if isinstance(data, str):
            data = [data]
        out = []
        for item in data if isinstance(data, list) else []:
            while isinstance(item, list) and item:  # при sl=auto приходит [перевод, язык]
                item = item[0]
            if isinstance(item, str):
                out.append(item)
        return out

    def translate_one(self, text: str) -> str:
        out = self._query([text])
        if len(out) != 1 or not out[0].strip():
            raise TranslationError(f"{self.name}: пустой ответ")
        return out[0]

    def translate_lines(self, lines: list[str]) -> list[str]:
        # строки отправляются пачками по несколько штук в одном запросе
        units: list[list[str]] = [split_long(line, self.limit) for line in lines]
        flat = [piece for unit in units for piece in unit]
        translated: list[str] = []
        batch: list[str] = []

        def flush():
            if not batch:
                return
            if translated:
                time.sleep(0.3)
            out = self._query(batch)
            if len(out) != len(batch):  # пачку не поняли — переводим по одной
                out = [self.translate_one(t) for t in batch]
            translated.extend(out)
            batch.clear()

        for piece in flat:
            if batch and (sum(map(len, batch)) + len(piece) > self.limit or len(batch) >= 40):
                flush()
            batch.append(piece)
        flush()

        result, pos = [], 0
        for unit in units:
            result.append(" ".join(translated[pos:pos + len(unit)]))
            pos += len(unit)
        return result


class MyMemory(TranslateProvider):
    """MyMemory: бесплатно, 5 000 символов в день (50 000 — если указан email)."""
    name = "mymemory"
    limit = 450
    size = staticmethod(lambda s: len(s.encode("utf-8")))
    URL = "https://api.mymemory.translated.net/get"

    def __init__(self, source: str, target: str, proxy: str = "", email_addr: str = ""):
        super().__init__("en" if source == "auto" else source, target, proxy)
        self.email = email_addr

    def translate_one(self, text: str) -> str:
        params = {"q": text, "langpair": f"{self.source}|{self.target}"}
        if self.email:
            params["de"] = self.email
        try:
            data = self._get(self.URL, params).json()
        except ValueError as exc:
            raise TranslationError(f"{self.name}: непонятный ответ ({exc})")
        translated = (data.get("responseData") or {}).get("translatedText") or ""
        status = str(data.get("responseStatus", "200"))
        if status != "200" or "MYMEMORY WARNING" in translated.upper() or not translated.strip():
            raise TranslationError(f"{self.name}: {data.get('responseDetails') or translated or status}")
        time.sleep(0.2)
        return html.unescape(translated)


class Translator:
    """Переводит текст первым сработавшим сервисом из списка."""

    def __init__(self, providers: list[TranslateProvider], target: str):
        self.providers = providers
        self.target = target
        self.last_provider = ""

    def translate(self, text: str) -> str:
        if not text.strip():
            return text
        if self.target == "ru" and mostly_cyrillic(text):
            return text  # уже по-русски
        lines = text.split("\n")
        idx = [i for i, line in enumerate(lines) if any(ch.isalpha() for ch in line)]
        translated = self.translate_many([lines[i].strip() for i in idx])
        result = list(lines)
        for i, t in zip(idx, translated):
            result[i] = t
        return "\n".join(result)

    def translate_many(self, texts: list[str]) -> list[str]:
        """Переводит список строк (без переносов внутри), сохраняя порядок и количество."""
        if not texts:
            return []
        errors = []
        for provider in self.providers:
            try:
                out = provider.translate_lines(texts)
                if len(out) != len(texts):
                    raise TranslationError(f"{provider.name}: число строк в ответе не совпало")
            except TranslationError as exc:
                errors.append(str(exc))
                log.warning("Переводчик %s не сработал: %s", provider.name, exc)
                continue
            self.last_provider = provider.name
            return out
        raise TranslationError("; ".join(errors) or "нет доступных переводчиков")


PROVIDER_TITLES = {"google": "Google Translate", "mymemory": "MyMemory"}


def make_translator(cfg: Config) -> Translator | None:
    if not cfg.translate:
        return None
    providers: list[TranslateProvider] = []
    for name in cfg.translate_providers:
        if name == "google":
            providers.append(GoogleClients5(cfg.translate_from, cfg.translate_to, cfg.translate_proxy))
        elif name == "mymemory":
            providers.append(MyMemory(cfg.translate_from, cfg.translate_to, cfg.translate_proxy, cfg.mymemory_email))
        else:
            log.warning("Неизвестный переводчик в TRANSLATE_PROVIDERS: %s (есть: google, mymemory)", name)
    return Translator(providers, cfg.translate_to) if providers else None


# ───────────────────────────── письмо как HTML-файл ─────────────────────────────

HTML_SKIP_TAGS = {"script", "style", "noscript", "code", "pre", "textarea"}
_NOT_TEXT = re.compile(r"^(?:https?://\S+|www\.\S+|\S+@\S+\.\S+|[@#][\w.]+)$", re.I)
_CSS_CID = re.compile(r"""url\(\s*(['"]?)cid:([^'")\s]+)\1\s*\)""", re.I)


def extract_html_part(msg: Message) -> tuple[str | None, dict[str, str]]:
    """Возвращает HTML-версию письма и картинки, встроенные в письмо (Content-ID → data URI)."""
    html_parts: list[str] = []
    cid_map: dict[str, str] = {}
    for part in msg.walk():
        if part.is_multipart():
            continue
        cid = content_id(part)
        if cid:
            payload = part.get_payload(decode=True)
            if payload:
                cid_map[cid] = f"data:{part.get_content_type()};base64,{base64.b64encode(payload).decode()}"
        disposition = (part.get("Content-Disposition") or "").lower()
        if part.get_content_type() == "text/html" and not disposition.startswith("attachment"):
            html_parts.append(part_text(part))
    if not html_parts:
        return None, cid_map
    return max(html_parts, key=len), cid_map


def _worth_translating(core: str) -> bool:
    if len(core) < 2 or _NOT_TEXT.match(core):
        return False
    if sum(1 for ch in core if ch.isalpha()) < 2:
        return False
    return not mostly_cyrillic(core)


def translate_html(soup: BeautifulSoup, translator: "Translator") -> int:
    """Переводит текст внутри HTML, не трогая разметку. Возвращает число переведённых фрагментов."""
    items: list[tuple[NavigableString, str, str]] = []
    for node in soup.find_all(string=True):
        if type(node) is not NavigableString:  # комментарии, doctype, содержимое <style>/<script>
            continue
        names = [p.name for p in node.parents]
        if any(n in HTML_SKIP_TAGS for n in names):
            continue
        if "head" in names and node.parent.name != "title":
            continue
        text = str(node)
        core = re.sub(r"\s+", " ", text.replace("\xa0", " ")).strip()
        if _worth_translating(core):
            items.append((node, text, core))
    unique = list(dict.fromkeys(core for _, _, core in items))
    if not unique:
        return 0
    translated = dict(zip(unique, translator.translate_many(unique)))
    for node, text, core in items:
        new = (translated.get(core) or core).strip()
        lead = " " if text[:1].isspace() else ""
        trail = " " if text[-1:].isspace() else ""
        node.replace_with(NavigableString(lead + new + trail))
    return len(items)


def finalize_html(soup: BeautifulSoup, cid_map: dict[str, str], lang: str) -> bytes:
    """Делает из письма самостоятельный файл: кодировка, мобильная вёрстка, встроенные картинки."""
    if cid_map:
        for tag in soup.find_all(True):
            for attr in ("src", "background"):
                value = tag.get(attr)
                if isinstance(value, str) and value.lower().startswith("cid:"):
                    uri = cid_map.get(value[4:].strip("<> ").lower())
                    if uri:
                        tag[attr] = uri
            style = tag.get("style")
            if isinstance(style, str) and "cid:" in style.lower():
                tag["style"] = _CSS_CID.sub(
                    lambda m: f"url('{cid_map[m.group(2).lower()]}')" if m.group(2).lower() in cid_map else m.group(0),
                    style)

    for meta in soup.find_all("meta"):
        if meta.get("charset") or str(meta.get("http-equiv", "")).lower() == "content-type":
            meta.decompose()
    html_tag = soup.find("html")
    head = soup.find("head")
    if head is None:
        head = soup.new_tag("head")
        (html_tag or soup).insert(0, head)
    head.insert(0, soup.new_tag("meta", attrs={"charset": "utf-8"}))
    if not head.find("meta", attrs={"name": "viewport"}):
        head.insert(1, soup.new_tag("meta", attrs={"name": "viewport",
                                                   "content": "width=device-width, initial-scale=1"}))
    if html_tag is not None:
        html_tag["lang"] = lang

    out = str(soup)
    if not out.lstrip().lower().startswith("<!doctype"):
        out = "<!DOCTYPE html>\n" + out
    return out.encode("utf-8")


def safe_filename(name: str, limit: int = 80) -> str:
    name = re.sub(r'[\\/:*?"<>|\r\n\t]+', " ", name)
    name = re.sub(r"\s+", " ", name).strip(" .")
    return name[:limit].rstrip(" .") or "письмо"


def email_info(msg: Message) -> tuple[str, str, str, str]:
    """Тема, отправитель, дата для показа и дата для имени файла."""
    sender_name, sender_addr = parseaddr(decode_mime(msg.get("From")))
    sender = f"{sender_name} <{sender_addr}>" if sender_name else sender_addr
    subject = decode_mime(msg.get("Subject")) or "(без темы)"
    date_str, file_date = "", ""
    try:
        if msg.get("Date"):
            dt = parsedate_to_datetime(msg["Date"]).astimezone()
            date_str, file_date = dt.strftime("%d.%m.%Y %H:%M"), dt.strftime("%Y-%m-%d")
    except Exception:
        date_str = decode_mime(msg.get("Date"))
    return subject, sender, date_str, file_date


def build_header(subject: str, sender: str, date_str: str, note: str) -> str:
    header = f"📧 <b>{html.escape(subject, quote=False)}</b>\n<i>От: {html.escape(sender, quote=False)}</i>"
    if date_str:
        header += f"\n<i>{html.escape(date_str, quote=False)}</i>"
    if note:
        header += f"\n<i>{note}</i>"
    return header


def provider_note(translator: "Translator") -> str:
    name = PROVIDER_TITLES.get(translator.last_provider, translator.last_provider)
    return f"🌐 переведено ({name})"


def format_messages(msg: Message, cfg: Config, translator: "Translator | None" = None) -> list[str]:
    """Готовит текст письма для Telegram: список HTML-сообщений, каждое ≤ 4096 символов."""
    subject, sender, date_str, _ = email_info(msg)

    body = extract_body(msg, cfg.show_links)
    truncated = False
    if cfg.max_body_chars and len(body) > cfg.max_body_chars:
        body = body[: cfg.max_body_chars].rstrip()
        truncated = True

    translate_note = ""
    if translator is not None:
        try:
            new_subject = translator.translate(subject) if subject != "(без темы)" else subject
            new_body = translator.translate(body)
            if new_subject != subject or new_body != body:
                subject, body = new_subject, normalize_text(new_body)
                translate_note = provider_note(translator)
        except TranslationError as exc:
            log.warning("Перевод не удался (%s), отправляю оригинал", exc)
            translate_note = "⚠️ перевод не удался — оригинал"
    body = body or "(письмо без текста)"

    header = build_header(subject, sender, date_str, translate_note)

    first_limit = TG_TEXT_LIMIT - len(header) - 2
    body_chunks = chunk_escaped(body, 3800)
    messages: list[str] = []
    if body_chunks and len(body_chunks[0]) <= first_limit:
        messages.append(f"{header}\n\n{body_chunks.pop(0)}")
    else:
        messages.append(header)
    messages.extend(body_chunks)
    if truncated:
        note = "\n\n<i>… текст обрезан, полностью — в почте</i>"
        if len(messages[-1]) + len(note) <= TG_TEXT_LIMIT:
            messages[-1] += note
        else:
            messages.append(note.strip())
    return messages


# ───────────────────────────── IMAP ─────────────────────────────

def imap_quote(name: str) -> str:
    return '"' + name.replace("\\", "\\\\").replace('"', '\\"') + '"'


class Mailbox:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.conn: imaplib.IMAP4 | None = None

    def _ssl_context(self) -> ssl.SSLContext:
        ctx = ssl.create_default_context()
        if not self.cfg.imap_verify_tls:
            # для серверов с самоподписанным сертификатом (IMAP_VERIFY_TLS=false)
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        return ctx

    def connect(self) -> None:
        self.close()
        cfg = self.cfg
        log.info("Подключаюсь к IMAP %s:%s (%s)", cfg.imap_host, cfg.imap_port, cfg.imap_security)
        if cfg.imap_security == "ssl":
            conn = imaplib.IMAP4_SSL(cfg.imap_host, cfg.imap_port, ssl_context=self._ssl_context(), timeout=60)
        else:
            conn = imaplib.IMAP4(cfg.imap_host, cfg.imap_port, timeout=60)
            if cfg.imap_security == "starttls":
                conn.starttls(ssl_context=self._ssl_context())
        conn.login(cfg.imap_user, cfg.imap_password)
        self.conn = conn

    def close(self) -> None:
        if self.conn is not None:
            try:
                self.conn.logout()
            except Exception:
                pass
            self.conn = None

    def status(self) -> tuple[int, int]:
        """Возвращает (UIDVALIDITY, UIDNEXT) папки."""
        typ, data = self.conn.status(imap_quote(self.cfg.imap_folder), "(UIDVALIDITY UIDNEXT)")
        if typ != "OK":
            raise imaplib.IMAP4.error(f"STATUS не выполнен: {data}")
        text = b" ".join(d for d in data if isinstance(d, bytes)).decode(errors="replace")
        validity = re.search(r"UIDVALIDITY (\d+)", text)
        uidnext = re.search(r"UIDNEXT (\d+)", text)
        if not validity or not uidnext:
            raise imaplib.IMAP4.error(f"Не удалось разобрать ответ STATUS: {text}")
        return int(validity.group(1)), int(uidnext.group(1))

    def select(self) -> None:
        typ, data = self.conn.select(imap_quote(self.cfg.imap_folder), readonly=True)
        if typ != "OK":
            raise imaplib.IMAP4.error(f"Папка {self.cfg.imap_folder!r} не найдена: {data}")

    def search_uids(self, criteria: str) -> list[int]:
        typ, data = self.conn.uid("SEARCH", None, criteria)
        if typ != "OK":
            raise imaplib.IMAP4.error(f"SEARCH не выполнен: {data}")
        raw = b" ".join(d for d in data if isinstance(d, bytes))
        return sorted(int(x) for x in raw.split() if x.isdigit())

    def _fetch(self, uid: int, what: str) -> bytes | None:
        typ, data = self.conn.uid("FETCH", str(uid), what)
        if typ != "OK":
            raise imaplib.IMAP4.error(f"FETCH не выполнен: {data}")
        for item in data:
            if isinstance(item, tuple) and len(item) >= 2:
                return item[1]
        return None

    def fetch_from_header(self, uid: int) -> str | None:
        raw = self._fetch(uid, "(BODY.PEEK[HEADER.FIELDS (FROM)])")
        if raw is None:
            return None
        return email.message_from_bytes(raw).get("From", "")

    def fetch_message(self, uid: int) -> Message | None:
        raw = self._fetch(uid, "(BODY.PEEK[])")
        return email.message_from_bytes(raw) if raw else None

    def list_folders(self) -> list[str]:
        typ, data = self.conn.list()
        folders = []
        for line in data or []:
            if isinstance(line, bytes):
                match = re.search(rb'"([^"]*)"\s*$|(\S+)\s*$', line)
                if match:
                    folders.append((match.group(1) or match.group(2)).decode(errors="replace"))
        return folders


# ───────────────────────────── состояние ─────────────────────────────

def load_state(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (ValueError, OSError) as exc:
        log.warning("Не удалось прочитать %s (%s), начинаю заново", path, exc)
        return {}


def save_state(path: Path, state: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    os.replace(tmp, path)


# ───────────────────────────── основная логика ─────────────────────────────

class Forwarder:
    def __init__(self, cfg: Config, mailbox: Mailbox | None = None, tg: Telegram | None = None):
        self.cfg = cfg
        self.mailbox = mailbox or Mailbox(cfg)
        self.tg = tg or Telegram(cfg.tg_token, cfg.tg_chat_id, cfg.tg_proxy)
        self.translator = make_translator(cfg)
        self.state = load_state(cfg.state_file)

    def _save(self) -> None:
        save_state(self.cfg.state_file, self.state)

    def _init_state(self, uidvalidity: int, uidnext: int) -> list[int]:
        """Первый запуск (или папку пересоздали): запоминаем текущую позицию, опционально досылаем последние письма."""
        backlog: list[int] = []
        if self.cfg.backfill:
            backlog = self.candidate_uids()[-self.cfg.backfill:]
        self.state = {"uidvalidity": uidvalidity, "last_uid": uidnext - 1}
        self._save()
        log.info("Начальная позиция: UID %s. Пересылаться будут письма, пришедшие после неё%s.",
                 uidnext - 1, f" (+{len(backlog)} последних для проверки)" if backlog else "")
        return backlog

    def candidate_uids(self) -> list[int]:
        """UID писем, которые могут быть от нужных отправителей (поиск на стороне сервера)."""
        if not self.cfg.from_filter:
            return self.mailbox.search_uids("ALL")
        uids: set[int] = set()
        for f in self.cfg.from_filter:
            uids.update(self.mailbox.search_uids(f"FROM {imap_quote(f.lstrip('@'))}"))
        return sorted(uids)

    def resend_last(self) -> str | None:
        """Пересылает последнее письмо от нужных отправителей, не трогая сохранённую позицию."""
        if self.mailbox.conn is None:
            self.mailbox.connect()
        self.mailbox.select()
        for uid in reversed(self.candidate_uids()):
            from_header = self.mailbox.fetch_from_header(uid)
            if from_header is None or not sender_matches(decode_mime(from_header), self.cfg.from_filter):
                continue
            msg = self.mailbox.fetch_message(uid)
            if msg is not None:
                self.forward(msg)
                return decode_mime(msg.get("Subject")) or "(без темы)"
        return None

    def poll(self) -> int:
        """Один проход: проверяет новые письма и пересылает подходящие. Возвращает число отправленных."""
        if self.mailbox.conn is not None:
            # Gmail закрывает простаивающие соединения — проверяем и при необходимости переподключаемся
            try:
                self.mailbox.conn.noop()
            except Exception:
                self.mailbox.close()
        if self.mailbox.conn is None:
            self.mailbox.connect()
        uidvalidity, uidnext = self.mailbox.status()
        self.mailbox.select()

        backlog: list[int] = []
        if self.state.get("uidvalidity") != uidvalidity or "last_uid" not in self.state:
            if self.state:
                log.warning("UIDVALIDITY папки изменился, начинаю отсчёт заново")
            backlog = self._init_state(uidvalidity, uidnext)

        last_uid = int(self.state["last_uid"])
        # "UID N:*" всегда возвращает хотя бы последнее письмо, поэтому отсеиваем уже обработанные
        new_uids = [u for u in self.mailbox.search_uids(f"UID {last_uid + 1}:*") if u > last_uid]

        sent = 0
        for uid in backlog:
            sent += self._process(uid, advance=False)
        for uid in new_uids:
            sent += self._process(uid, advance=True)
        return sent

    def _process(self, uid: int, advance: bool) -> int:
        from_header = self.mailbox.fetch_from_header(uid)
        forwarded = 0
        if from_header is not None and sender_matches(decode_mime(from_header), self.cfg.from_filter):
            msg = self.mailbox.fetch_message(uid)
            if msg is not None:
                self.forward(msg)
                forwarded = 1
                log.info("Переслано письмо UID %s от %s", uid, decode_mime(from_header))
        else:
            log.debug("Пропущено письмо UID %s от %s", uid, from_header)
        if advance:
            self.state["last_uid"] = uid
            self._save()
        return forwarded

    def _html_document(self, msg: Message) -> tuple[str, bytes, str, set[str]] | None:
        """Готовит письмо как HTML-файл (с переводом). Возвращает (имя файла, содержимое, подпись, cid картинок)."""
        raw_html, cid_map = extract_html_part(msg)
        if not raw_html:
            return None
        subject, sender, date_str, file_date = email_info(msg)
        soup = BeautifulSoup(raw_html, "html.parser")
        note, lang = "", "en"
        if self.translator is not None:
            self.translator.last_provider = ""
            try:
                count = translate_html(soup, self.translator)
                if subject != "(без темы)":
                    subject = self.translator.translate(subject)
                if count and self.translator.last_provider:
                    note, lang = provider_note(self.translator), self.cfg.translate_to
            except TranslationError as exc:
                log.warning("Перевод не удался (%s), отправляю оригинал", exc)
                soup = BeautifulSoup(raw_html, "html.parser")
                note = "⚠️ перевод не удался — оригинал"
        content = finalize_html(soup, cid_map, lang)
        filename = safe_filename(f"{file_date} {subject}" if file_date else subject) + ".html"
        caption = build_header(subject, sender, date_str, note) + \
            "\n\n📄 <i>Письмо целиком — в файле. Нажмите на него и откройте в браузере.</i>"
        used_cids = {cid for cid in cid_map if f"cid:{cid}" in raw_html.lower()}
        return filename, content, caption, used_cids

    def forward(self, msg: Message) -> None:
        first_id = None
        used_cids: set[str] = set()
        sent_html = False
        if self.cfg.delivery in ("html", "both"):
            doc = self._html_document(msg)
            if doc is not None:
                filename, content, caption, used_cids = doc
                try:
                    first_id = self.tg.send_document(filename, content, caption=caption, html_mode=True)
                except TelegramPermanentError as exc:
                    log.warning("Telegram отклонил подпись (%s), отправляю без форматирования", exc)
                    plain = html.unescape(re.sub(r"<[^>]+>", "", caption))
                    first_id = self.tg.send_document(filename, content, caption=plain)
                sent_html = True

        if not sent_html or self.cfg.delivery == "both":
            for text in format_messages(msg, self.cfg, self.translator):
                try:
                    mid = self.tg.send_text(text, html_mode=True, reply_to=first_id)
                except TelegramPermanentError as exc:
                    # например, Telegram не принял разметку — отправим без неё
                    log.warning("Telegram отклонил сообщение (%s), отправляю без форматирования", exc)
                    plain = re.sub(r"<[^>]+>", "", text)
                    mid = self.tg.send_text(html.unescape(plain), html_mode=False, reply_to=first_id)
                if first_id is None:
                    first_id = mid

        if not self.cfg.send_attachments:
            return
        limit = self.cfg.max_attachment_mb * 1024 * 1024
        for name, content in extract_attachments(msg, used_cids):
            size_mb = len(content) / 1024 / 1024
            if len(content) > limit:
                self.tg.send_text(f"📎 Вложение «{name}» ({size_mb:.1f} МБ) больше лимита "
                                  f"{self.cfg.max_attachment_mb} МБ — смотрите в почте.", reply_to=first_id)
                continue
            try:
                self.tg.send_document(name, content, reply_to=first_id)
            except TelegramPermanentError as exc:
                log.warning("Не удалось отправить вложение %s: %s", name, exc)


# ───────────────────────────── команды ─────────────────────────────

def auth_hint(exc: Exception) -> str:
    text = str(exc).lower()
    if "authenticationfailed" in text or "invalid credentials" in text or "application-specific password" in text:
        return ("\n   Подсказка: для Gmail в IMAP_PASSWORD нужен пароль приложения "
                "(https://myaccount.google.com/apppasswords), а не обычный пароль. "
                "Пароль приложения сбрасывается при смене пароля Google.")
    return ""


class GracefulExit:
    def __init__(self):
        self.stop = False
        signal.signal(signal.SIGTERM, self._handler)
        signal.signal(signal.SIGINT, self._handler)

    def _handler(self, *_):
        log.info("Получен сигнал остановки")
        self.stop = True

    def sleep(self, seconds: float) -> None:
        end = time.monotonic() + seconds
        while not self.stop and time.monotonic() < end:
            time.sleep(min(1.0, end - time.monotonic()))


def run_forever(cfg: Config) -> None:
    cfg.require("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "IMAP_USER", "IMAP_PASSWORD")
    forwarder = Forwarder(cfg)
    exit_flag = GracefulExit()
    log.info("mail2tg запущен. Папка: %s, фильтр: %s, интервал: %s с",
             cfg.imap_folder, ", ".join(cfg.from_filter) or "все письма", cfg.poll_interval)
    backoff = cfg.poll_interval
    while not exit_flag.stop:
        try:
            forwarder.poll()
            backoff = cfg.poll_interval
        except TelegramTemporaryError as exc:
            log.error("Telegram недоступен: %s. Письмо будет отправлено при следующей проверке.", exc)
        except TelegramPermanentError as exc:
            log.error("Telegram отклонил запрос: %s. Проверьте TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID.", exc)
            backoff = min(backoff * 2, 600)
        except (imaplib.IMAP4.error, OSError, socket.timeout, ssl.SSLError) as exc:
            log.error("Ошибка IMAP: %s. Переподключусь через %s с.%s", exc, backoff, auth_hint(exc))
            forwarder.mailbox.close()
            exit_flag.sleep(backoff)
            backoff = min(backoff * 2, 600)
            continue
        except Exception:
            log.exception("Непредвиденная ошибка")
            forwarder.mailbox.close()
            backoff = min(backoff * 2, 600)
        exit_flag.sleep(backoff)
    forwarder.mailbox.close()
    log.info("mail2tg остановлен")


def run_once(cfg: Config) -> None:
    cfg.require("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "IMAP_USER", "IMAP_PASSWORD")
    forwarder = Forwarder(cfg)
    try:
        sent = forwarder.poll()
        print(f"Готово, переслано писем: {sent}")
    finally:
        forwarder.mailbox.close()


def run_resend_last(cfg: Config) -> None:
    cfg.require("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "IMAP_USER", "IMAP_PASSWORD")
    forwarder = Forwarder(cfg)
    try:
        subject = forwarder.resend_last()
        print(f"Отправлено: {subject}" if subject else "Подходящих писем в папке не найдено.")
    finally:
        forwarder.mailbox.close()


def run_check(cfg: Config) -> None:
    ok = True
    print("1) Почта")
    if not (cfg.imap_user and cfg.imap_password):
        print("   ✗ Не заполнены IMAP_USER / IMAP_PASSWORD")
        ok = False
    else:
        box = Mailbox(cfg)
        try:
            box.connect()
            print(f"   ✓ Вход выполнен ({cfg.imap_host}:{cfg.imap_port})")
            folders = box.list_folders()
            print(f"   Папки: {', '.join(folders) if folders else '(список пуст)'}")
            validity, uidnext = box.status()
            print(f"   ✓ Папка {cfg.imap_folder}: писем примерно {uidnext - 1} (UIDNEXT={uidnext})")
        except Exception as exc:
            print(f"   ✗ Ошибка: {exc}{auth_hint(exc)}")
            ok = False
        finally:
            box.close()

    print("2) Telegram")
    if not cfg.tg_token:
        print("   ✗ Не заполнен TELEGRAM_BOT_TOKEN")
        ok = False
    else:
        tg = Telegram(cfg.tg_token, cfg.tg_chat_id, cfg.tg_proxy)
        try:
            me = tg.call("getMe", attempts=2)
            print(f"   ✓ Бот: @{me.get('username')}")
            if not cfg.tg_chat_id:
                print("   ✗ Не заполнен TELEGRAM_CHAT_ID (узнать: python3 mail2tg.py --get-chat-id)")
                ok = False
            else:
                tg.send_text("✅ mail2tg: тестовое сообщение, всё настроено.")
                print("   ✓ Тестовое сообщение отправлено")
        except Exception as exc:
            print(f"   ✗ Ошибка: {exc}")
            ok = False

    print("3) Перевод")
    translator = make_translator(cfg)
    translate_ok = True
    if translator is None:
        print("   выключен (TRANSLATE=false)")
    else:
        sample = "Hello! This is a test of the translation.\nSecond line."
        working = []
        for provider in translator.providers:
            title = PROVIDER_TITLES.get(provider.name, provider.name)
            try:
                result = Translator([provider], cfg.translate_to).translate(sample)
                print(f"   ✓ {title}: «{result.replace(chr(10), ' / ')}»")
                working.append(title)
            except TranslationError as exc:
                print(f"   ✗ {title}: {exc}")
        if working:
            print(f"   Будет использоваться: {working[0]}"
                  + (f" (запасной: {', '.join(working[1:])})" if len(working) > 1 else ""))
        else:
            translate_ok = False
            print("   Ни один переводчик не работает — письма будут приходить без перевода.\n"
                  "   Можно задать TRANSLATE_PROXY.")

    print(f"4) Фильтр отправителей: {', '.join(cfg.from_filter) or 'не задан — будут пересылаться ВСЕ письма'}")
    if not ok:
        print("\nЕсть ошибки, исправьте .env и запустите проверку снова.")
    elif not translate_ok:
        print("\nПочта и Telegram в порядке, но перевод не работает (письма будут приходить в оригинале).")
    else:
        print("\nВсё в порядке.")
    sys.exit(0 if ok else 1)


def run_get_chat_id(cfg: Config) -> None:
    cfg.require("TELEGRAM_BOT_TOKEN")
    tg = Telegram(cfg.tg_token, proxy=cfg.tg_proxy)
    me = tg.call("getMe", attempts=2)
    updates = tg.call("getUpdates", {"timeout": 0}, attempts=2)
    chats = {}
    for upd in updates:
        for key in ("message", "channel_post", "my_chat_member", "edited_message"):
            chat = (upd.get(key) or {}).get("chat")
            if chat:
                title = chat.get("title") or " ".join(
                    filter(None, [chat.get("first_name"), chat.get("last_name")])) or chat.get("username", "")
                chats[chat["id"]] = f"{title} ({chat.get('type')})"
    if not chats:
        print(f"Сообщений не найдено. Откройте https://t.me/{me.get('username')}, нажмите «Старт» "
              f"(или напишите что-нибудь) и запустите команду снова.")
        return
    print("Найденные чаты — скопируйте нужный id в TELEGRAM_CHAT_ID:")
    for chat_id, title in chats.items():
        print(f"  {chat_id}\t{title}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Пересылка писем из IMAP в Telegram")
    parser.add_argument("--env", default=str(BASE_DIR / ".env"), help="путь к .env (по умолчанию рядом со скриптом)")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--check", action="store_true", help="проверить подключение к почте и Telegram")
    group.add_argument("--get-chat-id", action="store_true", help="показать chat_id из последних сообщений боту")
    group.add_argument("--once", action="store_true", help="один проход и выход")
    group.add_argument("--resend-last", action="store_true",
                       help="переслать последнее письмо от нужных отправителей (для проверки)")
    args = parser.parse_args()

    load_env_file(Path(args.env))
    logging.basicConfig(
        level=env_str("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    cfg = Config.from_env()

    if args.check:
        run_check(cfg)
    elif args.get_chat_id:
        run_get_chat_id(cfg)
    elif args.resend_last:
        run_resend_last(cfg)
    elif args.once:
        run_once(cfg)
    else:
        run_forever(cfg)


if __name__ == "__main__":
    main()
