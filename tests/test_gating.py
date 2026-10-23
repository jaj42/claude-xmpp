"""Bot tests without a server: message gating, routing order, output and rejoin."""

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest
from slixmpp import JID

from claude_xmpp_agent.bot import Bot
from claude_xmpp_agent.config import AccessConfig, ClaudeConfig, Config, UiConfig, XmppConfig

ROOM = "room@muc.x"


def make_config(tmp_path: Path, **ui) -> Config:
    return Config(
        xmpp=XmppConfig(jid="bot@x", password="p", room=ROOM, nick="claude"),
        access=AccessConfig(allowed_jids=frozenset({"me@x"})),
        claude=ClaudeConfig(cwd=tmp_path),
        ui=UiConfig(**ui),
    )


class FakeRouter:
    def __init__(self):
        self.handled: list[str] = []

    async def handle(self, text):
        self.handled.append(text)


@pytest.fixture
async def bot(tmp_path):
    b = Bot(make_config(tmp_path), router=FakeRouter())
    b.sent = []
    b.send = lambda stanza, use_filters=True: b.sent.append(stanza)  # XMLStream.send
    b.muc.rooms[None][b.room] = {
        "al": {"nick": "al", "jid": JID("Me@x/phone")},
        "eve": {"nick": "eve", "jid": JID("eve@x/laptop")},
        "anon": {"nick": "anon", "jid": JID("")},
    }
    b.joined = True
    yield b
    if b._inbox_task is not None:
        b._inbox_task.cancel()


def msg(bot, nick, body, *, mtype="groupchat", room=ROOM, delay=False):
    m = bot.make_message(JID("bot@x"), mbody=body, mtype=mtype)
    m["from"] = f"{room}/{nick}" if nick else room
    if delay:
        m["delay"]["stamp"] = datetime(2026, 1, 1, tzinfo=UTC)
        m["delay"]["from"] = room
    return m


def bodies(bot):
    return [s["body"] for s in bot.sent if s["body"]]


def states(bot):
    return [s["chat_state"] for s in bot.sent if not s["body"]]


# -- gate ---------------------------------------------------------------------


async def test_allowed_user_passes(bot):
    assert bot.gate(msg(bot, "al", "hello")) == "hello"


async def test_unknown_jid_dropped_and_warned_once(bot):
    assert bot.gate(msg(bot, "eve", "hi")) is None
    assert bot.gate(msg(bot, "eve", "hi again")) is None
    assert bodies(bot) == ["🚫 ignoring eve: eve@x isn't allowed"]


async def test_missing_jid_dropped(bot):
    assert bot.gate(msg(bot, "anon", "hi")) is None
    assert bot.gate(msg(bot, "ghost", "not in roster")) is None
    assert bodies(bot) == [
        "🚫 ignoring anon: I can't see their real JID",
        "🚫 ignoring ghost: I can't see their real JID",
    ]


async def test_history_dropped(bot):
    assert bot.gate(msg(bot, "al", "old", delay=True)) is None
    assert bot.sent == []


async def test_own_nick_dropped(bot):
    assert bot.gate(msg(bot, "claude", "echo")) is None
    bot.nick = "claude_"
    assert bot.gate(msg(bot, "claude_", "echo")) is None
    assert bot.gate(msg(bot, "claude", "someone took my old nick")) is None  # not in roster
    assert bot.sent[-1]["body"].startswith("🚫 ignoring claude")


async def test_other_drops(bot):
    assert bot.gate(msg(bot, "al", "pm", mtype="chat")) is None
    assert bot.gate(msg(bot, "al", "elsewhere", room="other@muc.x")) is None
    assert bot.gate(msg(bot, None, "room notice")) is None
    assert bot.gate(msg(bot, "al", "   ")) is None
    assert bot.sent == []


# -- routing --------------------------------------------------------------------


async def test_messages_routed_in_order(bot):
    bot._inbox_task = asyncio.create_task(bot._drain_inbox())
    for nick, text in [("al", "one"), ("eve", "nope"), ("al", "two"), ("al", "/stop")]:
        bot._on_muc_message(msg(bot, nick, text))
    for _ in range(5):
        await asyncio.sleep(0)
    assert bot.router.handled == ["one", "two", "/stop"]


