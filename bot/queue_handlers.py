import html
import math
import re
from pathlib import Path
from urllib.parse import unquote, urlparse
from aiogram import Router, F
from aiogram.types import CallbackQuery
from aiogram.exceptions import TelegramBadRequest
from core.queue_manager import queue_engine, QueueTask
from bot.keyboards import queue_management_kb, main_menu_kb

router = Router()

PAGE_SIZE = 5  # نمایش ۵ کارت پراسس با جزئیات کامل در هر صفحه

QUEUE_HEADERS = {
    "download": "📥 DOWNLOAD (دانلودر)",
    "mkvtoolnix": "🎬 SOFTSUB (سافت‌ساب)",
    "handbrake": "🔥 HARDSUB / ENCODE (هندبریک)",
    "upload": "☁️ FTP UPLOAD (آپلود به سرور)",
}


def _make_modern_progress_bar(pct: float, width: int = 12) -> str:
    pct = min(100.0, max(0.0, pct))
    filled = int(round((pct / 100.0) * width))
    bar = "▰" * filled + "▱" * (width - filled)
    return f"[{bar}] {pct:.1f}%"


def _extract_task_paths_info(t: QueueTask) -> dict[str, str]:
    """
    استخراج هوشمند نام فایل، محل دانلود/ذخیره و مسیر مقصد از روی آرگومان‌های واقعی تسک
    """
    info = {
        "file_name": "",
        "dest_dir": "",
        "source_name": "",
    }
    args = t.args or ()

    if t.queue_type == "download":
        # در دانلودها آرگومان‌ها یا (target_link, dest_dir) هستند یا (bot, chat_id, target_link, dest_dir)
        link_str = ""
        dest_p = None
        for a in args:
            if isinstance(a, Path):
                dest_p = a
            elif isinstance(a, str) and (a.startswith(("http", "magnet:")) or a.endswith(".torrent")):
                link_str = a

        if dest_p:
            info["dest_dir"] = f"{dest_p.parent.name}/{dest_p.name}" if dest_p.parent else dest_p.name
            # اگر در حین دانلود فایلی در پوشه ساخته شده، نام واقعی آن را بردار
            if dest_p.exists() and dest_p.is_dir():
                vids = sorted(
                    [f for f in dest_p.iterdir() if f.is_file() and not f.name.startswith("_")],
                    key=lambda x: x.stat().st_mtime,
                    reverse=True
                )
                if vids:
                    info["file_name"] = vids[0].name.replace(".aria2", "")

        if not info["file_name"] and link_str:
            if link_str.startswith("magnet:"):
                m_dn = re.search(r"dn=([^&]+)", link_str)
                info["file_name"] = unquote(m_dn.group(1)) if m_dn else "Magnet Torrent"
            else:
                parsed_path = unquote(urlparse(link_str).path)
                info["file_name"] = Path(parsed_path).name or link_str[:40]

    elif t.queue_type == "mkvtoolnix":
        # آرگومان‌ها: (raw_video, output_file, ...)
        if len(args) >= 2 and isinstance(args[0], Path) and isinstance(args[1], Path):
            info["source_name"] = args[0].name
            info["file_name"] = args[1].name
            info["dest_dir"] = f"{args[1].parent.parent.name}/{args[1].parent.name}"

    elif t.queue_type == "handbrake":
        # یا آرگومان اول HardSubTarget است (در اتوماسیون/انکود) یا (video_path, primary_sub, output_media_path, ...)
        if len(args) >= 1 and hasattr(args[0], "output_file"):
            ht = args[0]
            info["source_name"] = ht.source_video.name
            info["file_name"] = ht.output_file.name
            info["dest_dir"] = f"{ht.output_dir.parent.name}/{ht.output_dir.name}"
        elif len(args) >= 3 and isinstance(args[0], Path) and isinstance(args[2], Path):
            info["source_name"] = args[0].name
            info["file_name"] = args[2].name
            info["dest_dir"] = f"{args[2].parent.parent.name}/{args[2].parent.name}"

    elif t.queue_type == "upload":
        # آرگومان‌ها: (local_target, remote_dir)
        if len(args) >= 1:
            first = args[0]
            if isinstance(first, Path):
                info["file_name"] = first.name
            elif isinstance(first, list) and first:
                info["file_name"] = f"{len(first)} فایل ({Path(first[0]).name[:25]}...)"
        if len(args) >= 2 and isinstance(args[1], str):
            info["dest_dir"] = args[1]

    if not info["file_name"]:
        info["file_name"] = t.title
    return info


