"""Dialogue samples for comparing Persian subtitles with a FullHD reference."""
import html
import logging
import re
from pathlib import Path
from tempfile import TemporaryDirectory

from services.automation_service import AutomationService
from services.mkv_service import MKVService
from services.subtitle_service import SubtitleService


TIMING = re.compile(r"(\d{2}):(\d{2}):(\d{2})[,.](\d{1,3})\s*-->\s*(\d{2}):(\d{2}):(\d{2})[,.](\d{1,3})")
FULLHD = re.compile(r"(?<![a-z0-9])(?:full[ ._-]*hd|fhd)(?![a-z0-9])", re.I)
H264 = re.compile(r"(?<![a-z0-9])(?:[hx][ ._-]?264|avc)(?![a-z0-9])", re.I)
OUTPUT_FOLDER = re.compile(r"softsub|hardsub|(?<![a-z0-9])ebtv(?![a-z0-9])", re.I)
EN_FOLDERS = {"en", "eng", "english"}
NEARBY_MS = 2000
Cue = tuple[int, int, str]


def review_base_dir(video: Path) -> Path:
    """Find the movie or season directory when the manual flow starts at a video."""
    parent = video.resolve().parent
    for folder in (parent, *parent.parents):
        if AutomationService.SEASON_DIR_REGEX.fullmatch(folder.name):
            return folder
        if folder.parent.name.casefold() in {"movies", "movie", "series", "serie", "anime"}:
            return folder
    name = parent.name
    if FULLHD.search(name) or AutomationService.QUALITY_TAG.search(name) or name.casefold() in {
        "softsub", "hardsub", "raw_temp",
    }:
        return parent.parent
    return parent


def dialogue(path: Path, folder: Path, label: str) -> list[Cue]:
    if path.suffix.lower() in {".ass", ".ssa"}:
        path = SubtitleService.convert_ass_to_srt_file(path, folder / f"{label}.srt")
    cues = []
    for block in re.split(r"\n\s*\n", SubtitleService.read_subtitle_safe(path)):
        timing = TIMING.search(block)
        if not timing:
            continue
        line = html.unescape(re.sub(r"<[^>]*>|\{[^}]*\}|\[[^\]]*\]|\([^)]*\)", "", block[timing.end():]))
        line = " ".join(line.split())
        if not any(c.isalpha() for c in line) or any(c in line for c in "♪♫"):
            continue
        values = [int(v.ljust(3, "0") if i in (3, 7) else v) for i, v in enumerate(timing.groups())]
        start = SubtitleService._time_to_ms(*values[:4])
        end = SubtitleService._time_to_ms(*values[4:])
        if end > start:
            cues.append((start, end, line[:150]))
    return sorted(cues)


def _media_title(name: str) -> str:
    return re.sub(r"[\W_]+", " ", AutomationService.format_clean_title(name).casefold()).strip()


async def _english_dialogue(
        base_dir: Path,
        episode: str | None,
        videos: list[Path],
        folder: Path,
        preferred_video: Path | None,
) -> tuple[list[Cue], Path | None]:
    season = AutomationService.extract_season_num_from_dir(base_dir)
    title_names = {base_dir.name, *(video.stem for video in videos)}
    movie_titles = {_media_title(name) for name in title_names}
    movie_years = {year for name in title_names if (year := AutomationService.extract_media_year(name))}

    def matches_media(path: Path) -> bool:
        tag = AutomationService.extract_episode_tag(path.name, fallback_season=season)
        if episode or tag:
            return tag == episode
        year = AutomationService.extract_media_year(path.stem)
        if year and movie_years and year not in movie_years:
            return False
        title = _media_title(path.stem)
        if path.suffix.casefold() in SubtitleService.SUB_EXTENSIONS:
            title = re.sub(r" (?:en|eng|english)(?: (?:forced|default|sdh|cc|hi|v\d+))?$", "", title)
        return title in movie_titles

    children = list(base_dir.iterdir()) if base_dir.is_dir() else []
    fullhd_dirs = [
        path for path in children
        if path.is_dir() and FULLHD.search(path.name) and not OUTPUT_FOLDER.search(path.name)
    ]
    fullhd_files = [path for directory in fullhd_dirs for path in directory.rglob("*") if path.is_file()]
    known_videos = set(videos)
    if preferred_video:
        known_videos.add(preferred_video)
    candidates = known_videos.union(children, fullhd_files)

    def explicit_fullhd(path: Path) -> bool:
        return any(FULLHD.search(part) for part in path.relative_to(base_dir).parts)

    sources = []
    for candidate in candidates:
        if (
            not candidate.is_relative_to(base_dir)
            or candidate.suffix.casefold() not in AutomationService.VIDEO_EXTS
            or not candidate.is_file()
            or any(OUTPUT_FOLDER.search(part) for part in candidate.relative_to(base_dir).parts[:-1])
            or re.search(r"softsub|hardsub|^_temp_", candidate.name, re.I)
        ):
            continue
        if candidate not in known_videos and not matches_media(candidate):
            continue
        if explicit_fullhd(candidate) or (
            AutomationService.quality_tag_from_name(candidate.name) == "1080p"
            and H264.search(candidate.name)
            and AutomationService.fullhd_source_score(candidate) > 0
        ):
            sources.append(candidate)
    sources.sort(key=lambda path: (path != preferred_video, not explicit_fullhd(path), str(path).casefold()))

    sidecar_dirs = {base_dir, *(path.parent for path in sources)}
    for root in list(sidecar_dirs):
        for subdir in ("srt", "sub", "subs", "EN", "srt/EN", "sub/EN", "subs/EN"):
            sidecar_dirs.add(root / subdir)

    sidecars = set(fullhd_files)
    for directory in sidecar_dirs:
        if directory.is_dir():
            sidecars.update(directory.iterdir())

    ranked_sidecars = []
    for sidecar in sidecars:
        if (
            sidecar.suffix.casefold() not in SubtitleService.SUB_EXTENSIONS
            or sidecar.name.startswith(("_temp_", "_soft_clean_"))
            or not sidecar.is_file()
        ):
            continue
        parts = [part.casefold() for part in sidecar.relative_to(base_dir).parts[:-1]]
        if any(part in {"fa", "fas", "farsi", "persian"} or OUTPUT_FOLDER.search(part) for part in parts):
            continue
        if not (AutomationService._is_english_subtitle_filename(sidecar.name)
                or EN_FOLDERS.intersection(parts)):
            continue
        source_index = next((
            index for index, video in enumerate(sources)
            if sidecar.stem.casefold() == video.stem.casefold()
            or sidecar.stem.casefold().startswith(video.stem.casefold() + ".")
        ), None)
        if source_index is None and not (explicit_fullhd(sidecar) and matches_media(sidecar)):
            continue
        ranked_sidecars.append((source_index if source_index is not None else len(sources), sidecar))

    for _, sidecar in sorted(ranked_sidecars, key=lambda item: (item[0], str(item[1]).casefold())):
        try:
            if cues := dialogue(sidecar, folder, "en"):
                return cues, sidecar
        except (OSError, ValueError):
            logging.exception("Could not read English timing reference %s", sidecar)

    for source in sources:
        try:
            track = await MKVService.preferred_english_track(source, text_only=True)
            if track:
                codec = track.get("codec", "")
                dest = folder / ("embedded" + MKVService.subtitle_extension_for_codec(codec))
                await MKVService.extract_subtitle_track(source, track["id"], dest, codec)
                if cues := dialogue(dest, folder, "en"):
                    return cues, source
        except Exception:
            logging.exception("Could not extract the English timing reference from %s", source)
    return [], None


