import { cn } from '@/lib/utils'

export function Progress({ value, className, label }: { value: number; className?: string; label?: string }) {
  const pct = Math.max(0, Math.min(100, Math.round(value)))
  return (
    <div
      role="progressbar"
      aria-valuemin={0}
      aria-valuemax={100}
      aria-valuenow={pct}
      aria-label={label}
      className={cn('h-2 overflow-hidden rounded-full bg-secondary', className)}
    >
      <div className="h-full rounded-full bg-primary transition-[width] duration-[260ms] ease-out" style={{ width: `${pct}%` }} />
    </div>
  )
}
