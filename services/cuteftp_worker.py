import ctypes
import struct
import time
import urllib.parse
from pathlib import Path
import win32api
import win32clipboard
import win32con
import win32gui
import win32process
from core.config_loader import cfg

user32 = ctypes.windll.user32


def _copy_files_to_windows_clipboard(file_paths: list[str]):
    """قرار دادن پوشه یا فایل در کلیپ‌بورد ویندوز با فرمت بومی CF_HDROP"""
    p_files_offset = 20
    f_wide = 1
    dropfiles_header = struct.pack("IiiII", p_files_offset, 0, 0, 0, f_wide)

    encoded_paths = b"".join(p.encode("utf-16le") + b"\x00\x00" for p in file_paths) + b"\x00\x00"
    hdrop_data = dropfiles_header + encoded_paths

    win32clipboard.OpenClipboard()
    try:
        win32clipboard.EmptyClipboard()
        win32clipboard.SetClipboardData(win32con.CF_HDROP, hdrop_data)
    finally:
        win32clipboard.CloseClipboard()


def _get_edit_text(edit_hwnd: int) -> str:
    """خواندن متن کنترل‌های ویندوز با ctypes (سازگار با Python 3.14)"""
    try:
        length = user32.SendMessageW(edit_hwnd, win32con.WM_GETTEXTLENGTH, 0, 0)
        if length <= 0:
            return ""
        buf = ctypes.create_unicode_buffer(length + 2)
        user32.SendMessageW(edit_hwnd, win32con.WM_GETTEXT, length + 1, buf)
        return buf.value
    except Exception:
        return ""


def _set_edit_text(edit_hwnd: int, text: str):
    """نوشتن مسیر جدید در نوار آدرس با ctypes"""
    buf = ctypes.create_unicode_buffer(text)
    user32.SendMessageW(edit_hwnd, win32con.WM_SETTEXT, 0, buf)


def _find_cuteftp_gui_window() -> int | None:
    found_hwnds = []

    def _enum_cb(hwnd, _):
        if win32gui.IsWindowVisible(hwnd):
            title = win32gui.GetWindowText(hwnd)
            if "cuteftp" in title.lower():
                found_hwnds.append(hwnd)

    win32gui.EnumWindows(_enum_cb, None)
    return found_hwnds[0] if found_hwnds else None


def _find_remote_controls(main_hwnd: int) -> tuple[int | None, int | None]:
    """
    پیدا کردن کنترل‌های پنل سمت راست (Remote) در CuteFTP 9.3 بدون وابستگی به وضعیت ریموت دسکتاپ:
    ۱. نوار آدرس Remote: کنترلی از نوع Edit که متن آن با '/' شروع می‌شود (مثلاً /English/Movies)
    ۲. لیست فایل‌های Remote: کنترل SysListView32 در سمت راست بالا
    """
    listviews = []
    slash_edits = []
    other_edits = []

    def _enum_child(hwnd, _):
        cls_name = win32gui.GetClassName(hwnd)
        try:
            c_rect = win32gui.GetClientRect(hwnd)
            w = c_rect[2] - c_rect[0]
            h = c_rect[3] - c_rect[1]
            w_rect = win32gui.GetWindowRect(hwnd)
        except Exception:
            return

        if cls_name == "SysListView32" and w > 120 and h > 100:
            listviews.append((w_rect[0], w_rect[1], w, h, hwnd))
        elif cls_name == "Edit" and w > 100 and 12 <= h <= 45:
            txt = _get_edit_text(hwnd).strip()
            if txt.startswith("/"):
                slash_edits.append((w_rect[0], hwnd))
            elif not (len(txt) >= 2 and txt[1] == ":"):  # حذف نوار آدرس ویندوز (C:\...)
                other_edits.append((w_rect[0], hwnd))

    win32gui.EnumChildWindows(main_hwnd, _enum_child, None)

    remote_edit = None
    if slash_edits:
        # نوار آدرسی که با '/' شروع می‌شود، قطعا نوار آدرس هاست Remote است
        slash_edits.sort(key=lambda x: x[0], reverse=True)
        remote_edit = slash_edits[0][1]
    elif other_edits:
        other_edits.sort(key=lambda x: x[0], reverse=True)
        remote_edit = other_edits[0][1]

    remote_listview = None
    if listviews:
        # اگر بیش از ۲ لیست وجود دارد، پایینی‌ترین لیست مربوط به Queue Window است؛ آن را کنار بگذار
        if len(listviews) >= 3:
            listviews.sort(key=lambda x: x[1])
            top_listviews = listviews[:-1]
        else:
            top_listviews = listviews

        # در بین دو پنل چپ و راست، پنل سمت راست بیشترین X را دارد
        top_listviews.sort(key=lambda x: x[0], reverse=True)
        remote_listview = top_listviews[0][4]

    return remote_listview, remote_edit


