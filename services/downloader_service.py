import asyncio
import json
import re
import sys
import time
from pathlib import Path
from typing import Callable, Optional

from core.config_loader import cfg
from core.proxy_manager import ProxyManager


class DownloaderService:
    VIDEO_EXTENSIONS = {
        ".mkv",
        ".mp4",
        ".avi",
        ".mov",
        ".ts",
        ".m4v",
        ".webm",
        ".wmv",
        ".flv",
    }

    SUBTITLE_EXTENSIONS = {
        ".srt",
        ".ass",
        ".ssa",
        ".vtt",
        ".sub",
    }

    # ---------------------------------------------------------
    # General helpers
    # ---------------------------------------------------------

    @staticmethod
    def _safe_name(name: str) -> str:
        """پاک‌سازی نام پوشه برای استفاده در ویندوز."""
        name = re.sub(r'[<>:"/\\|?*]', "", name)
        name = re.sub(r"\s+", "_", name.strip())
        return name[:150] or "Untitled"

    @staticmethod
    def _emit_progress(
        callback: Optional[Callable[[str], None]],
        message: str,
    ) -> None:
        """ارسال گزارش پیشرفت، بدون متوقف کردن عملیات در صورت خطای callback."""
        if callback:
            try:
                callback(message)
            except Exception:
                pass

    @staticmethod
    async def _terminate_process(
        proc: asyncio.subprocess.Process,
        timeout: int = 5,
    ) -> None:
        """
        خاتمه امن پردازش فرزند.

        ابتدا terminate و در صورت پاسخ ندادن، kill اجرا می‌شود.
        این تابع برای پشتیبانی از لغو عملیات توسط کاربر استفاده می‌شود.
        """
        if proc is None or proc.returncode is not None:
            return

        try:
            proc.terminate()
        except ProcessLookupError:
            return
        except Exception:
            pass

        try:
            await asyncio.wait_for(proc.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            except Exception:
                pass

            try:
                await proc.wait()
            except Exception:
                pass

    @classmethod
    async def _run_process(
        cls,
        cmd: list[str],
        progress_callback: Optional[Callable[[str], None]] = None,
        progress_filter: Optional[Callable[[str], bool]] = None,
        error_message: str = "خطا در اجرای پردازش",
    ) -> None:
        """
        اجرای یک پردازش خارجی با قابلیت لغو.

        خروجی stdout و stderr خوانده می‌شود تا بافر پردازش پر نشود.
        در صورت لغو coroutine، پردازش فرزند نیز متوقف خواهد شد.
        """
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )

        output_lines = []

        try:
            while True:
                line = await proc.stdout.readline()

                if not line:
                    break

                text = line.decode(
                    "utf-8",
                    errors="replace",
                ).strip()

                if not text:
                    continue

                if len(output_lines) < 30:
                    output_lines.append(text)

                if progress_callback:
                    if progress_filter is None or progress_filter(text):
                        cls._emit_progress(progress_callback, text)

            await proc.wait()

            if proc.returncode != 0:
                output = "\n".join(output_lines[-10:])
                raise RuntimeError(
                    f"{error_message} "
                    f"(Exit code: {proc.returncode})\n{output}"
                )

        except asyncio.CancelledError:
            await cls._terminate_process(proc)
            raise

        except Exception:
            if proc.returncode is None:
                await cls._terminate_process(proc)
            raise

    # ---------------------------------------------------------
    # Folder management
    # ---------------------------------------------------------

    @staticmethod
    def get_workspace_dir() -> Path:
        """دریافت مسیر workspace از تنظیمات."""
        workspace = cfg.get("paths", "workspace_dir")
        return Path(workspace)

    @classmethod
    def create_movie_folders(cls, movie_title: str) -> dict[str, Path]:
        """
        ساخت پوشه فیلم و زیرپوشه‌های موردنیاز.

        ساختار:

        workspace/
            Movie_Name/
                softsub/
                hardsub/
                sub/
        """
        workspace = cls.get_workspace_dir()
        workspace.mkdir(parents=True, exist_ok=True)

        safe_name = cls._safe_name(movie_title)
        base_dir = workspace / safe_name

        folders = {
            "base": base_dir,
            "softsub": base_dir / "softsub",
            "hardsub": base_dir / "hardsub",
            "sub": base_dir / "sub",
        }

        for folder in folders.values():
            folder.mkdir(parents=True, exist_ok=True)

        return folders

    @classmethod
    def get_existing_folders(cls) -> list[Path]:
        """دریافت پوشه‌های موجود در workspace."""
        workspace = cls.get_workspace_dir()
        workspace.mkdir(parents=True, exist_ok=True)

        return sorted(
            [
                path
                for path in workspace.iterdir()
                if path.is_dir()
            ],
            key=lambda path: path.name.lower(),
        )

    @classmethod
    def create_download_folder(cls, folder_name: str) -> Path:
        """ساخت پوشه جدید برای دانلود."""
        workspace = cls.get_workspace_dir()
        workspace.mkdir(parents=True, exist_ok=True)

        folder = workspace / cls._safe_name(folder_name)
        folder.mkdir(parents=True, exist_ok=True)

        return folder

    @classmethod
    def validate_destination(cls, dest_dir: Path) -> Path:
        """
        بررسی و آماده‌سازی مسیر مقصد.

        مسیر باید داخل workspace باشد تا دانلودها خارج از فضای
        مدیریت‌شده برنامه ذخیره نشوند.
        """
        workspace = cls.get_workspace_dir().resolve()
        destination = Path(dest_dir).resolve()

        try:
            destination.relative_to(workspace)
        except ValueError:
            raise ValueError(
                "مسیر مقصد باید داخل پوشه workspace باشد."
            )

        destination.mkdir(parents=True, exist_ok=True)

        return destination

    # ---------------------------------------------------------
    # File discovery
    # ---------------------------------------------------------

    @classmethod
    def find_video_files(cls, directory: Path) -> list[Path]:
        """پیدا کردن فایل‌های ویدئویی موجود در پوشه و زیرپوشه‌ها."""
        directory = Path(directory)

        if not directory.exists() or not directory.is_dir():
            return []

        videos = []

        for file_path in directory.rglob("*"):
            try:
                if (
                    file_path.is_file()
                    and file_path.suffix.lower() in cls.VIDEO_EXTENSIONS
                    and not file_path.name.startswith("_temp_")
                ):
                    videos.append(file_path)
            except OSError:
                continue

        return sorted(
            videos,
            key=lambda path: path.stat().st_size,
            reverse=True,
        )

    @classmethod
    def find_subtitle_files(
        cls,
        directory: Path,
        video_path: Optional[Path] = None,
        language: Optional[str] = None,
    ) -> list[Path]:
        """
        پیدا کردن فایل‌های زیرنویس در پوشه و زیرپوشه‌ها.

        language:
            fa = فارسی
            en = انگلیسی
            None = همه زیرنویس‌ها

        اولویت‌بندی:
            1. زیرنویسی که نام آن با نام ویدئو مطابقت دارد.
            2. زیرنویسی که نام زبان در آن مشخص شده است.
            3. سایر فایل‌های زیرنویس.
        """
        directory = Path(directory)

        if not directory.exists() or not directory.is_dir():
            return []

        video_stem = (
            video_path.stem.lower()
            if video_path
            else None
        )

        fa_tokens = {
            "fa",
            "per",
            "persian",
            "farsi",
            "فارسی",
            "iranian",
        }

        en_tokens = {
            "en",
            "eng",
            "english",
        }

        candidates = []

        for file_path in directory.rglob("*"):
            try:
                if (
                    not file_path.is_file()
                    or file_path.suffix.lower() not in cls.SUBTITLE_EXTENSIONS
                ):
                    continue

                stem = file_path.stem.lower()

                # تبدیل جداکننده‌های نام فایل به فاصله
                tokens = set(
                    re.split(r"[\s._\-()\[\]{}]+", stem)
                )

                # تشخیص زبان بر اساس کلمات کامل
                is_fa = bool(tokens.intersection(fa_tokens))
                is_en = bool(tokens.intersection(en_tokens))

                if language == "fa" and is_en and not is_fa:
                    continue

                if language == "en" and is_fa and not is_en:
                    continue

                score = 0

                if video_stem:
                    if stem == video_stem:
                        score += 100

                    elif stem.startswith(video_stem):
                        score += 70

                    elif video_stem in stem:
                        score += 40

                if language == "fa" and is_fa:
                    score += 50

                elif language == "en" and is_en:
                    score += 50

                candidates.append((score, file_path))

            except OSError:
                continue

        candidates.sort(
            key=lambda item: (
                -item[0],
                item[1].name.lower(),
            )
        )

        return [
            file_path
            for _, file_path in candidates
        ]

    # ---------------------------------------------------------
    # Target detection
    # ---------------------------------------------------------

    @staticmethod
    def is_torrent_target(target: str) -> bool:
        """تشخیص لینک مگنت، فایل تورنت یا لینک torrent."""
        target = str(target).strip()
        target_lower = target.lower()

        if target_lower.startswith("magnet:?"):
            return True

        if target_lower.endswith(".torrent"):
            return True

        try:
            local_path = Path(target)

            if (
                local_path.is_file()
                and local_path.suffix.lower() == ".torrent"
            ):
                return True

        except (OSError, ValueError):
            pass

        return False

    # ---------------------------------------------------------
    # Main download dispatcher
    # ---------------------------------------------------------

    @classmethod
    async def download(
        cls,
        url_or_magnet: str,
        dest_dir: Path,
        progress_callback: Optional[Callable[[str], None]] = None,
    ) -> Path:
        """
        تشخیص نوع لینک و انتخاب دانلودر مناسب.

        پشتیبانی از:
            - HTTP / HTTPS
            - Magnet
            - Torrent
            - Google Drive
            - Mega.nz

        در صورت لغو task، پردازش دانلود نیز متوقف می‌شود.
        """
        if not url_or_magnet or not str(url_or_magnet).strip():
            raise ValueError("لینک دانلود خالی است.")

        dest_dir = cls.validate_destination(Path(dest_dir))
        target = str(url_or_magnet).strip()
        target_lower = target.lower()

        cls._emit_progress(
            progress_callback,
            f"مسیر ذخیره‌سازی: {dest_dir}",
        )

        if cls.is_torrent_target(target):
            qbt_bin = cfg.get("paths", "qbt_bin")

            if qbt_bin and Path(qbt_bin).exists():
                return await cls._download_qbt(
                    target,
                    dest_dir,
                    progress_callback,
                )

            return await cls._download_aria2(
                target,
                dest_dir,
                progress_callback,
            )

        if "drive.google.com" in target_lower:
            return await cls._download_gdrive(
                target,
                dest_dir,
                progress_callback,
            )

        if "mega.nz" in target_lower:
            return await cls._download_mega(
                target,
                dest_dir,
                progress_callback,
            )

        return await cls._download_aria2(
            target,
            dest_dir,
            progress_callback,
        )

    # ---------------------------------------------------------
    # qBittorrent
    # ---------------------------------------------------------

    @staticmethod
    def _qbt_auth_args() -> list[str]:
        """ساخت پارامترهای اتصال به qBittorrent WebUI."""
        qbt_cfg = cfg.get("qbittorrent") or {}

        args = []

        if qbt_cfg.get("webui_url"):
            args.extend([
                "--url",
                str(qbt_cfg["webui_url"]),
            ])

        if qbt_cfg.get("username"):
            args.extend([
                "--username",
                str(qbt_cfg["username"]),
            ])

        if qbt_cfg.get("password"):
            args.extend([
                "--password",
                str(qbt_cfg["password"]),
            ])

        return args

    @classmethod
    async def _qbt_list_torrents(
        cls,
        qbt_bin: str,
        auth_args: list[str],
    ) -> list[dict]:
        """دریافت لیست تورنت‌های qBittorrent."""
        cmd = [
            qbt_bin,
            "torrent",
            "list",
            "--format",
            "json",
        ] + auth_args

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        try:
            stdout, _ = await proc.communicate()

            if proc.returncode != 0:
                return []

            data = json.loads(
                stdout.decode("utf-8", errors="replace")
            )

            return data if isinstance(data, list) else []

        except asyncio.CancelledError:
            await cls._terminate_process(proc)
            raise

        except Exception:
            if proc.returncode is None:
                await cls._terminate_process(proc)

            return []

    @classmethod
    async def _qbt_delete_torrent(
        cls,
        qbt_bin: str,
        auth_args: list[str],
        torrent_hash: str,
    ) -> None:
        """
        حذف تورنت از qBittorrent بدون حذف فایل‌های دانلودشده.

        این تابع فقط ورودی تورنت را از کلاینت حذف می‌کند.
        """
        if not torrent_hash:
            return

        cmd = [
            qbt_bin,
            "torrent",
            "delete",
            torrent_hash,
        ] + auth_args

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        try:
            await proc.communicate()
        except asyncio.CancelledError:
            await cls._terminate_process(proc)
            raise

    @classmethod
    async def _download_qbt(
        cls,
        target: str,
        dest_dir: Path,
        progress_callback: Optional[Callable[[str], None]] = None,
    ) -> Path:
        """
        دانلود تورنت با استفاده از qBittorrent CLI.

        برای جلوگیری از تداخل دانلودهای هم‌زمان، تورنت بر اساس
        save_path شناسایی می‌شود؛ نه صرفاً جدیدترین تورنت.
        """
        qbt_bin = cfg.get("paths", "qbt_bin")
        auth_args = cls._qbt_auth_args()

        dest_resolved = str(dest_dir.resolve())
        normalized_dest = dest_resolved.rstrip("\\/").lower()

        try:
            local_file = Path(target).is_file()
        except (OSError, ValueError):
            local_file = False

        if local_file:
            add_cmd = [
                qbt_bin,
                "torrent",
                "add",
                "file",
                str(Path(target).resolve()),
                "--folder",
                dest_resolved,
            ] + auth_args
        else:
            add_cmd = [
                qbt_bin,
                "torrent",
                "add",
                "url",
                target,
                "--folder",
                dest_resolved,
            ] + auth_args

        proc = await asyncio.create_subprocess_exec(
            *add_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )

        try:
            stdout, _ = await proc.communicate()

        except asyncio.CancelledError:
            await cls._terminate_process(proc)
            raise

        if proc.returncode != 0:
            raise RuntimeError(
                "خطا در اضافه کردن تورنت به qBittorrent:\n"
                + stdout.decode("utf-8", errors="replace")
            )

        cls._emit_progress(
            progress_callback,
            "تورنت به qBittorrent اضافه شد؛ در انتظار دریافت متادیتا...",
        )

        completed_states = {
            "uploading",
            "stalledup",
            "pausedup",
            "queuedup",
            "checkingup",
            "forcedup",
            "completed",
        }

        matched_hash = None
        start_ts = time.time()

        try:
            while True:
                await asyncio.sleep(3)

                torrents = await cls._qbt_list_torrents(
                    qbt_bin,
                    auth_args,
                )

                target_torrent = None

                # ابتدا تورنتی را پیدا می‌کنیم که hash آن قبلاً ثبت شده است.
                if matched_hash:
                    for torrent in torrents:
                        if (
                            str(torrent.get("hash", "")).lower()
                            == str(matched_hash).lower()
                        ):
                            target_torrent = torrent
                            break

                # در غیر این صورت بر اساس مسیر ذخیره‌سازی جست‌وجو می‌کنیم.
                if target_torrent is None:
                    matching = []

                    for torrent in torrents:
                        save_path = str(
                            torrent.get("save_path", "")
                        ).rstrip("\\/").lower()

                        if save_path == normalized_dest:
                            matching.append(torrent)

                    if matching:
                        # در صورت وجود چند نتیجه، جدیدترین مورد انتخاب می‌شود.
                        target_torrent = max(
                            matching,
                            key=lambda item: item.get("added_on", 0),
                        )

                        matched_hash = target_torrent.get("hash")

                # در 30 ثانیه اول، در صورت نبود save_path،
                # فقط برای سازگاری با نسخه‌های مختلف qbt تلاش محدود انجام می‌شود.
                if (
                    target_torrent is None
                    and not matched_hash
                    and time.time() - start_ts < 30
                ):
                    matching = [
                        torrent
                        for torrent in torrents
                        if torrent.get("added_on", 0) >= start_ts - 5
                    ]

                    if len(matching) == 1:
                        target_torrent = matching[0]
                        matched_hash = target_torrent.get("hash")

                if target_torrent is None:
                    cls._emit_progress(
                        progress_callback,
                        "در انتظار شناسایی تورنت در qBittorrent...",
                    )
                    continue

                progress = (
                    float(target_torrent.get("progress", 0.0))
                    * 100.0
                )

                state = str(
                    target_torrent.get("state", "")
                ).lower()

                speed_mb = (
                    float(target_torrent.get("dlspeed", 0))
                    / (1024 * 1024)
                )

                name = str(
                    target_torrent.get("name", "Torrent")
                )

                cls._emit_progress(
                    progress_callback,
                    (
                        f"{progress:.1f}% | "
                        f"سرعت: {speed_mb:.2f} MB/s | "
                        f"وضعیت: {state} | "
                        f"{name[:40]}"
                    ),
                )

                if progress >= 100.0 or state in completed_states:
                    if matched_hash:
                        await cls._qbt_delete_torrent(
                            qbt_bin,
                            auth_args,
                            matched_hash,
                        )

                    cls._emit_progress(
                        progress_callback,
                        "دانلود تورنت با موفقیت به پایان رسید.",
                    )

                    return dest_dir

        except asyncio.CancelledError:
            # هنگام لغو، تلاش می‌کنیم تورنت مربوط به همین مسیر را پیدا کنیم.
            try:
                if not matched_hash:
                    torrents = await cls._qbt_list_torrents(
                        qbt_bin,
                        auth_args,
                    )

                    matching = [
                        torrent
                        for torrent in torrents
                        if str(
                            torrent.get("save_path", "")
                        ).rstrip("\\/").lower() == normalized_dest
                    ]

                    if len(matching) == 1:
                        matched_hash = matching[0].get("hash")

                if matched_hash:
                    await cls._qbt_delete_torrent(
                        qbt_bin,
                        auth_args,
                        matched_hash,
                    )

            except Exception:
                pass

            raise

    # ---------------------------------------------------------
    # Aria2
    # ---------------------------------------------------------

    @classmethod
    async def _download_aria2(
        cls,
        target: str,
        dest_dir: Path,
        progress_callback: Optional[Callable[[str], None]] = None,
    ) -> Path:
        """دانلود لینک مستقیم یا تورنت با aria2c."""
        aria2_bin = cfg.get("paths", "aria2c_bin")

        if not aria2_bin:
            raise RuntimeError("مسیر aria2c در config.yaml تنظیم نشده است.")

        cmd = [
            str(aria2_bin),
            f"--dir={dest_dir}",
            "--seed-time=0",
            "--max-connection-per-server=16",
            "--split=16",
            "--continue=true",
            "--summary-interval=2",
            "--console-log-level=notice",
        ]

        proxy_url = ProxyManager.get_proxy_url()

        if proxy_url and proxy_url.startswith(("http://", "https://")):
            cmd.append(f"--all-proxy={proxy_url}")

        cmd.append(target)

        cls._emit_progress(
            progress_callback,
            "دانلود با aria2 شروع شد.",
        )

        await cls._run_process(
            cmd,
            progress_callback=progress_callback,
            progress_filter=lambda line: (
                "[#" in line
                or "Download complete" in line
                or "ERROR" in line.upper()
            ),
            error_message="دانلود با aria2 ناموفق بود",
        )

        cls._emit_progress(
            progress_callback,
            "دانلود با aria2 به پایان رسید.",
        )

        return dest_dir

    # ---------------------------------------------------------
    # Google Drive
    # ---------------------------------------------------------

    @classmethod
    async def _download_gdrive(
        cls,
        gdrive_url: str,
        dest_dir: Path,
        progress_callback: Optional[Callable[[str], None]] = None,
    ) -> Path:
        """
        دانلود از Google Drive با gdown به‌صورت subprocess.

        استفاده از subprocess به‌جای asyncio.to_thread باعث می‌شود
        در زمان لغو task بتوان پردازش دانلود را نیز متوقف کرد.
        """
        cls._emit_progress(
            progress_callback,
            "در حال دانلود از Google Drive...",
        )

        proxy_url = ProxyManager.get_proxy_url()

        cmd = [
            sys.executable,
            "-m",
            "gdown",
        ]

        if "/folders/" in gdrive_url.lower():
            cmd.extend([
                "--folder",
                gdrive_url,
                "--output",
                str(dest_dir),
            ])
        else:
            cmd.extend([
                gdrive_url,
                "--output",
                str(dest_dir),
                "--fuzzy",
            ])

        if proxy_url:
            cmd.extend([
                "--proxy",
                proxy_url,
            ])

        await cls._run_process(
            cmd,
            progress_callback=progress_callback,
            error_message="دانلود از Google Drive ناموفق بود",
        )

        cls._emit_progress(
            progress_callback,
            "دانلود Google Drive به پایان رسید.",
        )

        return dest_dir

    # ---------------------------------------------------------
    # Mega
    # ---------------------------------------------------------

    @classmethod
    async def _download_mega(
        cls,
        mega_url: str,
        dest_dir: Path,
        progress_callback: Optional[Callable[[str], None]] = None,
    ) -> Path:
        """دانلود از Mega.nz با megadl یا megatools."""
        megadl_bin = cfg.get("paths", "megadl_bin")

        if not megadl_bin:
            raise RuntimeError("مسیر megadl در config.yaml تنظیم نشده است.")

        if "megatools" in str(megadl_bin).lower():
            cmd = [
                str(megadl_bin),
                "dl",
                "--path",
                str(dest_dir),
            ]
        else:
            cmd = [
                str(megadl_bin),
                "--path",
                str(dest_dir),
            ]

        proxy_url = ProxyManager.get_proxy_url()

        if proxy_url:
            cmd.extend([
                "--proxy",
                proxy_url,
            ])

        cmd.append(mega_url)

        cls._emit_progress(
            progress_callback,
            "دانلود از Mega.nz شروع شد.",
        )

        await cls._run_process(
            cmd,
            progress_callback=progress_callback,
            progress_filter=lambda line: (
                "%" in line
                or "error" in line.lower()
                or "download" in line.lower()
            ),
            error_message="دانلود از Mega.nz ناموفق بود",
        )

        cls._emit_progress(
            progress_callback,
            "دانلود Mega.nz به پایان رسید.",
        )

        return dest_dir