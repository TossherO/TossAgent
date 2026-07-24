import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from .agent import response_content, extract_text


@dataclass
class CompactionConfig:
    context_limit: int = 50000
    max_messages: int = 50
    keep_head: int = 3
    keep_recent_tool_results: int = 3
    tool_result_budget: int = 200000
    persist_threshold: int = 30000
    persist_preview: int = 2000
    summary_max_chars: int = 80000
    reactive_tail_messages: int = 5


def estimate_context_size(system, tools, messages):
    return len(json.dumps({"system": system, "tools": tools, "messages": messages}, ensure_ascii=False, default=str))


def tool_use_ids(message):
    if message.get("role") != "assistant" or not isinstance(message.get("content"), list):
        return set()
    return {block.get("id") for block in message["content"] if isinstance(block, dict) and block.get("type") == "tool_use" and block.get("id")}


def tool_result_ids(message):
    if message.get("role") != "user" or not isinstance(message.get("content"), list):
        return set()
    return {block.get("tool_use_id") for block in message["content"] if isinstance(block, dict) and block.get("type") == "tool_result" and block.get("tool_use_id")}


class ContextCompactor:
    def __init__(self, config=None, output_dir=None, transcript_dir=None, session_id=None):
        self.config = config or CompactionConfig()
        self.output_dir = Path(output_dir) if output_dir else None
        self.transcript_dir = Path(transcript_dir) if transcript_dir else None
        self.session_id = session_id or "session"

    def _persist(self, tool_use_id, content):
        if not self.output_dir:
            return content
        self.output_dir.mkdir(parents=True, exist_ok=True)
        filename = f"{tool_use_id}.txt" if str(tool_use_id).replace("_", "").replace("-", "").isalnum() else hashlib.sha256(str(tool_use_id).encode("utf-8")).hexdigest() + ".txt"
        path = self.output_dir / filename
        if not path.exists():
            path.write_text(content, encoding="utf-8")
        return f"<persisted-output path={path} preview={content[:self.config.persist_preview]}>"

    def tool_result_budget(self, messages):
        if not messages or messages[-1].get("role") != "user" or not isinstance(messages[-1].get("content"), list):
            return messages
        blocks = [b for b in messages[-1]["content"] if isinstance(b, dict) and b.get("type") == "tool_result"]
        total = sum(len(str(b.get("content", ""))) for b in blocks)
        for block in sorted(blocks, key=lambda b: len(str(b.get("content", ""))), reverse=True):
            content = str(block.get("content", ""))
            if total <= self.config.tool_result_budget or len(content) <= self.config.persist_threshold:
                continue
            replacement = self._persist(block.get("tool_use_id", "unknown"), content)
            block["content"] = replacement
            total -= len(content) - len(replacement)
        return messages

    def snip_compact(self, messages):
        if len(messages) <= self.config.max_messages:
            return messages
        head_end = min(self.config.keep_head, len(messages))
        tail_start = max(head_end, len(messages) - (self.config.max_messages - self.config.keep_head))
        if tail_start < len(messages) and tool_result_ids(messages[tail_start]):
            ids = tool_result_ids(messages[tail_start])
            if tail_start and not tool_use_ids(messages[tail_start - 1]).isdisjoint(ids):
                tail_start -= 1
        removed = max(0, tail_start - head_end)
        return messages[:head_end] + [{"role": "user", "content": f"[snipped {removed} messages]"}] + messages[tail_start:]

    def micro_compact(self, messages):
        results = [block for message in messages if message.get("role") == "user" and isinstance(message.get("content"), list) for block in message["content"] if isinstance(block, dict) and block.get("type") == "tool_result"]
        for block in results[:-self.config.keep_recent_tool_results]:
            if len(str(block.get("content", ""))) > 120:
                block["content"] = "[Earlier tool result compacted. Re-run if needed.]"
        return messages

    def write_transcript(self, messages, reason):
        if not self.transcript_dir:
            return None
        self.transcript_dir.mkdir(parents=True, exist_ok=True)
        path = self.transcript_dir / f"transcript_{self.session_id}.jsonl"
        with path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"reason": reason, "messages": messages}, ensure_ascii=False, default=str) + "\n")
        return path

    def fallback_compact(self, messages, reason="compacted"):
        self.write_transcript(messages, reason)
        original = next((m.get("content", "") for m in messages if m.get("role") == "user" and isinstance(m.get("content"), str)), "")
        return [{"role": "user", "content": f"[{reason}]\nOriginal task: {original}"}, *messages[-self.config.reactive_tail_messages:]]

    def compact_history(self, messages, llm_client):
        self.write_transcript(messages, "summary")
        source = json.dumps(messages, ensure_ascii=False, default=str)
        if len(source) > self.config.summary_max_chars:
            third = self.config.summary_max_chars // 3
            source = source[:third] + "\n...[middle omitted]...\n" + source[-(self.config.summary_max_chars - third):]
        prompt = "Summarize this coding-agent conversation. Preserve the original goal and constraints, completed work, findings, files, important tool results, unresolved work, and next steps. Return only a concise factual summary.\n\n" + source
        try:
            response = llm_client.create("You summarize agent context. Do not use tools.", [{"role": "user", "content": prompt}], [])
            summary = extract_text(response_content(response)).strip()
            if summary:
                return [{"role": "user", "content": f"[Compacted conversation summary]\n\n{summary}"}]
        except Exception:
            pass
        return self.fallback_compact(messages, "Compacted conversation")

    def prepare(self, system, tools, messages, llm_client=None):
        self.tool_result_budget(messages)
        messages = self.snip_compact(messages)
        messages = self.micro_compact(messages)
        if estimate_context_size(system, tools, messages) > self.config.context_limit:
            messages = self.compact_history(messages, llm_client) if llm_client else self.fallback_compact(messages)
        return messages
