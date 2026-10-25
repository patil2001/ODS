"""Small server-side bridge from ODS Talk to the pinned Hermes dashboard.

ODS Talk deliberately does not expose Hermes's browser session token to the
phone. The dashboard-api fetches that token from the internal Hermes HTML page,
opens the JSON-RPC WebSocket on the Docker network, and returns only simplified
chat results to the mobile portal.

Architectural note: Hermes scopes streaming event delivery to the WebSocket
that owns the session. If we open WS-A for ``session.create`` and then open a
fresh WS-B for ``prompt.submit``, Hermes accepts the submit (returns
``{"status":"streaming"}``) but the streaming events fire to WS-A — which we
already closed. The bridge would then wait forever for events that never
arrive and 502 at the request timeout. So a single submit_prompt / stream_prompt
call MUST do both create-session and submit-prompt on the same WS.

Per-cookie connection pool (issue #1322): instead of opening a new WS for
every ODS Talk message, we hold one ``HermesConnection`` per ``session_key``
in a process-wide pool and reuse the same Hermes session across messages from
the same phone. Hermes's per-WS event scoping makes this work — the same WS
is the owner, so events keep flowing. The big win is llama-server's
prompt-cache stays warm across messages, so the second "hey" doesn't pay the
30-60s prefill of the 16k-token agent system prompt. An idle sweeper closes
connections that have been quiet for >5 minutes so a fleet of one-time
visitors doesn't pin Hermes resources forever.

Conversation continuity (issue #8137): the pooled WebSocket is a transport,
not the conversation. The browser keeps the visible transcript and sends only
the new turn, so when a pooled connection is replaced (stale WS) or evicted
(idle sweeper), the next turn must land in the SAME Hermes conversation. The
bridge therefore remembers, per Talk session_key, the durable Hermes session
key that ``session.create`` returned (``stored_session_id``) and reattaches a
new WebSocket with ``session.resume`` instead of creating a new session. If
the conversation cannot be restored, the turn fails with
``HermesConversationLost`` — a fresh Hermes session never silently stands in
for the conversation the phone still shows. ODS never replays a submitted
prompt, and upstream Hermes auto-continues (re-submits) a crash-interrupted
turn on a cold resume, so a conversation whose last turn never concluded is
not resumed at all, and a resume that still reports ``auto_continue`` is
interrupted and refused.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

import aiohttp
import hermes_auth

logger = logging.getLogger(__name__)

TOKEN_RE = re.compile(r'window\.__HERMES_SESSION_TOKEN__\s*=\s*"([^"]+)"')
DEFAULT_HERMES_URL = "http://ods-hermes:9119"
DEFAULT_TIMEOUT_SECONDS = 180


def _env_int(name: str, default: int, *, minimum: int = 1) -> int:
    raw = os.environ.get(name, "").strip()
    if raw.isdigit():
        return max(minimum, int(raw))
    return default


# Connection pool tuning. Override per environment if needed.
_IDLE_EXPIRY_SECONDS = _env_int("ODS_TALK_IDLE_EXPIRY", 300)  # 5 min default
_IDLE_SWEEP_INTERVAL = 60  # how often the background sweeper runs
_APPROVAL_SEND_TIMEOUT = 5.0
_APPROVAL_DISPLAY_LIMIT = 500
_SESSION_RPC_TIMEOUT = 30.0
# Upstream session.resume refusals that mean "try again", not "gone"
# (tui_gateway/session_lifecycle.py::_reattach_refusal at the pinned tag).
_RESUME_SETTLING_CODE = 4009
_RESUME_RETRY_HINT = "retry resume"
# Conversation metadata outlives pooled connections so a reconnect can resume.
# The TTL equals the 12h ODS Talk cookie lifetime (routers/auth.py,
# routers/magic_link.py): a record touched while its cookie is valid cannot
# expire before the cookie does. The size cap fails closed (new conversations
# are refused) instead of evicting a live one.
_CONVERSATION_TTL_SECONDS = 12 * 3600
_MAX_CONVERSATIONS = 1024

CONVERSATION_LOST_CODE = "talk_conversation_lost"
CONVERSATION_LOST_MESSAGE = (
    "ODS Talk could not restore this conversation with Hermes after reconnecting, "
    "so your message was not sent. Hermes no longer has the earlier messages shown "
    "here as context; sending again starts a new conversation."
)


class HermesBridgeError(RuntimeError):
    """Base bridge error surfaced as a 502/503 by the talk router."""


class HermesUnavailable(HermesBridgeError):
    """Hermes is not reachable or did not expose the expected dashboard API."""


class HermesRpcError(HermesBridgeError):
    """Hermes answered a JSON-RPC request with an error object."""

    def __init__(self, message: str, code: Any = None) -> None:
        super().__init__(message)
        self.code = code


class HermesConversationLost(HermesBridgeError):
    """The Hermes conversation behind a Talk session could not be resumed.

    Raised before the new prompt is submitted. The stored conversation is
    forgotten, so the user is told continuity was lost instead of a fresh
    Hermes session silently answering under the old visible transcript.
    """

    code = CONVERSATION_LOST_CODE

    def __init__(self, reason: str) -> None:
        super().__init__(CONVERSATION_LOST_MESSAGE)
        self.reason = reason


class HermesConnectionStale(HermesUnavailable):
    """The pooled WebSocket died before a prompt was submitted.

    This is safe to retry transparently because Hermes never accepted the
    prompt on that transport. Once prompt.submit has been sent, later
    connection drops surface as errors instead of retrying and potentially
    duplicating tool calls or streamed text.
    """


@dataclass
class HermesReply:
    session_id: str
    text: str
    status: str = "ok"
    warning: str | None = None


def _base_url() -> str:
    return (os.environ.get("HERMES_INTERNAL_URL") or DEFAULT_HERMES_URL).rstrip("/")


def _request_timeout() -> int:
    return _env_int("ODS_TALK_HERMES_TIMEOUT", DEFAULT_TIMEOUT_SECONDS, minimum=10)


def talk_session_key(cookie_value: str) -> str:
    """Stable opaque key for the lifetime of a ods-session cookie."""
    return hashlib.sha256(cookie_value.encode("utf-8")).hexdigest()


async def _fetch_hermes_token(session: aiohttp.ClientSession) -> str:
    url = _base_url()
    try:
        async with session.get(url) as resp:
            if resp.status >= 400:
                raise HermesUnavailable(f"Hermes dashboard returned HTTP {resp.status}")
            html = await resp.text()
    except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
        raise HermesUnavailable("Hermes dashboard is not reachable") from exc

    match = TOKEN_RE.search(html)
    if not match:
        raise HermesUnavailable("Hermes dashboard token was not found")
    return match.group(1)


async def _connect_ws(session: aiohttp.ClientSession) -> aiohttp.ClientWebSocketResponse:
    ws_base = _base_url().replace("http://", "ws://", 1).replace("https://", "wss://", 1)
    auth = hermes_auth.settings()
    legacy_protocol = False
    if auth:
        async with session.get(f"{_base_url()}/api/status", allow_redirects=False) as resp:
            if resp.status != 200:
                raise HermesUnavailable("Hermes dashboard protocol identity is unavailable")
            # The previously shipped 0.16 release has password login but no
            # ws-ticket endpoint (its token gate can mask missing routes).
            # Select its known protocol before sending a ticket request;
            # authentication failures never trigger a downgrade.
            legacy_protocol = (await resp.json()).get("version") == "0.16.0"
    authenticated = await login_dashboard(session, auth) if auth else None
    if authenticated is not None and not legacy_protocol:
        async with session.post(f"{_base_url()}/api/auth/ws-ticket", allow_redirects=False) as resp:
            if resp.status != 200:
                raise HermesUnavailable("Hermes did not issue a WebSocket ticket")
            ticket = (await resp.json()).get("ticket")
        if not isinstance(ticket, str) or not ticket:
            raise HermesUnavailable("Hermes WebSocket ticket was missing")
        from urllib.parse import quote
        url = f"{ws_base}/api/ws?ticket={quote(ticket, safe='')}"
    else:
        # Explicit older custom images retain their original token protocol.
        token = await _fetch_hermes_token(session)
        url = f"{ws_base}/api/ws?token={token}"
    try:
        return await session.ws_connect(url)
    except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
        raise HermesUnavailable("Hermes JSON-RPC websocket is not reachable") from exc


async def login_dashboard(session: aiohttp.ClientSession, auth: dict) -> list | None:
    """Supported upstream password login. Never relay the credential to a browser."""
    try:
        async with session.post(f"{_base_url()}/auth/password-login", json={
            "provider": "basic", "username": auth["username"], "password": auth["password"],
        }, allow_redirects=False) as resp:
            if resp.status == 404 and auth.get("managed"):
                # Older explicit image overrides predate password auth. Never
                # downgrade after a credential rejection or transport failure.
                return None
            if resp.status != 200 or (await resp.json()).get("ok") is not True:
                raise HermesUnavailable("Hermes dashboard sign-in failed")
            return list(resp.cookies.values())
    except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
        raise HermesUnavailable("Hermes dashboard sign-in is unavailable") from exc


async def _recv_json(ws: aiohttp.ClientWebSocketResponse, timeout: float) -> dict[str, Any]:
    msg = await asyncio.wait_for(ws.receive(), timeout=timeout)
    if msg.type == aiohttp.WSMsgType.TEXT:
        try:
            return json.loads(msg.data)
        except json.JSONDecodeError as exc:
            raise HermesBridgeError("Hermes sent malformed JSON") from exc
    if msg.type in {aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR}:
        raise HermesUnavailable("Hermes websocket closed")
    return {}


async def _session_rpc_on_ws(
    ws: aiohttp.ClientWebSocketResponse, method: str, params: dict[str, Any], *, timeout: float,
) -> dict[str, Any]:
    """Send one session-level JSON-RPC request and return its result object."""
    request_id = f"ods-talk-{method.replace('.', '-')}"
    await ws.send_str(json.dumps({
        "jsonrpc": "2.0",
        "id": request_id,
        "method": method,
        "params": params,
    }))
    while True:
        frame = await _recv_json(ws, timeout)
        if frame.get("id") != request_id:
            # Pre-create events (gateway.ready etc.) can arrive before the
            # result lands. Ignore them and keep reading.
            continue
        if frame.get("error"):
            err = frame["error"]
            message = err.get("message") if isinstance(err, dict) else str(err)
            code = err.get("code") if isinstance(err, dict) else None
            raise HermesRpcError(message or f"Hermes {method} failed", code)
        result = frame.get("result")
        if not isinstance(result, dict):
            raise HermesBridgeError(f"Hermes {method} returned an unexpected shape")
        return result


def _result_str(result: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = result.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


async def _create_conversation_on_ws(
    ws: aiohttp.ClientWebSocketResponse, *, timeout: float | None = None,
) -> tuple[str, str | None]:
    """Run session.create; return (runtime session_id, stored session key).

    The runtime id addresses the live session on this WebSocket. The stored
    key is the durable conversation id ``session.resume`` needs later; an
    image that does not report one cannot be resumed.
    """
    result = await _session_rpc_on_ws(ws, "session.create", {}, timeout=timeout or _SESSION_RPC_TIMEOUT)
    session_id = _result_str(result, "session_id", "id")
    if not session_id:
        raise HermesBridgeError("Hermes did not return a session id")
    return session_id, _result_str(result, "stored_session_id") or None


async def _create_session_on_ws(ws: aiohttp.ClientWebSocketResponse, *, timeout: float | None = None) -> str:
    """Run session.create over an already-open WS and return the session_id."""
    session_id, _stored = await _create_conversation_on_ws(ws, timeout=timeout)
    return session_id


async def _refuse_auto_continued_resume(ws: aiohttp.ClientWebSocketResponse, session_id: str) -> None:
    """Stop the turn Hermes auto-continued while resuming, before refusing it.

    Upstream schedules a re-submission of a crash-interrupted prompt as part
    of a cold ``session.resume``. ODS never replays a submitted prompt, so
    ask Hermes to interrupt that turn (which also retires its crash marker)
    on this socket, which nothing else is reading yet.
    """
    try:
        await asyncio.wait_for(
            _session_rpc_on_ws(ws, "session.interrupt", {"session_id": session_id}, timeout=_SESSION_RPC_TIMEOUT),
            timeout=_SESSION_RPC_TIMEOUT,
        )
    except (HermesBridgeError, asyncio.TimeoutError, aiohttp.ClientError, ConnectionError):
        logger.warning(
            "hermes-bridge: could not interrupt auto-continued turn on resumed session %s",
            session_id, exc_info=True,
        )


async def _resume_conversation_on_ws(
    ws: aiohttp.ClientWebSocketResponse, stored_session_id: str, *, timeout: float | None = None,
) -> tuple[str, str]:
    """Reattach an existing Hermes conversation; return (runtime id, stored key).

    Raises HermesConversationLost when Hermes answers but the conversation
    cannot be continued safely. A closed socket stays HermesUnavailable:
    nothing was decided, so the stored key is kept for a later attempt.
    """
    try:
        result = await _session_rpc_on_ws(ws, "session.resume", {
            "session_id": stored_session_id,
            # The phone already shows the transcript; don't ship it back.
            "omit_messages": True,
        }, timeout=timeout or _SESSION_RPC_TIMEOUT)
    except HermesUnavailable:
        raise
    except asyncio.TimeoutError as exc:
        raise HermesConversationLost("session.resume timed out") from exc
    except HermesRpcError as exc:
        if exc.code == _RESUME_SETTLING_CODE or _RESUME_RETRY_HINT in str(exc):
            # Upstream's transient reattach refusals: a disconnect interrupt
            # is still settling, or the live session was reaped mid-claim.
            # Nothing is lost yet; keep the stored key for the next turn.
            raise HermesBridgeError(
                "Hermes is still settling this conversation after the reconnect. Try again in a moment."
            ) from exc
        raise HermesConversationLost(f"session.resume failed: {exc}") from exc
    except HermesBridgeError as exc:
        raise HermesConversationLost(f"session.resume failed: {exc}") from exc

    session_id = _result_str(result, "session_id")
    if not session_id:
        raise HermesConversationLost("session.resume returned no session id")
    if result.get("auto_continue") is not None:
        await _refuse_auto_continued_resume(ws, session_id)
        raise HermesConversationLost("Hermes auto-continued an interrupted turn on resume")
    if result.get("running") is True:
        # A turn ODS Talk is not reading is still running on this
        # conversation; a new prompt.submit would queue behind it and its
        # completion would be mistaken for this reply.
        raise HermesBridgeError(
            "Hermes is still finishing an earlier turn in this conversation. Try again in a moment."
        )
    # Compression can move the conversation to a new stored tip.
    stored = _result_str(result, "session_key", "resumed") or stored_session_id
    return session_id, stored


@dataclass
class _TalkConversation:
    """The Hermes conversation behind one Talk session_key.

    Lives independently of the pooled WebSocket so a replaced or evicted
    connection can ``session.resume`` it. Holds only the durable Hermes key
    and bookkeeping; never prompt text.
    """
    # Durable key from session.create; None when the Hermes image did not
    # report one (that conversation cannot be resumed).
    stored_session_id: str | None
    # Set once a prompt.submit was sent: from then on Hermes holds context
    # the phone's transcript depends on, so a reconnect must resume it.
    submitted: bool = False
    # True from prompt.submit until Hermes reports the turn concluded
    # (message.complete, an error event, or an acknowledged interrupt).
    # Upstream leaves a crash marker only for a turn that never concluded,
    # and a cold resume auto-continues (re-submits) such a turn. So a
    # reconnect while this is set refuses to resume at all.
    turn_open: bool = False
    last_used: float = field(default_factory=time.monotonic)


@dataclass
class _HermesConnection:
    """One long-lived WS + Hermes session, scoped to a single phone cookie.

    Holding the WS open across messages lets Hermes reuse its session_id and
    keeps the 16k-token system prompt warm in llama-server's KV cache. Each
    new prompt on the same connection costs ~1k tokens of context (the user
    message + maybe a tool result), not 16k+1k. Big latency win.

    The per-connection ``lock`` serializes two prompts from the same phone:
    Hermes can't multiplex two prompt.submit calls on one session anyway,
    and the SPA's UI already enforces "wait for the previous reply" — this
    is the server-side belt to that suspenders.
    """
    http_session: aiohttp.ClientSession
    ws: aiohttp.ClientWebSocketResponse
    session_id: str
    last_used: float = field(default_factory=time.monotonic)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    # Approval responses must be writable while ``lock`` is held by the
    # long-running prompt receive loop. All WS writes use this narrower lock.
    write_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    # The browser never owns or echoes this protocol payload. It only submits
    # one of the two choices accepted by ``respond_approval``.
    pending_approval: dict[str, Any] | None = None
    closed: bool = False
    conversation: _TalkConversation | None = None

    async def aclose(self) -> None:
        if self.closed:
            return
        self.closed = True
        self.pending_approval = None
        try:
            await self.ws.close()
        except Exception:  # pragma: no cover — best-effort cleanup
            logger.debug("ws.close raised during pool eviction", exc_info=True)
        try:
            await self.http_session.close()
        except Exception:  # pragma: no cover
            logger.debug("http_session.close raised during pool eviction", exc_info=True)


_CONNECTION_POOL: dict[str, _HermesConnection] = {}
_POOL_GUARD = asyncio.Lock()
# Per-key creation locks: two concurrent first-messages from the same phone
# share one open instead of racing, without holding the global guard.
_OPENING_LOCKS: dict[str, asyncio.Lock] = {}
_SWEEPER_TASK: asyncio.Task | None = None
# Per-session_key Hermes conversation metadata (issue #8137). Keyed exactly
# like the pool, so one cookie can never resume another cookie's
# conversation. Bounded by _CONVERSATION_TTL_SECONDS and _MAX_CONVERSATIONS.
_CONVERSATIONS: dict[str, _TalkConversation] = {}


def _conversation_expired(conversation: _TalkConversation, now: float) -> bool:
    return now - conversation.last_used > _CONVERSATION_TTL_SECONDS


def _prune_conversations(now: float) -> None:
    """Drop expired conversation records that no pooled connection uses."""
    for key, conversation in list(_CONVERSATIONS.items()):
        if key not in _CONNECTION_POOL and _conversation_expired(conversation, now):
            del _CONVERSATIONS[key]


def _forget_conversation(session_key: str, conversation: _TalkConversation) -> None:
    if _CONVERSATIONS.get(session_key) is conversation:
        del _CONVERSATIONS[session_key]


async def _open_connection(session_key: str) -> _HermesConnection:
    """Open one fresh WS and attach it to this Talk session's conversation.

    A conversation that already received a prompt is reattached with
    ``session.resume``; only a brand-new (or never-used) conversation runs
    ``session.create``. Caller holds the per-key opening lock, NOT
    _POOL_GUARD — this does network I/O that can hang for the full client
    timeout when Hermes is slow or down, and must never stall other phones'
    pool lookups.
    """
    now = time.monotonic()
    conversation = _CONVERSATIONS.get(session_key)
    if conversation is not None and _conversation_expired(conversation, now):
        _forget_conversation(session_key, conversation)
        conversation = None
    resume = conversation is not None and conversation.submitted
    if resume and not conversation.stored_session_id:
        _forget_conversation(session_key, conversation)
        raise HermesConversationLost("Hermes did not report a resumable session key")
    if resume and conversation.turn_open:
        # The last turn's connection died before Hermes concluded it. If the
        # Hermes process died too, resuming would auto-continue that prompt.
        _forget_conversation(session_key, conversation)
        raise HermesConversationLost("the previous turn never concluded; resuming could re-run it")
    if conversation is None:
        _prune_conversations(now)
        if len(_CONVERSATIONS) >= _MAX_CONVERSATIONS:
            raise HermesUnavailable("ODS Talk has too many open conversations. Try again later.")

    timeout_seconds = _request_timeout()
    timeout = aiohttp.ClientTimeout(total=timeout_seconds + 20)
    http_session = aiohttp.ClientSession(timeout=timeout)
    try:
        ws = await _connect_ws(http_session)
    except Exception:
        await http_session.close()
        raise
    try:
        if resume:
            session_id, stored = await _resume_conversation_on_ws(ws, conversation.stored_session_id)
            conversation.stored_session_id = stored
        else:
            session_id, stored = await _create_conversation_on_ws(ws)
            conversation = _TalkConversation(stored_session_id=stored)
            _CONVERSATIONS[session_key] = conversation
    except HermesConversationLost as exc:
        _forget_conversation(session_key, conversation)
        logger.warning(
            "hermes-bridge: conversation for %s could not be resumed: %s", session_key[:8], exc.reason,
        )
        await ws.close()
        await http_session.close()
        raise
    except Exception:
        await ws.close()
        await http_session.close()
        raise
    conversation.last_used = time.monotonic()
    conn = _HermesConnection(
        http_session=http_session, ws=ws, session_id=session_id, conversation=conversation,
    )
    logger.info(
        "hermes-bridge: opened pooled connection for %s (session_id=%s, %s)",
        session_key[:8], session_id, "resumed" if resume else "created",
    )
    return conn


async def _get_connection(session_key: str) -> _HermesConnection:
    """Look up the pooled connection for this session_key, or create one.

    Caller must use ``async with conn.lock:`` around any send/receive cycle
    to keep concurrent same-key prompts from interleaving on the same WS.

    _POOL_GUARD is held only for dict bookkeeping. Opening a connection
    (token fetch + ws connect + session.create) is network I/O that can
    hang for the full client timeout when Hermes is slow or down; holding
    the global guard across it would head-of-line block every other
    phone's warm-connection lookup behind one cold open. The per-key
    opening lock keeps concurrent same-key calls from opening duplicates.
    """
    async with _POOL_GUARD:
        conn = _CONNECTION_POOL.get(session_key)
        if conn is not None and not conn.closed and not conn.ws.closed:
            return conn
        opening = _OPENING_LOCKS.setdefault(session_key, asyncio.Lock())

    async with opening:
        # Re-check under the guard: another same-key call may have finished
        # opening while this one waited on the opening lock.
        async with _POOL_GUARD:
            conn = _CONNECTION_POOL.get(session_key)
            if conn is not None and not conn.closed and not conn.ws.closed:
                return conn
            # Either no entry, marked closed, or the underlying ws died.
            # Drop whatever is in the slot and open fresh.
            stale = _CONNECTION_POOL.pop(session_key, None)
        if stale is not None:
            await stale.aclose()
        new_conn = await _open_connection(session_key)
        async with _POOL_GUARD:
            _CONNECTION_POOL[session_key] = new_conn
        return new_conn


async def _drop_connection(session_key: str, conn: _HermesConnection) -> None:
    """Evict a connection from the pool (e.g. after a dead-WS error)."""
    async with _POOL_GUARD:
        current = _CONNECTION_POOL.get(session_key)
        if current is conn:
            _CONNECTION_POOL.pop(session_key, None)
    await conn.aclose()


async def _sweep_idle_connections() -> None:
    """Background task: close pool entries idle for > _IDLE_EXPIRY_SECONDS.

    Runs forever; the task is held in ``_SWEEPER_TASK``. Cancelled by
    ``shutdown_pool()`` on app shutdown.
    """
    while True:
        try:
            await asyncio.sleep(_IDLE_SWEEP_INTERVAL)
            now = time.monotonic()
            stale: list[tuple[str, _HermesConnection]] = []
            async with _POOL_GUARD:
                for key, conn in list(_CONNECTION_POOL.items()):
                    if conn.lock.locked():
                        # Active prompt. Do not close the WS while Hermes is
                        # streaming or pre-filling a long response; last_used
                        # is refreshed again when the prompt completes.
                        continue
                    if conn.closed or conn.ws.closed:
                        stale.append((key, conn))
                        _CONNECTION_POOL.pop(key, None)
                        continue
                    if now - conn.last_used > _IDLE_EXPIRY_SECONDS:
                        stale.append((key, conn))
                        _CONNECTION_POOL.pop(key, None)
                # Opening locks are keyed by cookie hash and would otherwise
                # grow forever; drop the ones that are idle and unpooled.
                for key, lock in list(_OPENING_LOCKS.items()):
                    if not lock.locked() and key not in _CONNECTION_POOL:
                        del _OPENING_LOCKS[key]
                # Conversation records survive connection eviction (that is
                # what lets the next turn resume) but expire with the cookie.
                _prune_conversations(now)
            for key, conn in stale:
                logger.info("hermes-bridge: evicting idle connection for %s", key[:8])
                await conn.aclose()
        except asyncio.CancelledError:
            raise
        except Exception:  # pragma: no cover — keep sweeper alive on bugs
            logger.exception("hermes-bridge: idle sweeper hit an error; continuing")


def _ensure_sweeper_running() -> None:
    """Lazily start the idle sweeper on first use. Idempotent."""
    global _SWEEPER_TASK
    if _SWEEPER_TASK is not None and not _SWEEPER_TASK.done():
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    _SWEEPER_TASK = loop.create_task(_sweep_idle_connections())


async def shutdown_pool() -> None:
    """Close every pooled connection. Call from a FastAPI lifespan handler
    so a graceful shutdown doesn't leak WSes / file descriptors."""
    global _SWEEPER_TASK
    if _SWEEPER_TASK is not None:
        _SWEEPER_TASK.cancel()
        try:
            await _SWEEPER_TASK
        except (asyncio.CancelledError, Exception):
            pass
        _SWEEPER_TASK = None
    async with _POOL_GUARD:
        connections = list(_CONNECTION_POOL.values())
        _CONNECTION_POOL.clear()
        _OPENING_LOCKS.clear()
        _CONVERSATIONS.clear()
    for conn in connections:
        await conn.aclose()


