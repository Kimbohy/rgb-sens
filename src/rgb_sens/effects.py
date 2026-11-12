"""Lighting effects.

An effect turns audio features into one colour per frame. Colours are *linear light* values
in 0..1; gamma correction and dithering happen later, in :mod:`rgb_sens.output`.

To add an effect, subclass :class:`Effect` and decorate it with :func:`register`.
"""

from __future__ import annotations

import colorsys
import math
from abc import ABC, abstractmethod
from dataclasses import dataclass

import numpy as np

from .audio import NUM_BANDS, Features

RGB = tuple[float, float, float]


def clamp01(x: float) -> float:
    return 0.0 if x < 0.0 else 1.0 if x > 1.0 else x


def smoothstep(x: float) -> float:
    x = clamp01(x)
    return x * x * (3.0 - 2.0 * x)


def lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


class Follower:
    """Moves towards a target at an independent speed going up (attack) and down (release).

    Times are time constants in seconds; the result does not depend on the frame rate.
    """

    def __init__(self, attack: float, release: float, scale: float = 1.0):
        self.attack, self.release, self.scale = attack, release, scale
        self.value = 0.0

    def update(self, target: float, dt: float) -> float:
        tau = (self.attack if target > self.value else self.release) * self.scale
        self.value += (target - self.value) * (1.0 - math.exp(-dt / tau))
        return self.value


class HoldRelease:
    """Rises quickly, stays put for a while (hold), then falls slowly.

    This is what gives the delay before the colour goes back to white.
    """

    def __init__(self, attack: float, hold: float, release: float, scale: float = 1.0):
        self.attack, self.hold, self.release, self.scale = attack, hold, release, scale
        self.value = 0.0
        self._hold_until = 0.0

    def update(self, target: float, t: float, dt: float) -> float:
        if target > self.value + 0.01:
            self.value += (target - self.value) * (1.0 - math.exp(-dt / (self.attack * self.scale)))
            self._hold_until = t + self.hold
        elif t >= self._hold_until:
            self.value += (target - self.value) * (
                1.0 - math.exp(-dt / (self.release * self.scale))
            )
        return self.value


@dataclass(frozen=True)
class EffectConfig:
    hold: float = 1.0  # seconds the colour is kept once the sound stops
    smooth: float = 1.0  # multiplies every response time (>1 softer, <1 snappier)


def idle_white(t: float, level: float = 0.0) -> float:
    """The resting white: soft, breathes very slowly, rises a little with the volume."""
    breath = 0.5 - 0.5 * math.cos(2 * math.pi * t / 7.0)
    return 0.20 + 0.07 * breath * (1.0 - level) + 0.30 * level


class Effect(ABC):
    name: str
    label: str
    reactive: bool = True  # False for effects that ignore the sound (never go idle)

    def __init__(self, config: EffectConfig | None = None):
        self.config = config or EffectConfig()

    @abstractmethod
    def render(self, t: float, dt: float, f: Features) -> RGB:
        """Return the colour for this frame. ``t`` is the time in seconds, ``dt`` the frame time."""


EFFECTS: dict[str, type[Effect]] = {}


def register(cls: type[Effect]) -> type[Effect]:
    EFFECTS[cls.name] = cls
    return cls


@register
class Breath(Effect):
    """Plain white that breathes. Used when the firmware effect cannot be white."""

    name = "breath"
    label = "breath: soft white breathing (software)"
    reactive = False

    def render(self, t: float, dt: float, f: Features) -> RGB:
        v = 0.05 + 0.95 * (0.5 - 0.5 * math.cos(2 * math.pi * t / 6.0))
        return (v, v, v)


