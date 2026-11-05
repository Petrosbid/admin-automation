import ctypes
from ftplib import FTP
import logging
import struct
import threading
import time
import urllib.parse
from pathlib import Path
from typing import Callable
import pythoncom
import win32api
import win32clipboard
import win32com.client
import win32con
import win32gui
import win32process
from core.config_loader import cfg

user32 = ctypes.windll.user32

# قفل سراسری برای تضمین اینکه هرگز دو آپلود هم‌زمان به CuteFTP ارسال نشوند
_CUTEFTP_SERIAL_LOCK = threading.Lock()


def _open_clipboard_safe(retries: int = 10, delay: float = 0.05):
    for i in range(retries):
        try:
            win32clipboard.OpenClipboard()
            return
        except Exception:
            if i == retries - 1:
                raise
            time.sleep(delay)


def _copy_text_to_windows_clipboard(text: str):
    _open_clipboard_safe()
    try:
        win32clipboard.EmptyClipboard()
        win32clipboard.SetClipboardData(win32con.CF_UNICODETEXT, text)
    finally:
        win32clipboard.CloseClipboard()


def _copy_files_to_windows_clipboard(file_paths: list[str]):
    p_files_offset = 20
    f_wide = 1
    dropfiles_header = struct.pack("IiiII", p_files_offset, 0, 0, 0, f_wide)

    encoded_paths = b"".join(p.encode("utf-16le") + b"\x00\x00" for p in file_paths) + b"\x00\x00"
    hdrop_data = dropfiles_header + encoded_paths

    _open_clipboard_safe()
    try:
        win32clipboard.EmptyClipboard()
        win32clipboard.SetClipboardData(win32con.CF_HDROP, hdrop_data)
    finally:
        win32clipboard.CloseClipboard()


def _send_ctrl_key(letter: str):
    vk_code = ord(letter.upper())
    win32api.keybd_event(win32con.VK_CONTROL, 0, 0, 0)
    time.sleep(0.04)
    win32api.keybd_event(vk_code, 0, 0, 0)
    time.sleep(0.05)
    win32api.keybd_event(vk_code, 0, win32con.KEYEVENTF_KEYUP, 0)
    time.sleep(0.04)
    win32api.keybd_event(win32con.VK_CONTROL, 0, win32con.KEYEVENTF_KEYUP, 0)


def _send_enter_key():
    win32api.keybd_event(win32con.VK_RETURN, 0, 0, 0)
    time.sleep(0.05)
    win32api.keybd_event(win32con.VK_RETURN, 0, win32con.KEYEVENTF_KEYUP, 0)


def _get_window_text_safe(hwnd: int) -> str:
    try:
        length = user32.SendMessageW(hwnd, win32con.WM_GETTEXTLENGTH, 0, 0)
        if length <= 0:
            return ""
        buf = ctypes.create_unicode_buffer(length + 2)
        user32.SendMessageW(hwnd, win32con.WM_GETTEXT, length + 1, buf)
        return buf.value.strip()
    except Exception:
        return ""


def _find_cuteftp_gui_window() -> int | None:
    found_hwnds = []

    def _enum_cb(hwnd, _):
        if win32gui.IsWindowVisible(hwnd):
            title = win32gui.GetWindowText(hwnd)
            if "cuteftp" in title.lower():
                found_hwnds.append(hwnd)

    win32gui.EnumWindows(_enum_cb, None)
    return found_hwnds[0] if found_hwnds else None


def _wait_until_cuteftp_responsive(main_hwnd: int, timeout_sec: float = 600.0):
    """
    صبر کردن تا زمانی که پنجره CuteFTP از حالت فریز خارج شود (برای زمانی که مشغول ساخت پوشه یا آپلود فایل سنگین است)
    """
    start_t = time.time()
    res = ctypes.c_ulong()
    while time.time() - start_t < timeout_sec:
        ok = user32.SendMessageTimeoutW(
            main_hwnd,
            win32con.WM_NULL,
            0,
            0,
            0x0002,  # SMTO_ABORTIFHUNG
            2000,
            ctypes.byref(res)
        )
        if ok:
            return True
        time.sleep(1.0)
    return False


