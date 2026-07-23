from __future__ import annotations

import asyncio
import json
import os
import subprocess


class StdioTransport:
    def __init__(self, command, args, env=None, cwd=None):
        self.command, self.args, self.env, self.cwd = command, args, env or {}, cwd
        self.process = None
        self.lock = asyncio.Lock()
        self.stderr_task = None
        self.stderr_tail = []

    async def start(self):
        environment = os.environ.copy(); environment.update(self.env)
        self.process = await asyncio.create_subprocess_exec(self.command, *self.args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=environment, cwd=self.cwd)
        self.stderr_task = asyncio.create_task(self._read_stderr())

    async def _read_stderr(self):
        while self.process and self.process.stderr:
            line = await self.process.stderr.readline()
            if not line: break
            self.stderr_tail.append(line.decode(errors="replace").rstrip()); del self.stderr_tail[:-20]

    async def request(self, message, timeout):
        if self.process is None: await self.start()
        async with self.lock:
            self.process.stdin.write((json.dumps(message, ensure_ascii=False) + "\n").encode())
            await self.process.stdin.drain()
            raw = await asyncio.wait_for(self.process.stdout.readline(), timeout)
        if not raw: raise ConnectionError("MCP stdio server closed: " + "; ".join(self.stderr_tail[-3:]))
        return json.loads(raw)

    async def notification(self, message):
        if self.process is None: await self.start()
        async with self.lock:
            self.process.stdin.write((json.dumps(message, ensure_ascii=False) + "\n").encode())
            await self.process.stdin.drain()

    async def close(self):
        if self.process is None: return
        if self.stderr_task: self.stderr_task.cancel()
        if self.process.returncode is None:
            self.process.terminate()
            try: await asyncio.wait_for(self.process.wait(), 5)
            except asyncio.TimeoutError:
                self.process.kill(); await self.process.wait()
        self.process = None
