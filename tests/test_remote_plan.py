"""The auto list with entries on other machines: handing over, passing on, the diarization, /config and the start."""
import copy
import io
from collections.abc import Iterator
from typing import Any

import pytest

import remote
import server

TOKEN = {"X-STT-Suite-Token": "test-token"}
HOSTS = [{"name": "Aragon", "url": "http://aragon:5080", "token_file": "/x/token"}]


def entry(host: str | None, device: str, engine: str = "parakeet", model: str = "fp32") -> dict[str, Any]:
    return {"host": host, "device": device, "engine": engine, "model": model, "enabled": True}


class Calls:
    """What the stand-ins for the other machine and for the local engines were asked."""

    def __init__(self) -> None:
        self.probed: list[str] = []
        self.sent: list[dict[str, str]] = []
        self.local: list[str] = []
        self.diarize_submitted: list[tuple[str, int]] = []
        self.collected: list[int] = []


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> Calls:
    seen = Calls()
    monkeypatch.setattr(server, "_config", copy.deepcopy(server._config))
    monkeypatch.setattr(server, "MACHINE", "Mini")
    server._config["hosts"] = copy.deepcopy(HOSTS)
    monkeypatch.setattr(server, "_decode_to_wav16k", lambda path: path + ".16k.wav")
    monkeypatch.setattr(server, "_burn_in", {"running": False})

    def local(device: str) -> Any:
        def run(*args: Any, **kwargs: Any) -> dict[str, Any]:
            seen.local.append(device)
            return {"text": f"local {device}", "time": 0.1, "segments": [(0.0, 1.0, "hallo")]}
        return run

    monkeypatch.setattr(server, "_transcribe_cpu", lambda path, language, segments, model: local("cpu")(segments))
    monkeypatch.setattr(server, "_transcribe_parakeet_cpu", lambda path, language, quality, segments: local("cpu")(segments))
    monkeypatch.setattr(server, "_transcribe_gpu", lambda *args, **kwargs: local("cuda")())

    def submit(path: str, num_speakers: int = 0) -> bool:
        seen.diarize_submitted.append((path, num_speakers))
        return True

    def collect(timeout: int) -> dict[str, Any]:
        seen.collected.append(timeout)
        return {"turns": [(0.0, 1.0, "SPEAKER_00")], "speakers": ["SPEAKER_00"], "time": 0.5}

    monkeypatch.setattr(server, "_diarize_submit", submit)
    monkeypatch.setattr(server, "_diarize_collect", collect)
    return seen


@pytest.fixture
def host_answers(calls: Calls, monkeypatch: pytest.MonkeyPatch) -> Any:
    """Install the other machine's behaviour: a dict to answer with, or an exception to raise."""
    def install(probe: Exception | None = None, answer: dict[str, Any] | Exception | None = None) -> None:
        def probe_host(host: remote.Host, timeout_s: float) -> None:
            calls.probed.append(f"{host.name}:{timeout_s}")
            if probe:
                raise probe

        def transcribe_on_host(host: remote.Host, file_path: str, fields: dict[str, str], timeout_s: float) -> dict[str, Any]:
            calls.sent.append({"file": file_path, **fields})
            if isinstance(answer, Exception):
                raise answer
            return dict(answer or {"text": "from aragon", "device": fields["device"], "host": None})

        monkeypatch.setattr(server, "probe_host", probe_host)
        monkeypatch.setattr(server, "transcribe_on_host", transcribe_on_host)
    return install


@pytest.fixture
def client() -> Iterator[Any]:
    server.app.config["TESTING"] = True
    with server.app.test_client() as test_client:
        yield test_client


def post(client: Any, chain: list[dict[str, Any]], **form: str) -> Any:
    server._config["auto_chain"] = chain
    return client.post("/transcribe", data={"file": (io.BytesIO(b"audio"), "a.wav"), "device": "auto", **form})


# ---- handing over -------------------------------------------------------------------------