def _navigate_cuteftp_gui_to_path(main_hwnd: int, edit_hwnd: int | None, target_remote_dir: str) -> str:
    """
    تغییر پوشه مقصد در CuteFTP 9.3 بدون نیاز به SetCursorPos (سازگار با RDP بسته یا Minimize شده)
    """
    if not edit_hwnd or not target_remote_dir or target_remote_dir == "__CURRENT__":
        return _get_edit_text(edit_hwnd).strip() if edit_hwnd else ""

    normalized_target = "/" + target_remote_dir.replace("\\", "/").strip().lstrip("/")
    if len(normalized_target) > 1:
        normalized_target = normalized_target.rstrip("/")

    current_path = _get_edit_text(edit_hwnd).strip()
    if len(current_path) > 1:
        current_path = current_path.rstrip("/")

    if current_path.lower() == normalized_target.lower():
        return normalized_target

    target_tid, _ = win32process.GetWindowThreadProcessId(main_hwnd)
    cur_tid = win32api.GetCurrentThreadId()
    attached = False
    try:
        if cur_tid != target_tid:
            attached = bool(user32.AttachThreadInput(cur_tid, target_tid, True))

        # ۱. دادن فوکوس سیستمی به نوار آدرس Remote
        user32.SetFocus(edit_hwnd)
        lparam_click = win32api.MAKELONG(15, 8)
        user32.SendMessageW(edit_hwnd, win32con.WM_LBUTTONDOWN, win32con.MK_LBUTTON, lparam_click)
        user32.SendMessageW(edit_hwnd, win32con.WM_LBUTTONUP, 0, lparam_click)
        time.sleep(0.1)

        # ۲. نوشتن مسیر جدید در نوار آدرس و بردن مکان‌نما به انتهای خط
        _set_edit_text(edit_hwnd, normalized_target)
        text_len = len(normalized_target)
        user32.SendMessageW(edit_hwnd, win32con.EM_SETSEL, text_len, text_len)

        # ۳. اطلاع‌رسانی تغییر متن به کنترل والد (ComboBox)
        combo_hwnd = win32gui.GetParent(edit_hwnd)
        edit_id = win32gui.GetDlgCtrlID(edit_hwnd)
        if combo_hwnd:
            # ارسال EN_CHANGE و CBN_EDITCHANGE
            user32.SendMessageW(combo_hwnd, win32con.WM_COMMAND, (0x0300 << 16) | (edit_id & 0xFFFF), edit_hwnd)
            combo_parent = win32gui.GetParent(combo_hwnd)
            combo_id = win32gui.GetDlgCtrlID(combo_hwnd)
            if combo_parent:
                user32.SendMessageW(combo_parent, win32con.WM_COMMAND, (5 << 16) | (combo_id & 0xFFFF), combo_hwnd)

        time.sleep(0.1)

        # ۴. ارسال کلید Enter مستقیماً به نوار آدرس برای رفتن به پوشه جدید
        user32.PostMessageW(edit_hwnd, win32con.WM_KEYDOWN, win32con.VK_RETURN, 0x001C0001)
        user32.PostMessageW(edit_hwnd, win32con.WM_CHAR, 0x0D, 0x001C0001)
        user32.PostMessageW(edit_hwnd, win32con.WM_KEYUP, win32con.VK_RETURN, 0xC01C0001)
    finally:
        if attached:
            user32.AttachThreadInput(cur_tid, target_tid, False)

    # ۵. صبر برای دریافت لیست فایل‌های پوشه جدید از هاست دانلود
    time.sleep(2.2)

    updated_path = _get_edit_text(edit_hwnd).strip()
    return updated_path or normalized_target


