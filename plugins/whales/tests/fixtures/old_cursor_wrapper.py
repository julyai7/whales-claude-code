# The Cursor wrapper the Whales installer wrote before 0.6.0, verbatim from
# whales_test frontend/editor/public/install.sh (origin/main, 2026-09-29).
# Every Cursor machine set up before 0.6.0 runs this until it is replaced.
#!/usr/bin/env python3
"""Whales' Cursor hook. Written by the Whales installer; re-run it to replace.

Cursor runs this for every event wired in ~/.cursor/hooks.json. It does two
things, and must never do a third — block Cursor, which is what exit code 2
means to it. So it always exits 0.

1. Runs capture_hook.py (beside this file) with the same arguments and input,
   so edits made with Cursor's own tools reach Whales.
2. On a session start, at most once a day, starts a detached job that updates
   the Whales Claude Code plugin (Cursor loads it too) and capture_hook.py —
   and, with a new version, critique_source.py (the upload script beside
   this file) and Whales' own entries in ~/.cursor/hooks.json.

The installer also runs it once as `--whales-install-hooks <file>` to write
those entries, so the installer and the daily update merge them one way.

To stop the daily update: touch ~/.whales/auto_update_off
The last update's output: ~/.whales/update.log
"""
from __future__ import annotations

import datetime
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
WRAPPER = os.path.abspath(__file__)
CONFIG_DIR = os.path.dirname(HERE)
CAPTURE = os.path.join(HERE, "capture_hook.py")
HELPER = os.path.join(HERE, "critique_source.py")
CURSOR_HOOKS = os.path.join(os.path.expanduser("~"), ".cursor", "hooks.json")
CAPTURE_VERSION = os.path.join(HERE, "capture_hook.version")
OFF_FILE = os.path.join(CONFIG_DIR, "auto_update_off")
LOG = os.path.join(CONFIG_DIR, "update.log")
# The plugin repo's whales_hook.py, filled in by the installer. Only used when
# Claude Code is not on this machine to hand us the released copy.
HOOK_URL = "__WHALES_HOOK_SCRIPT_URL__"
MAX_DOWNLOAD = 2 * 1024 * 1024


def _auto_update_off() -> bool:
    # A file, not only an env var: Cursor opened from the Dock never sees the
    # designer's shell profile, so an exported variable would not reach here.
    return os.path.exists(OFF_FILE) or os.environ.get("WHALES_AUTO_UPDATE", "").lower() in (
        "0", "off", "false", "no")


