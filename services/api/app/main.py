"""A3 API service: every /a3 route, plus /healthz for the ALB target group.

Path: the auth gateway's HTTP API (JWT authorizer) -> ANY /a3/{proxy+} with a secret header -> the shared ALB -> this task.
Run locally: PYTHONPATH=../shared uvicorn app.main:app --port 8080 (from services/api).

Every request but /healthz logs one line: the route, status, durationMs and, once the token is verified,
the userId, plus jobId/projectId from the path. The line is also the RequestLatency metric by Route and in
total; a 5xx adds an error line (the Errors metric for Service api).
"""

import time
import traceback

from contracts import logs
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from . import store
from .routes import documents, history, jobs, projects, schemas, uploads

logs.configure("api")

app = FastAPI(title="interlixir-api")

for module in (projects, uploads, documents, jobs, schemas, history):
    app.include_router(module.router)


@app.exception_handler(store.NotFound)
def not_found(request: Request, exc: store.NotFound):
    return JSONResponse({"detail": "Not found"}, status_code=404)


@app.exception_handler(store.Conflict)
def conflict(request: Request, exc: store.Conflict):
    return JSONResponse({"detail": str(exc)}, status_code=409)


@app.middleware("http")
async def log_request(request: Request, call_next):
    if request.url.path == "/healthz":  # the ALB's every 30 s: noise, not traffic
        return await call_next(request)
    started, status, failure = time.monotonic(), 500, None
    try:
        response = await call_next(request)
        status = response.status_code
        return response
    except Exception as error:  # Starlette answers 500; the trace is only here
        failure = {"error": repr(error), "trace": traceback.format_exc()}
        raise
    finally:
        # The router leaves the matched route and its path parameters in the shared scope.
        route = request.scope.get("route")
        name = f"{request.method} {route.path}" if route is not None else "unmatched"
        params = request.scope.get("path_params", {})
        fields = {
            "method": request.method, "route": name, "status": status,
            "userId": getattr(request.state, "user_id", None),
            "jobId": params.get("job_id"), "projectId": params.get("project_id"),
        }
        duration = logs.ms_since(started)
        logs.metric("INFO" if status < 500 else "ERROR", "request",
                    {"RequestLatency": (duration, "Milliseconds")}, {"Route": name}, durationMs=duration, **fields)
        if status >= 500:
            logs.error("request failed", **fields, **(failure or {}))


@app.get("/healthz", include_in_schema=False)
def healthz():
    """Liveness only: no DynamoDB or cache call, so a dependency blip doesn't make the ALB drain every task."""
    return {"status": "ok"}
