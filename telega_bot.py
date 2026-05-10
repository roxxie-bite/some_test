
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

# === Фикс для Render Web Service (health check) ===
import os
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler

class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"OK")
    
    def do_HEAD(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        # Не отправляем тело для HEAD
    
    def log_message(self, format, *args):
        # Подавляем спам в логах от health checks
        if "HEAD" in format or "GET /" in format:
            return
        logger.info(f"Health check: {format % args}")

def _start_health_server():
    port = int(os.getenv("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    logger.info(f"🩺 Health server started on port {port}")
    server.serve_forever()  # Работает постоянно в фоне

# Запускаем сервер здоровья в отдельном потоке
health_thread = threading.Thread(target=_start_health_server, daemon=True)
health_thread.start()
# === Конец фикса ===

# Запуск бота (основной поток)
logger.info("🤖 Starting Telega Ban Bot...")
app.run_polling(drop_pending_updates=True)
# === КОНФИГУРАЦИЯ ===
# Токен бота (получите у @BotFather)
BOT_TOKEN = os.getenv("BOT_TOKEN")

# ID администраторов, которым доступны команды управления
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
            # Удаляем просроченную запись
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
        # Ограничиваем размер кэша (последние 1000 записей)
        if len(lookup_cache) > 1000:
            # Удаляем самые старые
            items = sorted(lookup_cache.items(), key=lambda x: x[1].get("checked_at", 0))
            for key, _ in items[:len(items) - 1000]:
                lookup_cache.pop(key, None)


def _post_json(url: str, data: dict) -> dict:
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
    if user_id <= 0:
        return None

    try:
        logger.info(f"🔍 Запрос session_key для user_id={user_id}")
        
        auth_payload = {
            "application_key": CALLS_API_KEY,
            "session_data": SESSION_DATA
        }
        
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "Telegram/8.0 (Android 13; Mobile)",
            "Accept": "application/json",
            "X-Requested-With": "XMLHttpRequest"
        }

        # 1. Получаем session_key
        resp = requests.post(
            f"{CALLS_BASE_URL}/api/auth/anonymLogin",
            json=auth_payload,
            headers=headers,
            timeout=10
        )
        
        logger.info(f"📥 Auth API [{resp.status_code}]: {resp.text[:400]}")
        resp.raise_for_status()
        auth_json = resp.json()
        
        session_key = _extract_session_key(auth_json)
        if not session_key:
            logger.warning(f"❌ session_key отсутствует. Ответ: {auth_json}")
            return None

        # 2. Проверяем ID
        payload = {
            "application_key": CALLS_API_KEY,
            "session_key": session_key,
            "externalIds": f'[{{"id":"{user_id}","ok_anonym":false}}]'
        }
        
        resp2 = requests.post(
            f"{CALLS_BASE_URL}/api/vchat/getOkIdsByExternalIds",
            json=payload,
            headers=headers,
            timeout=10
        )
        
        logger.info(f"📥 Lookup API [{resp2.status_code}]: {resp2.text[:400]}")
        resp2.raise_for_status()
        res_json = resp2.json()
        
        ids = res_json.get("ids") if isinstance(res_json, dict) else []
        result = _match_external_id(ids, str(user_id))
        return result

    except requests.exceptions.HTTPError as e:
        logger.error(f"🌐 HTTP ошибка API: {e.response.text if e.response else str(e)}")
    except Exception as e:
        logger.error(f"💥 Ошибка проверки пользователя {user_id}: {e}", exc_info=True)
        
    return None


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
        f"• /stats — показать статистику бота\n"
        f"• /help — подробная справка"
    )
    
    if chat and chat.type != "private":
        text += "\n\n💡 Добавьте меня в группу с правами администратора для автоматической защиты!"
    
    await update.message.reply_text(text, parse_mode="HTML")


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик /help"""
    text = (
        "📚 <b>Справка по командам</b>\n\n"
        "<b>🔍 Проверка пользователей:</b>\n"
        "• /check &lt;user_id&gt; — проверить по числовому ID\n"
        "• /check @username — проверить по юзернейму (в группе)\n"
        "• /check &lt;id&gt; --force — проверить, игнорируя кэш\n"
        "• Ответьте на сообщение и напишите /check — проверю автора\n"
        "• /checkme — проверить самого себя\n\n"
        "<b>🗄️ Управление кэшем (только админы):</b>\n"
        "• /cache list — показать последние записи кэша\n"
        "• /cache clear — очистить весь кэш\n"
        "• /cache delete &lt;id&gt; — удалить запись по ID\n\n"
        "<b>📊 Статистика:</b>\n"
        "• /stats — показать статистику работы бота\n"
        "• /reset_stats — сбросить статистику (только админы)\n\n"
        "<b>⚙️ Настройки (в коде):</b>\n"
        "• CHECK_ON_JOIN — проверять при входе в группу\n"
        "• AUTO_BAN — банить автоматически при обнаружении"
    )
    await update.message.reply_text(text, parse_mode="HTML")


async def check_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Проверка пользователя: /check <id|username> [--force]
    """
    user = update.effective_user
    chat = update.effective_chat
    
    # Проверка прав для небезопасных действий
    is_admin = user.id in ADMIN_USER_IDS
    if chat and not is_admin:
        try:
            chat_member = await chat.get_member(user.id)
            is_admin = chat_member.is_admin
        except Exception:
            pass
    
    # Парсинг аргументов
    args = context.args or []
    force_check = "--force" in args
    args = [a for a in args if a != "--force"]
    
    # Определение целевого пользователя
    target_user = None
    user_id = None
    username = None
    
    if not args:
        # Если команда в ответ на сообщение — берём автора
        if update.message and update.message.reply_to_message:
            target_user = update.message.reply_to_message.from_user
            user_id = target_user.id
            username = target_user.username
        else:
            await update.message.reply_text(
                "❌ <b>Использование:</b>\n"
                "• /check &lt;user_id&gt; — проверить по ID\n"
                "• /check @username — проверить по юзернейму (в группе)\n"
                "• /check &lt;id&gt; --force — проверить, игнорируя кэш\n"
                "• Ответьте на сообщение и напишите /check",
                parse_mode="HTML"
            )
            return
    else:
        target = args[0]
        
        if target.startswith("@"):
            # Поиск по юзернейму (только в группе)
            if not chat:
                await update.message.reply_text(
                    "❌ Поиск по @username работает только в группах"
                )
                return
            try:
                chat_member = await context.bot.get_chat_member(chat.id, target)
                target_user = chat_member.user
                user_id = target_user.id
                username = target_user.username
            except Exception as e:
                await update.message.reply_text(
                    f"❌ Не удалось найти пользователя {target}:\n<code>{e}</code>",
                    parse_mode="HTML"
                )
                return
        elif target.isdigit():
            user_id = int(target)
            username = None
        else:
            await update.message.reply_text(
                "❌ Укажите корректный ID (число) или @username"
            )
            return
    
    # Отправляем статус
    target_display = f"<code>{user_id}</code>"
    if username:
        target_display += f" (@{username})"
    if target_user and target_user.first_name:
        target_display = f"{target_user.mention_html()} {target_display}"
    
    status_msg = await update.message.reply_text(
        f"🔍 Проверяю пользователя {target_display}...",
        parse_mode="HTML"
    )
    
    # Выполняем проверку
    is_telega = _check_user_telega(user_id, force=force_check)
    
    # Формируем ответ
    if is_telega is None:
        await status_msg.edit_text(
            f"⚠️ Не удалось проверить пользователя {target_display}\n\n"
            f"Возможные причины:\n"
            f"• API временно недоступен\n"
            f"• Пользователь не найден в базе проверок\n"
            f"• Превышен лимит запросов — попробуйте позже",
            parse_mode="HTML"
        )
        
    elif is_telega:
        response_text = (
            f"🚨 <b>Обнаружено!</b>\n"
            f"Пользователь {target_display} использует клиент <b>Telega</b>.\n\n"
            f"<b>Почему это опасно:</b>\n"
            f"• Ваши данные могут передаваться третьим лицам\n"
            f"• Аккаунт может быть скомпрометирован\n"
            f"• Возможна кража сессии и переписка от вашего имени\n\n"
            f"<b>Рекомендации:</b>\n"
            f"• Удалить приложение Telega\n"
            f"• Завершить сессию в Настройки → Устройства → Завершить сеанс Telega\n"
            f"• Установить официальный клиент: telegram.org"
        )
        
        # Кнопка бана для админов
        keyboard = None
        if chat and is_admin:
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("🔨 Забанить", callback_data=f"ban_{user_id}")]
            ])
            response_text += "\n\n👆 Нажмите кнопку, чтобы заблокировать пользователя"
        
        await status_msg.edit_text(
            response_text,
            parse_mode="HTML",
            reply_markup=keyboard
        )
        
    else:
        await status_msg.edit_text(
            f"✅ Пользователь {target_display} <b>чист</b> — не использует Telega",
            parse_mode="HTML"
        )


