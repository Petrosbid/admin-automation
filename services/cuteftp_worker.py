from ftplib import FTP, FTP_TLS, error_perm, all_errors
import logging
import re
import socket
import threading
import time
import urllib.parse
from pathlib import Path
from typing import Callable

from core.config_loader import cfg

# قفل انحصاری برای تضمین اجرای ۱۰۰٪ ترتیبی آپلودها (یک فایل در هر لحظه تا اتمام کامل)
_FTP_SERIAL_LOCK = threading.Lock()

# سایز بلاک بهینه برای انتقال سریع فایل‌های ویدئویی حجیم روی شبکه (512 کیلوبایت)
UPLOAD_BLOCK_SIZE = 512 * 1024
MAX_UPLOAD_RETRIES = 4

_EP_TAG_RE = re.compile(r"[Ss](\d{1,2})[\s._\-]*[Ee][Pp]?(\d{1,3})")


def _format_speed_and_eta(uploaded_bytes: int, total_bytes: int, elapsed_sec: float) -> str:
    if elapsed_sec <= 0.1 or uploaded_bytes <= 0:
        return "در حال محاسبه..."
    speed_bps = uploaded_bytes / elapsed_sec
    speed_mbps = speed_bps / (1024 * 1024)
    remaining_bytes = max(0, total_bytes - uploaded_bytes)
    eta_sec = int(remaining_bytes / speed_bps) if speed_bps > 0 else 0
    eta_m, eta_s = divmod(eta_sec, 60)
    return f"{speed_mbps:.1f} MB/s | ETA: {eta_m:02d}:{eta_s:02d}"


def _enable_socket_keepalive(sock: socket.socket | None):
    """فعال‌سازی Keep-Alive روی سوکت TCP برای جلوگیری از قطع شدن اتصال کنترل در فایل‌های حجیم"""
    if sock is None:
        return
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        if hasattr(socket, "SIO_KEEPALIVE_VALS"):
            sock.ioctl(socket.SIO_KEEPALIVE_VALS, (1, 30000, 10000))
    except Exception:
        pass


def _create_ftp_connection(
    host: str,
    port: int,
    username: str,
    password: str,
    protocol: str = "FTP",
    timeout: int = 120,
    passive_mode: bool = True
) -> FTP:
    """ایجاد اتصال پایدار FTP یا FTPS با استفاده از مشخصات ثبت‌شده در config.yaml"""
    use_tls = str(protocol).strip().upper() in ("FTPS", "FTPES", "TLS", "SSL")
    ftp = FTP_TLS() if use_tls else FTP()
    ftp.encoding = "utf-8"

    ftp.connect(host=host, port=port, timeout=timeout)
    _enable_socket_keepalive(ftp.sock)

    ftp.login(user=username, passwd=password)
    if use_tls and isinstance(ftp, FTP_TLS):
        ftp.prot_p()

    ftp.set_pasv(bool(passive_mode))
    return ftp


def _close_ftp_quietly(ftp: FTP | None):
    if ftp is None:
        return
    try:
        ftp.quit()
    except Exception:
        try:
            ftp.close()
        except Exception:
            pass


def _normalize_remote_path(remote_dir: str) -> str:
    clean = "/" + str(remote_dir or "/").replace("\\", "/").strip().lstrip("/")
    if len(clean) > 1:
        clean = clean.rstrip("/")
    return clean


def _ensure_ftp_remote_dir(ftp: FTP, remote_dir: str):
    """ساخت تضمینی و مرحله‌به‌مرحله تمام پوشه‌ها و زیرپوشه‌ها در سرور مقصد با مسیر مطلق"""
    norm_dir = _normalize_remote_path(remote_dir)
    if norm_dir == "/":
        ftp.cwd("/")
        return

    accum = ""
    for part in norm_dir.strip("/").split("/"):
        if not part:
            continue
        accum = f"{accum}/{part}"
        try:
            ftp.cwd(accum)
        except error_perm:
            try:
                ftp.mkd(accum)
            except error_perm:
                pass
            ftp.cwd(accum)