@register
class Pulse(Effect):
    name = "pulse"
    label = "pulse: white, red on the bass, sparkle on the highs"

    def __init__(self, config: EffectConfig | None = None):
        super().__init__(config)
        k = self.config.smooth
        self._live = Follower(0.045, 0.20, k)  # pulses with every kick
        self._slow = Follower(0.30, 0.60, k)  # average bass level
        self._glow = HoldRelease(0.12, self.config.hold, 1.4, k)  # lingering pink tint
        self._hat = Follower(0.03, 0.14, k)
        self._level = Follower(0.25, 0.9, k)

    def render(self, t: float, dt: float, f: Features) -> RGB:
        bass = max(f.bands[0], f.bands[1])
        target = clamp01(0.55 * bass**1.2 + 0.85 * f.kick)
        live = self._live.update(target, dt)
        slow = self._slow.update(bass, dt)
        glow = self._glow.update(0.5 * slow, t, dt)
        amount = max(live, glow)
        hat = self._hat.update(f.hat, dt)
        level = self._level.update(f.level, dt)

        white = idle_white(t, level * 0.5)
        mix = smoothstep(amount)
        red = 0.28 + 0.72 * amount
        r = lerp(white, red, mix)
        g = lerp(white, 0.0, mix)
        b = lerp(white, 0.0, mix)
        sparkle = 0.30 * hat * (1.0 - 0.6 * mix)
        return (r + sparkle, g + sparkle * (1 - mix), b + sparkle * (1 - mix))


@register
class Hue(Effect):
    name = "hue"
    label = "hue: the colour follows the pitch (bass red ... treble violet)"

    def __init__(self, config: EffectConfig | None = None):
        super().__init__(config)
        k = self.config.smooth
        self._hue = Follower(0.14, 0.14, k)
        self._presence = HoldRelease(0.15, self.config.hold, 1.6, k)
        self._level = Follower(0.06, 0.40, k)
        self._kick = Follower(0.04, 0.25, k)

    def render(self, t: float, dt: float, f: Features) -> RGB:
        weights = f.bands**2
        total = float(weights.sum())
        if total > 0.08:  # centre of gravity of the spectrum
            centre = float((np.arange(NUM_BANDS) * weights).sum() / total) / (NUM_BANDS - 1)
            self._hue.update(0.75 * centre, dt)  # 0 = red ... 0.75 = violet
        level = self._level.update(f.level, dt)
        kick = self._kick.update(f.kick, dt)
        presence = smoothstep(self._presence.update(clamp01(f.level * 1.6), t, dt))
        music = clamp01(0.30 + 0.70 * level + 0.30 * kick)
        value = lerp(idle_white(t), music, presence)
        return colorsys.hsv_to_rgb(self._hue.value, presence, value)


@register
class Mixer(Effect):
    name = "mixer"
    label = "mixer: bass red, mids green, highs blue, flash on kicks"

    def __init__(self, config: EffectConfig | None = None):
        super().__init__(config)
        k = self.config.smooth
        self._scale = k
        self._env = [Follower(0.040, 0.32, k), Follower(0.045, 0.25, k), Follower(0.030, 0.16, k)]
        self._peak = Follower(0.05, 0.5, k)
        self._presence = HoldRelease(0.10, self.config.hold, 1.6, k)
        self._flash = Follower(0.025, 0.22, k)
        self._chroma = [1.0, 1.0, 1.0]

    def render(self, t: float, dt: float, f: Features) -> RGB:
        targets = (
            clamp01(0.9 * max(f.bands[0], f.bands[1]) ** 1.2 + 0.6 * f.kick),
            clamp01(0.9 * max(f.bands[2], f.bands[3]) ** 1.2 + 0.5 * f.snare),
            clamp01(0.9 * max(f.bands[4], f.bands[5]) ** 1.2 + 0.5 * f.hat),
        )
        env = [
            follower.update(target, dt) for follower, target in zip(self._env, targets, strict=True)
        ]
        peak_now = max(env)
        peak = self._peak.update(peak_now, dt)
        if peak_now > 0.08:  # keep the hue while there is sound
            k = 1.0 - math.exp(-dt / (0.06 * self._scale))
            for i in range(3):
                self._chroma[i] += (env[i] / peak_now - self._chroma[i]) * k
        flash = self._flash.update(f.kick, dt)

        presence = smoothstep(self._presence.update(max(targets), t, dt))
        brightness = 0.30 + 0.70 * peak
        white = idle_white(t)
        out = [lerp(white, c * brightness, presence) for c in self._chroma]
        extra = 0.30 * flash * presence
        return (out[0] + extra, out[1] + extra, out[2] + extra)
