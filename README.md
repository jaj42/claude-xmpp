# claude-xmpp-agent

Control a Claude Code session from any XMPP client. A bot joins a group chat (MUC) and forwards messages from allowed users to Claude. It posts Claude's replies, tool activity, permission prompts and questions back to the room, and you answer them from the chat.

Built on the [Claude Agent SDK](https://pypi.org/project/claude-agent-sdk/) and [slixmpp](https://pypi.org/project/slixmpp/).

## Prerequisites

- **Python 3.11+** and [uv](https://docs.astral.sh/uv/), or Docker (see [Docker](#docker)).
- **Claude authentication**: an OAuth token from a Claude subscription or an API key. See [Authentication](#authentication).
- **A bot account** on an XMPP server, e.g. `claude-bot@example.org`.
- **A non-anonymous room.** The bot checks each sender's *real* JID against an allowlist, so it must be able to see real JIDs. Configure the room as:
  - non-anonymous ("who can see real JIDs: everyone", or at least moderators, with the bot as moderator)
  - members-only (recommended), with the bot and your users as members
  - not publicly listed (recommended)

  If the bot can't see real JIDs it posts a warning and ignores everyone.

## Authentication

The SDK ships its own Claude Code CLI, and the bot runs it with the bot's environment. Give it **one** of these:

- **`CLAUDE_CODE_OAUTH_TOKEN`** (Pro, Max, Team or Enterprise plan). Recommended when you run the bot for yourself.
- **`ANTHROPIC_API_KEY`** from [platform.claude.com](https://platform.claude.com), billed per token. Use this if anyone else is on the allowlist (see below).
- For `uv run` only: an existing `claude` login on the host (`~/.claude/.credentials.json`).

### Getting the OAuth token

1. Run `claude setup-token` on any machine with a browser. It opens the usual claude.ai sign-in and then prints the token. It doesn't save it anywhere, so copy it right away.

   No Claude Code on the host? Use the CLI bundled in the Docker image (build it first, see [Docker](#docker)):

   ```sh
   docker run --rm -it --entrypoint claude claude-xmpp setup-token
   ```

   The browser can't reach the container's callback, so paste the code the browser shows back into the terminal.
2. The token lasts **one year**. Note the renewal date: once it expires, every turn fails with an auth error until you set a new one.
3. It can only make model requests. claude.ai connectors and Remote Control don't work with it; MCP servers configured locally do.

### Giving it to the bot

Set `CLAUDE_CODE_OAUTH_TOKEN` in the bot's environment. The bot doesn't read it itself; the CLI does.

- `uv run`: `export CLAUDE_CODE_OAUTH_TOKEN=…` before starting, or put it in the service's environment (e.g. a systemd `EnvironmentFile=` with mode `0600`).
- Docker: a line `CLAUDE_CODE_OAUTH_TOKEN=…` in `.env`, which `compose.yaml` loads. Without compose: `docker run --env-file .env …`. `.env` is ignored by git and by the Docker build.
- Never put it in `config.toml` or in the image.

**Set only one credential.** `ANTHROPIC_API_KEY` wins over `CLAUDE_CODE_OAUTH_TOKEN`: if both are set, usage is billed to the API key.

**Checking which one is used:** the `✓ done` line shows a `$` cost either way (only an estimate on a subscription), so it can't tell you. `/status` has an `auth:` line instead, and the bot logs it: `API key (ANTHROPIC_API_KEY)` for a key, `subscription (Claude Max)` for a token or login. It's confirmed by the first turn; before that `/status` shows the account's plan.

### Subscription or API key?

Anthropic asks developers of products built on the Agent SDK to use API keys, and doesn't allow Free/Pro/Max credentials to serve other users. Pro/Max limits "assume ordinary, individual usage of Claude Code and the Agent SDK", so running this bot for yourself with your own token fits. If other people are on the allowlist, use an API key: their prompts would otherwise run on your subscription.

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
| `[mcp_servers.<name>]` | | none | MCP servers for Claude, see below |

Unknown keys are rejected, so typos show up at startup.

### MCP servers

Each `[mcp_servers.<name>]` table adds one server, with the same fields as Claude Code's `.mcp.json`:

```toml
[mcp_servers.sfar]
url = "http://mcp.example.org:1234/mcp"              # type = "http" by default, or "sse"
headers = { Authorization = "Bearer ${SFAR_TOKEN}" }

[mcp_servers.files]
command = "npx"                                       # type = "stdio" when there's no url
args = ["-y", "@modelcontextprotocol/server-filesystem", "/workspace"]
env = { SOME_VAR = "value" }
```

`${VAR}` in any value is replaced by that environment variable when the bot starts, so tokens can go in `.env` (`SFAR_TOKEN=…`) instead of the config. If a referenced variable is unset or empty, the bot refuses to start (exit code `2`).

These servers are added to any configured in Claude's own settings (`setting_sources`). Their tools are called `mcp__<name>__<tool>` and need approval like any other tool; put `"mcp__<name>"` in `allowed_tools` to allow all of one server's tools.

## Running

```sh
uv run claude-xmpp --config config.toml
```

Options: `-c/--config PATH` (default `config.toml`), `--resume SESSION_ID` to continue an earlier Claude session, `--debug` for verbose logs. Logs go to stderr. Stop with Ctrl+C or SIGTERM.

Exit codes: `0` normal shutdown, `1` fatal error (Claude failed to start, bad XMPP password, bot banned), `2` config error.

The bot reconnects and rejoins the room on its own. Messages produced while it's away are held (up to 200) and posted on rejoin.

## Docker

The image runs the bot as a non-root user `agent` (Claude Code refuses `bypassPermissions` as root). It contains the locked dependencies and the Claude CLI bundled with `claude-agent-sdk`, on the `PATH` as `claude` (e.g. `docker compose exec claude-xmpp claude mcp list`); to update Claude Code, bump the SDK in `uv.lock` and rebuild. Auto-updates are off.

### Mounts and environment

| Path in the container | What | Notes |
|---|---|---|
| `/config/config.toml` | Your config, read-only | Set `claude.cwd = "/workspace"` (absolute: relative paths resolve against the config file's directory) |
| `/workspace` | The project Claude works on | Bind mount |
| `/home/agent/.claude` | Claude's sessions, settings, login and user skills | Bind mount of `./home` (git-ignored), so `/resume` and settings survive restarts |

Secrets go in `.env`, never in the image or the config:

```sh
XMPP_PASSWORD=…
CLAUDE_CODE_OAUTH_TOKEN=…     # or ANTHROPIC_API_KEY=…, not both
UID=1000                      # `id -u` of the workspace owner
GID=1000                      # `id -g`
WORKSPACE=/path/to/project    # default ./workspace
USERNS_MODE=keep-id:uid=1000,gid=1000   # rootless podman only, same numbers as UID/GID
```

**File ownership:** the container user has uid/gid 1000 by default. Files Claude creates in `/workspace` get that owner on the host. If your uid differs, set `UID`/`GID` in `.env` (compose passes them as build args) or build with `--build-arg UID=$(id -u) --build-arg GID=$(id -g)`. Changing them requires a rebuild.

**Rootless podman:** uid 1000 inside the container is not your host uid, it maps to a subordinate uid (see `podman unshare cat /proc/self/uid_map`), so files show up on the host owned by a number like `100999`. Set `USERNS_MODE=keep-id:uid=1000,gid=1000` in `.env` (with `podman run`: `--userns=keep-id:uid=1000,gid=1000`) to map the container user to your own. If the `claude-home` volume was created before this, its files keep the old owner; fix them with `podman unshare chown -R 0:0 "$(podman volume inspect --format '{{.Mountpoint}}' claude-xmpp_claude-home)"` (uid 0 in `podman unshare` is you).

### With compose

```sh
cp config.example.toml config.toml   # set claude.cwd = "/workspace"
$EDITOR config.toml .env
docker compose up -d --build
docker compose logs -f
```

`compose.yaml` uses `init: true` (to reap the CLI's child processes), `restart: unless-stopped`, and the three mounts above. `docker compose stop` sends SIGTERM and the bot shuts down cleanly.

### Without compose

```sh
docker build -t claude-xmpp --build-arg UID=$(id -u) --build-arg GID=$(id -g) .
docker run -d --name claude-xmpp --init --restart unless-stopped --env-file .env \
  -v "$PWD/config.toml:/config/config.toml:ro" \
  -v /path/to/project:/workspace \
  -v claude-home:/home/agent/.claude \
  claude-xmpp
```

Extra options go after the image name, replacing the default `--config /config/config.toml`, e.g. `claude-xmpp --config /config/config.toml --debug`.

## Using it

Type in the room. Every message from an allowed user goes to Claude. If Claude is busy, the message is queued and the bot says how many are ahead.

### Harness commands

These run in the bot, not in Claude:

| Command | What it does |
|---|---|
| `/help` | Commands and reply syntax |
| `/status` | Idle / working, queue, open prompts, session ID, model, mode, cost, auth |
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
- Point `cwd` at the project you want Claude to work on, not your home directory. In Docker, Claude only sees `/workspace` and its own home, which limits what a mistake can reach, but it still has network access and can use your credentials.
- Keep the password out of `config.toml` with `XMPP_PASSWORD` if the file might be shared.

## Development

```sh
uv run pytest
```

Layout: `config.py` (TOML loading), `approvals.py` (prompt IDs and reply parsing), `render.py` (SDK messages → chat text), `session.py` (the SDK client), `commands.py` (routing and harness commands), `bot.py` (XMPP), `__main__.py` (CLI). See `plan.md` for design notes.

Out of scope: multiple rooms or sessions, file uploads, 1:1 chat control, message corrections.
