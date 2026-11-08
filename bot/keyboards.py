import math
from pathlib import Path
from typing import Any
from aiogram.types import (
    InlineKeyboardMarkup,
    InlineKeyboardButton
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

from services.scraper_service import ScraperService
from services.handbrake_service import HandBrakeService
from services.cuteftp_service import CuteFTPService

BROWSER_PAGE_SIZE = 15  # تعداد پوشه‌ها/فایل‌ها در هر صفحه


def smart_middle_truncate(text: str, max_len: int = 40) -> str:
    text = str(text).strip()
    if len(text) <= max_len:
        return text
    head_len = (max_len - 1) // 2 + 2
    tail_len = max_len - head_len - 1
    return f"{text[:head_len]}…{text[-tail_len:]}"


def _append_pagination_row(
    builder: InlineKeyboardBuilder,
    prefix: str,
    page: int,
    total_pages: int
):
    """افزودن ردیف دکمه‌های ورق زدن صفحه (⬅️ قبلی | 📄 صفحه X از Y | بعدی ➡️)"""
    if total_pages <= 1:
        return
    nav_btns = []
    if page > 0:
        nav_btns.append(
            InlineKeyboardButton(text="⬅️ قبلی", callback_data=f"{prefix}:{page - 1}")
        )
    nav_btns.append(
        InlineKeyboardButton(text=f"📄 صفحه {page + 1} از {total_pages}", callback_data="qnoop")
    )
    if page < total_pages - 1:
        nav_btns.append(
            InlineKeyboardButton(text="بعدی ➡️", callback_data=f"{prefix}:{page + 1}")
        )
    builder.row(*nav_btns)


def main_menu_kb() -> InlineKeyboardMarkup:
    """منوی اصلی گروه‌بندی‌شده با برچسب‌های کوتاه‌تر و چیدمان سازگار با موبایل."""
    builder = InlineKeyboardBuilder()

    # عملیات اصلی
    builder.row(
        InlineKeyboardButton(
            text="🚀 اتوماسیون هوشمند",
            callback_data="menu:smart_auto"
        )
    )

    # پایش و وضعیت سیستم
    builder.row(
        InlineKeyboardButton(
            text="📡 مانیتورینگ",
            callback_data="menu:monitor"
        ),
        InlineKeyboardButton(
            text="📊 صف و پردازش‌ها",
            callback_data="menu:queues"
        )
    )

    # دریافت محتوا
    builder.row(
        InlineKeyboardButton(
            text="🔎 جستجو",
            callback_data="menu:search"
        ),
        InlineKeyboardButton(
            text="⬇️ دانلود لینک / تورنت",
            callback_data="menu:direct_dl"
        )
    )

    # مدیریت فایل و پوشه
    builder.row(
        InlineKeyboardButton(
            text="📁 ساخت پوشه",
            callback_data="menu:create_folder"
        ),
        InlineKeyboardButton(
            text="🗂 فایل‌منیجر",
            callback_data="menu:file_mgr"
        )
    )

    # زیرنویس و پردازش ویدئو
    builder.row(
        InlineKeyboardButton(
            text="📤 آپلود زیرنویس",
            callback_data="menu:upload_sub_target"
        ),
        InlineKeyboardButton(
            text="📝 استخراج زیرنویس",
            callback_data="menu:link_extract_sub"
        )
    )
    builder.row(
        InlineKeyboardButton(
            text="🎬 SoftSub / HardSub / Encode",
            callback_data="menu:sub_to_files"
        )
    )

    # تنظیمات
    builder.row(
        InlineKeyboardButton(
            text="⚙️ تنظیمات",
            callback_data="menu:settings"
        )
    )

    return builder.as_markup()


# ==================================================
# SMART FULL AUTOMATION KEYBOARDS
# ==================================================

def smart_auto_browser_kb(
    folders: list[Path],
    current_dir: Path,
    is_root: bool,
    detected_episodes: list[str],
    selected_episodes: set[str] | None = None,
    page: int = 0
) -> InlineKeyboardMarkup:
    selected_episodes = selected_episodes or set()
    builder = InlineKeyboardBuilder()

    if not is_root:
        if detected_episodes:
            ep_buttons = []
            for ep in detected_episodes[:30]:
                mark = "✅" if ep in selected_episodes else "⬜"
                ep_buttons.append(
                    InlineKeyboardButton(
                        text=f"{mark} {ep}",
                        callback_data=f"sauto:ep_toggle:{ep}"
                    )
                )
            for i in range(0, len(ep_buttons), 3):
                builder.row(*ep_buttons[i:i + 3])

            builder.row(
                InlineKeyboardButton(
                    text="☑️ انتخاب / لغو انتخاب همه قسمت‌های این فصل",
                    callback_data="sauto:ep_all"
                )
            )

            sel_count = len(selected_episodes)
            if sel_count > 0:
                builder.row(
                    InlineKeyboardButton(
                        text=f"🚀 شروع اتوماسیون برای {sel_count} قسمت انتخاب شده ➡️",
                        callback_data="sauto:ep_proceed"
                    )
                )
        else:
            builder.row(
                InlineKeyboardButton(
                    text=f"✅ اجرای اتوماسیون روی این پوشه ({smart_middle_truncate(current_dir.name, 24)})",
                    callback_data="sauto:select_dir"
                )
            )

    total_folders = len(folders)
    total_pages = max(1, math.ceil(total_folders / BROWSER_PAGE_SIZE))
    page = max(0, min(page, total_pages - 1))
    start_idx = page * BROWSER_PAGE_SIZE
    end_idx = start_idx + BROWSER_PAGE_SIZE

    for idx in range(start_idx, min(end_idx, total_folders)):
        folder = folders[idx]
        builder.row(
            InlineKeyboardButton(
                text=f"📁 [{idx+1}] {smart_middle_truncate(folder.name, 38)}",
                callback_data=f"sauto:open:{idx}"
            )
        )

    _append_pagination_row(builder, "sauto:page", page, total_pages)

    if not is_root:
        builder.row(
            InlineKeyboardButton(
                text="⬆️ بازگشت به پوشه قبلی (Up)",
                callback_data="sauto:up"
            )
        )

    builder.row(
        InlineKeyboardButton(
            text="🔙 انصراف و منوی اصلی",
            callback_data="menu:home"
        )
    )
    return builder.as_markup()


def smart_auto_sub_name_kb(suggested_name: str, original_name: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text=f"✅ نام استاندارد: {smart_middle_truncate(suggested_name, 32)}",
            callback_data="sauto_subname:standard"
        )
    )
    builder.row(
        InlineKeyboardButton(
            text=f"📄 حفظ نام فعلی: {smart_middle_truncate(original_name, 32)}",
            callback_data="sauto_subname:keep"
        )
    )
    builder.row(
        InlineKeyboardButton(
            text="🔙 انصراف",
            callback_data="menu:home"
        )
    )
    return builder.as_markup()


