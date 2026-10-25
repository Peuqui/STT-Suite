"""Card choice of the GPU workers: unused card first, then the most free VRAM; and the lock shared with the TTS-Suite."""
import fcntl
import multiprocessing
import subprocess
import threading
import time
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
    """GPU-Orc's GET /status for the placement: available_mib per card UUID; records the requests."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, available: dict[str, Any], token_file: str = "",
                 errors: list[dict[str, str]] | None = None) -> None:
        import io
        import json
        import urllib.request
        self.requests: list[Any] = []
        body = json.dumps({"gpus": [{"uuid": uuid, "available_mib": mib} for uuid, mib in available.items()],
                           "errors": errors or []}).encode()

        def urlopen(request: Any, timeout: float) -> io.BytesIO:
            self.requests.append(request)
            return io.BytesIO(body)

        monkeypatch.setattr(urllib.request, "urlopen", urlopen)
        monkeypatch.setattr(server, "_GPU_ORC_URL", "http://host.docker.internal:5070")
        monkeypatch.setattr(server, "_GPU_ORC_TOKEN_FILE", token_file)


def test_with_gpu_orc_a_vllm_models_share_is_not_free(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    token = tmp_path / "token"
    token.write_text("secret\n")
    _nvidia_smi(monkeypatch, (0, 9800, 39000, 7.5), (1, 1100, 31600, 7.0))
    orc = _GpuOrc(monkeypatch, {"GPU-0": 2458, "GPU-1": 1100}, token_file=str(token))
    assert server._find_best_gpu(min_vram_mib=3968) is None             # nvidia-smi alone would take GPU 0
    assert server._find_best_gpu(min_vram_mib=2000) == (0, "GPU-0")
    assert orc.requests[0].full_url == "http://host.docker.internal:5070/status"
    assert orc.requests[0].get_header("Authorization") == "Bearer secret"


def test_a_gpu_orc_that_cannot_say_lets_the_gpu_take_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    _nvidia_smi(monkeypatch, (0, 40000, 0, 7.5))
    _GpuOrc(monkeypatch, {"GPU-0": None}, errors=[{"consumer": "tts-suite", "detail": "down"}])
    assert server._find_best_gpu(min_vram_mib=1000) is None
    assert "GPU-Orc cannot say what is free" in server._no_room(1000)


def test_the_free_vram_per_card_is_capped_by_gpu_orc(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: subprocess.CompletedProcess(
        args, 0, "0, GPU-0, 9800\n1, GPU-1, 30000\n", ""))
    _GpuOrc(monkeypatch, {"GPU-0": 2458, "GPU-1": 31000})
    assert server._free_per_card_mib() == {0: 2458, 1: 30000}
