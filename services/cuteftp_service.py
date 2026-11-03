import asyncio
import urllib.parse
from pathlib import Path
from typing import Callable
import pythoncom
import win32com.client
from core.config_loader import cfg


class CuteFTPService:
    @staticmethod
    def _sync_upload_via_cuteftp(local_target: Path, remote_subpath: str = "") -> list[str]:
        """اجرای synchronous موتور COM برنامه CuteFTP 9 در ترد مجزا"""
        pythoncom.CoInitialize()
        try:
            ftp_cfg = cfg.get("cuteftp")
            te = win32com.client.Dispatch("CuteFTPPro.TEConnection")
            te.Option("ThrowError", False)
            te.Protocol = ftp_cfg.get("protocol", "FTP")
            te.Host = ftp_cfg["host"]
            te.Port = int(ftp_cfg.get("port", 21))
            te.Login = ftp_cfg["username"]
            te.Password = ftp_cfg["password"]

            te.Connect()

            base_remote = ftp_cfg["remote_base_dir"].rstrip("/")
            target_remote_dir = f"{base_remote}/{remote_subpath}".rstrip("/")
            base_url = ftp_cfg["base_download_url"].rstrip("/")

            generated_urls = []

            if local_target.is_file():
                te.RemoteFolder = target_remote_dir
                te.Upload(str(local_target))
                rel_url = f"{remote_subpath}/{local_target.name}".lstrip("/")
                generated_urls.append(f"{base_url}/{urllib.parse.quote(rel_url)}")
            else:
                # اگر ورودی یک پوشه کامل فیلم (حاوی softsub, hardsub, sub) باشد
                folder_name = local_target.name
                movie_remote_dir = f"{target_remote_dir}/{folder_name}"
                if not te.RemoteExists(movie_remote_dir):
                    te.CreateRemoteFolder(movie_remote_dir)

                # آپلود کل ساختار پوشه با موتور CuteFTP
                te.RemoteFolder = movie_remote_dir
                te.Upload(f"{local_target}\\*.*")

                for file_p in local_target.rglob("*"):
                    if file_p.is_file():
                        rel = file_p.relative_to(local_target.parent).as_posix()
                        generated_urls.append(f"{base_url}/{urllib.parse.quote(rel)}")

            te.Disconnect()
            return generated_urls
        finally:
            pythoncom.CoUninitialize()

    @classmethod
    async def upload_path(
            cls,
            local_target: Path,
            remote_subpath: str = "",
            progress_callback: Callable[[str], None] = None
    ) -> list[str]:
        """فراخوانی غیرمسدودکننده (Async) موتور CuteFTP 9"""
        if progress_callback:
            progress_callback(f"ارسال {local_target.name} به موتور انتقال CuteFTP 9...")

        urls = await asyncio.to_thread(
            cls._sync_upload_via_cuteftp,
            local_target.resolve(),
            remote_subpath
        )
        return urls