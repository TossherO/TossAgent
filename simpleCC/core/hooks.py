from collections.abc import Callable
from pathlib import Path
import sys

from ..tools.framework import ToolCall, ToolContext, ToolResult


USER_PROMPT_SUBMIT = "UserPromptSubmit"
PRE_TOOL_USE = "PreToolUse"
POST_TOOL_USE = "PostToolUse"
STOP = "Stop"


class HookManager:
    def __init__(self):
        self._hooks: dict[str, list[Callable]] = {}

    def register(self, event: str, callback: Callable) -> None:
        self._hooks.setdefault(self._normalize(event), []).append(callback)

    def _normalize(self, event: str) -> str:
        return {
            "before_tool": PRE_TOOL_USE,
            "after_tool": POST_TOOL_USE,
            "user_prompt_submit": USER_PROMPT_SUBMIT,
            "stop": STOP,
        }.get(event, event)

    def emit(self, event: str, *args, isolate: bool = True):
        results = []
        for callback in self._hooks.get(self._normalize(event), []):
            try:
                results.append(callback(*args))
            except Exception as exc:
                if not isolate:
                    raise
                results.append(exc)
        return results

    def emit_first(self, event: str, *args):
        for callback in self._hooks.get(self._normalize(event), []):
            try:
                result = callback(*args)
            except Exception as exc:
                return exc
            if result:
                return result
        return None


class PermissionPrompter:
    def confirm(self, title: str, details: str) -> bool:
        print(f"\n\033[31m⚠ {title}\033[0m", file=sys.stderr)
        print(f"\033[33m{details}\033[0m", file=sys.stderr)
        try:
            answer = input("Allow? [y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print(file=sys.stderr)
            return False
        return answer in {"y", "yes"}


class DenyPrompter(PermissionPrompter):
    def confirm(self, title: str, details: str) -> bool:
        return False


class AllowPrompter(PermissionPrompter):
    def confirm(self, title: str, details: str) -> bool:
        return True


def _preview(call: ToolCall) -> str:
    values = []
    for key, value in call.arguments.items():
        if key in {"content", "token", "password", "secret"}:
            value = "<redacted>"
        text = str(value).replace("\n", "\\n")
        values.append(f"{key}={text[:160]}")
    return f"Tool: {call.name}({', '.join(values)})"


# ====== Permission strategy table ======

class PermissionRule:
    def check(self, call: ToolCall, context: ToolContext, prompter, base: Path) -> str | None:
        raise NotImplementedError


class ReadOnlyRule(PermissionRule):
    ALLOWED = frozenset({"read_file", "glob", "list_skills", "load_skill", "read_memory"})

    def check(self, call, context, prompter, base):
        if context.agent_kind == "subagent" or context.read_only:
            if call.name not in self.ALLOWED:
                return "Tool denied for read-only subagent"
        return None


class DenyBashRule(PermissionRule):
    def __init__(self, patterns):
        self.patterns = patterns

    def check(self, call, context, prompter, base):
        if call.name != "bash":
            return None
        lowered = str(call.arguments.get("command", "")).lower()
        for pattern in self.patterns:
            if pattern in lowered:
                return f"[permission] blocked bash: matched {pattern!r}"
        return None


class ConfirmBashRule(PermissionRule):
    def __init__(self, patterns):
        self.patterns = patterns

    def check(self, call, context, prompter, base):
        if call.name != "bash":
            return None
        lowered = str(call.arguments.get("command", "")).lower()
        for pattern in self.patterns:
            if pattern in lowered:
                if not prompter.confirm("Potentially destructive command", _preview(call)):
                    return "Permission denied by user"
                break
        return None


class ConfirmOutsideWorkspaceRule(PermissionRule):
    def __init__(self, tool_names):
        self.tool_names = frozenset(tool_names)

    def check(self, call, context, prompter, base):
        if call.name not in self.tool_names:
            return None
        path = (base / str(call.arguments.get("path", ""))).resolve()
        if not path.is_relative_to(base):
            if not prompter.confirm("Writing outside workspace", _preview(call)):
                return "Permission denied by user"
        return None


def install_default_hooks(manager: HookManager, workdir: Path, prompter=None, permission_mode="interactive", output=None):
    if prompter is None:
        prompter = PermissionPrompter() if permission_mode == "interactive" else AllowPrompter() if permission_mode == "allow" else DenyPrompter()

    base = workdir.resolve()
    rules = [
        ReadOnlyRule(),
        DenyBashRule(("rm -rf /", "sudo", "shutdown", "reboot", "mkfs", "dd if=")),
        ConfirmBashRule(("rm ", "> /etc/", "chmod 777")),
        ConfirmOutsideWorkspaceRule({"write_file", "edit_file"}),
    ]

    def emit(message):
        if output:
            output(message)

    def context_inject(query, _context):
        emit(f"[prompt] working in {base}")

    def permission(call: ToolCall, context: ToolContext):
        for rule in rules:
            denial = rule.check(call, context, prompter, base)
            if denial:
                emit(f"[permission] blocked {call.name}")
                return denial if denial.startswith("Permission") else "Permission denied by policy"
        return None

    def log_hook(call: ToolCall, _context: ToolContext):
        emit(f"[tool] { _preview(call) }")

    def post_tool(call: ToolCall, result: ToolResult, _context: ToolContext):
        if result.is_error:
            emit(f"[warning] tool {call.name} failed")
        if result.metadata.get("output_chars", 0) > 100000:
            emit(f"[warning] large output from {call.name}")

    def summary(messages, context):
        count = sum(1 for message in messages if isinstance(message.get("content"), list) for block in message["content"] if isinstance(block, dict) and block.get("type") == "tool_result")
        emit(f"[session] completed; tool calls: {count}")

    manager.register(USER_PROMPT_SUBMIT, context_inject)
    manager.register(PRE_TOOL_USE, permission)
    manager.register(POST_TOOL_USE, post_tool)
    manager.register(STOP, summary)
