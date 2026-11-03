import re
import uuid
from pathlib import Path
from aiogram import Router, F, Bot
from aiogram.types import Message, CallbackQuery, FSInputFile, InlineKeyboardButton
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.utils.keyboard import InlineKeyboardBuilder

from core.config_loader import cfg
from core.queue_manager import queue_engine
from services.scraper_service import ScraperService
from services.downloader_service import DownloaderService
from services.subtitle_service import SubtitleService
from services.mkv_service import MKVService
from services.handbrake_service import HandBrakeService
from services.cuteftp_service import CuteFTPService
from services.monitor_service import monitor_service
from bot.keyboards import (
    main_menu_kb, monitor_menu_kb, folder_structure_choice_kb,
    target_folders_kb, search_categories_kb, search_sites_kb,
    search_results_kb, file_browser_kb, video_actions_kb,
    presets_kb, multi_video_select_kb, subtitle_mode_kb,
    skip_or_cancel_kb, sync_options_kb
)

router = Router()


class BotStates(StatesGroup):
    # ساخت پوشه جدید بدون لینک
    waiting_new_folder_name = State()
    waiting_new_folder_type = State()

    # آپلود زیرنویس در پوشه هدف
    waiting_target_folder_sub_file = State()

    # ۱. جستجو در سایت‌ها
    waiting_search_query = State()

    # ۲. دانلود لینک مستقیم / تورنت (qbt) / مگا / گوگل درایو
    waiting_direct_folder_name = State()
    waiting_direct_link = State()

    # ۳. دانلود لینک و استخراج خودکار زیرنویس و ارسال در تلگرام
    waiting_link_for_sub_extract = State()

    # ۴. انتخاب فایل‌ها، دریافت زیرنویس (FA/EN)، سینک زیرنویس و مرج/هاردساب
    selecting_target_videos = State()
    waiting_fa_subtitle = State()
    waiting_en_subtitle = State()
    waiting_sync_choice = State()
    waiting_manual_sync_ms = State()
    waiting_custom_output_name = State()
    waiting_operation_mode = State()
    waiting_preset_choice = State()


def is_admin(user_id: int) -> bool:
    return user_id in cfg.get("telegram", "admin_ids", [])


def _format_current_filenames_prompt(data: dict) -> str:
    """ساخت متن راهنمای تغییر نام شامل نام فعلی فایل اصلی به صورت Monospace"""
    target_videos = [Path(p) for p in data.get("target_videos", [])]
    if len(target_videos) == 1:
        v_file = target_videos[0]
        mono_info = (
            f"📄 **نام فعلی فایل اصلی (برای کپی لمس کنید):**\n"
            f"`{v_file.name}`\n"
            f"🔹 نام بدون پسوند:\n"
            f"`{v_file.stem}`\n\n"
        )
    else:
        joined = "\n".join([f"• `{v.name}`" for v in target_videos[:10]])
        mono_info = f"📄 **نام فعلی فایل‌های انتخاب شده:**\n{joined}\n\n"

    return (
        f"{mono_info}"
        "✏️ **نام جدید فایل خروجی** را ارسال کنید (یا عدد `1` را بفرستید تا با همان نام فعلی فایل اصلی به همراه تگ سایت ذخیره شود):"
    )


# ==================== منوی اصلی ====================
@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return await message.answer("⛔ شما دسترسی ادمین ندارید.")
    await state.clear()
    brand = cfg.get("branding", "global_title_and_track_name", ".::EmpireBestTv.Com::.")
    hb_workers = cfg.get("concurrency", "handbrake_workers", 2)
    await message.answer(
        f"🎬 **پنل اتوماسیون مدیا و آپلودر ({brand})**\n\n"
        f"🔥 ظرفیت هاردساب همزمان HandBrake: **`{hb_workers}` پردازش موازی**\n"
        "📡 پایشگر ۵ دقیقه‌ای سایت‌های **Pahe.ink**، **PSA.wf** و **YTS.gg** فعال است.\n"
        "✅ زیرنویس در **سافت‌ساب به صورت عادی** (فقط فارسی و انگلیسی) و در **هاردساب به صورت بولد و زرد (`#fbff00`)** اعمال می‌شود.",
        reply_markup=main_menu_kb(),
        parse_mode="Markdown"
    )


@router.callback_query(F.data == "menu:home")
async def cb_home(call: CallbackQuery, state: FSMContext):
    await state.clear()
    brand = cfg.get("branding", "global_title_and_track_name", ".::EmpireBestTv.Com::.")
    await call.message.edit_text(
        f"🎬 **پنل اتوماسیون مدیا و آپلودر ({brand})**\nیکی از گزینه‌های زیر را انتخاب کنید:",
        reply_markup=main_menu_kb(),
        parse_mode="Markdown"
    )


@router.callback_query(F.data == "menu:reload_cfg")
async def cb_reload_cfg(call: CallbackQuery):
    cfg.reload()
    await call.answer("✅ فایل config.yaml با موفقیت بازخوانی شد!", show_alert=True)


# ==================== ۰. بخش مانیتورینگ ۵ دقیقه‌ای سایت‌ها و حل کپچا ====================
@router.callback_query(F.data == "menu:monitor")
async def cb_monitor_menu(call: CallbackQuery):
    enabled = bool(cfg.get("monitor", "enabled", True))
    interval = cfg.get("monitor", "interval_seconds", 300)
    await call.message.edit_text(
        "📡 **پنل مانیتورینگ خودکار سایت‌ها**\n\n"
        "🌐 سایت‌های تحت رصد:\n"
        "1️⃣ `https://pahe.ink/`\n"
        "2️⃣ `https://psa.wf/`\n"
        "3️⃣ `https://yts.gg/home`\n\n"
        f"⏱ بازه رفرش خودکار: هر `{interval // 60}` دقیقه یک‌بار\n"
        f"📊 وضعیت فعلی: {'🟢 فعال' if enabled else '🔴 غیرفعال'}\n\n"
        "💡 در صورت برخورد به کپچا، دکمه **«باز کردن صفحات روی سرور برای حل کپچا»** را بزنید.",
        reply_markup=monitor_menu_kb(enabled),
        parse_mode="Markdown"
    )


