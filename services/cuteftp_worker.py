import ctypes
import logging
import struct
import threading
import time
import urllib.parse
from pathlib import Path
import win32api
import win32clipboard
import win32con
import win32gui
import win32process
from core.config_loader import cfg

logger = logging.getLogger(__name__)

user32 = ctypes.windll.user32
_CUTEFTP_GUI_LOCK = threading.RLock()


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


def _normalise_remote_path(path: str) -> str:
    path = (path or "").replace("\\", "/").strip()
    if not path:
        return ""
    if path == "/":
        return "/"
    return "/" + path.lstrip("/").rstrip("/")


def _paths_equal(left: str, right: str) -> bool:
    return _normalise_remote_path(left).casefold() == _normalise_remote_path(right).casefold()


def _find_remote_controls(main_hwnd: int) -> tuple[int | None, int | None]:
    """
    Find the Remote list and the address Edit control above it.
    Candidate controls are ranked by their current path text and geometry
    relative to the right-hand (Remote) file list.
    """
    main_left, main_top, main_right, main_bottom = win32gui.GetWindowRect(main_hwnd)
    main_mid_x = (main_left + main_right) // 2
    listviews = []
    edits = []

    def _enum_child(hwnd, _):
        try:
            cls_name = win32gui.GetClassName(hwnd)
            left, top, right, bottom = win32gui.GetWindowRect(hwnd)
            width, height = right - left, bottom - top
            if width <= 0 or height <= 0:
                return

            if cls_name in ("SysListView32", "TListView") and width > 120 and height > 100:
                if (left + right) // 2 > main_mid_x:
                    listviews.append((left, top, right, bottom, hwnd))

            if cls_name in ("Edit", "ComboBox", "ComboBoxEx32") and width > 100 and 12 <= height <= 55:
                if left >= main_mid_x - 10:
                    text = _get_edit_text(hwnd).strip()
                    edits.append((left, top, right, bottom, cls_name, text, hwnd))
        except Exception:
            return

    win32gui.EnumChildWindows(main_hwnd, _enum_child, None)

    remote_listview = None
    remote_rect = None
    if listviews:
        # The upper/right list is the Remote pane; lower list views are usually the queue.
        listviews.sort(key=lambda item: (item[1], -item[0]))
        remote_rect = listviews[0][:4]
        remote_listview = listviews[0][4]

    remote_edit = None
    if edits:
        def _score(item):
            left, top, right, bottom, cls_name, text, hwnd = item
            score = 0
            if text.startswith("/"):
                score += 1000
            if cls_name == "Edit":
                score += 30
            if text:
                score += 20
            if left >= main_mid_x:
                score += 50
            if remote_rect:
                list_left, list_top, list_right, _ = remote_rect
                overlap = max(0, min(right, list_right) - max(left, list_left))
                if overlap:
                    score += 250
                if bottom <= list_top and list_top - bottom <= 100:
                    score += 300 - (list_top - bottom)
            return score

        edits.sort(key=_score, reverse=True)
        best = edits[0]
        if _score(best) >= 100:
            remote_edit = best[6]

    return remote_listview, remote_edit


def _press_key(vk: int):
    win32api.keybd_event(vk, 0, 0, 0)
    time.sleep(0.04)
    win32api.keybd_event(vk, 0, win32con.KEYEVENTF_KEYUP, 0)
    time.sleep(0.08)


def _press_ctrl_key(vk: int):
    win32api.keybd_event(win32con.VK_CONTROL, 0, 0, 0)
    time.sleep(0.04)
    win32api.keybd_event(vk, 0, 0, 0)
    time.sleep(0.04)
    win32api.keybd_event(vk, 0, win32con.KEYEVENTF_KEYUP, 0)
    win32api.keybd_event(win32con.VK_CONTROL, 0, win32con.KEYEVENTF_KEYUP, 0)
    time.sleep(0.12)


def _copy_text_to_clipboard(text: str):
    last_error = None
    for _ in range(10):
        try:
            win32clipboard.OpenClipboard()
            try:
                win32clipboard.EmptyClipboard()
                win32clipboard.SetClipboardData(win32con.CF_UNICODETEXT, text)
            finally:
                win32clipboard.CloseClipboard()
            return
        except Exception as exc:
            last_error = exc
            time.sleep(0.1)
    raise RuntimeError(f"امکان قراردادن مسیر در کلیپ‌بورد وجود ندارد: {last_error}")


