"""
STT-Suite — speech to text as an HTTP service, with the engines faster-whisper (Whisper) and Parakeet.

Supports dual-device operation:
- CPU: runs in main process (permanent, no VRAM)
- GPU: runs in a separate child process (killed after TTL to fully release
  CUDA context + VRAM, enabling GPU P8 power state)

Device is selected per request. Model and parameters are changeable at
runtime via the Web-UI or /config endpoint.

Endpoints:
    GET  /          — Web-UI (status, model management, settings)
    GET  /health    — Health check (model status, device info)
    POST /transcribe — Transcribe audio file (multipart/form-data)
    GET  /status    — Detailed status (model info, memory usage)
    POST /unload    — Unload model(s) to free memory
    GET  /config    — Get current configuration
    POST /config    — Update configuration (JSON body)
"""

from __future__ import annotations

import contextlib
import fcntl
import gc
import json
import multiprocessing
import os
import queue
import tempfile
import threading
import time
from pathlib import Path

from flask import Flask, jsonify, redirect, request, send_from_directory

app = Flask(__name__)

# ── Configuration (mutable at runtime via /config) ───────────

AVAILABLE_MODELS = ["tiny", "base", "small", "medium", "large-v3"]
# Engines per request: Whisper (faster-whisper) or NVIDIA Parakeet TDT 0.6B v3
# (onnx-asr). Parakeet quality: fp32 (full) or int8 (smaller, more word errors).
ENGINES = ["whisper", "parakeet"]
PARAKEET_QUALITIES = ["fp32", "int8"]

_default_model = os.environ.get("WHISPER_MODEL", "medium")

_config = {
    "cpu_model": os.environ.get("WHISPER_CPU_MODEL", _default_model),
    "gpu_model": os.environ.get("WHISPER_GPU_MODEL", _default_model),
    "cpu_compute": os.environ.get("WHISPER_CPU_COMPUTE", "int8"),
    "gpu_compute": os.environ.get("WHISPER_GPU_COMPUTE", "float16"),
    "gpu_ttl_minutes": int(os.environ.get("STT_GPU_TTL_MINUTES", "30")),
    "language": os.environ.get("STT_LANGUAGE", "de"),
    "vad_filter": os.environ.get("WHISPER_VAD_FILTER", "1") in ("1", "true"),
    "beam_size": int(os.environ.get("WHISPER_BEAM_SIZE", "5")),
    # Carries the transcript so far into the next 30 s window. Without it the
    # initial_prompt below only styles the FIRST window and everything after
    # falls back to unpunctuated staccato (measured on a 2.5 h interview).
    # Known trade-off: on very long files this is what can trigger Whisper's
    # repetition loops — switch it off in the STT tab if that ever shows up.
    "condition_on_previous_text": os.environ.get(
        "WHISPER_CONDITION_ON_PREVIOUS", "1") in ("1", "true"),
    # "auto" = built-in per-language style primer, "" = off, anything else
    # is used verbatim. See _resolve_initial_prompt for why this exists.
    "initial_prompt": os.environ.get("WHISPER_INITIAL_PROMPT", "auto"),
    # Default speaker-count hint for diarization; 0 = let pyannote estimate.
    # Per-request num_speakers still wins. Set to 2 before transcribing an
    # interview and the greeting ping-pong stops collapsing into one speaker.
    "num_speakers": int(os.environ.get("STT_NUM_SPEAKERS", "0")),
    # Each device has its own engine, and with it its own model: a Whisper size
    # (cpu_model / gpu_model) or a Parakeet precision (cpu_quality / gpu_quality).
    # Any mix works, e.g. Parakeet int8 on the CPU and Whisper large-v3 on the GPU.
    # A request without engine, model or quality uses these for the device it asks for.
    "cpu_engine": os.environ.get("STT_CPU_ENGINE", "whisper"),
    "gpu_engine": os.environ.get("STT_GPU_ENGINE", "whisper"),
    "cpu_quality": os.environ.get("STT_CPU_QUALITY", "fp32"),
    "gpu_quality": os.environ.get("STT_GPU_QUALITY", "fp32"),
    # What `device=auto` tries, top to bottom: entries of device, engine and model (a Whisper size or a
    # Parakeet precision), each one switched on or off. None until the first start fills it (see
    # _default_auto_chain).
    "auto_chain": None,
    # Free VRAM (MiB) a card must keep beyond what a model needs before it is loaded there.
    "vram_margin_mib": int(os.environ.get("STT_VRAM_MARGIN_MIB", "256")),
}

# Settings changed through /config survive a restart: the container cannot write the
# host's .env, so they live in a file on a volume. The environment only supplies the
# first defaults; once the file exists it wins. Delete the file to return to the .env.
_CONFIG_FILE = Path(os.environ.get("STT_CONFIG_FILE", "/state/config.json"))


def _load_saved_config() -> None:
    if not _CONFIG_FILE.exists():
        return
    saved = json.loads(_CONFIG_FILE.read_text(encoding="utf-8"))
    unknown = set(saved) - set(_config)
    if unknown:
        raise SystemExit(f"{_CONFIG_FILE}: unknown settings {sorted(unknown)} — delete the file or fix it")
    _config.update(saved)
    print(f"[STT] Settings restored from {_CONFIG_FILE}", flush=True)


