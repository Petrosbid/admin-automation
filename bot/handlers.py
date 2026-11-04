import re
import shutil
from pathlib import Path
from aiogram import Router, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.utils.keyboard import InlineKeyboardBuilder

from core.config_loader import cfg
from core.queue_manager import queue_engine
from services.downloader_service import DownloaderService
from services.subtitle_service import SubtitleService
from services.cuteftp_service import CuteFTPService
from services.monitor_service import monitor_service
from bot.keyboards import (
    main_menu_kb, monitor_menu_kb, folder_structure_choice_kb,
    target_folders_kb, file_browser_kb, video_actions_kb,
    cuteftp_destination_kb
)

router = Router()

SECTION_LABELS = {
    "branding": "🏷 تگ سایت و نام ترک‌ها (Branding)",
    "subtitle_style": "🎨 استایل، فونت و رنگ زیرنویس",
    "subtitle_sync": "🔄 تنظیمات سینک خودکار زیرنویس",
    "concurrency": "⚡ تعداد پردازش همزمان (HandBrake / MKV)",
    "cuteftp": "☁️ مشخصات سرور دانلود و CuteFTP",
    "qbittorrent": "🧲 تنظیمات qBittorrent WebUI",
    "proxy": "🌐 تنظیمات پروکسی (SOCKS5 / HTTP)",
    "monitor": "📡 تنظیمات مانیتورینگ ۵ دقیقه‌ای سایت‌ها",
    "paths": "📂 مسیر فایل‌های اجرایی و پوشه‌ها",
}


class MainStates(StatesGroup):
    waiting_new_folder_name = State()
    waiting_new_folder_type = State()
    waiting_target_folder_sub_file = State()
    waiting_custom_cuteftp_path = State()
    waiting_new_fav_cuteftp_path = State()
    waiting_rename_file = State()
    waiting_rename_dir = State()
    waiting_new_config_value = State()


def is_admin(user_id: int) -> bool:
    return user_id in cfg.get("telegram", "admin_ids", [])


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
        "⚙️ برای تغییر مستقیم تنظیمات از دستور `/settings` استفاده کنید.",
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


# ==================== بخش تنظیمات تعاملی (/settings) ====================
def _sections_kb() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for sec_key, label in SECTION_LABELS.items():
        if sec_key in cfg.data:
            builder.row(InlineKeyboardButton(text=label, callback_data=f"cfgsec:{sec_key}"))
    builder.row(InlineKeyboardButton(text="🔙 بازگشت به منوی اصلی", callback_data="menu:home"))
    return builder.as_markup()


