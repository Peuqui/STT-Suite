// The texts of the page in German and English; the language follows the browser until it is switched.
const TEXTS = {
  de: {
    language: 'English', themeLight: 'Hell', themeDark: 'Dunkel', themeAuto: 'Automatisch',
    tabStatus: 'Zustand', tabConfig: 'Konfiguration', tabVram: 'Grafikspeicher', tabTest: 'Probe',
    up: 'Nach oben', down: 'Nach unten', add: 'Hinzufügen', remove: 'Entfernen',
    reload: 'Neu laden', save: 'Speichern', saved: 'Gespeichert: {changed}.', nothingChanged: 'Nichts geändert.',
    statusHint: 'Was im Speicher liegt. Das CPU-Modell bleibt geladen; das GPU-Modell lädt eine Anfrage mit device=cuda auf die Karte mit dem meisten freien Speicher und gibt sie nach der Leerlaufzeit wieder frei.',
    device: 'Gerät', configured: 'Eingestellt', engine: 'Engine', model: 'Modell', state: 'Zustand', details: 'Einzelheiten',
    cpu: 'CPU', gpu: 'GPU', loaded: 'geladen', idle: 'leer', busy: 'arbeitet',
    card: 'Karte {index} (Worker {pid})', ttlLeft: 'Freigabe in {time}', ttlOff: 'keine automatische Freigabe',
    unloadGpu: 'GPU freigeben', unloadCpu: 'CPU freigeben', unloadAll: 'Alles freigeben', unloaded: 'Freigegeben: {what}.', nothingLoaded: 'Es war nichts geladen.',
    gpuBusy: 'Auf der GPU läuft gerade eine Transkription.', force: 'Trotzdem beenden',
    configHint: 'Die Einstellungen gelten sofort und bleiben nach einem Neustart erhalten; die Umgebungsvariablen (.env) geben nur die ersten Vorgaben. Jedes Gerät hat seine eigene Engine, jede Mischung ist möglich. Eine Anfrage kann Engine, Modell und Sprache selbst wählen.',
    groupDevices: 'Engine je Gerät', groupDevicesHint: 'whisper oder parakeet, für CPU und GPU unabhängig.',
    groupWhisperHint: 'Modellgrößen von tiny bis large-v3; die Zahl ist der freie Grafikspeicher, den eine Karte für die Größe braucht.',
    groupParakeetHint: 'Ein Modell, wählbar ist die Genauigkeit: fp32 (voll) oder int8 (kleiner, mehr Wortfehler).',
    vramHint: 'Wie viel Grafikspeicher ein Modell auf einer Karte braucht. Mit diesem Bedarf fragt der Dienst GPU-Orc nach einer Karte; GPU-Orc rechnet seinen Puffer dazu. Ein Modell, für das GPU-Orc keine Karte nennt, wird gar nicht erst geladen, die Automatik geht zum nächsten Eintrag. Schätzwerte werden durch gemessene ersetzt.',
    needMib: 'Bedarf (MiB)', source: 'Quelle', measured: 'gemessen', estimate: 'Schätzung', measureIt: 'Messen',
    burnIn: 'Burn-in', burnInHint: 'Lädt jedes angehakte Modell nacheinander auf eine GPU, lässt es die gewählte Aufnahme transkribieren und misst den belegten Grafikspeicher. Nimm eine Aufnahme von mindestens einer Minute: Parakeet arbeitet Stücke bis 60 Sekunden einzeln ab (PARAKEET_BATCH = 1), eine längere Aufnahme braucht dann nicht mehr Speicher als ein volles Stück (gemessen: 3,7 GB in voller Genauigkeit, für 1 Minute wie für 10 Minuten). Whisper braucht immer etwa dasselbe. Während der Messung gehört die GPU dem Burn-in; ein geladenes Modell wird freigegeben. Andere Programme, die währenddessen auf derselben Karte Speicher belegen, verfälschen die Zahl.',
    startBurnIn: 'Burn-in starten', measuring: 'misst {step} …', burnInDone: 'Burn-in fertig.', burnInResults: 'Ergebnis', measuredMib: 'Gemessen (MiB)', card: 'Karte', seconds: 'Dauer (s)',
    groupGeneral: 'Allgemein', needsFree: 'braucht {mib} MiB', cpuAlways: 'CPU, nimmt immer', groupAuto: 'Automatik (device=auto)', groupAutoHint: 'Die Reihenfolge, die die Automatik von oben nach unten durchgeht: der erste Eintrag, der gerade kann, transkribiert. Auf der GPU entscheidet GPU-Orc, ob eine Karte Platz für den Bedarf hat (die Zahl rechts), die CPU nimmt immer, danach kommt nichts mehr. Einträge lassen sich ausschalten, verschieben und ändern; endet die Liste ohne CPU, kann die Automatik mit 503 enden.',
    cpuEngine: 'Engine auf der CPU', gpuEngine: 'Engine auf der GPU',
    cpuModel: 'Modell auf der CPU', gpuModel: 'Modell auf der GPU', cpuQuality: 'Genauigkeit auf der CPU', gpuQuality: 'Genauigkeit auf der GPU',
    ttl: 'Leerlaufzeit des GPU-Modells (Minuten, 0 = nie)', beam: 'Beam-Größe (1–20)', languageField: 'Sprache (Kürzel, zum Beispiel de; auto = erkennen)',
    vad: 'Pausen herausfiltern (VAD)', previous: 'Vorherigen Text als Zusammenhang nutzen', prompt: 'Anfangstext (auto = eingebauter je Sprache, leer = aus, sonst wörtlich)', speakers: 'Sprecheranzahl bei Sprechererkennung (0 = erkennen)',
    testHint: 'Eine Aufnahme oder Datei transkribieren, auf dem gewählten Gerät und mit der gewählten Engine. Ein Fehler 503 heißt: die GPU hat für das Modell keinen Platz; mit Automatik geht es stufenweise bis zur CPU weiter.',
    fileField: 'Audio', chooseFile: 'Datei wählen', field_device: 'Gerät', field_engine: 'Engine', field_model: 'Whisper-Modell', field_quality: 'Parakeet-Genauigkeit', field_language: 'Sprache', field_language_probability: 'Sicherheit der Spracherkennung (0–1)', field_time: 'Dauer (s)', field_speakers: 'Sprecher', field_diarize_time: 'Dauer der Sprechererkennung (s)', field_diarize_error: 'Fehler der Sprechererkennung', record: 'Aufnehmen', stopRecording: 'Aufnahme beenden', recording: 'Aufnahme läuft …', recorded: 'Aufnahme bereit.', noMicrophone: 'Das Mikrofon ist hier nicht erreichbar (nur über HTTPS oder localhost).',
    deviceField: 'Gerät', engineField: 'Engine nur für diese Probe', device_cpu: 'CPU', device_cuda: 'GPU', device_auto: 'Automatik (GPU, kleinere Modelle, dann CPU)', asConfigured: 'wie eingestellt', modelOverride: 'Whisper-Modell nur für diese Probe', qualityOverride: 'Parakeet-Genauigkeit nur für diese Probe', fallbackUsed: 'Nicht möglich gewesen (Rückfall)', diarize: 'Sprecher unterscheiden', transcribe: 'Transkribieren', working: 'läuft …', noAudio: 'Erst eine Datei wählen oder aufnehmen.',
    transcript: 'Text', speakersText: 'Text mit Sprechern', result: 'Ergebnis',
    gpuUnavailable: 'Die GPU hat dafür keinen Platz. Wähle Automatik oder die CPU, wenn es langsamer gehen darf.',
  },
  en: {
    language: 'Deutsch', themeLight: 'Light', themeDark: 'Dark', themeAuto: 'Automatic',
    tabStatus: 'Status', tabConfig: 'Configuration', tabVram: 'Graphics memory', tabTest: 'Test',
    up: 'Move up', down: 'Move down', add: 'Add', remove: 'Remove',
    reload: 'Reload', save: 'Save', saved: 'Saved: {changed}.', nothingChanged: 'Nothing changed.',
    statusHint: 'What is in memory. The CPU model stays loaded; a request with device=cuda loads the GPU model onto the card with the most free memory and it is released after the idle time.',
    device: 'Device', configured: 'Set to', engine: 'Engine', model: 'Model', state: 'State', details: 'Details',
    cpu: 'CPU', gpu: 'GPU', loaded: 'loaded', idle: 'empty', busy: 'working',
    card: 'Card {index} (worker {pid})', ttlLeft: 'released in {time}', ttlOff: 'no automatic release',
    unloadGpu: 'Release GPU', unloadCpu: 'Release CPU', unloadAll: 'Release all', unloaded: 'Released: {what}.', nothingLoaded: 'Nothing was loaded.',
    gpuBusy: 'A transcription is running on the GPU right now.', force: 'End it anyway',
    configHint: 'The settings apply at once and survive a restart; the environment variables (.env) only give the first defaults. Each device has an engine of its own, any mix works. A request can choose engine, model and language itself.',
    groupDevices: 'Engine per device', groupDevicesHint: 'whisper or parakeet, independent for the CPU and the GPU.',
    groupWhisperHint: 'Model sizes from tiny to large-v3; the number is the free graphics memory a card needs for the size.',
    groupParakeetHint: 'One model, the precision is the choice: fp32 (full) or int8 (smaller, more word errors).',
    vramHint: 'How much graphics memory a model needs on a card. The service asks GPU-Orc for a card with this need; GPU-Orc adds its own margin. A model GPU-Orc names no card for is not loaded at all, the automatic mode goes to the next entry. Estimates are replaced by measured values.',
    needMib: 'Need (MiB)', source: 'Source', measured: 'measured', estimate: 'estimate', measureIt: 'Measure',
    burnIn: 'Burn-in', burnInHint: 'Loads each ticked model onto a GPU in turn, has it transcribe the chosen recording and measures the graphics memory it takes. Use a recording of at least one minute: Parakeet works on pieces of up to 60 seconds one at a time (PARAKEET_BATCH = 1), so a longer recording needs no more memory than one full piece (measured: 3.7 GB at full precision, for 1 minute as for 10 minutes). Whisper always needs about the same. During the measurement the GPU belongs to the burn-in; a loaded model is released. Other programs taking memory on the same card meanwhile falsify the number.',
    startBurnIn: 'Start burn-in', measuring: 'measuring {step} …', burnInDone: 'Burn-in done.', burnInResults: 'Result', measuredMib: 'Measured (MiB)', card: 'Card', seconds: 'Duration (s)',
    groupGeneral: 'General', needsFree: 'needs {mib} MiB', cpuAlways: 'CPU, always takes it', groupAuto: 'Automatic (device=auto)', groupAutoHint: 'The order the automatic mode goes through from the top: the first entry that can now transcribes. On the GPU GPU-Orc decides whether a card has room for the need (the number on the right), the CPU always takes it, nothing comes after. Entries can be switched off, moved and changed; if the list ends without the CPU, the automatic mode can end with 503.',
    cpuEngine: 'Engine on the CPU', gpuEngine: 'Engine on the GPU',
    cpuModel: 'Model on the CPU', gpuModel: 'Model on the GPU', cpuQuality: 'Precision on the CPU', gpuQuality: 'Precision on the GPU',
    ttl: 'Idle time of the GPU model (minutes, 0 = never)', beam: 'Beam size (1–20)', languageField: 'Language (code, for example en; auto = detect)',
    vad: 'Filter out pauses (VAD)', previous: 'Use the previous text as context', prompt: 'Initial text (auto = built-in per language, empty = off, otherwise verbatim)', speakers: 'Number of speakers for speaker labels (0 = detect)',
    testHint: 'Transcribe a recording or a file on the chosen device with the chosen engine. An error 503 means the GPU has no room for the model; with Automatic it goes on step by step down to the CPU.',
    fileField: 'Audio', chooseFile: 'Choose file', field_device: 'Device', field_engine: 'Engine', field_model: 'Whisper model', field_quality: 'Parakeet precision', field_language: 'Language', field_language_probability: 'Confidence of the language detection (0–1)', field_time: 'Duration (s)', field_speakers: 'Speakers', field_diarize_time: 'Duration of the speaker labels (s)', field_diarize_error: 'Speaker label error', record: 'Record', stopRecording: 'Stop recording', recording: 'Recording …', recorded: 'Recording ready.', noMicrophone: 'The microphone is not reachable here (only over HTTPS or localhost).',
    deviceField: 'Device', engineField: 'Engine for this test only', device_cpu: 'CPU', device_cuda: 'GPU', device_auto: 'Automatic (GPU, smaller models, then CPU)', asConfigured: 'as configured', modelOverride: 'Whisper model for this test only', qualityOverride: 'Parakeet precision for this test only', fallbackUsed: 'Not possible (fallback)', diarize: 'Tell speakers apart', transcribe: 'Transcribe', working: 'working …', noAudio: 'Choose a file or record first.',
    transcript: 'Text', speakersText: 'Text with speakers', result: 'Result',
    gpuUnavailable: 'The GPU has no room for it. Choose Automatic or the CPU if slower is fine.',
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
