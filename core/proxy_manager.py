import logging
from urllib.parse import urlparse, parse_qs
from aiohttp_socks import ProxyConnector
from core.config_loader import cfg


class ProxyManager:
    @staticmethod
    def is_enabled() -> bool:
        return bool(cfg.get("proxy", "enabled", False) and cfg.get("proxy", "url"))

    @staticmethod
    def get_proxy_url() -> str | None:
        """استانداردسازی آدرس پروکسی و تشخیص لینک‌های تلگرام"""
        if not ProxyManager.is_enabled():
            return None

        raw_url = str(cfg.get("proxy", "url", "")).strip()
        if not raw_url:
            return None

        # ۱. بررسی لینک‌های تلگرام (t.me/proxy یا t.me/socks)
        if "t.me/" in raw_url or "telegram.me/" in raw_url:
            parsed = urlparse(raw_url)
            qs = parse_qs(parsed.query)

            # اگر لینک MTProto باشد (دارای secret)
            if "proxy" in parsed.path or "secret" in qs:
                logging.error(
                    "❌ خطای تنظیمات پروکسی: لینک وارد شده در config.yaml یک پروکسی MTProto تلگرام است (دارای secret). "
                    "پروکسی MTProto برای ربات و وب کار نمی‌کند! لطفاً از پروکسی SOCKS5 یا HTTP استفاده کنید "
                    "(مثلاً socks5://127.0.0.1:10808 در v2rayN)."
                )
                return None

            # اگر لینک SOCKS5 تلگرام باشد (t.me/socks?server=...&port=...)
            if "socks" in parsed.path and "server" in qs and "port" in qs:
                server = qs["server"][0]
                port = qs["port"][0]
                user = qs.get("user", [None])[0]
                password = qs.get("pass", [None])[0]
                if user and password:
                    return f"socks5://{user}:{password}@{server}:{port}"
                return f"socks5://{server}:{port}"

        # ۲. اصلاح پیشوندهای نامعتبر در پروکسی‌های عادی
        if raw_url.lower().startswith("https://"):
            raw_url = "http://" + raw_url[8:]
        elif raw_url.lower().startswith("socks5h://"):
            raw_url = "socks5://" + raw_url[10:]
        elif "://" not in raw_url:
            raw_url = f"socks5://{raw_url}"

        # ۳. اطمینان از وجود پورت معتبر در آدرس نهایی
        parsed_final = urlparse(raw_url)
        if not parsed_final.hostname or not parsed_final.port:
            logging.error(
                f"❌ آدرس پروکسی نامعتبر است ({raw_url}). فرمت صحیح: socks5://127.0.0.1:10808 یا http://ip:port"
            )
            return None

        return raw_url

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