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
    target_folders_kb, file_browser_kb, cuteftp_destination_kb
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
    waiting_batch_rename_pattern = State()
    waiting_rename_dir = State()
    waiting_new_config_value = State()
    waiting_new_site_url = State()


def is_admin(user_id: int) -> bool:
    return user_id in cfg.get("telegram", "admin_ids", [])


def _natural_sort_key(path_str: str) -> list:
    name = Path(path_str).name.lower()
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", name)]


def _build_renamed_filename(pattern: str, orig_path: Path, index_1based: int, total_count: int) -> str:
    """
    قانون تغییر نام تکی و گروهی فایل‌ها:
    - اگر کاربر در الگو علامت '*' گذاشته باشد -> جایگزینی '*' با 01, 02, 03, ...
    - اگر چند فایل باشد و کاربر '*' نگذارد -> قرار دادن 01, 02, 03 در انتهای نام دقیقاً قبل از پسوند فایل
    - حفظ خودکار پسوند اصلی فایل
    """
    orig_ext = orig_path.suffix  # مثلاً .mkv یا .srt
    clean_pat = re.sub(r'[<>:"/\\|?]', "", pattern).strip()  # حفظ کاراکتر '*'

    if orig_ext and clean_pat.lower().endswith(orig_ext.lower()):
        stem_pat = clean_pat[:-len(orig_ext)]
    else:
        stem_pat = clean_pat

    num_str = f"{index_1based:02d}"

    if "*" in stem_pat:
        final_stem = stem_pat.replace("*", num_str)
    elif total_count > 1:
        final_stem = f"{stem_pat}{num_str}"
    else:
        final_stem = stem_pat

    return f"{final_stem}{orig_ext}"


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
class MainStates(StatesGroup):
    waiting_new_folder_name = State()
    waiting_new_folder_type = State()
    waiting_target_folder_sub_file = State()
    waiting_custom_cuteftp_path = State()
    waiting_new_fav_cuteftp_path = State()
    waiting_batch_rename_pattern = State()
    waiting_rename_dir = State()
    waiting_new_config_value = State()
    waiting_new_site_url = State()


# ==================== بخش تنظیمات تعاملی (/settings) + مدیریت لینک و وضعیت تک‌تک سایت‌ها ====================
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

    # اگر بخش مانیتورینگ انتخاب شده است، دکمه‌های فعال/غیرفعال کردن و تغییر لینک تک‌تک سایت‌ها را اضافه کن
    if section == "monitor":
        sites_cfg = monitor_service.get_all_sites_config()
        for s_key, s_info in sites_cfg.items():
            status_icon = "🟢 فعال" if s_info["enabled"] else "🔴 غیرفعال"
            short_url = s_info["url"].replace("https://", "").replace("http://", "").rstrip("/")
            builder.row(
                InlineKeyboardButton(
                    text=f"{status_icon} | {s_info['title']}",
                    callback_data=f"monsite:toggle:{s_key}"
                ),
                InlineKeyboardButton(
                    text=f"🔗 تغییر لینک ({short_url[:18]})",
                    callback_data=f"monsite:url:{s_key}"
                )
            )
        builder.row(InlineKeyboardButton(text="📡 بازگشت به پنل مانیتورینگ", callback_data="menu:monitor"))

    builder.row(InlineKeyboardButton(text="🔙 بازگشت به دسته‌بندی تنظیمات", callback_data="menu:settings"))
    return builder.as_markup()


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

    if section == "monitor":
        lines.append("\n🌐 **وضعیت و آدرس فعلی تک‌تک سایت‌های مانیتورینگ:**")
        sites_cfg = monitor_service.get_all_sites_config()
        for s_key, s_info in sites_cfg.items():
            st_badge = "🟢 فعال" if s_info["enabled"] else "🔴 غیرفعال"
            lines.append(f"• **{s_info['title']}** ({st_badge}):\n  `{s_info['url']}`")
        lines.append("\n👇 برای **فعال/غیرفعال کردن هر سایت** یا **وارد کردن لینک جدید** (مثلاً آدرس جدید دیجی‌موویز)، روی دکمه مربوطه کلیک کنید:")
    else:
        lines.append("\n👇 برای تغییر هر مورد، روی دکمه آن کلیک کنید:")

    await call.message.edit_text(
        "\n".join(lines),
        reply_markup=_section_keys_kb(section),
        parse_mode="Markdown"
    )


