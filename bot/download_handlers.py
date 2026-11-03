import re
import uuid

from pathlib import Path

from aiogram import Router, F
from aiogram.types import (
    Message,
    CallbackQuery
)
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State

from core.config_loader import cfg
from core.queue_manager import queue_engine

from services.downloader_service import DownloaderService
from services.scraper_service import ScraperService

from bot.handlers import BotStates

from bot.keyboards import (
    main_menu_kb,
    download_target_folders_kb,
    search_results_kb
)


router = Router(name="download_management")


class DownloadStates(StatesGroup):

    waiting_link = State()

    waiting_folder_name = State()


def _safe_folder_name(name: str) -> str:

    name = re.sub(
        r'[<>:"/\\|?*]',
        "",
        name
    )

    name = name.strip().replace(" ", "_")

    return name[:100] or "New_Download"


def _format_search_results(results: list[dict]) -> str:

    lines = [
        "🔍 <b>نتایج جست‌وجو</b>",
        "",
        "نام کامل هر نتیجه:",
        ""
    ]

    for idx, item in enumerate(results[:25], 1):

        title = str(
            item.get("title", "بدون عنوان")
        )

        icon = (
            "🧲"
            if item.get("type") == "magnet"
            else "📄"
        )

        lines.append(
            f"{idx}. {icon} <b>{title}</b>"
        )

    lines.extend([
        "",
        "برای انتخاب، روی دکمه مربوط به نتیجه کلیک کنید."
    ])

    return "\n".join(lines)


async def _show_download_targets(
    target: str,
    suggested_name: str,
    state: FSMContext,
    message: Message,
    edit: bool = False
):

    folders = DownloaderService.list_workspace_folders()

    await state.update_data(
        pending_download_target=target,
        suggested_folder_name=suggested_name,
        available_download_folders=[
            str(folder)
            for folder in folders
        ]
    )

    text = (
        "📥 <b>انتخاب مقصد دانلود</b>\n\n"
        f"🎬 عنوان: <b>{suggested_name}</b>\n\n"
        "یکی از پوشه‌های موجود را انتخاب کنید "
        "یا یک پوشه جدید بسازید."
    )

    keyboard = download_target_folders_kb(
        folders
    )

    if edit:

        await message.edit_text(
            text,
            reply_markup=keyboard,
            parse_mode="HTML"
        )

    else:

        await message.answer(
            text,
            reply_markup=keyboard,
            parse_mode="HTML"
        )


async def _enqueue_download(
    message: Message,
    state: FSMContext,
    parent_dir: Path,
    suggested_name: str
):

    data = await state.get_data()

    target = data.get(
        "pending_download_target"
    )

    if not target:

        return await message.answer(
            "❌ لینک دانلود پیدا نشد."
        )

    safe_name = _safe_folder_name(
        suggested_name
    )

    dest_dir = parent_dir / safe_name

    dest_dir.mkdir(
        parents=True,
        exist_ok=True
    )

    task = await queue_engine.add_task(
        "download",
        f"دانلود: {safe_name}",
        DownloaderService.download,
        target,
        dest_dir
    )

    await state.clear()

    await message.answer(
        f"✅ <b>دانلود به صف اضافه شد.</b>\n\n"
        f"🎬 عنوان: <b>{safe_name}</b>\n"
        f"🆔 شناسه: <code>{task.task_id}</code>\n"
        f"📂 مسیر: <code>{dest_dir}</code>\n\n"
        "می‌توانید دانلودهای دیگری نیز اضافه کنید.",
        reply_markup=main_menu_kb(),
        parse_mode="HTML"
    )


async def _enqueue_download_from_callback(
    call: CallbackQuery,
    state: FSMContext,
    parent_dir: Path,
    suggested_name: str
):

    data = await state.get_data()

    target = data.get(
        "pending_download_target"
    )

    if not target:

        return await call.answer(
            "لینک دانلود پیدا نشد.",
            show_alert=True
        )

    safe_name = _safe_folder_name(
        suggested_name
    )

    dest_dir = parent_dir / safe_name

    dest_dir.mkdir(
        parents=True,
        exist_ok=True
    )

    task = await queue_engine.add_task(
        "download",
        f"دانلود: {safe_name}",
        DownloaderService.download,
        target,
        dest_dir
    )

    await state.clear()

    await call.message.edit_text(
        f"✅ <b>دانلود به صف اضافه شد.</b>\n\n"
        f"🎬 عنوان: <b>{safe_name}</b>\n"
        f"🆔 شناسه: <code>{task.task_id}</code>\n"
        f"📂 مسیر: <code>{dest_dir}</code>\n\n"
        "می‌توانید دانلودهای دیگری نیز اضافه کنید.",
        reply_markup=main_menu_kb(),
        parse_mode="HTML"
    )


