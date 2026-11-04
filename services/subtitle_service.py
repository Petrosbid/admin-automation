import asyncio
import re
import zipfile
from pathlib import Path
from typing import Callable
import chardet
from core.config_loader import cfg


class SubtitleService:
    SRT_TIME_PATTERN = re.compile(
        r"(\d{2}):(\d{2}):(\d{2})[,.](\d{3})\s*-->\s*(\d{2}):(\d{2}):(\d{2})[,.](\d{3})"
    )
    SUB_EXTENSIONS = {".srt", ".ass", ".ssa"}

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

    @classmethod
    def extract_zip_archive(cls, zip_path: Path, extract_to: Path, remove_zip: bool = False) -> list[Path]:
        """
        اکسترکت کامل فایل ZIP در مسیر مقصد (با پشتیبانی از کاراکترهای فارسی در نام فایل‌ها)
        و نرمال‌سازی خودکار فایل‌های .srt استخراج‌شده به UTF-8.
        """
        extract_to.mkdir(parents=True, exist_ok=True)
        extracted_files: list[Path] = []

        with zipfile.ZipFile(zip_path, "r") as zf:
            for member in zf.infolist():
                if member.is_dir():
                    continue

                raw_filename = member.filename
                # اصلاح انکودینگ نام فایل‌های فارسی در فایل‌های زیپ ویندوزی قدیمی
                if not (member.flag_bits & 0x800):
                    try:
                        raw_filename = raw_filename.encode("cp437").decode("utf-8")
                    except Exception:
                        try:
                            raw_filename = raw_filename.encode("cp437").decode("cp1256", errors="replace")
                        except Exception:
                            pass

                rel_parts = [
                    re.sub(r'[<>:"/\\|?*]', "_", p).strip()
                    for p in Path(raw_filename).parts
                    if p not in ("", ".", "..", "/", "\\") and not p.startswith("__MACOSX")
                ]
                if not rel_parts:
                    continue

                # اگر فایل داخل پوشه‌های تودرتوی زیپ یک زیرنویس است، آن را مستقیم در پوشه هدف بریز تا به راحتی پیدا شود
                suffix_low = Path(rel_parts[-1]).suffix.lower()
                if suffix_low in cls.SUB_EXTENSIONS:
                    target_path = extract_to / rel_parts[-1]
                else:
                    target_path = extract_to.joinpath(*rel_parts)

                target_path.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(member) as src, open(target_path, "wb") as dst:
                    dst.write(src.read())

                if target_path.suffix.lower() == ".srt":
                    try:
                        cls.normalize_srt_utf8(target_path)
                    except Exception:
                        pass

                extracted_files.append(target_path.resolve())

        if remove_zip and zip_path.exists():
            zip_path.unlink(missing_ok=True)

        return extracted_files

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
        clean_hex = hex_color.lstrip("#")
        r, g, b = clean_hex[0:2], clean_hex[2:4], clean_hex[4:6]
        return f"&H00{b.upper()}{g.upper()}{r.upper()}"

    @classmethod
    def style_srt_bold_yellow(cls, input_srt: Path, output_srt: Path) -> Path:
        """بولد کردن تمام خطوط و تغییر رنگ به #fbff00 در فایل SRT با حفظ سایز فونت پیش‌فرض"""
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