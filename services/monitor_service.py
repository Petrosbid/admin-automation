import asyncio
from datetime import datetime
import importlib.util
import json
import logging
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
    SITE_URLS = {
        "pahe": "https://pahe.ink/",
        "psa": "https://psa.wf/",
        "yts": "https://yts.gg/home",
        "digimovie": "https://digimoviez53.top/",
        "avamovie": "https://avasaraw.top/",
    }

    def __init__(self):
        self._task: asyncio.Task | None = None
        self._running: bool = False
        self._pw = None
        self._browser_context = None
        self._pages: dict[str, object] = {}
        self._browser_lock = asyncio.Lock()
        self._check_lock = asyncio.Lock()
        self._pw_installed = importlib.util.find_spec("playwright") is not None
        self._last_error_notified: dict[str, float] = {}

    @property
    def state_file(self) -> Path:
        ws = Path(cfg.get("paths", "workspace_dir", "./workspace")).resolve()
        ws.mkdir(parents=True, exist_ok=True)
        return ws / "monitor_state.json"

    @property
    def profile_dir(self) -> Path:
        ws = Path(cfg.get("paths", "workspace_dir", "./workspace")).resolve()
        p_dir = ws / "browser_profile"
        p_dir.mkdir(parents=True, exist_ok=True)
        return p_dir

    def _load_state(self) -> dict[str, list[str]]:
        default_state = {k: [] for k in self.SITE_URLS}
        if self.state_file.exists():
            try:
                raw = self.state_file.read_text(encoding="utf-8")
                data = json.loads(raw)
                if isinstance(data, dict):
                    for k in self.SITE_URLS:
                        if isinstance(data.get(k), list):
                            default_state[k] = data[k]
            except Exception as e:
                logging.error(f"Error reading monitor_state.json: {e}")
        return default_state

    def _save_state(self, state: dict[str, list[str]]):
        """ذخیره اتمیک و ایمن وضعیت سایت‌ها روی دیسک تا با ری‌استارت ربات هرگز پاک نشود"""
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
        """ارسال پیام خطا به تلگرام (با محدودیت ضد اسپم هر ۳۰ دقیقه یک‌بار برای هر سایت در پس‌زمینه)"""
        if not bot:
            return
        now = time.time()
        last_sent = self._last_error_notified.get(site_key, 0.0)
        if not force and (now - last_sent < 1800):
            return
        self._last_error_notified[site_key] = now

        url = self.SITE_URLS.get(site_key, site_key)
        msg = (
            f"⚠️ **خطا در مانیتورینگ سایت `{site_key.upper()}`**\n"
            f"🌐 آدرس: `{url}`\n"
            f"❌ علت خطا:\n`{str(error_text)[:350]}`"
        )
        for admin_id in cfg.get("telegram", "admin_ids", []):
            try:
                await bot.send_message(chat_id=admin_id, text=msg, parse_mode="Markdown")
            except Exception as e:
                logging.error(f"Failed to send monitor error alert to {admin_id}: {e}")

    async def ensure_real_browser_open(self) -> bool:
        if not self._pw_installed:
            return False

        async with self._browser_lock:
            if self._browser_context is not None:
                return True
            try:
                from playwright.async_api import async_playwright
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
                    ],
                }
                proxy_url = ProxyManager.get_proxy_url()
                if proxy_url:
                    launch_kwargs["proxy"] = {"server": proxy_url}

                try:
                    self._browser_context = await self._pw.chromium.launch_persistent_context(
                        channel="chrome", **launch_kwargs
                    )
                except Exception:
                    self._browser_context = await self._pw.chromium.launch_persistent_context(
                        **launch_kwargs
                    )

                for key, url in self.SITE_URLS.items():
                    page = await self._browser_context.new_page()
                    self._pages[key] = page
                    try:
                        await page.goto(url, wait_until="domcontentloaded", timeout=25000)
                    except Exception as e:
                        logging.warning(f"Initial navigation warning for {key}: {e}")

                logging.info("Real Browser opened with 5 tabs for Captcha solving.")
                return True
            except Exception as e:
                logging.warning(f"Playwright browser could not start: {e}")
                self._pw_installed = False
                return False

    async def open_captcha_tabs_for_admin(self) -> str:
        self._pw_installed = importlib.util.find_spec("playwright") is not None
        ok = await self.ensure_real_browser_open()
        if ok and self._browser_context:
            for key, url in self.SITE_URLS.items():
                page = self._pages.get(key)
                if page is None or page.is_closed():
                    page = await self._browser_context.new_page()
                    self._pages[key] = page
                try:
                    await page.goto(url, wait_until="domcontentloaded", timeout=25000)
                    await page.bring_to_front()
                except Exception:
                    pass
            return (
                "✅ پنجره مرورگر روی دسکتاپ ویندوز سرور باز شد و تب‌های هر ۵ سایت بارگذاری شدند.\n\n"
                "🔓 اکنون می‌توانید روی سرور کپچای آن‌ها را حل کنید؛ کوکی‌های حل کپچا ذخیره می‌شوند."
            )

        for url in self.SITE_URLS.values():
            try:
                webbrowser.open(url)
            except Exception:
                pass
        return (
            "🌐 صفحات سایت‌ها در مرورگر ویندوز سرور باز شدند.\n\n"
            "💡 برای راه‌اندازی مرورگر خودکار پایتون، دستورات زیر را در ترمینال سرور اجرا کنید:\n"
            "`pip install playwright`\n"
            "`playwright install chromium`"
        )

    @staticmethod
    def _is_captcha_page(html: str) -> bool:
        lower = html.lower()
        if "<title>just a moment..." in lower or "cf-browser-verification" in lower:
            return True
        if "verify you are human" in lower and "cloudflare" in lower:
            return True
        return False

    async def _get_site_html(self, site_key: str, bot: Bot | None = None) -> str:
        base_url = self.SITE_URLS[site_key]
        fresh_url = _add_cache_buster(base_url)

        # ۱. استفاده از مرورگر واقعی فقط اگر نصب و فعال باشد
        if cfg.get("monitor", "use_real_browser", True) and self._pw_installed:
            ok = await self.ensure_real_browser_open()
            if ok and self._browser_context:
                async with self._browser_lock:
                    page = self._pages.get(site_key)
                    if page is None or page.is_closed():
                        page = await self._browser_context.new_page()
                        self._pages[site_key] = page

                    try:
                        await page.goto(fresh_url, wait_until="domcontentloaded", timeout=25000)
                    except Exception:
                        await page.reload(wait_until="domcontentloaded", timeout=25000)

                    await asyncio.sleep(1.5)
                    html = await page.content()

                    if self._is_captcha_page(html):
                        await page.bring_to_front()
                        for _ in range(4):
                            await asyncio.sleep(3)
                            html = await page.content()
                            if not self._is_captcha_page(html):
                                break
                        if self._is_captcha_page(html):
                            raise RuntimeError("صفحه درگیر کپچای کلودفلر است و نیاز به حل دستی روی سرور دارد.")

                    return html

        # ۲. دریافت مستقیم با cloudscraper همراه با هدرهای ضدکش (جلوگیری از دریافت صفحه کش شده)
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

        html = await asyncio.to_thread(_sync_get)
        if self._is_captcha_page(html):
            raise RuntimeError("سایت با چالش کپچا/کلودفلر مواجه شد.")
        return html

    @staticmethod
    def _parse_pahe_html(html: str) -> list[dict]:
        soup = _make_soup(html)
        posts = []
        articles = soup.select("section.recent-blog article.item-list") or soup.select("article.item-list")

        for art in articles[:12]:
            title_el = art.select_one("h2.post-box-title a")
            if not title_el:
                continue
            post_title = title_el.get_text(strip=True)
            post_url = title_el.get("href", "").strip().split("?")[0]
            if not post_url:
                continue

            date_el = art.select_one("span.tie-date")
            post_date = date_el.get_text(strip=True) if date_el else ""
            cats = [a.get_text(strip=True) for a in art.select("span.post-cats a")]
            img_el = art.select_one("div.post-thumbnail img")
            poster = img_el.get("src", "").strip() if img_el else ""
            excerpt_el = art.select_one("div.entry p")
            excerpt = excerpt_el.get_text(" ", strip=True) if excerpt_el else ""

            # شناسه شامل لینک + عنوان کامل (که در Pahe حاوی شماره قسمت یا کیفیت جدید است)
            unique_id = f"{post_url}|{post_title}"
            posts.append({
                "id": unique_id,
                "site": "Pahe.ink",
                "title": post_title,
                "url": post_url,
                "details": post_date,
                "categories": ", ".join(cats),
                "poster": poster,
                "summary": excerpt[:250]
            })
        return posts

    @staticmethod
    def _parse_psa_html(html: str) -> list[dict]:
        base_url = "https://psa.wf/"
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
                "site": "PSA.wf",
                "title": post_title,
                "url": post_url,
                "details": f"{update_text} ({post_date})".strip(),
                "categories": " / ".join(cats),
                "poster": poster,
                "summary": excerpt[:250]
            })
        return posts

    @staticmethod
    def _parse_yts_html(html: str) -> list[dict]:
        soup = _make_soup(html)
        posts = []
        latest_cards = soup.select("div.content-dark div.home-movies div.browse-movie-wrap")
        if not latest_cards:
            latest_cards = soup.select("div.browse-movie-wrap")

        for card in latest_cards[:12]:
            title_el = card.select_one("a.browse-movie-title")
            if not title_el:
                continue
            post_title = title_el.get_text(" ", strip=True)
            post_url = urljoin("https://yts.gg", title_el.get("href", "").strip()).split("?")[0]

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
            poster = urljoin("https://yts.gg", img_el.get("src", "").strip()) if img_el else ""

            unique_id = f"{post_url}|{quality}"
            posts.append({
                "id": unique_id,
                "site": "YTS.gg",
                "title": f"{post_title} ({year_text})" if year_text else post_title,
                "url": post_url,
                "details": f"کیفیت: {quality or 'HD'} | امتیاز: ⭐ {rating}",
                "categories": ", ".join(genres),
                "poster": poster,
                "summary": ""
            })
        return posts

    @staticmethod
    def _parse_digimovie_html(html: str) -> list[dict]:
        base_url = "https://digimoviez53.top/"
        soup = _make_soup(html)
        posts = []

        # ۱. رصد بخش «سریال‌های جدید و محبوب -> جدیدترین»
        series_cards = soup.select("div.last_series div#box_tab1 div.item_dubbled")
        if not series_cards:
            series_cards = soup.select("div.last_series div.item_dubbled")

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

            unique_id = f"SERIE|{post_url}|{ep_update_text}"
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

            # استخراج کیفیت فعلی پست (تا اگر کیفیت جدیدتری مثل 1080p BluRay اضافه شد، تشخیص دهد)
            qual_el = item.select_one("div.meta_item ul li span.res_item.iranYekanReg")
            qual_text = qual_el.get_text(strip=True) if qual_el else ""

            plot_el = item.select_one("div.plot_text")
            plot = plot_el.get_text(" ", strip=True) if plot_el else ""

            unique_id = f"MAIN|{post_url}|{notif_text}|{qual_text}"
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
    def _parse_avamovie_html(html: str) -> list[dict]:
        base_url = "https://avasaraw.top/"
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

            unique_id = f"SERIE|{post_url}|{update_txt}"
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

            unique_id = f"MOVIE|{post_url}"
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
        # اعمال تایم‌اوت ۴۰ ثانیه‌ای روی دریافت هر سایت تا لوپ ۵ دقیقه‌ای هرگز قفل نشود
        html = await asyncio.wait_for(self._get_site_html(site_key, bot=bot), timeout=40.0)
        if site_key == "pahe":
            posts = self._parse_pahe_html(html)
        elif site_key == "psa":
            posts = self._parse_psa_html(html)
        elif site_key == "yts":
            posts = self._parse_yts_html(html)
        elif site_key == "digimovie":
            posts = self._parse_digimovie_html(html)
        elif site_key == "avamovie":
            posts = self._parse_avamovie_html(html)
        else:
            posts = []

        if not posts:
            raise RuntimeError("سایت باز شد اما هیچ پستی استخراج نشد (احتمال تغییر ساختار یا صفحه کپچا).")
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
        """
        بررسی هر ۵ سایت:
        - اگر برای سایتی هنوز هیچ سابقه‌ای در monitor_state.json ثبت نشده باشد (اولین اجرا):
          پست‌های فعلی را بدون اسپم کردن تلگرام در فایل ذخیره (Seed) می‌کند تا در چرخه‌های ۵ دقیقه‌ای بعدی،
          فقط پست‌های واقعاً جدید را اعلام کند!
        - اگر در چرخه‌های بعدی پست جدیدی منتشر شده باشد، بلافاصله در تلگرام ارسال می‌کند.
        """
        async with self._check_lock:
            now_str = datetime.now().strftime("%H:%M:%S")
            logging.info(f"🔎 [MONITOR {now_str}] شروع بررسی خودکار ۵ سایت (Pahe, PSA, YTS, Digimoviez, AvaMovie)...")

            state = self._load_state()
            site_keys = ("pahe", "psa", "yts", "digimovie", "avamovie")
            new_counts = {k: 0 for k in site_keys}

            for site_key in site_keys:
                try:
                    posts = await self.fetch_site_posts(site_key, bot=bot)
                    seen_list = state.get(site_key, [])
                    seen_ids = set(seen_list)
                    is_first_seed = (len(seen_ids) == 0)

                    # حفظ ترتیب انتشار (از قدیمی‌ترینِ جدیدها به جدیدترین)
                    new_posts = [p for p in reversed(posts) if p["id"] not in seen_ids]

                    if is_first_seed:
                        # ذخیره اولیه تمام پست‌های موجود روی صفحه تا موقع ری‌استارت اسپم نشوند
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
                                await self.send_post_notification(bot, post)
                                state.setdefault(site_key, []).append(post["id"])
                                new_counts[site_key] += 1
                                await asyncio.sleep(0.8)
                            self._save_state(state)
                        else:
                            logging.info(f"✔️ [MONITOR] سایت {site_key}: بدون تغییر ({len(posts)} پست بررسی شد).")

                except Exception as e:
                    logging.warning(f"⚠️ [MONITOR ERROR] خطا در بررسی سایت {site_key}: {e}")
                    await self.send_error_notification(bot, site_key, str(e), force=notify_on_first_seed)

            return new_counts

    async def _loop(self, bot: Bot):
        self._running = True
        logging.info("⏱ حلقه مانیتورینگ ۵ دقیقه‌ای فعال شد.")
        while self._running:
            try:
                if cfg.get("monitor", "enabled", True):
                    await self.check_all_sites_once(bot, notify_on_first_seed=False)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logging.error(f"Unexpected error in monitor loop: {e}")

            interval = max(60, int(cfg.get("monitor", "interval_seconds", 300)))
            await asyncio.sleep(interval)

    def start(self, bot: Bot):
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop(bot))
            logging.info("Site Monitor started (checking 5 sites every 5 minutes).")

    def stop(self):
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()


monitor_service = SiteMonitorService()