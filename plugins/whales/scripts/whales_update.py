#!/usr/bin/env python3
"""Keeps Whales up to date on a designer's machine, so the installer runs once.

Two things need updating, and nothing else updates them:

1. **The Claude Code plugin** (skills, hooks, scripts, .mcp.json). Claude
   Code's own auto-update only runs after the first prompt of an interactive
   session, then waits up to 10 minutes; idle sessions and `-p` runs never
   update. So this runs `claude plugin marketplace update` + `plugin update`
   itself, at session start, in a detached process. The new version loads on
   the next restart. Claude Code's own auto-update stays on: it is the
   independent way back if a release ever breaks this file.

2. **The files outside the plugin**, in ~/.whales and the hosts' configs.
   These were written once by the installer and never touched again:
   - ~/.whales/scripts/: the Cursor wrapper (``whales_hook.py``, the path
     ~/.cursor/hooks.json runs), the capture hook it runs
     (``capture_hook.py``), the critique upload helper both hosts use, this
     updater, and the ``whales`` command.
   - Whales' entries in ~/.cursor/hooks.json, and the version header in
     ~/.cursor/mcp.json.
   - The allow rules for Whales' read-and-record tools in Claude Code's
     settings.json, so a new tool does not prompt until someone re-installs.

   They are copied from the INSTALLED plugin (``installPath`` in
   installed_plugins.json), never from the session that happens to be running
   this: a ``--plugin-dir`` session would otherwise push unreleased code, and
   a session started before an update would write the old files back.
   Designers with Cursor but no Claude Code get them from the plugin repo.

Modes:
  --session-start --host claude-code|cursor
      What the hooks call. Returns at once and prints nothing (Claude Code
      feeds SessionStart stdout to the model): it starts ``--run`` in a
      detached process, at most once per CHECK_INTERVAL.
  --run [--host H] [--force]
      The update itself. One at a time per machine (a lock in ~/.whales),
      because Claude Code and Cursor opening together would otherwise run two
      `marketplace update`s, which collide and both fail.
  --sync [--source DIR|URL] [--enable-cursor-hooks]
      Only the files outside the plugin, from the given plugin copy. The
      installer's last step.
  --install-cursor-hooks FILE
      Merge Whales' Cursor hook entries from a plugin cursor/hooks.json file
      (the built-in list when it is not a valid one), printing which was used.

Run with no arguments it does nothing and exits 0: Cursor runs Claude Code
plugins' hooks.json on its own, without the exec-form args.

Standard library only, and every failure is logged, never raised: this runs
unattended on designers' machines.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request

HOME = os.path.expanduser("~")
CONFIG_DIR = os.path.join(HOME, ".whales")
SCRIPTS = os.path.join(CONFIG_DIR, "scripts")
BIN_DIR = os.path.join(CONFIG_DIR, "bin")
STATE_FILE = os.path.join(CONFIG_DIR, "update_state.json")
LOCK_FILE = os.path.join(CONFIG_DIR, "update.lock")
LOG_FILE = os.path.join(CONFIG_DIR, "update.log")
# Where Cursor-only machines fetch the plugin's files from. Written by the
# installer; the default is the stable channel.
SOURCE_FILE = os.path.join(CONFIG_DIR, "plugin_source")
DEFAULT_SOURCE = "https://raw.githubusercontent.com/julyai7/whales-claude-code/main/plugins/whales"
# A developer's escape hatch, not a designer-facing setting.
OFF_FILE = os.path.join(CONFIG_DIR, "auto_update_off")

WRAPPER = os.path.join(SCRIPTS, "whales_hook.py")
CAPTURE = os.path.join(SCRIPTS, "capture_hook.py")
HELPER = os.path.join(SCRIPTS, "critique_source.py")
UPDATER = os.path.join(SCRIPTS, "whales_update.py")
CLI = os.path.join(SCRIPTS, "whales_cli.py")
LAUNCHER = os.path.join(BIN_DIR, "whales")
# The version of the files above; also read by pre-0.6.0 Cursor wrappers.
RUNTIME_VERSION = os.path.join(SCRIPTS, "capture_hook.version")
# Present in every Cursor wrapper from 0.6.0 on. A wrapper without it is the
# installer's old heredoc, which the capture hook replaces (see whales_hook.py).
WRAPPER_MARKER = "WHALES_CURSOR_WRAPPER = 2"

CURSOR_DIR = os.path.join(HOME, ".cursor")
CURSOR_HOOKS = os.path.join(CURSOR_DIR, "hooks.json")
CURSOR_MCP = os.path.join(CURSOR_DIR, "mcp.json")

PLUGIN_ID = "whales@whales"
MARKETPLACE = "whales"
VERSION_HEADER = "X-Whales-Plugin-Version"
# Allowed without a prompt: Whales' read-and-record tools. Tools that write a
# design system or run a Figma extraction are left to Claude Code's prompt.
ALLOW_TOOLS = (
    "whales", "ask_whales", "record_reaction", "get_design_profile",
    "search_design_history", "list_design_systems", "get_design_system",
    "submit_design", "record_critique", "record_approval", "universal_critique",
    "get_rebuild_contract", "self_critique", "product_context",
)
ALLOW_PREFIXES = ("mcp__plugin_whales_whales__", "mcp__whales__")

CHECK_INTERVAL = int(os.environ.get("WHALES_UPDATE_INTERVAL", "600"))
STALE_LOCK_SECONDS = 15 * 60
MAX_DOWNLOAD = 2 * 1024 * 1024

# The plugin's cursor/hooks.json, for when it cannot be read. Keep identical.
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

LAUNCHER_TEXT = """#!/bin/sh
# The whales command. Written by the Whales updater; the program is
# ~/.whales/scripts/whales_cli.py, which updates with the plugin.
exec python3 "$HOME/.whales/scripts/whales_cli.py" "$@"
"""


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------

def log(message: str) -> None:
    print(message, flush=True)


def _read_text(path: str) -> str:
    try:
        with open(path) as fh:
            return fh.read().strip()
    except OSError:
        return ""


def _load_json(path: str):
    try:
        with open(path) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def _write_atomic(path: str, data: bytes, mode: int = 0o644) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".whales-")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def _write_json(path: str, value, backup: bool = False) -> None:
    """Atomic, and with ``backup`` a one-time copy of a file we did not write
    (never replaced, so it is always the copy from before Whales touched it)."""
    if backup and os.path.exists(path) and not os.path.exists(path + ".whales-backup"):
        shutil.copy2(path, path + ".whales-backup")
    mode = os.stat(path).st_mode & 0o777 if os.path.exists(path) else 0o644
    _write_atomic(path, (json.dumps(value, indent=2) + "\n").encode(), mode)


def read_state() -> dict:
    state = _load_json(STATE_FILE)
    return state if isinstance(state, dict) else {}


def write_state(**changes) -> None:
    state = read_state()
    state.update(changes)
    try:
        _write_json(STATE_FILE, state)
    except OSError:
        pass


def off_switch() -> bool:
    return os.path.exists(OFF_FILE) or os.environ.get("WHALES_AUTO_UPDATE", "").lower() in (
        "0", "off", "false", "no")


# --------------------------------------------------------------------------
# Claude Code
# --------------------------------------------------------------------------

def claude_config_dir() -> str:
    return os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(HOME, ".claude")


def installed_plugin() -> dict | None:
    """Whales' user-scope install, read straight from Claude Code's file (the
    CLI takes seconds to answer). {"version", "installPath"} or None."""
    data = _load_json(os.path.join(claude_config_dir(), "plugins", "installed_plugins.json"))
    entries = (data or {}).get("plugins", {}).get(PLUGIN_ID) if isinstance(data, dict) else None
    if not isinstance(entries, list):
        return None
    for entry in entries:
        if isinstance(entry, dict) and entry.get("scope", "user") == "user" and entry.get("installPath"):
            return {"version": str(entry.get("version") or ""), "installPath": entry["installPath"]}
    return None


def catalog_version() -> str:
    """The version the local copy of the marketplace lists, i.e. what
    `plugin update` would install. Read after `marketplace update`."""
    known = _load_json(os.path.join(claude_config_dir(), "plugins", "known_marketplaces.json"))
    location = ((known or {}).get(MARKETPLACE) or {}).get("installLocation") if isinstance(known, dict) else None
    if not location:
        return ""
    catalog = _load_json(os.path.join(location, ".claude-plugin", "marketplace.json"))
    for plugin in (catalog or {}).get("plugins", []) if isinstance(catalog, dict) else []:
        if isinstance(plugin, dict) and plugin.get("name") == "whales":
            return str(plugin.get("version") or "")
    return ""


def find_claude() -> str | None:
    found = shutil.which("claude")
    if found:
        return found
    # Cursor's environment often lacks the PATH entries a login shell adds.
    for path in ("~/.local/bin/claude", "/opt/homebrew/bin/claude", "/usr/local/bin/claude"):
        path = os.path.expanduser(path)
        if os.access(path, os.X_OK):
            return path
    return None


def clean_env() -> dict:
    """The environment for a `claude` we start. A hook runs inside a Claude
    Code session, and a nested `claude` that inherits its CLAUDECODE /
    CLAUDE_CODE_* markers runs as that session's child. CLAUDE_CONFIG_DIR is
    kept: it is which Claude Code install this is."""
    return {k: v for k, v in os.environ.items()
            if not k.startswith("CLAUDE") or k == "CLAUDE_CONFIG_DIR"}


def _run(cmd: list[str], timeout: int):
    log("$ " + " ".join(cmd))
    try:
        r = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                           timeout=timeout, env=clean_env())
    except (OSError, subprocess.SubprocessError) as exc:
        log(f"  failed: {exc}")
        return None
    out = (r.stdout + r.stderr).strip()
    log(f"  exit {r.returncode}: {out[-2000:]}")
    return r


# --------------------------------------------------------------------------
# Where the plugin's files come from
# --------------------------------------------------------------------------

class LocalSource:
    """An installed copy of the plugin."""

    def __init__(self, root: str):
        self.root = root
        self.label = root

    def read(self, rel: str) -> bytes:
        with open(os.path.join(self.root, rel), "rb") as fh:
            return fh.read()


class RemoteSource:
    """The plugin folder in the repo, for machines without Claude Code."""

    def __init__(self, base: str):
        self.base = base.rstrip("/")
        self.label = self.base

    def read(self, rel: str) -> bytes:
        with urllib.request.urlopen(f"{self.base}/{rel}", timeout=30) as resp:
            data = resp.read(MAX_DOWNLOAD + 1)
        if len(data) > MAX_DOWNLOAD:
            raise ValueError(f"{rel} is larger than expected")
        return data


def source_version(source) -> str:
    manifest = json.loads(source.read(".claude-plugin/plugin.json"))
    return str(manifest.get("version") or "")


def remote_base() -> str:
    return _read_text(SOURCE_FILE) or DEFAULT_SOURCE


def make_source(spec: str):
    if not spec:
        return None
    return RemoteSource(spec) if spec.startswith(("http://", "https://")) else LocalSource(spec)


# --------------------------------------------------------------------------
# Files outside the plugin
# --------------------------------------------------------------------------

def cursor_wired() -> bool:
    """Whether this machine has Whales in Cursor at all: our wrapper, or our
    server in Cursor's MCP config."""
    if os.path.exists(WRAPPER):
        return True
    cfg = _load_json(CURSOR_MCP)
    return isinstance(cfg, dict) and isinstance(cfg.get("mcpServers"), dict) \
        and "whales" in cfg["mcpServers"]


