from dataclasses import dataclass, field
from datetime import datetime
import json
import random
import time
from typing import Any

from .hooks import USER_PROMPT_SUBMIT, STOP


def block_type(block: Any) -> str:
    return block.get("type", "") if isinstance(block, dict) else getattr(block, "type", "")


def block_value(block: Any, name: str, default=None):
    return block.get(name, default) if isinstance(block, dict) else getattr(block, name, default)


def block_dict(block):
    if isinstance(block, dict):
        return dict(block)
    if hasattr(block, "model_dump"):
        return block.model_dump()
    return {"type": block_type(block)}


def extract_text(content):
    return "\n".join(block_value(b, "text", "") for b in content or [] if block_type(b) == "text")


def response_content(response):
    if isinstance(response, dict):
        return response.get("content", [])
    return getattr(response, "content", [])


def response_stop_reason(response):
    return response.get("stop_reason") if isinstance(response, dict) else getattr(response, "stop_reason", None)


def response_message(response):
    content = response_content(response)
    blocks = []
    for block in content:
        if isinstance(block, dict):
            blocks.append(block)
        elif hasattr(block, "model_dump"):
            blocks.append(block.model_dump())
        else:
            blocks.append({"type": block_type(block), "text": block_value(block, "text", "")})
    return {"role": "assistant", "content": blocks}


def tool_calls(response):
    return [b for b in response_content(response) if block_type(b) == "tool_use"]


def estimate_size(messages):
    return len(json.dumps(messages, ensure_ascii=False, default=str))


def compact(messages, max_chars=100000):
    return messages if estimate_size(messages) <= max_chars else [{"role": "user", "content": "[Earlier conversation compacted; recent context follows.]"}, *messages[-20:]]


@dataclass
class AgentEvent:
    type: str
    message: str = ""
    text: str = ""
    tool_call: Any = None
    tool_result: Any = None
    metadata: dict[str, Any] = field(default_factory=dict)


class PromptAssembler:
    def __init__(self, workdir, memory, skills):
        self.workdir, self.memory, self.skills = workdir, memory, skills

    def build(self, tools):
        sections = ["You are a concise coding agent.", f"Workspace: {self.workdir}", f"Current time: {datetime.now().isoformat(timespec='seconds')}", f"Available tools: {', '.join(s.name for s in tools)}"]
        tool_names = {spec.name for spec in tools}
        if "todo_write" in tool_names and {"create_task", "list_tasks", "get_task", "claim_task", "complete_task"}.issubset(tool_names):
            sections.append("Task planning rules:\n- For a single-step or small straightforward request, do not create a todo or task.\n- Use todo_write for multi-step work confined to the current session without dependencies or ownership.\n- Use Task tools when work must persist across sessions, be owned or claimed, involve collaboration, or contain ordering dependencies.\n- The Task workflow is create_task, claim_task, perform the actual work, then complete_task.\n- blocked_by must reference existing task IDs. Do not confuse todo IDs with task IDs.\n- todo_write replaces the current checklist; Task tools manage persistent task records.")
        sections.extend([f"Skills catalog:\n{self.skills.catalog()}", f"Memory index:\n{self.memory.read() or '(empty)'}", "Relevant memory details are loaded for the current request when applicable. Persist only stable user preferences, explicit feedback, or project facts."])
        return "\n\n".join(sections)

    def build_subagent(self, tools):
        return "\n\n".join(["You are a focused read-only subagent.", f"Workspace: {self.workdir}", "Investigate only the assigned task using the available tools.", "Do not modify files, run shell commands, use MCP, manage tasks or todos, or create another subagent.", "Return a concise factual summary with relevant evidence. Do not expose hidden reasoning.", f"Available tools: {', '.join(s.name for s in tools)}", f"Skills catalog:\n{self.skills.catalog()}", f"Memory index:\n{self.memory.read() or '(empty)'}"])


class RecoveryController:
    def __init__(self, attempts=5, sleep=time.sleep):
        self.attempts = attempts
        self.sleep = sleep

    def _status_code(self, exc):
        return getattr(exc, "status_code", None) or getattr(getattr(exc, "response", None), "status_code", None)

    def _retryable(self, exc):
        status = self._status_code(exc)
        if status in {408, 409, 429} or (isinstance(status, int) and 500 <= status <= 599):
            return True
        return isinstance(exc, (TimeoutError, ConnectionError, OSError))

    def call(self, function, on_retry=None):
        last = None
        consecutive_529 = 0
        for attempt in range(self.attempts):
            try:
                return function()
            except Exception as exc:
                last = exc
                status = self._status_code(exc)
                if not self._retryable(exc) or attempt + 1 >= self.attempts:
                    raise
                consecutive_529 = consecutive_529 + 1 if status == 529 else 0
                base = min(0.5 * (2 ** attempt), 32.0)
                delay = base + random.uniform(0, base * 0.25)
                if on_retry:
                    on_retry(attempt + 1, exc, delay, consecutive_529)
                self.sleep(delay)
        raise last


