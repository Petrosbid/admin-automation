import asyncio
import difflib
import logging
import re
import shutil
import time
from urllib.parse import parse_qs, unquote, urlparse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable
from core.config_loader import cfg
from core.queue_manager import queue_engine
from services.downloader_service import DownloaderService
from services.mkv_service import MKVService
from services.handbrake_service import HandBrakeService
from services.cuteftp_service import CuteFTPService


@dataclass
class SoftSubTarget:
    raw_video: Path
    quality_folder_name: str
    output_dir: Path
    output_file: Path


@dataclass
class HardSubTarget:
    mode: str                 # 'hardsub_srt' | 'encode_ass'
    quality_tag: str          # '1080p' | '720p' | '480p'
    source_video: Path
    output_dir: Path
    output_file: Path
    preset_file: str
    preset_name: str
    ass_logo_path: Path | None = None


@dataclass
class AutomationPlan:
    is_series: bool
    category: str             # 'Series' | 'Anime' | 'Movies'
    base_dir: Path
    series_title: str = ""
    season_ep_tag: str = ""   # مثلا S03E01
    source_type: str = "WEB-DL"
    fa_sub_dest_dir: Path = None
    en_sub_dest_dir: Path = None
    existing_en_sub: Path | None = None
    embedded_en_source_video: Path | None = None
    embedded_en_track_id: int | None = None
    embedded_en_codec: str = "srt"
    fullhd_source_video: Path | None = None
    fullhd_source_candidates: list[Path] = field(default_factory=list)
    softsub_targets: list[SoftSubTarget] = field(default_factory=list)
    available_episodes: list[str] = field(default_factory=list)


