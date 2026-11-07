import {
  AudioLines,
  ChevronDown,
  ChevronRight,
  ChevronsUpDown,
  CircleX,
  Download,
  Ellipsis,
  FileText,
  Folder,
  FolderOpen,
  LogOut,
  MessageSquare,
  Pencil,
  Plus,
  Trash2,
  Upload,
  X,
} from 'lucide-react'
import { useQueryClient, type InfiniteData } from '@tanstack/react-query'
import { useState } from 'react'
import { Link, useMatch } from 'react-router'
import { Button } from '@/components/ui/button'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import { Skeleton } from '@/components/ui/skeleton'
import { useAuth } from '@/lib/auth'
import { useExportDocument } from '@/lib/export'
import { formatCost, formatTokens, relativeTime, stripExtension } from '@/lib/format'
import { stageShort, stageTone } from '@/lib/jobs'
import { keys, useHistory, useJobs, useProject, useProjects } from '@/lib/queries'
import type { HistoryEvent, HistoryPage, Project } from '@/lib/types'
import { cn } from '@/lib/utils'
import { useDocumentActions } from './change-dialogs'
import { Logo } from './logo'
import { NewProjectButton } from './new-project-dialog'
import { useChat } from './shell'
import { StageIcon } from './status'

const overline = 'px-3 text-xs font-medium uppercase tracking-[0.08em] text-muted-foreground'

