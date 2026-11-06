import math
import re
import shutil
import uuid
from pathlib import Path
from aiogram import Router, F, Bot
from aiogram.types import Message, CallbackQuery, FSInputFile, InlineKeyboardButton
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.utils.keyboard import InlineKeyboardBuilder

from core.config_loader import cfg
from core.queue_manager import queue_engine
from services.scraper_service import ScraperService
from services.downloader_service import DownloaderService
from services.mkv_service import MKVService
from services.automation_service import AutomationService
from bot.keyboards import (
    BROWSER_PAGE_SIZE, main_menu_kb, search_categories_kb,
    search_sites_kb, search_results_kb, download_folder_tree_browser_kb,
    extract_sub_entry_kb, local_extract_browser_kb
)

router = Router()


class DownloadStates(StatesGroup):
    waiting_search_query = State()
    waiting_direct_link = State()
    browsing_download_folder = State()
    waiting_new_dl_subfolder_name = State()
    waiting_link_for_sub_extract = State()
    browsing_local_for_sub_extract = State()


def is_admin(user_id: int) -> bool:
    return user_id in cfg.get("telegram", "admin_ids", [])


def _extract_batch_links(raw_text: str) -> list[str]:
    if not raw_text:
        return []
    links: list[str] = []
    for line in raw_text.splitlines():
        clean = line.strip()
        if not clean:
            continue
        matches = re.findall(r"(magnet:\?xt=[^\s]+|https?://[^\s]+)", clean)
        if matches:
            for m in matches:
                if m not in links:
                    links.append(m)
        elif clean.startswith(("http://", "https://", "magnet:?")):
            if clean not in links:
                links.append(clean)
    return links


async def _render_download_folder_browser(
    target_msg_or_call: Message | CallbackQuery,
    state: FSMContext,
    current_dir: Path,
    page: int = 0
):
    workspace = Path(cfg.get("paths", "workspace_dir")).resolve()
    AutomationService.ensure_workspace_root_categories()
    current_dir = current_dir.resolve()
    current_dir.mkdir(parents=True, exist_ok=True)

    subfolders = sorted(
        [
            p for p in current_dir.iterdir()
            if p.is_dir() and not p.name.startswith("_") and p.name != "browser_profile"
        ],
        key=lambda x: x.name.lower()
    )

    total_folders = len(subfolders)
    total_pages = max(1, math.ceil(total_folders / BROWSER_PAGE_SIZE))
    page = max(0, min(page, total_pages - 1))
    start_idx = page * BROWSER_PAGE_SIZE
    end_idx = start_idx + BROWSER_PAGE_SIZE

    data = await state.get_data()
    targets = data.get("pending_direct_targets", [])

    await state.set_state(DownloadStates.browsing_download_folder)
    await state.update_data(
        dl_browser_curr_dir=str(current_dir),
        dl_browser_subfolders=[str(f.resolve()) for f in subfolders],
        dl_browser_page=page
    )

    is_root = (current_dir == workspace)
    folder_lines = "\n".join([
        f"`[{i+1}]` 📁 `{subfolders[i].name}`"
        for i in range(start_idx, min(end_idx, total_folders))
    ])
    list_block = folder_lines if folder_lines else "_(زیرپوشه‌ای در این مسیر وجود ندارد)_"
    batch_badge = f"📦 **تعداد لینک‌های آماده دانلود:** `{len(targets)}` مورد\n" if targets else ""
    page_badge = f" (صفحه {page + 1} از {total_pages} - کل: {total_folders})" if total_pages > 1 else ""

    text = (
        f"📂 **مرورگر انتخاب پوشه مقصد دانلود{page_badge}**\n"
        f"{batch_badge}"
        f"📍 **مسیر فعلی باز شده:**\n`{current_dir}`\n\n"
        f"📋 **زیرپوشه‌های این صفحه:**\n{list_block}\n\n"
        "• برای دانلود در همین پوشه فعلی، دکمه **«📥 دانلود در همین پوشه فعلی»** را بزنید.\n"
        "• برای ورود به زیرپوشه‌های بعدی، روی نام پوشه کلیک کنید و یا در همینجا پوشه جدید بسازید:"
    )
    kb = download_folder_tree_browser_kb(subfolders, current_dir, is_root, page=page)

    if isinstance(target_msg_or_call, CallbackQuery):
        await target_msg_or_call.message.edit_text(text[:4000], reply_markup=kb, parse_mode="Markdown")
    else:
        await target_msg_or_call.answer(text[:4000], reply_markup=kb, parse_mode="Markdown")


