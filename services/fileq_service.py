import asyncio
import json
import logging
import os
import shutil
import subprocess
import time
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Callable

from yarl import URL

from core.config_loader import cfg
from core.queue_manager import set_download_file_name


_FILEQ_HOSTS = {"fileq.net", "www.fileq.net"}
_start_lock = asyncio.Lock()
_download_slots = asyncio.Semaphore(3)
_claimed_inodes: set[int] = set()
_claimed_names: set[str] = set()

_TEMP_SUFFIXES = {".crdownload", ".part", ".tmp", ".download"}
_IGNORED_NAMES = {"desktop.ini", "thumbs.db", ".ds_store"}


def is_fileq_url(target: str) -> bool:
    try:
        url = URL(target)
        return url.scheme in {"http", "https"} and url.host in _FILEQ_HOSTS and url.user is None
    except ValueError:
        return False


def _inbox_dir() -> Path:
    inbox = (cfg.data_dir / "_fileq_inbox").resolve()
    inbox.mkdir(parents=True, exist_ok=True)
    return inbox


def _profile_dir() -> Path:
    profile = (cfg.data_dir / "fileq_native_profile").resolve()
    profile.mkdir(parents=True, exist_ok=True)
    return profile


def _find_native_browser() -> str:
    configured = cfg.get("fileq", "browser_bin") or cfg.get("paths", "chrome_bin")
    if configured and Path(configured).is_file():
        return str(Path(configured).resolve())

    program_files = os.environ.get("PROGRAMFILES", r"C:\Program Files")
    program_files_x86 = os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")
    local_app_data = os.environ.get("LOCALAPPDATA", "")

    candidates = [
        Path(program_files) / "Google" / "Chrome" / "Application" / "chrome.exe",
        Path(program_files_x86) / "Google" / "Chrome" / "Application" / "chrome.exe",
        Path(local_app_data) / "Google" / "Chrome" / "Application" / "chrome.exe",
        Path(program_files_x86) / "Microsoft" / "Edge" / "Application" / "msedge.exe",
        Path(program_files) / "Microsoft" / "Edge" / "Application" / "msedge.exe",
        Path(program_files) / "BraveSoftware" / "Brave-Browser" / "Application" / "brave.exe",
        Path(local_app_data) / "BraveSoftware" / "Brave-Browser" / "Application" / "brave.exe",
    ]
    for path in candidates:
        if path.is_file():
            return str(path.resolve())

    for exe in ("chrome", "google-chrome", "msedge", "brave-browser", "chromium"):
        found = shutil.which(exe)
        if found:
            return found

    raise RuntimeError(
        "مرورگر Chrome/Edge/Brave روی سیستم پیدا نشد. مسیر آن را در config.yaml بخش fileq.browser_bin ثبت کنید."
    )


def _ensure_browser_download_prefs(profile_dir: Path, inbox_dir: Path) -> None:
    """تنظیم مسیر دانلود پیش‌فرض مرورگر بدون نیاز به CDP یا پرچم‌های اتوماسیون."""
    default_dir = profile_dir / "Default"
    default_dir.mkdir(parents=True, exist_ok=True)
    prefs_path = default_dir / "Preferences"

    prefs: dict = {}
    if prefs_path.is_file():
        try:
            prefs = json.loads(prefs_path.read_text(encoding="utf-8"))
        except Exception:
            prefs = {}

    download_cfg = prefs.setdefault("download", {})
    if (
        download_cfg.get("default_directory") == str(inbox_dir)
        and download_cfg.get("prompt_for_download") is False
    ):
        return

    download_cfg["default_directory"] = str(inbox_dir)
    download_cfg["prompt_for_download"] = False
    download_cfg["directory_upgrade"] = True
    prefs.setdefault("savefile", {})["default_directory"] = str(inbox_dir)
    prefs.setdefault("profile", {}).setdefault("default_content_setting_values", {})[
        "automatic_downloads"
    ] = 1

    try:
        prefs_path.write_text(json.dumps(prefs, ensure_ascii=False), encoding="utf-8")
    except OSError as exc:
        logging.warning("Could not update browser Preferences (browser may already be open): %s", exc)


