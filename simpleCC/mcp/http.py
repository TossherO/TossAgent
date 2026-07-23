from __future__ import annotations

import itertools
import json


class StreamableHTTPTransport:
    def __init__(self, url, headers=None):
        self.url, self.headers = url, {"Accept": "application/json, text/event-stream", **(headers or {})}
        self.client, self.session_id, self.ids = None, None, itertools.count(1)

    async def start(self):
        import httpx
        self.client = httpx.AsyncClient(timeout=None)

    async def request(self, method, params, timeout):
        if self.client is None: await self.start()
        request_id = next(self.ids); headers = {**self.headers, "Content-Type": "application/json"}
        if self.session_id: headers["MCP-Session-Id"] = self.session_id
        response = await self.client.post(self.url, json={"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}, headers=headers, timeout=timeout)
        response.raise_for_status(); self.session_id = response.headers.get("MCP-Session-Id", self.session_id)
        data = await self._read_sse(response, request_id) if "text/event-stream" in response.headers.get("content-type", "") else response.json()
        if "error" in data: raise RuntimeError(data["error"].get("message", "MCP JSON-RPC error"))
        return data.get("result", {})

    async def notification(self, method, timeout):
        headers = {**self.headers, "Content-Type": "application/json"}
        if self.session_id: headers["MCP-Session-Id"] = self.session_id
        response = await self.client.post(self.url, json={"jsonrpc": "2.0", "method": method}, headers=headers, timeout=timeout); response.raise_for_status()

    async def _read_sse(self, response, request_id):
        async for line in response.aiter_lines():
            if line.startswith("data:"):
                message = json.loads(line[5:].strip())
                if message.get("id") == request_id: return message
        raise ConnectionError(f"MCP SSE stream ended before response {request_id}")

    async def close(self):
        if self.client: await self.client.aclose(); self.client = None
