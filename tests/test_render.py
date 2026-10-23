import pytest
from claude_agent_sdk import (
    AssistantMessage,
    ConversationResetMessage,
    PermissionUpdate,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)
from claude_agent_sdk.types import PermissionRuleValue

from claude_xmpp_agent.render import (
    FENCE,
    Renderer,
    chunk,
    describe_suggestions,
    format_duration,
    format_permission,
    format_plan,
    format_question,
    markdown_to_chat,
    tool_line,
)


def assistant(*blocks, parent=None, error=None):
    return AssistantMessage(content=list(blocks), model="m", parent_tool_use_id=parent, error=error)


def result(**kw):
    base = dict(
        subtype="success", duration_ms=12_300, duration_api_ms=1, is_error=False,
        num_turns=3, session_id="s", total_cost_usd=0.0123,
    )
    base.update(kw)
    return ResultMessage(**base)


# -- tool lines ------------------------------------------------------------------


@pytest.mark.parametrize(
    "name, inp, expected",
    [
        ("Bash", {"command": "ls -la"}, "🔧 Bash: `ls -la`"),
        ("Bash", {"command": "a\nb"}, "🔧 Bash: `a …`"),
        ("Bash", {"command": "echo `x`"}, "🔧 Bash: `echo 'x'`"),
        ("Edit", {"file_path": "/w/src/a.py"}, "✏️ Edit src/a.py"),
        ("Write", {"file_path": "/elsewhere/b"}, "📝 Write /elsewhere/b"),
        ("Read", {"file_path": "/w/README.md"}, "📖 Read README.md"),
        ("Grep", {"pattern": "foo", "path": "/w/src"}, "🔍 Grep `foo` in src"),
        ("Glob", {"pattern": "**/*.py"}, "🔍 Glob `**/*.py`"),
        ("WebFetch", {"url": "https://x.org"}, "🌐 WebFetch https://x.org"),
        ("Task", {"description": "look", "subagent_type": "Explore"}, "🤖 Task (Explore): look"),
        ("mcp__srv__do_it", {}, "🔌 srv/do_it"),
        ("Odd", {"k": 1}, '🔧 Odd {"k": 1}'),
    ],
)
def test_tool_line(name, inp, expected):
    assert tool_line(name, inp, cwd=__import__("pathlib").Path("/w")) == expected


def test_tool_line_todos():
    todos = [
        {"content": "a", "status": "completed", "activeForm": "Doing a"},
        {"content": "b", "status": "in_progress", "activeForm": "Doing b"},
        {"content": "c", "status": "pending", "activeForm": "Doing c"},
    ]
    assert tool_line("TodoWrite", {"todos": todos}) == "📋 Todos 1/3 · Doing b"


# -- assistant / user messages ----------------------------------------------------


def test_text_and_tool_normal():
    r = Renderer("normal")
    out = r.render(assistant(
        TextBlock("**Hi**"), ThinkingBlock("hmm", "sig"), ToolUseBlock("t1", "Bash", {"command": "ls"}),
    ))
    assert out == ["*Hi*", "🔧 Bash: `ls`"]


def test_quiet_hides_tools_and_results():
    r = Renderer("quiet")
    assert r.render(assistant(TextBlock("hi"), ToolUseBlock("t1", "Bash", {"command": "ls"}))) == ["hi"]
    assert r.render(UserMessage(content=[ToolResultBlock("t1", "boom", True)])) == []


def test_results_only_errors_in_normal():
    r = Renderer("normal")
    r.render(assistant(ToolUseBlock("t1", "Bash", {"command": "ls"}), ToolUseBlock("t2", "Read", {"file_path": "x"})))
    msg = UserMessage(content=[ToolResultBlock("t1", "file.txt", False), ToolResultBlock("t2", "nope", True)])
    assert r.render(msg) == ["✗ Read failed: nope"]


def test_verbose_shows_truncated_results():
    r = Renderer("verbose", result_preview=50)
    r.render(assistant(ToolUseBlock("t1", "Bash", {"command": "ls"})))
    long = "\n".join(f"line {i}" for i in range(100))
    (out,) = r.render(UserMessage(content=[ToolResultBlock("t1", [{"type": "text", "text": long}])]))
    assert out.startswith("↳ Bash:\n```\nline 0")
    assert out.endswith("…\n```")
    assert len(out) < 100