async def checkme_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Проверить самого себя: /checkme"""
    user = update.effective_user
    await update.message.reply_text(f"🔍 Проверяю вас (ID: {user.id})...")
    
    is_telega = _check_user_telega(user.id)
    
    if is_telega is None:
        await update.message.reply_text(
            "⚠️ Не удалось выполнить проверку. Попробуйте позже."
        )
    elif is_telega:
        await update.message.reply_text(
            "🚨 <b>Внимание!</b>\n"
            "Вы используете клиент <b>Telega</b>.\n\n"
            "Это может быть небезопасно:\n"
            "• Ваши данные могут передаваться третьим лицам\n"
            "• Аккаунт может быть скомпрометирован\n"
            "• Возможна кража сессии и переписка от вашего имени\n\n"
            "Рекомендуем перейти на официальный клиент Telegram:\n"
            "👉 telegram.org",
            parse_mode="HTML"
        )
    else:
        await update.message.reply_text(
            "✅ Вы используете безопасный клиент. Всё в порядке! 🎉"
        )


async def cache_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Управление кэшем:
    /cache list — показать записи
    /cache clear — очистить кэш
    /cache delete <id> — удалить запись по ID
    """
    user = update.effective_user
    if user.id not in ADMIN_USER_IDS:
        await update.message.reply_text("❌ Эта команда доступна только администраторам")
        return
    
    args = context.args or []
    action = args[0] if args else "list"
    
    if action == "clear":
        with cache_lock:
            count = len(lookup_cache)
            lookup_cache.clear()
        await update.message.reply_text(f"🗑️ Кэш очищен. Удалено записей: {count}")
        logger.info(f"Cache cleared by admin {user.id}")
        
    elif action == "delete" and len(args) > 1 and args[1].isdigit():
        uid = args[1]
        with cache_lock:
            if uid in lookup_cache:
                del lookup_cache[uid]
                await update.message.reply_text(f"🗑️ Запись для пользователя {uid} удалена")
            else:
                await update.message.reply_text(f"ℹ️ Запись для пользователя {uid} не найдена в кэше")
                
    elif action == "list":
        with cache_lock:
            total = len(lookup_cache)
            if total == 0:
                await update.message.reply_text("📭 Кэш пуст")
                return
            
            # Показываем последние 15 записей
            items = list(lookup_cache.items())[-15:]
            lines = [f"📋 Кэш ({total} записей, последние 15):\n"]
            for uid, data in reversed(items):
                status = "🚨 Telega" if data.get("value") else "✅ Clean"
                checked = data.get("checked_at", 0)
                ago = _now_ts() - checked
                time_str = f"{ago//60}м" if ago < 3600 else f"{ago//3600}ч"
                lines.append(f"• <code>{uid}</code>: {status} ({time_str} назад)")
        
        await update.message.reply_text("\n".join(lines), parse_mode="HTML")
        
    else:
        await update.message.reply_text(
            "❌ <b>Использование:</b>\n"
            "• /cache list — показать записи\n"
            "• /cache clear — очистить кэш\n"
            "• /cache delete &lt;id&gt; — удалить запись",
            parse_mode="HTML"
        )


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показать статистику: /stats"""
    with stats_lock:
        text = (
            f"📊 <b>Статистика Telega Ban Bot</b>\n\n"
            f"• Проверено пользователей: {stats.get('checked', 0)}\n"
            f"• Обнаружено Telega: {stats.get('telega_found', 0)}\n"
            f"• Забанено: {stats.get('banned', 0)}\n"
            f"• Ошибок: {stats.get('errors', 0)}\n\n"
            f"• Записей в кэше: {len(lookup_cache)}\n"
            f"• TTL кэша: {LOOKUP_CACHE_TTL // 3600} ч."
        )
    
    # Если админ — показываем дополнительные данные
    if update.effective_user.id in ADMIN_USER_IDS:
        text += f"\n\n<b>Конфигурация:</b>\n"
        text += f"• AUTO_BAN: {'✅ Да' if AUTO_BAN else '❌ Нет'}\n"
        text += f"• CHECK_ON_JOIN: {'✅ Да' if CHECK_ON_JOIN else '❌ Нет'}"
    
    await update.message.reply_text(text, parse_mode="HTML")


async def reset_stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Сбросить статистику (только админы)"""
    if update.effective_user.id not in ADMIN_USER_IDS:
        return
    
    with stats_lock:
        for key in stats:
            stats[key] = 0
    
    await update.message.reply_text("🔄 Статистика сброшена")
    logger.info(f"Stats reset by admin {update.effective_user.id}")


