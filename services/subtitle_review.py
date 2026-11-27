"""Dialogue samples for the operator's per-release timing review."""
import html
import logging
import re
from pathlib import Path
from tempfile import TemporaryDirectory

from services.mkv_service import MKVService
from services.subtitle_service import SubtitleService


TIMING = re.compile(r"(\d{2}):(\d{2}):(\d{2})[,.](\d{1,3})\s*-->\s*(\d{2}):(\d{2}):(\d{2})[,.](\d{1,3})")


def release_key(video: Path) -> str:
    for name in (video.stem, video.parent.name):
        match = re.search(r"(?:[-.]((?:EBTV|Pahe|PSA|RMTeam))|-([A-Za-z][A-Za-z0-9]{1,30}))$", name, re.I)
        if match:
            group = (match[1] or match[2]).casefold()
            if group not in {"web", "dl", "bluray", "fullhd", "x264", "x265", "hevc", "hdtv"}:
                return group
    return str(video.resolve())


def dialogue(path, folder, label):
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


async def sample_report(video, fa, offset=0):
    with TemporaryDirectory() as tmp:
        folder = Path(tmp)
        english = []
        persian = dialogue(fa, folder, "fa")
        try:
            track = await MKVService.preferred_english_track(video, text_only=True)
            if track:
                codec = track.get("codec", "")
                dest = folder / ("embedded" + MKVService.subtitle_extension_for_codec(codec))
                await MKVService.extract_subtitle_track(video, track["id"], dest, codec)
                english = dialogue(dest, folder, "en")
        except Exception:
            logging.exception("English timing reference unavailable for %s", video)
        if not persian:
            return "دیالوگ فارسی قابل خواندن نیست. فایل را دستی بررسی کنید."
        lines = []
        if not english:
            lines.append("مرجع انگلیسی متنی در دسترس نیست. این زمان‌ها را با صدای ویدیو بررسی کنید.")
        used = set()
        for fraction in (.15, .45, .68, .9):
            target = persian[-1][0] * fraction
            candidates = sorted(persian, key=lambda cue: abs(cue[0] - target))
            window = [c for c in candidates[:12] if c not in used]
            if not window:
                continue
            # Prefer an overlapping dialogue near the sample, allowing split translations.
            cue = window[0]
            matches = []
            for candidate in window:
                nearby = [e for e in english if e[0] < candidate[1] + offset and e[1] > candidate[0] + offset]
                if nearby:
                    cue, matches = candidate, nearby
                    break
            used.add(cue)
            timestamp = SubtitleService._ms_to_srt_time(max(0, cue[0] + offset))
            lines.append(f"{timestamp}  {cue[2]}")
            if english:
                lines.append("EN  " + (" / ".join(f"{SubtitleService._ms_to_srt_time(e[0])} {e[2]}" for e in matches[:2]) or "دیالوگ هم‌زمان پیدا نشد"))
        return "\n".join(lines)
