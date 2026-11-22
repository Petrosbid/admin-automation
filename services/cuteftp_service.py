import asyncio
import re
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote, unquote, urlsplit

from core.config_loader import cfg
from services.cuteftp_worker import (
    sync_upload_cuteftp,
    sync_download_episode_files,
    sync_download_movie_files,
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
    def _public_path_from_remote_path(cls, remote_path: str) -> str:
        clean_path = cls._normalize_remote_path(remote_path)
        for prefix in ("/public_html", "/www", "/htdocs"):
            if clean_path.casefold() == prefix or clean_path.casefold().startswith(prefix.casefold() + "/"):
                clean_path = clean_path[len(prefix):] or "/"
                break
        return clean_path if clean_path.startswith("/") else "/" + clean_path

    @classmethod
    def _folder_reference_candidates(cls, reference: str) -> list[tuple[dict, str]]:
        raw = str(reference or "").strip()
        if not raw:
            raise ValueError("لینک یا مسیر پوشه خالی است.")

        servers = cls.get_servers()
        if not servers:
            raise RuntimeError("هیچ سرور FTP معتبری در تنظیمات وجود ندارد.")

        parsed = urlsplit(raw)
        candidates: list[tuple[dict, str]] = []

        def add_candidate(server: dict, remote_path: str):
            decoded = unquote(str(remote_path or "")).replace("\\", "/")
            parts = [part for part in decoded.split("/") if part]
            if not parts or any(part in (".", "..") for part in parts):
                return
            normalized = cls._normalize_remote_path("/" + "/".join(parts))
            low_parts = [part.casefold() for part in parts]
            try:
                english_index = low_parts.index("english")
            except ValueError:
                return
            if english_index + 2 >= len(parts):
                return
            if low_parts[english_index + 1] not in ("movies", "series", "anime"):
                return
            if parts[-1].casefold() in {"index.html", "index.htm", "index.php", "default.htm"}:
                normalized = cls._normalize_remote_path(str(Path(normalized).parent).replace("\\", "/"))
            key = (str(server.get("id") or ""), normalized.casefold())
            if not any((str(s.get("id") or ""), p.casefold()) == key for s, p in candidates):
                candidates.append((server, normalized))

        if parsed.scheme.casefold() in ("ftp", "ftps"):
            try:
                incoming_port = parsed.port or 21
            except ValueError:
                incoming_port = 21
            for server in servers:
                try:
                    server_port = int(server.get("port", 21) or 21)
                except (TypeError, ValueError):
                    server_port = 21
                if (
                    str(server.get("host") or "").casefold() == str(parsed.hostname or "").casefold()
                    and server_port == incoming_port
                ):
                    add_candidate(server, parsed.path)
        elif parsed.scheme.casefold() in ("http", "https"):
            incoming_host = str(parsed.hostname or "").casefold()
            try:
                incoming_port = parsed.port or (443 if parsed.scheme.casefold() == "https" else 80)
            except ValueError:
                incoming_port = 443 if parsed.scheme.casefold() == "https" else 80
            incoming_path = unquote(parsed.path or "/").rstrip("/") or "/"

            for server in servers:
                base_url = str(
                    server.get("base_download_url")
                    or server.get("public_base_url")
                    or ""
                ).strip().rstrip("/")
                if ("dl.example.com" in base_url or not base_url) and str(server.get("username") or "").startswith("@"):
                    base_url = f"https://{str(server['username']).lstrip('@')}"
                if not base_url:
                    continue
                if "://" not in base_url:
                    base_url = "https://" + base_url

                base = urlsplit(base_url)
                try:
                    base_port = base.port or (443 if base.scheme.casefold() == "https" else 80)
                except ValueError:
                    continue
                if str(base.hostname or "").casefold() != incoming_host or base_port != incoming_port:
                    continue

                base_path = unquote(base.path or "").rstrip("/")
                if base_path and incoming_path != base_path and not incoming_path.startswith(base_path + "/"):
                    continue
                public_path = incoming_path[len(base_path):] if base_path else incoming_path
                public_path = public_path or "/"

                remote_root = cls._normalize_remote_path(
                    str(server.get("remote_base_dir") or "/English/Movies")
                )
                public_root = cls._public_path_from_remote_path(remote_root).rstrip("/") or "/"
                if public_root == "/":
                    if not public_path.startswith("/"):
                        continue
                    suffix = public_path
                elif public_path == public_root:
                    suffix = ""
                elif public_path.startswith(public_root + "/"):
                    suffix = public_path[len(public_root):]
                else:
                    continue

                add_candidate(server, remote_root.rstrip("/") + suffix)
        elif not parsed.scheme and raw.startswith("/"):
            for server in servers:
                add_candidate(server, raw)
        else:
            raise ValueError(
                "فرمت ورودی پشتیبانی نمی‌شود. لینک عمومی HTTP(S)، لینک FTP(S) یا مسیر مطلقی مثل "
                "/English/Series/Show/S01 ارسال کنید."
            )

        if not candidates:
            raise ValueError(
                "این لینک با آدرس عمومی یا مسیر ریشه هیچ‌یک از سرورهای تنظیم‌شده تطبیق ندارد. "
                "لینک پوشهٔ قابل دسترس را بفرستید یا مسیر FTP را به شکل /English/Series/... وارد کنید."
            )
        return candidates

    @classmethod
    async def inspect_remote_folder_reference(cls, reference: str) -> list[dict]:
        """Resolve a user-supplied folder URL/path and return only configured servers where it exists."""
        candidates = cls._folder_reference_candidates(reference)

        async def inspect_one(server: dict, remote_path: str) -> dict:
            try:
                result = await asyncio.to_thread(list_ftp_dir_entries, remote_path, server)
            except Exception as exc:
                result = {
                    "files": [],
                    "dirs": [],
                    "exists": False,
                    "checked": False,
                    "error": str(exc),
                }
            item = dict(result)
            item.update({
                "server_id": str(server.get("id") or ""),
                "server_name": str(server.get("name") or server.get("id") or "سرور"),
                "remote_dir": remote_path,
                "public_url": cls.build_public_url(server, remote_path),
            })
            return item

        inspected = await asyncio.gather(
            *(inspect_one(server, remote_path) for server, remote_path in candidates)
        )
        return [item for item in inspected if item.get("exists")]

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
        """بررسی موازی مسیر روی تمام سرورها با حفظ دقیق ترتیب config.yaml در خروجی."""
        servers = cls.get_servers()
        if not servers:
            return []

        async def _inspect_one(server: dict) -> dict:
            target_dir = cls._remote_path_for_server(remote_dir, server)
            try:
                info = await asyncio.to_thread(list_ftp_dir_entries, target_dir, server)
            except Exception as exc:
                info = {"files": [], "dirs": [], "exists": False, "checked": False, "error": str(exc)}
            item = dict(info)
            item.update({
                "server_id": server["id"],
                "server_name": server["name"],
                "remote_dir": target_dir,
                "public_url": cls.build_public_url(server, target_dir),
            })
            return item

        return list(await asyncio.gather(*(_inspect_one(s) for s in servers)))

    @classmethod
    async def find_existing_servers(cls, remote_dir: str) -> list[dict]:
        results = await cls.inspect_remote_dir_all(remote_dir)
        return [
            cls._public_server_info(server, result["remote_dir"])
            for server, result in zip(cls.get_servers(), results)
            if result.get("exists")
        ]

    @classmethod
    async def inspect_remote_dir(cls, remote_dir: str) -> dict[str, Any]:
        """
        سازگاری با فراخوانی‌های قدیمی: فایل‌ها و پوشه‌های تمام سرورهای دارای مقصد
        ادغام می‌شود و ترتیب اولویت در فهرست matches حفظ خواهد شد.
        """
        results = await cls.inspect_remote_dir_all(remote_dir)
        if not results:
            return {"files": [], "dirs": [], "exists": False, "matches": []}

        existing_results = [item for item in results if item.get("exists")]
        matches = [
            {
                "id": item["server_id"],
                "name": item["server_name"],
                "remote_dir": item["remote_dir"],
                "public_url": item.get("public_url", ""),
            }
            for item in existing_results
        ]
        selected = existing_results[0] if existing_results else results[0]
        response = dict(selected)

        def _unique_names(key: str) -> list[str]:
            found: dict[str, str] = {}
            for item in existing_results:
                for raw_name in item.get(key, []) or []:
                    name = str(raw_name).strip()
                    if name and name not in (".", ".."):
                        found.setdefault(name.casefold(), name)
            return sorted(found.values(), key=str.casefold)

        response["dirs"] = _unique_names("dirs")
        response["files"] = _unique_names("files")
        response["exists"] = bool(existing_results)
        response["checked"] = any(item.get("checked", False) for item in results)
        response["matches"] = matches
        response["server_ids"] = [item["server_id"] for item in existing_results]
        response["server_names"] = [item["server_name"] for item in existing_results]
        response["unavailable_servers"] = [
            {
                "id": item["server_id"],
                "name": item["server_name"],
                "error": str(item.get("error") or "اتصال برقرار نشد"),
            }
            for item in results
            if not item.get("checked", False) and not item.get("exists")
        ]
        return response


    @classmethod
    async def download_episode_files(
        cls,
        remote_dir: str,
        local_dir: Path,
        episode_tags: list[str],
        server_id: str,
        progress_callback: Callable[[str], None] | None = None,
    ) -> dict[str, Any]:
        """Download one or more episode tags from a specific FTP server into the matching Workspace season."""
        server = cls.get_server_by_id(server_id)
        if not server:
            raise RuntimeError("سرور انتخاب‌شده دیگر در تنظیمات FTP موجود نیست.")
        target_remote_dir = cls._remote_path_for_server(remote_dir, server)
        return await asyncio.to_thread(
            sync_download_episode_files,
            target_remote_dir,
            Path(local_dir),
            list(episode_tags or []),
            server,
            progress_callback,
        )

    @classmethod
    async def download_movie_files(
        cls,
        remote_dir: str,
        local_dir: Path,
        server_id: str,
        progress_callback: Callable[[str], None] | None = None,
    ) -> dict[str, Any]:
        """Download all raw movie videos from one selected FTP server, excluding SoftSub filenames."""
        server = cls.get_server_by_id(server_id)
        if not server:
            raise RuntimeError("سرور انتخاب‌شده دیگر در تنظیمات FTP موجود نیست.")
        target_remote_dir = cls._remote_path_for_server(remote_dir, server)
        return await asyncio.to_thread(
            sync_download_movie_files,
            target_remote_dir,
            Path(local_dir),
            server,
            progress_callback,
        )

    @classmethod
    async def _resolve_upload_server(cls, remote_subpath: str, server_id: str | None = None) -> dict:
        """
        Resolve one FTP destination without splitting a media automation across drives.

        Exact destination matches always win. When a new quality/SoftSub/HardSub
        folder does not exist yet, walk upward to the nearest existing media
        ancestor (season or series/movie root) and keep that server affinity.
        Only a genuinely new series/movie with no existing ancestor falls back
        to the first configured server.
        """
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

        target_paths = {
            server["id"]: cls._remote_path_for_server(remote_subpath, server)
            for server in servers
        }
        unavailable_by_id: dict[str, dict[str, str]] = {}

        def _remember_check(server: dict, result: dict):
            server_id_value = str(server["id"])
            if result.get("checked", False):
                unavailable_by_id.pop(server_id_value, None)
            else:
                unavailable_by_id[server_id_value] = {
                    "id": server_id_value,
                    "name": str(server.get("name") or server_id_value),
                    "error": str(result.get("error") or "اتصال برقرار نشد"),
                }

        def _selection_choices(matched_pairs: list[tuple[dict, dict]]) -> list[dict]:
            # Choices must target the new folder, not the existing parent folder.
            return [
                cls._public_server_info(server, target_paths[server["id"]])
                for server, _ in matched_pairs
            ]

        def _selection_display_path(matched_pairs: list[tuple[dict, dict]]) -> str:
            if not remote_subpath or remote_subpath == "__CURRENT__":
                return " | ".join(
                    f"{server['name']}: {target_paths[server['id']]}"
                    for server, _ in matched_pairs
                )
            return str(remote_subpath)

        # Prefer an exact existing destination.
        exact_results = await cls.inspect_remote_dir_all(remote_subpath)
        exact_pairs = list(zip(servers, exact_results))
        for server, result in exact_pairs:
            _remember_check(server, result)

        exact_matches = [
            (server, result)
            for server, result in exact_pairs
            if result.get("exists")
        ]
        if len(exact_matches) > 1:
            raise ServerSelectionRequired(
                _selection_display_path(exact_matches),
                _selection_choices(exact_matches),
                unavailable_servers=list(unavailable_by_id.values()),
            )
        if exact_matches:
            return exact_matches[0][0]

        # A newly-created quality folder may not exist on any server yet,
        # while its season/series root already belongs to the backup server.
        ancestor_paths = dict(target_paths)
        while ancestor_paths:
            next_ancestor_paths: dict[str, str] = {}
            for server in servers:
                current_path = ancestor_paths.get(server["id"])
                if not current_path:
                    continue
                parts = [part for part in current_path.split("/") if part]
                is_english_media_tree = (
                    len(parts) >= 2
                    and parts[0].casefold() == "english"
                    and parts[1].casefold() in {"movies", "series", "anime"}
                )
                minimum_parts = 3 if is_english_media_tree else 2
                if len(parts) > minimum_parts:
                    next_ancestor_paths[server["id"]] = "/" + "/".join(parts[:-1])

            if not next_ancestor_paths:
                break

            async def _inspect_ancestor(server: dict) -> dict:
                ancestor = next_ancestor_paths.get(server["id"])
                if not ancestor:
                    return {"exists": False, "checked": True, "remote_dir": ""}
                try:
                    info = await asyncio.to_thread(list_ftp_dir_entries, ancestor, server)
                except Exception as exc:
                    return {
                        "exists": False,
                        "checked": False,
                        "error": str(exc),
                        "remote_dir": ancestor,
                    }
                result = dict(info)
                result["remote_dir"] = ancestor
                return result

            ancestor_results = await asyncio.gather(
                *(_inspect_ancestor(server) for server in servers)
            )
            ancestor_pairs = [
                (server, result)
                for server, result in zip(servers, ancestor_results)
                if server["id"] in next_ancestor_paths
            ]
            for server, result in ancestor_pairs:
                _remember_check(server, result)

            ancestor_matches = [
                (server, result)
                for server, result in ancestor_pairs
                if result.get("exists")
            ]
            if len(ancestor_matches) > 1:
                raise ServerSelectionRequired(
                    _selection_display_path(ancestor_matches),
                    _selection_choices(ancestor_matches),
                    unavailable_servers=list(unavailable_by_id.values()),
                )
            if ancestor_matches:
                selected = ancestor_matches[0][0]
                logging.info(
                    "FTP destination routed to server %s using existing ancestor %s (new target: %s)",
                    selected["name"],
                    ancestor_matches[0][1].get("remote_dir"),
                    target_paths[selected["id"]],
                )
                return selected

            ancestor_paths = next_ancestor_paths

        # Fail closed when not all servers could be checked; do not silently
        # route a new folder to the primary drive while backup may own its parent.
        if unavailable_by_id:
            errors = "; ".join(
                f"{item['name']}: {item['error'][:120]}"
                for item in unavailable_by_id.values()
            )
            raise RuntimeError(
                "هیچ مقصد یا پوشه والد قابل‌شناسایی پیدا نشد و بررسی همه سرورها کامل نبود؛ "
                "برای جلوگیری از آپلود روی درایو اشتباه، عملیات متوقف شد. "
                + errors
            )

        # All servers were reachable and no matching media root exists.
        return servers[0]

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