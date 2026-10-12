import { service } from './api.js'
import { h, replaceChildren } from './dom.js'
import { t } from './i18n.js'

// The runtime configuration of the service: what a request without its own choice uses.
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

  function choose(key, options) {
    return h('select', { onchange: (event) => (draft[key] = event.target.value) },
      options.map((option) => h('option', { value: option, selected: option === draft[key] }, option)))
  }

  const number = (key) => h('input', { type: 'number', value: draft[key], onchange: (event) => (draft[key] = Number(event.target.value)) })
  const text = (key) => h('input', { type: 'text', value: draft[key], onchange: (event) => (draft[key] = event.target.value) })
  const check = (key, label) => h('label', { class: 'check' }, h('input', { type: 'checkbox', checked: draft[key], onchange: (event) => (draft[key] = event.target.checked) }), label)

  function render() {
    if (config === null) return
    for (const key of ['engine', 'cpu_model', 'gpu_model', 'cpu_quality', 'gpu_quality', 'gpu_ttl_minutes', 'beam_size', 'language',
      'vad_filter', 'condition_on_previous_text', 'initial_prompt', 'num_speakers']) draft[key] = config[key]
    replaceChildren(root,
      h('h2', {}, t('tabConfig')),
      h('p', { class: 'muted' }, t('configHint')),
      h('div', { class: 'fields' },
        h('label', {}, t('engineField')), choose('engine', config.engines),
        h('label', {}, t('cpuModel')), choose('cpu_model', config.available_models),
        h('label', {}, t('gpuModel')), choose('gpu_model', config.available_models),
        h('label', {}, t('cpuQuality')), choose('cpu_quality', config.parakeet_qualities),
        h('label', {}, t('gpuQuality')), choose('gpu_quality', config.parakeet_qualities),
        h('label', {}, t('ttl')), number('gpu_ttl_minutes'),
        h('label', {}, t('beam')), number('beam_size'),
        h('label', {}, t('languageField')), text('language'),
        h('label', {}, t('speakers')), number('num_speakers'),
        h('label', {}, t('prompt')), text('initial_prompt'),
        h('span'), check('vad_filter', t('vad')),
        h('span'), check('condition_on_previous_text', t('previous'))),
      h('div', { class: 'actions' },
        h('button', { class: 'primary', onclick: save }, t('save')),
        h('button', { onclick: load }, t('reload'))),
    )
  }

  return { root, load, render }
}
