import numpy as np
import pytest
from fakes import features

from rgb_sens.audio import Features
from rgb_sens.effects import Breath
from rgb_sens.engine import HARDWARE, Engine
from rgb_sens.output import Encoder

# -- encoder ---------------------------------------------------------------------------------


def test_encoder_endpoints_and_monotonic():
    enc = Encoder(dither=False)
    assert enc.encode((0.0, 0.0, 0.0)) == (0, 0, 0)
    assert enc.encode((1.0, 1.0, 1.0)) == (255, 255, 255)
    values = [enc.encode((x / 100,) * 3)[0] for x in range(101)]
    assert values == sorted(values)


def test_encoder_clamps_out_of_range_input():
    enc = Encoder()
    assert enc.encode((-1.0, 5.0, 0.5))[0] == 0
    assert enc.encode((-1.0, 5.0, 0.5))[1] == 255


def test_dithering_preserves_the_average_of_dim_colours():
    enc = Encoder(dither=True)
    target = 0.12**2.2 * 255  # a dim level that does not fall on a whole number
    outputs = [enc.encode((0.12, 0.12, 0.12))[0] for _ in range(1200)]
    assert abs(np.mean(outputs) - target) < 0.02
    assert len(set(outputs)) > 1  # it really alternates between neighbouring codes


def test_master_brightness_scales_the_output():
    full = Encoder(dither=False).encode((0.8, 0.8, 0.8))[0]
    half = Encoder(dither=False, master=0.5).encode((0.8, 0.8, 0.8))[0]
    assert half < full


# -- engine ----------------------------------------------------------------------------------


def test_unknown_or_empty_modes_are_rejected():
    with pytest.raises(ValueError, match="unknown"):
        Engine(["pulse", "disco"])
    with pytest.raises(ValueError):
        Engine([])


def test_select_wraps_around_and_flags_hardware():
    engine = Engine([HARDWARE, "pulse", "hue"])
    engine.select(0, 0.0)
    assert engine.is_hardware and engine.effect is None
    engine.select(4, 0.0)  # 4 % 3 == 1
    assert engine.current == "pulse" and not engine.is_hardware
    engine.select(-1, 0.0)
    assert engine.current == "hue"


def test_stepping_in_hardware_mode_is_an_error():
    engine = Engine([HARDWARE])
    engine.select(0, 0.0)
    with pytest.raises(RuntimeError):
        engine.step(0.0, 1 / 60, Features())


def test_crossfade_starts_from_the_previous_colour_and_reaches_the_new_effect():
    engine = Engine(["breath"])
    engine.select(0, 0.0)
    first = engine.step(0.0, 1 / 60, Features())
    assert first == pytest.approx((0.2, 0.2, 0.2))  # still the initial colour at the start
    t = Engine.CROSSFADE + 0.5
    later = engine.step(t, 1 / 60, Features())
    expected = Breath().render(t, 1 / 60, Features())
    assert later == pytest.approx(expected)


def test_switching_effects_keeps_the_last_colour_as_the_fade_origin():
    engine = Engine(["pulse", "mixer"])
    engine.select(0, 0.0)
    hit = features([0.9, 0.9, 0, 0, 0, 0], kick=0.8)
    last = (0.0, 0.0, 0.0)
    for i in range(120):
        last = engine.step(i / 60, 1 / 60, hit)
    engine.select(1, 2.0)
    start = engine.step(2.0, 1 / 60, hit)
    assert start == pytest.approx(last, abs=1e-6)
