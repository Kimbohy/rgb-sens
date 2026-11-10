"""OpenRGB access: connection, the mouse itself, and its built-in (firmware) effects."""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import time
from typing import Any

from openrgb import OpenRGBClient
from openrgb.utils import DeviceType, ModeFlags, RGBColor

log = logging.getLogger(__name__)

WHITE = RGBColor(255, 255, 255)
_NON_HARDWARE_MODES = {"direct", "off"}


class ModeNotFoundError(LookupError):
    """The mouse does not have a mode with this name."""


class OpenRGBSession:
    """Connects to the OpenRGB SDK server, optionally starting it first.

    >>> with OpenRGBSession(server_path="~/Applications/OpenRGB/OpenRGB.AppImage") as s:
    ...     mouse = s.mouse()
    """

    def __init__(self, host: str = "127.0.0.1", port: int = 6742, server_path: str | None = None):
        self.host, self.port = host, port
        self.server_path = os.path.expanduser(server_path) if server_path else None
        self.client: OpenRGBClient | None = None
        self._server: subprocess.Popen | None = None

    def __enter__(self) -> OpenRGBSession:
        self.client = self._connect()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _new_client(self) -> OpenRGBClient:
        return OpenRGBClient(address=self.host, port=self.port, name="rgb-sens")

    def _connect(self) -> OpenRGBClient:
        try:
            return self._new_client()
        except Exception as first_error:
            if not self.server_path:
                raise ConnectionError(
                    f"Cannot reach the OpenRGB server on {self.host}:{self.port} ({first_error}).\n"
                    "Start it with:  OpenRGB --server --noautoconnect\n"
                    "or let rgb-sens start it with:  --openrgb /path/to/OpenRGB.AppImage"
                ) from first_error
        log.info("Starting the OpenRGB server...")
        self._server = subprocess.Popen(
            [self.server_path, "--server", "--noautoconnect"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if self._server.poll() is not None:
                raise ConnectionError(
                    f"OpenRGB exited right after starting (code {self._server.returncode}). "
                    "For an AppImage on Debian: sudo apt install libfuse2t64"
                )
            try:
                return self._new_client()
            except Exception:
                time.sleep(0.5)
        raise ConnectionError("The OpenRGB server did not become ready within 20 seconds.")

    def mouse(self) -> MouseDevice:
        assert self.client is not None, "use the session as a context manager"
        mice = self.client.get_devices_by_type(DeviceType.MOUSE)
        if not mice:
            mice = [d for d in self.client.devices if "mouse" in d.name.lower()]
        if not mice:
            raise LookupError(
                "No mouse found in OpenRGB. Check the udev rules and that the mouse is plugged in."
            )
        return MouseDevice(mice[0])

    def close(self) -> None:
        if self.client is not None:
            try:
                self.client.disconnect()
            except Exception:
                log.debug("error while disconnecting", exc_info=True)
            self.client = None
        if self._server is not None:
            try:
                os.killpg(self._server.pid, signal.SIGTERM)
                self._server.wait(timeout=5)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                pass
            self._server = None


class MouseDevice:
    """A mouse seen through OpenRGB.

    Two ways to light it:

    * Direct mode: we send one colour at a time (what the software effects use).
    * Hardware modes: the mouse's own firmware animates; we only pick colour and speed.
    """

    def __init__(self, device: Any):
        self._dev = device
        self._direct = self._find("direct")

    @property
    def name(self) -> str:
        return self._dev.name

    def _find(self, name: str) -> Any | None:
        for mode in self._dev.modes:
            if mode.name.lower() == name.lower():
                return mode
        return None

    def hardware_modes(self) -> list[Any]:
        return [m for m in self._dev.modes if m.name.lower() not in _NON_HARDWARE_MODES]

    def find_mode(self, name: str) -> Any:
        mode = self._find(name)
        if mode is None:
            available = ", ".join(m.name for m in self.hardware_modes())
            raise ModeNotFoundError(f"mode '{name}' not found. Available: {available}")
        return mode

    @staticmethod
    def can_be_white(mode: Any) -> bool:
        """True when the firmware lets us choose the colour of this effect."""
        return ModeFlags.HAS_MODE_SPECIFIC_COLOR in mode.flags and (mode.colors_max or 0) >= 1

    def apply_hardware(self, name: str, speed: float = 0.0) -> bool:
        """Start a firmware effect, as white if it allows a colour. Returns True if it is white.

        ``speed`` goes from 0 (slowest) to 1 (fastest).
        """
        mode = self.find_mode(name)
        if ModeFlags.HAS_SPEED in mode.flags and mode.speed_min is not None:
            mode.speed = round(mode.speed_min + (mode.speed_max - mode.speed_min) * speed)
        if ModeFlags.HAS_BRIGHTNESS in mode.flags and mode.brightness_max is not None:
            mode.brightness = mode.brightness_max
        white = self.can_be_white(mode)
        if white:
            count = min(max(mode.colors_min or 1, 1), mode.colors_max)
            mode.colors = [WHITE] * count
        self._dev.set_mode(mode)
        return white

    def set_direct(self) -> None:
        if self._direct is None:
            raise ModeNotFoundError("this device has no Direct mode")
        self._dev.set_mode(self._direct)

    def send_color(self, rgb: tuple[int, int, int]) -> None:
        self._dev.set_color(RGBColor(*rgb), fast=True)

    def restore(self) -> None:
        """Leave the mouse in a plain, steady white."""
        self.set_direct()
        self._dev.set_color(WHITE)

    def describe(self) -> str:
        lines = [
            f"Device: {self.name}  "
            f"({len(self._dev.leds)} LED, zones: {[z.name for z in self._dev.zones]})"
        ]
        for m in self._dev.modes:
            flags = [f.name for f in ModeFlags if f in m.flags]
            lines.append(
                f"  - {m.name:<16} colours={m.color_mode.name:<14} "
                f"speed={m.speed_min}..{m.speed_max} "
                f"brightness={m.brightness_min}..{m.brightness_max} "
                f"colour slots={m.colors_min}..{m.colors_max}"
            )
            lines.append(f"      flags: {', '.join(flags) or '-'}")
        return "\n".join(lines)
