#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Telega Detector Bot — проверяет, использует ли пользователь клиент Telega (VK)

Features:
• Асинхронные запросы к API (не блокирует бота)
• Умный кэш с авто-очисткой и лимитом размера
• Retry с экспоненциальной задержкой при ошибках сети
• Rate limiting на команды пользователей
• Конфигурация через переменные окружения
• Индикация "печатает..." для лучшего UX
• Структурированное логирование с ротацией файлов
• Поддержка webhook и polling режимов
"""

# === ИМПОРТЫ ===
import os
import sys
import time
import json
import asyncio
import logging
from datetime import datetime, timedelta
from typing import Optional, Dict, Tuple, Final
from collections import OrderedDict
from functools import wraps

import httpx
from telegram import Update, constants
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
    Defaults,
)
from telegram.error import TelegramError, RetryAfter

# === КОНФИГУРАЦИЯ ===
class Config:
    """Централизованная конфигурация"""
    
    # Обязательные
    BOT_TOKEN: Final[str] = os.getenv("BOT_TOKEN", "").strip()
    
    # API настройки
    API_BASE_URL: Final[str] = os.getenv("API_BASE_URL", "https://calls.okcdn.ru")
    API_KEY: Final[str] = os.getenv("API_KEY", "CHKIPMKGDIHBABABA")
    
    # ⚠️ SESSION_DATA — оставлен как в оригинале, без изменений!
    SESSION_DATA: Final[str] = os.getenv(
        "SESSION_DATA",
        '{"device_id":"telega_alert","version":2,"client_version":"android_8","client_type":"SDK_ANDROID"}'
    )
    
    # Поведение
    CACHE_TTL_SECONDS: Final[int] = int(os.getenv("CACHE_TTL", "21600"))  # 6 часов
    CACHE_MAX_SIZE: Final[int] = int(os.getenv("CACHE_MAX_SIZE", "1000"))
    API_TIMEOUT_SECONDS: Final[int] = int(os.getenv("API_TIMEOUT", "15"))
    API_MAX_RETRIES: Final[int] = int(os.getenv("API_MAX_RETRIES", "3"))
    
    # Rate limiting
    RATE_LIMIT_PER_USER: Final[int] = int(os.getenv("RATE_LIMIT_PER_USER", "10"))
    RATE_LIMIT_WINDOW: Final[int] = int(os.getenv("RATE_LIMIT_WINDOW", "60"))
    
    # Режим запуска
    USE_WEBHOOK: Final[bool] = os.getenv("USE_WEBHOOK", "false").lower() == "true"
    WEBHOOK_URL: Final[str] = os.getenv("WEBHOOK_URL", "").rstrip("/")
    PORT: Final[int] = int(os.getenv("PORT", "8080"))
    
    # Логирование
    LOG_LEVEL: Final[str] = os.getenv("LOG_LEVEL", "INFO").upper()
    LOG_FILE: Final[str] = os.getenv("LOG_FILE", "telega_bot.log")
    
    @classmethod
    def validate(cls) -> bool:
        if not cls.BOT_TOKEN or cls.BOT_TOKEN == "YOUR_BOT_TOKEN_HERE":
            print("❌ ERROR: BOT_TOKEN не установлен!", file=sys.stderr)
            return False
        return True

# === ЛОГИРОВАНИЕ ===
def setup_logging() -> logging.Logger:
    from logging.handlers import RotatingFileHandler
    
    logger = logging.getLogger("telega_bot")
    logger.setLevel(getattr(logging, Config.LOG_LEVEL))
    
    console_format = logging.Formatter("%(asctime)s | %(levelname)-8s | %(message)s", datefmt="%H:%M:%S")
    file_format = logging.Formatter("%(asctime)s | %(levelname)s | %(name)s:%(lineno)d | %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    
    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.INFO)
    console.setFormatter(console_format)
    
    file_handler = RotatingFileHandler(Config.LOG_FILE, maxBytes=10*1024*1024, backupCount=5, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(file_format)
    
    logger.addHandler(console)
    logger.addHandler(file_handler)
    
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("telegram").setLevel(logging.WARNING)
    
    return logger

log = setup_logging()

# === УМНЫЙ КЭШ ===
class TTLCache:
    """LRU-кэш с TTL и авто-очисткой"""
    
    def __init__(self, ttl_seconds: int, max_size: int):
        self.ttl = ttl_seconds
        self.max_size = max_size
        self._cache: OrderedDict[str, Tuple[Optional[bool], float]] = OrderedDict()
        self._lock = asyncio.Lock()
    
    async def get(self, key: str) -> Optional[bool]:
        async with self._lock:
            if key not in self._cache:
                return None
            value, timestamp = self._cache[key]
            if time.time() - timestamp >= self.ttl:
                del self._cache[key]
                return None
            self._cache.move_to_end(key)
            log.debug(f"Cache HIT: {key} → {value}")
            return value
    
    async def set(self, key: str, value: Optional[bool]):
        async with self._lock:
            if key in self._cache:
                self._cache.move_to_end(key)
            elif len(self._cache) >= self.max_size:
                self._cache.popitem(last=False)
            self._cache[key] = (value, time.time())
    
    async def clear(self):
        async with self._lock:
            count = len(self._cache)
            self._cache.clear()
            log.info(f"Cache cleared: {count} entries")
            return count
    
    async def stats(self) -> Dict[str, int]:
        async with self._lock:
            now = time.time()
            valid = sum(1 for _, ts in self._cache.values() if now - ts < self.ttl)
            return {"total": len(self._cache), "valid": valid, "expired": len(self._cache) - valid}

cache = TTLCache(Config.CACHE_TTL_SECONDS, Config.CACHE_MAX_SIZE)

# === RATE LIMITER ===
class RateLimiter:
    def __init__(self, max_requests: int, window_seconds: int):
        self.max_requests = max_requests
        self.window = window_seconds
        self._requests: Dict[int, list[float]] = {}
        self._lock = asyncio.Lock()
    
    async def is_allowed(self, user_id: int) -> Tuple[bool, Optional[float]]:
        async with self._lock:
            now = time.time()
            if user_id not in self._requests:
                self._requests[user_id] = []
            self._requests[user_id] = [ts for ts in self._requests[user_id] if now - ts < self.window]
            if len(self._requests[user_id]) >= self.max_requests:
                oldest = min(self._requests[user_id])
                wait_time = self.window - (now - oldest)
                return False, max(0.1, wait_time)
            self._requests[user_id].append(now)
            return True, None
    
    async def cleanup(self):
        async with self._lock:
            now = time.time()
            inactive = [uid for uid, ts_list in self._requests.items() if all(now - t >= self.window for t in ts_list)]
            for uid in inactive:
                del self._requests[uid]

rate_limiter = RateLimiter(Config.RATE_LIMIT_PER_USER, Config.RATE_LIMIT_WINDOW)

def rate_limit(command_name: str):
    def decorator(func):
        @wraps(func)
        async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
            user = update.effective_user
            if not user:
                return await func(update, context)
            allowed, wait_time = await rate_limiter.is_allowed(user.id)
            if not allowed:
                log.warning(f"Rate limit: user {user.id} on {command_name}")
                await update.message.reply_text(f"⏳ Слишком много запросов. Попробуйте через {int(wait_time)} сек.")
                return
            try:
                return await func(update, context)
            finally:
                if user.id % 100 == 0:
                    asyncio.create_task(rate_limiter.cleanup())
        return wrapper
    return decorator

# === API CLIENT ===
class TelegaAPIClient:
    def __init__(self):
        self._session_key: Optional[str] = None
        self._session_expires: float = 0
        self._client: Optional[httpx.AsyncClient] = None
    
    async def __aenter__(self):
        self._client = httpx.AsyncClient(
            timeout=Config.API_TIMEOUT_SECONDS,
            headers={
                "Accept": "application/json",
                "User-Agent": "TelegaBot/1.0",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            follow_redirects=True,
        )
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self._client:
            await self._client.aclose()
    
    async def _get_session_key(self) -> Optional[str]:
        now = time.time()
        if self._session_key and now < self._session_expires:
            return self._session_key
        
        for attempt in range(Config.API_MAX_RETRIES):
            try:
                response = await self._client.post(
                    f"{Config.API_BASE_URL}/api/auth/anonymLogin",
                    data={
                        "application_key": Config.API_KEY,
                        "session_data": Config.SESSION_DATA,  # ← Оригинальный SESSION_DATA
                    },
                )
                response.raise_for_status()
                data = response.json()
                session_key = data.get("session_key")
                if session_key:
                    expires_in = data.get("expires_in", 3600)
                    self._session_key = session_key
                    self._session_expires = now + min(expires_in, 3600)
                    log.info(f"Session key obtained, expires in {expires_in}s")
                    return session_key
            except httpx.TimeoutException:
                log.warning(f"Timeout attempt {attempt+1}/{Config.API_MAX_RETRIES}")
            except httpx.HTTPStatusError as e:
                log.warning(f"HTTP {e.response.status_code} attempt {attempt+1}")
                if e.response.status_code >= 500:
                    continue
                break
            except Exception as e:
                log.warning(f"Error attempt {attempt+1}: {e}")
            if attempt < Config.API_MAX_RETRIES - 1:
                await asyncio.sleep(2 ** attempt)
        log.error("Failed to get session_key")
        return None
    
    async def check_user(self, user_id: int) -> Optional[bool]:
        session_key = await self._get_session_key()
        if not session_key:
            return None
        
        for attempt in range(Config.API_MAX_RETRIES):
            try:
                response = await self._client.post(
                    f"{Config.API_BASE_URL}/api/vchat/getOkIdsByExternalIds",
                    data={
                        "application_key": Config.API_KEY,
                        "session_key": session_key,
                        "externalIds": f'[{{"id":"{user_id}","ok_anonym":false}}]',
                    },
                )
                response.raise_for_status()
                data = response.json()
                ids = data.get("ids") or []
                for item in ids:
                    if not isinstance(item, dict):
                        continue
                    ext = item.get("external_user_id") or {}
                    if str(ext.get("id") or "") == str(user_id):
                        return True
                return False
            except httpx.TimeoutException:
                log.warning(f"Timeout checking {user_id}, attempt {attempt+1}")
            except httpx.HTTPStatusError as e:
                log.warning(f"HTTP {e.response.status_code} for {user_id}")
                if e.response.status_code >= 500:
                    continue
                return None
            except json.JSONDecodeError as e:
                log.error(f"JSON decode error for {user_id}: {e}")
                return None
            except Exception as e:
                log.error(f"Unexpected error for {user_id}: {e}")
            if attempt < Config.API_MAX_RETRIES - 1:
                await asyncio.sleep(2 ** attempt)
        log.error(f"Failed to check user {user_id}")
        return None

# === HANDLERS ===

@rate_limit("start")
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    log.info(f"User {user.id} started bot")
    await update.message.reply_text(
        f"🔍 *Telega IQ Detector*\n\n"
        f"Привет, {user.first_name}! Проверяю, использует ли человек небезопасный клиент *Telega*.\n\n"
        f"📋 *Как использовать:*\n"
        f"• Отправь числовой ID: `123456789`\n"
        f"• Перешли сообщение от пользователя\n"
        f"• /help — инструкция",
        parse_mode=constants.ParseMode.MARKDOWN_V2,
    )

@rate_limit("help")
async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "ℹ️ *Инструкция*\n\n"
        "1️⃣ Узнай ID: отправь @userinfobot сообщение от пользователя\n"
        "2️⃣ Проверь здесь: отправь числовой ID или перешли сообщение\n\n"
        "📊 *Результаты:*\n"
        "• 🤡 Telega — использует небезопасный клиент\n"
        "• ✅ Clean — Telega не обнаружен\n"
        "• ⚠️ Error — сервис временно недоступен",
        parse_mode=constants.ParseMode.MARKDOWN_V2,
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
        stats = await cache.stats()
        text = f"📊 *Кэш*\n• Всего: {stats['total']}\n• Валидных: {stats['valid']}\n• TTL: {stats['ttl_seconds']//3600}ч"
        await update.message.reply_text(text, parse_mode=constants.ParseMode.MARKDOWN_V2)
    else:
        await update.message.reply_text("🗄️ `/cache stats` | `/cache clear` (только админ)", parse_mode=constants.ParseMode.MARKDOWN_V2)

async def handle_forward(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    forwarded = msg.forward_from
    if not forwarded:
        await msg.reply_text("❌ Не удалось получить ID из пересланного сообщения. Введи ID вручную.", parse_mode=constants.ParseMode.MARKDOWN_V2)
        return
    user_id = forwarded.id
    name = forwarded.mention_markdown_v2() if forwarded.first_name else f"`{user_id}`"
    await _check_and_reply(msg, user_id, name)

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text.startswith("/"):
        return
    if text.startswith("@"):
        await update.message.reply_text("⚠️ Username не работает — нужен числовой ID. Используй @userinfobot", parse_mode=constants.ParseMode.MARKDOWN_V2)
        return
    try:
        user_id = int(text)
    except ValueError:
        await update.message.reply_text("❌ Это не числовой ID. Пример: `123456789`", parse_mode=constants.ParseMode.MARKDOWN_V2)
        return
    if user_id <= 0 or user_id > 9999999999:
        await update.message.reply_text("❌ Некорректный ID", parse_mode=constants.ParseMode.MARKDOWN_V2)
        return
    name = f"`{user_id}`"
    await _check_and_reply(update.message, user_id, name)

async def _check_and_reply(message, user_id: int, display_name: str):
    async with message._unfrozen_bot.action_chat(message.chat_id, constants.ChatAction.TYPING):
        cached = await cache.get(str(user_id))
        if cached is not None:
            result = cached
            log.info(f"Cache hit for {user_id}: {result}")
        else:
            async with TelegaAPIClient() as api:
                result = await api.check_user(user_id)
                if result is not None:
                    await cache.set(str(user_id), result)
    
    if result is True:
        text = f"🤡 *{display_name}*\n\nИспользует *Telega* от ВКонтакте 🔴\n\n⚠️ Это небезопасно:\n• Данные могут передаваться третьим лицам\n• Возможна компрометация аккаунта"
    elif result is False:
        text = f"✅ *{display_name}*\n\nTelega *не обнаружен* 🟢\n\nПользователь использует официальный клиент."
    else:
        text = f"⚠️ *{display_name}*\n\nНе удалось проверить — сервис временно недоступен."
    
    await message.reply_text(text, parse_mode=constants.ParseMode.MARKDOWN_V2, disable_web_page_preview=True)

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    error = context.error
    log.error(f"Update {update} caused error: {error}", exc_info=error)
    if update and update.effective_message:
        if isinstance(error, RetryAfter):
            await update.effective_message.reply_text(f"⏳ Подождите {error.retry_after} сек.")
        elif isinstance(error, TelegramError):
            await update.effective_message.reply_text("❌ Ошибка. Попробуйте позже.")

async def post_init(app: Application):
    log.info("Bot initialized")
    await app.bot.set_my_commands([
        ("start", "🚀 Запустить"),
        ("help", "ℹ️ Инструкция"),
        ("cache", "🗄️ Кэш (админ)"),
    ])
    if Config.USE_WEBHOOK and Config.WEBHOOK_URL:
        webhook_path = f"/{Config.BOT_TOKEN}"
        await app.bot.set_webhook(url=f"{Config.WEBHOOK_URL}{webhook_path}", allowed_updates=Update.ALL_TYPES)
        log.info(f"Webhook set: {Config.WEBHOOK_URL}{webhook_path}")

def main():
    if not Config.validate():
        sys.exit(1)
    log.info(f"Starting Telega Detector Bot (PID={os.getpid()})")
    
    app = (
        ApplicationBuilder()
        .token(Config.BOT_TOKEN)
        .defaults(Defaults(parse_mode=constants.ParseMode.MARKDOWN_V2))
        .get_updates_connection_pool_size(10)
        .connection_pool_size(20)
        .post_init(post_init)
        .build()
    )
    
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("cache", cmd_cache))
    app.add_handler(MessageHandler(filters.FORWARDED, handle_forward))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_error_handler(error_handler)
    
    if Config.USE_WEBHOOK and Config.WEBHOOK_URL:
        log.info(f"Starting WEBHOOK mode on port {Config.PORT}")
        app.run_webhook(
            listen="0.0.0.0",
            port=Config.PORT,
            url_path=f"/{Config.BOT_TOKEN}",
            webhook_url=f"{Config.WEBHOOK_URL}/{Config.BOT_TOKEN}",
            drop_pending_updates=True,
        )
    else:
        log.info("Starting POLLING mode")
        app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()