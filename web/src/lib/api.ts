import type {
  ChatEvent,
  ChatQuota,
  ChatSession,
  ChatSessionDetail,
  DocumentBody,
  DocumentChanged,
  HistoryPage,
  Job,
  Project,
  ProjectDetail,
  Schema,
  UploadResponse,
} from './types'

// Same origin in every environment: CloudFront routes /a3/* and /v1/auth/* to the API and /acp/* to the chat
// agent, and the Vite dev server proxies them. So paths are relative and there is no CORS.

export class ApiError extends Error {
  readonly status: number
  constructor(status: number, message: string) {
    super(message)
    this.status = status
  }
}

// The ID token is held in this module variable; AuthProvider mirrors it to sessionStorage so a page
// refresh restores it (cleared on logout, 401, expiry and tab close).
let token: string | null = null
let onUnauthorized: (() => void) | null = null

export function setToken(value: string | null) {
  token = value
}

export function setUnauthorizedHandler(handler: (() => void) | null) {
  onUnauthorized = handler
}

/** A readable message from FastAPI ({detail}), its 422 list, the auth service ({error, message}) or API Gateway ({message}). */
function messageFrom(status: number, body: unknown): string {
  if (body && typeof body === 'object') {
    const b = body as Record<string, unknown>
    if (typeof b.detail === 'string') return b.detail
    if (Array.isArray(b.detail) && b.detail.length > 0) {
      const first = b.detail[0] as { msg?: string }
      if (first?.msg) return first.msg.replace(/^Value error, /, '')
    }
    if (typeof b.message === 'string') return b.message
  }
  return status >= 500 ? "Something went wrong on our side. Try again in a moment." : `Request failed (${status})`
}

async function request<T>(method: string, path: string, body?: unknown, auth = true): Promise<T> {
  const headers: Record<string, string> = {}
  if (body !== undefined) headers['Content-Type'] = 'application/json'
  if (auth && token) headers.Authorization = `Bearer ${token}`

  let response: Response
  try {
    response = await fetch(path, { method, headers, body: body === undefined ? undefined : JSON.stringify(body) })
  } catch {
    throw new ApiError(0, "Couldn't reach the server. Check your connection and try again.")
  }

  const text = await response.text()
  let data: unknown = null
  if (text) {
    try {
      data = JSON.parse(text)
    } catch {
      data = text
    }
  }
  if (!response.ok) {
    // An expired or rejected token on an /a3 call sends the user back to log in.
    if (response.status === 401 && auth) onUnauthorized?.()
    throw new ApiError(response.status, messageFrom(response.status, data))
  }
  return data as T
}

/** 429 from the chat agent: this hour's messages are used up until quota.resetAt. */
export class QuotaError extends ApiError {
  readonly quota: ChatQuota
  constructor(message: string, quota: ChatQuota) {
    super(429, message)
    this.quota = quota
  }
}

/**
 * POST /acp/messages: one chat turn, its server-sent events passed to onEvent as they arrive. Read with fetch,
 * not EventSource, which can't send the Authorization header. Throws QuotaError over the quota, ApiError otherwise.
 */
export async function streamChat(
  body: { sessionId?: string | null; text: string },
  onEvent: (event: ChatEvent) => void,
) {
  let response: Response
  try {
    response = await fetch('/acp/messages', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', ...(token ? { Authorization: `Bearer ${token}` } : {}) },
      body: JSON.stringify(body.sessionId ? body : { text: body.text }),
    })
  } catch {
    throw new ApiError(0, "Couldn't reach the server. Check your connection and try again.")
  }
  if (!response.ok || !response.body) {
    let data: unknown = null
    try {
      data = await response.json()
    } catch {
      // not JSON: CloudFront or the ALB answered
    }
    if (response.status === 401) onUnauthorized?.()
    const b = data as { quota?: ChatQuota } | null
    if (response.status === 429 && b?.quota) throw new QuotaError(messageFrom(429, data), b.quota)
    throw new ApiError(response.status, messageFrom(response.status, data))
  }

  const reader = response.body.pipeThrough(new TextDecoderStream()).getReader()
  let buffer = ''
  for (;;) {
    const { value, done } = await reader.read()
    if (done) break
    buffer += value
    let end: number
    while ((end = buffer.indexOf('\n\n')) >= 0) {
      const block = buffer.slice(0, end)
      buffer = buffer.slice(end + 2)
      let event = 'message'
      let data = ''
      for (const line of block.split('\n')) {
        if (line.startsWith('event: ')) event = line.slice(7)
        else if (line.startsWith('data: ')) data += line.slice(6)
        // lines starting with ':' are the agent's heartbeats
      }
      if (data) onEvent({ event, data: JSON.parse(data) } as ChatEvent)
    }
  }
}