def smart_auto_missing_en_kb(has_embedded_en: bool) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    if has_embedded_en:
        builder.row(
            InlineKeyboardButton(
                text="✅ بله، خودکار از فیلم استخراج و در پوشه srt قرار بده",
                callback_data="sauto_en:auto_extract"
            )
        )
    builder.row(
        InlineKeyboardButton(
            text="📤 آپلود دستی فایل زیرنویس انگلیسی (.srt / .zip)",
            callback_data="sauto_en:upload"
        )
    )
    builder.row(
        InlineKeyboardButton(
            text="⏭ ادامه بدون زیرنویس انگلیسی",
            callback_data="sauto_en:skip"
        )
    )
    builder.row(
        InlineKeyboardButton(
            text="🔙 انصراف",
            callback_data="menu:home"
        )
    )
    return builder.as_markup()


def smart_auto_final_selection_kb(
    available_softsub_qualities: list[str],
    selected_softsub_qualities: set[str],
    selected_hardsub_srt: set[str],
    auto_upload: bool = True
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()

    if available_softsub_qualities:
        builder.row(
            InlineKeyboardButton(
                text="━━ 📎 ۱. انتخاب پوشه‌های کیفیت خام برای SoftSub ━━",
                callback_data="qnoop"
            )
        )
        soft_btns = []
        for idx, q_name in enumerate(available_softsub_qualities[:24]):
            mark = "✅" if q_name in selected_softsub_qualities else "⬜"
            soft_btns.append(
                InlineKeyboardButton(
                    text=f"{mark} {smart_middle_truncate(q_name, 22)}",
                    callback_data=f"sauto_hbq:toggle_soft:{idx}"
                )
            )
        for i in range(0, len(soft_btns), 2):
            builder.row(*soft_btns[i:i + 2])

        builder.row(
            InlineKeyboardButton(
                text="☑️ انتخاب / لغو انتخاب همه کیفیت‌های سافت‌ساب",
                callback_data="sauto_hbq:all_soft"
            )
        )

    builder.row(
        InlineKeyboardButton(
            text="━━ 🔥 ۲. انتخاب کیفیت‌های HardSub (از FullHD) ━━",
            callback_data="qnoop"
        )
    )
    hb_btns = []
    for q in ("1080p", "720p", "480p"):
        mark = "✅" if q in selected_hardsub_srt else "⬜"
        hb_btns.append(
            InlineKeyboardButton(
                text=f"{mark} {q}-HardSub",
                callback_data=f"sauto_hbq:toggle_srt:{q}"
            )
        )
    builder.row(*hb_btns)

    builder.row(
        InlineKeyboardButton(
            text="🔥 هر ۳ هاردساب",
            callback_data="sauto_hbq:all_srt"
        ),
        InlineKeyboardButton(
            text="🧹 بدون هاردساب (فقط سافت‌ساب)",
            callback_data="sauto_hbq:no_srt"
        )
    )

    up_mark = "🟢 فعال" if auto_upload else "🔴 غیرفعال"
    builder.row(
        InlineKeyboardButton(
            text=f"☁️ آپلود خودکار در سرور پس از اتمام هر فایل: {up_mark}",
            callback_data="sauto_hbq:toggle_upload"
        )
    )

    builder.row(
        InlineKeyboardButton(
            text=f"🚀 شروع اتوماسیون ({len(selected_softsub_qualities)} سافت‌ساب + {len(selected_hardsub_srt)} هاردساب)",
            callback_data="sauto_hbq:launch"
        )
    )
    builder.row(
        InlineKeyboardButton(
            text="🔙 انصراف",
            callback_data="menu:home"
        )
    )
    return builder.as_markup()


def manual_encode_ass_qualities_kb(
    selected_qualities: set[str],
    auto_upload: bool = True
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text="━━ 🎬 انتخاب کیفیت‌های انکود با movie.ass ━━",
            callback_data="qnoop"
        )
    )
    btns = []
    for q in ("1080p", "720p", "480p"):
        mark = "✅" if q in selected_qualities else "⬜"
        btns.append(
            InlineKeyboardButton(
                text=f"{mark} {q}-EBTV",
                callback_data=f"menc_q:toggle:{q}"
            )
        )
    builder.row(*btns)

    builder.row(
        InlineKeyboardButton(
            text="☑️ انتخاب هر ۳ کیفیت (1080p + 720p + 480p)",
            callback_data="menc_q:all"
        )
    )

    up_mark = "🟢 فعال" if auto_upload else "🔴 غیرفعال"
    builder.row(
        InlineKeyboardButton(
            text=f"☁️ آپلود خودکار به سرور پس از اتمام انکود: {up_mark}",
            callback_data="menc_q:toggle_upload"
        )
    )

    builder.row(
        InlineKeyboardButton(
            text=f"🚀 شروع انکود با movie.ass ({len(selected_qualities)} کیفیت)",
            callback_data="menc_q:launch"
        )
    )
    builder.row(
        InlineKeyboardButton(
            text="🔙 انصراف",
            callback_data="menu:home"
        )
    )
    return builder.as_markup()


