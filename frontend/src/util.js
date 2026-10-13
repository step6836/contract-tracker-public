import { useEffect, useState } from 'react'

export const fmt = (iso) => {
  if (!iso) return 'Not set'
  const [y, m, d] = iso.slice(0, 10).split('-').map(Number)
  return new Date(y, m - 1, d).toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' })
}

// "2026-10-15T14:00" -> "Oct 15, 2026 at 2:00 PM"
export const fmtAt = (iso) => {
  const [h, m] = iso.slice(11, 16).split(':').map(Number)
  return `${fmt(iso)} at ${h % 12 || 12}:${String(m).padStart(2, '0')} ${h < 12 ? 'AM' : 'PM'}`
}

export const todayISO = () => {
  const n = new Date()
  return `${n.getFullYear()}-${String(n.getMonth() + 1).padStart(2, '0')}-${String(n.getDate()).padStart(2, '0')}`
}

export const days = (n) => `${n} day${n === 1 ? '' : 's'}`

export const go = (path) => { window.location.hash = path }

export function useRoute() {
  const read = () => window.location.hash.slice(1) || '/'
  const [route, setRoute] = useState(read())
  useEffect(() => {
    const f = () => setRoute(read())
    window.addEventListener('hashchange', f)
    return () => window.removeEventListener('hashchange', f)
  }, [])
  return route
}

export const STATUS_LABEL = {
  active: 'Active', completed: 'Completed', not_approved: 'Closed: not approved', withdrawn: 'Closed: withdrawn',
}