def _section_keys_kb(section: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    sec_data = cfg.get(section) or {}
    if isinstance(sec_data, dict):
        for k, v in sec_data.items():
            if isinstance(v, (dict, list)):
                continue
            val_preview = "🟢 True" if v is True else ("🔴 False" if v is False else str(v)[:28])
            builder.row(
                InlineKeyboardButton(
                    text=f"✏️ {k}: {val_preview}",
                    callback_data=f"cfgkey:{section}:{k}"
                )
            )
    builder.row(InlineKeyboardButton(text="🔙 بازگشت به دسته‌بندی تنظیمات", callback_data="menu:settings"))
    return builder.as_markup()


def _parse_typed_value(raw_text: str, old_value):
    clean = raw_text.strip()
    if isinstance(old_value, bool):
        return clean.lower() in ("true", "1", "yes", "on", "فعال")
    if isinstance(old_value, int):
        return int(clean)
    if isinstance(old_value, float):
        return float(clean)
    return clean


@router.message(Command("settings"))
async def cmd_settings(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return await message.answer("⛔ شما دسترسی ادمین ندارید.")
    await state.clear()
    cfg.reload()
    await message.answer(
        "⚙️ **پنل مدیریت مستقیم تنظیمات (`config.yaml`)**\n\n"
        "هر تغییری که در این بخش بدهید، بلافاصله در فایل `config.yaml` ذخیره و اعمال می‌شود.\n"
        "بخش مورد نظر را انتخاب کنید:",
        reply_markup=_sections_kb(),
        parse_mode="Markdown"
    )


@router.callback_query(F.data == "menu:settings")
async def cb_settings_home(call: CallbackQuery, state: FSMContext):
    await state.clear()
    cfg.reload()
    await call.message.edit_text(
        "⚙️ **پنل مدیریت مستقیم تنظیمات (`config.yaml`)**\n\n"
        "بخش مورد نظر برای مشاهده یا ویرایش را انتخاب کنید:",
        reply_markup=_sections_kb(),
        parse_mode="Markdown"
    )


@router.callback_query(F.data.startswith("cfgsec:"))
async def cb_open_config_section(call: CallbackQuery, state: FSMContext):
    section = call.data.split(":", 1)[1]
    sec_data = cfg.get(section) or {}
    label = SECTION_LABELS.get(section, section)

    lines = [f"⚙️ **بخش: {label}**\n"]
    if isinstance(sec_data, dict):
        for k, v in sec_data.items():
            if isinstance(v, (dict, list)):
                continue
            lines.append(f"• `{k}` = `{v}`")

    lines.append("\n👇 برای تغییر هر مورد، روی دکمه آن کلیک کنید:")
    await call.message.edit_text(
        "\n".join(lines),
        reply_markup=_section_keys_kb(section),
        parse_mode="Markdown"
    )


@router.callback_query(F.data.startswith("cfgkey:"))
async def cb_edit_config_key(call: CallbackQuery, state: FSMContext):
    _, section, key = call.data.split(":", 2)
    current_val = cfg.get(section, key)

    if isinstance(current_val, bool):
        new_val = not current_val
        cfg.set_value(section, key, new_val)
        await call.answer(f"✅ مقدار {key} به {new_val} تغییر یافت!")
        return await cb_open_config_section(call, state)

    await state.update_data(cfg_section=section, cfg_key=key)
    await state.set_state(MainStates.waiting_new_config_value)

    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text="🔙 انصراف و بازگشت", callback_data=f"cfgsec:{section}"))

    await call.message.edit_text(
        f"✏️ **ویرایش تنظیمات `{section}.{key}`**\n\n"
        f"🔹 مقدار فعلی (برای کپی لمس کنید):\n`{current_val}`\n\n"
        "لطفاً **مقدار جدید** را در چت ارسال کنید:",
        reply_markup=kb.as_markup(),
        parse_mode="Markdown"
    )


@router.message(MainStates.waiting_new_config_value)
async def handle_new_config_value(message: Message, state: FSMContext):
    data = await state.get_data()
    section = data["cfg_section"]
    key = data["cfg_key"]
    old_val = cfg.get(section, key)

    try:
        new_val = _parse_typed_value(message.text or "", old_val)
    except ValueError:
        return await message.answer(
            f"⚠️ فرمت مقدار وارد شده صحیح نیست! مقدار قبلی (`{old_val}`) عددی بوده است. لطفاً یک عدد معتبر بفرستید:",
            parse_mode="Markdown"
        )

    cfg.set_value(section, key, new_val)
    await state.clear()

    await message.answer(
        f"✅ مقدار `{section}.{key}` با موفقیت به:\n`{new_val}`\nتغییر یافت و در `config.yaml` ذخیره شد!",
        reply_markup=_section_keys_kb(section),
        parse_mode="Markdown"
    )


# ==================== بخش مانیتورینگ ۵ دقیقه‌ای سایت‌ها و حل کپچا ====================
@router.callback_query(F.data == "menu:monitor")
async def cb_monitor_menu(call: CallbackQuery):
    enabled = bool(cfg.get("monitor", "enabled", True))
    interval = cfg.get("monitor", "interval_seconds", 300)
    await call.message.edit_text(
        "📡 **پنل مانیتورینگ خودکار ۵ سایت**\n\n"
        "🌐 سایت‌های تحت رصد:\n"
        "1️⃣ `https://pahe.ink/`\n"
        "2️⃣ `https://psa.wf/`\n"
        "3️⃣ `https://yts.gg/home`\n"
        "4️⃣ `https://digimoviez53.top/` (شامل باکس سریال‌های جدید)\n"
        "5️⃣ `https://avasaraw.top/`\n\n"
        f"⏱ بازه رفرش خودکار: هر `{interval // 60}` دقیقه یک‌بار\n"
        f"📊 وضعیت فعلی: {'🟢 فعال' if enabled else '🔴 غیرفعال'}",
        reply_markup=monitor_menu_kb(enabled),
        parse_mode="Markdown"
    )


