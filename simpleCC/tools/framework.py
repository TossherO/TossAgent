from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable
import time


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]
    source: str = "builtin"
    metadata: dict[str, Any] = field(default_factory=dict)

    def as_anthropic(self):
        return {"name": self.name, "description": self.description, "input_schema": self.input_schema}


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class ToolContext:
    workdir: Path
    session_id: str
    runtime: Any = None
    agent_kind: str = "main"
    parent_session_id: str | None = None
    depth: int = 0
    read_only: bool = False


@dataclass
class ToolResult:
    content: str
    is_error: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

    def as_anthropic(self, tool_use_id):
        return {"type": "tool_result", "tool_use_id": tool_use_id, "content": self.content, "is_error": self.is_error}


ToolHandler = Callable[[dict[str, Any], ToolContext], ToolResult]


class ToolRegistry:
    def __init__(self):
        self._items = {}

    def register(self, spec, handler):
        if spec.name in self._items:
            raise ValueError(f"Tool already registered: {spec.name}")
        self._items[spec.name] = (spec, handler)

    def replace(self, spec, handler):
        self._items[spec.name] = (spec, handler)

    def get(self, name):
        return self._items.get(name)

    def specs(self):
        return [item[0] for item in self._items.values()]

    def anthropic_tools(self):
        return [spec.as_anthropic() for spec in self.specs()]


class ToolRegistryView:
    def __init__(self, registry, allowed_names):
        self.registry = registry
        self.allowed_names = frozenset(allowed_names)

    def get(self, name):
        return self.registry.get(name) if name in self.allowed_names else None

    def specs(self):
        return [spec for spec in self.registry.specs() if spec.name in self.allowed_names]

    def anthropic_tools(self):
        return [spec.as_anthropic() for spec in self.specs()]


class ToolDispatcher:
    def __init__(self, registry, before=None, after=None):
        self.registry, self.before, self.after = registry, before, after

    def dispatch(self, call, context):
        registered = self.registry.get(call.name)
        if registered is None:
            result = ToolResult(f"Unknown tool: {call.name}", True, {"phase": "lookup"})
            if self.after:
                try:
                    self.after(call, result, context)
                except Exception:
                    pass
            return result
        _, handler = registered
        if self.before:
            try:
                decision = self.before(call, context)
            except Exception as exc:
                return ToolResult(f"Permission check failed: {type(exc).__name__}", True, {"blocked": True})
            if decision:
                result = ToolResult(str(decision), True, {"blocked": True})
                if self.after:
                    try:
                        self.after(call, result, context)
                    except Exception:
                        pass
                return result
        started = time.perf_counter()
        try:
            result = handler(call.arguments, context)
            if not isinstance(result, ToolResult):
                result = ToolResult(str(result))
        except Exception as exc:
            result = ToolResult(f"Tool {call.name} failed: {type(exc).__name__}: {exc}", True)
        result.metadata.setdefault("duration_ms", (time.perf_counter() - started) * 1000)
        result.metadata.setdefault("output_chars", len(result.content))
        if self.after:
            try:
                self.after(call, result, context)
            except Exception:
                pass
        return result
