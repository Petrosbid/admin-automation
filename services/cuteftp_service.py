import asyncio
import json
from pathlib import Path
from typing import Callable
from core.config_loader import cfg
from services.cuteftp_worker import sync_upload_cuteftp, get_current_cuteftp_remote_dir


class CuteFTPService:
    DEFAULT_DESTINATIONS = [
        {"label": "🎬 فیلم‌ها (/English/Movies)", "path": "/English/Movies"},
        {"label": "📺 سریال‌ها (/English/Series)", "path": "/English/Series"},
        {"label": "📂 پوشه English (/English)", "path": "/English"},
        {"label": "🏠 پوشه اصلی هاست (/)", "path": "/"},
    ]

    @classmethod
    def get_saved_destinations(cls) -> list[dict[str, str]]:
        ws = Path(cfg.get("paths", "workspace_dir", "./workspace"))
        dest_file = ws / "cuteftp_destinations.json"
        if dest_file.exists():
            try:
                data = json.loads(dest_file.read_text(encoding="utf-8"))
                if isinstance(data, list) and data:
                    return data
            except Exception:
                pass
        return cls.DEFAULT_DESTINATIONS

    @classmethod
    def add_saved_destination(cls, label: str, remote_path: str):
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
            progress_callback(f"شروع آپلود {local_target.name} به مسیر [{dest_label}]...")

        urls = await asyncio.to_thread(
            sync_upload_cuteftp,
            local_target.resolve(),
            remote_subpath,
            progress_callback
        )
        if progress_callback:
            progress_callback("آپلود با موفقیت تکمیل شد ✅")
        return urls