async def test_router_error_is_posted(bot):
    class Boom:
        async def handle(self, text):
            raise RuntimeError("bad")

    bot.router = Boom()
    bot._inbox_task = asyncio.create_task(bot._drain_inbox())
    bot._on_muc_message(msg(bot, "al", "x"))
    for _ in range(5):
        await asyncio.sleep(0)
    assert bodies(bot) == ["⚠️ RuntimeError: bad"]


# -- output -----------------------------------------------------------------------


async def test_output_is_chunked_groupchat(tmp_path, bot):
    bot.config = make_config(tmp_path, max_chunk=200)
    text = "\n".join(f"line {i:03d} " + "x" * 40 for i in range(20))
    await bot.output(text)
    assert len(bot.sent) > 1
    assert all(s["type"] == "groupchat" and s["to"] == JID(ROOM) for s in bot.sent)
    assert all(len(b) <= 200 for b in bodies(bot))
    assert "\n".join(bodies(bot)) == text


async def test_outbox_while_not_joined(bot):
    bot.joined = False
    await bot.output("first")
    await bot.output("second")
    assert bot.sent == []
    bot._joined(_self_presence(bot, codes={110, 100}), [])
    assert bodies(bot) == ["first", "second"]


async def test_chat_states(bot):
    await bot.set_busy(True)
    await bot.output("working on it")
    await bot.set_busy(False)
    assert [(s["body"], s["chat_state"]) for s in bot.sent] == [
        ("", "composing"),
        ("working on it", ""),
        ("", "composing"),
        ("", "active"),
    ]


async def test_no_chat_state_when_not_joined(bot):
    bot.joined = False
    await bot.set_busy(True)
    assert bot.sent == []
    assert bot.busy


# -- join / rejoin -------------------------------------------------------------------


def _self_presence(bot, codes, ptype=None, jid=""):
    p = bot.make_presence(pto=JID("bot@x/res"), pfrom=JID(f"{ROOM}/claude"))
    if ptype:
        p["type"] = ptype
    p["muc"]["status_codes"] = codes
    if jid:
        p["muc"]["jid"] = jid
    return p


async def test_warns_when_room_is_anonymous(bot):
    bot.joined = False
    bot._joined(_self_presence(bot, codes={110}), [])
    assert bodies(bot)[0].startswith("⚠️ I can't see real JIDs")


async def test_no_warning_when_jids_visible(bot):
    bot.joined = False
    bot._joined(_self_presence(bot, codes={110}, jid="bot@x/res"), [])
    bot._joined(_self_presence(bot, codes={110, 100}), [])
    assert bot.sent == []


async def test_rejoin_after_kick(bot, monkeypatch):
    scheduled = []
    monkeypatch.setattr(bot, "_schedule_join", lambda: scheduled.append(1))
    bot._on_self_presence(_self_presence(bot, codes={110, 307}, ptype="unavailable"))
    assert not bot.joined
    assert scheduled == [1]


async def test_no_rejoin_when_banned(bot, monkeypatch):
    scheduled = []
    monkeypatch.setattr(bot, "_schedule_join", lambda: scheduled.append(1))
    bot._on_self_presence(_self_presence(bot, codes={110, 301}, ptype="unavailable"))
    assert scheduled == []
    assert bot.failed and "banned" in bot.failed


async def test_nick_change_followed(bot):
    p = _self_presence(bot, codes={110, 303}, ptype="unavailable")
    p["muc"]["item_nick"] = "claude2"
    bot._on_self_presence(p)
    assert bot.nick == "claude2"
    assert bot.joined


async def test_reconnect_skipped_when_already_connecting(bot, monkeypatch):
    calls = []
    monkeypatch.setattr(bot, "connect", lambda *a, **k: calls.append(1))
    bot._current_connection_attempt = asyncio.get_running_loop().create_future()
    bot._reconnect()
    assert calls == []
    bot._current_connection_attempt.cancel()
    bot._reconnect()
    assert calls == [1]
    bot._closing = True
    bot._reconnect()
    assert calls == [1]
