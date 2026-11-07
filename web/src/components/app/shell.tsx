import { ChevronRight, Menu, Moon, Sparkles, Sun } from 'lucide-react'
import { Suspense, createContext, lazy, useCallback, useContext, useState, type ReactNode } from 'react'
import { Link, Outlet } from 'react-router'
import { Button } from '@/components/ui/button'
import { Dialog, SheetContent } from '@/components/ui/dialog'
import { useTheme } from '@/lib/theme'
import { cn } from '@/lib/utils'
import { Sidebar } from './sidebar'

// App shell (DESIGN.md, Layout): a 240px sidebar and a main column with a top bar. Below 768px the sidebar
// becomes a sheet opened from the top bar. The Ask AI panel opens over the right of the page from the top
// bar, or on a saved chat from its history row; it stays open across pages.

const ChatPanel = lazy(() => import('./chat-panel'))

const SheetContext = createContext<() => void>(() => {})

interface ChatControl {
  open: boolean
  /** Open the panel; with a sessionId, on that saved chat. */
  openChat: (sessionId?: string) => void
  toggle: () => void
}

const ChatContext = createContext<ChatControl>({ open: false, openChat: () => {}, toggle: () => {} })

export const useChat = () => useContext(ChatContext)

export function AppShell() {
  const [sheetOpen, setSheetOpen] = useState(false)
  const [chatOpen, setChatOpen] = useState(false)
  // A history row's chat remounts the panel on that session (the nonce makes a repeat click reload it too).
  const [chatRequest, setChatRequest] = useState<{ sessionId: string | null; nonce: number }>({ sessionId: null, nonce: 0 })
  const openChat = useCallback((sessionId?: string) => {
    setChatOpen(true)
    if (sessionId) setChatRequest((r) => ({ sessionId, nonce: r.nonce + 1 }))
  }, [])
  const chat: ChatControl = { open: chatOpen, openChat, toggle: () => setChatOpen((o) => !o) }
  return (
    <ChatContext.Provider value={chat}>
    <SheetContext.Provider value={() => setSheetOpen(true)}>
      <div className="flex h-dvh overflow-hidden bg-background">
        <aside className="hidden w-60 shrink-0 border-r border-sidebar-border bg-sidebar md:block">
          <Sidebar />
        </aside>
        <Dialog open={sheetOpen} onOpenChange={setSheetOpen}>
          <SheetContent title="Navigation">
            <Sidebar onClose={() => setSheetOpen(false)} />
          </SheetContent>
        </Dialog>
        <div className="flex min-w-0 flex-1 flex-col">
          <Outlet />
        </div>
        {chatOpen && (
          <Suspense fallback={null}>
            <ChatPanel key={chatRequest.nonce} initialSessionId={chatRequest.sessionId} onClose={() => setChatOpen(false)} />
          </Suspense>
        )}
      </div>
    </SheetContext.Provider>
    </ChatContext.Provider>
  )
}

export interface Crumb {
  label: string
  to?: string
}

/** A page inside the shell: the top bar with breadcrumbs, then the scrolling content area. */
export function Page({
  crumbs,
  children,
  width = 'full',
}: {
  crumbs: Crumb[]
  children: ReactNode
  width?: 'full' | 'doc' | 'form'
}) {
  const openSheet = useContext(SheetContext)
  const chat = useChat()
  const { theme, toggle } = useTheme()
  return (
    <>
      <header className="flex h-16 shrink-0 items-center gap-3 border-b border-border bg-card px-4 md:px-7">
        <Button variant="ghost" size="icon-sm" className="md:hidden" aria-label="Open navigation" onClick={openSheet}>
          <Menu />
        </Button>
        <nav aria-label="Breadcrumb" className="min-w-0 flex-1">
          <ol className="flex min-w-0 items-center gap-2 text-lg font-semibold tracking-[-0.02em]">
            {crumbs.map((c, i) => {
              const last = i === crumbs.length - 1
              return (
                <li key={i} className={cn('flex min-w-0 items-center gap-2', !last && 'max-md:hidden')}>
                  {i > 0 && <ChevronRight className="size-4 shrink-0 text-muted-foreground max-md:hidden" />}
                  {c.to && !last ? (
                    <Link to={c.to} className="truncate text-muted-foreground no-underline hover:text-foreground">
                      {c.label}
                    </Link>
                  ) : (
                    <span className={cn('truncate', !last && 'text-muted-foreground')} aria-current={last ? 'page' : undefined}>
                      {c.label}
                    </span>
                  )}
                </li>
              )
            })}
          </ol>
        </nav>
        <Button
          variant="outline"
          className={cn('max-md:hidden', chat.open && 'bg-accent [&_svg]:text-primary')}
          aria-pressed={chat.open}
          onClick={chat.toggle}
        >
          <Sparkles />
          Ask AI
        </Button>
        <Button
          variant="outline"
          size="icon-sm"
          className={cn('md:hidden', chat.open && 'bg-accent [&_svg]:text-primary')}
          aria-label="Ask AI"
          aria-pressed={chat.open}
          onClick={chat.toggle}
        >
          <Sparkles />
        </Button>
        <Button
          variant="outline"
          size="icon-sm"
          aria-label={theme === 'dark' ? 'Switch to light theme' : 'Switch to dark theme'}
          onClick={toggle}
        >
          {theme === 'dark' ? <Sun /> : <Moon />}
        </Button>
      </header>
      <main className="min-h-0 flex-1 overflow-y-auto p-4 md:p-7">
        <div
          className={cn(
            'mx-auto w-full',
            width === 'doc' && 'max-w-[960px]',
            width === 'form' && 'max-w-[720px]',
          )}
        >
          {children}
        </div>
      </main>
    </>
  )
}
