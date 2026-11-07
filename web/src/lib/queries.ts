import {
  useInfiniteQuery,
  useQueries,
  useQuery,
  useQueryClient,
  type QueryClient,
} from '@tanstack/react-query'
import { api } from './api'
import { isTerminal } from './jobs'
import type { Job } from './types'

export const POLL_MS = 3000

export const keys = {
  projects: ['projects'] as const,
  project: (projectId: string) => ['project', projectId] as const,
  job: (jobId: string) => ['job', jobId] as const,
  schemas: ['schemas'] as const,
  document: (projectId: string, documentId: string) => ['document', projectId, documentId] as const,
  history: ['history'] as const,
  chatQuota: ['chat', 'quota'] as const,
  chatSessions: ['chat', 'sessions'] as const,
  chatSession: (sessionId: string) => ['chat', 'session', sessionId] as const,
}

export function useProjects() {
  return useQuery({ queryKey: keys.projects, queryFn: api.projects })
}

export function useProject(projectId: string | undefined) {
  return useQuery({
    queryKey: keys.project(projectId ?? ''),
    queryFn: () => api.project(projectId!),
    enabled: !!projectId,
  })
}

export function useSchemas() {
  return useQuery({ queryKey: keys.schemas, queryFn: api.schemas, staleTime: 5 * 60_000 })
}

export function useDocument(projectId: string | undefined, documentId: string | null | undefined) {
  return useQuery({
    queryKey: keys.document(projectId ?? '', documentId ?? ''),
    queryFn: () => api.document(projectId!, documentId!),
    enabled: !!projectId && !!documentId,
    staleTime: 60_000,
  })
}

/**
 * Fetch a job, and when its stage moves on, refresh what depends on it: the project (a Done job adds a
 * Document with its title) and history. Polling every ~3 s stops by itself once the job is Done or Failed.
 */
function jobQuery(queryClient: QueryClient, jobId: string) {
  return {
    queryKey: keys.job(jobId),
    queryFn: async () => {
      const previous = queryClient.getQueryData<Job>(keys.job(jobId))
      const job = await api.job(jobId)
      if (previous && previous.stage !== job.stage) {
        if (job.projectId) void queryClient.invalidateQueries({ queryKey: keys.project(job.projectId) })
        void queryClient.invalidateQueries({ queryKey: keys.history })
      }
      return job
    },
    refetchInterval: (query: { state: { data?: Job } }) => (isTerminal(query.state.data?.stage) ? false : POLL_MS),
  }
}

export function useJob(jobId: string | undefined) {
  const queryClient = useQueryClient()
  return useQuery({ ...jobQuery(queryClient, jobId ?? ''), enabled: !!jobId })
}

/** Every job of a project, keyed by jobId (the sidebar tree and the project table). */
export function useJobs(jobIds: string[]) {
  const queryClient = useQueryClient()
  return useQueries({
    queries: jobIds.map((id) => jobQuery(queryClient, id)),
    combine: (results) => {
      const byId: Record<string, Job | undefined> = {}
      results.forEach((r, i) => (byId[jobIds[i]] = r.data))
      return { byId, isLoading: results.some((r) => r.isLoading) }
    },
  })
}

export function useHistory() {
  return useInfiniteQuery({
    queryKey: keys.history,
    queryFn: ({ pageParam }) => api.history(pageParam, pageParam ? 10 : 5),
    initialPageParam: null as string | null,
    getNextPageParam: (last) => last.nextCursor,
  })
}

export function useChatQuota(enabled: boolean) {
  return useQuery({ queryKey: keys.chatQuota, queryFn: api.chatQuota, enabled, staleTime: 0 })
}

export function useChatSessions(enabled: boolean) {
  return useQuery({ queryKey: keys.chatSessions, queryFn: api.chatSessions, enabled })
}
