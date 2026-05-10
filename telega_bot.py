#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Telega Ban Bot — автоматически проверяет новых участников группы
и банит пользователей, использующих клиент Telega.

Запуск на Render: Web Service с Webhook (рекомендуется)
"""

# === ИМПОРТЫ ===
import json
import time
import hashlib
import logging
import requests
import threading
import os
from datetime import datetime, timedelta
from typing import Optional, Dict, List

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, ChatMemberAdministrator
)
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# === КОНФИГУРАЦИЯ ===
# Токен бота (обязательно укажите в переменных окружения Render: BOT_TOKEN)
BOT_TOKEN = os.getenv("BOT_TOKEN")

# ID администраторов (через запятую в ADMIN_IDS или по умолчанию ваш ID)
ADMIN_USER_IDS = [int(x) for x in os.getenv("ADMIN_IDS", "2135550613").split(",") if x.isdigit()]

# Настройки поведения
CHECK_ON_JOIN = True          # Проверять пользователей при входе в группу
AUTO_BAN = True               # Банить автоматически или только предупреждать
BAN_AFTER_WARNING = False     # Банить после предупреждения (если AUTO_BAN=False)

# Настройки API (из оригинального плагина)
CALLS_BASE_URL = "https://calls.okcdn.ru"
CALLS_API_KEY = "CHKIPMKGDIHBABABA"
SESSION_DATA = '{"device_id":"telega_bot","version":2,"client_version":"bot_1","client_type":"SDK_BOT"}'

# Кэширование
LOOKUP_CACHE_TTL = 6 * 60 * 60  # 6 часов
lookup_cache: Dict[str, Dict] = {}
cache_lock = threading.Lock()

# Статистика
stats = {
    "checked": 0,
    "telega_found": 0,
    "banned": 0,
    "errors": 0
}
stats_lock = threading.Lock()

# Логирование
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
    handlers=[
        logging.FileHandler("telega_bot.log", encoding="utf-8"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)


# === УТИЛИТЫ ===

def _now_ts() -> int:
    """Текущее время в секундах"""
    return int(time.time())


def _cache_get(user_id: int) -> Optional[bool]:
    """Получить результат проверки из кэша"""
    with cache_lock:
        key = str(user_id)
        payload = lookup_cache.get(key)
        if not isinstance(payload, dict):
            return None
        ts = payload.get("checked_at", 0)
        if _now_ts() - ts > LOOKUP_CACHE_TTL:
            lookup_cache.pop(key, None)
            return None
        return payload.get("value")


def _cache_set(user_id: int, value: bool):
    """Сохранить результат проверки в кэш"""
    with cache_lock:
        lookup_cache[str(user_id)] = {
            "value": bool(value),
            "checked_at": _now_ts()
        }
        if len(lookup_cache) > 1000:
            items = sorted(lookup_cache.items(), key=lambda x: x[1].get("checked_at", 0))
            for key, _ in items[:len(items) - 1000]:
                lookup_cache.pop(key, None)


def _post_json(url: str,  dict) -> dict:
    """Отправить POST-запрос с JSON"""
    try:
        resp = requests.post(
            url,
            json=data,
            headers={"Accept": "application/json", "User-Agent": "TelegaBanBot/1.0"},
            timeout=12
        )
        resp.raise_for_status()
        return resp.json() if resp.content else {}
    except requests.exceptions.Timeout:
        logger.error(f"Timeout requesting {url}")
        return {}
    except requests.exceptions.RequestException as e:
        logger.error(f"API request failed: {e}")
        return {}


def _extract_session_key(auth_json: dict) -> str:
    """Извлечь session_key из ответа авторизации"""
    try:
        return str(auth_json.get("session_key") or "").strip()
    except Exception:
        return ""


def _match_external_id(ids: list, target: str) -> bool:
    """Проверить, есть ли target в списке external_id"""
    try:
        for item in ids or []:
            if not isinstance(item, dict):
                continue
            external = item.get("external_user_id", {})
            if isinstance(external, dict) and str(external.get("id") or "") == target:
                return True
    except Exception as e:
        logger.error(f"Error matching external_id: {e}")
    return False


def _lookup_is_telega_user(user_id: int) -> Optional[bool]:
    """Проверить пользователя через OK.ru API (form-urlencoded)"""
    if user_id <= 0:
        return None

    try:
        logger.info(f"🔍 Запрос session_key для user_id={user_id}")
        
        # === ШАГ 1: Получаем session_key ===
        auth_data = {
            "application_key": CALLS_API_KEY,
            "session_data": SESSION_DATA
        }
        
        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "Telegram/8.0 (Android 13; Mobile)",
            "Accept": "application/json",
        }

        resp = requests.post(
            f"{CALLS_BASE_URL}/api/auth/anonymLogin",
            data=auth_data,
            headers=headers,
            timeout=10
        )
        
        logger.info(f"📥 Auth API [{resp.status_code}]: {resp.text[:400]}")
        
        if resp.status_code != 200:
            logger.error(f"Auth failed: {resp.status_code} - {resp.text}")
            return None
            
        auth_json = resp.json()
        session_key = _extract_session_key(auth_json)
        
        if not session_key:
            logger.warning(f"❌ session_key отсутствует. Ответ: {auth_json}")
            return None

        # === ШАГ 2: Проверяем ID ===
        lookup_data = {
            "application_key": CALLS_API_KEY,
            "session_key": session_key,
            "externalIds": f'[{{"id":"{user_id}","ok_anonym":false}}]'
        }
        
        resp2 = requests.post(
            f"{CALLS_BASE_URL}/api/vchat/getOkIdsByExternalIds",
            data=lookup_data,
            headers=headers,
            timeout=10
        )
        
        logger.info(f"📥 Lookup API [{resp2.status_code}]: {resp2.text[:400]}")
        
        if resp2.status_code != 200:
            logger.error(f"Lookup failed: {resp2.status_code} - {resp2.text}")
            return None
            
        res_json = resp2.json()
        ids = res_json.get("ids") if isinstance(res_json, dict) else []
        result = _match_external_id(ids, str(user_id))
        return result

    except requests.exceptions.RequestException as e:
        logger.error(f"🌐 Network error: {e}")
    except Exception as e:
        logger.error(f"💥 Ошибка проверки пользователя {user_id}: {e}", exc_info=True)
        
    return None


def _check_user_telega(user_id: int, force: bool = False) -> Optional[bool]:
    """Проверить пользователя с учётом кэша"""
    if not force:
        cached = _cache_get(user_id)
        if cached is not None:
            logger.info(f"Cache hit for user {user_id}: {cached}")
            with stats_lock:
                stats["checked"] += 1
            return cached

    result = _lookup_is_telega_user(user_id)
    
    if result is not None:
        _cache_set(user_id, result)
        with stats_lock:
            stats["checked"] += 1
            if result:
                stats["telega_found"] += 1
        logger.info(f"Checked user {user_id}: {'Telega' if result else 'Clean'}")
    else:
        with stats_lock:
            stats["errors"] += 1
        logger.warning(f"Check failed for user {user_id}")
    
    return result


def _update_stats(key: str, increment: int = 1):
    """Обновить статистику"""
    with stats_lock:
        stats[key] = stats.get(key, 0) + increment


# === ОБРАБОТЧИКИ КОМАНД ===

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик /start"""
    user = update.effective_user
    chat = update.effective_chat
    
    text = (
        f"👋 Привет, {user.first_name}!\n\n"
        f"Я <b>Telega Ban Bot</b> — защищаю группы от пользователей клиента Telega.\n\n"
        f"<b>Как это работает:</b>\n"
        f"• Проверяю новых участников при входе в группу\n"
        f"• Если обнаруживаю Telega — предупреждаю или баню (настраивается)\n"
        f"• Кэширую результаты на 6 часов для скорости\n\n"
        f"<b>Команды:</b>\n"
        f"• /check &lt;id|@username&gt; — проверить пользователя\n"
        f"• /checkme — проверить себя\n"
        f"• /cache — управление кэшем\n"
        f"• /stats — показать статистику бота"
    )
    
    if chat and chat.type != "private":
        text += "\n\n💡 Добавьте меня в группу с правами администратора!"
    
    await update.message.reply_text(text, parse_mode="HTML")


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик /help"""
    text = (
        "📚 <b>Справка по командам</b>\n\n"
        "<b>🔍 Проверка:</b>\n"
        "• /check &lt;user_id&gt; — проверить по ID\n"
        "• /check @username — проверить по юзернейму (в группе)\n"
        "• /check &lt;id&gt; --force — проверить, игнорируя кэш\n"
        "• Ответьте на сообщение + /check — проверю автора\n"
        "• /checkme — проверить себя\n\n"
        "<b>🗄️ Кэш (админы):</b>\n"
        "• /cache list — показать записи\n"
        "• /cache clear — очистить кэш\n"
        "• /cache delete &lt;id&gt; — удалить запись"
    )
    await update.message.reply_text(text, parse_mode="HTML")


async def check_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Проверка пользователя: /check <id|username> [--force]"""
    user = update.effective_user
    chat = update.effective_chat
    
    is_admin = user.id in ADMIN_USER_IDS
    if chat and not is_admin:
        try:
            chat_member = await chat.get_member(user.id)
            is_admin = chat_member.is_admin
        except Exception:
            pass
    
    args = context.args or []
    force_check = "--force" in args
    args = [a for a in args if a != "--force"]
    
    target_user = None
    user_id = None
    username = None
    
    if not args:
        if update.message and update.message.reply_to_message:
            target_user = update.message.reply_to_message.from_user
            user_id = target_user.id
            username = target_user.username
        else:
            await update.message.reply_text(
                "❌ <b>Использование:</b>\n"
                "• /check &lt;user_id&gt;\n"
                "• /check @username (в группе)\n"
                "• /check &lt;id&gt; --force",
                parse_mode="HTML"
            )
            return
    else:
        target = args[0]
        
        if target.startswith("@"):
            if not chat:
                await update.message.reply_text("❌ @username работает только в группах")
                return
            try:
                chat_member = await context.bot.get_chat_member(chat.id, target)
                target_user = chat_member.user
                user_id = target_user.id
                username = target_user.username
            except Exception as e:
                await update.message.reply_text(f"❌ Не найден {target}:\n<code>{e}</code>", parse_mode="HTML")
                return
        elif target.isdigit():
            user_id = int(target)
            username = None
        else:
            await update.message.reply_text("❌ Укажите ID (число) или @username")
            return
    
    target_display = f"<code>{user_id}</code>"
    if username:
        target_display += f" (@{username})"
    if target_user and target_user.first_name:
        target_display = f"{target_user.mention_html()} {target_display}"
    
    status_msg = await update.message.reply_text(f"🔍 Проверяю {target_display}...", parse_mode="HTML")
    
    is_telega = _check_user_telega(user_id, force=force_check)
    
    if is_telega is None:
        await status_msg.edit_text(
            f"⚠️ Не удалось проверить {target_display}\n\n"
            f"Причины:\n• API недоступен\n• Пользователь не в базе",
            parse_mode="HTML"
        )
    elif is_telega:
        response_text = (
            f"🚨 <b>Обнаружено!</b>\n"
            f"{target_display} использует <b>Telega</b>.\n\n"
            f"<b>Рекомендации:</b>\n"
            f"• Удалить Telega\n"
            f"• Завершить сессию в настройках"
        )
        
        keyboard = None
        if chat and is_admin:
            keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🔨 Забанить", callback_data=f"ban_{user_id}")]])
            response_text += "\n\n👆 Нажмите для блокировки"
        
        await status_msg.edit_text(response_text, parse_mode="HTML", reply_markup=keyboard)
    else:
        await status_msg.edit_text(f"✅ {target_display} <b>чист</b>", parse_mode="HTML")