def _auto_align_filename_with_server_siblings(ftp: FTP, local_file: Path) -> Path:
    """
    بررسی فایل‌های موجود در پوشه مقصد روی سرور (دقیقاً قبل از آپلود):
    اگر قسمت‌های قبلی در همین پوشه روی سرور وجود داشته باشند و فرمول نامشان متفاوت باشد،
    فرمول نام سرور را کپی کرده، فقط بخش Eyy را به قسمت فعلی تغییر می‌دهد و فایل لوکال را هم تغییر نام می‌دهد.
    """
    m_cur = _EP_TAG_RE.search(local_file.name)
    if not m_cur:
        return local_file

    cur_ep_num = int(m_cur.group(2))
    cur_ext = local_file.suffix.lower()

    remote_files: list[str] = []
    try:
        for name, facts in ftp.mlsd():
            if name not in (".", "..") and str(facts.get("type", "")).lower() == "file":
                remote_files.append(name)
    except Exception:
        try:
            lines: list[str] = []
            ftp.retrlines("NLST", lines.append)
            remote_files = [Path(ln.strip()).name for ln in lines if ln.strip() not in (".", "..")]
        except Exception:
            return local_file

    video_exts = {".mkv", ".mp4", ".avi", ".ts", ".mov", ".m4v", ".webm"}
    is_vid = cur_ext in video_exts

    siblings = []
    for rf in remote_files:
        rf_ext = Path(rf).suffix.lower()
        if is_vid and rf_ext not in video_exts:
            continue
        if not is_vid and rf_ext != cur_ext:
            continue
        if _EP_TAG_RE.search(rf):
            siblings.append(rf)

    if not siblings:
        return local_file

    template_name = sorted(siblings)[-1]
    template_stem = Path(template_name).stem

    def _replace_ep(match: re.Match) -> str:
        s_num = int(match.group(1))
        return f"S{s_num:02d}E{cur_ep_num:02d}"

    aligned_stem = _EP_TAG_RE.sub(_replace_ep, template_stem, count=1)
    aligned_name = f"{aligned_stem}{local_file.suffix}"

    if aligned_name != local_file.name:
        new_local_path = local_file.with_name(aligned_name)
        try:
            if not new_local_path.exists():
                local_file.rename(new_local_path)
                logging.info(f"✏️ [AUTO-RENAME ON UPLOAD] '{local_file.name}' -> '{aligned_name}'")
                return new_local_path
        except Exception as e:
            logging.warning(f"Could not rename local file before upload: {e}")

    return local_file


