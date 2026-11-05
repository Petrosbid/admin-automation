import ssl

# غیرفعال‌سازی سراسری بررسی گواهی Self-Signed SSL در ابتدای برنامه
_orig_create_default_context = ssl.create_default_context


def _unverified_ssl_context(*args, **kwargs) -> ssl.SSLContext:
    ctx = _orig_create_default_context(*args, **kwargs)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


ssl.create_default_context = _unverified_ssl_context
ssl._create_default_https_context = _unverified_ssl_context

import asyncio
import importlib
import logging
import msvcrt
import sys
import traceback
import urllib3
from aiohttp import TCPConnector
from aiogram import Bot, Dispatcher, Router
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.filters import Command
from aiogram.types import Message

from core.config_loader import cfg
from core.proxy_manager import ProxyManager
from core.queue_manager import queue_engine

import services.monitor_service as monitor_mod

# ایمپورت ماژول‌های روتر برای قابلیت Reload زنده
import bot.queue_handlers as queue_handlers_mod
import bot.download_handlers as download_handlers_mod
import bot.subtitle_handlers as subtitle_handlers_mod
import bot.handlers as main_handlers_mod

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)

# لیست ترتیبی ماژول‌هایی که هنگام زدن کلید R باید از روی دیسک آپدیت شوند
# (توجه: core.queue_manager در این لیست نیست تا صف و پراسس‌های در حال اجرا حفظ شوند)
RELOADABLE_MODULES_ORDER = [
    "core.proxy_manager",
    "services.process_utils",
    "services.subtitle_service",
    "services.mkv_service",
    "services.handbrake_service",
    "services.cuteftp_worker",
    "services.cuteftp_service",
    "services.scraper_service",
    "services.downloader_service",
    "services.automation_service",
    "services.monitor_service",
    "bot.keyboards",
    "bot.queue_handlers",
    "bot.download_handlers",
    "bot.subtitle_handlers",
    "bot.handlers",
]

# روتر ثابت سیستمی برای دستور /reload از داخل تلگرام
system_router = Router()


def create_bot_session(proxy_url: str | None) -> AiohttpSession:
    session = AiohttpSession(proxy=proxy_url) if proxy_url else AiohttpSession()
    session._connector_init["ssl"] = _unverified_ssl_context()
    if not proxy_url:
        session._connector_type = TCPConnector
    return session


def _attach_all_routers(dp: Dispatcher):
    """جدا کردن روترهای قبلی و اتصال نسخه جدید روترها به Dispatcher"""
    dp.sub_routers.clear()

    # پاکسازی انتساب والد قبلی در صورت وجود
    for r in (
        system_router,
        queue_handlers_mod.router,
        download_handlers_mod.router,
        subtitle_handlers_mod.router,
        main_handlers_mod.router,
    ):
        r._parent_router = None

    dp.include_router(system_router)
    dp.include_router(queue_handlers_mod.router)
    dp.include_router(download_handlers_mod.router)
    dp.include_router(subtitle_handlers_mod.router)
    dp.include_router(main_handlers_mod.router)


async def hot_reload_code(dp: Dispatcher, bot: Bot) -> tuple[bool, str]:
    """
    بازخوانی زنده تمامی فایل‌های کد و config.yaml بدون قطع شدن پراسس‌های در حال اجرا
    """
    logging.info("🔄 [HOT-RELOAD] در حال بروزرسانی کدها و تنظیمات بدون توقف پراسس‌ها...")
    try:
        importlib.invalidate_caches()

        # ۱. بازخوانی فایل config.yaml
        cfg.reload()

        # ۲. توقف موقت لوپ مانیتورینگ قبلی قبل از ریلود شدن ماژول آن
        if hasattr(monitor_mod, "monitor_service"):
            monitor_mod.monitor_service.stop()

        # ۳. بازخوانی ترتیبی تمام سرویس‌ها، کیبوردها و هندلرها
        reloaded_count = 0
        for mod_name in RELOADABLE_MODULES_ORDER:
            if mod_name in sys.modules:
                importlib.reload(sys.modules[mod_name])
                reloaded_count += 1

        # ۴. جایگزینی روترهای جدید در Dispatcher تلگرام
        _attach_all_routers(dp)

        # ۵. راه‌اندازی مجدد مانیتورینگ با کدهای جدید
        if hasattr(monitor_mod, "monitor_service"):
            monitor_mod.monitor_service.start(bot)

        msg = f"✅ [HOT-RELOAD] تعداد {reloaded_count} ماژول و فایل config.yaml با موفقیت آپدیت شدند! (پراسس‌های فعال بدون وقفه ادامه دارند)"
        logging.info(msg)
        return True, msg

    except Exception as e:
        err_trace = traceback.format_exc()
        logging.error(f"❌ [HOT-RELOAD ERROR] خطا در کدهای ویرایش شده:\n{err_trace}")
        return False, f"❌ خطا در بروزرسانی کدها:\n`{e}`"


