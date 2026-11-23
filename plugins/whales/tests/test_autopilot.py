"""Whales Autopilot: the Stop / UserPromptSubmit nudges in the capture hook, and
the skill's autopilot_shoot.py helper.

The hook's one hard promise: a session that never called whales_autopilot
makes no network call for it. HTTP is mocked throughout; the browser tests run
only where Playwright and its Chromium are installed.
"""
from __future__ import annotations

import importlib.util
import io
import json
import subprocess
import sys
from pathlib import Path

import pytest

PLUGIN = Path(__file__).resolve().parents[1]
HOOK = PLUGIN / "scripts" / "whales_hook.py"
SHOOT = PLUGIN / "skills" / "whales-autopilot" / "autopilot_shoot.py"


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


wh = _load("whales_hook_autopilot", HOOK)
shoot = _load("autopilot_shoot", SHOOT)


class _Response:
    def __init__(self, body):
        self._body = json.dumps(body).encode()
        self.status = 200

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture
def gw(tmp_path, monkeypatch):
    """A configured machine, and a gateway whose /hooks/autopilot answer the test sets."""
    for attr, rel in (("CONFIG_DIR", ""), ("TOKEN_FILE", "token"), ("GATEWAY_FILE", "gateway"),
                      ("STATUS_FILE", "capture_status.json"), ("OFFSET_DIR", "offsets"),
                      ("SYNTHETIC_DIR", "synthetic"), ("AUTOPILOT_DIR", "autopilot")):
        monkeypatch.setattr(wh, attr, str(tmp_path / rel) if rel else str(tmp_path))
    (tmp_path / "token").write_text("tok")
    (tmp_path / "gateway").write_text("https://gw.test")

    class Gateway:
        answer = {"waiting_host": False, "waiting_person": False, "questions": []}
        calls = []
        down = False

    def urlopen(request, timeout=None):
        url = request.full_url if hasattr(request, "full_url") else request
        Gateway.calls.append({"url": url, "timeout": timeout,
                              "auth": request.get_header("Authorization") if hasattr(request, "get_header") else None})
        if Gateway.down:
            raise wh.urllib.error.URLError("down")
        return _Response(Gateway.answer)

    Gateway.calls = []
    monkeypatch.setattr(wh.urllib.request, "urlopen", urlopen)
    return Gateway


def _mark(session="s1", **state):
    wh._write_json(wh._autopilot_marker(session), {"run_id": None, "blocks": 0, **state})


def _waiting_host(run="r1"):
    return {"run_id": run, "status": "waiting_host", "waiting_host": True, "waiting_person": False, "questions": []}


class TestOnlyAutopilotSessionsCheck:
    def test_no_marker_no_network_on_stop_or_prompt(self, gw):
        assert wh.autopilot_output("Stop", {"session_id": "s1"}, "s1", "cc") == {}
        assert wh.autopilot_output("UserPromptSubmit", {"session_id": "s1"}, "s1", "cc") == {}
        assert gw.calls == []

    def test_the_autopilot_tool_call_writes_the_marker(self, gw):
        wh.note_autopilot_call({"session_id": "s1", "tool_name": "mcp__plugin_whales_whales__universal_critique"})
        assert not Path(wh._autopilot_marker("s1")).exists()
        wh.note_autopilot_call({"session_id": "s1", "tool_name": "mcp__plugin_whales_whales__whales_autopilot"})
        assert json.loads(Path(wh._autopilot_marker("s1")).read_text()) == {"run_id": None, "blocks": 0}

    def test_a_tool_call_resets_the_block_count_and_keeps_the_run(self, gw):
        _mark(run_id="r1", blocks=2)
        wh.note_autopilot_call({"session_id": "s1", "tool_name": "mcp__whales__whales_autopilot"})
        assert json.loads(Path(wh._autopilot_marker("s1")).read_text()) == {"run_id": "r1", "blocks": 0}

    def test_the_tool_gets_the_session_id_injected(self):
        out = wh.session_id_injection({"tool_name": "mcp__whales__whales_autopilot", "session_id": "s1",
                                       "tool_input": {"action": "next"}}, "cc")
        assert out["hookSpecificOutput"]["updatedInput"] == {"action": "next", "client_session_id": "cc:s1"}


