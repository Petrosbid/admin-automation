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
import logging
import urllib3
from aiohttp import TCPConnector
from aiogram import Bot, Dispatcher
from aiogram.client.session.aiohttp import AiohttpSession

from core.config_loader import cfg
from core.proxy_manager import ProxyManager
from core.queue_manager import queue_engine
from services.monitor_service import monitor_service

# Routers
from bot.queue_handlers import router as queue_router
from bot.download_handlers import router as download_router
from bot.subtitle_handlers import router as subtitle_router
from bot.handlers import router as main_router

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)


def create_bot_session(proxy_url: str | None) -> AiohttpSession:
    session = AiohttpSession(proxy=proxy_url) if proxy_url else AiohttpSession()
    session._connector_init["ssl"] = _unverified_ssl_context()
    if not proxy_url:
        session._connector_type = TCPConnector
    return session


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

    # ثبت روترها (دستور /settings داخل main_router تعبیه شده است)
    dp.include_router(queue_router)
    dp.include_router(download_router)
    dp.include_router(subtitle_router)
    dp.include_router(main_router)

    logging.info("Telegram routers registered successfully.")

    try:
        logging.info("Connecting to Telegram...")
        await bot.delete_webhook(drop_pending_updates=True)
        logging.info("Telegram connection established.")

        if hasattr(queue_engine, "set_bot"):
            queue_engine.set_bot(bot)
        queue_engine.start_workers()
        logging.info("Queue workers started.")

        monitor_service.start(bot)
        logging.info("Monitor service started.")

        logging.info("Telegram Bot Polling Started...")
        await dp.start_polling(bot, close_bot_session=False)

    except KeyboardInterrupt:
        logging.info("Bot stopped by user.")
    finally:
        logging.info("Stopping background services and closing Telegram session...")
        monitor_service.stop()
        await asyncio.sleep(0.2)
        await bot.session.close()
        logging.info("Telegram session closed cleanly.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.info("Application shutdown completed.")