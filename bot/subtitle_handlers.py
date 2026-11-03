from pathlib import Path
from aiogram.types import FSInputFile

from core.queue_manager import queue_engine

from services.mkv_service import MKVService
from aiogram import Router, F
from aiogram.types import (
    Message,
    CallbackQuery
)

from aiogram.fsm.context import FSMContext

from core.config_loader import cfg

from services.downloader_service import DownloaderService
from services.subtitle_service import SubtitleService

from bot.handlers import BotStates

from bot.keyboards import (
    main_menu_kb,
    multi_video_select_kb,
    subtitle_source_kb,
    subtitle_file_found_kb,
    sync_options_kb
)


router = Router(name="subtitle_management")


# ==================================================
# HELPERS
# ==================================================

def _full_video_list(videos: list[Path]) -> str:

    lines = [
        "🎬 <b>فایل‌های ویدئویی موجود</b>",
        "",
        "نام کامل فایل‌ها:"
    ]

    for idx, video in enumerate(videos[:25], 1):

        size_mb = video.stat().st_size / (
            1024 * 1024
        )

        lines.append(
            f"\n<b>{idx}.</b> <code>{video.name}</code>\n"
            f"📂 {video.parent}\n"
            f"📦 {size_mb:.1f} MB"
        )

    lines.extend([
        "",
        "برای انتخاب، روی دکمه فایل مورد نظر کلیک کنید.",
        "امکان انتخاب چند فایل وجود دارد."
    ])

    return "\n".join(lines)


def _subtitle_destination(
    video_path: Path
) -> Path:

    sub_dir = video_path.parent / "sub"

    sub_dir.mkdir(
        parents=True,
        exist_ok=True
    )

    return sub_dir


async def _ask_fa_source(
    message: Message,
    state: FSMContext
):

    await state.set_state(
        BotStates.waiting_fa_subtitle
    )

    data = await state.get_data()

    video_names = [
        Path(path).name
        for path in data.get("target_videos", [])
    ]

    text = (
        "🇮🇷 <b>زیرنویس فارسی</b>\n\n"
        "آیا زیرنویس فارسی در دایرکتوری فیلم وجود دارد؟\n\n"
        "📂 ابتدا پوشه <code>sub</code> و سپس پوشه اصلی فیلم بررسی می‌شود.\n\n"
    )

    if video_names:

        text += (
            "🎬 فایل مرجع:\n"
            f"<code>{video_names[0]}</code>\n\n"
        )

    text += "یکی از گزینه‌های زیر را انتخاب کنید:"

    await message.answer(
        text,
        reply_markup=subtitle_source_kb("fa"),
        parse_mode="HTML"
    )


async def _ask_en_source(
    message: Message,
    state: FSMContext
):

    await state.set_state(
        BotStates.waiting_en_subtitle
    )

    data = await state.get_data()

    video_names = [
        Path(path).name
        for path in data.get("target_videos", [])
    ]

    text = (
        "🇬🇧 <b>زیرنویس انگلیسی</b>\n\n"
        "آیا زیرنویس انگلیسی در دایرکتوری فیلم وجود دارد؟\n\n"
        "📂 ابتدا پوشه <code>sub</code> و سپس پوشه اصلی فیلم بررسی می‌شود.\n\n"
    )

    if video_names:

        text += (
            "🎬 فایل مرجع:\n"
            f"<code>{video_names[0]}</code>\n\n"
        )

    text += "یکی از گزینه‌های زیر را انتخاب کنید:"

    await message.answer(
        text,
        reply_markup=subtitle_source_kb("en"),
        parse_mode="HTML"
    )


async def _start_sync(
    message: Message,
    state: FSMContext
):

    data = await state.get_data()

    if not data.get("fa_sub_path") and not data.get("en_sub_path"):

        await message.answer(
            "⚠️ حداقل یکی از زیرنویس‌های فارسی یا انگلیسی باید موجود باشد."
        )

        return await _ask_en_source(
            message,
            state
        )

    await state.set_state(
        BotStates.waiting_sync_choice
    )

    await message.answer(
        "🔄 <b>مرحله هماهنگ‌سازی زیرنویس</b>\n\n"
        "چگونه می‌خواهید زمان‌بندی زیرنویس فارسی با فیلم هماهنگ شود؟",
        reply_markup=sync_options_kb(),
        parse_mode="HTML"
    )