class LLMClient:
    def __init__(self, config):
        from anthropic import Anthropic
        self.config, self.client, self.recovery = config, Anthropic(timeout=config.request_timeout), RecoveryController()
        self.current_model = config.model
        self.consecutive_529 = 0
        self.fallback_used = False

    def _create(self, system, messages, tools, max_tokens=None):
        return self.client.messages.create(model=self.current_model, max_tokens=max_tokens or self.config.max_tokens, system=system, messages=messages, tools=tools)

    def create(self, system, messages, tools, max_tokens=None):
        def retry(attempt, exc, _delay, consecutive_529):
            self.consecutive_529 = consecutive_529
            if consecutive_529 >= 3 and self.config.fallback_model and not self.fallback_used:
                self.current_model = self.config.fallback_model
                self.fallback_used = True
        return self.recovery.call(lambda: self._create(system, messages, tools, max_tokens), retry)

    def stream(self, system, messages, tools, on_text=None, max_tokens=None):
        emitted = []

        def emit(text):
            if text:
                emitted.append(text)
                if on_text:
                    on_text(text)

        def request():
            create = self.client.messages.create
            events = create(model=self.current_model, max_tokens=max_tokens or self.config.max_tokens, system=system, messages=messages, tools=tools, stream=True)
            blocks = []
            current = None
            for event in events:
                event_type = block_type(event)
                if event_type == "content_block_start":
                    current = block_dict(block_value(event, "content_block", {}))
                    blocks.append(current)
                elif event_type == "content_block_delta":
                    delta = block_dict(block_value(event, "delta", {}))
                    delta_type = block_type(delta)
                    if delta_type == "text_delta":
                        text = block_value(delta, "text", "")
                        emit(text)
                        if current is None:
                            current = {"type": "text", "text": ""}
                            blocks.append(current)
                        current["text"] = current.get("text", "") + text
                    elif delta_type == "input_json_delta" and current is not None:
                        current["partial_json"] = current.get("partial_json", "") + block_value(delta, "partial_json", "")
                elif event_type == "content_block_stop":
                    current = None
            normalized = []
            for block in blocks:
                block = dict(block)
                if block.get("type") == "tool_use" and "partial_json" in block:
                    raw = block.pop("partial_json", "")
                    block["input"] = json.loads(raw or "{}")
                elif block.get("type") == "tool_use" and "input" not in block:
                    block["input"] = {}
                block.pop("partial_json", None)
                normalized.append(block)
            return {"content": normalized}

        try:
            return self.recovery.call(request), "".join(emitted), False
        except Exception:
            response = self.create(system, messages, tools, max_tokens=max_tokens)
            return response, "".join(emitted), True


