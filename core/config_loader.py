import yaml
from pathlib import Path
from typing import Any


class ConfigManager:
    def __init__(self, config_path: str = "config.yaml"):
        self.config_path = Path(config_path).resolve()
        self._data: dict[str, Any] = {}
        self.reload()

    def reload(self) -> dict[str, Any]:
        """بارگذاری مجدد تنظیمات از فایل config.yaml"""
        with open(self.config_path, "r", encoding="utf-8") as f:
            self._data = yaml.safe_load(f) or {}

        paths = self._data.get("paths", {})
        if paths.get("workspace_dir"):
            Path(paths["workspace_dir"]).mkdir(parents=True, exist_ok=True)
        if paths.get("presets_dir"):
            Path(paths["presets_dir"]).mkdir(parents=True, exist_ok=True)
        return self._data

    def save(self):
        """ذخیره دائمی تغییرات اعمال‌شده از طریق دستور /settings در فایل config.yaml"""
        with open(self.config_path, "w", encoding="utf-8") as f:
            yaml.safe_dump(
                self._data,
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
        """تغییر یک مقدار در بخش مشخص و ذخیره خودکار در فایل"""
        if section not in self._data or not isinstance(self._data[section], dict):
            self._data[section] = {}
        self._data[section][key] = value
        self.save()

    @property
    def data(self) -> dict[str, Any]:
        return self._data


cfg = ConfigManager()