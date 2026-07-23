import argparse
import sys
from pathlib import Path

from dataclasses import asdict

try:
    import readline
    readline.parse_and_bind("set bind-tty-special-chars off")
    readline.parse_and_bind("set input-meta on")
    readline.parse_and_bind("set output-meta on")
    readline.parse_and_bind("set convert-meta off")
except ImportError:
    pass


COLORS = {
    "reset": "\033[0m",
    "text": "\033[37m",
    "thinking": "\033[90m",
    "status": "\033[32m",
    "tool": "\033[33m",
    "success": "\033[32m",
    "llm": "\033[90m",
    "session": "\033[32m",
    "error": "\033[31m",
    "prompt": "\033[90m",
}
COLOR_OUTPUT = False


def paint(text, kind):
    if not COLOR_OUTPUT:
        return text
    return f"{COLORS[kind]}{text}{COLORS['reset']}"


def write_status(text, kind="status"):
    print(paint(text, kind), file=sys.stderr)

if __package__ in (None, ""):
    _package_root = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(_package_root))
    from simpleCC.config import AppConfig
    from simpleCC.runtime import Runtime
else:
    from .config import AppConfig
    from .runtime import Runtime


def render_event(event):
    event_type = event.get("type") if isinstance(event, dict) else event.type
    if event_type == "text_delta":
        text = event.get("text", "") if isinstance(event, dict) else event.text
        sys.stdout.write(paint(text, "text"))
        sys.stdout.flush()
    elif event_type == "thinking_delta":
        text = event.get("text", "") if isinstance(event, dict) else event.text
        if text:
            write_status(text, "thinking")
    elif event_type == "status":
        message = event.get("message", "") if isinstance(event, dict) else event.message
        kind = "error" if message.startswith(("[permission]", "[warning]")) else "thinking" if message.startswith("[prompt]") else "status"
        write_status(message, kind)
    elif event_type == "tool_call":
        call = event.get("tool_call") if isinstance(event, dict) else event.tool_call
        args = []
        for key, value in call.arguments.items():
            if key in {"content", "token", "password", "secret"}:
                value = "<redacted>"
            args.append(f"{key}={str(value).replace(chr(10), chr(92) + 'n')[:160]}")
        write_status(f"[tool] {call.name}({', '.join(args)})", "tool")
    elif event_type == "llm_start":
        metadata = event.get("metadata", {}) if isinstance(event, dict) else event.metadata
        write_status(f"[llm] turn {metadata.get('turn', '?')}", "status")
    elif event_type == "llm_fallback":
        message = event.get("message", "") if isinstance(event, dict) else event.message
        write_status(f"[llm] {message}", "thinking")
    elif event_type == "session_start":
        write_status("[session] started", "session")
    elif event_type == "session_stop":
        write_status("\n[session] stopped", "session")
    elif event_type == "error":
        message = event.get("message", "") if isinstance(event, dict) else event.message
        write_status(f"[session] error: {message}", "error")


def render_event_quiet(event):
    event_type = event.get("type") if isinstance(event, dict) else event.type
    if event_type == "text_delta":
        text = event.get("text", "") if isinstance(event, dict) else event.text
        sys.stdout.write(paint(text, "text"))
        sys.stdout.flush()
    elif event_type == "thinking_delta":
        text = event.get("text", "") if isinstance(event, dict) else event.text
        if text:
            write_status(text, "thinking")


def build_parser():
    parser = argparse.ArgumentParser(description="simpleCC coding agent")
    parser.add_argument("--workdir", type=Path, help="workspace directory; defaults to simpleCC")
    parser.add_argument("--query", help="run one query and exit")
    parser.add_argument("--quiet", action="store_true", help="hide status output")
    parser.add_argument("--permission-mode", choices=("interactive", "deny", "allow"), help="permission policy")
    parser.add_argument("--no-stream", action="store_true", help="disable streaming output")
    parser.add_argument("--no-color", action="store_true", help="disable colored output")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    global COLOR_OUTPUT
    COLOR_OUTPUT = not args.no_color and sys.stderr.isatty()
    workdir = (args.workdir or Path(__file__).resolve().parent).resolve()
    try:
        config = AppConfig.from_env(workdir)
        if args.permission_mode:
            config = type(config)(**{**asdict(config), "permission_mode": args.permission_mode})
        elif args.query and config.permission_mode == "interactive":
            config = type(config)(**{**asdict(config), "permission_mode": "deny"})
        if args.no_stream:
            config = type(config)(**{**asdict(config), "streaming": False})
        runtime = Runtime.create(config)
    except Exception as exc:
        print(paint(f"启动失败: {type(exc).__name__}: {exc}", "error"), file=sys.stderr)
        return 2
    if not args.quiet:
        write_status(f"simpleCC workspace: {runtime.config.workdir}", "status")
        write_status(f"model: {runtime.config.model}", "status")
    try:
        if args.query:
            try:
                runtime.run(args.query, on_event=render_event if not args.quiet else render_event_quiet)
                print()
                return 0
            except KeyboardInterrupt:
                print(paint("\n已取消当前请求", "error"), file=sys.stderr)
                return 130
            except Exception as exc:
                write_status(f"运行失败: {type(exc).__name__}: {exc}", "error")
                return 1
        while True:
            try:
                query = input("\nsimpleCC> ")
            except EOFError:
                break
            except KeyboardInterrupt:
                print(paint("\n已取消当前输入", "error"), file=sys.stderr)
                continue
            if query.strip().lower() in {"exit", "quit"}:
                break
            if query.strip():
                try:
                    runtime.run(query, on_event=render_event if not args.quiet else render_event_quiet)
                    print()
                except KeyboardInterrupt:
                    print(paint("\n已取消当前请求", "error"), file=sys.stderr)
                except Exception as exc:
                    write_status(f"运行失败: {type(exc).__name__}: {exc}", "error")
    finally:
        runtime.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
