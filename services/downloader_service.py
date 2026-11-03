import asyncio
import json
import re
import time
from pathlib import Path
from typing import Callable
import gdown
from core.config_loader import cfg
from core.proxy_manager import ProxyManager


class DownloaderService:
    VIDEO_EXTENSIONS = {".mkv", ".mp4", ".avi", ".mov", ".ts", ".m4v", ".webm"}

    @staticmethod
    def create_movie_folders(movie_title: str) -> dict[str, Path]:
        """ساخت پوشه فیلم به همراه زیرپوشه‌های softsub, hardsub, sub"""
        workspace = Path(cfg.get("paths", "workspace_dir"))
        safe_name = re.sub(r'[<>:"/\\|?*]', "", movie_title).strip().replace(" ", "_")
        base_dir = workspace / safe_name

        folders = {
            "base": base_dir,
            "softsub": base_dir / "softsub",
            "hardsub": base_dir / "hardsub",
            "sub": base_dir / "sub",
        }
        for p in folders.values():
            p.mkdir(parents=True, exist_ok=True)
        return folders

    @classmethod
    def find_video_files(cls, directory: Path) -> list[Path]:
        """پیدا کردن تمامی فایل‌های ویدئویی موجود در پوشه (مرتب شده بر اساس حجم)"""
        if not directory.exists():
            return []
        videos = [
            f for f in directory.rglob("*")
            if f.is_file()
            and f.suffix.lower() in cls.VIDEO_EXTENSIONS
            and not f.name.startswith("_temp_")
        ]
        return sorted(videos, key=lambda f: f.stat().st_size, reverse=True)

    @staticmethod
    def is_torrent_target(target: str) -> bool:
        """تشخیص اینکه ورودی لینک مگنت یا فایل/لینک تورنت است یا خیر"""
        t_low = target.lower().strip()
        if t_low.startswith("magnet:?"):
            return True
        if t_low.endswith(".torrent"):
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
        """
        تشخیص هوشمند نوع لینک:
        - تورنت و مگنت -> qbittorrent-cli (در صورت نصب بودن) یا aria2c
        - گوگل درایو -> gdown
        - مگا -> megatools
        - لینک مستقیم HTTP/HTTPS -> aria2c
        """
        dest_dir.mkdir(parents=True, exist_ok=True)
        url_lower = url_or_magnet.lower().strip()

        if cls.is_torrent_target(url_or_magnet):
            qbt_bin = cfg.get("paths", "qbt_bin")
            if qbt_bin and Path(qbt_bin).exists():
                return await cls._download_qbt(url_or_magnet, dest_dir, progress_callback)
            return await cls._download_aria2(url_or_magnet, dest_dir, progress_callback)
        elif "drive.google.com" in url_lower:
            return await cls._download_gdrive(url_or_magnet, dest_dir, progress_callback)
        elif "mega.nz" in url_lower:
            return await cls._download_mega(url_or_magnet, dest_dir, progress_callback)
        else:
            return await cls._download_aria2(url_or_magnet, dest_dir, progress_callback)

    @staticmethod
    def _qbt_auth_args() -> list[str]:
        """پارامترهای اتصال qbt.exe به WebUI برنامه qBittorrent در ویندوز"""
        qbt_cfg = cfg.get("qbittorrent") or {}
        args = []
        if qbt_cfg.get("webui_url"):
            args.extend(["--url", qbt_cfg["webui_url"]])
        if qbt_cfg.get("username"):
            args.extend(["--username", qbt_cfg["username"]])
        if qbt_cfg.get("password"):
            args.extend(["--password", qbt_cfg["password"]])
        return args

    @classmethod
    async def _download_qbt(
        cls,
        target: str,
        dest_dir: Path,
        progress_callback: Callable[[str], None] = None
    ) -> Path:
        """دانلود تورنت با استفاده از qbittorrent-cli (qbt.exe)"""
        qbt_bin = cfg.get("paths", "qbt_bin")
        auth_args = cls._qbt_auth_args()
        dest_resolved = str(dest_dir.resolve())

        try:
            is_local_file = Path(target).is_file()
        except OSError:
            is_local_file = False

        if is_local_file:
            add_cmd = [qbt_bin, "torrent", "add", "file", str(Path(target).resolve()), "--folder", dest_resolved] + auth_args
        else:
            add_cmd = [qbt_bin, "torrent", "add", "url", target, "--folder", dest_resolved] + auth_args

        proc = await asyncio.create_subprocess_exec(
            *add_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT
        )
        stdout, _ = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(f"qbt torrent add error: {stdout.decode('utf-8', errors='replace')}")

        if progress_callback:
            progress_callback("تورنت به qBittorrent اضافه شد. در حال دریافت متادیتا...")

        completed_states = {"uploading", "stalledup", "pausedup", "queuedup", "checkingup", "forcedup"}
        matched_hash = None
        start_ts = time.time()

        while True:
            await asyncio.sleep(3)
            list_cmd = [qbt_bin, "torrent", "list", "--format", "json"] + auth_args
            l_proc = await asyncio.create_subprocess_exec(
                *list_cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            l_out, _ = await l_proc.communicate()
            try:
                torrents = json.loads(l_out.decode("utf-8", errors="replace"))
            except Exception:
                continue

            if not isinstance(torrents, list):
                continue

            target_torrent = None
            for t in torrents:
                save_p = str(t.get("save_path", "")).rstrip("\\/")
                if matched_hash and t.get("hash") == matched_hash:
                    target_torrent = t
                    break
                elif save_p.lower() == dest_resolved.rstrip("\\/").lower():
                    target_torrent = t
                    matched_hash = t.get("hash")
                    break

            if not target_torrent and torrents and (time.time() - start_ts < 30):
                newest = max(torrents, key=lambda x: x.get("added_on", 0))
                target_torrent = newest
                matched_hash = newest.get("hash")

            if not target_torrent:
                continue

            prog = float(target_torrent.get("progress", 0.0)) * 100.0
            state = str(target_torrent.get("state", "")).lower()
            dlspeed_mb = float(target_torrent.get("dlspeed", 0)) / (1024 * 1024)
            name = target_torrent.get("name", "Torrent")

            if progress_callback:
                progress_callback(f"{prog:.1f}% | سرعت: {dlspeed_mb:.2f} MB/s | وضعیت: {state} ({name[:25]})")

            if prog >= 100.0 or state in completed_states:
                if matched_hash:
                    del_cmd = [qbt_bin, "torrent", "delete", matched_hash] + auth_args
                    d_proc = await asyncio.create_subprocess_exec(
                        *del_cmd,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE
                    )
                    await d_proc.communicate()
                break

        return dest_dir

    @staticmethod
    async def _download_aria2(
        target: str,
        dest_dir: Path,
        progress_callback: Callable[[str], None] = None
    ) -> Path:
        aria2_bin = cfg.get("paths", "aria2c_bin")
        cmd = [
            aria2_bin,
            f"--dir={dest_dir}",
            "--seed-time=0",
            "--max-connection-per-server=16",
            "--split=16",
            "--continue=true",
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

        while True:
            line = await proc.stdout.readline()
            if not line:
                break
            text = line.decode("utf-8", errors="replace").strip()
            if "[#" in text and progress_callback:
                progress_callback(text)

        await proc.wait()
        if proc.returncode != 0:
            raise RuntimeError(f"Aria2c Download Failed (Exit code {proc.returncode})")

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

    @staticmethod
    async def _download_mega(
        mega_url: str,
        dest_dir: Path,
        progress_callback: Callable[[str], None] = None
    ) -> Path:
        megadl_bin = cfg.get("paths", "megadl_bin")
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