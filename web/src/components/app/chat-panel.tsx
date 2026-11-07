import { ArrowUp, Check, ChevronDown, Clock, LoaderCircle, SquarePen, Sparkles, Trash2, TriangleAlert, X } from 'lucide-react'
import { useQueryClient } from '@tanstack/react-query'
import { useCallback, useEffect, useRef, useState, type FormEvent, type KeyboardEvent } from 'react'
import Markdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { Button } from '@/components/ui/button'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import { useToast } from '@/components/ui/toast'
import { ApiError, QuotaError, api, streamChat } from '@/lib/api'
import { relativeTime } from '@/lib/format'
import { keys, useChatQuota, useChatSessions } from '@/lib/queries'
import type { ChatMessage, ChatQuota, ToolChip } from '@/lib/types'
import { cn } from '@/lib/utils'

// The Ask AI panel (design 10a-10f): a 420px panel over the right of the page (full width on a phone),
// with the session switcher, the conversation, tool chips as the agent works, and the hourly quota (warning from
// 80%, blocked at the limit with the reset time). Loaded on first open, with its Markdown renderer.

const SUGGESTIONS = [
  'Which themes recur across my usability tests?',
  'Summarise my most recent document',
  'What did my uploads cost this week?',
]

interface LiveTool extends ToolChip {
  id: string
  running: boolean
}

