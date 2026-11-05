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
    # استیت‌های اتوماسیون هوشمند ۱-کلیکی (فیلم و سریال SxxEyy)
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
# بخش اول: اتوماسیون کامل و هوشمند فیلم و سریال (Smart Full Automation UX)
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

    await state.set_state(SubtitleStates.sauto_browsing_dir)
    await state.update_data(
        sauto_curr_dir=str(current_dir),
        sauto_subfolders=[str(f.resolve()) for f in folders]
    )

    folder_lines = [f"`[{i+1}]` 📁 `{f.name}`" for i, f in enumerate(folders[:22])]
    list_block = "\n".join(folder_lines) if folder_lines else "_(زیرپوشه‌ای وجود ندارد)_"
    ep_badge = f"\n🎞 **قسمت‌های شناسایی‌شده در این فصل:** `{', '.join(detected_eps)}`" if detected_eps else ""

    text = (
        "🚀 **اتوماسیون کامل و هوشمند (فیلم و سریال)**\n"
        f"📂 مسیر فعلی:\n`{current_dir}`"
        f"{ep_badge}\n\n"
        f"📋 **پوشه‌ها:**\n{list_block}\n\n"
        "💡 **راهنما:**\n"
        "• برای **فیلم**: وارد پوشه فیلم شوید و دکمه **«✅ اجرای اتوماسیون روی کل این پوشه»** را بزنید.\n"
        "• برای **سریال**: وارد پوشه فصل (مثلاً `VisionQuest/S02`) شوید و روی دکمه قسمت مورد نظر (مثلاً `S02E01`) کلیک کنید.\n"
        "• همچنین می‌توانید همین الان مستقیماً **فایل زیرنویس فارسی (`.srt` / `.ass` / `.zip`)** را در چت بفرستید!"
    )
    kb = smart_auto_browser_kb(folders, current_dir, is_root, detected_eps)

    if isinstance(call_or_msg, CallbackQuery):
        await call_or_msg.message.edit_text(text[:4000], reply_markup=kb, parse_mode="Markdown")
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


@router.callback_query(SubtitleStates.sauto_browsing_dir, F.data.startswith("sauto:ep:"))
async def cb_sauto_select_episode(call: CallbackQuery, state: FSMContext):
    ep_tag = call.data.split(":")[2]
    data = await state.get_data()
    curr_dir = Path(data["sauto_curr_dir"]).resolve()
    await _init_smart_auto_plan(call, state, curr_dir, ep_tag)


