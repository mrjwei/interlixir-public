// Numbers are concrete and compact (DESIGN.md, Voice): "41 MB", "2h ago", "$0.04".

export function formatBytes(bytes: number) {
  const mb = bytes / (1024 * 1024)
  if (mb >= 10) return `${Math.round(mb)} MB`
  if (mb >= 0.1) return `${mb.toFixed(1)} MB`
  return `${Math.max(1, Math.round(bytes / 1024))} KB`
}

export function relativeTime(iso: string | null | undefined, now = Date.now()) {
  if (!iso) return '—'
  const then = Date.parse(iso)
  if (Number.isNaN(then)) return '—'
  const s = Math.max(0, Math.round((now - then) / 1000))
  if (s < 60) return 'Just now'
  const m = Math.round(s / 60)
  if (m < 60) return `${m}m ago`
  const h = Math.round(m / 60)
  if (h < 24) return `${h}h ago`
  const d = Math.round(h / 24)
  if (d === 1) return 'Yesterday'
  if (d < 7) return `${d}d ago`
  return formatDate(iso)
}

export function formatDate(iso: string | null | undefined) {
  if (!iso) return '—'
  return new Date(iso).toLocaleDateString('en-AU', { day: 'numeric', month: 'short', year: 'numeric' })
}

export function formatDateTime(iso: string | null | undefined) {
  if (!iso) return '—'
  const date = new Date(iso)
  const time = date.toLocaleTimeString('en-AU', { hour: 'numeric', minute: '2-digit' })
  const today = new Date()
  if (date.toDateString() === today.toDateString()) return `Today, ${time}`
  return `${formatDate(iso)}, ${time}`
}

export function formatDuration(seconds: number | null | undefined) {
  if (!seconds) return null
  const minutes = Math.round(seconds / 60)
  if (minutes < 1) return `${Math.round(seconds)} s`
  if (minutes < 60) return `${minutes} min`
  return `${Math.floor(minutes / 60)} h ${minutes % 60} min`
}

export function formatTokens(count: number) {
  return count.toLocaleString('en-AU')
}

export function formatCost(usd: number) {
  return usd < 0.01 && usd > 0 ? '<$0.01' : `$${usd.toFixed(2)}`
}

/** "p3-checkout.m4a" → "p3-checkout": a recording's label until its document has a title. */
export function stripExtension(fileName: string | null | undefined) {
  if (!fileName) return 'Untitled recording'
  const dot = fileName.lastIndexOf('.')
  return dot > 0 ? fileName.slice(0, dot) : fileName
}