def _calc_target_total_bytes(local_target: Path) -> int:
    if local_target.is_file():
        return local_target.stat().st_size
    return sum(f.stat().st_size for f in local_target.rglob("*") if f.is_file() and not f.name.startswith("_temp_"))


def _wait_for_gui_transfer_completion(
    main_hwnd: int,
    local_target: Path,
    progress_callback: Callable[[str], None] = None
):
    """
    پایش هوشمند اتمام آپلود در حالت GUI:
    ۱. صبر می‌کند تا پنجره CuteFTP اگر فریز شده کاملاً آزاد شود.
    ۲. بر اساس حجم فایل و سرعت انتقال سرور (~45 MB/s در شبکه سرور دانلود شما)،
       صبر می‌کند تا انتقال فایل فعلی به طور کامل تمام شود و سپس اجازه رفتن به پوشه مقصد بعدی را می‌دهد.
    """
    total_bytes = _calc_target_total_bytes(local_target)
    total_mb = total_bytes / (1024 * 1024)

    # تخمین زمان انتقال بر اساس حجم فایل (با حداقل ۴ ثانیه برای فایل‌های کوچک و زمان متناسب برای فایل‌های حجیم)
    estimated_seconds = max(4.0, (total_mb / 35.0) + 3.5)
    start_t = time.time()

    while True:
        _wait_until_cuteftp_responsive(main_hwnd, timeout_sec=300.0)
        elapsed = time.time() - start_t
        if elapsed >= estimated_seconds:
            break
        pct = min(99.0, (elapsed / estimated_seconds) * 100.0)
        if progress_callback:
            progress_callback(f"در حال آپلود در CuteFTP: {pct:.0f}% ({total_mb:.1f} MB)...")
        time.sleep(1.2)

    _wait_until_cuteftp_responsive(main_hwnd, timeout_sec=120.0)


def _find_remote_controls(main_hwnd: int) -> tuple[int | None, int | None]:
    listviews = []
    slash_edits = []

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
            listviews.append((w_rect[0], w_rect[1], hwnd))
        elif cls_name == "Edit" and w > 100 and 12 <= h <= 45:
            txt = _get_window_text_safe(hwnd)
            if txt.startswith("/"):
                slash_edits.append((w_rect[0], hwnd))

    win32gui.EnumChildWindows(main_hwnd, _enum_child, None)

    remote_edit = None
    if slash_edits:
        slash_edits.sort(key=lambda x: x[0], reverse=True)
        remote_edit = slash_edits[0][1]

    remote_listview = None
    if listviews:
        if len(listviews) >= 3:
            listviews.sort(key=lambda x: x[1])
            top_listviews = listviews[:-1]
        else:
            top_listviews = listviews
        top_listviews.sort(key=lambda x: x[0], reverse=True)
        remote_listview = top_listviews[0][2]

    return remote_listview, remote_edit


def extract_credentials_from_open_cuteftp() -> dict[str, str]:
    info = {
        "site_profile": "",
        "host": "",
        "username": "",
        "password": "",
        "port": "21",
        "remote_dir": "/English/Movies",
    }

    main_hwnd = _find_cuteftp_gui_window()
    if not main_hwnd:
        return info

    win_title = win32gui.GetWindowText(main_hwnd)
    if "[" in win_title and "]" in win_title:
        bracket_part = win_title.split("[", 1)[1].split("]", 1)[0]
        if " - " in bracket_part:
            prof, rest = bracket_part.split(" - ", 1)
            info["site_profile"] = prof.strip()
            host_candidate = rest.split(",", 1)[0].strip()
            if host_candidate:
                info["host"] = host_candidate

    edits_with_pos = []

    def _enum_child(hwnd, _):
        if not win32gui.IsWindowVisible(hwnd):
            return
        if win32gui.GetClassName(hwnd) == "Edit":
            try:
                rect = win32gui.GetWindowRect(hwnd)
                style = win32gui.GetWindowLong(hwnd, win32con.GWL_STYLE)
                is_password_box = bool(style & 0x0020)
                txt = _get_window_text_safe(hwnd)
                edits_with_pos.append((rect[1], rect[0], txt, is_password_box, hwnd))
            except Exception:
                pass

    win32gui.EnumChildWindows(main_hwnd, _enum_child, None)

    for _, _, txt, is_pwd, _ in edits_with_pos:
        if not txt:
            continue
        if is_pwd:
            info["password"] = txt
        elif txt.startswith("/"):
            info["remote_dir"] = txt
        elif "@" in txt and not txt.startswith("C:"):
            info["username"] = txt
        elif txt.isdigit() and 1 <= int(txt) <= 65535:
            info["port"] = txt
        elif txt.count(".") == 3 and all(p.isdigit() for p in txt.split(".")):
            info["host"] = txt

    if info["password"]:
        try:
            if info["host"]:
                cfg.set_value("cuteftp", "host", info["host"])
            if info["username"]:
                cfg.set_value("cuteftp", "username", info["username"])
            cfg.set_value("cuteftp", "password", info["password"])
        except Exception:
            pass

    return info


