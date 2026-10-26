# STT-Suite

Speech-to-text as a small HTTP service in Docker, built on
[faster-whisper](https://github.com/SYSTRAN/faster-whisper) and NVIDIA
[Parakeet TDT 0.6B v3](https://huggingface.co/nvidia/parakeet-tdt-0.6b-v3). One running container serves
every client on the machine — [AIfred Intelligence](https://github.com/Peuqui/AIfred-Intelligence)
and [Agent-Orc](https://github.com/Peuqui/Agent-Orc) use it for voice input.

STT-Suite is one half of a pair: it listens, [TTS-Suite](https://github.com/Peuqui/TTS-Suite)
speaks. Each is a service of its own that any program can use through a few HTTP calls, and
neither depends on the programs that use it. They share conventions: a `GET /health` on every
service, errors as JSON, and a README in German and in English. (The Docker container is called
`stt-suite`.)

[Deutsche Version](README.de.md)

- **Two engines, any mix per device:** Whisper (a size from `tiny` to `large-v3`) or Parakeet (fp32 or
  int8), set for the CPU and for the GPU independently: Parakeet int8 on the CPU and Whisper large-v3 on
  the GPU is as possible as the other way round. A request can override engine, model and precision.
- **CPU and GPU, chosen per request.** The CPU model stays loaded all the time (no load time,
  no VRAM). The GPU model is loaded on demand into the card with the most free VRAM and
  released again after a configurable idle time.
- **Exactly the model you chose.** `device=cuda` loads the configured GPU model or answers `503` when no
  card has the free VRAM it needs (`vram_mib.json`); nothing is substituted silently.
- **`device=auto` for a fallback you ask for:** it goes through a list you set (`auto_chain` in the
  configuration, on the web page under Configuration: entries of device, engine and model that can be
  moved, switched off, changed and added; at first the configured GPU model and then the configured CPU
  model). The first entry that can takes the request. Which one can is decided from the table of VRAM needs
  and one `nvidia-smi` query, without loading anything on trial, and before a model in memory is let go: a
  model that cannot be loaded never costs the one that is. The answer names what transcribed and lists in
  `fallback` what could not and why.
- **A table of VRAM needs you measure.** The shipped values are estimates. The web page (tab Graphics memory)
  runs a burn-in: it loads each chosen model onto a GPU in turn, transcribes a recording and takes the highest
  rise of the used memory (use a recording typical of your use: Parakeet needs about 3.7 GB for a 10 second dictation but
  about 6.3 GB for a recording of several minutes, which it works on in pieces of up to 60 seconds); the result replaces the estimate and is kept in the state volume. A margin
  (`vram_margin_mib`, default 256 MiB) is added to every need and kept free on the card; make it smaller when a
  model just no longer fits, or let the list go on to the next smaller one.
- **Optional speaker diarization** with pyannote, on a second GPU, per request.
- A web page at `/` (it goes to `/ui/`; German and English, light and dark, like the TTS-Suite's): the status of the
  models on the CPU and the GPU with a button to release each, the runtime configuration, and a test that
  transcribes a file or a recording from the microphone on the chosen device. It is plain HTML, CSS and JavaScript in
  `ui/` and uses the same HTTP calls as any other client.

## Requirements

- Docker with Compose
- For GPU use: an NVIDIA GPU and the NVIDIA Container Toolkit
- For speaker diarization only: a Hugging Face token with access to the pyannote models,
  stored in `~/.cache/huggingface/token` (for example via `huggingface-cli login`)

The compose file mounts `~/.cache/huggingface` read-only and reads the token from there.
Without a token file everything except diarization works as usual; add the token any time
and restart the container.

## Start

```
git clone https://github.com/Peuqui/STT-Suite.git && cd STT-Suite
docker compose up -d --build
curl http://localhost:5080/health
```

Without an NVIDIA GPU, or without its driver in Docker, the base file stops with “could not select device
driver "nvidia"”: add the CPU file, `docker compose -f docker-compose.yml -f docker-compose.cpu.yml up -d --build`
(it takes the GPU reservation away).

The service listens on port **5080**. Models are cached in the Docker volume `stt_suite_models`
and survive rebuilds. The container restarts with Docker (`restart: unless-stopped`).

## API

| Method | Path | Purpose |
|---|---|---|
| POST | `/transcribe` | Transcribe an audio file (multipart form, see below) |
| GET | `/health` | Readiness: `status`, `model_loaded`, loaded models per device |
| GET | `/status` | Detailed status and the full runtime configuration |
| GET/POST | `/config` | Read or change the runtime configuration (JSON) |
| POST | `/unload` | Unload the CPU models (the GPU workers: `/gpu/release`) |
| GET | `/gpu/holders` | The GPU workers that run: `item`, `gpu_uuids` (the card it was started on), `memory_mib` (what the GPU worker took when loading), `busy`, `need_mib` (what it needs on its card, as in `/gpu/needs`) |
| GET | `/gpu/needs` | What each model needs on a card, for GPU-Orc's plan: `item` (e.g. `parakeet-fp32`, `whisper-large-v3`, `diarize`), `need_mib` (the bare need without this service's margin), `source` (`measured` = burn-in, `estimate`), `default` (the model the GPU loads when a request names none) |
| POST | `/gpu/release` | JSON `{"items": ["gpu-worker", "diarize-worker"] \| "all", "force": false, "reason": "…"}`. One result per item: `released` (the process is proven ended), `not_running`, `busy` (left alone without force), `failed`. `200`, `409` when one is busy, `500` when one failed. With `force` a running transcription is told the reason and counts as "GPU unavailable": with `device=auto` it goes on on the CPU, with `device=cuda` it answers `503` with the reason. The common release API of the suites, used by [GPU-Orc](https://github.com/Peuqui/GPU-Orc) |
| POST, GET | `/burn-in` | `POST` (form: `file`, optional `entries`, a JSON list of `{"engine", "model"}`) starts the measurement of the VRAM models need, in the background; `GET` shows how it stands (`running`, `step`, `results`, `error`). `409` while one runs or the GPU transcribes |
| GET | `/`, `/ui/` | Web page: status, configuration, test |

`POST /transcribe` takes a multipart form:

| Field | Meaning |
|---|---|
| `file` | Audio file (WAV, MP3, M4A, OGG, FLAC, WebM) |
| `device` | `cpu` (default), `cuda`, or `auto` (the fallback list above) |
| `language` | Language code such as `de` or `en`; default from the configuration |
| `diarize` | `1` to label speakers (default off) |
| `num_speakers` | Optional hint when the number of speakers is known |
| `engine` | `whisper` or `parakeet`; default the engine configured for the device (`cpu_engine`, `gpu_engine`) |
| `model` | Whisper only: a size such as `medium`; default `cpu_model` or `gpu_model`, by device |
| `quality` | Parakeet only: `fp32` or `int8`; default `cpu_quality` or `gpu_quality`, by device |

The answer is JSON with `text`, the time taken, the device and the engine used and its model (`model` for
Whisper, `quality` for Parakeet); after a fallback also `fallback`, the list of what could not take the
request, each with `tried` and `reason`. Errors: `400` for a missing file or an invalid value, `503` when
the GPU has no room for the model (or is busy with another one), `500` otherwise.

```
curl -F file=@speech.webm -F device=cuda http://localhost:5080/transcribe
```

## Updating from before 12 October 2026

Engine, model and precision are now set per device: `STT_ENGINE` became `STT_CPU_ENGINE` and `STT_GPU_ENGINE`, and
`WHISPER_GPU_MIN_MODEL` is gone (the GPU no longer steps down by itself; `device=auto` and its list do that when you
ask). Rename the variable in your `.env`. The settings of the whole service lost their `WHISPER_` prefix too: `WHISPER_GPU_TTL_MINUTES`, `WHISPER_LANGUAGE`, `WHISPER_EAGER_LOAD`, `WHISPER_GPU_RESULT_TIMEOUT_S` and `WHISPER_NUM_SPEAKERS` are now `STT_GPU_TTL_MINUTES`, `STT_LANGUAGE`, `STT_EAGER_LOAD`, `STT_GPU_RESULT_TIMEOUT_S` and `STT_NUM_SPEAKERS` (those that only concern the Whisper engine, such as `WHISPER_MODEL`, keep their names). Settings already saved in the state volume (`/state/config.json`) hold the old
key `engine`, which the service refuses at start: replace it by `cpu_engine` and `gpu_engine` with the same value, or
delete the file to return to the `.env`. The container, the image and the volumes were renamed as well: `whisper-stt` became `stt-suite`, `whisper_models` became
`stt_suite_models` and `whisper_state` became `stt_suite_state`. Remove the old container (`docker rm -f whisper-stt`), start the
new one once (`docker compose up --no-start`, which makes the new volumes) and copy the contents of the two old volumes into
them (for example `docker run --rm -v whisper_models:/from:ro -v stt_suite_models:/to --entrypoint cp stt-suite -a /from/. /to/`),
or the models are downloaded again and the settings start from the `.env`. A fresh installation has nothing to change.

## Configuration

Set in `docker-compose.yml` or as environment variables when starting compose:

Tests: `python3 -m venv .venv && .venv/bin/pip install -r requirements-test.txt && .venv/bin/python -m pytest tests`.

| Variable | Default | Meaning |
|---|---|---|
| `WHISPER_MODEL` | `medium` | Model on the CPU (and the GPU default when `WHISPER_GPU_MODEL` is unset) |
| `WHISPER_CPU_MODEL` | `WHISPER_MODEL` | Model on the CPU |
| `WHISPER_GPU_MODEL` | `large-v3` | Whisper size on the GPU |
| `STT_GPU_TTL_MINUTES` | `30` | Idle minutes until the GPU worker is stopped and its VRAM freed; `0` keeps it |
| `STT_GPU_PLACEMENT_LOCK` | unset | Lock file (inside the container, e.g. `/gpu-placement/placement.lock`, in the directory `STT_GPU_PLACEMENT_DIR` of the host mounted there) that this service and the TTS-Suite lock with `flock` while they choose a card and load onto it. Two services starting together then do not pick the same card. Unset = off; the file must exist and be writable |
| `STT_GPU_ORC_URL` / `STT_GPU_ORC_TOKEN_FILE` / `STT_GPU_ORC_DIR` | unset | The GPU-Orc of this machine (from the container `http://host.docker.internal:5070`), its token file inside the container (e.g. `/gpu-orc/token`) and the host directory mounted there read-only (e.g. `~/.config/gpu-orc`). Its `available_mib` per card caps the free VRAM: a vLLM model may fill its share of a card while it computes, nvidia-smi shows it as free in between. If GPU-Orc does not answer or cannot say, the GPU takes no model (`auto` goes on, e.g. to the CPU). Unset = no GPU-Orc, nvidia-smi alone |
| `STT_LANGUAGE` | `de` | Default language (`auto` detects it) |
| `STT_EAGER_LOAD` | `1` | Load the CPU model at start |
| `STT_CONFIG_FILE` | `/state/config.json` | Where settings changed via `/config` are kept (volume `stt_suite_state`). The environment only gives the first defaults; once this file exists it wins. Delete it to return to the environment |
| `WHISPER_CPU_COMPUTE` / `WHISPER_GPU_COMPUTE` | `int8` / `float16` | Compute types |
| `DIARIZE_MODEL` | `pyannote/speaker-diarization-community-1` | Diarization pipeline |
| `STT_CPU_ENGINE` / `STT_GPU_ENGINE` | `whisper` / `whisper` | Engine on the CPU / the GPU when a request names none: `whisper` or `parakeet` |
| `STT_CPU_QUALITY` / `STT_GPU_QUALITY` | `fp32` / `fp32` | Parakeet precision on the CPU / the GPU when a request names none: `fp32` or `int8` |
| `PARAKEET_CHUNK_S` / `PARAKEET_MERGE_SILENCE_MS` | `60` / `5000` | Parakeet cuts audio at speech pauses into chunks of at most this length, merging pauses shorter than this |
| `PARAKEET_BATCH` | `1` | How many chunks Parakeet recognizes at once. At 1 a long recording needs no more VRAM than one chunk, and the burn-in (with a recording of at least `PARAKEET_CHUNK_S`) measures exactly that bound; above 1 the need grows with the length, up to that multiple |
| `STT_VRAM_FILE` | `vram_mib.json` | Free VRAM (MiB) a card needs before a model is loaded on it, per Whisper size and Parakeet precision: estimates, replaced by what a burn-in measured |
| `STT_VRAM_MEASURED_FILE` | `/state/vram_measured.json` | Where the measured needs are kept (volume `stt_suite_state`); delete it to return to the estimates |
| `STT_VRAM_MARGIN_MIB` | `256` | Free VRAM kept on a card beyond what a model needs; also set on the web page |

Available models: `tiny`, `base`, `small`, `medium`, `large-v3`.

## Choosing an engine

Measured on four one-minute excerpts of German audiobooks (different narrators); seconds per
minute of audio, lower is better:

| Engine | Device | Time (s) |
|---|---|---|
| Parakeet fp32 | GPU (V100) | 0.25 |
| Whisper large-v3 | GPU | 2.0–3.7 |
| Parakeet int8 | CPU | 2.5–3.8 |
| Parakeet fp32 | CPU | 2.8–5.1 |
| Whisper medium | CPU | 12.5–22.7 |

Quality, read by hand: Whisper large-v3 is best on names and grammar, but on one excerpt it
(and Whisper medium) dropped half the text. Parakeet fp32 came close to large-v3 and never
dropped anything; int8 makes more word errors (about Whisper medium's level). Parakeet covers
25 European languages and detects the language itself — for other languages use Whisper.

Parakeet needs no Hugging Face token; its model (≈3 GB, both qualities) is downloaded into
the model volume on first use.

## Security

The service has no login, and `docker-compose.yml` publishes port 5080 on every address of the
machine. Keep it behind a firewall, or publish it on the loopback address only
(`"127.0.0.1:5080:5052"`) when only programs on the same machine use it.

## Star History

![Star History](https://raw.githubusercontent.com/Peuqui/Traffic/main/data/STT-Suite/star-history.svg)

<sub>Recorded daily in [Peuqui/Traffic](https://github.com/Peuqui/Traffic), together with views and clones. GitHub restricted the stargazer API to repo admins on 2026-06-30, so external chart services now need a token with write access.</sub>

## License

[PolyForm Noncommercial 1.0.0](LICENSE)
