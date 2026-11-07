import asyncio
from pathlib import Path
from typing import Callable

from core.config_loader import cfg
from services.cuteftp_worker import (
    sync_upload_cuteftp,
    get_current_cuteftp_remote_dir,
    list_ftp_dir_entries,
)


class CuteFTPService:
    DEFAULT_DESTINATIONS = [
        {"label": "🎬 فیلم‌های خارجی (/English/Movies)", "path": "/English/Movies"},
        {"label": "📺 سریال‌های خارجی (/English/Series)", "path": "/English/Series"},
        {"label": "🎌 انیمه‌ها (/English/Anime)", "path": "/English/Anime"},
    ]

    @classmethod
    def get_saved_destinations(cls) -> list[dict[str, str]]:
        saved = cfg.get("cuteftp", "saved_destinations", None)
        if isinstance(saved, list) and saved:
            return saved
        return list(cls.DEFAULT_DESTINATIONS)

    @classmethod
    def add_saved_destination(cls, label: str, remote_path: str):
        clean_path = "/" + remote_path.replace("\\", "/").strip().lstrip("/")
        if len(clean_path) > 1:
            clean_path = clean_path.rstrip("/")
        current = cls.get_saved_destinations()
        if not any(d.get("path") == clean_path for d in current):
            current.append({"label": f"📂 {label} ({clean_path})", "path": clean_path})
            cfg.set_value("cuteftp", "saved_destinations", current)

    @staticmethod
    async def get_active_gui_remote_path() -> str:
        return await asyncio.to_thread(get_current_cuteftp_remote_dir)

    @staticmethod
    async def inspect_remote_dir(remote_dir: str) -> dict[str, list[str]]:
        """بررسی غیرهمگام محتویات یک پوشه روی سرور FTP (فایل‌ها، زیرپوشه‌ها و وضعیت وجود)"""
        return await asyncio.to_thread(list_ftp_dir_entries, remote_dir)

    @staticmethod
    async def upload_path(
        local_target: Path,
        remote_subpath: str = "__CURRENT__",
        progress_callback: Callable[[str], None] | None = None
    ) -> list[str]:
        return await asyncio.to_thread(
            sync_upload_cuteftp,
            Path(local_target),
            remote_subpath,
            progress_callback
        )

    @classmethod
    async def upload_multiple_or_single(
        cls,
        targets: list[Path] | Path,
        remote_subpath: str = "__CURRENT__",
        progress_callback: Callable[[str], None] | None = None
    ) -> list[str]:
        if isinstance(targets, (str, Path)):
            targets_list = [Path(targets)]
        else:
            targets_list = [Path(t) for t in targets]

        all_links: list[str] = []
        total = len(targets_list)

        for idx, item in enumerate(targets_list, start=1):
            def _sub_prog(msg: str, i=idx):
                if progress_callback:
                    prefix = f"آیتم {i}/{total} - " if total > 1 else ""
                    progress_callback(f"{prefix}{msg}")

            links = await cls.upload_path(
                local_target=item,
                remote_subpath=remote_subpath,
                progress_callback=_sub_prog
            )
            all_links.extend(links)

        return all_links