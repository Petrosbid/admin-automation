import re
from dataclasses import dataclass, field
from pathlib import Path
from core.config_loader import cfg
from services.downloader_service import DownloaderService
from services.mkv_service import MKVService
from services.handbrake_service import HandBrakeService


@dataclass
class SoftSubTarget:
    raw_video: Path
    quality_folder_name: str
    output_dir: Path
    output_file: Path


@dataclass
class HardSubTarget:
    quality_tag: str          # '1080p' | '720p' | '480p'
    source_video: Path
    output_dir: Path
    output_file: Path
    preset_file: str
    preset_name: str


@dataclass
class AutomationPlan:
    is_series: bool
    base_dir: Path            # پوشه فیلم (movie.name.2026) یا پوشه فصل سریال (VisionQuest/S02)
    series_title: str = ""
    season_ep_tag: str = ""   # مثلا S02E01
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
    EPISODE_REGEX = re.compile(r"[Ss](\d{1,2})[Ee](\d{1,3})")
    SEASON_DIR_REGEX = re.compile(r"^[Ss](\d{1,2})$")

    @classmethod
    def extract_episode_tag(cls, text: str) -> str | None:
        """استخراج استاندارد S02E01 از نام فایل ویدئو یا زیرنویس"""
        match = cls.EPISODE_REGEX.search(text)
        if not match:
            return None
        s_num = int(match.group(1))
        e_num = int(match.group(2))
        return f"S{s_num:02d}E{e_num:02d}"

    @staticmethod
    def clean_stem_for_ebtv(stem: str, target_res: str | None = None) -> str:
        """
        حذف پسوندهای تکراری .EBTV / .SoftSub / .HardSub از انتهای نام فایل خام
        و جایگزینی رزولوشن در صورت نیاز (مثلاً تبدیل 1080p به 720p در هاردساب 720p)
        """
        cleaned = stem.strip()
        # حذف تگ‌های قبلی از انتهای نام تا .EBTV.SoftSub.EBTV نشود
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
        """تولید نام دقیق فایل سافت‌ساب با پسوند .SoftSub.EBTV.mkv"""
        clean = cls.clean_stem_for_ebtv(raw_video.stem)
        return f"{clean}.SoftSub.EBTV.mkv"

    @classmethod
    def build_hardsub_filename(cls, source_video: Path, quality_tag: str) -> str:
        """تولید نام دقیق فایل هاردساب با پسوند .HardSub.EBTV.mp4"""
        clean = cls.clean_stem_for_ebtv(source_video.stem, target_res=quality_tag)
        return f"{clean}.HardSub.EBTV.mp4"

    @classmethod
    def create_movie_structure(cls, movie_folder: Path) -> dict[str, Path]:
        """
        ساختار استاندارد فیلم:
        movie.name.2026/
           SoftSub/
           Hardsub/
           srt/
        """
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
        """
        ساختار استاندارد فصل سریال:
        SeriesName/S02/
           FullHD/
           1080p-HardSub/
           720p-HardSub/
           srt/
              EN/
              FA/
        """
        season_folder.mkdir(parents=True, exist_ok=True)
        paths = {
            "base": season_folder,
            "fullhd": season_folder / "FullHD",
            "hardsub_1080": season_folder / "1080p-HardSub",
            "hardsub_720": season_folder / "720p-HardSub",
            "srt": season_folder / "srt",
            "srt_fa": season_folder / "srt" / "FA",
            "srt_en": season_folder / "srt" / "EN",
        }
        for p in paths.values():
            p.mkdir(parents=True, exist_ok=True)
        return paths

    @classmethod
    def is_series_season_dir(cls, folder: Path) -> bool:
        """تشخیص اینکه آیا پوشه انتخاب‌شده، پوشه فصل یک سریال (مثل S01 یا دارای زیرپوشه‌های کیفیت) است یا فیلم"""
        if cls.SEASON_DIR_REGEX.match(folder.name):
            return True
        if (folder / "srt" / "FA").exists() or (folder / "srt" / "EN").exists():
            return True
        if (folder / "FullHD").is_dir():
            return True
        # اگر زیرپوشه‌هایی با نام کیفیت (مثل 1080p-Pahe، 720p-x265-PSA) دارد
        quality_subdirs = [
            d for d in folder.iterdir()
            if d.is_dir() and any(q in d.name.lower() for q in ("1080p", "720p", "480p", "2160p", "fullhd"))
        ]
        return len(quality_subdirs) >= 1

    @classmethod
    def scan_available_episodes_in_season(cls, season_dir: Path) -> list[str]:
        """پیدا کردن تمام قسمت‌های SxxEyy موجود در زیرپوشه‌های خام یک فصل"""
        episodes: set[str] = set()
        if not season_dir.exists():
            return []

        for sub in season_dir.iterdir():
            if not sub.is_dir():
                continue
            low_name = sub.name.lower()
            if low_name in ("srt", "sub", "softsub", "hardsub") or low_name.endswith("-softsub") or low_name.endswith("-hardsub"):
                continue
            for vid in DownloaderService.find_video_files(sub, recursive=False):
                ep = cls.extract_episode_tag(vid.name)
                if ep:
                    episodes.append(ep) if isinstance(episodes, list) else episodes.add(ep)

        return sorted(list(episodes))

    @classmethod
    def find_matching_workspace_folder_for_sub(cls, sub_filename: str) -> tuple[Path | None, str | None]:
        """
        پیدا کردن هوشمند پوشه سریال/فصل یا فیلم در Workspace از روی نام فایل زیرنویس ارسالی
        خروجی: (پوشه هدف، تگ SxxEyy در صورت سریال بودن)
        """
        workspace = Path(cfg.get("paths", "workspace_dir", "./workspace")).resolve()
        if not workspace.exists():
            return None, None

        ep_tag = cls.extract_episode_tag(sub_filename)
        clean_sub = re.sub(r"[^a-zA-Z0-9]+", " ", Path(sub_filename).stem).lower().split()
        title_tokens = [t for t in clean_sub if len(t) > 2 and not cls.EPISODE_REGEX.match(t)]

        best_folder = None
        best_score = 0

        for top_dir in DownloaderService.list_workspace_folders():
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
            else:
                if overlap > best_score:
                    best_score = overlap
                    best_folder = top_dir

        return best_folder, ep_tag

    @classmethod
    async def analyze_and_build_plan(
        cls,
        target_dir: Path,
        episode_tag: str | None = None
    ) -> AutomationPlan:
        """
        تحلیل کامل پوشه فیلم یا فصل سریال:
        - ساخت پوشه‌های استاندارد srt / SoftSub / HardSub
        - پیدا کردن تمام فایل‌های خام مربوط به SxxEyy (برای سریال) یا فیلم
        - شناسایی فایل مرجع در پوشه FullHD برای هاردساب
        - بررسی وجود زیرنویس انگلیسی در پوشه srt و در صورت نبودن، بررسی ترک زیرنویس انگلیسی داخل فایل‌های ویدئو
        """
        target_dir = target_dir.resolve()
        is_series = cls.is_series_season_dir(target_dir) or bool(episode_tag)

        if is_series:
            paths = cls.create_series_season_structure(target_dir)
            series_title = target_dir.parent.name if cls.SEASON_DIR_REGEX.match(target_dir.name) else target_dir.name
            avail_eps = cls.scan_available_episodes_in_season(target_dir)
            if not episode_tag and avail_eps:
                episode_tag = avail_eps[-1]

            plan = AutomationPlan(
                is_series=True,
                base_dir=target_dir,
                series_title=series_title,
                season_ep_tag=(episode_tag or "").upper(),
                fa_sub_dest_dir=paths["srt_fa"],
                en_sub_dest_dir=paths["srt_en"],
                available_episodes=avail_eps,
            )

            # ۱. بررسی وجود زیرنویس انگلیسی در پوشه srt/EN برای این قسمت
            if plan.season_ep_tag and paths["srt_en"].exists():
                for f in paths["srt_en"].iterdir():
                    if f.is_file() and f.suffix.lower() in (".srt", ".ass"):
                        if plan.season_ep_tag.lower() in f.name.lower():
                            plan.existing_en_sub = f.resolve()
                            break

            # ۲. پیمایش تمام پوشه‌های کیفیت خام در پوشه فصل (مثلاً 1080p-Pahe, 720p-x265-PSA, FullHD, ...)
            for sub in sorted(target_dir.iterdir(), key=lambda p: p.name.lower()):
                if not sub.is_dir():
                    continue
                low_name = sub.name.lower()
                # رد کردن پوشه‌های خروجی SoftSub، HardSub و پوشه زیرنویس srt
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

                    # پوشه خروجی سافت‌ساب متناظر: مثلاً 1080p-Pahe -> 1080p-Pahe-SoftSub
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

                    # اگر پوشه FullHD است، این فایل را به عنوان منبع اصلی هاردساب علامت بزن
                    if low_name == "fullhd":
                        plan.fullhd_source_video = vid.resolve()

            # اگر در پوشه FullHD فایلی نبود، باکیفیت‌ترین فایل 1080p موجود را به عنوان جایگزین هاردساب انتخاب کن
            if not plan.fullhd_source_video and plan.softsub_targets:
                ten_eighty = [
                    t.raw_video for t in plan.softsub_targets
                    if "1080" in t.quality_folder_name.lower() or "1080" in t.raw_video.name.lower()
                ]
                plan.fullhd_source_video = ten_eighty[0] if ten_eighty else plan.softsub_targets[0].raw_video

        else:
            # ساختار فیلم سینمایی
            paths = cls.create_movie_structure(target_dir)
            plan = AutomationPlan(
                is_series=False,
                base_dir=target_dir,
                series_title=target_dir.name,
                season_ep_tag="",
                fa_sub_dest_dir=paths["srt"],
                en_sub_dest_dir=paths["srt"],
            )

            # بررسی وجود زیرنویس انگلیسی در پوشه srt/ فیلم
            for f in paths["srt"].iterdir():
                if f.is_file() and f.suffix.lower() in (".srt", ".ass"):
                    if ".en." in f.name.lower() or f.name.lower().startswith("en_"):
                        plan.existing_en_sub = f.resolve()
                        break

            # پیدا کردن تمام فایل‌های ویدئویی خام در ریشه پوشه فیلم (خارج از SoftSub, Hardsub, srt)
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
                # بزرگترین فایل (FullHD / 1080p) برای هاردساب فیلم
                plan.fullhd_source_video = raw_videos[0].resolve()

        # ۳. اگر زیرنویس انگلیسی در پوشه srt نبود، بررسی کن آیا داخل فایل‌های خام ترک زیرنویس انگلیسی هست یا خیر
        if not plan.existing_en_sub and plan.softsub_targets:
            # اولویت با فایل FullHD و سپس سایر فایل‌ها
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

    @staticmethod
    def resolve_preset_for_quality(quality_tag: str) -> dict[str, str]:
        """
        پیدا کردن بهترین پریست HandBrake برای هر یک از ۳ کیفیت اصلی (1080p, 720p, 480p)
        """
        presets = HandBrakeService.list_available_presets()
        if not presets:
            raise RuntimeError("هیچ فایل پریست (.json) در پوشه presets یافت نشد!")

        q_num = quality_tag.lower().replace("p", "")  # '1080' | '720' | '480'
        for pr in presets:
            if q_num in pr["name"].lower() or q_num in Path(pr["file"]).stem.lower():
                return pr

        # اگر پریست اختصاصی با نام 720 یا 480 نبود، اولین پریست موجود را برمی‌گرداند
        # و HandBrakeService خودش رزولوشن را با --maxHeight به 720p یا 480p محدود می‌کند!
        return presets[0]

    @classmethod
    def build_hardsub_targets(
        cls,
        plan: AutomationPlan,
        selected_qualities: list[str]
    ) -> list[HardSubTarget]:
        """
        ساخت لیست تسک‌های هاردساب بر اساس کیفیت‌های انتخاب‌شده توسط کاربر (1080p, 720p, 480p)
        از روی فایل پوشه FullHD
        """
        if not plan.fullhd_source_video or not selected_qualities:
            return []

        targets: list[HardSubTarget] = []
        order_map = {"1080p": 1, "720p": 2, "480p": 3}
        sorted_quals = sorted(selected_qualities, key=lambda q: order_map.get(q, 9))

        for q_tag in sorted_quals:
            pr = cls.resolve_preset_for_quality(q_tag)
            out_name = cls.build_hardsub_filename(plan.fullhd_source_video, q_tag)

            if plan.is_series:
                # در سریال‌ها: ذخیره در پوشه S02/1080p-HardSub ، S02/720p-HardSub ، S02/480p-HardSub
                out_dir = plan.base_dir / f"{q_tag}-HardSub"
            else:
                # در فیلم‌ها: ذخیره در پوشه movie.name.2026/Hardsub
                out_dir = plan.base_dir / "Hardsub"

            targets.append(
                HardSubTarget(
                    quality_tag=q_tag,
                    source_video=plan.fullhd_source_video,
                    output_dir=out_dir,
                    output_file=out_dir / out_name,
                    preset_file=pr["file"],
                    preset_name=pr["name"],
                )
            )
        return targets