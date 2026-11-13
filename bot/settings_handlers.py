from aiogram import Router, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.utils.keyboard import InlineKeyboardBuilder

from core.config_loader import cfg
from bot.keyboards import main_menu_kb

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


class SettingsStates(StatesGroup):
    waiting_new_value = State()


PRIVATE_SETTING_KEYS = {"bot_token", "password", "username", "admin_ids"}


def _is_private_setting(key: str) -> bool:
    return str(key).casefold() in PRIVATE_SETTING_KEYS


def is_admin(user_id: int) -> bool:
    return user_id in cfg.get("telegram", "admin_ids", [])


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
            if _is_private_setting(k) or isinstance(v, (dict, list)):
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
    """تبدیل هوشمند متن ورودی کاربر به نوع داده اصلی (bool, int, float, str)"""
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
        "⚙️ **پنل مدیریت تنظیمات ربات**\n\n"
        "تنظیمات غیرمحرمانه در `config.yaml` ذخیره می‌شوند. اطلاعات محرمانه مانند توکن و رمزهای عبور فقط از طریق فایل محلی `.env` تنظیم می‌شوند و در این پنل نمایش داده نمی‌شوند.\n"
        "پس از تغییر `.env` ربات را مجدداً اجرا کنید.\n"
        "بخش مورد نظر را انتخاب کنید:",
        reply_markup=_sections_kb(),
        parse_mode="Markdown"
    )


@router.callback_query(F.data == "menu:settings")
async def cb_settings_home(call: CallbackQuery, state: FSMContext):
    await state.clear()
    cfg.reload()
    await call.message.edit_text(
        "⚙️ **پنل مدیریت تنظیمات ربات**\n\n"
        "اطلاعات محرمانه در `.env` نگهداری می‌شوند و در این پنل قابل مشاهده یا ویرایش نیستند.\n"
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
        has_private_values = False
        for k, v in sec_data.items():
            if _is_private_setting(k):
                has_private_values = True
                continue
            if isinstance(v, (dict, list)):
                continue
            lines.append(f"• `{k}` = `{v}`")
        if has_private_values:
            lines.extend([
                "",
                "🔒 مقادیر محرمانهٔ این بخش از فایل محلی `.env` خوانده می‌شوند و برای امنیت نمایش داده نمی‌شوند.",
            ])

    lines.append("\n👇 برای تغییر هر مورد، روی دکمه آن کلیک کنید (گزینه‌های بولین با یک کلیک تغییر می‌کنند):")
    await call.message.edit_text(
        "\n".join(lines),
        reply_markup=_section_keys_kb(section),
        parse_mode="Markdown"
    )


@router.callback_query(F.data.startswith("cfgkey:"))
async def cb_edit_config_key(call: CallbackQuery, state: FSMContext):
    _, section, key = call.data.split(":", 2)
    if _is_private_setting(key):
        return await call.answer(
            "🔒 این مقدار محرمانه است؛ آن را فقط در فایل محلی .env تغییر دهید.",
            show_alert=True,
        )
    current_val = cfg.get(section, key)

    # اگر مقدار از نوع بولین (True/False) باشد، با همان یک کلیک آن را تغییر بده!
    if isinstance(current_val, bool):
        new_val = not current_val
        cfg.set_value(section, key, new_val)
        await call.answer(f"✅ مقدار {key} به {new_val} تغییر یافت!")
        return await cb_open_config_section(call, state)

    await state.update_data(cfg_section=section, cfg_key=key)
    await state.set_state(SettingsStates.waiting_new_value)

    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text="🔙 انصراف و بازگشت", callback_data=f"cfgsec:{section}"))

    await call.message.edit_text(
        f"✏️ **ویرایش تنظیمات `{section}.{key}`**\n\n"
        f"🔹 مقدار فعلی (برای کپی لمس کنید):\n`{current_val}`\n\n"
        "لطفاً **مقدار جدید** را در چت ارسال کنید:",
        reply_markup=kb.as_markup(),
        parse_mode="Markdown"
    )


@router.message(SettingsStates.waiting_new_value)
async def handle_new_config_value(message: Message, state: FSMContext):
    data = await state.get_data()
    section = data["cfg_section"]
    key = data["cfg_key"]
    if _is_private_setting(key):
        await state.clear()
        return await message.answer("🔒 این مقدار فقط در فایل محلی .env قابل تغییر است.")
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