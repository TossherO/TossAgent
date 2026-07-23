from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class WorktreeRequest:
    name: str
    task_id: str | None = None


class DisabledWorktreeManager:
    enabled = False

    def create(self, _request: WorktreeRequest):
        return "Worktree isolation is reserved for a future release and is disabled."

    def remove(self, _name: str):
        return "Worktree isolation is reserved for a future release and is disabled."

    def path_for(self, _name: str) -> Path | None:
        return None
