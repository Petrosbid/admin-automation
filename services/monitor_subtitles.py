import re
import ssl
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urljoin, urlsplit

import aiohttp
from bs4 import BeautifulSoup
from yarl import URL

from core.config_loader import cfg
from core.proxy_manager import ProxyManager


SUBTITLE_SITES = {"digimovie", "avamovie"}
_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩٫٬", "01234567890123456789.,")
_ORDINALS = {
    name: number for number, name in enumerate(
        "اول دوم سوم چهارم پنجم ششم هفتم هشتم نهم دهم یازدهم دوازدهم سیزدهم چهاردهم "
        "پانزدهم شانزدهم هفدهم هجدهم نوزدهم بیستم".split(), 1
    )
}
_SIZE = re.compile(r"(\d[\d,]*(?:\.\d+)?)\s*(Ki?B|Mi?B|Gi?B|Ti?B|کیلو\s*بایت|مگا\s*بایت|گیگا\s*بایت|ترا\s*بایت)\b", re.I)
_SOFTSUB = re.compile(r"soft[\s._-]*sub|سافت[\s‌-]*ساب|زیرنویس\s*(?:جدا|قابل\s*انتخاب)", re.I)
_HARDSUB = re.compile(r"hard[\s._-]*sub|هارد[\s‌-]*ساب", re.I)
_EPISODE_TAG = re.compile(r"(?<![a-z0-9])S(\d{1,3})[ ._-]*E(\d{1,4})(?!\d|[ ._-]*E\d)", re.I)
_VIDEO_EXTS = {".mkv", ".mp4", ".webm", ".m4v", ".avi", ".mov", ".ts"}


@dataclass(frozen=True)
class SubtitleVideo:
    url: str
    size: int
    season: int = 0
    episode: int = 0


class SubtitleAccessRequired(RuntimeError):
    def __init__(self, message: str, url: str, purchase_url: str = ""):
        super().__init__(message)
        self.url = url
        self.purchase_url = purchase_url


def _number(text: str, label: str) -> int:
    match = re.search(rf"{label}\s*[:：]?\s*([\d]+|[آ-ی]+)", text.translate(_DIGITS))
    if not match:
        return 0
    token = match[1]
    return int(token) if token.isdecimal() else _ORDINALS.get(token, 0)


def notification_episode(text: str) -> tuple[int, int]:
    text = re.sub(r"[\u200c\u200e\u200f]+", " ", text.translate(_DIGITS))
    match = _EPISODE_TAG.search(text)
    if match:
        return int(match[1]), int(match[2])
    return _number(text, "فصل"), _number(text, "قسمت")


def _filesize(node) -> int:
    if node:
        match = _SIZE.search(node.get_text(" ", strip=True).translate(_DIGITS))
        if match:
            unit = match[2].lower()[0]
            power = {"k": 1, "m": 2, "g": 3, "t": 4, "ک": 1, "م": 2, "گ": 3, "ت": 4}[unit]
            return round(float(match[1].replace(",", "")) * 1024 ** power)
    return 0


def _https_url(base: str, href: str) -> str:
    url = urljoin(base, href.strip()) if href.strip() else ""
    parsed = urlsplit(url)
    return url if parsed.scheme == "https" and parsed.hostname and not parsed.username and not parsed.password else ""


def _variant(link, page_url: str, quality: str, size: int, season: int = 0) -> SubtitleVideo | None:
    url = _https_url(page_url, link.get("href", ""))
    if not url or not size:
        return None
    path = unquote(urlsplit(url).path)
    label = link.get_text(" ", strip=True)
    extension = Path(path).suffix.lower()
    download_label = "دانلود" in label or "مستقیم" in label or link.has_attr("download")
    if extension not in _VIDEO_EXTS and (extension or not download_label):
        return None
    if _HARDSUB.search(f"{quality} {path}") or not _SOFTSUB.search(f"{quality} {path}"):
        return None
    episode = 0
    if season:
        tag = _EPISODE_TAG.search(path)
        if tag and int(tag[1]) != season:
            return None
        episode = int(tag[2]) if tag else _number(label, "قسمت")
        if not episode:
            return None
        part = link.find_parent(class_="part_item") or link.parent
        size = _filesize(part.select_one(".size_dl, .size")) or size
    return SubtitleVideo(url, size, season, episode)


def _digimovie_variants(soup, page_url: str, series: bool) -> list[SubtitleVideo]:
    variants = []
    rows = soup.select(".dllinks .item_row_series" if series else ".dllinks .itemdl")
    for row in rows:
        season_header = row.select_one(".head_season")
        season = _number(season_header.get_text(" ", strip=True), "فصل") if season_header else 0
        if series and not season:
            continue
        heading = row.select_one(".head_left_side h3")
        quality = heading.get_text(" ", strip=True) if heading else ""
        size = _filesize(row.select_one(".size_dl"))
        for link in row.select(".parts a.partlink[href]" if series else "a.btn_dl[href]"):
            variant = _variant(link, page_url, quality, size, season)
            if variant:
                variants.append(variant)
    return variants


def _avamovie_variants(soup, page_url: str, series: bool, requested_season: int):
    variants = []
    if series:
        pay = None
        for season_group in soup.select(".dl-item"):
            season_label = season_group.select_one(".item-header .season")
            season = _number(season_label.get_text(" ", strip=True), "فصل") if season_label else 0
            if not season or (requested_season and requested_season != season):
                continue
            pay = pay or season_group.select_one(".series-dl-links .need-vip a.vip[href]")
            for row in season_group.select(".dl-content .card:has(.series-dl-links)"):
                header = row.select_one(".card-header")
                if not header:
                    continue
                quality = header.get_text(" ", strip=True)
                size = _filesize(header.select_one(".size"))
                for link in row.select(".series-dl-links a[href]:not(.vip)"):
                    variant = _variant(link, page_url, quality, size, season)
                    if variant:
                        variants.append(variant)
        return variants, pay

    for row in soup.select(".movie-dl-links .dl-row"):
        heading = row.select_one(".quality strong")
        quality = heading.get_text(" ", strip=True) if heading else ""
        size = _filesize(row.select_one(".size"))
        for link in row.select(".directs a[href]:not(.vip)"):
            variant = _variant(link, page_url, quality, size)
            if variant:
                variants.append(variant)
    return variants, soup.select_one(".movie-dl-links .error-vip a.vip[href]")


