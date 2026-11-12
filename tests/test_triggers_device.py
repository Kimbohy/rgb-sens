import pytest
from fakes import FakeRawDevice

from rgb_sens.device import ModeNotFoundError, MouseDevice
from rgb_sens.triggers import (
    DEFAULT_BUTTON_TRIGGER,
    Debouncer,
    parse_trigger,
    report_matches,
)

# -- triggers --------------------------------------------------------------------------------


def test_parse_trigger():
    assert parse_trigger("hid:06f6") == ("hid", b"\x06\xf6")
    assert parse_trigger("hid:06 f6 02") == ("hid", b"\x06\xf6\x02")
    assert parse_trigger("evdev:BTN_EXTRA") == ("evdev", "BTN_EXTRA")


@pytest.mark.parametrize("bad", ["", "hid", "hid:", "usb:1234", "hid:zz"])
def test_parse_trigger_rejects_garbage(bad):
    with pytest.raises(ValueError):
        parse_trigger(bad)


def test_the_default_trigger_is_the_led_button_report():
    assert DEFAULT_BUTTON_TRIGGER == "hid:06f6"
    _, prefix = parse_trigger(DEFAULT_BUTTON_TRIGGER)
    assert report_matches(bytes.fromhex("06f601"), [prefix])
    # Another family of reports (06 f8) shows up on the same interface: it is not the button.
    assert not report_matches(bytes.fromhex("06f802"), [prefix])
    assert not report_matches(bytes.fromhex("0100ff00"), [prefix])  # e.g. pointer movement


def test_several_reports_for_one_press_count_as_one_press():
    debounce = Debouncer(0.3)
    reports = [0.000, 0.004, 1.000, 1.003, 2.000, 2.002]
    presses = [t for t in reports if debounce.ready(t)]
    assert presses == [0.000, 1.000, 2.000]


# -- device ----------------------------------------------------------------------------------


@pytest.fixture
def raw():
    return FakeRawDevice()


def test_hardware_modes_exclude_direct_and_off(raw):
    assert [m.name for m in MouseDevice(raw).hardware_modes()] == ["Loop", "Ripple"]


def test_apply_hardware_sets_white_slowest_and_brightest(raw):
    assert MouseDevice(raw).apply_hardware("loop", speed=0.0) is True
    name, speed, brightness, colours = raw.log[-1][1:]
    assert (name, speed, brightness) == ("Loop", 0, 7)
    assert colours == [(255, 255, 255)]
    assert raw.colors() == []  # an effect with a colour setting needs no priming


def test_apply_hardware_speed_scales_between_min_and_max(raw):
    MouseDevice(raw).apply_hardware("Loop", speed=1.0)
    assert raw.log[-1][2] == 5


def test_a_mode_without_colour_is_reported_as_not_guaranteed_white(raw):
    mouse = MouseDevice(raw)
    assert mouse.can_be_white(mouse.find_mode("Ripple")) is False
    assert mouse.apply_hardware("Ripple") is False


def test_a_mode_without_colour_is_primed_with_white_first(raw):
    MouseDevice(raw).apply_hardware("Ripple")
    first, second, third = raw.log
    assert first[:2] == ("mode", "Direct")
    assert second == ("color", 255, 255, 255)
    assert third[1:] == ("Ripple", 0, 7, [])  # slowest, brightest, no colour list


def test_a_loop_without_colour_setting_is_treated_like_ripple():
    raw = FakeRawDevice(loop_has_colour=False)
    mouse = MouseDevice(raw)
    assert mouse.apply_hardware("Loop") is False
    assert raw.log[-1][4] == []


def test_unknown_mode_error_lists_the_available_ones(raw):
    with pytest.raises(ModeNotFoundError, match="Ripple"):
        MouseDevice(raw).find_mode("Disco")


def test_restore_goes_back_to_direct_white(raw):
    MouseDevice(raw).restore()
    assert raw.mode_names() == ["Direct"]
    assert raw.colors() == [(255, 255, 255)]


def test_describe_mentions_every_mode(raw):
    text = MouseDevice(raw).describe()
    assert all(name in text for name in ("Direct", "Loop", "Ripple", "Off"))
