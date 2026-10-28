import { Fragment, useCallback, useEffect, useState } from 'react'
import { api } from '../api.js'
import { applies, days, fmt, fmtAt, go, STATUS_LABEL, todayISO } from '../util.js'
import { FlagField, LinkPicker, UnitList } from './NewCase.jsx'

export default function CaseDetail({ id, meta, cases, onChange }) {
  const [c, setC] = useState(null)
  const [error, setError] = useState('')
  const [notFound, setNotFound] = useState(false)
  const [editing, setEditing] = useState(false)
  const [closing, setClosing] = useState(null)
  const [holding, setHolding] = useState(false)
  const [events, setEvents] = useState(null)

  useEffect(() => {
    api.get(`/cases/${id}`).then(setC).catch((e) => (/not found/i.test(e.message) ? setNotFound(true) : setError(e.message)))
  }, [id])

  // Run an action, show the returned case, refresh the list, surface any error.
  const act = useCallback(async (fn) => {
    setError('')
    try {
      const next = await fn()
      setC(next); onChange(); setEvents(null)
      return true
    } catch (e) { setError(e.message); return false }
  }, [onChange])

  const loadEvents = () => api.get(`/events?case_id=${encodeURIComponent(id)}`).then(setEvents).catch(() => {})

  if (notFound) return <p className="empty">No case with the ID {id}. <a href="#/cases">See all cases</a></p>
  if (!c) return <p className="muted">{error || 'Loading…'}</p>

  const active = c.status === 'active'
  const post = (path, body) => act(() => api.post(`/cases/${c.id}${path}`, body))

  return (
    <div className="detail">
      <p className="crumbs"><a href="#/cases">All cases</a></p>

      <div className="case-head">
        <div className="row">
          <span className="case-id">{c.id}</span>
          <span className="pill type">{c.type_label}</span>
          <span className={`pill status-${c.status}`}>{STATUS_LABEL[c.status]}</span>
          {c.hold && <span className="pill status-hold">On hold</span>}
        </div>
        <h1>{c.store_name} <span className="muted store-no">#{c.store_number}</span></h1>
        {(c.franchisee || c.transferee) && (
          <p className="muted">
            {c.type === 'transfer'
              ? `Transferor ${c.franchisee || 'not set'}, transferee ${c.transferee || 'not set'}`
              : `Franchisee ${c.franchisee}`}
          </p>
        )}
        {c.closed_reason && <p className="muted">Reason: {c.closed_reason}</p>}
        <div className="row head-actions">
          <button className="btn" onClick={() => setEditing(!editing)}>{editing ? 'Close editor' : 'Edit details'}</button>
          {active && !closing && !holding && (
            <>
              {!c.hold && <button className="btn" onClick={() => setHolding(true)}>Put on hold</button>}
              <button className="btn" onClick={() => setClosing('not_approved')}>Close as not approved</button>
              <button className="btn" onClick={() => setClosing('withdrawn')}>Close as withdrawn</button>
            </>
          )}
          {(c.status === 'not_approved' || c.status === 'withdrawn') && <button className="btn" onClick={() => post('/reopen')}>Reopen case</button>}
        </div>
        {closing && <CloseForm outcome={closing} onCancel={() => setClosing(null)}
          onConfirm={async (reason) => { if (await post('/close', { outcome: closing, reason })) setClosing(null) }} />}
        {holding && <HoldForm onCancel={() => setHolding(false)}
          onConfirm={async (reason, on) => { if (await post('/hold', { reason, on })) setHolding(false) }} />}
        {c.hold && <HoldBanner hold={c.hold} active={active} onResume={(on) => post('/resume', { on })} />}
      </div>

      {error && <p className="error banner" role="alert">{error}</p>}

      {editing && <Editor c={c} onSave={async (fields) => { if (await act(() => api.patch(`/cases/${c.id}`, fields))) setEditing(false) }} />}

      {c.attention.length > 0 && (
        <ul className="callouts">
          {c.attention.map((a, i) => <li key={i} className={`reason ${a.level}`}>{a.text}</li>)}
        </ul>
      )}

      <dl className="facts">
        <div><dt>Started</dt><dd>{fmt(c.start_date)}</dd></div>
        {c.flag_defs.filter((fl) => fl.choice).map((fl) => <div key={fl.key}><dt>{fl.group_label}</dt><dd>{c.flags[fl.key] ? fl.label : fl.off_label}</dd></div>)}
        {c.date_defs.filter((d) => applies(d.only_when, c.flags)).map((d) => <div key={d.key}><dt>{d.label}</dt><dd>{fmt(c.dates[d.key])}</dd></div>)}
        {c.has_signing && <div><dt>Target signing</dt><dd>{fmt(c.target_signing_date)}</dd></div>}
        {c.has_signing && <div><dt>Signed</dt><dd>{c.signing_date ? fmt(c.signing_date) : 'Not yet'}</dd></div>}
        {c.type === 'transfer' && <div><dt>Transferee becomes franchisee</dt><dd>{c.effective_date ? fmt(c.effective_date) : '1 month after signing'}</dd></div>}
        {c.lease_deadline && <div><dt>Lease deadline</dt><dd className={c.lease_deadline.days_left < 0 ? 'stale-text' : ''}>
          {fmt(c.lease_deadline.date)}{c.lease_deadline.source === 'auto' && <span className="muted small"> (6 months after signing)</span>}</dd></div>}
        {c.type === 'expansion' && <div><dt>Related units</dt><dd>{c.units.length ? c.units.map((u, i) => (
          <div key={i}>{[u.store_number && `#${u.store_number}`, u.store_name].filter(Boolean).join(' ')}</div>)) : 'None'}</dd></div>}
        {c.type === 'transfer' && c.flags.transferee_is_existing && <div><dt>Related units</dt><dd>{c.related_units || 'None'}</dd></div>}
        {c.box_url && <div><dt>Box folder</dt><dd><a href={c.box_url} target="_blank" rel="noreferrer">Open</a></dd></div>}
      </dl>

      <div className="cols">
        <section>
          {c.side_track?.alongside && <SideTrack c={c} active={active} post={post} />}
          <div className="section-head">
            <h2>Progress</h2>
            <span className="muted">{c.progress.done} of {c.progress.total} steps</span>
          </div>
          <div className="bar wide"><i style={{ width: `${c.progress.percent}%` }} /></div>
          <Steps c={c} track="main" active={active} post={post} />
          {active && c.steps.some((s, i) => i > 0 && (s.status === 'done' || (s.status === 'skipped' && !s.auto))) && (
            <button className="link undo" onClick={() => window.confirm('Undo the most recent step?') && post('/undo', { track: 'main' })}>Undo last step</button>
          )}
          {c.status === 'completed' && (
            <p className="done-note">All steps are done. <button className="link" onClick={() => post('/undo', { track: 'main' })}>Undo last step</button></p>
          )}

          {c.side_track && !c.side_track.alongside && <SideTrack c={c} active={active} post={post} />}
          {c.template_notes.map((n, i) => <p key={i} className="muted small tnote">{n}</p>)}
        </section>

        <aside>
          <Related c={c} meta={meta} cases={cases} post={post} />
          <Notes c={c} post={post} />
          <details className="activity" onToggle={(e) => e.target.open && !events && loadEvents()}>
            <summary>Activity</summary>
            {!events ? <p className="muted small">Loading…</p> : events.length === 0 ? <p className="muted small">Nothing yet.</p> : (
              <ul>
                {events.map((e, i) => (
                  <li key={i}><span className="muted small">{fmt(e.ts)} {e.ts.slice(11, 16)}</span><br />{e.display_name} {e.detail?.text || e.action}</li>
                ))}
              </ul>
            )}
          </details>
        </aside>
      </div>
    </div>
  )
}