@router.callback_query(F.data.startswith("monsite:toggle:"))
async def cb_toggle_single_monitor_site(call: CallbackQuery, state: FSMContext):
    site_key = call.data.split(":")[2]
    new_state = monitor_service.toggle_site_enabled(site_key)
    status_txt = "فعال 🟢" if new_state else "غیرفعال 🔴"
    await call.answer(f"مانیتورینگ سایت {site_key.upper()} {status_txt} شد!")
    call.data = "cfgsec:monitor"
    await cb_open_config_section(call, state)


@router.callback_query(F.data.startswith("monsite:url:"))
async def cb_change_single_monitor_site_url(call: CallbackQuery, state: FSMContext):
    site_key = call.data.split(":")[2]
    sites_cfg = monitor_service.get_all_sites_config()
    s_info = sites_cfg.get(site_key, {})
    current_url = s_info.get("url", "")
    title = s_info.get("title", site_key.upper())

    await state.update_data(edit_site_key=site_key, edit_site_title=title)
    await state.set_state(MainStates.waiting_new_site_url)

    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text="🔙 انصراف و بازگشت", callback_data="cfgsec:monitor"))

    await call.message.edit_text(
        f"🔗 **تغییر آدرس سایت {title} (`{site_key}`)**\n\n"
        f"🔹 آدرس فعلی (برای کپی لمس کنید):\n`{current_url}`\n\n"
        "✏️ لطفاً **لینک (دامنه) جدید سایت** را ارسال کنید (مثلاً `https://digimoviez54.top/`):",
        reply_markup=kb.as_markup(),
        parse_mode="Markdown"
    )


@router.message(MainStates.waiting_new_site_url)
async def handle_new_site_url_input(message: Message, state: FSMContext):
    new_url = (message.text or "").strip()
    if not new_url or "." not in new_url:
        return await message.answer("⚠️ آدرس وارد شده معتبر نیست. لطفاً لینک کامل سایت را بفرستید:")

    data = await state.get_data()
    site_key = data["edit_site_key"]
    title = data.get("edit_site_title", site_key)

    monitor_service.set_site_url(site_key, new_url)
    saved_url = monitor_service.get_site_url(site_key)
    await state.clear()

    await message.answer(
        f"✅ آدرس سایت **{title}** با موفقیت به:\n`{saved_url}`\nتغییر یافت و در `config.yaml` ذخیره شد!",
        reply_markup=_section_keys_kb("monitor"),
        parse_mode="Markdown"
    )


