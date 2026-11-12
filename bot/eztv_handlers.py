import asyncio
import html
import logging
import re
from typing import Any

from aiogram import F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from core.config_loader import cfg
from services.eztv_tracker_service import eztv_tracker_service

router = Router()

PAGE_SIZE = 8

DAY_OPTIONS = [
    ("Monday", "دوشنبه"),
    ("Tuesday", "سه‌شنبه"),
    ("Wednesday", "چهارشنبه"),
    ("Thursday", "پنج‌شنبه"),
    ("Friday", "جمعه"),
    ("Saturday", "شنبه"),
    ("Sunday", "یکشنبه"),
    ("", "نامشخص / متغیر"),
]

QUALITY_LABELS = {
    "fullhd_h264": "FullHD H.264",
    "480p": "480p",
    "2160p": "2160p",
}

DEFAULT_QUALITY_FOLDERS = {
    "fullhd_h264": "FullHD",
    "480p": ["480p-RMTeam", "480p-EBTV"],
    "2160p": "2160p-HDR-x265",
}


class EztvEditStates(StatesGroup):
    title = State()
    aliases = State()
    season = State()
    next_episode = State()
    qualities = State()
    folders = State()


def _is_admin(message: Message) -> bool:
    return bool(
        message.from_user
        and message.from_user.id in (cfg.get("telegram", "admin_ids", []) or [])
    )


def _is_admin_callback(call: CallbackQuery) -> bool:
    return bool(
        call.from_user
        and call.from_user.id in (cfg.get("telegram", "admin_ids", []) or [])
    )


def _split_html_lines(text: str, limit: int = 3800) -> list[str]:
    chunks: list[str] = []
    current = ""
    for line in text.splitlines():
        candidate = current + ("\n" if current else "") + line
        if len(candidate) > limit and current:
            chunks.append(current)
            current = line
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks or ["گزارشی برای نمایش وجود ندارد."]


def _show_title(show: dict[str, Any]) -> str:
    return str(show.get("title", "بدون عنوان")).strip() or "بدون عنوان"


def _quality_text(qualities: list[str]) -> str:
    return "، ".join(QUALITY_LABELS.get(q, q) for q in qualities) or "بدون کیفیت"


def _day_text(day: str | None) -> str:
    return eztv_tracker_service.weekday_label(day)


def _folder_text(folders: dict[str, Any]) -> str:
    parts = []
    for key in ("fullhd_h264", "480p", "2160p"):
        if key not in folders:
            continue
        value = folders[key]
        if isinstance(value, list):
            value = "، ".join(str(x) for x in value)
        parts.append(f"{QUALITY_LABELS.get(key, key)}: {value}")
    return "\n".join(parts) if parts else "پوشه پیش‌فرض"


def _cancel_keyboard():
    builder = InlineKeyboardBuilder()
    builder.button(text="✖️ انصراف", callback_data="eztv:cancel")
    return builder.as_markup()


def _list_keyboard(entries: list[dict[str, Any]], page: int):
    builder = InlineKeyboardBuilder()
    start = page * PAGE_SIZE
    end = min(len(entries), start + PAGE_SIZE)

    for index in range(start, end):
        show = entries[index]
        title = _show_title(show)
        try:
            season = int(show.get("season", 0))
            episode = int(show.get("next_episode", 1))
            episode_tag = f"S{season:02d}E{episode:02d}"
        except (TypeError, ValueError):
            episode_tag = "قسمت نامشخص"
        builder.row(
            InlineKeyboardButton(
                text=f"🎬 {index + 1}. {title[:34]} — {episode_tag}",
                callback_data=f"eztv:open:{index}",
            )
        )

    nav_buttons = []
    if page > 0:
        nav_buttons.append(
            InlineKeyboardButton(text="⬅️ قبلی", callback_data=f"eztv:page:{page - 1}")
        )
    if end < len(entries):
        nav_buttons.append(
            InlineKeyboardButton(text="بعدی ➡️", callback_data=f"eztv:page:{page + 1}")
        )
    if nav_buttons:
        builder.row(*nav_buttons)

    builder.row(
        InlineKeyboardButton(text="➕ افزودن سریال", callback_data="eztv:add"),
        InlineKeyboardButton(text="🔄 تازه‌سازی", callback_data=f"eztv:page:{page}"),
    )
    return builder.as_markup()


