"""Turn SDK messages and approval prompts into chat text.

Pure logic: no XMPP dependency. Output uses XEP-0393 message styling
(`*bold*`, `_italic_`, `` `mono` ``, ``` fenced blocks), which most XMPP
clients render and which still reads fine as plain text.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ConversationResetMessage,
    PermissionUpdate,
    ResultMessage,
    ServerToolUseBlock,
    SystemMessage,
    TaskNotificationMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

FENCE = "```"

# Tools whose call is shown by its own prompt (question, plan), not a tool line.
PROMPT_TOOLS = frozenset({"AskUserQuestion", "ExitPlanMode"})

_EDIT_TOOLS = frozenset({"Edit", "MultiEdit", "Write", "NotebookEdit"})


# -- small helpers --------------------------------------------------------------


def truncate(text: str, limit: int) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[: max(limit - 1, 0)].rstrip() + "…"


def one_line(text: str, limit: int = 200) -> str:
    """First line of `text`, truncated; marks dropped lines with `…`."""
    lines = text.strip().splitlines() or [""]
    first = lines[0]
    if len(lines) > 1:
        first += " …"
    return truncate(first, limit)


def mono(text: str) -> str:
    """Inline monospace span. Backticks inside would end the span early."""
    return "`" + text.replace("`", "'") + "`" if text else "``"


def fenced(text: str, lang: str = "") -> str:
    # A ``` line inside the body would close the block early.
    body = re.sub(r"^```", "'''", text.rstrip("\n"), flags=re.MULTILINE)
    return f"{FENCE}{lang}\n{body}\n{FENCE}"


def _short_path(path: Any, cwd: Path | None) -> str:
    p = str(path or "")
    if cwd is not None and p:
        try:
            rel = Path(p).relative_to(cwd)
        except ValueError:
            return p
        return str(rel) or "."
    return p


def _compact_json(value: Any, limit: int) -> str:
    try:
        text = json.dumps(value, ensure_ascii=False, separators=(", ", ": "))
    except (TypeError, ValueError):
        text = repr(value)
    return truncate(text, limit)


def markdown_to_chat(text: str) -> str:
    """Light Markdown → XEP-0393 conversion. Code fences are left alone.

    `**bold**` → `*bold*` (`__x__` is left alone: it is usually a dunder), `# Heading` → `*Heading*`,
    `[text](url)` → `text (url)`, `- item` / `* item` → `• item`.
    """
    out: list[str] = []
    in_fence = False
    for line in text.split("\n"):
        if line.lstrip().startswith(FENCE):
            in_fence = not in_fence
            out.append(line)
            continue
        if in_fence:
            out.append(line)
            continue
        m = re.match(r"^\s{0,3}#{1,6}\s+(.*?)\s*#*\s*$", line)
        if m:
            line = f"*{_strip_bold(m.group(1))}*" if m.group(1) else ""
        else:
            line = re.sub(r"^(\s*)[-*+]\s+", r"\1• ", line)
            line = re.sub(r"\*\*(?=\S)(.+?)(?<=\S)\*\*", r"*\1*", line)
        line = re.sub(r"(?<!!)\[([^\]]+)\]\(([^)\s]+)\)", _link, line)
        out.append(line)
    return "\n".join(out)


def _strip_bold(text: str) -> str:
    return re.sub(r"\*\*(.+?)\*\*", r"\1", text)


def _link(m: re.Match[str]) -> str:
    label, url = m.group(1), m.group(2)
    return url if label == url else f"{label} ({url})"


def format_duration(ms: float) -> str:
    s = ms / 1000
    if s < 60:
        return f"{s:.0f}s" if s >= 10 else f"{s:.1f}s"
    m, s = divmod(int(round(s)), 60)
    if m < 60:
        return f"{m}m{s:02d}s"
    h, m = divmod(m, 60)
    return f"{h}h{m:02d}m"


def format_cost(usd: float | None) -> str | None:
    if usd is None:
        return None
    return f"${usd:.4f}" if usd < 1 else f"${usd:.2f}"


# -- tool lines ------------------------------------------------------------------


def tool_line(name: str, tool_input: Mapping[str, Any], cwd: Path | None = None) -> str:
    """One-line summary of a tool call, e.g. 🔧 Bash: `ls -la`."""
    i = tool_input or {}
    path = _short_path(i.get("file_path") or i.get("notebook_path") or i.get("path"), cwd)
    if name == "Bash":
        line = f"🔧 Bash: {mono(one_line(str(i.get('command', ''))))}"
        if i.get("run_in_background"):
            line += " (background)"
        return line
    if name in _EDIT_TOOLS:
        icon = "📝" if name == "Write" else "✏️"
        return f"{icon} {name} {path}".rstrip()
    if name == "Read":
        return f"📖 Read {path}".rstrip()
    if name in ("Grep", "Glob"):
        line = f"🔍 {name} {mono(one_line(str(i.get('pattern', '')), 120))}"
        return f"{line} in {path}" if path else line
    if name == "WebFetch":
        return f"🌐 WebFetch {i.get('url', '')}".rstrip()
    if name == "WebSearch":
        return f"🌐 WebSearch {mono(one_line(str(i.get('query', '')), 120))}"
    if name in ("Task", "Agent"):
        desc = i.get("description") or one_line(str(i.get("prompt", "")), 80)
        kind = i.get("subagent_type")
        return f"🤖 {name}{f' ({kind})' if kind else ''}: {desc}"
    if name == "TodoWrite":
        todos = i.get("todos") or []
        done = sum(1 for t in todos if isinstance(t, Mapping) and t.get("status") == "completed")
        active = next(
            (t for t in todos if isinstance(t, Mapping) and t.get("status") == "in_progress"),
            None,
        )
        line = f"📋 Todos {done}/{len(todos)}"
        if active:
            line += f" · {active.get('activeForm') or active.get('content', '')}"
        return line
    if name.startswith("mcp__"):
        parts = name.split("__", 2)
        label = "/".join(p for p in parts[1:] if p)
        return f"🔌 {label} {_compact_json(dict(i), 120)}" if i else f"🔌 {label}"
    return f"🔧 {name} {_compact_json(dict(i), 120)}" if i else f"🔧 {name}"


def result_text(content: Any) -> str:
    """Flatten ToolResultBlock.content to text."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    parts: list[str] = []
    for item in content:
        if isinstance(item, Mapping):
            if item.get("type") == "text":
                parts.append(str(item.get("text", "")))
            elif item.get("type") == "image":
                parts.append("[image]")
            else:
                parts.append(_compact_json(dict(item), 200))
        else:
            parts.append(str(item))
    return "\n".join(parts)


