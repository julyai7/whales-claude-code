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
        text, offset, truncated, _ = wh.transcript_delta(str(t), "s1")
        assert text == "line one\n"
        assert offset == t.stat().st_size
        assert truncated is False

    def test_second_read_returns_only_what_was_appended(self, tmp_path):
        t = tmp_path / "t.jsonl"
        t.write_text("line one\n")
        wh.OFFSET_DIR = str(tmp_path / "offsets")
        _, offset, _, _ = wh.transcript_delta(str(t), "s2")
        wh.save_offset("s2", offset)

        t.write_text("line one\nline two\n")
        text, _, _, _ = wh.transcript_delta(str(t), "s2")
        assert text == "line two\n", "a delta that re-ships the whole file is quadratic"

    def test_nothing_new_returns_empty(self, tmp_path):
        t = tmp_path / "t.jsonl"
        t.write_text("line one\n")
        wh.OFFSET_DIR = str(tmp_path / "offsets")
        _, offset, _, _ = wh.transcript_delta(str(t), "s3")
        wh.save_offset("s3", offset)
        text, offset2, _, _ = wh.transcript_delta(str(t), "s3")
        assert text == ""
        assert offset2 is None

    def test_a_shrunk_file_resets_instead_of_seeking_past_the_end(self, tmp_path):
        # A reused session id or a rotated transcript. Without the reset the
        # offset stays beyond EOF and the session is never captured again.
        t = tmp_path / "t.jsonl"
        t.write_text("a very long first session\n")
        wh.OFFSET_DIR = str(tmp_path / "offsets")
        _, offset, _, _ = wh.transcript_delta(str(t), "s4")
        wh.save_offset("s4", offset)

        t.write_text("short\n")
        text, _, _, _ = wh.transcript_delta(str(t), "s4")
        assert text == "short\n"

    def test_oversized_delta_ships_in_line_aligned_chunks_and_loses_nothing(self, tmp_path):
        # This used to keep only the tail and jump the offset to the end,
        # silently dropping the middle of every long turn.
        t = tmp_path / "t.jsonl"
        lines = [f'{{"n": {i}, "pad": "{"x" * 20}"}}\n' for i in range(10)]
        t.write_text("".join(lines))
        wh.OFFSET_DIR = str(tmp_path / "offsets")
        wh._MAX_TRANSCRIPT_BYTES, original = 100, wh._MAX_TRANSCRIPT_BYTES
        shipped = []
        try:
            while True:
                text, offset, more, start = wh.transcript_delta(str(t), "s5")
                if not text:
                    break
                assert text.endswith("\n"), "a JSONL record must never be split"
                assert start == sum(len(c.encode()) for c in shipped)
                shipped.append(text)
                wh.save_offset("s5", offset, str(t))
                if not more:
                    break
        finally:
            wh._MAX_TRANSCRIPT_BYTES = original
        assert len(shipped) > 1
        assert "".join(shipped) == "".join(lines)

    def test_offsets_count_bytes_not_characters(self, tmp_path):
        # A text-mode read counts characters; on a non-ASCII transcript the
        # offset then drifts and re-ships or skips bytes.
        t = tmp_path / "t.jsonl"
        t.write_text("héllo — ünïcode\n", encoding="utf-8")
        wh.OFFSET_DIR = str(tmp_path / "offsets")
        text, offset, more, start = wh.transcript_delta(str(t), "s7")
        assert (text, start, more) == ("héllo — ünïcode\n", 0, False)
        assert offset == t.stat().st_size

    def test_missing_transcript_is_not_an_error(self, tmp_path):
        wh.OFFSET_DIR = str(tmp_path / "offsets")
        assert wh.transcript_delta(str(tmp_path / "nope.jsonl"), "s6") == ("", None, False, None)


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

    def _configured(self, home, status=None):
        (home / ".whales").mkdir()
        (home / ".whales" / "token").write_text("tok")
        (home / ".whales" / "gateway").write_text("http://127.0.0.1:9")
        if status is not None:
            (home / ".whales" / "capture_status.json").write_text(json.dumps(status))

    def _session_start(self, home):
        r = self._run("SessionStart", {"session_id": "abc"}, home)
        return json.loads(r.stdout)

    def test_a_token_alone_does_not_claim_capture_is_active(self, tmp_path):
        # A token file says nothing about whether the gateway accepts it. This
        # used to read "capture is active" — shown for four weeks to a
        # designer whose every upload was being rejected.
        self._configured(tmp_path)
        out = self._session_start(tmp_path)
        ctx = out["hookSpecificOutput"]["additionalContext"]
        assert "capture is active" not in ctx
        assert "no upload has been confirmed" in ctx
        assert "systemMessage" not in out

    def test_session_start_claims_capture_after_a_confirmed_upload(self, tmp_path):
        self._configured(tmp_path, {"last_ok_at": "2026-09-24T10:00:00Z", "failed_since_ok": 0})
        ctx = self._session_start(tmp_path)["hookSpecificOutput"]["additionalContext"]
        assert "capture is active" in ctx
        assert "2026-09-24T10:00:00Z" in ctx, "say when it last worked, not just that it does"

    def test_a_rejected_token_is_reported_to_the_model_and_the_designer(self, tmp_path):
        self._configured(tmp_path, {
            "last_ok_at": "2026-08-27T15:54:00Z",
            "failed_since_ok": 412,
            "first_failed_at": "2026-08-27T16:10:00Z",
            "last_error": {"at": "2026-09-24T09:00:00Z", "status": 401, "reason": "Unauthorized"},
        })
        out = self._session_start(tmp_path)
        ctx = out["hookSpecificOutput"]["additionalContext"]
        assert "capture is active" not in ctx
        assert "rejected" in ctx and "2026-08-27T16:10:00Z" in ctx and "412" in ctx
        assert "installer" in out["systemMessage"], "the designer must see it, not only the model"

    def test_an_unreachable_gateway_is_failing_but_asks_nothing_of_the_designer(self, tmp_path):
        self._configured(tmp_path, {
            "failed_since_ok": 3,
            "first_failed_at": "2026-09-24T09:00:00Z",
            "last_error": {"at": "2026-09-24T09:05:00Z", "status": None, "reason": "timed out"},
        })
        out = self._session_start(tmp_path)
        ctx = out["hookSpecificOutput"]["additionalContext"]
        assert "failing" in ctx and "timed out" in ctx
        assert "systemMessage" not in out, "nothing for the designer to do about an outage"

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

    def test_first_upload_after_an_outage_reports_how_many_were_lost(self, tmp_path):
        server, received = _serve(200)
        (tmp_path / ".whales").mkdir()
        (tmp_path / ".whales" / "token").write_text("tok")
        (tmp_path / ".whales" / "gateway").write_text(f"http://127.0.0.1:{server.server_address[1]}")
        (tmp_path / ".whales" / "capture_status.json").write_text(json.dumps({
            "failed_since_ok": 3,
            "first_failed_at": "2026-09-24T09:00:00Z",
            "last_error": {"at": "2026-09-24T09:05:00Z", "status": 401, "reason": "Unauthorized"},
        }))
        r = self._run("UserPromptSubmit", {"session_id": "abc", "prompt": "hi"}, tmp_path)
        _wait_for(received)
        assert r.returncode == 0
        health = received["body"]["raw_payload"]["capture_health"]
        assert health["failed_since_ok"] == 3
        assert health["first_failed_at"] == "2026-09-24T09:00:00Z"


