import asyncio
import email.utils
import html
import json
import logging
import re
import statistics
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import aiohttp

from core.config_loader import cfg
from core.proxy_manager import ProxyManager
from core.queue_manager import queue_engine
from services.automation_service import AutomationService
from services.cuteftp_service import CuteFTPService
from services.downloader_service import DownloaderService


class EZTVTrackerService:
    """RSS watcher for explicitly configured shows and expected SxxEyy releases."""

    QUALITY_KEYS = ("fullhd_h264", "480p", "2160p")
    VIDEO_EXTENSIONS = DownloaderService.VIDEO_EXTENSIONS
    MAX_SEEN_IDS = 2500
    MAX_HISTORY_PER_EPISODE = 20
    MAX_STATUS_ITEMS = 20
    WEEKDAY_LABELS = {
        "Monday": "دوشنبه",
        "Tuesday": "سه‌شنبه",
        "Wednesday": "چهارشنبه",
        "Thursday": "پنج‌شنبه",
        "Friday": "جمعه",
        "Saturday": "شنبه",
        "Sunday": "یکشنبه",
    }

    def __init__(self):
        self._task: asyncio.Task | None = None
        self._running = False
        self._poll_lock = asyncio.Lock()
        self._last_error_notice = 0.0
        self._last_cycle_message = 0.0
        self._remote_quality_cache: dict[str, tuple[float, set[str]]] = {}

    @staticmethod
    def _workspace() -> Path:
        return Path(cfg.get("paths", "workspace_dir", "./workspace")).resolve()

    @property
    def state_file(self) -> Path:
        path = self._workspace() / "_state" / "eztv_tracker_state.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def managed_shows_file(self) -> Path:
        """Managed show list is stored separately so Telegram edits never rewrite config.yaml."""
        path = self._workspace() / "_state" / "eztv_tracker_shows.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def get_show_entries(self) -> list[dict[str, Any]]:
        if self.managed_shows_file.exists():
            try:
                payload = json.loads(self.managed_shows_file.read_text(encoding="utf-8"))
                if isinstance(payload, list):
                    return [dict(item) for item in payload if isinstance(item, dict)]
            except Exception:
                logging.exception("Could not read managed EZTV show list")
        raw = self.get_config().get("shows", [])
        return [dict(item) for item in raw if isinstance(item, dict)] if isinstance(raw, list) else []

    def save_show_entries(self, entries: list[dict[str, Any]]) -> None:
        if not isinstance(entries, list):
            raise TypeError("فهرست سریال‌ها باید یک لیست باشد.")
        temp = self.managed_shows_file.with_suffix(".tmp")
        temp.write_text(
            json.dumps(entries, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temp.replace(self.managed_shows_file)

    def migrate_show_state(
        self,
        old_show: dict[str, Any],
        new_show: dict[str, Any],
    ) -> None:
        """Keep release history when a title is renamed without resetting automatic progress."""
        state = self._load_state()
        old_normalized = dict(old_show)
        old_normalized["category"] = str(old_normalized.get("category", "Series")).title()
        old_normalized["title"] = str(old_normalized.get("title", "")).strip()
        new_normalized = dict(new_show)
        new_normalized["category"] = str(new_normalized.get("category", "Series")).title()
        new_normalized["title"] = str(new_normalized.get("title", "")).strip()

        old_state_key = self._show_state_key(old_normalized)
        new_state_key = self._show_state_key(new_normalized)
        old_season = int(old_normalized.get("season", 1))
        new_season = int(new_normalized.get("season", 1))
        old_category = old_normalized["category"]
        new_category = new_normalized["category"]
        old_episode_setting = int(old_normalized.get("next_episode", 1))
        new_episode_setting = int(new_normalized.get("next_episode", 1))

        progress = state.setdefault("progress", {})
        if old_state_key != new_state_key and old_state_key in progress:
            old_progress = progress.pop(old_state_key)
            progress[new_state_key] = old_progress

        # Renaming a show can safely migrate history within the same season.
        # Changing season/category starts a different tracking scope, so do not
        # re-label old season release dates or queued episode tasks as new ones.
        same_tracking_scope = old_season == new_season and old_category == new_category
        if old_state_key != new_state_key and same_tracking_scope:
            for section in ("release_history", "rss_matches"):
                values = state.setdefault(section, {})
                for key in list(values):
                    if str(key).startswith(old_state_key + "|"):
                        suffix = str(key)[len(old_state_key):]
                        values[new_state_key + suffix] = values.pop(key)

            old_task_prefix = (
                f"{old_category}|{self._slug(old_normalized['title'])}|S{old_season:02d}E"
            )
            new_task_prefix = (
                f"{new_category}|{self._slug(new_normalized['title'])}|S{new_season:02d}E"
            )
            scheduled = state.setdefault("scheduled_tasks", {})
            for key in list(scheduled):
                if str(key).startswith(old_task_prefix):
                    suffix = str(key)[len(old_task_prefix):]
                    migrated = scheduled.pop(key)
                    if isinstance(migrated, dict):
                        migrated["show_title"] = new_normalized["title"]
                    new_key = new_task_prefix + suffix
                    scheduled[new_key] = migrated

        progress_item = progress.setdefault(new_state_key, {})
        episode_setting_changed = (
            old_episode_setting != new_episode_setting
            or old_season != new_season
            or old_category != new_category
        )
        if episode_setting_changed or "next_episode" not in progress_item:
            progress_item["next_episode"] = new_episode_setting
            progress_item["config_next_episode"] = new_episode_setting
        else:
            # Preserve the auto-advanced next_episode when only weekday, aliases,
            # quality folders or other descriptive metadata were edited.
            progress_item.setdefault("config_next_episode", new_episode_setting)

        self._save_state(state)

    @classmethod
    def weekday_label(cls, value: str | None) -> str:
        key = str(value or "").strip().title()
        return cls.WEEKDAY_LABELS.get(key, "نامشخص")

    def observed_release_weekdays(
        self,
        show: dict[str, Any],
        state: dict[str, Any] | None = None,
    ) -> list[str]:
        state = state or self._load_state()
        identity_prefix = (
            f"{show['category']}|{self._slug(show['title'])}|S"
        )
        counts: dict[str, int] = {}
        for key, timestamps in state.get("release_history", {}).items():
            if not str(key).startswith(identity_prefix) or not isinstance(timestamps, list):
                continue
            for raw in timestamps:
                parsed = self._parse_datetime(str(raw))
                if not parsed:
                    continue
                day = parsed.astimezone(self._timezone()).strftime("%A")
                counts[day] = counts.get(day, 0) + 1
        if not counts:
            return []
        maximum = max(counts.values())
        ordered_days = [
            day for day in self.WEEKDAY_LABELS
            if counts.get(day) == maximum
        ]
        return ordered_days

    def _timezone(self) -> ZoneInfo:
        tz_name = str(cfg.get("eztv_tracker", "timezone", "Asia/Tehran") or "Asia/Tehran")
        try:
            return ZoneInfo(tz_name)
        except ZoneInfoNotFoundError:
            logging.warning("Unknown EZTV tracker timezone %r; falling back to Asia/Tehran", tz_name)
            return ZoneInfo("Asia/Tehran")

    def _load_state(self) -> dict[str, Any]:
        default = {
            "seen_item_ids": [],
            "progress": {},
            "release_history": {},
            "rss_matches": {},
            "scheduled_tasks": {},
            "last_daily_report_date": "",
            "last_feed_check": "",
        }
        if not self.state_file.exists():
            return default
        try:
            loaded = json.loads(self.state_file.read_text(encoding="utf-8"))
            if not isinstance(loaded, dict):
                return default
            for key, value in default.items():
                if key not in loaded or not isinstance(loaded[key], type(value)):
                    loaded[key] = value
            return loaded
        except Exception:
            logging.exception("Could not read EZTV tracker state; starting with empty state")
            return default

    def _save_state(self, state: dict[str, Any]) -> None:
        temp_path = self.state_file.with_suffix(".tmp")
        try:
            temp_path.write_text(
                json.dumps(state, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            temp_path.replace(self.state_file)
        except Exception:
            logging.exception("Could not save EZTV tracker state")

    def get_config(self) -> dict[str, Any]:
        raw = cfg.get("eztv_tracker") or {}
        return raw if isinstance(raw, dict) else {}

    def get_shows(self) -> list[dict[str, Any]]:
        raw_shows = self.get_show_entries()
        if not isinstance(raw_shows, list):
            return []
        result: list[dict[str, Any]] = []
        for raw in raw_shows:
            if not isinstance(raw, dict):
                continue
            title = str(raw.get("title", "")).strip()
            try:
                season = int(raw.get("season", 0))
                configured_episode = int(raw.get("next_episode", 1))
            except (TypeError, ValueError):
                continue
            if not title or season < 1 or configured_episode < 1:
                continue
            category = str(raw.get("category", "Series")).strip().title()
            if category not in ("Series", "Anime"):
                category = "Series"
            qualities = [
                str(q).strip().casefold()
                for q in raw.get("qualities", [])
                if str(q).strip().casefold() in self.QUALITY_KEYS
            ] if isinstance(raw.get("qualities", []), list) else []
            if not qualities:
                continue
            result.append({
                **raw,
                "title": title,
                "season": season,
                "next_episode": configured_episode,
                "category": category,
                "qualities": list(dict.fromkeys(qualities)),
                "aliases": [str(x).strip() for x in raw.get("aliases", []) if str(x).strip()]
                    if isinstance(raw.get("aliases", []), list) else [],
                "release_day": str(raw.get("release_day", "") or "").strip().title(),
                "quality_folders": dict(raw.get("quality_folders", {}))
                    if isinstance(raw.get("quality_folders", {}), dict) else {},
            })
        return result

    @staticmethod
    def _slug(value: str) -> str:
        return re.sub(r"[^a-z0-9]+", "", str(value).casefold())

    @classmethod
    def _show_state_key(cls, show: dict[str, Any]) -> str:
        return f"{show['category']}|{cls._slug(show['title'])}|S{show['season']:02d}"

    @staticmethod
    def _episode_tag(season: int, episode: int) -> str:
        return f"S{int(season):02d}E{int(episode):02d}"

    @staticmethod
    def _release_episode(title: str) -> tuple[int, int] | None:
        raw_title = title or ""
        # Reject common multi-episode packs such as S02E04-E05, S02E04E05 and S02E04+05.
        if re.search(
            r"(?i)\bS\d{1,2}E\d{1,3}(?:[-+._ &]+E?\d{1,3}|E\d{1,3})\b",
            raw_title,
        ):
            return None
        patterns = (
            re.compile(r"(?<![A-Z0-9])S(?P<s>\d{1,2})[\s._-]*E(?P<e>\d{1,3})(?!\d)", re.I),
            re.compile(r"(?<!\d)(?P<s>\d{1,2})[xX](?P<e>\d{1,3})(?!\d)"),
        )
        matches = []
        for pattern in patterns:
            matches.extend(pattern.finditer(raw_title))
        if len(matches) != 1:
            return None
        return int(matches[0].group("s")), int(matches[0].group("e"))

    @staticmethod
    def _parse_datetime(raw: str) -> datetime | None:
        value = str(raw or "").strip()
        if not value:
            return None
        try:
            parsed = email.utils.parsedate_to_datetime(value)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc)
        except Exception:
            pass
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc)
        except Exception:
            return None

    @staticmethod
    def _local_tag(tag: str) -> str:
        return str(tag).split("}")[-1].split(":")[-1].casefold()

    @classmethod
    def _child_text(cls, element: ET.Element, names: set[str]) -> str:
        for child in list(element):
            if cls._local_tag(child.tag) in names:
                text_value = (child.text or "").strip()
                if text_value:
                    return text_value
                href = child.attrib.get("href", "").strip()
                if href:
                    return href
        return ""

    @classmethod
    def _parse_feed(cls, xml_text: str) -> list[dict[str, Any]]:
        root = ET.fromstring(xml_text)
        entries = [
            node for node in root.iter()
            if cls._local_tag(node.tag) in ("item", "entry")
        ]
        parsed: list[dict[str, Any]] = []
        for item in entries:
            title = cls._child_text(item, {"title"})
            if not title:
                continue

            item_link = ""
            magnet_link = ""
            enclosure_link = ""
            guid = ""
            pubdate_raw = ""
            for child in list(item):
                key = cls._local_tag(child.tag)
                value = (child.text or "").strip()
                href = child.attrib.get("href", "").strip()
                if key == "link" and (value or href):
                    candidate = value or href
                    if candidate.lower().startswith("magnet:"):
                        magnet_link = candidate
                    elif not item_link:
                        item_link = candidate
                elif key in ("magneturi", "magnet_uri"):
                    magnet_link = value or href or magnet_link
                elif key == "enclosure":
                    enclosure_link = child.attrib.get("url", "").strip() or value
                elif key in ("guid", "id"):
                    guid = value or href
                elif key in ("pubdate", "published", "updated", "date"):
                    pubdate_raw = value

            link = magnet_link or (
                item_link if item_link.lower().startswith("magnet:")
                else enclosure_link or item_link
            )
            if not magnet_link and guid.lower().startswith("magnet:"):
                magnet_link = guid
            if magnet_link:
                link = magnet_link
            item_id = guid or magnet_link or item_link or f"{title}|{pubdate_raw}"
            if not link:
                # Some RSS pages expose the magnet URI only in an attribute.
                for child in item.iter():
                    for attr_name, attr_value in child.attrib.items():
                        if attr_name.casefold().endswith("magneturi") and attr_value.strip():
                            link = attr_value.strip()
                            break
                    if link:
                        break
            if not link:
                continue

            parsed.append({
                "id": item_id,
                "title": title,
                "link": link,
                "published_at": cls._parse_datetime(pubdate_raw),
                "published_raw": pubdate_raw,
            })
        return parsed

    async def _fetch_feed(self) -> list[dict[str, Any]]:
        feed_url = str(
            self.get_config().get("feed_url", "https://myrss.org/eztv")
            or "https://myrss.org/eztv"
        ).strip()
        timeout = aiohttp.ClientTimeout(total=45, connect=20)
        headers = {
            "User-Agent": "Mozilla/5.0 (compatible; AdminAutomationEZTV/1.0)",
            "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*",
        }
        connector = ProxyManager.get_aiohttp_connector()
        async with aiohttp.ClientSession(
            timeout=timeout,
            headers=headers,
            connector=connector,
        ) as session:
            async with session.get(feed_url) as response:
                response.raise_for_status()
                body = await response.text(errors="replace")
        if not body.strip():
            raise RuntimeError("EZTV RSS feed returned an empty response.")
        return self._parse_feed(body)

    @classmethod
    def _matches_show(cls, release_title: str, show: dict[str, Any]) -> bool:
        normalized_title = re.sub(r"[^a-z0-9]+", " ", release_title.casefold()).strip()
        candidates = [show["title"], *show.get("aliases", [])]
        for candidate in candidates:
            normalized_candidate = re.sub(r"[^a-z0-9]+", " ", candidate.casefold()).strip()
            if normalized_candidate and re.search(
                r"(?<![a-z0-9])" + re.escape(normalized_candidate) + r"(?![a-z0-9])",
                normalized_title,
            ):
                return True
        return False

    @staticmethod
    def _quality_matches(release_title: str, quality: str) -> bool:
        title = str(release_title or "").casefold()
        if quality == "fullhd_h264":
            has_fullhd = bool(re.search(r"(?<!\d)(?:1080p|full[ ._-]?hd|fhd)(?![a-z0-9])", title))
            has_h264 = bool(re.search(r"(?:h[ ._-]?264|x264|avc)", title))
            return has_fullhd and has_h264
        if quality == "480p":
            return bool(re.search(r"(?<!\d)480p(?!\d)", title))
        if quality == "2160p":
            return bool(re.search(r"(?<!\d)2160p(?!\d)", title))
        return False

    def _episode_progress(self, show: dict[str, Any], state: dict[str, Any]) -> int:
        key = self._show_state_key(show)
        progress = state.setdefault("progress", {})
        item = progress.setdefault(key, {})
        try:
            configured_episode = int(show["next_episode"])
        except (TypeError, ValueError):
            configured_episode = 1

        # اگر ادمین next_episode را در config تغییر دهد، مقدار ذخیره‌شدهٔ قبلی
        # را با مقدار جدید پیکربندی همگام کن؛ پیشرفت خودکار بعدی حفظ می‌شود.
        try:
            config_anchor = int(item.get("config_next_episode", configured_episode))
        except (TypeError, ValueError):
            config_anchor = configured_episode
        if config_anchor != configured_episode:
            item["next_episode"] = configured_episode
            item["config_next_episode"] = configured_episode

        try:
            current = int(item.get("next_episode", configured_episode))
        except (TypeError, ValueError):
            current = configured_episode
        item.setdefault("config_next_episode", configured_episode)
        return max(1, current)

    def _show_folder(self, show: dict[str, Any], episode: int) -> Path:
        safe_title = re.sub(r'[<>:"/\\|?*]', "", show["title"]).strip().rstrip(". ")
        if not safe_title:
            safe_title = self._slug(show["title"]) or "Series"
        return (
            self._workspace()
            / show["category"]
            / safe_title
            / f"S{show['season']:02d}"
        ).resolve()

    async def _prepare_season(self, show: dict[str, Any], episode: int) -> Path:
        season_dir = self._show_folder(show, episode)
        season_dir.mkdir(parents=True, exist_ok=True)
        await AutomationService.create_season_structure_from_server(
            season_dir,
            category=show["category"],
        )
        return season_dir

    async def _remote_quality_directories(self, season_dir: Path) -> set[str]:
        remote_path = AutomationService.compute_server_remote_dir_for_local_path(season_dir)
        cache_key = remote_path.casefold()
        cached = self._remote_quality_cache.get(cache_key)
        now = time.monotonic()
        if cached and now - cached[0] < 60:
            return set(cached[1])

        try:
            info = await CuteFTPService.inspect_remote_dir(remote_path)
            directories = {
                str(value).casefold()
                for value in (info.get("dirs", []) or [])
                if str(value).strip()
            }
            self._remote_quality_cache[cache_key] = (now, directories)
            return set(directories)
        except Exception:
            logging.exception("Could not inspect remote EZTV season folder %s", remote_path)
            if cached:
                return set(cached[1])
            return set()

    def _quality_folder_candidates(self, show: dict[str, Any], quality: str) -> list[str]:
        configured = show.get("quality_folders", {})
        if isinstance(configured, dict) and configured.get(quality):
            value = configured[quality]
            if isinstance(value, str):
                return [value]
            if isinstance(value, list):
                return [str(item) for item in value if str(item).strip()]

        if quality == "fullhd_h264":
            return ["FullHD"]
        if quality == "480p":
            return ["480p-RMTeam", "480p-EBTV"]
        if quality == "2160p":
            return ["2160p-HDR-x265"]
        return []

    async def _resolve_quality_folder(
        self,
        show: dict[str, Any],
        season_dir: Path,
        quality: str,
    ) -> Path:
        candidates = self._quality_folder_candidates(show, quality)
        remote_dirs = await self._remote_quality_directories(season_dir)
        local_dirs = {
            child.name.casefold()
            for child in season_dir.iterdir()
            if child.is_dir()
        }
        for name in candidates:
            if name.casefold() in local_dirs or name.casefold() in remote_dirs:
                path = season_dir / name
                path.mkdir(parents=True, exist_ok=True)
                return path

        # FullHD source and 2160p-HDR-x265 are explicit configured targets.
        # 480p follows the destination's existing RMTeam/EBTV folder if possible.
        if quality in ("fullhd_h264", "2160p"):
            path = season_dir / candidates[0]
            path.mkdir(parents=True, exist_ok=True)
            return path
        if quality == "480p":
            path = season_dir / candidates[0]
            path.mkdir(parents=True, exist_ok=True)
            return path
        raise RuntimeError(f"No destination folder configured for quality {quality}")

    @staticmethod
    def _find_episode_files(folder: Path, season: int, episode: int) -> list[Path]:
        if not folder.exists():
            return []
        expected = f"S{season:02d}E{episode:02d}"
        found: list[Path] = []
        for path in folder.rglob("*"):
            if not path.is_file() or path.suffix.casefold() not in DownloaderService.VIDEO_EXTENSIONS:
                continue
            tag = AutomationService.extract_episode_tag(path.name, fallback_season=season)
            if tag and tag.upper() == expected:
                found.append(path.resolve())
        return found

    @classmethod
    def _listed_episode_files(cls, names: list[str], season: int, episode: int) -> list[str]:
        expected = f"S{season:02d}E{episode:02d}"
        found: list[str] = []
        for raw_name in names or []:
            name = Path(str(raw_name)).name
            if Path(name).suffix.casefold() not in cls.VIDEO_EXTENSIONS:
                continue
            tag = AutomationService.extract_episode_tag(name, fallback_season=season)
            if tag and tag.upper() == expected:
                found.append(name)
        return found

    async def _inspect_remote_directory(
        self,
        remote_dir: str,
        refresh: bool = False,
    ) -> dict[str, Any]:
        cache_key = str(remote_dir or "").casefold()
        now = time.monotonic()
        cache = getattr(self, "_remote_listing_cache", None)
        if not isinstance(cache, dict):
            cache = {}
            self._remote_listing_cache = cache

        cached = cache.get(cache_key)
        if not refresh and cached and now - cached[0] < 20:
            return dict(cached[1])
        try:
            info = await CuteFTPService.inspect_remote_dir(remote_dir)
        except Exception as exc:
            logging.warning("Remote EZTV inspection failed for %s: %s", remote_dir, exc)
            info = {
                "exists": False,
                "checked": False,
                "files": [],
                "dirs": [],
                "error": str(exc),
            }
        cache[cache_key] = (now, dict(info))
        return dict(info)

    async def _episode_folder_presence(
        self,
        folder: Path,
        season: int,
        episode: int,
        refresh_remote: bool = False,
    ) -> tuple[list[Path], list[str], dict[str, Any]]:
        local_files = self._find_episode_files(folder, season, episode)
        remote_path = AutomationService.compute_server_remote_dir_for_local_path(folder)
        remote_info = await self._inspect_remote_directory(remote_path, refresh=refresh_remote)
        remote_files = self._listed_episode_files(
            list(remote_info.get("files", []) or []),
            season,
            episode,
        )
        return local_files, remote_files, remote_info

    @staticmethod
    def _is_quality_folder_name(name: str) -> bool:
        low = str(name or "").casefold()
        if (
            low in {
                "srt", "sub", "softsub", "hardsub",
                "_incoming_downloads", "_rejected_downloads",
            }
            or low.endswith(("-softsub", "-hardsub"))
        ):
            return False
        return bool(
            re.search(r"(?<!\d)(?:2160p|4k|uhd|1080p|fullhd|fhd|720p|480p|360p|240p)(?!\d)", low)
        )

    def _today_episodes_for_show(
        self,
        show: dict[str, Any],
        state: dict[str, Any],
        local_date,
    ) -> list[int]:
        prefix = self._show_state_key(show) + "|"
        scheduled_prefix = f"{show['category']}|{self._slug(show['title'])}|"
        episodes: set[int] = set()

        def is_today(value: Any) -> bool:
            parsed = self._parse_datetime(str(value or ""))
            return bool(parsed and parsed.astimezone(self._timezone()).date() == local_date)

        def collect(key: str, stamps: list[Any]):
            if not str(key).startswith(prefix):
                return
            match = re.search(r"\|S(\d{2})E(\d{2})(?:\||$)", str(key), re.IGNORECASE)
            if not match or int(match.group(1)) != int(show["season"]):
                return
            if any(is_today(stamp) for stamp in stamps):
                episodes.add(int(match.group(2)))

        for key, stamps in state.get("release_history", {}).items():
            collect(key, stamps if isinstance(stamps, list) else [])
        for key, item in state.get("rss_matches", {}).items():
            if isinstance(item, dict):
                collect(key, [
                    item.get("first_seen_at"),
                    item.get("published_at"),
                    item.get("updated_at"),
                ])
        for key, item in state.get("scheduled_tasks", {}).items():
            if not isinstance(item, dict) or not str(key).startswith(scheduled_prefix):
                continue
            match = re.search(r"\|S(\d{2})E(\d{2})(?:\||$)", str(key), re.IGNORECASE)
            if not match or int(match.group(1)) != int(show["season"]):
                continue

            if any(is_today(stamp) for stamp in (item.get("queued_at"), item.get("finished_at"))):
                episodes.add(int(match.group(2)))
                continue

            # A task started on an earlier day may finish today before the next RSS poll.
            task = queue_engine.tasks.get(str(item.get("task_id") or ""))
            finished_ts = float(getattr(task, "finished_ts", 0) or 0) if task else 0.0
            if finished_ts > 0:
                finished_at = datetime.fromtimestamp(finished_ts, tz=timezone.utc)
                if finished_at.astimezone(self._timezone()).date() == local_date:
                    episodes.add(int(match.group(2)))

        return sorted(episodes)

    def _release_history_key(self, show: dict[str, Any], episode: int) -> str:
        return f"{self._show_state_key(show)}|{self._episode_tag(show['season'], episode)}"

    def _record_release_time(
        self,
        state: dict[str, Any],
        show: dict[str, Any],
        episode: int,
        published_at: datetime | None,
    ) -> None:
        if not published_at:
            return
        history = state.setdefault("release_history", {})
        key = self._release_history_key(show, episode)
        values = history.setdefault(key, [])
        stamp = published_at.astimezone(timezone.utc).isoformat()
        if stamp not in values:
            values.append(stamp)
        history[key] = values[-self.MAX_HISTORY_PER_EPISODE:]

    @staticmethod
    def _scheduled_key(show: dict[str, Any], episode: int, quality: str) -> str:
        return f"{show['category']}|{EZTVTrackerService._slug(show['title'])}|S{show['season']:02d}E{episode:02d}|{quality}"

    def _task_is_active(self, task_id: str) -> bool:
        task = queue_engine.tasks.get(str(task_id))
        return bool(task and not task.is_finished and not task.is_cancelled)

    async def _download_release_job(
        self,
        bot,
        chat_id: int,
        release_link: str,
        destination: Path,
        expected_episode_tag: str,
        progress_callback=None,
    ):
        # Import lazily to avoid creating a router/service import cycle.
        from bot.download_handlers import _smart_download_pipeline_job
        return await _smart_download_pipeline_job(
            bot,
            chat_id,
            release_link,
            destination,
            progress_callback=progress_callback,
            automated_background=True,
            expected_episode_tag=expected_episode_tag,
        )

    async def _queue_release(
        self,
        bot,
        show: dict[str, Any],
        episode: int,
        quality: str,
        release: dict[str, Any],
        destination: Path,
        state: dict[str, Any],
    ) -> str | None:
        key = self._scheduled_key(show, episode, quality)
        scheduled = state.setdefault("scheduled_tasks", {})
        previous = scheduled.get(key, {})
        previous_id = str(previous.get("task_id", "")) if isinstance(previous, dict) else str(previous)
        if previous_id and self._task_is_active(previous_id):
            return None
        if isinstance(previous, dict) and previous.get("queued_at"):
            try:
                previous_queued_at = self._parse_datetime(str(previous["queued_at"]))
                if previous_queued_at and (
                    datetime.now(timezone.utc) - previous_queued_at
                ).total_seconds() < 900:
                    return None
            except Exception:
                pass

        local_existing, remote_existing, _ = await self._episode_folder_presence(
            destination, show["season"], episode
        )
        if local_existing or remote_existing:
            scheduled.pop(key, None)
            return None

        admin_ids = cfg.get("telegram", "admin_ids", []) or []
        if not admin_ids:
            logging.warning("EZTV tracker found a release but no Telegram admin_ids are configured.")
            return None
        chat_id = int(admin_ids[0])

        queue_task = await queue_engine.add_task(
            "download",
            f"EZTV [{show['title']} {self._episode_tag(show['season'], episode)} {quality}]",
            self._download_release_job,
            bot,
            chat_id,
            release["link"],
            destination,
            self._episode_tag(show["season"], episode),
            notify_completion=False,
        )
        scheduled[key] = {
            "task_id": queue_task.task_id,
            "release_id": release["id"],
            "show_title": show["title"],
            "episode_tag": self._episode_tag(show["season"], episode),
            "quality": quality,
            "queued_at": datetime.now(timezone.utc).isoformat(),
            "reported": False,
        }
        return queue_task.task_id

    def _collect_finished_task_updates(self, state: dict[str, Any]) -> list[dict[str, str]]:
        """Collect terminal download statuses once; one poll produces at most one admin summary."""
        updates: list[dict[str, str]] = []
        scheduled = state.setdefault("scheduled_tasks", {})
        now = datetime.now(timezone.utc).isoformat()

        for key, item in list(scheduled.items()):
            if not isinstance(item, dict):
                continue
            if item.get("reported"):
                continue
            task_id = str(item.get("task_id", "")).strip()
            if not task_id:
                continue
            task = queue_engine.tasks.get(task_id)
            if not task or not task.is_finished:
                continue

            status = str(task.status or "")
            is_success = "تکمیل" in status or status.startswith("✅")
            show = str(item.get("show_title") or (key.split("|")[1] if "|" in key else "سریال"))
            episode_match = re.search(r"(S\d{2}E\d{2})", key, flags=re.IGNORECASE)
            episode_tag = str(item.get("episode_tag") or (
                episode_match.group(1).upper() if episode_match else "قسمت"
            ))
            quality = str(item.get("quality") or key.rsplit("|", 1)[-1])

            item["reported"] = True
            item["finished_at"] = now
            item["result"] = "success" if is_success else "failed"
            item["final_status"] = status
            if not is_success:
                # بعد از خطا، ۱۵ دقیقه cooldown از زمان پایان محاسبه شود تا retry storm ایجاد نشود.
                item["queued_at"] = now

            updates.append({
                "show": show,
                "episode": episode_tag,
                "quality": quality,
                "task_id": task_id,
                "result": "success" if is_success else "failed",
                "detail": str(task.error or task.progress or status)[:180],
            })

        return updates

    async def poll_once(self, bot, send_summary: bool = True) -> dict[str, Any]:
        async with self._poll_lock:
            shows = self.get_shows()
            if not shows:
                return {"shows": 0, "queued": [], "message": "لیست سریال‌های EZTV هنوز تنظیم نشده است."}

            releases = await self._fetch_feed()
            state = self._load_state()
            seen_ids = set(str(x) for x in state.get("seen_item_ids", []))
            added_ids: list[str] = []
            completed_updates = self._collect_finished_task_updates(state)
            queued: list[dict[str, str]] = []
            already_local: list[str] = []
            already_remote: list[str] = []
            current_time = datetime.now(timezone.utc)

            for show in shows:
                episode = self._episode_progress(show, state)
                expected_tag = self._episode_tag(show["season"], episode)
                season_dir = await self._prepare_season(show, episode)

                for release in releases:
                    release_id = str(release["id"])
                    parsed_episode = self._release_episode(release["title"])
                    if parsed_episode != (show["season"], episode):
                        continue
                    if not self._matches_show(release["title"], show):
                        continue

                    matching_qualities = [
                        quality for quality in show["qualities"]
                        if self._quality_matches(release["title"], quality)
                    ]
                    if not matching_qualities:
                        continue

                    self._record_release_time(
                        state, show, episode, release.get("published_at")
                    )
                    rss_matches = state.setdefault("rss_matches", {})
                    episode_history_key = self._release_history_key(show, episode)
                    rss_matches.setdefault(episode_history_key, {
                        "first_seen_at": current_time.isoformat(),
                        "release_id": release_id,
                        "release_title": release["title"],
                    })
                    for quality in matching_qualities:
                        destination = await self._resolve_quality_folder(
                            show, season_dir, quality
                        )
                        local_files, remote_files, _ = await self._episode_folder_presence(
                            destination, show["season"], episode
                        )
                        if local_files or remote_files:
                            location = (
                                "Workspace و سرور مقصد"
                                if local_files and remote_files
                                else "Workspace"
                                if local_files
                                else "سرور مقصد"
                            )
                            note = f"{show['title']} {expected_tag} {quality}: موجود ({location})"
                            if local_files:
                                already_local.append(note)
                            if remote_files:
                                already_remote.append(note)
                            continue

                        task_id = await self._queue_release(
                            bot,
                            show,
                            episode,
                            quality,
                            release,
                            destination,
                            state,
                        )
                        if task_id:
                            queued.append({
                                "show": show["title"],
                                "episode": expected_tag,
                                "quality": quality,
                                "task_id": task_id,
                            })
                    if release_id not in seen_ids:
                        seen_ids.add(release_id)
                        added_ids.append(release_id)

                # If the expected episode is already fully processed locally, progress safely.
                report = await self._episode_status(show, episode, season_dir)
                if report["complete"]:
                    state.setdefault("progress", {}).setdefault(
                        self._show_state_key(show), {}
                    )["next_episode"] = episode + 1
                    for quality in show["qualities"]:
                        state.setdefault("scheduled_tasks", {}).pop(
                            self._scheduled_key(show, episode, quality), None
                        )

            state["seen_item_ids"] = list(seen_ids)[-self.MAX_SEEN_IDS:]
            state["last_feed_check"] = current_time.isoformat()
            self._save_state(state)

            if send_summary and (
                queued or completed_updates or ((already_local or already_remote) and added_ids)
            ):
                lines = ["📡 <b>گزارش خودکار EZTV</b>"]
                if queued:
                    lines.append(f"✅ {len(queued)} دانلود جدید به صف اضافه شد:")
                    for item in queued[:self.MAX_STATUS_ITEMS]:
                        lines.append(
                            f"• {html.escape(item['show'])} "
                            f"<code>{item['episode']}</code> — {html.escape(item['quality'])} "
                            f"(<code>{html.escape(item['task_id'])}</code>)"
                        )
                if already_local and added_ids:
                    lines.append(
                        f"✅ {len(already_local)} مورد از انتشارهای جدید از قبل در Workspace موجود بودند."
                    )
                if already_remote and added_ids:
                    lines.append(
                        f"☁️ {len(already_remote)} مورد از انتشارهای جدید از قبل روی سرور مقصد موجود بودند."
                    )

                if completed_updates:
                    success_items = [item for item in completed_updates if item["result"] == "success"]
                    failed_items = [item for item in completed_updates if item["result"] != "success"]
                    if success_items:
                        lines.append(f"\n✅ دانلودهای تکمیل‌شده: {len(success_items)}")
                        for item in success_items[:self.MAX_STATUS_ITEMS]:
                            lines.append(
                                f"• {html.escape(item['show'])} "
                                f"<code>{html.escape(item['episode'])}</code> — "
                                f"{html.escape(item['quality'])}: دریافت فایل به پایان رسید"
                            )
                    if failed_items:
                        lines.append(f"\n❌ دانلودهای ناموفق: {len(failed_items)}")
                        for item in failed_items[:self.MAX_STATUS_ITEMS]:
                            lines.append(
                                f"• {html.escape(item['show'])} "
                                f"<code>{html.escape(item['episode'])}</code> — "
                                f"{html.escape(item['quality'])}: {html.escape(item['detail'])}"
                            )
                if not queued and not completed_updates and not already_local and not already_remote:
                    lines.append("در این نوبت انتشار منطبق با قسمت‌های مورد انتظار پیدا نشد.")
                await self._send_admin_message(bot, "\n".join(lines))
            return {
                "shows": len(shows),
                "queued": queued,
                "completed_updates": completed_updates,
                "already_local": already_local,
                "already_remote": already_remote,
                "release_count": len(releases),
            }

    def _expected_release_time(
        self,
        state: dict[str, Any],
        show: dict[str, Any],
        episode: int,
    ) -> str | None:
        history = state.get("release_history", {}).get(
            self._release_history_key(show, episode), []
        )
        times: list[int] = []
        for raw in history[-8:]:
            parsed = self._parse_datetime(raw)
            if parsed:
                local = parsed.astimezone(self._timezone())
                times.append(local.hour * 60 + local.minute)
        if not times:
            # Previous episode releases are a fallback when current episode hasn't appeared yet.
            previous_key = self._release_history_key(show, max(1, episode - 1))
            previous = state.get("release_history", {}).get(previous_key, [])
            for raw in previous[-8:]:
                parsed = self._parse_datetime(raw)
                if parsed:
                    local = parsed.astimezone(self._timezone())
                    times.append(local.hour * 60 + local.minute)
        if not times:
            return None
        minutes = int(statistics.median(times)) % (24 * 60)
        return f"{minutes // 60:02d}:{minutes % 60:02d}"

    async def _episode_status(
        self,
        show: dict[str, Any],
        episode: int,
        season_dir: Path | None = None,
    ) -> dict[str, Any]:
        season_dir = season_dir or self._show_folder(show, episode)
        season_dir.mkdir(parents=True, exist_ok=True)

        remote_root = AutomationService.compute_server_remote_dir_for_local_path(season_dir)
        remote_root_info = await self._inspect_remote_directory(remote_root)
        remote_name_map = {
            str(name).casefold(): str(name)
            for name in (remote_root_info.get("dirs", []) or [])
            if str(name).strip()
        }
        local_name_map = {
            child.name.casefold(): child.name
            for child in season_dir.iterdir()
            if child.is_dir()
        }

        configured_candidates: dict[str, list[str]] = {}
        candidate_names: dict[str, str] = {}
        for quality in show["qualities"]:
            names = self._quality_folder_candidates(show, quality)
            configured_candidates[quality] = names
            for name in names:
                candidate_names.setdefault(name.casefold(), name)

        for folded, name in {**local_name_map, **remote_name_map}.items():
            if self._is_quality_folder_name(name):
                candidate_names.setdefault(folded, name)

        quality_folders: dict[str, dict[str, Any]] = {}
        for folded, name in sorted(candidate_names.items(), key=lambda item: item[1].casefold()):
            local_name = local_name_map.get(folded, name)
            local_dir = season_dir / local_name
            local_files = (
                self._find_episode_files(local_dir, show["season"], episode)
                if local_dir.is_dir()
                else []
            )
            remote_actual_name = remote_name_map.get(folded)
            remote_files: list[str] = []
            if remote_actual_name:
                remote_path = f"{remote_root.rstrip('/')}/{remote_actual_name}"
                remote_info = await self._inspect_remote_directory(remote_path)
                remote_files = self._listed_episode_files(
                    list(remote_info.get("files", []) or []),
                    show["season"],
                    episode,
                )

            quality_folders[name] = {
                "local": bool(local_files),
                "remote": bool(remote_files),
                "present": bool(local_files or remote_files),
                "local_files": [item.name for item in local_files],
                "remote_files": remote_files,
            }

        quality_by_folded = {
            name.casefold(): detail
            for name, detail in quality_folders.items()
        }
        configured_presence = {
            quality: any(
                quality_by_folded.get(name.casefold(), {}).get("present", False)
                for name in names
            )
            for quality, names in configured_candidates.items()
        }

        output_names = {
            name.casefold(): name
            for name in list(local_name_map.values()) + list(remote_name_map.values())
            if (
                "softsub" in name.casefold()
                or name.casefold() == "hardsub"
                or name.casefold().endswith("-hardsub")
            )
        }
        soft_local = False
        soft_remote = False
        hard_local = False
        hard_remote = False

        for folded, name in output_names.items():
            local_name = local_name_map.get(folded, name)
            local_dir = season_dir / local_name
            local_files = (
                self._find_episode_files(local_dir, show["season"], episode)
                if local_dir.is_dir()
                else []
            )
            remote_files: list[str] = []
            remote_actual_name = remote_name_map.get(folded)
            if remote_actual_name:
                remote_path = f"{remote_root.rstrip('/')}/{remote_actual_name}"
                remote_info = await self._inspect_remote_directory(remote_path)
                remote_files = self._listed_episode_files(
                    list(remote_info.get("files", []) or []),
                    show["season"],
                    episode,
                )

            is_soft = "softsub" in folded or folded == "softsub"
            is_hard = folded == "hardsub" or folded.endswith("-hardsub")
            if is_soft:
                soft_local = soft_local or bool(local_files)
                soft_remote = soft_remote or bool(remote_files)
            if is_hard:
                hard_local = hard_local or bool(local_files)
                hard_remote = hard_remote or bool(remote_files)

        soft_present = soft_local or soft_remote
        hard_present = hard_local or hard_remote
        required_outputs = self.get_config().get("required_outputs", ["softsub", "hardsub"])
        if not isinstance(required_outputs, list):
            required_outputs = ["softsub", "hardsub"]

        all_raw = bool(configured_presence) and all(configured_presence.values())
        any_raw = any(detail["present"] for detail in quality_folders.values())
        missing_qualities = [
            quality for quality, present in configured_presence.items() if not present
        ]

        outputs_ok = all(
            (soft_present if str(item).casefold() == "softsub"
             else hard_present if str(item).casefold() == "hardsub"
             else True)
            for item in required_outputs
        )

        return {
            "raw_present": {
                name: bool(detail["present"])
                for name, detail in quality_folders.items()
            },
            "quality_folders": quality_folders,
            "configured_presence": configured_presence,
            "any_raw": any_raw,
            "all_raw": all_raw,
            "missing_qualities": missing_qualities,
            "softsub": soft_present,
            "softsub_local": soft_local,
            "softsub_remote": soft_remote,
            "hardsub": hard_present,
            "hardsub_local": hard_local,
            "hardsub_remote": hard_remote,
            "remote_checked": bool(remote_root_info.get("checked", False)),
            "remote_unavailable": list(remote_root_info.get("unavailable_servers", []) or []),
            "required_outputs": required_outputs,
            "complete": all_raw and outputs_ok,
        }

    async def build_daily_report(self) -> str:
        shows = self.get_shows()
        if not shows:
            return "📊 <b>گزارش روزانه EZTV</b>\nلیست سریال‌ها در <code>config.yaml</code> خالی است."

        state = self._load_state()
        local_now = datetime.now(self._timezone())
        today = local_now.date()
        lines = [
            f"📊 <b>گزارش روزانه EZTV — {local_now.strftime('%Y-%m-%d')}</b>",
            f"🕘 منطقه زمانی: <code>{html.escape(str(self.get_config().get('timezone', 'Asia/Tehran')))}</code>",
            "📌 فقط سریال‌ها و قسمت‌هایی نمایش داده می‌شوند که امروز در RSS یا صف دانلود فعالیت داشته‌اند.",
            "",
        ]

        activity_count = 0
        for show in shows:
            today_episodes = self._today_episodes_for_show(show, state, today)
            if not today_episodes:
                continue

            for episode in today_episodes:
                activity_count += 1
                tag = self._episode_tag(show["season"], episode)
                season_dir = await self._prepare_season(show, episode)
                status = await self._episode_status(show, episode, season_dir)

                scheduled_keys = [
                    self._scheduled_key(show, episode, quality)
                    for quality in show["qualities"]
                ]
                scheduled_active = any(
                    self._task_is_active(
                        str(state.get("scheduled_tasks", {}).get(key, {}).get("task_id", ""))
                    )
                    for key in scheduled_keys
                    if isinstance(state.get("scheduled_tasks", {}).get(key, {}), dict)
                )
                release_key = self._release_history_key(show, episode)
                release_history = state.get("release_history", {}).get(release_key, [])
                rss_match = state.get("rss_matches", {}).get(release_key, {})
                published_in_feed = bool(release_history or rss_match)

                if not status["any_raw"] and scheduled_active:
                    headline = "📥 منتشر شده و دانلود در صف/در حال اجرا"
                elif not status["any_raw"] and published_in_feed:
                    headline = "⚠️ در RSS دیده شده، اما هنوز فایل در دسترس نیست"
                elif not status["any_raw"]:
                    headline = "⏳ فعالیت امروز ثبت شده، اما فایل کیفیتی پیدا نشد"
                elif status["complete"]:
                    headline = "✅ همه کیفیت‌های تنظیم‌شده و خروجی‌های لازم موجودند"
                else:
                    headline = "🟠 بخشی از فایل‌ها یا پردازش‌ها موجود است"

                lines.append(
                    f"🎬 <b>{html.escape(show['title'])}</b> — "
                    f"<code>{tag}</code>"
                )
                lines.append(f"وضعیت: {headline}")

                quality_status = []
                for folder_name, detail in status["quality_folders"].items():
                    if detail["present"]:
                        sources = []
                        if detail["local"]:
                            sources.append("محلی")
                        if detail["remote"]:
                            sources.append("سرور مقصد")
                        quality_status.append(
                            f"✅ {html.escape(folder_name)} ({' + '.join(sources)})"
                        )
                    else:
                        quality_status.append(
                            f"❌ {html.escape(folder_name)} (قسمت اضافه نشده)"
                        )
                if quality_status:
                    lines.append("کیفیت‌ها: " + " | ".join(quality_status))
                else:
                    lines.append("کیفیت‌ها: هیچ پوشه کیفیتی در Workspace یا سرور مقصد شناسایی نشد.")

                if status["missing_qualities"]:
                    lines.append(
                        "کیفیت‌های تنظیم‌شده و موجودنشده: "
                        + ", ".join(html.escape(x) for x in status["missing_qualities"])
                    )

                if "softsub" in [str(x).casefold() for x in status["required_outputs"]]:
                    sources = []
                    if status["softsub_local"]:
                        sources.append("محلی")
                    if status["softsub_remote"]:
                        sources.append("سرور مقصد")
                    lines.append(
                        "سافت‌ساب: "
                        + ("✅ موجود (" + " + ".join(sources) + ")" if sources else "❌ موجود نیست")
                    )
                if "hardsub" in [str(x).casefold() for x in status["required_outputs"]]:
                    sources = []
                    if status["hardsub_local"]:
                        sources.append("محلی")
                    if status["hardsub_remote"]:
                        sources.append("سرور مقصد")
                    lines.append(
                        "هاردساب: "
                        + ("✅ موجود (" + " + ".join(sources) + ")" if sources else "❌ موجود نیست")
                    )

                if not status["remote_checked"]:
                    lines.append("⚠️ بررسی سرور مقصد کامل نشد؛ وضعیت محلی جداگانه نمایش داده شده است.")
                elif status["remote_unavailable"]:
                    unavailable = "، ".join(
                        html.escape(str(item.get("name") or item.get("id") or "سرور"))
                        for item in status["remote_unavailable"]
                    )
                    lines.append(f"⚠️ دسترسی به برخی سرورها برقرار نشد: {unavailable}")
            configured_day = self.weekday_label(show.get("release_day"))
            observed_days = self.observed_release_weekdays(show, state)
            lines.append(f"📅 روز انتشار تنظیم‌شده: <b>{configured_day}</b>")
            if observed_days:
                observed_labels = "، ".join(self.weekday_label(day) for day in observed_days)
                lines.append(f"📚 روز(های) ثبت‌شده از RSS: <b>{observed_labels}</b>")
            else:
                lines.append("📚 روز انتشار ثبت‌شده: هنوز داده کافی وجود ندارد.")

            quality_status = [
                (
                    f"✅ {html.escape(quality)}"
                    if present
                    else f"❌ {html.escape(quality)} خالی"
                )
                for quality, present in status["raw_present"].items()
            ]
            if quality_status:
                lines.append("کیفیت‌ها: " + " | ".join(quality_status))
            if status["missing_qualities"]:
                lines.append(
                    "کیفیت‌های خالی: "
                    + ", ".join(html.escape(x) for x in status["missing_qualities"])
                )
            if "softsub" in [str(x).casefold() for x in status["required_outputs"]]:
                lines.append("سافت‌ساب: " + ("✅ موجود" if status["softsub"] else "❌ خالی"))
            if "hardsub" in [str(x).casefold() for x in status["required_outputs"]]:
                lines.append("هاردساب: " + ("✅ موجود" if status["hardsub"] else "❌ خالی"))

                expected_time = self._expected_release_time(state, show, episode)
                if not published_in_feed and expected_time:
                    lines.append(f"⏰ زمان معمول انتشار بر اساس سابقه: حدود <code>{expected_time}</code>")
                lines.append("")

        if activity_count == 0:
            lines.append("امروز هیچ انتشار، دانلود صف‌شده یا پایان دانلودی برای سریال‌های ردیابی‌شده ثبت نشده است.")
        lines.append(
            "تعریف «کامل»: تمام کیفیت‌های تعریف‌شده برای ردیاب و خروجی‌های موردنیاز سافت‌ساب/هاردساب "
            "در Workspace یا سرور مقصد موجود باشند."
        )
        return "\n".join(lines)

    async def _send_admin_message(self, bot, text: str) -> None:
        admin_ids = cfg.get("telegram", "admin_ids", []) or []
        chunks: list[str] = []
        current = ""
        for line in text.splitlines():
            candidate = current + ("\n" if current else "") + line
            if len(candidate) > 3800 and current:
                chunks.append(current)
                current = line
            else:
                current = candidate
        if current:
            chunks.append(current)

        for admin_id in admin_ids:
            for index, chunk in enumerate(chunks):
                try:
                    await bot.send_message(
                        chat_id=int(admin_id),
                        text=chunk,
                        parse_mode="HTML",
                        disable_web_page_preview=True,
                    )
                except Exception:
                    logging.exception("Could not send EZTV tracker message to admin %s", admin_id)
                if index < len(chunks) - 1:
                    await asyncio.sleep(1.1)

    async def send_daily_report(self, bot) -> str:
        report = await self.build_daily_report()
        await self._send_admin_message(bot, report)
        return report

    async def _loop(self, bot) -> None:
        self._running = True
        logging.info("EZTV RSS tracker loop started.")
        while self._running:
            try:
                config = self.get_config()
                if bool(config.get("enabled", True)):
                    try:
                        await self.poll_once(bot, send_summary=True)
                    except Exception as exc:
                        logging.exception("EZTV RSS poll failed")
                        now = time.time()
                        if now - self._last_error_notice >= 1800:
                            self._last_error_notice = now
                            await self._send_admin_message(
                                bot,
                                "⚠️ <b>خطا در بررسی EZTV RSS</b>\n"
                                + html.escape(str(exc)[:700]),
                            )

                    local_now = datetime.now(self._timezone())
                    report_time = str(config.get("daily_report_time", "09:00"))
                    try:
                        hour, minute = [int(x) for x in report_time.split(":", 1)]
                    except (TypeError, ValueError):
                        hour, minute = 9, 0
                    state = self._load_state()
                    today = local_now.date().isoformat()
                    if (
                        local_now.hour * 60 + local_now.minute >= hour * 60 + minute
                        and state.get("last_daily_report_date") != today
                    ):
                        await self.send_daily_report(bot)
                        state["last_daily_report_date"] = today
                        self._save_state(state)
            except asyncio.CancelledError:
                break
            except Exception:
                logging.exception("Unexpected EZTV tracker loop error")

            try:
                interval = int(self.get_config().get("poll_interval_seconds", 300) or 300)
            except (TypeError, ValueError):
                interval = 300
            interval = max(60, min(interval, 24 * 60 * 60))
            try:
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                break
        logging.info("EZTV RSS tracker loop stopped.")

    def start(self, bot) -> None:
        self._running = True
        if self._task is not None and not self._task.done():
            logging.info("EZTV tracker already running; duplicate start ignored.")
            return
        self._task = asyncio.create_task(self._loop(bot), name="eztv-rss-tracker")

    def stop(self) -> None:
        self._running = False
        if self._task is not None and not self._task.done():
            self._task.cancel()
        self._task = None


eztv_tracker_service = EZTVTrackerService()