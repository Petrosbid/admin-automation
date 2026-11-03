import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.session.aiohttp import AiohttpSession

from core.config_loader import cfg
from core.proxy_manager import ProxyManager
from core.queue_manager import queue_engine

from services.monitor_service import monitor_service

from bot.handlers import router


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)


async def main():

    bot_token = cfg.get(
        "telegram",
        "bot_token"
    )

    if not bot_token:
        raise RuntimeError(
            "Telegram bot token is not configured."
        )

    proxy_url = ProxyManager.get_proxy_url()

    session = (
        AiohttpSession(proxy=proxy_url)
        if proxy_url
        else None
    )

    bot = Bot(
        token=bot_token,
        session=session
    )

    dp = Dispatcher()

    dp.include_router(router)

    # معرفی Bot به سیستم صف برای ارسال اعلان پایان پردازش
    queue_engine.set_bot(bot)

    # راه‌اندازی Workerهای مستقل
    queue_engine.start_workers()

    logging.info(
        "Queue workers started successfully."
    )

    # راه‌اندازی مانیتورینگ
    monitor_service.start(bot)

    await bot.delete_webhook(
        drop_pending_updates=True
    )

    logging.info(
        "Telegram Bot Polling Started..."
    )

    try:

        await dp.start_polling(bot)

    finally:

        logging.info(
            "Stopping background services..."
        )

        monitor_service.stop()

        await queue_engine.stop_workers()

        await bot.session.close()


if __name__ == "__main__":

    try:

        asyncio.run(main())

    except KeyboardInterrupt:

        logging.info(
            "Application stopped by user."
        )