def test_an_entry_with_a_host_transcribes_there_and_no_local_candidate_runs(client: Any, calls: Calls, host_answers: Any) -> None:
    host_answers()
    answer = post(client, [entry("Aragon", "cuda"), entry(None, "cpu")])
    assert answer.status_code == 200
    assert answer.json["host"] == "Aragon"
    assert answer.json["text"] == "from aragon"
    assert calls.local == []
    assert calls.probed == [f"Aragon:{server._config['host_probe_timeout_ms'] / 1000}"]
    assert "fallback" not in answer.json


def test_the_fields_and_the_original_recording_go_to_the_host(client: Any, calls: Calls, host_answers: Any) -> None:
    host_answers()
    post(client, [entry("Aragon", "cuda", "parakeet", "int8")], language="en")
    post(client, [entry("Aragon", "cpu", "whisper", "large-v3")], language="en")
    first, second = calls.sent
    for sent in (first, second):  # the upload, not the 16 kHz copy the decoder stand-in makes
        assert not sent.pop("file").endswith(".16k.wav")
    assert first == {"device": "cuda", "engine": "parakeet", "language": "en", "quality": "int8"}
    assert second == {"device": "cpu", "engine": "whisper", "language": "en", "model": "large-v3"}


def test_a_local_answer_names_no_host(client: Any, calls: Calls) -> None:
    answer = post(client, [entry(None, "cpu")])
    assert answer.status_code == 200
    assert answer.json["host"] == "Mini"  # always a name, this machine's own
    assert answer.json["device"] == "cpu"


@pytest.mark.parametrize("failure", [
    remote.RemoteUnavailable("Aragon answered 503: GPU unavailable: no room"),
    remote.RemoteUnavailable("Aragon answered 500"),
    remote.RemoteUnavailable("Aragon unreachable: timed out"),
])
def test_a_host_that_cannot_hands_the_recording_to_the_next_entry(client: Any, calls: Calls, host_answers: Any, failure: Exception) -> None:
    host_answers(answer=failure)
    answer = post(client, [entry("Aragon", "cuda"), entry(None, "cpu")])
    assert answer.status_code == 200
    assert answer.json["host"] == "Mini"
    assert calls.local == ["cpu"]
    assert answer.json["fallback"] == [{"tried": "Aragon cuda parakeet fp32", "reason": str(failure)}]


def test_a_host_that_is_off_is_not_sent_the_recording(client: Any, calls: Calls, host_answers: Any) -> None:
    host_answers(probe=remote.HostUnreachable("Aragon unreachable: timed out"))
    answer = post(client, [entry("Aragon", "cuda"), entry(None, "cpu")])
    assert answer.json["fallback"][0]["reason"] == "Aragon unreachable: timed out"
    assert calls.sent == []


def test_a_host_that_is_off_is_asked_once_and_its_other_entries_pass(client: Any, calls: Calls, host_answers: Any) -> None:
    # Found live (22.11.2026): with Aragon's suite down, its CPU entry waited out the probe a second time.
    host_answers(probe=remote.HostUnreachable("Aragon unreachable: timed out"))
    answer = post(client, [entry("Aragon", "cuda"), entry(None, "cuda"), entry("Aragon", "cpu"), entry(None, "cpu")])
    assert len(calls.probed) == 1
    assert calls.local == ["cuda"]
    assert answer.json["fallback"] == [
        {"tried": "Aragon cuda parakeet fp32", "reason": "Aragon unreachable: timed out"},
        {"tried": "Aragon cpu parakeet fp32", "reason": "Aragon unreachable: timed out"},
    ]


def test_a_host_whose_card_is_full_still_gets_its_cpu_entry(client: Any, calls: Calls, host_answers: Any) -> None:
    host_answers(answer=remote.RemoteUnavailable("Aragon answered 503: GPU unavailable: no room"))
    post(client, [entry("Aragon", "cuda"), entry("Aragon", "cpu"), entry(None, "cpu")])
    assert [sent["device"] for sent in calls.sent] == ["cuda", "cpu"]