def _save_config() -> None:
    """Write all settings atomically; raises on failure so /config answers 500, not 200."""
    _CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = _CONFIG_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(_config, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(_CONFIG_FILE)


_load_saved_config()


def _default_auto_chain() -> list[dict]:
    """The list `device=auto` starts with: the configured GPU model, then the configured CPU model. From
    then on the list is the user's to change (more steps, other engines)."""
    gpu_engine, cpu_engine = _config["gpu_engine"], _config["cpu_engine"]
    gpu = _config["gpu_quality"] if gpu_engine == "parakeet" else _config["gpu_model"]
    cpu = _config["cpu_quality"] if cpu_engine == "parakeet" else _config["cpu_model"]
    return [{"device": "cuda", "engine": gpu_engine, "model": gpu, "enabled": True},
            {"device": "cpu", "engine": cpu_engine, "model": cpu, "enabled": True}]


if _config["auto_chain"] is None:
    _config["auto_chain"] = _default_auto_chain()

# Whisper continues whatever "style" the transcript has so far. At the very
# start there is no transcript, so the first 30 s window decides — and fast
# dialogue ping-pong (greetings, "right", "yeah") reliably tips it into an
# unpunctuated staccato that then sticks. Seeding a well-punctuated sentence
# as fake history locks in written style with punctuation instead. The
# primer text itself never appears in the output.
_INITIAL_PROMPTS = {
    "de": "Schön, Sie wiederzusehen. Wir haben heute viel zu besprechen, nicht wahr? Ja, absolut.",
    "en": "Well, it's great to see you again. We have a lot to talk about today, don't we? Yes, absolutely.",
}


def _resolve_initial_prompt(language: str) -> str | None:
    """Effective initial_prompt for a request, or None to send nothing.

    With language "auto" no primer is used: Whisper detects the language
    from the first window, and a primer in the wrong language could tip
    that detection.
    """
    configured = _config["initial_prompt"]
    if not configured:
        return None
    if configured != "auto":
        return configured
    return _INITIAL_PROMPTS.get(language)
EAGER_LOAD = os.environ.get("STT_EAGER_LOAD", "1") in ("1", "true", "True")

# Free VRAM (MiB) a card needs for each model before it is loaded there: the shipped estimates
# (vram_mib.json), overridden by what a burn-in measured on this machine (kept next to the settings,
# in the state volume). A model that fits no card is not loaded: the request gets 503, or in `auto`
# mode the next entry.
_VRAM_FILE = Path(os.environ.get("STT_VRAM_FILE", Path(__file__).parent / "vram_mib.json"))
_VRAM_MEASURED_FILE = Path(os.environ.get("STT_VRAM_MEASURED_FILE", "/state/vram_measured.json"))


def _read_vram_tables() -> tuple[dict, dict]:
    """The needs per engine and model (estimates with the measured ones laid over), and the measured ones."""
    needs = {engine: dict(models) for engine, models in json.loads(_VRAM_FILE.read_text(encoding="utf-8")).items()
             if not engine.startswith("_")}  # keys with "_" are notes in the file
    measured = json.loads(_VRAM_MEASURED_FILE.read_text(encoding="utf-8")) if _VRAM_MEASURED_FILE.exists() else {}
    for engine, models in measured.items():
        needs[engine].update(models)
    return needs, measured


_VRAM_MIB, _VRAM_MEASURED = _read_vram_tables()


def _vram_needed_mib(engine: str, model: str, quality: str) -> int:
    """Free VRAM a card needs for this model, with the margin: a Whisper size or a Parakeet precision."""
    return _VRAM_MIB[engine][quality if engine == "parakeet" else model] + _config["vram_margin_mib"]


# ── Speaker diarization (optional, own worker on its own GPU) ─
# Off by default: 99 % of requests are short voice commands where speaker
# labels are pointless and would only cost load time. Only long recordings
# (interviews, meetings) ask for it via diarize=1 per request.
# pyannote 4.x ships "community-1" as its current pipeline; the older 3.1
# identifier just redirects there, so name it directly.
DIARIZE_MODEL = os.environ.get("DIARIZE_MODEL", "pyannote/speaker-diarization-community-1")
# pyannote 3.1 ≈ 1 GiB VRAM — noticeably less than Whisper needs.
_DIARIZE_MIN_VRAM_MIB = int(os.environ.get("DIARIZE_MIN_VRAM_MIB", "1500"))
_DIARIZE_TTL_MINUTES = int(os.environ.get("DIARIZE_TTL_MINUTES", "10"))
# 0 = every GPU qualifies, which holds as long as torch is pinned to the
# CUDA 12 build (see Dockerfile): its cuDNN still supports Volta, so the
# V100s are usable. Should torch ever move to CUDA 13, sm_70 aborts with
# "cuDNN version ... not compatible with devices with SM < 7.5" — set this
# to 7.5 then to keep diarization on Turing or newer.
_DIARIZE_MIN_COMPUTE_CAP = float(os.environ.get("DIARIZE_MIN_COMPUTE_CAP", "0"))
_HF_TOKEN = os.environ.get("HF_TOKEN", "")

# Max wait for one GPU transcription result. Long meeting recordings need
# minutes even on GPU (medium ≈ 10-20x realtime → 2 h audio ≈ 6-12 min);
# matches the AIfred-side WHISPER_TRANSCRIBE_TIMEOUT_S.
_GPU_RESULT_TIMEOUT_S = int(os.environ.get("STT_GPU_RESULT_TIMEOUT_S", "1800"))


# ── CPU Model (main process, permanent) ──────────────────────

_model_cpu = None
_cpu_lock = threading.Lock()


_cpu_model_name: str = ""  # Track which model is actually loaded on CPU


def _load_cpu_model(model_name: str):
    """Load a Whisper model on CPU in the main process."""
    global _model_cpu, _cpu_model_name
    from faster_whisper import WhisperModel

    compute = _config["cpu_compute"]
    t0 = time.time()
    print(f"[STT] Loading model '{model_name}' on cpu ({compute})...", flush=True)
    _model_cpu = WhisperModel(model_name, device="cpu", compute_type=compute)
    _cpu_model_name = model_name
    print(f"[STT] Model loaded on cpu in {time.time() - t0:.1f}s", flush=True)


def _ensure_cpu_model(model_name: str):
    """The Whisper CPU model of this size; the caller holds _cpu_lock. Loads when nothing is
    loaded or another size is."""
    if _model_cpu is None or _cpu_model_name != model_name:
        _load_cpu_model(model_name)


def _transcribe_cpu(audio_path: str, language: str, return_segments: bool,
                    model_name: str) -> dict:
    """Transcribe using the Whisper CPU model of this size (main process)."""
    with _cpu_lock:
        _ensure_cpu_model(model_name)
        model = _model_cpu

    t0 = time.time()
    segments, info = model.transcribe(
        audio_path,
        language=language if language != "auto" else None,
        vad_filter=_config["vad_filter"],
        beam_size=_config["beam_size"],
        condition_on_previous_text=_config["condition_on_previous_text"],
        initial_prompt=_resolve_initial_prompt(language),
        # See the GPU worker: per-word stamps only when diarizing, so the
        # speaker merge can cut mid-segment where the speaker changes.
        word_timestamps=return_segments,
    )
    # Materialise once — ``segments`` is a generator; the timestamps are what
    # the speaker merge needs.
    seg_list = []
    text_parts = []
    for s in segments:
        text_parts.append(s.text)
        if return_segments and s.words:
            seg_list.extend((w.start, w.end, w.word) for w in s.words)
        elif return_segments:
            seg_list.append((s.start, s.end, s.text))
    text = " ".join(text_parts).strip()
    elapsed = time.time() - t0
    print(f"[STT] Transcribed (cpu, {elapsed:.2f}s): {text[:80]}...", flush=True)
    payload = {
        "text": text, "time": round(elapsed, 3), "device": "cpu",
        "language": info.language,
        "language_probability": round(info.language_probability, 3),
    }
    if return_segments:
        payload["segments"] = seg_list
    return payload


# ── Parakeet (onnx-asr) ──────────────────────────────────────
#
# Measured on German audiobooks (4 × 60 s): fp32 matches Whisper large-v3
# closely and never dropped text where Whisper skipped half a paragraph;
# CPU ≈ 5 s per audio minute (Whisper medium 12–23 s), V100 ≈ 0.25 s.

_PARAKEET_REPO = "istupakov/parakeet-tdt-0.6b-v3-onnx"
# A real directory inside the HF volume: onnxruntime refuses the fp32
# encoder's external weight file behind the HF cache's blob symlinks.
_PARAKEET_DIR = Path(os.environ.get("PARAKEET_DIR", "/root/.cache/huggingface/parakeet-tdt-0.6b-v3"))
# Audio is cut at speech pauses into chunks of at most this length, so VRAM
# does not grow with the recording. Short pieces hurt: below ~20 s the model
# loses context (names garbled, stray English words), so pauses shorter
# than PARAKEET_MERGE_SILENCE_MS are merged up to the chunk length.
_PARAKEET_CHUNK_S = float(os.environ.get("PARAKEET_CHUNK_S", "60"))
_PARAKEET_MERGE_SILENCE_MS = float(os.environ.get("PARAKEET_MERGE_SILENCE_MS", "5000"))
# Chunks recognized at once. onnx_asr batches 8 by default: a long recording then held up to eight
# chunks on the card while the burn-in (one chunk) measured a single one, and a 10-min file ran out
# of VRAM on a card the placement had judged big enough (25.10.2026). One chunk at a time keeps VRAM
# at what the burn-in measures, whatever the length.
_PARAKEET_BATCH = int(os.environ.get("PARAKEET_BATCH", "1"))
_parakeet_download_lock = threading.Lock()


def _parakeet_model_dir() -> str:
    """The model files, downloaded once into the HF volume."""
    with _parakeet_download_lock:
        if not (_PARAKEET_DIR / "config.json").exists():
            from huggingface_hub import snapshot_download
            print(f"[Parakeet] Downloading {_PARAKEET_REPO} → {_PARAKEET_DIR}", flush=True)
            snapshot_download(_PARAKEET_REPO, local_dir=str(_PARAKEET_DIR))
    return str(_PARAKEET_DIR)


def _load_parakeet(quality: str, provider: str):
    """Parakeet with VAD chunking and timestamps on one onnxruntime provider.

    onnxruntime falls back to the CPU silently when CUDA cannot load (wrong
    CUDA/cuDNN) — checked here, so a "GPU" worker never computes on the CPU.
    """
    import onnx_asr
    t0 = time.time()
    model = onnx_asr.load_model(
        "nemo-parakeet-tdt-0.6b-v3", _parakeet_model_dir(),
        quantization="int8" if quality == "int8" else None, providers=[provider],
    )
    active = model.asr._encoder.get_providers()[0]
    if active != provider:
        raise RuntimeError(f"Parakeet runs on {active}, not {provider}")
    vad = onnx_asr.load_vad("silero", providers=["CPUExecutionProvider"])
    print(f"[Parakeet] Loaded {quality} on {provider} in {time.time() - t0:.1f}s", flush=True)
    return model.with_vad(vad, batch_size=_PARAKEET_BATCH, max_speech_duration_s=_PARAKEET_CHUNK_S,
                          min_silence_duration_ms=_PARAKEET_MERGE_SILENCE_MS).with_timestamps()


def _parakeet_transcribe(model, wav_path: str, want_words: bool) -> tuple[str, list]:
    """Text, plus (start, end, word) for the speaker merge when asked.

    Tokens are word pieces; a new word starts with a space. Token times are
    relative to their chunk, so the chunk start is added.
    """
    texts: list[str] = []
    words: list[tuple[float, float, str]] = []
    for seg in model.recognize(wav_path):
        if seg.text.strip():
            texts.append(seg.text.strip())
        if not want_words:
            continue
        current: list | None = None
        for token, rel in zip(seg.tokens, seg.timestamps):
            at = seg.start + rel
            if current is None or token.startswith(" "):
                if current:
                    words.append(tuple(current))
                current = [at, at, token]
            else:
                current[1] = at
                current[2] += token
        if current:
            words.append(tuple(current))
    return " ".join(texts), words


_parakeet_cpu = None
_parakeet_cpu_quality = ""
_parakeet_cpu_lock = threading.Lock()


def _transcribe_parakeet_cpu(wav_path: str, language: str, quality: str,
                             return_segments: bool = False) -> dict:
    """Parakeet on the CPU (main process, stays loaded like Whisper's)."""
    global _parakeet_cpu, _parakeet_cpu_quality
    with _parakeet_cpu_lock:
        if _parakeet_cpu is None or _parakeet_cpu_quality != quality:
            _parakeet_cpu = _load_parakeet(quality, "CPUExecutionProvider")
            _parakeet_cpu_quality = quality
        model = _parakeet_cpu
    t0 = time.time()
    text, words = _parakeet_transcribe(model, wav_path, return_segments)
    elapsed = time.time() - t0
    print(f"[Parakeet] Transcribed (cpu, {quality}, {elapsed:.2f}s): {text[:80]}...", flush=True)
    payload = {"text": text, "time": round(elapsed, 3), "device": "cpu",
               "engine": "parakeet", "quality": quality,
               # Parakeet detects the language itself and reports none.
               "language": language, "language_probability": None}
    if return_segments:
        payload["segments"] = words
    return payload


# ── GPU Worker (child process, killed after TTL) ─────────────

_gpu_process: multiprocessing.Process | None = None
_gpu_request_queue: multiprocessing.Queue | None = None
_gpu_result_queue: multiprocessing.Queue | None = None
_gpu_busy = False  # True while a transcription job is in flight
_gpu_lock = threading.Lock()

# Lock file shared with the TTS-Suite (mounted into the container). Whoever chooses a card holds the lock
# until its model sits in VRAM, so the next one sees the real free memory instead of choosing the same
# card at the same moment. Unset = no other program to coordinate with.
_PLACEMENT_LOCK_FILE = os.environ.get("STT_GPU_PLACEMENT_LOCK", "")
# Descriptor of the lock file while this process holds the lock. A worker forked inside the lock inherits a copy,
# and flock holds the lock while any copy is open: the worker closes its copy first (_drop_inherited_placement_lock).
_held_placement_lock: int | None = None


@contextlib.contextmanager
def _placement_lock():
    """Choosing a card and loading the model onto it, one program at a time across the machine."""
    global _held_placement_lock
    if not _PLACEMENT_LOCK_FILE:
        yield
        return
    with open(_PLACEMENT_LOCK_FILE, "a") as lock_file:  # closing the file (or dying) releases the lock
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        _held_placement_lock = lock_file.fileno()
        try:
            yield
        finally:
            _held_placement_lock = None


def _drop_inherited_placement_lock() -> None:
    """In a worker started inside the lock: close the inherited copy of the lock's descriptor. Otherwise the
    worker would hold the lock for its whole life (found 25.10.: the TTS-Suite could not start an engine
    while the STT GPU worker lived)."""
    if _held_placement_lock is not None:
        os.close(_held_placement_lock)
_gpu_ttl_timer: threading.Timer | None = None
_last_gpu_request = 0.0
_gpu_device_index: int | None = None
_gpu_uuid: str = ""  # UUID of the card Whisper sits on (diarization avoids it)
_gpu_model_name: str = ""  # Track which model is loaded on GPU
_gpu_engine: str = ""  # whisper | parakeet — the worker holds one engine
_gpu_quality: str = ""  # Parakeet quality of the loaded worker
_gpu_footprint_mib: int = 0  # What the loaded worker took on its card right after loading


def _find_best_gpu(min_vram_mib: int,
                   exclude_uuid: str | None = None,
                   min_compute_cap: float = 0.0,
                   tag: str = "Whisper") -> tuple[int, str] | None:
    """Find the GPU with the most free VRAM. Prefers completely empty GPUs.

    Returns (nvidia-smi index, GPU UUID). The worker pins the card via its
    UUID — same SSOT technique as the llama-swap profiles — so the CUDA
    enumeration order (FASTEST_FIRST vs PCI) can never select a different
    card than the one reported here.

    ``exclude_uuid`` skips a card already taken by another worker, so the
    diarization model lands on a different GPU than Whisper instead of both
    fighting over the same scraps of free VRAM.

    ``min_compute_cap`` filters by GPU generation. PyTorch's bundled cuDNN
    dropped Volta: anything below 7.5 aborts with "cuDNN version ... is not
    compatible with devices with SM < 7.5" the moment a model is moved to
    that card. Whisper/CTranslate2 is unaffected and still uses every GPU.
    """
    import subprocess
    min_vram = min_vram_mib
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=index,uuid,memory.free,memory.used,name,compute_cap",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5, check=True,
        )
        best: tuple[int, str] | None = None
        best_free, best_used = 0, float("inf")
        for line in result.stdout.strip().split("\n"):
            parts = [p.strip() for p in line.split(",")]
            if len(parts) >= 5:
                idx, uuid = int(parts[0]), parts[1]
                free, used = int(parts[2]), int(parts[3])
                name = parts[4]
                cap = float(parts[5]) if len(parts) >= 6 else 0.0
                print(f"[{tag}]   GPU {idx}: {name} (sm_{cap}) — {free} MiB free, "
                      f"{used} MiB used", flush=True)
                if uuid == exclude_uuid:
                    continue
                if min_compute_cap and cap < min_compute_cap:
                    continue
                if free < min_vram:
                    continue
                if best is None or used == 0 and best_used > 0 or (used == 0) == (best_used == 0) and free > best_free:
                    best, best_free, best_used = (idx, uuid), free, used
        if best is not None:
            print(f"[{tag}] Selected GPU {best[0]} ({best[1]}, "
                  f"free={best_free} MiB, used={int(best_used)} MiB)", flush=True)
        else:
            print(f"[{tag}] No GPU with >= {min_vram} MiB free VRAM", flush=True)
        return best
    except (subprocess.SubprocessError, OSError, ValueError) as e:
        print(f"[{tag}] GPU detection failed: {e}", flush=True)
        return None


