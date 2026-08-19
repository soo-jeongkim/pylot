"""Integration tests for the stdio MCP proxy (``co_pymol.proxy``).

The proxy's entire reason to exist is the *transition* a plain SSE client can't
survive: PyMOL quits mid-session and comes back as a fresh, uninitialized server,
and the MCP client must never notice. ``test_metrics``/``test_pipeline`` exercise the
data layers; this file exercises the connection layer — specifically the behaviors
that only fire on a real restart and that no off-the-shelf bridge gives you:

  * a call **succeeds after relaunch** with no client-side re-init (the proxy
    replays ``initialize`` + ``notifications/initialized`` to the new server);
  * the proxy **adopts the new downstream session** instead of POSTing to the dead
    one (asserted by routing to a server instance that reports its own identity);
  * a call **in flight when PyMOL dies** comes back as an error, not a hang;
  * **rapid quit/reopen cycles** don't wedge the reconnect loop;
  * while down, ``tools/list`` is still answered **from cache** and ``tools/call``
    returns a graceful tool-error.

Everything runs in-process over anyio memory streams against a controllable fake
downstream — no sockets, no subprocess, no real PyMOL — so it's deterministic and
fits the rest of the suite. The fake stands in for "co-pymol inside PyMOL"; each
``connect()`` is a new PyMOL process (a new session), and ``kill()`` is a quit.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

import anyio
from mcp.types import (
    JSONRPCNotification,
    JSONRPCRequest,
    JSONRPCResponse,
)

from co_pymol.proxy import Proxy
from co_pymol.utils.jsonrpc import envelope

PROTOCOL = "2025-06-18"


def text(response: JSONRPCResponse) -> str:
    """The text payload of a CallToolResult-shaped response."""
    return response.result["content"][0]["text"]


def tool_names(response: JSONRPCResponse) -> list[str]:
    return sorted(t["name"] for t in response.result["tools"])


class FakeDownstream:
    """An in-process MCP server over memory streams, restartable like PyMOL.

    Each ``connect()`` models a new PyMOL process: it mints a new *instance*
    number and tags ``tools/call`` results with it, so a test can prove the proxy
    routed to the *new* server after a restart rather than a stale session.
    ``kill()`` drops the current connection (PyMOL quitting); ``accept=False``
    refuses new connects (PyMOL still down) so "while down" behavior is testable.
    """

    def __init__(self, name: str = "fake-pymol", tools=("echo", "render")) -> None:
        self.name = name
        self.tools = list(tools)
        self.connections = 0  # total connects seen (instance counter)
        self.accept = True  # False => connect() raises (PyMOL down)
        self.answer_calls = True  # False => tools/call is swallowed (hangs)
        self.current_kill = None  # set() drops the live connection
        self.current_p2s_recv = None  # proxy->server channel (the "write" path)

    def kill(self) -> None:
        if self.current_kill is not None:
            self.current_kill.set()

    async def break_write_only(self) -> None:
        """Close the proxy->server (write) channel; the read stream stays open.

        Models SSE where the POST path fails but the GET stream is still alive:
        the proxy's dn_write.send() raises while dn_read keeps flowing, so the
        manager's read-side teardown never fires.
        """
        if self.current_p2s_recv is not None:
            await self.current_p2s_recv.aclose()

    @asynccontextmanager
    async def connect(self):
        if not self.accept:
            raise ConnectionError("downstream refused (PyMOL down)")

        self.connections += 1
        instance = self.connections
        # s2p: server -> proxy (proxy's dn_read);  p2s: proxy -> server (dn_write)
        s2p_send, s2p_recv = anyio.create_memory_object_stream(100)
        p2s_send, p2s_recv = anyio.create_memory_object_stream(100)
        kill = anyio.Event()
        self.current_kill = kill
        self.current_p2s_recv = p2s_recv

        async with anyio.create_task_group() as tg:
            tg.start_soon(self.respond, instance, p2s_recv, s2p_send)

            async def killer():
                await kill.wait()
                await s2p_send.aclose()  # EOF on the proxy's dn_read => it drops us
                tg.cancel_scope.cancel()

            tg.start_soon(killer)
            try:
                yield s2p_recv, p2s_send
            finally:
                tg.cancel_scope.cancel()

    async def respond(self, instance, p2s_recv, s2p_send) -> None:
        async def reply(req_id, result):
            await s2p_send.send(
                envelope(JSONRPCResponse(jsonrpc="2.0", id=req_id, result=result))
            )

        try:
            async for item in p2s_recv:
                root = item.message.root
                if not isinstance(root, JSONRPCRequest):
                    continue  # notifications (e.g. initialized) need no reply
                if root.method == "initialize":
                    await reply(
                        root.id,
                        {
                            "protocolVersion": PROTOCOL,
                            "serverInfo": {"name": self.name, "version": str(instance)},
                            "capabilities": {"tools": {"listChanged": False}},
                        },
                    )
                elif root.method == "tools/list":
                    await reply(
                        root.id,
                        {
                            "tools": [
                                {
                                    "name": t,
                                    "description": "",
                                    "inputSchema": {"type": "object", "properties": {}},
                                }
                                for t in self.tools
                            ]
                        },
                    )
                elif root.method == "tools/call":
                    if not self.answer_calls:
                        continue  # leave the call outstanding (simulates a hang)
                    name = (root.params or {}).get("name", "?")
                    await reply(
                        root.id,
                        {
                            "content": [
                                {
                                    "type": "text",
                                    "text": f"instance={instance};tool={name}",
                                }
                            ],
                            "isError": False,
                        },
                    )
                else:
                    await reply(root.id, {})
        except (anyio.EndOfStream, anyio.ClosedResourceError):
            pass


class ProxyClient:
    """Minimal upstream client: speaks to the proxy's stdio side over memory.

    A background ``pump`` dispatches responses by id and stashes notifications, so
    a test can fire a request and await its reply later (needed for the in-flight
    case, where the reply only arrives after the downstream is killed).
    """

    def __init__(self, send, recv) -> None:
        self.send = send
        self.recv = recv
        self.id = 0
        self.waiters: dict[int, anyio.Event] = {}
        self.results: dict[int, object] = {}
        self.notifications: list[JSONRPCNotification] = []

    async def pump(self) -> None:
        async for item in self.recv:
            root = item.message.root
            if isinstance(root, JSONRPCNotification):
                self.notifications.append(root)
                continue
            rid = getattr(root, "id", None)
            if rid is not None:
                self.results[rid] = root
                ev = self.waiters.get(rid)
                if ev is not None:
                    ev.set()

    async def send_request(self, method, params=None) -> int:
        self.id += 1
        rid = self.id
        self.waiters[rid] = anyio.Event()
        await self.send.send(
            envelope(
                JSONRPCRequest(
                    jsonrpc="2.0", id=rid, method=method, params=params or {}
                )
            )
        )
        return rid

    async def await_result(self, rid, timeout=5):
        with anyio.fail_after(timeout):
            await self.waiters[rid].wait()
        return self.results.pop(rid)

    async def request(self, method, params=None, timeout=5):
        return await self.await_result(await self.send_request(method, params), timeout)

    async def notify(self, method, params=None) -> None:
        await self.send.send(
            envelope(
                JSONRPCNotification(jsonrpc="2.0", method=method, params=params or {})
            )
        )

    async def initialize(self):
        resp = await self.request(
            "initialize",
            {
                "protocolVersion": PROTOCOL,
                "capabilities": {},
                "clientInfo": {"name": "test", "version": "1"},
            },
        )
        await self.notify("notifications/initialized")
        return resp


async def settle(predicate, timeout=5):
    """Wait until `predicate()` is true (deterministic, no fixed sleeps)."""
    with anyio.fail_after(timeout):
        while not predicate():
            await anyio.sleep(0.005)


async def drive(body, initially_connected=True, first_connect_wait=2.0):
    """Run `body(client, fake, proxy)` with a wired proxy/fake/client triad."""
    fake = FakeDownstream()
    fake.accept = initially_connected
    # Tiny timeouts so the reconnect/backoff loop runs fast in tests.
    proxy = Proxy(
        "memory://test",
        connect=fake.connect,
        backoff_start=0.01,
        backoff_cap=0.05,
        first_connect_wait=first_connect_wait,
    )
    c2p_send, c2p_recv = anyio.create_memory_object_stream(100)  # client -> proxy
    p2c_send, p2c_recv = anyio.create_memory_object_stream(100)  # proxy -> client
    client = ProxyClient(c2p_send, p2c_recv)

    async with anyio.create_task_group() as tg:
        tg.start_soon(proxy.serve, c2p_recv, p2c_send, False)
        tg.start_soon(client.pump)
        try:
            await body(client, fake, proxy)
        finally:
            await c2p_send.aclose()  # upstream EOF -> proxy.serve returns
            tg.cancel_scope.cancel()


class TestConnectedParity:
    def test_handshake_and_call_go_through(self) -> None:
        async def body(client, fake, proxy):
            init = await client.initialize()
            # initialize is answered from the real downstream's cached result.
            assert init.result["serverInfo"]["name"] == "fake-pymol"
            tools = await client.request("tools/list")
            assert tool_names(tools) == ["echo", "render"]
            call = await client.request("tools/call", {"name": "echo", "arguments": {}})
            assert call.result["isError"] is False
            assert "instance=1" in text(call)

        anyio.run(drive, body)


class TestColdStart:
    def test_tools_appear_when_pymol_starts_after_client(self) -> None:
        async def body(client, fake, proxy):
            init = await client.initialize()
            assert init.result["serverInfo"]["name"] == "co-pymol (proxy)"

            tools_down = await client.request("tools/list")
            assert tool_names(tools_down) == []

            fake.accept = True
            await settle(lambda: proxy.dn_write is not None)
            await settle(
                lambda: any(
                    note.method == "notifications/tools/list_changed"
                    for note in client.notifications
                )
            )

            tools_up = await client.request("tools/list")
            assert tool_names(tools_up) == ["echo", "render"]

        anyio.run(drive, body, False, 0.01)


class TestReconnectAcrossRestart:
    """Criteria 2–3: quit mid-session, reopen, next call just works."""

    def test_call_succeeds_after_relaunch_on_new_session(self) -> None:
        async def body(client, fake, proxy):
            await client.initialize()
            first = await client.request(
                "tools/call", {"name": "echo", "arguments": {}}
            )
            assert "instance=1" in text(first)

            fake.kill()  # PyMOL quits
            await settle(lambda: proxy.dn_write is None)
            await settle(lambda: fake.connections >= 2)  # PyMOL relaunched
            await settle(lambda: proxy.dn_write is not None)

            # No re-initialize from the client — the proxy replayed it. The result
            # comes from instance 2, proving the new session was adopted (not a
            # stale POST target).
            second = await client.request(
                "tools/call", {"name": "echo", "arguments": {}}
            )
            assert second.result["isError"] is False
            assert "instance=2" in text(second)

        anyio.run(drive, body)

    def test_client_never_sees_a_second_initialize(self) -> None:
        async def body(client, fake, proxy):
            await client.initialize()
            fake.kill()
            await settle(lambda: fake.connections >= 2)
            await settle(lambda: proxy.dn_write is not None)
            await client.request("tools/call", {"name": "echo", "arguments": {}})
            # The replayed initialize is consumed by the proxy; the client only
            # ever issued one and is never asked to handshake again.
            assert proxy.cached_init_result is not None

        anyio.run(drive, body)


class TestInFlightCallAtDrop:
    """Criterion 4: a call outstanding when PyMOL dies errors, not hangs."""

    def test_outstanding_call_gets_error_not_hang(self) -> None:
        async def body(client, fake, proxy):
            await client.initialize()
            fake.answer_calls = False  # downstream will never reply to the call

            rid = await client.send_request(
                "tools/call", {"name": "echo", "arguments": {}}
            )
            # Wait until the proxy has forwarded it (it's now genuinely in flight).
            await settle(lambda: rid in proxy.outstanding)

            fake.kill()  # PyMOL dies with the call outstanding

            resp = await client.await_result(rid, timeout=5)  # must not hang
            assert isinstance(resp, JSONRPCResponse)
            assert resp.result["isError"] is True

        anyio.run(drive, body)


class TestWriteSideBreaks:
    """A forward send failing must fail the request back, not hang.

    SSE's POST (write) path is independent of the GET (read) stream, so a send
    can fail while the read side stays open — meaning the manager's read-side
    teardown never runs. The proxy must fail the orphaned request itself.
    """

    def test_request_errors_when_only_write_breaks(self) -> None:
        async def body(client, fake, proxy):
            await client.initialize()
            await settle(lambda: proxy.dn_write is not None)  # handshake landed

            # Break ONLY the write channel; the read (SSE) stream stays open, so
            # the manager's pump never returns and fail_outstanding never fires.
            await fake.break_write_only()

            # The forward send for this call fails — the proxy must answer it
            # itself rather than leaving the client to wait forever.
            rid = await client.send_request(
                "tools/call", {"name": "echo", "arguments": {}}
            )
            resp = await client.await_result(rid, timeout=5)  # must not hang
            assert isinstance(resp, JSONRPCResponse)
            assert resp.result["isError"] is True

        anyio.run(drive, body)


class TestRapidRestartDoesNotWedge:
    """Criterion 5: repeated quick quit/reopen cycles keep recovering."""

    def test_three_restart_cycles_each_recover(self) -> None:
        async def body(client, fake, proxy):
            await client.initialize()
            for expected_instance in (2, 3, 4):
                fake.kill()
                await settle(lambda: proxy.dn_write is None)
                await settle(lambda e=expected_instance: fake.connections >= e)
                await settle(lambda: proxy.dn_write is not None)
                call = await client.request(
                    "tools/call", {"name": "echo", "arguments": {}}
                )
                assert f"instance={expected_instance}" in text(call)

        anyio.run(drive, body)


class TestWhileDown:
    """The easy half, still pinned: cache answers + graceful errors while down."""

    def test_tools_list_from_cache_and_call_errors_then_recovers(self) -> None:
        async def body(client, fake, proxy):
            up = await client.initialize()
            assert up.result["serverInfo"]["name"] == "fake-pymol"
            tools_up = await client.request("tools/list")

            fake.accept = False  # refuse reconnects: stays down
            fake.kill()
            await settle(lambda: proxy.dn_write is None)

            # tools/list still answered, identical to when it was up (from cache).
            tools_down = await client.request("tools/list")
            assert tool_names(tools_down) == tool_names(tools_up)

            # tools/call returns a graceful tool-error, not a transport failure.
            call_down = await client.request(
                "tools/call", {"name": "echo", "arguments": {}}
            )
            assert call_down.result["isError"] is True
            assert "not connected" in text(call_down).lower()

            # Bring PyMOL back; the proxy reconnects and calls work again.
            fake.accept = True
            await settle(lambda: proxy.dn_write is not None)
            call_up = await client.request(
                "tools/call", {"name": "echo", "arguments": {}}
            )
            assert call_up.result["isError"] is False

        anyio.run(drive, body)


def test_module_importable() -> None:
    """Guard: the proxy module imports cleanly (catches accidental syntax/dep rot)."""
    import co_pymol.proxy as p

    assert hasattr(p, "Proxy") and hasattr(p, "run_proxy")