// The side track: the Expansion lease (runs on its own), or the Renewal inspection (runs alongside the
// main steps once the evaluation is done, so it sits above them and has a day count).
function SideTrack({ c, active, post }) {
  const t = c.side_track
  return (
    <div className={t.alongside ? 'side alongside' : 'side'}>
      <div className="section-head">
        <h2>{t.label}</h2>
        {t.note && <span className="muted">{t.note}</span>}
      </div>
      {!t.started && <p className="meta">{t.starts_note}</p>}
      <Steps c={c} track="side" timed={t.alongside} active={active} post={post} steps={t.steps} />
      {active && t.steps.some((s) => s.status === 'done') && (
        <button className="link undo" onClick={() => window.confirm(`Undo the most recent ${t.label} step?`) && post('/undo', { track: 'side' })}>
          Undo last {t.label} step</button>
      )}
    </div>
  )
}

function Steps({ c, track, active, post, steps, timed = track === 'main' }) {
  const list = (steps || c.steps).filter((s) => !s.hidden)
  return (
    <ol className="rail">
      {list.map((s) => <StepRow key={s.id} s={s} track={track} timed={timed} active={active} post={post} onHold={!!c.hold} />)}
    </ol>
  )
}

function StepRow({ s, track, timed, active, post, onHold }) {
  const [date, setDate] = useState(todayISO())
  const [skipping, setSkipping] = useState(false)
  const [reason, setReason] = useState('')
  const [editingDate, setEditingDate] = useState(false)
  const [newDate, setNewDate] = useState(s.date || '')
  const base = `/steps/${s.id}`
  // Checklist steps finish themselves: boxes are checked in order (per group), each on a date,
  // and the step completes once every box is checked.
  const hasList = s.checklist.length > 0
  const sections = checklistSections(s)
  const correcting = !!s.correction   // sent back here from a later step to fix documents

  const cls = `step ${s.status} ${s.is_current ? 'current' : ''} ${s.is_current && s.is_stale ? 'stale' : ''} ${s.indent ? 'indent' : ''}`

  if (s.is_current) {
    // e.g. FAI correction: an alternative to the next box (Draft received), so it sits just above it.
    const correctionRow = active && s.can_correct && (
      <div className="actions">
        <label className="check">
          <input type="checkbox" checked={false} disabled={!date}
            onChange={() => post(`${base}/correction`, { on: date, track })} />
          {s.correction_label}
        </label>
        <label className="inline">on
          <input type="date" value={date} max={todayISO()} onChange={(e) => setDate(e.target.value)} /></label>
        <span className="muted small">Problem with the documents? This sends the case back.</span>
      </div>
    )
    return (
      <li className={cls}>
        <span className="marker" aria-hidden />
        <div className="body">
          <div className="label">{s.label}</div>
          <div className="meta">Responsible: {s.owner}</div>
          {s.hints.map((h, i) => <div key={i} className="meta">{h}</div>)}
          {/* a booked step (e.g. the signing) has no clock until its time comes */}
          {timed && !(s.schedule?.question && s.schedule.at && !s.schedule.passed) && (
            <div className={`daycount ${s.is_stale ? 'stale-text' : ''}`}>
              <b>{s.days_in_step === 0 ? 'Started today' : days(s.days_in_step)}</b>{s.days_in_step === 0 ? '' : ' in this step'}
              {s.is_stale && <span className="muted small"> (flagged after {s.stale_after})</span>}
              {s.days_on_hold > 0 && <span className="muted small"> (not counting {days(s.days_on_hold)} on hold{onHold ? ', paused now' : ''})</span>}
            </div>
          )}
          <Holds s={s} />
          {s.reminders && <Reminders rem={s.reminders} active={active}
            onSet={(key, done, on) => post(`${base}/reminder`, { key, done, on })} />}
          {s.started_by && <div className="meta"><b>{s.started_by.label} {fmt(s.started_by.date)}</b></div>}
          {s.review_ends && (
            <div className={`meta ${s.review_over ? 'ok-text' : ''}`}>
              {s.review_over ? `Review period ended ${fmt(s.review_ends)}` : `Review period ends ${fmt(s.review_ends)}`}
            </div>
          )}
          {s.schedule && <Schedule key={s.schedule.at || 'none'} sc={s.schedule} active={active}
            onSchedule={(on, at) => post(`${base}/schedule`, { on, at, track })}
            onAnswer={(happened) => post(`${base}/happened`, { happened, track })} />}
          {correcting && (
            <div className="correction-banner">
              <b>Sent back from {s.correction.from_label} on {fmt(s.correction.started)} for correction.</b>
              <div>Check <b>Issue</b> next to each document with a problem. When every issue is cleared, the case goes back to {s.correction.from_label}.</div>
              {active && <label className="inline">Issues cleared on
                <input type="date" value={date} max={todayISO()} onChange={(e) => setDate(e.target.value)} /></label>}
            </div>
          )}
          <Corrections s={s} />
          {sections.map((sec) => (
            <ChecklistSection key={sec.label || 'list'} sec={sec} active={active && !correcting}
              onCheck={(ci, checked, on) => post(`${base}/check`, { key: ci.key, checked, checked_on: on, track })}
              beforeNext={!s.groups.length && correctionRow} />
          ))}
          {s.sections.length > 0 && (
            <div className="sections">
              {s.sections.map((sec) => (
                <StepSection key={sec.key} sec={sec} active={active && !correcting && s.checklist.every((ci) => ci.checked)}
                  correcting={active && correcting}
                  onIssue={(item, checked) => post(`${base}/issue`, { section: sec.key, item, checked, on: date, track })}
                  onAct={(body) => post(`${base}/section`, { section: sec.key, track, ...body })} />
              ))}
              {!s.checklist.every((ci) => ci.checked) && (
                <div className="meta">Check off {s.checklist.filter((ci) => !ci.checked).map((ci) => ci.label).join(' and ')} to start these.</div>
              )}
            </div>
          )}
          {active && (
            <div className="actions">
              {!hasList && !s.sections.length && !s.schedule && (
                <>
                  <label className="inline">Completed on
                    <input type="date" value={date} max={todayISO()} onChange={(e) => setDate(e.target.value)} />
                  </label>
                  <button className="btn primary" disabled={!date}
                    onClick={() => post(`${base}/complete`, { completed_on: date, track })}>Mark complete</button>
                </>
              )}
              {!skipping && !correcting && <button className="link" onClick={() => setSkipping(true)}>Skip this step</button>}
            </div>
          )}
          {active && skipping && (
            <div className="actions">
              <input placeholder="Why is it being skipped?" value={reason} onChange={(e) => setReason(e.target.value)} autoFocus />
              <button className="btn" disabled={!reason.trim()} onClick={() => post(`${base}/skip`, { reason, track })}>Skip step</button>
              <button className="link" onClick={() => { setSkipping(false); setReason('') }}>Cancel</button>
            </div>
          )}
        </div>
      </li>
    )
  }

  return (
    <li className={cls}>
      <span className="marker" aria-hidden />
      <div className="body">
        <div className="label">{s.label}</div>
        {s.status === 'done' && (
          <div className="meta">
            {editingDate ? (
              <span className="inline">
                <input type="date" value={newDate} max={todayISO()} onChange={(e) => setNewDate(e.target.value)} />
                <button className="link" onClick={async () => { await post(`${base}/date`, { new_date: newDate, track }); setEditingDate(false) }}>Save</button>
                <button className="link" onClick={() => setEditingDate(false)}>Cancel</button>
              </span>
            ) : (
              <>Done {fmt(s.date)}{s.by ? ` by ${s.by}` : ''}{active && <> <button className="link" onClick={() => { setNewDate(s.date); setEditingDate(true) }}>Change date</button></>}</>
            )}
          </div>
        )}
        {s.status === 'done' && s.started_by && <div className="meta">{s.started_by.label} {fmt(s.started_by.date)}</div>}
        {s.status === 'done' && s.review_ends && (
          <div className={`meta ${s.review_over ? 'ok-text' : ''}`}>
            {s.review_over ? `Review period ended ${fmt(s.review_ends)}` : `Review period ends ${fmt(s.review_ends)}`}
          </div>
        )}
        <Holds s={s} />
        {/* hide_when_done boxes (e.g. Draft sent) are shown on the next step instead */}
        {s.status === 'done' && checklistSections(s).filter((sec) => sec.items.some((ci) => ci.checked && !ci.hide_when_done)).map((sec) => (
          <div className="meta" key={sec.label || 'list'}>
            {sec.label && <>{sec.label}: </>}
            {sec.items.filter((ci) => ci.checked && !ci.hide_when_done).map((ci) => `✓ ${ci.label}${ci.date ? ` ${fmt(ci.date)}` : ''}`).join('  ·  ')}
          </div>
        ))}
        {s.status === 'done' && s.sections.filter((sec) => sec.done).map((sec) => (
          <div className="meta" key={sec.key}>✓ {[sec.label, sectionSummary(sec)].filter(Boolean).join(': ')}{sec.done_on ? `, ${fmt(sec.done_on)}` : ''}</div>
        ))}
        {s.status === 'done' && s.carry_on_open && s.sections.filter((sec) => !sec.done).map((sec) => (
          <div className="sections" key={sec.key}>
            <StepSection sec={sec} active={active} correcting={false} onIssue={() => {}}
              onAct={(body) => post(`${base}/section`, { section: sec.key, track, ...body })} />
            <div className="meta">Still open: it has to be done before the signing can be scheduled.</div>
          </div>
        ))}
        <Corrections s={s} />
        {s.status === 'skipped' && <div className="meta">Skipped: {s.reason}</div>}
        {s.status === 'pending' && s.hints.length > 0 && s.hints.map((h, i) => <div key={i} className="meta">{h}</div>)}
      </div>
    </li>
  )
}

