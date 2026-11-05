"""Who may do what, like the TTS-Suite. A client on this machine (loopback) transcribes and asks (every
``GET``) without a token. A command that changes something (the configuration, a burn-in) always needs
the token, also locally, since every program on the machine reaches localhost. A client on another
machine needs it for everything; the service listens beyond loopback only with a token.

Standard library only: ``python3 auth.py token <file>`` makes the token file on the host, outside the image.
"""

from __future__ import annotations

import ipaddress
import os
import secrets
import sys
from dataclasses import dataclass
from pathlib import Path

# Not `Authorization`: behind a reverse proxy with its own login that header belongs to the proxy.
TOKEN_HEADER = "X-STT-Suite-Token"
TOKEN_BYTES = 32
TOKEN_FILE_MODE = 0o600


def is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


@dataclass(frozen=True)
class Access:
    token: str | None
    # The service listens beyond loopback: every request from another machine needs the token.
    token_for_everything: bool

    def refusal(self, sent: str, changes_something: bool, client_host: str | None) -> str | None:
        """Why the request is refused, ``None`` when it may go ahead. ``sent``: the token it carries;
        ``client_host``: the address it came from (``None`` = unknown, counts as remote)."""
        if not changes_something and (not self.token_for_everything or is_loopback(client_host or "")):
            return None
        if self.token is None:
            return "this needs a token and none is configured"
        if not secrets.compare_digest(sent, self.token):
            return f"a valid token is needed ({TOKEN_HEADER})"
        return None


def access_for(host: str, token: str | None) -> Access:
    """The access rules for a service listening on `host`; refuses an open one on a network."""
    everywhere = not is_loopback(host)
    if everywhere and token is None:
        raise ValueError(f"listening on {host} needs a token (STT_TOKEN_FILE); there is none")
    return Access(token=token, token_for_everything=everywhere)


def read_token(path: Path) -> str | None:
    """The token in the file, None while there is no file."""
    if not path.exists():
        return None
    return path.read_text(encoding="utf-8").strip() or None


def create_token(path: Path) -> str:
    """A new token in a file only the user can read; an existing file is left as it is."""
    existing = read_token(path)
    if existing is not None:
        return existing
    path.parent.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(TOKEN_BYTES)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, TOKEN_FILE_MODE)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        os.fchmod(descriptor, TOKEN_FILE_MODE)
        handle.write(token + "\n")
    return token


def main(arguments: list[str]) -> None:
    if len(arguments) != 2 or arguments[0] != "token":
        raise SystemExit("usage: python3 auth.py token <file>   (makes the token file if there is none)")
    path = Path(arguments[1]).expanduser()
    create_token(path)
    print(f"The token is in {path} (readable for you alone).")


if __name__ == "__main__":
    main(sys.argv[1:])
