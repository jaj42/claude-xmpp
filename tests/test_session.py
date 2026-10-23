"""Session tests against a fake SDK client (no Claude CLI needed)."""

import asyncio
from pathlib import Path

import pytest
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ConversationResetMessage,
    PermissionResultAllow,
    PermissionResultDeny,
    PermissionUpdate,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ToolPermissionContext,
)
from claude_agent_sdk.types import PermissionRuleValue

from claude_xmpp_agent.approvals import Registry
from claude_xmpp_agent.config import AccessConfig, ClaudeConfig, Config, UiConfig, XmppConfig
from claude_xmpp_agent.session import Session


def make_config(tmp_path: Path, **ui) -> Config:
    return Config(
        xmpp=XmppConfig(jid="bot@x", password="p", room="r@muc.x"),
        access=AccessConfig(allowed_jids=frozenset({"me@x"})),
        claude=ClaudeConfig(cwd=tmp_path),
        ui=UiConfig(**ui),
    )


def result(**kw):
    base = dict(
        subtype="success", duration_ms=1000, duration_api_ms=1, is_error=False,
        num_turns=1, session_id="sess-1", total_cost_usd=0.01,
    )
    base.update(kw)
    return ResultMessage(**base)


def init(**data):
    return SystemMessage(
        subtype="init",
        data={"session_id": "sess-1", "model": "opus", "permissionMode": "default",
              "slash_commands": ["clear", "compact"], **data},
    )


class FakeClient:
    """Replies to each query with a scripted list of messages."""

    instances: list["FakeClient"] = []

    def __init__(self, options: ClaudeAgentOptions):
        self.options = options
        self.inbox: asyncio.Queue = asyncio.Queue()
        self.queries: list[str] = []
        self.calls: list[tuple] = []
        self.script: list = []
        self.hold = False
        FakeClient.instances.append(self)

    async def connect(self):
        self.calls.append(("connect", self.options.resume))

    async def get_server_info(self):
        return {"commands": [{"name": "clear"}, {"name": "review"}]}

    async def query(self, text):
        self.queries.append(text)
        if not self.hold:
            for m in self.script or [init(), AssistantMessage([TextBlock(f"echo {text}")], "m"), result()]:
                await self.inbox.put(m)

    async def receive_messages(self):
        while True:
            m = await self.inbox.get()
            if m is None:
                return
            yield m

    async def interrupt(self):
        self.calls.append(("interrupt",))
        await self.inbox.put(result(terminal_reason="aborted_streaming"))

    async def set_permission_mode(self, mode):
        self.calls.append(("mode", mode))

    async def set_model(self, model):
        self.calls.append(("model", model))

    async def disconnect(self):
        self.calls.append(("disconnect",))


@pytest.fixture
def out():
    return []


@pytest.fixture
async def session(tmp_path, out):
    FakeClient.instances = []
    s = Session(make_config(tmp_path), out.append, client_factory=FakeClient)
    await s.start()
    yield s
    await s.close()


async def settle(session, timeout=2.0):
    """Wait until the worker is idle and the queue is empty."""
    async def idle():
        while session.busy or session._queue.qsize():
            await asyncio.sleep(0.01)
    await asyncio.wait_for(idle(), timeout)
    await asyncio.sleep(0.01)


def ctx(**kw):
    return ToolPermissionContext(tool_use_id="t1", **kw)


# -- options ---------------------------------------------------------------------


def test_build_options(tmp_path):
    s = Session(make_config(tmp_path), print)
    o = s.build_options(resume="abc")
    assert o.cwd == str(tmp_path)
    assert o.resume == "abc"
    assert o.permission_mode == "default"
    assert o.system_prompt["preset"] == "claude_code"
    assert "XMPP" in o.system_prompt["append"]
    assert o.can_use_tool is not None
    assert "PreToolUse" in o.hooks


async def test_keep_stream_open_hook_returns_no_decision(tmp_path):
    s = Session(make_config(tmp_path), print)
    hook = s.build_options().hooks["PreToolUse"][0].hooks[0]
    out = await hook({}, "t1", None)
    assert "hookSpecificOutput" not in out and "decision" not in out


