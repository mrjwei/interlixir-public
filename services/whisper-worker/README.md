# services/whisper-worker

Transcription worker on **ECS Fargate** (arm64), Python 3.14 + faster-whisper 1.2.1 (CTranslate2 4.8.3) on CPU with int8 weights. It takes one 5-minute chunk at a time from the transcription queue, writes its transcript to S3, and hands a job to synthesis once every chunk is done. It scales 0–8 on queue backlog.

| File | What |
| --- | --- |
| `worker.py` | The loop: long poll → protection on → heartbeat → transcribe or skip → `ADD doneChunks` → claim + `SynthesisRequest` → delete → protection off. SIGTERM handling, structured JSON logs |
| `transcriber.py` | faster-whisper settings in one place (English, beam 5, VAD, no conditioning on previous text), the WAV reader, the vCPU count from the task metadata |
| `Dockerfile` | `python:3.14-slim` + the requirements + **the model downloaded at build time** + the shared event contract (named build context `shared`) |
| `tests/` | `make test-whisper-worker` (moto; the real-model tests run when the model is in the local Hugging Face cache). One-time setup: `make venv-whisper-worker` |

## Model: `distil-small.en`

Chosen by measurement on English clips with known text: six accents (AU, GB, IE, IN, ZA, US), crosstalk and noise, plus a 5-minute chunk for speed. Apple M1 Pro, the worker's own settings (a one-off benchmark; the script isn't part of this repo):

| Model | WER (7 clips, 336 words) | 5-min chunk, 1 thread | 5-min chunk, 2 threads | Peak RSS |
| --- | --- | --- | --- | --- |
| **distil-small.en** | 0.6% | **41.2 s** (RTF 0.137) | 26.5 s | **889 MB** |
| distil-medium.en | 0.6% | 92.5 s (RTF 0.308) | 55.5 s | 1409 MB |
| distil-large-v3.5 | 0.9% | 155.6 s (RTF 0.519) | 84.6 s | 1639 MB |
| small.en (not distilled, baseline) | 0.6% | 58.2 s (RTF 0.194) | 45.1 s | 1036 MB |

- Accuracy is a tie on synthesised speech (the remaining errors are US spellings of the British reference text), so **speed and memory decide**: distil-small.en is 1.4–3.8× faster than the others and the smallest.
- 2 threads are 1.3–1.8× faster than 1, not 2×, so per vCPU-second the **1-vCPU task** is cheaper; throughput comes from more tasks.
- Accuracy was measured on synthesised speech, so treat it as a sanity check rather than a real-world figure.
- Another checkpoint is a build argument (`--build-arg MODEL=distil-medium.en`); `MODEL_NAME` is recorded in every transcript.

## Per message

Input: `TranscriptionChunk` (`services/shared/contracts/messages.schema.json`) from `interlixir-transcription`.

1. **Task scale-in protection on**, through `$ECS_AGENT_URI/task-protection/v1/state` with a 60 min expiry. Off again after step 7. Outside a task (no `ECS_AGENT_URI`) it's skipped. If the role may not call it, the worker logs that once and carries on.
2. **Visibility heartbeat**: every 60 s the message's visibility is set to 300 s from now, for as long as the chunk takes.
3. The Job must exist, belong to the message's `userId`, and have the message's `chunksTotal`; otherwise the message is deleted (project deleted, or a message from an older chunking).
4. `transcripts/<userId>/<jobId>/chunks/<n>.json` already there → skip to 5 (a redelivery, or a chunk the job creator sent twice). Otherwise download `chunks/<jobId>/<n>.wav`, transcribe (language fixed to `en`, no detection pass) and put the transcript (contract: `services/shared/contracts/transcripts.schema.json`; segment times are from the start of the recording).
5. `ADD doneChunks :{n}`, conditional on the same user and `chunksTotal` (an `ADD` on a missing item would create it). `ReturnValues = ALL_NEW`: if the chunk was already in the set nothing changes, and the set must still come back.
6. If the set has `chunksTotal` members: conditional `SET synthesisEnqueued = <this message's ID>` (not set, or already this ID). The winner sends `SynthesisRequest` to `interlixir-synthesis` and puts `TranscriptionDone` on the bus (`eventId` = `transcriptiondone-<jobId>-<retryCount>`). A winner that dies before sending is finished by its own redelivery (same `MessageId`); no other message can take over.
7. Delete the message.

