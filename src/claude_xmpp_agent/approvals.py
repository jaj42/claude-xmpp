"""Open approval items (permission, question, plan) and parsing of chat replies.

Pure logic: no XMPP or SDK dependency. The session opens an item and awaits
its future; the router passes each inbound message to `Registry.handle()`,
which resolves the matching item or returns None when the message is not a
reply to anything.
"""

from __future__ import annotations

import asyncio
import random
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

# 'l' is left out: it is easy to confuse with 'i' and '1' in chat fonts.
ID_ALPHABET = "abcdefghijkmnopqrstuvwxyz"
ID_LENGTH = 5

Kind = Literal["permission", "question", "plan"]
Decision = Literal["yes", "no", "always"]

_DECISIONS: dict[str, Decision] = {
    "y": "yes", "yes": "yes",
    "n": "no", "no": "no",
    "a": "always", "always": "always",
}

REPLY_RE = re.compile(
    r"^(y|yes|n|no|a|always)(?:\s+([a-km-z]{5}))?(?:\s+(.*))?$",
    re.IGNORECASE | re.DOTALL,
)
_ID_PREFIX_RE = re.compile(r"^([a-km-z]{5})(?:\s+(.*))?$", re.IGNORECASE | re.DOTALL)
_NUMBERS_RE = re.compile(r"^\d+(?:\s*[,\s]\s*\d+)*$")


@dataclass(frozen=True)
class Reply:
    """A parsed `y|n|a [id] [text]` reply."""

    decision: Decision
    id: str | None = None
    text: str | None = None


@dataclass(frozen=True)
class Cancelled:
    """Result set on every open item by `Registry.cancel_all()`."""

    reason: str


@dataclass
class Pending:
    id: str
    kind: Kind
    future: asyncio.Future[Any]
    payload: Any = None


@dataclass(frozen=True)
class Outcome:
    """What `Registry.handle()` did with a message.

    `resolved` is the item that was answered, or None when the message was
    meant as a reply but could not be applied; `message` then says why.
    """

    resolved: Pending | None = None
    message: str | None = None

    @property
    def ok(self) -> bool:
        return self.resolved is not None


def parse_reply(text: str) -> Reply | None:
    """Parse `y|yes|n|no|a|always [id] [text]`, case-insensitive."""
    m = REPLY_RE.match(text.strip())
    if not m:
        return None
    word, id_, rest = m.groups()
    rest = rest.strip() if rest else None
    return Reply(_DECISIONS[word.lower()], id_.lower() if id_ else None, rest or None)


def _option_labels(question: Mapping[str, Any]) -> list[str]:
    return [str(o.get("label", "")) for o in question.get("options") or []]


def parse_answer(question: Mapping[str, Any], line: str) -> str:
    """Turn one reply line into an answer for one AskUserQuestion question.

    Numbers (`2` or `1,3`) pick option labels; anything else is free text.
    Raises ValueError with a user-facing message when the numbers are invalid.
    """
    line = line.strip()
    if not line:
        raise ValueError("empty answer")
    labels = _option_labels(question)
    if labels and _NUMBERS_RE.match(line):
        nums = [int(n) for n in re.split(r"[,\s]+", line)]
        bad = [n for n in nums if not 1 <= n <= len(labels)]
        if bad:
            raise ValueError(f"no option {bad[0]} (choose 1-{len(labels)})")
        if len(nums) > 1 and not question.get("multiSelect"):
            raise ValueError("pick only one option")
        picked = list(dict.fromkeys(labels[n - 1] for n in nums))
        return ", ".join(picked)
    return line


def parse_answers(questions: Sequence[Mapping[str, Any]], text: str) -> dict[str, str]:
    """Answer every question, one non-empty line per question.

    Returns the SDK `answers` mapping: question text -> answer.
    """
    lines = [ln for ln in text.strip().splitlines() if ln.strip()]
    if len(lines) != len(questions):
        if len(questions) == 1:
            lines = [" ".join(ln.strip() for ln in lines)]
        else:
            raise ValueError(
                f"expected {len(questions)} lines, one per question; got {len(lines)}"
            )
    answers: dict[str, str] = {}
    for i, (q, ln) in enumerate(zip(questions, lines), 1):
        try:
            answers[str(q.get("question", ""))] = parse_answer(q, ln)
        except ValueError as e:
            prefix = f"question {i}: " if len(questions) > 1 else ""
            raise ValueError(prefix + str(e)) from None
    return answers


