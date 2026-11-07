import { AudioLines, ChevronDown, ChevronRight, Download, FileText, Pencil, RefreshCw, Trash2 } from 'lucide-react'
import { lazy, Suspense, useEffect, useState } from 'react'
import { useParams } from 'react-router'
import { useDocumentActions } from '@/components/app/change-dialogs'
import { Page } from '@/components/app/shell'
import { StageStepper, StatusBadge } from '@/components/app/status'
import { Alert } from '@/components/ui/alert'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card } from '@/components/ui/card'
import { Textarea } from '@/components/ui/input'
import { Skeleton } from '@/components/ui/skeleton'
import { ApiError } from '@/lib/api'
import { useUpdateDocument } from '@/lib/changes'
import { useExportDocument } from '@/lib/export'
import { formatCost, formatDate, formatDateTime, formatDuration, formatTokens, stripExtension } from '@/lib/format'
import { useDocument, useJob, useProject, useSchemas } from '@/lib/queries'
import type { Job } from '@/lib/types'
import { cn } from '@/lib/utils'
import { NotFound } from './not-found-page'

// react-markdown and its parser are most of the bundle, and only this view needs them.
const MarkdownDocument = lazy(() => import('@/components/app/markdown-document'))
import { RetryButton } from './project-page'

/**
 * One recording's page, keyed by its job: the stage stepper while the job runs (polled ~3 s), then the
 * synthesised document once the job is Done (the job carries documentId from M8).
 */
export function JobPage() {
  const { projectId, jobId } = useParams()
  const job = useJob(jobId)
  const project = useProject(projectId)
  const schemas = useSchemas()

  const recording = project.data?.recordings.find((r) => r.jobId === jobId)
  const docTitle = project.data?.documents.find((d) => d.jobId === jobId)?.title
  const title = docTitle ?? stripExtension(recording?.fileName)
  const projectCrumb = { label: project.data?.name ?? 'Project', to: `/projects/${projectId}` }
  const schemaName = schemas.data?.find((s) => s.schemaId === job.data?.schemaId)?.name ?? job.data?.schemaId ?? ''

  const notFound =
    (job.error instanceof ApiError && job.error.status === 404) ||
    (job.data && job.data.projectId && job.data.projectId !== projectId)
  if (notFound) {
    return (
      <Page crumbs={[projectCrumb, { label: 'Not found' }]}>
        <NotFound message="This recording doesn't exist, or it isn't yours." />
      </Page>
    )
  }

  const data = job.data
  return (
    <Page width="doc" crumbs={[projectCrumb, { label: project.data ? title : '…' }]}>
      {job.isError && <Alert tone="destructive" title="Couldn't load this recording">{job.error.message}</Alert>}
      {!data && !job.isError && <HeaderSkeleton />}
      {data &&
        (data.stage === 'Done' && data.documentId ? (
          <DocumentView job={data} title={title} schemaName={schemaName} fileName={recording?.fileName} />
        ) : (
          <ProgressView job={data} title={title} schemaName={schemaName} fileName={recording?.fileName} />
        ))}
    </Page>
  )
}

function HeaderSkeleton() {
  return (
    <div className="flex flex-col gap-4">
      <Skeleton className="h-9 w-2/3" />
      <Skeleton className="h-6 w-56" />
      <Skeleton className="h-40 w-full" />
    </div>
  )
}

interface ViewProps {
  job: Job
  title: string
  schemaName: string
  fileName?: string | null
}

function ProgressView({ job, title, schemaName, fileName }: ViewProps) {
  const failed = job.stage === 'Failed'
  return (
    <div className="flex flex-col gap-6">
      <div className="flex flex-col gap-3">
        <h1 className="font-serif text-[40px] font-normal leading-[1.2] tracking-[-0.01em] [overflow-wrap:anywhere]">{title}</h1>
        <div className="flex flex-wrap gap-2">
          {schemaName && <Badge variant="outline">{schemaName}</Badge>}
          <StatusBadge stage={job.stage} />
        </div>
      </div>

      <Card className="flex flex-col gap-6 p-6">
        <StageStepper job={job} />
        <div className="grid gap-4 border-t border-border pt-6 sm:grid-cols-3">
          <Meta label="Recording">
            <span className="font-mono text-[13px] [overflow-wrap:anywhere]">{fileName ?? '—'}</span>
          </Meta>
          <Meta label="Schema">{schemaName || '—'}</Meta>
          <Meta label="Started">{formatDateTime(job.createdAt)}</Meta>
        </div>
        {failed ? (
          <Alert tone="destructive" title="Processing failed">
            <div className="flex flex-col items-start gap-1">
              <span>{job.failureReason || 'Something went wrong while processing this recording.'}</span>
              <RetryButton job={job} label="Retry processing" />
            </div>
          </Alert>
        ) : (
          <p className="flex items-center gap-2 text-xs text-muted-foreground">
            <RefreshCw className="size-3.5" />
            Updates every few seconds. You can close this page; processing carries on.
          </p>
        )}
      </Card>

      {!failed && (
        <Card className="flex flex-col gap-4 p-8 md:px-16" aria-hidden>
          <Skeleton className="h-7 w-1/3" />
          <Skeleton className="h-4 w-full" />
          <Skeleton className="h-4 w-11/12" />
          <Skeleton className="h-4 w-2/3" />
          <Skeleton className="mt-4 h-7 w-2/5" />
          <Skeleton className="h-28 w-full" />
        </Card>
      )}
    </div>
  )
}

