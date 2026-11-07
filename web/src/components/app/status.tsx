import { Check, CircleCheck, CircleX, Clock, LoaderCircle } from 'lucide-react'
import { Badge } from '@/components/ui/badge'
import { STEPS, chunkProgress, failedStepIndex, stageTone } from '@/lib/jobs'
import type { Job, Stage } from '@/lib/types'
import { cn } from '@/lib/utils'

export function StageIcon({ stage, className }: { stage: Stage; className?: string }) {
  if (stage === 'Done') return <CircleCheck className={className} />
  if (stage === 'Failed') return <CircleX className={className} />
  if (stage === 'Queued') return <Clock className={className} />
  return <LoaderCircle className={cn('animate-spin', className)} />
}

export function StatusBadge({ stage }: { stage: Stage }) {
  return (
    <Badge variant={stageTone(stage)}>
      <StageIcon stage={stage} />
      {stage}
    </Badge>
  )
}

/** Queued → Transcribing → Redacting → Synthesising → Done, with chunk progress under Transcribing. */
export function StageStepper({ job }: { job: Job }) {
  const failed = job.stage === 'Failed'
  const current = failed ? failedStepIndex(job) : STEPS.indexOf(job.stage)
  const finished = job.stage === 'Done'
  const chunks = chunkProgress(job)
  const pct = job.chunksTotal ? (job.chunksDone / job.chunksTotal) * 100 : 0

  return (
    <ol className="grid grid-cols-5 gap-2" aria-label="Processing stages">
      {STEPS.map((label, i) => {
        const done = finished || i < current
        const isCurrent = !finished && i === current
        const detail = isCurrent ? (failed ? 'Failed here' : label === 'Transcribing' && chunks ? `${chunks} chunks` : null) : null
        return (
          <li key={label} className="flex min-w-0 flex-col gap-2" aria-current={isCurrent ? 'step' : undefined}>
            <div className="flex items-center gap-2">
              <span
                className={cn(
                  'inline-flex size-8 shrink-0 items-center justify-center rounded-full border text-xs font-medium tabular-nums',
                  done && 'border-primary bg-primary text-primary-foreground',
                  isCurrent && !failed && 'tint-info',
                  isCurrent && failed && 'tint-destructive',
                  !done && !isCurrent && 'border-border bg-card text-muted-foreground',
                )}
              >
                {done ? (
                  <Check className="size-4" />
                ) : isCurrent && failed ? (
                  <CircleX className="size-4" />
                ) : isCurrent && label !== 'Queued' ? (
                  <LoaderCircle className="size-4 animate-spin" />
                ) : isCurrent ? (
                  <Clock className="size-4" />
                ) : (
                  i + 1
                )}
              </span>
              {i < STEPS.length - 1 && (
                <span className={cn('h-px flex-1', i < current || finished ? 'bg-primary' : 'bg-border')} />
              )}
            </div>
            <div className="flex min-w-0 flex-col gap-1">
              <span
                className={cn(
                  'truncate text-sm font-medium max-sm:text-xs',
                  !done && !isCurrent ? 'text-muted-foreground' : 'text-foreground',
                  isCurrent && failed && 'text-destructive',
                )}
              >
                {label}
              </span>
              {detail && <span className="truncate text-xs text-muted-foreground">{detail}</span>}
              {isCurrent && !failed && label === 'Transcribing' && pct > 0 && (
                <div className="h-1 overflow-hidden rounded-full bg-secondary">
                  <div className="h-full rounded-full bg-primary transition-[width] duration-[260ms]" style={{ width: `${pct}%` }} />
                </div>
              )}
            </div>
          </li>
        )
      })}
    </ol>
  )
}