The stage stays `Transcribing N/N` until synthesis moves it on.

## Failures and shutdown

| What happens | What the worker does |
| --- | --- |
| Any error in steps 3–6 (S3, DynamoDB, SQS, EventBridge, a missing chunk WAV) | Logs `chunk failed, will be retried` with the error and trace; the message is visible again after 30 s × its receive count. After 3 receives it's in `-transcription-dlq`; the dead-letter handler (`services/dlq-handler/`) then marks the job `Failed`: "Part n of N … couldn't be transcribed" |
| A body that isn't a valid `TranscriptionChunk` | Same path, so it ends in the DLQ where it can be read |
| SIGTERM during transcription (deployment, Spot interruption, manual stop) | Abandons the chunk at the next segment, hands the message back (below), protection off, exits. Fargate allows `stopTimeout` (120 s max) |
| ECS has already picked the task to stop (a scale-in caught it between chunks) | Turning protection on is refused with `DEPLOYMENT_BLOCKED`: the message is handed back untouched, the worker logs `released, task is being stopped` and exits instead of starting a chunk the coming SIGTERM would abandon |
| SIGTERM elsewhere | Finishes the current short step, then exits; a message received after the signal is handed back untouched |
| The task dies | The message comes back within 300 s (the heartbeat's last extension) |

**A stop is not an attempt.** Each receive counts toward the 3 before the DLQ, and a step-down stops several tasks at once. In a load test (8 → 4 tasks with chunks still queued) one message was received and released by three stopping tasks in a row, went to the DLQ and failed its job without ever being transcribed. So "handed back" means: the body is sent again as a **new message** (receive count 0) and the received one is deleted. The copy carries the first message's ID in the `claimId` message attribute, because the G8 claim is keyed on it. If the send fails (e.g. `sqs:SendMessage` on `-transcription` denied), the worker logs `could not hand back as a new message, released instead` and releases the message as before. If the delete fails, the chunk is queued twice, which the transcript check and the `doneChunks` set absorb. Real failures still back off and reach the DLQ after 3 receives.

## Logs

One JSON object per line on stdout (`awslogs` → `/ecs/interlixir-whisper-worker`), always with `level` and `msg`, and `jobId` + `chunk` on everything about a chunk:

- `ready`: `model`, `beamSize`, `cpuThreads`, `loadMs`
- `transcribed`: `audioSeconds`, `segments`, `downloadMs`, `transcribeMs`, **`rtf`** (processing time ÷ audio time: the real speed of the task size), `totalMs`
- `chunk recorded` (`chunksDone`), `synthesis requested` (`retryCount`), `transcript already written`, `skipped: …`
- `task protection works` (first success) / `task protection not permitted, continuing without it`
- `chunk failed, will be retried` (`error`, `trace`), `released on stop, …`, `released, task is being stopped` (all with `newMessageId`: the copy's ID, or null if released instead), `stop requested`, `stopped`

Logs Insights, one job: `fields @timestamp, msg, chunk, transcribeMs, rtf | filter jobId = "<jobId>" | sort @timestamp`.

## Deployment

| Setting | Value |
| --- | --- |
| Image | ECR repo `interlixir-whisper-worker`; `make push-whisper-worker` builds the arm64 image and pushes it tagged by git SHA |
| Task | Fargate, Linux/ARM64, **1 vCPU / 2 GB** (peak RSS ≈ 0.9 GB; 2 GB is Fargate's minimum for 1 vCPU), default 20 GiB ephemeral storage. One container, no port mappings, stop timeout 120 s, `awslogs` to `/ecs/interlixir-whisper-worker`. No environment: code defaults for table, bucket, queue URLs and bus (threads = the task's vCPUs). `FARGATE_PRICE_PER_HOUR` feeds the cost log and should change with the task size |
| Service | `interlixir-whisper-worker`, capacity provider FARGATE, desired count 0 (auto scaling owns it), private subnets, no public IP, no load balancer |
| Transcription queue | Visibility timeout 300 s (= `VISIBILITY_SECONDS`). The heartbeat keeps a busy message hidden, so the queue default only matters if a task dies within its first minute |

Image layers and chunk audio come through the S3 gateway endpoint, so no NAT charge applies to the bulk traffic.

## Auto scaling on backlog

Step scaling with **`ExactCapacity`** on the transcription queue's depth (visible + in flight; a busy worker's message is in flight, so depth stays above 0 while anything is being transcribed). Two CloudWatch alarms drive two policies on one scalable target (min 0, max 8). Unlike backlog-per-task target tracking, it works from 0 tasks.

How the pieces behave:
- An alarm with an Auto Scaling action re-invokes its policy every minute it stays in ALARM. While the queue drains, the scale-out policy keeps setting the step for the *current* depth, so capacity steps down as well as up.
- That step-down is a scale-in, and it's where task protection matters: ECS lowers the desired count but doesn't stop a protected task, so only tasks between chunks are stopped.
- Step bounds are offsets from the alarm threshold. `ExactCapacity` is clamped to the target's min and max.

Alarms (both on `e1 = FILL(m1, 0) + FILL(m2, 0)`, where `m1`/`m2` are `ApproximateNumberOfMessagesVisible`/`NotVisible` on `interlixir-transcription`, Maximum, 1 minute):

| Alarm | Condition | Missing data |
| --- | --- | --- |
| `interlixir-transcription-backlog` (scale-out) | `e1 >= 1`, 1 of 1 datapoints | not breaching |
| `interlixir-transcription-idle` (scale-in) | `e1 <= 0`, 10 of 10 datapoints (absorbs the gap between one upload's chunks and the next) | breaching (an idle queue stops publishing metrics, which still means "nothing to do") |

Policies:

| Policy | Alarm | Steps (bounds relative to the threshold of 1) |
| --- | --- | --- |
| `interlixir-worker-scale-out` (cooldown 60 s) | backlog | depth 1–4 → 1 task, 5–12 → 2, 13–24 → 4, ≥ 25 → 8 |
| `interlixir-worker-scale-in` (default cooldown) | idle | depth 0 → 0 tasks |

```bash
aws application-autoscaling register-scalable-target --service-namespace ecs --scalable-dimension ecs:service:DesiredCount \
  --resource-id service/interlixir-ecs-cluster/interlixir-whisper-worker --min-capacity 0 --max-capacity 8
aws application-autoscaling put-scaling-policy --service-namespace ecs --scalable-dimension ecs:service:DesiredCount \
  --resource-id service/interlixir-ecs-cluster/interlixir-whisper-worker --policy-name interlixir-worker-scale-out \
  --policy-type StepScaling --step-scaling-policy-configuration '{"AdjustmentType":"ExactCapacity","Cooldown":60,
  "MetricAggregationType":"Maximum","StepAdjustments":[{"MetricIntervalLowerBound":0,"MetricIntervalUpperBound":4,"ScalingAdjustment":1},
  {"MetricIntervalLowerBound":4,"MetricIntervalUpperBound":12,"ScalingAdjustment":2},
  {"MetricIntervalLowerBound":12,"MetricIntervalUpperBound":24,"ScalingAdjustment":4},
  {"MetricIntervalLowerBound":24,"ScalingAdjustment":8}]}'
```

The scale-in policy is the same call with `"StepAdjustments":[{"MetricIntervalUpperBound":0,"ScalingAdjustment":0}]`. Each alarm then takes its policy's ARN as an alarm action (`put-metric-alarm --alarm-actions <PolicyARN>`).

**Sizing.** About 3–4 chunks per worker. A 5-minute chunk of speech takes roughly 90–100 s on a 1-vCPU task, so a 2-hour recording (24 chunks) on 4 tasks takes about 10 minutes plus the scale-out lag. In a 2-hour test run the queue went 4 → 2 → 1 → 0 tasks as it drained, and no scale-in abandoned a chunk.
