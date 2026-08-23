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
| **Hooks** | Capture on SessionStart, UserPromptSubmit, PostToolUse(Write\|Edit), PreCompact, Stop, SessionEnd |
| **Permissions** | Read-and-record Whales tools pre-allowed |

`generate_design_system` and `register_design_system` are deliberately **not**
in the permission allowlist. One spawns a minute-long extraction, the other
overwrites a document the designer authored — neither should happen without
them seeing a prompt.

## Why hooks and not just an MCP server

An MCP server can only see its own tool calls. A design the agent writes
straight to disk with the native Write tool is completely invisible to it.
That gap is inherently local and cannot be closed by any transport choice.

The hooks also fix something the MCP server cannot: the host's session id and
the MCP transport's `mcp-session-id` are different identifier spaces, so a
design submitted through `submit_design` and then hand-edited would land in
two buckets that can never be joined. `SessionStart` injects the host session
id and tells the model to pass it as `client_session_id`, which is what makes
one session one story — and what lets outcomes be *observed* (submission then
approval means accepted) rather than guessed at by a model after the fact.

## Capture, stated plainly

With a token configured, the hooks send session activity — including
transcript content — to the Whales backend, so the designer's profile
improves from real work. Specifics:

- **Deltas, not whole files.** A byte offset per session is tracked under
  `~/.whales/offsets/`; each event ships only what was appended since the
  last one, capped at 512KB (tail kept).
- **Secrets are scrubbed before sending.** Common credential shapes — API
  keys, tokens, private key blocks, `SECRET=`-style assignments — are
  replaced. This is a regex, not a guarantee; it is here because a capture
  pipeline that hoovers up API keys is a liability regardless of intent.
- **Never blocks a turn.** The POST happens in a detached grandchild; the
  hook itself returns in milliseconds and always exits 0. If the backend is
  down, the designer notices nothing.
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