def download_folder_tree_browser_kb(
    subfolders: list[Path],
    current_dir: Path,
    is_root: bool,
    page: int = 0
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()

    builder.row(
        InlineKeyboardButton(
            text=f"📥 دانلود در همین پوشه فعلی ({smart_middle_truncate(current_dir.name, 24)})",
            callback_data="dlnav:select_here"
        )
    )

    total_folders = len(subfolders)
    total_pages = max(1, math.ceil(total_folders / BROWSER_PAGE_SIZE))
    page = max(0, min(page, total_pages - 1))
    start_idx = page * BROWSER_PAGE_SIZE
    end_idx = start_idx + BROWSER_PAGE_SIZE

    for idx in range(start_idx, min(end_idx, total_folders)):
        folder = subfolders[idx]
        builder.row(
            InlineKeyboardButton(
                text=f"📁 [{idx+1}] باز کردن: {smart_middle_truncate(folder.name, 34)}",
                callback_data=f"dlnav:open:{idx}"
            )
        )

    _append_pagination_row(builder, "dlnav:page", page, total_pages)

    builder.row(
        InlineKeyboardButton(
            text="➕ ساخت پوشه جدید در همین مسیر",
            callback_data="dlnav:mkdir_here"
        )
    )

    if not is_root:
        builder.row(
            InlineKeyboardButton(
                text="⬆️ بازگشت به پوشه قبلی (Up)",
                callback_data="dlnav:up"
            )
        )

    builder.row(
        InlineKeyboardButton(
            text="🔙 انصراف و منوی اصلی",
            callback_data="menu:home"
        )
    )
    return builder.as_markup()


def extract_sub_entry_kb() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text="📂 ۱. استخراج زیرنویس از فایل موجود در دایرکتوری (Workspace)",
            callback_data="extmode:local"
        )
    )
    builder.row(
        InlineKeyboardButton(
            text="🔗 ۲. ارسال لینک دانلود فیلم و استخراج خودکار زیرنویس",
            callback_data="extmode:link"
        )
    )
    builder.row(
        InlineKeyboardButton(
            text="🔙 بازگشت به منوی اصلی",
            callback_data="menu:home"
        )
    )
    return builder.as_markup()