@dataclass
class Registry:
    rng: random.Random = field(default_factory=random.Random)
    _items: dict[str, Pending] = field(default_factory=dict)

    # -- lifecycle ---------------------------------------------------------

    def open(self, kind: Kind, payload: Any = None) -> Pending:
        """Register a new item. Must be called with a running event loop."""
        future = asyncio.get_running_loop().create_future()
        item = Pending(self._new_id(), kind, future, payload)
        self._items[item.id] = item
        return item

    def resolve(self, id_: str, value: Any) -> Pending | None:
        item = self._items.pop(id_.lower(), None)
        if item is not None and not item.future.done():
            item.future.set_result(value)
        return item

    def discard(self, id_: str) -> None:
        """Forget an item without resolving it (e.g. after a timeout)."""
        self._items.pop(id_.lower(), None)

    def cancel_all(self, reason: str) -> int:
        items = list(self._items.values())
        self._items.clear()
        for item in items:
            if not item.future.done():
                item.future.set_result(Cancelled(reason))
        return len(items)

    def get(self, id_: str) -> Pending | None:
        return self._items.get(id_.lower())

    def items(self, *kinds: Kind) -> list[Pending]:
        return [i for i in self._items.values() if not kinds or i.kind in kinds]

    def __len__(self) -> int:
        return len(self._items)

    def _new_id(self) -> str:
        while True:
            id_ = "".join(self.rng.choice(ID_ALPHABET) for _ in range(ID_LENGTH))
            if id_ not in self._items:
                return id_

    # -- inbound messages ----------------------------------------------------

    def handle(self, text: str) -> Outcome | None:
        """Apply a chat message to the open items.

        Returns None when the message is not a reply (route it onwards).
        Results set on the futures:
          permission / plan -> `Reply`
          question          -> `dict[str, str]` (question text -> answer)
        """
        if not self._items:
            return None
        stripped = text.strip()
        if not stripped or stripped.startswith("/"):
            return None
        reply = parse_reply(stripped)
        if reply is not None:
            outcome = self._handle_decision(reply, stripped)
            if outcome is not None:
                return outcome
        return self._handle_answer(stripped)

    def _handle_decision(self, reply: Reply, raw: str) -> Outcome | None:
        if reply.id is not None and reply.id not in self._items:
            # Five letters that aren't an open ID are just the start of the text.
            parts = raw.split(None, 1)
            reply = Reply(reply.decision, None, parts[1].strip() if len(parts) > 1 else None)
        if reply.id is not None:
            item = self._items[reply.id]
            if item.kind == "question":
                return Outcome(
                    message=f"{item.id} is a question: reply with option numbers or text"
                )
        else:
            candidates = self.items("permission", "plan")
            if not candidates:
                return None
            if len(candidates) > 1:
                return Outcome(message=_ambiguous(candidates))
            item = candidates[0]
        if item.kind == "plan" and reply.decision == "always":
            return Outcome(message=f"plans take y or n (with feedback), not always: {item.id}")
        self.resolve(item.id, Reply(reply.decision, item.id, reply.text))
        return Outcome(resolved=item)

    def _handle_answer(self, text: str) -> Outcome | None:
        questions = self.items("question")
        if not questions:
            return None
        m = _ID_PREFIX_RE.match(text)
        item: Pending | None = None
        if m and m.group(1).lower() in self._items:
            item = self._items[m.group(1).lower()]
            if item.kind != "question":
                return Outcome(message=f"{item.id} is a {item.kind} prompt: reply y or n")
            text = m.group(2) or ""
        elif len(questions) == 1:
            item = questions[0]
        else:
            return Outcome(message=_ambiguous(questions))
        try:
            answers = parse_answers(_questions_of(item), text)
        except ValueError as e:
            return Outcome(message=f"{item.id}: {e}")
        self.resolve(item.id, answers)
        return Outcome(resolved=item)


def _questions_of(item: Pending) -> list[Mapping[str, Any]]:
    payload = item.payload
    if isinstance(payload, Mapping):
        return list(payload.get("questions") or [])
    return list(payload or [])


def _ambiguous(items: Sequence[Pending]) -> str:
    ids = ", ".join(i.id for i in items)
    return f"several prompts are open ({ids}): add the ID to your reply"
