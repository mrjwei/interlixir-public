import { useMutation, useQueryClient } from '@tanstack/react-query'
import { Ellipsis, FileText, Pencil, RotateCcw, Trash2, Upload } from 'lucide-react'
import { Link, useParams } from 'react-router'
import { useProjectActions } from '@/components/app/change-dialogs'
import { Page } from '@/components/app/shell'
import { StatusBadge } from '@/components/app/status'
import { Alert } from '@/components/ui/alert'
import { Button } from '@/components/ui/button'
import { Card } from '@/components/ui/card'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import { Skeleton } from '@/components/ui/skeleton'
import { useToast } from '@/components/ui/toast'
import { api, ApiError } from '@/lib/api'
import { formatDate, relativeTime, stripExtension } from '@/lib/format'
import { stageDetail } from '@/lib/jobs'
import { keys, useJobs, useProject, useSchemas } from '@/lib/queries'
import type { Job } from '@/lib/types'
import { NotFound } from './not-found-page'

export function ProjectPage() {
  const { projectId } = useParams()
  const project = useProject(projectId)
  const recordings = [...(project.data?.recordings ?? [])].filter((r) => r.jobId).reverse()
  const jobs = useJobs(recordings.map((r) => r.jobId!))
  const schemas = useSchemas()
  const schemaName = (id: string | null | undefined) =>
    schemas.data?.find((s) => s.schemaId === id)?.name ?? id ?? '—'
  const titles = Object.fromEntries((project.data?.documents ?? []).map((d) => [d.jobId, d.title]))

  if (project.error instanceof ApiError && project.error.status === 404) {
    return (
      <Page crumbs={[{ label: 'Not found' }]}>
        <NotFound message="This project doesn't exist, or it isn't yours." />
      </Page>
    )
  }

  const name = project.data?.name ?? ''
  return (
    <Page crumbs={[{ label: name || 'Project' }]}>
      <div className="flex flex-col gap-6">
        <div className="flex flex-wrap items-start justify-between gap-4">
          <div className="flex min-w-0 flex-col gap-1.5">
            {project.data ? (
              <>
                <h1 className="font-serif text-[40px] font-normal leading-[1.2] tracking-[-0.01em] [overflow-wrap:anywhere]">{name}</h1>
                <p className="text-muted-foreground">
                  Created {formatDate(project.data.createdAt)} · {recordings.length}{' '}
                  {recordings.length === 1 ? 'recording' : 'recordings'}
                </p>
              </>
            ) : (
              <>
                <Skeleton className="h-9 w-64" />
                <Skeleton className="h-5 w-48" />
              </>
            )}
          </div>
          <div className="flex items-center gap-2">
            <Button asChild>
              <Link to={`/projects/${projectId}/upload`}>
                <Upload />
                New upload
              </Link>
            </Button>
            {project.data && <ProjectMenu projectId={projectId!} name={name} />}
          </div>
        </div>

        {project.isError && !(project.error instanceof ApiError && project.error.status === 404) && (
          <Alert tone="destructive" title="Couldn't load this project">{project.error.message}</Alert>
        )}

        {project.data && recordings.length === 0 ? (
          <Card className="flex flex-col items-center gap-2 px-6 py-10 text-center">
            <h2 className="text-lg font-semibold tracking-[-0.02em]">No recordings yet</h2>
            <p className="max-w-[380px] text-muted-foreground">
              Upload interview recordings and each one becomes its own synthesised document.
            </p>
          </Card>
        ) : (
          <Card className="overflow-x-auto">
            <table className="w-full min-w-[720px] text-sm">
              <thead>
                <tr className="text-left text-xs font-medium uppercase tracking-[0.08em] text-muted-foreground">
                  <th className="px-6 py-2.5 font-medium">Title</th>
                  <th className="px-4 py-2.5 font-medium">Schema</th>
                  <th className="px-4 py-2.5 font-medium">Status</th>
                  <th className="px-4 py-2.5 font-medium">Stage</th>
                  <th className="px-6 py-2.5 font-medium">Created</th>
                </tr>
              </thead>
              <tbody>
                {!project.data &&
                  [72, 60, 80].map((w) => (
                    <tr key={w} className="border-t border-border">
                      <td className="px-6 py-4" colSpan={5}>
                        <Skeleton className="h-5" style={{ width: `${w}%` }} />
                      </td>
                    </tr>
                  ))}
                {recordings.map((r) => {
                  const job = jobs.byId[r.jobId!]
                  return (
                    <tr key={r.recordingId} className="border-t border-border">
                      <td className="px-6 py-3">
                        <Link
                          to={`/projects/${projectId}/jobs/${r.jobId}`}
                          className="flex items-center gap-2.5 font-medium no-underline hover:underline"
                        >
                          <FileText className="size-4 shrink-0 text-muted-foreground" />
                          <span className="line-clamp-2">{titles[r.jobId!] ?? stripExtension(r.fileName)}</span>
                        </Link>
                      </td>
                      <td className="px-4 py-3 text-muted-foreground">{job ? schemaName(job.schemaId) : ''}</td>
                      <td className="px-4 py-3">{job ? <StatusBadge stage={job.stage} /> : <Skeleton className="h-5 w-20" />}</td>
                      <td className="px-4 py-3 text-muted-foreground">
                        {job && (
                          <div className="flex flex-col items-start gap-1">
                            <span>{stageDetail(job)}</span>
                            {job.stage === 'Failed' && <RetryButton job={job} />}
                          </div>
                        )}
                      </td>
                      <td className="whitespace-nowrap px-6 py-3 text-muted-foreground">{job ? relativeTime(job.createdAt) : ''}</td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </Card>
        )}
      </div>
    </Page>
  )
}

/** Rename or delete the project. Deleting returns to the home page; GC removes the contents. */
function ProjectMenu({ projectId, name }: { projectId: string; name: string }) {
  const actions = useProjectActions({ projectId, name })
  return (
    <>
      {/* Not modal: a modal menu that opens a modal dialog can leave the page inert when both close. */}
      <DropdownMenu modal={false}>
        <DropdownMenuTrigger asChild>
          <Button variant="outline" size="icon" aria-label="Project actions">
            <Ellipsis />
          </Button>
        </DropdownMenuTrigger>
        <DropdownMenuContent align="end">
          <DropdownMenuItem onSelect={actions.openRename}>
            <Pencil />
            Rename project
          </DropdownMenuItem>
          <DropdownMenuSeparator />
          <DropdownMenuItem destructive onSelect={actions.openDelete}>
            <Trash2 />
            Delete project
          </DropdownMenuItem>
        </DropdownMenuContent>
      </DropdownMenu>
      {actions.dialogs}
    </>
  )
}

/** Failed → Queued, retryCount + 1 (M11's route). The stream router then re-runs the job. */
export function RetryButton({ job, label = 'Retry' }: { job: Job; label?: string }) {
  const queryClient = useQueryClient()
  const toast = useToast()
  const retry = useMutation({
    mutationFn: () => api.retryJob(job.jobId),
    onSuccess: (updated) => {
      queryClient.setQueryData(keys.job(job.jobId), updated)
      void queryClient.invalidateQueries({ queryKey: keys.job(job.jobId) }) // restarts polling
      toast({ tone: 'info', title: 'Retry queued' })
    },
    onError: (err) => toast({ tone: 'error', title: "Couldn't retry", description: err.message }),
  })
  return (
    <Button variant="ghost" size="sm" className="-ml-3" disabled={retry.isPending} onClick={() => retry.mutate()}>
      <RotateCcw />
      {retry.isPending ? 'Retrying…' : label}
    </Button>
  )
}
