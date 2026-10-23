"""XMPP side: joins the MUC, gates messages by real JID, posts Claude's output.

Wiring (done in `__main__`):
  bot = Bot(config)
  session = Session(config, output=bot.output, on_busy=bot.set_busy)
  bot.router = Router(session, config)

Inbound: `muc::<room>::message` -> `gate()` (own nick, history, real-JID
allowlist) -> inbox queue -> one task calls `router.handle()` in order.
Outbound: `output(text)` -> `render.chunk()` -> groupchat messages. Messages
produced while we're not in the room wait in a bounded outbox.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections import deque
from typing import Any

from slixmpp import JID, ClientXMPP
from slixmpp.exceptions import PresenceError
from slixmpp.stanza import Message, Presence

from .config import Config
from .render import chunk

log = logging.getLogger(__name__)

# XEP-0045 status codes
NON_ANONYMOUS = 100
SELF_PRESENCE = 110
BANNED = 301
NICK_CHANGED = 303

OUTBOX_LIMIT = 200
REJOIN_DELAYS = (2, 5, 15, 30, 60, 120, 300)
RECONNECT_DELAY = 5


class Bot(ClientXMPP):
    def __init__(self, config: Config, router: Any = None) -> None:
        super().__init__(config.xmpp.jid, config.xmpp.password)
        self.config = config
        self.room = JID(config.xmpp.room)
        self.router = router  # commands.Router; set before start()

        self.joined = False
        self.nick = config.xmpp.nick  # nick in the room (the server may change it)
        self.busy = False
        self.failed: str | None = None  # set on fatal errors (bad password, banned)

        self._closing = False
        self._warned_nicks: set[str] = set()
        self._outbox: deque[str] = deque(maxlen=OUTBOX_LIMIT)
        self._inbox: asyncio.Queue[str] = asyncio.Queue()
        self._inbox_task: asyncio.Task[None] | None = None
        self._join_task: asyncio.Task[None] | None = None
        self._leaving = False

        self.register_plugin("xep_0030")  # service discovery
        self.register_plugin("xep_0045")  # MUC
        self.register_plugin("xep_0085")  # chat states
        self.register_plugin("xep_0199", {"keepalive": True, "interval": 120, "timeout": 30})
        self.register_plugin("xep_0203")  # delayed delivery (history detection)

        self.add_event_handler("session_start", self._on_session_start)
        self.add_event_handler("disconnected", self._on_disconnected)
        self.add_event_handler("failed_auth", self._on_failed_auth)
        self.add_event_handler(f"muc::{self.room}::message", self._on_muc_message)
        self.add_event_handler(f"muc::{self.room}::self-presence", self._on_self_presence)

    @property
    def muc(self) -> Any:
        return self.plugin["xep_0045"]

    # -- lifecycle ---------------------------------------------------------------

    async def start(self) -> None:
        """Connect; joining happens on session_start. Reconnects on its own."""
        self._closing = False
        if self._inbox_task is None or self._inbox_task.done():
            self._inbox_task = asyncio.create_task(self._drain_inbox(), name="xmpp-inbox")
        self.connect()

    async def stop(self) -> None:
        """Leave the room and disconnect for good."""
        self._closing = True
        for task in (self._join_task, self._inbox_task):
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
        self._join_task = self._inbox_task = None
        if self.joined:
            self._leaving = True
            with contextlib.suppress(Exception):
                self.muc.leave_muc(self.room, self.nick, "bye")
            self.joined = False
        with contextlib.suppress(Exception):
            await self.disconnect(wait=2.0)

    def _on_session_start(self, _event: Any) -> None:
        self.send_presence()
        self._schedule_join()

    def _on_disconnected(self, reason: Any) -> None:
        self.joined = False
        if self._closing or self.failed:
            return
        log.warning("disconnected (%s), reconnecting in %ss", reason, RECONNECT_DELAY)
        self.loop.call_later(RECONNECT_DELAY, self._reconnect)

    def _reconnect(self) -> None:
        if self._closing or self.failed or self.is_connected():
            return
        attempt = self._current_connection_attempt
        if attempt is not None and not attempt.done():
            return  # slixmpp is already reconnecting (e.g. after a ping timeout)
        self.connect()

    def _on_failed_auth(self, _event: Any) -> None:
        self.failed = "authentication failed (check jid and password)"
        log.error(self.failed)
        asyncio.ensure_future(self.disconnect(wait=0))

    # -- joining -------------------------------------------------------------------

    def _schedule_join(self) -> None:
        if self._closing or self.failed:
            return
        if self._join_task is not None and not self._join_task.done():
            return
        self._join_task = asyncio.create_task(self._join_loop(), name="xmpp-join")

    async def _join_loop(self) -> None:
        nick = self.config.xmpp.nick
        attempt = 0
        while not self._closing and not self.failed:
            try:
                pres, _subject, occupants, _history = await self.muc.join_muc_wait(
                    self.room,
                    nick,
                    password=self.config.xmpp.room_password,
                    maxstanzas=0,
                    timeout=60,
                )
            except PresenceError as e:
                cond = e.condition
                if cond == "conflict":
                    nick += "_"
                    log.warning("nick in use, trying %s", nick)
                    continue
                log.error("cannot join %s: %s %s", self.room, cond, e.text or "")
                if cond in ("forbidden", "not-authorized", "registration-required"):
                    log.error(
                        "the bot account may need membership (members-only room) "
                        "or the room password"
                    )
            except (TimeoutError, asyncio.TimeoutError):
                log.warning("joining %s timed out", self.room)
            else:
                self._joined(pres, occupants)
                return
            delay = REJOIN_DELAYS[min(attempt, len(REJOIN_DELAYS) - 1)]
            attempt += 1
            await asyncio.sleep(delay)

    def _joined(self, pres: Presence, occupants: list[Presence]) -> None:
        self.joined = True
        self._leaving = False
        self.nick = pres["from"].resource or self.nick
        log.info("joined %s as %s", self.room, self.nick)
        if not self.real_jids_visible(pres, occupants):
            msg = (
                "⚠️ I can't see real JIDs in this room, so I'll ignore everyone. "
                "Make the room non-anonymous (members-only recommended)."
            )
            log.warning(msg)
            self._outbox.appendleft(msg)
        self._flush_outbox()
        self._still_busy()

    def real_jids_visible(self, pres: Presence, occupants: list[Presence]) -> bool:
        if NON_ANONYMOUS in pres["muc"]["status_codes"]:
            return True
        return any(str(p["muc"]["jid"]) for p in [pres, *occupants])

    def _on_self_presence(self, pres: Presence) -> None:
        if pres["type"] != "unavailable":
            return
        codes = pres["muc"]["status_codes"]
        if NICK_CHANGED in codes:
            self.nick = pres["muc"]["item_nick"] or self.nick
            return
        self.joined = False
        if self._leaving or self._closing:
            return
        if BANNED in codes:
            self.failed = f"banned from {self.room}"
            log.error(self.failed)
            return
        log.warning("left %s (status %s), rejoining", self.room, sorted(codes))
        self._schedule_join()

    # -- inbound -------------------------------------------------------------------

    def gate(self, msg: Message) -> str | None:
        """Return the text to route, or None when the message must be dropped."""
        if msg["type"] != "groupchat":
            return None
        body = msg["body"]
        if not body or not body.strip():
            return None
        sender = msg["from"]
        if sender.bare != self.room.bare:
            return None
        nick = sender.resource
        if not nick or nick == self.nick:
            return None  # room/server message, or our own echo
        if msg.get_plugin("delay", check=True) is not None:
            return None  # history replayed on join
        jid = self.muc.get_jid_property(self.room, nick, "jid")
        bare = JID(jid).bare if jid else ""
        if not bare:
            self._warn_once(nick, f"ignoring {nick}: I can't see their real JID")
            return None
        if bare not in self.config.access.allowed_jids:
            self._warn_once(nick, f"ignoring {nick}: {bare} isn't allowed")
            return None
        return body

    def _warn_once(self, nick: str, text: str) -> None:
        log.warning(text)
        if nick not in self._warned_nicks:
            self._warned_nicks.add(nick)
            self._post(f"🚫 {text}")

    def _on_muc_message(self, msg: Message) -> None:
        text = self.gate(msg)
        if text is not None:
            self._inbox.put_nowait(text)

    async def _drain_inbox(self) -> None:
        while True:
            text = await self._inbox.get()
            if self.router is None:
                log.error("no router set, dropping message")
                continue
            try:
                await self.router.handle(text)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                log.exception("routing failed")
                self._post(f"⚠️ {type(e).__name__}: {e}")

    # -- outbound ------------------------------------------------------------------

    async def output(self, text: str) -> None:
        """Output callback for the session: post `text`, chunked."""
        self._post(text)

    def _post(self, text: str) -> None:
        parts = chunk(text, self.config.ui.max_chunk)
        if not self.joined:
            self._outbox.extend(parts)
            return
        for part in parts:
            self._send_groupchat(part)
        self._still_busy()

    def _flush_outbox(self) -> None:
        while self._outbox and self.joined:
            self._send_groupchat(self._outbox.popleft())

    def _send_groupchat(self, body: str) -> None:
        self.make_message(self.room, mbody=body, mtype="groupchat").send()

    def _still_busy(self) -> None:
        # Clients clear the typing indicator when a message arrives.
        if self.busy:
            self._send_chat_state("composing")

    async def set_busy(self, busy: bool) -> None:
        """Session `on_busy` callback: chat state while Claude works."""
        self.busy = busy
        if self.joined:
            self._send_chat_state("composing" if busy else "active")

    def _send_chat_state(self, state: str) -> None:
        msg = self.make_message(self.room, mtype="groupchat")
        msg["chat_state"] = state
        msg.send()