async def _submit_on_connection(
    conn: _HermesConnection, text: str, timeout_seconds: int,
) -> AsyncIterator[dict[str, Any]]:
    """Send one prompt on an already-open pooled connection and yield events.

    Caller must hold ``conn.lock`` for the duration of this generator so two
    same-key prompts can't interleave on the same WS. Mutates ``conn.last_used``
    on completion. Raises HermesUnavailable when the WS is dead (caller is
    expected to evict + reopen), HermesBridgeError on protocol-level errors.
    """
    request_id = f"ods-talk-prompt-{int(time.monotonic() * 1000)}"
    try:
        conn.last_used = time.monotonic()
        async with conn.write_lock:
            await conn.ws.send_str(json.dumps({
                "jsonrpc": "2.0",
                "id": request_id,
                "method": "prompt.submit",
                "params": {"session_id": conn.session_id, "text": text},
            }))
        if conn.conversation is not None:
            # Hermes now holds context this phone's transcript depends on;
            # a later reconnect must resume it, never create a new session.
            conn.conversation.submitted = True
            conn.conversation.turn_open = True
            conn.conversation.last_used = time.monotonic()
    except (aiohttp.ClientError, ConnectionResetError, ConnectionError) as exc:
        # Pooled WS was closed under us between the freshness check and this
        # send (Hermes restart, network blip, idle timeout that hadn't been
        # noticed yet). Surface as HermesConnectionStale so stream_prompt
        # evicts the pool entry + retries on a fresh connection. This is the
        # only transparent-retry case because the prompt was not submitted.
        raise HermesConnectionStale(f"Hermes WS closed before prompt submit: {exc}") from exc

    chunks: list[str] = []
    while True:
        frame = await _recv_json(conn.ws, timeout_seconds)

        # Reply to our prompt.submit RPC — informational; events still follow.
        if frame.get("id") == request_id:
            if frame.get("error"):
                err = frame["error"]
                message = err.get("message") if isinstance(err, dict) else str(err)
                raise HermesBridgeError(message or "Hermes prompt failed")
            continue

        if frame.get("method") != "event":
            continue
        event = frame.get("params") or {}
        if not isinstance(event, dict):
            continue
        if event.get("session_id") and event.get("session_id") != conn.session_id:
            # Stray event from a sibling session — ignore.
            continue

        payload = event.get("payload") or {}
        if not isinstance(payload, dict):
            payload = {}

        event_type = event.get("type")
        if event_type == "message.delta":
            chunk = payload.get("text")
            if isinstance(chunk, str) and chunk:
                chunks.append(chunk)
                yield {"type": "delta", "text": chunk}
        elif event_type == "status.update":
            # Hermes (pinned image) emits status.update events with a pre-
            # formatted human-readable text and a `kind` category. Examples
            # we've observed in the wild:
            #
            #   {"kind": "lifecycle", "text": "⏳ Retrying in 5.1s (attempt 2/3)..."}
            #   {"kind": "lifecycle", "text": "⚠️ Max retries (3) exhausted..."}
            #
            # Upstream is responsible for the human-readable wording (often
            # with emoji prefixes); we just forward the text so the SPA's
            # spinner caption shows what Hermes is actually doing. The
            # "Searching the web…" / "Reading the page…" labels we add on
            # the SSE-layer side are for older `tool.start`-style events
            # that some builds may still emit; this branch handles the
            # newer status.update path.
            status_text = payload.get("text") if isinstance(payload.get("text"), str) else None
            if status_text:
                yield {
                    "type": "status",
                    "label": status_text,
                    "kind": payload.get("kind") if isinstance(payload.get("kind"), str) else None,
                }
        elif event_type == "tool.start":
            # Older-build path: Hermes may also emit explicit tool.start
            # events with name + context. We translate those into status
            # frames via the SSE layer's _label_for_tool() mapping.
            tool_name = payload.get("name") if isinstance(payload.get("name"), str) else None
            if tool_name:
                yield {
                    "type": "tool_start",
                    "tool": tool_name,
                    "detail": payload.get("context") if isinstance(payload.get("context"), str) else None,
                }
        elif event_type == "tool.complete":
            # Older-build path companion: tool finished, clear the caption.
            tool_name = payload.get("name") if isinstance(payload.get("name"), str) else None
            if tool_name:
                yield {
                    "type": "tool_complete",
                    "tool": tool_name,
                    "duration_s": payload.get("duration_s") if isinstance(payload.get("duration_s"), (int, float)) else None,
                    "summary": payload.get("summary") if isinstance(payload.get("summary"), str) else None,
                }
        elif event_type == "approval.request":
            # Keep the authoritative request on the live server-side
            # connection. The phone receives bounded display text only and
            # can answer with a fixed choice; it cannot alter the command.
            conn.pending_approval = dict(payload)
            command = payload.get("command") if isinstance(payload.get("command"), str) else ""
            description = payload.get("description") if isinstance(payload.get("description"), str) else ""
            yield {
                "type": "approval",
                "command": command[:_APPROVAL_DISPLAY_LIMIT],
                "description": description[:_APPROVAL_DISPLAY_LIMIT],
                "choices": ["once", "deny"],
            }
        elif event_type == "message.complete":
            final_text = payload.get("text")
            if not isinstance(final_text, str) or not final_text.strip():
                final_text = "".join(chunks)
            conn.last_used = time.monotonic()
            if conn.conversation is not None:
                conn.conversation.turn_open = False
                conn.conversation.last_used = conn.last_used
            conn.pending_approval = None
            yield {
                "type": "complete",
                "session_id": conn.session_id,
                "text": final_text.strip(),
                "status": str(payload.get("status") or "ok"),
                "warning": payload.get("warning") if isinstance(payload.get("warning"), str) else None,
            }
            return
        elif event_type == "error":
            conn.pending_approval = None
            if conn.conversation is not None:
                conn.conversation.turn_open = False
            message = payload.get("message") if isinstance(payload.get("message"), str) else "Hermes reported an error"
            raise HermesBridgeError(message)


