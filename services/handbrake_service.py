import asyncio
import json
import re
import uuid
from pathlib import Path
from typing import Callable
from core.config_loader import cfg
from services.subtitle_service import SubtitleService
from services.mkv_service import MKVService


class HandBrakeService:
    RESOLUTION_LIMITS = {
        "1080p": ("1920", "1080"),
        "720p": ("1280", "720"),
        "480p": ("854", "480"),
    }

    @staticmethod
    def list_available_presets() -> list[dict[str, str]]:
        """خواندن لیست تمام پریست‌های JSON موجود در پوشه presets"""
        presets_dir = Path(cfg.get("paths", "presets_dir"))
        presets = []
        if not presets_dir.exists():
            return presets
        for file_path in sorted(presets_dir.glob("*.json")):
            try:
                data = json.loads(file_path.read_text(encoding="utf-8"))
                preset_list = data.get("PresetList", [])
                if preset_list:
                    preset_name = preset_list[0].get("PresetName", file_path.stem)
                    presets.append({
                        "file": str(file_path),
                        "name": preset_name,
                        "label": f"{preset_name} ({file_path.name})"
                    })
            except Exception:
                continue
        return presets

    @classmethod
    async def _resolve_english_srt_for_softsub(
        cls,
        video_path: Path,
        secondary_en_srt_path: Path | None,
        temp_en_out: Path
    ) -> Path | None:
        """
        Resolve an already-extracted English subtitle for the final SoftSub mux.

        Embedded subtitle extraction is intentionally not performed here.
        English tracks are extracted only by the post-FullHD-download pipeline,
        then passed to HandBrake as an external subtitle file.
        """
        candidate: Path | None = None
        if secondary_en_srt_path and Path(secondary_en_srt_path).is_file():
            candidate = Path(secondary_en_srt_path)
        else:
            episode_match = re.search(
                r"[Ss](\d{1,2})[\s._\-]*[Ee](\d{1,3})",
                video_path.name,
            )
            episode_tag = (
                f"S{int(episode_match.group(1)):02d}E{int(episode_match.group(2)):02d}"
                if episode_match
                else ""
            )
            search_dirs = [
                video_path.parent.parent / "srt" / "EN",
                video_path.parent.parent / "srt",
                video_path.parent / "srt" / "EN",
                video_path.parent / "srt",
                video_path.parent,
            ]
            for directory in search_dirs:
                if not directory.is_dir():
                    continue
                for item in sorted(directory.iterdir(), key=lambda p: p.name.casefold()):
                    if not item.is_file() or item.suffix.casefold() not in (".srt", ".ass", ".ssa"):
                        continue
                    if episode_tag and episode_tag.casefold() not in item.name.casefold():
                        continue
                    if (
                        directory.name.casefold() == "en"
                        or ".en." in item.name.casefold()
                        or item.name.casefold().startswith("en_")
                    ):
                        candidate = item
                        break
                if candidate:
                    break

        if not candidate or not candidate.is_file():
            return None

        if candidate.suffix.casefold() in (".ass", ".ssa"):
            MKVService._ass_to_clean_srt(candidate, temp_en_out)
        else:
            temp_en_out.write_text(
                SubtitleService.read_subtitle_safe(candidate),
                encoding="utf-8-sig",
            )
            SubtitleService.normalize_srt_utf8(temp_en_out)

        return (
            temp_en_out
            if temp_en_out.is_file() and temp_en_out.stat().st_size > 0
            else None
        )

    @staticmethod
    async def _build_all_audio_cli_flags(
            video_path: Path,
            target_resolution: str | None = None
    ) -> list[str]:
        """
        شناسایی تمام ترک‌های صوتی فایل ورودی و ساخت فلگ‌های HandBrakeCLI
        برای حفظ و انکود تمامی ترک‌های صوتی (بدون حذف دوبله یا زبان‌های دیگر).
        """
        from services.mkv_service import MKVService

        audio_tracks_channels: list[int] = []
        try:
            info = await MKVService.inspect_tracks(video_path)
            for t in info.get("tracks", []):
                if t.get("type") == "audio":
                    ch = int(t.get("properties", {}).get("audio_channels", 2) or 2)
                    audio_tracks_channels.append(ch)
        except Exception:
            pass

        # اگر به هر دلیلی تعداد ترک‌ها خوانده نشد، فلگ --all-audio به تنهایی همه را انتخاب می‌کند
        if not audio_tracks_channels:
            return ["--all-audio"]

        num_tracks = len(audio_tracks_channels)
        track_indices = ",".join(str(i) for i in range(1, num_tracks + 1))

        # اگر کیفیت 1080p است و ترک صدای 6 کاناله (5.1) دارد، 5point1 حفظ شود؛ در غیر این صورت stereo
        is_1080 = (target_resolution == "1080p") or ("1080" in video_path.name)

        encoders = []
        bitrates = []
        mixdowns = []

        for ch in audio_tracks_channels:
            encoders.append("av_aac")
            if is_1080 and ch >= 6:
                mixdowns.append("5point1")
                bitrates.append("320")
            else:
                mixdowns.append("stereo")
                bitrates.append("160" if is_1080 else "128")

        return [
            "--audio", track_indices,
            "--aencoder", ",".join(encoders),
            "--ab", ",".join(bitrates),
            "--mixdown", ",".join(mixdowns),
        ]

    @classmethod
    async def encode_hardsub(
        cls,
        video_path: Path,
        primary_srt_path: Path | None,
        output_media_path: Path,
        preset_json_path: str,
        preset_name: str,
        secondary_en_srt_path: Path | None = None,
        sync_mode: str = "none",
        sync_offset_ms: int = 0,
        target_resolution: str | None = None,   # '1080p' | '720p' | '480p'
        progress_callback: Callable[[str], None] = None
    ) -> Path:
        """
        معماری ۲ مرحله‌ای تضمینی برای هاردساب و انکود:
        ۱. اگر فایل ورودی زیرنویس از نوع .ass باشد (مانند movie.ass یا زیرنویس استایل‌دار ASS):
           - در مرحله اول فقط فایل .ass به عنوان تنها ترک زیرنویس (Track 1) وارد HandBrake می‌شود و با --subtitle-burned=1
             به صورت ۱۰۰٪ قطعی روی تصویر هاردساب (Burn-in) می‌شود.
           - در مرحله دوم، زیرنویس انگلیسی (از فایل ارسالی، پوشه srt/EN یا داخل خود فیلم) به صورت سافت‌ساب (SoftSub)
             به کانتینر Matroska فایل خروجی اضافه می‌شود.
        ۲. اگر فایل ورودی زیرنویس از نوع .srt باشد:
           - بولد و زرد (#fbff00) شده، روی تصویر هاردساب (Burn-in) می‌شود و هیچ زیرنویس انگلیسی‌ای به آن اضافه نمی‌گردد.
        پسوند نام فایل نهایی از output_media_path گرفته می‌شود.
        """
        video_path = Path(video_path).expanduser().resolve()
        output_media_path = Path(output_media_path).expanduser().resolve()
        if not video_path.is_file() or video_path.stat().st_size <= 0:
            raise FileNotFoundError(f"ویدئوی ورودی HardSub پیدا نشد یا خالی است: {video_path}")
        primary_srt_path = MKVService.validate_subtitle_file(
            primary_srt_path,
            "زیرنویس اصلی برای Burn-in",
        )
        if secondary_en_srt_path is not None:
            secondary_en_srt_path = MKVService.validate_subtitle_file(
                secondary_en_srt_path,
                "زیرنویس انگلیسی تکمیلی",
            )
        if not Path(preset_json_path).is_file():
            raise FileNotFoundError(f"فایل Preset مربوط به HandBrake پیدا نشد: {preset_json_path}")

        output_media_path.parent.mkdir(parents=True, exist_ok=True)
        unique_tag = str(uuid.uuid4())[:6]
        working_sub = None
        temp_styled_srt = output_media_path.with_name(f"_temp_styled_{unique_tag}.srt")
        temp_en_srt = output_media_path.with_name(f"_temp_en_soft_{unique_tag}.srt")
        temp_mkv = output_media_path.with_name(f"_temp_hb_mux_{unique_tag}.mkv")
        temp_hb_encoded = output_media_path.with_name(f"_temp_hb_enc_{unique_tag}.mkv")
        # merge_softsub کانتینر را از پسوند مسیر خروجی انتخاب می‌کند.
        temp_final_output = output_media_path.with_name(f"_temp_hb_final_{unique_tag}.mkv")
        brand = cfg.get("branding", "global_title_and_track_name", ".::EmpireBestTv.Com::.")
        handbrake_bin = cfg.get("paths", "handbrake_bin")

        try:
            has_primary_sub = True
            burn_in_sub_path: Path | None = None
            is_ass_format = False

            if has_primary_sub:
                primary_srt_path = Path(primary_srt_path)
                ext_lower = primary_srt_path.suffix.lower()
                is_ass_format = ext_lower in (".ass", ".ssa")

                working_sub = output_media_path.with_name(f"_temp_sync_{unique_tag}{ext_lower}")
                working_sub.write_text(SubtitleService.read_subtitle_safe(primary_srt_path), encoding="utf-8-sig")

                if sync_mode == "auto" or cfg.get("subtitle_sync", "always_auto_sync", False):
                    await MKVService.sync_sub_against_video(
                        video_path=video_path,
                        sub_to_sync=working_sub,
                        en_sub_reference=secondary_en_srt_path,
                        progress_callback=progress_callback
                    )
                elif sync_mode == "manual" and sync_offset_ms != 0 and ext_lower == ".srt":
                    SubtitleService.shift_srt_time(working_sub, working_sub, sync_offset_ms)

                if is_ass_format:
                    if progress_callback:
                        progress_callback(f"آماده‌سازی {primary_srt_path.name} برای هاردساب (Burn-in) روی تصویر...")
                    burn_in_sub_path = working_sub
                else:
                    if progress_callback:
                        progress_callback("آماده‌سازی SRT بولد و زرد (#fbff00) برای هاردساب...")
                    SubtitleService.style_srt_bold_yellow(working_sub, temp_styled_srt)
                    burn_in_sub_path = temp_styled_srt

            # اگر فرمت ASS است (مثل movie.ass)، زیرنویس انگلیسی را برای مرحله سافت‌ساب نهایی آماده کن
            ready_en_srt: Path | None = None
            if is_ass_format:
                if progress_callback:
                    progress_callback("بررسی و استخراج زیرنویس انگلیسی برای سافت‌ساب...")
                ready_en_srt = await cls._resolve_english_srt_for_softsub(
                    video_path=video_path,
                    secondary_en_srt_path=secondary_en_srt_path,
                    temp_en_out=temp_en_srt
                )

            if progress_callback:
                progress_callback("آماده‌سازی استریم موقت برای HandBrake (سوزاندن قطعی زیرنویس روی تصویر)...")

            # مهم: در فایل موقت ورودیِ HandBrake، تمام زیرنویس‌های قبلی فیلم حذف می‌شوند (keep_old_subs=False)
            # و فقط و فقط burn_in_sub_path به عنوان تنها ترک زیرنویس (Track 1) قرار می‌گیرد تا HandBrake ۱۰۰٪ همان را Burn-in کند!
            await MKVService.merge_softsub(
                video_path=video_path,
                output_path=temp_mkv,
                fa_sub_path=burn_in_sub_path,
                en_sub_path=None,
                keep_old_subs=False,
                custom_brand=brand,
                sync_mode="none",
                require_fa_sub=True,
            )
            mux_subtitle_tracks = await MKVService.list_subtitle_tracks(temp_mkv)
            if len(mux_subtitle_tracks) != 1:
                raise RuntimeError(
                    "فایل واسط HandBrake باید دقیقاً یک ترک زیرنویس اصلی داشته باشد؛ "
                    f"تعداد فعلی: {len(mux_subtitle_tracks)}"
                )
            audio_cli_flags = await cls._build_all_audio_cli_flags(
                video_path=video_path,
                target_resolution=target_resolution
            )
            # همیشه ابتدا در فایل موقت انکود کن؛ خروجی ناقص نباید جایگزین نسخه قبلی شود.
            hb_target_out = temp_hb_encoded

            cmd = [
                handbrake_bin,
                "--preset-import-file", preset_json_path,
                "--preset", preset_name,
                "--format", "av_mkv",
                "--input", str(temp_mkv),
                *audio_cli_flags,
                "--output", str(hb_target_out),
            ]

            if target_resolution and target_resolution in cls.RESOLUTION_LIMITS:
                max_w, max_h = cls.RESOLUTION_LIMITS[target_resolution]
                cmd.extend(["--maxWidth", max_w, "--maxHeight", max_h])

            if burn_in_sub_path is None:
                raise RuntimeError("زیرنویس اصلی برای Burn-in آماده نشده است؛ انکود متوقف شد.")
            # فایل واسط دقیقاً یک ترک زیرنویس دارد؛ انتخاب ترک 1 و Burn-in اجباری است.
            cmd.extend(["--subtitle", "1", "--subtitle-burned=1"])

            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT
            )

            buffer = ""
            while True:
                chunk = await proc.stdout.read(256)
                if not chunk:
                    break
                buffer += chunk.decode("utf-8", errors="replace")
                lines = re.split(r"[\r\n]+", buffer)
                buffer = lines.pop()

                for line in lines:
                    if "Encoding:" in line and progress_callback:
                        progress_callback(line.replace("Encoding:", "").strip())

            await proc.wait()
            if proc.returncode != 0:
                raise RuntimeError(f"HandBrakeCLI exited with code {proc.returncode}")
            if not temp_hb_encoded.is_file() or temp_hb_encoded.stat().st_size <= 0:
                raise RuntimeError(
                    "HandBrake با کد صفر پایان یافت اما فایل خروجی معتبر تولید نکرد؛ "
                    "هیچ فایل ناقصی منتشر یا آپلود نمی‌شود."
                )

            # فقط پس از تأیید خروجی HandBrake، فایل نهایی منتشر می‌شود.
            if is_ass_format and ready_en_srt and ready_en_srt.exists():
                if progress_callback:
                    progress_callback("در حال افزودن زیرنویس انگلیسی به صورت سافت‌ساب به فایل انکود شده...")
                await MKVService.merge_softsub(
                    video_path=temp_hb_encoded,
                    output_path=temp_final_output,
                    fa_sub_path=None,
                    en_sub_path=ready_en_srt,
                    keep_old_subs=False,
                    custom_brand=brand,
                    sync_mode="none",
                    require_en_sub=True,
                )
            else:
                await MKVService.merge_softsub(
                    video_path=temp_hb_encoded,
                    output_path=temp_final_output,
                    keep_old_subs=False,
                    custom_brand=brand,
                )
            temp_final_output.replace(output_media_path)

            if not output_media_path.is_file() or output_media_path.stat().st_size <= 0:
                raise RuntimeError("فایل نهایی HardSub پس از مرحله انتشار پیدا نشد یا خالی است.")
            return output_media_path

        finally:
            if working_sub and working_sub.exists():
                working_sub.unlink(missing_ok=True)
            temp_styled_srt.unlink(missing_ok=True)
            temp_en_srt.unlink(missing_ok=True)
            temp_mkv.unlink(missing_ok=True)
            temp_hb_encoded.unlink(missing_ok=True)
            temp_final_output.unlink(missing_ok=True)
