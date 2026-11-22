import { ApiError, service } from './api.js'
import { cardTable, h, replaceChildren } from './dom.js'
import { duration, formatNumber } from './format.js'
import { t } from './i18n.js'
import { BURN_IN_POLL_MS, REFRESH_MS } from './settings.js'

// The catalog: every model of every engine, with where it is loaded and what a card needs for it, a release
// for each row, and a burn-in that measures what it takes and makes that its budget.
export function catalogView({ show, fail }) {
  const root = h('div')
  let status = null
  // The table and the burn-in of each machine, drawn and driven by the same code: this one (name null) and
  // the others, as the service reports them (`hosts`, each with its own rows and burnIn).
  const local = { name: null, rows: [], burnIn: { running: false } }
  let hosts = []
  const sourceOf = (name) => (name === null ? local : hosts.find((host) => host.name === name))
  // A release the GPU refused because it is transcribing: offered once more with force.
  let offerForce = false
  // A release under way: its buttons stay off and the rows it touches say so, so it is not pressed twice.
  let releasing = false

  async function loadTable(source) {
    ;[source.rows, source.burnIn] = await Promise.all([service.engines(source.name), service.burnIn(source.name)])
  }

  async function load() {
    try {
      ;[status, { hosts }] = await Promise.all([service.status(), service.hosts()])
      // A machine whose table cannot be read says so in its place; the others and this one are drawn anyway.
      await Promise.all([loadTable(local), ...hosts.filter((host) => host.reachable).map((host) =>
        loadTable(host).catch((error) => { host.tableError = error.message }))])
      render()
      for (const source of [local, ...hosts]) if (source.burnIn?.running) followBurnIn(source.name)
    } catch (error) { fail(error) }
  }

  // Asks until the burn-in has ended, also after a reload of the page; then the new value is in the catalog.
  const following = new Set()
  async function followBurnIn(name) {
    if (following.has(name)) return
    following.add(name)
    try {
      let state = sourceOf(name).burnIn
      while (state.running) {
        await new Promise((resolve) => setTimeout(resolve, BURN_IN_POLL_MS))
        state = await service.burnIn(name)
        const source = sourceOf(name)
        if (source) source.burnIn = state
        render()
      }
      if (state.error) fail(new Error(state.error))
      else show(t('burnInDone', { engine: state.engine, model: state.model, peak: formatNumber(state.peak_mib) }))
      await load()
    } catch (error) { fail(error) } finally { following.delete(name) }
  }

  async function startBurnIn(source, row) {
    const on = source.name === null ? '' : `${t('burnInOn', { host: source.name })} `
    if (!window.confirm(on + t('burnInConfirm', { engine: row.engine, model: row.model }))) return
    try {
      source.burnIn = await service.startBurnIn(source.name, row.engine, row.model)
      render()
      followBurnIn(source.name)
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

  // What a row says about the model: where it is loaded. What a release under way, the busy GPU and the card's
  // details say is known of this machine only.
  function stateOf(source, row) {
    if (row.loaded_on.length === 0) return h('span', { class: 'status idle' }, t('idle'))
    const isLocal = source.name === null
    if (isLocal && releasing) return h('span', { class: 'status busy' }, t('busyRelease'))
    const onGpu = row.loaded_on.includes('gpu')
    const busy = isLocal && onGpu && status.gpu_busy
    return [
      h('span', { class: `status ${busy ? 'busy' : 'loaded'}` }, `${row.loaded_on.map((device) => t(device)).join(', ')}: ${t(busy ? 'busy' : 'loaded')}`),
      isLocal && onGpu ? h('div', { class: 'muted' }, gpuDetails()) : null,
    ]
  }

  const measured = (row) => h('span', { class: row.measured ? 'status loaded' : 'muted' }, t(row.measured ? 'measured' : 'estimate'))
  const burnInButton = (source, row, otherBusy = false) =>
    h('button', { class: 'small', onclick: () => startBurnIn(source, row), disabled: source.burnIn.running || otherBusy }, t('burnIn'))
  // Releasing is this machine's own; another machine's models are released by its GPU-Orc.
  const buttons = (source, row) => h('div', { class: 'buttons' },
    source.name === null ? h('button', { class: 'small', onclick: () => release(...stepsFor(row)), disabled: releasing || row.loaded_on.length === 0 }, t('release')) : null,
    burnInButton(source, row, source.name === null && status.gpu_busy))
  // Why a burn-in button is off is written out: a running one shows its progress here.
  const burnInLine = (source) => (source.burnIn.running ? h('p', { class: 'status busy' }, burnInText(source)) : null)

  // One machine, always built the same way: its name, whether it answers, the progress of its burn-in and its models.
  function machineSection(source) {
    const isLocal = source.name === null
    const answers = isLocal || source.reachable
    return h('div', {},
      h('h3', {}, isLocal ? t('thisMachineTitle') : source.name),
      isLocal ? h('div', { class: 'row' },
        h('button', { onclick: () => release(() => releaseGpu(), unloadCpu), disabled: releasing }, t('releaseAll')),
        offerForce ? h('button', { class: 'primary', onclick: () => release(() => releaseGpu(true)), disabled: releasing }, t('force')) : null,
        h('span', { class: 'muted' }, t('releaseAllHint'))) : null,
      isLocal ? null : h('p', { class: `status ${source.reachable ? 'loaded' : 'busy'}` },
        `${source.reachable ? t('reachable') : `${t('unreachable')}: ${source.error}`} · ${source.url}`),
      answers && source.tableError ? h('p', { class: 'status busy' }, source.tableError) : null,
      answers && !source.tableError ? [
        burnInLine(source),
        cardTable([t('engine'), t('model'), t('state'), t('memoryNeeded'), t('source'), ''],
          source.rows.map((row) => [row.engine, row.model, stateOf(source, row), formatNumber(row.needed_mib), measured(row), buttons(source, row)])),
      ] : null)
  }

  function render() {
    if (status === null) return
    replaceChildren(root,
      h('h2', {}, t('tabCatalog')),
      h('p', { class: 'muted' }, t('catalogHint')),
      h('div', { class: 'row' }, h('button', { onclick: load }, t('reload'))),
      [local, ...hosts].map(machineSection))
  }

  function burnInText({ name, burnIn }) {
    const where = name === null ? '' : `${name}: `
    if (burnIn.peak_mib === 0) return where + t('burnInStarting', { engine: burnIn.engine, model: burnIn.model })
    return where + t('burnInRunning', { engine: burnIn.engine, model: burnIn.model, peak: formatNumber(burnIn.peak_mib) })
  }

  setInterval(() => {
    if (!root.closest('[hidden]') && !document.hidden && !releasing) load()
  }, REFRESH_MS)

  return { root, load, render }
}
