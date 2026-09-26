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
        `likely_downscaled`. Given Cursor's reduced copy of a pasted
        screenshot, it sends the original instead when it can find it
        (`original`), and says so when it cannot (`reduced_copy`); `--exact`
        sends the given file as is. For a page it first bundles everything the page
        loads from this machine (images, stylesheets, scripts, fonts) into
        the one file it sends, and prints what it bundled, what it could not
        find, and how many internet references it left as they are.

    critique_source.py fetch <critique_id> <index> [--out PATH]
        Downloads the exact image a critique ran on (screen `index` from the
        result's `renders`), for rebuilding from. Prints the saved path.

    critique_source.py compare <before> <after> [--out PATH] [--width N]
        Puts a screen and its rebuild side by side at the same height, each
        an image or an HTML page. Writes one self-contained HTML file (beside
        AFTER unless --out) and, when a Chrome-family browser is installed, a
        PNG of it. Prints both paths; `png` is null when none could be made.

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
import html
import json
import mimetypes
import os
import pathlib
import re
import shutil
import subprocess
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
# Screenshots: send the original, not a reduced copy
#
# Cursor saves a pasted image as a small JPEG — a 1179x2676 PNG arrives as
# 451x1024 — named "<original name>-<uuid>.jpg", in the project's `assets/`
# folder (spaces turned to underscores) and in its workspaceStorage (spaces
# kept). That copy is the only path the agent is given. Whales measures text
# contrast and icon sizes in pixels, and at that size its numbers are wrong:
# thin text blurs into its background and every icon falls under the size
# floor. The original is usually still where the designer took it from, so it
# is looked for by name and sent instead. `--exact` sends the given file as is.
# ---------------------------------------------------------------------------

_UUID_SUFFIX = re.compile(
    r"^(?P<stem>.+)-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
_CURSOR_COPY_DIRS = (("/.cursor/projects/", "/assets/"),
                     ("/Cursor/User/workspaceStorage/", "/images/"))
_IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp", ".gif")
SEARCH_DIRS = ("~/Downloads", "~/Desktop", "~/Pictures", "~/Documents")
_SEARCH_DEPTH = 2
# A phone screenshot narrower than this has lost most of its pixels (an iPhone
# screenshot is 1080-1320 wide); the critique still runs, with a warning.
_LOW_RES_WIDTH = 750


def _dimensions(content: bytes) -> tuple[int, int] | None:
    """Width and height from an image's header, for PNG, JPEG, GIF and WebP."""
    if content.startswith(b"\x89PNG\r\n\x1a\n") and len(content) >= 24:
        return int.from_bytes(content[16:20], "big"), int.from_bytes(content[20:24], "big")
    if content[:6] in (b"GIF87a", b"GIF89a") and len(content) >= 10:
        return int.from_bytes(content[6:8], "little"), int.from_bytes(content[8:10], "little")
    if content[:4] == b"RIFF" and content[8:12] == b"WEBP" and len(content) >= 30:
        chunk = content[12:16]
        if chunk == b"VP8X":
            return (int.from_bytes(content[24:27], "little") + 1,
                    int.from_bytes(content[27:30], "little") + 1)
        if chunk == b"VP8L":
            bits = int.from_bytes(content[21:25], "little")
            return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
        if chunk == b"VP8 ":
            return (int.from_bytes(content[26:28], "little") & 0x3FFF,
                    int.from_bytes(content[28:30], "little") & 0x3FFF)
        return None
    if content.startswith(b"\xff\xd8"):
        i = 2
        while i + 9 < len(content):
            if content[i] != 0xFF:
                i += 1
                continue
            marker = content[i + 1]
            if marker == 0xFF:
                i += 1
                continue
            if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
                i += 2
                continue
            if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                return (int.from_bytes(content[i + 7:i + 9], "big"),
                        int.from_bytes(content[i + 5:i + 7], "big"))
            i += 2 + int.from_bytes(content[i + 2:i + 4], "big")
    return None


def _file_dimensions(path: str) -> tuple[int, int] | None:
    try:
        with open(path, "rb") as fh:
            return _dimensions(fh.read())
    except OSError:
        return None


def _name_key(name: str) -> str:
    return " ".join(name.replace("_", " ").split()).casefold()


def _cursor_copy_stem(path: str) -> str | None:
    """The original file's name if `path` is Cursor's copy of a pasted image."""
    full = os.path.abspath(path)
    if not any(a in full and b in full for a, b in _CURSOR_COPY_DIRS):
        return None
    match = _UUID_SUFFIX.match(os.path.splitext(os.path.basename(full))[0])
    return match.group("stem") if match else None


def _candidates(stem: str):
    key = _name_key(stem)
    for top in SEARCH_DIRS:
        top = os.path.expanduser(top)
        base_depth = top.rstrip(os.sep).count(os.sep)
        for root, dirs, files in os.walk(top):
            if root.count(os.sep) - base_depth >= _SEARCH_DEPTH:
                dirs[:] = []
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            for name in files:
                base, ext = os.path.splitext(name)
                if ext.lower() in _IMAGE_SUFFIXES and _name_key(base) == key:
                    yield os.path.join(root, name)


def _find_original(stem: str, reduced: tuple[int, int]) -> tuple[str, tuple[int, int]] | None:
    """The largest same-named image with the copy's shape and more pixels."""
    ratio = reduced[0] / reduced[1]
    best = None
    for path in _candidates(stem):
        size = _file_dimensions(path)
        if not size or size[0] <= reduced[0]:
            continue
        # The same screen has the same shape, within the resize's rounding.
        if abs(size[0] / size[1] - ratio) > 0.02 * ratio:
            continue
        if best is None or size[0] > best[1][0]:
            best = (path, size)
    return best


def _pick_image(path: str, exact: bool) -> tuple[str, dict]:
    """The file to send for `path`, and what to tell the agent about it."""
    size = _file_dimensions(path)
    if not size:
        return path, {}
    stem = None if exact else _cursor_copy_stem(path)
    if stem:
        found = _find_original(stem, size)
        if found:
            original, original_size = found
            return original, {"original": {
                "path": original, "size": list(original_size),
                "instead_of": path, "reduced_size": list(size),
            }}
        return path, {"reduced_copy": {
            "size": list(size),
            "note": (f"This is Cursor's reduced copy of a pasted screenshot ({size[0]}x{size[1]}), "
                     f"and no original named \"{stem}\" was found in {', '.join(SEARCH_DIRS)}. "
                     "Whales measures text contrast and icon sizes in pixels, and at this size "
                     "they come out wrong. Ask the designer to drag the original file in or give "
                     "its path, and upload that instead."),
        }}
    if size[0] < _LOW_RES_WIDTH and size[1] > size[0] * 1.5:
        return path, {"low_resolution": {
            "size": list(size),
            "note": (f"This phone screenshot is only {size[0]}px wide, so text contrast and icon "
                     "sizes may be measured wrong. If the designer has the original file, it "
                     "will give a more accurate critique."),
        }}
    return path, {}


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


def upload(path: str, exact: bool = False) -> None:
    path = os.path.expanduser(path)
    report: dict = {}
    try:
        if path.lower().endswith(_PAGE_SUFFIXES):
            content, report = bundle_page(path)
            media = "text/html; charset=utf-8"
        else:
            path, report = _pick_image(path, exact)
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


# ---------------------------------------------------------------------------
# Before and after, side by side
#
# A rebuild is judged against the screen it came from, so the designer should
# see the two together at the same height rather than flip between files.
# Either side may be an image or an HTML page. A page is bundled exactly as for
# upload and laid out in an iframe at a phone or desktop width, so its
# neighbouring files load wherever the comparison is opened. The comparison is
# one HTML file; when a Chrome-family browser is installed it is also rendered
# to a PNG, which is what to show the designer (Cursor opens an HTML file as
# its source, which reads as if nothing was made).
# ---------------------------------------------------------------------------

_BROWSERS = (
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
)
_BROWSER_COMMANDS = ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser",
                     "microsoft-edge", "brave-browser")
_PHONE_WIDTH = 390
_DESKTOP_WIDTH = 1440
# Past this the PNG is mostly empty pixels; a phone screenshot is ~2600 tall.
_MAX_COMPARE_HEIGHT = 2000
_SIZE_ATTR = re.compile(r'data-whales-size="(\d+)x(\d+)"')

# Scales every panel to one height once the pages inside the iframes have laid
# out, then records the page's size for the screenshot pass to use.
_COMPARE_SCRIPT = """
window.addEventListener("load", () => {
  const panels = [...document.querySelectorAll("figure > .panel")];
  const natural = panels.map(el => {
    if (el.tagName === "IMG") return [el.naturalWidth, el.naturalHeight];
    const frame = el.querySelector("iframe");
    const doc = frame.contentDocument;
    const h = Math.max(doc.documentElement.scrollHeight, doc.body ? doc.body.scrollHeight : 0);
    frame.style.height = h + "px";
    return [Number(el.dataset.width), h];
  });
  const target = Math.min(Math.max(...natural.map(n => n[1])), %d);
  panels.forEach((el, i) => {
    const [w, h] = natural[i], s = target / h;
    el.style.width = Math.round(w * s) + "px";
    el.style.height = target + "px";
    if (el.tagName !== "IMG") el.querySelector("iframe").style.transform = "scale(" + s + ")";
  });
  const page = document.documentElement;
  document.body.setAttribute("data-whales-size", page.scrollWidth + "x" + page.scrollHeight);
});
""" % _MAX_COMPARE_HEIGHT

_COMPARE_STYLE = """
* { box-sizing: border-box; margin: 0; }
body { display: inline-flex; gap: 48px; padding: 40px 48px 48px; background: #f2f2f4;
       font: 20px/1.3 -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif; color: #222; }
figure { display: flex; flex-direction: column; gap: 16px; }
figcaption b { font-size: 26px; margin-right: 10px; }
figcaption span { color: #6b6b6b; }
.panel { display: block; overflow: hidden; background: #fff; box-shadow: 0 2px 12px rgba(0,0,0,.12); }
.panel iframe { border: 0; display: block; transform-origin: 0 0; }
"""


def _browser() -> str | None:
    override = os.environ.get("WHALES_BROWSER")
    if override:
        return override if os.path.isfile(override) else shutil.which(override)
    for path in _BROWSERS:
        if os.path.isfile(path):
            return path
    for name in _BROWSER_COMMANDS:
        found = shutil.which(name)
        if found:
            return found
    return None


def _is_page(path: str) -> bool:
    return path.lower().endswith(_PAGE_SUFFIXES)


def _page_width(other: str, width: int | None) -> int:
    """A page is laid out at `width`, else at the width its counterpart implies:
    a landscape screenshot is a desktop page, anything else a phone."""
    if width:
        return width
    size = None if _is_page(other) else _file_dimensions(other)
    return _DESKTOP_WIDTH if size and size[0] > size[1] else _PHONE_WIDTH


def _panel(path: str, label: str, width: int) -> tuple[str, list[str]]:
    caption = f"<figcaption><b>{html.escape(label)}</b><span>{html.escape(os.path.basename(path))}</span></figcaption>"
    if _is_page(path):
        content, report = bundle_page(path)
        doc = html.escape(content.decode("utf-8"), quote=True)
        body = (f'<div class="panel" data-width="{width}">'
                f'<iframe srcdoc="{doc}" width="{width}" scrolling="no"></iframe></div>')
        return f"<figure>{caption}{body}</figure>", report["missing"]
    with open(path, "rb") as fh:
        content = fh.read()
    if not _dimensions(content):
        _fail(f"{path} is neither an image nor an HTML page, so it cannot be compared.")
    uri = f"data:{_media_type(content, path)};base64,{base64.b64encode(content).decode('ascii')}"
    return f'<figure>{caption}<img class="panel" src="{uri}" alt=""></figure>', []


def compare_page(before: str, after: str, width: int | None = None) -> tuple[str, list[str]]:
    """One self-contained HTML page showing `before` and `after` side by side,
    and the local files either page referenced that could not be found."""
    left, missing_before = _panel(before, "Before", _page_width(after, width))
    right, missing_after = _panel(after, "After", _page_width(before, width))
    page = (f'<!doctype html><html><head><meta charset="utf-8"><title>Before and after</title>'
            f"<style>{_COMPARE_STYLE}</style></head><body>{left}{right}"
            f"<script>{_COMPARE_SCRIPT}</script></body></html>")
    return page, missing_before + missing_after


def _render_png(browser: str, page_path: str, png_path: str) -> str | None:
    """Screenshot the comparison at exactly its own size: one pass to lay it
    out and read that size, a second to capture it."""
    url = pathlib.Path(page_path).resolve().as_uri()
    base = [browser, "--headless=new", "--disable-gpu", "--hide-scrollbars",
            "--force-device-scale-factor=1", "--virtual-time-budget=5000"]
    try:
        dom = subprocess.run(base + ["--window-size=1600,1200", "--dump-dom", url],
                             capture_output=True, text=True, timeout=120).stdout
        size = _SIZE_ATTR.search(dom)
        if not size:
            return None
        subprocess.run(base + [f"--window-size={size.group(1)},{size.group(2)}",
                               f"--screenshot={os.path.abspath(png_path)}", url],
                       capture_output=True, timeout=120)
    except (OSError, subprocess.SubprocessError):
        return None
    return png_path if os.path.isfile(png_path) else None


def compare(before: str, after: str, out: str | None, width: int | None) -> None:
    before, after = os.path.expanduser(before), os.path.expanduser(after)
    for path in (before, after):
        if not os.path.isfile(path):
            _fail(f"Could not read {path}: no such file.")
    if out is None:
        out = os.path.splitext(after)[0] + ".compare.html"
    page, missing = compare_page(before, after, width)
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(page)
    result: dict = {"status": "ok", "html": out, "png": None}
    browser = _browser()
    if browser:
        result["png"] = _render_png(browser, out, os.path.splitext(out)[0] + ".png")
        if not result["png"]:
            result["note"] = "The browser could not render the comparison; open the HTML file instead."
    else:
        result["note"] = ("No Chrome, Chromium, Edge or Brave was found to render a PNG "
                          "(set WHALES_BROWSER to one); open the HTML file instead.")
    if missing:
        result["missing"] = missing
    print(json.dumps(result))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    up = sub.add_parser("upload", help="send a screenshot or an HTML page to Whales")
    up.add_argument("path")
    up.add_argument("--exact", action="store_true",
                    help="send this file as is, even if it is Cursor's reduced copy of a pasted image")
    fe = sub.add_parser("fetch", help="download the image a critique ran on")
    fe.add_argument("critique_id")
    fe.add_argument("index", type=int)
    fe.add_argument("--out")
    co = sub.add_parser("compare", help="show a screen and its rebuild side by side")
    co.add_argument("before")
    co.add_argument("after")
    co.add_argument("--out", help="the comparison's HTML path (default: beside AFTER); the PNG goes beside it")
    co.add_argument("--width", type=int,
                    help="lay out an HTML side at this width (default: 390, or 1440 beside a landscape screenshot)")
    args = parser.parse_args()
    if args.command == "upload":
        upload(args.path, args.exact)
    elif args.command == "fetch":
        fetch(args.critique_id, args.index, args.out)
    else:
        compare(args.before, args.after, args.out, args.width)


if __name__ == "__main__":
    main()
