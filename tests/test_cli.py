import subprocess
import sys
import time

import pytest

from rgb_sens import __version__
from rgb_sens.cli import build_parser, main
from rgb_sens.triggers import is_rgb_sens_process, pidfile_path, send_next


def test_version(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert __version__ in capsys.readouterr().out


def test_options_without_a_command_mean_run():
    # `rgb-sens --hold 2` must behave like `rgb-sens run --hold 2`
    args = build_parser().parse_args(["run", "--hold", "2"])
    assert args.command == "run" and args.hold == 2.0


def test_hw_demo_can_play_a_single_effect():
    args = build_parser().parse_args(["hw-demo", "--only", "Ripple"])
    assert args.only == "Ripple"


def test_run_defaults_match_the_documented_behaviour():
    args = build_parser().parse_args(["run"])
    assert args.hw_mode == "Ripple"
    assert args.silence_db == -50.0
    assert args.idle_delay == 4.0 and args.no_idle is False


def test_a_bad_mode_is_reported_before_trying_to_connect(capsys):
    assert main(["--modes", "hw,disco", "--port", "1"]) == 1
    err = capsys.readouterr().err
    assert "unknown mode" in err and "disco" in err
    assert "Cannot reach" not in err


def test_a_missing_openrgb_server_gives_a_helpful_message(capsys):
    assert main(["info", "--port", "1"]) == 1
    assert "--server" in capsys.readouterr().err


def test_meter_without_parec_fails_cleanly(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("PATH", str(tmp_path))
    assert main(["meter"]) == 2
    assert "pulseaudio-utils" in capsys.readouterr().err


# -- `rgb-sens next` ------------------------------------------------------------------------


def wait_until(predicate, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def test_next_when_nothing_is_running(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    assert main(["next"]) == 1
    assert "not running" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("cmdline", "expected"),
    [
        # the installed script, run from a venv inside a folder that is also called rgb-sens
        (
            b"/home/kim/code/project/rgb-sens/.venv/bin/python\0/home/kim/code/project/rgb-sens/.venv/bin/rgb-sens\0run\0",
            True,
        ),
        (b"/usr/bin/python3\0-m\0rgb_sens\0--hold\02\0", True),
        # strangers whose command line only *contains* the name in a path
        (b"/home/kim/code/project/rgb-sens/.venv/bin/python\0-c\0import time\0", False),
        (b"vim\0/home/kim/code/project/rgb-sens/README.md\0", False),
        (b"python3\0-m\0pytest\0/home/kim/code/project/rgb-sens/tests\0", False),
        (b"", False),
    ],
)
def test_only_the_program_itself_is_recognised(cmdline, expected):
    assert is_rgb_sens_process(cmdline) is expected


def test_next_refuses_to_signal_an_unrelated_process(monkeypatch, tmp_path, capsys):
    """A stale pid file must never make us send SIGUSR1 (which kills by default) to a stranger,
    even one that lives in a folder called rgb-sens."""
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    folder = tmp_path / "rgb-sens"
    folder.mkdir()
    other = folder / "other.py"
    other.write_text("import time\ntime.sleep(30)\n")
    stranger = subprocess.Popen([sys.executable, str(other)])
    try:
        pidfile_path().write_text(str(stranger.pid))
        assert send_next() == 1
        assert "stale" in capsys.readouterr().err
        time.sleep(0.1)
        assert stranger.poll() is None  # still alive
    finally:
        stranger.kill()


def test_next_signals_a_running_rgb_sens(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    ready, marker = tmp_path / "ready", tmp_path / "got-signal"
    # A script called `rgb-sens`, like the one pip installs, so the command line looks real.
    script = tmp_path / "rgb-sens"
    script.write_text(
        "import pathlib, signal, time\n"
        f"marker = pathlib.Path({str(marker)!r})\n"
        "signal.signal(signal.SIGUSR1, lambda *a: marker.write_text('x'))\n"
        f"pathlib.Path({str(ready)!r}).write_text('ready')\n"
        "time.sleep(30)\n"
    )
    proc = subprocess.Popen([sys.executable, str(script)])
    try:
        assert wait_until(ready.exists)
        pidfile_path().write_text(str(proc.pid))
        assert send_next() == 0
        assert wait_until(marker.exists)
    finally:
        proc.kill()
