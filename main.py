import asyncio
import logging
from aiogram import Bot, Dispatcher
from aiogram.client.session.aiohttp import AiohttpSession
from core.config_loader import cfg
from core.proxy_manager import ProxyManager
from core.queue_manager import queue_engine
from services.monitor_service import monitor_service
from bot.handlers import router

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")


async def main():
    bot_token = cfg.get("telegram", "bot_token")
    proxy_url = ProxyManager.get_proxy_url()

    # اتصال خودکار ربات تلگرام به پروکسی در صورت فعال بودن در config.yaml
    session = AiohttpSession(proxy=proxy_url) if proxy_url else None
    bot = Bot(token=bot_token, session=session)
    dp = Dispatcher()
    dp.include_router(router)

    # ۱. راه‌اندازی ورکرهای صف ترتیبی (HandBrake, MKVToolNix, Download, CuteFTP)
    queue_engine.start_workers()
    logging.info("All Sequential Queue Workers Started (MKVToolNix, HandBrake, Download, CuteFTP).")

    # ۲. راه‌اندازی سرویس مانیتورینگ ۵ دقیقه‌ای سایت‌های Pahe.ink, PSA.wf و YTS.gg
    monitor_service.start(bot)

    await bot.delete_webhook(drop_pending_updates=True)
    logging.info("Telegram Bot Polling Started...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())