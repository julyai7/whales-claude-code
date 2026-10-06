"""The `whales` command, run the way designers get it: from ~/.whales/scripts
beside the updater, through the launcher."""
from __future__ import annotations

import http.server
import json
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from test_whales_update import Machine, _cursor_machine  # noqa: F401 — shared fixtures

PLUGIN = Path(__file__).resolve().parents[1]


@pytest.fixture
def m(tmp_path) -> Machine:
    machine = Machine(tmp_path)
    machine.install_plugin("0.5.1", "0.6.0")
    # What a first update leaves behind: the CLI and the launcher.
    r = machine.updater("--run")
    assert r.returncode == 0, r.stdout
    return machine


def whales(m: Machine, *args) -> subprocess.CompletedProcess:
    return subprocess.run([str(m.whales / "bin" / "whales"), *args], capture_output=True, text=True,
                          env=m.env(), timeout=60)


def test_help_lists_every_command(m):
    out = whales(m).stdout
    for command in ("status", "update", "doctor", "logs", "version", "install", "uninstall"):
        assert f"whales {command}" in out


def test_unknown_command(m):
    r = whales(m, "frobnicate")
    assert r.returncode == 2
    assert "Unknown command: frobnicate" in r.stdout


def test_status_after_an_update(m):
    out = whales(m, "status").stdout
    assert "plugin 0.6.0" in out
    assert "updated 0.5.1 → 0.6.0" in out
    assert "Restart Claude Code" in out


def test_status_says_when_a_release_is_waiting(m):
    fake = json.loads((m.cfg / "fake.json").read_text())
    loc = m.cfg / "plugins" / "marketplaces" / "whales" / ".claude-plugin" / "marketplace.json"
    loc.write_text(json.dumps({"plugins": [{"name": "whales", "version": "0.6.1"}]}))
    assert "0.6.1 is out" in whales(m, "status").stdout
    assert fake  # the stub's state is untouched by status


def test_update_now(m):
    cache = m.cfg / "plugins" / "cache" / "whales" / "whales"
    shutil.copytree(cache / "0.6.0", cache / "0.6.1")
    fake = json.loads((m.cfg / "fake.json").read_text())
    fake["published"] = "0.6.1"
    fake["dirs"]["0.6.1"] = str(cache / "0.6.1")
    (m.cfg / "fake.json").write_text(json.dumps(fake))
    r = whales(m, "update")
    assert r.returncode == 0, r.stdout
    assert "Updated whales from 0.6.0 to 0.6.1" in r.stdout
    assert "plugin update" in whales(m, "logs").stdout


def test_doctor_finds_and_fixes(m):
    (m.whales / "token").write_text("t")
    (m.whales / "scripts" / "critique_source.py").unlink()
    r = whales(m, "doctor")
    assert r.returncode == 1
    assert "✗ the critique upload helper" in r.stdout
    r = whales(m, "doctor", "--fix")
    assert (m.whales / "scripts" / "critique_source.py").exists()
    assert "✓ the critique upload helper" in r.stdout


def test_doctor_checks_cursor(m):
    _cursor_machine(m)
    r = whales(m, "doctor")
    assert "✗ Cursor's whales hook is the current one" in r.stdout
    assert "✗ Cursor tells whales which version it runs (nothing)" in r.stdout
    whales(m, "doctor", "--fix")
    r = whales(m, "doctor")
    assert "✓ Cursor's whales hook is the current one" in r.stdout
    assert "✓ Cursor tells whales which version it runs (0.6.0)" in r.stdout


def _wired_cursor(m: Machine, gateway: str = "https://mcp.gojuly.ai") -> None:
    """Cursor set up the way the installer leaves it: its entry carries the
    saved token and points at the gateway the installer recorded."""
    _cursor_machine(m)
    (m.whales / "token").write_text("t")
    (m.whales / "gateway").write_text(gateway + "\n")
    mcp = m.home / ".cursor" / "mcp.json"
    cfg = json.loads(mcp.read_text())
    cfg["mcpServers"]["whales"]["url"] = gateway + "/mcp"
    mcp.write_text(json.dumps(cfg))
    # What lets `--fix` add Claude Code's allow rules, so only Cursor is in question.
    (m.cfg / "settings.json").write_text(json.dumps(
        {"extraKnownMarketplaces": {"whales": {"source": {"source": "github"}}}}))
    r = whales(m, "doctor", "--fix")
    assert r.returncode == 0, r.stdout


def _drop_claude_code_plugin(m: Machine) -> None:
    """Claude Code stays on the machine (`claude` resolves); whales is not in it."""
    (m.cfg / "plugins" / "installed_plugins.json").write_text(json.dumps({"version": 2, "plugins": {}}))


def _cursor_says(m: Machine, **changes) -> None:
    mcp = m.home / ".cursor" / "mcp.json"
    cfg = json.loads(mcp.read_text())
    entry = cfg["mcpServers"]["whales"]
    if "url" in changes:
        entry["url"] = changes["url"]
    if "token" in changes:
        entry["headers"]["Authorization"] = "Bearer " + changes["token"]
    mcp.write_text(json.dumps(cfg))


