"""The render loop: picks the mode, renders frames, talks to the mouse."""

from __future__ import annotations

import logging
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from .audio import DEFAULT_SILENCE_DB, Features
from .device import MouseDevice
from .effects import EffectConfig
from .engine import HARDWARE, Engine
from .output import Encoder
from .triggers import Debouncer

log = logging.getLogger(__name__)

WAKE_MARGIN_DB = 3.0  # the sound must rise this far above the threshold to wake the lights up
WAKE_CONFIRM = 0.12  # ... and stay there this long, so a click or a blip does not count


class FeatureSource(Protocol):
    def snapshot(self) -> Features: ...


@dataclass
class Settings:
    modes: tuple[str, ...] = (HARDWARE, "pulse", "hue", "mixer")
    start: int = 1  # 1-based
    hw_mode: str = "Ripple"  # firmware effect used for the "hw" mode and when it goes quiet
    hw_speed: float = 0.0  # 0 = slowest, 1 = fastest
    hold: float = 1.0
    smooth: float = 1.0
    brightness: float = 1.0
    dither: bool = True
    fps: int = 60
    silence_db: float = DEFAULT_SILENCE_DB  # quieter than this = nothing is playing
    idle_to_hw: bool = True  # when quiet, go back to the firmware effect
    idle_delay: float = 4.0  # seconds of quiet before that


class Runner:
    """Drives one mouse. :meth:`tick` renders a single frame; :meth:`run` loops in real time.

    The audio modes (pulse, hue, mixer) paint the mouse in Direct mode. When the sound stays
    under ``silence_db`` for ``idle_delay`` seconds, the mouse is handed back to its firmware
    effect (the "idle" state); when the sound returns, Direct mode takes over again.
    """

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
        self._hw = self._find_hw_mode()
        self._modes = self._resolve_modes(list(settings.modes))
        self.engine = Engine(self._modes, EffectConfig(settings.hold, settings.smooth))
        self.encoder = Encoder(dither=settings.dither, master=settings.brightness)
        self._requests: deque[tuple[str, int]] = deque()
        self._debounce = Debouncer(0.3)
        self._ignore_button_until = 0.0
        self._reassert_at: float | None = None
        self._last_sent: tuple[int, int, int] | None = None
        self._running = True
        self._idle_enabled = settings.idle_to_hw and self._hw is not None
        self._asleep = False
        self._quiet_since: float | None = None
        self._loud_since: float | None = None

    @property
    def asleep(self) -> bool:
        """True while the mouse is parked on its firmware effect because it is quiet."""
        return self._asleep

    # -- setup ------------------------------------------------------------------------------
    def _find_hw_mode(self) -> Any | None:
        try:
            mode = self.mouse.find_mode(self.settings.hw_mode)
        except LookupError as exc:
            log.warning("Firmware effect: %s.", exc)
            return None
        if not self.mouse.can_be_white(mode):
            log.warning(
                "The firmware effect '%s' has no colour setting in OpenRGB. White is requested "
                "anyway; if the mouse ignores it, pick another one with: rgb-sens hw-demo",
                mode.name,
            )
        return mode

    def _resolve_modes(self, modes: list[str]) -> list[str]:
        if HARDWARE in modes and self._hw is None:
            log.warning("Using a soft white breathing instead of the firmware effect.")
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
        if name == HARDWARE or self._asleep:
            self.mouse.apply_hardware(self.settings.hw_mode, self.settings.hw_speed)
        else:
            self.mouse.set_direct()
        self._ignore_button_until = self._clock() + self.BUTTON_GRACE

    def _activate(self, index: int, t: float) -> None:
        self._asleep = False
        self._quiet_since = self._loud_since = None
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

    # -- quiet / sound detection ---------------------------------------------------------------
    def _check_idle(self, t: float, f: Features) -> bool:
        """Update the quiet/sound state. Returns True while the mouse is parked on the firmware."""
        loud = f.loudness_db
        threshold = self.settings.silence_db
        if not self._asleep:
            if loud >= threshold:
                self._quiet_since = None
                return False
            if self._quiet_since is None:
                self._quiet_since = t
            if t - self._quiet_since < self.settings.idle_delay:
                return False
            self._sleep(loud)
            return True

        # Parked: wake up only for a real sound, clearly above the threshold (hysteresis).
        if loud < threshold + WAKE_MARGIN_DB:
            self._loud_since = None
            return True
        if self._loud_since is None:
            self._loud_since = t
        if t - self._loud_since < WAKE_CONFIRM:
            return True
        self._wake(t, loud)
        return False

    def _sleep(self, loud_db: float) -> None:
        self._asleep = True
        self._quiet_since = self._loud_since = None
        self._apply_mode(self.engine.current)
        log.info(
            "Quiet (%.0f dB): back to the firmware effect '%s'.", loud_db, self.settings.hw_mode
        )

    def _wake(self, t: float, loud_db: float) -> None:
        self._asleep = False
        self._quiet_since = self._loud_since = None
        self.engine.select(self.engine.index, t)  # fresh effect, faded in from the last colour
        self._apply_mode(self.engine.current)
        self._last_sent = None
        log.info("Sound is back (%.0f dB): %s", loud_db, self._label(self.engine.current))

    # -- frames ------------------------------------------------------------------------------
    def tick(self, t: float, dt: float) -> bool:
        """Handle requests and render one frame.

        Returns False when nothing was drawn (firmware effect running, in a hardware mode or
        because it is quiet).
        """
        while self._requests:
            kind, value = self._requests.popleft()
            self._activate(self.engine.index + 1 if kind == "next" else value, t)
        if self._reassert_at is not None and self._clock() >= self._reassert_at:
            self._reassert_at = None
            self._apply_mode(self.engine.current)

        if self.engine.is_hardware:
            return False
        features = self.audio.snapshot() if self.audio else Features()
        reactive = self.engine.effect is not None and self.engine.effect.reactive
        if self._idle_enabled and reactive and self._check_idle(t, features):
            return False
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
