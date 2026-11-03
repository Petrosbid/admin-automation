from aiogram import Router, F
from aiogram.types import CallbackQuery
from core.queue_manager import queue_engine
from bot.keyboards import queue_management_kb, main_menu_kb

router = Router()


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

    lines = ["📊 **وضعیت صف‌های پردازش (۲ ورکر موازی HandBrake / MKV / Download / CuteFTP):**\n"]
    for t in reversed(tasks):
        line = f"🔹 `{t.task_id}` | **[{t.queue_type.upper()}]** {t.title}\n   └ وضعیت: {t.status} ({t.progress})"
        if t.error:
            line += f"\n   └ ⚠️ خطا: `{t.error[:90]}`"
        if t.result and isinstance(t.result, list):
            links_str = "\n".join([f"   🔗 {u}" for u in t.result[:6]])
            line += f"\n{links_str}"
        lines.append(line)

    try:
        await call.message.edit_text(
            "\n\n".join(lines),
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
    await call.answer(f"🧹 تعداد {removed_count} پراسس تمام‌شده از لیست حذف شد!", show_alert=True)
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