@router.callback_query(F.data == "mon:toggle")
async def cb_monitor_toggle(call: CallbackQuery):
    mon_sec = cfg.get("monitor") or {}
    current = bool(mon_sec.get("enabled", True))
    mon_sec["enabled"] = not current
    await call.answer(f"وضعیت مانیتورینگ به {'فعال' if not current else 'غیرفعال'} تغییر یافت.")
    await cb_monitor_menu(call)


@router.callback_query(F.data == "mon:open_captcha")
async def cb_monitor_open_captcha(call: CallbackQuery):
    await call.message.edit_text(
        "⏳ در حال باز کردن پنجره مرورگر روی دسکتاپ ویندوز سرور برای سایت‌های **Pahe.ink**، **PSA.wf** و **YTS.gg**...",
        parse_mode="Markdown"
    )
    msg = await monitor_service.open_captcha_tabs_for_admin()
    enabled = bool(cfg.get("monitor", "enabled", True))
    await call.message.edit_text(msg, reply_markup=monitor_menu_kb(enabled), parse_mode="Markdown")


@router.callback_query(F.data == "mon:check_now")
async def cb_monitor_check_now(call: CallbackQuery):
    await call.message.edit_text("⏳ در حال رفرش و بررسی فوری سایت‌های **Pahe.ink**، **PSA.wf** و **YTS.gg**...", parse_mode="Markdown")
    counts = await monitor_service.check_all_sites_once(call.bot, notify_on_first_seed=True)
    total_new = sum(counts.values())
    enabled = bool(cfg.get("monitor", "enabled", True))
    await call.message.edit_text(
        f"✅ **بررسی فوری انجام شد!**\n\n"
        f"🔹 پست‌های جدید Pahe.ink: `{counts['pahe']}`\n"
        f"🔹 پست‌های جدید PSA.wf: `{counts['psa']}`\n"
        f"🔹 پست‌های جدید YTS.gg: `{counts['yts']}`\n\n"
        f"합 مجموع پست‌های جدید ارسال شده: `{total_new}`",
        reply_markup=monitor_menu_kb(enabled),
        parse_mode="Markdown"
    )


@router.callback_query(F.data.startswith("mon:latest:"))
async def cb_monitor_latest_site(call: CallbackQuery):
    site_key = call.data.split(":")[2]
    await call.message.edit_text(f"⏳ در حال دریافت آخرین پست‌های `{site_key.upper()}`...", parse_mode="Markdown")
    try:
        posts = await monitor_service.fetch_site_posts(site_key, bot=call.bot)
        if not posts:
            return await call.message.edit_text("❌ پستی دریافت نشد.", reply_markup=monitor_menu_kb(True))

        for p in posts[:3]:
            await monitor_service.send_post_notification(call.bot, p)

        enabled = bool(cfg.get("monitor", "enabled", True))
        await call.message.edit_text(
            f"✅ ۳ پست آخر سایت `{site_key.upper()}` در چت ارسال شد.",
            reply_markup=monitor_menu_kb(enabled),
            parse_mode="Markdown"
        )
    except Exception as e:
        await call.message.edit_text(f"❌ خطا در ارتباط با سایت:\n`{e}`", reply_markup=monitor_menu_kb(True), parse_mode="Markdown")


# ==================== ساخت پوشه جدید در Workspace بدون دادن لینک ====================
@router.callback_query(F.data == "menu:create_folder")
async def cb_create_folder_menu(call: CallbackQuery, state: FSMContext):
    workspace = Path(cfg.get("paths", "workspace_dir")).resolve()
    await state.update_data(mkdir_parent=str(workspace))
    await state.set_state(BotStates.waiting_new_folder_name)
    await call.message.edit_text(
        f"📁 **ساخت پوشه جدید در Workspace (بدون لینک دانلود)**\n"
        f"مسیر پایه: `{workspace}`\n\n"
        "✏️ لطفاً **نام پوشه جدید** (نام فیلم یا سریال) را ارسال کنید:",
        parse_mode="Markdown"
    )


@router.callback_query(F.data == "fact:mkdir_here")
async def cb_mkdir_in_current_dir(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    parent_dir = data.get("current_dir", cfg.get("paths", "workspace_dir"))
    await state.update_data(mkdir_parent=parent_dir)
    await state.set_state(BotStates.waiting_new_folder_name)
    await call.message.edit_text(
        f"📁 **ساخت پوشه جدید در مسیر:**\n`{parent_dir}`\n\n"
        "✏️ لطفاً **نام پوشه جدید** را ارسال کنید:",
        parse_mode="Markdown"
    )


@router.callback_query(F.data.startswith("qmkdir:"))
async def cb_quick_mkdir_from_monitor(call: CallbackQuery, state: FSMContext):
    folder_title = call.data.split(":", 1)[1]
    folders = DownloaderService.create_movie_folders(folder_title)
    await state.update_data(target_sub_dir=str(folders["sub"]), target_base_dir=str(folders["base"]))

    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text="📥 آپلود زیرنویس در همین پوشه", callback_data="qsub:now"))
    kb.row(InlineKeyboardButton(text="📂 باز کردن در فایل‌منیجر", callback_data="menu:file_mgr"))
    kb.row(InlineKeyboardButton(text="🔙 منوی اصلی", callback_data="menu:home"))

    await call.message.answer(
        f"✅ پوشه `{folders['base'].name}` به همراه زیرپوشه‌های `softsub`، `hardsub` و `sub` در Workspace ساخته شد!\n\n"
        f"📂 مسیر: `{folders['base']}`",
        reply_markup=kb.as_markup(),
        parse_mode="Markdown"
    )
    await call.answer("پوشه ساخته شد!")


