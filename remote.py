"""Another machine's STT-Suite as one entry of the auto list: ask whether it is there, hand it a recording.

The suite there is this very service (same code), so the request is the one any client sends: ``POST
/transcribe`` with the token in the header of auth.py. Whatever keeps the machine from transcribing now
(off, no answer, busy, an error) is one :class:`RemoteUnavailable`; the auto list then goes on with its
next entry, as it does between GPU and CPU.

Standard library only (like auth.py): ``requests`` is in the image only as a dependency of others.
"""

from __future__ import annotations

import http.client
import json
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

from auth import TOKEN_HEADER, read_token

# Generic on purpose: the other suite logs the User-Agent of every request.
USER_AGENT = "STT-Suite"


class CandidateUnavailable(Exception):
    """This entry of the plan cannot transcribe now; the plan goes on with the next one. Lives here, not in
    server.py, so that this module needs nothing from the service."""


class RemoteUnavailable(CandidateUnavailable):
    """The other machine cannot do this now; the message says why. When it answered with a refusal, `status`
    and `body` (its JSON, the `error` and `detail` of server._error) are what it sent, so a caller can pass
    them on."""

    def __init__(self, message: str, status: int | None = None, body: dict | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.body = body


class HostUnreachable(RemoteUnavailable):
    """The machine does not answer at all (off, or its suite is down): none of its entries can take the
    recording now, so the plan need not ask it again."""


@dataclass(frozen=True)
class Host:
    name: str
    url: str
    token_file: str

    def endpoint(self, path: str) -> str:
        """The one place a URL is put together, so a trailing slash in the configured URL does not matter."""
        return f"{self.url.rstrip('/')}{path}"


def _token(host: Host) -> str:
    token = read_token(Path(host.token_file).expanduser())
    if token is None:
        raise RemoteUnavailable(f"{host.name}: no token in {host.token_file}")
    return token


def _refusal(host: Host, error: urllib.error.HTTPError) -> RemoteUnavailable:
    """Status and, when the other suite sent one, its `detail` (its answers are shaped like server._error)."""
    try:
        body = json.loads(error.read().decode("utf-8", errors="replace"))
    except ValueError:
        body = None
    if not isinstance(body, dict):
        body = None
    detail = body.get("detail") if body else None
    reason = f"{host.name} answered {error.code}"
    return RemoteUnavailable(f"{reason}: {detail}" if detail else reason, status=error.code, body=body)


def _exchange(host: Host, request: urllib.request.Request, timeout_s: float,
              unreachable: type[RemoteUnavailable]) -> tuple[int, Any]:
    """The one place a request goes out and what can go wrong with it becomes a :class:`RemoteUnavailable`:
    a refusal (with status and body), no connection (`unreachable`, so a caller can tell a machine that is off
    from one that is slow), an answer that is no JSON."""
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as error:
        raise _refusal(host, error) from error
    except (OSError, http.client.HTTPException) as error:
        raise unreachable(f"{host.name} unreachable: {error}") from error
    except ValueError as error:
        raise RemoteUnavailable(f"{host.name} sent no valid JSON: {error}") from error


def _object(host: Host, answer: Any) -> dict:
    if not isinstance(answer, dict):
        raise RemoteUnavailable(f"{host.name} sent no JSON object")
    return answer


def _headers(host: Host, content_type: str | None = None) -> dict[str, str]:
    headers = {TOKEN_HEADER: _token(host), "User-Agent": USER_AGENT}
    if content_type:
        headers["Content-Type"] = content_type
    return headers


def request_json(host: Host, method: str, path: str, timeout_s: float, body: dict | None = None) -> tuple[int, Any]:
    """A JSON request to the other suite (GET, or POST with a JSON body): the status of its answer and its JSON,
    whatever shape that has. :class:`RemoteUnavailable` for a refusal (`status`, `body`), :class:`HostUnreachable` when
    nothing answers."""
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(host.endpoint(path), data=data, method=method,
                                     headers=_headers(host, "application/json" if data else None))
    return _exchange(host, request, timeout_s, HostUnreachable)


def health(host: Host, timeout_s: float) -> dict:
    """The ``GET /health`` answer of the machine's suite; :class:`RemoteUnavailable` unless it comes within
    `timeout_s`. A machine that is off answers nothing at all; the short limit decides that fast instead of
    waiting out the connection attempt."""
    return _object(host, request_json(host, "GET", "/health", timeout_s)[1])


def probe(host: Host, timeout_s: float) -> None:
    """Raises :class:`RemoteUnavailable` unless the machine answers ``GET /health`` within `timeout_s`."""
    health(host, timeout_s)


def _multipart(boundary: str, fields: dict[str, str], file_name: str, content: bytes) -> bytes:
    parts = [
        f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode()
        for name, value in fields.items()
    ]
    parts.append(
        f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{quote(file_name)}"\r\n'
        f"Content-Type: application/octet-stream\r\n\r\n".encode() + content + b"\r\n"
    )
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts)


def transcribe(host: Host, file_path: str, fields: dict[str, str], timeout_s: float) -> dict:
    """The answer of the other suite's ``POST /transcribe`` for this recording. Anything but a 200 with
    JSON is a :class:`RemoteUnavailable`. `timeout_s` is the wait for each step; the other suite answers
    only when it is done, so it is the wait for the transcription."""
    path = Path(file_path)
    boundary = uuid.uuid4().hex
    request = urllib.request.Request(
        host.endpoint("/transcribe"),
        data=_multipart(boundary, fields, path.name, path.read_bytes()),
        headers=_headers(host, f"multipart/form-data; boundary={boundary}"),
    )
    # Plain RemoteUnavailable also when nothing answers: it was asked a moment ago (the probe), so a slow or
    # dropped transcription is not a machine that is off, and its other entries stay.
    return _object(host, _exchange(host, request, timeout_s, RemoteUnavailable)[1])