def _upload_single_file_with_retry(
    ftp_holder: list[FTP | None],
    conn_params: dict,
    local_file: Path,
    remote_dest_dir: str,
    progress_prefix: str = "",
    progress_callback: Callable[[str], None] | None = None
) -> Path:
    """
    آپلود یک فایل منفرد با قابلیت Auto-Retry و تطبیق خودکار نام با سرور.
    در پایان، مسیر نهایی فایل (که ممکن است هم‌نام با فرمول سرور تغییر نام یافته باشد) را برمی‌گرداند.
    """
    if not local_file.exists() or not local_file.is_file():
        raise FileNotFoundError(f"فایل محلی برای آپلود یافت نشد: {local_file}")

    total_size = max(1, local_file.stat().st_size)
    total_mb = total_size / (1024 * 1024)
    last_error: Exception | None = None

    for attempt in range(1, MAX_UPLOAD_RETRIES + 1):
        try:
            # ۱. اطمینان از برقراری اتصال فعال
            ftp = ftp_holder[0]
            if ftp is None:
                if progress_callback:
                    progress_callback(f"{progress_prefix}در حال اتصال به سرور FTP (تلاش {attempt}/{MAX_UPLOAD_RETRIES})...")
                ftp = _create_ftp_connection(**conn_params)
                ftp_holder[0] = ftp
            else:
                try:
                    ftp.voidcmd("NOOP")
                except Exception:
                    _close_ftp_quietly(ftp)
                    ftp = _create_ftp_connection(**conn_params)
                    ftp_holder[0] = ftp

            # ۲. ورود به پوشه مقصد در سرور
            _ensure_ftp_remote_dir(ftp, remote_dest_dir)

            # ۲.۵. تطبیق خودکار نام فایل با فرمول قسمت‌های قبلی موجود در همین پوشه روی سرور (تغییر فقط Eyy)
            local_file = _auto_align_filename_with_server_siblings(ftp, local_file)

            # ۳. بررسی امکان Resume در صورتی که تلاش دوم به بعد باشد
            rest_offset = 0
            if attempt > 1:
                try:
                    ftp.voidcmd("TYPE I")
                    remote_size = ftp.size(local_file.name)
                    if isinstance(remote_size, int) and 0 < remote_size < total_size:
                        rest_offset = remote_size
                    elif remote_size == total_size:
                        if progress_callback:
                            progress_callback(f"{progress_prefix}100% ({total_mb:.1f} MB)")
                        return local_file
                except Exception:
                    rest_offset = 0

            uploaded = rest_offset
            start_time = time.time()
            last_report_time = 0.0

            def _on_chunk_sent(chunk: bytes):
                nonlocal uploaded, last_report_time
                uploaded += len(chunk)
                now = time.time()

                if progress_callback and (now - last_report_time >= 1.5):
                    last_report_time = now
                    pct = min(100.0, (uploaded / total_size) * 100.0)
                    elapsed = now - start_time
                    speed_info = _format_speed_and_eta(uploaded - rest_offset, total_size - rest_offset, elapsed)
                    progress_callback(
                        f"{progress_prefix}{pct:.1f}% ({uploaded / (1024*1024):.1f}/{total_mb:.1f} MB) | {speed_info}"
                    )

            with open(local_file, "rb") as f:
                if rest_offset > 0:
                    f.seek(rest_offset)
                try:
                    ftp.storbinary(
                        f"STOR {local_file.name}",
                        f,
                        blocksize=UPLOAD_BLOCK_SIZE,
                        callback=_on_chunk_sent,
                        rest=(rest_offset if rest_offset > 0 else None)
                    )
                except error_perm:
                    if rest_offset > 0:
                        f.seek(0)
                        uploaded = 0
                        ftp.storbinary(
                            f"STOR {local_file.name}",
                            f,
                            blocksize=UPLOAD_BLOCK_SIZE,
                            callback=_on_chunk_sent
                        )
                    else:
                        raise

            if progress_callback:
                progress_callback(f"{progress_prefix}100% ({total_mb:.1f} MB) - تکمیل شد")

            # برگرداندن مسیر فایل (با نام جدید در صورت تغییر نام خودکار)
            return local_file

        except all_errors as e:
            last_error = e
            logging.warning(
                f"⚠️ [FTP UPLOAD RETRY {attempt}/{MAX_UPLOAD_RETRIES}] خطا در آپلود '{local_file.name}': {e}"
            )
            _close_ftp_quietly(ftp_holder[0])
            ftp_holder[0] = None

            if attempt < MAX_UPLOAD_RETRIES:
                wait_sec = attempt * 3
                if progress_callback:
                    progress_callback(
                        f"⚠️ خطای شبکه در آپلود {local_file.name[:20]}؛ تلاش مجدد {attempt + 1}/{MAX_UPLOAD_RETRIES} تا {wait_sec} ثانیه دیگر..."
                    )
                time.sleep(wait_sec)

    raise RuntimeError(
        f"آپلود فایل '{local_file.name}' پس از {MAX_UPLOAD_RETRIES} بار تلاش ناموفق بود. آخرین خطا: {last_error}"
    )


def get_current_cuteftp_remote_dir() -> str:
    return str(cfg.get("cuteftp", "remote_base_dir", "/English/Movies")).strip() or "/English/Movies"


def extract_credentials_from_open_cuteftp() -> dict[str, str]:
    ftp_cfg = cfg.get("cuteftp") or {}
    return {
        "site_profile": str(ftp_cfg.get("site_profile", "dl15")),
        "host": str(ftp_cfg.get("host", "")).strip(),
        "username": str(ftp_cfg.get("username", "")).strip(),
        "password": str(ftp_cfg.get("password", "")).strip(),
        "port": str(ftp_cfg.get("port", 21)),
        "remote_dir": str(ftp_cfg.get("remote_base_dir", "/English/Movies")).strip(),
    }


