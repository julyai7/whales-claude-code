"""Tests for the turn diff: what a turn changed in UI files, from the working
tree itself, so edits made through Bash are seen as well as Write/Edit.

Real git repos in tmp dirs: the point is that it works on a designer's repo
(worktrees included) and never writes into it.
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parents[1] / "scripts" / "whales_hook.py"


def _load():
    spec = importlib.util.spec_from_file_location("whales_hook_turn_diff", HOOK)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


wh = _load()


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


@pytest.fixture()
def repo(tmp_path, monkeypatch):
    monkeypatch.setattr(wh, "TURNS_DIR", str(tmp_path / "whales_turns"))
    monkeypatch.delenv("WHALES_TURN_DIFF", raising=False)
    root = tmp_path / "app"
    (root / "src").mkdir(parents=True)
    git(root, "init", "-q")
    git(root, "config", "user.email", "t@example.com")
    git(root, "config", "user.name", "t")
    (root / "src" / "Panel.tsx").write_text('export const P = () => <div class="duration-200" />\n')
    (root / "README.md").write_text("readme\n")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "init")
    return root


def loose_objects(root):
    return sorted(p.name for p in (root / ".git" / "objects").rglob("*") if p.is_file())


def turn(root, session="s1"):
    event = {"cwd": str(root)}
    start = wh.turn_diff(event, "UserPromptSubmit", session)
    return event, start


def test_reports_every_ui_change_including_untracked(repo):
    event, start = turn(repo)
    assert start is None  # nothing changed between turns yet
    # As a Bash heredoc would: no Write/Edit tool involved.
    (repo / "src" / "Panel.tsx").write_text('export const P = () => <div class="duration-300" />\n')
    (repo / "src" / "motion.css").write_text(".x { transition: opacity 280ms ease-out; }\n")
    (repo / "README.md").write_text("changed, but not UI\n")
    key, diff = wh.turn_diff(event, "Stop", "s1")
    assert key == "whales_turn_diff"
    assert sorted(f["path"] for f in diff["files"]) == ["src/Panel.tsx", "src/motion.css"]
    assert "duration-300" in diff["patch"] and "280ms ease-out" in diff["patch"]


def test_never_writes_into_the_designers_repo(repo):
    before_objects = loose_objects(repo)
    before_status = git(repo, "status", "--porcelain")
    (repo / "src" / "New.tsx").write_text("export const N = 1\n")
    before_status = git(repo, "status", "--porcelain")
    event, _ = turn(repo)
    (repo / "src" / "New.tsx").write_text("export const N = 2\n")
    wh.turn_diff(event, "Stop", "s1")
    assert loose_objects(repo) == before_objects
    assert git(repo, "status", "--porcelain") == before_status
    assert git(repo, "diff", "--cached") == ""


def test_secrets_and_lockfiles_are_not_snapshotted(repo):
    event, _ = turn(repo)
    (repo / ".env").write_text("API_KEY=abc\n")
    (repo / "package-lock.json").write_text("{}\n")
    (repo / "src" / "a.ts").write_text("export const a = 1\n")
    _, diff = wh.turn_diff(event, "Stop", "s1")
    assert [f["path"] for f in diff["files"]] == ["src/a.ts"]
    assert "API_KEY" not in diff["patch"]


def test_a_turn_with_no_ui_change_reports_empty_files(repo):
    event, _ = turn(repo)
    _, diff = wh.turn_diff(event, "Stop", "s1")
    assert diff["files"] == []


def test_designer_edits_between_turns_ride_on_the_next_prompt(repo):
    event, _ = turn(repo)
    (repo / "src" / "Panel.tsx").write_text("by the host\n")
    wh.turn_diff(event, "Stop", "s1")
    (repo / "src" / "Panel.tsx").write_text("by the designer\n")
    key, between = wh.turn_diff(event, "UserPromptSubmit", "s1")
    assert key == "whales_between_turns_diff"
    assert "by the designer" in between["patch"]
    # And the next turn's diff starts from there, not from the old Stop.
    (repo / "src" / "Panel.tsx").write_text("host again\n")
    _, diff = wh.turn_diff(event, "Stop", "s1")
    assert "-by the designer" in diff["patch"] and "+host again" in diff["patch"]


def test_stop_without_a_start_snapshot_says_so(repo):
    key, diff = wh.turn_diff({"cwd": str(repo)}, "Stop", "fresh")
    assert key == "whales_turn_diff" and diff["error"] == "no start snapshot"
    # Its end becomes the next turn's fallback start.
    (repo / "src" / "Panel.tsx").write_text("later\n")
    _, diff = wh.turn_diff({"cwd": str(repo)}, "Stop", "fresh")
    assert [f["path"] for f in diff["files"]] == ["src/Panel.tsx"]


def test_works_in_a_worktree(repo, tmp_path):
    wt = tmp_path / "wt"
    git(repo, "worktree", "add", "-q", str(wt))
    assert (wt / ".git").is_file()
    event, _ = turn(wt, "s-wt")
    (wt / "src" / "Panel.tsx").write_text("in the worktree\n")
    _, diff = wh.turn_diff(event, "Stop", "s-wt")
    assert [f["path"] for f in diff["files"]] == ["src/Panel.tsx"]


def test_outside_a_repo_and_switched_off_it_does_nothing(repo, tmp_path, monkeypatch):
    plain = tmp_path / "plain"
    plain.mkdir()
    assert wh.turn_diff({"cwd": str(plain)}, "Stop", "s1") is None
    assert wh.turn_diff({}, "Stop", "s1") is None
    monkeypatch.setenv("WHALES_TURN_DIFF", "off")
    assert wh.turn_diff({"cwd": str(repo)}, "Stop", "s1") is None


def test_other_events_are_ignored(repo):
    assert wh.turn_diff({"cwd": str(repo)}, "PostToolUse", "s1") is None


def test_a_git_failure_never_raises(repo, monkeypatch):
    def broken(*a, **k):
        raise subprocess.TimeoutExpired("git", 3)
    monkeypatch.setattr(wh.subprocess, "run", broken)
    assert wh.turn_diff({"cwd": str(repo)}, "Stop", "s1") is None
