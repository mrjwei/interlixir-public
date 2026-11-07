# MCP server image: the sidecar in the chat-agent task. Build with `make push-mcp-server`
# (linux/arm64, services/shared as the build context "shared").
FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /srv

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY --from=shared contracts/__init__.py contracts/logs.py contracts/
COPY mcp_server/ mcp_server/

USER nobody
# Loopback only: the agent in the same task is the only client (awsvpc tasks share one network namespace).
# No access log: every tool call already writes its own JSON line (server.py).
CMD ["uvicorn", "mcp_server.server:app", "--host", "127.0.0.1", "--port", "8081", "--no-access-log"]
