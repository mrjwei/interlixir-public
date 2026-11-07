import { CircleAlert } from 'lucide-react'
import { useState, type FormEvent, type ReactNode } from 'react'
import { Link, useLocation, useNavigate } from 'react-router'
import { Logo } from '@/components/app/logo'
import { Alert } from '@/components/ui/alert'
import { Button } from '@/components/ui/button'
import { Card, CardDescription, CardTitle } from '@/components/ui/card'
import { FieldError, Input, Label } from '@/components/ui/input'
import { ApiError } from '@/lib/api'
import { useAuth } from '@/lib/auth'

function AuthLayout({ children }: { children: ReactNode }) {
  return (
    <main className="flex min-h-dvh flex-col items-center justify-center gap-6 bg-background px-4 py-12">
      <Logo />
      <Card className="w-full max-w-[400px] p-6">{children}</Card>
    </main>
  )
}

function ErrorText({ id, children }: { id: string; children?: string | null }) {
  if (!children) return null
  return (
    <FieldError id={id}>
      <CircleAlert className="size-4 shrink-0" />
      {children}
    </FieldError>
  )
}

/** Where to go after signing in: back to the page that sent the user here, else home. */
function useReturnTo() {
  const state = useLocation().state as { from?: string; expired?: boolean } | null
  return { to: state?.from && state.from !== '/login' ? state.from : '/', expired: !!state?.expired }
}

export function LoginPage() {
  const { login } = useAuth()
  const navigate = useNavigate()
  const { to, expired } = useReturnTo()
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  async function submit(e: FormEvent) {
    e.preventDefault()
    setBusy(true)
    setError(null)
    try {
      await login(email.trim(), password)
      navigate(to, { replace: true })
    } catch (err) {
      setError(err instanceof ApiError && err.status === 401 ? 'Incorrect email or password.' : (err as Error).message)
      setBusy(false)
    }
  }

  return (
    <AuthLayout>
      <form className="flex flex-col gap-5" onSubmit={submit} noValidate>
        <div className="flex flex-col gap-1">
          <CardTitle className="text-xl">Log in</CardTitle>
          <CardDescription>Use the email and password for your account.</CardDescription>
        </div>
        {expired && !error && <Alert tone="info">Your session ended. Log in again to continue.</Alert>}
        <div className="flex flex-col gap-2">
          <Label htmlFor="email">Email</Label>
          <Input
            id="email"
            type="email"
            autoComplete="email"
            required
            value={email}
            onChange={(e) => setEmail(e.target.value)}
          />
        </div>
        <div className="flex flex-col gap-2">
          <Label htmlFor="password">Password</Label>
          <Input
            id="password"
            type="password"
            autoComplete="current-password"
            required
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            aria-invalid={!!error || undefined}
            aria-describedby={error ? 'login-error' : undefined}
          />
          <ErrorText id="login-error">{error}</ErrorText>
        </div>
        <Button type="submit" disabled={busy || !email || !password}>
          {busy ? 'Logging in…' : 'Log in'}
        </Button>
        <p className="text-center text-sm text-muted-foreground">
          Don't have an account?{' '}
          <Link to="/signup" className="font-medium text-foreground underline underline-offset-4 hover:opacity-80">
            Sign up
          </Link>
        </p>
      </form>
    </AuthLayout>
  )
}

// The user pool's policy (describe-user-pool, 2026-10-15): 8+ characters, upper, lower, number, symbol.
// Checked here so the user sees it inline; Cognito still enforces it.
function passwordProblem(password: string) {
  if (password.length < 8) return 'Use at least 8 characters.'
  if (!/[A-Z]/.test(password)) return 'Add an uppercase letter.'
  if (!/[a-z]/.test(password)) return 'Add a lowercase letter.'
  if (!/[0-9]/.test(password)) return 'Add a number.'
  if (!/[^A-Za-z0-9]/.test(password)) return 'Add a symbol.'
  return null
}

