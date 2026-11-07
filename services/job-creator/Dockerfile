# Job creator: Lambda container image, Python 3.14 on arm64, plus static ffmpeg/ffprobe.
# Build and push with `make push-job-creator`: scripts/push_image.sh passes services/shared as the named
# build context "shared", so the image gets the event contract without copying it into this folder.
FROM mwader/static-ffmpeg:7.1.1 AS ffmpeg

FROM public.ecr.aws/lambda/python:3.14

COPY --from=ffmpeg /ffmpeg /ffprobe /usr/local/bin/
COPY --from=shared contracts/__init__.py contracts/events.py contracts/logs.py ${LAMBDA_TASK_ROOT}/contracts/
COPY handler.py media.py ${LAMBDA_TASK_ROOT}/

CMD ["handler.handler"]
