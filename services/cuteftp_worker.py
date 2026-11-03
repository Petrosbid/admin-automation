import struct
import time
import urllib.parse
from pathlib import Path
import pythoncom
import win32api
import win32clipboard
import win32com.client
import win32con
import win32gui
from core.config_loader import cfg


def _copy_files_to_windows_clipboard(file_paths: list[str]):
    """
    قرار دادن پوشه یا فایل در کلیپ‌بورد ویندوز با فرمت بومی CF_HDROP
    (دقیقاً مشابه زدن Ctrl+C روی پوشه در Windows Explorer)
    """
    # ساختار DROPFILES در ویندوز: 20 بایت هدر + مسیرهای یونیکد (UTF-16LE) پایان یافته با Null
    p_files_offset = 20
    f_wide = 1  # Unicode = True
    dropfiles_header = struct.pack("IiiII", p_files_offset, 0, 0, 0, f_wide)

    encoded_paths = b"".join(p.encode("utf-16le") + b"\x00\x00" for p in file_paths) + b"\x00\x00"
    hdrop_data = dropfiles_header + encoded_paths

    win32clipboard.OpenClipboard()
    try:
        win32clipboard.EmptyClipboard()
        win32clipboard.SetClipboardData(win32con.CF_HDROP, hdrop_data)
    finally:
        win32clipboard.CloseClipboard()


def _find_cuteftp_gui_window() -> int | None:
    """پیدا کردن پنجره باز نرم‌افزار CuteFTP روی دسکتاپ ویندوز سرور"""
    found_hwnds = []

    def _enum_cb(hwnd, _):
        if win32gui.IsWindowVisible(hwnd):
            title = win32gui.GetWindowText(hwnd)
            if "cuteftp" in title.lower():
                found_hwnds.append(hwnd)

    win32gui.EnumWindows(_enum_cb, None)
    return found_hwnds[0] if found_hwnds else None


def _find_remote_listview_pane(main_hwnd: int) -> int | None:
    """
    پیدا کردن پنل سمت راست (Remote Pane - SysListView32) در پنجره باز CuteFTP
    """
    listviews = []

    def _enum_child(hwnd, _):
        cls_name = win32gui.GetClassName(hwnd)
        if cls_name == "SysListView32" and win32gui.IsWindowVisible(hwnd):
            rect = win32gui.GetWindowRect(hwnd)
            width = rect[2] - rect[0]
            height = rect[3] - rect[1]
            if width > 100 and height > 100:
                # ذخیره مختصات افقی (left) برای تشخیص پنل سمت راست (Remote)
                listviews.append((rect[0], hwnd))

    win32gui.EnumChildWindows(main_hwnd, _enum_child, None)
    if not listviews:
        return None
    # در CuteFTP پنل Remote در سمت راست قرار دارد (بزرگترین مختصات X)
    listviews.sort(key=lambda x: x[0], reverse=True)
    return listviews[0][1]


def upload_via_open_cuteftp_gui(local_target: Path) -> bool:
    """
    آپلود پوشه یا فایل از طریق پنجره گرافیکی بازِ CuteFTP (بدون نیاز به پسورد در کانفیگ)
    """
    main_hwnd = _find_cuteftp_gui_window()
    if not main_hwnd:
        return False

    # ۱. کپی کردن پوشه یا فایل به کلیپ‌بورد ویندوز
    _copy_files_to_windows_clipboard([str(local_target.resolve())])

    # ۲. بالا آوردن پنجره CuteFTP در صورت Minimize بودن
    if win32gui.IsIconic(main_hwnd):
        win32gui.ShowWindow(main_hwnd, win32con.SW_RESTORE)
    try:
        win32gui.SetForegroundWindow(main_hwnd)
    except Exception:
        pass
    time.sleep(0.4)

    # ۳. فوکوس روی پنل Remote (سمت راست) و ارسال دستور Paste
    remote_pane_hwnd = _find_remote_listview_pane(main_hwnd)
    target_hwnd = remote_pane_hwnd or main_hwnd

    try:
        rect = win32gui.GetWindowRect(target_hwnd)
        center_x = (rect[0] + rect[2]) // 2
        center_y = (rect[1] + rect[3]) // 2
        # کلیک ملایم وسط پنل Remote برای اطمینان از فوکوس بودن پنل هاست مقصد
        lparam = win32api.MAKELONG(center_x - rect[0], center_y - rect[1])
        win32gui.PostMessage(target_hwnd, win32con.WM_LBUTTONDOWN, win32con.MK_LBUTTON, lparam)
        win32gui.PostMessage(target_hwnd, win32con.WM_LBUTTONUP, 0, lparam)
        time.sleep(0.3)
    except Exception:
        pass

    # ارسال استاندارد دستور Paste (ID_EDIT_PASTE = 0xE125 در برنامه‌های MFC مثل CuteFTP) + کلید Ctrl+V
    win32gui.PostMessage(main_hwnd, win32con.WM_COMMAND, 0xE125, 0)
    time.sleep(0.2)

    # ارسال کلید ترکیبی Ctrl+V برای اطمینان کامل
    win32api.keybd_event(win32con.VK_CONTROL, 0, 0, 0)
    win32api.keybd_event(ord("V"), 0, 0, 0)
    time.sleep(0.1)
    win32api.keybd_event(ord("V"), 0, win32con.KEYEVENTF_KEYUP, 0)
    win32api.keybd_event(win32con.VK_CONTROL, 0, win32con.KEYEVENTF_KEYUP, 0)

    return True