@router.message(BotStates.waiting_new_folder_name)
async def handle_new_folder_name(message: Message, state: FSMContext):
    raw_name = message.text.strip()
    safe_name = re.sub(r'[<>:"/\\|?*]', "", raw_name).strip().replace(" ", "_")
    if not safe_name:
        return await message.answer("⚠️ نام پوشه معتبر نیست. دوباره ارسال کنید:")

    await state.update_data(new_folder_name=safe_name)
    await state.set_state(BotStates.waiting_new_folder_type)
    await message.answer(
        f"📁 نام پوشه: `{safe_name}`\n"
        "نوع ساختار پوشه را انتخاب کنید:",
        reply_markup=folder_structure_choice_kb(),
        parse_mode="Markdown"
    )


@router.callback_query(BotStates.waiting_new_folder_type, F.data.startswith("mktype:"))
async def cb_create_folder_finalize(call: CallbackQuery, state: FSMContext):
    mk_type = call.data.split(":")[1]  # standard | empty
    data = await state.get_data()
    parent_dir = Path(data.get("mkdir_parent", cfg.get("paths", "workspace_dir")))
    folder_name = data["new_folder_name"]
    new_dir = parent_dir / folder_name
    new_dir.mkdir(parents=True, exist_ok=True)

    if mk_type == "standard":
        for sub_name in ("softsub", "hardsub", "sub"):
            (new_dir / sub_name).mkdir(parents=True, exist_ok=True)
        sub_target = new_dir / "sub"
        desc = "به همراه زیرپوشه‌های `softsub`، `hardsub` و `sub`"
    else:
        sub_target = new_dir
        desc = "به صورت پوشه خام"

    await state.clear()
    await state.update_data(target_sub_dir=str(sub_target), current_dir=str(new_dir))

    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text="📥 آپلود زیرنویس در این پوشه", callback_data="qsub:now"))
    kb.row(InlineKeyboardButton(text="📂 مشاهده پوشه در فایل‌منیجر", callback_data="menu:file_mgr"))
    kb.row(InlineKeyboardButton(text="🔙 منوی اصلی", callback_data="menu:home"))

    await call.message.edit_text(
        f"✅ پوشه جدید {desc} ساخته شد!\n\n"
        f"📂 مسیر: `{new_dir}`",
        reply_markup=kb.as_markup(),
        parse_mode="Markdown"
    )


# ==================== آپلود کردن زیرنویس در پوشه هدف (بدون زرد کردن) ====================
@router.callback_query(F.data == "menu:upload_sub_target")
async def cb_upload_sub_select_folder(call: CallbackQuery, state: FSMContext):
    workspace = Path(cfg.get("paths", "workspace_dir")).resolve()
    folders = [p for p in sorted(workspace.iterdir(), key=lambda x: x.name.lower()) if p.is_dir() and not p.name.startswith("_")]

    if not folders:
        return await call.answer("⚠️ هنوز هیچ پوشه‌ای در Workspace وجود ندارد! ابتدا یک پوشه بسازید.", show_alert=True)

    await state.update_data(available_target_folders=[str(f) for f in folders])
    await call.message.edit_text(
        "📥 **آپلود زیرنویس در پوشه هدف**\n\n"
        "پوشه فیلم یا سریال مورد نظر را از لیست زیر انتخاب کنید:",
        reply_markup=target_folders_kb(folders, workspace),
        parse_mode="Markdown"
    )


@router.callback_query(F.data.startswith("tsubdir:"))
async def cb_target_folder_chosen(call: CallbackQuery, state: FSMContext):
    idx = int(call.data.split(":")[1])
    data = await state.get_data()
    chosen_folder = Path(data["available_target_folders"][idx])

    dest_dir = (chosen_folder / "sub") if (chosen_folder / "sub").exists() else chosen_folder
    await state.update_data(target_sub_dir=str(dest_dir))
    await state.set_state(BotStates.waiting_target_folder_sub_file)
    await call.message.edit_text(
        f"📂 پوشه هدف انتخاب شد:\n`{dest_dir}`\n\n"
        "📎 لطفاً **فایل زیرنویس (`.srt` یا `.ass`)** را ارسال کنید تا در پوشه هدف ذخیره شود:",
        parse_mode="Markdown"
    )


@router.callback_query(F.data.in_({"fact:upload_sub_here", "qsub:now"}))
async def cb_upload_sub_in_active_dir(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    if call.data == "qsub:now" and data.get("target_sub_dir"):
        dest_dir = Path(data["target_sub_dir"])
    else:
        curr = Path(data.get("current_dir", cfg.get("paths", "workspace_dir")))
        dest_dir = (curr / "sub") if (curr / "sub").exists() else curr

    await state.update_data(target_sub_dir=str(dest_dir))
    await state.set_state(BotStates.waiting_target_folder_sub_file)
    await call.message.edit_text(
        f"📂 مسیر ذخیره زیرنویس:\n`{dest_dir}`\n\n"
        "📎 لطفاً **فایل زیرنویس (`.srt` یا `.ass`)** را ارسال کنید تا در این پوشه ذخیره شود:",
        parse_mode="Markdown"
    )


@router.message(BotStates.waiting_target_folder_sub_file, F.document)
async def handle_target_folder_sub_file(message: Message, state: FSMContext):
    data = await state.get_data()
    dest_dir = Path(data["target_sub_dir"])
    dest_dir.mkdir(parents=True, exist_ok=True)

    file_name = message.document.file_name or "subtitle.srt"
    dest_path = dest_dir / file_name
    await message.bot.download(message.document, destination=dest_path)

    if dest_path.suffix.lower() == ".srt":
        SubtitleService.normalize_srt_utf8(dest_path)

    await state.clear()
    await state.update_data(target_sub_dir=str(dest_dir), current_dir=str(dest_dir))

    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text="➕ آپلود یک زیرنویس دیگر در همین پوشه", callback_data="qsub:now"))
    kb.row(InlineKeyboardButton(text="📂 مشاهده در فایل‌منیجر", callback_data="menu:file_mgr"))
    kb.row(InlineKeyboardButton(text="🔙 منوی اصلی", callback_data="menu:home"))

    await message.answer(
        f"✅ زیرنویس `{file_name}` با موفقیت در پوشه هدف ذخیره شد!\n\n"
        f"📂 مسیر فایل:\n`{dest_path}`",
        reply_markup=kb.as_markup(),
        parse_mode="Markdown"
    )


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
    await state.set_state(BotStates.waiting_search_query)
    await call.message.edit_text(f"✏️ نام فیلم، سریال یا انیمه را برای جستجو در `{site_key}` ارسال کنید:", parse_mode="Markdown")