export interface AuthResult {
  token: string
  user: { email?: string; username?: string; id?: string }
}

export const api = {
  login: (email: string, password: string) =>
    request<AuthResult>('POST', '/v1/auth/login', { email, password }, false),
  register: (email: string, password: string) =>
    request<AuthResult>('POST', '/v1/auth/register', { email, password }, false),

  projects: () => request<{ projects: Project[] }>('GET', '/a3/projects').then((r) => r.projects),
  createProject: (name: string) => request<Project>('POST', '/a3/projects', { name }),
  project: (projectId: string) => request<ProjectDetail>('GET', `/a3/projects/${encodeURIComponent(projectId)}`),
  renameProject: (projectId: string, name: string) =>
    request<Project>('PATCH', `/a3/projects/${encodeURIComponent(projectId)}`, { name }),
  /** 204 once the project is gone from the list; GC removes its recordings, documents and files. */
  deleteProject: (projectId: string) => request<null>('DELETE', `/a3/projects/${encodeURIComponent(projectId)}`),

  schemas: () => request<{ schemas: Schema[] }>('GET', '/a3/schemas').then((r) => r.schemas),

  createUploads: (
    projectId: string,
    body: {
      schemaId: string
      instructionText: string
      recordings: { fileName: string; contentType: string; sizeBytes: number }[]
      contextFiles: { fileName: string; contentType: string; sizeBytes: number }[]
    },
  ) => request<UploadResponse>('POST', `/a3/projects/${encodeURIComponent(projectId)}/uploads`, body),

  job: (jobId: string) => request<Job>('GET', `/a3/jobs/${encodeURIComponent(jobId)}`),
  retryJob: (jobId: string) => request<Job>('POST', `/a3/jobs/${encodeURIComponent(jobId)}/retry`),

  document: (projectId: string, documentId: string) =>
    request<DocumentBody>(
      'GET',
      `/a3/projects/${encodeURIComponent(projectId)}/documents/${encodeURIComponent(documentId)}`,
    ),
  updateDocument: (projectId: string, documentId: string, change: { title?: string; markdown?: string }) =>
    request<DocumentChanged>(
      'PATCH',
      `/a3/projects/${encodeURIComponent(projectId)}/documents/${encodeURIComponent(documentId)}`,
      change,
    ),
  /** 204 once the document and its recording are gone; GC removes the job, audio and transcripts. */
  deleteDocument: (projectId: string, documentId: string) =>
    request<null>('DELETE', `/a3/projects/${encodeURIComponent(projectId)}/documents/${encodeURIComponent(documentId)}`),
  exportDocument: (projectId: string, documentId: string) =>
    request<{ url: string; fileName: string; expiresIn: number }>(
      'GET',
      `/a3/projects/${encodeURIComponent(projectId)}/documents/${encodeURIComponent(documentId)}/export`,
    ),

  chatQuota: () => request<ChatQuota>('GET', '/acp/quota'),
  chatSessions: () => request<{ sessions: ChatSession[] }>('GET', '/acp/sessions').then((r) => r.sessions),
  chatSession: (sessionId: string) =>
    request<ChatSessionDetail>('GET', `/acp/sessions/${encodeURIComponent(sessionId)}`),
  deleteChatSession: (sessionId: string) =>
    request<null>('DELETE', `/acp/sessions/${encodeURIComponent(sessionId)}`),

  history: (cursor?: string | null, limit = 5) => {
    const query = new URLSearchParams({ limit: String(limit) })
    if (cursor) query.set('cursor', cursor)
    return request<HistoryPage>('GET', `/a3/history?${query}`)
  },
}
