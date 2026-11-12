import asyncio
import html
import logging
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
from services.subtitle_service import SubtitleService
from services.mkv_service import MKVService
from services.automation_service import AutomationService
from services.cuteftp_service import CuteFTPService
from bot.keyboards import (
    BROWSER_PAGE_SIZE, main_menu_kb, search_categories_kb,
    search_sites_kb, search_results_kb, download_folder_tree_browser_kb,
    extract_sub_entry_kb, local_extract_browser_kb
)

router = Router()

# حافظه موقت برای نگهداری آیتم‌هایی که پس از اتمام دانلود منتظر تایید نام یا تصمیم FullHD توسط ادمین هستند
PENDING_POST_DL_ITEMS: dict[str, dict] = {}
PENDING_LINK_ACTIONS: dict[str, dict] = {}
_ACTIVE_FULLHD_BATCH_MONITORS: set[asyncio.Task] = set()


class DownloadStates(StatesGroup):
    waiting_search_query = State()
    waiting_direct_link = State()
    browsing_download_folder = State()
    waiting_new_dl_subfolder_name = State()
    waiting_link_for_sub_extract = State()
    browsing_local_for_sub_extract = State()
    waiting_post_dl_custom_name = State()


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


# ==============================================================================
# خط لوله هوشمند پس از دانلود (Post-Download Smart Pipeline)
# ==============================================================================

def _resolve_series_or_movie_context(dest_dir: Path) -> tuple[bool, Path, str]:
    """
    تشخیص اینکه پوشه دانلود مربوط به فصل سریال است یا فیلم سینمایی:
    خروجی: (is_series, season_or_movie_root_dir, series_or_movie_title)
    """
    dest_dir = dest_dir.resolve()
    parts_low = [p.lower() for p in dest_dir.parts]

    if "movies" in parts_low or "movie" in parts_low:
        media_quality_dir = (
            dest_dir.name.casefold() in ("fullhd", "softsub", "hardsub", "srt", "sub")
            or bool(re.search(r"(?i)(1080p|720p|480p).*EBTV", dest_dir.name))
        )
        if media_quality_dir and dest_dir.parent != dest_dir:
            movie_root = dest_dir.parent
            return False, movie_root, movie_root.name
        return False, dest_dir, dest_dir.name

    if AutomationService.SEASON_DIR_REGEX.match(dest_dir.parent.name):
        season_dir = dest_dir.parent
        series_title = season_dir.parent.name
        return True, season_dir, series_title

    if AutomationService.SEASON_DIR_REGEX.match(dest_dir.name):
        season_dir = dest_dir
        series_title = season_dir.parent.name
        return True, season_dir, series_title

    is_ser = AutomationService.is_series_season_dir(dest_dir.parent)
    if is_ser:
        return True, dest_dir.parent, dest_dir.parent.name
    return False, dest_dir, dest_dir.name


async def _check_and_notify_missing_local_folders(bot: Bot, chat_id: int, season_or_movie_dir: Path):
    """
    بررسی پوشه‌های موجود روی سرور مقصد در مقایسه با پوشه‌های موجود در مبدا (Workspace):
    اگر روی سرور مقصد پوشه‌ای وجود دارد که در مبدا نیست، به کاربر در ربات اطلاع می‌دهد!
    """
    remote_season_path = AutomationService.compute_server_remote_dir_for_local_path(season_or_movie_dir)
    remote_info = await CuteFTPService.inspect_remote_dir(remote_season_path)
    if not remote_info.get("exists"):
        return

    server_dirs = set(remote_info.get("dirs", []))
    local_dirs = {d.name for d in season_or_movie_dir.iterdir() if d.is_dir()} if season_or_movie_dir.exists() else set()

    missing_locally = sorted([
        d for d in server_dirs
        if d not in local_dirs and d not in (".", "..")
    ])

    if missing_locally:
        token = uuid.uuid4().hex[:6]
        PENDING_POST_DL_ITEMS[token] = {
            "type": "sync_dirs",
            "local_base": str(season_or_movie_dir),
            "missing_dirs": missing_locally,
        }
        kb = InlineKeyboardBuilder()
        kb.row(
            InlineKeyboardButton(
                text=f"📁 ساخت خودکار این {len(missing_locally)} پوشه در مبدا (Workspace)",
                callback_data=f"pdl:mkdirs:{token}"
            )
        )
        missing_str = "\n".join([f"• `{d}`" for d in missing_locally])
        await bot.send_message(
            chat_id=chat_id,
            text=(
                f"📡 **گزارش مغایرت پوشه‌های سرور مقصد و مبدا:**\n\n"
                f"در مسیر `{remote_season_path}` روی سرور اصلی، پوشه‌های زیر وجود دارند اما در پوشه مبدا (`{season_or_movie_dir.name}`) روی سیستم شما نیستند:\n"
                f"{missing_str}"
            ),
            reply_markup=kb.as_markup(),
            parse_mode="Markdown"
        )


async def _send_missing_remote_folder_upload_prompt(
    bot: Bot,
    chat_id: int,
    vid_path: Path,
    remote_folder_path: str
):
    """وقتی بعد از دانلود می‌خواهد فایلی را آپلود کند اما پوشه‌اش روی سرور نیست، از کاربر سوال می‌پرسد"""
    token = uuid.uuid4().hex[:6]
    PENDING_POST_DL_ITEMS[token] = {
        "type": "missing_remote_folder",
        "vid_path": str(vid_path.resolve()),
        "remote_dir": remote_folder_path,
    }
    kb = InlineKeyboardBuilder()
    kb.row(
        InlineKeyboardButton(
            text=f"✅ بله، پوشه {vid_path.parent.name} در سرور ساخته و آپلود شود",
            callback_data=f"pdl:fhdyes:{token}"
        )
    )
    kb.row(
        InlineKeyboardButton(
            text="❌ خیر، فقط در لوکال بماند (آپلود نشود)",
            callback_data=f"pdl:fhdno:{token}"
        )
    )
    await bot.send_message(
        chat_id=chat_id,
        text=(
            f"❓ **پوشه مقصد روی سرور وجود ندارد!**\n\n"
            f"📄 فایل دانلودشده:\n`{vid_path.name}`\n"
            f"📂 پوشه محلی: `{vid_path.parent.name}`\n"
            f"☁️ مسیر سرور: `{remote_folder_path}`\n\n"
            "این پوشه در حال حاضر روی سرور مقصد وجود ندارد. آیا می‌خواهید پوشه ساخته شده و فایل آپلود شود؟"
        ),
        reply_markup=kb.as_markup(),
        parse_mode="Markdown"
    )

