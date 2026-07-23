# simpleCC

simpleCC 是一个基于 Python 的精简 coding agent。它通过 Anthropic Messages API 处理请求，通过统一的工具注册与调度系统执行文件、Shell、任务、Todo、Skills、记忆、MCP 和 subagent 操作。

## 架构

```text
main.py                          CLI 入口、事件渲染
  └── runtime.py                Runtime 编排器（组装子系统、注册工具、委派调用）
        ├── core/
        │     ├── agent.py       AgentLoop、LLMClient、PromptAssembler、RecoveryController、AgentEvent
        │     ├── compaction.py  分层上下文压缩、摘要、transcript
        │     ├── hooks.py      生命周期 Hook、权限策略表
        │     ├── cron.py        Cron 定时任务、队列和持久化
        │     └── background.py  后台 Shell 任务、状态、输出和取消
        ├── tools/
        │     ├── framework.py   ToolSpec、Registry、Dispatcher、Context、Result
        │     └── builtin.py     文件工具（read/write/edit/glob）和 Shell 工具（bash）
        ├── features/
        │     ├── tasks.py       持久化 Task 和 TaskStore
        │     ├── todo.py        session 级 TodoState
        │     ├── skills.py      SkillStore（扫描/加载技能）
        │     └── memory.py      MemoryStore（提取/合并/相关记忆加载）
        ├── mcp/
        │     ├── client.py      MCPSession、MCPManager、协议转换
        │     ├── stdio.py       Stdio 传输（子进程 JSON-RPC）
        │     └── http.py        Streamable HTTP 传输（httpx + SSE）
        ├── reserved/
        │     ├── teams.py       预留且禁用的 Agent teams
        │     └── worktrees.py   预留且禁用的 worktree isolation
        └── utils.py             共享工具：safe_path、atomic_write、JsonStore、KeyedJsonStore
```

核心调用流程：

```text
用户输入
  → Runtime.run()
  → AgentLoop
  → 请求前上下文压缩
  → Anthropic Messages API
  → tool_use
  → Hook / 权限检查
  → ToolDispatcher
  → tool_result
  → 继续模型循环
  → 最终回答
```

## 功能

### 内置工具

- `read_file`、`write_file`、`edit_file`、`glob`
- `bash`
- `todo_write`
- `create_task`、`list_tasks`、`get_task`、`claim_task`、`complete_task`
- `list_skills`、`load_skill`
- `read_memory`
- `run_background`、`get_background_task`、`list_background_tasks`、`read_background_output`、`cancel_background_task`
- `schedule_cron`、`list_crons`、`cancel_cron`
- `connect_mcp`、`run_subagent`、`compact_context`

### 后台任务

后台任务用于执行不会阻塞当前 Agent 的 Shell 命令。`run_background` 会立即返回任务 ID，任务状态和输出分别保存到：

```text
<workdir>/.tasks/background/background_<id>.json
<workdir>/.task_outputs/tool-results/background_<id>.log
```

可通过 `get_background_task` 查询状态和输出预览，通过 `list_background_tasks` 列出任务，通过 `read_background_output` 读取输出，通过 `cancel_background_task` 取消 queued/running 任务。支持 `queued`、`running`、`completed`、`failed`、`timed_out` 和 `cancelled` 状态。Runtime 关闭时会终止仍在运行的后台进程；后台任务不会在进程重启后自动恢复执行。

### 定时任务

定时任务使用五字段 Cron 表达式：

```text
分钟 小时 日 月 星期
```

例如：

```text
*/5 * * * *
0 9 * * 1-5
```

`schedule_cron` 用于创建定时 prompt，`list_crons` 用于查看任务，`cancel_cron` 用于取消尚未消费的任务。任务到期后进入调度队列，在 Agent 可用时以 `[Scheduled] <prompt>` 的形式触发一次 Agent 执行。

支持通配符、步长、列表、范围和单值；星期使用 `0` 表示周日。周期任务默认持久化到 `<workdir>/.tasks/scheduled_tasks.json`，一次性任务可通过 `recurring=false` 创建。当前不补偿进程停止期间错过的任务，定时任务也不会在重启后恢复正在执行的 Agent 调用。