# -- SDK messages ----------------------------------------------------------------


class Renderer:
    """Stateful renderer: remembers tool names so results can be labelled.

    `render(msg)` returns the chat messages to post (possibly none). Chunking
    to the XMPP size limit is left to the sender (`chunk`).
    """

    def __init__(
        self,
        verbosity: str = "normal",
        cwd: Path | str | None = None,
        result_preview: int = 600,
    ) -> None:
        self.verbosity = verbosity
        self.cwd = Path(cwd) if cwd is not None else None
        self.result_preview = result_preview
        self._tools: dict[str, str] = {}

    @property
    def quiet(self) -> bool:
        return self.verbosity == "quiet"

    @property
    def verbose(self) -> bool:
        return self.verbosity == "verbose"

    def render(self, msg: Any) -> list[str]:
        if isinstance(msg, AssistantMessage):
            return self._assistant(msg)
        if isinstance(msg, UserMessage):
            return self._user(msg)
        if isinstance(msg, ResultMessage):
            line = self.result_line(msg)
            return [line] if line else []
        if isinstance(msg, ConversationResetMessage):
            self._tools.clear()
            return ["🧹 conversation cleared"]
        if isinstance(msg, SystemMessage):
            return self._system(msg)
        return []

    # assistant -----------------------------------------------------------------

    def _assistant(self, msg: AssistantMessage) -> list[str]:
        sub = msg.parent_tool_use_id is not None
        out: list[str] = []
        for block in msg.content:
            if isinstance(block, TextBlock):
                if sub and not self.verbose:
                    continue
                text = markdown_to_chat(block.text).strip()
                if text:
                    out.append(_indent(text) if sub else text)
            elif isinstance(block, (ToolUseBlock, ServerToolUseBlock)):
                self._tools[block.id] = block.name
                if block.name in PROMPT_TOOLS or self.quiet or (sub and not self.verbose):
                    continue
                line = tool_line(block.name, block.input, self.cwd)
                out.append(_indent(line) if sub else line)
            # ThinkingBlock, ServerToolResultBlock: hidden.
        if msg.error:
            out.append(f"⚠️ API error: {msg.error}")
        return out

    # user (tool results) -------------------------------------------------------

    def _user(self, msg: UserMessage) -> list[str]:
        if isinstance(msg.content, str) or self.quiet:
            return []
        sub = msg.parent_tool_use_id is not None
        out: list[str] = []
        for block in msg.content:
            if not isinstance(block, ToolResultBlock):
                continue
            name = self._tools.pop(block.tool_use_id, None)
            if name in PROMPT_TOOLS:
                continue  # the answer/denial is already visible in the chat
            if not block.is_error and not self.verbose:
                continue
            if sub and not self.verbose:
                continue
            text = result_text(block.content).strip()
            label = name or "tool"
            if block.is_error:
                head = f"✗ {label} failed"
            else:
                head = f"↳ {label}"
            if not text:
                line = head
            elif "\n" not in text and len(text) <= 120:
                line = f"{head}: {text}"
            else:
                line = f"{head}:\n{fenced(truncate(text, self.result_preview))}"
            out.append(_indent(line) if sub else line)
        return out

    # system --------------------------------------------------------------------

    def _system(self, msg: SystemMessage) -> list[str]:
        if msg.subtype == "compact_boundary":
            meta = msg.data.get("compact_metadata") or {}
            pre = meta.get("pre_tokens")
            trigger = meta.get("trigger")
            extra = [x for x in (trigger, f"{pre:,} tokens before" if isinstance(pre, int) else None) if x]
            return ["🗜️ context compacted" + (f" ({', '.join(extra)})" if extra else "")]
        if isinstance(msg, TaskNotificationMessage):
            if msg.status == "failed" and not self.quiet:
                summary = getattr(msg, "summary", None) or msg.data.get("summary") or ""
                return [f"⚠️ background task failed{': ' + one_line(str(summary)) if summary else ''}"]
            return []
        return []

    # result --------------------------------------------------------------------

    def result_line(self, msg: ResultMessage) -> str | None:
        """`✓ done · N turns · $X · Ns`, `⏹ stopped …` or `✗ <subtype> …`."""
        stats = [f"{msg.num_turns} turn{'s' if msg.num_turns != 1 else ''}"]
        cost = format_cost(msg.total_cost_usd)
        if cost:
            stats.append(cost)
        stats.append(format_duration(msg.duration_ms))
        tail = " · ".join(stats)
        if (msg.terminal_reason or "").startswith("aborted"):
            return f"⏹ stopped · {tail}"
        if msg.is_error or msg.subtype != "success":
            what = msg.subtype if msg.subtype != "success" else "error"
            if msg.api_error_status:
                what += f" (HTTP {msg.api_error_status})"
            detail = "; ".join(msg.errors or [])
            if not detail and msg.is_error and msg.result:
                detail = msg.result
            line = f"✗ {what} · {tail}"
            if detail:
                line += f"\n{truncate(detail, 500)}"
            return line
        if self.quiet:
            return None
        return f"✓ done · {tail}"