# -- turns -----------------------------------------------------------------------


async def test_turn_renders_and_tracks(session, out):
    await session.submit("hi")
    await settle(session)
    assert FakeClient.instances[0].queries == ["hi"]
    assert "echo hi" in out
    assert any(o.startswith("✓ done") for o in out)
    st = session.status()
    assert st.session_id == "sess-1" and st.model == "opus" and not st.busy
    assert st.session_cost_usd == pytest.approx(0.01)
    assert session.slash_commands == ["clear", "compact"]


async def test_server_info_commands_before_first_turn(session):
    assert session.slash_commands == ["clear", "review"]


async def test_queued_message(session, out):
    client = FakeClient.instances[0]
    client.hold = True
    await session.submit("one")
    await asyncio.sleep(0.02)
    assert session.busy
    await session.submit("two")
    assert "⏳ queued (1 ahead)" in out
    client.hold = False
    await client.inbox.put(result())
    await settle(session)
    assert client.queries == ["one", "two"]


async def test_interrupt(session, out):
    client = FakeClient.instances[0]
    client.hold = True
    await session.submit("long")
    await asyncio.sleep(0.02)
    await session.submit("later")
    assert await session.interrupt() is True
    await settle(session)
    assert ("interrupt",) in client.calls
    assert client.queries == ["long"]  # "later" was dropped
    assert any(o.startswith("⏹ stopped") for o in out)


async def test_interrupt_when_idle(session):
    assert await session.interrupt() is False
    assert ("interrupt",) not in FakeClient.instances[0].calls


async def test_cost_survives_clear(session):
    client = FakeClient.instances[0]
    client.script = [result(total_cost_usd=0.5)]
    await session.submit("a")
    await settle(session)
    client.script = [ConversationResetMessage(new_conversation_id="n", uuid="u", session_id="sess-1"),
                     result(total_cost_usd=0.1)]
    await session.submit("/clear")
    await settle(session)
    st = session.status()
    assert st.session_cost_usd == pytest.approx(0.1)
    assert st.total_cost_usd == pytest.approx(0.6)


async def test_restart_and_resume(session):
    await session.restart(resume="old-id")
    assert len(FakeClient.instances) == 2
    assert ("disconnect",) in FakeClient.instances[0].calls
    assert FakeClient.instances[1].calls[0] == ("connect", "old-id")
    assert session.session_id == "old-id"
    await session.submit("x")
    await settle(session)
    assert FakeClient.instances[1].queries == ["x"]


async def test_reconnects_after_cli_exit(session, out):
    first = FakeClient.instances[0]
    await session.submit("a")
    await settle(session)
    await first.inbox.put(None)  # CLI stream ends
    await asyncio.sleep(0.02)
    assert "⚠️ Claude session ended" in out
    await session.submit("b")
    await settle(session)
    assert FakeClient.instances[1].calls[0] == ("connect", "sess-1")
    assert FakeClient.instances[1].queries == ["b"]


async def test_mode_and_model(session):
    await session.set_mode("plan")
    await session.set_model("sonnet")
    assert FakeClient.instances[0].calls[-2:] == [("mode", "plan"), ("model", "sonnet")]
    assert session.mode == "plan" and session.model == "sonnet"
    assert session.mode_before_plan == "default"


# -- can_use_tool ------------------------------------------------------------------


async def ask(session, *args):
    task = asyncio.create_task(session._can_use_tool(*args))
    await asyncio.sleep(0.01)
    return task


def only_id(session):
    (item,) = session.registry.items()
    return item.id


async def test_permission_yes(session, out):
    task = await ask(session, "Bash", {"command": "ls"}, ctx())
    id_ = only_id(session)
    assert any("Permission" in o and id_ in o for o in out)
    assert session.registry.handle(f"y {id_}").ok
    res = await task
    assert isinstance(res, PermissionResultAllow) and res.updated_input == {"command": "ls"}


