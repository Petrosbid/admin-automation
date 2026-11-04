import re
from pathlib import Path
from aiogram import Router, F
from aiogram.types import Message, CallbackQuery, FSInputFile, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.utils.keyboard import InlineKeyboardBuilder

from core.config_loader import cfg
from core.queue_manager import queue_engine
from services.downloader_service import DownloaderService
from services.subtitle_service import SubtitleService
from services.mkv_service import MKVService
from services.handbrake_service import HandBrakeService
from bot.keyboards import (
    main_menu_kb, subtitle_folder_browser_kb, subtitle_source_kb,
    subtitle_file_found_kb, sync_options_kb, subtitle_mode_kb,
    skip_or_cancel_kb, presets_kb, output_destination_choice_kb,
    output_dest_tree_browser_kb
)

router = Router()


class SubtitleStates(StatesGroup):
    selecting_target_videos = State()
    waiting_fa_subtitle = State()
    waiting_en_subtitle = State()
    waiting_sync_choice = State()
    waiting_manual_sync_ms = State()
    waiting_custom_output_name = State()
    waiting_operation_mode = State()
    waiting_preset_choice = State()
    waiting_output_dest_choice = State()
    browsing_output_dest_dir = State()
    waiting_new_dest_subfolder_name = State()


def _natural_sort_key(path_str: str) -> list:
    name = Path(path_str).name.lower()
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", name)]


def _build_numbered_stem(pattern: str, orig_vid: Path, index_1based: int, total_count: int) -> str:
    """
    محاسبه نام فایل خروجی:
    - اگر کاربر '1' بفرستد -> همان نام اصلی فیلم
    - اگر چند فایل باشد و کاربر '*' بگذارد -> جایگزینی '*' با 01, 02, ...
    - اگر چند فایل باشد و '*' نگذارد -> افزودن 01, 02, ... به انتهای نام قبل از پسوند
    """
    if pattern == "1" or not pattern:
        return orig_vid.stem

    clean_pat = re.sub(r'[<>:"/\\|?]', "", pattern).strip()
    for ext in DownloaderService.VIDEO_EXTENSIONS:
        if clean_pat.lower().endswith(ext):
            clean_pat = clean_pat[:-len(ext)]
            break

    num_str = f"{index_1based:02d}"
    if "*" in clean_pat:
        return clean_pat.replace("*", num_str)
    elif total_count > 1:
        return f"{clean_pat}{num_str}"
    return clean_pat


def _resolve_media_dirs(video_path: Path, custom_base_stem: str) -> dict[str, Path]:
    """ساخت ساختار ۳ پوشه‌ای پیش‌فرض (softsub, hardsub, sub) فقط در حالت پیش‌فرض"""
    workspace = Path(cfg.get("paths", "workspace_dir")).resolve()
    vid_parent = video_path.resolve().parent

    if vid_parent.name.lower() in ("softsub", "hardsub", "sub", "raw_temp"):
        movie_base_dir = vid_parent.parent
    elif vid_parent == workspace:
        safe_name = re.sub(r'[<>:"/\\|?*]', "", custom_base_stem).strip().replace(" ", "_")
        movie_base_dir = workspace / (safe_name or "Media_Output")
    else:
        movie_base_dir = vid_parent

    folders = {
        "base": movie_base_dir,
        "softsub": movie_base_dir / "softsub",
        "hardsub": movie_base_dir / "hardsub",
        "sub": movie_base_dir / "sub",
    }
    for p in folders.values():
        p.mkdir(parents=True, exist_ok=True)
    return folders


def _get_sub_upload_dir_without_creating_3_folders(video_path: Path) -> Path:
    vid_parent = video_path.resolve().parent
    if (vid_parent / "sub").is_dir():
        return vid_parent / "sub"
    if vid_parent.name.lower() in ("softsub", "hardsub") and (vid_parent.parent / "sub").is_dir():
        return vid_parent.parent / "sub"
    return vid_parent


def _find_local_subtitles_for_video(video_path: Path) -> list[Path]:
    vid_parent = video_path.resolve().parent
    base_dir = vid_parent.parent if vid_parent.name.lower() in ("softsub", "hardsub", "sub") else vid_parent
    search_dirs = [base_dir / "sub", base_dir, vid_parent]
    found: list[Path] = []
    seen: set[str] = set()

    for d in search_dirs:
        if not d.exists():
            continue
        for f in sorted(d.iterdir(), key=lambda x: x.name.lower()):
            if (
                f.is_file()
                and f.suffix.lower() in (".srt", ".ass", ".ssa")
                and not f.name.startswith("_temp_")
                and not f.name.startswith("_soft_clean_")
            ):
                resolved_str = str(f.resolve())
                if resolved_str not in seen:
                    seen.add(resolved_str)
                    found.append(f.resolve())
    return found