async def _save_uploaded_subtitle(
    message: Message,
    state: FSMContext,
    language: str
):

    data = await state.get_data()

    target_videos = data.get(
        "target_videos",
        []
    )

    if not target_videos:

        return await message.answer(
            "❌ فایل ویدئویی انتخاب نشده است."
        )

    video_path = Path(
        target_videos[0]
    )

    original_name = (
        message.document.file_name or "subtitle.srt"
    )

    original_name = Path(original_name).name

    suffix = Path(original_name).suffix.lower()

    if suffix not in (
        ".srt",
        ".ass",
        ".ssa"
    ):

        return await message.answer(
            "⚠️ فقط فایل‌های SRT، ASS و SSA پشتیبانی می‌شوند."
        )

    prefix = (
        "FA_"
        if language == "fa"
        else "EN_"
    )

    sub_dir = _subtitle_destination(
        video_path
    )

    destination = sub_dir / (
        prefix + original_name
    )

    await message.bot.download(
        message.document,
        destination=destination
    )

    if suffix == ".srt":

        SubtitleService.normalize_srt_utf8(
            destination
        )

    await state.update_data(
        **{
            f"{language}_sub_path": str(destination)
        }
    )

    await message.answer(
        f"✅ زیرنویس دریافت و ذخیره شد.\n\n"
        f"📄 فایل: <code>{destination.name}</code>\n"
        f"📂 مسیر: <code>{destination.parent}</code>",
        parse_mode="HTML"
    )

    if language == "fa":

        await _ask_en_source(
            message,
            state
        )

    else:

        await _start_sync(
            message,
            state
        )


# ==================================================
# START MULTI VIDEO SELECTION
# ==================================================

@router.callback_query(
    F.data == "menu:sub_to_files"
)
async def start_video_selection(
    call: CallbackQuery,
    state: FSMContext
):

    workspace = Path(
        cfg.get("paths", "workspace_dir")
    )

    videos = DownloaderService.find_video_files(
        workspace
    )

    if not videos:

        return await call.answer(
            "هیچ فایل ویدئویی در Workspace پیدا نشد.",
            show_alert=True
        )

    videos = videos[:25]

    await state.clear()

    await state.set_state(
        BotStates.selecting_target_videos
    )

    await state.update_data(
        all_videos=[
            str(video)
            for video in videos
        ],
        selected_indices=[],
        operation_mode=None
    )

    await call.message.edit_text(
        _full_video_list(videos),
        reply_markup=multi_video_select_kb(
            videos,
            set()
        ),
        parse_mode="HTML"
    )

    await call.answer()


# ==================================================
# TOGGLE VIDEO SELECTION
# ==================================================

@router.callback_query(
    BotStates.selecting_target_videos,
    F.data.startswith("mvsel:")
)
async def toggle_video_selection(
    call: CallbackQuery,
    state: FSMContext
):

    idx = int(
        call.data.split(":")[1]
    )

    data = await state.get_data()

    videos = [
        Path(path)
        for path in data.get("all_videos", [])
    ]

    if idx < 0 or idx >= len(videos):

        return await call.answer(
            "فایل انتخاب شده معتبر نیست.",
            show_alert=True
        )

    selected = set(
        data.get("selected_indices", [])
    )

    if idx in selected:

        selected.remove(idx)

    else:

        selected.add(idx)

    await state.update_data(
        selected_indices=list(selected)
    )

    await call.message.edit_reply_markup(
        reply_markup=multi_video_select_kb(
            videos,
            selected
        )
    )

    await call.answer(
        f"{len(selected)} فایل انتخاب شده"
    )


# ==================================================
# CONTINUE TO SUBTITLE
# ==================================================

@router.callback_query(
    BotStates.selecting_target_videos,
    F.data == "mvact:proceed"
)
async def proceed_to_subtitles(
    call: CallbackQuery,
    state: FSMContext
):

    data = await state.get_data()

    selected = data.get(
        "selected_indices",
        []
    )

    if not selected:

        return await call.answer(
            "حداقل یک فیلم انتخاب کنید.",
            show_alert=True
        )

    all_videos = data.get(
        "all_videos",
        []
    )

    target_videos = [
        all_videos[idx]
        for idx in sorted(selected)
    ]

    await state.update_data(
        target_videos=target_videos,
        fa_sub_path=None,
        en_sub_path=None
    )

    await call.answer()

    await call.message.edit_text(
        f"✅ <b>{len(target_videos)}</b> فایل انتخاب شد.",
        parse_mode="HTML"
    )

    await _ask_fa_source(
        call.message,
        state
    )


