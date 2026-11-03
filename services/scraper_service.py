import asyncio
import re
import urllib.parse
import xml.etree.ElementTree as ET
from bs4 import BeautifulSoup
import cloudscraper
from core.proxy_manager import ProxyManager


class ScraperService:
    SITE_CATEGORIES = {
        "anime": {
            "nyaa": "Nyaa.land (تورنت انیمه)",
            "subsplease": "SubsPlease.org (API مستقیم انیمه)",
            "kayoanime": "KayoAnime.com (لینک GDrive/Mega)",
        },
        "general": {
            "bt4g": "BT4Gprx.com",
            "eztv": "EZTVx.to (سریال)",
            "uindex": "Uindex.org",
            "watchsomuch": "WatchSoMuch.to",
        },
        "korean": {
            "mkvdrama_net": "MKVDrama.net (کره‌ای - Mega/GDrive)",
            "mkvdrama_top": "MKVDrama.top (کره‌ای - Mega/GDrive)",
        }
    }

    @staticmethod
    def _create_scraper():
        scraper = cloudscraper.create_scraper(
            browser={"browser": "chrome", "platform": "windows", "desktop": True}
        )
        proxies = ProxyManager.get_requests_proxies()
        if proxies:
            scraper.proxies.update(proxies)
        return scraper

    @classmethod
    async def search(cls, site_key: str, query: str) -> list[dict]:
        """فراخوانی غیرمسدودکننده جستجو بر اساس کلید سایت"""
        return await asyncio.to_thread(cls._sync_search, site_key, query)

    @classmethod
    def _sync_search(cls, site_key: str, query: str) -> list[dict]:
        scraper = cls._create_scraper()
        encoded_q = urllib.parse.quote_plus(query)
        results = []

        # ۱. Nyaa.land (RSS XML)
        if site_key == "nyaa":
            url = f"https://nyaa.land/?page=rss&q={encoded_q}&c=0_0&f=0"
            resp = scraper.get(url, timeout=20)
            root = ET.fromstring(resp.text)
            for item in root.findall("./channel/item")[:12]:
                title = item.findtext("title", "No Title")
                info_hash = item.findtext("{https://nyaa.si/xmlns/nyaa}infoHash", "")
                size = item.findtext("{https://nyaa.si/xmlns/nyaa}size", "")
                seeders = item.findtext("{https://nyaa.si/xmlns/nyaa}seeders", "0")
                magnet = f"magnet:?xt=urn:btih:{info_hash}&dn={urllib.parse.quote(title)}"
                results.append({
                    "title": f"{title} [{size} | 🌱{seeders}]",
                    "target": magnet,
                    "type": "magnet"
                })

        # ۲. SubsPlease.org (JSON API)
        elif site_key == "subsplease":
            url = f"https://subsplease.org/api/?f=search&tz=UTC&s={encoded_q}"
            data = scraper.get(url, timeout=20).json()
            if isinstance(data, dict):
                for _, item in list(data.items())[:12]:
                    show_title = item.get("show", "")
                    ep = item.get("episode", "")
                    downloads = item.get("downloads", [])
                    for dl in downloads:
                        res = dl.get("res", "")
                        magnet = dl.get("magnet", "")
                        if magnet:
                            results.append({
                                "title": f"{show_title} - E{ep} [{res}p]",
                                "target": magnet,
                                "type": "magnet"
                            })

        # ۳. EZTVx.to
        elif site_key == "eztv":
            q_slug = query.strip().replace(" ", "-")
            url = f"https://eztvx.to/search/{urllib.parse.quote(q_slug)}"
            resp = scraper.get(url, timeout=20)
            soup = BeautifulSoup(resp.text, "lxml")
            for row in soup.select("tr.forum_header_border")[:12]:
                title_el = row.select_one("a.epinfo")
                magnet_el = row.select_one("a.magnet")
                if title_el and magnet_el:
                    results.append({
                        "title": title_el.get_text(strip=True),
                        "target": magnet_el["href"],
                        "type": "magnet"
                    })

        # ۴. BT4Gprx.com (RSS Mode)
        elif site_key == "bt4g":
            url = f"https://bt4gprx.com/search?q={encoded_q}&p=rss"
            resp = scraper.get(url, timeout=20)
            root = ET.fromstring(resp.text)
            for item in root.findall("./channel/item")[:12]:
                title = item.findtext("title", "")
                link = item.findtext("link", "")
                hash_match = re.search(r"([a-fA-F0-9]{40})", link)
                if hash_match:
                    ih = hash_match.group(1)
                    magnet = f"magnet:?xt=urn:btih:{ih}&dn={urllib.parse.quote(title)}"
                    results.append({"title": title, "target": magnet, "type": "magnet"})

        # ۵. Uindex.org
        elif site_key == "uindex":
            url = f"https://uindex.org/search.php?search={encoded_q}&c=0"
            resp = scraper.get(url, timeout=20)
            soup = BeautifulSoup(resp.text, "lxml")
            for a_tag in soup.select('a[href^="magnet:?"]')[:12]:
                tr = a_tag.find_parent("tr")
                title_text = tr.get_text(" ", strip=True)[:80] if tr else "Uindex Torrent"
                results.append({
                    "title": title_text,
                    "target": a_tag["href"],
                    "type": "magnet"
                })

        # ۶. WatchSoMuch.to
        elif site_key == "watchsomuch":
            url = f"https://watchsomuch.to/Movies/{encoded_q}"
            resp = scraper.get(url, timeout=20)
            soup = BeautifulSoup(resp.text, "lxml")
            for a_tag in soup.select('a[href^="magnet:?"]')[:12]:
                results.append({
                    "title": a_tag.get("title") or a_tag.get_text(strip=True) or query,
                    "target": a_tag["href"],
                    "type": "magnet"
                })

        # ۷. KayoAnime / MKVDrama.net / MKVDrama.top (سایت‌های مبتنی بر صفحات دانلود Mega/GDrive)
        elif site_key in ("kayoanime", "mkvdrama_net", "mkvdrama_top"):
            domains = {
                "kayoanime": "https://kayoanime.com",
                "mkvdrama_net": "https://mkvdrama.net",
                "mkvdrama_top": "https://mkvdrama.top",
            }
            base = domains[site_key]
            resp = scraper.get(f"{base}/?s={encoded_q}", timeout=20)
            soup = BeautifulSoup(resp.text, "lxml")
            for h_tag in soup.select("h2.entry-title a, h3.entry-title a, article h2 a")[:10]:
                results.append({
                    "title": h_tag.get_text(strip=True),
                    "target": h_tag["href"],
                    "type": "page"  # نیاز به استخراج لینک‌های داخل صفحه دارد
                })

        return results

    @classmethod
    async def extract_links_from_page(cls, page_url: str) -> list[dict]:
        """استخراج لینک‌های Mega، Google Drive، Magnet و مستقیم از داخل صفحه یک پست"""
        return await asyncio.to_thread(cls._sync_extract_page, page_url)

    @classmethod
    def _sync_extract_page(cls, page_url: str) -> list[dict]:
        scraper = cls._create_scraper()
        resp = scraper.get(page_url, timeout=20)
        soup = BeautifulSoup(resp.text, "lxml")
        found = []

        for a_tag in soup.find_all("a", href=True):
            href = a_tag["href"].strip()
            label = a_tag.get_text(strip=True) or "Link"
            if any(k in href for k in ["mega.nz", "drive.google.com", "magnet:?", ".mkv", ".mp4"]):
                found.append({"title": f"{label} ({href[:35]}...)", "target": href})
        return found[:20]