@router.callback_query(F.data == "menu:monitor")
async def cb_monitor_menu(call: CallbackQuery):
    enabled = bool(cfg.get("monitor", "enabled", True))
    interval = cfg.get("monitor", "interval_seconds", 300)
    sites_cfg = monitor_service.get_all_sites_config()

    site_lines = []
    for idx, (s_key, s_info) in enumerate(sites_cfg.items(), start=1):
        badge = "🟢" if s_info["enabled"] else "🔴"
        site_lines.append(f"{idx}️⃣ {badge} **{s_info['title']}**: `{s_info['url']}`")

    sites_block = "\n".join(site_lines)
    await call.message.edit_text(
        "📡 **پنل مانیتورینگ خودکار ۵ سایت**\n\n"
        f"🌐 **سایت‌های تحت رصد:**\n{sites_block}\n\n"
        f"⏱ بازه رفرش خودکار: هر `{interval // 60}` دقیقه یک‌بار\n"
        f"📊 وضعیت کلی مانیتورینگ: {'🟢 فعال' if enabled else '🔴 غیرفعال'}\n\n"
        "💡 برای تغییر آدرس سایت‌ها (مثل لینک جدید دیجی‌موویز) یا خاموش/روشن کردن هر سایت، دکمه **«⚙️ مدیریت لینک...»** را بزنید.",
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
    mk_type = call.data.split(":")[1]  # movie | series | anime | empty
    data = await state.get_data()
    workspace = Path(cfg.get("paths", "workspace_dir")).resolve()
    parent_dir = Path(data.get("mkdir_parent", str(workspace))).resolve()
    folder_name = data["new_folder_name"].replace("_", ".")

    from services.automation_service import AutomationService
    cats = AutomationService.ensure_workspace_root_categories()

    if mk_type == "movie":
        base_parent = cats["Movies"] if parent_dir == workspace else parent_dir
        new_dir = base_parent / folder_name
        AutomationService.create_movie_structure(new_dir)
        sub_target = new_dir / "srt"
        desc = "در شاخه `Movies` با ساختار استاندارد فیلم (`SoftSub`, `Hardsub`, `srt`)"
    elif mk_type in ("series", "anime"):
        cat_key = "Anime" if mk_type == "anime" else "Series"
        base_parent = cats[cat_key] if parent_dir == workspace else parent_dir
        show_dir = base_parent / folder_name
        new_dir = show_dir / "S01" if not AutomationService.SEASON_DIR_REGEX.match(folder_name) else show_dir
        AutomationService.create_series_season_structure(new_dir)
        sub_target = new_dir / "srt" / "FA"
        desc = f"در شاخه `{cat_key}` همراه با پوشه‌های استاندارد فصل (`FullHD`, `1080p-HardSub`, `1080p-EBTV`, `srt/FA`, `srt/EN`)"
    else:
        new_dir = parent_dir / folder_name
        new_dir.mkdir(parents=True, exist_ok=True)
        sub_target = new_dir
        desc = "به صورت پوشه ساده"

    await state.clear()
    await state.update_data(target_sub_dir=str(sub_target), current_dir=str(new_dir))

    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text="🚀 اجرای اتوماسیون هوشمند در این پوشه", callback_data="fact:run_smart_auto_here"))
    kb.row(InlineKeyboardButton(text="📥 آپلود زیرنویس / ZIP در این پوشه", callback_data="qsub:now"))
    kb.row(InlineKeyboardButton(text="📂 مشاهده پوشه در فایل‌منیجر", callback_data="menu:file_mgr"))
    kb.row(InlineKeyboardButton(text="🔙 منوی اصلی", callback_data="menu:home"))

    await call.message.edit_text(
        f"✅ پوشه جدید {desc} ساخته شد!\n\n"
        f"📂 مسیر در سرور:\n`{new_dir}`\n"
        f"☁️ مسیر معادل در هاست دانلود:\n`{AutomationService.compute_server_remote_dir_for_local_path(new_dir)}`",
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


# ==================== ۵. فایل‌منیجر سرور با قابلیت انتخاب چندفایلی و تغییر نام گروهی ====================
async def render_directory(call_or_msg: CallbackQuery | Message, state: FSMContext, current_dir: Path):
    root_dir = Path(cfg.get("paths", "workspace_dir")).resolve()
    current_dir = current_dir.resolve()
    current_dir.mkdir(parents=True, exist_ok=True)
    items = sorted(
        [p for p in current_dir.iterdir() if p.name != "browser_profile" and not p.name.startswith("_")],
        key=lambda p: (not p.is_dir(), p.name.lower())
    )

    data = await state.get_data()
    # حذف فایل‌هایی که دیگر وجود ندارند از لیست انتخاب‌شده‌ها
    selected_files = {p for p in data.get("fm_selected_files", []) if Path(p).exists()}

    await state.update_data(
        current_dir=str(current_dir),
        dir_items=[str(p.resolve()) for p in items],
        fm_selected_files=list(selected_files)
    )
    is_root = (current_dir == root_dir)

    item_lines = []
    for idx, p in enumerate(items[:25]):
        if p.is_dir():
            item_lines.append(f"`[{idx+1}]` 📁 `{p.name}`")
        else:
            is_sel = str(p.resolve()) in selected_files
            mark = "✅ " if is_sel else ""
            size_mb = p.stat().st_size / (1024 * 1024)
            icon = "🎬" if p.suffix.lower() in [".mkv", ".mp4", ".avi", ".ts"] else "📄"
            item_lines.append(f"{mark}`[{idx+1}]` {icon} `{p.name}` _({size_mb:.1f} MB)_")

    listing_block = "\n".join(item_lines) if item_lines else "_(این پوشه خالی است)_"

    msg_text = (
        f"📂 **مسیر فعلی:**\n`{current_dir}`\n"
        f"✅ **فایل‌های انتخاب‌شده:** `{len(selected_files)}` فایل\n\n"
        f"📋 **لیست کامل محتویات (برای کپی نام لمس کنید):**\n{listing_block}\n\n"
        "💡 _روی پوشه‌ها کلیک کنید تا باز شوند؛ روی فایل‌ها کلیک کنید تا تیک (`✅`) بخورند:_"
    )

    kb = file_browser_kb(items, is_root, selected_files)
    if isinstance(call_or_msg, CallbackQuery):
        try:
            await call_or_msg.message.edit_text(msg_text[:4000], reply_markup=kb, parse_mode="Markdown")
        except Exception:
            await call_or_msg.answer()
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
    curr = Path(data.get("current_dir", cfg.get("paths", "workspace_dir"))).resolve()
    root_dir = Path(cfg.get("paths", "workspace_dir")).resolve()
    parent = curr.parent if curr != root_dir else root_dir
    await render_directory(call, state, parent)


@router.callback_query(F.data.startswith("fitem:"))
async def cb_file_item(call: CallbackQuery, state: FSMContext):
    idx = int(call.data.split(":")[1])
    data = await state.get_data()
    dir_items = data.get("dir_items", [])
    if idx >= len(dir_items):
        return await call.answer("⚠️ آیتم یافت نشد.")

    selected_path = Path(dir_items[idx]).resolve()

    # اگر پوشه است، وارد پوشه شو
    if selected_path.is_dir():
        return await render_directory(call, state, selected_path)

    # اگر فایل است، وضعیت تیک خوردن (انتخاب چندتایی) آن را تغییر بده
    selected_files = set(data.get("fm_selected_files", []))
    f_str = str(selected_path)
    if f_str in selected_files:
        selected_files.remove(f_str)
    else:
        selected_files.add(f_str)

    await state.update_data(
        fm_selected_files=list(selected_files),
        selected_video=f_str,
        target_videos=sorted(list(selected_files), key=_natural_sort_key)
    )
    curr = Path(data.get("current_dir", cfg.get("paths", "workspace_dir")))
    await render_directory(call, state, curr)


@router.callback_query(F.data == "fact:select_all_files")
async def cb_select_all_files_in_dir(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    dir_items = [Path(p).resolve() for p in data.get("dir_items", [])]
    files_in_dir = [str(p) for p in dir_items if p.is_file()]
    selected_files = set(data.get("fm_selected_files", []))

    if files_in_dir and all(f in selected_files for f in files_in_dir):
        for f in files_in_dir:
            selected_files.discard(f)
    else:
        for f in files_in_dir:
            selected_files.add(f)

    await state.update_data(fm_selected_files=list(selected_files))
    curr = Path(data.get("current_dir", cfg.get("paths", "workspace_dir")))
    await render_directory(call, state, curr)


@router.callback_query(F.data == "fact:clear_selection")
async def cb_clear_file_selection(call: CallbackQuery, state: FSMContext):
    await state.update_data(fm_selected_files=[])
    data = await state.get_data()
    curr = Path(data.get("current_dir", cfg.get("paths", "workspace_dir")))
    await render_directory(call, state, curr)


# ==================== تغییر نام تکی و گروهی فایل‌ها (پشتیبانی از * و شماره‌گذاری 01) ====================
@router.callback_query(F.data.in_({"fact:batch_rename", "vact:rename_file"}))
async def cb_batch_rename_start(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    sel_files = sorted(data.get("fm_selected_files", []), key=_natural_sort_key)
    if not sel_files and data.get("selected_video"):
        sel_files = [data["selected_video"]]

    if not sel_files:
        return await call.answer("⚠️ هیچ فایلی انتخاب نشده است!", show_alert=True)

    await state.update_data(rename_targets=sel_files)
    await state.set_state(MainStates.waiting_batch_rename_pattern)

    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text="🔙 انصراف و بازگشت", callback_data="menu:file_mgr"))

    if len(sel_files) == 1:
        f_obj = Path(sel_files[0])
        prompt_text = (
            "✏️ **تغییر نام فایل**\n\n"
            f"📄 نام فعلی کامل (برای کپی لمس کنید):\n`{f_obj.name}`\n"
            f"🔹 نام بدون پسوند:\n`{f_obj.stem}`\n\n"
            "لطفاً **نام جدید فایل** را ارسال کنید (پسوند فایل به صورت خودکار حفظ می‌شود):"
        )
    else:
        file_list_str = "\n".join([f"`{i+1:02d}.` `{Path(p).name}`" for i, p in enumerate(sel_files[:15])])
        first_stem = Path(sel_files[0]).stem
        prompt_text = (
            f"✏️ **تغییر نام گروهی (`{len(sel_files)}` فایل انتخاب شده)**\n\n"
            f"📋 **ترتیب فایل‌ها برای شماره‌گذاری:**\n{file_list_str}\n\n"
            f"🔹 نمونه نام اولین فایل:\n`{first_stem}`\n\n"
            "💡 **راهنمای الگوی نام‌گذاری:**\n"
            "1️⃣ **با علامت `*`:** هر کجای نام که علامت `*` بگذارید، به ترتیب با `01`، `02`، `03` و... جایگزین می‌شود.\n"
            "   مثال: `Severance.S02E*.1080p` ⬅️ `Severance.S02E01.1080p.mkv`\n"
            "2️⃣ **بدون علامت `*`:** اگر `*` نگذارید، شماره‌های `01`، `02` و... به صورت خودکار در انتهای نام (قبل از پسوند فایل) اضافه می‌شوند.\n\n"
            "لطفاً نام یا الگوی جدید را ارسال کنید:"
        )

    await call.message.edit_text(prompt_text[:4000], reply_markup=kb.as_markup(), parse_mode="Markdown")


@router.message(MainStates.waiting_batch_rename_pattern)
async def handle_batch_rename_submit(message: Message, state: FSMContext):
    pattern = (message.text or "").strip()
    if not pattern:
        return await message.answer("⚠️ لطفاً یک نام معتبر ارسال کنید:")

    data = await state.get_data()
    targets = [Path(p).resolve() for p in data.get("rename_targets", []) if Path(p).exists()]
    if not targets:
        await state.set_state(None)
        return await message.answer("⚠️ فایل‌های انتخاب شده یافت نشدند.")

    total = len(targets)
    renamed_Preview = []
    new_selected = []

    # مرحله ۱: تغییر نام به نام موقت برای جلوگیری از تداخل احتمالی نام‌ها
    temp_pairs: list[tuple[Path, Path]] = []
    try:
        for idx, old_p in enumerate(targets, start=1):
            new_filename = _build_renamed_filename(pattern, old_p, idx, total)
            final_p = old_p.with_name(new_filename)
            temp_p = old_p.with_name(f"_ren_tmp_{idx}_{new_filename}")
            old_p.rename(temp_p)
            temp_pairs.append((temp_p, final_p))

        for temp_p, final_p in temp_pairs:
            temp_p.rename(final_p)
            new_selected.append(str(final_p.resolve()))
            renamed_Preview.append(f"✅ `{final_p.name}`")
    except Exception as e:
        return await message.answer(f"❌ خطا در تغییر نام:\n`{e}`", parse_mode="Markdown")

    await state.set_state(None)
    await state.update_data(fm_selected_files=new_selected)

    preview_block = "\n".join(renamed_Preview[:15])
    await message.answer(
        f"🎉 **تغییر نام `{len(new_selected)}` فایل با موفقیت انجام شد!**\n\n{preview_block}",
        parse_mode="Markdown"
    )
    curr_dir = Path(data.get("current_dir", targets[0].parent))
    await render_directory(message, state, curr_dir)


# ==================== حذف تکی و گروهی فایل‌ها در فایل‌منیجر ====================
@router.callback_query(F.data.in_({"fact:batch_delete", "vact:delete_file"}))
async def cb_batch_delete_ask(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    sel_files = sorted(data.get("fm_selected_files", []), key=_natural_sort_key)
    if not sel_files and data.get("selected_video"):
        sel_files = [data["selected_video"]]

    if not sel_files:
        return await call.answer("⚠️ هیچ فایلی انتخاب نشده است!", show_alert=True)

    await state.update_data(delete_targets=sel_files)
    file_lines = "\n".join([f"• `{Path(p).name}`" for p in sel_files[:15]])

    kb = InlineKeyboardBuilder()
    kb.row(
        InlineKeyboardButton(text=f"✅ بله، حذف شود ({len(sel_files)} فایل)", callback_data="fact:confirm_batch_del"),
        InlineKeyboardButton(text="❌ انصراف", callback_data="menu:file_mgr"),
    )
    await call.message.edit_text(
        f"⚠️ **آیا از حذف دائمی `{len(sel_files)}` فایل زیر اطمینان دارید؟**\n\n{file_lines}",
        reply_markup=kb.as_markup(),
        parse_mode="Markdown"
    )


@router.callback_query(F.data == "fact:confirm_batch_del")
async def cb_batch_delete_confirm(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    targets = [Path(p) for p in data.get("delete_targets", [])]
    deleted_count = 0

    for p in targets:
        try:
            if p.is_file():
                p.unlink(missing_ok=True)
                deleted_count += 1
        except Exception:
            pass

    await state.update_data(fm_selected_files=[], delete_targets=[])
    await call.answer(f"🗑 تعداد {deleted_count} فایل با موفقیت حذف شد!", show_alert=True)
    curr_dir = Path(data.get("current_dir", cfg.get("paths", "workspace_dir")))
    await render_directory(call, state, curr_dir)


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
    await state.update_data(current_dir=str(new_dir), fm_selected_files=[])
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

    await state.update_data(current_dir=str(parent_dir), fm_selected_files=[])
    await call.answer(f"🗑 پوشه {dir_name} به طور کامل حذف شد!", show_alert=True)
    await render_directory(call, state, parent_dir)


# ==================== انتخاب مسیر مقصد در هاست دانلود برای CuteFTP 9 (تکی، چندفایلی و پوشه‌ای) ====================
@router.callback_query(F.data.in_({"fact:upload_current_dir", "fact:upload_selected_files", "vact:upload_file"}))
async def cb_cuteftp_choose_destination(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()

    if call.data == "fact:upload_selected_files":
        sel_files = sorted(data.get("fm_selected_files", []), key=_natural_sort_key)
        if not sel_files:
            return await call.answer("⚠️ هیچ فایلی انتخاب نشده است!", show_alert=True)
        await state.update_data(cuteftp_upload_targets=sel_files)
        target_desc = f"{len(sel_files)} فایل انتخاب‌شده"
    elif call.data == "vact:upload_file":
        target = Path(data["selected_video"]).resolve()
        await state.update_data(cuteftp_upload_targets=[str(target)])
        target_desc = target.name
    else:
        target = Path(data.get("current_dir", cfg.get("paths", "workspace_dir"))).resolve()
        await state.update_data(cuteftp_upload_targets=[str(target)])
        target_desc = f"پوشه {target.name}"

    active_gui_path = await CuteFTPService.get_active_gui_remote_path()

    await call.message.edit_text(
        f"☁️ **آپلود به هاست دانلود (CuteFTP 9)**\n\n"
        f"📦 آیتم انتخاب شده برای آپلود:\n`{target_desc}`\n"
        f"📍 مسیر فعلی باز در برنامه CuteFTP:\n`{active_gui_path}`\n\n"
        "در کدام پوشه از هاست دانلود آپلود شود؟",
        reply_markup=cuteftp_destination_kb(active_gui_path),
        parse_mode="Markdown"
    )


async def _enqueue_cuteftp_targets(data: dict, remote_dest: str) -> str:
    targets = [Path(p) for p in data.get("cuteftp_upload_targets", [])]
    if not targets and data.get("cuteftp_upload_target"):
        targets = [Path(data["cuteftp_upload_target"])]

    title_label = targets[0].name if len(targets) == 1 else f"{len(targets)} فایل انتخابی"
    dest_title = "Current" if remote_dest == "__CURRENT__" else remote_dest
    task = await queue_engine.add_task(
        "upload",
        f"CuteFTP [{dest_title}]: {title_label}",
        CuteFTPService.upload_multiple_or_single,
        targets,
        remote_dest
    )
    return task.task_id


@router.callback_query(F.data == "cftp_dest:current")
async def cb_cuteftp_upload_to_current(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    task_id = await _enqueue_cuteftp_targets(data, "__CURRENT__")
    await state.clear()
    await call.message.edit_text(
        f"☁️ آپلود به **مسیر فعلی CuteFTP** در صف قرار گرفت!\n"
        f"🆔 کد تسک: `{task_id}`",
        reply_markup=main_menu_kb(),
        parse_mode="Markdown"
    )


@router.callback_query(F.data.startswith("cftp_dest:preset:"))
async def cb_cuteftp_upload_to_preset(call: CallbackQuery, state: FSMContext):
    idx = int(call.data.split(":")[2])
    destinations = CuteFTPService.get_saved_destinations()
    chosen_dest = destinations[idx]["path"]

    data = await state.get_data()
    task_id = await _enqueue_cuteftp_targets(data, chosen_dest)
    await state.clear()
    await call.message.edit_text(
        f"☁️ آپلود به مسیر `{chosen_dest}` در صف قرار گرفت!\n"
        f"🆔 کد تسک: `{task_id}`",
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
    task_id = await _enqueue_cuteftp_targets(data, remote_path)
    await state.clear()
    await message.answer(
        f"☁️ آپلود به مسیر `{remote_path}` آغاز شد!\n"
        f"🆔 کد تسک: `{task_id}`",
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