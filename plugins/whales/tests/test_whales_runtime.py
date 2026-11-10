"""Tests for runtime capture's decisions: which server, which routes, which
elements first, when it runs, and what it sends. The browser run itself is
exercised by hand against a real dev server (see the PR); these cover the
parts that fail silently.
"""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def _load(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


rt = _load("whales_runtime")
wh = _load("whales_hook")


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


class TestAddedClasses:
    def test_reads_classes_on_added_lines_only(self):
        patch = "\n".join([
            "+++ b/src/Panel.tsx",
            '+  <div className="rounded-md transition-colors duration-200 hover:bg-[#335869]/10">',
            '-  <div class="old-class">',
            "+  <span class={`px-2 ${active ? 'a' : 'b'}`} />",
        ])
        assert rt.added_classes(patch) == ["rounded-md", "transition-colors", "duration-200",
                                           "hover:bg-[#335869]/10", "px-2"]

    def test_empty_patch(self):
        assert rt.added_classes("") == []


class TestRoutes:
    def test_next_app_and_pages_files_map_to_their_routes(self, tmp_path):
        routes = rt.routes_for(str(tmp_path), ["app/(marketing)/pricing/page.tsx", "pages/about.tsx"])
        assert routes[0] == "/" and set(routes[1:]) == {"/pricing", "/about"}

    def test_dynamic_and_api_routes_are_skipped(self, tmp_path):
        assert rt.routes_for(str(tmp_path), ["app/post/[id]/page.tsx", "pages/api/x.ts", "pages/_app.tsx"]) == ["/"]

    def test_a_route_literal_naming_the_changed_component(self, tmp_path):
        write(tmp_path / "src" / "App.tsx", '<Route path="/compare" component={CompareFlow} />\n'
                                            '<Route path="/item/:id" component={CompareFlow} />\n'
                                            '<Route path="/other" component={Other} />\n')
        assert rt.routes_for(str(tmp_path), ["src/components/CompareFlow.tsx"]) == ["/", "/compare"]

    def test_repo_config_wins(self, tmp_path):
        write(tmp_path / ".whales" / "runtime.json", json.dumps({"routes": ["/ask", "/welcome"]}))
        assert rt.routes_for(str(tmp_path), ["src/x.tsx"]) == ["/ask", "/welcome"]

    def test_falls_back_to_the_root(self, tmp_path):
        assert rt.routes_for(str(tmp_path), ["src/styles.css"]) == ["/"]


class TestDevServer:
    def test_only_a_server_started_inside_this_repo(self, tmp_path, monkeypatch):
        repo, other = tmp_path / "repo", tmp_path / "repo-other"
        (repo / "web").mkdir(parents=True)
        other.mkdir()
        monkeypatch.delenv("WHALES_RUNTIME_URL", raising=False)
        monkeypatch.setattr(rt, "_serves_html", lambda url: True)
        servers = [("1", 3000, str(other)), ("2", 5173, str(repo / "web")), ("3", 5432, "/")]
        assert rt.find_dev_server(str(repo), servers) == "http://localhost:5173"
        # A sibling checkout whose path merely starts the same is not this repo.
        assert rt.find_dev_server(str(repo), [("1", 3000, str(other))]) == ""

    def test_a_port_that_does_not_serve_html_is_skipped(self, tmp_path, monkeypatch):
        monkeypatch.delenv("WHALES_RUNTIME_URL", raising=False)
        monkeypatch.setattr(rt, "_serves_html", lambda url: url.endswith(":5173/"))
        servers = [("1", 24678, str(tmp_path)), ("2", 5173, str(tmp_path))]
        assert rt.find_dev_server(str(tmp_path), servers) == "http://localhost:5173"

    def test_env_and_repo_config_override(self, tmp_path, monkeypatch):
        monkeypatch.setenv("WHALES_RUNTIME_URL", "http://localhost:9999/")
        assert rt.find_dev_server(str(tmp_path), []) == "http://localhost:9999"
        monkeypatch.delenv("WHALES_RUNTIME_URL")
        write(tmp_path / ".whales" / "runtime.json", json.dumps({"url": "http://localhost:4401"}))
        assert rt.find_dev_server(str(tmp_path), []) == "http://localhost:4401"


class TestRecipes:
    def test_loads_valid_recipes_by_name(self, tmp_path):
        write(tmp_path / ".whales" / "recipes" / "checkout.json", json.dumps({"route": "/cart", "steps": [{"do": "click", "selector": "button"}]}))
        write(tmp_path / ".whales" / "recipes" / "broken.json", "{not json")
        write(tmp_path / ".whales" / "recipes" / "nosteps.json", json.dumps({"route": "/"}))
        recipes = rt.load_recipes(str(tmp_path))
        assert [r["name"] for r in recipes] == ["checkout"]


class TestRunCapture:
    def test_no_dev_server_is_a_status_not_an_error(self, tmp_path, monkeypatch):
        monkeypatch.setattr(rt, "find_dev_server", lambda top: "")
        out = rt.run_capture({"repo": str(tmp_path), "out_dir": str(tmp_path / "out"), "capture_id": "x"})
        assert out["status"] == "no_dev_server"

    def test_no_node_is_a_status(self, tmp_path, monkeypatch):
        monkeypatch.setattr(rt, "_which", lambda name: "")
        assert rt.ensure_runtime() == ("", "node is not installed")

    def test_one_capture_per_repo_and_the_next_one_waits(self, tmp_path, monkeypatch):
        monkeypatch.setattr(rt, "CAPTURES_DIR", str(tmp_path / "captures"))
        monkeypatch.setattr(rt, "STATUS_FILE", str(tmp_path / "status.json"))
        ran, sent = [], []
        monkeypatch.setattr(rt, "run_capture", lambda job: ran.append(job["capture_id"]) or {"status": "ok"})
        monkeypatch.setattr(rt, "send_capture", lambda job, outcome: sent.append(job["capture_id"]))
        job = {"capture_id": "a", "repo": str(tmp_path)}
        job_file = tmp_path / "job.json"
        job_file.write_text(json.dumps(job))
        key = rt.hashlib.sha256(os.path.realpath(str(tmp_path)).encode()).hexdigest()[:16]
        lock = Path(rt.CAPTURES_DIR) / f".{key}.lock"
        lock.parent.mkdir(parents=True)
        lock.write_text("123")  # another capture of this repo is running
        rt.capture_job(str(job_file))
        assert ran == [] and (Path(rt.CAPTURES_DIR) / f".{key}.again").exists()
        lock.unlink()
        job_file.write_text(json.dumps({"capture_id": "b", "repo": str(tmp_path)}))
        rt.capture_job(str(job_file))
        assert ran == ["b", "a"] and sent == ["b", "a"]


class TestEvent:
    def test_a_large_capture_is_trimmed_to_fit(self):
        big = {"routes": [{"hover": {"results": [{"motion": [{"keyframes": ["x" * 1000] * 5}]} for _ in range(200)]},
                           "focus": {"results": []},
                           "scroll": {"scroll_linked": [{"samples": list(range(40))}]}}]}
        slim = rt.slim_for_event(big)
        assert slim["trimmed"] is True
        assert len(json.dumps(slim)) <= rt.MAX_EVENT_CHARS
        assert len(slim["routes"][0]["scroll"]["scroll_linked"][0]["samples"]) == 20

    def test_a_small_capture_is_sent_whole(self):
        small = {"routes": [{"load_motion": [{"name": "panelOpen"}]}]}
        assert rt.slim_for_event(small) is small

    def test_new_job_carries_the_turn(self):
        diff = {"head": "abc", "files": [{"path": "src/A.tsx"}], "patch": '+ <a className="duration-300" />'}
        job = rt.new_job("cc:s1", "/repo", diff)
        assert job["capture_id"].startswith("rc_") and job["files"] == ["src/A.tsx"]
        assert job["focus_classes"] == ["duration-300"] and job["turn_head"] == "abc"


class TestHookStartsIt:
    def test_switched_off_it_does_not_start(self, monkeypatch):
        monkeypatch.setenv("WHALES_RUNTIME", "off")
        assert wh.start_runtime_capture("cc:s1", {"repo": "/x", "files": [{"path": "a.tsx"}]}) is None

    def test_starts_detached_and_stamps_the_id(self, monkeypatch, tmp_path):
        monkeypatch.delenv("WHALES_RUNTIME", raising=False)
        started = []
        real = wh.importlib.util.spec_from_file_location

        def patched(name, path):
            spec = real(name, path)
            loader = spec.loader
            orig = loader.exec_module

            def exec_module(mod):
                orig(mod)
                mod.start_detached = lambda job: started.append(job)
            loader.exec_module = exec_module
            return spec
        monkeypatch.setattr(wh.importlib.util, "spec_from_file_location", patched)
        out = wh.start_runtime_capture("cc:s1", {"repo": str(tmp_path), "head": "h", "files": [{"path": "a.tsx"}], "patch": ""})
        assert out["status"] == "started" and out["capture_id"] == started[0]["capture_id"]
        assert started[0]["session_key"] == "cc:s1"