/** initialSessionId: open on that saved chat (from a history row); the shell remounts the panel for each one. */
export default function ChatPanel({ initialSessionId, onClose }: { initialSessionId: string | null; onClose: () => void }) {
  const queryClient = useQueryClient()
  const toast = useToast()
  const quota = useChatQuota(true)
  const sessions = useChatSessions(true)

  const [sessionId, setSessionId] = useState<string | null>(initialSessionId)
  const [title, setTitle] = useState<string | null>(null)
  const [messages, setMessages] = useState<ChatMessage[]>([])
  const [loading, setLoading] = useState(!!initialSessionId)
  const [live, setLive] = useState<LiveTool[] | null>(null) // non-null while a turn runs
  const [error, setError] = useState<string | null>(null)
  const [draft, setDraft] = useState('')
  const scroller = useRef<HTMLDivElement>(null)

  const sending = live !== null
  const q = quota.data
  const blocked = !!q?.blocked

  const load = useCallback(
    async (id: string) => {
      try {
        const detail = await queryClient.fetchQuery({ queryKey: keys.chatSession(id), queryFn: () => api.chatSession(id) })
        setTitle(detail.title)
        setMessages(detail.messages)
      } catch (e) {
        setSessionId(null)
        setMessages([])
        setError(e instanceof ApiError && e.status === 404 ? 'That chat no longer exists.' : messageOf(e))
      } finally {
        setLoading(false)
      }
    },
    [queryClient],
  )

  // Opened from a history row: load that chat once.
  useEffect(() => {
    if (initialSessionId) void load(initialSessionId)
  }, [initialSessionId, load])

  const openSession = (id: string | null) => {
    setError(null)
    setSessionId(id)
    setTitle(null)
    setMessages([])
    setLoading(!!id)
    if (id) void load(id)
  }

  useEffect(() => {
    scroller.current?.scrollTo({ top: scroller.current.scrollHeight, behavior: 'smooth' })
  }, [messages, live])

  const send = async (text: string) => {
    text = text.trim()
    if (!text || sending || blocked) return
    setError(null)
    setDraft('')
    setMessages((m) => [...m, { messageId: `local-${Date.now()}`, role: 'user', text, sentAt: new Date().toISOString() }])
    setLive([])
    try {
      await streamChat({ sessionId, text }, ({ event, data }) => {
        if (event === 'session') {
          setSessionId(data.sessionId)
          setTitle((t) => t ?? data.title)
        } else if (event === 'quota') {
          queryClient.setQueryData(keys.chatQuota, data)
        } else if (event === 'tool') {
          setLive((tools) => {
            const rest = (tools ?? []).filter((t) => t.id !== data.id)
            return [...rest, { id: data.id, name: data.name, label: data.label, ok: data.status !== 'error', running: data.status === 'running' }]
          })
        } else if (event === 'message') {
          setMessages((m) => [...m, data])
        } else if (event === 'error') {
          setError(data.detail)
        }
      })
    } catch (e) {
      if (e instanceof QuotaError) queryClient.setQueryData(keys.chatQuota, e.quota)
      setError(messageOf(e))
    } finally {
      setLive(null)
      void queryClient.invalidateQueries({ queryKey: keys.chatSessions })
      void queryClient.invalidateQueries({ queryKey: keys.history })
    }
  }

  const remove = async (id: string) => {
    try {
      await api.deleteChatSession(id)
      queryClient.removeQueries({ queryKey: keys.chatSession(id) })
      void queryClient.invalidateQueries({ queryKey: keys.chatSessions })
      if (id === sessionId) openSession(null)
    } catch (e) {
      toast({ tone: 'error', title: "Couldn't delete the chat", description: messageOf(e) })
    }
  }

  const onSubmit = (e: FormEvent) => {
    e.preventDefault()
    void send(draft)
  }
  const onKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault()
      void send(draft)
    }
  }

  const list = sessions.data ?? []
  return (
    <div
      role="dialog"
      aria-label="Ask AI"
      className="fixed inset-y-0 right-0 z-30 flex w-full flex-col border-l border-border bg-card shadow-xl md:w-[420px]"
    >
      <header className="flex h-16 shrink-0 items-center gap-2 border-b border-border px-4">
        <span className="whitespace-nowrap text-lg font-semibold tracking-[-0.02em]">Ask AI</span>
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <button
              type="button"
              className="flex h-9 min-w-0 flex-1 items-center gap-2 rounded-md border border-border px-3 text-left text-sm hover:bg-accent focus-ring data-[state=open]:bg-accent"
            >
              <span className="min-w-0 flex-1 truncate">{title ?? 'New chat'}</span>
              <ChevronDown className="size-4 shrink-0 text-muted-foreground" />
            </button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="start" className="max-h-[60vh] w-[340px] max-w-[calc(100vw-32px)] overflow-y-auto">
            <DropdownMenuLabel className="text-xs font-medium uppercase tracking-[0.08em]">Recent chats</DropdownMenuLabel>
            {list.length === 0 && <p className="px-2 py-1.5 text-sm text-muted-foreground">No saved chats yet</p>}
            {list.map((s) => (
              <DropdownMenuItem
                key={s.sessionId}
                className="h-auto items-start py-2"
                onSelect={() => openSession(s.sessionId)}
              >
                <Check className={cn('mt-0.5', s.sessionId !== sessionId && 'invisible')} />
                <span className="flex min-w-0 flex-1 flex-col">
                  <span className="line-clamp-2 font-medium">{s.title ?? 'Untitled chat'}</span>
                  <span className="text-xs text-muted-foreground">{relativeTime(s.updatedAt)}</span>
                </span>
                <button
                  type="button"
                  aria-label={`Delete chat ${s.title ?? ''}`}
                  className="inline-flex size-8 shrink-0 items-center justify-center rounded-md hover:bg-secondary focus-ring"
                  onPointerDown={(e) => e.stopPropagation()}
                  onClick={(e) => {
                    e.preventDefault()
                    e.stopPropagation()
                    void remove(s.sessionId)
                  }}
                >
                  <Trash2 />
                </button>
              </DropdownMenuItem>
            ))}
          </DropdownMenuContent>
        </DropdownMenu>
        <Button variant="ghost" size="icon-sm" aria-label="New chat" disabled={sending} onClick={() => openSession(null)}>
          <SquarePen />
        </Button>
        <Button variant="ghost" size="icon-sm" aria-label="Close" onClick={onClose}>
          <X />
        </Button>
      </header>

      <div ref={scroller} className="flex min-h-0 flex-1 flex-col gap-5 overflow-y-auto px-5 py-6">
        {loading && <p className="text-sm text-muted-foreground">Loading chat…</p>}
        {!loading && messages.length === 0 && !sending && (
          <EmptyState disabled={blocked} onPick={(text) => void send(text)} />
        )}
        {messages.map((m) => (m.role === 'user' ? <UserBubble key={m.messageId} text={m.text} /> : <Answer key={m.messageId} message={m} />))}
        {live && (
          <div className="flex flex-col gap-3">
            <Chips tools={live} />
            <p className="flex items-center gap-2 text-sm text-muted-foreground">
              <LoaderCircle className="size-4 animate-spin" />
              {live.some((t) => t.running) ? 'Looking through your data…' : 'Thinking…'}
            </p>
          </div>
        )}
        {error && <p role="alert" className="text-sm text-destructive">{error}</p>}
      </div>

      <form onSubmit={onSubmit} className="flex shrink-0 flex-col gap-2.5 px-4 pb-4 pt-3">
        <QuotaNotice quota={q} />
        <div className={cn('flex flex-col gap-2 rounded-lg border border-border bg-background p-3 focus-within:ring-[3px] focus-within:ring-ring/30', blocked && 'opacity-50')}>
          <textarea
            aria-label="Message"
            rows={2}
            maxLength={2000}
            value={draft}
            disabled={blocked}
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={onKeyDown}
            placeholder={blocked ? 'Message limit reached' : 'Ask about your projects and documents'}
            className="min-h-12 w-full resize-none bg-transparent text-sm outline-none placeholder:text-muted-foreground"
          />
          <div className="flex items-center justify-between gap-2">
            <span className="text-xs text-muted-foreground">
              {q?.used != null && `${q.used} of ${q.limit} messages this hour`}
            </span>
            <Button type="submit" size="icon-sm" aria-label="Send" disabled={!draft.trim() || sending || blocked}>
              <ArrowUp />
            </Button>
          </div>
        </div>
      </form>
    </div>
  )
}

