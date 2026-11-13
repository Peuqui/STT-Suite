import { h } from './dom.js'

// The dictation button of the page: the badge, its GPU mark and its spinner from badge/ (the one source for the
// shape and the three phases, also for the programs that use the STT-Suite; see badge/badge.css).
let badgeTemplate = null

// The shape is a file of its own, which has to sit inside the page to be coloured by badge.css.
export async function loadBadge() {
  if (badgeTemplate !== null) return
  const response = await fetch('badge/badge.svg')
  if (!response.ok) throw new Error(`badge/badge.svg: ${response.status}`)
  badgeTemplate = new DOMParser().parseFromString(await response.text(), 'image/svg+xml').documentElement
}

// `phase`: 'ready', 'listening' (speech is taken in) or 'transcribing'; `gpu`: whether the mark is shown.
export function dictationMic({ phase, gpu, title, gpuTitle, workingTitle, disabled, onclick }) {
  const badge = badgeTemplate.cloneNode(true)
  badge.classList.add(phase)
  return h('span', { class: 'dictation-mic' },
    h('button', { type: 'button', title, 'aria-label': title, disabled, onclick }, badge),
    gpu ? h('span', { class: `dictation-gpu ${phase}`, title: gpuTitle }, 'GPU') : null,
    phase === 'transcribing' ? h('span', { class: 'dictation-spinner', role: 'status', 'aria-label': workingTitle, title: workingTitle }) : null)
}
