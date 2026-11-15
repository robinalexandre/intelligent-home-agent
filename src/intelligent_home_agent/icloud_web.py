"""The user's iCloud through the web services icloud.com uses: Reminders, Notes and Find My.

Apple has no public API for them, so this goes through pyicloud, which speaks icloud.com's
private one. It logs in with the Apple ID's own password and 2FA (app-specific passwords
are refused), once, by hand: `python -m intelligent_home_agent.icloud_web` (docs/apple.md). The
agent keeps only the trusted session that login leaves on the data volume, never the
password, so when Apple ends that session the login must be done again.

pyicloud is blocking and its session is not safe across threads: one call at a time, in a
worker thread.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import importlib.util
import logging
import os
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any, TypeVar

from intelligent_home_agent.tools.base import ToolError

log = logging.getLogger(__name__)

T = TypeVar("T")

# Run on the host, outside the agent's container, the login leaves its session where the
# agent never looks: the command says where it runs, and how the user knows it worked.
LOGIN = (
    "in the agent's container, on the machine that runs it: `docker compose exec agent "
    "python -m intelligent_home_agent.icloud_web`, from the folder of docker-compose.yml "
    "(docs/apple.md). It asks the Apple ID password, then the 6-digit code shown on "
    "their devices, and is done only "
    'once it prints "Logged in: N Reminders lists"'
)
RENEW = (
    f"The iCloud login (Reminders, Notes, Find My) has expired: the user must log in again {LOGIN}."
)
FIND_MY_RENEW = (
    "Find My asks for a fresh iCloud login, which the agent can't give it alone (it keeps "
    "no password): locating and ringing devices fail until the user logs in again "
    f"{LOGIN}. Reminders and Notes still work."
)


class LoginNeeded(Exception):
    pass


def available() -> bool:
    """Whether pyicloud is installed (the "apple" extra; the Docker image has it)."""
    return importlib.util.find_spec("pyicloud") is not None


def session_dir(data_dir: str) -> str:
    """Where the login leaves its session: a folder of DATA_DIR, the agent's alone."""
    return str(Path(data_dir) / "icloud-web")


def open_session(apple_id: str, session_dir: str, password: str | None = None) -> Any:
    """A pyicloud service on the saved session; without a password it never logs in anew."""
    import keyring
    from keyring.backends.null import Keyring as NoKeyring
    from pyicloud import PyiCloudService

    # pyicloud looks for a saved password in the system keyring: there is none to find.
    keyring.set_keyring(NoKeyring())
    Path(session_dir).mkdir(parents=True, exist_ok=True)
    os.chmod(session_dir, 0o700)
    return PyiCloudService(
        apple_id,
        password,
        cookie_directory=session_dir,
        refresh_interval=None,
        with_family=True,
        authenticate=password is not None or _has_session(session_dir),
    )


def _has_session(session_dir: str) -> bool:
    return any(Path(session_dir).glob("*.session"))


class ICloudWeb:
    def __init__(
        self,
        apple_id: str,
        session_dir: str,
        opener: Callable[[str, str], Any] = open_session,
    ) -> None:
        self._apple_id = apple_id
        self._session_dir = session_dir
        self._opener = opener
        self._api: Any = None
        self._lock = threading.Lock()

    def _service(self) -> Any:
        if self._api is None:
            api = self._opener(self._apple_id, self._session_dir)
            if not getattr(api, "data", None) or api.requires_2fa or not api.is_trusted_session:
                raise LoginNeeded
            self._api = api
        return self._api

    def _call(self, fn: Callable[[Any], T]) -> T:
        from pyicloud.exceptions import (
            PyiCloud2FARequiredException,
            PyiCloud2SARequiredException,
            PyiCloudAuthRequiredException,
            PyiCloudException,
            PyiCloudFailedLoginException,
        )

        expired = (
            LoginNeeded,
            PyiCloudFailedLoginException,
            PyiCloud2FARequiredException,
            PyiCloud2SARequiredException,
        )
        with self._lock:
            try:
                return fn(self._service())
            except PyiCloudAuthRequiredException as exc:
                # Find My's own 450: the session still serves the other apps.
                log.warning("Find My wants a fresh login: %s", exc)
                raise ToolError(FIND_MY_RENEW) from exc
            except expired as exc:
                log.warning("iCloud login refused: %s", type(exc).__name__)
                self._api = None
                raise ToolError(RENEW) from exc
            except ToolError:
                raise
            except PyiCloudException as exc:
                log.warning("iCloud call failed: %s", exc)
                raise ToolError(f"iCloud unavailable: {type(exc).__name__}") from exc

    async def run(self, fn: Callable[[Any], T]) -> T:
        """Run fn(pyicloud service) in a worker thread, one call at a time."""
        return await asyncio.to_thread(self._call, fn)


def login(apple_id: str, session_dir: str) -> None:
    """Log in by hand, with the password and the 2FA code; only the session is kept."""
    password = getpass.getpass(f"Apple ID password for {apple_id} (not an app-specific one): ")
    api = open_session(apple_id, session_dir, password)
    if api.requires_2sa:
        raise SystemExit("This Apple ID uses two-step verification (2SA): turn on 2FA first.")
    if api.requires_2fa:
        api.request_2fa_code()
        code = input("2FA code shown on your devices: ").strip()
        if not api.validate_2fa_code(code):
            raise SystemExit("Code refused.")
    if not api.is_trusted_session and not api.trust_session():
        raise SystemExit("Apple did not trust this session: try again.")
    lists = list(api.reminders.lists())
    print(f"Logged in: {len(lists)} Reminders lists. The session is in {session_dir}.")


def main() -> None:
    from intelligent_home_agent.config import get_settings

    settings = get_settings()
    parser = argparse.ArgumentParser(description=login.__doc__)
    parser.add_argument("apple_id", nargs="?", default=settings.icloud_apple_id)
    args = parser.parse_args()
    if not args.apple_id:
        raise SystemExit("Set ICLOUD_APPLE_ID or pass the Apple ID.")
    if not available():
        raise SystemExit('pyicloud is not installed: pip install ".[icloud]"')
    login(args.apple_id, session_dir(settings.data_dir))


if __name__ == "__main__":
    main()