def _indent(text: str) -> str:
    """Mark subagent output. Only the first line, so fences stay intact."""
    return "  ↳ " + text


# -- approval prompts ------------------------------------------------------------


def describe_suggestions(suggestions: Iterable[PermissionUpdate | Mapping[str, Any]]) -> str:
    """Summarise what `a` (always allow) would add, e.g. `Bash(npm test:*)`."""
    parts: list[str] = []
    for s in suggestions:
        d = s.to_dict() if isinstance(s, PermissionUpdate) else dict(s)
        kind = d.get("type")
        where = d.get("destination")
        if kind in ("addRules", "replaceRules"):
            rules = []
            for r in d.get("rules") or []:
                tool = r.get("toolName") or r.get("tool_name") or "?"
                content = r.get("ruleContent") or r.get("rule_content")
                rules.append(f"{tool}({content})" if content else tool)
            text = ", ".join(rules) or "rules"
        elif kind == "setMode":
            text = f"mode {d.get('mode')}"
        elif kind == "addDirectories":
            text = "dirs " + ", ".join(d.get("directories") or [])
        else:
            continue
        if where and where != "session":
            text += f" [{where}]"
        parts.append(text)
    return "; ".join(parts)


def _input_preview(tool_name: str, tool_input: Mapping[str, Any], cwd: Path | None) -> str:
    i = tool_input or {}
    if tool_name == "Bash":
        body = fenced(truncate(str(i.get("command", "")), 1500))
        desc = i.get("description")
        return f"{desc}\n{body}" if desc else body
    if tool_name in ("Edit", "MultiEdit"):
        path = _short_path(i.get("file_path"), cwd)
        edits = i.get("edits") or [i]
        blocks = []
        for e in edits[:3]:
            old = truncate(str(e.get("old_string", "")), 400)
            new = truncate(str(e.get("new_string", "")), 400)
            diff = "\n".join(
                [f"- {ln}" for ln in old.splitlines()] + [f"+ {ln}" for ln in new.splitlines()]
            )
            blocks.append(fenced(diff))
        more = f"\n(+{len(edits) - 3} more edits)" if len(edits) > 3 else ""
        return f"{path}\n" + "\n".join(blocks) + more
    if tool_name == "Write":
        path = _short_path(i.get("file_path"), cwd)
        content = str(i.get("content", ""))
        n = len(content.splitlines())
        return f"{path} ({n} line{'s' if n != 1 else ''})\n{fenced(truncate(content, 600))}"
    line = tool_line(tool_name, i, cwd)
    # Drop the icon and name; the prompt header already shows them.
    detail = line.split(" ", 2)[2] if line.count(" ") >= 2 else ""
    if tool_name.startswith("mcp__") or detail.startswith("{") or not detail:
        return fenced(truncate(json.dumps(dict(i), indent=2, ensure_ascii=False, default=str), 1200)) if i else ""
    return detail