async def _smart_download_pipeline_job(
    bot: Bot,
    chat_id: int,
    target_link: str,
    dest_dir: Path,
    progress_callback=None,
    automated_background: bool = False,
) -> Path:
    """دانلود ایزوله، اکسترکت و اجرای پس‌پردازش فقط روی خروجی همین تسک."""
    dest_dir = Path(dest_dir).resolve()
    dest_dir.mkdir(parents=True, exist_ok=True)

    # هر لینک پوشه staging مستقل دارد؛ بنابراین تسک‌ها فایل‌های یکدیگر را نمی‌بینند.
    staging_dir = dest_dir / "_incoming_downloads" / uuid.uuid4().hex[:12]
    staging_dir.mkdir(parents=True, exist_ok=True)
    new_videos: list[Path] = []

    try:
        await DownloaderService.download(
            target_link,
            staging_dir,
            progress_callback=progress_callback,
        )

        staged_files = [
            p.resolve()
            for p in staging_dir.rglob("*")
            if p.is_file() and not p.name.endswith(".aria2")
        ]
        extracted_files: list[Path] = []

        for archive_path in list(staged_files):
            if archive_path.suffix.lower() not in SubtitleService.ARCHIVE_EXTENSIONS:
                continue
            if progress_callback:
                progress_callback(f"در حال اکسترکت خودکار {archive_path.name}...")
            try:
                extracted = await asyncio.to_thread(
                    SubtitleService.extract_archive,
                    archive_path,
                    archive_path.parent,
                    True,
                    False,
                )
                extracted_files.extend(Path(item).resolve() for item in extracted)
            except Exception as exc:
                logging.exception("Archive extraction failed for %s", archive_path.name)
                raise RuntimeError(f"اکسترکت آرشیو {archive_path.name} ناموفق بود: {exc}") from exc

        staged_candidates = staged_files + extracted_files
        staged_videos = sorted(
            {
                p.resolve()
                for p in staged_candidates
                if p.exists()
                and p.is_file()
                and p.suffix.lower() in DownloaderService.VIDEO_EXTENSIONS
            },
            key=lambda p: p.as_posix().casefold(),
        )
        if not staged_videos:
            raise FileNotFoundError(
                "این تسک هیچ فایل ویدئویی دانلود نکرد؛ خروجی تسک‌های دیگر به‌عنوان نتیجه استفاده نشد."
            )

        for source_video in staged_videos:
            target_video = dest_dir / source_video.name
            if target_video.exists():
                try:
                    if target_video.stat().st_size == source_video.stat().st_size:
                        logging.info("Duplicate download skipped: %s", target_video)
                        continue
                except OSError:
                    pass
                target_video = dest_dir / (
                    f"{source_video.stem}__{uuid.uuid4().hex[:6]}{source_video.suffix}"
                )

            shutil.move(str(source_video), str(target_video))
            new_videos.append(target_video.resolve())

    finally:
        shutil.rmtree(staging_dir, ignore_errors=True)
        try:
            staging_parent = staging_dir.parent
            if staging_parent.exists() and not any(staging_parent.iterdir()):
                staging_parent.rmdir()
        except OSError:
            pass

    if not new_videos:
        logging.info("Download contained no new video files in %s", dest_dir)
        return dest_dir

    is_series, season_or_movie_dir, series_or_movie_title = _resolve_series_or_movie_context(dest_dir)

    # همگام‌سازی ساختار فصل با سرور؛ در دانلود برنامه‌ریزی‌شده پیام جداگانه صادر نمی‌کنیم.
    if not automated_background:
        try:
            await _check_and_notify_missing_local_folders(bot, chat_id, season_or_movie_dir)
        except Exception:
            logging.exception("Could not compare remote and local season folders")

    remote_folder_path = AutomationService.compute_server_remote_dir_for_local_path(dest_dir)
    try:
        remote_folder_info = await CuteFTPService.inspect_remote_dir(remote_folder_path)
    except Exception as exc:
        logging.warning("Could not inspect remote download folder %s: %s", remote_folder_path, exc)
        remote_folder_info = {"files": [], "exists": False}

    server_files_in_folder = remote_folder_info.get("files", [])
    remote_folder_exists = bool(remote_folder_info.get("exists", False))
    auto_upload_cfg = bool(cfg.get("cuteftp", "auto_upload_after_download", True))

    for vid in new_videos:
        ep_tag = AutomationService.extract_episode_tag(
            vid.name,
            fallback_season=AutomationService.extract_season_num_from_dir(season_or_movie_dir),
        ) or ""

        # FullHD فقط سورس انکود است؛ کیفیت‌های خروجی از پوشه فصل تشخیص داده می‌شوند.
        if dest_dir.name.casefold() == "fullhd":
            copied_fullhd_name = None
            if is_series and ep_tag and server_files_in_folder:
                copied_fullhd_name = AutomationService.match_filename_from_server_siblings(
                    server_files_in_folder, ep_tag
                )

            if copied_fullhd_name:
                final_path = vid.with_name(copied_fullhd_name)
                if vid.resolve() != final_path.resolve():
                    if final_path.exists():
                        final_path.unlink(missing_ok=True)
                    vid.rename(final_path)
                    vid = final_path

                await _schedule_fullhd_destination_automation(
                    bot, chat_id, vid, season_or_movie_dir, is_series, ep_tag,
                    automated_background=automated_background,
                )
                if remote_folder_exists:
                    if auto_upload_cfg:
                        await queue_engine.add_task(
                            "upload",
                            f"AutoUpload [{remote_folder_path}]: {vid.name}",
                            CuteFTPService.upload_path,
                            vid,
                            remote_folder_path,
                        )
                elif not automated_background:
                    await _send_missing_remote_folder_upload_prompt(
                        bot, chat_id, vid, remote_folder_path
                    )
            else:
                suggested_std_name = await AutomationService.build_standard_raw_filename_for_folder(
                    video_path=vid,
                    folder_name=dest_dir.name,
                    series_or_movie_title=series_or_movie_title,
                    season_ep_tag=ep_tag,
                    is_series=is_series,
                )
                if automated_background:
                    final_path = vid.with_name(suggested_std_name)
                    if final_path.exists() and final_path.resolve() != vid.resolve():
                        final_path = vid.with_name(
                            f"{Path(suggested_std_name).stem}__{uuid.uuid4().hex[:6]}{Path(suggested_std_name).suffix}"
                        )
                    if final_path.resolve() != vid.resolve():
                        vid.rename(final_path)
                        vid = final_path
                    await _schedule_fullhd_destination_automation(
                        bot, chat_id, vid, season_or_movie_dir, is_series, ep_tag
                    )
                    if remote_folder_exists and auto_upload_cfg:
                        await queue_engine.add_task(
                            "upload",
                            f"AutoUpload [{remote_folder_path}]: {vid.name}",
                            CuteFTPService.upload_path,
                            vid,
                            remote_folder_path,
                            notify_completion=False,
                        )
                else:
                    await _send_standard_name_confirmation_prompt(
                        bot=bot,
                        chat_id=chat_id,
                        vid_path=vid,
                        suggested_name=suggested_std_name,
                        remote_folder_path=remote_folder_path,
                        should_auto_upload=(auto_upload_cfg and remote_folder_exists),
                        ask_fullhd_e01_question=(not remote_folder_exists),
                        fullhd_auto_encode=True,
                    )
            continue

        ask_missing_folder_question = not remote_folder_exists
        should_auto_upload = auto_upload_cfg and remote_folder_exists

        copied_server_name = None
        if is_series and ep_tag and server_files_in_folder:
            copied_server_name = AutomationService.match_filename_from_server_siblings(
                server_files_in_folder, ep_tag
            )

        if copied_server_name:
            final_path = vid.with_name(copied_server_name)
            if vid.resolve() != final_path.resolve():
                if final_path.exists():
                    final_path.unlink(missing_ok=True)
                vid.rename(final_path)
                vid = final_path

            if ask_missing_folder_question and not automated_background:
                await _send_missing_remote_folder_upload_prompt(
                    bot, chat_id, vid, remote_folder_path
                )
            elif should_auto_upload:
                await queue_engine.add_task(
                    "upload",
                    f"AutoUpload [{remote_folder_path}]: {vid.name}",
                    CuteFTPService.upload_path,
                    vid,
                    remote_folder_path,
                )

            await _check_and_prompt_auto_softsub_for_new_quality(
                bot=bot,
                chat_id=chat_id,
                vid_path=vid,
                is_series=is_series,
                season_or_movie_dir=season_or_movie_dir,
                ep_tag=ep_tag,
                automated_background=automated_background,
            )
        else:
            suggested_std_name = await AutomationService.build_standard_raw_filename_for_folder(
                video_path=vid,
                folder_name=dest_dir.name,
                series_or_movie_title=series_or_movie_title,
                season_ep_tag=ep_tag,
                is_series=is_series,
            )
            if automated_background:
                final_path = vid.with_name(suggested_std_name)
                if final_path.exists() and final_path.resolve() != vid.resolve():
                    final_path = vid.with_name(
                        f"{Path(suggested_std_name).stem}__{uuid.uuid4().hex[:6]}{Path(suggested_std_name).suffix}"
                    )
                if final_path.resolve() != vid.resolve():
                    vid.rename(final_path)
                    vid = final_path
                if should_auto_upload:
                    await queue_engine.add_task(
                        "upload",
                        f"AutoUpload [{remote_folder_path}]: {vid.name}",
                        CuteFTPService.upload_path,
                        vid,
                        remote_folder_path,
                        notify_completion=False,
                    )
                await _check_and_prompt_auto_softsub_for_new_quality(
                    bot=bot,
                    chat_id=chat_id,
                    vid_path=vid,
                    is_series=is_series,
                    season_or_movie_dir=season_or_movie_dir,
                    ep_tag=ep_tag,
                    automated_background=True,
                )
            else:
                await _send_standard_name_confirmation_prompt(
                    bot=bot,
                    chat_id=chat_id,
                    vid_path=vid,
                    suggested_name=suggested_std_name,
                    remote_folder_path=remote_folder_path,
                    should_auto_upload=should_auto_upload,
                    ask_fullhd_e01_question=ask_missing_folder_question,
                )

    return new_videos[0]


async def _send_fullhd_e01_prompt(bot: Bot, chat_id: int, vid_path: Path, remote_folder_path: str):
    # سازگاری با callbackهای قدیمی؛ سوال اکنون برای هر پوشه مقصدی استفاده می‌شود.
    return await _send_missing_remote_folder_upload_prompt(
        bot, chat_id, vid_path, remote_folder_path
    )

