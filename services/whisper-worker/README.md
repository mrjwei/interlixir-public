# services/whisper-worker

Transcription worker on **ECS Fargate** (arm64), Python 3.14 + faster-whisper 1.2.1 (CTranslate2 4.8.3) on CPU with int8 weights. It takes one 5-minute chunk at a time from the transcription queue, writes its transcript to S3, and hands a job to synthesis once every chunk is done. It scales 0–8 on queue backlog from M10.

| File | What |
| --- | --- |
| `worker.py` | The loop: long poll → protection on → heartbeat → transcribe or skip → `ADD doneChunks` → claim + `SynthesisRequest` → delete → protection off. SIGTERM handling, structured JSON logs |
| `transcriber.py` | faster-whisper settings in one place (English, beam 5, VAD, no conditioning on previous text), the WAV reader, the vCPU count from the task metadata |
| `bench.py` | `make bench-whisper`: speed vs word error rate for several checkpoints on English clips with known text (how the model was chosen) |
| `Dockerfile` | `python:3.14-slim` + the requirements + **the model downloaded at build time** + the shared event contract (named build context `shared`) |
| `tests/` | `make test-whisper-worker` (moto; the real-model tests run when the model is in the local Hugging Face cache). One-time setup: `make venv-whisper-worker` |

## Model: `distil-small.en`

Chosen by measurement (`make bench-whisper`, full output and caveats in the build logs), on English clips with known text: six accents (AU, GB, IE, IN, ZA, US), crosstalk and noise, plus a 5-minute chunk for speed. Apple M1 Pro, the worker's own settings:

| Model | WER (7 clips, 336 words) | 5-min chunk, 1 thread | 5-min chunk, 2 threads | Peak RSS |
| --- | --- | --- | --- | --- |
| **distil-small.en** | 0.6% | **41.2 s** (RTF 0.137) | 26.5 s | **889 MB** |
| distil-medium.en | 0.6% | 92.5 s (RTF 0.308) | 55.5 s | 1409 MB |
| distil-large-v3.5 | 0.9% | 155.6 s (RTF 0.519) | 84.6 s | 1639 MB |
| small.en (not distilled, baseline) | 0.6% | 58.2 s (RTF 0.194) | 45.1 s | 1036 MB |

- Accuracy is a tie on synthesised speech (the remaining errors are US spellings of the British reference text), so **speed and memory decide**: distil-small.en is 1.4–3.8× faster than the others and the smallest.
- The 8th clip (Indian-English voice) came out garbled from `say` in this run: every model produced the same broken text, and re-renders of the same sentence vary in length and words. It's excluded above and explained in the evidence file.
- 2 threads are 1.3–1.8× faster than 1, not 2×, so per vCPU-second the **1-vCPU task** is cheaper; throughput comes from more tasks.
- Before relying on accuracy in the report, re-run with real recordings and hand-checked transcripts: `make bench-whisper ARGS="--samples <dir>"` (`<name>.<audio>` + `<name>.txt`).
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
| ECS has already picked the task to stop (a scale-in caught it between chunks) | Turning protection on is refused with `DEPLOYMENT_BLOCKED`: the message is handed back untouched, the worker logs `released, task is being stopped` and exits instead of starting a chunk the coming SIGTERM would abandon (M10 run 1) |
| SIGTERM elsewhere | Finishes the current short step, then exits; a message received after the signal is handed back untouched |
| The task dies | The message comes back within 300 s (the heartbeat's last extension) |

**A stop is not an attempt.** Each receive counts toward the 3 before the DLQ, and a step-down stops several tasks at once. In the M17 burst (run 1, 8 → 4 tasks with chunks still queued) one message was received and released by three stopping tasks in a row, went to the DLQ and failed its job without ever being transcribed. So "handed back" means: the body is sent again as a **new message** (receive count 0) and the received one is deleted. The copy carries the first message's ID in the `claimId` message attribute, because the G8 claim is keyed on it. If the send fails (e.g. `sqs:SendMessage` on `-transcription` denied, the account's permission limits), the worker logs `could not hand back as a new message, released instead` and releases the message as before. If the delete fails, the chunk is queued twice, which the transcript check and the `doneChunks` set absorb. Real failures still back off and reach the DLQ after 3 receives.

