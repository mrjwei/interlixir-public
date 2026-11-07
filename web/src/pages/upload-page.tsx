import { useQueryClient } from '@tanstack/react-query'
import { AudioLines, Check, CircleAlert, CloudUpload, FileText, FolderOpen, LoaderCircle, Paperclip, RotateCcw, Upload, X } from 'lucide-react'
import { useEffect, useRef, useState, type DragEvent } from 'react'
import { Link, useParams } from 'react-router'
import { Page } from '@/components/app/shell'
import { Alert } from '@/components/ui/alert'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardDescription, CardTitle } from '@/components/ui/card'
import { Label, Textarea } from '@/components/ui/input'
import { Progress } from '@/components/ui/progress'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectSeparator,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import { api, ApiError } from '@/lib/api'
import { formatBytes } from '@/lib/format'
import { keys, useProject, useSchemas } from '@/lib/queries'
import type { PostPolicy } from '@/lib/types'
import {
  CONTEXT_EXTENSIONS,
  MAX_CONTEXT_FILES,
  MAX_CONTEXT_TOTAL_BYTES,
  MAX_INSTRUCTION_CHARS,
  MAX_RECORDINGS,
  MiB,
  RECORDING_EXTENSIONS,
  contextProblem,
  postToS3,
  recordingProblem,
} from '@/lib/upload'
import { cn } from '@/lib/utils'

type FileState = 'ready' | 'uploading' | 'success' | 'error'

interface Item {
  key: string
  file: File
  problem: string | null // can't be uploaded at all (format, size)
  state: FileState
  progress: number
  error?: string // the S3 POST failed; retry reuses the policy while it's valid
  policy?: PostPolicy
  jobId?: string
}

let seq = 0
const toItems = (files: FileList | File[], check: (f: File) => string | null): Item[] =>
  Array.from(files).map((file) => ({ key: `f${seq++}`, file, problem: check(file), state: 'ready', progress: 0 }))