def wrapper_outdated() -> bool:
    return os.path.exists(WRAPPER) and WRAPPER_MARKER not in _read_text(WRAPPER)


def _install_script(source, rel: str, dest: str) -> None:
    data = source.read(rel)
    compile(data, dest, "exec")  # never swap in something that cannot run
    _write_atomic(dest, data, 0o755)
    log(f"{os.path.basename(dest)} updated")


def our_cursor_entries(spec) -> dict | None:
    """Whales' ~/.cursor/hooks.json entries from a plugin hooks file, or None
    if it is not one. Only each entry's event, --event/--source, matcher and
    timeout come from the file. The command is rebuilt around the wrapper's
    absolute path: that path is how Whales' entries are told apart from
    everyone else's (here, on uninstall, and by the legacy-hook migration)."""
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


def has_our_cursor_entries() -> bool:
    cfg = _load_json(CURSOR_HOOKS)
    hooks = cfg.get("hooks") if isinstance(cfg, dict) else None
    return isinstance(hooks, dict) and any(
        WRAPPER in str((e or {}).get("command", ""))
        for listed in hooks.values() if isinstance(listed, list) for e in listed)


def merge_cursor_hooks(entries: dict) -> None:
    """Replace Whales' entries in ~/.cursor/hooks.json with ``entries`` in
    every event, and leave everyone else's exactly as they are. A file that is
    there but cannot be read is left alone (raises) rather than replaced."""
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
    _write_json(CURSOR_HOOKS, cfg)


