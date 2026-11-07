import { useState } from 'react'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogFooter } from '@/components/ui/dialog'
import { FieldError, Input, Label } from '@/components/ui/input'

// The rename and delete-confirmation dialogs (design frames 07d, 07f), for documents and projects alike.

export function RenameDialog({
  open,
  onOpenChange,
  title,
  ...form
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  title: string
} & Omit<RenameFormProps, 'onCancel'>) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent title={title}>
        {/* Unmounted while closed, so each opening starts from the current name. */}
        <RenameForm {...form} onCancel={() => onOpenChange(false)} />
      </DialogContent>
    </Dialog>
  )
}

interface RenameFormProps {
  initial: string
  maxLength: number
  pending: boolean
  error?: string
  onSubmit: (name: string) => void
  onCancel: () => void
}

function RenameForm({ initial, maxLength, pending, error, onSubmit, onCancel }: RenameFormProps) {
  const [name, setName] = useState(initial)
  return (
    <form
      className="flex flex-col gap-4"
      onSubmit={(e) => {
        e.preventDefault()
        const value = name.trim()
        if (value && value !== initial) onSubmit(value)
        else if (value) onCancel()
      }}
    >
      <div className="flex flex-col gap-2">
        <Label htmlFor="rename-input">Name</Label>
        <Input
          id="rename-input"
          autoFocus
          onFocus={(e) => e.currentTarget.select()}
          maxLength={maxLength}
          value={name}
          onChange={(e) => setName(e.target.value)}
          aria-invalid={error ? true : undefined}
        />
        <FieldError>{error}</FieldError>
      </div>
      <DialogFooter>
        <Button type="button" variant="outline" onClick={onCancel}>
          Cancel
        </Button>
        <Button type="submit" disabled={!name.trim() || pending}>
          {pending ? 'Renaming…' : 'Rename'}
        </Button>
      </DialogFooter>
    </form>
  )
}

export function ConfirmDeleteDialog({
  open,
  onOpenChange,
  title,
  description,
  confirmLabel,
  pending,
  error,
  onConfirm,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  title: string
  description: string
  confirmLabel: string
  pending: boolean
  error?: string
  onConfirm: () => void
}) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent title={title} description={description}>
        <FieldError>{error}</FieldError>
        <DialogFooter>
          <Button type="button" variant="outline" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button type="button" variant="destructive" disabled={pending} onClick={onConfirm}>
            {pending ? 'Deleting…' : confirmLabel}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
