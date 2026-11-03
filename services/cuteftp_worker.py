import json
import sys
import traceback

from pathlib import Path
from urllib.parse import quote

import pythoncom
import win32com.client

from core.config_loader import cfg


def upload(
    local_target: Path,
    remote_subpath: str = ""
) -> list[str]:

    pythoncom.CoInitialize()

    te = None

    try:

        ftp_cfg = cfg.get(
            "cuteftp"
        )

        te = win32com.client.Dispatch(
            "CuteFTPPro.TEConnection"
        )

        te.Option(
            "ThrowError",
            False
        )

        te.Protocol = ftp_cfg.get(
            "protocol",
            "FTP"
        )

        te.Host = ftp_cfg["host"]

        te.Port = int(
            ftp_cfg.get("port", 21)
        )

        te.Login = ftp_cfg["username"]

        te.Password = ftp_cfg["password"]

        te.Connect()

        base_remote = ftp_cfg[
            "remote_base_dir"
        ].rstrip("/")

        target_remote_dir = (
            f"{base_remote}/{remote_subpath}"
        ).rstrip("/")

        base_url = ftp_cfg[
            "base_download_url"
        ].rstrip("/")

        generated_urls = []

        if local_target.is_file():

            te.RemoteFolder = target_remote_dir

            te.Upload(
                str(local_target)
            )

            rel_url = (
                f"{remote_subpath}/"
                f"{local_target.name}"
            ).lstrip("/")

            generated_urls.append(
                f"{base_url}/{quote(rel_url)}"
            )

        else:

            folder_name = local_target.name

            movie_remote_dir = (
                f"{target_remote_dir}/{folder_name}"
            )

            if not te.RemoteExists(movie_remote_dir):

                te.CreateRemoteFolder(
                    movie_remote_dir
                )

            te.RemoteFolder = movie_remote_dir

            te.Upload(
                f"{local_target}\\*.*"
            )

            for file_path in local_target.rglob("*"):

                if file_path.is_file():

                    rel = file_path.relative_to(
                        local_target.parent
                    ).as_posix()

                    generated_urls.append(
                        f"{base_url}/{quote(rel)}"
                    )

        te.Disconnect()

        return generated_urls

    finally:

        if te:

            try:

                te.Disconnect()

            except Exception:

                pass

        pythoncom.CoUninitialize()


def main():

    try:

        local_target = Path(
            sys.argv[1]
        )

        remote_subpath = (
            sys.argv[2]
            if len(sys.argv) > 2
            else ""
        )

        if not local_target.exists():

            raise FileNotFoundError(
                str(local_target)
            )

        urls = upload(
            local_target,
            remote_subpath
        )

        print(
            "__RESULT__" + json.dumps(
                urls,
                ensure_ascii=False
            ),
            flush=True
        )

    except Exception as exc:

        print(
            "__ERROR__" + str(exc),
            file=sys.stderr,
            flush=True
        )

        traceback.print_exc()

        sys.exit(1)


if __name__ == "__main__":

    main()