def select_subtitle_video(
    site: str, page_html: str, page_url: str, season: int = 0, episode: int = 0,
) -> tuple[SubtitleVideo, str]:
    soup = BeautifulSoup(page_html, "html.parser")
    is_series = urlsplit(page_url).path.startswith(("/serie/", "/series/"))
    if is_series and not season:
        update = soup.select_one(".notif_items .item_notif, .item_info .se")
        if update:
            season, published_episode = notification_episode(update.get_text(" ", strip=True))
            episode = episode or published_episode

    if site == "digimovie":
        variants = _digimovie_variants(soup, page_url, is_series)
        pay = soup.select_one('.dllinks a[href*="/dashboard/subscription"]')
        login = soup.select_one('a[href*="/account/login/"]')
        locked = not variants and bool(pay)
    else:
        variants, pay = _avamovie_variants(soup, page_url, is_series, season)
        login = soup.select_one('a[href*="/sign-in/"]')
        locked = bool(pay)

    purchase_url = _https_url(page_url, pay.get("href", "")) if pay else ""
    if is_series and variants:
        selected_season = season or max(video.season for video in variants)
        variants = [video for video in variants if video.season == selected_season]
        if variants:
            selected_episode = episode or max(video.episode for video in variants)
            variants = [video for video in variants if video.episode == selected_episode]
    if variants:
        return min(variants, key=lambda video: video.size), purchase_url
    if locked:
        login_url = _https_url(page_url, login.get("href", "")) if login else ""
        raise SubtitleAccessRequired(
            "برای فعال شدن لینک دانلود، ابتدا در مرورگر ربات وارد سایت شوید." if login_url else
            "سایت برای فعال شدن لینک دانلود، اشتراک می‌خواهد.",
            login_url or purchase_url, purchase_url,
        )
    if urlsplit(page_url).path.startswith(("/sign-in/", "/account/login/")):
        raise SubtitleAccessRequired("ابتدا در مرورگر ربات وارد سایت شوید.", page_url)
    raise RuntimeError("نسخه سافت‌ساب دارای حجم و لینک دانلود برای این عنوان یا قسمت پیدا نشد.")


async def download_subtitle_video(
    context, url: str, dest_dir: Path, *, referer: str, user_agent: str, purchase_url: str,
    progress_callback=None,
) -> None:
    use_proxy = bool(cfg.get("monitor", "browser_use_proxy", False))
    connector = ProxyManager.get_aiohttp_connector() if use_proxy else None
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    tls.load_default_certs()
    timeout = aiohttp.ClientTimeout(total=None, sock_connect=30, sock_read=60)
    headers = {"Referer": referer.split("?", 1)[0], "User-Agent": user_agent}
    try:
        async with aiohttp.ClientSession(
            connector=connector, trust_env=not use_proxy, timeout=timeout, headers=headers,
        ) as session:
            for _ in range(6):
                cookies = {cookie["name"]: cookie["value"] for cookie in await context.cookies([url])}
                target = URL(url, encoded=url.isascii())
                async with session.get(target, cookies=cookies, allow_redirects=False, ssl=tls) as response:
                    if response.status in {301, 302, 303, 307, 308}:
                        url = _https_url(url, response.headers.get("Location", ""))
                        if not url:
                            raise RuntimeError("نشانی مقصد دانلود معتبر نیست.")
                        continue
                    blocked_response = response.status in {401, 403} or (
                        response.status < 400 and response.content_type in {"text/html", "application/xhtml+xml"}
                    )
                    if blocked_response:
                        raise SubtitleAccessRequired(
                            "سرور فایل دسترسی دانلود را تأیید نکرد.", purchase_url or referer, purchase_url,
                        )
                    response.raise_for_status()
                    filename = response.content_disposition.filename if response.content_disposition else None
                    filename = filename or unquote(urlsplit(url).path.rsplit("/", 1)[-1])
                    filename = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", filename).strip(" .") or "video"
                    if Path(filename).suffix.lower() not in _VIDEO_EXTS:
                        filename += ".mp4" if response.content_type == "video/mp4" else ".mkv"
                    total = response.content_length or 0
                    downloaded = 0
                    last_report = 0.0
                    with (dest_dir / filename).open("wb") as output:
                        async for chunk in response.content.iter_chunked(256 * 1024):
                            output.write(chunk)
                            downloaded += len(chunk)
                            now = time.monotonic()
                            if progress_callback and now - last_report >= 2:
                                percent = f"{downloaded / total:.1%} • " if total else ""
                                progress_callback(f"{percent}{downloaded / 1024 ** 2:.1f} MB")
                                last_report = now
                    if not downloaded:
                        raise RuntimeError("سرور فایل دانلودی خالی برگرداند.")
                    return
            raise RuntimeError("تعداد تغییر مسیرهای دانلود بیش از حد بود.")
    except aiohttp.ClientResponseError as exc:
        raise RuntimeError(f"سرور فایل پاسخ HTTP {exc.status} برگرداند.") from None
    except (aiohttp.ClientError, TimeoutError):
        raise RuntimeError("ارتباط با سرور فایل قطع شد یا زمان دانلود به پایان رسید.") from None
