#!/usr/bin/env python3
"""Whales capture hook — forwards one Claude Code hook event to the gateway.

Runs on the DESIGNER'S machine, invoked by their editor on every turn, so the
constraints here are different from the rest of the product:

- **Standard library only.** A designer should not have to install anything
  for a hook to work, and a missing dependency would turn every hook firing
  into an error in their session.
- **Always exits 0.** A non-zero exit can block a tool call or feed stderr
  back to the model. Capture is observation, never enforcement: if the
  gateway is down, the designer's turn carries on untouched. What it must
  not be is *invisible* — every upload's outcome is recorded in
  ``~/.whales/capture_status.json``, and the next SessionStart reports what
  it says instead of assuming capture works because a token file exists.
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
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

TIMEOUT_SECONDS = 8
CONFIG_DIR = os.path.expanduser("~/.whales")
TOKEN_FILE = os.path.join(CONFIG_DIR, "token")
GATEWAY_FILE = os.path.join(CONFIG_DIR, "gateway")
OFFSET_DIR = os.path.join(CONFIG_DIR, "offsets")
# What the last upload actually did. The only honest source for "is capture
# working": a token file existing says nothing about whether the gateway
# still accepts it — a designer once saw "capture is active" for four weeks
# while every upload was being rejected.
STATUS_FILE = os.path.join(CONFIG_DIR, "capture_status.json")

DEFAULT_GATEWAY = "https://mcp.gojuly.ai"

# Whales tools that take ``client_session_id``, matched on the bare tool name
# because the server prefix differs by install (``mcp__whales__``,
# ``mcp__plugin_whales_whales__``, a claude.ai connector's uuid). Kept in step
# with the gateway's tool list. ``get_rebuild_contract`` is left out on
# purpose: it takes no session id, and an argument it does not declare could
# be rejected.
SESSION_ID_TOOLS = frozenset({
    "whales", "ask_whales", "record_reaction", "get_design_profile",
    "search_design_history", "submit_design", "record_critique", "record_approval",
    "universal_critique", "list_design_systems", "get_design_system",
    "register_design_system", "generate_design_system", "extract_figma",
})

# A rejected credential, as opposed to a gateway that is down or unreachable.
# Worth telling apart: only the first needs the designer to do something.
_AUTH_STATUSES = (401, 403)

# Sessions a host opens for its own bookkeeping, not the designer's work.
# Conductor starts a separate session per workspace to name it: one prompt,
# a one-line reply, no transcript. Captured unmarked, they read as designer
# prompts and their reply as a model answer — ten of them in one designer's
# two weeks. Tagged rather than dropped: the raw store keeps everything, and
# the backend decides what to exclude.
_SYNTHETIC_PROMPTS = (
    ("conductor_title", re.compile(r"^\s*You are generating a short conversation title\.")),
)
SYNTHETIC_DIR = os.path.join(CONFIG_DIR, "synthetic")

# Host-injected preambles wrapped around what the designer typed. Conductor
# puts a long <system_instruction> block in front of every prompt, and
# attachments get one too; a classifier reading the raw prompt reads the
# preamble, and the 20,000-character cap can cut off the designer's words
# entirely.
_HOST_PREAMBLE = re.compile(r"<system_instruction>[\s\S]*?</system_instruction>")

# Host session ids and the MCP transport's own session id are different
# identifier spaces. Namespacing keeps two unrelated sessions from colliding
# on a shared id — including sessions from two different HOSTS, which is why
# this is keyed by --source rather than a single constant — and the
# SessionStart binding (see _session_start_context) is what lets the backend
# join the id-and-transport buckets back together within one host.
SESSION_PREFIX = {"claude_code_hook": "cc", "cursor_hook": "cur"}

# Keys that may carry a whole file. Kept but truncated: the point of capturing
# an edit is knowing which values changed, and a multi-megabyte file would
# bloat the store without adding signal.
_BULKY_KEYS = ("content", "new_string", "old_string", "prompt", "response", "output")
_MAX_FIELD_CHARS = 20_000

# Most bytes we will ship from a transcript in one call. A long session's
# JSONL grows without bound, and a single turn should never mail a 40MB file.
_MAX_TRANSCRIPT_BYTES = 512_000

# One transcript upload in flight per session. Without it, a burst of events
# (several quick edits, then Stop) all read the same stored offset before the
# first upload returns, and each ships the same bytes. A lock older than this
# belongs to an upload that died; it is taken over.
_LOCK_STALE_SECONDS = 180

# After the event's own chunk is accepted, the same detached process keeps
# shipping what is left, up to this many chunks (8MB). Otherwise the end of a
# long session waits for events that never come once Stop and SessionEnd
# have fired.
_MAX_DRAIN_CHUNKS = 16

# A chunk that failed waits before it is sent again: 30s, doubling, at most
# 30 minutes. Only the transcript waits; the event itself is still sent.
_RETRY_BASE_SECONDS = 30
_RETRY_MAX_SECONDS = 1800

# A chunk the gateway rejects as malformed this many times is skipped, and
# the skip reported, rather than blocking the rest of the session forever.
# Outages (5xx, timeouts) never skip: they only back off.
_MAX_CHUNK_REJECTIONS = 3

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
    """The next unshipped part of the transcript, plus where it ends.

    Delta rather than whole-file: a transcript grows monotonically and is read
    on every hook firing, so mailing all of it each turn would re-send the
    same session over and over — quadratic traffic for linear work.

    Returns ``(text, new_offset, more_pending, start)``. At most
    ``_MAX_TRANSCRIPT_BYTES`` ship per event, oldest first, cut at a line
    boundary so a JSONL record is never split; whatever remains ships with the
    following events. This used to keep only the tail and jump the offset to
    the end, which silently dropped the middle of every long turn — the turns
    where most of the design work happens. A file that SHRANK (a new session
    reusing an id, or a rotated file) resets to 0 rather than seeking past the
    end and shipping nothing forever.
    """
    if not transcript_path or not os.path.isfile(transcript_path):
        return "", None, False, None
    try:
        size = os.path.getsize(transcript_path)
        start = 0
        raw = _read(_offset_path(session_id))
        if raw.isdigit():
            start = int(raw)
        if start > size:
            start = 0
        if start == size:
            return "", None, False, None

        # Bytes, not text: offsets are byte positions, and a text-mode read
        # counts characters, which drifts on any non-ASCII transcript.
        with open(transcript_path, "rb") as fh:
            fh.seek(start)
            chunk = fh.read(_MAX_TRANSCRIPT_BYTES)
        more_pending = start + len(chunk) < size
        if more_pending:
            cut = chunk.rfind(b"\n")
            if cut >= 0:
                chunk = chunk[: cut + 1]
            # else: one line longer than the cap — ship it cut; the rest
            # follows next event rather than blocking the session forever.
        return chunk.decode("utf-8", errors="replace"), start + len(chunk), more_pending, start
    except OSError:
        return "", None, False, None


def _synthetic_marker(session_id: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", session_id or "unknown")
    return os.path.join(SYNTHETIC_DIR, safe)


def synthetic_kind(event: dict, session_id: str, event_name: str) -> str:
    """The kind of host bookkeeping session this event belongs to, or "".

    Recognised from the session's prompt, then remembered in a marker file so
    the session's later events (its Stop, whose reply is the title, and its
    SessionEnd) carry the same tag. The marker is removed at SessionEnd. The
    SessionStart that opens such a session is sent before any prompt exists,
    so it cannot be tagged here; the backend joins it by session id.
    """
    if not session_id:
        return ""
    marker = _synthetic_marker(session_id)
    kind = _read(marker)
    if not kind and event_name == "UserPromptSubmit":
        prompt = event.get("prompt") or ""
        for name, pattern in _SYNTHETIC_PROMPTS:
            if isinstance(prompt, str) and pattern.search(prompt):
                kind = name
                try:
                    os.makedirs(SYNTHETIC_DIR, exist_ok=True)
                    with open(marker, "w", encoding="utf-8") as fh:
                        fh.write(kind)
                except OSError:
                    pass
                break
    if kind and event_name == "SessionEnd":
        try:
            os.remove(marker)
        except OSError:
            pass
    return kind


def prompt_user_text(prompt) -> str:
    """The prompt with host-injected preamble blocks removed, or "" when there
    were none. Taken from the untruncated prompt, before the field cap."""
    if not isinstance(prompt, str) or "<system_instruction>" not in prompt:
        return ""
    return _HOST_PREAMBLE.sub("", prompt).strip()[:_MAX_FIELD_CHARS]


def _attach_transcript(raw_payload: dict, text: str, start, end, more_pending: bool) -> None:
    """Put one transcript chunk on an outgoing event.

    ``transcript_delta_range`` is the chunk's byte span in the transcript
    file. Retries now re-send a chunk whose first attempt may have reached the
    gateway before its response was lost, so the backend needs a key to
    collapse duplicates on: session id plus range.
    ``transcript_delta_truncated`` keeps its name for existing readers; it now
    means "more of this transcript follows in later events", not "the middle
    was dropped".
    """
    raw_payload["transcript_delta"] = text
    raw_payload["transcript_delta_range"] = [start, end]
    raw_payload["transcript_delta_truncated"] = more_pending


def save_offset(session_id: str, offset, transcript_path: str = "") -> None:
    """Record how far the transcript has been shipped.

    Never moves the offset backwards within the same file. Uploads run in
    detached processes, so two of them can finish out of order; the later one
    finishing first must not have its progress undone by the earlier one,
    which would re-ship bytes (harmless) but also make every following delta
    start from the wrong place. A stored offset that is beyond the file's
    current size means the file shrank (a reused id, a rotated file) — that is
    the one case where a smaller offset has to win.
    """
    if offset is None:
        return
    try:
        existing = _read(_offset_path(session_id))
        if existing.isdigit() and int(existing) > offset:
            try:
                current_size = os.path.getsize(transcript_path) if transcript_path else None
            except OSError:
                current_size = None
            if current_size is None or int(existing) <= current_size:
                return
        os.makedirs(OFFSET_DIR, exist_ok=True)
        with open(_offset_path(session_id), "w", encoding="utf-8") as fh:
            fh.write(str(offset))
    except OSError:
        pass


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def read_status() -> dict:
    """The last recorded upload outcome, or ``{}`` if nothing has been sent."""
    try:
        data = json.loads(_read(STATUS_FILE) or "{}")
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def record_send_result(ok: bool, status=None, reason: str = "") -> None:
    """Remember what the last upload did, so the next session can say so.

    A success resets the failure count; a failure keeps the time of the first
    one in the run, which is what tells a designer how long capture has been
    down. Written atomically (temp file + rename): uploads run concurrently,
    and a half-written file would read as "nothing sent yet". The count is
    approximate under concurrency, which is fine — it is a signal, not a
    ledger.
    """
    state = read_status()
    now = _now()
    if ok:
        state["last_ok_at"] = now
        state["failed_since_ok"] = 0
        state.pop("first_failed_at", None)
    else:
        if not state.get("failed_since_ok"):
            state["first_failed_at"] = now
        state["failed_since_ok"] = int(state.get("failed_since_ok") or 0) + 1
        state["last_error"] = {"at": now, "status": status, "reason": reason[:200]}
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        tmp = f"{STATUS_FILE}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(state, fh)
        os.replace(tmp, STATUS_FILE)
    except OSError:
        pass


def capture_state(token: str) -> str:
    """One of ``off``, ``unconfirmed``, ``active``, ``rejected``, ``failing``."""
    if not token:
        return "off"
    status = read_status()
    if status.get("failed_since_ok"):
        last = (status.get("last_error") or {}).get("status")
        return "rejected" if last in _AUTH_STATUSES else "failing"
    return "active" if status.get("last_ok_at") else "unconfirmed"


def _lock_path(session_id: str) -> str:
    return _offset_path(session_id)[: -len(".offset")] + ".lock"


def _fail_path(session_id: str) -> str:
    return _offset_path(session_id)[: -len(".offset")] + ".fail"


def acquire_upload_lock(session_id: str):
    """The session's upload lock, or ``None`` if another upload holds it.

    O_EXCL, not check-then-create: two hooks firing together must not both
    win. A lock left by an upload that died is taken over once stale.
    """
    path = _lock_path(session_id)
    try:
        os.makedirs(OFFSET_DIR, exist_ok=True)
    except OSError:
        return None
    for _ in range(2):
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            return path
        except FileExistsError:
            try:
                if time.time() - os.path.getmtime(path) < _LOCK_STALE_SECONDS:
                    return None
                os.remove(path)
            except OSError:
                continue
        except OSError:
            return None
    return None


def release_upload_lock(path) -> None:
    if path:
        try:
            os.remove(path)
        except OSError:
            pass


def read_chunk_failure(session_id: str) -> dict:
    try:
        data = json.loads(_read(_fail_path(session_id)) or "{}")
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def record_chunk_failure(session_id: str, start, status, end=None) -> None:
    """Count consecutive failures of the chunk ``[start, end)``. The end is
    kept so a retry re-sends exactly those bytes (see ``transcript_range``)."""
    previous = read_chunk_failure(session_id)
    same = previous.get("start") == start
    count = int(previous.get("count") or 0) + 1 if same else 1
    if end is None and same:
        end = previous.get("end")
    try:
        os.makedirs(OFFSET_DIR, exist_ok=True)
        with open(_fail_path(session_id), "w", encoding="utf-8") as fh:
            json.dump({"start": start, "end": end, "count": count, "status": status,
                       "at": time.time()}, fh)
    except OSError:
        pass


def transcript_range(transcript_path: str, start: int, end: int) -> str:
    """Exactly the bytes ``[start, end)``, or ``""`` if they are no longer
    there (the file shrank or went away).

    A retry re-sends the failed chunk byte for byte rather than reading to
    the file's new end: when the first attempt did reach the gateway and only
    its answer was lost, the identical ``transcript_delta_range`` is what lets
    the backend recognise the repeat. Read to the new end, the range would
    differ and the overlap would be stored twice.
    """
    try:
        if os.path.getsize(transcript_path) < end:
            return ""
        with open(transcript_path, "rb") as fh:
            fh.seek(start)
            return fh.read(end - start).decode("utf-8", errors="replace")
    except (OSError, TypeError, ValueError):
        return ""


def clear_chunk_failure(session_id: str) -> None:
    try:
        os.remove(_fail_path(session_id))
    except OSError:
        pass


def _is_permanent(status) -> bool:
    """A client error that re-sending the same bytes will not fix."""
    return isinstance(status, int) and 400 <= status < 500 and status not in (401, 403, 408, 429)


def chunk_decision(failure: dict, start, now: float) -> str:
    """``send``, ``wait`` or ``skip`` for the chunk starting at ``start``. Pure."""
    if not failure or failure.get("start") != start:
        return "send"
    count = int(failure.get("count") or 0)
    if _is_permanent(failure.get("status")) and count >= _MAX_CHUNK_REJECTIONS:
        return "skip"
    wait = min(_RETRY_BASE_SECONDS * 2 ** max(count - 1, 0), _RETRY_MAX_SECONDS)
    return "wait" if now - float(failure.get("at") or 0) < wait else "send"


def _chunk_body(source: str, session_key: str, session_id: str, transcript_path: str,
                event_name: str, text: str, start, end, more_pending: bool) -> bytes:
    raw = {"hook_event_name": event_name, "whales_event": event_name,
           "session_id": session_id, "transcript_path": transcript_path}
    _attach_transcript(raw, text, start, end, more_pending)
    return scrub(json.dumps({"source": source, "session_id": session_key,
                             "raw_payload": raw})).encode("utf-8")


def drain(url: str, token: str, session_id: str, transcript_path: str, source: str,
          session_key: str, max_chunks: int = _MAX_DRAIN_CHUNKS) -> int:
    """Ship what is left of the transcript, one chunk at a time; returns how
    many were accepted. Runs in the detached process that holds the session's
    upload lock, so nothing here can be shipped twice by a concurrent hook.
    Stops at the first failure, which is recorded for the backoff."""
    shipped = 0
    for _ in range(max_chunks):
        text, new_offset, more_pending, start = transcript_delta(transcript_path, session_id)
        if not text:
            break
        body = _chunk_body(source, session_key, session_id, transcript_path, "TranscriptChunk",
                           text, start, new_offset, more_pending)
        ok, status, reason = _send(url, token, body)
        record_send_result(ok, status, reason)
        if not ok:
            record_chunk_failure(session_id, start, status, end=new_offset)
            break
        clear_chunk_failure(session_id)
        save_offset(session_id, new_offset, transcript_path)
        shipped += 1
        if not more_pending:
            break
    return shipped


def deliver(url: str, token: str, body: bytes, session_id: str, new_offset,
            transcript_path: str, chunk_start=None, lock=None, drain_to=None) -> None:
    """Send one event, record the outcome, and only then move the offset.

    The offset is what says "these transcript bytes have been shipped".
    Advancing it before knowing the upload succeeded turned every failed
    upload — a rejected token, an outage — into bytes that were never sent
    again. Advancing it after means a failure is retried on a later event,
    at the cost of a repeat if the response is lost after the gateway stored
    the event. The retry re-sends exactly the failed range, flagged
    ``transcript_resend``, and the backend drops it if it already has it.

    ``chunk_start`` is set when the body carries a transcript chunk: its
    failure is counted for the backoff. ``lock`` is the session's upload
    lock, released here whatever happens. ``drain_to`` is
    ``(source, session_key)``: with it, an accepted upload goes on to ship
    the rest of the transcript.
    """
    try:
        ok, status, reason = _send(url, token, body)
        record_send_result(ok, status, reason)
        if chunk_start is not None:
            if ok:
                clear_chunk_failure(session_id)
            else:
                record_chunk_failure(session_id, chunk_start, status, end=new_offset)
        if ok:
            save_offset(session_id, new_offset, transcript_path)
            if drain_to and new_offset is not None:
                drain(url, token, session_id, transcript_path, *drain_to)
    finally:
        release_upload_lock(lock)


def post_detached(url: str, token: str, body: bytes, session_id: str = "",
                  new_offset=None, transcript_path: str = "", chunk_start=None,
                  lock=None, drain_to=None) -> None:
    """Deliver in a detached grandchild so the hook returns immediately.

    Double-fork so the intermediate child exits at once and the grandchild is
    reparented to init — otherwise the editor could still be waiting on a
    process it did not know it spawned. On any platform without fork we fall
    back to a short blocking send, which is still bounded by the timeout.
    """
    job = (url, token, body, session_id, new_offset, transcript_path, chunk_start, lock, drain_to)
    if not hasattr(os, "fork"):
        deliver(*job)
        return
    try:
        if os.fork() != 0:
            return  # parent: done, hook exits now; the grandchild owns the lock
    except OSError:
        deliver(*job)
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
        deliver(*job)
    finally:
        os._exit(0)


def _send(url: str, token: str, body: bytes):
    """POST one event. Returns ``(ok, http_status_or_None, reason)``.

    Never raises: our availability must never be the designer's problem, and
    a failed upload must not stall their turn. But the failure is returned
    rather than swallowed — ``deliver`` records it, which is the only way a
    broken capture ever becomes visible.
    """
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
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            return True, response.status, ""
    except urllib.error.HTTPError as exc:
        # Before URLError: HTTPError is its subclass, and the status code is
        # the part that tells a rejected token from an outage.
        return False, exc.code, str(exc.reason or "")
    except (urllib.error.URLError, OSError, TimeoutError, ValueError) as exc:
        return False, None, str(getattr(exc, "reason", "") or exc)


def _capture_lead(state: str, status: dict) -> str:
    """The first sentence of the SessionStart context: what capture is
    actually doing, based on what the last upload did — never on whether a
    token file happens to exist."""
    if state == "active":
        return (f"Whales capture is active for this session "
                f"(last upload confirmed {status.get('last_ok_at')}).")
    if state == "rejected":
        return (
            f"Whales capture is failing on this machine: the Whales gateway has "
            f"rejected its token since {status.get('first_failed_at')}, so "
            f"{status.get('failed_since_ok')} capture event(s) were not recorded. "
            f"Tell the designer once, briefly, that Whales capture needs its "
            f"token refreshed by re-running the Whales installer."
        )
    if state == "failing":
        reason = (status.get("last_error") or {}).get("reason") or "unreachable"
        return (
            f"Whales capture is failing on this machine: uploads have failed since "
            f"{status.get('first_failed_at')} ({reason}). Transcript is retried on "
            f"the next event, but {status.get('failed_since_ok')} event(s) were not "
            f"recorded."
        )
    if state == "unconfirmed":
        return ("Whales capture is configured on this machine, but no upload has "
                "been confirmed yet.")
    # Said plainly rather than omitted: the binding below is still worth
    # doing (tool calls work off the plugin's own credential, which is stored
    # separately from this file), but claiming capture is running when it is
    # not would make a silent misconfiguration look healthy.
    return ("Whales is connected, but local capture is not configured on this "
            "machine, so file edits made outside the Whales tools are not being "
            "recorded.")


def _session_start_context(session_id: str, lead: str, prefix: str) -> str:
    """What SessionStart injects into the model's context.

    This is the fix for the one gap an MCP server structurally cannot close on
    its own: the host's session id and the MCP transport's ``mcp-session-id``
    are different identifier spaces, so a design submitted through
    ``submit_design`` and then hand-edited with the native Write tool land in
    two buckets that can never be joined. The PreToolUse hook now adds the id
    to every Whales call itself; asking the model to pass it stays as the
    fallback for hosts that do not run that hook.
    """
    return (
        f"{lead} Its Claude Code session id is `{prefix}:{session_id}`. "
        f"Pass that exact string as the `client_session_id` argument on every "
        f"Whales MCP tool call you make in this session, so tool calls and file "
        f"edits are recorded as one piece of work rather than two unrelated ones."
    )


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


def _session_transcripts(projects_dir: str, since_ts: float):
    """Top-level Claude Code session transcripts modified since ``since_ts``.

    Subagent transcripts are skipped: their turns already appear in the
    parent session's transcript, so shipping them again would double-count
    the work.
    """
    found = []
    for root, dirs, files in os.walk(projects_dir):
        dirs[:] = [d for d in dirs if d != "subagents"]
        for name in files:
            if not name.endswith(".jsonl") or name.startswith("agent-"):
                continue
            path = os.path.join(root, name)
            try:
                if os.path.getmtime(path) >= since_ts:
                    found.append(path)
            except OSError:
                pass
    return sorted(found, key=os.path.getmtime)


def project_transcript_dir(cwd: str, projects_dir: str) -> str:
    """Where Claude Code keeps the transcripts of sessions started in ``cwd``:
    the path with every character that is not a letter or digit turned into
    ``-`` (``/Users/me/Koi`` -> ``-Users-me-Koi``)."""
    return os.path.join(projects_dir, re.sub(r"[^A-Za-z0-9]", "-", cwd))


def backfill(since: str, from_start: bool, projects_dir: str = "",
             all_projects: bool = False, cwd: str = "") -> int:
    """Ship the unshipped transcript of past sessions, run by hand.

    For recovering after capture was broken — a rejected token, an outage —
    when finished sessions will never fire another hook to carry their
    backlog. Sends synchronously, chunk by chunk, and moves each offset only
    once its chunk is accepted, so it is safe to interrupt and re-run. Stops
    at the first failure: if the token is rejected, every later request
    would be too.

    ``from_start`` ignores stored offsets. Offsets written by earlier
    versions of this script can sit at the end of transcripts that never
    reached the gateway (they moved before the upload was known to succeed),
    so resuming from them would skip exactly the data that was lost. The
    cost: parts that did arrive the first time are stored again. Backfill
    cuts its own 512KB windows, which never line up with the ranges live
    uploads used, so the backend has nothing to match them on.

    Scoped on purpose. ``since`` is required — there is no "everything ever"
    default — and only sessions started in ``cwd`` (the project it is run
    from) are sent unless ``all_projects`` is set, so a designer recovering
    one project's capture does not also upload every unrelated session on
    the machine, including ones from before Whales was installed.
    """
    capture_off = os.environ.get("WHALES_CAPTURE", "").lower() in ("0", "off", "false", "no")
    token = "" if capture_off else _read(TOKEN_FILE)
    if not token:
        print("Whales backfill: no token at ~/.whales/token (or WHALES_CAPTURE is off); nothing sent.")
        return 0
    if not since:
        print("Whales backfill: pass --since YYYY-MM-DD (the first day capture was broken); "
              "nothing sent.")
        return 0
    try:
        since_ts = datetime.strptime(since, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        print(f"Whales backfill: --since must be YYYY-MM-DD, got {since!r}.")
        return 0

    url = gateway_url()
    projects_dir = projects_dir or os.path.expanduser("~/.claude/projects")
    search_dir = projects_dir if all_projects else project_transcript_dir(cwd or os.getcwd(), projects_dir)
    if not os.path.isdir(search_dir):
        print(f"Whales backfill: no Claude Code sessions found for {cwd or os.getcwd()} "
              f"(looked in {search_dir}). Run it from the project, or pass --all-projects.")
        return 0
    sessions = chunks = sent_bytes = 0
    for path in _session_transcripts(search_dir, since_ts):
        session_id = os.path.splitext(os.path.basename(path))[0]
        if from_start:
            try:
                os.remove(_offset_path(session_id))
            except OSError:
                pass
        shipped_any = False
        while True:
            text, new_offset, more_pending, start = transcript_delta(path, session_id)
            if not text:
                break
            raw = {"hook_event_name": "Backfill", "whales_event": "Backfill",
                   "session_id": session_id, "transcript_path": path}
            _attach_transcript(raw, text, start, new_offset, more_pending)
            body = scrub(json.dumps({
                "source": "claude_code_hook",
                "session_id": f"{SESSION_PREFIX['claude_code_hook']}:{session_id}",
                "raw_payload": raw,
            })).encode("utf-8")
            ok, status, reason = _send(url, token, body)
            record_send_result(ok, status, reason)
            if not ok:
                why = "token rejected — re-run the Whales installer" if status in _AUTH_STATUSES \
                    else (reason or "gateway unreachable")
                print(f"Whales backfill stopped: {why} (HTTP {status}). "
                      f"Sent {chunks} chunk(s) from {sessions} session(s) before stopping; "
                      f"re-run to resume.")
                return 0
            save_offset(session_id, new_offset, path)
            shipped_any = True
            chunks += 1
            sent_bytes += len(text.encode("utf-8"))
            if not more_pending:
                break
        sessions += shipped_any
    print(f"Whales backfill done: {chunks} chunk(s), {sent_bytes // 1024} KB "
          f"from {sessions} session(s) since {since}.")
    return 0


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
    parser.add_argument("--backfill", action="store_true",
                        help="ship the unshipped transcript of past sessions, then exit")
    parser.add_argument("--since", default="",
                        help="with --backfill (required): only sessions modified on or after YYYY-MM-DD")
    parser.add_argument("--all-projects", action="store_true",
                        help="with --backfill: every project's sessions, not just this directory's")
    parser.add_argument("--from-start", action="store_true",
                        help="with --backfill: ignore stored offsets and re-ship from the beginning")
    args = parser.parse_args()

    if args.backfill:
        return backfill(args.since, args.from_start, all_projects=args.all_projects)

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
        if args.source == "claude_code_hook":
            out = session_id_injection(event, prefix)
            if out:
                print(json.dumps(out))
        return 0

    capture_off = os.environ.get("WHALES_CAPTURE", "").lower() in ("0", "off", "false", "no")
    token = "" if capture_off else _read(TOKEN_FILE)
    state = capture_state(token)
    status = read_status() if token else {}

    # Emitted before any network consideration: the session binding must work
    # even when capture is off or the designer has no token yet, because tool
    # calls authenticate off the plugin's own credential, not this file.
    #
    # Claude-Code-only: this is Claude Code's own hookSpecificOutput shape for
    # injecting additionalContext. Cursor's sessionStart event has no
    # documented equivalent output field, so emitting it there would be at
    # best ignored — skip it rather than guess at an undocumented contract.
    if args.event == "SessionStart" and session_id and args.source == "claude_code_hook":
        out = {
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": _session_start_context(
                    session_id, _capture_lead(state, status), prefix
                ),
            }
        }
        if state == "rejected":
            # Shown to the designer directly, not only to the model: a
            # rejected token is the one failure they have to act on, and the
            # model mentioning it is not guaranteed.
            out["systemMessage"] = (
                "Whales capture is failing: the gateway rejected this machine's "
                "token. Re-run the Whales installer to refresh it."
            )
        print(json.dumps(out))

    if not token:
        return 0  # capture off, or not connected yet — either way, nothing to send

    session_key = f"{prefix}:{session_id}" if session_id else None
    payload = {
        "source": args.source,
        "session_id": session_key,
        "raw_payload": truncate(event),
    }
    # The canonical event name, whatever the host calls it: Cursor's own
    # payload says beforeSubmitPrompt / afterFileEdit, and the backend passes
    # read this to treat both hosts alike.
    payload["raw_payload"]["whales_event"] = args.event
    # Both read the event before truncate() capped it: a long host preamble
    # can push the designer's own words past the cap.
    user_text = prompt_user_text(event.get("prompt"))
    if user_text:
        payload["raw_payload"]["prompt_user_text"] = user_text
    kind = synthetic_kind(event, session_id, args.event)
    if kind:
        payload["raw_payload"]["synthetic"] = kind

    # While the token is known to be rejected, send the event without the
    # transcript. Retrying is what keeps transcript from being lost, but
    # re-sending up to 512KB on every prompt and edit to a gateway that will
    # refuse it only costs the designer bandwidth. The offset stays put, so
    # the backlog ships once an upload is accepted again.
    #
    # Otherwise the transcript rides along only with the session's upload
    # lock: an upload already in flight will drain what this event would have
    # sent, and sending it here too would ship it twice.
    transcript_path = event.get("transcript_path", "")
    text, new_offset, more_pending, start = "", None, False, None
    lock = None
    if state != "rejected" and session_id and transcript_path:
        lock = acquire_upload_lock(session_id)
    if lock:
        text, new_offset, more_pending, start = transcript_delta(transcript_path, session_id)
        failure = read_chunk_failure(session_id) if text else {}
        decision = chunk_decision(failure, start, time.time()) if text else "send"
        if decision == "skip":
            # Rejected as malformed every time: move past it, and say so.
            payload["raw_payload"]["transcript_skipped"] = {
                "range": [start, new_offset], "http_status": failure.get("status"),
                "attempts": failure.get("count"),
            }
            save_offset(session_id, new_offset, transcript_path)
            clear_chunk_failure(session_id)
            text, new_offset, more_pending, start = transcript_delta(transcript_path, session_id)
        elif decision == "wait":
            text, new_offset, more_pending, start = "", None, False, None
        elif failure.get("start") == start and isinstance(failure.get("end"), int):
            # A retry: the same bytes as the failed attempt, flagged, so the
            # backend can drop them if that attempt did arrive.
            again = transcript_range(transcript_path, start, failure["end"])
            if again:
                text, new_offset = again, failure["end"]
                more_pending = new_offset < os.path.getsize(transcript_path)
                payload["raw_payload"]["transcript_resend"] = True
        if not text:
            release_upload_lock(lock)
            lock = None
    if text:
        _attach_transcript(payload["raw_payload"], text, start, new_offset, more_pending)

    # After an outage, the first event that gets through says how many did
    # not — without it the backend cannot tell "this designer went quiet"
    # from "this designer's capture was broken".
    if status.get("failed_since_ok"):
        payload["raw_payload"]["capture_health"] = {
            "failed_since_ok": status.get("failed_since_ok"),
            "first_failed_at": status.get("first_failed_at"),
            "last_error": status.get("last_error"),
        }

    try:
        body = scrub(json.dumps(payload)).encode("utf-8")
    except (TypeError, ValueError):
        release_upload_lock(lock)
        return 0

    # The offset moves inside deliver(), only once the gateway accepted the
    # upload. A failure leaves it where it was, so the same bytes ship again
    # on a later event (after the backoff) rather than leaving a silent hole
    # the backend can never know about.
    post_detached(gateway_url(), token, body, session_id, new_offset, transcript_path,
                  chunk_start=start if text else None, lock=lock,
                  drain_to=(args.source, session_key) if text else None)
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
