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
    _episode_re = re.compile(r"(?i)S(\d{1,2})[ ._-]*E(\d{1,3})")

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
                    stream.write(line + "\n")
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
             if re.fullmatch(r"(?i)S(?:eason[ ._-]*)?\d{1,2}", part)),
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

        def esc(value: Any) -> str:
            return html.escape(str(value or "—"))

        def media_key(item: dict[str, Any]) -> tuple[str, str]:
            title = str(item.get("media_title") or "Media").strip()
            episode = str(item.get("episode") or "").strip()
            if not episode:
                match = cls._episode_re.search(str(item.get("file_name") or ""))
                episode = (
                    f"S{int(match.group(1)):02d}E{int(match.group(2)):02d}"
                    if match else "فیلم"
                )
            return title, episode

        def quality_label(item: dict[str, Any]) -> str:
            folder = str(item.get("quality_folder") or "").strip()
            ignored = {"softsub", "hardsub", "srt", "sub"}
            if folder and folder.casefold() not in ignored:
                if re.search(
                    r"(?i)(2160p|1080p|720p|480p|360p|fullhd)",
                    folder,
                ):
                    return folder

            source_text = " ".join(
                str(item.get(key) or "")
                for key in ("file_name", "source", "path")
            )
            match = re.search(
                r"(?i)(?<!\d)(2160p|1080p|720p|480p|360p|fullhd)(?![a-z0-9])",
                source_text,
            )
            if not match:
                return ""
            quality = "FullHD" if match.group(1).lower() == "fullhd" else match.group(1).lower()
            if re.search(r"(?i)(x265|hevc)", source_text):
                quality += "-x265"
            return quality

        def grouped_media(items: list[dict[str, Any]], with_quality: bool) -> dict[tuple[str, str], set[str]]:
            grouped: dict[tuple[str, str], set[str]] = {}
            for item in items:
                key = media_key(item)
                grouped.setdefault(key, set())
                if with_quality:
                    quality = quality_label(item)
                    if quality:
                        grouped[key].add(quality)
            return grouped

        lines = [
            f"📊 <b>گزارش روزانه — {target_day.isoformat()}</b>",
            f"🕘 منطقه زمانی: <code>{esc(str(cls.timezone()))}</code>",
            "",
        ]
        section_count = 0

        qualities = [item for item in events if item.get("event") == "quality_added"]
        quality_groups = grouped_media(qualities, with_quality=True)
        if quality_groups:
            section_count += 1
            lines.append(f"🎞 <b>کیفیت‌های اضافه‌شده ({sum(len(v) for v in quality_groups.values())})</b>")
            for (title, episode), names in sorted(
                quality_groups.items(), key=lambda pair: (pair[0][0].casefold(), pair[0][1])
            )[:45]:
                quality_text = "، ".join(sorted(names, key=str.casefold)) or "کیفیت نامشخص"
                lines.append(
                    f"• {esc(title)} — <code>{esc(episode)}</code>: <b>{esc(quality_text)}</b>"
                )
            lines.append("")

        for event_name, label, emoji in (
            ("softsub_created", "سافت‌ساب‌شده", "🎬"),
            ("hardsub_created", "هاردساب‌شده", "🔥"),
        ):
            selected = [item for item in events if item.get("event") == event_name]
            groups = grouped_media(selected, with_quality=True)
            if groups:
                section_count += 1
                lines.append(f"{emoji} <b>{label} ({len(groups)})</b>")
                for (title, episode), names in sorted(
                    groups.items(), key=lambda pair: (pair[0][0].casefold(), pair[0][1])
                )[:45]:
                    suffix = f" — {', '.join(sorted(names, key=str.casefold))}" if names else ""
                    entry = f"• {esc(title)} — <code>{esc(episode)}</code>"
                    if suffix:
                        entry += esc(suffix)
                    lines.append(entry)
                lines.append("")

        folder_events = [
            item for item in events
            if item.get("event") == "automation_folder_mirrored"
        ]
        folder_groups: dict[tuple[str, str], set[str]] = {}
        folder_counts: dict[tuple[str, str], int] = {}
        for item in folder_events:
            title = str(item.get("media_title") or "Media").strip()
            season = str(item.get("season") or "فصل").strip()
            key = (title, season)
            folder_groups.setdefault(key, set())
            folder_groups[key].update(
                str(name).strip()
                for name in (item.get("folders") or [])
                if str(name).strip()
            )
            folder_counts[key] = max(
                folder_counts.get(key, 0),
                int(item.get("folder_count") or 0),
            )

        if folder_groups:
            section_count += 1
            lines.append(f"📁 <b>پوشه‌های اتوماسیون‌شده ({len(folder_groups)})</b>")
            for (title, season), names in sorted(
                folder_groups.items(), key=lambda pair: (pair[0][0].casefold(), pair[0][1])
            )[:35]:
                details = "، ".join(sorted(names, key=str.casefold))
                if not details:
                    details = f"{folder_counts.get((title, season), 0)} پوشه"
                lines.append(
                    f"• {esc(title)} — <code>{esc(season)}</code>: {esc(details)}"
                )
            lines.append("")

        # Resolve public series/anime index URLs from the configured FTP servers.
        # URLs are omitted when a server has no public base URL configured.
        index_targets: set[tuple[str, str]] = set()
        for item in events:
            category = str(item.get("category") or "").strip().casefold()
            title = str(item.get("media_title") or "").strip()
            if title and category in ("series", "anime"):
                canonical_category = "Anime" if category == "anime" else "Series"
                index_targets.add((canonical_category, title))

        index_lines: list[str] = []
        seen_index_urls: set[str] = set()
        if index_targets:
            try:
                from services.cuteftp_service import CuteFTPService

                servers = CuteFTPService.get_servers()
                for category, title in sorted(
                    index_targets,
                    key=lambda value: (value[0].casefold(), value[1].casefold()),
                ):
                    remote_path = f"/English/{category}/{title}"
                    for server in servers:
                        url = CuteFTPService.build_public_url(server, remote_path)
                        if not url:
                            continue
                        directory_url = url.rstrip("/") + "/"
                        if directory_url in seen_index_urls:
                            continue
                        seen_index_urls.add(directory_url)
                        server_name = esc(server.get("name") or server.get("id") or "سرور")
                        index_lines.append(
                            f"• <b>{esc(title)}</b> — "
                            f'<a href="{html.escape(directory_url, quote=True)}">📂 باز کردن ایندکس ({server_name})</a>'
                        )
                        if len(index_lines) >= 24:
                            break
                    if len(index_lines) >= 24:
                        break
            except Exception:
                logging.exception("Could not build public series index links for daily report")

        if index_lines:
            section_count += 1
            lines.extend([
                "🔗 <b>ایندکس پوشه سریال‌ها و انیمه‌ها</b>",
                *index_lines,
                "",
            ])

        if not section_count:
            lines.append("امروز کیفیت، سافت‌ساب، هاردساب یا پوشه اتوماسیون‌شده‌ای ثبت نشده است.")

        # Keep the daily report short and free of raw filenames, queue errors and monitor diagnostics.
        return "\n".join(lines)