class TestHostNoise:
    """Conductor's own bookkeeping must not read as the designer's work."""

    TITLE_PROMPT = ("You are generating a short conversation title.  Return only the title. "
                    "Do not include backticks, explanations, quotes, markdown, or `git branch -m`.")

    def test_a_conductor_title_session_is_tagged_on_every_event(self, whales_home, monkeypatch):
        monkeypatch.setattr(wh, "SYNTHETIC_DIR", str(whales_home / "synthetic"))
        assert wh.synthetic_kind({"prompt": self.TITLE_PROMPT}, "side", "UserPromptSubmit") == "conductor_title"
        # Its Stop carries the "reply" (the title) and has no prompt of its own.
        assert wh.synthetic_kind({"last_assistant_message": "whales-design-conventions"},
                                 "side", "Stop") == "conductor_title"
        assert wh.synthetic_kind({}, "side", "SessionEnd") == "conductor_title"
        assert not (whales_home / "synthetic" / "side").exists(), "marker is cleared at SessionEnd"

    def test_the_designers_own_session_is_not_tagged(self, whales_home, monkeypatch):
        monkeypatch.setattr(wh, "SYNTHETIC_DIR", str(whales_home / "synthetic"))
        wh.synthetic_kind({"prompt": self.TITLE_PROMPT}, "side", "UserPromptSubmit")
        assert wh.synthetic_kind({"prompt": "make the text 20% smaller"}, "work", "UserPromptSubmit") == ""
        assert wh.synthetic_kind({"last_assistant_message": "Done."}, "work", "Stop") == ""

    def test_a_title_prompt_quoted_later_in_a_real_session_does_not_tag_it(self, whales_home, monkeypatch):
        monkeypatch.setattr(wh, "SYNTHETIC_DIR", str(whales_home / "synthetic"))
        prompt = f"why does conductor send this: {self.TITLE_PROMPT}"
        assert wh.synthetic_kind({"prompt": prompt}, "work", "UserPromptSubmit") == ""

    def test_prompt_user_text_drops_the_preamble_and_keeps_what_was_typed(self):
        prompt = ("<system_instruction>\nYou are working inside Conductor, a Mac app…\n"
                  "</system_instruction>\n\nmake the text 20% smaller")
        assert wh.prompt_user_text(prompt) == "make the text 20% smaller"

    def test_prompt_user_text_survives_a_preamble_longer_than_the_field_cap(self):
        prompt = f"<system_instruction>{'x' * (wh._MAX_FIELD_CHARS + 5000)}</system_instruction>" \
                 "make the letters bigger"
        assert "make the letters bigger" not in wh.truncate({"prompt": prompt})["prompt"], \
            "precondition: the cap does cut the designer's words off the raw prompt"
        assert wh.prompt_user_text(prompt) == "make the letters bigger"

    def test_prompts_without_a_preamble_add_nothing(self):
        assert wh.prompt_user_text("make it 26") == ""
        assert wh.prompt_user_text(None) == ""

    def test_both_land_on_the_shipped_event(self, tmp_path):
        server, received = _serve(200)
        (tmp_path / ".whales").mkdir()
        (tmp_path / ".whales" / "token").write_text("tok")
        (tmp_path / ".whales" / "gateway").write_text(f"http://127.0.0.1:{server.server_address[1]}")
        subprocess.run(
            [sys.executable, str(HOOK), "--event", "UserPromptSubmit"],
            input=json.dumps({"session_id": "side", "prompt": self.TITLE_PROMPT}),
            capture_output=True, text=True, env=dict(os.environ, HOME=str(tmp_path)), timeout=20,
        )
        _wait_for(received)
        raw = received["body"]["raw_payload"]
        assert raw["synthetic"] == "conductor_title"
        assert raw["prompt"] == self.TITLE_PROMPT, "the raw prompt is kept as it was"


