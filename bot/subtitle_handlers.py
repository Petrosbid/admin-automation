import asyncio
import html
import logging
import math
import re
import shutil
import uuid
from pathlib import Path
from typing import Any
from aiogram import Router, F
from aiogram.types import Message, CallbackQuery, FSInputFile, InlineKeyboardButton
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.utils.keyboard import InlineKeyboardBuilder

from core.config_loader import cfg
from core.queue_manager import queue_engine
from services.downloader_service import DownloaderService
from services.subtitle_service import SubtitleService
from services.mkv_service import MKVService
from services.handbrake_service import HandBrakeService
from services.automation_service import AutomationService, AutomationPlan
from services.cuteftp_service import CuteFTPService
from bot.keyboards import (
    BROWSER_PAGE_SIZE, main_menu_kb, subtitle_folder_browser_kb,
    subtitle_source_kb, subtitle_file_found_kb, sync_options_kb,
    subtitle_mode_kb, skip_or_cancel_kb, presets_kb,
    output_destination_choice_kb, output_dest_tree_browser_kb,
    smart_auto_browser_kb, smart_auto_sub_name_kb,
    smart_auto_missing_en_kb, smart_auto_final_selection_kb,
    manual_encode_ass_qualities_kb
)

router = Router()

# نگه‌داشتن reference تسک‌های مانیتور تا زمان پایان آن‌ها.
_ACTIVE_AUTOMATION_MONITORS: set[asyncio.Task] = set()
PENDING_INCOMING_SUBTITLES: dict[str, dict] = {}
_PENDING_SUBTITLE_ALBUMS: dict[tuple[int, int, str, str], dict[str, Any]] = {}


def _release_automation_monitor(task: asyncio.Task):
    _ACTIVE_AUTOMATION_MONITORS.discard(task)
    if task.cancelled():
        return
    try:
        error = task.exception()
    except asyncio.CancelledError:
        return
    if error:
        logging.error(
            "Automation status monitor failed: %s",
            error,
            exc_info=(type(error), error, error.__traceback__),
        )


async def _collect_subtitle_documents(
        messages: list[Message],
        destination_root: Path,
) -> tuple[list[Path], Path]:
    """Download one Telegram document/album into isolated staging folders and extract subtitle archives."""
    batch_root = destination_root / "_incoming_subs" / uuid.uuid4().hex[:10]
    batch_root.mkdir(parents=True, exist_ok=True)
    candidates: list[Path] = []

    try:
        for index, message in enumerate(sorted(messages, key=lambda item: item.message_id), start=1):
            document = message.document
            if not document:
                continue

            raw_name = document.file_name or f"subtitle_{index}.srt"
            safe_name = re.sub(r'[<>:"/\\\\|?*]', "_", raw_name.replace("\\", "/").split("/")[-1]).strip()
            if not safe_name:
                safe_name = f"subtitle_{index}.srt"

            file_path = batch_root / f"file_{index:02d}" / safe_name
            file_path.parent.mkdir(parents=True, exist_ok=True)
            suffix = file_path.suffix.lower()

            if suffix not in SubtitleService.SUB_EXTENSIONS and suffix not in SubtitleService.ARCHIVE_EXTENSIONS:
                continue

            await message.bot.download(document, destination=file_path)

            if suffix in SubtitleService.ARCHIVE_EXTENSIONS:
                extracted = SubtitleService.extract_archive(
                    file_path,
                    file_path.parent,
                    remove_archive=True,
                )
                candidates.extend(
                    Path(item).resolve()
                    for item in extracted
                    if Path(item).suffix.lower() in SubtitleService.SUB_EXTENSIONS
                )
            else:
                candidates.append(file_path.resolve())
    except Exception:
        shutil.rmtree(batch_root, ignore_errors=True)
        raise

    valid: list[Path] = []
    for subtitle_path in candidates:
        if not subtitle_path.exists() or subtitle_path.suffix.lower() not in SubtitleService.SUB_EXTENSIONS:
            continue
        try:
            SubtitleService.normalize_subtitle_file(subtitle_path)
        except Exception as exc:
            logging.warning("Could not normalize incoming subtitle %s: %s", subtitle_path.name, exc)
        valid.append(subtitle_path.resolve())

    fallback_season = AutomationService.extract_season_num_from_dir(destination_root)
    valid.sort(
        key=lambda item: _subtitle_episode_sort_key(item, fallback_season)
    )

    if not valid:
        shutil.rmtree(batch_root, ignore_errors=True)
        raise ValueError("هیچ فایل زیرنویس معتبری در فایل‌های ارسالی یا آرشیوها پیدا نشد.")

    return valid, batch_root


def _subtitle_episode_sort_key(subtitle_path: Path, fallback_season: int | None = None) -> tuple:
    episode_tag = AutomationService.extract_episode_tag(
        subtitle_path.name,
        fallback_season=fallback_season,
    )
    if episode_tag:
        match = re.search(r"S(\d{1,2})E(\d{1,3})", episode_tag, flags=re.IGNORECASE)
        if match:
            return (0, int(match.group(1)), int(match.group(2)), subtitle_path.name.casefold())
    episode_number = AutomationService.extract_episode_number_only(subtitle_path.name)
    if episode_number is not None:
        return (0, fallback_season or 0, episode_number, subtitle_path.name.casefold())
    return (1, 999, 999, subtitle_path.name.casefold())


def _persist_matched_subtitles(
        matched: dict[str, Path],
        destination_dir: Path,
        batch_root: Path,
) -> dict[str, Path]:
    """Move only matched items out of the temporary album folder; keep FSM paths stable for partial batches."""
    destination_dir.mkdir(parents=True, exist_ok=True)
    persisted: dict[str, Path] = {}
    for episode_key, source_path in matched.items():
        source_path = Path(source_path)
        if not source_path.exists():
            continue
        target_path = destination_dir / source_path.name
        if target_path.exists() and target_path.resolve() != source_path.resolve():
            target_path = destination_dir / f"{source_path.stem}__{uuid.uuid4().hex[:6]}{source_path.suffix}"
        if source_path.resolve() != target_path.resolve():
            shutil.move(str(source_path), str(target_path))
        persisted[episode_key] = target_path.resolve()

    shutil.rmtree(batch_root, ignore_errors=True)
    return persisted


async def _queue_sauto_subtitle_album(
        message: Message,
        state: FSMContext,
        language: str,
) -> bool:
    """Collect Telegram document-album updates and process the whole album once it has arrived."""
    media_group_id = message.media_group_id
    if not media_group_id:
        return False

    user_id = message.from_user.id if message.from_user else 0
    key = (message.chat.id, user_id, str(media_group_id), language)
    current_state = await state.get_state()
    entry = _PENDING_SUBTITLE_ALBUMS.get(key)

    if entry is None:
        entry = {
            "messages": [],
            "state": current_state,
            "fsm": state,
            "task": None,
        }
        _PENDING_SUBTITLE_ALBUMS[key] = entry

    if all(item.message_id != message.message_id for item in entry["messages"]):
        entry["messages"].append(message)

    old_task = entry.get("task")
    if old_task and not old_task.done():
        old_task.cancel()

    async def flush_album():
        try:
            # Telegram delivers each document in an album as a separate update with the same media_group_id.
            await asyncio.sleep(1.8)
            active = _PENDING_SUBTITLE_ALBUMS.get(key)
            if active is not entry:
                return
            _PENDING_SUBTITLE_ALBUMS.pop(key, None)

            messages = sorted(entry["messages"], key=lambda item: item.message_id)
            fsm = entry["fsm"]
            if await fsm.get_state() != entry["state"]:
                await messages[0].answer(
                    "⚠️ مرحله اتوماسیون هنگام دریافت آلبوم تغییر کرده است؛ فایل‌ها پردازش نشدند. دوباره در مرحله زیرنویس ارسال کنید."
                )
                return

            if language == "FA":
                await _process_sauto_fa_sub_messages(messages, fsm)
            else:
                await _process_sauto_en_sub_messages(messages, fsm)
        except asyncio.CancelledError:
            raise
        except Exception:
            logging.exception("Failed to process %s subtitle album", language)
            try:
                await message.answer("❌ پردازش دسته‌ای زیرنویس‌ها با خطا مواجه شد. لطفاً دوباره ارسال کنید.")
            except Exception:
                pass

    entry["task"] = asyncio.create_task(flush_album())
    return True


class SubtitleStates(StatesGroup):
    sauto_browsing_dir = State()
    sauto_waiting_fa_sub = State()
    sauto_waiting_fa_sub_name = State()
    sauto_waiting_en_decision = State()
    sauto_waiting_en_upload = State()
    sauto_waiting_hb_qualities = State()

    selecting_target_videos = State()
    waiting_manual_initial_mode = State()
    waiting_manual_encode_ass_qualities = State()
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


def is_admin(user_id: int) -> bool:
    return user_id in cfg.get("telegram", "admin_ids", [])


def _natural_sort_key(path_str: str) -> list:
    name = Path(path_str).name.lower()
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", name)]


# ==============================================================================
# بخش اول: اتوماسیون کامل و هوشمند فیلم و چندقسمتی سریال (همراه با صفحه‌بندی پوشه‌ها)
# ==============================================================================

async def _render_smart_auto_browser(
        call_or_msg: CallbackQuery | Message,
        state: FSMContext,
        current_dir: Path,
        page: int = 0
):
    workspace = Path(cfg.get("paths", "workspace_dir")).resolve()
    AutomationService.ensure_workspace_root_categories()
    current_dir = current_dir.resolve()
    current_dir.mkdir(parents=True, exist_ok=True)

    folders = sorted(
        [
            p for p in current_dir.iterdir()
            if p.is_dir()
               and not p.name.startswith("_")
               and p.name != "browser_profile"
               and not p.name.lower().endswith("-softsub")
               and not p.name.lower().endswith("-hardsub")
               and p.name.lower() not in ("softsub", "hardsub", "srt", "sub")
        ],
        key=lambda x: x.name.lower()
    )
    detected_eps = AutomationService.scan_available_episodes_in_season(current_dir)
    is_root = (current_dir == workspace)

    total_folders = len(folders)
    total_pages = max(1, math.ceil(total_folders / BROWSER_PAGE_SIZE))
    page = max(0, min(page, total_pages - 1))
    start_idx = page * BROWSER_PAGE_SIZE
    end_idx = start_idx + BROWSER_PAGE_SIZE

    data = await state.get_data()
    prev_dir = data.get("sauto_curr_dir")
    selected_eps = set(data.get("sauto_selected_eps", [])) if prev_dir == str(current_dir) else set()
    selected_eps = {ep for ep in selected_eps if ep in detected_eps}

    await state.set_state(SubtitleStates.sauto_browsing_dir)
    await state.update_data(
        sauto_curr_dir=str(current_dir),
        sauto_subfolders=[str(f.resolve()) for f in folders],
        sauto_detected_eps=detected_eps,
        sauto_selected_eps=sorted(list(selected_eps)),
        sauto_page=page
    )

    folder_lines = [
        f"`[{i + 1}]` 📁 `{folders[i].name}`"
        for i in range(start_idx, min(end_idx, total_folders))
    ]
    list_block = "\n".join(folder_lines) if folder_lines else "_(زیرپوشه‌ای وجود ندارد)_"

    ep_badge = ""
    if detected_eps:
        ep_badge = (
            f"\n🎞 **قسمت‌های موجود در این فصل ({len(detected_eps)} قسمت):**\n"
            f"`{', '.join(detected_eps)}`\n"
            f"✅ **قسمت‌های انتخاب‌شده:** `{len(selected_eps)}` قسمت\n"
        )

    page_badge = f" (صفحه {page + 1} از {total_pages} - کل: {total_folders})" if total_pages > 1 else ""

    text = (
        f"🚀 **اتوماسیون کامل و هوشمند{page_badge}**\n"
        f"📂 مسیر فعلی:\n`{current_dir}`\n"
        f"{ep_badge}\n"
        f"📋 **پوشه‌های این صفحه:**\n{list_block}\n\n"
        "💡 **راهنما:**\n"
        "• برای **سریال / انیمه**: وارد پوشه فصل (مثلاً `Series/Name/S01`) شوید، قسمت‌های مورد نظر را تیک (`✅`) بزنید و دکمه **«🚀 شروع اتوماسیون»** را فشار دهید.\n"
        "• برای **فیلم**: وارد پوشه فیلم در `Movies/` شوید و دکمه اجرای اتوماسیون را بزنید."
    )
    kb = smart_auto_browser_kb(folders, current_dir, is_root, detected_eps, selected_eps, page=page)

    if isinstance(call_or_msg, CallbackQuery):
        try:
            await call_or_msg.message.edit_text(text[:4000], reply_markup=kb, parse_mode="Markdown")
        except Exception:
            await call_or_msg.answer()
    else:
        await call_or_msg.answer(text[:4000], reply_markup=kb, parse_mode="Markdown")


@router.callback_query(F.data == "menu:smart_auto")
async def cb_smart_auto_start(call: CallbackQuery, state: FSMContext):
    await state.clear()
    workspace = Path(cfg.get("paths", "workspace_dir")).resolve()
    await _render_smart_auto_browser(call, state, workspace, page=0)


