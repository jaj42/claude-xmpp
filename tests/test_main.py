"""Entry point tests: argument parsing, config errors, run/shutdown with fakes."""

import asyncio
from pathlib import Path

import pytest

from claude_xmpp_agent import __main__ as cli
from claude_xmpp_agent.config import AccessConfig, ClaudeConfig, Config, UiConfig, XmppConfig


def make_config(tmp_path: Path) -> Config:
    return Config(
        xmpp=XmppConfig(jid="bot@x", password="p", room="r@muc.x"),
        access=AccessConfig(allowed_jids=frozenset({"me@x"})),
        claude=ClaudeConfig(cwd=tmp_path),
        ui=UiConfig(),
    )


class FakeBot:
    def __init__(self):
        self.failed = None
        self.calls = []

    async def start(self):
        self.calls.append("start")

    async def stop(self):
        self.calls.append("stop")


class FakeSession:
    def __init__(self, fail=False):
        self.fail = fail
        self.calls = []

    async def start(self, resume=None):
        self.calls.append(("start", resume))
        if self.fail:
            raise RuntimeError("no auth")

    async def close(self):
        self.calls.append(("close",))


def test_parse_args_defaults():
    a = cli.parse_args([])
    assert a.config == "config.toml" and a.resume is None and not a.debug


def test_parse_args_all():
    a = cli.parse_args(["-c", "x.toml", "--resume", "abc", "--debug"])
    assert (a.config, a.resume, a.debug) == ("x.toml", "abc", True)


def test_help_exits_zero(capsys):
    with pytest.raises(SystemExit) as e:
        cli.parse_args(["--help"])
    assert e.value.code == 0
    assert "--config" in capsys.readouterr().out


def test_missing_config_exits_2(tmp_path, capsys):
    with pytest.raises(SystemExit) as e:
        cli.main(["--config", str(tmp_path / "nope.toml")])
    assert e.value.code == cli.EXIT_CONFIG
    assert "not found" in capsys.readouterr().err


async def test_run_until_stop(tmp_path):
    bot, session = FakeBot(), FakeSession()
    stop = asyncio.Event()
    task = asyncio.create_task(
        cli.run(make_config(tmp_path), resume="s1", stop=stop, make_parts=lambda c: (bot, session))
    )
    await asyncio.sleep(0.05)
    assert bot.calls == ["start"] and session.calls == [("start", "s1")]
    stop.set()
    assert await asyncio.wait_for(task, 2) == cli.EXIT_OK
    assert session.calls[-1] == ("close",) and bot.calls[-1] == "stop"


async def test_run_exits_when_bot_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "FAILED_POLL", 0.01)
    bot, session = FakeBot(), FakeSession()
    task = asyncio.create_task(cli.run(make_config(tmp_path), make_parts=lambda c: (bot, session)))
    await asyncio.sleep(0.05)
    bot.failed = "banned"
    assert await asyncio.wait_for(task, 2) == cli.EXIT_FATAL
    assert ("close",) in session.calls and "stop" in bot.calls


async def test_run_claude_start_failure(tmp_path):
    bot, session = FakeBot(), FakeSession(fail=True)
    code = await cli.run(make_config(tmp_path), make_parts=lambda c: (bot, session))
    assert code == cli.EXIT_FATAL
    assert "start" not in bot.calls  # never connected to XMPP
    assert ("close",) in session.calls


async def test_default_parts_wiring(tmp_path):
    bot, session = cli._default_parts(make_config(tmp_path))
    assert bot.router.session is session
    assert session._output == bot.output and session._on_busy == bot.set_busy