def test_prompt_tools_hidden():
    r = Renderer("verbose")
    assert r.render(assistant(ToolUseBlock("q", "AskUserQuestion", {"questions": []}))) == []
    assert r.render(UserMessage(content=[ToolResultBlock("q", "answered", False)])) == []


def test_subagent_hidden_unless_verbose():
    blocks = (TextBlock("sub text"), ToolUseBlock("t1", "Read", {"file_path": "f"}))
    assert Renderer("normal").render(assistant(*blocks, parent="p")) == []
    assert Renderer("verbose").render(assistant(*blocks, parent="p")) == [
        "  ↳ sub text", "  ↳ 📖 Read f",
    ]


def test_assistant_error():
    assert Renderer().render(assistant(error="rate_limit")) == ["⚠️ API error: rate_limit"]


def test_plain_user_message_ignored():
    assert Renderer().render(UserMessage(content="hello")) == []


# -- result / system --------------------------------------------------------------


def test_result_success():
    assert Renderer().render(result()) == ["✓ done · 3 turns · $0.0123 · 12s"]


def test_result_success_quiet_is_silent():
    assert Renderer("quiet").render(result()) == []


def test_result_error():
    (out,) = Renderer("quiet").render(result(subtype="error_max_turns", is_error=True, errors=["too many"], num_turns=1))
    assert out == "✗ error_max_turns · 1 turn · $0.0123 · 12s\ntoo many"


def test_result_api_error():
    (out,) = Renderer().render(result(is_error=True, api_error_status=529, result="Overloaded"))
    assert out.startswith("✗ error (HTTP 529) · ")
    assert out.endswith("\nOverloaded")


def test_result_stopped():
    (out,) = Renderer().render(result(terminal_reason="aborted_streaming"))
    assert out.startswith("⏹ stopped · ")


def test_compact_boundary():
    msg = SystemMessage("compact_boundary", {"compact_metadata": {"trigger": "manual", "pre_tokens": 12000}})
    assert Renderer().render(msg) == ["🗜️ context compacted (manual, 12,000 tokens before)"]


def test_init_and_reset():
    r = Renderer()
    assert r.render(SystemMessage("init", {"session_id": "x"})) == []
    reset = ConversationResetMessage(new_conversation_id="n", uuid="u", session_id="s")
    assert r.render(reset) == ["🧹 conversation cleared"]


@pytest.mark.parametrize("ms, text", [(1500, "1.5s"), (12_300, "12s"), (125_000, "2m05s"), (3_725_000, "1h02m")])
def test_format_duration(ms, text):
    assert format_duration(ms) == text


# -- markdown ---------------------------------------------------------------------


def test_markdown_to_chat():
    md = "# Title\n**bold** and __b2__\n- a\n  * b\n[site](https://x.org)\n```\n**raw** - x\n```"
    assert markdown_to_chat(md) == (
        "*Title*\n*bold* and __b2__\n• a\n  • b\nsite (https://x.org)\n```\n**raw** - x\n```"
    )


def test_markdown_keeps_snake_case():
    assert markdown_to_chat("call __init__ and my__var") == "call __init__ and my__var"


# -- prompts ----------------------------------------------------------------------


def test_format_permission_bash():
    text = format_permission("abcde", "Bash", {"command": "rm -rf x", "description": "Remove x"},
                             reason="not in allow list", always="Bash(rm:*)")
    lines = text.splitlines()
    assert lines[0] == "🔐 *Permission* `abcde` · Bash"
    assert "Remove x" in lines
    assert "```\nrm -rf x\n```" in text
    assert "_not in allow list_" in lines
    assert lines[-1] == "Reply: `y abcde` allow · `n abcde [reason]` deny · `a abcde` always allow Bash(rm:*)"


def test_format_permission_without_always():
    text = format_permission("abcde", "Read", {"file_path": "/w/x"}, cwd="/w")
    assert "a abcde" not in text
    assert "\nx\n" in text


