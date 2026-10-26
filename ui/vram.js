import { service } from './api.js'
import { cardTable, h, replaceChildren } from './dom.js'
import { formatNumber } from './format.js'
import { t } from './i18n.js'

// How often the page asks for the progress of a running burn-in.
const POLL_MS = 2000

// What each model needs on a card: the table the service asks GPU-Orc with, and the burn-in that measures
// the real values on this machine.
export function vramView({ show, fail }) {
  const root = h('div')
  let config = null
  let burnIn = { running: false, step: '', results: [], error: null }
  let audio = null
  let timer = null
  // The models to measure, as "engine model" keys; all of them at first.
  const chosen = new Set()

  const keyOf = (engine, model) => `${engine} ${model}`
  // In the order of the lists the service names: Whisper's sizes from small to large, Parakeet's precisions.
  const rows = () => config.engines.flatMap((engine) => (engine === 'parakeet' ? config.parakeet_qualities : config.available_models)
    .map((model) => ({ engine, model, mib: config.vram_needed_mib[engine][model] })))

  async function load() {
    try {
      config = await service.config()
      burnIn = await service.burnIn()
      if (chosen.size === 0) rows().forEach((row) => chosen.add(keyOf(row.engine, row.model)))
      render()
      poll()
    } catch (error) { fail(error) }
  }

  // While a burn-in runs, the progress is asked for until it ends; then the table has the new values.
  function poll() {
    clearTimeout(timer)
    if (!burnIn.running) return
    timer = setTimeout(async () => {
      try {
        const wasRunning = burnIn.running
        burnIn = await service.burnIn()
        if (wasRunning && !burnIn.running) { config = await service.config(); show(burnIn.error ?? t('burnInDone')) }
        render()
        poll()
      } catch (error) { fail(error) }
    }, POLL_MS)
  }

  async function start() {
    if (audio === null) return fail(new Error(t('noAudio')))
    const form = new FormData()
    form.append('file', audio.blob, audio.name)
    form.append('entries', JSON.stringify(rows().filter((row) => chosen.has(keyOf(row.engine, row.model))).map(({ engine, model }) => ({ engine, model }))))
    try {
      await service.startBurnIn(form)
      burnIn = await service.burnIn()
      render()
      poll()
    } catch (error) { fail(error) }
  }

  function table() {
    const measured = (row) => config.vram_measured[row.engine]?.[row.model] !== undefined
    return cardTable([t('engine'), t('model'), t('needMib'), t('source'), t('measureIt')], rows().map((row) => [
      row.engine, row.model, formatNumber(row.mib),
      h('span', { class: measured(row) ? 'status loaded' : 'muted' }, t(measured(row) ? 'measured' : 'estimate')),
      h('input', { type: 'checkbox', checked: chosen.has(keyOf(row.engine, row.model)), disabled: burnIn.running,
        onchange: (event) => { if (event.target.checked) chosen.add(keyOf(row.engine, row.model)); else chosen.delete(keyOf(row.engine, row.model)) } }),
    ]))
  }

  function results() {
    if (burnIn.results.length === 0 && !burnIn.error) return null
    return h('div', {}, h('h3', {}, t('burnInResults')),
      burnIn.error ? h('p', { class: 'status busy' }, burnIn.error) : null,
      cardTable([t('engine'), t('model'), t('measuredMib'), t('card'), t('seconds')],
        burnIn.results.map((result) => [result.engine, result.model, formatNumber(result.measured_mib), formatNumber(result.card), formatNumber(result.seconds)])))
  }

  function render() {
    if (config === null) return
    replaceChildren(root,
      h('h2', {}, t('tabVram')),
      h('p', { class: 'muted' }, t('vramHint')),
      table(),
      h('h3', {}, t('burnIn')),
      h('p', { class: 'muted' }, t('burnInHint')),
      h('div', { class: 'row' },
        h('input', { type: 'file', id: 'burn-in-file', hidden: true, accept: 'audio/*,video/*', onchange: (event) => { const file = event.target.files[0]; audio = file ? { blob: file, name: file.name } : audio; render() } }),
        h('button', { onclick: () => document.getElementById('burn-in-file').click(), disabled: burnIn.running }, t('chooseFile')),
        audio !== null ? h('span', { class: 'muted' }, audio.name) : null,
        h('button', { class: 'primary', onclick: start, disabled: burnIn.running }, burnIn.running ? t('measuring', { step: burnIn.step }) : t('startBurnIn'))),
      results(),
    )
  }

  return { root, load, render }
}
