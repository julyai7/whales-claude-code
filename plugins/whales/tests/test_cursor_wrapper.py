"""The Cursor wrapper (cursor/wrapper.py), moving Cursor machines off the
installer's old wrapper (whales_hook.py as capture_hook.py), and the restart
notice after a Claude Code update.

Every test uses a tmp HOME; the "updater" the wrapper starts is a stub that
records which copy ran.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

PLUGIN = Path(__file__).resolve().parents[1]
WRAPPER_SRC = PLUGIN / "cursor" / "wrapper.py"
HOOK_SRC = PLUGIN / "scripts" / "whales_hook.py"
MARKER = "WHALES_CURSOR_WRAPPER = 2"

STUB_UPDATER = """#!{python}
import os, sys
open(os.path.join(os.environ["HOME"], "updater_ran"), "a").write(__file__ + " " + " ".join(sys.argv[1:]) + "\\n")
"""
STUB_CAPTURE = """#!{python}
import sys
data = sys.stdin.read()
print("capture " + " ".join(sys.argv[1:]) + " stdin=" + data)
sys.exit(3)
"""


def _env(home: Path) -> dict:
    return {"HOME": str(home), "CLAUDE_CONFIG_DIR": str(home / ".claude"), "PATH": "/usr/bin:/bin"}


def _wait(predicate, timeout: float = 10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.1)
    return False


@pytest.fixture
def home(tmp_path) -> Path:
    scripts = tmp_path / ".whales" / "scripts"
    scripts.mkdir(parents=True)
    shutil.copy(WRAPPER_SRC, scripts / "whales_hook.py")
    (scripts / "capture_hook.py").write_text(STUB_CAPTURE.format(python=sys.executable))
    (scripts / "whales_update.py").write_text(STUB_UPDATER.format(python=sys.executable))
    return tmp_path


def _installed_plugin(home: Path) -> Path:
    root = home / ".claude" / "plugins" / "cache" / "whales" / "whales" / "0.6.0"
    (root / "scripts").mkdir(parents=True)
    (root / "scripts" / "whales_update.py").write_text(STUB_UPDATER.format(python=sys.executable))
    (home / ".claude" / "plugins" / "installed_plugins.json").write_text(json.dumps({"plugins": {
        "whales@whales": [{"scope": "user", "version": "0.6.0", "installPath": str(root)}]}}))
    return root


def _wrapper(home: Path, *args, stdin: str = "{}") -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(home / ".whales" / "scripts" / "whales_hook.py"), *args],
                          input=stdin, capture_output=True, text=True, env=_env(home), timeout=30)


class TestWrapper:
    def test_carries_the_marker_the_updater_looks_for(self):
        assert MARKER in WRAPPER_SRC.read_text()

    def test_passes_arguments_and_input_to_the_capture_hook_and_always_exits_0(self, home):
        r = _wrapper(home, "--event", "UserPromptSubmit", "--source", "cursor_hook", stdin='{"a":1}')
        assert r.returncode == 0  # the capture stub exits 3
        assert r.stdout.strip() == 'capture --event UserPromptSubmit --source cursor_hook stdin={"a":1}'

    def test_session_start_uses_the_installed_plugins_updater(self, home):
        root = _installed_plugin(home)
        _wrapper(home, "--event", "SessionStart", "--source", "cursor_hook")
        ran = home / "updater_ran"
        assert _wait(ran.exists)
        assert ran.read_text().split()[0] == str(root / "scripts" / "whales_update.py")
        assert "--session-start --host cursor" in ran.read_text()

    def test_session_start_falls_back_to_the_copy_beside_it(self, home):
        _wrapper(home, "--event", "SessionStart", "--source", "cursor_hook")
        ran = home / "updater_ran"
        assert _wait(ran.exists)
        assert ran.read_text().split()[0] == str(home / ".whales" / "scripts" / "whales_update.py")

    def test_any_event_starts_the_update_once_due(self, home):
        """A chat left open for days still gets a release: the next event in
        it runs the new capture_hook.py from disk."""
        then = time.time() - 601  # and succeeding: no recovery download
        (home / ".whales" / "update_state.json").write_text(json.dumps({"last_check": then, "last_success": then}))
        _wrapper(home, "--event", "Stop", "--source", "cursor_hook")
        ran = home / "updater_ran"
        assert _wait(ran.exists)
        assert "--session-start --host cursor" in ran.read_text()

    def test_no_update_within_the_interval(self, home):
        (home / ".whales" / "update_state.json").write_text(json.dumps({"last_check": time.time() - 60}))
        for event in ("SessionStart", "UserPromptSubmit", "Stop"):
            _wrapper(home, "--event", event, "--source", "cursor_hook")
        time.sleep(0.5)
        assert not (home / "updater_ran").exists()

    def test_never_on_the_tool_call_hooks(self, home):
        """Run on every tool call with a 5-second timeout."""
        for event in ("PreToolUse", "DesignContext"):
            _wrapper(home, "--event", event, "--source", "cursor_hook")
        time.sleep(0.5)
        assert not (home / "updater_ran").exists()

    def test_an_unreadable_state_file_counts_as_due(self, home):
        (home / ".whales" / "update_state.json").write_text("[1, 2")
        r = _wrapper(home, "--event", "UserPromptSubmit", "--source", "cursor_hook")
        assert r.returncode == 0
        assert _wait((home / "updater_ran").exists)

    def test_install_hooks_flag_still_works(self, home):
        """What the installer called on the wrapper it wrote."""
        scripts = home / ".whales" / "scripts"
        shutil.copy(PLUGIN / "scripts" / "whales_update.py", scripts / "whales_update.py")
        spec = home / "spec.json"
        spec.write_text((PLUGIN / "cursor" / "hooks.json").read_text())
        r = _wrapper(home, "--whales-install-hooks", str(spec))
        assert (r.returncode, r.stdout.strip()) == (0, "plugin")
        assert (home / ".cursor" / "hooks.json").exists()


class TestLeavingTheOldWrapper:
    """The installer's old wrapper updates capture_hook.py once a day but
    never itself. The new capture hook starts the updater that replaces it."""

    @pytest.fixture
    def old_machine(self, tmp_path) -> Path:
        scripts = tmp_path / ".whales" / "scripts"
        scripts.mkdir(parents=True)
        (scripts / "whales_hook.py").write_text("#!/usr/bin/env python3\n# whales' Cursor hook (old)\n")
        shutil.copy(HOOK_SRC, scripts / "capture_hook.py")
        (scripts / "whales_update.py").write_text(STUB_UPDATER.format(python=sys.executable))
        return tmp_path

    def _capture(self, home: Path, event: str):
        return subprocess.run(
            [sys.executable, str(home / ".whales" / "scripts" / "capture_hook.py"),
             "--event", event, "--source", "cursor_hook"],
            input="{}", capture_output=True, text=True, env=_env(home), timeout=30)

    def test_session_start_starts_the_updater(self, old_machine):
        r = self._capture(old_machine, "SessionStart")
        assert r.returncode == 0
        assert _wait((old_machine / "updater_ran").exists)
        assert "--session-start --host cursor" in (old_machine / "updater_ran").read_text()

    def test_prefers_the_installed_plugins_updater(self, old_machine):
        root = _installed_plugin(old_machine)
        self._capture(old_machine, "SessionStart")
        assert _wait((old_machine / "updater_ran").exists)
        assert (old_machine / "updater_ran").read_text().startswith(str(root))

    def test_nothing_once_the_wrapper_is_the_new_one(self, old_machine):
        (old_machine / ".whales" / "scripts" / "whales_hook.py").write_text(f"# {MARKER}\n")
        self._capture(old_machine, "SessionStart")
        time.sleep(0.5)
        assert not (old_machine / "updater_ran").exists()

    def test_nothing_on_other_events(self, old_machine):
        self._capture(old_machine, "Stop")
        time.sleep(0.5)
        assert not (old_machine / "updater_ran").exists()

    def test_nothing_when_run_from_the_plugin(self, tmp_path):
        """Only the copy the old wrapper runs (capture_hook.py) does this."""
        r = subprocess.run([sys.executable, str(HOOK_SRC), "--event", "SessionStart", "--source", "cursor_hook"],
                           input="{}", capture_output=True, text=True, env=_env(tmp_path), timeout=30)
        assert r.returncode == 0
        assert not (tmp_path / "updater_ran").exists()


class TestRestartNotice:
    def _cache(self, home: Path, version: str) -> Path:
        root = home / ".claude" / "plugins" / "cache" / "whales" / "whales" / version
        shutil.copytree(PLUGIN, root, ignore=shutil.ignore_patterns("tests", "__pycache__"))
        manifest = root / ".claude-plugin" / "plugin.json"
        data = json.loads(manifest.read_text())
        data["version"] = version
        manifest.write_text(json.dumps(data))
        return root

    def _prompt(self, home: Path, root: Path, session: str = "s1", event: str = "UserPromptSubmit") -> str:
        r = subprocess.run([sys.executable, str(root / "scripts" / "whales_hook.py"),
                            "--event", event],
                           input=json.dumps({"session_id": session, "prompt": "hi"}),
                           capture_output=True, text=True, env=_env(home), timeout=30)
        return r.stdout.strip()

    def _install(self, home: Path, root: Path, version: str):
        (home / ".claude" / "plugins" / "installed_plugins.json").write_text(json.dumps({"plugins": {
            "whales@whales": [{"scope": "user", "version": version, "installPath": str(root)}]}}))

    def test_tells_the_designer_once_per_session(self, tmp_path):
        running = self._cache(tmp_path, "0.6.0")
        self._install(tmp_path, self._cache(tmp_path, "0.6.1"), "0.6.1")
        out = json.loads(self._prompt(tmp_path, running))
        assert out["systemMessage"].startswith("whales 0.6.1 is installed (this session is on 0.6.0). Run /reload-plugins")
        assert self._prompt(tmp_path, running) == ""
        assert self._prompt(tmp_path, running, session="s2") != ""

    def test_nothing_when_running_the_installed_version(self, tmp_path):
        root = self._cache(tmp_path, "0.6.0")
        self._install(tmp_path, root, "0.6.0")
        assert self._prompt(tmp_path, root) == ""

    def test_nothing_for_a_plugin_dir_session(self, tmp_path):
        self._install(tmp_path, self._cache(tmp_path, "0.6.1"), "0.6.1")
        assert self._prompt(tmp_path, PLUGIN) == ""

    def test_also_at_the_end_of_a_turn(self, tmp_path):
        """An update the prompt started usually lands while the agent works."""
        running = self._cache(tmp_path, "0.6.0")
        self._install(tmp_path, self._cache(tmp_path, "0.6.1"), "0.6.1")
        out = json.loads(self._prompt(tmp_path, running, event="Stop"))
        assert "/reload-plugins" in out["systemMessage"]
        assert self._prompt(tmp_path, running) == ""


class TestClaudeCodeCheckEveryTurn:
    """A Claude Code session open for days: each turn starts the updater once
    its last check is older than the interval, using the INSTALLED plugin's
    updater (the running copy may be the old one)."""

    def _hook(self, home: Path, root: Path, event: str):
        return subprocess.run([sys.executable, str(root / "scripts" / "whales_hook.py"), "--event", event],
                              input=json.dumps({"session_id": "s1"}), capture_output=True, text=True,
                              env=_env(home), timeout=30)

    @pytest.fixture
    def machine(self, tmp_path):
        running = TestRestartNotice()._cache(tmp_path, "0.5.1")
        installed = _installed_plugin(tmp_path)  # 0.6.0, with the stub updater
        return tmp_path, running, installed

    @pytest.mark.parametrize("event", ["UserPromptSubmit", "Stop"])
    def test_starts_the_installed_updater_when_due(self, machine, event):
        home, running, installed = machine
        (home / ".whales").mkdir()
        (home / ".whales" / "update_state.json").write_text(json.dumps({"last_check": time.time() - 601}))
        assert self._hook(home, running, event).returncode == 0
        ran = home / "updater_ran"
        assert _wait(ran.exists)
        assert ran.read_text().split()[0] == str(installed / "scripts" / "whales_update.py")
        assert "--session-start --host claude-code" in ran.read_text()

    def test_nothing_within_the_interval(self, machine):
        home, running, _ = machine
        (home / ".whales").mkdir()
        (home / ".whales" / "update_state.json").write_text(json.dumps({"last_check": time.time() - 60}))
        self._hook(home, running, "UserPromptSubmit")
        time.sleep(0.5)
        assert not (home / "updater_ran").exists()

    @pytest.mark.parametrize("event", ["PreToolUse", "PostToolUse", "PreCompact"])
    def test_nothing_on_other_events(self, machine, event):
        home, running, _ = machine
        self._hook(home, running, event)
        time.sleep(0.5)
        assert not (home / "updater_ran").exists()


class TestRecoveringFromABrokenUpdater:
    """A Cursor-only machine has one updater, the copy in ~/.whales. If a
    release breaks it, it cannot update itself, so the wrapper fetches a
    fresh one once it has gone days without succeeding."""

    @pytest.fixture
    def server(self):
        import http.server
        import threading
        body = {"text": STUB_UPDATER.format(python=sys.executable).replace("updater_ran", "fresh_updater_ran")}

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                ok = self.path == "/scripts/whales_update.py"
                self.send_response(200 if ok else 404)
                self.end_headers()
                self.wfile.write(body["text"].encode() if ok else b"")

            def log_message(self, *a):
                pass

        srv = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        yield f"http://127.0.0.1:{srv.server_address[1]}"
        srv.shutdown()

    def _state(self, home: Path, **state):
        (home / ".whales" / "update_state.json").write_text(json.dumps(state))

    def test_fetches_a_fresh_updater_after_days_of_failures(self, home, server):
        (home / ".whales" / "plugin_source").write_text(server)
        now = time.time()
        self._state(home, last_check=now - 601, last_success=now - 4 * 86400)
        _wrapper(home, "--event", "SessionStart", "--source", "cursor_hook")
        assert _wait((home / "fresh_updater_ran").exists)
        state = json.loads((home / ".whales" / "update_state.json").read_text())
        assert state["last_recover"] == pytest.approx(now, abs=30)

    def test_leaves_a_working_updater_alone(self, home, server):
        (home / ".whales" / "plugin_source").write_text(server)
        now = time.time()
        self._state(home, last_check=now - 601, last_success=now - 3600)
        _wrapper(home, "--event", "SessionStart", "--source", "cursor_hook")
        assert _wait((home / "updater_ran").exists)
        assert not (home / "fresh_updater_ran").exists()

    def test_never_for_the_installed_plugins_updater(self, home, server):
        """Claude Code machines are fixed by the plugin update itself."""
        _installed_plugin(home)
        (home / ".whales" / "plugin_source").write_text(server)
        now = time.time()
        self._state(home, last_check=now - 601, last_success=now - 4 * 86400)
        _wrapper(home, "--event", "SessionStart", "--source", "cursor_hook")
        assert _wait((home / "updater_ran").exists)
        assert not (home / "fresh_updater_ran").exists()


def test_old_wrapper_with_no_updater_downloads_it_in_the_background(tmp_path):
    """A Cursor-only machine on the old wrapper has no updater anywhere: the
    capture hook downloads it in a detached child, so the hook still returns
    at once."""
    import http.server
    import threading

    scripts = tmp_path / ".whales" / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "whales_hook.py").write_text("# the installer's old wrapper\n")
    shutil.copy(HOOK_SRC, scripts / "capture_hook.py")
    body = STUB_UPDATER.format(python=sys.executable).encode()

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            time.sleep(1)  # slower than the hook may take
            self.send_response(200)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        env = dict(_env(tmp_path), WHALES_UPDATER_URL=f"http://127.0.0.1:{srv.server_address[1]}/u.py")
        started = time.time()
        r = subprocess.run([sys.executable, str(scripts / "capture_hook.py"), "--event", "SessionStart",
                            "--source", "cursor_hook"], input="{}", capture_output=True, text=True,
                           env=env, timeout=30)
        assert r.returncode == 0
        assert time.time() - started < 1
        assert _wait((tmp_path / "updater_ran").exists)
        assert (scripts / "whales_update.py").read_bytes() == body
        assert "--session-start --host cursor" in (tmp_path / "updater_ran").read_text()
    finally:
        srv.shutdown()
