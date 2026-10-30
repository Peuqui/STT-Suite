"""Card choice of the GPU workers: unused card first, then the most free VRAM; and the lock shared with the TTS-Suite."""
import fcntl
import io
import json
import multiprocessing
import queue
import subprocess
import threading
import time
import types
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

import server


def _nvidia_smi(monkeypatch: pytest.MonkeyPatch, *cards: tuple[int, int, int, float]) -> None:
    """Cards as (index, free MiB, used MiB, compute capability) in the answer of nvidia-smi."""
    lines = "\n".join(f"{index}, GPU-{index}, {free}, {used}, Card {index}, {cap}" for index, free, used, cap in cards)
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: subprocess.CompletedProcess(args, 0, lines, ""))


def test_a_card_nobody_uses_comes_before_the_one_with_the_most_free_vram(monkeypatch: pytest.MonkeyPatch) -> None:
    _nvidia_smi(monkeypatch, (0, 40000, 8000, 7.5), (1, 30000, 0, 7.0))
    assert server._find_best_gpu(min_vram_mib=4000) == (1, "GPU-1")


def test_without_an_unused_card_the_most_free_vram_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    _nvidia_smi(monkeypatch, (0, 20000, 28000, 7.5), (1, 30000, 2000, 7.0), (2, 10000, 38000, 7.5))
    assert server._find_best_gpu(min_vram_mib=4000) == (1, "GPU-1")


def test_among_unused_cards_the_one_with_the_most_free_vram_and_then_the_lower_index_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    _nvidia_smi(monkeypatch, (0, 30000, 0, 7.0), (1, 40000, 0, 7.5), (2, 40000, 0, 7.5))
    assert server._find_best_gpu(min_vram_mib=4000) == (1, "GPU-1")


def test_a_card_without_room_for_the_model_is_not_chosen(monkeypatch: pytest.MonkeyPatch) -> None:
    _nvidia_smi(monkeypatch, (0, 3000, 0, 7.5), (1, 9000, 20000, 7.0))
    assert server._find_best_gpu(min_vram_mib=4000) == (1, "GPU-1")
    assert server._find_best_gpu(min_vram_mib=12000) is None


def test_the_excluded_card_and_cards_of_too_old_a_generation_are_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    _nvidia_smi(monkeypatch, (0, 30000, 0, 7.0), (1, 20000, 0, 7.5), (2, 10000, 0, 8.0))
    assert server._find_best_gpu(min_vram_mib=4000, exclude_uuid="GPU-1") == (0, "GPU-0")
    assert server._find_best_gpu(min_vram_mib=4000, min_compute_cap=7.5) == (1, "GPU-1")


