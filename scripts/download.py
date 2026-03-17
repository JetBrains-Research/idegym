#!/usr/bin/env -S uv run --script --quiet
# /// script
# requires-python = ">=3.12"
# dependencies = [
#   "requests",
#   "tqdm",
# ]
# ///
from argparse import ArgumentParser, ArgumentTypeError, Namespace
from sys import stderr, stdout
from typing import Optional
from urllib.parse import urlparse

from requests import get as fetch
from requests.exceptions import HTTPError, RequestException
from tqdm import tqdm


def http_url(value: str) -> str:
    scheme, netloc, path, *_ = urlparse(value)
    match scheme:
        case "http" | "https" if bool(netloc):
            return value
        case _:
            raise ArgumentTypeError(f"Invalid URL: {scheme}")


def download(
    url: str,
    filename: str,
    accept: Optional[str] = None,
    auth_token: Optional[str] = None,
    auth_type: Optional[str] = "Bearer",
):
    headers = {
        "Accept": accept,
        "Authorization": f"{auth_type} {auth_token}" if auth_token is not None else None,
    }
    response = fetch(
        url=url,
        headers=headers,
        stream=True,
    )
    response.raise_for_status()
    size = int(response.headers.get("content-length", 0))
    with (
        tqdm(
            file=stdout,
            total=size,
            desc="Downloading",
            bar_format="{desc}: {n_fmt}/{total_fmt} [{elapsed}<{remaining}, {rate_fmt}]",
            unit="iB",
            unit_scale=True,
            unit_divisor=1024,
        ) as progress,
        open(filename, "wb") as file,
    ):
        for chunk in response.iter_content(chunk_size=1024):
            if chunk:  # Filter out keep-alive new chunks
                progress.update(len(chunk))
                file.write(chunk)


def main(args: Namespace):
    try:
        download(
            url=args.url,
            filename=args.name,
            accept=args.accept,
            auth_token=args.auth_token,
            auth_type=args.auth_type,
        )
    except (HTTPError, RequestException) as ex:
        print(f"{ex.response.status_code} {ex.response.reason} for: {args.url}", file=stderr)
        exit(1)
    except Exception as ex:
        print(ex, file=stderr)
        exit(1)


if __name__ == "__main__":
    parser = ArgumentParser(
        prog="download",
        description="Downloads a file from a remote source",
        epilog="To specify the value for a flag, you can either use --flag=[VALUE] or --flag [VALUE]",
    )
    parser.add_argument(
        "url",
        help="HTTP(S) URL of the file to download",
        type=http_url,
    )
    parser.add_argument(
        "name",
        help="Name used for the downloaded file",
    )
    parser.add_argument(
        "--accept",
        help="Download request accepted content type, expressed as a MIME type",
        default=None,
        const=None,
        nargs="?",
    )
    parser.add_argument(
        "--auth-type",
        help="Authorization type",
        choices=["Basic", "Bearer", "Token"],
        default="Bearer",
        const="Bearer",
        nargs="?",
    )
    parser.add_argument(
        "--auth-token",
        help="Optional authorization token",
        default=None,
        const=None,
        nargs="?",
    )
    main(args=parser.parse_args())
