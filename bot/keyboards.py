from pathlib import Path
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder
from services.scraper_service import ScraperService
from services.handbrake_service import HandBrakeService


def main_menu_kb() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="📡 مانیتورینگ خودکار (Pahe / PSA / YTS)", callback_data="menu:monitor"),
    )
    builder.row(
        InlineKeyboardButton(text="📁 ساخت پوشه جدید در Workspace (بدون لینک)", callback_data="menu:create_folder"),
        InlineKeyboardButton(text="📥 آپلود زیرنویس در پوشه هدف", callback_data="menu:upload_sub_target"),
    )
    builder.row(
        InlineKeyboardButton(text="🔍 ۱. جستجو در ۹ سایت (تورنت/انیمه/کره‌ای)", callback_data="menu:search"),
    )
    builder.row(
        InlineKeyboardButton(text="📥 ۲. دانلود لینک مستقیم / تورنت (qbt) / Mega / GDrive", callback_data="menu:direct_dl"),
    )
    builder.row(
        InlineKeyboardButton(text="📤 ۳. ارسال لینک فیلم و دریافت مستقیم زیرنویس آن", callback_data="menu:link_extract_sub"),
    )
    builder.row(
        InlineKeyboardButton(text="🎬 ۴. ارسال زیرنویس (FA/EN) و مرج با فایل‌های انتخابی", callback_data="menu:sub_to_files"),
    )
    builder.row(
        InlineKeyboardButton(text="📂 ۵. فایل‌منیجر سرور و آپلود با CuteFTP 9", callback_data="menu:file_mgr"),
    )
    builder.row(
        InlineKeyboardButton(text="📊 وضعیت صف‌ها (HandBrake / MKV / qbt)", callback_data="menu:queues"),
        InlineKeyboardButton(text="🔄 ریلود کانفیگ", callback_data="menu:reload_cfg"),
    )
    return builder.as_markup()


def monitor_menu_kb(is_enabled: bool) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    status_label = "🟢 فعال (توقف مانیتورینگ)" if is_enabled else "🔴 غیرفعال (شروع مانیتورینگ)"
    builder.row(InlineKeyboardButton(text=status_label, callback_data="mon:toggle"))
    builder.row(InlineKeyboardButton(text="🔄 بررسی فوری هر ۳ سایت همین الان", callback_data="mon:check_now"))
    builder.row(
        InlineKeyboardButton(text="🎬 آخرین‌های Pahe", callback_data="mon:latest:pahe"),
        InlineKeyboardButton(text="📺 آخرین‌های PSA", callback_data="mon:latest:psa"),
        InlineKeyboardButton(text="🍿 آخرین‌های YTS", callback_data="mon:latest:yts"),
    )
    builder.row(InlineKeyboardButton(text="🔙 بازگشت به منوی اصلی", callback_data="menu:home"))
    return builder.as_markup()


def folder_structure_choice_kb() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text="📂 استاندارد فیلم/سریال (همراه با softsub, hardsub, sub)",
            callback_data="mktype:standard"
        )
    )
    builder.row(
        InlineKeyboardButton(
            text="📁 فقط پوشه ساده (خالی)",
            callback_data="mktype:empty"
        )
    )
    builder.row(InlineKeyboardButton(text="🔙 انصراف", callback_data="menu:home"))
    return builder.as_markup()


def target_folders_kb(folders: list[Path], base_workspace: Path) -> InlineKeyboardMarkup:
    """لیست پوشه‌های موجود در Workspace برای انتخاب پوشه هدف جهت آپلود زیرنویس"""
    builder = InlineKeyboardBuilder()
    for idx, folder in enumerate(folders[:25]):
        try:
            rel_name = folder.relative_to(base_workspace).as_posix()
        except ValueError:
            rel_name = folder.name
        builder.row(
            InlineKeyboardButton(text=f"📁 {rel_name[:50]}", callback_data=f"tsubdir:{idx}")
        )
    builder.row(InlineKeyboardButton(text="🔙 منوی اصلی", callback_data="menu:home"))
    return builder.as_markup()