# ==================================================
# SINGLE VIDEO ACTIONS
# ==================================================

@router.callback_query(
    F.data.in_({
        "vact:softsub",
        "vact:hardsub",
        "vact:full_auto"
    })
)
async def single_video_subtitle_flow(
    call: CallbackQuery,
    state: FSMContext
):

    mode_map = {
        "vact:softsub": "softsub",
        "vact:hardsub": "hardsub",
        "vact:full_auto": "both"
    }

    data = await state.get_data()

    video = data.get(
        "selected_video"
    )

    if not video:

        return await call.answer(
            "فایل ویدئویی انتخاب نشده است.",
            show_alert=True
        )

    await state.update_data(
        target_videos=[video],
        operation_mode=mode_map[call.data],
        fa_sub_path=None,
        en_sub_path=None
    )

    await call.answer()

    await call.message.edit_text(
        f"🎬 فایل انتخاب شده:\n<code>{Path(video).name}</code>",
        parse_mode="HTML"
    )

    await _ask_fa_source(
        call.message,
        state
    )


# ==================================================
# SEARCH EXISTING SUBTITLES
# ==================================================

@router.callback_query(
    F.data.startswith("subscan:")
)
async def scan_existing_subtitles(
    call: CallbackQuery,
    state: FSMContext
):

    language = call.data.split(":")[1]

    if language not in ("fa", "en"):

        return await call.answer(
            "زبان نامعتبر است.",
            show_alert=True
        )

    data = await state.get_data()

    target_videos = data.get(
        "target_videos",
        []
    )

    if not target_videos:

        return await call.answer(
            "فایل ویدئویی انتخاب نشده است.",
            show_alert=True
        )

    video_path = Path(
        target_videos[0]
    )

    files = DownloaderService.find_subtitle_files(
        video_path,
        language
    )

    if not files:

        language_name = (
            "فارسی"
            if language == "fa"
            else "انگلیسی"
        )

        await call.message.edit_text(
            f"🔍 زیرنویس {language_name} در دایرکتوری پیدا نشد.\n\n"
            "می‌توانید فایل زیرنویس را آپلود کنید یا از این مرحله عبور کنید.",
            reply_markup=subtitle_source_kb(language),
            parse_mode="HTML"
        )

        return await call.answer(
            "زیرنویسی پیدا نشد."
        )

    await state.update_data(
        **{
            f"available_{language}_subtitles": [
                str(path)
                for path in files
            ]
        }
    )

    language_name = (
        "فارسی"
        if language == "fa"
        else "انگلیسی"
    )

    await call.message.edit_text(
        f"✅ <b>{len(files)}</b> زیرنویس {language_name} پیدا شد.\n\n"
        "فایل مورد نظر را انتخاب کنید:",
        reply_markup=subtitle_file_found_kb(
            language,
            files
        ),
        parse_mode="HTML"
    )

    await call.answer()


# ==================================================
# SELECT FOUND SUBTITLE
# ==================================================

@router.callback_query(
    F.data.startswith("subfile:")
)
async def choose_existing_subtitle(
    call: CallbackQuery,
    state: FSMContext
):

    try:

        _, language, idx_text = call.data.split(":")

        idx = int(idx_text)

        if language not in ("fa", "en"):

            raise ValueError("Invalid language")

        data = await state.get_data()

        available = data.get(
            f"available_{language}_subtitles",
            []
        )

        if idx < 0 or idx >= len(available):

            return await call.answer(
                "فایل انتخاب شده معتبر نیست.",
                show_alert=True
            )

        selected_path = Path(
            available[idx]
        )

        if not selected_path.is_file():

            return await call.answer(
                "فایل دیگر در مسیر وجود ندارد.",
                show_alert=True
            )

        await state.update_data(
            **{
                f"{language}_sub_path": str(selected_path)
            }
        )

        language_name = (
            "فارسی"
            if language == "fa"
            else "انگلیسی"
        )

        await call.message.edit_text(
            f"✅ زیرنویس {language_name} انتخاب شد:\n"
            f"<code>{selected_path.name}</code>",
            parse_mode="HTML"
        )

        await call.answer()

        if language == "fa":

            await _ask_en_source(
                call.message,
                state
            )

        else:

            await _start_sync(
                call.message,
                state
            )

    except Exception as exc:

        await call.answer(
            f"خطا: {str(exc)[:150]}",
            show_alert=True
        )


# ==================================================
# UPLOAD SUBTITLE
# ==================================================

