import math
import re
from aiogram import Router, F
from aiogram.types import CallbackQuery
from aiogram.exceptions import TelegramBadRequest
from core.queue_manager import queue_engine, QueueTask
from bot.keyboards import queue_management_kb, main_menu_kb

router = Router()

PAGE_SIZE = 6  # تعداد پراسس‌های نمایش داده شده در هر صفحه

QUEUE_ICONS = {
    "handbrake": "🔥 HandBrake",
    "mkvtoolnix": "🎬 MKVToolNix",
    "download": "📥 Download",
    "upload": "☁️ CuteFTP",
}


def _clean_code_text(text: str) -> str:
    """حذف کاراکترهای مخرب داخل بلوک کد Markdown"""
    return str(text or "").replace("`", "'").replace("\n", " ").strip()


def _make_visual_progress_bar(progress_text: str) -> str:
    """ساخت نوار پیشرفت گرافیکی ۱۰ خانه‌ای بدون شکستن فرمت Markdown"""
    raw_prog = _clean_code_text(progress_text)
    match = re.search(r"(\d+(?:\.\d+)?)\s*%", raw_prog)
    if not match:
        return f"⚙️ `{raw_prog[:70]}`"

    pct = min(100.0, max(0.0, float(match.group(1))))
    filled = int(round(pct / 10.0))
    bar = "█" * filled + "░" * (10 - filled)
    extra = raw_prog.replace(match.group(0), "").strip(" |-•,")
    if extra:
        return f"`[{bar}] {pct:.1f}%`\n┃ ↳ `{extra[:60]}`"
    return f"`[{bar}] {pct:.1f}%`"


def _format_task_card(t: QueueTask) -> str:
    q_label = QUEUE_ICONS.get(t.queue_type, t.queue_type.upper())
    safe_title = _clean_code_text(t.title)[:65]
    safe_status = _clean_code_text(t.status)

    header = f"┏ 🆔 `{t.task_id}` ▏ *{q_label}* ▏ 🕒 `{t.created_at}`"
    title_line = f"┃ 📌 `{safe_title}`"
    status_line = f"┃ 📊 وضعیت: `{safe_status}`"
    prog_line = f"┗ 📈 پیشرفت: {_make_visual_progress_bar(t.progress)}"

    card = f"{header}\n{title_line}\n{status_line}\n{prog_line}"
    if t.error:
        safe_err = _clean_code_text(t.error)[:120]
        card += f"\n   ⚠️ *خطا:* `{safe_err}`"
    if t.result and isinstance(t.result, list) and len(t.result) > 0:
        links_count = len(t.result)
        first_link = _clean_code_text(t.result[0])
        card += f"\n   🔗 لینک ({links_count} مورد): `{first_link}`"
    return card


def _sort_tasks_by_priority(tasks: list[QueueTask]) -> list[QueueTask]:
    """
    مرتب‌سازی هوشمند:
    ۱. پراسس‌های در حال اجرا (بالاترین اولویت در صفحه ۱)
    ۲. پراسس‌های در انتظار (به ترتیب نوبت اجرا)
    ۳. پراسس‌های تمام‌شده یا خطادار (جدیدترین‌ها اول)
    """
    running = []
    pending = []
    finished = []

    for t in tasks:
        st = str(t.status)
        if "اجرا" in st:
            running.append(t)
        elif "انتظار" in st:
            pending.append(t)
        else:
            finished.append(t)

    return running + pending + list(reversed(finished))


def _extract_page_from_call(data: str) -> int:
    parts = data.split(":")
    for p in reversed(parts):
        if p.isdigit():
            return int(p)
    return 0


