import asyncio
import re
from pathlib import Path
from typing import Callable
import chardet
from core.config_loader import cfg


class SubtitleService:
    SRT_TIME_PATTERN = re.compile(
        r"(\d{2}):(\d{2}):(\d{2})[,.](\d{3})\s*-->\s*(\d{2}):(\d{2}):(\d{2})[,.](\d{3})"
    )

    @staticmethod
    def read_subtitle_safe(sub_path: Path) -> str:
        """خواندن ایمن زیرنویس فارسی با تشخیص خودکار انکودینگ"""
        raw_bytes = sub_path.read_bytes()
        detected = chardet.detect(raw_bytes)
        encoding = detected.get("encoding") or "utf-8"
        if encoding.lower() in ["ascii", "windows-1252", "iso-8859-1"]:
            encoding = "cp1256"
        try:
            return raw_bytes.decode("utf-8-sig")
        except UnicodeDecodeError:
            return raw_bytes.decode(encoding, errors="replace")

    @classmethod
    def normalize_srt_utf8(cls, sub_path: Path) -> Path:
        """ذخیره زیرنویس با انکودینگ استاندارد UTF-8 بدون تغییر رنگ یا بولد کردن (مخصوص سافت‌ساب)"""
        content = cls.read_subtitle_safe(sub_path)
        sub_path.write_text(content, encoding="utf-8-sig")
        return sub_path

    @staticmethod
    def _time_to_ms(h: int, m: int, s: int, ms: int) -> int:
        return ((h * 3600) + (m * 60) + s) * 1000 + ms

    @staticmethod
    def _ms_to_srt_time(total_ms: int) -> str:
        if total_ms < 0:
            total_ms = 0
        h, rem = divmod(total_ms, 3600_000)
        m, rem = divmod(rem, 60_000)
        s, ms = divmod(rem, 1000)
        return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

    @classmethod
    def shift_srt_time(cls, input_srt: Path, output_srt: Path, offset_ms: int) -> Path:
        """سینک دستی زیرنویس با شیفت دادن تمامی تایم‌کدها به اندازه offset_ms"""
        content = cls.read_subtitle_safe(input_srt)
        if offset_ms == 0:
            output_srt.write_text(content, encoding="utf-8-sig")
            return output_srt

        def _replace_time(match: re.Match) -> str:
            h1, m1, s1, ms1, h2, m2, s2, ms2 = map(int, match.groups())
            new_start = cls._ms_to_srt_time(cls._time_to_ms(h1, m1, s1, ms1) + offset_ms)
            new_end = cls._ms_to_srt_time(cls._time_to_ms(h2, m2, s2, ms2) + offset_ms)
            return f"{new_start} --> {new_end}"

        shifted_content = cls.SRT_TIME_PATTERN.sub(_replace_time, content)
        output_srt.write_text(shifted_content, encoding="utf-8-sig")
        return output_srt

    @classmethod
    async def auto_sync_subtitle(
        cls,
        video_path: Path,
        input_srt: Path,
        output_srt: Path,
        reference_srt: Path | None = None,
        progress_callback: Callable[[str], None] = None
    ) -> Path:
        """سینک خودکار زیرنویس با فیلم یا زیرنویس مرجع توسط ffsubsync"""
        cls.normalize_srt_utf8(input_srt)

        ffsubsync_bin = cfg.get("paths", "ffsubsync_bin", "ffsubsync")
        ffmpeg_dir = cfg.get("paths", "ffmpeg_dir", "")

        ref_target = str(reference_srt) if (reference_srt and reference_srt.exists()) else str(video_path)
        if progress_callback:
            progress_callback("در حال سینک هوشمند زیرنویس با فیلم (ffsubsync)...")

        cmd = [
            ffsubsync_bin,
            ref_target,
            "-i", str(input_srt),
            "-o", str(output_srt),
            "--encoding", "utf-8",
        ]
        if ffmpeg_dir and Path(ffmpeg_dir).exists():
            cmd.extend(["--ffmpeg-path", ffmpeg_dir])

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT
        )
        stdout, _ = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(f"خطا در سینک خودکار زیرنویس: {stdout.decode('utf-8', errors='replace')}")

        return output_srt

    @staticmethod
    def html_hex_to_ass_bgr(hex_color: str) -> str:
        """تبدیل رنگ #fbff00 به فرمت استاندارد ASS یعنی &H00BBGGRR&"""
        clean_hex = hex_color.lstrip("#")
        r, g, b = clean_hex[0:2], clean_hex[2:4], clean_hex[4:6]
        return f"&H00{b.upper()}{g.upper()}{r.upper()}"

    @classmethod
    def style_srt_bold_yellow(cls, input_srt: Path, output_srt: Path) -> Path:
        """بولد کردن تمام خطوط و تغییر رنگ به #fbff00 در فایل SRT"""
        content = cls.read_subtitle_safe(input_srt)
        hex_color = cfg.get("subtitle_style", "hex_color", "#fbff00")

        blocks = re.split(r"\n\s*\n", content.strip())
        styled_blocks = []

        for block in blocks:
            lines = [line.strip("\r") for line in block.split("\n") if line.strip()]
            if len(lines) < 3:
                styled_blocks.append(block)
                continue

            idx_line = lines[0]
            time_line = lines[1]
            text_lines = lines[2:]

            cleaned_lines = [re.sub(r"<[^>]+>", "", line) for line in text_lines]
            joined_text = "\n".join(cleaned_lines)

            styled_text = f'<b><font color="{hex_color}">{joined_text}</font></b>'
            styled_blocks.append(f"{idx_line}\n{time_line}\n{styled_text}")

        output_srt.write_text("\n\n".join(styled_blocks) + "\n", encoding="utf-8-sig")
        return output_srt

    @classmethod
    def convert_srt_to_styled_ass(cls, input_srt: Path, output_ass: Path) -> Path:
        """تبدیل SRT به فرمت ASS با رنگ #fbff00 و بولد کامل منحصراً برای هاردساب در HandBrake"""
        content = cls.read_subtitle_safe(input_srt)
        style_cfg = cfg.get("subtitle_style")
        ass_color = cls.html_hex_to_ass_bgr(style_cfg.get("hex_color", "#fbff00"))
        font_name = style_cfg.get("font_name", "Tahoma")
        font_size = style_cfg.get("font_size", 22)
        bold_flag = -1 if style_cfg.get("bold", True) else 0
        outline = style_cfg.get("outline_width", 2.0)
        shadow = style_cfg.get("shadow_depth", 1.0)

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
        blocks = re.split(r"\n\s*\n", content.strip())

        def srt_time_to_ass(srt_t: str) -> str:
            srt_t = srt_t.strip().replace(",", ".")
            parts = srt_t.split(":")
            h = int(parts[0])
            m = parts[1]
            s_ms = parts[2][:5]
            return f"{h}:{m}:{s_ms}"

        for block in blocks:
            lines = [line.strip("\r") for line in block.split("\n") if line.strip()]
            if len(lines) < 3 or "-->" not in lines[1]:
                continue
            start_raw, end_raw = lines[1].split("-->")
            start_ass = srt_time_to_ass(start_raw)
            end_ass = srt_time_to_ass(end_raw)

            clean_lines = [re.sub(r"<[^>]+>", "", l) for l in lines[2:]]
            dialogue_text = "\\N".join(clean_lines)
            events.append(f"Dialogue: 0,{start_ass},{end_ass},Default,,0,0,0,,{{\\b1\\c{ass_color}}}{dialogue_text}")

        output_ass.write_text(ass_header + "\n".join(events) + "\n", encoding="utf-8-sig")
        return output_ass