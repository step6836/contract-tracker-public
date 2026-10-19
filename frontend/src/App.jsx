import { useCallback, useEffect, useState } from 'react'
import { api } from './api.js'
import { go, useRoute } from './util.js'
import Login from './views/Login.jsx'
import Attention from './views/Attention.jsx'
import Cases from './views/Cases.jsx'
import CaseDetail from './views/CaseDetail.jsx'
import NewCase from './views/NewCase.jsx'

export default function App() {
  const [user, setUser] = useState(undefined) // undefined = still checking
  const [meta, setMeta] = useState(null)
  const [cases, setCases] = useState(null)
  const route = useRoute()

  const refresh = useCallback(() => api.get('/cases').then(setCases).catch(() => {}), [])

  useEffect(() => {
    api.get('/me').then(setUser).catch(() => setUser(null))
    const lost = () => setUser(null)
    window.addEventListener('auth-lost', lost)
    return () => window.removeEventListener('auth-lost', lost)
  }, [])

  useEffect(() => {
    if (!user) return
    api.get('/meta').then(setMeta).catch(() => {})
    refresh()
    // pick up your teammate's changes without a manual reload
    const timer = setInterval(refresh, 60000)
    window.addEventListener('focus', refresh)
    return () => { clearInterval(timer); window.removeEventListener('focus', refresh) }
  }, [user, refresh])

  if (user === undefined) return <div className="boot">Loading…</div>
  if (!user) return <Login onSignedIn={setUser} />

  const attentionCount = (cases || []).filter((c) => c.status === 'active' && c.attention.some((a) => a.level !== 'ready')).length
  const [path, query] = route.split('?')
  const caseMatch = path.match(/^\/cases\/([^/]+)$/)

  let page
  if (path === '/') page = <Attention cases={cases} />
  else if (path === '/cases') page = <Cases cases={cases} />
  else if (path === '/new') page = <NewCase key={route} meta={meta} cases={cases} params={new URLSearchParams(query || '')} onCreated={refresh} />
  else if (caseMatch) page = <CaseDetail key={caseMatch[1]} id={decodeURIComponent(caseMatch[1])} meta={meta} cases={cases} onChange={refresh} />
  else page = <p className="empty">That page doesn't exist. <a href="#/">Back to Needs attention</a></p>

  const nav = (path, label, extra) => (
    <a href={`#${path}`} className={route === path ? 'nav on' : 'nav'}>{label}{extra}</a>
  )

  return (
    <>
      <header className="top">
        <a href="#/" className="brand">Contracts Tracker</a>
        <nav>
          {nav('/', 'Needs attention', attentionCount > 0 ? <span className="count">{attentionCount}</span> : null)}
          {nav('/cases', 'All cases')}
        </nav>
        <button className="btn primary" onClick={() => go('/new')}>New case</button>
        <span className="who">{user.display_name}</span>
        <button className="link" onClick={() => api.post('/logout').finally(() => setUser(null))}>Sign out</button>
      </header>
      <main>{page}</main>
    </>
  )
}
