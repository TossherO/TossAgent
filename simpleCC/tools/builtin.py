from typing import Any, Callable
import fnmatch
import subprocess

from ..utils import safe_path
from .framework import ToolSpec, ToolResult


def filesystem_tools(config=None) -> tuple[list[ToolSpec], dict[str, Callable]]:
    file_limit = getattr(config, "file_read_limit_chars", 20000)
    glob_limit = getattr(config, "glob_output_limit_chars", 12000)
    glob_matches_limit = getattr(config, "glob_max_matches", 1000)
    schemas = {
        "read_file": ({"type": "object", "properties": {"path": {"type": "string"}, "limit": {"type": "integer"}, "offset": {"type": "integer"}}, "required": ["path"]}, "Read a text file."),
        "write_file": ({"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}, "Write a text file."),
        "edit_file": ({"type": "object", "properties": {"path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"}}, "required": ["path", "old_text", "new_text"]}, "Replace one exact text occurrence."),
        "glob": ({"type": "object", "properties": {"pattern": {"type": "string"}}, "required": ["pattern"]}, "List workspace files matching a pattern."),
    }

    def read(args, ctx):
        lines = safe_path(ctx.workdir, args["path"]).read_text(encoding="utf-8").splitlines()
        offset, limit = int(args.get("offset", 0)), args.get("limit")
        selected = lines[offset:] if limit is None else lines[offset:offset + int(limit)]
        content = "\n".join(selected)
        truncated = len(content) > file_limit
        if truncated:
            content = content[:file_limit] + f"\n[file output truncated: limit={file_limit} chars]"
        return ToolResult(content, metadata={"truncated": truncated})

    def write(args, ctx):
        path = safe_path(ctx.workdir, args["path"]); path.parent.mkdir(parents=True, exist_ok=True); path.write_text(args["content"], encoding="utf-8"); return ToolResult(f"Wrote {path.relative_to(ctx.workdir)}")

    def edit(args, ctx):
        path = safe_path(ctx.workdir, args["path"]); text = path.read_text(encoding="utf-8"); count = text.count(args["old_text"])
        if count != 1: return ToolResult(f"Expected exactly one match, found {count}", True)
        path.write_text(text.replace(args["old_text"], args["new_text"], 1), encoding="utf-8"); return ToolResult(f"Edited {path.relative_to(ctx.workdir)}")

    def glob(args, ctx):
        matches = sorted(p.relative_to(ctx.workdir).as_posix() for p in ctx.workdir.rglob("*") if p.is_file() and fnmatch.fnmatch(p.relative_to(ctx.workdir).as_posix(), args["pattern"]))[:glob_matches_limit]
        content = "\n".join(matches)
        truncated = len(content) > glob_limit
        if truncated:
            content = content[:glob_limit] + f"\n[glob output truncated: limit={glob_limit} chars]"
        return ToolResult(content, metadata={"match_count": len(matches), "truncated": truncated})

    names = list(schemas.keys())
    specs = [ToolSpec(name, schemas[name][1], schemas[name][0]) for name in names]
    handlers = {"read_file": read, "write_file": write, "edit_file": edit, "glob": glob}
    return specs, handlers


def shell_tools(config) -> tuple[list[ToolSpec], dict[str, Callable]]:
    spec = ToolSpec("bash", "Run a shell command in the workspace.", {"type": "object", "properties": {"command": {"type": "string"}, "timeout": {"type": "number"}}, "required": ["command"]})

    def handler(args, ctx):
        timeout = float(args.get("timeout", config.command_timeout))
        try:
            completed = subprocess.run(args["command"], shell=True, cwd=ctx.workdir, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            return ToolResult(f"Command timed out after {timeout}s: {exc}", True)
        output = (completed.stdout or "") + (completed.stderr or "")
        if len(output) > config.max_output_chars:
            output = output[:config.max_output_chars] + f"\n[output truncated; limit={config.max_output_chars} characters]"
        return ToolResult((f"exit_code={completed.returncode}\n" if completed.returncode else "") + (output or "(no output)"), bool(completed.returncode))

    return [spec], {"bash": handler}
