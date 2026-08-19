"""Stdio MCP proxy that outlives PyMOL.

Run via the CLI: ``co-pymol proxy`` (see ``cli.py``). An MCP client launches it as
a subprocess and speaks the MCP **stdio** transport to it. Downstream, the proxy
is an MCP **SSE** client of the co-pymol server running inside PyMOL (default
http://127.0.0.1:8766/sse).

    MCP client  <-[stdio]->  proxy  <-[SSE :8766]->  co-pymol (in PyMOL)

The point is to decouple the client's connection lifetime from PyMOL's process
lifetime. A stdio server has no socket for the client to give up on, so PyMOL
quitting/restarting never trips the client's connection-retry backoff. The proxy
absorbs the downstream drop and reconnects on its own loop with no deadline.

What it does that an off-the-shelf bridge does not:
  * Caches the downstream ``initialize`` result and ``tools/list`` response on
    first connect, and answers the client from cache even while PyMOL is down —
    so the server keeps *appearing* healthy across a restart.
  * On downstream reconnect, **replays** ``initialize`` +
    ``notifications/initialized`` to the fresh PyMOL server before resuming.
  * While PyMOL is down, a ``tools/call`` returns a clean tool-error result
    ("PyMOL is not connected") rather than hanging or surfacing a transport error.
  * An in-flight ``tools/call`` whose PyMOL dies mid-call gets that same error
    instead of hanging forever.

This module imports ``mcp``/``anyio`` at the top level, so it must only be
imported in an environment where the package's dependencies are installed (the
CLI defers importing it into the ``proxy`` subcommand handler for exactly this
reason — keeping ``co-pymol install-hook`` runnable under a stdlib-only Python).
stdout is the protocol channel — keep it clean. All logging goes to stderr.
"""

from __future__ import annotations

import os
import sys
import threading
import traceback
from importlib.metadata import PackageNotFoundError, version

import anyio
from mcp.client.sse import sse_client
from mcp.server.stdio import stdio_server
from mcp.shared.message import SessionMessage
from mcp.types import (
    JSONRPCError,
    JSONRPCMessage,
    JSONRPCNotification,
    JSONRPCRequest,
    JSONRPCResponse,
)

from co_pymol.cli import server_url
from co_pymol.constants import (
    DEFAULT_HOST,
    DEFAULT_PORT,
    PROXY_BACKOFF_CAP,
    PROXY_BACKOFF_START,
    PROXY_FIRST_CONNECT_WAIT,
)
from co_pymol.utils.jsonrpc import (
    empty_response,
    envelope,
    rpc_error_response,
    rpc_id,
    tool_error_response,
)

try:  # pin the negotiated protocol version to whatever this SDK ships
    from mcp.types import LATEST_PROTOCOL_VERSION as DEFAULT_PROTOCOL
except Exception:  # pragma: no cover - defensive
    DEFAULT_PROTOCOL = "2025-06-18"

try:
    VERSION = version("co-pymol")
except PackageNotFoundError:  # pragma: no cover - running from a source checkout
    VERSION = "0"

# One user-facing phrasing for the downstream-unavailable condition, so every
# error the client sees for "PyMOL isn't there" reads the same.
PYMOL_NOT_CONNECTED = "PyMOL is not connected"


def log(msg: str) -> None:
    """Write a line to stderr (stdout is the protocol channel). Never raises."""
    try:
        sys.stderr.write(f"[pymol-proxy] {msg}\n")
        sys.stderr.flush()
    except Exception:
        pass