async def _render_list(target: Message, page: int = 0, edit: bool = False):
    entries = eztv_tracker_service.get_show_entries()
    total_pages = max(1, (len(entries) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))

    if entries:
        lines = [
            "📡 <b>مدیریت سریال‌ها و فیلم‌های EZTV</b>",
            f"تعداد موارد ثبت‌شده: <b>{len(entries)}</b>",
            "برای مشاهده جزئیات، روی عنوان موردنظر بزنید.",
            "",
        ]
        start = page * PAGE_SIZE
        for index, show in enumerate(entries[start:start + PAGE_SIZE], start=start + 1):
            try:
                tag = f"S{int(show.get('season', 0)):02d}E{int(show.get('next_episode', 1)):02d}"
            except (TypeError, ValueError):
                tag = "نامشخص"
            lines.append(
                f"{index}. <b>{html.escape(_show_title(show))}</b> "
                f"— <code>{tag}</code> — "
                f"{html.escape(_quality_text(show.get('qualities', [])))}"
            )
        lines.append(f"\nصفحه {page + 1} از {total_pages}")
    else:
        lines = [
            "📡 <b>مدیریت سریال‌ها و فیلم‌های EZTV</b>",
            "هنوز هیچ سریالی ثبت نشده است.",
            "برای شروع، روی «افزودن سریال» بزنید.",
        ]

    text = "\n".join(lines)
    markup = _list_keyboard(entries, page)
    if edit:
        await target.edit_text(
            text,
            reply_markup=markup,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
    else:
        await target.answer(
            text,
            reply_markup=markup,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )


async def _render_detail(target: Message, index: int, edit: bool = True):
    entries = eztv_tracker_service.get_show_entries()
    if index < 0 or index >= len(entries):
        if edit:
            return await target.edit_text("⚠️ این مورد دیگر در فهرست وجود ندارد.")
        return await target.answer("⚠️ این مورد دیگر در فهرست وجود ندارد.")

    show = entries[index]
    try:
        season = int(show.get("season", 1))
        episode = int(show.get("next_episode", 1))
        episode_tag = f"S{season:02d}E{episode:02d}"
    except (TypeError, ValueError):
        episode_tag = "نامشخص"

    aliases = show.get("aliases", [])
    aliases_text = "، ".join(str(x) for x in aliases) if aliases else "ندارد"
    category = str(show.get("category", "Series"))
    category_text = "انیمه" if category.casefold() == "anime" else "سریال"
    state = eztv_tracker_service._load_state()
    observed = eztv_tracker_service.observed_release_weekdays(show, state)
    observed_text = "، ".join(_day_text(day) for day in observed) if observed else "هنوز داده‌ای ثبت نشده"

    body = (
        f"🎬 <b>{html.escape(_show_title(show))}</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"نوع: <b>{category_text}</b>\n"
        f"قسمت بعدی: <code>{episode_tag}</code>\n"
        f"نام‌های جایگزین: {html.escape(aliases_text)}\n"
        f"کیفیت‌ها: <b>{html.escape(_quality_text(show.get('qualities', [])))}</b>\n"
        f"روز انتشار تنظیم‌شده: <b>{html.escape(_day_text(show.get('release_day')))}</b>\n"
        f"روزهای ثبت‌شده از RSS: <b>{html.escape(observed_text)}</b>\n"
        f"پوشه‌های مقصد:\n<code>{html.escape(_folder_text(show.get('quality_folders', {})))}</code>"
    )

    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="✏️ ویرایش مشخصات", callback_data=f"eztv:edit:{index}")
    )
    builder.row(
        InlineKeyboardButton(text="🗑 حذف", callback_data=f"eztv:delete_ask:{index}"),
        InlineKeyboardButton(text="🔙 فهرست", callback_data=f"eztv:page:{index // PAGE_SIZE}"),
    )
    if edit:
        await target.edit_text(body, reply_markup=builder.as_markup(), parse_mode="HTML")
    else:
        await target.answer(body, reply_markup=builder.as_markup(), parse_mode="HTML")