async def _enqueue_batch_downloads(targets: list[str], dest_dir: Path) -> list[str]:
    task_ids = []
    total = len(targets)
    for idx, t_link in enumerate(targets, start=1):
        label = f"دانلود [{idx}/{total}] در {dest_dir.name}" if total > 1 else f"دانلود در {dest_dir.name}"
        task = await queue_engine.add_task(
            "download",
            label,
            DownloaderService.download,
            t_link,
            dest_dir
        )
        task_ids.append(task.task_id)
    return task_ids


# ==================== ۱. جستجو و دانلود از ۹ سایت ====================
@router.callback_query(F.data == "menu:search")
async def cb_search_menu(call: CallbackQuery):
    await call.message.edit_text("🔍 دسته‌بندی سایت مورد نظر را انتخاب کنید:", reply_markup=search_categories_kb())


@router.callback_query(F.data.startswith("scat:"))
async def cb_search_cat(call: CallbackQuery):
    cat = call.data.split(":")[1]
    await call.message.edit_text("🌐 سایت مورد نظر برای جستجو را انتخاب کنید:", reply_markup=search_sites_kb(cat))


@router.callback_query(F.data.startswith("ssite:"))
async def cb_select_site(call: CallbackQuery, state: FSMContext):
    site_key = call.data.split(":")[1]
    await state.update_data(selected_site=site_key)
    await state.set_state(DownloadStates.waiting_search_query)
    await call.message.edit_text(f"✏️ نام فیلم، سریال یا انیمه را برای جستجو در `{site_key}` ارسال کنید:", parse_mode="Markdown")


@router.message(DownloadStates.waiting_search_query)
async def handle_search_query(message: Message, state: FSMContext):
    data = await state.get_data()
    site_key = data["selected_site"]
    wait_msg = await message.answer("⏳ در حال جستجو...")

    try:
        results = await ScraperService.search(site_key, message.text.strip())
        if not results:
            return await wait_msg.edit_text("❌ نتیجه‌ای یافت نشد.", reply_markup=main_menu_kb())

        await state.update_data(search_results=results)
        res_lines = "\n".join([f"`[{i+1}]` `{r['title']}`" for i, r in enumerate(results[:15])])
        await wait_msg.edit_text(
            f"✅ نتایج یافت شده برای `{message.text}`:\n\n{res_lines}\n\nروی گزینه دلخواه کلیک کنید تا دانلود شود:",
            reply_markup=search_results_kb(results),
            parse_mode="Markdown"
        )
    except Exception as e:
        await wait_msg.edit_text(f"❌ خطا در جستجو:\n`{e}`", reply_markup=main_menu_kb(), parse_mode="Markdown")


@router.callback_query(F.data.startswith("sres:"))
async def cb_click_search_result(call: CallbackQuery, state: FSMContext):
    idx = int(call.data.split(":")[1])
    data = await state.get_data()
    results = data.get("search_results", [])
    if idx >= len(results):
        return await call.answer("⚠️ نتیجه منقضی شده است، دوباره جستجو کنید.", show_alert=True)

    chosen = results[idx]

    if chosen.get("type") == "page":
        await call.message.edit_text("⏳ در حال استخراج لینک‌های Mega و Google Drive از داخل صفحه...")
        sub_links = await ScraperService.extract_links_from_page(chosen["target"])
        if not sub_links:
            return await call.message.edit_text("❌ لینک مستقیمی در صفحه پیدا نشد.", reply_markup=main_menu_kb())
        for sl in sub_links:
            sl["type"] = "direct"
        await state.update_data(search_results=sub_links)
        return await call.message.edit_text(
            "🔗 لینک‌های استخراج شده از صفحه (برای شروع دانلود کلیک کنید):",
            reply_markup=search_results_kb(sub_links)
        )

    await state.update_data(pending_direct_targets=[chosen["target"]])
    workspace = Path(cfg.get("paths", "workspace_dir")).resolve()
    await _render_download_folder_browser(call, state, workspace, page=0)


