from pathlib import Path

from ..tools.framework import ToolResult, ToolSpec
from ..utils import safe_path, truncate_content


class SkillStore:
    MAX_SKILL_BYTES = 200000

    def __init__(self, root: Path, output_limit_chars: int = 16000):
        self.root = root.resolve()
        self.output_limit_chars = output_limit_chars
        self.items: dict[str, Path] = {}
        self.scan()

    def _safe_file(self, path: Path):
        try:
            return safe_path(self.root, path)
        except ValueError:
            return None

    def scan(self):
        items = {}
        if self.root.exists():
            for path in self.root.glob("*/SKILL.md"):
                if not path.is_file():
                    continue
                safe = self._safe_file(path)
                if safe is not None:
                    items[path.parent.name] = safe
        self.items = items

    def catalog(self):
        return "\n".join(f"- {name}" for name in sorted(self.items)) or "(no skills)"

    def load(self, name):
        path = self.items.get(name)
        if path is None:
            raise ValueError(f"Unknown skill: {name}")
        safe = self._safe_file(path)
        if safe is None:
            raise ValueError(f"Skill path escapes skills directory: {name}")
        try:
            size = safe.stat().st_size
        except OSError as exc:
            raise ValueError(f"Cannot inspect skill: {exc}") from exc
        if size > self.MAX_SKILL_BYTES:
            raise ValueError(f"Skill is too large: {name} ({size} bytes)")
        try:
            content = safe.read_text(encoding="utf-8")
            content, _ = truncate_content(content, self.output_limit_chars, "skill output truncated")
            return content
        except UnicodeDecodeError as exc:
            raise ValueError(f"Skill is not valid UTF-8: {name}") from exc


def register_skills(store: SkillStore):
    specs = [
        ToolSpec("list_skills", "Refresh and list available skills under skills/<name>/SKILL.md. Returns skill names only; use load_skill to read a skill.", {"type": "object", "properties": {}}),
        ToolSpec("load_skill", "Refresh and load one skill's SKILL.md by name. Skills are read-only and limited in size; use list_skills first when unsure of the name.", {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}),
    ]

    def listing(_, __):
        store.scan()
        return ToolResult(store.catalog())

    def load(args, __):
        try:
            store.scan()
            return ToolResult(store.load(args["name"]))
        except Exception as exc:
            return ToolResult(str(exc), True)

    return specs, {"list_skills": listing, "load_skill": load}