// A step's checklist split into its groups (or one unnamed group when it has none).
function checklistSections(s) {
  if (!s.groups.length) return [{ label: null, items: s.checklist }]
  return s.groups.map((g) => ({ label: g.label, items: s.checklist.filter((ci) => ci.group === g.key) }))
}

// Boxes in a group go in order: only the next one can be checked (on the chosen date, today by default),
// and only the most recent one can be unchecked. Boxes in different groups (e.g. the After signing tasks,
// each its own group) can be checked in any order. A box marked show_after_previous stays hidden until the
// box before it is checked, and a box with `after` (e.g. Sent to Transferor) until that box is. `beforeNext` (e.g. the FAI correction row) is shown just above the next box.
// A from_schedule box (e.g. Interview held) is ticked by answering that the booked time happened.
function ChecklistSection({ sec, active, onCheck, beforeNext }) {
  const [date, setDate] = useState(todayISO())
  const group = (ci) => sec.items.filter((x) => x.group === ci.group)
  const ready = (ci) => !ci.after || sec.items.some((x) => x.key === ci.after && x.checked)
  const isNext = (ci) => group(ci).find((x) => !x.checked) === ci && ready(ci)
  const isLast = (ci) => { const g = group(ci); const n = g.findIndex((x) => !x.checked); return g[(n === -1 ? g.length : n) - 1] === ci
    && !sec.items.some((x) => x.after === ci.key && x.checked) }
  const next = sec.items.findIndex(isNext)  // the first box that can be checked; the date picker sits beside it
  return (
    <div className="checklist">
      {sec.label && <div className="meta"><b>{sec.label}</b></div>}
      {sec.items.map((ci, i) => (ci.show_after_previous && i > 0 && !sec.items[i - 1].checked) || !ready(ci) ? null : (
        <Fragment key={ci.key}>
          {i === next && beforeNext}
          <div className="actions">
            <label className={`check ${!ci.checked && !isNext(ci) ? 'muted' : ''}`}>
              <input type="checkbox" checked={ci.checked}
                disabled={!active || (ci.checked ? !isLast(ci) : ci.from_schedule || !isNext(ci) || !date)}
                onChange={(e) => onCheck(ci, e.target.checked, date)} />
              {ci.label}{ci.checked && ci.date && <span className="muted"> on {fmt(ci.date)}</span>}
            </label>
            {active && i === next && !ci.from_schedule && (
              <label className="inline">on
                <input type="date" value={date} max={todayISO()} onChange={(e) => setDate(e.target.value)} />
              </label>
            )}
          </div>
        </Fragment>
      ))}
    </div>
  )
}

