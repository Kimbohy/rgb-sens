"""Turning linear colours into the 8-bit values the mouse expects."""

from __future__ import annotations

from .effects import RGB, clamp01


class Encoder:
    """Gamma correction plus temporal dithering.

    LEDs are driven linearly but eyes are not, so a gamma curve makes fades look even.
    At low brightness 8 bits leave visible steps; carrying the rounding error from one
    frame to the next (dithering) makes slow fades look continuous.
    """

    def __init__(self, gamma: float = 2.2, dither: bool = True, master: float = 1.0):
        self.gamma = gamma
        self.dither = dither
        self.master = master
        self._error = [0.0, 0.0, 0.0]

    def encode(self, rgb: RGB) -> tuple[int, int, int]:
        out = []
        for i, channel in enumerate(rgb):
            value = clamp01(channel * self.master) ** self.gamma * 255.0
            if self.dither:
                value += self._error[i]
                q = min(255, max(0, round(value)))
                self._error[i] = max(-1.0, min(1.0, value - q))
            else:
                q = round(value)
            out.append(q)
        return (out[0], out[1], out[2])