class AgentLoop:
    def __init__(self, runtime):
        self.runtime = runtime

    def _emit(self, callback, event):
        if callback:
            callback(event)

    def run(self, query, on_event=None, *, registry=None, dispatcher=None, system_prompt=None, context=None, max_turns=None):
        import uuid
        from ..tools.framework import ToolCall, ToolContext
        registry = registry or self.runtime.registry
        dispatcher = dispatcher or self.runtime.dispatcher
        context = context or ToolContext(self.runtime.config.workdir, str(uuid.uuid4()), self.runtime)
        system_prompt = system_prompt or self.runtime.prompt.build(registry.specs())
        turn_limit = max_turns or self.runtime.config.max_turns
        compactor = getattr(self.runtime, "compaction", None)
        messages, memory_snapshot, should_extract = self._prepare(query, context, on_event)
        max_tokens = self.runtime.config.max_tokens
        escalated = False
        continuations = 0
        try:
            for turn in range(turn_limit):
                if compactor:
                    messages = compactor.prepare(system_prompt, registry.anthropic_tools(), messages, self.runtime.llm)
                self._emit(on_event, AgentEvent("llm_start", metadata={"turn": turn + 1}))
                response, emitted = self._call_llm(system_prompt, messages, registry, on_event, max_tokens, compactor)
                stop_reason = response_stop_reason(response)
                if stop_reason == "max_tokens":
                    should_continue, early, max_tokens, escalated, continuations = self._handle_max_tokens(
                        response, messages, max_tokens, escalated, continuations)
                    if early is not None:
                        return early
                    if should_continue:
                        continue
                calls = tool_calls(response)
                if calls and emitted:
                    self._emit(on_event, AgentEvent("thinking_delta", text=emitted))
                if not calls:
                    return self._handle_no_tool_calls(response, messages, context, should_extract, memory_snapshot, on_event)
                results, compact_requested = self._dispatch_tool_calls(calls, dispatcher, context, compactor, on_event)
                if compact_requested:
                    messages = compactor.compact_history(messages, self.runtime.llm) if compactor else messages
                messages.append({"role": "user", "content": results})
            text = f"Reached the maximum tool turns ({turn_limit})."
            self.runtime.hooks.emit(STOP, messages, context)
            self._emit(on_event, AgentEvent("session_stop", message=text))
            return text
        except Exception as exc:
            self.runtime.hooks.emit(STOP, messages, context)
            self._emit(on_event, AgentEvent("error", message=f"{type(exc).__name__}: {exc}"))
            raise

    def _prepare(self, query, context, on_event):
        messages = [{"role": "user", "content": query}]
        memory_snapshot = [dict(message) for message in messages]
        if context.agent_kind != "subagent":
            self.runtime.memory_sessions += 1
            self.runtime.memory_context_chars += len(query)
        should_extract = context.agent_kind != "subagent" and (
            self.runtime.memory_sessions % self.runtime.config.memory_extraction_interval == 0
            or self.runtime.memory_context_chars >= self.runtime.config.memory_context_threshold_chars
            or query.strip().lower().startswith(("remember", "记住", "请记住")))
        relevant = self.runtime.memory.load_relevant(messages, self.runtime.llm)
        if relevant:
            messages[0] = {"role": "user", "content": relevant + "\n\n" + query}
        self.runtime.hooks.emit(USER_PROMPT_SUBMIT, query, context)
        self._emit(on_event, AgentEvent("session_start"))
        self._emit(on_event, AgentEvent("prompt_submitted"))
        return messages, memory_snapshot, should_extract

    def _call_llm(self, system_prompt, messages, registry, on_event, max_tokens, compactor):
        reactive_retries = 0
        while True:
            try:
                if self.runtime.config.streaming:
                    response, emitted, fallback = self.runtime.llm.stream(system_prompt, messages, registry.anthropic_tools(), max_tokens=max_tokens)
                    if fallback:
                        self._emit(on_event, AgentEvent("llm_fallback", message="streaming unavailable; using non-streaming"))
                else:
                    response = self.runtime.llm.create(system_prompt, messages, registry.anthropic_tools(), max_tokens=max_tokens)
                    emitted = ""
                return response, emitted
            except Exception as exc:
                markers = ("prompt_too_long", "too many tokens", "context length", "maximum context")
                if compactor and reactive_retries < 1 and any(marker in str(exc).lower() for marker in markers):
                    messages[:] = compactor.reactive_compact(messages, self.runtime.llm)
                    reactive_retries += 1
                    self._emit(on_event, AgentEvent("llm_fallback", message="context too large; compacting and retrying"))
                    continue
                raise

    def _handle_max_tokens(self, response, messages, max_tokens, escalated, continuations):
        if not escalated:
            return True, None, min(max(max_tokens * 8, 64000), self.runtime.config.model_context_tokens * 4), True, continuations
        messages.append(response_message(response))
        if continuations >= 3:
            return False, extract_text(response_content(response)), max_tokens, escalated, continuations
        messages.append({"role": "user", "content": "Output token limit hit. Resume directly — no apology, no recap. Pick up mid-thought."})
        return True, None, max_tokens, escalated, continuations + 1

    def _handle_no_tool_calls(self, response, messages, context, should_extract, memory_snapshot, on_event):
        text = extract_text(response_content(response))
        self._emit(on_event, AgentEvent("text_delta", text=text))
        self.runtime.hooks.emit(STOP, messages, context)
        if context.agent_kind != "subagent" and should_extract:
            self._extract_and_consolidate(memory_snapshot)
        self._emit(on_event, AgentEvent("session_stop", metadata={"tool_calls": 0}))
        return text

    def _dispatch_tool_calls(self, calls, dispatcher, context, compactor, on_event):
        from ..tools.framework import ToolCall
        results = []
        for block in calls:
            call = ToolCall(block_value(block, "id"), block_value(block, "name"), block_value(block, "input", {}))
            self._emit(on_event, AgentEvent("tool_call", tool_call=call))
            result = dispatcher.dispatch(call, context)
            if result.metadata.get("compact_requested"):
                return results, True
            results.append(result.as_anthropic(call.id))
            self._emit(on_event, AgentEvent("tool_result", tool_call=call, tool_result=result))
        return results, False

    def _extract_and_consolidate(self, memory_snapshot):
        self.runtime.memory.extract(memory_snapshot, self.runtime.llm)
        self.runtime.memory.consolidate(self.runtime.llm, self.runtime.config.memory_consolidate_threshold, self.runtime.config.memory_consolidate_max_items)
        self.runtime.memory_context_chars = 0