async def _prompt_title(target: Message, state: FSMContext, edit: bool):
    data = await state.get_data()
    current = str(data.get("draft", {}).get("title", ""))
    prompt = (
        f"✏️ عنوان فعلی: <code>{html.escape(current)}</code>\n"
        "عنوان جدید را ارسال کنید. برای حفظ عنوان فعلی، - بفرستید."
        if edit else
        "نام سریال یا انیمه را ارسال کنید. بهتر است عنوان انگلیسی مطابق نام انتشارهای EZTV باشد."
    )
    await state.set_state(EztvEditStates.title)
    await target.answer(prompt, parse_mode="HTML", reply_markup=_cancel_keyboard())


async def _prompt_aliases(target: Message, state: FSMContext, edit: bool):
    data = await state.get_data()
    current = data.get("draft", {}).get("aliases", [])
    current_text = ", ".join(current) if current else "ندارد"
    prompt = (
        f"نام‌های جایگزین فعلی: <code>{html.escape(current_text)}</code>\n"
        "نام‌های جایگزین را با کاما یا سمی‌کالن جدا کنید.\n"
        "- یعنی بدون تغییر و none یعنی پاک‌کردن همه."
        if edit else
        "نام‌های جایگزین سریال را با کاما جدا کنید؛ مثلاً Example.Show, ExampleShow.\n"
        "اگر نام جایگزین ندارد، none بفرستید."
    )
    await state.set_state(EztvEditStates.aliases)
    await target.answer(prompt, parse_mode="HTML", reply_markup=_cancel_keyboard())


def _category_keyboard(current: str):
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text=("✅ " if current == "Series" else "") + "سریال",
            callback_data="eztv:category:Series",
        ),
        InlineKeyboardButton(
            text=("✅ " if current == "Anime" else "") + "انیمه",
            callback_data="eztv:category:Anime",
        ),
    )
    builder.row(InlineKeyboardButton(text="✖️ انصراف", callback_data="eztv:cancel"))
    return builder.as_markup()


async def _prompt_season(target: Message, state: FSMContext, edit: bool):
    data = await state.get_data()
    current = data.get("draft", {}).get("season", 1)
    prompt = (
        f"شماره فصل فعلی: <code>{current}</code>\n"
        "شماره فصل جدید را بفرستید؛ برای حفظ مقدار فعلی - بفرستید."
        if edit else
        "شماره فصل را وارد کنید؛ مثلاً 2."
    )
    await state.set_state(EztvEditStates.season)
    await target.answer(prompt, parse_mode="HTML", reply_markup=_cancel_keyboard())


async def _prompt_next_episode(target: Message, state: FSMContext, edit: bool):
    data = await state.get_data()
    current = data.get("draft", {}).get("next_episode", 1)
    prompt = (
        f"قسمت بعدی فعلی: <code>{current}</code>\n"
        "شماره قسمت بعدی را بفرستید؛ برای حفظ مقدار فعلی - بفرستید."
        if edit else
        "شماره قسمت بعدی که منتظر انتشار آن هستید را وارد کنید؛ مثلاً 4."
    )
    await state.set_state(EztvEditStates.next_episode)
    await target.answer(prompt, parse_mode="HTML", reply_markup=_cancel_keyboard())


def _quality_keyboard(selected: list[str]):
    builder = InlineKeyboardBuilder()
    for key in eztv_tracker_service.QUALITY_KEYS:
        mark = "✅" if key in selected else "⬜"
        builder.row(
            InlineKeyboardButton(
                text=f"{mark} {QUALITY_LABELS[key]}",
                callback_data=f"eztv:quality:{key}",
            )
        )
    builder.row(InlineKeyboardButton(text="✅ تأیید کیفیت‌ها", callback_data="eztv:qualities_done"))
    builder.row(InlineKeyboardButton(text="✖️ انصراف", callback_data="eztv:cancel"))
    return builder.as_markup()


