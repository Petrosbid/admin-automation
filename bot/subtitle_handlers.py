import logging
import re
import shutil
from pathlib import Path
from aiogram import Router, F
from aiogram.types import Message, CallbackQuery, FSInputFile, InlineKeyboardButton
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.utils.keyboard import InlineKeyboardBuilder

from core.config_loader import cfg
from core.queue_manager import queue_engine
from services.cuteftp_service import CuteFTPService
from services.downloader_service import DownloaderService
from services.subtitle_service import SubtitleService
from services.mkv_service import MKVService
from services.handbrake_service import HandBrakeService
from services.automation_service import AutomationService, AutomationPlan
from bot.keyboards import (
    main_menu_kb, subtitle_folder_browser_kb, subtitle_source_kb,
    subtitle_file_found_kb, sync_options_kb, subtitle_mode_kb,
    skip_or_cancel_kb, presets_kb, output_destination_choice_kb,
    output_dest_tree_browser_kb, smart_auto_browser_kb,
    smart_auto_sub_name_kb, smart_auto_missing_en_kb,
    smart_auto_hardsub_qualities_kb
)

router = Router()


class SubtitleStates(StatesGroup):
    # استیت‌های اتوماسیون هوشمند (تک‌قسمتی، چندقسمتی و فیلم)
    sauto_browsing_dir = State()
    sauto_waiting_fa_sub = State()
    sauto_waiting_fa_sub_name = State()
    sauto_waiting_en_decision = State()
    sauto_waiting_en_upload = State()
    sauto_waiting_hb_qualities = State()

    # استیت‌های حالت دستی
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


def is_admin(user_id: int) -> bool:
    return user_id in cfg.get("telegram", "admin_ids", [])


def _natural_sort_key(path_str: str) -> list:
    name = Path(path_str).name.lower()
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", name)]


# ==============================================================================
# بخش اول: اتوماسیون کامل و هوشمند فیلم و چندقسمتی سریال (Multi-Episode Automation)
# ==============================================================================

async def _render_smart_auto_browser(call_or_msg: CallbackQuery | Message, state: FSMContext, current_dir: Path):
    workspace = Path(cfg.get("paths", "workspace_dir")).resolve()
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

    data = await state.get_data()
    # اگر پوشه عوض شده، انتخاب قسمت‌های قبلی را ریست کن
    prev_dir = data.get("sauto_curr_dir")
    selected_eps = set(data.get("sauto_selected_eps", [])) if prev_dir == str(current_dir) else set()
    selected_eps = {ep for ep in selected_eps if ep in detected_eps}

    await state.set_state(SubtitleStates.sauto_browsing_dir)
    await state.update_data(
        sauto_curr_dir=str(current_dir),
        sauto_subfolders=[str(f.resolve()) for f in folders],
        sauto_detected_eps=detected_eps,
        sauto_selected_eps=sorted(list(selected_eps))
    )

    folder_lines = [f"`[{i+1}]` 📁 `{f.name}`" for i, f in enumerate(folders[:22])]
    list_block = "\n".join(folder_lines) if folder_lines else "_(زیرپوشه‌ای وجود ندارد)_"

    ep_badge = ""
    if detected_episodes := detected_eps:
        ep_badge = (
            f"\n🎞 **قسمت‌های موجود در این فصل ({len(detected_episodes)} قسمت):**\n"
            f"`{', '.join(detected_episodes)}`\n"
            f"✅ **قسمت‌های انتخاب‌شده:** `{len(selected_eps)}` قسمت\n"
        )

    text = (
        "🚀 **اتوماسیون کامل و هوشمند (فیلم و چندقسمتی سریال)**\n"
        f"📂 مسیر فعلی:\n`{current_dir}`\n"
        f"{ep_badge}\n"
        f"📋 **پوشه‌ها:**\n{list_block}\n\n"
        "💡 **راهنما:**\n"
        "• برای **سریال (تک‌قسمتی یا چندقسمتی)**: روی دکمه‌های `SxxEyy` کلیک کنید تا تیک (`✅`) بخورند (یا «انتخاب همه قسمت‌ها» را بزنید) و سپس روی **«🚀 شروع اتوماسیون»** کلیک کنید.\n"
        "• برای **فیلم**: وارد پوشه فیلم شوید و دکمه تایید پوشه را بزنید."
    )
    kb = smart_auto_browser_kb(folders, current_dir, is_root, detected_eps, selected_eps)

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
    await _render_smart_auto_browser(call, state, workspace)


