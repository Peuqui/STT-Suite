import { ApiError, service } from './api.js'
import { cardTable, h, replaceChildren } from './dom.js'
import { t } from './i18n.js'

// How often the page asks again while it is shown: the idle countdown and a worker that starts or ends stay current.
const REFRESH_MS = 5000

// Minutes and seconds past a minute, plain seconds below it.
function duration(seconds) {
  if (seconds < 60) return `${seconds} s`
  return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, '0')} min`
}

// What is in memory on the CPU and on the GPU, and the buttons that release it.
export function statusView({ show, fail }) {
  const root = h('div')
  let status = null
  // A release the GPU refused because it is transcribing: offered once more with force.
  let offerForce = false

  async function load() {
    try {
      status = await service.status()
      render()
    } catch (error) { fail(error) }
  }

  async function unload(device, force = false) {
    try {
      const { unloaded } = await service.unload(device, force)
      offerForce = false
      show(unloaded.length === 0 ? t('nothingLoaded') : t('unloaded', { what: unloaded.join(', ') }))
    } catch (error) {
      if (!(error instanceof ApiError && error.busy)) return fail(error)
      offerForce = true
      show(t('gpuBusy'))
    }
    await load()
  }

  // What a device is set to, as "engine model": a Whisper size or "parakeet-<precision>".
  const configured = (device) => {
    const engine = status[`${device}_engine`]
    return `${engine} ${engine === 'parakeet' ? status[`${device}_quality`] : status[`${device}_model`]}`
  }

  function cpuRow() {
    if (!status.cpu_loaded) return { loaded: false }
    return { engine: status.cpu_loaded_engine, model: status.cpu_loaded_model, loaded: true }
  }

  function gpuRow() {
    if (!status.gpu_loaded) return { loaded: false }
    const left = status.gpu_ttl_remaining_seconds
    const ttl = status.gpu_ttl_minutes === 0 ? t('ttlOff') : left === undefined ? '' : t('ttlLeft', { time: duration(left) })
    return {
      engine: status.gpu_loaded_engine, model: status.gpu_loaded_model, loaded: true, busy: status.gpu_busy,
      details: [t('card', { index: status.gpu_device_index, pid: status.gpu_worker_pid }), ttl].filter(Boolean).join(' · '),
    }
  }

  function stateOf(row) {
    if (!row.loaded) return h('span', { class: 'status idle' }, t('idle'))
    return h('span', { class: `status ${row.busy ? 'busy' : 'loaded'}` }, t(row.busy ? 'busy' : 'loaded'))
  }

  function render() {
    if (status === null) return
    const cpu = cpuRow()
    const gpu = gpuRow()
    const rows = [['cpu', cpu], ['gpu', gpu]]
    replaceChildren(root,
      h('h2', {}, t('tabStatus')),
      h('p', { class: 'muted' }, t('statusHint')),
      h('div', { class: 'row' },
        h('button', { onclick: load }, t('reload')),
        h('button', { onclick: () => unload('gpu'), disabled: !gpu.loaded }, t('unloadGpu')),
        h('button', { onclick: () => unload('cpu'), disabled: !cpu.loaded }, t('unloadCpu')),
        h('button', { onclick: () => unload('all') }, t('unloadAll')),
        offerForce ? h('button', { class: 'primary', onclick: () => unload('gpu', true) }, t('force')) : null),
      cardTable([t('device'), t('configured'), t('engine'), t('model'), t('state'), t('details')],
        rows.map(([name, row]) => [t(name), configured(name), row.engine ?? '', row.model ?? '', stateOf(row), row.details ?? ''])),
    )
  }

  setInterval(() => {
    if (!root.closest('[hidden]') && !document.hidden) load()
  }, REFRESH_MS)

  return { root, load, render }
}
