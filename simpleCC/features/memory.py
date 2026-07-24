import json
import re
import threading
from pathlib import Path

from ..tools.framework import ToolResult, ToolSpec
from ..utils import safe_path, atomic_write
from ..core.agent import extract_text, response_content


class MemoryStore:
    MEMORY_TYPES = {"user", "feedback", "project", "reference"}
    MAX_FILE_CHARS = 20000
    MAX_INDEX_LINES = 200

    def __init__(self, path: Path, limit: int = 4000):
        self.path = path if path.suffix == ".md" else path / "MEMORY.md"
        self.root = self.path.parent.resolve()
        self.limit = limit
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()

    def _safe_file(self, filename: str):
        try:
            path = safe_path(self.root, filename)
        except ValueError:
            return None
        if path.suffix != ".md":
            return None
        return path

    def _parse(self, text: str):
        if not text.startswith("---"):
            return {}, text.strip()
        parts = text.split("---", 2)
        if len(parts) != 3:
            return {}, text.strip()
        metadata = {}
        for line in parts[1].strip().splitlines():
            if ":" in line:
                key, value = line.split(":", 1)
                metadata[key.strip()] = value.strip().strip('"').strip("'")
        return metadata, parts[2].strip()

    def read(self):
        if not self.path.exists():
            return ""
        return self.path.read_text(encoding="utf-8")[: self.limit]

    def list_memories(self):
        records = []
        for path in sorted(self.root.glob("*.md")):
            if path.resolve() == self.path.resolve() or not path.is_file():
                continue
            metadata, body = self._parse(path.read_text(encoding="utf-8"))
            records.append({"filename": path.name, "name": metadata.get("name", path.stem), "description": metadata.get("description", ""), "type": metadata.get("type", "user"), "body": body})
        return records

    def read_file(self, filename: str):
        path = self._safe_file(filename)
        if path is None or path.resolve() == self.path.resolve() or not path.exists():
            return None
        return path.read_text(encoding="utf-8")[: self.MAX_FILE_CHARS]

    def _slug(self, name: str):
        slug = re.sub(r"[^a-zA-Z0-9_-]+", "-", name.strip().lower()).strip("-")
        return slug[:100] or "memory"

    def write_memory(self, name: str, mem_type: str, description: str, body: str):
        name, mem_type = str(name).strip(), str(mem_type).strip().lower()
        description, body = str(description).strip(), str(body).strip()
        if not name or not description or not body:
            raise ValueError("Memory name, description, and body are required")
        if mem_type not in self.MEMORY_TYPES:
            raise ValueError(f"Unsupported memory type: {mem_type}")
        if len(body) > self.MAX_FILE_CHARS:
            raise ValueError("Memory body is too large")
        path = self._safe_file(f"{self._slug(name)}.md")
        if path is None:
            raise ValueError("Invalid memory filename")
        with self.lock:
            atomic_write(path, f"---\nname: {name}\ndescription: {description}\ntype: {mem_type}\n---\n\n{body}\n")
            self.rebuild_index()
        return path

    def rebuild_index(self):
        lines = []
        for record in self.list_memories():
            lines.append(f"- [{record['name']}]({record['filename']}) — {record['description']}")
        content = "\n".join(lines[: self.MAX_INDEX_LINES])
        atomic_write(self.path, content + ("\n" if content else ""))

    def select_relevant(self, messages, llm, max_items=5):
        records = self.list_memories()
        recent = []
        for message in reversed(messages):
            if message.get("role") != "user":
                continue
            content = message.get("content", "")
            if isinstance(content, list):
                content = " ".join(str(item.get("text", "")) for item in content if isinstance(item, dict) and item.get("type") == "text")
            if isinstance(content, str) and content.strip():
                recent.append(content)
            if len(recent) == 3:
                break
        recent_text = " ".join(reversed(recent))[:2000]
        if not records or not recent_text:
            return []
        catalog = "\n".join(f"{index}: {item['name']} — {item['description']}" for index, item in enumerate(records))
        prompt = f"Select clearly relevant memory indices. Return only a JSON integer array, or [].\nRecent conversation:\n{recent_text}\nMemory catalog:\n{catalog}"
        try:
            response = llm.create("", [{"role": "user", "content": prompt}], [])
            text = extract_text(response_content(response)).strip()
            match = re.search(r"\[[^\]]*\]", text)
            if match:
                selected = []
                for index in json.loads(match.group()):
                    if isinstance(index, int) and 0 <= index < len(records) and records[index]["filename"] not in selected:
                        selected.append(records[index]["filename"])
                    if len(selected) >= max_items:
                        break
                return selected
        except Exception:
            pass
        keywords = {word.lower() for word in recent_text.split() if len(word) > 3}
        return [item["filename"] for item in records if any(word in (item["name"] + " " + item["description"]).lower() for word in keywords)][:max_items]

    def load_relevant(self, messages, llm, max_items=5, max_chars=30000):
        parts = ["<relevant_memories>"]
        total = 0
        for filename in self.select_relevant(messages, llm, max_items):
            content = self.read_file(filename)
            if content and total + len(content) <= max_chars:
                parts.append(content)
                total += len(content)
        return "\n\n".join(parts + ["</relevant_memories>"]) if len(parts) > 1 else ""

    def extract(self, messages, llm):
        dialogue = []
        for message in messages[-10:]:
            content = message.get("content", "")
            if isinstance(content, list):
                content = " ".join(str(item.get("text", "")) for item in content if isinstance(item, dict) and item.get("type") == "text")
            if isinstance(content, str) and content.strip():
                dialogue.append(f"{message.get('role', '?')}: {content}")
        if not dialogue:
            return 0
        existing = "\n".join(f"- {item['name']}: {item['description']}" for item in self.list_memories()) or "(none)"
        prompt = "Extract only stable user preferences, explicit feedback, or project facts. Return only a JSON array of objects with name, type, description, body. type must be user, feedback, project, or reference. Return [] when nothing new is present or existing memories cover it.\nExisting memories:\n" + existing + "\nDialogue:\n" + "\n".join(dialogue)[:6000]
        try:
            response = llm.create("", [{"role": "user", "content": prompt}], [])
            match = re.search(r"\[.*\]", extract_text(response_content(response)), re.DOTALL)
            if not match:
                return 0
            count = 0
            for item in json.loads(match.group()):
                if not isinstance(item, dict):
                    continue
                try:
                    self.write_memory(item.get("name", ""), item.get("type", ""), item.get("description", ""), item.get("body", ""))
                    count += 1
                except (TypeError, ValueError):
                    continue
            return count
        except Exception:
            return 0

    def consolidate(self, llm, threshold=10, max_items=30):
        records = self.list_memories()
        if len(records) < threshold:
            return 0
        catalog = "\n\n".join(f"{item['filename']}\nname: {item['name']}\ntype: {item['type']}\ndescription: {item['description']}\nbody:\n{item['body']}" for item in records)[:16000]
        prompt = "Merge duplicate or stale memories. Preserve important stable preferences and project facts. Return only a JSON array of objects with name, type, description, body, with at most " + str(max_items) + " items. Do not invent facts.\n" + catalog
        try:
            response = llm.create("", [{"role": "user", "content": prompt}], [])
            match = re.search(r"\[.*\]", extract_text(response_content(response)), re.DOTALL)
            if not match:
                return 0
            items = json.loads(match.group())
            if not isinstance(items, list) or len(items) > max_items:
                return 0
            validated = []
            for item in items:
                if not isinstance(item, dict):
                    return 0
                name, mem_type = str(item.get("name", "")).strip(), str(item.get("type", "")).strip().lower()
                description, body = str(item.get("description", "")).strip(), str(item.get("body", "")).strip()
                if not name or mem_type not in self.MEMORY_TYPES or not description or not body or len(body) > self.MAX_FILE_CHARS:
                    return 0
                validated.append((name, mem_type, description, body))
            with self.lock:
                old_paths = [self._safe_file(item["filename"]) for item in records]
                written = []
                for item in validated:
                    path = self._safe_file(f"{self._slug(item[0])}.md")
                    if path is None:
                        return 0
                    atomic_write(path, f"---\nname: {item[0]}\ndescription: {item[2]}\ntype: {item[1]}\n---\n\n{item[3]}\n")
                    written.append(path)
                for path in old_paths:
                    if path and path not in written and path.exists():
                        path.unlink()
                self.rebuild_index()
            return len(validated)
        except Exception:
            return 0


def register_memory(store: MemoryStore):
    spec = ToolSpec("read_memory", "Read the persistent workspace memory index. Relevant memory details are loaded automatically when applicable.", {"type": "object", "properties": {}})
    return [spec], {"read_memory": lambda _args, _ctx: ToolResult(store.read() or "(empty memory)")}
