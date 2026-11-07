import { FolderPlus, Plus } from 'lucide-react'
import { Navigate } from 'react-router'
import { NewProjectButton } from '@/components/app/new-project-dialog'
import { Page } from '@/components/app/shell'
import { Alert } from '@/components/ui/alert'
import { Button } from '@/components/ui/button'
import { Card } from '@/components/ui/card'
import { Skeleton } from '@/components/ui/skeleton'
import { useProjects } from '@/lib/queries'

/** "/": the newest project, or the first-run empty state. */
export function HomePage() {
  const projects = useProjects()
  const newest = projects.data?.at(-1) // the list is in creation order (UUIDv7 keys)
  if (newest) return <Navigate to={`/projects/${newest.projectId}`} replace />

  return (
    <Page crumbs={[{ label: 'Projects' }]}>
      {projects.isLoading && <Skeleton className="mx-auto mt-16 h-[200px] max-w-[480px]" />}
      {projects.isError && <Alert tone="destructive" title="Couldn't load your projects">{projects.error.message}</Alert>}
      {projects.data?.length === 0 && (
        <Card className="mx-auto mt-4 flex max-w-[480px] flex-col items-center gap-3 px-6 py-10 text-center md:mt-16">
          <span className="mb-1 inline-flex size-12 items-center justify-center rounded-lg border border-border bg-secondary">
            <FolderPlus className="size-5" />
          </span>
          <h1 className="text-xl font-semibold tracking-[-0.02em]">Create your first project</h1>
          <p className="max-w-[340px] text-muted-foreground">
            Projects hold your recordings and the documents synthesised from them.
          </p>
          <NewProjectButton>
            {(open) => (
              <Button className="mt-2" onClick={open}>
                <Plus />
                New project
              </Button>
            )}
          </NewProjectButton>
        </Card>
      )}
    </Page>
  )
}
