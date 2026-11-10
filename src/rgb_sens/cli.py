"""Command line interface: ``rgb-sens [run] | info | probe | hw-demo | meter``."""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import time
from collections.abc import Sequence

from . import __version__
from .app import Runner, Settings
from .audio import (
    BAND_NAMES,
    DEFAULT_SILENCE_DB,
    AudioAnalyzer,
    AudioCapture,
    AudioError,
)
from .device import OpenRGBSession
from .effects import EFFECTS
from .engine import HARDWARE
from .triggers import (
    DEFAULT_BUTTON_TRIGGER,
    ButtonListener,
    StdinListener,
    probe,
    remove_pidfile,
    send_next,
    write_pidfile,
)

log = logging.getLogger("rgb_sens")
COMMANDS = ("run", "info", "probe", "hw-demo", "meter", "next")
_BARS = " ▁▂▃▄▅▆▇█"


def build_parser() -> argparse.ArgumentParser:
    connection = argparse.ArgumentParser(add_help=False)
    connection.add_argument("--host", default="127.0.0.1", help="OpenRGB server address")
    connection.add_argument("--port", type=int, default=6742, help="OpenRGB server port")
    connection.add_argument(
        "--openrgb",
        metavar="PATH",
        help="start this OpenRGB binary/AppImage as a server if none is running",
    )
    connection.add_argument("-v", "--verbose", action="store_true", help="debug output")

    parser = argparse.ArgumentParser(
        prog="rgb-sens",
        description="Audio-reactive RGB lighting for the Acer OMW950 mouse (via OpenRGB).",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")

    run = sub.add_parser("run", parents=[connection], help="run the lighting (default command)")
    run.add_argument(
        "--modes",
        default=",".join([HARDWARE, *[e for e in EFFECTS if e != "breath"]]),
        help=f"modes in cycling order, from: {', '.join([HARDWARE, *EFFECTS])}",
    )
    run.add_argument("--start", type=int, default=1, help="number of the first mode (default 1)")
    run.add_argument("--hw-mode", default="Loop", help="firmware effect for the 'hw' mode")
    run.add_argument(
        "--hw-speed", type=float, default=0.0, help="firmware effect speed, 0 (slow) to 1 (fast)"
    )
    run.add_argument("--hold", type=float, default=1.0, help="seconds before fading back to white")
    run.add_argument(
        "--smooth", type=float, default=1.0, help="transition softness: 1.5 softer, 0.7 snappier"
    )
    run.add_argument("--sens", type=float, default=1.0, help="audio sensitivity (0.5 to 2)")
    run.add_argument(
        "--silence-db",
        type=float,
        default=DEFAULT_SILENCE_DB,
        help="level under which the sound counts as silence (raise it if the mouse never "
        "returns to white; see the 'meter' command)",
    )
    run.add_argument("--brightness", type=float, default=1.0, help="global brightness, 0 to 1")
    run.add_argument("--no-dither", action="store_true", help="disable temporal dithering")
    run.add_argument("--fps", type=int, default=60)
    run.add_argument("--source", default="@DEFAULT_MONITOR@", help="PulseAudio/PipeWire source")
    run.add_argument(
        "--trigger",
        action="append",
        metavar="SPEC",
        help=f"what switches mode: hid:<hex> or evdev:<NAME> (default {DEFAULT_BUTTON_TRIGGER}, "
        "the LED button); can be repeated",
    )
    run.add_argument("--no-button", action="store_true", help="do not listen to the mouse button")

    sub.add_parser(
        "next",
        help="switch a running rgb-sens to its next mode (bind it to a keyboard shortcut)",
    )
    sub.add_parser("info", parents=[connection], help="list the modes the mouse exposes")

    probe_parser = sub.add_parser(
        "probe",
        parents=[connection],
        help="show what the mouse sends when its LED button is pressed",
    )
    probe_parser.add_argument("seconds", nargs="?", type=float, default=15.0)

    demo = sub.add_parser(
        "hw-demo",
        parents=[connection],
        help="play every firmware effect in white, one after the other",
    )
    demo.add_argument("--seconds", type=float, default=8.0, help="time per effect")
    demo.add_argument("--hw-speed", type=float, default=0.0, help="0 (slow) to 1 (fast)")

    meter = sub.add_parser("meter", parents=[connection], help="live audio meter (no mouse needed)")
    meter.add_argument("--sens", type=float, default=1.0)
    meter.add_argument("--silence-db", type=float, default=DEFAULT_SILENCE_DB)
    meter.add_argument("--source", default="@DEFAULT_MONITOR@")
    return parser


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(message)s" if verbose else "%(message)s",
        datefmt="%H:%M:%S",
    )


def _session(args: argparse.Namespace) -> OpenRGBSession:
    return OpenRGBSession(args.host, args.port, args.openrgb)


