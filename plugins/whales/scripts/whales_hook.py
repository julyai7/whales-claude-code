#!/usr/bin/env python3
"""Whales capture hook — forwards one Claude Code hook event to the gateway.

Runs on the DESIGNER'S machine, invoked by their editor on every turn, so the
constraints here are different from the rest of the product:

- **Standard library only.** A designer should not have to install anything
  for a hook to work, and a missing dependency would turn every hook firing
  into an error in their session.
- **Always exits 0.** A non-zero exit can block a tool call or feed stderr
  back to the model. Capture is observation, never enforcement: if the
  gateway is down, the designer should notice nothing at all.
- **Never blocks the turn.** The POST happens in a detached grandchild
  process, so the hook itself returns in milliseconds. This is the main
  difference from the original adapter, which POSTed synchronously with a 5s
  timeout — acceptable for occasional events, not for something on every
  prompt and every file write.

Credential: ``~/.whales/token``, written by the installer. Deliberately a file
rather than an argv or an env var — ``${user_config.*}`` only interpolates
into a hook's exec-form ``args``, which would put the token in ``ps`` output,
and an env var would mean asking the designer to edit a shell profile, which
is the friction the one-command install exists to remove. The same file also
serves the Cursor hooks: the installer copies this script to a stable path
(outside the Claude Code plugin cache, which is version-pinned and would
break a hardcoded reference on every plugin update) and Cursor's own
hooks.json invokes it directly with ``--source cursor_hook``.

Cursor also auto-discovers and runs Claude Code plugins' hooks.json on its
own — undocumented, and it forwards only ``command``, never the exec-form
``args``, so an auto-discovered firing never carries ``--event``. Rather than
special-case that, ``--event`` is optional and a firing without it silently
no-ops: real capture only ever happens through the ``--event``-carrying
invocation that Cursor's own hooks.json (or Claude Code's plugin hooks.json)
supplies.

Why hooks exist at all: an MCP server can only see its own tool calls. A
design the agent writes straight to disk with the native Write tool is
completely invisible to it. That is the gap this closes, and it cannot be
closed from the server side by any transport choice.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request

TIMEOUT_SECONDS = 8
CONFIG_DIR = os.path.expanduser("~/.whales")
TOKEN_FILE = os.path.join(CONFIG_DIR, "token")
GATEWAY_FILE = os.path.join(CONFIG_DIR, "gateway")
OFFSET_DIR = os.path.join(CONFIG_DIR, "offsets")

DEFAULT_GATEWAY = "https://mcp.gojuly.ai"

# Host session ids and the MCP transport's own session id are different
# identifier spaces. Namespacing keeps two unrelated sessions from colliding
# on a shared id — including sessions from two different HOSTS, which is why
# this is keyed by --source rather than a single constant — and the
# SessionStart binding (see _session_start_context) is what lets the backend
# join the id-and-transport buckets back together within one host.
SESSION_PREFIX = {"claude_code_hook": "cc", "cursor_hook": "cur"}

# Whales tools that take client_session_id. Matched on the bare name because
# Claude Code prefixes mcp__…__ and Cursor uses MCP:<name>.
SESSION_ID_TOOLS = frozenset({
    "whales", "ask_whales", "record_reaction", "get_design_profile",
    "search_design_history", "submit_design", "record_critique", "record_approval",
    "universal_critique", "list_design_systems", "get_design_system",
    "register_design_system", "generate_design_system", "extract_figma",
    "product_context",
})

# Keys that may carry a whole file. Kept but truncated: the point of capturing
# an edit is knowing which values changed, and a multi-megabyte file would
# bloat the store without adding signal.
_BULKY_KEYS = ("content", "new_string", "old_string", "prompt", "response", "output")
_MAX_FIELD_CHARS = 20_000

# Most bytes we will ship from a transcript in one call. A long session's
# JSONL grows without bound, and a single turn should never mail a 40MB file.
_MAX_TRANSCRIPT_BYTES = 512_000

# Secret shapes, scrubbed before anything leaves the machine. This is not a
# guarantee — no regex is — but a capture pipeline that vacuums up API keys is
# a liability for us as much as for the designer, and the common shapes are
# cheap to catch. Ordered longest-prefix-first so a more specific pattern wins.
_SECRET_PATTERNS = [
    re.compile(r"(?i)\b(sk-ant-[A-Za-z0-9_\-]{16,})"),
    re.compile(r"\b(sk-[A-Za-z0-9]{20,})"),
    re.compile(r"\b(gh[pousr]_[A-Za-z0-9]{16,})"),
    re.compile(r"\b(github_pat_[A-Za-z0-9_]{20,})"),
    re.compile(r"\b(xox[abprs]-[A-Za-z0-9\-]{10,})"),
    re.compile(r"\b(AKIA[0-9A-Z]{16})\b"),
    re.compile(r"\b(whales_(?:agent|mcp_at|mcp_rt)_[A-Za-z0-9_\-]{16,})"),
    re.compile(r"(?i)\b(bearer\s+[A-Za-z0-9._\-]{20,})"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
    # KEY=value where the key name looks secret-ish (.env lines, shell exports)
    re.compile(
        r"(?i)\b([A-Z0-9_]*(?:SECRET|TOKEN|PASSWORD|PASSWD|API_?KEY|PRIVATE_?KEY|CREDENTIAL)[A-Z0-9_]*\s*[=:]\s*)"
        r"['\"]?([^\s'\"]{8,})['\"]?"
    ),
]

_REDACTED = "[redacted-by-whales]"


def scrub(text: str) -> str:
    """Replace credential-shaped substrings. Kept as one function over raw
    text (rather than per-field) because a transcript is JSON-in-JSON: a
    secret can appear inside a tool result that is itself a JSON string, where
    no structural walk would find it."""
    if not text:
        return text
    for pattern in _SECRET_PATTERNS:
        if pattern.groups >= 2:
            text = pattern.sub(lambda m: f"{m.group(1)}{_REDACTED}", text)
        else:
            text = pattern.sub(_REDACTED, text)
    return text


def truncate(payload):
    """Recursively cap bulky string fields, recording that it happened rather
    than doing it silently — a downstream reader must be able to tell a short
    edit from a trimmed one."""
    if isinstance(payload, dict):
        out = {}
        for k, v in payload.items():
            if k in _BULKY_KEYS and isinstance(v, str) and len(v) > _MAX_FIELD_CHARS:
                out[k] = v[:_MAX_FIELD_CHARS]
                out[f"{k}_truncated_from"] = len(v)
            else:
                out[k] = truncate(v)
        return out
    if isinstance(payload, list):
        return [truncate(v) for v in payload]
    return payload


def _read(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError:
        return ""


def gateway_url() -> str:
    return (_read(GATEWAY_FILE) or DEFAULT_GATEWAY).rstrip("/")


def _offset_path(session_id: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", session_id or "unknown")
    return os.path.join(OFFSET_DIR, f"{safe}.offset")


def transcript_delta(transcript_path: str, session_id: str):
    """The bytes appended to the transcript since we last shipped, plus the
    new offset.

    Delta rather than whole-file: a transcript grows monotonically and is read
    on every hook firing, so mailing all of it each turn would re-send the
    same session over and over — quadratic traffic for linear work.

    Returns ``(text, new_offset, truncated)``. A file that SHRANK (a new
    session reusing an id, or a rotated file) resets to 0 rather than seeking
    past the end and shipping nothing forever.
    """
    if not transcript_path or not os.path.isfile(transcript_path):
        return "", None, False
    try:
        size = os.path.getsize(transcript_path)
        start = 0
        raw = _read(_offset_path(session_id))
        if raw.isdigit():
            start = int(raw)
        if start > size:
            start = 0
        if start == size:
            return "", None, False

        truncated = False
        if size - start > _MAX_TRANSCRIPT_BYTES:
            # Keep the TAIL, not the head: the most recent turns are the ones
            # this event is about.
            start = size - _MAX_TRANSCRIPT_BYTES
            truncated = True

        with open(transcript_path, "r", encoding="utf-8", errors="replace") as fh:
            fh.seek(start)
            text = fh.read()
        return text, size, truncated
    except OSError:
        return "", None, False


def save_offset(session_id: str, offset) -> None:
    if offset is None:
        return
    try:
        os.makedirs(OFFSET_DIR, exist_ok=True)
        with open(_offset_path(session_id), "w", encoding="utf-8") as fh:
            fh.write(str(offset))
    except OSError:
        pass


def post_detached(url: str, token: str, body: bytes) -> None:
    """Send in a detached grandchild so the hook returns immediately.

    Double-fork so the intermediate child exits at once and the grandchild is
    reparented to init — otherwise the editor could still be waiting on a
    process it did not know it spawned. On any platform without fork we fall
    back to a short blocking send, which is still bounded by the timeout.
    """
    if not hasattr(os, "fork"):
        _send(url, token, body)
        return
    try:
        if os.fork() != 0:
            return  # parent: done, hook exits now
    except OSError:
        _send(url, token, body)
        return
    try:
        os.setsid()
        if os.fork() != 0:
            os._exit(0)
    except OSError:
        pass
    try:
        devnull = os.open(os.devnull, os.O_RDWR)
        os.dup2(devnull, 0)
        os.dup2(devnull, 1)
        os.dup2(devnull, 2)
    except OSError:
        pass
    try:
        _send(url, token, body)
    finally:
        os._exit(0)


def _send(url: str, token: str, body: bytes) -> None:
    request = urllib.request.Request(
        f"{url}/hooks/ingest",
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS):
            pass
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, TimeoutError):
        # Swallowed on purpose: our availability must never be the designer's
        # problem. A dropped event is strictly better than a stalled turn.
        pass


def _session_start_context(session_id: str, capturing: bool, prefix: str) -> str:
    """What SessionStart injects into the model's context.

    This is the fix for the one gap an MCP server structurally cannot close on
    its own: the host's session id and the MCP transport's ``mcp-session-id``
    are different identifier spaces, so a design submitted through
    ``submit_design`` and then hand-edited with the native Write tool land in
    two buckets that can never be joined. Telling the model the host id, and
    to pass it on every Whales call, is what makes one session one story —
    and it is why outcome derivation (submission then approval == accepted)
    can work at all.
    """
    lead = (
        "Whales capture is active for this session."
        if capturing
        # Said plainly rather than omitted: the binding below is still worth
        # doing (tool calls work off the plugin's own credential, which is
        # stored separately from this file), but claiming capture is running
        # when it is not would make a silent misconfiguration look healthy.
        else "Whales is connected, but local capture is not configured on this "
        "machine, so file edits made outside the Whales tools are not being "
        "recorded."
    )
    host = "Cursor" if prefix == "cur" else "Claude Code"
    return (
        f"{lead} Its {host} session id is `{prefix}:{session_id}`. "
        f"Pass that exact string as the `client_session_id` argument on every "
        f"Whales MCP tool call you make in this session, so tool calls and file "
        f"edits are recorded as one piece of work rather than two unrelated ones."
    )


def _tool_input(event: dict):
    tool_input = event.get("tool_input")
    if isinstance(tool_input, str):
        try:
            tool_input = json.loads(tool_input)
        except ValueError:
            return None
    return tool_input if isinstance(tool_input, dict) else None


def _bare_tool(tool: str) -> str:
    return tool.rsplit(":", 1)[-1].rsplit("__", 1)[-1]


def session_id_injection(event: dict, prefix: str):
    """The PreToolUse output that adds ``client_session_id`` to a Whales call,
    or ``None`` when there is nothing to do.

    Asking the model to pass the id did not work: the review behind this
    change found it on 4 of 150 tool calls, so reactions could not be joined
    to the designs they were about. Setting it here makes it deterministic.

    ``updatedInput`` replaces the tool's input rather than merging into it, so
    every original argument is echoed back. ``permissionDecision`` is left out
    on purpose — setting it would override the designer's own allow/ask rules
    for these tools, which is not this hook's business.
    """
    tool = event.get("tool_name") or ""
    tool_input = event.get("tool_input")
    session_id = event.get("session_id") or ""
    if not tool.startswith("mcp__") or not session_id or not isinstance(tool_input, dict):
        return None
    if tool.rsplit("__", 1)[-1] not in SESSION_ID_TOOLS:
        return None
    wanted = f"{prefix}:{session_id}"
    if tool_input.get("client_session_id") == wanted:
        return None
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "updatedInput": {**tool_input, "client_session_id": wanted},
        }
    }


def cursor_session_start_output(session_id: str, capturing: bool, prefix: str) -> dict:
    """Cursor's sessionStart output. ``additional_context`` is the documented
    field; ``env`` is the one later hooks can read even when that context is
    dropped. Claude Code's ``hookSpecificOutput`` shape is not this contract.
    """
    return {
        "additional_context": _session_start_context(session_id, capturing, prefix),
        "env": {"WHALES_CLIENT_SESSION_ID": f"{prefix}:{session_id}"},
    }


def cursor_session_id_injection(event: dict, prefix: str):
    """Cursor preToolUse uses ``updated_input``, not Claude Code's wrapper."""
    tool = event.get("tool_name") or ""
    tool_input = _tool_input(event)
    session_id = event.get("session_id") or event.get("conversation_id") or ""
    if _bare_tool(tool) not in SESSION_ID_TOOLS or not session_id or tool_input is None:
        return None
    wanted = f"{prefix}:{session_id}"
    if tool_input.get("client_session_id") == wanted:
        return None
    return {"updated_input": {**tool_input, "client_session_id": wanted}}