def _detect_gpu_compute(gpu_idx: int) -> str:
    """Detect best compute type for a GPU based on Compute Capability."""
    import subprocess
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=index,compute_cap",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5, check=True,
        )
        for line in result.stdout.strip().split("\n"):
            parts = [p.strip() for p in line.split(",")]
            if len(parts) >= 2 and int(parts[0]) == gpu_idx:
                cc = float(parts[1])
                if cc >= 7.0:
                    return _config["gpu_compute"]
                print(f"[STT] GPU {gpu_idx} CC {cc} < 7.0 → using int8", flush=True)
                return "int8"
    except (subprocess.SubprocessError, OSError, ValueError) as error:
        print(f"[STT] Compute capability of GPU {gpu_idx} unknown ({error}) → using int8", flush=True)
    return "int8"


def _gpu_worker(req_queue: multiprocessing.Queue, res_queue: multiprocessing.Queue,
                gpu_idx: int, gpu_uuid: str, model_name: str, compute: str,
                engine: str = "whisper", quality: str = "fp32"):
    """Child process: load model on GPU, process requests until killed."""
    _drop_inherited_placement_lock()
    # Pin by UUID (SSOT, same as llama-swap profiles) — immune to CUDA's
    # enumeration order, which differs from nvidia-smi on mixed-GPU hosts.
    os.environ["CUDA_VISIBLE_DEVICES"] = gpu_uuid
    print(f"[STT/GPU] Worker started on GPU {gpu_idx} ({gpu_uuid}, PID {os.getpid()})", flush=True)

    t0 = time.time()
    try:
        if engine == "parakeet":
            model = _load_parakeet(quality, "CUDAExecutionProvider")
        else:
            from faster_whisper import WhisperModel
            print(f"[STT/GPU] Loading model '{model_name}' ({compute})...", flush=True)
            model = WhisperModel(model_name, device="cuda", compute_type=compute, device_index=0)
    except Exception as e:  # noqa: BLE001 — whatever the load raises, the parent must hear of it
        print(f"[STT/GPU] Loading {engine} failed: {e}", flush=True)
        res_queue.put({"status": "failed", "error": str(e)})
        return
    print(f"[STT/GPU] {engine} loaded in {time.time() - t0:.1f}s", flush=True)

    # Signal parent that we're ready
    res_queue.put({"status": "ready"})

    while True:
        try:
            job = req_queue.get(timeout=10)
        except queue.Empty:
            continue  # Keep waiting

        if job is None:
            break  # Poison pill → exit

        audio_path = job["audio_path"]
        language = job["language"]

        try:
            t0 = time.time()
            want_words = bool(job.get("return_segments"))
            if engine == "parakeet":
                text, words = _parakeet_transcribe(model, audio_path, want_words)
                elapsed = time.time() - t0
                print(f"[Parakeet/GPU] Transcribed ({quality}, {elapsed:.2f}s): {text[:80]}...", flush=True)
                payload = {"text": text, "time": round(elapsed, 3), "device": "cuda",
                           "engine": "parakeet", "quality": quality,
                           "language": language, "language_probability": None}
                if want_words:
                    payload["segments"] = words
                res_queue.put(payload)
                continue
            segments, info = model.transcribe(
                audio_path,
                language=language if language != "auto" else None,
                vad_filter=job.get("vad_filter", True),
                beam_size=job.get("beam_size", 5),
                condition_on_previous_text=job.get("condition_on_previous_text", False),
                initial_prompt=job.get("initial_prompt"),
                # Word timestamps only when diarizing: a Whisper segment can
                # span 10+ seconds and contain BOTH speakers, so segment-level
                # assignment smears turns together. Per-word stamps let the
                # merge cut exactly where the speaker changes.
                word_timestamps=want_words,
            )
            # Materialise once — ``segments`` is a generator, a second pass
            # would be empty. Timestamps are what the speaker merge needs.
            seg_list = []
            text_parts = []
            for s in segments:
                text_parts.append(s.text)
                if want_words and s.words:
                    seg_list.extend((w.start, w.end, w.word) for w in s.words)
                elif want_words:
                    seg_list.append((s.start, s.end, s.text))
            text = " ".join(text_parts).strip()
            elapsed = time.time() - t0
            print(f"[STT/GPU] Transcribed ({elapsed:.2f}s): {text[:80]}...", flush=True)
            payload = {
                "text": text, "time": round(elapsed, 3), "device": "cuda",
                "language": info.language,
                "language_probability": round(info.language_probability, 3),
            }
            if job.get("return_segments"):
                payload["segments"] = seg_list
            res_queue.put(payload)
        except Exception as e:  # noqa: BLE001 — a failed job is the caller's answer, the worker goes on
            res_queue.put({"error": str(e)})

    print(f"[STT/GPU] Worker exiting (PID {os.getpid()})", flush=True)


def _free_per_card_mib() -> dict[int, int]:
    """Free VRAM (MiB) per card, by nvidia-smi index; empty without an NVIDIA driver."""
    import subprocess
    try:
        result = subprocess.run(["nvidia-smi", "--query-gpu=index,memory.free", "--format=csv,noheader,nounits"],
                                capture_output=True, text=True, timeout=5, check=True)
    except FileNotFoundError:
        return {}  # no NVIDIA driver in this container: no card has anything free
    return {int(index): int(free) for index, free in (line.split(",") for line in result.stdout.strip().splitlines())}


def _refusal(need: int, free_mib: int) -> str:
    return (f"needs {need} MiB free ({_config['vram_margin_mib']} MiB margin included), "
            f"the card with the most has {free_mib} MiB")


def _start_gpu_worker(engine: str, model: str, quality: str, check_fit: bool = True) -> str | None:
    """Start the GPU child process with exactly this model on the card with the most free VRAM.

    Nothing is substituted: a model that no card has room for is not loaded (`check_fit=False` is for
    the burn-in, which measures what a model needs). Returns None when the worker is ready, else the
    reason it is not.
    """
    need = _vram_needed_mib(engine, model, quality) if check_fit else 0
    with _placement_lock():
        selected = _find_best_gpu(min_vram_mib=need, tag="Parakeet" if engine == "parakeet" else "Whisper")
        if selected is None:
            return _refusal(need, max(_free_per_card_mib().values(), default=0))
        name = f"parakeet-{quality}" if engine == "parakeet" else model
        compute = "" if engine == "parakeet" else _detect_gpu_compute(selected[0])
        if not _spawn_gpu_worker(selected, name, compute, engine, quality):
            return f"the worker for {name} did not start"
    return None


def _room_after_release(need: int) -> str | None:
    """Whether a card has `need` MiB free once the loaded worker has let go of its memory; None when
    it does, else the reason. Decided from the table before the worker is stopped, so a model that
    cannot be loaded never costs the one that is."""
    free = _free_per_card_mib()
    if _gpu_device_index in free:
        free[_gpu_device_index] += _gpu_footprint_mib  # what it took when loaded: no more than it holds now
    best = max(free.values(), default=0)
    return None if best >= need else _refusal(need, best)


