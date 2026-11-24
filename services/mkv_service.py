import asyncio
import json
import logging
import os
import re
import shutil
import uuid
from pathlib import Path
from typing import Callable

from core.config_loader import cfg
from services.subtitle_service import SubtitleService


class MKVService:
    MP4_EXTENSIONS = {".mp4", ".m4v", ".mov"}
    MP4_SOFTSUB_PIPELINE_ID = "direct-srt-input-audit-2026-11-24"

    @staticmethod
    def _get_ffmpeg_bin() -> str | None:
        ffmpeg_cfg = str(cfg.get("paths", "ffmpeg_dir", "")).strip()
        candidates: list[Path] = []

        if ffmpeg_cfg:
            p = Path(ffmpeg_cfg)
            if p.is_file() and p.name.lower().startswith("ffmpeg"):
                candidates.append(p)
            else:
                candidates.append(p / "ffmpeg.exe")
                candidates.append(p / "ffmpeg")
                candidates.append(p / "bin" / "ffmpeg.exe")
                candidates.append(p / "bin" / "ffmpeg")

        which_ff = shutil.which("ffmpeg") or shutil.which("ffmpeg.exe")
        if which_ff:
            candidates.append(Path(which_ff))

        common_paths = [
            Path("C:/ffmpeg/bin/ffmpeg.exe"),
            Path("C:/ffmpeg/ffmpeg.exe"),
            Path("C:/Tools/ffmpeg/bin/ffmpeg.exe"),
            Path("C:/Tools/ffmpeg/ffmpeg.exe"),
            Path("C:/Program Files/ffmpeg/bin/ffmpeg.exe"),
            Path("C:/MediaBot/ffmpeg.exe"),
            Path.cwd() / "ffmpeg.exe",
        ]
        venv_scripts = Path(os.sys.executable).parent
        common_paths.append(venv_scripts / "ffmpeg.exe")
        candidates.extend(common_paths)

        for cand in candidates:
            try:
                if cand.is_file() and cand.exists():
                    return str(cand.resolve())
            except Exception:
                continue
        return None

    @staticmethod
    def _get_ffprobe_bin() -> str | None:
        """
        Resolve ffprobe from an explicit setting, beside ffmpeg, or PATH.
        FFprobe is used for MP4 inspection because MP4 timed-text subtitles
        (mov_text/tx3g) are not exposed by MKVToolNix (mkvmerge -J).
        """
        configured = str(cfg.get("paths", "ffprobe_bin", "")).strip()
        candidates: list[Path] = []

        if configured:
            configured_path = Path(configured).expanduser()
            if configured_path.is_file() or configured_path.name.casefold().startswith("ffprobe"):
                candidates.append(configured_path)
            else:
                candidates.extend([
                    configured_path / "ffprobe.exe",
                    configured_path / "ffprobe",
                    configured_path / "bin" / "ffprobe.exe",
                    configured_path / "bin" / "ffprobe",
                ])

        ffmpeg_bin = MKVService._get_ffmpeg_bin()
        if ffmpeg_bin:
            ffmpeg_path = Path(ffmpeg_bin)
            candidates.extend([
                ffmpeg_path.with_name("ffprobe.exe"),
                ffmpeg_path.with_name("ffprobe"),
            ])

        for executable in (shutil.which("ffprobe"), shutil.which("ffprobe.exe")):
            if executable:
                candidates.append(Path(executable))

        candidates.extend([
            Path("C:/ffmpeg/bin/ffprobe.exe"),
            Path("C:/Tools/ffmpeg/bin/ffprobe.exe"),
            Path("C:/Program Files/ffmpeg/bin/ffprobe.exe"),
            Path.cwd() / "ffprobe.exe",
            Path.cwd() / "ffprobe",
        ])

        seen: set[str] = set()
        for candidate in candidates:
            try:
                key = str(candidate.resolve()).casefold()
                if key in seen:
                    continue
                seen.add(key)
                if candidate.is_file():
                    return str(candidate.resolve())
            except (OSError, RuntimeError):
                continue
        return None

    @classmethod
    async def _inspect_av_streams_ffprobe(cls, video_path: Path) -> tuple[list[dict], list[dict]]:
        """
        Inspect video (excluding attached cover art) and audio streams using FFprobe.
        Returns (video_streams, audio_streams) with index and language tags.
        """
        ffprobe_bin = cls._get_ffprobe_bin()
        if not ffprobe_bin:
            raise FileNotFoundError(
                "برای بررسی استریم‌های MP4، فایل ffprobe.exe پیدا نشد. "
                "فایل ffprobe.exe را کنار ffmpeg.exe قرار دهید یا paths.ffprobe_bin را تنظیم کنید."
            )

        cmd = [
            ffprobe_bin,
            "-v", "error",
            "-show_entries",
            "stream=index,codec_type,codec_name:"
            "stream_tags=language,title,handler_name:"
            "stream_disposition=attached_pic,default",
            "-of", "json",
            str(video_path),
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            details = stderr.decode("utf-8", errors="replace")[-2400:]
            raise RuntimeError(
                f"FFprobe نتوانست استریم‌های فایل ورودی را بخواند (exit={proc.returncode}): {details}"
            )

        try:
            payload = json.loads(stdout.decode("utf-8", errors="replace") or "{}")
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"خروجی JSON ابزار FFprobe معتبر نیست: {exc}") from exc

        video_streams: list[dict] = []
        audio_streams: list[dict] = []

        for stream in payload.get("streams", []) or []:
            codec_type = str(stream.get("codec_type") or "").casefold()
            disposition = stream.get("disposition", {}) or {}
            tags = stream.get("tags", {}) or {}

            if codec_type == "video":
                if int(disposition.get("attached_pic", 0) or 0) == 1:
                    continue
                video_streams.append({
                    "index": int(stream["index"]),
                    "codec": stream.get("codec_name", ""),
                    "language": str(tags.get("language") or "").strip(),
                })
            elif codec_type == "audio":
                audio_streams.append({
                    "index": int(stream["index"]),
                    "codec": stream.get("codec_name", ""),
                    "language": str(tags.get("language") or "").strip(),
                })

        return video_streams, audio_streams

    @classmethod
    async def _list_mp4_subtitle_tracks_ffprobe(cls, video_path: Path) -> list[dict]:
        """
        Read MP4 timed-text (mov_text) streams with FFprobe.
        MKVToolNix (mkvmerge -J) does not expose MP4 tx3g/mov_text tracks.
        """
        ffprobe_bin = cls._get_ffprobe_bin()
        if not ffprobe_bin:
            raise FileNotFoundError(
                "برای بررسی ترک‌های زیرنویس MP4، ffprobe.exe پیدا نشد. "
                "فایل ffprobe.exe را کنار ffmpeg.exe قرار دهید یا مسیر "
                "paths.ffprobe_bin را در config تنظیم کنید."
            )

        cmd = [
            ffprobe_bin,
            "-v", "error",
            "-select_streams", "s",
            "-show_entries",
            "stream=index,codec_name,codec_type:"
            "stream_tags=language,language-ietf,title,handler_name:"
            "stream_disposition=default,forced",
            "-of", "json",
            str(video_path),
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            details = stderr.decode("utf-8", errors="replace")[-2400:]
            raise RuntimeError(
                f"FFprobe نتوانست ترک‌های زیرنویس MP4 را بررسی کند "
                f"(exit={proc.returncode}): {details}"
            )

        try:
            payload = json.loads(stdout.decode("utf-8", errors="replace") or "{}")
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"خروجی JSON ابزار FFprobe معتبر نیست: {exc}") from exc

        result: list[dict] = []
        for stream in payload.get("streams", []) or []:
            tags = stream.get("tags", {}) or {}
            disposition = stream.get("disposition", {}) or {}
            result.append({
                "id": stream.get("index"),
                "codec": stream.get("codec_name", "unknown"),
                "language": tags.get("language", "und"),
                "language_ietf": tags.get("language-ietf", tags.get("language_ietf", "")),
                "track_name": tags.get("handler_name") or tags.get("title") or "",
                "default_track": bool(disposition.get("default", 0)),
                "forced_track": bool(disposition.get("forced", 0)),
            })
        return result

    @classmethod
    async def _extract_mp4_subtitle_text(
        cls,
        video_path: Path,
        track: dict,
        language_label: str,
    ) -> tuple[int, str]:
        """
        Decode one MP4 subtitle stream back to SRT and prove it contains
        real timed caption text, not merely an empty subtitle stream.
        """
        ffmpeg_bin = cls._get_ffmpeg_bin()
        if not ffmpeg_bin:
            raise FileNotFoundError("برای بررسی محتوای زیرنویس MP4، FFmpeg پیدا نشد.")

        stream_index = track.get("id")
        if stream_index is None:
            raise RuntimeError(f"شناسه Stream زیرنویس {language_label} در MP4 مشخص نیست.")

        cmd = [
            ffmpeg_bin,
            "-hide_banner",
            "-loglevel", "error",
            "-i", str(video_path),
            "-map", f"0:{int(stream_index)}",
            "-f", "srt",
            "-",
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        decoded = stdout.decode("utf-8-sig", errors="replace")
        if proc.returncode != 0:
            details = stderr.decode("utf-8", errors="replace")[-1800:]
            raise RuntimeError(
                f"استخراج محتوای زیرنویس {language_label} از MP4 ناموفق بود "
                f"(exit={proc.returncode}): {details}"
            )

        cue_matches = list(re.finditer(
            r"(?m)^\s*\d{2}:\d{2}:\d{2}[,.]\d{1,3}\s*-->\s*"
            r"\d{2}:\d{2}:\d{2}[,.]\d{1,3}\s*$",
            decoded,
        ))
        if not cue_matches:
            raise RuntimeError(
                f"ترک {language_label} در MP4 وجود دارد، اما FFmpeg هیچ زیرنویس زمان‌داری از آن استخراج نکرد."
            )

        decoded_lines = decoded.splitlines()
        caption_lines = []
        for line_index, line in enumerate(decoded_lines):
            stripped = line.strip().lstrip("\ufeff")
            if not stripped:
                continue
            is_timestamp = bool(re.match(
                r"^\d{2}:\d{2}:\d{2}[,.]\d{1,3}\s*-->\s*"
                r"\d{2}:\d{2}:\d{2}[,.]\d{1,3}$",
                stripped,
            ))
            if is_timestamp:
                continue

            if stripped.isdigit():
                next_line = next(
                    (
                        candidate.strip().lstrip("\ufeff")
                        for candidate in decoded_lines[line_index + 1:]
                        if candidate.strip()
                    ),
                    "",
                )
                if re.match(
                    r"^\d{2}:\d{2}:\d{2}[,.]\d{1,3}\s*-->\s*"
                    r"\d{2}:\d{2}:\d{2}[,.]\d{1,3}$",
                    next_line,
                ):
                    continue
            caption_lines.append(stripped)

        caption_text = "\n".join(caption_lines).strip()
        if not caption_text:
            raise RuntimeError(
                f"ترک {language_label} در MP4 دارای زمان‌بندی است، اما متن قابل‌نمایشی ندارد."
            )

        return len(cue_matches), caption_text

    @staticmethod
    def _get_mkvmerge_bin() -> str:
        mkv_cfg = str(cfg.get("paths", "mkvmerge_bin", "C:\\Program Files\\MKVToolNix\\mkvmerge.exe")).strip()
        if Path(mkv_cfg).is_file():
            return mkv_cfg
        which_mkv = shutil.which("mkvmerge") or shutil.which("mkvmerge.exe")
        if which_mkv:
            return which_mkv
        default_p = Path("C:/Program Files/MKVToolNix/mkvmerge.exe")
        if default_p.is_file():
            return str(default_p)
        return mkv_cfg

    @staticmethod
    def _get_mkvextract_bin() -> str:
        ext_cfg = str(cfg.get("paths", "mkvextract_bin", "C:\\Program Files\\MKVToolNix\\mkvextract.exe")).strip()
        if Path(ext_cfg).is_file():
            return ext_cfg
        which_ext = shutil.which("mkvextract") or shutil.which("mkvextract.exe")
        if which_ext:
            return which_ext
        default_p = Path("C:/Program Files/MKVToolNix/mkvextract.exe")
        if default_p.is_file():
            return str(default_p)
        return ext_cfg

    @staticmethod
    def _is_english_track(track_info: dict) -> bool:
        """تشخیص EN با تگ زبان؛ نام‌هایی مثل Default نباید به‌اشتباه رد شوند."""
        lang = str(track_info.get("language", "") or "").strip().casefold()
        lang_ietf = str(track_info.get("language_ietf", "") or "").strip().casefold()
        name = str(track_info.get("track_name", "") or "").strip().casefold()

        explicit_persian = bool(
            re.search(
                r"(?<![a-z0-9])(?:fa|per|fas|persian|farsi)(?![a-z0-9])",
                name,
            )
        )
        explicit_english_name = bool(
            re.search(
                r"(?<![a-z0-9])(?:en|eng|english)(?![a-z0-9])",
                name,
            )
        )
        explicit_english_language = any(
            value in {"eng", "en", "english"}
            or value.startswith("en-")
            for value in (lang, lang_ietf)
        )

        if explicit_persian:
            return False
        return explicit_english_language or explicit_english_name

    @staticmethod
    def validate_subtitle_file(subtitle_path: Path | str | None, label: str = "زیرنویس") -> Path:
        """Reject missing, empty, malformed, or non-text subtitle inputs."""
        if subtitle_path is None:
            raise ValueError(f"{label} انتخاب نشده است.")
        path = Path(subtitle_path).expanduser().resolve()
        if not path.is_file():
            raise FileNotFoundError(f"{label} پیدا نشد: {path}")
        if path.suffix.casefold() not in {".srt", ".ass", ".ssa"}:
            raise ValueError(f"فرمت {label} پشتیبانی نمی‌شود: {path.suffix or '(بدون پسوند)'}")
        content = SubtitleService.read_subtitle_safe(path)
        if not content or not content.strip():
            raise ValueError(f"{label} خالی است: {path.name}")
        if path.suffix.casefold() == ".srt":
            cue_pattern = (
                r"(?m)^\s*\d{2}:\d{2}:\d{2}[,.]\d{1,3}\s*-->\s*"
                r"\d{2}:\d{2}:\d{2}[,.]\d{1,3}"
            )
            if not re.search(cue_pattern, content):
                raise ValueError(f"{label} از نوع SRT هیچ تایم‌کد معتبری ندارد: {path.name}")
        elif not re.search(r"(?im)^\s*Dialogue\s*:", content):
            raise ValueError(f"{label} از نوع ASS/SSA هیچ خط Dialogue ندارد: {path.name}")
        return path

    @classmethod
    async def _verify_muxed_subtitle_languages(
        cls,
        output_path: Path,
        *,
        require_fa_sub: bool = False,
        require_en_sub: bool = False,
        fa_lang: str = "per",
        en_lang: str = "eng",
    ) -> list[dict]:
        if not output_path.is_file() or output_path.stat().st_size <= 0:
            raise RuntimeError(f"فایل خروجی سافت‌ساب ساخته نشده یا خالی است: {output_path}")

        if output_path.suffix.casefold() in cls.MP4_EXTENSIONS:
            tracks = await cls._list_mp4_subtitle_tracks_ffprobe(output_path)
        else:
            tracks = await cls.list_subtitle_tracks(output_path)

        if not tracks and (require_fa_sub or require_en_sub):
            raise RuntimeError(f"خروجی {output_path.name} هیچ ترک زیرنویسی ندارد؛ فایل رد شد.")

        def values(track: dict) -> set[str]:
            return {
                str(track.get(key) or "").strip().casefold()
                for key in ("language", "language_ietf", "track_name")
                if str(track.get(key) or "").strip()
            }

        fa_aliases = {"fa", "fas", "per", "persian", "farsi", str(fa_lang or "").casefold()}
        en_aliases = {"en", "eng", "english", str(en_lang or "").casefold()}

        def matches(track: dict, aliases: set[str]) -> bool:
            return any(
                value in aliases or value.split("-", 1)[0] in aliases
                for value in values(track)
            )

        async def verify_language_content(aliases: set[str], label: str) -> None:
            candidates = [track for track in tracks if matches(track, aliases)]
            if not candidates:
                found = ", ".join(
                    str(track.get("language") or track.get("language_ietf") or "und")
                    for track in tracks
                ) or "هیچ‌کدام"
                raise RuntimeError(
                    f"ترک {label} در {output_path.name} تأیید نشد (زبان‌های موجود: {found}). "
                    "فایل ناقص رد شد."
                )

            if output_path.suffix.casefold() not in cls.MP4_EXTENSIONS:
                return

            failures: list[str] = []
            for track in candidates:
                try:
                    cue_count, _caption_text = await cls._extract_mp4_subtitle_text(
                        output_path,
                        track,
                        label,
                    )
                    if cue_count > 0:
                        return
                except Exception as exc:
                    failures.append(str(exc))

            diagnostic = " | ".join(failures[-3:]) or "هیچ Cue متنی پیدا نشد."
            raise RuntimeError(
                f"ترک {label} در MP4 فقط از نظر Stream شناسایی شده، اما محتوای واقعی زیرنویس "
                f"قابل استخراج نیست؛ فایل ناقص رد شد. جزئیات: {diagnostic}"
            )

        if require_fa_sub:
            await verify_language_content(fa_aliases, "فارسی")
        if require_en_sub:
            await verify_language_content(en_aliases, "انگلیسی")
        return tracks

    @staticmethod
    def subtitle_extension_for_codec(codec: str) -> str:
        value = str(codec or "").casefold()
        if "ssa" in value:
            return ".ssa"
        if "ass" in value:
            return ".ass"
        if any(x in value for x in ("pgs", "hdmv", "vobsub", "dvd_subtitle")):
            return ".sup" if "pgs" in value or "hdmv" in value else ".idx"
        return ".srt"

    @classmethod
    async def inspect_tracks(cls, video_path: Path) -> dict:
        mkvmerge_bin = cls._get_mkvmerge_bin()
        cmd = [mkvmerge_bin, "-J", str(video_path)]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(f"mkvmerge -J error: {stderr.decode('utf-8', errors='replace')}")
        return json.loads(stdout.decode("utf-8", errors="replace"))

    @classmethod
    async def convert_eac3_audio_to_aac(
        cls,
        video_path: Path,
        progress_callback: Callable[[str], None] = None,
    ) -> Path:
        """
        Convert only E-AC-3 audio streams to AAC while copying video and all other streams.
        The original file is replaced only after FFmpeg has produced a valid non-empty output.
        """
        video_path = Path(video_path).resolve()
        if not video_path.is_file():
            raise FileNotFoundError(f"فایل ویدئو برای تبدیل صدا پیدا نشد: {video_path}")

        info = await cls.inspect_tracks(video_path)
        eac3_audio_indices: list[tuple[int, int]] = []
        audio_index = 0

        for track in info.get("tracks", []):
            if track.get("type") != "audio":
                continue

            props = track.get("properties", {}) or {}
            codec_text = " ".join(
                str(value or "")
                for value in (
                    track.get("codec", ""),
                    props.get("codec_id", ""),
                    props.get("codec", ""),
                )
            ).casefold()
            normalized_codec = re.sub(r"[^a-z0-9]+", "", codec_text)

            if "eac3" in normalized_codec:
                channels = int(props.get("audio_channels", 2) or 2)
                eac3_audio_indices.append((audio_index, channels))
            audio_index += 1

        if not eac3_audio_indices:
            return video_path

        ffmpeg_bin = cls._get_ffmpeg_bin()
        if not ffmpeg_bin or (not Path(ffmpeg_bin).is_file() and not shutil.which(ffmpeg_bin)):
            raise FileNotFoundError(
                "کدک صدای فایل 480p از نوع E-AC-3 است اما FFmpeg پیدا نشد؛ "
                "مسیر ffmpeg_dir را در config.yaml بررسی کنید."
            )

        temp_output = video_path.with_name(
            f"_temp_aac_{uuid.uuid4().hex[:8]}{video_path.suffix}"
        )
        cmd = [
            ffmpeg_bin,
            "-hide_banner",
            "-y",
            "-i", str(video_path),
            "-map", "0",
            "-map_metadata", "0",
            "-map_chapters", "0",
            "-c", "copy",
        ]

        for stream_index, channels in eac3_audio_indices:
            bitrate = "384k" if channels >= 6 else "192k"
            cmd.extend([
                f"-c:a:{stream_index}", "aac",
                f"-b:a:{stream_index}", bitrate,
            ])

        if video_path.suffix.casefold() in cls.MP4_EXTENSIONS:
            cmd.extend(["-movflags", "+faststart"])
        cmd.append(str(temp_output))

        if progress_callback:
            progress_callback(
                f"کدک E-AC-3 در {len(eac3_audio_indices)} ترک صوتی شناسایی شد؛ در حال تبدیل به AAC..."
            )

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            output, _ = await proc.communicate()
            if (
                proc.returncode != 0
                or not temp_output.is_file()
                or temp_output.stat().st_size <= 0
            ):
                details = output.decode("utf-8", errors="replace")[-2400:]
                raise RuntimeError(
                    f"تبدیل صدای E-AC-3 به AAC ناموفق بود (exit={proc.returncode}): {details}"
                )

            temp_output.replace(video_path)
            if progress_callback:
                progress_callback("صدای فایل 480p با موفقیت به AAC تبدیل شد.")
            return video_path
        finally:
            temp_output.unlink(missing_ok=True)

    @classmethod
    async def list_subtitle_tracks(cls, video_path: Path) -> list[dict]:
        """
        List subtitle tracks accurately for both MP4 (via FFprobe) and MKV/other (via mkvmerge).
        """
        video_path = Path(video_path)
        if video_path.suffix.casefold() in cls.MP4_EXTENSIONS:
            return await cls._list_mp4_subtitle_tracks_ffprobe(video_path)

        info = await cls.inspect_tracks(video_path)
        subs = []
        for t in info.get("tracks", []):
            if t.get("type") == "subtitles":
                props = t.get("properties", {})
                subs.append({
                    "id": t.get("id"),
                    "codec": t.get("codec", "SubRip/SRT"),
                    "language": props.get("language", "und"),
                    "language_ietf": props.get("language_ietf", ""),
                    "track_name": props.get("track_name", ""),
                    "default_track": props.get("default_track", False),
                    "forced_track": props.get("forced_track", False),
                })
        return subs

    @classmethod
    async def extract_subtitle_track(
        cls,
        video_path: Path,
        track_id: int,
        output_sub_path: Path,
        track_codec=None,
        **kwargs
    ) -> Path:
        """
        Extract one subtitle track reliably:
        - For MP4/M4V/MOV: extract mov_text/tx3g via FFmpeg using stream index.
        - For MKV and other containers: extract via mkvextract.
        """
        video_path = Path(video_path).resolve()
        output_sub_path = Path(output_sub_path).resolve()
        output_sub_path.parent.mkdir(parents=True, exist_ok=True)
        if track_id is None:
            raise ValueError("شناسه ترک زیرنویس نامعتبر است.")

        selected_track_id = int(track_id)
        output_sub_path.unlink(missing_ok=True)

        if video_path.suffix.casefold() in cls.MP4_EXTENSIONS:
            ffmpeg_bin = cls._get_ffmpeg_bin()
            if not ffmpeg_bin or (not Path(ffmpeg_bin).is_file() and not shutil.which(ffmpeg_bin)):
                raise FileNotFoundError("برای استخراج زیرنویس از MP4، ابزار FFmpeg پیدا نشد.")

            out_ext = output_sub_path.suffix.casefold()
            sub_codec = "ass" if out_ext in {".ass", ".ssa"} else "srt"
            cmd = [
                ffmpeg_bin,
                "-hide_banner",
                "-loglevel", "error",
                "-y",
                "-i", str(video_path),
                "-map", f"0:{selected_track_id}",
                "-c:s", sub_codec,
                str(output_sub_path),
            ]
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            stdout, _ = await proc.communicate()
            if proc.returncode != 0 or not output_sub_path.exists() or output_sub_path.stat().st_size <= 0:
                raise RuntimeError(
                    "خطا در استخراج زیرنویس از MP4 با FFmpeg: "
                    + stdout.decode("utf-8", errors="replace")[-1600:]
                )
            if out_ext in (".srt", ".ass", ".ssa"):
                SubtitleService.normalize_subtitle_file(output_sub_path)
            return output_sub_path

        mkvextract_bin = cls._get_mkvextract_bin()
        if not Path(mkvextract_bin).is_file() and not shutil.which(mkvextract_bin):
            raise FileNotFoundError(f"mkvextract پیدا نشد: {mkvextract_bin}")

        cmd = [
            mkvextract_bin,
            "tracks",
            str(video_path),
            f"{selected_track_id}:{output_sub_path}",
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        stdout, _ = await proc.communicate()
        if proc.returncode != 0 or not output_sub_path.exists() or output_sub_path.stat().st_size <= 0:
            raise RuntimeError(
                "خطا در استخراج زیرنویس: "
                + stdout.decode("utf-8", errors="replace")[-1600:]
            )

        if output_sub_path.suffix.lower() in (".srt", ".ass", ".ssa"):
            SubtitleService.normalize_subtitle_file(output_sub_path)
        return output_sub_path

    @classmethod
    async def extract_all_subtitles(
        cls,
        video_path: Path,
        output_dir: Path,
        progress_callback: Callable[[str], None] = None
    ) -> list[Path]:
        output_dir.mkdir(parents=True, exist_ok=True)
        tracks = await cls.list_subtitle_tracks(video_path)
        extracted: list[Path] = []

        for idx, t in enumerate(tracks, start=1):
            tid = t["id"]
            lang = t["language"]
            codec_low = t["codec"].lower()
            ext = ".ass" if ("ass" in codec_low or "ssa" in codec_low) else (".sup" if "pgs" in codec_low else ".srt")
            out_file = output_dir / f"{video_path.stem}_track{tid}_{lang}{ext}"
            if progress_callback:
                progress_callback(f"استخراج زیرنویس {idx}/{len(tracks)} (Track #{tid} - {lang})...")
            try:
                await cls.extract_subtitle_track(video_path, tid, out_file)
                if out_file.exists():
                    extracted.append(out_file)
            except Exception:
                continue
        return extracted

    @classmethod
    async def sync_sub_against_video(
        cls,
        video_path: Path,
        sub_to_sync: Path,
        en_sub_reference: Path | None = None,
        progress_callback: Callable[[str], None] = None
    ) -> Path:
        ref_path = en_sub_reference if (en_sub_reference and en_sub_reference.exists()) else None
        temp_extracted_en = None
        try:
            if not ref_path:
                tracks = await cls.list_subtitle_tracks(video_path)
                en_tracks = [t for t in tracks if cls._is_english_track(t)]
                if en_tracks:
                    temp_extracted_en = sub_to_sync.with_name(f"_temp_ref_en_{uuid.uuid4().hex[:5]}.srt")
                    try:
                        await cls.extract_subtitle_track(video_path, en_tracks[0]["id"], temp_extracted_en)
                        if temp_extracted_en.exists():
                            ref_path = temp_extracted_en
                    except Exception:
                        temp_extracted_en = None

            await SubtitleService.auto_sync_subtitle(
                video_path=video_path,
                input_srt=sub_to_sync,
                output_srt=sub_to_sync,
                reference_srt=ref_path,
                progress_callback=progress_callback
            )
            return sub_to_sync
        finally:
            if temp_extracted_en and temp_extracted_en.exists():
                temp_extracted_en.unlink(missing_ok=True)

    @staticmethod
    def _ass_to_clean_srt(ass_path: Path, srt_out_path: Path) -> Path:
        content = SubtitleService.read_subtitle_safe(ass_path)
        events = []

        def _parse_ass_time(t_str: str) -> str:
            parts = t_str.strip().split(":")
            if len(parts) != 3:
                return "00:00:00,000"
            h = int(parts[0])
            m = int(parts[1])
            s_parts = parts[2].split(".")
            s = int(s_parts[0])
            cs = int(s_parts[1]) if len(s_parts) > 1 else 0
            return f"{h:02d}:{m:02d}:{s:02d},{cs * 10:03d}"

        for line in content.splitlines():
            match = re.match(r"^\s*Dialogue\s*:\s*(.*)$", line, flags=re.IGNORECASE)
            if not match:
                continue
            fields = match.group(1).split(",", 9)
            if len(fields) < 10:
                continue

            start_t = _parse_ass_time(fields[1])
            end_t = _parse_ass_time(fields[2])
            raw_txt = fields[9]
            clean_txt = (
                re.sub(r"\{[^}]*\}", "", raw_txt)
                .replace("\\N", "\n")
                .replace("\\n", "\n")
                .replace("\\h", " ")
                .strip()
            )
            if clean_txt:
                events.append((start_t, end_t, clean_txt))

        srt_blocks = [
            f"{idx}\n{st} --> {et}\n{txt}"
            for idx, (st, et, txt) in enumerate(events, start=1)
        ]
        srt_out_path.write_text("\n\n".join(srt_blocks) + "\n", encoding="utf-8")
        return srt_out_path

    @staticmethod
    def _mp4_language_tag(raw_value: str, fallback: str) -> str:
        """
        MP4/QuickTime requires 3-letter ISO-639-2 language codes.
        Two-letter codes like 'fa' or 'en' are silently dropped by FFmpeg's MP4 muxer.
        """
        value = str(raw_value or "").strip().casefold().replace("_", "-")
        value = value.split("-", 1)[0]
        aliases = {
            "fa": "per", "fas": "fas", "per": "per",
            "farsi": "per", "persian": "per",
            "en": "eng", "eng": "eng", "english": "eng",
            "ja": "jpn", "jp": "jpn", "jpn": "jpn",
            "de": "deu", "ger": "deu", "deu": "deu",
            "fr": "fra", "fre": "fra", "fra": "fra",
            "es": "spa", "spa": "spa",
            "ru": "rus", "rus": "rus",
            "ar": "ara", "ara": "ara",
            "zh": "zho", "chi": "zho", "zho": "zho",
            "ko": "kor", "kor": "kor",
            "it": "ita", "ita": "ita",
            "pt": "por", "por": "por",
        }
        if value in aliases:
            return aliases[value]
        if len(value) == 3 and value.isascii() and value.isalpha():
            return value
        return fallback

    @classmethod
    async def _merge_softsub_mp4_ffmpeg(
        cls,
        ffmpeg_bin: str,
        video_path: Path,
        output_path: Path,
        clean_fa_sub: Path | None,
        clean_en_sub: Path | None,
        kept_embedded_en_track_id: int | None,
        brand: str,
        fa_lang: str,
        en_lang: str,
        fa_default: bool,
        fa_forced: bool,
        require_fa_sub: bool = False,
        require_en_sub: bool = False,
        progress_callback: Callable[[str], None] = None
    ) -> Path:
        """
        Direct, lossless MP4 SoftSub pipeline with FFmpeg:
        1) Validate and normalize external/embedded SRT subtitle inputs.
        2) Inspect exact video/audio stream indices via FFprobe (skipping cover art).
        3) Remux video/audio with stream-copy and encode subtitles to mov_text (tx3g),
           explicitly writing MP4 handler_name & title on every video, audio, and
           subtitle stream so branding and track names appear in all players.
        4) Use clean 'default' disposition (avoiding 'forced' on full MP4 mov_text
           tracks, which causes players to hide or misclassify the subtitle).
        """
        uid = uuid.uuid4().hex[:8]
        temp_subs_to_cleanup: list[Path] = []

        def _ensure_srt_for_mp4(sub_p: Path | None, tag: str) -> Path | None:
            if sub_p is None:
                return None
            if not sub_p.is_file() or sub_p.stat().st_size <= 0:
                raise FileNotFoundError(f"زیرنویس {tag} برای MP4 پیدا نشد یا خالی است: {sub_p}")
            if sub_p.suffix.casefold() in (".ass", ".ssa"):
                tmp_srt = output_path.with_name(f"_mp4_sub_{tag}_{uid}.srt")
                cls._ass_to_clean_srt(sub_p, tmp_srt)
                if not tmp_srt.is_file() or tmp_srt.stat().st_size <= 0:
                    raise RuntimeError(
                        f"تبدیل زیرنویس {tag} از ASS/SSA به SRT خروجی خالی تولید کرد: {sub_p.name}"
                    )
                SubtitleService.normalize_subtitle_file(tmp_srt)
                cls.validate_subtitle_file(tmp_srt, f"زیرنویس {tag} تبدیل‌شده برای MP4")
                temp_subs_to_cleanup.append(tmp_srt)
                return tmp_srt
            return cls.validate_subtitle_file(sub_p, f"زیرنویس {tag} برای MP4")

        try:
            if not ffmpeg_bin or (not Path(ffmpeg_bin).is_file() and not shutil.which(ffmpeg_bin)):
                raise FileNotFoundError("FFmpeg برای ساخت فایل MP4 SoftSub پیدا نشد.")

            fa_srt_ready = _ensure_srt_for_mp4(clean_fa_sub, "fa")
            en_srt_ready = _ensure_srt_for_mp4(clean_en_sub, "en")

            if require_fa_sub and fa_srt_ready is None:
                raise ValueError("زیرنویس فارسی به مرحله ساخت MP4 نرسیده است.")
            if require_en_sub and en_srt_ready is None and kept_embedded_en_track_id is None:
                raise ValueError("زیرنویس انگلیسی موردنیاز برای MP4 موجود نیست.")

            if en_srt_ready is None and kept_embedded_en_track_id is not None:
                extracted_en = output_path.with_name(f"_mp4_emb_en_{uid}.srt")
                await cls.extract_subtitle_track(
                    video_path,
                    int(kept_embedded_en_track_id),
                    extracted_en,
                )
                cls.validate_subtitle_file(extracted_en, "زیرنویس انگلیسی استخراج‌شده برای MP4")
                en_srt_ready = extracted_en
                temp_subs_to_cleanup.append(extracted_en)

            subtitle_inputs: list[dict] = []
            if fa_srt_ready is not None:
                subtitle_inputs.append({
                    "path": fa_srt_ready,
                    "language": cls._mp4_language_tag(fa_lang, "per"),
                    "name": brand,
                    "default": bool(fa_default or require_fa_sub),
                    "label": "FA",
                })
            if en_srt_ready is not None:
                subtitle_inputs.append({
                    "path": en_srt_ready,
                    "language": cls._mp4_language_tag(en_lang, "eng"),
                    "name": brand,
                    "default": False,
                    "label": "EN",
                })

            if (require_fa_sub or fa_srt_ready is not None) and not any(
                item["label"] == "FA" for item in subtitle_inputs
            ):
                raise RuntimeError("ورودی زیرنویس فارسی برای نگاشت مستقیم MP4 آماده نیست.")

            video_streams, audio_streams = await cls._inspect_av_streams_ffprobe(video_path)
            if not video_streams:
                raise RuntimeError(f"هیچ استریم ویدئویی در فایل ورودی پیدا نشد: {video_path}")

            if progress_callback:
                progress_callback("در حال ساخت فایل MP4 همراه با زیرنویس mov_text و متادیتای استاندارد...")

            ff_cmd = [
                ffmpeg_bin,
                "-hide_banner",
                "-y",
                "-i", str(video_path),
            ]
            for subtitle_input in subtitle_inputs:
                ff_cmd.extend([
                    "-sub_charenc", "UTF-8",
                    "-i", str(subtitle_input["path"]),
                ])

            # Map exact video (non-attached_pic) and audio streams from input 0
            for v_stream in video_streams:
                ff_cmd.extend(["-map", f"0:{v_stream['index']}"])
            for a_stream in audio_streams:
                ff_cmd.extend(["-map", f"0:{a_stream['index']}"])

            # Map each validated SRT input explicitly
            for input_index in range(1, len(subtitle_inputs) + 1):
                ff_cmd.extend(["-map", f"{input_index}:0"])

            ff_cmd.extend([
                "-map_metadata", "0",
                "-map_chapters", "0",
                "-c:v", "copy",
                "-c:a", "copy",
                "-c:s", "mov_text",
                "-movflags", "+faststart",
                "-metadata", f"title={brand}",
            ])

            # MP4 stores track display names in the 'hdlr' atom (handler_name), not 'title'.
            for v_idx, v_stream in enumerate(video_streams):
                ff_cmd.extend([
                    f"-metadata:s:v:{v_idx}", f"handler_name={brand}",
                    f"-metadata:s:v:{v_idx}", f"title={brand}",
                ])
                if v_stream.get("language"):
                    ff_cmd.extend([
                        f"-metadata:s:v:{v_idx}",
                        f"language={cls._mp4_language_tag(v_stream['language'], 'und')}",
                    ])

            for a_idx, a_stream in enumerate(audio_streams):
                ff_cmd.extend([
                    f"-metadata:s:a:{a_idx}", f"handler_name={brand}",
                    f"-metadata:s:a:{a_idx}", f"title={brand}",
                ])
                if a_stream.get("language"):
                    ff_cmd.extend([
                        f"-metadata:s:a:{a_idx}",
                        f"language={cls._mp4_language_tag(a_stream['language'], 'und')}",
                    ])

            for sub_index, subtitle_input in enumerate(subtitle_inputs):
                language = subtitle_input["language"]
                track_name = subtitle_input["name"]
                ff_cmd.extend([
                    f"-metadata:s:s:{sub_index}", f"language={language}",
                    f"-metadata:s:s:{sub_index}", f"handler_name={track_name}",
                    f"-metadata:s:s:{sub_index}", f"title={track_name}",
                ])
                # In MP4 (tx3g), use clean 'default' disposition without 'forced'
                # so standard players list and auto-select the track properly.
                disposition = "default" if subtitle_input["default"] else "0"
                ff_cmd.extend([f"-disposition:s:{sub_index}", disposition])

            logging.info(
                "[MP4 SOFTSUB] source=%s output=%s video_streams=%d audio_streams=%d subtitle_inputs=%s",
                video_path,
                output_path,
                len(video_streams),
                len(audio_streams),
                [
                    {
                        "path": str(item["path"]),
                        "language": item["language"],
                        "label": item["label"],
                    }
                    for item in subtitle_inputs
                ],
            )

            ff_cmd.extend(["-f", "mp4", str(output_path)])
            logging.info(
                "[MP4 SOFTSUB IMPLEMENTATION] pipeline=%s module=%s ffmpeg=%s "
                "source=%s output=%s subtitle_inputs=%s mapped_inputs=%s",
                cls.MP4_SOFTSUB_PIPELINE_ID,
                Path(__file__).resolve(),
                ffmpeg_bin,
                video_path,
                output_path,
                [
                    {
                        "path": str(item["path"]),
                        "bytes": item["path"].stat().st_size,
                        "language": item["language"],
                        "label": item["label"],
                    }
                    for item in subtitle_inputs
                ],
                [
                    *[f"0:{stream['index']}" for stream in video_streams],
                    *[f"0:{stream['index']}" for stream in audio_streams],
                    *[f"{index}:0" for index in range(1, len(subtitle_inputs) + 1)],
                ],
            )
            ff_proc = await asyncio.create_subprocess_exec(
                *ff_cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            ff_output, _ = await ff_proc.communicate()
            ff_text = ff_output.decode("utf-8", errors="replace")
            logging.info(
                "[MP4 SOFTSUB FFMPEG FINISHED] pipeline=%s exit=%s output_exists=%s "
                "output_bytes=%s log_tail=%s",
                cls.MP4_SOFTSUB_PIPELINE_ID,
                ff_proc.returncode,
                output_path.is_file(),
                output_path.stat().st_size if output_path.is_file() else 0,
                ff_text[-1800:],
            )
            if (
                ff_proc.returncode != 0
                or not output_path.is_file()
                or output_path.stat().st_size <= 0
            ):
                raise RuntimeError(
                    "ساخت فایل MP4 با زیرنویس ناموفق بود "
                    f"(exit={ff_proc.returncode}). گزارش FFmpeg:\n{ff_text[-5000:]}"
                )

            if progress_callback:
                progress_callback("ساخت فایل MP4 با ترک‌های زیرنویس انجام شد؛ در حال تأیید نهایی...")
            return output_path

        finally:
            for temp_file in temp_subs_to_cleanup:
                temp_file.unlink(missing_ok=True)

    @classmethod
    async def merge_softsub(
        cls,
        video_path: Path,
        output_path: Path,
        fa_sub_path: Path | None = None,
        en_sub_path: Path | None = None,
        keep_old_subs: bool = True,
        custom_brand: str | None = None,
        sync_mode: str = "none",
        sync_offset_ms: int = 0,
        progress_callback: Callable[[str], None] = None,
        require_fa_sub: bool = False,
        require_en_sub: bool = False,
    ) -> Path:
        video_path = Path(video_path).expanduser().resolve()
        output_path = Path(output_path).expanduser().resolve()
        if not video_path.is_file() or video_path.stat().st_size <= 0:
            raise FileNotFoundError(f"فایل ویدئوی ورودی پیدا نشد یا خالی است: {video_path}")
        if output_path.suffix.casefold() in cls.MP4_EXTENSIONS:
            logging.info(
                "[MP4 SOFTSUB SERVICE ENTRY] pipeline=%s module=%s python=%s cwd=%s "
                "source=%s output=%s fa_sub=%s en_sub=%s",
                cls.MP4_SOFTSUB_PIPELINE_ID,
                Path(__file__).resolve(),
                os.sys.executable,
                Path.cwd(),
                video_path,
                output_path,
                fa_sub_path,
                en_sub_path,
            )
        if require_fa_sub and fa_sub_path is None:
            raise ValueError("برای ساخت SoftSub معتبر، زیرنویس فارسی الزامی است.")
        if require_en_sub and en_sub_path is None:
            raise ValueError("زیرنویس انگلیسی برای این خروجی الزامی است.")
        if fa_sub_path is not None:
            fa_sub_path = cls.validate_subtitle_file(fa_sub_path, "زیرنویس فارسی")
        if en_sub_path is not None:
            en_sub_path = cls.validate_subtitle_file(en_sub_path, "زیرنویس انگلیسی")

        output_path.parent.mkdir(parents=True, exist_ok=True)
        mkvmerge_bin = cls._get_mkvmerge_bin()
        ffmpeg_bin = cls._get_ffmpeg_bin()
        brand = custom_brand or cfg.get("branding", "global_title_and_track_name", ".::EmpireBestTv.Com::.")
        fa_lang = cfg.get("branding", "sub_fa_language", "per")
        en_lang = cfg.get("branding", "sub_en_language", "eng")
        fa_default = bool(cfg.get("branding", "fa_default_flag", True))
        fa_forced = bool(cfg.get("branding", "fa_forced_flag", True))

        is_mp4_output = output_path.suffix.casefold() in cls.MP4_EXTENSIONS
        if is_mp4_output and not ffmpeg_bin:
            raise FileNotFoundError("برای ساخت SoftSub با پسوند MP4، نصب/تنظیم FFmpeg الزامی است.")

        uid = uuid.uuid4().hex[:6]
        clean_fa_sub: Path | None = None
        clean_en_sub: Path | None = None
        staging_output_path = output_path.with_name(f"_temp_softsub_{uid}{output_path.suffix}")

        try:
            if fa_sub_path is not None:
                clean_fa_sub = output_path.with_name(f"_soft_clean_fa_{uid}{fa_sub_path.suffix}")
                clean_fa_sub.write_text(
                    SubtitleService.read_subtitle_safe(fa_sub_path),
                    encoding="utf-8",
                    newline="\n",
                )
                SubtitleService.normalize_subtitle_file(clean_fa_sub)

                if sync_mode == "auto":
                    await cls.sync_sub_against_video(
                        video_path=video_path,
                        sub_to_sync=clean_fa_sub,
                        en_sub_reference=Path(en_sub_path) if en_sub_path else None,
                        progress_callback=progress_callback
                    )
                elif sync_mode == "manual" and sync_offset_ms != 0 and clean_fa_sub.suffix.lower() == ".srt":
                    SubtitleService.shift_srt_time(clean_fa_sub, clean_fa_sub, sync_offset_ms)

            if en_sub_path is not None:
                clean_en_sub = output_path.with_name(f"_soft_clean_en_{uid}{en_sub_path.suffix}")
                clean_en_sub.write_text(
                    SubtitleService.read_subtitle_safe(en_sub_path),
                    encoding="utf-8",
                    newline="\n",
                )
                SubtitleService.normalize_subtitle_file(clean_en_sub)

            # Detect embedded English subtitle using the container-aware method
            # (FFprobe for MP4, mkvmerge for MKV).
            kept_embedded_en_id: int | None = None
            if keep_old_subs and not clean_en_sub:
                existing_sub_tracks = await cls.list_subtitle_tracks(video_path)
                for sub_track in existing_sub_tracks:
                    if cls._is_english_track(sub_track):
                        kept_embedded_en_id = sub_track.get("id")
                        break

            if is_mp4_output:
                mp4_require_fa_sub = bool(require_fa_sub or clean_fa_sub is not None)
                mp4_require_en_sub = bool(require_en_sub or clean_en_sub is not None)
                await cls._merge_softsub_mp4_ffmpeg(
                    ffmpeg_bin=ffmpeg_bin,
                    video_path=video_path,
                    output_path=staging_output_path,
                    clean_fa_sub=clean_fa_sub,
                    clean_en_sub=clean_en_sub,
                    kept_embedded_en_track_id=kept_embedded_en_id,
                    brand=brand,
                    fa_lang=fa_lang,
                    en_lang=en_lang,
                    fa_default=fa_default,
                    fa_forced=fa_forced,
                    require_fa_sub=mp4_require_fa_sub,
                    require_en_sub=mp4_require_en_sub,
                    progress_callback=progress_callback
                )

                staging_output_path.replace(output_path)
                try:
                    verified_tracks = await cls._verify_muxed_subtitle_languages(
                        output_path,
                        require_fa_sub=mp4_require_fa_sub,
                        require_en_sub=mp4_require_en_sub,
                        fa_lang=fa_lang,
                        en_lang=en_lang,
                    )
                except Exception:
                    output_path.unlink(missing_ok=True)
                    raise

                logging.info(
                    "[MP4 SOFTSUB VERIFIED] pipeline=%s module=%s python=%s cwd=%s "
                    "output=%s bytes=%d subtitle_tracks=%s",
                    cls.MP4_SOFTSUB_PIPELINE_ID,
                    Path(__file__).resolve(),
                    os.sys.executable,
                    Path.cwd(),
                    output_path,
                    output_path.stat().st_size,
                    [
                        {
                            "stream": track.get("id"),
                            "codec": track.get("codec"),
                            "language": track.get("language"),
                            "name": track.get("track_name"),
                            "default": track.get("default_track"),
                        }
                        for track in verified_tracks
                    ],
                )
                return output_path

            # MKV output path via mkvmerge
            info = await cls.inspect_tracks(video_path)
            tracks = info.get("tracks", [])

            cmd = [
                mkvmerge_bin,
                "--ui-language", "en",
                "--output", str(staging_output_path),
                "--title", brand,
            ]

            for t in tracks:
                tid = t.get("id")
                ttype = t.get("type")
                if ttype in ("video", "audio"):
                    cmd.extend(["--track-name", f"{tid}:{brand}"])

            extracted_mp4_en: Path | None = None
            if kept_embedded_en_id is not None and video_path.suffix.casefold() in cls.MP4_EXTENSIONS:
                # If source is MP4 and destination is MKV, extract mov_text via FFmpeg first
                extracted_mp4_en = output_path.with_name(f"_soft_emb_en_{uid}.srt")
                await cls.extract_subtitle_track(video_path, int(kept_embedded_en_id), extracted_mp4_en)
                clean_en_sub = extracted_mp4_en
                kept_embedded_en_id = None

            if kept_embedded_en_id is not None:
                cmd.extend([
                    "--subtitle-tracks", str(kept_embedded_en_id),
                    "--track-name", f"{kept_embedded_en_id}:{brand}",
                    "--default-track-flag", f"{kept_embedded_en_id}:no",
                    "--forced-display-flag", f"{kept_embedded_en_id}:no",
                ])
            else:
                cmd.append("--no-subtitles")

            cmd.append(str(video_path))

            if clean_fa_sub:
                cmd.extend([
                    "--language", f"0:{fa_lang}",
                    "--track-name", f"0:{brand}",
                    "--default-track-flag", f"0:{'yes' if fa_default else 'no'}",
                    "--forced-display-flag", f"0:{'yes' if fa_forced else 'no'}",
                    "--sub-charset", "0:UTF-8",
                    str(clean_fa_sub),
                ])

            if clean_en_sub:
                cmd.extend([
                    "--language", f"0:{en_lang}",
                    "--track-name", f"0:{brand}",
                    "--default-track-flag", "0:no",
                    "--forced-display-flag", "0:no",
                    "--sub-charset", "0:UTF-8",
                    str(clean_en_sub),
                ])

            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT
            )
            output_chunks: list[str] = []
            buffer = ""
            while True:
                chunk = await proc.stdout.read(4096)
                if not chunk:
                    break
                decoded = chunk.decode("utf-8", errors="replace")
                output_chunks.append(decoded)
                buffer += decoded
                lines = re.split(r"[\r\n]+", buffer)
                buffer = lines.pop()
                for line in lines:
                    if "Progress:" in line and progress_callback:
                        progress_callback(line.replace("Progress:", "").strip())
            if buffer.strip() and "Progress:" in buffer and progress_callback:
                progress_callback(buffer.replace("Progress:", "").strip())
            await proc.wait()
            mkvmerge_output = "".join(output_chunks).strip()
            if proc.returncode not in (0, 1):
                try:
                    output_path.unlink(missing_ok=True)
                except OSError:
                    pass
                diagnostic = mkvmerge_output[-8000:] or "(mkvmerge produced no diagnostic output)"
                raise RuntimeError(
                    f"mkvmerge failed with code {proc.returncode}. "
                    f"Input video: {video_path}\n"
                    f"Persian subtitle: {clean_fa_sub or '(none)'}\n"
                    f"English subtitle: {clean_en_sub or '(none)'}\n"
                    f"Output: {staging_output_path}\n"
                    f"mkvmerge output:\n{diagnostic}"
                )

            await cls._verify_muxed_subtitle_languages(
                staging_output_path,
                require_fa_sub=require_fa_sub,
                require_en_sub=require_en_sub,
                fa_lang=fa_lang,
                en_lang=en_lang,
            )
            staging_output_path.replace(output_path)
            return output_path

        finally:
            staging_output_path.unlink(missing_ok=True)
            if clean_fa_sub and clean_fa_sub.exists():
                clean_fa_sub.unlink(missing_ok=True)
            if clean_en_sub and clean_en_sub.exists():
                clean_en_sub.unlink(missing_ok=True)