#!/usr/bin/env python3
"""whales runtime capture — what the designer's UI actually does when it runs.

Started by the capture hook (detached) at the end of a turn that changed UI
files. Finds the dev server this repo is already running, works out which
routes the change touches, and runs ``runtime/capture.mjs`` (headless
Playwright) against them: motion as exact numbers, hover and focus states with
a check of whether the change is visible, scroll-linked motion, reduced
motion, a phone-sized layout, and the repo's own recipes
(``.whales/recipes/*.json``). The result is kept in ``~/.whales/captures`` with
an HTML report, and sent to whales as a ``RuntimeCapture`` event linked to
the turn.

Same rules as the hook: standard library only, never raises into the
designer's session, never starts a server, never writes to one (the capture
aborts every request that could). The browser is installed on first use into
``~/.whales/runtime``; its Chromium goes in Playwright's shared browser cache.
WHALES_RUNTIME=off turns it off.

usage:
  whales_runtime.py capture --job JOB.json   (what the hook runs)
  whales_runtime.py run [--cwd DIR] [--url URL] [--route /x ...]   (by hand; prints the report path)
  whales_runtime.py install
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import urllib.request
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_DIR = os.path.expanduser("~/.whales")
RUNTIME_DIR = os.path.join(CONFIG_DIR, "runtime")
CAPTURES_DIR = os.path.join(CONFIG_DIR, "captures")
STATUS_FILE = os.path.join(CONFIG_DIR, "runtime_status.json")
RUNTIME_SRC = os.path.join(os.path.dirname(HERE), "runtime")
RUNTIME_FILES = ("package.json", "package-lock.json", "recorder.js", "capture.mjs")

BUDGET_MS = 80_000          # the capture's own time budget
HARD_KILL_SECONDS = 120     # then it is killed, whatever it is doing
INSTALL_SECONDS = 300  # each install step; a failed download must not hang for long
LOCK_STALE_SECONDS = 300
MAX_ROUTES = 3
# Most capture JSON sent in one event; past it, the bulky parts are trimmed.
MAX_EVENT_CHARS = 600_000
# Ports a dev server commonly listens on, tried when the process table cannot be read.
COMMON_PORTS = (5173, 5174, 3000, 3001, 4321, 4200, 8080, 8000, 4401, 6006)
# Where node usually lives when a hook runs with a short PATH.
NODE_DIRS = ("/opt/homebrew/bin", "/usr/local/bin", os.path.expanduser("~/.volta/bin"),
             os.path.expanduser("~/.local/share/fnm/aliases/default/bin"))


def runtime_on() -> bool:
    return os.environ.get("WHALES_RUNTIME", "").lower() not in ("0", "off", "false", "no")


def _hook():
    """The capture hook module, for its snapshot, scrub and send."""
    spec = importlib.util.spec_from_file_location("whales_hook", os.path.join(HERE, "whales_hook.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def write_status(**fields) -> None:
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        tmp = f"{STATUS_FILE}.{os.getpid()}.tmp"
        with open(tmp, "w") as fh:
            json.dump({"at": _now(), **fields}, fh)
        os.replace(tmp, STATUS_FILE)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# The browser: Playwright pinned by the plugin's lockfile, in ~/.whales/runtime
# ---------------------------------------------------------------------------

def _which(name: str) -> str:
    found = shutil.which(name)
    if found:
        return found
    nvm = os.path.expanduser("~/.nvm/versions/node")
    dirs = list(NODE_DIRS)
    if os.path.isdir(nvm):
        dirs += [os.path.join(nvm, v, "bin") for v in sorted(os.listdir(nvm), reverse=True)]
    for d in dirs:
        path = os.path.join(d, name)
        if os.access(path, os.X_OK):
            return path
    return ""


def _runtime_env(node: str) -> dict:
    # Playwright's own browser cache: it keeps one folder per browser build, so
    # a designer who already has this build (any project's Playwright) downloads nothing.
    env = dict(os.environ)
    env.pop("PLAYWRIGHT_BROWSERS_PATH", None)
    env["PATH"] = os.path.dirname(node) + os.pathsep + env.get("PATH", "")
    return env


def _lock_hash() -> str:
    digest = hashlib.sha256()
    for name in ("package.json", "package-lock.json"):
        with open(os.path.join(RUNTIME_SRC, name), "rb") as fh:
            digest.update(fh.read())
    return digest.hexdigest()[:16]


def ensure_runtime():
    """``(node_path, "")`` once the browser is ready, else ``("", why)``.

    Copies the capture scripts every time (cheap, and a plugin update reaches
    them at once); installs Playwright and its headless Chromium only when the
    pinned versions changed. Never asks: the designer opted in by installing
    the plugin, and a missing browser just means no capture until it lands.
    """
    node, npm = _which("node"), _which("npm")
    if not node or not npm:
        return "", "node is not installed"
    try:
        os.makedirs(RUNTIME_DIR, exist_ok=True)
        for name in RUNTIME_FILES:
            shutil.copyfile(os.path.join(RUNTIME_SRC, name), os.path.join(RUNTIME_DIR, name))
    except OSError as exc:
        return "", f"runtime files could not be copied: {exc}"
    marker = os.path.join(RUNTIME_DIR, ".installed")
    want = _lock_hash()
    try:
        with open(marker) as fh:
            if fh.read().strip() == want:
                return node, ""
    except OSError:
        pass
    lock = os.path.join(RUNTIME_DIR, ".installing")
    if not _take_lock(lock, INSTALL_SECONDS):
        return "", "the browser is still being installed"
    try:
        env = _runtime_env(node)
        steps = ([npm, "ci", "--no-audit", "--no-fund", "--loglevel=error"],
                 [node, os.path.join(RUNTIME_DIR, "node_modules", "playwright", "cli.js"),
                  "install", "--only-shell", "chromium"])
        for cmd in steps:
            done = subprocess.run(cmd, cwd=RUNTIME_DIR, env=env, capture_output=True, text=True,
                                  timeout=INSTALL_SECONDS)
            if done.returncode != 0:
                return "", f"install failed: {(done.stderr or done.stdout).strip()[-300:]}"
        with open(marker, "w") as fh:
            fh.write(want)
        return node, ""
    except (OSError, subprocess.SubprocessError) as exc:
        return "", f"install failed: {exc}"[:300]
    finally:
        _drop_lock(lock)


def _take_lock(path: str, stale_seconds: int) -> bool:
    for _ in range(2):
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            return True
        except FileExistsError:
            try:
                if time.time() - os.path.getmtime(path) < stale_seconds:
                    return False
                os.remove(path)
            except OSError:
                return False
        except OSError:
            return False
    return False


def _drop_lock(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# The page: the dev server this repo is running, and the routes the change touches
# ---------------------------------------------------------------------------

def _repo_config(top: str) -> dict:
    try:
        with open(os.path.join(top, ".whales", "runtime.json")) as fh:
            value = json.load(fh)
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _serves_html(url: str) -> bool:
    try:
        req = urllib.request.Request(url, headers={"Accept": "text/html", "User-Agent": "whales-runtime"})
        with urllib.request.urlopen(req, timeout=2) as resp:
            return resp.status < 500 and "html" in (resp.headers.get("Content-Type") or "")
    except Exception:  # noqa: BLE001 — any failure means "not this one"
        return False


def listening_servers() -> list:
    """``[(pid, port, cwd)]`` for every TCP port a local process listens on."""
    lsof = shutil.which("lsof") or "/usr/sbin/lsof"
    try:
        out = subprocess.run([lsof, "-nP", "-iTCP", "-sTCP:LISTEN", "-Fpn"], capture_output=True,
                             text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    found, pid = [], None
    for line in out.splitlines():
        if line.startswith("p"):
            pid = line[1:]
        elif line.startswith("n") and pid:
            port = line.rsplit(":", 1)[-1]
            if port.isdigit():
                found.append((pid, int(port)))
    servers = []
    for pid, port in sorted(set(found), key=lambda x: x[1]):
        try:
            cwd_out = subprocess.run([lsof, "-a", "-p", pid, "-d", "cwd", "-Fn"], capture_output=True,
                                     text=True, timeout=5).stdout
        except (OSError, subprocess.SubprocessError):
            continue
        cwd = next((ln[1:] for ln in cwd_out.splitlines() if ln.startswith("n")), "")
        servers.append((pid, port, cwd))
    return servers


def find_dev_server(top: str, servers=None) -> str:
    """The base URL of a dev server started from inside this repo, or ``""``.

    Only one this repo started: a server from another checkout would be
    captured as if it showed this change. Never starts one.
    """
    override = os.environ.get("WHALES_RUNTIME_URL") or _repo_config(top).get("url")
    if override:
        return override.rstrip("/")
    real_top = os.path.realpath(top)
    servers = listening_servers() if servers is None else servers
    mine = [port for _, port, cwd in servers
            if cwd and (os.path.realpath(cwd) + os.sep).startswith(real_top + os.sep)]
    for port in sorted(set(mine), key=lambda p: (p not in COMMON_PORTS, p)):
        url = f"http://localhost:{port}"
        if _serves_html(url + "/"):
            return url
    return ""


_SRC_SUFFIXES = (".tsx", ".jsx", ".ts", ".js", ".vue", ".svelte", ".astro")
_ROUTE_LITERAL = re.compile(r"""\bpath\s*[=:]\s*\{?\s*["'`](/[^"'`]*)["'`]""")
_SKIP_DIRS = {"node_modules", ".git", "dist", "build", ".next", ".turbo", "coverage", ".whales"}


def _source_files(top: str, limit: int = 4000):
    count = 0
    for root, dirs, files in os.walk(top):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS and not d.startswith(".")]
        for name in files:
            if name.endswith(_SRC_SUFFIXES) and ".test." not in name and ".spec." not in name:
                yield os.path.join(root, name)
                count += 1
                if count >= limit:
                    return


def _file_route(rel: str) -> str:
    """The route a file-based router gives this file (Next.js app/ and pages/), or ``""``."""
    parts = rel.replace("\\", "/").split("/")
    for marker, page_only in (("app", True), ("pages", False)):
        if marker in parts:
            rest = parts[parts.index(marker) + 1:]
            stem = os.path.splitext(rest[-1])[0] if rest else ""
            if page_only and stem != "page":
                return ""
            if not page_only and (stem.startswith("_") or "api" in rest[:1]):
                return ""
            segs = [s for s in rest[:-1] if not s.startswith("(")] + ([] if stem in ("page", "index") else [stem])
            if any(s.startswith("[") for s in segs):
                return ""
            return "/" + "/".join(segs)
    return ""


def routes_for(top: str, changed: list) -> list:
    """Up to MAX_ROUTES routes that render the changed files, ``/`` first if unsure.

    Cheap, static and partial on purpose: a file-based route for a page file;
    otherwise a route literal (``path="/x"``) on a line that names the changed
    component. Routes with parameters are skipped: there is no id to fill in.
    """
    configured = _repo_config(top).get("routes")
    if isinstance(configured, list) and configured:
        return [r for r in configured if isinstance(r, str)][:MAX_ROUTES]
    routes = []
    stems = {os.path.splitext(os.path.basename(f))[0] for f in changed if f.endswith(_SRC_SUFFIXES)}
    stems -= {"index", "main", "App", "app", "page", "layout", "utils", "types"}
    for f in changed:
        r = _file_route(f)
        if r and r not in routes:
            routes.append(r)
    if stems and len(routes) < MAX_ROUTES:
        for path in _source_files(top):
            try:
                with open(path, errors="ignore") as fh:
                    text = fh.read()
            except OSError:
                continue
            if "path" not in text:
                continue
            for line in text.splitlines():
                m = _ROUTE_LITERAL.search(line)
                if not m or ":" in m.group(1) or "*" in m.group(1):
                    continue
                if any(re.search(rf"\b{re.escape(s)}\b", line) for s in stems) and m.group(1) not in routes:
                    routes.append(m.group(1))
            if len(routes) >= MAX_ROUTES:
                break
    if "/" not in routes and len(routes) < MAX_ROUTES:
        routes.insert(0, "/")
    return routes[:MAX_ROUTES]


_CLASS_ATTR = re.compile(r"""\b(?:class|className)\s*=\s*\{?\s*[`"']([^`"']+)""")


def added_classes(patch: str, limit: int = 60) -> list:
    """Class names on lines the turn added: the elements the capture looks at first."""
    seen = []
    for line in (patch or "").splitlines():
        if not line.startswith("+") or line.startswith("+++"):
            continue
        for m in _CLASS_ATTR.finditer(line):
            # A template literal's static part only: what follows ${ is code.
            for token in m.group(1).split("${")[0].split():
                if token not in seen:
                    seen.append(token)
    return seen[:limit]


def load_recipes(top: str) -> list:
    out = []
    folder = os.path.join(top, ".whales", "recipes")
    try:
        names = sorted(n for n in os.listdir(folder) if n.endswith(".json"))
    except OSError:
        return out
    for name in names[:10]:
        try:
            with open(os.path.join(folder, name)) as fh:
                recipe = json.load(fh)
        except (OSError, ValueError):
            continue
        if isinstance(recipe, dict) and isinstance(recipe.get("steps"), list):
            recipe.setdefault("name", os.path.splitext(name)[0])
            out.append(recipe)
    return out


# ---------------------------------------------------------------------------
# One capture
# ---------------------------------------------------------------------------

def slim_for_event(capture: dict) -> dict:
    """The capture as sent in the event: everything numeric, bulk trimmed to fit."""
    text = json.dumps(capture)
    if len(text) <= MAX_EVENT_CHARS:
        return capture
    slim = json.loads(text)
    for route in slim.get("routes", []):
        for item in (route.get("hover") or {}).get("results", []):
            for row in item.get("motion", []):
                row.pop("keyframes", None)
        for item in (route.get("scroll") or {}).get("scroll_linked", []):
            item["samples"] = item["samples"][::2]
    slim["trimmed"] = True
    text = json.dumps(slim)
    if len(text) > MAX_EVENT_CHARS:
        for route in slim.get("routes", []):
            for key in ("hover", "focus"):
                if route.get(key):
                    route[key]["results"] = route[key]["results"][:10]
    return slim


def run_capture(job: dict) -> dict:
    """Capture once. Returns the outcome; writes capture.json and report.html."""
    hook = _hook()
    top = job["repo"]
    out_dir = job["out_dir"]
    os.makedirs(out_dir, exist_ok=True)
    url = job.get("base_url") or find_dev_server(top)
    if not url:
        return {"status": "no_dev_server", "detail": "no dev server started from this repo is running"}
    node, why = ensure_runtime()
    if not node:
        return {"status": "no_browser", "detail": why}
    _, tree_before = hook.snapshot_tree(top)
    spec = {
        "base_url": url, "out_dir": out_dir, "budget_ms": BUDGET_MS,
        "routes": job.get("routes") or routes_for(top, job.get("files", [])),
        "focus_classes": job.get("focus_classes", []), "recipes": load_recipes(top),
    }
    job_file = os.path.join(out_dir, "job.json")
    with open(job_file, "w") as fh:
        json.dump(spec, fh)
    try:
        proc = subprocess.Popen([node, os.path.join(RUNTIME_DIR, "capture.mjs"), job_file], cwd=RUNTIME_DIR,
                                env=_runtime_env(node), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, start_new_session=True)
        try:
            _, err = proc.communicate(timeout=HARD_KILL_SECONDS)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.communicate()
            return {"status": "timeout", "detail": f"killed after {HARD_KILL_SECONDS}s", "base_url": url}
    except OSError as exc:
        return {"status": "error", "detail": str(exc)[:300], "base_url": url}
    try:
        with open(os.path.join(out_dir, "capture.json")) as fh:
            capture = json.load(fh)
    except (OSError, ValueError):
        return {"status": "error", "detail": (err or "no capture.json")[-300:], "base_url": url}
    _, tree_after = hook.snapshot_tree(top)
    # The next turn edited files while this one was being captured: the page may show either.
    capture["stale"] = bool(tree_before and tree_after and tree_before != tree_after)
    capture["ui_tree"] = tree_before
    capture["routes_requested"] = spec["routes"]
    with open(os.path.join(out_dir, "capture.json"), "w") as fh:
        json.dump(capture, fh, indent=1)
    return {"status": capture.get("status", "ok"), "capture": capture, "base_url": url,
            "report": os.path.join(out_dir, "report.html")}


def send_capture(job: dict, outcome: dict) -> None:
    """Send the outcome to whales as a RuntimeCapture event on the turn's session."""
    hook = _hook()
    token = hook._read(hook.TOKEN_FILE)
    if not token or os.environ.get("WHALES_CAPTURE", "").lower() in ("0", "off", "false", "no"):
        return
    raw = {
        "whales_event": "RuntimeCapture", "client_ts": hook.client_ts(),
        "capture_id": job["capture_id"], "turn_head": job.get("turn_head"), "cwd": job["repo"],
        "status": outcome["status"], "detail": outcome.get("detail"), "base_url": outcome.get("base_url"),
    }
    if outcome.get("capture"):
        raw["capture"] = slim_for_event(outcome["capture"])
    body = hook.scrub(json.dumps({"source": "claude_code_hook", "session_id": job.get("session_key"),
                                  "raw_payload": raw})).encode("utf-8")
    ok, status, reason = hook._send(hook.gateway_url(), token, body)
    hook.record_send_result(ok, status, reason)


def capture_job(job_path: str) -> int:
    with open(job_path) as fh:
        job = json.load(fh)
    top = job["repo"]
    key = hashlib.sha256(os.path.realpath(top).encode()).hexdigest()[:16]
    os.makedirs(CAPTURES_DIR, exist_ok=True)
    lock = os.path.join(CAPTURES_DIR, f".{key}.lock")
    again = os.path.join(CAPTURES_DIR, f".{key}.again")
    if not _take_lock(lock, LOCK_STALE_SECONDS):
        # One capture per repo at a time; the running one goes again when done.
        with open(again, "w") as fh:
            json.dump(job, fh)
        return 0
    try:
        while True:
            job.setdefault("out_dir", os.path.join(CAPTURES_DIR, key, job["capture_id"]))
            outcome = run_capture(job)
            write_status(capture_id=job["capture_id"], repo=top, status=outcome["status"],
                         detail=outcome.get("detail"), report=outcome.get("report"))
            send_capture(job, outcome)
            try:
                with open(again) as fh:
                    job = json.load(fh)
                os.remove(again)
            except (OSError, ValueError):
                break
    finally:
        _drop_lock(lock)
    return 0


def new_job(session_key: str, repo: str, diff: dict) -> dict:
    """The job the hook hands over at Stop."""
    capture_id = f"rc_{time.strftime('%Y%m%d%H%M%S', time.gmtime())}_{uuid.uuid4().hex[:6]}"
    return {"capture_id": capture_id, "session_key": session_key, "repo": repo, "turn_head": diff.get("head"),
            "files": [f["path"] for f in diff.get("files", [])], "focus_classes": added_classes(diff.get("patch", ""))}


def start_detached(job: dict) -> None:
    """Hand the job to a detached ``capture`` process; return at once."""
    os.makedirs(CAPTURES_DIR, exist_ok=True)
    path = os.path.join(CAPTURES_DIR, f".job-{job['capture_id']}.json")
    with open(path, "w") as fh:
        json.dump(job, fh)
    subprocess.Popen([sys.executable, os.path.abspath(__file__), "capture", "--job", path],
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True, close_fds=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd")
    c = sub.add_parser("capture")
    c.add_argument("--job", required=True)
    r = sub.add_parser("run")
    r.add_argument("--cwd", default=os.getcwd())
    r.add_argument("--url", default="")
    r.add_argument("--route", action="append", default=[])
    sub.add_parser("install")
    args = parser.parse_args()
    if args.cmd == "capture":
        return capture_job(args.job)
    if args.cmd == "install":
        node, why = ensure_runtime()
        print("ready" if node else why)
        return 0 if node else 1
    if args.cmd == "run":
        hook = _hook()
        repo = hook._repo(args.cwd)
        top = repo.get("top") or args.cwd
        key = hashlib.sha256(os.path.realpath(top).encode()).hexdigest()[:16]
        job = {"capture_id": f"rc_manual_{int(time.time())}", "repo": top, "base_url": args.url,
               "routes": args.route, "files": []}
        job["out_dir"] = os.path.join(CAPTURES_DIR, key, job["capture_id"])
        outcome = run_capture(job)
        print(json.dumps({k: v for k, v in outcome.items() if k != "capture"}))
        return 0
    parser.print_help()
    return 0


if __name__ == "__main__":
    try:
        code = main()
    except BaseException:  # noqa: BLE001 — never an error in the designer's session
        code = 0
    sys.exit(code or 0)