### Skills

Skills 位于当前 workspace：

```text
skills/<skill-name>/SKILL.md
```

程序启动时扫描技能目录；`list_skills` 和 `load_skill` 每次调用也会刷新。技能正文按需通过 `load_skill` 加载，不会全部预置到 system prompt 中。

技能文件必须是 UTF-8，真实路径必须位于 `skills` 目录内，单个文件最大约 200 KB；注入模型上下文的内容默认限制为 16,000 字符。

### Todo 与 Task

- `todo_write`：当前 session 的轻量 checklist，不持久化、不支持 owner 和依赖。
- Task 系列：持久化到 `<workdir>/.tasks`，支持 owner、状态和 `blocked_by` 依赖。
- `PromptAssembler` 仅在两类工具同时存在时注入选择规则。

Task 通常遵循：

```text
create_task → claim_task → 执行实际工作 → complete_task
```

### Subagent

`run_subagent` 是独立于 teams 的同步 focused subagent：

- 独立消息上下文和 session ID；
- 默认只读；
- 只允许 `read_file`、`glob`、`list_skills`、`load_skill`、`read_memory`；
- 不允许 Bash、写文件、MCP、Task/Todo 或递归创建 subagent；
- 完成后只向主 Agent 返回摘要。

Agent teams 和 worktree isolation 当前默认禁用（位于 `reserved/` 目录），不会启动 teammate 或执行 Git worktree 操作。

### 记忆

Memory 使用 workspace 级 `.memory/` 目录。`MEMORY.md` 保存索引，具体记忆保存为带 frontmatter 的 Markdown 文件。相关记忆会按当前请求按需加载；主 Agent 在达到配置的对话次数或上下文阈值后自动提取记忆，并在数量达到阈值时进行合并。Subagent 不会自动写入 memory。

默认配置：

```text
MEMORY_EXTRACTION_INTERVAL=10
MEMORY_CONTEXT_THRESHOLD_CHARS=24000
MEMORY_CONSOLIDATE_THRESHOLD=10
MEMORY_CONSOLIDATE_MAX_ITEMS=30
```

### Hooks 与权限

生命周期事件包括：

```text
UserPromptSubmit
PreToolUse
PostToolUse
Stop
```

默认策略包括：

- 拒绝高风险 Shell 命令；
- 对潜在破坏性命令进行交互确认；
- 对 workspace 外文件写入进行确认；
- subagent 强制只读；
- 工具调用输出和状态输出分离。

权限检查使用策略表模式，新增规则只需实现 `PermissionRule` 接口并注册到规则列表。

## 上下文管理

项目使用分层上下文压缩，顺序为：

```text
工具结果预算与持久化
  → 旧工具结果 micro-compaction
  → 消息数量裁剪
  → LLM 历史摘要
  → 上下文过长时 reactive compact 重试
```

大型工具结果可以保存到：

```text
<workdir>/.task_outputs/tool-results/
```

压缩前会将消息写入：

```text
<workdir>/.transcripts/
```

当前预算使用字符近似 token；`MODEL_CONTEXT_TOKENS` 和 `CONTEXT_BUDGET_TOKENS` 用于计算应用层压缩阈值，并不代表 SDK 会自动校验真实 token 数。工具结果、文件读取、glob、Skills 和 MCP 结果均有独立输出限制；Task/Todo 结果目前仍主要受整体压缩机制限制。

## 安装

建议使用 Python 3.10+ 虚拟环境：

```bash
cd simpleCC
pip install -r requirements.txt
```

`.env` 固定从本项目 `simpleCC/.env` 加载，与 `--workdir` 无关。常用配置：

```text
ANTHROPIC_API_KEY=your-key
MODEL_ID=your-model
ANTHROPIC_BASE_URL=https://api.anthropic.com
MAX_TOKENS=4096
MAX_TURNS=30
STREAMING=true
PERMISSION_MODE=interactive
MCP_CONFIG=mcp.json
```

不要将包含真实密钥的 `.env` 提交到版本库。

