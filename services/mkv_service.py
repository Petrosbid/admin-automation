import asyncio
import json
from pathlib import Path
from typing import Callable
from core.config_loader import cfg
from services.subtitle_service import SubtitleService


class MKVService:
    @staticmethod
    async def get_all_tracks(video_path: Path) -> list[dict]:
        """بررسی کامل فایل ویدئویی با mkvmerge -J طبق مستندات رسمی"""
        mkvmerge = cfg.get("paths", "mkvmerge_bin")
        proc = await asyncio.create_subprocess_exec(
            mkvmerge, "-J", str(video_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, _ = await proc.communicate()
        data = json.loads(stdout.decode("utf-8", errors="replace"))

        tracks_info = []
        for track in data.get("tracks", []):
            props = track.get("properties", {})
            tracks_info.append({
                "id": track["id"],
                "type": track.get("type", "unknown"),  # video | audio | subtitles
                "codec": track.get("codec", "Unknown"),
                "language": props.get("language", "und"),
                "track_name": props.get("track_name", "No Name"),
            })
        return tracks_info

    @classmethod
    async def list_subtitle_tracks(cls, video_path: Path) -> list[dict]:
        """فیلتر کردن فقط ترک‌های زیرنویس موجود در فایل ویدئویی"""
        all_tracks = await cls.get_all_tracks(video_path)
        return [t for t in all_tracks if t["type"] == "subtitles"]

    @staticmethod
    async def extract_subtitle_track(
        video_path: Path,
        track_id: int,
        output_srt: Path,
        progress_callback: Callable[[str], None] = None
    ) -> Path:
        """استخراج یک ترک زیرنویس مشخص با mkvextract"""
        mkvextract = cfg.get("paths", "mkvextract_bin")
        output_srt.parent.mkdir(parents=True, exist_ok=True)
        cmd = [
            mkvextract, "tracks",
            str(video_path),
            f"{track_id}:{output_srt}"
        ]
        if progress_callback:
            progress_callback(f"در حال استخراج زیرنویس (Track #{track_id})...")

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT
        )
        stdout, _ = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(f"MKVExtract Error: {stdout.decode('utf-8', errors='replace')}")
        return output_srt

    @classmethod
    async def extract_all_subtitles(
        cls,
        video_path: Path,
        output_dir: Path,
        progress_callback: Callable[[str], None] = None
    ) -> list[Path]:
        """استخراج خودکار تمامی ترک‌های زیرنویس موجود در یک فیلم"""
        sub_tracks = await cls.list_subtitle_tracks(video_path)
        if not sub_tracks:
            return []

        output_dir.mkdir(parents=True, exist_ok=True)
        extracted_files: list[Path] = []

        for t in sub_tracks:
            tid = t["id"]
            lang = t["language"]
            codec_lower = t["codec"].lower()
            ext = ".ass" if "ass" in codec_lower or "ssa" in codec_lower else (".sup" if "pgs" in codec_lower else ".srt")
            out_file = output_dir / f"{video_path.stem}.Track{tid}.{lang}{ext}"
            await cls.extract_subtitle_track(video_path, tid, out_file, progress_callback)
            if out_file.exists():
                extracted_files.append(out_file)

        return extracted_files

    @classmethod
    async def sync_sub_against_video(
        cls,
        video_path: Path,
        sub_to_sync: Path,
        en_sub_reference: Path | None = None,
        progress_callback: Callable[[str], None] = None
    ) -> Path:
        """
        سینک کردن زیرنویس فارسی قبل از مرج یا هاردساب:
        ۱. اگر زیرنویس انگلیسی سینک‌شده همراهش داده شده، با آن سینک می‌کند.
        ۲. اگر داخل خود فیلم زیرنویس متنی (SRT/ASS) وجود دارد، آن را استخراج موقت کرده و در ۲ ثانیه با آن سینک می‌کند.
        ۳. در غیر این صورت، مستقیماً با صدای فیلم سینک می‌کند.
        """
        temp_ref_sub = None
        try:
            ref_path = en_sub_reference
            if not ref_path or not ref_path.exists():
                sub_tracks = await cls.list_subtitle_tracks(video_path)
                text_subs = [
                    t for t in sub_tracks
                    if any(k in t["codec"].lower() for k in ["subrip", "srt", "substationalpha", "ass", "utf8"])
                ]
                if text_subs:
                    temp_ref_sub = sub_to_sync.with_name(f"_temp_ref_{video_path.stem}.srt")
                    await cls.extract_subtitle_track(video_path, text_subs[0]["id"], temp_ref_sub, progress_callback)
                    ref_path = temp_ref_sub

            await SubtitleService.auto_sync_subtitle(
                video_path=video_path,
                input_srt=sub_to_sync,
                output_srt=sub_to_sync,
                reference_srt=ref_path,
                progress_callback=progress_callback
            )
            return sub_to_sync
        finally:
            if temp_ref_sub and temp_ref_sub.exists():
                temp_ref_sub.unlink(missing_ok=True)

    @classmethod
    async def merge_softsub(
        cls,
        video_path: Path,
        output_path: Path,
        fa_sub_path: Path | None = None,
        en_sub_path: Path | None = None,
        keep_old_subs: bool = True,
        custom_brand: str | None = None,
        sync_mode: str = "none",     # 'none' | 'auto' | 'manual'
        sync_offset_ms: int = 0,
        progress_callback: Callable[[str], None] = None
    ) -> Path:
        """
        مرج حرفه‌ای زیرنویس فارسی و انگلیسی طبق مستندات رسمی mkvmerge:
        - اول سینک کردن زیرنویس فارسی (در صورت انتخاب auto یا manual یا فعال بودن در کانفیگ)
        - تنظیم File Title و تمامی Track Name ها به .::EmpireBestTv.Com::.
        - تنظیم زیرنویس فارسی به عنوان Default (yes) و Forced (yes)
        - غیرفعال کردن پرچم Default و Forced برای تمام زیرنویس‌های قدیمی داخل فیلم
        - چیدمان ترک‌ها با --track-order به طوری که زیرنویس فارسی اولین زیرنویس فیلم باشد
        """
        mkvmerge = cfg.get("paths", "mkvmerge_bin")
        brand = custom_brand or cfg.get("branding", "global_title_and_track_name", ".::EmpireBestTv.Com::.")
        fa_lang = cfg.get("branding", "sub_fa_language", "per")
        en_lang = cfg.get("branding", "sub_en_language", "eng")

        output_path.parent.mkdir(parents=True, exist_ok=True)

        # مرحله ۰: اجرای سینک روی زیرنویس فارسی قبل از مرج
        if fa_sub_path and fa_sub_path.exists():
            if sync_mode == "auto" or cfg.get("subtitle_sync", "always_auto_sync", False):
                await cls.sync_sub_against_video(video_path, fa_sub_path, en_sub_path, progress_callback)
                if fa_sub_path.suffix.lower() == ".srt":
                    SubtitleService.style_srt_bold_yellow(fa_sub_path, fa_sub_path)
            elif sync_mode == "manual" and sync_offset_ms != 0:
                if fa_sub_path.suffix.lower() == ".srt":
                    SubtitleService.shift_srt_time(fa_sub_path, fa_sub_path, sync_offset_ms)
                    SubtitleService.style_srt_bold_yellow(fa_sub_path, fa_sub_path)

        # ۱. دریافت لیست تمام ترک‌های فایل ویدئوی ورودی (File ID = 0)
        existing_tracks = await cls.get_all_tracks(video_path)

        # ۲. ساخت دستور mkvmerge
        cmd = [
            mkvmerge,
            "--gui-mode",
            "-o", str(output_path),
            "--title", brand,
        ]

        if not keep_old_subs:
            cmd.append("--no-subtitles")

        # ۳. تنظیم نام تمام ترک‌های موجود و خاموش کردن Default/Forced زیرنویس‌های قبلی فیلم
        for tr in existing_tracks:
            tid = tr["id"]
            if tr["type"] == "subtitles":
                if not keep_old_subs:
                    continue
                cmd.extend([
                    "--track-name", f"{tid}:{brand}",
                    "--default-track-flag", f"{tid}:no",
                    "--forced-display-flag", f"{tid}:no",
                ])
            else:
                cmd.extend(["--track-name", f"{tid}:{brand}"])

        # افزودن فایل ویدئوی ورودی (File ID = 0)
        cmd.append(str(video_path))

        # متغیرهای ردیابی File ID برای سوئیچ --track-order
        next_file_id = 1
        fa_file_id = None
        en_file_id = None

        # ۴. افزودن زیرنویس فارسی با Default=yes و Forced=yes
        if fa_sub_path and fa_sub_path.exists():
            fa_file_id = next_file_id
            next_file_id += 1
            cmd.extend([
                "--language", f"0:{fa_lang}",
                "--track-name", f"0:{brand}",
                "--default-track-flag", "0:yes",
                "--forced-display-flag", "0:yes",
                "--sub-charset", "0:UTF-8",
            ])
            # اگر فایل ASS بود و کاربر شیفت دستی داده بود، با --sync خود mkvmerge نیز پشتیبانی شود
            if sync_mode == "manual" and sync_offset_ms != 0 and fa_sub_path.suffix.lower() != ".srt":
                cmd.extend(["--sync", f"0:{sync_offset_ms}"])
            cmd.append(str(fa_sub_path))

        # ۵. افزودن زیرنویس انگلیسی با Default=no و Forced=no
        if en_sub_path and en_sub_path.exists():
            en_file_id = next_file_id
            next_file_id += 1
            cmd.extend([
                "--language", f"0:{en_lang}",
                "--track-name", f"0:{brand}",
                "--default-track-flag", "0:no",
                "--forced-display-flag", "0:no",
                "--sub-charset", "0:UTF-8",
                str(en_sub_path)
            ])

        # ۶. ساخت دقیق --track-order تا زیرنویس فارسی اولین زیرنویس پلیر باشد
        track_order = [
            f"0:{tr['id']}" for tr in existing_tracks if tr["type"] in ("video", "audio")
        ]
        if fa_file_id is not None:
            track_order.append(f"{fa_file_id}:0")
        if en_file_id is not None:
            track_order.append(f"{en_file_id}:0")
        if keep_old_subs:
            track_order.extend([
                f"0:{tr['id']}" for tr in existing_tracks if tr["type"] == "subtitles"
            ])

        if track_order:
            cmd.extend(["--track-order", ",".join(track_order)])

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT
        )

        while True:
            line = await proc.stdout.readline()
            if not line:
                break
            decoded = line.decode("utf-8", errors="replace").strip()
            if "#GUI#progress" in decoded and progress_callback:
                pct = decoded.replace("#GUI#progress", "").strip()
                progress_callback(pct)
            elif "Progress:" in decoded and progress_callback:
                progress_callback(decoded.replace("Progress:", "").strip())

        await proc.wait()
        if proc.returncode not in (0, 1):
            raise RuntimeError(f"MKVToolNix Merge Failed (Exit Code {proc.returncode})")

        return output_path