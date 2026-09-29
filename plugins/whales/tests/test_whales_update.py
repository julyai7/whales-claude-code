"""Tests for whales_update.py, the updater that keeps a designer from ever
re-running the installer.

Every test runs the real script as a subprocess with HOME and
CLAUDE_CONFIG_DIR in a tmpdir. `claude` is a stub that does what the real
`plugin marketplace update` / `plugin update` do to Claude Code's files, and
records how it was called.
"""
from __future__ import annotations

import http.server
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

PLUGIN = Path(__file__).resolve().parents[1]
UPDATER = PLUGIN / "scripts" / "whales_update.py"
MARKER = "WHALES_CURSOR_WRAPPER = 2"

FAKE_CLAUDE = """#!{python}
import json, os, sys
cfg = os.environ["CLAUDE_CONFIG_DIR"]
with open(os.path.join(cfg, "claude_calls.jsonl"), "a") as fh:
    fh.write(json.dumps({{"argv": sys.argv[1:],
                          "env": sorted(k for k in os.environ if k.startswith("CLAUDE"))}}) + "\\n")
fake = json.load(open(os.path.join(cfg, "fake.json")))
plugins = os.path.join(cfg, "plugins")
if sys.argv[1:4] == ["plugin", "marketplace", "update"]:
    loc = json.load(open(os.path.join(plugins, "known_marketplaces.json")))["whales"]["installLocation"]
    os.makedirs(os.path.join(loc, ".claude-plugin"), exist_ok=True)
    json.dump({{"plugins": [{{"name": "whales", "version": fake["published"]}}]}},
              open(os.path.join(loc, ".claude-plugin", "marketplace.json"), "w"))
elif sys.argv[1:3] == ["plugin", "update"] and not fake.get("update_fails"):
    path = os.path.join(plugins, "installed_plugins.json")
    data = json.load(open(path))
    data["plugins"]["whales@whales"][0].update(
        version=fake["published"], installPath=fake["dirs"][fake["published"]])
    json.dump(data, open(path, "w"))
    print("updated")
"""


def _plugin_copy(dest: Path, version: str) -> Path:
    """A copy of this plugin as Claude Code would cache it, at ``version``."""
    shutil.copytree(PLUGIN, dest, ignore=shutil.ignore_patterns("tests", "__pycache__"))
    manifest = dest / ".claude-plugin" / "plugin.json"
    data = json.loads(manifest.read_text())
    data["version"] = version
    manifest.write_text(json.dumps(data))
    return dest


class Machine:
    def __init__(self, root: Path):
        self.root = root
        self.home = root / "home"
        self.cfg = self.home / ".claude"
        self.bin = root / "bin"
        self.whales = self.home / ".whales"
        for d in (self.home, self.cfg / "plugins", self.bin):
            d.mkdir(parents=True, exist_ok=True)

    # -- Claude Code ------------------------------------------------------
    def install_plugin(self, installed: str, published: str, update_fails: bool = False):
        cache = self.cfg / "plugins" / "cache" / "whales" / "whales"
        dirs = {v: str(_plugin_copy(cache / v, v)) for v in {installed, published}}
        (self.cfg / "fake.json").write_text(json.dumps(
            {"published": published, "dirs": dirs, "update_fails": update_fails}))
        (self.cfg / "plugins" / "installed_plugins.json").write_text(json.dumps({"version": 2, "plugins": {
            "whales@whales": [{"scope": "user", "version": installed, "installPath": dirs[installed]}]}}))
        (self.cfg / "plugins" / "known_marketplaces.json").write_text(json.dumps({"whales": {
            "source": {"source": "github", "repo": "julyai7/whales-claude-code"},
            "installLocation": str(self.cfg / "plugins" / "marketplaces" / "whales")}}))
        claude = self.bin / "claude"
        claude.write_text(FAKE_CLAUDE.format(python=sys.executable))
        claude.chmod(0o755)

    def claude_calls(self) -> list[dict]:
        path = self.cfg / "claude_calls.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def installed(self) -> str:
        data = json.loads((self.cfg / "plugins" / "installed_plugins.json").read_text())
        return data["plugins"]["whales@whales"][0]["version"]

    # -- running ----------------------------------------------------------
    def env(self, **extra) -> dict:
        env = {
            "HOME": str(self.home),
            "CLAUDE_CONFIG_DIR": str(self.cfg),
            "PATH": f"{self.bin}:/usr/bin:/bin",
        }
        env.update(extra)
        return env

    def updater(self, *args, **extra_env) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, str(UPDATER), *args], capture_output=True,
                              text=True, env=self.env(**extra_env), timeout=60)

    def state(self) -> dict:
        path = self.whales / "update_state.json"
        return json.loads(path.read_text()) if path.exists() else {}


