import re
from aiogram import Router, F
from aiogram.types import CallbackQuery
from core.queue_manager import queue_engine, QueueTask
from bot.keyboards import queue_management_kb, main_menu_kb

router = Router()

QUEUE_ICONS = {
    "handbrake": "🔥 HandBrake",
    "mkvtoolnix": "🎬 MKVToolNix",
    "download": "📥 Download",
    "upload": "☁️ CuteFTP",
}


def _make_visual_progress_bar(progress_text: str) -> str:
    """ساخت نوار پیشرفت گرافیکی ۱۰ خانه‌ای از روی درصد پیشرفت"""
    match = re.search(r"(\d+(?:\.\d+)?)\s*%", str(progress_text))
    if not match:
        return f"⚙️ `{progress_text}`"

    pct = min(100.0, max(0.0, float(match.group(1))))
    filled = int(round(pct / 10.0))
    bar = "█" * filled + "░" * (10 - filled)
    extra = str(progress_text).replace(match.group(0), "").strip(" |-•,")
    if extra:
        return f"`[{bar}] {pct:.1f}%`\n      ↳ _{extra[:65]}_"
    return f"`[{bar}] {pct:.1f}%`"


def _format_task_card(t: QueueTask) -> str:
    q_label = QUEUE_ICONS.get(t.queue_type, t.queue_type.upper())
    header = f"┏ 🆔 `{t.task_id}` ▏ **{q_label}** ▏ 🕒 `{t.created_at}`"
    title_line = f"┃ 📌 **{t.title}**"
    status_line = f"┃ 📊 وضعیت: {t.status}"
    prog_line = f"┗ 📈 پیشرفت: {_make_visual_progress_bar(t.progress)}"

    card = f"{header}\n{title_line}\n{status_line}\n{prog_line}"
    if t.error:
        card += f"\n   ⚠️ **خطا:** `{t.error[:100]}`"
    if t.result and isinstance(t.result, list):
        links_str = "\n".join([f"   🔗 {u}" for u in t.result[:5]])
        card += f"\n{links_str}"
    return card


async def _render_queue_message(call: CallbackQuery):
    tasks = queue_engine.get_queue_summary()
    if not tasks:
        try:
            await call.message.edit_text(
                "📭 **در حال حاضر هیچ پراسسی در صف وجود ندارد.**",
                reply_markup=main_menu_kb(),
                parse_mode="Markdown"
            )
        except Exception:
            await call.answer("📭 صف خالی است.", show_alert=True)
        return

    running_count = sum(1 for t in tasks if "اجرا" in t.status)
    pending_count = sum(1 for t in tasks if "انتظار" in t.status)
    done_count = sum(1 for t in tasks if "تکمیل" in t.status)
    err_count = sum(1 for t in tasks if any(k in t.status for k in ("خطا", "لغو")))

    summary_banner = (
        "📊 **مانیتورینگ لحظه‌ای صف و پراسس‌ها**\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"⚙️ در حال اجرا: `{running_count}`  |  ⏳ در انتظار: `{pending_count}`\n"
        f"✅ تکمیل‌شده: `{done_count}`  |  ❌ خطا/لغو: `{err_count}`\n"
        "━━━━━━━━━━━━━━━━━━━━"
    )

    cards = [_format_task_card(t) for t in reversed(tasks)]
    full_text = summary_banner + "\n\n" + "\n\n".join(cards)

    try:
        await call.message.edit_text(
            full_text[:4000],
            reply_markup=queue_management_kb(tasks),
            parse_mode="Markdown"
        )
    except Exception:
        await call.answer("بروزرسانی شد.")


@router.callback_query(F.data == "menu:queues")
async def cb_show_queues(call: CallbackQuery):
    await _render_queue_message(call)


@router.callback_query(F.data == "qact:clear_finished")
async def cb_clear_finished_tasks(call: CallbackQuery):
    removed_count = queue_engine.clear_finished_tasks()
    await call.answer(f"🧹 تعداد {removed_count} پراسس تمام‌شده از لیست پاکسازی شد!", show_alert=True)
    await _render_queue_message(call)


@router.callback_query(F.data.startswith("qdel:"))
async def cb_delete_single_task(call: CallbackQuery):
    task_id = call.data.split(":")[1]
    queue_engine.remove_task(task_id)
    await call.answer(f"🗑 پراسس #{task_id} حذف شد.")
    await _render_queue_message(call)


@router.callback_query(F.data.startswith("qcancel:"))
async def cb_cancel_and_remove_task(call: CallbackQuery):
    task_id = call.data.split(":")[1]
    queue_engine.cancel_task(task_id)
    queue_engine.remove_task(task_id)
    await call.answer(f"🛑 پراسس #{task_id} متوقف و حذف شد.", show_alert=True)
    await _render_queue_message(call)