def _open_in_native_browser(url: str, profile_dir: Path, inbox_dir: Path) -> None:
    browser_bin = _find_native_browser()
    _ensure_browser_download_prefs(profile_dir, inbox_dir)

    cmd = [
        browser_bin,
        f"--user-data-dir={profile_dir}",
        "--no-first-run",
        "--no-default-browser-check",
    ]

    # پروکسی اختیاری اختصاصی FileQ (فقط در صورت تنظیم در config.yaml)
    custom_proxy = cfg.get("fileq", "proxy")
    if custom_proxy:
        cmd.append(f"--proxy-server={custom_proxy}")

    cmd.append(url)
    logging.info("Opening FileQ in native browser (zero-CDP): %s", browser_bin)
    subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _scan_inbox(inbox_dir: Path) -> list[tuple[Path, os.stat_result]]:
    entries: list[tuple[Path, os.stat_result]] = []
    for path in inbox_dir.iterdir():
        if path.name.lower() in _IGNORED_NAMES or path.name.startswith("."):
            continue
        try:
            st = path.stat()
            if path.is_file():
                entries.append((path, st))
        except OSError:
            continue
    return entries


def _is_temp_download(path: Path) -> bool:
    return path.suffix.lower() in _TEMP_SUFFIXES


def _display_name_for(path: Path) -> str | None:
    name = path.name
    if name.startswith("Unconfirmed ") and name.lower().endswith(".crdownload"):
        return None
    if name.lower().endswith(".crdownload"):
        return name[: -len(".crdownload")]
    return name


def _find_tracked_file(
    inbox_dir: Path,
    tracked_ino: int,
    last_path: Path,
    expected_final_name: str | None,
    baseline_names: set[str],
) -> tuple[Path, os.stat_result] | None:
    entries = _scan_inbox(inbox_dir)

    # ۱. ردیابی دقیق بر اساس NTFS File Index (حتی پس از تغییر نام Unconfirmed -> .crdownload -> نهایی)
    if tracked_ino:
        for path, st in entries:
            if st.st_ino == tracked_ino:
                return path, st

    # ۲. بررسی مسیر قبلی
    for path, st in entries:
        if path == last_path:
            return path, st

    # ۳. بررسی نام نهایی مورد انتظار (در صورت تغییر inode هنگام اتمام دانلود)
    if expected_final_name and expected_final_name.casefold() not in baseline_names:
        for path, st in entries:
            if path.name.casefold() == expected_final_name.casefold():
                if not st.st_ino or st.st_ino not in _claimed_inodes:
                    return path, st

    return None


async def _move_when_unlocked(src: Path, dest_dir: Path) -> Path:
    dest_dir.mkdir(parents=True, exist_ok=True)
    target = dest_dir / src.name
    if target.exists():
        target = dest_dir / f"{src.stem}_{int(time.time())}{src.suffix}"

    # اطمینان از پایداری حجم و آزاد شدن قفل فایل توسط مرورگر/آنتی‌ویروس ویندوز
    prev_size = -1
    for _ in range(30):
        try:
            curr_size = src.stat().st_size
        except OSError:
            await asyncio.sleep(0.5)
            continue

        if curr_size > 0 and curr_size == prev_size:
            try:
                await asyncio.to_thread(shutil.move, str(src), str(target))
                return target
            except PermissionError:
                pass
        prev_size = curr_size
        await asyncio.sleep(1.0)

    await asyncio.to_thread(shutil.move, str(src), str(target))
    return target