@pytest.fixture
def m(tmp_path) -> Machine:
    return Machine(tmp_path)


class TestHookEntry:
    def test_no_arguments_does_nothing(self, m):
        """Cursor runs Claude Code plugins' hooks.json itself, without args."""
        r = m.updater()
        assert (r.returncode, r.stdout, r.stderr) == (0, "", "")
        assert not m.whales.exists()

    def test_session_start_prints_nothing_and_checks_once_per_interval(self, m):
        m.install_plugin("0.6.0", "0.6.0")
        r = m.updater("--session-start", "--host", "claude-code")
        assert (r.returncode, r.stdout) == (0, "")
        first = m.state()["last_check"]
        _wait(lambda: m.state().get("last_run"))
        r = m.updater("--session-start", "--host", "claude-code")
        assert r.stdout == ""
        assert m.state()["last_check"] == pytest.approx(first, abs=5)
        assert len([c for c in m.claude_calls() if c["argv"][:3] == ["plugin", "marketplace", "update"]]) == 1

    def test_the_developer_off_switch(self, m):
        m.install_plugin("0.5.1", "0.6.0")
        m.whales.mkdir()
        (m.whales / "auto_update_off").touch()
        m.updater("--session-start", "--host", "claude-code")
        time.sleep(1)
        assert m.claude_calls() == []


class TestClaudeCode:
    def test_updates_the_plugin_then_the_files_outside_it(self, m):
        m.install_plugin("0.5.1", "0.6.0")
        r = m.updater("--run", "--host", "claude-code")
        assert r.returncode == 0, r.stdout
        assert m.installed() == "0.6.0"
        scripts = m.whales / "scripts"
        for name in ("whales_update.py", "whales_cli.py", "critique_source.py"):
            assert os.access(scripts / name, os.X_OK), name
        assert (scripts / "capture_hook.version").read_text() == "0.6.0"
        assert os.access(m.whales / "bin" / "whales", os.X_OK)
        assert m.state()["last_run"]["result"] == "updated"
        assert (m.state()["last_run"]["before"], m.state()["last_run"]["after"]) == ("0.5.1", "0.6.0")

    def test_no_cursor_files_for_a_claude_code_only_machine(self, m):
        m.install_plugin("0.6.0", "0.6.0")
        m.updater("--run", "--host", "claude-code")
        assert not (m.whales / "scripts" / "whales_hook.py").exists()
        assert not (m.whales / "scripts" / "capture_hook.py").exists()

    def test_the_nested_claude_runs_outside_the_session(self, m):
        """A hook runs inside a Claude Code session; a `claude` that inherits
        its markers runs as that session's child."""
        m.install_plugin("0.5.1", "0.6.0")
        m.updater("--run", "--host", "claude-code", CLAUDECODE="1",
                  CLAUDE_CODE_ENTRYPOINT="cli", CLAUDE_CODE_SESSION_ID="abc",
                  CLAUDE_PLUGIN_ROOT="/somewhere")
        for call in m.claude_calls():
            assert call["env"] == ["CLAUDE_CONFIG_DIR"], call

    def test_rolls_back_when_the_published_version_goes_down(self, m):
        m.install_plugin("0.6.0", "0.5.9")
        m.updater("--run", "--host", "claude-code")
        assert m.installed() == "0.5.9"

    def test_up_to_date_runs_no_plugin_update(self, m):
        m.install_plugin("0.6.0", "0.6.0")
        m.updater("--run", "--host", "claude-code")
        assert [c["argv"][:2] for c in m.claude_calls()] == [["plugin", "marketplace"]]
        assert m.state()["last_run"]["result"] == "updated"  # first sync of the files
        m.updater("--run", "--host", "claude-code")
        assert m.state()["last_run"]["result"] == "current"

    def test_a_failed_plugin_update_is_reported(self, m):
        m.install_plugin("0.5.1", "0.6.0", update_fails=True)
        r = m.updater("--run", "--host", "claude-code")
        assert r.returncode == 1
        assert m.state()["last_run"]["result"] == "failed"
        assert "still 0.5.1" in m.state()["last_run"]["error"]

    def test_files_come_from_the_installed_plugin_not_the_running_one(self, m, tmp_path):
        """A --plugin-dir session runs this file from a checkout; what it
        copies must still be the released plugin."""
        m.install_plugin("0.6.0", "0.6.0")
        checkout = _plugin_copy(tmp_path / "checkout", "9.9.9")
        (checkout / "scripts" / "whales_cli.py").write_text("# unreleased\n")
        subprocess.run([sys.executable, str(checkout / "scripts" / "whales_update.py"), "--run"],
                       env=m.env(), capture_output=True, timeout=60)
        assert "# unreleased" not in (m.whales / "scripts" / "whales_cli.py").read_text()
        assert (m.whales / "scripts" / "capture_hook.version").read_text() == "0.6.0"