async def _prompt_qualities(target: Message, state: FSMContext):
    data = await state.get_data()
    selected = data.get("draft_qualities", [])
    await state.set_state(EztvEditStates.qualities)
    await target.answer(
        "کیفیت‌هایی را که باید در RSS رصد شوند انتخاب کنید. برای فعال/غیرفعال‌کردن هر مورد روی آن بزنید:",
        reply_markup=_quality_keyboard(selected),
    )


def _default_folders(existing: dict[str, Any] | None = None) -> dict[str, Any]:
    result = dict(DEFAULT_QUALITY_FOLDERS)
    if isinstance(existing, dict):
        result.update(existing)
    return result


async def _prompt_folders(target: Message, state: FSMContext):
    data = await state.get_data()
    folders = _default_folders(data.get("draft", {}).get("quality_folders", {}))
    lines = [
        "📁 پوشه‌های مقصد را مشخص کنید؛ هر کیفیت را در یک خط وارد کنید:",
        "فرمت:",
        "<code>fullhd_h264=FullHD</code>",
        "<code>480p=480p-RMTeam,480p-EBTV</code>",
        "<code>2160p=2160p-HDR-x265</code>",
        "",
        "برای چند پوشه 480p از کاما استفاده کنید. نام پوشه باید نام یک پوشه مستقیم باشد و / نداشته باشد.",
        "ارسال - یعنی استفاده از تنظیمات فعلی/پیش‌فرض.",
        "",
        "<b>تنظیم فعلی:</b>",
        f"<code>{html.escape(_folder_text(folders))}</code>",
    ]
    await state.set_state(EztvEditStates.folders)
    await target.answer("\n".join(lines), parse_mode="HTML", reply_markup=_cancel_keyboard())


def _release_day_keyboard(current: str | None):
    builder = InlineKeyboardBuilder()
    for index in range(0, len(DAY_OPTIONS), 2):
        row = []
        for day, label in DAY_OPTIONS[index:index + 2]:
            mark = "✅ " if (day or "") == (current or "") else ""
            value = day or "Unknown"
            row.append(
                InlineKeyboardButton(
                    text=mark + label,
                    callback_data=f"eztv:day:{value}",
                )
            )
        builder.row(*row)
    builder.row(InlineKeyboardButton(text="✖️ انصراف", callback_data="eztv:cancel"))
    return builder.as_markup()


def _parse_aliases(raw: str) -> list[str]:
    if raw.strip().casefold() in ("", "none", "هیچ", "ندارد"):
        return []
    values = re.split(r"[,;،]+", raw)
    return list(dict.fromkeys(value.strip() for value in values if value.strip()))


def _parse_folders(raw: str, existing: dict[str, Any]) -> dict[str, Any]:
    if raw.strip() == "-":
        return _default_folders(existing)

    result = _default_folders(existing)
    allowed = set(eztv_tracker_service.QUALITY_KEYS)
    seen = set()
    for line in raw.splitlines():
        line = line.strip()
        if not line or "=" not in line:
            continue
        key, value = [part.strip() for part in line.split("=", 1)]
        key = key.casefold()
        if key not in allowed:
            continue
        if not value:
            raise ValueError(f"مقدار پوشه {key} خالی است.")
        names = [item.strip() for item in re.split(r"[,;،]+", value) if item.strip()]
        if not names:
            raise ValueError(f"پوشه مقصد {key} معتبر نیست.")
        for name in names:
            if name in (".", "..") or "/" in name or "\\" in name:
                raise ValueError("نام پوشه باید یک نام ساده و بدون / یا \\ باشد.")
        result[key] = names if key == "480p" and len(names) > 1 else names[0]
        seen.add(key)
    if not seen:
        raise ValueError("هیچ خط معتبری پیدا نشد؛ فرمت را مانند نمونه وارد کنید یا - بفرستید.")
    return result


