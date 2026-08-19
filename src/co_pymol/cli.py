"""CLI for co-pymol setup and diagnostics.

Subcommands:
    setup <client>     Install the PyMOL hook and configure one MCP client
    proxy              Run the stdio MCP proxy that survives PyMOL restarts

The pre-setup ``install-hook`` and ``install-config`` commands are still
accepted as hidden compatibility aliases, but are intentionally omitted from
the public help. ``install-codex`` was never released and is not retained.

The CLI is pure stdlib — it does not import pymol or mcp — so it can run
under any Python interpreter, even if the plugin itself was installed into
PyMOL's bundled Python.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

from co_pymol.constants import DEFAULT_HOST, DEFAULT_PORT

PYMOLRC_SENTINEL = "# co-pymol: auto-start MCP server on PyMOL launch"
PYMOLRC_LINE = "from co_pymol import __init_plugin__; __init_plugin__()"


def server_url(host: str, port: int) -> str:
    """The SSE endpoint a client connects to for a server at host:port."""
    return f"http://{host}:{port}/sse"


def load_config(path: Path) -> dict:
    """Read a JSON object from `path`, or {} if it's missing or empty."""
    text = path.read_text() if path.exists() else ""
    if not text.strip():
        return {}
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a JSON object at the top level.")
    return data


def pymol_server_entry(host: str, port: int, use_sse: bool) -> dict:
    """The `pymol` mcpServers entry: the restart-surviving proxy, or direct SSE.

    The proxy entry launches `-m co_pymol proxy` under *this* interpreter
    (`sys.executable`) — the one co-pymol and its deps are installed in, which is
    exactly what the proxy needs. This routes through the same CLI as the
    `co-pymol proxy` command. host/port are only emitted when non-default.
    """
    if use_sse:
        return {"url": server_url(host, port)}

    args = ["-m", "co_pymol", "proxy"]
    if host != DEFAULT_HOST:
        args += ["--host", host]
    if port != DEFAULT_PORT:
        args += ["--port", str(port)]
    return {"command": sys.executable, "args": args}


def write_mcp_config(path: Path, host: str, port: int, use_sse: bool = False) -> str:
    """Merge a `pymol` entry into mcpServers, preserving other servers.

    Writes the stdio proxy entry by default (survives PyMOL restarts); `use_sse`
    writes the direct SSE url form instead.
    """
    data = load_config(path)
    servers = data.setdefault("mcpServers", {})
    if not isinstance(servers, dict):
        raise ValueError(f"'mcpServers' in {path} must be an object.")

    kind = "SSE" if use_sse else "proxy"
    desired = pymol_server_entry(host, port, use_sse)
    existing = servers.get("pymol")
    if existing == desired:
        return f"Already configured: {path} -> pymol ({kind})"

    servers["pymol"] = desired
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n")

    action = "Updated" if existing is not None else "Wrote"
    return f"{action} {path} -> pymol ({kind})"


def write_pymolrc_hook(path: Path) -> str:
    """Append the plugin startup line to ~/.pymolrc.py if not already present."""
    existing = path.read_text() if path.exists() else ""
    if PYMOLRC_LINE in existing:
        return f"Already configured: {path}"

    prefix = "" if not existing else "\n" if existing.endswith("\n") else "\n\n"
    snippet = f"{prefix}{PYMOLRC_SENTINEL}\n{PYMOLRC_LINE}\n"

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        f.write(snippet)

    action = "Appended to" if existing else "Wrote"
    return f"{action} {path}. Restart PyMOL to load the plugin."


def cmd_install_hook(args: argparse.Namespace) -> None:
    print(write_pymolrc_hook(Path.home() / ".pymolrc.py"))


