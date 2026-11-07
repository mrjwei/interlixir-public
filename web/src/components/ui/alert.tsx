import { CircleAlert, CircleCheck, Info, TriangleAlert } from 'lucide-react'
import type { ReactNode } from 'react'
import { cn } from '@/lib/utils'

const tones = {
  info: { icon: Info, box: 'bg-card', icon_: 'text-info' },
  success: { icon: CircleCheck, box: 'bg-[color-mix(in_oklab,var(--success)_7%,var(--card))]', icon_: 'text-success-fg' },
  warning: { icon: TriangleAlert, box: 'bg-[color-mix(in_oklab,var(--warning)_9%,var(--card))]', icon_: 'text-warning-fg' },
  destructive: {
    icon: CircleAlert,
    box: 'bg-[color-mix(in_oklab,var(--destructive)_6%,var(--card))] text-destructive border-[color-mix(in_oklab,var(--destructive)_30%,transparent)]',
    icon_: 'text-destructive',
  },
}

export function Alert({
  tone = 'info',
  title,
  children,
  className,
}: {
  tone?: keyof typeof tones
  title?: ReactNode
  children?: ReactNode
  className?: string
}) {
  const t = tones[tone]
  const Icon = t.icon
  return (
    <div
      role={tone === 'destructive' ? 'alert' : 'status'}
      className={cn('flex gap-3 rounded-lg border border-border p-4 text-sm', t.box, className)}
    >
      <Icon className={cn('mt-px size-[18px] shrink-0', t.icon_)} />
      <div className="flex min-w-0 flex-1 flex-col gap-1">
        {title && <div className="font-semibold leading-[1.35]">{title}</div>}
        {children && <div className={cn(tone !== 'destructive' && 'text-muted-foreground')}>{children}</div>}
      </div>
    </div>
  )
}
