import html
import json
import logging
import re
import threading
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from core.config_loader import cfg


class DailyActivityService:
    """Persistent daily event journal for download, automation, queue and monitor operations."""

    _lock = threading.Lock()
    _episode_re = re.compile(r"(?i)S(\\d{1,2})[ ._-]*E(\\d{1,3})")

    @classmethod
    def timezone(cls):
        name = str(cfg.get("eztv_tracker", "timezone", "Asia/Tehran") or "Asia/Tehran")
        try:
            return ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError):
            return datetime.now().astimezone().tzinfo

    @classmethod
    def _state_dir(cls) -> Path:
        root = Path(cfg.get("paths", "workspace_dir", "./workspace")).resolve() / "_state"
        root.mkdir(parents=True, exist_ok=True)
        return root

    @classmethod
    def _day_file(cls, day: str) -> Path:
        return cls._state_dir() / f"daily_activity_{day}.jsonl"

    @staticmethod
    def _safe(value: Any):
        if isinstance(value, Path):
            return str(value)
        if isinstance(value, datetime):
            return value.isoformat()
        if isinstance(value, (str, int, float, bool)) or value is None:
            return value
        if isinstance(value, (list, tuple, set)):
            return [DailyActivityService._safe(item) for item in value]
        if isinstance(value, dict):
            return {str(key): DailyActivityService._safe(item) for key, item in value.items()}
        return str(value)

    @classmethod
    def record(cls, event: str, **details) -> None:
        try:
            now = datetime.now(timezone.utc)
            local_now = now.astimezone(cls.timezone())
            payload = {
                "timestamp": now.isoformat(),
                "local_time": local_now.strftime("%H:%M:%S"),
                "date": local_now.date().isoformat(),
                "event": str(event),
                **{key: cls._safe(value) for key, value in details.items()},
            }
            path = cls._day_file(payload["date"])
            line = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
            with cls._lock:
                with path.open("a", encoding="utf-8") as stream:
                    stream.write(line + "\\n")
        except Exception:
            logging.exception("Could not write daily activity event %s", event)

    @classmethod
    def record_media_file(
        cls,
        event: str,
        media_path: Path,
        *,
        source: str = "",
        quality_folder: str = "",
        mode: str = "",
    ) -> None:
        path = Path(media_path).resolve()
        parts = list(path.parts)
        season_index = next(
            (i for i, part in enumerate(parts)
             if re.fullmatch(r"(?i)S(?:eason[ ._-]*)?\\d{1,2}", part)),
            None,
        )
        category = "Movies"
        title = path.parent.name
        season = ""
        if season_index is not None:
            season = parts[season_index]
            title = parts[season_index - 1] if season_index > 0 else path.parent.name
            if season_index >= 2 and parts[season_index - 2].casefold() in ("series", "anime"):
                category = parts[season_index - 2]
        else:
            for root_name in ("Movies", "Series", "Anime"):
                if root_name in parts:
                    root_index = parts.index(root_name)
                    if root_index + 1 < len(parts):
                        title = parts[root_index + 1]
                        category = root_name
                    break

        episode_match = cls._episode_re.search(path.name)
        episode = ""
        if episode_match:
            episode = f"S{int(episode_match.group(1)):02d}E{int(episode_match.group(2)):02d}"
        cls.record(
            event,
            category=category,
            media_title=title,
            season=season,
            episode=episode,
            quality_folder=quality_folder or path.parent.name,
            file_name=path.name,
            path=str(path),
            source=source,
            mode=mode,
        )

    @classmethod
    def _read_day(cls, day: str) -> list[dict[str, Any]]:
        path = cls._day_file(day)
        if not path.is_file():
            return []
        events: list[dict[str, Any]] = []
        try:
            with path.open("r", encoding="utf-8") as stream:
                for line in stream:
                    try:
                        item = json.loads(line)
                    except (json.JSONDecodeError, TypeError):
                        continue
                    if isinstance(item, dict):
                        events.append(item)
        except OSError:
            logging.exception("Could not read daily activity file %s", path)
        return events

    @classmethod
    def build_report(cls, report_date: date | str | None = None) -> str:
        if report_date is None:
            target_day = datetime.now(cls.timezone()).date()
        elif isinstance(report_date, date):
            target_day = report_date
        else:
            target_day = date.fromisoformat(str(report_date))
        events = cls._read_day(target_day.isoformat())
        esc = lambda value: html.escape(str(value or "—"))
        lines = [
            f"📊 <b>گزارش جامع عملکرد روزانه — {target_day.isoformat()}</b>",
            f"🕘 منطقه زمانی: <code>{esc(cls.timezone())}</code>",
            f"📌 رویدادهای ثبت‌شده: <b>{len(events)}</b>",
            "",
        ]

        qualities = [item for item in events if item.get("event") == "quality_added"]
        lines.append(f"🎞 <b>کیفیت‌های دانلودشده ({len(qualities)})</b>")
        for item in qualities[:60]:
            lines.append(
                f"• {esc(item.get('media_title'))} — <code>{esc(item.get('episode') or 'فیلم')}</code> "
                f"— <b>{esc(item.get('quality_folder'))}</b>"
            )
            lines.append(f"  <code>{esc(item.get('file_name'))}</code>")
        if not qualities:
            lines.append("• موردی ثبت نشده است.")
        lines.append("")

        for event_name, label, emoji in (
            ("softsub_created", "سافت‌ساب‌های تکمیل‌شده", "🎬"),
            ("hardsub_created", "هاردساب‌ها / انکودهای تکمیل‌شده", "🔥"),
        ):
            selected = [item for item in events if item.get("event") == event_name]
            lines.append(f"{emoji} <b>{label} ({len(selected)})</b>")
            for item in selected[:50]:
                lines.append(
                    f"• {esc(item.get('media_title'))} — <code>{esc(item.get('episode') or 'فیلم')}</code> "
                    f"— <code>{esc(item.get('file_name'))}</code>"
                )
            if not selected:
                lines.append("• موردی ثبت نشده است.")
            lines.append("")

        queued = [item for item in events if item.get("event") == "task_queued"]
        completed = [item for item in events if item.get("event") == "task_completed"]
        failed = [item for item in events if item.get("event") == "task_failed"]
        uploads = [item for item in events if item.get("event") == "upload_completed"]
        lines.append("⚙️ <b>وضعیت صف عملیات</b>")
        lines.append(
            f"• وارد صف: <b>{len(queued)}</b> | تکمیل‌شده: <b>{len(completed)}</b> "
            f"| ناموفق: <b>{len(failed)}</b> | آپلود موفق: <b>{len(uploads)}</b>"
        )
        if completed:
            counts: dict[str, int] = {}
            for item in completed:
                key = str(item.get("queue_type") or "other")
                counts[key] = counts.get(key, 0) + 1
            lines.append(
                "• بر اساس نوع: "
                + " | ".join(f"{esc(key)}: {value}" for key, value in sorted(counts.items()))
            )
        for item in failed[:15]:
            lines.append(f"• ❌ {esc(item.get('title'))}: {esc(item.get('error'))}")
        lines.append("")

        eztv_items = [item for item in events if item.get("event") == "eztv_release_queued"]
        if eztv_items:
            lines.append(f"🧲 <b>انتشارهای EZTV واردشده به صف ({len(eztv_items)})</b>")
            for item in eztv_items[:40]:
                lines.append(
                    f"• {esc(item.get('media_title'))} — <code>{esc(item.get('episode'))}</code> "
                    f"— {esc(item.get('quality'))}"
                )
            lines.append("")

        monitor_checks = [item for item in events if item.get("event") == "monitor_checked"]
        monitor_posts = [item for item in events if item.get("event") == "monitor_post_found"]
        monitor_errors = [item for item in events if item.get("event") == "monitor_error"]
        lines.append("📡 <b>مانیتورینگ سایت‌ها</b>")
        lines.append(
            f"• چرخه‌ها: <b>{len(monitor_checks)}</b> | پست‌های جدید: <b>{len(monitor_posts)}</b> "
            f"| خطاها: <b>{len(monitor_errors)}</b>"
        )
        for item in monitor_posts[:20]:
            lines.append(f"• 🆕 {esc(item.get('site'))}: {esc(item.get('title'))}")
        for item in monitor_errors[:10]:
            lines.append(f"• ⚠️ {esc(item.get('site'))}: {esc(item.get('error'))}")
        lines.append("")

        other_downloads = [item for item in events if item.get("event") == "download_completed"]
        if other_downloads:
            lines.append(f"📥 <b>دانلودهای دیگر ({len(other_downloads)})</b>")
            for item in other_downloads[:25]:
                lines.append(f"• {esc(item.get('media_title'))}: <code>{esc(item.get('file_name'))}</code>")

        if not events:
            lines.append("در این تاریخ رویدادی ثبت نشده است.")
        lines.append("")
        lines.append("<i>گزارش از زمان نصب این نسخه، رویدادهای ثبت‌شده در سیستم را جمع‌آوری می‌کند.</i>")
        return "\\n".join(lines)