def install_codex_mcp(host: str, port: int) -> str:
    """Register the restart-surviving proxy as Codex's global `pymol` MCP.

    Codex owns its TOML configuration format, so use its supported CLI instead
    of editing ``~/.codex/config.toml`` directly. ``codex mcp add`` is an upsert,
    which makes this safe to re-run when the interpreter, host, or port changes.
    """
    codex = shutil.which("codex")
    if codex is None:
        raise OSError(
            "Codex CLI was not found on PATH. Install Codex first, then re-run "
            "`co-pymol setup codex`."
        )

    entry = pymol_server_entry(host, port, use_sse=False)
    command = [
        codex,
        "mcp",
        "add",
        "pymol",
        "--",
        entry["command"],
        *entry["args"],
    ]
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        suffix = f": {detail}" if detail else ""
        raise OSError(f"`codex mcp add` failed{suffix}")

    return result.stdout.strip() or "Configured Codex MCP server 'pymol'."


def install_claude_mcp(host: str, port: int) -> str:
    """Register the restart-surviving proxy as Claude Code's user MCP."""
    claude = shutil.which("claude")
    if claude is None:
        raise OSError(
            "Claude Code CLI was not found on PATH. Install Claude Code first, "
            "then re-run `co-pymol setup claude`."
        )

    entry = pymol_server_entry(host, port, use_sse=False)
    command = [
        claude,
        "mcp",
        "add",
        "--scope",
        "user",
        "pymol",
        "--",
        entry["command"],
        *entry["args"],
    ]
    result = subprocess.run(command, capture_output=True, text=True)

    # Unlike `codex mcp add`, Claude Code does not update an existing entry.
    # Remove only its user-scoped `pymol` entry and retry; other clients and
    # other Claude MCP servers are untouched.
    detail = (result.stderr or result.stdout).strip()
    if result.returncode != 0 and "already exists" in detail.lower():
        remove = subprocess.run(
            [claude, "mcp", "remove", "pymol", "--scope", "user"],
            capture_output=True,
            text=True,
        )
        if remove.returncode != 0:
            remove_detail = (remove.stderr or remove.stdout).strip()
            suffix = f": {remove_detail}" if remove_detail else ""
            raise OSError(f"`claude mcp remove` failed{suffix}")
        result = subprocess.run(command, capture_output=True, text=True)
        detail = (result.stderr or result.stdout).strip()

    if result.returncode != 0:
        suffix = f": {detail}" if detail else ""
        raise OSError(f"`claude mcp add` failed{suffix}")

    return result.stdout.strip() or "Configured Claude Code MCP server 'pymol'."


def install_cursor_mcp(host: str, port: int) -> str:
    """Register the restart-surviving proxy in Cursor's global MCP config."""
    return write_mcp_config(
        Path.home() / ".cursor" / "mcp.json", host, port, use_sse=False
    )


def cmd_setup(args: argparse.Namespace) -> None:
    """Install the shared PyMOL hook and configure only the chosen client."""
    if args.client == "codex":
        # Preflight before changing ~/.pymolrc.py so a missing selected client
        # fails without leaving a partial setup. Other clients are not checked.
        if shutil.which("codex") is None:
            raise OSError(
                "Codex CLI was not found on PATH. Install Codex first, then "
                "re-run `co-pymol setup codex`."
            )
        configure = install_codex_mcp
        restart = "Start a new Codex session"
    elif args.client == "claude":
        if shutil.which("claude") is None:
            raise OSError(
                "Claude Code CLI was not found on PATH. Install Claude Code "
                "first, then re-run `co-pymol setup claude`."
            )
        configure = install_claude_mcp
        restart = "Start a new Claude Code session"
    else:
        configure = install_cursor_mcp
        restart = "Fully quit and reopen Cursor"

    print(write_pymolrc_hook(Path.home() / ".pymolrc.py"))
    print(configure(args.host, args.port))
    print(f"Restart PyMOL. {restart} to load the pymol tools.")


def cmd_proxy(args: argparse.Namespace) -> int:
    # Deferred import: proxy.py pulls in mcp/anyio, which (like pymol) only exist
    # where the package's deps are installed. Importing it lazily here keeps the
    # setup/install helpers runnable under a stdlib-only Python that just has
    # the package source on its path.
    from co_pymol.proxy import run_proxy

    return run_proxy(args.host, args.port)


