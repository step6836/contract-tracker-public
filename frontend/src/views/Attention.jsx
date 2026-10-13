import { go, days, fmt } from '../util.js'

const RANK = { alert: 3, warn: 2, ready: 1 }
const top = (c) => Math.max(0, ...c.attention.map((a) => RANK[a.level]))

export default function Attention({ cases }) {
  if (!cases) return <p className="muted">Loading…</p>
  const active = cases.filter((c) => c.status === 'active')
  const held = active.filter((c) => c.hold)
  // On-hold cases are waiting on someone else, so they get their own list instead of a nudge.
  const flagged = active
    .filter((c) => !c.hold && c.attention.length)
    .sort((a, b) => top(b) - top(a) || (b.current?.days_in_step || 0) - (a.current?.days_in_step || 0))
  const stuck = flagged.filter((c) => top(c) >= 2)

  return (
    <>
      <div className="page-head">
        <h1>Needs attention</h1>
        <p className="muted">
          {active.length === 0
            ? 'No active cases yet.'
            : `${active.length} active ${active.length === 1 ? 'case' : 'cases'}, ${stuck.length} ${stuck.length === 1 ? 'needs' : 'need'} a nudge.`}
        </p>
      </div>

      {active.length === 0 && (
        <p className="empty">Start your first case with <strong>New case</strong> and it will show up here when it stalls.</p>
      )}
      {active.length > 0 && flagged.length === 0 && held.length === 0 && (
        <p className="empty">Nothing is stuck. Every active case is moving.</p>
      )}

      <ul className="att-list">
        {flagged.map((c) => (
          <li key={c.id}>
            <button className={`att-row lvl-${['', 'ready', 'warn', 'alert'][top(c)]}`} onClick={() => go(`/cases/${c.id}`)}>
              <div className="att-main">
                <div className="att-title">
                  <span className="case-id">{c.id}</span>
                  <strong>{c.store_name}</strong>
                  <span className="pill type">{c.type_label}</span>
                </div>
                {c.current && (
                  <div className="muted">
                    At “{c.current.label}” with {c.current.owner}, {days(c.current.days_in_step)} in
                    {c.current.status_note && <>. {c.current.status_note}</>}
                  </div>
                )}
              </div>
              <ul className="reasons">
                {c.attention.map((a, i) => (
                  <li key={i} className={`reason ${a.level}`}>{a.text}</li>
                ))}
              </ul>
            </button>
          </li>
        ))}
      </ul>

      {held.length > 0 && (
        <>
          <h2 className="att-group">On hold ({held.length})</h2>
          <ul className="att-list">
            {held.map((c) => (
              <li key={c.id}>
                <button className="att-row lvl-hold" onClick={() => go(`/cases/${c.id}`)}>
                  <div className="att-main">
                    <div className="att-title">
                      <span className="case-id">{c.id}</span>
                      <strong>{c.store_name}</strong>
                      <span className="pill type">{c.type_label}</span>
                    </div>
                    <div className="muted">On hold at “{c.hold.step_label}” since {fmt(c.hold.start)}, {days(c.hold.days)}</div>
                  </div>
                  <ul className="reasons">
                    <li className="reason hold">{c.hold.reason}</li>
                    {c.attention.map((a, i) => <li key={i} className={`reason ${a.level}`}>{a.text}</li>)}
                  </ul>
                </button>
              </li>
            ))}
          </ul>
        </>
      )}
    </>
  )
}
