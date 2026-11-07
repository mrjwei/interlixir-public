// Response shapes of the /a3 API (services/api/README.md) and the auth service's auth routes.

export type Stage = 'Queued' | 'Transcribing' | 'Redacting' | 'Synthesising' | 'Done' | 'Failed'

export interface Project {
  projectId: string
  name: string
  createdAt: string | null
}

export interface RecordingRef {
  recordingId: string
  fileName: string | null
  jobId: string | null
}

export interface DocumentRef {
  documentId: string
  title: string | null
  jobId: string | null
}

export interface ProjectDetail extends Project {
  recordings: RecordingRef[]
  documents: DocumentRef[]
}

export interface Job {
  jobId: string
  projectId: string | null
  recordingId: string | null
  schemaId: string | null
  instructionText: string | null
  createdAt: string | null
  stage: Stage
  audioSeconds: number | null
  chunksTotal: number | null
  chunksDone: number
  retryCount: number
  failureReason: string | null
  documentId: string | null
  tokenCount: number | null
  cost: number | null
}

export interface Schema {
  schemaId: string
  name: string | null
  description: string | null
  builtIn: boolean
}

export interface DocumentBody {
  documentId: string
  projectId: string
  jobId: string | null
  title: string | null
  markdown: string
  contextFiles: { contextFileId: string; fileName: string | null }[]
}

/** PATCH .../documents/{documentId}: a rename and/or a Markdown edit. */
export interface DocumentChanged {
  documentId: string
  projectId: string
  title: string | null
  updatedAt: string
}

export interface PostPolicy {
  url: string
  fields: Record<string, string>
}

export interface UploadResponse {
  expiresIn: number
  recordings: { recordingId: string; jobId: string; fileName: string; upload: PostPolicy }[]
  contextFiles: { contextFileId: string; fileName: string; upload: PostPolicy }[]
}

export interface HistoryEvent {
  eventId: string
  eventType: string | null
  occurredAt: string
  jobId?: string
  chatSessionId?: string
  /** Job events: the job's project and recording, copied by the history writer (absent if the job was gone). */
  projectId?: string
  fileName?: string
  /** JobFailed: the stage the job failed in. */
  stage?: Stage
  tokenCount?: number
  cost?: number
}

export interface HistoryPage {
  events: HistoryEvent[]
  nextCursor: string | null
}

// The chat agent's /acp routes (services/chat-agent/README.md).

export interface ChatQuota {
  /** null when the agent couldn't read the counter (no cache): nothing to show. */
  used: number | null
  limit: number
  resetAt: string | null
  warning: boolean
  blocked: boolean
}

export interface ToolChip {
  name: string
  ok: boolean
  /** A document title or project name, shown after the tool name. */
  label?: string
}

export interface ChatMessage {
  messageId: string
  role: 'user' | 'assistant'
  text: string
  sentAt: string
  tools?: ToolChip[]
  tokenCount?: number
  cost?: number
}

export interface ChatSession {
  sessionId: string
  title: string | null
  createdAt: string | null
  updatedAt: string | null
  messageCount: number
  tokenCount: number
  cost: number
}

export interface ChatSessionDetail extends ChatSession {
  messages: ChatMessage[]
}

/** One server-sent event of POST /acp/messages. */
export type ChatEvent =
  | { event: 'session'; data: { sessionId: string; title: string | null; created: boolean } }
  | { event: 'quota'; data: ChatQuota }
  | { event: 'tool'; data: { id: string; name: string; status: 'running' | 'done' | 'error'; label?: string } }
  | { event: 'message'; data: ChatMessage }
  | { event: 'error'; data: { detail: string } }
  | { event: 'done'; data: Record<string, never> }