def _claim_today() -> bool:
    """True for exactly one caller per day. O_EXCL, not check-then-touch: two
    Cursor windows opening together must not both start an update."""
    name = f"update-{datetime.date.today().isoformat()}.lock"
    try:
        os.close(os.open(os.path.join(CONFIG_DIR, name), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
    except OSError:
        return False
    for other in os.listdir(CONFIG_DIR):
        if other.startswith("update-") and other.endswith(".lock") and other != name:
            try:
                os.remove(os.path.join(CONFIG_DIR, other))
            except OSError:
                pass
    return True


def _start_background_update() -> None:
    if _auto_update_off() or not _claim_today():
        return
    with open(LOG, "wb") as log:
        # Its own session so it outlives this hook and a runner that kills the
        # hook's process group; no inherited stdio, because Cursor reads a
        # hook's stdout and this job's output is not an answer to Cursor.
        subprocess.Popen(
            [sys.executable, os.path.abspath(__file__), "--whales-update"],
            stdin=subprocess.DEVNULL, stdout=log, stderr=log,
            start_new_session=True, close_fds=True,
        )


def _find_claude() -> str | None:
    found = shutil.which("claude")
    if found:
        return found
    # Cursor's environment often lacks the PATH entries a login shell adds.
    for path in ("~/.local/bin/claude", "/opt/homebrew/bin/claude", "/usr/local/bin/claude"):
        path = os.path.expanduser(path)
        if os.access(path, os.X_OK):
            return path
    return None


def _run(cmd: list[str], timeout: int):
    print("$", " ".join(cmd), flush=True)
    try:
        r = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                           timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"  failed: {exc}", flush=True)
        return None
    print(f"  exit {r.returncode}: {(r.stdout + r.stderr).strip()[-1500:]}", flush=True)
    return r


def _installed_plugin(claude: str) -> dict | None:
    r = _run([claude, "plugin", "list", "--json"], 60)
    try:
        rows = json.loads(r.stdout) if r else []
    except ValueError:
        return None
    rows = rows.get("installed", []) if isinstance(rows, dict) else rows
    for row in rows:
        if row.get("id") == "whales@whales" and row.get("scope", "user") == "user":
            return row
    return None


def _read(path: str) -> str:
    try:
        with open(path) as fh:
            return fh.read().strip()
    except OSError:
        return ""


def _fetch(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=30) as resp:
        data = resp.read(MAX_DOWNLOAD + 1)
    if len(data) > MAX_DOWNLOAD:
        raise ValueError(f"{url} is larger than expected")
    return data


def _replace_capture(source: bytes, version: str) -> None:
    compile(source, CAPTURE, "exec")  # never swap in something that cannot run
    fd, tmp = tempfile.mkstemp(dir=HERE, prefix=".capture-")
    with os.fdopen(fd, "wb") as fh:
        fh.write(source)
    os.chmod(tmp, 0o755)
    os.replace(tmp, CAPTURE)
    with open(CAPTURE_VERSION, "w") as fh:
        fh.write(version)
    print(f"capture_hook.py is now {version}", flush=True)


# The plugin's cursor/hooks.json, for when it cannot be fetched (and until a
# plugin that has the file is published). Keep it identical to that file.
BUILTIN_CURSOR_HOOKS = {
    "sessionStart": [{"command": 'python3 "${HOME}/.whales/scripts/whales_hook.py" --event SessionStart --source cursor_hook', "timeout": 10}],
    "sessionEnd": [{"command": 'python3 "${HOME}/.whales/scripts/whales_hook.py" --event SessionEnd --source cursor_hook', "timeout": 10}],
    "beforeSubmitPrompt": [{"command": 'python3 "${HOME}/.whales/scripts/whales_hook.py" --event UserPromptSubmit --source cursor_hook', "timeout": 10}],
    "afterFileEdit": [{"command": 'python3 "${HOME}/.whales/scripts/whales_hook.py" --event PostToolUse --source cursor_hook', "timeout": 10}],
    "preToolUse": [{"command": 'python3 "${HOME}/.whales/scripts/whales_hook.py" --event PreToolUse --source cursor_hook', "timeout": 5}],
    "postToolUse": [{"command": 'python3 "${HOME}/.whales/scripts/whales_hook.py" --event DesignContext --source cursor_hook', "matcher": "Write", "timeout": 5}],
    "preCompact": [{"command": 'python3 "${HOME}/.whales/scripts/whales_hook.py" --event PreCompact --source cursor_hook', "timeout": 10}],
    "stop": [{"command": 'python3 "${HOME}/.whales/scripts/whales_hook.py" --event Stop --source cursor_hook', "timeout": 10}],
}
_HOOK_ARGS = re.compile(r'whales_hook\.py"?\s+(--event [A-Za-z]+ --source cursor_hook)\s*$')


def _our_entries(spec) -> dict | None:
    """Whales' ~/.cursor/hooks.json entries from a plugin hooks file, or None
    if it is not one. Only each entry's event, --event/--source, matcher and
    timeout are taken from the file. The command is rebuilt around this
    wrapper's absolute path, as it always has been: that path is how Whales'
    entries are told apart from everyone else's (here, on uninstall, and by
    the legacy-hook migration), and the file's "${HOME}" form would depend
    on Cursor running commands through a shell, which it does not document.
    """
    hooks = spec.get("hooks") if isinstance(spec, dict) else None
    if not isinstance(hooks, dict) or not hooks:
        return None
    out: dict = {}
    for event, entries in hooks.items():
        if not isinstance(entries, list) or not entries:
            return None
        for entry in entries:
            match = _HOOK_ARGS.search(str((entry or {}).get("command", "")))
            if not isinstance(entry, dict) or not match:
                return None
            ours = {"command": f"{WRAPPER} {match.group(1)}"}
            for key in ("matcher", "timeout"):
                if key in entry:
                    ours[key] = entry[key]
            out.setdefault(event, []).append(ours)
    return out


def merge_cursor_hooks(entries: dict) -> None:
    """Replace Whales' entries in ~/.cursor/hooks.json with ``entries``, in
    every event, and leave everyone else's exactly as they are. A file that
    is there but cannot be read is left alone (raises) rather than replaced."""
    try:
        with open(CURSOR_HOOKS) as fh:
            cfg = json.load(fh)
    except FileNotFoundError:
        cfg = {}
    if not isinstance(cfg, dict):
        raise ValueError(f"{CURSOR_HOOKS} is not a JSON object")
    cfg.setdefault("version", 1)
    hooks = cfg.setdefault("hooks", {})
    for event, listed in list(hooks.items()):
        if isinstance(listed, list):
            kept = [e for e in listed if WRAPPER not in str((e or {}).get("command", ""))]
            if kept:
                hooks[event] = kept
            else:
                hooks.pop(event)
    for event, ours in entries.items():
        hooks.setdefault(event, []).extend(ours)
    os.makedirs(os.path.dirname(CURSOR_HOOKS), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(CURSOR_HOOKS), prefix=".hooks-")
    with os.fdopen(fd, "w") as fh:
        json.dump(cfg, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, CURSOR_HOOKS)


def install_hooks(spec_path: str) -> int:
    """`--whales-install-hooks <file>`: the installer's one call. Uses the
    fetched plugin file when it is a valid one, the built-in copy otherwise,
    and prints which. Exit 1 only here — Cursor never runs this mode."""
    entries = None
    if spec_path:
        try:
            with open(spec_path) as fh:
                entries = _our_entries(json.load(fh))
        except (OSError, ValueError):
            entries = None
    origin = "plugin" if entries is not None else "built-in"
    if entries is None:
        entries = _our_entries({"hooks": BUILTIN_CURSOR_HOOKS})
    try:
        merge_cursor_hooks(entries)
    except (OSError, ValueError) as exc:
        print(f"failed: {exc}")
        return 1
    print(origin)
    return 0


def _replace_helper(source: bytes) -> None:
    compile(source, HELPER, "exec")  # never swap in something that cannot run
    fd, tmp = tempfile.mkstemp(dir=HERE, prefix=".helper-")
    with os.fdopen(fd, "wb") as fh:
        fh.write(source)
    os.chmod(tmp, 0o755)
    os.replace(tmp, HELPER)
    print("critique_source.py updated", flush=True)


def _refresh_extras(read) -> None:
    """With a new version: the upload script, and Whales' Cursor hook entries.
    Each on its own, so one failing leaves the other (and the capture hook's
    own update) to go ahead. A plugin older than these files simply has none."""
    try:
        _replace_helper(read("skills/universal-critique/critique_source.py"))
    except Exception as exc:  # noqa: BLE001
        print(f"critique_source.py not updated: {exc}", flush=True)
    try:
        entries = _our_entries(json.loads(read("cursor/hooks.json")))
        if entries is None:
            raise ValueError("not a Whales Cursor hooks file")
        if os.path.exists(CURSOR_HOOKS):
            merge_cursor_hooks(entries)
            print("Cursor hook entries updated", flush=True)
    except Exception as exc:  # noqa: BLE001
        print(f"Cursor hook entries not updated: {exc}", flush=True)


def _read_file(path: str) -> bytes:
    with open(path, "rb") as fh:
        return fh.read()


def _background_update() -> None:
    plugin = None
    claude = _find_claude()
    if claude and _installed_plugin(claude):
        # `plugin update` alone does not refresh a git marketplace: it reports
        # "already at the latest" until the catalog is fetched.
        _run([claude, "plugin", "marketplace", "update", "whales"], 180)
        _run([claude, "plugin", "update", "whales@whales"], 300)
        plugin = _installed_plugin(claude)

    current = _read(CAPTURE_VERSION)
    if plugin:
        # The copy Claude Code just installed is a released version, so Cursor
        # capture follows the same bump-to-release rule as the plugin.
        version = plugin.get("version") or ""
        source = os.path.join(plugin.get("installPath") or "", "scripts", "whales_hook.py")
        if version and version != current and os.path.isfile(source):
            root = plugin.get("installPath") or ""
            # Before the capture hook, whose version file is what marks this
            # version done.
            _refresh_extras(lambda rel: _read_file(os.path.join(root, rel)))
            with open(source, "rb") as fh:
                _replace_capture(fh.read(), version)
        return
    if "/scripts/" not in HOOK_URL:
        return
    base = HOOK_URL.rsplit("/scripts/", 1)[0]
    manifest = json.loads(_fetch(base + "/.claude-plugin/plugin.json"))
    version = manifest.get("version") or ""
    if version and version != current:
        _refresh_extras(lambda rel: _fetch(f"{base}/{rel}"))
        _replace_capture(_fetch(HOOK_URL), version)


def main() -> None:
    args = sys.argv[1:]
    if args[:1] == ["--whales-update"]:
        try:
            _background_update()
        except Exception as exc:  # noqa: BLE001 — a failed update waits for tomorrow
            print(f"update failed: {exc}", flush=True)
        return
    try:
        i = args.index("--event")
        if args[i + 1:i + 2] == ["SessionStart"]:
            _start_background_update()
    except (ValueError, OSError):
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
        sys.exit(install_hooks(sys.argv[2] if len(sys.argv) > 2 else ""))
    try:
        main()
    except BaseException:  # noqa: BLE001 — anything but 0 can block Cursor
        pass
    sys.exit(0)
