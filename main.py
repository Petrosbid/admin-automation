import asyncio
import datetime
import importlib
import logging
import sys
import time
from pathlib import Path
from typing import Any

from aiogram import Bot, Dispatcher, BaseMiddleware
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.session.middlewares.base import BaseRequestMiddleware, NextRequestMiddlewareType
from aiogram.exceptions import TelegramRetryAfter, TelegramBadRequest
from aiogram.methods import (
    TelegramMethod,
    EditMessageText,
    EditMessageReplyMarkup,
    SendMessage,
    AnswerCallbackQuery,
)
from aiogram.methods.base import Response, TelegramType
from aiogram.types import TelegramObject, Message, CallbackQuery, Chat

from core.config_loader import cfg
import core.proxy_manager as proxy_mod
import core.queue_manager as queue_mod

import services.process_utils as process_utils_mod
import services.subtitle_service as subtitle_service_mod
import services.mkv_service as mkv_service_mod
import services.handbrake_service as handbrake_service_mod
import services.downloader_service as downloader_service_mod
import services.cuteftp_worker as cuteftp_worker_mod
import services.cuteftp_service as cuteftp_service_mod
import services.scraper_service as scraper_service_mod
import services.monitor_service as monitor_service_mod
import services.automation_service as automation_service_mod

import bot.keyboards as keyboards_mod
import bot.handlers as handlers_mod
import bot.subtitle_handlers as subtitle_handlers_mod
import bot.download_handlers as download_handlers_mod
import bot.queue_handlers as queue_handlers_mod
import bot.settings_handlers as settings_handlers_mod

try:
    import services.prowlarr_service as prowlarr_service_mod
    import bot.prowlarr_handlers as prowlarr_handlers_mod
    HAS_PROWLARR = True
except ImportError:
    prowlarr_service_mod = None
    prowlarr_handlers_mod = None
    HAS_PROWLARR = False


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("mediabot.log", encoding="utf-8")
    ]
)


def _resolve_bot_session() -> AiohttpSession | None:
    pm = getattr(proxy_mod, "ProxyManager", None)
    if pm is not None:
        for method_name in (
            "get_bot_session",
            "create_bot_session",
            "get_session",
            "create_session",
            "get_aiogram_session",
            "build_session",
        ):
            fn = getattr(pm, method_name, None)
            if callable(fn):
                return fn()

        for url_method in ("get_proxy_url", "get_telegram_proxy", "get_proxy"):
            fn = getattr(pm, url_method, None)
            if callable(fn):
                proxy_url = fn()
                return AiohttpSession(proxy=proxy_url) if proxy_url else AiohttpSession()

    if bool(cfg.get("proxy", "enabled", False)):
        p_url = str(cfg.get("proxy", "url", "") or "").strip()
        if p_url:
            return AiohttpSession(proxy=p_url)

    return AiohttpSession()


