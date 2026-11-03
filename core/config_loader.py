import yaml
from pathlib import Path
from typing import Any


class ConfigManager:
    def __init__(self, config_path: str = "config.yaml"):
        self.config_path = Path(config_path).resolve()
        self._data: dict[str, Any] = {}
        self.reload()

    def reload(self) -> dict[str, Any]:
        """بارگذاری مجدد تنظیمات بدون نیاز به ری‌استارت کردن ربات"""
        with open(self.config_path, "r", encoding="utf-8") as f:
            self._data = yaml.safe_load(f)

        # اطمینان از وجود پوشه‌های کاری در ویندوز
        Path(self._data["paths"]["workspace_dir"]).mkdir(parents=True, exist_ok=True)
        Path(self._data["paths"]["presets_dir"]).mkdir(parents=True, exist_ok=True)
        return self._data

    def get(self, section: str, key: str = None, default: Any = None) -> Any:
        sec = self._data.get(section, {})
        if key is None:
            return sec
        return sec.get(key, default)


cfg = ConfigManager()