export function UploadPage() {
  const { projectId } = useParams()
  const project = useProject(projectId)
  const schemas = useSchemas()
  const queryClient = useQueryClient()

  const [chosenSchema, setSchemaId] = useState<string>('default')
  const [instruction, setInstruction] = useState('')
  const [recordings, setRecordings] = useState<Item[]>([])
  const [contexts, setContexts] = useState<Item[]>([])
  const [phase, setPhase] = useState<'editing' | 'uploading' | 'done'>('editing')
  const [requestError, setRequestError] = useState<string | null>(null)
  const recordingInput = useRef<HTMLInputElement>(null)
  const contextInput = useRef<HTMLInputElement>(null)


  // Leaving mid-upload abandons the POSTs: ask first.
  useEffect(() => {
    if (phase !== 'uploading') return
    const warn = (e: BeforeUnloadEvent) => e.preventDefault()
    window.addEventListener('beforeunload', warn)
    return () => window.removeEventListener('beforeunload', warn)
  }, [phase])

  const builtIn = schemas.data?.filter((s) => s.builtIn) ?? []
  const own = schemas.data?.filter((s) => !s.builtIn) ?? []
  // 'default' is a built-in; if it's ever missing, fall back to the first schema rather than a 422.
  const schemaId =
    schemas.data && !schemas.data.some((s) => s.schemaId === chosenSchema)
      ? (schemas.data[0]?.schemaId ?? chosenSchema)
      : chosenSchema
  const schema = schemas.data?.find((s) => s.schemaId === schemaId)

  const validRecordings = recordings.filter((r) => !r.problem)
  const invalidRecordings = recordings.length - validRecordings.length
  const tooManyRecordings = recordings.length > MAX_RECORDINGS
  const contextBytes = contexts.reduce((n, c) => n + c.file.size, 0)
  const contextOver = contextBytes > MAX_CONTEXT_TOTAL_BYTES
  const tooManyContexts = contexts.length > MAX_CONTEXT_FILES
  const invalidContexts = contexts.some((c) => c.problem)
  const instructionOver = instruction.trim().length > MAX_INSTRUCTION_CHARS
  const editing = phase === 'editing'
  const canUpload =
    editing &&
    validRecordings.length > 0 &&
    invalidRecordings === 0 &&
    !tooManyRecordings &&
    !contextOver &&
    !tooManyContexts &&
    !invalidContexts &&
    !instructionOver

  function patch(setter: typeof setRecordings, key: string, change: Partial<Item>) {
    setter((list) => list.map((i) => (i.key === key ? { ...i, ...change } : i)))
  }

  async function send(setter: typeof setRecordings, item: Item, policy: PostPolicy) {
    patch(setter, item.key, { state: 'uploading', progress: 0, error: undefined, policy })
    try {
      await postToS3(policy, item.file, (f) => patch(setter, item.key, { progress: f * 100 }))
      patch(setter, item.key, { state: 'success', progress: 100 })
      return true
    } catch (err) {
      patch(setter, item.key, { state: 'error', error: (err as Error).message })
      return false
    }
  }

  async function upload() {
    if (!canUpload || !projectId) return
    setPhase('uploading')
    setRequestError(null)
    let response
    try {
      // One call pre-creates every Recording, Job and ContextFile and returns a POST policy per file.
      // contentType is left empty: the server then fixes the canonical type for the extension.
      response = await api.createUploads(projectId, {
        schemaId,
        instructionText: instruction.trim(),
        recordings: recordings.map((r) => ({ fileName: r.file.name, contentType: '', sizeBytes: r.file.size })),
        contextFiles: contexts.map((c) => ({ fileName: c.file.name, contentType: '', sizeBytes: c.file.size })),
      })
    } catch (err) {
      setRequestError(err instanceof ApiError ? err.message : 'Upload could not start.')
      setPhase('editing')
      return
    }
    void queryClient.invalidateQueries({ queryKey: keys.project(projectId) }) // the new jobs appear as Queued

    // Context files first: they are small, and synthesis reads them once the recordings are transcribed.
    await Promise.all(contexts.map((c, i) => send(setContexts, c, response.contextFiles[i].upload)))
    setRecordings((list) => list.map((r, i) => ({ ...r, jobId: response.recordings[i].jobId })))
    await Promise.all(recordings.map((r, i) => send(setRecordings, r, response.recordings[i].upload)))
    setPhase('done')
  }

  async function retry(setter: typeof setRecordings, item: Item) {
    if (!item.policy) return
    setPhase('uploading')
    await send(setter, item, item.policy)
    setPhase('done')
  }

  function reset() {
    setRecordings([])
    setContexts([])
    setInstruction('')
    setPhase('editing')
  }

  function onDrop(e: DragEvent) {
    e.preventDefault()
    if (!editing) return
    const added = toItems(e.dataTransfer.files, recordingProblem) // the FileList is only valid during the event
    if (added.length) setRecordings((l) => [...l, ...added])
  }

  const uploaded = recordings.filter((r) => r.state === 'success')
  const failed = [...recordings, ...contexts].filter((r) => r.state === 'error')
  const projectName = project.data?.name ?? 'Project'

  let note: string
  if (phase === 'uploading') note = 'Keep this page open until uploads finish.'
  else if (phase === 'done' && failed.length === 0) note = `${uploaded.length} ${uploaded.length === 1 ? 'recording' : 'recordings'} uploaded`
  else if (phase === 'done') note = `${failed.length} ${failed.length === 1 ? 'file' : 'files'} didn't upload. Retry them, or start a new upload.`
  else if (recordings.length === 0) note = 'No recordings selected'
  else if (tooManyRecordings) note = `Remove ${recordings.length - MAX_RECORDINGS} to stay within ${MAX_RECORDINGS} recordings.`
  else if (invalidRecordings) {
    const total = validRecordings.reduce((n, r) => n + r.file.size, 0)
    note = `${validRecordings.length} ready · ${formatBytes(total)}. Remove the ${invalidRecordings} with errors to continue.`
  } else {
    const total = recordings.reduce((n, r) => n + r.file.size, 0)
    note = `${recordings.length} ${recordings.length === 1 ? 'recording' : 'recordings'} ready · ${formatBytes(total)}`
  }

  return (
    <Page
      width="form"
      crumbs={[{ label: projectName, to: `/projects/${projectId}` }, { label: 'New upload' }]}
    >
      <div className="flex flex-col gap-5">
        <div className="flex flex-col gap-1.5">
          <h1 className="font-serif text-[40px] font-normal leading-[1.2] tracking-[-0.01em]">New upload</h1>
          <p className="text-muted-foreground">
            Add recordings to {projectName}. Each recording becomes its own document.
          </p>
        </div>

        {phase === 'done' && failed.length === 0 && (
          <Alert tone="success" title={`${uploaded.length} ${uploaded.length === 1 ? 'recording' : 'recordings'} uploaded.`}>
            <div className="flex flex-col items-start gap-2">
              <span>Each one becomes its own document. You can leave this page; processing carries on.</span>
              <div className="flex flex-wrap gap-x-4 gap-y-1">
                {recordings[0]?.jobId && (
                  <Link className="font-medium text-foreground underline underline-offset-4" to={`/projects/${projectId}/jobs/${recordings[0].jobId}`}>
                    View progress
                  </Link>
                )}
                <button type="button" className="font-medium text-foreground underline underline-offset-4" onClick={reset}>
                  Upload more
                </button>
              </div>
            </div>
          </Alert>
        )}
        {requestError && <Alert tone="destructive" title="Upload could not start">{requestError}</Alert>}

        <Card className="flex flex-col gap-4 p-6">
          <CardTitle>Schema</CardTitle>
          <div className="flex flex-col gap-2">
            <Label htmlFor="schema">Synthesis schema</Label>
            <Select value={schemaId} onValueChange={setSchemaId} disabled={!editing || !schemas.data}>
              <SelectTrigger id="schema">
                <SelectValue placeholder={schemas.isLoading ? 'Loading schemas…' : 'Choose a schema'} />
              </SelectTrigger>
              <SelectContent>
                {builtIn.map((s) => (
                  <SelectItem key={s.schemaId} value={s.schemaId}>
                    {s.name ?? s.schemaId}
                  </SelectItem>
                ))}
                {own.length > 0 && <SelectSeparator />}
                {own.map((s) => (
                  <SelectItem key={s.schemaId} value={s.schemaId} aside={<Badge variant="outline">Custom</Badge>}>
                    {s.name ?? s.schemaId}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            {schema?.description && <p className="text-xs text-muted-foreground">{schema.description}</p>}
          </div>
        </Card>

        <Card className="overflow-hidden">
          <div className="flex flex-col gap-1 px-6 pt-6">
            <CardTitle>Recordings</CardTitle>
            <CardDescription>
              {RECORDING_EXTENSIONS.slice(0, -1).join(', ')} or {RECORDING_EXTENSIONS.at(-1)} · up to {MAX_RECORDINGS} files ·
              200 MB each · English only
            </CardDescription>
          </div>
          <div className="flex flex-col gap-3 px-6 pb-6 pt-4">
            {editing && (
              <button
                type="button"
                onClick={() => recordingInput.current?.click()}
                onDragOver={(e) => e.preventDefault()}
                onDrop={onDrop}
                className={cn(
                  'flex w-full flex-col items-center justify-center gap-1 rounded-lg border border-dashed border-input text-muted-foreground transition-colors hover:bg-accent focus-ring',
                  recordings.length === 0 ? 'py-10' : 'h-14 flex-row gap-2',
                )}
              >
                {recordings.length === 0 ? (
                  <>
                    <span className="mb-1 inline-flex size-10 items-center justify-center rounded-lg bg-secondary text-foreground">
                      <CloudUpload className="size-5" />
                    </span>
                    <span className="font-medium text-foreground">Drag recordings here</span>
                    <span className="text-xs">or click to choose files</span>
                  </>
                ) : (
                  <>
                    <CloudUpload className="size-4" />
                    Drop more recordings here, or click to add
                  </>
                )}
              </button>
            )}
            <input
              ref={recordingInput}
              type="file"
              multiple
              hidden
              accept={RECORDING_EXTENSIONS.map((e) => `.${e}`).join(',')}
              onChange={(e) => {
                // Copy the files now: clearing the input empties its FileList before the updater runs.
                const added = toItems(e.target.files ?? [], recordingProblem)
                setRecordings((l) => [...l, ...added])
                e.target.value = ''
              }}
            />
            {recordings.map((item) => (
              <FileRow
                key={item.key}
                item={item}
                icon={<AudioLines className="size-[18px]" />}
                onRemove={editing ? () => setRecordings((l) => l.filter((r) => r.key !== item.key)) : undefined}
                onRetry={phase === 'done' && item.state === 'error' ? () => void retry(setRecordings, item) : undefined}
              />
            ))}
          </div>
        </Card>

        <Card className="flex flex-col gap-3 p-6">
          <div className="flex items-baseline justify-between gap-3">
            <Label htmlFor="instruction" className="text-lg font-semibold tracking-[-0.02em]">
              Instructions <span className="text-sm font-normal tracking-normal text-muted-foreground">(optional)</span>
            </Label>
            <span className={cn('font-mono text-xs', instructionOver ? 'text-destructive' : 'text-muted-foreground')}>
              {instruction.trim().length}/{MAX_INSTRUCTION_CHARS}
            </span>
          </div>
          <Textarea
            id="instruction"
            disabled={!editing}
            value={instruction}
            onChange={(e) => setInstruction(e.target.value)}
            aria-invalid={instructionOver || undefined}
            placeholder="Tell the model what to focus on, e.g. 'Focus on onboarding friction and pricing objections.'"
          />
        </Card>

        <Card className="flex flex-col gap-4 p-6">
          <div className="flex flex-wrap items-baseline justify-between gap-3">
            <CardTitle>
              Context files <span className="text-sm font-normal tracking-normal text-muted-foreground">(optional)</span>
            </CardTitle>
            <span className={cn('font-mono text-xs font-medium', contextOver ? 'text-destructive' : 'text-muted-foreground')}>
              {(contextBytes / MiB).toFixed(1)} of 20 MB
            </span>
          </div>
          <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
            <Button variant="outline" disabled={!editing} onClick={() => contextInput.current?.click()}>
              <Paperclip />
              Add context files
            </Button>
            <span className="text-xs text-muted-foreground">
              PDF, DOCX, Markdown or text · up to {MAX_CONTEXT_FILES} files · 20 MB total
            </span>
            <input
              ref={contextInput}
              type="file"
              multiple
              hidden
              accept={CONTEXT_EXTENSIONS.map((e) => `.${e}`).join(',')}
              onChange={(e) => {
                const added = toItems(e.target.files ?? [], contextProblem)
                setContexts((l) => [...l, ...added])
                e.target.value = ''
              }}
            />
          </div>
          {contexts.map((item) => (
            <FileRow
              key={item.key}
              item={item}
              compact
              icon={<FileText className="size-4" />}
              onRemove={editing ? () => setContexts((l) => l.filter((c) => c.key !== item.key)) : undefined}
              onRetry={phase === 'done' && item.state === 'error' ? () => void retry(setContexts, item) : undefined}
            />
          ))}
          {contextOver && (
            <Alert tone="destructive">
              Context files total {formatBytes(contextBytes)}. Remove files to get under 20 MB.
            </Alert>
          )}
          {tooManyContexts && <Alert tone="destructive">Use at most {MAX_CONTEXT_FILES} context files.</Alert>}
        </Card>

        <Card className="flex flex-wrap items-center gap-3 px-6 py-4">
          <span className="min-w-0 flex-[1_1_220px] text-muted-foreground">{note}</span>
          {phase === 'done' ? (
            <Button variant="outline" onClick={reset}>
              <FolderOpen />
              New upload
            </Button>
          ) : (
            <Button disabled={!canUpload} onClick={() => void upload()}>
              {phase === 'uploading' ? <LoaderCircle className="animate-spin" /> : <Upload />}
              {phase === 'uploading' ? 'Uploading…' : 'Upload'}
            </Button>
          )}
        </Card>
      </div>
    </Page>
  )
}

function FileRow({
  item,
  icon,
  compact,
  onRemove,
  onRetry,
}: {
  item: Item
  icon: React.ReactNode
  compact?: boolean
  onRemove?: () => void
  onRetry?: () => void
}) {
  const reason = item.problem ?? item.error
  const showBar = item.state === 'uploading' || item.state === 'success'
  return (
    <div className={cn('flex gap-3 rounded-lg border border-border bg-card', compact ? 'items-start p-3' : 'p-3')}>
      <span
        className={cn(
          'inline-flex shrink-0 items-center justify-center rounded-md bg-secondary text-muted-foreground',
          compact ? 'size-7' : 'size-9',
        )}
      >
        {icon}
      </span>
      <div className="flex min-w-0 flex-1 flex-col gap-2">
        <div className="flex min-h-7 flex-wrap items-center gap-x-3 gap-y-1">
          <span className="min-w-0 flex-[1_1_200px] font-mono text-[13px] [overflow-wrap:anywhere]">{item.file.name}</span>
          <span className="text-xs tabular-nums text-muted-foreground">{formatBytes(item.file.size)}</span>
          {item.problem || item.state === 'error' ? (
            <Badge variant="destructive">
              <CircleAlert />
              Error
            </Badge>
          ) : item.state === 'uploading' ? (
            <span className="inline-flex items-center gap-1.5 text-xs font-medium tabular-nums text-info-fg">
              <LoaderCircle className="size-3 animate-spin" />
              Uploading {Math.round(item.progress)}%
            </span>
          ) : item.state === 'success' ? (
            <Badge variant="success">
              <Check />
              Uploaded
            </Badge>
          ) : (
            <span className="text-xs text-muted-foreground">Ready</span>
          )}
        </div>
        {showBar && !compact && <Progress value={item.progress} label={`Uploading ${item.file.name}`} />}
        {reason && <div className="text-[13px] text-destructive">{reason}</div>}
      </div>
      {onRetry && (
        <Button variant="ghost" size="icon-sm" aria-label={`Retry ${item.file.name}`} onClick={onRetry}>
          <RotateCcw />
        </Button>
      )}
      {onRemove && (
        <Button
          variant="ghost"
          size="icon-sm"
          className="text-muted-foreground"
          aria-label={`Remove ${item.file.name}`}
          onClick={onRemove}
        >
          <X />
        </Button>
      )}
    </div>
  )
}
