from __future__ import annotations

import json
import os
import signal
import subprocess
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from ..tools.framework import ToolResult, ToolSpec
from ..utils import KeyedJsonStore


class BackgroundTaskManager(KeyedJsonStore):
    def __init__(self, root: Path, workdir: Path, output_dir: Path, max_workers: int = 4, output_limit_chars: int = 12000):
        super().__init__(root, "background_")
        self.workdir = workdir.resolve()
        self.output_dir = output_dir.resolve()
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.output_limit_chars = output_limit_chars
        self.executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="simplecc-bg")
        self.processes: dict[str, subprocess.Popen] = {}
        self.closed = False

    def _output_path(self, task_id: str):
        return self.output_dir / f"background_{task_id}.log"

    def _now(self):
        return datetime.now(timezone.utc).isoformat()

    def _preview(self, record, tail_chars=2000):
        path = Path(record["output_path"])
        if not path.exists():
            return ""
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""
        return text[-max(0, min(int(tail_chars), self.output_limit_chars)):]

    def _result(self, record, tail_chars=2000):
        result = dict(record)
        result["output_preview"] = self._preview(record, tail_chars)
        return result

    def start(self, command: str, timeout: float = 120.0, label: str = ""):
        command = str(command).strip()
        if not command:
            raise ValueError("Background command is required")
        timeout = max(0.1, float(timeout))
        with self.lock:
            if self.closed:
                raise RuntimeError("Background task manager is closed")
            task_id = f"bg-{uuid.uuid4().hex}"
            output_path = self._output_path(task_id)
            record = {"id": task_id, "command": command, "label": str(label).strip(), "status": "queued", "created_at": self._now(), "started_at": None, "finished_at": None, "timeout": timeout, "pid": None, "return_code": None, "error": None, "output_path": str(output_path)}
            self.write(task_id, record)
            self.executor.submit(self._run, task_id)
            return self._result(record)

    def _run(self, task_id: str):
        with self.lock:
            record = self.read(task_id)
            if not record or self.closed:
                return
            output_path = Path(record["output_path"])
            record.update({"status": "running", "started_at": self._now()})
            self.write(task_id, record)
        process = None
        try:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            stream = output_path.open("w", encoding="utf-8", errors="replace")
            try:
                process = subprocess.Popen(record["command"], shell=True, cwd=str(self.workdir), stdout=stream, stderr=subprocess.STDOUT, text=True, start_new_session=True)
                with self.lock:
                    self.processes[task_id] = process
                    record = self.read(task_id) or record
                    record["pid"] = process.pid
                    self.write(task_id, record)
                try:
                    return_code = process.wait(timeout=record["timeout"])
                    status = "completed" if return_code == 0 else "failed"
                    error = None
                except subprocess.TimeoutExpired:
                    self._terminate_process(process)
                    return_code = process.returncode
                    status = "timed_out"
                    error = f"Command timed out after {record['timeout']}s"
            finally:
                stream.close()
            with self.lock:
                record = self.read(task_id) or record
                if record["status"] != "cancelled":
                    record.update({"status": status, "return_code": return_code, "error": error, "finished_at": self._now()})
                self.write(task_id, record)
        except Exception as exc:
            with self.lock:
                record = self.read(task_id) or {"id": task_id}
                record.update({"status": "failed", "error": f"{type(exc).__name__}: {exc}", "finished_at": self._now()})
                self.write(task_id, record)
        finally:
            with self.lock:
                self.processes.pop(task_id, None)

    def _terminate_process(self, process):
        if process.poll() is not None:
            return
        try:
            os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=3)
        except (ProcessLookupError, subprocess.TimeoutExpired, OSError):
            if process.poll() is None:
                process.kill()
                process.wait()

    def get(self, task_id: str, tail_chars: int = 2000):
        with self.lock:
            record = self.read(str(task_id))
            return self._result(record, tail_chars) if record else None

    def list(self, include_completed: bool = True):
        with self.lock:
            records = []
            for key in self.list_keys():
                record = self.read(key)
                if record and (include_completed or record["status"] in {"queued", "running"}):
                    records.append(self._result(record, 500))
            return records

    def read_output(self, task_id: str, tail_chars: int = 12000):
        with self.lock:
            record = self.read(str(task_id))
            if not record:
                return None
            return {"id": record["id"], "status": record["status"], "output": self._preview(record, tail_chars), "output_path": record["output_path"]}

    def cancel(self, task_id: str):
        with self.lock:
            record = self.read(str(task_id))
            if not record:
                return None
            if record["status"] in {"completed", "failed", "timed_out", "cancelled"}:
                return self._result(record)
            record["status"] = "cancelled"
            record["finished_at"] = self._now()
            process = self.processes.get(str(task_id))
            if process:
                self._terminate_process(process)
            self.write(str(task_id), record)
            return self._result(record)

    def close(self):
        with self.lock:
            if self.closed:
                return
            self.closed = True
            for task_id, process in list(self.processes.items()):
                self._terminate_process(process)
                record = self.read(task_id)
                if record and record["status"] in {"queued", "running"}:
                    record.update({"status": "cancelled", "finished_at": self._now()})
                    self.write(task_id, record)
            self.processes.clear()
        self.executor.shutdown(wait=False, cancel_futures=True)


def register_background_tools(manager: BackgroundTaskManager):
    specs = [
        ToolSpec("run_background", "Run a shell command in the background and return immediately with a task ID.", {"type": "object", "properties": {"command": {"type": "string"}, "timeout": {"type": "number"}, "label": {"type": "string"}}, "required": ["command"]}),
        ToolSpec("get_background_task", "Get one background task status, process information, and output preview.", {"type": "object", "properties": {"task_id": {"type": "string"}, "tail_chars": {"type": "integer"}}, "required": ["task_id"]}),
        ToolSpec("list_background_tasks", "List background tasks and their current statuses.", {"type": "object", "properties": {"include_completed": {"type": "boolean"}}}),
        ToolSpec("read_background_output", "Read the recent output of a background task.", {"type": "object", "properties": {"task_id": {"type": "string"}, "tail_chars": {"type": "integer"}}, "required": ["task_id"]}),
        ToolSpec("cancel_background_task", "Cancel a queued or running background task.", {"type": "object", "properties": {"task_id": {"type": "string"}}, "required": ["task_id"]}),
    ]

    def run(args, _ctx):
        return ToolResult(json.dumps(manager.start(args["command"], args.get("timeout", 120), args.get("label", "")), ensure_ascii=False))

    def get(args, _ctx):
        result = manager.get(args["task_id"], args.get("tail_chars", 2000))
        return ToolResult(json.dumps(result, ensure_ascii=False) if result else "Background task not found", result is None)

    def listing(args, _ctx):
        return ToolResult(json.dumps(manager.list(args.get("include_completed", True)), ensure_ascii=False))

    def output(args, _ctx):
        result = manager.read_output(args["task_id"], args.get("tail_chars", 12000))
        return ToolResult(json.dumps(result, ensure_ascii=False) if result else "Background task not found", result is None)

    def cancel(args, _ctx):
        result = manager.cancel(args["task_id"])
        return ToolResult(json.dumps(result, ensure_ascii=False) if result else "Background task not found", result is None)

    return specs, {"run_background": run, "get_background_task": get, "list_background_tasks": listing, "read_background_output": output, "cancel_background_task": cancel}