class Proxy:
    def __init__(
        self,
        url: str,
        connect=None,
        *,
        backoff_start: float = PROXY_BACKOFF_START,
        backoff_cap: float = PROXY_BACKOFF_CAP,
        first_connect_wait: float = PROXY_FIRST_CONNECT_WAIT,
    ) -> None:
        self.url = url

        # Downstream connection factory: a zero-arg callable returning an async
        # context manager that yields (read, write) streams. Defaults to a fresh
        # SSE client per call — so every reconnect gets a *new* downstream session
        # (the proxy never reuses a stale POST target). Injectable so tests can
        # drive a controllable fake without sockets. The timeouts are likewise
        # parameterised (defaulting to the module constants) so tests can run the
        # reconnect/backoff loop fast.
        self.connect = connect or (lambda: sse_client(self.url))
        self.backoff_start = backoff_start
        self.backoff_cap = backoff_cap
        self.first_connect_wait = first_connect_wait

        # Upstream (toward the MCP client) write stream — set once stdio is up.
        self.up_write = None

        # Downstream (toward PyMOL) write stream — present only while connected.
        self.dn_write = None

        # Cached downstream handshake artifacts. Tools are static, so once captured
        # they answer the MCP client forever, including while PyMOL is down.
        self.cached_init_result: dict | None = None
        self.cached_tools_result: dict | None = None
        self.tools_signature: str | None = None
        # True after a cold-start tools/list had to return an empty list. The first
        # real downstream handshake must then notify the client to fetch again.
        self.sent_empty_tools = False

        # Set the first time the cache is populated; lets initialize/tools/list
        # block briefly on a cold start until the first connect lands.
        self.cache_ready = anyio.Event()

        # Protocol version the *client* negotiated, reused when we (re)handshake
        # downstream so the fresh PyMOL server agrees with the MCP client.
        self.client_protocol = DEFAULT_PROTOCOL

        # Client request ids forwarded downstream and not yet answered. On a
        # downstream drop these are failed back so nothing hangs.
        self.outstanding: dict[object, str] = {}

        self.internal_counter = 0

    # -- upstream send helpers ------------------------------------------------
    async def send_up(self, msg: JSONRPCMessage) -> None:
        if self.up_write is None:
            return
        try:
            await self.up_write.send(SessionMessage(message=msg))
        except Exception as exc:  # MCP client went away mid-write
            log(f"upstream send failed: {exc!r}")

    # -- lifecycle ------------------------------------------------------------
    async def run(self) -> None:
        async with stdio_server() as (up_read, up_write):
            await self.serve(up_read, up_write, arm_watchdog=True)

    async def serve(self, up_read, up_write, arm_watchdog: bool = True) -> None:
        """Drive the proxy over already-open upstream streams until they close.

        Split out from ``run`` so tests can feed in-memory streams; production
        wraps it with the real ``stdio_server`` transport.
        """
        self.up_write = up_write
        async with anyio.create_task_group() as tg:
            tg.start_soon(self.downstream_manager)
            # Upstream loop owns the lifetime: when stdin closes, the MCP client is
            # gone, so tear everything down.
            await self.upstream_loop(up_read)
            if arm_watchdog:
                # Watchdog: guarantee the process dies promptly on stdin EOF even
                # if anyio teardown wedges unwinding the reconnect loop (its sleep
                # /finally awaits can delay cancellation). The OS reclaims sockets.
                # Off in tests — os._exit would kill the test runner.
                arm_exit_watchdog(2.0)
            tg.cancel_scope.cancel()

    # -- downstream connection management ------------------------------------
    async def downstream_manager(self) -> None:
        backoff = self.backoff_start
        while True:
            try:
                async with self.connect() as (dn_read, dn_write):
                    await self.handshake(dn_read, dn_write)
                    self.dn_write = dn_write
                    backoff = self.backoff_start
                    log("downstream connected")
                    await self.downstream_pump(dn_read)
                    log("downstream stream ended")
            except anyio.get_cancelled_exc_class():
                raise
            except Exception as exc:
                log(f"downstream connect/serve error: {exc!r}")
            finally:
                self.dn_write = None
                await self.fail_outstanding()
            await anyio.sleep(backoff)
            backoff = min(backoff * 2, self.backoff_cap)

    async def handshake(self, dn_read, dn_write) -> None:
        """Initialize the (fresh) downstream server, then capture tools/list.

        Runs before the general pump starts, reading dn_read directly for the two
        responses we care about and forwarding any interleaved notifications.
        """
        init_id = self.next_internal_id()
        init_params = {
            "protocolVersion": self.client_protocol,
            "capabilities": {},
            "clientInfo": {"name": "co-pymol-proxy", "version": VERSION},
        }
        await dn_write.send(
            envelope(
                JSONRPCRequest(
                    jsonrpc="2.0", id=init_id, method="initialize", params=init_params
                )
            )
        )
        init_result = await self.read_until_response(dn_read, init_id)

        await dn_write.send(
            envelope(
                JSONRPCNotification(jsonrpc="2.0", method="notifications/initialized")
            )
        )

        tools_id = self.next_internal_id()
        await dn_write.send(
            envelope(
                JSONRPCRequest(
                    jsonrpc="2.0", id=tools_id, method="tools/list", params={}
                )
            )
        )
        tools_result = await self.read_until_response(dn_read, tools_id)

        self.cached_init_result = init_result
        changed = self.update_tools_cache(tools_result)
        if not self.cache_ready.is_set():
            self.cache_ready.set()
        notify_client = changed or self.sent_empty_tools
        self.sent_empty_tools = False
        if notify_client:
            # The tool set changed, or a cold-start request saw no tools before
            # PyMOL connected. Tell the MCP client to fetch the real list.
            log("tools/list is now available or changed; notifying client")
            await self.send_up(
                JSONRPCMessage(
                    JSONRPCNotification(
                        jsonrpc="2.0", method="notifications/tools/list_changed"
                    )
                )
            )

    def update_tools_cache(self, tools_result: dict) -> bool:
        """Cache the tools list; return True if it changed from a prior connect."""
        names = sorted(t.get("name", "") for t in tools_result.get("tools", []))
        signature = "\n".join(names)
        changed = self.tools_signature is not None and signature != self.tools_signature
        self.cached_tools_result = tools_result
        self.tools_signature = signature
        return changed

    async def read_until_response(self, dn_read, want_id) -> dict:
        """Read downstream messages until the response to `want_id`.

        Notifications seen along the way are forwarded upstream; other responses
        are ignored. Raises on stream end or a JSON-RPC error for `want_id`.
        """
        async for item in dn_read:
            if isinstance(item, Exception):
                raise item
            root = item.message.root
            if isinstance(root, JSONRPCResponse) and root.id == want_id:
                return root.result
            if isinstance(root, JSONRPCError) and root.id == want_id:
                raise RuntimeError(f"downstream error during handshake: {root.error}")
            if isinstance(root, JSONRPCNotification):
                await self.send_up(item.message)
            # else: a response/request we did not ask for during handshake — drop
        raise RuntimeError("downstream stream ended during handshake")

    async def downstream_pump(self, dn_read) -> None:
        """Forward everything from PyMOL to the MCP client until the stream ends."""
        async for item in dn_read:
            if isinstance(item, Exception):
                log(f"downstream read error: {item!r}")
                return
            root = item.message.root
            rid = rpc_id(root)
            if isinstance(root, (JSONRPCResponse, JSONRPCError)):
                self.outstanding.pop(rid, None)
            await self.send_up(item.message)

    async def fail_one(self, req_id) -> None:
        """Fail a single outstanding request back upstream; no-op if already gone.

        Pops before the first await so concurrent callers (the send path and the
        manager's teardown) can't both answer the same id.
        """
        method = self.outstanding.pop(req_id, None)
        if method is None:
            return
        if method == "tools/call":
            await self.send_up(tool_error_response(req_id, PYMOL_NOT_CONNECTED))
        else:
            await self.send_up(rpc_error_response(req_id, PYMOL_NOT_CONNECTED))

    async def fail_outstanding(self) -> None:
        for req_id in list(self.outstanding):
            await self.fail_one(req_id)

    # -- upstream request handling -------------------------------------------
    async def upstream_loop(self, up_read) -> None:
        async for item in up_read:
            try:
                if isinstance(item, Exception):
                    log(f"upstream read error: {item!r}")
                    continue
                root = item.message.root
                if isinstance(root, JSONRPCRequest):
                    await self.handle_request(item, root)
                elif isinstance(root, JSONRPCNotification):
                    await self.handle_notification(item, root)
                else:
                    # A response from the client to a server-initiated request.
                    if self.dn_write is not None:
                        await self.forward_down(item)
            except anyio.get_cancelled_exc_class():
                raise
            except Exception:
                # Crash-proofing: one bad message must never kill the proxy.
                log("error handling upstream message:\n" + traceback.format_exc())
        log("stdin closed; shutting down")

    async def handle_request(self, item: SessionMessage, root: JSONRPCRequest) -> None:
        method = root.method
        req_id = root.id

        if method == "initialize":
            await self.handle_initialize(req_id, root.params or {})
            return

        if method == "ping":
            # Answer locally so client health checks pass even when PyMOL is down.
            await self.send_up(empty_response(req_id))
            return

        if method == "tools/list":
            await self.handle_tools_list(req_id)
            return

        if method == "tools/call":
            name = (root.params or {}).get("name", "?")
            await self.forward_or_fail(
                item,
                req_id,
                method,
                tool_error_response(
                    req_id,
                    f"{PYMOL_NOT_CONNECTED} — cannot run tool '{name}'. "
                    "Open/restart PyMOL and try again.",
                ),
            )
            return

        # Anything else (resources/*, prompts/*, completion/*, ...).
        await self.forward_or_fail(
            item, req_id, method, rpc_error_response(req_id, PYMOL_NOT_CONNECTED)
        )

    async def forward_or_fail(
        self, item: SessionMessage, req_id, method: str, down_response: JSONRPCMessage
    ) -> None:
        """Forward a request downstream (tracking it), else answer down_response."""
        if self.dn_write is not None:
            self.outstanding[req_id] = method
            await self.forward_down(item)
        else:
            await self.send_up(down_response)

    async def handle_initialize(self, req_id, params: dict) -> None:
        # Remember the client's protocol version for downstream (re)handshakes.
        proto = params.get("protocolVersion")
        if isinstance(proto, str) and proto:
            self.client_protocol = proto

        await self.await_cache(self.first_connect_wait)

        if self.cached_init_result is not None:
            # Pass through PyMOL's actual negotiated protocolVersion rather than
            # echoing the client's request — we're standing in for that server, so
            # we shouldn't claim a version it doesn't speak. In practice both ends
            # ship the same SDK, so these match.
            result = dict(self.cached_init_result)
        else:
            # Synthesized fallback: PyMOL wasn't up in time, so echo the client's
            # version. Advertise listChanged so we can nudge the client to refetch
            # tools once PyMOL appears.
            result = {
                "protocolVersion": self.client_protocol,
                "serverInfo": {"name": "co-pymol (proxy)", "version": VERSION},
                "capabilities": {"tools": {"listChanged": True}},
                "instructions": (
                    "PyMOL is not connected yet; tools appear once it starts."
                ),
            }
        caps = result.setdefault("capabilities", {})
        tools_cap = caps.setdefault("tools", {})
        if isinstance(tools_cap, dict):
            tools_cap["listChanged"] = True
        await self.send_up(
            JSONRPCMessage(JSONRPCResponse(jsonrpc="2.0", id=req_id, result=result))
        )

    async def handle_tools_list(self, req_id) -> None:
        await self.await_cache(self.first_connect_wait)
        if self.cached_tools_result is None:
            self.sent_empty_tools = True
            result = {"tools": []}
        else:
            result = self.cached_tools_result
        await self.send_up(
            JSONRPCMessage(JSONRPCResponse(jsonrpc="2.0", id=req_id, result=result))
        )

    async def handle_notification(
        self, item: SessionMessage, root: JSONRPCNotification
    ) -> None:
        method = root.method
        if method == "notifications/initialized":
            # The proxy already initialized its own downstream session; do not
            # double-send to PyMOL.
            return
        # Cancellations, progress acks, etc. — best-effort forward when connected.
        if self.dn_write is not None:
            await self.forward_down(item)

    async def forward_down(self, item: SessionMessage) -> None:
        rid = rpc_id(item.message.root)
        dn_write = self.dn_write
        if dn_write is None:
            # Lost the connection between registering and forwarding; fail it now.
            if rid is not None:
                await self.fail_one(rid)
            return
        try:
            await dn_write.send(item)
        except Exception as exc:
            # SSE's POST (write) path is independent of the GET (read) stream, so a
            # send can fail while the read side stays open — in which case the
            # manager's fail_outstanding (which only runs after the read ends)
            # would never fire for this request. Fail it back immediately rather
            # than relying on the read side; mark disconnected so later requests
            # take the clean down-path instead of orphaning too.
            log(f"downstream send failed: {exc!r}")
            self.dn_write = None
            if rid is not None:
                await self.fail_one(rid)

    async def await_cache(self, timeout: float) -> None:
        if self.cache_ready.is_set():
            return
        with anyio.move_on_after(timeout):
            await self.cache_ready.wait()

    def next_internal_id(self) -> str:
        self.internal_counter += 1
        return f"proxy-{self.internal_counter}"


def arm_exit_watchdog(seconds: float) -> None:
    """Force a hard process exit after `seconds` as a teardown backstop."""

    def _boom() -> None:
        log("watchdog: forcing exit")
        os._exit(0)

    t = threading.Timer(seconds, _boom)
    t.daemon = True
    t.start()


def run_proxy(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> int:
    """Run the proxy until stdin closes. Returns a process exit code.

    The downstream URL defaults to the co-pymol SSE endpoint for host:port, but
    PYMOL_PROXY_URL overrides it (e.g. to point at a non-default path).
    """
    url = os.environ.get("PYMOL_PROXY_URL") or server_url(host, port)

    async def _amain() -> None:
        log(f"starting; downstream = {url}")
        await Proxy(url).run()

    try:
        anyio.run(_amain)
    except KeyboardInterrupt:
        pass
    except Exception:
        log("fatal:\n" + traceback.format_exc())
        return 1
    return 0