# === ОБРАБОТЧИК CALLBACK (кнопки) ===

async def ban_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик нажатия на кнопку бана"""
    query = update.callback_query
    await query.answer()  # Обязательно для callback
    
    if not query.data or not query.data.startswith("ban_"):
        return
    
    try:
        user_id = int(query.data.split("_")[1])
        chat = query.message.chat
        
        if not chat:
            await query.edit_message_text("❌ Не удалось определить чат")
            return
        
        # Проверка прав
        admin_id = query.from_user.id
        is_admin = admin_id in ADMIN_USER_IDS
        if not is_admin:
            try:
                chat_member = await chat.get_member(admin_id)
                is_admin = chat_member.is_admin
            except Exception:
                pass
        
        if not is_admin:
            await query.edit_message_text("❌ Только администраторы могут банить")
            return
        
        # Бан пользователя
        await chat.ban_member(user_id)
        
        # Уведомление (если бот может отправлять)
        try:
            await query.edit_message_text(
                f"✅ Пользователь <code>{user_id}</code> заблокирован",
                parse_mode="HTML"
            )
        except Exception:
            pass  # Сообщение могло быть удалено
        
        _update_stats("banned")
        logger.info(f"User {user_id} banned by {admin_id} in chat {chat.id}")
        
    except Exception as e:
        logger.error(f"Ban error: {e}")
        try:
            await query.edit_message_text(f"❌ Ошибка при блокировке: <code>{e}</code>", parse_mode="HTML")
        except Exception:
            pass


# === ОБРАБОТЧИК НОВЫХ УЧАСТНИКОВ ===

async def on_new_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик входа новых участников в группу"""
    if not CHECK_ON_JOIN:
        return
    
    chat = update.effective_chat
    if not chat or chat.type == "private":
        return
    
    # Проверка прав бота
    try:
        bot_member = await chat.get_member(context.bot.id)
        if not isinstance(bot_member, ChatMemberAdministrator) or not bot_member.can_restrict_members:
            logger.warning(f"Bot lacks ban permissions in chat {chat.id}")
            return
    except Exception as e:
        logger.error(f"Can't check bot permissions: {e}")
        return
    
    # Получаем список новых участников
    new_members = update.effective_message.new_chat_members if update.effective_message else []
    if not new_members:
        return
    
    for user in new_members:
        if user.is_bot:
            continue  # Пропускаем ботов
        
        user_id = user.id
        username = f"@{user.username}" if user.username else ""
        logger.info(f"New member: {user_id} {username} in chat {chat.id}")
        
        # Проверка на Telega
        is_telega = _check_user_telega(user_id)
        
        if is_telega is None:
            # Не удалось проверить — логируем и пропускаем
            logger.warning(f"Check failed for new member {user_id}")
            continue
        
        if is_telega:
            # Формируем предупреждение
            mention = user.mention_html(user.first_name) if user.first_name else f"<code>{user_id}</code>"
            warning = (
                f"🚨 <b>Обнаружен пользователь Telega!</b>\n"
                f"{mention} {username} использует небезопасный клиент.\n\n"
                f"<b>Рекомендации:</b>\n"
                f"• Удалить Telega\n"
                f"• Завершить сессию в настройках аккаунта"
            )
            
            if AUTO_BAN:
                # Автоматический бан
                try:
                    await chat.ban_member(user_id)
                    await context.bot.send_message(
                        chat_id=chat.id,
                        text=f"{warning}\n\n❌ Пользователь заблокирован.",
                        parse_mode="HTML"
                    )
                    _update_stats("banned")
                    logger.info(f"Auto-banned Telega user {user_id} in chat {chat.id}")
                except Exception as e:
                    logger.error(f"Failed to ban user {user_id}: {e}")
                    await context.bot.send_message(
                        chat_id=chat.id,
                        text=f"{warning}\n\n❌ Ошибка при блокировке: <code>{e}</code>",
                        parse_mode="HTML"
                    )
            elif BAN_AFTER_WARNING:
                # Предупреждение с кнопкой бана
                keyboard = InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔨 Забанить", callback_data=f"ban_{user_id}")]
                ])
                await context.bot.send_message(
                    chat_id=chat.id,
                    text=f"{warning}\n\n👆 Админ может нажать для блокировки",
                    parse_mode="HTML",
                    reply_markup=keyboard
                )
            else:
                # Только предупреждение
                await context.bot.send_message(
                    chat_id=chat.id,
                    text=warning,
                    parse_mode="HTML"
                )
        else:
            logger.info(f"New member {user_id} is clean")