export function Sidebar({ onClose }: { onClose?: () => void }) {
  const projectMatch = useMatch('/projects/:projectId/*')
  const jobMatch = useMatch('/projects/:projectId/jobs/:jobId')
  const activeProject = projectMatch?.params.projectId
  const activeJob = jobMatch?.params.jobId
  const projects = useProjects()

  // The project being viewed is expanded unless the user collapsed it; others only once expanded.
  const [toggled, setToggled] = useState<Record<string, boolean>>({})
  const isOpen = (id: string) => toggled[id] ?? id === activeProject
  const toggle = (id: string) => setToggled((t) => ({ ...t, [id]: !isOpen(id) }))

  return (
    <div className="flex h-full flex-col">
      <div className="flex items-center justify-between gap-2 px-4 pb-3 pt-4">
        <Link to="/" className="rounded-md no-underline focus-ring" onClick={onClose}>
          <Logo />
        </Link>
        {onClose && (
          <Button variant="ghost" size="icon-sm" aria-label="Close navigation" onClick={onClose}>
            <X />
          </Button>
        )}
      </div>
      <div className="px-3 pb-2">
        <NewProjectButton>
          {(openDialog) => (
            <Button variant="outline" className="w-full" onClick={openDialog}>
              <Plus />
              New project
            </Button>
          )}
        </NewProjectButton>
      </div>

      <nav aria-label="Projects" className="flex min-h-0 flex-1 flex-col gap-6 overflow-y-auto px-3 py-4">
        <section className="flex flex-col gap-1">
          <h2 className={cn(overline, 'pb-1')}>Projects</h2>
          {projects.isLoading && (
            <div className="flex flex-col gap-2 px-3 py-1">
              <Skeleton className="h-5 w-4/5" />
              <Skeleton className="h-5 w-3/5" />
            </div>
          )}
          {projects.isError && <p className="px-3 text-sm text-destructive">Couldn't load projects.</p>}
          {projects.data?.length === 0 && <p className="px-3 text-sm text-muted-foreground">No projects yet</p>}
          {[...(projects.data ?? [])].reverse().map((p) => (
            <ProjectNode
              key={p.projectId}
              project={p}
              open={isOpen(p.projectId)}
              onToggle={() => toggle(p.projectId)}
              activeJob={activeJob}
              onNavigate={onClose}
            />
          ))}
        </section>
        <HistorySection onNavigate={onClose} />
      </nav>

      <UserMenu />
    </div>
  )
}

function ProjectNode({
  project,
  open,
  onToggle,
  activeJob,
  onNavigate,
}: {
  project: Project
  open: boolean
  onToggle: () => void
  activeJob?: string
  onNavigate?: () => void
}) {
  const detail = useProject(open ? project.projectId : undefined)
  const recordings = [...(detail.data?.recordings ?? [])].filter((r) => r.jobId).reverse()
  const jobs = useJobs(open ? recordings.map((r) => r.jobId!) : [])
  const documents = Object.fromEntries((detail.data?.documents ?? []).map((d) => [d.jobId, d]))
  const Icon = open ? FolderOpen : Folder

  return (
    <div className="flex flex-col">
      <div className="group flex items-center rounded-md hover:bg-accent">
        <button
          type="button"
          aria-expanded={open}
          aria-label={open ? `Collapse ${project.name}` : `Expand ${project.name}`}
          onClick={onToggle}
          className="inline-flex size-8 shrink-0 items-center justify-center rounded-md text-muted-foreground focus-ring"
        >
          {open ? <ChevronDown className="size-4" /> : <ChevronRight className="size-4" />}
        </button>
        <Link
          to={`/projects/${project.projectId}`}
          onClick={onNavigate}
          className="flex min-w-0 flex-1 items-center gap-2.5 rounded-md py-2 pr-2 text-sm font-medium focus-ring"
        >
          <Icon className="size-[18px] shrink-0 text-muted-foreground" />
          <span className="min-w-0 flex-1 truncate">{project.name}</span>
          {detail.data && (
            <span className="text-xs tabular-nums text-muted-foreground">{detail.data.recordings.length}</span>
          )}
        </Link>
      </div>
      {open && (
        <div className="ml-4 flex flex-col gap-0.5 border-l border-border py-1 pl-2">
          {detail.isLoading && <Skeleton className="mx-2 my-1 h-5 w-4/5" />}
          {detail.data && recordings.length === 0 && (
            <span className="px-2 py-1 text-sm text-muted-foreground">No recordings yet</span>
          )}
          {recordings.map((r) => {
            const job = jobs.byId[r.jobId!]
            const active = activeJob === r.jobId
            const doc = documents[r.jobId!]
            const title = doc?.title ?? stripExtension(r.fileName)
            // The project list lags the job by up to its cache TTL, so a job that just finished brings its own ID.
            const documentId = doc?.documentId ?? job?.documentId
            const tone = job ? stageTone(job.stage) : 'neutral'
            return (
              <div
                key={r.recordingId}
                className={cn(
                  'group/doc flex items-start rounded-md hover:bg-accent',
                  active ? 'bg-secondary text-foreground' : 'text-muted-foreground',
                )}
              >
                <Link
                  to={`/projects/${project.projectId}/jobs/${r.jobId}`}
                  onClick={onNavigate}
                  aria-current={active ? 'page' : undefined}
                  className="flex min-w-0 flex-1 flex-col gap-1 rounded-md px-2 py-2 text-sm focus-ring"
                >
                  <span className="line-clamp-2">{title}</span>
                  {job && job.stage !== 'Done' && (
                    <span
                      className={cn(
                        'flex items-center gap-1.5 text-xs font-medium',
                        tone === 'info' && 'text-info-fg',
                        tone === 'destructive' && 'text-destructive',
                        tone === 'neutral' && 'text-foreground',
                      )}
                    >
                      <StageIcon stage={job.stage} className="size-3" />
                      {stageShort(job)}
                    </span>
                  )}
                </Link>
                {documentId && (
                  <DocumentMenu projectId={project.projectId} documentId={documentId} jobId={r.jobId} title={title} />
                )}
              </div>
            )
          })}
        </div>
      )}
    </div>
  )
}

/** The design's "Document actions" menu (frame 07e): Rename, Export Markdown, Delete. */
function DocumentMenu({
  projectId,
  documentId,
  jobId,
  title,
}: {
  projectId: string
  documentId: string
  jobId: string | null
  title: string
}) {
  const exportDoc = useExportDocument()
  const actions = useDocumentActions({ projectId, documentId, jobId, title })
  return (
    <>
      {/* Not modal: a modal menu that opens a modal dialog can leave the page inert when both close. */}
      <DropdownMenu modal={false}>
        <DropdownMenuTrigger asChild>
          <Button
            variant="outline"
            aria-label={`Actions for ${title}`}
            // Shown on hover, keyboard focus or while open; always shown where nothing can hover (touch).
            className="mr-1 mt-1 size-7 bg-card opacity-0 group-hover/doc:opacity-100 group-focus-within/doc:opacity-100 data-[state=open]:opacity-100 [@media(hover:none)]:opacity-100"
          >
            <Ellipsis />
          </Button>
        </DropdownMenuTrigger>
        <DropdownMenuContent align="start">
          <DropdownMenuItem onSelect={actions.openRename}>
            <Pencil />
            Rename
          </DropdownMenuItem>
          <DropdownMenuItem
            disabled={exportDoc.isPending}
            onSelect={() => exportDoc.mutate({ projectId, documentId })}
          >
            <Download />
            Export Markdown
          </DropdownMenuItem>
          <DropdownMenuSeparator />
          <DropdownMenuItem destructive onSelect={actions.openDelete}>
            <Trash2 />
            Delete
          </DropdownMenuItem>
        </DropdownMenuContent>
      </DropdownMenu>
      {actions.dialogs}
    </>
  )
}

const EVENT_LABELS: Record<string, { label: string; icon: typeof Upload }> = {
  UploadReceived: { label: 'Upload received', icon: Upload },
  TranscriptionDone: { label: 'Transcription done', icon: AudioLines },
  SynthesisDone: { label: 'Synthesis done', icon: FileText },
  JobFailed: { label: 'Job failed', icon: CircleX },
  ChatSession: { label: 'Chat message', icon: MessageSquare },
}

function HistorySection({ onNavigate }: { onNavigate?: () => void }) {
  const [open, setOpen] = useState(false)
  return (
    <section className="flex flex-col gap-1">
      <button
        type="button"
        aria-expanded={open}
        onClick={() => setOpen(!open)}
        className={cn(overline, 'flex items-center justify-between rounded-md py-1 hover:text-foreground focus-ring')}
      >
        History
        {open ? <ChevronDown className="size-4" /> : <ChevronRight className="size-4" />}
      </button>
      {open && <HistoryList onNavigate={onNavigate} />}
    </section>
  )
}

/** Newest first: 5 events, then 10 more per "Show more". "Show less" drops back to the first page. */
function HistoryList({ onNavigate }: { onNavigate?: () => void }) {
  const history = useHistory()
  const queryClient = useQueryClient()
  const pages = history.data?.pages ?? []
  const events = pages.flatMap((p) => p.events)
  if (history.isLoading) return <Skeleton className="mx-3 my-1 h-5 w-4/5" />
  if (history.isError) return <p className="px-3 text-sm text-destructive">Couldn't load history.</p>
  if (events.length === 0) return <p className="px-3 text-sm text-muted-foreground">No activity yet</p>

  const showLess = () =>
    queryClient.setQueryData<InfiniteData<HistoryPage, string | null>>(keys.history, (data) =>
      data && { pages: data.pages.slice(0, 1), pageParams: data.pageParams.slice(0, 1) },
    )
  return (
    <div className="flex flex-col gap-0.5">
      {events.map((e) => (
        <HistoryRow key={e.eventId} event={e} onNavigate={onNavigate} />
      ))}
      {(history.hasNextPage || pages.length > 1) && (
        <div className="flex gap-1">
          {history.hasNextPage && (
            <Button
              variant="ghost"
              size="sm"
              className="justify-start"
              disabled={history.isFetchingNextPage}
              onClick={() => void history.fetchNextPage()}
            >
              {history.isFetchingNextPage ? 'Loading…' : 'Show more'}
            </Button>
          )}
          {pages.length > 1 && (
            <Button variant="ghost" size="sm" className="justify-start" onClick={showLess}>
              Show less
            </Button>
          )}
        </div>
      )}
    </div>
  )
}

const FAILED_AT: Record<string, string> = {
  Queued: 'while preparing the audio',
  Transcribing: 'while transcribing',
  Redacting: 'while removing personal details',
  Synthesising: 'while writing the document',
}

function HistoryRow({ event, onNavigate }: { event: HistoryEvent; onNavigate?: () => void }) {
  const chat = useChat()
  const meta = EVENT_LABELS[event.eventType ?? ''] ?? { label: event.eventType ?? 'Event', icon: FileText }
  const Icon = meta.icon
  const failedAt = event.eventType === 'JobFailed' && event.stage ? FAILED_AT[event.stage] : undefined
  const body = (
    <>
      <Icon className={cn('mt-0.5 size-4 shrink-0', event.eventType === 'JobFailed' ? 'text-destructive' : 'text-muted-foreground')} />
      <div className="flex min-w-0 flex-1 flex-col gap-0.5">
        <div className="flex items-baseline justify-between gap-2">
          <span className="font-medium">{meta.label}</span>
          <time dateTime={event.occurredAt} className="shrink-0 text-xs text-muted-foreground">
            {relativeTime(event.occurredAt)}
          </time>
        </div>
        {(event.fileName || failedAt) && (
          <span className="truncate text-xs text-muted-foreground">
            {[event.fileName && stripExtension(event.fileName), failedAt].filter(Boolean).join(' · ')}
          </span>
        )}
        {event.tokenCount != null && (
          <span className="font-mono text-xs text-muted-foreground">
            {formatTokens(event.tokenCount)} tok{event.cost != null && ` · ${formatCost(event.cost)}`}
          </span>
        )}
      </div>
    </>
  )
  const row = 'flex gap-2.5 rounded-md px-3 py-2 text-sm'
  // Job events carry their project (copied by the history writer), so the row opens the job.
  if (event.projectId && event.jobId) {
    return (
      <Link
        to={`/projects/${event.projectId}/jobs/${event.jobId}`}
        onClick={onNavigate}
        className={cn(row, 'text-foreground no-underline hover:bg-accent focus-ring')}
      >
        {body}
      </Link>
    )
  }
  // A chat event (one per answered message) reopens its chat in the Ask AI panel.
  if (event.chatSessionId) {
    const sessionId = event.chatSessionId
    return (
      <button
        type="button"
        onClick={() => {
          chat.openChat(sessionId)
          onNavigate?.()
        }}
        className={cn(row, 'w-full text-left hover:bg-accent focus-ring')}
      >
        {body}
      </button>
    )
  }
  return <div className={row}>{body}</div>
}

function UserMenu() {
  const { session, logout } = useAuth()
  const email = session?.email ?? ''
  return (
    <div className="border-t border-sidebar-border p-3">
      <DropdownMenu>
        <DropdownMenuTrigger asChild>
          <button
            type="button"
            className="flex w-full items-center gap-2.5 rounded-md p-1.5 text-left text-sm hover:bg-accent focus-ring data-[state=open]:bg-accent"
          >
            <span className="inline-flex size-8 shrink-0 items-center justify-center rounded-full bg-secondary text-xs font-medium uppercase">
              {email.slice(0, 2)}
            </span>
            <span className="min-w-0 flex-1 truncate font-medium">{email}</span>
            <ChevronsUpDown className="size-4 shrink-0 text-muted-foreground" />
          </button>
        </DropdownMenuTrigger>
        <DropdownMenuContent side="top" align="start" className="w-[var(--radix-dropdown-menu-trigger-width)]">
          <DropdownMenuLabel>Signed in as {email}</DropdownMenuLabel>
          <DropdownMenuSeparator />
          <DropdownMenuItem onSelect={logout}>
            <LogOut />
            Log out
          </DropdownMenuItem>
        </DropdownMenuContent>
      </DropdownMenu>
    </div>
  )
}
