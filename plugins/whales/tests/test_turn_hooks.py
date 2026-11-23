"""The hook server path (``/hooks/turn/{event}``): what the hook sends, how the server's answer is merged into the
hook's JSON, that only ``.whales/`` is ever written, and that any failure leaves the turn as it was."""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

HOOK = Path(__file__).resolve().parents[1] / "scripts" / "whales_hook.py"


def _load():
    spec = importlib.util.spec_from_file_location("whales_hook_turn", HOOK)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


wh = _load()


def test_merge_adds_context_notice_and_a_stop_block():
    out = {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": "product question"}}
    wh.merge_turn_answer(out, {"context": "whales · rebuild", "notice": "whales: critique first"}, "UserPromptSubmit")
    assert out["hookSpecificOutput"]["additionalContext"] == "product question\n\nwhales · rebuild"
    assert out["systemMessage"] == "whales: critique first"
    stop = wh.merge_turn_answer({}, {"decision": {"block": True, "reason": "contrast on the button"}}, "Stop")
    assert stop == {"decision": "block", "reason": "contrast on the button"}
    # A block is only honoured at Stop, and a restart notice already shown is kept.
    out = wh.merge_turn_answer({"systemMessage": "restart"}, {"decision": {"block": True, "reason": "x"},
                                                              "notice": "n"}, "UserPromptSubmit")
    assert out == {"systemMessage": "restart"}


def test_an_empty_answer_changes_nothing():
    out = {"systemMessage": "x"}
    assert wh.merge_turn_answer(dict(out), {}, "UserPromptSubmit") == out


def test_files_are_written_only_under_dot_whales(tmp_path):
    wh.write_turn_files(str(tmp_path), [
        {"path": ".whales/brief.md", "content": "the full brief"},
        {"path": "../escape.md", "content": "no"},
        {"path": "src/App.tsx", "content": "no"},
        {"path": ".whales/../.env", "content": "no"},
    ])
    assert (tmp_path / ".whales" / "brief.md").read_text() == "the full brief"
    assert (tmp_path / ".whales" / ".gitignore").read_text() == "*\n"
    assert not (tmp_path.parent / "escape.md").exists()
    assert not (tmp_path / "src").exists() and not (tmp_path / ".env").exists()


def test_prompt_event_ids_repeat_for_a_retry_only(tmp_path):
    transcript = tmp_path / "t.jsonl"
    transcript.write_text("a")
    first = wh.prompt_event_id("cc:1", "ok", str(transcript))
    assert wh.prompt_event_id("cc:1", "ok", str(transcript)) == first
    transcript.write_text("a\nb")
    assert wh.prompt_event_id("cc:1", "ok", str(transcript)) != first


def test_repo_map_reads_framework_styling_and_token_files(tmp_path):
    (tmp_path / "package.json").write_text(json.dumps({
        "dependencies": {"next": "15", "react": "19"}, "devDependencies": {"tailwindcss": "4"},
        "scripts": {"dev": "next dev"}}))
    (tmp_path / "tailwind.config.ts").write_text("export default {}")
    m = wh.repo_map(str(tmp_path))
    assert m["framework"] == "Next.js" and m["styling"] == "Tailwind" and m["dev_command"] == "npm run dev"
    assert "tailwind.config.ts" in m["token_files"]
    assert wh.repo_map("") == {}


def test_turn_event_fails_open():
    assert wh.turn_event("http://127.0.0.1:9", "t", "prompt", {"client_session_id": "cc:1"}, 0.5) == {}


class _Server:
    """A fake gateway: answers /hooks/turn/{event} with a canned answer and records what it got."""

    def __init__(self, answers):
        seen = self.seen = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])) or b"{}")
                seen.append((self.path, body))
                answer = answers.get(self.path.rsplit("/", 1)[-1], {}) if self.path.startswith("/hooks/turn/") else {}
                data = json.dumps(answer).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *a):
                pass

        self.httpd = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def close(self):
        self.httpd.shutdown()


