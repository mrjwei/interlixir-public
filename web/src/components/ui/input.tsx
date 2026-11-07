import type { ComponentProps } from 'react'
import { cn } from '@/lib/utils'

const field =
  'w-full rounded-md border border-input bg-card text-sm text-foreground placeholder:text-muted-foreground outline-none transition-[border-color,box-shadow] duration-[120ms] focus-visible:border-ring focus-visible:ring-[3px] focus-visible:ring-ring/30 aria-invalid:border-destructive aria-invalid:ring-[3px] aria-invalid:ring-destructive/30 disabled:opacity-50'

export function Input({ className, ...props }: ComponentProps<'input'>) {
  return <input className={cn(field, 'h-10 px-3', className)} {...props} />
}

export function Textarea({ className, ...props }: ComponentProps<'textarea'>) {
  return <textarea className={cn(field, 'min-h-24 resize-y px-3 py-2 leading-normal', className)} {...props} />
}

export function Label({ className, ...props }: ComponentProps<'label'>) {
  return <label className={cn('text-sm font-medium leading-[1.35] text-foreground', className)} {...props} />
}

export function FieldError({ id, children }: { id?: string; children: React.ReactNode }) {
  if (!children) return null
  return (
    <p id={id} role="alert" className="flex items-center gap-2 text-sm text-destructive">
      {children}
    </p>
  )
}