async def stream_prompt(session_key: str, text: str) -> AsyncIterator[dict[str, Any]]:
    """Submit a prompt to Hermes and yield delta events as they stream back.

    Yields dicts with:
      {"type": "session",        "session_id": <id>}                          # once at start
      {"type": "tool_start",     "tool": <name>, "detail": <context|None>}    # zero or more
      {"type": "tool_complete",  "tool": <name>, "duration_s": <float|None>,
                                 "summary": <str|None>}                       # zero or more
      {"type": "delta",          "text": <chunk>}                             # zero or more
      {"type": "complete",       "session_id": <id>, "text": <full>, ...}     # once at end

    On error, raises HermesUnavailable / HermesBridgeError; no partial yield.

    Uses the per-cookie connection pool so subsequent messages from the same
    phone reuse the same Hermes session_id (and therefore keep llama-server's
    prompt cache warm for the 16k-token agent system prompt). On the first
    call for a cookie, opens a fresh WS and runs session.create; on later
    calls, reuses the pooled connection and just sends prompt.submit. The
    pool's idle sweeper closes inactive connections after
    ODS_TALK_IDLE_EXPIRY seconds (default 300s = 5 min) so a fleet of
    one-time visitors doesn't pin resources forever.

    Two same-key prompts can't overlap (per-connection ``lock`` serializes
    them); two different-key prompts run in parallel as long as
    llama-server's slots can absorb the load.
    """
    _ensure_sweeper_running()
    timeout_seconds = _request_timeout()

    # Attempt twice only for the pre-submit stale-WS case: the first try uses
    # the pooled connection (warm cache, fast path); if send_str fails before
    # Hermes accepts prompt.submit, evict it and try once more with a fresh
    # connection. Once prompt.submit has been sent, later WS failures surface
    # as errors instead of retrying and duplicating tool calls or text.
    session_frame_emitted = False
    for attempt in (1, 2):
        conn = await _get_connection(session_key)
        async with conn.lock:
            if not session_frame_emitted:
                # Yield the session frame once, from the *first* attempt's
                # connection. If the connection dies before any deltas, the
                # retry's session_id will be different but the SPA only cares
                # about the most recent — replacing the frame is fine.
                yield {"type": "session", "session_id": conn.session_id}
                session_frame_emitted = True
            try:
                async for event in _submit_on_connection(conn, text, timeout_seconds):
                    yield event
                return
            except HermesConnectionStale as exc:
                await _drop_connection(session_key, conn)
                if attempt == 2:
                    # Already retried once; give up and surface the failure.
                    raise
                logger.info("hermes-bridge: retrying once after stale WS (%s)", exc)
                # Drop the lock + loop to attempt 2 with a fresh connection.
                continue
            except HermesUnavailable:
                await _drop_connection(session_key, conn)
                raise


