import ctypes
from ftplib import FTP
import logging
import struct
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
from core.config_loader import cfg

user32 = ctypes.windll.user32


def _get_window_text_safe(hwnd: int) -> str:
    """خواندن متن کنترل‌های Edit در ویندوز (حتی کادرهای Password با استایل ES_PASSWORD در همان نشست کاربری)"""
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


def extract_credentials_from_open_cuteftp() -> dict[str, str]:
    """
    استخراج هوشمند Host، Username، Password، Port، نام پروفایل Site Manager (مثلاً dl15)
    و مسیر فعلی Remote مستقیماً از پنجره بازِ CuteFTP 9.3 روی سرور!
    """
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

    # ۱. استخراج نام پروفایل Site Manager و IP از تایتل پنجره اصلی:
    # مثال تایتل در عکس شما: Globalscape - CuteFTP 9.3 - [dl15 - 185.130.50.130, Status: Connected]
    win_title = win32gui.GetWindowText(main_hwnd)
    if "[" in win_title and "]" in win_title:
        bracket_part = win_title.split("[", 1)[1].split("]", 1)[0]
        if " - " in bracket_part:
            prof, rest = bracket_part.split(" - ", 1)
            info["site_profile"] = prof.strip()
            host_candidate = rest.split(",", 1)[0].strip()
            if host_candidate:
                info["host"] = host_candidate

    # ۲. پیمایش تمام کادرهای متنی (Edit) در نوار ابزار بالای CuteFTP (Host, Username, Password, Port, Remote Path)
    edits_with_pos = []

    def _enum_child(hwnd, _):
        if not win32gui.IsWindowVisible(hwnd):
            return
        cls_name = win32gui.GetClassName(hwnd)
        if cls_name == "Edit":
            try:
                rect = win32gui.GetWindowRect(hwnd)
                style = win32gui.GetWindowLong(hwnd, win32con.GWL_STYLE)
                is_password_box = bool(style & 0x0020)  # ES_PASSWORD = 0x0020
                txt = _get_window_text_safe(hwnd)
                edits_with_pos.append((rect[1], rect[0], txt, is_password_box, hwnd))
            except Exception:
                pass

    win32gui.EnumChildWindows(main_hwnd, _enum_child, None)

    for top_y, left_x, txt, is_pwd, _ in edits_with_pos:
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

    # اگر رمز عبور از کادر ستاره‌دار CuteFTP استخراج شد، آن را در config.yaml هم ذخیره کن تا همیشه در دسترس باشد
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
    """خواندن مسیر فعلی که پنجره باز CuteFTP روی آن قرار دارد"""
    extracted = extract_credentials_from_open_cuteftp()
    return extracted.get("remote_dir") or cfg.get("cuteftp", "remote_base_dir", "/English/Movies")


def _ensure_ftp_remote_dir(ftp: FTP, remote_dir: str):
    """ساخت مرحله‌به‌مرحله تمام پوشه‌ها و زیرپوشه‌های مسیر مقصد در هاست دانلود و ورود به آن"""
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
    """
    آپلود ۱۰۰٪ مستقیم، سریع و بدون خطا به هاست دانلود با ساخت خودکار پوشه‌ها و گزارش درصد پیشرفت لحظه‌ای
    """
    ftp = FTP()
    ftp.encoding = "utf-8"
    ftp.connect(host=host, port=port, timeout=60)
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
            # آپلود یک پوشه کامل به همراه تمام زیرپوشه‌های آن (softsub, hardsub, sub و غیره)
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