/** the auth service passes Cognito's exception text through ("An error occurred (X) when calling Y: message"). */
function cleanCognitoMessage(message: string) {
  return message.replace(/^An error occurred \(\w+\) when calling the \w+ operation: /, '')
}

export function SignupPage() {
  const { register } = useAuth()
  const navigate = useNavigate()
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [confirm, setConfirm] = useState('')
  const [touched, setTouched] = useState({ password: false, confirm: false })
  const [error, setError] = useState<{ field: 'email' | 'password' | 'form'; message: string } | null>(null)
  const [busy, setBusy] = useState(false)

  const pwProblem = touched.password && password ? passwordProblem(password) : null
  const mismatch = touched.confirm && confirm && confirm !== password ? "Passwords don't match." : null

  async function submit(e: FormEvent) {
    e.preventDefault()
    setTouched({ password: true, confirm: true })
    if (passwordProblem(password) || password !== confirm) return
    setBusy(true)
    setError(null)
    try {
      await register(email.trim(), password)
      navigate('/', { replace: true })
    } catch (err) {
      const message = cleanCognitoMessage((err as Error).message)
      if (err instanceof ApiError && err.status === 409) {
        setError({ field: 'email', message: 'An account with this email already exists.' })
      } else if (err instanceof ApiError && err.status === 400) {
        setError({ field: /email/i.test(message) ? 'email' : 'password', message })
      } else {
        setError({ field: 'form', message })
      }
      setBusy(false)
    }
  }

  const emailError = error?.field === 'email' ? error.message : null
  const passwordError = pwProblem ?? (error?.field === 'password' ? error.message : null)

  return (
    <AuthLayout>
      <form className="flex flex-col gap-5" onSubmit={submit} noValidate>
        <div className="flex flex-col gap-1">
          <CardTitle className="text-xl">Create your account</CardTitle>
          <CardDescription>You can start uploading straight after.</CardDescription>
        </div>
        {error?.field === 'form' && <Alert tone="destructive">{error.message}</Alert>}
        <div className="flex flex-col gap-2">
          <Label htmlFor="email">Email</Label>
          <Input
            id="email"
            type="email"
            autoComplete="email"
            required
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            aria-invalid={!!emailError || undefined}
            aria-describedby={emailError ? 'email-error' : undefined}
          />
          <ErrorText id="email-error">{emailError}</ErrorText>
        </div>
        <div className="flex flex-col gap-2">
          <Label htmlFor="password">Password</Label>
          <Input
            id="password"
            type="password"
            autoComplete="new-password"
            required
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            onBlur={() => setTouched((t) => ({ ...t, password: true }))}
            aria-invalid={!!passwordError || undefined}
            aria-describedby="password-hint"
          />
          {passwordError ? (
            <ErrorText id="password-hint">{passwordError}</ErrorText>
          ) : (
            <p id="password-hint" className="text-xs text-muted-foreground">
              At least 8 characters, with an uppercase letter, a lowercase letter, a number and a symbol.
            </p>
          )}
        </div>
        <div className="flex flex-col gap-2">
          <Label htmlFor="confirm">Confirm password</Label>
          <Input
            id="confirm"
            type="password"
            autoComplete="new-password"
            required
            value={confirm}
            onChange={(e) => setConfirm(e.target.value)}
            onBlur={() => setTouched((t) => ({ ...t, confirm: true }))}
            aria-invalid={!!mismatch || undefined}
            aria-describedby={mismatch ? 'confirm-error' : undefined}
          />
          <ErrorText id="confirm-error">{mismatch}</ErrorText>
        </div>
        <Button type="submit" disabled={busy || !email || !password || !confirm}>
          {busy ? 'Creating account…' : 'Create account'}
        </Button>
        <p className="text-center text-sm text-muted-foreground">
          Already have an account?{' '}
          <Link to="/login" className="font-medium text-foreground underline underline-offset-4 hover:opacity-80">
            Log in
          </Link>
        </p>
      </form>
    </AuthLayout>
  )
}
