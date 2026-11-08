import logging
import re
import time
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
        m = re.search(r"(?:[Ee][Pp]?(?:isode)?[\s._\-]*|\s-\s|[\s._\-\[]E)(\d{1,3})(?:v\d)?(?:[\s._\-\]\[]|$)", cleaned)
        if m:
            val = int(m.group(1))
            if 0 <= val <= 999:
                return val
        return None

    @staticmethod
    def detect_source_type(text: str) -> str:
        low = text.lower()
        if "bluray" in low or "blu-ray" in low or "bdrip" in low or "brrip" in low:
            return "BluRay"
        return "WEB-DL"

    @classmethod
    def format_clean_title(cls, raw_title: str) -> str:
        cleaned = re.sub(r'[<>:"/\\|?*]', "", raw_title).strip()
        cleaned = re.split(
            r"[\s._\-]+(?:[Ss]\d{1,2}[Ee]\d{1,3}|(?:19|20)\d{2}|2160p|1080p|720p|480p|FullHD|WEB-DL|WEBRip|BluRay)\b",
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
                parent_low = f.parent.name.lower()
                if parent_low in ("softsub", "hardsub", "srt", "sub") or parent_low.endswith(("-softsub", "-hardsub")):
                    continue

            videos.append(f.resolve())

        return sorted(videos, key=lambda x: x.name.lower())

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

    @classmethod
    async def build_standard_raw_filename_for_folder(
        cls,
        video_path: Path,
        folder_name: str,
        series_or_movie_title: str,
        season_ep_tag: str | None = None,
        is_series: bool = True
    ) -> str:
        clean_name = cls.format_clean_title(series_or_movie_title)
        fallback_s = cls.extract_season_num_from_dir(video_path.parent)
        ep_tag = season_ep_tag or cls.extract_episode_tag(video_path.name, fallback_season=fallback_s) or ""
        src_type = cls.detect_source_type(video_path.name)
        ch_tag = await cls.detect_audio_channels_tag(video_path)
        f_low = folder_name.lower()
        orig_ext = video_path.suffix.lower() or ".mkv"

        if is_series and ep_tag:
            if "1080p-x265-judas" in f_low or "judas" in f_low:
                return f"{clean_name}.{ep_tag}.1080p.10bit.{src_type}.x265.Judas.EBTV.mkv"
            if "1080p-x265-psa" in f_low:
                return f"{clean_name}.{ep_tag}.1080p.10bit.{src_type}.{ch_tag}.x265.PSA.EBTV.mkv"
            if "720p-x265-psa" in f_low:
                return f"{clean_name}.{ep_tag}.720p.10bit.{src_type}.2CH.x265.PSA.EBTV.mkv"
            if "1080p-pahe" in f_low:
                return f"{clean_name}.{ep_tag}.1080p.{src_type}.6CH.Pahe.EBTV.mkv"
            if "720p-pahe" in f_low:
                return f"{clean_name}.{ep_tag}.720p.{src_type}.Pahe.EBTV.mkv"
            if "480p-rmteam" in f_low or "480p-rmt" in f_low:
                return f"{clean_name}.{ep_tag}.480p.{src_type}.RMTeam.EBTV.mkv"
            if f_low == "fullhd":
                net, enc_name = cls.detect_encoder_and_network(video_path.name)
                return f"{clean_name}.{ep_tag}.1080p.{net}.{src_type}.FullHD.{enc_name}.EBTV.mkv"
            if "1080p-ebtv" in f_low:
                return f"{clean_name}.{ep_tag}.1080p.{src_type}.6CH.EBTV.mp4"
            if "720p-ebtv" in f_low:
                return f"{clean_name}.{ep_tag}.720p.{src_type}.EBTV.mp4"
            if "480p-ebtv" in f_low:
                return f"{clean_name}.{ep_tag}.480p.{src_type}.EBTV.mp4"

            custom_parts = [p for p in folder_name.split("-") if p]
            custom_tag = ".".join(custom_parts)
            return f"{clean_name}.{ep_tag}.{src_type}.{custom_tag}.EBTV{orig_ext}"

        year_match = re.search(r"\b((?:19|20)\d{2})\b", video_path.name) or re.search(r"\b((?:19|20)\d{2})\b", series_or_movie_title)
        year_str = f".{year_match.group(1)}" if year_match else ""
        res_match = re.search(r"\b(2160p|1080p|720p|480p)\b", video_path.name, flags=re.IGNORECASE)
        res_str = res_match.group(1).lower() if res_match else "1080p"

        encoder_tag = ""
        for enc_cand in ("PSA", "Pahe", "YTS", "RARBG", "RMTeam", "Tigole", "GalaxyRG"):
            if enc_cand.lower() in video_path.name.lower():
                encoder_tag = f".{enc_cand}"
                break
        x265_tag = ".10bit.x265" if ("x265" in video_path.name.lower() or "hevc" in video_path.name.lower()) else ""
        ch_part = f".{ch_tag}" if ch_tag == "6CH" else ""

        return f"{clean_name}{year_str}.{res_str}.{src_type}{ch_part}{x265_tag}{encoder_tag}.EBTV{orig_ext}"

    @classmethod
    def match_filename_from_server_siblings(
        cls,
        server_filenames: list[str],
        new_ep_tag: str
    ) -> str | None:
        if not server_filenames or not new_ep_tag:
            return None

        video_exts = (".mkv", ".mp4", ".avi", ".ts", ".mov", ".m4v", ".webm")
        valid_siblings = [
            f for f in server_filenames
            if f.lower().endswith(video_exts) and cls.EPISODE_REGEX.search(f)
        ]
        if not valid_siblings:
            return None

        template_name = sorted(valid_siblings)[-1]
        new_e_only = new_ep_tag.split("E")[1]

        def _replacer(match: re.Match) -> str:
            s_part = match.group(1)
            return f"S{int(s_part):02d}E{new_e_only}"

        return cls.EPISODE_REGEX.sub(_replacer, template_name, count=1)

    @classmethod
    def build_standard_subtitle_filename(
        cls,
        series_or_movie_title: str,
        season_ep_tag: str,
        source_type: str,
        lang: str,
        ext: str = ".srt"
    ) -> str:
        clean_name = cls.format_clean_title(series_or_movie_title)
        lang_up = lang.upper()
        if not ext.startswith("."):
            ext = f".{ext}"
        if season_ep_tag:
            return f"{clean_name}.{season_ep_tag}.{source_type}.{lang_up}{ext}"
        return f"{clean_name}.{source_type}.{lang_up}{ext}"

    @classmethod
    def build_hardsub_srt_filename(
        cls,
        series_or_movie_title: str,
        season_ep_tag: str,
        quality_tag: str,
        source_type: str = "WEB-DL"
    ) -> str:
        clean_name = cls.format_clean_title(series_or_movie_title)
        if season_ep_tag:
            return f"{clean_name}.{season_ep_tag}.{source_type}.{quality_tag}.HardSub.EBTV.mp4"
        return f"{clean_name}.{source_type}.{quality_tag}.HardSub.EBTV.mp4"

    @classmethod
    def build_encode_ass_filename(
        cls,
        series_or_movie_title: str,
        season_ep_tag: str,
        quality_tag: str,
        source_type: str = "WEB-DL"
    ) -> str:
        clean_name = cls.format_clean_title(series_or_movie_title)
        ch_part = ".6CH" if quality_tag == "1080p" else ""
        if season_ep_tag:
            return f"{clean_name}.{season_ep_tag}.{source_type}.{quality_tag}{ch_part}.EBTV.mp4"
        return f"{clean_name}.{source_type}.{quality_tag}{ch_part}.EBTV.mp4"

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
        return cleaned

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
    def find_matching_workspace_folder_for_sub(cls, sub_filename: str) -> tuple[Path | None, list[str]]:
        ws = Path(cfg.get("paths", "workspace_dir", "./workspace")).resolve()
        if not ws.exists():
            return None, []

        ep_tag = cls.extract_episode_tag(sub_filename)
        clean_sub = re.sub(r"[^a-zA-Z0-9]+", " ", Path(sub_filename).stem).lower().split()
        title_tokens = [t for t in clean_sub if len(t) > 2 and not cls.EPISODE_REGEX.match(t)]

        candidate_roots = []
        for d in ws.iterdir():
            if not d.is_dir() or d.name.startswith("_") or d.name == "browser_profile":
                continue
            if d.name in cls.ROOT_CATEGORIES or d.name.lower() in ("movie", "movies", "serie", "series", "anime"):
                candidate_roots.extend([sub for sub in d.iterdir() if sub.is_dir()])
            else:
                candidate_roots.append(d)

        best_folder = None
        best_score = 0
        matched_eps: list[str] = []

        for top_dir in candidate_roots:
            top_tokens = set(re.sub(r"[^a-zA-Z0-9]+", " ", top_dir.name).lower().split())
            overlap = sum(1 for t in title_tokens[:4] if t in top_tokens)

            if ep_tag:
                s_num = int(ep_tag[1:3])
                season_candidates = [
                    top_dir / f"S{s_num:02d}",
                    top_dir / f"S{s_num}",
                    top_dir,
                ]
                for s_cand in season_candidates:
                    if s_cand.is_dir():
                        eps = cls.scan_available_episodes_in_season(s_cand)
                        if ep_tag in eps:
                            score = overlap + 10
                            if score > best_score:
                                best_score = score
                                best_folder = s_cand
                                matched_eps = [ep_tag]
            else:
                for sub_d in [top_dir] + [d for d in top_dir.iterdir() if d.is_dir() and cls.SEASON_DIR_REGEX.match(d.name)]:
                    if overlap > best_score:
                        best_score = overlap
                        best_folder = sub_d
                        matched_eps = cls.scan_available_episodes_in_season(sub_d)

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
                        if (f_ep and f_ep == plan.season_ep_tag) or plan.season_ep_tag.lower() in f.name.lower():
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
                        if vid_ep and vid_ep.upper() != plan.season_ep_tag.upper():
                            continue
                        if not vid_ep and len(videos_in_sub) > 1:
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

                    if low_name == "fullhd":
                        plan.fullhd_source_video = vid.resolve()
                        plan.source_type = cls.detect_source_type(vid.name)

            # ۲. اگر کاربر به جای پوشه فصل، مستقیماً یک زیرپوشه کیفیت یا پوشه حاوی ویدئو را انتخاب کرده بود:
            if not plan.softsub_targets:
                direct_vids = cls._find_raw_videos_in_dir(target_dir, recursive=False)
                for vid in direct_vids:
                    vid_ep = cls.extract_episode_tag(vid.name, fallback_season=fallback_s)
                    if plan.season_ep_tag and vid_ep and vid_ep.upper() != plan.season_ep_tag.upper():
                        continue
                    soft_out_dir = target_dir / "SoftSub"
                    soft_out_file = soft_out_dir / cls.build_softsub_filename(vid)
                    plan.softsub_targets.append(
                        SoftSubTarget(
                            raw_video=vid.resolve(),
                            quality_folder_name=target_dir.name,
                            output_dir=soft_out_dir,
                            output_file=soft_out_file,
                        )
                    )

            if not plan.fullhd_source_video and plan.softsub_targets:
                ten_eighty = [
                    t.raw_video for t in plan.softsub_targets
                    if "1080" in t.quality_folder_name.lower() or "1080" in t.raw_video.name.lower()
                ]
                plan.fullhd_source_video = ten_eighty[0] if ten_eighty else plan.softsub_targets[0].raw_video
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
                    if ".en." in f.name.lower() or f.name.lower().startswith("en_"):
                        plan.existing_en_sub = f.resolve()
                        break

            raw_videos = cls._find_raw_videos_in_dir(target_dir, recursive=True)
            for vid in raw_videos:
                soft_out_file = paths["softsub"] / cls.build_softsub_filename(vid)
                plan.softsub_targets.append(
                    SoftSubTarget(
                        raw_video=vid.resolve(),
                        quality_folder_name=vid.parent.name if vid.parent != target_dir else vid.name,
                        output_dir=paths["softsub"],
                        output_file=soft_out_file,
                    )
                )

            if raw_videos:
                plan.fullhd_source_video = raw_videos[0].resolve()
                plan.source_type = cls.detect_source_type(plan.fullhd_source_video.name)

        if plan.category != "Anime" and not plan.existing_en_sub and plan.softsub_targets:
            search_vids = (
                [plan.fullhd_source_video] + [t.raw_video for t in plan.softsub_targets]
                if plan.fullhd_source_video
                else [t.raw_video for t in plan.softsub_targets]
            )
            seen_v = set()
            for candidate_vid in search_vids:
                if not candidate_vid or str(candidate_vid) in seen_v:
                    continue
                seen_v.add(str(candidate_vid))
                try:
                    tracks = await MKVService.list_subtitle_tracks(candidate_vid)
                    eng_tracks = [
                        t for t in tracks
                        if MKVService._is_english_track(t)
                        and any(k in t["codec"].lower() for k in ("subrip", "srt", "ass", "ssa", "utf8", "text"))
                    ]
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
            if sub.is_dir() and sub.name.lower() not in ("en", "softsub", "hardsub") and not sub.name.lower().endswith(("-softsub", "-hardsub")):
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
                if low_name.startswith(("_temp_", "_soft_clean_", "en_")) or low_name.endswith((".en.srt", ".en.ass", "_en.srt")):
                    continue
                res_str = str(f.resolve())
                if res_str not in seen:
                    seen.add(res_str)
                    found.append(f.resolve())
        return found

    @classmethod
    def find_local_fa_subtitles(cls, plan: AutomationPlan) -> list[Path]:
        all_subs = cls.collect_all_local_fa_subtitles_in_dir(plan.base_dir, plan.fa_sub_dest_dir)
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
    async def run_softsub_and_optional_upload(
        cls,
        raw_video: Path,
        output_file: Path,
        fa_sub: Path | None,
        en_sub: Path | None,
        brand: str,
        auto_upload: bool = False,
        progress_callback: Callable[[str], None] = None
    ) -> Path:
        output_file.parent.mkdir(parents=True, exist_ok=True)
        res_path = await MKVService.merge_softsub(
            video_path=raw_video,
            output_path=output_file,
            fa_sub_path=fa_sub,
            en_sub_path=en_sub,
            keep_old_subs=True,
            custom_brand=brand,
            sync_mode="none",
            sync_offset_ms=0,
            progress_callback=progress_callback
        )
        if auto_upload and res_path.exists():
            remote_dir = cls.compute_server_remote_dir_for_local_path(res_path)
            await queue_engine.add_task(
                "upload",
                f"AutoUpload [{remote_dir}]: {res_path.name}",
                CuteFTPService.upload_path,
                res_path,
                remote_dir
            )
        return res_path

    @classmethod
    async def run_hardsub_or_encode_and_optional_upload(
        cls,
        ht: HardSubTarget,
        fa_sub: Path | None,
        en_sub: Path | None,
        brand: str,
        auto_upload: bool = False,
        progress_callback: Callable[[str], None] = None
    ) -> Path:
        ht.output_dir.mkdir(parents=True, exist_ok=True)
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

        if auto_upload and res_path.exists():
            remote_dir = cls.compute_server_remote_dir_for_local_path(res_path)
            await queue_engine.add_task(
                "upload",
                f"AutoUpload [{remote_dir}]: {res_path.name}",
                CuteFTPService.upload_path,
                res_path,
                remote_dir
            )
        return res_path
