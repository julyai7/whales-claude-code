# whales-claude-code

The whales plugin for Claude Code, and the marketplace that serves it.

This repo is both: `.claude-plugin/marketplace.json` at the root, the plugin
itself under `plugins/whales/`. There is no review or submission step for a
marketplace — a user adds it directly and installs from it.

## Install

Normally you don't. The [installer](https://gojuly.ai) does it for you, with
your token already filled in. To do it by hand:

```bash
claude plugin marketplace add julyai7/whales-claude-code
claude plugin install whales@whales --config whales_token=<your token>
```

Get a token from the Connect MCP page (`/mcp`) in the whales web app. `--yes` is required when stdin
isn't a TTY, which is the case inside `curl | bash`.

## What's in it

| | |
|---|---|
| **MCP server** | The whales gateway over streamable HTTP, authenticated with `${user_config.whales_token}` |
| **Skills** | `design-profile` (apply the designer's conventions to UI work), `design-system` (extract/register/maintain a design system), `universal-critique` (whales critiques a screen; its `critique_source.py` uploads the file itself) |
| **Agent** | `design-review` — conformance-checks a diff in its own context |
| **Hooks** | Capture on SessionStart, UserPromptSubmit, PostToolUse(Write\|Edit), PreCompact, Stop, SessionEnd; PreToolUse on whales tools adds `client_session_id` |
**Permissions are not in the plugin.** Claude Code ignores `permissions` in a
plugin's `settings.json`; only `agent` and `subagentStatusLine` take effect
there (checked 2026-09-24 on 2.1.282: a plugin-allowed command still needed
approval, while the same rule passed via `--allowedTools` did not). The whales
installer adds the read-and-record tools to the designer's own
`~/.claude/settings.json` instead, and the updater adds any new ones on each
release — see `ALLOW_TOOLS` in `scripts/whales_update.py` for the list and
what it leaves out.

## Updates: the installer runs once

`scripts/whales_update.py` runs at every session start, in Claude Code (its
own SessionStart hook) and in Cursor (the wrapper below). In a detached
process, at most every 10 minutes, it:

1. runs `claude plugin marketplace update whales` and, when the catalog lists
   a different version, `claude plugin update whales@whales`. Different, not
   higher: lowering the published version rolls everyone back.
2. brings everything outside the plugin to the installed version, copied from
   the installed plugin (never from the running session, so a `--plugin-dir`
   session cannot publish unreleased code): the Cursor wrapper and capture
   hook, `critique_source.py`, the updater itself, the `whales` command,
   whales' entries in `~/.cursor/hooks.json`, the version header in
   `~/.cursor/mcp.json`, and the allow rules above.

A new version loads when Claude Code or Cursor restarts; the next prompt in a
session still on the old one says so. Claude Code's own auto-update stays on
as a second way in if a release ever breaks the updater. Machines without
Claude Code fetch the same files from this repo's `main`.

**Releasing is merging a version bump to `main`.** Every installed machine
picks it up at its next session start. Updates follow the published version
**down** as well as up, so a plugin PR must bump *above whatever `main` has at
merge time*: a branch cut before someone else's release, merged as is, rolls
every designer back. Never below 0.6.0, the first version that updates itself
(`tests/test_whales_update.py` checks that floor). There are no channels yet (JUL-652
Phase 2), so test a release before merging: point your own install at the
branch (edit `ref` under `extraKnownMarketplaces.whales.source` in
`~/.claude/settings.json` **and** `whales.source` in
`~/.claude/plugins/known_marketplaces.json`, then `whales update`), or run a
checkout for one session with `claude --plugin-dir ./plugins/whales`.

`touch ~/.whales/auto_update_off` stops it on a developer's machine.

## Why hooks and not just an MCP server

An MCP server can only see its own tool calls. A design the agent writes
straight to disk with the native Write tool is completely invisible to it.
That gap is inherently local and cannot be closed by any transport choice.

The hooks also fix something the MCP server cannot: the host's session id and
the MCP transport's `mcp-session-id` are different identifier spaces, so a
design submitted through `submit_design` and then hand-edited would land in
two buckets that can never be joined. A `PreToolUse` hook adds the host
session id to every whales tool call as `client_session_id`, which is what
makes one session one story — and what lets outcomes be *observed*
(submission then approval means accepted) rather than guessed at by a model
after the fact. It rewrites the arguments only; it sets no permission
decision, so the designer's own allow/ask rules still apply. `SessionStart`
also tells the model the id, as the fallback for hosts that do not run the
hook — on its own that reached only 4 of 150 calls.

## Capture, stated plainly

With a token configured, the hooks send session activity — including
transcript content — to the whales backend, so the designer's profile
improves from real work. Specifics:

- **Deltas, not whole files.** A byte offset per session is tracked under
  `~/.whales/offsets/`; each event ships only what was appended since the
  last one, 512KB per upload, oldest first. The offset moves only after the
  gateway accepts the upload, so a failed upload is re-sent later instead of
  lost. A re-send carries exactly the same bytes, marked `transcript_resend`,
  so the backend can drop it when the first attempt did arrive and only its
  answer was lost. One upload per session runs at a time (a lock under
  `~/.whales/offsets/`), and it keeps going until the transcript is caught
  up, so the end of a session is not left waiting for events that never
  come.
- **Failures back off.** A chunk that failed waits 30s, doubling up to 30
  minutes, before it is sent again; the events themselves still go. A chunk
  the gateway rejects as malformed three times is skipped, and the skip is
  reported on the next event (`transcript_skipped`) rather than silently.
- **Backfill is scoped.** `whales_hook.py --backfill --since YYYY-MM-DD`
  re-sends the unshipped transcript of past sessions started in the current
  directory; `--all-projects` widens it. There is no "everything" default.
  `--from-start` re-sends from the beginning of each transcript, and parts
  that had already arrived are stored again: the backend cannot match them.
- **Secrets are scrubbed before sending.** Common credential shapes — API
  keys, tokens, private key blocks, `SECRET=`-style assignments — are
  replaced. This is a regex, not a guarantee; it is here because a capture
  pipeline that hoovers up API keys is a liability regardless of intent.
- **Host noise is labelled, not dropped.** Sessions a host opens for its own
  bookkeeping (Conductor's workspace-title sessions) are tagged `synthetic`
  on every event. Prompts wrapped in a host's `<system_instruction>` preamble
  also carry `prompt_user_text`: what the designer typed, taken before the
  field cap, which a long preamble can otherwise push out of the prompt.
- **Never blocks a turn.** The POST happens in a detached grandchild; the
  hook itself returns in milliseconds and always exits 0. If the backend is
  down, the designer's turn is unaffected.
- **Never silently broken.** Each upload's outcome is recorded in
  `~/.whales/capture_status.json`. `SessionStart` reports what it says —
  active (with when the last upload was confirmed), failing, or not yet
  confirmed — rather than assuming capture works because a token exists. A
  rejected token is also shown to the designer directly. The first upload
  after an outage carries a `capture_health` count of the events that did not
  get through.
- **Off switch:** `export WHALES_CAPTURE=off`. To stop entirely,
  `claude plugin uninstall whales` and delete `~/.whales/`.

## Critique sources: the file itself, never a copy

`universal_critique` and `self_critique` take a `source_id`, and
`skills/universal-critique/critique_source.py upload <path>` is how a file on
the designer's machine becomes one. An image is sent as it is. An HTML page is
sent as one self-contained file: everything it loads from this machine
(images, `srcset`, stylesheets and their own `url()`/`@import`, scripts,
fonts) is inlined, because whales renders the page on its own server, where
the page's neighbouring files do not exist. Internet references are left for
whales to load. The script prints what it bundled, what it could not find
(`missing`) and how many internet references it left as they are — nothing is
dropped silently.

The point is that the model never retypes a page into a tool call. That is
slow and costly (two photos as base64 are about 45k output tokens), and what
arrives is the model's copy, not the file. Pasted HTML is saved to a file once
and uploaded like any other.

`critique_source.py compare <before> <after>` shows a critiqued screen and its
rebuild side by side at the same height; each side is an image or an HTML page
(bundled the same way as an upload). It writes one self-contained HTML file
beside the after and, when Chrome, Chromium, Edge or Brave is installed (or
named by `WHALES_BROWSER`), a PNG of it. Standard library only, like the rest
of the script.

## Cursor

Cursor runs the same capture script, but not from this plugin's `hooks.json`.
Cursor does load a Claude Code plugin it finds, but it drops the `args` that
carry `--event`, so those firings exit quietly on purpose (see the script's
docstring). Its real hooks are in `~/.cursor/hooks.json`, which the whales
installer writes from **`plugins/whales/cursor/hooks.json` — the one list of
Cursor events**:

| Cursor event | `--event` | What it does |
|---|---|---|
| `sessionStart` | `SessionStart` | Tells the model its `cur:<id>` (`additional_context`) and sets `WHALES_CLIENT_SESSION_ID` for later hooks (`env`) |
| `beforeSubmitPrompt`, `afterFileEdit`, `preCompact`, `stop`, `sessionEnd` | as in Claude Code | Capture, same as Claude Code |
| `preToolUse` | `PreToolUse` | Adds `client_session_id` to whales tool calls through `updated_input`; no `permission`, so the designer's own rules apply |
| `postToolUse` (`Write`) | `DesignContext` | Context only: names a page or image just written and the upload command for it, so "critique this" uploads that file instead of drawing a stand-in. A canvas is said not to be a critique source |

Each entry runs `~/.whales/scripts/whales_hook.py`, the plugin's
`cursor/wrapper.py` copied there by the updater. It runs `capture_hook.py`
(this plugin's `whales_hook.py`, copied beside it) and, on a session start,
the updater — the installed plugin's copy when there is one, so a release can
fix a broken copy in `~/.whales`. A Cursor-only machine whose updater has not
succeeded for three days downloads a fresh one first.

Machines set up before 0.6.0 have the installer's old wrapper, which only
updates `capture_hook.py`. The new `capture_hook.py` starts the updater when
it sees that old wrapper beside it, and the updater replaces it: no
re-install.

## The `whales` command

`~/.whales/bin/whales` (linked into `~/.local/bin` when that exists) runs
`scripts/whales_cli.py`, which updates with the plugin:

| | |
|---|---|
| `whales status` | versions per app, the server, and what the last update did |
| `whales update` | check now instead of at the next session start |
| `whales doctor [--fix]` | find, and repair, anything missing or out of date |
| `whales logs` | the last update's output |
| `whales install claude\|cursor` | connect another app with the token already here (runs the installer's `--only`) |
| `whales uninstall [claude\|cursor]` | disconnect one app, or everything |

## Credential

`~/.whales/token`, mode 0600, written by the installer. The MCP server reads
the plugin's own `user_config` (macOS Keychain) instead; the hooks use the
file because `${user_config.*}` only interpolates into a hook's exec-form
`args`, which would expose the token in `ps`. The file is also what the Cursor
hooks read, since they have no plugin config.

`~/.whales/gateway` optionally overrides the gateway base URL, for pointing a
dev machine at a local backend.

## Development

```bash
claude plugin validate . --strict            # marketplace manifest
claude plugin validate ./plugins/whales --strict
python -m pytest plugins/whales/tests -q     # hook, updater, wrapper, CLI

# try it without touching your real config:
CLAUDE_CONFIG_DIR=$(mktemp -d) claude plugin marketplace add ./ \
  && CLAUDE_CONFIG_DIR=... claude plugin install whales@whales -y --config whales_token=TEST
```

`claude plugin details whales@whales` prints the component inventory and the
always-on token cost, which is worth watching — everything here is paid for in
every session's context.