class AutomationService:
    EPISODE_REGEX = re.compile(r"[Ss](\d{1,2})[\s._\-]*[Ee][Pp]?(\d{1,3})")
    ALT_SXE_REGEX = re.compile(r"\b(\d{1,2})[xX](\d{1,2})\b")
    SEASON_DIR_REGEX = re.compile(r"^[Ss](?:eason[\s._\-]*)?(\d{1,2})$", re.IGNORECASE)
    ROOT_CATEGORIES = ("Movies", "Series", "Anime")
    VIDEO_EXTS = {".mkv", ".mp4", ".avi", ".ts", ".mov", ".m4v", ".webm"}

    DEFAULT_SERIES_QUALITY_DIRS = (
        "1080p-EBTV",
        "720p-EBTV",
        "1080p-x265-PSA",
        "720p-x265-PSA",
        "1080p-Pahe",
        "720p-Pahe",
        "480p-RMTeam",
    )

    DEFAULT_ANIME_QUALITY_DIRS = (
        "1080p-EBTV",
        "720p-EBTV",
        "480p-EBTV",
        "FullHD",
        "1080p-x265-Judas",
    )

    @classmethod
    def ensure_workspace_root_categories(cls) -> dict[str, Path]:
        ws = Path(cfg.get("paths", "workspace_dir", "./workspace")).resolve()
        ws.mkdir(parents=True, exist_ok=True)
        cats = {}
        for c in cls.ROOT_CATEGORIES:
            p = ws / c
            p.mkdir(parents=True, exist_ok=True)
            cats[c] = p
        return cats

    @classmethod
    def get_movie_ass_logo_path(cls) -> Path:
        presets_dir = Path(cfg.get("paths", "presets_dir", "C:\\MediaBot\\presets")).resolve()
        candidates = [
            presets_dir.parent / "movie.ass",
            presets_dir / "movie.ass",
            Path.cwd() / "movie.ass",
        ]
        for c in candidates:
            if c.exists():
                return c
        return candidates[0]

    @classmethod
    def compute_server_remote_dir_for_local_path(cls, local_path: Path) -> str:
        ws = Path(cfg.get("paths", "workspace_dir", "./workspace")).resolve()
        target_dir = local_path.resolve().parent if local_path.is_file() else local_path.resolve()

        try:
            rel_posix = target_dir.relative_to(ws).as_posix()
        except ValueError:
            rel_posix = target_dir.name

        if rel_posix in ("", "."):
            return "/English"

        parts = [p for p in rel_posix.split("/") if p]
        first_low = parts[0].lower()
        if first_low in ("movie", "movies"):
            parts[0] = "Movies"
        elif first_low in ("serie", "series"):
            parts[0] = "Series"
        elif first_low == "anime":
            parts[0] = "Anime"

        return "/English/" + "/".join(parts)

    @classmethod
    def extract_season_num_from_dir(cls, folder: Path | None) -> int | None:
        if not folder:
            return None
        for p in (folder, folder.parent, folder.parent.parent if folder.parent else None):
            if p and p.name:
                m = cls.SEASON_DIR_REGEX.match(p.name.strip())
                if m:
                    return int(m.group(1))
        return None

    @classmethod
    def extract_episode_tag(cls, text: str, fallback_season: int | None = None) -> str | None:
        """
        استخراج هوشمند تگ SxxEyy:
        ۱. فرمت استاندارد S03E01 یا s03.e01
        ۲. فرمت 3x01
        ۳. اگر fallback_season مشخص باشد (مثلاً داخل پوشه S03 باشیم) و در نام فایل فقط E01 یا EP01 یا - 01 باشد، S03E01 می‌سازد.
        """
        match = cls.EPISODE_REGEX.search(text)
        if match:
            s_num = int(match.group(1))
            e_num = int(match.group(2))
            return f"S{s_num:02d}E{e_num:02d}"

        match_alt = cls.ALT_SXE_REGEX.search(text)
        if match_alt:
            s_num = int(match_alt.group(1))
            e_num = int(match_alt.group(2))
            return f"S{s_num:02d}E{e_num:02d}"

        if fallback_season is not None:
            ep_only = cls.extract_episode_number_only(text)
            if ep_only is not None:
                return f"S{fallback_season:02d}E{ep_only:02d}"

        return None

    @classmethod
    def extract_episode_number_only(cls, text: str) -> int | None:
        match = cls.EPISODE_REGEX.search(text)
        if match:
            return int(match.group(2))
        match_alt = cls.ALT_SXE_REGEX.search(text)
        if match_alt:
            return int(match_alt.group(2))

        # حذف رزولوشن‌ها و کدک‌ها قبل از جستجوی شماره قسمت تکی (برای جلوگیری از اشتباه گرفتن 1080 یا 265)
        cleaned = re.sub(
            r"\b(?:2160p|1080p|720p|480p|360p|x264|x265|h264|h265|10bit|8bit|6ch|2ch|ddp5|aac2)\b",
            "",
            text,
            flags=re.IGNORECASE
        )
        # Anime releases often encode the episode as [01] or (01), not SxxEyy.
        # In a known season directory this numeric token is a safe episode fallback.
        bracketed = re.findall(r"[\[(]\s*(\d{1,3})(?:v\d)?\s*[\])]", cleaned)
        for token in reversed(bracketed):
            value = int(token)
            if 0 <= value <= 999:
                return value

        m = re.search(r"(?:[Ee][Pp]?(?:isode)?[\s._\-]*|\s-\s|[\s._\-\[]E)(\d{1,3})(?:v\d)?(?:[\s._\-\]\[]|$)", cleaned)
        if m:
            val = int(m.group(1))
            if 0 <= val <= 999:
                return val

        # Final bare episode token immediately before release metadata, e.g.
        # Show.05.1080p.WEB-DL or Show 05 [HEVC].
        trailing_tokens = re.findall(
            r"(?:^|[ ._\-])(\d{1,3})(?:v\d)?(?=\s*(?:\[|\())",
            cleaned,
        )
        if trailing_tokens:
            value = int(trailing_tokens[-1])
            if 0 <= value <= 999:
                return value
        return None

    @staticmethod
    def detect_source_type(text: str) -> str:
        low = text.lower()
        if re.search(r"blu[ ._-]?ray|bdrip|brrip", low):
            return "BluRay"
        if re.search(r"web[ ._-]?rip", low):
            return "WEBRip"
        if "hdtv" in low:
            return "HDTV"
        return "WEB-DL"

    @classmethod
    def format_clean_title(cls, raw_title: str) -> str:
        cleaned = re.sub(r'[<>:"/\\|?*]', "", raw_title).strip()
        cleaned = re.split(
            r"(?:[\s._-]+(?:[\[(]\s*)?|[\[(]\s*)(?:S\d{1,2}E\d{1,3}|(?:19|20)\d{2}|2160p|1080p|720p|480p|FullHD|WEB-DL|WEBRip|BluRay)\b",
            cleaned,
            maxsplit=1,
            flags=re.IGNORECASE
        )[0]
        cleaned = re.sub(r"[\s_]+", ".", cleaned).strip(".")
        return cleaned or "Media"

    @classmethod
    def _find_raw_videos_in_dir(cls, directory: Path, recursive: bool = True) -> list[Path]:
        """
        پیدا کردن مستقیم تمام فایل‌های ویدئویی خام در یک پوشه کیفیت:
        - فایل‌هایی که تگ .EBTV دارند را به درستی می‌پذیرد.
        - فقط فایل‌های موقت (_temp_) و فایل‌هایی که قبلاً SoftSub یا HardSub شده‌اند را رد می‌کند.
        """
        if not directory or not directory.exists():
            return []

        iterator = directory.rglob("*") if recursive else directory.iterdir()
        videos: list[Path] = []

        for f in iterator:
            try:
                if not f.is_file():
                    continue
            except Exception:
                continue

            if f.suffix.lower() not in cls.VIDEO_EXTS:
                continue
            low_name = f.name.lower()
            if low_name.startswith(("_", ".")):
                continue
            if ".softsub." in low_name or ".hardsub." in low_name:
                continue
            # اگر در حالت recursive زیرپوشه‌ای از نوع SoftSub یا HardSub بود رد کن
            if recursive and f.parent != directory:
                try:
                    relative_parent_parts = f.relative_to(directory).parts[:-1]
                except ValueError:
                    relative_parent_parts = (f.parent.name,)
                excluded_parent = any(
                    part.casefold() in ("softsub", "hardsub", "srt", "sub", "subs")
                    or "softsub" in part.casefold()
                    or "hardsub" in part.casefold()
                    for part in relative_parent_parts
                )
                if excluded_parent:
                    continue

            videos.append(f.resolve())

        return sorted(videos, key=lambda x: x.name.lower())

    @classmethod
    def fullhd_source_score(cls, video_path: Path) -> int:
        """Score a raw video as a likely FullHD/encode source using its name and folder."""
        video_path = Path(video_path)
        filename = video_path.name.casefold()
        parent = video_path.parent.name.casefold()
        ancestor_names = [item.name.casefold() for item in list(video_path.parents)[:5]]
        combined = " ".join([filename, *ancestor_names])

        if (
            "softsub" in combined
            or "hardsub" in combined
            or re.search(r"(?i)(?:^|[._-])ebtv(?:[._-]|$)", filename)
        ):
            return 0

        score = 0
        if "fullhd" in ancestor_names:
            score += 150
        if re.search(r"(?i)full[ ._-]*hd|(?<![a-z0-9])fhd(?![a-z0-9])", combined):
            score += 120
        if re.search(r"(?i)(?<![a-z0-9])h[ ._-]*264(?![a-z0-9])|(?<![a-z0-9])x264(?![a-z0-9])|(?<![a-z0-9])avc(?![a-z0-9])", combined):
            score += 90
        if re.search(r"(?i)(?<![a-z0-9])1080p(?![a-z0-9])|1920[ ._x-]+1080", combined):
            score += 70
        return score

    @classmethod
    def find_fullhd_source_candidates(cls, videos: list[Path]) -> list[Path]:
        """Return likely FullHD sources, ordered by strongest filename/folder evidence."""
        scored: dict[str, tuple[int, Path]] = {}
        for raw_path in videos or []:
            path = Path(raw_path).resolve()
            if not path.is_file():
                continue
            score = cls.fullhd_source_score(path)
            if score > 0:
                scored[str(path).casefold()] = (score, path)
        return [
            item[1]
            for item in sorted(
                scored.values(),
                key=lambda item: (-item[0], item[1].name.casefold(), str(item[1]).casefold()),
            )
        ]

    @classmethod
    async def detect_audio_channels_tag(cls, video_path: Path) -> str:
        name_up = video_path.name.upper()
        if any(k in name_up for k in ("6CH", "5.1", "DDP5.1", "DD5.1", "AAC5.1", "7.1", "8CH")):
            return "6CH"
        if any(k in name_up for k in ("2CH", "2.0", "DDP2.0", "AAC2.0")):
            return "2CH"

        try:
            info = await MKVService.inspect_tracks(video_path)
            for t in info.get("tracks", []):
                if t.get("type") == "audio":
                    ch = int(t.get("properties", {}).get("audio_channels", 2))
                    if ch >= 6:
                        return "6CH"
                    return "2CH"
        except Exception:
            pass
        return "6CH" if "1080" in name_up else "2CH"

    @staticmethod
    def detect_encoder_and_network(filename: str) -> tuple[str, str]:
        net = "AMZN"
        for cand_net in ("AMZN", "NF", "ATVP", "DSNP", "HMAX", "MAX", "PCOK", "HULU", "PMTP", "STAN", "CRAV", "BBC", "ITV"):
            if re.search(rf"\b{cand_net}\b", filename, flags=re.IGNORECASE):
                net = cand_net.upper()
                break

        stem = Path(filename).stem
        stem = re.sub(r"([.\-_](?:SoftSub|HardSub|EBTV|EmpireBestTv))+?$", "", stem, flags=re.IGNORECASE)
        m_group = re.search(r"-([A-Za-z0-9]+)$", stem)
        if m_group:
            enc = m_group.group(1)
        else:
            known_encoders = ("NTb", "FLUX", "ETHEL", "Kitsune", "SuccessfulCrab", "RAWR", "TEPES", "SYNCOPY", "EDITH", "FENiX", "NOSiViD", "PECULATE")
            enc = "FLUX"
            for ke in known_encoders:
                if ke.lower() in stem.lower():
                    enc = ke
                    break
        return net, enc

    QUALITY_TAG = re.compile(r"(?<![a-z0-9])(2160p|1440p|1080p|720p|576p|540p|480p|360p|240p|4k|uhd)(?![a-z0-9])", re.I)

    @classmethod
    def quality_tag_from_name(cls, name: str) -> str:
        match = cls.QUALITY_TAG.search(name)
        tag = match.group(1).lower() if match else ""
        return "2160p" if tag in ("4k", "uhd") else tag

    @classmethod
    async def detect_movie_quality(cls, video_path: Path, folder_name: str) -> str:
        named_quality = cls.quality_tag_from_name(video_path.stem) or cls.quality_tag_from_name(folder_name)
        dimensions = await MKVService.read_video_dimensions(video_path)
        if dimensions is None:
            return named_quality or ("1080p" if folder_name.casefold() == "fullhd" else "")
        width, height = dimensions
        # Standard widths account for releases cropped to a wider aspect ratio.
        for quality, min_width, min_height in (
            ("2160p", 3600, 1600), ("1440p", 2400, 1120),
            ("1080p", 1800, 810), ("720p", 1200, 600),
        ):
            if width >= min_width or height >= min_height:
                return quality
        if height in (576, 540):
            return f"{height}p"
        if height >= 400 or width >= 800:
            return "480p"
        return "360p" if height >= 300 or width >= 600 else "240p"

    @classmethod
    def normalize_release_order(cls, stem: str) -> str:
        source = r"(WEB[ ._-]?DL|WEB[ ._-]?Rip|Blu[ ._-]?Ray|HDTV)"
        stem = re.sub(
            rf"(?<![a-z0-9]){source}[. _-]+{cls.QUALITY_TAG.pattern}",
            lambda match: f"{cls.quality_tag_from_name(match[2])}.{cls.detect_source_type(match[1])}",
            stem,
            flags=re.IGNORECASE,
        )
        return re.sub(
            rf"{cls.QUALITY_TAG.pattern}[. _-]+(\d{{1,2}}[ _-]?bit)[. _-]+{source}(?![a-z0-9])",
            lambda match: f"{cls.quality_tag_from_name(match[1])}.{cls.detect_source_type(match[3])}.{match[2]}",
            stem,
            flags=re.IGNORECASE,
        )

    @classmethod
    async def build_movie_release_filename(
        cls, video_path: Path, folder_name: str, title: str, *,
        softsub: bool = False, quality: str | None = None,
    ) -> str:
        stem = cls.clean_stem_for_ebtv(video_path.stem)
        release_parts = re.split(r"(?<!\d)(?:19|20)\d{2}(?!\d)", stem, maxsplit=1)
        metadata = release_parts[-1]
        release = re.search(
            rf"{cls.QUALITY_TAG.pattern}|\b(?:WEB[ ._-]?DL|WEB[ ._-]?Rip|Blu[ ._-]?Ray|HDTV)\b",
            metadata,
            flags=re.IGNORECASE,
        )
        if release and len(release_parts) == 1:
            metadata = metadata[release.start():]
        tags = f"{metadata} {folder_name}"
        leading_group = re.match(r"^\[([a-z0-9]+)\]", stem, re.I)

        def has_tag(tag: str) -> bool:
            return bool(re.search(rf"(?<![a-z0-9]){re.escape(tag)}(?![a-z0-9])", tags, re.I))

        year = cls.extract_media_year(title, video_path.name)
        if quality is None:
            quality = await cls.detect_movie_quality(video_path, folder_name)
        network = next((tag for tag in (
            "AMZN", "NF", "ATVP", "DSNP", "HMAX", "MAX", "PCOK", "HULU",
            "PMTP", "STAN", "CRAV", "BBC", "ITV",
        ) if has_tag(tag)), "")
        audio = next((tag for tag in (
            "DDP7.1", "DDP5.1", "DDP2.0", "DD5.1", "DD2.0", "AAC5.1", "AAC2.0",
            "EAC3", "AC3", "AAC", "TrueHD", "DTS", "6CH", "2CH",
        ) if has_tag(tag)), "")
        encoder = next((tag for tag in (
            "PSA", "Pahe", "YIFY", "YTS", "CHORTLE", "Judas", "RARBG", "RMTeam", "Tigole", "GalaxyRG",
            "NTb", "FLUX", "ETHEL", "Kitsune", "SuccessfulCrab", "RAWR", "TEPES",
            "SYNCOPY", "EDITH", "FENiX", "NOSiViD", "PECULATE",
        ) if has_tag(tag) or (leading_group and leading_group[1].casefold() == tag.casefold())), "")
        if not encoder and release:
            separator = r"[.-]" if stem != video_path.stem else "-"
            group = re.search(rf"(?<=[a-z0-9.]){separator}([a-z][a-z0-9]*)$", metadata, re.I)
            if group and group[1].casefold() not in {network.casefold(), audio.casefold()} and not re.fullmatch(
                r"DL|Rip|Ray|HD|FHD|FullHD|WEBRip|BluRay|HDTV|[xh]26[45]|HEVC|AVC",
                group[1], re.I,
            ):
                encoder = group[1]
        is_x265 = any(has_tag(tag) for tag in ("x265", "h265", "h.265", "HEVC"))
        bit_depth = "10bit" if has_tag("10bit") or has_tag("10-bit") or (encoder == "PSA" and is_x265) else ""
        fullhd = "FullHD" if quality == "1080p" and (has_tag("FullHD") or has_tag("FHD")) else ""
        parts = (
            cls.format_clean_title(title), year, quality, network, cls.detect_source_type(tags),
            audio, bit_depth, "x265" if is_x265 else "", fullhd, encoder,
            "SoftSub" if softsub else "", "EBTV",
        )
        ext = video_path.suffix.lower() or ".mkv"
        if softsub and ext != ".mp4":
            ext = ".mkv"
        return ".".join(part for part in parts if part) + ext

    @classmethod
    async def build_standard_raw_filename_for_folder(
        cls,
        video_path: Path,
        folder_name: str,
        series_or_movie_title: str,
        season_ep_tag: str | None = None,
        is_series: bool = True,
        movie_quality: str | None = None,
    ) -> str:
        if not is_series:
            return await cls.build_movie_release_filename(
                video_path, folder_name, series_or_movie_title, quality=movie_quality,
            )
        clean_name = cls.format_clean_title(series_or_movie_title)
        fallback_s = cls.extract_season_num_from_dir(video_path.parent)
        ep_tag = season_ep_tag or cls.extract_episode_tag(video_path.name, fallback_season=fallback_s) or ""
        src_type = cls.detect_source_type(video_path.name)
        ch_tag = await cls.detect_audio_channels_tag(video_path)
        f_low = folder_name.lower()
        orig_ext = video_path.suffix.lower() or ".mkv"

        if ep_tag:
            if "1080p-x265-judas" in f_low:
                return f"{clean_name}.{ep_tag}.1080p.{src_type}.10bit.x265.Judas.EBTV{orig_ext}"
            if "1080p-x265-psa" in f_low:
                return f"{clean_name}.{ep_tag}.1080p.{src_type}.10bit.{ch_tag}.x265.PSA.EBTV{orig_ext}"
            if "720p-x265-psa" in f_low:
                return f"{clean_name}.{ep_tag}.720p.{src_type}.10bit.2CH.x265.PSA.EBTV{orig_ext}"
            if "1080p-pahe" in f_low:
                return f"{clean_name}.{ep_tag}.1080p.{src_type}.6CH.Pahe.EBTV{orig_ext}"
            if "720p-pahe" in f_low:
                return f"{clean_name}.{ep_tag}.720p.{src_type}.Pahe.EBTV{orig_ext}"
            if "480p-rmteam" in f_low or "480p-rmt" in f_low:
                return f"{clean_name}.{ep_tag}.480p.{src_type}.RMTeam.EBTV{orig_ext}"
            if f_low == "fullhd":
                net, enc_name = cls.detect_encoder_and_network(video_path.name)
                return f"{clean_name}.{ep_tag}.1080p.{net}.{src_type}.FullHD.{enc_name}.EBTV{orig_ext}"
            if "1080p-ebtv" in f_low:
                return f"{clean_name}.{ep_tag}.1080p.{src_type}.6CH.EBTV{orig_ext}"
            if "720p-ebtv" in f_low:
                return f"{clean_name}.{ep_tag}.720p.{src_type}.EBTV{orig_ext}"
            if "480p-ebtv" in f_low:
                return f"{clean_name}.{ep_tag}.480p.{src_type}.EBTV{orig_ext}"

        quality = cls.quality_tag_from_name(folder_name) or cls.quality_tag_from_name(video_path.stem)
        if f_low == "fullhd" and not quality:
            quality = "1080p"
        custom_tag = cls.QUALITY_TAG.sub("", folder_name)
        custom_tag = re.sub(r"WEB[ ._-]?(?:DL|Rip)|Blu[ ._-]?Ray|HDTV", "", custom_tag, flags=re.I)
        custom_parts = [
            part for part in re.split(r"[-._\s]+", custom_tag)
            if part and part.casefold() != "ebtv" and not cls.SEASON_DIR_REGEX.fullmatch(part)
        ]
        parts = (clean_name, ep_tag, quality, src_type, *custom_parts, "EBTV")
        return ".".join(part for part in parts if part) + orig_ext

    @classmethod
    def match_filename_from_server_siblings(
        cls,
        server_filenames: list[str],
        new_ep_tag: str,
        video_extension: str | None = None,
    ) -> str | None:
        if not server_filenames or not new_ep_tag:
            return None

        video_exts = (".mkv", ".mp4", ".avi", ".ts", ".mov", ".m4v", ".webm")
        valid_siblings = [
            f for f in server_filenames
            if f.lower().endswith(video_exts)
            and "/" not in f and "\\" not in f
            and (not video_extension or Path(f).suffix.casefold() == video_extension.casefold())
            and cls.EPISODE_REGEX.search(f)
        ]
        if not valid_siblings:
            return None

        template_name = sorted(valid_siblings)[-1]
        new_e_only = new_ep_tag.split("E")[1]

        def _replacer(match: re.Match) -> str:
            s_part = match.group(1)
            return f"S{int(s_part):02d}E{new_e_only}"

        return cls.normalize_release_order(cls.EPISODE_REGEX.sub(_replacer, template_name, count=1))

    @classmethod
    def extract_media_year(cls, *values: object) -> str | None:
        """Extract a plausible release year from a title, folder, or source filename."""
        for value in values:
            if value is None:
                continue
            match = re.search(r"(?<!\d)((?:19|20)\d{2})(?!\d)", str(value))
            if match:
                return match.group(1)
        return None

    @classmethod
    def build_standard_subtitle_filename(
        cls,
        series_or_movie_title: str,
        season_ep_tag: str,
        source_type: str,
        lang: str,
        ext: str = ".srt",
        year: str | None = None,
    ) -> str:
        clean_name = cls.format_clean_title(series_or_movie_title)
        year_match = re.fullmatch(r"(?:19|20)\d{2}", str(year or "").strip())
        year_part = f".{year_match.group(0)}" if year_match else ""
        lang_up = lang.upper()
        if not ext.startswith("."):
            ext = f".{ext}"
        if season_ep_tag:
            return f"{clean_name}{year_part}.{season_ep_tag}.{source_type}.{lang_up}{ext}"
        return f"{clean_name}{year_part}.{source_type}.{lang_up}{ext}"

    @classmethod
    def build_hardsub_srt_filename(
        cls,
        series_or_movie_title: str,
        season_ep_tag: str,
        quality_tag: str,
        source_type: str = "WEB-DL"
    ) -> str:
        clean_name = cls.format_clean_title(series_or_movie_title)
        year = cls.extract_media_year(series_or_movie_title) if not season_ep_tag else None
        parts = (clean_name, year, season_ep_tag, quality_tag, source_type, "HardSub", "EBTV", "mkv")
        return ".".join(part for part in parts if part)

    @classmethod
    def build_encode_ass_filename(
        cls,
        series_or_movie_title: str,
        season_ep_tag: str,
        quality_tag: str,
        source_type: str = "WEB-DL"
    ) -> str:
        clean_name = cls.format_clean_title(series_or_movie_title)
        year = cls.extract_media_year(series_or_movie_title) if not season_ep_tag else None
        channels = "6CH" if quality_tag == "1080p" else ""
        parts = (clean_name, year, season_ep_tag, quality_tag, source_type, channels, "EBTV", "mkv")
        return ".".join(part for part in parts if part)

    @staticmethod
    def clean_stem_for_ebtv(stem: str, target_res: str | None = None) -> str:
        cleaned = stem.strip()
        cleaned = re.sub(
            r"([.\-_](?:SoftSub|HardSub|EBTV|EmpireBestTv))+?$",
            "",
            cleaned,
            flags=re.IGNORECASE
        )
        if target_res in ("1080p", "720p", "480p"):
            if re.search(r"\b(1080p|720p|480p|2160p|FullHD)\b", cleaned, flags=re.IGNORECASE):
                cleaned = re.sub(
                    r"\b(1080p|720p|480p|2160p|FullHD)\b",
                    target_res,
                    cleaned,
                    count=1,
                    flags=re.IGNORECASE
                )
            else:
                cleaned = f"{cleaned}.{target_res}"
        return AutomationService.normalize_release_order(cleaned)

    @classmethod
    def build_softsub_filename(cls, raw_video: Path) -> str:
        clean = cls.clean_stem_for_ebtv(raw_video.stem)
        out_ext = ".mp4" if raw_video.suffix.lower() == ".mp4" else ".mkv"
        return f"{clean}.SoftSub.EBTV{out_ext}"

    @classmethod
    def create_movie_structure(cls, movie_folder: Path) -> dict[str, Path]:
        movie_folder.mkdir(parents=True, exist_ok=True)
        srt_dir = movie_folder / "srt"
        srt_dir.mkdir(parents=True, exist_ok=True)
        return {
            "base": movie_folder,
            "softsub": movie_folder / "SoftSub",
            "hardsub": movie_folder / "Hardsub",
            "srt": srt_dir,
        }

    REMOTE_STRUCTURE_CACHE_TTL = 45
    _remote_structure_cache: dict[str, tuple[float, bool, list[str]]] = {}

    @classmethod
    def normalize_server_quality_directories(cls, remote_dirs: list[str]) -> list[str]:
        """
        تبدیل پوشه‌های مستقیم سرور به نام پوشه‌های مجاز در Workspace.

        - پوشه‌های SoftSub روی مبدا نیز ساخته می‌شوند.
        - برای هر پوشه *-SoftSub، پوشه مبدأ بدون پسوند SoftSub را نیز می‌سازد.
        - اگر نام یکی از پوشه‌های مقصد شامل EBTV باشد، FullHD را نیز تضمین می‌کند.
        """
        local_names: set[str] = set()

        for raw_name in remote_dirs or []:
            name = str(raw_name or "").strip().rstrip("/").strip()
            if (
                not name
                or name in (".", "..")
                or any(char in name for char in '<>:"/\\|?*')
                or name.casefold() == "srt"
            ):
                continue

            local_names.add(name)

            softsub_match = re.search(r"(?:[-_. ]?softsub)$", name, flags=re.IGNORECASE)
            if softsub_match:
                source_name = name[:softsub_match.start()].rstrip(" ._-")
                if source_name and source_name.casefold() != "srt":
                    local_names.add(source_name)

        if any("ebtv" in str(name).casefold() for name in remote_dirs or []):
            local_names.add("FullHD")

        return sorted(local_names, key=str.casefold)

    @classmethod
    async def create_season_structure_from_server(
        cls,
        season_folder: Path,
        category: str = "Series"
    ) -> dict[str, Path]:
        """
        ساخت ساختار محلی فصل بر اساس زیرپوشه‌های همان مسیر در سرور مقصد.
        درخواست FTP در thread جدا اجرا می‌شود تا حلقه اصلی ربات مسدود نشود.
        """
        season_folder = Path(season_folder).resolve()
        remote_dir = cls.compute_server_remote_dir_for_local_path(season_folder)
        cache_key = remote_dir.casefold()
        now = time.monotonic()

        cached = cls._remote_structure_cache.get(cache_key)
        if cached and now - cached[0] < cls.REMOTE_STRUCTURE_CACHE_TTL:
            remote_exists, remote_dirs = cached[1], list(cached[2])
        else:
            remote_exists = False
            remote_dirs: list[str] = []
            try:
                remote_info = await CuteFTPService.inspect_remote_dir(remote_dir)
                remote_exists = bool(remote_info.get("exists", False))
                if remote_exists:
                    remote_dirs = list(remote_info.get("dirs", []))
                    cls._remote_structure_cache[cache_key] = (
                        now,
                        True,
                        remote_dirs.copy(),
                    )
            except Exception as exc:
                logging.warning(
                    "Could not inspect destination season folder %s: %s",
                    remote_dir,
                    exc,
                )

        quality_dirs = cls.normalize_server_quality_directories(remote_dirs)

        if str(category).strip().lower() == "anime":
            paths = cls.create_anime_season_structure(season_folder, quality_dirs)
        else:
            paths = cls.create_series_season_structure(season_folder, quality_dirs)

        if not remote_exists:
            logging.warning(
                "Destination season folder was not found or could not be inspected: %s. "
                "Only the local subtitle structure was created.",
                remote_dir,
            )
        else:
            logging.info(
                "Mirrored %d destination folders from %s into %s",
                len(quality_dirs),
                remote_dir,
                season_folder,
            )
            if quality_dirs:
                try:
                    from services.daily_activity_service import DailyActivityService
                    media_title = (
                        season_folder.parent.name
                        if cls.SEASON_DIR_REGEX.match(season_folder.name)
                        else season_folder.name
                    )
                    DailyActivityService.record(
                        "automation_folder_mirrored",
                        category=str(category),
                        media_title=media_title,
                        season=season_folder.name,
                        remote_dir=remote_dir,
                        folder_count=len(quality_dirs),
                        folders=quality_dirs,
                    )
                except Exception:
                    logging.exception(
                        "Could not record mirrored automation folders for %s",
                        remote_dir,
                    )

        return paths

    @classmethod
    def create_series_season_structure(
        cls,
        season_folder: Path,
        quality_dirs: list[str] | None = None
    ) -> dict[str, Path]:
        season_folder.mkdir(parents=True, exist_ok=True)
        for q_dir in quality_dirs or []:
            if q_dir and q_dir not in (".", "..") and not any(c in q_dir for c in '<>:"/\\|?*'):
                (season_folder / q_dir).mkdir(parents=True, exist_ok=True)

        srt_fa = season_folder / "srt" / "FA"
        srt_en = season_folder / "srt" / "EN"
        srt_fa.mkdir(parents=True, exist_ok=True)
        srt_en.mkdir(parents=True, exist_ok=True)

        return {
            "base": season_folder,
            "srt": season_folder / "srt",
            "srt_fa": srt_fa,
            "srt_en": srt_en,
        }

    @classmethod
    def create_anime_season_structure(
        cls,
        season_folder: Path,
        quality_dirs: list[str] | None = None
    ) -> dict[str, Path]:
        season_folder.mkdir(parents=True, exist_ok=True)
        for q_dir in quality_dirs or []:
            if q_dir and q_dir not in (".", "..") and not any(c in q_dir for c in '<>:"/\\|?*'):
                (season_folder / q_dir).mkdir(parents=True, exist_ok=True)

        srt_dir = season_folder / "srt"
        srt_dir.mkdir(parents=True, exist_ok=True)

        return {
            "base": season_folder,
            "srt": srt_dir,
            "srt_fa": srt_dir,
            "srt_en": srt_dir,
        }

    @classmethod
    def detect_category_from_path(cls, folder: Path) -> str:
        parts_low = [p.lower() for p in folder.parts]
        if "anime" in parts_low:
            return "Anime"
        if "series" in parts_low or "serie" in parts_low:
            return "Series"
        if "movies" in parts_low or "movie" in parts_low:
            return "Movies"
        return "Series" if cls.is_series_season_dir(folder) else "Movies"

    @classmethod
    def is_series_season_dir(cls, folder: Path) -> bool:
        parts_low = [p.lower() for p in folder.parts]
        if "movies" in parts_low or "movie" in parts_low:
            return False
        if cls.SEASON_DIR_REGEX.match(folder.name):
            return True
        if "series" in parts_low or "anime" in parts_low:
            if (folder / "srt" / "FA").exists() or any((folder / d).is_dir() for d in cls.DEFAULT_SERIES_QUALITY_DIRS):
                return True
        if (folder / "srt" / "FA").exists() or (folder / "srt" / "EN").exists():
            return True
        if not folder.exists():
            return False
        quality_subdirs = [
            d for d in folder.iterdir()
            if d.is_dir() and any(q in d.name.lower() for q in ("1080p", "720p", "480p", "2160p", "fullhd"))
        ]
        return len(quality_subdirs) >= 1

    @classmethod
    def scan_available_episodes_in_season(cls, season_dir: Path) -> list[str]:
        """اسکن تمام قسمت‌های موجود در زیرپوشه‌های فصل (پشتیبانی از S03E01، E01، 3x01 و - 01)"""
        episodes: set[str] = set()
        if not season_dir or not season_dir.exists():
            return []

        fallback_s = cls.extract_season_num_from_dir(season_dir)

        for sub in season_dir.iterdir():
            if not sub.is_dir():
                continue
            low_name = sub.name.lower()
            if (
                low_name in ("srt", "sub", "softsub", "hardsub")
                or low_name.endswith("-softsub")
                or low_name.endswith("-hardsub")
            ):
                continue
            for vid in cls._find_raw_videos_in_dir(sub, recursive=True):
                ep = cls.extract_episode_tag(vid.name, fallback_season=fallback_s)
                if ep:
                    episodes.add(ep)

        for vid in cls._find_raw_videos_in_dir(season_dir, recursive=False):
            ep = cls.extract_episode_tag(vid.name, fallback_season=fallback_s)
            if ep:
                episodes.add(ep)

        return sorted(list(episodes))

    @classmethod
    def scan_all_episodes_anywhere_in_season(cls, season_dir: Path) -> list[str]:
        episodes: set[str] = set()
        if not season_dir or not season_dir.exists():
            return []
        fallback_s = cls.extract_season_num_from_dir(season_dir)
        for f in season_dir.rglob("*"):
            if f.is_file() and not f.name.startswith("_"):
                ep = cls.extract_episode_tag(f.name, fallback_season=fallback_s)
                if ep:
                    episodes.add(ep)
        return sorted(list(episodes))

    @classmethod
    def collect_all_episode_files_for_smart_upload(
        cls,
        season_dir: Path,
        episode_tag: str
    ) -> list[tuple[Path, str]]:
        season_dir = season_dir.resolve()
        ep_up = episode_tag.upper()
        fallback_s = cls.extract_season_num_from_dir(season_dir)
        matched_items: list[tuple[Path, str]] = []

        for item in sorted(season_dir.rglob("*"), key=lambda p: p.as_posix().lower()):
            if not item.is_file():
                continue
            if item.name.startswith("_") or item.name.endswith(".aria2"):
                continue

            item_ep = cls.extract_episode_tag(item.name, fallback_season=fallback_s)
            if item_ep == ep_up:
                remote_dir = cls.compute_server_remote_dir_for_local_path(item)
                matched_items.append((item, remote_dir))

        return matched_items

    @classmethod
    def extract_media_title_query(cls, reference: str) -> str:
        """
        استخراج عنوان احتمالی فیلم/سریال از نام فایل، لینک دانلود یا Magnet URI.
        تگ فصل/قسمت، رزولوشن، کدک و عبارت‌های انتشار حذف می‌شوند.
        """
        raw = unquote(str(reference or "")).strip()
        if not raw:
            return ""

        try:
            parsed = urlparse(raw)
            if parsed.scheme in ("http", "https", "magnet"):
                query = parse_qs(parsed.query)
                for key in ("dn", "filename", "file", "name", "title"):
                    values = query.get(key)
                    if values and values[0].strip():
                        raw = unquote(values[0]).strip()
                        break
                else:
                    raw = Path(unquote(parsed.path)).name or raw
        except Exception:
            pass

        raw = raw.replace("\\", "/").rsplit("/", 1)[-1]
        raw = re.sub(r"\.(?:mkv|mp4|avi|mov|m4v|webm|ts|srt|ass|ssa|sub|idx|zip|rar|7z|torrent)$", "", raw, flags=re.IGNORECASE)
        raw = re.sub(r"(?i)\b(?:subs?|subtitle|subtitles|farsi|persian|english|multi|dual|proper|repack|internal)\b", " ", raw)

        episode_match = re.search(
            r"(?i)(?:[._ -]S\d{1,2}[._ -]*E\d{1,3}|[._ -]\d{1,2}x\d{1,3}|[._ -]E(?:P)?\d{1,3})",
            raw,
        )
        if episode_match:
            raw = raw[:episode_match.start()]

        release_markers = re.search(
            r"(?i)[._ -](?:2160p|1080p|720p|480p|360p|4k|8k|web[._ -]?(?:dl|rip)|blu[._ -]?ray|bdrip|brrip|hdtv|x264|x265|h264|h265|hevc|av1|10bit|8bit|aac|ddp|dts|proper|repack)\b",
            raw,
        )
        if release_markers:
            raw = raw[:release_markers.start()]

        raw = re.sub(r"\[[^\]]*\]|\([^)]*\)", " ", raw)
        raw = re.sub(r"[^\w\s'-]", " ", raw, flags=re.UNICODE)
        raw = re.sub(r"[._-]+", " ", raw)
        raw = re.sub(r"\s+", " ", raw).strip(" -_")
        return raw[:100].strip()

    @staticmethod
    def _media_title_similarity(query: str, candidate: str) -> float:
        def normalize(value: str) -> str:
            value = re.sub(r"[^\w]+", " ", str(value or "").casefold(), flags=re.UNICODE)
            return " ".join(value.split())

        q_norm = normalize(query)
        c_norm = normalize(candidate)
        if not q_norm or not c_norm:
            return 0.0

        q_tokens = set(q_norm.split())
        c_tokens = set(c_norm.split())
        overlap = len(q_tokens & c_tokens) / max(1, len(q_tokens))
        sequence = difflib.SequenceMatcher(None, q_norm, c_norm).ratio()

        if q_norm in c_norm or c_norm in q_norm:
            sequence = max(sequence, 0.86)

        return min(1.0, max(sequence, overlap * 0.92))

    @classmethod
    async def search_media_destination_candidates(
        cls,
        reference: str,
        limit: int = 8,
    ) -> list[dict]:
        """
        جستجوی عنوان در Workspace و پوشه‌های سطح اول سرور مقصد.
        خروجی path محلی متناظر دارد؛ اگر فولدر فقط روی سرور باشد، هنگام انتخاب ساخته می‌شود.
        برای سریال، در صورت وجود SxxEyy فصل مرتبط پیشنهاد می‌شود.
        """
        query = cls.extract_media_title_query(reference)
        if not query:
            return []

        ws = Path(cfg.get("paths", "workspace_dir", "./workspace")).resolve()
        category_names = ("Movies", "Series", "Anime")
        category_roots = cls.ensure_workspace_root_categories()
        episode_tag = cls.extract_episode_tag(reference)
        season_name = f"S{int(episode_tag[1:3]):02d}" if episode_tag else None

        # فهرست پوشه‌های محلی و سرور در سطح عنوان.
        names_by_category: dict[str, set[str]] = {name: set() for name in category_names}
        for category, root in category_roots.items():
            try:
                names_by_category[category].update(
                    p.name for p in root.iterdir()
                    if p.is_dir() and not p.name.startswith("_")
                    and p.name.lower() not in ("browser_profile", "srt", "softsub", "hardsub", "fullhd")
                    and not p.name.lower().endswith(("-softsub", "-hardsub"))
                )
            except OSError:
                continue

        remote_roots = {
            "Movies": "/English/Movies",
            "Series": "/English/Series",
            "Anime": "/English/Anime",
        }
        async def _inspect_category_root(category_name: str, remote_root: str):
            try:
                return category_name, await asyncio.wait_for(
                    CuteFTPService.inspect_remote_dir(remote_root),
                    timeout=8.0,
                )
            except Exception as exc:
                logging.debug("Destination title search failed for %s: %s", remote_root, exc)
                return category_name, {}

        local_match_scores = [
            cls._media_title_similarity(query, title_name)
            for names in names_by_category.values()
            for title_name in names
        ]
        has_strong_local_match = any(score >= 0.62 for score in local_match_scores)

        # A good local match is enough; don't block subtitle suggestions on FTP availability.
        # Remote discovery is used only when local Workspace folders do not provide a strong hit.
        if has_strong_local_match:
            root_results = []
        else:
            root_results = await asyncio.gather(
                *(
                    _inspect_category_root(category, remote_root)
                    for category, remote_root in remote_roots.items()
                )
            )
        for category, info in root_results:
            if info.get("exists"):
                names_by_category[category].update(
                    str(name) for name in info.get("dirs", [])
                    if name and not str(name).startswith("_")
                )

        ranked: list[tuple[float, str, str]] = []
        for category, names in names_by_category.items():
            for title_name in names:
                score = cls._media_title_similarity(query, title_name)
                if score >= 0.24:
                    ranked.append((score, category, title_name))

        ranked.sort(key=lambda item: (-item[0], item[1], item[2].casefold()))

        results: list[dict] = []
        seen_paths: set[str] = set()
        for score, category, title_name in ranked[:max(limit * 2, limit)]:
            category_root = category_roots[category]
            show_dir = category_root / title_name
            selected_dirs: list[Path] = []

            if category == "Movies":
                selected_dirs = [show_dir]
            elif season_name:
                selected_dirs = [show_dir / season_name]
            else:
                local_seasons = []
                if show_dir.is_dir():
                    try:
                        local_seasons = sorted(
                            [p for p in show_dir.iterdir() if p.is_dir() and cls.SEASON_DIR_REGEX.match(p.name)],
                            key=lambda p: p.name.casefold(),
                        )
                    except OSError:
                        pass
                selected_dirs = local_seasons[:3] or [show_dir]

            # اگر عنوان فقط روی سرور است، فصل‌های آن را هم برای همان عنوان بررسی کن.
            if category != "Movies" and season_name is None and (
                not any(cls.SEASON_DIR_REGEX.match(path.name) for path in selected_dirs)
            ):
                remote_show = f"{remote_roots[category]}/{title_name}"
                try:
                    info = await asyncio.wait_for(
                        CuteFTPService.inspect_remote_dir(remote_show),
                        timeout=8.0,
                    )
                    remote_seasons = [
                        str(name) for name in info.get("dirs", [])
                        if cls.SEASON_DIR_REGEX.match(str(name))
                    ] if info.get("exists") else []
                    if season_name:
                        selected_dirs = [show_dir / season_name]
                    elif remote_seasons:
                        selected_dirs = [show_dir / name for name in sorted(remote_seasons, key=str.casefold)[:3]]
                except Exception:
                    pass

            for candidate_path in selected_dirs:
                normalized = str(candidate_path.resolve()).casefold()
                if normalized in seen_paths:
                    continue
                seen_paths.add(normalized)

                label = title_name
                if category != "Movies" and cls.SEASON_DIR_REGEX.match(candidate_path.name):
                    label = f"{title_name} · {candidate_path.name}"
                results.append({
                    "name": title_name,
                    "label": label,
                    "category": category,
                    "path": str(candidate_path.resolve()),
                    "remote_path": cls.compute_server_remote_dir_for_local_path(candidate_path),
                    "score": round(score, 3),
                    "exists_local": candidate_path.exists(),
                    "episode_tag": episode_tag or "",
                })
                if len(results) >= limit:
                    return results

        return results

    @classmethod
    def find_matching_workspace_folder_for_sub(cls, sub_filename: str) -> tuple[Path | None, list[str]]:
        """Unicode-aware local fallback for matching an incoming subtitle to a title/season folder."""
        ws = Path(cfg.get("paths", "workspace_dir", "./workspace")).resolve()
        if not ws.is_dir():
            return None, []

        query = cls.extract_media_title_query(sub_filename)
        if not query:
            return None, []

        ep_tag = cls.extract_episode_tag(sub_filename)
        category_roots = cls.ensure_workspace_root_categories()
        candidate_roots: list[Path] = []
        seen: set[str] = set()

        for root in category_roots.values():
            try:
                children = list(root.iterdir())
            except OSError:
                children = []
            for child in children:
                if child.is_dir() and not child.name.startswith("_"):
                    key = str(child.resolve()).casefold()
                    if key not in seen:
                        seen.add(key)
                        candidate_roots.append(child)

        # Keep support for older workspaces where titles live directly under Workspace.
        for child in ws.iterdir():
            if (
                not child.is_dir()
                or child.name.startswith("_")
                or child.name.casefold() in {"browser_profile", "movies", "series", "anime"}
            ):
                continue
            key = str(child.resolve()).casefold()
            if key not in seen:
                seen.add(key)
                candidate_roots.append(child)

        best_folder: Path | None = None
        best_score = 0.0
        matched_eps: list[str] = []

        for title_dir in candidate_roots:
            score = cls._media_title_similarity(query, title_dir.name)
            if score < 0.24:
                continue

            if ep_tag:
                season_num = int(ep_tag[1:3])
                season_candidates = [
                    title_dir / f"S{season_num:02d}",
                    title_dir / f"S{season_num}",
                ]
                season_dir = next((item for item in season_candidates if item.is_dir()), None)
                candidate = season_dir or title_dir
                candidate_score = score + (0.08 if season_dir else 0.0)
                candidate_eps = [ep_tag]
            else:
                try:
                    seasons = sorted(
                        [
                            item for item in title_dir.iterdir()
                            if item.is_dir() and cls.SEASON_DIR_REGEX.match(item.name)
                        ],
                        key=lambda item: item.name.casefold(),
                    )
                except OSError:
                    seasons = []
                candidate = seasons[0] if len(seasons) == 1 else title_dir
                candidate_score = score + (0.04 if len(seasons) == 1 else 0.0)
                candidate_eps = cls.scan_available_episodes_in_season(candidate)

            if candidate_score > best_score:
                best_score = candidate_score
                best_folder = candidate
                matched_eps = candidate_eps

        return best_folder, matched_eps

    @classmethod
    async def analyze_and_build_plan(
        cls,
        target_dir: Path,
        episode_tag: str | None = None
    ) -> AutomationPlan:
        target_dir = target_dir.resolve()
        category = cls.detect_category_from_path(target_dir)
        is_series = cls.is_series_season_dir(target_dir) or bool(episode_tag) or category in ("Series", "Anime")

        if is_series:
            is_anime = (category == "Anime")
            paths = await cls.create_season_structure_from_server(
                target_dir,
                category="Anime" if is_anime else "Series",
            )

            series_title = target_dir.parent.name if cls.SEASON_DIR_REGEX.match(target_dir.name) else target_dir.name
            fallback_s = cls.extract_season_num_from_dir(target_dir)
            avail_eps = cls.scan_available_episodes_in_season(target_dir)
            if not episode_tag and avail_eps:
                episode_tag = avail_eps[-1]

            plan = AutomationPlan(
                is_series=True,
                category="Anime" if is_anime else "Series",
                base_dir=target_dir,
                series_title=series_title,
                season_ep_tag=(episode_tag or "").upper(),
                fa_sub_dest_dir=paths["srt_fa"],
                en_sub_dest_dir=paths["srt_en"],
                available_episodes=avail_eps,
            )

            if not is_anime and plan.season_ep_tag and paths["srt_en"].exists():
                for f in paths["srt_en"].iterdir():
                    if f.is_file() and f.suffix.lower() in (".srt", ".ass"):
                        f_ep = cls.extract_episode_tag(f.name, fallback_season=fallback_s)
                        low_name = f.name.casefold()
                        is_named_english = (
                            ".en." in low_name
                            or low_name.startswith("en_")
                            or low_name.endswith(("_en.srt", "_en.ass", "_en.ssa"))
                        )
                        if is_named_english and (
                            (f_ep and f_ep == plan.season_ep_tag)
                            or plan.season_ep_tag.lower() in low_name
                        ):
                            plan.existing_en_sub = f.resolve()
                            break

            # ۱. اسکن تمام زیرپوشه‌های کیفیت داخل فصل (مانند 1080p-EBTV, 1080p-x265-PSA, FullHD و...)
            for sub in sorted(target_dir.iterdir(), key=lambda p: p.name.lower()):
                if not sub.is_dir():
                    continue
                low_name = sub.name.lower()
                if (
                    low_name in ("srt", "sub", "softsub", "hardsub")
                    or low_name.endswith("-softsub")
                    or low_name.endswith("-hardsub")
                ):
                    continue

                videos_in_sub = cls._find_raw_videos_in_dir(sub, recursive=True)
                for vid in videos_in_sub:
                    vid_ep = cls.extract_episode_tag(vid.name, fallback_season=fallback_s)
                    if plan.season_ep_tag:
                        target_ep_num = cls.extract_episode_number_only(plan.season_ep_tag)
                        vid_ep_num = cls.extract_episode_number_only(vid.name)
                        if vid_ep and vid_ep.upper() != plan.season_ep_tag.upper():
                            continue
                        if not vid_ep and vid_ep_num is not None and target_ep_num is not None:
                            if vid_ep_num != target_ep_num:
                                continue
                        elif not vid_ep and vid_ep_num is None and len(videos_in_sub) > 1:
                            # Never fan out one untagged source video to every episode
                            # when multiple raw episodes exist in the same quality folder.
                            continue

                    soft_out_dir = target_dir / f"{sub.name}-SoftSub"
                    soft_out_file = soft_out_dir / cls.build_softsub_filename(vid)

                    plan.softsub_targets.append(
                        SoftSubTarget(
                            raw_video=vid.resolve(),
                            quality_folder_name=sub.name,
                            output_dir=soft_out_dir,
                            output_file=soft_out_file,
                        )
                    )

            direct_videos = cls._find_raw_videos_in_dir(target_dir, recursive=False)
            for vid in direct_videos:
                vid_ep = cls.extract_episode_tag(vid.name, fallback_season=fallback_s)
                if plan.season_ep_tag:
                    if vid_ep and vid_ep != plan.season_ep_tag:
                        continue
                    if not vid_ep and (len(direct_videos) != 1 or plan.softsub_targets):
                        continue
                soft_out_dir = target_dir / "SoftSub"
                plan.softsub_targets.append(
                    SoftSubTarget(
                        raw_video=vid,
                        quality_folder_name=target_dir.name,
                        output_dir=soft_out_dir,
                        output_file=soft_out_dir / cls.build_softsub_filename(vid),
                    )
                )

            # FullHD source identification is filename-aware. Prefer an explicit FullHD
            # folder, then H.264/x264/AVC/1080p names; never silently take the first
            # quality file when several plausible sources exist.
            explicit_fullhd = [
                target.raw_video
                for target in plan.softsub_targets
                if target.raw_video.parent.name.casefold() == "fullhd"
            ]
            candidates = list(dict.fromkeys(
                [
                    Path(item).resolve()
                    for item in explicit_fullhd
                ] + cls.find_fullhd_source_candidates(
                    [target.raw_video for target in plan.softsub_targets]
                )
            ))
            if not candidates and len(plan.softsub_targets) == 1:
                candidates = [plan.softsub_targets[0].raw_video]
            plan.fullhd_source_candidates = list(dict.fromkeys(
                Path(item).resolve() for item in candidates
            ))
            if len(plan.fullhd_source_candidates) == 1:
                plan.fullhd_source_video = plan.fullhd_source_candidates[0]
                plan.source_type = cls.detect_source_type(plan.fullhd_source_video.name)
            elif plan.fullhd_source_candidates:
                plan.fullhd_source_video = None

            if not plan.fullhd_source_candidates and plan.softsub_targets:
                candidate_videos = cls.find_fullhd_source_candidates(
                    [target.raw_video for target in plan.softsub_targets]
                )
                if not candidate_videos and len(plan.softsub_targets) == 1:
                    candidate_videos = [plan.softsub_targets[0].raw_video]
                plan.fullhd_source_candidates = candidate_videos
                if len(candidate_videos) == 1:
                    plan.fullhd_source_video = candidate_videos[0]
                    plan.source_type = cls.detect_source_type(plan.fullhd_source_video.name)

        else:
            paths = cls.create_movie_structure(target_dir)
            plan = AutomationPlan(
                is_series=False,
                category="Movies",
                base_dir=target_dir,
                series_title=target_dir.name,
                season_ep_tag="",
                fa_sub_dest_dir=paths["srt"],
                en_sub_dest_dir=paths["srt"],
            )

            for f in paths["srt"].iterdir():
                if f.is_file() and f.suffix.lower() in (".srt", ".ass"):
                    low_name = f.name.casefold()
                    if (
                        ".en." in low_name
                        or low_name.startswith("en_")
                        or low_name.endswith(("_en.srt", "_en.ass", "_en.ssa"))
                    ):
                        plan.existing_en_sub = f.resolve()
                        break

            raw_videos = cls._find_raw_videos_in_dir(target_dir, recursive=True)
            for vid in raw_videos:
                soft_name = await cls.build_movie_release_filename(
                    vid, vid.parent.name, target_dir.name, softsub=True,
                )
                soft_out_file = paths["softsub"] / soft_name
                plan.softsub_targets.append(
                    SoftSubTarget(
                        raw_video=vid.resolve(),
                        quality_folder_name=vid.parent.name if vid.parent != target_dir else vid.name,
                        output_dir=paths["softsub"],
                        output_file=soft_out_file,
                    )
                )

            if raw_videos:
                candidates = cls.find_fullhd_source_candidates(raw_videos)
                # If no filename advertises FullHD/H.264, keep all movie videos
                # as explicit choices instead of guessing the first one.
                if not candidates:
                    candidates = [Path(item).resolve() for item in raw_videos]
                plan.fullhd_source_candidates = list(dict.fromkeys(candidates))
                if len(plan.fullhd_source_candidates) == 1:
                    plan.fullhd_source_video = plan.fullhd_source_candidates[0]
                    plan.source_type = cls.detect_source_type(plan.fullhd_source_video.name)

        if plan.category != "Anime" and not plan.existing_en_sub and plan.softsub_targets:
            search_vids = (
                list(plan.fullhd_source_candidates)
                + ([plan.fullhd_source_video] if plan.fullhd_source_video else [])
                + [t.raw_video for t in plan.softsub_targets]
            )
            seen_v = set()
            for candidate_vid in search_vids:
                if not candidate_vid or str(candidate_vid) in seen_v:
                    continue
                seen_v.add(str(candidate_vid))
                try:
                    tracks = await MKVService.list_subtitle_tracks(candidate_vid)
                    text_tracks = [
                        t for t in tracks
                        if any(
                            k in str(t.get("codec", "")).lower()
                            for k in ("subrip", "srt", "ass", "ssa", "utf8", "text")
                        )
                    ]
                    eng_tracks = [
                        t for t in text_tracks
                        if MKVService._is_english_track(t)
                    ]

                    # اگر زبان ترک und است، فقط وقتی دقیقاً یک ترک متنیِ نامشخص وجود دارد
                    # و هیچ ترک انگلیسی/فارسی صریحی پیدا نشده، آن را به‌عنوان نامزد احتمالی می‌پذیریم.
                    if not eng_tracks and len(text_tracks) == 1:
                        only_track = text_tracks[0]
                        lang = str(only_track.get("language", "und")).lower()
                        lang_ietf = str(only_track.get("language_ietf", "")).lower()
                        track_name = str(only_track.get("track_name", "")).lower()
                        if (
                            lang in ("", "und", "unknown")
                            and lang_ietf in ("", "und", "unknown")
                            and not re.search(
                                r"(?<![a-z0-9])(?:persian|farsi|فارسی|fa|per|fas)(?![a-z0-9])",
                                track_name,
                            )
                        ):
                            eng_tracks = [only_track]

                    if eng_tracks:
                        plan.embedded_en_source_video = candidate_vid
                        plan.embedded_en_track_id = eng_tracks[0]["id"]
                        plan.embedded_en_codec = eng_tracks[0]["codec"]
                        break
                except Exception:
                    continue

        return plan

    @classmethod
    async def analyze_multi_episodes_plan(
        cls,
        target_dir: Path,
        episode_tags: list[str]
    ) -> list[AutomationPlan]:
        if not episode_tags:
            single = await cls.analyze_and_build_plan(target_dir, None)
            return [single] if single.softsub_targets else []

        plans: list[AutomationPlan] = []
        for ep in sorted(set(episode_tags)):
            p = await cls.analyze_and_build_plan(target_dir, ep)
            if p.softsub_targets:
                plans.append(p)
        return plans

    @classmethod
    async def extract_english_subtitle_for_plan(
            cls,
            plan: AutomationPlan,
            preferred_video: Path | None = None,
    ) -> Path | None:
        """
        Find and extract one explicit English text subtitle for this movie/episode.
        Tracks with unknown (und) or non-English languages are NOT auto-assumed as English.
        """
        if plan.existing_en_sub and plan.existing_en_sub.is_file():
            try:
                if plan.existing_en_sub.stat().st_size > 0:
                    return plan.existing_en_sub.resolve()
            except OSError:
                pass

        from services.subtitle_service import SubtitleService

        fallback_season = cls.extract_season_num_from_dir(plan.base_dir)
        sidecar_candidates: list[tuple[int, Path]] = []
        untagged_sidecars: list[tuple[int, Path]] = []
        allow_untagged_series_sidecar = (
                not plan.is_series
                or len(cls.scan_available_episodes_in_season(plan.base_dir)) <= 1
        )
        for sidecar in plan.base_dir.rglob("*"):
            if not sidecar.is_file() or sidecar.suffix.casefold() not in (".srt", ".ass", ".ssa"):
                continue
            try:
                relative_parts = sidecar.relative_to(plan.base_dir).parts[:-1]
            except ValueError:
                relative_parts = sidecar.parent.parts
            parent_parts = [part.casefold() for part in relative_parts]
            if any(
                    part in {
                        "fa", "fas", "farsi", "persian", "softsub", "hardsub",
                        "sub", "subs", "_incoming_downloads", "_rejected_downloads",
                    }
                    or part.startswith("_")
                    or "softsub" in part
                    or "hardsub" in part
                    for part in parent_parts
            ):
                continue

            low_name = sidecar.name.casefold()
            explicit_en_name = (
                    ".en." in low_name
                    or ".eng." in low_name
                    or ".english." in low_name
                    or low_name.startswith(("en_", "eng_", "english_"))
                    or low_name.endswith((
                "_en.srt", "_en.ass", "_en.ssa",
                "_eng.srt", "_eng.ass", "_eng.ssa",
                "_english.srt", "_english.ass", "_english.ssa",
            ))
            )
            in_en_folder = any(part in {"en", "eng", "english"} for part in parent_parts)
            if not explicit_en_name and not in_en_folder:
                continue

            try:
                if sidecar.stat().st_size <= 0:
                    continue
            except OSError:
                continue

            en_tag = cls.extract_episode_tag(sidecar.name, fallback_season=fallback_season)
            score = (100 if explicit_en_name else 50) + (20 if in_en_folder else 0)
            if plan.is_series:
                expected_tag = str(plan.season_ep_tag or "").upper()
                if en_tag and en_tag.upper() != expected_tag:
                    continue
                if en_tag:
                    sidecar_candidates.append((score + 40, sidecar.resolve()))
                elif allow_untagged_series_sidecar:
                    untagged_sidecars.append((score, sidecar.resolve()))
            else:
                sidecar_candidates.append((score, sidecar.resolve()))

        if not sidecar_candidates and plan.is_series and len(untagged_sidecars) == 1:
            sidecar_candidates = untagged_sidecars

        if sidecar_candidates:
            sidecar_candidates.sort(
                key=lambda item: (-item[0], item[1].name.casefold(), str(item[1]).casefold())
            )
            sidecar_source = sidecar_candidates[0][1]
            extension = sidecar_source.suffix.lower()
            standard_name = cls.build_standard_subtitle_filename(
                plan.series_title,
                plan.season_ep_tag if plan.is_series else "",
                cls.detect_source_type(sidecar_source.name),
                "EN",
                extension,
                year=cls.extract_media_year(
                    plan.series_title,
                    plan.base_dir.name,
                    plan.base_dir.parent.name,
                    sidecar_source.name,
                ),
            )
            plan.en_sub_dest_dir.mkdir(parents=True, exist_ok=True)
            sidecar_destination = plan.en_sub_dest_dir / standard_name
            if sidecar_source.resolve() != sidecar_destination.resolve():
                shutil.copy2(sidecar_source, sidecar_destination)
            if sidecar_destination.suffix.casefold() == ".srt":
                SubtitleService.normalize_srt_utf8(sidecar_destination)
            plan.existing_en_sub = sidecar_destination.resolve()
            return plan.existing_en_sub

        ordered: list[Path] = []
        for item in (
                            [preferred_video] if preferred_video else []
                    ) + list(plan.fullhd_source_candidates or []) + (
                            [plan.fullhd_source_video] if plan.fullhd_source_video else []
                    ) + [target.raw_video for target in plan.softsub_targets]:
            if not item:
                continue
            candidate = Path(item).resolve()
            if candidate.is_file() and candidate not in ordered:
                ordered.append(candidate)

        if not ordered:
            ordered = cls._find_raw_videos_in_dir(plan.base_dir, recursive=True)

        for video_path in ordered:
            try:
                tracks = await MKVService.list_subtitle_tracks(video_path)
            except Exception as exc:
                logging.debug("Could not inspect subtitle tracks in %s: %s", video_path, exc)
                continue

            text_tracks = []
            for track in tracks:
                extension = MKVService.subtitle_extension_for_codec(track.get("codec", ""))
                if track.get("id") is not None and extension in (".srt", ".ass", ".ssa"):
                    text_tracks.append((track, extension))

            english_tracks = [
                item for item in text_tracks
                if MKVService._is_english_track(item[0])
            ]
            if not english_tracks:
                continue

            english_tracks.sort(key=lambda item: (
                not bool(item[0].get("default_track")),
                {".srt": 0, ".ass": 1, ".ssa": 2}.get(item[1], 9),
                int(item[0].get("id", 0)),
            ))
            track, extension = english_tracks[0]
            try:
                return await cls.extract_specific_track_as_english_for_plan(
                    plan=plan,
                    video_path=video_path,
                    track_id=int(track["id"]),
                    track_codec=str(track.get("codec") or ""),
                )
            except Exception:
                logging.exception(
                    "Could not extract EN subtitle from %s (track %s)",
                    video_path,
                    track.get("id"),
                )
                continue

        return None

    @classmethod
    async def find_non_english_or_und_subtitle_tracks_for_plan(
            cls,
            plan: AutomationPlan,
    ) -> list[dict]:
        """
        پیدا کردن ترک‌های زیرنویس متنی که زبانشان انگلیسی نیست یا نامشخص (und) است
        تا در زمان اتوماسیون به کاربر اطلاع داده شود و در صورت تایید به عنوان EN اضافه شوند.
        """
        ordered: list[Path] = []
        for item in (
                list(plan.fullhd_source_candidates or [])
                + ([plan.fullhd_source_video] if plan.fullhd_source_video else [])
                + [target.raw_video for target in plan.softsub_targets]
        ):
            if not item:
                continue
            candidate = Path(item).resolve()
            if candidate.is_file() and candidate not in ordered:
                ordered.append(candidate)

        if not ordered:
            ordered = cls._find_raw_videos_in_dir(plan.base_dir, recursive=True)

        plan_key = plan.season_ep_tag if (plan.is_series and plan.season_ep_tag) else "MOVIE"
        found: list[dict] = []

        for video_path in ordered:
            try:
                tracks = await MKVService.list_subtitle_tracks(video_path)
            except Exception as exc:
                logging.debug("Could not inspect subtitle tracks in %s: %s", video_path, exc)
                continue

            for track in tracks:
                extension = MKVService.subtitle_extension_for_codec(track.get("codec", ""))
                if track.get("id") is None or extension not in (".srt", ".ass", ".ssa"):
                    continue
                if MKVService._is_english_track(track):
                    continue

                lang = str(track.get("language", "und") or "und").strip()
                track_name = str(track.get("track_name", "") or "").strip()
                # رد کردن ترک‌های صراحتاً فارسی
                if lang.casefold() in ("per", "fas", "fa", "persian", "farsi") or re.search(
                        r"(?<![a-z0-9])(?:persian|farsi|فارسی|fa|per|fas)(?![a-z0-9])",
                        track_name.casefold(),
                ):
                    continue

                found.append({
                    "plan_key": plan_key,
                    "video_path": str(video_path),
                    "video_name": video_path.name,
                    "track_id": int(track["id"]),
                    "language": lang or "und",
                    "track_name": track_name,
                    "codec": str(track.get("codec") or ""),
                    "extension": extension,
                })

            # اگر از اولین فایل دارای ترک، کاندیدا پیدا شد، برای جلوگیری از تکرار کیفیت‌ها کافی است
            if found:
                break

        return found

    @classmethod
    async def extract_specific_track_as_english_for_plan(
            cls,
            plan: AutomationPlan,
            video_path: Path,
            track_id: int,
            track_codec: str = "",
    ) -> Path:
        """استخراج یک ترک مشخص از ویدئو و ذخیره آن با نام استاندارد زیرنویس انگلیسی (EN)."""
        from services.subtitle_service import SubtitleService

        video_path = Path(video_path).resolve()
        extension = MKVService.subtitle_extension_for_codec(track_codec)
        if extension not in (".srt", ".ass", ".ssa"):
            extension = ".srt"

        episode = plan.season_ep_tag if plan.is_series else ""
        year = cls.extract_media_year(
            plan.series_title,
            plan.base_dir.name,
            plan.base_dir.parent.name,
            video_path.name,
        )
        filename = cls.build_standard_subtitle_filename(
            plan.series_title,
            episode,
            cls.detect_source_type(video_path.name),
            "EN",
            extension,
            year=year,
        )
        plan.en_sub_dest_dir.mkdir(parents=True, exist_ok=True)
        destination = plan.en_sub_dest_dir / filename

        temp_path = destination.with_name(
            f"_temp_en_extract_{time.time_ns()}{extension}"
        )
        try:
            await MKVService.extract_subtitle_track(
                video_path=video_path,
                track_id=int(track_id),
                output_sub_path=temp_path,
                track_codec=track_codec,
            )
            if not temp_path.is_file() or temp_path.stat().st_size <= 0:
                raise RuntimeError("استخراج زیرنویس خروجی خالی تولید کرد.")
            SubtitleService.normalize_subtitle_file(temp_path)
            temp_path.replace(destination)
            plan.existing_en_sub = destination.resolve()
            plan.embedded_en_source_video = video_path
            plan.embedded_en_track_id = int(track_id)
            plan.embedded_en_codec = str(track_codec or "")
            return plan.existing_en_sub
        except Exception:
            temp_path.unlink(missing_ok=True)
            raise

    @classmethod
    async def run_softsub_and_optional_upload(
            cls,
            raw_video: Path,
            output_file: Path,
            fa_sub: Path | None,
            en_sub: Path | None,
            brand: str,
            auto_upload: bool = False,
            progress_callback: Callable[[str], None] = None,
            server_id: str | None = None,
            keep_old_subs: bool = True,
    ) -> Path:
        raw_video = Path(raw_video).expanduser().resolve()
        output_file = Path(output_file).expanduser().resolve()
        if not raw_video.is_file() or raw_video.stat().st_size <= 0:
            raise FileNotFoundError(f"ویدئوی خام برای SoftSub پیدا نشد یا خالی است: {raw_video}")
        if raw_video == output_file:
            raise RuntimeError(
                "مسیر خروجی SoftSub با فایل خام یکسان است؛ برای جلوگیری از جایگزینی فایل اصلی، عملیات متوقف شد."
            )
        fa_sub = MKVService.validate_subtitle_file(fa_sub, "زیرنویس فارسی SoftSub")
        if en_sub is not None:
            en_sub = MKVService.validate_subtitle_file(en_sub, "زیرنویس انگلیسی SoftSub")

        logging.info(
            "[SOFTSUB AUTOMATION DISPATCH] module=%s python=%s cwd=%s "
            "source=%s output=%s fa_sub=%s en_sub=%s",
            Path(__file__).resolve(),
            __import__("sys").executable,
            Path.cwd(),
            raw_video,
            output_file,
            fa_sub,
            en_sub,
        )
        output_file.parent.mkdir(parents=True, exist_ok=True)
        res_path = await MKVService.merge_softsub(
            video_path=raw_video,
            output_path=output_file,
            fa_sub_path=fa_sub,
            en_sub_path=en_sub,
            keep_old_subs=keep_old_subs,
            custom_brand=brand,
            sync_mode="none",
            sync_offset_ms=0,
            progress_callback=progress_callback,
            require_fa_sub=True,
        )
        if not res_path.is_file() or res_path.stat().st_size <= 0:
            raise RuntimeError(f"خروجی SoftSub معتبر ساخته نشد: {res_path}")

        # Re-verify the exact file that will be recorded and uploaded.
        # For MP4 this decodes each required text track back to SRT and
        # rejects empty subtitle streams before an upload task is queued.
        if res_path.suffix.casefold() in {".mp4", ".m4v", ".mov"}:
            verified_tracks = await MKVService._verify_muxed_subtitle_languages(
                res_path,
                require_fa_sub=True,
                fa_lang=cfg.get("branding", "sub_fa_language", "per"),
                en_lang=cfg.get("branding", "sub_en_language", "eng"),
            )
            logging.info(
                "[SOFTSUB VERIFIED] source=%s output=%s bytes=%d subtitle_tracks=%s",
                raw_video,
                res_path,
                res_path.stat().st_size,
                [
                    {
                        "stream": track.get("id"),
                        "language": track.get("language"),
                        "codec": track.get("codec"),
                        "default": track.get("default_track"),
                    }
                    for track in verified_tracks
                ],
            )

        if res_path.exists():
            try:
                from services.daily_activity_service import DailyActivityService
                DailyActivityService.record_media_file(
                    "softsub_created",
                    res_path,
                    source=raw_video.name,
                )
            except Exception:
                logging.exception("Could not record SoftSub output")
        if auto_upload and res_path.exists():
            remote_dir = cls.compute_server_remote_dir_for_local_path(res_path)
            await queue_engine.add_task(
                "upload",
                f"AutoUpload [{remote_dir}]: {res_path.name}",
                CuteFTPService.upload_path,
                res_path,
                remote_dir,
                server_id=server_id,
            )
        return res_path


    @staticmethod
    def _is_english_subtitle_filename(filename: str) -> bool:
        stem = Path(str(filename or "")).stem.casefold().strip()
        if stem.startswith(("en_", "eng_", "english_")) or stem in {"en", "eng", "english"}:
            return True
        # Treat a language tag as English only at the filename tail (optionally
        # followed by common subtitle attributes), not when "English" is part of
        # a legitimate media title such as "The English Teacher".
        return bool(re.search(
            r"(?:^|[._-])(?:en|eng|english)(?:[._-](?:forced|default|sdh|cc|hi|v\d+))?$",
            stem,
        ))

    @classmethod
    def collect_all_local_fa_subtitles_in_dir(cls, base_dir: Path, fa_dest_dir: Path) -> list[Path]:
        from services.subtitle_service import SubtitleService

        search_dirs = [
            fa_dest_dir,
            base_dir / "srt" / "FA",
            base_dir / "srt",
            base_dir / "sub",
            base_dir,
        ]
        for sub in base_dir.iterdir():
            if sub.is_dir() and sub.name.lower() not in ("en", "eng", "english", "softsub", "hardsub") and not sub.name.lower().endswith(("-softsub", "-hardsub")):
                if sub not in search_dirs:
                    search_dirs.append(sub)

        for d in list(search_dirs):
            if not d or not d.exists():
                continue
            for ext_pat in ("*.zip", "*.rar", "*.7z"):
                for arch_f in list(d.glob(ext_pat)):
                    try:
                        SubtitleService.extract_archive(arch_f, fa_dest_dir, remove_archive=True)
                    except Exception:
                        pass

        found: list[Path] = []
        seen: set[str] = set()
        for d in search_dirs:
            if not d or not d.exists() or d.name.upper() == "EN":
                continue
            for f in sorted(d.iterdir(), key=lambda x: x.name.lower()):
                if not f.is_file() or f.suffix.lower() not in (".srt", ".ass", ".ssa"):
                    continue
                low_name = f.name.lower()
                if low_name.startswith(("_temp_", "_soft_clean_")) or cls._is_english_subtitle_filename(low_name):
                    continue
                res_str = str(f.resolve())
                if res_str not in seen:
                    seen.add(res_str)
                    found.append(f.resolve())
        return found

    @classmethod
    def find_local_fa_subtitles(cls, plan: AutomationPlan) -> list[Path]:
        all_subs = cls.collect_all_local_fa_subtitles_in_dir(plan.base_dir, plan.fa_sub_dest_dir)
        validated_subs: list[Path] = []
        for subtitle_path in all_subs:
            try:
                validated_subs.append(MKVService.validate_subtitle_file(subtitle_path, "زیرنویس فارسی"))
            except (OSError, ValueError) as exc:
                logging.warning("Skipping invalid Persian subtitle candidate %s: %s", subtitle_path, exc)
        all_subs = validated_subs
        if not plan.is_series or not plan.season_ep_tag:
            return all_subs

        matched = []
        fallback_s = cls.extract_season_num_from_dir(plan.base_dir)
        target_ep_num = int(plan.season_ep_tag.split("E")[1])

        for f in all_subs:
            sub_ep = cls.extract_episode_tag(f.name, fallback_season=fallback_s)
            sub_ep_num = cls.extract_episode_number_only(f.name)

            if sub_ep:
                if sub_ep == plan.season_ep_tag:
                    matched.append(f)
            elif sub_ep_num is not None:
                if sub_ep_num == target_ep_num:
                    matched.append(f)
            else:
                matched.append(f)

        return matched

    @classmethod
    def match_subtitles_to_episodes(
        cls,
        plans: list[AutomationPlan],
        candidate_subs: list[Path]
    ) -> dict[str, Path]:
        if not plans:
            return {}

        if not plans[0].is_series or not plans[0].season_ep_tag:
            return {"MOVIE": candidate_subs[0]} if candidate_subs else {}

        fallback_s = cls.extract_season_num_from_dir(plans[0].base_dir)
        mapping: dict[str, Path] = {}
        used_subs: set[str] = set()

        for p in plans:
            ep_tag = p.season_ep_tag
            for sub in candidate_subs:
                if str(sub) in used_subs:
                    continue
                if cls.extract_episode_tag(sub.name, fallback_season=fallback_s) == ep_tag:
                    mapping[ep_tag] = sub
                    used_subs.add(str(sub))
                    break

        for p in plans:
            ep_tag = p.season_ep_tag
            if ep_tag in mapping:
                continue
            target_ep_num = int(ep_tag.split("E")[1])
            for sub in candidate_subs:
                if str(sub) in used_subs:
                    continue
                explicit_s_e = cls.extract_episode_tag(sub.name, fallback_season=fallback_s)
                if explicit_s_e and explicit_s_e != ep_tag:
                    continue
                if cls.extract_episode_number_only(sub.name) == target_ep_num:
                    mapping[ep_tag] = sub
                    used_subs.add(str(sub))
                    break

        if len(plans) == 1 and not mapping:
            untagged_subs = [
                sub for sub in candidate_subs
                if str(sub) not in used_subs
                and cls.extract_episode_tag(sub.name, fallback_season=fallback_s) is None
                and cls.extract_episode_number_only(sub.name) is None
            ]
            if len(untagged_subs) == 1:
                mapping[plans[0].season_ep_tag] = untagged_subs[0]

        return mapping

    @classmethod
    async def find_remote_fa_subtitle(cls, plan: AutomationPlan) -> dict | None:
        if plan.is_series and not plan.season_ep_tag:
            return None
        remote_dirs = dict.fromkeys(
            cls.compute_server_remote_dir_for_local_path(path)
            for path in (
                plan.fa_sub_dest_dir, plan.base_dir / "srt" / "FA",
                plan.base_dir / "srt", plan.base_dir / "sub", plan.base_dir,
            )
        )
        listings = await asyncio.gather(*(CuteFTPService.inspect_remote_dir_all(path) for path in remote_dirs))
        candidates: dict[Path, dict] = {}
        for servers in listings:
            for server in servers:
                if not server.get("server_id") or not server.get("exists"):
                    continue
                for filename in sorted(server.get("files", []), key=str.casefold):
                    if "/" in filename or "\\" in filename:
                        continue
                    path = Path(server["remote_dir"]) / filename
                    if path.suffix.casefold() not in (".srt", ".ass", ".ssa"):
                        continue
                    if filename.casefold().startswith(("_temp_", "_soft_clean_")) or cls._is_english_subtitle_filename(filename):
                        continue
                    candidates.setdefault(path, {
                        "filename": filename,
                        "remote_dir": server["remote_dir"],
                        "server_id": server["server_id"],
                        "server_name": server["server_name"],
                    })
        match = cls.match_subtitles_to_episodes([plan], list(candidates)).get(plan.season_ep_tag or "MOVIE")
        return candidates.get(match)

    @staticmethod
    def resolve_preset_for_quality(quality_tag: str) -> dict[str, str]:
        presets = HandBrakeService.list_available_presets()
        if not presets:
            raise RuntimeError("هیچ فایل پریست (.json) در پوشه presets یافت نشد!")

        q_num = quality_tag.lower().replace("p", "")
        for pr in presets:
            if q_num in pr["name"].lower() or q_num in Path(pr["file"]).stem.lower():
                return pr
        return presets[0]

    @classmethod
    def build_hardsub_targets(
        cls,
        plan: AutomationPlan,
        hardsub_srt_qualities: list[str]
    ) -> list[HardSubTarget]:
        if not plan.fullhd_source_video or not hardsub_srt_qualities:
            return []

        targets: list[HardSubTarget] = []
        order_map = {"1080p": 1, "720p": 2, "480p": 3}

        for q_tag in sorted(set(hardsub_srt_qualities), key=lambda q: order_map.get(q, 9)):
            pr = cls.resolve_preset_for_quality(q_tag)
            out_name = cls.build_hardsub_srt_filename(
                plan.series_title, plan.season_ep_tag, q_tag, plan.source_type
            )
            out_dir = (plan.base_dir / f"{q_tag}-HardSub") if plan.is_series else (plan.base_dir / "Hardsub")
            targets.append(
                HardSubTarget(
                    mode="hardsub_srt",
                    quality_tag=q_tag,
                    source_video=plan.fullhd_source_video,
                    output_dir=out_dir,
                    output_file=out_dir / out_name,
                    preset_file=pr["file"],
                    preset_name=pr["name"],
                )
            )
        return targets

    @classmethod
    def build_manual_encode_ass_targets(
        cls,
        source_videos: list[Path],
        selected_qualities: list[str]
    ) -> list[HardSubTarget]:
        order_map = {"1080p": 1, "720p": 2, "480p": 3}
        sorted_quals = sorted(set(selected_qualities), key=lambda q: order_map.get(q, 9))
        logo_ass = cls.get_movie_ass_logo_path()
        targets: list[HardSubTarget] = []

        for vid in source_videos:
            vid = Path(vid).resolve()
            fallback_s = cls.extract_season_num_from_dir(vid.parent)
            ep_tag = cls.extract_episode_tag(vid.name, fallback_season=fallback_s) or ""
            src_type = cls.detect_source_type(vid.name)

            if vid.parent.name.lower() in ("fullhd", "1080p", "720p", "480p") or cls.SEASON_DIR_REGEX.match(vid.parent.parent.name):
                season_or_movie_dir = vid.parent.parent
                series_title = (
                    season_or_movie_dir.parent.name
                    if cls.SEASON_DIR_REGEX.match(season_or_movie_dir.name)
                    else season_or_movie_dir.name
                )
                is_series = True
            elif cls.SEASON_DIR_REGEX.match(vid.parent.name):
                season_or_movie_dir = vid.parent
                series_title = season_or_movie_dir.parent.name
                is_series = True
            else:
                season_or_movie_dir = vid.parent
                series_title = vid.stem
                is_series = bool(ep_tag)

            for q_tag in sorted_quals:
                pr = cls.resolve_preset_for_quality(q_tag)
                out_name = cls.build_encode_ass_filename(series_title, ep_tag, q_tag, src_type)
                out_dir = (season_or_movie_dir / f"{q_tag}-EBTV") if is_series else season_or_movie_dir
                targets.append(
                    HardSubTarget(
                        mode="encode_ass",
                        quality_tag=q_tag,
                        source_video=vid,
                        output_dir=out_dir,
                        output_file=out_dir / out_name,
                        preset_file=pr["file"],
                        preset_name=pr["name"],
                        ass_logo_path=logo_ass,
                    )
                )
        return targets


    @classmethod
    async def run_hardsub_or_encode_and_optional_upload(
        cls,
        ht: HardSubTarget,
        fa_sub: Path | None,
        en_sub: Path | None,
        brand: str,
        auto_upload: bool = False,
        progress_callback: Callable[[str], None] = None,
        server_id: str | None = None,
    ) -> Path:
        ht.output_dir.mkdir(parents=True, exist_ok=True)
        if ht.mode == "hardsub_srt":
            fa_sub = MKVService.validate_subtitle_file(fa_sub, "زیرنویس فارسی HardSub")
        needs_english_track = (
            ht.mode == "encode_ass"
            or bool(fa_sub and fa_sub.suffix.casefold() in (".ass", ".ssa"))
        )
        if needs_english_track and en_sub is not None:
            en_sub = MKVService.validate_subtitle_file(en_sub, "زیرنویس انگلیسی HardSub")
        elif not needs_english_track:
            en_sub = None
        if ht.mode == "encode_ass":
            logo_ass = ht.ass_logo_path or cls.get_movie_ass_logo_path()
            if not logo_ass.exists():
                raise FileNotFoundError(
                    f"فایل movie.ass در مسیر {logo_ass} یافت نشد! لطفاً فایل movie.ass را کنار پوشه presets قرار دهید."
                )
            res_path = await HandBrakeService.encode_hardsub(
                video_path=ht.source_video,
                primary_srt_path=logo_ass,
                output_media_path=ht.output_file,
                preset_json_path=ht.preset_file,
                preset_name=ht.preset_name,
                secondary_en_srt_path=en_sub,
                sync_mode="none",
                sync_offset_ms=0,
                target_resolution=ht.quality_tag,
                progress_callback=progress_callback
            )
        else:
            is_fa_ass = bool(fa_sub and fa_sub.suffix.lower() in (".ass", ".ssa"))
            secondary_en = en_sub if is_fa_ass else None
            res_path = await HandBrakeService.encode_hardsub(
                video_path=ht.source_video,
                primary_srt_path=fa_sub,
                output_media_path=ht.output_file,
                preset_json_path=ht.preset_file,
                preset_name=ht.preset_name,
                secondary_en_srt_path=secondary_en,
                sync_mode="none",
                sync_offset_ms=0,
                target_resolution=ht.quality_tag,
                progress_callback=progress_callback
            )

        if not res_path.is_file() or res_path.stat().st_size <= 0:
            raise RuntimeError(f"خروجی HardSub معتبر ساخته نشد: {res_path}")
        if res_path.exists():
            try:
                from services.daily_activity_service import DailyActivityService
                DailyActivityService.record_media_file(
                    "hardsub_created",
                    res_path,
                    source=ht.source_video.name,
                    mode=ht.mode,
                )
            except Exception:
                logging.exception("Could not record HardSub output")
        if auto_upload and res_path.exists():
            remote_dir = cls.compute_server_remote_dir_for_local_path(res_path)
            await queue_engine.add_task(
                "upload",
                f"AutoUpload [{remote_dir}]: {res_path.name}",
                CuteFTPService.upload_path,
                res_path,
                remote_dir,
                server_id=server_id,
            )
        return res_path
