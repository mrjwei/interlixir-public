import { useMutation, useQueryClient } from '@tanstack/react-query'
import { useToast } from '@/components/ui/toast'
import { api } from './api'
import { keys } from './queries'
import type { DocumentBody, Project, ProjectDetail } from './types'

// Rename, edit and delete. Each one updates the cached queries in place, then refetches them: the API has
// already dropped its own cached copies, so the refetch sees the change.

export function useRenameProject() {
  const queryClient = useQueryClient()
  const toast = useToast()
  return useMutation({
    mutationFn: ({ projectId, name }: { projectId: string; name: string }) => api.renameProject(projectId, name),
    onSuccess: (project) => {
      queryClient.setQueryData<Project[]>(keys.projects, (list) =>
        list?.map((p) => (p.projectId === project.projectId ? { ...p, name: project.name } : p)),
      )
      queryClient.setQueryData<ProjectDetail>(keys.project(project.projectId), (d) => d && { ...d, name: project.name })
      toast({ tone: 'success', title: 'Project renamed', description: project.name })
    },
  })
}

export function useDeleteProject() {
  const queryClient = useQueryClient()
  const toast = useToast()
  return useMutation({
    mutationFn: ({ projectId }: { projectId: string; name: string }) => api.deleteProject(projectId),
    onSuccess: (_, { projectId, name }) => {
      queryClient.setQueryData<Project[]>(keys.projects, (list) => list?.filter((p) => p.projectId !== projectId))
      queryClient.removeQueries({ queryKey: keys.project(projectId) })
      void queryClient.invalidateQueries({ queryKey: keys.projects })
      toast({ tone: 'success', title: 'Project deleted', description: name })
    },
  })
}

interface DocumentRef {
  projectId: string
  documentId: string
}

/** A rename (title), an edit (markdown), or both. */
export function useUpdateDocument() {
  const queryClient = useQueryClient()
  const toast = useToast()
  return useMutation({
    mutationFn: ({ projectId, documentId, ...change }: DocumentRef & { title?: string; markdown?: string }) =>
      api.updateDocument(projectId, documentId, change),
    onSuccess: (changed, { projectId, documentId, markdown }) => {
      queryClient.setQueryData<DocumentBody>(
        keys.document(projectId, documentId),
        (d) => d && { ...d, title: changed.title, markdown: markdown ?? d.markdown },
      )
      queryClient.setQueryData<ProjectDetail>(
        keys.project(projectId),
        (d) =>
          d && {
            ...d,
            documents: d.documents.map((doc) => (doc.documentId === documentId ? { ...doc, title: changed.title } : doc)),
          },
      )
      // A rename alone can rewrite the Markdown's heading on the server, so the document is read again.
      if (markdown === undefined) void queryClient.invalidateQueries({ queryKey: keys.document(projectId, documentId) })
      toast({ tone: 'success', title: markdown === undefined ? 'Document renamed' : 'Changes saved' })
    },
  })
}

export function useDeleteDocument() {
  const queryClient = useQueryClient()
  const toast = useToast()
  return useMutation({
    mutationFn: ({ projectId, documentId }: DocumentRef & { jobId: string | null }) =>
      api.deleteDocument(projectId, documentId),
    onSuccess: (_, { projectId, documentId, jobId }) => {
      // The API deletes the Recording with the Document, so the sidebar entry goes too.
      queryClient.setQueryData<ProjectDetail>(
        keys.project(projectId),
        (d) =>
          d && {
            ...d,
            recordings: d.recordings.filter((r) => r.jobId !== jobId),
            documents: d.documents.filter((doc) => doc.documentId !== documentId),
          },
      )
      queryClient.removeQueries({ queryKey: keys.document(projectId, documentId) })
      if (jobId) queryClient.removeQueries({ queryKey: keys.job(jobId) })
      void queryClient.invalidateQueries({ queryKey: keys.project(projectId) })
      toast({ tone: 'success', title: 'Document deleted' })
    },
  })
}