// Booking a later step from this one (the review period books the signing), and once the booked time has
// passed, asking whether it happened. A date inside the review period is refused with a message.
function Schedule({ sc, active, onSchedule, onAnswer }) {
  const [editing, setEditing] = useState(!sc.at)
  const [on, setOn] = useState(sc.at ? sc.at.slice(0, 10) : '')
  const [at, setAt] = useState(sc.at ? sc.at.slice(11, 16) : '')
  const tooEarly = on && sc.not_before && on < sc.not_before
  if (!sc.at && sc.blocked) return <p className="meta stale-text">{sc.blocked}</p>
  if (sc.at && !editing) {
    return (
      <div className="schedule">
        <div className="meta"><b>{sc.label} scheduled for {fmtAt(sc.at)}</b>
          {active && !sc.passed && <> <button className="link" onClick={() => setEditing(true)}>Change</button></>}</div>
        {active && sc.passed && sc.question && (
          <div className="actions">
            <span className="q">{sc.question}</span>
            <button className="btn primary" onClick={() => onAnswer(true)}>Yes</button>
            <button className="btn" onClick={() => window.confirm(`${sc.label} didn't happen? The date will be cleared so you can schedule a new one.`) && onAnswer(false)}>No</button>
          </div>
        )}
      </div>
    )
  }
  if (!active) return null
  return (
    <div className="schedule">
      <div className="actions">
        <label className="inline">{sc.label} scheduled on
          <input type="date" value={on} onChange={(e) => setOn(e.target.value)} /></label>
        <label className="inline">at
          <input type="time" value={at} onChange={(e) => setAt(e.target.value)} /></label>
        <button className="btn primary" disabled={!on || !at || tooEarly || !!sc.blocked}
          onClick={async () => { if (await onSchedule(on, at)) setEditing(false) }}>Schedule</button>
        {sc.at && <button className="link" onClick={() => setEditing(false)}>Cancel</button>}
      </div>
      {sc.blocked && <p className="error" role="alert">{sc.blocked}</p>}
      {tooEarly && (
        <p className="error" role="alert">
          {sc.not_before_why} isn't over until {fmt(sc.not_before)}, so the {sc.label.toLowerCase()} can't be scheduled on this date.
        </p>
      )}
    </div>
  )
}

