import pytest
from fakes import FakeClock, FakeRawDevice, ScriptedAudio

from rgb_sens.app import Runner, Settings
from rgb_sens.device import MouseDevice

FPS = 60


class Harness:
    """A Runner with a fake mouse, fake audio and a fake clock that advances with the frames."""

    def __init__(self, modes=("hw", "pulse", "hue", "mixer"), loop_has_colour=True, **settings):
        self.raw = FakeRawDevice(loop_has_colour)
        self.clock = FakeClock()
        self.audio = ScriptedAudio()
        self.runner = Runner(
            MouseDevice(self.raw), self.audio, Settings(modes=tuple(modes), **settings), self.clock
        )
        self.t = 0.0
        self.runner._activate(0, self.t)

    def frames(self, seconds: float) -> None:
        for _ in range(int(seconds * FPS)):
            self.t += 1 / FPS
            self.clock.advance(1 / FPS)
            self.runner.tick(self.t, 1 / FPS)


def test_starts_in_the_hardware_mode_and_sends_no_colour():
    h = Harness()
    h.frames(1.0)
    assert h.raw.mode_names()[0] == "Loop"
    assert h.raw.colors() == []


def test_the_button_cycles_through_the_modes_and_wraps():
    h = Harness()
    seen = [h.runner.engine.current]
    for _ in range(4):
        h.clock.advance(2.0)  # let the grace period after our own mode change pass
        h.runner.request_next("button")
        h.frames(0.1)
        seen.append(h.runner.engine.current)
    assert seen == ["hw", "pulse", "hue", "mixer", "hw"]
    assert "Direct" in h.raw.mode_names()
    assert h.raw.colors()  # software modes do send colours


def test_two_reports_from_one_press_change_the_mode_only_once():
    h = Harness()
    h.clock.advance(2.0)
    h.runner.request_next("button")
    h.clock.advance(0.004)
    h.runner.request_next("button")
    h.frames(0.1)
    assert h.runner.engine.index == 1


def test_button_reports_caused_by_our_own_mode_change_are_ignored():
    h = Harness()
    h.runner.request_next("button")  # right after activation: the grace period is running
    h.frames(0.1)
    assert h.runner.engine.index == 0


def test_the_keyboard_is_not_subject_to_the_grace_period():
    h = Harness()
    h.runner.request_next("keyboard")
    h.frames(0.1)
    assert h.runner.engine.index == 1


def test_selecting_a_mode_by_number():
    h = Harness()
    h.runner.request_select(2)
    h.frames(0.1)
    assert h.runner.engine.current == "hue"


def test_the_mode_is_reasserted_shortly_after_a_change():
    h = Harness()
    h.runner.request_select(1)
    h.frames(0.1)
    before = h.raw.mode_names().count("Direct")
    h.frames(0.6)  # the mouse may have changed mode itself when its button was pressed
    assert h.raw.mode_names().count("Direct") == before + 1


def test_falls_back_to_a_software_breath_when_the_firmware_effect_has_no_colour():
    h = Harness(loop_has_colour=False)
    assert h.runner.engine.modes[0] == "breath"
    h.frames(0.5)
    assert h.raw.colors()


def test_falls_back_to_a_software_breath_when_the_firmware_effect_does_not_exist():
    h = Harness(hw_mode="Disco")
    assert h.runner.engine.modes[0] == "breath"


@pytest.mark.parametrize("mode", ["pulse", "hue", "mixer"])
def test_goes_back_to_white_when_the_audio_goes_silent(mode):
    h = Harness(modes=(mode,))
    h.audio.loud_bass()
    h.frames(4.0)
    assert max(h.raw.colors()[-1]) - min(h.raw.colors()[-1]) > 20  # clearly coloured
    h.audio.silent()
    h.frames(8.0)
    r, g, b = h.raw.colors()[-1]
    assert max(r, g, b) - min(r, g, b) <= 3  # white again (dithering can differ by a code)
    assert 5 < r < 120  # a soft white, not blinding


def test_hold_delays_the_return_to_white():
    h = Harness(modes=("hue",), hold=2.0)
    h.audio.loud_bass()
    h.frames(4.0)
    h.audio.silent()
    h.frames(1.0)
    r, g, b = h.raw.colors()[-1]
    # The hue is held (it dims, but stays saturated) one second after the sound stopped.
    assert (max(r, g, b) - min(r, g, b)) / max(r, g, b) > 0.4


def test_only_changed_colours_are_sent():
    h = Harness(modes=("breath",))
    h.frames(2.0)
    sent = h.raw.colors()
    assert all(a != b for a, b in zip(sent, sent[1:], strict=False))
