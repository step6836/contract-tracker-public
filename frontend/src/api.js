async function request(method, path, body) {
  const res = await fetch('/api' + path, {
    method,
    headers: body !== undefined ? { 'Content-Type': 'application/json' } : {},
    credentials: 'same-origin',
    body: body !== undefined ? JSON.stringify(body) : undefined,
  })
  if (res.status === 401 && path !== '/login' && path !== '/me') {
    window.dispatchEvent(new Event('auth-lost'))
    throw new Error('Please sign in again.')
  }
  const data = await res.json().catch(() => null)
  if (!res.ok) {
    const d = data && data.detail
    const msg = typeof d === 'string' ? d : Array.isArray(d) ? d.map((x) => x.msg).join('; ') : `Something went wrong (${res.status}).`
    throw new Error(msg)
  }
  return data
}

export const api = {
  get: (p) => request('GET', p),
  post: (p, b = {}) => request('POST', p, b),
  patch: (p, b) => request('PATCH', p, b),
}
