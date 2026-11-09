import asyncio
import re
from pathlib import Path
from typing import Callable
from urllib.parse import quote

from core.config_loader import cfg
from services.cuteftp_worker import (
    sync_upload_cuteftp,
    get_current_cuteftp_remote_dir,
    list_ftp_dir_entries,
)


_PLACEHOLDER_PASSWORDS = {
    "ftppassword",
    "your_ftp_password",
    "change_me",
    "changeme",
    "replace_me",
    "your_password",
}


class ServerSelectionRequired(RuntimeError):
    """Raised by an upload task when the destination exists on multiple FTP servers."""

    requires_server_selection = True

    def __init__(
        self,
        remote_dir: str,
        servers: list[dict],
        unavailable_servers: list[dict] | None = None,
    ):
        self.remote_dir = str(remote_dir or "/")
        self.unavailable_servers = list(unavailable_servers or [])
        self.servers = [
            {
                "id": str(item.get("id", "")),
                "name": str(item.get("name", item.get("id", "Server"))),
                "public_url": str(item.get("public_url", "")),
                "remote_dir": str(item.get("remote_dir", remote_dir)),
            }
            for item in servers
        ]
        names = "، ".join(item["name"] for item in self.servers)
        super().__init__(
            f"پوشه {self.remote_dir} روی چند سرور وجود دارد و انتخاب مقصد لازم است: {names}"
        )