def _release_fullhd_monitor(task: asyncio.Task):
    _ACTIVE_FULLHD_BATCH_MONITORS.discard(task)
    if task.cancelled():
        return
    try:
        error = task.exception()
    except asyncio.CancelledError:
        return
    if error:
        logging.error(
            "FullHD automation status monitor failed: %s",
            error,
            exc_info=(type(error), error, error.__traceback__),
        )


def _fullhd_status_markup():
    kb = InlineKeyboardBuilder()
    kb.row(
        InlineKeyboardButton(text="📊 مشاهده صف کامل", callback_data="menu:queues"),
        InlineKeyboardButton(text="📂 فایل‌منیجر", callback_data="menu:file_mgr"),
    )
    kb.row(InlineKeyboardButton(text="🔙 منوی اصلی", callback_data="menu:home"))
    return kb.as_markup()


def _build_fullhd_status_text(title: str, tasks: list, notes: list[str], final: bool = False) -> str:
    running = [
        t for t in tasks
        if not t.is_finished and ("اجرا" in str(t.status) or t.running_asyncio_task is not None)
    ]
    done = [
        t for t in tasks
        if t.is_finished and "خطا" not in str(t.status) and "لغو" not in str(t.status)
    ]
    failed = [t for t in tasks if "خطا" in str(t.status) or "لغو" in str(t.status)]
    pending = [t for t in tasks if not t.is_finished and t not in running]

    if final:
        stage = "✅ تمام عملیات ثبت‌شده پایان یافت"
    elif any(t.queue_type == "upload" for t in running):
        stage = "☁️ آپلود خروجی‌ها به سرور"
    elif any(t.queue_type == "mkvtoolnix" for t in running):
        stage = "📎 ساخت نسخه SoftSub"
    elif any(t.queue_type == "handbrake" for t in running):
        stage = "🎬 انکود کیفیت‌های EBTV"
    elif pending:
        stage = "⏳ انتظار در صف پردازش"
    else:
        stage = "🧾 آماده‌سازی گزارش"

    lines = [
        f"🎬 <b>اتوماسیون FullHD: {html.escape(title)}</b>",
        "━━━━━━━━━━━━━━━━━━━━",
        f"📍 <b>{stage}</b>",
        (
            f"✅ تکمیل‌شده: <b>{len(done)}</b>  |  "
            f"⚙️ در حال اجرا: <b>{len(running)}</b>  |  "
            f"⏳ منتظر: <b>{len(pending)}</b>"
        ),
        f"❌ خطا/لغو: <b>{len(failed)}</b>",
        "",
        "<b>عملیات:</b>",
    ]

    for task in tasks[:14]:
        if "خطا" in str(task.status):
            icon, detail = "❌", str(task.error or task.progress or "خطا")[:70]
        elif "لغو" in str(task.status):
            icon, detail = "🛑", "لغو شد"
        elif task.is_finished:
            icon, detail = "✅", "تکمیل شد"
        elif task in running:
            icon, detail = "⚙️", str(task.progress or "در حال اجرا")[:70]
        else:
            icon, detail = "⏳", "در انتظار"
        lines.append(
            f"{icon} <code>#{html.escape(str(task.task_id))} "
            f"{html.escape(str(task.title)[:85])}</code> — "
            f"<i>{html.escape(detail)}</i>"
        )

    if len(tasks) > 14:
        lines.append(f"<i>... و {len(tasks) - 14} عملیات دیگر</i>")

    if notes:
        lines.extend(["", "<b>یادداشت‌ها:</b>"])
        lines.extend(f"▫️ {html.escape(note[:160])}" for note in notes[:6])

    if final and failed:
        lines.extend(["", "⚠️ <b>اتوماسیون پایان یافت، اما بخشی از عملیات خطا یا لغو شد.</b>"])
    elif final:
        lines.extend(["", "🎉 <b>اتوماسیون با موفقیت به پایان رسید.</b>"])
    return "\n".join(lines)[:3900]


async def _watch_fullhd_batch(
    status_message: Message,
    batch_id: str,
    title: str,
    initial_tasks: list,
    notes: list[str],
):
    known = {task.task_id: task for task in initial_tasks}
    markup = _fullhd_status_markup()
    last_text = None
    stable_cycles = 0

    while True:
        # زیرتسک‌های Upload که پس از Encode یا SoftSub به صف اضافه می‌شوند، batch را به ارث می‌برند.
        for task in list(queue_engine.tasks.values()):
            if getattr(task, "batch_id", None) == batch_id:
                known[task.task_id] = task

        tasks = list(known.values())
        complete = bool(tasks) and all(task.is_finished for task in tasks)
        stable_cycles = stable_cycles + 1 if complete else 0
        report = _build_fullhd_status_text(
            title, tasks, notes, final=stable_cycles >= 2
        )
        if report != last_text:
            try:
                await status_message.edit_text(
                    report,
                    reply_markup=markup,
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                )
                last_text = report
            except Exception as exc:
                if "message is not modified" not in str(exc).lower():
                    logging.warning("Could not update FullHD automation status: %s", exc)

        if stable_cycles >= 2:
            return
        await asyncio.sleep(3)


async def _run_fullhd_encode_with_destination_routing(
    encode_target,
    fa_sub: Path | None,
    en_sub: Path | None,
    brand: str,
    remote_ebtv_dir: str | None,
    local_softsub_dir: Path | None,
    remote_softsub_dir: str | None,
    progress_callback=None,
) -> Path:
    """انکود FullHD؛ در صورت وجود مقصد SoftSub و زیرنویس، Mux وگرنه آپلود EBTV."""
    encoded = await AutomationService.run_hardsub_or_encode_and_optional_upload(
        encode_target,
        fa_sub,
        en_sub,
        brand,
        False,
        progress_callback,
    )

    if remote_softsub_dir and local_softsub_dir and fa_sub and fa_sub.exists():
        soft_file = local_softsub_dir / AutomationService.build_softsub_filename(encoded)
        await AutomationService.run_softsub_and_optional_upload(
            encoded,
            soft_file,
            fa_sub,
            en_sub,
            brand,
            True,
            progress_callback,
        )
    elif remote_ebtv_dir:
        await queue_engine.add_task(
            "upload",
            f"AutoUpload FullHD Encode [{remote_ebtv_dir}]: {encoded.name}",
            CuteFTPService.upload_path,
            encoded,
            remote_ebtv_dir,
        )
    elif remote_softsub_dir:
        # فایل خام encode شده نباید در پوشه‌ای بارگذاری شود که مخصوص خروجی SoftSub است.
        logging.warning(
            "SoftSub destination %s exists but no matching Persian subtitle was found; "
            "keeping encoded file locally instead of uploading a non-SoftSub file there.",
            remote_softsub_dir,
        )
    return encoded