def _focus_cuteftp_control(main_hwnd: int, control_hwnd: int):
    if win32gui.IsIconic(main_hwnd):
        win32gui.ShowWindow(main_hwnd, win32con.SW_RESTORE)
        time.sleep(0.25)

    try:
        win32gui.BringWindowToTop(main_hwnd)
        win32gui.SetForegroundWindow(main_hwnd)
    except Exception:
        pass
    time.sleep(0.15)

    foreground = win32gui.GetForegroundWindow()
    if foreground != main_hwnd:
        try:
            _, fg_pid = win32process.GetWindowThreadProcessId(foreground)
            _, main_pid = win32process.GetWindowThreadProcessId(main_hwnd)
        except Exception:
            fg_pid, main_pid = -1, -2
        if fg_pid != main_pid:
            raise RuntimeError(
                "CuteFTP نمی‌تواند پنجره فعال شود. CuteFTP و ربات باید در یک "
                "نشست تعاملی ویندوز اجرا شوند و صفحه قفل نباشد."
            )

    target_tid, _ = win32process.GetWindowThreadProcessId(control_hwnd)
    current_tid = win32api.GetCurrentThreadId()
    attached = False
    try:
        if current_tid != target_tid:
            attached = bool(user32.AttachThreadInput(current_tid, target_tid, True))
        user32.SetFocus(control_hwnd)
    finally:
        if attached:
            user32.AttachThreadInput(current_tid, target_tid, False)


def _paste_text_into_control(main_hwnd: int, control_hwnd: int, text: str) -> bool:
    _copy_text_to_clipboard(text)
    _focus_cuteftp_control(main_hwnd, control_hwnd)
    _press_ctrl_key(ord("A"))
    _press_ctrl_key(ord("V"))
    time.sleep(0.15)
    return _get_edit_text(control_hwnd).strip() == text


def _find_cuteftp_dialog(main_hwnd: int, timeout: float = 2.5) -> int | None:
    _, process_id = win32process.GetWindowThreadProcessId(main_hwnd)
    deadline = time.monotonic() + timeout

    while time.monotonic() < deadline:
        dialogs = []

        def _enum_window(hwnd, _):
            if hwnd == main_hwnd or not win32gui.IsWindowVisible(hwnd):
                return
            try:
                _, pid = win32process.GetWindowThreadProcessId(hwnd)
                if pid != process_id:
                    return
                owner = win32gui.GetWindow(hwnd, win32con.GW_OWNER)
                if owner == main_hwnd or win32gui.GetClassName(hwnd) == "#32770":
                    dialogs.append(hwnd)
            except Exception:
                return

        win32gui.EnumWindows(_enum_window, None)
        if dialogs:
            return dialogs[0]
        time.sleep(0.08)

    return None


def _find_dialog_edit(dialog_hwnd: int) -> int | None:
    candidates = []

    def _enum_child(hwnd, _):
        try:
            cls_name = win32gui.GetClassName(hwnd)
            if cls_name not in ("Edit", "ComboBox", "ComboBoxEx32"):
                return
            left, top, right, bottom = win32gui.GetWindowRect(hwnd)
            width, height = right - left, bottom - top
            if width >= 100 and 12 <= height <= 60:
                candidates.append((width, hwnd))
        except Exception:
            return

    win32gui.EnumChildWindows(dialog_hwnd, _enum_child, None)
    candidates.sort(reverse=True)
    return candidates[0][1] if candidates else None


def _navigate_using_cuteftp_dialog(
    main_hwnd: int,
    remote_listview: int | None,
    target_path: str,
) -> bool:
    """CuteFTP 9 documents Ctrl+D as its Change Remote Folder command."""
    _focus_cuteftp_control(main_hwnd, remote_listview or main_hwnd)
    _press_ctrl_key(ord("D"))

    dialog_hwnd = _find_cuteftp_dialog(main_hwnd)
    if not dialog_hwnd:
        return False

    dialog_edit = _find_dialog_edit(dialog_hwnd)
    if not dialog_edit:
        return False

    if not _paste_text_into_control(dialog_hwnd, dialog_edit, target_path):
        return False

    # IDOK is the standard OK command for a Windows dialog.
    user32.SendMessageW(dialog_hwnd, win32con.WM_COMMAND, 1, 0)

    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        if not win32gui.IsWindow(dialog_hwnd) or not win32gui.IsWindowVisible(dialog_hwnd):
            time.sleep(1.5)
            _, address_edit = _find_remote_controls(main_hwnd)
            current = _get_edit_text(address_edit).strip() if address_edit else ""
            return bool(current and _paths_equal(current, target_path))
        time.sleep(0.1)

    return False


