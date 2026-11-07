import { X } from 'lucide-react'
import { Dialog as D } from 'radix-ui'
import type { ComponentProps, ReactNode } from 'react'
import { cn } from '@/lib/utils'

export const Dialog = D.Root
export const DialogTrigger = D.Trigger
export const DialogClose = D.Close

export function DialogContent({
  className,
  children,
  title,
  description,
  ...props
}: ComponentProps<typeof D.Content> & { title: ReactNode; description?: ReactNode }) {
  return (
    <D.Portal>
      <D.Overlay className="fixed inset-0 z-50 bg-overlay" />
      <D.Content
        className={cn(
          'fixed left-1/2 top-1/2 z-50 flex max-h-[calc(100dvh-32px)] w-[calc(100%-32px)] max-w-[480px] -translate-x-1/2 -translate-y-1/2 flex-col gap-4 overflow-y-auto rounded-lg border border-border bg-popover p-6 text-popover-foreground shadow-xl outline-none',
          className,
        )}
        {...props}
      >
        <div className="flex flex-col gap-1.5 pr-8">
          <D.Title className="text-xl font-semibold leading-[1.35] tracking-[-0.02em]">{title}</D.Title>
          {description ? (
            <D.Description className="text-sm text-muted-foreground">{description}</D.Description>
          ) : (
            <D.Description className="sr-only">{typeof title === 'string' ? title : 'Dialog'}</D.Description>
          )}
        </div>
        {children}
        <D.Close
          aria-label="Close"
          className="absolute right-4 top-4 inline-flex size-8 items-center justify-center rounded-md text-muted-foreground transition-colors hover:bg-accent hover:text-foreground focus-ring"
        >
          <X className="size-4" />
        </D.Close>
      </D.Content>
    </D.Portal>
  )
}

export function DialogFooter({ className, ...props }: ComponentProps<'div'>) {
  return <div className={cn('flex flex-wrap justify-end gap-3 pt-2', className)} {...props} />
}

/** A sheet from the left: the mobile sidebar (DESIGN.md, Responsive). */
export function SheetContent({ children, title }: { children: ReactNode; title: string }) {
  return (
    <D.Portal>
      <D.Overlay className="fixed inset-0 z-50 bg-overlay" />
      <D.Content className="fixed inset-y-0 left-0 z-50 flex w-[300px] max-w-[85vw] flex-col bg-sidebar shadow-xl outline-none">
        <D.Title className="sr-only">{title}</D.Title>
        <D.Description className="sr-only">Projects, documents and history</D.Description>
        {children}
      </D.Content>
    </D.Portal>
  )
}