def _timestamp(ms: int) -> str:
    return f"<code>{SubtitleService._ms_to_srt_time(ms).replace(',', '.')}</code>"


async def sample_report(
        base_dir: Path,
        episode: str | None,
        videos: list[Path],
        fa: Path,
        offset: int = 0,
        preferred_video: Path | None = None,
) -> str:
    with TemporaryDirectory() as tmp:
        folder = Path(tmp)
        persian = [(max(0, start + offset), end + offset, line)
                   for start, end, line in dialogue(fa, folder, "fa") if end + offset > 0]
        if not persian:
            return "⚠️ دیالوگ فارسی قابل‌خواندن پیدا نشد. زیرنویس را دستی بررسی کنید."
        english, reference = await _english_dialogue(base_dir, episode, videos, folder, preferred_video)

    if reference:
        reference_name = html.escape(reference.name[:120])
        lines = [f"<b>مرجع انگلیسی FullHD</b>  ·  <code>{reference_name}</code>"]
    else:
        lines = ["⚠️ مرجع انگلیسی FullHD پیدا نشد. زمان‌های فارسی را با ویدیو بررسی کنید."]
    if offset:
        lines.append(f"تنظیم دستی زمان فارسی  ·  <code>{offset:+d} ms</code>")

    remaining = list(persian)
    samples = []
    for fraction in (.15, .45, .68, .9)[:len(remaining)]:
        target = persian[0][0] + (persian[-1][0] - persian[0][0]) * fraction
        cue = min(remaining, key=lambda item: abs(item[0] - target))
        remaining.remove(cue)
        samples.append(cue)

    for start, end, line in sorted(samples):
        block = [f"<b>فارسی</b>  {_timestamp(start)}\n{html.escape(line)}"]
        overlaps = [cue for cue in english if cue[0] < end and cue[1] > start]
        if overlaps:
            matches = sorted(overlaps, key=lambda cue: abs(cue[0] - start))[:2]
            block.append("<b>انگلیسی</b>  ·  هم‌پوشانی زمانی")
        else:
            before = [cue for cue in english if 0 < start - cue[0] <= NEARBY_MS]
            after = [cue for cue in english if 0 < cue[0] - start <= NEARBY_MS]
            matches = before[-1:] + after[:1]
            if matches:
                block.append("<b>انگلیسی</b>  ·  نزدیک به زمان فارسی")
            elif english:
                block.append("<b>انگلیسی</b>  ·  تا ۲ ثانیه قبل و بعد دیالوگی پیدا نشد")
        for en_start, _, en_line in sorted(matches):
            delta = en_start - start
            difference = ""
            if delta:
                direction = "زودتر" if delta < 0 else "دیرتر"
                difference = f"  ·  <code>{abs(delta) / 1000:.3f}</code> ثانیه {direction}"
            block.append(f"{_timestamp(en_start)}{difference}\n{html.escape(en_line)}")
        lines.append("\n".join(block))
    return "\n\n".join(lines)
