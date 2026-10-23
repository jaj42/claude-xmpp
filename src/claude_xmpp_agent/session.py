"""One long-lived Claude Agent SDK session, driven by chat messages.

No XMPP dependency: everything to show goes through the injected `output`
callback (one chat message per call; chunking is the sender's job), and
approval replies arrive through the shared `approvals.Registry`.

Message flow:
  submit(text) -> prompt queue -> worker: client.query(text), wait for the turn
  reader task: client.receive_messages() -> Renderer -> output
  can_use_tool -> registry item + prompt -> await the reply -> allow / deny
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from claude_agent_sdk import (
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ConversationResetMessage,
    HookMatcher,
    PermissionResultAllow,
    PermissionResultDeny,
    PermissionUpdate,
    ResultMessage,
    SystemMessage,
    ToolPermissionContext,
)

from .approvals import Cancelled, Pending, Registry, Reply
from .config import Config
from .render import (
    Renderer,
    describe_suggestions,
    format_permission,
    format_plan,
    format_question,
)

log = logging.getLogger(__name__)

Output = Callable[[str], Awaitable[None] | None]
BusyCallback = Callable[[bool], Awaitable[None] | None]
ClientFactory = Callable[[ClaudeAgentOptions], Any]

SYSTEM_APPEND = (
    "The user talks to you through an XMPP group chat, not a terminal. "
    "Keep replies short and chat-sized. Prefer plain text: chat clients render "
    "only *bold*, _italic_, `code` and ``` fenced blocks; avoid tables and "
    "large headings. The user approves tool calls and answers questions from "
    "the chat, so ask with AskUserQuestion when a choice is needed."
)


@dataclass
class Status:
    session_id: str | None
    model: str | None
    mode: str | None
    busy: bool
    queued: int
    open_prompts: int
    session_cost_usd: float
    total_cost_usd: float
    connected: bool


async def _call(cb: Callable[..., Any] | None, *args: Any) -> None:
    if cb is None:
        return
    result = cb(*args)
    if inspect.isawaitable(result):
        await result


async def _keep_stream_open(_input: Any, _tool_use_id: str | None, _ctx: Any) -> dict[str, Any]:
    """Dummy PreToolUse hook from the SDK docs' `can_use_tool` example.

    It returns no permission decision, so `can_use_tool` still runs (a hook
    that returns *allow* would skip it).
    """
    return {"continue_": True}


class Session:
    def __init__(
        self,
        config: Config,
        output: Output,
        registry: Registry | None = None,
        *,
        on_busy: BusyCallback | None = None,
        client_factory: ClientFactory = ClaudeSDKClient,
    ) -> None:
        self.config = config
        self.registry = registry if registry is not None else Registry()
        self._output = output
        self._on_busy = on_busy
        self._client_factory = client_factory
        cwd = config.claude.cwd
        self.renderer = Renderer(config.ui.verbosity, cwd=cwd)

        self.client: Any = None
        self.session_id: str | None = None
        self.model: str | None = config.claude.model
        self.mode: str | None = config.claude.permission_mode
        self.slash_commands: list[str] = []
        self.busy = False

        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._worker: asyncio.Task[None] | None = None
        self._reader: asyncio.Task[None] | None = None
        self._turn_done = asyncio.Event()
        self._lock = asyncio.Lock()  # serialises connect / restart
        self._mode_before_plan: str = (
            config.claude.permission_mode if config.claude.permission_mode != "plan" else "default"
        )
        # ResultMessage.total_cost_usd is a running total for the conversation;
        # it starts again from zero after /clear or a reconnect.
        self._cost_current = 0.0
        self._cost_base = 0.0

    # -- lifecycle -------------------------------------------------------------

    def build_options(self, resume: str | None = None) -> ClaudeAgentOptions:
        c = self.config.claude
        return ClaudeAgentOptions(
            cwd=str(c.cwd),
            model=self.model,
            permission_mode=self.mode,  # type: ignore[arg-type]
            allowed_tools=list(c.allowed_tools),
            setting_sources=list(c.setting_sources),  # type: ignore[arg-type]
            system_prompt={"type": "preset", "preset": "claude_code", "append": SYSTEM_APPEND},
            can_use_tool=self._can_use_tool,
            hooks={"PreToolUse": [HookMatcher(matcher=None, hooks=[_keep_stream_open])]},
            resume=resume,
        )

    async def start(self, resume: str | None = None) -> None:
        """Connect to Claude and start the worker. Safe to call once."""
        async with self._lock:
            await self._connect(resume)
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(self._work(), name="claude-worker")

    async def close(self) -> None:
        """Stop everything and disconnect."""
        self.registry.cancel_all("shutting down")
        if self._worker is not None:
            self._worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._worker
            self._worker = None
        async with self._lock:
            await self._disconnect()

    async def restart(self, resume: str | None = None) -> None:
        """Start a fresh conversation, or resume `resume` (a session ID)."""
        self._drain_queue()
        self.registry.cancel_all("session restarted")
        async with self._lock:
            await self._disconnect()
            self._fold_cost()
            self.session_id = None
            await self._connect(resume)
        if resume:
            self.session_id = resume
        self._finish_turn()

    async def _connect(self, resume: str | None) -> None:
        client = self._client_factory(self.build_options(resume))
        await client.connect()
        self.client = client
        self.renderer = Renderer(self.config.ui.verbosity, cwd=self.config.claude.cwd)
        info = None
        with contextlib.suppress(Exception):
            info = await client.get_server_info()
        if info:
            cmds = info.get("commands") or []
            names = [c.get("name") if isinstance(c, dict) else str(c) for c in cmds]
            self.slash_commands = [n for n in names if n]
        self._reader = asyncio.create_task(self._read(client), name="claude-reader")

    async def _disconnect(self) -> None:
        reader, self._reader = self._reader, None
        if reader is not None:
            reader.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await reader
        client, self.client = self.client, None
        if client is not None:
            with contextlib.suppress(Exception):
                await client.disconnect()

    @property
    def connected(self) -> bool:
        return self.client is not None and self._reader is not None and not self._reader.done()

    # -- prompts -----------------------------------------------------------------

    async def submit(self, text: str) -> None:
        """Queue a prompt for Claude. Says so in the chat when it has to wait."""
        ahead = self._queue.qsize() + (1 if self.busy else 0)
        await self._queue.put(text)
        if ahead:
            await self.say(f"⏳ queued ({ahead} ahead)")

    async def interrupt(self) -> bool:
        """Stop the current turn, drop queued prompts and open prompts.

        Returns False when there was nothing to stop.
        """
        dropped = self._drain_queue()
        cancelled = self.registry.cancel_all("stopped by user")
        if not self.busy:
            return bool(dropped or cancelled)
        if self.client is not None:
            try:
                await self.client.interrupt()
            except Exception as e:  # noqa: BLE001
                log.warning("interrupt failed: %s", e)
        # The reader posts the final ResultMessage (`⏹ stopped`) and ends the turn.
        return True

    def _drain_queue(self) -> int:
        n = 0
        while True:
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                return n
            n += 1

    async def _work(self) -> None:
        while True:
            text = await self._queue.get()
            try:
                await self._run_turn(text)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                log.exception("turn failed")
                await self.say(f"⚠️ {type(e).__name__}: {e}")
                await self._set_busy(False)

    async def _run_turn(self, text: str) -> None:
        if not self.connected:
            # The CLI went away: pick the conversation up again where it was.
            async with self._lock:
                await self._disconnect()
                await self._connect(self.session_id)
        self._turn_done.clear()
        await self._set_busy(True)
        await self.client.query(text)
        await self._turn_done.wait()

    def _finish_turn(self) -> None:
        self._turn_done.set()
        if self.busy:
            self.busy = False
            asyncio.ensure_future(_call(self._on_busy, False))

    async def _set_busy(self, busy: bool) -> None:
        if self.busy != busy:
            self.busy = busy
            await _call(self._on_busy, busy)

    # -- inbound SDK messages ------------------------------------------------------

    async def _read(self, client: Any) -> None:
        try:
            async for msg in client.receive_messages():
                await self._handle_message(msg)
            await self.say("⚠️ Claude session ended")
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            log.exception("reader failed")
            await self.say(f"⚠️ Claude session failed: {e}")
        finally:
            if self._reader is asyncio.current_task():
                self.registry.cancel_all("Claude session ended")
                self._finish_turn()

    async def _handle_message(self, msg: Any) -> None:
        self._track(msg)
        for text in self.renderer.render(msg):
            await self.say(text)
        if isinstance(msg, ResultMessage):
            self._finish_turn()

    def _track(self, msg: Any) -> None:
        if isinstance(msg, ConversationResetMessage):
            self._fold_cost()
            return
        if isinstance(msg, SystemMessage) and msg.subtype == "init":
            d = msg.data
            self.session_id = d.get("session_id") or self.session_id
            self.model = d.get("model") or self.model
            self.mode = d.get("permissionMode") or self.mode
            if d.get("slash_commands"):
                self.slash_commands = list(d["slash_commands"])
            return
        if isinstance(msg, ResultMessage):
            self.session_id = msg.session_id or self.session_id
            if msg.total_cost_usd is not None:
                self._cost_current = msg.total_cost_usd

    def _fold_cost(self) -> None:
        self._cost_base += self._cost_current
        self._cost_current = 0.0

    # -- settings --------------------------------------------------------------------

    async def set_mode(self, mode: str) -> None:
        if mode == "plan" and self.mode != "plan":
            self._mode_before_plan = self.mode or "default"
        if self.client is not None:
            await self.client.set_permission_mode(mode)
        self.mode = mode

    async def set_model(self, model: str | None) -> None:
        if self.client is not None:
            await self.client.set_model(model)
        self.model = model

    @property
    def mode_before_plan(self) -> str:
        return self._mode_before_plan

    def status(self) -> Status:
        return Status(
            session_id=self.session_id,
            model=self.model,
            mode=self.mode,
            busy=self.busy,
            queued=self._queue.qsize(),
            open_prompts=len(self.registry),
            session_cost_usd=self._cost_current,
            total_cost_usd=self._cost_base + self._cost_current,
            connected=self.connected,
        )

    async def say(self, text: str) -> None:
        try:
            await _call(self._output, text)
        except Exception:  # noqa: BLE001
            log.exception("output callback failed")

    # -- permissions -----------------------------------------------------------------

    async def _ask(self, item: Pending, prompt: str) -> Any:
        """Post `prompt` and wait for the reply (or the timeout)."""
        await self.say(prompt)
        timeout = self.config.ui.approval_timeout
        try:
            return await asyncio.wait_for(asyncio.shield(item.future), timeout)
        except TimeoutError:
            self.registry.discard(item.id)
            await self.say(f"⌛ {item.id} timed out")
            return Cancelled("no reply in time")

    async def _can_use_tool(
        self, tool_name: str, tool_input: dict[str, Any], ctx: ToolPermissionContext
    ) -> PermissionResultAllow | PermissionResultDeny:
        if tool_name == "AskUserQuestion":
            return await self._question(tool_input)
        if tool_name == "ExitPlanMode":
            return await self._plan(tool_input)
        return await self._permission(tool_name, tool_input, ctx)

    async def _question(self, tool_input: dict[str, Any]) -> PermissionResultAllow | PermissionResultDeny:
        questions = list(tool_input.get("questions") or [])
        item = self.registry.open("question", {"questions": questions})
        result = await self._ask(item, format_question(item.id, questions))
        if isinstance(result, Cancelled):
            return _cancelled(result)
        return PermissionResultAllow(updated_input={**tool_input, "questions": questions, "answers": result})

    async def _plan(self, tool_input: dict[str, Any]) -> PermissionResultAllow | PermissionResultDeny:
        plan = str(tool_input.get("plan") or "")
        item = self.registry.open("plan", {"plan": plan})
        result = await self._ask(item, format_plan(item.id, plan))
        if isinstance(result, Cancelled):
            return _cancelled(result)
        assert isinstance(result, Reply)
        if result.decision == "yes":
            # Leave plan mode, back to the mode in use before `/plan`.
            back = self._mode_before_plan
            self.mode = back
            return PermissionResultAllow(
                updated_input=tool_input,
                updated_permissions=[PermissionUpdate(type="setMode", mode=back, destination="session")],  # type: ignore[arg-type]
            )
        feedback = result.text or "The user rejected the plan."
        return PermissionResultDeny(message=f"Plan not approved. User feedback: {feedback}")

    async def _permission(
        self, tool_name: str, tool_input: dict[str, Any], ctx: ToolPermissionContext
    ) -> PermissionResultAllow | PermissionResultDeny:
        suggestions = list(ctx.suggestions or [])
        always = describe_suggestions(suggestions) if suggestions else None
        item = self.registry.open(
            "permission", {"tool": tool_name, "input": tool_input, "suggestions": suggestions}
        )
        prompt = format_permission(
            item.id,
            tool_name,
            tool_input,
            title=ctx.title,
            description=ctx.description,
            reason=ctx.decision_reason,
            blocked_path=ctx.blocked_path,
            always=always or None,
            cwd=self.config.claude.cwd,
        )
        result = await self._ask(item, prompt)
        if isinstance(result, Cancelled):
            return _cancelled(result)
        assert isinstance(result, Reply)
        if result.decision == "yes":
            return PermissionResultAllow(updated_input=tool_input)
        if result.decision == "always":
            if not suggestions:
                await self.say(f"{item.id}: nothing to remember for this tool, allowed once")
            return PermissionResultAllow(
                updated_input=tool_input, updated_permissions=suggestions or None
            )
        reason = result.text or "The user denied this tool call."
        return PermissionResultDeny(message=reason)


def _cancelled(c: Cancelled) -> PermissionResultDeny:
    # A stop or restart also ends the turn; a timeout only denies this call.
    return PermissionResultDeny(message=f"Not approved: {c.reason}.", interrupt=c.reason != "no reply in time")

