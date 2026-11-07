import { useCallback, useSyncExternalStore } from 'react'

// Light and dark from one token contract (DESIGN.md): `.dark` on <html>. The choice is a per-viewer
// convenience, so localStorage is fine; it can throw (private mode, blocked storage), hence the try/catch.

type Theme = 'light' | 'dark'
const KEY = 'interlixir-theme'
const listeners = new Set<() => void>()

function stored(): Theme | null {
  try {
    const value = localStorage.getItem(KEY)
    return value === 'light' || value === 'dark' ? value : null
  } catch {
    return null
  }
}

function current(): Theme {
  return document.documentElement.classList.contains('dark') ? 'dark' : 'light'
}

export function applyInitialTheme() {
  const theme = stored() ?? (window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light')
  document.documentElement.classList.toggle('dark', theme === 'dark')
}

export function useTheme() {
  const theme = useSyncExternalStore(
    (listener) => {
      listeners.add(listener)
      return () => listeners.delete(listener)
    },
    current,
  )
  const toggle = useCallback(() => {
    const next: Theme = current() === 'dark' ? 'light' : 'dark'
    document.documentElement.classList.toggle('dark', next === 'dark')
    try {
      localStorage.setItem(KEY, next)
    } catch {
      // not remembered; still applied for this page
    }
    listeners.forEach((l) => l())
  }, [])
  return { theme, toggle }
}
