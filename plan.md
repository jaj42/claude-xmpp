# Claude XMPP Agent — implementation plan


## Context
The goal is to control a Claude Code session from any XMPP client. A bot joins a MUC (group chat) and forwards messages from allowed users to Claude. It posts Claude's replies, tool activity, permission prompts and questions back to the room.

Decisions:
- **Claude Agent SDK daemon** (`claude-agent-sdk`, `ClaudeSDKClient`), not a channel MCP server.
- **slixmpp from PyPI.** The local `slixmpp/` checkout is reference only.
- **Real-JID allowlist** for access control, which needs a non-anonymous room (members-only recommended).
- **uv** for project management.

Workflow:
- **After each step:** update this plan (mark the step done, record findings and deviations), then make a git commit for that step.

Slash commands:
- **Sent to Claude unchanged:** `/clear`, `/compact`, `/context`, `/usage`, skills, and `.claude/commands/*`. The SDK runs these when they arrive as prompt text.
- **Handled by the harness** (terminal-only in Claude Code): `/plan`, `/mode`, `/model`, `/stop`, `/new`, `/resume`, `/status`, `/commands`, `/help`. These map to SDK calls: `set_permission_mode`, `set_model`, `interrupt()`, and reconnecting with `resume=`.

Final layout (`/workspace/claude_xmpp_agent/`):
```
pyproject.toml  config.example.toml  README.md  plan.md
src/claude_xmpp_agent/  __main__.py config.py bot.py session.py approvals.py commands.py render.py
tests/  test_config.py test_approvals.py test_render.py test_commands.py test_gating.py
```

---

## Step 1 — Project scaffold ✅ done
- `uv init --package --name claude-xmpp-agent --python 3.11` in `/workspace/claude_xmpp_agent/`
- `uv add slixmpp claude-agent-sdk`
- `uv add --dev pytest pytest-asyncio`
- Script entry: `claude-xmpp = "claude_xmpp_agent.__main__:main"`
- `.gitignore`: `.venv`, `config.toml`, `slixmpp/`. Leave the `slixmpp/` checkout untouched.
- Read the installed `claude_agent_sdk` types to confirm exact names and fields (`ToolPermissionContext.suggestions`, `PermissionUpdate.destination`, `get_server_info`, message classes).

**Done when:** `uv run python -c "import slixmpp, claude_agent_sdk"` works.

**Notes (done):**
- Installed: slixmpp 1.17.0, claude-agent-sdk 0.2.173, pytest 9.1.1, pytest-asyncio 1.4.0.
- Python 3.11 isn't installed here and uv downloads are disabled, so `.python-version` is `3.13`. `requires-python` stays `>=3.11`.
- `__main__.py` is a stub until Step 8. `.gitignore` also covers `__pycache__/` and `.pytest_cache/`.
- SDK names confirmed:
  - `ToolPermissionContext.suggestions: list[PermissionUpdate]`, plus `tool_use_id`, `title`, `display_name`, `description`, `decision_reason`, `blocked_path`.
  - `PermissionUpdate.destination: "userSettings"|"projectSettings"|"localSettings"|"session"`.
  - `PermissionResultAllow(updated_input, updated_permissions)`, `PermissionResultDeny(message, interrupt)`.
  - `ClaudeSDKClient`: `connect`, `query`, `receive_response`, `interrupt`, `set_permission_mode`, `set_model`, `get_server_info()` (returns the init dict, or None before connect finishes).
  - `PermissionMode` also includes `"dontAsk"` and `"auto"`; `/mode` should accept these.
  - Messages: `AssistantMessage`, `UserMessage`, `SystemMessage(subtype, data)`, `ResultMessage(subtype, duration_ms, is_error, num_turns, session_id, total_cost_usd, result, …)`. Blocks: `TextBlock`, `ThinkingBlock`, `ToolUseBlock`, `ToolResultBlock(is_error)`, plus `ServerToolUseBlock`/`ServerToolResultBlock`.
  - `can_use_tool` is skipped for tools allowed by `allowed_tools`, by the permission mode or by settings allow rules. A `PreToolUse` hook that returns *allow* also skips it, so the dummy hook (Step 5) must return no decision.