async def _start_wizard(
    target: Message,
    state: FSMContext,
    mode: str,
    index: int | None = None,
):
    entries = eztv_tracker_service.get_show_entries()
    old_show = entries[index] if mode == "edit" and index is not None and 0 <= index < len(entries) else {}
    draft = dict(old_show)
    draft.setdefault("title", "")
    draft.setdefault("aliases", [])
    draft.setdefault("category", "Series")
    draft.setdefault("season", 1)
    draft.setdefault("next_episode", 1)
    draft.setdefault("qualities", ["fullhd_h264", "480p"])
    draft.setdefault("quality_folders", _default_folders())
    draft.setdefault("release_day", "")

    await state.clear()
    await state.update_data(
        eztv_mode=mode,
        eztv_index=index,
        old_show=old_show,
        draft=draft,
        draft_qualities=list(draft.get("qualities", [])),
    )
    await _prompt_title(target, state, mode == "edit")


async def _save_wizard(call: CallbackQuery, state: FSMContext, release_day: str):
    data = await state.get_data()
    draft = dict(data.get("draft", {}))
    draft["release_day"] = release_day
    draft["qualities"] = list(data.get("draft_qualities", []))

    if not draft.get("title", "").strip():
        return await call.answer("عنوان سریال خالی است.", show_alert=True)
    if not draft["qualities"]:
        return await call.answer("حداقل یک کیفیت باید انتخاب شود.", show_alert=True)

    mode = data.get("eztv_mode", "add")
    index = data.get("eztv_index")
    entries = eztv_tracker_service.get_show_entries()
    duplicate_index = next(
        (
            i for i, item in enumerate(entries)
            if i != index
            and _show_title(item).casefold() == draft["title"].strip().casefold()
            and str(item.get("season", "")) == str(draft.get("season", ""))
        ),
        None,
    )
    if duplicate_index is not None:
        return await call.answer("این عنوان و فصل از قبل در فهرست وجود دارد.", show_alert=True)

    draft["title"] = draft["title"].strip()
    draft["category"] = str(draft.get("category", "Series")).title()
    draft["season"] = int(draft["season"])
    draft["next_episode"] = int(draft["next_episode"])
    draft["aliases"] = list(draft.get("aliases", []))
    draft["quality_folders"] = _default_folders(draft.get("quality_folders", {}))

    old_show = data.get("old_show", {})
    if mode == "edit" and isinstance(index, int) and 0 <= index < len(entries):
        entries[index] = draft
    else:
        entries.append(draft)
        index = len(entries) - 1

    try:
        eztv_tracker_service.save_show_entries(entries)
        if mode == "edit" and old_show:
            eztv_tracker_service.migrate_show_state(old_show, draft)
    except Exception as exc:
        logging.exception("Could not save EZTV show editor data")
        return await call.answer(f"ذخیره ناموفق بود: {str(exc)[:180]}", show_alert=True)

    await state.clear()
    await call.answer("مشخصات ذخیره شد.")
    await call.message.edit_text(
        "✅ مشخصات با موفقیت ذخیره شد.\n"
        "فهرست مدیریت‌شده در فایل جداگانه ذخیره شده و config.yaml بازنویسی نشده است.",
        parse_mode="HTML",
    )
    await _render_detail(call.message, int(index), edit=True)


@router.message(Command("eztv_edit"))
async def cmd_eztv_edit(message: Message, state: FSMContext):
    if not _is_admin(message):
        return
    await state.clear()
    await _render_list(message, page=0, edit=False)


