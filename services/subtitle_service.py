import asyncio
import re
import shutil
import subprocess
import uuid
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
    ARCHIVE_EXTENSIONS = {".zip", ".rar", ".7z"}

    @staticmethod
    def read_subtitle_safe(sub_path: Path) -> str:
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
        content = cls.read_subtitle_safe(sub_path)
        sub_path.write_text(content, encoding="utf-8-sig")
        return sub_path

    @staticmethod
    def _find_archive_tool() -> tuple[str | None, str]:
        """
        پیدا کردن ابزار اکسترکت RAR / 7Z / ZIP روی ویندوز سرور:
        ۱. WinRAR / UnRAR
        ۲. 7-Zip (7z.exe)
        ۳. ابزار داخلی ویندوز ۱۰/۱۱ (tar.exe که مبتنی بر libarchive است و RAR/ZIP/7Z را باز می‌کند)
        """
        winrar_paths = [
            shutil.which("unrar"),
            shutil.which("winrar"),
            r"C:\Program Files\WinRAR\UnRAR.exe",
            r"C:\Program Files\WinRAR\WinRAR.exe",
            r"C:\Program Files (x86)\WinRAR\UnRAR.exe",
            r"C:\Program Files (x86)\WinRAR\WinRAR.exe",
        ]
        for p in winrar_paths:
            if p and Path(p).exists():
                return str(Path(p)), "unrar"

        seven_zip_paths = [
            shutil.which("7z"),
            r"C:\Program Files\7-Zip\7z.exe",
            r"C:\Program Files (x86)\7-Zip\7z.exe",
            r"C:\Tools\7zip\7z.exe",
        ]
        for p in seven_zip_paths:
            if p and Path(p).exists():
                return str(Path(p)), "7z"

        tar_bin = shutil.which("tar") or r"C:\Windows\System32\tar.exe"
        if tar_bin and Path(tar_bin).exists():
            return str(Path(tar_bin)), "tar"

        return None, ""

    @classmethod
    def extract_archive(
        cls,
        archive_path: Path,
        extract_to: Path,
        remove_archive: bool = False,
        flatten_subtitles: bool = True
    ) -> list[Path]:
        """
        اکسترکت هوشمند انواع فایل‌های فشرده (.zip, .rar, .7z):
        - برای .zip ابتدا از ماژول داخلی پایتون استفاده می‌کند.
        - برای .rar و .7z (یا زیپ‌های خاص) از WinRAR، 7-Zip، tar.exe داخلی ویندوز و یا rarfile استفاده می‌کند.
        """
        archive_path = Path(archive_path).resolve()
        extract_to = Path(extract_to).resolve()
        extract_to.mkdir(parents=True, exist_ok=True)

        temp_extract_dir = extract_to / f"_tmp_ext_{uuid.uuid4().hex[:6]}"
        temp_extract_dir.mkdir(parents=True, exist_ok=True)

        ext_low = archive_path.suffix.lower()
        extracted_ok = False
        last_err = ""

        try:
            # ۱. اگر فایل استاندارد ZIP است، ابتدا با zipfile پایتون باز کن
            if ext_low == ".zip" and zipfile.is_zipfile(archive_path):
                try:
                    with zipfile.ZipFile(archive_path, "r") as zf:
                        for member in zf.infolist():
                            if member.is_dir():
                                continue
                            raw_filename = member.filename
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
                            dest_f = temp_extract_dir.joinpath(*rel_parts)
                            dest_f.parent.mkdir(parents=True, exist_ok=True)
                            with zf.open(member) as src, open(dest_f, "wb") as dst:
                                dst.write(src.read())
                    extracted_ok = True
                except Exception as ez:
                    last_err = str(ez)

            # ۲. اگر فایل RAR یا 7Z است (یا باز کردن ZIP با خطا مواجه شد)، از ابزارهای سیستمی استفاده کن
            if not extracted_ok:
                tool_bin, tool_type = cls._find_archive_tool()

                if tool_type == "unrar":
                    cmd = [tool_bin, "x", "-y", "-o+", str(archive_path), f"{temp_extract_dir}\\"]
                    res = subprocess.run(cmd, capture_output=True, text=True)
                    if res.returncode == 0 and any(temp_extract_dir.rglob("*")):
                        extracted_ok = True
                    else:
                        last_err = res.stderr or res.stdout

                if not extracted_ok and tool_type == "7z":
                    cmd = [tool_bin, "x", "-y", f"-o{temp_extract_dir}", str(archive_path)]
                    res = subprocess.run(cmd, capture_output=True, text=True)
                    if res.returncode == 0 and any(temp_extract_dir.rglob("*")):
                        extracted_ok = True
                    else:
                        last_err = res.stderr or res.stdout

                # امتحان کردن tar.exe داخلی ویندوز ۱۰/۱۱ (bsdtar از فرمت‌های RAR، ZIP و 7Z پشتیبانی می‌کند)
                if not extracted_ok:
                    tar_bin = shutil.which("tar") or r"C:\Windows\System32\tar.exe"
                    if tar_bin and Path(tar_bin).exists():
                        cmd = [str(tar_bin), "-xf", str(archive_path), "-C", str(temp_extract_dir)]
                        res = subprocess.run(cmd, capture_output=True, text=True)
                        if res.returncode == 0 and any(temp_extract_dir.rglob("*")):
                            extracted_ok = True
                        else:
                            last_err = res.stderr or res.stdout

                # امتحان کردن کتابخانه پایتونی rarfile در صورت نصب بودن
                if not extracted_ok and ext_low == ".rar":
                    try:
                        import rarfile
                        with rarfile.RarFile(archive_path) as rf:
                            rf.extractall(temp_extract_dir)
                        extracted_ok = True
                    except Exception as er:
                        last_err = f"{last_err} | {er}"

            if not extracted_ok:
                raise RuntimeError(
                    f"عدم موفقیت در استخراج فایل {archive_path.name}. "
                    "برای فایل‌های RAR5 مطمئن شوید WinRAR یا 7-Zip روی ویندوز سرور نصب باشد. "
                    f"({last_err[:150]})"
                )

            # ۳. انتقال فایل‌های استخراج‌شده از پوشه موقت به پوشه مقصد و نرمال‌سازی زیرنویس‌های SRT
            final_files: list[Path] = []
            for item in temp_extract_dir.rglob("*"):
                if not item.is_file():
                    continue
                if item.name.startswith("._") or "__MACOSX" in item.parts:
                    continue

                if flatten_subtitles and item.suffix.lower() in cls.SUB_EXTENSIONS:
                    target_path = extract_to / item.name
                else:
                    rel = item.relative_to(temp_extract_dir)
                    target_path = extract_to / rel

                target_path.parent.mkdir(parents=True, exist_ok=True)
                if target_path.exists():
                    target_path.unlink(missing_ok=True)
                shutil.move(str(item), str(target_path))

                if target_path.suffix.lower() == ".srt":
                    try:
                        cls.normalize_srt_utf8(target_path)
                    except Exception:
                        pass

                final_files.append(target_path.resolve())

            if remove_archive and archive_path.exists():
                archive_path.unlink(missing_ok=True)

            return final_files

        finally:
            if temp_extract_dir.exists():
                shutil.rmtree(temp_extract_dir, ignore_errors=True)

    # حفظ نام متد قبلی به عنوان Alias تا تمام بخش‌های کد با هر سه فرمت ZIP/RAR/7Z کار کنند
    extract_zip_archive = extract_archive

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
        content = cls.read_subtitle_safe(input_srt)
        hex_color = cfg.get("subtitle_style", "hex_color", "#fbff00")

        blocks = re.split(r"\n\s*\n", content.strip())
        styled_blocks = []

        for blocks_item in blocks:
            lines = [line.strip("\r") for line in blocks_item.split("\n") if line.strip()]
            if len(lines) < 3:
                styled_blocks.append(blocks_item)
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