def install_cursor_hooks(spec_path: str) -> int:
    """`--install-cursor-hooks FILE`: the plugin file's entries when it is a
    valid one, the built-in list otherwise; prints which."""
    entries = None
    if spec_path:
        try:
            with open(spec_path) as fh:
                entries = our_cursor_entries(json.load(fh))
        except (OSError, ValueError):
            entries = None
    origin = "plugin" if entries is not None else "built-in"
    if entries is None:
        entries = our_cursor_entries({"hooks": BUILTIN_CURSOR_HOOKS})
    try:
        merge_cursor_hooks(entries)
    except (OSError, ValueError) as exc:
        print(f"failed: {exc}")
        return 1
    print(origin)
    return 0


def set_cursor_version_header(version: str) -> bool:
    """Cursor's MCP entry, written once by the installer, never said which
    Whales it runs. Updates only an entry that is there."""
    cfg = _load_json(CURSOR_MCP)
    entry = cfg.get("mcpServers", {}).get("whales") if isinstance(cfg, dict) else None
    if not isinstance(entry, dict):
        return False
    headers = entry.get("headers")
    if not isinstance(headers, dict):
        headers = entry["headers"] = {}
    if headers.get(VERSION_HEADER) == version:
        return False
    headers[VERSION_HEADER] = version
    _write_json(CURSOR_MCP, cfg, backup=True)
    return True


