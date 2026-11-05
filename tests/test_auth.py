"""Who may do what: loopback transcribes and asks without a token, the commands that change something need
it always, a caller on another machine needs it for everything (auth.py, server._check_access)."""
from collections.abc import Iterator
from pathlib import Path

import pytest

import auth
import server

TOKEN = "s3cret"
LOCAL = "127.0.0.1"
REMOTE = "10.0.0.2"


def test_loopback_asks_without_token() -> None:
    access = auth.access_for("0.0.0.0", TOKEN)
    assert access.refusal("", changes_something=False, client_host=LOCAL) is None


def test_change_needs_token_also_on_loopback() -> None:
    access = auth.access_for("127.0.0.1", TOKEN)
    assert access.refusal("", changes_something=True, client_host=LOCAL) is not None
    assert access.refusal(TOKEN, changes_something=True, client_host=LOCAL) is None


def test_remote_needs_token_for_everything() -> None:
    access = auth.access_for("0.0.0.0", TOKEN)
    assert access.refusal("", changes_something=False, client_host=REMOTE) is not None
    assert access.refusal("wrong", changes_something=False, client_host=REMOTE) is not None
    assert access.refusal(TOKEN, changes_something=False, client_host=REMOTE) is None


def test_unknown_client_counts_as_remote() -> None:
    access = auth.access_for("0.0.0.0", TOKEN)
    assert access.refusal("", changes_something=False, client_host=None) is not None


def test_change_without_configured_token_is_refused() -> None:
    access = auth.access_for("127.0.0.1", None)
    assert access.refusal("", changes_something=True, client_host=LOCAL) is not None


def test_listening_on_a_network_without_token_is_refused() -> None:
    with pytest.raises(ValueError):
        auth.access_for("0.0.0.0", None)


def test_create_token_makes_a_private_file_and_keeps_it(tmp_path: Path) -> None:
    path = tmp_path / "sub" / "token"
    first = auth.create_token(path)
    assert path.stat().st_mode & 0o777 == auth.TOKEN_FILE_MODE
    assert auth.create_token(path) == first
    assert auth.read_token(path) == first


def test_read_token_without_file(tmp_path: Path) -> None:
    assert auth.read_token(tmp_path / "missing") is None


@pytest.fixture
def networked(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """The service as it runs for other machines: on all addresses, with a token."""
    monkeypatch.setattr(server, "_access", auth.access_for("0.0.0.0", TOKEN))
    yield


def _get(path: str, client: str, token: str | None = None) -> int:
    headers = {auth.TOKEN_HEADER: token} if token else {}
    response = server.app.test_client().get(path, headers=headers, environ_base={"REMOTE_ADDR": client})
    return response.status_code


def _post_config(client: str, token: str | None = None) -> int:
    headers = {auth.TOKEN_HEADER: token} if token else {}
    response = server.app.test_client().post("/config", json={}, headers=headers, environ_base={"REMOTE_ADDR": client})
    return response.status_code


@pytest.mark.usefixtures("networked")
def test_service_lets_loopback_ask_and_refuses_remote_without_token() -> None:
    assert _get("/config", LOCAL) == 200
    assert _get("/config", REMOTE) == 401
    assert _get("/config", REMOTE, TOKEN) == 200


@pytest.mark.usefixtures("networked")
def test_service_page_and_health_are_open() -> None:
    assert _get("/health", REMOTE) == 200
    assert _get("/ui/", REMOTE) == 200


@pytest.mark.usefixtures("networked")
def test_service_config_change_needs_token_on_loopback() -> None:
    assert _post_config(LOCAL) == 401
    assert _post_config(LOCAL, TOKEN) == 200


@pytest.mark.usefixtures("networked")
def test_refusal_is_shaped_like_the_other_errors() -> None:
    response = server.app.test_client().get("/status", environ_base={"REMOTE_ADDR": REMOTE})
    assert response.status_code == 401
    assert response.get_json()["error"] == "unauthorized"
