import { RELEASE_REASON } from './settings.js'

// The calls to the service. The token is kept in this browser (localStorage, so that closing the tab does not lose it) and sent with
// every call; the service asks for it only where something is changed (or everywhere on a network), like the TTS-Suite.
const TOKEN_KEY = 'stt-suite-token'
// Not `Authorization`: behind a reverse proxy with its own login that header belongs to the proxy.
const TOKEN_HEADER = 'X-STT-Suite-Token'

export const token = {
  get: () => localStorage.getItem(TOKEN_KEY) ?? '',
  set: (value) => (value ? localStorage.setItem(TOKEN_KEY, value) : localStorage.removeItem(TOKEN_KEY)),
}

function headers(sendsJson) {
  const result = {}
  if (sendsJson) result['Content-Type'] = 'application/json'
  if (token.get()) result[TOKEN_HEADER] = token.get()
  return result
}

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
    headers: headers(!(body === undefined || body instanceof FormData)),
    body: body === undefined || body instanceof FormData ? body : JSON.stringify(body),
  })
  const data = await response.json().catch(() => ({}))
  if (!response.ok) throw new ApiError(response.status, data.detail ?? response.statusText, response.status === 409)
  return data
}


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
