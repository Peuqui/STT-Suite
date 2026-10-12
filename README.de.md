# STT-Suite

Spracherkennung als kleiner HTTP-Dienst in Docker, auf Basis von
[faster-whisper](https://github.com/SYSTRAN/faster-whisper) und NVIDIA
[Parakeet TDT 0.6B v3](https://huggingface.co/nvidia/parakeet-tdt-0.6b-v3). Ein laufender Container bedient alle
Programme auf dem Rechner – [AIfred Intelligence](https://github.com/Peuqui/AIfred-Intelligence)
und [Agent-Orc](https://github.com/Peuqui/Agent-Orc) nutzen ihn für die Spracheingabe.

STT-Suite ist die eine Hälfte eines Paars: Sie hört zu, [TTS-Suite](https://github.com/Peuqui/TTS-Suite)
spricht. Beide sind eigene Dienste, die jedes Programm mit wenigen HTTP-Aufrufen nutzen kann, und
keiner hängt von den Programmen ab, die ihn nutzen. Sie teilen Konventionen: ein `GET /health` bei
jedem Dienst, Fehler als JSON und eine README auf Deutsch und auf Englisch. (Der Docker-Container
behält seinen Namen `whisper-stt`, damit ein laufender nicht neu angelegt wird.)

[English version](README.md)

- **Zwei Engines, pro Anfrage gewählt:** Whisper oder Parakeet (siehe unten).
- **CPU und GPU, pro Anfrage gewählt.** Das CPU-Modell bleibt dauerhaft geladen (keine
  Ladezeit, kein VRAM). Das GPU-Modell wird bei Bedarf auf die Karte mit dem meisten freien
  VRAM geladen und nach einer einstellbaren Leerlaufzeit wieder freigegeben.
- **Kein stiller Gerätewechsel.** Hat keine GPU Platz, antwortet `/transcribe` mit `503`; der
  Aufrufer entscheidet, ob er stattdessen die CPU nimmt.
- **Modellwahl nach freiem VRAM.** Die GPU versucht das eingestellte Modell (Standard
  `large-v3`) und steigt bei knappem VRAM auf kleinere ab, aber nie unter
  `WHISPER_GPU_MIN_MODEL` (Standard `medium`; kleinere Modelle erkennen zu schlecht).
- **Optionale Sprechererkennung** mit pyannote, auf einer zweiten GPU, pro Anfrage.
- Eine Webseite unter `/` (leitet auf `/ui/`; Deutsch und Englisch, hell und dunkel, wie bei der TTS-Suite): der Zustand der
  Modelle auf CPU und GPU mit einem Knopf zum Freigeben, die Laufzeit-Konfiguration und eine Probe, die eine Datei oder eine
  Aufnahme vom Mikrofon auf dem gewählten Gerät transkribiert. Sie besteht aus HTML, CSS und JavaScript in `ui/` und nutzt
  dieselben HTTP-Aufrufe wie jeder andere Client.

## Voraussetzungen

- Docker mit Compose
- Für die GPU: eine NVIDIA-Karte und das NVIDIA Container Toolkit
- Nur für die Sprechererkennung: ein Hugging-Face-Token mit Zugriff auf die pyannote-Modelle,
  abgelegt in `~/.cache/huggingface/token` (z. B. per `huggingface-cli login`)

Die Compose-Datei bindet `~/.cache/huggingface` schreibgeschützt ein und liest das Token von
dort. Ohne Token-Datei funktioniert alles außer der Sprechererkennung wie gewohnt; das Token
lässt sich jederzeit nachtragen, danach den Container neu starten.

## Start

```
git clone https://github.com/Peuqui/STT-Suite.git && cd STT-Suite
docker compose up -d --build
curl http://localhost:5080/health
```

Ohne NVIDIA-Grafikkarte, oder ohne deren Treiber in Docker, bricht die Basisdatei mit „could not select device
driver "nvidia"“ ab: Die CPU-Datei dazunehmen, `docker compose -f docker-compose.yml -f docker-compose.cpu.yml up -d --build`
(sie nimmt die GPU-Reservierung weg).

Der Dienst hört auf Port **5080**. Die Modelle liegen im Docker-Volume `whisper_models` und
überstehen einen Neubau. Der Container startet mit Docker neu (`restart: unless-stopped`).

## API

| Methode | Pfad | Zweck |
|---|---|---|
| POST | `/transcribe` | Audiodatei transkribieren (Multipart-Formular, siehe unten) |
| GET | `/health` | Bereitschaft: `status`, `model_loaded`, geladene Modelle je Gerät |
| GET | `/status` | Ausführlicher Status und die vollständige Laufzeit-Konfiguration |
| GET/POST | `/config` | Laufzeit-Konfiguration lesen oder ändern (JSON) |
| POST | `/unload?device=cpu\|gpu\|cuda\|all` | Modelle entladen; `409`, solange eine GPU-Transkription läuft, `force=1` beendet sie trotzdem |
| GET | `/`, `/ui/` | Webseite: Zustand, Konfiguration, Probe |

`POST /transcribe` erwartet ein Multipart-Formular:

| Feld | Bedeutung |
|---|---|
| `file` | Audiodatei (WAV, MP3, M4A, OGG, FLAC, WebM) |
| `device` | `cpu` (Standard) oder `cuda` |
| `language` | Sprachcode wie `de` oder `en`; Standard aus der Konfiguration |
| `diarize` | `1`, um Sprecher zu kennzeichnen (Standard: aus) |
| `num_speakers` | Optionaler Hinweis, wenn die Sprecherzahl bekannt ist |
| `engine` | `whisper` oder `parakeet`; Standard `STT_ENGINE` |
| `quality` | nur Parakeet: `fp32` oder `int8`; Standard `STT_CPU_QUALITY` bzw. `STT_GPU_QUALITY`, je nach Gerät |

Die Antwort ist JSON mit `text`, der benötigten Zeit und dem verwendeten Gerät. Fehler: `400`
bei fehlender Datei oder ungültigem Gerät, `503`, wenn keine GPU Platz für ein Modell hat,
sonst `500`.

```
curl -F file=@sprache.webm -F device=cuda http://localhost:5080/transcribe
```

## Konfiguration

In `docker-compose.yml` oder als Umgebungsvariablen beim Start von Compose:

| Variable | Standard | Bedeutung |
|---|---|---|
| `WHISPER_MODEL` | `medium` | Modell auf der CPU (und GPU-Standard, wenn `WHISPER_GPU_MODEL` fehlt) |
| `WHISPER_CPU_MODEL` | `WHISPER_MODEL` | Modell auf der CPU |
| `WHISPER_GPU_MODEL` | `large-v3` | Erste Wahl auf der GPU |
| `WHISPER_GPU_MIN_MODEL` | `medium` | Kleinstes Modell, auf das die GPU absteigt |
| `WHISPER_GPU_TTL_MINUTES` | `30` | Leerlauf-Minuten, bis der GPU-Worker endet und sein VRAM frei wird; `0` behält ihn |
| `WHISPER_LANGUAGE` | `de` | Standardsprache (`auto` erkennt sie) |
| `WHISPER_EAGER_LOAD` | `1` | CPU-Modell beim Start laden |
| `STT_CONFIG_FILE` | `/state/config.json` | Ablage der über `/config` geänderten Einstellungen (Volume `whisper_state`). Die Umgebung liefert nur die ersten Standardwerte; sobald die Datei existiert, gilt sie. Zum Zurücksetzen löschen |
| `WHISPER_CPU_COMPUTE` / `WHISPER_GPU_COMPUTE` | `int8` / `float16` | Rechengenauigkeit |
| `DIARIZE_MODEL` | `pyannote/speaker-diarization-community-1` | Pipeline der Sprechererkennung |
| `STT_ENGINE` | `whisper` | Engine, wenn eine Anfrage keine nennt: `whisper` oder `parakeet` |
| `STT_CPU_QUALITY` / `STT_GPU_QUALITY` | `fp32` / `fp32` | Parakeet-Qualität auf CPU / GPU, wenn eine Anfrage keine nennt: `fp32` oder `int8` |
| `PARAKEET_CHUNK_S` / `PARAKEET_MERGE_SILENCE_MS` | `60` / `5000` | Parakeet schneidet das Audio an Sprechpausen in Stücke von höchstens dieser Länge und fasst kürzere Pausen zusammen |
| `PARAKEET_MIN_VRAM_MIB` | `4500` | Freier VRAM, den eine Karte für Parakeet braucht |

Verfügbare Modelle: `tiny`, `base`, `small`, `medium`, `large-v3`.

## Welche Engine?

Gemessen an vier einminütigen Ausschnitten deutscher Hörbücher (verschiedene Sprecher);
Sekunden pro Minute Audio, weniger ist besser:

| Engine | Gerät | Zeit (s) |
|---|---|---|
| Parakeet fp32 | GPU (V100) | 0,25 |
| Whisper large-v3 | GPU | 2,0–3,7 |
| Parakeet int8 | CPU | 2,5–3,8 |
| Parakeet fp32 | CPU | 2,8–5,1 |
| Whisper medium | CPU | 12,5–22,7 |

Qualität, von Hand gelesen: Whisper large-v3 ist bei Namen und Grammatik am besten, hat aber
in einem Ausschnitt (wie auch Whisper medium) den halben Text ausgelassen. Parakeet fp32 kam
large-v3 nahe und hat nie etwas ausgelassen; int8 macht mehr Wortfehler (etwa auf dem Niveau
von Whisper medium). Parakeet kann 25 europäische Sprachen und erkennt die Sprache selbst —
für andere Sprachen Whisper nehmen.

Parakeet braucht kein Hugging-Face-Token; das Modell (≈3 GB, beide Qualitäten) wird beim
ersten Gebrauch ins Modell-Volume geladen.

## Sicherheit

Der Dienst hat keine Anmeldung, und `docker-compose.yml` veröffentlicht Port 5080 auf allen Adressen
des Rechners. Halte ihn hinter einer Firewall oder veröffentliche ihn nur auf der Loopback-Adresse
(`"127.0.0.1:5080:5052"`), wenn nur Programme auf demselben Rechner ihn nutzen.

## Lizenz

[PolyForm Noncommercial 1.0.0](LICENSE)
