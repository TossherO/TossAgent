import json
import uuid
import threading

from .config import AppConfig
from .core.agent import AgentLoop, LLMClient, PromptAssembler, extract_text
from .core.compaction import CompactionConfig, ContextCompactor
from .core.hooks import install_default_hooks, HookManager
from .mcp import MCPManager
from .tools import ToolDispatcher, ToolRegistry, ToolRegistryView, ToolContext, ToolResult, ToolSpec, filesystem_tools, shell_tools, READONLY_TOOLS
from .features.memory import MemoryStore, register_memory
from .features.skills import SkillStore, register_skills
from .features.tasks import TaskStore, register_tasks
from .features.todo import TodoState, register_todo
from .core.background import BackgroundTaskManager, register_background_tools
from .core.cron import CronJob, CronScheduler, register_cron_tools
from .reserved.teams import DisabledTeamCoordinator
from .reserved.worktrees import DisabledWorktreeManager


class Runtime:
    def __init__(self, config):
        self.config = config
        self.config.ensure_dirs()
        self.registry = ToolRegistry()
        self.hooks = HookManager()
        self.status_output = None
        self._closed = False
        install_default_hooks(self.hooks, config.workdir, permission_mode=config.permission_mode, output=self._status)
        self.mcp = MCPManager(config.mcp_servers, config.workdir, config.mcp_output_limit_chars)
        self.teams = DisabledTeamCoordinator()
        self.worktrees = DisabledWorktreeManager()
        self.tasks = TaskStore(config.tasks_dir)
        self.background_tasks = BackgroundTaskManager(config.tasks_dir / "background", config.workdir, config.output_dir, output_limit_chars=config.task_output_limit_chars)
        self.cron_lock = threading.RLock()
        self.skills = SkillStore(config.skills_dir, config.skill_output_limit_chars)
        self.memory = MemoryStore(config.memory_dir / "MEMORY.md")
        self.memory_sessions = 0
        self.memory_context_chars = 0
        self.todos = TodoState([])
        self.cron_scheduler = CronScheduler(config.tasks_dir, self._accept_cron_job)
        self._register_builtin_tools()
        self.dispatcher = ToolDispatcher(self.registry, self._before_tool, self._after_tool)
        self.llm = LLMClient(config)
        self.prompt = PromptAssembler(config.workdir, self.memory, self.skills)
        self.compaction = ContextCompactor(CompactionConfig(context_limit=max(1024, min(config.context_limit, int(config.context_budget_tokens * config.estimated_chars_per_token))), max_messages=config.compaction_max_messages, keep_recent_tool_results=config.compaction_keep_recent_tools, tool_result_budget=config.compaction_tool_result_budget, persist_threshold=config.compaction_persist_threshold), config.output_dir, config.transcript_dir)

    def _register_builtin_tools(self):
        for specs, handlers in (
            filesystem_tools(self.config),
            shell_tools(self.config),
            register_tasks(self.tasks),
            register_skills(self.skills),
            register_todo(self.todos),
            register_background_tools(self.background_tasks),
            register_cron_tools(self.cron_scheduler),
            register_memory(self.memory),
        ):
            for spec in specs:
                self.registry.register(spec, handlers[spec.name])
        connect_spec = ToolSpec("connect_mcp", "Connect to a configured MCP server.", {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]})
        self.registry.register(connect_spec, self._connect_mcp)
        subagent_spec = ToolSpec("run_subagent", "Run a synchronous, read-only subagent with an independent context for a focused investigation. It returns a concise summary and cannot modify files, run shell commands, use MCP, manage tasks or todos, or create another subagent.", {"type": "object", "properties": {"task": {"type": "string"}, "context": {"type": "string"}, "max_turns": {"type": "integer", "minimum": 1, "maximum": 20}}, "required": ["task"]})
        self.registry.register(subagent_spec, self._run_subagent)
        compact_spec = ToolSpec("compact_context", "Summarize earlier conversation history to free context space. This does not modify files or task state.", {"type": "object", "properties": {}})
        self.registry.register(compact_spec, self._compact_context)

    def _run_subagent(self, args, parent_context):
        task = str(args.get("task", "")).strip()
        if not task:
            return ToolResult("Subagent task is required", True)
        if parent_context.agent_kind == "subagent" or parent_context.depth >= 1:
            return ToolResult("Nested subagents are not allowed", True, {"blocked": True})
        max_turns = max(1, min(int(args.get("max_turns", 8)), 20))
        agent_id = f"subagent-{uuid.uuid4().hex[:12]}"
        registry = ToolRegistryView(self.registry, READONLY_TOOLS)
        context = ToolContext(self.config.workdir, agent_id, self, "subagent", parent_context.session_id, parent_context.depth + 1, True)
        query = f"Focused task:\n{task}\n\nAdditional context:\n{str(args.get('context', '')).strip() or '(none)'}\n\nReturn a concise final summary only."
        try:
            dispatcher = ToolDispatcher(registry, self._before_tool, self._after_tool)
            text = AgentLoop(self).run(query, registry=registry, dispatcher=dispatcher, system_prompt=self.prompt.build_subagent(registry.specs()), context=context, max_turns=max_turns)
            payload = {"status": "completed", "agent_id": agent_id, "summary": text}
            return ToolResult(json.dumps(payload, ensure_ascii=False), metadata={"agent_id": agent_id, "agent_kind": "subagent"})
        except Exception as exc:
            payload = {"status": "failed", "agent_id": agent_id, "error": f"{type(exc).__name__}: {exc}"}
            return ToolResult(json.dumps(payload, ensure_ascii=False), True, {"agent_id": agent_id, "agent_kind": "subagent"})

    def _compact_context(self, _args, context):
        if context.agent_kind == "subagent":
            return ToolResult("Context compaction is not available to subagents", True)
        return ToolResult("Context compaction will be applied before the next model request", metadata={"compact_requested": True})

    def _connect_mcp(self, args, _context):
        try:
            specs = self.mcp.connect(args["name"])
            for spec in specs:
                server = spec.metadata["server"]
                original = spec.metadata["original_tool_name"]
                self.registry.replace(spec, lambda values, _ctx, s=server, t=original: self.mcp.call(s, t, values))
            return ToolResult(f"Connected MCP server {args['name']} with {len(specs)} tools")
        except Exception as exc:
            return ToolResult(f"MCP connection failed: {exc}", True)

    def _accept_cron_job(self, job: CronJob):
        if self._closed:
            return True
        if not self.cron_lock.acquire(blocking=False):
            return False
        try:
            threading.Thread(target=self._run_cron_job, args=(job,), name="simplecc-cron-agent", daemon=True).start()
            return True
        finally:
            self.cron_lock.release()

    def _run_cron_job(self, job: CronJob):
        try:
            self.run(f"[Scheduled] {job.prompt}")
        except Exception as exc:
            self._status(f"Scheduled job {job.id} failed: {type(exc).__name__}: {exc}")

    def _status(self, message):
        if self.status_output:
            self.status_output(message)

    def _before_tool(self, call, context):
        return self.hooks.emit_first("PreToolUse", call, context)

    def _after_tool(self, call, result, context):
        self.hooks.emit("PostToolUse", call, result, context)

    def extract_text(self, response):
        return extract_text(getattr(response, "content", []))

    def close(self):
        if self._closed:
            return
        self._closed = True
        self.cron_scheduler.close()
        self.background_tasks.close()
        self.mcp.close_all()

    @classmethod
    def create(cls, config=None):
        return cls(config or AppConfig.from_env())

    def run(self, query, on_event=None):
        self.status_output = (lambda message: on_event({"type": "status", "message": message})) if on_event else None
        return AgentLoop(self).run(query, on_event=on_event)