def format_permission(
    id_: str,
    tool_name: str,
    tool_input: Mapping[str, Any],
    *,
    title: str | None = None,
    description: str | None = None,
    reason: str | None = None,
    blocked_path: str | None = None,
    always: str | None = None,
    cwd: Path | str | None = None,
) -> str:
    """Prompt for a tool permission. `always` describes what `a` would allow;
    when None, `a` is not offered."""
    cwd = Path(cwd) if cwd is not None else None
    lines = [f"🔐 *Permission* {mono(id_)} · {title or tool_name}"]
    if description and description != title:
        lines.append(description)
    preview = _input_preview(tool_name, tool_input, cwd)
    if preview:
        lines.append(preview)
    if blocked_path:
        lines.append(f"Path: {blocked_path}")
    if reason:
        lines.append(f"_{reason}_")
    choices = [f"{mono('y ' + id_)} allow", f"{mono('n ' + id_ + ' [reason]')} deny"]
    if always:
        choices.append(f"{mono('a ' + id_)} always allow {always}")
    lines.append("Reply: " + " · ".join(choices))
    return "\n".join(lines)


def format_question(id_: str, questions: Sequence[Mapping[str, Any]]) -> str:
    """Prompt for AskUserQuestion; numbers map to option labels."""
    many = len(questions) > 1
    lines = [f"❓ *Question* {mono(id_)}"]
    for n, q in enumerate(questions, 1):
        header = q.get("header")
        text = str(q.get("question", ""))
        prefix = f"{n}) " if many else ""
        lines.append(f"{prefix}*{header}*: {text}" if header else f"{prefix}{text}")
        for k, opt in enumerate(q.get("options") or [], 1):
            label = opt.get("label", "")
            desc = opt.get("description")
            lines.append(f"   {k}. {label}" + (f" — {desc}" if desc else ""))
        if q.get("multiSelect"):
            lines.append("   _(pick several: 1,3)_")
    if many:
        lines.append(
            f"Reply: {mono(id_)} then one line per question, each with option numbers or text"
        )
    else:
        lines.append(f"Reply: {mono(id_ + ' 2')}, option numbers, or your own text")
    return "\n".join(lines)


def format_plan(id_: str, plan: str) -> str:
    """Prompt for ExitPlanMode."""
    body = markdown_to_chat(plan.strip()) or "_(empty plan)_"
    return (
        f"📋 *Plan* {mono(id_)}\n{body}\n"
        f"Reply: {mono('y ' + id_)} approve · {mono('n ' + id_ + ' <feedback>')} revise"
    )


# -- chunking --------------------------------------------------------------------


def chunk(text: str, max_len: int) -> list[str]:
    """Split `text` into messages of at most `max_len` characters.

    Splits on line boundaries. A ``` block that spans a split is closed at
    the end of one chunk and reopened (with its language tag) in the next.
    Lines longer than a chunk are hard-split.
    """
    if max_len < 20:
        raise ValueError("max_len too small")
    text = text.strip("\n")
    if len(text) <= max_len:
        return [text] if text.strip() else []

    chunks: list[str] = []
    cur: list[str] = []
    cur_len = 0  # length of "\n".join(cur)
    opener: str | None = None  # the ``` line of the open fence, if any
    close_cost = len(FENCE) + 1

    def flush() -> None:
        nonlocal cur, cur_len
        body = list(cur)
        if opener is not None:
            if body and body[-1] == opener:
                body.pop()  # split right after the opener: no empty block
            else:
                body.append(FENCE)
        content = "\n".join(body).strip("\n")
        if content.strip():
            chunks.append(content)
        cur = [opener] if opener is not None else []
        cur_len = len(opener) if opener is not None else 0

    for line in text.split("\n"):
        is_fence = line.lstrip().startswith(FENCE)
        if is_fence:
            pieces = [line]
        else:
            # A chunk may need to hold the reopened fence and the closing one too.
            limit = max_len - (len(opener) + 1 + close_cost if opener is not None else 0)
            pieces = [line[k : k + limit] for k in range(0, len(line), limit)] or [""]
        # Is a fence open once this line is in? Then leave room to close it.
        open_after = (opener is None) == is_fence
        reserve = close_cost if open_after else 0
        for piece in pieces:
            if cur and cur_len + 1 + len(piece) + reserve > max_len:
                flush()
            cur_len += len(piece) + (1 if cur else 0)
            cur.append(piece)
        if is_fence:
            opener = None if opener is not None else line.strip()
    flush()  # closes a fence left open in the input
    return chunks
