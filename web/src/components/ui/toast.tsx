import { CircleCheck, CircleAlert, Info } from 'lucide-react'
import { createContext, useCallback, useContext, useMemo, useState, type ReactNode } from 'react'

// Bottom-right, popover surface, status shown by the leading icon, auto-dismiss (DESIGN.md, Toast).

type Tone = 'success' | 'info' | 'error'
interface Toast {
  id: number
  tone: Tone
  title: string
  description?: string
}

const ToastContext = createContext<((t: Omit<Toast, 'id'>) => void) | null>(null)
let nextId = 1

export function Toaster({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<Toast[]>([])
  const push = useCallback((t: Omit<Toast, 'id'>) => {
    const id = nextId++
    setToasts((list) => [...list.slice(-3), { ...t, id }])
    window.setTimeout(() => setToasts((list) => list.filter((x) => x.id !== id)), 4500)
  }, [])
  const value = useMemo(() => push, [push])
  return (
    <ToastContext.Provider value={value}>
      {children}
      <div
        aria-live="polite"
        className="pointer-events-none fixed bottom-4 right-4 z-[60] flex w-[min(360px,calc(100vw-32px))] flex-col gap-2"
      >
        {toasts.map((t) => {
          const Icon = t.tone === 'success' ? CircleCheck : t.tone === 'error' ? CircleAlert : Info
          const color = t.tone === 'success' ? 'text-success-fg' : t.tone === 'error' ? 'text-destructive' : 'text-foreground'
          return (
            <div
              key={t.id}
              role="status"
              className="pointer-events-auto flex gap-3 rounded-lg border border-border bg-popover p-4 text-sm text-popover-foreground shadow-lg"
            >
              <Icon className={`mt-px size-[18px] shrink-0 ${color}`} />
              <div className="flex min-w-0 flex-col gap-0.5">
                <div className="font-medium">{t.title}</div>
                {t.description && <div className="truncate text-muted-foreground">{t.description}</div>}
              </div>
            </div>
          )
        })}
      </div>
    </ToastContext.Provider>
  )
}

export function useToast() {
  const push = useContext(ToastContext)
  if (!push) throw new Error('useToast outside Toaster')
  return push
}
