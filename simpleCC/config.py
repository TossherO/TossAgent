import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv


@dataclass(frozen=True)
class AppConfig:
    workdir: Path
    model: str
    fallback_model: str | None = None
    max_tokens: int = 4096
    request_timeout: float = 120.0
    command_timeout: float = 120.0
    max_output_chars: int = 20000
    max_turns: int = 30
    streaming: bool = True
    permission_mode: str = "interactive"
    color_output: bool = True
    status_output: bool = True
    context_limit: int = 50000
    compaction_max_messages: int = 50
    compaction_tool_result_budget: int = 200000
    compaction_persist_threshold: int = 30000
    compaction_keep_recent_tools: int = 3
    model_context_tokens: int = 128000
    context_budget_tokens: int = 100000
    estimated_chars_per_token: float = 3.5
    file_read_limit_chars: int = 20000
    glob_max_matches: int = 1000
    glob_output_limit_chars: int = 12000
    skill_output_limit_chars: int = 16000
    task_output_limit_chars: int = 12000
    mcp_output_limit_chars: int = 16000
    summary_max_tokens: int = 1500
    memory_extraction_interval: int = 10
    memory_context_threshold_chars: int = 24000
    memory_consolidate_threshold: int = 10
    memory_consolidate_max_items: int = 30
    mcp_servers: dict[str, dict] = field(default_factory=dict)

    @classmethod
    def from_env(cls, workdir: Path | None = None) -> "AppConfig":
        workspace = (workdir or Path(__file__).resolve().parent).resolve()
        config_root = Path(__file__).resolve().parent
        load_dotenv(config_root / ".env", override=False)
        config_path = os.getenv("MCP_CONFIG")
        mcp_servers = {}
        if config_path:
            path = Path(config_path)
            if not path.is_absolute():
                path = config_root / path
            if path.exists():
                data = json.loads(path.read_text(encoding="utf-8"))
                mcp_servers = data.get("mcp", data.get("servers", data))
        return cls(
            workdir=workspace,
            model=os.getenv("MODEL_ID") or os.getenv("ANTHROPIC_MODEL") or "claude-3-5-sonnet-latest",
            fallback_model=os.getenv("FALLBACK_MODEL") or None,
            max_tokens=int(os.getenv("MAX_TOKENS", "4096")),
            request_timeout=float(os.getenv("REQUEST_TIMEOUT", "120")),
            command_timeout=float(os.getenv("COMMAND_TIMEOUT", "120")),
            max_output_chars=int(os.getenv("MAX_OUTPUT_CHARS", "20000")),
            max_turns=int(os.getenv("MAX_TURNS", "30")),
            streaming=os.getenv("STREAMING", "true").lower() not in {"0", "false", "no"},
            permission_mode=os.getenv("PERMISSION_MODE", "interactive"),
            color_output=os.getenv("COLOR_OUTPUT", "true").lower() not in {"0", "false", "no"},
            status_output=os.getenv("STATUS_OUTPUT", "true").lower() not in {"0", "false", "no"},
            context_limit=int(os.getenv("CONTEXT_LIMIT", "50000")),
            compaction_max_messages=int(os.getenv("COMPACTION_MAX_MESSAGES", "50")),
            compaction_tool_result_budget=int(os.getenv("COMPACTION_TOOL_RESULT_BUDGET", "200000")),
            compaction_persist_threshold=int(os.getenv("COMPACTION_PERSIST_THRESHOLD", "30000")),
            compaction_keep_recent_tools=int(os.getenv("COMPACTION_KEEP_RECENT_TOOLS", "3")),
            model_context_tokens=int(os.getenv("MODEL_CONTEXT_TOKENS", "128000")),
            context_budget_tokens=int(os.getenv("CONTEXT_BUDGET_TOKENS", "100000")),
            estimated_chars_per_token=float(os.getenv("ESTIMATED_CHARS_PER_TOKEN", "3.5")),
            file_read_limit_chars=int(os.getenv("FILE_READ_LIMIT_CHARS", "20000")),
            glob_max_matches=int(os.getenv("GLOB_MAX_MATCHES", "1000")),
            glob_output_limit_chars=int(os.getenv("GLOB_OUTPUT_LIMIT_CHARS", "12000")),
            skill_output_limit_chars=int(os.getenv("SKILL_OUTPUT_LIMIT_CHARS", "16000")),
            task_output_limit_chars=int(os.getenv("TASK_OUTPUT_LIMIT_CHARS", "12000")),
            mcp_output_limit_chars=int(os.getenv("MCP_OUTPUT_LIMIT_CHARS", "16000")),
            summary_max_tokens=int(os.getenv("SUMMARY_MAX_TOKENS", "1500")),
            memory_extraction_interval=int(os.getenv("MEMORY_EXTRACTION_INTERVAL", "10")),
            memory_context_threshold_chars=int(os.getenv("MEMORY_CONTEXT_THRESHOLD_CHARS", "24000")),
            memory_consolidate_threshold=int(os.getenv("MEMORY_CONSOLIDATE_THRESHOLD", "10")),
            memory_consolidate_max_items=int(os.getenv("MEMORY_CONSOLIDATE_MAX_ITEMS", "30")),
            mcp_servers=mcp_servers,


        )

    @property
    def tasks_dir(self) -> Path:
        return self.workdir / ".tasks"

    @property
    def memory_dir(self) -> Path:
        return self.workdir / ".memory"

    @property
    def transcript_dir(self) -> Path:
        return self.workdir / ".transcripts"

    @property
    def output_dir(self) -> Path:
        return self.workdir / ".task_outputs" / "tool-results"

    @property
    def skills_dir(self) -> Path:
        return self.workdir / "skills"

    def ensure_dirs(self) -> None:
        for path in (self.tasks_dir, self.memory_dir, self.transcript_dir, self.output_dir):
            path.mkdir(parents=True, exist_ok=True)