## Logs

One JSON object per line on stdout (`awslogs` → `/ecs/interlixir-whisper-worker`), always with `level` and `msg`, and `jobId` + `chunk` on everything about a chunk:

- `ready`: `model`, `beamSize`, `cpuThreads`, `loadMs`
- `transcribed`: `audioSeconds`, `segments`, `downloadMs`, `transcribeMs`, **`rtf`** (processing time ÷ audio time: the real speed of the task size), `totalMs`
- `chunk recorded` (`chunksDone`), `synthesis requested` (`retryCount`), `transcript already written`, `skipped: …`
- `task protection works` (first success) / `task protection not permitted, continuing without it`: settles the account's permission limits
- `chunk failed, will be retried` (`error`, `trace`), `released on stop, …`, `released, task is being stopped` (all with `newMessageId`: the copy's ID, or null if released instead), `stop requested`, `stopped`

Logs Insights, one job: `fields @timestamp, msg, chunk, transcribeMs, rtf | filter jobId = "<jobId>" | sort @timestamp`.

## Console settings

| Setting | Value |
| --- | --- |
| ECR repo | `interlixir-whisper-worker`, both tags (check `purpose` stuck). Push with `make push-whisper-worker` |
| Task definition | Family `interlixir-whisper-worker`, Fargate, **Linux/ARM64**, **1 vCPU / 2 GB** (peak RSS 0.9 GB; 2 GB is Fargate's minimum for 1 vCPU), task role `ecs-task-role`, execution role `ecs-execution-role`, ephemeral storage default (20 GiB; a chunk is ~10 MB, the image ~0.85 GB), both tags |
| Container | `worker`, the pushed image URI, essential, **no port mappings** (it listens on nothing), **stop timeout 120 s**, log driver `awslogs` with log group `/ecs/interlixir-whisper-worker` (let ECS create it; no retention, row 25). No environment: code defaults (table, bucket, queue URLs, bus; threads = the task's vCPUs; `FARGATE_PRICE_PER_HOUR` 0.04737 for the cost meter, M16: change it with the task size) |
| ECS service | `interlixir-whisper-worker` in `interlixir-ecs-cluster`, capacity provider FARGATE, **desired 0** (auto scaling owns it from M10; set 1 for the M7 check, then 0), no load balancer, no service auto scaling yet, propagate tags from the task definition, both tags |
| Networking | **Private subnets 1A/2A/3A, public IP off**, `AppSG`. It listens on nothing, so the SG's open ports expose nothing either way; private keeps one rule for every service. Image layers and chunk audio come through the S3 gateway endpoint (no NAT charge); SQS, DynamoDB and EventBridge calls are small |
| Transcription queue | Change visibility timeout 900 s → **300 s** (= `VISIBILITY_SECONDS`): the heartbeat keeps a busy message hidden, so the queue default only matters if a task dies within the first minute |

## Auto scaling on backlog

Step scaling with **`ExactCapacity`** on the transcription queue's depth = **visible + in flight** (a busy worker's message is in flight, so depth stays > 0 while anything is being transcribed). Two alarms, two policies, one scalable target. It works from 0 tasks, where backlog-per-task target tracking would divide by zero.

How the pieces behave (AWS docs, checked 2026-10-16):
- An alarm with an Auto Scaling action **re-invokes its policy every minute it stays in ALARM** (other alarm actions fire only on a state change). So while the queue drains, the scale-out policy keeps setting the step for the *current* depth: 4 tasks at depth 13–24, then 2 at 5–12, then 1. Capacity steps **down** as well as up.
- That step-down is a scale-in, and it's where task protection matters: ECS lowers the desired count but doesn't stop a task while its protection is on, so only tasks between chunks are stopped. `make watch-scaling` checks this from ECS's stopped-task reasons and the worker's log.
- Step bounds are **offsets from the alarm threshold**. Above the threshold the lower bound is inclusive and the upper exclusive; below it the reverse.
- `ExactCapacity` is clamped to the target's min and max, so the scale-in policy's "set to 0" means "set to min": 1 during the demo, 0 otherwise.
- Cooldowns only apply between policy-driven activities. A scale-out during a scale-in cooldown happens at once.

### 1. Two alarms (CloudWatch → Alarms → Create alarm → Select metric → SQS → Queue metrics, then Add math)

Both use the same expression. `m1` = `ApproximateNumberOfMessagesVisible`, `m2` = `ApproximateNumberOfMessagesNotVisible`, both `QueueName = interlixir-transcription`, statistic **Maximum**, period **1 minute**. `e1` = `FILL(m1, 0) + FILL(m2, 0)`, label "Transcription backlog". Alarm on `e1` only (untick `m1`, `m2`).

| Alarm | Condition | Datapoints | Missing data | Actions |
| --- | --- | --- | --- | --- |
| `interlixir-transcription-backlog` (scale-out) | `e1` **>= 1** | **1 out of 1** (react within a minute or two; SQS metrics arrive late anyway) | not breaching | none here: the ECS policy adds itself in step 2 |
| `interlixir-transcription-idle` (scale-in) | `e1` **<= 0** | **10 out of 10** (empty for 10 minutes: absorbs the gap between one upload's chunks and the next) | **breaching** (a queue idle for 6 h stops publishing; that still means "nothing to do") | none here |

Both tags on each, if the wizard offers a tags section; otherwise in the alarm's Tags tab after saving.

### 2. Service auto scaling (ECS → Clusters → `interlixir-ecs-cluster` → `interlixir-whisper-worker` → Update service → Service auto scaling)

| Setting | Value |
| --- | --- |
| Use service auto scaling | on. **Minimum 0**, **maximum 8**. Leave the desired count at 0 (the target owns it from now on). **For the demo: minimum 1**, the one edit the design allows |
| Policy 1 | Step scaling, name `interlixir-worker-scale-out`, alarm `interlixir-transcription-backlog`, adjustment type **ExactCapacity** (the console may call it "Set to"), metric aggregation **Maximum**, cooldown **60 s**, steps (bounds relative to the threshold 1): |
| | lower **0**, upper **4** → **1** task (depth 1–4) |
| | lower **4**, upper **12** → **2** (depth 5–12) |
| | lower **12**, upper **24** → **4** (depth 13–24) |
| | lower **24**, upper *(blank = +∞)* → **8** (depth ≥ 25) |
| Policy 2 | Step scaling, name `interlixir-worker-scale-in`, alarm `interlixir-transcription-idle`, adjustment type **ExactCapacity**, aggregation **Maximum**, cooldown default (300 s), one step: lower *(blank = −∞)*, upper **0** → **0** |

Saving adds each policy's ARN to its alarm's actions. The scalable target and the policies have no tag field in this form; policies can't be tagged at all.

**What actually happened (2026-10-18, the account's permission limits):** the form created the target and both policies, then failed on the last step. Its untagged `PutMetricAlarm` rewrite of each alarm hit an explicit deny (`Do-Not-Delete-LT1-DenyPolicy-4`). The fix was to re-put each alarm from the CLI with its full definition plus `AlarmActions` and both tags:

```bash
# alarm-<name>.json = describe-alarms output for that alarm, trimmed to put-metric-alarm's fields,
# plus "AlarmActions": ["<policy ARN>"] and the two tags
aws cloudwatch put-metric-alarm --cli-input-json file://alarm-backlog.json
aws cloudwatch put-metric-alarm --cli-input-json file://alarm-idle.json
```

Don't re-save this form's auto scaling section afterwards (it repeats the untagged rewrite). Change a policy with `put-scaling-policy` instead: an update keeps the policy's ARN, so the alarm's action still points at it.

**The form's step bounds appear to be absolute metric values, not offsets.** It seems to subtract the threshold: entering 4 / 12 / 24 stored 3 / 11 / 23 (depth 1–3 → 1, 4–11 → 2, 12–23 → 4, ≥ 24 → 8). To get the bands above, type 5 / 13 / 25, or use the CLI `put-scaling-policy` below, which takes offsets. (M10 run 1 hit this: depth 24 went to 8 tasks. The policy was corrected before run 2 and now reads back 0 / 4 / 12 / 24. The scale-in policy reads back aggregation `Average`; with its one step at ≤ 0 that changes nothing.)

**About 3–4 chunks per worker** is the G3 starting point. A 5-minute chunk of speech took 87–99 s on a 1-vCPU task (RTF 0.29–0.33, the build logs), so 24 chunks on 4 tasks is about 6 rounds ≈ 9–10 min, plus the scale-out lag. Re-tune the bounds after `make watch-scaling` shows the real lag and drain rate.

**If the ECS form can't do this** (no metric-math alarm in its alarm list, no exact-capacity type): create the same thing with the CLI. You run these; they're mutating, and the alarm then takes the policy ARN as its action (`put-metric-alarm --alarm-actions <PolicyARN>`, with the expression above in `--metrics`):

```bash
aws application-autoscaling register-scalable-target --service-namespace ecs --scalable-dimension ecs:service:DesiredCount \
  --resource-id service/interlixir-ecs-cluster/interlixir-whisper-worker --min-capacity 0 --max-capacity 8 \
  --tags 'project=interlixir'
aws application-autoscaling put-scaling-policy --service-namespace ecs --scalable-dimension ecs:service:DesiredCount \
  --resource-id service/interlixir-ecs-cluster/interlixir-whisper-worker --policy-name interlixir-worker-scale-out \
  --policy-type StepScaling --step-scaling-policy-configuration '{"AdjustmentType":"ExactCapacity","Cooldown":60,
  "MetricAggregationType":"Maximum","StepAdjustments":[{"MetricIntervalLowerBound":0,"MetricIntervalUpperBound":4,"ScalingAdjustment":1},
  {"MetricIntervalLowerBound":4,"MetricIntervalUpperBound":12,"ScalingAdjustment":2},
  {"MetricIntervalLowerBound":12,"MetricIntervalUpperBound":24,"ScalingAdjustment":4},
  {"MetricIntervalLowerBound":24,"ScalingAdjustment":8}]}'
```

(and the same `put-scaling-policy` for `interlixir-worker-scale-in` with `"StepAdjustments":[{"MetricIntervalUpperBound":0,"ScalingAdjustment":0}]`.)

### 3. Read-only checks, then the run

```bash
aws application-autoscaling describe-scalable-targets --service-namespace ecs --resource-ids service/interlixir-ecs-cluster/interlixir-whisper-worker
aws application-autoscaling describe-scaling-policies --service-namespace ecs --resource-id service/interlixir-ecs-cluster/interlixir-whisper-worker \
  --query 'ScalingPolicies[].{n:PolicyName,t:StepScalingPolicyConfiguration.AdjustmentType,s:StepScalingPolicyConfiguration.StepAdjustments,a:Alarms[].AlarmName}'
aws cloudwatch describe-alarms --alarm-names interlixir-transcription-backlog interlixir-transcription-idle \
  --query 'MetricAlarms[].{n:AlarmName,s:StateValue,c:ComparisonOperator,t:Threshold,e:EvaluationPeriods,m:TreatMissingData,a:AlarmActions}'
```

Expect: min 0 / max 8; both policies `ExactCapacity` with the steps above; each alarm's `AlarmActions` holds its policy's ARN; the backlog alarm `OK` and the idle alarm `ALARM` while the queue is empty (that's normal: it keeps setting min capacity, which changes nothing).

Then the 2-hour test: `make watch-scaling ARGS="--upload --minutes 120"` (24 chunks of real speech). It samples depth and tasks every 15 s, prints the scaling activities and the stopped tasks, and writes the build logs and `.svg` (the queue-depth-vs-tasks graph). Expect: 4 tasks within a few minutes, steps down to 2 then 1 as the queue drains, 0 about 10 minutes after the last chunk, and no task stopped by a scale-in that abandoned a chunk.

**Result (2026-10-18, run 2, the build logs):** a task running 138 s after the queue filled, 24 chunks in ~11 min on 4 tasks, 4 → 2 → 1 → 0, 4 tasks stopped by scale-in with no chunk abandoned; 5/5.

Milestones: **M7**, M10 (scaling, protection under scale-in), M11 (DLQ → `Failed`).