@router.message(BotStates.waiting_search_query)
async def handle_search_query(message: Message, state: FSMContext):
    data = await state.get_data()
    site_key = data["selected_site"]
    wait_msg = await message.answer("⏳ در حال جستجو...")

    try:
        results = await ScraperService.search(site_key, message.text.strip())
        if not results:
            return await wait_msg.edit_text("❌ نتیجه‌ای یافت نشد.", reply_markup=main_menu_kb())

        await state.update_data(search_results=results)
        await wait_msg.edit_text(
            f"✅ نتایج یافت شده برای `{message.text}`:\nروی گزینه دلخواه کلیک کنید تا با `qbt` یا `aria2c` دانلود شود:",
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

    folders = DownloaderService.create_movie_folders(chosen["title"][:45])
    task = await queue_engine.add_task(
        "download",
        f"دانلود: {chosen['title'][:35]}",
        DownloaderService.download,
        chosen["target"],
        folders["base"]
    )
    await call.message.edit_text(
        f"✅ به صف دانلود (`qbt` / `aria2c`) اضافه شد!\n"
        f"🆔 کد تسک: `{task.task_id}`\n📂 پوشه ساخته شده: `{folders['base'].name}`",
        reply_markup=main_menu_kb(),
        parse_mode="Markdown"
    )


# ==================== ۲. دانلود لینک مستقیم / تورنت (qbt) / مگا / گوگل درایو ====================
@router.callback_query(F.data == "menu:direct_dl")
async def cb_direct_dl(call: CallbackQuery, state: FSMContext):
    await state.set_state(BotStates.waiting_direct_folder_name)
    await call.message.edit_text(
        "📁 ابتدا **نام پوشه فیلم یا سریال** را وارد کنید (مثلاً `Severance S02E01 1080p`):",
        parse_mode="Markdown"
    )


@router.message(BotStates.waiting_direct_folder_name)
async def handle_direct_folder_name(message: Message, state: FSMContext):
    folder_name = message.text.strip()
    data = await state.get_data()

    pending_target = data.get("pending_direct_target")
    if pending_target:
        folders = DownloaderService.create_movie_folders(folder_name)
        task = await queue_engine.add_task(
            "download",
            f"دانلود در {folders['base'].name}",
            DownloaderService.download,
            pending_target,
            folders["base"]
        )
        await state.clear()
        return await message.answer(
            f"✅ دانلود با موفقیت در صف قرار گرفت!\n"
            f"📂 پوشه: `{folders['base'].name}` (شامل `softsub`, `hardsub`, `sub`)\n"
            f"🆔 کد تسک: `{task.task_id}`",
            reply_markup=main_menu_kb(),
            parse_mode="Markdown"
        )

    await state.update_data(dl_folder_name=folder_name)
    await state.set_state(BotStates.waiting_direct_link)
    await message.answer(
        "🔗 حالا **لینک مستقیم، لینک Magnet، لینک تورنت، Mega.nz یا Google Drive** را ارسال کنید (یا فایل `.torrent` بفرستید):",
        parse_mode="Markdown"
    )


@router.message(BotStates.waiting_direct_link)
async def handle_direct_link(message: Message, state: FSMContext):
    data = await state.get_data()
    folders = DownloaderService.create_movie_folders(data["dl_folder_name"])

    if message.document and message.document.file_name.lower().endswith(".torrent"):
        torrent_path = folders["base"] / message.document.file_name
        await message.bot.download(message.document, destination=torrent_path)
        target = str(torrent_path)
    else:
        target = (message.text or "").strip()

    task = await queue_engine.add_task(
        "download",
        f"دانلود در {folders['base'].name}",
        DownloaderService.download,
        target,
        folders["base"]
    )
    await state.clear()
    await message.answer(
        f"✅ دانلود در پوشه `{folders['base'].name}` به صف اضافه شد!\n"
        f"پوشه‌های `softsub`, `hardsub`, `sub` نیز ساخته شدند.\n🆔 کد تسک: `{task.task_id}`",
        reply_markup=main_menu_kb(),
        parse_mode="Markdown"
    )


# ==================== ۳. ارسال لینک دانلود فیلم و استخراج خودکار زیرنویس آن ====================
async def _download_and_extract_subs_job(
    bot: Bot,
    chat_id: int,
    target_link: str,
    temp_dir: Path,
    progress_callback=None
) -> list[str]:
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
        await bot.send_message(chat_id, f"⚠️ فیلم `{video_file.name}` دانلود شد اما هیچ ترک زیرنویسی داخل آن وجود نداشت.", parse_mode="Markdown")
        return []

    for sub_p in extracted:
        await bot.send_document(
            chat_id,
            FSInputFile(sub_p),
            caption=f"📤 زیرنویس استخراج شده از:\n`{video_file.name}`\n📄 فایل: `{sub_p.name}`",
            parse_mode="Markdown"
        )
    return [str(p) for p in extracted]


@router.callback_query(F.data == "menu:link_extract_sub")
async def cb_link_extract_sub(call: CallbackQuery, state: FSMContext):
    await state.set_state(BotStates.waiting_link_for_sub_extract)
    await call.message.edit_text(
        "📤 **دانلود فیلم و استخراج خودکار زیرنویس**\n\n"
        "لینک مستقیم فیلم، لینک Magnet/تورنت، یا لینک Mega/GDrive را ارسال کنید (یا فایل `.torrent` بفرستید).\n"
        "ربات فیلم را دانلود کرده و تمام زیرنویس‌های داخل آن را استخراج و در همین چت برایتان ارسال می‌کند:",
        parse_mode="Markdown"
    )


@router.message(BotStates.waiting_link_for_sub_extract)
async def handle_link_for_sub_extract(message: Message, state: FSMContext):
    job_uid = str(uuid.uuid4())[:6]
    temp_dir = Path(cfg.get("paths", "workspace_dir")) / f"SubExtract_{job_uid}"
    temp_dir.mkdir(parents=True, exist_ok=True)

    if message.document and message.document.file_name.lower().endswith(".torrent"):
        t_path = temp_dir / message.document.file_name
        await message.bot.download(message.document, destination=t_path)
        target = str(t_path)
    else:
        target = (message.text or "").strip()

    task = await queue_engine.add_task(
        "download",
        f"دانلود + استخراج زیرنویس ({job_uid})",
        _download_and_extract_subs_job,
        message.bot,
        message.chat.id,
        target,
        temp_dir
    )
    await state.clear()
    await message.answer(
        f"✅ در صف قرار گرفت! (کد تسک: `{task.task_id}`)\n"
        "به محض اتمام دانلود، فایل‌های زیرنویس استخراج شده در همین صفحه برایتان ارسال می‌شوند.",
        reply_markup=main_menu_kb(),
        parse_mode="Markdown"
    )


# ==================== ۴. انتخاب فیلم(ها)، دریافت زیرنویس (FA/EN) و سینک کردن ====================
@router.callback_query(F.data == "menu:sub_to_files")
async def cb_sub_to_files_start(call: CallbackQuery, state: FSMContext):
    workspace = Path(cfg.get("paths", "workspace_dir"))
    videos = DownloaderService.find_video_files(workspace)
    if not videos:
        return await call.answer("❌ هیچ فایل ویدئویی در پوشه workspace یافت نشد!", show_alert=True)

    await state.set_state(BotStates.selecting_target_videos)
    await state.update_data(all_videos=[str(v) for v in videos], selected_indices=[], operation_mode=None)
    await call.message.edit_text(
        "🎬 **انتخاب فیلم(ها) برای سافت‌ساب / هاردساب**\n"
        "روی فایل‌های مورد نظر کلیک کنید تا تیک (`✅`) بخورند، سپس دکمه «ادامه به مرحله ارسال زیرنویس» را بزنید:",
        reply_markup=multi_video_select_kb(videos, set()),
        parse_mode="Markdown"
    )


@router.callback_query(BotStates.selecting_target_videos, F.data.startswith("mvsel:"))
async def cb_toggle_video_selection(call: CallbackQuery, state: FSMContext):
    idx = int(call.data.split(":")[1])
    data = await state.get_data()
    videos = [Path(p) for p in data["all_videos"]]
    sel = set(data.get("selected_indices", []))

    if idx in sel:
        sel.remove(idx)
    else:
        sel.add(idx)

    await state.update_data(selected_indices=list(sel))
    await call.message.edit_reply_markup(reply_markup=multi_video_select_kb(videos, sel))


@router.callback_query(BotStates.selecting_target_videos, F.data == "mvact:proceed")
async def cb_proceed_to_subs(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    sel = data.get("selected_indices", [])
    if not sel:
        return await call.answer("⚠️ حداقل یک فایل ویدئویی را تیک بزنید!", show_alert=True)

    target_videos = [data["all_videos"][i] for i in sorted(sel)]
    await state.update_data(target_videos=target_videos, fa_sub_path=None, en_sub_path=None)
    await state.set_state(BotStates.waiting_fa_subtitle)
    await call.message.edit_text(
        f"✅ تعداد `{len(target_videos)}` فایل فیلم انتخاب شد.\n\n"
        "🇮🇷 **مرحله ۱ زیرنویس:** لطفاً **فایل زیرنویس فارسی (`.srt` یا `.ass`)** را ارسال کنید\n"
        "*(زیرنویس در سافت‌ساب بدون تغییر رنگ و در هاردساب به رنگ زرد `#fbff00` و بولد اعمال می‌شود)*:",
        reply_markup=skip_or_cancel_kb("skip:fa_sub"),
        parse_mode="Markdown"
    )


@router.callback_query(BotStates.waiting_fa_subtitle, F.data == "skip:fa_sub")
async def cb_skip_fa_sub(call: CallbackQuery, state: FSMContext):
    await state.set_state(BotStates.waiting_en_subtitle)
    await call.message.edit_text(
        "🇬🇧 **مرحله ۲ زیرنویس:** حالا **فایل زیرنویس انگلیسی (`.srt` یا `.ass`)** را ارسال کنید:",
        reply_markup=skip_or_cancel_kb("skip:en_sub"),
        parse_mode="Markdown"
    )


@router.message(BotStates.waiting_fa_subtitle, F.document)
async def handle_fa_sub_upload(message: Message, state: FSMContext):
    data = await state.get_data()
    first_video = Path(data["target_videos"][0])
    sub_dir = first_video.parent / "sub" if (first_video.parent / "sub").exists() else first_video.parent
    fa_dest = sub_dir / f"FA_{message.document.file_name}"
    await message.bot.download(message.document, destination=fa_dest)

    # فقط نرمال‌سازی UTF-8 بدون زرد و بولد کردن (تا در سافت‌ساب عادی بماند)
    if fa_dest.suffix.lower() == ".srt":
        SubtitleService.normalize_srt_utf8(fa_dest)

    await state.update_data(fa_sub_path=str(fa_dest))
    await state.set_state(BotStates.waiting_en_subtitle)
    await message.answer(
        "✅ زیرنویس **فارسی** دریافت شد.\n\n"
        "🇬🇧 **مرحله ۲ زیرنویس:** اگر **زیرنویس انگلیسی** هم دارید فایل آن را بفرستید (تا تمام زیرنویس‌های قدیمی فیلم حذف شوند و فقط همین دو زیرنویس بمانند).\n"
        "در صورت زدن دکمه «رد شدن»، اولین زیرنویس انگلیسی داخل خود فیلم (در صورت وجود) حفظ و بقیه حذف می‌شوند:",
        reply_markup=skip_or_cancel_kb("skip:en_sub"),
        parse_mode="Markdown"
    )


@router.callback_query(BotStates.waiting_en_subtitle, F.data == "skip:en_sub")
async def cb_skip_en_sub(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    if not data.get("fa_sub_path") and not data.get("en_sub_path"):
        return await call.answer("⚠️ حداقل یکی از زیرنویس‌های فارسی یا انگلیسی باید ارسال شود!", show_alert=True)

    await state.set_state(BotStates.waiting_sync_choice)
    await call.message.edit_text(
        "🔄 **مرحله سینک (هماهنگ‌سازی) زیرنویس قبل از پردازش:**\n"
        "چگونه می‌خواهید تایمینگ زیرنویس با فیلم سینک شود؟",
        reply_markup=sync_options_kb(),
        parse_mode="Markdown"
    )


@router.message(BotStates.waiting_en_subtitle, F.document)
async def handle_en_sub_upload(message: Message, state: FSMContext):
    data = await state.get_data()
    first_video = Path(data["target_videos"][0])
    sub_dir = first_video.parent / "sub" if (first_video.parent / "sub").exists() else first_video.parent
    en_dest = sub_dir / f"EN_{message.document.file_name}"
    await message.bot.download(message.document, destination=en_dest)

    if en_dest.suffix.lower() == ".srt":
        SubtitleService.normalize_srt_utf8(en_dest)

    await state.update_data(en_sub_path=str(en_dest))
    await state.set_state(BotStates.waiting_sync_choice)
    await message.answer(
        "✅ زیرنویس **انگلیسی** نیز دریافت شد.\n\n"
        "🔄 **مرحله سینک (هماهنگ‌سازی) زیرنویس قبل از پردازش:**\n"
        "چگونه می‌خواهید تایمینگ زیرنویس فارسی با فیلم سینک شود؟",
        reply_markup=sync_options_kb(),
        parse_mode="Markdown"
    )


@router.callback_query(BotStates.waiting_sync_choice, F.data.startswith("syncopt:"))
async def cb_sync_option_selected(call: CallbackQuery, state: FSMContext):
    sync_opt = call.data.split(":")[1]  # auto | manual | none
    await state.update_data(sync_mode=sync_opt, sync_offset_ms=0)

    if sync_opt == "manual":
        await state.set_state(BotStates.waiting_manual_sync_ms)
        return await call.message.edit_text(
            "⏱ **شیفت زمانی دستی زیرنویس (به میلی‌ثانیه):**\n\n"
            "عدد مورد نظر را وارد کنید:\n"
            "• مثلاً `+1500` یعنی زیرنویس **۱.۵ ثانیه دیرتر** نمایش داده شود.\n"
            "• مثلاً `-800` یعنی زیرنویس **۰.۸ ثانیه زودتر** نمایش داده شود.",
            parse_mode="Markdown"
        )

    data = await state.get_data()
    await state.set_state(BotStates.waiting_custom_output_name)
    await call.message.edit_text(
        _format_current_filenames_prompt(data),
        parse_mode="Markdown"
    )


@router.message(BotStates.waiting_manual_sync_ms)
async def handle_manual_sync_ms(message: Message, state: FSMContext):
    try:
        offset_ms = int(message.text.strip().replace("+", ""))
    except ValueError:
        return await message.answer("⚠️ لطفاً یک عدد صحیح به میلی‌ثانیه وارد کنید (مثلاً `1500` یا `-1200`):", parse_mode="Markdown")

    await state.update_data(sync_offset_ms=offset_ms)
    data = await state.get_data()
    await state.set_state(BotStates.waiting_custom_output_name)
    await message.answer(
        f"✅ شیفت زمانی `{offset_ms} ms` ثبت شد.\n\n"
        f"{_format_current_filenames_prompt(data)}",
        parse_mode="Markdown"
    )


@router.message(BotStates.waiting_custom_output_name)
async def handle_custom_output_name(message: Message, state: FSMContext):
    await state.update_data(custom_output_name=message.text.strip())
    data = await state.get_data()

    preselected_mode = data.get("operation_mode")
    if preselected_mode:
        if preselected_mode in ("hardsub", "both"):
            await state.set_state(BotStates.waiting_preset_choice)
            return await message.answer(
                "🔥 لطفاً **پریست HandBrake** را از لیست زیر برای هاردساب انتخاب کنید:",
                reply_markup=presets_kb(),
                parse_mode="Markdown"
            )
        else:
            return await _schedule_subtitle_jobs_from_msg(message, state, chosen_preset=None)

    await state.set_state(BotStates.waiting_operation_mode)
    brand = cfg.get("branding", "global_title_and_track_name", ".::EmpireBestTv.Com::.")
    await message.answer(
        f"⚙️ تمام Track Name ها و File Title روی `{brand}` تنظیم شدند و زیرنویس فارسی روی **Default + Forced** قرار گرفت.\n"
        "حالا نوع عملیات را انتخاب کنید:",
        reply_markup=subtitle_mode_kb(),
        parse_mode="Markdown"
    )


@router.callback_query(BotStates.waiting_operation_mode, F.data.startswith("submode:"))
async def cb_select_operation_mode(call: CallbackQuery, state: FSMContext):
    mode = call.data.split(":")[1]  # softsub | hardsub | both
    await state.update_data(operation_mode=mode)

    if mode in ("hardsub", "both"):
        await state.set_state(BotStates.waiting_preset_choice)
        return await call.message.edit_text(
            "🔥 لطفاً **پریست HandBrake** را از لیست زیر برای هاردساب انتخاب کنید:",
            reply_markup=presets_kb(),
            parse_mode="Markdown"
        )

    await _schedule_subtitle_jobs(call, state, chosen_preset=None)


@router.callback_query(BotStates.waiting_preset_choice, F.data.startswith("preset:"))
async def cb_select_preset_for_jobs(call: CallbackQuery, state: FSMContext):
    idx = int(call.data.split(":")[1])
    presets = HandBrakeService.list_available_presets()
    chosen_preset = presets[idx]
    await _schedule_subtitle_jobs(call, state, chosen_preset=chosen_preset)


async def _build_and_enqueue_jobs(data: dict, chosen_preset: dict | None) -> list[str]:
    target_videos = [Path(p) for p in data["target_videos"]]
    fa_sub = Path(data["fa_sub_path"]) if data.get("fa_sub_path") else None
    en_sub = Path(data["en_sub_path"]) if data.get("en_sub_path") else None
    custom_name = data.get("custom_output_name", "1")
    mode = data.get("operation_mode", "softsub")
    sync_mode = data.get("sync_mode", "none")
    sync_offset_ms = int(data.get("sync_offset_ms", 0))
    site_tag = cfg.get("branding", "site_tag", "EmpireBestTv")
    brand = cfg.get("branding", "global_title_and_track_name", ".::EmpireBestTv.Com::.")

    scheduled_ids = []

    for idx, vid in enumerate(target_videos):
        base_stem = vid.stem if custom_name == "1" else (
            f"{Path(custom_name).stem}_{idx+1}" if len(target_videos) > 1 else Path(custom_name).stem
        )

        if mode == "both":
            folders = DownloaderService.create_movie_folders(base_stem)
            soft_dir = folders["softsub"]
            hard_dir = folders["hardsub"]
        else:
            soft_dir = (vid.parent / "softsub") if (vid.parent / "softsub").exists() else vid.parent
            hard_dir = (vid.parent / "hardsub") if (vid.parent / "hardsub").exists() else vid.parent

        if mode in ("softsub", "both"):
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
            scheduled_ids.append(f"📎 SoftSub: `{t_soft.task_id}`")

        if mode in ("hardsub", "both") and chosen_preset:
            hard_out = hard_dir / f"{base_stem}.HardSub.{site_tag}.mp4"
            primary_sub = fa_sub or en_sub
            secondary_sub = en_sub if (fa_sub and en_sub) else None
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
            scheduled_ids.append(f"🔥 HardSub: `{t_hard.task_id}`")

    return scheduled_ids


async def _schedule_subtitle_jobs(call: CallbackQuery, state: FSMContext, chosen_preset: dict | None):
    data = await state.get_data()
    brand = cfg.get("branding", "global_title_and_track_name", ".::EmpireBestTv.Com::.")
    scheduled_ids = await _build_and_enqueue_jobs(data, chosen_preset)
    await state.clear()
    summary_text = "\n".join(scheduled_ids)
    await call.message.edit_text(
        f"✅ عملیات با تگ `{brand}` در صف قرار گرفت!\n"
        "• سافت‌ساب: بدون تغییر رنگ (فقط فارسی و انگلیسی)\n"
        "• هاردساب: بولد و زرد (`#fbff00`)\n\n"
        f"{summary_text}",
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
        f"✅ عملیات با تگ `{brand}` در صف قرار گرفت!\n"
        "• سافت‌ساب: بدون تغییر رنگ (فقط فارسی و انگلیسی)\n"
        "• هاردساب: بولد و زرد (`#fbff00`)\n\n"
        f"{summary_text}",
        reply_markup=main_menu_kb(),
        parse_mode="Markdown"
    )


# ==================== ۵. فایل‌منیجر سرور و اکشن‌های تک‌فایلی ====================
async def render_directory(call: CallbackQuery, state: FSMContext, current_dir: Path):
    root_dir = Path(cfg.get("paths", "workspace_dir")).resolve()
    current_dir = current_dir.resolve()
    current_dir.mkdir(parents=True, exist_ok=True)
    items = sorted(list(current_dir.iterdir()), key=lambda p: (not p.is_dir(), p.name.lower()))
    await state.update_data(
        current_dir=str(current_dir),
        dir_items=[str(p) for p in items]
    )
    is_root = (current_dir == root_dir)
    await call.message.edit_text(
        f"📂 **مسیر فعلی:**\n`{current_dir}`\n\nروی پوشه یا فایل ویدئویی کلیک کنید:",
        reply_markup=file_browser_kb(items, is_root),
        parse_mode="Markdown"
    )


@router.callback_query(F.data == "menu:file_mgr")
async def cb_file_mgr(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    start_dir = Path(data.get("current_dir", cfg.get("paths", "workspace_dir")))
    if not start_dir.exists():
        start_dir = Path(cfg.get("paths", "workspace_dir"))
    await render_directory(call, state, start_dir)


@router.callback_query(F.data == "fnav:up")
async def cb_file_up(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    curr = Path(data.get("current_dir", cfg.get("paths", "workspace_dir")))
    await render_directory(call, state, curr.parent)


@router.callback_query(F.data.startswith("fitem:"))
async def cb_file_item(call: CallbackQuery, state: FSMContext):
    idx = int(call.data.split(":")[1])
    data = await state.get_data()
    selected_path = Path(data["dir_items"][idx])

    if selected_path.is_dir():
        return await render_directory(call, state, selected_path)

    await state.update_data(selected_video=str(selected_path), target_videos=[str(selected_path)])
    size_mb = selected_path.stat().st_size / (1024 * 1024)
    await call.message.edit_text(
        f"🎬 **فایل انتخاب شده:**\n`{selected_path.name}`\n📦 حجم: `{size_mb:.1f} MB`\n\nچه عملیاتی انجام شود؟",
        reply_markup=video_actions_kb(),
        parse_mode="Markdown"
    )


@router.callback_query(F.data == "vact:extract_sub")
async def cb_extract_sub_list(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    video_path = Path(data["selected_video"])
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
    video_path = Path(data["selected_video"])
    out_srt = video_path.with_name(f"{video_path.stem}_track{track_id}.srt")

    await call.message.edit_text("⏳ در حال استخراج زیرنویس با `mkvextract`...", parse_mode="Markdown")
    await MKVService.extract_subtitle_track(video_path, track_id, out_srt)
    await call.message.answer_document(
        FSInputFile(out_srt),
        caption=f"✅ زیرنویس استخراج شد:\n`{out_srt.name}`",
        parse_mode="Markdown"
    )


@router.callback_query(F.data.in_({"vact:softsub", "vact:hardsub", "vact:full_auto"}))
async def cb_single_video_sub_flow(call: CallbackQuery, state: FSMContext):
    mode_map = {
        "vact:softsub": "softsub",
        "vact:hardsub": "hardsub",
        "vact:full_auto": "both"
    }
    data = await state.get_data()
    vid = data["selected_video"]
    await state.update_data(
        target_videos=[vid],
        operation_mode=mode_map[call.data],
        fa_sub_path=None,
        en_sub_path=None
    )
    await state.set_state(BotStates.waiting_fa_subtitle)
    await call.message.edit_text(
        f"🎬 فایل انتخاب شده:\n`{Path(vid).name}`\n\n"
        "🇮🇷 **مرحله ۱ زیرنویس:** لطفاً **فایل زیرنویس فارسی (`.srt` یا `.ass`)** را بفرستید (یا اگر ندارید روی رد شدن کلیک کنید):",
        reply_markup=skip_or_cancel_kb("skip:fa_sub"),
        parse_mode="Markdown"
    )


# ==================== آپلود با CuteFTP 9 و دریافت لینک دانلود ====================
@router.callback_query(F.data.in_({"fact:upload_current_dir", "vact:upload_file"}))
async def cb_cuteftp_upload(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    if call.data == "vact:upload_file":
        target = Path(data["selected_video"])
    else:
        target = Path(data.get("current_dir", cfg.get("paths", "workspace_dir")))

    task = await queue_engine.add_task(
        "upload",
        f"CuteFTP 9: {target.name}",
        CuteFTPService.upload_path,
        target
    )
    await call.message.edit_text(
        f"☁️ ارسال `{target.name}` به صف **CuteFTP 9 Pro** انجام شد!\n"
        f"🆔 کد تسک: `{task.task_id}`\n"
        f"از بخش «وضعیت صف‌ها» لینک‌های دانلود نهایی را دریافت کنید.",
        reply_markup=main_menu_kb(),
        parse_mode="Markdown"
    )


# ==================== نمایش لحظه‌ای وضعیت صف‌ها ====================
@router.callback_query(F.data == "menu:queues")
async def cb_show_queues(call: CallbackQuery):
    tasks = queue_engine.get_queue_summary()
    if not tasks:
        return await call.answer("📭 در حال حاضر هیچ تسکی در صف نیست.", show_alert=True)

    lines = ["📊 **وضعیت صف‌های پردازش (۲ ورکر موازی HandBrake / MKV / qbt / CuteFTP):**\n"]
    for t in reversed(tasks):
        line = f"🔹 `{t.task_id}` | **[{t.queue_type.upper()}]** {t.title}\n   └ وضعیت: {t.status} ({t.progress})"
        if t.error:
            line += f"\n   └ ⚠️ خطا: `{t.error[:80]}`"
        if t.result and isinstance(t.result, list):
            links_str = "\n".join([f"   🔗 {u}" for u in t.result[:6]])
            line += f"\n{links_str}"
        lines.append(line)

    kb = InlineKeyboardBuilder()
    kb.button(text="🔄 بروزرسانی درصد پیشرفت", callback_data="menu:queues")
    kb.button(text="🔙 منوی اصلی", callback_data="menu:home")
    kb.adjust(1)

    await call.message.edit_text("\n\n".join(lines), reply_markup=kb.as_markup(), parse_mode="Markdown")


# ==================== تشخیص هوشمند ارسال مستقیم لینک یا فایل تورنت در چت ====================
@router.message(F.document)
async def catch_direct_torrent_file(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    if message.document.file_name and message.document.file_name.lower().endswith(".torrent"):
        temp_dir = Path(cfg.get("paths", "workspace_dir")) / "_incoming_torrents"
        temp_dir.mkdir(parents=True, exist_ok=True)
        t_path = temp_dir / message.document.file_name
        await message.bot.download(message.document, destination=t_path)

        await state.update_data(pending_direct_target=str(t_path))
        await state.set_state(BotStates.waiting_direct_folder_name)
        await message.answer(
            "🧲 فایل `.torrent` دریافت شد!\n📁 لطفاً **نام پوشه فیلم یا سریال** را وارد کنید تا با `qbt` دانلود شود:",
            parse_mode="Markdown"
        )


@router.message(F.text.regexp(r"^(magnet:\?xt=|https?://)"))
async def catch_direct_links_in_chat(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    await state.update_data(pending_direct_target=message.text.strip())
    await state.set_state(BotStates.waiting_direct_folder_name)
    await message.answer(
        "🔗 لینک دریافت شد!\n📁 لطفاً **نام پوشه فیلم یا سریال** را وارد کنید تا دانلود آغاز شود:",
        parse_mode="Markdown"
    )