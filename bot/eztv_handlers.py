import html
import logging

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from core.config_loader import cfg
from services.eztv_tracker_service import eztv_tracker_service

router = Router()


def _is_admin(message: Message) -> bool:
    if not message.from_user:
        return False
    return message.from_user.id in (cfg.get("telegram", "admin_ids", []) or [])


@router.message(Command("eztv_status"))
async def cmd_eztv_status(message: Message):
    if not _is_admin(message):
        return

    config = eztv_tracker_service.get_config()
    shows = eztv_tracker_service.get_shows()
    if not shows:
        return await message.answer(
            "📡 ردیاب EZTV فعال است، اما فهرست سریال‌ها خالی است.\n"
            "عنوان، فصل، قسمت بعدی و کیفیت‌ها را در بخش <code>eztv_tracker.shows</code> فایل config.yaml وارد کنید.",
            parse_mode="HTML",
        )

    state = eztv_tracker_service._load_state()
    lines = [
        "📡 <b>وضعیت ردیاب EZTV</b>",
        f"وضعیت سرویس: {'✅ فعال' if config.get('enabled', True) else '⏸ غیرفعال'}",
        f"RSS: <code>{html.escape(str(config.get('feed_url', 'https://myrss.org/eztv')))}</code>",
        f"فاصله بررسی: <code>{int(config.get('poll_interval_seconds', 300) or 300)}</code> ثانیه",
        f"گزارش روزانه: <code>{html.escape(str(config.get('daily_report_time', '09:00')))}</code>",
        "",
    ]
    for show in shows:
        episode = eztv_tracker_service._episode_progress(show, state)
        tag = eztv_tracker_service._episode_tag(show["season"], episode)
        qualities = "، ".join(show["qualities"])
        lines.append(
            f"• <b>{html.escape(show['title'])}</b> "
            f"<code>{tag}</code> — {html.escape(qualities)}"
        )
    await message.answer("\n".join(lines), parse_mode="HTML")


@router.message(Command("eztv_check"))
async def cmd_eztv_check(message: Message):
    if not _is_admin(message):
        return
    wait = await message.answer("🔎 در حال بررسی فوری RSS سایت EZTV و قسمت‌های مورد انتظار...")
    try:
        result = await eztv_tracker_service.poll_once(message.bot, send_summary=True)
        queued = result.get("queued", [])
        await wait.edit_text(
            "✅ بررسی فوری تمام شد.\n"
            f"سریال‌های تنظیم‌شده: <code>{result.get('shows', 0)}</code>\n"
            f"دانلودهای جدید به صف: <code>{len(queued)}</code>\n"
            f"مواردی که از قبل موجود بودند: <code>{len(result.get('already_local', []))}</code>",
            parse_mode="HTML",
        )
    except Exception as exc:
        logging.exception("Manual EZTV check failed")
        await wait.edit_text(
            "❌ بررسی EZTV با خطا مواجه شد:\n<code>"
            + html.escape(str(exc)[:1000])
            + "</code>",
            parse_mode="HTML",
        )


@router.message(Command("eztv_report"))
async def cmd_eztv_report(message: Message):
    if not _is_admin(message):
        return
    wait = await message.answer("📊 در حال تهیه گزارش روزانه...")
    try:
        report = await eztv_tracker_service.build_daily_report()
        chunks = [report[i:i + 3800] for i in range(0, len(report), 3800)]
        if not chunks:
            chunks = ["گزارشی برای نمایش وجود ندارد."]
        await wait.edit_text(chunks[0], parse_mode="HTML")
        for chunk in chunks[1:]:
            await message.answer(chunk, parse_mode="HTML")
    except Exception as exc:
        logging.exception("Manual EZTV report failed")
        await wait.edit_text(
            "❌ تهیه گزارش با خطا مواجه شد:\n<code>"
            + html.escape(str(exc)[:1000])
            + "</code>",
            parse_mode="HTML",
        )
