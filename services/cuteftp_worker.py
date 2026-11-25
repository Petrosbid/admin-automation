import hashlib
import os
import posixpath
import uuid
from ftplib import FTP, FTP_TLS, error_perm, all_errors
import logging
import re
import socket
import threading
import time
import urllib.parse
from pathlib import Path
from typing import Any, Callable

from core.config_loader import cfg
from core.queue_manager import set_download_file_name

# قفل انحصاری برای تضمین اجرای ۱۰۰٪ ترتیبی آپلودها (یک فایل در هر لحظه تا اتمام کامل)
_FTP_SERIAL_LOCK = threading.Lock()

# سایز بلاک بهینه برای انتقال سریع فایل‌های ویدئویی حجیم روی شبکه (512 کیلوبایت)
UPLOAD_BLOCK_SIZE = 512 * 1024
MAX_UPLOAD_RETRIES = 4
INSPECT_TIMEOUT_SECONDS = 10

_EP_TAG_RE = re.compile(r"[Ss](\d{1,2})[\s._\-]*[Ee][Pp]?(\d{1,3})")


class _NatSafeFTP(FTP):
    """
    جلوگیری از خطای WinError 10060 در حالت Passive (PASV) زمانی که سرور FTP پشت NAT/فایروال است
    و در پاسخ 227 یک IP داخلی یا غیرقابل دسترس برمی‌گرداند.
    """
    def makepasv(self) -> tuple[str, int]:
        _, port = super().makepasv()
        return self.host, port


class _NatSafeFTP_TLS(FTP_TLS):
    """نسخه FTPS/TLS مقاوم در برابر بازگشت IP اشتباه در Passive Mode"""
    def makepasv(self) -> tuple[str, int]:
        _, port = super().makepasv()
        return self.host, port


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
    ftp = _NatSafeFTP_TLS() if use_tls else _NatSafeFTP()
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


def _list_remote_files_for_alignment(ftp: FTP, remote_dir: str | None = None) -> list[str]:
    """List remote files for naming templates without changing the active FTP directory."""
    try:
        entries = ftp.mlsd(remote_dir) if remote_dir else ftp.mlsd()
        return [
            posixpath.basename(str(name).rstrip("/"))
            for name, facts in entries
            if str(name).strip() not in (".", "..")
            and str((facts or {}).get("type", "")).casefold() == "file"
        ]
    except Exception:
        try:
            lines: list[str] = []
            command = "NLST" if not remote_dir else f"NLST {remote_dir}"
            ftp.retrlines(command, lines.append)
            return [
                posixpath.basename(str(line).strip().rstrip("/"))
                for line in lines
                if str(line).strip() not in ("", ".", "..")
            ]
        except Exception:
            return []


def _list_remote_media_sibling_dirs(ftp: FTP, current_dir: str) -> list[str]:
    """Find raw quality folders beside SoftSub/HardSub output folders."""
    parent_dir = posixpath.dirname(current_dir.rstrip("/")) or "/"
    current_name = posixpath.basename(current_dir.rstrip("/"))
    raw_counterpart = re.sub(
        r"(?i)[._\- ](?:softsub|hardsub)$",
        "",
        current_name,
    ).strip(" ._-")

    candidates: list[str] = []
    try:
        entries = ftp.mlsd(parent_dir)
        for name, facts in entries:
            if str((facts or {}).get("type", "")).casefold() != "dir":
                continue
            folder_name = posixpath.basename(str(name).rstrip("/"))
            if folder_name:
                candidates.append(folder_name)
    except Exception:
        # A number of FTP hosts disable MLSD. NLST may list both files and
        # folders, so probe only media-looking candidates with CWD and always
        # restore the original working directory before continuing the upload.
        try:
            original_dir = str(ftp.pwd() or current_dir).replace("\\", "/")
            lines: list[str] = []
            ftp.retrlines(f"NLST {parent_dir}", lines.append)
            for line in lines:
                folder_name = posixpath.basename(str(line).strip().rstrip("/"))
                if not folder_name:
                    continue
                low = folder_name.casefold()
                looks_like_quality = (
                    folder_name.casefold() == raw_counterpart.casefold()
                    or "fullhd" in low
                    or "ebtv" in low
                    or bool(re.search(r"(?i)(?:2160p|1080p|720p|480p)", folder_name))
                )
                if not looks_like_quality:
                    continue
                full_path = posixpath.join(parent_dir, folder_name).replace("\\", "/")
                is_dir = False
                try:
                    ftp.cwd(full_path)
                    is_dir = True
                except Exception:
                    is_dir = False
                finally:
                    try:
                        ftp.cwd(original_dir)
                    except Exception as restore_exc:
                        raise RuntimeError(
                            f"Could not restore FTP working directory to {original_dir}: {restore_exc}"
                        ) from restore_exc
                if is_dir:
                    candidates.append(folder_name)
        except Exception:
            return []

    ranked: list[tuple[int, str]] = []
    for folder_name in candidates:
        if not folder_name or folder_name.casefold() == current_name.casefold():
            continue
        low = folder_name.casefold()
        if folder_name.casefold() == raw_counterpart.casefold():
            ranked.append((0, folder_name))
        elif (
            "softsub" not in low
            and "hardsub" not in low
            and (
                "fullhd" in low
                or "ebtv" in low
                or re.search(r"(?i)(?:2160p|1080p|720p|480p)", folder_name)
            )
        ):
            ranked.append((1, folder_name))

    ranked.sort(key=lambda item: (item[0], item[1].casefold()))
    result: list[str] = []
    for _, name in ranked:
        if name not in result:
            result.append(name)
        if len(result) >= 10:
            break
    return [posixpath.join(parent_dir, name).replace("\\", "/") for name in result]


