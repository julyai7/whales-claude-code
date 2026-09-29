#!/usr/bin/env python3
"""Whales' Cursor hook: ~/.cursor/hooks.json runs this for every event.

The Whales updater copies it from the plugin's cursor/wrapper.py to
~/.whales/scripts/whales_hook.py, the path Cursor's hooks.json has always
pointed at. It does two things, and must never do a third: block Cursor,
which is what exit code 2 means to it. So it always exits 0.

1. Runs capture_hook.py (beside this file) with the same arguments and input,
   so edits made with Cursor's own tools reach Whales.
2. On a session start, starts the updater, which checks for a new Whales at
   most every few minutes in a detached process. It prefers the updater
   inside the installed Claude Code plugin over the copy beside this file:
   that way a release can fix a broken copy here. Designers without Claude
   Code only have the copy here, so when it has not succeeded for days, a
   fresh one is downloaded first: a broken release must not strand them.

Kept small on purpose: this file is only replaced by the updater it starts.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.request

# The updater replaces any wrapper without this line (the installer's old one).
WHALES_CURSOR_WRAPPER = 2

HERE = os.path.dirname(os.path.abspath(__file__))
CAPTURE = os.path.join(HERE, "capture_hook.py")
CONFIG_DIR = os.path.dirname(HERE)
STATE_FILE = os.path.join(CONFIG_DIR, "update_state.json")
SOURCE_FILE = os.path.join(CONFIG_DIR, "plugin_source")
DEFAULT_SOURCE = "https://raw.githubusercontent.com/julyai7/whales-claude-code/main/plugins/whales"
RECOVER_AFTER = 3 * 86400


def _updater() -> str:
    config = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude")
    try:
        with open(os.path.join(config, "plugins", "installed_plugins.json")) as fh:
            entries = json.load(fh)["plugins"]["whales@whales"]
        for entry in entries:
            if entry.get("scope", "user") == "user":
                path = os.path.join(entry["installPath"], "scripts", "whales_update.py")
                if os.path.isfile(path):
                    return path
    except Exception:  # noqa: BLE001 — no Claude Code install: use the copy here
        pass
    return os.path.join(HERE, "whales_update.py")


def _recover(updater: str) -> None:
    """Replaces this machine's own updater with a fresh download when it has
    been checking without succeeding for RECOVER_AFTER, at most once a day."""
    try:
        with open(STATE_FILE) as fh:
            state = json.load(fh)
    except (OSError, ValueError):
        return  # never ran yet: nothing to recover from
    now = time.time()
    last_ok = float(state.get("last_success") or 0)
    if not state.get("last_check") or now - last_ok < RECOVER_AFTER \
            or now - float(state.get("last_recover") or 0) < 86400:
        return
    state["last_recover"] = now
    with open(STATE_FILE, "w") as fh:
        json.dump(state, fh)
    try:
        with open(SOURCE_FILE) as fh:
            base = fh.read().strip()
    except OSError:
        base = ""
    with urllib.request.urlopen((base or DEFAULT_SOURCE) + "/scripts/whales_update.py", timeout=10) as resp:
        data = resp.read(2 * 1024 * 1024)
    compile(data, updater, "exec")
    with open(updater + ".tmp", "wb") as fh:
        fh.write(data)
    os.chmod(updater + ".tmp", 0o755)
    os.replace(updater + ".tmp", updater)


def _start_update() -> None:
    updater = _updater()
    if not os.path.isfile(updater):
        return
    if os.path.dirname(updater) == HERE:
        try:
            _recover(updater)
        except Exception:  # noqa: BLE001 — try again tomorrow
            pass
    subprocess.Popen(
        [sys.executable, updater, "--session-start", "--host", "cursor"],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True, close_fds=True,
    )


def main(args: list[str]) -> None:
    if args[:1] == ["--whales-install-hooks"]:
        # What the installer used to call on the wrapper it wrote.
        spec = args[1] if len(args) > 1 else ""
        r = subprocess.run([sys.executable, _updater(), "--install-cursor-hooks", spec])
        sys.exit(r.returncode)
    try:
        if args[args.index("--event") + 1] == "SessionStart":
            _start_update()
    except (ValueError, IndexError, OSError):
        pass
    try:
        payload = sys.stdin.buffer.read()
    except OSError:
        payload = b""
    try:
        subprocess.run([sys.executable, CAPTURE, *args], input=payload, timeout=60)
    except (OSError, subprocess.SubprocessError):
        pass


if __name__ == "__main__":
    if sys.argv[1:2] == ["--whales-install-hooks"]:
        main(sys.argv[1:])
    try:
        main(sys.argv[1:])
    except BaseException:  # noqa: BLE001 — anything but 0 can block Cursor
        pass
    sys.exit(0)
