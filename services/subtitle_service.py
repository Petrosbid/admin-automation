import asyncio
import re

from pathlib import Path
from typing import Callable

import chardet

from core.config_loader import cfg

from services.process_utils import (
    communicate_cancellable
)


class SubtitleService:

    SRT_TIME_PATTERN = re.compile(
        r"(\d{2}):(\d{2}):(\d{2})[,.](\d{3})\s*-->\s*(\d{2}):(\d{2}):(\d{2})[,.](\d{3})"
    )

    # ==================================================
    # ENCODING
    # ==================================================

    @staticmethod
    def read_subtitle_safe(
        sub_path: Path
    ) -> str:

        raw_bytes = Path(sub_path).read_bytes()

        detected = chardet.detect(
            raw_bytes
        )

        encoding = (
            detected.get("encoding")
            or "utf-8"
        )

        if encoding.lower() in [
            "ascii",
            "windows-1252",
            "iso-8859-1"
        ]:

            encoding = "cp1256"

        try:

            return raw_bytes.decode(
                "utf-8-sig"
            )

        except UnicodeDecodeError:

            return raw_bytes.decode(
                encoding,
                errors="replace"
            )

    @classmethod
    def normalize_srt_utf8(
        cls,
        sub_path: Path
    ) -> Path:

        content = cls.read_subtitle_safe(
            sub_path
        )

        sub_path.write_text(
            content,
            encoding="utf-8-sig"
        )

        return sub_path

    # ==================================================
    # TIME CONVERSION
    # ==================================================

    @staticmethod
    def _time_to_ms(
        h: int,
        m: int,
        s: int,
        ms: int
    ) -> int:

        return (
            (h * 3600)
            + (m * 60)
            + s
        ) * 1000 + ms

    @staticmethod
    def _ms_to_srt_time(
        total_ms: int
    ) -> str:

        total_ms = max(
            0,
            total_ms
        )

        h, rem = divmod(
            total_ms,
            3600_000
        )

        m, rem = divmod(
            rem,
            60_000
        )

        s, ms = divmod(
            rem,
            1000
        )

        return (
            f"{h:02d}:"
            f"{m:02d}:"
            f"{s:02d},"
            f"{ms:03d}"
        )

    # ==================================================
    # MANUAL SYNC
    # ==================================================

    @classmethod
    def shift_srt_time(
        cls,
        input_srt: Path,
        output_srt: Path,
        offset_ms: int
    ) -> Path:

        content = cls.read_subtitle_safe(
            input_srt
        )

        if offset_ms == 0:

            output_srt.write_text(
                content,
                encoding="utf-8-sig"
            )

            return output_srt

        def replace_time(
            match: re.Match
        ) -> str:

            (
                h1,
                m1,
                s1,
                ms1,
                h2,
                m2,
                s2,
                ms2
            ) = map(
                int,
                match.groups()
            )

            new_start = cls._ms_to_srt_time(
                cls._time_to_ms(
                    h1,
                    m1,
                    s1,
                    ms1
                ) + offset_ms
            )

            new_end = cls._ms_to_srt_time(
                cls._time_to_ms(
                    h2,
                    m2,
                    s2,
                    ms2
                ) + offset_ms
            )

            return (
                f"{new_start} --> {new_end}"
            )

        shifted_content = cls.SRT_TIME_PATTERN.sub(
            replace_time,
            content
        )

        output_srt.write_text(
            shifted_content,
            encoding="utf-8-sig"
        )

        return output_srt

    # ==================================================
    # AUTO SYNC
    # ==================================================

    @classmethod
    async def auto_sync_subtitle(
        cls,
        video_path: Path,
        input_srt: Path,
        output_srt: Path,
        reference_srt: Path | None = None,
        progress_callback: Callable[[str], None] = None
    ) -> Path:

        cls.normalize_srt_utf8(
            input_srt
        )

        ffsubsync_bin = cfg.get(
            "paths",
            "ffsubsync_bin",
            "ffsubsync"
        )

        ffmpeg_dir = cfg.get(
            "paths",
            "ffmpeg_dir",
            ""
        )

        ref_target = (
            str(reference_srt)
            if reference_srt and reference_srt.exists()
            else str(video_path)
        )

        if progress_callback:

            progress_callback(
                "در حال سینک هوشمند زیرنویس با فیلم (ffsubsync)..."
            )

        cmd = [
            ffsubsync_bin,
            ref_target,
            "-i",
            str(input_srt),
            "-o",
            str(output_srt),
            "--encoding",
            "utf-8"
        ]

        if ffmpeg_dir and Path(ffmpeg_dir).exists():

            cmd.extend([
                "--ffmpeg-path",
                ffmpeg_dir
            ])

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT
        )

        stdout, _ = await communicate_cancellable(
            proc
        )

        if proc.returncode != 0:

            raise RuntimeError(
                "خطا در سینک خودکار زیرنویس:\n"
                + stdout.decode(
                    "utf-8",
                    errors="replace"
                )
            )

        return output_srt

    # ==================================================
    # COLOR
    # ==================================================

    @staticmethod
    def html_hex_to_ass_bgr(
        hex_color: str
    ) -> str:

        clean_hex = hex_color.lstrip("#")

        r = clean_hex[0:2]
        g = clean_hex[2:4]
        b = clean_hex[4:6]

        return (
            f"&H00{b.upper()}"
            f"{g.upper()}"
            f"{r.upper()}"
        )

    # ==================================================
    # SRT STYLING
    # ==================================================

    @classmethod
    def style_srt_bold_yellow(
        cls,
        input_srt: Path,
        output_srt: Path
    ) -> Path:

        content = cls.read_subtitle_safe(
            input_srt
        )

        hex_color = cfg.get(
            "subtitle_style",
            "hex_color",
            "#fbff00"
        )

        blocks = re.split(
            r"\n\s*\n",
            content.strip()
        )

        styled_blocks = []

        for block in blocks:

            lines = [
                line.strip("\r")
                for line in block.split("\n")
                if line.strip()
            ]

            if len(lines) < 3:

                styled_blocks.append(block)

                continue

            idx_line = lines[0]

            time_line = lines[1]

            text_lines = lines[2:]

            cleaned_lines = [
                re.sub(
                    r"<[^>]+>",
                    "",
                    line
                )
                for line in text_lines
            ]

            joined_text = "\n".join(
                cleaned_lines
            )

            styled_text = (
                f'<b><font color="{hex_color}">'
                f'{joined_text}'
                f'</font></b>'
            )

            styled_blocks.append(
                f"{idx_line}\n"
                f"{time_line}\n"
                f"{styled_text}"
            )

        output_srt.write_text(
            "\n\n".join(styled_blocks) + "\n",
            encoding="utf-8-sig"
        )

        return output_srt

    # ==================================================
    # ASS CONVERSION
    # ==================================================

    @classmethod
    def convert_srt_to_styled_ass(
        cls,
        input_srt: Path,
        output_ass: Path
    ) -> Path:

        content = cls.read_subtitle_safe(
            input_srt
        )

        style_cfg = cfg.get(
            "subtitle_style"
        )

        ass_color = cls.html_hex_to_ass_bgr(
            style_cfg.get(
                "hex_color",
                "#fbff00"
            )
        )

        font_name = style_cfg.get(
            "font_name",
            "Tahoma"
        )

        font_size = style_cfg.get(
            "font_size",
            22
        )

        bold_flag = (
            -1
            if style_cfg.get("bold", True)
            else 0
        )

        outline = style_cfg.get(
            "outline_width",
            2.0
        )

        shadow = style_cfg.get(
            "shadow_depth",
            1.0
        )

        ass_header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: 1920
