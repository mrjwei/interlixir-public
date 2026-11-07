"""Chat agent service: the /acp routes behind CloudFront, plus /healthz for the ALB.

Path: SPA -> CloudFront /acp/* (caching off, viewer headers but Host forwarded, the origin secret header added)
-> the shared ALB, app host, rule "/acp/* + secret" -> this task. The MCP server runs beside it in the same task.

Routes (all need the user's Cognito ID token):
  GET    /acp/quota                       this hour's count: {used, limit, resetAt, warning, blocked}
  GET    /acp/sessions                    the user's sessions, most recent first
  GET    /acp/sessions/{sessionId}        one session and its messages
  DELETE /acp/sessions/{sessionId}        204; the session and its messages
  POST   /acp/messages {sessionId?, text} a turn, streamed as server-sent events (below); 429 over the quota

The turn is streamed (SSE) because it takes several model and tool calls: the response starts at once and
CloudFront's 30 s origin read timeout never sees an idle connection. Events, each `data:` one JSON object:
  session  {sessionId, title, created}            first, always (a new session's ID)
  quota    {used, limit, resetAt, warning, blocked}
  tool     {id, name, status: running|done|error, label?}
  message  {messageId, role, text, sentAt, tools, tokenCount, cost}
  error    {detail}
  done     {}
plus a `: ping` comment every HEARTBEAT_SECONDS while the model or a tool is working.

The turn runs in its own task: if the browser goes away mid-answer, the answer is still saved (and its
tokens are already spent), so reopening the session shows it.

Every request but /healthz logs one line: route, status, durationMs (to the first byte for the stream), userId.
"""

import asyncio
import json
import time
import traceback
from typing import Annotated

from contracts import logs
from fastapi import FastAPI, Path, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, StringConstraints

from . import auth, chat, quota, store
from .auth import CallerDep

logs.configure("chat-agent")

HEARTBEAT_SECONDS = 15
MAX_MESSAGE_CHARS = 2000

app = FastAPI(title="interlixir-chat-agent")

SessionId = Annotated[str, Path(pattern=auth.ID_PATTERN)]

# Turns still running after their client left; kept so the event loop doesn't drop the tasks.
_turns = set()


@app.exception_handler(store.NotFound)
def not_found(request: Request, exc: store.NotFound):
    return JSONResponse({"detail": "Not found"}, status_code=404)


@app.middleware("http")
async def log_request(request: Request, call_next):
    if request.url.path == "/healthz":
        return await call_next(request)
    started, status, failure = time.monotonic(), 500, None
    try:
        response = await call_next(request)
        status = response.status_code
        return response
    except Exception as error:
        failure = {"error": repr(error), "trace": traceback.format_exc()}
        raise
    finally:
        route = request.scope.get("route")
        name = f"{request.method} {route.path}" if route is not None else "unmatched"
        fields = {"method": request.method, "route": name, "status": status,
                  "userId": getattr(request.state, "user_id", None)}
        duration = logs.ms_since(started)
        logs.metric("INFO" if status < 500 else "ERROR", "request",
                    {"RequestLatency": (duration, "Milliseconds")}, {"Route": name}, durationMs=duration, **fields)
        if status >= 500:
            logs.error("request failed", **fields, **(failure or {}))


@app.get("/healthz", include_in_schema=False)
def healthz():
    return {"status": "ok"}


@app.get("/acp/quota")
def get_quota(caller: CallerDep):
    return quota.peek(caller.user_id)


@app.get("/acp/sessions")
def list_sessions(caller: CallerDep):
    return {"sessions": [store.session_view(item) for item in store.list_sessions(caller.user_id)]}


@app.get("/acp/sessions/{session_id}")
def get_session(session_id: SessionId, caller: CallerDep):
    session = store.get_session(caller.user_id, session_id)
    messages = store.list_messages(caller.user_id, session_id)
    return {**store.session_view(session), "messages": [store.message_view(m) for m in messages]}


@app.delete("/acp/sessions/{session_id}", status_code=204)
def delete_session(session_id: SessionId, caller: CallerDep):
    store.delete_session(caller.user_id, session_id)
    return Response(status_code=204)


class NewMessage(BaseModel):
    sessionId: Annotated[str, StringConstraints(pattern=auth.ID_PATTERN)] | None = None
    text: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_MESSAGE_CHARS)]


@app.post("/acp/messages")
async def post_message(body: NewMessage, caller: CallerDep):
    # The session check comes before the quota, so a wrong ID doesn't use up a message.
    prior, session = [], None
    if body.sessionId:
        session = await asyncio.to_thread(store.get_session, caller.user_id, body.sessionId)
        prior = [store.message_view(m) for m in
                 await asyncio.to_thread(store.list_messages, caller.user_id, body.sessionId)]
    allowed, status = await asyncio.to_thread(quota.take, caller.user_id)
    if not allowed:
        logs.info("chat blocked by quota", userId=caller.user_id, used=status["used"], limit=status["limit"])
        retry_after = max(1, int(quota.window()[1].timestamp() - time.time()))
        return JSONResponse({"detail": f"You've reached {status['limit']} messages this hour.", "quota": status},
                            status_code=429, headers={"Retry-After": str(retry_after)})
    created = session is None
    if created:
        session = await asyncio.to_thread(store.create_session, caller.user_id, body.text)
    session_id = session["SK"].removeprefix(store.CHAT_PREFIX)
    try:
        await asyncio.to_thread(store.add_message, caller.user_id, session_id, "user", body.text)
    except store.Gone:
        raise store.NotFound from None

    events = asyncio.Queue()

    async def emit(event, data):
        await events.put((event, data))

    async def turn():
        await emit("session", {"sessionId": session_id, "title": session.get("title"), "created": created})
        await emit("quota", status)
        try:
            result = await chat.run_turn(caller, prior, body.text, emit)
        except chat.TurnFailed as error:
            await emit("error", {"detail": str(error)})
            return
        try:
            message = await asyncio.to_thread(store.add_message, caller.user_id, session_id, "assistant",
                                              result["text"], result["tokenCount"], result["cost"], result["tools"])
        except store.Gone:
            logs.info("session deleted during its turn, answer dropped", userId=caller.user_id,
                      chatSessionId=session_id)
            await emit("error", {"detail": "This chat was deleted."})
            return
        await emit("message", store.message_view(message))
        await asyncio.to_thread(store.publish_turn, caller.user_id, session_id, message["messageId"],
                                result["tokenCount"], result["cost"])

    async def run():
        try:
            await turn()
        except Exception as error:
            logs.error("chat turn crashed", userId=caller.user_id, error=repr(error), trace=traceback.format_exc())
            await emit("error", {"detail": "Something went wrong on our side. Try again in a moment."})
        finally:
            await events.put(None)

    task = asyncio.create_task(run())
    _turns.add(task)
    task.add_done_callback(_turns.discard)

    async def stream():
        while True:
            try:
                item = await asyncio.wait_for(events.get(), timeout=HEARTBEAT_SECONDS)
            except TimeoutError:
                yield ": ping\n\n"
                continue
            if item is None:
                yield "event: done\ndata: {}\n\n"
                return
            event, data = item
            yield f"event: {event}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
