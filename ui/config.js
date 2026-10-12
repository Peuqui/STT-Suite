import { service } from './api.js'
import { h, replaceChildren } from './dom.js'
import { t } from './i18n.js'

// The runtime configuration of the service: what a request without its own choice uses. Grouped by
// what a setting belongs to: the engine of each device, the settings of each engine, and the general ones.
export function configView({ show, fail }) {
  const root = h('div')
  let config = null

  async function load() {
    try {
      config = await service.config()
      render()
    } catch (error) { fail(error) }
  }

  async function save() {
    try {
      const { changed } = await service.saveConfig(settings())
      show(changed.length === 0 ? t('nothingChanged') : t('saved', { changed: changed.join(', ') }))
      await load()
    } catch (error) { fail(error) }
  }

  // The values the form holds; only what differs from the service's is sent (and reported as changed).
  const draft = {}
  const settings = () => Object.fromEntries(Object.entries(draft).filter(([key, value]) => value !== config[key]))

  function choose(key, options, labelOf = (option) => option) {
    return h('select', { onchange: (event) => (draft[key] = event.target.value) },
      options.map((option) => h('option', { value: option, selected: option === draft[key] }, labelOf(option))))
  }

  // What a Whisper size or a Parakeet precision needs before a card takes it, in the choice itself.
  const whisperLabel = (size) => `${size} · ${t('needsFree', { mib: config.vram_needed_mib.whisper[size] })}`
  const parakeetLabel = (precision) => `${precision} · ${t('needsFree', { mib: config.vram_needed_mib.parakeet[precision] })}`

  const number = (key) => h('input', { type: 'number', value: draft[key], onchange: (event) => (draft[key] = Number(event.target.value)) })
  const text = (key) => h('input', { type: 'text', value: draft[key], onchange: (event) => (draft[key] = event.target.value) })
  const check = (key, label) => h('label', { class: 'check' }, h('input', { type: 'checkbox', checked: draft[key], onchange: (event) => (draft[key] = event.target.checked) }), label)

  const group = (title, hint, ...fields) => h('fieldset', {}, h('legend', {}, title), hint ? h('p', { class: 'muted' }, hint) : null, h('div', { class: 'fields' }, fields))

  function render() {
    if (config === null) return
    for (const key of ['cpu_engine', 'gpu_engine', 'cpu_model', 'gpu_model', 'cpu_quality', 'gpu_quality', 'gpu_ttl_minutes', 'beam_size',
      'language', 'vad_filter', 'condition_on_previous_text', 'initial_prompt', 'num_speakers']) draft[key] = config[key]
    replaceChildren(root,
      h('h2', {}, t('tabConfig')),
      h('p', { class: 'muted' }, t('configHint')),
      group(t('groupDevices'), t('groupDevicesHint'),
        h('label', {}, t('cpuEngine')), choose('cpu_engine', config.engines),
        h('label', {}, t('gpuEngine')), choose('gpu_engine', config.engines)),
      group('Whisper', t('groupWhisperHint'),
        h('label', {}, t('cpuModel')), choose('cpu_model', config.available_models, whisperLabel),
        h('label', {}, t('gpuModel')), choose('gpu_model', config.available_models, whisperLabel),
        h('label', {}, t('beam')), number('beam_size'),
        h('label', {}, t('prompt')), text('initial_prompt'),
        h('span'), check('vad_filter', t('vad')),
        h('span'), check('condition_on_previous_text', t('previous'))),
      group('Parakeet', t('groupParakeetHint'),
        h('label', {}, t('cpuQuality')), choose('cpu_quality', config.parakeet_qualities, parakeetLabel),
        h('label', {}, t('gpuQuality')), choose('gpu_quality', config.parakeet_qualities, parakeetLabel)),
      group(t('groupGeneral'), '',
        h('label', {}, t('languageField')), text('language'),
        h('label', {}, t('ttl')), number('gpu_ttl_minutes'),
        h('label', {}, t('speakers')), number('num_speakers')),
      h('div', { class: 'actions' },
        h('button', { class: 'primary', onclick: save }, t('save')),
        h('button', { onclick: load }, t('reload'))),
    )
  }

  return { root, load, render }
}
