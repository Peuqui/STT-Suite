// The calls to the service. It has no login (see the README), so there is no token.
export class ApiError extends Error {
  constructor(status, detail, busy) {
    super(detail)
    this.status = status
    // 409 from `/gpu/release`: a transcription is running on the GPU.
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
  if (!response.ok) throw new ApiError(response.status, data.detail ?? response.statusText, response.status === 409)
  return data
}

// Who asks, in the log of the service and in what a forced release tells a running transcription.
const RELEASE_REASON = 'STT-Suite page'

export const service = {
  status: () => json('../status'),
  engines: () => json('../engines'),
  config: () => json('../config'),
  saveConfig: (config) => json('../config', { method: 'POST', body: config }),
  unloadCpu: () => json('../unload', { method: 'POST' }),
  releaseGpu: (force) => json('../gpu/release', { method: 'POST', body: { items: 'all', force, reason: RELEASE_REASON } }),
  burnIn: () => json('../burn-in'),
  startBurnIn: (engine, model) => json('../burn-in', { method: 'POST', body: { engine, model } }),
  transcribe: (form) => json('../transcribe', { method: 'POST', body: form }),
}
