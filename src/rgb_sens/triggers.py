"""Ways to change mode: the mouse's LED button, a signal, or the keyboard.

The LED button of the Acer OMW950 is handled by the mouse firmware, but it also reports
itself to the PC: each press sends HID reports starting with ``06 f6`` on the
mouse's LED interface (``/dev/hidraw*``). Reading them needs no special group on most
systems, because OpenRGB's udev rules already give your user access to that device.
"""

from __future__ import annotations

import collections
import contextlib
import glob
import logging
import os
import select
import signal
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Iterable
from pathlib import Path

log = logging.getLogger(__name__)

MOUSE_VID = 0x30FA
MOUSE_PID = 0x1040
_HID_ID = "0003:000030FA:00001040"
DEFAULT_BUTTON_TRIGGER = "hid:06f6"


def parse_trigger(spec: str) -> tuple[str, bytes | str]:
    """``hid:06f6`` -> ("hid", b"\\x06\\xf6");  ``evdev:BTN_EXTRA`` -> ("evdev", "BTN_EXTRA")."""
    kind, sep, value = spec.partition(":")
    value = value.strip()
    if not sep or not value:
        raise ValueError(f"bad trigger '{spec}': use hid:<hex> or evdev:<BUTTON_NAME>")
    if kind == "hid":
        return ("hid", bytes.fromhex(value.replace(" ", "")))
    if kind == "evdev":
        return ("evdev", value)
    raise ValueError(f"bad trigger '{spec}': unknown kind '{kind}' (use hid or evdev)")


def report_matches(report: bytes, prefixes: Iterable[bytes]) -> bool:
    return any(report.startswith(p) for p in prefixes)


class Debouncer:
    """One button press produces two reports; this turns them into one event."""

    def __init__(self, interval: float = 0.3):
        self.interval = interval
        self._last = float("-inf")

    def ready(self, now: float) -> bool:
        if now - self._last >= self.interval:
            self._last = now
            return True
        return False


def pidfile_path() -> Path:
    return Path(os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir()) / "rgb-sens.pid"


def write_pidfile() -> None:
    try:
        pidfile_path().write_text(str(os.getpid()))
    except OSError:
        log.debug("could not write the pid file", exc_info=True)


def remove_pidfile() -> None:
    with contextlib.suppress(OSError):
        pidfile_path().unlink()


def is_rgb_sens_process(cmdline: bytes) -> bool:
    """True if a /proc/<pid>/cmdline is the rgb-sens program itself.

    Only the program name counts: the `rgb-sens` script (``python .../bin/rgb-sens run``) or
    ``python -m rgb_sens``. A path that merely contains the name, such as a text editor open on
    ``~/code/project/rgb-sens/README.md``, must not match.
    """
    args = [a.decode(errors="replace") for a in cmdline.split(b"\0") if a]
    for i, arg in enumerate(args):
        if os.path.basename(arg) == "rgb-sens":
            return True
        if arg == "-m" and i + 1 < len(args) and args[i + 1] == "rgb_sens":
            return True
    return False


def send_next() -> int:
    """Switch a running rgb-sens to its next mode (what `rgb-sens next` does).

    The process is looked up through a pid file and checked before signalling it, so a
    stale file can never make us signal an unrelated program.
    """
    try:
        pid = int(pidfile_path().read_text())
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes()
    except (OSError, ValueError):
        print("rgb-sens is not running.", file=sys.stderr)
        return 1
    if not is_rgb_sens_process(cmdline):
        print("rgb-sens is not running (stale pid file).", file=sys.stderr)
        return 1
    os.kill(pid, signal.SIGUSR1)
    return 0


def hid_nodes() -> list[str]:
    """The /dev/hidraw* nodes that belong to the mouse."""
    nodes = []
    for path in sorted(glob.glob("/sys/class/hidraw/hidraw*")):
        try:
            with open(os.path.join(path, "device", "uevent")) as fh:
                if _HID_ID in fh.read().upper():
                    nodes.append("/dev/" + os.path.basename(path))
        except OSError:
            continue
    return nodes


def evdev_devices():
    """(evdev module or None, list of the mouse's input devices)."""
    try:
        import evdev
    except ImportError:
        return None, []
    found = []
    for path in evdev.list_devices():
        try:
            dev = evdev.InputDevice(path)
        except OSError:
            continue
        if dev.info.vendor == MOUSE_VID and dev.info.product == MOUSE_PID:
            found.append(dev)
    return evdev, found