## Step 2 — Configuration (`config.py`, `config.example.toml`) ✅ done
- Dataclasses loaded with `tomllib`.
- `[xmpp]`: jid, password (fallback `$XMPP_PASSWORD`), room, nick, room_password
- `[access]`: `allowed_jids`
- `[claude]`: cwd, model, permission_mode = `"default"`, allowed_tools, setting_sources = `["user","project","local"]`, allow_bypass = false
- `[ui]`: verbosity `quiet|normal|verbose`, max_chunk = 3000, approval_timeout (optional)
- Validate: room and allowlist are not empty; JIDs are normalised to bare form.

**Done when:** `tests/test_config.py` passes (load, env fallback, validation errors).

**Notes (done):**
- API: `load_config(path, env=None) -> Config` (frozen dataclasses `XmppConfig`, `AccessConfig`, `ClaudeConfig`, `UiConfig`); `ConfigError` for every failure; `normalize_jid()` is public so the bot (Step 7) can reuse it.
- JIDs are normalised with `slixmpp.JID(...).bare` (lower-cases, strips the resource). `allowed_jids` is a `frozenset`. The room must contain `@`.
- Extra validation beyond the plan: unknown keys are rejected (catches typos); `permission_mode`, `setting_sources`, `verbosity` are checked against fixed sets; `bypassPermissions` in config requires `allow_bypass = true`; `max_chunk >= 200`; `approval_timeout > 0`.
- `claude.cwd` defaults to `.`, supports `~`, and a relative path is resolved against the config file's directory. It must exist.
- The config password wins over `$XMPP_PASSWORD`.
- `pyproject.toml` now sets `testpaths = ["tests"]` (pytest was collecting the `slixmpp/` checkout) and `asyncio_mode = "auto"`.
- 25 tests pass.

## Step 3 — Approvals registry (`approvals.py`) — pure logic ✅ done
- IDs: 5 letters from `a-km-z`, unique among open items.
- `Pending(kind: permission|question|plan, future, payload)`, with `open() / resolve() / cancel_all(reason)`.
- Reply parser `^(y|yes|n|no|a|always)(?:\s+([a-km-z]{5}))?(?:\s+(.*))?$` (case-insensitive):
  - The ID may be left out when exactly one item is open.
  - Trailing text after `n` becomes the deny reason or feedback.
- Question answers: `<id> 1,3`, numbers, or free text (when exactly one question is open). Multi-question prompts are answered one line per question. Numbers map to option labels; anything else is used as free text.

**Done when:** `tests/test_approvals.py` passes.

**Notes (done):**
- API: `Registry.open(kind, payload) -> Pending` (needs a running loop), `resolve(id, value)`, `discard(id)` (for timeouts), `cancel_all(reason) -> int`, `get`, `items(*kinds)`, `len()`. `handle(text) -> Outcome | None`: None means "not a reply, route it onwards"; an `Outcome` with `resolved=None` carries a `message` to post back (ambiguous, bad number, wrong kind).
- Future results: permission/plan → `Reply(decision: "yes"|"no"|"always", id, text)`; question → `dict[question text, answer]` (the SDK `answers` shape; multi-select labels joined with `", "`); `cancel_all` → `Cancelled(reason)` instead of cancelling the future, so `_can_use_tool` gets a plain value.
- Deviations / extra rules:
  - "Exactly one open" is counted per kind: `y/n/a` look only at permission and plan items, answers only at questions. So with one permission and one question open, `y` answers the permission and `2` answers the question.
  - Five letters after `n` that aren't an open ID are part of the reason (`n words here`), with the original case kept.
  - Messages starting with `/` are never treated as replies, so `/stop` still works while a question is open. Any other text is an answer when exactly one question is open.
  - `a` on a plan is refused with a hint; `y <question-id>` and `<permission-id> 1` get an error message.
  - A single question accepts multi-line free text (lines joined with spaces). Out-of-range numbers or several numbers on a single-select question are errors, and the item stays open.
- Pure helpers `parse_reply`, `parse_answer`, `parse_answers` are public for the renderer and tests.
- 83 tests pass (58 new).

## Step 4 — Rendering (`render.py`) — pure logic
- TextBlock → text.
- ToolUseBlock:
  - normal: one line (`🔧 Bash: \`cmd\``, `✏️ Edit path`, `📖 Read path`)
  - verbose: also shows truncated results
  - quiet: hidden