@router.callback_query(
    F.data.startswith("subupload:")
)
async def request_subtitle_upload(
    call: CallbackQuery,
    state: FSMContext
):

    language = call.data.split(":")[1]

    if language not in ("fa", "en"):

        return await call.answer(
            "زبان نامعتبر است.",
            show_alert=True
        )

    next_state = (
        BotStates.waiting_fa_subtitle
        if language == "fa"
        else BotStates.waiting_en_subtitle
    )

    await state.set_state(next_state)

    language_name = (
        "فارسی"
        if language == "fa"
        else "انگلیسی"
    )

    await call.message.edit_text(
        f"📤 لطفاً فایل زیرنویس {language_name} را ارسال کنید.\n\n"
        "فرمت‌های قابل قبول: SRT / ASS / SSA",
        parse_mode="HTML"
    )

    await call.answer()


@router.message(
    BotStates.waiting_fa_subtitle,
    F.document
)
async def upload_fa_subtitle(
    message: Message,
    state: FSMContext
):

    await _save_uploaded_subtitle(
        message,
        state,
        "fa"
    )


@router.message(
    BotStates.waiting_en_subtitle,
    F.document
)
async def upload_en_subtitle(
    message: Message,
    state: FSMContext
):

    await _save_uploaded_subtitle(
        message,
        state,
        "en"
    )


# ==================================================
# SKIP SUBTITLES
# ==================================================

@router.callback_query(
    BotStates.waiting_fa_subtitle,
    F.data == "skip:fa_sub"
)
async def skip_fa_subtitle(
    call: CallbackQuery,
    state: FSMContext
):

    await call.answer()

    await call.message.edit_text(
        "⏭ مرحله فارسی رد شد.",
    )

    await _ask_en_source(
        call.message,
        state
    )


@router.callback_query(
    BotStates.waiting_en_subtitle,
    F.data == "skip:en_sub"
)
async def skip_en_subtitle(
    call: CallbackQuery,
    state: FSMContext
):

    data = await state.get_data()

    if (
        not data.get("fa_sub_path")
        and not data.get("en_sub_path")
    ):

        return await call.answer(
            "حداقل یک زیرنویس باید انتخاب یا آپلود شود.",
            show_alert=True
        )

    await call.answer()

    await call.message.edit_text(
        "⏭ مرحله انگلیسی رد شد."
    )

    await _start_sync(
        call.message,
        state
    )
async def _extract_and_send_subtitle_job(
    bot,
    chat_id: int,
    video_path: Path,
    track_id: int,
    output_path: Path,
    progress_callback=None
):

    await MKVService.extract_subtitle_track(
        video_path,
        track_id,
        output_path,
        progress_callback
    )

    if not output_path.exists():

        raise FileNotFoundError(
            "فایل زیرنویس استخراج‌شده پیدا نشد."
        )

    await bot.send_document(
        chat_id=chat_id,
        document=FSInputFile(output_path),
        caption=(
            "✅ استخراج زیرنویس تکمیل شد.\n"
            f"🎬 فیلم: {video_path.name}\n"
            f"📄 زیرنویس: {output_path.name}"
        )
    )

    return output_path


@router.callback_query(
    F.data.startswith("exttrack:")
)
async def queue_subtitle_extraction(
    call: CallbackQuery,
    state: FSMContext
):

    try:

        track_id = int(
            call.data.split(":")[1]
        )

        data = await state.get_data()

        video = data.get(
            "selected_video"
        )

        if not video:

            return await call.answer(
                "فایل ویدئویی انتخاب نشده است.",
                show_alert=True
            )

        video_path = Path(video)

        output_path = video_path.with_name(
            f"{video_path.stem}_track{track_id}.srt"
        )

        task = await queue_engine.add_task(
            "mkvtoolnix",
            f"استخراج زیرنویس Track #{track_id}: {video_path.name}",
            _extract_and_send_subtitle_job,
            call.bot,
            call.message.chat.id,
            video_path,
            track_id,
            output_path
        )

        await call.message.edit_text(
            "📤 <b>استخراج زیرنویس به صف اضافه شد.</b>\n\n"
            f"🎬 فیلم: <code>{video_path.name}</code>\n"
            f"🆔 شناسه: <code>{task.task_id}</code>\n\n"
            "پس از پایان استخراج، فایل زیرنویس برایتان ارسال می‌شود.",
            parse_mode="HTML"
        )

        await call.answer()

    except Exception as exc:

        await call.answer(
            f"خطا: {str(exc)[:150]}",
            show_alert=True
        )