def get_current_cuteftp_remote_dir() -> str:
    extracted = extract_credentials_from_open_cuteftp()
    return extracted.get("remote_dir") or cfg.get("cuteftp", "remote_base_dir", "/English/Movies")


def _ensure_ftp_remote_dir(ftp: FTP, remote_dir: str):
    normalized = "/" + remote_dir.replace("\\", "/").strip().lstrip("/")
    ftp.cwd("/")
    if normalized == "/":
        return

    for part in normalized.strip("/").split("/"):
        if not part:
            continue
        try:
            ftp.cwd(part)
        except Exception:
            try:
                ftp.mkd(part)
            except Exception:
                pass
            ftp.cwd(part)


def _upload_via_direct_ftp(
    host: str,
    port: int,
    username: str,
    password: str,
    local_target: Path,
    target_remote_dir: str,
    progress_callback: Callable[[str], None] = None
) -> str:
    """آپلود سنکرون و دقیق که تا پایان کامل انتقال ۱۰۰٪ بایت‌های فایل منتظر می‌ماند"""
    ftp = FTP()
    ftp.encoding = "utf-8"
    ftp.connect(host=host, port=port, timeout=120)
    ftp.login(user=username, passwd=password)
    ftp.set_pasv(True)

    try:
        _ensure_ftp_remote_dir(ftp, target_remote_dir)

        if local_target.is_file():
            total_size = max(1, local_target.stat().st_size)
            uploaded = 0
            last_update = 0.0

            def _chunk_cb(chunk: bytes):
                nonlocal uploaded, last_update
                uploaded += len(chunk)
                now = time.time()
                if progress_callback and (now - last_update >= 1.5):
                    last_update = now
                    pct = (uploaded / total_size) * 100.0
                    progress_callback(f"{pct:.1f}% ({uploaded / (1024*1024):.1f} / {total_size / (1024*1024):.1f} MB)")

            with open(local_target, "rb") as f:
                ftp.storbinary(f"STOR {local_target.name}", f, blocksize=1024 * 256, callback=_chunk_cb)
        else:
            movie_remote_root = f"{target_remote_dir.rstrip('/')}/{local_target.name}"
            _ensure_ftp_remote_dir(ftp, movie_remote_root)

            all_files = [
                p for p in local_target.rglob("*")
                if p.is_file() and not p.name.startswith("_temp_")
            ]
            total_files = len(all_files)

            for idx, file_p in enumerate(all_files, start=1):
                rel_parent = file_p.parent.relative_to(local_target).as_posix()
                dest_folder = movie_remote_root if rel_parent == "." else f"{movie_remote_root}/{rel_parent}"
                _ensure_ftp_remote_dir(ftp, dest_folder)

                f_size = max(1, file_p.stat().st_size)
                sent = 0
                last_t = 0.0

                def _file_cb(chunk: bytes):
                    nonlocal sent, last_t
                    sent += len(chunk)
                    now = time.time()
                    if progress_callback and (now - last_t >= 1.5):
                        last_t = now
                        pct = (sent / f_size) * 100.0
                        progress_callback(f"فایل {idx}/{total_files}: {pct:.1f}% ({file_p.name[:25]})")

                with open(file_p, "rb") as f:
                    ftp.storbinary(f"STOR {file_p.name}", f, blocksize=1024 * 256, callback=_file_cb)

        return target_remote_dir
    finally:
        try:
            ftp.quit()
        except Exception:
            ftp.close()