def _season_from_remote_dir(remote_dir: str) -> int | None:
    for part in reversed([item for item in str(remote_dir or "").replace("\\", "/").split("/") if item]):
        match = re.fullmatch(r"(?i)s(?:eason[\s._\-]*)?(\d{1,2})", part)
        if match:
            return int(match.group(1))
    return None


def _extract_upload_episode_identity(
    filename: str,
    fallback_season: int | None = None,
) -> tuple[int, int, int, int, str] | None:
    """
    Return (season, episode, token_start, token_end, format) from a release name.
    Supports canonical SxxEyy, 1x02, E02/EP02, " - 02", and bracketed [02]/(02)
    when the parent remote directory provides an unambiguous season.
    """
    stem = Path(str(filename or "")).stem

    match = _EP_TAG_RE.search(stem)
    if match:
        return (
            int(match.group(1)),
            int(match.group(2)),
            match.start(),
            match.end(),
            "standard",
        )

    alt_match = re.search(r"(?<!\d)(\d{1,2})[xX](\d{1,3})(?!\d)", stem)
    if alt_match:
        return (
            int(alt_match.group(1)),
            int(alt_match.group(2)),
            alt_match.start(),
            alt_match.end(),
            "alternate",
        )

    if fallback_season is None:
        return None

    patterns = (
        (
            r"(?i)(?:^|[ ._\-])(?:episode|ep|e)[ ._\-]*(\d{1,3})(?=$|[ ._\-\[\]()])",
            "numeric",
        ),
        (r"\s-\s(\d{1,3})(?=$|[ ._\-\[\]()])", "numeric"),
        (r"[\[(]\s*(\d{1,3})(?:v\d)?\s*[\])]", "numeric"),
        (
            r"(?:^|[ ._\-])(\d{1,3})(?:v\d)?(?=\s*(?:\[|\())",
            "numeric",
        ),
    )
    for pattern, style in patterns:
        match = re.search(pattern, stem)
        if match:
            token = match.group(1)
            return (
                int(fallback_season),
                int(token),
                match.start(1),
                match.end(1),
                style,
            )
    return None


def _build_aligned_upload_filename(
    local_filename: str,
    template_filename: str,
    fallback_season: int | None = None,
) -> str | None:
    """Apply the server's release naming pattern while preserving the local output type."""
    local_path = Path(local_filename)
    template_path = Path(template_filename)
    current = _extract_upload_episode_identity(local_path.name, fallback_season)
    template = _extract_upload_episode_identity(template_path.name, fallback_season)
    if not current or not template:
        return None

    current_season, current_episode = current[0], current[1]
    template_season, _, template_start, template_end, template_style = template
    if current_season != template_season:
        return None

    # Preserve the template's episode-token format and zero-padding width.
    if template_style == "standard":
        replacement = f"S{current_season:02d}E{current_episode:02d}"
    elif template_style == "alternate":
        token = template_path.stem[template_start:template_end]
        x_char = "X" if "X" in token else "x"
        replacement = f"{current_season:02d}{x_char}{current_episode:02d}"
    else:
        template_token = template_path.stem[template_start:template_end]
        width = len(template_token)
        replacement = f"{current_episode:0{width}d}"

    template_stem = template_path.stem
    local_stem = local_path.stem
    local_marker_match = re.search(
        r"(?i)(?:[._\-](?:softsub|hardsub)(?:[._\-]ebtv)?|[._\-]ebtv)$",
        local_stem,
    )
    local_output_marker = local_marker_match.group(0) if local_marker_match else ""

    template_tail = template_stem[template_end:]
    template_tail = re.sub(
        r"(?i)(?:[._\-](?:softsub|hardsub)(?:[._\-]ebtv)?|[._\-]ebtv)$",
        "",
        template_tail,
    )
    aligned_stem = (
        template_stem[:template_start]
        + replacement
        + template_tail
        + local_output_marker
    )
    aligned_filename = f"{aligned_stem}{local_path.suffix}"
    return aligned_filename if aligned_filename.casefold() != local_path.name.casefold() else None



