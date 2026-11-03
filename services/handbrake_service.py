import asyncio
import json
import re
import uuid

from pathlib import Path
from typing import Callable

from core.config_loader import cfg

from services.subtitle_service import SubtitleService
from services.mkv_service import MKVService

from services.process_utils import terminate_process


class HandBrakeService:

    # ==================================================
    # PRESETS
    # ==================================================

    @staticmethod
    def list_available_presets() -> list[dict[str, str]]:

        presets_dir = Path(
            cfg.get("paths", "presets_dir")
        )

        presets = []

        for file_path in presets_dir.glob("*.json"):

            try:

                data = json.loads(
                    file_path.read_text(
                        encoding="utf-8"
                    )
                )

                preset_list = data.get(
                    "PresetList",
                    []
                )

                if preset_list:

                    preset_name = preset_list[0].get(
                        "PresetName",
                        file_path.stem
                    )

                    presets.append({
                        "file": str(file_path),
                        "name": preset_name,
                        "label": (
                            f"{preset_name} "
                            f"({file_path.name})"
                        )
                    })

            except Exception:
                continue

        return presets

    # ==================================================
    # HARDSUB ENCODING
    # ==================================================

    @classmethod
    async def encode_hardsub(
        cls,
        video_path: Path,
        primary_srt_path: Path,
        output_media_path: Path,
        preset_json_path: str,
        preset_name: str,
        secondary_en_srt_path: Path | None = None,
        sync_mode: str = "none",
        sync_offset_ms: int = 0,
        progress_callback: Callable[[str], None] = None
    ) -> Path:

        output_media_path.parent.mkdir(
            parents=True,
            exist_ok=True
        )

        unique_tag = str(uuid.uuid4())[:8]

        working_srt = output_media_path.with_name(
            f"_temp_sync_{unique_tag}.srt"
        )

        temp_ass = output_media_path.with_name(
            f"_temp_styled_{unique_tag}.ass"
        )

        temp_mkv = output_media_path.with_name(
            f"_temp_hb_mux_{unique_tag}.mkv"
        )

        brand = cfg.get(
            "branding",
            "global_title_and_track_name",
            ".::EmpireBestTv.Com::."
        )

        proc = None

        try:

            if not primary_srt_path or not Path(primary_srt_path).is_file():

                raise FileNotFoundError(
                    "زیرنویس اصلی برای هاردساب موجود نیست."
                )

            # ==========================================
            # 1. COPY SUBTITLE
            # ==========================================

            working_srt.write_text(
                SubtitleService.read_subtitle_safe(
                    primary_srt_path
                ),
                encoding="utf-8-sig"
            )

            # ==========================================
            # 2. SYNC
            # ==========================================

            if (
                sync_mode == "auto"
                or cfg.get(
                    "subtitle_sync",
                    "always_auto_sync",
                    False
                )
            ):

                await MKVService.sync_sub_against_video(
                    video_path=video_path,
                    sub_to_sync=working_srt,
                    en_sub_reference=secondary_en_srt_path,
                    progress_callback=progress_callback
                )

            elif (
                sync_mode == "manual"
                and sync_offset_ms != 0
            ):

                SubtitleService.shift_srt_time(
                    working_srt,
                    working_srt,
                    sync_offset_ms
                )

            # ==========================================
            # 3. STYLE
            # ==========================================

            if progress_callback:

                progress_callback(
                    "اعمال استایل بولد و زرد برای هاردساب..."
                )

            SubtitleService.convert_srt_to_styled_ass(
                working_srt,
                temp_ass
            )

            # ==========================================
            # 4. PREPARE TEMP MKV
            # ==========================================

            if progress_callback:

                progress_callback(
                    "آماده‌سازی فایل موقت MKV..."
                )

            await MKVService.merge_softsub(
                video_path=video_path,
                output_path=temp_mkv,
                fa_sub_path=temp_ass,
                keep_old_subs=False,
                custom_brand=brand,
                sync_mode="none"
            )

            # ==========================================
            # 5. HANDBRAKE COMMAND
            # ==========================================

            handbrake_bin = cfg.get(
                "paths",
                "handbrake_bin"
            )

            cmd = [
                handbrake_bin,
                "--preset-import-file",
                preset_json_path,
                "--preset",
                preset_name,
                "--input",
                str(temp_mkv),
                "--output",
                str(output_media_path),
                "--subtitle",
                "1",
                "--subtitle-burned=1"
            ]

            if progress_callback:

                progress_callback(
                    "HandBrake شروع به Encoding کرد..."
                )

            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT
            )

            # ==========================================
            # 6. READ PROGRESS
            # ==========================================

            buffer = ""

            while True:

                chunk = await proc.stdout.read(256)

                if not chunk:
                    break

                buffer += chunk.decode(
                    "utf-8",
                    errors="replace"
                )

                lines = re.split(
                    r"[\r\n]+",
                    buffer
                )

                buffer = lines.pop()

                for line in lines:

                    if (
                        "Encoding:" in line
                        and progress_callback
                    ):

                        progress_callback(
                            line.replace(
                                "Encoding:",
                                ""
                            ).strip()
                        )

            await proc.wait()

            if proc.returncode != 0:

                raise RuntimeError(
                    f"HandBrakeCLI exited with code {proc.returncode}"
                )

            proc = None

            # ==========================================
            # 7. FINAL MKV TAGGING
            # ==========================================

            if output_media_path.suffix.lower() == ".mkv":

                tagged_mkv = output_media_path.with_name(
                    f"_tagged_{unique_tag}_{output_media_path.name}"
                )

                await MKVService.merge_softsub(
                    video_path=output_media_path,
                    output_path=tagged_mkv,
                    keep_old_subs=False,
                    custom_brand=brand
                )

                output_media_path.unlink(
                    missing_ok=True
                )

                tagged_mkv.rename(
                    output_media_path
                )

            if progress_callback:

                progress_callback(
                    "هاردساب با موفقیت تکمیل شد."
                )

            return output_media_path

        except asyncio.CancelledError:

            if proc:

                await terminate_process(proc)

            # پاک کردن فایل خروجی ناقص
            output_media_path.unlink(
                missing_ok=True
            )

            raise

        finally:

            if proc:

                await terminate_process(proc)

            working_srt.unlink(
                missing_ok=True
            )

            temp_ass.unlink(
                missing_ok=True
            )

            temp_mkv.unlink(
                missing_ok=True
            )