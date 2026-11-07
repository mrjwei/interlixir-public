# services

One folder per deployable unit (maintainability NFR). Lambdas use `python3.14`; the containers are Python too (FastAPI for the API, faster-whisper for the worker). Each is created in the AWS console and recorded in the deployment notes.

| Service | Runs on | Triggered by | Milestone |
| --- | --- | --- | --- |
| `api/` | ECS Fargate, private subnets | the auth gateway's HTTP API `/a3/*` → the shared ALB | M4, M20 |
| `job-creator/` | Lambda container image + ffmpeg | SQS upload queue: S3 `ObjectCreated:Post` on `recordings/`, and EventBridge `RetryRequested` (rule → the same queue) | M6 |
| `whisper-worker/` | ECS Fargate (0–8) | SQS transcription queue | M7, M10 |
| `synthesis/` | Lambda | SQS synthesis queue | M8 |
| `stream-router/` | Lambda (not in VPC) | DynamoDB Stream (filtered) | M3 |
| `dlq-handler/` | Lambda (not in VPC) | SQS: the upload, transcription and synthesis DLQs | M11 |
| `history-writer/` | Lambda (not in VPC) | SQS `interlixir-history`, fed by the rule for the 5 history events | M13 |
| `gc/` | Lambda (not in VPC) | SQS `interlixir-gc`, fed by the rule for `DeleteRequested` | M20 |
| `chat-agent/` | ECS Fargate behind ALB | CloudFront `/acp/*` | M18 |
| `mcp-server/` | Sidecar container in the chat-agent task | chat-agent only (localhost) | M18 |
| `shared/` | library | — | M3+ |
