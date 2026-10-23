import random
import re

import pytest

from claude_xmpp_agent.approvals import (
    ID_ALPHABET,
    Cancelled,
    Registry,
    Reply,
    parse_answer,
    parse_answers,
    parse_reply,
)

Q_COLOR = {
    "question": "Which color?",
    "header": "Color",
    "options": [{"label": "Red"}, {"label": "Green"}, {"label": "Blue"}],
    "multiSelect": False,
}
Q_TOPPINGS = {
    "question": "Which toppings?",
    "header": "Toppings",
    "options": [{"label": "Cheese"}, {"label": "Ham"}, {"label": "Olives"}],
    "multiSelect": True,
}


# -- parse_reply --------------------------------------------------------------


@pytest.mark.parametrize(
    "text, expected",
    [
        ("y", Reply("yes")),
        ("YES", Reply("yes")),
        ("n", Reply("no")),
        ("No", Reply("no")),
        ("a", Reply("always")),
        ("always", Reply("always")),
        ("y abcde", Reply("yes", "abcde")),
        ("Y ABCDE", Reply("yes", "abcde")),
        ("n abcde too risky", Reply("no", "abcde", "too risky")),
        ("n too risky", Reply("no", None, "too risky")),
        ("no thanks mate", Reply("no", None, "thanks mate")),
        ("  y  ", Reply("yes")),
    ],
)
def test_parse_reply(text, expected):
    assert parse_reply(text) == expected


@pytest.mark.parametrize("text", ["", "yep", "maybe", "nope", "yes!", "ok"])
def test_parse_reply_rejects(text):
    assert parse_reply(text) is None


def test_parse_reply_id_cannot_contain_l():
    # 'l' is not in the ID alphabet, so "hello" is text, not an ID.
    assert parse_reply("n hello") == Reply("no", None, "hello")


# -- parse_answer(s) ------------------------------------------------------------


def test_parse_answer_number_maps_to_label():
    assert parse_answer(Q_COLOR, "2") == "Green"


def test_parse_answer_multi_select():
    assert parse_answer(Q_TOPPINGS, "1,3") == "Cheese, Olives"
    assert parse_answer(Q_TOPPINGS, "3 1") == "Olives, Cheese"
    assert parse_answer(Q_TOPPINGS, "1, 1") == "Cheese"


def test_parse_answer_free_text():
    assert parse_answer(Q_COLOR, "  something purple ") == "something purple"
    assert parse_answer(Q_COLOR, "2 please") == "2 please"


@pytest.mark.parametrize(
    "line, msg",
    [("4", "no option 4"), ("0", "no option 0"), ("1,2", "only one"), ("", "empty")],
)
def test_parse_answer_errors(line, msg):
    with pytest.raises(ValueError, match=msg):
        parse_answer(Q_COLOR, line)


def test_parse_answers_one_line_per_question():
    answers = parse_answers([Q_COLOR, Q_TOPPINGS], "3\n\n1,2\n")
    assert answers == {"Which color?": "Blue", "Which toppings?": "Cheese, Ham"}


def test_parse_answers_wrong_line_count():
    with pytest.raises(ValueError, match="expected 2 lines"):
        parse_answers([Q_COLOR, Q_TOPPINGS], "1")


def test_parse_answers_single_question_joins_lines():
    assert parse_answers([Q_COLOR], "a dark\nshade of red") == {
        "Which color?": "a dark shade of red"
    }


def test_parse_answers_error_names_question():
    with pytest.raises(ValueError, match="question 2: no option 9"):
        parse_answers([Q_COLOR, Q_TOPPINGS], "1\n9")


# -- Registry lifecycle ---------------------------------------------------------


async def test_ids_are_unique_and_valid():
    reg = Registry(rng=random.Random(0))
    ids = {reg.open("permission").id for _ in range(200)}
    assert len(ids) == 200 == len(reg)
    assert all(re.fullmatch(f"[{ID_ALPHABET}]{{5}}", i) for i in ids)
    assert not any("l" in i for i in ids)


async def test_ids_skip_collisions():
    class FixedRng:
        def __init__(self, seq):
            self.seq = iter(seq)

        def choice(self, _alphabet):
            return next(self.seq)

    reg = Registry(rng=FixedRng("aaaaa" "aaaaa" "bbbbb"))
    assert reg.open("permission").id == "aaaaa"
    assert reg.open("permission").id == "bbbbb"


async def test_resolve_sets_future_and_removes():
    reg = Registry()
    item = reg.open("permission", {"tool": "Bash"})
    assert reg.get(item.id.upper()) is item
    assert reg.resolve(item.id, "ok") is item
    assert await item.future == "ok"
    assert reg.get(item.id) is None
    assert reg.resolve(item.id, "again") is None


async def test_cancel_all():
    reg = Registry()
    a, b = reg.open("permission"), reg.open("question")
    assert reg.cancel_all("stopped") == 2
    assert len(reg) == 0
    assert await a.future == Cancelled("stopped")
    assert await b.future == Cancelled("stopped")


async def test_discard_leaves_future_alone():
    reg = Registry()
    item = reg.open("plan")
    reg.discard(item.id)
    assert len(reg) == 0 and not item.future.done()


async def test_items_filters_by_kind():
    reg = Registry()
    p, q = reg.open("permission"), reg.open("question")
    assert reg.items() == [p, q]
    assert reg.items("question") == [q]
    assert reg.items("permission", "plan") == [p]


