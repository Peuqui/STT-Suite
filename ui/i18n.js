// The texts of the page in German and English; the language follows the browser until it is switched.
const TEXTS = {
  de: {
    language: 'English', themeLight: 'Hell', themeDark: 'Dunkel', themeAuto: 'Automatisch',
    tabStatus: 'Zustand', tabConfig: 'Konfiguration', tabTest: 'Probe',
    reload: 'Neu laden', save: 'Speichern', saved: 'Gespeichert: {changed}.', nothingChanged: 'Nichts geändert.',
    statusHint: 'Was im Speicher liegt. Das CPU-Modell bleibt geladen; das GPU-Modell lädt eine Anfrage mit device=cuda auf die Karte mit dem meisten freien Speicher und gibt sie nach der Leerlaufzeit wieder frei.',
    device: 'Gerät', engine: 'Engine', model: 'Modell', state: 'Zustand', details: 'Einzelheiten',
    cpu: 'CPU', gpu: 'GPU', loaded: 'geladen', idle: 'leer', busy: 'arbeitet',
    card: 'Karte {index} (Worker {pid})', ttlLeft: 'Freigabe in {time}', ttlOff: 'keine automatische Freigabe',
    unloadGpu: 'GPU freigeben', unloadCpu: 'CPU freigeben', unloadAll: 'Alles freigeben', unloaded: 'Freigegeben: {what}.', nothingLoaded: 'Es war nichts geladen.',
    gpuBusy: 'Auf der GPU läuft gerade eine Transkription.', force: 'Trotzdem beenden',
    configHint: 'Die Einstellungen gelten sofort und bleiben nach einem Neustart erhalten; die Umgebungsvariablen (.env) geben nur die ersten Vorgaben. Eine Anfrage kann Engine, Gerät und Sprache selbst wählen.',
    engineField: 'Engine', cpuModel: 'Whisper-Modell auf der CPU', gpuModel: 'Whisper-Modell auf der GPU', cpuQuality: 'Parakeet-Genauigkeit auf der CPU', gpuQuality: 'Parakeet-Genauigkeit auf der GPU',
    ttl: 'Leerlaufzeit des GPU-Modells (Minuten, 0 = nie)', beam: 'Beam-Größe (1–20)', languageField: 'Sprache (Kürzel, zum Beispiel de; auto = erkennen)',
    vad: 'Pausen herausfiltern (VAD)', previous: 'Vorherigen Text als Zusammenhang nutzen', prompt: 'Anfangstext (auto = eingebauter je Sprache, leer = aus, sonst wörtlich)', speakers: 'Sprecheranzahl bei Sprechererkennung (0 = erkennen)',
    testHint: 'Eine Aufnahme oder Datei transkribieren, auf dem gewählten Gerät und mit der gewählten Engine. Ein Fehler 503 heißt: keine GPU hat Platz; die CPU wählt man selbst.',
    fileField: 'Audio', chooseFile: 'Datei wählen', field_device: 'Gerät', field_language: 'Sprache', field_language_probability: 'Sicherheit der Spracherkennung (0–1)', field_time: 'Dauer (s)', field_speakers: 'Sprecher', field_diarize_time: 'Dauer der Sprechererkennung (s)', field_diarize_error: 'Fehler der Sprechererkennung', record: 'Aufnehmen', stopRecording: 'Aufnahme beenden', recording: 'Aufnahme läuft …', recorded: 'Aufnahme bereit.', noMicrophone: 'Das Mikrofon ist hier nicht erreichbar (nur über HTTPS oder localhost).',
    deviceField: 'Gerät', engineDefault: 'Vorgabe ({engine})', diarize: 'Sprecher unterscheiden', transcribe: 'Transkribieren', working: 'läuft …', noAudio: 'Erst eine Datei wählen oder aufnehmen.',
    transcript: 'Text', speakersText: 'Text mit Sprechern', result: 'Ergebnis',
    gpuUnavailable: 'Keine GPU hat Platz. Wähle die CPU, wenn es langsamer gehen darf.',
  },
  en: {
    language: 'Deutsch', themeLight: 'Light', themeDark: 'Dark', themeAuto: 'Automatic',
    tabStatus: 'Status', tabConfig: 'Configuration', tabTest: 'Test',
    reload: 'Reload', save: 'Save', saved: 'Saved: {changed}.', nothingChanged: 'Nothing changed.',
    statusHint: 'What is in memory. The CPU model stays loaded; a request with device=cuda loads the GPU model onto the card with the most free memory and it is released after the idle time.',
    device: 'Device', engine: 'Engine', model: 'Model', state: 'State', details: 'Details',
    cpu: 'CPU', gpu: 'GPU', loaded: 'loaded', idle: 'empty', busy: 'working',
    card: 'Card {index} (worker {pid})', ttlLeft: 'released in {time}', ttlOff: 'no automatic release',
    unloadGpu: 'Release GPU', unloadCpu: 'Release CPU', unloadAll: 'Release all', unloaded: 'Released: {what}.', nothingLoaded: 'Nothing was loaded.',
    gpuBusy: 'A transcription is running on the GPU right now.', force: 'End it anyway',
    configHint: 'The settings apply at once and survive a restart; the environment variables (.env) only give the first defaults. A request can choose engine, device and language itself.',
    engineField: 'Engine', cpuModel: 'Whisper model on the CPU', gpuModel: 'Whisper model on the GPU', cpuQuality: 'Parakeet precision on the CPU', gpuQuality: 'Parakeet precision on the GPU',
    ttl: 'Idle time of the GPU model (minutes, 0 = never)', beam: 'Beam size (1–20)', languageField: 'Language (code, for example en; auto = detect)',
    vad: 'Filter out pauses (VAD)', previous: 'Use the previous text as context', prompt: 'Initial text (auto = built-in per language, empty = off, otherwise verbatim)', speakers: 'Number of speakers for speaker labels (0 = detect)',
    testHint: 'Transcribe a recording or a file on the chosen device with the chosen engine. An error 503 means no GPU has room; the CPU is yours to choose.',
    fileField: 'Audio', chooseFile: 'Choose file', field_device: 'Device', field_language: 'Language', field_language_probability: 'Confidence of the language detection (0–1)', field_time: 'Duration (s)', field_speakers: 'Speakers', field_diarize_time: 'Duration of the speaker labels (s)', field_diarize_error: 'Speaker label error', record: 'Record', stopRecording: 'Stop recording', recording: 'Recording …', recorded: 'Recording ready.', noMicrophone: 'The microphone is not reachable here (only over HTTPS or localhost).',
    deviceField: 'Device', engineDefault: 'Default ({engine})', diarize: 'Tell speakers apart', transcribe: 'Transcribe', working: 'working …', noAudio: 'Choose a file or record first.',
    transcript: 'Text', speakersText: 'Text with speakers', result: 'Result',
    gpuUnavailable: 'No GPU has room. Choose the CPU if slower is fine.',
  },
}

const STORAGE_KEY = 'stt-suite-language'
let language = localStorage.getItem(STORAGE_KEY) ?? (navigator.language.startsWith('de') ? 'de' : 'en')
const listeners = []

export function currentLanguage() { return language }
export function onLanguageChange(listener) { listeners.push(listener) }
export function toggleLanguage() {
  language = language === 'de' ? 'en' : 'de'
  localStorage.setItem(STORAGE_KEY, language)
  document.documentElement.lang = language
  listeners.forEach((listener) => listener())
}
export function t(key, values = {}) {
  const text = TEXTS[language][key] ?? TEXTS.en[key] ?? key
  return text.replace(/\{(\w+)\}/g, (_, name) => String(values[name] ?? ''))
}
