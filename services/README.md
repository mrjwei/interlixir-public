# services

One folder per deployable unit. Lambdas use `python3.14`; the containers are Python too (FastAPI for the API, faster-whisper for the worker). Infrastructure is created by hand in the AWS console; each README lists the settings its service needs.

| Service | Runs on | Triggered by |
| --- | --- | --- |
| `api/` | ECS Fargate, private subnets | the auth gateway's HTTP API `/a3/*` → the shared ALB |
| `job-creator/` | Lambda container image + ffmpeg | SQS upload queue: S3 `ObjectCreated:Post` on `recordings/`, and EventBridge `RetryRequested` (rule → the same queue) |
| `whisper-worker/` | ECS Fargate (0–8) | SQS transcription queue |
| `synthesis/` | Lambda | SQS synthesis queue |
| `stream-router/` | Lambda (not in VPC) | DynamoDB Stream (filtered) |
| `dlq-handler/` | Lambda (not in VPC) | SQS: the upload, transcription and synthesis DLQs |
| `history-writer/` | Lambda (not in VPC) | SQS `interlixir-history`, fed by the rule for the 5 history events |
| `gc/` | Lambda (not in VPC) | SQS `interlixir-gc`, fed by the rule for `DeleteRequested` |
| `chat-agent/` | ECS Fargate behind ALB | CloudFront `/acp/*` |
| `mcp-server/` | Sidecar container in the chat-agent task | chat-agent only (localhost) |
| `shared/` | library | — |