// Past trips back for correction (e.g. from the FA draft back to FAI), for the record.
function Corrections({ s }) {
  return s.corrections.map((c, i) => (
    <div className="meta" key={i}>
      Sent back for correction {fmt(c.started)}{c.resolved ? `, documents fixed ${fmt(c.resolved)}` : ', in progress'}
    </div>
  ))
}

// The answers that shaped a finished section, e.g. "LLC" or "Lease on file? Yes, Lease term covers renewal term? No".
function sectionSummary(sec) {
  if (sec.done_text) return sec.done_text
  return sec.nodes.filter((n) => n.type === 'choice' && n.answer)
    .map((n) => {
      const label = n.options.find((o) => o.value === n.answer)?.label
      return n.label.endsWith('?') ? `${n.label} ${label}` : label
    }).join(', ')
}

// One part of a step that runs alongside the others (e.g. FAI's personal documents). It follows its
// answers down: choices pick which boxes appear, and it completes (on the chosen date) when it reaches the end.
function StepSection({ sec, active, correcting, onAct, onIssue }) {
  const [date, setDate] = useState(todayISO())
  const act = (body) => onAct({ ...body, done_on: date })
  return (
    <div className={`section ${sec.done ? 'done' : ''}`}>
      <div className="meta">
        <b>{sec.label}</b>
        {sec.done
          ? <span className="ok-text"> ✓ {sec.done_text || 'Complete'}{sec.done_on ? `, ${fmt(sec.done_on)}` : ''}</span>
          : active && <label className="inline"> completed on <input type="date" value={date} max={todayISO()} onChange={(e) => setDate(e.target.value)} /></label>}
      </div>
      {sec.nodes.map((n) => n.type === 'choice' ? (
        <div className="node choice" key={n.key}>
          <span className="q">{n.label}</span>
          {n.options.map((o) => (
            <label className="check" key={o.value}>
              <input type="radio" name={`${sec.key}-${n.key}`} checked={n.answer === o.value} disabled={!active}
                onChange={() => act({ node: n.key, value: o.value })} />
              {o.label}
            </label>
          ))}
        </div>
      ) : (
        <div className="node" key={n.key}>
          {n.label && <div className="meta">{n.label}</div>}
          {n.instruction && <div className="instruction">{n.instruction}</div>}
          {n.items.map((it, i) => (
            <div className="doc-row" key={it.key}>
              <label className={`check ${n.ordered && !it.checked && i > 0 && !n.items[i - 1].checked ? 'muted' : ''}`}>
                <input type="checkbox" checked={it.checked}
                  disabled={!active || (n.ordered && (it.checked ? !!n.items[i + 1]?.checked : i > 0 && !n.items[i - 1].checked))}
                  onChange={(e) => act({ node: n.key, item: it.key, checked: e.target.checked })} />
                {it.label}
              </label>
              {n.issues && (correcting || it.issue) && (
                <label className={`check issue ${it.issue ? 'on' : ''}`}>
                  <input type="checkbox" checked={it.issue} disabled={!correcting}
                    onChange={(e) => onIssue(it.key, e.target.checked)} />
                  Issue
                </label>
              )}
            </div>
          ))}
        </div>
      ))}
    </div>
  )
}