def subtitle_upload_options_kb() -> InlineKeyboardMarkup:
    """انتخاب نحوه ذخیره زیرنویس در پوشه هدف"""
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="🎨 ذخیره همراه با استایل بولد و زرد (#fbff00)", callback_data="subsave:styled")
    )
    builder.row(
        InlineKeyboardButton(text="📄 ذخیره به صورت خام (بدون تغییر استایل)", callback_data="subsave:raw")
    )
    builder.row(InlineKeyboardButton(text="🔙 انصراف", callback_data="menu:home"))
    return builder.as_markup()


def search_categories_kb() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="🎌 سایت‌های انیمه (Nyaa / SubsPlease / Kayo)", callback_data="scat:anime"))
    builder.row(InlineKeyboardButton(text="🎬 سایت‌های فیلم و سریال (BT4G / EZTV / Uindex / WSM)", callback_data="scat:general"))
    builder.row(InlineKeyboardButton(text="🇰🇷 سایت‌های کره‌ای (MKVDrama .net / .top)", callback_data="scat:korean"))
    builder.row(InlineKeyboardButton(text="🔙 بازگشت به منوی اصلی", callback_data="menu:home"))
    return builder.as_markup()


def search_sites_kb(category: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    sites = ScraperService.SITE_CATEGORIES.get(category, {})
    for key, title in sites.items():
        builder.row(InlineKeyboardButton(text=title, callback_data=f"ssite:{key}"))
    builder.row(InlineKeyboardButton(text="🔙 بازگشت", callback_data="menu:search"))
    return builder.as_markup()


def search_results_kb(results: list[dict]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for idx, item in enumerate(results):
        icon = "🧲" if item.get("type") == "magnet" else "📄"
        builder.row(
            InlineKeyboardButton(text=f"{icon} {item['title'][:50]}", callback_data=f"sres:{idx}")
        )
    builder.row(InlineKeyboardButton(text="🔙 منوی اصلی", callback_data="menu:home"))
    return builder.as_markup()


def file_browser_kb(items: list[Path], is_root: bool) -> InlineKeyboardMarkup:
    """نمایش پوشه‌ها و فایل‌های ویندوز سرور روی گوشی"""
    builder = InlineKeyboardBuilder()
    for idx, p in enumerate(items[:25]):
        icon = "📁" if p.is_dir() else ("🎬" if p.suffix.lower() in [".mkv", ".mp4", ".avi", ".ts"] else "📄")
        builder.row(
            InlineKeyboardButton(text=f"{icon} {p.name[:48]}", callback_data=f"fitem:{idx}")
        )
    if not is_root:
        builder.row(InlineKeyboardButton(text="⬆️ پوشه قبلی (Up)", callback_data="fnav:up"))
    builder.row(
        InlineKeyboardButton(text="📁 ساخت پوشه جدید در اینجا", callback_data="fact:mkdir_here"),
        InlineKeyboardButton(text="📥 آپلود زیرنویس در این پوشه", callback_data="fact:upload_sub_here"),
    )
    builder.row(
        InlineKeyboardButton(text="☁️ آپلود کل این پوشه با CuteFTP 9", callback_data="fact:upload_current_dir")
    )
    builder.row(InlineKeyboardButton(text="🔙 منوی اصلی", callback_data="menu:home"))
    return builder.as_markup()


def multi_video_select_kb(videos: list[Path], selected_indices: set[int]) -> InlineKeyboardMarkup:
    """کیبورد انتخاب یک یا چند فایل ویدئویی برای اعمال سافت‌ساب یا هاردساب گروهی"""
    builder = InlineKeyboardBuilder()
    for idx, v in enumerate(videos[:25]):
        mark = "✅" if idx in selected_indices else "⬜"
        rel_label = f"{v.parent.name}/{v.name}" if v.parent else v.name
        builder.row(
            InlineKeyboardButton(text=f"{mark} {rel_label[:48]}", callback_data=f"mvsel:{idx}")
        )
    builder.row(
        InlineKeyboardButton(text="📎 ادامه به مرحله ارسال زیرنویس (فارسی / انگلیسی) ➡️", callback_data="mvact:proceed")
    )
    builder.row(InlineKeyboardButton(text="🔙 انصراف و منوی اصلی", callback_data="menu:home"))
    return builder.as_markup()


def sync_options_kb() -> InlineKeyboardMarkup:
    """انتخاب وضعیت سینک شدن زیرنویس فارسی قبل از مرج یا هاردساب"""
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="⚡ ۱. سینک خودکار هوشمند با فیلم (ffsubsync)", callback_data="syncopt:auto")
    )
    builder.row(
        InlineKeyboardButton(text="⏱ ۲. سینک دستی با شیفت زمانی (+/- میلی‌ثانیه)", callback_data="syncopt:manual")
    )
    builder.row(
        InlineKeyboardButton(text="✅ ۳. زیرنویس از قبل سینک است (بدون تغییر تایم)", callback_data="syncopt:none")
    )
    builder.row(InlineKeyboardButton(text="🔙 انصراف", callback_data="menu:home"))
    return builder.as_markup()