class TestBackfill:
    """Finished sessions never fire another hook, so after an outage their
    backlog only ships if someone runs --backfill."""

    def _setup(self, home, gateway_status):
        import threading
        from http.server import BaseHTTPRequestHandler, HTTPServer

        bodies = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                bodies.append(json.loads(self.rfile.read(length)))
                self.send_response(gateway_status)
                self.end_headers()

            def log_message(self, *a):
                pass

        server = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        (home / ".whales").mkdir()
        (home / ".whales" / "token").write_text("tok")
        (home / ".whales" / "gateway").write_text(f"http://127.0.0.1:{server.server_address[1]}")
        project = home / ".claude" / "projects" / "-Users-x-repo"
        (project / "subagents").mkdir(parents=True)
        (project / "sess1.jsonl").write_text('{"a": 1}\n{"b": 2}\n')
        (project / "subagents" / "agent-1.jsonl").write_text('{"sub": 1}\n')
        return server, bodies, project

    def _run(self, home, *flags, scope=("--since", "2000-01-01", "--all-projects"), cwd=None):
        return subprocess.run(
            [sys.executable, str(HOOK), "--backfill", *scope, *flags],
            capture_output=True, text=True, env=dict(os.environ, HOME=str(home)), timeout=20,
            cwd=cwd,
        )

    def test_since_is_required(self, tmp_path):
        server, bodies, _ = self._setup(tmp_path, 200)
        try:
            r = self._run(tmp_path, scope=("--all-projects",))
        finally:
            server.shutdown()
        assert bodies == [] and "pass --since" in r.stdout

    def test_only_this_directorys_sessions_by_default(self, tmp_path):
        server, bodies, project = self._setup(tmp_path, 200)
        here = tmp_path / "work" / "Koi app"
        here.mkdir(parents=True)
        mine = project.parent / wh.re.sub(r"[^A-Za-z0-9]", "-", os.path.realpath(here))
        mine.mkdir()
        (mine / "koi1.jsonl").write_text('{"k": 1}\n')
        try:
            self._run(tmp_path, scope=("--since", "2000-01-01"), cwd=str(here))
        finally:
            server.shutdown()
        assert [b["session_id"] for b in bodies] == ["cc:koi1"], "another project's sess1 stays home"

    def test_ships_past_sessions_but_not_subagent_transcripts(self, tmp_path):
        server, bodies, _ = self._setup(tmp_path, 200)
        try:
            r = self._run(tmp_path)
        finally:
            server.shutdown()
        assert r.returncode == 0
        assert [b["session_id"] for b in bodies] == ["cc:sess1"]
        raw = bodies[0]["raw_payload"]
        assert raw["hook_event_name"] == "Backfill"
        assert raw["transcript_delta"] == '{"a": 1}\n{"b": 2}\n'
        assert raw["transcript_delta_range"] == [0, len('{"a": 1}\n{"b": 2}\n')]
        assert "done" in r.stdout

    def test_resumes_from_the_offset_and_from_start_overrides_it(self, tmp_path):
        server, bodies, project = self._setup(tmp_path, 200)
        (tmp_path / ".whales" / "offsets").mkdir()
        # An offset written by the old script: at the end, though nothing arrived.
        (tmp_path / ".whales" / "offsets" / "sess1.offset").write_text(
            str((project / "sess1.jsonl").stat().st_size))
        try:
            self._run(tmp_path)
            assert bodies == [], "resuming from a stale offset ships nothing"
            self._run(tmp_path, "--from-start")
        finally:
            server.shutdown()
        assert len(bodies) == 1 and bodies[0]["raw_payload"]["transcript_delta_range"][0] == 0

    def test_stops_at_a_rejected_token_and_leaves_the_offset(self, tmp_path):
        server, bodies, _ = self._setup(tmp_path, 401)
        try:
            r = self._run(tmp_path)
        finally:
            server.shutdown()
        assert len(bodies) == 1, "every later request would be rejected too"
        assert "token rejected" in r.stdout
        assert not (tmp_path / ".whales" / "offsets" / "sess1.offset").exists()
        status = json.loads((tmp_path / ".whales" / "capture_status.json").read_text())
        assert status["last_error"]["status"] == 401

    def test_without_a_token_it_sends_nothing(self, tmp_path):
        r = self._run(tmp_path)
        assert r.returncode == 0
        assert "nothing sent" in r.stdout


