import { useEffect, useState } from 'react'
import { api } from '../api.js'
import { go, todayISO } from '../util.js'

// ?type=lease&from=<case id> (Start a lease case, on a renewal/transfer/expansion) opens the form for that
// type with the store filled in and the case linked.
export default function NewCase({ meta, cases, params, onCreated }) {
  const [type, setType] = useState(params.get('type') || 'renewal')
  const [f, setF] = useState({ store_number: '', store_name: '', franchisee: '', transferee: '', related_units: '', start_date: todayISO(), target_signing_date: '', box_url: '' })
  const [links, setLinks] = useState(params.get('from') ? [params.get('from')] : [])
  const from = (cases || []).find((x) => x.id === params.get('from'))
  useEffect(() => {
    if (from) setF((f) => ({ ...f, store_number: from.store_number, store_name: from.store_name, franchisee: from.franchisee }))
  }, [from?.id])
  const [units, setUnits] = useState([{ store_number: '', store_name: '' }])
  const [dates, setDates] = useState({})
  const [flags, setFlags] = useState({})
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  if (!meta) return <p className="muted">Loading…</p>

  const t = meta.templates[type] || meta.templates.renewal
  const set = (k) => (e) => setF({ ...f, [k]: e.target.value })
  // Transfers to an existing franchisee can list the franchisee's other units (expansions use UnitList).
  const hasUnits = type === 'transfer' && !!flags.transferee_is_existing
  const unitsField = (
    <label>Related units <span className="opt">(optional)</span>
      <input value={f.related_units} onChange={set('related_units')} />
      <span className="hint">Leave blank if there are none.</span></label>
  )

  const submit = async (e) => {
    e.preventDefault()
    setBusy(true); setError('')
    try {
      const body = { ...f, type, flags, related_units: hasUnits ? f.related_units : '',
        units: type === 'expansion' ? units : [], target_signing_date: f.target_signing_date || null,
        links: t.links_to.length ? links : [],
        dates: Object.fromEntries(t.dates.map((d) => [d.key, dates[d.key] || null])) }
      const c = await api.post('/cases', body)
      onCreated()
      go(`/cases/${c.id}`)
    } catch (err) { setError(err.message); setBusy(false) }
  }

  return (
    <form className="form narrow" onSubmit={submit}>
      <div className="page-head"><h1>New case</h1></div>

      <div className="seg" role="group" aria-label="Case type">
        {Object.entries(meta.templates).map(([k, v]) => (
          <button type="button" key={k} className={type === k ? 'on' : ''} onClick={() => { setType(k); setFlags({}); setDates({}) }}>{v.label}</button>
        ))}
      </div>

      <div className="grid2">
        <label>Store #<input required inputMode="numeric" pattern="[0-9]+" title="Numbers only" value={f.store_number}
                 onChange={(e) => setF({ ...f, store_number: e.target.value.replace(/\D/g, '') })} placeholder="e.g. 97" />
          <span className="hint">Use 0 for a new location that has no number yet.</span></label>
        <label>Store name<input required value={f.store_name} onChange={set('store_name')} placeholder="e.g. Stone Mountain 2, GA" /></label>
      </div>

      <div className="grid2">
        <label>{type === 'transfer' ? 'Transferor (current franchisee)' : 'Franchisee'}
          <input value={f.franchisee} onChange={set('franchisee')} /></label>
        {t.has_transferee && <div className="stack">
          <label>Transferee<input value={f.transferee} onChange={set('transferee')} /></label>
          {hasUnits && unitsField}
        </div>}
      </div>

      {type === 'expansion' && <UnitList units={units} onChange={setUnits} />}

      <div className="grid2">
        <label>Start date
          <input type="date" required max={todayISO()} value={f.start_date} onChange={set('start_date')} />
          <span className="hint">The official start: {t.start_label}. That step is marked done on this date.</span></label>
        {t.has_signing && <label>Target signing date <span className="opt">(optional)</span>
          <input type="date" value={f.target_signing_date} onChange={set('target_signing_date')} />
          {t.target_from && <span className="hint">Leave blank to use the {t.dates.find((d) => d.key === t.target_from)?.label.toLowerCase()} date.</span>}</label>}
      </div>

      {t.dates.length > 0 && (
        <div className="grid2">
          {t.dates.map((d) => (
            <label key={d.key}>{d.label}
              <input type="date" required={d.required} value={dates[d.key] || ''} onChange={(e) => setDates({ ...dates, [d.key]: e.target.value })} />
              {d.hint && <span className="hint">{d.hint}</span>}</label>
          ))}
        </div>
      )}

      {t.flags.map((fl) => (
        <label className="check" key={fl.key}>
          <input type="checkbox" checked={!!flags[fl.key]} onChange={(e) => setFlags({ ...flags, [fl.key]: e.target.checked })} />
          {fl.label}
        </label>
      ))}

      {t.links_to.length > 0 && <LinksField types={t.links_to} cases={cases} storeNumber={f.store_number} links={links} onChange={setLinks} />}

      <label>Box folder link <span className="opt">(optional)</span><input value={f.box_url} onChange={set('box_url')} placeholder="https://…" /></label>

      {t.notes.map((n, i) => <p key={i} className="muted small">{n}</p>)}
      {error && <p className="error" role="alert">{error}</p>}
      <div className="row">
        <button className="btn primary" disabled={busy}>{busy ? 'Creating…' : 'Create case'}</button>
        <button type="button" className="btn" onClick={() => history.back()}>Cancel</button>
      </div>
    </form>
  )
}