async def download_fileq(
    target: str,
    dest_dir: Path,
    progress_callback: Callable[[str], None] | None = None,
) -> Path:
    if progress_callback:
        progress_callback("در انتظار نوبت FileQ...")

    inbox_dir = _inbox_dir()
    profile_dir = _profile_dir()
    start_timeout = float(cfg.get("fileq", "start_timeout_seconds", 300))
    url = str(URL(target).with_scheme("https"))

    tracked_ino = 0
    tracked_name_key: str | None = None
    tracked_path: Path | None = None
    expected_final_name: str | None = None
    baseline_names: set[str] = set()

    try:
        async with AsyncExitStack() as transfer:
            await transfer.enter_async_context(_download_slots)

            # تا زمانی که دانلود فعلی در مرورگر شروع نشده، لینک بعدی باز نمی‌شود
            async with _start_lock:
                initial_entries = _scan_inbox(inbox_dir)
                baseline_inodes = {st.st_ino for _, st in initial_entries if st.st_ino} | set(
                    _claimed_inodes
                )
                baseline_names = {p.name.casefold() for p, _ in initial_entries} | set(
                    _claimed_names
                )

                _open_in_native_browser(url, profile_dir, inbox_dir)

                if progress_callback:
                    progress_callback(
                        "FileQ در مرورگر واقعی باز شد؛ در صورت نیاز تأیید کلادفلر و کلیک دانلود را انجام دهید."
                    )

                deadline = time.monotonic() + start_timeout
                while time.monotonic() < deadline:
                    await asyncio.sleep(0.5)
                    for path, st in _scan_inbox(inbox_dir):
                        if path.suffix.lower() == ".tmp":
                            continue
                        if st.st_ino and st.st_ino in baseline_inodes:
                            continue
                        if path.name.casefold() in baseline_names:
                            continue

                        tracked_path = path
                        tracked_ino = st.st_ino
                        tracked_name_key = path.name.casefold()
                        expected_final_name = _display_name_for(path)
                        if tracked_ino:
                            _claimed_inodes.add(tracked_ino)
                        _claimed_names.add(tracked_name_key)
                        break

                    if tracked_path is not None:
                        break

                if tracked_path is None:
                    raise RuntimeError(
                        "دانلود FileQ در مهلت تعیین‌شده شروع نشد. صفحه را در مرورگر بررسی کنید."
                    )

            # ردیابی پیشرفت و اتمام دانلود بدون نگه داشتن _start_lock
            if expected_final_name:
                set_download_file_name(expected_final_name)
            if progress_callback:
                progress_callback("دانلود FileQ آغاز شد...")

            last_size = 0
            last_report_ts = time.monotonic()
            missing_ticks = 0

            while True:
                await asyncio.sleep(1.0)
                found = _find_tracked_file(
                    inbox_dir,
                    tracked_ino,
                    tracked_path,
                    expected_final_name,
                    baseline_names,
                )
                if found is None:
                    missing_ticks += 1
                    if missing_ticks >= 4:
                        raise RuntimeError("فایل دانلودی FileQ در مرورگر لغو یا حذف شد.")
                    continue

                missing_ticks = 0
                tracked_path, st = found
                if st.st_ino and st.st_ino != tracked_ino:
                    if tracked_ino:
                        _claimed_inodes.discard(tracked_ino)
                    tracked_ino = st.st_ino
                    _claimed_inodes.add(tracked_ino)

                display = _display_name_for(tracked_path)
                if display:
                    expected_final_name = display
                    set_download_file_name(display)

                if not _is_temp_download(tracked_path):
                    break

                now = time.monotonic()
                dt = now - last_report_ts
                if progress_callback and dt >= 2.0:
                    size_mb = st.st_size / (1024 * 1024)
                    speed_mb = max(0.0, (st.st_size - last_size) / dt) / (1024 * 1024)
                    label = (expected_final_name or "FileQ")[:28]
                    progress_callback(f"FileQ: {size_mb:.1f} MB | {speed_mb:.2f} MB/s ({label})")
                    last_size = st.st_size
                    last_report_ts = now

            final_file = await _move_when_unlocked(tracked_path, dest_dir)
            set_download_file_name(final_file.name)
            return dest_dir

    finally:
        if tracked_ino:
            _claimed_inodes.discard(tracked_ino)
        if tracked_name_key:
            _claimed_names.discard(tracked_name_key)