def test_doctor_cursor_only_with_claude_code_around_is_not_a_failure(m):
    _wired_cursor(m)
    _drop_claude_code_plugin(m)
    r = whales(m, "doctor")
    assert r.returncode == 0, r.stdout
    assert "! Claude Code is on this Mac but not connected to whales (`whales install claude` adds it)" in r.stdout
    assert "✗" not in r.stdout
    assert "Everything looks right." in r.stdout
    assert "quit Cursor (Cmd+Q) and reopen it" in r.stdout


def test_doctor_without_any_host_still_fails_on_the_missing_plugin(m):
    (m.whales / "token").write_text("t")
    _drop_claude_code_plugin(m)
    r = whales(m, "doctor", "--fix")
    assert r.returncode == 1
    assert "✗ the Claude Code plugin is installed" in r.stdout
    assert "• the Claude Code plugin is installed: `whales install claude` adds it" in r.stdout
    assert "needs a person" not in r.stdout


def test_doctor_catches_a_cursor_entry_that_drifted(m):
    _wired_cursor(m)
    staging = "https://whales-mcp-gateway-staging-2a5f90a8c6d7.herokuapp.com/mcp"
    _cursor_says(m, url=staging, token="an-older-token")
    r = whales(m, "doctor", "--fix")
    assert r.returncode == 1
    assert f"✗ Cursor points at the whales server ({staging})" in r.stdout
    assert "✗ Cursor sends the token saved in ~/.whales/token" in r.stdout
    assert "• Cursor sends the token saved in ~/.whales/token: `whales install cursor` rewrites" in r.stdout
    assert "and https://mcp.gojuly.ai/mcp" in r.stdout


@pytest.fixture
def gateway():
    """An MCP server that accepts one token, and remembers who knocked."""
    seen = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            seen.append((self.path, self.headers["Authorization"], body["params"]["clientInfo"]["name"]))
            self.send_response(200 if self.headers["Authorization"] == "Bearer t" else 401)
            self.end_headers()

        def log_message(self, *a):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}", seen
    server.shutdown()


def test_doctor_online_tests_the_token_cursor_sends(m, gateway):
    url, seen = gateway
    _wired_cursor(m, gateway=url)
    assert not seen  # plain doctor never calls the server
    r = whales(m, "doctor", "--online")
    assert r.returncode == 0, r.stdout
    assert "✓ the whales server accepts Cursor's token" in r.stdout
    assert seen == [("/mcp", "Bearer t", "whales-doctor")]

    _cursor_says(m, token="revoked")
    (m.whales / "token").write_text("revoked")
    r = whales(m, "doctor", "--online")
    assert r.returncode == 1
    assert "✗ the whales server accepts Cursor's token (it answered HTTP 401)" in r.stdout


def test_doctor_online_unreachable_is_a_note(m):
    _wired_cursor(m, gateway="http://127.0.0.1:9")
    r = whales(m, "doctor", "--online")
    assert r.returncode == 0, r.stdout
    assert "! could not reach the whales server at http://127.0.0.1:9/mcp" in r.stdout


def test_install_needs_a_token(m):
    r = whales(m, "install", "cursor")
    assert r.returncode == 1
    assert "No whales token" in r.stdout


def test_install_refuses_an_installer_without_host_support(m, tmp_path):
    """An older installer would treat `--only cursor` as a full install, and
    `--uninstall cursor` as removing everything."""
    (m.whales / "token").write_text("t")
    site = tmp_path / "site"
    site.mkdir()
    (site / "install.sh").write_text("#!/bin/bash\necho old installer\n")
    (m.whales / "app_url").write_text("file://" + str(site))
    r = whales(m, "install", "cursor")
    assert r.returncode == 1
    assert "does not support this yet" in r.stdout
    r = whales(m, "uninstall", "cursor")
    assert "does not support this yet" in r.stdout


def test_install_runs_the_installer_for_one_host(m, tmp_path):
    (m.whales / "token").write_text("t")
    site = tmp_path / "site"
    site.mkdir()
    (site / "install.sh").write_text(
        '#!/bin/bash\n# whales-installer-api: 2\necho "installer args: $*"\n')
    (m.whales / "app_url").write_text("file://" + str(site))
    r = whales(m, "install", "cursor")
    assert "installer args: --only cursor" in r.stdout
    r = whales(m, "uninstall", "claude")
    assert "installer args: --uninstall claude" in r.stdout


def test_uninstall_everything_asks_first(m):
    r = subprocess.run([str(m.whales / "bin" / "whales"), "uninstall"], capture_output=True, text=True,
                       env=m.env(), timeout=60, stdin=subprocess.DEVNULL, start_new_session=True)
    assert r.returncode == 1
    assert "Nothing removed." in r.stdout