def _auto_align_filename_with_server_siblings(ftp: FTP, local_file: Path) -> Path:
    """
    Correct the local upload name from the naming convention already used on the
    destination server. For SoftSub/HardSub output folders, also inspect their
    raw EBTV/FullHD sibling quality folders, because the output folder is often
    empty before its first upload.
    """
    import logging

    local_file = Path(local_file)
    try:
        current_dir = str(ftp.pwd() or "").replace("\\", "/").rstrip("/") or "/"
    except Exception:
        current_dir = ""

    fallback_season = _season_from_remote_dir(current_dir)
    current_identity = _extract_upload_episode_identity(local_file.name, fallback_season)
    if not current_identity:
        return local_file

    candidate_dirs: list[str | None] = [None]  # Current working directory first.
    if current_dir:
        candidate_dirs.extend(_list_remote_media_sibling_dirs(ftp, current_dir))

    video_exts = {".mkv", ".mp4", ".avi", ".ts", ".mov", ".m4v", ".webm"}
    is_video = local_file.suffix.casefold() in video_exts
    current_season = current_identity[0]
    best_template: tuple[int, int, str] | None = None

    for dir_priority, remote_dir in enumerate(candidate_dirs):
        remote_names = _list_remote_files_for_alignment(ftp, remote_dir)
        siblings: list[tuple[int, str]] = []
        for remote_name in remote_names:
            remote_ext = Path(remote_name).suffix.casefold()
            if is_video and remote_ext not in video_exts:
                continue
            if not is_video and remote_ext != local_file.suffix.casefold():
                continue
            identity = _extract_upload_episode_identity(remote_name, fallback_season)
            if not identity or identity[0] != current_season:
                continue
            siblings.append((identity[1], remote_name))

        if siblings:
            episode_number, template_name = max(
                siblings,
                key=lambda item: (item[0], item[1].casefold()),
            )
            best_template = (dir_priority, episode_number, template_name)
            break

    if not best_template:
        return local_file

    aligned_name = _build_aligned_upload_filename(
        local_file.name,
        best_template[2],
        fallback_season=fallback_season,
    )
    if not aligned_name:
        return local_file

    aligned_path = local_file.with_name(aligned_name)
    if aligned_path.exists():
        logging.warning(
            "Server naming template resolved to %s, but that local path already exists; "
            "leaving the new file name unchanged.",
            aligned_path,
        )
        return local_file

    try:
        local_file.rename(aligned_path)
        logging.info(
            "✏️ [AUTO-RENAME ON UPLOAD] '%s' -> '%s' (template=%s, remote_dir_priority=%s)",
            local_file.name,
            aligned_path.name,
            best_template[2],
            best_template[0],
        )
        return aligned_path
    except Exception:
        logging.exception("Could not apply server naming template to %s", local_file)
        return local_file


