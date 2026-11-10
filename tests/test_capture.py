"""AudioCapture against a fake `parec`, including the "source goes quiet" case."""

import stat
import sys
import time

import pytest

from rgb_sens.audio import AudioAnalyzer, AudioCapture, AudioError

FAKE_PAREC = """#!{python}
import math, struct, sys, time
RATE = 44100
# one second of a 55 Hz tone, sent in real time ...
for chunk in range(100):
    n = RATE // 100
    samples = [0.5 * math.sin(2 * math.pi * 55 * (chunk * n + i) / RATE) for i in range(n)]
    sys.stdout.buffer.write(struct.pack("<%df" % n, *samples))
    sys.stdout.buffer.flush()
    time.sleep(0.01)
# ... then, like an idle PipeWire sink, stay alive but send nothing at all.
time.sleep(60)
"""


@pytest.fixture
def fake_parec(tmp_path, monkeypatch):
    script = tmp_path / "parec"
    script.write_text(FAKE_PAREC.format(python=sys.executable))
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{tmp_path}:/usr/bin:/bin")
    return script


def wait_for(predicate, timeout):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def test_reports_the_sound_then_silence_when_the_source_stops_sending(fake_parec):
    analyzer = AudioAnalyzer()
    capture = AudioCapture(analyzer)
    capture.start()
    try:
        heard = []
        assert wait_for(lambda: heard.append(analyzer.snapshot().level) or max(heard) > 0.3, 3.0)
        # The fake source now sends nothing. Without the watchdog the last values would stay
        # frozen forever; with it, the analyzer must fall back to silence.
        assert wait_for(lambda: analyzer.snapshot().level == 0.0, 3.0)
    finally:
        capture.stop()


def test_a_missing_parec_gives_a_helpful_error(monkeypatch, tmp_path):
    monkeypatch.setenv("PATH", str(tmp_path))
    with pytest.raises(AudioError, match="pulseaudio-utils"):
        AudioCapture(AudioAnalyzer()).start()
