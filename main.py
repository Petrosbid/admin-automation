import asyncio
import importlib
import logging
import msvcrt
import sys
import traceback
from aiogram import Bot, Dispatcher, Router, BaseMiddleware
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.filters import Command
from aiogram.types import Message, TelegramObject

from core.config_loader import cfg
from core.proxy_manager import ProxyManager
import core.queue_manager as queue_mod
from core.queue_manager import queue_engine

import services.monitor_service as monitor_mod

import bot.queue_handlers as queue_handlers_mod
import bot.download_handlers as download_handlers_mod
import bot.subtitle_handlers as subtitle_handlers_mod
import bot.handlers as main_handlers_mod

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)

# لیست ماژول‌های قابل ریلود زنده با کلید R (اکنون شامل core.queue_manager و services.automation_service)
RELOADABLE_MODULES_ORDER = [
    "core.proxy_manager",
    "core.queue_manager",
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

system_router = Router()


class RegisterAdminChatMiddleware(BaseMiddleware):
    """ثبت خودکار آیدی چت ادمین در QueueManager با هر کلیک یا پیام برای تضمین ارسال نوتیفیکیشن‌ها"""
    async def __call__(self, handler, event: TelegramObject, data: dict):
        user = data.get("event_from_user")
        if user and user.id in (cfg.get("telegram", "admin_ids", []) or []):
            queue_mod.queue_engine.register_chat(user.id)
        return await handler(event, data)


def create_bot_session(proxy_url: str | None) -> AiohttpSession:
    # از دست‌کاری private API کتابخانه و غیرفعال‌کردن اعتبارسنجی TLS خودداری می‌کنیم.
    return AiohttpSession(proxy=proxy_url) if proxy_url else AiohttpSession()


def _attach_all_routers(dp: Dispatcher):
    dp.sub_routers.clear()

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
    logging.info("🔄 [HOT-RELOAD] در حال بروزرسانی کدها و تنظیمات بدون توقف پراسس‌ها...")
    try:
        importlib.invalidate_caches()
        cfg.reload()

        if hasattr(monitor_mod, "monitor_service"):
            monitor_mod.monitor_service.stop()

        active_engine = queue_mod.queue_engine

        reloaded_count = 0
        for mod_name in RELOADABLE_MODULES_ORDER:
            if mod_name in sys.modules:
                importlib.reload(sys.modules[mod_name])
                reloaded_count += 1

                # مهم: reload کردن core.queue_manager یک singleton تازه می‌سازد.
                # باید همان لحظه نمونه زنده قبلی را برگردانیم تا ماژول‌های بعدی
                # (automation_service و handlerها) دقیقاً همان queue_engine را import کنند.
                if mod_name == "core.queue_manager":
                    queue_mod.queue_engine = active_engine
                    active_engine.__class__ = queue_mod.QueueManager

        active_engine.__class__ = queue_mod.QueueManager
        if not hasattr(active_engine, "_known_chats"):
            active_engine._known_chats = set()
        active_engine.set_bot(bot)
        active_engine.restart_workers_for_hot_reload()
        queue_mod.queue_engine = active_engine

        _attach_all_routers(dp)

        if hasattr(monitor_mod, "monitor_service"):
            monitor_mod.monitor_service.start(bot)

        msg = f"✅ [HOT-RELOAD] تعداد {reloaded_count} ماژول و ورکرهای نوتیفیکیشن با موفقیت آپدیت شدند!"
        logging.info(msg)
        return True, msg

    except Exception as e:
        err_trace = traceback.format_exc()
        logging.error(f"❌ [HOT-RELOAD ERROR] خطا در کدهای ویرایش شده:\n{err_trace}")
        return False, f"❌ خطا در بروزرسانی کدها:\n`{e}`"

@system_router.message(Command("reload"))
async def cmd_telegram_hot_reload(message: Message, dispatcher: Dispatcher):
    admin_ids = cfg.get("telegram", "admin_ids", [])
    if message.from_user.id not in admin_ids:
        return
    ok, report = await hot_reload_code(dispatcher, message.bot)
    await message.answer(report, parse_mode="Markdown")


async def console_key_listener(dp: Dispatcher, bot: Bot):
    logging.info("⌨️  [HOT-RELOAD READY] هر زمان کدها را تغییر دادید، کلید 'R' را در همین کنسول فشار دهید.")
    while True:
        try:
            if msvcrt.kbhit():
                ch = msvcrt.getwch()
                if ch in ("r", "R", "ق"):
                    await hot_reload_code(dp, bot)
                elif ch in ("\x00", "\xe0"):
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
    dp.update.outer_middleware(RegisterAdminChatMiddleware())

    _attach_all_routers(dp)
    logging.info("Telegram routers registered successfully.")

    console_task: asyncio.Task | None = None

    try:
        logging.info("Connecting to Telegram...")
        await bot.delete_webhook(drop_pending_updates=True)
        logging.info("Telegram connection established.")

        queue_engine.set_bot(bot)
        queue_engine.start_workers()
        logging.info("Queue workers started.")

        monitor_mod.monitor_service.start(bot)
        logging.info("Monitor service started.")

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