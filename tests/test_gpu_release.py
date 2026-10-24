"""The suites' common GPU release API: /gpu/holders and /gpu/release; /unload is for the CPU models only."""
from collections.abc import Iterator
from typing import Any

import pytest

import server


class FakeProcess:
    """A worker process; a stubborn one survives SIGKILL (the end cannot be proven)."""

    def __init__(self, pid: int, stubborn: bool = False) -> None:
        self.pid = pid
        self.alive = True
        self.stubborn = stubborn

    def kill(self) -> None:
        if not self.stubborn:
            self.alive = False

    def join(self, timeout: float | None = None) -> None:
        pass

    def is_alive(self) -> bool:
        return self.alive


class FakeQueue:
    def __init__(self) -> None:
        self.closed = False
        self.items: list[Any] = []

    def close(self) -> None:
        self.closed = True

    def put(self, item: Any) -> None:
        self.items.append(item)


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    for name, value in {
        "_gpu_process": None, "_gpu_busy": False, "_gpu_uuid": "", "_gpu_footprint_mib": 0,
        "_gpu_request_queue": None, "_gpu_result_queue": None,
        "_diarize_process": None, "_diarize_busy": False, "_diarize_uuid": "",
        "_diarize_request_queue": None, "_diarize_result_queue": None,
    }.items():
        monkeypatch.setattr(server, name, value)
    server.app.testing = True
    with server.app.test_client() as test_client:
        yield test_client


def gpu_worker(monkeypatch: pytest.MonkeyPatch, busy: bool = False, stubborn: bool = False) -> FakeQueue:
    results = FakeQueue()
    monkeypatch.setattr(server, "_gpu_process", FakeProcess(4711, stubborn))
    monkeypatch.setattr(server, "_gpu_busy", busy)
    monkeypatch.setattr(server, "_gpu_uuid", "GPU-bbbb")
    monkeypatch.setattr(server, "_gpu_footprint_mib", 3100)
    monkeypatch.setattr(server, "_gpu_request_queue", FakeQueue())
    monkeypatch.setattr(server, "_gpu_result_queue", results)
    return results


def release(client: Any, **body: Any) -> tuple[int, list[tuple[str, str]]]:
    response = client.post("/gpu/release", json={"items": "all", "force": False, "reason": "test", **body})
    return response.status_code, [(r["item"], r["outcome"]) for r in response.get_json()["results"]]


def test_holders_name_the_card_the_memory_and_whether_it_works(client: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    assert client.get("/gpu/holders").get_json() == {"holders": []}
    gpu_worker(monkeypatch, busy=True)
    monkeypatch.setattr(server, "_diarize_process", FakeProcess(4712))
    monkeypatch.setattr(server, "_diarize_uuid", "GPU-cccc")
    assert client.get("/gpu/holders").get_json() == {"holders": [
        {"item": "gpu-worker", "gpu_uuids": ["GPU-bbbb"], "memory_mib": {"GPU-bbbb": 3100}, "busy": True},
        {"item": "diarize-worker", "gpu_uuids": ["GPU-cccc"], "memory_mib": None, "busy": False},
    ]}


def test_nothing_running(client: Any) -> None:
    assert release(client) == (200, [("gpu-worker", "not_running"), ("diarize-worker", "not_running")])


def test_busy_worker_is_left_alone_without_force(client: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    gpu_worker(monkeypatch, busy=True)
    assert release(client, items=["gpu-worker"]) == (409, [("gpu-worker", "busy")])
    assert server._gpu_process is not None and server._gpu_process.is_alive()


def test_force_ends_a_busy_worker_and_tells_the_transcription(client: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    results = gpu_worker(monkeypatch, busy=True)
    assert release(client, items=["gpu-worker"], force=True, reason="loading X") == (200, [("gpu-worker", "released")])
    assert server._gpu_process is None and server._gpu_uuid == ""
    # The waiting /transcribe reads the reason and treats it as "GPU unavailable".
    assert results.items == [{"withdrawn": "loading X"}] and not results.closed


def test_idle_worker_released_and_queues_closed(client: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    results = gpu_worker(monkeypatch)
    assert release(client, items=["gpu-worker"]) == (200, [("gpu-worker", "released")])
    assert results.closed and results.items == []


def test_a_worker_that_survives_the_kill_is_failed_not_released(client: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    gpu_worker(monkeypatch, stubborn=True)
    monkeypatch.setattr(server, "_WORKER_END_TIMEOUT_S", 0)
    status, outcomes = release(client, items=["gpu-worker"])
    assert (status, outcomes) == (500, [("gpu-worker", "failed")])
    # The state stays: the worker still holds its card and is still reported.
    assert server._gpu_process is not None and client.get("/gpu/holders").get_json()["holders"]


@pytest.mark.parametrize("body", [
    {"items": "all"},                                              # no reason
    {"items": ["gpu-worker"], "force": "yes", "reason": "r"},
    {"items": ["cpu"], "reason": "r"},
    {"items": [], "reason": "r"},
    {"items": "all", "reason": "r", "extra": 1},
])
def test_bad_release_requests(client: Any, body: dict[str, Any]) -> None:
    assert client.post("/gpu/release", json=body).status_code == 400


def test_unload_is_for_the_cpu_models_only(client: Any) -> None:
    assert client.post("/unload?device=cuda").status_code == 400
    response = client.post("/unload")
    assert response.status_code == 200 and response.get_json()["success"] is True


class AnsweringQueue(FakeQueue):
    def __init__(self, answer: dict[str, Any]) -> None:
        super().__init__()
        self.answer = answer

    def get(self, timeout: float | None = None) -> dict[str, Any]:
        return self.answer


def test_a_withdrawn_gpu_counts_as_unavailable_so_auto_goes_on_on_the_cpu(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(server, "_gpu_process", FakeProcess(4711))
    monkeypatch.setattr(server, "_gpu_engine", "whisper")
    monkeypatch.setattr(server, "_gpu_model_name", "large-v3")
    monkeypatch.setattr(server, "_gpu_request_queue", FakeQueue())
    monkeypatch.setattr(server, "_gpu_result_queue", AnsweringQueue({"withdrawn": "GPU-Orc: loading X"}))
    with pytest.raises(server.GpuUnavailable, match="withdrawn: GPU-Orc: loading X"):
        server._transcribe_gpu("audio.wav", "de", False, "whisper", "large-v3", "fp32")
    assert server._gpu_busy is False