def _parse_metrics_by_type(queue_type: str, raw_prog: str) -> dict[str, str]:
    """
    پارس کردن هوشمند رشته پیشرفت (Progress) برای استخراج:
    درصد، مقدار دانلود/آپلود شده، سرعت (Speed / FPS) و زمان باقی‌مانده (ETA)
    """
    res = {
        "pct_float": 0.0,
        "has_pct": False,
        "transferred": "",
        "speed": "",
        "eta": "",
        "extra_note": "",
    }
    clean = str(raw_prog or "").strip()

    # ۱. استخراج درصد
    m_pct = re.search(r"(\d+(?:\.\d+)?)\s*%", clean)
    if m_pct:
        res["pct_float"] = float(m_pct.group(1))
        res["has_pct"] = True

    if queue_type == "download":
        # پشتیبانی از خروجی aria2c مثلاً: [#12ab 450MiB/1.2GiB(37%) CN:16 DL:24.5MiB ETA:32s]
        # و خروجی qBittorrent مثلاً: 45.2% | سرعت: 18.4 MB/s | دانلود شده: 512 / 1100 MB
        m_aria_size = re.search(r"(\d+(?:\.\d+)?(?:KiB|MiB|GiB|B|MB|GB)\s*/\s*\d+(?:\.\d+)?(?:KiB|MiB|GiB|B|MB|GB))", clean, re.I)
        m_paren_size = re.search(r"\(([^)]*(?:MB|GB|MiB|GiB)[^)]*)\)", clean, re.I)
        if m_aria_size:
            res["transferred"] = m_aria_size.group(1)
        elif m_paren_size:
            res["transferred"] = m_paren_size.group(1)

        m_dl_speed = re.search(r"(?:DL:|سرعت:\s*)(\d+(?:\.\d+)?\s*(?:KiB|MiB|GiB|KB|MB|GB)(?:/s)?)", clean, re.I)
        if not m_dl_speed:
            m_dl_speed = re.search(r"(\d+(?:\.\d+)?\s*(?:MB/s|KB/s|MiB/s|KiB/s))", clean, re.I)
        if m_dl_speed:
            sp = m_dl_speed.group(1)
            res["speed"] = sp if sp.lower().endswith("/s") else f"{sp}/s"

        m_eta = re.search(r"ETA[:\s]+([0-9hms:]+)", clean, re.I)
        if m_eta:
            res["eta"] = m_eta.group(1)

    elif queue_type == "upload":
        # فرمت ورکر آپلود: 45.2% (210.4/450.0 MB) | 38.5 MB/s | ETA: 00:08
        m_size = re.search(r"\(([\d.]+\s*/\s*[\d.]+\s*MB|[\d.]+\s*MB)\)", clean, re.I)
        if m_size:
            res["transferred"] = m_size.group(1)
        m_sp = re.search(r"([\d.]+\s*MB/s)", clean, re.I)
        if m_sp:
            res["speed"] = m_sp.group(1)
        m_eta = re.search(r"ETA:\s*([0-9:]+)", clean, re.I)
        if m_eta:
            res["eta"] = m_eta.group(1)

    elif queue_type == "handbrake":
        # فرمت خروجی HandBrakeCLI: task 1 of 1, 45.23 % (112.45 fps, avg 98.12 fps, ETA 00h04m12s)
        m_fps = re.search(r"(\d+(?:\.\d+)?\s*fps(?:,\s*avg\s*\d+(?:\.\d+)?\s*fps)?)", clean, re.I)
        if m_fps:
            res["speed"] = m_fps.group(1)
        m_eta = re.search(r"ETA\s+([0-9hms:]+)", clean, re.I)
        if m_eta:
            res["eta"] = m_eta.group(1)

    if not res["has_pct"]:
        res["extra_note"] = clean[:75]

    return res