# ==================== ۲. دانلود لینک مستقیم / تورنت / مگا / گوگل درایو ====================
@router.callback_query(F.data == "menu:direct_dl")
async def cb_direct_dl(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await state.set_state(DownloadStates.waiting_direct_link)
    await call.message.edit_text(
        "📥 **دانلود فیلم یا سریال (تکی و گروهی Batch)**\n\n"
        "🔗 لطفاً **یک یا چند لینک (مستقیم، Magnet، Mega.nz، Google Drive)** را ارسال کنید:\n"
        "• برای **دانلود گروهی (Batch)**، کافی است بین هر لینک یک خط فاصله بگذارید (یا هر لینک را در یک خط قرار دهید).\n"
        "• همچنین می‌توانید فایل `.torrent` ارسال کنید:",
        parse_mode="Markdown"
    )


@router.message(DownloadStates.waiting_direct_link)
async def handle_direct_link_input(message: Message, state: FSMContext):
    if message.document and message.document.file_name and message.document.file_name.lower().endswith(".torrent"):
        temp_dir = Path(cfg.get("paths", "workspace_dir")) / "_incoming_torrents"
        temp_dir.mkdir(parents=True, exist_ok=True)
        t_path = temp_dir / message.document.file_name
        await message.bot.download(message.document, destination=t_path)
        targets = [str(t_path.resolve())]
    elif message.text:
        targets = _extract_batch_links(message.text)
        if not targets:
            return await message.answer("⚠️ هیچ لینک معتبری در پیام شما یافت نشد! لطفاً لینک‌ها را با `http://`، `https://` یا `magnet:?` ارسال کنید:")
    else:
        return await message.answer("⚠️ لطفاً لینک یا فایل `.torrent` ارسال کنید:")

    await state.update_data(pending_direct_targets=targets)
    workspace = Path(cfg.get("paths", "workspace_dir")).resolve()
    await _render_download_folder_browser(message, state, workspace, page=0)


@router.callback_query(DownloadStates.browsing_download_folder, F.data.startswith("dlnav:page:"))
async def cb_dlnav_change_page(call: CallbackQuery, state: FSMContext):
    page = int(call.data.split(":")[2])
    data = await state.get_data()
    curr = Path(data.get("dl_browser_curr_dir", cfg.get("paths", "workspace_dir"))).resolve()
    await _render_download_folder_browser(call, state, curr, page=page)


@router.callback_query(DownloadStates.browsing_download_folder, F.data.startswith("dlnav:open:"))
async def cb_dlnav_open_subfolder(call: CallbackQuery, state: FSMContext):
    idx = int(call.data.split(":")[2])
    data = await state.get_data()
    subfolders = data.get("dl_browser_subfolders", [])
    if idx < len(subfolders):
        await _render_download_folder_browser(call, state, Path(subfolders[idx]), page=0)


@router.callback_query(DownloadStates.browsing_download_folder, F.data == "dlnav:up")
async def cb_dlnav_up(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    curr = Path(data.get("dl_browser_curr_dir", cfg.get("paths", "workspace_dir"))).resolve()
    workspace = Path(cfg.get("paths", "workspace_dir")).resolve()
    parent = curr.parent if curr != workspace else workspace
    await _render_download_folder_browser(call, state, parent, page=0)


@router.callback_query(DownloadStates.browsing_download_folder, F.data == "dlnav:select_here")
async def cb_dlnav_select_here(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    chosen_dir = Path(data.get("dl_browser_curr_dir", cfg.get("paths", "workspace_dir"))).resolve()
    chosen_dir.mkdir(parents=True, exist_ok=True)

    targets = data.get("pending_direct_targets", [])
    if not targets and data.get("pending_direct_target"):
        targets = [data["pending_direct_target"]]

    task_ids = await _enqueue_batch_downloads(targets, chosen_dir)
    await state.clear()

    ids_str = ", ".join([f"`{tid}`" for tid in task_ids])
    await call.message.edit_text(
        f"✅ تعداد **`{len(task_ids)}` دانلود** با موفقیت در صف قرار گرفت!\n"
        f"📂 مسیر دقیق ذخیره:\n`{chosen_dir}`\n"
        f"🆔 کد تسک‌ها: {ids_str}",
        reply_markup=main_menu_kb(),
        parse_mode="Markdown"
    )


@router.callback_query(DownloadStates.browsing_download_folder, F.data == "dlnav:mkdir_here")
async def cb_dlnav_mkdir_here(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    curr_dir = data.get("dl_browser_curr_dir", cfg.get("paths", "workspace_dir"))
    await state.set_state(DownloadStates.waiting_new_dl_subfolder_name)

    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text="🔙 بازگشت به مرورگر پوشه", callback_data="dlnav:resume"))
    await call.message.edit_text(
        f"➕ **ساخت پوشه جدید در مسیر:**\n`{curr_dir}`\n\n"
        "✏️ لطفاً **نام پوشه جدید** را ارسال کنید (می‌توانید نام ساده مثل `1080p-Pahe` یا مسیر چندسطحی مثل `The.Greatest/S01/FullHD` بفرستید):",
        reply_markup=kb.as_markup(),
        parse_mode="Markdown"
    )


@router.callback_query(F.data == "dlnav:resume")
async def cb_dlnav_resume(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    curr_dir = Path(data.get("dl_browser_curr_dir", cfg.get("paths", "workspace_dir")))
    await _render_download_folder_browser(call, state, curr_dir, page=data.get("dl_browser_page", 0))


@router.message(DownloadStates.waiting_new_dl_subfolder_name)
async def handle_new_dl_subfolder_name(message: Message, state: FSMContext):
    raw_name = re.sub(r'[<>:"|?*]', "", (message.text or "").strip()).strip().lstrip("/\\")
    if not raw_name:
        return await message.answer("⚠️ نام پوشه معتبر نیست. لطفاً دوباره ارسال کنید:")

    data = await state.get_data()
    curr_dir = Path(data.get("dl_browser_curr_dir", cfg.get("paths", "workspace_dir"))).resolve()
    new_dir = (curr_dir / raw_name).resolve()
    new_dir.mkdir(parents=True, exist_ok=True)

    parts_low = [p.lower() for p in new_dir.parts]
    if ("movies" in parts_low or "movie" in parts_low) and new_dir.parent.name.lower() in ("movies", "movie"):
        AutomationService.create_movie_structure(new_dir)
    elif AutomationService.SEASON_DIR_REGEX.match(new_dir.name):
        AutomationService.create_series_season_structure(new_dir)

    await message.answer(f"✅ پوشه `{new_dir.name}` ساخته شد و وارد آن شدید!", parse_mode="Markdown")
    await _render_download_folder_browser(message, state, new_dir, page=0)


# ==================== ۳. استخراج زیرنویس (همراه با صفحه‌بندی دایرکتوری) ====================
@router.callback_query(F.data == "menu:link_extract_sub")
async def cb_link_extract_sub_menu(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.message.edit_text(
        "📤 **بخش استخراج زیرنویس از فیلم**\n\n"
        "لطفاً روش مورد نظر خود را انتخاب کنید:\n"
        "1️⃣ **از فایل موجود در دایرکتوری:** انتخاب فیلم از داخل پوشه‌های سرور و دریافت زیرنویس‌های داخل آن.\n"
        "2️⃣ **از طریق لینک دانلود (تکی یا Batch):** ارسال یک یا چند لینک تا دانلود شده، زیرنویس‌هایشان در چت ارسال شود و پوشه موقتشان خودکار پاک گردد.",
        reply_markup=extract_sub_entry_kb(),
        parse_mode="Markdown"
    )


async def _render_local_extract_browser(
    call: CallbackQuery,
    state: FSMContext,
    browse_dir: Path,
    page: int = 0
):
    workspace = Path(cfg.get("paths", "workspace_dir")).resolve()
    AutomationService.ensure_workspace_root_categories()
    browse_dir = browse_dir.resolve()
    browse_dir.mkdir(parents=True, exist_ok=True)

    folders = sorted(
        [p for p in browse_dir.iterdir() if p.is_dir() and not p.name.startswith("_") and p.name != "browser_profile"],
        key=lambda x: x.name.lower()
    )
    videos = DownloaderService.find_video_files(browse_dir, recursive=False)

    combined: list[tuple[str, int, Path]] = (
        [("dir", i, f) for i, f in enumerate(folders)]
        + [("vid", i, v) for i, v in enumerate(videos)]
    )
    total_items = len(combined)
    total_pages = max(1, math.ceil(total_items / BROWSER_PAGE_SIZE))
    page = max(0, min(page, total_pages - 1))
    start_idx = page * BROWSER_PAGE_SIZE
    end_idx = start_idx + BROWSER_PAGE_SIZE

    await state.set_state(DownloadStates.browsing_local_for_sub_extract)
    await state.update_data(
        ext_browse_dir=str(browse_dir),
        ext_dir_folders=[str(f.resolve()) for f in folders],
        ext_dir_videos=[str(v.resolve()) for v in videos],
        ext_browse_page=page
    )

    is_root = (browse_dir == workspace)

    full_lines = []
    for kind, orig_idx, path_obj in combined[start_idx:end_idx]:
        if kind == "dir":
            full_lines.append(f"`[{orig_idx+1}]` 📁 `{path_obj.name}`")
        else:
            size_mb = path_obj.stat().st_size / (1024 * 1024)
            full_lines.append(f"`[F{orig_idx+1}]` 🎬 `{path_obj.name}` _({size_mb:.1f} MB)_")

    details_block = "\n".join(full_lines) if full_lines else "_(در این مسیر پوشه یا فایل ویدئویی وجود ندارد)_"
    page_badge = f" (صفحه {page + 1} از {total_pages} - کل: {total_items})" if total_pages > 1 else ""

    msg_text = (
        f"📂 **استخراج زیرنویس از فایل موجود در دایرکتوری{page_badge}**\n"
        f"📍 مسیر فعلی:\n`{browse_dir}`\n\n"
        f"📋 **لیست پوشه‌ها و فیلم‌های این صفحه:**\n{details_block}\n\n"
        "روی پوشه کلیک کنید تا باز شود، یا روی فیلم (`🎬`) کلیک کنید تا ترک‌های زیرنویس آن نمایش داده شود:"
    )

    await call.message.edit_text(
        msg_text[:4000],
        reply_markup=local_extract_browser_kb(folders, videos, is_root, page=page),
        parse_mode="Markdown"
    )


@router.callback_query(F.data == "extmode:local")
async def cb_extmode_local_start(call: CallbackQuery, state: FSMContext):
    workspace = Path(cfg.get("paths", "workspace_dir")).resolve()
    await _render_local_extract_browser(call, state, workspace, page=0)


@router.callback_query(DownloadStates.browsing_local_for_sub_extract, F.data.startswith("extnav:page:"))
async def cb_extnav_change_page(call: CallbackQuery, state: FSMContext):
    page = int(call.data.split(":")[2])
    data = await state.get_data()
    curr = Path(data.get("ext_browse_dir", cfg.get("paths", "workspace_dir"))).resolve()
    await _render_local_extract_browser(call, state, curr, page=page)


@router.callback_query(DownloadStates.browsing_local_for_sub_extract, F.data.startswith("extnav:dir:"))
async def cb_extnav_open_dir(call: CallbackQuery, state: FSMContext):
    idx = int(call.data.split(":")[2])
    data = await state.get_data()
    folders = data.get("ext_dir_folders", [])
    if idx < len(folders):
        await _render_local_extract_browser(call, state, Path(folders[idx]), page=0)


@router.callback_query(DownloadStates.browsing_local_for_sub_extract, F.data == "extnav:up")
async def cb_extnav_up(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    curr = Path(data.get("ext_browse_dir", cfg.get("paths", "workspace_dir"))).resolve()
    workspace = Path(cfg.get("paths", "workspace_dir")).resolve()
    parent = curr.parent if curr != workspace else workspace
    await _render_local_extract_browser(call, state, parent, page=0)


@router.callback_query(DownloadStates.browsing_local_for_sub_extract, F.data.startswith("extnav:vid:"))
async def cb_extnav_select_video(call: CallbackQuery, state: FSMContext):
    idx = int(call.data.split(":")[2])
    data = await state.get_data()
    videos = data.get("ext_dir_videos", [])
    if idx >= len(videos):
        return await call.answer("⚠️ فایل یافت نشد.", show_alert=True)

    video_path = Path(videos[idx]).resolve()
    tracks = await MKVService.list_subtitle_tracks(video_path)
    if not tracks:
        return await call.answer("❌ هیچ ترک زیرنویسی داخل این فایل ویدئویی وجود ندارد!", show_alert=True)

    await state.update_data(selected_video=str(video_path))

    kb = InlineKeyboardBuilder()
    kb.row(
        InlineKeyboardButton(
            text=f"📥 استخراج یکجای تمام زیرنویس‌ها ({len(tracks)} ترک)",
            callback_data="extall_local"
        )
    )
    for t in tracks:
        kb.row(
            InlineKeyboardButton(
                text=f"📤 Track #{t['id']} | {t['language']} | {t['track_name']} ({t['codec']})",
                callback_data=f"exttrack_local:{t['id']}:{t['codec']}"
            )
        )
    kb.row(
        InlineKeyboardButton(
            text="🔙 بازگشت به پوشه",
            callback_data="extnav:back_to_dir"
        )
    )

    await call.message.edit_text(
        f"🎬 **فایل انتخاب شده:**\n`{video_path.name}`\n\n"
        f"✅ تعداد **`{len(tracks)}` ترک زیرنویس** در این فیلم پیدا شد.\n"
        "می‌توانید همه را یکجا استخراج کنید و یا ترک مورد نظر را انتخاب نمایید:",
        reply_markup=kb.as_markup(),
        parse_mode="Markdown"
    )


@router.callback_query(F.data == "extnav:back_to_dir")
async def cb_extnav_back_to_dir(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    curr = Path(data.get("ext_browse_dir", cfg.get("paths", "workspace_dir")))
    await _render_local_extract_browser(call, state, curr, page=data.get("ext_browse_page", 0))


@router.callback_query(F.data.startswith("exttrack_local:"))
async def cb_extract_single_local_track(call: CallbackQuery, state: FSMContext):
    parts = call.data.split(":")
    track_id = int(parts[1])
    codec_lower = parts[2].lower() if len(parts) > 2 else "srt"

    data = await state.get_data()
    video_path = Path(data["selected_video"]).resolve()

    sub_dir = (video_path.parent / "srt") if (video_path.parent / "srt").is_dir() else video_path.parent
    ext = ".ass" if ("ass" in codec_lower or "ssa" in codec_lower) else (".sup" if "pgs" in codec_lower else ".srt")
    out_sub = sub_dir / f"{video_path.stem}_Track{track_id}{ext}"

    await call.message.edit_text(
        f"⏳ در حال استخراج زیرنویس `Track #{track_id}` از:\n`{video_path.name}`...",
        parse_mode="Markdown"
    )
    await MKVService.extract_subtitle_track(video_path, track_id, out_sub)

    await call.message.answer_document(
        FSInputFile(out_sub),
        caption=(
            f"✅ زیرنویس استخراج شد!\n"
            f"🎬 فیلم: `{video_path.name}`\n"
            f"📄 فایل ذخیره شده در سرور:\n`{out_sub}`"
        ),
        parse_mode="Markdown"
    )
    curr = Path(data.get("ext_browse_dir", video_path.parent))
    await _render_local_extract_browser(call, state, curr, page=data.get("ext_browse_page", 0))


@router.callback_query(F.data == "extall_local")
async def cb_extract_all_local_tracks(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    video_path = Path(data["selected_video"]).resolve()
    sub_dir = (video_path.parent / "srt") if (video_path.parent / "srt").is_dir() else video_path.parent

    await call.message.edit_text(
        f"⏳ در حال استخراج تمامی زیرنویس‌های:\n`{video_path.name}`...",
        parse_mode="Markdown"
    )
    extracted_files = await MKVService.extract_all_subtitles(video_path, sub_dir)

    if not extracted_files:
        return await call.answer("❌ هیچ زیرنویسی استخراج نشد.", show_alert=True)

    for sub_p in extracted_files:
        await call.message.answer_document(
            FSInputFile(sub_p),
            caption=f"📤 زیرنویس استخراج شده:\n`{sub_p.name}`\n📂 مسیر در سرور: `{sub_p.parent}`",
            parse_mode="Markdown"
        )

    curr = Path(data.get("ext_browse_dir", video_path.parent))
    await _render_local_extract_browser(call, state, curr, page=data.get("ext_browse_page", 0))


async def _download_and_extract_subs_job(
    bot: Bot,
    chat_id: int,
    target_link: str,
    temp_dir: Path,
    progress_callback=None
) -> list[str]:
    try:
        await DownloaderService.download(target_link, temp_dir, progress_callback=progress_callback)
        videos = DownloaderService.find_video_files(temp_dir)
        if not videos:
            raise FileNotFoundError("هیچ فایل ویدئویی پس از دانلود یافت نشد.")

        video_file = videos[0]
        if progress_callback:
            progress_callback(f"در حال استخراج زیرنویس‌ها از {video_file.name}...")

        subs_dir = temp_dir / "extracted_subs"
        extracted = await MKVService.extract_all_subtitles(video_file, subs_dir, progress_callback=progress_callback)

        if not extracted:
            await bot.send_message(
                chat_id,
                f"⚠️ فیلم `{video_file.name}` دانلود شد اما هیچ ترک زیرنویسی داخل آن وجود نداشت.",
                parse_mode="Markdown"
            )
            return []

        for sub_p in extracted:
            await bot.send_document(
                chat_id,
                FSInputFile(sub_p),
                caption=f"📤 زیرنویس استخراج شده از:\n`{video_file.name}`\n📄 فایل: `{sub_p.name}`",
                parse_mode="Markdown"
            )

        if progress_callback:
            progress_callback("در حال پاکسازی پوشه موقت SubExtract...")
        return [sub_p.name for sub_p in extracted]

    finally:
        try:
            if temp_dir.exists():
                shutil.rmtree(temp_dir, ignore_errors=True)
        except Exception:
            pass


@router.callback_query(F.data == "extmode:link")
async def cb_extmode_link_start(call: CallbackQuery, state: FSMContext):
    await state.set_state(DownloadStates.waiting_link_for_sub_extract)
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text="🔙 بازگشت", callback_data="menu:link_extract_sub"))
    await call.message.edit_text(
        "📤 **دانلود فیلم و استخراج خودکار زیرنویس (تکی و Batch)**\n\n"
        "یک یا چند لینک فیلم (مستقیم، Magnet، Mega، GDrive) را با یک خط فاصله ارسال کنید.\n"
        "ربات فیلم‌ها را دانلود کرده، زیرنویس‌هایشان را در چت می‌فرستد و پوشه موقت `SubExtract` را خودکار پاک می‌کند:",
        reply_markup=kb.as_markup(),
        parse_mode="Markdown"
    )


@router.message(DownloadStates.waiting_link_for_sub_extract)
async def handle_link_for_sub_extract(message: Message, state: FSMContext):
    targets: list[str] = []
    if message.document and message.document.file_name and message.document.file_name.lower().endswith(".torrent"):
        job_uid = str(uuid.uuid4())[:6]
        temp_dir = Path(cfg.get("paths", "workspace_dir")) / f"SubExtract_{job_uid}"
        temp_dir.mkdir(parents=True, exist_ok=True)
        t_path = temp_dir / message.document.file_name
        await message.bot.download(message.document, destination=t_path)
        targets = [str(t_path.resolve())]
    elif message.text:
        targets = _extract_batch_links(message.text)

    if not targets:
        return await message.answer("⚠️ لطفاً حداقل یک لینک معتبر یا فایل `.torrent` ارسال کنید:")

    task_ids = []
    for t_link in targets:
        job_uid = str(uuid.uuid4())[:6]
        temp_dir = Path(cfg.get("paths", "workspace_dir")) / f"SubExtract_{job_uid}"
        temp_dir.mkdir(parents=True, exist_ok=True)
        task = await queue_engine.add_task(
            "download",
            f"دانلود + استخراج زیرنویس ({job_uid})",
            _download_and_extract_subs_job,
            message.bot,
            message.chat.id,
            t_link,
            temp_dir
        )
        task_ids.append(task.task_id)

    await state.clear()
    ids_str = ", ".join([f"`{tid}`" for tid in task_ids])
    await message.answer(
        f"✅ تعداد **`{len(task_ids)}` تسک استخراج زیرنویس** در صف قرار گرفت! ({ids_str})\n"
        "به محض اتمام دانلود هر کدام، زیرنویس‌ها ارسال شده و پوشه موقت حذف می‌گردد.",
        reply_markup=main_menu_kb(),
        parse_mode="Markdown"
    )


@router.message(StateFilter(None), F.document)
async def receive_download_torrent(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    if message.document.file_name and message.document.file_name.lower().endswith(".torrent"):
        temp_dir = Path(cfg.get("paths", "workspace_dir")) / "_incoming_torrents"
        temp_dir.mkdir(parents=True, exist_ok=True)
        t_path = temp_dir / message.document.file_name
        await message.bot.download(message.document, destination=t_path)

        await state.update_data(pending_direct_targets=[str(t_path.resolve())])
        workspace = Path(cfg.get("paths", "workspace_dir")).resolve()
        await _render_download_folder_browser(message, state, workspace, page=0)


@router.message(StateFilter(None), F.text.regexp(r"(magnet:\?xt=|https?://)"))
async def catch_direct_links_in_chat(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    targets = _extract_batch_links(message.text or "")
    if not targets:
        return
    await state.update_data(pending_direct_targets=targets)
    workspace = Path(cfg.get("paths", "workspace_dir")).resolve()
    await _render_download_folder_browser(message, state, workspace, page=0)