def _upload_via_cuteftp_com_engine(
    local_target: Path,
    target_remote_dir: str,
    extracted_info: dict[str, str],
    progress_callback: Callable[[str], None] = None
) -> str:
    """
    آپلود از طریق موتور COM رسمی CuteFTP 9 (CuteFTPPro.TEConnection)
    با استفاده از پروفایل ذخیره‌شده در Site Manager (مثلاً 'dl15') یا مشخصات استخراج‌شده از GUI
    """
    pythoncom.CoInitialize()
    try:
        ftp_cfg = cfg.get("cuteftp") or {}
        te = win32com.client.Dispatch("CuteFTPPro.TEConnection")
        te.Option("ThrowError", False)
        te.Option("AutoCloseMethod", 2)

        host = extracted_info.get("host") or ftp_cfg.get("host", "185.130.50.130")
        username = extracted_info.get("username") or ftp_cfg.get("username", "@dl15.cdnzed.sbs")
        password = extracted_info.get("password") or ftp_cfg.get("password", "")
        port = int(extracted_info.get("port") or ftp_cfg.get("port", 21))
        site_profile = extracted_info.get("site_profile") or ftp_cfg.get("site_profile", "dl15")

        if not bool(getattr(te, "IsConnected", False)):
            if password and password != "ftppassword":
                te.Protocol = ftp_cfg.get("protocol", "FTP")
                te.Host = host
                te.Port = port
                te.Login = username
                te.Password = password
                te.Connect()
            else:
                # اتصال مستقیم با نام سایت در Site Manager برنامه CuteFTP (بدون نیاز به پسورد!)
                try:
                    te.Connect(site_profile)
                except Exception:
                    te.Host = host
                    te.Port = port
                    te.Login = username
                    te.Connect()

        if progress_callback:
            progress_callback(f"ایجاد مسیر {target_remote_dir} در موتور CuteFTP...")

        # ساخت مرحله‌به‌مرحله پوشه مقصد در صورت عدم وجود
        accum = ""
        for part in target_remote_dir.strip("/").split("/"):
            if not part:
                continue
            accum = f"{accum}/{part}"
            if not te.RemoteExists(accum):
                te.CreateRemoteFolder(accum)

        if local_target.is_file():
            te.RemoteFolder = target_remote_dir
            te.Upload(str(local_target.resolve()))
        else:
            movie_remote_dir = f"{target_remote_dir.rstrip('/')}/{local_target.name}"
            if not te.RemoteExists(movie_remote_dir):
                te.CreateRemoteFolder(movie_remote_dir)
            te.RemoteFolder = movie_remote_dir
            te.Upload(f"{local_target.resolve()}\\*.*")

        return target_remote_dir
    finally:
        pythoncom.CoUninitialize()


def sync_upload_cuteftp(
    local_target: Path,
    remote_subpath: str = "__CURRENT__",
    progress_callback: Callable[[str], None] = None
) -> list[str]:
    """
    اجرای قطعی آپلود به هاست دانلود و تولید لینک‌های مستقیم:
    ۱. خواندن خودکار مشخصات و مسیر فعلی از پنجره بازِ CuteFTP 9.3
    ۲. آپلود مستقیم با موتور COM برنامه CuteFTP (پروفایل dl15 یا مشخصات استخراج‌شده)
    ۳. آپلود مستقیم FTP در صورت داشتن یا استخراج شدن پسورد
    """
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
        # تشخیص خودکار دامنه دانلود از روی نام کاربری (مثلاً @dl15.cdnzed.sbs -> https://dl15.cdnzed.sbs)
        base_url = f"https://{username.lstrip('@')}"

    if not remote_subpath or remote_subpath == "__CURRENT__":
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

    # روش ۱: اگر پسورد از پنجره باز CuteFTP استخراج شد یا در کانفیگ بود -> آپلود مستقیم و ۱۰۰٪ تضمینی با نمایش درصد پیشرفت
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
            logging.warning(f"Direct FTP upload attempt warning: {e1}")
            errors.append(f"DirectFTP: {e1}")

    # روش ۲: استفاده از موتور COM خودِ CuteFTP (متصل به پروفایل dl15 در Site Manager)
    try:
        used_dir = _upload_via_cuteftp_com_engine(
            local_target=local_target,
            target_remote_dir=target_remote_dir,
            extracted_info=extracted,
            progress_callback=progress_callback
        )
        return _generate_links(used_dir)
    except Exception as e2:
        logging.warning(f"CuteFTP COM Engine warning: {e2}")
        errors.append(f"COM: {e2}")

    raise RuntimeError(
        "خطا در آپلود به سرور دانلود!\n"
        f"جزئیات: {' | '.join(errors)}\n"
        "💡 راهنما: یک بار دستور /settings را در ربات بزنید و در بخش cuteftp بررسی کنید که host و password تنظیم شده باشند."
    )