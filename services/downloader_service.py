import asyncio
import json
import logging
import os
import re
import shutil
import time
from pathlib import Path
from typing import Callable
import aiohttp
import gdown
from core.config_loader import cfg
from core.proxy_manager import ProxyManager


class _TorrentAlreadySubmittedError(RuntimeError):
    """The torrent was accepted by qBittorrent; do not fall back and start a duplicate download."""


class DownloaderService:
    VIDEO_EXTENSIONS = {".mkv", ".mp4", ".avi", ".mov", ".ts", ".m4v", ".webm"}

    @staticmethod
    def _resolve_binary(config_key: str, exe_name: str, common_paths: list[str] = None) -> str:
        configured = cfg.get("paths", config_key)
        if configured and Path(configured).is_file():
            return str(Path(configured))

        in_path = shutil.which(exe_name)
        if in_path:
            return in_path

        for cp in (common_paths or []):
            if Path(cp).is_file():
                return cp

        return configured or exe_name

    @staticmethod
    def list_workspace_folders() -> list[Path]:
        """لیست کردن تمامی پوشه‌های فیلم/سریال موجود در Workspace"""
        workspace = Path(cfg.get("paths", "workspace_dir", "./workspace")).resolve()
        workspace.mkdir(parents=True, exist_ok=True)
        return sorted(
            [
                p for p in workspace.iterdir()
                if p.is_dir() and not p.name.startswith("_") and p.name != "browser_profile"
            ],
            key=lambda x: x.name.lower()
        )

    @staticmethod
    def create_movie_folders(movie_title: str) -> dict[str, Path]:
        """ساخت پوشه فیلم به همراه زیرپوشه‌های استاندارد SoftSub, Hardsub, srt"""
        workspace = Path(cfg.get("paths", "workspace_dir")).resolve()
        safe_name = re.sub(r'[<>:"/\\|?*]', "", movie_title).strip().replace(" ", ".")
        if not safe_name:
            safe_name = "Media_Download"
        base_dir = workspace / safe_name

        folders = {
            "base": base_dir,
            "softsub": base_dir / "SoftSub",
            "hardsub": base_dir / "Hardsub",
            "sub": base_dir / "srt",
        }
        for p in folders.values():
            p.mkdir(parents=True, exist_ok=True)
        return folders

    @classmethod
    def find_video_files(cls, directory: Path, recursive: bool = True) -> list[Path]:
        """پیدا کردن فایل‌های ویدئویی موجود در پوشه"""
        if not directory.exists():
            return []
        iterator = directory.rglob("*") if recursive else directory.iterdir()
        videos = [
            f for f in iterator
            if f.is_file()
            and f.suffix.lower() in cls.VIDEO_EXTENSIONS
            and not f.name.startswith("_temp_")
        ]
        return sorted(videos, key=lambda f: f.stat().st_size, reverse=True)

    @staticmethod
    def is_torrent_target(target: str) -> bool:
        t_low = target.lower().strip()
        if t_low.startswith("magnet:?"):
            return True
        if t_low.split("?")[0].endswith(".torrent"):
            return True
        try:
            p = Path(target)
            if p.is_file() and p.suffix.lower() == ".torrent":
                return True
        except OSError:
            pass
        return False

    @classmethod
    async def download(
        cls,
        url_or_magnet: str,
        dest_dir: Path,
        progress_callback: Callable[[str], None] = None
    ) -> Path:
        dest_dir.mkdir(parents=True, exist_ok=True)
        target = url_or_magnet.strip()
        url_lower = target.lower()

        if cls.is_torrent_target(target):
            try:
                return await cls._download_qbt_webui_api(target, dest_dir, progress_callback)
            except _TorrentAlreadySubmittedError:
                raise
            except Exception as webui_err:
                logging.warning(f"qBittorrent WebUI API fallback ({webui_err}), trying qbt.exe / aria2c...")

            qbt_bin = cls._resolve_binary("qbt_bin", "qbt.exe", [r"C:\Tools\qbt\qbt.exe"])
            if Path(qbt_bin).is_file():
                try:
                    return await cls._download_qbt_cli(qbt_bin, target, dest_dir, progress_callback)
                except Exception as qbt_err:
                    logging.warning(f"qbt.exe failed ({qbt_err}), falling back to aria2c...")

            return await cls._download_aria2(target, dest_dir, progress_callback)

        elif "drive.google.com" in url_lower:
            return await cls._download_gdrive(target, dest_dir, progress_callback)
        elif "mega.nz" in url_lower:
            return await cls._download_mega(target, dest_dir, progress_callback)
        else:
            try:
                return await cls._download_aria2(target, dest_dir, progress_callback)
            except Exception as aria_err:
                logging.warning(f"aria2c failed ({aria_err}), falling back to direct HTTP stream download...")
                return await cls._download_http_direct(target, dest_dir, progress_callback)

    @classmethod
    async def _download_qbt_webui_api(
        cls,
        target: str,
        dest_dir: Path,
        progress_callback: Callable[[str], None] = None
    ) -> Path:
        qbt_cfg = cfg.get("qbittorrent") or {}
        base_url = str(qbt_cfg.get("webui_url", "http://127.0.0.1:8080")).rstrip("/")
        username = qbt_cfg.get("username", "admin")
        password = qbt_cfg.get("password", "adminpassword")
        dest_resolved = str(dest_dir.resolve())
        dest_key = os.path.normcase(os.path.normpath(dest_resolved)).casefold()

        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            login_resp = await session.post(
                f"{base_url}/api/v2/auth/login",
                data={"username": username, "password": password}
            )
            if login_resp.status != 200:
                raise RuntimeError(f"qBittorrent WebUI HTTP {login_resp.status}")

            # Snapshot قبل از افزودن تورنت؛ هیچ وقت «جدیدترین تورنت کل برنامه»
            # را به‌عنوان دانلود این تسک فرض نکن.
            before_resp = await session.get(f"{base_url}/api/v2/torrents/info")
            if before_resp.status != 200:
                raise RuntimeError(f"qBittorrent info HTTP {before_resp.status}")
            before_torrents = await before_resp.json()
            before_hashes = {
                str(item.get("hash", "")).casefold()
                for item in (before_torrents if isinstance(before_torrents, list) else [])
                if item.get("hash")
            }

            data = aiohttp.FormData()
            data.add_field("savepath", dest_resolved)
            try:
                is_local_file = Path(target).is_file()
            except OSError:
                is_local_file = False

            if is_local_file:
                t_file = Path(target)
                data.add_field(
                    "torrents",
                    t_file.read_bytes(),
                    filename=t_file.name,
                    content_type="application/x-bittorrent"
                )
            else:
                data.add_field("urls", target)

            add_resp = await session.post(f"{base_url}/api/v2/torrents/add", data=data)
            if add_resp.status != 200:
                raise RuntimeError(f"qBittorrent add failed: {await add_resp.text()}")

            # از این نقطه تورنت به qBittorrent تحویل داده شده؛ fallback به aria2
            # خطر دانلود موازی/تکراری دارد و نباید اجرا شود.
            start_ts = time.time()
            matched_hash: str | None = None
            target_torrent: dict | None = None
            registration_deadline = start_ts + 90

            while time.time() < registration_deadline and not matched_hash:
                await asyncio.sleep(2)
                info_resp = await session.get(f"{base_url}/api/v2/torrents/info")
                if info_resp.status != 200:
                    continue
                torrents = await info_resp.json()
                if not isinstance(torrents, list):
                    continue

                candidates = []
                for item in torrents:
                    raw_save = str(item.get("save_path", "") or "")
                    save_key = os.path.normcase(os.path.normpath(raw_save)).casefold()
                    torrent_hash = str(item.get("hash", "")).casefold()
                    if save_key == dest_key and torrent_hash and torrent_hash not in before_hashes:
                        candidates.append(item)

                if len(candidates) == 1:
                    target_torrent = candidates[0]
                    matched_hash = str(target_torrent.get("hash"))
                elif len(candidates) > 1:
                    # اگر چند فایل تورنت با همین مسیر به طور همزمان اضافه شده‌اند،
                    # نام/لینک همین درخواست را تطبیق می‌دهیم؛ وگرنه fail closed.
                    target_names = {
                        Path(target).name.casefold(),
                        Path(target).stem.casefold(),
                    }
                    named = [
                        item for item in candidates
                        if str(item.get("name", "")).casefold() in target_names
                        or Path(str(item.get("name", ""))).stem.casefold() in target_names
                    ]
                    if len(named) == 1:
                        target_torrent = named[0]
                        matched_hash = str(target_torrent.get("hash"))

            if not matched_hash:
                raise _TorrentAlreadySubmittedError(
                    "تورنت به qBittorrent تحویل شد اما تا ۹۰ ثانیه شناسهٔ یکتای آن در مسیر اختصاصی این دانلود پیدا نشد؛ "
                    "برای جلوگیری از دانلود تکراری، fallback انجام نشد."
                )

            if progress_callback:
                progress_callback("تورنت در qBittorrent شناسایی شد؛ دریافت فایل‌ها آغاز شد...")

            while True:
                await asyncio.sleep(3)
                info_resp = await session.get(f"{base_url}/api/v2/torrents/info")
                if info_resp.status != 200:
                    continue
                torrents = await info_resp.json()
                if not isinstance(torrents, list):
                    continue

                target_torrent = next(
                    (
                        item for item in torrents
                        if str(item.get("hash", "")).casefold() == matched_hash.casefold()
                    ),
                    None,
                )
                if target_torrent is None:
                    # حذف از qBittorrent پس از تکمیل، یا حذف دستی؛ وضعیت واقعی روی دیسک
                    # را caller بررسی خواهد کرد.
                    break

                progress = float(target_torrent.get("progress", 0.0)) * 100.0
                state = str(target_torrent.get("state", "")).lower()
                speed_mb = float(target_torrent.get("dlspeed", 0)) / (1024 * 1024)
                name = str(target_torrent.get("name", "Torrent"))

                if progress_callback:
                    progress_callback(f"{progress:.1f}% | سرعت: {speed_mb:.2f} MB/s ({name[:25]})")

                if progress >= 100.0:
                    await session.post(
                        f"{base_url}/api/v2/torrents/delete",
                        data={"hashes": matched_hash, "deleteFiles": "false"}
                    )
                    break

        return dest_dir

    @classmethod
    async def _download_qbt_cli(
        cls,
        qbt_bin: str,
        target: str,
        dest_dir: Path,
        progress_callback: Callable[[str], None] = None
    ) -> Path:
        qbt_cfg = cfg.get("qbittorrent") or {}
        auth_args = []
        if qbt_cfg.get("webui_url"):
            auth_args.extend(["--url", qbt_cfg["webui_url"]])
        if qbt_cfg.get("username"):
            auth_args.extend(["--username", qbt_cfg["username"]])
        if qbt_cfg.get("password"):
            auth_args.extend(["--password", qbt_cfg["password"]])

        dest_resolved = str(dest_dir.resolve())
        dest_key = os.path.normcase(os.path.normpath(dest_resolved)).casefold()

        list_cmd = [qbt_bin, "torrent", "list", "--format", "json"] + auth_args
        before_proc = await asyncio.create_subprocess_exec(
            *list_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        before_out, _ = await before_proc.communicate()
        try:
            before_items = json.loads(before_out.decode("utf-8", errors="replace"))
        except Exception:
            before_items = []
        before_hashes = {
            str(item.get("hash", "")).casefold()
            for item in (before_items if isinstance(before_items, list) else [])
            if item.get("hash")
        }

        try:
            is_local_file = Path(target).is_file()
        except OSError:
            is_local_file = False

        if is_local_file:
            add_cmd = [
                qbt_bin, "torrent", "add", "file",
                str(Path(target).resolve()), "--folder", dest_resolved,
            ] + auth_args
        else:
            add_cmd = [
                qbt_bin, "torrent", "add", "url",
                target, "--folder", dest_resolved,
            ] + auth_args

        proc = await asyncio.create_subprocess_exec(
            *add_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        stdout, _ = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(f"qbt error: {stdout.decode('utf-8', errors='replace')}")

        # Torrent has been accepted; never switch to aria2 or another torrent because
        # that would duplicate the just-submitted download.
        start_ts = time.time()
        matched_hash: str | None = None
        target_torrent: dict | None = None
        try:
            while True:
                await asyncio.sleep(3)
                l_proc = await asyncio.create_subprocess_exec(
                    *list_cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                l_out, _ = await l_proc.communicate()
                try:
                    torrents = json.loads(l_out.decode("utf-8", errors="replace"))
                except Exception:
                    torrents = []
                if not isinstance(torrents, list):
                    continue

                if matched_hash:
                    target_torrent = next(
                        (
                            item for item in torrents
                            if str(item.get("hash", "")).casefold() == matched_hash.casefold()
                        ),
                        None,
                    )
                else:
                    candidates = []
                    for item in torrents:
                        save_p = str(item.get("save_path", "") or "")
                        save_key = os.path.normcase(os.path.normpath(save_p)).casefold()
                        torrent_hash = str(item.get("hash", "")).casefold()
                        if save_key == dest_key and torrent_hash and torrent_hash not in before_hashes:
                            candidates.append(item)
                    if len(candidates) == 1:
                        target_torrent = candidates[0]
                        matched_hash = str(target_torrent.get("hash"))
                    elif len(candidates) > 1:
                        target_names = {Path(target).name.casefold(), Path(target).stem.casefold()}
                        named = [
                            item for item in candidates
                            if str(item.get("name", "")).casefold() in target_names
                            or Path(str(item.get("name", ""))).stem.casefold() in target_names
                        ]
                        if len(named) == 1:
                            target_torrent = named[0]
                            matched_hash = str(target_torrent.get("hash"))

                if not matched_hash:
                    if time.time() - start_ts > 90:
                        raise _TorrentAlreadySubmittedError(
                            "qbt.exe تورنت را ثبت کرد اما شناسه یکتای آن در مسیر اختصاصی این دانلود پیدا نشد؛ "
                            "برای جلوگیری از دانلود تکراری fallback انجام نشد."
                        )
                    continue

                if target_torrent is None:
                    # Torrent disappeared from the client; caller will validate the staged files.
                    break

                progress = float(target_torrent.get("progress", 0.0)) * 100.0
                speed_mb = float(target_torrent.get("dlspeed", 0)) / (1024 * 1024)
                if progress_callback:
                    progress_callback(f"{progress:.1f}% | {speed_mb:.2f} MB/s")

                if progress >= 100.0:
                    del_cmd = [qbt_bin, "torrent", "delete", matched_hash] + auth_args
                    d_proc = await asyncio.create_subprocess_exec(
                        *del_cmd,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                    )
                    await d_proc.communicate()
                    break

        except _TorrentAlreadySubmittedError:
            raise
        except Exception as exc:
            raise _TorrentAlreadySubmittedError(
                f"qbt.exe تورنت را ثبت کرده اما هنگام ردیابی همان دانلود خطا رخ داد: {exc}"
            ) from exc

        return dest_dir

    @classmethod
    async def _download_aria2(
        cls,
        target: str,
        dest_dir: Path,
        progress_callback: Callable[[str], None] = None
    ) -> Path:
        aria2_bin = cls._resolve_binary(
            "aria2c_bin",
            "aria2c.exe",
            [r"C:\Tools\aria2\aria2c.exe", r"C:\Program Files\aria2\aria2c.exe"]
        )
        cmd = [
            aria2_bin,
            f"--dir={dest_dir.resolve()}",
            "--seed-time=0",
            "--max-connection-per-server=16",
            "--split=16",
            "--continue=true",
            "--check-certificate=false",
            "--summary-interval=2",
        ]
        proxy_url = ProxyManager.get_proxy_url()
        if proxy_url and proxy_url.startswith("http"):
            cmd.append(f"--all-proxy={proxy_url}")

        cmd.append(target)

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT
        )

        last_err_lines = []
        while True:
            line = await proc.stdout.readline()
            if not line:
                break
            text = line.decode("utf-8", errors="replace").strip()
            if text:
                last_err_lines.append(text)
                if len(last_err_lines) > 10:
                    last_err_lines.pop(0)
            if ("%" in text or "[#" in text) and progress_callback:
                progress_callback(text[:70])

        await proc.wait()
        if proc.returncode != 0:
            err_detail = " | ".join(last_err_lines[-3:])
            raise RuntimeError(f"Aria2c Exit {proc.returncode}: {err_detail}")

        return dest_dir

    @staticmethod
    async def _download_http_direct(
        url: str,
        dest_dir: Path,
        progress_callback: Callable[[str], None] = None
    ) -> Path:
        filename = url.split("?")[0].rstrip("/").split("/")[-1] or "downloaded_video.mkv"
        if "." not in filename:
            filename += ".mkv"
        out_file = dest_dir / filename

        connector = ProxyManager.get_aiohttp_connector()
        async with aiohttp.ClientSession(connector=connector) as session:
            async with session.get(url, ssl=False) as resp:
                resp.raise_for_status()
                total = int(resp.headers.get("Content-Length", 0))
                downloaded = 0
                last_report = 0.0

                with open(out_file, "wb") as f:
                    async for chunk in resp.content.iter_chunked(1024 * 256):
                        f.write(chunk)
                        downloaded += len(chunk)
                        now = time.time()
                        if progress_callback and (now - last_report >= 2.0):
                            last_report = now
                            if total > 0:
                                pct = (downloaded / total) * 100
                                progress_callback(f"{pct:.1f}% ({downloaded / (1024*1024):.1f} MB)")
                            else:
                                progress_callback(f"{downloaded / (1024*1024):.1f} MB دانلود شد...")

        return dest_dir

    @staticmethod
    async def _download_gdrive(
        gdrive_url: str,
        dest_dir: Path,
        progress_callback: Callable[[str], None] = None
    ) -> Path:
        if progress_callback:
            progress_callback("در حال دانلود از Google Drive...")

        proxy_url = ProxyManager.get_proxy_url()

        def _sync_gdown():
            if "/folders/" in gdrive_url:
                gdown.download_folder(url=gdrive_url, output=str(dest_dir), quiet=False, proxy=proxy_url)
            else:
                gdown.download(url=gdrive_url, output=str(dest_dir) + "\\", quiet=False, fuzzy=True, proxy=proxy_url)

        await asyncio.to_thread(_sync_gdown)
        return dest_dir

    @classmethod
    async def _download_mega(
        cls,
        mega_url: str,
        dest_dir: Path,
        progress_callback: Callable[[str], None] = None
    ) -> Path:
        megadl_bin = cls._resolve_binary(
            "megadl_bin",
            "megatools.exe",
            [r"C:\Tools\megatools\megatools.exe", r"C:\Tools\megatools\megadl.exe"]
        )
        cmd = (
            [megadl_bin, "dl", "--path", str(dest_dir)]
            if "megatools" in megadl_bin.lower()
            else [megadl_bin, "--path", str(dest_dir)]
        )
        proxy_url = ProxyManager.get_proxy_url()
        if proxy_url:
            cmd.extend(["--proxy", proxy_url])
        cmd.append(mega_url)

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT
        )

        while True:
            line = await proc.stdout.readline()
            if not line:
                break
            text = line.decode("utf-8", errors="replace").strip()
            if "%" in text and progress_callback:
                progress_callback(text)

        await proc.wait()
        if proc.returncode != 0:
            raise RuntimeError("خطا در دانلود از Mega.nz")
        return dest_dir