class TestLock:
    def test_a_running_update_is_left_alone(self, m):
        m.install_plugin("0.5.1", "0.6.0")
        m.whales.mkdir()
        (m.whales / "update.lock").write_text("123")
        r = m.updater("--run")
        assert "already running" in r.stdout
        assert m.claude_calls() == []

    def test_a_lock_left_by_a_killed_run_is_cleared(self, m):
        m.install_plugin("0.5.1", "0.6.0")
        m.whales.mkdir()
        lock = m.whales / "update.lock"
        lock.write_text("123")
        old = time.time() - 3600
        os.utime(lock, (old, old))
        m.updater("--run")
        assert m.installed() == "0.6.0"
        assert not lock.exists()


def _cursor_machine(m: Machine, old_wrapper: bool = True, our_hooks: bool = True):
    cursor = m.home / ".cursor"
    cursor.mkdir()
    (cursor / "mcp.json").write_text(json.dumps({"mcpServers": {
        "whales": {"url": "https://mcp.gojuly.ai/mcp", "headers": {"Authorization": "Bearer t"}},
        "someone-else": {"url": "https://other/mcp"}}}))
    wrapper = m.whales / "scripts" / "whales_hook.py"
    wrapper.parent.mkdir(parents=True, exist_ok=True)
    wrapper.write_text("#!/usr/bin/env python3\n# the installer's old wrapper\n" if old_wrapper
                       else f"# {MARKER}\n")
    hooks = {"stop": [{"command": "someone-else.sh"}]}
    if our_hooks:
        hooks["stop"].append({"command": f"{wrapper} --event Stop --source cursor_hook"})
    (cursor / "hooks.json").write_text(json.dumps({"version": 1, "hooks": hooks}))
    return cursor


class TestCursor:
    def test_replaces_the_old_wrapper_and_updates_hooks_and_header(self, m):
        m.install_plugin("0.6.0", "0.6.0")
        cursor = _cursor_machine(m)
        m.updater("--run", "--host", "cursor")
        scripts = m.whales / "scripts"
        assert MARKER in (scripts / "whales_hook.py").read_text()
        assert os.access(scripts / "whales_hook.py", os.X_OK)
        assert (scripts / "capture_hook.py").exists()
        hooks = json.loads((cursor / "hooks.json").read_text())["hooks"]
        assert {"command": "someone-else.sh"} in hooks["stop"]
        ours = [e for e in hooks["stop"] if str(scripts / "whales_hook.py") in e["command"]]
        assert len(ours) == 1
        assert "sessionStart" in hooks
        mcp = json.loads((cursor / "mcp.json").read_text())["mcpServers"]
        assert mcp["whales"]["headers"] == {"Authorization": "Bearer t", "X-Whales-Plugin-Version": "0.6.0"}
        assert "someone-else" in mcp
        assert (cursor / "mcp.json.whales-backup").exists()

    def test_an_outdated_wrapper_is_replaced_even_at_the_same_version(self, m):
        m.install_plugin("0.6.0", "0.6.0")
        _cursor_machine(m)
        (m.whales / "scripts" / "capture_hook.version").write_text("0.6.0")
        m.updater("--run", "--host", "cursor")
        assert MARKER in (m.whales / "scripts" / "whales_hook.py").read_text()

    def test_hooks_a_designer_removed_stay_removed(self, m):
        m.install_plugin("0.6.0", "0.6.0")
        cursor = _cursor_machine(m, our_hooks=False)
        m.updater("--run", "--host", "cursor")
        hooks = json.loads((cursor / "hooks.json").read_text())["hooks"]
        assert hooks == {"stop": [{"command": "someone-else.sh"}]}

    def test_cursor_only_machine_updates_from_the_repo(self, m, plugin_server):
        base, files = plugin_server
        _cursor_machine(m)
        (m.whales / "plugin_source").write_text(base)
        r = m.updater("--run", "--host", "cursor")
        assert r.returncode == 0, r.stdout
        scripts = m.whales / "scripts"
        assert MARKER in (scripts / "whales_hook.py").read_text()
        assert (scripts / "capture_hook.version").read_text() == "0.6.0"
        # And the next release reaches it too.
        files["/.claude-plugin/plugin.json"] = json.dumps({"version": "0.6.1"})
        m.updater("--run", "--host", "cursor")
        assert (scripts / "capture_hook.version").read_text() == "0.6.1"

    def test_a_file_that_does_not_compile_is_never_installed(self, m, plugin_server):
        base, files = plugin_server
        _cursor_machine(m, old_wrapper=False)
        (m.whales / "plugin_source").write_text(base)
        files["/scripts/whales_cli.py"] = "def broken(:\n"
        r = m.updater("--run", "--host", "cursor")
        assert r.returncode == 1
        assert not (m.whales / "scripts" / "whales_cli.py").exists()
        # The version is not marked done, so the next run tries again.
        assert not (m.whales / "scripts" / "capture_hook.version").exists()