async def respond_approval(session_key: str, choice: str) -> bool:
    """Answer the current approval on an existing pooled Talk connection.

    This deliberately never opens a connection and never acquires the prompt
    ``lock``: that lock is held while Hermes waits for this response. The
    pending request is claimed under ``write_lock`` before sending, so at most
    one racing browser request can succeed. After the send begins, failures are
    treated as ambiguous and the request is not restored or retried.
    """
    if choice not in {"once", "deny"}:
        raise ValueError("choice must be 'once' or 'deny'")

    async with _POOL_GUARD:
        conn = _CONNECTION_POOL.get(session_key)
        if conn is None or conn.closed or conn.ws.closed:
            return False

    try:
        await asyncio.wait_for(conn.write_lock.acquire(), timeout=_APPROVAL_SEND_TIMEOUT)
    except asyncio.TimeoutError as exc:
        raise HermesBridgeError("Hermes approval channel is busy") from exc

    try:
        if conn.closed or conn.ws.closed or conn.pending_approval is None:
            return False

        rpc = json.dumps({
            "jsonrpc": "2.0",
            "id": f"ods-talk-approval-{time.monotonic_ns()}",
            "method": "approval.respond",
            "params": {
                "session_id": conn.session_id,
                "choice": choice,
                "all": False,
            },
        })
        conn.pending_approval = None
        try:
            await asyncio.wait_for(
                conn.ws.send_str(rpc),
                timeout=_APPROVAL_SEND_TIMEOUT,
            )
        except asyncio.TimeoutError as exc:
            raise HermesBridgeError(
                "Hermes approval response timed out; response state is unknown"
            ) from exc
        except (aiohttp.ClientError, ConnectionResetError, ConnectionError) as exc:
            raise HermesBridgeError("Hermes approval response could not be sent") from exc
        conn.last_used = time.monotonic()
        return True
    finally:
        conn.write_lock.release()


