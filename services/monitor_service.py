import asyncio
from datetime import datetime
import html
import importlib.util
import json
import logging
import os
import re
import time
import webbrowser
from pathlib import Path
from urllib.parse import urljoin, urlparse, urlunparse, parse_qsl, urlencode
from bs4 import BeautifulSoup, FeatureNotFound
import cloudscraper
from aiogram import Bot
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder

from core.config_loader import cfg
from core.proxy_manager import ProxyManager


def _make_soup(html: str) -> BeautifulSoup:
    try:
        return BeautifulSoup(html, "lxml")
    except FeatureNotFound:
        return BeautifulSoup(html, "html.parser")


def _add_cache_buster(url: str) -> str:
    """افزودن پارامتر زمان به URL برای دور زدن کش LiteSpeed و Cloudflare در هر ۵ دقیقه"""
    parsed = urlparse(url)
    query = dict(parse_qsl(parsed.query))
    query["_cb"] = str(int(time.time()))
    return urlunparse(parsed._replace(query=urlencode(query)))


class SiteMonitorService:
    DEFAULT_SITES = {
        "pahe": {"title": "Pahe", "url": "https://pahe.ink/", "enabled": True},
        "psa": {"title": "PSA", "url": "https://psa.wf/", "enabled": True},
        "yts": {"title": "YTS", "url": "https://yts.gg/home", "enabled": True},
        "digimovie": {"title": "Digimoviez", "url": "https://digimoviez53.top/", "enabled": True},
        "avamovie": {"title": "AvaMovie", "url": "https://avasaraw.top/", "enabled": True},
    }

    def __init__(self):
        self._task: asyncio.Task | None = None
        self._running: bool = False
        self._pw = None
        self._browser_context = None
        self._active_profile_dir: Path | None = None
        self._pages: dict[str, object] = {}
        self._browser_lock = asyncio.Lock()
        self._check_lock = asyncio.Lock()
        self._pw_installed = importlib.util.find_spec("playwright") is not None
        self._browser_start_error: str = ""
        self._browser_warmup_task: asyncio.Task | None = None
        self._last_error_notified: dict[str, float] = {}

    @classmethod
    def get_all_sites_config(cls) -> dict[str, dict]:
        """
        خواندن پویا و لحظه‌ای لیست سایت‌ها، لینک فعلی و وضعیت فعال/غیرفعال بودن هر کدام از config.yaml
        (پشتیبانی هم از فرمت دیکشنری جدید و هم فرمت رشته‌ای قدیمی)
        """
        raw_sites = cfg.get("monitor", "sites") or {}
        sites_Status = {}

        for key, def_info in cls.DEFAULT_SITES.items():
            val = raw_sites.get(key)
            if isinstance(val, dict):
                url = str(val.get("url") or def_info["url"]).strip()
                enabled = bool(val.get("enabled", True))
            elif isinstance(val, str) and val.strip():
                url = val.strip()
                enabled = True
            else:
                url = def_info["url"]
                enabled = def_info["enabled"]

            sites_Status[key] = {
                "title": def_info["title"],
                "url": url,
                "enabled": enabled,
            }
        return sites_Status

    @classmethod
    def get_site_url(cls, site_key: str) -> str:
        all_cfg = cls.get_all_sites_config()
        return all_cfg.get(site_key, {}).get("url", cls.DEFAULT_SITES.get(site_key, {}).get("url", ""))

    @classmethod
    def is_site_enabled(cls, site_key: str) -> bool:
        all_cfg = cls.get_all_sites_config()
        return bool(all_cfg.get(site_key, {}).get("enabled", True))

    @staticmethod
    def _page_navigation_timeout_ms() -> int:
        try:
            configured = int(cfg.get("monitor", "page_load_timeout_ms", 90000) or 90000)
        except (TypeError, ValueError):
            configured = 90000
        return max(30000, min(configured, 240000))

    @staticmethod
    def _page_retry_delay_seconds() -> float:
        try:
            configured = float(cfg.get("monitor", "page_load_retry_delay_seconds", 3) or 3)
        except (TypeError, ValueError):
            configured = 3.0
        return max(1.0, min(configured, 15.0))

    async def _navigate_page_with_retry(self, page, url: str, site_key: str):
        """Navigate with a generous timeout and retry once via reload on slow/failed loads."""
        timeout_ms = self._page_navigation_timeout_ms()
        retry_delay = self._page_retry_delay_seconds()
        last_error = None
        response = None

        for attempt in range(2):
            try:
                if attempt == 0 or not page.url or page.url == "about:blank":
                    response = await page.goto(
                        url,
                        wait_until="domcontentloaded",
                        timeout=timeout_ms,
                    )
                else:
                    response = await page.reload(
                        wait_until="domcontentloaded",
                        timeout=timeout_ms,
                    )
                return response, None
            except Exception as exc:
                last_error = exc
                logging.warning(
                    "Monitor page load for %s failed on attempt %s/2 (timeout=%sms): %s",
                    site_key,
                    attempt + 1,
                    timeout_ms,
                    exc,
                )
                if attempt == 0:
                    await asyncio.sleep(retry_delay)

        return response, last_error

    @classmethod
    def set_site_url(cls, site_key: str, new_url: str):
        """تغییر آدرس یک سایت و ذخیره دائمی در config.yaml"""
        clean_url = new_url.strip()
        if not clean_url.startswith(("http://", "https://")):
            clean_url = "https://" + clean_url

        all_cfg = cls.get_all_sites_config()
        if site_key in all_cfg:
            all_cfg[site_key]["url"] = clean_url

        serializable = {
            k: {"enabled": v["enabled"], "url": v["url"]}
            for k, v in all_cfg.items()
        }
        cfg.set_value("monitor", "sites", serializable)

    @classmethod
    def toggle_site_enabled(cls, site_key: str) -> bool:
        """فعال یا غیرفعال کردن یک سایت خاص و ذخیره در config.yaml"""
        all_cfg = cls.get_all_sites_config()
        new_state = not all_cfg.get(site_key, {}).get("enabled", True)
        if site_key in all_cfg:
            all_cfg[site_key]["enabled"] = new_state

        serializable = {
            k: {"enabled": v["enabled"], "url": v["url"]}
            for k, v in all_cfg.items()
        }
        cfg.set_value("monitor", "sites", serializable)
        return new_state

    @property
    def state_file(self) -> Path:
        ws = Path(cfg.get("paths", "workspace_dir", "./workspace")).resolve()
        ws.mkdir(parents=True, exist_ok=True)
        return ws / "monitor_state.json"

    @property
    def profile_dir(self) -> Path:
        ws = Path(cfg.get("paths", "workspace_dir", "./workspace")).resolve()
        # Each bot process gets its own persistent profile. Chromium locks a user-data
        # directory; sharing one fixed path with another running bot instance causes
        # "Opening in existing browser session" and leaves the monitor without pages.
        p_dir = ws / f"browser_profile_playwright_{os.getpid()}"
        p_dir.mkdir(parents=True, exist_ok=True)
        return p_dir

    def _load_state(self) -> dict[str, list[str]]:
        default_state = {k: [] for k in self.DEFAULT_SITES}
        if self.state_file.exists():
            try:
                raw = self.state_file.read_text(encoding="utf-8")
                data = json.loads(raw)
                if isinstance(data, dict):
                    for k in self.DEFAULT_SITES:
                        if isinstance(data.get(k), list):
                            default_state[k] = data[k]
            except Exception as e:
                logging.error(f"Error reading monitor_state.json: {e}")
        return default_state

    def _save_state(self, state: dict[str, list[str]]):
        try:
            trimmed = {k: list(dict.fromkeys(v))[-300:] for k, v in state.items()}
            temp_file = self.state_file.with_suffix(".tmp")
            temp_file.write_text(
                json.dumps(trimmed, ensure_ascii=False, indent=2),
                encoding="utf-8"
            )
            temp_file.replace(self.state_file)
        except Exception as e:
            logging.error(f"Failed to save monitor_state.json: {e}")

    async def send_error_notification(self, bot: Bot | None, site_key: str, error_text: str, force: bool = False):
        if not bot:
            return
        now = time.time()
        last_sent = self._last_error_notified.get(site_key, 0.0)
        if not force and (now - last_sent < 1800):
            return
        self._last_error_notified[site_key] = now

        url = self.get_site_url(site_key)
        msg = (
            f"⚠️ **خطا در مانیتورینگ سایت `{site_key.upper()}`**\n"
            f"🌐 آدرس فعلی: `{url}`\n"
            f"❌ علت خطا:\n`{str(error_text)[:350]}`\n\n"
            "💡 اگر آدرس سایت تغییر کرده است، از بخش `/settings` ⬅️ **تنظیمات مانیتورینگ** لینک جدید را وارد کنید."
        )
        for admin_id in cfg.get("telegram", "admin_ids", []):
            try:
                await bot.send_message(chat_id=admin_id, text=msg, parse_mode="Markdown")
            except Exception as e:
                logging.error(f"Failed to send monitor error alert to {admin_id}: {e}")

    async def ensure_real_browser_open(self) -> bool:
        """Start the Playwright-managed headed Chromium browser, never silently degrade."""
        try:
            from playwright.async_api import async_playwright
        except Exception as exc:
            self._pw_installed = False
            self._browser_start_error = (
                f"کتابخانه Playwright در همین Python فعال نیست: {type(exc).__name__}: {exc}"
            )
            logging.exception("Playwright Python package could not be imported")
            return False

        self._pw_installed = True
        async with self._browser_lock:
            if self._browser_context is not None:
                try:
                    _ = self._browser_context.pages
                    return True
                except Exception:
                    logging.warning("Previous Playwright browser context is no longer usable.")
                    self._browser_context = None
                    self._pages.clear()

            try:
                self._pw = await async_playwright().start()
                launch_kwargs = {
                    "user_data_dir": str(self.profile_dir),
                    "headless": False,
                    "no_viewport": True,
                    "ignore_https_errors": True,
                    "args": [
                        "--disable-blink-features=AutomationControlled",
                        "--start-maximized",
                        "--disable-http-cache",
                        "--no-first-run",
                        "--no-default-browser-check",
                    ],
                }

                # Chromium uses Windows/system proxy by default. Only force the bot's SOCKS
                # proxy when the explicit monitor.browser_use_proxy setting is enabled.
                use_proxy = bool(cfg.get("monitor", "browser_use_proxy", False))
                if use_proxy:
                    proxy_url = ProxyManager.get_proxy_url()
                    if proxy_url:
                        launch_kwargs["proxy"] = {"server": proxy_url}
                        logging.info("Playwright Chromium will use configured proxy: %s", proxy_url)
                    else:
                        logging.warning(
                            "monitor.browser_use_proxy is true, but no valid app proxy is configured."
                        )
                else:
                    logging.info(
                        "Playwright Chromium will use Windows/system proxy settings (browser_use_proxy=false)."
                    )

                # Use the bundled Chromium installed via the Playwright CLI.
                # No channel='chrome' fallback: it used to hide the bundled-browser launch error.
                self._active_profile_dir = Path(launch_kwargs["user_data_dir"])
                self._browser_context = await self._pw.chromium.launch_persistent_context(
                    **launch_kwargs
                )
                self._pages = {}

                # A visible page forces the persistent headed browser window to appear.
                control_page = await self._browser_context.new_page()
                self._pages["__control__"] = control_page
                await control_page.goto("about:blank")

                self._browser_start_error = ""
                logging.info(
                    "Playwright headed Chromium started. Profile=%s Executable=%s",
                    self._active_profile_dir,
                    self._pw.chromium.executable_path,
                )
                return True

            except Exception as exc:
                self._browser_start_error = f"{type(exc).__name__}: {exc}"
                logging.exception(
                    "Could not launch Playwright Chromium. Profile=%s",
                    self.profile_dir,
                )
                self._browser_context = None
                self._active_profile_dir = None
                self._pages.clear()
                if self._pw is not None:
                    try:
                        await self._pw.stop()
                    except Exception:
                        logging.debug("Could not stop failed Playwright driver cleanly.", exc_info=True)
                self._pw = None
                return False

    async def open_captcha_tabs_for_admin(self) -> str:
        """Open all enabled monitor sites in visible Playwright Chromium tabs."""
        sites_cfg = self.get_all_sites_config()
        active_sites = {key: value for key, value in sites_cfg.items() if value["enabled"]}

        ok = await self.ensure_real_browser_open()
        if not ok or self._browser_context is None:
            detail = self._browser_start_error or "خطای نامشخص در راه‌اندازی مرورگر"
            return (
                "مرورگر Chromium متعلق به Playwright اجرا نشد.\n\n"
                f"علت: {detail}\n\n"
                "در PowerShell و در پوشه پروژه این دستورات را اجرا کنید:\n"
                "cd C:\\MediaBot\\bot\n"
                ".\\.venv\\Scripts\\python.exe -m pip install --upgrade playwright\n"
                ".\\.venv\\Scripts\\python.exe -m playwright install chromium\n"
                ".\\.venv\\Scripts\\python.exe -c \"from playwright.sync_api import sync_playwright; p=sync_playwright().start(); b=p.chromium.launch(headless=False); print(b.version); b.close(); p.stop()\"\n\n"
                "این بار به‌جای مرورگر معمولی ویندوز، خطای واقعی اجرای Chromium گزارش شده است."
            )

        opened: list[str] = []
        failed: list[str] = []
        for key, info in active_sites.items():
            try:
                page = self._pages.get(key)
                if page is None or page.is_closed():
                    # Reuse the initial control tab for the first site so the browser
                    # does not remain showing an empty about:blank tab.
                    control_page = self._pages.pop("__control__", None)
                    if control_page is not None and not control_page.is_closed():
                        page = control_page
                    else:
                        page = await self._browser_context.new_page()
                    self._pages[key] = page

                _, navigation_error = await self._navigate_page_with_retry(
                    page,
                    info["url"],
                    key,
                )
                if navigation_error:
                    # Keep a usable partially loaded document open after the retry.
                    try:
                        page_html = await page.content()
                    except Exception:
                        page_html = ""
                    if not page_html.strip() or page.url == "about:blank":
                        raise navigation_error
                    logging.warning(
                        "Playwright navigation for %s remained slow after retry; keeping the loaded tab open.",
                        key,
                    )

                await page.bring_to_front()
                opened.append(key.upper())
            except Exception as exc:
                logging.exception("Could not open Playwright tab for %s", key)
                failed.append(f"{key.upper()}: {type(exc).__name__}: {str(exc)[:180]}")

        result = (
            "پنجره Chromium با Playwright باز شد.\n"
            f"تب‌های بازشده: {', '.join(opened) if opened else 'هیچ‌کدام'}"
        )
        if failed:
            result += "\n\nخطا در باز کردن بعضی تب‌ها:\n" + "\n".join(failed)
        result += (
            "\n\nاگر CAPTCHA مشاهده می‌کنی، آن را در همین پنجره حل کن. "
            "کوکی‌ها در پروفایل اختصاصی Playwright ذخیره می‌شوند."
        )
        return result

    @staticmethod
    def _captcha_detection_reason(html: str) -> str | None:
        """
        Return a reason only for a real challenge document.
        Do not treat generic Cloudflare scripts/resources or HTTP 403 pages as CAPTCHA.
        """
        lower = (html or "").lower()
        soup = _make_soup(html or "")
        title = (soup.title.get_text(" ", strip=True).lower() if soup.title else "")
        body_text = re.sub(r"\s+", " ", soup.get_text(" ", strip=True).lower())

        # Strong Cloudflare interstitial signals; generic "cf-chl-" resource names
        # are deliberately excluded because normal pages may load challenge scripts.
        if title.startswith("just a moment"):
            return "Cloudflare interstitial title"
        if any(marker in lower for marker in (
            'id="challenge-form"',
            "id='challenge-form'",
            "cf-chl-widget",
            "__cf_chl_tk",
            "cf_chl_opt",
        )):
            return "Cloudflare challenge form/token"
        if (
            ("verify you are human" in body_text or "checking your browser" in body_text
             or "performing security verification" in body_text)
            and ("cloudflare" in lower or "challenge" in lower or "security" in body_text)
        ):
            return "visible browser verification text"

        # hCaptcha must have an actual challenge/verification UI, not merely a script include.
        has_hcaptcha_frame = bool(soup.select(
            'iframe[src*="hcaptcha.com"], iframe[title*="hCaptcha" i], '
            '[data-hcaptcha], .h-captcha'
        ))
        if has_hcaptcha_frame and any(
            phrase in body_text for phrase in ("verify", "select", "challenge", "captcha")
        ):
            return "hCaptcha challenge widget"

        # Generic CAPTCHA wording is accepted only when paired with a visible form/widget.
        has_captcha_widget = bool(soup.select(
            'iframe[src*="captcha"], [class*="captcha" i], [id*="captcha" i], '
            'input[name*="captcha" i]'
        ))
        if has_captcha_widget and any(
            phrase in body_text for phrase in ("verify", "captcha", "security check")
        ):
            return "CAPTCHA widget in document"
        return None

    @staticmethod
    def _is_captcha_page(html: str) -> bool:
        return SiteMonitorService._captcha_detection_reason(html) is not None

    async def _wait_for_manual_captcha(
        self,
        page,
        site_key: str,
        bot: Bot | None = None,
    ) -> str:
        try:
            wait_seconds = int(cfg.get("monitor", "captcha_wait_seconds", 300) or 300)
        except (TypeError, ValueError):
            wait_seconds = 300
        wait_seconds = max(60, min(wait_seconds, 1800))

        try:
            await page.bring_to_front()
        except Exception:
            pass

        notice = (
            f"🔐 <b>کپچا در مانیتورینگ {site_key.upper()} شناسایی شد.</b>\n"
            f"مرورگر Playwright باز و فعال است. کپچا را روی دسکتاپ ویندوز سرور حل کنید.\n"
            f"ربات تا <b>{wait_seconds // 60} دقیقه</b> منتظر می‌ماند و سپس صفحه را می‌خواند."
        )
        if bot:
            for admin_id in cfg.get("telegram", "admin_ids", []) or []:
                try:
                    await bot.send_message(
                        chat_id=int(admin_id),
                        text=notice,
                        parse_mode="HTML",
                    )
                except Exception:
                    logging.exception("Could not send CAPTCHA notice to admin %s", admin_id)

        logging.warning(
            "CAPTCHA detected for %s; waiting up to %s seconds for manual completion in Playwright.",
            site_key,
            wait_seconds,
        )
        deadline = asyncio.get_running_loop().time() + wait_seconds
        while asyncio.get_running_loop().time() < deadline:
            if page.is_closed():
                raise RuntimeError(
                    f"تب مرورگر {site_key} بسته شد؛ از پنل مانیتورینگ تب CAPTCHA را دوباره باز کنید."
                )
            await asyncio.sleep(3)
            html = await page.content()
            if not self._is_captcha_page(html):
                await asyncio.sleep(2)
                html = await page.content()
                if not self._is_captcha_page(html):
                    logging.info("Manual CAPTCHA challenge solved for %s.", site_key)
                    return html
            else:
                try:
                    await page.bring_to_front()
                except Exception:
                    pass

        raise RuntimeError(
            f"CAPTCHA سایت {site_key} در مهلت {wait_seconds} ثانیه‌ای حل نشد. "
            "از پنل مانیتورینگ تب‌های CAPTCHA را باز کنید و دوباره بررسی کنید."
        )

    async def _get_site_html(self, site_key: str, bot: Bot | None = None) -> str:
        base_url = self.get_site_url(site_key)
        fresh_url = _add_cache_buster(base_url)

        use_real_browser = bool(cfg.get("monitor", "use_real_browser", True))
        if use_real_browser:
            ok = await self.ensure_real_browser_open()
            if not ok or self._browser_context is None:
                detail = self._browser_start_error or "دلیل نامشخص"
                raise RuntimeError(
                    "Playwright/Chromium در دسترس نیست و چون monitor.use_real_browser فعال است "
                    "درخواست به cloudscraper واگذار نشد. علت راه‌اندازی مرورگر: " + detail
                )

            async with self._browser_lock:
                page = self._pages.get(site_key)
                if page is None or page.is_closed():
                    page = await self._browser_context.new_page()
                    self._pages[site_key] = page

                response, navigation_error = await self._navigate_page_with_retry(
                    page,
                    fresh_url,
                    site_key,
                )
                try:
                    settle_seconds = float(cfg.get("monitor", "page_settle_seconds", 3) or 3)
                except (TypeError, ValueError):
                    settle_seconds = 3.0
                await asyncio.sleep(max(0.5, min(settle_seconds, 15.0)))
                try:
                    page_html = await page.content()
                except Exception as exc:
                    raise RuntimeError(
                        f"Playwright نتوانست HTML صفحه {site_key} را بخواند: {exc}"
                    ) from exc

                if not page_html.strip() or page.url == "about:blank":
                    raise RuntimeError(
                        f"Chromium نتوانست صفحه {site_key} را بارگذاری کند: {navigation_error or 'صفحه خالی است'}"
                    )

                status = response.status if response is not None else None
                if status is not None and status >= 400:
                    logging.warning(
                        "Playwright received HTTP %s for %s; page remains available for manual inspection.",
                        status,
                        site_key,
                    )

                captcha_reason = self._captcha_detection_reason(page_html)
                if captcha_reason:
                    logging.warning(
                        "Real CAPTCHA challenge detected for %s: %s (HTTP status=%s, URL=%s)",
                        site_key,
                        captcha_reason,
                        status,
                        page.url,
                    )
                    page_html = await self._wait_for_manual_captcha(
                        page,
                        site_key,
                        bot=bot,
                    )
                elif status is not None and status >= 400:
                    title = ""
                    try:
                        title = await page.title()
                    except Exception:
                        pass
                    raise RuntimeError(
                        f"سایت {site_key} پاسخ HTTP {status} برگرداند، اما صفحه نشانه معتبر CAPTCHA ندارد. "
                        f"عنوان صفحه: {title[:120] or 'بدون عنوان'}. "
                        "این خطا به‌عنوان CAPTCHA در نظر گرفته نشد؛ احتمالاً دسترسی توسط سایت رد شده است."
                    )

                return page_html

        # Legacy HTTP scraping is used only when explicitly selected in config.yaml.
        def _sync_get():
            scraper = cloudscraper.create_scraper(
                browser={"browser": "chrome", "platform": "windows", "desktop": True}
            )
            scraper.headers.update({
                "Cache-Control": "no-cache, no-store, must-revalidate",
                "Pragma": "no-cache",
                "Expires": "0",
            })
            proxies = ProxyManager.get_requests_proxies()
            if proxies:
                scraper.proxies.update(proxies)
            resp = scraper.get(fresh_url, timeout=25)
            resp.raise_for_status()
            return resp.text

        page_html = await asyncio.to_thread(_sync_get)
        if self._is_captcha_page(page_html):
            raise RuntimeError("سایت با چالش کپچا/کلودفلر مواجه شد.")
        return page_html

    @staticmethod
    def _parse_pahe_html(html: str, base_url: str) -> list[dict]:
        soup = _make_soup(html)
        posts = []
        articles = soup.select("section.recent-blog article.item-list") or soup.select("article.item-list")

        for art in articles[:12]:
            title_el = art.select_one("h2.post-box-title a")
            if not title_el:
                continue
            post_title = title_el.get_text(strip=True)
            post_url = urljoin(base_url, title_el.get("href", "").strip()).split("?")[0]
            if not post_url:
                continue

            date_el = art.select_one("span.tie-date")
            post_date = date_el.get_text(strip=True) if date_el else ""
            cats = [a.get_text(strip=True) for a in art.select("span.post-cats a")]
            img_el = art.select_one("div.post-thumbnail img")
            poster = urljoin(base_url, img_el.get("src", "").strip()) if img_el else ""
            excerpt_el = art.select_one("div.entry p")
            excerpt = excerpt_el.get_text(" ", strip=True) if excerpt_el else ""

            unique_id = f"{post_url}|{post_title}"
            posts.append({
                "id": unique_id,
                "site": "Pahe",
                "title": post_title,
                "url": post_url,
                "details": post_date,
                "categories": ", ".join(cats),
                "poster": poster,
                "summary": excerpt[:250]
            })
        return posts

    @staticmethod
    def _parse_psa_html(html: str, base_url: str) -> list[dict]:
        soup = _make_soup(html)
        posts = []
        articles = soup.select("div.featured article.post, div#grid-wrapper article.post")

        for art in articles[:12]:
            title_el = art.select_one("h2.post-title a")
            if not title_el:
                continue
            post_title = title_el.get_text(strip=True)
            post_url = urljoin(base_url, title_el.get("href", "").strip()).split("?")[0]

            caption_el = art.select_one("p.caption")
            update_text = caption_el.get_text(strip=True) if caption_el else ""
            cats = [a.get_text(strip=True) for a in art.select("p.post-category a")]
            time_el = art.select_one("p.post-date time")
            post_date = time_el.get("datetime") or (time_el.get_text(strip=True) if time_el else "")
            img_el = art.select_one("div.post-thumbnail img")
            poster = urljoin(base_url, img_el.get("src", "").strip()) if img_el else ""
            excerpt_el = art.select_one("div.entry.excerpt p")
            excerpt = excerpt_el.get_text(" ", strip=True) if excerpt_el else ""

            unique_id = f"{post_url}|{post_title}|{update_text}"
            posts.append({
                "id": unique_id,
                "site": "PSA",
                "title": post_title,
                "url": post_url,
                "details": f"{update_text} ({post_date})".strip(),
                "categories": " / ".join(cats),
                "poster": poster,
                "summary": excerpt[:250]
            })
        return posts

    @staticmethod
    def _parse_yts_html(html: str, base_url: str) -> list[dict]:
        soup = _make_soup(html)
        posts = []
        latest_cards = soup.select("div.content-dark div.home-movies div.browse-movie-wrap") or soup.select("div.browse-movie-wrap")

        for card in latest_cards[:12]:
            title_el = card.select_one("a.browse-movie-title")
            if not title_el:
                continue
            post_title = title_el.get_text(" ", strip=True)
            post_url = urljoin(base_url, title_el.get("href", "").strip()).split("?")[0]

            year_el = card.select_one("div.browse-movie-year")
            year_text = year_el.get_text(strip=True) if year_el else ""

            quality = ""
            q_banner = card.select_one("img.quality-banner")
            if q_banner:
                q_src = q_banner.get("src", "")
                for q_tag in ("2160p", "1080p", "720p", "480p"):
                    if q_tag in q_src or q_tag in q_banner.get("alt", ""):
                        quality = q_tag
                        break

            rating_el = card.select_one("figcaption h4.rating")
            rating = rating_el.get_text(strip=True) if rating_el else "N/A"
            genres = [
                h4.get_text(strip=True)
                for h4 in card.select("figcaption h4")
                if "rating" not in (h4.get("class") or [])
            ]
            img_el = card.select_one("figure img.img-responsive")
            poster = urljoin(base_url, img_el.get("src", "").strip()) if img_el else ""

            unique_id = f"{post_url}|{quality}"
            posts.append({
                "id": unique_id,
                "site": "YTS",
                "title": f"{post_title} ({year_text})" if year_text else post_title,
                "url": post_url,
                "details": f"کیفیت: {quality or 'HD'} | امتیاز: ⭐ {rating}",
                "categories": ", ".join(genres),
                "poster": poster,
                "summary": ""
            })
        return posts

    @staticmethod
    def _parse_digimovie_html(html: str, base_url: str) -> list[dict]:
        soup = _make_soup(html)
        posts = []

        # ۱. رصد بخش «سریال‌های جدید و محبوب -> جدیدترین»
        series_cards = soup.select("div.last_series div#box_tab1 div.item_dubbled") or soup.select("div.last_series div.item_dubbled")

        for s_card in series_cards[:15]:
            a_tag = s_card.select_one("a")
            if not a_tag:
                continue
            post_url = urljoin(base_url, a_tag.get("href", "").strip()).split("?")[0]
            if not post_url:
                continue

            img_el = s_card.select_one("div.cover img")
            raw_title = a_tag.get("title", "").strip()
            if not raw_title and img_el:
                raw_title = img_el.get("alt", "").strip()
            if not raw_title:
                raw_title = post_url.rstrip("/").split("/")[-1].replace("-", " ").title()

            poster = ""
            if img_el:
                raw_src = (img_el.get("src") or img_el.get("data-src") or "").strip()
                if raw_src:
                    poster = urljoin(base_url, raw_src)

            ep_el = s_card.select_one("div.text_holder_series")
            ep_update_text = re.sub(r"\s+", " ", ep_el.get_text(" ", strip=True)) if ep_el else "قسمت جدید منتشر شد"

            # استفاده از مسیر نسبی (Path) در شناسه یکتا تا با تغییر دامنه سایت، پست‌های قبلی تکراری ارسال نشوند!
            rel_path = urlparse(post_url).path.rstrip("/")
            unique_id = f"SERIE|{rel_path}|{ep_update_text}"
            posts.append({
                "id": unique_id,
                "site": "Digimoviez (بروزرسانی سریال)",
                "title": raw_title,
                "url": post_url,
                "details": f"🔥 {ep_update_text}",
                "categories": "سریال جدید / قسمت جدید",
                "poster": poster,
                "summary": ""
            })

        # ۲. رصد پست‌های اصلی فیلم و سریال در بدنه صفحه
        items = soup.select("div.holder_seactions div.item_def_loop")
        for item in items[:10]:
            title_el = item.select_one("div.title_h h2 a")
            if not title_el:
                continue
            post_title = title_el.get_text(strip=True)
            post_url = urljoin(base_url, title_el.get("href", "").strip()).split("?")[0]

            img_el = item.select_one("div.cover img")
            poster = urljoin(base_url, (img_el.get("src") or img_el.get("data-src") or "").strip()) if img_el else ""

            notif_el = item.select_one("div.notif_items div.item_notif")
            notif_text = notif_el.get_text(strip=True) if notif_el else ""

            qual_el = item.select_one("div.meta_item ul li span.res_item.iranYekanReg")
            qual_text = qual_el.get_text(strip=True) if qual_el else ""

            plot_el = item.select_one("div.plot_text")
            plot = plot_el.get_text(" ", strip=True) if plot_el else ""

            rel_path = urlparse(post_url).path.rstrip("/")
            unique_id = f"MAIN|{rel_path}|{notif_text}|{qual_text}"
            posts.append({
                "id": unique_id,
                "site": "Digimoviez",
                "title": post_title,
                "url": post_url,
                "details": " | ".join(filter(None, [notif_text, qual_text])) or "پست جدید",
                "categories": "",
                "poster": poster,
                "summary": plot[:250]
            })

        return posts

    @staticmethod
    def _parse_avamovie_html(html: str, base_url: str) -> list[dict]:
        soup = _make_soup(html)
        posts = []
        series_items = soup.select("section.update-series div.item-series")
        movie_items = soup.select("section.home-section.movie div.item-movie")

        for item in series_items[:12]:
            title_el = item.select_one("div.title")
            link_el = item.select_one("a")
            if not title_el or not link_el:
                continue
            post_title = title_el.get_text(strip=True)
            post_url = urljoin(base_url, link_el.get("href", "").strip()).split("?")[0]

            se_el = item.select_one("div.se")
            update_txt = re.sub(r"\s+", " ", se_el.get_text(" ", strip=True)) if se_el else ""

            plot_el = item.select_one("div.plot div.text")
            plot = plot_el.get_text(" ", strip=True) if plot_el else ""

            rel_path = urlparse(post_url).path.rstrip("/")
            unique_id = f"SERIE|{rel_path}|{update_txt}"
            posts.append({
                "id": unique_id,
                "site": "AvaMovie (سریال)",
                "title": f"سریال {post_title}",
                "url": post_url,
                "details": update_txt,
                "categories": "بروزرسانی سریال",
                "poster": "",
                "summary": plot[:250]
            })

        for item in movie_items[:8]:
            title_el = item.select_one("div.bottom div.title_en")
            link_el = item.select_one("a")
            if not title_el or not link_el:
                continue
            post_title = title_el.get_text(strip=True)
            post_url = urljoin(base_url, link_el.get("href", "").strip()).split("?")[0]

            genre_el = item.select_one("div.hover div.genre")
            genres = genre_el.get_text(strip=True) if genre_el else ""

            rel_path = urlparse(post_url).path.rstrip("/")
            unique_id = f"MOVIE|{rel_path}"
            posts.append({
                "id": unique_id,
                "site": "AvaMovie (فیلم)",
                "title": f"فیلم {post_title}",
                "url": post_url,
                "details": genres,
                "categories": "فیلم",
                "poster": "",
                "summary": ""
            })
        return posts

    async def fetch_site_posts(self, site_key: str, bot: Bot | None = None) -> list[dict]:
        base_url = self.get_site_url(site_key)
        try:
            captcha_wait = int(cfg.get("monitor", "captcha_wait_seconds", 300) or 300)
        except (TypeError, ValueError):
            captcha_wait = 300
        # Allow two full page-load attempts plus the optional manual CAPTCHA wait.
        navigation_budget = (self._page_navigation_timeout_ms() / 1000) * 2
        fetch_timeout = max(
            90,
            navigation_budget
            + self._page_retry_delay_seconds()
            + min(captcha_wait, 1800)
            + 45,
        )
        html = await asyncio.wait_for(
            self._get_site_html(site_key, bot=bot),
            timeout=fetch_timeout,
        )
        if site_key == "pahe":
            posts = self._parse_pahe_html(html, base_url)
        elif site_key == "psa":
            posts = self._parse_psa_html(html, base_url)
        elif site_key == "yts":
            posts = self._parse_yts_html(html, base_url)
        elif site_key == "digimovie":
            posts = self._parse_digimovie_html(html, base_url)
        elif site_key == "avamovie":
            posts = self._parse_avamovie_html(html, base_url)
        else:
            posts = []

        if not posts:
            raise RuntimeError("سایت باز شد اما هیچ پستی استخراج نشد (احتمال تغییر دامنه یا صفحه کپچا).")
        return posts

    @staticmethod
    def _build_post_caption(post: dict) -> str:
        lines = [
            f"🔔 **پست جدید در {post['site']}**",
            f"🎬 **{post['title']}**",
        ]
        if post.get("details"):
            lines.append(f"📌 `{post['details']}`")
        if post.get("categories"):
            lines.append(f"🎭 دسته: {post['categories']}")
        if post.get("summary"):
            lines.append(f"\n📝 _{post['summary']}_")
        lines.append(f"\n🔗 [مشاهده صفحه در سایت]({post['url']})")
        return "\n".join(lines)

    @staticmethod
    def _build_post_kb(post: dict) -> InlineKeyboardMarkup:
        builder = InlineKeyboardBuilder()
        builder.row(InlineKeyboardButton(text="🌐 باز کردن لینک پست", url=post["url"]))
        safe_title = post["title"][:35].replace(":", "").replace("/", " ")
        builder.row(
            InlineKeyboardButton(text="📁 ساخت پوشه برای این عنوان", callback_data=f"qmkdir:{safe_title}")
        )
        return builder.as_markup()

    async def send_post_notification(self, bot: Bot, post: dict):
        admin_ids = cfg.get("telegram", "admin_ids", [])
        caption = self._build_post_caption(post)
        kb = self._build_post_kb(post)
        poster = post.get("poster", "")

        for admin_id in admin_ids:
            try:
                if poster and poster.startswith("http"):
                    try:
                        await bot.send_photo(
                            chat_id=admin_id,
                            photo=poster,
                            caption=caption,
                            reply_markup=kb,
                            parse_mode="Markdown"
                        )
                        continue
                    except Exception:
                        pass
                await bot.send_message(
                    chat_id=admin_id,
                    text=caption,
                    reply_markup=kb,
                    parse_mode="Markdown",
                    disable_web_page_preview=False
                )
            except Exception as e:
                logging.error(f"Failed to send monitor alert to {admin_id}: {e}")

    async def check_all_sites_once(self, bot: Bot, notify_on_first_seed: bool = False) -> dict[str, int]:
        async with self._check_lock:
            now_str = datetime.now().strftime("%H:%M:%S")
            sites_cfg = self.get_all_sites_config()
            active_keys = [k for k, v in sites_cfg.items() if v["enabled"]]
            logging.info(f"🔎 [MONITOR {now_str}] بررسی خودکار سایت‌های فعال: {active_keys}...")

            state = self._load_state()
            new_counts = {k: 0 for k in self.DEFAULT_SITES}

            for site_key, s_info in sites_cfg.items():
                if not s_info["enabled"]:
                    continue
                try:
                    posts = await self.fetch_site_posts(site_key, bot=bot)
                    try:
                        from services.daily_activity_service import DailyActivityService
                        DailyActivityService.record(
                            "monitor_checked",
                            site=site_key,
                            posts_scanned=len(posts),
                        )
                    except Exception:
                        logging.exception("Could not record monitor check")
                    seen_list = state.get(site_key, [])
                    seen_ids = set(seen_list)
                    # تطبیق شناسه‌های قدیمی که با کل URL ذخیره شده بودند با شناسه‌های جدید مبتنی بر Path
                    seen_paths = {sid.split("|")[1] if "|" in sid else sid for sid in seen_ids}
                    is_first_seed = (len(seen_ids) == 0)

                    new_posts = [
                        p for p in reversed(posts)
                        if p["id"] not in seen_ids
                    ]

                    if is_first_seed:
                        state[site_key] = [p["id"] for p in reversed(posts)]
                        self._save_state(state)
                        logging.info(f"📌 [MONITOR] سایت {site_key}: تعداد {len(posts)} پست اولیه در حافظه ثبت شد.")
                        if notify_on_first_seed and posts:
                            await self.send_post_notification(bot, posts[0])
                            new_counts[site_key] = 1
                    else:
                        if new_posts:
                            logging.info(f"🎉 [MONITOR] سایت {site_key}: تعداد {len(new_posts)} محتوای جدید پیدا شد!")
                            for post in new_posts:
                                try:
                                    from services.daily_activity_service import DailyActivityService
                                    DailyActivityService.record(
                                        "monitor_post_found",
                                        site=site_key,
                                        title=post.get("title", ""),
                                        url=post.get("url", ""),
                                    )
                                except Exception:
                                    logging.exception("Could not record monitored post")
                                await self.send_post_notification(bot, post)
                                state.setdefault(site_key, []).append(post["id"])
                                new_counts[site_key] += 1
                                await asyncio.sleep(0.8)
                            self._save_state(state)
                        else:
                            logging.info(f"✔️ [MONITOR] سایت {site_key}: بدون تغییر ({len(posts)} پست بررسی شد).")

                except Exception as e:
                    logging.warning(f"⚠️ [MONITOR ERROR] خطا در بررسی سایت {site_key}: {e}")
                    try:
                        from services.daily_activity_service import DailyActivityService
                        DailyActivityService.record(
                            "monitor_error",
                            site=site_key,
                            error=str(e)[:700],
                        )
                    except Exception:
                        logging.exception("Could not record monitor error")
                    await self.send_error_notification(bot, site_key, str(e), force=notify_on_first_seed)

            return new_counts

    async def _loop(self, bot: Bot):
        self._running = True
        logging.info("⏱ حلقه مانیتورینگ خودکار فعال شد.")

        # Do not let the first automatic scan navigate pages while the startup
        # task is still opening the visible site tabs in the same browser context.
        warmup_task = self._browser_warmup_task
        if warmup_task is not None and warmup_task is not asyncio.current_task():
            try:
                await warmup_task
            except asyncio.CancelledError:
                raise
            except Exception:
                logging.exception("Monitor browser warmup failed before the first scan.")

        while self._running:
            cycle_started = time.monotonic()
            try:
                if bool(cfg.get("monitor", "enabled", True)):
                    counts = await self.check_all_sites_once(
                        bot,
                        notify_on_first_seed=False,
                    )
                    total_new = sum(int(value or 0) for value in counts.values())
                    logging.info(
                        "✅ [MONITOR] چرخه بررسی تمام شد؛ محتوای جدید: %d",
                        total_new,
                    )
                else:
                    logging.info("⏸ [MONITOR] مانیتورینگ در config.yaml غیرفعال است.")
            except asyncio.CancelledError:
                logging.info("Monitor loop cancellation received.")
                raise
            except Exception:
                logging.exception("Unexpected error in automatic monitor cycle; loop will continue.")

            try:
                interval = int(cfg.get("monitor", "interval_seconds", 300) or 300)
            except (TypeError, ValueError):
                interval = 300
            interval = max(60, min(interval, 24 * 60 * 60))

            # زمان‌بندی بر اساس انتهای چرخه نیست: مدت اجرای بررسی از فاصله هدف کم می‌شود.
            elapsed = max(0.0, time.monotonic() - cycle_started)
            sleep_for = max(1.0, interval - elapsed)
            logging.info(
                "⏱ [MONITOR] چرخه بعدی حدود %.0f ثانیه دیگر اجرا می‌شود.",
                sleep_for,
            )
            try:
                await asyncio.sleep(sleep_for)
            except asyncio.CancelledError:
                logging.info("Monitor loop stopped during interval wait.")
                raise

        logging.info("Site monitor loop stopped.")

    async def _warmup_browser(self) -> None:
        try:
            # Open the actual configured monitor URLs during startup. Previously warmup
            # only created an about:blank control tab, which looked like monitoring had
            # opened Chromium but never loaded any sites.
            result = await self.open_captcha_tabs_for_admin()
            if result:
                logging.info("Monitor browser startup tabs: %s", result.replace("\n", " | "))
        except Exception:
            logging.exception("Unexpected Playwright browser warmup error")

    def start(self, bot: Bot):
        self._running = True
        if self._task is not None and not self._task.done():
            logging.info("Site Monitor is already running; duplicate start ignored.")
            return

        if bool(cfg.get("monitor", "use_real_browser", True)):
            if self._browser_warmup_task is None or self._browser_warmup_task.done():
                self._browser_warmup_task = asyncio.create_task(
                    self._warmup_browser(),
                    name="monitor-playwright-warmup",
                )

        self._task = asyncio.create_task(
            self._loop(bot),
            name="site-monitor-loop",
        )

        def _done(task: asyncio.Task):
            if task.cancelled():
                logging.info("Site Monitor background task was cancelled.")
                return
            try:
                error = task.exception()
            except asyncio.CancelledError:
                return
            if error:
                logging.error(
                    "Site Monitor background task exited unexpectedly: %s",
                    error,
                    exc_info=(type(error), error, error.__traceback__),
                )

        self._task.add_done_callback(_done)
        logging.info(
            "Site Monitor started (enabled=%s, interval=%s seconds).",
            bool(cfg.get("monitor", "enabled", True)),
            cfg.get("monitor", "interval_seconds", 300),
        )

    def stop(self):
        self._running = False
        task = self._task
        if task is not None and not task.done():
            task.cancel()
        self._task = None
        logging.info("Site Monitor stop requested.")


monitor_service = SiteMonitorService()