#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Telega Detector Bot v2.0
Проверяет, использует ли пользователь клиент Telega (VK).
Асинхронный, с кэшем, rate-limit и поддержкой HTML-разметки.
"""

import os
import sys
import time
import json
import asyncio
import logging
import html as html_lib
from logging.handlers import RotatingFileHandler
from typing import Optional, Dict, Tuple, Final
from collections import OrderedDict
from functools import wraps

import httpx
from telegram import Update, constants
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters
from telegram.error import TelegramError, RetryAfter

# ==============================================================================
# CONFIGURATION
# ==============================================================================
class Config:
    BOT_TOKEN: Final[str] = os.getenv("BOT_TOKEN", "").strip()
    API_BASE_URL: Final[str] = os.getenv("API_BASE_URL", "https://calls.okcdn.ru")
    API_KEY: Final[str] = os.getenv("API_KEY", "CHKIPMKGDIHBABABA")
    # SESSION_DATA оставлен без изменений, как в оригинале
    SESSION_DATA: Final[str] = os.getenv(
        "SESSION_DATA",
        '{"device_id":"telega_alert","version":2,"client_version":"android_8","client_type":"SDK_ANDROID"}'
    )
    CACHE_TTL_SECONDS: Final[int] = int(os.getenv("CACHE_TTL", "21600"))
    CACHE_MAX_SIZE: Final[int] = int(os.getenv("CACHE_MAX_SIZE", "1000"))
    API_TIMEOUT_SECONDS: Final[int] = int(os.getenv("API_TIMEOUT", "15"))
    API_MAX_RETRIES: Final[int] = int(os.getenv("API_MAX_RETRIES", "3"))
    RATE_LIMIT_PER_USER: Final[int] = int(os.getenv("RATE_LIMIT_PER_USER", "10"))
    RATE_LIMIT_WINDOW: Final[int] = int(os.getenv("RATE_LIMIT_WINDOW", "60"))
    USE_WEBHOOK: Final[bool] = os.getenv("USE_WEBHOOK", "false").lower() == "true"
    WEBHOOK_URL: Final[str] = os.getenv("WEBHOOK_URL", "").rstrip("/")
    PORT: Final[int] = int(os.getenv("PORT", "8080"))
    LOG_LEVEL: Final[str] = os.getenv("LOG_LEVEL", "INFO").upper()

    @classmethod
    def validate(cls) -> bool:
        if not cls.BOT_TOKEN:
            print("❌ ERROR: Переменная окружения BOT_TOKEN не установлена!", file=sys.stderr)
            return False
        return True

# ==============================================================================
# LOGGING
# ==============================================================================
def setup_logging() -> logging.Logger:
    logger = logging.getLogger("telega_bot")
    logger.setLevel(getattr(logging, Config.LOG_LEVEL))
    
    fmt_console = logging.Formatter("%(asctime)s | %(levelname)-8s | %(message)s", datefmt="%H:%M:%S")
    fmt_file = logging.Formatter("%(asctime)s | %(levelname)s | %(name)s:%(lineno)d | %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt_console)
    
    file_handler = RotatingFileHandler("telega_bot.log", maxBytes=10*1024*1024, backupCount=3, encoding="utf-8")
    file_handler.setFormatter(fmt_file)
    
    logger.addHandler(console)
    logger.addHandler(file_handler)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("telegram").setLevel(logging.WARNING)
    return logger

log = setup_logging()

# ==============================================================================
# UTILS
# ==============================================================================
def safe_html(text: str) -> str:
    """Экранирует < > & для безопасного использования в parse_mode='HTML'"""
    return html_lib.escape(str(text))

# ==============================================================================
# TTL CACHE
# ==============================================================================
class TTLCache:
    def __init__(self, ttl: int, max_size: int):
        self.ttl = ttl
        self.max_size = max_size
        self._cache: OrderedDict[str, Tuple[Optional[bool], float]] = OrderedDict()
        self._lock = asyncio.Lock()

    async def get(self, key: str) -> Optional[bool]:
        async with self._lock:
            if key not in self._cache:
                return None
            value, ts = self._cache[key]
            if time.time() - ts >= self.ttl:
                del self._cache[key]
                return None
            self._cache.move_to_end(key)
            return value

    async def set(self, key: str, value: Optional[bool]):
        async with self._lock:
            if key in self._cache:
                self._cache.move_to_end(key)
            elif len(self._cache) >= self.max_size:
                self._cache.popitem(last=False)
            self._cache[key] = (value, time.time())

    async def clear(self) -> int:
        async with self._lock:
            count = len(self._cache)
            self._cache.clear()
            return count

    async def stats(self) -> dict:
        async with self._lock:
            now = time.time()
            valid = sum(1 for _, ts in self._cache.values() if now - ts < self.ttl)
            return {"total": len(self._cache), "valid": valid, "expired": len(self._cache) - valid}

cache = TTLCache(Config.CACHE_TTL_SECONDS, Config.CACHE_MAX_SIZE)

# ==============================================================================
# RATE LIMITER
# ==============================================================================
class RateLimiter:
    def __init__(self, max_req: int, window: int):
        self.max_req = max_req
        self.window = window
        self._requests: Dict[int, list[float]] = {}
        self._lock = asyncio.Lock()

    async def is_allowed(self, uid: int) -> Tuple[bool, Optional[float]]:
        async with self._lock:
            now = time.time()
            if uid not in self._requests:
                self._requests[uid] = []
            self._requests[uid] = [t for t in self._requests[uid] if now - t < self.window]
            if len(self._requests[uid]) >= self.max_req:
                wait = self.window - (now - min(self._requests[uid]))
                return False, max(0.1, wait)
            self._requests[uid].append(now)
            return True, None

rate_limiter = RateLimiter(Config.RATE_LIMIT_PER_USER, Config.RATE_LIMIT_WINDOW)

def rate_limit(name: str):
    def decorator(func):
        @wraps(func)
        async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
            uid = (update.effective_user or {}).get("id", 0)
            allowed, wait = await rate_limiter.is_allowed(uid)
            if not allowed:
                await update.message.reply_text(f"⏳ Лимит запросов. Попробуй через {int(wait)} сек.")
                return
            return await func(update, context)
        return wrapper
    return decorator

# ==============================================================================
# API CLIENT
# ==============================================================================
class TelegaAPIClient:
    def __init__(self):
        self._session_key: Optional[str] = None
        self._session_expires: float = 0
        self._client: Optional[httpx.AsyncClient] = None

    async def __aenter__(self):
        self._client = httpx.AsyncClient(
            timeout=Config.API_TIMEOUT_SECONDS,
            headers={"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"},
            follow_redirects=True
        )
        return self

    async def __aexit__(self, *args):
        if self._client: await self._client.aclose()

    async def _get_session(self) -> Optional[str]:
        now = time.time()
        if self._session_key and now < self._session_expires:
            return self._session_key

        for attempt in range(Config.API_MAX_RETRIES):
            try:
                r = await self._client.post(
                    f"{Config.API_BASE_URL}/api/auth/anonymLogin",
                    data={"application_key": Config.API_KEY, "session_data": Config.SESSION_DATA}
                )
                r.raise_for_status()
                data = r.json()
                sk = data.get("session_key")
                if sk:
                    self._session_key = sk
                    self._session_expires = now + min(data.get("expires_in", 3600), 3600)
                    return sk
            except Exception as e:
                log.warning(f"Session retry {attempt+1}: {e}")
            if attempt < Config.API_MAX_RETRIES - 1:
                await asyncio.sleep(2 ** attempt)
        return None

    async def check_user(self, user_id: int) -> Optional[bool]:
        sk = await self._get_session()
        if not sk: return None

        for attempt in range(Config.API_MAX_RETRIES):
            try:
                r = await self._client.post(
                    f"{Config.API_BASE_URL}/api/vchat/getOkIdsByExternalIds",
                    data={
                        "application_key": Config.API_KEY,
                        "session_key": sk,
                        "externalIds": f'[{{"id":"{user_id}","ok_anonym":false}}]'
                    }
                )
                r.raise_for_status()
                ids = r.json().get("ids") or []
                for item in ids:
                    if not isinstance(item, dict): continue
                    ext = (item.get("external_user_id") or {})
                    if str(ext.get("id") or "") == str(user_id):
                        return True
                return False
            except Exception as e:
                log.warning(f"Check retry {attempt+1} for {user_id}: {e}")
            if attempt < Config.API_MAX_RETRIES - 1:
                await asyncio.sleep(2 ** attempt)
        return None

# ==============================================================================
# HANDLERS
# ==============================================================================
@rate_limit("start")
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await update.message.reply_text(
        f"🔍 <b>Telega IQ Detector</b>\n\n"
        f"Привет, {safe_html(user.first_name)}! Проверяю, использует ли человек "
        f"небезопасный клиент <b>Telega</b>.\n\n"
        f"📋 <b>Как использовать:</b>\n"
        f"• Отправь числовой ID: <code>123456789</code>\n"
        f"• Перешли сообщение от пользователя\n"
        f"• <code>/help</code> — инструкция",
        parse_mode="HTML"
    )

@rate_limit("help")
async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "ℹ️ <b>Инструкция</b>\n\n"
        "1️⃣ Узнай ID: отправь @userinfobot сообщение от пользователя\n"
        "2️⃣ Проверь здесь: отправь числовой ID или перешли сообщение\n\n"
        "📊 <b>Результаты:</b>\n"
        "• 🤡 Telega — использует небезопасный клиент\n"
        "• ✅ Clean — Telega не обнаружен\n"
        "• ⚠️ Error — сервис временно недоступен",
        parse_mode="HTML"
    )

@rate_limit("cache")
async def cmd_cache(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    admin_ids = [int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.isdigit()]
    if user.id not in admin_ids:
        return
    args = context.args or []
    if args and args[0] == "clear":
        count = await cache.clear()
        await update.message.reply_text(f"🗑️ Кэш очищен: {count} записей")
    elif args and args[0] == "stats":
        s = await cache.stats()
        await update.message.reply_text(
            f"📊 <b>Кэш</b>\n• Всего: {s['total']}\n• Валидных: {s['valid']}\n• TTL: {s['ttl']//3600}ч",
            parse_mode="HTML"
        )
    else:
        await update.message.reply_text("🗄️ <code>/cache stats</code> | <code>/cache clear</code> (админ)", parse_mode="HTML")

async def handle_forward(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    fwd = msg.forward_from
    if not fwd:
        await msg.reply_text("❌ Не удалось получить ID. Возможно, форвард скрыт.", parse_mode="HTML")
        return
    name = f"<a href='tg://user?id={fwd.id}'>{safe_html(fwd.first_name or 'User')}</a>"
    await _check_and_reply(msg, fwd.id, name, context)

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text.startswith("/"): return
    if text.startswith("@"):
        await update.message.reply_text("⚠️ Нужен числовой ID. Используй @userinfobot", parse_mode="HTML")
        return
    try:
        uid = int(text)
    except ValueError:
        await update.message.reply_text("❌ Это не числовой ID. Пример: <code>123456789</code>", parse_mode="HTML")
        return
    if uid <= 0 or uid > 9999999999:
        await update.message.reply_text("❌ Некорректный ID", parse_mode="HTML")
        return
    await _check_and_reply(update.message, uid, f"<code>{uid}</code>", context)

async def _check_and_reply(message, user_id: int, display_name: str, context: ContextTypes.DEFAULT_TYPE):
    await context.bot.send_chat_action(chat_id=message.chat_id, action=constants.ChatAction.TYPING)
    
    cached = await cache.get(str(user_id))
    if cached is not None:
        result = cached
    else:
        async with TelegaAPIClient() as api:
            result = await api.check_user(user_id)
            if result is not None:
                await cache.set(str(user_id), result)

    if result is True:
        txt = f"🤡 <b>{display_name}</b>\n\nИспользует <b>Telega</b> от ВКонтакте 🔴\n\n⚠️ Это небезопасно:\n• Данные могут передаваться третьим лицам\n• Возможна компрометация аккаунта"
    elif result is False:
        txt = f"✅ <b>{display_name}</b>\n\nTelega <b>не обнаружен</b> 🟢\n\nПользователь использует официальный клиент."
    else:
        txt = f"⚠️ <b>{display_name}</b>\n\nНе удалось проверить — сервис временно недоступен."

    await message.reply_text(txt, parse_mode="HTML", disable_web_page_preview=True)

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    error = context.error
    log.error(f"Update error: {error}", exc_info=error)
    if update and update.effective_message:
        if isinstance(error, RetryAfter):
            await update.effective_message.reply_text(f"⏳ Подождите {error.retry_after} сек.")
        else:
            # Без parse_mode, чтобы не вызывать вложенных ошибок
            await update.effective_message.reply_text("❌ Внутренняя ошибка. Попробуй позже.")

# ==============================================================================
# MAIN
# ==============================================================================
def main():
    if not Config.validate():
        sys.exit(1)
    log.info("🚀 Starting Telega Detector Bot...")

    app = Application.builder().token(Config.BOT_TOKEN).build()
    
    # Регистрируем хендлеры
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("cache", cmd_cache))
    app.add_handler(MessageHandler(filters.FORWARDED, handle_forward))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_error_handler(error_handler)

    if Config.USE_WEBHOOK and Config.WEBHOOK_URL:
        log.info(f"🌐 Webhook mode: {Config.WEBHOOK_URL}/{Config.BOT_TOKEN}")
        app.run_webhook(
            listen="0.0.0.0", port=Config.PORT,
            url_path=f"/{Config.BOT_TOKEN}",
            webhook_url=f"{Config.WEBHOOK_URL}/{Config.BOT_TOKEN}",
            drop_pending_updates=True
        )
    else:
        log.info("📡 Polling mode (with auto-conflict handling)")
        from telegram.error import Conflict
        import time
        
        # Запускаем polling с обработкой конфликтов деплоя
        max_retries = 5
        for attempt in range(max_retries):
            try:
                app.run_polling(drop_pending_updates=True)
                break  # Успешный запуск
            except Conflict:
                wait_sec = 20 * (attempt + 1)
                log.warning(f"⚠️ Polling conflict! Another instance is active. Retrying in {wait_sec}s... ({attempt+1}/{max_retries})")
                time.sleep(wait_sec)
            except Exception as e:
                log.error(f"❌ Fatal polling error: {e}")
                break
        else:
            log.error("❌ Failed to start polling after retries. Check for zombie instances.")

if __name__ == "__main__":
    main()