def _get_ftp_inspection_config() -> dict:
    """
    پیکربندی جدید cuteftp بر تنظیمات قدیمی ftp اولویت دارد.
    مقادیر خالی یا placeholder در cuteftp اطلاعات معتبر ftp را از بین نمی‌برند.
    """
    legacy_cfg = cfg.get("ftp") or {}
    current_cfg = cfg.get("cuteftp") or {}

    merged = dict(legacy_cfg) if isinstance(legacy_cfg, dict) else {}
    if isinstance(current_cfg, dict):
        placeholders = {
            "ftppassword",
            "your_ftp_password",
            "change_me",
            "changeme",
            "replace_me",
        }
        for key, value in current_cfg.items():
            if value is None:
                continue
            value_text = str(value).strip()
            if not value_text:
                continue
            if key.lower() == "password" and value_text.casefold() in placeholders:
                continue
            merged[key] = value

    if not merged.get("remote_base_dir"):
        merged["remote_base_dir"] = merged.get("root_remote_dir", "/English/Movies")
    if not merged.get("port"):
        merged["port"] = 21
    if not merged.get("protocol"):
        merged["protocol"] = "FTP"

    return merged


def list_ftp_dir_entries(remote_dir: str, server_config: dict | None = None) -> dict[str, list[str]]:
    """اتصال به یک سرور مشخص و دریافت فایل‌ها و زیرپوشه‌های مسیر remote_dir"""
    result = {"files": [], "dirs": [], "exists": False}
    ftp_cfg = dict(server_config) if isinstance(server_config, dict) else _get_ftp_inspection_config()
    protocol = str(ftp_cfg.get("protocol", "FTP")).strip().upper()
    host = str(ftp_cfg.get("host", "")).strip()
    port = int(ftp_cfg.get("port", 21) or 21)
    username = str(ftp_cfg.get("username", "")).strip()
    password = str(ftp_cfg.get("password", "")).strip()

    if (
        not host
        or not username
        or not password
        or password.strip().casefold() in {
            "ftppassword",
            "your_ftp_password",
            "change_me",
            "changeme",
            "replace_me",
        }
    ):
        return result

    norm_dir = _normalize_remote_path(remote_dir)
    ftp = None
    try:
        ftp = _create_ftp_connection(
            host=host,
            port=port,
            username=username,
            password=password,
            protocol=protocol,
            timeout=int(ftp_cfg.get("timeout", 30) or 30),
            passive_mode=bool(ftp_cfg.get("passive_mode", True)),
        )
        try:
            ftp.cwd(norm_dir)
            result["exists"] = True
        except Exception:
            return result

        try:
            for name, facts in ftp.mlsd():
                if name in (".", ".."):
                    continue
                ftype = str(facts.get("type", "")).lower()
                if ftype == "dir":
                    result["dirs"].append(name)
                elif ftype == "file":
                    result["files"].append(name)
            return result
        except Exception:
            pass

        lines: list[str] = []
        ftp.retrlines("LIST", lines.append)
        for line in lines:
            parts = line.split(None, 8)
            if len(parts) < 9:
                continue
            name = parts[8].strip()
            if name in (".", ".."):
                continue
            if line.startswith("d") or "<DIR>" in line.upper():
                result["dirs"].append(name)
            else:
                result["files"].append(name)

        return result
    except Exception as e:
        logging.warning(f"FTP inspect warning for {norm_dir}: {e}")
        return result
    finally:
        _close_ftp_quietly(ftp)