async def checkme_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Проверить самого себя: /checkme"""
    user = update.effective_user
    await update.message.reply_text(f"🔍 Проверяю вас (ID: {user.id})...")
    
    is_telega = _check_user_telega(user.id)
    
    if is_telega is None:
        await update.message.reply_text("⚠️ Не удалось проверить. Попробуйте позже.")
    elif is_telega:
        await update.message.reply_text(
            "🚨 <b>Внимание!</b>\nВы используете <b>Telega</b>.\n\n"
            "Рекомендуем официальный клиент: telegram.org",
            parse_mode="HTML"
        )
    else:
        await update.message.reply_text("✅ Вы используете безопасный клиент! 🎉")


async def cache_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Управление кэшем (только админы)"""
    user = update.effective_user
    if user.id not in ADMIN_USER_IDS:
        await update.message.reply_text("❌ Только для администраторов")
        return
    
    args = context.args or []
    action = args[0] if args else "list"
    
    if action == "clear":
        with cache_lock:
            count = len(lookup_cache)
            lookup_cache.clear()
        await update.message.reply_text(f"🗑️ Кэш очищен. Удалено: {count}")
    elif action == "delete" and len(args) > 1 and args[1].isdigit():
        uid = args[1]
        with cache_lock:
            if uid in lookup_cache:
                del lookup_cache[uid]
                await update.message.reply_text(f"🗑️ Запись {uid} удалена")
            else:
                await update.message.reply_text(f"ℹ️ Запись {uid} не найдена")
    elif action == "list":
        with cache_lock:
            total = len(lookup_cache)
            if total == 0:
                await update.message.reply_text("📭 Кэш пуст")
                return
            items = list(lookup_cache.items())[-15:]
            lines = [f"📋 Кэш ({total} записей):\n"]
            for uid, data in reversed(items):
                status = "🚨 Telega" if data.get("value") else "✅ Clean"
                ago = _now_ts() - data.get("checked_at", 0)
                time_str = f"{ago//60}м" if ago < 3600 else f"{ago//3600}ч"
                lines.append(f"• <code>{uid}</code>: {status} ({time_str} назад)")
        await update.message.reply_text("\n".join(lines), parse_mode="HTML")
    else:
        await update.message.reply_text(
            "❌ <b>Использование:</b>\n• /cache list\n• /cache clear\n• /cache delete &lt;id&gt;",
            parse_mode="HTML"
        )


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показать статистику"""
    with stats_lock:
        text = (
            f"📊 <b>Статистика</b>\n"
            f"• Проверено: {stats.get('checked', 0)}\n"
            f"• Telega: {stats.get('telega_found', 0)}\n"
            f"• Забанено: {stats.get('banned', 0)}\n"
            f"• Ошибок: {stats.get('errors', 0)}\n"
            f"• В кэше: {len(lookup_cache)}"
        )
    await update.message.reply_text(text, parse_mode="HTML")


async def reset_stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Сбросить статистику (только админы)"""
    if update.effective_user.id not in ADMIN_USER_IDS:
        return
    with stats_lock:
        for key in stats:
            stats[key] = 0
    await update.message.reply_text("🔄 Статистика сброшена")


