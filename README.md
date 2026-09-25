# whales-claude-code

The Whales plugin for Claude Code, and the marketplace that serves it.

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

Get a token from your Whales settings page. `--yes` is required when stdin
isn't a TTY, which is the case inside `curl | bash`.

## What's in it

| | |
|---|---|
| **MCP server** | The Whales gateway over streamable HTTP, authenticated with `${user_config.whales_token}` |
| **Skills** | `design-profile` (apply the designer's conventions to UI work), `design-system` (extract/register/maintain a design system) |
| **Agent** | `design-review` — conformance-checks a diff in its own context |
| **Hooks** | Capture on SessionStart, UserPromptSubmit, PostToolUse(Write\|Edit), PreCompact, Stop, SessionEnd; PreToolUse on Whales tools adds `client_session_id` |
**Permissions are not in the plugin.** Claude Code ignores `permissions` in a
plugin's `settings.json`; only `agent` and `subagentStatusLine` take effect
there (checked 2026-09-24 on 2.1.282: a plugin-allowed command still needed
approval, while the same rule passed via `--allowedTools` did not). The Whales
installer adds the read-and-record tools to the designer's own
`~/.claude/settings.json` instead — see `WHALES_ALLOW_TOOLS` in the installer
for the list and the reasons for what it leaves out.

## Why hooks and not just an MCP server

An MCP server can only see its own tool calls. A design the agent writes
straight to disk with the native Write tool is completely invisible to it.
That gap is inherently local and cannot be closed by any transport choice.

The hooks also fix something the MCP server cannot: the host's session id and
the MCP transport's `mcp-session-id` are different identifier spaces, so a
design submitted through `submit_design` and then hand-edited would land in
two buckets that can never be joined. A `PreToolUse` hook adds the host
session id to every Whales tool call as `client_session_id`, which is what
makes one session one story — and what lets outcomes be *observed*
(submission then approval means accepted) rather than guessed at by a model
after the fact. It rewrites the arguments only; it sets no permission
decision, so the designer's own allow/ask rules still apply. `SessionStart`
also tells the model the id, as the fallback for hosts that do not run the
hook — on its own that reached only 4 of 150 calls.

## Capture, stated plainly

With a token configured, the hooks send session activity — including
transcript content — to the Whales backend, so the designer's profile
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
python -m pytest plugins/whales/tests -q     # hook script

# try it without touching your real config:
CLAUDE_CONFIG_DIR=$(mktemp -d) claude plugin marketplace add ./ \
  && CLAUDE_CONFIG_DIR=... claude plugin install whales@whales -y --config whales_token=TEST
```

`claude plugin details whales@whales` prints the component inventory and the
always-on token cost, which is worth watching — everything here is paid for in
every session's context.