async def deny_pending_approval(session_key: str) -> bool:
    """Best-effort safety denial used before abandoning an SSE stream."""
    try:
        return await respond_approval(session_key, "deny")
    except (HermesBridgeError, asyncio.TimeoutError):
        logger.warning(
            "hermes-bridge: could not deny abandoned approval for %s",
            session_key[:8],
            exc_info=True,
        )
        return False


async def _interrupt_session_with_ack(session_id: str) -> None:
    """Interrupt a Hermes session on a short-lived control WebSocket.

    The prompt's pooled WebSocket already has a reader blocked in
    ``_submit_on_connection``.  Reading an interrupt reply on that socket
    would race that reader, while sending and immediately closing gives the
    Hermes gateway no delivery acknowledgement.  A separate authenticated
    control connection can address the process-wide session id, receive the
    JSON-RPC result, and prove Hermes processed the interrupt before the
    prompt-owning socket is evicted.
    """
    timeout = aiohttp.ClientTimeout(total=_APPROVAL_SEND_TIMEOUT * 2)
    control_session = aiohttp.ClientSession(timeout=timeout)
    control_ws: aiohttp.ClientWebSocketResponse | None = None
    request_id = f"ods-talk-interrupt-{time.monotonic_ns()}"
    deadline = time.monotonic() + _APPROVAL_SEND_TIMEOUT
    try:
        control_ws = await _connect_ws(control_session)
        await asyncio.wait_for(
            control_ws.send_str(json.dumps({
                "jsonrpc": "2.0",
                "id": request_id,
                "method": "session.interrupt",
                "params": {"session_id": session_id},
            })),
            timeout=max(0.01, deadline - time.monotonic()),
        )
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise asyncio.TimeoutError
            frame = await _recv_json(control_ws, remaining)
            if frame.get("id") != request_id:
                # gateway.ready and unrelated process-level events may arrive
                # before the RPC result on a newly opened control socket.
                continue
            if frame.get("error"):
                err = frame["error"]
                message = err.get("message") if isinstance(err, dict) else str(err)
                raise HermesBridgeError(message or "Hermes session interrupt failed")
            result = frame.get("result")
            if not isinstance(result, dict) or result.get("status") != "interrupted":
                raise HermesBridgeError("Hermes did not acknowledge the session interrupt")
            return
    finally:
        if control_ws is not None:
            with contextlib.suppress(Exception):
                await control_ws.close()
        with contextlib.suppress(Exception):
            await control_session.close()


