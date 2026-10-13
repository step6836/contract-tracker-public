import { useMemo, useState } from 'react'
import { api } from '../api.js'
import { fmt, go, STATUS_LABEL } from '../util.js'

const TYPES = [['all', 'All types'], ['renewal', 'Renewals'], ['transfer', 'Transfers'], ['expansion', 'Expansions']]

export default function Cases({ cases }) {
  const [type, setType] = useState('all')
  const [status, setStatus] = useState('active')
  const [q, setQ] = useState('')
  const [publishing, setPublishing] = useState(false)
  const [published, setPublished] = useState(null)

  const publishNow = () => {
    setPublishing(true)
    api.post('/publish')
      .then(setPublished)
      .catch((e) => setPublished({ ok: false, detail: e.message }))
      .finally(() => setPublishing(false))
  }

  const rows = useMemo(() => {
    const needle = q.trim().toLowerCase()
    return (cases || [])
      .filter((c) => (type === 'all' || c.type === type))
      .filter((c) => (status === 'all' || (status === 'closed' ? ['not_approved', 'withdrawn'].includes(c.status)
        : status === 'hold' ? !!c.hold : c.status === status)))
      .filter((c) => !needle || [c.id, c.store_name, c.store_number, c.franchisee, c.transferee].join(' ').toLowerCase().includes(needle))
      .sort((a, b) => (a.target_signing_date || '9999').localeCompare(b.target_signing_date || '9999') || a.start_date.localeCompare(b.start_date))
  }, [cases, type, status, q])

  if (!cases) return <p className="muted">Loading…</p>

  return (
    <>
      <div className="page-head row">
        <h1>All cases</h1>
        <span className="spacer" />
        {published && <span className={published.ok ? 'small muted' : 'small stale-text'}>{published.detail}</span>}
        <button className="btn" onClick={publishNow} disabled={publishing} title="Refresh the Excel report for execs">
          {publishing ? 'Publishing…' : 'Publish now'}
        </button>
      </div>
      <div className="filters">
        <div className="seg" role="group" aria-label="Case type">
          {TYPES.map(([k, l]) => (
            <button key={k} className={type === k ? 'on' : ''} onClick={() => setType(k)}>{l}</button>
          ))}
        </div>
        <select value={status} onChange={(e) => setStatus(e.target.value)} aria-label="Status">
          <option value="active">Active</option>
          <option value="hold">On hold</option>
          <option value="completed">Completed</option>
          <option value="closed">Closed early</option>
          <option value="all">All statuses</option>
        </select>
        <input className="search" placeholder="Search store, name or case ID" value={q} onChange={(e) => setQ(e.target.value)} />
      </div>

      {rows.length === 0 ? (
        <p className="empty">No cases match. {cases.length === 0 && <>Use <strong>New case</strong> to add one.</>}</p>
      ) : (
        <div className="table-wrap">
          <table>
            <thead>
              <tr><th>Case</th><th>Store</th><th>Current step</th><th>In step</th><th>Responsible</th><th>Progress</th><th>Target signing</th></tr>
            </thead>
            <tbody>
              {rows.map((c) => (
                <tr key={c.id} onClick={() => go(`/cases/${c.id}`)} tabIndex={0} onKeyDown={(e) => e.key === 'Enter' && go(`/cases/${c.id}`)}>
                  <td><span className="case-id">{c.id}</span><div className="muted small">{c.type_label}</div></td>
                  <td>{c.store_name}<div className="muted small">{[c.franchisee, c.transferee && `to ${c.transferee}`].filter(Boolean).join(' ')}</div></td>
                  <td>
                    {c.current ? c.current.label : (c.lease_pending && c.status === 'active' ? 'Waiting on lease' : STATUS_LABEL[c.status])}
                    {c.current?.status_note && <div className="muted small">{c.current.status_note}</div>}
                    {c.hold && <div className="small hold-text">On hold: {c.hold.reason}</div>}
                  </td>
                  <td className={c.current?.is_stale ? 'stale-text' : ''}>{c.current ? `${c.current.days_in_step}d` : ''}</td>
                  <td>{c.current?.owner}</td>
                  <td>
                    <div className="bar" title={`${c.progress.done} of ${c.progress.total} steps`}><i style={{ width: `${c.progress.percent}%` }} /></div>
                    <span className="small muted">{c.progress.done}/{c.progress.total}</span>
                  </td>
                  <td>{c.target_signing_date ? fmt(c.target_signing_date) : <span className="muted">Not set</span>}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </>
  )
}
