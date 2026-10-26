"""The card of a GPU worker comes from GPU-Orc (POST /place): the request, its answer and refusals, the model switch."""
import io
import json
import urllib.error
import urllib.request
from email.message import Message
from pathlib import Path
from typing import Any

import pytest

import server

PLACED: dict[str, Any] = {"card": {"index": 2, "uuid": "GPU-cccc"}, "why": None, "reserved_s": 60.0, "actions": [], "duration_s": 0.4}


class _GpuOrc:
    """GPU-Orc for the service: GET /health says caller_timeout_s, POST /place gives ``answer`` (or raises
    ``error``); ``calls`` records (method, url, body, timeout, authorization)."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, answer: dict[str, Any] | None = None,
                 error: OSError | None = None, token_file: str = "", url: str = "http://host.docker.internal:5070") -> None:
        self.answer = answer
        self.error = error
        self.calls: list[tuple[str, str, Any, float, str | None]] = []
        monkeypatch.setattr(urllib.request, "urlopen", self._urlopen)
        monkeypatch.setattr(server, "_GPU_ORC_URL", url)
        monkeypatch.setattr(server, "_GPU_ORC_TOKEN_FILE", token_file)
        monkeypatch.setattr(server, "_GPU_ORC_CONSUMER", "stt-suite")

    def _urlopen(self, request: urllib.request.Request, timeout: float) -> io.BytesIO:
        body = None if request.data is None else json.loads(request.data)  # type: ignore[arg-type]
        self.calls.append((request.get_method(), request.full_url, body, timeout, request.get_header("Authorization")))
        if request.full_url.endswith("/health"):
            return io.BytesIO(json.dumps({"status": "ok", "caller_timeout_s": 210.0}).encode())
        if self.error is not None:
            raise self.error
        return io.BytesIO(json.dumps(self.answer).encode())

    def places(self) -> list[Any]:
        return [call[2] for call in self.calls if call[0] == "POST"]


def test_gpu_orc_names_the_card_with_the_token_and_is_waited_for_as_long_as_it_says(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    token = tmp_path / "token"
    token.write_text("secret\n")
    orc = _GpuOrc(monkeypatch, PLACED, token_file=str(token))
    assert server._place("gpu-worker", 3712, "transcription with parakeet-fp32") == (2, "GPU-cccc")
    health, place = orc.calls
    assert health[:2] == ("GET", "http://host.docker.internal:5070/health")
    assert place == ("POST", "http://host.docker.internal:5070/place",
                     {"consumer": "stt-suite", "item": "gpu-worker", "need_mib": 3712,
                      "reason": "transcription with parakeet-fp32"}, 210.0, "Bearer secret")


def test_the_diarization_worker_asks_with_its_floor_and_the_compute_capability(monkeypatch: pytest.MonkeyPatch) -> None:
    orc = _GpuOrc(monkeypatch, PLACED)
    server._place("diarize-worker", 1500, "speaker diarization", 7.5)
    assert orc.places() == [{"consumer": "stt-suite", "item": "diarize-worker", "need_mib": 1500,
                             "reason": "speaker diarization", "min_compute_cap": 7.5}]


def test_no_card_names_the_reason_of_gpu_orc(monkeypatch: pytest.MonkeyPatch) -> None:
    why = "needs 3712 MiB and 512 MiB headroom, the most room is 1650 MiB on GPU 4"
    _GpuOrc(monkeypatch, {"card": None, "why": why, "reserved_s": None, "actions": [], "duration_s": 0.3})
    with pytest.raises(server.NoCard) as no_card:
        server._place("gpu-worker", 3712, "test")
    assert str(no_card.value) == f"GPU-Orc: {why}"


@pytest.mark.parametrize(("error", "message"), [
    (urllib.error.HTTPError("http://x/place", 404, "Not Found", Message(), io.BytesIO(b'{"error": "no side-model service"}')),
     'refused: 404 {"error": "no side-model service"}'),
    (urllib.error.URLError("connection refused"), "gave no usable answer"),
])
def test_a_gpu_orc_that_refuses_or_does_not_answer_gives_no_card(
    monkeypatch: pytest.MonkeyPatch, error: OSError, message: str,
) -> None:
    _GpuOrc(monkeypatch, error=error)
    with pytest.raises(server.NoCard, match=message):
        server._place("gpu-worker", 3712, "test")


def test_without_gpu_orc_nothing_loads_on_a_gpu(monkeypatch: pytest.MonkeyPatch) -> None:
    orc = _GpuOrc(monkeypatch, PLACED, url="")
    with pytest.raises(server.NoCard, match="no GPU-Orc configured"):
        server._place("gpu-worker", 3712, "test")
    assert orc.calls == []


# ---- The model switch: GPU-Orc is asked before the loaded model is let go -----------------------


class _Worker:
    def __init__(self) -> None:
        self.alive = True

    def is_alive(self) -> bool:
        return self.alive


@pytest.fixture
def loaded_whisper(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """A worker that holds Whisper medium; the returned list records kills and starts."""
    events: list[str] = []
    monkeypatch.setattr(server, "_gpu_process", _Worker())
    monkeypatch.setattr(server, "_gpu_engine", "whisper")
    monkeypatch.setattr(server, "_gpu_model_name", "medium")
    monkeypatch.setattr(server, "_gpu_busy", False)
    monkeypatch.setattr(server, "_burn_in", {"running": False})
    monkeypatch.setattr(server, "_VRAM_MIB", {"whisper": {"medium": 2600, "large-v3": 4200}, "parakeet": {"fp32": 3712}})

    def kill(withdrawn: str = "") -> str:
        events.append("kill")
        return ""

    def start(card: tuple[int, str], engine: str, model: str, quality: str) -> str:
        events.append(f"start {engine} {model} on {card[1]}")
        return "stop here"  # the test ends before a job is sent

    monkeypatch.setattr(server, "_kill_gpu_worker", kill)
    monkeypatch.setattr(server, "_start_gpu_worker", start)
    return events


def test_a_switch_that_gpu_orc_refuses_keeps_the_loaded_model(monkeypatch: pytest.MonkeyPatch, loaded_whisper: list[str]) -> None:
    orc = _GpuOrc(monkeypatch, {"card": None, "why": "no room", "reserved_s": None, "actions": [], "duration_s": 0.1})
    with pytest.raises(server.GpuUnavailable, match="GPU-Orc: no room"):
        server._transcribe_gpu("audio.wav", "de", False, "whisper", "large-v3", "fp32")
    assert loaded_whisper == []
    assert orc.places()[0]["item"] == "gpu-worker" and orc.places()[0]["need_mib"] == 4200


def test_a_switch_lets_the_loaded_model_go_only_after_gpu_orc_named_the_card(
    monkeypatch: pytest.MonkeyPatch, loaded_whisper: list[str],
) -> None:
    _GpuOrc(monkeypatch, PLACED)
    with pytest.raises(server.GpuUnavailable, match="stop here"):
        server._transcribe_gpu("audio.wav", "de", False, "parakeet", "medium", "fp32")
    assert loaded_whisper == ["kill", "start parakeet medium on GPU-cccc"]


def test_the_burn_in_asks_without_a_need(monkeypatch: pytest.MonkeyPatch, loaded_whisper: list[str]) -> None:
    orc = _GpuOrc(monkeypatch, PLACED)
    with pytest.raises(server.GpuUnavailable, match="stop here"):
        server._transcribe_gpu("audio.wav", "de", False, "whisper", "large-v3", "fp32", measuring=True)
    assert orc.places()[0]["need_mib"] == 0 and orc.places()[0]["reason"] == "burn-in"
