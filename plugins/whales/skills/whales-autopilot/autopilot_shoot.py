#!/usr/bin/env python3
"""Screenshot, measure and upload a Whales Autopilot version.

Usage (see the whales-autopilot skill):

    python3 autopilot_shoot.py --url http://localhost:3000 \
        --frames '[{"name": "inbox", "url_or_path": "/inbox"}, {"name": "detail", "url_or_path": "/inbox/1"}]' \
        --viewport 390x844 --lint-js lint.js --conv-js conv.js --walk --out .whales/autopilot/run/v3

    python3 autopilot_shoot.py --url file:///abs/path/w2.html   # a wireframe board: shoots #window

    python3 autopilot_shoot.py --url http://localhost:3000/inbox --press "Archive"   # one panel step

Prints one JSON object on stdout:

    {"source_ids": [...], "frames": [{"name", "png"}],
     "measurements": {"lint", "conv", "walk": {"pressed": [...], "dead": [...]}},
     "panel": {"pressed", "changed", "error"?}}          # panel only with --press

The measuring mirrors the rethink engine (v9 shoot_v and press.py): the board
is ``#window`` when the page has one, else the whole page; a dead button is
one whose press leaves ``#window`` (or ``body``) unchanged.

Uploads go through the universal-critique skill's ``critique_source.py``
(same ``~/.whales/token`` credential and gateway). Needs Playwright for
Python; without it this exits 2 and says how to install it.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from urllib.parse import urljoin, urlparse
from urllib.request import pathname2url

INSTALL = "pip install playwright && python -m playwright install chromium"
DEFAULT_VIEWPORT = (1800, 1000)
MAX_WALK = 5
UPLOAD_SECONDS = 120

# v9 unclip(): a board of frames in one no-wrap row can run past the viewport,
# and an element screenshot then cuts every frame after the first few.
UNCLIP_JS = """() => { let n = 0; for (const e of document.querySelectorAll('#window, #window *')) {
    const cs = getComputedStyle(e); if (cs.display.includes('flex') && cs.flexWrap === 'nowrap' && e.children.length > 1
        && (e.scrollWidth > e.clientWidth + 4 || e.getBoundingClientRect().width > innerWidth + 4)) {
        e.style.width = 'auto'; e.style.maxWidth = '100%'; e.style.flexWrap = 'wrap'; e.style.rowGap = e.style.rowGap || '32px'; n++; } }
    return n; }"""

# v9's walk: the first five distinct visible button labels.
LABELS_JS = """() => { const w = document.querySelector('#window') || document.body; const out = [], seen = new Set();
    for (const el of w.querySelectorAll('button, [role=button]')) { const t = (el.innerText || '').trim().split('\\n')[0];
      if (!t || seen.has(t)) continue; seen.add(t); out.push(t); if (out.length >= %d) break; } return out; }""" % MAX_WALK

SNAPSHOT_JS = "(document.querySelector('#window')||document.body).innerHTML"


def fail(message: str, code: int = 1):
    print(json.dumps({"status": "error", "detail": message}))
    sys.exit(code)


def parse_viewport(text: str | None) -> tuple[int, int]:
    if not text:
        return DEFAULT_VIEWPORT
    m = re.fullmatch(r"\s*(\d+)\s*[xX,]\s*(\d+)\s*", text)
    if not m:
        raise ValueError(f"--viewport must be WxH, like 390x844 (got {text!r})")
    return int(m.group(1)), int(m.group(2))


def to_url(target: str, base: str = "") -> str:
    """An http(s)/file URL as is; an existing path as file://; anything else
    joined onto ``base`` (a route like ``/inbox`` on the app URL)."""
    if urlparse(target).scheme in ("http", "https", "file"):
        return target
    path = os.path.expanduser(target)
    if os.path.exists(path):
        return "file://" + pathname2url(os.path.abspath(path))
    if base and urlparse(base).scheme in ("http", "https"):
        return urljoin(base, target)
    return "file://" + pathname2url(os.path.abspath(path))


def parse_frames(raw: str | None, url: str) -> list[dict]:
    """``--frames`` as JSON (or a path to a JSON file): a list of names or of
    ``{name, url_or_path?, selector?}``. Absent: one frame, the board at --url."""
    if not raw:
        return [{"name": "board", "url": to_url(url), "selector": None}]
    if not raw.lstrip().startswith(("[", "{")) and os.path.isfile(os.path.expanduser(raw)):
        with open(os.path.expanduser(raw)) as fh:
            raw = fh.read()
    value = json.loads(raw)
    if isinstance(value, dict):
        value = [value]
    if not isinstance(value, list) or not value:
        raise ValueError("--frames must be a non-empty JSON list")
    frames = []
    for i, item in enumerate(value):
        if isinstance(item, str):
            item = {"name": item}
        if not isinstance(item, dict):
            raise ValueError(f"--frames item {i} must be a name or an object")
        target = item.get("url_or_path") or item.get("url") or item.get("path") or url
        frames.append({"name": str(item.get("name") or f"frame{i + 1}"),
                       "url": to_url(target, to_url(url)), "selector": item.get("selector") or None})
    return frames


def safe_name(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", name).strip("_") or "frame"


def read_script(path: str | None) -> str | None:
    if not path:
        return None
    with open(os.path.expanduser(path)) as fh:
        return fh.read()


def merge_walks(walks: list[dict]) -> dict:
    """One ``{pressed, dead}`` for the version, in frame order, each label once."""
    pressed, dead = [], []
    for w in walks:
        pressed += [x for x in w.get("pressed", []) if x not in pressed]
        dead += [x for x in w.get("dead", []) if x not in dead]
    return {"pressed": pressed, "dead": dead}


def per_frame(results: list[tuple[str, object]]):
    """One frame: its result as the engine's single-page script returns it.
    Several: keyed by frame name, in order."""
    if len(results) == 1:
        return results[0][1]
    return {name: value for name, value in results}


# --- the upload ----------------------------------------------------------------

def upload_helper() -> str:
    here = os.path.dirname(os.path.abspath(__file__))
    for path in (os.path.join(here, "..", "universal-critique", "critique_source.py"),
                 os.path.expanduser("~/.whales/scripts/critique_source.py")):
        if os.path.isfile(path):
            return os.path.abspath(path)
    return ""


def upload(png: str) -> dict:
    """``{"source_id": ...}`` or ``{"error": ...}``, never raises."""
    helper = upload_helper()
    if not helper:
        return {"error": "critique_source.py not found (reinstall the whales plugin)"}
    try:
        done = subprocess.run([sys.executable, helper, "upload", png, "--exact"],
                              capture_output=True, text=True, timeout=UPLOAD_SECONDS)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"error": str(exc)}
    try:
        result = json.loads(done.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        result = {}
    if done.returncode == 0 and result.get("source_id"):
        return {"source_id": result["source_id"]}
    return {"error": result.get("detail") or done.stderr.strip()[-500:] or f"exit {done.returncode}"}


# --- the browser -------------------------------------------------------------

def press(page, label: str, timeout: int = 3000) -> None:
    """Press what a person names, the way a person would (rethink press.py):
    buttons and links by accessible name first, then any text; the first match
    a real click can reach."""
    w = page.locator("#window")
    if not w.count():
        w = page.locator("body")
    words = [x for x in re.split(r"\W+", label) if x]
    rx = re.compile(r"\W*".join(re.escape(x) for x in words), re.I) if words else None
    groups = [w.get_by_role("button", name=label, exact=True), w.get_by_role("button", name=label),
              w.get_by_role("link", name=label), w.get_by_text(label, exact=True), w.get_by_text(label)]
    if rx:
        groups += [w.get_by_role("button", name=rx), w.get_by_role("link", name=rx), w.get_by_text(rx)]
    for loc in groups:
        for i in range(min(loc.count(), 6)):
            el = loc.nth(i)
            try:
                el.click(trial=True, timeout=600)
            except Exception:  # noqa: BLE001 — hidden, covered or disabled: try the next
                continue
            el.click(timeout=timeout)
            return
    raise RuntimeError(f"nothing a person could press is named {label!r}")


def load(page, url: str, settle_ms: int = 1200) -> None:
    page.goto(url)
    page.wait_for_timeout(settle_ms)
    try:
        if page.evaluate(UNCLIP_JS):
            page.wait_for_timeout(300)
    except Exception:  # noqa: BLE001
        pass


def shot(page, selector: str | None, png: str) -> None:
    loc = page.locator(selector or "#window")
    if loc.count():
        loc.first.screenshot(path=png)
    elif selector:
        raise RuntimeError(f"no element matches {selector!r}")
    else:
        page.screenshot(path=png, full_page=True)


def walk(page, url: str) -> dict:
    """v9's dead-button walk: reload, press each label, compare the board."""
    labels = page.evaluate(LABELS_JS)
    dead = []
    clock = getattr(page, "clock", None)
    try:
        clock.install()
    except Exception:  # noqa: BLE001 — older Playwright: plain waits instead
        clock = None
    for label in labels:
        page.goto(url)
        page.wait_for_timeout(600)
        if clock:
            try:
                clock.pause_at(int(time.time() * 1000) + 1000)
            except Exception:  # noqa: BLE001
                clock = None
        before = page.evaluate(SNAPSHOT_JS)
        try:
            press(page, label)
            if clock:
                clock.run_for(700)
            page.wait_for_timeout(300 if clock else 1000)
            if page.evaluate(SNAPSHOT_JS) == before:
                dead.append(label)
        except Exception:  # noqa: BLE001 — a press that fails is a dead action
            dead.append(label)
    return {"pressed": labels, "dead": dead}


