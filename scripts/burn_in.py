#!/usr/bin/env python3
"""Measures how much VRAM each model needs and writes it into vram_mib.json.

For every Whisper size and Parakeet precision this loads the model on a GPU through the running
service (`device=cuda` with the model named in the request), transcribes one audio file while
sampling `nvidia-smi`, and takes the rise of the used memory on the card the worker sits on. Run it on
the machine that has the GPUs (it calls `nvidia-smi`), with a file that is a minute or more long (Parakeet
cuts its work into 60 s pieces: the peak needs a full one), and while nothing else loads onto that card:
another program growing there during a run is counted too. The GPU model the service holds is released
first and after each model.

    scripts/burn_in.py --audio speech.wav [--url http://localhost:5080] [--margin-mib 256] [--write]
"""

from __future__ import annotations

import argparse
import json
import subprocess
import threading
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

SAMPLE_INTERVAL_S = 0.1


def card_used_mib() -> dict[int, int]:
    """Used VRAM (MiB) per card, by nvidia-smi index."""
    output = subprocess.run(
        ["nvidia-smi", "--query-gpu=index,memory.used", "--format=csv,noheader,nounits"],
        capture_output=True, text=True, check=True, timeout=10,
    ).stdout
    return {int(index): int(used) for index, used in (line.split(",") for line in output.strip().splitlines())}


def request(url: str, method: str = "GET", form: dict[str, str] | None = None, audio: Path | None = None) -> dict:
    """One call to the service; a multipart form when `audio` is given."""
    if audio is None:
        call = urllib.request.Request(url, method=method)
    else:
        boundary = uuid.uuid4().hex
        parts = [f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode()
                 for name, value in (form or {}).items()]
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{audio.name}"\r\n'
                     "Content-Type: application/octet-stream\r\n\r\n".encode() + audio.read_bytes() + b"\r\n")
        parts.append(f"--{boundary}--\r\n".encode())
        call = urllib.request.Request(url, data=b"".join(parts), method="POST",
                                      headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    try:
        with urllib.request.urlopen(call, timeout=1800) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        return {"error": error.read().decode(errors="replace"), "http": error.code}


def measure(base: str, audio: Path, entry: dict[str, str]) -> tuple[int | None, str]:
    """The rise of the used VRAM on the card the model lands on (MiB), and what the service said."""
    request(f"{base}/unload?device=gpu", "POST")
    time.sleep(2)  # the card has let go of the memory
    before = card_used_mib()
    peaks = dict(before)
    stop = threading.Event()

    def sample() -> None:
        while not stop.is_set():
            for index, used in card_used_mib().items():
                peaks[index] = max(peaks[index], used)
            time.sleep(SAMPLE_INTERVAL_S)

    sampler = threading.Thread(target=sample, daemon=True)
    sampler.start()
    answer = request(f"{base}/transcribe", "POST", {"device": "cuda", **entry}, audio)
    stop.set()
    sampler.join()
    if "error" in answer:
        return None, answer["error"]
    card = request(f"{base}/status")["gpu_device_index"]
    return peaks[card] - before[card], f"card {card}, {answer['time']} s"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--audio", type=Path, required=True, help="a speech file of a minute or more")
    parser.add_argument("--url", default="http://localhost:5080", help="the STT-Suite service")
    parser.add_argument("--margin-mib", type=int, default=256, help="added to each measurement before it is written")
    parser.add_argument("--vram-file", type=Path, default=Path(__file__).parent.parent / "vram_mib.json")
    parser.add_argument("--write", action="store_true", help="write the measured values into the file (else only print)")
    arguments = parser.parse_args()

    status = request(f"{arguments.url}/status")
    entries = [("whisper", size, {"engine": "whisper", "model": size}) for size in status["available_models"]]
    entries += [("parakeet", precision, {"engine": "parakeet", "quality": precision})
                for precision in status["parakeet_qualities"]]
    table = json.loads(arguments.vram_file.read_text(encoding="utf-8"))
    print(f"{'engine':9} {'model':9} {'measured MiB':>13}  note")
    for engine, name, entry in entries:
        used, note = measure(arguments.url, arguments.audio, entry)
        print(f"{engine:9} {name:9} {used if used is not None else '-':>13}  {note}")
        if used is not None:
            table[engine][name] = used + arguments.margin_mib
    request(f"{arguments.url}/unload?device=gpu", "POST")
    if arguments.write:
        arguments.vram_file.write_text(json.dumps(table, indent=2) + "\n", encoding="utf-8")
        print(f"written to {arguments.vram_file} (measurement plus {arguments.margin_mib} MiB); rebuild the container to use it")


if __name__ == "__main__":
    main()