def test_a_model_is_not_started_when_no_card_has_room_even_after_the_loaded_one_lets_go(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(server, "_free_per_card_mib", lambda: {0: 1000, 1: 2000})
    monkeypatch.setattr(server, "_gpu_device_index", 1)
    monkeypatch.setattr(server, "_gpu_footprint_mib", 3000)
    assert server._room_after_release(need=4500) is None  # card 1 has 5000 once the worker lets go
    assert server._room_after_release(need=6000) is not None


def test_without_a_lock_file_choosing_does_not_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(server, "_PLACEMENT_LOCK_FILE", "")
    with server._placement_lock():
        pass


def test_the_second_program_waits_for_the_first_to_let_go_of_the_lock(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    lock = tmp_path / "placement.lock"
    monkeypatch.setattr(server, "_PLACEMENT_LOCK_FILE", str(lock))
    entered = threading.Event()

    def choose_card() -> None:
        with server._placement_lock():
            entered.set()

    with lock.open("a") as other_program:  # the TTS-Suite, say, choosing its card
        fcntl.flock(other_program, fcntl.LOCK_EX)
        waiter = threading.Thread(target=choose_card)
        waiter.start()
        time.sleep(0.3)
        assert not entered.is_set()
    waiter.join(timeout=5)
    assert entered.is_set()


# ---- The shared placement lock and forked workers ----------------------------------------


def _like_a_worker(ready: Any, stop: Any) -> None:
    server._drop_inherited_placement_lock()
    ready.set()
    stop.wait(10)


def test_a_worker_forked_inside_the_placement_lock_does_not_keep_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Found 25.10.: the STT GPU worker kept the lock for its whole life, the TTS-Suite could not start an engine.
    lock = tmp_path / "placement.lock"
    monkeypatch.setattr(server, "_PLACEMENT_LOCK_FILE", str(lock))
    fork = multiprocessing.get_context("fork")
    ready, stop = fork.Event(), fork.Event()
    with server._placement_lock():
        worker = fork.Process(target=_like_a_worker, args=(ready, stop))
        worker.start()
        assert ready.wait(5)
    try:
        with open(lock, "a") as other:  # another program: gets the lock while the worker still lives
            fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert worker.is_alive()
    finally:
        stop.set()
        worker.join(5)


class _Dropped(Exception):
    pass


@pytest.mark.parametrize("worker", ["_gpu_worker", "_diarize_worker"])
def test_both_workers_drop_the_inherited_lock_before_anything_else(worker: str, monkeypatch: pytest.MonkeyPatch) -> None:
    def drop() -> None:
        raise _Dropped

    monkeypatch.setattr(server, "_drop_inherited_placement_lock", drop)
    with pytest.raises(_Dropped):
        getattr(server, worker)(None, None, 0, "GPU-x", "model", "")


class _GpuOrc:
    """What ``urlopen`` gets for ``GET /loading``: an answer, or the error it raises."""

    def __init__(self, answer: object) -> None:
        self.answer = answer
        self.asked: list[Any] = []

    def urlopen(self, request: Any, timeout: float) -> Any:
        self.asked.append(request)
        if isinstance(self.answer, Exception):
            raise self.answer
        return io.BytesIO(json.dumps(self.answer).encode())


@pytest.fixture
def gpu_orc(monkeypatch: pytest.MonkeyPatch) -> Callable[[object], _GpuOrc]:
    def install(answer: object) -> _GpuOrc:
        fake = _GpuOrc(answer)
        monkeypatch.setattr(server, "_GPU_ORC_URL", "http://orc:5070")
        monkeypatch.setattr(server, "_GPU_ORC_TOKEN_FILE", "")
        monkeypatch.setattr(urllib.request, "urlopen", fake.urlopen)
        return fake
    return install


def test_without_a_gpu_orc_nothing_stops_a_start(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(server, "_GPU_ORC_URL", "")
    assert server._loading_refusal() is None


def test_a_green_light_lets_a_start_go_on(gpu_orc: Callable[[object], _GpuOrc]) -> None:
    fake = gpu_orc({"loading": []})
    assert server._loading_refusal() is None
    assert fake.asked[0].full_url == "http://orc:5070/loading"


def test_a_loading_model_refuses_and_is_named(gpu_orc: Callable[[object], _GpuOrc]) -> None:
    gpu_orc({"loading": ["DeepSeek-vllm"]})
    assert server._loading_refusal() == "DeepSeek-vllm is loading"


@pytest.mark.parametrize("answer", [OSError("refused"), {"other": 1}])
def test_an_unusable_answer_is_no_green_light(gpu_orc: Callable[[object], _GpuOrc], answer: object) -> None:
    gpu_orc(answer)
    assert "no usable answer" in (server._loading_refusal() or "")


def test_the_token_goes_with_the_question(gpu_orc: Callable[[object], _GpuOrc], monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fake = gpu_orc({"loading": []})
    token = tmp_path / "token"
    token.write_text("secret\n")
    monkeypatch.setattr(server, "_GPU_ORC_TOKEN_FILE", str(token))
    server._loading_refusal()
    assert fake.asked[0].get_header("Authorization") == "Bearer secret"


def test_a_loading_model_keeps_the_gpu_worker_from_starting(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(server, "_loading_refusal", lambda: "DeepSeek-vllm is loading")
    monkeypatch.setattr(server, "_gpu_process", None)
    monkeypatch.setattr(server, "_burn_in", {"running": False})
    monkeypatch.setattr(server, "_start_gpu_worker", lambda *_, **__: pytest.fail("must not start"))
    with pytest.raises(server.GpuUnavailable, match="DeepSeek-vllm is loading"):
        server._transcribe_gpu("a.wav", "de", False, "whisper", "tiny", "fp32")


def test_a_worker_that_already_holds_the_model_works_on_while_another_loads(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(server, "_loading_refusal", lambda: pytest.fail("no new load, nothing to ask"))
    alive = types.SimpleNamespace(is_alive=lambda: True)
    monkeypatch.setattr(server, "_gpu_process", alive)
    monkeypatch.setattr(server, "_gpu_engine", "whisper")
    monkeypatch.setattr(server, "_gpu_model_name", "tiny")
    monkeypatch.setattr(server, "_burn_in", {"running": False})
    queue_in, queue_out = queue.Queue(), queue.Queue()
    queue_out.put({"text": "ok", "time": 0.1})
    monkeypatch.setattr(server, "_gpu_request_queue", queue_in)
    monkeypatch.setattr(server, "_gpu_result_queue", queue_out)
    assert server._transcribe_gpu("a.wav", "de", False, "whisper", "tiny", "fp32")["text"] == "ok"


def test_the_diarization_waits_for_the_green_light_too(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(server, "_loading_refusal", lambda: "DeepSeek-vllm is loading")
    monkeypatch.setattr(server, "_choose_card_and_spawn_diarize_worker", lambda: pytest.fail("must not start"))
    assert server._start_diarize_worker() is False