async def _schedule_fullhd_destination_automation(
    bot: Bot,
    chat_id: int,
    vid_path: Path,
    season_or_movie_dir: Path,
    is_series: bool,
    ep_tag: str,
    automated_background: bool = False,
):
    """
    کیفیت‌های تعریف‌شده در مقصد را می‌خواند و فقط همان رزولوشن‌ها را از FullHD انکود می‌کند.
    وجود پوشه -SoftSub به معنی ساخت نسخه SoftSub در همان مقصد است؛ در غیر این صورت
    فایل انکودشده به پوشه EBTV متناظر Upload می‌شود.
    """
    if not vid_path.exists():
        return

    remote_root = AutomationService.compute_server_remote_dir_for_local_path(season_or_movie_dir)
    try:
        remote_info = await CuteFTPService.inspect_remote_dir(remote_root)
    except Exception as exc:
        if automated_background:
            logging.warning("FullHD quality inspection failed for %s: %s", remote_root, exc)
            return
        await bot.send_message(
            chat_id,
            f"⚠️ بررسی پوشه‌های کیفیت مقصد ناموفق بود؛ انکود خودکار انجام نشد.\n"
            f"مسیر: <code>{html.escape(remote_root)}</code>\n"
            f"خطا: <code>{html.escape(str(exc)[:250])}</code>",
            parse_mode="HTML",
        )
        return

    if not remote_info.get("exists"):
        if automated_background:
            logging.info("FullHD destination is not present remotely: %s", remote_root)
            return
        await bot.send_message(
            chat_id,
            f"⚠️ پوشه مقصد قابل بررسی نیست یا دسترسی FTP تنظیم نشده است؛ "
            f"کیفیت‌های EBTV برای <code>{html.escape(remote_root)}</code> شناسایی نشدند.",
            parse_mode="HTML",
        )
        return

    quality_map: dict[str, dict[str, str]] = {}
    for remote_name in remote_info.get("dirs", []):
        name = str(remote_name).strip()
        low = name.casefold()
        match = re.search(r"(?i)(1080p|720p|480p)", name)
        if not match:
            continue
        q_tag = match.group(1).lower()
        if not (
            re.search(r"(?i)(?:^|[-._\s])EBTV$", name)
            or re.search(r"(?i)(?:^|[-._\s])EBTV-SoftSub$", name)
        ):
            continue

        bucket = quality_map.setdefault(q_tag, {})
        if re.search(r"(?i)(?:^|[-._\s])EBTV-SoftSub$", name):
            bucket["soft"] = name
        else:
            bucket["base"] = name

    if not quality_map:
        if automated_background:
            logging.info("No remote EBTV quality folders found for FullHD source %s", vid_path)
            return
        await bot.send_message(
            chat_id,
            f"ℹ️ در مسیر <code>{html.escape(remote_root)}</code> هیچ پوشه‌ای با پسوند "
            "<code>EBTV</code> یا <code>EBTV-SoftSub</code> پیدا نشد؛ "
            "فایل FullHD در Workspace باقی ماند.",
            parse_mode="HTML",
        )
        return

    if not AutomationService.get_movie_ass_logo_path().exists():
        if automated_background:
            logging.warning("movie.ass is missing; skipping FullHD encode for %s", vid_path)
            return
        await bot.send_message(
            chat_id,
            "❌ فایل <code>movie.ass</code> برای انکود/واترمارک در مسیر presets یا کنار برنامه پیدا نشد؛ "
            "انکود خودکار انجام نشد.",
            parse_mode="HTML",
        )
        return

    plan = await AutomationService.analyze_and_build_plan(
        season_or_movie_dir,
        ep_tag if is_series and ep_tag else None,
    )
    fa_sub = None
    en_sub = plan.existing_en_sub if plan.existing_en_sub and plan.existing_en_sub.exists() else None
    try:
        candidates = AutomationService.find_local_fa_subtitles(plan)
        key = plan.season_ep_tag if (plan.is_series and plan.season_ep_tag) else "MOVIE"
        matched = AutomationService.match_subtitles_to_episodes([plan], candidates)
        candidate = matched.get(key)
        if candidate and candidate.exists():
            fa_sub = candidate
    except Exception as exc:
        logging.warning("Could not match FullHD subtitle: %s", exc)

    qualities = sorted(
        quality_map,
        key=lambda q: {"1080p": 1, "720p": 2, "480p": 3}.get(q, 9),
    )
    targets = AutomationService.build_manual_encode_ass_targets([vid_path], qualities)
    if not is_series:
        source_type = AutomationService.detect_source_type(vid_path.name)
        for target in targets:
            movie_output_name = AutomationService.build_encode_ass_filename(
                season_or_movie_dir.name,
                "",
                target.quality_tag,
                source_type,
            )
            target.output_file = target.output_file.with_name(movie_output_name)
    targets_by_quality = {target.quality_tag: target for target in targets}
    batch_id = uuid.uuid4().hex[:10]
    tasks = []
    notes: list[str] = []

    for q_tag in qualities:
        target = targets_by_quality.get(q_tag)
        if not target:
            notes.append(f"{q_tag}: preset انکود متناظر ساخته نشد.")
            continue

        names = quality_map[q_tag]
        base_name = names.get("base", f"{q_tag}-EBTV")
        soft_name = names.get("soft")

        local_encode_dir = season_or_movie_dir / base_name
        local_encode_dir.mkdir(parents=True, exist_ok=True)
        target.output_dir = local_encode_dir
        target.output_file = local_encode_dir / target.output_file.name

        local_soft_dir = (season_or_movie_dir / soft_name) if soft_name else None
        if local_soft_dir:
            local_soft_dir.mkdir(parents=True, exist_ok=True)

        remote_base = f"{remote_root.rstrip('/')}/{base_name}" if "base" in names else None
        remote_soft = f"{remote_root.rstrip('/')}/{soft_name}" if soft_name else None

        if soft_name and (not fa_sub or not fa_sub.exists()):
            notes.append(
                f"{q_tag}: پوشه SoftSub مقصد وجود دارد اما زیرنویس فارسی این عنوان/قسمت پیدا نشد؛ "
                "در صورت وجود پوشه EBTV اصلی، خروجی انکودشده به آن ارسال می‌شود."
            )

        task = await queue_engine.add_task(
            "handbrake",
            f"FullHD Encode [{q_tag}]: {target.output_file.name}",
            _run_fullhd_encode_with_destination_routing,
            target,
            fa_sub,
            en_sub,
            cfg.get("branding", "global_title_and_track_name", ".::EmpireBestTv.Com::."),
            remote_base,
            local_soft_dir,
            remote_soft,
            batch_id=batch_id,
            notify_completion=False,
        )
        tasks.append(task)

    if not tasks:
        if automated_background:
            logging.warning("No FullHD encode tasks could be scheduled for %s: %s", vid_path, notes)
            return
        return await bot.send_message(
            chat_id,
            "⚠️ کیفیت مناسب برای انکود در صف قرار نگرفت.",
            parse_mode="HTML",
        )

    if automated_background:
        logging.info(
            "Scheduled %d FullHD encode tasks for %s: %s",
            len(tasks),
            vid_path.name,
            [task.task_id for task in tasks],
        )
        return

    status_title = (
        season_or_movie_dir.parent.name
        if is_series and AutomationService.SEASON_DIR_REGEX.match(season_or_movie_dir.name)
        else season_or_movie_dir.name
    )
    status_message = await bot.send_message(
        chat_id,
        _build_fullhd_status_text(status_title, tasks, notes),
        reply_markup=_fullhd_status_markup(),
        parse_mode="HTML",
        disable_web_page_preview=True,
    )
    monitor = asyncio.create_task(
        _watch_fullhd_batch(status_message, batch_id, status_title, tasks, notes),
        name=f"fullhd-status-{batch_id}",
    )
    _ACTIVE_FULLHD_BATCH_MONITORS.add(monitor)
    monitor.add_done_callback(_release_fullhd_monitor)


