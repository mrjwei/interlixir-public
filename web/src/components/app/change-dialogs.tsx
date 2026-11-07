import { useState } from 'react'
import { useLocation, useNavigate } from 'react-router'
import { useDeleteDocument, useDeleteProject, useRenameProject, useUpdateDocument } from '@/lib/changes'
import { ConfirmDeleteDialog, RenameDialog } from './rename-delete-dialogs'

// Rename and delete for documents and projects. Each hook returns openers plus the dialogs to render next to
// (not inside) the menu that opens them, so closing the menu doesn't unmount an open dialog.

type Open = 'rename' | 'delete' | null

/** Rename and Delete for one document: the sidebar menu and the document view both open these. */
export function useDocumentActions({
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
  const [open, setOpen] = useState<Open>(null)
  const rename = useUpdateDocument()
  const remove = useDeleteDocument()
  const navigate = useNavigate()
  const { pathname } = useLocation()
  const close = (next: boolean) => {
    if (!next) {
      setOpen(null)
      rename.reset()
      remove.reset()
    }
  }

  const dialogs = (
    <>
      <RenameDialog
        open={open === 'rename'}
        onOpenChange={close}
        title="Rename document"
        initial={title}
        maxLength={200}
        pending={rename.isPending}
        error={rename.error?.message}
        onSubmit={(value) => rename.mutate({ projectId, documentId, title: value }, { onSuccess: () => close(false) })}
      />
      <ConfirmDeleteDialog
        open={open === 'delete'}
        onOpenChange={close}
        title="Delete this document?"
        description="This removes the document, its recording, transcript and context files. This can't be undone."
        confirmLabel="Delete document"
        pending={remove.isPending}
        error={remove.error?.message}
        onConfirm={() =>
          remove.mutate(
            { projectId, documentId, jobId },
            {
              onSuccess: () => {
                close(false)
                // Leave the page of what was just deleted.
                if (jobId && pathname === `/projects/${projectId}/jobs/${jobId}`) navigate(`/projects/${projectId}`)
              },
            },
          )
        }
      />
    </>
  )
  return { openRename: () => setOpen('rename'), openDelete: () => setOpen('delete'), dialogs }
}

/** Rename and Delete for one project (the project page's menu). */
export function useProjectActions({ projectId, name }: { projectId: string; name: string }) {
  const [open, setOpen] = useState<Open>(null)
  const rename = useRenameProject()
  const remove = useDeleteProject()
  const navigate = useNavigate()
  const close = (next: boolean) => {
    if (!next) {
      setOpen(null)
      rename.reset()
      remove.reset()
    }
  }

  const dialogs = (
    <>
      <RenameDialog
        open={open === 'rename'}
        onOpenChange={close}
        title="Rename project"
        initial={name}
        maxLength={100}
        pending={rename.isPending}
        error={rename.error?.message}
        onSubmit={(value) => rename.mutate({ projectId, name: value }, { onSuccess: () => close(false) })}
      />
      <ConfirmDeleteDialog
        open={open === 'delete'}
        onOpenChange={close}
        title="Delete this project?"
        description="This removes the project with every recording, transcript, context file and document in it. This can't be undone."
        confirmLabel="Delete project"
        pending={remove.isPending}
        error={remove.error?.message}
        onConfirm={() =>
          remove.mutate(
            { projectId, name },
            {
              onSuccess: () => {
                close(false)
                navigate('/')
              },
            },
          )
        }
      />
    </>
  )
  return { openRename: () => setOpen('rename'), openDelete: () => setOpen('delete'), dialogs }
}
