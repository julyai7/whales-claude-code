"""The `whales` command, run the way designers get it: from ~/.whales/scripts
beside the updater, through the launcher."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
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
