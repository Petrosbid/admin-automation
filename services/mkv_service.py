import asyncio
import json
import os
import re
import shutil
import uuid
from pathlib import Path
from typing import Callable

from core.config_loader import cfg
from services.subtitle_service import SubtitleService


class MKVService:
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
                candidates.append(p / "bin" / "ffmpeg.exe")

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
        FFprobe is used for MP4 subtitle verification because MP4 subtitles
        are stored as mov_text streams, not as Matroska subtitle tracks.
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
    async def _list_mp4_subtitle_tracks_ffprobe(cls, video_path: Path) -> list[dict]:
        """
        Read MP4 timed-text (mov_text) streams with FFprobe.

        Do not use mkvmerge -J for this check: MKVToolNix identifies Matroska
        subtitle formats, while MP4 text subtitles are exposed by FFmpeg as
        subtitle streams (normally codec_name=mov_text).
        """
        ffprobe_bin = cls._get_ffprobe_bin()
        if not ffprobe_bin:
            raise FileNotFoundError(
                "برای تأیید ترک‌های زیرنویس MP4، ffprobe.exe پیدا نشد. "
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
                "track_name": tags.get("title") or tags.get("handler_name", ""),
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
            r"(?m)^\\s*\\d{2}:\\d{2}:\\d{2}[,.]\\d{1,3}\\s*-->\\s*"
            r"\\d{2}:\\d{2}:\\d{2}[,.]\\d{1,3}\\s*$",
            decoded,
        ))
        if not cue_matches:
            raise RuntimeError(
                f"ترک {language_label} در MP4 وجود دارد، اما FFmpeg هیچ زیرنویس زمان‌داری از آن استخراج نکرد."
            )

        caption_lines = []
        for line in decoded.splitlines():
            stripped = line.strip().lstrip("\\ufeff")
            if not stripped or stripped.isdigit():
                continue
            if re.match(
                r"^\\d{2}:\\d{2}:\\d{2}[,.]\\d{1,3}\\s*-->\\s*"
                r"\\d{2}:\\d{2}:\\d{2}[,.]\\d{1,3}$",
                stripped,
            ):
                continue
            caption_lines.append(stripped)

        caption_text = "\\n".join(caption_lines).strip()
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
        # MP4 subtitle streams are timed-text (mov_text). Inspect them with
        # FFprobe instead of mkvmerge -J, whose track model is Matroska-focused.
        if output_path.suffix.casefold() in {".mp4", ".m4v", ".mov"}:
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

            if output_path.suffix.casefold() not in {".mp4", ".m4v", ".mov"}:
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

        if video_path.suffix.casefold() == ".mp4":
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
        """Extract one MKVToolNix subtitle track and fail clearly when output is empty."""
        video_path = Path(video_path).resolve()
        output_sub_path = Path(output_sub_path).resolve()
        output_sub_path.parent.mkdir(parents=True, exist_ok=True)
        if track_id is None:
            raise ValueError("شناسه ترک زیرنویس نامعتبر است.")

        mkvextract_bin = cls._get_mkvextract_bin()
        if not Path(mkvextract_bin).is_file() and not shutil.which(mkvextract_bin):
            raise FileNotFoundError(f"mkvextract پیدا نشد: {mkvextract_bin}")

        target_for_extract = video_path
        selected_track_id = int(track_id)
        temp_remux_mkv: Path | None = None
        try:
            # شناسه ترک mkvmerge با شماره stream در ffmpeg یکی نیست؛ برای MP4 ابتدا
            # ترک انتخابی را با mkvmerge به یک MKV تک‌ترکی remux می‌کنیم.
            if video_path.suffix.lower() == ".mp4":
                mkvmerge_bin = cls._get_mkvmerge_bin()
                if not Path(mkvmerge_bin).is_file() and not shutil.which(mkvmerge_bin):
                    raise FileNotFoundError(f"mkvmerge پیدا نشد: {mkvmerge_bin}")

                temp_remux_mkv = output_sub_path.with_name(
                    f"_tmp_sub_remux_{uuid.uuid4().hex[:8]}.mkv"
                )
                remux_cmd = [
                    mkvmerge_bin,
                    "--output", str(temp_remux_mkv),
                    "--no-video", "--no-audio",
                    "--subtitle-tracks", str(selected_track_id),
                    str(video_path),
                ]
                remux_proc = await asyncio.create_subprocess_exec(
                    *remux_cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                )
                remux_out, _ = await remux_proc.communicate()
                if remux_proc.returncode != 0 or not temp_remux_mkv.exists() or temp_remux_mkv.stat().st_size <= 0:
                    raise RuntimeError(
                        "Remux زیرنویس MP4 ناموفق بود: "
                        + remux_out.decode("utf-8", errors="replace")[-1200:]
                    )
                target_for_extract = temp_remux_mkv
                selected_track_id = 0

            output_sub_path.unlink(missing_ok=True)
            cmd = [
                mkvextract_bin,
                "tracks",
                str(target_for_extract),
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
        finally:
            if temp_remux_mkv and temp_remux_mkv.exists():
                temp_remux_mkv.unlink(missing_ok=True)

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
            # ASS event lines may contain leading whitespace or different casing.
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
        srt_out_path.write_text("\n\n".join(srt_blocks) + "\n", encoding="utf-8-sig")
        return srt_out_path

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
        Reliable MP4 SoftSub pipeline:
        1) Mux subtitle files into a temporary MKV with MKVToolNix.
        2) Verify that the temporary MKV really contains the requested tracks.
        3) Remux that known-good MKV to MP4 with FFmpeg, converting text
           subtitle streams to mov_text.
        4) The caller verifies the final MP4 with FFprobe before publishing it.
        """
        uid = uuid.uuid4().hex[:8]
        temp_mkv = output_path.with_name(f"_mp4_softsub_mux_{uid}.mkv")
        temp_subs_to_cleanup: list[Path] = []
        mkvmerge_bin = cls._get_mkvmerge_bin()

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
            if not Path(mkvmerge_bin).is_file() and not shutil.which(mkvmerge_bin):
                raise FileNotFoundError(
                    f"mkvmerge برای ساخت واسط MP4 SoftSub پیدا نشد: {mkvmerge_bin}"
                )
            if not ffmpeg_bin or (not Path(ffmpeg_bin).is_file() and not shutil.which(ffmpeg_bin)):
                raise FileNotFoundError("FFmpeg برای تبدیل واسط MKV به MP4 پیدا نشد.")

            fa_srt_ready = _ensure_srt_for_mp4(clean_fa_sub, "فارسی")
            en_srt_ready = _ensure_srt_for_mp4(clean_en_sub, "انگلیسی")

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

            if progress_callback:
                progress_callback("مرحله ۱/۲: ساخت فایل واسط MKV همراه با زیرنویس فارسی/انگلیسی...")

            source_info = await cls.inspect_tracks(video_path)
            source_tracks = source_info.get("tracks", []) or []
            mux_cmd = [
                mkvmerge_bin,
                "--ui-language", "en",
                "--output", str(temp_mkv),
                "--title", brand,
            ]

            for track in source_tracks:
                if track.get("type") in ("video", "audio"):
                    track_id = track.get("id")
                    if track_id is not None:
                        mux_cmd.extend(["--track-name", f"{track_id}:{brand}"])

            if kept_embedded_en_track_id is not None and en_srt_ready is None:
                mux_cmd.extend([
                    "--subtitle-tracks", str(int(kept_embedded_en_track_id)),
                    "--track-name", f"{int(kept_embedded_en_track_id)}:{brand}",
                    "--default-track-flag", f"{int(kept_embedded_en_track_id)}:no",
                    "--forced-display-flag", f"{int(kept_embedded_en_track_id)}:no",
                ])
            else:
                mux_cmd.append("--no-subtitles")
            mux_cmd.append(str(video_path))

            if fa_srt_ready is not None:
                mux_cmd.extend([
                    "--language", f"0:{fa_lang}",
                    "--track-name", f"0:{brand}",
                    "--default-track-flag", f"0:{'yes' if fa_default else 'no'}",
                    "--forced-display-flag", f"0:{'yes' if fa_forced else 'no'}",
                    "--sub-charset", "0:UTF-8",
                    str(fa_srt_ready),
                ])

            if en_srt_ready is not None:
                mux_cmd.extend([
                    "--language", f"0:{en_lang}",
                    "--track-name", f"0:{brand}",
                    "--default-track-flag", "0:no",
                    "--forced-display-flag", "0:no",
                    "--sub-charset", "0:UTF-8",
                    str(en_srt_ready),
                ])

            mux_proc = await asyncio.create_subprocess_exec(
                *mux_cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            mux_output, _ = await mux_proc.communicate()
            mux_text = mux_output.decode("utf-8", errors="replace")
            if (
                mux_proc.returncode not in (0, 1)
                or not temp_mkv.is_file()
                or temp_mkv.stat().st_size <= 0
            ):
                raise RuntimeError(
                    "ساخت فایل واسط MKV برای خروجی MP4 ناموفق بود "
                    f"(exit={mux_proc.returncode}): {mux_text[-5000:]}"
                )

            muxed_tracks = await cls._verify_muxed_subtitle_languages(
                temp_mkv,
                require_fa_sub=require_fa_sub,
                require_en_sub=require_en_sub,
                fa_lang=fa_lang,
                en_lang=en_lang,
            )
            if (require_fa_sub or require_en_sub) and not muxed_tracks:
                raise RuntimeError("فایل واسط MKV ساخته شد اما هیچ ترک زیرنویسی در آن ثبت نشده است.")

            if progress_callback:
                progress_callback("مرحله ۲/۲: تبدیل فایل واسط MKV به MP4 با حفظ ترک‌های زیرنویس...")

            ff_cmd = [
                ffmpeg_bin,
                "-hide_banner",
                "-y",
                "-i", str(temp_mkv),
                "-map", "0:v?",
                "-map", "0:a?",
            ]
            if muxed_tracks:
                ff_cmd.extend(["-map", "0:s"])

            ff_cmd.extend([
                "-map_metadata", "0",
                "-map_chapters", "0",
                "-c:v", "copy",
                "-c:a", "copy",
                "-c:s", "mov_text",
                "-movflags", "+faststart",
                "-metadata", f"title={brand}",
            ])

            for sub_index, track in enumerate(muxed_tracks):
                language = str(track.get("language") or "und").strip() or "und"
                track_name = str(track.get("track_name") or brand).strip() or brand
                ff_cmd.extend([
                    f"-metadata:s:s:{sub_index}", f"language={language}",
                    f"-metadata:s:s:{sub_index}", f"title={track_name}",
                ])
                dispositions = []
                if track.get("default_track"):
                    dispositions.append("default")
                if track.get("forced_track"):
                    dispositions.append("forced")
                ff_cmd.extend([
                    f"-disposition:s:{sub_index}",
                    "+".join(dispositions) if dispositions else "0",
                ])

            ff_cmd.append(str(output_path))
            ff_proc = await asyncio.create_subprocess_exec(
                *ff_cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            ff_output, _ = await ff_proc.communicate()
            ff_text = ff_output.decode("utf-8", errors="replace")
            if (
                ff_proc.returncode != 0
                or not output_path.is_file()
                or output_path.stat().st_size <= 0
            ):
                raise RuntimeError(
                    "تبدیل MKV واسط به MP4 ناموفق بود "
                    f"(exit={ff_proc.returncode}). گزارش FFmpeg:\n{ff_text[-5000:]}"
                )

            if progress_callback:
                progress_callback("ساخت فایل MP4 با ترک‌های زیرنویس انجام شد؛ در حال تأیید نهایی...")
            return output_path

        finally:
            temp_mkv.unlink(missing_ok=True)
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

        if output_path.suffix.casefold() == ".mp4" and not ffmpeg_bin:
            raise FileNotFoundError("برای ساخت SoftSub با پسوند MP4، نصب/تنظیم FFmpeg الزامی است.")
        uid = uuid.uuid4().hex[:6]
        clean_fa_sub: Path | None = None
        clean_en_sub: Path | None = None
        staging_output_path = output_path.with_name(f"_temp_softsub_{uid}{output_path.suffix}")

        try:
            if fa_sub_path is not None:
                clean_fa_sub = output_path.with_name(f"_soft_clean_fa_{uid}{fa_sub_path.suffix}")
                clean_fa_sub.write_text(SubtitleService.read_subtitle_safe(fa_sub_path), encoding="utf-8", newline="\n")
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
                clean_en_sub.write_text(SubtitleService.read_subtitle_safe(en_sub_path), encoding="utf-8", newline="\n")
                SubtitleService.normalize_subtitle_file(clean_en_sub)

            info = await cls.inspect_tracks(video_path)
            tracks = info.get("tracks", [])

            kept_embedded_en_id: int | None = None
            if keep_old_subs and not clean_en_sub:
                for t in tracks:
                    if t.get("type") == "subtitles":
                        props = t.get("properties", {})
                        t_dict = {
                            "id": t.get("id"),
                            "language": props.get("language", ""),
                            "track_name": props.get("track_name", "")
                        }
                        if cls._is_english_track(t_dict):
                            kept_embedded_en_id = t.get("id")
                            break

            if output_path.suffix.lower() == ".mp4":
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
                    require_fa_sub=require_fa_sub,
                    require_en_sub=require_en_sub,
                    progress_callback=progress_callback
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