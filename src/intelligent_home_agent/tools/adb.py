"""An Android device over ADB, for what the TV remote protocol can't do: see the screen.

The `adb` binary does the talking (install it where the agent runs: the package `adb` or
`android-tools-adb`). Its key, which the device accepted when it was paired, lives under
the data directory. Outside the home the device is reached through a local forwarder over
the home network proxy, as the remote is.

Pairing, once, from where the agent runs while the device shows its wireless debugging
pairing code: see scripts/android_tv_adb.py. Android closes the fixed port 5555 when the
device restarts.

The navigator uses it (tools/tv_navigator.py): a helper model that sees the screen and
presses the remote's keys to reach something inside an app.
"""

from __future__ import annotations

import asyncio
import io
import os
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from xml.etree.ElementTree import ParseError

from defusedxml import ElementTree as ET

from intelligent_home_agent.tools.android_tv import Tunnel
from intelligent_home_agent.tools.base import ToolError

ADB_PORT = 5555
ADB_OFF = (
    "ADB does not answer on the TV device: it closes when the device restarts. Reopen it "
    "(Settings > System > Developer options > Wireless debugging), then run "
    "scripts/android_tv_adb.py tcpip <port shown there> (see that script)."
)


@dataclass
class Element:
    label: str
    focused: bool
    bounds: str

    def line(self) -> str:
        return f"{'> ' if self.focused else '  '}{self.label} {self.bounds}"


class Adb:
    def __init__(
        self,
        host: str,
        key_dir: str,
        proxy: str | None = None,
        port: int = ADB_PORT,
        timeout: float = 15.0,
    ) -> None:
        self._host = host
        self._port = port
        self._proxy = proxy
        self._timeout = timeout
        home = Path(key_dir).resolve()
        (home / ".android").mkdir(parents=True, exist_ok=True)
        # Older adb builds (Debian's) keep their key under $HOME/.android whatever
        # ANDROID_USER_HOME says: both point to the data directory. Its own server port keeps
        # it from talking to an adb server started with another key.
        self._env = {
            **os.environ,
            "HOME": str(home),
            "ANDROID_USER_HOME": str(home / ".android"),
            "ANDROID_ADB_SERVER_PORT": "5038",
        }

    async def run(self, *args: str, seconds: float | None = None) -> bytes:
        try:
            proc = await asyncio.create_subprocess_exec(
                "adb",
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=self._env,
            )
        except FileNotFoundError as exc:
            raise ToolError("adb is not installed where the agent runs.") from exc
        try:
            out, err = await asyncio.wait_for(proc.communicate(), seconds or self._timeout)
        except TimeoutError as exc:
            proc.kill()
            raise ToolError(f"adb {args[0]} timed out.") from exc
        if proc.returncode:
            raise ToolError(f"adb {args[0]} failed: {err.decode(errors='replace').strip()[:200]}")
        return out

    @asynccontextmanager
    async def session(self, port: int | None = None) -> AsyncIterator[AdbSession]:
        async with AsyncExitStack() as stack:
            host, local = self._host, port or self._port
            if self._proxy:
                tunnel = Tunnel(self._proxy, self._host, local, self._timeout)
                host, local = "127.0.0.1", await stack.enter_async_context(tunnel)
            serial = f"{host}:{local}"
            said = (await self.run("connect", serial)).decode(errors="replace")
            if "connected" not in said or "cannot" in said or "failed" in said:
                raise ToolError(ADB_OFF)
            stack.push_async_callback(self._disconnect, serial)
            session = AdbSession(self, serial)
            await session.wait_ready()
            yield session

    async def _disconnect(self, serial: str) -> None:
        try:
            await self.run("disconnect", serial)
        except ToolError:
            pass


class AdbSession:
    def __init__(self, adb: Adb, serial: str) -> None:
        self._adb = adb
        self.serial = serial

    async def shell(self, command: str) -> str:
        out = await self._adb.run("-s", self.serial, "shell", command)
        return out.decode(errors="replace")

    async def wait_ready(self) -> None:
        for _ in range(10):
            try:
                await self.shell("true")
                return
            except ToolError as exc:
                if "unauthorized" in str(exc):
                    raise ToolError(
                        "The TV device has not authorized the agent's ADB key: accept the "
                        "prompt on screen, with 'Always allow'."
                    ) from exc
                await asyncio.sleep(0.5)
        raise ToolError(ADB_OFF)

    async def screenshot(self, width: int = 960) -> bytes | None:
        """The screen as a JPEG, or None when it is blank (a protected video plays)."""
        from PIL import Image  # the android-tv extra

        png = await self._adb.run("-s", self.serial, "exec-out", "screencap", "-p")
        if not png:
            return None
        image = Image.open(io.BytesIO(png)).convert("RGB")
        if image.getbbox() is None:
            return None
        image.thumbnail((width, width))
        out = io.BytesIO()
        image.save(out, "JPEG", quality=70)
        return out.getvalue()

    async def elements(self, limit: int = 80) -> list[Element]:
        """What the screen shows, by the names apps give their elements."""
        await self.shell("uiautomator dump /sdcard/home-agent-ui.xml >/dev/null 2>&1")
        xml = await self._adb.run("-s", self.serial, "exec-out", "cat", "/sdcard/home-agent-ui.xml")
        return parse_elements(xml, limit)

    async def type_text(self, text: str) -> None:
        """Into the field being edited (its keyboard open), one word at a time."""
        words = text.split()
        for i, word in enumerate(words):
            if i:
                await self.shell("input keyevent 62")
            await self.shell(f"input text {_shell_quote(word)}")
            await asyncio.sleep(0.2)

    async def foreground_app(self) -> str | None:
        out = await self.shell("dumpsys activity activities | grep -m1 topResumedActivity")
        for part in out.split():
            if "/" in part:
                return part.split("/")[0]
        return None


def parse_elements(xml: bytes, limit: int = 80) -> list[Element]:
    try:
        root = ET.fromstring(xml)
    except (ParseError, ValueError):
        return []
    found: list[Element] = []
    for node in root.iter("node"):
        label = (node.get("text") or "").strip() or (node.get("content-desc") or "").strip()
        focused = node.get("focused") == "true"
        if not label and not focused:
            continue
        found.append(Element(label[:120] or "(focused)", focused, node.get("bounds") or ""))
    return found[:limit]


def _shell_quote(word: str) -> str:
    escaped = word.replace("'", "'\\''")
    return f"'{escaped}'"
