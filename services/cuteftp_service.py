import asyncio
from pathlib import Path
from typing import Callable
from services.cuteftp_worker import sync_upload_cuteftp


class CuteFTPService:
    @classmethod
    async def upload_path(
        cls,
        local_target: Path,
        remote_subpath: str = "",
        progress_callback: Callable[[str], None] = None
    ) -> list[str]:
        """ارسال غیرمسدودکننده پوشه یا فایل به نرم‌افزار CuteFTP 9 (نشست فعال GUI یا COM)"""
        if progress_callback:
            progress_callback(f"ارسال {local_target.name} به نرم‌افزار CuteFTP 9...")

        urls = await asyncio.to_thread(
            sync_upload_cuteftp,
            local_target.resolve(),
            remote_subpath
        )
        if progress_callback:
            progress_callback("به صف انتقال CuteFTP اضافه شد ✅")
        return urls