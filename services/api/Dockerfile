# API service image. Fargate runs it on arm64, so build for that platform with `make push-api`:
#   docker buildx build --platform linux/arm64 --build-context shared=services/shared -t <ecr repo>:<git sha> services/api
FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /srv

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# The shared log contract; scripts/push_image.sh passes services/shared as the build context "shared".
COPY --from=shared contracts/__init__.py contracts/logs.py contracts/
COPY app/ app/

USER nobody
EXPOSE 8080
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