def _spawn_gpu_worker(selected: tuple[int, str], model_name: str, compute: str,
                      engine: str, quality: str) -> bool:
    """Start the worker process on the chosen card and wait until it is ready."""
    global _gpu_process, _gpu_request_queue, _gpu_result_queue, _gpu_device_index, _gpu_model_name, _gpu_uuid
    global _gpu_engine, _gpu_quality, _gpu_footprint_mib
    gpu_idx, gpu_uuid = selected
    used_before = _used_per_card_mib().get(gpu_idx, 0)
    _gpu_device_index = gpu_idx
    _gpu_uuid = gpu_uuid
    _gpu_model_name = model_name
    _gpu_engine = engine
    _gpu_quality = quality if engine == "parakeet" else ""

    _gpu_request_queue = multiprocessing.Queue()
    _gpu_result_queue = multiprocessing.Queue()

    _gpu_process = multiprocessing.Process(
        target=_gpu_worker,
        args=(_gpu_request_queue, _gpu_result_queue, gpu_idx, gpu_uuid, model_name, compute,
              engine, quality),
        daemon=True,
        name=f"{engine}-gpu-{gpu_idx}",
    )
    _gpu_process.start()

    # Wait for ready signal (the first Parakeet start also downloads the model)
    try:
        msg = _gpu_result_queue.get(timeout=600 if engine == "parakeet" else 120)
        if msg.get("status") == "ready":
            _gpu_footprint_mib = max(0, _used_per_card_mib().get(gpu_idx, 0) - used_before)
            print(f"[STT] GPU worker ({engine}) ready on GPU {gpu_idx}, {_gpu_footprint_mib} MiB", flush=True)
            return True
    except queue.Empty:
        print(f"[STT] GPU worker ({engine}) did not report ready in time", flush=True)

    print(f"[STT] GPU worker ({engine}) failed to start", flush=True)
    _kill_gpu_worker()
    return False


# How long a worker gets to end after SIGKILL before its end counts as unproven.
_WORKER_END_TIMEOUT_S = 10


def _end_worker(process: multiprocessing.Process, label: str) -> str:
    """End ``process`` and prove it: "" when it is gone, else what still runs. kill() on a process that
    already ended does nothing, so this also proves a worker that died by itself."""
    process.kill()
    process.join(timeout=_WORKER_END_TIMEOUT_S)
    if process.is_alive():
        message = f"{label} PID {process.pid} still alive {_WORKER_END_TIMEOUT_S} s after SIGKILL"
        print(f"[STT] {message}", flush=True)
        return message
    return ""


def _kill_gpu_worker(withdrawn: str = "") -> str:
    """End the GPU child process to release its CUDA context and VRAM.

    Returns what still runs ("" when the worker is proven gone; then the state is cleared). A
    transcription running on the worker is told ``withdrawn`` (or that the worker was ended): it counts as
    "GPU unavailable", so a request with ``device=auto`` goes on on the CPU.
    """
    global _gpu_process, _gpu_request_queue, _gpu_result_queue, _gpu_device_index, _gpu_model_name, _gpu_uuid
    global _gpu_engine, _gpu_quality, _gpu_footprint_mib

    if _gpu_process is not None:
        print(f"[STT] Ending GPU worker (PID {_gpu_process.pid}, GPU {_gpu_device_index})", flush=True)
        still_running = _end_worker(_gpu_process, "GPU worker")
        if still_running:
            return still_running
        _gpu_process = None
        print("[STT] GPU worker ended — VRAM released", flush=True)

    if _gpu_request_queue is not None:
        _gpu_request_queue.close()
        _gpu_request_queue = None
    if _gpu_result_queue is not None:
        if _gpu_busy:
            # The /transcribe request waiting on this queue reads the reason from it instead of sitting
            # out the full result timeout; that is why the queue stays open here.
            _gpu_result_queue.put({"withdrawn": withdrawn or "the GPU worker was ended"})
        else:
            _gpu_result_queue.close()
        _gpu_result_queue = None
    _gpu_device_index = None
    _gpu_uuid = ""
    _gpu_model_name = ""
    _gpu_engine = ""
    _gpu_quality = ""
    _gpu_footprint_mib = 0
    return ""


def _reset_gpu_ttl():
    """Reset the GPU auto-kill timer."""
    global _gpu_ttl_timer, _last_gpu_request
    _last_gpu_request = time.time()
    ttl = _config["gpu_ttl_minutes"]
    if ttl <= 0:
        return
    if _gpu_ttl_timer is not None:
        _gpu_ttl_timer.cancel()
    _gpu_ttl_timer = threading.Timer(ttl * 60, _kill_gpu_worker)
    _gpu_ttl_timer.daemon = True
    _gpu_ttl_timer.start()


def _rearm_gpu_ttl():
    """Apply a changed gpu_ttl_minutes to the running timer at once, counted from the
    last GPU request (idle time already beyond the new TTL releases the GPU now). A job
    in flight needs nothing: its result re-arms the timer."""
    global _gpu_ttl_timer
    if _gpu_ttl_timer is not None:
        _gpu_ttl_timer.cancel()
        _gpu_ttl_timer = None
    ttl = _config["gpu_ttl_minutes"]
    if ttl <= 0 or _gpu_busy or _gpu_process is None or not _gpu_process.is_alive():
        return
    remaining = ttl * 60 - (time.time() - _last_gpu_request)
    if remaining <= 0:
        _kill_gpu_worker()
        return
    _gpu_ttl_timer = threading.Timer(remaining, _kill_gpu_worker)
    _gpu_ttl_timer.daemon = True
    _gpu_ttl_timer.start()


class GpuUnavailable(Exception):
    """The GPU cannot take this model now: no card has room, or the worker is busy with another one."""


class GpuWithdrawn(GpuUnavailable):
    """The card was taken back during the transcription (GPU-Orc before a model load): this request
    gets no GPU any more, not even with another model."""


def _model_name(engine: str, model: str, quality: str) -> str:
    """How a loaded model is named: a Whisper size, or "parakeet-<precision>"."""
    return f"parakeet-{quality}" if engine == "parakeet" else model


def _transcribe_gpu(audio_path: str, language: str, return_segments: bool,
                    engine: str, model: str, quality: str, measuring: bool = False) -> dict:
    """Transcribe with exactly this model in the GPU child process. :class:`GpuUnavailable`
    when it cannot be loaded now; the caller decides what that means."""
    global _gpu_busy
    if _burn_in["running"] and not measuring:
        raise GpuUnavailable("a burn-in is measuring on the GPU")
    with _gpu_lock:
        alive = _gpu_process is not None and _gpu_process.is_alive()
        wanted = _model_name(engine, model, quality)
        # The worker holds one model; another one means a swap — unless a job is still
        # running on it, then this request is refused like one without room.
        if alive and (_gpu_engine, _gpu_model_name) != (engine, wanted):
            if _gpu_busy:
                raise GpuUnavailable(f"the GPU is busy with {_gpu_model_name}")
            if not measuring and (refusal := _room_after_release(_vram_needed_mib(engine, model, quality))):
                raise GpuUnavailable(refusal)  # decided before the loaded model is let go
            if still_running := _kill_gpu_worker():
                raise GpuUnavailable(still_running)
            alive = False
        if not alive:
            refusal = _start_gpu_worker(engine, model, quality, check_fit=not measuring)
            if refusal is not None:
                raise GpuUnavailable(refusal)

        # Send job
        _gpu_busy = True
        _gpu_request_queue.put({
            "audio_path": audio_path,
            "language": language,
            "vad_filter": _config["vad_filter"],
            "beam_size": _config["beam_size"],
            "condition_on_previous_text": _config["condition_on_previous_text"],
            "initial_prompt": _resolve_initial_prompt(language),
            "return_segments": return_segments,
        })

    # Wait for result (outside lock so CPU requests aren't blocked)
    try:
        result = _gpu_result_queue.get(timeout=_GPU_RESULT_TIMEOUT_S)
        if "withdrawn" in result:
            # The card was taken back for something else (GPU-Orc before a model load): to the plan this
            # is "no GPU now", so device=auto goes on on the CPU and device=cuda answers 503 with the reason.
            raise GpuWithdrawn(f"the GPU was withdrawn: {result['withdrawn']}")
        _reset_gpu_ttl()
        return result
    except queue.Empty:
        return {"error": f"GPU worker timeout ({_GPU_RESULT_TIMEOUT_S}s)"}
    finally:
        _gpu_busy = False


def _decode_to_wav16k(src_path: str) -> str:
    """Decode audio once to 16 kHz mono WAV — the format BOTH engines want.

    Whisper and pyannote each resample to 16 kHz mono internally, so without
    this the same MP3 gets decoded twice in parallel: two CPU cores busy for
    the same result, and on a 2.5 h recording that is a minute of pure waste
    before either GPU sees data. Decoding once up front makes it a shared
    input (and both engines then just read PCM).

    Returns the WAV path, or the original path if conversion failed (logged
    loudly — the engines can still decode it themselves).
    """
    import subprocess

    wav_path = f"{src_path}.16k.wav"
    t0 = time.time()
    try:
        proc = subprocess.run(
            ["ffmpeg", "-nostdin", "-loglevel", "error", "-i", src_path,
             "-ar", "16000", "-ac", "1", "-f", "wav", "-y", wav_path],
            capture_output=True, text=True, timeout=1800, check=False,  # the return code is read below
        )
        if proc.returncode == 0 and Path(wav_path).exists():
            size_mb = Path(wav_path).stat().st_size / 1024 / 1024
            print(f"[STT] Decoded once to 16 kHz mono WAV "
                  f"({size_mb:.0f} MB, {time.time() - t0:.1f}s) — shared by both engines",
                  flush=True)
            return wav_path
        print(f"[STT] ffmpeg pre-decode FAILED (rc={proc.returncode}): "
              f"{proc.stderr[:200]} — engines will decode separately", flush=True)
    except (subprocess.SubprocessError, OSError) as e:
        print(f"[STT] ffmpeg pre-decode FAILED: {e} — engines will decode separately",
              flush=True)
    return src_path


# ── Burn-in: measuring what a model needs ───────────────────

# Progress of the measurement the web page starts; one at a time, and while it runs the GPU is its own.
_burn_in: dict = {"running": False, "step": "", "results": [], "error": None}


def _used_per_card_mib() -> dict[int, int]:
    """Used VRAM (MiB) per card, by nvidia-smi index."""
    import subprocess
    output = subprocess.run(["nvidia-smi", "--query-gpu=index,memory.used", "--format=csv,noheader,nounits"],
                            capture_output=True, text=True, timeout=5, check=True).stdout
    return {int(index): int(used) for index, used in (line.split(",") for line in output.strip().splitlines())}


