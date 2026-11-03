import asyncio
import json
import sys

from pathlib import Path
from typing import Callable

from services.process_utils import terminate_process


class CuteFTPService:

    @classmethod
    async def upload_path(
        cls,
        local_target: Path,
        remote_subpath: str = "",
        progress_callback: Callable[[str], None] = None
    ) -> list[str]:

        local_target = Path(
            local_target
        ).resolve()

        if not local_target.exists():

            raise FileNotFoundError(
                str(local_target)
            )

        if progress_callback:

            progress_callback(
                f"ارسال {local_target.name} به موتور CuteFTP 9..."
            )

        cmd = [
            sys.executable,
            "-m",
            "services.cuteftp_worker",
            str(local_target),
            remote_subpath
        ]

        project_root = Path(
            __file__
        ).resolve().parents[1]

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=str(project_root)
        )

        output_lines = []

        generated_urls = None

        try:

            while True:

                line = await proc.stdout.readline()

                if not line:
                    break

                text = line.decode(
                    "utf-8",
                    errors="replace"
                ).strip()

                if not text:
                    continue

                output_lines.append(text)

                if text.startswith("__RESULT__"):

                    result_json = text[len("__RESULT__"):]

                    generated_urls = json.loads(
                        result_json
                    )

                elif text.startswith("__ERROR__"):

                    if progress_callback:

                        progress_callback(
                            text
                        )

                else:

                    if progress_callback:

                        progress_callback(
                            text[:250]
                        )

            await proc.wait()

            if proc.returncode != 0:

                raise RuntimeError(
                    "CuteFTP upload failed:\n"
                    + "\n".join(output_lines[-15:])
                )

            if generated_urls is None:

                raise RuntimeError(
                    "CuteFTP completed without returning download URLs."
                )

            if progress_callback:

                progress_callback(
                    "آپلود با CuteFTP با موفقیت انجام شد."
                )

            return generated_urls

        except asyncio.CancelledError:

            await terminate_process(
                proc
            )

            raise

        finally:

            if proc.returncode is None:

                await terminate_process(
                    proc
                )