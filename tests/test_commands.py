"""Router and harness command tests against a fake session."""

from dataclasses import replace
from pathlib import Path

import pytest

from claude_xmpp_agent.approvals import Registry, Reply
from claude_xmpp_agent.commands import COMMANDS, Router
from claude_xmpp_agent.config import AccessConfig, ClaudeConfig, Config, UiConfig, XmppConfig
from claude_xmpp_agent.session import Status


def make_config(tmp_path: Path, **claude) -> Config:
    return Config(
        xmpp=XmppConfig(jid="bot@x", password="p", room="r@muc.x"),
        access=AccessConfig(allowed_jids=frozenset({"me@x"})),
        claude=ClaudeConfig(cwd=tmp_path, **claude),
        ui=UiConfig(),
    )


class FakeSession:
    def __init__(self):
        self.registry = Registry()
        self.calls: list[tuple] = []
        self.said: list[str] = []
        self.mode = "default"
        self.model = None
        self.session_id = "sess-1"
        self.slash_commands = ["clear", "compact", "model", "review"]
        self.mode_before_plan = "default"
        self.interrupt_result = True
        self.busy = False

    async def say(self, text):
        self.said.append(text)

    async def submit(self, text):
        self.calls.append(("submit", text))

    async def interrupt(self):
        self.calls.append(("interrupt",))
        return self.interrupt_result

    async def set_mode(self, mode):
        self.calls.append(("set_mode", mode))
        if mode == "plan" and self.mode != "plan":
            self.mode_before_plan = self.mode
        self.mode = mode

    async def set_model(self, model):
        self.calls.append(("set_model", model))
        self.model = model

    async def restart(self, resume=None):
        self.calls.append(("restart", resume))

    def status(self):
        return Status(
            session_id=self.session_id, model=self.model, mode=self.mode, busy=self.busy,
            queued=2, open_prompts=len(self.registry), session_cost_usd=0.0123,
            total_cost_usd=1.5, connected=True,
        )


@pytest.fixture
def session():
    return FakeSession()


@pytest.fixture
def router(session, tmp_path):
    return Router(session, make_config(tmp_path))


# -- routing -------------------------------------------------------------------


async def test_plain_text_goes_to_claude(router, session):
    await router.handle("what's in this directory?")
    assert session.calls == [("submit", "what's in this directory?")]


async def test_unknown_slash_command_goes_to_claude(router, session):
    for text in ("/clear", "/compact keep the API notes", "/review", "/etc/hosts?"):
        await router.handle(text)
    assert [c[1] for c in session.calls] == [
        "/clear", "/compact keep the API notes", "/review", "/etc/hosts?"
    ]


async def test_blank_and_bare_slash(router, session):
    await router.handle("   ")
    await router.handle("/")
    assert session.calls == [("submit", "/")]


async def test_approval_reply_is_consumed(router, session):
    item = session.registry.open("permission", {"tool": "Bash"})
    await router.handle("y")
    assert item.future.result() == Reply("yes", item.id, None)
    assert session.calls == []
    assert session.said == []


async def test_approval_error_message_is_posted(router, session):
    session.registry.open("permission")
    session.registry.open("permission")
    await router.handle("y")
    assert "several prompts are open" in session.said[0]
    assert session.calls == []


async def test_command_works_while_prompt_open(router, session):
    session.registry.open("question", {"questions": [{"question": "Q?", "options": []}]})
    await router.handle("/stop")
    assert session.calls == [("interrupt",)]


async def test_text_without_open_prompt_goes_to_claude(router, session):
    await router.handle("y")
    assert session.calls == [("submit", "y")]


async def test_command_name_case_insensitive(router, session):
    await router.handle("/STOP")
    assert session.calls == [("interrupt",)]


async def test_command_failure_is_reported(router, session):
    async def boom(model):
        raise RuntimeError("no such model")

    session.set_model = boom
    await router.handle("/model nope")
    assert session.said == ["⚠️ /model failed: no such model"]


# -- commands --------------------------------------------------------------------


async def test_help_lists_commands_and_reply_syntax(router, session):
    await router.handle("/help")
    text = session.said[0]
    for name in COMMANDS:
        assert f"/{name}" in text
    assert "`y [id]`" in text


async def test_status(router, session):
    session.busy = True
    session.registry.open("permission")
    await router.handle("/status")
    text = session.said[0]
    assert "working · 2 queued · 1 open prompt" in text
    assert "`sess-1`" in text
    assert "mode: default" in text
    assert "$0.0123 this session · $1.50 total" in text


async def test_stop(router, session):
    await router.handle("/stop")
    assert session.said == []
    session.interrupt_result = False
    await router.handle("/stop")
    assert session.said == ["nothing to stop"]


async def test_plan_on_and_off(router, session):
    session.mode = "acceptEdits"
    await router.handle("/plan")
    assert session.mode == "plan"
    await router.handle("/plan")
    assert session.said[-1] == "already in plan mode"
    await router.handle("/plan off")
    assert session.calls == [("set_mode", "plan"), ("set_mode", "acceptEdits")]
    await router.handle("/plan off")
    assert session.said[-1] == "not in plan mode"
    await router.handle("/plan maybe")
    assert session.said[-1] == "usage: /plan [off]"


async def test_mode(router, session):
    await router.handle("/mode")
    assert session.said[-1].startswith("mode: default\nmodes: default, acceptEdits")
    await router.handle("/mode acceptedits")
    assert session.calls == [("set_mode", "acceptEdits")]
    await router.handle("/mode yolo")
    assert "unknown mode 'yolo'" in session.said[-1]
    assert len(session.calls) == 1


async def test_mode_bypass_refused_unless_allowed(session, tmp_path):
    router = Router(session, make_config(tmp_path))
    await router.handle("/mode bypassPermissions")
    assert session.calls == []
    assert "disabled" in session.said[-1]

    router = Router(session, make_config(tmp_path, allow_bypass=True))
    await router.handle("/mode bypassPermissions")
    assert session.calls == [("set_mode", "bypassPermissions")]


async def test_model(router, session):
    await router.handle("/model")
    assert session.said[-1] == "model: default"
    await router.handle("/model sonnet")
    assert session.calls == [("set_model", "sonnet")]
    await router.handle("/model default")
    assert session.calls[-1] == ("set_model", None)


async def test_new(router, session):
    await router.handle("/new")
    assert session.calls == [("restart", None)]


async def test_resume(router, session):
    await router.handle("/resume")
    assert session.calls == []
    assert "current session: `sess-1`" in session.said[-1]
    await router.handle("/resume abc-123")
    assert session.calls == [("restart", "abc-123")]


async def test_commands_lists_both(router, session):
    await router.handle("/commands")
    text = session.said[0]
    assert "/stop" in text.splitlines()[0]
    # Harness commands are not repeated in Claude's list.
    assert text.splitlines()[1] == "*Claude:* /clear /compact /review"


async def test_commands_before_init(router, session):
    session.slash_commands = []
    await router.handle("/commands")
    assert "not known yet" in session.said[0]


async def test_router_with_real_session_registry(tmp_path):
    # The router shares the session's registry by default.
    s = FakeSession()
    r = Router(s, make_config(tmp_path))
    assert r.registry is s.registry
    other = Registry()
    assert Router(s, replace(make_config(tmp_path)), other).registry is other
