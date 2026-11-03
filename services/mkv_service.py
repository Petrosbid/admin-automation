import asyncio
import json

from pathlib import Path
from typing import Callable

from core.config_loader import cfg

from services.subtitle_service import SubtitleService

from services.process_utils import (
    communicate_cancellable,
    terminate_process
)


class MKVService:

    # ==================================================
    # TRACK INFORMATION
    # ==================================================

    @staticmethod
    async def get_all_tracks(
        video_path: Path
    ) -> list[dict]:

        mkvmerge = cfg.get(
            "paths",
            "mkvmerge_bin"
        )

        proc = await asyncio.create_subprocess_exec(
            mkvmerge,
            "-J",
            str(video_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )

        stdout, stderr = await communicate_cancellable(
            proc
        )

        if proc.returncode != 0:

            raise RuntimeError(
                stderr.decode(
                    "utf-8",
                    errors="replace"
                )
            )

        data = json.loads(
            stdout.decode(
                "utf-8",
                errors="replace"
            )
        )

        tracks_info = []

        for track in data.get("tracks", []):

            props = track.get(
                "properties",
                {}
            )

            tracks_info.append({
                "id": track["id"],
                "type": track.get(
                    "type",
                    "unknown"
                ),
                "codec": track.get(
                    "codec",
                    "Unknown"
                ),
                "language": str(
                    props.get(
                        "language",
                        "und"
                    )
                ).lower(),
                "language_ietf": str(
                    props.get(
                        "language_ietf",
                        ""
                    )
                ).lower(),
                "track_name": props.get(
                    "track_name",
                    "No Name"
                )
            })

        return tracks_info

    @classmethod
    async def list_subtitle_tracks(
        cls,
        video_path: Path
    ) -> list[dict]:

        all_tracks = await cls.get_all_tracks(
            video_path
        )

        return [
            track
            for track in all_tracks
            if track["type"] == "subtitles"
        ]

    # ==================================================
    # LANGUAGE DETECTION
    # ==================================================

    @staticmethod
    def _is_english_track(
        track: dict
    ) -> bool:

        lang = track.get(
            "language",
            ""
        ).lower()

        ietf = track.get(
            "language_ietf",
            ""
        ).lower()

        track_name = str(
            track.get(
                "track_name",
                ""
            )
        ).lower()

        if lang in ("eng", "en"):
            return True

        if ietf.startswith("en"):
            return True

        if "english" in track_name:
            return True

        return False

    # ==================================================
    # EXTRACT SINGLE SUBTITLE
    # ==================================================

    @staticmethod
    async def extract_subtitle_track(
        video_path: Path,
        track_id: int,
        output_srt: Path,
        progress_callback: Callable[[str], None] = None
    ) -> Path:

        mkvextract = cfg.get(
            "paths",
            "mkvextract_bin"
        )

        output_srt.parent.mkdir(
            parents=True,
            exist_ok=True
        )

        cmd = [
            mkvextract,
            "tracks",
            str(video_path),
            f"{track_id}:{output_srt}"
        ]

        if progress_callback:

            progress_callback(
                f"در حال استخراج زیرنویس Track #{track_id}..."
            )

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
                "MKVExtract Error:\n"
                + stdout.decode(
                    "utf-8",
                    errors="replace"
                )
            )

        return output_srt

    # ==================================================
    # EXTRACT ALL SUBTITLES
    # ==================================================

    @classmethod
    async def extract_all_subtitles(
        cls,
        video_path: Path,
        output_dir: Path,
        progress_callback: Callable[[str], None] = None
    ) -> list[Path]:

        sub_tracks = await cls.list_subtitle_tracks(
            video_path
        )

        if not sub_tracks:
            return []

        output_dir.mkdir(
            parents=True,
            exist_ok=True
        )

        extracted_files = []

        for track in sub_tracks:

            track_id = track["id"]

            language = track["language"]

            codec = track["codec"].lower()

            if "ass" in codec or "ssa" in codec:

                extension = ".ass"

            elif "pgs" in codec:

                extension = ".sup"

            else:

                extension = ".srt"

            output_file = output_dir / (
                f"{video_path.stem}."
                f"Track{track_id}."
                f"{language}"
                f"{extension}"
            )

            await cls.extract_subtitle_track(
                video_path,
                track_id,
                output_file,
                progress_callback
            )

            if output_file.exists():

                extracted_files.append(
                    output_file
                )

        return extracted_files

    # ==================================================
    # AUTO SYNC
    # ==================================================

    @classmethod
    async def sync_sub_against_video(
        cls,
        video_path: Path,
        sub_to_sync: Path,
        en_sub_reference: Path | None = None,
        progress_callback: Callable[[str], None] = None
    ) -> Path:

        temp_ref_sub = None

        try:

            reference_path = en_sub_reference

            if (
                not reference_path
                or not reference_path.exists()
            ):

                sub_tracks = await cls.list_subtitle_tracks(
                    video_path
                )

                text_subs = [
                    track
                    for track in sub_tracks
                    if any(
                        codec in track["codec"].lower()
                        for codec in (
                            "subrip",
                            "srt",
                            "substationalpha",
                            "ass",
                            "utf8"
                        )
                    )
                ]

                if text_subs:

                    english_tracks = [
                        track
                        for track in text_subs
                        if cls._is_english_track(track)
                    ]

                    chosen_ref = (
                        english_tracks[0]
                        if english_tracks
                        else text_subs[0]
                    )

                    temp_ref_sub = sub_to_sync.with_name(
                        f"_temp_ref_{video_path.stem}_{chosen_ref['id']}.srt"
                    )

                    await cls.extract_subtitle_track(
                        video_path,
                        chosen_ref["id"],
                        temp_ref_sub,
                        progress_callback
                    )

                    reference_path = temp_ref_sub

            await SubtitleService.auto_sync_subtitle(
                video_path=video_path,
                input_srt=sub_to_sync,
                output_srt=sub_to_sync,
                reference_srt=reference_path,
                progress_callback=progress_callback
            )

            return sub_to_sync

        finally:

            if temp_ref_sub and temp_ref_sub.exists():

                temp_ref_sub.unlink(
                    missing_ok=True
                )

    # ==================================================
    # SOFTSUB MERGE
    # ==================================================

    @classmethod
    async def merge_softsub(
        cls,
        video_path: Path,
        output_path: Path,
        fa_sub_path: Path | None = None,
        en_sub_path: Path | None = None,
        keep_old_subs: bool = True,
        custom_brand: str | None = None,
        sync_mode: str = "none",
        sync_offset_ms: int = 0,
        progress_callback: Callable[[str], None] = None
    ) -> Path:

        mkvmerge = cfg.get(
            "paths",
            "mkvmerge_bin"
        )

        brand = (
            custom_brand
            or cfg.get(
                "branding",
                "global_title_and_track_name",
                ".::EmpireBestTv.Com::."
            )
        )

        fa_lang = cfg.get(
            "branding",
            "sub_fa_language",
            "per"
        )

        en_lang = cfg.get(
            "branding",
            "sub_en_language",
            "eng"
        )

        output_path.parent.mkdir(
            parents=True,
            exist_ok=True
        )

        working_fa_sub = None

        proc = None

        try:

            # ==========================================
            # 0. PREPARE PERSIAN SUBTITLE
            # ==========================================

            if fa_sub_path and Path(fa_sub_path).exists():

                if fa_sub_path.suffix.lower() == ".srt":

                    working_fa_sub = fa_sub_path.with_name(
                        f"_soft_clean_{fa_sub_path.name}"
                    )

                    working_fa_sub.write_text(
                        SubtitleService.read_subtitle_safe(
                            fa_sub_path
                        ),
                        encoding="utf-8-sig"
                    )

                else:

                    working_fa_sub = fa_sub_path

                # سینک فارسی
                if (
                    sync_mode == "auto"
                    or cfg.get(
                        "subtitle_sync",
                        "always_auto_sync",
                        False
                    )
                ):

                    await cls.sync_sub_against_video(
                        video_path,
                        working_fa_sub,
                        en_sub_path,
                        progress_callback
                    )

                elif (
                    sync_mode == "manual"
                    and sync_offset_ms != 0
                    and working_fa_sub.suffix.lower() == ".srt"
                ):

                    SubtitleService.shift_srt_time(
                        working_fa_sub,
                        working_fa_sub,
                        sync_offset_ms
                    )

            # ==========================================
            # 1. GET ORIGINAL TRACKS
            # ==========================================

            existing_tracks = await cls.get_all_tracks(
                video_path
            )

            # ==========================================
            # 2. SELECT INTERNAL ENGLISH SUBTITLE
            # ==========================================

            kept_internal_eng_track_id = None

            has_external_en = bool(
                en_sub_path
                and Path(en_sub_path).exists()
            )

            if keep_old_subs and not has_external_en:

                for track in existing_tracks:

                    if (
                        track["type"] == "subtitles"
                        and cls._is_english_track(track)
                    ):

                        kept_internal_eng_track_id = track["id"]

                        break

            # ==========================================
            # 3. BUILD MKVMERGE COMMAND
            # ==========================================

            cmd = [
                mkvmerge,
                "--gui-mode",
                "-o",
                str(output_path),
                "--title",
                brand
            ]

            if kept_internal_eng_track_id is not None:

                cmd.extend([
                    "--subtitle-tracks",
                    str(kept_internal_eng_track_id)
                ])

            else:

                cmd.append(
                    "--no-subtitles"
                )

            # ==========================================
            # 4. TRACK NAMES
            # ==========================================

            for track in existing_tracks:

                track_id = track["id"]

                if track["type"] == "subtitles":

                    if track_id == kept_internal_eng_track_id:

                        cmd.extend([
                            "--track-name",
                            f"{track_id}:{brand}",
                            "--default-track-flag",
                            f"{track_id}:no",
                            "--forced-display-flag",
                            f"{track_id}:no"
                        ])

                else:

                    cmd.extend([
                        "--track-name",
                        f"{track_id}:{brand}"
                    ])

            # ==========================================
            # 5. INPUT VIDEO
            # ==========================================

            cmd.append(
                str(video_path)
            )

            next_file_id = 1

            fa_file_id = None

            en_file_id = None

            # ==========================================
            # 6. ADD PERSIAN SUBTITLE
            # ==========================================

            if (
                working_fa_sub
                and working_fa_sub.exists()
            ):

                fa_file_id = next_file_id

                next_file_id += 1

                cmd.extend([
                    "--language",
                    f"0:{fa_lang}",
                    "--track-name",
                    f"0:{brand}",
                    "--default-track-flag",
                    "0:yes",
                    "--forced-display-flag",
                    "0:yes",
                    "--sub-charset",
                    "0:UTF-8"
                ])

                if (
                    sync_mode == "manual"
                    and sync_offset_ms != 0
                    and working_fa_sub.suffix.lower() != ".srt"
                ):

                    cmd.extend([
                        "--sync",
                        f"0:{sync_offset_ms}"
                    ])

                cmd.append(
                    str(working_fa_sub)
                )

            # ==========================================
            # 7. ADD ENGLISH SUBTITLE
            # ==========================================

            if has_external_en:

                en_file_id = next_file_id

                next_file_id += 1

                cmd.extend([
                    "--language",
                    f"0:{en_lang}",
                    "--track-name",
                    f"0:{brand}",
                    "--default-track-flag",
                    "0:no",
                    "--forced-display-flag",
                    "0:no",
                    "--sub-charset",
                    "0:UTF-8",
                    str(en_sub_path)
                ])

            # ==========================================
            # 8. TRACK ORDER
            # ==========================================

            track_order = [
                f"0:{track['id']}"
                for track in existing_tracks
                if track["type"] in (
                    "video",
                    "audio"
                )
            ]

            if fa_file_id is not None:

                track_order.append(
                    f"{fa_file_id}:0"
                )

            if en_file_id is not None:

                track_order.append(
                    f"{en_file_id}:0"
                )

            elif kept_internal_eng_track_id is not None:

                track_order.append(
                    f"0:{kept_internal_eng_track_id}"
                )

            if track_order:

                cmd.extend([
                    "--track-order",
                    ",".join(track_order)
                ])

            # ==========================================
            # 9. RUN MKVMERGE
            # ==========================================

            if progress_callback:

                progress_callback(
                    "شروع مرج با MKVToolNix..."
                )

            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT
            )

            while True:

                line = await proc.stdout.readline()

                if not line:
                    break

                decoded = line.decode(
                    "utf-8",
                    errors="replace"
                ).strip()

                if (
                    "#GUI#progress" in decoded
                    and progress_callback
                ):

                    progress_callback(
                        decoded.replace(
                            "#GUI#progress",
                            ""
                        ).strip()
                    )

                elif (
                    "Progress:" in decoded
                    and progress_callback
                ):

                    progress_callback(
                        decoded.replace(
                            "Progress:",
                            ""
                        ).strip()
                    )

            await proc.wait()

            if proc.returncode not in (0, 1):

                raise RuntimeError(
                    f"MKVToolNix Merge Failed "
                    f"(Exit Code {proc.returncode})"
                )

            proc = None

            if progress_callback:

                progress_callback(
                    "مرج SoftSub با موفقیت تکمیل شد."
                )

            return output_path

        except asyncio.CancelledError:

            if proc:

                await terminate_process(
                    proc
                )

            output_path.unlink(
                missing_ok=True
            )

            raise

        finally:

            if proc:

                await terminate_process(
                    proc
                )

            if (
                working_fa_sub
                and working_fa_sub != fa_sub_path
                and working_fa_sub.exists()
            ):

                working_fa_sub.unlink(
                    missing_ok=True
                )