class CuteFTPService:
    DEFAULT_DESTINATIONS = [
        {"label": "🎬 فیلم‌های خارجی (/English/Movies)", "path": "/English/Movies"},
        {"label": "📺 سریال‌های خارجی (/English/Series)", "path": "/English/Series"},
        {"label": "🎌 انیمه‌ها (/English/Anime)", "path": "/English/Anime"},
    ]

    @staticmethod
    def _merge_legacy_config() -> dict:
        """پشتیبانی از تنظیمات قدیمی ftp و cuteftp؛ مقادیر معتبر cuteftp اولویت دارند."""
        legacy_cfg = cfg.get("ftp") or {}
        current_cfg = cfg.get("cuteftp") or {}
        merged = dict(legacy_cfg) if isinstance(legacy_cfg, dict) else {}

        if isinstance(current_cfg, dict):
            for key, value in current_cfg.items():
                if value is None or not str(value).strip():
                    continue
                if key.lower() == "password" and str(value).strip().casefold() in _PLACEHOLDER_PASSWORDS:
                    continue
                merged[key] = value

        if not merged.get("remote_base_dir"):
            merged["remote_base_dir"] = merged.get("root_remote_dir", "/English/Movies")
        merged.setdefault("port", 21)
        merged.setdefault("protocol", "FTP")
        return merged

    @classmethod
    def _normalize_server(cls, raw: dict, index: int) -> dict | None:
        if not isinstance(raw, dict) or raw.get("enabled", True) is False:
            return None

        server = dict(raw)
        server_id = re.sub(r"[^a-zA-Z0-9_-]", "", str(server.get("id") or f"server{index + 1}"))[:24]
        if not server_id:
            server_id = f"server{index + 1}"

        host = str(server.get("host") or "").strip()
        username = str(server.get("username") or "").strip()
        password = str(server.get("password") or "").strip()
        if (
            not host
            or not username
            or not password
            or password.casefold() in _PLACEHOLDER_PASSWORDS
        ):
            return None

        try:
            port = int(server.get("port", 21) or 21)
        except (TypeError, ValueError):
            port = 21

        base_url = str(
            server.get("public_base_url")
            or server.get("base_download_url")
            or server.get("public_url")
            or ""
        ).strip().rstrip("/")
        remote_base = str(
            server.get("remote_base_dir")
            or server.get("root_remote_dir")
            or "/English/Movies"
        ).strip()

        try:
            timeout = max(5, int(server.get("timeout", 120) or 120))
        except (TypeError, ValueError):
            timeout = 120

        server.update({
            "id": server_id,
            "name": str(server.get("name") or f"سرور {index + 1}").strip(),
            "host": host,
            "port": port,
            "username": username,
            "password": password,
            "protocol": str(server.get("protocol") or "FTP").strip().upper(),
            "timeout": timeout,
            "passive_mode": bool(server.get("passive_mode", True)),
            "remote_base_dir": cls._normalize_remote_path(remote_base),
            "base_download_url": base_url,
        })
        return server

    @staticmethod
    def _normalize_remote_path(remote_dir: str) -> str:
        clean = "/" + str(remote_dir or "/").replace("\\", "/").strip().lstrip("/")
        return clean.rstrip("/") if len(clean) > 1 else "/"

    @classmethod
    def get_servers(cls) -> list[dict]:
        """
        ترتیب آرایه ftp.servers ترتیب واقعی بررسی سرورهاست.
        برای هر عضو می‌توان use_legacy: true گذاشت تا اطلاعات سرور اول از تنظیمات
        قدیمی ftp/cuteftp خوانده و فقط مقادیر لازم override شوند.
        """
        ftp_section = cfg.get("ftp") or {}
        configured = ftp_section.get("servers") if isinstance(ftp_section, dict) else None
        result: list[dict] = []

        if isinstance(configured, list) and configured:
            legacy = cls._merge_legacy_config()
            used_ids: set[str] = set()
            for index, item in enumerate(configured):
                if not isinstance(item, dict):
                    continue
                if item.get("use_legacy"):
                    merged = dict(legacy)
                    merged.update({
                        key: value for key, value in item.items()
                        if key not in ("use_legacy",) and value is not None
                    })
                else:
                    merged = dict(item)

                normalized = cls._normalize_server(merged, index)
                if not normalized or normalized["id"] in used_ids:
                    continue
                used_ids.add(normalized["id"])
                result.append(normalized)
            return result

        # رفتار قبلی بدون نیاز به تغییر config کاربران حفظ می‌شود.
        legacy_server = cls._normalize_server(cls._merge_legacy_config(), 0)
        return [legacy_server] if legacy_server else []

    @classmethod
    def get_server_by_id(cls, server_id: str) -> dict | None:
        return next((s for s in cls.get_servers() if s["id"] == str(server_id)), None)

    @staticmethod
    def _remote_path_for_server(remote_subpath: str, server: dict) -> str:
        if not remote_subpath or remote_subpath == "__CURRENT__":
            return CuteFTPService._normalize_remote_path(
                str(server.get("remote_base_dir") or "/English/Movies")
            )
        return CuteFTPService._normalize_remote_path(remote_subpath)

    @classmethod
    def build_public_url(cls, server: dict, remote_path: str) -> str:
        base_url = str(
            server.get("base_download_url")
            or server.get("public_base_url")
            or ""
        ).strip().rstrip("/")
        if not base_url:
            return ""

        clean_path = cls._normalize_remote_path(remote_path)
        for prefix in ("/public_html", "/www", "/htdocs"):
            if clean_path.startswith(prefix):
                clean_path = clean_path[len(prefix):]
        return f"{base_url}/{quote(clean_path.strip('/'), safe='/')}"

    @classmethod
    def _public_server_info(cls, server: dict, remote_path: str) -> dict:
        return {
            "id": server["id"],
            "name": server["name"],
            "remote_dir": remote_path,
            "public_url": cls.build_public_url(server, remote_path),
        }

    @classmethod
    def get_saved_destinations(cls) -> list[dict[str, str]]:
        saved = cfg.get("cuteftp", "saved_destinations", None)
        if isinstance(saved, list) and saved:
            return saved
        return list(cls.DEFAULT_DESTINATIONS)

    @classmethod
    def add_saved_destination(cls, label: str, remote_path: str):
        clean_path = cls._normalize_remote_path(remote_path)
        current = cls.get_saved_destinations()
        if not any(d.get("path") == clean_path for d in current):
            current.append({"label": f"📂 {label} ({clean_path})", "path": clean_path})
            cfg.set_value("cuteftp", "saved_destinations", current)

    @staticmethod
    async def get_active_gui_remote_path() -> str:
        return await asyncio.to_thread(get_current_cuteftp_remote_dir)

    @classmethod
    async def inspect_remote_dir_all(cls, remote_dir: str) -> list[dict]:
        """بررسی ترتیبی مسیر روی تمام سرورها؛ ترتیب خروجی مطابق ترتیب config.yaml است."""
        servers = cls.get_servers()
        results: list[dict] = []
        for server in servers:
            target_dir = cls._remote_path_for_server(remote_dir, server)
            try:
                info = await asyncio.to_thread(list_ftp_dir_entries, target_dir, server)
            except Exception as exc:
                info = {"files": [], "dirs": [], "exists": False, "error": str(exc)}
            item = dict(info)
            item.update({
                "server_id": server["id"],
                "server_name": server["name"],
                "remote_dir": target_dir,
                "public_url": cls.build_public_url(server, target_dir),
            })
            results.append(item)
        return results

    @classmethod
    async def find_existing_servers(cls, remote_dir: str) -> list[dict]:
        results = await cls.inspect_remote_dir_all(remote_dir)
        return [
            cls._public_server_info(server, result["remote_dir"])
            for server, result in zip(cls.get_servers(), results)
            if result.get("exists")
        ]

    @classmethod
    async def inspect_remote_dir(cls, remote_dir: str) -> dict[str, list[str]]:
        """
        سازگاری با فراخوانی‌های قدیمی: اولین سروری که پوشه را دارد برگردانده می‌شود.
        اطلاعات تمام سرورهای دارای پوشه نیز در کلید matches قرار می‌گیرد.
        """
        results = await cls.inspect_remote_dir_all(remote_dir)
        if not results:
            return {"files": [], "dirs": [], "exists": False, "matches": []}

        matches = [
            {
                "id": item["server_id"],
                "name": item["server_name"],
                "remote_dir": item["remote_dir"],
                "public_url": item.get("public_url", ""),
            }
            for item in results if item.get("exists")
        ]
        selected = next((item for item in results if item.get("exists")), results[0])
        response = dict(selected)
        response["matches"] = matches
        return response

    @classmethod
    async def _resolve_upload_server(cls, remote_subpath: str, server_id: str | None = None) -> dict:
        servers = cls.get_servers()
        if not servers:
            raise RuntimeError(
                "هیچ سرور FTP معتبری تنظیم نشده است. لطفاً بخش ftp در config.yaml را بررسی کنید."
            )

        if server_id:
            selected = next((s for s in servers if s["id"] == str(server_id)), None)
            if not selected:
                raise RuntimeError("سرور انتخاب‌شده دیگر در config.yaml وجود ندارد.")
            return selected

        results = await cls.inspect_remote_dir_all(remote_subpath)
        pairs = list(zip(servers, results))
        matches = [
            (server, result)
            for server, result in pairs
            if result.get("exists")
        ]
        unavailable = [
            {
                "id": server["id"],
                "name": server["name"],
                "error": str(result.get("error") or "اتصال برقرار نشد"),
            }
            for server, result in pairs
            if not result.get("checked", False) and not result.get("exists")
        ]
        if len(matches) > 1:
            choices = [
                cls._public_server_info(server, result["remote_dir"])
                for server, result in matches
            ]
            display_path = remote_subpath
            if not display_path or display_path == "__CURRENT__":
                display_path = " | ".join(
                    f"{result['server_name']}: {result['remote_dir']}"
                    for _, result in matches
                )
            raise ServerSelectionRequired(display_path, choices, unavailable_servers=unavailable)
        if matches:
            return matches[0][0]

        # وقتی هیچ پوشه‌ای پیدا نشده، فقط روی اولین سروری ایجاد می‌کنیم که واقعاً بررسی شده است.
        reachable = [
            server for server, result in pairs
            if result.get("checked", False)
        ]
        if reachable:
            return reachable[0]

        # اگر هیچ سروری بررسی نشده باشد، مقصد را حدس نمی‌زنیم تا فایل در سرور اشتباه آپلود نشود.
        errors = "; ".join(
            f"{item['name']}: {item['error'][:120]}" for item in unavailable
        )
        raise RuntimeError(
            "هیچ‌یک از سرورهای FTP قابل بررسی نبودند؛ برای جلوگیری از آپلود اشتباه عملیات متوقف شد. "
            + errors
        )

    @classmethod
    async def upload_path(
        cls,
        local_target: Path,
        remote_subpath: str = "__CURRENT__",
        progress_callback: Callable[[str], None] | None = None,
        server_id: str | None = None,
    ) -> list[str]:
        selected_server = await cls._resolve_upload_server(remote_subpath, server_id)
        return await asyncio.to_thread(
            sync_upload_cuteftp,
            Path(local_target),
            remote_subpath,
            progress_callback,
            selected_server,
        )

    @classmethod
    async def upload_multiple_or_single(
        cls,
        targets: list[Path] | Path,
        remote_subpath: str = "__CURRENT__",
        progress_callback: Callable[[str], None] | None = None,
        server_id: str | None = None,
    ) -> list[str]:
        if isinstance(targets, (str, Path)):
            targets_list = [Path(targets)]
        else:
            targets_list = [Path(t) for t in targets]

        # سرور یک بار برای کل عملیات انتخاب می‌شود تا فایل‌های یک batch در چند سرور پخش نشوند.
        selected_server = await cls._resolve_upload_server(remote_subpath, server_id)
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
                progress_callback=_sub_prog,
                server_id=selected_server["id"],
            )
            all_links.extend(links)

        return all_links