_DESIGN_SUFFIXES = (".html", ".htm", ".png", ".jpg", ".jpeg", ".webp", ".gif", ".canvas.tsx")


def cursor_design_context(event: dict) -> str:
    """A path the model can hand to Whales, from the file just written.

    A canvas has no pixels. Saying so here is what stops a later "critique
    this" from inventing a second page and screenshotting that.
    """
    path = event.get("file_path") or ""
    if not path:
        tool_input = _tool_input(event)
        if tool_input:
            path = tool_input.get("path") or tool_input.get("file_path") or ""
    if not any(str(path).endswith(suffix) for suffix in _DESIGN_SUFFIXES):
        return ""
    if str(path).endswith(".canvas.tsx"):
        return (
            f"Whales: a canvas was just written at `{path}`. Cursor has not "
            f"exported it to a PNG, so it is not a critique source. Do not draw "
            f"a substitute page and screenshot that."
        )
    if str(path).endswith((".html", ".htm")):
        return (
            f"Whales: HTML written this session is at `{path}`. If the designer "
            f"asks to critique it, pass that file's markup as `html`. Do not "
            f"screenshot a recreation."
        )
    return (
        f"Whales: an image written this session is at `{path}`. If the designer "
        f"asks to critique it, upload that path. Do not redraw it."
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    # Not required: Cursor auto-discovers and runs Claude Code plugins'
    # hooks.json on its own, forwarding only `command` and never the exec-form
    # `args` that would carry --event. A firing with no --event is that
    # auto-discovered path, not a real one — see the module docstring — so it
    # no-ops below rather than erroring, which is what argparse's own
    # required=True would otherwise do (sys.exit(2), which Cursor treats as a
    # blocked turn — the bug this fixes).
    parser.add_argument("--event", default=None)
    parser.add_argument(
        "--source", default="claude_code_hook", choices=["claude_code_hook", "cursor_hook"]
    )
    args = parser.parse_args()

    if not args.event:
        return 0

    prefix = SESSION_PREFIX[args.source]

    try:
        raw_stdin = sys.stdin.read()
        event = json.loads(raw_stdin) if raw_stdin.strip() else {}
    except (json.JSONDecodeError, OSError):
        event = {}

    session_id = event.get("session_id") or event.get("conversation_id") or ""

    # Not a capture event: it rewrites a Whales tool call's arguments and
    # ships nothing, so it runs regardless of token or WHALES_CAPTURE — the
    # tool call itself authenticates with the plugin's own credential.
    if args.event == "PreToolUse":
        out = (session_id_injection(event, prefix) if args.source == "claude_code_hook"
               else cursor_session_id_injection(event, prefix))
        if out:
            print(json.dumps(out))
        return 0

    # Context only. afterFileEdit already posts the write; this must not post
    # it a second time. Cursor reads additional_context from postToolUse.
    if args.event == "DesignContext" and args.source == "cursor_hook":
        text = cursor_design_context(event)
        if text:
            print(json.dumps({"additional_context": text}))
        return 0

    capture_off = os.environ.get("WHALES_CAPTURE", "").lower() in ("0", "off", "false", "no")
    token = "" if capture_off else _read(TOKEN_FILE)

    # Emitted before any network consideration: the session binding must work
    # even when capture is off or the designer has no token yet, because tool
    # calls authenticate off the plugin's own credential, not this file.
    #
    # Each host has its own stdout contract. Claude Code reads
    # hookSpecificOutput.additionalContext. Cursor reads additional_context
    # and env (https://cursor.com/docs/hooks). Emitting the wrong shape is
    # ignored, which is how a Cursor session ended up with no client_session_id.
    if args.event == "SessionStart" and session_id and args.source == "claude_code_hook":
        print(
            json.dumps(
                {
                    "hookSpecificOutput": {
                        "hookEventName": "SessionStart",
                        "additionalContext": _session_start_context(
                            session_id, capturing=bool(token), prefix=prefix
                        ),
                    }
                }
            )
        )
    elif args.event == "SessionStart" and session_id and args.source == "cursor_hook":
        print(json.dumps(cursor_session_start_output(
            session_id, bool(token), prefix
        )))

    if not token:
        return 0  # capture off, or not connected yet — either way, nothing to send

    payload = {
        "source": args.source,
        "session_id": f"{prefix}:{session_id}" if session_id else None,
        "raw_payload": truncate(event),
    }

    text, new_offset, was_truncated = transcript_delta(
        event.get("transcript_path", ""), session_id
    )
    if text:
        payload["raw_payload"]["transcript_delta"] = text
        payload["raw_payload"]["transcript_delta_truncated"] = was_truncated

    try:
        body = scrub(json.dumps(payload)).encode("utf-8")
    except (TypeError, ValueError):
        return 0

    # Offset advances only once the payload is built. If we crash before this,
    # the same bytes ship again next turn — a duplicate, which the backend can
    # collapse, rather than a silent hole it can never know about.
    save_offset(session_id, new_offset)
    post_detached(gateway_url(), token, body)
    return 0


if __name__ == "__main__":
    # Always 0 — see the module docstring on why a capture hook must never
    # signal failure back into the designer's editor.
    #
    # BaseException, not Exception: argparse rejects bad arguments by raising
    # SystemExit(2), which Exception does not catch — and Cursor reads exit 2
    # from beforeSubmitPrompt as "block this prompt".
    try:
        code = main()
    except BaseException:
        code = 0
    sys.exit(code or 0)
