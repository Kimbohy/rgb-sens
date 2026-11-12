import numpy as np
import pytest
from fakes import sine

from rgb_sens.audio import HOP, AudioAnalyzer


def feed(analyzer: AudioAnalyzer, samples: np.ndarray) -> None:
    for i in range(0, len(samples) - HOP + 1, HOP):
        analyzer.process(samples[i : i + HOP])


def test_silence_gives_zero_features():
    analyzer = AudioAnalyzer()
    feed(analyzer, np.zeros(HOP * 200, dtype=np.float32))
    f = analyzer.snapshot()
    assert f.level == 0.0
    assert not f.bands.any()
    assert (f.kick, f.snare, f.hat) == (0.0, 0.0, 0.0)


def test_bass_tone_lights_the_low_bands_only():
    analyzer = AudioAnalyzer()
    feed(analyzer, sine(55, 0.6))
    f = analyzer.snapshot()
    assert max(f.bands[0], f.bands[1]) > 0.5
    assert f.bands[4] < 0.1 and f.bands[5] < 0.1
    assert f.level > 0.3


def test_treble_tone_lights_the_high_bands_only():
    analyzer = AudioAnalyzer()
    feed(analyzer, sine(8000, 0.6, amplitude=0.3))
    f = analyzer.snapshot()
    assert f.bands[5] > 0.5
    assert f.bands[0] < 0.1 and f.bands[1] < 0.1


def test_a_hit_gives_a_spike_but_a_steady_tone_does_not():
    analyzer = AudioAnalyzer()
    feed(analyzer, np.zeros(HOP * 100, dtype=np.float32))
    analyzer.snapshot()
    feed(analyzer, sine(55, 0.2))  # sudden hit
    hit = analyzer.snapshot()
    assert hit.kick > 0.5

    feed(analyzer, sine(55, 2.0))  # same tone, now steady
    analyzer.snapshot()
    feed(analyzer, sine(55, 0.2))
    steady = analyzer.snapshot()
    assert steady.kick < 0.1


def test_snapshot_keeps_the_peak_between_two_calls():
    analyzer = AudioAnalyzer()
    feed(analyzer, sine(55, 0.3))
    feed(analyzer, np.zeros(HOP * 8, dtype=np.float32))
    first = analyzer.snapshot()
    second = analyzer.snapshot()
    assert first.level >= second.level


def test_silence_method_forgets_everything():
    analyzer = AudioAnalyzer()
    feed(analyzer, sine(55, 0.5))
    analyzer.silence()
    f = analyzer.snapshot()
    assert f.level == 0.0 and not f.bands.any() and f.kick == 0.0


def test_silence_threshold_is_configurable():
    quiet = sine(55, 0.5, amplitude=0.001)  # about -60 dB
    low_gate = AudioAnalyzer(silence_db=-80.0)
    feed(low_gate, quiet)
    assert low_gate.snapshot().level > 0.0
    high_gate = AudioAnalyzer(silence_db=-40.0)
    feed(high_gate, quiet)
    assert high_gate.snapshot().level == 0.0


def test_loudness_is_reported_in_decibels():
    analyzer = AudioAnalyzer()
    feed(analyzer, sine(440, 0.5, amplitude=0.5))
    assert analyzer.snapshot().loudness_db == pytest.approx(-9.0, abs=1.0)


def test_silence_has_a_very_low_loudness():
    analyzer = AudioAnalyzer()
    feed(analyzer, np.zeros(HOP * 100, dtype=np.float32))
    assert analyzer.snapshot().loudness_db < -100


def test_a_faint_hiss_counts_as_quiet_by_default():
    analyzer = AudioAnalyzer()
    feed(analyzer, sine(1000, 1.0, amplitude=0.0018))  # about -58 dB: not a total silence
    f = analyzer.snapshot()
    assert -62 < f.loudness_db < -54
    assert f.level == 0.0 and not f.bands.any()


def test_wrong_block_size_is_rejected():
    with pytest.raises(ValueError):
        AudioAnalyzer().process(np.zeros(HOP + 1, dtype=np.float32))