def allow_whales_tools() -> int:
    """Adds allow rules for Whales' read-and-record tools to Claude Code's
    user settings. The same guards as the installer: only next to our
    marketplace entry, add only, and a tool the designer put under deny or
    ask is theirs."""
    path = os.path.join(claude_config_dir(), "settings.json")
    cfg = _load_json(path)
    if not isinstance(cfg, dict):
        return 0
    known = cfg.get("extraKnownMarketplaces")
    if not isinstance(known, dict) or not isinstance(known.get(MARKETPLACE), dict):
        return 0
    perms = cfg.get("permissions")
    if perms is None:
        perms = cfg["permissions"] = {}
    if not isinstance(perms, dict):
        return 0
    allow = perms.setdefault("allow", [])
    if not isinstance(allow, list):
        return 0
    theirs = set(perms.get("deny") or []) | set(perms.get("ask") or [])
    new = [prefix + tool for tool in ALLOW_TOOLS for prefix in ALLOW_PREFIXES
           if prefix + tool not in allow and prefix + tool not in theirs]
    if not new:
        return 0
    allow.extend(new)
    _write_json(path, cfg, backup=True)
    return len(new)


def install_launcher() -> None:
    _write_atomic(LAUNCHER, LAUNCHER_TEXT.encode(), 0o755)
    # ~/.local/bin is on PATH wherever Claude Code's own installer ran. Only a
    # link that is missing or already ours: never replace someone's `whales`.
    local_bin = os.path.join(HOME, ".local", "bin")
    link = os.path.join(local_bin, "whales")
    if os.path.isdir(local_bin) and not os.path.lexists(link):
        os.symlink(LAUNCHER, link)
        log(f"linked {link}")


