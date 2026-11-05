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


def smart_middle_truncate(text: str, max_len: int = 40) -> str:
    text = str(text).strip()
    if len(text) <= max_len:
        return text
    head_len = (max_len - 1) // 2 + 2
    tail_len = max_len - head_len - 1
    return f"{text[:head_len]}…{text[-tail_len:]}"


def main_menu_kb() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()

    # دکمه ویژه اتوماسیون کامل ۱-کلیکی فیلم و سریال در بالاترین بخش منو
    builder.row(
        InlineKeyboardButton(
            text="🚀 اتوماسیون کامل و هوشمند (فیلم / سریال SxxEyy)",
            callback_data="menu:smart_auto"
        )
    )

    builder.row(
        InlineKeyboardButton(
            text="📡 مانیتورینگ خودکار (۵ سایت فیلم و سریال)",
            callback_data="menu:monitor"
        )
    )

    builder.row(
        InlineKeyboardButton(
            text="📁 ساخت پوشه (فیلم / سریال)",
            callback_data="menu:create_folder"
        ),
        InlineKeyboardButton(
            text="📥 آپلود زیرنویس (SRT/ASS/ZIP)",
            callback_data="menu:upload_sub_target"
        )
    )

    builder.row(
        InlineKeyboardButton(
            text="🔍 ۱. جستجو در سایت‌ها",
            callback_data="menu:search"
        ),
        InlineKeyboardButton(
            text="📥 ۲. دانلود لینک / تورنت",
            callback_data="menu:direct_dl"
        )
    )

    builder.row(
        InlineKeyboardButton(
            text="📤 ۳. استخراج زیرنویس (از پوشه / لینک)",
            callback_data="menu:link_extract_sub"
        ),
        InlineKeyboardButton(
            text="🎬 ۴. سافت‌ساب/هاردساب دستی",
            callback_data="menu:sub_to_files"
        )
    )

    builder.row(
        InlineKeyboardButton(
            text="📂 ۵. فایل‌منیجر (انتخاب چندتایی / اکسترکت ZIP / CuteFTP)",
            callback_data="menu:file_mgr"
        )
    )

    builder.row(
        InlineKeyboardButton(
            text="📊 وضعیت صف و پراسس‌ها",
            callback_data="menu:queues"
        ),
        InlineKeyboardButton(
            text="⚙️ تنظیمات کانفیگ (/settings)",
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
    selected_episodes: set[str] | None = None
) -> InlineKeyboardMarkup:
    """
    مرورگر انتخاب پوشه فیلم یا انتخاب چندقسمتی (Multi-Episode) فصل سریال برای اتوماسیون کامل
    """
    selected_episodes = selected_episodes or set()
    builder = InlineKeyboardBuilder()

    if not is_root:
        if detected_episodes:
            # نمایش دکمه‌های تیک‌دار برای انتخاب یک یا چند قسمت از فصل
            ep_buttons = []
            for ep in detected_episodes[:24]:
                mark = "✅" if ep in selected_episodes else "⬜"
                ep_buttons.append(
                    InlineKeyboardButton(
                        text=f"{mark} {ep}",
                        callback_data=f"sauto:ep_toggle:{ep}"
                    )
                )
            # چیدمان ۳تایی دکمه‌های قسمت‌ها در هر ردیف برای زیبایی و سرعت بالا
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

    for idx, folder in enumerate(folders[:22]):
        builder.row(
            InlineKeyboardButton(
                text=f"📁 [{idx+1}] {smart_middle_truncate(folder.name, 38)}",
                callback_data=f"sauto:open:{idx}"
            )
        )

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
    """کیبورد انتخاب سریع نام فایل زیرنویس فارسی در پوشه srt/FA یا srt"""
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
    """کیبورد تصمیم‌گیری در صورت نبودن زیرنویس انگلیسی در پوشه srt/EN"""
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


def smart_auto_hardsub_qualities_kb(
    selected_hardsub_srt: set[str],
    selected_encode_ass: set[str] | None = None,
    auto_upload: bool = True
) -> InlineKeyboardMarkup:
    """
    پنل انتخاب چندگزینه‌ای دو نوع هاردساب + سوییچ آپلود خودکار بعد از اتمام:
    ۱. هاردساب فارسی SRT (در پوشه‌های 1080p-HardSub, 720p-HardSub, 480p-HardSub)
    ۲. انکود با movie.ass + سافت‌ساب انگلیسی (در پوشه‌های 1080p-EBTV, 720p-EBTV, 480p-EBTV)
    """
    selected_encode_ass = selected_encode_ass or set()
    builder = InlineKeyboardBuilder()

    # بخش اول: هاردساب زیرنویس فارسی (SRT) -> پوشه X-HardSub
    builder.row(
        InlineKeyboardButton(
            text="━━ 🔥 ۱. هاردساب زیرنویس فارسی (پوشه HardSub) ━━",
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

    # بخش دوم: انکود با movie.ass + سافت‌ساب انگلیسی -> پوشه X-EBTV
    builder.row(
        InlineKeyboardButton(
            text="━━ 🎬 ۲. انکود با movie.ass (پوشه EBTV) ━━",
            callback_data="qnoop"
        )
    )
    enc_btns = []
    for q in ("1080p", "720p", "480p"):
        mark = "✅" if q in selected_encode_ass else "⬜"
        enc_btns.append(
            InlineKeyboardButton(
                text=f"{mark} {q}-EBTV",
                callback_data=f"sauto_hbq:toggle_ass:{q}"
            )
        )
    builder.row(*enc_btns)

    # دکمه‌های انتخاب سریع
    builder.row(
        InlineKeyboardButton(
            text="⚡ هر دو نوع (1080p + 720p)",
            callback_data="sauto_hbq:preset_both_hd"
        ),
        InlineKeyboardButton(
            text="🧹 بدون هاردساب/انکود (فقط سافت‌ساب)",
            callback_data="sauto_hbq:clear_all"
        )
    )

    # سوییچ آپلود خودکار بعد از اتمام هر فایل
    up_mark = "🟢 فعال" if auto_upload else "🔴 غیرفعال"
    builder.row(
        InlineKeyboardButton(
            text=f"☁️ آپلود خودکار در سرور پس از اتمام هر فایل: {up_mark}",
            callback_data="sauto_hbq:toggle_upload"
        )
    )

    total_jobs = len(selected_hardsub_srt) + len(selected_encode_ass)
    builder.row(
        InlineKeyboardButton(
            text=f"🚀 شروع اتوماسیون ({total_jobs} هاردساب/انکود + تمام سافت‌ساب‌ها)",
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
    is_root: bool
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()

    for idx, folder in enumerate(folders[:20]):
        builder.row(
            InlineKeyboardButton(
                text=f"📁 [{idx+1}] {smart_middle_truncate(folder.name, 38)}",
                callback_data=f"extnav:dir:{idx}"
            )
        )

    for idx, vid in enumerate(videos[:25]):
        builder.row(
            InlineKeyboardButton(
                text=f"🎬 [F{idx+1}] {smart_middle_truncate(vid.name, 36)}",
                callback_data=f"extnav:vid:{idx}"
            )
        )

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
            text="📺 سریال در Series/ (همراه با فصل S01 و پوشه‌های استاندارد)",
            callback_data="mktype:series"
        )
    )

    builder.row(
        InlineKeyboardButton(
            text="🎌 انیمه در Anime/ (همراه با فصل S01 و پوشه‌های استاندارد)",
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
            text="🔥 هاردساب فارسی SRT (در پوشه HardSub)",
            callback_data="submode:hardsub"
        )
    )

    builder.row(
        InlineKeyboardButton(
            text="🎬 انکود با movie.ass + سافت‌ساب انگلیسی (در پوشه EBTV)",
            callback_data="submode:encode_ass"
        )
    )

    builder.row(
        InlineKeyboardButton(
            text="🚀 هر دو عملیات (SoftSub + HardSub)",
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

def target_folders_kb(
    folders: list[Path],
    base_workspace: Path
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()

    for idx, folder in enumerate(folders[:25]):
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
    builder = InlineKeyboardBuilder()

    for idx, folder in enumerate(folders[:25]):
        builder.row(
            InlineKeyboardButton(
                text=f"📂 [{idx+1}] {smart_middle_truncate(folder.name, 38)}",
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
    selected_files: set[str] | None = None
) -> InlineKeyboardMarkup:
    selected_files = selected_files or set()
    builder = InlineKeyboardBuilder()

    has_files_in_dir = False

    if not is_root:
        builder.row(
            InlineKeyboardButton(
                text="🚀 اجرای اتوماسیون هوشمند روی همین پوشه",
                callback_data="fact:run_smart_auto_here"
            )
        )

    for idx, path in enumerate(items[:25]):
        if path.is_dir():
            builder.row(
                InlineKeyboardButton(
                    text=f"📁 [{idx+1}] {smart_middle_truncate(path.name, 38)}",
                    callback_data=f"fitem:{idx}"
                )
            )
        else:
            has_files_in_dir = True
            is_sel = str(path.resolve()) in selected_files
            mark = "✅" if is_sel else "⬜"
            ext_low = path.suffix.lower()
            if ext_low in [".mkv", ".mp4", ".avi", ".ts", ".mov", ".m4v", ".webm"]:
                icon = "🎬"
            elif ext_low == ".zip":
                icon = "📦"
            else:
                icon = "📄"

            builder.row(
                InlineKeyboardButton(
                    text=f"{mark} {icon} [{idx+1}] {smart_middle_truncate(path.name, 34)}",
                    callback_data=f"fitem:{idx}"
                )
            )

    if has_files_in_dir:
        builder.row(
            InlineKeyboardButton(
                text="☑️ انتخاب / لغو انتخاب همه فایل‌های این پوشه",
                callback_data="fact:select_all_files"
            )
        )

    sel_count = len(selected_files)
    if sel_count > 0:
        zip_selected_count = sum(1 for p in selected_files if Path(p).suffix.lower() == ".zip")
        if zip_selected_count > 0:
            builder.row(
                InlineKeyboardButton(
                    text=f"📦 اکسترکت {zip_selected_count} فایل ZIP در همین پوشه",
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
                text=f"🎬 سافت‌ساب / هاردساب دستی ({sel_count} فایل)",
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
            text="📥 آپلود زیرنویس / ZIP",
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


def multi_video_select_kb(
    videos: list[Path],
    selected_indices: set[int]
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()

    for idx, video in enumerate(videos[:25]):
        mark = "✅" if idx in selected_indices else "⬜"
        rel_label = f"{video.parent.name}/{video.name}"

        builder.row(
            InlineKeyboardButton(
                text=f"{mark} [{idx+1}] {smart_middle_truncate(rel_label, 36)}",
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


def subtitle_folder_browser_kb(
    folders: list[Path],
    videos: list[Path],
    selected_paths: set[str],
    is_root: bool
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()

    for idx, folder in enumerate(folders[:20]):
        builder.row(
            InlineKeyboardButton(
                text=f"📁 [{idx+1}] {smart_middle_truncate(folder.name, 38)}",
                callback_data=f"subnav:dir:{idx}"
            )
        )

    for idx, vid in enumerate(videos[:25]):
        is_sel = str(vid.resolve()) in selected_paths
        mark = "✅" if is_sel else "⬜"
        builder.row(
            InlineKeyboardButton(
                text=f"{mark} 🎬 [F{idx+1}] {smart_middle_truncate(vid.name, 35)}",
                callback_data=f"subnav:vid:{idx}"
            )
        )

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
            text=f"📎 ادامه به مرحله زیرنویس ({count} فایل انتخاب شده) ➡️",
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
    """
    کیبورد مدیریت صف مجهز به دکمه‌های تغییر اولویت (⏫ صدر صف، ⬆️ بالاتر، ⬇️ پایین‌تر) برای پراسس‌های در انتظار
    """
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

        if is_done:
            builder.row(
                InlineKeyboardButton(
                    text=f"🗑 حذف #{t.task_id} | {smart_middle_truncate(t.title, 28)}",
                    callback_data=f"qdel:{t.task_id}:{page}"
                )
            )
        elif is_pending:
            # برای پراسس‌های در انتظار: دکمه‌های جابجایی اولویت + دکمه لغو
            builder.row(
                InlineKeyboardButton(
                    text=f"⏫ صدر #{t.task_id}",
                    callback_data=f"qprio:top:{t.task_id}:{page}"
                ),
                InlineKeyboardButton(
                    text="⬆️ بالاتر",
                    callback_data=f"qprio:up:{t.task_id}:{page}"
                ),
                InlineKeyboardButton(
                    text="⬇️ پایین‌تر",
                    callback_data=f"qprio:down:{t.task_id}:{page}"
                ),
                InlineKeyboardButton(
                    text="🛑 لغو",
                    callback_data=f"qcancel:{t.task_id}:{page}"
                ),
            )
        else:
            builder.row(
                InlineKeyboardButton(
                    text=f"🛑 توقف در حال اجرا #{t.task_id} | {smart_middle_truncate(t.title, 22)}",
                    callback_data=f"qcancel:{t.task_id}:{page}"
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
            text=f"📤 آپلود فایل زیرنویس {language_name} جدید (.srt / .ass / .zip)",
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
            text="📤 آپلود یک فایل زیرنویس دیگر (.srt / .ass / .zip)",
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
            text="🚀 هر دو عملیات (SoftSub + HardSub)",
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
    is_root: bool
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()

    builder.row(
        InlineKeyboardButton(
            text=f"✅ ذخیره در همین پوشه ({smart_middle_truncate(current_dir.name, 26)})",
            callback_data="destnav:select_here"
        )
    )

    for idx, folder in enumerate(subfolders[:22]):
        builder.row(
            InlineKeyboardButton(
                text=f"📁 [{idx+1}] باز کردن: {smart_middle_truncate(folder.name, 34)}",
                callback_data=f"destnav:open:{idx}"
            )
        )

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
                text="📦 اکسترکت این فایل ZIP در همین پوشه",
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
                text="🔥 HardSub",
                callback_data="vact:hardsub"
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