def _home(tmp_path, url):
    home = tmp_path / "home"
    (home / ".whales").mkdir(parents=True)
    (home / ".whales" / "token").write_text("tok")
    (home / ".whales" / "gateway").write_text(url)
    return home


def _run(event, payload, home, env=None):
    e = dict(os.environ, HOME=str(home), WHALES_UPDATE="off", WHALES_TURN="on")
    e.update(env or {})
    return subprocess.run([sys.executable, str(HOOK), "--event", event], input=json.dumps(payload),
                          capture_output=True, text=True, env=e, timeout=30)


def test_a_prompt_gets_the_servers_brief_and_writes_the_overflow(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    server = _Server({"prompt": {"context": "whales · rebuild\n\nDo first:\n- Critique the current screen",
                                 "notice": "whales: critique first", "decision": None,
                                 "files": [{"path": ".whales/brief.md", "content": "full"}], "trace_id": "t"}})
    try:
        r = _run("UserPromptSubmit", {"session_id": "s1", "cwd": str(repo), "prompt": "redesign the rule editor"},
                 _home(tmp_path, server.url))
    finally:
        server.close()
    out = json.loads(r.stdout)
    assert out["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
    assert "Critique the current screen" in out["hookSpecificOutput"]["additionalContext"]
    assert out["systemMessage"] == "whales: critique first"
    assert (repo / ".whales" / "brief.md").read_text() == "full"
    turn = [b for p, b in server.seen if p == "/hooks/turn/prompt"]
    assert turn and turn[0]["client_session_id"] == "cc:s1" and turn[0]["payload"]["prompt"] == "redesign the rule editor"
    assert turn[0]["event_id"] and turn[0]["deadline_s"] == wh.TURN_DEADLINES["prompt"]


def test_shadow_or_off_answers_leave_the_prompt_untouched(tmp_path):
    server = _Server({"prompt": {"context": None, "notice": None, "shadow": True}})
    try:
        r = _run("UserPromptSubmit", {"session_id": "s1", "prompt": "hi"}, _home(tmp_path, server.url))
    finally:
        server.close()
    assert r.returncode == 0 and r.stdout.strip() == ""


def test_whales_turn_off_sends_nothing(tmp_path):
    server = _Server({"prompt": {"context": "x"}})
    try:
        r = _run("UserPromptSubmit", {"session_id": "s1", "prompt": "hi"}, _home(tmp_path, server.url),
                 env={"WHALES_TURN": "off"})
    finally:
        server.close()
    assert "x" not in r.stdout
    assert not [p for p, _ in server.seen if p.startswith("/hooks/turn/")]


def test_session_start_merges_the_brief_after_the_binding(tmp_path):
    server = _Server({"session_start": {"context": "Where you left off: rebuild of the rule editor",
                                        "notice": "whales: picked up the rule editor rebuild"}})
    try:
        r = _run("SessionStart", {"session_id": "s2", "source": "compact"}, _home(tmp_path, server.url))
    finally:
        server.close()
    out = json.loads(r.stdout)
    ctx = out["hookSpecificOutput"]["additionalContext"]
    assert ctx.index("cc:s2") < ctx.index("Where you left off")
    assert out["systemMessage"] == "whales: picked up the rule editor rebuild"
    start = [b for p, b in server.seen if p == "/hooks/turn/session_start"]
    assert start and start[0]["payload"]["why"] == "compact"


def test_stop_honours_a_block(tmp_path):
    server = _Server({"stop": {"decision": {"block": True, "reason": "You didn't address the contrast failure."}}})
    try:
        r = _run("Stop", {"session_id": "s3", "last_assistant_message": "Done."}, _home(tmp_path, server.url))
    finally:
        server.close()
    out = json.loads(r.stdout)
    assert out["decision"] == "block" and "contrast" in out["reason"]
    stop = [b for p, b in server.seen if p == "/hooks/turn/stop"]
    assert stop[0]["payload"]["reply"] == "Done."


# --- what a Stop sends the grader --------------------------------------------------------------------------------

def _git_repo(path):
    path.mkdir()
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "-c", "user.email=a@b", "-c", "user.name=a", "commit", "-q",
                    "--allow-empty", "-m", "init"], check=True)
    return path