- ToolResultBlock: only `is_error` results are shown (in normal mode).
- ThinkingBlock: hidden.
- ResultMessage → `✓ done · N turns · $X · Ns`, or `✗ subtype + errors`.
- SystemMessage `compact_boundary` → "context compacted".
- Formatters for permission, question and plan prompts (each includes the ID and how to reply).
- `chunk(text, max)`: split on line boundaries and never inside a ``` fence (close and reopen it across chunks). Use XEP-0393 styling.

**Done when:** `tests/test_render.py` passes.

## Step 5 — Claude session (`session.py`)
- Builds `ClaudeAgentOptions` from config:
  - `system_prompt` preset `claude_code` + an `append` note: the user is on XMPP, so keep replies chat-sized and in plain text
  - `can_use_tool`
  - the documented dummy `PreToolUse` hook workaround
- Holds one `ClaudeSDKClient`, a prompt `asyncio.Queue`, and one worker task. The worker runs `query()`, then iterates `receive_response()` and sends each message to `render` and then an output callback.
- Tracks session_id, model, `slash_commands` (from init), cumulative cost, and busy state.
- `submit(text)`: replies "queued (n ahead)" when busy.
- `interrupt()`: `client.interrupt()`, empty the queue, cancel pending approvals. The worker drains to the ResultMessage.
- `set_mode()` and `set_model()`: thin wrappers around the SDK calls. `restart(resume=None)` reconnects with a fresh session or `resume=session_id`.
- `_can_use_tool`:
  - `AskUserQuestion`: allow with `{"questions", "answers"}`.
  - `ExitPlanMode`: show the plan. Approve → allow. Feedback → deny with a message.
  - Any other tool: `y` → allow, `a` → allow + `updated_permissions` from `ctx.suggestions`, `n` → deny with reason.
  - The output callback is injected, so the session has no XMPP dependency.

**Done when:** import smoke test passes. Optionally, a local run where the output callback prints to stdout answers a prompt.

## Step 6 — Harness commands (`commands.py`)
- Dispatch only for messages starting with `/`. Unknown commands go to Claude as-is (SDK commands like `/clear`, `/compact`, skills).
- Commands:
  - `/help`
  - `/status`
  - `/stop`
  - `/plan [off]`
  - `/mode <m>` (refuses `bypassPermissions` unless `allow_bypass`)
  - `/model [name]`
  - `/new`
  - `/resume <id>`
  - `/commands`
- Routing order for each inbound message: approval reply → harness command → `session.submit`.

**Done when:** `tests/test_commands.py` passes, using a fake session that records calls.

## Step 7 — XMPP bot (`bot.py`)
- `ClientXMPP` with xep_0030, xep_0045, xep_0199 (keepalive), xep_0085 (chat states).
- `session_start`: `send_presence()`, then `join_muc_wait(room, nick, maxstanzas=0)`.
- On join, warn if real JIDs aren't visible (semi-anonymous room).
- `muc::<room>::message` handler:
  - Drop our own nick and `<delay>` history.
  - Resolve the real JID with `get_jid_property(room, nick, 'jid')`.
  - Drop the message if the JID is missing or not in the allowlist. Warn once per nick.
  - Pass allowed messages to the router.
- `send(text)`: chunked `groupchat` messages. `composing` / `active` chat states while Claude is busy.
- Auto-reconnect and rejoin.

**Done when:** `tests/test_gating.py` passes (stub messages and roster: allowed passes; unknown, missing JID and history are dropped).

## Step 8 — Entry point and docs (`__main__.py`, `README.md`)
- CLI: `--config`, `--debug`. Logging goes to stderr. One asyncio loop runs the bot and the session together. Shutdown on SIGINT/SIGTERM disconnects both.
- README covers:
  - prerequisites (Claude auth, members-only non-anonymous MUC, bot account)
  - config
  - running
  - the command table
  - the approval syntax
  - security notes (allowlisted users can run tools on this machine)

**Done when:** `uv run claude-xmpp --help` works.

## Step 9 — Verification
- `uv run pytest`: all green.
- End-to-end (manual, needs an XMPP account, a room and Claude auth):
  1. `uv run claude-xmpp --config config.toml`
  2. In Gajim, Dino or Conversations, ask "what's in this directory?" → tool line + answer + result line.
  3. Ask for a file edit → prompt with ID → `y <id>`.
  4. `/plan`, then a task → plan → `n <id> add tests` → revised plan → `y`.
  5. `/stop` mid-task; `/clear`; `/status`; `/model sonnet`.
  6. A message from a JID that isn't allowlisted is ignored.

## Out of scope
Multiple rooms or sessions, file uploads (XEP-0363), 1:1 chat control, message corrections.