function Notes({ c, post }) {
  const [text, setText] = useState('')
  const [fu, setFu] = useState('')
  const add = async () => { if (await post('/notes', { text, follow_up_date: fu || null })) { setText(''); setFu('') } }
  const notes = [...c.notes].reverse()

  return (
    <div className="notes">
      <h2>Notes and follow-ups</h2>
      <textarea rows={3} placeholder="What happened, or what needs to happen next?" value={text} onChange={(e) => setText(e.target.value)} />
      <div className="actions">
        <label className="inline">Follow up on <input type="date" value={fu} onChange={(e) => setFu(e.target.value)} /></label>
        <button className="btn primary" disabled={!text.trim()} onClick={add}>Add note</button>
      </div>
      {notes.length === 0 && <p className="muted small">No notes yet. Add a follow-up date and it will show up in Needs attention when it's due.</p>}
      <ul>
        {notes.map((n) => (
          <li key={n.id} className={n.resolved ? 'resolved' : ''}>
            <p>{n.text}</p>
            <div className="small muted">{n.author}, {fmt(n.created_at)}{n.step && <> · at {n.step}</>}</div>
            {n.follow_up_date && (
              <div className={`fu ${n.overdue ? 'alert' : n.due_today ? 'warn' : ''}`}>
                {n.resolved ? `Follow-up done (${fmt(n.follow_up_date)})` : `Follow up ${fmt(n.follow_up_date)}`}
                {!n.resolved && <button className="link" onClick={() => post(`/notes/${n.id}/resolve`)}>Mark done</button>}
              </div>
            )}
          </li>
        ))}
      </ul>
    </div>
  )
}

function CloseForm({ outcome, onConfirm, onCancel }) {
  const [reason, setReason] = useState('')
  return (
    <div className="actions closeform">
      <input placeholder={`Why is this being closed as ${outcome.replace('_', ' ')}?`} value={reason} onChange={(e) => setReason(e.target.value)} autoFocus />
      <button className="btn" disabled={!reason.trim()} onClick={() => onConfirm(reason)}>Close case</button>
      <button className="link" onClick={onCancel}>Cancel</button>
    </div>
  )
}

function Editor({ c, onSave }) {
  const [f, setF] = useState({
    store_name: c.store_name, franchisee: c.franchisee, transferee: c.transferee, related_units: c.related_units || '', box_url: c.box_url,
    start_date: c.start_date, target_signing_date: c.target_signing_date || '', lease_deadline: c.lease_deadline_set || '',
  })
  const [units, setUnits] = useState(c.units.length ? c.units : [{ store_number: '', store_name: '' }])
  const [dates, setDates] = useState(c.dates)
  const [flags, setFlags] = useState(c.flags)
  const set = (k) => (e) => setF({ ...f, [k]: e.target.value })
  const hasUnits = c.type === 'transfer' && !!flags.transferee_is_existing
  const save = () => {
    const body = { store_name: f.store_name, franchisee: f.franchisee, box_url: f.box_url, start_date: f.start_date,
      target_signing_date: f.target_signing_date || null, flags }
    if (c.type === 'transfer') body.transferee = f.transferee
    if (c.type === 'transfer') body.related_units = hasUnits ? f.related_units : ''
    if (c.type === 'expansion') body.units = units
    const dateDefs = c.date_defs.filter((d) => applies(d.only_when, flags))
    if (dateDefs.length) body.dates = Object.fromEntries(dateDefs.map((d) => [d.key, dates[d.key] || null]))
    if (c.side_track) body.lease_deadline = f.lease_deadline || null
    onSave(body)
  }
  return (
    <div className="editor form">
      <div className="grid2">
        <label>Store name<input value={f.store_name} onChange={set('store_name')} /></label>
        <label>{c.type === 'transfer' ? 'Transferor' : 'Franchisee'}<input value={f.franchisee} onChange={set('franchisee')} /></label>
        {c.type === 'transfer' && <label>Transferee<input value={f.transferee} onChange={set('transferee')} /></label>}
        {hasUnits && <label>Related units<input value={f.related_units} onChange={set('related_units')} /></label>}
        <label>Start date<input type="date" max={todayISO()} value={f.start_date} onChange={set('start_date')} /></label>
        {c.date_defs.filter((d) => applies(d.only_when, flags)).map((d) => (
          <label key={d.key}>{d.label}
            <input type="date" required={d.required} value={dates[d.key] || ''} onChange={(e) => setDates({ ...dates, [d.key]: e.target.value })} /></label>
        ))}
        {c.has_signing && <label>Target signing date<input type="date" value={f.target_signing_date} onChange={set('target_signing_date')} /></label>}
        {c.side_track && <label>Lease deadline<input type="date" value={f.lease_deadline} onChange={set('lease_deadline')} />
          <span className="hint">Leave blank to use 6 months after signing.</span></label>}
        <label>Box folder link<input value={f.box_url} onChange={set('box_url')} /></label>
      </div>
      {c.type === 'expansion' && <UnitList units={units} onChange={setUnits} />}
      {c.flag_defs.map((fl) => <FlagField key={fl.key} fl={fl} flags={flags} onChange={setFlags} />)}
      <button className="btn primary" onClick={save}>Save changes</button>
    </div>
  )
}
function HoldForm({ onConfirm, onCancel }) {
  const [reason, setReason] = useState('')
  const [on, setOn] = useState(todayISO())
  return (
    <div className="actions closeform">
      <input placeholder="Why is it on hold? (kept in the case history)" value={reason} onChange={(e) => setReason(e.target.value)} autoFocus />
      <label className="inline">since <input type="date" value={on} max={todayISO()} onChange={(e) => setOn(e.target.value)} /></label>
      <button className="btn" disabled={!reason.trim() || !on} onClick={() => onConfirm(reason, on)}>Put on hold</button>
      <button className="link" onClick={onCancel}>Cancel</button>
    </div>
  )
}

// The open hold: where it was set, why, and for how long; the day count on the current step is paused.
function HoldBanner({ hold, active, onResume }) {
  const [on, setOn] = useState(todayISO())
  return (
    <div className="hold-banner">
      <b>On hold at {hold.step_label} since {fmt(hold.start)} ({days(hold.days)})</b>
      <div>{hold.reason} <span className="muted small">({hold.by})</span></div>
      {active && (
        <div className="actions">
          <label className="inline">Resume on <input type="date" value={on} max={todayISO()} onChange={(e) => setOn(e.target.value)} /></label>
          <button className="btn" disabled={!on} onClick={() => onResume(on)}>Resume</button>
        </div>
      )}
    </div>
  )
}

// Every hold set at this step, for the record.
function Holds({ s }) {
  return s.holds.map((h, i) => (
    <div className="meta hold-text" key={i}>
      {h.end ? `On hold ${fmt(h.start)} to ${fmt(h.end)} (${days(h.days)})` : `On hold since ${fmt(h.start)} (${days(h.days)})`}: {h.reason}
    </div>
  ))
}

// Follow-ups due before one of the case's dates (e.g. the Election Form due date), ticked once done.
function Reminders({ rem, active, onSet }) {
  const [date, setDate] = useState(todayISO())
  if (!rem.due) return <div className="meta">Add the {rem.what} due date (Edit details) to get follow-up reminders.</div>
  return (
    <div className="checklist">
      <div className="meta"><b>{rem.what} due {fmt(rem.due)}</b></div>
      {rem.items.map((r) => (
        <div className="actions" key={r.key}>
          <label className={`check ${!r.done && r.from > todayISO() ? 'muted' : ''}`}>
            <input type="checkbox" checked={!!r.done} disabled={!active} onChange={(e) => onSet(r.key, e.target.checked, date)} />
            {r.label}
            <span className="muted"> {r.done ? `done ${fmt(r.done)}` : `(from ${fmt(r.from)})`}</span>
          </label>
        </div>
      ))}
      {active && <label className="inline small">Followed up on <input type="date" value={date} max={todayISO()} onChange={(e) => setDate(e.target.value)} /></label>}
    </div>
  )
}

// Cases tied to this one. A lease case links to the renewal, transfer or expansion for the same premises, and
// both sides show the link with who opened the other case, so whoever handles either one is in the loop.
// An open case for the same store # that isn't linked yet is suggested (and flagged in Needs attention).
function Related({ c, meta, cases, post }) {
  const rel = c.related || { linked: [], suggested: [] }
  const linker = c.links_to.length > 0
  const linkers = meta ? Object.keys(meta.templates).filter((k) => meta.templates[k].links_to.includes(c.type)) : []
  if (!linker && !linkers.length) return null
  const kinds = linker ? c.links_to : linkers
  const taken = new Set([c.id, ...rel.linked.map((o) => o.id), ...rel.suggested.map((o) => o.id)])
  const options = (cases || []).filter((o) => o.status === 'active' && kinds.includes(o.type) && !taken.has(o.id))
  const link = (id, linked) => post('/links', { case_id: id, linked })
  const kindLabel = linker ? 'cases' : `${linkers.map((k) => meta.templates[k].label.toLowerCase()).join(' / ')} cases`
  return (
    <div className="related">
      <h2>Linked {kindLabel}</h2>
      {rel.linked.length === 0 && rel.suggested.length === 0 && (
        <p className="muted small">{linker ? 'Not linked to a renewal, transfer or expansion.' : `No ${kindLabel} linked.`}</p>
      )}
      <ul>
        {rel.linked.map((o) => (
          <RelatedRow key={o.id} o={o}>
            <button className="link small" onClick={() => window.confirm(`Unlink ${o.id}?`) && link(o.id, false)}>Unlink</button>
          </RelatedRow>
        ))}
        {rel.suggested.map((o) => (
          <RelatedRow key={o.id} o={o} suggested>
            <div className="small">Same store #, not linked yet. <button className="link" onClick={() => link(o.id, true)}>Link</button></div>
          </RelatedRow>
        ))}
      </ul>
      <LinkPicker options={options} onPick={(id) => link(id, true)} />
      {!linker && <button className="link small" onClick={() => go(`/new?type=${linkers[0]}&from=${encodeURIComponent(c.id)}`)}>
        Start a {meta.templates[linkers[0]].label.toLowerCase()} case for this store</button>}
    </div>
  )
}

function RelatedRow({ o, suggested, children }) {
  const at = o.status !== 'active' ? STATUS_LABEL[o.status]
    : o.current ? `At “${o.current.label}” with ${o.current.owner}` : o.side?.waiting_label
  return (
    <li className={suggested ? 'suggested' : ''}>
      <div className="rel-title"><a className="case-id" href={`#/cases/${encodeURIComponent(o.id)}`}>{o.id}</a>
        <span className="pill type">{o.type_label}</span>{o.store_name}</div>
      <div className="small muted">{at}{o.status === 'active' && o.current?.status_note ? `. ${o.current.status_note}` : ''}</div>
      {o.type === 'lease' && o.expires && <div className="small muted">Lease expires {fmt(o.expires)}</div>}
      <div className="small muted">Opened by {o.created_by_name}</div>
      {children}
    </li>
  )
}