## 重要配置

### 模型和请求

```text
REQUEST_TIMEOUT=120
COMMAND_TIMEOUT=120
MAX_TOKENS=4096
MAX_TURNS=30
STREAMING=true
```

`MAX_TOKENS` 是单次模型输出上限，不等于上下文窗口大小。

### 上下文预算

```text
MODEL_CONTEXT_TOKENS=128000
CONTEXT_BUDGET_TOKENS=100000
ESTIMATED_CHARS_PER_TOKEN=3.5
CONTEXT_LIMIT=50000
```

`CONTEXT_LIMIT` 是旧版兼容配置，实际压缩阈值仍按字符近似；`CONTEXT_BUDGET_TOKENS` 用于限制应用预算。对于确定支持 1M 上下文的模型，可在项目 `.env` 中覆盖：

```text
MODEL_CONTEXT_TOKENS=1000000
CONTEXT_BUDGET_TOKENS=900000
```

### 记忆、后台任务与定时任务

```text
MEMORY_EXTRACTION_INTERVAL=10
MEMORY_CONTEXT_THRESHOLD_CHARS=24000
MEMORY_CONSOLIDATE_THRESHOLD=10
MEMORY_CONSOLIDATE_MAX_ITEMS=30
```

后台任务默认最多使用 4 个 worker；定时调度器默认每秒检查一次任务。定时任务和后台任务均属于当前 Runtime 进程，关闭 CLI 后不会作为独立服务继续运行。

### 压缩配置

```text
COMPACTION_MAX_MESSAGES=50
COMPACTION_TOOL_RESULT_BUDGET=200000
COMPACTION_PERSIST_THRESHOLD=30000
COMPACTION_KEEP_RECENT_TOOLS=3
SUMMARY_MAX_TOKENS=1500
```

### 工具输出限制

```text
MAX_OUTPUT_CHARS=20000
FILE_READ_LIMIT_CHARS=20000
GLOB_MAX_MATCHES=1000
GLOB_OUTPUT_LIMIT_CHARS=12000
SKILL_OUTPUT_LIMIT_CHARS=16000
TASK_OUTPUT_LIMIT_CHARS=12000
MCP_OUTPUT_LIMIT_CHARS=16000
```

`MAX_OUTPUT_CHARS` 主要用于 bash；其他工具使用各自的专用限制。

## MCP 配置

通过项目 `.env` 中的 `MCP_CONFIG` 指定 JSON 文件；相对路径相对于 `simpleCC` 项目目录解析：

```json
{
  "servers": {
    "local": {
      "transport": "stdio",
      "command": "python",
      "args": ["/absolute/path/to/server.py"],
      "timeout": 30
    },
    "remote": {
      "transport": "streamable_http",
      "url": "http://127.0.0.1:8000/mcp",
      "headers": {},
      "timeout": 30
    }
  }
}
```

启动后调用 `connect_mcp`，成功后 MCP 工具以 `mcp__server__tool` 形式加入工具池。当前不实现 legacy HTTP+SSE、OAuth、resources、prompts 和 sampling。

## 运行

默认 workspace 是 `simpleCC` 目录：

```bash
cd simpleCC
python main.py
```

也支持从仓库根目录运行：

```bash
python simpleCC/main.py
```

单次请求：

```bash
python simpleCC/main.py --query "读取当前目录的文件结构"
```

指定 workspace：

```bash
python simpleCC/main.py --workdir /path/to/workspace
```

常用选项：

```text
--query QUERY                 执行单次请求后退出
--workdir PATH                指定 workspace
--quiet                       隐藏状态和中间输出
--permission-mode MODE        interactive / deny / allow
--no-stream                   禁用流式输出
--no-color                    禁用 ANSI 颜色
```

输出规则：

- 正式模型回答写入 stdout；
- 生命周期、工具调用、权限和错误写入 stderr；
- 模型文本支持流式输出；
- 工具调用显示名称和参数摘要，不显示完整工具结果；
- `exit`、`quit`、EOF 退出；Ctrl-C 可取消输入或当前请求。