class TestStop:
    def test_a_waiting_move_blocks_the_stop(self, gw):
        _mark()
        gw.answer = _waiting_host("r1")
        out = wh.autopilot_output("Stop", {"session_id": "s1"}, "s1", "cc")
        assert out == {"decision": "block", "reason": "Whales autopilot run r1 has a move waiting. Call "
                       "whales_autopilot with action=next (follow the whales-autopilot skill)."}
        call, = gw.calls
        assert call["url"] == "https://gw.test/hooks/autopilot?client_session_id=cc%3As1"
        assert call["timeout"] == 2
        assert call["auth"] == "Bearer tok"

    def test_stop_hook_active_never_blocks_or_calls(self, gw):
        _mark()
        gw.answer = _waiting_host()
        assert wh.autopilot_output("Stop", {"session_id": "s1", "stop_hook_active": True}, "s1", "cc") == {}
        assert gw.calls == []

    def test_three_consecutive_blocks_then_the_stop_goes_through(self, gw):
        _mark()
        gw.answer = _waiting_host("r1")
        outs = [wh.autopilot_output("Stop", {"session_id": "s1"}, "s1", "cc") for _ in range(4)]
        assert [bool(o) for o in outs] == [True, True, True, False]

    def test_the_count_resets_once_the_run_is_not_waiting_on_the_host(self, gw):
        _mark(run_id="r1", blocks=3)
        gw.answer = {"run_id": "r1", "status": "working", "waiting_host": False, "waiting_person": False,
                     "questions": []}
        assert wh.autopilot_output("Stop", {"session_id": "s1"}, "s1", "cc") == {}
        gw.answer = _waiting_host("r1")
        assert wh.autopilot_output("Stop", {"session_id": "s1"}, "s1", "cc")["decision"] == "block"

    def test_a_new_run_starts_its_own_count(self, gw):
        _mark(run_id="r1", blocks=3)
        gw.answer = _waiting_host("r2")
        assert wh.autopilot_output("Stop", {"session_id": "s1"}, "s1", "cc")["decision"] == "block"

    def test_any_gateway_error_lets_the_stop_through(self, gw):
        _mark(run_id="r1", blocks=1)
        gw.down = True
        assert wh.autopilot_output("Stop", {"session_id": "s1"}, "s1", "cc") == {}
        assert json.loads(Path(wh._autopilot_marker("s1")).read_text())["blocks"] == 1

    def test_the_fail_open_shape_does_nothing(self, gw):
        _mark()
        assert wh.autopilot_output("Stop", {"session_id": "s1"}, "s1", "cc") == {}

    def test_an_ended_run_drops_the_marker_so_later_stops_make_no_call(self, gw):
        _mark(run_id="r1")
        gw.answer = {"run_id": "r1", "status": "done", "waiting_host": False, "waiting_person": False,
                     "questions": []}
        assert wh.autopilot_output("Stop", {"session_id": "s1"}, "s1", "cc") == {}
        assert not Path(wh._autopilot_marker("s1")).exists()
        wh.autopilot_output("Stop", {"session_id": "s1"}, "s1", "cc")
        assert len(gw.calls) == 1

    def test_no_token_no_call(self, gw, tmp_path):
        _mark()
        (tmp_path / "token").unlink()
        assert wh.autopilot_output("Stop", {"session_id": "s1"}, "s1", "cc") == {}
        assert gw.calls == []


