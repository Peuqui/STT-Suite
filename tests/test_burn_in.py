"""The catalog and the burn-in: one model at a time, the peak replaces its need, and the page can follow."""
from __future__ import annotations

import threading
import wave

import pytest

import server


@pytest.fixture
def client():
    return server.app.test_client()


@pytest.fixture(autouse=True)
def clean_state(monkeypatch: pytest.MonkeyPatch, tmp_path):
    monkeypatch.setitem(server._config, "headroom_mib", 256)
    monkeypatch.setattr(server, "_burn_in", {"running": False, "engine": None, "model": None, "peak_mib": 0, "error": None})
    monkeypatch.setattr(server, "_gpu_busy", False)
    monkeypatch.setattr(server, "_VRAM_MIB", {"whisper": dict.fromkeys(server.AVAILABLE_MODELS, 1000), "parakeet": dict.fromkeys(server.PARAKEET_QUALITIES, 3000)})
    monkeypatch.setattr(server, "_VRAM_MEASURED", {})
    monkeypatch.setattr(server, "_VRAM_MEASURED_FILE", tmp_path / "vram_measured.json")
    monkeypatch.setattr(server, "_loaded", lambda: {"cpu": None, "gpu": None})


def _no_thread(monkeypatch: pytest.MonkeyPatch) -> list[tuple]:
    started: list[tuple] = []

    class NoThread:
        def __init__(self, target, args, **_: object) -> None:
            self.args = args

        def start(self) -> None:
            started.append(self.args)

    monkeypatch.setattr(threading, "Thread", NoThread)
    return started


def test_the_catalog_names_every_model_with_budget_plus_headroom(client) -> None:
    rows = client.get("/engines").get_json()
    assert [(row["engine"], row["model"]) for row in rows] == (
        [("whisper", size) for size in server.AVAILABLE_MODELS] + [("parakeet", quality) for quality in server.PARAKEET_QUALITIES])
    assert rows[0]["needed_mib"] == 1256
    assert rows[-1]["needed_mib"] == 3256
    assert all(row["measured"] is False and row["loaded_on"] == [] for row in rows)


def test_the_catalog_shows_where_a_model_is_loaded(client, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(server, "_loaded", lambda: {"cpu": {"engine": "parakeet", "model": "parakeet-fp32"},
                                                    "gpu": {"engine": "parakeet", "model": "parakeet-fp32"}})
    loaded = {(row["engine"], row["model"]): row["loaded_on"] for row in client.get("/engines").get_json() if row["loaded_on"]}
    assert loaded == {("parakeet", "fp32"): ["cpu", "gpu"]}


def test_a_burn_in_starts_for_one_model_and_reports_it(client, monkeypatch: pytest.MonkeyPatch) -> None:
    started = _no_thread(monkeypatch)
    answer = client.post("/burn-in", json={"engine": "whisper", "model": "tiny"})
    assert answer.status_code == 202
    assert started == [("whisper", "tiny")]
    assert client.get("/burn-in").get_json() == {"running": True, "engine": "whisper", "model": "tiny", "peak_mib": 0, "error": None}


@pytest.mark.parametrize("body", [{"engine": "whisper", "model": "fp32"}, {"engine": "vosk", "model": "tiny"}, {"engine": "parakeet"}, ["whisper"]])
def test_a_burn_in_needs_a_model_the_engine_has(client, body: object) -> None:
    answer = client.post("/burn-in", json=body)
    assert answer.status_code == 400
    assert answer.get_json()["error"] == "bad_request"
    assert answer.get_json()["detail"]


def test_one_burn_in_at_a_time(client, monkeypatch: pytest.MonkeyPatch) -> None:
    server._burn_in["running"] = True
    answer = client.post("/burn-in", json={"engine": "whisper", "model": "tiny"})
    assert (answer.status_code, answer.get_json()["error"]) == (409, "burn_in_running")


def test_no_burn_in_while_the_gpu_transcribes(client, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(server, "_gpu_busy", True)
    answer = client.post("/burn-in", json={"engine": "whisper", "model": "tiny"})
    assert (answer.status_code, answer.get_json()["error"]) == (409, "gpu_busy")


def test_no_burn_in_while_a_big_model_loads(client, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(server, "_loading_refusal", lambda: "DeepSeek-vllm is loading")
    answer = client.post("/burn-in", json={"engine": "whisper", "model": "tiny"})
    assert (answer.status_code, answer.get_json()["error"]) == (409, "model_loading")


def test_the_peak_replaces_the_estimate_and_the_catalog_shows_it(client, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(server, "_measure", lambda engine, model: 2000)
    monkeypatch.setattr(server, "_kill_gpu_worker", lambda: None)
    server._burn_in["running"] = True
    server._burn_in_job("whisper", "tiny")
    assert server._burn_in == {"running": False, "engine": None, "model": None, "peak_mib": 2000, "error": None}
    row = client.get("/engines").get_json()[0]
    assert (row["needed_mib"], row["measured"]) == (2256, True)


def test_a_failed_measurement_changes_nothing_and_says_why(client, monkeypatch: pytest.MonkeyPatch) -> None:
    def fails(engine: str, model: str) -> int:
        raise RuntimeError("the worker died")

    killed: list[bool] = []
    monkeypatch.setattr(server, "_measure", fails)
    monkeypatch.setattr(server, "_kill_gpu_worker", lambda: killed.append(True))
    server._burn_in["running"] = True
    server._burn_in_job("whisper", "tiny")
    assert (server._burn_in["running"], server._burn_in["error"]) == (False, "the worker died")
    assert killed == [True]
    assert client.get("/engines").get_json()[0]["measured"] is False


def test_the_measurement_is_the_highest_rise_on_the_card_the_model_sits_on(monkeypatch: pytest.MonkeyPatch) -> None:
    used = {0: 100, 1: 8000}
    monkeypatch.setattr(server, "_used_per_card_mib", lambda: dict(used))
    monkeypatch.setattr(server, "_kill_gpu_worker", lambda: None)
    monkeypatch.setattr(server.time, "sleep", lambda _: None)
    monkeypatch.setattr(server, "_gpu_device_index", None)

    def transcribe(*_: object, measuring: bool) -> dict:
        assert measuring
        monkeypatch.setattr(server, "_gpu_device_index", 1)
        used.update({0: 500, 1: 11000})  # the other card grows too, and less
        threading.Event().wait(0.25)  # the sampler sees the peak
        assert server._burn_in["peak_mib"] == 3000
        used[1] = 9000
        threading.Event().wait(0.25)
        return {"text": "", "time": 1.0}

    monkeypatch.setattr(server, "_transcribe_gpu", transcribe)
    assert server._measure("whisper", "tiny") == 3000


def test_the_recording_is_longer_than_the_longest_piece_parakeet_works_on() -> None:
    with wave.open(str(server._BURN_IN_RECORDING)) as recording:
        assert recording.getframerate() == 16000
        assert recording.getnframes() / recording.getframerate() > server._PARAKEET_CHUNK_S


def test_the_headroom_is_a_setting_and_the_old_name_is_gone(client) -> None:
    assert client.get("/config").get_json()["headroom_mib"] == 256
    answer = client.post("/config", json={"vram_margin_mib": 512})
    assert (answer.status_code, answer.get_json()["error"]) == (400, "bad_request")