async def _check_and_prompt_auto_softsub_for_new_quality(
    bot: Bot,
    chat_id: int,
    vid_path: Path,
    is_series: bool,
    season_or_movie_dir: Path,
    ep_tag: str,
    automated_background: bool = False
):
    """
    بررسی وجود زیرنویس فارسی در پوشه زیرنویس پس از دانلود یک کیفیت جدید:
    اگر برای این قسمت (یا فیلم) زیرنویس فارسی موجود باشد، از کاربر تاییدیه می‌گیرد
    تا نسخه سافت‌ساب این کیفیت جدید را هم بسازد و به سرور آپلود کند.
    """
    if not vid_path.exists():
        return

    # اگر خودِ پوشه از نوع SoftSub یا HardSub است، نیازی به سافت‌ساب مجدد نیست
    parent_low = vid_path.parent.name.lower()
    if parent_low in ("softsub", "hardsub", "srt", "sub") or parent_low.endswith(("-softsub", "-hardsub")):
        return

    plan = await AutomationService.analyze_and_build_plan(
        season_or_movie_dir,
        ep_tag if is_series else None
    )

    # جستجوی دقیق زیرنویس فارسی مربوط به همین قسمت در پوشه srt/FA یا srt
    fa_candidates = AutomationService.find_local_fa_subtitles(plan)
    matched_map = AutomationService.match_subtitles_to_episodes([plan], fa_candidates)
    key = plan.season_ep_tag if (is_series and plan.season_ep_tag) else "MOVIE"
    fa_sub = matched_map.get(key)

    if not fa_sub or not fa_sub.exists():
        return

    en_sub = plan.existing_en_sub if (plan.existing_en_sub and plan.existing_en_sub.exists()) else None

    if is_series:
        soft_out_dir = season_or_movie_dir / f"{vid_path.parent.name}-SoftSub"
    else:
        soft_out_dir = season_or_movie_dir / "SoftSub"

    soft_out_file = soft_out_dir / AutomationService.build_softsub_filename(vid_path)
    remote_soft_dir = AutomationService.compute_server_remote_dir_for_local_path(soft_out_file)

    if automated_background:
        brand = cfg.get("branding", "global_title_and_track_name", ".::EmpireBestTv.Com::.")
        auto_upload = False
        if bool(cfg.get("cuteftp", "auto_upload_after_download", True)):
            try:
                remote_info = await CuteFTPService.inspect_remote_dir(remote_soft_dir)
                auto_upload = bool(remote_info.get("exists", False))
            except Exception:
                logging.exception("Could not inspect automatic SoftSub destination %s", remote_soft_dir)

        task = await queue_engine.add_task(
            "mkvtoolnix",
            f"AutoSoftSub [{vid_path.parent.name}]: {soft_out_file.name}",
            AutomationService.run_softsub_and_optional_upload,
            vid_path,
            soft_out_file,
            fa_sub,
            en_sub,
            brand,
            auto_upload,
            notify_completion=False,
        )
        logging.info(
            "Automatic SoftSub queued for %s (task=%s, upload=%s)",
            vid_path.name,
            task.task_id,
            auto_upload,
        )
        return

    token = uuid.uuid4().hex[:6]
    PENDING_POST_DL_ITEMS[token] = {
        "type": "new_quality_softsub",
        "raw_video": str(vid_path.resolve()),
        "output_file": str(soft_out_file.resolve()),
        "fa_sub": str(fa_sub.resolve()),
        "en_sub": str(en_sub.resolve()) if en_sub else None,
        "quality_folder": vid_path.parent.name,
        "remote_soft_dir": remote_soft_dir,
    }

    kb = InlineKeyboardBuilder()
    kb.row(
        InlineKeyboardButton(
            text="✅ بله، سافت‌ساب کن و در سرور آپلود کن",
            callback_data=f"pdl:softyes:{token}"
        )
    )
    kb.row(
        InlineKeyboardButton(
            text="📎 فقط سافت‌ساب کن (بدون آپلود)",
            callback_data=f"pdl:softlocal:{token}"
        ),
        InlineKeyboardButton(
            text="❌ خیر، نیازی نیست",
            callback_data=f"pdl:softno:{token}"
        )
    )

    en_badge = f"`{en_sub.name}`" if en_sub else "`داخلی فیلم / ندارد`"
    await bot.send_message(
        chat_id=chat_id,
        text=(
            f"🎬 **زیرنویس فارسی برای این کیفیت جدید در پوشه موجود است!**\n\n"
            f"📥 **کیفیت دانلودشده:** `{vid_path.parent.name}/{vid_path.name}`\n"
            f"🇮🇷 **زیرنویس فارسی یافت‌شده:** `{fa_sub.name}`\n"
            f"🇬🇧 **زیرنویس انگلیسی:** {en_badge}\n"
            f"📂 **مسیر سافت‌ساب خروجی:** `{soft_out_dir.name}/{soft_out_file.name}`\n"
            f"☁️ **مسیر آپلود در سرور:** `{remote_soft_dir}`\n\n"
            "آیا می‌خواهید این کیفیت جدید به صورت خودکار **سافت‌ساب شده و به سرور آپلود گردد**؟"
        ),
        reply_markup=kb.as_markup(),
        parse_mode="Markdown"
    )


@router.callback_query(F.data.startswith("pdl:softyes:"))
async def cb_pdl_new_quality_softsub_yes(call: CallbackQuery):
    token = call.data.split(":")[2]
    info = PENDING_POST_DL_ITEMS.pop(token, None)
    if not info:
        return await call.answer("⚠️ این آیتم منقضی شده است.", show_alert=True)

    raw_vid = Path(info["raw_video"])
    out_file = Path(info["output_file"])
    fa_sub = Path(info["fa_sub"])
    en_sub = Path(info["en_sub"]) if info.get("en_sub") else None
    brand = cfg.get("branding", "global_title_and_track_name", ".::EmpireBestTv.Com::.")

    out_file.parent.mkdir(parents=True, exist_ok=True)
    task = await queue_engine.add_task(
        "mkvtoolnix",
        f"SoftSub [{info['quality_folder']}]: {out_file.name}",
        AutomationService.run_softsub_and_optional_upload,
        raw_vid,
        out_file,
        fa_sub,
        en_sub,
        brand,
        True  # آپلود خودکار به سرور پس از اتمام سافت‌ساب
    )

    await call.message.edit_text(
        f"✅ **سافت‌ساب و آپلود خودکار در صف قرار گرفت!**\n\n"
        f"📎 فایل خروجی: `{out_file.parent.name}/{out_file.name}`\n"
        f"☁️ مقصد آپلود پس از اتمام: `{info['remote_soft_dir']}`\n"
        f"🆔 کد تسک: `{task.task_id}`",
        parse_mode="Markdown"
    )


@router.callback_query(F.data.startswith("pdl:softlocal:"))
async def cb_pdl_new_quality_softsub_local_only(call: CallbackQuery):
    token = call.data.split(":")[2]
    info = PENDING_POST_DL_ITEMS.pop(token, None)
    if not info:
        return await call.answer("⚠️ این آیتم منقضی شده است.", show_alert=True)

    raw_vid = Path(info["raw_video"])
    out_file = Path(info["output_file"])
    fa_sub = Path(info["fa_sub"])
    en_sub = Path(info["en_sub"]) if info.get("en_sub") else None
    brand = cfg.get("branding", "global_title_and_track_name", ".::EmpireBestTv.Com::.")

    out_file.parent.mkdir(parents=True, exist_ok=True)
    task = await queue_engine.add_task(
        "mkvtoolnix",
        f"SoftSub [{info['quality_folder']}]: {out_file.name}",
        AutomationService.run_softsub_and_optional_upload,
        raw_vid,
        out_file,
        fa_sub,
        en_sub,
        brand,
        False  # بدون آپلود به سرور
    )

    await call.message.edit_text(
        f"✅ **سافت‌ساب (بدون آپلود) در صف قرار گرفت!**\n"
        f"📎 فایل خروجی: `{out_file.parent.name}/{out_file.name}`\n"
        f"🆔 کد تسک: `{task.task_id}`",
        parse_mode="Markdown"
    )


@router.callback_query(F.data.startswith("pdl:softno:"))
async def cb_pdl_new_quality_softsub_no(call: CallbackQuery):
    token = call.data.split(":")[2]
    PENDING_POST_DL_ITEMS.pop(token, None)
    await call.message.edit_text("⏭ ساخت سافت‌ساب برای این کیفیت رد شد.")

async def _send_standard_name_confirmation_prompt(
    bot: Bot,
    chat_id: int,
    vid_path: Path,
    suggested_name: str,
    remote_folder_path: str,
    should_auto_upload: bool,
    ask_fullhd_e01_question: bool,
    fullhd_auto_encode: bool = False
):
    token = uuid.uuid4().hex[:6]
    PENDING_POST_DL_ITEMS[token] = {
        "type": "confirm_name",
        "vid_path": str(vid_path.resolve()),
        "suggested_name": suggested_name,
        "remote_dir": remote_folder_path,
        "should_auto_upload": should_auto_upload,
        "ask_fullhd_e01": ask_fullhd_e01_question,
        "fullhd_auto_encode": fullhd_auto_encode,
    }
    kb = InlineKeyboardBuilder()
    kb.row(
        InlineKeyboardButton(
            text="✅ تایید همین نام استاندارد و ادامه",
            callback_data=f"pdl:stdok:{token}"
        )
    )
    kb.row(
        InlineKeyboardButton(
            text="✏️ نوشتن دستی نام نهایی توسط شما",
            callback_data=f"pdl:custom:{token}"
        )
    )
    kb.row(
        InlineKeyboardButton(
            text="📄 حفظ نام فعلی فایل بدون تغییر",
            callback_data=f"pdl:keep:{token}"
        )
    )

    await bot.send_message(
        chat_id=chat_id,
        text=(
            f"📝 **تایید نام استاندارد فایل دانلودشده**\n"
            f"*(در پوشه `{remote_folder_path}` روی سرور اصلی قسمت قبلی برای کپی خودکار نام یافت نشد)*\n\n"
            f"📂 پوشه: `{vid_path.parent.name}`\n"
            f"📄 **نام فعلی فایل (برای کپی لمس کنید):**\n`{vid_path.name}`\n\n"
            f"✨ **نام استاندارد پیشنهادی (برای کپی لمس کنید):**\n`{suggested_name}`\n\n"
            "👇 لطفاً نام استاندارد را تایید کنید و یا دکمه **«✏️ نوشتن دستی نام نهایی»** را بزنید:"
        ),
        reply_markup=kb.as_markup(),
        parse_mode="Markdown"
    )