def cmd_info(args: argparse.Namespace) -> int:
    with _session(args) as session:
        print(session.mouse().describe())
    return 0


def cmd_probe(args: argparse.Namespace) -> int:
    return probe(args.seconds)


def cmd_hw_demo(args: argparse.Namespace) -> int:
    with _session(args) as session:
        mouse = session.mouse()
        modes = mouse.hardware_modes()
        print("Watch the mouse. Note any effect where the light travels around it.\n")
        try:
            for i, mode in enumerate(modes, 1):
                white = mouse.apply_hardware(mode.name, args.hw_speed)
                tag = "white" if white else "fixed colours"
                print(f"  [{i}/{len(modes)}] {mode.name}  ({tag})")
                time.sleep(args.seconds)
        except KeyboardInterrupt:
            print()
        finally:
            mouse.restore()
        print("\nUse one with:  rgb-sens --hw-mode NAME")
    return 0


def cmd_meter(args: argparse.Namespace) -> int:
    analyzer = AudioAnalyzer(args.sens, args.silence_db)
    capture = AudioCapture(analyzer, args.source)
    try:
        capture.start()
    except AudioError as exc:
        print(exc, file=sys.stderr)
        return 2
    print("Play some sound. Ctrl+C to quit.   bands: " + " ".join(BAND_NAMES))
    try:
        while True:
            f = analyzer.snapshot()
            bars = "".join(_BARS[min(8, int(v * 8.99))] * 3 + " " for v in f.bands)
            state = "SILENCE" if f.level == 0.0 else f"level {f.level:4.2f}"
            print(
                f"\r{bars} | {state} | kick {f.kick:4.2f} snare {f.snare:4.2f} hat {f.hat:4.2f}  ",
                end="",
                flush=True,
            )
            time.sleep(0.05)
    except KeyboardInterrupt:
        print()
    finally:
        capture.stop()
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    modes = tuple(m.strip() for m in args.modes.split(",") if m.strip())
    unknown = [m for m in modes if m != HARDWARE and m not in EFFECTS]
    if unknown or not modes:
        valid = ", ".join([HARDWARE, *EFFECTS])
        raise ValueError(
            f"unknown mode(s): {', '.join(unknown) or '(none given)'} (valid: {valid})"
        )
    settings = Settings(
        modes=modes,
        start=args.start,
        hw_mode=args.hw_mode,
        hw_speed=args.hw_speed,
        hold=args.hold,
        smooth=max(args.smooth, 0.2),
        brightness=args.brightness,
        dither=not args.no_dither,
        fps=args.fps,
    )
    capture = None
    analyzer = None
    if any(m in EFFECTS and m != "breath" for m in modes):
        analyzer = AudioAnalyzer(args.sens, args.silence_db)
        capture = AudioCapture(analyzer, args.source)
        try:
            capture.start()
        except AudioError as exc:
            print(exc, file=sys.stderr)
            return 2

    with _session(args) as session:
        mouse = session.mouse()
        runner = Runner(mouse, analyzer, settings)
        log.info("Mouse: %s", mouse.name)

        if not args.no_button:
            listener = ButtonListener(args.trigger or [DEFAULT_BUTTON_TRIGGER], runner.request_next)
            if listener.open():
                listener.start()
                log.info("Press the LED button on the mouse to change mode.")
        signal.signal(signal.SIGUSR1, lambda *_: runner.request_next("signal"))
        signal.signal(signal.SIGTERM, lambda *_: runner.stop())
        write_pidfile()  # lets `rgb-sens next` find us
        if sys.stdin.isatty():
            StdinListener(
                lambda: runner.request_next("keyboard"), runner.request_select, runner.stop
            ).start()
            log.info("Enter = next mode, a number = that mode, q = quit.")
        try:
            runner.run()
        except KeyboardInterrupt:
            pass
        finally:
            remove_pidfile()
            try:
                mouse.restore()
            except Exception:
                log.debug("could not restore the mouse", exc_info=True)
            if capture:
                capture.stop()
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args_in = list(sys.argv[1:] if argv is None else argv)
    known = (*COMMANDS, "-h", "--help", "--version")
    if not args_in or args_in[0] not in known:
        args_in.insert(0, "run")  # `rgb-sens --hold 2` means `rgb-sens run --hold 2`
    args = build_parser().parse_args(args_in)
    _setup_logging(getattr(args, "verbose", False))
    handlers = {
        "run": cmd_run,
        "info": cmd_info,
        "probe": cmd_probe,
        "hw-demo": cmd_hw_demo,
        "meter": cmd_meter,
        "next": lambda _args: send_next(),
    }
    try:
        return handlers[args.command](args)
    except (ConnectionError, LookupError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
