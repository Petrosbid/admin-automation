import asyncio
import html
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
        """خواندن ایمن فایل زیرنویس با تشخیص خودکار انکودینگ و حذف بایت‌های مخرب"""
        raw_bytes = sub_path.read_bytes()
        # حذف کاراکترهای Null که باعث خراب شدن فایل در پلیرها و نوت‌پد ویندوز می‌شوند
        raw_bytes = raw_bytes.replace(b"\x00", b"")

        # بررسی BOM های استاندارد
        if raw_bytes.startswith(b"\xef\xbb\xbf"):
            text = raw_bytes.decode("utf-8-sig", errors="replace")
        elif raw_bytes.startswith(b"\xff\xfe") or raw_bytes.startswith(b"\xfe\xff"):
            text = raw_bytes.decode("utf-16", errors="replace")
        else:
            try:
                text = raw_bytes.decode("utf-8")
            except UnicodeDecodeError:
                detected = chardet.detect(raw_bytes)
                encoding = (detected.get("encoding") or "cp1256").lower()
                if encoding in ("ascii", "windows-1252", "iso-8859-1", "macroman"):
                    encoding = "cp1256"
                try:
                    text = raw_bytes.decode(encoding, errors="replace")
                except Exception:
                    text = raw_bytes.decode("cp1256", errors="replace")

        # پاکسازی کاراکترهای کنترلی غیرمجاز و یکسان‌سازی پایان خطوط
        text = text.lstrip("\ufeff")
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        return text

    @classmethod
    def normalize_subtitle_file(cls, sub_path: Path) -> Path:
        """
        نرمال‌سازی کامل فایل زیرنویس (.srt, .ass, .ssa) به فرمت استاندارد ویندوز (CRLF + UTF-8-BOM)
        تا بدون نیاز به باز کردن و Save As دستی، در تمام پلیرها و نرم‌افزارها به درستی باز شود.
        """
        sub_path = Path(sub_path)
        if not sub_path.exists() or sub_path.suffix.lower() not in cls.SUB_EXTENSIONS:
            return sub_path

        content = cls.read_subtitle_safe(sub_path)
        ext_low = sub_path.suffix.lower()

        if ext_low == ".srt":
            # بازسازی ساختار بلوک‌های SRT برای رفع به هم ریختگی شماره‌ها یا خطوط خالی
            blocks = re.split(r"\n\s*\n", content.strip())
            clean_blocks = []
            counter = 1
            for b in blocks:
                lines = [ln.strip() for ln in b.split("\n") if ln.strip()]
                if not lines:
                    continue
                # پیدا کردن خط تایم‌کد
                time_idx = -1
                for i, ln in enumerate(lines):
                    if "-->" in ln:
                        time_idx = i
                        break
                if time_idx == -1:
                    continue
                time_line = re.sub(r"(\d{2}:\d{2}:\d{2})\.(\d{3})", r"\1,\2", lines[time_idx])
                text_lines = lines[time_idx + 1:]
                if not text_lines:
                    continue
                clean_blocks.append(f"{counter}\n{time_line}\n" + "\n".join(text_lines))
                counter += 1

            if clean_blocks:
                content = "\n\n".join(clean_blocks) + "\n"

        # تبدیل تمام خطوط به استاندارد ویندوز (CRLF: \r\n) و ذخیره با امضای UTF-8-SIG (BOM)
        windows_content = content.replace("\r\n", "\n").replace("\n", "\r\n")
        with open(sub_path, "wb") as f:
            f.write(windows_content.encode("utf-8-sig"))

        return sub_path

    # حفظ نام قبلی برای سازگاری با کل پروژه
    normalize_srt_utf8 = normalize_subtitle_file

    @classmethod
    def convert_ass_to_srt_file(cls, ass_path: Path, srt_out_path: Path | None = None) -> Path:
        """
        تبدیل کامل و حرفه‌ای فایل زیرنویس .ass / .ssa به فایل .srt استاندارد:
        - حذف کامل تگ‌های استایل ASS مانند {\pos(...)}, {\fad(...)}, {\c&H...&}, {\fn...}
        - حذف خطوط ترسیم وکتور گرافیکی ({\p1}...{\p0}) تا کدهای مختصات در زیرنویس نمایش داده نشوند
        - تبدیل \N و \n به خط جدید واقعی و تبدیل \h به فاصله
        - مرتب‌سازی بر اساس زمان شروع و شماره‌گذاری استاندارد با فرمت UTF-8-SIG و CRLF
        """
        ass_path = Path(ass_path).resolve()
        if srt_out_path is None:
            srt_out_path = ass_path.with_suffix(".srt")
        else:
            srt_out_path = Path(srt_out_path).resolve()

        content = cls.read_subtitle_safe(ass_path)

        def _ass_time_to_ms_and_srt(t_str: str) -> tuple[int, str]:
            parts = t_str.strip().split(":")
            if len(parts) != 3:
                return 0, "00:00:00,000"
            try:
                h = int(parts[0])
                m = int(parts[1])
                s_parts = parts[2].replace(",", ".").split(".")
                s = int(s_parts[0])
                frac = s_parts[1] if len(s_parts) > 1 else "0"
                ms = int((frac + "000")[:3])
                total_ms = ((h * 3600) + (m * 60) + s) * 1000 + ms
                return total_ms, f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"
            except Exception:
                return 0, "00:00:00,000"

        parsed_events: list[tuple[int, int, str, str, str]] = []

        for line in content.splitlines():
            line_str = line.strip()
            if not line_str.startswith("Dialogue:"):
                continue

            fields = line_str[9:].split(",", 9)
            if len(fields) < 10:
                continue

            start_ms, start_srt = _ass_time_to_ms_and_srt(fields[1])
            end_ms, end_srt = _ass_time_to_ms_and_srt(fields[2])
            if end_ms <= start_ms:
                continue

            raw_text = fields[9]

            # ۱. اگر خط حاوی دستورات ترسیم شکل گرافیکی ASS ({\p1} تا {\p0}) است، آن را کاملاً حذف کن
            if re.search(r"\{\\[^}]*\\p[1-9][^}]*\}", raw_text):
                raw_text = re.sub(r"\{\\[^}]*\\p[1-9][^}]*\}.*?(?:\{\\[^}]*\\p0[^}]*\}|$)", "", raw_text)
                if not raw_text.strip():
                    continue

            # ۲. حذف تمامی بلوک‌های تگ و فرمت‌بندی ASS داخل آکولاد {...}
            clean_text = re.sub(r"\{[^}]*\}", "", raw_text)

            # ۳. تبدیل کاراکترهای خاص ASS به متن عادی
            clean_text = clean_text.replace("\\N", "\n").replace("\\n", "\n").replace("\\h", " ")
            clean_text = html.unescape(clean_text)

            # ۴. پاکسازی خطوط خالی و فاصله‌های اضافی
            clean_lines = [ln.strip() for ln in clean_text.split("\n") if ln.strip()]
            if not clean_lines:
                continue

            final_text = "\n".join(clean_lines)
            parsed_events.append((start_ms, end_ms, start_srt, end_srt, final_text))

        # مرتب‌سازی دیالوگ‌ها بر اساس زمان شروع و حذف خطوط تکراری هم‌زمان
        parsed_events.sort(key=lambda x: (x[0], x[1]))
        deduped_blocks = []
        seen_keys = set()

        for start_ms, end_ms, start_srt, end_srt, txt in parsed_events:
            key = (start_srt, end_srt, txt)
            if key in seen_keys:
                continue
            seen_keys.add(key)
            idx = len(deduped_blocks) + 1
            deduped_blocks.append(f"{idx}\r\n{start_srt} --> {end_srt}\r\n{txt}")

        srt_content = "\r\n\r\n".join(deduped_blocks) + "\r\n"
        srt_out_path.parent.mkdir(parents=True, exist_ok=True)
        with open(srt_out_path, "wb") as f:
            f.write(srt_content.encode("utf-8-sig"))

        return srt_out_path

    @staticmethod
    def _find_archive_tool() -> tuple[str | None, str]:
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
        archive_path = Path(archive_path).resolve()
        extract_to = Path(extract_to).resolve()
        extract_to.mkdir(parents=True, exist_ok=True)

        temp_extract_dir = extract_to / f"_tmp_ext_{uuid.uuid4().hex[:6]}"
        temp_extract_dir.mkdir(parents=True, exist_ok=True)

        ext_low = archive_path.suffix.lower()
        extracted_ok = False
        last_err = ""

        try:
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

                if not extracted_ok:
                    tar_bin = shutil.which("tar") or r"C:\Windows\System32\tar.exe"
                    if tar_bin and Path(tar_bin).exists():
                        cmd = [str(tar_bin), "-xf", str(archive_path), "-C", str(temp_extract_dir)]
                        res = subprocess.run(cmd, capture_output=True, text=True)
                        if res.returncode == 0 and any(temp_extract_dir.rglob("*")):
                            extracted_ok = True
                        else:
                            last_err = res.stderr or res.stdout

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
                    f"({last_err[:150]})"
                )

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

                if target_path.suffix.lower() in cls.SUB_EXTENSIONS:
                    try:
                        cls.normalize_subtitle_file(target_path)
                    except Exception:
                        pass

                final_files.append(target_path.resolve())

            if remove_archive and archive_path.exists():
                archive_path.unlink(missing_ok=True)

            return final_files

        finally:
            if temp_extract_dir.exists():
                shutil.rmtree(temp_extract_dir, ignore_errors=True)

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
            return cls.normalize_subtitle_file(output_srt)

        def _replace_time(match: re.Match) -> str:
            h1, m1, s1, ms1, h2, m2, s2, ms2 = map(int, match.groups())
            new_start = cls._ms_to_srt_time(cls._time_to_ms(h1, m1, s1, ms1) + offset_ms)
            new_end = cls._ms_to_srt_time(cls._time_to_ms(h2, m2, s2, ms2) + offset_ms)
            return f"{new_start} --> {new_end}"

        shifted_content = cls.SRT_TIME_PATTERN.sub(_replace_time, content)
        output_srt.write_text(shifted_content, encoding="utf-8-sig")
        return cls.normalize_subtitle_file(output_srt)

    @classmethod
    async def auto_sync_subtitle(
        cls,
        video_path: Path,
        input_srt: Path,
        output_srt: Path,
        reference_srt: Path | None = None,
        progress_callback: Callable[[str], None] = None
    ) -> Path:
        cls.normalize_subtitle_file(input_srt)

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

        return cls.normalize_subtitle_file(output_srt)

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
        return cls.normalize_subtitle_file(output_srt)