async def _render_queue_message(call: CallbackQuery, page: int = 0):
    all_tasks = queue_engine.get_queue_summary()
    if not all_tasks:
        try:
            await call.message.edit_text(
                "📭 *در حال حاضر هیچ پراسسی در صف وجود ندارد.*",
                reply_markup=main_menu_kb(),
                parse_mode="Markdown"
            )
        except TelegramBadRequest as e:
            if "message is not modified" in str(e).lower():
                await call.answer("📭 صف خالی است.")
            else:
                await call.message.answer("📭 در حال حاضر هیچ پراسسی در صف وجود ندارد.", reply_markup=main_menu_kb())
        return

    running_count = sum(1 for t in all_tasks if "اجرا" in str(t.status))
    pending_count = sum(1 for t in all_tasks if "انتظار" in str(t.status))
    done_count = sum(1 for t in all_tasks if "تکمیل" in str(t.status))
    err_count = sum(1 for t in all_tasks if any(k in str(t.status) for k in ("خطا", "لغو")))

    sorted_tasks = _sort_tasks_by_priority(all_tasks)
    total_tasks = len(sorted_tasks)
    total_pages = max(1, math.ceil(total_tasks / PAGE_SIZE))

    # تنظیم محدوده مجاز شماره صفحه
    page = max(0, min(page, total_pages - 1))
    start_idx = page * PAGE_SIZE
    end_idx = start_idx + PAGE_SIZE
    page_tasks = sorted_tasks[start_idx:end_idx]

    summary_banner = (
        f"📊 *مانیتورینگ صف و پراسس‌ها (صفحه {page + 1} از {total_pages})*\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"⚙️ در حال اجرا: `{running_count}`  ▏  ⏳ در انتظار: `{pending_count}`\n"
        f"✅ تکمیل‌شده: `{done_count}`  ▏  ❌ خطا/لغو: `{err_count}`\n"
        f"📦 کل پراسس‌ها: `{total_tasks}` مورد\n"
        "━━━━━━━━━━━━━━━━━━━━"
    )

    cards = [_format_task_card(t) for t in page_tasks]
    full_text = summary_banner + "\n\n" + "\n\n".join(cards)
    kb = queue_management_kb(page_tasks, page=page, total_pages=total_pages)

    try:
        await call.message.edit_text(
            full_text,
            reply_markup=kb,
            parse_mode="Markdown"
        )
        await call.answer()
    except TelegramBadRequest as e:
        err_msg = str(e).lower()
        if "message is not modified" in err_msg:
            await call.answer("✅ وضعیت صف بروز است.")
        else:
            # در صورت هرگونه خطای احتمالی دیگر، بدون فرمت Markdown نمایش بده تا هرگز صفحه قفل نشود
            plain_text = full_text.replace("*", "").replace("`", "")
            await call.message.edit_text(plain_text[:4000], reply_markup=kb)
            await call.answer()


@router.callback_query(F.data == "menu:queues")
async def cb_show_queues(call: CallbackQuery):
    await _render_queue_message(call, page=0)


@router.callback_query(F.data.startswith("qpage:"))
async def cb_change_queue_page(call: CallbackQuery):
    page = int(call.data.split(":")[1])
    await _render_queue_message(call, page=page)


@router.callback_query(F.data == "qnoop")
async def cb_queue_noop(call: CallbackQuery):
    await call.answer()


@router.callback_query(F.data.startswith("qact:clear_finished"))
async def cb_clear_finished_tasks(call: CallbackQuery):
    page = _extract_page_from_call(call.data)
    removed_count = queue_engine.clear_finished_tasks()
    await call.answer(f"🧹 تعداد {removed_count} پراسس تمام‌شده پاکسازی شد!", show_alert=True)
    await _render_queue_message(call, page=page)


@router.callback_query(F.data.startswith("qdel:"))
async def cb_delete_single_task(call: CallbackQuery):
    parts = call.data.split(":")
    task_id = parts[1]
    page = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
    queue_engine.remove_task(task_id)
    await call.answer(f"🗑 پراسس #{task_id} حذف شد.")
    await _render_queue_message(call, page=page)


@router.callback_query(F.data.startswith("qcancel:"))
async def cb_cancel_and_remove_task(call: CallbackQuery):
    parts = call.data.split(":")
    task_id = parts[1]
    page = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
    queue_engine.cancel_task(task_id)
    queue_engine.remove_task(task_id)
    await call.answer(f"🛑 پراسس #{task_id} متوقف و حذف شد.", show_alert=True)
    await _render_queue_message(call, page=page)