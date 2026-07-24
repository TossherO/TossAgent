from __future__ import annotations

import json
import threading
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from ..tools.framework import ToolResult, ToolSpec
from ..utils import JsonStore


@dataclass
class CronJob:
    id: str
    cron: str
    prompt: str
    recurring: bool = True
    durable: bool = True


_RANGES = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 6))


def _field_matches(field: str, value: int, minimum: int, maximum: int) -> bool:
    for part in field.split(","):
        part = part.strip()
        if not part:
            return False
        if part == "*":
            if minimum <= value <= maximum:
                return True
            continue
        if part.startswith("*/"):
            try:
                step = int(part[2:])
            except ValueError:
                return False
            if step <= 0:
                return False
            if value % step == 0:
                return True
            continue
        if "-" in part:
            pieces = part.split("-")
            if len(pieces) != 2:
                return False
            try:
                lower, upper = map(int, pieces)
            except ValueError:
                return False
            if minimum <= lower <= upper <= maximum and lower <= value <= upper:
                return True
            continue
        try:
            if value == int(part) and minimum <= value <= maximum:
                return True
        except ValueError:
            return False
    return False


def _field_valid(field: str, minimum: int, maximum: int) -> bool:
    if not field:
        return False
    for part in field.split(","):
        part = part.strip()
        if part == "*":
            continue
        if part.startswith("*/"):
            try:
                if not 0 < int(part[2:]) <= maximum - minimum + 1:
                    return False
            except ValueError:
                return False
            continue
        if "-" in part:
            pieces = part.split("-")
            if len(pieces) != 2:
                return False
            try:
                lower, upper = map(int, pieces)
            except ValueError:
                return False
            if not minimum <= lower <= upper <= maximum:
                return False
            continue
        try:
            if not minimum <= int(part) <= maximum:
                return False
        except ValueError:
            return False
    return True


def validate_cron(expression: str) -> str | None:
    fields = str(expression).split()
    if len(fields) != 5:
        return "Cron expression must contain five fields: minute hour day-of-month month day-of-week"
    for field, (minimum, maximum) in zip(fields, _RANGES):
        if not _field_valid(field, minimum, maximum):
            return f"Invalid cron field: {field}"
    return None


def cron_matches(expression: str, moment: datetime) -> bool:
    minute, hour, day, month, weekday = expression.split()
    values = [moment.minute, moment.hour, moment.day, moment.month, (moment.weekday() + 1) % 7]
    if not _field_matches(minute, values[0], 0, 59) or not _field_matches(hour, values[1], 0, 23) or not _field_matches(month, values[3], 1, 12):
        return False
    day_ok = _field_matches(day, values[2], 1, 31)
    weekday_ok = _field_matches(weekday, values[4], 0, 6)
    if day == "*" or weekday == "*":
        return day_ok and weekday_ok
    return day_ok or weekday_ok


class CronScheduler:
    def __init__(self, root: Path, on_job, poll_interval: float = 1.0):
        self.root = root.resolve()
        self.on_job = on_job
        self.poll_interval = max(0.1, float(poll_interval))
        self.lock = threading.RLock()
        self.wake = threading.Event()
        self.jobs: dict[str, CronJob] = {}
        self.pending: list[CronJob] = []
        self.last_fired: dict[str, str] = {}
        self.closed = False
        self._store = JsonStore(self.root / "scheduled_tasks.json")
        self.thread = threading.Thread(target=self._loop, name="simplecc-cron", daemon=True)
        self._load()
        self.thread.start()

    def _write(self):
        self._store.write([asdict(job) for job in self.jobs.values() if job.durable])

    def _load(self):
        items = self._store.read(default=[])
        for item in items:
            if not isinstance(item, dict) or validate_cron(item.get("cron", "")):
                continue
            job = CronJob(str(item["id"]), str(item["cron"]), str(item["prompt"]), bool(item.get("recurring", True)), bool(item.get("durable", True)))
            self.jobs[job.id] = job

    def schedule(self, expression: str, prompt: str, recurring: bool = True, durable: bool = True):
        error = validate_cron(expression)
        if error:
            raise ValueError(error)
        if not str(prompt).strip():
            raise ValueError("Scheduled prompt is required")
        with self.lock:
            if self.closed:
                raise RuntimeError("Cron scheduler is closed")
            job = CronJob(f"cron-{uuid.uuid4().hex}", expression, str(prompt).strip(), bool(recurring), bool(durable))
            self.jobs[job.id] = job
            self._write()
            self.wake.set()
            return job

    def list(self):
        with self.lock:
            return [asdict(job) for job in self.jobs.values()]

    def cancel(self, job_id: str):
        with self.lock:
            job = self.jobs.pop(job_id, None)
            self.pending = [item for item in self.pending if item.id != job_id]
            if job:
                self._write()
                return job
            return None

    def _loop(self):
        while not self.closed:
            now = datetime.now()
            marker = now.strftime("%Y-%m-%d %H:%M")
            with self.lock:
                for job in list(self.jobs.values()):
                    if cron_matches(job.cron, now) and self.last_fired.get(job.id) != marker:
                        self.last_fired[job.id] = marker
                        self.pending.append(job)
                        if not job.recurring:
                            self.jobs.pop(job.id, None)
                        self._write()
                pending = list(self.pending)
            for job in pending:
                if self.closed:
                    return
                try:
                    accepted = bool(self.on_job(job))
                except Exception:
                    accepted = True
                if accepted:
                    with self.lock:
                        self.pending = [item for item in self.pending if item.id != job.id]
            self.wake.wait(self.poll_interval)
            self.wake.clear()

    def close(self):
        with self.lock:
            if self.closed:
                return
            self.closed = True
            self.pending.clear()
            self.wake.set()
        self.thread.join(timeout=5)


def register_cron_tools(scheduler: CronScheduler):
    specs = [
        ToolSpec("schedule_cron", "Schedule a prompt using a five-field cron expression. The prompt runs when the Agent is idle.", {"type": "object", "properties": {"cron": {"type": "string"}, "prompt": {"type": "string"}, "recurring": {"type": "boolean"}, "durable": {"type": "boolean"}}, "required": ["cron", "prompt"]}),
        ToolSpec("list_crons", "List scheduled cron prompts.", {"type": "object", "properties": {}}),
        ToolSpec("cancel_cron", "Cancel a scheduled cron prompt before it is consumed.", {"type": "object", "properties": {"job_id": {"type": "string"}}, "required": ["job_id"]}),
    ]

    def schedule(args, _ctx):
        try:
            job = scheduler.schedule(args["cron"], args["prompt"], args.get("recurring", True), args.get("durable", True))
            return ToolResult(json.dumps(asdict(job), ensure_ascii=False))
        except Exception as exc:
            return ToolResult(str(exc), True)

    def listing(_args, _ctx):
        return ToolResult(json.dumps(scheduler.list(), ensure_ascii=False))

    def cancel(args, _ctx):
        job = scheduler.cancel(args["job_id"])
        return ToolResult(json.dumps(asdict(job), ensure_ascii=False) if job else "Cron job not found", job is None)

    return specs, {"schedule_cron": schedule, "list_crons": listing, "cancel_cron": cancel}
