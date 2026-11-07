from ftplib import FTP, FTP_TLS, error_perm, all_errors
import logging
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
            # در ویندوز: ارسال پکت Keep-Alive هر ۳۰ ثانیه، در صورت عدم پاسخ هر ۱۰ ثانیه
            sock.ioctl(socket.SIO_KEEPALIVE_VALS, (1, 30000, 10000))
    except Exception:
        pass


def _create_ftp_connection(
    host: str,
    port: int,
    username: str,
    password: str,
    protocol: str = "FTP",
    timeout: int = 120
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

    ftp.set_pasv(True)
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
    """
    ساخت تضمینی و مرحله‌به‌مرحله تمام پوشه‌ها و زیرپوشه‌ها در سرور مقصد با مسیر مطلق (Absolute Path)
    """
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


def _upload_single_file_with_retry(
    ftp_holder: list[FTP | None],
    conn_params: dict,
    local_file: Path,
    remote_dest_dir: str,
    progress_prefix: str = "",
    progress_callback: Callable[[str], None] | None = None
):
    """
    آپلود یک فایل منفرد با قابلیت تلاش مجدد خودکار (Auto-Retry) و بازسازی اتصال در صورت قطعی شبکه.
    اگر سرور از دستور REST (ادامه آپلود از نقطه قطع‌شده) پشتیبانی کند، ادامه فایل را می‌فرستد؛
    در غیر این صورت فایل را از ابتدا به صورت کامل ارسال می‌کند.
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

            # ۲. ورود به پوشه مقصد (و ساخت خودکار آن در صورت عدم وجود)
            _ensure_ftp_remote_dir(ftp, remote_dest_dir)

            # ۳. بررسی امکان Resume در صورتی که تلاش دوم به بعد باشد
            rest_offset = 0
            if attempt > 1:
                try:
                    ftp.voidcmd("TYPE I")
                    remote_size = ftp.size(local_file.name)
                    if isinstance(remote_size, int) and 0 < remote_size < total_size:
                        rest_offset = remote_size
                    elif remote_size == total_size:
                        # فایل در تلاش قبلی به طور کامل آپلود شده است
                        if progress_callback:
                            progress_callback(f"{progress_prefix}100% ({total_mb:.1f} MB)")
                        return
                except Exception:
                    rest_offset = 0

            uploaded = rest_offset
            start_time = time.time()
            last_report_time = 0.0
            last_noop_time = time.time()

            def _on_chunk_sent(chunk: bytes):
                nonlocal uploaded, last_report_time, last_noop_time
                uploaded += len(chunk)
                now = time.time()

                # گزارش زنده پیشرفت به ربات هر ۱.۵ ثانیه
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
                    # اگر سرور دستور REST را پشتیبانی نکرد، از اول فایل ارسال کن
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
            return

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
    """خواندن مسیر پیش‌فرض سرور از config.yaml بدون نیاز به رابط گرافیکی"""
    return str(cfg.get("cuteftp", "remote_base_dir", "/English/Movies")).strip() or "/English/Movies"


def extract_credentials_from_open_cuteftp() -> dict[str, str]:
    """خواندن مستقیم اطلاعات اتصال از config.yaml (برای سازگاری با سایر ماژول‌ها)"""
    ftp_cfg = cfg.get("cuteftp") or {}
    return {
        "site_profile": str(ftp_cfg.get("site_profile", "dl15")),
        "host": str(ftp_cfg.get("host", "")).strip(),
        "username": str(ftp_cfg.get("username", "")).strip(),
        "password": str(ftp_cfg.get("password", "")).strip(),
        "port": str(ftp_cfg.get("port", 21)),
        "remote_dir": str(ftp_cfg.get("remote_base_dir", "/English/Movies")).strip(),
    }


def sync_upload_cuteftp(
    local_target: Path,
    remote_subpath: str = "__CURRENT__",
    progress_callback: Callable[[str], None] | None = None
) -> list[str]:
    """
    موتور اصلی آپلود مستقیم به سرور از طریق مشخصات config.yaml (بدون هیچ‌گونه نیاز به GUI):
    - با قفل انحصاری (_FTP_SERIAL_LOCK) تضمین می‌کند که در هر لحظه فقط یک آیتم آپلود شود.
    - اگر local_target یک پوشه باشد، کل درخت پوشه و تمام فایل‌های داخل آن را به ترتیب در مسیر مقصد آپلود می‌کند.
    - در پایان، لیست دقیق لینک‌های دانلود مستقیم تمام فایل‌های آپلودشده را برمی‌گرداند.
    """
    local_target = Path(local_target).resolve()
    if not local_target.exists():
        raise FileNotFoundError(f"فایل یا پوشه مورد نظر برای آپلود وجود ندارد: {local_target}")

    with _FTP_SERIAL_LOCK:
        ftp_cfg = cfg.get("cuteftp") or {}
        protocol = str(ftp_cfg.get("protocol", "FTP")).strip().upper()
        host = str(ftp_cfg.get("host", "")).strip()
        port = int(ftp_cfg.get("port", 21) or 21)
        username = str(ftp_cfg.get("username", "")).strip()
        password = str(ftp_cfg.get("password", "")).strip()

        if not host or not username or not password or password == "ftppassword":
            raise RuntimeError(
                "مشخصات اتصال FTP (فیلدهای host، username یا password) در config.yaml تنظیم نشده است!\n"
                "لطفاً دستور /settings را در ربات بزنید، وارد بخش «☁️ مشخصات سرور دانلود و CuteFTP» شوید و رمز عبور سرور را ثبت کنید."
            )

        base_url = str(ftp_cfg.get("base_download_url", "https://dl15.cdnzed.sbs")).rstrip("/")
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
            "timeout": 120,
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
            return f"{base_url}/{urllib.parse.quote(rel_url)}"

        try:
            if local_target.is_file():
                logging.info(f"☁️ [FTP UPLOAD START] File: {local_target.name} -> {target_remote_dir}")
                _upload_single_file_with_retry(
                    ftp_holder=ftp_holder,
                    conn_params=conn_params,
                    local_file=local_target,
                    remote_dest_dir=target_remote_dir,
                    progress_prefix="",
                    progress_callback=progress_callback
                )
                uploaded_urls.append(_build_public_url(target_remote_dir, local_target.name))
                logging.info(f"✅ [FTP UPLOAD DONE] File: {local_target.name}")

            else:
                # آپلود کامل یک پوشه به همراه تمام زیرپوشه‌ها به ترتیب الفبا و شماره قسمت
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

                    _upload_single_file_with_retry(
                        ftp_holder=ftp_holder,
                        conn_params=conn_params,
                        local_file=file_p,
                        remote_dest_dir=dest_folder,
                        progress_prefix=prefix_str,
                        progress_callback=progress_callback
                    )
                    uploaded_urls.append(_build_public_url(dest_folder, file_p.name))

                logging.info(f"✅ [FTP FOLDER UPLOAD DONE] Folder: {local_target.name} ({total_files} files)")

            return uploaded_urls

        finally:
            _close_ftp_quietly(ftp_holder[0])