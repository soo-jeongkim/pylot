"""CLI for co-pymol setup and diagnostics.

Subcommands:
    install-hook       Append the plugin startup line to ~/.pymolrc.py
    install-config     Write Cursor MCP config (global by default)
    proxy              Run the stdio MCP proxy that survives PyMOL restarts

The CLI is pure stdlib — it does not import pymol or mcp — so it can run
under any Python interpreter, even if the plugin itself was installed into
PyMOL's bundled Python.
"""

from __future__ import annotations

import argparse
import json
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


def cmd_proxy(args: argparse.Namespace) -> int:
    # Deferred import: proxy.py pulls in mcp/anyio, which (like pymol) only exist
    # where the package's deps are installed. Importing it lazily here keeps the
    # rest of the CLI (install-hook/install-config) runnable under a stdlib-only
    # Python that just has the package source on its path.
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
    """Add the shared --host/--port options locating the co-pymol SSE server."""
    parser.add_argument(
        "--host",
        default=DEFAULT_HOST,
        help=f"co-pymol SSE host (default: {DEFAULT_HOST})",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_PORT,
        help=f"co-pymol SSE port (default: {DEFAULT_PORT})",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="co-pymol",
        description="co-pymol setup and diagnostics",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_hook = sub.add_parser(
        "install-hook",
        help="Append plugin startup line to ~/.pymolrc.py so PyMOL loads it on launch",
        description=(
            "Append a one-liner to ~/.pymolrc.py that starts the MCP server when "
            "PyMOL launches. Safe to re-run — does nothing if the line is already "
            "present."
        ),
    )
    p_hook.set_defaults(func=cmd_install_hook)

    p_install = sub.add_parser(
        "install-config",
        help="Write Cursor MCP config so the pymol server is available everywhere",
        description=(
            "Write Cursor MCP config (~/.cursor/mcp.json by default; --project "
            "writes ./.cursor/mcp.json). The pymol entry launches the "
            "restart-surviving stdio proxy by default; use --sse for the direct "
            "SSE url form. Other mcpServers entries are preserved."
        ),
    )
    p_install.add_argument(
        "--project",
        action="store_true",
        help="Write project-level config (./.cursor/mcp.json) instead of global",
    )
    p_install.add_argument(
        "--project-dir",
        default=".",
        help="Project root for --project (default: current directory)",
    )
    p_install.add_argument(
        "--sse",
        action="store_true",
        help="Write the direct SSE url entry instead of the restart-surviving proxy",
    )
    add_server_opts(p_install)
    p_install.set_defaults(func=cmd_install_config)

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


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        # Subcommands return an exit code (proxy) or None (setup commands).
        return args.func(args) or 0
    except (OSError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