def cmd_install_config(args: argparse.Namespace) -> None:
    if args.project:
        target = Path(args.project_dir).resolve() / ".cursor" / "mcp.json"
    else:
        target = Path.home() / ".cursor" / "mcp.json"

    print(write_mcp_config(target, args.host, args.port, use_sse=args.sse))
    if not args.project:
        print("Restart Cursor to pick up the change.")


def add_server_opts(parser: argparse.ArgumentParser) -> None:
    """Add client-side options locating an existing co-pymol SSE server."""
    parser.add_argument(
        "--host",
        default=DEFAULT_HOST,
        help=(
            "SSE host the configured client or proxy connects to; does not "
            "change PyMOL's "
            f"automatic bind (default: {DEFAULT_HOST})"
        ),
    )
    parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_PORT,
        help=(
            "SSE port the configured client or proxy connects to; does not "
            "change PyMOL's "
            f"automatic bind (default: {DEFAULT_PORT})"
        ),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="co-pymol",
        description="co-pymol setup and diagnostics",
    )
    sub = parser.add_subparsers(dest="cmd", required=True, metavar="{setup,proxy}")

    p_setup = sub.add_parser(
        "setup",
        help="Install the PyMOL hook and configure one MCP client",
        description=(
            "Set up co-pymol for exactly one client. This installs the shared "
            "PyMOL startup hook and configures only the selected client; other "
            "clients do not need to be installed."
        ),
    )
    setup_clients = p_setup.add_subparsers(dest="client", required=True)
    for client, label in (
        ("codex", "Codex"),
        ("cursor", "Cursor"),
        ("claude", "Claude Code"),
    ):
        p_client = setup_clients.add_parser(
            client,
            help=f"Install the PyMOL hook and configure {label}",
        )
        add_server_opts(p_client)
        p_client.set_defaults(func=cmd_setup)

    p_proxy = sub.add_parser(
        "proxy",
        help="Run the stdio MCP proxy that survives PyMOL restarts",
        description=(
            "Run a stdio MCP proxy in the foreground. An MCP client launches this "
            "as a subprocess; it forwards to the co-pymol SSE server "
            "in PyMOL and survives PyMOL quitting/restarting so the client's "
            "connection never drops. Configure the client's stdio MCP entry to "
            "run `co-pymol proxy`."
        ),
    )
    add_server_opts(p_proxy)
    p_proxy.set_defaults(func=cmd_proxy)

    return parser


def build_legacy_parser(command: str) -> argparse.ArgumentParser:
    """Build a parser for a pre-setup compatibility command.

    These commands remain callable by old scripts but stay out of the public
    parser, so ``co-pymol --help`` presents only the supported setup interface.
    """
    parser = argparse.ArgumentParser(prog=f"co-pymol {command}")
    if command == "install-hook":
        parser.description = (
            "Compatibility command: append the PyMOL startup hook. New installs "
            "should use `co-pymol setup <client>`."
        )
        parser.set_defaults(func=cmd_install_hook)
        return parser

    if command != "install-config":  # defensive; main filters this already
        raise ValueError(f"Unknown compatibility command: {command}")

    parser.description = (
        "Compatibility command: write Cursor MCP configuration. New installs "
        "should use `co-pymol setup cursor`."
    )
    parser.add_argument(
        "--project",
        action="store_true",
        help="Write project-level config (./.cursor/mcp.json) instead of global",
    )
    parser.add_argument(
        "--project-dir",
        default=".",
        help="Project root for --project (default: current directory)",
    )
    parser.add_argument(
        "--sse",
        action="store_true",
        help="Write the direct SSE url entry instead of the restart-surviving proxy",
    )
    add_server_opts(parser)
    parser.set_defaults(func=cmd_install_config)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    if raw_argv and raw_argv[0] in {"install-hook", "install-config"}:
        command = raw_argv.pop(0)
        args = build_legacy_parser(command).parse_args(raw_argv)
    else:
        args = build_parser().parse_args(raw_argv)
    try:
        # Subcommands return an exit code (proxy) or None (setup commands).
        return args.func(args) or 0
    except (OSError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