def _upload_single_file_with_retry(
    ftp_holder: list[FTP | None],
    conn_params: dict,
    local_file: Path,
    remote_dest_dir: str,
    progress_prefix: str = "",
    progress_callback: Callable[[str], None] | None = None,
    *,
    preserve_filename: bool = False,
    skip_identical: bool = False,
) -> Path:
    """
    Upload one file with retry and optional server-side filename alignment.

    Every retry starts a fresh STOR from byte zero. Blind REST-resume is unsafe
    when an older file with the same name already exists: its prefix may not
    match the newly generated MP4/MKV, producing a corrupt hybrid file.
    """
    import logging

    local_file = Path(local_file).resolve()
    if not local_file.exists() or not local_file.is_file():
        raise FileNotFoundError(f"فایل محلی برای آپلود یافت نشد: {local_file}")

    last_error: Exception | None = None

    for attempt in range(1, MAX_UPLOAD_RETRIES + 1):
        try:
            ftp = ftp_holder[0]
            if ftp is None:
                if progress_callback:
                    progress_callback(
                        f"{progress_prefix}در حال اتصال به سرور FTP (تلاش {attempt}/{MAX_UPLOAD_RETRIES})..."
                    )
                ftp = _create_ftp_connection(**conn_params)
                ftp_holder[0] = ftp
            else:
                try:
                    ftp.voidcmd("NOOP")
                except Exception:
                    _close_ftp_quietly(ftp)
                    ftp = _create_ftp_connection(**conn_params)
                    ftp_holder[0] = ftp

            _ensure_ftp_remote_dir(ftp, remote_dest_dir)
            if not preserve_filename:
                local_file = _auto_align_filename_with_server_siblings(ftp, local_file)

            if not local_file.is_file():
                raise FileNotFoundError(f"فایل محلی پس از تطبیق نام پیدا نشد: {local_file}")

            total_size = local_file.stat().st_size
            if total_size <= 0:
                raise ValueError(f"فایل محلی برای آپلود خالی است: {local_file}")

            # Inspect an existing remote file for diagnostics only. Never treat
            # equal size as proof of equal content and never resume from its tail.
            existing_remote_size = None
            try:
                ftp.voidcmd("TYPE I")
                existing_remote_size = ftp.size(local_file.name)
            except Exception:
                existing_remote_size = None

            if skip_identical and existing_remote_size == total_size:
                remote_digest = hashlib.sha256()
                try:
                    ftp.retrbinary(f"RETR {local_file.name}", remote_digest.update)
                except error_perm:
                    logging.info("FTP read unavailable for %s; proceeding with upload", local_file.name)
                else:
                    with local_file.open("rb") as source:
                        local_digest = hashlib.sha256(source.read()).digest()
                    if remote_digest.digest() == local_digest:
                        return local_file

            if existing_remote_size is not None and progress_callback:
                progress_callback(
                    f"{progress_prefix}نسخه قبلی مقصد ({existing_remote_size} بایت) بررسی شد؛ "
                    "فایل جدید از ابتدا جایگزین می‌شود..."
                )

            total_mb = total_size / (1024 * 1024)
            uploaded = 0
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
                    speed_info = _format_speed_and_eta(uploaded, total_size, elapsed)
                    progress_callback(
                        f"{progress_prefix}{pct:.1f}% "
                        f"({uploaded / (1024 * 1024):.1f}/{total_mb:.1f} MB) | {speed_info}"
                    )

            with open(local_file, "rb") as file_handle:
                ftp.storbinary(
                    f"STOR {local_file.name}",
                    file_handle,
                    blocksize=UPLOAD_BLOCK_SIZE,
                    callback=_on_chunk_sent,
                )

            # Verify the actual remote object after the server confirms STOR.
            ftp.voidcmd("TYPE I")
            remote_size = ftp.size(local_file.name)
            if remote_size is None or int(remote_size) != total_size:
                raise IOError(
                    f"تأیید اندازه فایل روی سرور ناموفق بود: "
                    f"local={total_size}, remote={remote_size}, file={local_file.name}"
                )

            if progress_callback:
                progress_callback(
                    f"{progress_prefix}100% ({total_mb:.1f} MB) - "
                    f"آپلود و تأیید اندازه ({remote_size} بایت) تکمیل شد"
                )

            logging.info(
                "[FTP UPLOAD VERIFIED] file=%s local_bytes=%d remote_bytes=%d",
                local_file.name,
                total_size,
                remote_size,
            )
            return local_file

        except all_errors as exc:
            last_error = exc
            logging.warning(
                "⚠️ [FTP UPLOAD RETRY %s/%s] Failed to upload '%s': %s",
                attempt,
                MAX_UPLOAD_RETRIES,
                local_file.name,
                exc,
            )
            _close_ftp_quietly(ftp_holder[0])
            ftp_holder[0] = None

            if attempt < MAX_UPLOAD_RETRIES:
                wait_sec = attempt * 3
                if progress_callback:
                    progress_callback(
                        f"⚠️ خطای شبکه در آپلود {local_file.name[:20]}؛ "
                        f"تلاش مجدد {attempt + 1}/{MAX_UPLOAD_RETRIES} تا {wait_sec} ثانیه دیگر..."
                    )
                time.sleep(wait_sec)

    raise RuntimeError(
        f"آپلود فایل '{local_file.name}' پس از {MAX_UPLOAD_RETRIES} بار تلاش ناموفق بود. "
        f"آخرین خطا: {last_error}"
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


def list_ftp_dir_entries(remote_dir: str, server_config: dict | None = None) -> dict[str, Any]:
    """اتصال سریع به یک سرور مشخص و دریافت فایل‌ها و زیرپوشه‌های مسیر remote_dir"""
    result = {"files": [], "dirs": [], "exists": False, "checked": False}
    ftp_cfg = dict(server_config) if isinstance(server_config, dict) else _get_ftp_inspection_config()
    protocol = str(ftp_cfg.get("protocol", "FTP")).strip().upper()
    host = str(ftp_cfg.get("host", "")).strip()
    port = int(ftp_cfg.get("port", 21) or 21)
    username = str(ftp_cfg.get("username", "")).strip()
    password = str(ftp_cfg.get("password", "")).strip()
    server_label = str(ftp_cfg.get("name") or ftp_cfg.get("id") or host)

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
        result["error"] = "مشخصات اتصال FTP کامل نیست."
        return result

    norm_dir = _normalize_remote_path(remote_dir)
    # استفاده از تایم‌اوت کوتاه (حداکثر ۱۰ ثانیه) برای Inspect تا در صورت قطعی یک سرور، ربات ۲۱+ ثانیه قفل نشود
    inspect_timeout = min(INSPECT_TIMEOUT_SECONDS, int(ftp_cfg.get("inspect_timeout", INSPECT_TIMEOUT_SECONDS) or INSPECT_TIMEOUT_SECONDS))

    ftp = None
    try:
        ftp = _create_ftp_connection(
            host=host,
            port=port,
            username=username,
            password=password,
            protocol=protocol,
            timeout=inspect_timeout,
            passive_mode=bool(ftp_cfg.get("passive_mode", True)),
        )
        result["checked"] = True
        try:
            ftp.cwd(norm_dir)
            result["exists"] = True
        except error_perm:
            # پوشه هنوز روی این سرور وجود ندارد (خطای شبکه نیست)
            return result
        except Exception as cwd_exc:
            result["checked"] = False
            result["error"] = str(cwd_exc)
            logging.warning(f"FTP cwd warning on [{server_label}] for {norm_dir}: {cwd_exc}")
            return result

        # ۱. تلاش با دستور استاندارد MLSD
        try:
            for name, facts in ftp.mlsd():
                if name in (".", ".."):
                    continue
                ftype = str(facts.get("type", "")).lower()
                if ftype in ("dir", "cdir", "pdir"):
                    result["dirs"].append(name)
                elif ftype == "file":
                    result["files"].append(name)
            return result
        except Exception:
            pass

        # ۲. تلاش جایگزین با LIST
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
        result["error"] = str(e)
        logging.warning(f"FTP inspect warning on [{server_label} - {host}] for {norm_dir}: {e}")
        return result
    finally:
        _close_ftp_quietly(ftp)



_DOWNLOAD_MEDIA_EXTENSIONS = {".mkv", ".mp4", ".avi", ".ts", ".mov", ".m4v", ".webm"}
_DOWNLOAD_SKIP_DIRS = {
    "srt", "sub", "subs", "subtitle", "subtitles",
    "softsub", "hardsub", "softsubs", "hardsubs",
    "_incoming_downloads", "_rejected_downloads",
}
_DOWNLOAD_LOCK = threading.Lock()


def _episode_tags_for_remote_filename(filename: str, fallback_season: int | None = None) -> set[str]:
    """Extract every explicit episode tag in a release filename without confusing resolution digits."""
    name = Path(str(filename)).stem
    found: set[str] = set()

    standard = list(re.finditer(
        r"(?i)(?<![A-Z0-9])S(\d{1,2})[ ._-]*E(?:P)?(\d{1,3})",
        name,
    ))
    for match in standard:
        season = int(match.group(1))
        found.add(f"S{season:02d}E{int(match.group(2)):02d}")
        tail = name[match.end():]
        for extra in re.finditer(
            r"(?i)E(?:P)?(\d{1,3})(?=$|[ ._+&-])",
            tail,
        ):
            found.add(f"S{season:02d}E{int(extra.group(1)):02d}")

    for match in re.finditer(r"(?i)(?<!\d)(\d{1,2})x(\d{1,3})(?!\d)", name):
        found.add(f"S{int(match.group(1)):02d}E{int(match.group(2)):02d}")

    if not found and fallback_season is not None:
        fallback = re.search(
            r"(?i)(?:^|[\s._\-\[])E(?:P|pisode)?[\s._-]*(\d{1,3})(?=$|[\s._\-\]])",
            name,
        )
        if fallback:
            found.add(f"S{int(fallback_season):02d}E{int(fallback.group(1)):02d}")

    return found


def _safe_ftp_entry_name(name: str) -> bool:
    invalid_windows_chars = '<>:"|?*'
    return bool(
        name
        and name not in (".", "..")
        and "/" not in name
        and "\\" not in name
        and "\x00" not in name
        and not any(ord(char) < 32 for char in name)
        and not any(char in invalid_windows_chars for char in name)
        and not name.endswith((" ", "."))
    )


def _list_ftp_directory_for_download(ftp: FTP, remote_dir: str) -> tuple[list[tuple[str, int | None]], list[str]]:
    """List one FTP directory using MLSD, falling back to common Unix/Windows LIST formats."""
    ftp.cwd(remote_dir)
    files: list[tuple[str, int | None]] = []
    dirs: list[str] = []

    try:
        for name, facts in ftp.mlsd():
            if not _safe_ftp_entry_name(name):
                continue
            kind = str(facts.get("type", "")).casefold()
            if kind in ("dir", "cdir", "pdir"):
                if kind == "dir":
                    dirs.append(name)
            elif kind == "file":
                try:
                    size = int(facts.get("size")) if facts.get("size") is not None else None
                except (TypeError, ValueError):
                    size = None
                files.append((name, size))
        return files, dirs
    except Exception:
        pass

    lines: list[str] = []
    try:
        ftp.retrlines("LIST", lines.append)
    except Exception as exc:
        raise RuntimeError(f"فهرست‌کردن پوشه FTP ناموفق بود: {remote_dir}: {exc}") from exc

    for line in lines:
        parts = line.split(None, 8)
        name = ""
        size: int | None = None
        is_dir = False

        if len(parts) >= 9 and parts[0] and parts[0][0] in "dl-":
            is_dir = parts[0][0] == "d"
            name = parts[8].strip()
            try:
                size = int(parts[4])
            except (IndexError, TypeError, ValueError):
                size = None
        elif any(part.upper() == "<DIR>" for part in parts):
            marker = next(i for i, part in enumerate(parts) if part.upper() == "<DIR>")
            is_dir = True
            name = " ".join(parts[marker + 1:]).strip()
        elif len(parts) >= 4:
            name = " ".join(parts[3:]).strip()
            try:
                size = int(parts[2])
            except (TypeError, ValueError):
                size = None

        if not _safe_ftp_entry_name(name):
            continue
        if is_dir:
            dirs.append(name)
        elif Path(name).suffix.casefold() in _DOWNLOAD_MEDIA_EXTENSIONS:
            files.append((name, size))

    return files, dirs


def sync_download_subtitle(
    remote_dir: str, filename: str, local_dir: Path, server_config: dict,
) -> Path:
    if not _safe_ftp_entry_name(filename) or Path(filename).suffix.casefold() not in {".srt", ".ass", ".ssa"}:
        raise ValueError(f"نام فایل زیرنویس نامعتبر است: {filename}")
    ftp = _create_ftp_connection(
        host=server_config["host"],
        port=server_config["port"],
        username=server_config["username"],
        password=server_config["password"],
        protocol=server_config["protocol"],
        timeout=server_config["timeout"],
        passive_mode=server_config["passive_mode"],
    )
    try:
        ftp.cwd(_normalize_remote_path(remote_dir))
        target = local_dir / filename
        with target.open("xb") as output:
            ftp.retrbinary(f"RETR {filename}", output.write)
        return target
    finally:
        _close_ftp_quietly(ftp)


def sync_download_episode_files(
    remote_season_dir: str,
    local_season_dir: Path,
    episode_tags: list[str],
    server_config: dict,
    progress_callback: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """
    Download only video releases matching the requested episode tags.
    The relative FTP directory tree is preserved under local_season_dir.
    Existing files are never overwritten; incomplete transfers use isolated .part files.
    """
    remote_root = _normalize_remote_path(remote_season_dir)
    local_root = Path(local_season_dir).resolve()
    wanted = {str(tag).strip().upper() for tag in episode_tags if str(tag).strip()}
    if not wanted:
        raise ValueError("برای دانلود دقیق، حداقل یک تگ قسمت مانند S01E03 لازم است.")

    season_numbers = {
        int(match.group(1))
        for tag in wanted
        if (match := re.fullmatch(r"S(\d{2})E\d{2,3}", tag, flags=re.IGNORECASE))
    }
    fallback_season = next(iter(season_numbers)) if len(season_numbers) == 1 else None

    ftp_cfg = dict(server_config or {})
    host = str(ftp_cfg.get("host", "")).strip()
    username = str(ftp_cfg.get("username", "")).strip()
    password = str(ftp_cfg.get("password", "")).strip()
    if not host or not username or not password:
        raise RuntimeError("اطلاعات اتصال FTP برای دانلود کامل نیست.")
    try:
        port = int(ftp_cfg.get("port", 21) or 21)
        timeout = max(30, int(ftp_cfg.get("timeout", 120) or 120))
    except (TypeError, ValueError):
        port, timeout = 21, 120

    conn_params = {
        "host": host,
        "port": port,
        "username": username,
        "password": password,
        "protocol": str(ftp_cfg.get("protocol", "FTP")).strip().upper(),
        "timeout": timeout,
        "passive_mode": bool(ftp_cfg.get("passive_mode", True)),
    }
    ftp_holder: list[FTP | None] = [None]

    def connect() -> FTP:
        return _create_ftp_connection(**conn_params)

    result: dict[str, Any] = {
        "downloaded": 0,
        "skipped": 0,
        "failed": 0,
        "bytes": 0,
        "files": [],
        "skipped_files": [],
        "errors": [],
    }

    with _DOWNLOAD_LOCK:
        try:
            ftp_holder[0] = connect()
            candidates: list[tuple[str, tuple[str, ...], str, int | None]] = []
            visited: set[str] = set()

            def walk(remote_dir: str, relative_parts: tuple[str, ...], depth: int):
                normalized = _normalize_remote_path(remote_dir)
                key = normalized.casefold()
                if key in visited:
                    return
                visited.add(key)

                ftp = ftp_holder[0]
                if ftp is None:
                    ftp_holder[0] = connect()
                    ftp = ftp_holder[0]
                try:
                    remote_files, remote_dirs = _list_ftp_directory_for_download(ftp, normalized)
                except Exception as first_listing_error:
                    _close_ftp_quietly(ftp_holder[0])
                    ftp_holder[0] = None
                    try:
                        ftp_holder[0] = connect()
                        remote_files, remote_dirs = _list_ftp_directory_for_download(
                            ftp_holder[0],
                            normalized,
                        )
                    except Exception as retry_listing_error:
                        _close_ftp_quietly(ftp_holder[0])
                        ftp_holder[0] = None
                        if not relative_parts:
                            raise RuntimeError(
                                f"خواندن پوشه اصلی FTP ناموفق بود: {retry_listing_error}"
                            ) from first_listing_error
                        result["failed"] += 1
                        result["errors"].append(
                            f"فهرست {normalized} خوانده نشد: {retry_listing_error}"
                        )
                        return

                for filename, size in remote_files:
                    if Path(filename).suffix.casefold() not in _DOWNLOAD_MEDIA_EXTENSIONS:
                        continue
                    low_name = filename.casefold()
                    if ".softsub." in low_name or ".hardsub." in low_name:
                        continue
                    tags = _episode_tags_for_remote_filename(filename, fallback_season)
                    if not (tags & wanted):
                        continue
                    candidates.append((normalized, relative_parts, filename, size))

                if depth >= 5:
                    return
                for dirname in sorted(remote_dirs, key=str.casefold):
                    low_dir = dirname.casefold()
                    if (
                        low_dir in _DOWNLOAD_SKIP_DIRS
                        or low_dir.endswith(("-softsub", "-hardsub"))
                        or low_dir.startswith("_")
                        or not _safe_ftp_entry_name(dirname)
                    ):
                        continue
                    child_remote = _normalize_remote_path(f"{normalized}/{dirname}")
                    walk(child_remote, relative_parts + (dirname,), depth + 1)

            walk(remote_root, (), 0)
            candidates.sort(key=lambda item: ("/".join(item[1]).casefold(), item[2].casefold()))

            local_root.mkdir(parents=True, exist_ok=True)
            total = len(candidates)
            for index, (remote_dir, relative_parts, filename, expected_size) in enumerate(candidates, start=1):
                target_dir = local_root.joinpath(*relative_parts)
                target_dir.mkdir(parents=True, exist_ok=True)
                target_path = (target_dir / filename).resolve()
                try:
                    target_path.relative_to(local_root)
                except ValueError:
                    result["failed"] += 1
                    result["errors"].append(f"مسیر ناامن رد شد: {filename}")
                    continue

                if target_path.exists():
                    try:
                        current_size = target_path.stat().st_size
                    except OSError:
                        current_size = -1
                    if current_size > 0 and (expected_size is None or current_size == expected_size):
                        result["skipped"] += 1
                        result["skipped_files"].append(str(target_path))
                    else:
                        result["failed"] += 1
                        result["errors"].append(
                            f"فایل محلی هم‌نام ولی با اندازه متفاوت وجود دارد؛ بازنویسی نشد: {target_path}"
                        )
                    continue

                part_path = target_path.with_name(
                    target_path.name + ".part-" + uuid.uuid4().hex[:8]
                )
                set_download_file_name(target_path.name)
                last_error: Exception | None = None
                for attempt in range(1, 4):
                    try:
                        ftp = ftp_holder[0]
                        if ftp is None:
                            ftp_holder[0] = connect()
                            ftp = ftp_holder[0]
                        ftp.cwd(remote_dir)
                        with part_path.open("wb") as output:
                            ftp.retrbinary(
                                f"RETR {filename}",
                                output.write,
                                blocksize=UPLOAD_BLOCK_SIZE,
                            )
                        received_size = part_path.stat().st_size
                        if expected_size is not None and received_size != expected_size:
                            raise IOError(
                                f"اندازه فایل دریافتی ({received_size}) با اندازه سرور ({expected_size}) برابر نیست."
                            )
                        os.replace(part_path, target_path)
                        result["downloaded"] += 1
                        result["bytes"] += received_size
                        result["files"].append(str(target_path))
                        last_error = None
                        break
                    except Exception as exc:
                        last_error = exc
                        try:
                            part_path.unlink(missing_ok=True)
                        except OSError:
                            pass
                        _close_ftp_quietly(ftp_holder[0])
                        ftp_holder[0] = None
                        if attempt < 3:
                            time.sleep(attempt * 2)

                if last_error is not None:
                    result["failed"] += 1
                    result["errors"].append(f"{filename}: {last_error}")
                if progress_callback:
                    progress_callback(
                        f"دریافت از سرور {index}/{total} | دانلود: {result['downloaded']} | "
                        f"موجود: {result['skipped']} | خطا: {result['failed']}"
                    )

            if result["failed"] and not result["downloaded"] and not result["skipped"]:
                raise RuntimeError("هیچ فایلی با موفقیت دریافت نشد: " + "; ".join(result["errors"][:3]))
            return result
        finally:
            _close_ftp_quietly(ftp_holder[0])


def sync_download_movie_files(
    remote_movie_dir: str,
    local_movie_dir: Path,
    server_config: dict,
    progress_callback: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """
    Download raw movie video files recursively from one FTP movie folder.
    Keep the relative directory layout and skip any filename/path containing
    SoftSub. Existing complete files are kept and incomplete transfers use
    isolated .part files.
    """
    remote_root = _normalize_remote_path(remote_movie_dir)
    local_root = Path(local_movie_dir).resolve()
    ftp_cfg = dict(server_config or {})
    host = str(ftp_cfg.get("host", "")).strip()
    username = str(ftp_cfg.get("username", "")).strip()
    password = str(ftp_cfg.get("password", "")).strip()
    if not host or not username or not password:
        raise RuntimeError("اطلاعات اتصال FTP برای دانلود فیلم کامل نیست.")

    try:
        port = int(ftp_cfg.get("port", 21) or 21)
        timeout = max(30, int(ftp_cfg.get("timeout", 120) or 120))
    except (TypeError, ValueError):
        port, timeout = 21, 120

    conn_params = {
        "host": host,
        "port": port,
        "username": username,
        "password": password,
        "protocol": str(ftp_cfg.get("protocol", "FTP")).strip().upper(),
        "timeout": timeout,
        "passive_mode": bool(ftp_cfg.get("passive_mode", True)),
    }
    ftp_holder: list[FTP | None] = [None]

    def connect() -> FTP:
        return _create_ftp_connection(**conn_params)

    result: dict[str, Any] = {
        "downloaded": 0,
        "skipped": 0,
        "failed": 0,
        "bytes": 0,
        "files": [],
        "skipped_files": [],
        "errors": [],
    }

    with _DOWNLOAD_LOCK:
        try:
            ftp_holder[0] = connect()
            candidates: list[tuple[str, tuple[str, ...], str, int | None]] = []
            visited: set[str] = set()

            def walk(remote_dir: str, relative_parts: tuple[str, ...], depth: int):
                normalized = _normalize_remote_path(remote_dir)
                key = normalized.casefold()
                if key in visited:
                    return
                visited.add(key)

                ftp = ftp_holder[0]
                if ftp is None:
                    ftp_holder[0] = connect()
                    ftp = ftp_holder[0]
                try:
                    remote_files, remote_dirs = _list_ftp_directory_for_download(ftp, normalized)
                except Exception as first_error:
                    _close_ftp_quietly(ftp_holder[0])
                    ftp_holder[0] = None
                    try:
                        ftp_holder[0] = connect()
                        remote_files, remote_dirs = _list_ftp_directory_for_download(
                            ftp_holder[0], normalized
                        )
                    except Exception as retry_error:
                        _close_ftp_quietly(ftp_holder[0])
                        ftp_holder[0] = None
                        if not relative_parts:
                            raise RuntimeError(
                                f"خواندن پوشه اصلی فیلم از FTP ناموفق بود: {retry_error}"
                            ) from first_error
                        result["failed"] += 1
                        result["errors"].append(
                            f"فهرست {normalized} خوانده نشد: {retry_error}"
                        )
                        return

                for filename, size in remote_files:
                    if Path(filename).suffix.casefold() not in _DOWNLOAD_MEDIA_EXTENSIONS:
                        continue
                    if filename.startswith(("_", ".")) or any(
                        marker in filename.casefold() for marker in ("softsub", ".hardsub.")
                    ):
                        continue
                    candidates.append((normalized, relative_parts, filename, size))

                if depth >= 6:
                    return

                for dirname in sorted(remote_dirs, key=str.casefold):
                    low_dir = dirname.casefold()
                    if (
                        "softsub" in low_dir
                        or low_dir in _DOWNLOAD_SKIP_DIRS
                        or low_dir.endswith("-hardsub")
                        or low_dir.startswith("_")
                        or not _safe_ftp_entry_name(dirname)
                    ):
                        continue
                    child_remote = _normalize_remote_path(f"{normalized}/{dirname}")
                    walk(child_remote, relative_parts + (dirname,), depth + 1)

            walk(remote_root, (), 0)
            candidates.sort(
                key=lambda item: ("/".join(item[1]).casefold(), item[2].casefold())
            )
            local_root.mkdir(parents=True, exist_ok=True)
            total = len(candidates)
            for index, (remote_dir, relative_parts, filename, expected_size) in enumerate(
                candidates,
                start=1,
            ):
                target_dir = local_root.joinpath(*relative_parts)
                target_dir.mkdir(parents=True, exist_ok=True)
                target_path = (target_dir / filename).resolve()
                try:
                    target_path.relative_to(local_root)
                except ValueError:
                    result["failed"] += 1
                    result["errors"].append(f"مسیر ناامن رد شد: {filename}")
                    continue

                if target_path.exists():
                    try:
                        current_size = target_path.stat().st_size
                    except OSError:
                        current_size = -1
                    if current_size > 0 and (expected_size is None or current_size == expected_size):
                        result["skipped"] += 1
                        result["skipped_files"].append(str(target_path))
                    else:
                        result["failed"] += 1
                        result["errors"].append(
                            f"فایل محلی هم‌نام با اندازه متفاوت وجود دارد؛ بازنویسی نشد: {target_path}"
                        )
                    continue

                part_path = target_path.with_name(
                    target_path.name + ".part-" + uuid.uuid4().hex[:8]
                )
                set_download_file_name(target_path.name)
                last_error: Exception | None = None
                for attempt in range(1, 4):
                    try:
                        ftp = ftp_holder[0]
                        if ftp is None:
                            ftp_holder[0] = connect()
                            ftp = ftp_holder[0]
                        ftp.cwd(remote_dir)
                        with part_path.open("wb") as output:
                            ftp.retrbinary(
                                f"RETR {filename}",
                                output.write,
                                blocksize=UPLOAD_BLOCK_SIZE,
                            )
                        received_size = part_path.stat().st_size
                        if expected_size is not None and received_size != expected_size:
                            raise IOError(
                                f"اندازه فایل دریافتی ({received_size}) با اندازه سرور "
                                f"({expected_size}) برابر نیست."
                            )
                        os.replace(part_path, target_path)
                        result["downloaded"] += 1
                        result["bytes"] += received_size
                        result["files"].append(str(target_path))
                        last_error = None
                        break
                    except Exception as exc:
                        last_error = exc
                        try:
                            part_path.unlink(missing_ok=True)
                        except OSError:
                            pass
                        _close_ftp_quietly(ftp_holder[0])
                        ftp_holder[0] = None
                        if attempt < 3:
                            time.sleep(attempt * 2)

                if last_error is not None:
                    result["failed"] += 1
                    result["errors"].append(f"{filename}: {last_error}")

                if progress_callback:
                    progress_callback(
                        f"دریافت فیلم از سرور {index}/{total} | دانلود: {result['downloaded']} | "
                        f"موجود: {result['skipped']} | خطا: {result['failed']}"
                    )

            if result["failed"] and not result["downloaded"] and not result["skipped"]:
                raise RuntimeError(
                    "هیچ فایل فیلمی با موفقیت دریافت نشد: "
                    + "; ".join(result["errors"][:3])
                )
            return result
        finally:
            _close_ftp_quietly(ftp_holder[0])


def sync_upload_cuteftp(
    local_target: Path,
    remote_subpath: str = "__CURRENT__",
    progress_callback: Callable[[str], None] | None = None,
    server_config: dict | None = None,
    *,
    preserve_filename: bool = False,
    skip_identical: bool = False,
) -> list[str]:
    import logging
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
                local_target = _upload_single_file_with_retry(
                    ftp_holder=ftp_holder,
                    conn_params=conn_params,
                    local_file=local_target,
                    remote_dest_dir=target_remote_dir,
                    progress_prefix="",
                    progress_callback=progress_callback,
                    preserve_filename=preserve_filename,
                    skip_identical=skip_identical,
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

                    file_p = _upload_single_file_with_retry(
                        ftp_holder=ftp_holder,
                        conn_params=conn_params,
                        local_file=file_p,
                        remote_dest_dir=dest_folder,
                        progress_prefix=prefix_str,
                        progress_callback=progress_callback,
                        preserve_filename=preserve_filename,
                    )
                    public_url = _build_public_url(dest_folder, file_p.name)
                    if public_url:
                        uploaded_urls.append(public_url)

                logging.info(f"✅ [FTP FOLDER UPLOAD DONE] Folder: {local_target.name} ({total_files} files)")

            return uploaded_urls

        finally:
            _close_ftp_quietly(ftp_holder[0])