def upload_via_open_cuteftp_gui(local_target: Path, target_remote_dir: str = "__CURRENT__") -> tuple[bool, str]:
    """
    آپلود پوشه یا فایل از طریق پنجره بازِ CuteFTP بدون نیاز به ماوس فیزیکی (SetCursorPos)
    """
    main_hwnd = _find_cuteftp_gui_window()
    if not main_hwnd:
        return False, ""

    try:
        if win32gui.IsIconic(main_hwnd):
            win32gui.ShowWindow(main_hwnd, win32con.SW_RESTORE)
            time.sleep(0.2)
    except Exception:
        pass

    remote_pane_hwnd, remote_edit_hwnd = _find_remote_controls(main_hwnd)

    # ۱. تغییر پوشه در نوار آدرس Remote
    actual_remote_path = _navigate_cuteftp_gui_to_path(main_hwnd, remote_edit_hwnd, target_remote_dir)

    # ۲. کپی کردن فایل یا پوشه محلی در کلیپ‌بورد ویندوز
    _copy_files_to_windows_clipboard([str(local_target.resolve())])
    time.sleep(0.15)

    # ۳. فعال‌سازی پنل سمت راست (Remote Pane) و ارسال دستور Paste (ID_EDIT_PASTE = 0xE125)
    target_tid, _ = win32process.GetWindowThreadProcessId(main_hwnd)
    cur_tid = win32api.GetCurrentThreadId()
    attached = False
    try:
        if cur_tid != target_tid:
            attached = bool(user32.AttachThreadInput(cur_tid, target_tid, True))

        if remote_pane_hwnd:
            user32.SetFocus(remote_pane_hwnd)
            try:
                c_rect = win32gui.GetClientRect(remote_pane_hwnd)
                click_x = max(10, (c_rect[2] - c_rect[0]) - 20)
            except Exception:
                click_x = 300
            # ارسال کلیک مجازی به حاشیه سمت راست لیست Remote تا پنل Remote در MFC فعال شود
            lparam = win32api.MAKELONG(click_x, 25)
            user32.SendMessageW(remote_pane_hwnd, win32con.WM_LBUTTONDOWN, win32con.MK_LBUTTON, lparam)
            user32.SendMessageW(remote_pane_hwnd, win32con.WM_LBUTTONUP, 0, lparam)
            time.sleep(0.15)

        # اجرای دستور Edit -> Paste در CuteFTP
        user32.PostMessageW(main_hwnd, win32con.WM_COMMAND, 0xE125, 0)
    finally:
        if attached:
            user32.AttachThreadInput(cur_tid, target_tid, False)

    time.sleep(0.3)
    return True, actual_remote_path


def get_current_cuteftp_remote_dir() -> str:
    """خواندن مسیر فعلی که پنجره باز CuteFTP روی آن قرار دارد"""
    try:
        main_hwnd = _find_cuteftp_gui_window()
        if not main_hwnd:
            return cfg.get("cuteftp", "remote_base_dir", "/English/Movies")
        _, remote_edit_hwnd = _find_remote_controls(main_hwnd)
        if remote_edit_hwnd:
            txt = _get_edit_text(remote_edit_hwnd).strip()
            if txt:
                return txt
    except Exception:
        pass
    return cfg.get("cuteftp", "remote_base_dir", "/English/Movies")


def sync_upload_cuteftp(local_target: Path, remote_subpath: str = "__CURRENT__") -> list[str]:
    ftp_cfg = cfg.get("cuteftp") or {}
    base_url = str(ftp_cfg.get("base_download_url", "https://dl15.cdnzed.sbs")).rstrip("/")

    if not remote_subpath or remote_subpath == "__CURRENT__":
        target_remote_dir = "__CURRENT__"
    else:
        target_remote_dir = "/" + remote_subpath.replace("\\", "/").strip().lstrip("/")

    def _generate_links(final_remote_dir: str) -> list[str]:
        clean_sub = ""
        if final_remote_dir and final_remote_dir != "__CURRENT__":
            rel_remote = final_remote_dir.replace("\\", "/")
            for pub_prefix in ("/public_html", "/www", "/htdocs"):
                if rel_remote.startswith(pub_prefix):
                    rel_remote = rel_remote[len(pub_prefix):]
            clean_sub = rel_remote.strip("/")

        urls = []
        if local_target.is_file():
            rel_url = f"{clean_sub}/{local_target.name}".lstrip("/")
            urls.append(f"{base_url}/{urllib.parse.quote(rel_url)}")
        else:
            for file_p in local_target.rglob("*"):
                if file_p.is_file() and not file_p.name.startswith("_temp_"):
                    rel = file_p.relative_to(local_target.parent).as_posix()
                    if clean_sub:
                        rel = f"{clean_sub}/{rel}"
                    urls.append(f"{base_url}/{urllib.parse.quote(rel)}")
        return urls

    ok, used_remote = upload_via_open_cuteftp_gui(local_target, target_remote_dir)
    if ok:
        return _generate_links(used_remote)

    raise RuntimeError(
        "پنجره نرم‌افزار CuteFTP 9 روی سرور پیدا نشد! لطفاً مطمئن شوید برنامه CuteFTP باز است."
    )