def test_when_everything_fails_it_is_503_with_all_reasons(client: Any, calls: Calls, host_answers: Any) -> None:
    host_answers(answer=remote.RemoteUnavailable("Aragon answered 503"))
    answer = post(client, [entry("Aragon", "cuda"), entry("Aragon", "cpu")])
    assert answer.status_code == 503
    assert answer.json["error"] == "unavailable"
    assert answer.json["detail"].count("Aragon answered 503") == 2
    assert "Aragon cuda parakeet fp32" in answer.json["detail"] and "Aragon cpu parakeet fp32" in answer.json["detail"]


def test_a_withdrawn_local_gpu_keeps_the_cuda_entries_of_a_host(monkeypatch: pytest.MonkeyPatch) -> None:
    plan = [
        {**server._chain_candidate(entry(None, "cuda"))},
        {**server._chain_candidate(entry("Aragon", "cuda"))},
        {**server._chain_candidate(entry(None, "cuda", "whisper", "medium"))},
    ]
    tried: list[str] = []

    def run(candidate: dict[str, Any], _job: Any) -> dict[str, Any]:
        tried.append(server._label(candidate))
        if not candidate["host"]:
            raise server.GpuWithdrawn("the GPU was withdrawn: GPU-Orc: loading X")
        return {"text": "ok"}

    monkeypatch.setattr(server, "_run_candidate", run)
    job = server.Job("a.wav", "a.wav", "de", diarize=False, num_speakers=0)
    result, skipped, done_by = server._run_plan(plan, job)
    assert done_by["host"] == "Aragon"
    assert tried == ["cuda parakeet fp32", "Aragon cuda parakeet fp32"]
    assert result == {"text": "ok"}
    assert [item["tried"] for item in skipped] == ["cuda parakeet fp32", "cuda whisper medium"]


def test_the_label_names_the_host_in_front() -> None:
    assert server._label(server._chain_candidate(entry("Aragon", "cuda", "whisper", "large-v3"))) == "Aragon cuda whisper large-v3"
    assert server._label(server._chain_candidate(entry(None, "cuda", "whisper", "large-v3"))) == "cuda whisper large-v3"


def test_a_requested_device_always_means_this_machine() -> None:
    assert server._candidate("cuda", None, None, None)["host"] is None


# ---- diarization --------------------------------------------------------------------------


def test_a_host_that_transcribes_gets_the_wish_and_no_local_diarization_starts(client: Any, calls: Calls, host_answers: Any) -> None:
    host_answers(answer={"text": "from aragon", "text_speakers": "[A] hi", "speakers": ["A"], "diarize_time": 1.0})
    answer = post(client, [entry("Aragon", "cuda"), entry(None, "cpu")], diarize="1", num_speakers="3")
    assert calls.sent[0]["diarize"] == "1" and calls.sent[0]["num_speakers"] == "3"
    assert calls.diarize_submitted == []
    assert answer.json["text_speakers"] == "[A] hi"
    assert answer.json["host"] == "Aragon"


def test_without_the_wish_no_diarize_field_goes_to_the_host(client: Any, calls: Calls, host_answers: Any) -> None:
    host_answers()
    post(client, [entry("Aragon", "cuda")])
    assert "diarize" not in calls.sent[0] and "num_speakers" not in calls.sent[0]


