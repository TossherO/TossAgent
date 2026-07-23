from dataclasses import dataclass

from ..tools.framework import ToolContext, ToolResult, ToolSpec


@dataclass
class TodoState:
    items: list[dict]


def register_todo(state: TodoState):
    todo_item = {
        "type": "object",
        "properties": {
            "id": {"type": "string"},
            "content": {"type": "string"},
            "status": {"type": "string", "enum": ["pending", "in_progress", "completed"]},
            "priority": {"type": "string", "enum": ["low", "medium", "high"]},
        },
        "required": ["id", "content", "status", "priority"],
        "additionalProperties": False,
    }
    spec = ToolSpec("todo_write", "Maintain a lightweight checklist for the current session. Use it for multi-step work without persistent tracking, ownership, or dependency requirements. This replaces the entire current todo list and does not create persistent tasks.", {"type": "object", "properties": {"todos": {"type": "array", "items": todo_item}}, "required": ["todos"]})

    def write(args, _ctx: ToolContext):
        state.items = args["todos"]
        return ToolResult(f"Updated {len(state.items)} todos")

    return [spec], {"todo_write": write}