def _bring_cuteftp_to_front_and_focus(main_hwnd: int, remote_pane_hwnd: int | None):
    _wait_until_cuteftp_responsive(main_hwnd, timeout_sec=120.0)

    if win32gui.IsIconic(main_hwnd):
        win32gui.ShowWindow(main_hwnd, win32con.SW_RESTORE)
        time.sleep(0.25)

    fg_hwnd = win32gui.GetForegroundWindow()
    target_tid, _ = win32process.GetWindowThreadProcessId(main_hwnd)
    cur_tid = win32api.GetCurrentThreadId()
    fg_tid = win32process.GetWindowThreadProcessId(fg_hwnd)[0] if fg_hwnd else 0

    attached_cur = False
    attached_fg = False
    try:
        if cur_tid != target_tid:
            attached_cur = bool(user32.AttachThreadInput(cur_tid, target_tid, True))
        if fg_tid and fg_tid != target_tid and fg_tid != cur_tid:
            attached_fg = bool(user32.AttachThreadInput(fg_tid, target_tid, True))

        user32.ReleaseCapture()

        if win32gui.GetForegroundWindow() != main_hwnd:
            win32api.keybd_event(win32con.VK_MENU, 0, 0, 0)
            win32api.keybd_event(win32con.VK_MENU, 0, win32con.KEYEVENTF_KEYUP, 0)
            user32.BringWindowToTop(main_hwnd)
            user32.SetForegroundWindow(main_hwnd)
            time.sleep(0.2)

        if remote_pane_hwnd:
            parent_view = win32gui.GetParent(remote_pane_hwnd)
            if parent_view:
                user32.SetFocus(parent_view)
            user32.SetFocus(remote_pane_hwnd)
            time.sleep(0.15)
        else:
            user32.SetFocus(main_hwnd)
    finally:
        if attached_fg:
            user32.AttachThreadInput(fg_tid, target_tid, False)
        if attached_cur:
            user32.AttachThreadInput(cur_tid, target_tid, False)


def _upload_via_open_cuteftp_gui(
    local_target: Path,
    target_remote_dir: str,
    change_dir: bool = True,
    progress_callback: Callable[[str], None] = None
) -> str:
    """
    آپلود ترتیبی از طریق پنجره بازِ CuteFTP همراه با صبر کامل تا اتمام آپلود و آزاد شدن برنامه از فریز
    """
    main_hwnd = _find_cuteftp_gui_window()
    if not main_hwnd:
        raise RuntimeError("پنجره باز CuteFTP روی دسکتاپ یافت نشد.")

    # ۱. ابتدا صبر کن اگر CuteFTP از عملیات قبلی فریز است، کاملاً آزاد شود
    _wait_until_cuteftp_responsive(main_hwnd, timeout_sec=300.0)

    remote_pane_hwnd, remote_edit_hwnd = _find_remote_controls(main_hwnd)
    _bring_cuteftp_to_front_and_focus(main_hwnd, remote_pane_hwnd)

    if change_dir and target_remote_dir:
        current_in_box = _get_window_text_safe(remote_edit_hwnd) if remote_edit_hwnd else ""
        if current_in_box.rstrip("/").lower() != target_remote_dir.rstrip("/").lower():
            if progress_callback:
                progress_callback(f"تغییر دایرکتوری CuteFTP به {target_remote_dir}...")
            _copy_text_to_windows_clipboard(target_remote_dir)
            time.sleep(0.15)
            _send_ctrl_key("D")
            time.sleep(0.45)
            _send_ctrl_key("V")
            time.sleep(0.25)
            _send_enter_key()
            time.sleep(2.2)
            _wait_until_cuteftp_responsive(main_hwnd, timeout_sec=60.0)

    _bring_cuteftp_to_front_and_focus(main_hwnd, remote_pane_hwnd)
    _copy_files_to_windows_clipboard([str(local_target.resolve())])
    time.sleep(0.25)

    _send_ctrl_key("V")
    time.sleep(0.5)

    # ۲. صبر قطعی تا پایان آپلود این فایل/پوشه قبل از رفتن سراغ دایرکتوری بعدی
    _wait_for_gui_transfer_completion(main_hwnd, local_target, progress_callback=progress_callback)

    if remote_edit_hwnd:
        read_p = _get_window_text_safe(remote_edit_hwnd)
        if read_p:
            return read_p
    return target_remote_dir


