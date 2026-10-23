# claude-xmpp-agent

Control a Claude Code session from any XMPP client. A bot joins a group chat (MUC) and forwards messages from allowed users to Claude. It posts Claude's replies, tool activity, permission prompts and questions back to the room, and you answer them from the chat.

Built on the [Claude Agent SDK](https://pypi.org/project/claude-agent-sdk/) and [slixmpp](https://pypi.org/project/slixmpp/).

## Prerequisites

- **Python 3.11+** and [uv](https://docs.astral.sh/uv/).
- **Claude authentication** on the machine that runs the bot. The SDK ships its own Claude Code CLI and uses the same login as `claude` (run `claude` once and log in), or `ANTHROPIC_API_KEY`.
- **A bot account** on an XMPP server, e.g. `claude-bot@example.org`.
- **A non-anonymous room.** The bot checks each sender's *real* JID against an allowlist, so it must be able to see real JIDs. Configure the room as:
  - non-anonymous ("who can see real JIDs: everyone", or at least moderators, with the bot as moderator)
  - members-only (recommended), with the bot and your users as members
  - not publicly listed (recommended)

  If the bot can't see real JIDs it posts a warning and ignores everyone.

## Setup

```sh
uv sync
cp config.example.toml config.toml   # config.toml is git-ignored
$EDITOR config.toml
```

### Config

| Section | Key | Default | Meaning |
|---|---|---|---|
| `[xmpp]` | `jid` | — | Bot account JID |
| | `password` | `$XMPP_PASSWORD` | Bot password; the config value wins over the env var |
| | `room` | — | Room JID, e.g. `claude@conference.example.org` |
| | `nick` | `claude` | Nick in the room (`_` is appended on conflict) |
| | `room_password` | — | Room password, if any |
| `[access]` | `allowed_jids` | — | Bare JIDs allowed to talk to Claude (required, non-empty) |
| `[claude]` | `cwd` | `.` | Working directory for Claude; relative to the config file |
| | `model` | CLI default | Model name or alias |
| | `permission_mode` | `default` | `default`, `acceptEdits`, `plan`, `dontAsk`, `auto`, `bypassPermissions` |
| | `allowed_tools` | `[]` | Tools that never need approval, e.g. `["Read", "Grep", "Glob"]` |
| | `setting_sources` | `["user", "project", "local"]` | Which Claude settings files to load (permission rules, `CLAUDE.md`, commands) |
| | `allow_bypass` | `false` | Must be `true` to use `bypassPermissions` (config or `/mode`) |
| `[ui]` | `verbosity` | `normal` | `quiet` (text + errors), `normal` (+ one line per tool call), `verbose` (+ tool results, subagents) |
| | `max_chunk` | `3000` | Max characters per chat message; longer output is split |
| | `approval_timeout` | none | Seconds before an unanswered prompt is denied |

Unknown keys are rejected, so typos show up at startup.

## Running

```sh
uv run claude-xmpp --config config.toml
```

Options: `-c/--config PATH` (default `config.toml`), `--resume SESSION_ID` to continue an earlier Claude session, `--debug` for verbose logs. Logs go to stderr. Stop with Ctrl+C or SIGTERM.

Exit codes: `0` normal shutdown, `1` fatal error (Claude failed to start, bad XMPP password, bot banned), `2` config error.

The bot reconnects and rejoins the room on its own. Messages produced while it's away are held (up to 200) and posted on rejoin.

## Using it

Type in the room. Every message from an allowed user goes to Claude. If Claude is busy, the message is queued and the bot says how many are ahead.

### Harness commands

These run in the bot, not in Claude:

| Command | What it does |
|---|---|
| `/help` | Commands and reply syntax |
| `/status` | Idle / working, queue, open prompts, session ID, model, mode, cost |
| `/stop` | Interrupt Claude, drop queued messages, cancel open prompts |
| `/plan [off]` | Enter plan mode, or go back to the previous mode |
| `/mode [mode]` | Show or set the permission mode |
| `/model [name\|default]` | Show or set the model |
| `/new` | Start a fresh conversation |
| `/resume <id>` | Resume a conversation by session ID |
| `/commands` | List harness commands and Claude's slash commands |

Any other `/command` goes to Claude unchanged: `/clear`, `/compact`, `/context`, skills and your `.claude/commands/*`.

### Answering prompts

Each prompt has a five-letter ID. The ID can be left out when only one prompt of that kind is open.

| Prompt | Reply |
|---|---|
| Tool permission | `y [id]` allow · `a [id]` allow and remember (when offered) · `n [id] [reason]` deny |
| Question | `[id] 1,3` option numbers, or free text; one line per question for multi-question prompts |
| Plan (plan mode) | `y [id]` approve · `n [id] feedback` ask for a revision |

Examples: `y`, `n qwert use the other file`, `2`, `xkcdz 1,3`.

## Security

- **Allowlisted users can run tools on this machine** as the user running the bot: shell commands, file edits, network access. Only allowlist people you would give a shell to.
- Access control depends on the room showing real JIDs, and on the XMPP server being trusted to report them. Use a members-only room on a server you trust.
- Prefer `permission_mode = "default"` and a short `allowed_tools` list. `bypassPermissions` skips every prompt and is disabled unless `allow_bypass = true`.
- Point `cwd` at the project you want Claude to work on, not your home directory.
- Keep the password out of `config.toml` with `XMPP_PASSWORD` if the file might be shared.

## Development

```sh
uv run pytest
```

Layout: `config.py` (TOML loading), `approvals.py` (prompt IDs and reply parsing), `render.py` (SDK messages → chat text), `session.py` (the SDK client), `commands.py` (routing and harness commands), `bot.py` (XMPP), `__main__.py` (CLI). See `plan.md` for design notes.

Out of scope: multiple rooms or sessions, file uploads, 1:1 chat control, message corrections.