async def test_permission_no_with_reason(session):
    task = await ask(session, "Bash", {"command": "rm -rf x"}, ctx())
    session.registry.handle("n use git clean instead")
    res = await task
    assert isinstance(res, PermissionResultDeny)
    assert res.message == "use git clean instead" and not res.interrupt


async def test_permission_always_uses_suggestions(session, out):
    sugg = [PermissionUpdate(type="addRules", rules=[PermissionRuleValue("Bash", "npm test:*")],
                             behavior="allow", destination="localSettings")]
    task = await ask(session, "Bash", {"command": "npm test"}, ctx(suggestions=sugg))
    assert any("Bash(npm test:*)" in o for o in out)
    session.registry.handle("a")
    res = await task
    assert res.updated_permissions == sugg


async def test_question(session, out):
    qs = [{"question": "Which?", "header": "Pick", "multiSelect": False,
           "options": [{"label": "A"}, {"label": "B"}]}]
    task = await ask(session, "AskUserQuestion", {"questions": qs}, ctx())
    assert any("Question" in o for o in out)
    session.registry.handle("2")
    res = await task
    assert res.updated_input == {"questions": qs, "answers": {"Which?": "B"}}


async def test_plan_approve_restores_mode(session):
    await session.set_mode("acceptEdits")
    await session.set_mode("plan")
    task = await ask(session, "ExitPlanMode", {"plan": "1. do it"}, ctx())
    session.registry.handle("y")
    res = await task
    assert isinstance(res, PermissionResultAllow)
    (upd,) = res.updated_permissions
    assert upd.type == "setMode" and upd.mode == "acceptEdits"
    assert session.mode == "acceptEdits"


async def test_plan_feedback_denies(session):
    task = await ask(session, "ExitPlanMode", {"plan": "1. do it"}, ctx())
    session.registry.handle("n add tests first")
    res = await task
    assert isinstance(res, PermissionResultDeny) and "add tests first" in res.message


async def test_interrupt_cancels_open_prompt(session):
    task = await ask(session, "Bash", {"command": "ls"}, ctx())
    await session.interrupt()
    res = await task
    assert isinstance(res, PermissionResultDeny) and res.interrupt
    assert len(session.registry) == 0


async def test_approval_timeout(tmp_path, out):
    FakeClient.instances = []
    s = Session(make_config(tmp_path, approval_timeout=0.05), out.append, registry=Registry(),
                client_factory=FakeClient)
    await s.start()
    try:
        res = await s._can_use_tool("Bash", {"command": "ls"}, ctx())
        assert isinstance(res, PermissionResultDeny) and not res.interrupt
        assert any("timed out" in o for o in out)
        assert len(s.registry) == 0
    finally:
        await s.close()


def test_describe_auth():
    from claude_xmpp_agent.session import describe_auth
    assert describe_auth(None, None) is None
    assert describe_auth(None, "Claude Max") == "Claude Max account (confirmed on the first turn)"
    assert describe_auth("none", "Claude Max") == "subscription (Claude Max)"
    assert describe_auth("none", None) == "no API key (subscription login or token)"
    assert describe_auth("ANTHROPIC_API_KEY", "Claude Max") == "API key (ANTHROPIC_API_KEY)"


async def test_status_auth_from_account_then_init(tmp_path, out):
    class AccountClient(FakeClient):
        async def get_server_info(self):
            return {"commands": [], "account": {"subscriptionType": "Claude Pro"}}

    s = Session(make_config(tmp_path), out.append, client_factory=AccountClient)
    await s.start()
    try:
        assert s.status().auth == "Claude Pro account (confirmed on the first turn)"
        AccountClient.instances[-1].script = [
            init(apiKeySource="none"), AssistantMessage([TextBlock("hi")], "m"), result(),
        ]
        await s.submit("hello")
        await settle(s)
        assert s.status().auth == "subscription (Claude Pro)"
        AccountClient.instances[-1].script = [
            init(apiKeySource="ANTHROPIC_API_KEY"), result(),
        ]
        await s.submit("again")
        await settle(s)
        assert s.status().auth == "API key (ANTHROPIC_API_KEY)"
    finally:
        await s.close()
