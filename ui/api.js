// The calls to the service. It has no login (see the README), so there is no token.
export class ApiError extends Error {
  constructor(status, detail, busy) {
    super(detail)
    this.status = status
    // `unload` answers 409 with busy: a transcription is running on the GPU.
    this.busy = busy
  }
}

export async function json(path, { method = 'GET', body } = {}) {
  const response = await fetch(path, {
    method,
    headers: body === undefined || body instanceof FormData ? {} : { 'Content-Type': 'application/json' },
    body: body === undefined || body instanceof FormData ? body : JSON.stringify(body),
  })
  const data = await response.json().catch(() => ({}))
  if (!response.ok) throw new ApiError(response.status, data.error ?? response.statusText, data.busy === true)
  return data
}

const query = (parameters) => {
  const search = new URLSearchParams()
  for (const [key, value] of Object.entries(parameters)) if (value !== null && value !== undefined) search.set(key, value)
  const text = search.toString()
  return text ? `?${text}` : ''
}

export const service = {
  status: () => json('../status'),
  config: () => json('../config'),
  saveConfig: (config) => json('../config', { method: 'POST', body: config }),
  unload: (device, force) => json(`../unload${query({ device, force: force ? '1' : null })}`, { method: 'POST' }),
  burnIn: () => json('../burn-in'),
  startBurnIn: (form) => json('../burn-in', { method: 'POST', body: form }),
  transcribe: (form) => json('../transcribe', { method: 'POST', body: form }),
}