async def interrupt_active_prompt(session_key: str) -> bool:
    """Interrupt and evict the active Hermes prompt for one Talk session.

    Closing the WebSocket alone is not cancellation: Hermes deliberately
    detaches an in-flight session and lets its agent continue after a client
    disconnect. On a single-slot local model that can block every later Talk,
    Pixel, and capability request for minutes. Send Hermes's session-scoped
    abort RPC before closing the pooled connection so the abandoned turn frees
    its upstream model slot without affecting any sibling session.

    This is best-effort disconnect cleanup. It never opens a new connection,
    never interrupts an idle session, and always evicts the used connection so
    late frames from the interrupted turn cannot be mistaken for a later Talk
    response.
    """
    async with _POOL_GUARD:
        conn = _CONNECTION_POOL.get(session_key)
        if (
            conn is None
            or conn.closed
            or conn.ws.closed
            or not conn.lock.locked()
        ):
            return False
        _CONNECTION_POOL.pop(session_key, None)

    acknowledged = False
    try:
        await _interrupt_session_with_ack(conn.session_id)
        conn.pending_approval = None
        conn.last_used = time.monotonic()
        if conn.conversation is not None:
            conn.conversation.turn_open = False
        acknowledged = True
        logger.info(
            "hermes-bridge: Hermes acknowledged interrupt for abandoned prompt %s",
            session_key[:8],
        )
    except (
        HermesBridgeError,
        asyncio.TimeoutError,
        aiohttp.ClientError,
        ConnectionResetError,
        ConnectionError,
    ):
        logger.warning(
            "hermes-bridge: could not interrupt abandoned prompt for %s",
            session_key[:8],
            exc_info=True,
        )
    finally:
        try:
            await asyncio.wait_for(
                conn.aclose(),
                timeout=_APPROVAL_SEND_TIMEOUT,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "hermes-bridge: timed out closing interrupted connection for %s",
                session_key[:8],
            )
    return acknowledged