# -- Registry.handle: y/n/a -----------------------------------------------------


async def test_handle_nothing_open_passes_through():
    assert Registry().handle("y") is None


async def test_handle_single_permission_without_id():
    reg = Registry()
    item = reg.open("permission")
    out = reg.handle("y")
    assert out.ok and out.resolved is item
    assert await item.future == Reply("yes", item.id, None)


async def test_handle_always():
    reg = Registry()
    item = reg.open("permission")
    reg.handle("always")
    assert (await item.future).decision == "always"


async def test_handle_deny_reason_keeps_case():
    reg = Registry()
    item = reg.open("permission")
    reg.handle("n Words Here are the reason")
    assert await item.future == Reply("no", item.id, "Words Here are the reason")


async def test_handle_with_id_among_several():
    reg = Registry()
    a, b = reg.open("permission"), reg.open("permission")
    out = reg.handle(f"n {b.id.upper()} not that one")
    assert out.resolved is b
    assert await b.future == Reply("no", b.id, "not that one")
    assert reg.items() == [a]


async def test_handle_ambiguous_without_id():
    reg = Registry()
    a, b = reg.open("permission"), reg.open("plan")
    out = reg.handle("y")
    assert not out.ok
    assert a.id in out.message and b.id in out.message
    assert len(reg) == 2


async def test_handle_plan_feedback():
    reg = Registry()
    item = reg.open("plan", "the plan")
    reg.handle("n add tests first")
    assert await item.future == Reply("no", item.id, "add tests first")


async def test_handle_plan_rejects_always():
    reg = Registry()
    item = reg.open("plan")
    out = reg.handle("a")
    assert not out.ok and "not always" in out.message
    assert reg.get(item.id) is item


async def test_handle_slash_commands_pass_through():
    reg = Registry()
    reg.open("permission")
    reg.open("question", {"questions": [Q_COLOR]})
    assert reg.handle("/stop") is None
    assert len(reg) == 2


async def test_handle_plain_text_with_only_permission_open_passes_through():
    reg = Registry()
    reg.open("permission")
    assert reg.handle("what are you doing?") is None


# -- Registry.handle: questions ---------------------------------------------------


async def test_handle_question_number_without_id():
    reg = Registry()
    item = reg.open("question", {"questions": [Q_COLOR]})
    out = reg.handle("2")
    assert out.resolved is item
    assert await item.future == {"Which color?": "Green"}


async def test_handle_question_free_text_without_id():
    reg = Registry()
    item = reg.open("question", {"questions": [Q_COLOR]})
    reg.handle("yes please, something warm")
    assert await item.future == {"Which color?": "yes please, something warm"}


async def test_handle_question_bare_yes_is_an_answer_when_no_permission_open():
    reg = Registry()
    item = reg.open("question", {"questions": [Q_COLOR]})
    reg.handle("yes")
    assert await item.future == {"Which color?": "yes"}


async def test_handle_question_with_id():
    reg = Registry()
    q1 = reg.open("question", {"questions": [Q_COLOR]})
    q2 = reg.open("question", {"questions": [Q_TOPPINGS]})
    out = reg.handle(f"{q2.id} 1,3")
    assert out.resolved is q2
    assert await q2.future == {"Which toppings?": "Cheese, Olives"}
    assert reg.items() == [q1]


async def test_handle_question_multi_line():
    reg = Registry()
    item = reg.open("question", {"questions": [Q_COLOR, Q_TOPPINGS]})
    reg.handle(f"{item.id} 1\n2,3")
    assert await item.future == {"Which color?": "Red", "Which toppings?": "Ham, Olives"}


async def test_handle_question_multi_line_without_id():
    reg = Registry()
    item = reg.open("question", {"questions": [Q_COLOR, Q_TOPPINGS]})
    reg.handle("blue\n1")
    assert await item.future == {"Which color?": "blue", "Which toppings?": "Cheese"}


async def test_handle_question_ambiguous_without_id():
    reg = Registry()
    reg.open("question", {"questions": [Q_COLOR]})
    reg.open("question", {"questions": [Q_COLOR]})
    out = reg.handle("1")
    assert not out.ok and "add the ID" in out.message


async def test_handle_question_invalid_number_keeps_item_open():
    reg = Registry()
    item = reg.open("question", {"questions": [Q_COLOR]})
    out = reg.handle("7")
    assert not out.ok and out.message.startswith(item.id) and "no option 7" in out.message
    assert reg.get(item.id) is item


async def test_handle_yes_with_question_id_is_an_error():
    reg = Registry()
    item = reg.open("question", {"questions": [Q_COLOR]})
    out = reg.handle(f"y {item.id}")
    assert not out.ok and "is a question" in out.message


async def test_handle_answer_with_permission_id_is_an_error():
    reg = Registry()
    p = reg.open("permission")
    reg.open("question", {"questions": [Q_COLOR]})
    out = reg.handle(f"{p.id} 1")
    assert not out.ok and "reply y or n" in out.message


async def test_handle_permission_and_question_open():
    reg = Registry()
    p = reg.open("permission")
    q = reg.open("question", {"questions": [Q_COLOR]})
    assert reg.handle("y").resolved is p
    assert reg.handle("3").resolved is q
    assert await q.future == {"Which color?": "Blue"}


async def test_handle_question_payload_as_list():
    reg = Registry()
    item = reg.open("question", [Q_COLOR])
    reg.handle("1")
    assert await item.future == {"Which color?": "Red"}
