"""Harness slash commands and routing of inbound chat messages.

Routing order for each message from an allowed user:
  1. a reply to an open prompt (`approvals.Registry.handle`)
  2. a harness command (`/stop`, `/mode`, ...)
  3. anything else, unknown slash commands included, goes to Claude

No XMPP dependency: replies go through `session.say()`.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from .approvals import Registry
from .config import PERMISSION_MODES, Config
from .render import format_cost

log = logging.getLogger(__name__)

Handler = Callable[["Router", str], Awaitable[None]]


@dataclass(frozen=True)
class Command:
    name: str
    usage: str
    help: str
    handler: Handler


APPROVAL_HELP = (
    "Replying to prompts:\n"
    "• `y [id]` allow · `a [id]` allow and remember · `n [id] [reason]` deny\n"
    "• questions: `[id] 1,3` option numbers, or free text\n"
    "• plans: `y [id]` approve · `n [id] feedback` revise\n"
    "The ID can be left out when only one prompt is open."
)


class Router:
    """Routes chat messages to the registry, the harness commands or Claude.

    `session` is a `session.Session` (or anything with the same methods).
    """

    def __init__(self, session: Any, config: Config, registry: Registry | None = None) -> None:
        self.session = session
        self.config = config
        self.registry = registry if registry is not None else session.registry

    async def handle(self, text: str) -> None:
        if not text.strip():
            return
        outcome = self.registry.handle(text)
        if outcome is not None:
            if outcome.message:
                await self.session.say(outcome.message)
            return
        if await self.dispatch(text):
            return
        await self.session.submit(text)

    async def dispatch(self, text: str) -> bool:
        """Run a harness command. Returns False when `text` isn't one."""
        stripped = text.strip()
        if not stripped.startswith("/"):
            return False
        parts = stripped[1:].split(None, 1)
        if not parts:
            return False
        cmd = COMMANDS.get(parts[0].lower())
        if cmd is None:
            return False
        arg = parts[1].strip() if len(parts) > 1 else ""
        try:
            await cmd.handler(self, arg)
        except Exception as e:  # noqa: BLE001
            log.exception("/%s failed", cmd.name)
            await self.session.say(f"⚠️ /{cmd.name} failed: {e}")
        return True

    # -- commands ------------------------------------------------------------------

    async def _help(self, _arg: str) -> None:
        lines = ["*Harness commands*"]
        lines += [f"`{c.usage}` — {c.help}" for c in COMMANDS.values()]
        lines.append("Any other `/command` (e.g. `/clear`, `/compact`, skills) goes to Claude.")
        lines += ["", APPROVAL_HELP]
        await self.session.say("\n".join(lines))

    async def _status(self, _arg: str) -> None:
        s = self.session.status()
        if not s.connected:
            state = "⚪ not connected"
        elif s.busy:
            state = "⏳ working"
        else:
            state = "🟢 idle"
        if s.queued:
            state += f" · {s.queued} queued"
        if s.open_prompts:
            state += f" · {s.open_prompts} open prompt{'s' if s.open_prompts != 1 else ''}"
        lines = [
            state,
            f"session: `{s.session_id or '–'}`",
            f"model: {s.model or 'default'} · mode: {s.mode or 'default'}",
            f"cost: {format_cost(s.session_cost_usd)} this session · "
            f"{format_cost(s.total_cost_usd)} total",
            f"auth: {s.auth or 'unknown until the first turn'}",
        ]
        await self.session.say("\n".join(lines))

    async def _stop(self, _arg: str) -> None:
        if not await self.session.interrupt():
            await self.session.say("nothing to stop")

    async def _plan(self, arg: str) -> None:
        a = arg.lower()
        if a in ("", "on"):
            if self.session.mode == "plan":
                await self.session.say("already in plan mode")
                return
            await self.session.set_mode("plan")
            await self.session.say("📝 plan mode: Claude will propose a plan before changing anything")
        elif a == "off":
            if self.session.mode != "plan":
                await self.session.say("not in plan mode")
                return
            back = self.session.mode_before_plan
            await self.session.set_mode(back)
            await self.session.say(f"plan mode off · mode: {back}")
        else:
            await self.session.say("usage: /plan [off]")

    async def _mode(self, arg: str) -> None:
        if not arg:
            await self.session.say(
                f"mode: {self.session.mode or 'default'}\nmodes: {', '.join(PERMISSION_MODES)}"
            )
            return
        mode = _match_mode(arg)
        if mode is None:
            await self.session.say(f"unknown mode {arg!r}; modes: {', '.join(PERMISSION_MODES)}")
            return
        if mode == "bypassPermissions" and not self.config.claude.allow_bypass:
            await self.session.say("bypassPermissions is disabled (set claude.allow_bypass = true)")
            return
        await self.session.set_mode(mode)
        await self.session.say(f"mode: {mode}")

    async def _model(self, arg: str) -> None:
        if not arg:
            await self.session.say(f"model: {self.session.model or 'default'}")
            return
        model = None if arg.lower() == "default" else arg
        await self.session.set_model(model)
        await self.session.say(f"model: {model or 'default'}")

    async def _new(self, _arg: str) -> None:
        await self.session.restart()
        await self.session.say("🆕 new conversation")

    async def _resume(self, arg: str) -> None:
        if not arg or len(arg.split()) != 1:
            current = self.session.session_id
            hint = f"\ncurrent session: `{current}`" if current else ""
            await self.session.say(f"usage: /resume <session-id>{hint}")
            return
        await self.session.restart(resume=arg)
        await self.session.say(f"↩️ resumed `{arg}`")

    async def _commands(self, _arg: str) -> None:
        harness = " ".join(f"/{n}" for n in COMMANDS)
        claude = sorted({c.lstrip("/") for c in self.session.slash_commands} - set(COMMANDS))
        lines = [f"*Harness:* {harness}"]
        if claude:
            lines.append("*Claude:* " + " ".join(f"/{c}" for c in claude))
        else:
            lines.append("*Claude:* (not known yet; they are listed after the first message)")
        await self.session.say("\n".join(lines))


def _match_mode(arg: str) -> str | None:
    a = arg.strip().lower()
    for m in PERMISSION_MODES:
        if m.lower() == a:
            return m
    return None


COMMANDS: dict[str, Command] = {
    c.name: c
    for c in (
        Command("help", "/help", "this help", Router._help),
        Command("status", "/status", "session, model, mode, cost, auth", Router._status),
        Command("stop", "/stop", "interrupt Claude, drop queued messages and open prompts", Router._stop),
        Command("plan", "/plan [off]", "enter or leave plan mode", Router._plan),
        Command("mode", "/mode [mode]", "show or set the permission mode", Router._mode),
        Command("model", "/model [name|default]", "show or set the model", Router._model),
        Command("new", "/new", "start a fresh conversation", Router._new),
        Command("resume", "/resume <id>", "resume a conversation by session ID", Router._resume),
        Command("commands", "/commands", "list available slash commands", Router._commands),
    )
}
