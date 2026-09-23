#!/usr/bin/env python3
"""Move screenshots between this machine and Whales for `universal_critique`.

A model cannot put image bytes in an MCP tool call, so the critique tool takes
a handle instead, and this script is how the bytes travel:

    critique_source.py upload <image-path>
        Sends one screenshot to Whales. Prints JSON with `source_id` (pass it
        to `universal_critique`), the image size, and `likely_downscaled`.

    critique_source.py fetch <critique_id> <index> [--out PATH]
        Downloads the exact image a critique ran on (screen `index` from the
        result's `renders`), for rebuilding from. Prints the saved path.

Credential: `~/.whales/token`, written by the Whales installer — the same file
the capture hooks read, and for the same reason: it keeps the token out of the
command line, where it would land in the transcript and in `ps`. The gateway
is `~/.whales/gateway` if present (the hooks read the same file), else
https://mcp.gojuly.ai.

Standard library only, like the hook script beside the skills: this runs in
whatever Python the designer's machine has, with nothing installed for it.
"""
from __future__ import annotations

import argparse
import json
import mimetypes
import os
import sys
import tempfile
import urllib.error
import urllib.request

# WHALES_CONFIG_DIR exists for testing against a local stack without touching
# the designer's real ~/.whales; nothing a designer runs sets it.
CONFIG_DIR = os.environ.get("WHALES_CONFIG_DIR") or os.path.expanduser("~/.whales")
TOKEN_FILE = os.path.join(CONFIG_DIR, "token")
GATEWAY_FILE = os.path.join(CONFIG_DIR, "gateway")
DEFAULT_GATEWAY = "https://mcp.gojuly.ai"
TIMEOUT_SECONDS = 60

# Sniffed from the bytes, not trusted from the extension: a dragged file can be
# misnamed (a JPEG saved as `.png`), and the critique's vision pass fails on an
# image whose stated type doesn't match its bytes. The backend detects the real
# format too (whales_test #392); this sends an honest Content-Type regardless.
_SIGNATURES = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
)


def _read(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError:
        return ""


def _gateway() -> str:
    return (_read(GATEWAY_FILE) or DEFAULT_GATEWAY).rstrip("/")


def _token() -> str:
    token = _read(TOKEN_FILE)
    if not token:
        _fail("No Whales token at ~/.whales/token. Re-run the Whales installer from your "
              "Whales settings page, then try again.")
    return token


def _media_type(content: bytes, path: str) -> str:
    for signature, media in _SIGNATURES:
        if content.startswith(signature):
            return media
    if content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        return "image/webp"
    return mimetypes.guess_type(path)[0] or "application/octet-stream"


def _fail(message: str, code: int = 1) -> None:
    print(json.dumps({"status": "error", "detail": message}))
    sys.exit(code)


def _request(req: urllib.request.Request) -> tuple[bytes, str]:
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as resp:
            return resp.read(), resp.headers.get("Content-Type", "")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        try:
            body = json.loads(body).get("detail", body)
        except ValueError:
            pass
        _fail(f"Whales refused the request ({exc.code}): {body}")
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        _fail(f"Could not reach Whales at {_gateway()}: {exc}")
    raise AssertionError("unreachable")


def upload(path: str) -> None:
    path = os.path.expanduser(path)
    try:
        with open(path, "rb") as fh:
            content = fh.read()
    except OSError as exc:
        _fail(f"Could not read {path}: {exc}")
    req = urllib.request.Request(
        f"{_gateway()}/critique-sources/",
        data=content,
        method="POST",
        headers={"Authorization": f"Bearer {_token()}",
                 "Content-Type": _media_type(content, path)},
    )
    body, _ = _request(req)
    result = json.loads(body)
    result["filename"] = os.path.basename(path)
    print(json.dumps(result))


def fetch(critique_id: str, index: int, out: str | None) -> None:
    req = urllib.request.Request(
        f"{_gateway()}/critique-sources/{critique_id}/renders/{index}",
        headers={"Authorization": f"Bearer {_token()}"},
    )
    content, media = _request(req)
    ext = {"image/jpeg": ".jpg", "image/webp": ".webp", "image/gif": ".gif"}.get(
        media.split(";")[0].strip(), ".png")
    if out is None:
        out = os.path.join(tempfile.gettempdir(), f"whales-critique-{critique_id}-{index}{ext}")
    with open(out, "wb") as fh:
        fh.write(content)
    print(json.dumps({"status": "ok", "path": out, "bytes": len(content)}))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    up = sub.add_parser("upload", help="send a screenshot to Whales")
    up.add_argument("path")
    fe = sub.add_parser("fetch", help="download the image a critique ran on")
    fe.add_argument("critique_id")
    fe.add_argument("index", type=int)
    fe.add_argument("--out")
    args = parser.parse_args()
    if args.command == "upload":
        upload(args.path)
    else:
        fetch(args.critique_id, args.index, args.out)


if __name__ == "__main__":
    main()
