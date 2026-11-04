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
        primary_srt_path: Path | None,
        output_media_path: Path,
        preset_json_path: str,
        preset_name: str,
        secondary_en_srt_path: Path | None = None,
        sync_mode: str = "none",     # 'none' | 'auto' | 'manual'
        sync_offset_ms: int = 0,
        progress_callback: Callable[[str], None] = None
    ) -> Path:
        """
        منطق جدید هاردساب با HandBrakeCLI طبق خواسته شما:
        ۱. اگر زیرنویس اصلی از نوع .ass یا .ssa باشد:
           بدون هیچ تغییر رنگ زرد یا بولد کردن، دقیقاً با استایل اصلی خودش هاردساب (Burn-in) می‌شود.
        ۲. اگر زیرنویس اصلی از نوع .srt باشد:
           به صورت SRT بولد و زرد (#fbff00) با سایز فونت پیش‌فرض و بزرگ هاردساب می‌شود.
        ۳. وضعیت زیرنویس انگلیسی (ارسالی یا موجود در خود فایل فیلم):
           اگر زیرنویس انگلیسی آپلود شده باشد و یا از قبل داخل خود فایل فیلم وجود داشته باشد،
           بدون هاردساب شدن (بدون تیک Burn-in) و بدون بولد و زرد شدن، به عنوان ترک زیرنویس سافت‌ساب به فایل خروجی اضافه می‌شود.
        """
        output_media_path.parent.mkdir(parents=True, exist_ok=True)
        unique_tag = str(uuid.uuid4())[:6]
        working_sub = None
        temp_styled_srt = output_media_path.with_name(f"_temp_styled_{unique_tag}.srt")
        temp_mkv = output_media_path.with_name(f"_temp_hb_mux_{unique_tag}.mkv")
        brand = cfg.get("branding", "global_title_and_track_name", ".::EmpireBestTv.Com::.")
        handbrake_bin = cfg.get("paths", "handbrake_bin")

        try:
            has_primary_sub = bool(primary_srt_path and Path(primary_srt_path).exists())
            has_secondary_en = bool(secondary_en_srt_path and Path(secondary_en_srt_path).exists())

            burn_in_sub_path: Path | None = None

            if has_primary_sub:
                primary_srt_path = Path(primary_srt_path)
                ext_lower = primary_srt_path.suffix.lower()
                working_sub = output_media_path.with_name(f"_temp_sync_{unique_tag}{ext_lower}")
                working_sub.write_text(SubtitleService.read_subtitle_safe(primary_srt_path), encoding="utf-8-sig")

                # اجرای سینک در صورت انتخاب کاربر
                if sync_mode == "auto" or cfg.get("subtitle_sync", "always_auto_sync", False):
                    await MKVService.sync_sub_against_video(
                        video_path=video_path,
                        sub_to_sync=working_sub,
                        en_sub_reference=secondary_en_srt_path,
                        progress_callback=progress_callback
                    )
                elif sync_mode == "manual" and sync_offset_ms != 0 and ext_lower == ".srt":
                    SubtitleService.shift_srt_time(working_sub, working_sub, sync_offset_ms)

                # شرط مهم ۲: اگر فایل زیرنویس .ass یا .ssa بود، بدون تغییر رنگ و بدون بولد کردن همان را هاردساب کن
                if ext_lower in (".ass", ".ssa"):
                    if progress_callback:
                        progress_callback("فایل ASS تشخیص داده شد؛ هاردساب با استایل اصلی بدون تغییر رنگ...")
                    burn_in_sub_path = working_sub
                else:
                    # اگر فایل .srt بود، بولد و زرد (#fbff00) کن
                    if progress_callback:
                        progress_callback("آماده‌سازی SRT بولد و زرد (#fbff00) برای هاردساب...")
                    SubtitleService.style_srt_bold_yellow(working_sub, temp_styled_srt)
                    burn_in_sub_path = temp_styled_srt

            # ساخت فایل موقت MKV با keep_old_subs=True:
            # متد merge_softsub به صورت هوشمند:
            # - اگر secondary_en_srt_path داده شده باشد آن را بدون تغییر رنگ اضافه می‌کند
            # - اگر secondary_en_srt_path نداده باشید اما داخل خود فیلم زیرنویس انگلیسی وجود داشته باشد،
            #   اولین زیرنویس انگلیسیِ خودِ فیلم را نگه می‌دارد!
            if progress_callback:
                progress_callback("آماده‌سازی ترک‌های زیرنویس در کانتینر موقت...")

            await MKVService.merge_softsub(
                video_path=video_path,
                output_path=temp_mkv,
                fa_sub_path=burn_in_sub_path,
                en_sub_path=secondary_en_srt_path if has_secondary_en else None,
                keep_old_subs=True,  # نگه داشتن زیرنویس انگلیسی داخل فیلم در صورت عدم آپلود زیرنویس انگلیسی
                custom_brand=brand,
                sync_mode="none"
            )

            # بررسی ترک‌های زیرنویس موجود در فایل موقت temp_mkv
            temp_sub_tracks = await MKVService.list_subtitle_tracks(temp_mkv)
            num_subs = len(temp_sub_tracks)

            cmd = [
                handbrake_bin,
                "--preset-import-file", preset_json_path,
                "--preset", preset_name,
                "--input", str(temp_mkv),
                "--output", str(output_media_path),
            ]

            if burn_in_sub_path is not None:
                if num_subs >= 2:
                    # ترک ۱ (فارسی) هاردساب (Burn-in) شود + ترک ۲ (انگلیسی) بدون Burn-in و بدون زرد شدن به فایل اضافه شود
                    cmd.extend([
                        "--subtitle", "1,2",
                        "--subtitle-burned=1",
                        "--subtitle-default=none",
                    ])
                else:
                    # فقط ترک ۱ (فارسی) وجود دارد -> هاردساب شود
                    cmd.extend([
                        "--subtitle", "1",
                        "--subtitle-burned=1",
                    ])
            else:
                # کاربر هیچ زیرنویسی برای هاردساب نداده است:
                if num_subs >= 1:
                    # اگر داخل خود فیلم زیرنویس انگلیسی بوده (یا فقط انگلیسی آپلود شده)، بدون Burn-in به فایل اضافه شود
                    cmd.extend([
                        "--subtitle", "1",
                        "--subtitle-burned=none",
                        "--subtitle-default=none",
                    ])
                else:
                    # هیچ زیرنویسی وجود ندارد -> انکود کاملاً بدون زیرنویس
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

            if output_media_path.suffix.lower() == ".mkv":
                tagged_mkv = output_media_path.with_name(f"_tagged_{unique_tag}_{output_media_path.name}")
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
            if working_sub and working_sub.exists():
                working_sub.unlink(missing_ok=True)
            temp_styled_srt.unlink(missing_ok=True)
            temp_mkv.unlink(missing_ok=True)