// The open cases a new lease case links to. Open cases for the same store # are offered first.
function LinksField({ types, cases, storeNumber, links, onChange }) {
  const open = (cases || []).filter((o) => o.status === 'active' && types.includes(o.type))
  const byId = Object.fromEntries((cases || []).map((o) => [o.id, o]))
  const store = storeNumber.replace(/^0+/, '')
  const same = open.filter((o) => store && o.store_number.replace(/^0+/, '') === store && !links.includes(o.id))
  const rest = open.filter((o) => !links.includes(o.id) && !same.includes(o))
  const add = (id) => onChange([...links, id])
  return (
    <fieldset className="units links-field">
      <legend>Linked cases <span className="opt">(optional)</span></legend>
      {links.length > 0 && <div className="row">{links.map((id) => (
        <span className="chip" key={id}>{id} {byId[id] && <span className="muted">{byId[id].type_label}</span>}
          <button type="button" className="link" aria-label={`Remove ${id}`} onClick={() => onChange(links.filter((x) => x !== id))}>×</button></span>
      ))}</div>}
      {same.map((o) => (
        <div className="small" key={o.id}>Store #{o.store_number} has an open {o.type_label.toLowerCase()} case, {o.id}.{' '}
          <button type="button" className="link" onClick={() => add(o.id)}>Link it</button></div>
      ))}
      <LinkPicker options={rest} onPick={add} />
      <span className="hint">Link the renewal, transfer or expansion for these premises so whoever handles it stays in the loop.</span>
    </fieldset>
  )
}

// A dropdown of open cases to link; picking one links it.
export function LinkPicker({ options, onPick }) {
  if (!options.length) return null
  return (
    <select value="" onChange={(e) => e.target.value && onPick(e.target.value)} aria-label="Link a case">
      <option value="">Link another open case…</option>
      {options.map((o) => <option key={o.id} value={o.id}>{o.id} · {o.type_label} · {o.store_name}</option>)}
    </select>
  )
}

// An expansion's related units, one row per unit; "+ Add unit" for each extra one.
export function UnitList({ units, onChange }) {
  const edit = (i, k) => (e) => onChange(units.map((u, j) => (j === i ? { ...u, [k]: k === 'store_number' ? e.target.value.replace(/\D/g, '') : e.target.value } : u)))
  return (
    <fieldset className="units">
      <legend>Related units <span className="opt">(optional)</span></legend>
      {units.map((u, i) => (
        <div className="unit-row" key={i}>
          <label>Store #<input inputMode="numeric" value={u.store_number} onChange={edit(i, 'store_number')} /></label>
          <label>Store name<input value={u.store_name} onChange={edit(i, 'store_name')} /></label>
          <button type="button" className="link" aria-label={`Remove unit ${i + 1}`}
            onClick={() => onChange(units.length > 1 ? units.filter((_, j) => j !== i) : [{ store_number: '', store_name: '' }])}>Remove</button>
        </div>
      ))}
      <button type="button" className="btn" onClick={() => onChange([...units, { store_number: '', store_name: '' }])}>+ Add unit</button>
      <span className="hint">The franchisee's other units. Leave blank if there are none.</span>
    </fieldset>
  )
}