def _measure(engine: str, model: str, wav_path: str, language: str) -> dict:
    """Load this model on a GPU, transcribe the file, and take the highest rise of the used memory on the
    card it sits on (sampled every 0.1 s). Another program growing on that card meanwhile counts too."""
    quality = model if engine == "parakeet" else _config["gpu_quality"]
    whisper_size = _config["gpu_model"] if engine == "parakeet" else model
    with _gpu_lock:
        # A loaded model would be part of the starting point.
        if still_running := _kill_gpu_worker():
            raise RuntimeError(f"cannot measure: {still_running}")
    time.sleep(2)  # the card has let go of its memory
    before = _used_per_card_mib()
    peaks = dict(before)
    stop = threading.Event()

    def sample() -> None:
        while not stop.is_set():
            for index, used in _used_per_card_mib().items():
                peaks[index] = max(peaks[index], used)
            stop.wait(0.1)

    sampler = threading.Thread(target=sample, daemon=True)
    sampler.start()
    try:
        result = _transcribe_gpu(wav_path, language, False, engine, whisper_size, quality, measuring=True)
    finally:
        stop.set()
        sampler.join()
    if "error" in result:
        raise RuntimeError(result["error"])
    card = _gpu_device_index
    return {"engine": engine, "model": model, "measured_mib": peaks[card] - before[card], "card": card,
            "seconds": result["time"]}