# === CALLBACK HANDLERS ===

async def ban_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик кнопки бана"""
    query = update.callback_query
    await query.answer()
    
    if not query.data or not query.data.startswith("ban_"):
        return
    
    try:
        user_id = int(query.data.split("_")[1])
        chat = query.message.chat
        
        if not chat:
            await query.edit_message_text("❌ Не удалось определить чат")
            return
        
        admin_id = query.from_user.id
        is_admin = admin_id in ADMIN_USER_IDS
        if not is_admin:
            try:
                chat_member = await chat.get_member(admin_id)
                is_admin = chat_member.is_admin
            except Exception:
                pass
        
        if not is_admin:
            await query.edit_message_text("❌ Только администраторы")
            return
        
        await chat.ban_member(user_id)
        await query.edit_message_text(f"✅ <code>{user_id}</code> заблокирован", parse_mode="HTML")
        _update_stats("banned")
        logger.info(f"User {user_id} banned by {admin_id}")
        
    except Exception as e:
        logger.error(f"Ban error: {e}")
        await query.edit_message_text(f"❌ Ошибка: <code>{e}</code>", parse_mode="HTML")


# === НОВЫЕ УЧАСТНИКИ ===

async def on_new_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик входа новых участников"""
    if not CHECK_ON_JOIN:
        return
    
    chat = update.effective_chat
    if not chat or chat.type == "private":
        return
    
    try:
        bot_member = await chat.get_member(context.bot.id)
        if not bot_member.can_restrict_members:
            return
    except Exception:
        return
    
    new_members = update.effective_message.new_chat_members if update.effective_message else []
    if not new_members:
        return
    
    for user in new_members:
        if user.is_bot:
            continue
        
        user_id = user.id
        logger.info(f"New member: {user_id} in chat {chat.id}")
        
        is_telega = _check_user_telega(user_id)
        
        if is_telega is None:
            continue
        
        if is_telega:
            mention = user.mention_html(user.first_name) if user.first_name else f"<code>{user_id}</code>"
            warning = f"🚨 <b>Telega обнаружен!</b>\n{mention} использует небезопасный клиент."
            
            if AUTO_BAN:
                try:
                    await chat.ban_member(user_id)
                    await context.bot.send_message(chat_id=chat.id, text=f"{warning}\n\n❌ Заблокирован.", parse_mode="HTML")
                    _update_stats("banned")
                except Exception as e:
                    logger.error(f"Ban failed: {e}")
            else:
                await context.bot.send_message(chat_id=chat.id, text=warning, parse_mode="HTML")