@pytest.fixture
def plugin_server():
    files = {}
    for path in PLUGIN.rglob("*"):
        if path.is_file() and "tests" not in path.parts and "__pycache__" not in path.parts:
            files["/" + str(path.relative_to(PLUGIN))] = path.read_text()
    files["/.claude-plugin/plugin.json"] = json.dumps({"version": "0.6.0"})

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            body = files.get(self.path)
            self.send_response(200 if body is not None else 404)
            self.end_headers()
            self.wfile.write((body or "").encode())

        def log_message(self, *a):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}", files
    server.shutdown()


class TestAllowRules:
    def _settings(self, m, **extra):
        cfg = {"extraKnownMarketplaces": {"whales": {"source": {"source": "github"}}}}
        cfg.update(extra)
        (m.cfg / "settings.json").write_text(json.dumps(cfg))

    def test_adds_rules_for_new_tools_but_not_ones_the_designer_denied(self, m):
        m.install_plugin("0.6.0", "0.6.0")
        denied = "mcp__plugin_whales_whales__submit_design"
        self._settings(m, permissions={"deny": [denied], "allow": ["Bash(ls)"]})
        m.updater("--run")
        perms = json.loads((m.cfg / "settings.json").read_text())["permissions"]
        assert "mcp__plugin_whales_whales__self_critique" in perms["allow"]
        assert "Bash(ls)" in perms["allow"]
        assert denied not in perms["allow"]

    def test_never_without_our_marketplace_entry(self, m):
        m.install_plugin("0.6.0", "0.6.0")
        (m.cfg / "settings.json").write_text(json.dumps({"theme": "dark"}))
        m.updater("--run")
        assert json.loads((m.cfg / "settings.json").read_text()) == {"theme": "dark"}


class TestLauncher:
    def test_links_into_local_bin(self, m):
        m.install_plugin("0.6.0", "0.6.0")
        (m.home / ".local" / "bin").mkdir(parents=True)
        m.updater("--run")
        link = m.home / ".local" / "bin" / "whales"
        assert os.readlink(link) == str(m.whales / "bin" / "whales")

    def test_never_replaces_someone_elses_whales(self, m):
        m.install_plugin("0.6.0", "0.6.0")
        (m.home / ".local" / "bin").mkdir(parents=True)
        theirs = m.home / ".local" / "bin" / "whales"
        theirs.write_text("#!/bin/sh\necho mine\n")
        m.updater("--run")
        assert theirs.read_text() == "#!/bin/sh\necho mine\n"


class TestInstallCursorHooks:
    def test_uses_the_plugin_file_when_valid(self, m, tmp_path):
        spec = tmp_path / "hooks.json"
        spec.write_text((PLUGIN / "cursor" / "hooks.json").read_text())
        (m.home / ".cursor").mkdir()
        r = m.updater("--install-cursor-hooks", str(spec))
        assert r.stdout.strip() == "plugin"
        hooks = json.loads((m.home / ".cursor" / "hooks.json").read_text())["hooks"]
        assert set(hooks) == set(json.loads(spec.read_text())["hooks"])

    def test_falls_back_to_the_built_in_list(self, m, tmp_path):
        spec = tmp_path / "hooks.json"
        spec.write_text("not json")
        r = m.updater("--install-cursor-hooks", str(spec))
        assert r.stdout.strip() == "built-in"

    def test_built_in_list_matches_the_plugin_file(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("whales_update", UPDATER)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        assert mod.BUILTIN_CURSOR_HOOKS == json.loads((PLUGIN / "cursor" / "hooks.json").read_text())["hooks"]


def _wait(predicate, timeout: float = 20.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return
        time.sleep(0.2)
    raise AssertionError("timed out")
