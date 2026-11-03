import asyncio
import json
from pathlib import Path
from typing import Callable
from core.config_loader import cfg
from services.cuteftp_worker import sync_upload_cuteftp, get_current_cuteftp_remote_dir


class CuteFTPService:
    # مسیرهای پیش‌فرض منطبق با ساختار واقعی سرور شما در CuteFTP
    DEFAULT_DESTINATIONS = [
        {"label": "🎬 فیلم‌ها (/English/Movies)", "path": "/English/Movies"},
        {"label": "📺 سریال‌ها (/English/Series)", "path": "/English/Series"},
        {"label": "📂 پوشه English (/English)", "path": "/English"},
        {"label": "🏠 پوشه اصلی هاست (/)", "path": "/"},
    ]

    @classmethod
    def get_saved_destinations(cls) -> list[dict[str, str]]:
        """خواندن لیست مسیرهای پرکاربرد هاست دانلود"""
        ws = Path(cfg.get("paths", "workspace_dir", "./workspace"))
        dest_file = ws / "cuteftp_destinations.json"
        if dest_file.exists():
            try:
                data = json.loads(dest_file.read_text(encoding="utf-8"))
                if isinstance(data, list) and data:
                    # ارتقای خودکار مسیرهای قدیمی که بدون /English بودند
                    upgraded = []
                    for item in data:
                        p = item.get("path", "")
                        if p in ("Movies", "/Movies"):
                            item["path"] = "/English/Movies"
                            item["label"] = "🎬 فیلم‌ها (/English/Movies)"
                        elif p in ("Series", "/Series"):
                            item["path"] = "/English/Series"
                            item["label"] = "📺 سریال‌ها (/English/Series)"
                        upgraded.append(item)
                    return upgraded
            except Exception:
                pass
        return cls.DEFAULT_DESTINATIONS

    @classmethod
    def add_saved_destination(cls, label: str, remote_path: str):
        """ذخیره یک مسیر جدید در لیست مسیرهای آماده CuteFTP"""
        ws = Path(cfg.get("paths", "workspace_dir", "./workspace"))
        ws.mkdir(parents=True, exist_ok=True)
        dest_file = ws / "cuteftp_destinations.json"
        norm_path = "/" + remote_path.replace("\\", "/").strip().lstrip("/")
        current = cls.get_saved_destinations()
        if not any(d["path"] == norm_path for d in current):
            current.append({"label": f"📂 {label} ({norm_path})", "path": norm_path})
            dest_file.write_text(json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8")

    @staticmethod
    async def get_active_gui_remote_path() -> str:
        return await asyncio.to_thread(get_current_cuteftp_remote_dir)

    @classmethod
    async def upload_path(
        cls,
        local_target: Path,
        remote_subpath: str = "__CURRENT__",
        progress_callback: Callable[[str], None] = None
    ) -> list[str]:
        dest_label = "مسیر فعلی CuteFTP" if remote_subpath == "__CURRENT__" else remote_subpath
        if progress_callback:
            progress_callback(f"تغییر مسیر CuteFTP به [{dest_label}] و ارسال {local_target.name}...")

        urls = await asyncio.to_thread(
            sync_upload_cuteftp,
            local_target.resolve(),
            remote_subpath
        )
        if progress_callback:
            progress_callback(f"در مسیر [{dest_label}] به صف CuteFTP اضافه شد ✅")
        return urls