class FloodSafeSessionMiddleware(BaseRequestMiddleware):
    """
    لایه مدیریت هوشمند نرخ درخواست‌های تلگرام (بدون پاک کردن پیام‌ها!):
    1. حفظ کامل EditMessageText برای منوها و چک‌لیست‌ها.
    2. اعمال تایمر دیلی (Pacer) بین SendMessage ها و EditMessageText های پس‌زمینه تا هرگز Flood رخ ندهد.
    3. برگرداندن یک شیء Message معتبر در زمان رد کردن پیام‌های موقت ('⏳ در حال...') تا کدهای بعدی (مثل استخراج زیرنویس یا سرچ Workspace) هرگز متوقف نشوند!
    """
    def __init__(self):
        self.edit_ban_until: dict[int | str, float] = {}
        self.send_ban_until: dict[int | str, float] = {}
        self.msg_edit_history: dict[tuple[int | str, int], tuple[float, str]] = {}
        self.msg_edit_counts: dict[tuple[int | str, int], int] = {}
        self.last_send_ts: dict[int | str, float] = {}
        self.send_locks: dict[int | str, asyncio.Lock] = {}

        # فاصله ایمن برای آپدیت‌های پس‌زمینه (داشبورد صف و وضعیت اتوماسیون)
        self.bg_edit_interval = 10.0
        # فاصله ایمن بین ارسال پیام‌های جدید (SendMessage) پشت سر هم
        self.min_send_delay = 1.6

    @staticmethod
    def _build_dummy_message(chat_id: int | str, message_id: int | None = None, text: str = "") -> Message:
        """
        ساخت یک شیء Message معتبر زمانی که درخواست به خاطر Throttle یا Flood رد می‌شود؛
        این کار تضمین می‌کند خطوط بعدی کد مثل wait_msg.edit_text(...) یا استخراج زیرنویس هرگز کرش یا قفل نکنند!
        """
        cid = int(chat_id) if str(chat_id).lstrip("-").isdigit() else 0
        mid = int(message_id) if message_id else 1
        return Message(
            message_id=mid,
            date=datetime.datetime.now(datetime.timezone.utc),
            chat=Chat(id=cid, type="private"),
            text=text or "..."
        )

    @staticmethod
    def _is_background_status_text(txt: str) -> bool:
        bg_markers = (
            "داشبورد زنده صف",
            "وضعیت زنده اتوماسیون",
            "وضعیت اجرای اتوماسیون",
            "در حال اجرای اتوماسیون",
            "پیشرفت:",
        )
        return any(m in txt for m in bg_markers)

    @staticmethod
    def _is_transient_loading_text(txt: str) -> bool:
        """تشخیص پیام‌های موقت مثل '⏳ در حال بررسی لینک...' یا '⏳ در حال استخراج زیرنویس انگلیسی...'"""
        t = txt.strip()
        return t.startswith("⏳") and ("در حال " in t or "لطفاً" in t)

    async def _paced_send_message(
        self,
        make_request: NextRequestMiddlewareType[TelegramType],
        bot: Bot,
        method: SendMessage
    ) -> Any:
        chat_id = method.chat_id
        now = time.monotonic()

        # اگر SendMessage توسط تلگرام بن شده باشد:
        ban_until = self.send_ban_until.get(chat_id, 0.0)
        if now < ban_until:
            rem = ban_until - now
            # اگر کمتر از ۲۰ ثانیه مانده، صبر کن و بفرست؛ در غیر این صورت Dummy برگردان تا برنامه قفل نکند
            if rem <= 20.0:
                await asyncio.sleep(rem + 0.5)
            else:
                logging.warning(f"⏳ [FloodGuard] SendMessage still banned for {rem:.0f}s in chat {chat_id}. Skipping message.")
                return self._build_dummy_message(chat_id, 1, str(method.text or ""))

        lock = self.send_locks.setdefault(chat_id, asyncio.Lock())
        async with lock:
            now = time.monotonic()
            elapsed = now - self.last_send_ts.get(chat_id, 0.0)
            if elapsed < self.min_send_delay:
                await asyncio.sleep(self.min_send_delay - elapsed)

            try:
                resp = await make_request(bot, method)
                self.last_send_ts[chat_id] = time.monotonic()
                return resp
            except TelegramRetryAfter as e:
                wait_s = float(getattr(e, "retry_after", 15))
                self.send_ban_until[chat_id] = time.monotonic() + wait_s
                logging.warning(f"🛡️ [FloodGuard] SendMessage rate-limited for {wait_s:.0f}s in chat {chat_id}.")
                if wait_s <= 20.0:
                    await asyncio.sleep(wait_s + 1.0)
                    resp = await make_request(bot, method)
                    self.last_send_ts[chat_id] = time.monotonic()
                    return resp
                return self._build_dummy_message(chat_id, 1, str(method.text or ""))

    async def __call__(
        self,
        make_request: NextRequestMiddlewareType[TelegramType],
        bot: Bot,
        method: TelegramMethod[TelegramType],
    ) -> Response[TelegramType]:

        # ۱. مدیریت SendMessage با تایمر دیلی (Pacer) و محافظت در برابر خطاهای پیام‌های موقت
        if isinstance(method, SendMessage) and method.chat_id:
            txt = str(getattr(method, "text", "") or "")
            chat_id = method.chat_id
            # اگر پیام موقت '⏳ در حال بررسی...' است و چت در وضعیت محدودیت است، اصلاً آن را نفرست تا سهمیه برای منوی اصلی بماند
            if self._is_transient_loading_text(txt) and getattr(method, "reply_markup", None) is None:
                now = time.monotonic()
                if now < self.edit_ban_until.get(chat_id, 0.0) or now < self.send_ban_until.get(chat_id, 0.0):
                    return self._build_dummy_message(chat_id, 0, txt)  # type: ignore

            return await self._paced_send_message(make_request, bot, method)

        # ۲. مدیریت EditMessageText (حفظ کامل ادیت برای چک‌لیست‌ها و منوها + ترکیب هوشمند با SendMessage)
        if isinstance(method, EditMessageText) and method.chat_id and method.message_id is not None:
            now = time.monotonic()
            chat_id = method.chat_id
            msg_id = int(method.message_id)
            new_text = str(getattr(method, "text", "") or "")
            has_kb = getattr(method, "reply_markup", None) is not None

            # اگر پیام قبلی یک پیام موقت حذف‌شده/فرضی (message_id <= 1) بوده است:
            if msg_id <= 1:
                if self._is_transient_loading_text(new_text) and not has_kb:
                    return self._build_dummy_message(chat_id, msg_id, new_text)  # type: ignore
                send_req = SendMessage(
                    chat_id=chat_id,
                    text=new_text,
                    parse_mode=getattr(method, "parse_mode", None),
                    entities=getattr(method, "entities", None),
                    disable_web_page_preview=getattr(method, "disable_web_page_preview", None),
                    reply_markup=getattr(method, "reply_markup", None),
                )
                return await self._paced_send_message(make_request, bot, send_req)

            msg_key = (chat_id, msg_id)
            last_ts, last_txt = self.msg_edit_history.get(msg_key, (0.0, ""))

            # جلوگیری از ارسال ادیت تکراری وقتی متن تغییری نکرده است
            if new_text == last_txt and not has_kb:
                return self._build_dummy_message(chat_id, msg_id, new_text)  # type: ignore

            is_bg_status = self._is_background_status_text(new_text)
            is_transient = self._is_transient_loading_text(new_text) and not has_kb
            edit_ban = self.edit_ban_until.get(chat_id, 0.0)

            # اگر یک پیام موقت بدون دکمه است ('⏳ در حال استخراج زیرنویس انگلیسی...'):
            # در صورتی که به تازگی این پیام ادیت شده یا محدودیت داریم، بدون تماس با تلگرام Dummy برگردان تا استخراج زیرنویس بلافاصله اجرا شود!
            if is_transient and (now < edit_ban or (now - last_ts) < 4.0):
                return self._build_dummy_message(chat_id, msg_id, new_text)  # type: ignore

            # اگر آپدیت پس‌زمینه اتوماسیون/صف است:
            if is_bg_status:
                if now < edit_ban or (now - last_ts) < self.bg_edit_interval:
                    return self._build_dummy_message(chat_id, msg_id, new_text)  # type: ignore

                # هر ۱۲ بار ادیت روی یک پیامِ وضعیت طولانی، ۱ بار به جای ادیت یک پیام جدید بفرست تا محدودیت Edit روی یک پیام پر نشود
                cnt = self.msg_edit_counts.get(msg_key, 0) + 1
                self.msg_edit_counts[msg_key] = cnt
                if cnt >= 12 and now >= self.send_ban_until.get(chat_id, 0.0):
                    self.msg_edit_counts[msg_key] = 0
                    send_req = SendMessage(
                        chat_id=chat_id,
                        text=new_text,
                        parse_mode=getattr(method, "parse_mode", None),
                        disable_web_page_preview=getattr(method, "disable_web_page_preview", None),
                        reply_markup=getattr(method, "reply_markup", None),
                    )
                    return await self._paced_send_message(make_request, bot, send_req)

            # اگر الان EditMessageText در تلگرام مسدود است ولی کاربر روی منو/چک‌لیست کلیک کرده است:
            if now < edit_ban:
                if now >= self.send_ban_until.get(chat_id, 0.0):
                    # بدون پاک کردن پیام قبلی، منو را با تایمر دیلی در پیام جدید باز کن
                    send_req = SendMessage(
                        chat_id=chat_id,
                        text=new_text,
                        parse_mode=getattr(method, "parse_mode", None),
                        entities=getattr(method, "entities", None),
                        disable_web_page_preview=getattr(method, "disable_web_page_preview", None),
                        reply_markup=getattr(method, "reply_markup", None),
                    )
                    return await self._paced_send_message(make_request, bot, send_req)
                return self._build_dummy_message(chat_id, msg_id, new_text)  # type: ignore

            try:
                resp = await make_request(bot, method)
                self.msg_edit_history[msg_key] = (time.monotonic(), new_text)
                return resp

            except TelegramRetryAfter as e:
                wait_s = float(getattr(e, "retry_after", 15))
                self.edit_ban_until[chat_id] = time.monotonic() + wait_s
                logging.warning(f"🛡️ [FloodGuard] EditMessageText rate-limited for {wait_s:.0f}s in chat {chat_id}.")

                if is_bg_status or is_transient:
                    return self._build_dummy_message(chat_id, msg_id, new_text)  # type: ignore

                # اگر منوی تعاملی کاربر است و SendMessage باز است، با SendMessage بفرست (بدون حذف پیام قبلی)
                if time.monotonic() >= self.send_ban_until.get(chat_id, 0.0):
                    send_req = SendMessage(
                        chat_id=chat_id,
                        text=new_text,
                        parse_mode=getattr(method, "parse_mode", None),
                        entities=getattr(method, "entities", None),
                        disable_web_page_preview=getattr(method, "disable_web_page_preview", None),
                        reply_markup=getattr(method, "reply_markup", None),
                    )
                    return await self._paced_send_message(make_request, bot, send_req)

                return self._build_dummy_message(chat_id, msg_id, new_text)  # type: ignore

            except TelegramBadRequest as e:
                err_low = str(e).lower()
                if "message is not modified" in err_low:
                    self.msg_edit_history[msg_key] = (now, new_text)
                    return self._build_dummy_message(chat_id, msg_id, new_text)  # type: ignore
                if "message to edit not found" in err_low or "message can't be edited" in err_low:
                    if not is_bg_status and not is_transient:
                        send_req = SendMessage(
                            chat_id=chat_id,
                            text=new_text,
                            parse_mode=getattr(method, "parse_mode", None),
                            disable_web_page_preview=getattr(method, "disable_web_page_preview", None),
                            reply_markup=getattr(method, "reply_markup", None),
                        )
                        return await self._paced_send_message(make_request, bot, send_req)
                    return self._build_dummy_message(chat_id, msg_id, new_text)  # type: ignore
                raise

        if isinstance(method, (AnswerCallbackQuery, EditMessageReplyMarkup)):
            try:
                return await make_request(bot, method)
            except Exception:
                return True  # type: ignore

        return await make_request(bot, method)


