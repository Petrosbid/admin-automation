from aiogram import Router, F
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from core.config_loader import cfg

from core.queue_manager import (
    queue_engine,
    STATUS_QUEUED,
    STATUS_RUNNING,
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_CANCELLED
)


router = Router(name="queue_management")


def is_admin(user_id: int) -> bool:

    return user_id in cfg.get(
        "telegram",
        "admin_ids",
        []
    )


def escape_html(value) -> str:

    return (
        str(value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def build_dashboard():

    stats = queue_engine.get_statistics()

    workers = queue_engine.get_worker_counts()

    tasks = queue_engine.get_queue_summary(
        limit=20
    )

    lines = [
        "📊 <b>مدیریت صف پردازش‌ها</b>",
        "",
        f"📦 کل پردازش‌ها: <b>{stats['total']}</b>",
        "",
        "📈 <b>وضعیت پردازش‌ها</b>",
        f"⏳ در انتظار: <b>{stats[STATUS_QUEUED]}</b>",
        f"⚙️ در حال اجرا: <b>{stats[STATUS_RUNNING]}</b>",
        f"✅ تکمیل شده: <b>{stats[STATUS_COMPLETED]}</b>",
        f"❌ ناموفق: <b>{stats[STATUS_FAILED]}</b>",
        f"🚫 لغو شده: <b>{stats[STATUS_CANCELLED]}</b>",
        "",
        "━━━━━━━━━━━━━━━━━━",
        "⚙️ <b>ظرفیت پردازش هم‌زمان</b>",
        f"📥 دانلود: {workers.get('download', 0)}",
        f"📎 SoftSub: {workers.get('mkvtoolnix', 0)}",
        f"🔥 HardSub: {workers.get('handbrake', 0)}",
        f"☁️ آپلود: {workers.get('upload', 0)}",
        "",
        "━━━━━━━━━━━━━━━━━━",
        "📋 <b>آخرین پردازش‌ها</b>",
        ""
    ]

    builder = InlineKeyboardBuilder()

    if not tasks:

        lines.append(
            "📭 هنوز هیچ پردازشی ثبت نشده است."
        )

    for task in reversed(tasks):

        lines.append(
            f"{task.queue_label} <b>{escape_html(task.title)}</b>"
        )

        lines.append(
            f"🆔 <code>{task.task_id}</code>"
        )

        lines.append(
            f"📊 {task.status_label}"
        )

        lines.append(
            f"📈 {escape_html(task.progress)}"
        )

        lines.append(
            f"⏱ مدت اجرا: {task.duration}"
        )

        if task.worker_id:

            lines.append(
                f"👷 Worker شماره {task.worker_id}"
            )

        if task.error:

            lines.append(
                "⚠️ خطا:\n"
                f"<code>{escape_html(task.error[:300])}</code>"
            )

        if isinstance(task.result, list):

            for link in task.result[:5]:

                lines.append(
                    f"🔗 {escape_html(link)}"
                )

        if task.status in (
            STATUS_QUEUED,
            STATUS_RUNNING
        ):

            builder.row(
                InlineKeyboardButton(
                    text=f"🚫 لغو {task.task_id}",
                    callback_data=f"qcancel:{task.task_id}"
                )
            )

        lines.append(
            "━━━━━━━━━━━━━━━━━━"
        )

    builder.row(
        InlineKeyboardButton(
            text="🔄 بروزرسانی",
            callback_data="menu:queues"
        )
    )

    builder.row(
        InlineKeyboardButton(
            text="🔙 منوی اصلی",
            callback_data="menu:home"
        )
    )

    return (
        "\n".join(lines),
        builder.as_markup()
    )


@router.callback_query(F.data == "menu:queues")
async def show_queues(call: CallbackQuery):

    if not is_admin(call.from_user.id):

        return await call.answer(
            "دسترسی غیرمجاز",
            show_alert=True
        )

    text, keyboard = build_dashboard()

    await call.answer()

    try:

        await call.message.edit_text(
            text,
            reply_markup=keyboard,
            parse_mode="HTML"
        )

    except Exception as exc:

        if "message is not modified" not in str(exc).lower():

            raise


@router.callback_query(F.data.startswith("qcancel:"))
async def cancel_task(call: CallbackQuery):

    if not is_admin(call.from_user.id):

        return await call.answer(
            "دسترسی غیرمجاز",
            show_alert=True
        )

    task_id = call.data.split(
        ":",
        1
    )[1]

    task = queue_engine.get_task(task_id)

    if not task:

        return await call.answer(
            "پردازش پیدا نشد.",
            show_alert=True
        )

    if task.is_terminal:

        return await call.answer(
            "این پردازش قبلاً به پایان رسیده است.",
            show_alert=True
        )

    success = await queue_engine.cancel_task(
        task_id
    )

    if not success:

        return await call.answer(
            "امکان لغو پردازش وجود ندارد.",
            show_alert=True
        )

    await call.answer(
        "درخواست لغو ثبت شد.",
        show_alert=True
    )

    text, keyboard = build_dashboard()

    try:

        await call.message.edit_text(
            text,
            reply_markup=keyboard,
            parse_mode="HTML"
        )

    except Exception as exc:

        if "message is not modified" not in str(exc).lower():

            raise