function EmptyState({ onPick, disabled }: { onPick: (text: string) => void; disabled: boolean }) {
  return (
    <div className="my-auto flex flex-col gap-4">
      <span className="inline-flex size-10 items-center justify-center rounded-lg border border-border">
        <Sparkles className="size-5" />
      </span>
      <div className="flex flex-col gap-1.5">
        <h2 className="text-xl font-semibold tracking-[-0.02em]">Ask about your research</h2>
        <p className="text-sm text-muted-foreground">I can read your projects and documents to find patterns, quotes and answers.</p>
      </div>
      <div className="flex flex-col gap-2">
        {SUGGESTIONS.map((s) => (
          <Button key={s} variant="outline" className="h-auto justify-start whitespace-normal py-2.5 text-left" disabled={disabled} onClick={() => onPick(s)}>
            {s}
          </Button>
        ))}
      </div>
    </div>
  )
}

function UserBubble({ text }: { text: string }) {
  return <div className="ml-10 self-end whitespace-pre-wrap rounded-lg bg-secondary px-4 py-3 text-sm">{text}</div>
}

function Answer({ message }: { message: ChatMessage }) {
  return (
    <div className="flex flex-col gap-3">
      {message.tools && message.tools.length > 0 && <Chips tools={message.tools} />}
      {/* react-markdown renders no raw HTML, so model output can't inject markup. */}
      <div className="prose-chat">
        <Markdown remarkPlugins={[remarkGfm]}>{message.text}</Markdown>
      </div>
    </div>
  )
}

function Chips({ tools }: { tools: (ToolChip & { running?: boolean })[] }) {
  return (
    <ul className="flex flex-col items-start gap-1.5" aria-label="Tools used">
      {tools.map((t, i) => (
        <li
          key={i}
          className={cn(
            'inline-flex max-w-full items-start gap-1.5 rounded-sm bg-secondary px-2 py-1 text-xs text-muted-foreground',
            !t.ok && 'text-destructive',
          )}
        >
          {t.running ? (
            <LoaderCircle className="mt-px size-3.5 shrink-0 animate-spin" />
          ) : t.ok ? (
            <Check className="mt-px size-3.5 shrink-0" />
          ) : (
            <X className="mt-px size-3.5 shrink-0" />
          )}
          <span className="min-w-0 break-words">{t.label ? `${t.name} · ${t.label}` : t.name}</span>
        </li>
      ))}
    </ul>
  )
}

function QuotaNotice({ quota }: { quota: ChatQuota | undefined }) {
  if (!quota || quota.used == null) return null
  const box = 'flex items-start gap-3 rounded-lg border border-[color-mix(in_oklab,var(--warning)_40%,transparent)] bg-[color-mix(in_oklab,var(--warning)_9%,var(--card))] p-3 text-sm'
  if (quota.blocked) {
    const at = quota.resetAt
      ? new Date(quota.resetAt).toLocaleTimeString('en-AU', { hour: 'numeric', minute: '2-digit' })
      : 'the top of the hour'
    return (
      <div role="alert" className={box}>
        <Clock className="mt-px size-[18px] shrink-0 text-warning-fg" />
        <span>
          You've reached {quota.limit} messages this hour. You can send more at {at}.
        </span>
      </div>
    )
  }
  if (quota.warning) {
    return (
      <div role="status" className={box}>
        <TriangleAlert className="mt-px size-[18px] shrink-0 text-warning-fg" />
        <span>
          You've used {quota.used} of {quota.limit} messages this hour.
        </span>
      </div>
    )
  }
  return null
}

function messageOf(e: unknown) {
  return e instanceof Error ? e.message : 'Something went wrong. Try again in a moment.'
}