class ChatRegistrationMiddleware(BaseMiddleware):
    async def __call__(self, handler, event: TelegramObject, data: dict):
        chat_id = None
        if isinstance(event, Message) and event.chat:
            chat_id = event.chat.id
        elif isinstance(event, CallbackQuery) and event.message and event.message.chat:
            chat_id = event.message.chat.id

        if chat_id:
            queue_mod.queue_engine.register_chat(chat_id)

        try:
            return await handler(event, data)
        except TelegramRetryAfter as e:
            logging.warning(f"⚠️ Uncaught TelegramRetryAfter suppressed ({e.retry_after}s).")
            return None


def setup_routers(dp: Dispatcher):
    dp.sub_routers.clear()
    dp.include_router(handlers_mod.router)
    dp.include_router(subtitle_handlers_mod.router)
    dp.include_router(download_handlers_mod.router)
    dp.include_router(queue_handlers_mod.router)
    dp.include_router(settings_handlers_mod.router)
    if HAS_PROWLARR and prowlarr_handlers_mod:
        dp.include_router(prowlarr_handlers_mod.router)


async def hot_reload_all(dp: Dispatcher, bot: Bot):
    logging.info("🔄 در حال اجرای Hot-Reload روی تمامی ماژول‌ها...")
    cfg.reload()

    modules_to_reload = [
        proxy_mod,
        process_utils_mod,
        subtitle_service_mod,
        mkv_service_mod,
        handbrake_service_mod,
        downloader_service_mod,
        cuteftp_worker_mod,
        cuteftp_service_mod,
        scraper_service_mod,
        monitor_service_mod,
        automation_service_mod,
        keyboards_mod,
        handlers_mod,
        subtitle_handlers_mod,
        download_handlers_mod,
        queue_handlers_mod,
        settings_handlers_mod,
    ]
    if HAS_PROWLARR and prowlarr_service_mod and prowlarr_handlers_mod:
        modules_to_reload.extend([prowlarr_service_mod, prowlarr_handlers_mod])

    for mod in modules_to_reload:
        try:
            importlib.reload(mod)
        except Exception as e:
            logging.error(f"❌ خطا در ریلود ماژول {mod.__name__}: {e}")

    for h_mod in (handlers_mod, subtitle_handlers_mod, download_handlers_mod, queue_handlers_mod, settings_handlers_mod):
        h_mod.router._parent_router = None
    if HAS_PROWLARR and prowlarr_handlers_mod:
        prowlarr_handlers_mod.router._parent_router = None

    setup_routers(dp)
    queue_mod.queue_engine.set_bot(bot)
    queue_mod.queue_engine.restart_workers_for_hot_reload()
    if HAS_PROWLARR and prowlarr_service_mod:
        prowlarr_service_mod.ProwlarrService.start_background_monitor(bot)

    logging.info("✅ تمامی کدها و تنظیمات بدون توقف ربات با موفقیت Reload شدند!")


