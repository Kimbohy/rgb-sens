"""Test doubles: a fake OpenRGB device, a fake clock, scripted audio features."""

from __future__ import annotations

import numpy as np
from openrgb.utils import ModeColors, ModeData, ModeFlags, RGBColor

from rgb_sens.audio import NUM_BANDS, SAMPLE_RATE, Features


def make_mode(mode_id: int, name: str, flags: ModeFlags, color_mode: ModeColors, **kw) -> ModeData:
    fields = dict(
        speed_min=None, speed_max=None, brightness_min=None, brightness_max=None,
        colors_min=None, colors_max=None, speed=None, brightness=None, direction=None, colors=None,
    )  # fmt: skip
    fields.update(kw)
    return ModeData(
        id=mode_id, name=name, value=mode_id, flags=flags, color_mode=color_mode, **fields
    )


class FakeRawDevice:
    """Looks like an ``openrgb`` Device: Direct, Loop, Ripple, Off.

    Like the real mouse, Ripple has no colour setting. ``loop_has_colour`` says whether Loop has.
    """

    name = "Instant USB Gaming Mouse"
    leds = [object()]
    zones: list = []

    def __init__(self, loop_has_colour: bool = True):
        self.log: list[tuple] = []
        direct = make_mode(0, "Direct", ModeFlags.HAS_PER_LED_COLOR, ModeColors.PER_LED)
        if loop_has_colour:
            loop = make_mode(
                1, "Loop",
                ModeFlags.HAS_SPEED | ModeFlags.HAS_BRIGHTNESS | ModeFlags.HAS_MODE_SPECIFIC_COLOR,
                ModeColors.MODE_SPECIFIC,
                speed_min=0, speed_max=5, brightness_min=0, brightness_max=7,
                colors_min=1, colors_max=1, speed=2, brightness=3, colors=[RGBColor(1, 2, 3)],
            )  # fmt: skip
        else:
            loop = make_mode(
                1, "Loop", ModeFlags.HAS_SPEED, ModeColors.RANDOM,
                speed_min=0, speed_max=5, speed=2,
            )  # fmt: skip
        ripple = make_mode(
            2, "Ripple", ModeFlags.HAS_SPEED | ModeFlags.HAS_BRIGHTNESS, ModeColors.RANDOM,
            speed_min=0, speed_max=5, brightness_min=0, brightness_max=7, speed=2, brightness=3,
        )  # fmt: skip
        off = make_mode(3, "Off", ModeFlags(0), ModeColors.NONE)
        self.modes = [direct, loop, ripple, off]

    def set_mode(self, mode: ModeData) -> None:
        colors = [(c.red, c.green, c.blue) for c in (mode.colors or [])]
        self.log.append(("mode", mode.name, mode.speed, mode.brightness, colors))

    def set_color(self, color: RGBColor, fast: bool = False) -> None:
        self.log.append(("color", color.red, color.green, color.blue))

    # helpers for assertions
    def colors(self) -> list[tuple[int, int, int]]:
        return [e[1:] for e in self.log if e[0] == "color"]

    def mode_names(self) -> list[str]:
        return [e[1] for e in self.log if e[0] == "mode"]


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class ScriptedAudio:
    """A feature source whose output the test controls."""

    def __init__(self) -> None:
        self.features = Features()

    def silent(self) -> None:
        """Total silence."""
        self.features = Features()

    def hiss(self, db: float = -58.0) -> None:
        """A faint noise: not a total silence, but far under the quiet threshold."""
        self.features = Features(
            bands=np.array([0.3, 0.2, 0.1, 0.1, 0.1, 0.1]), level=0.25, loudness_db=db
        )

    def loud_bass(self, db: float = -20.0) -> None:
        self.features = Features(
            bands=np.array([0.9, 0.9, 0.1, 0.1, 0.0, 0.0]),
            level=0.8,
            kick=0.8,
            loudness_db=db,
        )

    def snapshot(self) -> Features:
        return self.features.copy()


def features(bands, level=0.8, kick=0.0, snare=0.0, hat=0.0) -> Features:
    arr = np.zeros(NUM_BANDS)
    arr[: len(bands)] = bands
    return Features(arr, level, kick, snare, hat, loudness_db=-20.0)


def sine(freq: float, seconds: float, amplitude: float = 0.5, start: int = 0) -> np.ndarray:
    t = (np.arange(int(seconds * SAMPLE_RATE)) + start) / SAMPLE_RATE
    return (amplitude * np.sin(2 * np.pi * freq * t)).astype(np.float32)
