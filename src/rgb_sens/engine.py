"""Mode sequencing and cross-fades between modes."""

from __future__ import annotations

from collections.abc import Sequence

from .audio import Features
from .effects import EFFECTS, RGB, Effect, EffectConfig, clamp01, lerp, smoothstep

HARDWARE = "hw"  # the mouse's own firmware effect: nothing to render in software


class Engine:
    """Holds the list of modes, the active effect, and fades between effects."""

    CROSSFADE = 0.7  # seconds

    def __init__(self, modes: Sequence[str], config: EffectConfig | None = None):
        unknown = [m for m in modes if m != HARDWARE and m not in EFFECTS]
        if unknown:
            valid = ", ".join([HARDWARE, *EFFECTS])
            raise ValueError(f"unknown mode(s): {', '.join(unknown)} (valid: {valid})")
        if not modes:
            raise ValueError("at least one mode is required")
        self.modes = list(modes)
        self.config = config or EffectConfig()
        self.index = 0
        self.effect: Effect | None = None
        self._last: RGB = (0.2, 0.2, 0.2)
        self._fade_from: RGB | None = None
        self._fade_start = 0.0

    @property
    def current(self) -> str:
        return self.modes[self.index]

    @property
    def is_hardware(self) -> bool:
        return self.current == HARDWARE

    def label(self, name: str) -> str:
        return EFFECTS[name](self.config).label if name in EFFECTS else name

    def select(self, index: int, t: float) -> None:
        self.index = index % len(self.modes)
        name = self.current
        self.effect = None if name == HARDWARE else EFFECTS[name](self.config)
        self._fade_from = self._last
        self._fade_start = t

    def step(self, t: float, dt: float, features: Features) -> RGB:
        if self.effect is None:
            raise RuntimeError("the active mode is rendered by the mouse firmware")
        rgb = self.effect.render(t, dt, features)
        if self._fade_from is not None:
            k = smoothstep((t - self._fade_start) / self.CROSSFADE)
            rgb = (
                lerp(self._fade_from[0], rgb[0], k),
                lerp(self._fade_from[1], rgb[1], k),
                lerp(self._fade_from[2], rgb[2], k),
            )
            if k >= 1.0:
                self._fade_from = None
        self._last = (clamp01(rgb[0]), clamp01(rgb[1]), clamp01(rgb[2]))
        return self._last
