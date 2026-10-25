"""ODS Talk keeps the same Hermes conversation across reconnects (#8137).

These tests run the real bridge (token fetch, WebSocket connect, JSON-RPC)
against a small fake Hermes gateway served by aiohttp. The fake mirrors the
pinned upstream contract: ``session.create`` returns a runtime ``session_id``
plus the durable ``stored_session_id``; ``session.resume`` takes the stored
key and returns a NEW runtime ``session_id``; ``prompt.submit`` is addressed
by runtime id. Each reply quotes the conversation's stored key and every
prompt it has seen, so a turn that lands in the wrong conversation is visible.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from aiohttp import web

import hermes_bridge

TOKEN = "fake-hermes-token"


class FakeHermes:
    def __init__(self) -> None:
        self.rpcs: list[tuple[str, dict]] = []
        self.history: dict[str, list[str]] = {}
        self.runtime_to_stored: dict[str, str] = {}
        self.sockets: list[web.WebSocketResponse] = []
        self.resume_mode = "ok"
        self.drop_on_submit = False
        self._ids = 0
        self.url = ""
        self._runner: web.AppRunner | None = None

    def methods(self) -> list[str]:
        return [method for method, _params in self.rpcs]

    def params_for(self, method: str) -> list[dict]:
        return [params for name, params in self.rpcs if name == method]

    def _runtime_id(self, stored: str) -> str:
        self._ids += 1
        runtime = f"rt-{self._ids}"
        self.runtime_to_stored[runtime] = stored
        return runtime

    async def _index(self, _request: web.Request) -> web.Response:
        return web.Response(
            text=f'<script>window.__HERMES_SESSION_TOKEN__ = "{TOKEN}";</script>',
            content_type="text/html",
        )

    async def _ws(self, request: web.Request) -> web.WebSocketResponse:
        assert request.query.get("token") == TOKEN
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self.sockets.append(ws)
        await ws.send_json({"jsonrpc": "2.0", "method": "event", "params": {"type": "gateway.ready"}})
        async for msg in ws:
            frame = json.loads(msg.data)
            await self._handle(ws, frame["id"], frame["method"], frame.get("params") or {})
        return ws

    async def _handle(self, ws, rid, method, params) -> None:
        self.rpcs.append((method, params))
        if method == "session.create":
            self._ids += 1
            stored = f"stored-{self._ids}"
            self.history[stored] = []
            runtime = self._runtime_id(stored)
            await ws.send_json({"jsonrpc": "2.0", "id": rid, "result": {
                "session_id": runtime, "stored_session_id": stored, "message_count": 0, "messages": [],
            }})
        elif method == "session.resume":
            await self._resume(ws, rid, params)
        elif method == "prompt.submit":
            stored = self.runtime_to_stored.get(params["session_id"])
            if stored is None:
                await ws.send_json({"jsonrpc": "2.0", "id": rid, "error": {"code": 4001, "message": "session not found"}})
                return
            self.history[stored].append(params["text"])
            if self.drop_on_submit:
                # The gateway (or the network) dies mid-turn: no terminal frame.
                await ws.close()
                return
            await ws.send_json({"jsonrpc": "2.0", "id": rid, "result": {"status": "streaming"}})
            await ws.send_json({"jsonrpc": "2.0", "method": "event", "params": {
                "type": "message.complete",
                "session_id": params["session_id"],
                "payload": {"text": f"{stored}: {' | '.join(self.history[stored])}", "status": "complete"},
            }})
        elif method == "session.interrupt":
            await ws.send_json({"jsonrpc": "2.0", "id": rid, "result": {"status": "interrupted"}})

    async def _resume(self, ws, rid, params) -> None:
        if self.resume_mode == "hang":
            return
        stored = params.get("session_id", "")
        if self.resume_mode == "settling":
            # Upstream _reattach_refusal while a client-gone interrupt settles.
            await ws.send_json({"jsonrpc": "2.0", "id": rid, "error": {
                "code": 4009, "message": "session disconnect interrupt settling"}})
            return
        if self.resume_mode == "error" or stored not in self.history:
            await ws.send_json({"jsonrpc": "2.0", "id": rid, "error": {"code": 4007, "message": "session not found"}})
            return
        result = {
            "session_id": self._runtime_id(stored), "resumed": stored, "session_key": stored,
            "message_count": len(self.history[stored]), "messages": [], "messages_omitted": True,
            "info": {}, "inflight": None, "running": self.resume_mode == "running",
            "started_at": 0, "status": "idle",
        }
        if self.resume_mode == "auto_continue":
            result["auto_continue"] = {"attempt": 1, "interrupted_at": 0}
        await ws.send_json({"jsonrpc": "2.0", "id": rid, "result": result})

    async def start(self) -> None:
        app = web.Application()
        app.router.add_get("/", self._index)
        app.router.add_get("/api/ws", self._ws)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        self.url = f"http://127.0.0.1:{port}"

    async def stop(self) -> None:
        for ws in self.sockets:
            await ws.close()
        await self._runner.cleanup()


@pytest.fixture(autouse=True)
def _clean_bridge(monkeypatch):
    monkeypatch.setattr(hermes_bridge.hermes_auth, "settings", lambda: None)
    hermes_bridge._CONNECTION_POOL.clear()
    hermes_bridge._OPENING_LOCKS.clear()
    hermes_bridge._CONVERSATIONS.clear()
    hermes_bridge._SWEEPER_TASK = None
    yield
    hermes_bridge._CONNECTION_POOL.clear()
    hermes_bridge._OPENING_LOCKS.clear()
    hermes_bridge._CONVERSATIONS.clear()
    hermes_bridge._SWEEPER_TASK = None


def _run(monkeypatch, scenario):
    """Run ``scenario(hermes)`` against a live fake Hermes on one event loop."""
    hermes = FakeHermes()

    async def main():
        await hermes.start()
        monkeypatch.setenv("HERMES_INTERNAL_URL", hermes.url)
        try:
            return await scenario(hermes)
        finally:
            await hermes_bridge.shutdown_pool()
            await hermes.stop()

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(main())
    finally:
        loop.close()


async def _turn(session_key: str, text: str) -> list[dict]:
    return [event async for event in hermes_bridge.stream_prompt(session_key, text)]


def _complete(events: list[dict]) -> dict:
    return next(event for event in events if event["type"] == "complete")


async def _stop_sweeper() -> None:
    task = hermes_bridge._SWEEPER_TASK
    hermes_bridge._SWEEPER_TASK = None
    if task is not None:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


async def _run_sweeper_until(done) -> None:
    """Run the real idle sweeper at a fast interval until ``done()`` holds."""
    await _stop_sweeper()  # stream_prompt started one with the 60s interval
    hermes_bridge._ensure_sweeper_running()
    for _ in range(200):
        if done():
            break
        await asyncio.sleep(0.01)
    await _stop_sweeper()
    assert done()


async def _evict_idle(monkeypatch, session_key: str) -> None:
    """Let the real idle sweeper evict the pooled connection."""
    monkeypatch.setattr(hermes_bridge, "_IDLE_EXPIRY_SECONDS", 0)
    monkeypatch.setattr(hermes_bridge, "_IDLE_SWEEP_INTERVAL", 0.01)
    await _run_sweeper_until(lambda: session_key not in hermes_bridge._CONNECTION_POOL)


def test_turn_after_idle_eviction_resumes_the_same_hermes_conversation(monkeypatch):
    async def scenario(hermes):
        first = await _turn("phone", "my name is Ada")
        await _evict_idle(monkeypatch, "phone")
        second = await _turn("phone", "what is my name?")
        return hermes, first, second

    hermes, first, second = _run(monkeypatch, scenario)

    assert hermes.methods() == ["session.create", "prompt.submit", "session.resume", "prompt.submit"]
    assert hermes.params_for("session.resume") == [{"session_id": "stored-1", "omit_messages": True}]
    resumed_runtime = second[0]["session_id"]
    assert resumed_runtime != first[0]["session_id"]
    assert hermes.params_for("prompt.submit")[1] == {"session_id": resumed_runtime, "text": "what is my name?"}
    assert _complete(second)["text"] == "stored-1: my name is Ada | what is my name?"
    assert _complete(second)["session_id"] == resumed_runtime


def test_stale_pooled_socket_is_replaced_by_a_resumed_connection(monkeypatch):
    async def scenario(hermes):
        await _turn("phone", "remember 42")
        # Hermes / network drops the pooled socket while the phone is idle.
        await hermes.sockets[0].close()
        conn = hermes_bridge._CONNECTION_POOL["phone"]
        for _ in range(200):
            if conn.ws.closed:
                break
            await asyncio.sleep(0.01)
        second = await _turn("phone", "what number?")
        return hermes, second

    hermes, second = _run(monkeypatch, scenario)

    assert hermes.methods().count("session.create") == 1
    assert hermes.params_for("session.resume") == [{"session_id": "stored-1", "omit_messages": True}]
    assert [params["text"] for params in hermes.params_for("prompt.submit")] == ["remember 42", "what number?"]
    assert _complete(second)["text"] == "stored-1: remember 42 | what number?"


@pytest.mark.parametrize("mode", ["error", "hang"])
def test_failed_resume_reports_lost_continuity_and_never_creates(monkeypatch, mode):
    monkeypatch.setattr(hermes_bridge, "_SESSION_RPC_TIMEOUT", 0.2)

    async def scenario(hermes):
        await _turn("phone", "first")
        await _evict_idle(monkeypatch, "phone")
        hermes.resume_mode = mode
        with pytest.raises(hermes_bridge.HermesConversationLost) as lost:
            await _turn("phone", "second")
        methods_after_failure = hermes.methods()
        record_after_failure = "phone" in hermes_bridge._CONVERSATIONS
        pooled_after_failure = "phone" in hermes_bridge._CONNECTION_POOL
        # The user was told; only the next message starts a new conversation.
        hermes.resume_mode = "ok"
        third = await _turn("phone", "third")
        return hermes, lost.value, methods_after_failure, record_after_failure, pooled_after_failure, third

    hermes, lost, methods, record_kept, pooled, third = _run(monkeypatch, scenario)

    assert methods == ["session.create", "prompt.submit", "session.resume"]
    assert str(lost) == hermes_bridge.CONVERSATION_LOST_MESSAGE
    assert lost.code == "talk_conversation_lost"
    assert record_kept is False
    assert pooled is False
    assert [params["text"] for params in hermes.params_for("prompt.submit")] == ["first", "third"]
    assert _complete(third)["text"].endswith(": third")
    assert not _complete(third)["text"].startswith("stored-1")


def test_resume_with_auto_continue_is_interrupted_and_refused(monkeypatch):
    async def scenario(hermes):
        await _turn("phone", "delete the old backups")
        await _evict_idle(monkeypatch, "phone")
        hermes.resume_mode = "auto_continue"
        with pytest.raises(hermes_bridge.HermesConversationLost):
            await _turn("phone", "now list files")
        return hermes

    hermes = _run(monkeypatch, scenario)

    resumed_runtime = [rt for rt, stored in hermes.runtime_to_stored.items() if stored == "stored-1"][-1]
    assert hermes.methods() == ["session.create", "prompt.submit", "session.resume", "session.interrupt"]
    assert hermes.params_for("session.interrupt") == [{"session_id": resumed_runtime}]
    assert [params["text"] for params in hermes.params_for("prompt.submit")] == ["delete the old backups"]
    assert "phone" not in hermes_bridge._CONVERSATIONS


def test_turn_cut_off_mid_stream_is_never_resumed_or_replayed(monkeypatch):
    """A turn whose socket died before Hermes concluded it may have left an
    upstream crash marker; a cold resume would auto-continue (re-submit) it.
    The bridge reports lost continuity without calling session.resume."""
    async def scenario(hermes):
        await _turn("phone", "first")
        hermes.drop_on_submit = True
        with pytest.raises(hermes_bridge.HermesUnavailable):
            await _turn("phone", "run the migration")
        hermes.drop_on_submit = False
        with pytest.raises(hermes_bridge.HermesConversationLost):
            await _turn("phone", "did it finish?")
        return hermes

    hermes = _run(monkeypatch, scenario)

    assert hermes.methods() == ["session.create", "prompt.submit", "prompt.submit"]
    assert [params["text"] for params in hermes.params_for("prompt.submit")] == ["first", "run the migration"]
    assert "phone" not in hermes_bridge._CONVERSATIONS


@pytest.mark.parametrize("mode", ["running", "settling"])
def test_transient_resume_refusal_keeps_the_conversation(monkeypatch, mode):
    async def scenario(hermes):
        await _turn("phone", "first")
        await _evict_idle(monkeypatch, "phone")
        hermes.resume_mode = mode
        with pytest.raises(hermes_bridge.HermesBridgeError) as busy:
            await _turn("phone", "second")
        hermes.resume_mode = "ok"
        later = await _turn("phone", "third")
        return hermes, busy.value, later

    hermes, busy, later = _run(monkeypatch, scenario)

    assert not isinstance(busy, hermes_bridge.HermesConversationLost)
    assert hermes.methods() == [
        "session.create", "prompt.submit", "session.resume", "session.resume", "prompt.submit",
    ]
    assert [params["text"] for params in hermes.params_for("prompt.submit")] == ["first", "third"]
    assert _complete(later)["text"] == "stored-1: first | third"


def test_cookies_never_resume_each_others_conversation(monkeypatch):
    async def scenario(hermes):
        await _turn("phone-a", "I am A")
        await _turn("phone-b", "I am B")
        await _evict_idle(monkeypatch, "phone-a")
        await _evict_idle(monkeypatch, "phone-b")
        b = await _turn("phone-b", "who am I?")
        a = await _turn("phone-a", "who am I?")
        return hermes, a, b

    hermes, a, b = _run(monkeypatch, scenario)

    assert [params["session_id"] for params in hermes.params_for("session.resume")] == ["stored-3", "stored-1"]
    assert _complete(a)["text"] == "stored-1: I am A | who am I?"
    assert _complete(b)["text"] == "stored-3: I am B | who am I?"


def test_unused_conversation_is_recreated_not_reported_lost(monkeypatch):
    async def scenario(hermes):
        conn = await hermes_bridge._get_connection("phone")
        await hermes_bridge._drop_connection("phone", conn)
        events = await _turn("phone", "hello")
        return hermes, events

    hermes, events = _run(monkeypatch, scenario)

    assert hermes.methods() == ["session.create", "session.create", "prompt.submit"]
    assert _complete(events)["text"].endswith(": hello")


def test_conversation_metadata_is_bounded(monkeypatch):
    async def scenario(hermes):
        await _turn("old", "hi")
        await _evict_idle(monkeypatch, "old")
        record = hermes_bridge._CONVERSATIONS["old"]
        # Records hold only the durable key and bookkeeping, never prompts.
        assert set(vars(record)) == {"stored_session_id", "submitted", "turn_open", "last_used"}

        # Expired records are pruned by the sweeper once unpooled.
        monkeypatch.setattr(hermes_bridge, "_CONVERSATION_TTL_SECONDS", 0)
        await asyncio.sleep(0.01)
        await _run_sweeper_until(lambda: "old" not in hermes_bridge._CONVERSATIONS)

        # At capacity a NEW conversation is refused before touching Hermes;
        # live conversations are never evicted to make room.
        monkeypatch.setattr(hermes_bridge, "_CONVERSATION_TTL_SECONDS", 3600)
        monkeypatch.setattr(hermes_bridge, "_MAX_CONVERSATIONS", 1)
        await _turn("kept", "hi")
        rpcs_before = len(hermes.rpcs)
        with pytest.raises(hermes_bridge.HermesUnavailable):
            await _turn("newcomer", "hi")
        refused_without_rpc = len(hermes.rpcs) == rpcs_before
        still_tracked = sorted(hermes_bridge._CONVERSATIONS)
        return refused_without_rpc, still_tracked

    refused_without_rpc, still_tracked = _run(monkeypatch, scenario)

    assert refused_without_rpc is True
    assert still_tracked == ["kept"]