class TestUserPromptSubmit:
    QUESTIONS = {"run_id": "r9", "status": "waiting_person", "waiting_host": False, "waiting_person": True,
                 "questions": [{"question": "Who signs off refunds?", "assumed": "the agent"},
                               {"question": "Is mobile in scope?"}]}

    def test_waiting_person_adds_the_questions(self, gw):
        _mark()
        gw.answer = self.QUESTIONS
        out = wh.autopilot_output("UserPromptSubmit", {"session_id": "s1"}, "s1", "cc")
        ctx = out["hookSpecificOutput"]["additionalContext"]
        assert out["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
        assert "r9" in ctx and "Who signs off refunds?" in ctx and "Is mobile in scope?" in ctx
        assert "whales_autopilot steer kind=answer" in ctx

    def test_a_prompt_never_blocks(self, gw):
        _mark()
        gw.answer = _waiting_host()
        assert wh.autopilot_output("UserPromptSubmit", {"session_id": "s1"}, "s1", "cc") == {}

    def test_merges_with_the_product_question(self):
        notice = {"systemMessage": "update", "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit", "additionalContext": "Are you on Stuut?"}}
        extra = {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": "run r9 waits"}}
        out = wh.merge_autopilot(notice, extra)
        assert out["systemMessage"] == "update"
        assert out["hookSpecificOutput"]["additionalContext"] == "Are you on Stuut?\n\nrun r9 waits"
        assert wh.merge_autopilot(notice, {}) is notice


class TestThroughMain:
    def _main(self, monkeypatch, capsys, event, payload, source="claude_code_hook"):
        sent = []
        monkeypatch.setattr(wh, "check_for_update", lambda: None)
        monkeypatch.setattr(wh, "restart_notice", lambda sid: None)
        monkeypatch.setattr(wh, "pending_question", lambda *a: "")
        monkeypatch.setattr(wh, "memory_on", lambda: False)
        monkeypatch.setattr(wh, "turn_diff_on", lambda: False)
        monkeypatch.setattr(wh, "post_detached", lambda *a, **k: sent.append(a))
        monkeypatch.setattr(sys, "argv", ["whales_hook.py", "--event", event, "--source", source])
        monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
        assert wh.main() == 0
        lines = capsys.readouterr().out.strip().splitlines()
        return (json.loads(lines[-1]) if lines else None), sent

    def test_stop_blocks_and_still_ships_the_event(self, gw, monkeypatch, capsys):
        _mark()
        gw.answer = _waiting_host("r1")
        out, sent = self._main(monkeypatch, capsys, "Stop", {"session_id": "s1"})
        assert out["decision"] == "block"
        assert len(sent) == 1, "the Stop event is captured as before"

    def test_a_session_without_autopilot_makes_no_autopilot_call(self, gw, monkeypatch, capsys):
        out, sent = self._main(monkeypatch, capsys, "Stop", {"session_id": "s1"})
        assert out is None
        assert not [c for c in gw.calls if "/hooks/autopilot" in c["url"]]
        assert len(sent) == 1

    def test_pretooluse_on_the_tool_marks_the_session(self, gw, monkeypatch, capsys):
        monkeypatch.setattr(wh, "mark_whales_project", lambda e: None)
        out, _ = self._main(monkeypatch, capsys, "PreToolUse", {
            "session_id": "s1", "tool_name": "mcp__whales__whales_autopilot", "tool_input": {"action": "start"}})
        assert out["hookSpecificOutput"]["updatedInput"]["client_session_id"] == "cc:s1"
        assert Path(wh._autopilot_marker("s1")).exists()

    def test_cursor_is_left_alone(self, gw, monkeypatch, capsys):
        _mark()
        gw.answer = _waiting_host()
        out, _ = self._main(monkeypatch, capsys, "Stop", {"session_id": "s1"}, source="cursor_hook")
        assert out is None
        assert not [c for c in gw.calls if "/hooks/autopilot" in c["url"]]


def test_hooks_json_routes_the_tool_through_pretooluse():
    hooks = json.loads((PLUGIN / "hooks" / "hooks.json").read_text())
    assert "whales_autopilot" in hooks["hooks"]["PreToolUse"][0]["matcher"]


# --- autopilot_shoot.py -------------------------------------------------------

class TestShootArgs:
    def test_viewport(self):
        assert shoot.parse_viewport("390x844") == (390, 844)
        assert shoot.parse_viewport(None) == (1800, 1000)
        with pytest.raises(ValueError):
            shoot.parse_viewport("wide")

    def test_targets(self, tmp_path):
        page = tmp_path / "w2.html"
        page.write_text("<html></html>")
        assert shoot.to_url(str(page)).startswith("file://") and shoot.to_url(str(page)).endswith("/w2.html")
        assert shoot.to_url("/inbox", "http://localhost:3000/app") == "http://localhost:3000/inbox"
        assert shoot.to_url("https://x.test/a") == "https://x.test/a"

    def test_frames(self, tmp_path):
        assert shoot.parse_frames(None, "http://localhost:3000") == [
            {"name": "board", "url": "http://localhost:3000", "selector": None}]
        frames = shoot.parse_frames('["empty", {"name": "detail", "url_or_path": "/i/1", "selector": "main"}]',
                                    "http://localhost:3000/")
        assert frames == [{"name": "empty", "url": "http://localhost:3000/", "selector": None},
                          {"name": "detail", "url": "http://localhost:3000/i/1", "selector": "main"}]
        spec = tmp_path / "frames.json"
        spec.write_text('[{"name": "a"}]')
        assert shoot.parse_frames(str(spec), "http://h/")[0]["name"] == "a"
        with pytest.raises(ValueError):
            shoot.parse_frames("[]", "http://h/")

    def test_walks_merge_in_order_once(self):
        assert shoot.merge_walks([{"pressed": ["A", "B"], "dead": ["B"]}, {"pressed": ["B", "C"], "dead": ["C"]}]) == {
            "pressed": ["A", "B", "C"], "dead": ["B", "C"]}

    def test_one_frame_keeps_the_engines_shape(self):
        assert shoot.per_frame([("board", {"x": 1})]) == {"x": 1}
        assert shoot.per_frame([("a", 1), ("b", 2)]) == {"a": 1, "b": 2}


class TestShootUpload:
    def test_reads_the_source_id(self, tmp_path, monkeypatch):
        helper = tmp_path / "critique_source.py"
        helper.write_text("import json, sys\nassert sys.argv[1:3] == ['upload', '/x.png']\n"
                          "print(json.dumps({'source_id': 'abc.png', 'filename': 'x.png'}))\n")
        monkeypatch.setattr(shoot, "upload_helper", lambda: str(helper))
        assert shoot.upload("/x.png") == {"source_id": "abc.png"}

    def test_relays_the_helpers_error(self, tmp_path, monkeypatch):
        helper = tmp_path / "critique_source.py"
        helper.write_text("import json, sys\nprint(json.dumps({'status': 'error', 'detail': 'No whales token'}))\n"
                          "sys.exit(1)\n")
        monkeypatch.setattr(shoot, "upload_helper", lambda: str(helper))
        assert shoot.upload("/x.png") == {"error": "No whales token"}

    def test_finds_the_universal_critique_helper(self):
        assert shoot.upload_helper().endswith("universal-critique/critique_source.py")


def test_without_playwright_it_exits_2_with_the_install_commands(monkeypatch, capsys):
    monkeypatch.setitem(sys.modules, "playwright", None)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", None)
    with pytest.raises(SystemExit) as exc:
        shoot.main(["--url", "http://localhost:3000", "--no-upload"])
    assert exc.value.code == 2
    assert "pip install playwright && python -m playwright install chromium" in capsys.readouterr().out


def _chromium_ready() -> bool:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return False
    try:
        with sync_playwright() as pw:
            pw.chromium.launch().close()
        return True
    except Exception:  # noqa: BLE001
        return False


needs_browser = pytest.mark.skipif(not _chromium_ready(), reason="Playwright + Chromium not installed")

PAGE = """<html><body><div id="window"><p id="c">0</p>
<button onclick="document.getElementById('c').textContent='1'">Add one</button>
<button>Nothing</button></div></body></html>"""


def _run_shoot(*args):
    done = subprocess.run([sys.executable, str(SHOOT), *args, "--no-upload"], capture_output=True, text=True,
                          timeout=120)
    return done.returncode, json.loads(done.stdout.strip().splitlines()[-1])


@needs_browser
def test_shoots_measures_and_finds_the_dead_button(tmp_path):
    page = tmp_path / "w1.html"
    page.write_text(PAGE)
    lint = tmp_path / "lint.js"
    lint.write_text("() => ({buttons: document.querySelectorAll('button').length})")
    code, out = _run_shoot("--url", str(page), "--viewport", "390x844", "--lint-js", str(lint), "--walk",
                           "--out", str(tmp_path / "out"))
    assert code == 0
    assert out["measurements"] == {"lint": {"buttons": 2}, "walk": {"pressed": ["Add one", "Nothing"],
                                                                   "dead": ["Nothing"]}}
    assert Path(out["frames"][0]["png"]).read_bytes()[:4] == b"\x89PNG"
    assert "panel" not in out


@needs_browser
def test_a_panel_press_reports_whether_the_screen_changed(tmp_path):
    page = tmp_path / "w1.html"
    page.write_text(PAGE)
    _, live = _run_shoot("--url", str(page), "--press", "Add one", "--out", str(tmp_path / "a"))
    _, dead = _run_shoot("--url", str(page), "--press", "Nothing", "--out", str(tmp_path / "b"))
    _, missing = _run_shoot("--url", str(page), "--press", "Refund", "--out", str(tmp_path / "c"))
    assert live["panel"] == {"pressed": "Add one", "changed": True}
    assert dead["panel"] == {"pressed": "Nothing", "changed": False}
    assert missing["panel"]["changed"] is False and "Refund" in missing["panel"]["error"]