def sync_upload_cuteftp(
    local_target: Path,
    remote_subpath: str = "__CURRENT__",
    progress_callback: Callable[[str], None] | None = None,
    server_config: dict | None = None,
) -> list[str]:
    local_target = Path(local_target).resolve()
    if not local_target.exists():
        raise FileNotFoundError(f"فایل یا پوشه مورد نظر برای آپلود وجود ندارد: {local_target}")

    with _FTP_SERIAL_LOCK:
        ftp_cfg = dict(server_config) if isinstance(server_config, dict) else _get_ftp_inspection_config()
        protocol = str(ftp_cfg.get("protocol", "FTP")).strip().upper()
        host = str(ftp_cfg.get("host", "")).strip()
        port = int(ftp_cfg.get("port", 21) or 21)
        username = str(ftp_cfg.get("username", "")).strip()
        password = str(ftp_cfg.get("password", "")).strip()

        if (
            not host
            or not username
            or not password
            or password.strip().casefold() in {
                "ftppassword", "your_ftp_password", "change_me",
                "changeme", "replace_me", "your_password"
            }
        ):
            raise RuntimeError(
                "مشخصات اتصال FTP (فیلدهای host، username یا password) در config.yaml تنظیم نشده است!\n"
                "لطفاً دستور /settings را در ربات بزنید و رمز عبور سرور را ثبت کنید."
            )

        base_url = str(
            ftp_cfg.get("base_download_url")
            or ftp_cfg.get("public_base_url")
            or ""
        ).rstrip("/")
        if ("dl.example.com" in base_url or not base_url) and username.startswith("@"):
            base_url = f"https://{username.lstrip('@')}"

        if not remote_subpath or remote_subpath == "__CURRENT__":
            target_remote_dir = _normalize_remote_path(
                str(ftp_cfg.get("remote_base_dir", "/English/Movies"))
            )
        else:
            target_remote_dir = _normalize_remote_path(remote_subpath)

        conn_params = {
            "host": host,
            "port": port,
            "username": username,
            "password": password,
            "protocol": protocol,
            "timeout": int(ftp_cfg.get("timeout", 120) or 120),
            "passive_mode": bool(ftp_cfg.get("passive_mode", True)),
        }

        ftp_holder: list[FTP | None] = [None]
        uploaded_urls: list[str] = []

        def _build_public_url(remote_folder: str, filename: str) -> str:
            clean_sub = remote_folder.replace("\\", "/")
            for pub_prefix in ("/public_html", "/www", "/htdocs"):
                if clean_sub.startswith(pub_prefix):
                    clean_sub = clean_sub[len(pub_prefix):]
            clean_sub = clean_sub.strip("/")
            rel_url = f"{clean_sub}/{filename}".lstrip("/")
            return f"{base_url}/{urllib.parse.quote(rel_url)}" if base_url else ""

        try:
            if local_target.is_file():
                logging.info(f"☁️ [FTP UPLOAD START] File: {local_target.name} -> {target_remote_dir}")
                # دریافت مسیر نهایی فایل (در صورتی که هنگام آپلود هم‌فرمول با سرور تغییر نام یافته باشد)
                local_target = _upload_single_file_with_retry(
                    ftp_holder=ftp_holder,
                    conn_params=conn_params,
                    local_file=local_target,
                    remote_dest_dir=target_remote_dir,
                    progress_prefix="",
                    progress_callback=progress_callback
                )
                public_url = _build_public_url(target_remote_dir, local_target.name)
                if public_url:
                    uploaded_urls.append(public_url)
                logging.info(f"✅ [FTP UPLOAD DONE] File: {local_target.name}")

            else:
                last_remote_part = target_remote_dir.rstrip("/").split("/")[-1]
                if last_remote_part.lower() == local_target.name.lower():
                    folder_remote_root = target_remote_dir
                else:
                    folder_remote_root = _normalize_remote_path(f"{target_remote_dir}/{local_target.name}")

                all_files = sorted(
                    [
                        p for p in local_target.rglob("*")
                        if p.is_file()
                        and not p.name.startswith("_temp_")
                        and not p.name.startswith("_soft_clean_")
                        and not p.name.startswith("_mp4_")
                        and not p.name.endswith(".aria2")
                    ],
                    key=lambda p: p.as_posix().lower()
                )

                if not all_files:
                    raise RuntimeError(f"هیچ فایلی داخل پوشه '{local_target.name}' برای آپلود وجود ندارد!")

                total_files = len(all_files)
                logging.info(
                    f"☁️ [FTP FOLDER UPLOAD START] Folder: {local_target.name} ({total_files} files) -> {folder_remote_root}"
                )

                for idx, file_p in enumerate(all_files, start=1):
                    rel_parent = file_p.parent.relative_to(local_target).as_posix()
                    dest_folder = (
                        folder_remote_root
                        if rel_parent in ("", ".")
                        else _normalize_remote_path(f"{folder_remote_root}/{rel_parent}")
                    )
                    prefix_str = f"[{idx}/{total_files}] {file_p.name[:22]}: "

                    # دریافت مسیر نهایی هر فایل پس از تطبیق نام با سرور
                    file_p = _upload_single_file_with_retry(
                        ftp_holder=ftp_holder,
                        conn_params=conn_params,
                        local_file=file_p,
                        remote_dest_dir=dest_folder,
                        progress_prefix=prefix_str,
                        progress_callback=progress_callback
                    )
                    public_url = _build_public_url(dest_folder, file_p.name)
                    if public_url:
                        uploaded_urls.append(public_url)

                logging.info(f"✅ [FTP FOLDER UPLOAD DONE] Folder: {local_target.name} ({total_files} files)")

            return uploaded_urls

        finally:
            _close_ftp_quietly(ftp_holder[0])
