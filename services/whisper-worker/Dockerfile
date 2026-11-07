# Whisper worker: faster-whisper on CPU (int8), Python 3.14 on arm64 for Fargate.
# Build and push with `make push-whisper-worker`: scripts/push_image.sh passes services/shared as the named build
# context "shared", so the image gets the event contract without copying it into this folder.
FROM python:3.14-slim

# The checkpoint, chosen by measurement (bench.py). Another one: --build-arg MODEL=distil-medium.en
ARG MODEL=distil-small.en
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 MODEL_NAME=${MODEL} MODEL_DIR=/opt/model
WORKDIR /srv

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Weights are baked in at build time: no download at task start (faster cold start, no Hugging Face dependency).
RUN python -c "import os; from faster_whisper import download_model; download_model(os.environ['MODEL_NAME'], output_dir=os.environ['MODEL_DIR'])"
ENV HF_HUB_OFFLINE=1

COPY --from=shared contracts/__init__.py contracts/events.py contracts/logs.py contracts/
COPY worker.py transcriber.py ./

USER nobody
CMD ["python", "worker.py"]