function Meta({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex min-w-0 flex-col gap-1">
      <span className="text-xs font-medium uppercase tracking-[0.08em] text-muted-foreground">{label}</span>
      <span>{children}</span>
    </div>
  )
}

function DocumentView({ job, title, schemaName, fileName }: ViewProps) {
  const { projectId } = useParams()
  const doc = useDocument(projectId, job.documentId)
  const [inputsOpen, setInputsOpen] = useState(false)
  const [draft, setDraft] = useState<string | null>(null) // the Markdown being edited, null when not editing
  const exportDoc = useExportDocument()
  const actions = useDocumentActions({
    projectId: projectId!,
    documentId: job.documentId!,
    jobId: job.jobId,
    title: doc.data?.title ?? title,
  })

  const contextFiles = doc.data?.contextFiles ?? []
  const meta = [
    formatDate(job.createdAt) !== '—' ? `Created ${formatDate(job.createdAt)}` : null,
    formatDuration(job.audioSeconds),
  ].filter(Boolean)
  const usage = [
    job.tokenCount != null ? `${formatTokens(job.tokenCount)} tokens` : null,
    job.cost != null ? formatCost(job.cost) : null,
  ].filter(Boolean)
  const inputsSummary = [
    job.instructionText ? 'Instructions' : 'No instructions',
    contextFiles.length === 0 ? 'no context files' : `${contextFiles.length} context ${contextFiles.length === 1 ? 'file' : 'files'}`,
  ].join(' and ')

  return (
    <div className="flex flex-col gap-6">
      <div className="flex flex-col gap-3">
        <div className="flex flex-wrap items-start justify-between gap-4">
          <h1 className="min-w-0 flex-[1_1_320px] font-serif text-[40px] font-normal leading-[1.2] tracking-[-0.01em] [overflow-wrap:anywhere]">
            {doc.data?.title ?? title}
          </h1>
          {draft === null && (
            <div className="flex flex-wrap items-center gap-2">
              <Button variant="outline" disabled={!doc.data} onClick={() => setDraft(doc.data!.markdown)}>
                <Pencil />
                Edit
              </Button>
              <Button
                disabled={exportDoc.isPending}
                onClick={() => exportDoc.mutate({ projectId: projectId!, documentId: job.documentId! })}
              >
                <Download />
                Export Markdown
              </Button>
              <Button variant="ghost" className="text-destructive hover:bg-destructive/10" onClick={actions.openDelete}>
                <Trash2 />
                Delete
              </Button>
            </div>
          )}
        </div>
        <div className="flex flex-wrap gap-2">
          {schemaName && <Badge variant="outline">{schemaName}</Badge>}
          <StatusBadge stage={job.stage} />
        </div>
        <div className="flex flex-wrap items-center gap-x-2 gap-y-1 text-[13px] text-muted-foreground">
          {fileName && (
            <span className="inline-flex items-center gap-1.5 font-mono">
              <AudioLines className="size-3.5" />
              {fileName}
            </span>
          )}
          {meta.map((m) => (
            <span key={m} className="before:mr-2 before:content-['·']">
              {m}
            </span>
          ))}
          {usage.length > 0 && (
            <span className="font-mono before:mr-2 before:font-sans before:content-['·']">{usage.join(' · ')}</span>
          )}
        </div>
      </div>

      <Card>
        <button
          type="button"
          aria-expanded={inputsOpen}
          onClick={() => setInputsOpen(!inputsOpen)}
          className="flex w-full items-center gap-3 rounded-lg px-6 py-4 text-left focus-ring"
        >
          {inputsOpen ? <ChevronDown className="size-4 text-muted-foreground" /> : <ChevronRight className="size-4 text-muted-foreground" />}
          <span className="font-semibold">Inputs</span>
          <span className="truncate text-muted-foreground">{inputsSummary}</span>
        </button>
        {inputsOpen && (
          <div className="flex flex-col gap-5 border-t border-border px-6 py-5">
            <div className="flex flex-col gap-1.5">
              <span className="text-xs font-medium uppercase tracking-[0.08em] text-muted-foreground">Instructions</span>
              <p className="whitespace-pre-wrap [overflow-wrap:anywhere]">
                {job.instructionText || <span className="text-muted-foreground">None</span>}
              </p>
            </div>
            <div className="flex flex-col gap-2">
              <span className="text-xs font-medium uppercase tracking-[0.08em] text-muted-foreground">Context files</span>
              {contextFiles.length === 0 ? (
                <span className="text-muted-foreground">None</span>
              ) : (
                <ul className="flex flex-col gap-2">
                  {contextFiles.map((c) => (
                    <li key={c.contextFileId} className="flex items-center gap-2.5 rounded-md border border-border px-3 py-2">
                      <FileText className="size-4 shrink-0 text-muted-foreground" />
                      <span className="font-mono text-[13px] [overflow-wrap:anywhere]">{c.fileName}</span>
                    </li>
                  ))}
                </ul>
              )}
            </div>
          </div>
        )}
      </Card>

      {draft !== null && doc.data ? (
        <DocumentEditor
          projectId={projectId!}
          documentId={job.documentId!}
          original={doc.data.markdown}
          draft={draft}
          onChange={setDraft}
          onClose={() => setDraft(null)}
        />
      ) : (
      <Card className="px-5 py-8 md:px-16 md:py-12">
        {doc.isLoading && (
          <div className="flex flex-col gap-3">
            <Skeleton className="h-7 w-1/3" />
            <Skeleton className="h-4 w-full" />
            <Skeleton className="h-4 w-5/6" />
          </div>
        )}
        {doc.isError && <Alert tone="destructive" title="Couldn't load the document">{doc.error.message}</Alert>}
        {doc.data && (
          <Suspense fallback={<Skeleton className="h-40 w-full" />}>
            <MarkdownDocument markdown={doc.data.markdown} />
          </Suspense>
        )}
      </Card>
      )}
      {actions.dialogs}
    </div>
  )
}