def test_format_permission_edit_diff():
    text = format_permission("abcde", "Edit", {"file_path": "/w/a.py", "old_string": "x = 1", "new_string": "x = 2"}, cwd="/w")
    assert "a.py\n```\n- x = 1\n+ x = 2\n```" in text


def test_format_permission_mcp_json():
    text = format_permission("abcde", "mcp__s__t", {"q": "v"})
    assert '```\n{\n  "q": "v"\n}\n```' in text


def test_describe_suggestions():
    s = [
        PermissionUpdate("addRules", rules=[PermissionRuleValue("Bash", "npm test:*")], behavior="allow", destination="session"),
        PermissionUpdate("setMode", mode="acceptEdits", destination="localSettings"),
        {"type": "addDirectories", "directories": ["/tmp/x"]},
    ]
    assert describe_suggestions(s) == "Bash(npm test:*); mode acceptEdits [localSettings]; dirs /tmp/x"


def test_format_question_single():
    q = {"question": "Color?", "header": "Color", "multiSelect": True,
         "options": [{"label": "Red", "description": "warm"}, {"label": "Blue"}]}
    assert format_question("abcde", [q]) == (
        "❓ *Question* `abcde`\n*Color*: Color?\n   1. Red — warm\n   2. Blue\n"
        "   _(pick several: 1,3)_\nReply: `abcde 2`, option numbers, or your own text"
    )


def test_format_question_many():
    qs = [{"question": "A?", "options": [{"label": "x"}]}, {"question": "B?", "options": []}]
    text = format_question("abcde", qs)
    assert "1) A?" in text and "2) B?" in text
    assert "one line per question" in text


def test_format_plan():
    text = format_plan("abcde", "## Steps\n- one\n- two")
    assert text == (
        "📋 *Plan* `abcde`\n*Steps*\n• one\n• two\n"
        "Reply: `y abcde` approve · `n abcde <feedback>` revise"
    )


# -- chunk ------------------------------------------------------------------------


def test_chunk_short_unchanged():
    assert chunk("hello\nworld", 100) == ["hello\nworld"]
    assert chunk("", 100) == []


def test_chunk_splits_on_lines():
    text = "\n".join(f"line {i:02d}" for i in range(30))  # 7 chars + newline each
    parts = chunk(text, 40)
    assert all(len(p) <= 40 for p in parts)
    assert "\n".join(parts) == text


def test_chunk_reopens_fence():
    body = "\n".join(f"code {i:02d} " + "x" * 20 for i in range(20))
    text = f"intro\n```py\n{body}\n```\noutro"
    parts = chunk(text, 120)
    assert len(parts) > 2
    for p in parts:
        assert len(p) <= 120
        lines = p.splitlines()
        assert sum(ln.startswith(FENCE) for ln in lines) % 2 == 0, p
    assert parts[1].startswith("```py\n")
    # Joining the parts and dropping the added fences gives the original lines back.
    rebuilt = [ln for p in parts for ln in p.splitlines() if not ln.startswith(FENCE)]
    assert rebuilt == [ln for ln in text.splitlines() if not ln.startswith(FENCE)]


def test_chunk_no_empty_block_when_split_after_opener():
    text = "a" * 30 + "\n```\n" + "b" * 30 + "\n```"
    parts = chunk(text, 40)
    assert all(p != "```\n```" for p in parts)
    assert all(len(p) <= 40 for p in parts)
    assert parts[0] == "a" * 30


def test_chunk_hard_splits_long_lines():
    text = "y" * 250
    parts = chunk(text, 100)
    assert parts == ["y" * 100, "y" * 100, "y" * 50]


def test_chunk_hard_split_inside_fence():
    text = "```\n" + "z" * 250 + "\n```"
    parts = chunk(text, 60)
    for p in parts:
        assert len(p) <= 60
        assert p.startswith("```\n") and p.endswith("\n```")
    assert "".join(p[4:-4] for p in parts) == "z" * 250


def test_chunk_closes_unterminated_fence():
    text = "```\n" + "\n".join("w" * 10 for _ in range(10))
    parts = chunk(text, 50)
    assert all(p.endswith("```") for p in parts)
