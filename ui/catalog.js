import { ApiError, service } from './api.js'
import { cardTable, h, replaceChildren } from './dom.js'
import { duration, formatNumber } from './format.js'
import { t } from './i18n.js'
import { BURN_IN_POLL_MS, REFRESH_MS } from './settings.js'

// The catalog: every model of every engine, with where it is loaded and what a card needs for it, a release
// for each row, and a burn-in that measures what it takes and makes that its budget.
export function catalogView({ show, fail }) {
  const root = h('div')
  let rows = []
  let status = null
  let burnIn = { running: false }
  // A release the GPU refused because it is transcribing: offered once more with force.
  let offerForce = false
  // A release under way: its buttons stay off and the rows it touches say so, so it is not pressed twice.
  let releasing = false

  async function load() {
    try {
      ;[rows, status, burnIn] = await Promise.all([service.engines(), service.status(), service.burnIn()])
      render()
      if (burnIn.running) followBurnIn()
    } catch (error) { fail(error) }
  }

  // Asks until the burn-in has ended, also after a reload of the page; then the new value is in the catalog.
  let following = false
  async function followBurnIn() {
    if (following) return
    following = true
    try {
      while (burnIn.running) {
        await new Promise((resolve) => setTimeout(resolve, BURN_IN_POLL_MS))
        burnIn = await service.burnIn()
        render()
      }
      if (burnIn.error) fail(new Error(burnIn.error))
      else show(t('burnInDone', { engine: burnIn.engine, model: burnIn.model, peak: formatNumber(burnIn.peak_mib) }))
      await load()
    } catch (error) { fail(error) } finally { following = false }
  }

  async function startBurnIn(row) {
    if (!window.confirm(t('burnInConfirm', { engine: row.engine, model: row.model }))) return
    try {
      burnIn = await service.startBurnIn(row.engine, row.model)
      render()
      followBurnIn()
    } catch (error) { fail(error) }
  }

  async function releaseGpu(force = false) {
    try {
      const { results } = await service.releaseGpu(force)
      offerForce = false
      const released = results.filter((result) => result.outcome === 'released').map((result) => result.item)
      show(released.length === 0 ? t('nothingLoaded') : t('unloaded', { what: released.join(', ') }))
    } catch (error) {
      if (!(error instanceof ApiError && error.busy)) return fail(error)
      offerForce = true
      show(t('gpuBusy'))
    }
  }

  async function unloadCpu() {
    try {
      const { unloaded } = await service.unloadCpu()
      show(unloaded.length === 0 ? t('nothingLoaded') : t('unloaded', { what: unloaded.join(', ') }))
    } catch (error) { fail(error) }
  }

  // One button may release on both sides; the page is read again afterwards.
  async function release(...steps) {
    releasing = true
    render()
    try { for (const step of steps) await step() } finally { releasing = false }
    await load()
  }

  const stepsFor = (row) => [...(row.loaded_on.includes('gpu') ? [() => releaseGpu()] : []), ...(row.loaded_on.includes('cpu') ? [unloadCpu] : [])]

  // What the worker on the card is doing; only for the row it holds.
  function gpuDetails() {
    const left = status.gpu_ttl_remaining_seconds
    const ttl = status.gpu_ttl_minutes === 0 ? t('ttlOff') : left === undefined ? '' : t('ttlLeft', { time: duration(left) })
    return [t('cardOf', { index: status.gpu_device_index, pid: status.gpu_worker_pid }), ttl].filter(Boolean).join(' · ')
  }

  function stateOf(row) {
    if (row.loaded_on.length === 0) return h('span', { class: 'status idle' }, t('idle'))
    if (releasing) return h('span', { class: 'status busy' }, t('busyRelease'))
    const onGpu = row.loaded_on.includes('gpu')
    const busy = onGpu && status.gpu_busy
    return [
      h('span', { class: `status ${busy ? 'busy' : 'loaded'}` }, `${row.loaded_on.map((device) => t(device)).join(', ')}: ${t(busy ? 'busy' : 'loaded')}`),
      onGpu ? h('div', { class: 'muted' }, gpuDetails()) : null,
    ]
  }

  function render() {
    if (status === null) return
    const measured = (row) => h('span', { class: row.measured ? 'status loaded' : 'muted' }, t(row.measured ? 'measured' : 'estimate'))
    const buttons = (row) => h('div', { class: 'buttons' },
      h('button', { class: 'small', onclick: () => release(...stepsFor(row)), disabled: releasing || row.loaded_on.length === 0 }, t('release')),
      h('button', { class: 'small', onclick: () => startBurnIn(row), disabled: burnIn.running || status.gpu_busy }, t('burnIn')))
    replaceChildren(root,
      h('h2', {}, t('tabCatalog')),
      h('p', { class: 'muted' }, t('catalogHint')),
      h('div', { class: 'row' },
        h('button', { onclick: load }, t('reload')),
        h('button', { onclick: () => release(() => releaseGpu(), unloadCpu), disabled: releasing }, t('releaseAll')),
        offerForce ? h('button', { class: 'primary', onclick: () => release(() => releaseGpu(true)), disabled: releasing }, t('force')) : null),
      burnIn.running ? h('p', { class: 'status busy' }, burnInText()) : null,
      cardTable([t('engine'), t('model'), t('state'), t('memoryNeeded'), t('source'), ''],
        rows.map((row) => [row.engine, row.model, stateOf(row), formatNumber(row.needed_mib), measured(row), buttons(row)])),
    )
  }

  function burnInText() {
    if (burnIn.peak_mib === 0) return t('burnInStarting', { engine: burnIn.engine, model: burnIn.model })
    return t('burnInRunning', { engine: burnIn.engine, model: burnIn.model, peak: formatNumber(burnIn.peak_mib) })
  }

  setInterval(() => {
    if (!root.closest('[hidden]') && !document.hidden && !releasing) load()
  }, REFRESH_MS)

  return { root, load, render }
}
