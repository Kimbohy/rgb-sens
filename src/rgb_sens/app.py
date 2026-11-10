"""The render loop: picks the mode, renders frames, talks to the mouse."""

from __future__ import annotations

import logging
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from .audio import Features
from .device import MouseDevice
from .effects import EffectConfig
from .engine import HARDWARE, Engine
from .output import Encoder
from .triggers import Debouncer

log = logging.getLogger(__name__)


class FeatureSource(Protocol):
    def snapshot(self) -> Features: ...


@dataclass
class Settings:
    modes: tuple[str, ...] = (HARDWARE, "pulse", "hue", "mixer")
    start: int = 1  # 1-based
    hw_mode: str = "Loop"  # firmware effect used for the "hw" mode
    hw_speed: float = 0.0  # 0 = slowest, 1 = fastest
    hold: float = 1.0
    smooth: float = 1.0
    brightness: float = 1.0
    dither: bool = True
    fps: int = 60


class Runner:
    """Drives one mouse. :meth:`tick` renders a single frame; :meth:`run` loops in real time."""

    REASSERT_DELAY = 0.35  # the mouse also changes mode by itself when its button is pressed
    BUTTON_GRACE = 0.75  # ignore button reports caused by our own mode changes

    def __init__(
        self,
        mouse: MouseDevice,
        audio: FeatureSource | None,
        settings: Settings,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.mouse = mouse
        self.audio = audio
        self.settings = settings
        self._clock = clock
        self._modes = self._resolve_modes(list(settings.modes))
        self.engine = Engine(self._modes, EffectConfig(settings.hold, settings.smooth))
        self.encoder = Encoder(dither=settings.dither, master=settings.brightness)
        self._requests: deque[tuple[str, int]] = deque()
        self._debounce = Debouncer(0.3)
        self._ignore_button_until = 0.0
        self._reassert_at: float | None = None
        self._last_sent: tuple[int, int, int] | None = None
        self._running = True

    # -- setup ------------------------------------------------------------------------------
    def _resolve_modes(self, modes: list[str]) -> list[str]:
        if HARDWARE not in modes:
            return modes
        try:
            mode = self.mouse.find_mode(self.settings.hw_mode)
        except LookupError as exc:
            log.warning("Hardware effect: %s. Using a software white breathing instead.", exc)
            return ["breath" if m == HARDWARE else m for m in modes]
        if not self.mouse.can_be_white(mode):
            log.warning(
                "Hardware effect '%s' has fixed colours, it cannot be white. "
                "Using a software white breathing instead. Try: rgb-sens hw-demo",
                mode.name,
            )
            return ["breath" if m == HARDWARE else m for m in modes]
        return modes

    # -- requests (callable from any thread) -----------------------------------------------
    def request_next(self, source: str = "button") -> None:
        now = self._clock()
        if source == "button" and now < self._ignore_button_until:
            return
        if self._debounce.ready(now):
            self._requests.append(("next", 0))

    def request_select(self, index: int) -> None:
        self._requests.append(("select", index))

    def stop(self) -> None:
        self._running = False

    # -- modes -------------------------------------------------------------------------------
    def _apply_mode(self, name: str) -> None:
        if name == HARDWARE:
            self.mouse.apply_hardware(self.settings.hw_mode, self.settings.hw_speed)
        else:
            self.mouse.set_direct()
        self._ignore_button_until = self._clock() + self.BUTTON_GRACE

    def _activate(self, index: int, t: float) -> None:
        self.engine.select(index, t)
        self._apply_mode(self.engine.current)
        self._last_sent = None
        self._reassert_at = self._clock() + self.REASSERT_DELAY
        log.info(
            "Mode %d/%d: %s",
            self.engine.index + 1,
            len(self._modes),
            self._label(self.engine.current),
        )

    def _label(self, name: str) -> str:
        if name == HARDWARE:
            return f"hw: firmware effect '{self.settings.hw_mode}' in white"
        return self.engine.label(name)

    # -- frames ------------------------------------------------------------------------------
    def tick(self, t: float, dt: float) -> bool:
        """Handle requests and render one frame. Returns False in hardware mode (nothing sent)."""
        while self._requests:
            kind, value = self._requests.popleft()
            self._activate(self.engine.index + 1 if kind == "next" else value, t)
        if self._reassert_at is not None and self._clock() >= self._reassert_at:
            self._reassert_at = None
            self._apply_mode(self.engine.current)

        if self.engine.is_hardware:
            return False
        features = self.audio.snapshot() if self.audio else Features()
        rgb = self.encoder.encode(self.engine.step(t, dt, features))
        if rgb != self._last_sent:
            self.mouse.send_color(rgb)
            self._last_sent = rgb
        return True

    def run(self) -> None:
        start = self._clock()
        self._activate(self.settings.start - 1, 0.0)
        frame = 1.0 / self.settings.fps
        next_frame = previous = self._clock()
        while self._running:
            now = self._clock()
            rendered = self.tick(now - start, max(now - previous, 1e-3))
            previous = now
            if not rendered:
                time.sleep(0.05)
                next_frame = self._clock()
                continue
            next_frame += frame
            delay = next_frame - self._clock()
            if delay > 0:
                time.sleep(delay)
            else:
                next_frame = self._clock()
