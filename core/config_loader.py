import copy
import os
import yaml
from pathlib import Path
from typing import Any


class ConfigManager:
    def __init__(self, config_path: str = "config.yaml"):
        self.config_path = Path(config_path).resolve()
        self._data: dict[str, Any] = {}
        self.reload()

    def _load_dotenv(self) -> None:
        """Load simple KEY=VALUE pairs from the project-local .env without overwriting OS variables."""
        env_path = self.config_path.parent / ".env"
        if not env_path.exists():
            return
        try:
            for raw_line in env_path.read_text(encoding="utf-8").splitlines():
                line = raw_line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                if key:
                    os.environ.setdefault(key, value)
        except OSError:
            pass

    def _apply_environment_overrides(self) -> None:
        def set_value(section: str, key: str, env_name: str, cast=None):
            raw = os.environ.get(env_name)
            if raw is None or raw == "":
                return
            self._data.setdefault(section, {})
            value: Any = raw
            if cast:
                try:
                    value = cast(raw)
                except (TypeError, ValueError):
                    return
            self._data[section][key] = value

        set_value("telegram", "bot_token", "TELEGRAM_BOT_TOKEN")
        admin_ids = os.environ.get("TELEGRAM_ADMIN_IDS")
        if admin_ids:
            try:
                self._data.setdefault("telegram", {})["admin_ids"] = [
                    int(item.strip()) for item in admin_ids.split(",") if item.strip()
                ]
            except ValueError:
                pass

        set_value("qbittorrent", "username", "QBITTORRENT_USERNAME")
        set_value("qbittorrent", "password", "QBITTORRENT_PASSWORD")
        set_value("ftp", "username", "FTP_USERNAME")
        set_value("ftp", "password", "FTP_PASSWORD")

        ftp = self._data.setdefault("ftp", {})
        servers = ftp.get("servers", [])
        if isinstance(servers, list):
            for server in servers:
                if not isinstance(server, dict) or server.get("use_legacy"):
                    continue
                server_id = str(server.get("id", "")).strip().upper()
                prefix = "FTP_" + (server_id if server_id and server_id != "PRIMARY" else "PRIMARY")
                for field in ("host", "username", "password", "public_base_url", "remote_base_dir"):
                    raw = os.environ.get(f"{prefix}_{field.upper()}")
                    if raw:
                        server[field] = raw
                raw_port = os.environ.get(f"{prefix}_PORT")
                if raw_port:
                    try:
                        server["port"] = int(raw_port)
                    except ValueError:
                        pass

    def reload(self) -> dict[str, Any]:
        """Load config.yaml and then overlay private settings from .env / OS environment."""
        self._load_dotenv()
        with open(self.config_path, "r", encoding="utf-8") as f:
            self._data = yaml.safe_load(f) or {}
        self._apply_environment_overrides()

        paths = self._data.get("paths", {})
        if paths.get("workspace_dir"):
            Path(paths["workspace_dir"]).mkdir(parents=True, exist_ok=True)
        if paths.get("presets_dir"):
            Path(paths["presets_dir"]).mkdir(parents=True, exist_ok=True)
        return self._data

    def save(self):
        """Persist non-secret settings only; credentials belong in the ignored .env file."""
        safe_data = copy.deepcopy(self._data)

        def scrub(value):
            if isinstance(value, dict):
                for key in list(value):
                    lowered = str(key).casefold()
                    if lowered in {"bot_token", "password", "username"}:
                        value[key] = ""
                    elif lowered == "admin_ids":
                        value[key] = []
                    else:
                        scrub(value[key])
            elif isinstance(value, list):
                for item in value:
                    scrub(item)

        scrub(safe_data)
        with open(self.config_path, "w", encoding="utf-8") as f:
            yaml.safe_dump(
                safe_data,
                f,
                allow_unicode=True,
                sort_keys=False,
                default_flow_style=False
            )

    def get(self, section: str, key: str = None, default: Any = None) -> Any:
        sec = self._data.get(section, {})
        if key is None:
            return sec
        if isinstance(sec, dict):
            return sec.get(key, default)
        return default

    def set_value(self, section: str, key: str, value: Any):
        """Change a non-secret setting; credentials must be changed in .env."""
        if key.casefold() in {"bot_token", "password", "username", "admin_ids"}:
            raise ValueError("اطلاعات محرمانه را در فایل .env تغییر دهید، نه از طریق config.yaml.")
        if section not in self._data or not isinstance(self._data[section], dict):
            self._data[section] = {}
        self._data[section][key] = value
        self.save()

    @property
    def data(self) -> dict[str, Any]:
        return self._data


cfg = ConfigManager()