@router.callback_query(SubtitleStates.sauto_browsing_dir, F.data == "sauto:select_dir")
async def cb_sauto_select_dir(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    curr_dir = Path(data["sauto_curr_dir"]).resolve()
    eps = AutomationService.scan_available_episodes_in_season(curr_dir)
    ep_tag = eps[-1] if eps else None
    await _init_smart_auto_plan(call, state, curr_dir, ep_tag)


async def _init_smart_auto_plan(
    call_or_msg: CallbackQuery | Message,
    state: FSMContext,
    target_dir: Path,
    ep_tag: str | None,
    pre_uploaded_fa_sub: Path | None = None
):
    """ساخت نقشه اتوماسیون و درخواست زیرنویس فارسی (یا پردازش زیرنویس از قبل ارسال‌شده)"""
    plan = await AutomationService.analyze_and_build_plan(target_dir, ep_tag)
    if not plan.softsub_targets:
        msg = (
            f"⚠️ هیچ فایل ویدئویی خامی در پوشه `{target_dir.name}` "
            f"{f'برای قسمت `{ep_tag}` ' if ep_tag else ''}پیدا نشد!\n"
            "لطفاً مطمئن شوید فایل‌های خام در پوشه قرار دارند."
        )
        if isinstance(call_or_msg, CallbackQuery):
            return await call_or_msg.answer(msg[:200], show_alert=True)
        return await call_or_msg.answer(msg, parse_mode="Markdown")

    await state.update_data(
        sauto_target_dir=str(target_dir),
        sauto_ep_tag=plan.season_ep_tag,
        sauto_is_series=plan.is_series,
        sauto_fa_dest_dir=str(plan.fa_sub_dest_dir),
        sauto_en_dest_dir=str(plan.en_sub_dest_dir),
    )

    if pre_uploaded_fa_sub and pre_uploaded_fa_sub.exists():
        return await _process_sauto_fa_subtitle_placement(call_or_msg, state, plan, pre_uploaded_fa_sub)

    # بررسی اینکه آیا از قبل در پوشه srt/FA یا srt زیرنویس فارسی وجود دارد یا خیر
    existing_fa_subs = [
        f for f in plan.fa_sub_dest_dir.iterdir()
        if f.is_file() and f.suffix.lower() in (".srt", ".ass", ".ssa")
        and (not plan.season_ep_tag or plan.season_ep_tag.lower() in f.name.lower())
        and not f.name.lower().endswith(".en.srt")
    ] if plan.fa_sub_dest_dir.exists() else []

    await state.set_state(SubtitleStates.sauto_waiting_fa_sub)
    await state.update_data(sauto_existing_fa_candidates=[str(p.resolve()) for p in existing_fa_subs])

    kb = InlineKeyboardBuilder()
    for idx, f_sub in enumerate(existing_fa_subs[:10]):
        kb.row(
            InlineKeyboardButton(
                text=f"📄 استفاده از زیرنویس موجود: {f_sub.name[:35]}",
                callback_data=f"sauto_pickfa:{idx}"
            )
        )
    kb.row(InlineKeyboardButton(text="🔙 انصراف", callback_data="menu:home"))

    qualities_list = ", ".join([t.quality_folder_name for t in plan.softsub_targets])
    fullhd_status = f"✅ `{plan.fullhd_source_video.name}`" if plan.fullhd_source_video else "⚠️ یافت نشد"

    info_text = (
        f"🚀 **اتوماسیون هوشمند آماده شد!**\n\n"
        f"🎬 عنوان: **`{plan.series_title}`** {f'(`{plan.season_ep_tag}`)' if plan.season_ep_tag else '(فیلم سینمایی)'}\n"
        f"📦 تعداد کیفیت‌های خام شناسایی‌شده برای سافت‌ساب: **`{len(plan.softsub_targets)}` فایل**\n"
        f"   └ پوشه‌ها: `{qualities_list}`\n"
        f"🔥 سورس هاردساب (`FullHD`): {fullhd_status}\n\n"
        f"🇮🇷 **لطفاً فایل زیرنویس فارسی (`.srt` / `.ass` / `.zip`) را ارسال کنید**"
        f"{' و یا از دکمه زیرنویس‌های موجود در پوشه استفاده نمایید:' if existing_fa_subs else ':'}"
    )

    if isinstance(call_or_msg, CallbackQuery):
        await call_or_msg.message.edit_text(info_text[:4000], reply_markup=kb.as_markup(), parse_mode="Markdown")
    else:
        await call_or_msg.answer(info_text[:4000], reply_markup=kb.as_markup(), parse_mode="Markdown")


@router.callback_query(SubtitleStates.sauto_waiting_fa_sub, F.data.startswith("sauto_pickfa:"))
async def cb_sauto_pick_existing_fa(call: CallbackQuery, state: FSMContext):
    idx = int(call.data.split(":")[1])
    data = await state.get_data()
    cands = data.get("sauto_existing_fa_candidates", [])
    if idx >= len(cands):
        return await call.answer("⚠️ فایل یافت نشد.")
    chosen_fa = Path(cands[idx])
    plan = await AutomationService.analyze_and_build_plan(
        Path(data["sauto_target_dir"]), data.get("sauto_ep_tag")
    )
    await _process_sauto_fa_subtitle_placement(call, state, plan, chosen_fa)


@router.message(SubtitleStates.sauto_waiting_fa_sub, F.document)
async def handle_sauto_fa_sub_upload(message: Message, state: FSMContext):
    data = await state.get_data()
    target_dir = Path(data["sauto_target_dir"])
    ep_tag = data.get("sauto_ep_tag")
    plan = await AutomationService.analyze_and_build_plan(target_dir, ep_tag)

    fa_dir = plan.fa_sub_dest_dir
    fa_dir.mkdir(parents=True, exist_ok=True)

    orig_name = message.document.file_name or "subtitle.FA.srt"
    temp_dest = fa_dir / orig_name
    await message.bot.download(message.document, destination=temp_dest)

    if temp_dest.suffix.lower() == ".zip":
        extracted = SubtitleService.extract_zip_archive(temp_dest, fa_dir, remove_zip=True)
        sub_files = [p for p in extracted if p.suffix.lower() in SubtitleService.SUB_EXTENSIONS]
        if not sub_files:
            return await message.answer("❌ هیچ فایل زیرنویسی داخل ZIP یافت نشد! لطفاً فایل دیگری بفرستید:")
        # اگر در فایل زیپ چند قسمت بود، قسمتی که با SxxEyy مطابقت دارد را بردار
        matched_ep_subs = [
            p for p in sub_files
            if ep_tag and AutomationService.extract_episode_tag(p.name) == ep_tag
        ]
        chosen_sub = matched_ep_subs[0] if matched_ep_subs else sub_files[0]
    else:
        chosen_sub = temp_dest
        if chosen_sub.suffix.lower() == ".srt":
            SubtitleService.normalize_srt_utf8(chosen_sub)

    await _process_sauto_fa_subtitle_placement(message, state, plan, chosen_sub)


async def _process_sauto_fa_subtitle_placement(
    call_or_msg: CallbackQuery | Message,
    state: FSMContext,
    plan: AutomationPlan,
    fa_sub_path: Path
):
    """
    قرار دادن زیرنویس فارسی در پوشه درست (srt/FA برای سریال یا srt برای فیلم)
    و پرسیدن سوال درباره نام فایل زیرنویس فارسی
    """
    fa_dir = plan.fa_sub_dest_dir
    fa_dir.mkdir(parents=True, exist_ok=True)

    if fa_sub_path.parent.resolve() != fa_dir.resolve():
        dest_p = fa_dir / fa_sub_path.name
        shutil.copy2(fa_sub_path, dest_p)
        fa_sub_path = dest_p

    ext = fa_sub_path.suffix
    if plan.is_series and plan.season_ep_tag:
        clean_series = re.sub(r'[<>:"/\\|?*]', "", plan.series_title).strip().replace(" ", ".")
        suggested_fa_name = f"{clean_series}.{plan.season_ep_tag}.FA{ext}"
    else:
        clean_movie = AutomationService.clean_stem_for_ebtv(plan.base_dir.name)
        suggested_fa_name = f"{clean_movie}.FA{ext}"

    await state.update_data(
        sauto_fa_sub_current_path=str(fa_sub_path.resolve()),
        sauto_suggested_fa_name=suggested_fa_name
    )
    await state.set_state(SubtitleStates.sauto_waiting_fa_sub_name)

    text = (
        f"✅ زیرنویس فارسی در مسیر استاندارد قرار گرفت:\n"
        f"📂 `{fa_dir}`\n\n"
        f"📄 **نام فعلی زیرنویس (برای کپی لمس کنید):**\n`{fa_sub_path.name}`\n"
        f"✨ **نام استاندارد پیشنهادی:**\n`{suggested_fa_name}`\n\n"
        "✏️ **نام فایل زیرنویس فارسی چه باشد؟**\n"
        "می‌توانید روی یکی از ۲ دکمه زیر کلیک کنید و یا **نام دلخواه خودتان** را تایپ و ارسال نمایید:"
    )
    kb = smart_auto_sub_name_kb(suggested_fa_name, fa_sub_path.name)

    if isinstance(call_or_msg, CallbackQuery):
        await call_or_msg.message.edit_text(text, reply_markup=kb, parse_mode="Markdown")
    else:
        await call_or_msg.answer(text, reply_markup=kb, parse_mode="Markdown")


@router.callback_query(SubtitleStates.sauto_waiting_fa_sub_name, F.data.startswith("sauto_subname:"))
async def cb_sauto_fa_sub_name_choice(call: CallbackQuery, state: FSMContext):
    choice = call.data.split(":")[1]
    data = await state.get_data()
    curr_fa = Path(data["sauto_fa_sub_current_path"])

    if choice == "standard":
        new_name = data["sauto_suggested_fa_name"]
        new_path = curr_fa.with_name(new_name)
        if curr_fa.resolve() != new_path.resolve():
            if new_path.exists():
                new_path.unlink(missing_ok=True)
            curr_fa.rename(new_path)
            curr_fa = new_path

    await state.update_data(sauto_final_fa_sub=str(curr_fa.resolve()))
    await _check_english_sub_step(call, state)


@router.message(SubtitleStates.sauto_waiting_fa_sub_name)
async def handle_sauto_custom_fa_sub_name(message: Message, state: FSMContext):
    raw_name = re.sub(r'[<>:"/\\|?*]', "", (message.text or "").strip()).strip()
    if not raw_name:
        return await message.answer("⚠️ نام وارد شده معتبر نیست. دوباره بفرستید:")

    data = await state.get_data()
    curr_fa = Path(data["sauto_fa_sub_current_path"])
    if raw_name == "1":
        raw_name = data["sauto_suggested_fa_name"]
    elif raw_name == "2":
        raw_name = curr_fa.name

    if not raw_name.lower().endswith(curr_fa.suffix.lower()):
        # اگر پسوند .FA.srt نداشت اضافه کن
        if not raw_name.lower().endswith(".fa"):
            raw_name += ".FA"
        raw_name += curr_fa.suffix

    new_path = curr_fa.with_name(raw_name)
    if curr_fa.resolve() != new_path.resolve():
        if new_path.exists():
            new_path.unlink(missing_ok=True)
        curr_fa.rename(new_path)
        curr_fa = new_path

    await state.update_data(sauto_final_fa_sub=str(curr_fa.resolve()))
    await _check_english_sub_step(message, state)


async def _check_english_sub_step(call_or_msg: CallbackQuery | Message, state: FSMContext):
    """
    بررسی وجود زیرنویس انگلیسی در پوشه srt/EN (برای سریال) یا srt (برای فیلم):
    اگر وجود نداشت، از کاربر می‌پرسد که آیا به طور خودکار از فایل ویدئو استخراج و در آنجا قرار داده شود؟
    """
    data = await state.get_data()
    target_dir = Path(data["sauto_target_dir"])
    ep_tag = data.get("sauto_ep_tag")
    plan = await AutomationService.analyze_and_build_plan(target_dir, ep_tag)

    if plan.existing_en_sub and plan.existing_en_sub.exists():
        await state.update_data(sauto_final_en_sub=str(plan.existing_en_sub))
        return await _prompt_hardsub_qualities_step(
            call_or_msg, state, plan,
            en_note=f"✅ زیرنویس انگلیسی در پوشه یافت شد: `{plan.existing_en_sub.name}`"
        )

    # زیرنویس انگلیسی در پوشه srt/EN (یا srt) وجود ندارد -> از کاربر سوال بپرس
    await state.set_state(SubtitleStates.sauto_waiting_en_decision)
    has_embedded = bool(plan.embedded_en_source_video and plan.embedded_en_track_id is not None)
    await state.update_data(
        sauto_emb_vid=str(plan.embedded_en_source_video) if has_embedded else None,
        sauto_emb_tid=plan.embedded_en_track_id,
        sauto_emb_codec=plan.embedded_en_codec,
    )

    if has_embedded:
        text = (
            f"🇬🇧 **وضعیت زیرنویس انگلیسی ({plan.season_ep_tag or 'فیلم'}):**\n\n"
            f"در پوشه `{plan.en_sub_dest_dir.name}` فایل زیرنویس انگلیسی یافت نشد، اما **داخل فایل ویدئوی `{plan.embedded_en_source_video.name}` زیرنویس انگلیسی وجود دارد!**\n\n"
            "آیا می‌خواهید ربات به طور خودکار زیرنویس انگلیسی را استخراج کرده و در پوشه قرار دهد؟"
        )
    else:
        text = (
            f"🇬🇧 **وضعیت زیرنویس انگلیسی ({plan.season_ep_tag or 'فیلم'}):**\n\n"
            f"زیرنویس انگلیسی در پوشه `{plan.en_sub_dest_dir.name}` یافت نشد.\n"
            "می‌توانید فایل زیرنویس انگلیسی را آپلود کنید یا بدون زیرنویس انگلیسی ادامه دهید:"
        )

    kb = smart_auto_missing_en_kb(has_embedded)
    if isinstance(call_or_msg, CallbackQuery):
        await call_or_msg.message.edit_text(text, reply_markup=kb, parse_mode="Markdown")
    else:
        await call_or_msg.answer(text, reply_markup=kb, parse_mode="Markdown")


@router.callback_query(SubtitleStates.sauto_waiting_en_decision, F.data == "sauto_en:auto_extract")
async def cb_sauto_auto_extract_en(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    target_dir = Path(data["sauto_target_dir"])
    ep_tag = data.get("sauto_ep_tag")
    plan = await AutomationService.analyze_and_build_plan(target_dir, ep_tag)

    emb_vid = Path(data["sauto_emb_vid"])
    emb_tid = int(data["sauto_emb_tid"])
    fa_sub = Path(data["sauto_final_fa_sub"])

    # نام‌گذاری استاندارد زیرنویس انگلیسی هماهنگ با نام زیرنویس فارسی
    en_filename = re.sub(r"\.FA(\.[^.]+)$", r".EN\1", fa_sub.name, flags=re.IGNORECASE)
    if en_filename == fa_sub.name:
        en_filename = f"{fa_sub.stem}.EN.srt"

    out_en_path = plan.en_sub_dest_dir / en_filename
    await call.message.edit_text(
        f"⏳ در حال استخراج خودکار زیرنویس انگلیسی از `{emb_vid.name}` و ذخیره در `{out_en_path}`...",
        parse_mode="Markdown"
    )
    try:
        await MKVService.extract_subtitle_track(emb_vid, emb_tid, out_en_path)
        if out_en_path.suffix.lower() == ".srt":
            SubtitleService.normalize_srt_utf8(out_en_path)
        await state.update_data(sauto_final_en_sub=str(out_en_path.resolve()))
        en_note = f"✅ زیرنویس انگلیسی به طور خودکار در `{out_en_path.name}` قرار گرفت."
    except Exception as e:
        logging.warning(f"Auto EN extract failed: {e}")
        await state.update_data(sauto_final_en_sub=None)
        en_note = f"⚠️ خطا در استخراج خودکار انگلیسی ({e})؛ ادامه با زیرنویس داخلی."

    await _prompt_hardsub_qualities_step(call, state, plan, en_note=en_note)


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
    ep_tag = data.get("sauto_ep_tag")
    plan = await AutomationService.analyze_and_build_plan(target_dir, ep_tag)

    en_dir = plan.en_sub_dest_dir
    en_dir.mkdir(parents=True, exist_ok=True)
    fa_sub = Path(data["sauto_final_fa_sub"])

    en_filename = re.sub(r"\.FA(\.[^.]+)$", r".EN\1", fa_sub.name, flags=re.IGNORECASE)
    if en_filename == fa_sub.name:
        en_filename = f"{fa_sub.stem}.EN.srt"

    temp_dest = en_dir / (message.document.file_name or en_filename)
    await message.bot.download(message.document, destination=temp_dest)

    if temp_dest.suffix.lower() == ".zip":
        extracted = SubtitleService.extract_zip_archive(temp_dest, en_dir, remove_zip=True)
        sub_files = [p for p in extracted if p.suffix.lower() in SubtitleService.SUB_EXTENSIONS]
        chosen_en = sub_files[0] if sub_files else temp_dest
    else:
        chosen_en = temp_dest

    final_en = en_dir / en_filename
    if chosen_en.exists() and chosen_en.resolve() != final_en.resolve():
        if final_en.exists():
            final_en.unlink(missing_ok=True)
        chosen_en.rename(final_en)
        chosen_en = final_en

    if chosen_en.suffix.lower() == ".srt":
        SubtitleService.normalize_srt_utf8(chosen_en)

    await state.update_data(sauto_final_en_sub=str(chosen_en.resolve()))
    await _prompt_hardsub_qualities_step(
        message, state, plan,
        en_note=f"✅ زیرنویس انگلیسی در `{chosen_en.name}` ذخیره شد."
    )


@router.callback_query(F.data == "sauto_en:skip")
async def cb_sauto_skip_en(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    target_dir = Path(data["sauto_target_dir"])
    ep_tag = data.get("sauto_ep_tag")
    plan = await AutomationService.analyze_and_build_plan(target_dir, ep_tag)
    await state.update_data(sauto_final_en_sub=None)
    await _prompt_hardsub_qualities_step(call, state, plan, en_note="⏭ بدون فایل زیرنویس انگلیسی جداگانه.")


async def _prompt_hardsub_qualities_step(
    call_or_msg: CallbackQuery | Message,
    state: FSMContext,
    plan: AutomationPlan,
    en_note: str = ""
):
    """
    نمایش سوال چندگزینه‌ای (Multi-Choice) برای انتخاب کدام یک از ۳ کیفیت اصلی (1080p, 720p, 480p)
    از روی فایل موجود در پوشه FullHD هاردساب شوند.
    """
    default_selected = ["1080p", "720p"]
    await state.set_state(SubtitleStates.sauto_waiting_hb_qualities)
    await state.update_data(sauto_hb_qualities=default_selected)

    soft_summary = "\n".join([
        f"   • `{t.quality_folder_name}` ⬅️ `{t.output_dir.name}/{t.output_file.name}`"
        for t in plan.softsub_targets[:10]
    ])
    fullhd_name = plan.fullhd_source_video.name if plan.fullhd_source_video else "یافت نشد"

    text = (
        f"{en_note}\n\n"
        f"🎯 **مرحله نهایی: انتخاب کیفیت‌های هاردساب (سوال چندگزینه‌ای)**\n\n"
        f"📎 **سافت‌ساب‌ها (`{len(plan.softsub_targets)}` کیفیت خام که خودکار در پوشه متناظر قرار می‌گیرند):**\n"
        f"{soft_summary}\n\n"
        f"🔥 **سورس هاردساب (از پوشه `FullHD`):**\n`{fullhd_name}`\n\n"
        "👇 **کدام یک از ۳ کیفیت اصلی زیر می‌خواهید هاردساب شوند؟** (روی گزینه‌ها کلیک کنید تا تیک بخورند و سپس دکمه شروع اتوماسیون را بزنید):"
    )
    kb = smart_auto_hardsub_qualities_kb(set(default_selected))

    if isinstance(call_or_msg, CallbackQuery):
        await call_or_msg.message.edit_text(text[:4000], reply_markup=kb, parse_mode="Markdown")
    else:
        await call_or_msg.answer(text[:4000], reply_markup=kb, parse_mode="Markdown")


@router.callback_query(SubtitleStates.sauto_waiting_hb_qualities, F.data.startswith("sauto_hbq:"))
async def cb_sauto_hb_quality_action(call: CallbackQuery, state: FSMContext):
    action = call.data.split(":", 1)[1]
    data = await state.get_data()
    selected = set(data.get("sauto_hb_qualities", ["1080p", "720p"]))

    if action.startswith("toggle:"):
        q = action.split(":")[1]
        if q in selected:
            selected.remove(q)
        else:
            selected.add(q)
        await state.update_data(sauto_hb_qualities=list(selected))
        return await call.message.edit_reply_markup(reply_markup=smart_auto_hardsub_qualities_kb(selected))

    elif action == "all":
        selected = {"1080p", "720p", "480p"}
        await state.update_data(sauto_hb_qualities=list(selected))
        return await call.message.edit_reply_markup(reply_markup=smart_auto_hardsub_qualities_kb(selected))

    elif action == "1080_720":
        selected = {"1080p", "720p"}
        await state.update_data(sauto_hb_qualities=list(selected))
        return await call.message.edit_reply_markup(reply_markup=smart_auto_hardsub_qualities_kb(selected))

    elif action == "none":
        selected = set()
        await state.update_data(sauto_hb_qualities=[])
        return await _execute_smart_automation_plan(call, state, [])

    elif action == "launch":
        return await _execute_smart_automation_plan(call, state, list(selected))


async def _execute_smart_automation_plan(
    call: CallbackQuery,
    state: FSMContext,
    selected_hb_qualities: list[str]
):
    """اجرای یکجای تمام سافت‌ساب‌ها در پوشه‌های متناظر و هاردساب‌های انتخابی از پوشه FullHD"""
    data = await state.get_data()
    target_dir = Path(data["sauto_target_dir"])
    ep_tag = data.get("sauto_ep_tag")
    fa_sub = Path(data["sauto_final_fa_sub"]) if data.get("sauto_final_fa_sub") else None
    en_sub = Path(data["sauto_final_en_sub"]) if data.get("sauto_final_en_sub") else None
    brand = cfg.get("branding", "global_title_and_track_name", ".::EmpireBestTv.Com::.")

    plan = await AutomationService.analyze_and_build_plan(target_dir, ep_tag)
    hardsub_targets = AutomationService.build_hardsub_targets(plan, selected_hb_qualities)

    scheduled_soft_lines = []
    scheduled_hard_lines = []

    # ۱. صف‌بندی خودکار تمام سافت‌ساب‌ها در پوشه‌های اختصاصی خودشان (.SoftSub.EBTV.mkv)
    for st in plan.softsub_targets:
        st.output_dir.mkdir(parents=True, exist_ok=True)
        t_soft = await queue_engine.add_task(
            "mkvtoolnix",
            f"SoftSub [{st.quality_folder_name}]: {st.output_file.name}",
            MKVService.merge_softsub,
            st.raw_video,
            st.output_file,
            fa_sub,
            en_sub,
            True,
            brand,
            "none",
            0
        )
        scheduled_soft_lines.append(f"📎 `{st.output_dir.name}/{st.output_file.name}` (`{t_soft.task_id}`)")

    # ۲. صف‌بندی خودکار هاردساب‌ها از روی فایل پوشه FullHD در پوشه‌های 1080p-HardSub / 720p-HardSub / 480p-HardSub
    is_fa_ass = bool(fa_sub and fa_sub.suffix.lower() in (".ass", ".ssa"))
    secondary_en_for_hb = en_sub if is_fa_ass else None

    for ht in hardsub_targets:
        ht.output_dir.mkdir(parents=True, exist_ok=True)
        t_hard = await queue_engine.add_task(
            "handbrake",
            f"HardSub [{ht.quality_tag}]: {ht.output_file.name}",
            HandBrakeService.encode_hardsub,
            ht.source_video,
            fa_sub,
            ht.output_file,
            ht.preset_file,
            ht.preset_name,
            secondary_en_for_hb,
            "none",
            0,
            ht.quality_tag
        )
        scheduled_hard_lines.append(f"🔥 `{ht.output_dir.name}/{ht.output_file.name}` (`{t_hard.task_id}`)")

    await state.clear()
    await state.update_data(current_dir=str(target_dir))

    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text="📊 مشاهده زنده پیشرفت صف‌ها", callback_data="menu:queues"))
    kb.row(InlineKeyboardButton(text="☁️ آپلود کل این پوشه با CuteFTP", callback_data="fact:upload_current_dir"))
    kb.row(InlineKeyboardButton(text="📂 باز کردن در فایل‌منیجر", callback_data="menu:file_mgr"))
    kb.row(InlineKeyboardButton(text="🔙 منوی اصلی", callback_data="menu:home"))

    soft_block = "\n".join(scheduled_soft_lines) if scheduled_soft_lines else "_(ندارد)_"
    hard_block = "\n".join(scheduled_hard_lines) if scheduled_hard_lines else "_(بدون هاردساب)_"

    report = (
        f"🎉 **اتوماسیون کامل `{plan.series_title}` {f'({plan.season_ep_tag})' if plan.season_ep_tag else ''} آغاز شد!**\n\n"
        f"🇮🇷 زیرنویس فارسی:\n`{fa_sub.name if fa_sub else 'ندارد'}`\n"
        f"🇬🇧 زیرنویس انگلیسی:\n`{en_sub.name if en_sub else 'داخلی / ندارد'}`\n\n"
        f"📦 **سافت‌ساب‌های ثبت‌شده (`{len(scheduled_soft_lines)}` فایل):**\n{soft_block}\n\n"
        f"🔥 **هاردساب‌های ثبت‌شده از FullHD (`{len(scheduled_hard_lines)}` فایل):**\n{hard_block}"
    )

    await call.message.edit_text(report[:4000], reply_markup=kb.as_markup(), parse_mode="Markdown")


# ==================== تشخیص بدون کلیک (Zero-Click Drop): ارسال مستقیم زیرنویس در چت ====================
@router.message(StateFilter(None), F.document)
async def catch_direct_subtitle_drop_for_smart_auto(message: Message, state: FSMContext):
    """
    وقتی ادمین بدون رفتن به هیچ منویی، مستقیماً یک فایل زیرنویس (.srt / .ass / .zip) را در ربات رها می‌کند:
    ربات نام فیلم یا قسمت سریال (SxxEyy) را تشخیص داده و اتوماسیون هوشمند را بلافاصله پیشنهاد می‌دهد!
    """
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

    if ext_low == ".zip":
        extracted = SubtitleService.extract_zip_archive(saved_p, temp_dir, remove_zip=True)
        sub_files = [p for p in extracted if p.suffix.lower() in SubtitleService.SUB_EXTENSIONS]
        if not sub_files:
            return await message.answer("❌ فایل ZIP اکسترکت شد اما زیرنویسی داخل آن نبود.")
        saved_p = sub_files[0]
    elif ext_low == ".srt":
        SubtitleService.normalize_srt_utf8(saved_p)

    matched_dir, ep_tag = AutomationService.find_matching_workspace_folder_for_sub(saved_p.name)
    if matched_dir:
        return await _init_smart_auto_plan(message, state, matched_dir, ep_tag, pre_uploaded_fa_sub=saved_p)

    # اگر پوشه به صورت خودکار از نام زیرنویس پیدا نشد، مرورگر پوشه‌ها را باز کن تا با ۱ کلیک پوشه را انتخاب کند
    workspace = Path(cfg.get("paths", "workspace_dir")).resolve()
    await message.answer(
        f"📥 زیرنویس `{saved_p.name}` دریافت شد!\n"
        "لطفاً پوشه فیلم یا فصل سریال مربوط به این زیرنویس را از لیست زیر انتخاب کنید تا اتوماسیون کامل انجام شود:",
        parse_mode="Markdown"
    )
    await _render_smart_auto_browser(message, state, workspace)


# ==============================================================================
# بخش دوم: حالت دستی سافت‌ساب / هاردساب (برای فایل‌های تکی یا خاص)
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

        if mode in ("softsub", "both") and (fa_sub or en_sub):
            soft_out = soft_dir / f"{base_stem}.SoftSub.EBTV.mkv"
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
    data = await state.get_data()
    if call.data == "vact:full_auto":
        vid = Path(data["selected_video"]).resolve()
        ep_tag = AutomationService.extract_episode_tag(vid.name)
        target_dir = vid.parent.parent if ep_tag and vid.parent.name.lower() != "workspace" else vid.parent
        return await _init_smart_auto_plan(call, state, target_dir, ep_tag)

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