import { service } from './api.js'
import { dictationMic, loadBadge } from './dictation-mic.js'
import { h, replaceChildren } from './dom.js'
import { formatNumber } from './format.js'
import { t } from './i18n.js'
import { TEST_DEVICE } from './settings.js'

// Transcribes a file or a recording from the microphone with the chosen device and engine.
export function testView({ show, fail }) {
  const root = h('div')
  let config = null
  let audio = null // { blob, name }
  let recorder = null
  let running = false
  let result = null
  let gpuLoaded = false
  let statusTimer
  const choice = { device: TEST_DEVICE, engine: '', model: '', quality: '', language: '', diarize: false }

  async function load() {
    try {
      await loadBadge()
      config = await service.config()
      await refreshStatus()
      render()
    } catch (error) { fail(error) }
  }

  // Whether the service has a model on the GPU (the mark at the microphone). Its worker ends itself after the idle
  // time, so the page asks again then; otherwise the mark would outlive the model.
  async function refreshStatus() {
    clearTimeout(statusTimer)
    const status = await service.status()
    gpuLoaded = status.gpu_loaded
    if (gpuLoaded && status.gpu_ttl_remaining_seconds !== undefined) {
      statusTimer = setTimeout(() => refreshStatus().then(render).catch(fail), (status.gpu_ttl_remaining_seconds + 2) * 1000)
    }
  }

  async function startRecording() {
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true })
      const chunks = []
      recorder = new MediaRecorder(stream)
      recorder.ondataavailable = (event) => chunks.push(event.data)
      recorder.onstop = () => {
        stream.getTracks().forEach((track) => track.stop())
        audio = { blob: new Blob(chunks, { type: recorder.mimeType }), name: 'recording.webm' }
        recorder = null
        show(t('recorded'))
        render()
      }
      recorder.start()
      render()
    } catch (error) {
      fail(navigator.mediaDevices ? error : new Error(t('noMicrophone')))
    }
  }

  async function transcribe() {
    if (audio === null) return fail(new Error(t('noAudio')))
    const form = new FormData()
    form.append('file', audio.blob, audio.name)
    form.append('device', choice.device)
    for (const key of ['engine', 'model', 'quality']) if (choice[key]) form.append(key, choice[key])
    if (choice.language) form.append('language', choice.language)
    if (choice.diarize) form.append('diarize', '1')
    running = true
    result = null
    render()
    try {
      result = await service.transcribe(form)
    } catch (error) {
      fail(error.status === 503 ? new Error(`${error.message} — ${t('gpuUnavailable')}`) : error)
    }
    running = false
    await refreshStatus().catch(fail)
    render()
  }

  // What the service sends besides the text: a name of its own when the page has one, else the key as sent.
  const fieldLabel = (key) => (t(`field_${key}`) === `field_${key}` ? key : t(`field_${key}`))

  // A choice for this request only; empty keeps what the service is set to.
  function overrideSelect(key, options) {
    return h('select', { onchange: (event) => (choice[key] = event.target.value) },
      h('option', { value: '', selected: choice[key] === '' }, t('asConfigured')),
      options.map((option) => h('option', { value: option, selected: option === choice[key] }, option)))
  }

  function resultView() {
    if (result === null) return null
    const { text, text_speakers: withSpeakers, fallback, ...rest } = result
    return h('div', {},
      h('h3', {}, t('transcript')), h('div', { class: 'transcript' }, text ?? ''),
      withSpeakers ? [h('h3', {}, t('speakersText')), h('div', { class: 'transcript' }, withSpeakers)] : null,
      fallback ? [h('h3', {}, t('fallbackUsed')), h('ul', {}, fallback.map((entry) => h('li', {}, `${entry.tried}: ${entry.reason}`)))] : null,
      h('h3', {}, t('result')),
      h('div', { class: 'fields' }, Object.entries(rest).flatMap(([key, value]) => [h('label', {}, fieldLabel(key)), h('code', {}, typeof value === 'object' ? JSON.stringify(value) : typeof value === 'number' ? formatNumber(value) : String(value))])))
  }

  function render() {
    if (config === null) return
    replaceChildren(root,
      h('h2', {}, t('tabTest')),
      h('p', { class: 'muted' }, t('testHint')),
      h('div', { class: 'fields' },
        h('label', {}, t('fileField')),
        h('div', { class: 'row' },
          // The page is drawn again after a choice, which would empty a visible file field: the field stays hidden
          // and the file's name is shown next to the buttons.
          h('input', { type: 'file', id: 'audio-file', hidden: true, accept: 'audio/*,video/*', onchange: (event) => { const file = event.target.files[0]; audio = file ? { blob: file, name: file.name } : audio; render() } }),
          h('button', { onclick: () => document.getElementById('audio-file').click() }, t('chooseFile')),
          dictationMic({
            phase: recorder !== null ? 'listening' : running ? 'transcribing' : 'ready',
            gpu: gpuLoaded && choice.device !== 'cpu',
            title: t(recorder === null ? 'record' : 'stopRecording'),
            gpuTitle: t('micOnGpu'),
            workingTitle: t('working'),
            disabled: running,
            onclick: recorder === null ? startRecording : () => recorder.stop(),
          }),
          audio !== null && recorder === null ? h('span', { class: 'muted' }, audio.name) : null),
        h('label', {}, t('deviceField')),
        // Drawn again, because the GPU mark at the microphone depends on it.
        h('select', { onchange: (event) => { choice.device = event.target.value; render() } },
          ['cpu', 'cuda', 'auto'].map((device) => h('option', { value: device, selected: device === choice.device }, t(`device_${device}`)))),
        h('label', {}, t('engineField')), overrideSelect('engine', config.engines),
        h('label', {}, t('modelOverride')), overrideSelect('model', config.available_models),
        h('label', {}, t('qualityOverride')), overrideSelect('quality', config.parakeet_qualities),
        h('label', {}, t('languageField')),
        h('input', { type: 'text', value: choice.language, placeholder: config.language, onchange: (event) => (choice.language = event.target.value.trim()) }),
        h('span'), h('label', { class: 'check' }, h('input', { type: 'checkbox', checked: choice.diarize, onchange: (event) => (choice.diarize = event.target.checked) }), t('diarize'))),
      h('div', { class: 'actions' }, h('button', { class: 'primary', onclick: transcribe, disabled: running }, running ? t('working') : t('transcribe'))),
      resultView(),
    )
  }

  return { root, load, render }
}