def _format_task_card_html(t: QueueTask, pending_pos: tuple[int, int] = (0, 0)) -> str:
    """
    طراحی کارت گرافیکی و حرفه‌ای برای هر پراسس متناسب با نوع عملیات (HTML)
    """
    q_header = QUEUE_HEADERS.get(t.queue_type, t.queue_type.upper())
    p_info = _extract_task_paths_info(t)
    metrics = _parse_metrics_by_type(t.queue_type, t.progress)

    safe_file = html.escape(p_info["file_name"][:60])
    safe_dest = html.escape(p_info["dest_dir"][:55])
    safe_status = html.escape(str(t.status))
    elapsed = getattr(t, "elapsed_str", "00:00")

    pos_badge = ""
    if "انتظار" in str(t.status) and pending_pos[0] > 0:
        pos_badge = f" (نوبت {pending_pos[0]} از {pending_pos[1]})"

    lines = [
        f"┏ 🆔 <code>#{html.escape(t.task_id)}</code> ▏ <b>{q_header}</b>",
        f"┃ 📄 <b>فایل:</b> <code>{safe_file}</code>",
    ]

    if safe_dest:
        dest_label = "☁️ مقصد سرور" if t.queue_type == "upload" else ("📂 محل دانلود" if t.queue_type == "download" else "📂 مسیر خروجی")
        lines.append(f"┃ {dest_label}: <code>{safe_dest}</code>")

    lines.append(f"┃ 📊 <b>وضعیت:</b> <code>{safe_status}{pos_badge}</code>  ▏  ⏱ <code>{elapsed}</code>")

    # نمایش جزئیات تخصصی بر اساس نوع صف
    if metrics["has_pct"]:
        bar_str = _make_modern_progress_bar(metrics["pct_float"])
        lines.append(f"┃ 📈 <b>پیشرفت:</b> <code>{bar_str}</code>")

        detail_parts = []
        if metrics["transferred"]:
            lbl = "📦 دانلود شده" if t.queue_type == "download" else "📦 ارسال شده"
            detail_parts.append(f"{lbl}: <code>{html.escape(metrics['transferred'])}</code>")
        if metrics["speed"]:
            lbl = "🎞 سرعت رندر" if t.queue_type == "handbrake" else "⚡ سرعت"
            detail_parts.append(f"{lbl}: <code>{html.escape(metrics['speed'])}</code>")
        if metrics["eta"]:
            detail_parts.append(f"⏳ مانده: <code>{html.escape(metrics['eta'])}</code>")

        if detail_parts:
            lines.append("┗ " + "  ▏  ".join(detail_parts))
        else:
            lines[-1] = "┗" + lines[-1][1:]
    else:
        note = html.escape(metrics["extra_note"] or str(t.progress))
        lines.append(f"┗ ⚙️ <b>عملیات جاری:</b> <code>{note}</code>")

    if t.error:
        safe_err = html.escape(str(t.error)[:140])
        lines.append(f"   ⚠️ <b>خطا:</b> <code>{safe_err}</code>")

    if t.result and isinstance(t.result, list) and len(t.result) > 0:
        first_link = html.escape(str(t.result[0]))
        lines.append(f"   🔗 <b>لینک ({len(t.result)}):</b> <code>{first_link}</code>")

    return "\n".join(lines)


def _sort_tasks_by_priority(tasks: list[QueueTask]) -> list[QueueTask]:
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

    # مرتب‌سازی پراسس‌های در انتظار بر اساس ترتیب واقعی آن‌ها در pending_lists
    ordered_pending = []
    seen_p = set()
    for q_type in queue_engine.QUEUE_TYPES:
        for tid in queue_engine.pending_lists.get(q_type, []):
            t_obj = queue_engine.tasks.get(tid)
            if t_obj and "انتظار" in str(t_obj.status):
                ordered_pending.append(t_obj)
                seen_p.add(tid)
    for t_obj in pending:
        if t_obj.task_id not in seen_p:
            ordered_pending.append(t_obj)

    return running + ordered_pending + list(reversed(finished))


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
                "📭 <b>در حال حاضر هیچ پراسسی در صف وجود ندارد.</b>",
                reply_markup=main_menu_kb(),
                parse_mode="HTML"
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

    page = max(0, min(page, total_pages - 1))
    start_idx = page * PAGE_SIZE
    end_idx = start_idx + PAGE_SIZE
    page_tasks = sorted_tasks[start_idx:end_idx]

    summary_banner = (
        f"📊 <b>داشبورد زنده صف و پراسس‌ها (صفحه {page + 1} از {total_pages})</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        f"⚙️ در حال اجرا: <code>{running_count}</code>  ▏  ⏳ در انتظار: <code>{pending_count}</code>\n"
        f"✅ تکمیل‌شده: <code>{done_count}</code>  ▏  ❌ خطا/لغو: <code>{err_count}</code>\n"
        "━━━━━━━━━━━━━━━━━━━━━━"
    )

    cards = [
        _format_task_card_html(t, queue_engine.get_pending_position(t.task_id))
        for t in page_tasks
    ]
    full_text = summary_banner + "\n\n" + "\n\n".join(cards)
    kb = queue_management_kb(page_tasks, page=page, total_pages=total_pages)

    try:
        await call.message.edit_text(
            full_text[:4000],
            reply_markup=kb,
            parse_mode="HTML"
        )
        await call.answer()
    except TelegramBadRequest as e:
        err_msg = str(e).lower()
        if "message is not modified" in err_msg:
            await call.answer("✅ اطلاعات داشبورد بروز است.")
        else:
            plain_text = re.sub(r"<[^>]+>", "", full_text)
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


@router.callback_query(F.data.startswith("qprio:"))
async def cb_change_task_priority(call: CallbackQuery):
    parts = call.data.split(":")
    direction = parts[1]   # 'up' | 'down' | 'top'
    task_id = parts[2]
    page = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 0

    moved = queue_engine.move_pending_task(task_id, direction)
    if moved:
        pos, total_p = queue_engine.get_pending_position(task_id)
        await call.answer(f"✅ اولویت #{task_id} به جایگاه {pos} از {total_p} تغییر یافت!")
    else:
        await call.answer("⚠️ امکان جابجایی بیشتر در این جهت وجود ندارد.")
    await _render_queue_message(call, page=page)