@router.callback_query(F.data == "mon:toggle")
async def cb_monitor_toggle(call: CallbackQuery):
    current = bool(cfg.get("monitor", "enabled", True))
    cfg.set_value("monitor", "enabled", not current)
    await call.answer(f"وضعیت مانیتورینگ به {'فعال' if not current else 'غیرفعال'} تغییر یافت.")
    await cb_monitor_menu(call)


@router.callback_query(F.data == "mon:open_captcha")
async def cb_monitor_open_captcha(call: CallbackQuery):
    await call.message.edit_text(
        "⏳ در حال باز کردن پنجره مرورگر روی دسکتاپ ویندوز سرور برای هر ۵ سایت...",
        parse_mode="Markdown"
    )
    msg = await monitor_service.open_captcha_tabs_for_admin()
    enabled = bool(cfg.get("monitor", "enabled", True))
    await call.message.edit_text(msg, reply_markup=monitor_menu_kb(enabled), parse_mode="Markdown")


@router.callback_query(F.data == "mon:check_now")
async def cb_monitor_check_now(call: CallbackQuery):
    await call.message.edit_text("⏳ در حال رفرش و بررسی فوری هر ۵ سایت...", parse_mode="Markdown")
    counts = await monitor_service.check_all_sites_once(call.bot, notify_on_first_seed=True)
    total_new = sum(counts.values())
    enabled = bool(cfg.get("monitor", "enabled", True))
    await call.message.edit_text(
        f"✅ **بررسی فوری انجام شد!**\n\n"
        f"🔹 Pahe.ink: `{counts.get('pahe', 0)}`\n"
        f"🔹 PSA.wf: `{counts.get('psa', 0)}`\n"
        f"🔹 YTS.gg: `{counts.get('yts', 0)}`\n"
        f"🔹 Digimoviez: `{counts.get('digimovie', 0)}`\n"
        f"🔹 AvaMovie: `{counts.get('avamovie', 0)}`\n\n"
        f"مجموع پست‌های جدید ارسال شده: `{total_new}`",
        reply_markup=monitor_menu_kb(enabled),
        parse_mode="Markdown"
    )


@router.callback_query(F.data.startswith("mon:latest:"))
async def cb_monitor_latest_site(call: CallbackQuery):
    site_key = call.data.split(":")[2]
    await call.message.edit_text(f"⏳ در حال دریافت آخرین پست‌های `{site_key.upper()}`...", parse_mode="Markdown")
    try:
        posts = await monitor_service.fetch_site_posts(site_key, bot=call.bot)
        for p in posts[:4]:
            await monitor_service.send_post_notification(call.bot, p)

        enabled = bool(cfg.get("monitor", "enabled", True))
        await call.message.edit_text(
            f"✅ آخرین پست‌های سایت `{site_key.upper()}` در چت ارسال شد.",
            reply_markup=monitor_menu_kb(enabled),
            parse_mode="Markdown"
        )
    except Exception as e:
        await monitor_service.send_error_notification(call.bot, site_key, str(e))
        await call.message.edit_text(f"❌ خطا در ارتباط با سایت:\n`{e}`", reply_markup=monitor_menu_kb(True), parse_mode="Markdown")


# ==================== ساخت پوشه جدید در Workspace بدون دادن لینک ====================
@router.callback_query(F.data == "menu:create_folder")
async def cb_create_folder_menu(call: CallbackQuery, state: FSMContext):
    workspace = Path(cfg.get("paths", "workspace_dir")).resolve()
    await state.update_data(mkdir_parent=str(workspace))
    await state.set_state(MainStates.waiting_new_folder_name)
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
    await state.set_state(MainStates.waiting_new_folder_name)
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


@router.message(MainStates.waiting_new_folder_name)
async def handle_new_folder_name(message: Message, state: FSMContext):
    raw_name = (message.text or "").strip()
    safe_name = re.sub(r'[<>:"/\\|?*]', "", raw_name).strip().replace(" ", "_")
    if not safe_name:
        return await message.answer("⚠️ نام پوشه معتبر نیست. دوباره ارسال کنید:")

    await state.update_data(new_folder_name=safe_name)
    await state.set_state(MainStates.waiting_new_folder_type)
    await message.answer(
        f"📁 نام پوشه: `{safe_name}`\n"
        "نوع ساختار پوشه را انتخاب کنید:",
        reply_markup=folder_structure_choice_kb(),
        parse_mode="Markdown"
    )