@router.callback_query(F.data == "fact:run_smart_auto_here")
async def cb_run_smart_auto_from_filemgr(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    curr_dir = Path(data.get("current_dir", cfg.get("paths", "workspace_dir"))).resolve()
    await state.update_data(sauto_selected_eps=[])
    await _render_smart_auto_browser(call, state, curr_dir, page=0)


@router.callback_query(SubtitleStates.sauto_browsing_dir, F.data.startswith("sauto:page:"))
async def cb_sauto_change_page(call: CallbackQuery, state: FSMContext):
    page = int(call.data.split(":")[2])
    data = await state.get_data()
    curr_dir = Path(data.get("sauto_curr_dir", cfg.get("paths", "workspace_dir"))).resolve()
    await _render_smart_auto_browser(call, state, curr_dir, page=page)


@router.callback_query(SubtitleStates.sauto_browsing_dir, F.data.startswith("sauto:open:"))
async def cb_sauto_open_dir(call: CallbackQuery, state: FSMContext):
    idx = int(call.data.split(":")[2])
    data = await state.get_data()
    subfolders = data.get("sauto_subfolders", [])
    if idx < len(subfolders):
        await _render_smart_auto_browser(call, state, Path(subfolders[idx]), page=0)


@router.callback_query(SubtitleStates.sauto_browsing_dir, F.data == "sauto:up")
async def cb_sauto_up(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    curr = Path(data.get("sauto_curr_dir", cfg.get("paths", "workspace_dir"))).resolve()
    workspace = Path(cfg.get("paths", "workspace_dir")).resolve()
    parent = curr.parent if curr != workspace else workspace
    await _render_smart_auto_browser(call, state, parent, page=0)


@router.callback_query(SubtitleStates.sauto_browsing_dir, F.data.startswith("sauto:ep_toggle:"))
async def cb_sauto_toggle_episode(call: CallbackQuery, state: FSMContext):
    ep_tag = call.data.split(":")[2]
    data = await state.get_data()
    selected = set(data.get("sauto_selected_eps", []))
    if ep_tag in selected:
        selected.remove(ep_tag)
    else:
        selected.add(ep_tag)
    await state.update_data(sauto_selected_eps=sorted(list(selected)))
    curr_dir = Path(data["sauto_curr_dir"]).resolve()
    await _render_smart_auto_browser(call, state, curr_dir, page=data.get("sauto_page", 0))


@router.callback_query(SubtitleStates.sauto_browsing_dir, F.data == "sauto:ep_all")
async def cb_sauto_toggle_all_episodes(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    detected = data.get("sauto_detected_eps", [])
    selected = set(data.get("sauto_selected_eps", []))
    if detected and all(ep in selected for ep in detected):
        selected.clear()
    else:
        selected = set(detected)
    await state.update_data(sauto_selected_eps=sorted(list(selected)))
    curr_dir = Path(data["sauto_curr_dir"]).resolve()
    await _render_smart_auto_browser(call, state, curr_dir, page=data.get("sauto_page", 0))


@router.callback_query(SubtitleStates.sauto_browsing_dir, F.data == "sauto:ep_proceed")
async def cb_sauto_proceed_selected_episodes(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    selected_eps = sorted(data.get("sauto_selected_eps", []))
    if not selected_eps:
        return await call.answer("⚠️ حداقل یک قسمت را تیک بزنید!", show_alert=True)
    curr_dir = Path(data["sauto_curr_dir"]).resolve()
    incoming = data.get("sauto_incoming_subs", [])
    if incoming:
        return await _show_incoming_subtitle_actions(
            call, curr_dir, [Path(p) for p in incoming], selected_eps
        )
    await _init_smart_auto_batch(call, state, curr_dir, selected_eps)


@router.callback_query(SubtitleStates.sauto_browsing_dir, F.data == "sauto:select_dir")
async def cb_sauto_select_dir(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    curr_dir = Path(data["sauto_curr_dir"]).resolve()
    eps = AutomationService.scan_available_episodes_in_season(curr_dir)
    incoming = data.get("sauto_incoming_subs", [])
    if incoming:
        return await _show_incoming_subtitle_actions(
            call, curr_dir, [Path(p) for p in incoming], eps
        )
    await _init_smart_auto_batch(call, state, curr_dir, eps)


async def _init_smart_auto_batch(
        call_or_msg: CallbackQuery | Message,
        state: FSMContext,
        target_dir: Path,
        selected_episodes: list[str],
        pre_uploaded_subs: list[Path] | None = None
):
    plans = await AutomationService.analyze_multi_episodes_plan(target_dir, selected_episodes)
    if not plans:
        msg = f"⚠️ هیچ فایل ویدئویی خامی در پوشه `{target_dir.name}` یافت نشد!"
        if isinstance(call_or_msg, CallbackQuery):
            return await call_or_msg.answer(msg, show_alert=True)
        return await call_or_msg.answer(msg, parse_mode="Markdown")

    first_plan = plans[0]
    ep_tags = [p.season_ep_tag for p in plans if p.season_ep_tag]
    selected_ep_nums = {int(ep.split("E")[1]) for ep in ep_tags if "E" in ep}

    # فایل‌های ارسالی کاربر را پیش از تحلیل/اتوماسیون در پوشه استاندارد FA یا EN قرار بده.
    # در غیر این صورت Queue ممکن است فایل موقت _incoming_subs را به‌عنوان مسیر مقصد FTP تشخیص دهد.
    placed_fa_subs: list[Path] = []
    for source_sub in pre_uploaded_subs or []:
        source_sub = Path(source_sub)
        if not source_sub.exists() or source_sub.suffix.lower() not in SubtitleService.SUB_EXTENSIONS:
            continue

        low_sub_name = source_sub.name.casefold()
        is_english_upload = (
                low_sub_name.startswith("en_")
                or ".en." in low_sub_name
                or low_sub_name.endswith(("_en.srt", "_en.ass", "_en.ssa"))
        )
        if is_english_upload and first_plan.category != "Anime":
            destination_dir = first_plan.en_sub_dest_dir
        else:
            destination_dir = first_plan.fa_sub_dest_dir

        destination_dir.mkdir(parents=True, exist_ok=True)
        destination_sub = destination_dir / source_sub.name
        if source_sub.resolve() != destination_sub.resolve():
            shutil.copy2(source_sub, destination_sub)
        if destination_sub.suffix.lower() == ".srt":
            SubtitleService.normalize_srt_utf8(destination_sub)

        if is_english_upload and first_plan.category != "Anime":
            fallback_season = AutomationService.extract_season_num_from_dir(first_plan.base_dir)
            sub_ep = AutomationService.extract_episode_tag(
                destination_sub.name,
                fallback_season=fallback_season,
            )
            for plan_item in plans:
                if not sub_ep or not plan_item.season_ep_tag or plan_item.season_ep_tag == sub_ep:
                    plan_item.existing_en_sub = destination_sub.resolve()
        else:
            placed_fa_subs.append(destination_sub.resolve())

    if pre_uploaded_subs is not None:
        pre_uploaded_subs = placed_fa_subs

    await state.update_data(
        sauto_target_dir=str(target_dir),
        sauto_ep_tags=ep_tags,
        sauto_is_series=first_plan.is_series,
        sauto_fa_dest_dir=str(first_plan.fa_sub_dest_dir),
        sauto_en_dest_dir=str(first_plan.en_sub_dest_dir),
        sauto_fa_sub_map={},
        sauto_en_sub_map={},
    )

    raw_local_subs = AutomationService.collect_all_local_fa_subtitles_in_dir(
        first_plan.base_dir, first_plan.fa_sub_dest_dir
    )

    valid_local_subs: list[Path] = []
    for sub_p in raw_local_subs:
        if not first_plan.is_series or not ep_tags:
            valid_local_subs.append(sub_p)
            continue

        sub_s_e = AutomationService.extract_episode_tag(sub_p.name)
        sub_e_num = AutomationService.extract_episode_number_only(sub_p.name)

        if sub_s_e:
            if sub_s_e in ep_tags:
                valid_local_subs.append(sub_p)
        elif sub_e_num is not None:
            if sub_e_num in selected_ep_nums:
                valid_local_subs.append(sub_p)
        else:
            valid_local_subs.append(sub_p)

    all_candidate_subs = (pre_uploaded_subs or []) + [
        p for p in valid_local_subs if p not in (pre_uploaded_subs or [])
    ]

    matched_map = AutomationService.match_subtitles_to_episodes(plans, all_candidate_subs)

    required_keys = ep_tags if ep_tags else ["MOVIE"]
    if all(k in matched_map for k in required_keys):
        return await _place_and_ask_fa_sub_names(call_or_msg, state, plans, matched_map)

    await state.set_state(SubtitleStates.sauto_waiting_fa_sub)
    await state.update_data(
        sauto_partial_fa_map={k: str(v) for k, v in matched_map.items()},
        sauto_existing_fa_candidates=[str(p) for p in all_candidate_subs]
    )

    kb = InlineKeyboardBuilder()
    if matched_map:
        kb.row(
            InlineKeyboardButton(
                text=f"▶️ ادامه با همین {len(matched_map)} زیرنویس یافت‌شده",
                callback_data="sauto_fa:use_partial"
            )
        )
    if len(plans) == 1 and all_candidate_subs:
        for idx, f_sub in enumerate(all_candidate_subs[:10]):
            kb.row(
                InlineKeyboardButton(
                    text=f"📄 [{idx + 1}] انتخاب دستی: {f_sub.name[:34]}",
                    callback_data=f"sauto_pickfa:{idx}"
                )
            )
    kb.row(InlineKeyboardButton(text="🔙 انصراف", callback_data="menu:home"))

    total_soft = sum(len(p.softsub_targets) for p in plans)
    total_fhd = sum(1 for p in plans if p.fullhd_source_video)

    status_lines = []
    for p in plans:
        key = p.season_ep_tag or "MOVIE"
        if key in matched_map:
            status_lines.append(f"✅ `{key}`: `{matched_map[key].name}`")
        else:
            status_lines.append(f"❌ `{key}`: _زیرنویس فارسی این قسمت در پوشه یافت نشد_")

    ep_status_block = "\n".join(status_lines)

    info_text = (
        f"🚀 **اتوماسیون هوشمند آماده شد!**\n\n"
        f"🎬 عنوان: **`{first_plan.series_title}`**\n"
        f"🎞 قسمت‌های انتخاب‌شده: **`{', '.join(ep_tags) if ep_tags else 'فیلم سینمایی'}`**\n"
        f"📦 مجموع فایل‌های خام برای سافت‌ساب: **`{total_soft}` فایل**\n"
        f"🔥 سورس‌های آماده هاردساب (`FullHD`): **`{total_fhd}` فایل**\n\n"
        f"📋 **وضعیت زیرنویس فارسی در پوشه:**\n{ep_status_block}\n\n"
        "📤 **می‌توانید زیرنویس‌های فارسی همه قسمت‌ها را یک‌جا به‌صورت آلبوم فایل ارسال کنید.** نام هر فایل باید شماره قسمت مثل E01 یا S02E01 را داشته باشد تا خودکار مرتب و به همان قسمت وصل شود. فرمت‌ها: `.srt` / `.ass` / `.ssa` / `.zip` / `.rar`."
    )

    if isinstance(call_or_msg, CallbackQuery):
        await call_or_msg.message.edit_text(info_text[:4000], reply_markup=kb.as_markup(), parse_mode="Markdown")
    else:
        await call_or_msg.answer(info_text[:4000], reply_markup=kb.as_markup(), parse_mode="Markdown")


@router.callback_query(SubtitleStates.sauto_waiting_fa_sub, F.data == "sauto_fa:use_partial")
async def cb_sauto_use_partial_fa(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    partial_map = {k: Path(v) for k, v in data.get("sauto_partial_fa_map", {}).items()}
    if not partial_map:
        return await call.answer("⚠️ هیچ زیرنویسی تطبیق نیافته است!", show_alert=True)

    target_dir = Path(data["sauto_target_dir"])
    ep_tags = [k for k in data.get("sauto_ep_tags", []) if k in partial_map]
    await state.update_data(sauto_ep_tags=ep_tags)
    plans = await AutomationService.analyze_multi_episodes_plan(target_dir, ep_tags)
    await _place_and_ask_fa_sub_names(call, state, plans, partial_map)


@router.callback_query(SubtitleStates.sauto_waiting_fa_sub, F.data.startswith("sauto_pickfa:"))
async def cb_sauto_pick_existing_fa(call: CallbackQuery, state: FSMContext):
    idx = int(call.data.split(":")[1])
    data = await state.get_data()
    cands = data.get("sauto_existing_fa_candidates", [])
    if idx >= len(cands):
        return await call.answer("⚠️ فایل یافت نشد.")
    chosen_fa = Path(cands[idx])
    target_dir = Path(data["sauto_target_dir"])
    ep_tags = data.get("sauto_ep_tags", [])
    plans = await AutomationService.analyze_multi_episodes_plan(target_dir, ep_tags)
    key = plans[0].season_ep_tag if (plans[0].is_series and plans[0].season_ep_tag) else "MOVIE"
    await _place_and_ask_fa_sub_names(call, state, plans, {key: chosen_fa})


async def _process_sauto_fa_sub_messages(messages: list[Message], state: FSMContext):
    message = messages[0]
    data = await state.get_data()
    target_dir = Path(data["sauto_target_dir"])
    ep_tags = data.get("sauto_ep_tags", [])
    plans = await AutomationService.analyze_multi_episodes_plan(target_dir, ep_tags)
    if not plans:
        return await message.answer("❌ برای این مسیر، پلن اتوماسیون معتبری پیدا نشد.")

    fa_dir = plans[0].fa_sub_dest_dir
    try:
        new_subs, batch_root = await _collect_subtitle_documents(messages, fa_dir)
    except Exception as exc:
        return await message.answer(f"❌ دریافت زیرنویس‌ها ناموفق بود: {html.escape(str(exc))}")

    fallback_season = AutomationService.extract_season_num_from_dir(plans[0].base_dir)
    new_subs.sort(key=lambda item: _subtitle_episode_sort_key(item, fallback_season))
    matched_map = AutomationService.match_subtitles_to_episodes(plans, new_subs)

    if len(plans) == 1 and len(new_subs) == 1 and not matched_map:
        only_key = plans[0].season_ep_tag if (plans[0].is_series and plans[0].season_ep_tag) else "MOVIE"
        matched_map[only_key] = new_subs[0]

    matched_source_paths = {Path(value).resolve() for value in matched_map.values()}
    persisted_map = _persist_matched_subtitles(matched_map, fa_dir, batch_root)
    partial_prev = {
        key: Path(value)
        for key, value in data.get("sauto_partial_fa_map", {}).items()
        if Path(value).exists()
    }
    partial_prev.update(persisted_map)

    required_keys = ep_tags if ep_tags else ["MOVIE"]
    if all(key in partial_prev for key in required_keys):
        ordered_map = {key: partial_prev[key] for key in required_keys}
        return await _place_and_ask_fa_sub_names(message, state, plans, ordered_map)

    await state.update_data(
        sauto_partial_fa_map={key: str(value) for key, value in partial_prev.items()}
    )
    missing_keys = [key for key in required_keys if key not in partial_prev]
    matched_lines = [
        f"✅ {key}: {Path(partial_prev[key]).name}"
        for key in required_keys
        if key in partial_prev
    ]
    unmatched_names = [
        Path(item).name
        for item in new_subs
        if Path(item).resolve() not in matched_source_paths
    ]

    kb = InlineKeyboardBuilder()
    if partial_prev:
        kb.row(
            InlineKeyboardButton(
                text=f"▶️ ادامه با همین {len(partial_prev)} قسمت تطبیق‌یافته",
                callback_data="sauto_fa:use_partial"
            )
        )
    kb.row(InlineKeyboardButton(text="🔙 انصراف", callback_data="menu:home"))

    status = "\n".join(matched_lines) if matched_lines else "هنوز زیرنویسی با شماره قسمت قابل‌تطبیق پیدا نشد."
    unmatched_note = ""
    if unmatched_names:
        unmatched_note = "\n\nفایل‌های بدون تطبیق:\n" + "\n".join(f"• {name}" for name in unmatched_names[:8])

    await message.answer(
        f"📥 تعداد {len(new_subs)} فایل زیرنویس دریافت شد و بر اساس شماره قسمت (Eyy) مرتب شد.\n"
        f"✅ تطبیق‌یافته: {len(partial_prev)} از {len(required_keys)} قسمت\n\n"
        f"{status}\n\n"
        f"⏳ قسمت‌های باقی‌مانده: {', '.join(missing_keys) if missing_keys else '—'}"
        f"{unmatched_note}\n\n"
        "می‌توانید زیرنویس‌های باقی‌مانده را در یک آلبوم دیگر بفرستید یا ادامه را انتخاب کنید:",
        reply_markup=kb.as_markup(),
    )


@router.message(SubtitleStates.sauto_waiting_fa_sub, F.document)
async def handle_sauto_fa_sub_upload(message: Message, state: FSMContext):
    if await _queue_sauto_subtitle_album(message, state, "FA"):
        return
    await _process_sauto_fa_sub_messages([message], state)


async def _place_and_ask_fa_sub_names(
        call_or_msg: CallbackQuery | Message,
        state: FSMContext,
        plans: list[AutomationPlan],
        fa_sub_map: dict[str, Path]
):
    first_plan = plans[0]
    fa_dir = first_plan.fa_sub_dest_dir
    fa_dir.mkdir(parents=True, exist_ok=True)

    placed_map: dict[str, str] = {}
    suggested_map: dict[str, str] = {}

    for p in plans:
        key = p.season_ep_tag if (p.is_series and p.season_ep_tag) else "MOVIE"
        sub_p = fa_sub_map.get(key)
        if not sub_p or not sub_p.exists():
            continue

        if sub_p.suffix.lower() == ".srt":
            SubtitleService.normalize_srt_utf8(sub_p)

        if sub_p.parent.resolve() != fa_dir.resolve():
            dest_p = fa_dir / sub_p.name
            if dest_p.exists():
                dest_p.unlink(missing_ok=True)
            shutil.move(str(sub_p), str(dest_p))
            sub_p = dest_p

        sug_name = AutomationService.build_standard_subtitle_filename(
            series_or_movie_title=p.series_title,
            season_ep_tag=p.season_ep_tag,
            source_type=p.source_type,
            lang="FA",
            ext=sub_p.suffix
        )

        placed_map[key] = str(sub_p.resolve())
        suggested_map[key] = sug_name

    await state.update_data(
        sauto_fa_sub_map=placed_map,
        sauto_suggested_fa_map=suggested_map
    )
    await state.set_state(SubtitleStates.sauto_waiting_fa_sub_name)

    if len(placed_map) == 1:
        only_key = next(iter(placed_map))
        curr_p = Path(placed_map[only_key])
        sug_n = suggested_map[only_key]
        text = (
            f"✅ زیرنویس فارسی در مسیر استاندارد قرار گرفت:\n"
            f"📂 `{fa_dir}`\n\n"
            f"📄 **نام فعلی زیرنویس:**\n`{curr_p.name}`\n"
            f"✨ **نام استاندارد پیشنهادی:**\n`{sug_n}`\n\n"
            "✏️ **نام فایل زیرنویس فارسی چه باشد؟** (روی دکمه‌ها کلیک کنید یا نام دلخواه را بفرستید):"
        )
        kb = smart_auto_sub_name_kb(sug_n, curr_p.name)
    else:
        preview_lines = [
            f"• `{k}`: `{Path(placed_map[k]).name}` ⬅️ `{suggested_map[k]}`"
            for k in sorted(placed_map.keys())[:12]
        ]
        first_sug = next(iter(suggested_map.values()))
        text = (
                f"✅ تعداد **`{len(placed_map)}` فایل زیرنویس فارسی** در پوشه `{fa_dir}` قرار گرفتند!\n\n"
                f"📋 **پیش‌نمایش نام‌های استاندارد پیشنهادی (`{{name}}.S{{xx}}E{{yy}}.{first_plan.source_type}.FA.srt`):**\n"
                + "\n".join(preview_lines)
                + "\n\n✏️ **نام زیرنویس‌ها چگونه تنظیم شود؟**"
        )
        kb = smart_auto_sub_name_kb(f"استاندارد برای همه ({first_sug})", "حفظ نام فعلی همه فایل‌ها")

    if isinstance(call_or_msg, CallbackQuery):
        await call_or_msg.message.edit_text(text[:4000], reply_markup=kb, parse_mode="Markdown")
    else:
        await call_or_msg.answer(text[:4000], reply_markup=kb, parse_mode="Markdown")


def _ensure_anime_dual_ass_and_srt_in_folder(plans: list[AutomationPlan], final_fa_map: dict[str, str]):
    """
    مخصوص انیمه:
    اگر زیرنویس فارسی با فرمت .ass یا .ssa باشد، علاوه بر نگه داشتن خود فایل .ass (برای سافت‌ساب/هاردساب)،
    یک نسخه کاملاً کانورت‌شده و تمیز با فرمت .srt هم در همان پوشه srt (بدون زیرپوشه) می‌سازد!
    """
    if not plans or plans[0].category != "Anime":
        return

    for key, path_str in list(final_fa_map.items()):
        sub_p = Path(path_str)
        if sub_p.exists() and sub_p.suffix.lower() in (".ass", ".ssa"):
            srt_sibling = sub_p.with_suffix(".srt")
            try:
                SubtitleService.convert_ass_to_srt_file(sub_p, srt_sibling)
            except Exception as e:
                logging.warning(f"Failed to convert Anime ASS to SRT for {sub_p.name}: {e}")


@router.callback_query(SubtitleStates.sauto_waiting_fa_sub_name, F.data.startswith("sauto_subname:"))
async def cb_sauto_fa_sub_name_choice(call: CallbackQuery, state: FSMContext):
    choice = call.data.split(":")[1]
    data = await state.get_data()
    placed_map: dict[str, str] = data.get("sauto_fa_sub_map", {})
    suggested_map: dict[str, str] = data.get("sauto_suggested_fa_map", {})
    final_fa_map: dict[str, str] = {}

    for key, path_str in placed_map.items():
        curr_fa = Path(path_str)
        if choice == "standard" and key in suggested_map:
            new_path = curr_fa.with_name(suggested_map[key])
            if curr_fa.resolve() != new_path.resolve():
                if new_path.exists():
                    new_path.unlink(missing_ok=True)
                curr_fa.rename(new_path)
                curr_fa = new_path
        SubtitleService.normalize_subtitle_file(curr_fa)
        final_fa_map[key] = str(curr_fa.resolve())

    await state.update_data(sauto_final_fa_map=final_fa_map)
    await _check_english_sub_batch_step(call, state)


@router.message(SubtitleStates.sauto_waiting_fa_sub_name)
async def handle_sauto_custom_fa_sub_name(message: Message, state: FSMContext):
    raw_pattern = re.sub(r'[<>:"/\\|?]', "", (message.text or "").strip()).strip()
    if not raw_pattern:
        return await message.answer("⚠️ نام وارد شده معتبر نیست. دوباره بفرستید:")

    data = await state.get_data()
    placed_map: dict[str, str] = data.get("sauto_fa_sub_map", {})
    suggested_map: dict[str, str] = data.get("sauto_suggested_fa_map", {})
    final_fa_map: dict[str, str] = {}
    sorted_keys = sorted(placed_map.keys())
    total = len(sorted_keys)

    for idx, key in enumerate(sorted_keys, start=1):
        curr_fa = Path(placed_map[key])
        ext = curr_fa.suffix

        if raw_pattern == "1":
            target_name = suggested_map.get(key, curr_fa.name)
        elif raw_pattern == "2":
            target_name = curr_fa.name
        else:
            pat = raw_pattern
            if pat.lower().endswith(ext.lower()):
                pat = pat[:-len(ext)]
            ep_num_str = key.split("E")[1] if ("E" in key and key != "MOVIE") else f"{idx:02d}"
            if "*" in pat:
                stem = pat.replace("*", ep_num_str)
            elif total > 1:
                stem = f"{pat}{ep_num_str}"
            else:
                stem = pat
            if not stem.lower().endswith(".fa"):
                stem += ".FA"
            target_name = f"{stem}{ext}"

        new_path = curr_fa.with_name(target_name)
        if curr_fa.resolve() != new_path.resolve():
            if new_path.exists():
                new_path.unlink(missing_ok=True)
            curr_fa.rename(new_path)
            curr_fa = new_path
        SubtitleService.normalize_subtitle_file(curr_fa)
        final_fa_map[key] = str(curr_fa.resolve())

    await state.update_data(sauto_final_fa_map=final_fa_map)
    await _check_english_sub_batch_step(message, state)


async def _check_english_sub_batch_step(call_or_msg: CallbackQuery | Message, state: FSMContext):
    data = await state.get_data()
    target_dir = Path(data["sauto_target_dir"])
    ep_tags = data.get("sauto_ep_tags", [])
    final_fa_map: dict[str, str] = data.get("sauto_final_fa_map", {})
    plans = await AutomationService.analyze_multi_episodes_plan(target_dir, ep_tags)

    # اگر انیمه است:
    # ۱. در صورتی که زیرنویس .ass است، نسخه تبدیل‌شده .srt آن را هم در پوشه srt بساز
    # ۲. نیازی به ساخت یا استخراج زیرنویس انگلیسی نیست -> مستقیم برو به مرحله انتخاب کیفیت‌ها!
    if plans and plans[0].category == "Anime":
        _ensure_anime_dual_ass_and_srt_in_folder(plans, final_fa_map)
        await state.update_data(sauto_final_en_map={})
        return await _prompt_final_automation_choices_step(
            call_or_msg,
            state,
            plans,
            en_note="🎌 **حالت انیمه:** زیرنویس در پوشه `srt` (همراه با تبدیل خودکار `.ass` به `.srt`) آماده شد (بدون نیاز به زیرنویس انگلیسی)."
        )

    en_map: dict[str, str] = {}
    missing_extractable: list[str] = []
    missing_no_source: list[str] = []

    for p in plans:
        key = p.season_ep_tag if (p.is_series and p.season_ep_tag) else "MOVIE"
        if p.existing_en_sub and p.existing_en_sub.exists():
            en_map[key] = str(p.existing_en_sub.resolve())
        elif p.embedded_en_source_video and p.embedded_en_track_id is not None:
            missing_extractable.append(key)
        else:
            missing_no_source.append(key)

    await state.update_data(sauto_final_en_map=en_map)

    if not missing_extractable and not missing_no_source:
        return await _prompt_final_automation_choices_step(
            call_or_msg, state, plans,
            en_note=f"✅ زیرنویس انگلیسی برای هر `{len(en_map)}` مورد در پوشه موجود است."
        )

    await state.set_state(SubtitleStates.sauto_waiting_en_decision)
    has_embedded = len(missing_extractable) > 0

    lines = ["🇬🇧 **بررسی وضعیت زیرنویس انگلیسی:**\n"]
    if en_map:
        lines.append(f"✅ موجود در پوشه (`{len(en_map)}` مورد): `{', '.join(en_map.keys())}`")
    if missing_extractable:
        lines.append(
            f"🔍 **قابل استخراج خودکار از داخل فیلم (`{len(missing_extractable)}` مورد):** `{', '.join(missing_extractable)}`"
        )
    if missing_no_source:
        lines.append(f"⚠️ بدون زیرنویس انگلیسی (`{len(missing_no_source)}` مورد): `{', '.join(missing_no_source)}`")

    if has_embedded:
        lines.append(
            "\nآیا می‌خواهید ربات زیرنویس انگلیسی قسمت‌های کسری را **به طور خودکار از فایل فیلم استخراج کرده و در پوشه `srt` قرار دهد**؟")
    else:
        lines.append("\nمی‌توانید فایل زیرنویس انگلیسی (`.srt` / `.zip` / `.rar`) آپلود کنید یا ادامه دهید:")

    kb = smart_auto_missing_en_kb(has_embedded)
    text = "\n".join(lines)
    if isinstance(call_or_msg, CallbackQuery):
        await call_or_msg.message.edit_text(text, reply_markup=kb, parse_mode="Markdown")
    else:
        await call_or_msg.answer(text, reply_markup=kb, parse_mode="Markdown")


@router.callback_query(SubtitleStates.sauto_waiting_en_decision, F.data == "sauto_en:auto_extract")
@router.callback_query(SubtitleStates.sauto_waiting_en_decision, F.data == "sauto_en:extract")
async def cb_sauto_extract_en(call: CallbackQuery, state: FSMContext):
    # نمایش اعلان بالای صفحه بدون مصرف سهمیه EditMessageText
    try:
        await call.answer("⏳ در حال استخراج خودکار زیرنویس انگلیسی در پوشه srt...")
    except Exception:
        pass

    data = await state.get_data()
    target_dir = Path(data["sauto_target_dir"])
    ep_tags = data.get("sauto_ep_tags", [])
    en_map: dict[str, str] = data.get("sauto_final_en_map", {})
    plans = await AutomationService.analyze_multi_episodes_plan(target_dir, ep_tags)

    extracted_count = 0
    for p in plans:
        key = p.season_ep_tag if (p.is_series and p.season_ep_tag) else "MOVIE"
        if key in en_map:
            continue
        if p.embedded_en_source_video and p.embedded_en_track_id is not None:
            std_en_name = AutomationService.build_standard_subtitle_filename(
                p.series_title, p.season_ep_tag, p.source_type, "EN", ".srt"
            )
            p.en_sub_dest_dir.mkdir(parents=True, exist_ok=True)
            en_dest = p.en_sub_dest_dir / std_en_name
            try:
                await MKVService.extract_subtitle_track(
                    video_path=p.embedded_en_source_video,
                    track_id=p.embedded_en_track_id,
                    output_sub_path=en_dest,
                    track_codec=p.embedded_en_codec
                )
                SubtitleService.normalize_subtitle_file(en_dest)
                en_map[key] = str(en_dest.resolve())
                extracted_count += 1
            except Exception as e:
                logging.error(f"Error extracting EN subtitle for {key}: {e}")

    await state.update_data(sauto_final_en_map=en_map)
    await _prompt_final_automation_choices_step(
        call,
        state,
        plans,
        en_note=f"✅ تعداد `{extracted_count}` زیرنویس انگلیسی با موفقیت استخراج و در پوشه `srt` ذخیره شد."
    )

@router.callback_query(SubtitleStates.sauto_waiting_en_decision, F.data == "sauto_en:upload")
async def cb_sauto_upload_en_ask(call: CallbackQuery, state: FSMContext):
    await state.set_state(SubtitleStates.sauto_waiting_en_upload)
    await call.message.edit_text(
        "📤 می‌توانید زیرنویس‌های انگلیسی همه قسمت‌ها را یک‌جا به‌صورت آلبوم فایل ارسال کنید. نام هر فایل باید تگ قسمت مثل E01 یا S02E01 را داشته باشد تا خودکار مرتب شود:",
        reply_markup=skip_or_cancel_kb("sauto_en:skip"),
        parse_mode="Markdown"
    )


async def _process_sauto_en_sub_messages(messages: list[Message], state: FSMContext):
    message = messages[0]
    data = await state.get_data()
    target_dir = Path(data["sauto_target_dir"])
    ep_tags = data.get("sauto_ep_tags", [])
    fa_map: dict[str, str] = data.get("sauto_final_fa_map", {})
    en_map: dict[str, str] = data.get("sauto_final_en_map", {})

    plans = await AutomationService.analyze_multi_episodes_plan(target_dir, ep_tags)
    if not plans:
        return await message.answer("❌ برای این مسیر، پلن اتوماسیون معتبری پیدا نشد.")

    en_dir = plans[0].en_sub_dest_dir
    try:
        en_candidates, batch_root = await _collect_subtitle_documents(messages, en_dir)
    except Exception as exc:
        return await message.answer(f"❌ دریافت زیرنویس‌های انگلیسی ناموفق بود: {html.escape(str(exc))}")

    fallback_season = AutomationService.extract_season_num_from_dir(plans[0].base_dir)
    en_candidates.sort(key=lambda item: _subtitle_episode_sort_key(item, fallback_season))
    matched_en = AutomationService.match_subtitles_to_episodes(plans, en_candidates)

    if len(plans) == 1 and len(en_candidates) == 1 and not matched_en:
        only_key = plans[0].season_ep_tag if (plans[0].is_series and plans[0].season_ep_tag) else "MOVIE"
        matched_en[only_key] = en_candidates[0]

    persisted_en = _persist_matched_subtitles(matched_en, en_dir, batch_root)
    for key, chosen_en in persisted_en.items():
        fa_sub_p = Path(fa_map[key]) if key in fa_map and Path(fa_map[key]).exists() else None
        if fa_sub_p:
            en_filename = re.sub(r"\.FA(\.[^.]+)$", r".EN\1", fa_sub_p.name, flags=re.IGNORECASE)
            if en_filename == fa_sub_p.name:
                en_filename = f"{fa_sub_p.stem}.EN{chosen_en.suffix}"
        else:
            en_filename = chosen_en.name

        final_en = en_dir / en_filename
        if chosen_en.exists() and chosen_en.resolve() != final_en.resolve():
            if final_en.exists():
                final_en.unlink(missing_ok=True)
            chosen_en.rename(final_en)
            chosen_en = final_en

        if chosen_en.suffix.lower() == ".srt":
            SubtitleService.normalize_srt_utf8(chosen_en)
        en_map[key] = str(chosen_en.resolve())

    await state.update_data(sauto_final_en_map=en_map)
    unmatched_count = len(en_candidates) - len(persisted_en)
    unmatched_note = (
        f" موارد بدون تطبیق با Eyy: {unmatched_count}."
        if unmatched_count > 0
        else ""
    )
    await _prompt_final_automation_choices_step(
        message,
        state,
        plans,
        en_note=(
            f"✅ تعداد {len(persisted_en)} زیرنویس انگلیسی از مجموع {len(en_candidates)} فایل "
            f"بر اساس Eyy مرتب و به قسمت متناظر وصل شد.{unmatched_note}"
        ),
    )


@router.message(SubtitleStates.sauto_waiting_en_upload, F.document)
async def handle_sauto_en_upload(message: Message, state: FSMContext):
    if await _queue_sauto_subtitle_album(message, state, "EN"):
        return
    await _process_sauto_en_sub_messages([message], state)


@router.callback_query(F.data == "sauto_en:skip")
async def cb_sauto_skip_en(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    target_dir = Path(data["sauto_target_dir"])
    ep_tags = data.get("sauto_ep_tags", [])
    plans = await AutomationService.analyze_multi_episodes_plan(target_dir, ep_tags)
    await _prompt_final_automation_choices_step(call, state, plans, en_note="⏭ ادامه بدون زیرنویس انگلیسی جدید.")


async def _detect_existing_automation_quality_defaults(
        plans: list[AutomationPlan],
) -> dict[str, Any]:
    """
    کیفیت‌هایی را پیش‌فرض انتخاب می‌کند که خروجی متناظرشان در مقصد FTP وجود دارد.

    Series/Anime:
      - SoftSub: پوشه‌هایی مثل 1080p-EBTV-SoftSub
      - HardSub: پوشه‌هایی مثل 1080p-HardSub

    Movies:
      - چون همه خروجی‌های SoftSub و HardSub داخل یک پوشه مشترک قرار می‌گیرند،
        نام فایل‌های موجود در پوشه بررسی می‌شود تا کیفیت دقیق تشخیص داده شود.
    """
    selected_soft: set[str] = set()
    selected_hard: set[str] = set()
    checked_roots: set[str] = set()
    unavailable_roots: list[str] = []
    remote_cache: dict[str, dict] = {}

    async def inspect(path: str) -> dict:
        key = path.casefold()
        if key not in remote_cache:
            try:
                remote_cache[key] = await CuteFTPService.inspect_remote_dir(path)
            except Exception as exc:
                logging.warning("Could not inspect automation output folder %s: %s", path, exc)
                remote_cache[key] = {"exists": False, "dirs": [], "files": []}
        return remote_cache[key]

    hard_qualities = ("1080p", "720p", "480p")

    for plan in plans:
        remote_root = AutomationService.compute_server_remote_dir_for_local_path(plan.base_dir)
        root_info = await inspect(remote_root)

        if not root_info.get("exists"):
            if remote_root not in unavailable_roots:
                unavailable_roots.append(remote_root)
            continue

        checked_roots.add(remote_root)
        remote_dirs = {
            str(name).strip().casefold()
            for name in root_info.get("dirs", [])
            if str(name).strip()
        }

        if plan.is_series:
            # در سریال و انیمه هر کیفیت خروجی پوشه مستقل دارد.
            for target in plan.softsub_targets:
                remote_output_dir = AutomationService.compute_server_remote_dir_for_local_path(
                    target.output_dir
                ).rsplit("/", 1)[-1].casefold()
                if remote_output_dir in remote_dirs:
                    selected_soft.add(target.quality_folder_name)

            for quality in hard_qualities:
                expected_dir = f"{quality}-HardSub".casefold()
                if plan.fullhd_source_video and expected_dir in remote_dirs:
                    selected_hard.add(quality)
            continue

        # فیلم سینمایی: همه خروجی‌ها داخل SoftSub/Hardsub هستند؛ فایل‌ها کیفیت را مشخص می‌کنند.
        if "softsub" in remote_dirs:
            soft_remote_path = f"{remote_root.rstrip('/')}/SoftSub"
            soft_info = await inspect(soft_remote_path)
            existing_soft_files = {
                str(name).casefold()
                for name in soft_info.get("files", [])
            }
            if soft_info.get("exists"):
                for target in plan.softsub_targets:
                    if target.output_file.name.casefold() in existing_soft_files:
                        selected_soft.add(target.quality_folder_name)

        if plan.fullhd_source_video and "hardsub" in remote_dirs:
            hard_remote_path = f"{remote_root.rstrip('/')}/Hardsub"
            hard_info = await inspect(hard_remote_path)
            existing_hard_files = list(hard_info.get("files", []))
            for quality in hard_qualities:
                quality_pattern = re.compile(
                    rf"(?<![a-z0-9]){re.escape(quality)}(?![a-z0-9])",
                    flags=re.IGNORECASE,
                )
                if any(quality_pattern.search(str(name)) for name in existing_hard_files):
                    selected_hard.add(quality)

    return {
        "soft": selected_soft,
        "hard": selected_hard,
        "checked_roots": sorted(checked_roots),
        "unavailable_roots": unavailable_roots,
    }


async def _prompt_final_automation_choices_step(
        call_or_msg: CallbackQuery | Message,
        state: FSMContext,
        plans: list[AutomationPlan],
        en_note: str = ""
):
    avail_soft_quals = []
    for p in plans:
        for st in p.softsub_targets:
            if st.quality_folder_name not in avail_soft_quals:
                avail_soft_quals.append(st.quality_folder_name)

    server_defaults = await _detect_existing_automation_quality_defaults(plans)
    default_selected_soft = sorted(
        set(avail_soft_quals) & set(server_defaults["soft"]),
        key=str.casefold,
    )
    default_srt_hb = sorted(
        set(server_defaults["hard"]),
        key=lambda q: {"1080p": 1, "720p": 2, "480p": 3}.get(q, 9),
    )
    default_auto_upload = True

    checked_roots = server_defaults["checked_roots"]
    unavailable_roots = server_defaults["unavailable_roots"]
    if checked_roots:
        remote_check_note = (
            "☁️ تیک‌های اولیه بر اساس پوشه‌ها/فایل‌های موجود در سرور مقصد تنظیم شده‌اند.\n"
        )
    else:
        remote_check_note = (
            "⚠️ پوشه مقصد در سرور قابل بررسی نبود؛ هیچ کیفیتی به‌صورت پیش‌فرض تیک نخورده است. "
            "می‌توانید کیفیت‌ها را دستی انتخاب کنید.\n"
        )
    if unavailable_roots and checked_roots:
        remote_check_note += (
            f"⚠️ دسترسی به {len(unavailable_roots)} مسیر مقصد برقرار نشد؛ "
            "برای آن مسیرها انتخاب پیش‌فرضی انجام نشد.\n"
        )

    await state.set_state(SubtitleStates.sauto_waiting_hb_qualities)
    await state.update_data(
        sauto_avail_soft_quals=avail_soft_quals,
        sauto_selected_soft_quals=default_selected_soft,
        sauto_hb_srt_qualities=default_srt_hb,
        sauto_auto_upload=default_auto_upload
    )

    fhd_list = [f"• `{p.season_ep_tag or 'فیلم'}`: `{p.fullhd_source_video.name}`" for p in plans if
                p.fullhd_source_video]
    fhd_block = "\n".join(fhd_list[:8]) if fhd_list else "_(فایل FullHD یافت نشد)_"

    text = (
        f"{en_note}\n\n"
        f"🎯 **مرحله نهایی اتوماسیون: انتخاب کیفیت‌های سافت‌ساب و هاردساب**\n\n"
        f"🎞 تعداد قسمت‌های آماده: **`{len(plans)}` مورد**\n"
        f"📂 پوشه‌های کیفیت خام موجود: `{', '.join(avail_soft_quals) or 'موردی یافت نشد'}`\n"
        f"🔥 **سورس هاردساب (`FullHD`):**\n{fhd_block}\n\n"
        f"{remote_check_note}\n"
        f"✅ پیش‌فرض SoftSub: `{', '.join(default_selected_soft) or 'هیچ‌کدام'}`\n"
        f"✅ پیش‌فرض HardSub: `{', '.join(default_srt_hb) or 'هیچ‌کدام'}`\n\n"
        "👇 در پنل زیر می‌توانید:\n"
        "1️⃣ مشخص کنید کدام **پوشه‌های کیفیت خام** سافت‌ساب شوند (`✅`).\n"
        "2️⃣ مشخص کنید کدام یک از **۳ کیفیت اصلی (`1080p`, `720p`, `480p`)** از روی فایل `FullHD` هاردساب شوند:"
    )
    kb = smart_auto_final_selection_kb(
        available_softsub_qualities=avail_soft_quals,
        selected_softsub_qualities=set(default_selected_soft),
        selected_hardsub_srt=set(default_srt_hb),
        auto_upload=default_auto_upload
    )

    if isinstance(call_or_msg, CallbackQuery):
        await call_or_msg.message.edit_text(text[:4000], reply_markup=kb, parse_mode="Markdown")
    else:
        await call_or_msg.answer(text[:4000], reply_markup=kb, parse_mode="Markdown")


@router.callback_query(SubtitleStates.sauto_waiting_hb_qualities, F.data.startswith("sauto_hbq:"))
async def cb_sauto_hb_quality_action(call: CallbackQuery, state: FSMContext):
    action = call.data.split(":", 1)[1]
    data = await state.get_data()
    avail_soft: list[str] = data.get("sauto_avail_soft_quals", [])
    sel_soft = set(data.get("sauto_selected_soft_quals", avail_soft))
    srt_set = set(data.get("sauto_hb_srt_qualities", []))
    auto_up = bool(data.get("sauto_auto_upload", True))

    if action.startswith("toggle_soft:"):
        idx = int(action.split(":")[1])
        if idx < len(avail_soft):
            q_name = avail_soft[idx]
            sel_soft.remove(q_name) if q_name in sel_soft else sel_soft.add(q_name)
    elif action == "all_soft":
        if len(sel_soft) == len(avail_soft):
            sel_soft.clear()
        else:
            sel_soft = set(avail_soft)
    elif action.startswith("toggle_srt:"):
        q = action.split(":")[1]
        srt_set.remove(q) if q in srt_set else srt_set.add(q)
    elif action == "all_srt":
        srt_set = {"1080p", "720p", "480p"}
    elif action == "no_srt":
        srt_set.clear()
    elif action == "toggle_upload":
        auto_up = not auto_up
    elif action == "launch":
        return await _execute_smart_automation_batch(
            call, state, list(sel_soft), list(srt_set), auto_up
        )

    await state.update_data(
        sauto_selected_soft_quals=list(sel_soft),
        sauto_hb_srt_qualities=list(srt_set),
        sauto_auto_upload=auto_up
    )
    await call.message.edit_reply_markup(
        reply_markup=smart_auto_final_selection_kb(
            available_softsub_qualities=avail_soft,
            selected_softsub_qualities=sel_soft,
            selected_hardsub_srt=srt_set,
            auto_upload=auto_up
        )
    )


def _automation_batch_status_markup():
    kb = InlineKeyboardBuilder()
    kb.row(
        InlineKeyboardButton(text="📊 مشاهده صف کامل", callback_data="menu:queues"),
        InlineKeyboardButton(text="📂 فایل‌منیجر", callback_data="menu:file_mgr"),
    )
    kb.row(InlineKeyboardButton(text="🔙 منوی اصلی", callback_data="menu:home"))
    return kb.as_markup()


def _build_automation_batch_status_text(
        title: str,
        tasks: list,
        skipped_items: list[str],
        auto_upload: bool,
        final: bool = False,
) -> str:
    """ساخت یک گزارش HTML جمع‌وجور از وضعیت کل Batch."""
    done = [t for t in tasks if t.is_finished and "خطا" not in str(t.status) and "لغو" not in str(t.status)]
    failed = [t for t in tasks if "خطا" in str(t.status)]
    cancelled = [t for t in tasks if "لغو" in str(t.status)]
    running = [
        t for t in tasks
        if not t.is_finished and ("اجرا" in str(t.status) or t.running_asyncio_task is not None)
    ]
    pending = [t for t in tasks if not t.is_finished and t not in running]

    if final:
        stage = "✅ اجرای مراحل ثبت‌شده به پایان رسید"
    elif any(t.queue_type == "upload" for t in running):
        stage = "☁️ مرحله جاری: آپلود به سرور"
    elif any(t.queue_type == "handbrake" for t in running):
        stage = "🔥 مرحله جاری: پردازش HardSub"
    elif any(t.queue_type == "mkvtoolnix" for t in running):
        stage = "📎 مرحله جاری: پردازش SoftSub"
    elif pending:
        stage = "⏳ مرحله جاری: انتظار در صف پردازش"
    else:
        stage = "🧾 در حال آماده‌سازی گزارش نهایی"

    lines = [
        f"🎬 <b>گزارش زنده اتوماسیون: {html.escape(title)}</b>",
        "━━━━━━━━━━━━━━━━━━━━",
        f"📍 <b>{stage}</b>",
        f"☁️ آپلود خودکار: {'فعال' if auto_upload else 'غیرفعال'}",
        "",
        (
            f"✅ تکمیل‌شده: <b>{len(done)}</b>  |  "
            f"⚙️ در حال اجرا: <b>{len(running)}</b>  |  "
            f"⏳ منتظر: <b>{len(pending)}</b>"
        ),
        f"❌ خطا: <b>{len(failed)}</b>  |  🛑 لغوشده: <b>{len(cancelled)}</b>",
        "",
        "<b>وضعیت مراحل:</b>",
    ]

    stage_labels = (
        ("mkvtoolnix", "📎 SoftSub"),
        ("handbrake", "🔥 HardSub"),
        ("upload", "☁️ Upload"),
    )
    for queue_type, label in stage_labels:
        subset = [t for t in tasks if t.queue_type == queue_type]
        if not subset:
            continue
        subset_done = sum(
            1 for t in subset
            if t.is_finished and "خطا" not in str(t.status) and "لغو" not in str(t.status)
        )
        subset_failed = sum(
            1 for t in subset
            if "خطا" in str(t.status) or "لغو" in str(t.status)
        )
        lines.append(
            f"• {label}: <b>{subset_done}/{len(subset)}</b> تکمیل"
            + (f" | <b>{subset_failed}</b> ناموفق" if subset_failed else "")
        )

    if tasks:
        lines.extend(["", "<b>فایل‌ها و عملیات:</b>"])
        for task in tasks[:14]:
            status = str(task.status)
            if "خطا" in status:
                icon, detail = "❌", f"خطا: {str(task.error or task.progress)[:65]}"
            elif "لغو" in status:
                icon, detail = "🛑", "لغو شد"
            elif task.is_finished:
                icon, detail = "✅", "تکمیل شد"
            elif task in running:
                icon, detail = "⚙️", str(task.progress or "در حال اجرا")[:65]
            else:
                icon, detail = "⏳", "در انتظار"
            lines.append(
                f"{icon} <code>#{html.escape(str(task.task_id))} "
                f"{html.escape(str(task.title)[:90])}</code> — "
                f"<i>{html.escape(detail)}</i>"
            )
        if len(tasks) > 14:
            lines.append(f"<i>... و {len(tasks) - 14} عملیات دیگر</i>")

    if skipped_items:
        lines.extend(["", "<b>انجام‌نشده / ردشده:</b>"])
        for item in skipped_items[:6]:
            lines.append(f"▫️ {html.escape(item[:150])}")
        if len(skipped_items) > 6:
            lines.append(f"<i>... و {len(skipped_items) - 6} مورد دیگر</i>")

    if final:
        if not tasks:
            lines.extend(["", "ℹ️ <b>هیچ عملیات اجرایی در صف ثبت نشد.</b>"])
        elif failed or cancelled:
            lines.extend(["", "⚠️ <b>اتوماسیون با موارد ناموفق/لغوشده پایان یافت.</b>"])
        else:
            lines.extend(["", "🎉 <b>تمام عملیات ثبت‌شده با موفقیت تکمیل شدند.</b>"])

    return "\n".join(lines)[:3900]


async def _watch_smart_automation_batch(
    status_message: Message,
    batch_id: str,
    title: str,
    initial_tasks: list,
    skipped_items: list[str],
    auto_upload: bool,
):
    """
    آپدیت وضعیت اتوماسیون با فاصله ایمن ۱۲ ثانیه:
    هر ۱۰ بار ادیت، یک بار پیام جدید ارسال می‌کند تا سقف EditMessageText تلگرام هرگز پر نشود.
    """
    known_tasks = {task.task_id: task for task in initial_tasks}
    markup = _automation_batch_status_markup()
    last_rendered = None
    stable_completed_cycles = 0
    edit_counter = 0
    current_msg = status_message

    while True:
        for task in list(queue_engine.tasks.values()):
            if getattr(task, "batch_id", None) == batch_id:
                known_tasks[task.task_id] = task

        tasks = list(known_tasks.values())
        is_complete = bool(tasks) and all(task.is_finished for task in tasks)
        stable_completed_cycles = stable_completed_cycles + 1 if is_complete else 0
        is_final = stable_completed_cycles >= 2

        report = _build_automation_batch_status_text(
            title=title,
            tasks=tasks,
            skipped_items=skipped_items,
            auto_upload=auto_upload,
            final=is_final,
        )

        if report != last_rendered:
            try:
                # اگر کار تمام شده یا هر ۱۰ دور یک بار، یک پیام جدید بفرست تا ادیت روی یک پیام واحد اسپم نشود
                if is_final or edit_counter >= 10:
                    sent = await current_msg.answer(
                        report,
                        reply_markup=markup,
                        parse_mode="HTML",
                        disable_web_page_preview=True,
                    )
                    if isinstance(sent, Message) and sent.message_id > 1:
                        current_msg = sent
                    edit_counter = 0
                else:
                    await current_msg.edit_text(
                        report,
                        reply_markup=markup,
                        parse_mode="HTML",
                        disable_web_page_preview=True,
                    )
                    edit_counter += 1
                last_rendered = report
            except TelegramRetryAfter as exc:
                wait_sec = float(getattr(exc, "retry_after", 20))
                await asyncio.sleep(wait_sec + 2.0)
            except Exception as exc:
                if "message is not modified" not in str(exc).lower():
                    logging.debug("Status update skipped: %s", exc)

        if is_final:
            return

        # فاصله ۱۲ ثانیه‌ای بین هر بررسی (کاملاً ایمن در برابر محدودیت تلگرام)
        await asyncio.sleep(12)

async def _execute_smart_automation_batch(
        call: CallbackQuery,
        state: FSMContext,
        selected_soft_qualities: list[str],
        selected_hb_srt: list[str],
        auto_upload: bool
):
    data = await state.get_data()
    target_dir = Path(data["sauto_target_dir"])
    ep_tags = data.get("sauto_ep_tags", [])
    fa_map: dict[str, str] = data.get("sauto_final_fa_map", {})
    en_map: dict[str, str] = data.get("sauto_final_en_map", {})
    brand = cfg.get("branding", "global_title_and_track_name", ".::EmpireBestTv.Com::.")

    plans = await AutomationService.analyze_multi_episodes_plan(target_dir, ep_tags)
    selected_soft_set = set(selected_soft_qualities)
    batch_id = uuid.uuid4().hex[:10]
    batch_tasks = []
    skipped_items: list[str] = []

    for plan in plans:
        key = plan.season_ep_tag if (plan.is_series and plan.season_ep_tag) else "MOVIE"
        fa_sub = Path(fa_map[key]) if key in fa_map else None
        en_sub = Path(en_map[key]) if key in en_map else plan.existing_en_sub

        if not fa_sub or not fa_sub.exists():
            skipped_items.append(
                f"{key}: زیرنویس فارسی معتبر پیدا نشد؛ SoftSub/HardSub این مورد اجرا نشد."
            )
            continue

        if auto_upload:
            if fa_sub.exists():
                r_dir_fa = AutomationService.compute_server_remote_dir_for_local_path(fa_sub)
                subtitle_task = await queue_engine.add_task(
                    "upload",
                    f"AutoUpload Sub [{r_dir_fa}]: {fa_sub.name}",
                    CuteFTPService.upload_path,
                    fa_sub,
                    r_dir_fa,
                    batch_id=batch_id,
                    notify_completion=False,
                )
                batch_tasks.append(subtitle_task)

                # در انیمه، نسخه SRT تبدیل‌شده نیز باید همراه ASS ارسال شود.
                if plan.category == "Anime" and fa_sub.suffix.lower() in (".ass", ".ssa"):
                    converted_srt = fa_sub.with_suffix(".srt")
                    if converted_srt.exists():
                        converted_task = await queue_engine.add_task(
                            "upload",
                            f"AutoUpload Sub [{r_dir_fa}]: {converted_srt.name}",
                            CuteFTPService.upload_path,
                            converted_srt,
                            r_dir_fa,
                            batch_id=batch_id,
                            notify_completion=False,
                        )
                        batch_tasks.append(converted_task)

            if en_sub and en_sub.exists() and plan.category != "Anime":
                r_dir_en = AutomationService.compute_server_remote_dir_for_local_path(en_sub)
                en_task = await queue_engine.add_task(
                    "upload",
                    f"AutoUpload Sub [{r_dir_en}]: {en_sub.name}",
                    CuteFTPService.upload_path,
                    en_sub,
                    r_dir_en,
                    batch_id=batch_id,
                    notify_completion=False,
                )
                batch_tasks.append(en_task)

        for st in plan.softsub_targets:
            if st.quality_folder_name not in selected_soft_set:
                continue
            st.output_dir.mkdir(parents=True, exist_ok=True)
            soft_task = await queue_engine.add_task(
                "mkvtoolnix",
                f"SoftSub [{st.quality_folder_name}]: {st.output_file.name}",
                AutomationService.run_softsub_and_optional_upload,
                st.raw_video,
                st.output_file,
                fa_sub,
                en_sub,
                brand,
                auto_upload,
                batch_id=batch_id,
                notify_completion=False,
            )
            batch_tasks.append(soft_task)

        hardsub_targets = AutomationService.build_hardsub_targets(plan, selected_hb_srt)
        if selected_hb_srt and not plan.fullhd_source_video:
            skipped_items.append(
                f"{key}: سورس FullHD موجود نیست؛ HardSub انتخاب‌شده اجرا نشد."
            )

        for ht in hardsub_targets:
            ht.output_dir.mkdir(parents=True, exist_ok=True)
            hard_task = await queue_engine.add_task(
                "handbrake",
                f"HardSub [{ht.quality_tag}]: {ht.output_file.name}",
                AutomationService.run_hardsub_or_encode_and_optional_upload,
                ht,
                fa_sub,
                en_sub,
                brand,
                auto_upload,
                batch_id=batch_id,
                notify_completion=False,
            )
            batch_tasks.append(hard_task)

        if (
                not any(st.quality_folder_name in selected_soft_set for st in plan.softsub_targets)
                and not selected_hb_srt
        ):
            skipped_items.append(
                f"{key}: هیچ کیفیت SoftSub یا HardSub برای اجرا انتخاب/موجود نبود."
            )

    await state.clear()
    await state.update_data(current_dir=str(target_dir))

    title_str = plans[0].series_title if plans else target_dir.name
    initial_report = _build_automation_batch_status_text(
        title=title_str,
        tasks=batch_tasks,
        skipped_items=skipped_items,
        auto_upload=auto_upload,
        final=not bool(batch_tasks),
    )

    status_message = await call.message.edit_text(
        initial_report,
        reply_markup=_automation_batch_status_markup(),
        parse_mode="HTML",
        disable_web_page_preview=True,
    )

    if batch_tasks:
        monitor_task = asyncio.create_task(
            _watch_smart_automation_batch(
                status_message=status_message,
                batch_id=batch_id,
                title=title_str,
                initial_tasks=batch_tasks,
                skipped_items=skipped_items,
                auto_upload=auto_upload,
            ),
            name=f"automation-status-{batch_id}",
        )
        _ACTIVE_AUTOMATION_MONITORS.add(monitor_task)
        monitor_task.add_done_callback(_release_automation_monitor)

    await call.answer("اتوماسیون در صف قرار گرفت و گزارش آن به‌روزرسانی می‌شود.")


def _incoming_subtitle_episode_tags(sub_files: list[Path], target_dir: Path) -> list[str]:
    fallback_season = AutomationService.extract_season_num_from_dir(target_dir)
    tags = {
        ep
        for sub_file in sub_files
        if (ep := AutomationService.extract_episode_tag(sub_file.name, fallback_season=fallback_season))
    }
    return sorted(tags)


def _resolve_incoming_subtitle_target(target_dir: Path, sub_files: list[Path]) -> tuple[Path, list[str]]:
    target_dir = target_dir.resolve()
    ep_tags = _incoming_subtitle_episode_tags(sub_files, target_dir)
    category = AutomationService.detect_category_from_path(target_dir)
    if ep_tags and category in ("Series", "Anime") and not AutomationService.SEASON_DIR_REGEX.match(target_dir.name):
        if target_dir.name.casefold() not in ("series", "anime", "workspace"):
            season_num = int(ep_tags[0][1:3])
            target_dir = target_dir / f"S{season_num:02d}"
    return target_dir, ep_tags


async def _show_incoming_subtitle_actions(
        call_or_msg: CallbackQuery | Message,
        target_dir: Path,
        sub_files: list[Path],
        selected_eps: list[str] | None = None,
):
    target_dir, inferred_eps = _resolve_incoming_subtitle_target(target_dir, sub_files)
    ep_tags = sorted(set(selected_eps or inferred_eps))
    token = uuid.uuid4().hex[:8]
    PENDING_INCOMING_SUBTITLES[token] = {
        "target_dir": str(target_dir.resolve()),
        "sub_files": [str(p.resolve()) for p in sub_files if p.exists()],
        "episode_tags": ep_tags,
    }

    category = AutomationService.detect_category_from_path(target_dir)
    kb = InlineKeyboardBuilder()
    kb.row(
        InlineKeyboardButton(
            text="🚀 اجرای اتوماسیون کامل با این زیرنویس",
            callback_data=f"subfind:auto:{token}",
        )
    )
    kb.row(
        InlineKeyboardButton(
            text="☁️ فقط آپلود زیرنویس در پوشه مقصد",
            callback_data=f"subfind:upload:{token}",
        )
    )
    kb.row(
        InlineKeyboardButton(
            text="📂 انتخاب پوشه دیگری",
            callback_data=f"subfind:manual:{token}",
        )
    )
    kb.row(InlineKeyboardButton(text="❌ لغو", callback_data=f"subfind:cancel:{token}"))

    text = (
        "✅ <b>مقصد زیرنویس انتخاب شد</b>\n"
        f"🎬 عنوان/پوشه: <b>{html.escape(target_dir.parent.name if AutomationService.SEASON_DIR_REGEX.match(target_dir.name) else target_dir.name)}</b>\n"
        f"📁 دسته‌بندی: <b>{html.escape(category)}</b>\n"
        f"📂 فصل/مسیر: <code>{html.escape(str(target_dir))}</code>\n"
        f"🎞 قسمت‌های شناسایی‌شده: <code>{html.escape(', '.join(ep_tags) or 'از روی فایل‌ها قابل تشخیص نبود')}</code>\n"
        f"📄 تعداد زیرنویس‌ها: <b>{len(sub_files)}</b>\n\n"
        "عملیات موردنظر را انتخاب کنید. در حالت آپلود، ربات پوشهٔ استاندارد زیرنویس همان فیلم/فصل را روی Workspace و سرور مقصد استفاده می‌کند."
    )

    if isinstance(call_or_msg, CallbackQuery):
        await call_or_msg.message.edit_text(
            text,
            reply_markup=kb.as_markup(),
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
    else:
        await call_or_msg.answer(
            text,
            reply_markup=kb.as_markup(),
            parse_mode="HTML",
            disable_web_page_preview=True,
        )


async def _start_incoming_subtitle_upload_batch(
        call: CallbackQuery,
        target_dir: Path,
        sub_files: list[Path],
):
    plan = await AutomationService.analyze_and_build_plan(
        target_dir,
        AutomationService.extract_episode_tag(sub_files[0].name) if sub_files else None,
    )
    tasks = []
    skipped: list[str] = []
    batch_id = uuid.uuid4().hex[:10]

    for source in sub_files:
        if not source.exists() or source.suffix.lower() not in SubtitleService.SUB_EXTENSIONS:
            skipped.append(f"{source.name}: فایل زیرنویس معتبر موجود نیست.")
            continue

        low = source.name.casefold()
        is_english = (
                low.startswith("en_")
                or ".en." in low
                or low.endswith(("_en.srt", "_en.ass", "_en.ssa"))
        )
        if is_english and plan.category != "Anime":
            dest_dir = plan.en_sub_dest_dir
        else:
            dest_dir = plan.fa_sub_dest_dir

        dest_dir.mkdir(parents=True, exist_ok=True)
        destination = dest_dir / source.name
        if source.resolve() != destination.resolve():
            if destination.exists():
                skipped.append(f"{source.name}: فایلی با همین نام از قبل در مقصد وجود دارد؛ بازنویسی نشد.")
                continue
            shutil.copy2(source, destination)

        if destination.suffix.lower() == ".srt":
            SubtitleService.normalize_srt_utf8(destination)

        remote_dir = AutomationService.compute_server_remote_dir_for_local_path(destination)
        task = await queue_engine.add_task(
            "upload",
            f"Subtitle Upload [{remote_dir}]: {destination.name}",
            CuteFTPService.upload_path,
            destination,
            remote_dir,
            batch_id=batch_id,
            notify_completion=False,
        )
        tasks.append(task)

    title = plan.series_title or target_dir.name
    if not tasks:
        return await call.message.edit_text(
            "⚠️ هیچ فایل زیرنویسی برای آپلود در مقصد ثبت نشد.\n"
            + "\n".join(html.escape(item) for item in skipped[:8]),
            parse_mode="HTML",
        )

    initial = _build_automation_batch_status_text(
        title=f"آپلود زیرنویس: {title}",
        tasks=tasks,
        skipped_items=skipped,
        auto_upload=True,
    )
    status_message = await call.message.edit_text(
        initial,
        reply_markup=_automation_batch_status_markup(),
        parse_mode="HTML",
        disable_web_page_preview=True,
    )
    monitor = asyncio.create_task(
        _watch_smart_automation_batch(
            status_message=status_message,
            batch_id=batch_id,
            title=f"آپلود زیرنویس: {title}",
            initial_tasks=tasks,
            skipped_items=skipped,
            auto_upload=True,
        ),
        name=f"subtitle-upload-status-{batch_id}",
    )
    _ACTIVE_AUTOMATION_MONITORS.add(monitor)
    monitor.add_done_callback(_release_automation_monitor)


@router.callback_query(F.data.startswith("subfind:auto:"))
async def cb_incoming_subtitle_auto(call: CallbackQuery, state: FSMContext):
    token = call.data.split(":")[2]
    info = PENDING_INCOMING_SUBTITLES.pop(token, None)
    if not info:
        return await call.answer("این انتخاب منقضی شده است.", show_alert=True)
    await state.update_data(sauto_incoming_subs=[])

    target_dir = Path(info["target_dir"])
    subs = [Path(p) for p in info["sub_files"]]
    eps = list(info.get("episode_tags", []))
    await _init_smart_auto_batch(call, state, target_dir, eps, pre_uploaded_subs=subs)


@router.callback_query(F.data.startswith("subfind:upload:"))
async def cb_incoming_subtitle_upload(call: CallbackQuery, state: FSMContext):
    token = call.data.split(":")[2]
    info = PENDING_INCOMING_SUBTITLES.pop(token, None)
    if not info:
        return await call.answer("این انتخاب منقضی شده است.", show_alert=True)
    await state.update_data(sauto_incoming_subs=[])
    await _start_incoming_subtitle_upload_batch(
        call,
        Path(info["target_dir"]),
        [Path(p) for p in info["sub_files"]],
    )


@router.callback_query(F.data.startswith("subfind:manual:"))
async def cb_incoming_subtitle_manual(call: CallbackQuery, state: FSMContext):
    token = call.data.split(":")[2]
    info = PENDING_INCOMING_SUBTITLES.pop(token, None)
    if not info:
        return await call.answer("این انتخاب منقضی شده است.", show_alert=True)
    subs = [Path(p) for p in info["sub_files"]]
    workspace = Path(cfg.get("paths", "workspace_dir")).resolve()
    await state.update_data(sauto_incoming_subs=[str(p.resolve()) for p in subs])
    await _render_smart_auto_browser(call, state, workspace, page=0)


@router.callback_query(F.data.startswith("subfind:cancel:"))
async def cb_incoming_subtitle_cancel(call: CallbackQuery, state: FSMContext):
    token = call.data.split(":")[2]
    PENDING_INCOMING_SUBTITLES.pop(token, None)
    await state.update_data(sauto_incoming_subs=[])
    await call.message.edit_text("⏭ عملیات زیرنویس لغو شد.", reply_markup=main_menu_kb())


@router.message(StateFilter(None), F.document)
async def catch_direct_subtitle_drop_for_smart_auto(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    fname = message.document.file_name or ""
    ext_low = Path(fname).suffix.lower()
    if ext_low not in (".srt", ".ass", ".ssa", ".zip", ".rar", ".7z"):
        return

    temp_dir = Path(cfg.get("paths", "workspace_dir")) / "_incoming_subs"
    temp_dir.mkdir(parents=True, exist_ok=True)
    saved_p = temp_dir / fname
    await message.bot.download(message.document, destination=saved_p)

    sub_files: list[Path] = []
    if ext_low in SubtitleService.ARCHIVE_EXTENSIONS:
        extracted = SubtitleService.extract_archive(saved_p, temp_dir, remove_archive=True)
        sub_files = [p for p in extracted if p.suffix.lower() in SubtitleService.SUB_EXTENSIONS]
        if not sub_files:
            return await message.answer("❌ فایل فشرده اکسترکت شد اما زیرنویسی داخل آن نبود.")
    else:
        if ext_low == ".srt":
            SubtitleService.normalize_srt_utf8(saved_p)
        sub_files = [saved_p]

    search_references = [fname] + [p.name for p in sub_files[:5]]
    candidates = []
    search_errors = []
    for reference in search_references:
        try:
            candidates = await AutomationService.search_media_destination_candidates(reference, limit=7)
        except Exception as exc:
            logging.warning("Subtitle destination search failed for %s: %s", reference, exc)
            search_errors.append(str(exc))
        if candidates:
            break

    if candidates:
        token = uuid.uuid4().hex[:8]
        PENDING_INCOMING_SUBTITLES[token] = {
            "sub_files": [str(p.resolve()) for p in sub_files],
            "candidates": candidates,
        }
        kb = InlineKeyboardBuilder()
        lines = []
        for idx, candidate in enumerate(candidates):
            kb.row(
                InlineKeyboardButton(
                    text=f"📁 {candidate['label'][:42]}",
                    callback_data=f"subfind:pick:{token}:{idx}",
                )
            )
            lines.append(
                f"<b>{idx + 1}.</b> {html.escape(candidate['label'])} "
                f"— <code>{html.escape(candidate['category'])}</code> "
                f"({'محلی' if candidate.get('exists_local') else 'فقط در سرور'})"
            )
        kb.row(
            InlineKeyboardButton(
                text="📂 پیشنهاد مناسب نبود؛ انتخاب دستی مقصد",
                callback_data=f"subfind:manual:{token}",
            )
        )
        kb.row(InlineKeyboardButton(text="❌ لغو", callback_data=f"subfind:cancel:{token}"))
        query = AutomationService.extract_media_title_query(fname)
        if not query and sub_files:
            query = AutomationService.extract_media_title_query(sub_files[0].name)
        return await message.answer(
            f"🔎 عنوان تشخیص‌داده‌شده از زیرنویس: <b>{html.escape(query or 'نامشخص')}</b>\n"
            f"📄 تعداد فایل‌های زیرنویس: <b>{len(sub_files)}</b>\n\n"
            "چند مقصد احتمالی از روی نام زیرنویس و پوشه‌های Workspace/سرور پیدا شد. "
            "عنوان درست را انتخاب کنید:\n\n"
            + "\n".join(lines),
            reply_markup=kb.as_markup(),
            parse_mode="HTML",
            disable_web_page_preview=True,
        )

    token = uuid.uuid4().hex[:8]
    PENDING_INCOMING_SUBTITLES[token] = {
        "sub_files": [str(p.resolve()) for p in sub_files],
        "candidates": [],
    }
    kb = InlineKeyboardBuilder()
    kb.row(
        InlineKeyboardButton(
            text="📂 انتخاب دستی مقصد از Workspace",
            callback_data=f"subfind:manual:{token}",
        )
    )
    kb.row(InlineKeyboardButton(text="❌ لغو", callback_data=f"subfind:cancel:{token}"))
    error_note = (
        f"\nجزئیات بررسی: <code>{html.escape(search_errors[-1][:200])}</code>"
        if search_errors else ""
    )
    await message.answer(
        f"📥 تعداد <b>{len(sub_files)}</b> فایل زیرنویس دریافت شد، اما مقصد مطمئنی از روی نام پیدا نشد."
        f"{error_note}\n"
        "می‌توانید پوشه فیلم یا فصل سریال را دستی انتخاب کنید.",
        reply_markup=kb.as_markup(),
        parse_mode="HTML",
    )


@router.callback_query(F.data.startswith("subfind:pick:"))
async def cb_incoming_subtitle_pick(call: CallbackQuery):
    parts = call.data.split(":")
    if len(parts) != 4:
        return await call.answer("درخواست نامعتبر است.", show_alert=True)
    token, idx_text = parts[2], parts[3]
    info = PENDING_INCOMING_SUBTITLES.pop(token, None)
    if not info:
        return await call.answer("این پیشنهاد منقضی شده است.", show_alert=True)
    try:
        candidate = info.get("candidates", [])[int(idx_text)]
    except (IndexError, ValueError, TypeError):
        return await call.answer("پوشهٔ انتخاب‌شده معتبر نیست.", show_alert=True)

    subs = [Path(p) for p in info["sub_files"]]
    await _show_incoming_subtitle_actions(
        call,
        Path(candidate["path"]),
        subs,
        [candidate["episode_tag"]] if candidate.get("episode_tag") else None,
    )


# ==============================================================================
# بخش دوم: حالت دستی (همراه با صفحه‌بندی کامل پوشه‌ها و فایل‌ها)
# ==============================================================================

def _build_numbered_stem(pattern: str, orig_vid: Path, index_1based: int, total_count: int) -> str:
    if pattern == "1" or not pattern:
        return AutomationService.clean_stem_for_ebtv(orig_vid.stem)

    clean_pat = re.sub(r'[<>:"/\\|?]', "", pattern).strip()
    for ext in DownloaderService.VIDEO_EXTENSIONS:
        if clean_pat.lower().endswith(ext):
            clean_pat = clean_pat[:-len(ext)]
            break

    clean_pat = AutomationService.clean_stem_for_ebtv(clean_pat)
    num_str = f"{index_1based:02d}"
    if "*" in clean_pat:
        return clean_pat.replace("*", num_str)
    elif total_count > 1:
        return f"{clean_pat}{num_str}"
    return clean_pat


def _resolve_media_dirs(video_path: Path, custom_base_stem: str) -> dict[str, Path]:
    workspace = Path(cfg.get("paths", "workspace_dir")).resolve()
    vid_parent = video_path.resolve().parent

    if vid_parent.name.lower() in ("softsub", "hardsub", "sub", "srt", "raw_temp"):
        movie_base_dir = vid_parent.parent
    elif vid_parent == workspace:
        safe_name = re.sub(r'[<>:"/\\|?*]', "", custom_base_stem).strip().replace(" ", ".")
        movie_base_dir = workspace / (safe_name or "Media_Output")
    else:
        movie_base_dir = vid_parent

    folders = {
        "base": movie_base_dir,
        "softsub": movie_base_dir / "SoftSub",
        "hardsub": movie_base_dir / "Hardsub",
        "sub": movie_base_dir / "srt",
    }
    for p in folders.values():
        p.mkdir(parents=True, exist_ok=True)
    return folders


def _get_sub_upload_dir_without_creating_3_folders(video_path: Path) -> Path:
    vid_parent = video_path.resolve().parent
    for sub_folder_name in ("srt/FA", "srt", "sub"):
        cand = vid_parent / sub_folder_name
        if cand.is_dir():
            return cand
        if vid_parent.parent and (vid_parent.parent / sub_folder_name).is_dir():
            return vid_parent.parent / sub_folder_name
    return vid_parent


def _find_local_subtitles_for_video(video_path: Path) -> list[Path]:
    vid_parent = video_path.resolve().parent
    base_dir = vid_parent.parent if vid_parent.name.lower() in ("softsub", "hardsub", "sub", "srt") else vid_parent
    search_dirs = [
        base_dir / "srt" / "FA",
        base_dir / "srt" / "EN",
        base_dir / "srt",
        base_dir / "sub",
        base_dir,
        vid_parent,
    ]
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
        rule_hint = "✏️ **نام جدید فایل خروجی** را ارسال کنید (یا عدد `1` را بفرستید تا با همان نام فعلی و پسوند `.SoftSub.EBTV` / `.HardSub.EBTV` ذخیره شود):"
    else:
        joined = "\n".join([f"`{i + 1:02d}.` `{v.name}`" for i, v in enumerate(target_videos[:15])])
        mono_info = f"📄 **نام فعلی فایل‌های انتخاب شده ({len(target_videos)} فایل):**\n{joined}\n\n"
        rule_hint = (
            "✏️ **الگوی نام جدید** را ارسال کنید (یا عدد `1` برای حفظ نام‌های فعلی):\n"
            "• اگر علامت `*` بگذارید (مثلاً `Series.S01E*.1080p`)، از `01` شماره‌گذاری می‌شود.\n"
            "• اگر `*` نگذارید، شماره‌های `01`، `02` و... در انتهای نام اضافه می‌شوند:"
        )

    return f"{mono_info}{rule_hint}"


async def _render_sub_folder_browser(
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

    data = await state.get_data()
    selected_paths = set(data.get("selected_video_paths", []))

    await state.update_data(
        sub_browse_dir=str(browse_dir),
        sub_dir_folders=[str(f.resolve()) for f in folders],
        sub_dir_videos=[str(v.resolve()) for v in videos],
        selected_video_paths=list(selected_paths),
        sub_browse_page=page
    )

    is_root = (browse_dir == workspace)

    full_lines = []
    for kind, orig_idx, path_obj in combined[start_idx:end_idx]:
        if kind == "dir":
            full_lines.append(f"`[{orig_idx + 1}]` 📁 `{path_obj.name}`")
        else:
            is_sel = str(path_obj.resolve()) in selected_paths
            mark = "✅" if is_sel else "⬜"
            size_mb = path_obj.stat().st_size / (1024 * 1024)
            full_lines.append(f"{mark} `[F{orig_idx + 1}]` 🎬 `{path_obj.name}` _({size_mb:.1f} MB)_")

    details_block = "\n".join(full_lines) if full_lines else "_(در این مسیر پوشه یا فایل ویدئویی وجود ندارد)_"
    page_badge = f" (صفحه {page + 1} از {total_pages} - کل: {total_items})" if total_pages > 1 else ""

    msg_text = (
        f"🎬 **انتخاب دستی فیلم‌ها (سافت‌ساب / هاردساب فارسی / انکود با `movie.ass`){page_badge}**\n"
        f"📂 مسیر فعلی:\n`{browse_dir}`\n"
        f"✅ کل فایل‌های انتخاب شده: `{len(selected_paths)}` فایل\n\n"
        f"📋 **نام کامل پوشه‌ها و فایل‌های این صفحه:**\n{details_block}\n\n"
        "فایل‌های مورد نظر را تیک بزنید و سپس دکمه **«➡️ ادامه و انتخاب نوع عملیات»** را بزنید:"
    )

    await call.message.edit_text(
        msg_text[:4000],
        reply_markup=subtitle_folder_browser_kb(folders, videos, selected_paths, is_root, page=page),
        parse_mode="Markdown"
    )


@router.callback_query(F.data == "menu:sub_to_files")
async def cb_sub_to_files_start(call: CallbackQuery, state: FSMContext):
    workspace = Path(cfg.get("paths", "workspace_dir")).resolve()
    await state.set_state(SubtitleStates.selecting_target_videos)
    await state.update_data(selected_video_paths=[], operation_mode=None, custom_output_dir=None, sub_browse_page=0)
    await _render_sub_folder_browser(call, state, workspace, page=0)


@router.callback_query(SubtitleStates.selecting_target_videos, F.data.startswith("subnav:page:"))
async def cb_subnav_change_page(call: CallbackQuery, state: FSMContext):
    page = int(call.data.split(":")[2])
    data = await state.get_data()
    curr = Path(data.get("sub_browse_dir", cfg.get("paths", "workspace_dir"))).resolve()
    await _render_sub_folder_browser(call, state, curr, page=page)


@router.callback_query(SubtitleStates.selecting_target_videos, F.data.startswith("subnav:dir:"))
async def cb_subnav_open_dir(call: CallbackQuery, state: FSMContext):
    idx = int(call.data.split(":")[2])
    data = await state.get_data()
    folders = data.get("sub_dir_folders", [])
    if idx < len(folders):
        await _render_sub_folder_browser(call, state, Path(folders[idx]), page=0)


@router.callback_query(SubtitleStates.selecting_target_videos, F.data == "subnav:up")
async def cb_subnav_up(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    curr = Path(data.get("sub_browse_dir", cfg.get("paths", "workspace_dir"))).resolve()
    workspace = Path(cfg.get("paths", "workspace_dir")).resolve()
    parent = curr.parent if curr != workspace else workspace
    await _render_sub_folder_browser(call, state, parent, page=0)


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
    await _render_sub_folder_browser(call, state, curr, page=data.get("sub_browse_page", 0))


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
    await _render_sub_folder_browser(call, state, curr, page=data.get("sub_browse_page", 0))


@router.callback_query(SubtitleStates.selecting_target_videos, F.data == "mvact:proceed")
async def cb_proceed_to_subs(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    sel = sorted(data.get("selected_video_paths", []), key=_natural_sort_key)
    if not sel:
        return await call.answer("⚠️ حداقل یک فایل ویدئویی را از داخل پوشه‌ها تیک بزنید!", show_alert=True)

    await state.update_data(target_videos=sel, fa_sub_path=None, en_sub_path=None)
    await state.set_state(SubtitleStates.waiting_manual_initial_mode)
    await call.message.edit_text(
        f"✅ تعداد **`{len(sel)}` فایل ویدئویی** انتخاب شد.\n\n"
        "👇 **چه عملیاتی می‌خواهید روی این فایل‌ها انجام شود؟**\n"
        "• اگر **«🎬 انکود با movie.ass»** را انتخاب کنید، بدون نیاز به آپلود زیرنویس، مستقیماً با فایل `movie.ass` و حفظ سافت‌ساب انگلیسی در پوشه‌های `1080p-EBTV` / `720p-EBTV` / `480p-EBTV` انکود می‌شوند.",
        reply_markup=subtitle_mode_kb(),
        parse_mode="Markdown"
    )


@router.callback_query(SubtitleStates.waiting_manual_initial_mode, F.data.startswith("submode:"))
async def cb_manual_initial_mode_chosen(call: CallbackQuery, state: FSMContext):
    mode = call.data.split(":")[1]
    await state.update_data(operation_mode=mode)

    if mode == "encode_ass":
        return await _prompt_manual_encode_ass_qualities(call, state)

    await _prompt_fa_subtitle_step(call, state)


async def _prompt_manual_encode_ass_qualities(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    target_videos = [Path(p) for p in data["target_videos"]]
    default_quals = ["1080p", "720p", "480p"]
    await state.set_state(SubtitleStates.waiting_manual_encode_ass_qualities)
    await state.update_data(
        menc_selected_qualities=default_quals,
        menc_auto_upload=True
    )

    logo_path = AutomationService.get_movie_ass_logo_path()
    logo_status = f"✅ موجود (`{logo_path}`)" if logo_path.exists() else f"❌ یافت نشد (`{logo_path}`)"
    vids_preview = "\n".join([f"• `{v.name}`" for v in target_videos[:10]])

    await call.message.edit_text(
        f"🎬 **انکود اختصاصی با `movie.ass` (بدون نیاز به زیرنویس فارسی)**\n\n"
        f"🎨 وضعیت فایل `movie.ass`: {logo_status}\n"
        f"🇬🇧 زیرنویس انگلیسی داخل فایل فیلم به صورت خودکار **سافت‌ساب** می‌شود.\n"
        f"📂 مسیر ذخیره خروجی‌ها: پوشه‌های `1080p-EBTV` (با تگ `6CH`)، `720p-EBTV` و `480p-EBTV`\n\n"
        f"🎞 **فایل‌های ورودی (`{len(target_videos)}` فایل):**\n{vids_preview}\n\n"
        "👇 کیفیت‌های مورد نظر برای انکود را انتخاب کنید:",
        reply_markup=manual_encode_ass_qualities_kb(set(default_quals), auto_upload=True),
        parse_mode="Markdown"
    )


@router.callback_query(SubtitleStates.waiting_manual_encode_ass_qualities, F.data.startswith("menc_q:"))
async def cb_manual_encode_ass_action(call: CallbackQuery, state: FSMContext):
    action = call.data.split(":", 1)[1]
    data = await state.get_data()
    sel_q = set(data.get("menc_selected_qualities", ["1080p", "720p", "480p"]))
    auto_up = bool(data.get("menc_auto_upload", True))

    if action.startswith("toggle:"):
        q = action.split(":")[1]
        sel_q.remove(q) if q in sel_q else sel_q.add(q)
    elif action == "all":
        sel_q = {"1080p", "720p", "480p"}
    elif action == "toggle_upload":
        auto_up = not auto_up
    elif action == "launch":
        if not sel_q:
            return await call.answer("⚠️ حداقل یک کیفیت را انتخاب کنید!", show_alert=True)
        target_videos = [Path(p) for p in data["target_videos"]]
        enc_targets = AutomationService.build_manual_encode_ass_targets(target_videos, list(sel_q))
        brand = cfg.get("branding", "global_title_and_track_name", ".::EmpireBestTv.Com::.")

        scheduled_lines = []
        for ht in enc_targets:
            ht.output_dir.mkdir(parents=True, exist_ok=True)
            t_enc = await queue_engine.add_task(
                "handbrake",
                f"Encode-ASS [{ht.quality_tag}]: {ht.output_file.name}",
                AutomationService.run_hardsub_or_encode_and_optional_upload,
                ht,
                None,
                None,
                brand,
                auto_up
            )
            scheduled_lines.append(f"🎬 `{ht.output_dir.name}/{ht.output_file.name}` (`{t_enc.task_id}`)")

        await state.clear()
        kb = InlineKeyboardBuilder()
        kb.row(InlineKeyboardButton(text="📊 مشاهده صف و تغییر اولویت‌ها", callback_data="menu:queues"))
        kb.row(InlineKeyboardButton(text="🔙 منوی اصلی", callback_data="menu:home"))

        return await call.message.edit_text(
            f"✅ تعداد **`{len(scheduled_lines)}` تسک انکود با `movie.ass`** در صف قرار گرفت!\n\n"
            + "\n".join(scheduled_lines[:15]),
            reply_markup=kb.as_markup(),
            parse_mode="Markdown"
        )

    await state.update_data(menc_selected_qualities=list(sel_q), menc_auto_upload=auto_up)
    await call.message.edit_reply_markup(
        reply_markup=manual_encode_ass_qualities_kb(sel_q, auto_upload=auto_up)
    )


async def _after_fa_subtitle_chosen(
        msg_or_call: Message | CallbackQuery,
        state: FSMContext,
        chosen_fa_sub: Path
):
    SubtitleService.normalize_subtitle_file(chosen_fa_sub)
    await state.update_data(fa_sub_path=str(chosen_fa_sub))
    data = await state.get_data()
    op_mode = data.get("operation_mode")
    ext_low = chosen_fa_sub.suffix.lower()

    first_vid = Path(data["target_videos"][0]).resolve()
    cat = AutomationService.detect_category_from_path(first_vid)

    # اگر انیمه است و فایل زیرنویس .ass است: نسخه .srt را در پوشه srt بساز و مرحله زیرنویس انگلیسی را رد کن!
    if cat == "Anime":
        season_dir = first_vid.parent.parent if first_vid.parent.name.lower() != "anime" else first_vid.parent
        srt_dir = season_dir / "srt"
        srt_dir.mkdir(parents=True, exist_ok=True)
        if chosen_fa_sub.parent.resolve() != srt_dir.resolve():
            dest_sub = srt_dir / chosen_fa_sub.name
            shutil.copy2(chosen_fa_sub, dest_sub)
            chosen_fa_sub = dest_sub
            await state.update_data(fa_sub_path=str(chosen_fa_sub))
        if ext_low in (".ass", ".ssa"):
            SubtitleService.convert_ass_to_srt_file(chosen_fa_sub, srt_dir / f"{chosen_fa_sub.stem}.srt")

        await state.update_data(en_sub_path=None)
        await state.set_state(SubtitleStates.waiting_sync_choice)
        text = (
                f"🎌 **زیرنویس انیمه (`{chosen_fa_sub.name}`) آماده شد!**\n"
                + ("✅ نسخه تبدیل‌شده `.srt` نیز در پوشه `srt` ساخته شد.\n\n" if ext_low in (".ass", ".ssa") else "\n")
                + "🔄 **مرحله سینک (هماهنگ‌سازی) زیرنویس قبل از پردازش:**"
        )
        kb = sync_options_kb()
        if isinstance(msg_or_call, CallbackQuery):
            return await msg_or_call.message.edit_text(text, reply_markup=kb, parse_mode="Markdown")
        return await msg_or_call.answer(text, reply_markup=kb, parse_mode="Markdown")


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
        f"📂 زیرنویس‌های موجود در پوشه فیلم / `srt`: **`{len(local_subs)}` فایل**\n\n"
        "🇮🇷 **مرحله ۱: زیرنویس فارسی**\n"
        "می‌توانید زیرنویس فارسی را از داخل پوشه انتخاب کنید، یا فایل جدید (`.srt` / `.ass` / `.zip` / `.rar`) آپلود نمایید:"
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
        "می‌توانید زیرنویس انگلیسی را از داخل پوشه انتخاب کنید، فایل جدید (`.srt` / `.ass` / `.zip` / `.rar`) بفرستید و یا رد کنید:"
    )
    kb = subtitle_source_kb("en", len(local_subs))
    if isinstance(msg_or_call, CallbackQuery):
        await msg_or_call.message.edit_text(text, reply_markup=kb, parse_mode="Markdown")
    else:
        await msg_or_call.answer(text, reply_markup=kb, parse_mode="Markdown")


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
            "❌ هیچ فایل زیرنویسی (.srt / .ass) در پوشه srt یا کنار فیلم پیدا نشد! لطفاً گزینه آپلود را بزنید.",
            show_alert=True
        )

    sub_lines = [f"`[{i + 1}]` 📄 `{p.name}`" for i, p in enumerate(local_subs[:20])]
    full_subs_block = "\n".join(sub_lines)

    await call.message.edit_text(
        f"📂 **لیست کامل زیرنویس‌های موجود در پوشه فیلم و `srt/`:**\n"
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
        await _after_fa_subtitle_chosen(call, state, chosen_sub)
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
            "📤 لطفاً **فایل زیرنویس فارسی (`.srt` یا `.ass` یا فایل فشرده `.zip`/`.rar`)** را در چت ارسال کنید:",
            reply_markup=skip_or_cancel_kb("skip:fa_sub"),
            parse_mode="Markdown"
        )
    else:
        await state.set_state(SubtitleStates.waiting_en_subtitle)
        await call.message.edit_text(
            "📤 لطفاً **فایل زیرنویس انگلیسی (`.srt` یا `.ass` یا فایل فشرده `.zip`/`.rar`)** را در چت ارسال کنید:",
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

    orig_name = message.document.file_name or "subtitle.srt"
    raw_dest = sub_dir / f"FA_{orig_name}"
    await message.bot.download(message.document, destination=raw_dest)

    if raw_dest.suffix.lower() in SubtitleService.ARCHIVE_EXTENSIONS:
        extracted = SubtitleService.extract_archive(raw_dest, sub_dir, remove_archive=True)
        sub_files = [p for p in extracted if p.suffix.lower() in SubtitleService.SUB_EXTENSIONS]
        if not sub_files:
            return await message.answer(
                "❌ فایل فشرده اکسترکت شد اما هیچ فایل زیرنویسی داخل آن نبود! لطفاً فایل دیگری بفرستید:")
        if len(sub_files) == 1:
            await _after_fa_subtitle_chosen(message, state, sub_files[0])
            return
        await state.update_data(local_sub_candidates=[str(p) for p in sub_files])
        sub_lines = [f"`[{i + 1}]` 📄 `{p.name}`" for i, p in enumerate(sub_files[:20])]
        return await message.answer(
            f"📦 فایل فشرده اکسترکت شد و **`{len(sub_files)}` فایل زیرنویس** در آن پیدا شد:\n"
            + "\n".join(sub_lines)
            + "\n\nکدام فایل به عنوان **زیرنویس فارسی** استفاده شود؟",
            reply_markup=subtitle_file_found_kb("fa", sub_files),
            parse_mode="Markdown"
        )

    if raw_dest.suffix.lower() == ".srt":
        SubtitleService.normalize_srt_utf8(raw_dest)

    await _after_fa_subtitle_chosen(message, state, raw_dest)


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
            "ℹ️ **انکود با HandBrake بدون زیرنویس**.\n\n"
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

    orig_name = message.document.file_name or "subtitle_en.srt"
    en_dest = sub_dir / f"EN_{orig_name}"
    await message.bot.download(message.document, destination=en_dest)

    if en_dest.suffix.lower() in SubtitleService.ARCHIVE_EXTENSIONS:
        extracted = SubtitleService.extract_archive(en_dest, sub_dir, remove_archive=True)
        sub_files = [p for p in extracted if p.suffix.lower() in SubtitleService.SUB_EXTENSIONS]
        if not sub_files:
            return await message.answer("❌ فایل فشرده اکسترکت شد اما هیچ فایل زیرنویسی داخل آن نبود! دوباره بفرستید:")
        if len(sub_files) > 1:
            await state.update_data(local_sub_candidates=[str(p) for p in sub_files])
            sub_lines = [f"`[{i + 1}]` 📄 `{p.name}`" for i, p in enumerate(sub_files[:20])]
            return await message.answer(
                f"📦 فایل فشرده اکسترکت شد و **`{len(sub_files)}` فایل زیرنویس** در آن پیدا شد:\n"
                + "\n".join(sub_lines)
                + "\n\nکدام فایل به عنوان **زیرنویس انگلیسی** استفاده شود؟",
                reply_markup=subtitle_file_found_kb("en", sub_files),
                parse_mode="Markdown"
            )
        en_dest = sub_files[0]

    if en_dest.suffix.lower() == ".srt":
        SubtitleService.normalize_srt_utf8(en_dest)

    await state.update_data(en_sub_path=str(en_dest))
    await state.set_state(SubtitleStates.waiting_sync_choice)
    await message.answer(
        f"✅ زیرنویس **انگلیسی** (`{en_dest.name}`) دریافت شد.\n\n"
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
        return await message.answer("⚠️ لطفاً یک عدد صحیح به میلی‌ثانیه وارد کنید (مثلاً `1500` یا `-1200`):",
                                    parse_mode="Markdown")

    await state.update_data(sync_offset_ms=offset_ms)
    data = await state.get_data()
    await state.set_state(SubtitleStates.waiting_custom_output_name)
    await message.answer(
        f"✅ شیفت زمانی `{offset_ms} ms` ثبت شد.\n\n"
        f"{_format_current_filenames_prompt(data)}",
        parse_mode="Markdown"
    )


async def _ask_output_destination_choice(msg_or_call: Message | CallbackQuery, state: FSMContext):
    await state.set_state(SubtitleStates.waiting_output_dest_choice)
    text = (
        "📂 **انتخاب پوشه مقصد برای ذخیره خروجی:**\n\n"
        "• **حالت پیش‌فرض:** ذخیره در پوشه‌های استاندارد `SoftSub` و `Hardsub`.\n"
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

    if mode == "encode_ass":
        return await _prompt_manual_encode_ass_qualities(call, state)

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


async def _render_output_dest_browser(
        call_or_msg: CallbackQuery | Message,
        state: FSMContext,
        current_dir: Path,
        page: int = 0
):
    workspace = Path(cfg.get("paths", "workspace_dir")).resolve()
    current_dir = current_dir.resolve()
    current_dir.mkdir(parents=True, exist_ok=True)

    subfolders = sorted(
        [p for p in current_dir.iterdir() if p.is_dir() and not p.name.startswith("_") and p.name != "browser_profile"],
        key=lambda x: x.name.lower()
    )
    total_folders = len(subfolders)
    total_pages = max(1, math.ceil(total_folders / BROWSER_PAGE_SIZE))
    page = max(0, min(page, total_pages - 1))
    start_idx = page * BROWSER_PAGE_SIZE
    end_idx = start_idx + BROWSER_PAGE_SIZE

    await state.set_state(SubtitleStates.browsing_output_dest_dir)
    await state.update_data(
        dest_browser_curr_dir=str(current_dir),
        dest_browser_subfolders=[str(f.resolve()) for f in subfolders],
        dest_browser_page=page
    )

    is_root = (current_dir == workspace)
    folder_lines = [
        f"`[{i + 1}]` 📁 `{subfolders[i].name}`"
        for i in range(start_idx, min(end_idx, total_folders))
    ]
    folders_block = "\n".join(folder_lines) if folder_lines else "_(زیرپوشه‌ای در این مسیر وجود ندارد)_"
    page_badge = f" (صفحه {page + 1} از {total_pages})" if total_pages > 1 else ""

    text = (
        f"📁 **انتخاب پوشه مقصد خروجی{page_badge}**\n\n"
        f"📍 **مسیر فعلی باز شده:**\n`{current_dir}`\n\n"
        f"📂 **زیرپوشه‌های این صفحه:**\n{folders_block}\n\n"
        "• برای ذخیره در همین مسیر فعلی، دکمه **«✅ ذخیره در همین پوشه»** را بزنید.\n"
        "• برای رفتن به زیرپوشه‌های بعدی، روی نام زیرپوشه کلیک کنید و یا در همینجا پوشه جدید بسازید:"
    )
    kb = output_dest_tree_browser_kb(subfolders, current_dir, is_root, page=page)

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
    await _render_output_dest_browser(call, state, workspace, page=0)


@router.callback_query(SubtitleStates.browsing_output_dest_dir, F.data.startswith("destnav:page:"))
async def cb_destnav_change_page(call: CallbackQuery, state: FSMContext):
    page = int(call.data.split(":")[2])
    data = await state.get_data()
    curr = Path(data.get("dest_browser_curr_dir", cfg.get("paths", "workspace_dir"))).resolve()
    await _render_output_dest_browser(call, state, curr, page=page)


@router.callback_query(SubtitleStates.browsing_output_dest_dir, F.data.startswith("destnav:open:"))
async def cb_destnav_open_subfolder(call: CallbackQuery, state: FSMContext):
    idx = int(call.data.split(":")[2])
    data = await state.get_data()
    subfolders = data.get("dest_browser_subfolders", [])
    if idx < len(subfolders):
        await _render_output_dest_browser(call, state, Path(subfolders[idx]), page=0)


@router.callback_query(SubtitleStates.browsing_output_dest_dir, F.data == "destnav:up")
async def cb_destnav_up(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    curr = Path(data.get("dest_browser_curr_dir", cfg.get("paths", "workspace_dir"))).resolve()
    workspace = Path(cfg.get("paths", "workspace_dir")).resolve()
    parent = curr.parent if curr != workspace else workspace
    await _render_output_dest_browser(call, state, parent, page=0)


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
    await _render_output_dest_browser(call, state, curr_dir, page=data.get("dest_browser_page", 0))


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
    await _render_output_dest_browser(message, state, new_dir, page=0)


async def _build_and_enqueue_jobs(data: dict, chosen_preset: dict | None) -> list[str]:
    target_videos = [Path(p) for p in data["target_videos"]]
    fa_sub = Path(data["fa_sub_path"]) if data.get("fa_sub_path") else None
    en_sub = Path(data["en_sub_path"]) if data.get("en_sub_path") else None
    custom_name = data.get("custom_output_name", "1")
    mode = data.get("operation_mode", "softsub")
    sync_mode = data.get("sync_mode", "none")
    sync_offset_ms = int(data.get("sync_offset_ms", 0))
    custom_output_dir = data.get("custom_output_dir")
    brand = cfg.get("branding", "global_title_and_track_name", ".::EmpireBestTv.Com::.")

    scheduled_ids = []
    total_vids = len(target_videos)

    for idx, vid in enumerate(target_videos, start=1):
        base_stem = _build_numbered_stem(custom_name, vid, idx, total_vids)

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
            soft_ext = ".mp4" if vid.suffix.lower() == ".mp4" else ".mkv"
            soft_out = soft_dir / f"{base_stem}.SoftSub.EBTV{soft_ext}"
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
            hard_out = hard_dir / f"{base_stem}.HardSub.EBTV.mp4"
            primary_sub = fa_sub
            is_fa_ass = bool(fa_sub and fa_sub.suffix.lower() in (".ass", ".ssa"))
            secondary_sub = en_sub if is_fa_ass else None

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
            callback_data=f"exttrack:{t['id']}:{t['codec']}"
        )
    kb.adjust(1)
    await call.message.edit_text("📤 کدام ترک زیرنویس استخراج شود؟", reply_markup=kb.as_markup())


@router.callback_query(F.data.startswith("exttrack:"))
async def cb_extract_sub_run(call: CallbackQuery, state: FSMContext):
    parts = call.data.split(":")
    track_id = int(parts[1])
    codec_low = parts[2].lower() if len(parts) > 2 else "srt"

    data = await state.get_data()
    sel_files = data.get("fm_selected_files", [])
    video_path = Path(sel_files[0]) if sel_files else Path(data["selected_video"])

    ext = ".ass" if ("ass" in codec_low or "ssa" in codec_low) else ".srt"
    out_sub = video_path.with_name(f"{video_path.stem}_track{track_id}{ext}")

    await call.message.edit_text("⏳ در حال استخراج و استانداردسازی زیرنویس برای موبایل و دسکتاپ...",
                                 parse_mode="Markdown")
    await MKVService.extract_subtitle_track(video_path, track_id, out_sub)
    SubtitleService.normalize_subtitle_file(out_sub)

    await call.message.answer_document(
        FSInputFile(out_sub),
        caption=f"✅ زیرنویس استخراج و استانداردسازی شد:\n`{out_sub.name}`",
        parse_mode="Markdown"
    )

    # اگر فایل استخراج‌شده .ass بود، یک نسخه تبدیل‌شده و تمیز .srt هم کنارش برای راحتی ادیت در موبایل بفرست
    if ext == ".ass":
        srt_copy = out_sub.with_suffix(".srt")
        SubtitleService.convert_ass_to_srt_file(out_sub, srt_copy)
        await call.message.answer_document(
            FSInputFile(srt_copy),
            caption=f"📱 نسخه تبدیل‌شده به **SRT استاندارد** (مخصوص ادیت در موبایل):\n`{srt_copy.name}`",
            parse_mode="Markdown"
        )


@router.callback_query(
    F.data.in_({"vact:softsub", "vact:hardsub", "vact:encode_ass", "vact:full_auto", "fact:batch_sub"}))
async def cb_single_or_batch_video_sub_flow(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    if call.data == "vact:full_auto":
        vid = Path(data["selected_video"]).resolve()
        ep_tag = AutomationService.extract_episode_tag(vid.name)
        target_dir = vid.parent.parent if ep_tag and vid.parent.name.lower() != "workspace" else vid.parent
        return await _init_smart_auto_batch(call, state, target_dir, [ep_tag] if ep_tag else [])

    if call.data == "fact:batch_sub":
        sel_files = sorted(data.get("fm_selected_files", []), key=_natural_sort_key)
        videos = [p for p in sel_files if Path(p).suffix.lower() in DownloaderService.VIDEO_EXTENSIONS]
        if not videos:
            return await call.answer("⚠️ هیچ فایل ویدئویی در بین فایل‌های انتخاب‌شده وجود ندارد!", show_alert=True)
        await state.update_data(target_videos=videos, fa_sub_path=None, en_sub_path=None, custom_output_dir=None)
        await state.set_state(SubtitleStates.waiting_manual_initial_mode)
        return await call.message.edit_text(
            f"✅ تعداد **`{len(videos)}` فایل ویدئویی** انتخاب شد.\n\n"
            "👇 **چه عملیاتی می‌خواهید روی این فایل‌ها انجام شود؟**",
            reply_markup=subtitle_mode_kb(),
            parse_mode="Markdown"
        )

    videos = [data["selected_video"]]
    if call.data == "vact:encode_ass":
        await state.update_data(target_videos=videos, operation_mode="encode_ass")
        return await _prompt_manual_encode_ass_qualities(call, state)

    mode_map = {
        "vact:softsub": "softsub",
        "vact:hardsub": "hardsub",
    }
    await state.update_data(
        target_videos=videos,
        operation_mode=mode_map[call.data],
        fa_sub_path=None,
        en_sub_path=None,
        custom_output_dir=None
    )
    await _prompt_fa_subtitle_step(call, state)