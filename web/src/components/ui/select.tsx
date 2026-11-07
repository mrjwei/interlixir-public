import { Check, ChevronDown } from 'lucide-react'
import { Select as S } from 'radix-ui'
import type { ComponentProps, ReactNode } from 'react'
import { cn } from '@/lib/utils'

export const Select = S.Root
export const SelectGroup = S.Group

export function SelectTrigger({ className, children, ...props }: ComponentProps<typeof S.Trigger>) {
  return (
    <S.Trigger
      className={cn(
        'flex h-10 w-full items-center justify-between gap-2 rounded-md border border-input bg-card pl-3 pr-3 text-left text-sm outline-none transition-[border-color,box-shadow] duration-[120ms] focus-visible:border-ring focus-visible:ring-[3px] focus-visible:ring-ring/30 data-[placeholder]:text-muted-foreground disabled:opacity-50 [&>span]:truncate',
        className,
      )}
      {...props}
    >
      {children}
      <S.Icon asChild>
        <ChevronDown className="size-4 shrink-0 text-muted-foreground" />
      </S.Icon>
    </S.Trigger>
  )
}

export const SelectValue = S.Value

export function SelectContent({ className, children, ...props }: ComponentProps<typeof S.Content>) {
  return (
    <S.Portal>
      <S.Content
        position="popper"
        sideOffset={4}
        className={cn(
          'z-50 max-h-[min(var(--radix-select-content-available-height),360px)] w-[var(--radix-select-trigger-width)] overflow-y-auto rounded-lg border border-border bg-popover p-1 text-popover-foreground shadow-lg',
          className,
        )}
        {...props}
      >
        <S.Viewport>{children}</S.Viewport>
      </S.Content>
    </S.Portal>
  )
}

export function SelectItem({
  className,
  children,
  aside,
  ...props
}: ComponentProps<typeof S.Item> & { aside?: ReactNode }) {
  return (
    <S.Item
      className={cn(
        'relative flex min-h-8 cursor-pointer select-none items-center gap-2 rounded-md py-1.5 pl-8 pr-2 text-sm outline-none data-[highlighted]:bg-accent data-[disabled]:opacity-50',
        className,
      )}
      {...props}
    >
      <S.ItemIndicator className="absolute left-2 inline-flex">
        <Check className="size-4" />
      </S.ItemIndicator>
      <S.ItemText>{children}</S.ItemText>
      {aside && <span className="ml-auto">{aside}</span>}
    </S.Item>
  )
}

export function SelectSeparator() {
  return <S.Separator className="-mx-1 my-1 h-px bg-border" />
}
