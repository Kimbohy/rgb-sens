# rgb-sens

Audio-reactive RGB lighting for the **Acer OMW950** gaming mouse on Linux, built on
[OpenRGB](https://openrgb.org/).

The mouse listens to what your PC plays: the bass pushes red into a soft white, the pitch of the
music can colour the whole mouse, and when the sound stops the light fades back to white.
Press the mouse's own LED button to switch between modes. One command, nothing to click.

[Français](README.fr.md)

## Modes

| # | Mode | What it does |
|---|------|--------------|
| 1 | `hw` | The mouse's own firmware effect (default: `Loop`), set to slow white. Costs no CPU. |
| 2 | `pulse` | Soft white. Red rises on every kick and lingers, highs add a light sparkle. |
| 3 | `hue` | The colour follows the pitch: bass is red, then orange, green, blue, treble is violet. Brightness follows the volume. |
| 4 | `mixer` | Bass is red, mids are green, highs are blue, and kicks add a short white flash. |

In modes 2 to 4, when the sound stops the colour is held for a moment (`--hold`, 1 s by default),
then fades slowly back to white.

## Requirements

- Linux with PulseAudio or PipeWire (`parec` from the `pulseaudio-utils` package)
- [OpenRGB](https://openrgb.org/) 1.0 or newer (the AppImage works)
- Python 3.10 or newer

Developed on Debian 13 (GNOME, Wayland) with OpenRGB 1.1.

## Install

```bash
# 1. Audio capture tool
sudo apt install pulseaudio-utils python3-venv

# 2. OpenRGB. For the AppImage on Debian 13 you also need FUSE:
sudo apt install libfuse2t64
# Install OpenRGB's udev rules (60-openrgb.rules, shipped with OpenRGB) in /etc/udev/rules.d/,
# then reload them and replug the mouse:
sudo udevadm control --reload-rules && sudo udevadm trigger

# 3. rgb-sens
git clone https://github.com/<your-name>/rgb-sens.git
cd rgb-sens
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

## Use

```bash
rgb-sens --openrgb ~/Applications/OpenRGB/OpenRGB.AppImage
```

`--openrgb` starts the OpenRGB server for you and stops it on exit. If you already run a server,
just type `rgb-sens`.

- **LED button on the mouse**: next mode.
- In the terminal: `Enter` = next mode, a number = that mode, `q` = quit.
- From anywhere (for example a GNOME keyboard shortcut): the command `rgb-sens next`.

### Commands

| Command | Purpose |
|---------|---------|
| `rgb-sens [run]` | Run the lighting (default). |
| `rgb-sens next` | Switch the running program to its next mode (for a keyboard shortcut). |
| `rgb-sens meter` | Live audio meter, no mouse needed. Use it to check what the program hears. |
| `rgb-sens info` | List the modes your mouse exposes through OpenRGB. |
| `rgb-sens hw-demo` | Play every firmware effect in white, one after the other. |
| `rgb-sens probe` | Show what the mouse sends to the PC when its LED button is pressed. |

### Tuning

| Option | Default | Effect |
|--------|---------|--------|
| `--modes` | `hw,pulse,hue,mixer` | Which modes, in which order. `--modes hue` keeps only one. |
| `--hold` | `1.0` | Seconds before the colour fades back to white once the sound stops. |
| `--smooth` | `1.0` | Softness of every transition: `1.5` softer, `0.7` snappier. |
| `--sens` | `1.0` | Audio sensitivity. The volume of the PC does not matter, this is the contrast. |
| `--silence-db` | `-66` | Level under which the sound counts as silence. |
| `--brightness` | `1.0` | Global brightness. |
| `--hw-mode` | `Loop` | Firmware effect used by the `hw` mode. |
| `--hw-speed` | `0.0` | Firmware effect speed, from 0 (slowest) to 1. |
| `--no-dither` | off | Disable temporal dithering (see below) if you see flicker. |
| `--source` | default monitor | Audio source, see `pactl list short sources`. |

## What the hardware can and cannot do

- OpenRGB sees the OMW950 as **one LED**: in software, the whole mouse always has the same colour.
  A light that travels around the mouse is therefore **not possible** from this program. Only the
  firmware effects can animate across the mouse, and with white as the colour the `Loop` effect
  shows a slow pulse. Run `rgb-sens hw-demo` to see every firmware effect in white, and pick
  the one you like with `--hw-mode`.
- The firmware also changes mode by itself when you press its LED button. rgb-sens takes control
  back about a third of a second later, so you may see a very short flash of the firmware mode.
- The mouse's USB id is `30fa:1040` (`INSTANT USB GAMING MOUSE`). Each press of the LED button
  sends two short HID reports starting with `06 f8` on the mouse's LED interface; this is what
  rgb-sens listens to. These two bytes are specific to this mouse.

## How it works

```
 sound played by the PC
        │  parec (monitor source)
        ▼
  AudioAnalyzer ── FFT → 6 bands in dB, loudness, hit detection (kick / snare / hat)
        │  Features (peak values between two frames)
        ▼
  Effect (pulse / hue / mixer) ── attack and release envelopes, hold before white
        │  linear RGB
        ▼
  Engine ── cross-fade between modes
        ▼
  Encoder ── gamma 2.2 + temporal dithering → 8-bit RGB
        ▼
  OpenRGB SDK server ── USB HID ──▶ mouse
```

A few choices that make it look smooth rather than "twitchy":

- **Attack fast, release slow.** Every value rises quickly and falls slowly, so the light has
  momentum instead of flickering.
- **One shared loudness reference for colours, one reference per band for hits.** Bass-only music
  gives red only; a steady tone gives no "hit", a drum gives a clean spike at any volume.
- **Automatic gain.** The analysis adapts to the volume, so the lighting looks the same at 10 %
  or 100 % volume.
- **Gamma and dithering.** LEDs are linear and eyes are not, and 8 bits leave visible steps at low
  brightness. Gamma evens out the fades, and carrying the rounding error from one frame to the
  next removes the steps.
- **Silence watchdog.** PipeWire stops sending samples on a monitor source when nothing plays.
  A blocking read would freeze the last colour forever; rgb-sens waits with a timeout and
  falls back to silence, which brings the white back.

## Troubleshooting

**The mouse never returns to white after the music stops.**
Run `rgb-sens meter` and stop the sound. It must show `SILENCE`. If it shows a level instead,
something is still playing quietly (a hiss, a notification sound); raise the threshold, for
example `rgb-sens --silence-db -50`.

**No reaction to the sound.**
Play something and run `rgb-sens meter`. If the bars stay empty, check
`pactl get-default-sink` and try a source from `pactl list short sources` with `--source`.
The capture follows the default output device at startup: restart after changing it.

**`Cannot reach the OpenRGB server`.**
Start OpenRGB with `--server --noautoconnect`, or pass `--openrgb /path/to/OpenRGB.AppImage`.

**The AppImage does not start (`libfuse.so.2`).** `sudo apt install libfuse2t64`.

**No mouse found, or `Permission denied`.** The OpenRGB udev rules are missing: see Install.

**The LED button does nothing.** Run `rgb-sens probe`, press the button three times and check
that reports appear. `--trigger hid:<hex>` and `--trigger evdev:<BUTTON_NAME>` (needs
`pip install -e ".[evdev]"` and membership of the `input` group) can use any other event.

**Flicker at low brightness.** Try `--no-dither`.

## Start at login (optional)

```bash
mkdir -p ~/.config/systemd/user
cp systemd/rgb-sens.service ~/.config/systemd/user/
# edit the two paths in the file, then:
systemctl --user daemon-reload
systemctl --user enable --now rgb-sens
```

## Development

```bash
pip install -e ".[dev]"
pytest
ruff check .
```

The tests need no mouse and no sound card: they use a fake OpenRGB device, scripted audio and a
fake `parec`. Source layout:

| File | Role |
|------|------|
| `audio.py` | Capture (`parec`) and analysis (FFT, bands, hits) |
| `effects.py` | The effects and the envelope helpers |
| `engine.py` | Mode list, cross-fades |
| `output.py` | Gamma and dithering |
| `device.py` | OpenRGB connection, the mouse, firmware effects |
| `triggers.py` | LED button, keyboard, `probe` |
| `app.py` | The real-time loop |
| `cli.py` | Command line |

### Adding an effect

```python
from rgb_sens.effects import Effect, register


@register
class Strobe(Effect):
    name = "strobe"
    label = "strobe: white flash on every kick"

    def render(self, t, dt, f):  # f.bands, f.level, f.kick, f.snare, f.hat
        v = f.kick
        return (v, v, v)  # linear RGB, 0..1
```

Put it in `effects.py`: it then appears in `--modes` and in the default button cycle.

## License

MIT, see [LICENSE](LICENSE).