def sync_upload_cuteftp(
    local_target: Path,
    remote_subpath: str = "__CURRENT__",
    progress_callback: Callable[[str], None] = None
) -> list[str]:
    """
    اجرای ترتیبی و انحصاری آپلود (با قفل _CUTEFTP_SERIAL_LOCK) تا هرگز دو آپلود با هم تداخل نکنند
    """
    with _CUTEFTP_SERIAL_LOCK:
        ftp_cfg = cfg.get("cuteftp") or {}
        extracted = extract_credentials_from_open_cuteftp()

        host = extracted.get("host") or ftp_cfg.get("host", "185.130.50.130")
        port = int(extracted.get("port") or ftp_cfg.get("port", 21))
        username = extracted.get("username") or ftp_cfg.get("username", "@dl15.cdnzed.sbs")
        password = extracted.get("password") or str(ftp_cfg.get("password", "")).strip()
        if password == "ftppassword":
            password = ""

        base_url = str(ftp_cfg.get("base_download_url", "https://dl15.cdnzed.sbs")).rstrip("/")
        if "dl.example.com" in base_url and username.startswith("@"):
            base_url = f"https://{username.lstrip('@')}"

        is_current_dir_mode = (not remote_subpath or remote_subpath == "__CURRENT__")
        if is_current_dir_mode:
            target_remote_dir = extracted.get("remote_dir") or ftp_cfg.get("remote_base_dir", "/English/Movies")
        else:
            target_remote_dir = "/" + remote_subpath.replace("\\", "/").strip().lstrip("/")

        if len(target_remote_dir) > 1:
            target_remote_dir = target_remote_dir.rstrip("/")

        def _generate_links(final_remote_dir: str) -> list[str]:
            clean_sub = final_remote_dir.replace("\\", "/")
            for pub_prefix in ("/public_html", "/www", "/htdocs"):
                if clean_sub.startswith(pub_prefix):
                    clean_sub = clean_sub[len(pub_prefix):]
            clean_sub = clean_sub.strip("/")

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

        errors = []

        # لایه ۱: آپلود مستقیم FTP (که به صورت ذاتی تا اتمام ۱۰۰٪ فایل صبر می‌کند و پوشه مقصد را هم خودش می‌سازد)
        if password:
            try:
                used_dir = _upload_via_direct_ftp(
                    host=host,
                    port=port,
                    username=username,
                    password=password,
                    local_target=local_target,
                    target_remote_dir=target_remote_dir,
                    progress_callback=progress_callback
                )
                return _generate_links(used_dir)
            except Exception as e1:
                logging.warning(f"Direct FTP upload warning: {e1}")
                errors.append(f"DirectFTP: {e1}")

        # لایه ۲: آپلود از طریق پنجره گرافیکی CuteFTP با انتظار کامل تا پایان انتقال و آزاد شدن از فریز
        try:
            used_dir = _upload_via_open_cuteftp_gui(
                local_target=local_target,
                target_remote_dir=target_remote_dir,
                change_dir=(not is_current_dir_mode),
                progress_callback=progress_callback
            )
            return _generate_links(used_dir)
        except Exception as e_gui:
            logging.warning(f"CuteFTP GUI automation warning: {e_gui}")
            errors.append(f"GUI: {e_gui}")

        raise RuntimeError(
            "خطا در آپلود به سرور دانلود!\n"
            f"جزئیات: {' | '.join(errors)}"
        )