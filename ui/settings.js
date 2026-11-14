// The one place for the numbers and defaults of this page.

// How long a message stays before it fades (an error longer), in ms.
export const NOTICE_MS = { ok: 4000, error: 10000 }
// How often the catalog asks again while it is shown: the idle countdown and a worker that starts or ends stay current (ms).
export const REFRESH_MS = 5000
// How often the page asks for the progress of a running burn-in (ms).
export const BURN_IN_POLL_MS = 1500
// Who asks, in the log of the service and in what a forced release tells a running transcription.
export const RELEASE_REASON = 'STT-Suite page'
// The device a test transcribes on until another is chosen.
export const TEST_DEVICE = 'cpu'
