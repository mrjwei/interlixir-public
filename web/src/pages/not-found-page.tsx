import { Link } from 'react-router'
import { Page } from '@/components/app/shell'
import { Button } from '@/components/ui/button'
import { Card } from '@/components/ui/card'

export function NotFound({ message = "This page doesn't exist, or it isn't yours." }: { message?: string }) {
  return (
    <Card className="mx-auto mt-4 flex max-w-[480px] flex-col items-center gap-3 px-6 py-10 text-center md:mt-16">
      <h1 className="text-xl font-semibold tracking-[-0.02em]">Not found</h1>
      <p className="text-muted-foreground">{message}</p>
      <Button asChild variant="outline" className="mt-2">
        <Link to="/">Go to projects</Link>
      </Button>
    </Card>
  )
}

export function NotFoundPage() {
  return (
    <Page crumbs={[{ label: 'Not found' }]}>
      <NotFound />
    </Page>
  )
}
