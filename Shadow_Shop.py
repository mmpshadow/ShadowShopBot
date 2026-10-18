import os
import sqlite3
import asyncio
import html
import re
import time
import json
import urllib.request
import urllib.error
import urllib.parse
import socket
import ssl
import http.client
import inspect
import tempfile
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from telegram import (
    Update,
    InlineKeyboardButton as TelegramInlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    KeyboardButton,
)
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from telegram.error import RetryAfter, Forbidden, TelegramError
from telegram.request import HTTPXRequest

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "bot.db")
RECEIPTS_DIR = os.path.join(BASE_DIR, "receipts")

# =========================
# تنظیمات
# =========================
# نسخه داخلی: 1.46 — renewal admin approval, Telegram Stars (XTR), glass UI, bug fixes

BOT_TOKEN = "XXXXXXXXXXXXX"
# SOCKS5 proxy used for Telegram Bot API connections. Override via TELEGRAM_PROXY env var if needed.
TELEGRAM_PROXY = os.getenv("TELEGRAM_PROXY", "").strip()
ADMIN_ID = XX
BROADCAST_DELAY = 0.08
PAGE_SIZE = 10
IRAN_TZ = timezone(timedelta(hours=3, minutes=30))
UTC_TZ = timezone.utc

# تنظیمات UI/Runtime که از دیتابیس بارگذاری می‌شوند.
BUTTON_STYLE_MODE = "colorful"  # colorful | glass


def _button_style_for(text, callback_data=None, url=None, **kwargs):
    """انتخاب خودکار استایل دکمه‌های Telegram Bot API 10.x."""
    if BUTTON_STYLE_MODE != "colorful":
        return None
    value = f"{text} {callback_data or ''}".lower()
    danger_words = (
        "حذف", "لغو", "رد", "حذف کردن", "delete", "reject", "remove",
        "cancel", "danger", "ban", "unban", "پاک"
    )
    success_words = (
        "تأیید", "تایید", "ذخیره", "خرید", "شارژ", "تمدید", "انتخاب",
        "عضویت", "بررسی", "approve", "confirm", "save", "buy", "renew",
        "select", "check", "success"
    )
    if any(word in value for word in danger_words):
        return "danger"
    if any(word in value for word in success_words):
        return "success"
    if url:
        return "primary"
    return "primary"


# بعضی نسخه‌های python-telegram-bot پارامتر style را ندارند.
# با introspection فقط در نسخه‌هایی که واقعاً پشتیبانی می‌شود آن را ارسال می‌کنیم.
_TELEGRAM_BUTTON_SUPPORTS_STYLE = (
    "style" in inspect.signature(TelegramInlineKeyboardButton).parameters
)


def InlineKeyboardButton(text, *args, **kwargs):
    """سازنده مرکزی دکمه‌ها با سازگاری با نسخه‌های مختلف PTB.

    اگر نسخه نصب‌شده style را پشتیبانی نکند، آن پارامتر حذف می‌شود تا بات
    بدون خطای TypeError اجرا شود. حالت glass از ظاهر پیش‌فرض کلاینت Telegram
    استفاده می‌کند؛ رنگ دلخواه/شفافیت سفارشی در نسخه‌های قدیمی قابل اعمال نیست.
    """
    if _TELEGRAM_BUTTON_SUPPORTS_STYLE and "style" not in kwargs:
        style = _button_style_for(
            text, kwargs.get("callback_data"), kwargs.get("url")
        )
        if style:
            kwargs["style"] = style
    elif not _TELEGRAM_BUTTON_SUPPORTS_STYLE:
        # اگر جایی از کد style را مستقیم هم ارسال کرده باشد، حذفش می‌کنیم.
        kwargs.pop("style", None)
    return TelegramInlineKeyboardButton(text, *args, **kwargs)




def get_bot_setting(key, default=None):
    try:
        conn = db_connect()
        row = conn.execute("SELECT value FROM bot_settings WHERE key=?", (str(key),)).fetchone()
        conn.close()
        return row[0] if row else default
    except Exception:
        return default


def set_bot_setting(key, value):
    conn = db_connect()
    try:
        conn.execute(
            "INSERT INTO bot_settings(key,value,updated_at) VALUES(?,?,CURRENT_TIMESTAMP) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=CURRENT_TIMESTAMP",
            (str(key), str(value))
        )
        conn.commit()
    finally:
        conn.close()


def load_runtime_settings():
    """بارگذاری تنظیمات قابل تغییر از DB؛ مقادیر پیش‌فرض فقط بار اول استفاده می‌شوند."""
    global BOT_TOKEN, ADMIN_ID, BROADCAST_DELAY, PAGE_SIZE, STAR_TO_TOMAN, CHANNELS, BUTTON_STYLE_MODE
    try:
        bot_token = get_bot_setting("bot_token", BOT_TOKEN)
        admin_id = get_bot_setting("admin_id", str(ADMIN_ID))
        broadcast_delay = get_bot_setting("broadcast_delay", str(BROADCAST_DELAY))
        page_size = get_bot_setting("page_size", str(PAGE_SIZE))
        button_style = get_bot_setting("button_style_mode", BUTTON_STYLE_MODE)
        channels_json = get_bot_setting("channels_json", json.dumps(CHANNELS, ensure_ascii=False))

        BOT_TOKEN = str(bot_token or BOT_TOKEN).strip()
        ADMIN_ID = int(admin_id)
        BROADCAST_DELAY = max(0.0, float(broadcast_delay))
        PAGE_SIZE = max(1, min(100, int(page_size)))
        BUTTON_STYLE_MODE = "glass" if str(button_style).lower() == "glass" else "colorful"
        parsed_channels = json.loads(channels_json)
        if isinstance(parsed_channels, list):
            CHANNELS = parsed_channels

        # نرخ استار از جدول اختصاصی خودش خوانده می‌شود.
        conn = db_connect()
        row = conn.execute("SELECT rate FROM star_config WHERE id=1").fetchone()
        conn.close()
        if row and row[0]:
            STAR_TO_TOMAN = int(row[0])
    except Exception as exc:
        print("Runtime settings load warning:", exc)




class _IPv4HTTPConnection(http.client.HTTPConnection):
    def connect(self):
        self.sock = socket.create_connection((self.host, self.port), self.timeout, source_address=self.source_address)


class _IPv4HTTPSConnection(http.client.HTTPSConnection):
    def connect(self):
        sock = socket.create_connection((self.host, self.port), self.timeout, source_address=self.source_address)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)





























def parse_volume_gb(volume):
    v = str(volume or "").strip().lower()
    if v in ("unlimited", "نامحدود", "∞"):
        return 0
    m = re.search(r"([0-9]+(?:\.[0-9]+)?)", v)
    if not m:
        raise ValueError("invalid volume")
    return int(float(m.group(1)) * 1024**3)

def duration_expiry_ms(months):
    return int((datetime.now(timezone.utc) + timedelta(days=30*int(months))).timestamp() * 1000)

# استار تلگرام: ۱ استار = چند تومان (قابل تغییر از پنل ادمین)
STAR_TO_TOMAN = 3200

def format_bytes(value):
    n=float(value or 0)
    units=("B","KB","MB","GB","TB")
    i=0
    while n>=1024 and i<len(units)-1: n/=1024; i+=1
    return f"{n:.1f} {units[i]}" if i else f"{int(n)} {units[i]}"

def iran_now():
    """زمان فعلی ایران به صورت naive برای سازگاری با SQLite."""
    return datetime.now(IRAN_TZ).replace(tzinfo=None)

def iran_time_text(value):
    """UTC/naive DB timestamp را به ساعت ایران برای نمایش تبدیل می‌کند."""
    if value is None:
        return "نامشخص"
    if isinstance(value, datetime):
        dt = value
    else:
        raw = str(value).strip()
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            try:
                dt = datetime.strptime(raw, "%Y-%m-%d %H:%M:%S")
            except ValueError:
                return raw
    if dt.tzinfo is None:
        # CURRENT_TIMESTAMP در SQLite به UTC ذخیره می‌شود.
        dt = dt.replace(tzinfo=UTC_TZ)
    return dt.astimezone(IRAN_TZ).strftime("%Y/%m/%d %H:%M:%S")

def iran_db_now_text():
    return iran_now().strftime("%Y-%m-%d %H:%M:%S")


def iran_local_time_text(value):
    """Format timestamps that are intentionally stored as Iran-local naive text."""
    if value is None:
        return ""
    raw = str(value).strip()
    if not raw:
        return ""
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(raw, fmt).strftime("%Y/%m/%d %H:%M:%S")
        except ValueError:
            pass
    return raw


def is_invalid_media_error(error):
    """Telegram errors that mean the stored media reference is unusable.
    These are expected recovery cases, not application failures.
    """
    msg = str(error or "").lower()
    return any(marker in msg for marker in (
        "wrong file identifier/http url specified",
        "wrong file identifier",
        "file identifier",
        "bad file",
    ))

async def safe_bot_edit_text(bot, chat_id, message_id, text, reply_markup=None, parse_mode=None):
    """Edit a bot message without leaking expected stale-message errors."""
    try:
        return await bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=str(text),
            reply_markup=reply_markup,
            parse_mode=parse_mode,
        )
    except TelegramError as e:
        msg = str(e).lower()
        if any(marker in msg for marker in (
            "message to edit not found",
            "message can't be edited",
            "message is not modified",
            "message not found",
        )):
            return None
        raise

async def download_telegram_media(bot, media_id, target_path):
    """Best-effort durable local archive for a Telegram media file."""
    try:
        os.makedirs(os.path.dirname(target_path), exist_ok=True)
        tg_file = await bot.get_file(media_id)
        await tg_file.download_to_drive(target_path)
        return target_path if os.path.isfile(target_path) else None
    except TelegramError:
        return None
    except OSError:
        return None




async def send_receipt_media(bot, chat_id, receipt_file_id=None, archive_chat_id=None, archive_message_id=None,
                             local_path=None, caption=None, reply_markup=None, deposit_id=None):
    """Display a receipt through independent recovery layers.
    Invalid Telegram file_ids are expected stale-data cases and stay silent.
    """
    caption = str(caption or "")
    kwargs = {"chat_id": chat_id, "caption": caption, "reply_markup": reply_markup, "parse_mode": "HTML"}
    if receipt_file_id:
        try:
            sent = await bot.send_photo(photo=receipt_file_id, **kwargs)
            if deposit_id and (not local_path or not os.path.isfile(local_path)):
                try:
                    saved = await download_telegram_media(bot, receipt_file_id, os.path.join(RECEIPTS_DIR, f"deposit_{int(deposit_id)}.jpg"))
                    if saved:
                        conn = db_connect()
                        try:
                            conn.execute("UPDATE deposits SET receipt_local_path=? WHERE id=?", (saved, int(deposit_id)))
                            conn.commit()
                        finally:
                            conn.close()
                except Exception:
                    pass
            return sent
        except TelegramError as e:
            if not is_invalid_media_error(e):
                print(f"Receipt file preview error: {type(e).__name__}: {e}")
    if local_path and os.path.isfile(local_path):
        try:
            return await bot.send_photo(photo=local_path, **kwargs)
        except TelegramError as e:
            print(f"Receipt local archive preview error: {type(e).__name__}: {e}")
    if archive_chat_id is not None and archive_message_id is not None:
        try:
            copied = await bot.copy_message(
                chat_id=chat_id, from_chat_id=int(archive_chat_id), message_id=int(archive_message_id),
                reply_markup=reply_markup
            )
            if caption:
                await bot.send_message(chat_id=chat_id, text=caption, parse_mode="HTML")
            return copied
        except TelegramError as e:
            if not is_invalid_media_error(e):
                print(f"Receipt Telegram archive copy error: {type(e).__name__}: {e}")
    return None


async def safe_query_answer(query, *args, **kwargs):
    try:
        await query.answer(*args, **kwargs)
    except TelegramError:
        pass


async def context_bot_send_text(query, text, reply_markup=None, parse_mode=None):
    """پس از حذف پیام رسانه‌ای، لیست متنی را در همان چت بازسازی می‌کند."""
    message = getattr(query, "message", None)
    chat = getattr(message, "chat", None)
    chat_id = getattr(chat, "id", None)
    if chat_id is None:
        raise TelegramError("Cannot determine chat id")
    return await query.get_bot().send_message(
        chat_id=chat_id,
        text=str(text),
        reply_markup=reply_markup,
        parse_mode=parse_mode
    )


async def safe_edit_message(query, text, reply_markup=None, parse_mode=None):
    """ویرایش امن هر نوع پیام تلگرام؛ پیام متنی و رسانه‌ای را درست تشخیص می‌دهد."""
    message = getattr(query, "message", None)
    media_fields = (
        "photo", "document", "video", "animation", "audio",
        "voice", "video_note", "sticker"
    )
    has_media = bool(message and any(getattr(message, name, None) for name in media_fields))
    try:
        if has_media and not getattr(message, "sticker", None):
            caption = str(text)
            # وقتی از جزئیات رسید/پرداخت به یک لیست یا منوی دیگر می‌رویم،
            # نباید عکس رسید با edit_caption باقی بماند. فقط خودِ صفحات detail
            # اجازه دارند همان عکس را به عنوان پیش‌نمایش نگه دارند.
            callback_data = str(getattr(query, "data", "") or "")
            keep_media = "detail" in callback_data.lower()
            if not keep_media or len(caption) > 1024:
                try:
                    await message.delete()
                except TelegramError:
                    pass
                return await context_bot_send_text(query, caption, reply_markup, parse_mode)
            return await message.edit_caption(
                caption=caption,
                reply_markup=reply_markup,
                parse_mode=parse_mode
            )
        return await query.edit_message_text(
            text=str(text),
            reply_markup=reply_markup,
            parse_mode=parse_mode
        )
    except TelegramError as e:
        msg = str(e).lower()
        recoverable = (
            "there is no text in the message to edit" in msg
            or "there is no caption in the message to edit" in msg
            or "message is not modified" in msg
            or "message can't be edited" in msg
            or "message to edit not found" in msg
            or "message not found" in msg
            or "message identifier is not specified" in msg
        )
        if recoverable:
            # Callbackها ممکن است به پیامی اشاره کنند که کاربر/ادمین قبلاً
            # حذفش کرده، یا Telegram دیگر اجازه ویرایشش را نمی‌دهد.
            # در این حالت به‌جای Unhandled bot error یک پیام تازه می‌سازیم.
            try:
                return await context_bot_send_text(query, text, reply_markup, parse_mode)
            except TelegramError:
                pass
        raise
async def replace_message_with_text(query, text, reply_markup=None, parse_mode=None):
    """نمایش متن در همان نقطه؛ اگر پیام فعلی رسانه‌ای باشد، اول خود رسانه را حذف می‌کند."""
    message = getattr(query, "message", None)
    media_fields = ("photo", "document", "video", "animation", "audio", "voice", "video_note", "sticker")
    has_media = bool(message and any(getattr(message, name, None) for name in media_fields))
    if has_media:
        try:
            await message.delete()
        except TelegramError:
            pass
        return await message.chat.send_message(
            text=str(text), reply_markup=reply_markup, parse_mode=parse_mode
        )
    return await safe_edit_message(query, text, reply_markup=reply_markup, parse_mode=parse_mode)

BROADCAST_BATCH_SIZE = 50

# کانال‌هایی که کاربر باید عضو آن‌ها باشد
# username را بدون @ وارد کنید
CHANNELS = [
    {
        "username": "Shadow_V2rayVpn",
        "title": "Shadow V2ray",
        "link": "https://t.me/Shadow_V2rayVpn",
    },
]


# =========================
# ابزارهای سریع دیتابیس و state
# =========================
def db_connect():
    conn = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    # WAL mode is persistent at database level and is initialized once in init_db.
    # Setting it on every connection adds unnecessary locking/overhead.
    return conn

INPUT_STATE_KEYS = (
    "admin_search", "admin_subscription_reply", "admin_messaging",
    "waiting_for_broadcast", "waiting_for_discount_create",
    "waiting_for_discount_code", "waiting_for_cdn_price",
    "waiting_for_wallet_reason", "wallet_action", "wallet_target",
    "wallet_amount", "wallet_reason", "waiting_for_amount",
    "payment_method", "deposit_amount", "editing_cdn_volume",
    "waiting_for_xui_url", "waiting_for_xui_username", "waiting_for_xui_password", "waiting_for_xui_token",
    "discount_volume", "discount_months", "discount_code", "discount_price", "discount_amount", "discount_service_type",
    "waiting_for_tunnel_price", "editing_tunnel_volume",
    "waiting_for_service_create", "waiting_for_service_plan_create", "waiting_for_service_plan_edit", "waiting_for_service_plan_price_edit", "waiting_for_service_plan_volume_edit", "waiting_for_service_rename", "waiting_for_service_code_edit", "waiting_for_service_extra_day_price", "waiting_for_service_extra_gb_price", "waiting_for_service_price_percentage", "service_price_percentage_service_id", "service_price_percentage_direction", "waiting_for_extra_traffic", "waiting_for_renewal_days", "renewal_days", "service_admin_id", "service_admin_plan_id", "renewal_sub_id", "payment_return_pending", "waiting_custom_base", "waiting_custom_monthly", "waiting_custom_day", "waiting_custom_country_name", "waiting_custom_country_price", "custom_waiting_volume", "custom_waiting_discount", "custom_admin_country_id",
)

def clear_input_states(context, keep=()):
    keep = set(keep or ())
    for key in INPUT_STATE_KEYS:
        if key not in keep:
            context.user_data.pop(key, None)

def begin_input_state(context, *keep):
    clear_input_states(context, keep=keep)

def clear_admin_states(context):
    for key in (
        "admin_search", "admin_subscription_reply", "admin_messaging",
        "waiting_for_broadcast", "waiting_for_discount_create",
        "waiting_for_discount_code", "discount_code", "discount_price",
        "waiting_for_cdn_price", "editing_cdn_volume",
        "waiting_for_tunnel_price", "editing_tunnel_volume",
        "waiting_for_service_create", "waiting_for_service_plan_create", "waiting_for_service_plan_edit", "waiting_for_service_plan_price_edit", "waiting_for_service_plan_volume_edit", "waiting_for_service_rename", "waiting_for_service_code_edit", "waiting_for_service_extra_day_price", "waiting_for_service_extra_gb_price", "waiting_for_service_price_percentage", "service_price_percentage_service_id", "service_price_percentage_direction", "waiting_for_extra_traffic", "waiting_for_renewal_days", "renewal_days", "service_admin_id", "service_admin_plan_id", "renewal_sub_id", "waiting_custom_base", "waiting_custom_monthly", "waiting_custom_day", "waiting_custom_country_name", "waiting_custom_country_price", "custom_waiting_volume", "custom_waiting_discount",
    ):
        context.user_data.pop(key, None)

# =========================
# بررسی عضویت
# =========================

async def is_member(bot, user_id, channel_username):
    if user_id == ADMIN_ID:
        return True
    try:
        member = await bot.get_chat_member(
            chat_id=f"@{channel_username}",
            user_id=user_id
        )
        return member.status in ("member", "administrator", "creator")
    except TelegramError as e:
        msg = str(e)
        if "Member list is inaccessible" in msg or "member list is inaccessible" in msg:
            print(f"Membership check unavailable for @{channel_username}: bot cannot access the channel member list.")
        else:
            print(f"Membership check error for @{channel_username}: {type(e).__name__}: {e}")
        return False
    except Exception as e:
        print(f"Membership check unexpected error for @{channel_username}: {type(e).__name__}: {e}")
        return False


def forced_join_enabled():
    """Whether mandatory channel membership is enabled globally."""
    value = str(get_bot_setting("forced_join_enabled", "1")).strip().lower()
    return value not in ("0", "false", "off", "no", "disabled")


async def check_all_channels(bot, user_id):
    if not forced_join_enabled() or user_id == ADMIN_ID:
        return True
    for channel in CHANNELS:
        if not await is_member(bot, user_id, channel["username"]):
            return False
    return True


# =========================
# نمایش صفحه جوین اجباری
# =========================

async def show_join_page(update: Update):
    if not forced_join_enabled():
        return await show_main_menu(update, None)
    keyboard = []

    for channel in CHANNELS:
        keyboard.append([
            InlineKeyboardButton(
                f"📢 عضویت در {channel['title']}",
                url=channel["link"]
            )
        ])

    keyboard.append([
        InlineKeyboardButton(
            "✅ بررسی عضویت",
            callback_data="check_membership"
        )
    ])

    text = (
        "🔒 برای استفاده از ربات ابتدا باید "
        "در کانال‌های زیر عضو شوید:\n\n"
        "بعد از عضویت، روی «بررسی عضویت» بزنید."
    )

    if update.callback_query:
        await safe_edit_message(update.callback_query, 
            text=text,
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    else:
        await update.message.reply_text(
            text=text,
            reply_markup=InlineKeyboardMarkup(keyboard)
        )


# =========================
# دستور /start
# =========================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    clear_input_states(context)
    user_id = update.effective_user.id

    # /start همیشه باید حالت‌های موقت قبلی را لغو کند.
    # مخصوصاً اگر ادمین وسط ارسال لینک اشتراک /start بزند،
    # پیام بعدی نباید برای سفارش قبلی ارسال شود.
    clear_admin_states(context)

    # --- ثبت / به‌روزرسانی اطلاعات کاربر ---
    conn = db_connect()
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT OR IGNORE INTO users (user_id, balance, username, full_name)
        VALUES (?, 0, ?, ?)
        """,
        (user_id, update.effective_user.username, update.effective_user.full_name)
    )
    cursor.execute(
        """
        UPDATE users SET username = ?, full_name = ?
        WHERE user_id = ?
        """,
        (update.effective_user.username, update.effective_user.full_name, user_id)
    )
    conn.commit()
    conn.close()
    # ----------------------------------------

    # =========================
    # بررسی مسدودیت   <-- اینجا، قبل از بررسی عضویت کانال
    # =========================

    conn = db_connect()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT is_banned FROM users WHERE user_id = ?",
        (user_id,)
    )
    row = cursor.fetchone()
    conn.close()

    if row and row[0] == 1:
        await update.message.reply_text(
            "🚫 حساب شما مسدود شده است.\n\n"
            "برای پیگیری با پشتیبانی تماس بگیرید."
        )
        return

    # --- بقیه start ---
    joined = await check_all_channels(
        context.bot,
        user_id
    )

    if not joined:
        await show_join_page(update)
        return

    await show_main_menu(update, context)

# =========================
# بررسی دوباره عضویت
# =========================

async def check_membership(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await safe_query_answer(query)
    user_id = query.from_user.id
    if not await check_all_channels(context.bot, user_id):
        await show_join_page(update)
        return
    await show_main_menu(update, context)


# =========================
# منوی اصلی و زبان کاربر
# =========================
def get_user_language(user_id):
    try:
        conn = db_connect()
        row = conn.execute("SELECT language FROM user_languages WHERE user_id=?", (int(user_id),)).fetchone()
        conn.close()
        return (row[0] if row else "fa") or "fa"
    except Exception:
        return "fa"

def set_user_language(user_id, language):
    conn = db_connect()
    try:
        conn.execute("INSERT INTO user_languages(user_id,language) VALUES(?,?) ON CONFLICT(user_id) DO UPDATE SET language=excluded.language", (int(user_id), "en" if language == "en" else "fa"))
        conn.commit()
    finally:
        conn.close()

def main_menu_keyboard(user_id):
    en = get_user_language(user_id) == "en"

    # استایل یکدست و مینیمال؛ تلگرام رنگ/شفافیت واقعی برای InlineKeyboard ندارد،
    # بنابراین حس شیشه‌ای با چیدمان، فاصله و متن کوتاه ساخته می‌شود.
    keyboard = [
        [
            InlineKeyboardButton("🛍️ Buy Products" if en else "🛍️ خرید محصول", callback_data="buy_subscription"),
            InlineKeyboardButton("📦 Purchased Products" if en else "📦 محصولات خریداری‌شده", callback_data="my_subscriptions"),
        ],
        [
            InlineKeyboardButton("💰 My Balance" if en else "💰 موجودی من", callback_data="my_balance"),
            InlineKeyboardButton("💎 Add Funds" if en else "💎 شارژ حساب", callback_data="charge"),
        ],
        [
            InlineKeyboardButton("🧾 My Payments" if en else "🧾 پرداخت‌های من", callback_data="my_deposits"),
            InlineKeyboardButton("🤝 Agency" if en else "🤝 نمایندگی", callback_data="agency"),
        ],
        [
            InlineKeyboardButton("✨ About Bot" if en else "✨ درباره ربات", callback_data="about"),
            InlineKeyboardButton("🌐 Language: فارسی" if en else "🌐 Languech: English", callback_data="toggle_language"),
        ],
    ]
    if user_id == ADMIN_ID:
        keyboard.append([
            InlineKeyboardButton("⚙️  پنل مدیریت", callback_data="admin_panel")
        ])
    return InlineKeyboardMarkup(keyboard)


async def show_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE = None):
    if context is not None:
        clear_input_states(context)

    user_id = update.effective_user.id
    text = (
        ("✨ <b>Welcome, friend!</b>\n━━━━━━━━━━━━━━\nEverything you need is here: shop, add funds, and track your purchases.\n\n👇 <b>Where shall we start?</b>" if get_user_language(user_id) == "en" else
         "✨ <b>خوش اومدی رفیق!</b>\n━━━━━━━━━━━━━━\nاینجا همه‌چی دم دستته؛ خرید محصول، شارژ و پیگیری خریدها.\n\n👇 <b>از کجا شروع کنیم؟</b>")
    )
    markup = main_menu_keyboard(user_id)

    if update.callback_query:
        # برای callbackها پیام جدید نساز؛ همان پیام قبلی را به منوی اصلی تبدیل کن.
        await safe_edit_message(
            update.callback_query,
            text,
            reply_markup=markup,
            parse_mode="HTML"
        )
    else:
        # /start فقط یک پیام منوی اصلی ارسال می‌کند.
        await update.message.reply_text(
            text=text,
            reply_markup=markup,
            parse_mode="HTML"
        )


# =========================
# admin panel
# =========================
async def admin_panel(
        update: Update,
        context: ContextTypes.DEFAULT_TYPE
):
    query = update.callback_query
    user_id = query.from_user.id

    clear_admin_states(context)

    if user_id != ADMIN_ID:
        await safe_query_answer(query, 
            "❌ شما دسترسی به پنل مدیریت ندارید.",
            show_alert=True
        )
        return

    await safe_query_answer(query)

    keyboard = [
        [InlineKeyboardButton("👥 کاربران", callback_data="admin_users"), InlineKeyboardButton("💰 کیف پول", callback_data="admin_wallet")],
        [InlineKeyboardButton("🛒 مدیریت محصولات", callback_data="products_admin")],
        [InlineKeyboardButton("🧾 محصولات خریداری‌شده", callback_data="admin_purchased_products")],
        [InlineKeyboardButton("📥 دریافت بکاپ دیتابیس", callback_data="admin_backup_db")],
        [InlineKeyboardButton("💳 پرداخت‌ها", callback_data="admin_payments")],
        [InlineKeyboardButton("⏳ پرداخت‌های در انتظار", callback_data="admin_pending_actions")],
        [InlineKeyboardButton("📢 ارسال پیام همگانی", callback_data="admin_broadcast")],
        [InlineKeyboardButton("🎟 مدیریت کدهای تخفیف", callback_data="admin_discounts")],
        [InlineKeyboardButton("⚙️ تنظیمات ربات", callback_data="admin_settings")],
        [InlineKeyboardButton("📊 آمار کلی ربات", callback_data="admin_stats")],
        [InlineKeyboardButton("‹ برگشت", callback_data="back_main")]
    ]

    await safe_edit_message(query,
        "⚙️ <b>پنل مدیریت</b>\n\n"
        "لطفاً بخش موردنظر را انتخاب کنید:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="HTML"
    )


# پاسخ ادمین


# =========================
# مدیریت درخواست‌های در انتظار
# =========================

async def show_pending_actions(query, context):
    """فهرست درخواست‌های پرداخت؛ جریان اشتراک قدیمی حذف شده است."""
    if query.from_user.id != ADMIN_ID:
        await safe_query_answer(query, "❌ شما دسترسی ندارید.", show_alert=True)
        return
    conn = db_connect()
    pending_deposits = int(conn.execute("SELECT COUNT(*) FROM deposits WHERE status='pending'").fetchone()[0] or 0)
    conn.close()
    kb = [
        [InlineKeyboardButton(f"💳 پرداخت‌های در انتظار ({pending_deposits:,})", callback_data="admin_pending_payments")],
        [InlineKeyboardButton("🔄 بروزرسانی", callback_data="admin_pending_actions")],
        [InlineKeyboardButton("🔙 پنل مدیریت", callback_data="admin_panel")]
    ]
    await safe_edit_message(query,
        "⏳ <b>درخواست‌های در انتظار</b>\n\n"
        f"💳 پرداخت‌های در انتظار تأیید: <b>{pending_deposits:,}</b>",
        reply_markup=InlineKeyboardMarkup(kb), parse_mode="HTML")


# =========================
# لیست تمدید‌های در انتظار
# =========================



# =========================
# لیست پرداخت‌های pending
# =========================

async def show_pending_payments(query, context):
    """نمایش فقط پرداخت‌هایی که هنوز بررسی نشده‌اند"""

    user_id = query.from_user.id

    if user_id != ADMIN_ID:
        await safe_query_answer(query, 
            "❌ شما دسترسی ندارید.",
            show_alert=True
        )
        return

    conn = db_connect()
    cursor = conn.cursor()

    cursor.execute(
        """
        SELECT d.id, d.user_id, d.amount, d.payment_method, d.created_at,
               u.username, u.full_name
        FROM deposits d
        LEFT JOIN users u ON u.user_id = d.user_id
        WHERE d.status = 'pending'
        ORDER BY id ASC
        """
    )

    deposits = cursor.fetchall()
    conn.close()

    keyboard = []

    if not deposits:
        keyboard.append([
            InlineKeyboardButton(
                "🔄 بروزرسانی",
                callback_data="admin_pending_payments"
            )
        ])
        keyboard.append([
            InlineKeyboardButton(
                "🔙 درخواست‌های در انتظار",
                callback_data="admin_pending_actions"
            )
        ])

        await replace_message_with_text(query, 
            "💳 <b>پرداخت‌های در انتظار تأیید</b>\n\n"
            "✅ در حال حاضر هیچ پرداختی در انتظار بررسی نیست.",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="HTML"
        )

        return

    text = (
        "💳 <b>پرداخت‌های در انتظار تأیید</b>\n\n"
        f"⏳ تعداد: <b>{len(deposits)}</b>\n\n"
        "برای بررسی، روی هر پرداخت بزنید:\n"
    )

    method_titles = {
        "card": "💳 کارت به کارت",
        "ton": "💎 TON",
        "star": "⭐️ استار",
    }

    for (
        deposit_id,
        target_user_id,
        amount,
        method,
        created_at,
        username,
        full_name
    ) in deposits:

        method_title = method_titles.get(
            method,
            method or "نامشخص"
        )

        user_title = (
            f"@{username}"
            if username
            else (full_name or f"ID:{target_user_id}")
        )

        text += (
            f"\n⏳ <b>#{deposit_id}</b>\n"
            f"👤 {user_title}\n"
            f"💰 {amount:,} تومان\n"
            f"💳 {method_title}\n"
            f"📅 {iran_time_text(created_at)}\n"
        )

        keyboard.append([
            InlineKeyboardButton(
                f"⏳ #{deposit_id} | {user_title} | {amount:,} تومان",
                callback_data=f"admin_pending_deposit_detail_{deposit_id}"
            )
        ])

    keyboard.append([
        InlineKeyboardButton(
            "🔄 بروزرسانی",
            callback_data="admin_pending_payments"
        )
    ])

    keyboard.append([
        InlineKeyboardButton(
            "🔙 درخواست‌های در انتظار",
            callback_data="admin_pending_actions"
        )
    ])

    await replace_message_with_text(query, 
        text,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="HTML"
    )


# =========================
# لیست اشتراک‌های pending
# =========================



# =========================
# صفحه‌بندی پنل و کد تخفیف
# =========================
async def show_admin_payments_page(query, context, page=0):
    if query.from_user.id != ADMIN_ID:
        return
    offset = max(0, page) * PAGE_SIZE
    conn = db_connect(); cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM deposits"); total = cur.fetchone()[0]
    cur.execute("""SELECT d.id,d.user_id,d.amount,d.status,d.created_at,
                   COALESCE(u.username,''),COALESCE(u.full_name,'')
                   FROM deposits d LEFT JOIN users u ON u.user_id=d.user_id
                   ORDER BY d.id DESC LIMIT ? OFFSET ?""", (PAGE_SIZE, offset))
    rows=cur.fetchall()
    cur.execute("SELECT COUNT(*) FROM deposits WHERE status='pending'"); pending=cur.fetchone()[0]
    cur.execute("SELECT COALESCE(SUM(amount),0) FROM deposits WHERE status='approved'"); approved=cur.fetchone()[0]
    conn.close()
    text=f"💳 مدیریت پرداخت‌ها\n\n⏳ در انتظار: {pending:,}\n✅ مجموع تأیید شده: {approved:,} تومان\n\n📋 صفحه {page+1} از {max(1,(total+PAGE_SIZE-1)//PAGE_SIZE)}\n"
    kb=[]
    for dep_id,uid,amount,status,created,username,fullname in rows:
        name=f"@{username.lstrip('@')}" if username else (fullname or f"ID:{uid}")
        icon={'pending':'⏳','approved':'✅','rejected':'❌'}.get(status,'❓')
        kb.append([InlineKeyboardButton(f"{icon} #{dep_id} | {name} | 💰 {amount:,}",callback_data=f"admin_deposit_detail_{dep_id}")])
    nav=[]
    if page>0: nav.append(InlineKeyboardButton("⬅️ قبلی",callback_data=f"admin_payments_page_{page-1}"))
    if offset+len(rows)<total: nav.append(InlineKeyboardButton("بعدی ➡️",callback_data=f"admin_payments_page_{page+1}"))
    if nav: kb.append(nav)
    kb += [[InlineKeyboardButton("🔄 بروزرسانی",callback_data=f"admin_payments_page_{page}")],[InlineKeyboardButton("🔙 پنل مدیریت",callback_data="admin_panel")]]
    await safe_edit_message(query, text,reply_markup=InlineKeyboardMarkup(kb))


async def show_my_deposits_page(query, user_id, page=0):
    page=max(0,int(page)); offset=page*PAGE_SIZE
    conn=db_connect(); cur=conn.cursor()
    total=cur.execute("SELECT COUNT(*) FROM deposits WHERE user_id=?",(user_id,)).fetchone()[0]
    rows=cur.execute("SELECT id,amount,payment_method,status,created_at,processed_at FROM deposits WHERE user_id=? ORDER BY id DESC LIMIT ? OFFSET ?",(user_id,PAGE_SIZE,offset)).fetchall(); conn.close()
    total_pages=max(1,(total+PAGE_SIZE-1)//PAGE_SIZE)
    if not rows:
        await safe_edit_message(query, "💳 <b>پرداخت‌های من</b>\n\n❌ موردی وجود ندارد.",parse_mode="HTML",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("‹  برگشت",callback_data="back_main")]])); return
    st={'pending':'⏳ در انتظار تأیید','approved':'✅ تأیید شده','rejected':'❌ رد شده'}
    text=f"💳 <b>پرداخت‌های من</b>\n\n📋 صفحه <b>{page+1}</b> از <b>{total_pages}</b>\n\nیکی از پرداخت‌ها را انتخاب کنید:"
    kb=[]
    for did,amt,method,status,created,processed in rows:
        date_text = iran_time_text(created) if created else "نامشخص"
        kb.append([InlineKeyboardButton(
            f"{st.get(status,status)}  #{did}  •  {amt:,} تومان  •  📅 {date_text}",
            callback_data=f"my_deposit_detail_{did}"
        )])
    nav=[]
    if page>0: nav.append(InlineKeyboardButton("⬅️ قبلی",callback_data=f"my_deposits_page_{page-1}"))
    if offset+len(rows)<total: nav.append(InlineKeyboardButton("بعدی ➡️",callback_data=f"my_deposits_page_{page+1}"))
    if nav: kb.append(nav)
    kb += [[InlineKeyboardButton("💎 شارژ حساب",callback_data="charge")],[InlineKeyboardButton("‹  برگشت",callback_data="back_main")]]
    await safe_edit_message(query, text,reply_markup=InlineKeyboardMarkup(kb),parse_mode="HTML")

async def show_my_deposit_detail(query,user_id,deposit_id):
    conn=db_connect(); cur=conn.cursor(); row=cur.execute(
        "SELECT id,amount,payment_method,status,created_at,processed_at,receipt_file_id,receipt_archive_message_id,receipt_archive_chat_id,receipt_local_path FROM deposits WHERE id=? AND user_id=?",
        (deposit_id,user_id)
    ).fetchone(); conn.close()
    if not row:
        await safe_query_answer(query,"❌ پرداخت پیدا نشد.",show_alert=True); return
    did,amt,method,status,created,processed,receipt_file_id,receipt_archive_message_id,receipt_archive_chat_id,receipt_local_path=row
    st={'pending':'⏳ در انتظار تأیید','approved':'✅ تأیید شده','rejected':'❌ رد شده'}; mt={'card':'💳 کارت به کارت','ton':'💎 TON','star':'⭐️ استار تلگرام'}
    text=(f"💳 <b>جزئیات پرداخت #{did}</b>\n\n💰 مبلغ: <b>{amt:,} تومان</b>\n💳 روش پرداخت: {html.escape(mt.get(method,method or 'نامشخص'))}\n📌 وضعیت: {st.get(status,status)}\n📅 ثبت: {html.escape(iran_time_text(created) if created else 'نامشخص')}")
    if processed: text+=f"\n🕒 پردازش: {html.escape(iran_time_text(processed))}"
    markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 پرداخت‌های من",callback_data="my_deposits")],[InlineKeyboardButton("🏠 منوی اصلی",callback_data="back_main")]])
    if receipt_file_id:
        # رسید باید جای پیام لیست را بگیرد؛ در نتیجه با برگشت، helper عمومی آن را حذف می‌کند.
        message=getattr(query,"message",None)
        if message and not getattr(message,"photo",None):
            try: await message.delete()
            except TelegramError: pass
            try:
                sent = await send_receipt_media(query.get_bot(), user_id, receipt_file_id, receipt_archive_chat_id, receipt_archive_message_id, receipt_local_path, text, markup, deposit_id=did)
                if sent is None:
                    raise TelegramError("Receipt media is unavailable")
            except TelegramError:
                await message.chat.send_message(text=text+"\n\n📸 تصویر رسید در حال حاضر توسط تلگرام قابل بازیابی نیست؛ اطلاعات پرداخت شما محفوظ است.",reply_markup=markup,parse_mode="HTML")
        elif message and getattr(message,"photo",None):
            await safe_edit_message(query,text,parse_mode="HTML",reply_markup=markup)
        else:
            try:
                sent = await send_receipt_media(query.get_bot(), user_id, receipt_file_id, receipt_archive_chat_id, receipt_archive_message_id, receipt_local_path, text, markup, deposit_id=did)
                if sent is None:
                    raise TelegramError("Receipt media is unavailable")
            except TelegramError:
                await query.get_bot().send_message(chat_id=user_id,text=text+"\n\n📸 تصویر رسید در حال حاضر توسط تلگرام قابل بازیابی نیست؛ اطلاعات پرداخت شما محفوظ است.",reply_markup=markup,parse_mode="HTML")
        await safe_query_answer(query,"📸 رسید نمایش داده شد.")
        return
    await safe_edit_message(query,text,parse_mode="HTML",reply_markup=markup)


async def context_bot_send_low_traffic(query, remaining):
    try:
        await query.get_bot().send_message(chat_id=query.from_user.id,text=f"⚠️ <b>حجم سرویس شما رو به پایان است</b>\n\n📦 حجم باقی‌مانده: <b>{format_bytes(remaining)}</b>\n💡 برای ادامه، از بخش «تمدید سرویس» اقدام کنید.",parse_mode="HTML")
    except Exception as exc:
        print("Low traffic direct alert error:",exc)




async def discount_admin_menu(query):
    """سازگاری با مسیرهای قدیمی؛ پنل جدید صفحه‌بندی‌شده را نمایش می‌دهد."""
    await show_discount_admin_page(query, 0)

async def show_admin_users_page(query, context, page=0):
    if query.from_user.id != ADMIN_ID: return
    offset=max(0,page)*PAGE_SIZE; conn=db_connect(); cur=conn.cursor()
    total=cur.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    rows=cur.execute("SELECT user_id,balance,is_banned,COALESCE(username,''),COALESCE(full_name,'') FROM users ORDER BY user_id DESC LIMIT ? OFFSET ?",(PAGE_SIZE,offset)).fetchall(); conn.close()
    text=f"👥 کاربران\n\n📋 صفحه {page+1} از {max(1,(total+PAGE_SIZE-1)//PAGE_SIZE)}\n\n"; kb=[]
    # ترتیب tuple: user_id,balance,is_banned,username,full_name
    text=f"👥 کاربران\n\n📋 صفحه {page+1} از {max(1,(total+PAGE_SIZE-1)//PAGE_SIZE)}\n\n"
    for uid,balance,is_banned,username,fullname in rows:
        name=f"@{username.lstrip('@')}" if username else (fullname or f"ID:{uid}")
        kb.append([InlineKeyboardButton(f"{'🚫' if is_banned else '👤'} {name} | {balance:,} تومان",callback_data=f"admin_user_{uid}")])
    nav=[]
    if page>0: nav.append(InlineKeyboardButton('⬅️ قبلی',callback_data=f'admin_users_page_{page-1}'))
    if offset+len(rows)<total: nav.append(InlineKeyboardButton('بعدی ➡️',callback_data=f'admin_users_page_{page+1}'))
    if nav: kb.append(nav)
    kb += [[InlineKeyboardButton('🔍 جستجوی کاربر',callback_data='admin_search')],[InlineKeyboardButton('🔙 پنل مدیریت',callback_data='admin_panel')]]
    await safe_edit_message(query, text,reply_markup=InlineKeyboardMarkup(kb))

async def show_pending_payments_page(query, context, page=0):
    if query.from_user.id != ADMIN_ID: return
    offset=max(0,page)*PAGE_SIZE; conn=db_connect(); cur=conn.cursor()
    total=cur.execute("SELECT COUNT(*) FROM deposits WHERE status='pending'").fetchone()[0]
    rows=cur.execute("""SELECT d.id,d.user_id,d.amount,d.payment_method,d.created_at,COALESCE(u.username,''),COALESCE(u.full_name,'')
                       FROM deposits d LEFT JOIN users u ON u.user_id=d.user_id WHERE d.status='pending' ORDER BY d.id ASC LIMIT ? OFFSET ?""",(PAGE_SIZE,offset)).fetchall(); conn.close()
    text=f"⏳ پرداخت‌های در انتظار\n\n📋 صفحه {page+1} از {max(1,(total+PAGE_SIZE-1)//PAGE_SIZE)}\n\n"; kb=[]
    for did,uid,amt,method,created,username,fullname in rows:
        name=f"@{username.lstrip('@')}" if username else (fullname or f"ID:{uid}")
        kb.append([InlineKeyboardButton(f"⏳ #{did} | {name} | {amt:,} تومان",callback_data=f"admin_pending_deposit_detail_{did}")])
    nav=[]
    if page>0: nav.append(InlineKeyboardButton('⬅️ قبلی',callback_data=f'admin_pending_payments_page_{page-1}'))
    if offset+len(rows)<total: nav.append(InlineKeyboardButton('بعدی ➡️',callback_data=f'admin_pending_payments_page_{page+1}'))
    if nav: kb.append(nav)
    kb.append([InlineKeyboardButton('🔙 درخواست‌های در انتظار',callback_data='admin_pending_actions')])
    await replace_message_with_text(query, text if rows else '⏳ پرداخت‌های در انتظار\n\n✅ موردی وجود ندارد.',reply_markup=InlineKeyboardMarkup(kb))


# =========================
# ابزارهای صفحه‌بندی پنل مدیریت
# =========================
def _page_nav(page, has_next, prev_cb, next_cb):
    row=[]
    if page > 0:
        row.append(InlineKeyboardButton("⬅️ قبلی", callback_data=prev_cb))
    if has_next:
        row.append(InlineKeyboardButton("بعدی ➡️", callback_data=next_cb))
    return [row] if row else []

async def show_admin_wallet_page(query, context, page=0):
    if query.from_user.id != ADMIN_ID: return
    page=max(0,page); offset=page*PAGE_SIZE
    conn=db_connect(); cur=conn.cursor()
    total=cur.execute("SELECT COUNT(*) FROM balance_logs").fetchone()[0]
    rows=cur.execute("""SELECT id,user_id,action,amount,old_balance,new_balance,reason,created_at
                       FROM balance_logs ORDER BY id DESC LIMIT ? OFFSET ?""",(PAGE_SIZE,offset)).fetchall()
    total_users=cur.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    total_balance=cur.execute("SELECT COALESCE(SUM(balance),0) FROM users").fetchone()[0]
    conn.close()
    titles={"add":("➕","افزایش"),"sub":("➖","کاهش"),"set":("✏️","تنظیم")}
    total_pages=max(1,(total+PAGE_SIZE-1)//PAGE_SIZE)
    text=(f"💰 <b>کیف پول من</b>\n\n👥 کاربران: <b>{total_users:,}</b>\n"
          f"💵 مجموع موجودی: <b>{total_balance:,}</b> تومان\n"
          f"📋 کل تراکنش‌ها: <b>{total:,}</b>\n\n📄 صفحه <b>{page+1}</b> از <b>{total_pages}</b>\n\n")
    kb=[]
    for log_id,uid,action,amount,oldb,newb,reason,created in rows:
        e,t=titles.get(action,("❓",action))
        text += f"{e} <b>{t}</b> {amount:,} تومان\n🆔 <code>{uid}</code> | {iran_time_text(created)}\n└ {oldb:,} ← {newb:,}\n"
        if reason: text += f"📝 {html.escape(str(reason))}\n"
        text += "\n"
        kb.append([InlineKeyboardButton(f"{e} کاربر {uid} | {amount:,} تومان",callback_data=f"admin_user_wallet_{uid}")])
    kb += _page_nav(page, offset+len(rows)<total, f"admin_wallet_page_{page-1}", f"admin_wallet_page_{page+1}")
    kb += [[InlineKeyboardButton("🔄 بروزرسانی",callback_data=f"admin_wallet_page_{page}")],
           [InlineKeyboardButton("🔙 پنل مدیریت",callback_data="admin_panel")]]
    await safe_edit_message(query, text,reply_markup=InlineKeyboardMarkup(kb),parse_mode="HTML")


async def show_admin_user_deposits_page(query, target_user_id, page=0):
    if query.from_user.id != ADMIN_ID: return
    page=max(0,page); offset=page*PAGE_SIZE
    conn=db_connect(); cur=conn.cursor()
    total=cur.execute("SELECT COUNT(*) FROM deposits WHERE user_id=?",(target_user_id,)).fetchone()[0]
    rows=cur.execute("""SELECT id,amount,payment_method,status,processed_by,created_at,processed_at
                       FROM deposits WHERE user_id=? ORDER BY id DESC LIMIT ? OFFSET ?""",(target_user_id,PAGE_SIZE,offset)).fetchall()
    paid=cur.execute("SELECT COALESCE(SUM(amount),0) FROM deposits WHERE user_id=? AND status='approved'",(target_user_id,)).fetchone()[0]
    pending=cur.execute("SELECT COUNT(*) FROM deposits WHERE user_id=? AND status='pending'",(target_user_id,)).fetchone()[0]
    conn.close()
    icons={"pending":"⏳","approved":"✅","rejected":"❌"}; methods={"card":"💳 کارت به کارت","ton":"💎 TON","star":"⭐️ استار"}
    total_pages=max(1,(total+PAGE_SIZE-1)//PAGE_SIZE)
    text=f"💳 <b>پرداخت‌های کاربر</b> <code>{target_user_id}</code>\n\n💰 تأیید شده: {paid:,} تومان\n⏳ در انتظار: {pending:,}\n📄 صفحه <b>{page+1}</b> از <b>{total_pages}</b>\n\n"
    kb=[]
    for did,amount,method,status,processed,created,processed_at in rows:
        date_text = iran_time_text(created) if created else "نامشخص"
        kb.append([InlineKeyboardButton(
            f"{icons.get(status,'❓')} #{did} | {amount:,} تومان | 📅 {date_text}",
            callback_data=f"admin_deposit_detail_{did}"
        )])
    kb += _page_nav(page, offset+len(rows)<total, f"admin_user_deposits_page_{target_user_id}_{page-1}", f"admin_user_deposits_page_{target_user_id}_{page+1}")
    kb += [[InlineKeyboardButton("🔄 بروزرسانی",callback_data=f"admin_user_deposits_page_{target_user_id}_{page}")],
           [InlineKeyboardButton("🔙 برگشت به اطلاعات کاربر",callback_data=f"admin_user_{target_user_id}")]]
    await safe_edit_message(query, text,reply_markup=InlineKeyboardMarkup(kb),parse_mode="HTML")

def validate_discount(conn, code, user_id, base_price):
    code = str(code or "").strip().upper()
    base_price = max(0, int(base_price or 0))
    row = conn.execute("SELECT id,code,discount_type,value,max_uses,used_count,max_uses_per_user,min_purchase,max_discount,expires_at FROM discount_codes WHERE UPPER(code)=? AND active=1 AND deleted_at IS NULL ORDER BY id DESC LIMIT 1", (code,)).fetchone()
    if not row: return None, "❌ کد تخفیف معتبر یا فعال نیست."
    did, saved_code, typ, value, max_uses, used_count, per_user, minimum, cap, expires = row
    if expires:
        try:
            if datetime.fromisoformat(str(expires)) < iran_now(): return None, "❌ مهلت استفاده از این کد به پایان رسیده است."
        except (ValueError, TypeError): pass
    if int(max_uses or 0) > 0 and int(used_count or 0) >= int(max_uses): return None, "❌ ظرفیت استفاده از این کد تمام شده است."
    if int(minimum or 0) > base_price: return None, f"❌ حداقل خرید این کد {int(minimum):,} تومان است."
    used = conn.execute("SELECT COUNT(*) FROM discount_usages WHERE discount_id=? AND user_id=?", (did,user_id)).fetchone()[0]
    try:
        used += conn.execute("SELECT COUNT(*) FROM product_discount_usages WHERE discount_id=? AND user_id=?", (did,user_id)).fetchone()[0]
    except sqlite3.OperationalError:
        pass
    if int(per_user or 0) > 0 and used >= int(per_user): return None, "❌ سقف استفاده شما از این کد تمام شده است."
    value=int(value or 0)
    discount=(base_price*value//100) if typ=="percent" else value
    if int(cap or 0)>0: discount=min(discount,int(cap))
    discount=min(base_price,max(0,discount))
    return {"id":int(did),"code":str(saved_code),"final":base_price-discount,"discount":discount}, None

def consume_discount(conn, discount_id, user_id, subscription_id, code, base_price):
    """ثبت مصرف کد تخفیف در همان تراکنش خرید؛ خطا را به صورت (None, message) برمی‌گرداند."""
    try:
        base_price = max(0, int(base_price or 0))
        row = conn.execute(
            "SELECT id, code, discount_type, value, max_uses, used_count, max_uses_per_user, min_purchase, max_discount, active, deleted_at "
            "FROM discount_codes WHERE id=?",
            (int(discount_id),)
        ).fetchone()
        if not row:
            return None, "❌ کد تخفیف پیدا نشد."
        did, saved_code, typ, value, max_uses, used_count, per_user, minimum, cap, active, deleted_at = row
        if not active or deleted_at is not None:
            return None, "❌ این کد تخفیف دیگر فعال نیست."
        if str(saved_code).strip().upper() != str(code or "").strip().upper():
            return None, "❌ کد تخفیف با سفارش مطابقت ندارد."
        if int(minimum or 0) > base_price:
            return None, f"❌ حداقل خرید این کد {int(minimum):,} تومان است."
        if int(max_uses or 0) > 0 and int(used_count or 0) >= int(max_uses):
            return None, "❌ ظرفیت استفاده از این کد تمام شده است."
        used_by_user = conn.execute(
            "SELECT COUNT(*) FROM discount_usages WHERE discount_id=? AND user_id=?",
            (int(did), int(user_id))
        ).fetchone()[0]
        if int(per_user or 0) > 0 and int(used_by_user) >= int(per_user):
            return None, "❌ سقف استفاده شما از این کد تمام شده است."
        value = int(value or 0)
        discount_amount = (base_price * value // 100) if typ == "percent" else value
        if int(cap or 0) > 0:
            discount_amount = min(discount_amount, int(cap))
        discount_amount = min(base_price, max(0, discount_amount))
        updated = conn.execute(
            "UPDATE discount_codes SET used_count=used_count+1 WHERE id=? AND active=1 AND deleted_at IS NULL AND (max_uses=0 OR used_count<max_uses)",
            (int(did),)
        )
        if updated.rowcount != 1:
            return None, "❌ ظرفیت استفاده از این کد همین الان تمام شده است. دوباره تلاش کنید."
        conn.execute(
            "INSERT INTO discount_usages(discount_id,user_id,subscription_id,base_price,discount_amount) VALUES(?,?,?,?,?)",
            (int(did), int(user_id), int(subscription_id), base_price, discount_amount)
        )
        return {"id": int(did), "discount": discount_amount}, None
    except Exception as exc:
        return None, f"❌ ثبت کد تخفیف انجام نشد: {exc}"

async def show_discount_admin_page(query, page=0):
    if query.from_user.id != ADMIN_ID: return
    page=max(0,page); offset=page*PAGE_SIZE
    conn=db_connect(); cur=conn.cursor()
    total=cur.execute("SELECT COUNT(*) FROM discount_codes WHERE deleted_at IS NULL").fetchone()[0]
    rows=cur.execute("SELECT id,code,discount_type,value,max_uses,used_count,max_uses_per_user,min_purchase,max_discount,active,expires_at FROM discount_codes WHERE deleted_at IS NULL ORDER BY id DESC LIMIT ? OFFSET ?",(PAGE_SIZE,offset)).fetchall()
    conn.close()
    total_pages=max(1,(total+PAGE_SIZE-1)//PAGE_SIZE)
    text=f"🎟 <b>مدیریت کدهای تخفیف</b>\n\n📄 صفحه <b>{page+1}</b> از <b>{total_pages}</b>\n\n"
    kb=[]
    for did,code,typ,val,maxuses,used,per_user,min_purchase,max_discount,active,expires in rows:
        detail=f"{val}%" if typ=='percent' else f"{val:,} تومان"
        limit='∞' if maxuses==0 else f"{used}/{maxuses}"
        user_limit='∞' if per_user==0 else f"هر کاربر {per_user}"
        state='🟢 فعال' if active else '🔴 غیرفعال'
        extra=[]
        if min_purchase>0: extra.append(f"حداقل {min_purchase:,}")
        if max_discount>0: extra.append(f"سقف {max_discount:,}")
        text += f"🎟 <b>{html.escape(code)}</b> | {detail} | کل: {limit} | {user_limit} | {state}" + (f" | {' | '.join(extra)}" if extra else '') + "\n"
        kb.append([InlineKeyboardButton(f"{state} {code}",callback_data=f"discount_toggle_{did}"), InlineKeyboardButton("🗑 حذف",callback_data=f"discount_delete_{did}")])
    kb += _page_nav(page, offset+len(rows)<total, f"admin_discounts_page_{page-1}", f"admin_discounts_page_{page+1}")
    kb += [[InlineKeyboardButton('➕ ساخت کد تخفیف',callback_data='discount_create')],
           [InlineKeyboardButton('🔄 بروزرسانی',callback_data=f'admin_discounts_page_{page}')],
           [InlineKeyboardButton('🔙 پنل مدیریت',callback_data='admin_panel')]]
    await safe_edit_message(query, text,reply_markup=InlineKeyboardMarkup(kb),parse_mode='HTML')

# =========================
# دکمه‌های منو
# =========================

class MutableCallbackQueryProxy:
    """Proxy امن برای callbackهای داخلی؛ Telegram CallbackQuery.data را readonly نگه می‌دارد."""
    def __init__(self, original, data):
        self._original = original
        self.data = data
        self.message = original.message
        self.from_user = original.from_user

    def __getattr__(self, name):
        return getattr(self._original, name)



SERVICE_CONFIG = {
    "cdn": {"title": "🌐 CDN", "table": "service_plans", "service_id": None},
    "tunnel": {"title": "🚇 Tunnel", "table": "service_plans", "service_id": None},
}





def sync_legacy_plan_table(conn, service_id, volume, price=None, delete=False):
    """Keep legacy CDN/Tunnel price tables synchronized with the generic service plan table."""
    row = conn.execute("SELECT code,legacy_key FROM service_catalog WHERE id=?", (service_id,)).fetchone()
    code = row[0] if row else None
    legacy_key = row[1] if row else None
    if legacy_key not in ("cdn", "tunnel"):
        return
    table = "cdn_prices" if legacy_key == "cdn" else "tunnel_prices"
    if delete:
        conn.execute(f"DELETE FROM {table} WHERE volume=?", (volume,))
    elif price is not None:
        conn.execute(
            f"INSERT INTO {table}(volume,price) VALUES(?,?) "
            f"ON CONFLICT(volume) DO UPDATE SET price=excluded.price",
            (volume, int(price)),
        )





































async def _charge_user(user_id, amount, reason):
    conn=db_connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row=conn.execute("SELECT balance,is_banned FROM users WHERE user_id=?",(user_id,)).fetchone()
        if not row or int(row[1]): conn.rollback(); return None
        balance=int(row[0] or 0); amount=int(amount)
        if balance<amount: conn.rollback(); return (False,balance)
        new_balance=balance-amount
        cur=conn.execute("UPDATE users SET balance=? WHERE user_id=? AND balance>=?",(new_balance,user_id,amount))
        if cur.rowcount!=1: conn.rollback(); return (False,balance)
        conn.execute("INSERT INTO balance_logs(user_id,admin_id,action,amount,old_balance,new_balance,reason) VALUES(?,?,?,?,?,?,?)",(user_id,ADMIN_ID,'sub',amount,balance,new_balance,reason))
        conn.commit(); return (True,new_balance)
    except Exception:
        conn.rollback(); raise
    finally: conn.close()

async def _refund_user(user_id, amount, reason):
    conn=db_connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row=conn.execute("SELECT balance FROM users WHERE user_id=?",(user_id,)).fetchone(); old=int(row[0] or 0) if row else 0; new=old+int(amount)
        conn.execute("UPDATE users SET balance=? WHERE user_id=?",(new,user_id))
        conn.execute("INSERT INTO balance_logs(user_id,admin_id,action,amount,old_balance,new_balance,reason) VALUES(?,?,?,?,?,?,?)",(user_id,ADMIN_ID,'refund',int(amount),old,new,reason)); conn.commit()
    finally: conn.close()








async def start_add_traffic(query, context, sub_id):
    user_id = query.from_user.id
    row = await _get_subscription_for_user(user_id, sub_id)
    if not row:
        await safe_query_answer(query, "❌ اشتراک پیدا نشد.", show_alert=True)
        return
    if row[5] not in ('completed', 'expired') or not row[7]:
        await safe_query_answer(query, "❌ اشتراک قابل استفاده نیست.", show_alert=True)
        return

    # Read the price directly from service_catalog on every click so an admin
    # price change is immediately effective and never depends on stale config.
    gb_price = (get_custom_subscription_rates(sub_id)[0] if row[1]=='custom' else get_service_extra_gb_price(row[1]))
    if gb_price <= 0:
        await safe_query_answer(
            query,
            "❌ قیمت حجم اضافه برای این سرویس تنظیم نشده است.",
            show_alert=True,
        )
        return

    begin_input_state(context, 'renewal_sub_id')
    context.user_data['renewal_sub_id'] = int(sub_id)
    context.user_data['waiting_for_extra_traffic'] = True
    await safe_edit_message(
        query,
        f"📦 <b>خرید حجم اضافه</b>\n\n"
        f"💰 قیمت هر ۱ گیگ: <b>{gb_price:,} تومان</b>\n\n"
        "حجم دلخواه را فقط به صورت عدد وارد کنید.\n"
        "مثال: <code>5</code> یا <code>10</code> (هر عدد = گیگابایت)",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🔙 انصراف", callback_data=f"my_subscription_detail_{sub_id}")]
        ]),
        parse_mode='HTML',
    )



async def wallet_admin_home(query):
    if query.from_user.id != ADMIN_ID:
        await safe_query_answer(query, "دسترسی ندارید.", show_alert=True); return
    conn=db_connect(); rows=conn.execute("SELECT id,token_name,network,rate_toman,active FROM wallet_tokens ORDER BY id DESC").fetchall(); conn.close()
    kb=[[InlineKeyboardButton(f"{'🟢' if r['active'] else '⚪'} {r['token_name']} · {r['network']} · {r['rate_toman']:g}ت",callback_data=f"wallet_admin_toggle_{r['id']}")] for r in rows]
    kb += [[InlineKeyboardButton("➕ افزودن ولت",callback_data="wallet_admin_add")],
           [InlineKeyboardButton("🔙 تنظیمات ربات",callback_data="admin_settings")]]
    await safe_edit_message(query,"💎 <b>مدیریت ولت‌ها</b>\n\nهر ولت شامل نام توکن، شبکه، آدرس و نرخ هر توکن به تومان است.",reply_markup=InlineKeyboardMarkup(kb),parse_mode="HTML")

async def wallet_admin_add_start(query, context):
    if query.from_user.id != ADMIN_ID: return
    begin_input_state(context); context.user_data['wallet_admin_stage']='name'; context.user_data['wallet_admin_draft']={}
    await safe_edit_message(query,"نام توکن را وارد کن؛ مثال: USDT")

async def wallet_user_home(query):
    conn=db_connect(); rows=conn.execute("SELECT id,token_name,network,rate_toman FROM wallet_tokens WHERE active=1 ORDER BY token_name").fetchall(); conn.close()
    kb=[[InlineKeyboardButton(f"{r['token_name']} · {r['network']}",callback_data=f"wallet_user_token_{r['id']}")] for r in rows]
    kb += [[InlineKeyboardButton("‹ برگشت به شارژ حساب",callback_data="charge")]]
    await safe_edit_message(query,"💎 <b>افزایش موجودی با ولت</b>\n\nتوکن و شبکه موردنظر را انتخاب کن:",reply_markup=InlineKeyboardMarkup(kb),parse_mode="HTML")

async def wallet_user_token(query, context, token_id):
    conn=db_connect(); r=conn.execute("SELECT * FROM wallet_tokens WHERE id=? AND active=1",(token_id,)).fetchone(); conn.close()
    if not r:
        await safe_query_answer(query,"این ولت دیگر فعال نیست.",show_alert=True); return
    context.user_data['wallet_deposit_token']=dict(r); context.user_data['wallet_deposit_stage']='amount'
    await safe_edit_message(query,
        f"💎 <b>{html.escape(r['token_name'])} · {html.escape(r['network'])}</b>\n\n"
        f"📥 آدرس دریافت:\n<code>{html.escape(r['wallet_address'])}</code>\n"
        f"💱 نرخ فعلی: <b>{float(r['rate_toman']):g} تومان</b> برای هر توکن\n\n"
        "مقدار توکنی را که انتقال داده‌ای به‌صورت عدد ارسال کن. سپس لینک تراکنش یا عکس رسید را بفرست.",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("‹ انتخاب توکن دیگر",callback_data="charge_wallet")]]),parse_mode="HTML")

async def submit_wallet_proof(update, context, proof_text=None, proof_file_id=None):
    uid=update.effective_user.id; token=context.user_data.get('wallet_deposit_token')
    if not token or context.user_data.get('wallet_deposit_stage')!='proof': return False
    amount=float(context.user_data.get('wallet_deposit_amount') or 0)
    if amount<=0: return False
    toman=int(round(amount*float(token['rate_toman'])))
    conn=db_connect()
    cur=conn.execute("""INSERT INTO wallet_deposits(user_id,token_id,token_name,network,wallet_address,token_amount,rate_toman,toman_amount,proof_text,proof_file_id)
        VALUES(?,?,?,?,?,?,?,?,?,?)""",(uid,token['id'],token['token_name'],token['network'],token['wallet_address'],amount,float(token['rate_toman']),toman,proof_text,proof_file_id))
    did=cur.lastrowid; conn.commit(); conn.close()
    user=update.effective_user
    txt=(f"💎 <b>درخواست شارژ با ولت #{did}</b>\n👤 کاربر: {html.escape(user.full_name or '')} (@{html.escape(user.username or 'ندارد')})\n🆔 <code>{uid}</code>\n🪙 توکن: <b>{html.escape(token['token_name'])}</b> · {html.escape(token['network'])}\n🔢 مقدار: <b>{amount:g}</b>\n💱 نرخ: {float(token['rate_toman']):g} تومان\n💰 معادل: <b>{toman:,} تومان</b>\n📥 آدرس: <code>{html.escape(token['wallet_address'])}</code>\n")
    if proof_text: txt += f"\n🔗 لینک/متن ارسالی:\n{html.escape(proof_text)}"
    kb=InlineKeyboardMarkup([[InlineKeyboardButton("✅ تأیید و افزایش موجودی",callback_data=f"wallet_dep_approve_{did}"),InlineKeyboardButton("❌ رد",callback_data=f"wallet_dep_reject_{did}")]])
    try:
        await context.bot.send_message(ADMIN_ID,txt,parse_mode="HTML",reply_markup=kb)
        if proof_file_id: await context.bot.send_photo(ADMIN_ID,proof_file_id,caption=f"رسید درخواست #{did}")
    except Exception as e: print("Wallet deposit admin notify failed:",e)
    context.user_data.pop('wallet_deposit_stage',None); context.user_data.pop('wallet_deposit_token',None); context.user_data.pop('wallet_deposit_amount',None)
    await update.message.reply_text(f"✅ درخواستت ثبت شد و بعد از بررسی ادمین نتیجه اعلام می‌شود.\n💰 معادل تقریبی: {toman:,} تومان")
    return True

async def buttons(update: Update, context: ContextTypes.DEFAULT_TYPE, _query_override=None):
    query = _query_override or update.callback_query
    await safe_query_answer(query)
    # انقضای اشتراک فقط در صفحات مرتبط/تمدید و هنگام startup بررسی می‌شود؛
    # اجرای UPDATE روی هر کلیک منویی باعث کندی غیرضروری ربات می‌شد.

    # هر کلیک منویی یک intent جدید است؛ stateهای ورودی قبلی نباید به منوی جدید نشت کنند.
    # user_id باید قبل از هر شاخه‌ای که دسترسی ادمین را بررسی می‌کند مقداردهی شود.
    user_id = query.from_user.id
    data = query.data or ""
    if data == "admin_backup_db":
        if user_id != ADMIN_ID:
            await safe_query_answer(query, "❌ شما دسترسی ندارید.", show_alert=True)
            return
        if not os.path.isfile(DB_PATH):
            await safe_query_answer(query, "❌ فایل دیتابیس پیدا نشد.", show_alert=True)
            return
        backup_path = None
        try:
            # SQLite backup API creates a consistent snapshot even if the bot is writing concurrently.
            fd, backup_path = tempfile.mkstemp(prefix="shadow_v2ray_db_backup_", suffix=".db")
            os.close(fd)
            with sqlite3.connect(DB_PATH, timeout=30) as source_conn:
                with sqlite3.connect(backup_path, timeout=30) as backup_conn:
                    source_conn.backup(backup_conn)
            stamp = datetime.now(IRAN_TZ).strftime("%Y-%m-%d_%H-%M-%S")
            filename = f"Shadow_V2ray_database_backup_{stamp}.db"
            with open(backup_path, "rb") as backup_file:
                await context.bot.send_document(
                    chat_id=user_id,
                    document=backup_file,
                    filename=filename,
                    caption=(
                        "✅ بکاپ دیتابیس ربات Shadow_V2ray\n"
                        f"🕒 زمان تهیه: {stamp}\n"
                        "این فایل شامل اطلاعات ذخیره‌شده در دیتابیس است؛ فایل‌های رسید و کد برنامه داخل آن نیستند."
                    ),
                )
        except Exception as exc:
            logger.exception("Database backup send failed") if "logger" in globals() else print(f"Database backup send failed: {exc}")
            try:
                await context.bot.send_message(
                    chat_id=user_id,
                    text="❌ تهیه یا ارسال بکاپ ناموفق بود. لاگ ربات را بررسی کنید."
                )
            except Exception:
                pass
        finally:
            if backup_path and os.path.exists(backup_path):
                try:
                    os.remove(backup_path)
                except OSError:
                    pass
        return
    if data in ("admin_card_number","admin_card_holder"):
        if user_id != ADMIN_ID: return
        context.user_data["admin_setting_input"]="card_number" if data=="admin_card_number" else "card_holder"
        await safe_edit_message(query, "شماره کارت جدید را ارسال کن:" if data=="admin_card_number" else "نام صاحب کارت را ارسال کن:", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("لغو",callback_data="admin_settings")]])); return
    if data == "wallet_admin_home": await wallet_admin_home(query); return
    if data == "wallet_admin_add": await wallet_admin_add_start(query, context); return
    if data == "charge_wallet": await wallet_user_home(query); return
    if data.startswith("wallet_user_token_"):
        await wallet_user_token(query, context, int(data.rsplit("_",1)[1])); return
    if data.startswith("wallet_admin_toggle_"):
        if user_id != ADMIN_ID: return
        tid=int(data.rsplit("_",1)[1]); conn=db_connect(); conn.execute("UPDATE wallet_tokens SET active=1-active WHERE id=?",(tid,)); conn.commit(); conn.close(); await wallet_admin_home(query); return
    if data.startswith("wallet_dep_approve_") or data.startswith("wallet_dep_reject_"):
        if user_id != ADMIN_ID: await safe_query_answer(query,"دسترسی ندارید.",show_alert=True); return
        approve=data.startswith("wallet_dep_approve_"); did=int(data.rsplit("_",1)[1]); conn=db_connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            dep=conn.execute("SELECT * FROM wallet_deposits WHERE id=?",(did,)).fetchone()
            if not dep or dep['status']!='pending':
                conn.rollback(); await safe_query_answer(query,"این درخواست قبلاً بررسی شده یا وجود ندارد.",show_alert=True); return
            if approve:
                conn.execute("UPDATE users SET balance=balance+? WHERE user_id=?",(int(dep['toman_amount']),int(dep['user_id'])))
                conn.execute("INSERT INTO balance_logs(user_id,admin_id,action,amount,old_balance,new_balance,reason) SELECT user_id,?,'add',?,balance-?,balance,? FROM users WHERE user_id=?",
                    (int(user_id),int(dep['toman_amount']),int(dep['toman_amount']),f"شارژ ولت #{did}",int(dep['user_id'])))
                status='approved'
            else: status='rejected'
            conn.execute("UPDATE wallet_deposits SET status=?,processed_by=?,processed_at=CURRENT_TIMESTAMP WHERE id=?",(status,user_id,did))
            conn.commit()
        except Exception: conn.rollback(); raise
        finally: conn.close()
        await safe_edit_message(query, f"{'✅ درخواست تأیید شد و موجودی افزایش یافت.' if approve else '❌ درخواست رد شد.'}\nدرخواست #{did}")
        try: await context.bot.send_message(int(dep['user_id']), f"{'✅ شارژ ولت تأیید شد.' if approve else '❌ درخواست شارژ ولت رد شد.'}\n💰 مبلغ: {int(dep['toman_amount']):,} تومان")
        except Exception: pass
        return
    # سامانه قدیمی اشتراک/X-UI کاملاً از مسیرهای قابل استفاده خارج شده است.
    removed_prefixes = (
        "admin_xui", "admin_settings_xui", "admin_sub_", "admin_subscriptions", "admin_pending_subscriptions",
        "admin_renewal", "admin_renewals", "admin_services", "admin_service_", "admin_plan_", "admin_toggle_inbound_", "admin_pending_subscriptions",
        "admin_custom_plan", "admin_custom_country", "admin_custom_ib_toggle", "admin_xui_inbound_", "admin_xui_reset_", "admin_xui_delete_", "admin_xui_toggle_", "admin_user_subs_", "admin_user_subs_page_",
        "my_subscription_detail_", "my_subscriptions_page_", "subscription_qr_", "user_delete_subscription_", "renew_subscription_", "renew_confirm_", "renew_full_",
        "renew_days_", "add_traffic_", "custom_start", "custom_country_toggle_", "custom_countries_done",
        "custom_month_", "custom_change_months", "custom_cancel", "custom_final_confirm", "custom_discount_apply", "custom_", "service_", "cdn_", "discount_apply", "service_volume_", "confirm_service_",
        "final_service_", "cdn_volume_", "confirm_cdn_", "cdn_final_"
    )
    if data.startswith(removed_prefixes) or data in ("admin_subscriptions", "admin_renewals", "admin_custom_plan", "admin_xui_inbounds"):
        await safe_query_answer(query, "این بخش از ربات حذف شده است. از فروشگاه محصولات استفاده کنید.", show_alert=True)
        return
    if await product_callback_router(query, context, data):
        return
    if data.startswith(("admin_services", "admin_service_", "admin_plan_", "admin_toggle_inbound_")):
        if user_id != ADMIN_ID:
            await safe_query_answer(query, "❌ شما دسترسی ندارید.", show_alert=True)
            return
        clear_input_states(context)

    # انتخاب/حذف Inbound باید قبل از شاخه admin_service_ بررسی شود،
    # چون callback آن با admin_toggle_inbound_ شروع می‌شود.
    if data.startswith("admin_toggle_inbound_"):
        try:
            parts = data.split("_")
            if len(parts) != 5:
                raise ValueError("invalid callback")
            sid = int(parts[3])
            iid = int(parts[4])
        except (ValueError, IndexError):
            await safe_query_answer(query, "❌ شناسه Inbound نامعتبر است.", show_alert=True)
            return

        conn = db_connect()
        try:
            exists = conn.execute(
                "SELECT 1 FROM service_inbounds WHERE service_id=? AND inbound_id=?",
                (sid, iid)
            ).fetchone()
            if exists:
                conn.execute(
                    "DELETE FROM service_inbounds WHERE service_id=? AND inbound_id=?",
                    (sid, iid)
                )
                selected = False
            else:
                conn.execute(
                    "INSERT INTO service_inbounds(service_id,inbound_id) VALUES(?,?)",
                    (sid, iid)
                )
                selected = True
            conn.commit()
        except Exception as exc:
            conn.rollback()
            await safe_query_answer(query, f"❌ ذخیره انتخاب Inbound ناموفق بود: {str(exc)[:180]}", show_alert=True)
            return
        finally:
            conn.close()

        await safe_query_answer(query, "✅ Inbound انتخاب شد." if selected else "☑️ Inbound حذف شد.")
        await show_admin_service_inbounds(query, context, sid)
        return

    # مدیریت واقعی Inboundهای X-UI، جدا از انتخاب Inbound برای هر سرویس.
    if data == "admin_xui_inbounds":
        await show_admin_xui_inbounds(query, context); return
    if data.startswith("admin_xui_inbound_"):
        try: iid = int(data.rsplit("_", 1)[1])
        except (ValueError, IndexError):
            await safe_query_answer(query, "❌ شناسه Inbound نامعتبر است.", show_alert=True); return
        await show_admin_xui_inbound_detail(query, context, iid); return
    if data.startswith("admin_xui_toggle_"):
        try: iid = int(data.rsplit("_", 1)[1])
        except (ValueError, IndexError):
            await safe_query_answer(query, "❌ شناسه Inbound نامعتبر است.", show_alert=True); return
        try:
            inbound = await xui_get_inbound(iid)
            new_enable = not bool(inbound.get("enable", True))
            await xui_set_inbound_enable(iid, new_enable)
            await safe_query_answer(query, "✅ وضعیت Inbound تغییر کرد.")
            await show_admin_xui_inbound_detail(query, context, iid)
        except Exception as exc:
            await safe_query_answer(query, f"❌ تغییر وضعیت انجام نشد: {str(exc)[:220]}", show_alert=True)
        return
    if data.startswith("admin_xui_reset_"):
        try: iid = int(data.rsplit("_", 1)[1])
        except (ValueError, IndexError):
            await safe_query_answer(query, "❌ شناسه Inbound نامعتبر است.", show_alert=True); return
        try:
            await xui_reset_inbound_traffic(iid)
            await safe_query_answer(query, "✅ ترافیک Inbound ریست شد.")
            await show_admin_xui_inbound_detail(query, context, iid)
        except Exception as exc:
            await safe_query_answer(query, f"❌ ریست ترافیک انجام نشد: {str(exc)[:220]}", show_alert=True)
        return
    if data.startswith("admin_xui_delclients_confirm_"):
        try: iid = int(data.rsplit("_", 1)[1])
        except (ValueError, IndexError):
            await safe_query_answer(query, "❌ شناسه Inbound نامعتبر است.", show_alert=True); return
        await show_admin_xui_delete_clients_confirm(query, context, iid); return
    if data.startswith("admin_xui_delclients_"):
        try: iid = int(data.rsplit("_", 1)[1])
        except (ValueError, IndexError):
            await safe_query_answer(query, "❌ شناسه Inbound نامعتبر است.", show_alert=True); return
        try:
            result = await xui_delete_inbound_clients(iid)
            deleted = result.get("deleted") if isinstance(result, dict) else None
            msg = f"✅ همه کلاینت‌ها حذف شدند" + (f" ({deleted} مورد)" if deleted is not None else "") + "."
            await safe_query_answer(query, msg)
            await show_admin_xui_inbound_detail(query, context, iid)
        except Exception as exc:
            await safe_query_answer(query, f"❌ حذف کلاینت‌ها انجام نشد: {str(exc)[:220]}", show_alert=True)
        return
    if data.startswith("admin_xui_delete_confirm_"):
        try: iid = int(data.rsplit("_", 1)[1])
        except (ValueError, IndexError):
            await safe_query_answer(query, "❌ شناسه Inbound نامعتبر است.", show_alert=True); return
        await show_admin_xui_delete_confirm(query, context, iid); return
    if data.startswith("admin_xui_delete_"):
        try: iid = int(data.rsplit("_", 1)[1])
        except (ValueError, IndexError):
            await safe_query_answer(query, "❌ شناسه Inbound نامعتبر است.", show_alert=True); return
        try:
            await xui_delete_inbound(iid)
            # Inbound حذف‌شده نباید در انتخاب‌های سرویس باقی بماند.
            conn = db_connect()
            try:
                conn.execute("DELETE FROM service_inbounds WHERE inbound_id=?", (iid,))
                conn.commit()
            finally:
                conn.close()
            await safe_query_answer(query, "🗑 Inbound حذف شد.")
            await show_admin_xui_inbounds(query, context)
        except Exception as exc:
            await safe_query_answer(query, f"❌ حذف Inbound انجام نشد: {str(exc)[:220]}", show_alert=True)
        return

    # مدیریت پلن سفارشی (مجزا از پلن‌های ثابت)
    if data == "admin_custom_plan":
        if user_id != ADMIN_ID: await safe_query_answer(query,"دسترسی ندارید.",show_alert=True); return
        active,base,monthly,day=custom_plan_settings()
        conn=db_connect(); countries=conn.execute("SELECT id,title,gb_price,active FROM custom_countries ORDER BY id").fetchall(); conn.close()
        kb=[[InlineKeyboardButton("✏️ قیمت پایه هر گیگ",callback_data="admin_custom_base"),InlineKeyboardButton("✏️ هزینه ماهانه",callback_data="admin_custom_monthly")],
            [InlineKeyboardButton("✏️ هزینه هر روز تمدید",callback_data="admin_custom_day")],
            [InlineKeyboardButton("➕ افزودن کشور",callback_data="admin_custom_add_country")]]
        for cid,title,gp,ca in countries:
            kb.append([InlineKeyboardButton(f"{'✅' if ca else '🚫'} {title} • گیگی {int(gp):,}",callback_data=f"admin_custom_country_{cid}")])
        kb += [[InlineKeyboardButton("🚫 غیرفعال‌کردن پلن" if active else "✅ فعال‌کردن پلن",callback_data="admin_custom_toggle")],[InlineKeyboardButton("🔙 پنل مدیریت",callback_data="admin_panel")]]
        await safe_edit_message(query,f"🌍 <b>مدیریت پلن سفارشی</b>\n\nوضعیت: {'فعال' if active else 'غیرفعال'}\nقیمت پایه هر گیگ: {int(base):,} تومان\nقیمت ماهانه: {int(monthly):,} تومان\nهزینه روز تمدید: {int(day):,} تومان\n\nقیمت خرید = (پایه هر گیگ + مجموع قیمت گیگی کشورها) × حجم + (هزینه ماهانه × ماه‌ها)\nقیمت تمدید حجم = پایه + مجموع تعرفه گیگی کشورها.",reply_markup=InlineKeyboardMarkup(kb),parse_mode="HTML"); return
    if data == "admin_custom_toggle":
        if user_id!=ADMIN_ID: return
        conn=db_connect(); conn.execute("UPDATE custom_plan_settings SET active=CASE WHEN active=1 THEN 0 ELSE 1 END,updated_at=CURRENT_TIMESTAMP WHERE id=1"); conn.commit(); conn.close(); await show_admin_custom_page(query); return
    if data in ("admin_custom_base","admin_custom_monthly","admin_custom_day","admin_custom_add_country"):
        if user_id!=ADMIN_ID: return
        begin_input_state(context)
        states={"admin_custom_base":"waiting_custom_base","admin_custom_monthly":"waiting_custom_monthly","admin_custom_day":"waiting_custom_day","admin_custom_add_country":"waiting_custom_country_name"}
        context.user_data[states[data]]=True
        prompts={"admin_custom_base":"قیمت پایه هر گیگ را به تومان بفرستید:","admin_custom_monthly":"هزینه هر ماه اشتراک را به تومان بفرستید:","admin_custom_day":"هزینه هر روز تمدید را به تومان بفرستید:","admin_custom_add_country":"نام کشور/لوکیشن را ارسال کنید:"}
        await safe_edit_message(query,prompts[data],reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 انصراف",callback_data="admin_custom_plan")]])); return
    if re.fullmatch(r"admin_custom_country_[0-9]+", data):
        if user_id!=ADMIN_ID: return
        cid=int(data.rsplit('_',1)[1]); conn=db_connect(); row=conn.execute("SELECT title,gb_price,active FROM custom_countries WHERE id=?",(cid,)).fetchone(); conn.close()
        if not row: await safe_query_answer(query,"کشور پیدا نشد.",show_alert=True); return
        kb=[[InlineKeyboardButton("✏️ تغییر قیمت هر گیگ",callback_data=f"admin_custom_country_price_{cid}")],[InlineKeyboardButton("🌐 تنظیم Inboundها",callback_data=f"admin_custom_country_inbounds_{cid}")],[InlineKeyboardButton("🚫 غیرفعال" if row[2] else "✅ فعال",callback_data=f"admin_custom_country_toggle_{cid}")],[InlineKeyboardButton("🗑 حذف کشور",callback_data=f"admin_custom_country_delete_{cid}")],[InlineKeyboardButton("🔙 برگشت",callback_data="admin_custom_plan")]]
        await safe_edit_message(query,f"🌍 <b>{html.escape(str(row[0]))}</b>\nقیمت هر گیگ: {int(row[1]):,} تومان\nوضعیت: {'فعال' if row[2] else 'غیرفعال'}",reply_markup=InlineKeyboardMarkup(kb),parse_mode="HTML"); return
    if data.startswith("admin_custom_country_price_"):
        cid=int(data.rsplit('_',1)[1]); begin_input_state(context); context.user_data['waiting_custom_country_price']=True; context.user_data['custom_admin_country_id']=cid
        await safe_edit_message(query,"قیمت هر گیگ این کشور را به تومان ارسال کنید:",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 انصراف",callback_data=f"admin_custom_country_{cid}")]])); return
    if data.startswith("admin_custom_country_toggle_"):
        cid=int(data.rsplit('_',1)[1]); conn=db_connect(); conn.execute("UPDATE custom_countries SET active=CASE WHEN active=1 THEN 0 ELSE 1 END WHERE id=?",(cid,)); conn.commit(); conn.close(); await show_admin_custom_country(query,cid); return
    if data.startswith("admin_custom_country_delete_"):
        cid=int(data.rsplit('_',1)[1]); conn=db_connect(); conn.execute("DELETE FROM custom_countries WHERE id=?",(cid,)); conn.commit(); conn.close(); await show_admin_custom_page(query); return
    if data.startswith("admin_custom_country_inbounds_"):
        cid=int(data.rsplit('_',1)[1])
        try: opts=await xui_get_inbounds()
        except Exception as exc: await safe_query_answer(query,f"X-UI: {str(exc)[:120]}",show_alert=True); return
        conn=db_connect(); selected={int(r[0]) for r in conn.execute("SELECT inbound_id FROM custom_country_inbounds WHERE country_id=?",(cid,)).fetchall()}; conn.close()
        kb=[]
        for item in opts:
            iid=int(item.get('id')); title=item.get('remark') or item.get('tag') or f"Inbound {iid}"
            kb.append([InlineKeyboardButton(("✅ " if iid in selected else "⬜ ")+f"{title} • {item.get('protocol','')}:{item.get('port','')}",callback_data=f"admin_custom_ib_toggle_{cid}_{iid}")])
        kb += [[InlineKeyboardButton("🔄 بروزرسانی",callback_data=f"admin_custom_country_inbounds_{cid}")],[InlineKeyboardButton("🔙 کشور",callback_data=f"admin_custom_country_{cid}")]]
        await safe_edit_message(query,"برای هر کشور می‌توانید چند Inbound انتخاب کنید:",reply_markup=InlineKeyboardMarkup(kb)); return
    if data.startswith("admin_custom_ib_toggle_"):
        _,_,_,_,cid,iid=data.split('_'); cid=int(cid); iid=int(iid); conn=db_connect(); exists=conn.execute("SELECT 1 FROM custom_country_inbounds WHERE country_id=? AND inbound_id=?",(cid,iid)).fetchone()
        if exists: conn.execute("DELETE FROM custom_country_inbounds WHERE country_id=? AND inbound_id=?",(cid,iid))
        else: conn.execute("INSERT OR IGNORE INTO custom_country_inbounds(country_id,inbound_id) VALUES(?,?)",(cid,iid))
        conn.commit(); conn.close()
        # بازنمایی لیست، بدون محدودیت به یک Inbound برای کشور
        try: opts=await xui_get_inbounds()
        except Exception: opts=[]
        conn=db_connect(); selected={int(r[0]) for r in conn.execute("SELECT inbound_id FROM custom_country_inbounds WHERE country_id=?",(cid,)).fetchall()}; conn.close(); kb=[]
        for item in opts:
            iid2=int(item.get('id')); title=item.get('remark') or item.get('tag') or f"Inbound {iid2}"
            kb.append([InlineKeyboardButton(("✅ " if iid2 in selected else "⬜ ")+f"{title} • {item.get('protocol','')}:{item.get('port','')}",callback_data=f"admin_custom_ib_toggle_{cid}_{iid2}")])
        kb += [[InlineKeyboardButton("🔄 بروزرسانی",callback_data=f"admin_custom_country_inbounds_{cid}")],[InlineKeyboardButton("🔙 کشور",callback_data=f"admin_custom_country_{cid}")]]
        await safe_edit_message(query,"برای هر کشور می‌توانید چند Inbound انتخاب کنید:",reply_markup=InlineKeyboardMarkup(kb)); return
    if data == "custom_start": await show_custom_countries(query,context); return
    if data.startswith("custom_country_toggle_"):
        cid=int(data.rsplit('_',1)[1]); selected=list(context.user_data.get('custom_selected_countries') or [])
        if cid in selected: selected.remove(cid)
        else: selected.append(cid)
        context.user_data['custom_selected_countries']=selected; await render_custom_country_picker(query,context); return
    if data == "custom_countries_done": await show_custom_months(query,context); return
    if data.startswith("custom_month_"):
        context.user_data['custom_months']=int(data.rsplit('_',1)[1]); await show_custom_quote(query,context); return
    if data == "custom_change_months": await show_custom_month_picker(query,context); return
    if data == "custom_cancel":
        for k in list(context.user_data):
            if k.startswith('custom_') or k in ('discount_code','discount_price','discount_amount'): context.user_data.pop(k,None)
        context.user_data.pop('waiting_for_discount_code',None); context.user_data.pop('custom_waiting_volume',None); context.user_data.pop('custom_waiting_discount',None)
        await safe_edit_message(query,"خرید پلن سفارشی لغو شد.",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🛍 خرید اشتراک",callback_data="buy_subscription")]])); return
    if data == "custom_discount_apply":
        begin_input_state(context,"custom_selected_countries","custom_volume","custom_months","custom_quote_price","discount_service_type","discount_volume","discount_months")
        context.user_data['waiting_for_discount_code']=True; context.user_data['custom_waiting_discount']=True
        await safe_edit_message(query,"🎟 کد تخفیف را ارسال کنید:",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ انصراف",callback_data="custom_cancel")]])); return
    if data == "custom_confirm":
        countries=context.user_data.get('custom_selected_countries') or []
        volume=int(context.user_data.get('custom_volume') or 0); months=int(context.user_data.get('custom_months') or 0)
        base_price=int(context.user_data.get('custom_quote_price') or 0)
        price=int(context.user_data.get('discount_price') if context.user_data.get('discount_price') is not None else base_price)
        code=context.user_data.get('discount_code')
        if not countries or volume<1 or months<1 or base_price<=0:
            await safe_query_answer(query,"اطلاعات خرید ناقص است؛ از ابتدا انتخاب کنید.",show_alert=True); return
        await safe_edit_message(query,f"✅ <b>تأیید نهایی پلن سفارشی</b>\n\n🌍 تعداد کشور: {len(countries)}\n📦 حجم: {volume} گیگابایت\n⏳ مدت: {months} ماه\n💰 مبلغ اصلی: {base_price:,} تومان\n🎟 تخفیف: {int(context.user_data.get('discount_amount') or 0):,} تومان\n💵 مبلغ پرداختی: <b>{price:,} تومان</b>\n\nپس از تأیید، کانفیگ‌ها در X-UI ساخته می‌شوند.",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🚀 تأیید و ساخت کانفیگ",callback_data="custom_final_confirm")],[InlineKeyboardButton("🎟 اعمال کد تخفیف",callback_data="custom_discount_apply")],[InlineKeyboardButton("❌ انصراف",callback_data="custom_cancel")]]),parse_mode="HTML"); return
    if data == "custom_final_confirm": await finalize_custom_purchase(query,context); return

    # مدیریت داینامیک سرویس‌ها و پلن‌ها عمداً قبل از مسیرهای قدیمی قرار دارد.
    if data == "admin_services":
        await show_admin_services(query, context); return
    if data.startswith("admin_service_extra_day_"):
        sid=int(data.rsplit("_",1)[1]); begin_input_state(context); context.user_data["waiting_for_service_extra_day_price"]=True; context.user_data["service_admin_id"]=sid
        await safe_edit_message(query,"📅 <b>قیمت روز اضافه</b>\n\nقیمت هر ۱ روز اضافه را به تومان ارسال کنید.\nمثال: <code>3000</code>",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 انصراف",callback_data=f"admin_service_id_{sid}")]]),parse_mode="HTML"); return
    if data.startswith("admin_service_extra_gb_"):
        sid=int(data.rsplit("_",1)[1]); begin_input_state(context); context.user_data["waiting_for_service_extra_gb_price"]=True; context.user_data["service_admin_id"]=sid
        await safe_edit_message(query,"📦 <b>قیمت حجم اضافه</b>\n\nقیمت هر ۱ گیگابایت اضافه را به تومان ارسال کنید.\nمثال: <code>12000</code>",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 انصراف",callback_data=f"admin_service_id_{sid}")]]),parse_mode="HTML"); return
    if data.startswith("admin_service_"):
        if data == "admin_service_add":
            begin_input_state(context); context.user_data["waiting_for_service_create"] = True
            await safe_edit_message(query,"➕ <b>افزودن نوع اشتراک</b>\n\nفرمت: <code>CODE | عنوان</code>\nمثال: <code>dns | 🧭 DNS</code>\n\nCODE فقط حروف انگلیسی کوچک، عدد و _ باشد.",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 انصراف",callback_data="admin_services")]]),parse_mode="HTML"); return
        if data.startswith("admin_service_code_"):
            try: sid=int(data.rsplit("_",1)[1])
            except (ValueError, IndexError):
                await safe_query_answer(query, "❌ شناسه سرویس نامعتبر است.", show_alert=True); return
            conn=db_connect()
            try:
                row=conn.execute("SELECT code,title FROM service_catalog WHERE id=?",(sid,)).fetchone()
            finally: conn.close()
            if not row:
                await safe_query_answer(query,"❌ سرویس پیدا نشد.",show_alert=True); return
            begin_input_state(context); context.user_data["waiting_for_service_code_edit"] = True; context.user_data["service_admin_id"]=sid
            await safe_edit_message(query, f"🔑 <b>تغییر کد سرویس</b>\n\nکد فعلی: <code>{html.escape(str(row[0]))}</code>\n\nکد جدید را ارسال کنید.\nفقط حروف انگلیسی کوچک، عدد و _ و حداکثر 32 کاراکتر.\n\nتغییر کد، مراجع داخلی اشتراک‌ها و تمدیدها را هم هماهنگ می‌کند.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 انصراف",callback_data=f"admin_service_{row[0]}")]]), parse_mode="HTML"); return
        if data.startswith("admin_service_rename_"):
            try: sid=int(data.rsplit("_",1)[1])
            except (ValueError, IndexError):
                await safe_query_answer(query, "❌ شناسه سرویس نامعتبر است.", show_alert=True); return
            begin_input_state(context); context.user_data["waiting_for_service_rename"] = True; context.user_data["service_admin_id"]=sid
            await safe_edit_message(query,"✏️ عنوان جدید سرویس را ارسال کنید:",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 انصراف",callback_data="admin_services")]])); return
        if data.startswith("admin_service_inbounds_"):
            try: sid=int(data.rsplit("_",1)[1])
            except: await safe_query_answer(query,"❌ شناسه نامعتبر است.",show_alert=True); return
            await show_admin_service_inbounds(query,context,sid); return
        if data.startswith("admin_service_id_"):
            try: sid=int(data.rsplit("_",1)[1])
            except: return
            conn=db_connect(); row=conn.execute("SELECT code FROM service_catalog WHERE id=?",(sid,)).fetchone(); conn.close()
            await show_admin_service_plans(query,context,row[0] if row else ""); return
        if data.startswith("admin_service_delete_confirm_"):
            try: sid=int(data.rsplit("_",1)[1])
            except (ValueError, IndexError):
                await safe_query_answer(query,"❌ شناسه سرویس نامعتبر است.",show_alert=True); return
            conn=db_connect()
            try: row=conn.execute("SELECT code,title FROM service_catalog WHERE id=?",(sid,)).fetchone()
            finally: conn.close()
            if not row:
                await safe_query_answer(query,"❌ سرویس پیدا نشد.",show_alert=True); return
            code,title=row
            await safe_edit_message(query,f"⚠️ <b>حذف کامل سرویس</b>\n\n🧩 {html.escape(str(title))}\n🔑 کد: <code>{html.escape(str(code))}</code>\n\nتمام پلن‌ها و Inboundهای انتخاب‌شده حذف می‌شوند. اشتراک‌های قبلی کاربران حذف نمی‌شوند.\n\nآیا مطمئن هستید؟",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🗑 بله، حذف کامل",callback_data=f"admin_service_delete_{sid}"),InlineKeyboardButton("❌ انصراف",callback_data=f"admin_service_id_{sid}")]]),parse_mode="HTML"); return
        if data.startswith("admin_service_delete_"):
            try: sid=int(data.rsplit("_",1)[1])
            except (ValueError, IndexError):
                await safe_query_answer(query,"❌ شناسه سرویس نامعتبر است.",show_alert=True); return
            conn=db_connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                row=conn.execute("SELECT code,title FROM service_catalog WHERE id=?",(sid,)).fetchone()
                if not row:
                    conn.rollback(); await safe_query_answer(query,"❌ سرویس پیدا نشد.",show_alert=True); return
                code,title=row
                conn.execute("DELETE FROM service_inbounds WHERE service_id=?",(sid,))
                conn.execute("DELETE FROM service_code_aliases WHERE service_id=?",(sid,))
                conn.execute("DELETE FROM service_plans WHERE service_id=?",(sid,))
                conn.execute("DELETE FROM service_catalog WHERE id=?",(sid,))
                conn.commit()
            except Exception as exc:
                conn.rollback(); await safe_query_answer(query,f"❌ حذف انجام نشد: {str(exc)[:160]}",show_alert=True); return
            finally: conn.close()
            await show_admin_services(query,context)
            await safe_query_answer(query,f"✅ سرویس «{html.escape(str(title))}» حذف شد.",show_alert=True); return
        if data.startswith("admin_service_toggle_"):
            try: sid=int(data.rsplit("_",1)[1])
            except (ValueError, IndexError):
                await safe_query_answer(query, "❌ شناسه سرویس نامعتبر است.", show_alert=True); return
            conn=db_connect(); conn.execute("UPDATE service_catalog SET active=CASE WHEN active=1 THEN 0 ELSE 1 END WHERE id=?",(sid,)); conn.commit(); conn.close(); None; await show_admin_services(query,context); return
        code=resolve_service_type(data.replace("admin_service_",'',1)); await show_admin_service_plans(query,context,code); return
    if data.startswith("admin_plan_"):
        if user_id != ADMIN_ID: return
        if data.startswith("admin_plan_add_"):
            try: sid=int(data.rsplit("_",1)[1])
            except (ValueError, IndexError):
                await safe_query_answer(query, "❌ شناسه سرویس نامعتبر است.", show_alert=True); return
            begin_input_state(context); context.user_data["waiting_for_service_plan_create"]=True; context.user_data["service_admin_id"]=sid
            await safe_edit_message(query,"➕ <b>افزودن پلن</b>\n\nفرمت: <code>MONTHS VOLUME PRICE</code>\nمثال: <code>1 15G 45000</code>\nمثال: <code>3 50G 110000</code>",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 انصراف",callback_data="admin_services")]]),parse_mode="HTML"); return
        if data.startswith("admin_plan_delete_confirm_"):
            try: pid=int(data.rsplit("_",1)[1])
            except (ValueError, IndexError):
                await safe_query_answer(query, "❌ شناسه پلن نامعتبر است.", show_alert=True); return
            conn=db_connect()
            try:
                row=conn.execute("SELECT p.id,s.code,s.title,p.volume,p.price,p.active,p.duration_months FROM service_plans p JOIN service_catalog s ON s.id=p.service_id WHERE p.id=?",(pid,)).fetchone()
            finally:
                conn.close()
            if not row:
                await safe_query_answer(query,"❌ این گزینه قبلاً حذف شده یا وجود ندارد.",show_alert=True)
                return
            _pid,_code,_title,_volume,_price,_active,_duration=row
            await safe_edit_message(
                query,
                f"⚠️ <b>تأیید حذف گزینه</b>\n\n"
                f"🧩 سرویس: <b>{html.escape(str(_title))}</b>\n"
                f"📦 حجم: <b>{html.escape(str(_volume))}</b>\n"
                f"⏳ مدت: <b>{_duration} ماه</b>\n"
                f"💰 قیمت فعلی: <b>{int(_price):,}</b> تومان\n\n"
                "این عملیات گزینه را <b>واقعاً از فهرست مدیریت و خرید حذف</b> می‌کند.\n"
                "سوابق اشتراک‌های قبلی و تمدیدهای ثبت‌شده حذف نمی‌شوند.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("⚠️ بله، حذف شود",callback_data=f"admin_plan_delete_{pid}")],
                    [InlineKeyboardButton("🔙 انصراف",callback_data=f"admin_plan_{pid}")]
                ]),
                parse_mode="HTML"
            )
            return
        try: pid=int(data.rsplit("_",1)[1])
        except (ValueError, IndexError):
            await safe_query_answer(query, "❌ شناسه پلن نامعتبر است.", show_alert=True); return
        if data.startswith("admin_plan_price_"):
            begin_input_state(context); context.user_data["waiting_for_service_plan_price_edit"]=True; context.user_data["service_admin_plan_id"]=pid
            await safe_edit_message(query,"💰 <b>تغییر قیمت</b>\n\nفقط قیمت جدید را به صورت عدد ارسال کنید.\nمثال: <code>70000</code>",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 انصراف",callback_data=f"admin_plan_{pid}")]]),parse_mode="HTML"); return
        if data.startswith("admin_plan_volume_"):
            begin_input_state(context); context.user_data["waiting_for_service_plan_volume_edit"]=True; context.user_data["service_admin_plan_id"]=pid
            await safe_edit_message(query,"📦 <b>تغییر حجم</b>\n\nفقط حجم جدید را ارسال کنید.\nمثال: <code>20G</code>",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 انصراف",callback_data=f"admin_plan_{pid}")]]),parse_mode="HTML"); return
        # سازگاری با callback قدیمی: ویرایش همزمان حجم و قیمت
        if data.startswith("admin_plan_edit_"):
            begin_input_state(context); context.user_data["waiting_for_service_plan_edit"]=True; context.user_data["service_admin_plan_id"]=pid
            await safe_edit_message(query,"✏️ <b>ویرایش حجم و قیمت</b>\n\nفرمت: <code>VOLUME PRICE</code>\nمثال: <code>20G 70000</code>",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 انصراف",callback_data=f"admin_plan_{pid}")]]),parse_mode="HTML"); return
        if data.startswith("admin_plan_toggle_"):
            conn=db_connect(); conn.execute("UPDATE service_plans SET active=CASE WHEN active=1 THEN 0 ELSE 1 END, updated_at=CURRENT_TIMESTAMP WHERE id=?",(pid,)); conn.commit(); conn.close(); await show_admin_plan(query,context,pid); return
        if data.startswith("admin_plan_delete_"):
            conn=db_connect()
            svc_code=None
            try:
                conn.execute("BEGIN IMMEDIATE")
                row=conn.execute("SELECT p.service_id,p.volume,s.code,s.legacy_key FROM service_plans p JOIN service_catalog s ON s.id=p.service_id WHERE p.id=?",(pid,)).fetchone()
                if not row:
                    conn.rollback()
                    await safe_query_answer(query,"❌ این گزینه قبلاً حذف شده یا وجود ندارد.",show_alert=True)
                    return
                service_id,volume,svc_code,legacy_key=row
                cur=conn.execute("DELETE FROM service_plans WHERE id=?",(pid,))
                if cur.rowcount != 1:
                    conn.rollback()
                    await safe_query_answer(query,"❌ حذف گزینه انجام نشد.",show_alert=True)
                    return
                if legacy_key == "cdn":
                    conn.execute("DELETE FROM cdn_prices WHERE volume=?",(volume,))
                elif legacy_key == "tunnel":
                    conn.execute("DELETE FROM tunnel_prices WHERE volume=?",(volume,))
                conn.commit()
            except Exception as exc:
                try: conn.rollback()
                except Exception: pass
                print(f"Service plan delete error (plan={pid}): {exc}")
                await safe_query_answer(query,"❌ حذف گزینه انجام نشد. جزئیات خطا در لاگ ثبت شد.",show_alert=True)
                return
            finally:
                conn.close()
            await safe_query_answer(query,"✅ گزینه واقعاً حذف شد.",show_alert=True)
            await show_admin_service_plans(query,context,svc_code or "")
            return
        await show_admin_plan(query,context,pid); return
    if data.startswith("renew_days_confirm_"):
        await process_renew_days_confirm(query,context,int(data.rsplit("_",1)[1])); return
    if data.startswith("renew_days_"):
        await start_renew_days(query,context,int(data.rsplit("_",1)[1])); return
    if data.startswith("add_traffic_"):
        await start_add_traffic(query,context,int(data.rsplit("_",1)[1])); return
    if data.startswith("renew_subscription_"):
        await renew_subscription(update,context,query,int(data.rsplit("_",1)[1])); return
    if data.startswith("renew_confirm_"):
        await process_renew_full_subscription(query,int(data.rsplit("_",1)[1])); return
    if data.startswith("renew_full_"):
        await renew_full_subscription(query,int(data.rsplit("_",1)[1])); return
    if data.startswith(("cdn_volume_", "confirm_cdn_", "cdn_final_", "service_volume_", "confirm_service_", "final_service_")) or data == "discount_apply" or (data.startswith("service_") and not data.startswith(("service_volume_", "confirm_service_", "final_service_"))):
        clear_input_states(context, keep=("discount_volume", "discount_months", "discount_code", "discount_price", "discount_amount", "discount_service_type"))
    elif data.startswith("renew_days_confirm_"):
        # تعداد روز انتخاب‌شده باید تا زمان تأیید نهایی باقی بماند.
        clear_input_states(context, keep=("renewal_days", "renewal_sub_id"))
    elif data.startswith(("wallet_back_confirm_", "wallet_apply_", "wallet_add_reason_")):
        clear_input_states(context, keep=("wallet_action", "wallet_target", "wallet_amount", "wallet_reason"))
    else:
        clear_input_states(context)

    # مسیرهای صفحه‌بندی و مدیریت تخفیف باید قبل از شاخه‌های قدیمی باشند.
    if user_id == ADMIN_ID:
        if query.data == "admin_payments" or query.data.startswith("admin_payments_page_"):
            page=int(query.data.rsplit("_",1)[1]) if query.data.startswith("admin_payments_page_") else 0
            await show_admin_payments_page(query, context, page); return
        if query.data == "admin_subscriptions" or query.data.startswith("admin_subscriptions_page_"):
            page=int(query.data.rsplit("_",1)[1]) if query.data.startswith("admin_subscriptions_page_") else 0
            await show_admin_subscriptions_page(query, context, page); return
        if query.data == "admin_users" or query.data.startswith("admin_users_page_"):
            page=int(query.data.rsplit("_",1)[1]) if query.data.startswith("admin_users_page_") else 0
            await show_admin_users_page(query, context, page); return
        if query.data == "admin_pending_payments" or query.data.startswith("admin_pending_payments_page_"):
            page=int(query.data.rsplit("_",1)[1]) if query.data.startswith("admin_pending_payments_page_") else 0
            await show_pending_payments_page(query, context, page); return
        if query.data == "admin_pending_subscriptions" or query.data.startswith("admin_pending_subscriptions_page_"):
            page=int(query.data.rsplit("_",1)[1]) if query.data.startswith("admin_pending_subscriptions_page_") else 0
            await show_pending_subscriptions_page(query, context, page); return
        if query.data == "admin_wallet" or query.data.startswith("admin_wallet_page_"):
            page=int(query.data.rsplit("_",1)[1]) if query.data.startswith("admin_wallet_page_") else 0
            await show_admin_wallet_page(query, context, page); return
        if query.data == "admin_discounts" or query.data.startswith("admin_discounts_page_"):
            page=int(query.data.rsplit("_",1)[1]) if query.data.startswith("admin_discounts_page_") else 0
            await show_discount_admin_page(query, page); return
        if query.data == "admin_settings":
            if user_id != ADMIN_ID:
                await safe_query_answer(query, "❌ شما دسترسی ندارید.", show_alert=True)
                return
            await show_admin_settings(query, context); return
        if query.data == "discount_create":
            begin_input_state(context); context.user_data["waiting_for_discount_create"] = True
            await safe_edit_message(query, 
                "➕ <b>ساخت کد تخفیف</b>\n\n"
                "فرمت کامل: <code>CODE TYPE VALUE MAX_PER_USER MAX_USES MIN_PURCHASE MAX_DISCOUNT</code>\n\n"
                "مثال درصدی: <code>SUMMER10 PERCENT 10 1 100 0 50000</code>\n"
                "مثال مبلغی: <code>VIP FIXED 50000 2 0 100000 50000</code>\n\n"
                "MAX_USES=0 نامحدود | MAX_PER_USER=0 نامحدود | MIN_PURCHASE=0 بدون حداقل | MAX_DISCOUNT=0 بدون سقف\n"
                "با ساخت نسخه جدید، نسخه فعال قبلی همان نام خودکار غیرفعال می‌شود و تاریخچه مصرف حفظ می‌شود.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ انصراف",callback_data="admin_discounts")]]),
                parse_mode="HTML"
            ); return
        if query.data.startswith("discount_toggle_"):
            try: did=int(query.data.rsplit("_",1)[1])
            except ValueError: return
            conn = db_connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute("SELECT code, active FROM discount_codes WHERE id=? AND deleted_at IS NULL", (did,)).fetchone()
                if not row:
                    conn.rollback()
                    await safe_query_answer(query, "❌ کد تخفیف پیدا نشد.", show_alert=True)
                    return
                code, is_active = row[0], int(row[1] or 0)
                if is_active:
                    conn.execute("UPDATE discount_codes SET active=0 WHERE id=?", (did,))
                else:
                    # قبل از فعال‌سازی، نسخه فعال قبلی همین نام را خاموش کن.
                    conn.execute("UPDATE discount_codes SET active=0 WHERE code=? AND deleted_at IS NULL AND active=1", (code,))
                    conn.execute("UPDATE discount_codes SET active=1 WHERE id=? AND deleted_at IS NULL", (did,))
                conn.commit()
            except sqlite3.IntegrityError:
                conn.rollback()
                await safe_query_answer(query, "❌ تغییر وضعیت کد انجام نشد.", show_alert=True)
                return
            except sqlite3.OperationalError:
                conn.rollback()
                await safe_query_answer(query, "❌ دیتابیس موقتاً مشغول است؛ دوباره تلاش کنید.", show_alert=True)
                return
            finally:
                conn.close()
            await show_discount_admin_page(query,0); return
        if query.data == "admin_settings_star_rate":
            await admin_settings_star_rate(update, context, query); return
        if query.data == "admin_settings_forced_join_toggle":
            new_value = "0" if forced_join_enabled() else "1"
            set_bot_setting("forced_join_enabled", new_value)
            await safe_query_answer(query, "🔄 وضعیت عضویت اجباری تغییر کرد.")
            await show_admin_settings(query, context)
            return
        if query.data == "admin_settings_button_style":
            global BUTTON_STYLE_MODE
            BUTTON_STYLE_MODE = "glass" if BUTTON_STYLE_MODE == "colorful" else "colorful"
            set_bot_setting("button_style_mode", BUTTON_STYLE_MODE)
            await safe_query_answer(query, "🎨 ظاهر دکمه‌ها تغییر کرد.")
            await show_admin_settings(query, context)
            return
        if query.data == "admin_settings_xui":
            await show_admin_xui_settings(query, context); return
        if query.data == "admin_settings_xui_test":
            await admin_settings_xui_test(query, context); return
        if query.data == "admin_settings_xui_url":
            await admin_settings_xui_field(update, context, query, "url"); return
        if query.data == "admin_settings_xui_username":
            await admin_settings_xui_field(update, context, query, "username"); return
        if query.data == "admin_settings_xui_password":
            await admin_settings_xui_field(update, context, query, "password"); return
        if query.data == "admin_settings_xui_token":
            await admin_settings_xui_field(update, context, query, "token"); return
        if query.data.startswith("admin_user_subs_page_"):
            parts=query.data.split("_")
            if len(parts)==6:
                await show_admin_user_subs_page(query,int(parts[4]),int(parts[5])); return
        if query.data.startswith("admin_user_deposits_page_"):
            parts=query.data.split("_")
            if len(parts)==6:
                await show_admin_user_deposits_page(query,int(parts[4]),int(parts[5])); return

    # =========================
    # اعمال کد تخفیف در خرید CDN
    # =========================
    if query.data == "discount_apply":
        volume = context.user_data.get("discount_volume")
        service_type = context.user_data.get("discount_service_type", "cdn")
        if not volume or service_type not in SERVICE_CONFIG:
            await safe_query_answer(query, "❌ ابتدا سرویس و حجم اشتراک را انتخاب کنید.", show_alert=True)
            return
        begin_input_state(context, "discount_volume", "discount_months", "discount_code", "discount_price", "discount_amount", "discount_service_type")
        context.user_data["waiting_for_discount_code"] = True
        await safe_edit_message(
            query,
            "🎟 <b>اعمال کد تخفیف</b>\n\nکد تخفیف خود را ارسال کنید:",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("❌ انصراف", callback_data="discount_cancel")
            ]]),
            parse_mode="HTML"
        )
        return

    # =========================
    # مسیرهای قطعی لیست‌های کاربری
    # =========================
    # این مسیرها باید قبل از شاخه‌های قدیمی پایین‌تر باشند؛
    # شاخه‌های قدیمی ممکن است لیست را به صورت یک متن بلند نمایش دهند.
    if query.data.startswith("my_deposits_page_"):
        try:
            page = max(0, int(query.data.rsplit("_", 1)[1]))
        except (ValueError, IndexError):
            page = 0
        await show_my_deposits_page(query, user_id, page)
        return

    if query.data == "my_deposits":
        await show_my_deposits_page(query, user_id, 0)
        return

    if query.data.startswith("my_deposit_detail_"):
        try:
            deposit_id = int(query.data.rsplit("_", 1)[1])
        except (ValueError, IndexError):
            await safe_query_answer(query, "❌ پرداخت نامعتبر است.", show_alert=True)
            return
        await show_my_deposit_detail(query, user_id, deposit_id)
        return

    if query.data.startswith("my_subscriptions_page_"):
        try:
            page = max(0, int(query.data.rsplit("_", 1)[1]))
        except (ValueError, IndexError):
            page = 0
        await show_my_subscriptions_page(query, user_id, page)
        return

    if query.data == "my_subscriptions":
        await show_my_subscriptions_page(query, user_id, 0)
        return

    if query.data.startswith("user_delete_subscription_"):
        try: sid=int(query.data.rsplit("_",1)[1])
        except ValueError: return
        conn=db_connect()
        try:
            cur=conn.execute("UPDATE subscriptions SET status='user_deleted' WHERE id=? AND user_id=? AND status<>'user_deleted'",(sid,user_id))
            conn.commit()
        finally: conn.close()
        if cur.rowcount:
            await safe_edit_message(query,"✅ سرویس از فهرست اشتراک‌های شما حذف شد.\nاین کار فقط فهرست ربات را تغییر می‌دهد و سرویس در X-UI حذف نمی‌شود.",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📦 اشتراک‌های من",callback_data="my_subscriptions")],[InlineKeyboardButton("🏠 منوی اصلی",callback_data="back_main")]]))
        else: await safe_query_answer(query,"❌ اشتراک پیدا نشد.",show_alert=True)
        return

    if query.data.startswith("discount_delete_"):
        if user_id != ADMIN_ID: return
        try: did=int(query.data.rsplit("_",1)[1])
        except ValueError: return
        conn=db_connect()
        try:
            conn.execute("UPDATE discount_codes SET active=0, deleted_at=CURRENT_TIMESTAMP WHERE id=? AND deleted_at IS NULL",(did,))
            conn.commit()
        finally: conn.close()
        await safe_query_answer(query,"🗑 کد تخفیف حذف شد.")
        await show_discount_admin_page(query,0); return

    if query.data.startswith("service_price_pct_"):
        if user_id != ADMIN_ID: return
        parts=query.data.split("_")
        if len(parts) != 5 or parts[3] not in ("up", "down"):
            await safe_query_answer(query,"❌ درخواست نامعتبر است.",show_alert=True); return
        try: sid=int(parts[4])
        except (ValueError,TypeError):
            await safe_query_answer(query,"❌ شناسه سرویس نامعتبر است.",show_alert=True); return
        begin_input_state(context)
        context.user_data["waiting_for_service_price_percentage"] = True
        context.user_data["service_price_percentage_service_id"] = sid
        context.user_data["service_price_percentage_direction"] = parts[3]
        action="افزایش" if parts[3] == "up" else "کاهش"
        limit="۱ تا ۱۰۰۰" if parts[3] == "up" else "۱ تا ۱۰۰"
        await safe_edit_message(query,
            f"🧮 <b>{action} درصدی قیمت پلن‌ها</b>\n\nدرصد موردنظر را به عدد ارسال کنید.\nمثال: <code>15</code> برای {action} ۱۵٪.\n\nمحدوده مجاز: {limit} درصد.\nاین تغییر روی تمام پلن‌های همین سرویس اعمال می‌شود.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ انصراف",callback_data=f"admin_service_id_{sid}")]]),parse_mode="HTML")
        return

    if query.data.startswith("my_subscription_detail_"):
        try:
            sub_id = int(query.data.rsplit("_", 1)[1])
        except (ValueError, IndexError):
            await safe_query_answer(query, "❌ اشتراک نامعتبر است.", show_alert=True)
            return
        await show_my_subscription_detail(query, user_id, sub_id)
        return

    # =========================
    # مدیریت کاربران
    # =========================

    if query.data == "admin_users":
        if user_id != ADMIN_ID:
            await safe_query_answer(query, 
                "❌ شما دسترسی ندارید.",
                show_alert=True
            )
            return
        conn = db_connect()
        cursor = conn.cursor()
        cursor.execute("""
                       SELECT user_id, balance, is_banned
                       FROM users
                       ORDER BY user_id DESC
                       """)
        users = cursor.fetchall()
        conn.close()
        if not users:
            keyboard = [
                [
                    InlineKeyboardButton(
                        "🔙 پنل مدیریت",
                        callback_data="admin_panel"
                    )
                ]
            ]
            await safe_edit_message(query, 
                "👥 مدیریت کاربران\n\n"
                "❌ هنوز هیچ کاربری در ربات ثبت نشده است.",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
            return
        keyboard = []
        for target_user_id, balance, is_banned in users:
            try:
                target_user = await context.bot.get_chat(
                    target_user_id
                )
                if target_user.username:
                    user_title = f"@{target_user.username}"
                else:
                    user_title = target_user.full_name
            except Exception:
                user_title = f"کاربر {target_user_id}"
            # ⬇️ اگه بن شده، علامت 🚫 بذار
            ban_mark = "🚫 " if is_banned else ""
            keyboard.append([
                InlineKeyboardButton(
                    f"{ban_mark}👤 {user_title} | 💰 {balance:,}",
                    callback_data=f"admin_user_{target_user_id}"
                )
            ])
        keyboard.append([
            InlineKeyboardButton(
                "🔄 بروزرسانی",
                callback_data="admin_users"
            )
        ])
        keyboard.append([
            InlineKeyboardButton(
                "🔍 جستجوی کاربر",
                callback_data="admin_search"
            )
        ])
        keyboard.append([
            InlineKeyboardButton(
                "⚙️ پنل مدیریت",
                callback_data="admin_panel"
            )
        ])
        await safe_edit_message(query, 
            f"👥 مدیریت کاربران\n\n"
            f"👤 تعداد کاربران: {len(users)}\n\n"
            "🚫 = کاربر مسدود است\n\n"
            "برای مشاهده اطلاعات هر کاربر، روی نام او بزنید:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    # =========================
    # 💰 مدیریت کیف پول - نسخه کامل
    # =========================
    elif query.data.startswith("admin_user_wallet_"):
        if user_id != ADMIN_ID:
            await safe_query_answer(query, "❌ شما دسترسی ندارید.", show_alert=True)
            return
        target_user_id = int(
            query.data.replace("admin_user_wallet_", "")
        )
        conn = db_connect()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT balance, is_banned FROM users WHERE user_id = ?",
            (target_user_id,)
        )
        result = cursor.fetchone()
        if not result:
            conn.close()
            await safe_query_answer(query, "❌ کاربر پیدا نشد.", show_alert=True)
            return
        balance, is_banned = result
        # آمار کلی
        cursor.execute(
            """
            SELECT COALESCE(SUM(amount), 0)
            FROM deposits
            WHERE user_id = ?
              AND status = 'approved'
            """,
            (target_user_id,)
        )
        total_deposits = cursor.fetchone()[0]
        cursor.execute(
            """
            SELECT COALESCE((SELECT SUM(price) FROM subscriptions WHERE user_id=? AND status='completed'),0) + COALESCE((SELECT SUM(amount) FROM subscription_renewals WHERE user_id=?),0)
            """,
            (target_user_id,target_user_id)
        )
        total_spent = cursor.fetchone()[0]
        cursor.execute(
            "SELECT COUNT(*) FROM balance_logs WHERE user_id = ?",
            (target_user_id,)
        )
        logs_count = cursor.fetchone()[0]
        conn.close()
        ban_status = "🚫 مسدود" if is_banned else "✅ فعال"
        keyboard = [
            [
                InlineKeyboardButton(
                    "➕ افزایش موجودی",
                    callback_data=f"wallet_add_{target_user_id}"
                )
            ],
            [
                InlineKeyboardButton(
                    "➖ کاهش موجودی",
                    callback_data=f"wallet_sub_{target_user_id}"
                )
            ],
            [
                InlineKeyboardButton(
                    "✏️ تنظیم موجودی",
                    callback_data=f"wallet_set_{target_user_id}"
                )
            ],
            [
                InlineKeyboardButton(
                    f"📜 تاریخچه ({logs_count})",
                    callback_data=f"wallet_logs_{target_user_id}_0"
                )
            ],
            [
                InlineKeyboardButton(
                    "🔙 برگشت به اطلاعات کاربر",
                    callback_data=f"admin_user_{target_user_id}"
                )
            ]
        ]
        await safe_edit_message(query, 
            "💰 <b>کیف پول من</b>\n\n"
            f"🆔 کاربر: <code>{target_user_id}</code>\n"
            f"📌 وضعیت حساب: {ban_status}\n\n"
            f"💵 موجودی فعلی: <b>{balance:,}</b> تومان\n"
            f"💳 مجموع واریزی تأیید شده: {total_deposits:,} تومان\n"
            f"🛒 مجموع خرید: {total_spent:,} تومان\n"
            f"📊 تعداد تراکنش‌ها: {logs_count}\n\n"
            "عملیات موردنظر را انتخاب کنید:",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="HTML"
        )

        # افزودن توضیح (قبل از wallet_add_ چون پیشوند مشترک داره)
    elif query.data.startswith("wallet_add_reason_"):
        if user_id != ADMIN_ID:
            await safe_query_answer(query, "❌ شما دسترسی ندارید.", show_alert=True)
            return
        target_user_id = int(
            query.data.replace("wallet_add_reason_", "")
        )
        begin_input_state(context, "wallet_action", "wallet_target", "wallet_amount", "wallet_reason"); context.user_data["waiting_for_wallet_reason"] = target_user_id
        keyboard = [
            [
                InlineKeyboardButton(
                    "↩️ بازگشت به تأیید",
                    callback_data=f"wallet_back_confirm_{target_user_id}"
                )
            ],
            [
                InlineKeyboardButton(
                    "❌ انصراف",
                    callback_data=f"admin_user_wallet_{target_user_id}"
                )
            ]
        ]
        current_reason = context.user_data.get("wallet_reason", "")
        reason_info = (
            f"\n📝 توضیح فعلی: {current_reason}"
            if current_reason else ""
        )
        await safe_edit_message(query, 
            "📝 <b>افزودن توضیح</b>\n\n"
            "لطفاً دلیل این تغییر موجودی را وارد کنید:\n\n"
            "💡 این توضیح در تاریخچه ثبت می‌شود "
            "و برای کاربر هم ارسال خواهد شد."
            f"{reason_info}",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="HTML"
        )


    # بازگشت به صفحه تأیید
    elif query.data.startswith("wallet_back_confirm_"):
        if user_id != ADMIN_ID:
            await safe_query_answer(query, "❌ شما دسترسی ندارید.", show_alert=True)
            return
        target_user_id = int(
            query.data.replace("wallet_back_confirm_", "")
        )
        context.user_data.pop("waiting_for_wallet_reason", None)
        await show_wallet_confirmation(
            query, context, target_user_id
        )

    # اعمال نهایی تغییر موجودی
    elif query.data.startswith("wallet_apply_"):
        if user_id != ADMIN_ID:
            await safe_query_answer(query, "❌ شما دسترسی ندارید.", show_alert=True)
            return
        target_user_id = int(
            query.data.replace("wallet_apply_", "")
        )
        wallet_action = context.user_data.get("wallet_action")
        amount = context.user_data.get("wallet_amount")
        reason = context.user_data.get("wallet_reason", "")
        if not wallet_action or not amount:
            await safe_query_answer(query, 
                "❌ اطلاعات منقضی شده. لطفاً دوباره تلاش کنید.",
                show_alert=True
            )
            clear_wallet_state(context)
            return
        conn = db_connect()
        cursor = conn.cursor()
        try:
            result = cursor.execute("SELECT balance FROM users WHERE user_id = ?", (target_user_id,)).fetchone()
            if not result:
                clear_wallet_state(context)
                await safe_query_answer(query, "❌ کاربر پیدا نشد.", show_alert=True)
                return
            old_balance = result[0]
            if wallet_action == "add":
                cursor.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (amount, target_user_id))
            elif wallet_action == "sub":
                cursor.execute("UPDATE users SET balance = balance - ? WHERE user_id = ? AND balance >= ?", (amount, target_user_id, amount))
                if cursor.rowcount != 1:
                    await safe_query_answer(query, "❌ موجودی کافی نیست.", show_alert=True)
                    return
            else:
                cursor.execute("UPDATE users SET balance = ? WHERE user_id = ?", (amount, target_user_id))
            new_balance = cursor.execute("SELECT balance FROM users WHERE user_id = ?", (target_user_id,)).fetchone()[0]
            cursor.execute("""INSERT INTO balance_logs
                (user_id, admin_id, action, amount, old_balance, new_balance, reason)
                VALUES (?, ?, ?, ?, ?, ?, ?)""", (target_user_id,user_id,wallet_action,amount,old_balance,new_balance,reason or None))
            conn.commit()
        except Exception as e:
            conn.rollback()
            print("Wallet apply error:", e)
            await safe_edit_message(query, 
            "❌ خطایی هنگام اعمال تغییر رخ داد."
            )
            return
        finally:
                conn.close()
                # پاک کردن state
        clear_wallet_state(context)
        action_titles = {
                "add": "➕ افزایش موجودی",
                "sub": "➖ کاهش موجودی",
                "set": "✏️ تنظیم موجودی",
            }
        keyboard = [
            [
                InlineKeyboardButton(
                    "💰 کیف پول کاربر",
                    callback_data=f"admin_user_wallet_{target_user_id}"
                )
            ],
            [
                InlineKeyboardButton(
                    "👤 اطلاعات کاربر",
                    callback_data=f"admin_user_{target_user_id}"
                )
            ],
            [
                InlineKeyboardButton(
                    "⚙️ پنل مدیریت",
                    callback_data="admin_panel"
                )
            ]
        ]
        reason_text = f"\n📝 توضیح: {reason}\n" if reason else ""
        await safe_edit_message(query, 
            f"✅ <b>موجودی با موفقیت تغییر کرد</b>\n\n"
            f"🔧 عملیات: {action_titles[wallet_action]}\n"
            f"💰 مبلغ: {amount:,} تومان\n"
            f"{reason_text}\n"
            f"💵 موجودی قبلی: {old_balance:,} تومان\n"
            f"💰 موجودی جدید: <b>{new_balance:,}</b> تومان",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="HTML"
        )

        # اطلاع به کاربر
        try:
            if wallet_action == "add":
                notify = (
                    "✅ مدیریت مبلغی به موجودی شما اضافه کرد.\n\n"
                    f"💰 مبلغ: +{amount:,} تومان\n"
                    f"💵 موجودی جدید: {new_balance:,} تومان"
                )
            elif wallet_action == "sub":
                notify = (
                    "⚠️ مدیریت مبلغی از موجودی شما کسر کرد.\n\n"
                    f"💸 مبلغ: -{amount:,} تومان\n"
                    f"💵 موجودی جدید: {new_balance:,} تومان"
                )
            else:
                diff = new_balance - old_balance

                if diff > 0:
                    diff_text = f"+{diff:,}"
                elif diff < 0:
                    diff_text = f"{diff:,}"
                else:
                    diff_text = "0"
                notify = (
                    "ℹ️ مدیریت موجودی شما را تنظیم کرد.\n\n"
                    f"💵 موجودی جدید: {new_balance:,} تومان\n"
                    f"📊 تغییر: {diff_text} تومان"
                )
            if reason:
                notify += f"\n\n📝 توضیح: {reason}"
            await context.bot.send_message(
                chat_id=target_user_id,
                text=notify
            )
        except Exception as e:
            print("User wallet notification error:", e)

    #

    # ➕ افزایش موجودی
    elif query.data.startswith("wallet_add_"):
        if user_id != ADMIN_ID:
            await safe_query_answer(query, "❌ شما دسترسی ندارید.", show_alert=True)
            return
        target_user_id = int(query.data.replace("wallet_add_", ""))
        conn = db_connect()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT balance FROM users WHERE user_id = ?",
            (target_user_id,)
        )
        result = cursor.fetchone()
        conn.close()
        if not result:
            await safe_query_answer(query, "❌ کاربر پیدا نشد.", show_alert=True)
            return
        balance = result[0]
        # تنظیم state
        begin_input_state(context); context.user_data["wallet_action"] = "add"
        context.user_data["wallet_target"] = target_user_id
        context.user_data.pop("wallet_amount", None)
        context.user_data.pop("wallet_reason", None)
        keyboard = [
            [
                InlineKeyboardButton(
                    "🔙 برگشت به کیف پول",
                    callback_data=f"admin_user_wallet_{target_user_id}"
                )
            ]
        ]
        await safe_edit_message(query, 
            "➕ <b>افزایش موجودی</b>\n\n"
            f"🆔 کاربر: <code>{target_user_id}</code>\n"
            f"💵 موجودی فعلی: {balance:,} تومان\n\n"
            "💰 مبلغ موردنظر را به تومان وارد کنید:\n\n"
            "مثال: <code>50000</code>",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="HTML"
        )


    #

    # ➖ کاهش موجودی
    elif query.data.startswith("wallet_sub_"):
        if user_id != ADMIN_ID:
            await safe_query_answer(query, "❌ شما دسترسی ندارید.", show_alert=True)
            return
        target_user_id = int(query.data.replace("wallet_sub_", ""))
        conn = db_connect()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT balance FROM users WHERE user_id = ?",
            (target_user_id,)
        )
        result = cursor.fetchone()
        conn.close()
        if not result:
            await safe_query_answer(query, "❌ کاربر پیدا نشد.", show_alert=True)
            return
        balance = result[0]
        begin_input_state(context); context.user_data["wallet_action"] = "sub"
        context.user_data["wallet_target"] = target_user_id
        context.user_data.pop("wallet_amount", None)
        context.user_data.pop("wallet_reason", None)
        keyboard = [
            [
                InlineKeyboardButton(
                    "🔙 برگشت به کیف پول",
                    callback_data=f"admin_user_wallet_{target_user_id}"
                )
            ]
        ]
        await safe_edit_message(query, 
            "➖ <b>کاهش موجودی</b>\n\n"
            f"🆔 کاربر: <code>{target_user_id}</code>\n"
            f"💵 موجودی فعلی: {balance:,} تومان\n"
            f"📉 حداکثر قابل کسر: {balance:,} تومان\n\n"
            "💰 مبلغ موردنظر برای کسر را به تومان وارد کنید:\n\n"
            "مثال: <code>20000</code>",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="HTML"
        )


    # ✏️ تنظیم موجودی
    elif query.data.startswith("wallet_set_"):
        if user_id != ADMIN_ID:
            await safe_query_answer(query, "❌ شما دسترسی ندارید.", show_alert=True)
            return
        target_user_id = int(query.data.replace("wallet_set_", ""))
        conn = db_connect()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT balance FROM users WHERE user_id = ?",
            (target_user_id,)
        )
        result = cursor.fetchone()
        conn.close()
        if not result:
            await safe_query_answer(query, "❌ کاربر پیدا نشد.", show_alert=True)
            return
        balance = result[0]
        begin_input_state(context); context.user_data["wallet_action"] = "set"
        context.user_data["wallet_target"] = target_user_id
        context.user_data.pop("wallet_amount", None)
        context.user_data.pop("wallet_reason", None)
        keyboard = [
            [
                InlineKeyboardButton(
                    "🔙 برگشت به کیف پول",
                    callback_data=f"admin_user_wallet_{target_user_id}"
                )
            ]
        ]
        await safe_edit_message(query, 
            "✏️ <b>تنظیم موجودی</b>\n\n"
            f"🆔 کاربر: <code>{target_user_id}</code>\n"
            f"💵 موجودی فعلی: {balance:,} تومان\n\n"
            "💰 موجودی جدید را به تومان وارد کنید:\n\n"
            "مثال: <code>100000</code>",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="HTML"
        )


    # تاریخچه با صفحه‌بندی
    elif query.data.startswith("wallet_logs_"):
        if user_id != ADMIN_ID:
            await safe_query_answer(query, "❌ شما دسترسی ندارید.", show_alert=True)
            return
        parts = query.data.split("_")
        # wallet_logs_{user_id}_{page}
        target_user_id = int(parts[2])
        page = int(parts[3]) if len(parts) > 3 else 0
        per_page = 10
        offset = page * per_page
        conn = db_connect()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT COUNT(*) FROM balance_logs WHERE user_id = ?",
            (target_user_id,)
        )
        total_logs = cursor.fetchone()[0]
        cursor.execute(
            """
            SELECT action, amount, old_balance, new_balance,
                   admin_id, reason, created_at
            FROM balance_logs
            WHERE user_id = ?
            ORDER BY id DESC
            LIMIT ? OFFSET ?
            """,
            (target_user_id, per_page, offset)
        )

        logs = cursor.fetchall()
        cursor.execute(
            "SELECT balance FROM users WHERE user_id = ?",
            (target_user_id,)
        )
        result = cursor.fetchone()
        current_balance = result[0] if result else 0
        conn.close()
        if not logs:
            keyboard = [
                [
                    InlineKeyboardButton(
                        "🔙 برگشت به کیف پول",
                        callback_data=f"admin_user_wallet_{target_user_id}"
                    )
                ]
            ]

            await safe_edit_message(query, 
                "📜 <b>تاریخچه تراکنش‌ها</b>\n\n"
                "❌ هیچ تراکنشی برای این کاربر ثبت نشده است.",
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="HTML"
            )

            return
        action_titles = {
            "add": ("➕", "افزایش"),
            "sub": ("➖", "کاهش"),
            "set": ("✏️", "تنظیم"),
        }
        total_pages = (total_logs + per_page - 1) // per_page
        text = (
            f"📜 <b>تاریخچه تراکنش‌های کاربر</b>\n\n"
            f"🆔 کاربر: <code>{target_user_id}</code>\n"
            f"💵 موجودی فعلی: {current_balance:,} تومان\n"
            f"📊 کل تراکنش‌ها: {total_logs}\n"
            f"📄 صفحه {page + 1} از {total_pages}\n\n"
        )
        for (
            action, amount, old_balance, new_balance,
            admin_id, reason, created_at
        ) in logs:
            emoji, action_title = action_titles.get(
                action, ("❓", action)
            )
            text += (
                f"{emoji} <b>{action_title}</b> {amount:,} تومان\n"
                f"├ {old_balance:,} ← {new_balance:,}\n"
                f"├ ادمین: <code>{admin_id}</code>\n"
                f"├ تاریخ: <code>{iran_time_text(created_at)}</code>\n"
            )
            if reason:
                text += f"└ 📝 {reason}\n"
            text += "\n"
        # دکمه‌های صفحه‌بندی
        keyboard = []
        nav_row = []
        if page > 0:
            nav_row.append(
                InlineKeyboardButton(
                    "◀️ قبلی",
                    callback_data=(
                        f"wallet_logs_{target_user_id}_{page - 1}"
                    )
                )
            )
        if offset + per_page < total_logs:
            nav_row.append(
                InlineKeyboardButton(
                    "▶️ بعدی",
                    callback_data=(
                        f"wallet_logs_{target_user_id}_{page + 1}"
                    )
                )
            )
        if nav_row:
            keyboard.append(nav_row)
        keyboard.append([
            InlineKeyboardButton(
                "🔙 برگشت به کیف پول",
                callback_data=f"admin_user_wallet_{target_user_id}"
            )
        ])
        await safe_edit_message(query, 
            text,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="HTML"
        )


    elif query.data.startswith("admin_user_") and not query.data.startswith(("admin_user_wallet_", "admin_user_subs_", "admin_user_deposits_")):

        if user_id != ADMIN_ID:
            await safe_query_answer(query, 
                "❌ شما دسترسی ندارید.",
                show_alert=True
            )
            return
        target_user_id = int(
            query.data.replace(
                "admin_user_",
                ""
            )
        )
        conn = db_connect()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT balance, is_banned FROM users WHERE user_id = ?",
            (target_user_id,)
        )

        result = cursor.fetchone()
        if not result:
            conn.close()
            await safe_query_answer(query, 
                "❌ کاربر پیدا نشد.",
                show_alert=True
            )
            return

        balance, is_banned = result
        cursor.execute(
            "SELECT COUNT(*) FROM deposits WHERE user_id = ?",
            (target_user_id,)
        )

        deposit_count = cursor.fetchone()[0]
        conn.close()

        try:
            target_user = await context.bot.get_chat(target_user_id)
            full_name = target_user.full_name
            username = (
                f"@{target_user.username}"
                if target_user.username
                else "ندارد"
            )


        except Exception:
            full_name = "نامشخص"
            username = "نامشخص"

        # وضعیت بن

        if is_banned:
            ban_status = "🚫 مسدود است"
            ban_button_text = "✅ رفع مسدودیت"
            ban_callback = f"user_unban_{target_user_id}"

        else:
            ban_status = "✅ فعال است"
            ban_button_text = "🚫 مسدود کردن کاربر"
            ban_callback = f"user_ban_{target_user_id}"

        keyboard = [
            [
                InlineKeyboardButton(
                    "💰 مدیریت موجودی",
                    callback_data=f"admin_user_wallet_{target_user_id}"
                )
            ],
            [
                InlineKeyboardButton("🛒 خریدهای محصول", callback_data=f"admin_user_products_{target_user_id}")
            ],
            [
                InlineKeyboardButton(
                    "💳 پرداخت‌ها",
                    callback_data=f"admin_user_deposits_page_{target_user_id}_0"
                )
            ],
            [
                InlineKeyboardButton(
                    "📨 ارسال پیام به کاربر",
                    callback_data=f"user_msg_{target_user_id}"
                )
            ],
            # دکمه پویا
            [
                InlineKeyboardButton(
                    ban_button_text,
                    callback_data=ban_callback
                )
            ],
            [
                InlineKeyboardButton(
                    "🔙 کاربران",

                    callback_data="admin_users"
                )
            ]
        ]

        await safe_edit_message(query, 
            "👤 اطلاعات کاربر\n\n"
            f"👤 نام: {full_name}\n"
            f"🔹 Username: {username}\n"
            f"🆔 ID: {target_user_id}\n\n"
            f"💰 موجودی: {balance:,} تومان\n"
            f"💳 تعداد پرداخت‌ها: {deposit_count}\n"
            f"📌 وضعیت حساب: {ban_status}",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )


# اشتراک های یک کاربر خاص
    elif query.data.startswith("admin_user_subs_"):
        if user_id != ADMIN_ID:
            await safe_query_answer(query, "❌ شما دسترسی ندارید.", show_alert=True)
            return
        target_user_id = int(
            query.data.replace("admin_user_subs_", "")
        )
        conn = db_connect()
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT id, service_type, volume, price,
                   duration_months, status,
                   subscription_text, subscription_media_type, subscription_media_id, created_at,
                   completed_at, expires_at
            FROM subscriptions
            WHERE user_id = ?
            ORDER BY id DESC
            LIMIT 20
            """,
            (target_user_id,)
        )
        subscriptions = cursor.fetchall()
        cursor.execute(
            """
            SELECT COUNT(*)
            FROM subscriptions
            WHERE user_id = ?
              AND status = 'completed'
              AND expires_at > ?
            """
            ,(target_user_id, iran_db_now_text())
        )
        active_count = cursor.fetchone()[0]
        cursor.execute(
            """
            SELECT COALESCE((SELECT SUM(price) FROM subscriptions WHERE user_id=? AND status='completed'),0) + COALESCE((SELECT SUM(amount) FROM subscription_renewals WHERE user_id=?),0)
            """,
            (target_user_id,target_user_id)
        )
        total_spent = cursor.fetchone()[0]
        conn.close()
        if not subscriptions:
            keyboard = [
                [
                    InlineKeyboardButton(
                        "🔙 برگشت به اطلاعات کاربر",
                        callback_data=f"admin_user_{target_user_id}"
                    )
                ]
            ]
            await safe_edit_message(query, 
                f"📦 اشتراک‌های کاربر <code>{target_user_id}</code>\n\n"
                f"✅ فعال: {active_count}\n"
                f"💰 مجموع خرید: {total_spent:,} تومان\n\n"
                "❌ این کاربر هنوز هیچ اشتراکی ندارد.",
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="HTML"
            )

            return
        now = iran_now()
        status_icons = {
            "pending": "⏳",
            "completed": "✅",
            "cancelled": "❌",
            "expired": "⌛",
        }
        text = (
            f"📦 اشتراک‌های کاربر <code>{target_user_id}</code>\n\n"
            f"✅ فعال: {active_count}\n"
            f"💰 مجموع خرید: {total_spent:,} تومان\n\n"
        )
        keyboard = []
        for (
                sub_id, service_type, volume, price,
                duration_months, status,
                subscription_text, subscription_media_type, subscription_media_id, created_at,
                completed_at, expires_at
        ) in subscriptions:
            icon = status_icons.get(status, "❓")
            # تشخیص وضعیت واقعی
            real_status = status
            if status == "completed" and expires_at:
                try:
                    expiry_date = datetime.strptime(
                        expires_at, "%Y-%m-%d %H:%M:%S"
                    )
                    if expiry_date <= now:
                        real_status = "expired"
                        icon = "⌛"
                except ValueError:
                    pass
            service_title = service_display_title(service_type)
            button_text = (
                f"{icon} #{sub_id} | {service_title} | "
                f"{volume} | {price:,} تومان"
            )
            keyboard.append([
                InlineKeyboardButton(
                    button_text,
                    callback_data=f"admin_sub_detail_{sub_id}"
                )
            ])
        keyboard.append([
            InlineKeyboardButton(
                "🔄 بروزرسانی",
                callback_data=f"admin_user_subs_page_{target_user_id}_0"
            )
        ])
        keyboard.append([
            InlineKeyboardButton(
                "🔙 برگشت به اطلاعات کاربر",
                callback_data=f"admin_user_{target_user_id}"
            )
        ])
        await safe_edit_message(query, 
            text,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="HTML"
        )


    #پرداخت های یک کاربر خاص

    elif query.data.startswith("admin_user_deposits_"):
        if user_id != ADMIN_ID:
            await safe_query_answer(query, "❌ شما دسترسی ندارید.", show_alert=True)
            return
        target_user_id = int(
            query.data.replace("admin_user_deposits_", "")
        )
        conn = db_connect()
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT id, amount, payment_method, status,
                   processed_by, created_at, processed_at
            FROM deposits
            WHERE user_id = ?
            ORDER BY id DESC
            LIMIT 20
            """,
            (target_user_id,)
        )
        deposits = cursor.fetchall()
        cursor.execute(
            """
            SELECT COALESCE(SUM(amount), 0)
            FROM deposits
            WHERE user_id = ?
              AND status = 'approved'
            """,
            (target_user_id,)
        )
        total_paid = cursor.fetchone()[0]
        cursor.execute(
            """
            SELECT COUNT(*)
            FROM deposits
            WHERE user_id = ?
              AND status = 'pending'
            """,
            (target_user_id,)
        )
        pending_count = cursor.fetchone()[0]
        conn.close()
        if not deposits:
            keyboard = [
                [
                    InlineKeyboardButton(
                        "🔙 برگشت به اطلاعات کاربر",
                        callback_data=f"admin_user_{target_user_id}"
                    )
                ]
            ]
            await safe_edit_message(query, 
                f"💳 پرداخت‌های کاربر <code>{target_user_id}</code>\n\n"
                f"💰 مجموع واریزی تأیید شده: {total_paid:,} تومان\n"
                f"⏳ در انتظار: {pending_count}\n\n"
                "❌ این کاربر هنوز هیچ پرداختی نداشته است.",
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="HTML"
            )

            return
        status_icons = {
            "pending": "⏳",
            "approved": "✅",
            "rejected": "❌",
        }
        method_titles = {
            "card": "💳 کارت به کارت",
            "ton": "💎 TON",
            "star": "⭐️ استار",
        }
        text = (
            f"💳 پرداخت‌های کاربر <code>{target_user_id}</code>\n\n"
            f"💰 مجموع واریزی تأیید شده: {total_paid:,} تومان\n"
            f"⏳ در انتظار: {pending_count}\n\n"
        )
        keyboard = []
        for (
                dep_id, amount, method, status,
                processed_by, created_at, processed_at
        ) in deposits:
            icon = status_icons.get(status, "❓")
            method_title = method_titles.get(method, method or "نامشخص")
            date_text = iran_time_text(created_at) if created_at else "نامشخص"
            button_text = (
                f"{icon} #{dep_id} | {method_title} | "
                f"💰 {amount:,} تومان | 📅 {date_text}"
            )
            keyboard.append([
                InlineKeyboardButton(
                    button_text,
                    callback_data=f"admin_deposit_detail_{dep_id}"
                )
            ])
        keyboard.append([
            InlineKeyboardButton(
                "🔄 بروزرسانی",
                callback_data=f"admin_user_deposits_page_{target_user_id}_0"
            )
        ])
        keyboard.append([
            InlineKeyboardButton(
                "🔙 برگشت به اطلاعات کاربر",
                callback_data=f"admin_user_{target_user_id}"
            )
        ])
        await safe_edit_message(query, 
            text,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="HTML"
        )



    # =========================
    # لغو سفارش اشتراک توسط ادمین
    # =========================
    elif query.data.startswith("admin_cancel_sub_"):
        if user_id != ADMIN_ID:
            await safe_query_answer(query, "❌ شما دسترسی ندارید.", show_alert=True)
            return
        try:
            sub_id = int(query.data.rsplit("_", 1)[1])
        except (ValueError, IndexError):
            await safe_query_answer(query, "❌ شماره اشتراک نامعتبر است.", show_alert=True)
            return

        conn = db_connect()
        try:
            cur = conn.cursor()
            cur.execute("BEGIN IMMEDIATE")
            sub = cur.execute(
                """SELECT user_id, service_type, volume, price, status, admin_message_id
                   FROM subscriptions WHERE id=?""",
                (sub_id,)
            ).fetchone()
            if not sub:
                conn.rollback()
                await safe_query_answer(query, "🔎 این اشتراک پیدا نشد.", show_alert=True)
                return
            target_user_id, service_type, volume, price, status, admin_message_id = sub
            if status != "pending":
                conn.rollback()
                await safe_query_answer(
                    query,
                    "⚠️ فقط سفارش‌های در انتظار ارسال قابل لغو هستند.",
                    show_alert=True
                )
                return

            # بازگرداندن دقیق مبلغ پرداخت‌شده و آزاد کردن مصرف تخفیف؛
            # هر دو در همان تراکنش انجام می‌شوند تا لغو دوباره یا همزمانی باعث
            # برگشت پول/ظرفیت مضاعف نشود.
            usage = cur.execute(
                "SELECT discount_id FROM discount_usages WHERE subscription_id=? LIMIT 1",
                (sub_id,)
            ).fetchone()
            cur.execute(
                """UPDATE subscriptions
                   SET status='cancelled',
                       cancelled_at=CURRENT_TIMESTAMP,
                       cancelled_by=?
                   WHERE id=? AND status='pending'""",
                (user_id, sub_id)
            )
            if cur.rowcount != 1:
                conn.rollback()
                await safe_query_answer(query, "⚠️ سفارش دیگر در انتظار نیست.", show_alert=True)
                return
            cur.execute("UPDATE users SET balance=balance+? WHERE user_id=?", (int(price), target_user_id))
            if usage:
                discount_id = int(usage[0])
                deleted_usage = cur.execute(
                    "DELETE FROM discount_usages WHERE subscription_id=?",
                    (sub_id,)
                ).rowcount
                if deleted_usage:
                    cur.execute(
                        "UPDATE discount_codes SET used_count=CASE WHEN used_count>0 THEN used_count-1 ELSE 0 END WHERE id=?",
                        (discount_id,)
                    )
            conn.commit()
        except Exception as e:
            conn.rollback()
            print("Admin subscription cancellation error:", e)
            await safe_query_answer(query, "❌ خطایی هنگام لغو اشتراک رخ داد.", show_alert=True)
            return
        finally:
            conn.close()

        try:
            await context.bot.send_message(
                chat_id=target_user_id,
                text=(
                    "🚫 <b>سفارش اشتراک شما توسط مدیریت لغو شد.</b>\n\n"
                    f"🔢 شماره سفارش: <code>{sub_id}</code>\n"
                    f"🌐 سرویس: {service_display_title(service_type)}\n"
                    f"📦 حجم: <code>{html.escape(str(volume))}</code>\n"
                    f"💰 مبلغ بازگشتی: <b>{int(price):,}</b> تومان\n\n"
                    "💵 مبلغ سفارش به موجودی شما بازگردانده شد.\n"
                    "اگر مایل باشید می‌توانید دوباره سفارش ثبت کنید."
                ),
                parse_mode="HTML"
            )
        except Exception as e:
            print("Cancelled subscription notification error:", e)

        if admin_message_id:
            try:
                await safe_bot_edit_text(
                    context.bot,
                    ADMIN_ID,
                    int(admin_message_id),
                    text=(
                        "🚫 <b>سفارش اشتراک لغو شد</b>\n\n"
                        f"🔢 شماره سفارش: <code>{sub_id}</code>\n"
                        f"👤 ID کاربر: <code>{target_user_id}</code>\n"
                        f"🌐 سرویس: {service_display_title(service_type)}\n"
                        f"📦 حجم: <code>{html.escape(str(volume))}</code>\n"
                        f"💰 مبلغ بازگشتی: {int(price):,} تومان\n\n"
                        "🚫 وضعیت: لغو شده توسط مدیریت"
                    ),
                    parse_mode="HTML"
                )
            except Exception as e:
                print("Admin order cancellation message update error:", e)

        await safe_query_answer(query, "✅ سفارش لغو شد و مبلغ به موجودی کاربر برگشت.", show_alert=True)
        await show_admin_subscriptions_page(query, context, 0)
        return

    # =========================
    # تأیید/رد تمدید اشتراک توسط ادمین
    # =========================
    elif query.data.startswith("admin_approve_renewal_"):
        if user_id != ADMIN_ID:
            await safe_query_answer(query, "❌ شما دسترسی ندارید.", show_alert=True)
            return
        try:
            sub_id = int(query.data.rsplit("_", 1)[1])
        except (ValueError, IndexError):
            await safe_query_answer(query, "❌ شماره اشتراک نامعتبر است.", show_alert=True)
            return

        conn = db_connect()
        try:
            cur = conn.cursor()
            cur.execute("BEGIN IMMEDIATE")
            sub = cur.execute(
                "SELECT user_id, service_type, volume, status, expires_at, admin_message_id, last_renewal_price FROM subscriptions WHERE id=?",
                (sub_id,)
            ).fetchone()
            if not sub:
                conn.rollback()
                await safe_query_answer(query, "🔎 این اشتراک پیدا نشد.", show_alert=True)
                return
            target_user_id, service_type, volume, status, expires_at, admin_message_id, last_renewal_price = sub
            if status != "renewal_pending":
                conn.rollback()
                await safe_query_answer(query, "⚠️ این اشتراک در انتظار تمدید نیست.", show_alert=True)
                return

            # آپدیت وضعیت به completed
            cur.execute(
                "UPDATE subscriptions SET status='completed' WHERE id=? AND status='renewal_pending'",
                (sub_id,)
            )
            if cur.rowcount != 1:
                conn.rollback()
                await safe_query_answer(query, "⚠️ اشتراک دیگر در انتظار تمدید نیست.", show_alert=True)
                return

            # آپدیت balance_logs برای نشان دادن تأیید
            cur.execute(
                "UPDATE balance_logs SET reason=? WHERE id = (SELECT id FROM balance_logs WHERE user_id=? AND action='sub' AND reason LIKE ? ORDER BY id DESC LIMIT 1)",
                (f'تمدید اشتراک #{sub_id} (تأیید شده)', target_user_id, f'تمدید اشتراک #{sub_id}%')
            )

            conn.commit()
        except Exception as e:
            conn.rollback()
            print("Approve renewal error:", e)
            await safe_query_answer(query, "❌ خطایی هنگام تأیید تمدید رخ داد.", show_alert=True)
            return
        finally:
            conn.close()

        # اطلاع به کاربر
        service_title = service_display_title(service_type)
        try:
            await context.bot.send_message(
                chat_id=target_user_id,
                text=(
                    "✅ <b>تمدید اشتراک شما تأیید شد!</b>\n\n"
                    f"🌐 سرویس: {service_title}\n"
                    f"📦 حجم: <code>{html.escape(str(volume))}</code>\n"
                    f"⏳ مدت: ۱ ماه اضافه شد\n"
                    f"💰 مبلغ: {int(last_renewal_price or 0):,} تومان\n\n"
                    "🎉 اشتراک شما با موفقیت تمدید شد و اکنون فعال است."
                ),
                parse_mode="HTML"
            )
        except Exception as e:
            print("User renewal approval notification error:", e)

        # آپدیت پیام ادمین
        if admin_message_id:
            try:
                await safe_bot_edit_text(
                    context.bot,
                    ADMIN_ID,
                    int(admin_message_id),
                    text=(
                        "✅ <b>تمدید اشتراک تأیید شد</b>\n\n"
                        f"🔢 شماره سفارش: <code>{sub_id}</code>\n"
                        f"👤 ID کاربر: <code>{target_user_id}</code>\n"
                        f"🌐 سرویس: {service_title}\n"
                        f"📦 حجم: <code>{html.escape(str(volume))}</code>\n"
                        f"💰 مبلغ: {int(last_renewal_price or 0):,} تومان\n\n"
                        "✅ وضعیت: تأیید و تمدید شده"
                    ),
                    parse_mode="HTML"
                )
            except Exception as e:
                print("Admin renewal approval message update error:", e)

        await safe_query_answer(query, "✅ تمدید تأیید و کاربر اطلاع‌رسانی شد.", show_alert=True)
        await show_admin_subscriptions_page(query, context, 0)
        return

    elif query.data.startswith("admin_reject_renewal_"):
        if user_id != ADMIN_ID:
            await safe_query_answer(query, "❌ شما دسترسی ندارید.", show_alert=True)
            return
        try:
            sub_id = int(query.data.rsplit("_", 1)[1])
        except (ValueError, IndexError):
            await safe_query_answer(query, "❌ شماره اشتراک نامعتبر است.", show_alert=True)
            return

        conn = db_connect()
        try:
            cur = conn.cursor()
            cur.execute("BEGIN IMMEDIATE")
            sub = cur.execute(
                "SELECT user_id, service_type, volume, status, expires_at, admin_message_id, last_renewal_price FROM subscriptions WHERE id=?",
                (sub_id,)
            ).fetchone()
            if not sub:
                conn.rollback()
                await safe_query_answer(query, "🔎 این اشتراک پیدا نشد.", show_alert=True)
                return
            target_user_id, service_type, volume, status, expires_at, admin_message_id, last_renewal_price = sub
            if status != "renewal_pending":
                conn.rollback()
                await safe_query_answer(query, "⚠️ این اشتراک در انتظار تمدید نیست.", show_alert=True)
                return

            amount = int(last_renewal_price or 0)

            # بازگرداندن مبلغ به کاربر
            cur.execute("UPDATE users SET balance=balance+? WHERE user_id=?", (amount, target_user_id))

            # برگرداندن وضعیت اشتراک به وضعیت قبلی (completed/expired)
            old_status = "expired" if expires_at else "completed"
            try:
                exp_dt = datetime.strptime(str(expires_at), "%Y-%m-%d %H:%M:%S") if expires_at else None
                if exp_dt and exp_dt > iran_now():
                    old_status = "completed"
                else:
                    old_status = "expired"
            except ValueError:
                old_status = "completed"

            cur.execute(
                "UPDATE subscriptions SET status=?, expires_at=?, renewal_count=COALESCE(renewal_count,0)-1 WHERE id=? AND status='renewal_pending'",
                (old_status, expires_at, sub_id)
            )
            if cur.rowcount != 1:
                conn.rollback()
                await safe_query_answer(query, "⚠️ اشتراک دیگر در انتظار تمدید نیست.", show_alert=True)
                return

            # آپدیت balance_logs
            cur.execute(
                "UPDATE balance_logs SET reason=? WHERE id = (SELECT id FROM balance_logs WHERE user_id=? AND action='sub' AND reason LIKE ? ORDER BY id DESC LIMIT 1)",
                (f'تمدید اشتراک #{sub_id} (رد شده و بازگشت وجه)', target_user_id, f'تمدید اشتراک #{sub_id}%')
            )

            conn.commit()
        except Exception as e:
            conn.rollback()
            print("Reject renewal error:", e)
            await safe_query_answer(query, "❌ خطایی هنگام رد تمدید رخ داد.", show_alert=True)
            return
        finally:
            conn.close()

        # اطلاع به کاربر
        service_title = service_display_title(service_type)
        try:
            await context.bot.send_message(
                chat_id=target_user_id,
                text=(
                    "❌ <b>درخواست تمدید اشتراک شما رد شد.</b>\n\n"
                    f"🌐 سرویس: {service_title}\n"
                    f"📦 حجم: <code>{html.escape(str(volume))}</code>\n"
                    f"💰 مبلغ بازگشتی: <b>{amount:,}</b> تومان\n\n"
                    "💵 مبلغ کسر شده به موجودی شما بازگردانده شد.\n"
                    "می‌توانید مجدداً درخواست تمدید دهید."
                ),
                parse_mode="HTML"
            )
        except Exception as e:
            print("User renewal rejection notification error:", e)

        # آپدیت پیام ادمین
        if admin_message_id:
            try:
                await safe_bot_edit_text(
                    context.bot,
                    ADMIN_ID,
                    int(admin_message_id),
                    text=(
                        "❌ <b>تمدید اشتراک رد شد</b>\n\n"
                        f"🔢 شماره سفارش: <code>{sub_id}</code>\n"
                        f"👤 ID کاربر: <code>{target_user_id}</code>\n"
                        f"🌐 سرویس: {service_title}\n"
                        f"📦 حجم: <code>{html.escape(str(volume))}</code>\n"
                        f"💰 مبلغ بازگشتی: {amount:,} تومان\n\n"
                        "❌ وضعیت: رد شده و مبلغ برگشت داده شد"
                    ),
                    parse_mode="HTML"
                )
            except Exception as e:
                print("Admin renewal rejection message update error:", e)

        await safe_query_answer(query, "❌ تمدید رد و مبلغ به کاربر برگشت.", show_alert=True)
        await show_admin_subscriptions_page(query, context, 0)
        return

    #جزئیات یک اشتراک خاص

    elif query.data.startswith("admin_sub_detail_"):
        if user_id != ADMIN_ID:
            await safe_query_answer(query, "❌ شما دسترسی ندارید.", show_alert=True)
            return
        sub_id = int(
            query.data.replace("admin_sub_detail_", "")
        )
        conn = db_connect()
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT user_id, service_type, volume, price,
                   duration_months, status,
                   subscription_text, subscription_media_type, subscription_media_id, created_at,
                   completed_at, expires_at, admin_message_id, renewal_count, last_renewed_at, last_renewal_price
            FROM subscriptions
            WHERE id = ?
            """,
            (sub_id,)
        )
        sub = cursor.fetchone()
        conn.close()
        if not sub:
            await safe_query_answer(query, "🔎 این اشتراک پیدا نشد.", show_alert=True)
            return
        (
            target_user_id, service_type, volume, price,
            duration_months, status,
            subscription_text, subscription_media_type, subscription_media_id, created_at,
            completed_at, expires_at, admin_message_id, renewal_count, last_renewed_at, last_renewal_price
        ) = sub
        try:
            target_user = await context.bot.get_chat(target_user_id)
            full_name = target_user.full_name
            username = (
                f"@{target_user.username}"
                if target_user.username
                else "ندارد"
            )
        except Exception:
            full_name = "نامشخص"
            username = "نامشخص"
        now = iran_now()
        # تشخیص وضعیت واقعی
        real_status = status
        if status == "completed" and expires_at:
            try:
                expiry_date = datetime.strptime(
                    expires_at, "%Y-%m-%d %H:%M:%S"
                )
                if expiry_date <= now:
                    real_status = "expired"
            except ValueError:
                pass
        status_titles = {
            "pending": "⏳ در انتظار",
            "completed": "✅ تکمیل شده",
            "rejected": "❌ رد شده",
            "expired": "⌛ منقضی شده",
            "cancelled": "🚫 لغو شده",
        }
        service_title = service_display_title(service_type)
        status_title = status_titles.get(real_status, real_status)
        text = (
            f"📦 جزئیات اشتراک #{sub_id}\n\n"
            f"👤 نام: {full_name}\n"
            f"🔹 Username: {username}\n"
            f"🆔 ID: {target_user_id}\n\n"
            f"🌐 نوع: {service_title}\n"
            f"📦 حجم: {volume}\n"
            f"⏳ مدت: {duration_months} ماه\n"
            f"💰 مبلغ: {price:,} تومان\n"
            f"📌 وضعیت: {status_title}\n\n"
            f"📅 تاریخ ثبت: {iran_time_text(created_at)}\n"
        )
        if completed_at:
            text += f"✅ تاریخ تکمیل: {iran_time_text(completed_at)}\n"
        if expires_at:
            text += f"⌛ تاریخ انقضا: {iran_local_time_text(expires_at)}\n"
        if renewal_count:
            text += f"🔄 دفعات تمدید: {int(renewal_count)}\n"
            if last_renewed_at: text += f"🕒 آخرین تمدید: {iran_time_text(last_renewed_at)}\n"
            if last_renewal_price: text += f"💰 مبلغ آخرین تمدید: {int(last_renewal_price):,} تومان\n"
        if status == "cancelled":
            conn = db_connect()
            try:
                cancel_row = conn.execute(
                    "SELECT cancelled_at, cancelled_by, cancellation_reason FROM subscriptions WHERE id=?",
                    (sub_id,)
                ).fetchone()
            finally:
                conn.close()
            if cancel_row:
                cancelled_at, cancelled_by, cancellation_reason = cancel_row
                if cancelled_at:
                    text += f"🚫 تاریخ لغو: {iran_time_text(cancelled_at)}\n"
                if cancelled_by:
                    text += f"👮 لغو توسط: <code>{cancelled_by}</code>\n"
                if cancellation_reason:
                    text += f"📝 دلیل لغو: {html.escape(str(cancellation_reason))}\n"
        # اطلاعات زنده کلاینت X-UI را هم در پنل مدیریت نشان بده.
        # این بخش عمداً از خود X-UI رفرش می‌شود تا UUID/Sub ID/Email، حجم
        # باقی‌مانده و روزهای باقی‌مانده فقط از snapshot قدیمی سفارش نباشند.
        xui_email = None
        xui_uuid = None
        xui_sub_id = None
        xui_total = 0
        xui_used = 0
        if real_status in ("completed", "expired"):
            try:
                conn = db_connect()
                xrow = conn.execute(
                    "SELECT xui_email,xui_uuid,xui_sub_id,xui_total_bytes,xui_used_bytes FROM subscriptions WHERE id=?",
                    (sub_id,)
                ).fetchone()
                conn.close()
                if xrow:
                    xui_email, xui_uuid, xui_sub_id, xui_total, xui_used = xrow
                if xui_email:
                    client = await xui_find_client(xui_email)
                    if client:
                        xui_email = client.get("email") or xui_email
                        xui_uuid = client.get("uuid") or xui_uuid
                        xui_sub_id = client.get("subId") or xui_sub_id
                        xui_total = int(client.get("totalGB") or xui_total or 0)
                        traffic = client.get("traffic") or {}
                        xui_used = int(traffic.get("up", 0) or 0) + int(traffic.get("down", 0) or 0)
                        conn = db_connect()
                        conn.execute(
                            "UPDATE subscriptions SET xui_email=?,xui_uuid=?,xui_sub_id=?,xui_total_bytes=?,xui_used_bytes=? WHERE id=?",
                            (xui_email, xui_uuid, xui_sub_id, xui_total, xui_used, sub_id)
                        )
                        conn.commit(); conn.close()
            except Exception as exc:
                print(f"Admin subscription X-UI refresh error (sub={sub_id}): {exc}")

        if xui_email:
            text += f"\n👤 X-UI Email: <code>{html.escape(str(xui_email))}</code>\n"
        if xui_uuid:
            text += f"🆔 UUID: <code>{html.escape(str(xui_uuid))}</code>\n"
        if xui_sub_id:
            text += f"🔑 Sub ID: <code>{html.escape(str(xui_sub_id))}</code>\n"

        if xui_total > 0:
            remaining_bytes = max(0, int(xui_total) - int(xui_used))
            text += (
                f"📊 حجم کل: <b>{format_bytes(xui_total)}</b>\n"
                f"📉 مصرف شده: <b>{format_bytes(xui_used)}</b>\n"
                f"💾 حجم باقی‌مانده: <b>{format_bytes(remaining_bytes)}</b>\n"
            )
        else:
            text += "💾 حجم باقی‌مانده: ♾️ <b>نامحدود</b>\n"

        remaining_days = 0
        if expires_at:
            try:
                remaining_days = max(0, (datetime.strptime(str(expires_at), "%Y-%m-%d %H:%M:%S") - iran_now()).days)
            except (TypeError, ValueError):
                remaining_days = 0
        text += f"📅 روزهای باقی‌مانده: <b>{remaining_days}</b> روز\n"

        if subscription_text:
            text += (
                f"\n🔗 لینک / متن اشتراک:\n"
                f"━━━━━━━━━━━━━━━\n"
                f"{html.escape(str(subscription_text))}\n"
                f"━━━━━━━━━━━━━━━"
            )
        keyboard = []

        if status == "pending":
            keyboard.append([
                InlineKeyboardButton(
                    "🔗 ارسال لینک اشتراک",
                    callback_data=f"admin_send_sub_link_{sub_id}"
                )
            ])
            keyboard.append([
                InlineKeyboardButton(
                    "🚫 لغو سفارش و بازگشت وجه",
                    callback_data=f"admin_cancel_sub_{sub_id}"
                )
            ])

        keyboard.extend([
            [
                InlineKeyboardButton(
                    "👤 مشاهده کاربر",
                    callback_data=f"admin_user_{target_user_id}"
                )
            ],
            [
                InlineKeyboardButton(
                    "🛒 خریدهای محصول",
                    callback_data=f"admin_user_products_{target_user_id}"
                )
            ],
        ])
        markup=InlineKeyboardMarkup(keyboard)
        if subscription_media_id:
            msg=getattr(query,"message",None)
            if msg and not any(getattr(msg,n,None) for n in ("photo","document","video","animation","audio","voice")):
                try: await msg.delete()
                except TelegramError: pass
                await send_subscription_media(context.bot,ADMIN_ID,subscription_media_type,subscription_media_id,text,markup, subscription_id=sub_id)
            else:
                await safe_edit_message(query,text,reply_markup=markup,parse_mode="HTML")
        else:
            await safe_edit_message(query,text,reply_markup=markup,parse_mode="HTML")

        # آمار کلی

    elif query.data == "admin_stats":

        if user_id != ADMIN_ID:
            await safe_query_answer(query, "❌ شما دسترسی ندارید.", show_alert=True)
            return

            conn = db_connect()
        cursor = conn.cursor()

        # تعداد کل کاربران
        cursor.execute("SELECT COUNT(*) FROM users")
        total_users = cursor.fetchone()[0]

        # مجموع موجودی کاربران
        cursor.execute("SELECT SUM(balance) FROM users")
        total_balance = cursor.fetchone()[0] or 0

        # تعداد اشتراک‌های فعال
        cursor.execute(
            """
            SELECT COUNT(*)
            FROM subscriptions
            WHERE status = 'completed'
              AND expires_at > ?
            """,
            (iran_db_now_text(),)
        )
        active_subscriptions = cursor.fetchone()[0]

        # تعداد اشتراک‌های در انتظار
        cursor.execute(
            "SELECT COUNT(*) FROM subscriptions WHERE status = 'pending'"
        )
        pending_subscriptions = cursor.fetchone()[0]

        # درآمد کل (مجموع خریدهای تکمیل شده)
        cursor.execute("SELECT COALESCE((SELECT SUM(price) FROM subscriptions WHERE status='completed'),0) + COALESCE((SELECT SUM(amount) FROM subscription_renewals),0)")
        total_revenue = cursor.fetchone()[0] or 0

        # درخواست‌های واریز در انتظار
        cursor.execute(
            "SELECT COUNT(*) FROM deposits WHERE status = 'pending'"
        )
        pending_deposits = cursor.fetchone()[0]

        # تعداد کاربران امروز
        iran_today_start = iran_now().replace(hour=0, minute=0, second=0, microsecond=0)
        iran_today_end = iran_today_start + timedelta(days=1)
        utc_start = iran_today_start.replace(tzinfo=IRAN_TZ).astimezone(UTC_TZ).strftime("%Y-%m-%d %H:%M:%S")
        utc_end = iran_today_end.replace(tzinfo=IRAN_TZ).astimezone(UTC_TZ).strftime("%Y-%m-%d %H:%M:%S")
        cursor.execute(
            """
            SELECT COUNT(*)
            FROM users
            WHERE created_at >= ? AND created_at < ?
            """,
            (utc_start, utc_end)
        )
        today_new_users = cursor.fetchone()[0]

        conn.close()

        keyboard = [
            [
                InlineKeyboardButton(
                    "🔄 بروزرسانی",
                    callback_data="admin_stats"
                )
            ],
            [
                InlineKeyboardButton(
                    "🔙 پنل مدیریت",
                    callback_data="admin_panel"
                )
            ]
        ]

        await safe_edit_message(query, 
            "📊 <b>آمار کلی ربات</b>\n\n"
            f"👥 کل کاربران: <code>{total_users:,}</code>\n"
            f"🆕 کاربران امروز: <code>{today_new_users:,}</code>\n\n"
            f"💰 مجموع موجودی: <code>{total_balance:,}</code> تومان\n"
            f"💵 درآمد کل: <code>{total_revenue:,}</code> تومان\n\n"
            f"📦 اشتراک‌های فعال: <code>{active_subscriptions:,}</code>\n"
            f"⏳ سفارشات در انتظار: <code>{pending_subscriptions:,}</code>\n"
            f"💳 درخواست‌های واریز pending: <code>{pending_deposits:,}</code>",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="HTML"
        )


    # مدیریت پرداخت‌ها توسط ادمین
    elif query.data == "admin_payments":
        if user_id != ADMIN_ID:
            await safe_query_answer(query, "❌ شما دسترسی ندارید.", show_alert=True)
            return
        conn = db_connect()
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT id, user_id, amount, status, created_at
            FROM deposits
            ORDER BY id DESC
            LIMIT 30
            """
        )
        deposits = cursor.fetchall()
        cursor.execute(
            "SELECT COUNT(*) FROM deposits WHERE status = 'pending'"
        )
        pending_count = cursor.fetchone()[0]
        cursor.execute(
            "SELECT SUM(amount) FROM deposits WHERE status = 'approved'"
        )
        total_approved = cursor.fetchone()[0] or 0
        conn.close()
        status_icons = {
            "pending": "⏳",
            "approved": "✅",
            "rejected": "❌",
        }
        if not deposits:
            keyboard = [
                [
                    InlineKeyboardButton(
                        "🔙 پنل مدیریت",
                        callback_data="admin_panel"
                    )
                ]
            ]
            await safe_edit_message(query, 
                "💳 مدیریت پرداخت‌ها\n\n"
                "❌ هنوز هیچ پرداختی ثبت نشده است.\n\n"
                f"⏳ در انتظار تأیید: {pending_count}\n"
                f"✅ مجموع تأیید شده: {total_approved:,} تومان",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
            return
        text = (
            "💳 مدیریت پرداخت‌ها\n\n"
            f"⏳ در انتظار: {pending_count}\n"
            f"✅ مجموع تأیید شده: {total_approved:,} تومان\n\n"
            "📋 لیست پرداخت‌ها:\n"
        )
        keyboard = []
        for dep_id, target_user_id, amount, status, created_at in deposits[:20]:
            try:
                target_user = await context.bot.get_chat(target_user_id)
                user_title = (
                    f"@{target_user.username}"
                    if target_user.username
                    else target_user.full_name
                )
            except Exception:
                user_title = f"ID:{target_user_id}"
            icon = status_icons.get(status, "❓")
            keyboard.append([
                InlineKeyboardButton(
                    f"{icon} #{dep_id} | {user_title} | 💰 {amount:,}",
                    callback_data=f"admin_deposit_detail_{dep_id}"
                )
            ])
        keyboard.append([
            InlineKeyboardButton(
                "🔄 بروزرسانی",
                callback_data="admin_payments"
            )
        ])
        keyboard.append([
            InlineKeyboardButton(
                "🔙 پنل مدیریت",
                callback_data="admin_panel"
            )
        ])
        await safe_edit_message(query, 
            text,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="HTML"
        )

    # =========================
    # درخواست‌های در انتظار
    # =========================

    elif query.data == "admin_pending_actions":

        context.user_data.pop(
            "admin_subscription_reply",
        )

        if user_id != ADMIN_ID:
            await safe_query_answer(query, 
                "❌ شما دسترسی ندارید.",
                show_alert=True
            )
            return

        await show_pending_actions(query, context)

    elif query.data == "admin_renewals":
        if user_id != ADMIN_ID:
            await safe_query_answer(query, "❌ شما دسترسی ندارید.", show_alert=True)
            return
        await show_pending_renewals(query, context)

    # =========================
    # پرداخت‌های در انتظار
    # =========================

    elif query.data == "admin_pending_payments":

        context.user_data.pop(
            "admin_subscription_reply",
        )

        if user_id != ADMIN_ID:
            await safe_query_answer(query, 
                "❌ شما دسترسی ندارید.",
                show_alert=True
            )
            return

        await show_pending_payments(query, context)

    # =========================
    # اشتراک‌های در انتظار ارسال
    # =========================

    elif query.data == "admin_pending_subscriptions":

        context.user_data.pop(
            "admin_subscription_reply",
        )

        if user_id != ADMIN_ID:
            await safe_query_answer(query, 
                "❌ شما دسترسی ندارید.",
                show_alert=True
            )
            return

        await show_pending_subscriptions(query, context)

    # =========================
    # جزئیات اشتراک در انتظار
    # =========================

    elif query.data.startswith("admin_pending_sub_detail_"):

        # ورود به جزئیات/برگشت از «انصراف» نباید سفارش قبلی را در حالت ارسال نگه دارد.
        context.user_data.pop("admin_subscription_reply", None)

        if user_id != ADMIN_ID:
            await safe_query_answer(query, 
                "❌ شما دسترسی ندارید.",
                show_alert=True
            )
            return

        sub_id = int(
            query.data.replace(
                "admin_pending_sub_detail_",
                ""
            )
        )

        conn = db_connect()
        cursor = conn.cursor()

        cursor.execute(
            """
            SELECT user_id,
                   service_type,
                   volume,
                   price,
                   duration_months,
                   status,
                   subscription_text,
                   created_at,
                   admin_message_id
            FROM subscriptions
            WHERE id = ?
            """,
            (sub_id,)
        )

        sub = cursor.fetchone()
        conn.close()

        if not sub:
            await safe_query_answer(query, 
                "🔎 این اشتراک پیدا نشد.",
                show_alert=True
            )
            return

        (
            target_user_id,
            service_type,
            volume,
            price,
            duration_months,
            status,
            subscription_text,
            created_at,
            admin_message_id
        ) = sub

        # اگر دیگر pending نیست
        if status != "pending":
            await safe_query_answer(query, 
                "⚠️ این اشتراک دیگر در انتظار ارسال نیست.",
                show_alert=True
            )

            await show_pending_subscriptions(
                query,
                context
            )
            return

        try:
            target_user = await context.bot.get_chat(
                target_user_id
            )

            full_name = target_user.full_name or "نامشخص"

            username = (
                f"@{target_user.username}"
                if target_user.username
                else "ندارد"
            )

        except Exception:
            full_name = "نامشخص"
            username = "ندارد"

        service_title = service_display_title(service_type)

        text = (
            f"📦 <b>اشتراک در انتظار ارسال #{sub_id}</b>\n\n"
            f"👤 نام: {full_name}\n"
            f"🔹 Username: {username}\n"
            f"🆔 ID: <code>{target_user_id}</code>\n\n"
            f"🌐 نوع: {service_title}\n"
            f"📦 حجم: {volume}\n"
            f"⏳ مدت: {duration_months} ماه\n"
            f"💰 مبلغ: {price:,} تومان\n"
            f"📅 تاریخ ثبت: {iran_time_text(created_at)}\n\n"
            "⏳ <b>وضعیت: منتظر ارسال لینک اشتراک</b>"
        )

        keyboard = [
            [
                InlineKeyboardButton(
                    "🔗 ارسال لینک اشتراک",
                    callback_data=f"admin_send_sub_link_{sub_id}"
                )
            ],
            [
                InlineKeyboardButton(
                    "🚫 لغو سفارش و بازگشت وجه",
                    callback_data=f"admin_cancel_sub_{sub_id}"
                )
            ],
            [
                InlineKeyboardButton(
                    "📨 ارسال پیام به کاربر",
                    callback_data=f"user_msg_{target_user_id}"
                )
            ],
            [
                InlineKeyboardButton(
                    "👤 مشاهده کاربر",
                    callback_data=f"admin_user_{target_user_id}"
                )
            ],
            [
                InlineKeyboardButton(
                    "🔙 اشتراک‌های در انتظار",
                    callback_data="admin_pending_subscriptions"
                )
            ]
        ]

        await safe_edit_message(query,
                    text,
                    reply_markup=InlineKeyboardMarkup(keyboard),
                    parse_mode="HTML"
                )


    # =========================
    # جزئیات تمدید در انتظار
    # =========================

    elif query.data.startswith("admin_renewal_detail_"):

                if user_id != ADMIN_ID:
                    await safe_query_answer(query,
                        "❌ شما دسترسی ندارید.",
                        show_alert=True
                    )
                    return

                try:
                    renewal_id = int(query.data.replace("admin_renewal_detail_", ""))
                except (ValueError, IndexError):
                    await safe_query_answer(query, "❌ شناسه تمدید نامعتبر است.", show_alert=True)
                    return

                conn = db_connect()
                cursor = conn.cursor()

                cursor.execute("""
                    SELECT sr.id, sr.subscription_id, sr.user_id, sr.service_type, sr.volume,
                           sr.amount, sr.old_expires_at, sr.new_expires_at, sr.created_at,
                           u.username, u.full_name
                    FROM subscription_renewals sr
                    LEFT JOIN users u ON u.user_id = sr.user_id
                    LEFT JOIN subscriptions s ON s.id = sr.subscription_id
                    WHERE sr.id = ? AND s.status = 'renewal_pending'
                """, (renewal_id,))

                renewal = cursor.fetchone()
                conn.close()

                if not renewal:
                    await safe_query_answer(query, "❌ درخواست تمدید پیدا نشد یا دیگر در انتظار نیست.", show_alert=True)
                    return

                (
                    r_id, sub_id, r_user_id, service_type, volume,
                    amount, old_expires_at, new_expires_at, created_at,
                    username, full_name
                ) = renewal

                name = f"@{username.lstrip('@')}" if username else (full_name or f"ID:{r_user_id}")
                service_title = service_display_title(service_type)

                text = (
                    "🔄 <b>جزئیات درخواست تمدید</b>\n\n"
                    f"🔢 شماره تمدید: <code>{r_id}</code>\n"
                    f"📦 شماره اشتراک: <code>{sub_id}</code>\n\n"
                    f"👤 نام: {html.escape(name)}\n"
                    f"🆔 ID: <code>{r_user_id}</code>\n\n"
                    f"🌐 سرویس: {service_title}\n"
                    f"📦 حجم: <code>{html.escape(str(volume))}</code>\n"
                    f"💰 مبلغ: {amount:,} تومان\n\n"
                    f"⏳ انقضای قبلی: {html.escape(str(old_expires_at) or 'ندارد')}\n"
                    f"📅 انقضای جدید: {html.escape(str(new_expires_at))}\n"
                    f"🕐 درخواست شده: {html.escape(str(created_at))}\n\n"
                    "✅ برای تأیید و تمدید، دکمه زیر را بزنید:"
                )

                keyboard = [
                    [
                        InlineKeyboardButton(
                            "✅ تأیید تمدید",
                            callback_data=f"admin_approve_renewal_{sub_id}"
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            "❌ رد و بازگشت وجه",
                            callback_data=f"admin_reject_renewal_{sub_id}"
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            "🔙 برگشت به لیست",
                            callback_data="admin_renewals"
                        )
                    ]
                ]

                await safe_edit_message(query, text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
                return


    # =========================
    # شروع ارسال لینک برای اشتراک انتخاب‌شده
    # =========================

    elif query.data.startswith("admin_send_sub_link_"):
        if user_id != ADMIN_ID:
            await safe_query_answer(query,
                    "❌ شما دسترسی ندارید.", show_alert=True)
            return


        if user_id != ADMIN_ID:
            await safe_query_answer(query, 
                "❌ شما دسترسی ندارید.",
                show_alert=True
            )
            return

        sub_id = int(
            query.data.replace(
                "admin_send_sub_link_",""
            )
        )

        begin_input_state(context); context.user_data["admin_subscription_reply"] = sub_id

        conn = db_connect()
        cursor = conn.cursor()

        cursor.execute(
            """
            SELECT user_id,
                   service_type,
                   volume,
                   price,
                   duration_months,
                   status
            FROM subscriptions
            WHERE id = ?
            """,
            (sub_id,)
        )

        sub = cursor.fetchone()
        conn.close()

        if not sub:
            await safe_query_answer(query, 
                "🔎 این اشتراک پیدا نشد.",
                show_alert=True
            )
            return

        (
            target_user_id,
            service_type,
            volume,
            price,
            duration_months,
            status
        ) = sub

        if status != "pending":
            await safe_query_answer(query, 
                "⚠️ این اشتراک قبلاً تکمیل شده یا دیگر در انتظار نیست.",
                show_alert=True
            )
            return

        # ذخیره سفارش انتخاب‌شده
        begin_input_state(context); context.user_data["admin_subscription_reply"] = sub_id

        keyboard = [
            [
                InlineKeyboardButton(
                    "❌ انصراف",
                    callback_data=f"admin_pending_sub_detail_{sub_id}"
                )
            ]
        ]

        await safe_edit_message(query, 
            "🔗 <b>ارسال لینک اشتراک</b>\n\n"
            f"🔢 شماره سفارش: <code>{sub_id}</code>\n"
            f"🌐 سرویس: {service_display_title(service_type)}\n"
            f"📦 حجم: {volume}\n"
            f"⏳ مدت: {duration_months} ماه\n"
            f"💰 مبلغ: {price:,} تومان\n\n"
            "لطفاً <b>لینک یا متن اشتراک</b> را در پیام بعدی ارسال کنید.\n\n"
            "⚠️ این بار لازم نیست روی پیام قدیمی Reply کنید؛ "
            "ربات همین سفارش انتخاب‌شده را تکمیل می‌کند.",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="HTML"
        )



    elif query.data.startswith("admin_pending_deposit_detail_"):
        if user_id != ADMIN_ID:
            await safe_query_answer(query, "❌ شما دسترسی ندارید.", show_alert=True)
            return
        try:
            deposit_id = int(query.data.rsplit("_", 1)[1])
        except (ValueError, IndexError):
            await safe_query_answer(query, "❌ پرداخت نامعتبر است.", show_alert=True)
            return
        conn = db_connect()
        row = conn.execute(
            """SELECT d.user_id,d.amount,d.payment_method,d.status,d.processed_by,d.created_at,d.processed_at,d.receipt_file_id,d.receipt_archive_message_id,d.receipt_archive_chat_id,d.receipt_local_path,
                      COALESCE(u.username,''),COALESCE(u.full_name,'')
               FROM deposits d LEFT JOIN users u ON u.user_id=d.user_id WHERE d.id=?""",
            (deposit_id,)
        ).fetchone()
        conn.close()
        if not row:
            await safe_query_answer(query, "❌ پرداخت پیدا نشد.", show_alert=True)
            return
        target_user_id, amount, method, status, processed_by, created_at, processed_at, receipt_file_id, receipt_archive_message_id, receipt_archive_chat_id, receipt_local_path, username, full_name = row
        methods={"card":"💳 کارت به کارت","ton":"💎 TON","star":"⭐️ استار تلگرام"}
        statuses={"pending":"⏳ در انتظار تأیید","approved":"✅ تأیید شده","rejected":"❌ رد شده"}
        user_title=f"@{username.lstrip('@')}" if username else (full_name or f"ID:{target_user_id}")
        text=(f"💳 <b>جزئیات پرداخت #{deposit_id}</b>\n\n"
              f"👤 {html.escape(user_title)}\n🆔 <code>{target_user_id}</code>\n"
              f"💰 مبلغ: <b>{amount:,} تومان</b>\n"
              f"💳 روش: {methods.get(method, html.escape(method or 'نامشخص'))}\n"
              f"📌 وضعیت: {statuses.get(status, html.escape(status or 'نامشخص'))}\n"
              f"📅 ثبت: {html.escape(iran_time_text(created_at))}")
        if processed_at:
            text += f"\n🕒 پردازش: {html.escape(iran_time_text(processed_at))}"
        if processed_by:
            text += f"\n👮 بررسی توسط: <code>{processed_by}</code>"
        kb=[]
        if status == "pending":
            kb.append([InlineKeyboardButton("✅ تأیید", callback_data=f"approve_pending_{deposit_id}"),
                       InlineKeyboardButton("❌ رد", callback_data=f"reject_pending_{deposit_id}")])
        kb.append([InlineKeyboardButton("👤 مشاهده کاربر", callback_data=f"admin_user_{target_user_id}")])
        kb.append([InlineKeyboardButton("🔙 برگشت به پرداخت‌های در انتظار", callback_data="admin_pending_payments")])
        markup=InlineKeyboardMarkup(kb)
        if receipt_file_id and query.message and not getattr(query.message, "photo", None):
            # پیام لیست باید واقعاً جای خود را به رسید بدهد؛ در غیر این صورت رسید قدیمی در چت باقی می‌ماند.
            chat = query.message.chat
            try:
                await query.message.delete()
            except TelegramError:
                pass
            sent = await send_receipt_media(context.bot, chat.id, receipt_file_id, receipt_archive_chat_id, receipt_archive_message_id, receipt_local_path, text, markup, deposit_id=deposit_id)
            if sent is None:
                await safe_edit_message(query, text + "\n📸 رسید قابل بازیابی نیست؛ اطلاعات پرداخت محفوظ است.", reply_markup=markup, parse_mode="HTML")
        else:
            await safe_edit_message(query, text, reply_markup=markup, parse_mode="HTML")
        return

    elif query.data.startswith("admin_deposit_detail_"):
        if user_id != ADMIN_ID:
            await safe_query_answer(query, "❌ شما دسترسی ندارید.", show_alert=True)
            return
        deposit_id = int(
            query.data.replace("admin_deposit_detail_", "")
        )
        conn = db_connect()
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT user_id, amount, payment_method, status,
                   processed_by, created_at, processed_at, receipt_file_id, receipt_archive_message_id, receipt_archive_chat_id, receipt_local_path
            FROM deposits
            WHERE id = ?
            """,
            (deposit_id,)
        )
        deposit = cursor.fetchone()
        conn.close()
        if not deposit:
            await safe_query_answer(query, "❌ پرداخت پیدا نشد.", show_alert=True)
            return
        (
            target_user_id, amount, method, status,
            processed_by, created_at, processed_at, receipt_file_id, receipt_archive_message_id, receipt_archive_chat_id, receipt_local_path
        ) = deposit
        try:
            target_user = await context.bot.get_chat(target_user_id)
            full_name = target_user.full_name
            username = (
                f"@{target_user.username}"
                if target_user.username
                else "ندارد"
            )
        except Exception:
            full_name = "نامشخص"
            username = "نامشخص"
        status_titles = {
            "pending": "⏳ در انتظار تأیید",
            "approved": "✅ تأیید شده",
            "rejected": "❌ رد شده",
        }
        method_titles = {
            "card": "💳 کارت به کارت",
            "ton": "💎 TON",
            "star": "⭐️ استار تلگرام",
        }
        status_title = status_titles.get(status, status)
        method_title = method_titles.get(method, method or "نامشخص")
        text = (
            "💳 جزئیات پرداخت\n\n"
            f"🔢 شماره: {deposit_id}\n"
            f"👤 نام: {full_name}\n"
            f"🔹 Username: {username}\n"
            f"🆔 ID: {target_user_id}\n\n"
            f"💰 مبلغ: {amount:,} تومان\n"
            f"💳 روش پرداخت: {method_title}\n"
            f"📌 وضعیت: {status_title}\n\n"
            f"📅 تاریخ ثبت: {iran_time_text(created_at)}\n"
        )
        if processed_at:
            text += f"✅ تاریخ بررسی: {iran_time_text(processed_at)}\n"
        if processed_by:
            text += f"👮 بررسی توسط: {processed_by}\n"
        keyboard = []
        if status == "pending":
            keyboard.append([
                InlineKeyboardButton(
                    "✅ تأیید",
                    callback_data=f"approve_{deposit_id}"
                ),
                InlineKeyboardButton(
                    "❌ رد",
                    callback_data=f"reject_{deposit_id}"
                )
            ])
        keyboard.append([
            InlineKeyboardButton(
                "👤 مشاهده کاربر",
                callback_data=f"admin_user_{target_user_id}"
            )
        ])
        keyboard.append([
            InlineKeyboardButton(
                "📨 ارسال پیام به کاربر",
                callback_data=f"user_msg_{target_user_id}"
            )
        ])
        keyboard.append([
            InlineKeyboardButton(
                "🔙 بازگشت به پرداخت‌ها",
                callback_data="admin_payments"
            )
        ])
        if receipt_file_id:
            chat_id = query.message.chat_id
            if query.message and not getattr(query.message, "photo", None):
                try:
                    await query.message.delete()
                except TelegramError:
                    pass
            sent = await send_receipt_media(
                context.bot, chat_id, receipt_file_id,
                receipt_archive_chat_id, receipt_archive_message_id,
                receipt_local_path, text, InlineKeyboardMarkup(keyboard), deposit_id=deposit_id
            )
            if sent is not None:
                await safe_query_answer(query, "📸 رسید نمایش داده شد.")
                return
        await safe_edit_message(
            query,
            text + ("\n📸 رسید تصویر قابل بازیابی نیست؛ اطلاعات پرداخت همچنان موجود است." if receipt_file_id else "\n📸 برای این درخواست رسیدی ذخیره نشده است."),
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="HTML"
        )

    #جستجو
    elif query.data == "admin_search":

        if user_id != ADMIN_ID:
            await safe_query_answer(query, "❌ شما دسترسی ندارید.", show_alert=True)
            return

        begin_input_state(context); context.user_data["admin_search"] = True

        keyboard = [
            [
                InlineKeyboardButton(
                    "🔙 انصراف",
                    callback_data="admin_panel"
                )
            ]
        ]

        await safe_edit_message(query, 
            "🔍 <b>جستجوی کاربر</b>\n\n"
            "لطفاً یکی از موارد زیر را ارسال کنید:\n\n"
            "• <code>آیدی عددی کاربر</code> (مثل <code>123456789</code>)\n"
            "• <code>@username</code> (مثل <code>@example</code>)\n\n"
            "برای لغو، دکمه برگشت را بزنید.",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="HTML"
        )

        #

    elif query.data.startswith("user_msg_"):

        if user_id != ADMIN_ID:
            await safe_query_answer(query, "❌ شما دسترسی ندارید.", show_alert=True)
            return

        target_user_id = int(query.data.replace("user_msg_", ""))

        begin_input_state(context); context.user_data["admin_messaging"] = target_user_id

        keyboard = [
            [
                InlineKeyboardButton(
                    "🔙 انصراف",
                    callback_data=f"admin_user_{target_user_id}"
                )
            ]
        ]

        await safe_edit_message(query, 
            f"📨 <b>ارسال پیام به کاربر <code>{target_user_id}</code></b>\n\n"
            "پیام، عکس یا هر محتوایی که می‌خواهید ارسال شود را بفرستید:",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="HTML"
        )


    # پیام همگانی
    elif query.data == "admin_broadcast":
        if user_id != ADMIN_ID:
            await safe_query_answer(query, "❌ شما دسترسی به این بخش را ندارید.", show_alert=True)
            return

        begin_input_state(context); context.user_data["waiting_for_broadcast"] = True
        keyboard = [
            [
                InlineKeyboardButton(
                    "‹  برگشت",
                    callback_data="back_main"
                )
            ]
        ]

        await safe_edit_message(query, 
            "📢 <b>ارسال پیام همگانی</b>\n\n"
            "لطفاً متن، عکس، فایل، ویدیو یا هر محتوایی که می‌خواهید برای همه ارسال شود را بفرستید:\n\n"
            "در صورت انصراف دکمه برگشت را بزنید.",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="HTML"
        )

    # مدیریت قیمت CDN (مسیر سازگاری؛ پنل جدید همه عملیات را پوشش می‌دهد)
    elif query.data == "admin_cdn_prices":
        if user_id != ADMIN_ID:
            await safe_query_answer(query,"❌ شما دسترسی ندارید.",show_alert=True); return
        await show_admin_service_plans(query, context, resolve_service_type("cdn"))
        return

    ###
    elif query.data.startswith("admin_price_"):

        if user_id != ADMIN_ID:
            await safe_query_answer(query, 
                "❌ شما دسترسی ندارید.",
                show_alert=True
            )
            return

        volume = query.data.replace("admin_price_", "")
        cfg = SERVICE_CONFIG.get(resolve_service_type("cdn"))
        conn = db_connect()
        try:
            result = conn.execute("SELECT price FROM service_plans WHERE service_id=? AND volume=? AND active=1", (cfg["service_id"], volume)).fetchone() if cfg else None
        finally:
            conn.close()
        if not result:
            await safe_query_answer(query, "❌ این حجم پیدا نشد یا فعال نیست.", show_alert=True)
            return
        price = result[0]

        if volume == "unlimited":
            volume_title = "♾️ نامحدود"
        else:
            volume_title = f"📦 {volume}"

        begin_input_state(context, "editing_cdn_volume"); context.user_data["waiting_for_cdn_price"] = True
        context.user_data["editing_cdn_volume"] = volume

        keyboard = [
            [
                InlineKeyboardButton(
                    "‹  برگشت",
                    callback_data="admin_cdn_prices"
                )
            ]
        ]

        await safe_edit_message(query, 
            f"✏️ تغییر قیمت CDN\n\n"
            f"{volume_title}\n"
            f"💰 قیمت فعلی: {price:,} تومان\n\n"
            "لطفاً قیمت جدید را به صورت عدد وارد کنید.\n\n"
            "مثال:\n"
            "30000",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )


    # مدیریت قیمت Tunnel (مسیر سازگاری؛ پنل جدید همه عملیات را پوشش می‌دهد)
    elif query.data == "admin_tunnel_prices":
        if user_id != ADMIN_ID:
            await safe_query_answer(query, "❌ شما دسترسی ندارید.", show_alert=True); return
        await show_admin_service_plans(query, context, resolve_service_type("tunnel"))
        return

    elif query.data.startswith("admin_tunnel_price_"):
        if user_id != ADMIN_ID:
            await safe_query_answer(query, "❌ شما دسترسی ندارید.", show_alert=True)
            return
        volume = query.data.replace("admin_tunnel_price_", "", 1)
        cfg = SERVICE_CONFIG.get(resolve_service_type("tunnel"))
        conn = db_connect()
        try:
            row = conn.execute("SELECT price FROM service_plans WHERE service_id=? AND volume=? AND active=1", (cfg["service_id"], volume)).fetchone() if cfg else None
        finally:
            conn.close()
        if not row:
            await safe_query_answer(query, "❌ این حجم پیدا نشد یا فعال نیست.", show_alert=True)
            return
        begin_input_state(context, "editing_tunnel_volume")
        context.user_data["waiting_for_tunnel_price"] = True
        context.user_data["editing_tunnel_volume"] = volume
        await safe_edit_message(
            query,
            f"✏️ <b>تغییر قیمت Tunnel</b>\n\n"
            f"📦 حجم: {html.escape(str(volume))}\n"
            f"💰 قیمت فعلی: {int(row[0]):,} تومان\n\n"
            "قیمت جدید را فقط به صورت عدد وارد کنید.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔙 مدیریت قیمت Tunnel", callback_data="admin_tunnel_prices")
            ]]),
            parse_mode="HTML"
        )

    # افزایش موجودی
    elif query.data == "charge":
        keyboard = [
            [
                InlineKeyboardButton(
                    "⭐️  استارز تلگرام  •  سریع و راحت",
                    callback_data="charge_star"
                )
            ],
            [
                InlineKeyboardButton(
                    "💳  کارت به کارت  •  دستی",
                    callback_data="charge_card"
                )
            ],
            [
                InlineKeyboardButton(
                    "💎  افزایش موجودی با ولت",
                    callback_data="charge_wallet"
                )
            ],
            [
                InlineKeyboardButton(
                    "🏠  بیخیال، برگردیم",
                    callback_data="back_main"
                )
            ]
        ]

        await safe_edit_message(
            query,
            "💰 <b>خب رفیق، چقدر می‌خوای حسابتو شارژ کنی؟</b>\n\n"
            "✨ یه روش رو انتخاب کن تا بریم سراغش.\n"
            "🔒 پرداخت‌ها از داخل تلگرام انجام می‌شن و روندش ساده‌ست.",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="HTML"
        )






    # موجودی من پول حرام
    elif query.data == "my_balance":
        user_id = query.from_user.id

        conn = db_connect()
        cursor = conn.cursor()

        cursor.execute(
            "SELECT balance FROM users WHERE user_id = ?",
            (user_id,)
        )

        result = cursor.fetchone()
        conn.close()

        balance = result[0] if result else 0

        keyboard = [
            [
                InlineKeyboardButton(
                    "🏠 برگشت به خانه",
                    callback_data="back_main"
                )
            ]
        ]

        await safe_edit_message(query, 
            text=(
                f"💵 موجودی شما:\n\n"
                f"💰 {balance:,} تومان"
            ),
            reply_markup=InlineKeyboardMarkup(keyboard)
        )


    # خرید اشتراک
    elif query.data == "buy_subscription":
        conn = db_connect()
        try:
            active_services = conn.execute(
                "SELECT code, title FROM service_catalog WHERE active=1 ORDER BY sort_order, id"
            ).fetchall()
        finally:
            conn.close()
        keyboard = [
            [InlineKeyboardButton(str(title), callback_data=f"service_{code}")]
            for code, title in active_services
        ]
        if custom_plan_settings()[0]:
            keyboard.append([InlineKeyboardButton("🌍 کشور انتخاب خودم", callback_data="custom_start")])
        if not keyboard:
            keyboard.append([InlineKeyboardButton("‹  برگشت", callback_data="back_main")])
            await safe_edit_message(query, "🛒 نوع اشتراک را انتخاب کنید:\n\n❌ در حال حاضر هیچ سرویس فعالی وجود ندارد.", reply_markup=InlineKeyboardMarkup(keyboard))
            return
        keyboard.append([InlineKeyboardButton("‹  برگشت", callback_data="back_main")])
        await safe_edit_message(query, "🛍️ <b>خب، چی لازم داری؟</b>\n\n👇 سرویس موردنظرت رو انتخاب کن:", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")


    # پرداخت‌های من
    elif query.data == "my_deposits":
        conn = db_connect()
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT id, amount, payment_method, status,
                   created_at, processed_at
            FROM deposits
            WHERE user_id = ?
            ORDER BY id DESC
            LIMIT 20
            """,
            (user_id,)
        )
        deposits = cursor.fetchall()
        conn.close()
        if not deposits:
            keyboard = [
                [
                    InlineKeyboardButton(
                        "💎 شارژ حساب",
                        callback_data="charge"
                    )
                ],
                [
                    InlineKeyboardButton(
                        "‹  برگشت",
                        callback_data="back_main"
                    )
                ]
            ]
            await safe_edit_message(query, 
                "🧾 <b>تاریخچه پرداخت‌هات</b>\n\n"
                "🌱 هنوز پرداختی ثبت نکردی.\n\nاگه می‌خوای شروع کنیم، از همین‌جا حسابت رو شارژ کن.",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
            return
        status_titles = {
            "pending": "⏳ در انتظار تأیید",
            "approved": "✅ تأیید شده",
            "rejected": "❌ رد شده",
        }
        method_titles = {
            "card": "💳 کارت به کارت",
            "ton": "💎 TON",
            "star": "⭐️ استار تلگرام",
        }
        text = "🧾 <b>تاریخچه پرداخت‌هات</b>\n\n"
        for (
                dep_id, amount, method, status,
                created_at, processed_at
        ) in deposits:
            method_title = method_titles.get(method, method or "نامشخص")
            status_title = status_titles.get(status, status)
            text += (
                f"🔢 شماره: {dep_id}\n"
                f"💰 مبلغ: {amount:,} تومان\n"
                f"💳 روش: {method_title}\n"
                f"📌 وضعیت: {status_title}\n"
                f"📅 تاریخ ثبت: {iran_time_text(created_at)}\n"
            )
            if processed_at:
                text += f"✅ تاریخ بررسی: {iran_time_text(processed_at)}\n"
            text += "\n──────────────\n\n"
        keyboard = [
            [
                InlineKeyboardButton(
                    "💎 شارژ حساب",
                    callback_data="charge"
                )
            ],
            [
                InlineKeyboardButton(
                    "‹  برگشت",
                    callback_data="back_main"
                )
            ]
        ]
        await safe_edit_message(query, 
            text,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="HTML"
        )

    # نمایندگی

    elif query.data == "agency":

        keyboard = [
            [
                InlineKeyboardButton(
                    "‹  برگشت",
                    callback_data="back_main"
                )
            ]
        ]

        await safe_edit_message(query, 
            "👤 درخواست نمایندگی\n\n"
            "جهت هماهنگی به @mmp_shadow پیام دهید.",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

        # =========================
    # خرید سرویس‌ها
    elif query.data.startswith("service_months_"):
        parts=query.data.split("_")
        if len(parts)!=4:
            await safe_query_answer(query,"❌ درخواست نامعتبر است.",show_alert=True); return
        await show_service_month_volumes(query,resolve_service_type(parts[2]),int(parts[3])); return

    elif query.data.startswith("service_") and not query.data.startswith(("service_volume_","service_months_","service_moshtarak")):
        service_type=resolve_service_type(query.data.replace("service_","",1)); None
        if service_type not in SERVICE_CONFIG:
            await safe_query_answer(query,"❌ سرویس پیدا نشد.",show_alert=True); return
        conn=db_connect(); active=conn.execute("SELECT active FROM service_catalog WHERE code=?",(service_type,)).fetchone(); conn.close()
        if not active or not int(active[0]):
            await safe_query_answer(query,"❌ این سرویس در حال حاضر فعال نیست.",show_alert=True); return
        await show_service_volumes(query,service_type)

    elif query.data.startswith("service_volume_"):
        parts=query.data.split("_",4)
        if len(parts)!=5:
            await safe_query_answer(query,"❌ درخواست نامعتبر است.",show_alert=True); return
        service_type=resolve_service_type(parts[2]); months=int(parts[3]); volume=parts[4]
        await show_service_volume_confirm(query,context,service_type,volume,months)

    elif query.data.startswith("confirm_service_"):
        parts=query.data.split("_",4)
        if len(parts)!=5:
            await safe_query_answer(query,"❌ درخواست نامعتبر است.",show_alert=True); return
        service_type=resolve_service_type(parts[2]); months=int(parts[3]); volume=parts[4]
        if service_type not in SERVICE_CONFIG:
            await safe_query_answer(query,"❌ سرویس نامعتبر است.",show_alert=True); return
        conn=db_connect(); row=conn.execute("SELECT price FROM service_plans WHERE service_id=? AND volume=? AND duration_months=? AND active=1",(SERVICE_CONFIG[service_type]["service_id"],volume,months)).fetchone(); user_row=conn.execute("SELECT balance,is_banned FROM users WHERE user_id=?",(user_id,)).fetchone(); conn.close()
        if not row or not user_row:
            await safe_query_answer(query,"❌ اطلاعات خرید پیدا نشد.",show_alert=True); return
        base_price=int(row[0]); price=base_price; discount_code=context.user_data.get("discount_code")
        if discount_code:
            conn=db_connect(); check,err=validate_discount(conn,discount_code,user_id,base_price); conn.close()
            if err: await safe_query_answer(query,err,show_alert=True); return
            price=check["final"]
        balance,is_banned=user_row
        if is_banned:
            await safe_query_answer(query,"❌ حساب شما مسدود است.",show_alert=True); return
        if balance<price:
            await safe_edit_message(query,f"❌ <b>موجودی کافی نیست</b>\n\n💰 قیمت: {price:,} تومان\n💵 موجودی: {balance:,} تومان\n💸 کمبود: {price-balance:,} تومان",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("💎 شارژ حساب",callback_data="charge")],[InlineKeyboardButton("‹ برگشت",callback_data=f"service_months_{service_type}_{months}")]]),parse_mode="HTML"); return
        await safe_edit_message(query,f"✅ <b>تأیید نهایی خرید</b>\n\n🌐 سرویس: {service_display_title(service_type)}\n📦 حجم: <code>{html.escape(str(volume))}</code>\n⏳ مدت: <b>{months} ماه</b>\n💰 مبلغ: <b>{price:,}</b> تومان\n💵 موجودی بعد از خرید: <b>{balance-price:,}</b> تومان\n\n⚡ بعد از تأیید، کاربر مستقیماً در X-UI ساخته می‌شود.",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✅ خرید نهایی",callback_data=f"final_service_{service_type}_{months}_{volume}")],[InlineKeyboardButton("❌ انصراف",callback_data=f"service_volume_{service_type}_{months}_{volume}")]]),parse_mode="HTML")

    elif query.data.startswith("final_service_"):
        parts=query.data.split("_",4)
        if len(parts)!=5:
            await safe_query_answer(query,"❌ درخواست نامعتبر است.",show_alert=True); return
        service_type=resolve_service_type(parts[2]); months=int(parts[3]); volume=parts[4]
        await finalize_service_purchase(update,context,query,service_type,volume,months)
    elif query.data.startswith("cdn_final_"):
        volume = query.data.replace("cdn_final_", "", 1)
        await finalize_service_purchase(update, context, query, "cdn", volume)

    # مشترک فعلاً فعال نشده است.
    elif query.data == "service_moshtarak":
        await safe_edit_message(
            query,
            "🔗 مشترک\n\n❌ در حال حاضر اشتراکی برای این بخش موجود نیست.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("‹  برگشت", callback_data="buy_subscription")]])
        )

    elif query.data == "admin_wallet":
        if user_id != ADMIN_ID:
            await safe_query_answer(query, "❌ شما دسترسی ندارید.", show_alert=True)
            return
        # آمار کلی
        conn = db_connect()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT COUNT(*), COALESCE(SUM(amount), 0) FROM balance_logs WHERE action = 'add'"
        )
        add_count, add_total = cursor.fetchone()
        cursor.execute(
            "SELECT COUNT(*), COALESCE(SUM(amount), 0) FROM balance_logs WHERE action = 'sub'"
        )
        sub_count, sub_total = cursor.fetchone()
        cursor.execute(
            "SELECT COUNT(*), COALESCE(SUM(amount), 0) FROM balance_logs WHERE action = 'set'"
        )
        set_count, set_total = cursor.fetchone()
        cursor.execute("SELECT COUNT(*) FROM balance_logs")
        total_logs = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(*) FROM users")
        total_users = cursor.fetchone()[0]
        # ۱۰ تراکنش اخیر
        cursor.execute(
            """
            SELECT id, user_id, action, amount,
                   old_balance, new_balance, reason, created_at
            FROM balance_logs
            ORDER BY id DESC
            LIMIT 10
            """
        )
        recent_logs = cursor.fetchall()
        conn.close()
        action_titles = {
            "add": ("➕", "افزایش"),
            "sub": ("➖", "کاهش"),
            "set": ("✏️", "تنظیم"),
        }
        text = (
            "💰 <b>کیف پول من</b>\n\n"
            f"📊 <b>آمار کلی:</b>\n"
            f"👥 تعداد کاربران: {total_users:,}\n"
            f"📋 کل تراکنش‌ها: {total_logs:,}\n\n"
            f"➕ افزایشها: {add_count:,} مورد | جمع: {add_total:,} تومان\n"
            f"➖ کاهشها: {sub_count:,} مورد | جمع: {sub_total:,} تومان\n"
            f"✏️ تنظیمها: {set_count:,} مورد | جمع: {set_total:,} تومان\n\n"
            "📜 <b>۱۰ تراکنش اخیر:</b>\n\n"
        )
        keyboard = []
        for (
            log_id, target_user_id, action,
            amount, old_balance, new_balance,
            reason, created_at
        ) in recent_logs:
            emoji, action_title = action_titles.get(
                action, ("❓", action)
            )
            try:
                target_user = await context.bot.get_chat(target_user_id)
                user_title = (
                    f"@{target_user.username}"
                    if target_user.username
                    else target_user.full_name
                )
            except Exception:
                user_title = f"ID:{target_user_id}"
            text += (
                f"{emoji} <b>{action_title}</b> {amount:,} تومان\n"
                f"👤 {user_title} (<code>{target_user_id}</code>)\n"
                f"├ {old_balance:,} ← {new_balance:,}\n"
                f"└ {iran_time_text(created_at)}\n\n"
            )
            keyboard.append([
                InlineKeyboardButton(
                    f"{emoji} {user_title} | {amount:,} تومان",
                    callback_data=f"admin_user_wallet_{target_user_id}"
                )
            ])
        keyboard.append([
            InlineKeyboardButton(
                "👥 مشاهده کاربران",
                callback_data="admin_users"
            )
        ])
        keyboard.append([
            InlineKeyboardButton(
                "🔙 پنل مدیریت",
                callback_data="admin_panel"
            )
        ])
        await safe_edit_message(query, 
            text,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="HTML"
        )



    elif query.data == "admin_subscriptions":
        if user_id != ADMIN_ID:
            await safe_query_answer(query, "❌ شما دسترسی ندارید.", show_alert=True)
            return
            conn = db_connect()
        cursor = conn.cursor()
        # آمار کلی
        cursor.execute(
            """
            SELECT COUNT(*)
            FROM subscriptions
            WHERE status = 'completed'
              AND expires_at > ?
            """,
            (iran_db_now_text(),)
        )
        active_count = cursor.fetchone()[0]
        cursor.execute(
            "SELECT COUNT(*) FROM subscriptions WHERE status = 'pending'"
        )
        pending_count = cursor.fetchone()[0]
        cursor.execute(
            """
            SELECT COUNT(*)
            FROM subscriptions
            WHERE status = 'completed'
              AND expires_at <= ?
            """,
            (iran_db_now_text(),)
        )
        expired_count = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(*) FROM subscriptions WHERE status = 'rejected'")
        rejected_count = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(*) FROM subscriptions WHERE status = 'cancelled'")
        cancelled_count = cursor.fetchone()[0]
        cursor.execute("SELECT COALESCE((SELECT SUM(price) FROM subscriptions WHERE status='completed'),0) + COALESCE((SELECT SUM(amount) FROM subscription_renewals),0)")
        total_revenue = cursor.fetchone()[0]
        # ۲۰ سفارش اخیر
        cursor.execute(
            """
            SELECT id, user_id, service_type, volume, price,
                   duration_months, status, created_at
            FROM subscriptions
            ORDER BY id DESC
            LIMIT 20
            """
        )
        recent_subs = cursor.fetchall()
        conn.close()
        status_icons = {
            "pending": "⏳",
            "completed": "✅",
            "rejected": "❌",
            "expired": "⌛",
            "cancelled": "🚫",
        }
        status_titles = {
            "pending": "در انتظار",
            "completed": "تکمیل شده",
            "rejected": "رد شده",
            "expired": "منقضی",
            "cancelled": "لغو شده",
        }
        text = (
            "📦 <b>مدیریت اشتراک‌ها</b>\n\n"
            "📊 <b>آمار کلی:</b>\n"
            f"✅ فعال: {active_count:,}\n"
            f"⏳ در انتظار: {pending_count:,}\n"
            f"⌛ منقضی شده: {expired_count:,}\n"
            f"❌ رد شده: {rejected_count:,}\n"
            f"🚫 لغو شده: {cancelled_count:,}\n"
            f"💰 درآمد کل: {total_revenue:,} تومان\n\n"
            "📋 <b>۲۰ سفارش اخیر:</b>\n\n"
        )
        keyboard = []
        for (
            sub_id, target_user_id, service_type, volume,
            price, duration_months, status, created_at
        ) in recent_subs:
            icon = status_icons.get(status, "❓")
            service_title = service_display_title(service_type)
            try:
                target_user = await context.bot.get_chat(target_user_id)
                user_title = (
                    f"@{target_user.username}"
                    if target_user.username
                    else target_user.full_name
                )
            except Exception:
                user_title = f"ID:{target_user_id}"
            text += (
                f"{icon} <b>#{sub_id}</b> {service_title} - {volume}\n"
                f"👤 {user_title} (<code>{target_user_id}</code>)\n"
                f"💰 {price:,} تومان | {iran_time_text(created_at)}\n\n"
            )
            keyboard.append([
                InlineKeyboardButton(
                    f"{icon} #{sub_id} | {user_title} | {volume}",
                    callback_data=f"admin_sub_detail_{sub_id}"
                )
            ])
        keyboard.append([
            InlineKeyboardButton(
                "🔄 بروزرسانی",
                callback_data="admin_subscriptions"
            )
        ])
        keyboard.append([
            InlineKeyboardButton(
                "🔙 پنل مدیریت",
                callback_data="admin_panel"
            )
        ])
        await safe_edit_message(query, 
            text,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="HTML"
        )


    # ماه‌های Tunnel
    elif query.data == "service_tunnel":
        keyboard = [
            [InlineKeyboardButton("‹  برگشت", callback_data="buy_subscription")]
        ]

        await safe_edit_message(query, 
            "🚇 Tunnel\n\n"
            "🌱 فعلاً چیزی برای این بخش آماده نیست.\n\nبه‌زودی گزینه‌های بیشتری اضافه می‌کنیم.",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    # ماه‌های مشترک
    elif query.data == "service_moshtarak":
        keyboard = [
            [InlineKeyboardButton("‹  برگشت", callback_data="buy_subscription")]
        ]

        await safe_edit_message(query, 
            "🔗 مشترک\n\n"
            "🌱 فعلاً چیزی برای این بخش آماده نیست.\n\nبه‌زودی گزینه‌های بیشتری اضافه می‌کنیم.",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    # روش‌های پرداخت
    elif query.data == "charge_ton":

        keyboard = [
            [
                InlineKeyboardButton(
                    "‹  برگشت",
                    callback_data="charge"
                )
            ]
        ]

        await safe_edit_message(query, 
            "💎 پرداخت از طریق TON\n\n"
            "🛠️ این بخش فعلاً آماده نیست؛ یه کم دیگه برگرد سراغش.",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    elif query.data == "charge_star":
            begin_input_state(context)
            context.user_data["waiting_for_star_amount"] = True

            keyboard = [
                [
                    InlineKeyboardButton(
                        "🔙  نه، برگرد",
                        callback_data="charge"
                    )
                ]
            ]

            await safe_edit_message(
                query,
                "⭐️ <b>شارژ با استارز</b>\n\n"
                "💎 <b>نرخ امروز:</b> هر ۱ استار = "
                f"<code>{STAR_TO_TOMAN:,}</code> تومان\n\n"
                "✨ حالا فقط تعداد استارزی که می‌خوای بدی رو بفرست.\n"
                "مثلاً <code>10</code> یا <code>50</code> 👇\n\n"
                "🧾 بعدش فاکتور رسمی تلگرام برات باز می‌شه "
                "و اگه پرداختش کنی، موجودی حسابت خودکار اضافه می‌شه.",
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="HTML"
            )

        # پردازش تعداد استار وارد شده و باز کردن Invoice تلگرام

    elif query.data == "charge_card":

        begin_input_state(context); context.user_data["waiting_for_amount"] = True
        context.user_data["payment_method"] = "card"

        keyboard = [
            [
                InlineKeyboardButton(
                    "‹  برگشت",
                    callback_data="charge"
                )
            ]
        ]

        await safe_edit_message(query, 
            "💳 افزایش موجودی از طریق کارت به کارت\n\n"
            "💰 مبلغ موردنظر را به تومان وارد کنید.\n\n"
            "مثال:\n"
            "250000",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    elif query.data == "features":
        # بجای امکانات، منوی اصلی نمایش داده می‌شه
        await safe_edit_message(query, 
            "🏠 <b>منوی اصلی</b>\n\n👇 بگو بریم کجا؟",
            reply_markup=main_menu_keyboard(user_id),
            parse_mode="HTML"
        )

    elif query.data == "about":
        # فعلاً placeholder - بعداً کامل می‌شه
        keyboard = [
            [
                InlineKeyboardButton(
                    "🏠 برگشت به خانه",
                    callback_data="back_main"
                )
            ]
        ]
        about_text = ("<b>✨ About the Bot\n\nHere you can find everything — just click.\n\nShadow_V2ray helps you browse products, add funds, and review your purchases in one place.\n\nWritten by @mmp_shadow</b>" if get_user_language(user_id) == "en" else "<b>✨ درباره ربات\n\nاینجا همه چیز پیدا میشه تو فقط کلیک کن.\n\n🌑 با Shadow_V2ray می‌تونی محصول بخری، حسابت رو شارژ کنی و خریدهای قبلی رو ببینی.\n\nنوشته شده توسط @mmp_shadow</b>")
        await safe_edit_message(
            query,
            about_text,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="HTML"
        )


    # دکمه برگشت به منوی اصلی
    elif query.data == "back_main":
        context.user_data.pop("waiting_for_broadcast", None)
        context.user_data.pop("waiting_for_amount", None)
        context.user_data.pop("payment_method", None)
        context.user_data.pop("deposit_amount", None)
        context.user_data.pop("waiting_for_cdn_price", None)
        context.user_data.pop("editing_cdn_volume", None)
        context.user_data.pop("wallet_action", None)
        context.user_data.pop("wallet_target", None)
        context.user_data.pop("wallet_amount", None)   # ⬅️ جدید
        context.user_data.pop("wallet_reason", None)   # ⬅️ جدید
        context.user_data.pop("waiting_for_wallet_reason", None)  # ⬅️ جدید
        context.user_data.pop("admin_search", None)
        context.user_data.pop("admin_messaging", None)
        await safe_edit_message(query, 
            "🏠 <b>منوی اصلی</b>\n\n👇 بگو بریم کجا؟",
            reply_markup=main_menu_keyboard(user_id),
            parse_mode="HTML"
        )

        # هندلر جدید در buttons:



    elif query.data.startswith("user_ban_"):

        if user_id != ADMIN_ID:
            await safe_query_answer(query, "❌ شما دسترسی ندارید.", show_alert=True)
            return
        target_user_id = int(query.data.replace("user_ban_", ""))
        # جلوگیری از بن کردن خود ادمین
        if target_user_id == ADMIN_ID:
            await safe_query_answer(query, 
                "❌ نمی‌توانید خود را مسدود کنید.",
                show_alert=True
            )
            return
        conn = db_connect()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT is_banned FROM users WHERE user_id = ?",
            (target_user_id,)
        )
        result = cursor.fetchone()
        if not result:
            conn.close()
            await safe_query_answer(query, "❌ کاربر پیدا نشد.", show_alert=True)
            return
        if result[0] == 1:
            conn.close()
            await safe_query_answer(query, 
                "⚠️ این کاربر از قبل مسدود است.",
                show_alert=True
            )
            return
        cursor.execute(
            "UPDATE users SET is_banned = 1 WHERE user_id = ?",
            (target_user_id,)
        )
        conn.commit()
        conn.close()
        # اطلاع به کاربر
        try:
            await context.bot.send_message(
                chat_id=target_user_id,
                text="🚫 حساب شما توسط مدیریت مسدود شد.\n\n"
                     "برای پیگیری با پشتیبانی تماس بگیرید."
            )
        except Exception:
            pass
        await safe_query_answer(query, "✅ کاربر مسدود شد.", show_alert=True)
        # ⬇️ صفحه تأیید با دکمه «رفع مسدودیت»
        await safe_edit_message(query, 
            f"🚫 کاربر <code>{target_user_id}</code> مسدود شد.",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "✅ رفع مسدودیت",
                        callback_data=f"user_unban_{target_user_id}"
                    )
                ],
                [
                    InlineKeyboardButton(
                        "👤 بازگشت به اطلاعات کاربر",
                        callback_data=f"admin_user_{target_user_id}"
                    )
                ],
                [
                    InlineKeyboardButton(
                        "⚙️ پنل مدیریت",
                        callback_data="admin_panel"
                    )
                ]
            ]),
            parse_mode="HTML"
        )


    #
    elif query.data.startswith("user_unban_"):
        if user_id != ADMIN_ID:
            await safe_query_answer(query, "❌ شما دسترسی ندارید.", show_alert=True)
            return
        target_user_id = int(query.data.replace("user_unban_", ""))
        conn = db_connect()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT is_banned FROM users WHERE user_id = ?",
            (target_user_id,)
        )
        result = cursor.fetchone()
        if not result:
            conn.close()
            await safe_query_answer(query, "❌ کاربر پیدا نشد.", show_alert=True)
            return
        if result[0] == 0:
            conn.close()
            await safe_query_answer(query, 
                "⚠️ این کاربر از قبل مسدود نیست.",
                show_alert=True
            )
            return
        cursor.execute(
            "UPDATE users SET is_banned = 0 WHERE user_id = ?",
            (target_user_id,)
        )
        conn.commit()
        conn.close()
        # اطلاع به کاربر
        try:
            await context.bot.send_message(
                chat_id=target_user_id,
                text=(
                    "✅ حساب شما از حالت مسدود خارج شد!\n\n"
                    "🎉 شما دوباره می‌توانید از ربات استفاده کنید.\n\n"
                    "برای ورود روی /start بزنید."
                )
            )
        except Exception:
            pass
        await safe_query_answer(query, "✅ مسدودیت کاربر رفع شد.", show_alert=True)
        # ⬇️ صفحه تأیید با دکمه «مسدود کردن مجدد»
        await safe_edit_message(query, 
            f"✅ مسدودیت کاربر <code>{target_user_id}</code> رفع شد.",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "🚫 مسدود کردن مجدد",
                        callback_data=f"user_ban_{target_user_id}"
                    )
                ],
                [
                    InlineKeyboardButton(
                        "👤 بازگشت به اطلاعات کاربر",
                        callback_data=f"admin_user_{target_user_id}"
                    )
                ],
                [
                    InlineKeyboardButton(
                        "⚙️ پنل مدیریت",
                        callback_data="admin_panel"
                    )
                ]
            ]),
            parse_mode="HTML"
        )




    # اشتراک های من
    elif query.data == "my_subscriptions":

        conn = db_connect()
        cursor = conn.cursor()

        cursor.execute(
            """
            SELECT id,
                   volume,
                   price,
                   duration_months,
                   status,
                   subscription_text,
                   expires_at
            FROM subscriptions
            WHERE user_id = ?
            ORDER BY id DESC
            """,
            (user_id,)
        )

        subscriptions = cursor.fetchall()
        conn.close()

        if not subscriptions:
            keyboard = [
                [
                    InlineKeyboardButton(
                        "🛒 خرید اشتراک",
                        callback_data="buy_subscription"
                    )
                ],
                [
                    InlineKeyboardButton(
                        "‹  برگشت",
                        callback_data="back_main"
                    )
                ]
            ]

            await safe_edit_message(query, 
                "📦 اشتراک‌های من\n\n"
                "❌ هنوز اشتراکی ندارید.",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )

            return

        text = "📦 اشتراک‌های من\n\n"

        now = iran_now()

        for (
                subscription_id,
                volume,
                price,
                duration_months,
                status,
                subscription_text,
                expires_at
        ) in subscriptions:

            # -------------------------
            # سفارش در انتظار
            # -------------------------
            if status == "pending":

                status_text = "⏳ در انتظار آماده‌سازی"

            # -------------------------
            # اشتراک تکمیل شده
            # -------------------------
            elif status == "completed":

                if expires_at:
                    try:
                        expiry_date = datetime.strptime(
                            expires_at,
                            "%Y-%m-%d %H:%M:%S"
                        )

                        if expiry_date <= now:
                            status_text = "❌ منقضی شده"
                        else:
                            status_text = "✅ فعال"

                    except ValueError:
                        status_text = "✅ آماده"

                else:
                    status_text = "✅ آماده"

            else:
                status_text = status

            text += (
                f"🔢 سفارش: {subscription_id}\n"
                f"🌐 نوع: {service_display_title(service_type)}\n"
                f"📦 حجم: {volume}\n"
                f"⏳ مدت: {duration_months} ماه\n"
                f"💰 مبلغ: {price:,} تومان\n"
                f"📌 وضعیت: {status_text}\n"
            )

            if expires_at:
                try:
                    expiry_date = datetime.strptime(
                        expires_at,
                        "%Y-%m-%d %H:%M:%S"
                    )

                    text += (
                        f"📅 تاریخ انقضا: "
                        f"{expiry_date.strftime('%Y/%m/%d %H:%M')}\n"
                    )

                except ValueError:
                    pass

            if status == "completed" and subscription_text:
                text += (
                    f"\n🔗 اشتراک:\n"
                    f"{subscription_text}\n"
                )

            text += "\n──────────────\n\n"

        keyboard = [
            [
                InlineKeyboardButton(
                    "🛒 خرید اشتراک جدید",
                    callback_data="buy_subscription"
                )
            ],
            [
                InlineKeyboardButton(
                    "‹  برگشت",
                    callback_data="back_main"
                )
            ]
        ]

        await safe_edit_message(query, 
            text,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="HTML"
        )



    # تایید از لیست «پرداخت‌های در انتظار»؛ CallbackQuery.data فقط خواندنی است،
    # بنابراین برای ورود به مسیر مشترک از proxy قابل‌تغییر استفاده می‌کنیم.
    elif query.data.startswith("approve_pending_"):
        deposit_id = query.data.rsplit("_", 1)[1]
        context.user_data["payment_return_pending"] = True
        proxy = MutableCallbackQueryProxy(query, f"approve_{deposit_id}")
        await buttons(update, context, _query_override=proxy)
        return

    elif query.data.startswith("approve_"):
        if user_id != ADMIN_ID:
            await safe_query_answer(query, 
                "❌ شما دسترسی به این بخش را ندارید.",
                show_alert=True
            )
            return
        deposit_id = int(query.data.replace("approve_", ""))
        conn = db_connect()
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT user_id, amount, status
            FROM deposits
            WHERE id = ?
            """,
            (deposit_id,)
        )
        deposit = cursor.fetchone()
        if not deposit:
            conn.close()
            await safe_query_answer(query, 
                "درخواست پیدا نشد.",
                show_alert=True
            )
            return
        target_user_id, amount, status = deposit
        if status != "pending":
            conn.close()
            await safe_query_answer(query, 
                "این درخواست قبلاً بررسی شده.",
                show_alert=True
            )
            return
        # ⬇️ به‌روزرسانی با ثبت ادمین و زمان
        cursor.execute(
            """
            UPDATE deposits
            SET status       = 'approved',
                processed_by = ?,
                processed_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (user_id, deposit_id)
        )
        cursor.execute(
            """
            INSERT INTO users (user_id, balance)
            VALUES (?, ?) ON CONFLICT(user_id)
            DO
            UPDATE SET balance = balance + excluded.balance
            """,
            (target_user_id, amount)
        )
        conn.commit()
        cursor.execute(
            "SELECT balance FROM users WHERE user_id = ?",
            (target_user_id,)
        )
        new_balance = cursor.fetchone()[0]
        conn.close()
        # اطلاع به کاربر
        try:
            await context.bot.send_message(
                chat_id=target_user_id,
                text=(
                    "✅ افزایش موجودی شما تأیید شد.\n\n"
                    f"💰 مبلغ واریزی: {amount:,} تومان\n"
                    f"💵 موجودی جدید: {new_balance:,} تومان\n"
                    f"🔢 شماره درخواست: {deposit_id}\n\n"
                    "🎉 مبلغ با موفقیت به حساب شما اضافه شد."
                )
            )
        except Exception as e:
            print("Deposit approval notification error:", e)
        user_info = await context.bot.get_chat(target_user_id)
        username = (
            f"@{user_info.username}"
            if user_info.username
            else "ندارد"
        )
        caption_text = (
            f"✅ تأیید شد\n\n"
            f"👤 نام: {user_info.full_name}\n"
            f"🔹 نام کاربری: {username}\n"
            f"🆔 ID: {target_user_id}\n"
            f"💰 مبلغ: {amount:,} تومان\n"
            f"💵 موجودی جدید: {new_balance:,} تومان\n"
            f"🔢 شماره درخواست: {deposit_id}"
        )
        # ⬇️ بعد از تأیید، خودِ عکس رسید باید حذف شود؛ فقط نتیجه متنی باقی بماند.
        try:
            back_cb = "admin_pending_payments" if context.user_data.pop("payment_return_pending", False) else "admin_payments"
            markup = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 بازگشت به پرداخت‌ها", callback_data=back_cb)]])
            message = getattr(query, "message", None)
            has_media = bool(message and any(getattr(message, name, None) for name in (
                "photo", "document", "video", "animation", "audio", "voice", "video_note", "sticker"
            )))
            if has_media:
                try:
                    await message.delete()
                except TelegramError as e:
                    print("Delete approved receipt message error:", e)
                await context_bot_send_text(query, caption_text, reply_markup=markup, parse_mode="HTML")
            else:
                await safe_edit_message(query, caption_text, reply_markup=markup, parse_mode="HTML")
        except TelegramError as e:
            print("Edit approve message error:", e)



    elif query.data.startswith("reject_pending_"):
        deposit_id = query.data.rsplit("_", 1)[1]
        context.user_data["payment_return_pending"] = True
        proxy = MutableCallbackQueryProxy(query, f"reject_{deposit_id}")
        await buttons(update, context, _query_override=proxy)
        return

    elif query.data.startswith("reject_"):

        if user_id != ADMIN_ID:
            await safe_query_answer(query, 
                "❌ شما دسترسی به این بخش را ندارید.",
                show_alert=True
            )
            return

        deposit_id = int(query.data.replace("reject_", ""))
        conn = db_connect()
        cursor = conn.cursor()
        cursor.execute("""
            SELECT user_id, amount, status
            FROM deposits
            WHERE id = ?
            """,
            (deposit_id,)
        )

        deposit = cursor.fetchone()
        if not deposit:
            conn.close()
            await safe_query_answer(query, 
                "درخواست پیدا نشد.",
                show_alert=True
            )
            return

        target_user_id, amount, status = deposit
        if status != "pending":
            conn.close()
            await safe_query_answer(query, 
                "این درخواست قبلاً بررسی شده.",
                show_alert=True
            )
            return

        # ⬇️ به‌روزرسانی با ثبت ادمین و زمان

        cursor.execute(
            """
            UPDATE deposits
            SET status       = 'rejected',
                processed_by = ?,
                processed_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (user_id, deposit_id)
        )

        conn.commit()
        conn.close()
        user_info = await context.bot.get_chat(target_user_id)
        username = (
            f"@{user_info.username}"
            if user_info.username
            else "ندارد"
        )
        caption_text = (
            f"❌ رد شد\n\n"
            f"👤 نام: {user_info.full_name}\n"
            f"🔹 نام کاربری: {username}\n"
            f"🆔 ID: {target_user_id}\n"
            f"💰 مبلغ: {amount:,} تومان\n"
            f"🔢 شماره درخواست: {deposit_id}"
        )

        # ⬇️ بعد از رد، خودِ عکس رسید باید حذف شود؛ فقط نتیجه متنی باقی بماند.
        try:
            back_cb = "admin_pending_payments" if context.user_data.pop("payment_return_pending", False) else "admin_payments"
            markup = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 بازگشت به پرداخت‌ها", callback_data=back_cb)]])
            message = getattr(query, "message", None)
            has_media = bool(message and any(getattr(message, name, None) for name in (
                "photo", "document", "video", "animation", "audio", "voice", "video_note", "sticker"
            )))
            if has_media:
                try:
                    await message.delete()
                except TelegramError as e:
                    print("Delete rejected receipt message error:", e)
                await context_bot_send_text(query, caption_text, reply_markup=markup, parse_mode="HTML")
            else:
                await safe_edit_message(query, caption_text, reply_markup=markup, parse_mode="HTML")
        except TelegramError as e:
            print("Edit reject message error:", e)

        # اطلاع به کاربر
        try:
            await context.bot.send_message(
                chat_id=target_user_id,
                text=(
                    "❌ درخواست افزایش موجودی شما رد شد.\n\n"
                    f"💰 مبلغ درخواست: {amount:,} تومان\n\n"
                    "در صورت نیاز مجدداً درخواست افزایش موجودی ثبت کنید."
                )
            )

        except Exception as e:
            print("Reject notification error:", e)


async def send_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """پیام همگانی: متن، عکس، فایل، ویدیو، صدا و هر media قابل copy."""
    if update.effective_user.id != ADMIN_ID: return
    message=update.message
    if context.bot_data.get("broadcast_running"):
        await message.reply_text("⚠️ یک پیام همگانی در حال ارسال است."); return
    context.bot_data["broadcast_running"]=True
    context.user_data.pop("waiting_for_broadcast",None)
    status=await message.reply_text("⏳ ارسال پیام همگانی شروع شد...")
    sent=failed=blocked=processed=0
    try:
        conn=db_connect(); total=conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]; conn.close()
        last_id=0
        while True:
            conn=db_connect(); rows=conn.execute("SELECT user_id FROM users WHERE user_id>? ORDER BY user_id LIMIT ?",(last_id,BROADCAST_BATCH_SIZE)).fetchall(); conn.close()
            if not rows: break
            last_id=rows[-1][0]
            for (uid,) in rows:
                try:
                    await context.bot.copy_message(chat_id=uid,from_chat_id=message.chat_id,message_id=message.message_id)
                    sent+=1
                except RetryAfter as e:
                    await asyncio.sleep(int(e.retry_after)+1)
                    try:
                        await context.bot.copy_message(chat_id=uid,from_chat_id=message.chat_id,message_id=message.message_id); sent+=1
                    except Exception: failed+=1
                except Forbidden:
                    failed+=1; blocked+=1
                except TelegramError as e:
                    failed+=1; print(f"Broadcast TelegramError {uid}: {e}")
                except Exception as e:
                    failed+=1; print(f"Broadcast error {uid}: {e}")
                processed+=1
                if processed % 25 == 0:
                    try: await status.edit_text(f"📢 ارسال پیام همگانی\n\n👥 کل: {total:,}\n📤 بررسی‌شده: {processed:,}\n✅ موفق: {sent:,}\n❌ ناموفق: {failed:,}\n🚫 بلاک: {blocked:,}")
                    except Exception: pass
                await asyncio.sleep(BROADCAST_DELAY)
        try: await status.edit_text(f"✅ ارسال تمام شد.\n\n👥 کل: {total:,}\n✅ موفق: {sent:,}\n❌ ناموفق: {failed:,}\n🚫 بلاک: {blocked:,}")
        except Exception: pass
    finally:
        context.bot_data["broadcast_running"]=False


# =========================
# دریافت ورودی متنی یا رسانه (پیام همگانی یا مبلغ)
# =========================
async def handle_text_input(
        update: Update,
        context: ContextTypes.DEFAULT_TYPE
):
    user_id = update.effective_user.id
    if await product_text_router(update, context):
        return

    # =========================
    # جستجوی کاربر توسط ادمین — باید قبل از سایر stateها پردازش شود
    # =========================
    if context.user_data.get("admin_search"):
        if user_id != ADMIN_ID:
            context.user_data.pop("admin_search", None)
            return

        search_text = (update.message.text or "").strip()
        if not search_text:
            await update.message.reply_text("❌ لطفاً آیدی عددی یا یوزرنیم را وارد کنید.")
            return

        search_text = search_text.lstrip("@").strip()
        conn = db_connect()
        cursor = conn.cursor()
        try:
            if search_text.isdigit():
                cursor.execute(
                    "SELECT user_id, balance, is_banned, username, full_name FROM users WHERE user_id = ? LIMIT 1",
                    (int(search_text),)
                )
            else:
                cursor.execute(
                    "SELECT user_id, balance, is_banned, username, full_name FROM users WHERE LOWER(REPLACE(COALESCE(username, ''), '@', '')) LIKE LOWER(?) LIMIT 20",
                    (f"%{search_text}%",)
                )
            users = cursor.fetchall()
        finally:
            conn.close()

        context.user_data.pop("admin_search", None)

        if not users:
            await update.message.reply_text(
                "❌ کاربری با این مشخصات پیدا نشد.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🔍 تلاش مجدد", callback_data="admin_search")
                ], [
                    InlineKeyboardButton("🔙 پنل مدیریت", callback_data="admin_panel")
                ]])
            )
            return

        if len(users) == 1:
            target_user_id, balance, is_banned, username, full_name = users[0]
            ban_status = "🚫 مسدود" if is_banned else "✅ فعال"
            keyboard = [
                [InlineKeyboardButton("👤 مشاهده کاربر", callback_data=f"admin_user_{target_user_id}")],
                [InlineKeyboardButton("🔙 پنل مدیریت", callback_data="admin_panel")]
            ]
            await update.message.reply_text(
                "🔎 <b>نتیجه جستجو</b>\n\n"
                f"🆔 ID: <code>{target_user_id}</code>\n"
                f"👤 نام: {full_name or 'نامشخص'}\n"
                f"🔗 یوزرنیم: @{username.lstrip('@') if username else 'ندارد'}\n"
                f"💰 موجودی: {balance:,} تومان\n"
                f"📌 وضعیت: {ban_status}",
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="HTML"
            )

            return

        keyboard = []
        for target_user_id, balance, is_banned, username, full_name in users:
            title = f"@{username.lstrip('@')}" if username else (full_name or f"ID:{target_user_id}")
            keyboard.append([InlineKeyboardButton(
                f"{title} | ID:{target_user_id}",
                callback_data=f"admin_user_{target_user_id}"
            )])
        keyboard.append([InlineKeyboardButton("🔙 پنل مدیریت", callback_data="admin_panel")])
        await update.message.reply_text(
            f"🔎 <b>{len(users)} کاربر پیدا شد:</b>",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="HTML"
        )
        return

    # ورودی‌های پنل پلن سفارشی
    raw_text=(update.message.text or "").strip() if update.message else ""
    custom_price_states={"waiting_custom_base":"base_gb_price","waiting_custom_monthly":"monthly_price","waiting_custom_day":"extra_day_price"}
    for state,col in custom_price_states.items():
        if context.user_data.get(state):
            if user_id!=ADMIN_ID: context.user_data.pop(state,None); return
            val=raw_text.replace(',','')
            if not val.isdigit() or int(val)<0:
                await update.message.reply_text("❌ مبلغ نامعتبر است؛ عدد صفر یا بزرگ‌تر بفرستید."); return
            conn=db_connect(); conn.execute(f"UPDATE custom_plan_settings SET {col}=?,updated_at=CURRENT_TIMESTAMP WHERE id=1",(int(val),)); conn.commit(); conn.close(); context.user_data.pop(state,None)
            await update.message.reply_text("✅ تنظیم ذخیره شد. برای مشاهده پنل پلن سفارشی روی دکمه زیر بزنید.",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🌍 مدیریت پلن سفارشی",callback_data="admin_custom_plan")]])); return
    if context.user_data.get('waiting_custom_country_name'):
        if user_id!=ADMIN_ID: context.user_data.pop('waiting_custom_country_name',None); return
        if not raw_text or len(raw_text)>80:
            await update.message.reply_text("نام کشور باید بین 1 تا 80 نویسه باشد."); return
        try:
            conn=db_connect(); cur=conn.execute("INSERT INTO custom_countries(title,gb_price,active) VALUES(?,0,1)",(raw_text,)); cid=cur.lastrowid; conn.commit(); conn.close()
        except sqlite3.IntegrityError:
            await update.message.reply_text("❌ این نام قبلاً ثبت شده است."); return
        context.user_data.pop('waiting_custom_country_name',None)
        await update.message.reply_text("✅ کشور اضافه شد. حالا قیمت هر گیگ و Inboundهای مربوط به آن را تنظیم کنید.",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⚙️ تنظیم کشور",callback_data=f"admin_custom_country_{cid}")],[InlineKeyboardButton("🌍 پنل پلن سفارشی",callback_data="admin_custom_plan")]])); return
    if context.user_data.get('waiting_custom_country_price'):
        if user_id!=ADMIN_ID: context.user_data.pop('waiting_custom_country_price',None); return
        val=raw_text.replace(',',''); cid=int(context.user_data.get('custom_admin_country_id') or 0)
        if not val.isdigit() or int(val)<0: await update.message.reply_text("❌ قیمت نامعتبر است؛ عدد صفر یا بزرگ‌تر بفرستید."); return
        conn=db_connect(); conn.execute("UPDATE custom_countries SET gb_price=? WHERE id=?",(int(val),cid)); conn.commit(); conn.close(); context.user_data.pop('waiting_custom_country_price',None); context.user_data.pop('custom_admin_country_id',None)
        await update.message.reply_text("✅ قیمت کشور ذخیره شد.",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⚙️ برگشت به کشور",callback_data=f"admin_custom_country_{cid}")]])); return
    if context.user_data.get('custom_waiting_volume'):
        raw=raw_text.replace(',','')
        if not raw.isdigit() or not 1<=int(raw)<=10000: await update.message.reply_text("❌ حجم باید عددی بین 1 تا 10000 گیگابایت باشد."); return
        context.user_data['custom_volume']=int(raw); context.user_data.pop('custom_waiting_volume',None)
        await update.message.reply_text("حجم ثبت شد؛ مدت اشتراک را انتخاب کنید:",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(f"{m} ماه",callback_data=f"custom_month_{m}")] for m in range(1,13)]+[[InlineKeyboardButton("❌ انصراف",callback_data="custom_cancel")]])); return

    # =========================
    # مدیریت داینامیک سرویس‌ها و پلن‌ها
    # =========================
    if context.user_data.get("waiting_for_renewal_days"):
        sid=int(context.user_data.get("renewal_sub_id") or 0)
        raw=(update.message.text or "").strip()
        if not raw.isdigit() or int(raw)<=0 or int(raw)>3650:
            await update.message.reply_text("❌ تعداد روز نامعتبر است. یک عدد بین 1 تا 3650 ارسال کنید.\nمثال: 7 یا 30")
            return
        await process_renew_days_from_message(update, context, sid, int(raw))
        return

    if context.user_data.get("waiting_for_extra_traffic"):
        sid=int(context.user_data.get("renewal_sub_id") or 0); raw=(update.message.text or "").strip().replace(",","")
        m=re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)",raw)
        if not m or float(m.group(1))<=0:
            await update.message.reply_text("❌ حجم نامعتبر است. فقط عدد وارد کنید؛ مثال: 5 یا 10"); return
        gb=float(m.group(1)); row=await _get_subscription_for_user(user_id,sid)
        if not row: await update.message.reply_text("❌ اشتراک پیدا نشد."); return
        price_per_gb=(get_custom_subscription_rates(sid)[0] if row[1]=='custom' else get_service_extra_gb_price(row[1])); amount=int(round(gb*price_per_gb))
        if amount<=0: await update.message.reply_text("❌ قیمت حجم اضافه برای این سرویس تنظیم نشده است."); return
        charged=await _charge_user(user_id,amount,f'خرید {gb:g}GB حجم اضافه اشتراک #{sid}')
        if not charged or charged[0] is False:
            bal=charged[1] if charged else 0; await update.message.reply_text(f"❌ موجودی کافی نیست.\n\n💰 هزینه: {amount:,} تومان\n💵 موجودی: {bal:,} تومان"); return
        try:
            add_bytes = int(gb * 1024**3)
            # در نسخه‌های جدید X-UI bulkAdjust بهترین مسیر است؛ بعضی buildها
            # آن endpoint را ندارند یا payload متفاوتی می‌خواهند. در آن حالت
            # کلاینت فعلی را می‌خوانیم و totalGB را مستقیم افزایش می‌دهیم.
            bulk_error = None
            try:
                await xui_bulk_adjust(row[7], add_bytes=add_bytes)
            except Exception as bulk_exc:
                bulk_error = bulk_exc
                # Fallback برای buildهایی که bulkAdjust را ندارند/متفاوت پیاده‌سازی کرده‌اند.
                client_before = await xui_find_client(row[7])
                if not client_before:
                    raise bulk_exc
                current_total = int(client_before.get('totalGB') or row[9] or 0)
                if current_total <= 0:
                    raise RuntimeError(f"X-UI حجم فعلی را محدود گزارش نکرد: {bulk_exc}")
                client_before['totalGB'] = current_total + add_bytes
                await xui_update_client(row[7], client_before)

            client = await xui_find_client(row[7])
            if not client:
                raise RuntimeError("کلاینت X-UI بعد از افزایش حجم قابل دریافت نیست")
            total = int(client.get('totalGB') or 0)
            traffic = client.get('traffic') or {}
            used = int(traffic.get('up',0) or 0) + int(traffic.get('down',0) or 0)
            # اگر پاسخ X-UI traffic را برنگرداند، مقدار قبلی DB را نگه می‌داریم.
            if not traffic:
                used = int(row[10] or 0)
            conn=db_connect(); conn.execute("UPDATE subscriptions SET xui_total_bytes=?,xui_used_bytes=?,low_traffic_notified=0 WHERE id=?",(total,used,sid)); conn.commit(); conn.close()
        except Exception as exc:
            await _refund_user(user_id,amount,f'بازگشت وجه خرید حجم ناموفق #{sid}'); await update.message.reply_text(f"❌ افزایش حجم انجام نشد و مبلغ برگشت داده شد.\n{str(exc)[:180]}"); return
        context.user_data.pop("waiting_for_extra_traffic",None); context.user_data.pop("renewal_sub_id",None)
        await update.message.reply_text(f"✅ <b>{gb:g} GB حجم اضافه شد.</b>\n\n💰 مبلغ: {amount:,} تومان\n💵 موجودی جدید: {charged[1]:,} تومان",parse_mode="HTML",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📦 مشاهده اشتراک",callback_data=f"my_subscription_detail_{sid}")]])); return

    if context.user_data.get("waiting_for_service_price_percentage"):
        if user_id != ADMIN_ID: return
        raw=(update.message.text or "").strip()
        # پشتیبانی از ارقام فارسی/عربی و درصد اعشاری مثل 0.908 یا ۰٫۹۰۸
        digit_map = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩٫٪", "01234567890123456789.%")
        raw=raw.translate(digit_map).replace("%", "").replace("٪", "").replace(" ", "")
        if "," in raw and "." not in raw:
            raw=raw.replace(",", ".")
        else:
            raw=raw.replace(",", "")
        try:
            pct=Decimal(raw)
            if not pct.is_finite():
                raise InvalidOperation
        except (InvalidOperation, ValueError):
            await update.message.reply_text("❌ لطفاً درصد را به‌صورت عدد وارد کنید؛ مثلاً 15 یا 0.908")
            return
        sid=int(context.user_data.get("service_price_percentage_service_id") or 0)
        direction=context.user_data.get("service_price_percentage_direction")
        if direction not in ("up", "down") or pct < Decimal("0.0001") or pct > Decimal("1000" if direction == "up" else "100"):
            await update.message.reply_text("❌ درصد نامعتبر است. افزایش: بیشتر از 0 تا 1000؛ کاهش: بیشتر از 0 تا 100. اعشار هم مجاز است.")
            return
        delta=pct if direction == "up" else -pct
        conn=db_connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            plans=conn.execute("SELECT id,volume,price FROM service_plans WHERE service_id=?",(sid,)).fetchall()
            if not plans:
                conn.rollback()
                await update.message.reply_text("❌ برای این سرویس پلنی پیدا نشد.")
                return
            for pid,volume,price in plans:
                new_price=max(1, int((Decimal(int(price)) * (Decimal("100") + delta) / Decimal("100")).quantize(Decimal("1"), rounding=ROUND_HALF_UP)))
                conn.execute("UPDATE service_plans SET price=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",(new_price,pid))
                sync_legacy_plan_table(conn,sid,volume,price=new_price)
            conn.commit()
            code=conn.execute("SELECT code,title FROM service_catalog WHERE id=?",(sid,)).fetchone()
        except Exception as exc:
            conn.rollback()
            await update.message.reply_text(f"❌ تغییر قیمت انجام نشد: {str(exc)[:180]}")
            return
        finally:
            conn.close()
        context.user_data.pop("waiting_for_service_price_percentage",None)
        context.user_data.pop("service_price_percentage_service_id",None)
        context.user_data.pop("service_price_percentage_direction",None)
        sign="+" if delta > 0 else "−"
        buttons=[[InlineKeyboardButton("🧩 بازگشت به پلن‌های سرویس",callback_data=f"admin_service_id_{sid}")],
                 [InlineKeyboardButton("🔙 مدیریت سرویس‌ها",callback_data="admin_services")]]
        pct_text=format(pct, "f").rstrip("0").rstrip(".") if "." in format(pct, "f") else format(pct, "f")
        await update.message.reply_text(f"✅ قیمت تمام {len(plans)} پلن سرویس {html.escape(str(code[1] if code else sid))} به میزان {sign}{pct_text}% تغییر کرد.",parse_mode="HTML",reply_markup=InlineKeyboardMarkup(buttons))
        return

    if context.user_data.get("waiting_for_service_extra_day_price") or context.user_data.get("waiting_for_service_extra_gb_price"):
        if user_id!=ADMIN_ID: return
        raw=(update.message.text or "").replace(",","").strip()
        try: value=int(raw)
        except ValueError: await update.message.reply_text("❌ فقط عدد وارد کنید."); return
        if value<0: await update.message.reply_text("❌ قیمت نمی‌تواند منفی باشد."); return
        sid=int(context.user_data.get("service_admin_id") or 0); field="extra_day_price" if context.user_data.get("waiting_for_service_extra_day_price") else "extra_gb_price"
        conn=db_connect(); conn.execute(f"UPDATE service_catalog SET {field}=? WHERE id=?",(value,sid)); conn.commit(); conn.close(); context.user_data.pop("waiting_for_service_extra_day_price",None); context.user_data.pop("waiting_for_service_extra_gb_price",None); context.user_data.pop("service_admin_id",None); None; await update.message.reply_text("✅ قیمت ذخیره شد.",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🧩 مدیریت سرویس‌ها",callback_data="admin_services")]])); return

    if context.user_data.get("waiting_for_service_create"):
        if user_id != ADMIN_ID: return
        raw=(update.message.text or "").strip()
        parts=[x.strip() for x in raw.split("|",1)]
        if len(parts)!=2 or not service_slug_valid(parts[0]) or not parts[1]:
            await update.message.reply_text("❌ فرمت نامعتبر است. مثال: dns | 🧭 DNS"); return
        code=parts[0].lower(); title=parts[1][:80]
        conn=db_connect()
        try:
            conn.execute("INSERT INTO service_catalog(code,title,sort_order) VALUES(?,?,?)",(code,title,100))
            conn.commit()
        except sqlite3.IntegrityError:
            conn.rollback(); await update.message.reply_text("❌ این کد سرویس قبلاً وجود دارد."); return
        finally: conn.close()
        context.user_data.pop("waiting_for_service_create",None); None
        await update.message.reply_text("✅ نوع اشتراک ساخته شد. حالا می‌توانید حجم‌ها و قیمت‌هایش را اضافه کنید.",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🧩 مدیریت سرویس‌ها",callback_data="admin_services")]])); return

    if context.user_data.get("waiting_for_service_plan_create"):
        if user_id != ADMIN_ID: return
        raw=(update.message.text or "").replace(",", " ").split()
        if len(raw)!=3:
            await update.message.reply_text("❌ فرمت: MONTHS VOLUME PRICE — مثال: 1 15G 45000"); return
        try: months=int(raw[0]); price=int(raw[2])
        except ValueError: await update.message.reply_text("❌ ماه و قیمت باید عدد باشند."); return
        volume=raw[1].upper()
        if months<1 or price<=0 or (volume!="UNLIMITED" and not re.fullmatch(r"[0-9]+(?:\.[0-9]+)?G",volume)):
            await update.message.reply_text("❌ مقدار پلن نامعتبر است."); return
        sid=int(context.user_data.get("service_admin_id") or 0); conn=db_connect()
        try:
            conn.execute("INSERT INTO service_plans(service_id,volume,price,duration_months,active) VALUES(?,?,?,?,1) ON CONFLICT(service_id,volume,duration_months) DO UPDATE SET price=excluded.price,active=1,updated_at=CURRENT_TIMESTAMP",(sid,volume,price,months)); sync_legacy_plan_table(conn,sid,volume,price=price); conn.commit()
        except Exception as exc:
            conn.rollback(); await update.message.reply_text(f"❌ ثبت پلن انجام نشد: {exc}"); return
        finally: conn.close()
        context.user_data.pop("waiting_for_service_plan_create",None); context.user_data.pop("service_admin_id",None); None; await update.message.reply_text("✅ پلن ثبت شد.",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🧩 مدیریت سرویس‌ها",callback_data="admin_services")]])); return

    if context.user_data.get("waiting_for_service_plan_price_edit"):
        if user_id != ADMIN_ID: return
        raw=(update.message.text or "").replace(",", "").strip()
        try: price=int(raw)
        except ValueError:
            await update.message.reply_text("❌ قیمت باید فقط عدد باشد."); return
        if price<=0:
            await update.message.reply_text("❌ قیمت باید بیشتر از صفر باشد."); return
        pid=int(context.user_data.get("service_admin_plan_id") or 0)
        conn=db_connect()
        try:
            row=conn.execute("SELECT service_id,volume,active FROM service_plans WHERE id=?",(pid,)).fetchone()
            if not row:
                conn.rollback(); await update.message.reply_text("❌ گزینه پیدا نشد."); return
            if not int(row[2]):
                conn.rollback(); await update.message.reply_text("❌ این گزینه غیرفعال است. ابتدا آن را فعال کنید."); return
            conn.execute("UPDATE service_plans SET price=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",(price,pid))
            svc=conn.execute("SELECT code FROM service_catalog WHERE id=?",(row[0],)).fetchone()
            sync_legacy_plan_table(conn, row[0], row[1], price=price)
            conn.commit()
        finally: conn.close()
        context.user_data.pop("waiting_for_service_plan_price_edit",None); context.user_data.pop("service_admin_plan_id",None)
        await update.message.reply_text("✅ قیمت با موفقیت تغییر کرد.",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🧩 مدیریت سرویس‌ها",callback_data="admin_services")]])); return

    if context.user_data.get("waiting_for_service_plan_volume_edit"):
        if user_id != ADMIN_ID: return
        volume=(update.message.text or "").strip().upper()
        if not volume.endswith("G") or len(volume)>24 or not volume[:-1].isdigit():
            await update.message.reply_text("❌ حجم نامعتبر است. مثال: <code>20G</code>",parse_mode="HTML"); return
        pid=int(context.user_data.get("service_admin_plan_id") or 0)
        conn=db_connect()
        try:
            row=conn.execute("SELECT service_id,price,volume,active FROM service_plans WHERE id=?",(pid,)).fetchone()
            if not row:
                conn.rollback(); await update.message.reply_text("❌ گزینه پیدا نشد."); return
            if not int(row[3]):
                conn.rollback(); await update.message.reply_text("❌ این گزینه غیرفعال است. ابتدا آن را فعال کنید."); return
            dup=conn.execute("SELECT id FROM service_plans WHERE service_id=? AND volume=? AND duration_months=(SELECT duration_months FROM service_plans WHERE id=?) AND id<>?",(row[0],volume,pid,pid)).fetchone()
            if dup:
                conn.rollback(); await update.message.reply_text("❌ این حجم قبلاً برای همین سرویس وجود دارد."); return
            conn.execute("UPDATE service_plans SET volume=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",(volume,pid))
            svc=conn.execute("SELECT code FROM service_catalog WHERE id=?",(row[0],)).fetchone()
            sync_legacy_plan_table(conn, row[0], volume, price=row[1])
            conn.commit()
        finally: conn.close()
        context.user_data.pop("waiting_for_service_plan_volume_edit",None); context.user_data.pop("service_admin_plan_id",None)
        await update.message.reply_text("✅ حجم با موفقیت تغییر کرد.",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🧩 مدیریت سرویس‌ها",callback_data="admin_services")]])); return

    if context.user_data.get("waiting_for_service_plan_edit"):
        if user_id != ADMIN_ID: return
        raw=(update.message.text or "").replace(",", " ").split()
        if len(raw)!=2 or not raw[0].upper().endswith("G"):
            await update.message.reply_text("❌ فرمت: VOLUME PRICE — مثال: 20G 70000"); return
        try: price=int(raw[1]); pid=int(context.user_data.get("service_admin_plan_id") or 0)
        except ValueError: await update.message.reply_text("❌ اطلاعات ویرایش نامعتبر است."); return
        if price<=0: await update.message.reply_text("❌ قیمت باید بیشتر از صفر باشد."); return
        volume=raw[0].upper(); conn=db_connect()
        try:
            existing=conn.execute("SELECT service_id,active FROM service_plans WHERE id=?",(pid,)).fetchone()
            if not existing: conn.rollback(); await update.message.reply_text("❌ گزینه پیدا نشد."); return
            if not int(existing[1]): conn.rollback(); await update.message.reply_text("❌ این گزینه غیرفعال است. ابتدا آن را فعال کنید."); return
            dup=conn.execute("SELECT id FROM service_plans WHERE service_id=? AND volume=? AND duration_months=(SELECT duration_months FROM service_plans WHERE id=?) AND id<>?",(existing[0],volume,pid,pid)).fetchone()
            if dup: conn.rollback(); await update.message.reply_text("❌ این حجم قبلاً برای همین سرویس وجود دارد."); return
            conn.execute("UPDATE service_plans SET volume=?,price=?,active=1,updated_at=CURRENT_TIMESTAMP WHERE id=?",(volume,price,pid))
            svc_code=conn.execute("SELECT s.code FROM service_catalog s JOIN service_plans p ON p.service_id=s.id WHERE p.id=?",(pid,)).fetchone()
            sync_legacy_plan_table(conn, existing[0], volume, price=price)
            conn.commit()
        finally: conn.close()
        context.user_data.pop("waiting_for_service_plan_edit",None); context.user_data.pop("service_admin_plan_id",None); None
        await update.message.reply_text("✅ حجم و قیمت با موفقیت تغییر کرد.",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🧩 مدیریت سرویس‌ها",callback_data="admin_services")]])); return

    if context.user_data.get("waiting_for_service_code_edit") and context.user_data.get("service_admin_id"):
        if user_id != ADMIN_ID: return
        sid=int(context.user_data.get("service_admin_id") or 0)
        new_code=normalize_service_code(update.message.text)
        if not valid_service_code(new_code):
            await update.message.reply_text("❌ کد نامعتبر است. فرمت مجاز: <code>dns</code> یا <code>my_service_2</code>\nکد باید با حرف انگلیسی شروع شود و فقط شامل a-z، عدد و _ باشد.",parse_mode="HTML"); return
        conn=db_connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row=conn.execute("SELECT code FROM service_catalog WHERE id=?",(sid,)).fetchone()
            if not row:
                conn.rollback(); await update.message.reply_text("❌ سرویس پیدا نشد."); return
            old_code=str(row[0])
            if new_code == old_code:
                conn.rollback(); await update.message.reply_text("❌ کد جدید با کد فعلی یکسان است."); return
            dup=conn.execute("SELECT id FROM service_catalog WHERE code=? AND id<>?",(new_code,sid)).fetchone()
            if dup:
                conn.rollback(); await update.message.reply_text("❌ این کد سرویس قبلاً استفاده شده است."); return
            # کد قبلی را به عنوان alias نگه می‌داریم تا callbackهای قدیمی
            # و مسیرهای سازگاری بعد از تغییر کد همچنان همان سرویس را پیدا کنند.
            conn.execute("INSERT OR IGNORE INTO service_code_aliases(alias_code,service_id) VALUES(?,?)", (old_code, sid))
            conn.execute("UPDATE service_catalog SET code=? WHERE id=?",(new_code,sid))
            # اگر کد جدید قبلاً alias سرویس دیگری باشد، اجازه تغییر نمی‌دهیم.
            alias_owner = conn.execute("SELECT service_id FROM service_code_aliases WHERE alias_code=?", (new_code,)).fetchone()
            if alias_owner and int(alias_owner[0]) != sid:
                raise ValueError("new service code is already used as a historical alias")
            conn.execute("DELETE FROM service_code_aliases WHERE alias_code=? AND service_id=?", (new_code, sid))
            conn.execute("UPDATE subscriptions SET service_type=? WHERE service_type=?",(new_code,old_code))
            conn.execute("UPDATE subscription_renewals SET service_type=? WHERE service_type=?",(new_code,old_code))
            conn.commit()
        except Exception as exc:
            try: conn.rollback()
            except Exception: pass
            print(f"Service code edit error (service={sid}): {exc}")
            await update.message.reply_text("❌ تغییر کد انجام نشد. هیچ بخشی از اطلاعات تغییر نکرد.")
            return
        finally:
            conn.close()
        context.user_data.pop("waiting_for_service_code_edit",None); context.user_data.pop("service_admin_id",None)
        await update.message.reply_text(f"✅ کد سرویس از <code>{html.escape(old_code)}</code> به <code>{html.escape(new_code)}</code> تغییر کرد و سوابق مرتبط هماهنگ شد.",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🧩 مدیریت سرویس‌ها",callback_data="admin_services")]]),parse_mode="HTML"); return

    if context.user_data.get("waiting_for_service_rename") and context.user_data.get("service_admin_id"):
        if user_id != ADMIN_ID: return
        sid=int(context.user_data.pop("service_admin_id")); context.user_data.pop("waiting_for_service_rename",None); title=(update.message.text or "").strip()[:80]
        if not title: await update.message.reply_text("❌ عنوان نامعتبر است."); return
        conn=db_connect(); conn.execute("UPDATE service_catalog SET title=? WHERE id=?",(title,sid)); conn.commit(); conn.close(); None
        await update.message.reply_text("✅ عنوان سرویس تغییر کرد.",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🧩 مدیریت سرویس‌ها",callback_data="admin_services")]])); return

    # =========================
    # ساخت کد تخفیف توسط ادمین
    # =========================
    if context.user_data.get("waiting_for_discount_create"):
        if user_id != ADMIN_ID: return
        raw=(update.message.text or '').strip().split()
        if len(raw) not in (4,5,6,7):
            await update.message.reply_text("❌ فرمت اشتباه است.\nCODE PERCENT VALUE MAX_USES\nیا: CODE PERCENT VALUE MAX_PER_USER MAX_USES\nیا: CODE PERCENT VALUE MAX_PER_USER MAX_USES MIN_PURCHASE\nیا کامل: CODE PERCENT VALUE MAX_PER_USER MAX_USES MIN_PURCHASE MAX_DISCOUNT")
            return
        code,typ,val_s,first_limit_s=raw[:4]
        # فرمت ۴تایی قدیمی: CODE TYPE VALUE MAX_USES
        # فرمت ۵ تا ۷تایی جدید و خوانا: CODE TYPE VALUE MAX_PER_USER MAX_USES [MIN_PURCHASE] [MAX_DISCOUNT]
        if len(raw) == 4:
            per_s = '1'
            max_s = first_limit_s
        else:
            per_s = first_limit_s
            max_s = raw[4]
        min_s = raw[5] if len(raw) >= 6 else '0'
        cap_s = raw[6] if len(raw) >= 7 else '0'
        code=code.upper()
        if not code.isalnum() or len(code)>32:
            await update.message.reply_text("❌ کد فقط حروف و عدد و حداکثر ۳۲ کاراکتر باشد."); return
        typ=typ.lower()
        try:
            val=int(val_s); maxuses=int(max_s); per_user=int(per_s); min_purchase=int(min_s); max_discount=int(cap_s)
        except ValueError:
            await update.message.reply_text("❌ همه مقادیر عددی باید عدد صحیح باشند."); return
        if (typ not in ('percent','fixed') or val<0 or maxuses<0 or per_user<0 or min_purchase<0 or max_discount<0
            or (typ=='percent' and val>100) or (typ=='percent' and max_discount>0 and max_discount<1)):
            await update.message.reply_text("❌ مقادیر کد تخفیف نامعتبر است."); return
        conn=db_connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            # اگر همان نام قبلاً وجود داشته، نسخه فعال قبلی را می‌بندیم و نسخه جدید را مستقل می‌سازیم.
            old=conn.execute("SELECT COUNT(*) FROM discount_codes WHERE code=? AND active=1 AND deleted_at IS NULL",(code,)).fetchone()[0]
            conn.execute("UPDATE discount_codes SET active=0 WHERE code=? AND active=1 AND deleted_at IS NULL",(code,))
            conn.execute("""INSERT INTO discount_codes(code,discount_type,value,max_uses,max_uses_per_user,min_purchase,max_discount,active)
                           VALUES(?,?,?,?,?,?,?,1)""",(code,typ,val,maxuses,per_user,min_purchase,max_discount))
            conn.commit()
        except sqlite3.IntegrityError:
            conn.rollback(); conn.close(); await update.message.reply_text("❌ ساخت نسخه جدید کد انجام نشد."); return
        except sqlite3.OperationalError:
            conn.rollback(); conn.close(); await update.message.reply_text("❌ دیتابیس موقتاً مشغول است؛ دوباره تلاش کنید."); return
        finally:
            try: conn.close()
            except Exception: pass
        context.user_data.pop('waiting_for_discount_create',None)
        msg=f"✅ نسخه جدید کد <b>{html.escape(code)}</b> ساخته شد." + ("\n🔄 نسخه فعال قبلی غیرفعال شد." if old else "")
        await update.message.reply_text(msg,parse_mode='HTML',reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('🎟 مدیریت تخفیف‌ها',callback_data='admin_discounts')]])); return

    # =========================
    # اعمال کد تخفیف توسط کاربر
    # =========================
    if context.user_data.get("waiting_for_discount_code"):
        if context.user_data.get('custom_waiting_discount'):
            code=(update.message.text or '').strip().upper()
            price=int(context.user_data.get('custom_quote_price') or 0)
            conn=db_connect()
            try: result,err=validate_discount(conn,code,user_id,price)
            finally: conn.close()
            if err:
                await update.message.reply_text(err); return
            context.user_data['discount_code']=result['code']; context.user_data['discount_price']=result['final']; context.user_data['discount_amount']=result['discount']; context.user_data['custom_waiting_discount']=False
            await update.message.reply_text(f"✅ تخفیف اعمال شد.\nقیمت اصلی: {price:,} تومان\nتخفیف: {result['discount']:,} تومان\nقیمت نهایی: {result['final']:,} تومان",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✅ ادامه و تأیید",callback_data="custom_confirm")],[InlineKeyboardButton("❌ انصراف",callback_data="custom_cancel")]])); return
        code=(update.message.text or '').strip().upper()
        volume=context.user_data.get('discount_volume')
        service_type=context.user_data.get('discount_service_type','cdn')
        months=int(context.user_data.get('discount_months',1) or 1)
        if not volume or service_type not in SERVICE_CONFIG:
            context.user_data.pop('waiting_for_discount_code',None)
            await update.message.reply_text('❌ اطلاعات خرید منقضی شده است. لطفاً سرویس را دوباره انتخاب کنید.')
            return
        conn=db_connect()
        try:
            # قیمت را با سرویس، حجم و مدت دقیق می‌خوانیم؛ ممکن است یک حجم برای مدت‌های مختلف قیمت متفاوتی داشته باشد.
            plan=get_service_plan(conn,service_type,volume,months,True)
            if plan:
                base_price=int(plan[2])
                result,err=validate_discount(conn,code,user_id,base_price)
            else:
                base_price=None
                result,err=None,'❌ پلن اشتراک پیدا نشد یا دیگر فعال نیست. لطفاً خرید را از ابتدا انجام دهید.'
        finally:
            conn.close()
        if not plan:
            context.user_data.pop('waiting_for_discount_code',None)
            await update.message.reply_text(err)
            return
        if err:
            await update.message.reply_text(err)
            return
        context.user_data.pop('waiting_for_discount_code',None)
        context.user_data['discount_code']=result['code']
        context.user_data['discount_price']=result['final']
        context.user_data['discount_amount']=result['discount']
        context.user_data['discount_service_type']=service_type
        context.user_data['discount_volume']=volume
        context.user_data['discount_months']=months
        await update.message.reply_text(
            f"✅ کد تخفیف اعمال شد.\n\n"
            f"🌐 سرویس: {service_display_title(service_type)}\n"
            f"📦 حجم: {volume}\n"
            f"⏳ مدت: {months} ماه\n"
            f"💰 قیمت اصلی: {base_price:,} تومان\n"
            f"🎁 تخفیف: {result['discount']:,} تومان\n"
            f"💵 قیمت نهایی: {result['final']:,} تومان",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton('✅ ادامه خرید',callback_data=f'confirm_service_{service_type}_{months}_{volume}')],
                [InlineKeyboardButton('🔙 برگشت',callback_data=f'service_volume_{service_type}_{months}_{volume}')]
            ])
        )
        return


    # =========================
    # جستجوی کاربر توسط ادمین
    # =========================
    # ۱. جستجو

    # ۱. جستجو
    if context.user_data.get("admin_search"):
        if user_id != ADMIN_ID:
            context.user_data.pop("admin_search", None)
            return

        if not update.message.text:
            await update.message.reply_text(
                "❌ لطفاً آیدی عددی یا یوزرنیم را وارد کنید."
            )
            return

        search_text = update.message.text.strip().replace("@", "")
        conn = db_connect()
        cursor = conn.cursor()
        # ⬇️ تغییر: is_banned هم select می‌شه
        if search_text.isdigit():
            cursor.execute(
                "SELECT user_id, balance, is_banned FROM users WHERE user_id = ?",
                (int(search_text),)
            )
        else:
            cursor.execute(
                "SELECT user_id, balance, is_banned FROM users WHERE LOWER(COALESCE(username, '')) LIKE LOWER(?)",
                (f"%{search_text}%",)
            )
        users = cursor.fetchall()
        conn.close()
        context.user_data.pop("admin_search", None)
        if not users:
            keyboard = [
                [
                    InlineKeyboardButton(
                        "🔍 تلاش مجدد",
                        callback_data="admin_search"
                    )
                ],
                [
                    InlineKeyboardButton(
                        "🔙 پنل مدیریت",
                        callback_data="admin_panel"
                    )
                ]
            ]
            await update.message.reply_text(
                "❌ کاربری با این مشخصات پیدا نشد.",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
            return

        # اگر فقط یک کاربر پیدا شد
        if len(users) == 1:
            # ⬇️ تغییر: is_banned هم از تاپل خونده می‌شه
            target_user_id, balance, is_banned = users[0]
            # ⬇️ تغییر: تبدیل به متن فارسی
            ban_status = "🚫 مسدود" if is_banned else "✅ فعال"
            try:
                target_user = await context.bot.get_chat(target_user_id)
                full_name = target_user.full_name or "نامشخص"
                username = f"@{target_user.username}" if target_user.username else "ندارد"
            except Exception:
                full_name = "نامشخص"
                username = "ندارد"
            keyboard = [
                [
                    InlineKeyboardButton(
                        "💰 مدیریت موجودی",
                        callback_data=f"admin_user_wallet_{target_user_id}"
                    )
                ],
                [
                    InlineKeyboardButton(
                        "💳 پرداخت‌ها",
                        callback_data=f"admin_user_deposits_page_{target_user_id}_0"
                    )
                ],
                [
                    InlineKeyboardButton(
                        "📨 ارسال پیام به کاربر",
                        callback_data=f"user_msg_{target_user_id}"
                    )
                ],
                [
                    InlineKeyboardButton(
                        "🔙 پنل مدیریت",
                        callback_data="admin_panel"
                    )
                ]
            ]
            await update.message.reply_text(
                "✅ کاربر پیدا شد:\n\n"
                f"👤 نام: {full_name}\n"
                f"🔹 Username: {username}\n"
                f"🆔 ID: {target_user_id}\n"
                f"💰 موجودی: {balance:,} تومان\n"
                f"📌 وضعیت: {ban_status}",  # ⬅️ خط جدید
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
            return

        # اگر چند کاربر پیدا شد
        keyboard = []
        # ⬇️ تغییر: is_banned هم از تاپل خونده می‌شه
        for target_user_id, balance, is_banned in users[:20]:
            try:
                target_user = await context.bot.get_chat(target_user_id)
                user_title = f"@{target_user.username}" if target_user.username else target_user.full_name
            except Exception:
                user_title = f"کاربر {target_user_id}"
            # ⬇️ تغییر: اگه بن شده، علامت 🚫 بذار
            ban_mark = "🚫 " if is_banned else ""
            keyboard.append([
                InlineKeyboardButton(
                    f"{ban_mark}👤 {user_title} | 💰 {balance:,}",
                    callback_data=f"admin_user_{target_user_id}"
                )
            ])
        keyboard.append([
            InlineKeyboardButton(
                "🔙 پنل مدیریت",
                callback_data="admin_panel"
            )
        ])
        await update.message.reply_text(
            f"🔍 نتایج جستجو ({len(users)} کاربر یافت شد):\n\n"
            "🚫 = کاربر مسدود است",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return


    # -- بخش ارسال پیام همگانی (فقط ادمین) --
    # ۲. پیام همگانی
    if context.user_data.get("waiting_for_broadcast"):

        if user_id != ADMIN_ID:
            context.user_data.pop(
                "waiting_for_broadcast",
            )
            return

        await send_broadcast(
            update,
            context
        )

        return

    # =========================
    # تغییر قیمت سرویس‌ها توسط ادمین
    # =========================
    if context.user_data.get("waiting_for_cdn_price") or context.user_data.get("waiting_for_tunnel_price"):
        if user_id != ADMIN_ID:
            context.user_data.pop("waiting_for_cdn_price", None)
            context.user_data.pop("waiting_for_tunnel_price", None)
            context.user_data.pop("editing_cdn_volume", None)
            context.user_data.pop("editing_tunnel_volume", None)
            return
        service_type = "cdn" if context.user_data.get("waiting_for_cdn_price") else "tunnel"
        volume = context.user_data.get("editing_cdn_volume" if service_type == "cdn" else "editing_tunnel_volume")
        raw = (update.message.text or "").replace(",", "").strip()
        try:
            new_price = int(raw)
        except ValueError:
            await update.message.reply_text("❌ قیمت نامعتبر است.\n\nلطفاً فقط عدد وارد کنید.")
            return
        if new_price <= 0:
            await update.message.reply_text("❌ قیمت باید بیشتر از صفر باشد.")
            return
        if not volume:
            context.user_data.pop("waiting_for_cdn_price", None)
            context.user_data.pop("waiting_for_tunnel_price", None)
            return
        cfg = SERVICE_CONFIG.get(service_type)
        conn = db_connect()
        try:
            cur = conn.execute("UPDATE service_plans SET price=?,updated_at=CURRENT_TIMESTAMP WHERE service_id=? AND volume=? AND active=1", (new_price, cfg["service_id"], volume))
            updated = cur.rowcount
            if updated == 1 and service_type in ("cdn", "tunnel"):
                legacy = "cdn_prices" if service_type == "cdn" else "tunnel_prices"
                conn.execute(f"INSERT INTO {legacy}(volume,price) VALUES(?,?) ON CONFLICT(volume) DO UPDATE SET price=excluded.price", (volume,new_price))
            conn.commit()
        finally:
            conn.close()
        context.user_data.pop("waiting_for_cdn_price", None)
        context.user_data.pop("waiting_for_tunnel_price", None)
        context.user_data.pop("editing_cdn_volume", None)
        context.user_data.pop("editing_tunnel_volume", None)
        if updated != 1:
            await update.message.reply_text("❌ این حجم در دیتابیس پیدا نشد.")
            return
        title = service_volume_title(service_type, volume)
        back_cb = "admin_cdn_prices" if service_type == "cdn" else "admin_tunnel_prices"
        await update.message.reply_text(
            f"✅ <b>قیمت با موفقیت تغییر کرد.</b>\n\n"
            f"{service_display_title(service_type)}\n"
            f"{title}\n"
            f"💰 قیمت جدید: {new_price:,} تومان",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(f"{service_display_title(service_type)} — مدیریت قیمت", callback_data=back_cb)],
                [InlineKeyboardButton("🔙 پنل مدیریت", callback_data="admin_panel")]
            ]),
            parse_mode="HTML"
        )
        return

    # =========================
    # دریافت توضیح تغییر موجودی
    # =========================
    if context.user_data.get("waiting_for_wallet_reason"):
        if user_id != ADMIN_ID:
            context.user_data.pop("waiting_for_wallet_reason", None)
            return
        target_user_id = context.user_data.pop(
            "waiting_for_wallet_reason"
        )
        if not update.message.text:
            await update.message.reply_text(
                "❌ لطفاً توضیح را به صورت متن وارد کنید."
            )
            return
        reason_text = update.message.text.strip()
        if len(reason_text) > 200:
            await update.message.reply_text(
                "❌ توضیح نباید بیشتر از ۲۰۰ کاراکتر باشد."
            )
            return
        context.user_data["wallet_reason"] = reason_text
        # شبیه‌سازی callback برای بازگشت به تأیید
        class FakeQuery:
            def __init__(self, target_uid):
                self.data = f"wallet_back_confirm_{target_uid}"
                self.from_user = update.effective_user
                self.message = update.message
            async def edit_message_text(
                self, text, reply_markup=None, parse_mode=None
            ):
                await update.message.reply_text(
                    text=text,
                    reply_markup=reply_markup,
                    parse_mode=parse_mode
                )
        fake_query = FakeQuery(target_user_id)
        await show_wallet_confirmation(
            fake_query, context, target_user_id
        )
        return

    # =========================
    # دریافت مبلغ تغییر موجودی
    # =========================
    if context.user_data.get("wallet_action"):
        if user_id != ADMIN_ID:
            clear_wallet_state(context)
            return
        if not update.message.text:
            await update.message.reply_text(
                "❌ لطفاً فقط مبلغ را به صورت عدد وارد کنید."
            )
            return
        try:
            amount = int(
                update.message.text.replace(",", "").strip()
            )
        except ValueError:
            await update.message.reply_text(
                "❌ مبلغ نامعتبر است.\n\n"
                "لطفاً فقط عدد وارد کنید.\n"
                "مثال: <code>50000</code>",
                parse_mode="HTML"
            )

            return
        if amount <= 0:
            await update.message.reply_text(
                "❌ مبلغ باید بیشتر از صفر باشد."
            )
            return
        if amount > 1_000_000_000:
            await update.message.reply_text(
                "❌ مبلغ خیلی زیاد است (حداکثر ۱ میلیارد)."
            )
            return
        wallet_action = context.user_data.get("wallet_action")
        target_user_id = context.user_data.get("wallet_target")
        if not target_user_id:
            clear_wallet_state(context)
            return
        # بررسی موجودی برای کسر
        if wallet_action == "sub":
            conn = db_connect()
            cursor = conn.cursor()
            cursor.execute(
                "SELECT balance FROM users WHERE user_id = ?",
                (target_user_id,)
            )
            current = cursor.fetchone()
            conn.close()
            if not current:
                await update.message.reply_text("❌ کاربر پیدا نشد.")
                clear_wallet_state(context)
                return
            current = current[0]
            if amount > current:
                keyboard = [
                    [
                        InlineKeyboardButton(
                            "🔙 برگشت به کیف پول",
                            callback_data=(
                                f"admin_user_wallet_{target_user_id}"
                            )
                        )
                    ]
                ]
                await update.message.reply_text(
                    f"❌ <b>موجودی کاربر کافی نیست.</b>\n\n"
                    f"💵 موجودی فعلی: {current:,} تومان\n"
                    f"💸 مبلغ درخواستی: {amount:,} تومان\n"
                    f"📉 حداکثر قابل کسر: {current:,} تومان",
                    reply_markup=InlineKeyboardMarkup(keyboard),
                    parse_mode="HTML"
                )

                return
        context.user_data["wallet_amount"] = amount
        # نمایش صفحه تأیید
        class FakeQuery:
            def __init__(self, target_uid):
                self.data = f"wallet_back_confirm_{target_uid}"
                self.from_user = update.effective_user
                self.message = update.message
            async def edit_message_text(
                self, text, reply_markup=None, parse_mode=None
            ):
                await update.message.reply_text(
                    text=text,
                    reply_markup=reply_markup,
                    parse_mode=parse_mode
                )
        fake_query = FakeQuery(target_user_id)
        await show_wallet_confirmation(
            fake_query, context, target_user_id
        )
        return

    # -- بخش دریافت تعداد استار برای پرداخت استار تلگرام --
    if context.user_data.get("waiting_for_star_amount"):
        if not update.message.text:
            await update.message.reply_text(
                "🙈 یه عدد بفرست رفیق؛ مثلاً <code>10</code> استار.",
                parse_mode="HTML"
            )
            return

        try:
            star_count = int(update.message.text.replace(",", "").strip())
        except ValueError:
            await update.message.reply_text(
                "😅 این که عدد نبود!\n\n"
                "فقط تعداد استار رو به صورت عدد بفرست؛ مثلاً <code>10</code>.",
                parse_mode="HTML"
            )
            return

        if star_count < 1:
            await update.message.reply_text("⭐️ حداقل ۱ استار لازمه رفیق.")
            return
        if star_count > 10000:
            await update.message.reply_text("🚫 بیشتر از ۱۰٬۰۰۰ استار رو نمی‌شه توی یه تراکنش فرستاد.")
            return

        context.user_data["waiting_for_star_amount"] = False
        context.user_data["star_amount"] = star_count

        # ایجاد فاکتور تلگرام استارز
        from telegram import LabeledPrice
        prices = [LabeledPrice("Telegram Stars", star_count)]

        await context.bot.send_invoice(
            chat_id=update.effective_chat.id,
            title="افزایش موجودی با استار تلگرام",
            description=f"خرید {star_count} استار برای افزایش موجودی",
            payload=f"star_deposit_{star_count}_{update.effective_user.id}",
            provider_token="",  # خالی برای Telegram Stars
            currency="XTR",
            prices=prices,
            start_parameter="star_topup"
        )
        return

    # -- بخش دریافت نرخ استار جدید --
    if context.user_data.get("waiting_for_star_rate"):
        if not update.message.text:
            await update.message.reply_text("❌ لطفاً فقط نرخ را به صورت عدد وارد کنید.")
            return

        try:
            new_rate = int(update.message.text.replace(",", "").strip())
        except ValueError:
            await update.message.reply_text(
                "❌ نرخ نامعتبر است.\n"
                "لطفاً فقط عدد وارد کنید.\n\n"
                "مثال: 3200"
            )
            return

        if new_rate < 100:
            await update.message.reply_text("❌ نرخ باید حداقل ۱۰۰ تومان باشد.")
            return
        if new_rate > 100000:
            await update.message.reply_text("❌ نرخ خیلی بالا است (حداکثر ۱۰۰،۰۰۰ تومان).")
            return

        # اینجا باید متغیر سراسری STAR_TO_TOMAN آپدیت شود
        global STAR_TO_TOMAN
        STAR_TO_TOMAN = new_rate
        set_bot_setting("star_to_toman", new_rate)
        conn = db_connect()
        conn.execute("UPDATE star_config SET rate=?, updated_at=CURRENT_TIMESTAMP WHERE id=1", (new_rate,))
        conn.commit()
        conn.close()
        context.user_data["waiting_for_star_rate"] = False

        await update.message.reply_text(
            f"✅ نرخ استار با موفقیت به <b>{new_rate:,} تومان</b> تغییر یافت.",
            parse_mode="HTML"
        )
        return

    # ۵. ارسال پیام به کاربر خاص
    # =========================
    # ارسال پیام به کاربر خاص
    # =========================

    if context.user_data.get("admin_messaging"):

        if user_id != ADMIN_ID:
            context.user_data.pop("admin_messaging", None)
            return

        target_user_id = context.user_data.pop("admin_messaging")

        try:
            await context.bot.copy_message(
                chat_id=target_user_id,
                from_chat_id=update.message.chat_id,
                message_id=update.message.message_id
            )
            await update.message.reply_text("✅ پیام با موفقیت ارسال شد.")
        except Exception as e:
            print("User message error:", e)
            await update.message.reply_text(
                "❌ خطا در ارسال پیام. ممکن است کاربر ربات را بلاک کرده باشد."
            )

        return

    # -- بخش دریافت مبلغ (کد قبلی شما برای کارت به کارت) --
    # ۶. دریافت مبلغ شارژ (آخرین)
    if not context.user_data.get("waiting_for_amount"):
        return

    if not update.message.text:
        await update.message.reply_text("❌ لطفاً فقط مبلغ را به صورت عدد وارد کنید.")
        return

    try:
        amount = int(update.message.text.replace(",", "").strip())
    except ValueError:
        await update.message.reply_text(
            "❌ مبلغ نامعتبر است.\n"
            "لطفاً فقط عدد وارد کنید.\n\n"
            "مثال: 250000"
        )
        return

    if amount < 10000:
        await update.message.reply_text(
            "❌ حداقل مبلغ افزایش موجودی ۱۰,۰۰۰ تومان است."
        )
        return

    context.user_data["waiting_for_amount"] = False
    context.user_data["deposit_amount"] = amount

    if context.user_data.get("payment_method") == "card":
        keyboard = [
            [
                InlineKeyboardButton(
                    "‹  برگشت",
                    callback_data="charge"
                )
            ]
        ]

        await update.message.reply_text(
            f"💳 <b>درخواست افزایش موجودی</b>\n\n"
            f"💰 مبلغ: {amount:,} تومان\n\n"
            f"لطفاً مبلغ دقیقاً همین مقدار را به شماره کارت زیر واریز کنید:\n\n"
            f"💳 شماره کارت:\n"
            f"<code>{html.escape(get_bot_setting('card_number', 'تنظیم نشده'))}</code>\n\n"
            f"👤 به نام: {html.escape(get_bot_setting('card_holder', 'تنظیم نشده'))}\n\n"
            f"⚠️ لطفاً دقیقاً همان مبلغ را واریز کنید.\n\n"
            f"📸 بعد از پرداخت، عکس واضح رسید را همینجا ارسال کنید.\n\n"
            f"⏳ پس از ارسال رسید، درخواست شما برای مدیریت ارسال می‌شود "
            f"و تا زمان تأیید، مبلغی به موجودی شما اضافه نخواهد شد.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

        return

    await update.message.reply_text(
        f"💰 مبلغ درخواست شده: {amount:,} تومان\n\n"
        "حالا مبلغ را دقیقاً به همین مقدار پرداخت کنید "
        "و عکس رسید پرداخت را همینجا ارسال کنید.\n\n"
        "📸 فقط عکس رسید را ارسال کنید."
    )



# =========================
# دریافت عکس رسید
# =========================
async def receive_receipt(
        update: Update,
        context: ContextTypes.DEFAULT_TYPE
):
    if not update.message or not update.effective_user:
        return
    if update.effective_user.id == ADMIN_ID and context.user_data.get("admin_subscription_reply"):
        await receive_selected_admin_subscription(update, context)
        return
    if not context.user_data.get("deposit_amount"):
        return

    #ادمین عکس به کاربر خاص ...
    if( update.effective_user.id == ADMIN_ID
        and context.user_data.get("admin_messaging")
    ):
        target_user_id = context.user_data.pop("admin_messaging")
        try:
            await context.bot.copy_message(
                chat_id=target_user_id,
                from_chat_id=update.message.chat_id,
                message_id=update.message.message_id
            )
            await update.message.reply_text("پیام با موفقیت ارسال شد ✅")
        except Exception as e:
            print("User message error:", e)
            await update.message.reply_text(
                "❌خطا در ارسال پیام. ممکن است کاربر ربات را بلاک کرده باشد."
            )
        return


    #اریپلای ادمین روی پیام تحویل اشتراک با عکس....
    if (
            update.effective_user.id == ADMIN_ID
            and update.message.reply_to_message
            and not context.user_data.get("waiting_for_broadcast")
    ):
        handled = await receive_admin_subscription(update, context)
        if handled:
            return
    # اگر ادمین در حالت پیام همگانی است
    # عکس را به عنوان Broadcast ارسال کن
    if (
            update.effective_user.id == ADMIN_ID
            and context.user_data.get("waiting_for_broadcast")
    ):
        await send_broadcast(
            update,
            context
        )
        return

    # اگر کاربر رسید پرداخت فرستاده
    if "deposit_amount" not in context.user_data:
        await update.message.reply_text(
            "❌ ابتدا از بخش «افزایش موجودی» مبلغ موردنظر را وارد کنید."
        )
        return

    amount = context.user_data["deposit_amount"]
    user = update.effective_user

    photo = update.message.photo[-1]

    conn = db_connect()
    cursor = conn.cursor()

    cursor.execute(
        """
        INSERT INTO deposits (user_id, amount, payment_method, status, receipt_file_id)
        VALUES (?, ?, ?, 'pending', ?)
        """,
        (user.id, amount, context.user_data.get("payment_method", "card"), photo.file_id)
    )

    deposit_id = cursor.lastrowid
    created_row = cursor.execute(
        "SELECT created_at FROM deposits WHERE id = ?",
        (deposit_id,)
    ).fetchone()
    created_at = created_row[0] if created_row else None

    conn.commit()
    conn.close()

    # آرشیو محلی اختیاری و مقاوم در برابر file_id نامعتبر.
    receipt_local_path = None
    try:
        os.makedirs(RECEIPTS_DIR, exist_ok=True)
        receipt_local_path = os.path.join(RECEIPTS_DIR, f"deposit_{deposit_id}.jpg")
        tg_file = await context.bot.get_file(photo.file_id)
        await tg_file.download_to_drive(receipt_local_path)
        conn = db_connect()
        try:
            conn.execute("UPDATE deposits SET receipt_local_path=? WHERE id=?", (receipt_local_path, deposit_id))
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        receipt_local_path = None
        print(f"Receipt local archive save failed for #{deposit_id}: {type(e).__name__}: {e}")

    # ارسال رسید برای ادمین
    keyboard = [
        [
            InlineKeyboardButton(
                "✅ تأیید",
                callback_data=f"approve_{deposit_id}"
            ),
            InlineKeyboardButton(
                "❌ رد",
                callback_data=f"reject_{deposit_id}"
            )
        ]
    ]

    receipt_caption = (
        "💰 درخواست افزایش موجودی\n\n"
        f"👤 نام: {user.full_name}\n"
        f"🔹 نام کاربری: @{user.username if user.username else 'ندارد'}\n"
        f"🆔 ID: {user.id}\n"
        f"💵 مبلغ: {amount:,} تومان\n"
        f"🔢 شماره درخواست: {deposit_id}\n"
        f"📅 تاریخ ثبت: {iran_time_text(created_at) if created_at else 'نامشخص'}"
    )
    admin_receipt_message = await send_receipt_media(
        context.bot, ADMIN_ID, photo.file_id, None, None, receipt_local_path,
        receipt_caption, InlineKeyboardMarkup(keyboard), deposit_id=deposit_id
    )
    if admin_receipt_message is not None and getattr(admin_receipt_message, "message_id", None):
        conn = db_connect()
        try:
            conn.execute(
                "UPDATE deposits SET receipt_archive_message_id=?, receipt_archive_chat_id=? WHERE id=?",
                (admin_receipt_message.message_id, ADMIN_ID, deposit_id)
            )
            conn.commit()
        finally:
            conn.close()

    await update.message.reply_text(
        "✅ رسید شما دریافت شد.\n\n"
        f"💰 مبلغ: {amount:,} تومان\n"
        f"🔢 شماره درخواست: {deposit_id}\n\n"
        "⏳ رسید برای مدیریت ارسال شد و پس از تأیید، "
        "مبلغ به موجودی شما اضافه می‌شود."
    )

    context.user_data.pop("deposit_amount", None)
    context.user_data.pop("waiting_for_amount", None)
    context.user_data.pop("payment_method", None)


def add_missing_columns(cursor):
    cursor.execute("PRAGMA table_info(subscriptions)")
    columns = [row[1] for row in cursor.fetchall()]

    if "duration_months" not in columns:
        cursor.execute("""
                       ALTER TABLE subscriptions
                           ADD COLUMN duration_months INTEGER NOT NULL DEFAULT 1
                       """)

    if "subscription_text" not in columns:
        cursor.execute("ALTER TABLE subscriptions ADD COLUMN subscription_text TEXT")
    if "admin_message_id" not in columns:
        cursor.execute("ALTER TABLE subscriptions ADD COLUMN admin_message_id INTEGER")
    if "created_at" not in columns:
        cursor.execute("ALTER TABLE subscriptions ADD COLUMN created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP")
    if "completed_at" not in columns:
        cursor.execute("ALTER TABLE subscriptions ADD COLUMN completed_at TIMESTAMP")

    if "expires_at" not in columns:
        cursor.execute("""
                       ALTER TABLE subscriptions
                           ADD COLUMN expires_at TIMESTAMP
                       """)

    if "subscription_media_type" not in columns:
        cursor.execute("ALTER TABLE subscriptions ADD COLUMN subscription_media_type TEXT")
    if "subscription_media_id" not in columns:
        cursor.execute("ALTER TABLE subscriptions ADD COLUMN subscription_media_id TEXT")
    if "subscription_media_archive_chat_id" not in columns:
        cursor.execute("ALTER TABLE subscriptions ADD COLUMN subscription_media_archive_chat_id INTEGER")
    if "subscription_media_archive_message_id" not in columns:
        cursor.execute("ALTER TABLE subscriptions ADD COLUMN subscription_media_archive_message_id INTEGER")
    if "subscription_media_local_path" not in columns:
        cursor.execute("ALTER TABLE subscriptions ADD COLUMN subscription_media_local_path TEXT")
    if "cancelled_at" not in columns:
        cursor.execute("ALTER TABLE subscriptions ADD COLUMN cancelled_at TIMESTAMP")
    if "cancelled_by" not in columns:
        cursor.execute("ALTER TABLE subscriptions ADD COLUMN cancelled_by INTEGER")
    if "cancellation_reason" not in columns:
        cursor.execute("ALTER TABLE subscriptions ADD COLUMN cancellation_reason TEXT")
    if "renewal_count" not in columns:
        cursor.execute("ALTER TABLE subscriptions ADD COLUMN renewal_count INTEGER NOT NULL DEFAULT 0")
    if "last_renewed_at" not in columns:
        cursor.execute("ALTER TABLE subscriptions ADD COLUMN last_renewed_at TIMESTAMP")
    if "last_renewal_price" not in columns:
        cursor.execute("ALTER TABLE subscriptions ADD COLUMN last_renewal_price INTEGER DEFAULT 0")


def calculate_expiry_date(months=1):
    try:
        months = max(1, int(months or 1))
    except (TypeError, ValueError):
        months = 1
    return iran_now() + timedelta(days=30 * months)




# =========================
# دیتابیس
# =========================
def init_db():
    conn = db_connect()
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA wal_autocheckpoint=1000")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=30000")
    cursor = conn.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            balance INTEGER DEFAULT 0,
            username TEXT,
            full_name TEXT,
            is_banned INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    cursor.execute("PRAGMA table_info(users)")
    columns = [row for row in cursor.fetchall()]
    existing_columns = [c[1] for c in columns]


    if "username" not in existing_columns:
        try:
            cursor.execute("ALTER TABLE users ADD COLUMN username TEXT")
        except Exception:
            pass

    if "full_name" not in existing_columns:
        try:
            cursor.execute("ALTER TABLE users ADD COLUMN full_name TEXT")
        except Exception:
            pass

    if "is_banned" not in existing_columns:
        try:
            cursor.execute("ALTER TABLE users ADD COLUMN is_banned INTEGER DEFAULT 0")
        except Exception:
            pass

    if "created_at" not in existing_columns:
        try:
            cursor.execute("ALTER TABLE users ADD COLUMN created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP")
        except Exception:
            pass

    # =========================
    # بقیه جدول‌ها (بدون تغییر)
    # =========================

    cursor.execute("""
                   CREATE TABLE IF NOT EXISTS balance_logs (
                       id INTEGER PRIMARY KEY AUTOINCREMENT,
                       user_id INTEGER NOT NULL,
                       admin_id INTEGER NOT NULL,
                       action TEXT NOT NULL,
                       amount INTEGER NOT NULL,
                       old_balance INTEGER NOT NULL,
                       new_balance INTEGER NOT NULL,
                       reason TEXT,
                       created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                   )
                   """)

    cursor.execute("""
                   CREATE TABLE IF NOT EXISTS deposits
                   (
                       id INTEGER PRIMARY KEY AUTOINCREMENT, 
                       user_id INTEGER,
                       amount INTEGER,
                       payment_method TEXT,
                       status TEXT DEFAULT 'pending',
                       processed_by INTEGER, 
                       processed_at TIMESTAMP,
                        receipt_file_id TEXT,
                       created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                   )
                   """)

    # بررسی و اضافه کردن ستون‌های جدید برای دیتابیس قدیمی
    cursor.execute("PRAGMA table_info(deposits)")
    deposit_columns = [row[1] for row in cursor.fetchall()]

    if "payment_method" not in deposit_columns:
        try:
            cursor.execute("ALTER TABLE deposits ADD COLUMN payment_method TEXT")
        except Exception:
            pass

    if "processed_by" not in deposit_columns:
        try:
            cursor.execute("ALTER TABLE deposits ADD COLUMN processed_by INTEGER")
        except Exception:
            pass

    if "processed_at" not in deposit_columns:
        try:
            cursor.execute("ALTER TABLE deposits ADD COLUMN processed_at TIMESTAMP")
        except Exception:
            pass

    if "receipt_file_id" not in deposit_columns:
        try:
            cursor.execute("ALTER TABLE deposits ADD COLUMN receipt_file_id TEXT")
        except Exception:
            pass

    if "receipt_archive_message_id" not in deposit_columns:
        try:
            cursor.execute("ALTER TABLE deposits ADD COLUMN receipt_archive_message_id INTEGER")
        except Exception:
            pass

    if "receipt_archive_chat_id" not in deposit_columns:
        try:
            cursor.execute("ALTER TABLE deposits ADD COLUMN receipt_archive_chat_id INTEGER")
        except Exception:
            pass

    if "receipt_local_path" not in deposit_columns:
        try:
            cursor.execute("ALTER TABLE deposits ADD COLUMN receipt_local_path TEXT")
        except Exception:
            pass

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS cdn_prices (
              volume TEXT PRIMARY KEY,
              price INTEGER NOT NULL
    )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS tunnel_prices (
              volume TEXT PRIMARY KEY,
              price INTEGER NOT NULL
        )
    """)

    cursor.execute("""
                CREATE TABLE IF NOT EXISTS subscriptions (
                      id INTEGER PRIMARY KEY AUTOINCREMENT,
                      user_id INTEGER NOT NULL,
                      service_type TEXT NOT NULL,
                      volume TEXT NOT NULL,
                      price INTEGER NOT NULL,
                      duration_months INTEGER NOT NULL DEFAULT 1,
                      subscription_text TEXT,
                      subscription_media_type TEXT,
                      subscription_media_id TEXT,
                      subscription_media_archive_chat_id INTEGER,
                      subscription_media_archive_message_id INTEGER,
                      subscription_media_local_path TEXT,
                      status TEXT NOT NULL DEFAULT 'pending',
                      admin_message_id INTEGER,
                      created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                      completed_at TIMESTAMP,
                      expires_at TIMESTAMP
                )
    """)

    # ستون‌های اشتراک باید قبل از هر index/query وابسته به آن‌ها کامل شوند.
    add_missing_columns(cursor)

    # سیستم تخفیف: چند نسخه با یک نام مجاز است؛ فقط یک نسخه فعال برای هر نام.
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS discount_codes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT NOT NULL,
            discount_type TEXT NOT NULL CHECK(discount_type IN ('percent','fixed')),
            value INTEGER NOT NULL,
            max_uses INTEGER NOT NULL DEFAULT 0,
            used_count INTEGER NOT NULL DEFAULT 0,
            max_uses_per_user INTEGER NOT NULL DEFAULT 1,
            min_purchase INTEGER NOT NULL DEFAULT 0,
            max_discount INTEGER NOT NULL DEFAULT 0,
            active INTEGER NOT NULL DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            expires_at TIMESTAMP,
            deleted_at TIMESTAMP
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS discount_usages (
            id INTEGER PRIMARY KEY AUTOINCREMENT, discount_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL, subscription_id INTEGER,
            base_price INTEGER NOT NULL DEFAULT 0, discount_amount INTEGER NOT NULL DEFAULT 0,
            used_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(discount_id) REFERENCES discount_codes(id)
        )
    """)
    ucols = {r[1] for r in cursor.execute("PRAGMA table_info(discount_usages)").fetchall()}
    if 'base_price' not in ucols:
        cursor.execute("ALTER TABLE discount_usages ADD COLUMN base_price INTEGER NOT NULL DEFAULT 0")
    if 'discount_amount' not in ucols:
        cursor.execute("ALTER TABLE discount_usages ADD COLUMN discount_amount INTEGER NOT NULL DEFAULT 0")
    # مدل داینامیک سرویس‌ها و پلن‌ها
    # این جداول باید قبل از ساخت ایندکس‌هایشان ایجاد شوند تا دیتابیس‌های قدیمی
    # نیز بدون خطای "no such table" قابل ارتقا باشند.
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS service_catalog (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT NOT NULL UNIQUE,
            title TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 1,
            sort_order INTEGER NOT NULL DEFAULT 100,
            legacy_key TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # تاریخچه کدهای سرویس برای اینکه callbackهای قدیمی و لینک‌های داخلی بعد از تغییر کد
    # همچنان به همان سرویس resolve شوند. کد فعلی در service_catalog یکتا می‌ماند.
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS service_code_aliases (
            alias_code TEXT PRIMARY KEY,
            service_id INTEGER NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(service_id) REFERENCES service_catalog(id) ON DELETE CASCADE
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_service_code_alias_service ON service_code_aliases(service_id)")

    scols = {r[1] for r in cursor.execute("PRAGMA table_info(service_catalog)").fetchall()}
    if "legacy_key" not in scols:
        cursor.execute("ALTER TABLE service_catalog ADD COLUMN legacy_key TEXT")
    if "extra_day_price" not in scols:
        cursor.execute("ALTER TABLE service_catalog ADD COLUMN extra_day_price INTEGER NOT NULL DEFAULT 0")
    if "extra_gb_price" not in scols:
        cursor.execute("ALTER TABLE service_catalog ADD COLUMN extra_gb_price INTEGER NOT NULL DEFAULT 0")
    cursor.execute("UPDATE service_catalog SET legacy_key='cdn' WHERE code='cdn' AND (legacy_key IS NULL OR legacy_key='')")
    cursor.execute("UPDATE service_catalog SET legacy_key='tunnel' WHERE code='tunnel' AND (legacy_key IS NULL OR legacy_key='')")

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS service_plans (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            service_id INTEGER NOT NULL,
            volume TEXT NOT NULL,
            price INTEGER NOT NULL,
            active INTEGER NOT NULL DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(service_id, volume),
            FOREIGN KEY(service_id) REFERENCES service_catalog(id) ON DELETE CASCADE
        )
    """)
    # پلن‌های جدید: حجم + مدت + قیمت
    try:
        pcols = {r[1] for r in cursor.execute("PRAGMA table_info(service_plans)").fetchall()}
        if "duration_months" not in pcols:
            cursor.execute("ALTER TABLE service_plans ADD COLUMN duration_months INTEGER NOT NULL DEFAULT 1")
        # در دیتابیس‌های قدیمی UNIQUE(service_id,volume) اجازه چند مدت برای یک حجم را نمی‌دهد.
        idx_rows = cursor.execute("PRAGMA index_list(service_plans)").fetchall()
        has_old_unique = False
        for r in idx_rows:
            try:
                info = cursor.execute(f"PRAGMA index_info([{r[1]}])").fetchall()
                cols = [x[2] for x in info]
                if r[2] and cols == ["service_id", "volume"]:
                    has_old_unique = True
            except Exception:
                pass
        if has_old_unique:
            cursor.execute("""CREATE TABLE service_plans__new (
                id INTEGER PRIMARY KEY AUTOINCREMENT, service_id INTEGER NOT NULL, volume TEXT NOT NULL,
                price INTEGER NOT NULL, duration_months INTEGER NOT NULL DEFAULT 1, active INTEGER NOT NULL DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(service_id, volume, duration_months),
                FOREIGN KEY(service_id) REFERENCES service_catalog(id) ON DELETE CASCADE
            )""")
            cursor.execute("INSERT INTO service_plans__new(id,service_id,volume,price,duration_months,active,created_at,updated_at) SELECT id,service_id,volume,price,COALESCE(duration_months,1),active,created_at,updated_at FROM service_plans")
            cursor.execute("DROP TABLE service_plans")
            cursor.execute("ALTER TABLE service_plans__new RENAME TO service_plans")
    except Exception as exc:
        print("service_plans migration warning:", exc)

    cursor.execute("""CREATE TABLE IF NOT EXISTS service_inbounds (
        service_id INTEGER NOT NULL, inbound_id INTEGER NOT NULL,
        PRIMARY KEY(service_id, inbound_id),
        FOREIGN KEY(service_id) REFERENCES service_catalog(id) ON DELETE CASCADE
    )""")
    # پلن سفارشی مستقل؛ هیچ تغییری در جدول/منطق پلن‌های ثابت ایجاد نمی‌کند.
    cursor.execute("""CREATE TABLE IF NOT EXISTS custom_plan_settings (
        id INTEGER PRIMARY KEY CHECK(id=1), active INTEGER NOT NULL DEFAULT 0,
        base_gb_price INTEGER NOT NULL DEFAULT 0, monthly_price INTEGER NOT NULL DEFAULT 0,
        extra_day_price INTEGER NOT NULL DEFAULT 0, updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""")
    cursor.execute("INSERT OR IGNORE INTO custom_plan_settings(id,active,base_gb_price,monthly_price,extra_day_price) VALUES(1,0,0,0,0)")
    cursor.execute("""CREATE TABLE IF NOT EXISTS custom_countries (
        id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL UNIQUE,
        gb_price INTEGER NOT NULL DEFAULT 0, active INTEGER NOT NULL DEFAULT 1,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""")
    cursor.execute("""CREATE TABLE IF NOT EXISTS custom_country_inbounds (
        country_id INTEGER NOT NULL, inbound_id INTEGER NOT NULL,
        PRIMARY KEY(country_id,inbound_id),
        FOREIGN KEY(country_id) REFERENCES custom_countries(id) ON DELETE CASCADE
    )""")
    # پلن سفارشی مستقل؛ هیچ تغییری در جدول/منطق پلن‌های ثابت ایجاد نمی‌کند.
    subcols = {r[1] for r in cursor.execute("PRAGMA table_info(subscriptions)").fetchall()}
    for col, typ in (("xui_email","TEXT"),("xui_sub_id","TEXT"),("xui_inbound_ids","TEXT"),("xui_subscription_url","TEXT"),("xui_uuid","TEXT"),("xui_total_bytes","INTEGER"),("xui_used_bytes","INTEGER"),("low_traffic_notified","INTEGER NOT NULL DEFAULT 0"),("expiry_notified","INTEGER NOT NULL DEFAULT 0"),("service_plan_id","INTEGER"),("service_title_snapshot","TEXT"),("service_config_snapshot","TEXT")):
        if col not in subcols:
            cursor.execute(f"ALTER TABLE subscriptions ADD COLUMN {col} {typ}")

    cursor.execute("""\n        CREATE TABLE IF NOT EXISTS subscription_renewals (\n            id INTEGER PRIMARY KEY AUTOINCREMENT,\n            subscription_id INTEGER NOT NULL,\n            user_id INTEGER NOT NULL,\n            service_type TEXT NOT NULL,\n            volume TEXT NOT NULL,\n            amount INTEGER NOT NULL,\n            old_expires_at TIMESTAMP,\n            new_expires_at TIMESTAMP NOT NULL,\n            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,\n            FOREIGN KEY(subscription_id) REFERENCES subscriptions(id) ON DELETE CASCADE\n        )\n    """)

    # تنظیمات استار تلگرام
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS star_config (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            rate INTEGER NOT NULL DEFAULT 3200,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cursor.execute("INSERT OR IGNORE INTO star_config (id, rate) VALUES (1, 3200)")

    # تنظیمات اصلی ربات: مقادیر فقط بار اول از تنظیمات کد وارد DB می‌شوند و
    # بعد از آن DB منبع اصلی است تا با هر ری‌استارت تنظیمات ادمین برنگردد.
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS bot_settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    bot_defaults = {
        "bot_token": BOT_TOKEN,
        "admin_id": str(ADMIN_ID),
        "broadcast_delay": str(BROADCAST_DELAY),
        "page_size": str(PAGE_SIZE),
        "button_style_mode": BUTTON_STYLE_MODE,
        "channels_json": json.dumps(CHANNELS, ensure_ascii=False),
        "forced_join_enabled": "1",
    }
    for _key, _value in bot_defaults.items():
        cursor.execute(
            "INSERT OR IGNORE INTO bot_settings(key,value) VALUES(?,?)",
            (_key, str(_value))
        )

    cursor.execute("CREATE INDEX IF NOT EXISTS idx_service_plans_service_active ON service_plans(service_id, active, id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_service_catalog_active ON service_catalog(active, sort_order, id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_subscription_renewals_sub ON subscription_renewals(subscription_id, id DESC)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_subscriptions_expiry ON subscriptions(status, expires_at)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_discount_usage_user ON discount_usages(discount_id, user_id, id DESC)")
    cursor.execute("DROP INDEX IF EXISTS idx_discount_usage_unique_user")

    # =========================
    # مهاجرت امن محدودیت‌های قدیمی کد تخفیف
    # =========================
    # SQLite اجازه خاموش/روشن کردن foreign_keys را وسط transaction نمی‌دهد.
    # تمام بازسازی‌های parent/child را بنابراین در یک نقطه، بعد از commit، با
    # foreign_keys=OFF انجام می‌دهیم و در پایان با foreign_key_check صحت را تأیید می‌کنیم.
    cursor.connection.commit()
    cursor.connection.execute("PRAGMA foreign_keys=OFF")
    try:
        # اگر migration قبلی نصفه مانده باشد، temp tableهای شناخته‌شده را پاک می‌کنیم.
        for tmp in ("discount_usages__legacy_fk", "discount_usages__old_unique_user",
                    "discount_usages__old_fk", "discount_codes__old_unique", "discount_codes_legacy"):
            cursor.execute(f"DROP TABLE IF EXISTS [{tmp}]")

        def _table_unique_on_columns(table_name, wanted_columns):
            wanted = list(wanted_columns)
            for idx in cursor.execute(f"PRAGMA index_list([{table_name}])").fetchall():
                if int(idx[2] or 0) != 1:
                    continue
                idx_name = str(idx[1])
                safe_name = idx_name.replace("]", "]]" )
                cols = [r[2] for r in cursor.execute(f"PRAGMA index_info([{safe_name}])").fetchall()]
                if cols == wanted:
                    return True
            return False

        code_cols = {r[1] for r in cursor.execute("PRAGMA table_info(discount_codes)").fetchall()}
        required_code_cols = {"max_uses_per_user", "min_purchase", "max_discount", "deleted_at"}
        code_needs_rebuild = not required_code_cols.issubset(code_cols) or _table_unique_on_columns("discount_codes", ["code"])

        usage_cols = {r[1] for r in cursor.execute("PRAGMA table_info(discount_usages)").fetchall()}
        usage_needs_rebuild = bool(usage_cols) and (_table_unique_on_columns("discount_usages", ["discount_id", "user_id"]) or not {"base_price", "discount_amount"}.issubset(usage_cols))

        if code_needs_rebuild:
            cursor.execute("ALTER TABLE discount_codes RENAME TO discount_codes__old_unique")
            cursor.execute("""CREATE TABLE discount_codes (
                id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT NOT NULL,
                discount_type TEXT NOT NULL CHECK(discount_type IN ('percent','fixed')),
                value INTEGER NOT NULL, max_uses INTEGER NOT NULL DEFAULT 0,
                used_count INTEGER NOT NULL DEFAULT 0, max_uses_per_user INTEGER NOT NULL DEFAULT 1,
                min_purchase INTEGER NOT NULL DEFAULT 0, max_discount INTEGER NOT NULL DEFAULT 0,
                active INTEGER NOT NULL DEFAULT 1, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                expires_at TIMESTAMP, deleted_at TIMESTAMP
            )""")
            oldcols = {r[1] for r in cursor.execute("PRAGMA table_info(discount_codes__old_unique)").fetchall()}
            def _col(name, default):
                return name if name in oldcols else default
            cursor.execute(f"""INSERT INTO discount_codes
                (id,code,discount_type,value,max_uses,used_count,max_uses_per_user,min_purchase,max_discount,active,created_at,expires_at,deleted_at)
                SELECT id,code,discount_type,value,max_uses,used_count,
                       {_col('max_uses_per_user','1')},{_col('min_purchase','0')},{_col('max_discount','0')},
                       active,created_at,expires_at,{_col('deleted_at','NULL')}
                FROM discount_codes__old_unique""")
            usage_needs_rebuild = bool(usage_cols)

        if usage_needs_rebuild:
            cursor.execute("ALTER TABLE discount_usages RENAME TO discount_usages__old_fk")
            cursor.execute("""CREATE TABLE discount_usages (
                id INTEGER PRIMARY KEY AUTOINCREMENT, discount_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
                subscription_id INTEGER, base_price INTEGER NOT NULL DEFAULT 0, discount_amount INTEGER NOT NULL DEFAULT 0,
                used_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, FOREIGN KEY(discount_id) REFERENCES discount_codes(id)
            )""")
            old_usage_cols = {r[1] for r in cursor.execute("PRAGMA table_info(discount_usages__old_fk)").fetchall()}
            def _ucol(name, default):
                return name if name in old_usage_cols else default
            cursor.execute(f"""INSERT INTO discount_usages(id,discount_id,user_id,subscription_id,base_price,discount_amount,used_at)
                SELECT id,discount_id,user_id,subscription_id,COALESCE({_ucol('base_price','0')},0),
                       COALESCE({_ucol('discount_amount','0')},0),{_ucol('used_at','CURRENT_TIMESTAMP')}
                FROM discount_usages__old_fk""")
            cursor.execute("DROP TABLE discount_usages__old_fk")

        if code_needs_rebuild:
            cursor.execute("DROP TABLE discount_codes__old_unique")
        # Commit the rebuild before re-enabling foreign keys; SQLite ignores a
        # PRAGMA foreign_keys change while a transaction is active.
        cursor.connection.commit()
    except Exception:
        cursor.connection.rollback()
        raise
    finally:
        cursor.connection.execute("PRAGMA foreign_keys=ON")

    fk_errors = cursor.connection.execute("PRAGMA foreign_key_check").fetchall()
    if fk_errors:
        raise sqlite3.IntegrityError(f"Foreign key integrity check failed after migration: {fk_errors[:5]}")
    cursor.connection.commit()

    # ایندکس‌های کد تخفیف فقط بعد از کامل شدن migration ساخته می‌شوند.
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_discount_code ON discount_codes(code)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_discount_active ON discount_codes(code, active, deleted_at, expires_at)")
    cursor.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_discount_one_active_code ON discount_codes(code) WHERE active=1 AND deleted_at IS NULL")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_deposits_user_id ON deposits(user_id, id DESC)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_deposits_status_id ON deposits(status, id ASC)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_subscriptions_user_id ON subscriptions(user_id, id DESC)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_subscriptions_status_id ON subscriptions(status, id ASC)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_users_username ON users(username)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_users_created ON users(created_at)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_balance_logs_user_created ON balance_logs(user_id, id DESC)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_subscriptions_created ON subscriptions(id DESC)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_deposits_created ON deposits(id DESC)")

    cdn_prices = [
        ("10G", 25000),
        ("20G", 50000),
        ("30G", 75000),
        ("40G", 100000),
        ("50G", 125000),
        ("60G", 150000),
        ("70G", 175000),
        ("80G", 200000),
        ("90G", 225000),
        ("100G", 250000),
        ("unlimited", 300000),
    ]
    tunnel_prices = [
        ("10G", 35000),
        ("20G", 70000),
        ("30G", 105000),
        ("40G", 140000),
        ("50G", 175000),
        ("60G", 210000),
        ("70G", 245000),
        ("80G", 280000),
        ("90G", 315000),
        ("100G", 350000),
    ]

    for volume, price in cdn_prices:
        cursor.execute(
            """
            INSERT OR IGNORE INTO cdn_prices (volume, price)
            VALUES (?, ?)
            """,
            (volume, price)
        )
    for volume, price in tunnel_prices:
        cursor.execute(
            """
            INSERT OR IGNORE INTO tunnel_prices (volume, price)
            VALUES (?, ?)
            """,
            (volume, price)
        )

    # سرویس‌های پیش‌فرض قدیمی CDN/Tunnel از کاتالوگ جدید پاک می‌شوند و دیگر خودکار ساخته نمی‌شوند.
    # سوابق subscriptions کاربران حذف نمی‌شوند.
    for _sid_row in cursor.execute("SELECT id FROM service_catalog WHERE code IN ('cdn','tunnel')").fetchall():
        _sid = int(_sid_row[0])
        cursor.execute("DELETE FROM service_inbounds WHERE service_id=?", (_sid,))
        cursor.execute("DELETE FROM service_code_aliases WHERE service_id=?", (_sid,))
        cursor.execute("DELETE FROM service_plans WHERE service_id=?", (_sid,))
        cursor.execute("DELETE FROM service_catalog WHERE id=?", (_sid,))
    add_missing_columns(cursor)

    # ایندکس‌های لازم برای پنل مدیریت
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_subscriptions_admin_message_id ON subscriptions(admin_message_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_subscriptions_status ON subscriptions(status)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_subscriptions_user_status ON subscriptions(user_id, status)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_deposits_status ON deposits(status)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_deposits_user_status ON deposits(user_id, status)")

    cursor.execute("""CREATE TABLE IF NOT EXISTS user_languages (
        user_id INTEGER PRIMARY KEY, language TEXT NOT NULL DEFAULT 'fa')""")
    cursor.execute("""CREATE TABLE IF NOT EXISTS product_categories (
        id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE, active INTEGER NOT NULL DEFAULT 1,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
    cursor.execute("""CREATE TABLE IF NOT EXISTS products (
        id INTEGER PRIMARY KEY AUTOINCREMENT, category_id INTEGER NOT NULL, name TEXT NOT NULL,
        description TEXT NOT NULL DEFAULT '', delivery_text TEXT NOT NULL DEFAULT '', price INTEGER NOT NULL DEFAULT 0,
        photo_file_id TEXT, active INTEGER NOT NULL DEFAULT 1, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(category_id) REFERENCES product_categories(id))""")
    cursor.execute("""CREATE TABLE IF NOT EXISTS product_purchases (
        id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, product_id INTEGER NOT NULL,
        product_name TEXT NOT NULL, description TEXT NOT NULL DEFAULT '', delivery_text TEXT NOT NULL DEFAULT '',
        price INTEGER NOT NULL, purchased_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
    cursor.execute("""CREATE TABLE IF NOT EXISTS product_discount_usages (
        id INTEGER PRIMARY KEY AUTOINCREMENT, discount_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
        product_purchase_id INTEGER NOT NULL, base_price INTEGER NOT NULL, discount_amount INTEGER NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
    cursor.execute("""CREATE TABLE IF NOT EXISTS wallet_tokens (
        id INTEGER PRIMARY KEY AUTOINCREMENT, token_name TEXT NOT NULL, network TEXT NOT NULL,
        wallet_address TEXT NOT NULL, rate_toman REAL NOT NULL DEFAULT 0, active INTEGER NOT NULL DEFAULT 1,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
    cursor.execute("""CREATE TABLE IF NOT EXISTS wallet_deposits (
        id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, token_id INTEGER NOT NULL,
        token_name TEXT NOT NULL, network TEXT NOT NULL, wallet_address TEXT NOT NULL,
        token_amount REAL NOT NULL, rate_toman REAL NOT NULL, toman_amount INTEGER NOT NULL,
        proof_text TEXT, proof_file_id TEXT, status TEXT NOT NULL DEFAULT 'pending',
        processed_by INTEGER, processed_at TIMESTAMP, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")

    conn.commit()
    conn.close()
async def handle_media_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user and context.user_data.get("wallet_deposit_stage")=="proof" and update.message and update.message.photo:
        if await submit_wallet_proof(update,context,proof_file_id=update.message.photo[-1].file_id): return
    """تمام مدیاها را با اولویت سفارش اشتراک، سپس پیام‌رسانی ادمین و در نهایت رسید مسیریابی می‌کند."""
    if not update.message or not update.effective_user:
        return
    if await product_media_router(update, context):
        return

    # Reply مستقیم روی پیام سفارش اشتراک مستقل از state فعلی است.
    if update.effective_user.id == ADMIN_ID and update.message.reply_to_message:
        handled = await receive_admin_subscription(update, context)
        if handled:
            return

    # ارسال اشتراک انتخاب‌شده از پنل
    if update.effective_user.id == ADMIN_ID and context.user_data.get("admin_subscription_reply"):
        await receive_selected_admin_subscription(update, context)
        return

    # پیام همگانی
    if update.effective_user.id == ADMIN_ID and context.user_data.get("waiting_for_broadcast"):
        await send_broadcast(update, context)
        return

    # پیام خصوصی به کاربر
    if update.effective_user.id == ADMIN_ID and context.user_data.get("admin_messaging"):
        target=context.user_data.pop("admin_messaging")
        try:
            await context.bot.copy_message(chat_id=target,from_chat_id=update.message.chat_id,message_id=update.message.message_id)
            await update.message.reply_text("✅ ارسال شد.")
        except Exception as e:
            print("media user message error",e)
            await update.message.reply_text("❌ ارسال نشد.")
        return

    # فقط وقتی واقعاً در انتظار رسید پرداخت هستیم.
    if update.message.photo and context.user_data.get("deposit_amount"):
        await receive_receipt(update,context)
        return

    if update.message.photo:
        await update.message.reply_text("❌ این عکس در هیچ حالت فعالی برای شما ثبت نمی‌شود. لطفاً ابتدا بخش موردنظر را انتخاب کنید.")



async def handle_all_text_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    تمام پیام‌های متنی را دقیقاً یک بار پردازش می‌کند.
    پاسخ ادمین به پیام سفارش ربات، قبل از ورودی‌های عادی بررسی می‌شود.
    """
    if not update.message or not update.effective_user:
        return

    if update.message.text and update.message.text.strip() == "🏠 منوی اصلی":
        await show_main_menu(update, context)
        return

    # ورودی‌های تنظیمات کارت و ساخت ولت
    uid=update.effective_user.id
    if uid == ADMIN_ID and context.user_data.get("admin_setting_input"):
        key=context.user_data.pop("admin_setting_input")
        val=(update.message.text or "").strip()
        if key=="card_number": set_bot_setting("card_number",val)
        elif key=="card_holder": set_bot_setting("card_holder",val)
        await update.message.reply_text("✅ تنظیم ذخیره شد.")
        return
    if uid == ADMIN_ID and context.user_data.get("wallet_admin_stage"):
        stage=context.user_data.get("wallet_admin_stage"); draft=context.user_data.setdefault("wallet_admin_draft",{}); val=(update.message.text or "").strip()
        if stage=="name": draft["token_name"]=val; context.user_data["wallet_admin_stage"]="network"; prompt="نام شبکه را وارد کن؛ مثال: TRC20"
        elif stage=="network": draft["network"]=val; context.user_data["wallet_admin_stage"]="address"; prompt="آدرس دریافت ولت را ارسال کن:"
        elif stage=="address": draft["wallet_address"]=val; context.user_data["wallet_admin_stage"]="rate"; prompt="نرخ فعلی هر توکن را به تومان وارد کن؛ مثال: 62000"
        else:
            try: rate=float(val.replace(",",""))
            except ValueError: await update.message.reply_text("نرخ باید عددی باشد؛ دوباره وارد کن."); return
            if rate<=0: await update.message.reply_text("نرخ باید بیشتر از صفر باشد."); return
            conn=db_connect(); conn.execute("INSERT INTO wallet_tokens(token_name,network,wallet_address,rate_toman) VALUES(?,?,?,?)",(draft["token_name"],draft["network"],draft["wallet_address"],rate)); conn.commit(); conn.close()
            context.user_data.pop("wallet_admin_stage",None); context.user_data.pop("wallet_admin_draft",None)
            await update.message.reply_text("✅ ولت با موفقیت اضافه شد."); return
        await update.message.reply_text(prompt); return
    if context.user_data.get("product_discount_pid"):
        pid=int(context.user_data.pop("product_discount_pid")); code=(update.message.text or "").strip()
        conn=db_connect()
        try:
            p=conn.execute("SELECT price,active FROM products WHERE id=?",(pid,)).fetchone()
            if not p or not p["active"] or conn.execute("SELECT 1 FROM product_purchases WHERE product_id=?",(pid,)).fetchone():
                await update.message.reply_text("❌ این محصول ناموجود شده است."); return
            result,err=validate_discount(conn,code,uid,int(p["price"]))
            if err:
                await update.message.reply_text(err); return
            context.user_data["product_discount"]={"pid":pid,"id":result["id"],"code":result["code"],"final":result["final"],"base":int(p["price"]),"discount":result["discount"]}
            await update.message.reply_text(f"✅ کد تخفیف اعمال شد.\nقیمت اولیه: {int(p['price']):,} تومان\nتخفیف: {result['discount']:,} تومان\nقیمت نهایی: {result['final']:,} تومان\n\nحالا روی خرید محصول بزن.",reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✅ ادامه خرید",callback_data=f"product_buy_{pid}")],[InlineKeyboardButton("‹ بازگشت به محصول",callback_data=f"product_view_{pid}")]]))
        finally: conn.close()
        return
    if context.user_data.get("wallet_deposit_stage")=="amount":
        raw=(update.message.text or "").strip().replace(",","")
        try: amount=float(raw)
        except ValueError: await update.message.reply_text("مقدار توکن را فقط به عدد وارد کن."); return
        if amount<=0: await update.message.reply_text("مقدار باید بیشتر از صفر باشد."); return
        context.user_data["wallet_deposit_amount"]=amount; context.user_data["wallet_deposit_stage"]="proof"
        await update.message.reply_text("🔗 حالا لینک تراکنش را بفرست (اولویت دارد) یا عکس رسید انتقال را ارسال کن.")
        return
    if context.user_data.get("wallet_deposit_stage")=="proof":
        if await submit_wallet_proof(update,context,proof_text=(update.message.text or "").strip()): return

    # اگر ادمین در حالت ارسال لینک با Reply به پیام سفارش پاسخ داده،
    # همان مسیر مخصوص اشتراک را اجرا کن.
    if (
        update.effective_user.id == ADMIN_ID
        and update.message.reply_to_message
    ):
        handled = await receive_admin_subscription(update, context)
        if handled:
            return

    # تحویل اشتراک انتخاب‌شده: متن یا هر نوع مدیای پشتیبانی‌شده.
    if update.effective_user.id == ADMIN_ID and context.user_data.get("admin_subscription_reply"):
        await receive_selected_admin_subscription(update, context)
        return

    # هر پیام فقط یک بار به handle_text_input داده شود.
    await handle_text_input(update, context)


# =========================
# توابع کمکی کیف پول
# =========================
def clear_wallet_state(context):
    """پاک کردن همه state های مربوط به کیف پول"""
    keys = [
        "wallet_action",
        "wallet_target",
        "wallet_amount",
        "wallet_reason",
        "waiting_for_wallet_reason",
    ]
    for key in keys:
        context.user_data.pop(key, None)


async def show_wallet_confirmation(
    query, context, target_user_id
):
    """نمایش صفحه تأیید تغییر موجودی"""
    wallet_action = context.user_data.get("wallet_action")
    amount = context.user_data.get("wallet_amount")
    reason = context.user_data.get("wallet_reason", "")
    if not wallet_action or not amount:
        await safe_edit_message(query, 
            "❌ اطلاعات منقضی شده. لطفاً دوباره تلاش کنید."
        )
        clear_wallet_state(context)
        return
    # گرفتن اطلاعات کاربر
    try:
        target_user = await context.bot.get_chat(target_user_id)
        user_name = html.escape(target_user.full_name or "نامشخص")
        if target_user.username:
            user_name += f" (@{html.escape(target_user.username)})"
    except Exception:
        user_name = f"کاربر {target_user_id}"
    # گرفتن موجودی فعلی
    conn = db_connect()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT balance FROM users WHERE user_id = ?",
        (target_user_id,)
    )
    result = cursor.fetchone()
    conn.close()
    if not result:
        await safe_edit_message(query, "❌ کاربر پیدا نشد.")
        clear_wallet_state(context)
        return
    old_balance = result[0]
    # محاسبه موجودی جدید
    if wallet_action == "add":
        new_balance = old_balance + amount
        action_text = "➕ افزایش"
        action_emoji = "📈"
        diff_text = f"+{amount:,}"
    elif wallet_action == "sub":
        new_balance = old_balance - amount
        action_text = "➖ کاهش"
        action_emoji = "📉"
        diff_text = f"-{amount:,}"
    else:  # set
        new_balance = amount
        action_text = "✏️ تنظیم"
        action_emoji = "📝"
        diff = amount - old_balance
        if diff > 0:
            diff_text = f"+{diff:,}"
        elif diff < 0:
            diff_text = f"{diff:,}"
        else:
            diff_text = "بدون تغییر"
    reason_button = (
        "📝 تغییر توضیح" if reason else "📝 افزودن توضیح (اختیاری)"
    )
    keyboard = [
        [
            InlineKeyboardButton(
                "✅ تأیید و اعمال",
                callback_data=f"wallet_apply_{target_user_id}"
            )
        ],
        [
            InlineKeyboardButton(
                reason_button,
                callback_data=f"wallet_add_reason_{target_user_id}"
            )
        ],
        [
            InlineKeyboardButton(
                "❌ انصراف",
                callback_data=f"admin_user_wallet_{target_user_id}"
            )
        ]
    ]
    reason_escaped = html.escape(str(reason)) if reason else ""
    reason_text = f"\n📝 توضیح: {reason_escaped}\n" if reason_escaped else ""
    try:
        await safe_edit_message(query, 
            f"{action_emoji} <b>پیش‌نمایش تغییر موجودی</b>\n\n"
            f"👤 کاربر: {user_name}\n"
            f"🆔 ID: <code>{target_user_id}</code>\n\n"
            f"🔧 عملیات: {action_text}\n"
            f"💰 مبلغ: <b>{amount:,}</b> تومان\n"
            f"📊 تغییر: {diff_text} تومان\n"
            f"{reason_text}\n"
            f"💵 موجودی فعلی: {old_balance:,} تومان\n"
            f"💰 موجودی جدید: <b>{new_balance:,}</b> تومان\n\n"
            "⚠️ آیا از اعمال این تغییر مطمئن هستید؟",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="HTML"
        )

    except Exception as e:
        print("show_wallet_confirmation send error:", e)
        #بدون فرمت اگه مارک داون رد شد
        await safe_edit_message(query, 
            f"{action_emoji} پیش‌نمایش تغییر موجودی\n\n"
            f"👤 کاربر: {user_name}\n"
            f"🆔 ID: {target_user_id}\n\n"
            f"🔧 عملیات: {action_text}\n"
            f"💰 مبلغ: {amount:,} تومان\n"
            f"📊 تغییر: {diff_text} تومان\n"
            f"{reason_text}\n"
            f"💵 موجودی فعلی: {old_balance:,} تومان\n"
            f"💰 موجودی جدید: {new_balance:,} تومان\n\n"
            "⚠️ آیا از اعمال این تغییر مطمئن هستید؟",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

# =========================
# پاکسازی state کاربر و جلوگیری از رشد بی‌نهایت Application.user_data
# =========================
def cleanup_transient_user_data(update, context):
    """Remove empty per-user state containers and stale abandoned state.
    All user_data in this bot is transient workflow state; it must not become an
    unbounded in-memory user database.
    """
    try:
        user_data = context.user_data
        transient_keys = [k for k in user_data if k != "_state_touched_at"]
        if transient_keys:
            return
        user_data.pop("_state_touched_at", None)
        effective_user = getattr(update, "effective_user", None)
        user_id = getattr(effective_user, "id", None)
        app = getattr(context, "application", None)
        drop = getattr(app, "drop_user_data", None)
        if user_id is not None and callable(drop):
            drop(user_id)
    except Exception:
        # Cleanup is best-effort and must never break a real update.
        pass


def with_state_cleanup(handler):
    async def _wrapped(update, context):
        try:
            context.user_data["_state_touched_at"] = time.monotonic()
            return await handler(update, context)
        finally:
            cleanup_transient_user_data(update, context)
    _wrapped.__name__ = getattr(handler, "__name__", "wrapped_handler")
    return _wrapped


STATE_CLEANUP_INTERVAL = 300
STATE_TTL = 3600

async def _stale_user_state_cleanup(application):
    """Bound the lifetime of abandoned workflow states without JobQueue."""
    try:
        while True:
            await asyncio.sleep(STATE_CLEANUP_INTERVAL)
            now = time.monotonic()
            user_data_map = getattr(application, "user_data", {})
            for user_id, data in list(user_data_map.items()):
                if not data:
                    application.drop_user_data(user_id)
                    continue
                touched = data.get("_state_touched_at")
                if touched is None or now - float(touched) > STATE_TTL:
                    application.drop_user_data(user_id)
    except asyncio.CancelledError:
        raise


async def _low_traffic_monitor(application):
    """Periodically refresh live X-UI usage and send one-time expiry/quota alerts.

    Expiry alert: when a completed subscription has <= 24 hours remaining.
    Traffic alert: when live usage reaches >= 90%% of the configured quota.
    Both flags are stored per subscription so a user is not spammed.
    """
    try:
        while True:
            await asyncio.sleep(1800)
            now = iran_now()
            conn = db_connect()
            rows = conn.execute(
                "SELECT id,user_id,xui_email,xui_total_bytes,COALESCE(xui_used_bytes,0),"
                "       COALESCE(low_traffic_notified,0),COALESCE(expiry_notified,0),expires_at "
                "FROM subscriptions "
                "WHERE status='completed' AND user_id IS NOT NULL"
            ).fetchall()
            conn.close()

            for sid, uid, email, stored_total, stored_used, traffic_notified, expiry_notified, expires_at in rows:
                try:
                    # --- expiry: no X-UI call is needed ---
                    expiry_dt = None
                    if expires_at:
                        try:
                            expiry_dt = datetime.strptime(str(expires_at), "%Y-%m-%d %H:%M:%S")
                        except ValueError:
                            try:
                                expiry_dt = datetime.fromisoformat(str(expires_at).replace("Z", "+00:00"))
                                if expiry_dt.tzinfo:
                                    expiry_dt = expiry_dt.astimezone(IRAN_TZ).replace(tzinfo=None)
                            except ValueError:
                                expiry_dt = None

                    if expiry_dt is not None:
                        remaining_seconds = (expiry_dt - now).total_seconds()
                        if 0 < remaining_seconds <= 24 * 3600 and not int(expiry_notified or 0):
                            await application.bot.send_message(
                                chat_id=uid,
                                text=(
                                    "⚠️ <b>سرویس شما رو به اتمامه</b>\n\n"
                                    f"⏳ زمان باقی‌مانده: <b>کمتر از ۱ روز</b>\n\n"
                                    "برای ادامه استفاده می‌توانید سرویس را تمدید کنید یا روز اضافه بخرید."
                                ),
                                parse_mode="HTML",
                                reply_markup=InlineKeyboardMarkup([
                                    [
                                        InlineKeyboardButton("🔄 تمدید سرویس", callback_data=f"renew_subscription_{sid}"),
                                        InlineKeyboardButton("📅 تمدید روز", callback_data=f"renew_days_{sid}"),
                                    ],
                                    [InlineKeyboardButton("📦 اشتراک من", callback_data=f"my_subscription_detail_{sid}")],
                                ]),
                            )
                            c = db_connect()
                            c.execute("UPDATE subscriptions SET expiry_notified=1 WHERE id=?", (sid,))
                            c.commit(); c.close()

                    if not email:
                        continue

                    client = await xui_find_client(email)
                    traffic = client.get("traffic") or {}
                    used = int(traffic.get("up", 0) or 0) + int(traffic.get("down", 0) or 0)
                    total = int(client.get("totalGB") or stored_total or 0)
                    if total <= 0:
                        continue

                    usage_percent = (used / total) * 100.0
                    if usage_percent >= 90.0 and not int(traffic_notified or 0):
                        remaining = max(0, total - used)
                        await application.bot.send_message(
                            chat_id=uid,
                            text=(
                                "⚠️ <b>حجم سرویس شما رو به اتمامه</b>\n\n"
                                f"📊 میزان مصرف: <b>{usage_percent:.1f}٪</b>\n"
                                f"📦 حجم باقی‌مانده: <b>{format_bytes(remaining)}</b>\n\n"
                                "برای ادامه استفاده می‌توانید سرویس را تمدید کنید یا حجم اضافه بخرید."
                            ),
                            parse_mode="HTML",
                            reply_markup=InlineKeyboardMarkup([
                                [InlineKeyboardButton("🔄 تمدید سرویس", callback_data=f"renew_subscription_{sid}")],
                                [InlineKeyboardButton("📦 خرید حجم اضافه", callback_data=f"add_traffic_{sid}")],
                                [InlineKeyboardButton("📋 مشاهده اشتراک", callback_data=f"my_subscription_detail_{sid}")],
                            ]),
                        )
                        traffic_notified = 1

                    c = db_connect()
                    c.execute(
                        "UPDATE subscriptions SET xui_total_bytes=?,xui_used_bytes=?,low_traffic_notified=? WHERE id=?",
                        (total, used, int(traffic_notified or 0), sid),
                    )
                    c.commit(); c.close()
                except Exception as exc:
                    print(f"Subscription monitor error #{sid}:", exc)
    except asyncio.CancelledError:
        raise


async def _start_state_cleanup(application):
    task = application.bot_data.get("_state_cleanup_task")
    if task is None or task.done():
        application.bot_data["_state_cleanup_task"] = asyncio.create_task(_stale_user_state_cleanup(application))
    # فروشگاه محصولات مستقل است؛ مانیتور قدیمی غیرفعال است.


async def _stop_state_cleanup(application):
    task = application.bot_data.pop("_state_cleanup_task", None)
    monitor = application.bot_data.pop("_low_traffic_task", None)
    if monitor is not None:
        monitor.cancel()
        try: await monitor
        except asyncio.CancelledError: pass
    if task is not None:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


# =========================
# اجرای ربات
# =========================

# =========================
# مدیریت خطای سراسری
# =========================
async def global_error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    err = context.error
    print(f"Unhandled bot error: {type(err).__name__}: {err}")

# =========================
# هندلرهای پرداخت استار تلگرام
# =========================
async def pre_checkout_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """پیش‌تأیید پرداخت استار تلگرام"""
    query = update.pre_checkout_query
    # بررسی payload
    if query.invoice_payload.startswith("star_deposit_"):
        await query.answer(ok=True)
    else:
        await query.answer(ok=False, error_message="پرداخت نامعتبر است.")


async def handle_successful_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """پرداخت موفق استار تلگرام - اضافه کردن مبلغ به کیف پول"""
    if not update.message or not update.message.successful_payment:
        return

    payment = update.message.successful_payment
    user_id = update.effective_user.id

    # بررسی payload
    if not payment.invoice_payload.startswith("star_deposit_"):
        return

    try:
        # استخراج تعداد استار از payload: star_deposit_{count}_{user_id}
        parts = payment.invoice_payload.split("_")
        star_count = int(parts[2])

        # محاسبه مبلغ تومان
        toman_amount = star_count * STAR_TO_TOMAN

        # اضافه کردن به موجودی کاربر
        conn = db_connect()
        try:
            cur = conn.cursor()
            cur.execute("BEGIN IMMEDIATE")

            # دریافت موجودی قبلی
            row = cur.execute("SELECT balance FROM users WHERE user_id=?", (user_id,)).fetchone()
            if not row:
                cur.execute("INSERT INTO users (user_id, balance) VALUES (?, ?)", (user_id, 0))
                old_balance = 0
            else:
                old_balance = int(row[0] or 0)

            new_balance = old_balance + toman_amount

            # آپدیت موجودی
            cur.execute("UPDATE users SET balance=? WHERE user_id=?", (new_balance, user_id))

            # ثبت در balance_logs
            cur.execute(
                "INSERT INTO balance_logs (user_id, admin_id, action, amount, old_balance, new_balance, reason) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (user_id, 0, 'add', toman_amount, old_balance, new_balance, f'پرداخت {star_count} استار تلگرام (معادل {toman_amount:,} تومان)')
            )

            conn.commit()
        except Exception as e:
            conn.rollback()
            print("Star payment balance update error:", e)
            await update.message.reply_text("❌ خطا در ثبت پرداخت. لطفاً با پشتیبانی تماس بگیرید.")
            return
        finally:
            conn.close()

        # اطلاع به کاربر
        await update.message.reply_text(
            "✅ <b>پرداخت استار تلگرام با موفقیت انجام شد!</b>\n\n"
            f"⭐️ تعداد استار: {star_count}\n"
            f"💰 مبلغ اضافه شده: {toman_amount:,} تومان\n"
            f"💵 موجودی جدید: {new_balance:,} تومان\n\n"
            "مبلغ بلافاصله به کیف‌پل شما اضافه شد.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("💵 موجودی من", callback_data="my_balance")],
                [InlineKeyboardButton("🏠 برگشت به خانه", callback_data="back_main")]
            ])
        )

    except Exception as e:
        print("Successful star payment handling error:", e)
        await update.message.reply_text("❌ خطا در پردازش پرداخت. لطفاً با پشتیبانی تماس بگیرید.")


# =========================
# پنل تنظیمات ربات (برای ادمین)
# =========================
async def show_admin_settings(query, context):
    if query.from_user.id != ADMIN_ID:
        await safe_query_answer(query, "❌ شما دسترسی ندارید.", show_alert=True)
        return
    conn = db_connect()
    row = conn.execute("SELECT rate FROM star_config WHERE id=1").fetchone()
    conn.close()
    current_rate = int(row[0]) if row else STAR_TO_TOMAN
    style_label = ("🎨 دکمه‌ها: رنگی" if _TELEGRAM_BUTTON_SUPPORTS_STYLE else "🎨 دکمه‌ها: رنگی (نیازمند ارتقای کتابخانه)") if BUTTON_STYLE_MODE == "colorful" else "🫧 دکمه‌ها: شیشه‌ای/پیش‌فرض"
    fj_label = "🟢 عضویت اجباری: روشن" if forced_join_enabled() else "🔴 عضویت اجباری: خاموش"
    card_number = get_bot_setting('card_number', '')
    card_holder = get_bot_setting('card_holder', '')
    kb = [
        [InlineKeyboardButton("💳 تنظیم شماره کارت", callback_data="admin_card_number")],
        [InlineKeyboardButton("👤 تنظیم نام صاحب کارت", callback_data="admin_card_holder")],
        [InlineKeyboardButton("💎 مدیریت ولت‌های رمزارز", callback_data="wallet_admin_home")],
        [InlineKeyboardButton("⭐️ تغییر نرخ استار", callback_data="admin_settings_star_rate")],
        [InlineKeyboardButton(fj_label, callback_data="admin_settings_forced_join_toggle")],
        [InlineKeyboardButton(style_label, callback_data="admin_settings_button_style")],
        [InlineKeyboardButton("🔙 برگشت به پنل مدیریت", callback_data="admin_panel")]
    ]
    note = "\n\nℹ️ رنگ واقعی دکمه‌ها فقط در نسخه‌های جدید Bot API و کتابخانه پشتیبانی‌شده نمایش داده می‌شود؛ در نسخه‌های قدیمی تلگرام، دکمه‌ها ظاهر پیش‌فرض دارند."
    await safe_edit_message(query,
        "⚙️ <b>تنظیمات ربات</b>\n\n"
        f"⭐️ نرخ استار: <b>{current_rate:,} تومان</b>\n"
        f"💳 شماره کارت: <b>{html.escape(card_number or 'تنظیم نشده')}</b>\n"
        f"👤 صاحب کارت: <b>{html.escape(card_holder or 'تنظیم نشده')}</b>\n"
        f"🎨 حالت دکمه‌ها: <b>{('رنگی' if _TELEGRAM_BUTTON_SUPPORTS_STYLE else 'رنگی انتخاب شده، اما کتابخانه فعلی پشتیبانی نمی‌کند') if BUTTON_STYLE_MODE == 'colorful' else 'شیشه‌ای/پیش‌فرض'}</b>"
        + note,
        reply_markup=InlineKeyboardMarkup(kb), parse_mode="HTML")

async def admin_settings_star_rate(update, context, query):
    """تغییر نرخ استار تلگرام"""
    if query.from_user.id != ADMIN_ID:
        await safe_query_answer(query, "❌ شما دسترسی ندارید.", show_alert=True)
        return

    begin_input_state(context)
    context.user_data["waiting_for_star_rate"] = True

    keyboard = [[
        InlineKeyboardButton("‹  برگشت", callback_data="admin_settings")
    ]]

    await safe_edit_message(query,
        "⭐️ <b>تغییر نرخ استار تلگرام</b>\n\n"
        f"نرخ فعلی: {STAR_TO_TOMAN:,} تومان به ازای هر استار\n\n"
        "لطفاً نرخ جدید را به تومان وارد کنید:\n\n"
        "مثال:\n"
        "<code>3500</code> (معادل ۱ استار = ۳،۵۰۰ تومان)\n"
        "<code>4000</code> (معادل ۱ استار = ۴،۰۰۰ تومان)",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="HTML"
    )


# =========================

# =========================
# فروشگاه محصولات مستقل
# =========================
async def products_home(query):
    conn=db_connect()
    cats=conn.execute("SELECT id,name FROM product_categories WHERE active=1 ORDER BY id").fetchall()
    conn.close()
    kb=[[InlineKeyboardButton(str(c['name']),callback_data=f'product_cat_{c["id"]}')] for c in cats]
    kb += [[InlineKeyboardButton('📦 محصولات خریداری‌شده',callback_data='my_products')], [InlineKeyboardButton('‹ برگشت',callback_data='back_main')]]
    await safe_edit_message(query, ('🛍️ <b>Product Shop</b>\n\nChoose a category:' if get_user_language(query.from_user.id)=='en' else '🛍️ <b>فروشگاه محصولات</b>\n\nدسته‌بندی موردنظرت رو انتخاب کن:'),reply_markup=InlineKeyboardMarkup(kb),parse_mode='HTML')

async def product_category_page(query, cid):
    conn=db_connect(); rows=conn.execute('SELECT id,name,price FROM products WHERE category_id=? AND active=1 AND NOT EXISTS (SELECT 1 FROM product_purchases pp WHERE pp.product_id=products.id) ORDER BY id DESC',(cid,)).fetchall(); cat=conn.execute('SELECT name FROM product_categories WHERE id=?',(cid,)).fetchone(); conn.close()
    kb=[[InlineKeyboardButton(f"{r['name']} · {int(r['price']):,} تومان",callback_data=f"product_view_{r['id']}")] for r in rows]
    kb.append([InlineKeyboardButton('‹ دسته‌بندی‌ها',callback_data='buy_subscription')])
    await safe_edit_message(query, f"🗂 <b>{html.escape(cat['name'] if cat else 'دسته‌بندی')}</b>\n\n"+(('No products are available in this category.' if get_user_language(query.from_user.id)=='en' else 'محصولی در این دسته موجود نیست.') if not rows else ('Choose a product:' if get_user_language(query.from_user.id)=='en' else 'محصول موردنظرت رو انتخاب کن:')),reply_markup=InlineKeyboardMarkup(kb),parse_mode='HTML')

async def product_view(query, pid):
    conn=db_connect(); r=conn.execute('SELECT p.*,c.name AS category_name FROM products p LEFT JOIN product_categories c ON c.id=p.category_id WHERE p.id=? AND p.active=1 AND NOT EXISTS (SELECT 1 FROM product_purchases pp WHERE pp.product_id=p.id)',(pid,)).fetchone(); conn.close()
    if not r:
        await safe_query_answer(query,'محصول پیدا نشد یا غیرفعال شده.',show_alert=True); return
    body=f"🛍️ <b>{html.escape(r['name'])}</b>\n🗂 دسته‌بندی: {html.escape(r['category_name'] or '—')}\n💰 قیمت: <b>{int(r['price']):,} تومان</b>\n\n📝 <b>توضیحات محصول</b>\n{html.escape(r['description'] or 'توضیحی ثبت نشده.') }"
    kb=[[InlineKeyboardButton('🎟 Apply Discount Code' if get_user_language(query.from_user.id)=='en' else '🎟 اعمال کد تخفیف',callback_data=f'product_discount_{pid}')],[InlineKeyboardButton('✅ Buy Product' if get_user_language(query.from_user.id)=='en' else '✅ خرید محصول',callback_data=f"product_buy_{pid}")],[InlineKeyboardButton('‹ Back' if get_user_language(query.from_user.id)=='en' else '‹ برگشت',callback_data=f"product_cat_{r['category_id']}")]]
    if r['photo_file_id']:
        try:
            await query.message.reply_photo(photo=r['photo_file_id'],caption=body[:1024],reply_markup=InlineKeyboardMarkup(kb),parse_mode='HTML'); return
        except Exception: pass
    await safe_edit_message(query,body,reply_markup=InlineKeyboardMarkup(kb),parse_mode='HTML')

async def my_products_page(query, uid):
    conn=db_connect(); rows=conn.execute('SELECT id,product_name,price,purchased_at FROM product_purchases WHERE user_id=? ORDER BY id DESC LIMIT 50',(uid,)).fetchall(); conn.close()
    kb=[[InlineKeyboardButton(f"{r['product_name']} · {int(r['price']):,} تومان",callback_data=f"my_product_detail_{r['id']}")] for r in rows]
    kb.append([InlineKeyboardButton('‹ برگشت',callback_data='back_main')])
    await safe_edit_message(query,'📦 <b>محصولات خریداری‌شده</b>\n\n'+('هنوز محصولی خریداری نکردی.' if not rows else 'برای دیدن توضیحات و اطلاعات تحویل روی محصول بزن:'),reply_markup=InlineKeyboardMarkup(kb),parse_mode='HTML')

async def my_product_detail(query, uid, purchase_id):
    conn=db_connect(); r=conn.execute('SELECT * FROM product_purchases WHERE id=? AND user_id=?',(purchase_id,uid)).fetchone(); conn.close()
    if not r: await safe_query_answer(query,'خرید پیدا نشد.',show_alert=True); return
    text=f"📦 <b>{html.escape(r['product_name'])}</b>\n💰 مبلغ خرید: {int(r['price']):,} تومان\n🗓 تاریخ: {html.escape(str(r['purchased_at']))}\n\n📝 <b>توضیحات محصول</b>\n{html.escape(r['description'] or '—')}\n\n🔐 <b>توضیحات تحویل</b>\n{html.escape(r['delivery_text'] or 'اطلاعات تحویل ثبت نشده است.')}"
    await safe_edit_message(query,text,reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('‹ محصولات من',callback_data='my_products')]]),parse_mode='HTML')

async def products_admin_home(query):
    if query.from_user.id != ADMIN_ID: return
    conn=db_connect(); cats=conn.execute('SELECT id,name,active FROM product_categories ORDER BY id DESC').fetchall(); count=conn.execute('SELECT COUNT(*) FROM products').fetchone()[0]; conn.close()
    kb=[[InlineKeyboardButton(('🟢 ' if c['active'] else '⚪ ') + c['name'],callback_data=f"prod_admin_cat_{c['id']}")] for c in cats]
    kb += [[InlineKeyboardButton('➕ افزودن دسته‌بندی',callback_data='prod_admin_addcat')],[InlineKeyboardButton('➕ افزودن محصول',callback_data='prod_admin_add')],[InlineKeyboardButton(f'📦 فهرست محصولات ({count})',callback_data='prod_admin_list')],[InlineKeyboardButton('🧾 محصولات خریداری‌شده',callback_data='admin_purchased_products')],[InlineKeyboardButton('‹ پنل مدیریت',callback_data='admin_panel')]]
    await safe_edit_message(query,'🛒 <b>مدیریت محصولات</b>\n\nابتدا دسته‌بندی بساز، سپس محصول اضافه کن.',reply_markup=InlineKeyboardMarkup(kb),parse_mode='HTML')

async def product_admin_list(query):
    conn=db_connect(); rows=conn.execute('SELECT p.id,p.name,p.price,p.active,c.name AS cat FROM products p LEFT JOIN product_categories c ON c.id=p.category_id ORDER BY p.id DESC').fetchall(); conn.close()
    kb=[[InlineKeyboardButton(f"{'🟢' if r['active'] else '⚪'} {r['name']} · {int(r['price']):,}ت · {r['cat'] or '—'}",callback_data=f"prod_admin_edit_{r['id']}")] for r in rows]
    kb.append([InlineKeyboardButton('‹ مدیریت محصولات',callback_data='products_admin')])
    await safe_edit_message(query,'📦 <b>محصولات</b>\n\n'+('هنوز محصولی ثبت نشده.' if not rows else 'برای تغییر وضعیت یا حذف، محصول را انتخاب کن.'),reply_markup=InlineKeyboardMarkup(kb),parse_mode='HTML')

async def product_callback_router(query, context, data):
    uid=query.from_user.id
    if data == 'toggle_language':
        if uid == ADMIN_ID:
            # ترجمه رابط کاربری ادمین هم عمداً قابل تغییر است، اما پنل مدیریت همیشه فارسی می‌ماند.
            pass
        new_language = 'fa' if get_user_language(uid) == 'en' else 'en'
        set_user_language(uid, new_language)
        await show_main_menu(Update(update_id=0, callback_query=query), context)
        return True
    if data == 'admin_purchased_products':
        if uid != ADMIN_ID:
            await safe_query_answer(query, 'دسترسی ندارید.', show_alert=True); return True
        conn=db_connect()
        rows=conn.execute("SELECT id,product_name,price,purchased_at,user_id FROM product_purchases ORDER BY datetime(purchased_at) DESC, id DESC LIMIT 100").fetchall()
        conn.close()
        kb=[[InlineKeyboardButton(f"#{r['id']} · {r['product_name']} · {int(r['price']):,} تومان", callback_data=f"admin_purchased_detail_{r['id']}")] for r in rows]
        kb.append([InlineKeyboardButton('‹ پنل مدیریت', callback_data='admin_panel')])
        await safe_edit_message(query, '🧾 <b>محصولات خریداری‌شده</b>\n\n'+('هنوز خریدی ثبت نشده.' if not rows else 'خریدها از جدیدترین به قدیمی‌ترین مرتب شده‌اند. برای دیدن خریدار روی محصول بزن:'), reply_markup=InlineKeyboardMarkup(kb), parse_mode='HTML')
        return True
    if data.startswith('admin_purchased_detail_'):
        if uid != ADMIN_ID:
            await safe_query_answer(query, 'دسترسی ندارید.', show_alert=True); return True
        purchase_id=int(data.rsplit('_',1)[1]); conn=db_connect()
        r=conn.execute("SELECT pp.*,u.username,u.full_name FROM product_purchases pp LEFT JOIN users u ON u.user_id=pp.user_id WHERE pp.id=?", (purchase_id,)).fetchone()
        conn.close()
        if not r:
            await safe_query_answer(query, 'خرید پیدا نشد.', show_alert=True); return True
        username=(' @'+r['username']) if r['username'] else 'ندارد'
        name=r['full_name'] or 'نام ثبت نشده'
        body=(f"🧾 <b>جزئیات خرید #{r['id']}</b>\n\n📦 محصول: <b>{html.escape(r['product_name'])}</b>\n💰 مبلغ: <b>{int(r['price']):,} تومان</b>\n🗓 زمان خرید: {html.escape(str(r['purchased_at']))}\n\n👤 نام خریدار: {html.escape(name)}\n🔗 نام کاربری: {html.escape(username)}\n🆔 شناسه کاربر: <code>{int(r['user_id'])}</code>")
        kb=[[InlineKeyboardButton('👤 مشاهده کاربر', callback_data=f"admin_user_{int(r['user_id'])}")],[InlineKeyboardButton('‹ محصولات خریداری‌شده', callback_data='admin_purchased_products')]]
        await safe_edit_message(query, body, reply_markup=InlineKeyboardMarkup(kb), parse_mode='HTML')
        return True
    if data=='buy_subscription': await products_home(query); return True
    if data=='my_subscriptions': await my_products_page(query,uid); return True
    if data=='my_products': await my_products_page(query,uid); return True
    if data.startswith('my_product_detail_'): await my_product_detail(query,uid,int(data.rsplit('_',1)[1])); return True
    if data.startswith('product_cat_'): await product_category_page(query,int(data.rsplit('_',1)[1])); return True
    if data.startswith('product_view_'): await product_view(query,int(data.rsplit('_',1)[1])); return True
    if data.startswith('product_discount_'):
        pid=int(data.rsplit('_',1)[1]); conn=db_connect()
        p=conn.execute('SELECT id,name,price,active FROM products WHERE id=?',(pid,)).fetchone(); conn.close()
        if not p or not p['active']:
            await safe_query_answer(query,'این محصول ناموجود شده است.',show_alert=True); return True
        begin_input_state(context)
        context.user_data['product_discount_pid']=pid
        await safe_edit_message(query, f'🎟 کد تخفیف محصول «{html.escape(p["name"])}» را ارسال کن:', reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('لغو',callback_data=f'product_view_{pid}')]]), parse_mode='HTML')
        return True
    if data.startswith('product_buy_'):
        pid=int(data.rsplit('_',1)[1]); conn=db_connect()
        try:
            conn.execute('BEGIN IMMEDIATE'); p=conn.execute('SELECT * FROM products WHERE id=? AND active=1 AND NOT EXISTS (SELECT 1 FROM product_purchases pp WHERE pp.product_id=products.id)',(pid,)).fetchone(); u=conn.execute('SELECT balance FROM users WHERE user_id=?',(uid,)).fetchone()
            if not p: conn.rollback(); context.user_data.pop('product_discount',None); await safe_query_answer(query,'❌ این محصول ناموجود شده است؛ کاربر دیگری زودتر خرید را تکمیل کرده.',show_alert=True); return True
            final_price=int(p['price'])
            applied=context.user_data.get('product_discount') or {}
            discount_result=None
            if applied.get('pid')==pid:
                discount_result, discount_error = validate_discount(conn, applied.get('code'), uid, int(p['price']))
                if discount_error:
                    conn.rollback(); context.user_data.pop('product_discount',None)
                    await safe_query_answer(query, discount_error, show_alert=True); return True
                final_price=int(discount_result['final'])
            if not u or int(u['balance'] or 0)<final_price:
                conn.rollback()
                balance = int(u['balance'] or 0) if u else 0
                price = final_price
                await safe_query_answer(query, 'موجودی کیف پول کافی نیست.', show_alert=True)
                try:
                    await query.message.reply_text(
                        f"❌ <b>موجودی کافی نیست</b>\n\n📦 محصول: <b>{html.escape(p['name'])}</b>\n"
                        f"💰 قیمت: <b>{price:,} تومان</b>\n💵 موجودی شما: <b>{balance:,} تومان</b>\n"
                        f"💸 مبلغ موردنیاز برای شارژ: <b>{max(0, price-balance):,} تومان</b>\n\n"
                        "برای تکمیل خرید، ابتدا کیف پولت را شارژ کن.",
                        parse_mode='HTML',
                        reply_markup=InlineKeyboardMarkup([
                            [InlineKeyboardButton('💎 شارژ حساب', callback_data='charge')],
                            [InlineKeyboardButton('‹ برگشت به محصول', callback_data=f'product_view_{pid}')]
                        ])
                    )
                except Exception as e:
                    print('Insufficient product balance notification failed:', e)
                return True
            # BEGIN IMMEDIATE serializes competing buyers; this conditional claim ensures one buyer only.
            claimed=conn.execute('UPDATE products SET active=0 WHERE id=? AND active=1 AND NOT EXISTS (SELECT 1 FROM product_purchases pp WHERE pp.product_id=products.id)',(pid,))
            if claimed.rowcount != 1:
                conn.rollback(); context.user_data.pop('product_discount',None); await safe_query_answer(query,'❌ محصول همین الان توسط کاربر دیگری خریداری شد و ناموجود است.',show_alert=True); return True
            conn.execute('UPDATE users SET balance=balance-? WHERE user_id=?',(final_price,uid))
            cur=conn.execute('INSERT INTO product_purchases(user_id,product_id,product_name,description,delivery_text,price) VALUES(?,?,?,?,?,?)',(uid,pid,p['name'],p['description'],p['delivery_text'],final_price))
            purchase_id=cur.lastrowid
            if discount_result:
                updated=conn.execute("UPDATE discount_codes SET used_count=used_count+1 WHERE id=? AND active=1 AND deleted_at IS NULL AND (max_uses=0 OR used_count<max_uses)",(int(discount_result['id']),))
                if updated.rowcount != 1:
                    conn.rollback(); context.user_data.pop('product_discount',None)
                    await safe_query_answer(query,'ظرفیت کد تخفیف همین الان تمام شد؛ دوباره تلاش کن.',show_alert=True); return True
                conn.execute("INSERT INTO product_discount_usages(discount_id,user_id,product_purchase_id,base_price,discount_amount) VALUES(?,?,?,?,?)",
                    (int(discount_result['id']),uid,int(purchase_id),int(p['price']),int(discount_result['discount'])))
            conn.commit(); product=dict(p); product['price']=final_price; context.user_data.pop('product_discount',None)
        except Exception:
            conn.rollback(); raise
        finally: conn.close()
        await safe_query_answer(query,'خرید با موفقیت انجام شد!',show_alert=True)
        await my_product_detail(query,uid,purchase_id)
        try:
            user=query.from_user; await context.bot.send_message(chat_id=ADMIN_ID,text=f"🛒 <b>خرید محصول جدید</b>\n👤 کاربر: {html.escape(user.full_name or '')} (@{html.escape(user.username or 'ندارد')})\n🆔 ID: <code>{uid}</code>\n📦 محصول: <b>{html.escape(product['name'])}</b>\n💰 مبلغ: {int(product['price']):,} تومان\n🧾 خرید: #{purchase_id}",parse_mode='HTML')
        except Exception as e: print('Product purchase admin notification failed:',e)
        return True
    if data.startswith('admin_user_products_'):
        if uid!=ADMIN_ID: await safe_query_answer(query,'دسترسی ندارید.',show_alert=True); return True
        target=int(data.rsplit('_',1)[1]); conn=db_connect(); rows=conn.execute('SELECT id,product_name,price,purchased_at FROM product_purchases WHERE user_id=? ORDER BY id DESC LIMIT 50',(target,)).fetchall(); conn.close()
        kb=[[InlineKeyboardButton(f"#{r['id']} {r['product_name']} · {int(r['price']):,} تومان",callback_data=f"admin_user_product_detail_{target}_{r['id']}")] for r in rows]
        kb.append([InlineKeyboardButton('‹ اطلاعات کاربر',callback_data=f'admin_user_{target}')])
        await safe_edit_message(query,'🛒 <b>خریدهای محصول کاربر</b>\n\n'+('خریدی ثبت نشده.' if not rows else 'برای مشاهده توضیحات و تحویل روی خرید بزن:'),reply_markup=InlineKeyboardMarkup(kb),parse_mode='HTML'); return True
    if data.startswith('admin_user_product_detail_'):
        if uid!=ADMIN_ID: return True
        parts=data.split('_'); target=int(parts[-2]); purchase=int(parts[-1]); conn=db_connect(); r=conn.execute('SELECT * FROM product_purchases WHERE id=? AND user_id=?',(purchase,target)).fetchone(); conn.close()
        if not r: await safe_query_answer(query,'خرید پیدا نشد.',show_alert=True); return True
        text=f"🛒 <b>{html.escape(r['product_name'])}</b>\n👤 کاربر: <code>{target}</code>\n💰 مبلغ: {int(r['price']):,} تومان\n🗓 تاریخ: {html.escape(str(r['purchased_at']))}\n\n📝 <b>توضیحات محصول</b>\n{html.escape(r['description'] or '—')}\n\n🔐 <b>توضیحات تحویل</b>\n{html.escape(r['delivery_text'] or '—')}"
        await safe_edit_message(query,text,reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('‹ خریدهای کاربر',callback_data=f'admin_user_products_{target}')]]),parse_mode='HTML'); return True
    if data=='products_admin': await products_admin_home(query); return True
    if data.startswith('prod_admin_cat_'):
        if uid!=ADMIN_ID: await safe_query_answer(query,'دسترسی ندارید.',show_alert=True); return True
        cid=int(data.rsplit('_',1)[1]); conn=db_connect(); conn.execute('UPDATE product_categories SET active=1-active WHERE id=?',(cid,)); conn.commit(); conn.close(); await products_admin_home(query); return True
    if data=='prod_admin_addcat':
        if uid!=ADMIN_ID: await safe_query_answer(query,'دسترسی ندارید.',show_alert=True); return True
        begin_input_state(context); context.user_data['product_admin_state']='category_name'; await safe_edit_message(query,'نام دسته‌بندی جدید را ارسال کن:',reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('لغو',callback_data='products_admin')]])); return True
    if data=='prod_admin_add':
        if uid!=ADMIN_ID: await safe_query_answer(query,'دسترسی ندارید.',show_alert=True); return True
        conn=db_connect(); cats=conn.execute('SELECT id,name FROM product_categories WHERE active=1 ORDER BY id').fetchall(); conn.close()
        if not cats: await safe_query_answer(query,'اول یک دسته‌بندی بساز.',show_alert=True); return True
        context.user_data['product_admin_state']='product_category'; context.user_data['product_admin_categories']=[(int(c['id']),c['name']) for c in cats]
        await safe_edit_message(query,'دسته‌بندی محصول را انتخاب کن:',reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(c['name'],callback_data=f"prod_admin_choosecat_{c['id']}")] for c in cats]+[[InlineKeyboardButton('لغو',callback_data='products_admin')]])); return True
    if data.startswith('prod_admin_choosecat_'):
        if uid!=ADMIN_ID: return True
        context.user_data['product_draft']={'category_id':int(data.rsplit('_',1)[1])}; context.user_data['product_admin_state']='product_name'
        await safe_edit_message(query,'نام محصول را ارسال کن:'); return True
    if data=='prod_admin_list': await product_admin_list(query); return True
    if data.startswith('prod_admin_edit_'):
        if uid!=ADMIN_ID: return True
        pid=int(data.rsplit('_',1)[1]); conn=db_connect(); r=conn.execute('SELECT * FROM products WHERE id=?',(pid,)).fetchone(); conn.close()
        if not r: await safe_query_answer(query,'محصول پیدا نشد.',show_alert=True); return True
        kb=[[InlineKeyboardButton('🟢/⚪ تغییر وضعیت',callback_data=f'prod_admin_toggle_{pid}')],[InlineKeyboardButton('🗑 حذف محصول',callback_data=f'prod_admin_delete_{pid}')],[InlineKeyboardButton('‹ فهرست',callback_data='prod_admin_list')]]
        await safe_edit_message(query,f"📦 <b>{html.escape(r['name'])}</b>\nدسته: {r['category_id']}\nقیمت: {int(r['price']):,} تومان\n\nتوضیحات:\n{html.escape(r['description'])}\n\nتحویل:\n{html.escape(r['delivery_text'])}",reply_markup=InlineKeyboardMarkup(kb),parse_mode='HTML'); return True
    if data.startswith('prod_admin_toggle_'):
        if uid!=ADMIN_ID: return True
        pid=int(data.rsplit('_',1)[1]); conn=db_connect(); conn.execute('UPDATE products SET active=1-active WHERE id=?',(pid,)); conn.commit(); conn.close(); await product_admin_list(query); return True
    if data.startswith('prod_admin_delete_'):
        if uid!=ADMIN_ID: return True
        pid=int(data.rsplit('_',1)[1]); conn=db_connect(); conn.execute('DELETE FROM products WHERE id=?',(pid,)); conn.commit(); conn.close(); await product_admin_list(query); return True
    return False

async def product_text_router(update, context):
    state=context.user_data.get('product_admin_state')
    if not state: return False
    if update.effective_user.id!=ADMIN_ID: context.user_data.pop('product_admin_state',None); return True
    msg=update.message; value=(msg.text or '').strip()
    if state=='category_name':
        if not value: await msg.reply_text('نام دسته‌بندی خالی است؛ دوباره ارسال کن.'); return True
        conn=db_connect()
        try: conn.execute('INSERT INTO product_categories(name) VALUES(?)',(value,)); conn.commit()
        except sqlite3.IntegrityError: await msg.reply_text('این دسته‌بندی از قبل وجود دارد.'); return True
        finally: conn.close()
        context.user_data.pop('product_admin_state',None); await msg.reply_text('✅ دسته‌بندی ثبت شد.'); return True
    draft=context.user_data.setdefault('product_draft',{})
    if state=='product_name': draft['name']=value; context.user_data['product_admin_state']='product_description'; await msg.reply_text('توضیحات محصول را ارسال کن. فاصله‌ها و خط‌های خالی عیناً حفظ می‌شوند:'); return True
    if state=='product_description': draft['description']=msg.text or ''; context.user_data['product_admin_state']='product_delivery'; await msg.reply_text('توضیحات تحویل را ارسال کن (مثلاً username و password).'); return True
    if state=='product_delivery': draft['delivery_text']=msg.text or ''; context.user_data['product_admin_state']='product_price'; await msg.reply_text('قیمت محصول را فقط به عدد تومان ارسال کن؛ مثال: 150000'); return True
    if state=='product_price':
        try: price=int(value.replace(',','').replace('٬',''))
        except ValueError: await msg.reply_text('قیمت باید عدد صحیح باشد؛ دوباره بفرست.'); return True
        if price<0: await msg.reply_text('قیمت نمی‌تواند منفی باشد.'); return True
        draft['price']=price; context.user_data['product_admin_state']='product_photo'; await msg.reply_text('حالا عکس محصول را ارسال کن؛ اگر عکس نمی‌خواهی بنویس «رد».'); return True
    if state=='product_photo' and value.lower() in ('رد','skip','بدون عکس','ندارد'):
        await save_product_draft(msg,context,None); return True
    return True

async def save_product_draft(message, context, photo_id):
    draft=context.user_data.get('product_draft') or {}
    try:
        conn=db_connect(); conn.execute('INSERT INTO products(category_id,name,description,delivery_text,price,photo_file_id) VALUES(?,?,?,?,?,?)',(draft['category_id'],draft['name'],draft.get('description',''),draft.get('delivery_text',''),int(draft['price']),photo_id)); conn.commit(); conn.close()
        await message.reply_text('✅ محصول با موفقیت ثبت شد.')
    except Exception as e:
        print('Product save failed:',e); await message.reply_text('❌ ذخیره محصول انجام نشد؛ جزئیات در لاگ ثبت شد.')
    context.user_data.pop('product_admin_state',None); context.user_data.pop('product_draft',None)

async def product_media_router(update, context):
    if context.user_data.get('product_admin_state')=='product_photo' and update.effective_user.id==ADMIN_ID:
        photo=update.message.photo
        if photo:
            await save_product_draft(update.message,context,photo[-1].file_id); return True
        await update.message.reply_text('لطفاً عکس بفرست یا برای رد کردن بنویس «رد».'); return True
    return False

def _telegram_httpx_request(proxy_url):
    """Create a PTB HTTPXRequest compatible with multiple PTB versions."""
    params = inspect.signature(HTTPXRequest).parameters
    kwargs = {}
    if proxy_url:
        if "proxy" in params:
            kwargs["proxy"] = proxy_url
        elif "proxy_url" in params:
            kwargs["proxy_url"] = proxy_url
        else:
            raise RuntimeError(
                "نسخه python-telegram-bot نصب‌شده پارامتر پراکسی را پشتیبانی نمی‌کند؛ "
                "لطفاً python-telegram-bot را به نسخه جدیدتر ارتقا بده."
            )
    return HTTPXRequest(**kwargs)


def main():
    if not _TELEGRAM_BUTTON_SUPPORTS_STYLE:
        print("Button colors disabled: installed python-telegram-bot InlineKeyboardButton does not support the style parameter. Upgrade python-telegram-bot to a Bot API-compatible release to enable colored buttons.")
    init_db()
    load_runtime_settings()
    # Route both Bot API calls and long polling through the configured SOCKS5 proxy.
    proxy_url = TELEGRAM_PROXY or None
    builder = Application.builder().token(BOT_TOKEN)
    if proxy_url:
        builder = builder.request(_telegram_httpx_request(proxy_url))
        builder = builder.get_updates_request(_telegram_httpx_request(proxy_url))
        print("Telegram SOCKS5 proxy enabled: tun.biastore.ir:12015")
    app = (
        builder
        .post_init(_start_state_cleanup)
        .post_shutdown(_stop_state_cleanup)
        .build()
    )
    app.add_handler(
        CommandHandler("start", with_state_cleanup(start))
    )

    app.add_handler(
        CallbackQueryHandler(
            with_state_cleanup(check_membership),
            pattern="^check_membership$"
        )
    )

    app.add_handler(
        CallbackQueryHandler(
            with_state_cleanup(admin_panel),
            pattern="^admin_panel$"
        )
    )

    app.add_handler(
        CallbackQueryHandler(with_state_cleanup(buttons))
    )
    app.add_error_handler(global_error_handler)

    # پیام‌های متنی
    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            with_state_cleanup(handle_all_text_messages)
        )
    )

    # تمام مدیاها ابتدا وارد router مرکزی می‌شوند تا عکس اشتراک با رسید پرداخت اشتباه نشود.
    app.add_handler(
        MessageHandler(
            filters.PHOTO | filters.Document.ALL | filters.VIDEO | filters.AUDIO | filters.VOICE | filters.VIDEO_NOTE | filters.Sticker.ALL,
            with_state_cleanup(handle_media_messages)
        )
    )

    # تلگرام استارز: پیش‌تأیید پرداخت
    from telegram.ext import PreCheckoutQueryHandler
    app.add_handler(PreCheckoutQueryHandler(pre_checkout_handler))

    # تلگرام استارز: پرداخت موفق
    app.add_handler(
        MessageHandler(
            filters.SUCCESSFUL_PAYMENT,
            with_state_cleanup(handle_successful_payment)
        )
    )

    # ر دریافت متن (هم برای مبلغ و هم برای پیام همگانی)
    # ...

    print("Bot is running...")

    # Python 3.14 no longer creates a current event loop automatically.
    # python-telegram-bot run_polling() expects one to exist.
    asyncio.set_event_loop(asyncio.new_event_loop())
    app.run_polling()


if __name__ == "__main__":
    main()
