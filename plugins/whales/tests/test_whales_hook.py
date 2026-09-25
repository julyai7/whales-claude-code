"""Tests for the capture hook.

Two properties get real coverage because both fail silently in production:
scrubbing (a leaked key is invisible until it isn't) and delta offsets (a
broken offset either re-ships the whole session every turn or ships nothing
ever, and both look like "it's working" from the editor).

Stdlib + pytest only, matching the script's own no-dependency rule.
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parents[1] / "scripts" / "whales_hook.py"


def _load():
    spec = importlib.util.spec_from_file_location("whales_hook", HOOK)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


wh = _load()


class TestScrub:
    @pytest.mark.parametrize(
        "raw",
        [
            "sk-ant-api03-AAAAAAAAAAAAAAAAAAAAAAAA",
            "sk-AAAAAAAAAAAAAAAAAAAAAAAA",
            "ghp_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
            "github_pat_AAAAAAAAAAAAAAAAAAAAAAAA",
            "xoxb-AAAAAAAAAAAAAAAAAAAA",
            "AKIAIOSFODNN7EXAMPLE",
            "whales_agent_abcdefghijklmnopqrstuvwxyz012345",
        ],
    )
    def test_known_credential_shapes_do_not_survive(self, raw):
        assert raw not in wh.scrub(f"prefix {raw} suffix")

    def test_env_style_assignment_keeps_the_key_and_drops_the_value(self):
        # The key name is signal worth keeping ("they configured Stripe");
        # the value is the part that must never leave the machine.
        out = wh.scrub("STRIPE_SECRET_KEY=abcd1234efgh5678")
        assert "STRIPE_SECRET_KEY" in out
        assert "abcd1234efgh5678" not in out

    def test_private_key_block_is_removed_whole(self):
        blob = (
            "-----BEGIN RSA PRIVATE KEY-----\n"
            "MIIEowIBAAKCAQEAxxxx\nyyyy\n"
            "-----END RSA PRIVATE KEY-----"
        )
        out = wh.scrub(f"before {blob} after")
        assert "MIIEowIBAAKCAQEAxxxx" not in out
        assert "before" in out and "after" in out

    def test_ordinary_prose_is_untouched(self):
        text = "make the primary button #1e4fd8 with 8px radius"
        assert wh.scrub(text) == text

    def test_empty_input_is_safe(self):
        assert wh.scrub("") == ""


class TestTranscriptDelta:
    def test_first_read_returns_everything_and_reports_the_offset(self, tmp_path):
        t = tmp_path / "t.jsonl"
        t.write_text("line one\n")
        wh.OFFSET_DIR = str(tmp_path / "offsets")
        text, offset, truncated = wh.transcript_delta(str(t), "s1")
        assert text == "line one\n"
        assert offset == t.stat().st_size
        assert truncated is False

    def test_second_read_returns_only_what_was_appended(self, tmp_path):
        t = tmp_path / "t.jsonl"
        t.write_text("line one\n")
        wh.OFFSET_DIR = str(tmp_path / "offsets")
        _, offset, _ = wh.transcript_delta(str(t), "s2")
        wh.save_offset("s2", offset)

        t.write_text("line one\nline two\n")
        text, _, _ = wh.transcript_delta(str(t), "s2")
        assert text == "line two\n", "a delta that re-ships the whole file is quadratic"

    def test_nothing_new_returns_empty(self, tmp_path):
        t = tmp_path / "t.jsonl"
        t.write_text("line one\n")
        wh.OFFSET_DIR = str(tmp_path / "offsets")
        _, offset, _ = wh.transcript_delta(str(t), "s3")
        wh.save_offset("s3", offset)
        text, offset2, _ = wh.transcript_delta(str(t), "s3")
        assert text == ""
        assert offset2 is None

    def test_a_shrunk_file_resets_instead_of_seeking_past_the_end(self, tmp_path):
        # A reused session id or a rotated transcript. Without the reset the
        # offset stays beyond EOF and the session is never captured again.
        t = tmp_path / "t.jsonl"
        t.write_text("a very long first session\n")
        wh.OFFSET_DIR = str(tmp_path / "offsets")
        _, offset, _ = wh.transcript_delta(str(t), "s4")
        wh.save_offset("s4", offset)

        t.write_text("short\n")
        text, _, _ = wh.transcript_delta(str(t), "s4")
        assert text == "short\n"

    def test_oversized_delta_keeps_the_tail_and_says_so(self, tmp_path):
        t = tmp_path / "t.jsonl"
        t.write_text("x" * 10 + "TAIL_MARKER\n")
        wh.OFFSET_DIR = str(tmp_path / "offsets")
        wh._MAX_TRANSCRIPT_BYTES, original = 12, wh._MAX_TRANSCRIPT_BYTES
        try:
            text, _, truncated = wh.transcript_delta(str(t), "s5")
        finally:
            wh._MAX_TRANSCRIPT_BYTES = original
        assert truncated is True
        assert "TAIL_MARKER" in text, "the recent turns are the ones the event is about"

    def test_missing_transcript_is_not_an_error(self, tmp_path):
        wh.OFFSET_DIR = str(tmp_path / "offsets")
        assert wh.transcript_delta(str(tmp_path / "nope.jsonl"), "s6") == ("", None, False)


class TestProcessBehaviour:
    """Run the real script as a subprocess — exit code and stdout are the
    contract the editor actually consumes."""

    def _run(self, event_name, payload, home, env=None):
        e = dict(os.environ, HOME=str(home))
        e.update(env or {})
        return subprocess.run(
            [sys.executable, str(HOOK), "--event", event_name],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            env=e,
            timeout=20,
        )

    def test_exits_zero_with_no_token(self, tmp_path):
        r = self._run("UserPromptSubmit", {"session_id": "s"}, tmp_path)
        assert r.returncode == 0
        assert r.stdout.strip() == ""

    def test_exits_zero_on_garbage_stdin(self, tmp_path):
        e = dict(os.environ, HOME=str(tmp_path))
        r = subprocess.run(
            [sys.executable, str(HOOK), "--event", "Stop"],
            input="not json at all",
            capture_output=True, text=True, env=e, timeout=20,
        )
        assert r.returncode == 0

    def test_session_start_emits_the_binding(self, tmp_path):
        r = self._run("SessionStart", {"session_id": "abc"}, tmp_path)
        out = json.loads(r.stdout)
        ctx = out["hookSpecificOutput"]["additionalContext"]
        assert out["hookSpecificOutput"]["hookEventName"] == "SessionStart"
        assert "cc:abc" in ctx
        assert "client_session_id" in ctx

    def test_session_start_does_not_claim_capture_when_unconfigured(self, tmp_path):
        # A silent misconfiguration must not read as healthy.
        r = self._run("SessionStart", {"session_id": "abc"}, tmp_path)
        ctx = json.loads(r.stdout)["hookSpecificOutput"]["additionalContext"]
        assert "capture is active" not in ctx
        assert "not configured" in ctx

    def test_session_start_claims_capture_once_a_token_exists(self, tmp_path):
        (tmp_path / ".whales").mkdir()
        (tmp_path / ".whales" / "token").write_text("tok")
        (tmp_path / ".whales" / "gateway").write_text("http://127.0.0.1:9")
        r = self._run("SessionStart", {"session_id": "abc"}, tmp_path)
        ctx = json.loads(r.stdout)["hookSpecificOutput"]["additionalContext"]
        assert "capture is active" in ctx

    def test_capture_can_be_switched_off(self, tmp_path):
        (tmp_path / ".whales").mkdir()
        (tmp_path / ".whales" / "token").write_text("tok")
        r = self._run(
            "SessionStart", {"session_id": "abc"}, tmp_path, env={"WHALES_CAPTURE": "off"}
        )
        ctx = json.loads(r.stdout)["hookSpecificOutput"]["additionalContext"]
        assert "not configured" in ctx, "WHALES_CAPTURE=off must actually stop capture"

    def test_unreachable_gateway_still_exits_zero(self, tmp_path):
        (tmp_path / ".whales").mkdir()
        (tmp_path / ".whales" / "token").write_text("tok")
        (tmp_path / ".whales" / "gateway").write_text("http://127.0.0.1:9")
        r = self._run("Stop", {"session_id": "s"}, tmp_path)
        assert r.returncode == 0, "our availability must never be the designer's problem"

    def test_missing_event_no_ops_instead_of_erroring(self, tmp_path):
        # Cursor auto-discovers and runs a Claude Code plugin's hooks.json on
        # its own, forwarding only `command` and never the exec-form `args`
        # that would carry --event — this firing shape must not crash or
        # print anything, or Cursor treats the nonzero exit as a blocked turn.
        e = dict(os.environ, HOME=str(tmp_path))
        r = subprocess.run(
            [sys.executable, str(HOOK)],
            input=json.dumps({"session_id": "s", "hook_event_name": "beforeSubmitPrompt"}),
            capture_output=True, text=True, env=e, timeout=20,
        )
        assert r.returncode == 0
        assert r.stdout.strip() == ""

    def test_bad_arguments_never_block_the_editor(self, tmp_path):
        # Any argparse rejection raises SystemExit(2), which a plain
        # `except Exception` misses. A host that forwards unexpected flags
        # must still get exit 0, never a blocked turn.
        e = dict(os.environ, HOME=str(tmp_path))
        r = subprocess.run(
            [sys.executable, str(HOOK), "--source", "not_a_host", "--bogus"],
            input="{}", capture_output=True, text=True, env=e, timeout=20,
        )
        assert r.returncode == 0

    def test_cursor_source_binds_a_cur_prefixed_session_in_cursors_shape(self, tmp_path):
        # Cursor reads additional_context and env, not Claude Code's
        # hookSpecificOutput. Emitting the Claude shape here is ignored.
        r = self._run(
            "SessionStart", {"session_id": "abc"}, tmp_path, env={}
        )
        assert r.stdout.strip() == "" or "cc:" in r.stdout  # default source unaffected

        r = subprocess.run(
            [sys.executable, str(HOOK), "--event", "SessionStart", "--source", "cursor_hook"],
            input=json.dumps({"session_id": "abc"}),
            capture_output=True, text=True,
            env=dict(os.environ, HOME=str(tmp_path)),
            timeout=20,
        )
        assert r.returncode == 0
        out = json.loads(r.stdout)
        assert "hookSpecificOutput" not in out
        assert out["env"]["WHALES_CLIENT_SESSION_ID"] == "cur:abc"
        assert "cur:abc" in out["additional_context"]

    def test_cursor_source_ships_a_cur_prefixed_session_id(self, tmp_path):
        import threading
        from http.server import BaseHTTPRequestHandler, HTTPServer

        received = {}

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                received["body"] = json.loads(self.rfile.read(length))
                self.send_response(200)
                self.end_headers()

            def log_message(self, *a):
                pass

        server = HTTPServer(("127.0.0.1", 0), Handler)
        port = server.server_address[1]
        thread = threading.Thread(target=server.handle_request)
        thread.start()

        (tmp_path / ".whales").mkdir()
        (tmp_path / ".whales" / "token").write_text("tok")
        (tmp_path / ".whales" / "gateway").write_text(f"http://127.0.0.1:{port}")

        r = subprocess.run(
            [sys.executable, str(HOOK), "--event", "UserPromptSubmit", "--source", "cursor_hook"],
            input=json.dumps({"session_id": "abc"}),
            capture_output=True, text=True,
            env=dict(os.environ, HOME=str(tmp_path)),
            timeout=20,
        )
        thread.join(timeout=5)
        assert r.returncode == 0
        assert received["body"]["session_id"] == "cur:abc"
        assert received["body"]["source"] == "cursor_hook"
