import type { Job, Stage } from './types'

export type Tone = 'success' | 'info' | 'neutral' | 'destructive'

export const STEPS: Stage[] = ['Queued', 'Transcribing', 'Redacting', 'Synthesising', 'Done']

export function isTerminal(stage: Stage | undefined) {
  return stage === 'Done' || stage === 'Failed'
}

export function chunkProgress(job: Pick<Job, 'chunksDone' | 'chunksTotal'>) {
  return job.chunksTotal ? `${job.chunksDone}/${job.chunksTotal}` : null
}

export function stageTone(stage: Stage): Tone {
  if (stage === 'Done') return 'success'
  if (stage === 'Failed') return 'destructive'
  if (stage === 'Queued') return 'neutral'
  return 'info'
}

/** Short status for the sidebar: "Transcribing 2/3". */
export function stageShort(job: Job) {
  if (job.stage === 'Transcribing') {
    const chunks = chunkProgress(job)
    return chunks ? `Transcribing ${chunks}` : 'Transcribing'
  }
  return job.stage
}

/** The stage column on the project page. */
export function stageDetail(job: Job) {
  switch (job.stage) {
    case 'Queued':
      return 'Waiting to start'
    case 'Transcribing': {
      const chunks = chunkProgress(job)
      return chunks ? `Transcribing ${chunks} chunks` : 'Preparing audio'
    }
    case 'Redacting':
      return 'Removing personal details'
    case 'Synthesising':
      return 'Writing the document'
    case 'Done':
      return 'Synthesised'
    case 'Failed':
      return job.failureReason || 'Processing failed.'
  }
}

/**
 * The stepper index where a Failed job stopped. The job doesn't record it, so infer it from how far the
 * chunks got: none planned → Queued (the job creator failed), some left → Transcribing, all done →
 * Synthesising (redaction and synthesis run in the same Lambda, and synthesis is the likelier failure).
 */
export function failedStepIndex(job: Job) {
  if (!job.chunksTotal) return 0
  if (job.chunksDone < job.chunksTotal) return 1
  return 3
}