def _navigate_cuteftp_gui_to_path(
    main_hwnd: int,
    edit_hwnd: int | None,
    remote_listview: int | None,
    target_remote_dir: str,
) -> str:
    if not target_remote_dir or target_remote_dir == "__CURRENT__":
        current = _get_edit_text(edit_hwnd).strip() if edit_hwnd else ""
        return _normalise_remote_path(current)

    target = _normalise_remote_path(target_remote_dir)
    if not target:
        raise RuntimeError("مسیر مقصد خالی است.")

    current = _get_edit_text(edit_hwnd).strip() if edit_hwnd else ""
    if current and _paths_equal(current, target):
        return target

    # First use the actual Remote address Edit. Real Ctrl+A / Ctrl+V / Enter
    # generates the control notifications that WM_SETTEXT + PostMessage missed.
    if edit_hwnd:
        try:
            if _paste_text_into_control(main_hwnd, edit_hwnd, target):
                _press_key(win32con.VK_RETURN)
                time.sleep(2.0)
                if _paths_equal(_get_edit_text(edit_hwnd).strip(), target):
                    return target
        except Exception:
            logger.exception("Direct CuteFTP Remote address navigation failed.")

    # Fallback to CuteFTP's own Change Remote Folder dialog (Ctrl+D).
    if _navigate_using_cuteftp_dialog(main_hwnd, remote_listview, target):
        return target

    raise RuntimeError(
        f"مسیر مقصد [{target}] در CuteFTP اعمال و تأیید نشد؛ "
        "برای جلوگیری از آپلود در پوشه قبلی، هیچ فایلی ارسال نشد."
    )


def upload_via_open_cuteftp_gui(
    local_target: Path,
    target_remote_dir: str = "__CURRENT__",
) -> tuple[bool, str]:
    """Upload through the user's already-authenticated, open CuteFTP session."""
    local_target = Path(local_target).resolve()
    if not local_target.exists():
        raise FileNotFoundError(f"فایل یا پوشه محلی پیدا نشد: {local_target}")

    with _CUTEFTP_GUI_LOCK:
        main_hwnd = _find_cuteftp_gui_window()
        if not main_hwnd:
            raise RuntimeError(
                "پنجره CuteFTP پیدا نشد. برنامه باید در همان نشست تعاملی ویندوز باز باشد."
            )

        remote_pane_hwnd, remote_edit_hwnd = _find_remote_controls(main_hwnd)
        if not remote_pane_hwnd:
            raise RuntimeError(
                "پنل Remote در CuteFTP پیدا نشد؛ آپلود برای جلوگیری از مقصد اشتباه لغو شد."
            )

        actual_remote_path = _navigate_cuteftp_gui_to_path(
            main_hwnd,
            remote_edit_hwnd,
            remote_pane_hwnd,
            target_remote_dir,
        )
        if not actual_remote_path:
            raise RuntimeError(
                "مسیر فعلی Remote قابل تشخیص نیست؛ آپلود در مقصد نامشخص انجام نشد."
            )

        # Reacquire after navigation in case CuteFTP rebuilt its child controls.
        remote_pane_hwnd, remote_edit_hwnd = _find_remote_controls(main_hwnd)
        if not remote_pane_hwnd:
            raise RuntimeError("پس از تغییر مسیر، پنل Remote پیدا نشد؛ آپلود لغو شد.")

        if target_remote_dir and target_remote_dir != "__CURRENT__":
            visible_path = _get_edit_text(remote_edit_hwnd).strip() if remote_edit_hwnd else ""
            if not visible_path or not _paths_equal(visible_path, actual_remote_path):
                raise RuntimeError(
                    "مسیر قابل مشاهده در نوار Remote با مقصد انتخابی مطابقت ندارد؛ آپلود لغو شد."
                )

        # CF_HDROP + Ctrl+V is the native CuteFTP clipboard-upload workflow.
        _copy_files_to_windows_clipboard([str(local_target)])
        _focus_cuteftp_control(main_hwnd, remote_pane_hwnd)
        _press_ctrl_key(ord("V"))
        time.sleep(0.5)

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
                return _normalise_remote_path(txt)
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