#!/usr/bin/env python3
"""The `whales` command: see and fix a machine's whales setup.

Lives at ~/.whales/scripts/whales_cli.py and is started by the small
~/.whales/bin/whales launcher. The updater replaces this file with each
release, so new commands arrive without a re-install. It reads everything
through whales_update.py beside it, so the two never disagree about what is
installed.
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))


def _load_updater():
    spec = importlib.util.spec_from_file_location("whales_update", os.path.join(HERE, "whales_update.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


up = _load_updater()

APP_URL_FILE = os.path.join(up.CONFIG_DIR, "app_url")
TOKEN_FILE = os.path.join(up.CONFIG_DIR, "token")
GATEWAY_FILE = os.path.join(up.CONFIG_DIR, "gateway")
DEFAULT_APP_URL = "https://whales.gojuly.ai"
# The installer declares this once it takes `--only <host>` and
# `--uninstall <host>`. An older one would treat both as a full run.
INSTALLER_API = "whales-installer-api: 2"
HOSTS = ("claude", "cursor")

HELP = """\
whales — your whales setup on this machine

  whales status              what is installed, and whether it is up to date
  whales update              check for a new whales now, instead of at the next session
  whales doctor [--fix]      find (and fix) anything missing or out of date
                [--online]   also ask the whales server whether it accepts your token
  whales logs                what the last update did
  whales version             installed versions
  whales install claude|cursor
                             connect another app, with the token already on this machine
  whales uninstall [claude|cursor]
                             disconnect one app, or everything when none is named
  whales help                this