class ButtonListener(threading.Thread):
    """Calls ``callback`` whenever a matching report or button press arrives."""

    def __init__(self, specs: Iterable[str], callback: Callable[[], None]):
        super().__init__(daemon=True, name="button-listener")
        self._callback = callback
        self._hid_prefixes: list[bytes] = []
        self._evdev_names: set[str] = set()
        for spec in specs:
            kind, value = parse_trigger(spec)
            if kind == "hid":
                self._hid_prefixes.append(value)  # type: ignore[arg-type]
            else:
                self._evdev_names.add(value)  # type: ignore[arg-type]
        self._sources: dict[int, tuple[str, object]] = {}
        self._evdev = None

    def open(self) -> int:
        """Open the devices. Returns how many sources are listened to."""
        if self._hid_prefixes:
            nodes = hid_nodes()
            if not nodes:
                log.warning("Button: no HID node found for the mouse.")
            for node in nodes:
                try:
                    fd = os.open(node, os.O_RDONLY | os.O_NONBLOCK)
                except OSError as exc:
                    log.warning("Button: cannot open %s (%s)", node, exc)
                    continue
                self._sources[fd] = ("hid", node)
        if self._evdev_names:
            self._evdev, devices = evdev_devices()
            if self._evdev is None:
                log.warning("Button: python-evdev is missing (pip install 'rgb-sens[evdev]').")
            elif not devices:
                log.warning(
                    "Button: no readable input device (is your user in the 'input' group?)."
                )
            for dev in devices:
                self._sources[dev.fd] = ("evdev", dev)
        return len(self._sources)

    def run(self) -> None:
        while True:
            ready, _, _ = select.select(list(self._sources), [], [], 1.0)
            for fd in ready:
                kind, source = self._sources[fd]
                try:
                    if kind == "hid":
                        if report_matches(os.read(fd, 64), self._hid_prefixes):
                            self._callback()
                    else:
                        self._handle_evdev(source)
                except (BlockingIOError, OSError):
                    continue

    def _handle_evdev(self, device) -> None:
        codes = self._evdev.ecodes
        for event in device.read():
            if event.type == codes.EV_KEY and event.value == 1:
                names = codes.bytype[codes.EV_KEY].get(event.code, "")
                names = names if isinstance(names, list) else [names]
                if self._evdev_names & set(names):
                    self._callback()


class StdinListener(threading.Thread):
    """Enter = next mode, a number = that mode, q = quit. Only useful in a terminal."""

    def __init__(
        self,
        on_next: Callable[[], None],
        on_select: Callable[[int], None],
        on_quit: Callable[[], None],
    ):
        super().__init__(daemon=True, name="stdin-listener")
        self._on_next, self._on_select, self._on_quit = on_next, on_select, on_quit

    def run(self) -> None:
        for line in sys.stdin:
            text = line.strip().lower()
            if text == "q":
                self._on_quit()
                return
            if text.isdigit():
                self._on_select(int(text) - 1)
            else:
                self._on_next()


def probe(seconds: float = 15.0) -> int:
    """Print everything the mouse sends while its LED button is pressed."""
    evdev, devices = evdev_devices()
    sources: dict[int, tuple[str, object]] = {}
    print("Sources found for the mouse:")
    if evdev is None:
        print("  (python-evdev not installed: only HID reports will be shown)")
    for dev in devices:
        print(f"  evdev  {dev.path}  '{dev.name}'")
        sources[dev.fd] = ("evdev", dev)
    for node in hid_nodes():
        try:
            fd = os.open(node, os.O_RDONLY | os.O_NONBLOCK)
        except OSError as exc:
            print(f"  hidraw {node}  cannot open: {exc}")
            continue
        sources[fd] = ("hid", node)
        print(f"  hidraw {node}")
    if not sources:
        print("\nNothing readable. Is the mouse plugged in? Are the OpenRGB udev rules installed?")
        return 1

    print(
        f"\nDo NOT move the mouse. Press the LED button 3 times in the next {seconds:g} s "
        "(Ctrl+C to stop early).\n"
    )
    seen: collections.Counter[str] = collections.Counter()
    end = time.monotonic() + seconds
    try:
        while time.monotonic() < end:
            ready, _, _ = select.select(list(sources), [], [], 0.2)
            for fd in ready:
                kind, source = sources[fd]
                try:
                    if kind == "hid":
                        data = os.read(fd, 64)
                        print(f"  [{source}] {data.hex(' ')}")
                        seen[f"hid:{data.hex()}"] += 1
                    else:
                        _probe_evdev(evdev, source, seen)
                except (BlockingIOError, OSError):
                    continue
    except KeyboardInterrupt:
        print()
    print()
    if not seen:
        print("No event: the button seems to be handled by the firmware only.")
        print("Alternatives: use another mouse button (run this again and press it), or bind a")
        print("keyboard shortcut to the command:  rgb-sens next")
        return 0
    print("Possible triggers (the one seen about 3 times is the one):")
    for spec, count in seen.most_common():
        print(f"  --trigger {spec}   (seen {count}x)")
    if any(s.startswith("hid:06f6") for s in seen):
        print("\nThat is the LED button of the OMW950: it is already the default trigger.")
    return 0


def _probe_evdev(evdev, device, seen: collections.Counter) -> None:
    codes = evdev.ecodes
    for event in device.read():
        if event.type == codes.EV_SYN:
            continue
        name = codes.bytype.get(event.type, {}).get(event.code, event.code)
        name = name[0] if isinstance(name, list) else name
        print(f"  [{device.path}] {codes.EV[event.type]} {name} value={event.value}")
        if event.type == codes.EV_KEY and event.value == 1:
            seen[f"evdev:{name}"] += 1