def subtitle_mode_kb() -> InlineKeyboardMarkup:
    """انتخاب نوع عملیات پس از دریافت و سینک زیرنویس"""
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="📎 فقط سافت‌ساب (صف MKVToolNix)", callback_data="submode:softsub"))
    builder.row(InlineKeyboardButton(text="🔥 فقط هاردساب زرد و بولد (صف HandBrake)", callback_data="submode:hardsub"))
    builder.row(InlineKeyboardButton(text="🚀 هر دو: پکیج کامل (پوشه softsub + hardsub + sub)", callback_data="submode:both"))
    builder.row(InlineKeyboardButton(text="🔙 انصراف", callback_data="menu:home"))
    return builder.as_markup()


def skip_or_cancel_kb(skip_cb: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="⏭ رد شدن از این زیرنویس (ندارم)", callback_data=skip_cb))
    builder.row(InlineKeyboardButton(text="🔙 انصراف", callback_data="menu:home"))
    return builder.as_markup()


def video_actions_kb() -> InlineKeyboardMarkup:
    """عملیات‌های قابل انجام روی یک فایل فیلم انتخاب شده در فایل‌منیجر"""
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="📤 ۱. استخراج زیرنویس از این فیلم", callback_data="vact:extract_sub"))
    builder.row(InlineKeyboardButton(text="📎 ۲. مرج سافت‌ساب فارسی + انگلیسی (صف MKVToolNix)", callback_data="vact:softsub"))
    builder.row(InlineKeyboardButton(text="🔥 ۳. بولد + زرد (#fbff00) + هاردساب (صف HandBrake)", callback_data="vact:hardsub"))
    builder.row(InlineKeyboardButton(text="🚀 ۴. اتوماسیون کامل (ساخت پوشه softsub/hardsub/sub)", callback_data="vact:full_auto"))
    builder.row(InlineKeyboardButton(text="☁️ ۵. آپلود همین فایل با CuteFTP 9", callback_data="vact:upload_file"))
    builder.row(InlineKeyboardButton(text="🔙 بازگشت به پوشه", callback_data="menu:file_mgr"))
    return builder.as_markup()


def presets_kb() -> InlineKeyboardMarkup:
    """لیست پریست‌های HandBrake از پوشه presets"""
    builder = InlineKeyboardBuilder()
    presets = HandBrakeService.list_available_presets()
    for idx, pr in enumerate(presets):
        builder.row(InlineKeyboardButton(text=f"⚙️ {pr['label']}", callback_data=f"preset:{idx}"))
    builder.row(InlineKeyboardButton(text="🔙 انصراف", callback_data="menu:file_mgr"))
    return builder.as_markup()