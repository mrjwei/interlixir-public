import type { ComponentProps } from 'react'
import { cn } from '@/lib/utils'

export function Skeleton({ className, ...props }: ComponentProps<'div'>) {
  return (
    <div
      aria-hidden
      className={cn(
        'rounded-md bg-secondary bg-[linear-gradient(90deg,transparent_0%,color-mix(in_oklab,var(--card)_55%,transparent)_50%,transparent_100%)] bg-[length:200%_100%] animate-[shimmer_1.6s_linear_infinite]',
        className,
      )}
      {...props}
    />
  )
}
