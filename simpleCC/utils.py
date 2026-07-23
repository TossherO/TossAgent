from __future__ import annotations

import json
import threading
from pathlib import Path


def safe_path(base: Path, value: str | Path) -> Path:
    base = Path(base).resolve()
    path = (base / value).resolve()
    if not path.is_relative_to(base):
        raise ValueError("Path escapes workspace")
    return path


def atomic_write(path: Path, content: str, *, encoding: str = "utf-8") -> None:
    path = Path(path)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(content, encoding=encoding)
    tmp.replace(path)


class JsonStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()

    def read(self, default=None):
        with self.lock:
            if not self.path.exists():
                return default
            try:
                return json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                return default

    def write(self, data) -> None:
        with self.lock:
            atomic_write(self.path, json.dumps(data, ensure_ascii=False, indent=2))


class KeyedJsonStore:
    def __init__(self, root: Path, prefix: str):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.prefix = prefix
        self.lock = threading.RLock()

    def path_for(self, key: str) -> Path:
        return self.root / f"{self.prefix}{key}.json"

    def read(self, key: str):
        path = self.path_for(key)
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    def write(self, key: str, data) -> None:
        atomic_write(self.path_for(key), json.dumps(data, ensure_ascii=False, indent=2))

    def list_keys(self) -> list[str]:
        names = [p.stem for p in sorted(self.root.glob(f"{self.prefix}*.json"))]
        return [n.removeprefix(self.prefix) for n in names]