def local_extract_browser_kb(
    folders: list[Path],
    videos: list[Path],
    is_root: bool,
    page: int = 0
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()

    combined: list[tuple[str, int, Path]] = (
        [("dir", i, f) for i, f in enumerate(folders)]
        + [("vid", i, v) for i, v in enumerate(videos)]
    )
    total_items = len(combined)
    total_pages = max(1, math.ceil(total_items / BROWSER_PAGE_SIZE))
    page = max(0, min(page, total_pages - 1))
    start_idx = page * BROWSER_PAGE_SIZE
    end_idx = start_idx + BROWSER_PAGE_SIZE

    for kind, orig_idx, path_obj in combined[start_idx:end_idx]:
        if kind == "dir":
            builder.row(
                InlineKeyboardButton(
                    text=f"📁 [{orig_idx+1}] {smart_middle_truncate(path_obj.name, 38)}",
                    callback_data=f"extnav:dir:{orig_idx}"
                )
            )
        else:
            builder.row(
                InlineKeyboardButton(
                    text=f"🎬 [F{orig_idx+1}] {smart_middle_truncate(path_obj.name, 36)}",
                    callback_data=f"extnav:vid:{orig_idx}"
                )
            )

    _append_pagination_row(builder, "extnav:page", page, total_pages)

    if not is_root:
        builder.row(
            InlineKeyboardButton(
                text="⬆️ بازگشت به پوشه قبلی (Up)",
                callback_data="extnav:up"
            )
        )

    builder.row(
        InlineKeyboardButton(
            text="🔙 بازگشت",
            callback_data="menu:link_extract_sub"
        )
    )
    return builder.as_markup()


def monitor_menu_kb(is_enabled: bool) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()

    status_label = (
        "🟢 مانیتورینگ کلی: فعال (توقف)"
        if is_enabled
        else "🔴 مانیتورینگ کلی: غیرفعال (شروع)"
    )

    builder.row(
        InlineKeyboardButton(
            text=status_label,
            callback_data="mon:toggle"
        )
    )

    builder.row(
        InlineKeyboardButton(
            text="⚙️ مدیریت لینک جدید و فعال/غیرفعال کردن تک‌تک سایت‌ها",
            callback_data="cfgsec:monitor"
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
            text="🔄 بررسی فوری سایت‌های فعال",
            callback_data="mon:check_now"
        )
    )

    builder.row(
        InlineKeyboardButton(text="Pahe", callback_data="mon:latest:pahe"),
        InlineKeyboardButton(text="PSA", callback_data="mon:latest:psa"),
        InlineKeyboardButton(text="YTS", callback_data="mon:latest:yts"),
    )
    builder.row(
        InlineKeyboardButton(text="Digimoviez", callback_data="mon:latest:digimovie"),
        InlineKeyboardButton(text="AvaMovie", callback_data="mon:latest:avamovie"),
    )

    builder.row(
        InlineKeyboardButton(
            text="🔙 منوی اصلی",
            callback_data="menu:home"
        )
    )

    return builder.as_markup()


def folder_structure_choice_kb() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()

    builder.row(
        InlineKeyboardButton(
            text="🎬 فیلم سینمایی در Movies/ (SoftSub / Hardsub / srt)",
            callback_data="mktype:movie"
        )
    )

    builder.row(
        InlineKeyboardButton(
            text="📺 سریال در Series/ (پوشه‌های کیفیت از سرور)",
            callback_data="mktype:series"
        )
    )

    builder.row(
        InlineKeyboardButton(
            text="🎌 انیمه در Anime/ (پوشه‌های کیفیت از سرور)",
            callback_data="mktype:anime"
        )
    )

    builder.row(
        InlineKeyboardButton(
            text="📁 فقط پوشه ساده در همین مسیر",
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
    base_workspace: Path,
    page: int = 0
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()

    total_folders = len(folders)
    total_pages = max(1, math.ceil(total_folders / BROWSER_PAGE_SIZE))
    page = max(0, min(page, total_pages - 1))
    start_idx = page * BROWSER_PAGE_SIZE
    end_idx = start_idx + BROWSER_PAGE_SIZE

    for idx in range(start_idx, min(end_idx, total_folders)):
        folder = folders[idx]
        try:
            rel_name = folder.relative_to(base_workspace).as_posix()
        except ValueError:
            rel_name = folder.name

        builder.row(
            InlineKeyboardButton(
                text=f"📁 [{idx+1}] {smart_middle_truncate(rel_name, 38)}",
                callback_data=f"tsubdir:{idx}"
            )
        )

    _append_pagination_row(builder, "tsubpage", page, total_pages)

    builder.row(
        InlineKeyboardButton(
            text="🔙 منوی اصلی",
            callback_data="menu:home"
        )
    )

    return builder.as_markup()


def cuteftp_destination_kb(active_gui_path: str = "/") -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()

    builder.row(
        InlineKeyboardButton(
            text=f"📍 مسیر فعلی CuteFTP ({smart_middle_truncate(active_gui_path, 22)})",
            callback_data="cftp_dest:current"
        )
    )

    destinations = CuteFTPService.get_saved_destinations()
    for idx, item in enumerate(destinations[:15]):
        builder.row(
            InlineKeyboardButton(
                text=item["label"][:50],
                callback_data=f"cftp_dest:preset:{idx}"
            )
        )

    builder.row(
        InlineKeyboardButton(
            text="✏️ تایپ دستی مسیر مقصد در هاست (مثلاً /English/Series/Severance)",
            callback_data="cftp_dest:custom"
        )
    )
    builder.row(
        InlineKeyboardButton(
            text="➕ افزودن مسیر جدید به لیست ثابت",
            callback_data="cftp_dest:add_fav"
        )
    )
    builder.row(
        InlineKeyboardButton(
            text="🔙 بازگشت به فایل‌منیجر",
            callback_data="menu:file_mgr"
        )
    )
    return builder.as_markup()


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

    sites = ScraperService.SITE_CATEGORIES.get(category, {})

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
        icon = "🧲" if item.get("type") == "magnet" else "📄"
        title = str(item.get("title", "بدون عنوان"))

        builder.row(
            InlineKeyboardButton(
                text=f"{icon} [{idx+1}] {smart_middle_truncate(title, 38)}",
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


def file_browser_kb(
    items: list[Path],
    is_root: bool,
    selected_files: set[str] | None = None,
    page: int = 0
) -> InlineKeyboardMarkup:
    selected_files = selected_files or set()
    builder = InlineKeyboardBuilder()

    has_files_in_dir = any(p.is_file() for p in items)

    if not is_root:
        builder.row(
            InlineKeyboardButton(
                text="🚀 اجرای اتوماسیون هوشمند روی همین پوشه",
                callback_data="fact:run_smart_auto_here"
            )
        )
        builder.row(
            InlineKeyboardButton(
                text="☁️ آپلود هوشمند یک قسمت (SxxEyy) در پوشه‌های متناظر سرور",
                callback_data="fact:smart_ep_upload"
            )
        )

    total_items = len(items)
    total_pages = max(1, math.ceil(total_items / BROWSER_PAGE_SIZE))
    page = max(0, min(page, total_pages - 1))
    start_idx = page * BROWSER_PAGE_SIZE
    end_idx = start_idx + BROWSER_PAGE_SIZE

    for idx in range(start_idx, min(end_idx, total_items)):
        path = items[idx]
        if path.is_dir():
            builder.row(
                InlineKeyboardButton(
                    text=f"📁 [{idx+1}] {smart_middle_truncate(path.name, 38)}",
                    callback_data=f"fitem:{idx}"
                )
            )
        else:
            is_sel = str(path.resolve()) in selected_files
            mark = "✅" if is_sel else "⬜"
            ext_low = path.suffix.lower()
            if ext_low in [".mkv", ".mp4", ".avi", ".ts", ".mov", ".m4v", ".webm"]:
                icon = "🎬"
            elif ext_low in (".zip", ".rar", ".7z"):
                icon = "📦"
            else:
                icon = "📄"

            builder.row(
                InlineKeyboardButton(
                    text=f"{mark} {icon} [{idx+1}] {smart_middle_truncate(path.name, 34)}",
                    callback_data=f"fitem:{idx}"
                )
            )

    _append_pagination_row(builder, "fpage", page, total_pages)

    if has_files_in_dir:
        builder.row(
            InlineKeyboardButton(
                text="☑️ انتخاب / لغو انتخاب همه فایل‌های این پوشه",
                callback_data="fact:select_all_files"
            )
        )

    sel_count = len(selected_files)
    if sel_count > 0:
        archive_selected_count = sum(
            1 for p in selected_files if Path(p).suffix.lower() in (".zip", ".rar", ".7z")
        )
        if archive_selected_count > 0:
            builder.row(
                InlineKeyboardButton(
                    text=f"📦 اکسترکت {archive_selected_count} فایل فشرده (ZIP/RAR) در همین پوشه",
                    callback_data="fact:extract_zip"
                )
            )

        builder.row(
            InlineKeyboardButton(
                text=f"✏️ تغییر نام ({sel_count} فایل)",
                callback_data="fact:batch_rename"
            ),
            InlineKeyboardButton(
                text=f"🗑 حذف ({sel_count} فایل)",
                callback_data="fact:batch_delete"
            )
        )
        builder.row(
            InlineKeyboardButton(
                text=f"🎬 سافت‌ساب / هاردساب / انکود دستی ({sel_count} فایل)",
                callback_data="fact:batch_sub"
            )
        )
        video_selected_count = sum(
            1 for p in selected_files
            if Path(p).suffix.lower() in [".mkv", ".mp4", ".avi", ".ts", ".mov", ".m4v", ".webm"]
        )
        if video_selected_count == 1:
            builder.row(
                InlineKeyboardButton(
                    text="📤 استخراج زیرنویس از این فیلم",
                    callback_data="vact:extract_sub"
                )
            )
        builder.row(
            InlineKeyboardButton(
                text=f"☁️ آپلود {sel_count} فایل انتخابی با CuteFTP",
                callback_data="fact:upload_selected_files"
            )
        )
        builder.row(
            InlineKeyboardButton(
                text="✖️ پاک کردن انتخاب‌ها",
                callback_data="fact:clear_selection"
            )
        )

    if not is_root:
        builder.row(
            InlineKeyboardButton(
                text="⬆️ پوشه قبلی (Up)",
                callback_data="fnav:up"
            )
        )
        builder.row(
            InlineKeyboardButton(
                text="✏️ تغییر نام این پوشه",
                callback_data="fact:rename_dir"
            ),
            InlineKeyboardButton(
                text="🗑 حذف کل این پوشه",
                callback_data="fact:delete_dir"
            )
        )

    builder.row(
        InlineKeyboardButton(
            text="📁 ساخت پوشه جدید",
            callback_data="fact:mkdir_here"
        ),
        InlineKeyboardButton(
            text="📥 آپلود زیرنویس / ZIP / RAR",
            callback_data="fact:upload_sub_here"
        )
    )

    builder.row(
        InlineKeyboardButton(
            text="☁️ آپلود کل این پوشه با CuteFTP (انتخاب مقصد)",
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


def subtitle_folder_browser_kb(
    folders: list[Path],
    videos: list[Path],
    selected_paths: set[str],
    is_root: bool,
    page: int = 0
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()

    combined: list[tuple[str, int, Path]] = (
        [("dir", i, f) for i, f in enumerate(folders)]
        + [("vid", i, v) for i, v in enumerate(videos)]
    )
    total_items = len(combined)
    total_pages = max(1, math.ceil(total_items / BROWSER_PAGE_SIZE))
    page = max(0, min(page, total_pages - 1))
    start_idx = page * BROWSER_PAGE_SIZE
    end_idx = start_idx + BROWSER_PAGE_SIZE

    for kind, orig_idx, path_obj in combined[start_idx:end_idx]:
        if kind == "dir":
            builder.row(
                InlineKeyboardButton(
                    text=f"📁 [{orig_idx+1}] {smart_middle_truncate(path_obj.name, 38)}",
                    callback_data=f"subnav:dir:{orig_idx}"
                )
            )
        else:
            is_sel = str(path_obj.resolve()) in selected_paths
            mark = "✅" if is_sel else "⬜"
            builder.row(
                InlineKeyboardButton(
                    text=f"{mark} 🎬 [F{orig_idx+1}] {smart_middle_truncate(path_obj.name, 35)}",
                    callback_data=f"subnav:vid:{orig_idx}"
                )
            )

    _append_pagination_row(builder, "subnav:page", page, total_pages)

    if videos:
        builder.row(
            InlineKeyboardButton(
                text="☑️ انتخاب / لغو انتخاب همه فیلم‌های این پوشه",
                callback_data="subnav:sel_all"
            )
        )

    if not is_root:
        builder.row(
            InlineKeyboardButton(
                text="⬆️ بازگشت به پوشه قبلی (Up)",
                callback_data="subnav:up"
            )
        )

    count = len(selected_paths)
    builder.row(
        InlineKeyboardButton(
            text=f"➡️ ادامه و انتخاب نوع عملیات ({count} فایل انتخاب شده)",
            callback_data="mvact:proceed"
        )
    )
    builder.row(
        InlineKeyboardButton(
            text="🔙 انصراف و منوی اصلی",
            callback_data="menu:home"
        )
    )
    return builder.as_markup()


def queue_management_kb(
    page_tasks: list[Any],
    page: int = 0,
    total_pages: int = 1
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()

    builder.row(
        InlineKeyboardButton(
            text="🔄 بروزرسانی لحظه‌ای",
            callback_data=f"qpage:{page}"
        ),
        InlineKeyboardButton(
            text="🧹 پاکسازی تمام‌شده‌ها",
            callback_data=f"qact:clear_finished:{page}"
        ),
    )

    if total_pages > 1:
        nav_buttons = []
        if page > 0:
            nav_buttons.append(
                InlineKeyboardButton(text="⬅️ قبلی", callback_data=f"qpage:{page - 1}")
            )
        nav_buttons.append(
            InlineKeyboardButton(text=f"📄 صفحه {page + 1} از {total_pages}", callback_data="qnoop")
        )
        if page < total_pages - 1:
            nav_buttons.append(
                InlineKeyboardButton(text="بعدی ➡️", callback_data=f"qpage:{page + 1}")
            )
        builder.row(*nav_buttons)

    for t in page_tasks:
        status_str = str(getattr(t, "status", ""))
        is_done = getattr(
            t,
            "is_finished",
            any(k in status_str for k in ("تکمیل", "خطا", "لغو"))
        )
        is_pending = "انتظار" in status_str
        task_id = t.task_id
        task_title = smart_middle_truncate(getattr(t, "title", "بدون عنوان"), 34)

        if is_done:
            builder.row(
                InlineKeyboardButton(
                    text=f"✅ #{task_id} · {task_title}",
                    callback_data="qnoop"
                )
            )
            builder.row(
                InlineKeyboardButton(
                    text="🗑 حذف از فهرست",
                    callback_data=f"qdel:{task_id}:{page}"
                )
            )
        elif is_pending:
            builder.row(
                InlineKeyboardButton(
                    text=f"⏳ #{task_id} · {task_title}",
                    callback_data="qnoop"
                )
            )
            builder.row(
                InlineKeyboardButton(
                    text="⏫ انتقال به ابتدا",
                    callback_data=f"qprio:top:{task_id}:{page}"
                ),
                InlineKeyboardButton(
                    text="✖️ لغو",
                    callback_data=f"qcancel:{task_id}:{page}"
                )
            )
            builder.row(
                InlineKeyboardButton(
                    text="⬆️ بالاتر",
                    callback_data=f"qprio:up:{task_id}:{page}"
                ),
                InlineKeyboardButton(
                    text="⬇️ پایین‌تر",
                    callback_data=f"qprio:down:{task_id}:{page}"
                )
            )
        else:
            builder.row(
                InlineKeyboardButton(
                    text=f"⚙️ در حال اجرا · #{task_id} · {task_title}",
                    callback_data="qnoop"
                )
            )
            builder.row(
                InlineKeyboardButton(
                    text="🛑 توقف پردازش",
                    callback_data=f"qcancel:{task_id}:{page}"
                )
            )

    builder.row(
        InlineKeyboardButton(
            text="🔙 منوی اصلی",
            callback_data="menu:home"
        )
    )
    return builder.as_markup()


def subtitle_source_kb(
    language: str,
    local_subs_count: int = 0
) -> InlineKeyboardMarkup:
    language_name = "فارسی" if language == "fa" else "انگلیسی"
    builder = InlineKeyboardBuilder()

    count_badge = f" ({local_subs_count} فایل یافت شد)" if local_subs_count > 0 else ""
    builder.row(
        InlineKeyboardButton(
            text=f"📂 انتخاب زیرنویس {language_name} از پوشه srt / فیلم{count_badge}",
            callback_data=f"subscan:{language}"
        )
    )

    builder.row(
        InlineKeyboardButton(
            text=f"📤 آپلود فایل زیرنویس {language_name} جدید (.srt / .ass / .zip / .rar)",
            callback_data=f"subupload:{language}"
        )
    )

    builder.row(
        InlineKeyboardButton(
            text=f"⏭ بدون زیرنویس {language_name} (رد کردن)",
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
        folder_hint = f"[{path.parent.name}] " if path.parent.name.lower() in ("sub", "srt", "fa", "en") else ""
        builder.row(
            InlineKeyboardButton(
                text=f"📄 [{idx+1}] {folder_hint}{smart_middle_truncate(path.name, 34)}",
                callback_data=f"subfile:{language}:{idx}"
            )
        )

    builder.row(
        InlineKeyboardButton(
            text="📤 آپلود یک فایل زیرنویس دیگر (.srt / .ass / .zip / .rar)",
            callback_data=f"subupload:{language}"
        )
    )

    builder.row(
        InlineKeyboardButton(
            text="⏭ رد شدن از این زبان",
            callback_data=f"skip:{language}_sub"
        )
    )

    return builder.as_markup()


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


def subtitle_mode_kb() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()

    builder.row(
        InlineKeyboardButton(
            text="🎬 انکود با movie.ass (در پوشه EBTV - بدون نیاز به زیرنویس)",
            callback_data="submode:encode_ass"
        )
    )

    builder.row(
        InlineKeyboardButton(
            text="📎 فقط SoftSub (نیازمند زیرنویس)",
            callback_data="submode:softsub"
        )
    )

    builder.row(
        InlineKeyboardButton(
            text="🔥 فقط HardSub فارسی (نیازمند زیرنویس)",
            callback_data="submode:hardsub"
        )
    )

    builder.row(
        InlineKeyboardButton(
            text="🚀 هر دو عملیات (SoftSub + HardSub فارسی)",
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


def output_destination_choice_kb() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text="📂 حالت پیش‌فرض (ذخیره در پوشه استاندارد SoftSub/HardSub)",
            callback_data="outdest:default"
        )
    )
    builder.row(
        InlineKeyboardButton(
            text="📍 ذخیره مستقیم کنار خود فایل فیلم (بدون ساخت ۳ پوشه)",
            callback_data="outdest:same_dir"
        )
    )
    builder.row(
        InlineKeyboardButton(
            text="📁 انتخاب پوشه مقصد دلخواه (مرور زیرپوشه‌ها / ساخت پوشه)",
            callback_data="outdest:browse"
        )
    )
    builder.row(
        InlineKeyboardButton(
            text="🔙 انصراف",
            callback_data="menu:home"
        )
    )
    return builder.as_markup()


hardsub_destination_choice_kb = output_destination_choice_kb


def output_dest_tree_browser_kb(
    subfolders: list[Path],
    current_dir: Path,
    is_root: bool,
    page: int = 0
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()

    builder.row(
        InlineKeyboardButton(
            text=f"✅ ذخیره در همین پوشه ({smart_middle_truncate(current_dir.name, 26)})",
            callback_data="destnav:select_here"
        )
    )

    total_folders = len(subfolders)
    total_pages = max(1, math.ceil(total_folders / BROWSER_PAGE_SIZE))
    page = max(0, min(page, total_pages - 1))
    start_idx = page * BROWSER_PAGE_SIZE
    end_idx = start_idx + BROWSER_PAGE_SIZE

    for idx in range(start_idx, min(end_idx, total_folders)):
        folder = subfolders[idx]
        builder.row(
            InlineKeyboardButton(
                text=f"📁 [{idx+1}] باز کردن: {smart_middle_truncate(folder.name, 34)}",
                callback_data=f"destnav:open:{idx}"
            )
        )

    _append_pagination_row(builder, "destnav:page", page, total_pages)

    builder.row(
        InlineKeyboardButton(
            text="➕ ساخت پوشه جدید در همین مسیر",
            callback_data="destnav:mkdir_here"
        )
    )

    if not is_root:
        builder.row(
            InlineKeyboardButton(
                text="⬆️ بازگشت به پوشه قبلی (Up)",
                callback_data="destnav:up"
            )
        )

    builder.row(
        InlineKeyboardButton(
            text="🔙 انصراف و منوی اصلی",
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


def video_actions_kb(is_video: bool = True, is_zip: bool = False) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()

    if is_zip:
        builder.row(
            InlineKeyboardButton(
                text="📦 اکسترکت این فایل فشرده در همین پوشه",
                callback_data="fact:extract_zip"
            )
        )

    if is_video:
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
            ),
            InlineKeyboardButton(
                text="🔥 HardSub فارسی",
                callback_data="vact:hardsub"
            )
        )

        builder.row(
            InlineKeyboardButton(
                text="🎬 انکود با movie.ass (پوشه EBTV - بدون زیرنویس)",
                callback_data="vact:encode_ass"
            )
        )

        builder.row(
            InlineKeyboardButton(
                text="🚀 اتوماسیون کامل (SoftSub + HardSub)",
                callback_data="vact:full_auto"
            )
        )

    builder.row(
        InlineKeyboardButton(
            text="✏️ تغییر نام این فایل",
            callback_data="vact:rename_file"
        ),
        InlineKeyboardButton(
            text="🗑 حذف این فایل",
            callback_data="vact:delete_file"
        )
    )

    builder.row(
        InlineKeyboardButton(
            text="☁️ آپلود این فایل با CuteFTP (انتخاب مقصد)",
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