# === ОБРАБОТЧИК ОШИБОК ===

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Глобальный обработчик ошибок"""
    error = context.error
    logger.error(f"Update {update} caused error: {error}", exc_info=error)
    
    if error and ("Unauthorized" in str(error) or "Forbidden" in str(error)):
        for admin_id in ADMIN_USER_IDS:
            try:
                await context.bot.send_message(chat_id=admin_id, text=f"❌ Ошибка: <code>{error}</code>", parse_mode="HTML")
            except Exception:
                pass


# === MAIN ===

def main():
    """Точка входа"""
    if not BOT_TOKEN or BOT_TOKEN == "YOUR_BOT_TOKEN_HERE":
        logger.error("❌ Укажите BOT_TOKEN в переменной окружения!")
        return
    
    logger.info("🤖 Starting Telega Ban Bot (Webhook mode)...")
    
    # Создаём приложение
    app = Application.builder().token(BOT_TOKEN).build()
    
    # Регистрируем обработчики
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("check", check_command))
    app.add_handler(CommandHandler("checkme", checkme_command))
    app.add_handler(CommandHandler("cache", cache_command))
    app.add_handler(CommandHandler("stats", stats_command))
    app.add_handler(CommandHandler("reset_stats", reset_stats_command))
    app.add_handler(CallbackQueryHandler(ban_callback, pattern="^ban_"))
    
    if CHECK_ON_JOIN:
        app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS & ~filters.COMMAND, on_new_member))
    
    app.add_error_handler(error_handler)
    
    # === WEBHOOK ДЛЯ RENDER ===
    webhook_url = os.getenv("RENDER_EXTERNAL_URL", "").rstrip("/")
    port = int(os.getenv("PORT", 10000))
    
    if webhook_url:
        # Уникальный путь для безопасности
        webhook_path = f"/{BOT_TOKEN}"
        full_webhook_url = f"{webhook_url}{webhook_path}"
        
        logger.info(f"🔗 Webhook URL: {full_webhook_url}")
        
        # Запускаем вебхук
        app.run_webhook(
            listen="0.0.0.0",
            port=port,
            url_path=webhook_path,
            webhook_url=full_webhook_url,
            drop_pending_updates=True,
            allowed_updates=Update.ALL_TYPES
        )
    else:
        logger.error("❌ RENDER_EXTERNAL_URL не установлен. Бот не будет получать обновления.")
        logger.error("💡 Добавьте в Render переменную: RENDER_EXTERNAL_URL=https://ваш-проект.onrender.com")
        # Fallback: polling (только для локального тестирования)
        logger.warning("⚠️ Запускаю polling для тестирования (не для продакшена!)")
        app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()