def test_a_rejected_token_sends_events_without_re_sending_the_transcript(tmp_path):
    server, received = _serve(200)
    (tmp_path / ".whales").mkdir()
    (tmp_path / ".whales" / "token").write_text("tok")
    (tmp_path / ".whales" / "gateway").write_text(f"http://127.0.0.1:{server.server_address[1]}")
    (tmp_path / ".whales" / "capture_status.json").write_text(json.dumps({
        "failed_since_ok": 5,
        "first_failed_at": "2026-09-24T09:00:00Z",
        "last_error": {"at": "2026-09-24T09:05:00Z", "status": 401, "reason": "Unauthorized"},
    }))
    transcript = tmp_path / "t.jsonl"
    transcript.write_text("unshipped backlog\n")
    subprocess.run(
        [sys.executable, str(HOOK), "--event", "Stop"],
        input=json.dumps({"session_id": "abc", "transcript_path": str(transcript)}),
        capture_output=True, text=True, env=dict(os.environ, HOME=str(tmp_path)), timeout=20,
    )
    _wait_for(received)
    assert "transcript_delta" not in received["body"]["raw_payload"]
    assert not (tmp_path / ".whales" / "offsets" / "abc.offset").exists(), (
        "the backlog must still be there to ship once the token works again"
    )


def _serve(status):
    """A one-request local gateway that answers with ``status`` and keeps the
    body it received."""
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    received = {}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            received["body"] = json.loads(self.rfile.read(length))
            self.send_response(status)
            self.end_headers()

        def log_message(self, *a):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.handle_request, daemon=True).start()
    return server, received


def _wait_for(received, timeout=5.0):
    import time

    deadline = time.time() + timeout
    while "body" not in received and time.time() < deadline:
        time.sleep(0.02)


