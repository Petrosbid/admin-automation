import asyncio
import json
import logging
from pathlib import Path
from urllib.parse import urljoin
from bs4 import BeautifulSoup
import cloudscraper
from aiogram import Bot
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder

from core.config_loader import cfg
from core.proxy_manager import ProxyManager


class SiteMonitorService:
    def __init__(self):
        self._task: asyncio.Task | None = None
        self._running: bool = False

    @property
    def state_file(self) -> Path:
        ws = Path(cfg.get("paths", "workspace_dir", "./workspace"))
        ws.mkdir(parents=True, exist_ok=True)
        return ws / "monitor_state.json"

    def _load_state(self) -> dict[str, list[str]]:
        if self.state_file.exists():
            try:
                return json.loads(self.state_file.read_text(encoding="utf-8"))
            except Exception:
                pass
        return {"pahe": [], "psa": [], "yts": []}

    def _save_state(self, state: dict[str, list[str]]):
        # نگهداری حداکثر ۲۰۰ شناسه آخر برای هر سایت
        trimmed = {k: v[-200:] for k, v in state.items()}
        self.state_file.write_text(
            json.dumps(trimmed, ensure_ascii=False, indent=2),
            encoding="utf-8"
        )

    @staticmethod
    def _create_scraper():
        scraper = cloudscraper.create_scraper(
            browser={"browser": "chrome", "platform": "windows", "desktop": True}
        )
        proxies = ProxyManager.get_requests_proxies()
        if proxies:
            scraper.proxies.update(proxies)
        return scraper

    # ==================== ۱. پارسر اختصاصی Pahe.ink ====================
    @classmethod
    def _fetch_pahe_sync(cls) -> list[dict]:
        url = "https://pahe.ink/"
        scraper = cls._create_scraper()
        resp = scraper.get(url, timeout=25)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "lxml")

        posts = []
        articles = soup.select("section.recent-blog article.item-list")
        if not articles:
            articles = soup.select("article.item-list")

        for art in articles[:10]:
            title_el = art.select_one("h2.post-box-title a")
            if not title_el:
                continue
            post_title = title_el.get_text(strip=True)
            post_url = title_el.get("href", "").strip()
            if not post_url:
                continue

            date_el = art.select_one("span.tie-date")
            post_date = date_el.get_text(strip=True) if date_el else ""

            cats = [a.get_text(strip=True) for a in art.select("span.post-cats a")]
            img_el = art.select_one("div.post-thumbnail img")
            poster = img_el.get("src", "").strip() if img_el else ""

            excerpt_el = art.select_one("div.entry p")
            excerpt = excerpt_el.get_text(" ", strip=True) if excerpt_el else ""

            posts.append({
                "id": post_url,
                "site": "Pahe.ink",
                "title": post_title,
                "url": post_url,
                "details": post_date,
                "categories": ", ".join(cats),
                "poster": poster,
                "summary": excerpt[:250]
            })
        return posts

    # ==================== ۲. پارسر اختصاصی PSA.wf ====================
    @classmethod
    def _fetch_psa_sync(cls) -> list[dict]:
        url = "https://psa.wf/"
        scraper = cls._create_scraper()
        resp = scraper.get(url, timeout=25)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "lxml")

        posts = []
        # ترکیب پست ویژه بالای صفحه (featured) و گرید پست‌های جدید (grid-wrapper)
        articles = soup.select("div.featured article.post, div#grid-wrapper article.post")

        for art in articles[:12]:
            title_el = art.select_one("h2.post-title a")
            if not title_el:
                continue
            post_title = title_el.get_text(strip=True)
            post_url = urljoin(url, title_el.get("href", "").strip())

            caption_el = art.select_one("p.caption")
            update_text = caption_el.get_text(strip=True) if caption_el else ""

            cats = [a.get_text(strip=True) for a in art.select("p.post-category a")]
            time_el = art.select_one("p.post-date time")
            post_date = time_el.get("datetime") or (time_el.get_text(strip=True) if time_el else "")

            img_el = art.select_one("div.post-thumbnail img")
            poster = urljoin(url, img_el.get("src", "").strip()) if img_el else ""

            excerpt_el = art.select_one("div.entry.excerpt p")
            excerpt = excerpt_el.get_text(" ", strip=True) if excerpt_el else ""

            # شناسه یکتا شامل لینک + کپشن آپدیت است تا قسمت‌های جدید سریال‌ها تشخیص داده شوند
            unique_id = f"{post_url}|{update_text}"

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

    # ==================== ۳. پارسر اختصاصی YTS.gg/home ====================
    @classmethod
    def _fetch_yts_sync(cls) -> list[dict]:
        url = "https://yts.gg/home"
        scraper = cls._create_scraper()
        resp = scraper.get(url, timeout=25)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "lxml")

        posts = []
        # فقط بخش Latest YTS YIFY Movies Torrents داخل content-dark
        latest_cards = soup.select("div.content-dark div.home-movies div.browse-movie-wrap")

        for card in latest_cards[:12]:
            title_el = card.select_one("a.browse-movie-title")
            if not title_el:
                continue
            post_title = title_el.get_text(" ", strip=True)
            post_url = urljoin("https://yts.gg", title_el.get("href", "").strip())

            year_el = card.select_one("div.browse-movie-year")
            year_text = year_el.get_text(strip=True) if year_el else ""

            # استخراج کیفیت از بنر (مثلا banner2160p.png یا alt="... 1080p")
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

    @classmethod
    async def fetch_site_posts(cls, site_key: str) -> list[dict]:
        if site_key == "pahe":
            return await asyncio.to_thread(cls._fetch_pahe_sync)
        elif site_key == "psa":
            return await asyncio.to_thread(cls._fetch_psa_sync)
        elif site_key == "yts":
            return await asyncio.to_thread(cls._fetch_yts_sync)
        return []

    @staticmethod
    def _build_post_caption(post: dict) -> str:
        lines = [
            f"🔔 **پست جدید در {post['site']}**",
            f"🎬 **{post['title']}**",
        ]
        if post.get("details"):
            lines.append(f"📌 `{post['details']}`")
        if post.get("categories"):
            lines.append(f"🎭 ژانر: {post['categories']}")
        if post.get("summary"):
            lines.append(f"\n📝 _{post['summary']}_")
        lines.append(f"\n🔗 [مشاهده صفحه در سایت]({post['url']})")
        return "\n".join(lines)

    @staticmethod
    def _build_post_kb(post: dict) -> InlineKeyboardMarkup:
        builder = InlineKeyboardBuilder()
        builder.row(InlineKeyboardButton(text="🌐 باز کردن لینک پست", url=post["url"]))
        # نام ایمن برای ساخت سریع پوشه از روی دکمه اعلان
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
        """بررسی هر ۳ سایت و ارسال پست‌های جدید به تلگرام"""
        state = self._load_state()
        new_counts = {"pahe": 0, "psa": 0, "yts": 0}

        for site_key in ("pahe", "psa", "yts"):
            try:
                posts = await self.fetch_site_posts(site_key)
                if not posts:
                    continue

                seen_ids = set(state.get(site_key, []))
                is_first_run = len(seen_ids) == 0

                # از قدیمی‌تر به جدیدتر بررسی شود تا ترتیب پیام‌ها در تلگرام درست باشد
                new_posts = [p for p in reversed(posts) if p["id"] not in seen_ids]

                if is_first_run and not notify_on_first_seed:
                    # در اولین اجرای اولیه، لیست فعلی ذخیره می‌شود تا ۱۰ پیام قدیمی یکجا اسپم نشود
                    state[site_key] = [p["id"] for p in reversed(posts)]
                    # ارسال فقط آخرین پستِ هر سایت به عنوان تایید راه‌اندازی اولیه
                    await self.send_post_notification(bot, posts[0])
                    new_counts[site_key] = 1
                else:
                    for post in new_posts:
                        await self.send_post_notification(bot, post)
                        state.setdefault(site_key, []).append(post["id"])
                        new_counts[site_key] += 1
                        await asyncio.sleep(1)

            except Exception as e:
                logging.warning(f"Monitor error on {site_key}: {e}")

        self._save_state(state)
        return new_counts

    async def _loop(self, bot: Bot):
        self._running = True
        while self._running:
            interval = int(cfg.get("monitor", "interval_seconds", 300))
            if cfg.get("monitor", "enabled", True):
                await self.check_all_sites_once(bot)
            await asyncio.sleep(interval)

    def start(self, bot: Bot):
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop(bot))
            logging.info("Site Monitor started (checking pahe.ink, psa.wf, yts.gg every 5 minutes).")

    def stop(self):
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()


monitor_service = SiteMonitorService()