def sync(source, version: str, enable_cursor_hooks: bool = False) -> bool:
    """Brings every file outside the plugin to ``version``. Each step on its
    own, so one failing leaves the rest to go ahead. True when the files the
    hosts run were all written, which is what marks the version done."""
    ok = True
    steps = [("scripts/whales_update.py", UPDATER), ("scripts/whales_cli.py", CLI),
             ("skills/universal-critique/critique_source.py", HELPER)]
    cursor = cursor_wired() or enable_cursor_hooks
    if cursor:
        # The capture hook before the wrapper that runs it.
        steps += [("scripts/whales_hook.py", CAPTURE), ("cursor/wrapper.py", WRAPPER)]
    for rel, dest in steps:
        try:
            _install_script(source, rel, dest)
        except Exception as exc:  # noqa: BLE001
            ok = False
            log(f"{os.path.basename(dest)} not updated: {exc}")
    try:
        install_launcher()
    except OSError as exc:
        log(f"whales command not installed: {exc}")

    if cursor and (enable_cursor_hooks or has_our_cursor_entries()):
        # Only entries that are already there (or asked for): a designer who
        # removed Whales' Cursor hooks keeps them removed.
        try:
            spec = json.loads(source.read("cursor/hooks.json"))
            entries = our_cursor_entries(spec)
            if entries is None:
                raise ValueError("not a Whales Cursor hooks file")
        except Exception as exc:  # noqa: BLE001
            log(f"using the built-in Cursor hook list: {exc}")
            entries = our_cursor_entries({"hooks": BUILTIN_CURSOR_HOOKS})
        try:
            merge_cursor_hooks(entries)
            log("Cursor hook entries updated")
        except (OSError, ValueError) as exc:
            log(f"Cursor hook entries not updated: {exc}")
    try:
        if set_cursor_version_header(version):
            log(f"Cursor MCP entry now reports {version}")
    except OSError as exc:
        log(f"Cursor MCP entry not updated: {exc}")
    try:
        added = allow_whales_tools()
        if added:
            log(f"added {added} allow rules")
    except OSError as exc:
        log(f"allow rules not updated: {exc}")

    if ok:
        _write_atomic(RUNTIME_VERSION, version.encode())
        log(f"files outside the plugin are at {version}")
    return ok


# --------------------------------------------------------------------------
# The update
# --------------------------------------------------------------------------