@system_router.message(Command("reload"))
async def cmd_telegram_hot_reload(message: Message, dispatcher: Dispatcher):
    """قابلیت آپدیت کدها از داخل خود تلگرام با دستور /reload"""
    admin_ids = cfg.get("telegram", "admin_ids", [])
    if message.from_user.id not in admin_ids:
        return
    ok, report = await hot_reload_code(dispatcher, message.bot)
    await message.answer(report, parse_mode="Markdown")


async def console_key_listener(dp: Dispatcher, bot: Bot):
    """
    گوش دادن دائمی به کیبورد در پنجره کنسول ویندوز:
    با فشردن کلید R (یا r / ق) کدها درجا آپدیت می‌شوند.
    """
    logging.info("⌨️  [HOT-RELOAD READY] هر زمان کدها را تغییر دادید، کلید 'R' را در همین کنسول فشار دهید.")
    while True:
        try:
            if msvcrt.kbhit():
                ch = msvcrt.getwch()
                # پشتیبانی از R، r و کلید هم‌ردیف در کیبورد فارسی ('ق')
                if ch in ("r", "R", "ق"):
                    await hot_reload_code(dp, bot)
                elif ch in ("\x00", "\xe0"):
                    # رد کردن کد دوم کلیدهای جهتی/تابعی ویندوز
                    msvcrt.getwch()
        except Exception as e:
            logging.debug(f"Console key listener warning: {e}")
        await asyncio.sleep(0.15)


async def main():
    bot_token = cfg.get("telegram", "bot_token")
    if not bot_token:
        raise ValueError("Telegram bot token is missing in config.yaml")

    proxy_url = ProxyManager.get_proxy_url()
    if proxy_url:
        logging.info(f"Using Proxy: {proxy_url}")
    else:
        logging.info("Connecting directly without proxy...")

    session = create_bot_session(proxy_url)
    bot = Bot(token=bot_token, session=session)
    dp = Dispatcher()

    # اتصال اولیه تمام روترها
    _attach_all_routers(dp)
    logging.info("Telegram routers registered successfully.")

    console_task: asyncio.Task | None = None

    try:
        logging.info("Connecting to Telegram...")
        await bot.delete_webhook(drop_pending_updates=True)
        logging.info("Telegram connection established.")

        if hasattr(queue_engine, "set_bot"):
            queue_engine.set_bot(bot)
        queue_engine.start_workers()
        logging.info("Queue workers started.")

        monitor_mod.monitor_service.start(bot)
        logging.info("Monitor service started.")

        # شروع شنونده کلید R در کنسول ویندوز
        console_task = asyncio.create_task(console_key_listener(dp, bot))

        logging.info("Telegram Bot Polling Started...")
        await dp.start_polling(bot, close_bot_session=False)

    except KeyboardInterrupt:
        logging.info("Bot stopped by user.")
    finally:
        logging.info("Stopping background services and closing Telegram session...")
        if console_task and not console_task.done():
            console_task.cancel()
        monitor_mod.monitor_service.stop()
        await asyncio.sleep(0.2)
        await bot.session.close()
        logging.info("Telegram session closed cleanly.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.info("Application shutdown completed.")