def test_screenshots_in_the_transcript():
    entries = [
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Bash", "input": {"command": "npx playwright screenshot http://localhost:3000 shots/pricing.png"}},
            {"type": "tool_use", "name": "Read", "input": {"file_path": "/tmp/after.png"}},
            {"type": "tool_use", "name": "mcp__playwright__browser_take_screenshot", "input": {"filename": "x.jpeg"}},
            {"type": "tool_use", "name": "Edit", "input": {"file_path": "/r/src/A.tsx"}},
        ]}},
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "content": [{"type": "image", "source": {}}]}]}},
    ]
    tools, paths = wh.screenshots_in_transcript(entries, "/r")
    assert tools[0].startswith("Bash: npx playwright") and "Read /tmp/after.png" in tools
    assert "mcp__playwright__browser_take_screenshot" in tools and "image returned to Claude" in tools
    assert "/r/shots/pricing.png" in paths and "/tmp/after.png" in paths and "/r/x.jpeg" in paths
    assert wh.screenshots_in_transcript([{"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": "Edit", "input": {"file_path": "/r/a.tsx"}}]}}], "/r") == ([], [])


def test_stop_evidence_sends_the_diff_new_files_and_this_turns_screenshots(tmp_path, monkeypatch):
    monkeypatch.setattr(wh, "TURNS_DIR", str(tmp_path / "turns"))
    repo = _git_repo(tmp_path / "repo")
    (repo / "old.png").write_bytes(b"old")
    os.utime(repo / "old.png", (1, 1))
    transcript = tmp_path / "t.jsonl"
    transcript.write_text(json.dumps({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": "Read", "input": {"file_path": "/tmp/earlier-turn.png"}}]}}) + "\n")
    wh.mark_turn_start("cc:s9", str(transcript))
    with transcript.open("a") as fh:
        fh.write(json.dumps({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Bash", "input": {"command": "screencapture shot.png"}}]}}) + "\n")
    (repo / "shot.png").write_bytes(b"\x89PNG fake")
    (repo / "src").mkdir()
    (repo / "src" / "Pricing.tsx").write_text("export const Pricing = () => <main />\n")
    out = wh.stop_evidence("cc:s9", str(repo), str(transcript))
    assert "diff --git a/src/Pricing.tsx b/src/Pricing.tsx" in out["diff"] and "+export const Pricing" in out["diff"]
    ev = out["evidence"]
    assert any(p.endswith("src/Pricing.tsx") for p in ev["new_files"])
    assert ev["screenshot_tools"] == ["Bash: screencapture shot.png"]
    assert [s["path"].rsplit("/", 1)[-1] for s in ev["screenshots"]] == ["shot.png"]
    assert ev["screenshots"][0]["media_type"] == "image/png" and ev["screenshots"][0]["data"]


def test_stop_evidence_never_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(wh, "TURNS_DIR", str(tmp_path / "turns"))
    out = wh.stop_evidence("cc:none", str(tmp_path / "missing"), "")
    assert out == {} or out["evidence"]["screenshots"] == []


def test_a_stop_posts_the_evidence(tmp_path):
    repo = _git_repo(tmp_path / "repo")
    home = None
    server = _Server({"prompt": {}, "stop": {"notice": "whales: checked 3 items, all pass"}})
    try:
        home = _home(tmp_path, server.url)
        _run("UserPromptSubmit", {"session_id": "s4", "cwd": str(repo), "prompt": "build pricing"}, home)
        (repo / "Page.tsx").write_text("<main/>\n")
        r = _run("Stop", {"session_id": "s4", "cwd": str(repo), "last_assistant_message": "Built it."}, home)
    finally:
        server.close()
    assert json.loads(r.stdout)["systemMessage"] == "whales: checked 3 items, all pass"
    stop = [b for p, b in server.seen if p == "/hooks/turn/stop"][0]
    assert "Page.tsx" in stop["payload"]["diff"] and stop["deadline_s"] == wh.TURN_DEADLINES["stop"]
    assert "screenshots" in stop["payload"]["evidence"]


# --- background jobs: a screenshot Claude opens, and the wake-up -------------------------------------------------

def test_opening_a_screenshot_sends_it_and_relays_where_the_result_will_be(tmp_path):
    shot = tmp_path / "screen.png"
    shot.write_bytes(b"\x89PNG fake")
    server = _Server({"edit": {"context": 'whales is measuring this screen (ref cb_1). Call whales(ref="cb_1").'}})
    try:
        home = _home(tmp_path, server.url)
        r = _run("ImageRead", {"session_id": "s5", "tool_name": "Read", "tool_use_id": "t1",
                               "tool_input": {"file_path": str(shot)}}, home)
        none = _run("ImageRead", {"session_id": "s5", "tool_name": "Read",
                                  "tool_input": {"file_path": str(tmp_path / "notes.md")}}, home)
    finally:
        server.close()
    out = json.loads(r.stdout)
    assert out["hookSpecificOutput"]["hookEventName"] == "PostToolUse"
    assert 'whales(ref="cb_1")' in out["hookSpecificOutput"]["additionalContext"]
    sent = [b for p, b in server.seen if p == "/hooks/turn/edit"]
    assert len(sent) == 1 and sent[0]["payload"]["kind"] == "image" and sent[0]["payload"]["image"]
    # A file that isn't an image: nothing sent, nothing printed, and never a capture.
    assert none.returncode == 0 and none.stdout.strip() == ""
    assert not [p for p, _ in server.seen if p == "/hooks/ingest"]


def test_the_wake_up_exits_2_with_the_result_and_0_with_nothing(tmp_path):
    server = _Server({"wake": {"context": 'whales finished: call whales(ref="cb_1")'}})
    try:
        r = _run("StopWake", {"session_id": "s6"}, _home(tmp_path, server.url))
    finally:
        server.close()
    assert r.returncode == 2 and 'whales(ref="cb_1")' in r.stderr
    server = _Server({"wake": {"context": None}})
    try:
        quiet = _run("StopWake", {"session_id": "s6"}, _home(tmp_path / "b", server.url))
        off = _run("StopWake", {"session_id": "s6"}, _home(tmp_path / "c", server.url), env={"WHALES_TURN": "off"})
    finally:
        server.close()
    assert quiet.returncode == 0 and off.returncode == 0 and not quiet.stdout.strip()


def test_hooks_json_runs_the_wake_up_in_the_background_with_a_timeout():
    hooks = json.loads((HOOK.parents[1] / "hooks" / "hooks.json").read_text())["hooks"]
    stops = [h for entry in hooks["Stop"] for h in entry["hooks"]]
    wake = [h for h in stops if h["args"] == ["--event", "StopWake"]][0]
    assert wake["asyncRewake"] is True and wake["timeout"] > wh.WAKE_TOTAL_S
    stop = [h for h in stops if h["args"] == ["--event", "Stop"]][0]
    assert stop["timeout"] > wh.TURN_DEADLINES["stop"] and not stop.get("asyncRewake")
    reads = [e for e in hooks["PostToolUse"] if e.get("matcher") == "Read"]
    assert reads and reads[0]["hooks"][0]["args"] == ["--event", "ImageRead"]


def test_the_wake_up_keeps_asking_while_jobs_are_pending(monkeypatch):
    answers = [{"context": None, "refs": [{"handle": "cb_1", "state": "pending"}]},
               {"context": None, "refs": [{"handle": "cb_1", "state": "pending"}]},
               {"context": 'ready: whales(ref="cb_1")', "refs": []}]
    calls = []
    monkeypatch.setattr(wh, "turn_event", lambda url, token, event, body, deadline: (calls.append(deadline),
                                                                                     answers.pop(0))[1])
    assert wh.stop_wake("t", "cc:1", "") == 2 and len(calls) == 3
    monkeypatch.setattr(wh, "turn_event", lambda *a: {"context": None, "refs": []})
    assert wh.stop_wake("t", "cc:1", "") == 0