# ==================================================
# DIRECT DOWNLOAD
# ==================================================

@router.callback_query(F.data == "menu:direct_dl")
async def start_direct_download(
    call: CallbackQuery,
    state: FSMContext
):

    await state.clear()

    await state.set_state(
        DownloadStates.waiting_link
    )

    await call.message.edit_text(
        "📥 <b>دانلود مستقیم</b>\n\n"
        "لینک مورد نظر را ارسال کنید:\n\n"
        "• HTTP / HTTPS\n"
        "• Magnet\n"
        "• Torrent\n"
        "• Mega.nz\n"
        "• Google Drive\n\n"
        "همچنین می‌توانید فایل .torrent را ارسال کنید.",
        parse_mode="HTML"
    )

    await call.answer()


@router.message(
    DownloadStates.waiting_link,
    F.text
)
async def receive_download_link(
    message: Message,
    state: FSMContext
):

    target = (
        message.text or ""
    ).strip()

    if not target.startswith(
        ("http://", "https://", "magnet:?")
    ):

        return await message.answer(
            "⚠️ لینک معتبر ارسال کنید."
        )

    await state.update_data(
        pending_download_target=target
    )

    await _show_download_targets(
        target=target,
        suggested_name="New_Download",
        state=state,
        message=message
    )


@router.message(
    DownloadStates.waiting_link,
    F.document
)
async def receive_download_torrent(
    message: Message,
    state: FSMContext
):

    filename = (
        message.document.file_name or ""
    )

    if not filename.lower().endswith(".torrent"):

        return await message.answer(
            "⚠️ لطفاً فایل .torrent ارسال کنید."
        )

    workspace = Path(
        cfg.get("paths", "workspace_dir")
    ).resolve()

    incoming_dir = workspace / "_incoming_torrents"

    incoming_dir.mkdir(
        parents=True,
        exist_ok=True
    )

    safe_filename = Path(filename).name

    torrent_path = incoming_dir / (
        f"{uuid.uuid4().hex[:8]}_{safe_filename}"
    )

    await message.bot.download(
        message.document,
        destination=torrent_path
    )

    target = str(torrent_path)

    await _show_download_targets(
        target=target,
        suggested_name=Path(filename).stem,
        state=state,
        message=message
    )


# ==================================================
# DIRECT LINKS SENT OUTSIDE THE DOWNLOAD MENU
# ==================================================

@router.message(
    F.text.regexp(r"^(magnet:\?xt=|https?://)")
)
async def catch_direct_link(
    message: Message,
    state: FSMContext
):

    if message.from_user.id not in cfg.get(
        "telegram",
        "admin_ids",
        []
    ):
        return

    target = message.text.strip()

    await state.clear()

    await _show_download_targets(
        target=target,
        suggested_name="New_Download",
        state=state,
        message=message
    )


@router.message(
    F.document.file_name.regexp(r"(?i)\.torrent$")
)
async def catch_torrent_file(
    message: Message,
    state: FSMContext
):

    if message.from_user.id not in cfg.get(
        "telegram",
        "admin_ids",
        []
    ):
        return

    workspace = Path(
        cfg.get("paths", "workspace_dir")
    ).resolve()

    incoming_dir = workspace / "_incoming_torrents"

    incoming_dir.mkdir(
        parents=True,
        exist_ok=True
    )

    filename = Path(
        message.document.file_name
    ).name

    torrent_path = incoming_dir / (
        f"{uuid.uuid4().hex[:8]}_{filename}"
    )

    await message.bot.download(
        message.document,
        destination=torrent_path
    )

    await state.clear()

    await _show_download_targets(
        target=str(torrent_path),
        suggested_name=Path(filename).stem,
        state=state,
        message=message
    )


# ==================================================
# EXISTING FOLDER SELECTION
# ==================================================

