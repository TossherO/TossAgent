from __future__ import annotations

import asyncio
import itertools
import json
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from ..tools.framework import ToolResult, ToolSpec
from .stdio import StdioTransport
from .http import StreamableHTTPTransport


@dataclass(frozen=True)
class MCPServerConfig:
    name: str
    transport: Literal["stdio", "streamable_http"]
    command: str | None = None
    args: tuple[str, ...] = ()
    env: dict[str, str] = field(default_factory=dict)
    url: str | None = None
    headers: dict[str, str] = field(default_factory=dict)
    timeout: float = 30.0
    cwd: str | None = None

    @classmethod
    def from_dict(cls, name: str, data: dict):
        return cls(name, data["transport"], data.get("command"), tuple(data.get("args", ())), dict(data.get("env", {})), data.get("url"), dict(data.get("headers", {})), float(data.get("timeout", 30)), data.get("cwd"))


def tool_name(server: str, original: str) -> str:
    def normalize(value):
        value = re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("_")
        if not value:
            raise ValueError("MCP name cannot be empty")
        return value
    return f"mcp__{normalize(server)}__{normalize(original)}"


def schema_to_spec(server: str, definition: dict) -> ToolSpec:
    original = definition["name"]
    return ToolSpec(tool_name(server, original), definition.get("description", "MCP tool"), definition.get("inputSchema", {"type": "object"}), "mcp", {"server": server, "original_tool_name": original})


def result_to_tool_result(result, max_chars=16000) -> ToolResult:
    content = result.get("content", []) if isinstance(result, dict) else getattr(result, "content", [])
    is_error = bool(result.get("isError", False)) if isinstance(result, dict) else bool(getattr(result, "isError", False))
    structured = result.get("structuredContent") if isinstance(result, dict) else getattr(result, "structuredContent", None)
    parts = []
    for item in content or []:
        kind = item.get("type") if isinstance(item, dict) else getattr(item, "type", "")
        if kind == "text":
            parts.append(item.get("text", "") if isinstance(item, dict) else getattr(item, "text", ""))
        elif kind == "image":
            parts.append(f"[image: {item.get('mimeType', '') if isinstance(item, dict) else getattr(item, 'mimeType', '')}]")
        else:
            parts.append(f"[{kind or 'content'}]")
    if structured is not None:
        parts.append(json.dumps(structured, ensure_ascii=False))
    text = "\n".join(parts) or "(empty MCP result)"
    truncated = len(text) > max_chars
    if truncated:
        text = text[:max_chars] + f"\n[MCP result truncated: limit={max_chars} chars]"
    return ToolResult(text, is_error, {"truncated": truncated})


class EventLoopRunner:
    def __init__(self):
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True, name="simplecc-mcp")
        self.thread.start()
        self.loop.call_soon_threadsafe(asyncio.set_event_loop, self.loop)

    def run(self, coroutine):
        return asyncio.run_coroutine_threadsafe(coroutine, self.loop).result()

    def close(self):
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(timeout=5)
        self.loop.close()


class MCPSession:
    def __init__(self, config, workspace, output_limit_chars=16000):
        self.config, self.workspace, self.output_limit_chars = config, workspace, output_limit_chars
        self.transport, self.tools = None, {}
        self.ids = itertools.count(1)

    async def connect(self):
        try:
            if self.config.transport == "stdio":
                if not self.config.command: raise ValueError("stdio MCP server requires command")
                self.transport = StdioTransport(self.config.command, self.config.args, self.config.env, self.config.cwd or str(self.workspace)); await self.transport.start()
                await self._stdio_request("initialize", self._init_params()); await self.transport.notification({"jsonrpc": "2.0", "method": "notifications/initialized"}); result = await self._stdio_request("tools/list", {})
            else:
                if not self.config.url: raise ValueError("HTTP MCP server requires url")
                self.transport = StreamableHTTPTransport(self.config.url, self.config.headers); await self.transport.start(); await self.transport.request("initialize", self._init_params(), self.config.timeout); await self.transport.notification("notifications/initialized", self.config.timeout); result = await self.transport.request("tools/list", {}, self.config.timeout)
            self.tools = {item["name"]: item for item in result.get("tools", [])}
        except Exception:
            await self.close(); raise

    @staticmethod
    def _init_params(): return {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "simpleCC", "version": "1.0"}}

    async def _stdio_request(self, method, params):
        response = await self.transport.request({"jsonrpc": "2.0", "id": next(self.ids), "method": method, "params": params}, self.config.timeout)
        if "error" in response: raise RuntimeError(response["error"].get("message", "MCP error"))
        return response.get("result", {})

    async def call(self, name, arguments):
        result = await self._stdio_request("tools/call", {"name": name, "arguments": arguments}) if self.config.transport == "stdio" else await self.transport.request("tools/call", {"name": name, "arguments": arguments}, self.config.timeout)
        return result_to_tool_result(result, self.output_limit_chars)

    def specs(self): return [schema_to_spec(self.config.name, item) for item in self.tools.values()]
    async def close(self):
        if self.transport: await self.transport.close(); self.transport = None


class MCPManager:
    def __init__(self, configs=None, workspace=None, output_limit_chars=16000):
        self.configs = {name: MCPServerConfig.from_dict(name, data) for name, data in (configs or {}).items()}; self.workspace = workspace or Path.cwd(); self.output_limit_chars = output_limit_chars; self.runner = EventLoopRunner(); self.sessions = {}; self.closed = False

    def connect(self, name):
        if name not in self.configs: raise ValueError(f"MCP server is not configured: {name}")
        self.close(name); session = MCPSession(self.configs[name], self.workspace); self.runner.run(session.connect()); self.sessions[name] = session; return session.specs()
    def call(self, server, tool, arguments):
        if server not in self.sessions: raise ValueError(f"MCP server is not connected: {server}")
        return self.runner.run(self.sessions[server].call(tool, arguments))
    def close(self, name):
        if self.closed:
            return
        session = self.sessions.pop(name, None)
        if session: self.runner.run(session.close())
    def close_all(self):
        if self.closed:
            return
        for name in tuple(self.sessions): self.close(name)
        self.closed = True
        self.runner.close()