async def console_key_listener(dp: Dispatcher, bot: Bot):
    if sys.platform != "win32":
        return
    import msvcrt
    logging.info("⌨️ کلید میانبر کنسول فعال است: هر زمان کلید [R] را بزنید، کل پروژه Hot-Reload می‌شود.")
    while True:
        if msvcrt.kbhit():
            ch = msvcrt.getwch()
            if ch.lower() == "r":
                await hot_reload_all(dp, bot)
        await asyncio.sleep(0.25)


async def main():
    cfg.reload()
    automation_service_mod.AutomationService.ensure_workspace_root_categories()

    token = cfg.get("telegram", "bot_token")
    session = _resolve_bot_session()
    bot = Bot(token=token, session=session)

    bot.session.middleware(FloodSafeSessionMiddleware())

    dp = Dispatcher()
    dp.message.outer_middleware(ChatRegistrationMiddleware())
    dp.callback_query.outer_middleware(ChatRegistrationMiddleware())

    setup_routers(dp)
    queue_mod.queue_engine.set_bot(bot)
    queue_mod.queue_engine.start_workers()

    if HAS_PROWLARR and prowlarr_service_mod:
        prowlarr_service_mod.ProwlarrService.start_background_monitor(bot)

    asyncio.create_task(console_key_listener(dp, bot))

    logging.info("🤖 MediaBot آماده به کار است...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())