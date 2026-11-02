"""The memory snapshot: the notes Claude Code keeps about how the designer works, sent whole so whales holds them.

Edit capture alone left whales unable to rebuild Ruolan's MEMORY.md (16 edits, never a whole file) and missed notes
changed with Bash. These pin what the snapshot sends, when, and for which projects.
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parents[1] / "scripts" / "whales_hook.py"


def _load():
    spec = importlib.util.spec_from_file_location("whales_hook_memory", HOOK)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


wh = _load()
PROJECT = "-Users-ruolan-whales-whales-test"


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(wh, "CONFIG_DIR", str(tmp_path / ".whales"))
    monkeypatch.setattr(wh, "MEMORY_PROJECTS_FILE", str(tmp_path / ".whales" / "memory_projects.json"))
    monkeypatch.setattr(wh, "MEMORY_STATE_DIR", str(tmp_path / ".whales" / "memory"))
    memory = tmp_path / ".claude" / "projects" / PROJECT / "memory"
    memory.mkdir(parents=True)
    (memory / "MEMORY.md").write_text("- [Limit loud moments](limit-loud-moments.md) — keep 1-2\n")
    (memory / "limit-loud-moments.md").write_text("---\nname: limit-loud-moments\n---\nKeep 1-2.\n")
    return tmp_path


def _event(home, session="s1"):
    return {"session_id": session, "cwd": "/Users/ruolan/whales/whales_test",
            "transcript_path": str(home / ".claude" / "projects" / PROJECT / f"{session}.jsonl")}


def _bodies(home, sent=None):
    files = wh.read_memory(str(home / ".claude" / "projects" / PROJECT / "memory"))
    return [json.loads(b) for b in wh.memory_snapshot_bodies(PROJECT, files, sent or {}, "cc:s1", "2026-11-02T00:00:00Z")]


def test_the_project_is_the_folder_the_transcript_lives_in(home):
    assert wh.memory_location(_event(home)) == (PROJECT, str(home / ".claude" / "projects" / PROJECT / "memory"))
    # A desktop-app scratch workspace is a project of its own, whatever it is called.
    scratch = "-Users-ruolan-Library-Application-Support-Claude-scratch-2026-10-23-05a861"
    ev = {"transcript_path": str(home / ".claude" / "projects" / scratch / "s.jsonl")}
    assert wh.memory_location(ev)[0] == scratch
    assert wh.memory_location({"transcript_path": "/tmp/elsewhere/s.jsonl"}) == ("", "")


def test_the_first_snapshot_sends_every_note_whole_with_the_manifest(home):
    [body] = _bodies(home)
    raw = body["raw_payload"]
    assert raw["whales_event"] == "MemorySnapshot" and raw["memory_project"] == PROJECT
    assert sorted(f["name"] for f in raw["memory_files"]) == ["MEMORY.md", "limit-loud-moments.md"]
    assert set(raw["memory_manifest"]) == {"MEMORY.md", "limit-loud-moments.md"}
    assert body["session_id"] == "cc:s1" and body["source"] == "claude_code_hook"


def test_an_unchanged_folder_sends_nothing_and_a_change_sends_only_that_note(home):
    files = wh.read_memory(str(home / ".claude" / "projects" / PROJECT / "memory"))
    sent = {name: digest for name, (_t, digest, _m) in files.items()}
    assert _bodies(home, sent) == []
    note = home / ".claude" / "projects" / PROJECT / "memory" / "limit-loud-moments.md"
    note.write_text(note.read_text() + "Gopuff was still busy.\n")  # e.g. changed with Bash: no Edit to capture
    [body] = _bodies(home, sent)
    assert [f["name"] for f in body["raw_payload"]["memory_files"]] == ["limit-loud-moments.md"]
    assert set(body["raw_payload"]["memory_manifest"]) == {"MEMORY.md", "limit-loud-moments.md"}


def test_a_deleted_note_is_sent_as_a_manifest_without_it(home):
    files = wh.read_memory(str(home / ".claude" / "projects" / PROJECT / "memory"))
    sent = {name: digest for name, (_t, digest, _m) in files.items()}
    (home / ".claude" / "projects" / PROJECT / "memory" / "limit-loud-moments.md").unlink()
    [body] = _bodies(home, sent)
    assert body["raw_payload"]["memory_files"] == [] and set(body["raw_payload"]["memory_manifest"]) == {"MEMORY.md"}


def test_a_long_index_is_sent_whole_not_capped_like_an_edit(home):
    index = home / ".claude" / "projects" / PROJECT / "memory" / "MEMORY.md"
    index.write_text("- [x](x.md) — rule\n" * 3000)  # ~57 KB, past the 20,000-character edit cap
    [body] = _bodies(home)
    sent = next(f for f in body["raw_payload"]["memory_files"] if f["name"] == "MEMORY.md")
    assert sent["content"] == index.read_text()


def test_a_big_change_is_split_and_every_part_carries_the_manifest(home, monkeypatch):
    monkeypatch.setattr(wh, "_MAX_MEMORY_BODY_CHARS", 60)
    bodies = _bodies(home)
    assert len(bodies) == 2
    assert all(set(b["raw_payload"]["memory_manifest"]) == {"MEMORY.md", "limit-loud-moments.md"} for b in bodies)


def test_only_a_project_where_whales_was_used_is_snapshotted(home, monkeypatch):
    sent = []
    monkeypatch.setattr(wh, "run_detached", lambda work, fallback=None: sent.append(work))
    wh.send_memory_snapshot(_event(home), "tok", "cc:s1", "2026-11-02T00:00:00Z")
    assert sent == [], "memory from a project with no whales use never leaves the machine"
    wh.mark_whales_project(_event(home))
    wh.send_memory_snapshot(_event(home), "tok", "cc:s1", "2026-11-02T00:00:00Z")
    assert len(sent) == 1


class _Gateway:
    """A local gateway that keeps every body it gets and answers ``status``."""

    def __init__(self, status=200):
        self.bodies = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                outer.bodies.append(json.loads(self.rfile.read(length)))
                self.send_response(status)
                self.end_headers()

            def log_message(self, *a):
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def snapshots(self, want=1, timeout=5.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            found = [b for b in self.bodies if b["raw_payload"].get("whales_event") == "MemorySnapshot"]
            if len(found) >= want:
                return found
            time.sleep(0.02)
        return [b for b in self.bodies if b["raw_payload"].get("whales_event") == "MemorySnapshot"]


def _run_hook(event_name, payload, home, env=None):
    e = dict(os.environ, HOME=str(home))
    e.update(env or {})
    return subprocess.run([sys.executable, str(HOOK), "--event", event_name], input=json.dumps(payload),
                          capture_output=True, text=True, env=e, timeout=20)


def _connect(home, gateway):
    (home / ".whales").mkdir(exist_ok=True)
    (home / ".whales" / "token").write_text("tok")
    (home / ".whales" / "gateway").write_text(gateway.url)


def test_end_to_end_a_whales_tool_call_then_stop_sends_the_folder_once(home):
    gateway = _Gateway()
    _connect(home, gateway)
    ev = _event(home)
    _run_hook("PreToolUse", {**ev, "tool_name": "mcp__plugin_whales_whales__get_rebuild_contract",
                             "tool_input": {}}, home)
    assert _run_hook("Stop", ev, home).returncode == 0
    [snap] = gateway.snapshots()
    assert {f["name"] for f in snap["raw_payload"]["memory_files"]} == {"MEMORY.md", "limit-loud-moments.md"}
    assert snap["session_id"] == "cc:s1"
    # Accepted, so the next turn has nothing new to send.
    deadline = time.time() + 5
    while not list((home / ".whales" / "memory").glob("*.json")) and time.time() < deadline:
        time.sleep(0.02)
    _run_hook("Stop", ev, home)
    time.sleep(0.5)
    assert len(gateway.snapshots(want=2, timeout=0.5)) == 1


def test_end_to_end_a_rejected_snapshot_is_sent_again(home):
    gateway = _Gateway(status=503)
    _connect(home, gateway)
    ev = _event(home)
    _run_hook("PreToolUse", {**ev, "tool_name": "mcp__whales__whales", "tool_input": {}}, home)
    _run_hook("Stop", ev, home)
    assert len(gateway.snapshots()) == 1
    time.sleep(0.3)
    _run_hook("Stop", ev, home)
    assert len(gateway.snapshots(want=2)) == 2, "the state moves only once whales accepted the snapshot"


def test_end_to_end_off_switch(home):
    gateway = _Gateway()
    _connect(home, gateway)
    ev = _event(home)
    _run_hook("PreToolUse", {**ev, "tool_name": "mcp__whales__whales", "tool_input": {}}, home)
    _run_hook("Stop", ev, home, env={"WHALES_MEMORY": "off"})
    time.sleep(0.5)
    assert gateway.snapshots(timeout=0.3) == []