def run(args) -> dict:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        fail(f"Playwright for Python is not installed. Run: {INSTALL}", 2)
    viewport = parse_viewport(args.viewport)
    frames = parse_frames(args.frames, args.url)
    lint_js, conv_js = read_script(args.lint_js), read_script(args.conv_js)
    out = os.path.abspath(os.path.expanduser(args.out or tempfile.mkdtemp(prefix="whales-autopilot-")))
    os.makedirs(out, exist_ok=True)

    shots, lint, conv, walks, panel = [], [], [], [], None
    with sync_playwright() as pw:
        try:
            browser = pw.chromium.launch()
        except Exception as exc:  # noqa: BLE001 — usually the browser was never downloaded
            fail(f"Could not start Chromium ({exc}). Run: {INSTALL}", 2)
        try:
            for i, frame in enumerate(frames):
                page = browser.new_page(viewport={"width": viewport[0], "height": viewport[1]})
                load(page, frame["url"])
                if args.press and i == 0:
                    # A panel step: earlier presses replay the path, the last is this step's.
                    for n, label in enumerate(args.press):
                        before = page.evaluate(SNAPSHOT_JS)
                        try:
                            press(page, label)
                            page.wait_for_timeout(900)
                            panel = {"pressed": label, "changed": page.evaluate(SNAPSHOT_JS) != before}
                        except Exception as exc:  # noqa: BLE001
                            panel = {"pressed": label, "changed": False, "error": str(exc)[:300]}
                            break
                png = os.path.join(out, f"{i + 1:02d}-{safe_name(frame['name'])}.png")
                shot(page, frame["selector"], png)
                shots.append({"name": frame["name"], "png": png})
                for script, into in ((lint_js, lint), (conv_js, conv)):
                    if script:
                        try:
                            into.append((frame["name"], page.evaluate(script)))
                        except Exception as exc:  # noqa: BLE001
                            into.append((frame["name"], {"error": str(exc)[:300]}))
                if args.walk:
                    walks.append(walk(page, frame["url"]))
                page.close()
        finally:
            browser.close()

    measurements: dict = {}
    if lint:
        measurements["lint"] = per_frame(lint)
    if conv:
        measurements["conv"] = per_frame(conv)
    if args.walk:
        measurements["walk"] = merge_walks(walks)
    result = {"source_ids": [], "frames": shots, "measurements": measurements}
    if panel is not None:
        result["panel"] = panel
    if not args.no_upload:
        errors = []
        for s in shots:
            up = upload(s["png"])
            if up.get("source_id"):
                s["source_id"] = up["source_id"]
                result["source_ids"].append(up["source_id"])
            else:
                errors.append({"frame": s["name"], "error": up.get("error")})
        if errors:
            result["upload_errors"] = errors
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--url", required=True, help="the app URL, or a file:// URL or path to a wireframe HTML")
    parser.add_argument("--frames", help="JSON list (or a file of it): names or {name, url_or_path?, selector?}")
    parser.add_argument("--viewport", help="WxH, e.g. 390x844 (default 1800x1000)")
    parser.add_argument("--lint-js", help="file holding the instruction's scripts.lint_js")
    parser.add_argument("--conv-js", help="file holding the instruction's scripts.conv_js")
    parser.add_argument("--walk", action="store_true", help="press up to 5 buttons per frame, report dead ones")
    parser.add_argument("--press", action="append",
                        help="panel step: press this control on the first frame (repeat to replay earlier presses)")
    parser.add_argument("--out", help="directory for the PNGs (default: a new temp dir)")
    parser.add_argument("--no-upload", action="store_true", help="shoot and measure only")
    args = parser.parse_args(argv)
    try:
        result = run(args)
    except (ValueError, OSError) as exc:
        fail(str(exc))
    print(json.dumps(result))
    return 1 if result.get("upload_errors") else 0


if __name__ == "__main__":
    sys.exit(main())