@router.callback_query(MainStates.waiting_new_folder_type, F.data.startswith("mktype:"))
async def cb_create_folder_finalize(call: CallbackQuery, state: FSMContext):
    mk_type = call.data.split(":")[1]
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


# ==================== آپلود زیرنویس در پوشه هدف ====================
@router.callback_query(F.data == "menu:upload_sub_target")
async def cb_upload_sub_select_folder(call: CallbackQuery, state: FSMContext):
    workspace = Path(cfg.get("paths", "workspace_dir")).resolve()
    folders = DownloaderService.list_workspace_folders()

    if not folders:
        return await call.answer("⚠️ هنوز هیچ پوشه‌ای در Workspace وجود ندارد! ابتدا یک پوشه بسازید.", show_alert=True)

    await state.update_data(available_target_folders=[str(f) for f in folders])
    full_list_text = "\n".join([f"`[{i+1}]` 📁 `{f.name}`" for i, f in enumerate(folders[:25])])
    await call.message.edit_text(
        "📥 **آپلود زیرنویس در پوشه هدف**\n\n"
        "📋 **نام کامل پوشه‌ها:**\n"
        f"{full_list_text}\n\n"
        "پوشه مورد نظر را بر اساس شماره انتخاب کنید:",
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
    await state.set_state(MainStates.waiting_target_folder_sub_file)
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
    await state.set_state(MainStates.waiting_target_folder_sub_file)
    await call.message.edit_text(
        f"📂 مسیر ذخیره زیرنویس:\n`{dest_dir}`\n\n"
        "📎 لطفاً **فایل زیرنویس (`.srt` یا `.ass`)** را ارسال کنید تا در این پوشه ذخیره شود:",
        parse_mode="Markdown"
    )


@router.message(MainStates.waiting_target_folder_sub_file, F.document)
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


# ==================== ۵. فایل‌منیجر سرور + حذف و تغییر نام فایل و پوشه ====================
async def render_directory(call_or_msg: CallbackQuery | Message, state: FSMContext, current_dir: Path):
    root_dir = Path(cfg.get("paths", "workspace_dir")).resolve()
    current_dir = current_dir.resolve()
    current_dir.mkdir(parents=True, exist_ok=True)
    items = sorted(
        [p for p in current_dir.iterdir() if p.name != "browser_profile" and not p.name.startswith("_")],
        key=lambda p: (not p.is_dir(), p.name.lower())
    )
    await state.update_data(
        current_dir=str(current_dir),
        dir_items=[str(p) for p in items]
    )
    is_root = (current_dir == root_dir)

    item_lines = []
    for idx, p in enumerate(items[:25]):
        if p.is_dir():
            item_lines.append(f"`[{idx+1}]` 📁 `{p.name}`")
        else:
            size_mb = p.stat().st_size / (1024 * 1024)
            icon = "🎬" if p.suffix.lower() in [".mkv", ".mp4", ".avi", ".ts"] else "📄"
            item_lines.append(f"`[{idx+1}]` {icon} `{p.name}` _({size_mb:.1f} MB)_")

    listing_block = "\n".join(item_lines) if item_lines else "_(این پوشه خالی است)_"

    msg_text = (
        f"📂 **مسیر فعلی:**\n`{current_dir}`\n\n"
        f"📋 **لیست کامل محتویات این پوشه:**\n{listing_block}\n\n"
        "روی شماره آیتم مورد نظر در دکمه‌های زیر کلیک کنید:"
    )

    kb = file_browser_kb(items, is_root)
    if isinstance(call_or_msg, CallbackQuery):
        await call_or_msg.message.edit_text(msg_text[:4000], reply_markup=kb, parse_mode="Markdown")
    else:
        await call_or_msg.answer(msg_text[:4000], reply_markup=kb, parse_mode="Markdown")


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
    is_vid = selected_path.suffix.lower() in DownloaderService.VIDEO_EXTENSIONS
    await call.message.edit_text(
        f"📄 **فایل انتخاب شده (نام کامل):**\n`{selected_path.name}`\n\n"
        f"📂 پوشه:\n`{selected_path.parent}`\n"
        f"📦 حجم: `{size_mb:.2f} MB`\n\nچه عملیاتی انجام شود؟",
        reply_markup=video_actions_kb(is_video=is_vid),
        parse_mode="Markdown"
    )


# ==================== تغییر نام و حذف پوشه در فایل‌منیجر ====================
@router.callback_query(F.data == "fact:rename_dir")
async def cb_rename_dir_start(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    curr_dir = Path(data.get("current_dir", cfg.get("paths", "workspace_dir"))).resolve()
    root_dir = Path(cfg.get("paths", "workspace_dir")).resolve()
    if curr_dir == root_dir:
        return await call.answer("⛔ پوشه اصلی Workspace قابل تغییر نام نیست!", show_alert=True)

    await state.set_state(MainStates.waiting_rename_dir)
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text="🔙 انصراف", callback_data="menu:file_mgr"))
    await call.message.edit_text(
        f"✏️ **تغییر نام پوشه**\n\n"
        f"📁 نام فعلی پوشه (برای کپی لمس کنید):\n`{curr_dir.name}`\n\n"
        "لطفاً **نام جدید پوشه** را ارسال کنید:",
        reply_markup=kb.as_markup(),
        parse_mode="Markdown"
    )


