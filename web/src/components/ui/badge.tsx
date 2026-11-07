import { cva, type VariantProps } from 'class-variance-authority'
import type { ComponentProps } from 'react'
import { cn } from '@/lib/utils'

// Status is a tint, not a fill (DESIGN.md): hue at ~14% for the background, full for the text.
const badgeVariants = cva(
  'inline-flex h-[22px] shrink-0 items-center gap-1.5 whitespace-nowrap rounded-full border px-2 text-xs font-medium [&_svg]:size-3',
  {
    variants: {
      variant: {
        solid: 'border-transparent bg-primary text-primary-foreground',
        secondary: 'border-border bg-secondary text-secondary-foreground',
        outline: 'border-border bg-transparent text-foreground',
        success: 'tint-success',
        info: 'tint-info',
        warning: 'tint-warning',
        destructive: 'tint-destructive',
        neutral: 'border-border bg-secondary text-secondary-foreground',
      },
    },
    defaultVariants: { variant: 'secondary' },
  },
)

export type BadgeVariant = NonNullable<VariantProps<typeof badgeVariants>['variant']>

export function Badge({ className, variant, ...props }: ComponentProps<'span'> & VariantProps<typeof badgeVariants>) {
  return <span className={cn(badgeVariants({ variant }), className)} {...props} />
}
