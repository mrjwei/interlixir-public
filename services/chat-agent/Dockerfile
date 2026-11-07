# Chat agent image. Build with `make push-chat-agent` (linux/arm64, services/shared as the
# build context "shared").
FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /srv

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# The log and event contracts: logs.py for every line, events.py for the ChatSession event.
COPY --from=shared contracts/__init__.py contracts/logs.py contracts/events.py contracts/prices.py contracts/
COPY agent/ agent/

USER nobody
EXPOSE 8080
CMD ["uvicorn", "agent.main:app", "--host", "0.0.0.0", "--port", "8080"]