async def submit_prompt(session_key: str, text: str) -> HermesReply:
    """Blocking wrapper that consumes stream_prompt and returns the final reply.

    Kept for callers (and tests) that want the full reply as a single dict
    instead of an event stream. New UI code should use stream_prompt directly
    so the user sees tokens land in real time.
    """
    session_id = ""
    final_text = ""
    status = "ok"
    warning: str | None = None
    async for event in stream_prompt(session_key, text):
        et = event.get("type")
        if et == "session":
            session_id = event.get("session_id", "") or session_id
        elif et == "complete":
            session_id = event.get("session_id", "") or session_id
            final_text = event.get("text", "") or final_text
            status = event.get("status") or "ok"
            warning = event.get("warning")
    if not session_id and not final_text:
        raise HermesBridgeError("Hermes did not finish the response.")
    return HermesReply(session_id=session_id, text=final_text, status=status, warning=warning)


# -------- legacy compat shims (kept so existing tests keep importing OK) --------

_SESSION_IDS: dict[str, str] = {}


async def ensure_session(session_key: str) -> str:
    """Legacy: tests call this to seed _SESSION_IDS before invoking submit_prompt.

    The streaming bridge now creates a fresh Hermes session per call, so the
    stored value is informational only. We still return *something* truthy so
    tests that assert "ensure_session returned a non-empty string" pass.
    """
    existing = _SESSION_IDS.get(session_key)
    if existing:
        return existing
    timeout_seconds = _request_timeout()
    timeout = aiohttp.ClientTimeout(total=timeout_seconds)
    async with aiohttp.ClientSession(timeout=timeout) as http_session:
        ws = await _connect_ws(http_session)
        async with ws:
            session_id = await _create_session_on_ws(ws, timeout=30)
    _SESSION_IDS[session_key] = session_id
    return session_id


def clear_session_for_tests(session_key: str | None = None) -> None:
    if session_key is None:
        _SESSION_IDS.clear()
    else:
        _SESSION_IDS.pop(session_key, None)
