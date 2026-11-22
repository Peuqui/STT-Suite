"""remote.py against a real little HTTP server: what another machine's suite does, answered and refused."""
import json
import socket
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import pytest

import remote
from auth import TOKEN_HEADER


class Peer:
    """What the other suite does: answers by `behaviour` and keeps what it was sent."""

    def __init__(self) -> None:
        self.status = 200
        self.body: bytes = json.dumps({"text": "hallo", "device": "cuda"}).encode()
        self.delay = 0.0
        self.requests: list[dict[str, Any]] = []


@pytest.fixture
def peer() -> Iterator[tuple[Peer, str]]:
    state = Peer()

    class Handler(BaseHTTPRequestHandler):
        def _answer(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            state.requests.append({"method": self.command, "path": self.path, "headers": {name.lower(): value for name, value in self.headers.items()},
                                   "body": self.rfile.read(length)})
            time.sleep(state.delay)
            self.send_response(state.status)
            self.send_header("Content-Length", str(len(state.body)))
            self.end_headers()
            self.wfile.write(state.body)

        do_GET = do_POST = _answer

        def log_message(self, *args: Any) -> None:
            pass

    httpd = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()
    yield state, f"http://127.0.0.1:{httpd.server_port}"
    httpd.shutdown()
    httpd.server_close()


def make_host(url: str, tmp_path: Path, token: str | None = "secret") -> remote.Host:
    token_file = tmp_path / "token"
    if token is not None:
        token_file.write_text(token + "\n")
    return remote.Host("Aragon", url, str(token_file))


@pytest.fixture
def recording(tmp_path: Path) -> str:
    path = tmp_path / "aufnahme.wav"
    path.write_bytes(b"RIFF-not-really-audio")
    return str(path)


def test_probe_sends_the_token_and_accepts_an_answer(peer: tuple[Peer, str], tmp_path: Path) -> None:
    state, url = peer
    remote.probe(make_host(url, tmp_path), 2)
    assert state.requests[0]["path"] == "/health"
    assert state.requests[0]["headers"][TOKEN_HEADER.lower()] == "secret"
    assert state.requests[0]["headers"]["user-agent"] == "STT-Suite"


def test_a_trailing_slash_in_the_url_does_not_matter(peer: tuple[Peer, str], tmp_path: Path) -> None:
    state, url = peer
    remote.probe(make_host(url + "/", tmp_path), 2)
    assert state.requests[0]["path"] == "/health"


def test_probe_of_a_port_nobody_listens_on_is_unavailable(tmp_path: Path) -> None:
    with socket.socket() as taken:
        taken.bind(("127.0.0.1", 0))
        port = taken.getsockname()[1]
    with pytest.raises(remote.HostUnreachable, match="Aragon unreachable"):
        remote.probe(make_host(f"http://127.0.0.1:{port}", tmp_path), 2)


def test_probe_gives_up_at_its_short_limit_on_a_machine_that_does_not_answer(tmp_path: Path) -> None:
    with socket.socket() as silent:  # listens, but never accepts or answers
        silent.bind(("127.0.0.1", 0))
        silent.listen(1)
        started = time.time()
        with pytest.raises(remote.HostUnreachable, match="Aragon unreachable"):
            remote.probe(make_host(f"http://127.0.0.1:{silent.getsockname()[1]}", tmp_path), 0.3)
        assert time.time() - started < 2


def test_probe_refused_with_a_status_is_unavailable(peer: tuple[Peer, str], tmp_path: Path) -> None:
    state, url = peer
    state.status, state.body = 401, json.dumps({"error": "unauthorized", "detail": "wrong token"}).encode()
    with pytest.raises(remote.RemoteUnavailable, match="answered 401: wrong token") as raised:
        remote.probe(make_host(url, tmp_path), 2)
    assert not isinstance(raised.value, remote.HostUnreachable)  # it is there, it refuses


def test_without_a_token_nothing_is_sent(peer: tuple[Peer, str], tmp_path: Path, recording: str) -> None:
    state, url = peer
    host = make_host(url, tmp_path, token=None)
    with pytest.raises(remote.RemoteUnavailable, match=f"no token in {host.token_file}"):
        remote.probe(host, 2)
    with pytest.raises(remote.RemoteUnavailable, match="no token in"):
        remote.transcribe(host, recording, {}, 2)
    assert state.requests == []


def test_transcribe_posts_the_recording_as_a_form_with_the_token(peer: tuple[Peer, str], tmp_path: Path, recording: str) -> None:
    state, url = peer
    answer = remote.transcribe(make_host(url, tmp_path), recording, {"device": "cuda", "engine": "whisper", "model": "large-v3"}, 5)
    assert answer == {"text": "hallo", "device": "cuda"}
    sent = state.requests[0]
    assert (sent["method"], sent["path"]) == ("POST", "/transcribe")
    assert sent["headers"][TOKEN_HEADER.lower()] == "secret"
    assert sent["headers"]["content-type"].startswith("multipart/form-data; boundary=")
    for field in (b'name="device"\r\n\r\ncuda', b'name="engine"\r\n\r\nwhisper', b'name="model"\r\n\r\nlarge-v3',
                  b'name="file"; filename="aufnahme.wav"', b"RIFF-not-really-audio"):
        assert field in sent["body"]


@pytest.mark.parametrize("status", [503, 500, 400])
def test_a_refusal_carries_status_and_detail_of_the_other_side(peer: tuple[Peer, str], tmp_path: Path, recording: str, status: int) -> None:
    state, url = peer
    state.status, state.body = status, json.dumps({"error": "x", "detail": "GPU unavailable: no room"}).encode()
    with pytest.raises(remote.RemoteUnavailable, match=f"Aragon answered {status}: GPU unavailable: no room"):
        remote.transcribe(make_host(url, tmp_path), recording, {}, 5)


def test_a_refusal_without_detail_still_names_the_status(peer: tuple[Peer, str], tmp_path: Path, recording: str) -> None:
    state, url = peer
    state.status, state.body = 500, b"<html>boom</html>"
    with pytest.raises(remote.RemoteUnavailable, match="Aragon answered 500$"):
        remote.transcribe(make_host(url, tmp_path), recording, {}, 5)


def test_a_transcription_that_takes_too_long_is_unavailable(peer: tuple[Peer, str], tmp_path: Path, recording: str) -> None:
    state, url = peer
    state.delay = 1.0
    with pytest.raises(remote.RemoteUnavailable, match="Aragon unreachable") as raised:
        remote.transcribe(make_host(url, tmp_path), recording, {}, 0.3)
    # It answered the probe: a slow transcription is not a machine that is off, its other entries stay.
    assert not isinstance(raised.value, remote.HostUnreachable)


def test_an_answer_that_is_no_json_object_is_unavailable(peer: tuple[Peer, str], tmp_path: Path, recording: str) -> None:
    state, url = peer
    state.body = b"not json"
    with pytest.raises(remote.RemoteUnavailable, match="no valid JSON"):
        remote.transcribe(make_host(url, tmp_path), recording, {}, 5)
    state.body = b"[1]"
    with pytest.raises(remote.RemoteUnavailable, match="no JSON object"):
        remote.transcribe(make_host(url, tmp_path), recording, {}, 5)


def test_remote_unavailable_is_a_candidate_unavailable() -> None:
    assert issubclass(remote.RemoteUnavailable, remote.CandidateUnavailable)


def test_health_returns_the_answer_of_the_other_suite(peer: tuple[Peer, str], tmp_path: Path) -> None:
    state, url = peer
    state.body = json.dumps({"status": "ok", "cpu_loaded": True}).encode()
    assert remote.health(make_host(url, tmp_path), 2) == {"status": "ok", "cpu_loaded": True}


def test_the_hosts_endpoint_asks_real_machines(peer: tuple[Peer, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import copy

    import server

    state, url = peer
    state.body = json.dumps({"status": "ok", "cpu_loaded": True, "cpu_engine": "parakeet"}).encode()
    with socket.socket() as taken:
        taken.bind(("127.0.0.1", 0))
        closed_port = taken.getsockname()[1]
    token_file = make_host(url, tmp_path).token_file
    monkeypatch.setattr(server, "_config", copy.deepcopy(server._config))
    server._config["hosts"] = [
        {"name": "On", "url": url, "token_file": token_file},
        {"name": "Off", "url": f"http://127.0.0.1:{closed_port}", "token_file": token_file},
    ]
    answer = server.app.test_client().get("/hosts").json["hosts"]
    assert [(item["name"], item["reachable"]) for item in answer] == [("On", True), ("Off", False)]
    assert answer[0]["health"]["cpu_engine"] == "parakeet"
    assert "On" not in (answer[1]["error"] or "") and "Off unreachable" in answer[1]["error"]
    state.status, state.body = 503, json.dumps({"detail": "loading"}).encode()
    refused = server.app.test_client().get("/hosts").json["hosts"][0]
    assert refused["reachable"] is False and refused["error"] == "On answered 503: loading"


# ---- the JSON request and the endpoints that pass it on ------------------------------------


def test_request_json_gets_any_shape_with_the_token(peer: tuple[Peer, str], tmp_path: Path) -> None:
    state, url = peer
    state.body = json.dumps([{"engine": "whisper", "model": "tiny"}]).encode()
    assert remote.request_json(make_host(url, tmp_path), "GET", "/engines", 2) == (200, [{"engine": "whisper", "model": "tiny"}])
    sent = state.requests[0]
    assert (sent["method"], sent["path"]) == ("GET", "/engines")
    assert sent["headers"][TOKEN_HEADER.lower()] == "secret" and sent["headers"]["user-agent"] == "STT-Suite"


def test_request_json_posts_a_json_body(peer: tuple[Peer, str], tmp_path: Path) -> None:
    state, url = peer
    remote.request_json(make_host(url, tmp_path), "POST", "/burn-in", 2, {"engine": "parakeet", "model": "fp32"})
    sent = state.requests[0]
    assert sent["method"] == "POST" and sent["headers"]["content-type"] == "application/json"
    assert json.loads(sent["body"]) == {"engine": "parakeet", "model": "fp32"}


def test_a_refusal_keeps_status_and_body_for_the_caller(peer: tuple[Peer, str], tmp_path: Path) -> None:
    state, url = peer
    refusal = {"error": "burn_in_running", "detail": "a burn-in is running, one at a time"}
    state.status, state.body = 409, json.dumps(refusal).encode()
    with pytest.raises(remote.RemoteUnavailable) as raised:
        remote.request_json(make_host(url, tmp_path), "POST", "/burn-in", 2, {})
    assert raised.value.status == 409 and raised.value.body == refusal
    assert not isinstance(raised.value, remote.HostUnreachable)


def test_request_json_to_nobody_is_host_unreachable(tmp_path: Path) -> None:
    with socket.socket() as taken:
        taken.bind(("127.0.0.1", 0))
        port = taken.getsockname()[1]
    with pytest.raises(remote.HostUnreachable) as raised:
        remote.request_json(make_host(f"http://127.0.0.1:{port}", tmp_path), "GET", "/engines", 2)
    assert raised.value.status is None


@pytest.fixture
def through(peer: tuple[Peer, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[Peer, Any]]:
    import copy

    import server

    state, url = peer
    with socket.socket() as taken:
        taken.bind(("127.0.0.1", 0))
        closed_port = taken.getsockname()[1]
    token_file = make_host(url, tmp_path).token_file
    monkeypatch.setattr(server, "_config", copy.deepcopy(server._config))
    server._config["hosts"] = [
        {"name": "Aragon", "url": url, "token_file": token_file},
        {"name": "Off", "url": f"http://127.0.0.1:{closed_port}", "token_file": token_file},
    ]
    yield state, server.app.test_client()


def test_engines_and_burn_in_state_are_passed_through(through: tuple[Peer, Any]) -> None:
    state, client = through
    state.body = json.dumps([{"engine": "parakeet", "model": "fp32", "needed_mib": 3976}]).encode()
    assert client.get("/hosts/Aragon/engines").json == [{"engine": "parakeet", "model": "fp32", "needed_mib": 3976}]
    state.body = json.dumps({"running": False}).encode()
    assert client.get("/hosts/Aragon/burn-in").json == {"running": False}
    assert [(r["method"], r["path"]) for r in state.requests] == [("GET", "/engines"), ("GET", "/burn-in")]


def test_the_burn_in_start_is_passed_on_with_its_body_and_the_token_of_the_other_side(through: tuple[Peer, Any]) -> None:
    state, client = through
    state.status, state.body = 202, json.dumps({"running": True}).encode()
    answer = client.post("/hosts/Aragon/burn-in", json={"engine": "parakeet", "model": "fp32"}, headers={TOKEN_HEADER: "test-token"})
    assert answer.status_code == 202 and answer.json == {"running": True}
    sent = state.requests[0]
    assert (sent["method"], sent["path"]) == ("POST", "/burn-in")
    assert json.loads(sent["body"]) == {"engine": "parakeet", "model": "fp32"}
    assert sent["headers"][TOKEN_HEADER.lower()] == "secret"


def test_the_burn_in_start_needs_the_token_of_this_service_also_on_loopback(through: tuple[Peer, Any]) -> None:
    state, client = through
    assert client.post("/hosts/Aragon/burn-in", json={"engine": "parakeet", "model": "fp32"}).status_code == 401
    assert state.requests == []


def test_an_unknown_machine_is_404(through: tuple[Peer, Any]) -> None:
    _, client = through
    for answer in (client.get("/hosts/Mars/engines"), client.get("/hosts/Mars/burn-in")):
        assert answer.status_code == 404 and answer.json["error"] == "unknown_host"
    posted = client.post("/hosts/Mars/burn-in", json={}, headers={TOKEN_HEADER: "test-token"})
    assert posted.status_code == 404 and posted.json["error"] == "unknown_host"


def test_a_refusal_of_the_other_side_comes_back_with_its_status_and_body(through: tuple[Peer, Any]) -> None:
    state, client = through
    refusal = {"error": "burn_in_running", "detail": "a burn-in is running, one at a time"}
    state.status, state.body = 409, json.dumps(refusal).encode()
    answer = client.post("/hosts/Aragon/burn-in", json={"engine": "parakeet", "model": "fp32"}, headers={TOKEN_HEADER: "test-token"})
    assert answer.status_code == 409 and answer.json == refusal


def test_a_machine_that_is_off_is_502_with_the_reason(through: tuple[Peer, Any]) -> None:
    _, client = through
    answer = client.get("/hosts/Off/engines")
    assert answer.status_code == 502 and answer.json["error"] == "host_unreachable"
    assert "Off unreachable" in answer.json["detail"]
