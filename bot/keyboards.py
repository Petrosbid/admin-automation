from pathlib import Path

from aiogram.types import (
    InlineKeyboardMarkup,
    InlineKeyboardButton
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

from services.scraper_service import ScraperService
from services.handbrake_service import HandBrakeService


def main_menu_kb() -> InlineKeyboardMarkup:

    builder = InlineKeyboardBuilder()

    builder.row(
        InlineKeyboardButton(
            text="📡 مانیتورینگ خودکار (Pahe / PSA / YTS)",
            callback_data="menu:monitor"
        )
    )

    builder.row(
        InlineKeyboardButton(
            text="📁 ساخت پوشه جدید در Workspace",
            callback_data="menu:create_folder"
        ),
        InlineKeyboardButton(
            text="📥 آپلود زیرنویس",
            callback_data="menu:upload_sub_target"
        )
    )

    builder.row(
        InlineKeyboardButton(
            text="🔍 ۱. جستجو در سایت‌ها",
            callback_data="menu:search"
        )
    )

    builder.row(
        InlineKeyboardButton(
            text="📥 ۲. دانلود لینک مستقیم / تورنت / Mega / GDrive",
            callback_data="menu:direct_dl"
        )
    )

    builder.row(
        InlineKeyboardButton(
            text="📤 ۳. دانلود فیلم و استخراج زیرنویس",
            callback_data="menu:link_extract_sub"
        )
    )

    builder.row(
        InlineKeyboardButton(
            text="🎬 ۴. سافت‌ساب / هاردساب فیلم‌ها",
            callback_data="menu:sub_to_files"
        )
    )

    builder.row(
        InlineKeyboardButton(
            text="📂 ۵. فایل‌منیجر و آپلود با CuteFTP",
            callback_data="menu:file_mgr"
        )
    )

    builder.row(
        InlineKeyboardButton(
            text="📊 وضعیت صف پردازش‌ها",
            callback_data="menu:queues"
        )
    )

    builder.row(
        InlineKeyboardButton(
            text="🔄 ریلود کانفیگ",
            callback_data="menu:reload_cfg"
        )
    )

    return builder.as_markup()


# ==================================================
# MONITORING
# ==================================================

def monitor_menu_kb(is_enabled: bool) -> InlineKeyboardMarkup:

    builder = InlineKeyboardBuilder()

    status_label = (
        "🟢 فعال (توقف مانیتورینگ)"
        if is_enabled
        else "🔴 غیرفعال (شروع مانیتورینگ)"
    )

    builder.row(
        InlineKeyboardButton(
            text=status_label,
            callback_data="mon:toggle"
        )
    )

    builder.row(
        InlineKeyboardButton(
            text="🔓 باز کردن صفحات روی سرور برای حل کپچا",
            callback_data="mon:open_captcha"
        )
    )

    builder.row(
        InlineKeyboardButton(
            text="🔄 بررسی فوری سایت‌ها",
            callback_data="mon:check_now"
        )
    )

    builder.row(
        InlineKeyboardButton(
            text="🎬 آخرین‌های Pahe",
            callback_data="mon:latest:pahe"
        ),
        InlineKeyboardButton(
            text="📺 آخرین‌های PSA",
            callback_data="mon:latest:psa"
        ),
        InlineKeyboardButton(
            text="🍿 آخرین‌های YTS",
            callback_data="mon:latest:yts"
        )
    )

    builder.row(
        InlineKeyboardButton(
            text="🔙 منوی اصلی",
            callback_data="menu:home"
        )
    )

    return builder.as_markup()


# ==================================================
# FOLDER MANAGEMENT
# ==================================================

def folder_structure_choice_kb() -> InlineKeyboardMarkup:

    builder = InlineKeyboardBuilder()

    builder.row(
        InlineKeyboardButton(
            text="📂 ساختار استاندارد (softsub / hardsub / sub)",
            callback_data="mktype:standard"
        )
    )

    builder.row(
        InlineKeyboardButton(
            text="📁 فقط پوشه خالی",
            callback_data="mktype:empty"
        )
    )

    builder.row(
        InlineKeyboardButton(
            text="🔙 انصراف",
            callback_data="menu:home"
        )
    )

    return builder.as_markup()


def target_folders_kb(
    folders: list[Path],
    base_workspace: Path
) -> InlineKeyboardMarkup:

    builder = InlineKeyboardBuilder()

    for idx, folder in enumerate(folders[:25]):

        try:
            rel_name = folder.relative_to(
                base_workspace
            ).as_posix()

        except ValueError:
            rel_name = folder.name

        builder.row(
            InlineKeyboardButton(
                text=f"📁 {rel_name[:45]}",
                callback_data=f"tsubdir:{idx}"
            )
        )

    builder.row(
        InlineKeyboardButton(
            text="🔙 منوی اصلی",
            callback_data="menu:home"
        )
    )

    return builder.as_markup()


def download_target_folders_kb(
    folders: list[Path]
) -> InlineKeyboardMarkup:

    """
    انتخاب مقصد دانلود:
    - پوشه موجود
    - ساخت پوشه جدید
    """

    builder = InlineKeyboardBuilder()

    for idx, folder in enumerate(folders[:25]):

        builder.row(
            InlineKeyboardButton(
                text=f"📂 {folder.name[:45]}",
                callback_data=f"dlfolder:{idx}"
            )
        )

    builder.row(
        InlineKeyboardButton(
            text="➕ ساخت پوشه جدید",
            callback_data="dlnew"
        )
    )

    builder.row(
        InlineKeyboardButton(
            text="🔙 انصراف",
            callback_data="menu:home"
        )
    )

    return builder.as_markup()


# ==================================================
# SEARCH
# ==================================================

def search_categories_kb() -> InlineKeyboardMarkup:

    builder = InlineKeyboardBuilder()

    builder.row(
        InlineKeyboardButton(
            text="🎌 سایت‌های انیمه",
            callback_data="scat:anime"
        )
    )

    builder.row(
        InlineKeyboardButton(
            text="🎬 سایت‌های فیلم و سریال",
            callback_data="scat:general"
        )
    )

    builder.row(
        InlineKeyboardButton(
            text="🇰🇷 سایت‌های کره‌ای",
            callback_data="scat:korean"
        )
    )

    builder.row(
        InlineKeyboardButton(
            text="🔙 بازگشت",
            callback_data="menu:home"
        )
    )

    return builder.as_markup()


def search_sites_kb(category: str) -> InlineKeyboardMarkup:

    builder = InlineKeyboardBuilder()

    sites = ScraperService.SITE_CATEGORIES.get(
        category,
        {}
    )

    for key, title in sites.items():

        builder.row(
            InlineKeyboardButton(
                text=title[:60],
                callback_data=f"ssite:{key}"
            )
        )

    builder.row(
        InlineKeyboardButton(
            text="🔙 بازگشت",
            callback_data="menu:search"
        )
    )

    return builder.as_markup()


def search_results_kb(
    results: list[dict]
) -> InlineKeyboardMarkup:

    builder = InlineKeyboardBuilder()

    for idx, item in enumerate(results):

        icon = (
            "🧲"
            if item.get("type") == "magnet"
            else "📄"
        )

        # متن دکمه محدود می‌شود؛
        # نام کامل در متن پیام جداگانه نمایش داده خواهد شد.

        title = str(
            item.get("title", "بدون عنوان")
        )

        builder.row(
            InlineKeyboardButton(
                text=f"{icon} {title[:48]}",
                callback_data=f"sres:{idx}"
            )
        )

    builder.row(
        InlineKeyboardButton(
            text="🔙 منوی اصلی",
            callback_data="menu:home"
        )
    )

    return builder.as_markup()


# ==================================================
# FILE MANAGER
# ==================================================

def file_browser_kb(
    items: list[Path],
    is_root: bool
) -> InlineKeyboardMarkup:

    builder = InlineKeyboardBuilder()

    for idx, path in enumerate(items[:25]):

        if path.is_dir():
            icon = "📁"

        elif path.suffix.lower() in [
            ".mkv",
            ".mp4",
            ".avi",
            ".ts",
            ".mov",
            ".m4v",
            ".webm"
        ]:
            icon = "🎬"

        else:
            icon = "📄"

        builder.row(
            InlineKeyboardButton(
                text=f"{icon} {path.name[:45]}",
                callback_data=f"fitem:{idx}"
            )
        )

    if not is_root:

        builder.row(
            InlineKeyboardButton(
                text="⬆️ پوشه قبلی",
                callback_data="fnav:up"
            )
        )

    builder.row(
        InlineKeyboardButton(
            text="📁 ساخت پوشه جدید",
            callback_data="fact:mkdir_here"
        ),
        InlineKeyboardButton(
            text="📥 آپلود زیرنویس",
            callback_data="fact:upload_sub_here"
        )
    )

    builder.row(
        InlineKeyboardButton(
            text="☁️ آپلود کل پوشه با CuteFTP",
            callback_data="fact:upload_current_dir"
        )
    )

    builder.row(
        InlineKeyboardButton(
            text="🔙 منوی اصلی",
            callback_data="menu:home"
        )
    )

    return builder.as_markup()


# ==================================================
# VIDEO SELECTION
# ==================================================

def multi_video_select_kb(
    videos: list[Path],
    selected_indices: set[int]
) -> InlineKeyboardMarkup:

    builder = InlineKeyboardBuilder()

    for idx, video in enumerate(videos[:25]):

        mark = (
            "✅"
            if idx in selected_indices
            else "⬜"
        )

        rel_label = (
            f"{video.parent.name}/{video.name}"
        )

        builder.row(
            InlineKeyboardButton(
                text=f"{mark} {rel_label[:43]}",
                callback_data=f"mvsel:{idx}"
            )
        )

    builder.row(
        InlineKeyboardButton(
            text="📎 ادامه به مرحله زیرنویس ➡️",
            callback_data="mvact:proceed"
        )
    )

    builder.row(
        InlineKeyboardButton(
            text="🔙 انصراف",
            callback_data="menu:home"
        )
    )

    return builder.as_markup()


# ==================================================
# SUBTITLE SOURCE SELECTION
# ==================================================

def subtitle_source_kb(
    language: str
) -> InlineKeyboardMarkup:

    """
    language:
        fa
        en

    کاربر می‌تواند:
    1. پوشه را برای زیرنویس موجود بررسی کند.
    2. فایل زیرنویس را آپلود کند.
    3. از این زبان عبور کند.
    """

    language_name = (
        "فارسی"
        if language == "fa"
        else "انگلیسی"
    )

    builder = InlineKeyboardBuilder()

    builder.row(
        InlineKeyboardButton(
            text=f"🔍 بله، زیرنویس {language_name} در پوشه است",
            callback_data=f"subscan:{language}"
        )
    )

    builder.row(
        InlineKeyboardButton(
            text=f"📤 خیر، آپلود زیرنویس {language_name}",
            callback_data=f"subupload:{language}"
        )
    )

    builder.row(
        InlineKeyboardButton(
            text="⏭ این زبان را رد کن",
            callback_data=f"skip:{language}_sub"
        )
    )

    builder.row(
        InlineKeyboardButton(
            text="🔙 انصراف",
            callback_data="menu:home"
        )
    )

    return builder.as_markup()


def subtitle_file_found_kb(
    language: str,
    files: list[Path]
) -> InlineKeyboardMarkup:

    builder = InlineKeyboardBuilder()

    for idx, path in enumerate(files[:20]):

        builder.row(
            InlineKeyboardButton(
                text=f"📄 {path.name[:45]}",
                callback_data=f"subfile:{language}:{idx}"
            )
        )

    builder.row(
        InlineKeyboardButton(
            text="📤 آپلود فایل دیگر",
            callback_data=f"subupload:{language}"
        )
    )

    builder.row(
        InlineKeyboardButton(
            text="⏭ رد شدن",
            callback_data=f"skip:{language}_sub"
        )
    )

    return builder.as_markup()


# ==================================================
# SUBTITLE SYNC
# ==================================================

def sync_options_kb() -> InlineKeyboardMarkup:

    builder = InlineKeyboardBuilder()

    builder.row(
        InlineKeyboardButton(
            text="⚡ سینک خودکار با ffsubsync",
            callback_data="syncopt:auto"
        )
    )

    builder.row(
        InlineKeyboardButton(
            text="⏱ سینک دستی (میلی‌ثانیه)",
            callback_data="syncopt:manual"
        )
    )

    builder.row(
        InlineKeyboardButton(
            text="✅ زیرنویس از قبل سینک است",
            callback_data="syncopt:none"
        )
    )

    builder.row(
        InlineKeyboardButton(
            text="🔙 انصراف",
            callback_data="menu:home"
        )
    )

    return builder.as_markup()


# ==================================================
# OPERATION MODE
# ==================================================

def subtitle_mode_kb() -> InlineKeyboardMarkup:

    builder = InlineKeyboardBuilder()

    builder.row(
        InlineKeyboardButton(
            text="📎 فقط SoftSub",
            callback_data="submode:softsub"
        )
    )

    builder.row(
        InlineKeyboardButton(
            text="🔥 فقط HardSub",
            callback_data="submode:hardsub"
        )
    )

    builder.row(
        InlineKeyboardButton(
            text="🚀 هر دو عملیات",
            callback_data="submode:both"
        )
    )

    builder.row(
        InlineKeyboardButton(
            text="🔙 انصراف",
            callback_data="menu:home"
        )
    )

    return builder.as_markup()


def skip_or_cancel_kb(
    skip_cb: str
) -> InlineKeyboardMarkup:

    builder = InlineKeyboardBuilder()

    builder.row(
        InlineKeyboardButton(
            text="⏭ رد شدن",
            callback_data=skip_cb
        )
    )

    builder.row(
        InlineKeyboardButton(
            text="🔙 انصراف",
            callback_data="menu:home"
        )
    )

    return builder.as_markup()


# ==================================================
# VIDEO ACTIONS
# ==================================================

def video_actions_kb() -> InlineKeyboardMarkup:

    builder = InlineKeyboardBuilder()

    builder.row(
        InlineKeyboardButton(
            text="📤 استخراج زیرنویس",
            callback_data="vact:extract_sub"
        )
    )

    builder.row(
        InlineKeyboardButton(
            text="📎 SoftSub",
            callback_data="vact:softsub"
        )
    )

    builder.row(
        InlineKeyboardButton(
            text="🔥 HardSub",
            callback_data="vact:hardsub"
        )
    )

    builder.row(
        InlineKeyboardButton(
            text="🚀 اتوماسیون کامل",
            callback_data="vact:full_auto"
        )
    )

    builder.row(
        InlineKeyboardButton(
            text="☁️ آپلود با CuteFTP",
            callback_data="vact:upload_file"
        )
    )

    builder.row(
        InlineKeyboardButton(
            text="🔙 بازگشت به پوشه",
            callback_data="menu:file_mgr"
        )
    )

    return builder.as_markup()


# ==================================================
# HANDBRAKE PRESETS
# ==================================================

def presets_kb() -> InlineKeyboardMarkup:

    builder = InlineKeyboardBuilder()

    presets = HandBrakeService.list_available_presets()

    for idx, preset in enumerate(presets):

        builder.row(
            InlineKeyboardButton(
                text=f"⚙️ {preset['label'][:50]}",
                callback_data=f"preset:{idx}"
            )
        )

    builder.row(
        InlineKeyboardButton(
            text="🔙 انصراف",
            callback_data="menu:file_mgr"
        )
    )

    return builder.as_markup()