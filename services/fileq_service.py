import asyncio
import logging
import re
from contextlib import AsyncExitStack
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Callable

from yarl import URL

from core.config_loader import cfg
from core.queue_manager import set_download_file_name

try:
    from patchright.async_api import (
        BrowserContext,
        Page,
        TimeoutError as PlaywrightTimeoutError,
        async_playwright,
    )
    _USING_PATCHRIGHT = True
except ImportError:
    from playwright.async_api import (
        BrowserContext,
        Page,
        TimeoutError as PlaywrightTimeoutError,
        async_playwright,
    )
    _USING_PATCHRIGHT = False


_FILEQ_HOSTS = {"fileq.net", "www.fileq.net"}
_start_lock = asyncio.Lock()
_context_lock = asyncio.Lock()
_download_slots = asyncio.Semaphore(3)
_download_button = "form[name='F1'] #downloadbtn"

_shared_playwright_cm = None
_shared_playwright = None
_shared_context: BrowserContext | None = None
_active_transfers = 0


def is_fileq_url(target: str) -> bool:
    try:
        url = URL(target)
        return url.scheme in {"http", "https"} and url.host in _FILEQ_HOSTS and url.user is None
    except ValueError:
        return False


def _profile_dir() -> Path:
    profile = cfg.data_dir / "fileq_browser_profile"
    profile.mkdir(parents=True, exist_ok=True)
    return profile


async def _close_shared_context_unlocked() -> None:
    global _shared_playwright_cm, _shared_playwright, _shared_context
    if _shared_context is not None:
        try:
            await _shared_context.close()
        except Exception:
            pass
        _shared_context = None
    if _shared_playwright_cm is not None:
        try:
            await _shared_playwright_cm.__aexit__(None, None, None)
        except Exception:
            pass
        _shared_playwright_cm = None
        _shared_playwright = None


async def _launch_persistent_browser(playwright) -> BrowserContext:
    profile_dir = str(_profile_dir())
    base_kwargs: dict = {
        "user_data_dir": profile_dir,
        "headless": False,
        "no_viewport": True,
        "accept_downloads": True,
    }
    if not _USING_PATCHRIGHT:
        base_kwargs["ignore_default_args"] = ["--enable-automation"]
        base_kwargs["args"] = [
            "--disable-blink-features=AutomationControlled",
            "--no-first-run",
            "--no-default-browser-check",
        ]

    last_error: Exception | None = None
    for channel in ("chrome", "msedge", None):
        kwargs = dict(base_kwargs)
        if channel is not None:
            kwargs["channel"] = channel
        try:
            context = await playwright.chromium.launch_persistent_context(**kwargs)
            logging.info(
                "FileQ browser started (engine=%s, channel=%s)",
                "patchright" if _USING_PATCHRIGHT else "playwright",
                channel or "chromium",
            )
            return context
        except Exception as exc:
            last_error = exc
            logging.warning(
                "FileQ browser launch failed for channel=%s: %s",
                channel or "chromium",
                exc,
            )

    raise RuntimeError(f"راه‌اندازی مرورگر برای FileQ ناموفق بود: {last_error}") from last_error


async def _acquire_page() -> Page:
    global _shared_playwright_cm, _shared_playwright, _shared_context, _active_transfers

    async with _context_lock:
        if _shared_context is not None:
            try:
                page = await _shared_context.new_page()
                _active_transfers += 1
                return page
            except Exception:
                await _close_shared_context_unlocked()

        _shared_playwright_cm = async_playwright()
        _shared_playwright = await _shared_playwright_cm.__aenter__()
        try:
            _shared_context = await _launch_persistent_browser(_shared_playwright)
            existing_blank = [
                p for p in _shared_context.pages
                if p.url in ("about:blank", "chrome://newtab/", "")
            ]
            page = existing_blank[0] if existing_blank else await _shared_context.new_page()
        except Exception:
            await _close_shared_context_unlocked()
            raise

        _active_transfers += 1
        return page


async def _release_page(page: Page) -> None:
    global _active_transfers
    async with _context_lock:
        _active_transfers = max(0, _active_transfers - 1)
        if _active_transfers == 0:
            await _close_shared_context_unlocked()
        else:
            try:
                if not page.is_closed():
                    await page.close()
            except Exception:
                pass


async def _click_download_when_ready(page: Page) -> None:
    try:
        button = page.locator(_download_button)
        await button.wait_for(timeout=120_000)
        if not is_fileq_url(page.url):
            return

        countdown = button.locator("#seconds")
        if await countdown.count():
            raw_seconds = await countdown.inner_text()
            match = re.search(r"\d+", raw_seconds or "")
            if match:
                await asyncio.sleep(int(match.group(0)) + 0.5)

        await page.wait_for_function(
            """selector => {
                const button = document.querySelector(selector);
                if (!button || button.disabled) return false;
                const form = button.form;
                return !form || !form.querySelector('.cf-turnstile') ||
                    Boolean(form.querySelector('[name="cf-turnstile-response"]')?.value);
            }""",
            arg=_download_button,
            timeout=240_000,
        )
        if not is_fileq_url(page.url):
            return

        await button.click(timeout=10_000)
        direct_link = page.locator(
            "a#direct_link[href], #direct_link a[href], a#downloadbtn[href], a[download]"
        ).first
        await direct_link.wait_for(timeout=15_000)
        if is_fileq_url(page.url):
            await direct_link.click(timeout=10_000)
    except (PlaywrightTimeoutError, asyncio.CancelledError):
        # صفحه باز می‌ماند تا در صورت نیاز کاربر ادامهٔ مراحل سایت را دستی انجام دهد.
        return
    except Exception as exc:
        logging.debug("FileQ auto-click stopped (manual flow still available): %s", exc)
        return


async def download_fileq(
    target: str,
    dest_dir: Path,
    progress_callback: Callable[[str], None] | None = None,
) -> Path:
    if progress_callback:
        progress_callback("در انتظار نوبت FileQ...")

    async with AsyncExitStack() as transfer:
        # تا زمانی که اسلات آزاد نشود و دانلود قبلی شروع نشده باشد، تب جدید باز نمی‌شود.
        async with _start_lock:
            await transfer.enter_async_context(_download_slots)
            temp_dir = transfer.enter_context(TemporaryDirectory(prefix=".fileq-", dir=dest_dir))
            page = await _acquire_page()
            transfer.push_async_callback(_release_page, page)

            if progress_callback:
                progress_callback("FileQ: در صورت نیاز، تأیید کلادفلر یا دکمهٔ دانلود را در مرورگر انجام دهید.")

            auto_click = None
            try:
                await page.bring_to_front()
                async with page.expect_download(timeout=300_000) as download_event:
                    await page.goto(str(URL(target).with_scheme("https")), wait_until="commit")
                    auto_click = asyncio.create_task(_click_download_when_ready(page))
                download = await download_event.value
            except PlaywrightTimeoutError as exc:
                raise RuntimeError(
                    "دانلود FileQ در مهلت تعیین‌شده شروع نشد. پیام سایت را در مرورگر بررسی کنید."
                ) from exc
            finally:
                if auto_click is not None:
                    auto_click.cancel()
                    await asyncio.gather(auto_click, return_exceptions=True)

        filename = Path(download.suggested_filename).name
        set_download_file_name(filename)
        if progress_callback:
            progress_callback("دانلود FileQ آغاز شد. وضعیت انتقال در مرورگر قابل مشاهده است.")
        partial = Path(temp_dir) / "download.part"
        await download.save_as(partial)
        partial.replace(dest_dir / filename)
    return dest_dir