def test_after_a_host_failed_the_local_diarization_starts_once_before_the_local_entry(
    client: Any, calls: Calls, host_answers: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    host_answers(answer=remote.RemoteUnavailable("Aragon answered 503"))
    monkeypatch.setattr(server, "_transcribe_gpu", lambda *a, **k: (_ for _ in ()).throw(server.GpuUnavailable("no room")))
    answer = post(client, [entry("Aragon", "cuda"), entry(None, "cuda"), entry(None, "cpu")], diarize="1")
    assert answer.status_code == 200
    assert len(calls.diarize_submitted) == 1
    assert calls.diarize_submitted[0][0].endswith(".16k.wav")
    assert answer.json["text_speakers"]
    assert answer.json["diarize_time"] == 0.5


def test_a_local_diarization_whose_plan_ends_at_a_host_is_drained_and_dropped(client: Any, calls: Calls, host_answers: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    host_answers(answer={"text": "from aragon"})
    monkeypatch.setattr(server, "_transcribe_gpu", lambda *a, **k: (_ for _ in ()).throw(server.GpuUnavailable("no room")))
    answer = post(client, [entry(None, "cuda"), entry("Aragon", "cpu")], diarize="1")
    assert len(calls.diarize_submitted) == 1
    assert calls.collected == [10]
    assert "text_speakers" not in answer.json and "diarize_error" not in answer.json
    assert answer.json["host"] == "Aragon"


def test_when_everything_fails_a_started_diarization_is_drained(client: Any, calls: Calls, host_answers: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    host_answers(answer=remote.RemoteUnavailable("down"))
    monkeypatch.setattr(server, "_transcribe_gpu", lambda *a, **k: (_ for _ in ()).throw(server.GpuUnavailable("no room")))
    answer = post(client, [entry(None, "cuda"), entry("Aragon", "cpu")], diarize="1")
    assert answer.status_code == 503
    assert calls.collected == [10]


# ---- /config ------------------------------------------------------------------------------


@pytest.fixture
def config_state(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(server, "_config", copy.deepcopy(server._config))
    server._config["hosts"] = copy.deepcopy(HOSTS)
    server._config["auto_chain"] = [entry("Aragon", "cuda"), entry(None, "cpu")]


def config(client: Any, **data: Any) -> Any:
    return client.post("/config", json=data, headers=TOKEN)


def test_hosts_are_set_through_config(client: Any, config_state: None) -> None:
    hosts = [*HOSTS, {"name": "Mini", "url": "https://mini", "token_file": "t"}]
    answer = config(client, hosts=hosts)
    assert answer.status_code == 200 and server._config["hosts"] == hosts


@pytest.mark.parametrize(("hosts", "reason"), [
    ("Aragon", "hosts must be a list"),
    ([{"name": "A", "url": "http://a"}], "needs exactly name, url and token_file"),
    ([{"name": "A", "url": "http://a", "token_file": "t", "port": 1}], "needs exactly name, url and token_file"),
    ([{"name": "", "url": "http://a", "token_file": "t"}], "must not be empty"),
    ([{"name": "A", "url": "ftp://a", "token_file": "t"}], "must start with http:// or https://"),
    ([{"name": "A", "url": "http://a", "token_file": ""}], "must not be empty"),
    ([{"name": "A", "url": 5, "token_file": "t"}], "are texts"),
    ([{"name": "A", "url": "http://a", "token_file": "t"}, {"name": "A", "url": "http://b", "token_file": "t"}], "used twice"),
    (["A"], "needs exactly name, url and token_file"),
])
def test_invalid_hosts_are_refused_with_their_reason(client: Any, config_state: None, hosts: object, reason: str) -> None:
    answer = config(client, hosts=hosts)
    assert answer.status_code == 400
    assert reason in answer.json["detail"]
    assert server._config["hosts"] == HOSTS


def test_a_list_with_an_unknown_host_is_refused(client: Any, config_state: None) -> None:
    answer = config(client, auto_chain=[entry("Mars", "cuda"), entry(None, "cpu")])
    assert answer.status_code == 400
    assert "host is null (this machine) or the name of one of hosts" in answer.json["detail"]


def test_hosts_and_list_sent_together_count_with_the_new_hosts(client: Any, config_state: None) -> None:
    hosts = [{"name": "Mars", "url": "http://mars", "token_file": "t"}]
    answer = config(client, hosts=hosts, auto_chain=[entry("Mars", "cuda"), entry(None, "cpu")])
    assert answer.status_code == 200
    assert server._config["hosts"] == hosts


def test_a_host_the_list_still_uses_cannot_go(client: Any, config_state: None) -> None:
    answer = config(client, hosts=[])
    assert answer.status_code == 400
    assert "host is null (this machine) or the name of one of hosts" in answer.json["detail"]
    assert server._config["hosts"] == HOSTS


def test_an_entry_without_host_is_refused(client: Any, config_state: None) -> None:
    without = {"device": "cpu", "engine": "whisper", "model": "tiny", "enabled": True}
    answer = config(client, auto_chain=[without])
    assert answer.status_code == 400
    assert "needs host, device, engine, model and enabled" in answer.json["detail"]


def test_the_timeouts_have_limits(client: Any, config_state: None) -> None:
    config(client, host_probe_timeout_ms=1, host_timeout_s=0)
    assert server._config["host_probe_timeout_ms"] == 50
    assert server._config["host_timeout_s"] == 1


def test_the_default_list_has_a_host_key_set_to_null() -> None:
    assert all(item["host"] is None for item in server._default_auto_chain())


# ---- the start ----------------------------------------------------------------------------


def test_a_saved_list_without_host_stops_the_start(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(server, "_config", {**server._config, "auto_chain": [
        {"device": "cpu", "engine": "whisper", "model": "tiny", "enabled": True}]})
    with pytest.raises(SystemExit, match="needs host"):
        server._require_valid_lists()


def test_a_valid_saved_list_lets_the_start_go_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(server, "_config", {**server._config, "hosts": HOSTS, "auto_chain": [entry("Aragon", "cuda")]})
    server._require_valid_lists()


# ---- GET /hosts ---------------------------------------------------------------------------


def test_hosts_state_shows_reachable_unreachable_and_refusing_machines(
    client: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Any,
) -> None:
    monkeypatch.setattr(server, "_config", copy.deepcopy(server._config))
    server._config["hosts"] = [
        {"name": "On", "url": "http://on", "token_file": "t"},
        {"name": "Off", "url": "http://off", "token_file": "t"},
        {"name": "Busy", "url": "http://busy", "token_file": "t"},
    ]

    def health(host: remote.Host, timeout_s: float) -> dict[str, Any]:
        assert timeout_s == server._config["host_probe_timeout_ms"] / 1000
        if host.name == "Off":
            raise remote.RemoteUnavailable("Off unreachable: timed out")
        if host.name == "Busy":
            raise remote.RemoteUnavailable("Busy answered 503")
        return {"status": "ok", "gpu_loaded": False}

    monkeypatch.setattr(server, "host_health", health)
    answer = client.get("/hosts")
    assert answer.status_code == 200
    by_name = {item["name"]: item for item in answer.json["hosts"]}
    assert by_name["On"] == {"name": "On", "url": "http://on", "reachable": True, "error": None, "health": {"status": "ok", "gpu_loaded": False}}
    assert by_name["Off"]["reachable"] is False and by_name["Off"]["error"] == "Off unreachable: timed out" and by_name["Off"]["health"] is None
    assert by_name["Busy"]["error"] == "Busy answered 503"


def test_without_hosts_the_state_is_empty(client: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(server, "_config", {**server._config, "hosts": []})
    assert client.get("/hosts").json == {"hosts": []}


# ---- the machine's own name ---------------------------------------------------------------


def test_health_and_status_name_this_machine(client: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(server, "MACHINE", "Mini")
    assert client.get("/health").json["machine"] == "Mini"
    assert client.get("/status").json["machine"] == "Mini"


def test_the_machine_is_the_hostname() -> None:
    import socket

    assert socket.gethostname() == server.MACHINE


def test_a_local_diarized_answer_names_this_machine_and_keeps_its_speakers(client: Any, calls: Calls) -> None:
    answer = post(client, [entry(None, "cpu")], diarize="1")
    assert answer.json["host"] == "Mini" and answer.json["text_speakers"]
    assert calls.collected == [server._GPU_RESULT_TIMEOUT_S]