PlayResY: 1080
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,{font_name},{font_size},{ass_color},&H000000FF,&H00000000,&H80000000,{bold_flag},0,0,0,100,100,0,0,1,{outline},{shadow},2,30,30,45,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

        events = []

        blocks = re.split(
            r"\n\s*\n",
            content.strip()
        )

        def srt_time_to_ass(
            srt_time: str
        ) -> str:

            srt_time = (
                srt_time.strip()
                .replace(",", ".")
            )

            parts = srt_time.split(":")

            h = int(parts[0])

            m = parts[1]

            s_ms = parts[2][:5]

            return (
                f"{h}:{m}:{s_ms}"
            )

        for block in blocks:

            lines = [
                line.strip("\r")
                for line in block.split("\n")
                if line.strip()
            ]

            if len(lines) < 3:
                continue

            if "-->" not in lines[1]:
                continue

            start_raw, end_raw = lines[1].split(
                "-->"
            )

            start_ass = srt_time_to_ass(
                start_raw
            )

            end_ass = srt_time_to_ass(
                end_raw
            )

            clean_lines = [
                re.sub(
                    r"<[^>]+>",
                    "",
                    line
                )
                for line in lines[2:]
            ]

            dialogue_text = "\\N".join(
                clean_lines
            )

            events.append(
                f"Dialogue: 0,"
                f"{start_ass},"
                f"{end_ass},"
                f"Default,,0,0,0,,"
                f"{{\\b1\\c{ass_color}}}"
                f"{dialogue_text}"
            )

        output_ass.write_text(
            ass_header + "\n".join(events) + "\n",
            encoding="utf-8-sig"
        )

        return output_ass