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
        پیدا کردن یا استخراج خودکار زیرنویس انگلیسی و تبدیل تضمینی آن به فرمت .srt استاندارد
        تا در خروجی MP4 به صورت سافت‌ساب (mov_text) قرار بگیرد:
        ۱. اگر فایل زیرنویس انگلیسی داده شده باشد، از همان استفاده می‌کند.
        ۲. اگر داده نشده باشد، پوشه‌های srt/EN و srt کنار فیلم را می‌گردد.
        ۳. اگر در پوشه‌ها نبود، مستقیماً ترک زیرنویس انگلیسی داخل خودِ فایل ویدئو را استخراج می‌کند!
        """
        cand_en: Path | None = None
        if secondary_en_srt_path and Path(secondary_en_srt_path).exists():
            cand_en = Path(secondary_en_srt_path)
        else:
            # جستجو در پوشه srt/EN یا srt فصل/فیلم
            ep_tag_match = re.search(r"[Ss](\d{1,2})[\s._\-]*[Ee](\d{1,3})", video_path.name)
            ep_tag = f"S{int(ep_tag_match.group(1)):02d}E{int(ep_tag_match.group(2)):02d}" if ep_tag_match else ""
            search_dirs = [
                video_path.parent.parent / "srt" / "EN",
                video_path.parent.parent / "srt",
                video_path.parent / "srt" / "EN",
                video_path.parent / "srt",
                video_path.parent,
            ]
            for d in search_dirs:
                if not d.is_dir():
                    continue
                for f in sorted(d.iterdir(), key=lambda x: x.name.lower()):
                    if f.is_file() and f.suffix.lower() in (".srt", ".ass", ".ssa"):
                        if ep_tag and ep_tag.lower() not in f.name.lower():
                            continue
                        if d.name.upper() == "EN" or ".en." in f.name.lower() or f.name.lower().startswith("en_"):
                            cand_en = f
                            break
                if cand_en:
                    break

        if cand_en and cand_en.exists():
            if cand_en.suffix.lower() in (".ass", ".ssa"):
                MKVService._ass_to_clean_srt(cand_en, temp_en_out)
            else:
                temp_en_out.write_text(SubtitleService.read_subtitle_safe(cand_en), encoding="utf-8-sig")
            return temp_en_out if temp_en_out.exists() else None

        # اگر فایل زیرنویس انگلیسی بیرونی نبود، ترک انگلیسی داخل خود ویدئوی سورس را استخراج کن
        try:
            tracks = await MKVService.list_subtitle_tracks(video_path)
            en_tracks = [
                t for t in tracks
                if MKVService._is_english_track(t)
                and any(k in t["codec"].lower() for k in ("subrip", "srt", "ass", "ssa", "utf8", "text", "tx3g"))
            ]
            if en_tracks:
                tid = en_tracks[0]["id"]
                codec_low = en_tracks[0]["codec"].lower()
                if "ass" in codec_low or "ssa" in codec_low:
                    tmp_ass = temp_en_out.with_suffix(".ass")
                    await MKVService.extract_subtitle_track(video_path, tid, tmp_ass)
                    if tmp_ass.exists():
                        MKVService._ass_to_clean_srt(tmp_ass, temp_en_out)
                        tmp_ass.unlink(missing_ok=True)
                else:
                    await MKVService.extract_subtitle_track(video_path, tid, temp_en_out)

                if temp_en_out.exists() and temp_en_out.stat().st_size > 10:
                    SubtitleService.normalize_srt_utf8(temp_en_out)
                    return temp_en_out
        except Exception:
            pass

        return None

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
             به فایل خروجی MP4 اضافه می‌شود!
        ۲. اگر فایل ورودی زیرنویس از نوع .srt باشد:
           - بولد و زرد (#fbff00) شده، روی تصویر هاردساب (Burn-in) می‌شود و هیچ زیرنویس انگلیسی‌ای به آن اضافه نمی‌گردد.
        """
        output_media_path.parent.mkdir(parents=True, exist_ok=True)
        unique_tag = str(uuid.uuid4())[:6]
        working_sub = None
        temp_styled_srt = output_media_path.with_name(f"_temp_styled_{unique_tag}.srt")
        temp_en_srt = output_media_path.with_name(f"_temp_en_soft_{unique_tag}.srt")
        temp_mkv = output_media_path.with_name(f"_temp_hb_mux_{unique_tag}.mkv")
        temp_hb_encoded = output_media_path.with_name(f"_temp_hb_enc_{unique_tag}{output_media_path.suffix}")
        brand = cfg.get("branding", "global_title_and_track_name", ".::EmpireBestTv.Com::.")
        handbrake_bin = cfg.get("paths", "handbrake_bin")

        try:
            has_primary_sub = bool(primary_srt_path and Path(primary_srt_path).exists())
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
                sync_mode="none"
            )
            audio_cli_flags = await cls._build_all_audio_cli_flags(
                video_path=video_path,
                target_resolution=target_resolution
            )
            # اگر قرار است بعد از انکود، زیرنویس انگلیسی به صورت سافت‌ساب اضافه شود، خروجی HandBrake را در فایل موقت بریز
            hb_target_out = temp_hb_encoded if (is_ass_format and ready_en_srt) else output_media_path

            cmd = [
                handbrake_bin,
                "--preset-import-file", preset_json_path,
                "--preset", preset_name,
                "--input", str(temp_mkv),
                *audio_cli_flags,
                "--output", str(hb_target_out),
            ]

            if target_resolution and target_resolution in cls.RESOLUTION_LIMITS:
                max_w, max_h = cls.RESOLUTION_LIMITS[target_resolution]
                cmd.extend(["--maxWidth", max_w, "--maxHeight", max_h])

            if burn_in_sub_path is not None:
                # اجبار قطعی HandBrake به سوزاندن (Burn-in) ترک شماره ۱ روی تصویر
                cmd.extend([
                    "--subtitle", "1",
                    "--subtitle-burned=1",
                ])
            else:
                cmd.extend(["--subtitle", "none"])

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

            # مرحله ۲: اگر فایل ASS بوده (مثل movie.ass) و زیرنویس انگلیسی داریم،
            # اکنون که movie.ass روی تصویر سوزانده (Burn-in) شده، زیرنویس انگلیسی را به صورت سافت‌ساب به MP4 اضافه کن!
            if is_ass_format and ready_en_srt and ready_en_srt.exists():
                if progress_callback:
                    progress_callback("در حال افزودن زیرنویس انگلیسی به صورت سافت‌ساب به فایل انکود شده...")
                await MKVService.merge_softsub(
                    video_path=temp_hb_encoded,
                    output_path=output_media_path,
                    fa_sub_path=None,
                    en_sub_path=ready_en_srt,
                    keep_old_subs=False,
                    custom_brand=brand,
                    sync_mode="none"
                )
            elif output_media_path.suffix.lower() == ".mkv":
                tagged_mkv = output_media_path.with_name(f"_tagged_{unique_tag}_{output_media_path.name}")
                await MKVService.merge_softsub(
                    video_path=output_media_path,
                    output_path=tagged_mkv,
                    keep_old_subs=False,
                    custom_brand=brand
                )
                output_media_path.unlink(missing_ok=True)
                tagged_mkv.rename(output_media_path)

            return output_media_path

        finally:
            if working_sub and working_sub.exists():
                working_sub.unlink(missing_ok=True)
            temp_styled_srt.unlink(missing_ok=True)
            temp_en_srt.unlink(missing_ok=True)
            temp_mkv.unlink(missing_ok=True)
            temp_hb_encoded.unlink(missing_ok=True)