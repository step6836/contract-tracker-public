import { useState } from 'react'
import { api } from '../api.js'

export default function Login({ onSignedIn }) {
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  const submit = async (e) => {
    e.preventDefault()
    setBusy(true); setError('')
    try { onSignedIn(await api.post('/login', { username, password })) }
    catch (err) { setError(err.message) }
    finally { setBusy(false) }
  }

  return (
    <div className="login">
      <form onSubmit={submit} className="login-card">
        <h1>Contracts Tracker</h1>
        <p className="muted">Sign in to see where every franchise case stands.</p>
        <label>Username<input value={username} onChange={(e) => setUsername(e.target.value)} autoFocus autoComplete="username" /></label>
        <label>Password<input type="password" value={password} onChange={(e) => setPassword(e.target.value)} autoComplete="current-password" /></label>
        {error && <p className="error" role="alert">{error}</p>}
        <button className="btn primary" disabled={busy || !username || !password}>{busy ? 'Signing in…' : 'Sign in'}</button>
      </form>
    </div>
  )
}
