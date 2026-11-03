import asyncio
import json
import re
from pathlib import Path
from typing import Callable
from core.config_loader import cfg
from services.subtitle_service import SubtitleService
from services.mkv_service import MKVService


class HandBrakeService:
    @staticmethod
    def list_available_presets() -> list[dict[str, str]]:
        """خواندن لیست تمام پریست‌های JSON موجود در پوشه presets"""
        presets_dir = Path(cfg.get("paths", "presets_dir"))
        presets = []
        for file_path in presets_dir.glob("*.json"):
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
    async def encode_hardsub(
        cls,
        video_path: Path,
        primary_srt_path: Path,
        output_media_path: Path,
        preset_json_path: str,
        preset_name: str,
        secondary_en_srt_path: Path | None = None,
        sync_mode: str = "none",     # 'none' | 'auto' | 'manual'
        sync_offset_ms: int = 0,
        progress_callback: Callable[[str], None] = None
    ) -> Path:
        """
        هاردساب کردن طبق مستندات رسمی HandBrakeCLI:
        ۱. سینک کردن زیرنویس فارسی (در صورت انتخاب auto یا manual)
        ۲. تبدیل زیرنویس فارسی سینک‌شده به ASS بولد و زرد (#fbff00)
        ۳. سوار کردن زیرنویس ASS روی کانتینر موقت با پرچم Default و Forced و تگ .::EmpireBestTv.Com::.
        ۴. اجرای HandBrakeCLI با --subtitle 1 و --subtitle-burned=1
        """
        output_media_path.parent.mkdir(parents=True, exist_ok=True)
        working_srt = primary_srt_path.with_suffix(".synced.srt")
        temp_ass = primary_srt_path.with_suffix(".styled.ass")
        temp_mkv = video_path.with_name(f"_temp_hb_mux_{video_path.stem}.mkv")
        brand = cfg.get("branding", "global_title_and_track_name", ".::EmpireBestTv.Com::.")

        try:
            # ۱. اعمال سینک روی یک کپی کاری از زیرنویس قبل از تبدیل به ASS
            working_srt.write_text(SubtitleService.read_subtitle_safe(primary_srt_path), encoding="utf-8-sig")
            if sync_mode == "auto" or cfg.get("subtitle_sync", "always_auto_sync", False):
                await MKVService.sync_sub_against_video(
                    video_path=video_path,
                    sub_to_sync=working_srt,
                    en_sub_reference=secondary_en_srt_path,
                    progress_callback=progress_callback
                )
            elif sync_mode == "manual" and sync_offset_ms != 0:
                SubtitleService.shift_srt_time(working_srt, working_srt, sync_offset_ms)

            # ۲. تبدیل زیرنویس سینک‌شده به فرمت ASS زرد (#fbff00) و بولد
            if progress_callback:
                progress_callback("آماده‌سازی استایل زرد (#fbff00) و بولد زیرنویس سینک‌شده...")
            SubtitleService.convert_srt_to_styled_ass(working_srt, temp_ass)

            # ۳. ساخت کانتینر موقت برای HandBrake
            if progress_callback:
                progress_callback("آماده‌سازی استریم موقت با تگ .::EmpireBestTv.Com::. ...")
            await MKVService.merge_softsub(
                video_path=video_path,
                output_path=temp_mkv,
                fa_sub_path=temp_ass,
                keep_old_subs=False,
                custom_brand=brand,
                sync_mode="none"
            )

            handbrake_bin = cfg.get("paths", "handbrake_bin")
            cmd = [
                handbrake_bin,
                "--preset-import-file", preset_json_path,
                "--preset", preset_name,
                "--input", str(temp_mkv),
                "--output", str(output_media_path),
                "--subtitle", "1",
                "--subtitle-burned=1",
            ]

            # افزودن زیرنویس انگلیسی به عنوان سافت‌ساب جانبی در صورت ارسال
            if secondary_en_srt_path and secondary_en_srt_path.exists():
                en_lang = cfg.get("branding", "sub_en_language", "eng")
                cmd.extend([
                    "--srt-file", str(secondary_en_srt_path),
                    "--srt-codeset", "UTF-8",
                    "--srt-lang", en_lang,
                    "--srt-default=0"
                ])

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

            # تثبیت تگ .::EmpireBestTv.Com::. در صورت خروجی MKV
            if output_media_path.suffix.lower() == ".mkv":
                tagged_mkv = output_media_path.with_name(f"_tagged_{output_media_path.name}")
                await MKVService.merge_softsub(
                    video_path=output_media_path,
                    output_path=tagged_mkv,
                    keep_old_subs=True,
                    custom_brand=brand
                )
                output_media_path.unlink(missing_ok=True)
                tagged_mkv.rename(output_media_path)

            return output_media_path

        finally:
            working_srt.unlink(missing_ok=True)
            temp_ass.unlink(missing_ok=True)
            temp_mkv.unlink(missing_ok=True)