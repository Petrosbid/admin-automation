import re
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
    mode: str                 # 'hardsub_srt' (پوشه 1080p-HardSub) | 'encode_ass' (پوشه 1080p-EBTV با movie.ass)
    quality_tag: str          # '1080p' | '720p' | '480p'
    source_video: Path
    output_dir: Path
    output_file: Path
    preset_file: str
    preset_name: str
    ass_logo_path: Path | None = None
    also_create_softsub_dir: Path | None = None
    also_create_softsub_file: Path | None = None


@dataclass
class AutomationPlan:
    is_series: bool
    category: str             # 'Series' | 'Anime' | 'Movies'
    base_dir: Path            # پوشه فیلم (Movies/Movie.2026) یا پوشه فصل سریال/انیمه (Series/VisionQuest/S02)
    series_title: str = ""
    season_ep_tag: str = ""   # مثلا S02E01
    source_type: str = "WEB-DL"  # 'WEB-DL' | 'BluRay'
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
    EPISODE_REGEX = re.compile(r"[Ss](\d{1,2})[\s._\-]*[Ee](\d{1,3})")
    SEASON_DIR_REGEX = re.compile(r"^[Ss](\d{1,2})$")
    ROOT_CATEGORIES = ("Movies", "Series", "Anime")

    @classmethod
    def ensure_workspace_root_categories(cls) -> dict[str, Path]:
        """ایجاد ۳ پوشه اصلی Movies, Series, Anime در Workspace منطبق با ساختار سرور"""
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
        """
        مسیر فایل movie.ass که در دایرکتوری کنار دایرکتوری presets قرار دارد:
        مثلاً C:\\MediaBot\\movie.ass (و بررسی داخل پوشه presets یا ریشه پروژه به عنوان پشتیبان)
        """
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
        """
        محاسبه خودکار آدرس پوشه مقصد در هاست دانلود از روی مسیر فایل در Workspace:
        مثال:
        C:\\MediaBot\\workspace\\Series\\The.Greatest\\S01\\720p-HardSub\\the.greatest...mp4
        ---> /English/Series/The.Greatest/S01/720p-HardSub
        """
        ws = Path(cfg.get("paths", "workspace_dir", "./workspace")).resolve()
        target_dir = local_path.resolve().parent if local_path.is_file() else local_path.resolve()

        try:
            rel_posix = target_dir.relative_to(ws).as_posix()
        except ValueError:
            rel_posix = target_dir.name

        if rel_posix in ("", "."):
            return "/English"

        parts = [p for p in rel_posix.split("/") if p]
        # نگاشت نام پوشه اول به ساختار سرور (/English/Series, /English/Anime, /English/Movies)
        first_low = parts[0].lower()
        if first_low in ("movie", "movies"):
            parts[0] = "Movies"
        elif first_low in ("serie", "series"):
            parts[0] = "Series"
        elif first_low == "anime":
            parts[0] = "Anime"

        if parts[0] in ("Movies", "Series", "Anime"):
            return "/English/" + "/".join(parts)
        return "/English/" + "/".join(parts)

    @classmethod
    def extract_episode_tag(cls, text: str) -> str | None:
        match = cls.EPISODE_REGEX.search(text)
        if not match:
            return None
        s_num = int(match.group(1))
        e_num = int(match.group(2))
        return f"S{s_num:02d}E{e_num:02d}"

    @classmethod
    def extract_episode_number_only(cls, text: str) -> int | None:
        ep_tag = cls.extract_episode_tag(text)
        if ep_tag:
            return int(ep_tag.split("E")[1])
        m = re.search(r"(?:[Ee]p(?:isode)?|[\s._\-\[])E?(\d{1,3})(?:[\s._\-\]]|$)", text)
        if m:
            return int(m.group(1))
        return None

    @staticmethod
    def detect_source_type(text: str) -> str:
        """تشخیص WEB-DL یا BluRay از روی نام فایل ویدئو"""
        low = text.lower()
        if "bluray" in low or "blu-ray" in low or "bdrip" in low or "brrip" in low:
            return "BluRay"
        return "WEB-DL"

    @classmethod
    def format_clean_title(cls, raw_title: str) -> str:
        """تبدیل نام پوشه سریال یا فیلم به فرمت استاندارد نقطه‌دار: مثلاً The.Greatest"""
        cleaned = re.sub(r'[<>:"/\\|?*]', "", raw_title).strip()
        cleaned = re.sub(r"[\s_]+", ".", cleaned)
        return cleaned

    @classmethod
    def build_standard_subtitle_filename(
        cls,
        series_or_movie_title: str,
        season_ep_tag: str,
        source_type: str,
        lang: str,
        ext: str = ".srt"
    ) -> str:
        """
        ساخت نام استاندارد زیرنویس طبق فرمت درخواستی شما:
        {name}.S{xx}E{yy}.WEB-DL(یا BluRay).FA/EN.srt
        """
        clean_name = cls.format_clean_title(series_or_movie_title)
        lang_up = lang.upper()  # 'FA' | 'EN'
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
        """
        فرمت نام‌گذاری نوع اول (هاردساب با زیرنویس فارسی SRT در پوشه 1080p-HardSub و ...):
        {name}.S{xx}E{yy}.1080p.WEB-DL.HardSub.EBTV.mp4
        {name}.S{xx}E{yy}.720p.WEB-DL.HardSub.EBTV.mp4
        {name}.S{xx}E{yy}.480p.WEB-DL.HardSub.EBTV.mp4
        """
        clean_name = cls.format_clean_title(series_or_movie_title)
        if season_ep_tag:
            return f"{clean_name}.{season_ep_tag}.{quality_tag}.{source_type}.HardSub.EBTV.mp4"
        return f"{clean_name}.{quality_tag}.{source_type}.HardSub.EBTV.mp4"

    @classmethod
    def build_encode_ass_filename(
        cls,
        series_or_movie_title: str,
        season_ep_tag: str,
        quality_tag: str,
        source_type: str = "WEB-DL"
    ) -> str:
        """
        فرمت نام‌گذاری نوع دوم (انکود با movie.ass در پوشه‌های 1080p-EBTV، 720p-EBTV، 480p-EBTV):
        {name}.S{xx}E{yy}.1080p.WEB-DL.6CH.EBTV.mp4
        {name}.S{xx}E{yy}.720p.WEB-DL.EBTV.mp4
        {name}.S{xx}E{yy}.480p.WEB-DL.EBTV.mp4
        """
        clean_name = cls.format_clean_title(series_or_movie_title)
        ch_part = ".6CH" if quality_tag == "1080p" else ""
        if season_ep_tag:
            return f"{clean_name}.{season_ep_tag}.{quality_tag}.{source_type}{ch_part}.EBTV.mp4"
        return f"{clean_name}.{quality_tag}.{source_type}{ch_part}.EBTV.mp4"

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
        """
        تولید نام دقیق فایل سافت‌ساب:
        - اگر فایل ورودی .mp4 باشد -> .SoftSub.EBTV.mp4
        - در غیر این صورت -> .SoftSub.EBTV.mkv
        """
        clean = cls.clean_stem_for_ebtv(raw_video.stem)
        out_ext = ".mp4" if raw_video.suffix.lower() == ".mp4" else ".mkv"
        return f"{clean}.SoftSub.EBTV{out_ext}"

    @classmethod
    def create_movie_structure(cls, movie_folder: Path) -> dict[str, Path]:
        movie_folder.mkdir(parents=True, exist_ok=True)
        paths = {
            "base": movie_folder,
            "softsub": movie_folder / "SoftSub",
            "hardsub": movie_folder / "Hardsub",
            "srt": movie_folder / "srt",
        }
        for p in paths.values():
            p.mkdir(parents=True, exist_ok=True)
        return paths

    @classmethod
    def create_series_season_structure(cls, season_folder: Path) -> dict[str, Path]:
        season_folder.mkdir(parents=True, exist_ok=True)
        paths = {
            "base": season_folder,
            "fullhd": season_folder / "FullHD",
            "hardsub_1080": season_folder / "1080p-HardSub",
            "hardsub_720": season_folder / "720p-HardSub",
            "ebtv_1080": season_folder / "1080p-EBTV",
            "ebtv_720": season_folder / "720p-EBTV",
            "ebtv_480": season_folder / "480p-EBTV",
            "srt": season_folder / "srt",
            "srt_fa": season_folder / "srt" / "FA",
            "srt_en": season_folder / "srt" / "EN",
        }
        for p in paths.values():
            p.mkdir(parents=True, exist_ok=True)
        return paths

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
            if (folder / "FullHD").is_dir() or (folder / "srt" / "FA").exists():
                return True
        if (folder / "srt" / "FA").exists() or (folder / "srt" / "EN").exists():
            return True
        if (folder / "FullHD").is_dir():
            return True
        quality_subdirs = [
            d for d in folder.iterdir()
            if d.is_dir() and any(q in d.name.lower() for q in ("1080p", "720p", "480p", "2160p", "fullhd"))
        ]
        return len(quality_subdirs) >= 1

    @classmethod
    def scan_available_episodes_in_season(cls, season_dir: Path) -> list[str]:
        episodes: set[str] = set()
        if not season_dir.exists():
            return []

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
            for vid in DownloaderService.find_video_files(sub, recursive=False):
                ep = cls.extract_episode_tag(vid.name)
                if ep:
                    episodes.add(ep)

        return sorted(list(episodes))

    @classmethod
    def find_matching_workspace_folder_for_sub(cls, sub_filename: str) -> tuple[Path | None, list[str]]:
        ws = Path(cfg.get("paths", "workspace_dir", "./workspace")).resolve()
        if not ws.exists():
            return None, []

        ep_tag = cls.extract_episode_tag(sub_filename)
        clean_sub = re.sub(r"[^a-zA-Z0-9]+", " ", Path(sub_filename).stem).lower().split()
        title_tokens = [t for t in clean_sub if len(t) > 2 and not cls.EPISODE_REGEX.match(t)]

        # جمع‌آوری پوشه‌های کاندید هم از ریشه Workspace و هم از داخل پوشه‌های Series, Anime, Movies
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
            paths = cls.create_series_season_structure(target_dir)
            series_title = target_dir.parent.name if cls.SEASON_DIR_REGEX.match(target_dir.name) else target_dir.name
            avail_eps = cls.scan_available_episodes_in_season(target_dir)
            if not episode_tag and avail_eps:
                episode_tag = avail_eps[-1]

            plan = AutomationPlan(
                is_series=True,
                category=category if category in ("Series", "Anime") else "Series",
                base_dir=target_dir,
                series_title=series_title,
                season_ep_tag=(episode_tag or "").upper(),
                fa_sub_dest_dir=paths["srt_fa"],
                en_sub_dest_dir=paths["srt_en"],
                available_episodes=avail_eps,
            )

            if plan.season_ep_tag and paths["srt_en"].exists():
                for f in paths["srt_en"].iterdir():
                    if f.is_file() and f.suffix.lower() in (".srt", ".ass"):
                        if plan.season_ep_tag.lower() in f.name.lower():
                            plan.existing_en_sub = f.resolve()
                            break

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

                videos_in_sub = DownloaderService.find_video_files(sub, recursive=False)
                for vid in videos_in_sub:
                    vid_ep = cls.extract_episode_tag(vid.name)
                    if plan.season_ep_tag and vid_ep != plan.season_ep_tag:
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

            raw_videos = DownloaderService.find_video_files(target_dir, recursive=False)
            for vid in raw_videos:
                soft_out_file = paths["softsub"] / cls.build_softsub_filename(vid)
                plan.softsub_targets.append(
                    SoftSubTarget(
                        raw_video=vid.resolve(),
                        quality_folder_name="Root",
                        output_dir=paths["softsub"],
                        output_file=soft_out_file,
                    )
                )

            if raw_videos:
                plan.fullhd_source_video = raw_videos[0].resolve()
                plan.source_type = cls.detect_source_type(plan.fullhd_source_video.name)

        if not plan.existing_en_sub and plan.softsub_targets:
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
            for zf in list(d.glob("*.zip")):
                try:
                    SubtitleService.extract_zip_archive(zf, fa_dest_dir, remove_zip=True)
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
        target_ep_num = int(plan.season_ep_tag.split("E")[1])
        for f in all_subs:
            sub_ep = cls.extract_episode_tag(f.name)
            if sub_ep == plan.season_ep_tag:
                matched.append(f)
            elif not sub_ep:
                ep_num = cls.extract_episode_number_only(f.name)
                if ep_num == target_ep_num:
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

        mapping: dict[str, Path] = {}
        used_subs: set[str] = set()

        for p in plans:
            ep_tag = p.season_ep_tag
            for sub in candidate_subs:
                if str(sub) in used_subs:
                    continue
                if cls.extract_episode_tag(sub.name) == ep_tag:
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
                if cls.extract_episode_number_only(sub.name) == target_ep_num:
                    mapping[ep_tag] = sub
                    used_subs.add(str(sub))
                    break

        if len(plans) == 1 and not mapping and len(candidate_subs) == 1:
            mapping[plans[0].season_ep_tag] = candidate_subs[0]

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
    def build_hardsub_and_encode_targets(
        cls,
        plan: AutomationPlan,
        hardsub_srt_qualities: list[str],
        encode_ass_qualities: list[str]
    ) -> list[HardSubTarget]:
        """
        ساخت تسک‌های هر دو نوع هاردساب از روی فایل FullHD:
        ۱. هاردساب فارسی SRT (در پوشه‌های 1080p-HardSub, 720p-HardSub, 480p-HardSub)
        ۲. انکود با movie.ass (در پوشه‌های 1080p-EBTV, 720p-EBTV, 480p-EBTV) + تولید سافت‌ساب متناظر برای آن‌ها
        """
        if not plan.fullhd_source_video:
            return []

        targets: list[HardSubTarget] = []
        order_map = {"1080p": 1, "720p": 2, "480p": 3}
        movie_ass_file = cls.get_movie_ass_logo_path()

        # ۱. ساخت اهداف هاردساب فارسی (SRT)
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

        # ۲. ساخت اهداف انکود با movie.ass (در پوشه‌های 1080p-EBTV, 720p-EBTV, 480p-EBTV)
        for q_tag in sorted(set(encode_ass_qualities), key=lambda q: order_map.get(q, 9)):
            pr = cls.resolve_preset_for_quality(q_tag)
            out_name = cls.build_encode_ass_filename(
                plan.series_title, plan.season_ep_tag, q_tag, plan.source_type
            )
            out_dir = (plan.base_dir / f"{q_tag}-EBTV") if plan.is_series else plan.base_dir
            out_file = out_dir / out_name

            # بررسی اینکه آیا انکود از قبل در پوشه 1080p-EBTV وجود دارد یا خیر؛
            # اگر تازه انکود می‌شود، پس از اتمام انکود، نسخه SoftSub آن هم خودکار در 1080p-EBTV-SoftSub ساخته شود!
            soft_dir = (plan.base_dir / f"{q_tag}-EBTV-SoftSub") if plan.is_series else (plan.base_dir / "SoftSub")
            soft_file = soft_dir / cls.build_softsub_filename(out_file)

            targets.append(
                HardSubTarget(
                    mode="encode_ass",
                    quality_tag=q_tag,
                    source_video=plan.fullhd_source_video,
                    output_dir=out_dir,
                    output_file=out_file,
                    preset_file=pr["file"],
                    preset_name=pr["name"],
                    ass_logo_path=movie_ass_file,
                    also_create_softsub_dir=soft_dir,
                    also_create_softsub_file=soft_file,
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
        """اجرای سافت‌ساب و در صورت فعال بودن auto_upload، افزودن خودکار فایل خروجی به صف آپلود با مسیر دقیق سرور"""
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
        """
        اجرای هاردساب فارسی SRT یا انکود movie.ass:
        - اگر حالت 'encode_ass' باشد: از فایل movie.ass استفاده می‌کند، زیرنویس انگلیسی را سافت‌ساب می‌کند،
          فایل را در 1080p-EBTV ذخیره و آپلود می‌کند و سپس نسخه سافت‌ساب فارسی آن را هم در 1080p-EBTV-SoftSub می‌سازد!
        - اگر حالت 'hardsub_srt' باشد: زیرنویس فارسی SRT را بدون انگلیسی در 1080p-HardSub هاردساب و آپلود می‌کند.
        """
        if ht.mode == "encode_ass":
            logo_ass = ht.ass_logo_path or cls.get_movie_ass_logo_path()
            if not logo_ass.exists():
                raise FileNotFoundError(
                    f"فایل لوگوی انکود (movie.ass) در مسیر {logo_ass} یافت نشد! لطفاً فایل movie.ass را کنار پوشه presets قرار دهید."
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
            if auto_upload and res_path.exists():
                remote_dir = cls.compute_server_remote_dir_for_local_path(res_path)
                await queue_engine.add_task(
                    "upload",
                    f"AutoUpload [{remote_dir}]: {res_path.name}",
                    CuteFTPService.upload_path,
                    res_path,
                    remote_dir
                )
            # اگر زیرنویس فارسی هم داریم، سافت‌ساب این انکود جدید را در پوشه 1080p-EBTV-SoftSub بساز
            if fa_sub and fa_sub.exists() and ht.also_create_softsub_file:
                ht.also_create_softsub_dir.mkdir(parents=True, exist_ok=True)
                await queue_engine.add_task(
                    "mkvtoolnix",
                    f"SoftSub [{ht.quality_tag}-EBTV]: {ht.also_create_softsub_file.name}",
                    cls.run_softsub_and_optional_upload,
                    res_path,
                    ht.also_create_softsub_file,
                    fa_sub,
                    en_sub,
                    brand,
                    auto_upload
                )
            return res_path

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