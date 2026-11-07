import { useQueryClient } from '@tanstack/react-query'
import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from 'react'
import { Navigate, useLocation, useNavigate } from 'react-router'
import { api, setToken, setUnauthorizedHandler } from './api'

// Sign-in goes through the auth service's /v1/auth/login and /v1/auth/register: the app client is
// confidential, so the browser can't talk to Cognito itself. Both return a Cognito ID token, kept in memory and mirrored to sessionStorage so a refresh keeps the session.

interface Session {
  email: string
}

interface AuthContextValue {
  session: Session | null
  login: (email: string, password: string) => Promise<void>
  register: (email: string, password: string) => Promise<void>
  logout: () => void
}

const AuthContext = createContext<AuthContextValue | null>(null)

// The token and email are mirrored to sessionStorage so a page refresh keeps the user signed in; the
// entry is dropped on logout, on a 401, once the token's `exp` passes, and when the tab closes.
const STORAGE_KEY = 'a3-session'

function tokenExpired(token: string): boolean {
  try {
    const payload = JSON.parse(atob(token.split('.')[1].replace(/-/g, '+').replace(/_/g, '/')))
    return typeof payload.exp !== 'number' || payload.exp * 1000 <= Date.now()
  } catch {
    return true
  }
}

function restoreSession(): Session | null {
  try {
    const raw = sessionStorage.getItem(STORAGE_KEY)
    if (!raw) return null
    const saved = JSON.parse(raw) as { token?: string; email?: string }
    if (!saved.token || !saved.email || tokenExpired(saved.token)) {
      sessionStorage.removeItem(STORAGE_KEY)
      return null
    }
    setToken(saved.token)
    return { email: saved.email }
  } catch {
    return null
  }
}

export function AuthProvider({ children }: { children: ReactNode }) {
  const [session, setSession] = useState<Session | null>(restoreSession)
  const queryClient = useQueryClient()
  const navigate = useNavigate()
  const location = useLocation()

  const start = useCallback((token: string, email: string) => {
    setToken(token)
    sessionStorage.setItem(STORAGE_KEY, JSON.stringify({ token, email }))
    setSession({ email })
  }, [])

  const logout = useCallback(() => {
    setToken(null)
    sessionStorage.removeItem(STORAGE_KEY)
    setSession(null)
    queryClient.clear() // nothing of one user's data survives into the next session
  }, [queryClient])

  // A 401 from any /a3 call (expired or rejected token) ends the session and returns to login.
  useEffect(() => {
    setUnauthorizedHandler(() => {
      logout()
      navigate('/login', { replace: true, state: { from: location.pathname, expired: true } })
    })
    return () => setUnauthorizedHandler(null)
  }, [logout, navigate, location.pathname])

  const value = useMemo<AuthContextValue>(
    () => ({
      session,
      login: async (email, password) => {
        const result = await api.login(email, password)
        start(result.token, result.user.email ?? email)
      },
      register: async (email, password) => {
        const result = await api.register(email, password)
        start(result.token, result.user.email ?? email)
      },
      logout,
    }),
    [session, start, logout],
  )

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
}

export function useAuth() {
  const value = useContext(AuthContext)
  if (!value) throw new Error('useAuth outside AuthProvider')
  return value
}

/** Route guard: no session → login, remembering where the user was going. */
export function RequireAuth({ children }: { children: ReactNode }) {
  const { session } = useAuth()
  const location = useLocation()
  if (!session) return <Navigate to="/login" replace state={{ from: location.pathname }} />
  return children
}

/** The reverse, for the login and sign-up pages. */
export function RedirectIfAuthed({ children }: { children: ReactNode }) {
  const { session } = useAuth()
  if (session) return <Navigate to="/" replace />
  return children
}