@pytest.fixture
def whales_home(tmp_path, monkeypatch):
    """Point the module's state files at a temp dir for in-process tests."""
    monkeypatch.setattr(wh, "CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(wh, "STATUS_FILE", str(tmp_path / "capture_status.json"))
    monkeypatch.setattr(wh, "OFFSET_DIR", str(tmp_path / "offsets"))
    return tmp_path


class TestDelivery:
    """The offset may only move once the gateway has accepted the upload —
    moving it first turned every failed upload into transcript that was never
    sent again."""

    def _transcript(self, home, text="line one\n"):
        t = home / "t.jsonl"
        t.write_text(text)
        return t

    def test_a_rejected_upload_leaves_the_offset_so_it_is_retried(self, whales_home):
        t = self._transcript(whales_home)
        server, _ = _serve(401)
        wh.deliver(f"http://127.0.0.1:{server.server_address[1]}", "tok", b"{}",
                   "s1", t.stat().st_size, str(t))
        text = wh.transcript_delta(str(t), "s1")[0]
        assert text == "line one\n", "a failed upload must ship the same bytes again"
        status = wh.read_status()
        assert status["failed_since_ok"] == 1
        assert status["last_error"]["status"] == 401
        assert wh.capture_state("tok") == "rejected"

    def test_an_unreachable_gateway_is_recorded_as_failing_not_rejected(self, whales_home):
        t = self._transcript(whales_home)
        wh.deliver("http://127.0.0.1:9", "tok", b"{}", "s1", t.stat().st_size, str(t))
        assert wh.read_status()["last_error"]["status"] is None
        assert wh.capture_state("tok") == "failing"
        assert wh.transcript_delta(str(t), "s1")[0] == "line one\n"

    def test_an_accepted_upload_moves_the_offset_and_clears_the_failure_run(self, whales_home):
        t = self._transcript(whales_home)
        wh.record_send_result(False, 401, "Unauthorized")
        server, _ = _serve(200)
        wh.deliver(f"http://127.0.0.1:{server.server_address[1]}", "tok", b"{}",
                   "s1", t.stat().st_size, str(t))
        assert wh.transcript_delta(str(t), "s1")[0] == ""
        status = wh.read_status()
        assert status["failed_since_ok"] == 0
        assert "first_failed_at" not in status
        assert wh.capture_state("tok") == "active"

    def test_a_failure_run_keeps_the_time_it_started(self, whales_home):
        wh.record_send_result(False, 401, "Unauthorized")
        first = wh.read_status()["first_failed_at"]
        wh.record_send_result(False, 401, "Unauthorized")
        status = wh.read_status()
        assert status["failed_since_ok"] == 2
        assert status["first_failed_at"] == first

    def test_a_token_with_no_recorded_upload_is_unconfirmed(self, whales_home):
        assert wh.capture_state("tok") == "unconfirmed"
        assert wh.capture_state("") == "off"


class TestOffsetOrdering:
    """Uploads finish out of order; an earlier one finishing last must not
    undo a later one's progress."""

    def test_offset_never_moves_backwards_within_the_same_file(self, whales_home):
        t = whales_home / "t.jsonl"
        t.write_text("x" * 200)
        wh.save_offset("s", 150, str(t))
        wh.save_offset("s", 100, str(t))
        assert (whales_home / "offsets" / "s.offset").read_text() == "150"

    def test_a_shrunk_file_still_lets_the_offset_reset(self, whales_home):
        t = whales_home / "t.jsonl"
        t.write_text("x" * 200)
        wh.save_offset("s", 200, str(t))
        t.write_text("x" * 30)
        wh.save_offset("s", 30, str(t))
        assert (whales_home / "offsets" / "s.offset").read_text() == "30"


class TestSessionIdInjection:
    """The model passed ``client_session_id`` on 4 of 150 Whales calls; the
    PreToolUse hook sets it instead."""

    def _run(self, payload, home, source="claude_code_hook"):
        return subprocess.run(
            [sys.executable, str(HOOK), "--event", "PreToolUse", "--source", source],
            input=json.dumps(payload), capture_output=True, text=True,
            env=dict(os.environ, HOME=str(home)), timeout=20,
        )

    def _event(self, tool, tool_input=None):
        return {
            "session_id": "abc",
            "hook_event_name": "PreToolUse",
            "tool_name": tool,
            "tool_input": {"html": "<div/>", "product": "Koi"} if tool_input is None else tool_input,
        }

    @pytest.mark.parametrize("prefix", [
        "mcp__whales__", "mcp__plugin_whales_whales__",
        "mcp__9ee03eae-1c7e-4bbb-8a9e-271027f5c120__",
    ])
    def test_adds_the_session_id_and_keeps_every_other_argument(self, tmp_path, prefix):
        # No token on purpose: tool calls authenticate with the plugin's own
        # credential, so this must work even where capture is off.
        r = self._run(self._event(f"{prefix}submit_design"), tmp_path)
        assert r.returncode == 0
        out = json.loads(r.stdout)["hookSpecificOutput"]
        assert out["hookEventName"] == "PreToolUse"
        assert out["updatedInput"] == {"html": "<div/>", "product": "Koi", "client_session_id": "cc:abc"}
        assert "permissionDecision" not in out, "must not override the designer's permission rules"

    def test_overwrites_a_wrong_session_id(self, tmp_path):
        r = self._run(self._event("mcp__whales__ask_whales",
                                  {"question": "q", "client_session_id": "cc:other"}), tmp_path)
        assert json.loads(r.stdout)["hookSpecificOutput"]["updatedInput"]["client_session_id"] == "cc:abc"

    @pytest.mark.parametrize("tool", [
        "mcp__whales__get_rebuild_contract",  # declares no client_session_id
        "Write",                              # not an MCP tool
        "mcp__figma__get_design_context",     # someone else's tool
    ])
    def test_leaves_other_tools_alone(self, tmp_path, tool):
        r = self._run(self._event(tool), tmp_path)
        assert r.returncode == 0
        assert r.stdout.strip() == ""

    def test_no_output_when_the_id_is_already_right(self, tmp_path):
        r = self._run(self._event("mcp__whales__whales",
                                  {"request": "r", "client_session_id": "cc:abc"}), tmp_path)
        assert r.stdout.strip() == ""

    def test_cursor_source_answers_in_cursors_shape_not_claude_codes(self, tmp_path):
        r = self._run(self._event("MCP:submit_design"), tmp_path, source="cursor_hook")
        assert r.returncode == 0
        out = json.loads(r.stdout)
        assert "hookSpecificOutput" not in out
        assert out["updated_input"]["client_session_id"] == "cur:abc"

    def test_hooks_json_matcher_names_exactly_the_injected_tools(self):
        # Two lists of the same tools; a gateway tool added to one and not the
        # other either goes unlinked or starts the hook for nothing.
        import re

        hooks = json.loads((HOOK.parents[1] / "hooks" / "hooks.json").read_text())
        matcher = hooks["hooks"]["PreToolUse"][0]["matcher"]
        assert set(re.search(r"\((.*)\)", matcher).group(1).split("|")) == set(wh.SESSION_ID_TOOLS)

    def test_ships_nothing_to_the_gateway(self, tmp_path):
        server, received = _serve(200)
        (tmp_path / ".whales").mkdir()
        (tmp_path / ".whales" / "token").write_text("tok")
        (tmp_path / ".whales" / "gateway").write_text(f"http://127.0.0.1:{server.server_address[1]}")
        self._run(self._event("mcp__whales__submit_design"), tmp_path)
        _wait_for(received, timeout=1.0)
        assert "body" not in received, "PreToolUse rewrites arguments; it is not a capture event"


class TestOneUploadPerSession:
    """A burst of events must not ship the same transcript bytes more than
    once, and the end of a long session must not wait for events that never
    come."""

    def test_the_lock_is_exclusive_until_released(self, whales_home):
        first = wh.acquire_upload_lock("s1")
        assert first and wh.acquire_upload_lock("s1") is None
        wh.release_upload_lock(first)
        assert wh.acquire_upload_lock("s1")

    def test_a_lock_left_by_a_dead_upload_is_taken_over(self, whales_home):
        stale = wh.acquire_upload_lock("s1")
        old = os.path.getmtime(stale) - wh._LOCK_STALE_SECONDS - 5
        os.utime(stale, (old, old))
        assert wh.acquire_upload_lock("s1")

    def test_an_event_during_an_upload_goes_without_the_transcript(self, tmp_path):
        server, received = _serve(200)
        (tmp_path / ".whales" / "offsets").mkdir(parents=True)
        (tmp_path / ".whales" / "token").write_text("tok")
        (tmp_path / ".whales" / "gateway").write_text(f"http://127.0.0.1:{server.server_address[1]}")
        (tmp_path / ".whales" / "offsets" / "abc.lock").write_text("123")
        transcript = tmp_path / "t.jsonl"
        transcript.write_text("in flight\n")
        subprocess.run(
            [sys.executable, str(HOOK), "--event", "PostToolUse"],
            input=json.dumps({"session_id": "abc", "transcript_path": str(transcript)}),
            capture_output=True, text=True, env=dict(os.environ, HOME=str(tmp_path)), timeout=20,
        )
        _wait_for(received)
        assert "transcript_delta" not in received["body"]["raw_payload"]
        assert received["body"]["raw_payload"]["whales_event"] == "PostToolUse"
        assert (tmp_path / ".whales" / "offsets" / "abc.lock").exists(), "not ours to release"

    def test_an_accepted_upload_drains_the_rest_and_releases_the_lock(self, whales_home):
        import threading
        from http.server import BaseHTTPRequestHandler, HTTPServer

        bodies = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                bodies.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                self.send_response(200)
                self.end_headers()

            def log_message(self, *a):
                pass

        server = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        t = whales_home / "t.jsonl"
        line = "x" * 1000 + "\n"
        t.write_text(line * 1300)  # ~1.3MB: three chunks
        try:
            lock = wh.acquire_upload_lock("s1")
            text, end, more, start = wh.transcript_delta(str(t), "s1")
            assert more
            wh.deliver(f"http://127.0.0.1:{server.server_address[1]}", "tok", b"{}", "s1", end,
                       str(t), chunk_start=start, lock=lock, drain_to=("claude_code_hook", "cc:s1"))
        finally:
            server.shutdown()
        assert len(bodies) == 3
        ranges = [b["raw_payload"]["transcript_delta_range"] for b in bodies[1:]]
        assert ranges[0][0] == end and ranges[-1][1] == t.stat().st_size
        assert {b["raw_payload"]["hook_event_name"] for b in bodies[1:]} == {"TranscriptChunk"}
        assert wh.transcript_delta(str(t), "s1")[0] == ""
        assert not os.path.exists(wh._lock_path("s1"))


class TestChunkBackoff:
    def test_a_new_chunk_is_sent(self):
        assert wh.chunk_decision({}, 0, 1000.0) == "send"
        assert wh.chunk_decision({"start": 5, "count": 9, "status": 500, "at": 1000.0}, 0, 1000.0) == "send"

    def test_a_failed_chunk_waits_longer_each_time(self):
        once = {"start": 0, "count": 1, "status": 503, "at": 1000.0}
        assert wh.chunk_decision(once, 0, 1010.0) == "wait"
        assert wh.chunk_decision(once, 0, 1031.0) == "send"
        thrice = {**once, "count": 3}
        assert wh.chunk_decision(thrice, 0, 1100.0) == "wait"  # 120s
        assert wh.chunk_decision({**once, "count": 30}, 0, 1000.0 + 1801) == "send"  # capped

    def test_an_outage_never_skips_but_a_malformed_chunk_does(self):
        assert wh.chunk_decision({"start": 0, "count": 50, "status": 502, "at": 0}, 0, 10.0**9) == "send"
        assert wh.chunk_decision({"start": 0, "count": 3, "status": 413, "at": 0}, 0, 1.0) == "skip"
        assert wh.chunk_decision({"start": 0, "count": 3, "status": 429, "at": 0}, 0, 10.0**9) == "send"

    def test_a_failed_chunk_is_counted_and_an_accepted_one_clears_it(self, whales_home):
        t = whales_home / "t.jsonl"
        t.write_text("line\n")
        server, _ = _serve(500)
        wh.deliver(f"http://127.0.0.1:{server.server_address[1]}", "tok", b"{}", "s1",
                   t.stat().st_size, str(t), chunk_start=0)
        assert wh.read_chunk_failure("s1")["count"] == 1
        wh.record_chunk_failure("s1", 0, 500)
        assert wh.read_chunk_failure("s1")["count"] == 2
        server, _ = _serve(200)
        wh.deliver(f"http://127.0.0.1:{server.server_address[1]}", "tok", b"{}", "s1",
                   t.stat().st_size, str(t), chunk_start=0)
        assert wh.read_chunk_failure("s1") == {}

    def test_a_chunk_rejected_three_times_is_skipped_and_reported(self, tmp_path):
        server, received = _serve(200)
        offsets = tmp_path / ".whales" / "offsets"
        offsets.mkdir(parents=True)
        (tmp_path / ".whales" / "token").write_text("tok")
        (tmp_path / ".whales" / "gateway").write_text(f"http://127.0.0.1:{server.server_address[1]}")
        (offsets / "abc.fail").write_text(json.dumps({"start": 0, "count": 3, "status": 422, "at": 0}))
        transcript = tmp_path / "t.jsonl"
        transcript.write_text("poison\n")
        subprocess.run(
            [sys.executable, str(HOOK), "--event", "Stop"],
            input=json.dumps({"session_id": "abc", "transcript_path": str(transcript)}),
            capture_output=True, text=True, env=dict(os.environ, HOME=str(tmp_path)), timeout=20,
        )
        _wait_for(received)
        raw = received["body"]["raw_payload"]
        assert raw["transcript_skipped"] == {"range": [0, len("poison\n")], "http_status": 422, "attempts": 3}
        assert "transcript_delta" not in raw
        assert (offsets / "abc.offset").read_text() == str(len("poison\n"))


class TestRetrySendsTheSameBytes:
    """A retry must carry exactly the failed range: when the first attempt
    reached the gateway and only its answer was lost, an identical range is
    the only way the backend can tell the repeat apart."""

    def test_the_failed_range_is_recorded(self, whales_home):
        t = whales_home / "t.jsonl"
        t.write_text("first\n")
        wh.deliver("http://127.0.0.1:9", "tok", b"{}", "s1", t.stat().st_size, str(t), chunk_start=0)
        assert wh.read_chunk_failure("s1")["end"] == len("first\n")

    def test_a_retry_resends_the_same_range_although_the_file_grew(self, tmp_path):
        server, received = _serve(200)
        offsets = tmp_path / ".whales" / "offsets"
        offsets.mkdir(parents=True)
        (tmp_path / ".whales" / "token").write_text("tok")
        (tmp_path / ".whales" / "gateway").write_text(f"http://127.0.0.1:{server.server_address[1]}")
        transcript = tmp_path / "t.jsonl"
        transcript.write_text("first\nsecond, written after the failed attempt\n")
        (offsets / "abc.fail").write_text(json.dumps(
            {"start": 0, "end": len("first\n"), "count": 1, "status": None, "at": 0}))
        subprocess.run(
            [sys.executable, str(HOOK), "--event", "Stop"],
            input=json.dumps({"session_id": "abc", "transcript_path": str(transcript)}),
            capture_output=True, text=True, env=dict(os.environ, HOME=str(tmp_path)), timeout=20,
        )
        _wait_for(received)
        raw = received["body"]["raw_payload"]
        assert raw["transcript_delta"] == "first\n"
        assert raw["transcript_delta_range"] == [0, len("first\n")]
        assert raw["transcript_resend"] is True
        assert raw["transcript_delta_truncated"] is True, "the rest follows"

    def test_a_range_that_is_gone_is_not_resent(self, whales_home):
        t = whales_home / "t.jsonl"
        t.write_text("ab\n")
        assert wh.transcript_range(str(t), 0, 50) == ""
        assert wh.transcript_range(str(t), 0, 2) == "ab"


def test_a_burst_of_events_ships_every_byte_once(tmp_path):
    """Five hooks firing together (quick edits, then Stop) against one
    transcript: the ranges that reach the gateway must not overlap, and
    together must cover the file."""
    import threading
    import time
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    ranges = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            span = body["raw_payload"].get("transcript_delta_range")
            if span:
                time.sleep(0.2)  # slow enough that the others fire mid-upload
                ranges.append(tuple(span))
            self.send_response(200)
            self.end_headers()

        def log_message(self, *a):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    (tmp_path / ".whales").mkdir()
    (tmp_path / ".whales" / "token").write_text("tok")
    (tmp_path / ".whales" / "gateway").write_text(f"http://127.0.0.1:{server.server_address[1]}")
    transcript = tmp_path / "t.jsonl"
    transcript.write_text(("y" * 1000 + "\n") * 1200)  # ~1.2MB: three chunks
    size = transcript.stat().st_size
    procs = [subprocess.Popen(
        [sys.executable, str(HOOK), "--event", event],
        stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        env=dict(os.environ, HOME=str(tmp_path)),
    ) for event in ("PostToolUse", "PostToolUse", "PostToolUse", "Stop", "SessionEnd")]
    for p in procs:
        p.communicate(json.dumps({"session_id": "abc", "transcript_path": str(transcript)}).encode(),
                      timeout=20)
    deadline = time.time() + 15
    while sum(e - s for s, e in ranges) < size and time.time() < deadline:
        time.sleep(0.05)
    time.sleep(0.5)  # anything shipped twice would land now
    server.shutdown()
    spans = sorted(ranges)
    assert spans[0][0] == 0 and spans[-1][1] == size
    for (_, end), (start, _) in zip(spans, spans[1:]):
        assert end == start, f"overlap or gap in {spans}"


class TestCursorHost:
    """Cursor's own answer shapes (https://cursor.com/docs/hooks): it ignores
    Claude Code's hookSpecificOutput, so each of these used to do nothing."""

    def _run(self, event_name, payload, home, env=None):
        e = dict(os.environ, HOME=str(home))
        e.pop("WHALES_CLIENT_SESSION_ID", None)
        e.update(env or {})
        return subprocess.run(
            [sys.executable, str(HOOK), "--event", event_name, "--source", "cursor_hook"],
            input=json.dumps(payload), capture_output=True, text=True, env=e, timeout=20,
        )

    # -- preToolUse ---------------------------------------------------------

    @pytest.mark.parametrize("tool", ["MCP:universal_critique", "mcp__whales__universal_critique"])
    def test_injection_keeps_every_argument_and_adds_the_session(self, tool):
        out = wh.cursor_session_id_injection(
            {"tool_name": tool, "conversation_id": "c1",
             "tool_input": {"source_id": "a" * 32 + ".html", "goal": "RSVPs"}}, "cur")
        assert out == {"updated_input": {"source_id": "a" * 32 + ".html", "goal": "RSVPs",
                                         "client_session_id": "cur:c1"}}
        assert "permission" not in out

    def test_a_json_string_tool_input_is_parsed(self):
        out = wh.cursor_session_id_injection(
            {"tool_name": "MCP:self_critique", "session_id": "s",
             "tool_input": json.dumps({"source_id": "x", "critique_id": "c"})}, "cur")
        assert out["updated_input"] == {"source_id": "x", "critique_id": "c", "client_session_id": "cur:s"}

    def test_falls_back_to_the_env_session_start_set(self, monkeypatch):
        monkeypatch.setenv("WHALES_CLIENT_SESSION_ID", "cur:from-env")
        out = wh.cursor_session_id_injection(
            {"tool_name": "MCP:record_critique", "tool_input": {"critique": "too busy"}}, "cur")
        assert out["updated_input"]["client_session_id"] == "cur:from-env"

    @pytest.mark.parametrize("event", [
        {"tool_name": "MCP:get_rebuild_contract", "session_id": "s", "tool_input": {}},
        {"tool_name": "Write", "session_id": "s", "tool_input": {"file_path": "a.html"}},
        {"tool_name": "MCP:submit_design", "session_id": "s", "tool_input": "not json"},
        {"tool_name": "MCP:submit_design", "tool_input": {"html": "<p/>"}},
        {"tool_name": "MCP:submit_design", "session_id": "s",
         "tool_input": {"client_session_id": "cur:s"}},
    ])
    def test_nothing_to_do(self, event, monkeypatch):
        monkeypatch.delenv("WHALES_CLIENT_SESSION_ID", raising=False)
        assert wh.cursor_session_id_injection(event, "cur") is None

    # -- sessionStart --------------------------------------------------------

    def test_session_start_gives_context_and_env(self):
        out = wh.cursor_session_start_output("abc", "Whales capture is active.", "cur")
        assert set(out) == {"additional_context", "env"}
        assert out["env"] == {"WHALES_CLIENT_SESSION_ID": "cur:abc"}
        assert "Cursor session id is `cur:abc`" in out["additional_context"]
        assert out["additional_context"].startswith("Whales capture is active.")

    # -- postToolUse (DesignContext) ------------------------------------------

    @pytest.mark.parametrize("path,kind", [
        ("/w/landing.html", "page"), ("/w/landing.HTM", "page"),
        ("/w/shot.png", "image"), ("/w/shot.webp", "image"),
    ])
    def test_a_critiquable_file_is_named_with_its_upload_command(self, path, kind):
        text = wh.cursor_design_context({"tool_name": "Write", "tool_input": {"file_path": path}})
        assert f"this {kind} was written at `{path}`" in text
        assert f'critique_source.py upload "{path}"' in text
        assert "`source_id`" in text and "universal_critique" in text

    def test_a_canvas_is_not_a_critique_source(self):
        text = wh.cursor_design_context({"file_path": "/w/board.canvas.tsx"})
        assert "not something Whales can critique" in text
        assert "upload" not in text

    def test_other_files_get_no_context(self):
        assert wh.cursor_design_context({"tool_input": {"path": "/w/app.ts"}}) == ""

    def test_design_context_prints_context_and_never_posts(self, tmp_path):
        server, received = _serve(200)
        (tmp_path / ".whales").mkdir()
        (tmp_path / ".whales" / "token").write_text("tok")
        (tmp_path / ".whales" / "gateway").write_text(f"http://127.0.0.1:{server.server_address[1]}")
        r = self._run("DesignContext",
                      {"conversation_id": "c", "tool_name": "Write",
                       "tool_input": json.dumps({"file_path": "/w/a.html"})}, tmp_path)
        assert r.returncode == 0
        assert "critique_source.py upload" in json.loads(r.stdout)["additional_context"]
        _wait_for(received, timeout=1.0)
        assert "body" not in received, "afterFileEdit already records the write"

    def test_design_context_is_cursor_only(self, tmp_path):
        r = subprocess.run(
            [sys.executable, str(HOOK), "--event", "DesignContext"],
            input=json.dumps({"tool_input": {"file_path": "/w/a.html"}}),
            capture_output=True, text=True, env=dict(os.environ, HOME=str(tmp_path)), timeout=20)
        assert r.returncode == 0 and r.stdout.strip() == ""

    # -- the one list of Cursor events ----------------------------------------

    def test_cursor_hooks_json_is_the_full_event_list_and_runs_the_wrapper(self):
        import re

        cfg = json.loads((HOOK.parents[1] / "cursor" / "hooks.json").read_text())
        assert cfg["version"] == 1
        events = cfg["hooks"]
        assert set(events) == {"sessionStart", "sessionEnd", "beforeSubmitPrompt", "afterFileEdit",
                               "preToolUse", "postToolUse", "preCompact", "stop"}
        known = {"SessionStart", "SessionEnd", "UserPromptSubmit", "PostToolUse", "PreToolUse",
                 "DesignContext", "PreCompact", "Stop"}
        for name, entries in events.items():
            assert len(entries) == 1, name
            command = entries[0]["command"]
            m = re.fullmatch(r'python3 "\$\{HOME\}/\.whales/scripts/whales_hook\.py" '
                             r'--event (\w+) --source cursor_hook', command)
            assert m and m.group(1) in known, command
        assert events["postToolUse"][0]["matcher"] == "Write"
        assert "DesignContext" in events["postToolUse"][0]["command"]
        assert "matcher" not in events["preToolUse"][0]