whales updates itself when Claude Code or Cursor starts. Restart them to use a new version.
"""

GREEN, YELLOW, RED, DIM, NC = ("\033[32m", "\033[33m", "\033[31m", "\033[2m", "\033[0m") \
    if sys.stdout.isatty() else ("", "", "", "", "")


def _ok(text: str) -> None:
    print(f"{GREEN}✓{NC} {text}")


def _warn(text: str) -> None:
    print(f"{YELLOW}!{NC} {text}")


def _bad(text: str) -> None:
    print(f"{RED}✗{NC} {text}")


def _ago(ts) -> str:
    if not ts:
        return "never"
    seconds = int(time.time() - float(ts))
    if seconds < 90:
        return "just now"
    if seconds < 5400:
        return f"{seconds // 60} min ago"
    if seconds < 172800:
        return f"{seconds // 3600} h ago"
    return f"{seconds // 86400} days ago"


def _cursor_mcp_version() -> str:
    cfg = up._load_json(up.CURSOR_MCP)
    entry = cfg.get("mcpServers", {}).get("whales") if isinstance(cfg, dict) else None
    headers = entry.get("headers") if isinstance(entry, dict) else None
    return str(headers.get(up.VERSION_HEADER, "")) if isinstance(headers, dict) else ""


def _versions() -> dict:
    plugin = up.installed_plugin()
    return {
        "claude_code": plugin["version"] if plugin else "",
        "published": up.catalog_version() if plugin else "",
        "files": up._read_text(up.RUNTIME_VERSION),
        "cursor": up.cursor_wired(),
        "cursor_mcp": _cursor_mcp_version(),
    }


def cmd_version(_args) -> int:
    v = _versions()
    print(f"Claude Code plugin  {v['claude_code'] or 'not installed'}")
    print(f"Cursor              {v['files'] if v['cursor'] else 'not connected'}")
    print(f"whales command      {v['files'] or 'unknown'}")
    return 0


def cmd_status(_args) -> int:
    v = _versions()
    state = up.read_state()
    last = state.get("last_run") or {}
    print("whales on this machine\n")
    if v["claude_code"]:
        behind = v["published"] and v["published"] != v["claude_code"]
        line = f"Claude Code  plugin {v['claude_code']}"
        if behind:
            line += f" · {v['published']} is out, `whales update` gets it now"
        print(line)
    else:
        print(f"Claude Code  {DIM}not connected{NC}")
    if v["cursor"]:
        print(f"Cursor       {v['files'] or 'unknown version'}")
    else:
        print(f"Cursor       {DIM}not connected{NC}")
    gateway = up._read_text(GATEWAY_FILE) or "https://mcp.gojuly.ai"
    token = "token saved" if up._read_text(TOKEN_FILE) else "no token saved"
    print(f"Server       {gateway} ({token})")
    result = last.get("result", "")
    before, after = last.get("before") or "", last.get("after") or last.get("runtime") or ""
    detail = {"updated": f"updated {before} → {after}" if before and before != after else f"updated to {after}",
              "current": "already up to date",
              "failed": f"failed: {last.get('error', '')}"}.get(result, "no update has run yet")
    print(f"Last check   {_ago(state.get('last_check'))} · {detail}")
    if result == "updated":
        print(f"\n{DIM}Restart Claude Code and Cursor to use it, if you have not since.{NC}")
    if up.off_switch():
        _warn("Automatic updates are off on this machine (~/.whales/auto_update_off).")
    return 0


def _run_update(force: bool) -> int:
    updater = os.path.join(HERE, "whales_update.py")
    r = subprocess.run([sys.executable, updater, "--run", "--host", "cli"] + (["--force"] if force else []),
                       capture_output=True, text=True)
    output = r.stdout + r.stderr
    try:
        with open(up.LOG_FILE, "w") as fh:
            fh.write(output)
    except OSError:
        pass
    return r.returncode


def cmd_update(_args) -> int:
    before = _versions()
    print("Checking for a new whales…")
    code = _run_update(force=True)
    after = _versions()
    last = up.read_state().get("last_run") or {}
    if code != 0 or last.get("result") == "failed":
        _bad(f"The update did not finish: {last.get('error') or 'see `whales logs`'}")
        return 1
    if after["claude_code"] and after["claude_code"] != before["claude_code"]:
        _ok(f"Updated whales from {before['claude_code']} to {after['claude_code']}.")
        print("Restart Claude Code and Cursor to use it.")
    else:
        _ok(f"whales is up to date ({after['claude_code'] or after['files']}).")
    return 0


def cmd_logs(_args) -> int:
    text = up._read_text(up.LOG_FILE)
    print(text or "No update has run on this machine yet.")
    return 0


OK, WARN, BAD = "ok", "warn", "bad"
DEFAULT_GATEWAY = "https://mcp.gojuly.ai"
# Neither the updater nor `--fix` writes Cursor's server entry: only the
# installer does, so that is what to send someone whose entry is wrong.
REWRITE_CURSOR = "`whales install cursor` rewrites Cursor's whales entry with the token on this machine"


def _gateway() -> str:
    return (up._read_text(GATEWAY_FILE) or DEFAULT_GATEWAY).rstrip("/")


def _cursor_entry() -> dict:
    cfg = up._load_json(up.CURSOR_MCP) or {}
    entry = (cfg.get("mcpServers") or {}).get("whales") if isinstance(cfg, dict) else None
    return entry if isinstance(entry, dict) else {}


def _cursor_token(entry: dict) -> str:
    headers = entry.get("headers")
    auth = str(headers.get("Authorization") or "") if isinstance(headers, dict) else ""
    return auth[len("Bearer "):].strip() if auth.startswith("Bearer ") else ""


def _checks() -> list[tuple[str, str, str]]:
    """(level, what should be true, what to do when `--fix` cannot make it so)."""
    v = _versions()
    token = up._read_text(TOKEN_FILE)
    app = (up._read_text(APP_URL_FILE) or DEFAULT_APP_URL).rstrip("/")
    out: list[tuple[str, str, str]] = []

    def check(passed: bool, text: str, hint: str = "") -> None:
        out.append((OK if passed else BAD, text, hint))

    check(bool(token), "a whales token is saved in ~/.whales/token",
          f"run the install command from {app}/mcp")
    claude = up.find_claude()
    if claude or v["claude_code"]:
        if v["claude_code"] or not v["cursor"]:
            check(bool(v["claude_code"]), "the Claude Code plugin is installed",
                  "`whales install claude` adds it")
        else:
            # Someone who uses whales through Cursor and happens to have
            # Claude Code too has nothing wrong. `--fix` only updates an
            # installed plugin, so as a failure this sent them looking for help.
            out.append((WARN, "Claude Code is on this Mac but not connected to whales"
                        " (`whales install claude` adds it)", ""))
        if v["claude_code"] and v["published"]:
            check(v["claude_code"] == v["published"],
                  f"the Claude Code plugin is the latest ({v['claude_code']} installed, {v['published']} out)")
        settings = up._load_json(os.path.join(up.claude_config_dir(), "settings.json")) or {}
        allow = set(((settings.get("permissions") or {}).get("allow") or [])) if isinstance(settings, dict) else set()
        if v["claude_code"]:
            missing = [t for t in up.ALLOW_TOOLS if up.ALLOW_PREFIXES[0] + t not in allow]
            check(not missing, "Claude Code won't ask before whales' read-and-record tools"
                  + (f" (missing: {', '.join(missing)})" if missing else ""))
    if v["cursor"]:
        entry = _cursor_entry()
        check(bool(entry), "Cursor has the whales server", REWRITE_CURSOR)
        if entry:
            # Both are written once, by the installer, and nothing repairs
            # them. A stale server address or a token from an earlier install
            # leaves every other row green and Cursor quietly not connecting.
            want = _gateway() + "/mcp"
            url = str(entry.get("url") or "").rstrip("/")
            check(url == want, f"Cursor points at the whales server ({url or 'no address'})",
                  f"{REWRITE_CURSOR} and {want}")
            if token:
                check(_cursor_token(entry) == token, "Cursor sends the token saved in ~/.whales/token",
                      REWRITE_CURSOR)
        check(v["cursor_mcp"] == v["files"] and bool(v["files"]),
              f"Cursor tells whales which version it runs ({v['cursor_mcp'] or 'nothing'})")
        check(up.has_our_cursor_entries(), "Cursor runs whales' capture hooks")
        check(os.path.isfile(up.WRAPPER) and not up.wrapper_outdated(),
              "Cursor's whales hook is the current one")
        check(os.path.isfile(up.CAPTURE), "the capture hook is in ~/.whales/scripts")
    check(os.path.isfile(up.HELPER), "the critique upload helper is in ~/.whales/scripts")
    last = up.read_state().get("last_run") or {}
    check(last.get("result") != "failed", "the last update succeeded"
          + (f" (it said: {last.get('error')})" if last.get("result") == "failed" else ""),
          "`whales logs` shows what it tried")
    return out


def _handshake(url: str, token: str) -> str:
    """The HTTP status of an MCP `initialize`, the same one the installer
    sends. curl rather than urllib: python.org's Python on a Mac often has no
    certificates to verify TLS with."""
    curl = shutil.which("curl")
    if not curl:
        return ""
    r = subprocess.run(
        [curl, "-s", "-o", "/dev/null", "-w", "%{http_code}", "--max-time", "15", "-X", "POST", url,
         "-H", f"Authorization: Bearer {token}",
         "-H", "Content-Type: application/json",
         "-H", "Accept: application/json, text/event-stream",
         "-d", json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
             "protocolVersion": "2024-11-05", "capabilities": {},
             "clientInfo": {"name": "whales-doctor", "version": "1"}}})],
        capture_output=True, text=True)
    return r.stdout.strip()


def _online_check() -> tuple[str, str, str] | None:
    """Opt-in (`--online`): the server sees this as the token being used,
    which is the same signal that otherwise shows whether an app has ever
    connected. It tests the token and address Cursor itself sends."""
    entry = _cursor_entry() if up.cursor_wired() else {}
    if entry:
        url, token, whose = str(entry.get("url") or ""), _cursor_token(entry), "Cursor's"
    else:
        url, token, whose = _gateway() + "/mcp", up._read_text(TOKEN_FILE), "the saved"
    if not url or not token:
        return None
    code = _handshake(url, token)
    if code == "200":
        return OK, f"the whales server accepts {whose} token", ""
    if code in ("401", "403"):
        app = (up._read_text(APP_URL_FILE) or DEFAULT_APP_URL).rstrip("/")
        return (BAD, f"the whales server accepts {whose} token (it answered HTTP {code})",
                f"get a fresh token at {app}/mcp and run its install command again")
    # Offline, a proxy, a server hiccup: worth saying, not a fault on this machine.
    return WARN, f"could not reach the whales server at {url} (HTTP {code or 'no answer'})", ""


def _report(checks) -> list[tuple[str, str, str]]:
    show = {OK: _ok, WARN: _warn, BAD: _bad}
    for level, text, _hint in checks:
        show[level](text)
    if not shutil.which("whales"):
        # A note, not a failure: PATH belongs to the designer's shell profile,
        # which nothing but the interactive installer edits.
        _warn(f"`whales` is not on your PATH; run it as {up.LAUNCHER}")
    return [c for c in checks if c[0] == BAD]


def cmd_doctor(args) -> int:
    def run() -> list[tuple[str, str, str]]:
        checks = _checks()
        if "--online" in args:
            online = _online_check()
            if online:
                checks.append(online)
        return checks

    if not _report(run()):
        print("\nEverything looks right.")
        if up.cursor_wired():
            # The one thing no check here can see: whether Cursor has loaded
            # the server. It reads mcp.json only when it starts.
            print("If whales is missing or red in Cursor Settings → MCP, quit Cursor (Cmd+Q) and reopen it.")
        return 0
    if "--fix" not in args:
        print("\nRun `whales doctor --fix` to repair what it can.")
        return 1
    print("\nRepairing…\n")
    _run_update(force=True)
    failing = _report(run())
    if failing:
        print("\nStill to do:")
        for _level, text, hint in failing:
            print(f"  • {text}: {hint or 'the repair could not fix this; `whales logs` shows what it tried'}")
        return 1
    print()
    _ok("Repaired. Restart Claude Code and Cursor to pick it up.")
    return 0


def _installer(extra: list[str]) -> int:
    """Runs the whales installer with the token already on this machine. It
    owns host setup (token checks, old Claude Code versions, legacy
    migrations), so it is reused rather than copied here."""
    app = (up._read_text(APP_URL_FILE) or DEFAULT_APP_URL).rstrip("/")
    curl = shutil.which("curl")
    if not curl:
        _bad("curl is needed for this.")
        return 1
    fetched = subprocess.run([curl, "-fsSL", f"{app}/install.sh"], capture_output=True, text=True)
    if fetched.returncode != 0 or not fetched.stdout.strip():
        _bad(f"Could not download the installer from {app}.")
        return 1
    if INSTALLER_API not in fetched.stdout:
        _bad(f"The installer at {app} does not support this yet.")
        return 1
    return subprocess.run(["/bin/bash", "-s", "--", *extra], input=fetched.stdout, text=True).returncode


def cmd_install(args) -> int:
    if len(args) != 1 or args[0] not in HOSTS:
        print("Usage: whales install claude|cursor")
        return 2
    if not up._read_text(TOKEN_FILE):
        app = (up._read_text(APP_URL_FILE) or DEFAULT_APP_URL).rstrip("/")
        _bad(f"No whales token on this machine yet. Run the install command from {app}/mcp.")
        return 1
    return _installer(["--only", args[0]])


def cmd_uninstall(args) -> int:
    if len(args) > 1 or (args and args[0] not in HOSTS):
        print("Usage: whales uninstall [claude|cursor]")
        return 2
    if not args:
        print("This disconnects whales from every app on this machine and deletes its token.")
        try:
            with open("/dev/tty") as tty:
                sys.stdout.write("Continue? [y/N] ")
                sys.stdout.flush()
                answer = tty.readline().strip().lower()
        except OSError:
            answer = ""
        if answer not in ("y", "yes"):
            print("Nothing removed.")
            return 1
        return _installer(["--uninstall"])
    return _installer(["--uninstall", args[0]])


COMMANDS = {
    "help": lambda _a: (print(HELP, end=""), 0)[1],
    "version": cmd_version,
    "status": cmd_status,
    "update": cmd_update,
    "doctor": cmd_doctor,
    "logs": cmd_logs,
    "install": cmd_install,
    "uninstall": cmd_uninstall,
}


def main(argv: list[str]) -> int:
    name = argv[0] if argv else "help"
    if name in ("-h", "--help"):
        name = "help"
    command = COMMANDS.get(name)
    if command is None:
        print(f"Unknown command: {name}\n")
        print(HELP, end="")
        return 2
    return command(argv[1:])


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