@router.callback_query(F.data.startswith("pdl:mkdirs:"))
async def cb_pdl_create_missing_local_dirs(call: CallbackQuery):
    token = call.data.split(":")[2]
    info = PENDING_POST_DL_ITEMS.pop(token, None)
    if not info:
        return await call.answer("⚠️ این پیام منقضی شده است.", show_alert=True)

    base_dir = Path(info["local_base"])
    created = []
    for d_name in info.get("missing_dirs", []):
        p = base_dir / d_name
        p.mkdir(parents=True, exist_ok=True)
        created.append(f"`{d_name}`")

    await call.message.edit_text(
        f"✅ پوشه‌های زیر در مسیر `{base_dir}` همگام با سرور ساخته شدند:\n" + " ، ".join(created),
        parse_mode="Markdown"
    )


@router.callback_query(F.data.startswith("pdl:fhdyes:"))
async def cb_pdl_fullhd_upload_yes(call: CallbackQuery):
    token = call.data.split(":")[2]
    info = PENDING_POST_DL_ITEMS.pop(token, None)
    if not info:
        return await call.answer("⚠️ این آیتم منقضی شده است.", show_alert=True)

    vid_path = Path(info["vid_path"])
    remote_dir = info["remote_dir"]
    task = await queue_engine.add_task(
        "upload",
        f"AutoUpload [{remote_dir}]: {vid_path.name}",
        CuteFTPService.upload_path,
        vid_path,
        remote_dir
    )
    await call.message.edit_text(
        f"☁️ پوشه مقصد ساخته و آپلود فایل `{vid_path.name}` به مسیر `{remote_dir}` در صف قرار گرفت! (کد: `{task.task_id}`)",
        parse_mode="Markdown"
    )


@router.callback_query(F.data.startswith("pdl:fhdno:"))
async def cb_pdl_fullhd_upload_no(call: CallbackQuery):
    token = call.data.split(":")[2]
    info = PENDING_POST_DL_ITEMS.pop(token, None)
    vid_name = Path(info["vid_path"]).name if info else "FullHD"
    await call.message.edit_text(
        f"✅ فایل `{vid_name}` در Workspace باقی ماند و به سرور آپلود نشد.",
        parse_mode="Markdown"
    )


async def _finalize_post_dl_rename_and_upload(
    msg_or_call: Message | CallbackQuery,
    info: dict,
    new_filename: str
):
    vid_path = Path(info["vid_path"])
    if not vid_path.exists():
        text = "❌ فایل مورد نظر روی دیسک یافت نشد!"
        if isinstance(msg_or_call, CallbackQuery):
            return await msg_or_call.message.edit_text(text)
        return await msg_or_call.answer(text)

    if not new_filename.lower().endswith(vid_path.suffix.lower()):
        new_filename = f"{new_filename}{vid_path.suffix}"

    final_path = vid_path.with_name(new_filename)
    if vid_path.resolve() != final_path.resolve():
        if final_path.exists():
            final_path.unlink(missing_ok=True)
        vid_path.rename(final_path)
        vid_path = final_path

    remote_dir = info["remote_dir"]
    bot = msg_or_call.bot
    chat_id = msg_or_call.message.chat.id if isinstance(msg_or_call, CallbackQuery) else msg_or_call.chat.id

    if info.get("fullhd_auto_encode"):
        confirmation = f"✅ نام فایل FullHD نهایی شد: <code>{html.escape(vid_path.name)}</code>"
        if isinstance(msg_or_call, CallbackQuery):
            await msg_or_call.message.edit_text(confirmation, parse_mode="HTML")
        else:
            await msg_or_call.answer(confirmation, parse_mode="HTML")
        is_ser, season_or_movie_dir, _ = _resolve_series_or_movie_context(vid_path.parent)
        ep_tag = AutomationService.extract_episode_tag(
            vid_path.name,
            fallback_season=AutomationService.extract_season_num_from_dir(season_or_movie_dir),
        ) or ""
        await _schedule_fullhd_destination_automation(
            bot, chat_id, vid_path, season_or_movie_dir, is_ser, ep_tag
        )
        if info.get("ask_fullhd_e01"):
            return await _send_missing_remote_folder_upload_prompt(
                bot, chat_id, vid_path, remote_dir
            )
        if info.get("should_auto_upload"):
            task = await queue_engine.add_task(
                "upload",
                f"AutoUpload [{remote_dir}]: {vid_path.name}",
                CuteFTPService.upload_path,
                vid_path,
                remote_dir,
            )
            return await bot.send_message(
                chat_id,
                f"☁️ فایل FullHD نیز به دلیل موجود بودن پوشه مقصد، بدون نیاز به تأیید در صف آپلود قرار گرفت. کد: <code>{task.task_id}</code>",
                parse_mode="HTML",
            )
        return

    if info.get("ask_fullhd_e01"):
        confirmation = f"✅ نام فایل نهایی شد: <code>{html.escape(vid_path.name)}</code>"
        if isinstance(msg_or_call, CallbackQuery):
            await msg_or_call.message.edit_text(confirmation, parse_mode="HTML")
        else:
            await msg_or_call.answer(confirmation, parse_mode="HTML")
        return await _send_missing_remote_folder_upload_prompt(
            bot, chat_id, vid_path, remote_dir
        )

    if info.get("should_auto_upload", True):
        task = await queue_engine.add_task(
            "upload",
            f"AutoUpload [{remote_dir}]: {vid_path.name}",
            CuteFTPService.upload_path,
            vid_path,
            remote_dir
        )
        done_txt = (
            f"✅ نام فایل به:\n`{vid_path.name}`\nتغییر یافت و آپلود خودکار آن به مسیر `{remote_dir}` در صف قرار گرفت! (کد: `{task.task_id}`)"
        )
    else:
        done_txt = (
            f"✅ نام فایل به:\n`{vid_path.name}`\nتغییر یافت.\n"
            "ℹ️ آپلود خودکار غیرفعال است؛ فایل در Workspace باقی ماند."
        )

    if isinstance(msg_or_call, CallbackQuery):
        await msg_or_call.message.edit_text(done_txt, parse_mode="Markdown")
    else:
        await msg_or_call.answer(done_txt, parse_mode="Markdown")

        # پس از نهایی شدن نام فایل خام، بررسی کن آیا زیرنویس فارسی برای این قسمت در پوشه هست تا سافت‌ساب شود؟
    is_ser, season_or_movie_dir, _ = _resolve_series_or_movie_context(vid_path.parent)
    ep_tag = AutomationService.extract_episode_tag(vid_path.name) or ""
    await _check_and_prompt_auto_softsub_for_new_quality(
        bot=bot,
        chat_id=chat_id,
        vid_path=vid_path,
        is_series=is_ser,
        season_or_movie_dir=season_or_movie_dir,
        ep_tag=ep_tag
    )


@router.callback_query(F.data.startswith("pdl:stdok:"))
async def cb_pdl_confirm_standard_name(call: CallbackQuery):
    token = call.data.split(":")[2]
    info = PENDING_POST_DL_ITEMS.pop(token, None)
    if not info:
        return await call.answer("⚠️ این آیتم منقضی شده است.", show_alert=True)
    await _finalize_post_dl_rename_and_upload(call, info, info["suggested_name"])


@router.callback_query(F.data.startswith("pdl:keep:"))
async def cb_pdl_keep_original_name(call: CallbackQuery):
    token = call.data.split(":")[2]
    info = PENDING_POST_DL_ITEMS.pop(token, None)
    if not info:
        return await call.answer("⚠️ این آیتم منقضی شده است.", show_alert=True)
    orig_name = Path(info["vid_path"]).name
    await _finalize_post_dl_rename_and_upload(call, info, orig_name)


@router.callback_query(F.data.startswith("pdl:custom:"))
async def cb_pdl_ask_custom_name(call: CallbackQuery, state: FSMContext):
    token = call.data.split(":")[2]
    info = PENDING_POST_DL_ITEMS.get(token)
    if not info:
        return await call.answer("⚠️ این آیتم منقضی شده است.", show_alert=True)

    await state.update_data(pdl_active_token=token)
    await state.set_state(DownloadStates.waiting_post_dl_custom_name)
    await call.message.edit_text(
        f"✏️ **ارسال نام دلخواه برای فایل دانلودشده:**\n\n"
        f"🔹 نام پیشنهادی (برای کپی و ویرایش لمس کنید):\n`{info['suggested_name']}`\n\n"
        "لطفاً **نام نهایی فایل** را تایپ و ارسال کنید تا تغییر نام داده شده و در سرور آپلود شود:",
        parse_mode="Markdown"
    )


