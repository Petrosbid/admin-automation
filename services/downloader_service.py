import asyncio
import re
from pathlib import Path
from typing import Callable
import gdown
from core.config_loader import cfg
from core.proxy_manager import ProxyManager


class DownloaderService:
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
    async def download(
        cls,
        url_or_magnet: str,
        dest_dir: Path,
        progress_callback: Callable[[str], None] = None
    ) -> Path:
        """تشخیص هوشمند نوع لینک و دانلود در مسیر مقصد"""
        dest_dir.mkdir(parents=True, exist_ok=True)
        url_lower = url_or_magnet.lower()

        if "drive.google.com" in url_lower:
            return await cls._download_gdrive(url_or_magnet, dest_dir, progress_callback)
        elif "mega.nz" in url_lower:
            return await cls._download_mega(url_or_magnet, dest_dir, progress_callback)
        else:
            return await cls._download_aria2(url_or_magnet, dest_dir, progress_callback)

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
            # استخراج درصد و سرعت دانلود از خروجی aria2c
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
        cmd = [megadl_bin, "--path", str(dest_dir)]
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