@router.callback_query(F.data.startswith("eztv:"))
async def cb_eztv_manager(call: CallbackQuery, state: FSMContext):
    if not _is_admin_callback(call):
        return await call.answer("دسترسی به مدیریت EZTV ندارید.", show_alert=True)

    parts = (call.data or "").split(":")
    action = parts[1] if len(parts) > 1 else ""

    if action == "page":
        page = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
        await state.clear()
        await _render_list(call.message, page=page, edit=True)
        return await call.answer()

    if action == "open":
        index = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else -1
        await state.clear()
        await _render_detail(call.message, index, edit=True)
        return await call.answer()

    if action == "add":
        await _start_wizard(call.message, state, "add")
        return await call.answer()

    if action == "edit":
        index = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else -1
        entries = eztv_tracker_service.get_show_entries()
        if index < 0 or index >= len(entries):
            return await call.answer("این سریال دیگر در فهرست نیست.", show_alert=True)
        await _start_wizard(call.message, state, "edit", index)
        return await call.answer()

    if action == "delete_ask":
        index = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else -1
        entries = eztv_tracker_service.get_show_entries()
        if index < 0 or index >= len(entries):
            return await call.answer("این سریال دیگر در فهرست نیست.", show_alert=True)
        builder = InlineKeyboardBuilder()
        builder.row(
            InlineKeyboardButton(text="🗑 بله، حذف شود", callback_data=f"eztv:delete_yes:{index}"),
            InlineKeyboardButton(text="انصراف", callback_data=f"eztv:open:{index}"),
        )
        await call.message.edit_text(
            f"⚠️ آیا سریال <b>{html.escape(_show_title(entries[index]))}</b> از فهرست ردیابی حذف شود؟\n"
            "فایل‌های دانلودشده حذف نمی‌شوند؛ فقط ردیابی خودکار این مورد متوقف خواهد شد.",
            reply_markup=builder.as_markup(),
            parse_mode="HTML",
        )
        return await call.answer()

    if action == "delete_yes":
        index = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else -1
        entries = eztv_tracker_service.get_show_entries()
        if index < 0 or index >= len(entries):
            return await call.answer("این سریال دیگر در فهرست نیست.", show_alert=True)
        removed = entries.pop(index)
        try:
            eztv_tracker_service.save_show_entries(entries)
        except Exception as exc:
            logging.exception("Could not delete EZTV show")
            return await call.answer(f"حذف ناموفق بود: {str(exc)[:160]}", show_alert=True)
        await call.answer(f"{_show_title(removed)[:50]} حذف شد.")
        await _render_list(call.message, page=min(index // PAGE_SIZE, max(0, (len(entries) - 1) // PAGE_SIZE)), edit=True)
        return

    if action == "category":
        category = parts[2] if len(parts) > 2 else ""
        if category not in ("Series", "Anime"):
            return await call.answer("نوع محتوا معتبر نیست.", show_alert=True)
        data = await state.get_data()
        draft = dict(data.get("draft", {}))
        draft["category"] = category
        await state.update_data(draft=draft)
        await call.message.edit_text(
            f"✅ نوع محتوا: {'سریال' if category == 'Series' else 'انیمه'}"
        )
        await _prompt_season(call.message, state, data.get("eztv_mode") == "edit")
        return await call.answer()

    if action == "quality":
        quality = parts[2] if len(parts) > 2 else ""
        if quality not in eztv_tracker_service.QUALITY_KEYS:
            return await call.answer("کیفیت معتبر نیست.", show_alert=True)
        data = await state.get_data()
        selected = list(data.get("draft_qualities", []))
        if quality in selected:
            selected.remove(quality)
        else:
            selected.append(quality)
        await state.update_data(draft_qualities=selected)
        await call.message.edit_reply_markup(reply_markup=_quality_keyboard(selected))
        return await call.answer("انتخاب کیفیت به‌روزرسانی شد.")

    if action == "qualities_done":
        data = await state.get_data()
        selected = list(data.get("draft_qualities", []))
        if not selected:
            return await call.answer("حداقل یک کیفیت انتخاب کنید.", show_alert=True)
        await call.message.edit_text(
            "✅ کیفیت‌ها: <b>" + html.escape(_quality_text(selected)) + "</b>",
            parse_mode="HTML",
        )
        await _prompt_folders(call.message, state)
        return await call.answer()

    if action == "day":
        value = parts[2] if len(parts) > 2 else "Unknown"
        if value == "Unknown":
            value = ""
        if value not in {item[0] for item in DAY_OPTIONS}:
            return await call.answer("روز انتخاب‌شده معتبر نیست.", show_alert=True)
        return await _save_wizard(call, state, value)

    if action == "cancel":
        await state.clear()
        await call.answer("عملیات لغو شد.")
        return await _render_list(call.message, page=0, edit=True)

    await call.answer()


@router.message(EztvEditStates.title)
async def handle_eztv_title(message: Message, state: FSMContext):
    if not _is_admin(message):
        return
    raw = (message.text or "").strip()
    data = await state.get_data()
    draft = dict(data.get("draft", {}))
    editing = data.get("eztv_mode") == "edit"

    if raw == "-" and editing:
        pass
    elif not raw or raw.startswith("/"):
        return await message.answer("⚠️ عنوان معتبر نیست؛ دوباره وارد کنید:")
    else:
        draft["title"] = raw

    await state.update_data(draft=draft)
    await _prompt_aliases(message, state, editing)


@router.message(EztvEditStates.aliases)
async def handle_eztv_aliases(message: Message, state: FSMContext):
    if not _is_admin(message):
        return
    raw = (message.text or "").strip()
    data = await state.get_data()
    draft = dict(data.get("draft", {}))
    if raw == "-" and data.get("eztv_mode") == "edit":
        pass
    else:
        draft["aliases"] = _parse_aliases(raw)
    await state.update_data(draft=draft)
    await message.answer("نوع محتوا را انتخاب کنید:", reply_markup=_category_keyboard(str(draft.get("category", "Series"))))


@router.message(EztvEditStates.season)
async def handle_eztv_season(message: Message, state: FSMContext):
    if not _is_admin(message):
        return
    raw = (message.text or "").strip()
    data = await state.get_data()
    draft = dict(data.get("draft", {}))
    if raw != "-":
        try:
            value = int(raw)
            if value < 1 or value > 99:
                raise ValueError
            draft["season"] = value
        except ValueError:
            return await message.answer("⚠️ شماره فصل باید عددی بین 1 تا 99 باشد:")
    await state.update_data(draft=draft)
    await _prompt_next_episode(message, state, data.get("eztv_mode") == "edit")


@router.message(EztvEditStates.next_episode)
async def handle_eztv_next_episode(message: Message, state: FSMContext):
    if not _is_admin(message):
        return
    raw = (message.text or "").strip()
    data = await state.get_data()
    draft = dict(data.get("draft", {}))
    if raw != "-":
        try:
            value = int(raw)
            if value < 1 or value > 999:
                raise ValueError
            draft["next_episode"] = value
        except ValueError:
            return await message.answer("⚠️ شماره قسمت باید عددی بین 1 تا 999 باشد:")
    await state.update_data(draft=draft, draft_qualities=list(draft.get("qualities", [])))
    await _prompt_qualities(message, state)


@router.message(EztvEditStates.folders)
async def handle_eztv_folders(message: Message, state: FSMContext):
    if not _is_admin(message):
        return
    raw = (message.text or "").strip()
    data = await state.get_data()
    draft = dict(data.get("draft", {}))
    try:
        draft["quality_folders"] = _parse_folders(
            raw,
            draft.get("quality_folders", {}),
        )
    except ValueError as exc:
        return await message.answer(f"⚠️ {html.escape(str(exc))}\nدوباره ارسال کنید:", parse_mode="HTML")
    await state.update_data(draft=draft)
    await message.answer(
        "📅 روز معمول انتشار سریال را انتخاب کنید:",
        reply_markup=_release_day_keyboard(str(draft.get("release_day", "") or "")),
    )


@router.message(Command("cancel"), StateFilter(EztvEditStates))
async def cmd_eztv_cancel(message: Message, state: FSMContext):
    if not _is_admin(message):
        return
    await state.clear()
    await message.answer("عملیات مدیریت EZTV لغو شد.")
    await _render_list(message, page=0, edit=False)


@router.message(Command("eztv_status"))
async def cmd_eztv_status(message: Message):
    if not _is_admin(message):
        return

    config = eztv_tracker_service.get_config()
    shows = eztv_tracker_service.get_shows()
    if not shows:
        return await message.answer(
            "📡 ردیاب EZTV فعال است، اما فهرست سریال‌ها خالی است.\n"
            "با دستور /eztv_edit سریال‌ها را از داخل تلگرام اضافه کنید.",
            parse_mode="HTML",
        )

    state = eztv_tracker_service._load_state()
    lines = [
        "📡 <b>وضعیت ردیاب EZTV</b>",
        f"وضعیت سرویس: {'✅ فعال' if config.get('enabled', True) else '⏸ غیرفعال'}",
        f"RSS: <code>{html.escape(str(config.get('feed_url', 'https://myrss.org/eztv')))}</code>",
        f"فاصله بررسی: <code>{int(config.get('poll_interval_seconds', 300) or 300)}</code> ثانیه",
        f"گزارش روزانه: <code>{html.escape(str(config.get('daily_report_time', '09:00')))}</code>",
        "",
    ]
    for show in shows:
        episode = eztv_tracker_service._episode_progress(show, state)
        tag = eztv_tracker_service._episode_tag(show["season"], episode)
        observed = eztv_tracker_service.observed_release_weekdays(show, state)
        observed_text = "، ".join(_day_text(day) for day in observed) if observed else "ثبت نشده"
        lines.append(
            f"• <b>{html.escape(show['title'])}</b> "
            f"<code>{tag}</code> — {html.escape(_quality_text(show['qualities']))}\n"
            f"  روز انتشار: <b>{html.escape(_day_text(show.get('release_day')))}</b> "
            f"(سابقه RSS: {html.escape(observed_text)})"
        )
    for chunk in _split_html_lines("\n".join(lines)):
        await message.answer(chunk, parse_mode="HTML")
        await asyncio.sleep(1.1)


@router.message(Command("eztv_check"))
async def cmd_eztv_check(message: Message):
    if not _is_admin(message):
        return
    wait = await message.answer("🔎 در حال بررسی فوری RSS سایت EZTV و قسمت‌های مورد انتظار...")
    try:
        result = await eztv_tracker_service.poll_once(message.bot, send_summary=True)
        queued = result.get("queued", [])
        completed = result.get("completed_updates", [])
        await wait.edit_text(
            "✅ بررسی فوری تمام شد.\n"
            f"سریال‌های تنظیم‌شده: <code>{result.get('shows', 0)}</code>\n"
            f"دانلودهای جدید به صف: <code>{len(queued)}</code>\n"
            f"نتایج نهایی جدید: <code>{len(completed)}</code>\n"
            f"مواردی که از قبل موجود بودند: <code>{len(result.get('already_local', []))}</code>",
            parse_mode="HTML",
        )
    except Exception as exc:
        logging.exception("Manual EZTV check failed")
        await wait.edit_text(
            "❌ بررسی EZTV با خطا مواجه شد:\n<code>"
            + html.escape(str(exc)[:1000])
            + "</code>",
            parse_mode="HTML",
        )


@router.message(Command("eztv_report"))
async def cmd_eztv_report(message: Message):
    if not _is_admin(message):
        return
    wait = await message.answer("📊 در حال تهیه گزارش روزانه...")
    try:
        report = await eztv_tracker_service.build_daily_report()
        chunks = _split_html_lines(report)
        await wait.edit_text(chunks[0], parse_mode="HTML")
        for chunk in chunks[1:]:
            await asyncio.sleep(1.1)
            await message.answer(chunk, parse_mode="HTML")
    except Exception as exc:
        logging.exception("Manual EZTV report failed")
        await wait.edit_text(
            "❌ تهیه گزارش با خطا مواجه شد:\n<code>"
            + html.escape(str(exc)[:1000])
            + "</code>",
            parse_mode="HTML",
        )