@router.callback_query(F.data.startswith("dlfolder:"))
async def choose_existing_download_folder(
    call: CallbackQuery,
    state: FSMContext
):

    try:

        idx = int(
            call.data.split(":")[1]
        )

        data = await state.get_data()

        folders = data.get(
            "available_download_folders",
            []
        )

        if idx < 0 or idx >= len(folders):

            return await call.answer(
                "پوشه انتخاب شده معتبر نیست.",
                show_alert=True
            )

        selected_folder = Path(
            folders[idx]
        ).resolve()

        workspace = Path(
            cfg.get("paths", "workspace_dir")
        ).resolve()

        if not selected_folder.is_relative_to(workspace):

            return await call.answer(
                "مسیر انتخاب شده خارج از Workspace است.",
                show_alert=True
            )

        if not selected_folder.is_dir():

            return await call.answer(
                "پوشه دیگر وجود ندارد.",
                show_alert=True
            )

        suggested_name = data.get(
            "suggested_folder_name",
            "New_Download"
        )

        await _enqueue_download_from_callback(
            call,
            state,
            selected_folder,
            suggested_name
        )

        await call.answer(
            "دانلود به صف اضافه شد."
        )

    except Exception as exc:

        await call.answer(
            f"خطا: {str(exc)[:150]}",
            show_alert=True
        )


# ==================================================
# CREATE NEW FOLDER
# ==================================================

@router.callback_query(F.data == "dlnew")
async def create_download_folder_start(
    call: CallbackQuery,
    state: FSMContext
):

    await state.set_state(
        DownloadStates.waiting_folder_name
    )

    data = await state.get_data()

    suggested_name = data.get(
        "suggested_folder_name",
        ""
    )

    await call.message.edit_text(
        "📁 <b>ساخت پوشه جدید</b>\n\n"
        f"نام پیشنهادی:\n<code>{suggested_name}</code>\n\n"
        "نام پوشه مقصد را ارسال کنید:",
        parse_mode="HTML"
    )

    await call.answer()


@router.message(
    DownloadStates.waiting_folder_name,
    F.text
)
async def receive_new_download_folder_name(
    message: Message,
    state: FSMContext
):

    folder_name = _safe_folder_name(
        message.text
    )

    workspace = Path(
        cfg.get("paths", "workspace_dir")
    ).resolve()

    folders = DownloaderService.create_movie_folders(
        folder_name
    )

    await _enqueue_download(
        message,
        state,
        workspace,
        folder_name
    )


# ==================================================
# SEARCH
# ==================================================

@router.message(
    BotStates.waiting_search_query,
    F.text
)
async def search_with_full_titles(
    message: Message,
    state: FSMContext
):

    data = await state.get_data()

    site_key = data.get(
        "selected_site"
    )

    if not site_key:

        return await message.answer(
            "❌ سایت جست‌وجو مشخص نشده است."
        )

    wait_message = await message.answer(
        "🔍 در حال جست‌وجو..."
    )

    try:

        results = await ScraperService.search(
            site_key,
            message.text.strip()
        )

        if not results:

            return await wait_message.edit_text(
                "❌ نتیجه‌ای یافت نشد.",
                reply_markup=main_menu_kb()
            )

        results = results[:25]

        await state.update_data(
            search_results=results
        )

        await wait_message.edit_text(
            _format_search_results(results),
            reply_markup=search_results_kb(results),
            parse_mode="HTML"
        )

    except Exception as exc:

        await wait_message.edit_text(
            f"❌ خطا در جست‌وجو:\n<code>{str(exc)[:1000]}</code>",
            reply_markup=main_menu_kb(),
            parse_mode="HTML"
        )


@router.callback_query(
    F.data.startswith("sres:")
)
async def choose_search_result(
    call: CallbackQuery,
    state: FSMContext
):

    try:

        idx = int(
            call.data.split(":")[1]
        )

        data = await state.get_data()

        results = data.get(
            "search_results",
            []
        )

        if idx < 0 or idx >= len(results):

            return await call.answer(
                "نتیجه انتخاب شده معتبر نیست.",
                show_alert=True
            )

        chosen = results[idx]

        if chosen.get("type") == "page":

            await call.message.edit_text(
                "⏳ در حال استخراج لینک‌های دانلود..."
            )

            sub_links = await ScraperService.extract_links_from_page(
                chosen["target"]
            )

            if not sub_links:

                return await call.message.edit_text(
                    "❌ لینک مستقیمی پیدا نشد.",
                    reply_markup=main_menu_kb()
                )

            for link in sub_links:
                link["type"] = "direct"

            await state.update_data(
                search_results=sub_links
            )

            await call.message.edit_text(
                _format_search_results(sub_links),
                reply_markup=search_results_kb(sub_links),
                parse_mode="HTML"
            )

            return

        target = chosen.get("target")

        if not target:

            return await call.answer(
                "لینک دانلود پیدا نشد.",
                show_alert=True
            )

        await _show_download_targets(
            target=target,
            suggested_name=chosen.get(
                "title",
                "New_Download"
            ),
            state=state,
            message=call.message,
            edit=True
        )

        await call.answer()

    except Exception as exc:

        await call.answer(
            f"خطا: {str(exc)[:150]}",
            show_alert=True
        )