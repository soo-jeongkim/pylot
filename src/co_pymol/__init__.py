"""PyMOL plugin entry point for co-pymol.

Exposes PyMOL's Python API as an MCP server so any MCP client
(Codex, Claude Code, Cursor, etc.) can drive PyMOL.
"""

from __future__ import annotations

import socket
import threading
from types import SimpleNamespace

from co_pymol.cli import server_url
from co_pymol.constants import DEFAULT_HOST, DEFAULT_PORT

state = SimpleNamespace(thread=None)


def __init_plugin__(app=None):
    """Called by PyMOL's plugin system on startup."""
    from pymol import cmd

    cmd.extend("start_mcp", start_mcp)
    start_mcp()


def start_mcp(port: int | str = DEFAULT_PORT, host: str = DEFAULT_HOST):
    """Start the MCP server in a background thread.

    Can be called from PyMOL command line: start_mcp [port [host]]
    """
    # `cmd.extend` exposes start_mcp as a PyMOL command, so a user can re-run it
    # from the command line after __init_plugin__ already auto-started it. This
    # guards the same-process re-run; a *different* PyMOL is caught by the bind below.
    if state.thread is not None and state.thread.is_alive():
        print("co-pymol: MCP server is already running")
        return

    # PyMOL-specific constraint: mcp/uvicorn (like pymol) only exist inside PyMOL's
    # bundled interpreter, so these imports are function-local — at module level
    # they'd break `import co_pymol` under the plain Python the stdlib-only CLI uses.
    import uvicorn

    from co_pymol.server import create_server

    # PyMOL's command extension passes all args as strings (`start_mcp 9000`).
    port = int(port)

    # Bind the listening socket ourselves, synchronously, so the bind *is* the
    # check: if another process (e.g. a second PyMOL) already holds host:port, this
    # raises EADDRINUSE here — no probe-then-bind race — and we report it before
    # spawning the thread. This catches the bind collision specifically; serve-time
    # failures after start() can still die quietly on the daemon thread. bind() is
    # the detector; listen() just hardens the serve path rather than relying on
    # uvicorn/asyncio to call it (idempotent, so a later listen() downstream is a
    # no-op).
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind((host, port))
        sock.listen()
    except OSError as err:
        sock.close()
        print(
            f"co-pymol: can't bind {host}:{port} ({err}). Another PyMOL/co-pymol may "
            f"already be running there — try `start_mcp <port>` with a free port."
        )
        return

    # We own `sock` until uvicorn adopts it in the serve loop; if anything between
    # here and start() raises, close it explicitly rather than leaving the fd for
    # __del__ to reclaim whenever.
    try:
        server = create_server(host=host, port=port, log_level="WARNING")
        uv_server = uvicorn.Server(
            uvicorn.Config(server.sse_app(), log_level="warning")
        )
        state.thread = threading.Thread(
            target=lambda: uv_server.run(sockets=[sock]),
            daemon=True,
        )
        state.thread.start()
    except Exception:
        sock.close()
        raise

    print(f"co-pymol: MCP server running on {server_url(host, port)}")