def acquire_lock() -> bool:
    os.makedirs(CONFIG_DIR, exist_ok=True)
    for _ in range(2):
        try:
            fd = os.open(LOCK_FILE, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            try:
                if time.time() - os.path.getmtime(LOCK_FILE) > STALE_LOCK_SECONDS:
                    os.remove(LOCK_FILE)  # left by a killed run
                    continue
            except OSError:
                continue
            return False
        with os.fdopen(fd, "w") as fh:
            fh.write(str(os.getpid()))
        return True
    return False


def release_lock() -> None:
    try:
        os.remove(LOCK_FILE)
    except OSError:
        pass


def run(host: str, force: bool = False) -> int:
    if not acquire_lock():
        log("another Whales update is already running")
        return 0
    started = time.time()
    write_state(last_check=started)
    before = installed_plugin()
    after = before
    errors: list[str] = []
    changed = False
    try:
        claude = find_claude()
        source = None
        if before:
            if claude:
                _run([claude, "plugin", "marketplace", "update", MARKETPLACE], 180)
                published = catalog_version()
                # `!=`, not `<`: lowering the published version is how a bad
                # release is rolled back, and that has to reach people too.
                if published and published != before["version"]:
                    _run([claude, "plugin", "update", PLUGIN_ID], 300)
                    after = installed_plugin() or before
                    if after["version"] == before["version"]:
                        errors.append(f"plugin still {before['version']}, published {published}")
                    changed = after["version"] != before["version"]
            else:
                log("the claude command was not found: syncing from the installed plugin only")
            if os.path.isdir(after["installPath"]):
                source = LocalSource(after["installPath"])
        elif cursor_wired():
            source = RemoteSource(remote_base())

        if source is not None:
            version = source_version(source)
            if force or version != _read_text(RUNTIME_VERSION) or wrapper_outdated():
                if sync(source, version):
                    changed = True
                else:
                    errors.append("some files outside the plugin were not updated")
    except Exception as exc:  # noqa: BLE001 — logged; the next session tries again
        errors.append(str(exc))
    finally:
        release_lock()
    result = "failed" if errors else ("updated" if changed else "current")
    error = "; ".join(errors)
    if result != "failed":
        write_state(last_success=time.time())
    write_state(last_run={
        "host": host, "started": started, "finished": time.time(), "result": result,
        "error": error,
        "before": (before or {}).get("version", ""), "after": (after or {}).get("version", ""),
        "runtime": _read_text(RUNTIME_VERSION),
    })
    log(f"result: {result}" + (f" ({error})" if error else ""))
    return 0 if result != "failed" else 1


def session_start(host: str) -> int:
    """Called by the hooks on every session start. Prints nothing."""
    if off_switch():
        return 0
    if time.time() - float(read_state().get("last_check") or 0) < CHECK_INTERVAL:
        return 0
    os.makedirs(CONFIG_DIR, exist_ok=True)
    # Claimed before the child starts, so sessions opening together start one
    # run between them (the lock stops the rest).
    write_state(last_check=time.time())
    with open(LOG_FILE, "wb") as out:
        # Its own session, so it outlives the hook and a host that kills the
        # hook's process group. No inherited stdio: the host reads a hook's
        # stdout as its answer.
        subprocess.Popen([sys.executable, os.path.abspath(__file__), "--run", "--host", host],
                         stdin=subprocess.DEVNULL, stdout=out, stderr=out,
                         start_new_session=True, close_fds=True)
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--session-start", action="store_true")
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--sync", action="store_true")
    parser.add_argument("--install-cursor-hooks", metavar="FILE")
    parser.add_argument("--host", default="")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--source", default="")
    parser.add_argument("--enable-cursor-hooks", action="store_true")
    args, _ = parser.parse_known_args(argv)

    if args.install_cursor_hooks is not None:
        return install_cursor_hooks(args.install_cursor_hooks)
    if args.session_start:
        return session_start(args.host or "unknown")
    if args.run:
        return run(args.host or "cli", force=args.force)
    if args.sync:
        plugin = installed_plugin()
        source = make_source(args.source) or (
            LocalSource(plugin["installPath"]) if plugin and os.path.isdir(plugin["installPath"])
            else RemoteSource(remote_base()))
        try:
            version = source_version(source)
        except Exception as exc:  # noqa: BLE001
            log(f"cannot read the plugin at {source.label}: {exc}")
            return 1
        return 0 if sync(source, version, enable_cursor_hooks=args.enable_cursor_hooks) else 1
    return 0


if __name__ == "__main__":
    try:
        code = main(sys.argv[1:])
    except BaseException:  # noqa: BLE001 — a hook must never fail the host
        code = 0 if "--session-start" in sys.argv or len(sys.argv) == 1 else 1
    sys.exit(code)
