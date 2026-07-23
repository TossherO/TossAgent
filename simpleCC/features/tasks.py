import json
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

from ..tools.framework import ToolResult, ToolSpec
from ..utils import KeyedJsonStore


@dataclass
class Task:
    id: str
    subject: str
    description: str
    status: str = "pending"
    owner: str | None = None
    blocked_by: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict) -> "Task":
        return cls(data["id"], data["subject"], data.get("description", ""), data.get("status", "pending"), data.get("owner"), data.get("blocked_by", data.get("blockedBy", [])))


class TaskStore(KeyedJsonStore):
    def __init__(self, root: Path):
        super().__init__(root, "task_")

    def get(self, task_id):
        data = self.read(task_id)
        return Task.from_dict(data) if data else None

    def list(self):
        with self.lock:
            return [task for key in self.list_keys() if (task := self.get(key)) is not None]

    def create(self, subject, description, blocked_by=None):
        with self.lock:
            task = Task(uuid.uuid4().hex, subject, description, blocked_by=list(blocked_by or []))
            self.write(task.id, asdict(task))
            return task

    def claim(self, task_id, owner):
        with self.lock:
            task = self.get(task_id)
            if task is None:
                return None, "Task not found"
            if task.status != "pending" or task.owner:
                return None, "Task is not available"
            for dependency in task.blocked_by:
                item = self.get(dependency)
                if item is None or item.status != "completed":
                    return None, f"Task is blocked by {dependency}"
            task.status, task.owner = "in_progress", owner
            self.write(task.id, asdict(task))
            return task, None

    def complete(self, task_id, owner=None):
        with self.lock:
            task = self.get(task_id)
            if task is None:
                return None, "Task not found"
            if task.status != "in_progress":
                return None, "Task is not in progress"
            if owner and task.owner != owner:
                return None, "Task belongs to another owner"
            task.status = "completed"
            self.write(task.id, asdict(task))
            return task, None


def register_tasks(store: TaskStore):
    schemas = {
        "create_task": {"type": "object", "properties": {"subject": {"type": "string"}, "description": {"type": "string"}, "blocked_by": {"type": "array", "items": {"type": "string"}}}, "required": ["subject", "description"]},
        "list_tasks": {"type": "object", "properties": {}},
        "get_task": {"type": "object", "properties": {"task_id": {"type": "string"}}, "required": ["task_id"]},
        "claim_task": {"type": "object", "properties": {"task_id": {"type": "string"}, "owner": {"type": "string"}}, "required": ["task_id"]},
        "complete_task": {"type": "object", "properties": {"task_id": {"type": "string"}, "owner": {"type": "string"}}, "required": ["task_id"]},
    }
    descriptions = {
        "create_task": "Create a persistent task in workspace/.tasks. Use it for work spanning sessions, requiring ownership, collaboration, or dependencies. blocked_by must contain existing task IDs. Creating a task does not execute it.",
        "list_tasks": "List persistent tasks with IDs, status, owners, and dependencies. Use this to discover tasks and obtain IDs for get_task, claim_task, or blocked_by.",
        "get_task": "Retrieve one persistent task by ID, including its status, owner, description, and dependencies.",
        "claim_task": "Claim an available persistent task for the current agent. It must be pending, unowned, and all blocked_by tasks must be completed. Claiming changes it to in_progress but does not perform the work.",
        "complete_task": "Mark a task owned by the current agent as completed after the actual work is finished. This only updates task state.",
    }
    specs = [ToolSpec(name, descriptions[name], schema) for name, schema in schemas.items()]

    def create(args, _):
        return ToolResult(json.dumps(asdict(store.create(args["subject"], args["description"], args.get("blocked_by"))), ensure_ascii=False))
    def listing(_, __):
        return ToolResult(json.dumps([asdict(t) for t in store.list()], ensure_ascii=False))
    def get(args, _):
        task = store.get(args["task_id"])
        return ToolResult(json.dumps(asdict(task), ensure_ascii=False) if task else "Task not found", task is None)
    def claim(args, ctx):
        task, error = store.claim(args["task_id"], args.get("owner", ctx.session_id))
        return ToolResult(json.dumps(asdict(task), ensure_ascii=False) if task else error, task is None)
    def complete(args, ctx):
        task, error = store.complete(args["task_id"], args.get("owner", ctx.session_id))
        return ToolResult(json.dumps(asdict(task), ensure_ascii=False) if task else error, task is None)
    return specs, {"create_task": create, "list_tasks": listing, "get_task": get, "claim_task": claim, "complete_task": complete}