def sync_upload_cuteftp(local_target: Path, remote_subpath: str = "") -> list[str]:
    """
    تلاش برای آپلود:
    ۱. اگر پسورد در config.yaml خالی باشد یا پنجره CuteFTP GUI باز باشد -> ارسال مستقیم به GUI بازِ CuteFTP
    ۲. در غیر این صورت یا در صورت فعال بودن نشست COM -> استفاده از CuteFTPPro.TEConnection
    """
    ftp_cfg = cfg.get("cuteftp") or {}
    base_url = str(ftp_cfg.get("base_download_url", "https://dl.example.com/media")).rstrip("/")
    password = str(ftp_cfg.get("password", "")).strip()
    has_valid_password = bool(password and password != "ftppassword")

    # تابع کمکی ساخت لینک‌های دانلود خروجی
    def _generate_links() -> list[str]:
        urls = []
        if local_target.is_file():
            rel_url = f"{remote_subpath}/{local_target.name}".lstrip("/")
            urls.append(f"{base_url}/{urllib.parse.quote(rel_url)}")
        else:
            for file_p in local_target.rglob("*"):
                if file_p.is_file() and not file_p.name.startswith("_temp_"):
                    rel = file_p.relative_to(local_target.parent).as_posix()
                    if remote_subpath:
                        rel = f"{remote_subpath.strip('/')}/{rel}"
                    urls.append(f"{base_url}/{urllib.parse.quote(rel)}")
        return urls

    # ۱. اگر پسورد نداریم، ابتدا مستقیماً از طریق پنجره GUI بازِ CuteFTP آپلود کن
    if not has_valid_password:
        if upload_via_open_cuteftp_gui(local_target):
            return _generate_links()

    # ۲. تلاش از طریق موتور COM (استفاده از نشستِ از قبل متصل‌شده یا اتصال با مشخصات)
    pythoncom.CoInitialize()
    try:
        te = win32com.client.Dispatch("CuteFTPPro.TEConnection")
        te.Option("ThrowError", False)

        # فقط اگر از قبل متصل نیست و مشخصات داریم، فیلدها را مقداردهی کن
        if not bool(getattr(te, "IsConnected", False)):
            if ftp_cfg.get("host") and ftp_cfg.get("host") != "ftp.dl.example.com":
                te.Protocol = ftp_cfg.get("protocol", "FTP")
                te.Host = ftp_cfg["host"]
                te.Port = int(ftp_cfg.get("port", 21))
            if ftp_cfg.get("username") and ftp_cfg.get("username") != "ftpuser":
                te.Login = ftp_cfg["username"]
            if has_valid_password:
                te.Password = password
            te.Connect()

        base_remote = str(ftp_cfg.get("remote_base_dir", "/")).rstrip("/")
        target_remote_dir = f"{base_remote}/{remote_subpath}".rstrip("/") or "/"

        if local_target.is_file():
            te.RemoteFolder = target_remote_dir
            te.Upload(str(local_target))
        else:
            folder_name = local_target.name
            movie_remote_dir = f"{target_remote_dir}/{folder_name}"
            if not te.RemoteExists(movie_remote_dir):
                te.CreateRemoteFolder(movie_remote_dir)
            te.RemoteFolder = movie_remote_dir
            te.Upload(f"{local_target}\\*.*")

        return _generate_links()
    except Exception as com_err:
        # فال‌بک نهایی به پنجره گرافیکی CuteFTP
        if upload_via_open_cuteftp_gui(local_target):
            return _generate_links()
        raise RuntimeError(
            f"خطا در ارتباط با CuteFTP: مطمئن شوید پنجره نرم‌افزار CuteFTP روی سرور باز و به هاست متصل است. ({com_err})"
        )
    finally:
        pythoncom.CoUninitialize()