def _format_current_filenames_prompt(data: dict) -> str:
    target_videos = [Path(p) for p in data.get("target_videos", [])]
    if len(target_videos) == 1:
        v_file = target_videos[0]
        mono_info = (
            f"📄 **نام فعلی فایل اصلی (برای کپی لمس کنید):**\n"
            f"`{v_file.name}`\n"
            f"🔹 نام بدون پسوند:\n"
            f"`{v_file.stem}`\n\n"
        )
        rule_hint = "✏️ **نام جدید فایل خروجی** را ارسال کنید (یا عدد `1` را بفرستید تا با همان نام فعلی ذخیره شود):"
    else:
        joined = "\n".join([f"`{i+1:02d}.` `{v.name}`" for i, v in enumerate(target_videos[:15])])
        mono_info = f"📄 **نام فعلی فایل‌های انتخاب شده ({len(target_videos)} فایل):**\n{joined}\n\n"
        rule_hint = (
            "✏️ **الگوی نام جدید** را ارسال کنید (یا عدد `1` برای حفظ نام‌های فعلی):\n"
            "• اگر در نام علامت `*` بگذارید (مثلاً `Series.S01E*.1080p`)، از `01` شماره‌گذاری می‌شود.\n"
            "• اگر `*` نگذارید، شماره‌های `01`، `02` و... در انتهای نام قبل از فرمت فایل اضافه می‌شوند:"
        )

    return f"{mono_info}{rule_hint}"


async def _prompt_fa_subtitle_step(msg_or_call: Message | CallbackQuery, state: FSMContext):
    data = await state.get_data()
    sorted_targets = sorted(data["target_videos"], key=_natural_sort_key)
    await state.update_data(target_videos=sorted_targets, custom_output_dir=None)

    first_video = Path(sorted_targets[0])
    local_subs = _find_local_subtitles_for_video(first_video)
    await state.update_data(local_sub_candidates=[str(p) for p in local_subs])
    await state.set_state(SubtitleStates.waiting_fa_subtitle)

    text = (
        f"🎬 **تعداد فیلم‌های انتخاب شده:** `{len(sorted_targets)}` فایل\n"
        f"📄 اولین فایل: `{first_video.name}`\n"
        f"📂 زیرنویس‌های موجود در پوشه فیلم / `sub`: **`{len(local_subs)}` فایل**\n\n"
        "🇮🇷 **مرحله ۱: زیرنویس فارسی**\n"
        "می‌توانید زیرنویس فارسی را از داخل پوشه انتخاب کنید، یا فایل جدیدی آپلود نمایید (یا رد کنید):"
    )
    kb = subtitle_source_kb("fa", len(local_subs))
    if isinstance(msg_or_call, CallbackQuery):
        await msg_or_call.message.edit_text(text, reply_markup=kb, parse_mode="Markdown")
    else:
        await msg_or_call.answer(text, reply_markup=kb, parse_mode="Markdown")


async def _prompt_en_subtitle_step(msg_or_call: Message | CallbackQuery, state: FSMContext, prefix_note: str = ""):
    data = await state.get_data()
    first_video = Path(data["target_videos"][0])
    local_subs = _find_local_subtitles_for_video(first_video)
    await state.update_data(local_sub_candidates=[str(p) for p in local_subs])
    await state.set_state(SubtitleStates.waiting_en_subtitle)

    text = (
        f"{prefix_note}"
        "🇬🇧 **مرحله ۲: زیرنویس انگلیسی**\n"
        "می‌توانید زیرنویس انگلیسی را از داخل پوشه انتخاب کنید، فایل جدید بفرستید و یا رد کنید\n"
        "*(در صورت رد کردن، اگر داخل خود فایل فیلم زیرنویس انگلیسی وجود داشته باشد، بدون هاردساب شدن و بدون زرد شدن به فایل اضافه می‌شود)*:"
    )
    kb = subtitle_source_kb("en", len(local_subs))
    if isinstance(msg_or_call, CallbackQuery):
        await msg_or_call.message.edit_text(text, reply_markup=kb, parse_mode="Markdown")
    else:
        await msg_or_call.answer(text, reply_markup=kb, parse_mode="Markdown")


# ==================== مرورگر پوشه‌به‌پوشه برای انتخاب فیلم‌ها ====================
async def _render_sub_folder_browser(call: CallbackQuery, state: FSMContext, browse_dir: Path):
    workspace = Path(cfg.get("paths", "workspace_dir")).resolve()
    browse_dir = browse_dir.resolve()
    browse_dir.mkdir(parents=True, exist_ok=True)

    folders = sorted(
        [p for p in browse_dir.iterdir() if p.is_dir() and not p.name.startswith("_") and p.name != "browser_profile"],
        key=lambda x: x.name.lower()
    )
    videos = DownloaderService.find_video_files(browse_dir, recursive=False)

    data = await state.get_data()
    selected_paths = set(data.get("selected_video_paths", []))

    await state.update_data(
        sub_browse_dir=str(browse_dir),
        sub_dir_folders=[str(f.resolve()) for f in folders],
        sub_dir_videos=[str(v.resolve()) for v in videos],
        selected_video_paths=list(selected_paths)
    )

    is_root = (browse_dir == workspace)

    full_lines = []
    for idx, folder in enumerate(folders[:20]):
        full_lines.append(f"`[{idx+1}]` 📁 `{folder.name}`")
    for idx, vid in enumerate(videos[:25]):
        is_sel = str(vid.resolve()) in selected_paths
        mark = "✅" if is_sel else "⬜"
        size_mb = vid.stat().st_size / (1024 * 1024)
        full_lines.append(f"{mark} `[F{idx+1}]` 🎬 `{vid.name}` _({size_mb:.1f} MB)_")

    details_block = "\n".join(full_lines) if full_lines else "_(در این مسیر پوشه یا فایل ویدئویی وجود ندارد)_"

    msg_text = (
        f"🎬 **انتخاب پوشه‌به‌پوشه فیلم‌ها برای زیرنویس / انکود**\n"
        f"📂 مسیر فعلی:\n`{browse_dir}`\n"
        f"✅ کل فایل‌های انتخاب شده: `{len(selected_paths)}` فایل\n\n"
        f"📋 **نام کامل پوشه‌ها و فایل‌های این مسیر:**\n{details_block}\n\n"
        "روی دکمه متناظر در زیر کلیک کنید:"
    )

    await call.message.edit_text(
        msg_text[:4000],
        reply_markup=subtitle_folder_browser_kb(folders, videos, selected_paths, is_root),
        parse_mode="Markdown"
    )