def _save_measured(engine: str, model: str, mib: int) -> None:
    """Keep a measurement: it replaces the estimate from now on, and survives a restart."""
    _VRAM_MEASURED.setdefault(engine, {})[model] = mib
    _VRAM_MIB[engine][model] = mib
    _VRAM_MEASURED_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = _VRAM_MEASURED_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(_VRAM_MEASURED, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(_VRAM_MEASURED_FILE)


def _burn_in_job(wav_path: str, entries: list[tuple[str, str]], language: str) -> None:
    """The measurement, in a thread of its own: one model after the other."""
    try:
        for engine, model in entries:
            _burn_in["step"] = f"{engine} {model}"
            result = _measure(engine, model, wav_path, language)
            _save_measured(engine, model, result["measured_mib"])
            _burn_in["results"].append(result)
            print(f"[Burn-in] {engine} {model}: {result['measured_mib']} MiB on card {result['card']}", flush=True)
    except Exception as error:  # noqa: BLE001 — the burn-in shows any failure on its page instead of dying
        _burn_in["error"] = str(error)
        print(f"[Burn-in] failed: {error}", flush=True)
    finally:
        with _gpu_lock:
            _kill_gpu_worker()
        Path(wav_path).unlink(missing_ok=True)
        _burn_in["step"] = ""
        _burn_in["running"] = False


# ── Speaker Diarization (own worker process, own GPU) ────────
#
# Transcription and diarization are fully independent: both only read the
# same audio FILE, no tensors are shared. That is why the pyannote model can
# sit on a different card than Whisper — and why both jobs can run at the
# same time. They only meet at the end, on the CPU, where each Whisper
# segment gets the speaker whose turn overlaps it most.

_diarize_process: multiprocessing.Process | None = None
_diarize_request_queue: multiprocessing.Queue | None = None
_diarize_result_queue: multiprocessing.Queue | None = None
_diarize_lock = threading.Lock()
_diarize_ttl_timer: threading.Timer | None = None
_diarize_device_index: int | None = None
_diarize_uuid: str = ""  # UUID of the card the diarization worker sits on
_diarize_busy = False  # True between a submitted job and its collected result


def _diarize_worker(req_queue: multiprocessing.Queue, res_queue: multiprocessing.Queue,
                    gpu_idx: int, gpu_uuid: str, model_name: str, hf_token: str):
    """Child process: load the pyannote pipeline on its own GPU."""
    _drop_inherited_placement_lock()
    os.environ["CUDA_VISIBLE_DEVICES"] = gpu_uuid
    print(f"[Diarize] Worker started on GPU {gpu_idx} ({gpu_uuid}, PID {os.getpid()})", flush=True)

    import wave

    import numpy as np
    import torch
    from pyannote.audio import Pipeline

    def load_wav16k(path: str):
        """Read the pre-decoded WAV into a (channel, time) float tensor.

        Deliberately bypasses pyannote's own decoder (torchcodec): that one
        is built against a different CUDA generation than the torch we pin
        for Volta support and fails on missing libs. Since ffmpeg already
        produced 16 kHz mono PCM upstream, handing the samples over directly
        is both simpler and one decode cheaper.
        """
        with wave.open(path, "rb") as w:
            sample_rate = w.getframerate()
            channels = w.getnchannels()
            raw = w.readframes(w.getnframes())
        data = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
        if channels > 1:
            data = data.reshape(-1, channels).mean(axis=1)
        return torch.from_numpy(data.copy()).unsqueeze(0), sample_rate

    t0 = time.time()
    print(f"[Diarize] Loading pipeline '{model_name}'...", flush=True)
    # pyannote 4.x renamed the auth parameter to ``token``. Passing None
    # lets huggingface_hub fall back to the mounted token file.
    pipeline = Pipeline.from_pretrained(model_name, token=hf_token or None)
    if pipeline is None:
        res_queue.put({"error": f"Pipeline '{model_name}' could not be loaded "
                                "(gated model — check HF_TOKEN and licence acceptance)"})
        return
    pipeline.to(torch.device("cuda"))
    print(f"[Diarize] Pipeline loaded in {time.time() - t0:.1f}s", flush=True)

    res_queue.put({"status": "ready"})

    while True:
        try:
            job = req_queue.get(timeout=10)
        except queue.Empty:
            continue
        if job is None:
            break

        try:
            t0 = time.time()
            kwargs = {}
            if job.get("num_speakers"):
                kwargs["num_speakers"] = int(job["num_speakers"])
            waveform, sample_rate = load_wav16k(job["audio_path"])
            output = pipeline({"waveform": waveform, "sample_rate": sample_rate},
                              **kwargs)
            # pyannote 4 returns a DiarizeOutput. Prefer its "exclusive"
            # variant: overlapping speech is resolved to a single speaker per
            # instant, which is exactly what assigning whole transcript
            # segments to one speaker needs.
            annotation = getattr(output, "exclusive_speaker_diarization", None)
            if annotation is None:
                annotation = getattr(output, "speaker_diarization", output)
            turns = [(seg.start, seg.end, spk)
                     for seg, _, spk in annotation.itertracks(yield_label=True)]
            speakers = sorted({t[2] for t in turns})
            elapsed = time.time() - t0
            print(f"[Diarize] {len(turns)} turns, {len(speakers)} speaker(s) "
                  f"({elapsed:.1f}s)", flush=True)
            res_queue.put({"turns": turns, "speakers": speakers,
                           "time": round(elapsed, 3)})
        except Exception as e:  # noqa: BLE001 — a failed job is the caller's answer, the worker goes on
            res_queue.put({"error": str(e)})

    print(f"[Diarize] Worker exiting (PID {os.getpid()})", flush=True)


def _start_diarize_worker() -> bool:
    with _placement_lock():
        return _choose_card_and_spawn_diarize_worker()


def _choose_card_and_spawn_diarize_worker() -> bool:
    """Start the diarization child process, avoiding Whisper's GPU."""
    global _diarize_process, _diarize_request_queue, _diarize_result_queue, _diarize_device_index, _diarize_uuid

    selected = _find_best_gpu(min_vram_mib=_DIARIZE_MIN_VRAM_MIB,
                              exclude_uuid=_gpu_uuid or None,
                              min_compute_cap=_DIARIZE_MIN_COMPUTE_CAP, tag="Diarize")
    if selected is None and _gpu_uuid:
        # No second card with room — sharing Whisper's GPU is still better
        # than failing outright, as long as it has enough headroom left.
        print("[Diarize] No separate GPU free — retrying on Whisper's card", flush=True)
        selected = _find_best_gpu(min_vram_mib=_DIARIZE_MIN_VRAM_MIB,
                                  min_compute_cap=_DIARIZE_MIN_COMPUTE_CAP, tag="Diarize")
    if selected is None:
        return False
    gpu_idx, gpu_uuid = selected

    _diarize_device_index = gpu_idx
    _diarize_uuid = gpu_uuid
    _diarize_request_queue = multiprocessing.Queue()
    _diarize_result_queue = multiprocessing.Queue()

    _diarize_process = multiprocessing.Process(
        target=_diarize_worker,
        args=(_diarize_request_queue, _diarize_result_queue, gpu_idx, gpu_uuid,
              DIARIZE_MODEL, _HF_TOKEN),
        daemon=True,
        name=f"diarize-gpu-{gpu_idx}",
    )
    _diarize_process.start()

    try:
        msg = _diarize_result_queue.get(timeout=300)  # first run downloads models
        if msg.get("status") == "ready":
            print(f"[Diarize] Worker ready on GPU {gpu_idx}", flush=True)
            return True
        print(f"[Diarize] Worker failed: {msg.get('error')}", flush=True)
    except queue.Empty:
        print("[Diarize] Worker failed to start (timeout)", flush=True)

    _kill_diarize_worker()
    return False


def _kill_diarize_worker(withdrawn: str = "") -> str:
    """End the diarization process to release its VRAM; returns what still runs ("" when proven gone).
    A job in flight gets ``withdrawn`` as its error, so the transcript comes without speakers."""
    global _diarize_process, _diarize_request_queue, _diarize_result_queue, _diarize_device_index, _diarize_uuid

    if _diarize_process is not None:
        print(f"[Diarize] Ending worker (PID {_diarize_process.pid}, GPU {_diarize_device_index})", flush=True)
        still_running = _end_worker(_diarize_process, "diarization worker")
        if still_running:
            return still_running
        _diarize_process = None

    if _diarize_request_queue is not None:
        _diarize_request_queue.close()
    if _diarize_result_queue is not None:
        if _diarize_busy:
            _diarize_result_queue.put({"error": f"diarization withdrawn: {withdrawn or 'the worker was ended'}"})
        else:
            _diarize_result_queue.close()
    _diarize_request_queue = None
    _diarize_result_queue = None
    _diarize_device_index = None
    _diarize_uuid = ""
    return ""


def _reset_diarize_ttl():
    """Reset the diarization auto-kill timer."""
    global _diarize_ttl_timer
    if _DIARIZE_TTL_MINUTES <= 0:
        return
    if _diarize_ttl_timer is not None:
        _diarize_ttl_timer.cancel()
    _diarize_ttl_timer = threading.Timer(_DIARIZE_TTL_MINUTES * 60, _kill_diarize_worker)
    _diarize_ttl_timer.daemon = True
    _diarize_ttl_timer.start()


def _diarize_submit(audio_path: str, num_speakers: int = 0) -> bool:
    """Queue a diarization job — returns immediately so Whisper can run in
    parallel on its own card. Result is picked up by _diarize_collect()."""
    global _diarize_busy
    with _diarize_lock:
        if (_diarize_process is None or not _diarize_process.is_alive()) and not _start_diarize_worker():
            return False
        _diarize_request_queue.put({"audio_path": audio_path,
                                    "num_speakers": num_speakers})
        _diarize_busy = True
    return True


def _diarize_collect(timeout: int) -> dict | None:
    """Wait for the queued diarization result."""
    global _diarize_busy
    results = _diarize_result_queue
    if results is None:
        _diarize_busy = False
        return None
    try:
        result = results.get(timeout=timeout)
        _reset_diarize_ttl()
        return result
    except queue.Empty:
        return {"error": f"Diarization timeout ({timeout}s)"}
    finally:
        _diarize_busy = False


def _smooth_speakers(merged: list, min_run: int = 2) -> list:
    """Drop speaker runs shorter than ``min_run`` words.

    Word-level assignment reacts to every flicker in the diarization: a
    single "yeah" landing on the other speaker would split a sentence into
    three blocks. Runs below the threshold are absorbed into the surrounding
    speaker, which keeps real turn changes (always several words) intact.
    """
    if not merged:
        return merged

    # Group into consecutive runs of the same speaker
    runs: list[list] = [[merged[0]]]
    for item in merged[1:]:
        if item[3] == runs[-1][0][3]:
            runs[-1].append(item)
        else:
            runs.append([item])

    for i, run in enumerate(runs):
        # Words the diarization left uncovered (gaps between turns) always
        # join a neighbour — a bare "SPEAKER_?" block would only chop the
        # sentence apart without adding information.
        if run[0][3] is not None and len(run) >= min_run:
            continue
        # Absorb into whichever neighbour exists (prefer the previous one,
        # so a stray word joins the sentence it interrupts).
        neighbour = None
        if i > 0:
            neighbour = runs[i - 1][0][3]
        elif i + 1 < len(runs):
            neighbour = runs[i + 1][0][3]
        if neighbour is not None:
            runs[i] = [(s, e, t, neighbour) for s, e, t, _ in run]

    return [item for run in runs for item in run]


def _merge_speakers(segments: list, turns: list) -> list:
    """Attach a speaker label to each Whisper segment by largest overlap.

    Both lists are time-sorted, so a moving index keeps this linear instead
    of comparing every segment against every turn (a 2.5 h recording has
    thousands of each).
    """
    turns = sorted(turns, key=lambda t: t[0])
    merged = []
    idx = 0
    for start, end, text in segments:
        # Turns that end before this segment starts can never match a later
        # segment either — skip them permanently.
        while idx < len(turns) and turns[idx][1] < start:
            idx += 1
        best_speaker, best_overlap = None, 0.0
        probe = idx
        while probe < len(turns) and turns[probe][0] < end:
            overlap = min(end, turns[probe][1]) - max(start, turns[probe][0])
            if overlap > best_overlap:
                best_overlap, best_speaker = overlap, turns[probe][2]
            probe += 1
        merged.append((start, end, text, best_speaker))
    return merged


def _format_speaker_text(merged: list) -> str:
    """Render merged pieces as speaker-labelled blocks.

    Consecutive pieces of the same speaker are joined into one block, so the
    result reads like a dialogue transcript instead of one label per word.

    Pieces are single words when diarizing (Whisper's word timestamps carry
    their own leading space, so they are concatenated verbatim — inserting
    another space would double every gap).
    """
    blocks: list[str] = []
    current: str | None = None
    buf: list[str] = []

    def flush() -> None:
        if buf:
            blocks.append(f"[{current}] {''.join(buf).strip()}")

    for _start, _end, text, speaker in merged:
        label = speaker or "SPEAKER_?"
        if label != current:
            flush()
            current, buf = label, []
        # Whisper words start with a space; segment fallbacks may not, so
        # add one where it would otherwise glue two pieces together.
        piece = text if (text.startswith(" ") or not buf) else f" {text}"
        buf.append(piece)
    flush()
    return "\n\n".join(blocks)


# ── Web-UI ───────────────────────────────────────────────────

def _log_client(action: str, **details: str) -> None:
    """Who asked for what — the service has no access log, and the caller of a GPU load
    would otherwise stay unknown."""
    fields = " ".join(f"{key}={value}" for key, value in details.items())
    print(f"[Request] {action} {fields} from={request.remote_addr} "
          f"agent={request.user_agent.string!r}", flush=True)


# The web page (plain HTML, CSS and JavaScript in ui/, no build step). It holds no data of its own: it
# asks /status, /config, /unload and /transcribe, like any other client.
UI_DIR = Path(__file__).parent / "ui"
UI_PREFIX = "/ui"
# It may only load its own files and talk to this service; no-cache so that a fixed page arrives at once
# (the ETag decides, an unchanged file costs one short answer).
UI_HEADERS = {
    "Content-Security-Policy": "default-src 'self'; img-src 'self' data:; frame-ancestors 'none'",
    "X-Content-Type-Options": "nosniff",
    "Cache-Control": "no-cache",
}


@app.route("/", methods=["GET"])
def index():
    # Relative, so that it also works behind a reverse proxy that serves the service under a prefix (/stt-suite/).
    return redirect("ui/")


@app.route(f"{UI_PREFIX}/", defaults={"filename": "index.html"})
@app.route(f"{UI_PREFIX}/<path:filename>")
def ui(filename: str):
    response = send_from_directory(UI_DIR, filename)
    response.headers.update(UI_HEADERS)
    return response


# ── API Endpoints ────────────────────────────────────────────

def _loaded() -> dict:
    """What is in memory per device: engine and model name ("parakeet-fp32", "large-v3"), or None."""
    gpu_alive = _gpu_process is not None and _gpu_process.is_alive()
    if _parakeet_cpu is not None:
        cpu = {"engine": "parakeet", "model": f"parakeet-{_parakeet_cpu_quality}"}
    elif _model_cpu is not None:
        cpu = {"engine": "whisper", "model": _cpu_model_name}
    else:
        cpu = None
    gpu = {"engine": _gpu_engine, "model": _gpu_model_name} if gpu_alive else None
    return {"cpu": cpu, "gpu": gpu}


@app.route("/health", methods=["GET"])
def health():
    """Health check — reports model status per device."""
    loaded = _loaded()
    if loaded["cpu"] or loaded["gpu"]:
        status, model_loaded = "ok", True
    elif EAGER_LOAD:
        status, model_loaded = "loading", False
    else:
        status, model_loaded = "idle", False

    return jsonify({
        "status": status,
        "model_loaded": model_loaded,
        "cpu_loaded": loaded["cpu"] is not None,
        "cpu_engine": _config["cpu_engine"],
        "cpu_model": loaded["cpu"]["model"] if loaded["cpu"] else _config["cpu_model"],
        "gpu_loaded": loaded["gpu"] is not None,
        "gpu_engine": _config["gpu_engine"],
        "gpu_model": loaded["gpu"]["model"] if loaded["gpu"] else _config["gpu_model"],
        "gpu_device_index": _gpu_device_index,
        "gpu_ttl_minutes": _config["gpu_ttl_minutes"],
    })


# ── Choosing what transcribes ────────────────────────────────

DEVICES = ["cpu", "cuda", "auto"]


def _candidate(device: str, engine: str | None, model: str | None, quality: str | None) -> dict:
    """One thing that could transcribe: a device with an engine and its model. What a request
    does not name is the configured value of that device."""
    prefix = "gpu" if device == "cuda" else "cpu"
    return {
        "device": device,
        "engine": engine or _config[f"{prefix}_engine"],
        "model": model or _config[f"{prefix}_model"],
        "quality": quality or _config[f"{prefix}_quality"],
    }


def _chain_candidate(entry: dict) -> dict:
    """An entry of the auto list as a candidate: its model is a Whisper size or a Parakeet precision."""
    prefix = "gpu" if entry["device"] == "cuda" else "cpu"
    parakeet = entry["engine"] == "parakeet"
    return {
        "device": entry["device"],
        "engine": entry["engine"],
        "model": _config[f"{prefix}_model"] if parakeet else entry["model"],
        "quality": entry["model"] if parakeet else _config[f"{prefix}_quality"],
    }


def _plan(device: str, engine: str | None, model: str | None, quality: str | None) -> list[dict]:
    """What to try, in order. `cpu` and `cuda` name exactly one candidate (what the device is set to,
    or what the request names); `auto` tries the entries of the configured list from the top."""
    if device != "auto":
        return [_candidate(device, engine, model, quality)]
    return [_chain_candidate(entry) for entry in _config["auto_chain"] if entry["enabled"]]


def _label(candidate: dict) -> str:
    """The candidate for messages and answers: "cuda whisper large-v3", "cpu parakeet int8"."""
    detail = candidate["quality"] if candidate["engine"] == "parakeet" else candidate["model"]
    return f"{candidate['device']} {candidate['engine']} {detail}"


def _run_candidate(candidate: dict, work_path: str, language: str, segments: bool) -> dict:
    """Transcribe with this candidate. :class:`GpuUnavailable` when the GPU cannot take it."""
    engine, model, quality = candidate["engine"], candidate["model"], candidate["quality"]
    if candidate["device"] == "cuda":
        result = _transcribe_gpu(work_path, language, segments, engine, model, quality)
    elif engine == "parakeet":
        result = _transcribe_parakeet_cpu(work_path, language, quality, segments)
    else:
        result = _transcribe_cpu(work_path, language, segments, model)
    if "error" not in result:
        # The answer names what really transcribed.
        result["device"] = candidate["device"]
        result["engine"] = engine
        if engine == "parakeet":
            result["quality"] = quality
        else:
            result["model"] = model
    return result


def _run_plan(plan: list[dict], work_path: str, language: str, segments: bool) -> tuple[dict, list[dict]]:
    """Walk the plan until one candidate transcribes. Returns its result and the entries that
    could not take it (with the reason), so an answer after a fallback says what happened.
    Raises :class:`GpuUnavailable` when no candidate could (a single GPU candidate, or a list
    that does not end with the CPU)."""
    skipped: list[dict] = []
    remaining = list(plan)
    while remaining:
        candidate = remaining.pop(0)
        try:
            return _run_candidate(candidate, work_path, language, segments), skipped
        except GpuUnavailable as unavailable:
            skipped.append({"tried": _label(candidate), "reason": str(unavailable)})
            if isinstance(unavailable, GpuWithdrawn):
                # Another GPU model would take the card straight back from whoever freed it.
                skipped += [{"tried": _label(c), "reason": str(unavailable)} for c in remaining if c["device"] == "cuda"]
                remaining = [c for c in remaining if c["device"] != "cuda"]
            following = f" — next: {_label(remaining[0])}" if remaining else ""
            print(f"[STT] {_label(candidate)} not possible ({unavailable}){following}", flush=True)
    raise GpuUnavailable("; ".join(f"{entry['tried']}: {entry['reason']}" for entry in skipped))


@app.route("/transcribe", methods=["POST"])
def transcribe():
    """Transcribe audio file.

    Form data:
        file:         Audio file (WAV, MP3, M4A, OGG, FLAC, WebM)
        device:       "cpu" or "cuda" (default: "cpu"): exactly the configured model of that
                      device, or a 503 when the GPU has no room for it. "auto": the configured
                      GPU model, then smaller ones, then the configured CPU model; the answer
                      lists what could not take it (`fallback`).
        language:     Language code, e.g. "de", "en" (default: from config)
        diarize:      "1" to label speakers (default: off — pointless for
                      short voice commands, only worth it for interviews)
        num_speakers: Optional hint if the speaker count is known
        engine:       "whisper" or "parakeet" (default: the engine configured for the device)
        model:        Whisper only — a size from `available_models` for this request
                      (default: cpu_model / gpu_model, by device)
        quality:      Parakeet only — "fp32" or "int8" for this request
                      (default: cpu_quality / gpu_quality, by device)
    """
    if "file" not in request.files:
        return jsonify({"error": "No file provided"}), 400

    audio_file = request.files["file"]
    device = request.form.get("device", "cpu")
    language = request.form.get("language", _config["language"])
    diarize = request.form.get("diarize", "0") in ("1", "true", "True")
    try:
        num_speakers = int(request.form.get("num_speakers", "0"))
    except ValueError:
        num_speakers = 0
    # Request wins; otherwise the configured default (UI: "Sprecheranzahl").
    if num_speakers <= 0:
        num_speakers = _config["num_speakers"]

    engine = request.form.get("engine") or None
    model = request.form.get("model") or None
    quality = request.form.get("quality") or None
    for name, value, allowed in (("device", device, DEVICES), ("engine", engine, ENGINES),
                                 ("model", model, AVAILABLE_MODELS), ("quality", quality, PARAKEET_QUALITIES)):
        if value is not None and value not in allowed:
            return jsonify({"error": f"Invalid {name}: {value}. Use one of {allowed}"}), 400
    if device == "auto" and (engine or model or quality):
        return jsonify({"error": "engine, model and quality do not apply to device=auto: it follows the auto list"}), 400
    plan = _plan(device, engine, model, quality)

    _log_client("transcribe", device=device, engine=plan[0]["engine"], diarize=str(diarize))

    suffix = Path(audio_file.filename or "audio.wav").suffix or ".wav"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        audio_file.save(tmp)
        tmp_path = tmp.name

    work_path = tmp_path
    try:
        # With diarization both engines need the same 16 kHz mono audio —
        # decode it once here instead of twice in parallel (SSOT for the
        # decoded audio). Without diarization Whisper decodes internally as
        # before, so nothing changes for the common short-command case.
        # Parakeet (onnx-asr) reads only WAV, so it always gets the decoded file.
        parakeet_possible = any(candidate["engine"] == "parakeet" for candidate in plan)
        if diarize or parakeet_possible:
            work_path = _decode_to_wav16k(tmp_path)
            if parakeet_possible and work_path == tmp_path:
                return jsonify({"error": "Audio could not be decoded to 16 kHz WAV"}), 500

        # Queue diarization FIRST so it runs on its own card while Whisper
        # transcribes — the two jobs never touch, so the wall-clock cost of
        # speaker labels is close to zero. It needs the decoded WAV (the
        # worker reads PCM directly instead of using pyannote's decoder), so
        # a failed conversion means no speaker labels — never a broken run.
        wav_ready = diarize and work_path != tmp_path
        diarize_queued = _diarize_submit(work_path, num_speakers) if wav_ready else False
        if diarize and not diarize_queued:
            print("[Diarize] Could not start — returning plain transcript", flush=True)

        try:
            result, skipped = _run_plan(plan, work_path, language, diarize_queued)
        except GpuUnavailable as unavailable:
            # 503 (not 500): tells the client "the GPU cannot take this model now" — distinct from
            # real errors. Which device to use instead is the client's choice (or `device=auto`).
            if diarize_queued:
                _diarize_collect(timeout=10)  # drain, worker already ran
            return jsonify({"error": f"GPU unavailable: {unavailable}"}), 503
        if "error" in result:
            return jsonify({"error": result["error"]}), 500

        if diarize_queued:
            dia = _diarize_collect(timeout=_GPU_RESULT_TIMEOUT_S)
            segments = result.pop("segments", None)
            if dia and "turns" in dia and segments:
                merged = _smooth_speakers(_merge_speakers(segments, dia["turns"]))
                result["text_speakers"] = _format_speaker_text(merged)
                result["speakers"] = dia["speakers"]
                result["diarize_time"] = dia["time"]
            else:
                # Diarization failed — the plain transcript is still valid,
                # so report the reason instead of failing the whole request.
                result["diarize_error"] = (dia or {}).get("error", "no diarization result")
        result.pop("segments", None)
        if skipped:
            result["fallback"] = skipped

        return jsonify(result)
    finally:
        Path(tmp_path).unlink(missing_ok=True)
        if work_path != tmp_path:
            Path(work_path).unlink(missing_ok=True)


@app.route("/status", methods=["GET"])
def status():
    """Detailed status including memory usage and full config."""
    loaded = _loaded()
    gpu_alive = loaded["gpu"] is not None
    diarize_alive = _diarize_process is not None and _diarize_process.is_alive()
    result = {
        **_config,
        # What is in memory now (a configured model can differ: it loads on the next request).
        "cpu_loaded": loaded["cpu"] is not None,
        "cpu_loaded_engine": loaded["cpu"]["engine"] if loaded["cpu"] else None,
        "cpu_loaded_model": loaded["cpu"]["model"] if loaded["cpu"] else None,
        "gpu_loaded": gpu_alive,
        "gpu_loaded_engine": loaded["gpu"]["engine"] if gpu_alive else None,
        "gpu_loaded_model": loaded["gpu"]["model"] if gpu_alive else None,
        "gpu_busy": _gpu_busy,
        "gpu_device_index": _gpu_device_index,
        "gpu_worker_pid": _gpu_process.pid if gpu_alive else None,
        # Which card holds what, by UUID (indices differ between machines and containers), and what the
        # worker took there right after loading. GPU-Orc reads these to free cards without guessing.
        "gpu_uuid": _gpu_uuid if gpu_alive else None,
        "gpu_footprint_mib": _gpu_footprint_mib if gpu_alive else None,
        "diarize_loaded": diarize_alive,
        "diarize_gpu_uuid": _diarize_uuid if diarize_alive else None,
        # SSOT for clients building a dropdown (AIfred STT tab, the web page).
        "available_models": AVAILABLE_MODELS,
        "engines": ENGINES,
        "parakeet_qualities": PARAKEET_QUALITIES,
        "devices": DEVICES,
        # Free VRAM a card needs per model before it is loaded there (the margin comes on top), and which
        # of those a burn-in measured on this machine (the others are estimates).
        "vram_needed_mib": _VRAM_MIB,
        "vram_measured": _VRAM_MEASURED,
    }

    if _last_gpu_request > 0:
        idle = time.time() - _last_gpu_request
        result["gpu_idle_seconds"] = round(idle, 0)
        if _config["gpu_ttl_minutes"] > 0:
            result["gpu_ttl_remaining_seconds"] = round(
                max(0, _config["gpu_ttl_minutes"] * 60 - idle), 0)

    return jsonify(result)


@app.route("/burn-in", methods=["GET"])
def burn_in_status():
    """How the measurement stands: running, the model it works on, the results so far, an error."""
    return jsonify(_burn_in)


@app.route("/burn-in", methods=["POST"])
def burn_in_start():
    """Measure what models need on a GPU. Form data: `file` (speech of a minute or more, so that the
    longest piece Parakeet works on is in it) and optionally `entries`, a JSON list of
    {"engine", "model"} (default: every Whisper size and Parakeet precision). It runs in the
    background (GET /burn-in); each result replaces the estimate in the table. The GPU model in memory
    is released for it, and nothing else uses the GPU meanwhile (409 for a second burn-in or while the
    GPU is transcribing)."""
    if "file" not in request.files:
        return jsonify({"error": "No file provided"}), 400
    try:
        requested = json.loads(request.form["entries"]) if "entries" in request.form else None
    except ValueError:
        return jsonify({"error": "entries is no JSON"}), 400
    if requested is None:
        entries = [("whisper", size) for size in AVAILABLE_MODELS] + [("parakeet", quality) for quality in PARAKEET_QUALITIES]
    else:
        entries = [(entry.get("engine"), entry.get("model")) for entry in requested]
        for engine, model in entries:
            if engine not in ENGINES or model not in (PARAKEET_QUALITIES if engine == "parakeet" else AVAILABLE_MODELS):
                return jsonify({"error": f"Invalid entry: {engine} {model}"}), 400
    if _burn_in["running"]:
        return jsonify({"error": "A burn-in is running"}), 409
    if _gpu_busy:
        return jsonify({"error": "The GPU is transcribing"}), 409
    _burn_in.update(running=True, step="", results=[], error=None)
    suffix = Path(request.files["file"].filename or "audio.wav").suffix or ".wav"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        request.files["file"].save(tmp)
    wav_path = _decode_to_wav16k(tmp.name)
    if wav_path != tmp.name:
        Path(tmp.name).unlink(missing_ok=True)
    threading.Thread(target=_burn_in_job, args=(wav_path, entries, _config["language"]), daemon=True,
                     name="burn-in").start()
    return jsonify({"started": [f"{engine} {model}" for engine, model in entries]}), 202


@app.route("/unload", methods=["POST"])
def unload():
    """Unload the CPU models. The GPU workers are released through /gpu/release only (one way)."""
    global _model_cpu, _parakeet_cpu, _parakeet_cpu_quality
    if request.args:
        return jsonify({"error": "/unload takes no parameters; GPU workers: POST /gpu/release"}), 400
    unloaded = []
    _log_client("unload")
    if _model_cpu is not None:
        _model_cpu = None
        gc.collect()
        unloaded.append("cpu")
    if _parakeet_cpu is not None:
        _parakeet_cpu = None
        _parakeet_cpu_quality = ""
        gc.collect()
        unloaded.append("parakeet-cpu")

    return jsonify({"success": True, "unloaded": unloaded})


# The suites' common GPU release API (also TTS-Suite; GPU-Orc uses it): what the service holds on the
# GPUs of this machine, and releasing it. Released means the worker process is proven ended.
_GPU_ITEMS = ("gpu-worker", "diarize-worker")


@app.route("/gpu/holders", methods=["GET"])
def gpu_holders():
    """The GPU workers that run, with the card each was started on (from its configuration, always
    known), the memory the GPU worker took when loading, and whether it works right now."""
    holders = []
    if _gpu_process is not None and _gpu_process.is_alive():
        holders.append({
            "item": "gpu-worker",
            "gpu_uuids": [_gpu_uuid],
            "memory_mib": {_gpu_uuid: _gpu_footprint_mib} if _gpu_footprint_mib else None,
            "busy": _gpu_busy,
        })
    if _diarize_process is not None and _diarize_process.is_alive():
        holders.append({"item": "diarize-worker", "gpu_uuids": [_diarize_uuid], "memory_mib": None,
                        "busy": _diarize_busy})
    return jsonify({"holders": holders})


@app.route("/gpu/release", methods=["POST"])
def gpu_release():
    """Release GPU workers: ``{"items": [...] | "all", "force": bool, "reason": str}``.

    One result per item: ``released`` (the worker process is proven ended), ``not_running``, ``busy``
    (working and left alone, only without force) or ``failed`` (the end could not be proven). HTTP 200
    when everything is released or was not running, 409 when an item is busy, 500 when one failed.
    With force a transcription in flight is told the reason and counts as "GPU unavailable".
    """
    body = request.get_json(silent=True)
    if not isinstance(body, dict) or set(body) - {"items", "force", "reason"}:
        return jsonify({"error": "body: {items, force, reason}"}), 400
    items, force, reason = body.get("items"), body.get("force", False), body.get("reason")
    if items == "all":
        items = list(_GPU_ITEMS)
    if (not isinstance(items, list) or not items or len(set(items)) != len(items)
            or any(item not in _GPU_ITEMS for item in items)):
        return jsonify({"error": f'items: "all" or a list of {list(_GPU_ITEMS)}'}), 400
    if not isinstance(force, bool) or not isinstance(reason, str) or not reason.strip():
        return jsonify({"error": "force must be true or false, and reason is required"}), 400
    _log_client("gpu/release", items=",".join(items), force=str(force), reason=reason)
    results = [_release_gpu_item(item, force, reason) for item in items]
    outcomes = {result["outcome"] for result in results}
    status = 500 if "failed" in outcomes else 409 if "busy" in outcomes else 200
    return jsonify({"results": results}), status


def _release_gpu_item(item: str, force: bool, reason: str) -> dict:
    if item == "gpu-worker":
        lock, kill = _gpu_lock, _kill_gpu_worker
    else:
        lock, kill = _diarize_lock, _kill_diarize_worker
    with lock:
        process = _gpu_process if item == "gpu-worker" else _diarize_process
        busy = _gpu_busy if item == "gpu-worker" else _diarize_busy
        if process is None or not process.is_alive():
            return {"item": item, "outcome": "not_running", "detail": "nothing runs"}
        if busy and not force:
            return {"item": item, "outcome": "busy", "detail": "a job is running on it"}
        still_running = kill(withdrawn=reason)
    if still_running:
        return {"item": item, "outcome": "failed", "detail": still_running}
    return {"item": item, "outcome": "released", "detail": f"PID {process.pid} ended"}


# The settings that name one of a fixed set of values, and the set.
_CHOICES = {
    "cpu_engine": ENGINES, "gpu_engine": ENGINES,
    "cpu_model": AVAILABLE_MODELS, "gpu_model": AVAILABLE_MODELS,
    "cpu_quality": PARAKEET_QUALITIES, "gpu_quality": PARAKEET_QUALITIES,
}
# The other settings, and how to bring a received value into its type.
_NUMBERS = {
    "gpu_ttl_minutes": lambda value: max(0, int(value)),
    "beam_size": lambda value: max(1, min(20, int(value))),
    "num_speakers": lambda value: max(0, min(10, int(value))),
    "vram_margin_mib": lambda value: max(0, int(value)),
}
_FLAGS = ("vad_filter", "condition_on_previous_text")
_TEXTS = ("language", "initial_prompt")


def _valid_auto_chain(chain: object) -> str | None:
    """Why this is no auto list (None when it is one): a list of entries with a device, an engine, a
    model that engine has — a Whisper size or a Parakeet precision — and whether it is switched on;
    at least one on."""
    if not isinstance(chain, list) or not any(isinstance(entry, dict) and entry.get("enabled") is True for entry in chain):
        return "auto_chain must be a list with at least one entry that is enabled"
    for entry in chain:
        if not isinstance(entry, dict) or set(entry) != {"device", "engine", "model", "enabled"}:
            return f"auto_chain entry {entry!r}: needs device, engine, model and enabled"
        models = PARAKEET_QUALITIES if entry["engine"] == "parakeet" else AVAILABLE_MODELS
        if (entry["device"] not in ("cpu", "cuda") or entry["engine"] not in ENGINES or entry["model"] not in models
                or not isinstance(entry["enabled"], bool)):
            return (f"auto_chain entry {entry!r}: device is cpu or cuda, engine one of {ENGINES}, model a Whisper "
                    f"size {AVAILABLE_MODELS} or a Parakeet precision {PARAKEET_QUALITIES}, enabled true or false")
    return None


@app.route("/config", methods=["GET", "POST"])
def config_endpoint():
    """Get or update runtime configuration. A setting this service does not know, or a value
    outside its set, is answered with 400 and nothing is changed."""
    if request.method == "GET":
        return jsonify({**_config, "available_models": AVAILABLE_MODELS, "engines": ENGINES,
                        "parakeet_qualities": PARAKEET_QUALITIES, "devices": DEVICES,
                        "vram_needed_mib": _VRAM_MIB, "vram_measured": _VRAM_MEASURED})

    data = request.get_json(silent=True) or {}
    unknown = sorted(set(data) - set(_CHOICES) - set(_NUMBERS) - set(_FLAGS) - set(_TEXTS) - {"auto_chain"})
    if unknown:
        return jsonify({"error": f"Unknown settings: {unknown}"}), 400
    for key, allowed in _CHOICES.items():
        if key in data and data[key] not in allowed:
            return jsonify({"error": f"Invalid {key}: {data[key]!r}. Use one of {allowed}"}), 400
    if "auto_chain" in data and (invalid := _valid_auto_chain(data["auto_chain"])):
        return jsonify({"error": invalid}), 400
    try:
        received = {key: _NUMBERS[key](data[key]) for key in _NUMBERS if key in data}
    except (TypeError, ValueError) as error:
        return jsonify({"error": f"Not a number: {error}"}), 400
    received |= {key: data[key] for key in _CHOICES if key in data}
    received |= {key: bool(data[key]) for key in _FLAGS if key in data}
    received |= {key: str(data[key]) for key in _TEXTS if key in data}
    if "auto_chain" in data:
        received["auto_chain"] = data["auto_chain"]

    changed = [key for key, value in received.items() if _config[key] != value]
    _config.update(received)
    if changed:
        _save_config()
    if "gpu_ttl_minutes" in changed:
        _rearm_gpu_ttl()
    # The CPU model stays resident: a changed engine, model or precision loads now, so the
    # next request does not wait for it.
    if EAGER_LOAD and {"cpu_engine", "cpu_model", "cpu_quality"} & set(changed):
        threading.Thread(target=_preload_cpu, daemon=True, name="cpu-preload").start()

    print(f"[STT] Config updated: {', '.join(changed)}", flush=True)
    return jsonify({"success": True, "changed": changed, "config": _config})


# ── Startup ──────────────────────────────────────────────────

_invalid = {key: _config[key] for key, allowed in _CHOICES.items() if _config[key] not in allowed}
if _invalid:
    raise SystemExit(f"Invalid settings {_invalid}: " + ", ".join(f"{key} one of {allowed}" for key, allowed in _CHOICES.items()))

def _preload_cpu():
    """Load the CPU model of the configured engine now and drop the other engine's CPU
    model, which would only sit in RAM. SSOT for the start and for config changes."""
    global _model_cpu, _cpu_model_name, _parakeet_cpu, _parakeet_cpu_quality
    if _config["cpu_engine"] == "parakeet":
        with _cpu_lock:
            _model_cpu, _cpu_model_name = None, ""
        with _parakeet_cpu_lock:
            if _parakeet_cpu is None or _parakeet_cpu_quality != _config["cpu_quality"]:
                _parakeet_cpu = _load_parakeet(_config["cpu_quality"], "CPUExecutionProvider")
                _parakeet_cpu_quality = _config["cpu_quality"]
    else:
        with _parakeet_cpu_lock:
            _parakeet_cpu, _parakeet_cpu_quality = None, ""
        with _cpu_lock:
            _ensure_cpu_model(_config["cpu_model"])


if EAGER_LOAD:
    def _eager_load():
        time.sleep(2)
        _preload_cpu()
    threading.Thread(target=_eager_load, daemon=True).start()

print(f'[STT] Server starting — cpu={_config["cpu_engine"]} {_config["cpu_model"]}/{_config["cpu_quality"]}, '
      f'gpu={_config["gpu_engine"]} {_config["gpu_model"]}/{_config["gpu_quality"]}, '
      f'eager_load={EAGER_LOAD}, gpu_ttl={_config["gpu_ttl_minutes"]}min', flush=True)