/** Edit mode (design frame 07c): Write / Preview tabs over the Markdown, and a bar to save or cancel. */
function DocumentEditor({
  projectId,
  documentId,
  original,
  draft,
  onChange,
  onClose,
}: {
  projectId: string
  documentId: string
  original: string
  draft: string
  onChange: (markdown: string) => void
  onClose: () => void
}) {
  const [tab, setTab] = useState<'write' | 'preview'>('write')
  const save = useUpdateDocument()
  const dirty = draft !== original

  // The browser's own "leave site?" prompt while there are unsaved changes.
  useEffect(() => {
    if (!dirty) return
    const warn = (e: BeforeUnloadEvent) => e.preventDefault()
    window.addEventListener('beforeunload', warn)
    return () => window.removeEventListener('beforeunload', warn)
  }, [dirty])

  const tabClass = (active: boolean) =>
    cn(
      'h-8 rounded-md px-3 text-sm font-medium focus-ring',
      active ? 'bg-card text-foreground shadow-xs' : 'text-muted-foreground hover:text-foreground',
    )
  return (
    <>
      <Card className="flex flex-col">
        <div className="flex items-center justify-between gap-3 border-b border-border px-4 py-3 md:px-6">
          <div role="tablist" aria-label="Editor" className="inline-flex gap-1 rounded-lg bg-secondary p-1">
            <button type="button" role="tab" aria-selected={tab === 'write'} className={tabClass(tab === 'write')} onClick={() => setTab('write')}>
              Write
            </button>
            <button type="button" role="tab" aria-selected={tab === 'preview'} className={tabClass(tab === 'preview')} onClick={() => setTab('preview')}>
              Preview
            </button>
          </div>
          <span className="text-xs text-muted-foreground">Markdown</span>
        </div>
        <div className="p-4 md:p-6">
          {tab === 'write' ? (
            <Textarea
              aria-label="Document Markdown"
              autoFocus
              spellCheck
              maxLength={500_000}
              value={draft}
              onChange={(e) => onChange(e.target.value)}
              className="min-h-[60vh] font-mono text-[13px] leading-relaxed"
            />
          ) : (
            <div className="px-1 py-4 md:px-10">
              <Suspense fallback={<Skeleton className="h-40 w-full" />}>
                <MarkdownDocument markdown={draft} />
              </Suspense>
            </div>
          )}
        </div>
      </Card>
      <div className="sticky bottom-0 z-10 flex flex-wrap items-center justify-between gap-3 rounded-lg border border-border bg-card px-4 py-3 shadow-lg">
        <span className="text-sm text-muted-foreground" role="status">
          {save.error ? <span className="text-destructive">{save.error.message}</span> : dirty ? 'Unsaved changes' : 'No changes'}
        </span>
        <div className="flex gap-3">
          <Button variant="outline" disabled={save.isPending} onClick={onClose}>
            Cancel
          </Button>
          <Button
            disabled={!dirty || !draft.trim() || save.isPending}
            onClick={() => save.mutate({ projectId, documentId, markdown: draft }, { onSuccess: onClose })}
          >
            {save.isPending ? 'Saving…' : 'Save changes'}
          </Button>
        </div>
      </div>
    </>
  )
}