@router.callback_query(F.data == "menu:sub_to_files")
async def cb_sub_to_files_start(call: CallbackQuery, state: FSMContext):
    workspace = Path(cfg.get("paths", "workspace_dir")).resolve()
    await state.set_state(SubtitleStates.selecting_target_videos)
    await state.update_data(selected_video_paths=[], operation_mode=None, custom_output_dir=None)
    await _render_sub_folder_browser(call, state, workspace)


@router.callback_query(SubtitleStates.selecting_target_videos, F.data.startswith("subnav:dir:"))
async def cb_subnav_open_dir(call: CallbackQuery, state: FSMContext):
    idx = int(call.data.split(":")[2])
    data = await state.get_data()
    folders = data.get("sub_dir_folders", [])
    if idx < len(folders):
        await _render_sub_folder_browser(call, state, Path(folders[idx]))


@router.callback_query(SubtitleStates.selecting_target_videos, F.data == "subnav:up")
async def cb_subnav_up(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    curr = Path(data.get("sub_browse_dir", cfg.get("paths", "workspace_dir"))).resolve()
    workspace = Path(cfg.get("paths", "workspace_dir")).resolve()
    parent = curr.parent if curr != workspace else workspace
    await _render_sub_folder_browser(call, state, parent)


@router.callback_query(SubtitleStates.selecting_target_videos, F.data.startswith("subnav:vid:"))
async def cb_subnav_toggle_video(call: CallbackQuery, state: FSMContext):
    idx = int(call.data.split(":")[2])
    data = await state.get_data()
    videos = data.get("sub_dir_videos", [])
    if idx >= len(videos):
        return
    vid_str = str(Path(videos[idx]).resolve())
    selected_paths = set(data.get("selected_video_paths", []))

    if vid_str in selected_paths:
        selected_paths.remove(vid_str)
    else:
        selected_paths.add(vid_str)

    await state.update_data(selected_video_paths=list(selected_paths))
    curr = Path(data.get("sub_browse_dir", cfg.get("paths", "workspace_dir")))
    await _render_sub_folder_browser(call, state, curr)


@router.callback_query(SubtitleStates.selecting_target_videos, F.data == "subnav:sel_all")
async def cb_subnav_select_all_in_folder(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    videos = [str(Path(v).resolve()) for v in data.get("sub_dir_videos", [])]
    selected_paths = set(data.get("selected_video_paths", []))

    if all(v in selected_paths for v in videos):
        for v in videos:
            selected_paths.discard(v)
    else:
        for v in videos:
            selected_paths.add(v)

    await state.update_data(selected_video_paths=list(selected_paths))
    curr = Path(data.get("sub_browse_dir", cfg.get("paths", "workspace_dir")))
    await _render_sub_folder_browser(call, state, curr)


@router.callback_query(SubtitleStates.selecting_target_videos, F.data == "mvact:proceed")
async def cb_proceed_to_subs(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    sel = sorted(data.get("selected_video_paths", []), key=_natural_sort_key)
    if not sel:
        return await call.answer("⚠️ حداقل یک فایل ویدئویی را از داخل پوشه‌ها تیک بزنید!", show_alert=True)

    await state.update_data(target_videos=sel, fa_sub_path=None, en_sub_path=None)
    await _prompt_fa_subtitle_step(call, state)


# ==================== اسکن، انتخاب از پوشه یا آپلود زیرنویس فارسی و انگلیسی ====================
@router.callback_query(F.data.startswith("subscan:"))
async def cb_scan_directory_for_subs(call: CallbackQuery, state: FSMContext):
    lang = call.data.split(":")[1]
    data = await state.get_data()
    first_video = Path(data["target_videos"][0])
    local_subs = _find_local_subtitles_for_video(first_video)
    await state.update_data(local_sub_candidates=[str(p) for p in local_subs])

    lang_label = "فارسی" if lang == "fa" else "انگلیسی"
    if not local_subs:
        return await call.answer(
            "❌ هیچ فایل زیرنویسی (.srt / .ass) در پوشه sub یا کنار فیلم پیدا نشد! لطفاً گزینه آپلود را بزنید.",
            show_alert=True
        )

    sub_lines = [f"`[{i+1}]` 📄 `{p.name}`" for i, p in enumerate(local_subs[:20])]
    full_subs_block = "\n".join(sub_lines)

    await call.message.edit_text(
        f"📂 **لیست کامل زیرنویس‌های موجود در پوشه فیلم و `sub/`:**\n"
        f"{full_subs_block}\n\n"
        f"کدام فایل به عنوان **زیرنویس {lang_label}** استفاده شود؟",
        reply_markup=subtitle_file_found_kb(lang, local_subs),
        parse_mode="Markdown"
    )


@router.callback_query(F.data.startswith("subfile:"))
async def cb_select_existing_sub_file(call: CallbackQuery, state: FSMContext):
    _, lang, idx_str = call.data.split(":")
    idx = int(idx_str)
    data = await state.get_data()
    candidates = data.get("local_sub_candidates", [])
    if idx >= len(candidates):
        return await call.answer("⚠️ فایل یافت نشد.", show_alert=True)

    chosen_sub = Path(candidates[idx])
    if chosen_sub.suffix.lower() == ".srt":
        SubtitleService.normalize_srt_utf8(chosen_sub)

    if lang == "fa":
        await state.update_data(fa_sub_path=str(chosen_sub))
        await _prompt_en_subtitle_step(
            call, state,
            prefix_note=f"✅ زیرنویس فارسی انتخاب شد:\n`{chosen_sub.name}`\n\n"
        )
    else:
        await state.update_data(en_sub_path=str(chosen_sub))
        await state.set_state(SubtitleStates.waiting_sync_choice)
        await call.message.edit_text(
            f"✅ زیرنویس انگلیسی انتخاب شد:\n`{chosen_sub.name}`\n\n"
            "🔄 **مرحله سینک (هماهنگ‌سازی) زیرنویس قبل از پردازش:**\n"
            "چگونه می‌خواهید تایمینگ زیرنویس با فیلم سینک شود؟",
            reply_markup=sync_options_kb(),
            parse_mode="Markdown"
        )


@router.callback_query(F.data.startswith("subupload:"))
async def cb_ask_upload_sub(call: CallbackQuery, state: FSMContext):
    lang = call.data.split(":")[1]
    if lang == "fa":
        await state.set_state(SubtitleStates.waiting_fa_subtitle)
        await call.message.edit_text(
            "📤 لطفاً **فایل زیرنویس فارسی (`.srt` یا `.ass`)** را در چت ارسال کنید:",
            reply_markup=skip_or_cancel_kb("skip:fa_sub"),
            parse_mode="Markdown"
        )
    else:
        await state.set_state(SubtitleStates.waiting_en_subtitle)
        await call.message.edit_text(
            "📤 لطفاً **فایل زیرنویس انگلیسی (`.srt` یا `.ass`)** را در چت ارسال کنید:",
            reply_markup=skip_or_cancel_kb("skip:en_sub"),
            parse_mode="Markdown"
        )


@router.callback_query(SubtitleStates.waiting_fa_subtitle, F.data == "skip:fa_sub")
async def cb_skip_fa_sub(call: CallbackQuery, state: FSMContext):
    await _prompt_en_subtitle_step(call, state, prefix_note="⏭ زیرنویس فارسی رد شد.\n\n")


@router.message(SubtitleStates.waiting_fa_subtitle, F.document)
async def handle_fa_sub_upload(message: Message, state: FSMContext):
    data = await state.get_data()
    first_video = Path(data["target_videos"][0])
    sub_dir = _get_sub_upload_dir_without_creating_3_folders(first_video)
    sub_dir.mkdir(parents=True, exist_ok=True)
    fa_dest = sub_dir / f"FA_{message.document.file_name}"
    await message.bot.download(message.document, destination=fa_dest)

    if fa_dest.suffix.lower() == ".srt":
        SubtitleService.normalize_srt_utf8(fa_dest)

    await state.update_data(fa_sub_path=str(fa_dest))
    await _prompt_en_subtitle_step(
        message, state,
        prefix_note=f"✅ زیرنویس فارسی دریافت شد:\n`{fa_dest.name}`\n\n"
    )


@router.callback_query(SubtitleStates.waiting_en_subtitle, F.data == "skip:en_sub")
async def cb_skip_en_sub(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    has_fa = bool(data.get("fa_sub_path"))
    has_en = bool(data.get("en_sub_path"))
    preselected_mode = data.get("operation_mode")

    if not has_fa and not has_en:
        if preselected_mode == "softsub":
            return await call.answer("⚠️ برای عملیات سافت‌ساب حداقل یک زیرنویس باید انتخاب شود!", show_alert=True)
        await state.update_data(sync_mode="none", sync_offset_ms=0, operation_mode="hardsub")
        await state.set_state(SubtitleStates.waiting_custom_output_name)
        return await call.message.edit_text(
            "ℹ️ **انکود با HandBrake بدون زیرنویس فارسی** (در صورت وجود زیرنویس انگلیسی در خود فیلم، بدون Burn-in به فایل اضافه می‌شود).\n\n"
            f"{_format_current_filenames_prompt(data)}",
            parse_mode="Markdown"
        )

    await state.set_state(SubtitleStates.waiting_sync_choice)
    await call.message.edit_text(
        "🔄 **مرحله سینک (هماهنگ‌سازی) زیرنویس قبل از پردازش:**\n"
        "چگونه می‌خواهید تایمینگ زیرنویس با فیلم سینک شود؟",
        reply_markup=sync_options_kb(),
        parse_mode="Markdown"
    )


@router.message(SubtitleStates.waiting_en_subtitle, F.document)
async def handle_en_sub_upload(message: Message, state: FSMContext):
    data = await state.get_data()
    first_video = Path(data["target_videos"][0])
    sub_dir = _get_sub_upload_dir_without_creating_3_folders(first_video)
    sub_dir.mkdir(parents=True, exist_ok=True)
    en_dest = sub_dir / f"EN_{message.document.file_name}"
    await message.bot.download(message.document, destination=en_dest)

    if en_dest.suffix.lower() == ".srt":
        SubtitleService.normalize_srt_utf8(en_dest)

    await state.update_data(en_sub_path=str(en_dest))
    await state.set_state(SubtitleStates.waiting_sync_choice)
    await message.answer(
        "✅ زیرنویس **انگلیسی** نیز دریافت شد.\n\n"
        "🔄 **مرحله سینک (هماهنگ‌سازی) زیرنویس قبل از پردازش:**\n"
        "چگونه می‌خواهید تایمینگ زیرنویس فارسی با فیلم سینک شود؟",
        reply_markup=sync_options_kb(),
        parse_mode="Markdown"
    )


@router.callback_query(SubtitleStates.waiting_sync_choice, F.data.startswith("syncopt:"))
async def cb_sync_option_selected(call: CallbackQuery, state: FSMContext):
    sync_opt = call.data.split(":")[1]
    await state.update_data(sync_mode=sync_opt, sync_offset_ms=0)

    if sync_opt == "manual":
        await state.set_state(SubtitleStates.waiting_manual_sync_ms)
        return await call.message.edit_text(
            "⏱ **شیفت زمانی دستی زیرنویس (به میلی‌ثانیه):**\n\n"
            "عدد مورد نظر را وارد کنید:\n"
            "• مثلاً `+1500` یعنی زیرنویس **۱.۵ ثانیه دیرتر** نمایش داده شود.\n"
            "• مثلاً `-800` یعنی زیرنویس **۰.۸ ثانیه زودتر** نمایش داده شود.",
            parse_mode="Markdown"
        )

    data = await state.get_data()
    await state.set_state(SubtitleStates.waiting_custom_output_name)
    await call.message.edit_text(
        _format_current_filenames_prompt(data),
        parse_mode="Markdown"
    )


@router.message(SubtitleStates.waiting_manual_sync_ms)
async def handle_manual_sync_ms(message: Message, state: FSMContext):
    try:
        offset_ms = int(message.text.strip().replace("+", ""))
    except ValueError:
        return await message.answer("⚠️ لطفاً یک عدد صحیح به میلی‌ثانیه وارد کنید (مثلاً `1500` یا `-1200`):", parse_mode="Markdown")

    await state.update_data(sync_offset_ms=offset_ms)
    data = await state.get_data()
    await state.set_state(SubtitleStates.waiting_custom_output_name)
    await message.answer(
        f"✅ شیفت زمانی `{offset_ms} ms` ثبت شد.\n\n"
        f"{_format_current_filenames_prompt(data)}",
        parse_mode="Markdown"
    )


async def _ask_output_destination_choice(msg_or_call: Message | CallbackQuery, state: FSMContext):
    """پرسیدن نحوه انتخاب پوشه مقصد برای سافت‌ساب و هاردساب"""
    await state.set_state(SubtitleStates.waiting_output_dest_choice)
    text = (
        "📂 **انتخاب پوشه مقصد برای ذخیره خروجی:**\n\n"
        "• **حالت پیش‌فرض:** ساخت خودکار پوشه‌های `softsub` و `hardsub`.\n"
        "• **کنار خود فیلم:** ذخیره مستقیم در همان پوشه فعلی فیلم (بدون ساخت ۳ پوشه).\n"
        "• **انتخاب پوشه مقصد دلخواه:** مرور تمام پوشه‌ها و زیرپوشه‌ها یا ساخت پوشه جدید در هر مسیر دلخواه (بدون ساخت ۳ پوشه):"
    )
    kb = output_destination_choice_kb()
    if isinstance(msg_or_call, CallbackQuery):
        await msg_or_call.message.edit_text(text, reply_markup=kb, parse_mode="Markdown")
    else:
        await msg_or_call.answer(text, reply_markup=kb, parse_mode="Markdown")


@router.message(SubtitleStates.waiting_custom_output_name)
async def handle_custom_output_name(message: Message, state: FSMContext):
    await state.update_data(custom_output_name=message.text.strip())
    data = await state.get_data()

    preselected_mode = data.get("operation_mode")
    if preselected_mode:
        if preselected_mode in ("hardsub", "both"):
            await state.set_state(SubtitleStates.waiting_preset_choice)
            return await message.answer(
                "🔥 لطفاً **پریست HandBrake** را از لیست زیر انتخاب کنید:",
                reply_markup=presets_kb(),
                parse_mode="Markdown"
            )
        else:
            await state.update_data(chosen_preset=None)
            return await _ask_output_destination_choice(message, state)

    await state.set_state(SubtitleStates.waiting_operation_mode)
    brand = cfg.get("branding", "global_title_and_track_name", ".::EmpireBestTv.Com::.")
    await message.answer(
        f"⚙️ تمام Track Name ها و File Title روی `{brand}` تنظیم شدند.\n"
        "حالا نوع عملیات را انتخاب کنید:",
        reply_markup=subtitle_mode_kb(),
        parse_mode="Markdown"
    )


@router.callback_query(SubtitleStates.waiting_operation_mode, F.data.startswith("submode:"))
async def cb_select_operation_mode(call: CallbackQuery, state: FSMContext):
    mode = call.data.split(":")[1]
    await state.update_data(operation_mode=mode)

    if mode in ("hardsub", "both"):
        await state.set_state(SubtitleStates.waiting_preset_choice)
        return await call.message.edit_text(
            "🔥 لطفاً **پریست HandBrake** را از لیست زیر برای هاردساب انتخاب کنید:",
            reply_markup=presets_kb(),
            parse_mode="Markdown"
        )

    await state.update_data(chosen_preset=None)
    await _ask_output_destination_choice(call, state)


@router.callback_query(SubtitleStates.waiting_preset_choice, F.data.startswith("preset:"))
async def cb_select_preset_for_jobs(call: CallbackQuery, state: FSMContext):
    idx = int(call.data.split(":")[1])
    presets = HandBrakeService.list_available_presets()
    chosen_preset = presets[idx]
    await state.update_data(chosen_preset=chosen_preset)
    await _ask_output_destination_choice(call, state)


# ==================== مرورگر درختی پوشه‌ها و زیرپوشه‌ها + ساخت پوشه جدید برای مقصد خروجی ====================
async def _render_output_dest_browser(call_or_msg: CallbackQuery | Message, state: FSMContext, current_dir: Path):
    workspace = Path(cfg.get("paths", "workspace_dir")).resolve()
    current_dir = current_dir.resolve()
    current_dir.mkdir(parents=True, exist_ok=True)

    subfolders = sorted(
        [p for p in current_dir.iterdir() if p.is_dir() and not p.name.startswith("_") and p.name != "browser_profile"],
        key=lambda x: x.name.lower()
    )
    await state.set_state(SubtitleStates.browsing_output_dest_dir)
    await state.update_data(
        dest_browser_curr_dir=str(current_dir),
        dest_browser_subfolders=[str(f.resolve()) for f in subfolders]
    )

    is_root = (current_dir == workspace)
    folder_lines = [f"`[{i+1}]` 📁 `{f.name}`" for i, f in enumerate(subfolders[:22])]
    folders_block = "\n".join(folder_lines) if folder_lines else "_(زیرپوشه‌ای در این مسیر وجود ندارد)_"

    text = (
        "📁 **انتخاب پوشه مقصد خروجی (مرورگر پوشه و زیرپوشه)**\n\n"
        f"📍 **مسیر فعلی باز شده:**\n`{current_dir}`\n\n"
        f"📂 **زیرپوشه‌های موجود در این مسیر:**\n{folders_block}\n\n"
        "• برای ذخیره در همین مسیر فعلی، دکمه **«✅ ذخیره در همین پوشه»** را بزنید.\n"
        "• برای رفتن به زیرپوشه‌های بعدی، روی نام زیرپوشه کلیک کنید و یا در همینجا پوشه جدید بسازید:"
    )
    kb = output_dest_tree_browser_kb(subfolders, current_dir, is_root)

    if isinstance(call_or_msg, CallbackQuery):
        await call_or_msg.message.edit_text(text[:4000], reply_markup=kb, parse_mode="Markdown")
    else:
        await call_or_msg.answer(text[:4000], reply_markup=kb, parse_mode="Markdown")


@router.callback_query(SubtitleStates.waiting_output_dest_choice, F.data == "outdest:default")
async def cb_outdest_default(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    await state.update_data(custom_output_dir=None)
    await _schedule_subtitle_jobs(call, state, chosen_preset=data.get("chosen_preset"))


@router.callback_query(SubtitleStates.waiting_output_dest_choice, F.data == "outdest:same_dir")
async def cb_outdest_same_dir(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    first_vid = Path(data["target_videos"][0]).resolve()
    await state.update_data(custom_output_dir=str(first_vid.parent))
    await _schedule_subtitle_jobs(call, state, chosen_preset=data.get("chosen_preset"))


@router.callback_query(SubtitleStates.waiting_output_dest_choice, F.data == "outdest:browse")
async def cb_outdest_start_browse(call: CallbackQuery, state: FSMContext):
    workspace = Path(cfg.get("paths", "workspace_dir")).resolve()
    await _render_output_dest_browser(call, state, workspace)


@router.callback_query(SubtitleStates.browsing_output_dest_dir, F.data.startswith("destnav:open:"))
async def cb_destnav_open_subfolder(call: CallbackQuery, state: FSMContext):
    idx = int(call.data.split(":")[2])
    data = await state.get_data()
    subfolders = data.get("dest_browser_subfolders", [])
    if idx < len(subfolders):
        await _render_output_dest_browser(call, state, Path(subfolders[idx]))


@router.callback_query(SubtitleStates.browsing_output_dest_dir, F.data == "destnav:up")
async def cb_destnav_up(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    curr = Path(data.get("dest_browser_curr_dir", cfg.get("paths", "workspace_dir"))).resolve()
    workspace = Path(cfg.get("paths", "workspace_dir")).resolve()
    parent = curr.parent if curr != workspace else workspace
    await _render_output_dest_browser(call, state, parent)


@router.callback_query(SubtitleStates.browsing_output_dest_dir, F.data == "destnav:select_here")
async def cb_destnav_select_here(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    curr_dir = data.get("dest_browser_curr_dir", cfg.get("paths", "workspace_dir"))
    await state.update_data(custom_output_dir=curr_dir)
    await _schedule_subtitle_jobs(call, state, chosen_preset=data.get("chosen_preset"))


@router.callback_query(SubtitleStates.browsing_output_dest_dir, F.data == "destnav:mkdir_here")
async def cb_destnav_mkdir_here(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    curr_dir = data.get("dest_browser_curr_dir", cfg.get("paths", "workspace_dir"))
    await state.set_state(SubtitleStates.waiting_new_dest_subfolder_name)

    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text="🔙 بازگشت به مرورگر پوشه", callback_data="outdest:browse_resume"))
    await call.message.edit_text(
        f"➕ **ساخت پوشه جدید در مسیر:**\n`{curr_dir}`\n\n"
        "✏️ لطفاً **نام پوشه جدید** را ارسال کنید (این پوشه بدون ۳ پوشه اضافی ساخته می‌شود و مستقیماً وارد آن می‌شوید):",
        reply_markup=kb.as_markup(),
        parse_mode="Markdown"
    )


@router.callback_query(F.data == "outdest:browse_resume")
async def cb_outdest_browse_resume(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    curr_dir = Path(data.get("dest_browser_curr_dir", cfg.get("paths", "workspace_dir")))
    await _render_output_dest_browser(call, state, curr_dir)


@router.message(SubtitleStates.waiting_new_dest_subfolder_name)
async def handle_new_dest_subfolder_name(message: Message, state: FSMContext):
    raw_name = re.sub(r'[<>:"/\\|?*]', "", (message.text or "").strip()).strip()
    if not raw_name:
        return await message.answer("⚠️ نام پوشه معتبر نیست. لطفاً دوباره ارسال کنید:")

    data = await state.get_data()
    curr_dir = Path(data.get("dest_browser_curr_dir", cfg.get("paths", "workspace_dir"))).resolve()
    new_dir = curr_dir / raw_name
    new_dir.mkdir(parents=True, exist_ok=True)

    await message.answer(f"✅ پوشه `{new_dir.name}` ساخته شد و وارد آن شدید!", parse_mode="Markdown")
    await _render_output_dest_browser(message, state, new_dir)


async def _build_and_enqueue_jobs(data: dict, chosen_preset: dict | None) -> list[str]:
    target_videos = [Path(p) for p in data["target_videos"]]
    fa_sub = Path(data["fa_sub_path"]) if data.get("fa_sub_path") else None
    en_sub = Path(data["en_sub_path"]) if data.get("en_sub_path") else None
    custom_name = data.get("custom_output_name", "1")
    mode = data.get("operation_mode", "softsub")
    sync_mode = data.get("sync_mode", "none")
    sync_offset_ms = int(data.get("sync_offset_ms", 0))
    custom_output_dir = data.get("custom_output_dir")
    site_tag = cfg.get("branding", "site_tag", "EmpireBestTv")
    brand = cfg.get("branding", "global_title_and_track_name", ".::EmpireBestTv.Com::.")

    scheduled_ids = []
    total_vids = len(target_videos)

    for idx, vid in enumerate(target_videos, start=1):
        base_stem = _build_numbered_stem(custom_name, vid, idx, total_vids)

        # اگر کاربر پوشه مقصد مشخص کرده باشد، دیگر ۳ پوشه (softsub/hardsub/sub) ساخته نمی‌شود
        if custom_output_dir:
            out_dir = Path(custom_output_dir)
            out_dir.mkdir(parents=True, exist_ok=True)
            soft_dir = out_dir
            hard_dir = out_dir
        else:
            dirs = _resolve_media_dirs(vid, base_stem)
            soft_dir = dirs["softsub"]
            hard_dir = dirs["hardsub"]

        if mode in ("softsub", "both") and (fa_sub or en_sub):
            soft_out = soft_dir / f"{base_stem}.SoftSub.{site_tag}.mkv"
            t_soft = await queue_engine.add_task(
                "mkvtoolnix",
                f"SoftSub: {soft_out.name}",
                MKVService.merge_softsub,
                vid,
                soft_out,
                fa_sub,
                en_sub,
                True,
                brand,
                sync_mode,
                sync_offset_ms
            )
            scheduled_ids.append(f"📎 SoftSub (`{soft_out.name}` در `{soft_dir.name}/`): `{t_soft.task_id}`")

        if mode in ("hardsub", "both") and chosen_preset:
            hard_out = hard_dir / f"{base_stem}.HardSub.{site_tag}.mp4"
            primary_sub = fa_sub
            secondary_sub = en_sub
            t_hard = await queue_engine.add_task(
                "handbrake",
                f"HardSub ({chosen_preset['name']}): {hard_out.name}",
                HandBrakeService.encode_hardsub,
                vid,
                primary_sub,
                hard_out,
                chosen_preset["file"],
                chosen_preset["name"],
                secondary_sub,
                sync_mode,
                sync_offset_ms
            )
            scheduled_ids.append(f"🔥 HardSub (`{hard_out.name}` در `{hard_dir.name}/`): `{t_hard.task_id}`")

    return scheduled_ids


async def _schedule_subtitle_jobs(call: CallbackQuery, state: FSMContext, chosen_preset: dict | None):
    data = await state.get_data()
    brand = cfg.get("branding", "global_title_and_track_name", ".::EmpireBestTv.Com::.")
    scheduled_ids = await _build_and_enqueue_jobs(data, chosen_preset)
    await state.clear()
    summary_text = "\n".join(scheduled_ids)
    await call.message.edit_text(
        f"✅ عملیات با تگ `{brand}` در صف قرار گرفت!\n\n{summary_text}",
        reply_markup=main_menu_kb(),
        parse_mode="Markdown"
    )


async def _schedule_subtitle_jobs_from_msg(message: Message, state: FSMContext, chosen_preset: dict | None):
    data = await state.get_data()
    brand = cfg.get("branding", "global_title_and_track_name", ".::EmpireBestTv.Com::.")
    scheduled_ids = await _build_and_enqueue_jobs(data, chosen_preset)
    await state.clear()
    summary_text = "\n".join(scheduled_ids)
    await message.answer(
        f"✅ عملیات با تگ `{brand}` در صف قرار گرفت!\n\n{summary_text}",
        reply_markup=main_menu_kb(),
        parse_mode="Markdown"
    )


# ==================== اکشن‌های زیرنویس از داخل فایل‌منیجر ====================
@router.callback_query(F.data == "vact:extract_sub")
async def cb_extract_sub_list(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    sel_files = data.get("fm_selected_files", [])
    video_path = Path(sel_files[0]) if sel_files else Path(data["selected_video"])
    tracks = await MKVService.list_subtitle_tracks(video_path)
    if not tracks:
        return await call.answer("❌ هیچ زیرنویسی داخل این فایل پیدا نشد!", show_alert=True)

    kb = InlineKeyboardBuilder()
    for t in tracks:
        kb.button(
            text=f"Track #{t['id']} | {t['language']} | {t['track_name']} ({t['codec']})",
            callback_data=f"exttrack:{t['id']}"
        )
    kb.adjust(1)
    await call.message.edit_text("📤 کدام ترک زیرنویس استخراج شود؟", reply_markup=kb.as_markup())


@router.callback_query(F.data.startswith("exttrack:"))
async def cb_extract_sub_run(call: CallbackQuery, state: FSMContext):
    track_id = int(call.data.split(":")[1])
    data = await state.get_data()
    sel_files = data.get("fm_selected_files", [])
    video_path = Path(sel_files[0]) if sel_files else Path(data["selected_video"])
    out_srt = video_path.with_name(f"{video_path.stem}_track{track_id}.srt")

    await call.message.edit_text("⏳ در حال استخراج زیرنویس با `mkvextract`...", parse_mode="Markdown")
    await MKVService.extract_subtitle_track(video_path, track_id, out_srt)
    await call.message.answer_document(
        FSInputFile(out_srt),
        caption=f"✅ زیرنویس استخراج شد:\n`{out_srt.name}`",
        parse_mode="Markdown"
    )


@router.callback_query(F.data.in_({"vact:softsub", "vact:hardsub", "vact:full_auto", "fact:batch_sub"}))
async def cb_single_or_batch_video_sub_flow(call: CallbackQuery, state: FSMContext):
    mode_map = {
        "vact:softsub": "softsub",
        "vact:hardsub": "hardsub",
        "vact:full_auto": "both",
        "fact:batch_sub": None,
    }
    data = await state.get_data()
    if call.data == "fact:batch_sub":
        sel_files = sorted(data.get("fm_selected_files", []), key=_natural_sort_key)
        videos = [p for p in sel_files if Path(p).suffix.lower() in DownloaderService.VIDEO_EXTENSIONS]
        if not videos:
            return await call.answer("⚠️ هیچ فایل ویدئویی در بین فایل‌های انتخاب‌شده وجود ندارد!", show_alert=True)
    else:
        videos = [data["selected_video"]]

    await state.update_data(
        target_videos=videos,
        operation_mode=mode_map[call.data],
        fa_sub_path=None,
        en_sub_path=None,
        custom_output_dir=None
    )
    await _prompt_fa_subtitle_step(call, state)