@router.message(DownloadStates.waiting_post_dl_custom_name)
async def handle_pdl_custom_name_input(message: Message, state: FSMContext):
    custom_name = re.sub(r'[<>:"/\\|?*]', "", (message.text or "").strip()).strip()
    if not custom_name:
        return await message.answer("⚠️ نام وارد شده معتبر نیست. لطفاً دوباره بفرستید:")

    data = await state.get_data()
    token = data.get("pdl_active_token")
    info = PENDING_POST_DL_ITEMS.pop(token, None) if token else None
    await state.clear()

    if not info:
        return await message.answer("⚠️ اطلاعات فایل منقضی شده است.", reply_markup=main_menu_kb())

    await _finalize_post_dl_rename_and_upload(message, info, custom_name)


# ==============================================================================
# مرورگر انتخاب پوشه مقصد دانلود و صف‌بندی هوشمند دانلودها
# ==============================================================================

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
        "• پس از اتمام دانلود، فایل به صورت خودکار (در صورت زیپ بودن اکسترکت شده)، بر اساس سرور اصلی تغییر نام می‌یابد و در همان پوشه در سرور آپلود می‌شود:"
    )
    kb = download_folder_tree_browser_kb(subfolders, current_dir, is_root, page=page)

    if isinstance(target_msg_or_call, CallbackQuery):
        await target_msg_or_call.message.edit_text(text[:4000], reply_markup=kb, parse_mode="Markdown")
    else:
        await target_msg_or_call.answer(text[:4000], reply_markup=kb, parse_mode="Markdown")