# === ОБРАБОТЧИК ОШИБОК ===

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Глобальный обработчик ошибок"""
    error = context.error
    logger.error(f"Update {update} caused error: {error}", exc_info=error)
    
    # Уведомляем админов о критических ошибках
    if error and ("Unauthorized" in str(error) or "Forbidden" in str(error)):
        for admin_id in ADMIN_USER_IDS:
            try:
                await context.bot.send_message(
                    chat_id=admin_id,
                    text=f"❌ <b>Критическая ошибка бота:</b>\n<code>{error}</code>",
                    parse_mode="HTML"
                )
            except Exception:
                pass


# === ЗАПУСК БОТА ===

def main():
    """Запуск бота"""
    # Проверка токена
    if BOT_TOKEN == "YOUR_BOT_TOKEN_HERE":
        logger.error("❌ Укажите BOT_TOKEN в конфигурации или переменной окружения!")
        print("ERROR: Please set BOT_TOKEN environment variable or edit the script")
        return
    
    logger.info("🤖 Starting Telega Ban Bot...")
    
    # Создаём приложение
    app = Application.builder().token(BOT_TOKEN).build()
    
    # === Регистрация обработчиков ===
    
    # Команды
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("check", check_command))
    app.add_handler(CommandHandler("checkme", checkme_command))
    app.add_handler(CommandHandler("cache", cache_command))
    app.add_handler(CommandHandler("stats", stats_command))
    app.add_handler(CommandHandler("reset_stats", reset_stats_command))
    
    # Callback для кнопок
    app.add_handler(CallbackQueryHandler(ban_callback, pattern="^ban_"))
    
    # Новые участники (только если включено)
    if CHECK_ON_JOIN:
        app.add_handler(
            MessageHandler(
                filters.StatusUpdate.NEW_CHAT_MEMBERS & ~filters.COMMAND,
                on_new_member
            )
        )
    
    # Обработчик ошибок
    app.add_error_handler(error_handler)
    
    # Запуск
    logger.info("✅ Bot handlers registered. Starting polling...")
    print(f"🤖 Telega Ban Bot запущен! (лог: telega_bot.log)")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()