@router.message(MainStates.waiting_rename_dir)
async def handle_rename_dir_submit(message: Message, state: FSMContext):
    new_name = re.sub(r'[<>:"/\\|?*]', "", (message.text or "").strip()).strip()
    if not new_name:
        return await message.answer("⚠️ نام وارد شده معتبر نیست. دوباره بفرستید:")

    data = await state.get_data()
    curr_dir = Path(data["current_dir"]).resolve()
    new_dir = curr_dir.parent / new_name

    try:
        curr_dir.rename(new_dir)
    except Exception as e:
        return await message.answer(f"❌ خطا در تغییر نام پوشه:\n`{e}`", parse_mode="Markdown")

    await state.set_state(None)
    await state.update_data(current_dir=str(new_dir))
    await message.answer(f"✅ نام پوشه با موفقیت به `{new_dir.name}` تغییر یافت!", parse_mode="Markdown")
    await render_directory(message, state, new_dir)


@router.callback_query(F.data == "fact:delete_dir")
async def cb_delete_dir_ask(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    curr_dir = Path(data.get("current_dir", cfg.get("paths", "workspace_dir"))).resolve()
    root_dir = Path(cfg.get("paths", "workspace_dir")).resolve()
    if curr_dir == root_dir:
        return await call.answer("⛔ پوشه اصلی Workspace قابل حذف نیست!", show_alert=True)

    kb = InlineKeyboardBuilder()
    kb.row(
        InlineKeyboardButton(text="✅ بله، کل این پوشه حذف شود", callback_data="fact:confirm_del_dir"),
        InlineKeyboardButton(text="❌ انصراف", callback_data="menu:file_mgr"),
    )
    await call.message.edit_text(
        f"⚠️ **آیا از حذف کامل پوشه زیر و تمام محتویات داخل آن اطمینان دارید؟**\n\n"
        f"📁 `{curr_dir}`",
        reply_markup=kb.as_markup(),
        parse_mode="Markdown"
    )


@router.callback_query(F.data == "fact:confirm_del_dir")
async def cb_delete_dir_confirm(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    curr_dir = Path(data["current_dir"]).resolve()
    parent_dir = curr_dir.parent
    dir_name = curr_dir.name

    try:
        shutil.rmtree(curr_dir)
    except Exception as e:
        return await call.answer(f"❌ خطا در حذف پوشه: {e}", show_alert=True)

    await state.update_data(current_dir=str(parent_dir))
    await call.answer(f"🗑 پوشه {dir_name} به طور کامل حذف شد!", show_alert=True)
    await render_directory(call, state, parent_dir)


# ==================== تغییر نام و حذف فایل در فایل‌منیجر ====================
@router.callback_query(F.data == "vact:rename_file")
async def cb_rename_file_start(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    file_path = Path(data["selected_video"]).resolve()

    await state.set_state(MainStates.waiting_rename_file)
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text="🔙 انصراف", callback_data="menu:file_mgr"))
    await call.message.edit_text(
        f"✏️ **تغییر نام فایل**\n\n"
        f"📄 نام فعلی کامل (برای کپی لمس کنید):\n`{file_path.name}`\n"
        f"🔹 نام بدون پسوند:\n`{file_path.stem}`\n\n"
        "لطفاً **نام جدید فایل** را ارسال کنید (اگر پسوند را ننویسید، پسوند فعلی به صورت خودکار حفظ می‌شود):",
        reply_markup=kb.as_markup(),
        parse_mode="Markdown"
    )


@router.message(MainStates.waiting_rename_file)
async def handle_rename_file_submit(message: Message, state: FSMContext):
    raw_new = re.sub(r'[<>:"/\\|?*]', "", (message.text or "").strip()).strip()
    if not raw_new:
        return await message.answer("⚠️ نام وارد شده معتبر نیست. دوباره بفرستید:")

    data = await state.get_data()
    old_path = Path(data["selected_video"]).resolve()
    orig_ext = old_path.suffix

    if orig_ext and not raw_new.lower().endswith(orig_ext.lower()):
        raw_new += orig_ext

    new_path = old_path.with_name(raw_new)
    try:
        old_path.rename(new_path)
    except Exception as e:
        return await message.answer(f"❌ خطا در تغییر نام فایل:\n`{e}`", parse_mode="Markdown")

    await state.set_state(None)
    await state.update_data(selected_video=str(new_path), target_videos=[str(new_path)])
    is_vid = new_path.suffix.lower() in DownloaderService.VIDEO_EXTENSIONS
    size_mb = new_path.stat().st_size / (1024 * 1024)

    await message.answer(
        f"✅ نام فایل با موفقیت تغییر یافت!\n\n"
        f"📄 نام جدید:\n`{new_path.name}`\n"
        f"📦 حجم: `{size_mb:.2f} MB`",
        reply_markup=video_actions_kb(is_video=is_vid),
        parse_mode="Markdown"
    )


@router.callback_query(F.data == "vact:delete_file")
async def cb_delete_file_ask(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    file_path = Path(data["selected_video"]).resolve()

    kb = InlineKeyboardBuilder()
    kb.row(
        InlineKeyboardButton(text="✅ بله، فایل حذف شود", callback_data="vact:confirm_del_file"),
        InlineKeyboardButton(text="❌ انصراف", callback_data="menu:file_mgr"),
    )
    await call.message.edit_text(
        f"⚠️ **آیا از حذف دائمی این فایل اطمینان دارید؟**\n\n"
        f"📄 `{file_path.name}`",
        reply_markup=kb.as_markup(),
        parse_mode="Markdown"
    )


@router.callback_query(F.data == "vact:confirm_del_file")
async def cb_delete_file_confirm(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    file_path = Path(data["selected_video"]).resolve()
    parent_dir = file_path.parent
    fname = file_path.name

    try:
        file_path.unlink(missing_ok=True)
    except Exception as e:
        return await call.answer(f"❌ خطا در حذف فایل: {e}", show_alert=True)

    await call.answer(f"🗑 فایل {fname[:30]} حذف شد!", show_alert=True)
    await render_directory(call, state, parent_dir)


# ==================== انتخاب مسیر مقصد در هاست دانلود برای CuteFTP 9 ====================
@router.callback_query(F.data.in_({"fact:upload_current_dir", "vact:upload_file"}))
async def cb_cuteftp_choose_destination(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    if call.data == "vact:upload_file":
        target = Path(data["selected_video"])
    else:
        target = Path(data.get("current_dir", cfg.get("paths", "workspace_dir")))

    await state.update_data(cuteftp_upload_target=str(target.resolve()))
    active_gui_path = await CuteFTPService.get_active_gui_remote_path()

    await call.message.edit_text(
        f"☁️ **آپلود به هاست دانلود (CuteFTP 9)**\n\n"
        f"📦 آیتم انتخاب شده برای آپلود:\n`{target.name}`\n"
        f"📍 مسیر فعلی باز در برنامه CuteFTP:\n`{active_gui_path}`\n\n"
        "در کدام پوشه از هاست دانلود آپلود شود؟",
        reply_markup=cuteftp_destination_kb(active_gui_path),
        parse_mode="Markdown"
    )


@router.callback_query(F.data == "cftp_dest:current")
async def cb_cuteftp_upload_to_current(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    target = Path(data["cuteftp_upload_target"])
    task = await queue_engine.add_task(
        "upload",
        f"CuteFTP ({target.name})",
        CuteFTPService.upload_path,
        target,
        "__CURRENT__"
    )
    await state.clear()
    await call.message.edit_text(
        f"☁️ ارسال `{target.name}` به **مسیر فعلی CuteFTP** در صف قرار گرفت!\n"
        f"🆔 کد تسک: `{task.task_id}`",
        reply_markup=main_menu_kb(),
        parse_mode="Markdown"
    )


@router.callback_query(F.data.startswith("cftp_dest:preset:"))
async def cb_cuteftp_upload_to_preset(call: CallbackQuery, state: FSMContext):
    idx = int(call.data.split(":")[2])
    destinations = CuteFTPService.get_saved_destinations()
    chosen_dest = destinations[idx]["path"]

    data = await state.get_data()
    target = Path(data["cuteftp_upload_target"])
    task = await queue_engine.add_task(
        "upload",
        f"CuteFTP [{chosen_dest}]: {target.name}",
        CuteFTPService.upload_path,
        target,
        chosen_dest
    )
    await state.clear()
    await call.message.edit_text(
        f"☁️ ارسال `{target.name}` به مسیر `{chosen_dest}` آغاز شد!\n"
        f"🆔 کد تسک: `{task.task_id}`",
        reply_markup=main_menu_kb(),
        parse_mode="Markdown"
    )


@router.callback_query(F.data == "cftp_dest:custom")
async def cb_cuteftp_ask_custom_path(call: CallbackQuery, state: FSMContext):
    await state.set_state(MainStates.waiting_custom_cuteftp_path)
    await call.message.edit_text(
        "✏️ **تایپ مسیر مقصد در هاست دانلود:**\n\n"
        "مسیر پوشه مورد نظر در هاست را وارد کنید (مثلاً `/English/Series/Severance/S02` یا `/English/Movies`):",
        parse_mode="Markdown"
    )


@router.message(MainStates.waiting_custom_cuteftp_path)
async def handle_custom_cuteftp_path(message: Message, state: FSMContext):
    remote_path = (message.text or "").strip()
    if not remote_path:
        return await message.answer("⚠️ لطفاً یک مسیر معتبر وارد کنید:")

    data = await state.get_data()
    target = Path(data["cuteftp_upload_target"])
    task = await queue_engine.add_task(
        "upload",
        f"CuteFTP [{remote_path}]: {target.name}",
        CuteFTPService.upload_path,
        target,
        remote_path
    )
    await state.clear()
    await message.answer(
        f"☁️ ارسال `{target.name}` به مسیر `{remote_path}` آغاز شد!\n"
        f"🆔 کد تسک: `{task.task_id}`",
        reply_markup=main_menu_kb(),
        parse_mode="Markdown"
    )


@router.callback_query(F.data == "cftp_dest:add_fav")
async def cb_cuteftp_add_fav_start(call: CallbackQuery, state: FSMContext):
    await state.set_state(MainStates.waiting_new_fav_cuteftp_path)
    await call.message.edit_text(
        "➕ **افزودن مسیر جدید به دکمه‌های ثابت CuteFTP:**\n\n"
        "لطفاً نام و مسیر را با فرمت `نام | مسیر` ارسال کنید.\n"
        "مثال:\n`سریال‌های ۲۰۲۵ | /English/Series/2025`",
        parse_mode="Markdown"
    )


@router.message(MainStates.waiting_new_fav_cuteftp_path)
async def handle_new_fav_cuteftp_path(message: Message, state: FSMContext):
    text = (message.text or "").strip()
    if "|" in text:
        label, path = [x.strip() for x in text.split("|", 1)]
    else:
        label = text
        path = text

    CuteFTPService.add_saved_destination(label, path)
    await state.set_state(None)
    active_gui_path = await CuteFTPService.get_active_gui_remote_path()
    await message.answer(
        f"✅ مسیر `{path}` به لیست دکمه‌های ثابت CuteFTP اضافه شد!\nحالا مقصد آپلود را انتخاب کنید:",
        reply_markup=cuteftp_destination_kb(active_gui_path),
        parse_mode="Markdown"
    )