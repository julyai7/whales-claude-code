#!/usr/bin/env python3
"""Move screens between this machine and Whales for `universal_critique`.

A model cannot put image bytes in an MCP tool call, and it should not retype a
page it already wrote: that is slow, costly, and what arrives is a copy, not
the file. So the critique tools take a handle instead, and this script is how
the file itself travels:

    critique_source.py upload <path>
        Sends one screenshot, or one HTML page, to Whales. Prints JSON with
        `source_id` — pass it to `universal_critique`, or to `self_critique`
        for a rebuilt page. For an image it also prints the size and
        `likely_downscaled`. For a page it first bundles everything the page
        loads from this machine (images, stylesheets, scripts, fonts) into
        the one file it sends, and prints what it bundled, what it could not
        find, and how many internet references it left as they are.

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
import base64
import json
import mimetypes
import os
import re
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request

# WHALES_CONFIG_DIR exists for testing against a local stack without touching
# the designer's real ~/.whales; nothing a designer runs sets it.
CONFIG_DIR = os.environ.get("WHALES_CONFIG_DIR") or os.path.expanduser("~/.whales")
TOKEN_FILE = os.path.join(CONFIG_DIR, "token")
GATEWAY_FILE = os.path.join(CONFIG_DIR, "gateway")
DEFAULT_GATEWAY = "https://mcp.gojuly.ai"
TIMEOUT_SECONDS = 60
# The gateway's own ceiling (it answers 413 above this). Checked here too, so
# an oversized page fails with a message that says what made it big.
MAX_UPLOAD_BYTES = 10 * 1024 * 1024

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


# ---------------------------------------------------------------------------
# HTML pages: one self-contained file
#
# Whales renders the page on its own server, where the page's neighbouring
# files do not exist: an `<img src="hero.png">` sent as-is renders as a broken
# image, and the critique then measures a page without its pictures. So every
# reference to a file on this machine is inlined — images and fonts as data:
# URIs, stylesheets as <style>, scripts as inline <script> — and internet
# references are left alone (Whales loads those itself). Nothing is dropped
# silently: a local file that cannot be found is reported in `missing`.
# ---------------------------------------------------------------------------

_PAGE_SUFFIXES = (".html", ".htm")
_LEAVE_ALONE = ("data:", "blob:", "#", "mailto:", "javascript:", "tel:", "about:")
_EXTRA_TYPES = {
    ".woff2": "font/woff2", ".woff": "font/woff", ".ttf": "font/ttf", ".otf": "font/otf",
    ".svg": "image/svg+xml", ".webp": "image/webp", ".avif": "image/avif",
    ".ico": "image/x-icon", ".js": "text/javascript", ".mjs": "text/javascript",
    ".css": "text/css", ".json": "application/json",
}
_ATTR_RE = r'(\s{name}\s*=\s*)("[^"]*"|\'[^\']*\'|[^\s>]+)'
_CSS_URL_RE = re.compile(r'url\(\s*("[^"]*"|\'[^\']*\'|[^)]*?)\s*\)', re.I)
_CSS_IMPORT_RE = re.compile(
    r'@import\s+(?:url\(\s*("[^"]*"|\'[^\']*\'|[^)]*?)\s*\)|("[^"]*"|\'[^\']*\'))([^;]*);', re.I)


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


class _Bundle:
    def __init__(self, root: str):
        self.root = root
        self.bundled: list[str] = []
        self.missing: list[str] = []
        self.external = 0

    def _is_external(self, ref: str) -> bool:
        lower = ref.strip().lower()
        if lower.startswith(("http://", "https://", "//")):
            self.external += 1
            return True
        return not lower or lower.startswith(_LEAVE_ALONE)

    def _local_path(self, ref: str, base_dir: str):
        """The file a local reference names, or None (recorded as missing).
        A leading "/" is the page's own folder: a page opened from disk has
        no web root, and its folder is what the author meant."""
        clean = urllib.parse.unquote(re.split(r"[?#]", ref.strip(), maxsplit=1)[0])
        if not clean:
            return None
        if clean.startswith("file://"):
            clean = urllib.parse.urlparse(clean).path
            path = clean
        elif clean.startswith("/") and not os.path.isfile(clean):
            path = os.path.join(self.root, clean.lstrip("/"))
        else:
            path = os.path.join(base_dir, clean)
        path = os.path.normpath(path)
        if os.path.isfile(path):
            return path
        self.missing.append(ref.strip())
        return None

    def _note(self, path: str) -> None:
        shown = os.path.relpath(path, self.root)
        if shown not in self.bundled:
            self.bundled.append(shown)

    def data_uri(self, ref: str, base_dir: str) -> str | None:
        if self._is_external(ref):
            return None
        path = self._local_path(ref, base_dir)
        if path is None:
            return None
        with open(path, "rb") as fh:
            content = fh.read()
        ext = os.path.splitext(path)[1].lower()
        media = _EXTRA_TYPES.get(ext) or _media_type(content, path)
        self._note(path)
        return f"data:{media};base64,{base64.b64encode(content).decode('ascii')}"

    def css(self, text: str, base_dir: str, depth: int = 0) -> str:
        def imported(match):
            ref = _unquote(match.group(1) or match.group(2) or "")
            media = match.group(3).strip()
            if depth > 8 or self._is_external(ref):
                return match.group(0)
            path = self._local_path(ref, base_dir)
            if path is None:
                return match.group(0)
            self._note(path)
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                inner = self.css(fh.read(), os.path.dirname(path), depth + 1)
            return f"@media {media} {{\n{inner}\n}}" if media else inner

        def url(match):
            ref = _unquote(match.group(1))
            uri = self.data_uri(ref, base_dir)
            # Unquoted: a data: URI has no spaces, quotes or parentheses, and
            # quotes would end a style="…" attribute this CSS may sit in.
            return f"url({uri})" if uri else match.group(0)

        # Imports are resolved first but kept out of the url() pass: their
        # CSS has already been through it, and a second pass would walk (and
        # count) its internet references twice.
        inlined: list[str] = []

        def hold(match):
            inlined.append(imported(match))
            return f"\x00WHALES_IMPORT_{len(inlined) - 1}\x00"

        text = _CSS_URL_RE.sub(url, _CSS_IMPORT_RE.sub(hold, text))
        return re.sub(r"\x00WHALES_IMPORT_(\d+)\x00", lambda m: inlined[int(m.group(1))], text)

    def attr(self, tag: str, name: str, fn) -> str:
        def swap(match):
            new = fn(_unquote(match.group(2)))
            return f'{match.group(1)}"{new}"' if new is not None else match.group(0)
        return re.sub(_ATTR_RE.format(name=name), swap, tag, flags=re.I)

    def srcset(self, value: str) -> str | None:
        if "data:" in value:  # a data: URI's own comma would split it wrongly
            return None
        parts, changed = [], False
        for candidate in value.split(","):
            bits = candidate.strip().split(None, 1)
            if not bits:
                continue
            uri = self.data_uri(bits[0], self.root)
            changed = changed or uri is not None
            parts.append(" ".join([uri or bits[0], *bits[1:]]))
        return ", ".join(parts) if changed else None

    def html(self, text: str) -> str:
        root = self.root
        uri = lambda ref: self.data_uri(ref, root)

        def stylesheet(match):
            tag = match.group(0)
            if not re.search(r'\srel\s*=\s*["\']?[^"\'>]*stylesheet', tag, re.I):
                if re.search(r'\srel\s*=\s*["\']?[^"\'>]*icon', tag, re.I):
                    return self.attr(tag, "href", uri)
                return tag
            href = re.search(_ATTR_RE.format(name="href"), tag, re.I)
            ref = _unquote(href.group(2)) if href else ""
            if not ref or self._is_external(ref):
                return tag
            path = self._local_path(ref, root)
            if path is None:
                return tag
            self._note(path)
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                css = self.css(fh.read(), os.path.dirname(path))
            media = re.search(_ATTR_RE.format(name="media"), tag, re.I)
            media_attr = f' media="{_unquote(media.group(2))}"' if media else ""
            return f"<style{media_attr}>\n{css}\n</style>"

        def script(match):
            attrs = match.group(1)
            src = re.search(_ATTR_RE.format(name="src"), attrs, re.I)
            ref = _unquote(src.group(2)) if src else ""
            if not ref or self._is_external(ref):
                return match.group(0)
            path = self._local_path(ref, root)
            if path is None:
                return match.group(0)
            self._note(path)
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                code = fh.read().replace("</script", "<\\/script")
            kept = re.sub(_ATTR_RE.format(name="src"), "", attrs, flags=re.I)
            return f"<script{kept}>\n{code}\n</script>"

        def media_tag(match):
            tag = match.group(0)
            for name in ("src", "poster"):
                tag = self.attr(tag, name, uri)
            return self.attr(tag, "srcset", self.srcset)

        # The page's own CSS first, so a stylesheet inlined below is not
        # walked a second time (and its internet references not counted twice).
        text = re.sub(r"(<style\b[^>]*>)([\s\S]*?)(</style\s*>)",
                      lambda m: m.group(1) + self.css(m.group(2), root) + m.group(3), text, flags=re.I)
        text = re.sub(r'(\sstyle\s*=\s*)("[^"]*"|\'[^\']*\')',
                      lambda m: m.group(1) + m.group(2)[0] + self.css(m.group(2)[1:-1], root) + m.group(2)[0],
                      text, flags=re.I)
        text = re.sub(r"<(?:img|source|video|audio|track|embed|input)\b[^>]*>", media_tag, text, flags=re.I)
        text = re.sub(r"<script\b([^>]*)>\s*</script\s*>", script, text, flags=re.I)
        text = re.sub(r"<link\b[^>]*>", stylesheet, text, flags=re.I)
        return text


def bundle_page(path: str) -> tuple[bytes, dict]:
    """The page at ``path`` as one self-contained UTF-8 file, and a report of
    what was inlined, what was missing, and how many internet references
    were left as they are."""
    with open(path, "rb") as fh:
        raw = fh.read()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = raw.decode("cp1252", errors="replace")
    bundle = _Bundle(os.path.dirname(os.path.abspath(path)))
    content = bundle.html(text).encode("utf-8")
    return content, {"bundled": bundle.bundled, "missing": bundle.missing,
                     "external": bundle.external}


def upload(path: str) -> None:
    path = os.path.expanduser(path)
    report: dict = {}
    try:
        if path.lower().endswith(_PAGE_SUFFIXES):
            content, report = bundle_page(path)
            media = "text/html; charset=utf-8"
        else:
            with open(path, "rb") as fh:
                content = fh.read()
            media = _media_type(content, path)
    except OSError as exc:
        _fail(f"Could not read {path}: {exc}")
    if len(content) > MAX_UPLOAD_BYTES:
        biggest = ""
        if report.get("bundled"):
            root = os.path.dirname(os.path.abspath(path))
            sizes = sorted(((os.path.getsize(os.path.join(root, f)), f) for f in report["bundled"]),
                           reverse=True)[:3]
            biggest = " Largest bundled files: " + ", ".join(
                f"{name} ({size // 1024} KB)" for size, name in sizes) + "."
        _fail(f"{os.path.basename(path)} is {len(content) // 1024} KB once bundled; Whales "
              f"accepts up to {MAX_UPLOAD_BYTES // (1024 * 1024)} MB.{biggest}")
    req = urllib.request.Request(
        f"{_gateway()}/critique-sources/",
        data=content,
        method="POST",
        headers={"Authorization": f"Bearer {_token()}", "Content-Type": media},
    )
    body, _ = _request(req)
    result = json.loads(body)
    result["filename"] = os.path.basename(path)
    result.update(report)
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
    up = sub.add_parser("upload", help="send a screenshot or an HTML page to Whales")
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
