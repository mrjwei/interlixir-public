import { useMutation, useQueryClient } from '@tanstack/react-query'
import { useState, type ReactNode } from 'react'
import { useNavigate } from 'react-router'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogFooter } from '@/components/ui/dialog'
import { FieldError, Input, Label } from '@/components/ui/input'
import { useToast } from '@/components/ui/toast'
import { api } from '@/lib/api'
import { keys } from '@/lib/queries'

export function NewProjectDialog({ open, onOpenChange }: { open: boolean; onOpenChange: (open: boolean) => void }) {
  const [name, setName] = useState('')
  const queryClient = useQueryClient()
  const navigate = useNavigate()
  const toast = useToast()
  const create = useMutation({
    mutationFn: (value: string) => api.createProject(value),
    onSuccess: (project) => {
      void queryClient.invalidateQueries({ queryKey: keys.projects })
      onOpenChange(false)
      setName('')
      toast({ tone: 'success', title: 'Project created', description: project.name })
      navigate(`/projects/${project.projectId}`)
    },
  })

  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        onOpenChange(next)
        if (!next) create.reset()
      }}
    >
      <DialogContent title="New project" description="Projects hold your recordings and the documents made from them.">
        <form
          className="flex flex-col gap-4"
          onSubmit={(e) => {
            e.preventDefault()
            if (name.trim()) create.mutate(name.trim())
          }}
        >
          <div className="flex flex-col gap-2">
            <Label htmlFor="project-name">Name</Label>
            <Input
              id="project-name"
              autoFocus
              maxLength={100}
              placeholder="e.g. Checkout redesign"
              value={name}
              onChange={(e) => setName(e.target.value)}
              aria-invalid={create.isError || undefined}
            />
            <FieldError>{create.error?.message}</FieldError>
          </div>
          <DialogFooter>
            <Button type="button" variant="outline" onClick={() => onOpenChange(false)}>
              Cancel
            </Button>
            <Button type="submit" disabled={!name.trim() || create.isPending}>
              {create.isPending ? 'Creating…' : 'Create project'}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}

export function NewProjectButton({ children }: { children: (open: () => void) => ReactNode }) {
  const [open, setOpen] = useState(false)
  return (
    <>
      {children(() => setOpen(true))}
      <NewProjectDialog open={open} onOpenChange={setOpen} />
    </>
  )
}
