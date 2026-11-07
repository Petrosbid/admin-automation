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
        lang = str(track_info.get("language", "")).lower()
        name = str(track_info.get("track_name", "")).lower()
        if lang in ("eng", "en", "english", "und"):
            if "fa" not in name and "per" not in name and "persian" not in name:
                return True
        if "english" in name or "eng" in name:
            return True
        return False

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
        output_sub_path: Path
    ) -> Path:
        output_sub_path.parent.mkdir(parents=True, exist_ok=True)
        ffmpeg_bin = cls._get_ffmpeg_bin()
        mkvextract_bin = cls._get_mkvextract_bin()

        if video_path.suffix.lower() == ".mp4" and ffmpeg_bin:
            cmd = [
                ffmpeg_bin, "-y",
                "-i", str(video_path),
                "-map", f"0:{track_id}",
                str(output_sub_path)
            ]
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT
            )
            await proc.communicate()
            if output_sub_path.exists() and output_sub_path.stat().st_size > 0:
                if output_sub_path.suffix.lower() == ".srt":
                    SubtitleService.normalize_srt_utf8(output_sub_path)
                return output_sub_path

        target_for_mkvextract = video_path
        temp_remux_mkv = None
        try:
            if video_path.suffix.lower() == ".mp4":
                temp_remux_mkv = output_sub_path.with_name(f"_tmp_sub_remux_{uuid.uuid4().hex[:5]}.mkv")
                mkvmerge_bin = cls._get_mkvmerge_bin()
                remux_cmd = [
                    mkvmerge_bin,
                    "--output", str(temp_remux_mkv),
                    "--no-video", "--no-audio",
                    "--subtitle-tracks", str(track_id),
                    str(video_path)
                ]
                r_proc = await asyncio.create_subprocess_exec(
                    *remux_cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT
                )
                await r_proc.communicate()
                if temp_remux_mkv.exists():
                    target_for_mkvextract = temp_remux_mkv
                    track_id = 0

            cmd = [
                mkvextract_bin,
                "tracks",
                str(target_for_mkvextract),
                f"{track_id}:{output_sub_path}"
            ]
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT
            )
            stdout, _ = await proc.communicate()
            if proc.returncode != 0 or not output_sub_path.exists():
                raise RuntimeError(f"خطا در استخراج زیرنویس: {stdout.decode('utf-8', errors='replace')}")

            if output_sub_path.suffix.lower() == ".srt":
                SubtitleService.normalize_srt_utf8(output_sub_path)
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
            if line.startswith("Dialogue:"):
                fields = line[9:].split(",", 9)
                if len(fields) >= 10:
                    start_t = _parse_ass_time(fields[1])
                    end_t = _parse_ass_time(fields[2])
                    raw_txt = fields[9]
                    clean_txt = re.sub(r"\{[^}]*\}", "", raw_txt).replace("\\N", "\n").replace("\\n", "\n").strip()
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
        progress_callback: Callable[[str], None] = None
    ) -> Path:
        uid = uuid.uuid4().hex[:6]
        temp_subs_to_cleanup: list[Path] = []

        def _ensure_srt_for_mp4(sub_p: Path | None, tag: str) -> Path | None:
            if not sub_p or not sub_p.exists():
                return None
            if sub_p.suffix.lower() in (".ass", ".ssa"):
                tmp_srt = output_path.with_name(f"_mp4_sub_{tag}_{uid}.srt")
                cls._ass_to_clean_srt(sub_p, tmp_srt)
                temp_subs_to_cleanup.append(tmp_srt)
                return tmp_srt
            return sub_p

        try:
            fa_srt_ready = _ensure_srt_for_mp4(clean_fa_sub, "fa")
            en_srt_ready = _ensure_srt_for_mp4(clean_en_sub, "en")

            if not en_srt_ready and kept_embedded_en_track_id is not None:
                extracted_en = output_path.with_name(f"_mp4_emb_en_{uid}.srt")
                try:
                    await cls.extract_subtitle_track(video_path, kept_embedded_en_track_id, extracted_en)
                    if extracted_en.exists():
                        en_srt_ready = extracted_en
                        temp_subs_to_cleanup.append(extracted_en)
                except Exception:
                    pass

            cmd = [ffmpeg_bin, "-y", "-i", str(video_path)]
            sub_inputs = []
            if fa_srt_ready:
                cmd.extend(["-sub_charenc", "UTF-8", "-i", str(fa_srt_ready)])
                sub_inputs.append(("fa", fa_lang, True))
            if en_srt_ready:
                cmd.extend(["-sub_charenc", "UTF-8", "-i", str(en_srt_ready)])
                sub_inputs.append(("en", en_lang, False))

            cmd.extend(["-map", "0:v", "-map", "0:a?"])
            for idx in range(len(sub_inputs)):
                cmd.extend(["-map", f"{idx + 1}:0"])

            cmd.extend([
                "-c:v", "copy",
                "-c:a", "copy",
                "-c:s", "mov_text",
                "-metadata", f"title={brand}",
                "-metadata:s:v:0", f"title={brand}",
                "-metadata:s:a:0", f"title={brand}",
            ])

            for s_idx, (_, s_lang, is_fa) in enumerate(sub_inputs):
                cmd.extend([
                    f"-metadata:s:s:{s_idx}", f"language={s_lang}",
                    f"-metadata:s:s:{s_idx}", f"title={brand}",
                ])
                if is_fa:
                    disp_flags = []
                    if fa_default:
                        disp_flags.append("default")
                    if fa_forced:
                        disp_flags.append("forced")
                    cmd.extend([f"-disposition:s:{s_idx}", "+".join(disp_flags) if disp_flags else "default"])
                else:
                    cmd.extend([f"-disposition:s:{s_idx}", "0"])

            cmd.extend(["-movflags", "+faststart", str(output_path)])

            if progress_callback:
                progress_callback("در حال ساخت سافت‌ساب با فرمت MP4...")

            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT
            )
            stdout, _ = await proc.communicate()
            if proc.returncode != 0:
                raise RuntimeError(f"FFmpeg MP4 SoftSub error: {stdout.decode('utf-8', errors='replace')[-400:]}")

            if progress_callback:
                progress_callback("100%")
            return output_path

        finally:
            for tmp_f in temp_subs_to_cleanup:
                tmp_f.unlink(missing_ok=True)

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
        progress_callback: Callable[[str], None] = None
    ) -> Path:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        mkvmerge_bin = cls._get_mkvmerge_bin()
        ffmpeg_bin = cls._get_ffmpeg_bin()
        brand = custom_brand or cfg.get("branding", "global_title_and_track_name", ".::EmpireBestTv.Com::.")
        fa_lang = cfg.get("branding", "sub_fa_language", "per")
        en_lang = cfg.get("branding", "sub_en_language", "eng")
        fa_default = bool(cfg.get("branding", "fa_default_flag", True))
        fa_forced = bool(cfg.get("branding", "fa_forced_flag", True))

        uid = uuid.uuid4().hex[:6]
        clean_fa_sub: Path | None = None
        clean_en_sub: Path | None = None

        try:
            if fa_sub_path and Path(fa_sub_path).exists():
                fa_sub_path = Path(fa_sub_path)
                clean_fa_sub = output_path.with_name(f"_soft_clean_fa_{uid}{fa_sub_path.suffix}")
                clean_fa_sub.write_text(SubtitleService.read_subtitle_safe(fa_sub_path), encoding="utf-8-sig")

                if sync_mode == "auto":
                    await cls.sync_sub_against_video(
                        video_path=video_path,
                        sub_to_sync=clean_fa_sub,
                        en_sub_reference=Path(en_sub_path) if en_sub_path else None,
                        progress_callback=progress_callback
                    )
                elif sync_mode == "manual" and sync_offset_ms != 0 and clean_fa_sub.suffix.lower() == ".srt":
                    SubtitleService.shift_srt_time(clean_fa_sub, clean_fa_sub, sync_offset_ms)

            if en_sub_path and Path(en_sub_path).exists():
                en_sub_path = Path(en_sub_path)
                clean_en_sub = output_path.with_name(f"_soft_clean_en_{uid}{en_sub_path.suffix}")
                clean_en_sub.write_text(SubtitleService.read_subtitle_safe(en_sub_path), encoding="utf-8-sig")

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

            if output_path.suffix.lower() == ".mp4" and ffmpeg_bin:
                return await cls._merge_softsub_mp4_ffmpeg(
                    ffmpeg_bin=ffmpeg_bin,
                    video_path=video_path,
                    output_path=output_path,
                    clean_fa_sub=clean_fa_sub,
                    clean_en_sub=clean_en_sub,
                    kept_embedded_en_track_id=kept_embedded_en_id,
                    brand=brand,
                    fa_lang=fa_lang,
                    en_lang=en_lang,
                    fa_default=fa_default,
                    fa_forced=fa_forced,
                    progress_callback=progress_callback
                )

            cmd = [
                mkvmerge_bin,
                "--ui-language", "en",
                "--output", str(output_path),
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

            buffer = ""
            while True:
                chunk = await proc.stdout.read(128)
                if not chunk:
                    break
                buffer += chunk.decode("utf-8", errors="replace")
                lines = re.split(r"[\r\n]+", buffer)
                buffer = lines.pop()
                for line in lines:
                    if "Progress:" in line and progress_callback:
                        progress_callback(line.replace("Progress:", "").strip())

            await proc.wait()
            if proc.returncode not in (0, 1):
                raise RuntimeError(f"mkvmerge failed with code {proc.returncode}")

            return output_path

        finally:
            if clean_fa_sub and clean_fa_sub.exists():
                clean_fa_sub.unlink(missing_ok=True)
            if clean_en_sub and clean_en_sub.exists():
                clean_en_sub.unlink(missing_ok=True)