@router.callback_query(F.data == "fact:run_smart_auto_here")
async def cb_run_smart_auto_from_filemgr(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    curr_dir = Path(data.get("current_dir", cfg.get("paths", "workspace_dir"))).resolve()
    await state.update_data(sauto_selected_eps=[])
    await _render_smart_auto_browser(call, state, curr_dir)


@router.callback_query(SubtitleStates.sauto_browsing_dir, F.data.startswith("sauto:open:"))
async def cb_sauto_open_dir(call: CallbackQuery, state: FSMContext):
    idx = int(call.data.split(":")[2])
    data = await state.get_data()
    subfolders = data.get("sauto_subfolders", [])
    if idx < len(subfolders):
        await _render_smart_auto_browser(call, state, Path(subfolders[idx]))


@router.callback_query(SubtitleStates.sauto_browsing_dir, F.data == "sauto:up")
async def cb_sauto_up(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    curr = Path(data.get("sauto_curr_dir", cfg.get("paths", "workspace_dir"))).resolve()
    workspace = Path(cfg.get("paths", "workspace_dir")).resolve()
    parent = curr.parent if curr != workspace else workspace
    await _render_smart_auto_browser(call, state, parent)


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
    await _render_smart_auto_browser(call, state, curr_dir)


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
    await _render_smart_auto_browser(call, state, curr_dir)


@router.callback_query(SubtitleStates.sauto_browsing_dir, F.data == "sauto:ep_proceed")
async def cb_sauto_proceed_selected_episodes(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    selected_eps = sorted(data.get("sauto_selected_eps", []))
    if not selected_eps:
        return await call.answer("⚠️ حداقل یک قسمت را تیک بزنید!", show_alert=True)
    curr_dir = Path(data["sauto_curr_dir"]).resolve()
    await _init_smart_auto_batch(call, state, curr_dir, selected_eps)


@router.callback_query(SubtitleStates.sauto_browsing_dir, F.data == "sauto:select_dir")
async def cb_sauto_select_dir(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    curr_dir = Path(data["sauto_curr_dir"]).resolve()
    eps = AutomationService.scan_available_episodes_in_season(curr_dir)
    await _init_smart_auto_batch(call, state, curr_dir, eps)


async def _init_smart_auto_batch(
    call_or_msg: CallbackQuery | Message,
    state: FSMContext,
    target_dir: Path,
    selected_episodes: list[str],
    pre_uploaded_subs: list[Path] | None = None
):
    """
    موتور شروع اتوماسیون هوشمند (برای ۱ فیلم، ۱ قسمت سریال، یا چندین قسمت سریال به صورت هم‌زمان)
    """
    plans = await AutomationService.analyze_multi_episodes_plan(target_dir, selected_episodes)
    if not plans:
        msg = f"⚠️ هیچ فایل ویدئویی خامی در پوشه `{target_dir.name}` یافت نشد!"
        if isinstance(call_or_msg, CallbackQuery):
            return await call_or_msg.answer(msg, show_alert=True)
        return await call_or_msg.answer(msg, parse_mode="Markdown")

    first_plan = plans[0]
    ep_tags = [p.season_ep_tag for p in plans if p.season_ep_tag]

    await state.update_data(
        sauto_target_dir=str(target_dir),
        sauto_ep_tags=ep_tags,
        sauto_is_series=first_plan.is_series,
        sauto_fa_dest_dir=str(first_plan.fa_sub_dest_dir),
        sauto_en_dest_dir=str(first_plan.en_sub_dest_dir),
        sauto_fa_sub_map={},
        sauto_en_sub_map={},
    )

    # ۱. جمع‌آوری زیرنویس‌های فارسی از فایل‌های آپلودشده قبلی یا موجود در پوشه
    local_subs = AutomationService.collect_all_local_fa_subtitles_in_dir(
        first_plan.base_dir, first_plan.fa_sub_dest_dir
    )
    all_candidate_subs = (pre_uploaded_subs or []) + [p for p in local_subs if p not in (pre_uploaded_subs or [])]

    matched_map = AutomationService.match_subtitles_to_episodes(plans, all_candidate_subs)

    # اگر برای تمام قسمت‌های انتخاب‌شده (یا فیلم) زیرنویس فارسی منطبق در پوشه پیدا شد، مستقیم برو به مرحله قرارگیری و نام‌گذاری!
    required_keys = ep_tags if ep_tags else ["MOVIE"]
    if all(k in matched_map for k in required_keys):
        return await _place_and_ask_fa_sub_names(call_or_msg, state, plans, matched_map)

    # در غیر این صورت، وضعیت زیرنویس‌های یافت‌شده و کسری‌ها را نشان بده تا کاربر آپلود کند (مثلاً یک فایل ZIP شامل همه زیرنویس‌ها)
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
                    text=f"📄 [{idx+1}] انتخاب: {f_sub.name[:34]}",
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
            status_lines.append(f"❌ `{key}`: _زیرنویس فارسی یافت نشد_")

    ep_status_block = "\n".join(status_lines)

    info_text = (
        f"🚀 **اتوماسیون هوشمند آماده شد!**\n\n"
        f"🎬 عنوان: **`{first_plan.series_title}`**\n"
        f"🎞 تعداد قسمت‌های انتخاب‌شده: **`{len(plans)}` مورد**\n"
        f"📦 مجموع فایل‌های خام برای سافت‌ساب: **`{total_soft}` فایل**\n"
        f"🔥 سورس‌های آماده هاردساب (`FullHD`): **`{total_fhd}` فایل**\n\n"
        f"📋 **وضعیت تطبیق زیرنویس فارسی در پوشه:**\n{ep_status_block}\n\n"
        "📤 **لطفاً فایل زیرنویس فارسی (`.srt` / `.ass`) یا یک فایل `.zip` حاوی زیرنویس قسمت‌ها را ارسال کنید**\n"
        "*(می‌توانید چند فایل زیرنویس را هم پشت سر هم بفرستید؛ ربات بر اساس `SxxEyy` آن‌ها را به قسمت مربوطه وصل می‌کند)*:"
    )

    if isinstance(call_or_msg, CallbackQuery):
        await call_or_msg.message.edit_text(info_text[:4000], reply_markup=kb.as_markup(), parse_mode="Markdown")
    else:
        await call_or_msg.answer(info_text[:4000], reply_markup=kb, parse_mode="Markdown")


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


@router.message(SubtitleStates.sauto_waiting_fa_sub, F.document)
async def handle_sauto_fa_sub_upload(message: Message, state: FSMContext):
    data = await state.get_data()
    target_dir = Path(data["sauto_target_dir"])
    ep_tags = data.get("sauto_ep_tags", [])
    plans = await AutomationService.analyze_multi_episodes_plan(target_dir, ep_tags)

    fa_dir = plans[0].fa_sub_dest_dir
    fa_dir.mkdir(parents=True, exist_ok=True)

    orig_name = message.document.file_name or "subtitle.FA.srt"
    temp_dest = fa_dir / orig_name
    await message.bot.download(message.document, destination=temp_dest)

    new_subs: list[Path] = []
    if temp_dest.suffix.lower() == ".zip":
        extracted = SubtitleService.extract_zip_archive(temp_dest, fa_dir, remove_zip=True)
        new_subs = [p for p in extracted if p.suffix.lower() in SubtitleService.SUB_EXTENSIONS]
        if not new_subs:
            return await message.answer("❌ هیچ فایل زیرنویسی داخل ZIP یافت نشد! لطفاً فایل دیگری بفرستید:")
    else:
        if temp_dest.suffix.lower() == ".srt":
            SubtitleService.normalize_srt_utf8(temp_dest)
        new_subs = [temp_dest]

    # ترکیب زیرنویس‌های جدید با زیرنویس‌های قبلی پوشه و تطبیق مجدد با تمام قسمت‌ها
    local_subs = AutomationService.collect_all_local_fa_subtitles_in_dir(plans[0].base_dir, fa_dir)
    all_cands = new_subs + [p for p in local_subs if p not in new_subs]

    matched_map = AutomationService.match_subtitles_to_episodes(plans, all_cands)
    partial_prev = {k: Path(v) for k, v in data.get("sauto_partial_fa_map", {}).items() if Path(v).exists()}
    partial_prev.update(matched_map)

    required_keys = ep_tags if ep_tags else ["MOVIE"]
    if all(k in partial_prev for k in required_keys):
        return await _place_and_ask_fa_sub_names(message, state, plans, partial_prev)

    # اگر هنوز برخی قسمت‌ها زیرنویس ندارند، منتظر فایل‌های بعدی بمان یا اجازه ادامه بده
    await state.update_data(sauto_partial_fa_map={k: str(v) for k, v in partial_prev.items()})
    missing_keys = [k for k in required_keys if k not in partial_prev]

    kb = InlineKeyboardBuilder()
    if partial_prev:
        kb.row(
            InlineKeyboardButton(
                text=f"▶️ ادامه با همین {len(partial_prev)} قسمت تطبیق‌یافته",
                callback_data="sauto_fa:use_partial"
            )
        )
    kb.row(InlineKeyboardButton(text="🔙 انصراف", callback_data="menu:home"))

    await message.answer(
        f"✅ تعداد `{len(partial_prev)}` از `{len(required_keys)}` قسمت زیرنویس فارسی دریافت کردند.\n"
        f"⏳ قسمت‌های باقی‌مانده: `{', '.join(missing_keys)}`\n\n"
        "می‌توانید زیرنویس قسمت‌های باقی‌مانده را هم بفرستید و یا دکمه ادامه را بزنید:",
        reply_markup=kb.as_markup(),
        parse_mode="Markdown"
    )


async def _place_and_ask_fa_sub_names(
    call_or_msg: CallbackQuery | Message,
    state: FSMContext,
    plans: list[AutomationPlan],
    fa_sub_map: dict[str, Path]
):
    """
    قرار دادن زیرنویس‌های فارسی در پوشه استاندارد و پیشنهاد نام دقیق طبق الگوی:
    {name}.S{xx}E{yy}.WEB-DL(یا BluRay).FA.srt
    """
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
            # اگر شماره قسمت واقعی در key وجود دارد (مثلا S02E05 -> 05) از شماره قسمت واقعی برای * استفاده کن
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
        final_fa_map[key] = str(curr_fa.resolve())

    await state.update_data(sauto_final_fa_map=final_fa_map)
    await _check_english_sub_batch_step(message, state)


async def _check_english_sub_batch_step(call_or_msg: CallbackQuery | Message, state: FSMContext):
    """
    بررسی وجود زیرنویس انگلیسی برای تمام قسمت‌های انتخاب‌شده در پوشه srt/EN (یا srt فیلم):
    اگر برای قسمتی وجود نداشت اما داخل فایل ویدئو بود، از کاربر برای استخراج خودکار گروهی سوال می‌پرسد.
    """
    data = await state.get_data()
    target_dir = Path(data["sauto_target_dir"])
    ep_tags = data.get("sauto_ep_tags", [])
    plans = await AutomationService.analyze_multi_episodes_plan(target_dir, ep_tags)

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

    # اگر همه قسمت‌ها از قبل در پوشه srt/EN زیرنویس انگلیسی دارند، مستقیم برو به مرحله انتخاب کیفیت هاردساب!
    if not missing_extractable and not missing_no_source:
        return await _prompt_hardsub_qualities_step(
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
        lines.append("\nآیا می‌خواهید ربات زیرنویس انگلیسی قسمت‌های کسری را **به طور خودکار از فایل فیلم استخراج کرده و در پوشه `srt` قرار دهد**؟")
    else:
        lines.append("\nمی‌توانید فایل زیرنویس انگلیسی (`.srt` / `.zip`) آپلود کنید یا ادامه دهید:")

    kb = smart_auto_missing_en_kb(has_embedded)
    text = "\n".join(lines)
    if isinstance(call_or_msg, CallbackQuery):
        await call_or_msg.message.edit_text(text, reply_markup=kb, parse_mode="Markdown")
    else:
        await call_or_msg.answer(text, reply_markup=kb, parse_mode="Markdown")


@router.callback_query(SubtitleStates.sauto_waiting_en_decision, F.data == "sauto_en:auto_extract")
async def cb_sauto_auto_extract_en(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    target_dir = Path(data["sauto_target_dir"])
    ep_tags = data.get("sauto_ep_tags", [])
    fa_map: dict[str, str] = data.get("sauto_final_fa_map", {})
    en_map: dict[str, str] = data.get("sauto_final_en_map", {})

    plans = await AutomationService.analyze_multi_episodes_plan(target_dir, ep_tags)

    await call.message.edit_text("⏳ در حال استخراج خودکار زیرنویس انگلیسی و قرار دادن در پوشه `srt`...", parse_mode="Markdown")

    extracted_count = 0
    for p in plans:
        key = p.season_ep_tag if (p.is_series and p.season_ep_tag) else "MOVIE"
        if key in en_map:
            continue
        if p.embedded_en_source_video and p.embedded_en_track_id is not None:
            fa_sub_p = Path(fa_map[key]) if key in fa_map else None
            if fa_sub_p:
                en_filename = re.sub(r"\.FA(\.[^.]+)$", r".EN\1", fa_sub_p.name, flags=re.IGNORECASE)
                if en_filename == fa_sub_p.name:
                    en_filename = f"{fa_sub_p.stem}.EN.srt"
            else:
                en_filename = f"{p.series_title}.{key}.EN.srt"

            out_en_path = p.en_sub_dest_dir / en_filename
            try:
                await MKVService.extract_subtitle_track(
                    p.embedded_en_source_video, p.embedded_en_track_id, out_en_path
                )
                if out_en_path.suffix.lower() == ".srt":
                    SubtitleService.normalize_srt_utf8(out_en_path)
                en_map[key] = str(out_en_path.resolve())
                extracted_count += 1
            except Exception as e:
                logging.warning(f"Auto EN extract failed for {key}: {e}")

    await state.update_data(sauto_final_en_map=en_map)
    await _prompt_hardsub_qualities_step(
        call, state, plans,
        en_note=f"✅ تعداد `{extracted_count}` زیرنویس انگلیسی به طور خودکار استخراج و در پوشه قرار گرفت."
    )


@router.callback_query(SubtitleStates.sauto_waiting_en_decision, F.data == "sauto_en:upload")
async def cb_sauto_upload_en_ask(call: CallbackQuery, state: FSMContext):
    await state.set_state(SubtitleStates.sauto_waiting_en_upload)
    await call.message.edit_text(
        "📤 لطفاً **فایل زیرنویس انگلیسی (`.srt` / `.ass` / `.zip`)** را ارسال کنید:",
        reply_markup=skip_or_cancel_kb("sauto_en:skip"),
        parse_mode="Markdown"
    )


@router.message(SubtitleStates.sauto_waiting_en_upload, F.document)
async def handle_sauto_en_upload(message: Message, state: FSMContext):
    data = await state.get_data()
    target_dir = Path(data["sauto_target_dir"])
    ep_tags = data.get("sauto_ep_tags", [])
    fa_map: dict[str, str] = data.get("sauto_final_fa_map", {})
    en_map: dict[str, str] = data.get("sauto_final_en_map", {})

    plans = await AutomationService.analyze_multi_episodes_plan(target_dir, ep_tags)
    en_dir = plans[0].en_sub_dest_dir
    en_dir.mkdir(parents=True, exist_ok=True)

    temp_dest = en_dir / (message.document.file_name or "subtitle.EN.srt")
    await message.bot.download(message.document, destination=temp_dest)

    if temp_dest.suffix.lower() == ".zip":
        extracted = SubtitleService.extract_zip_archive(temp_dest, en_dir, remove_zip=True)
        en_cands = [p for p in extracted if p.suffix.lower() in SubtitleService.SUB_EXTENSIONS]
    else:
        en_cands = [temp_dest]

    matched_en = AutomationService.match_subtitles_to_episodes(plans, en_cands)
    for key, chosen_en in matched_en.items():
        fa_sub_p = Path(fa_map[key]) if key in fa_map else None
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
    await _prompt_hardsub_qualities_step(
        message, state, plans,
        en_note=f"✅ تعداد `{len(en_map)}` زیرنویس انگلیسی در پوشه ذخیره شد."
    )


@router.callback_query(F.data == "sauto_en:skip")
async def cb_sauto_skip_en(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    target_dir = Path(data["sauto_target_dir"])
    ep_tags = data.get("sauto_ep_tags", [])
    plans = await AutomationService.analyze_multi_episodes_plan(target_dir, ep_tags)
    await _prompt_hardsub_qualities_step(call, state, plans, en_note="⏭ ادامه بدون زیرنویس انگلیسی جدید.")


async def _prompt_hardsub_qualities_step(
    call_or_msg: CallbackQuery | Message,
    state: FSMContext,
    plans: list[AutomationPlan],
    en_note: str = ""
):
    default_srt_hb = ["1080p", "720p"]
    default_ass_enc = []
    default_auto_upload = True

    await state.set_state(SubtitleStates.sauto_waiting_hb_qualities)
    await state.update_data(
        sauto_hb_srt_qualities=default_srt_hb,
        sauto_encode_ass_qualities=default_ass_enc,
        sauto_auto_upload=default_auto_upload
    )

    total_soft = sum(len(p.softsub_targets) for p in plans)
    fhd_list = [f"• `{p.season_ep_tag or 'فیلم'}`: `{p.fullhd_source_video.name}`" for p in plans if p.fullhd_source_video]
    fhd_block = "\n".join(fhd_list[:8]) if fhd_list else "_(فایل FullHD یافت نشد)_"
    movie_ass_path = AutomationService.get_movie_ass_logo_path()
    ass_status = f"✅ موجود (`{movie_ass_path}`)" if movie_ass_path.exists() else f"⚠️ یافت نشد (`{movie_ass_path}`)"

    text = (
        f"{en_note}\n\n"
        f"🎯 **مرحله نهایی: انتخاب کیفیت‌های هاردساب / انکود و آپلود خودکار**\n\n"
        f"🎞 تعداد قسمت‌های آماده: **`{len(plans)}` مورد** ▏ 📎 سافت‌ساب‌ها: **`{total_soft}` فایل**\n"
        f"🎨 وضعیت فایل تگ انکود (`movie.ass`): {ass_status}\n"
        f"🔥 **سورس `FullHD`:**\n{fhd_block}\n\n"
        "1️⃣ **هاردساب فارسی SRT:** بدون زیرنویس انگلیسی، ذخیره در `1080p-HardSub` / `720p-HardSub` / `480p-HardSub`\n"
        "2️⃣ **انکود با `movie.ass`:** همراه با سافت‌ساب انگلیسی، ذخیره در `1080p-EBTV` (با تگ `6CH`) / `720p-EBTV` / `480p-EBTV`"
    )
    kb = smart_auto_hardsub_qualities_kb(
        set(default_srt_hb),
        set(default_ass_enc),
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
    srt_set = set(data.get("sauto_hb_srt_qualities", ["1080p", "720p"]))
    ass_set = set(data.get("sauto_encode_ass_qualities", []))
    auto_up = bool(data.get("sauto_auto_upload", True))

    if action.startswith("toggle_srt:"):
        q = action.split(":")[1]
        srt_set.remove(q) if q in srt_set else srt_set.add(q)
    elif action.startswith("toggle_ass:"):
        q = action.split(":")[1]
        ass_set.remove(q) if q in ass_set else ass_set.add(q)
    elif action == "preset_both_hd":
        srt_set = {"1080p", "720p"}
        ass_set = {"1080p", "720p"}
    elif action == "clear_all":
        srt_set.clear()
        ass_set.clear()
    elif action == "toggle_upload":
        auto_up = not auto_up
    elif action == "launch":
        return await _execute_smart_automation_batch(call, state, list(srt_set), list(ass_set), auto_up)

    await state.update_data(
        sauto_hb_srt_qualities=list(srt_set),
        sauto_encode_ass_qualities=list(ass_set),
        sauto_auto_upload=auto_up
    )
    await call.message.edit_reply_markup(
        reply_markup=smart_auto_hardsub_qualities_kb(srt_set, ass_set, auto_upload=auto_up)
    )


async def _execute_smart_automation_batch(
    call: CallbackQuery,
    state: FSMContext,
    selected_hb_srt: list[str],
    selected_encode_ass: list[str],
    auto_upload: bool
):
    data = await state.get_data()
    target_dir = Path(data["sauto_target_dir"])
    ep_tags = data.get("sauto_ep_tags", [])
    fa_map: dict[str, str] = data.get("sauto_final_fa_map", {})
    en_map: dict[str, str] = data.get("sauto_final_en_map", {})
    brand = cfg.get("branding", "global_title_and_track_name", ".::EmpireBestTv.Com::.")

    plans = await AutomationService.analyze_multi_episodes_plan(target_dir, ep_tags)

    scheduled_soft_lines = []
    scheduled_hard_lines = []
    scheduled_enc_lines = []

    for plan in plans:
        key = plan.season_ep_tag if (plan.is_series and plan.season_ep_tag) else "MOVIE"
        fa_sub = Path(fa_map[key]) if key in fa_map else None
        en_sub = Path(en_map[key]) if key in en_map else plan.existing_en_sub

        # ۱. آپلود خودکار فایل‌های زیرنویس استاندارد شده در پوشه srt/FA و srt/EN سرور
        if auto_upload:
            if fa_sub and fa_sub.exists():
                r_dir_fa = AutomationService.compute_server_remote_dir_for_local_path(fa_sub)
                await queue_engine.add_task("upload", f"AutoUpload Sub [{r_dir_fa}]: {fa_sub.name}", CuteFTPService.upload_path, fa_sub, r_dir_fa)
            if en_sub and en_sub.exists():
                r_dir_en = AutomationService.compute_server_remote_dir_for_local_path(en_sub)
                await queue_engine.add_task("upload", f"AutoUpload Sub [{r_dir_en}]: {en_sub.name}", CuteFTPService.upload_path, en_sub, r_dir_en)

        # ۲. صف‌بندی سافت‌ساب‌ها برای تمام پوشه‌های کیفیت خام موجود
        if fa_sub:
            for st in plan.softsub_targets:
                st.output_dir.mkdir(parents=True, exist_ok=True)
                t_soft = await queue_engine.add_task(
                    "mkvtoolnix",
                    f"SoftSub [{st.quality_folder_name}]: {st.output_file.name}",
                    AutomationService.run_softsub_and_optional_upload,
                    st.raw_video,
                    st.output_file,
                    fa_sub,
                    en_sub,
                    brand,
                    auto_upload
                )
                scheduled_soft_lines.append(f"📎 `{st.output_dir.name}/{st.output_file.name}` (`{t_soft.task_id}`)")

        # ۳. صف‌بندی هاردساب‌های فارسی SRT و انکودهای movie.ass از روی فایل پوشه FullHD
        hb_targets = AutomationService.build_hardsub_and_encode_targets(
            plan, selected_hb_srt, selected_encode_ass
        )
        for ht in hb_targets:
            ht.output_dir.mkdir(parents=True, exist_ok=True)
            label_prefix = "Encode-ASS" if ht.mode == "encode_ass" else "HardSub-SRT"
            t_hb = await queue_engine.add_task(
                "handbrake",
                f"{label_prefix} [{ht.quality_tag}]: {ht.output_file.name}",
                AutomationService.run_hardsub_or_encode_and_optional_upload,
                ht,
                fa_sub,
                en_sub,
                brand,
                auto_upload
            )
            if ht.mode == "encode_ass":
                scheduled_enc_lines.append(f"🎬 `{ht.output_dir.name}/{ht.output_file.name}` (`{t_hb.task_id}`)")
            else:
                scheduled_hard_lines.append(f"🔥 `{ht.output_dir.name}/{ht.output_file.name}` (`{t_hb.task_id}`)")

    await state.clear()
    await state.update_data(current_dir=str(target_dir))

    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text="📊 مشاهده صف و تغییر اولویت‌ها", callback_data="menu:queues"))
    kb.row(InlineKeyboardButton(text="📂 باز کردن در فایل‌منیجر", callback_data="menu:file_mgr"))
    kb.row(InlineKeyboardButton(text="🔙 منوی اصلی", callback_data="menu:home"))

    title_str = plans[0].series_title if plans else target_dir.name
    up_status = "🟢 فعال (پس از اتمام هر فایل به ترتیب آپلود می‌شود)" if auto_upload else "🔴 غیرفعال"

    report = (
        f"🎉 **اتوماسیون کامل `{title_str}` آغاز شد!**\n"
        f"☁️ آپلود خودکار به هاست: {up_status}\n\n"
        f"📎 **سافت‌ساب‌ها (`{len(scheduled_soft_lines)}` فایل):**\n"
        + ("\n".join(scheduled_soft_lines[:8]) or "_(ندارد)_")
        + f"\n\n🔥 **هاردساب‌های فارسی SRT (`{len(scheduled_hard_lines)}` فایل):**\n"
        + ("\n".join(scheduled_hard_lines[:6]) or "_(ندارد)_")
        + f"\n\n🎬 **انکودهای `movie.ass` در پوشه EBTV (`{len(scheduled_enc_lines)}` فایل):**\n"
        + ("\n".join(scheduled_enc_lines[:6]) or "_(ندارد)_")
    )

    await call.message.edit_text(report[:4000], reply_markup=kb.as_markup(), parse_mode="Markdown")


# ==================== تشخیص بدون کلیک (Zero-Click Drop): ارسال مستقیم زیرنویس یا ZIP چندقسمتی در چت ====================
@router.message(StateFilter(None), F.document)
async def catch_direct_subtitle_drop_for_smart_auto(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    fname = message.document.file_name or ""
    ext_low = Path(fname).suffix.lower()
    if ext_low not in (".srt", ".ass", ".ssa", ".zip"):
        return

    temp_dir = Path(cfg.get("paths", "workspace_dir")) / "_incoming_subs"
    temp_dir.mkdir(parents=True, exist_ok=True)
    saved_p = temp_dir / fname
    await message.bot.download(message.document, destination=saved_p)

    sub_files: list[Path] = []
    if ext_low == ".zip":
        extracted = SubtitleService.extract_zip_archive(saved_p, temp_dir, remove_zip=True)
        sub_files = [p for p in extracted if p.suffix.lower() in SubtitleService.SUB_EXTENSIONS]
        if not sub_files:
            return await message.answer("❌ فایل ZIP اکسترکت شد اما زیرنویسی داخل آن نبود.")
    else:
        if ext_low == ".srt":
            SubtitleService.normalize_srt_utf8(saved_p)
        sub_files = [saved_p]

    matched_dir, matched_eps = AutomationService.find_matching_workspace_folder_for_sub(fname)
    if not matched_dir and sub_files:
        matched_dir, matched_eps = AutomationService.find_matching_workspace_folder_for_sub(sub_files[0].name)

    if matched_dir:
        # اگر چند فایل زیرنویس داخل ZIP بوده، تمام قسمت‌های متناظرشان را پیدا کن
        if len(sub_files) > 1:
            zip_eps = sorted({
                ep for sf in sub_files
                if (ep := AutomationService.extract_episode_tag(sf.name))
            })
            if zip_eps:
                matched_eps = zip_eps
        return await _init_smart_auto_batch(message, state, matched_dir, matched_eps, pre_uploaded_subs=sub_files)

    workspace = Path(cfg.get("paths", "workspace_dir")).resolve()
    await message.answer(
        f"📥 تعداد `{len(sub_files)}` فایل زیرنویس دریافت شد!\n"
        "لطفاً پوشه فیلم یا فصل سریال مربوطه را از لیست زیر انتخاب کنید تا اتوماسیون کامل انجام شود:",
        parse_mode="Markdown"
    )
    await _render_smart_auto_browser(message, state, workspace)


# ==============================================================================
# بخش دوم: حالت دستی سافت‌ساب / هاردساب
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
        joined = "\n".join([f"`{i+1:02d}.` `{v.name}`" for i, v in enumerate(target_videos[:15])])
        mono_info = f"📄 **نام فعلی فایل‌های انتخاب شده ({len(target_videos)} فایل):**\n{joined}\n\n"
        rule_hint = (
            "✏️ **الگوی نام جدید** را ارسال کنید (یا عدد `1` برای حفظ نام‌های فعلی):\n"
            "• اگر علامت `*` بگذارید (مثلاً `Series.S01E*.1080p`)، از `01` شماره‌گذاری می‌شود.\n"
            "• اگر `*` نگذارید، شماره‌های `01`، `02` و... در انتهای نام اضافه می‌شوند:"
        )

    return f"{mono_info}{rule_hint}"


async def _after_fa_subtitle_chosen(
    msg_or_call: Message | CallbackQuery,
    state: FSMContext,
    chosen_fa_sub: Path
):
    await state.update_data(fa_sub_path=str(chosen_fa_sub))
    data = await state.get_data()
    op_mode = data.get("operation_mode")
    ext_low = chosen_fa_sub.suffix.lower()

    if op_mode == "hardsub" and ext_low == ".srt":
        await state.update_data(en_sub_path=None)
        await state.set_state(SubtitleStates.waiting_sync_choice)
        text = (
            f"✅ زیرنویس فارسی (`{chosen_fa_sub.name}`) با فرمت **SRT** برای هاردساب انتخاب شد.\n"
            "ℹ️ *(برای هاردسابِ فایل SRT هیچ زیرنویس انگلیسی‌ای اضافه نمی‌شود)*\n\n"
            "🔄 **مرحله سینک (هماهنگ‌سازی) زیرنویس قبل از پردازش:**\n"
            "چگونه می‌خواهید تایمینگ زیرنویس با فیلم سینک شود؟"
        )
        kb = sync_options_kb()
        if isinstance(msg_or_call, CallbackQuery):
            return await msg_or_call.message.edit_text(text, reply_markup=kb, parse_mode="Markdown")
        else:
            return await msg_or_call.answer(text, reply_markup=kb, parse_mode="Markdown")

    await _prompt_en_subtitle_step(
        msg_or_call,
        state,
        prefix_note=f"✅ زیرنویس فارسی انتخاب شد:\n`{chosen_fa_sub.name}`\n\n"
    )


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
        "می‌توانید زیرنویس فارسی را از داخل پوشه انتخاب کنید، یا فایل جدید (`.srt` / `.ass` / `.zip`) آپلود نمایید:"
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
        "می‌توانید زیرنویس انگلیسی را از داخل پوشه انتخاب کنید، فایل جدید (`.srt` / `.ass` / `.zip`) بفرستید و یا رد کنید:"
    )
    kb = subtitle_source_kb("en", len(local_subs))
    if isinstance(msg_or_call, CallbackQuery):
        await msg_or_call.message.edit_text(text, reply_markup=kb, parse_mode="Markdown")
    else:
        await msg_or_call.answer(text, reply_markup=kb, parse_mode="Markdown")


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
        f"🎬 **انتخاب دستی فیلم‌ها برای زیرنویس / انکود**\n"
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

    sub_lines = [f"`[{i+1}]` 📄 `{p.name}`" for i, p in enumerate(local_subs[:20])]
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
            "📤 لطفاً **فایل زیرنویس فارسی (`.srt` یا `.ass` یا فایل فشرده `.zip`)** را در چت ارسال کنید:",
            reply_markup=skip_or_cancel_kb("skip:fa_sub"),
            parse_mode="Markdown"
        )
    else:
        await state.set_state(SubtitleStates.waiting_en_subtitle)
        await call.message.edit_text(
            "📤 لطفاً **فایل زیرنویس انگلیسی (`.srt` یا `.ass` یا فایل فشرده `.zip`)** را در چت ارسال کنید:",
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

    if raw_dest.suffix.lower() == ".zip":
        extracted = SubtitleService.extract_zip_archive(raw_dest, sub_dir, remove_zip=True)
        sub_files = [p for p in extracted if p.suffix.lower() in SubtitleService.SUB_EXTENSIONS]
        if not sub_files:
            return await message.answer("❌ فایل ZIP اکسترکت شد اما هیچ فایل زیرنویسی داخل آن نبود! لطفاً فایل دیگری بفرستید:")
        if len(sub_files) == 1:
            await _after_fa_subtitle_chosen(message, state, sub_files[0])
            return
        await state.update_data(local_sub_candidates=[str(p) for p in sub_files])
        sub_lines = [f"`[{i+1}]` 📄 `{p.name}`" for i, p in enumerate(sub_files[:20])]
        return await message.answer(
            f"📦 فایل ZIP اکسترکت شد و **`{len(sub_files)}` فایل زیرنویس** در آن پیدا شد:\n"
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

    if en_dest.suffix.lower() == ".zip":
        extracted = SubtitleService.extract_zip_archive(en_dest, sub_dir, remove_zip=True)
        sub_files = [p for p in extracted if p.suffix.lower() in SubtitleService.SUB_EXTENSIONS]
        if not sub_files:
            return await message.answer("❌ فایل ZIP اکسترکت شد اما هیچ فایل زیرنویسی داخل آن نبود! دوباره بفرستید:")
        if len(sub_files) > 1:
            await state.update_data(local_sub_candidates=[str(p) for p in sub_files])
            sub_lines = [f"`[{i+1}]` 📄 `{p.name}`" for i, p in enumerate(sub_files[:20])]
            return await message.answer(
                f"📦 فایل ZIP اکسترکت شد و **`{len(sub_files)}` فایل زیرنویس** در آن پیدا شد:\n"
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
    mode = call.data.split(":")[1]  # 'softsub' | 'hardsub' | 'encode_ass' | 'both'
    await state.update_data(operation_mode=mode)

    if mode in ("hardsub", "encode_ass", "both"):
        await state.set_state(SubtitleStates.waiting_preset_choice)
        return await call.message.edit_text(
            "🔥 لطفاً **پریست HandBrake** را از لیست زیر انتخاب کنید:",
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

        if mode == "encode_ass" and chosen_preset:
            logo_ass = AutomationService.get_movie_ass_logo_path()
            ep_tag = AutomationService.extract_episode_tag(vid.name) or ""
            src_type = AutomationService.detect_source_type(vid.name)
            q_tag = "1080p" if "1080" in chosen_preset["name"] else (
                "720p" if "720" in chosen_preset["name"] else ("480p" if "480" in chosen_preset["name"] else "720p"))

            if custom_output_dir:
                enc_dir = Path(custom_output_dir)
            else:
                enc_dir = (vid.parent.parent / f"{q_tag}-EBTV") if ep_tag else vid.parent
            enc_dir.mkdir(parents=True, exist_ok=True)

            if custom_name == "1":
                enc_filename = AutomationService.build_encode_ass_filename(
                    vid.parent.parent.name if ep_tag else base_stem, ep_tag, q_tag, src_type
                )
            else:
                ch_tag = ".6CH" if q_tag == "1080p" else ""
                enc_filename = f"{base_stem}{ch_tag}.EBTV.mp4"

            enc_out = enc_dir / enc_filename
            t_enc = await queue_engine.add_task(
                "handbrake",
                f"Encode-ASS ({chosen_preset['name']}): {enc_out.name}",
                HandBrakeService.encode_hardsub,
                vid,
                logo_ass,
                enc_out,
                chosen_preset["file"],
                chosen_preset["name"],
                en_sub,
                "none",
                0,
                q_tag
            )
            scheduled_ids.append(f"🎬 Encode-ASS (`{enc_out.name}` در `{enc_dir.name}/`): `{t_enc.task_id}`")

        if mode in ("softsub", "both") and (fa_sub or en_sub):
            # حفظ فرمت .mp4 در صورتی که ویدئوی اصلی .mp4 باشد
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
    data = await state.get_data()
    if call.data == "vact:full_auto":
        vid = Path(data["selected_video"]).resolve()
        ep_tag = AutomationService.extract_episode_tag(vid.name)
        target_dir = vid.parent.parent if ep_tag and vid.parent.name.lower() != "workspace" else vid.parent
        return await _init_smart_auto_batch(call, state, target_dir, [ep_tag] if ep_tag else [])

    mode_map = {
        "vact:softsub": "softsub",
        "vact:hardsub": "hardsub",
        "fact:batch_sub": None,
    }
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