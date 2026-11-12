import pytest
from fakes import FakeClock, FakeRawDevice, ScriptedAudio

from rgb_sens.app import Runner, Settings
from rgb_sens.device import MouseDevice

FPS = 60
WHITE = (255, 255, 255)


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
        for _ in range(round(seconds * FPS)):
            self.t += 1 / FPS
            self.clock.advance(1 / FPS)
            self.runner.tick(self.t, 1 / FPS)

    def fall_asleep(self) -> None:
        """Play loud sound, then stop it long enough to be parked on the firmware effect."""
        self.audio.loud_bass()
        self.frames(3.0)
        self.audio.silent()
        self.frames(6.0)
        assert self.runner.asleep


# -- the firmware mode (mode 1) --------------------------------------------------------------


def test_starts_on_the_ripple_firmware_effect_with_white_requested():
    h = Harness()
    h.frames(1.0)
    assert h.raw.mode_names()[-1] == "Ripple"
    assert set(h.raw.colors()) == {WHITE}  # only the white used to prime the effect


def test_an_effect_without_colour_setting_is_primed_with_white_then_started():
    h = Harness()
    first, second, third = h.raw.log[:3]
    assert first[:2] == ("mode", "Direct")
    assert second == ("color", 255, 255, 255)
    assert third[1] == "Ripple"


def test_an_effect_with_a_colour_setting_gets_white_directly():
    h = Harness(hw_mode="Loop")
    h.frames(1.0)
    assert h.raw.log[0][1:] == ("Loop", 0, 7, [WHITE])
    assert h.raw.colors() == []


def test_falls_back_to_a_soft_breath_when_the_firmware_effect_does_not_exist():
    h = Harness(hw_mode="Disco")
    assert h.runner.engine.modes[0] == "breath"
    h.frames(0.5)
    assert h.raw.colors()


# -- changing mode ---------------------------------------------------------------------------


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


def test_only_changed_colours_are_sent():
    h = Harness(modes=("breath",))
    h.frames(2.0)
    sent = h.raw.colors()
    assert all(a != b for a, b in zip(sent, sent[1:], strict=False))


# -- fading back to white (idle switching disabled) ------------------------------------------


@pytest.mark.parametrize("mode", ["pulse", "hue", "mixer"])
def test_fades_back_to_white_when_the_audio_stops(mode):
    h = Harness(modes=(mode,), idle_to_hw=False)
    h.audio.loud_bass()
    h.frames(4.0)
    assert max(h.raw.colors()[-1]) - min(h.raw.colors()[-1]) > 20  # clearly coloured
    h.audio.silent()
    h.frames(8.0)
    r, g, b = h.raw.colors()[-1]
    assert max(r, g, b) - min(r, g, b) <= 3  # white again (dithering can differ by a code)
    assert 5 < r < 120  # a soft white, not blinding
    assert not h.runner.asleep


def test_hold_delays_the_return_to_white():
    h = Harness(modes=("hue",), hold=2.0, idle_to_hw=False)
    h.audio.loud_bass()
    h.frames(4.0)
    h.audio.silent()
    h.frames(1.0)
    r, g, b = h.raw.colors()[-1]
    # The hue is held (it dims, but stays saturated) one second after the sound stopped.
    assert (max(r, g, b) - min(r, g, b)) / max(r, g, b) > 0.4


# -- back to the firmware effect when it is quiet --------------------------------------------


@pytest.mark.parametrize("mode", ["pulse", "hue", "mixer"])
def test_goes_back_to_ripple_when_the_sound_stops(mode):
    h = Harness(modes=(mode,))
    h.audio.loud_bass()
    h.frames(3.0)
    assert not h.runner.asleep
    h.audio.silent()
    h.frames(2.0)
    assert not h.runner.asleep  # still inside the delay
    h.frames(3.0)
    assert h.runner.asleep
    assert h.raw.mode_names()[-1] == "Ripple"
    assert h.raw.colors()[-1] == WHITE  # primed with white


@pytest.mark.parametrize("mode", ["pulse", "hue", "mixer"])
def test_a_faint_hiss_counts_as_quiet(mode):
    """The case that was broken: not a total silence, just a very low level."""
    h = Harness(modes=(mode,))
    h.audio.loud_bass()
    h.frames(3.0)
    h.audio.hiss(-58.0)
    h.frames(6.0)
    assert h.runner.asleep
    assert h.raw.mode_names()[-1] == "Ripple"


def test_music_just_above_the_threshold_keeps_the_lights_on():
    h = Harness(modes=("hue",))
    h.audio.loud_bass(db=-45.0)  # the default threshold is -50 dB
    h.frames(10.0)
    assert not h.runner.asleep


def test_the_threshold_can_be_changed():
    h = Harness(modes=("hue",), silence_db=-40.0)
    h.audio.loud_bass(db=-45.0)
    h.frames(6.0)
    assert h.runner.asleep


def test_a_short_pause_in_the_music_does_not_trigger_it():
    h = Harness(modes=("hue",))
    for _ in range(2):
        h.audio.loud_bass()
        h.frames(2.0)
        h.audio.silent()
        h.frames(3.0)  # under the 4 s delay
    assert not h.runner.asleep
    assert "Ripple" not in h.raw.mode_names()


def test_wakes_up_when_the_sound_comes_back():
    h = Harness(modes=("hue",))
    h.fall_asleep()
    sent_before = len(h.raw.colors())
    h.audio.loud_bass()
    h.frames(0.5)
    assert not h.runner.asleep
    assert h.raw.mode_names()[-1] == "Direct"
    assert len(h.raw.colors()) > sent_before  # and the lights react again


def test_a_blip_does_not_wake_it_up():
    h = Harness(modes=("hue",))
    h.fall_asleep()
    h.audio.loud_bass()
    h.frames(0.05)  # far shorter than the confirmation time
    h.audio.silent()
    h.frames(1.0)
    assert h.runner.asleep


def test_a_level_just_over_the_threshold_does_not_wake_it_up():
    h = Harness(modes=("hue",))
    h.fall_asleep()
    h.audio.loud_bass(db=-49.0)  # above -50 dB but inside the 3 dB hysteresis margin
    h.frames(2.0)
    assert h.runner.asleep


def test_the_button_while_parked_goes_to_the_next_mode():
    h = Harness(modes=("pulse", "hue"))
    h.fall_asleep()
    h.clock.advance(2.0)
    h.runner.request_next("button")
    h.frames(0.2)
    assert h.runner.engine.current == "hue"
    assert not h.runner.asleep


def test_idle_can_be_disabled():
    h = Harness(modes=("hue",), idle_to_hw=False)
    h.audio.silent()
    h.frames(10.0)
    assert not h.runner.asleep
    assert "Ripple" not in h.raw.mode_names()


def test_no_idle_switching_without_a_firmware_effect_to_go_back_to():
    h = Harness(modes=("hue",), hw_mode="Disco")
    h.frames(10.0)
    assert not h.runner.asleep


def test_the_plain_breath_effect_never_goes_idle():
    h = Harness(modes=("breath",))
    h.frames(10.0)
    assert not h.runner.asleep
