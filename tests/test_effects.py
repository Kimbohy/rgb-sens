import numpy as np
import pytest
from fakes import features

from rgb_sens.audio import Features
from rgb_sens.effects import EFFECTS, Effect, EffectConfig
from rgb_sens.output import Encoder

FPS = 60
AUDIO_EFFECTS = ["pulse", "hue", "mixer"]


def run(effect: Effect, feature_for, seconds: float, start: float = 0.0):
    """Render ``seconds`` of video; ``feature_for(t)`` gives the audio features at time t."""
    out = []
    for i in range(int(seconds * FPS)):
        t = start + i / FPS
        out.append(effect.render(t, 1 / FPS, feature_for(t)))
    return np.array(out)


def colourfulness(rgb) -> float:
    return (max(rgb) - min(rgb)) / max(max(rgb), 1e-6)


def test_registry_contains_all_effects():
    assert {"breath", "pulse", "hue", "mixer"} <= set(EFFECTS)
    assert all(cls.label for cls in EFFECTS.values())


@pytest.mark.parametrize("name", AUDIO_EFFECTS)
def test_output_is_finite_and_in_range_for_random_input(name):
    rng = np.random.default_rng(0)
    effect = EFFECTS[name]()
    for i in range(600):
        f = Features(rng.random(6), rng.random(), rng.random(), rng.random(), rng.random())
        r, g, b = effect.render(i / FPS, 1 / FPS, f)
        assert all(np.isfinite([r, g, b]))
        assert all(-1e-9 <= c <= 2.0 for c in (r, g, b))


@pytest.mark.parametrize("name", AUDIO_EFFECTS)
def test_returns_to_white_after_the_sound_stops(name):
    effect = EFFECTS[name]()
    loud = features([0.9, 0.9, 0.1, 0.1, 0, 0], level=0.8, kick=0.8)
    run(effect, lambda t: loud, 4.0)
    tail = run(effect, lambda t: Features(), 8.0, start=4.0)
    assert colourfulness(tail[-1]) < 0.05, "should be white again"
    assert 0.1 < tail[-1].mean() < 0.4, "white should be soft, not blinding"


@pytest.mark.parametrize("name", ["hue", "mixer"])
def test_colour_is_held_before_fading(name):
    effect = EFFECTS[name](EffectConfig(hold=1.0))
    loud = features([0.9, 0.9, 0.1, 0.1, 0, 0], level=0.8, kick=0.8)
    run(effect, lambda t: loud, 4.0)
    tail = run(effect, lambda t: Features(), 1.0, start=4.0)
    assert colourfulness(tail[int(0.5 * FPS)]) > 0.3


def test_pulse_is_red_on_the_bass():
    effect = EFFECTS["pulse"]()
    f = features([0.9, 0.9, 0, 0, 0, 0], kick=0.8)
    r, g, b = run(effect, lambda t: f, 2.0)[-1]
    assert r > g + 0.2 and r > b + 0.2


def test_hue_goes_from_red_to_blue_with_pitch():
    low = run(EFFECTS["hue"](), lambda t: features([0.9, 0.9, 0, 0, 0, 0]), 3.0)[-1]
    high = run(EFFECTS["hue"](), lambda t: features([0, 0, 0, 0, 0.9, 0.9]), 3.0)[-1]
    assert low[0] > low[1] and low[0] > low[2]
    assert high[2] > high[0]


def test_mixer_channels_follow_their_bands():
    bass = run(EFFECTS["mixer"](), lambda t: features([0.9, 0.9, 0, 0, 0, 0]), 3.0)[-1]
    mids = run(EFFECTS["mixer"](), lambda t: features([0, 0, 0.9, 0.9, 0, 0]), 3.0)[-1]
    highs = run(EFFECTS["mixer"](), lambda t: features([0, 0, 0, 0, 0.9, 0.9]), 3.0)[-1]
    assert bass.argmax() == 0 and mids.argmax() == 1 and highs.argmax() == 2


@pytest.mark.parametrize("name", AUDIO_EFFECTS)
def test_a_sudden_hit_never_jumps_too_far_in_one_frame(name):
    """Guards smoothness: on a hard step, no frame may change by more than ~1/3 of the range."""
    effect = EFFECTS[name]()
    hit = features([0.9, 0.9, 0.5, 0.5, 0.3, 0.3], level=0.9, kick=1.0, snare=1.0, hat=1.0)
    frames = run(effect, lambda t: hit if t >= 1.0 else Features(), 3.0)
    encoder = Encoder(dither=False)
    codes = np.array([encoder.encode(tuple(c)) for c in frames], dtype=float)
    assert np.abs(np.diff(codes, axis=0)).max() <= 100


def test_smoother_setting_slows_the_attack():
    hit = features([0.9, 0.9, 0, 0, 0, 0], kick=1.0)
    fast = run(EFFECTS["pulse"](EffectConfig(smooth=0.7)), lambda t: hit, 0.1)[-1]
    slow = run(EFFECTS["pulse"](EffectConfig(smooth=2.0)), lambda t: hit, 0.1)[-1]
    assert fast[0] - fast[1] > slow[0] - slow[1]
