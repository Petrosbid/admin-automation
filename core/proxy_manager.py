from aiohttp_socks import ProxyConnector
from core.config_loader import cfg


class ProxyManager:
    @staticmethod
    def is_enabled() -> bool:
        return bool(cfg.get("proxy", "enabled", False) and cfg.get("proxy", "url"))

    @staticmethod
    def get_proxy_url() -> str | None:
        if ProxyManager.is_enabled():
            return cfg.get("proxy", "url")
        return None

    @staticmethod
    def get_aiohttp_connector() -> ProxyConnector | None:
        """کانکتور پروکسی برای aiohttp و ربات تلگرام"""
        proxy_url = ProxyManager.get_proxy_url()
        if proxy_url:
            return ProxyConnector.from_url(proxy_url)
        return None

    @staticmethod
    def get_requests_proxies() -> dict[str, str] | None:
        """دیکشنری پروکسی برای cloudscraper و gdown"""
        proxy_url = ProxyManager.get_proxy_url()
        if proxy_url:
            return {"http": proxy_url, "https": proxy_url}
        return None