async def _enqueue_batch_downloads(bot: Bot, chat_id: int, targets: list[str], dest_dir: Path) -> list[str]:
    task_ids = []
    total = len(targets)
    for idx, t_link in enumerate(targets, start=1):
        label = f"دانلود هوشمند [{idx}/{total}] در {dest_dir.name}" if total > 1 else f"دانلود هوشمند در {dest_dir.name}"
        task = await queue_engine.add_task(
            "download",
            label,
            _smart_download_pipeline_job,
            bot,
            chat_id,
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
        "📥 **دانلود هوشمند فیلم یا سریال (تکی و گروهی Batch)**\n\n"
        "🔗 لطفاً **یک یا چند لینک (مستقیم، Magnet، Mega.nz، Google Drive)** را ارسال کنید:\n"
        "• برای **دانلود گروهی (Batch)**، بین هر لینک یک خط فاصله بگذارید.\n"
        "• پس از دانلود، اگر فایل زیپ باشد خودکار خارج می‌شود، نام آن از روی قسمت‌های قبلی سرور کپی (یا استانداردسازی) شده و در همان پوشه در سرور آپلود می‌گردد:",
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

    await state.clear()
    await _show_link_purpose_prompt(message, targets)


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
        target_dir = Path(subfolders[idx]).resolve()
        if AutomationService.SEASON_DIR_REGEX.match(target_dir.name):
            parts_low = [part.casefold() for part in target_dir.parts]
            category = "Anime" if "anime" in parts_low else "Series"
            await AutomationService.create_season_structure_from_server(
                target_dir,
                category=category,
            )
        await _render_download_folder_browser(call, state, target_dir, page=0)


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

    task_ids = await _enqueue_batch_downloads(call.bot, call.message.chat.id, targets, chosen_dir)
    await state.clear()

    ids_str = ", ".join([f"`{tid}`" for tid in task_ids])
    remote_eq = AutomationService.compute_server_remote_dir_for_local_path(chosen_dir)
    await call.message.edit_text(
        f"✅ تعداد **`{len(task_ids)}` دانلود هوشمند** در صف قرار گرفت!\n"
        f"📂 مسیر ذخیره لوکال:\n`{chosen_dir}`\n"
        f"☁️ مسیر متناظر در سرور اصلی:\n`{remote_eq}`\n"
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
        "✏️ نام پوشه را ارسال کنید. اگر نام فصل مثل `S01` باشد، برنامه پوشه‌های کیفیت را از مسیر متناظر سرور می‌خواند و همان ساختار را در Workspace می‌سازد:",
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

    # ایجاد فصل جدید از داخل S01 یا زیرپوشه‌های آن باید در کنار S01 انجام شود.
    if AutomationService.SEASON_DIR_REGEX.match(raw_name):
        for ancestor in (curr_dir, curr_dir.parent, curr_dir.parent.parent):
            if ancestor and AutomationService.SEASON_DIR_REGEX.match(ancestor.name):
                curr_dir = ancestor.parent
                break

    new_dir = (curr_dir / raw_name).resolve()
    new_dir.mkdir(parents=True, exist_ok=True)

    parts_low = [p.lower() for p in new_dir.parts]
    if ("movies" in parts_low or "movie" in parts_low) and new_dir.parent.name.lower() in ("movies", "movie"):
        AutomationService.create_movie_structure(new_dir)
    elif AutomationService.SEASON_DIR_REGEX.match(new_dir.name):
        category = "Anime" if "anime" in parts_low else "Series"
        await AutomationService.create_season_structure_from_server(
            new_dir,
            category=category,
        )

    await message.answer(f"✅ پوشه `{new_dir.name}` ساخته شد و وارد آن شدید!", parse_mode="Markdown")
    await _render_download_folder_browser(message, state, new_dir, page=0)


# ==================== ۳. استخراج زیرنویس ====================
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


async def _show_link_purpose_prompt(
    message: Message,
    targets: list[str],
):
    token = uuid.uuid4().hex[:8]
    PENDING_LINK_ACTIONS[token] = {
        "targets": list(targets),
        "reference": targets[0] if targets else "",
    }
    kb = InlineKeyboardBuilder()
    kb.row(
        InlineKeyboardButton(
            text="⬇️ دانلود فیلم / سریال",
            callback_data=f"lnk:purpose:{token}:download",
        )
    )
    kb.row(
        InlineKeyboardButton(
            text="📤 استخراج زیرنویس از فایل ویدئویی",
            callback_data=f"lnk:purpose:{token}:extract",
        )
    )
    await message.answer(
        "🔗 لینک دریافت شد. می‌خواهید با آن چه کاری انجام شود؟\n\n"
        "• برای دانلود، نام فیلم/سریال از روی نام فایل یا متن لینک استخراج می‌شود و "
        "پوشه‌های محتمل Workspace و سرور مقصد پیشنهاد داده می‌شوند.\n"
        "• برای استخراج، ویدئو دانلود و ترک‌های زیرنویس آن استخراج و در همین چت ارسال می‌شوند.",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(F.data.startswith("lnk:purpose:"))
async def cb_link_purpose(call: CallbackQuery, state: FSMContext):
    parts = call.data.split(":")
    if len(parts) != 4:
        return await call.answer("درخواست نامعتبر است.", show_alert=True)

    token, purpose = parts[2], parts[3]
    info = PENDING_LINK_ACTIONS.get(token)
    if not info:
        return await call.answer("این درخواست منقضی شده است؛ لینک را دوباره ارسال کنید.", show_alert=True)

    targets = list(info.get("targets", []))
    if not targets:
        PENDING_LINK_ACTIONS.pop(token, None)
        return await call.answer("لینکی برای پردازش ثبت نشده است.", show_alert=True)

    await state.clear()

    if purpose == "extract":
        PENDING_LINK_ACTIONS.pop(token, None)
        task_ids = []
        for target in targets:
            job_uid = uuid.uuid4().hex[:6]
            temp_dir = Path(cfg.get("paths", "workspace_dir")) / f"SubExtract_{job_uid}"
            temp_dir.mkdir(parents=True, exist_ok=True)
            task = await queue_engine.add_task(
                "download",
                f"دانلود + استخراج زیرنویس ({job_uid})",
                _download_and_extract_subs_job,
                call.bot,
                call.message.chat.id,
                target,
                temp_dir,
            )
            task_ids.append(task.task_id)

        ids = ", ".join(f"<code>{html.escape(str(tid))}</code>" for tid in task_ids)
        await call.message.edit_text(
            f"📤 تعداد <b>{len(task_ids)}</b> عملیات دانلود و استخراج زیرنویس در صف قرار گرفت.\n"
            f"شناسه‌ها: {ids}",
            parse_mode="HTML",
        )
        return await call.answer("در صف قرار گرفت.")

    if purpose != "download":
        return await call.answer("نوع عملیات نامعتبر است.", show_alert=True)

    wait_msg = await call.message.edit_text(
        "🔎 در حال جستجوی عنوان لینک در پوشه‌های Workspace و سرور مقصد..."
    )
    candidates = []
    query = ""
    try:
        query = AutomationService.extract_media_title_query(info.get("reference", ""))
        candidates = await AutomationService.search_media_destination_candidates(
            info.get("reference", ""),
            limit=7,
        )
    except Exception as exc:
        logging.exception("Destination suggestion search failed")
        await wait_msg.edit_text(
            f"⚠️ جستجوی پیشنهادها انجام نشد: <code>{html.escape(str(exc)[:250])}</code>\n"
            "می‌توانید مقصد را دستی انتخاب کنید.",
            parse_mode="HTML",
        )

    PENDING_LINK_ACTIONS[token]["candidates"] = candidates
    kb = InlineKeyboardBuilder()

    if candidates:
        lines = [
            f"<b>{idx + 1}.</b> {html.escape(item['label'])} "
            f"— <code>{html.escape(item['category'])}</code> "
            f"({'محلی' if item.get('exists_local') else 'فقط در سرور'})"
            for idx, item in enumerate(candidates)
        ]
        for idx, item in enumerate(candidates):
            kb.row(
                InlineKeyboardButton(
                    text=f"📁 {item['label'][:42]}",
                    callback_data=f"lnk:candidate:{token}:{idx}",
                )
            )
        body = (
            f"🔎 عنوان استخراج‌شده: <b>{html.escape(query or 'قابل تشخیص نبود')}</b>\n\n"
            "پوشه‌های پیشنهادی؛ مقصد مناسب را انتخاب کنید:\n"
            + "\n".join(lines)
        )
    else:
        body = (
            f"🔎 عنوان استخراج‌شده: <b>{html.escape(query or 'قابل تشخیص نبود')}</b>\n\n"
            "پوشهٔ مطمئنی در Workspace یا فهرست پوشه‌های سرور پیدا نشد. "
            "می‌توانید مقصد را به‌صورت دستی از Workspace انتخاب کنید."
        )

    kb.row(
        InlineKeyboardButton(
            text="📂 انتخاب دستی مقصد",
            callback_data=f"lnk:manual:{token}",
        )
    )
    kb.row(InlineKeyboardButton(text="❌ لغو", callback_data=f"lnk:cancel:{token}"))
    await wait_msg.edit_text(
        body[:3900],
        reply_markup=kb.as_markup(),
        parse_mode="HTML",
        disable_web_page_preview=True,
    )


@router.callback_query(F.data.startswith("lnk:candidate:"))
async def cb_link_candidate_selected(call: CallbackQuery, state: FSMContext):
    parts = call.data.split(":")
    if len(parts) != 4:
        return await call.answer("درخواست نامعتبر است.", show_alert=True)
    token, idx_text = parts[2], parts[3]
    info = PENDING_LINK_ACTIONS.get(token)
    if not info:
        return await call.answer("این پیشنهاد منقضی شده است.", show_alert=True)

    try:
        candidate = info.get("candidates", [])[int(idx_text)]
    except (IndexError, ValueError, TypeError):
        return await call.answer("پوشهٔ انتخاب‌شده معتبر نیست.", show_alert=True)

    info["selected_dir"] = candidate["path"]
    info["selected_label"] = candidate["label"]
    chosen_dir = Path(candidate["path"]).resolve()
    category = str(candidate.get("category", "")).strip()
    reference = str(info.get("reference", "") or "")
    episode_tag = (
        candidate.get("episode_tag")
        or AutomationService.extract_episode_tag(reference)
        or ""
    )

    # انتخاب یک پیشنهاد Series/Anime، عنوان را تأیید می‌کند؛
    # برای جلوگیری از دانلود در ریشه عنوان، ساختار فصل باید ابتدا آماده شود.
    if category in ("Series", "Anime"):
        if not AutomationService.SEASON_DIR_REGEX.match(chosen_dir.name):
            if episode_tag:
                season_name = f"S{int(episode_tag[1:3]):02d}"
                chosen_dir = chosen_dir / season_name

        chosen_dir.mkdir(parents=True, exist_ok=True)
        await call.answer("عنوان تأیید شد؛ ساختار فصل از سرور بررسی و همگام می‌شود.")

        if AutomationService.SEASON_DIR_REGEX.match(chosen_dir.name):
            await AutomationService.create_season_structure_from_server(
                chosen_dir,
                category=category,
            )
            # در صورت وجود EBTV روی مقصد، همگام‌سازی FullHD را نیز ایجاد می‌کند.
            preferred_download_dir = (
                chosen_dir / "FullHD"
                if (chosen_dir / "FullHD").is_dir()
                else chosen_dir
            )
            await state.update_data(
                pending_direct_targets=list(info.get("targets", [])),
                dl_smart_series_title=candidate.get("name", chosen_dir.parent.name),
                dl_smart_season=chosen_dir.name,
                dl_smart_season_dir=str(chosen_dir),
            )
            PENDING_LINK_ACTIONS.pop(token, None)
            await _render_download_folder_browser(
                call,
                state,
                preferred_download_dir,
                page=0,
            )
            return

        # اگر شماره فصل از لینک قابل تشخیص نبود و سرور هم فصلی را پیشنهاد نکرد،
        # کاربر باید از داخل پوشه سریال، فصل مناسب را انتخاب کند.
        await state.update_data(
            pending_direct_targets=list(info.get("targets", [])),
            dl_smart_series_title=candidate.get("name", chosen_dir.name),
        )
        PENDING_LINK_ACTIONS.pop(token, None)
        await _render_download_folder_browser(call, state, chosen_dir, page=0)
        return

    # برای فیلم سینمایی، همان مسیر عنوان مقصد مستقیم است.
    kb = InlineKeyboardBuilder()
    kb.row(
        InlineKeyboardButton(
            text="⬇️ شروع دانلود در این پوشه",
            callback_data=f"lnk:start:{token}",
        )
    )
    kb.row(
        InlineKeyboardButton(
            text="📂 انتخاب دستی پوشه دیگر",
            callback_data=f"lnk:manual:{token}",
        )
    )
    kb.row(InlineKeyboardButton(text="🔙 بازگشت به پیشنهادها", callback_data=f"lnk:purpose:{token}:download"))
    await call.message.edit_text(
        f"✅ مقصد انتخاب‌شده: <b>{html.escape(candidate['label'])}</b>\n"
        f"📂 مسیر محلی: <code>{html.escape(str(chosen_dir))}</code>\n"
        f"☁️ مسیر متناظر سرور: <code>{html.escape(candidate['remote_path'])}</code>\n\n"
        "با تأیید زیر دانلود شروع می‌شود.",
        reply_markup=kb.as_markup(),
        parse_mode="HTML",
    )


@router.callback_query(F.data.startswith("lnk:start:"))
async def cb_link_start_download(call: CallbackQuery, state: FSMContext):
    token = call.data.split(":")[2]
    info = PENDING_LINK_ACTIONS.pop(token, None)
    if not info or not info.get("selected_dir"):
        return await call.answer("اطلاعات مقصد منقضی شده است.", show_alert=True)

    targets = list(info.get("targets", []))
    chosen_dir = Path(info["selected_dir"]).resolve()
    chosen_dir.mkdir(parents=True, exist_ok=True)
    task_ids = await _enqueue_batch_downloads(call.bot, call.message.chat.id, targets, chosen_dir)
    await state.clear()
    remote_eq = AutomationService.compute_server_remote_dir_for_local_path(chosen_dir)
    await call.message.edit_text(
        f"✅ تعداد <b>{len(task_ids)}</b> دانلود در صف قرار گرفت.\n"
        f"📂 مقصد: <code>{html.escape(str(chosen_dir))}</code>\n"
        f"☁️ مسیر سرور: <code>{html.escape(remote_eq)}</code>\n"
        f"🆔 شناسه‌ها: {', '.join(html.escape(str(x)) for x in task_ids)}",
        reply_markup=main_menu_kb(),
        parse_mode="HTML",
    )


@router.callback_query(F.data.startswith("lnk:manual:"))
async def cb_link_manual_destination(call: CallbackQuery, state: FSMContext):
    token = call.data.split(":")[2]
    info = PENDING_LINK_ACTIONS.pop(token, None)
    if not info:
        return await call.answer("این درخواست منقضی شده است.", show_alert=True)

    await state.update_data(pending_direct_targets=list(info.get("targets", [])))
    workspace = Path(cfg.get("paths", "workspace_dir")).resolve()
    await _render_download_folder_browser(call, state, workspace, page=0)


@router.callback_query(F.data.startswith("lnk:cancel:"))
async def cb_link_cancel(call: CallbackQuery):
    token = call.data.split(":")[2]
    PENDING_LINK_ACTIONS.pop(token, None)
    await call.message.edit_text("⏭ عملیات لینک لغو شد.", reply_markup=main_menu_kb())


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
    await _show_link_purpose_prompt(message, targets)
