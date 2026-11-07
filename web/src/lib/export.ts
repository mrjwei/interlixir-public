import { useMutation } from '@tanstack/react-query'
import { useToast } from '@/components/ui/toast'
import { api } from './api'

/** Export Markdown, shared by the document view's button and the sidebar's document menu. */
export function useExportDocument() {
  const toast = useToast()
  return useMutation({
    mutationFn: ({ projectId, documentId }: { projectId: string; documentId: string }) =>
      api.exportDocument(projectId, documentId),
    onSuccess: ({ url, fileName }) => {
      // A 300 s pre-signed GET with Content-Disposition: attachment, so the browser downloads it in place.
      window.location.assign(url)
      toast({ tone: 'success', title: 'Document exported', description: fileName })
    },
    onError: (err) => toast({ tone: 'error', title: "Couldn't export", description: err.message }),
  })
}
