#!/usr/bin/env python3
"""Controle RGB de la souris Acer OMW950 via le serveur SDK d'OpenRGB.

Modes :
  white  -> respiration blanche tres douce (equivalent "Loop" tout blanc)
  audio  -> blanc doux + touches de rouge qui suivent les basses du son du PC

Usage :
  python mouse_rgb.py white
  python mouse_rgb.py audio --sens 1.0
"""
import argparse
import subprocess
import threading
import time
import math

import numpy as np
from openrgb import OpenRGBClient
from openrgb.utils import RGBColor, DeviceType

RATE = 44100
CHUNK = 512      # ~11.6 ms d'audio par lecture
FFT = 2048       # fenetre d'analyse
FPS = 60         # cadence d'envoi vers la souris


def gamma(x):
    """Correction gamma : rend les fondus lineaires a l'oeil."""
    return max(0.0, min(1.0, x)) ** 2.2


def rgb(r, g, b):
    return RGBColor(int(255 * gamma(r)), int(255 * gamma(g)), int(255 * gamma(b)))


class AudioAnalyzer:
    """Ecoute le son sortant du PC (moniteur PulseAudio/PipeWire) via parec."""

    def __init__(self, sens=1.0):
        self.bass = 0.0
        self.high = 0.0
        self.sens = sens
        self._buf = np.zeros(FFT, dtype=np.float32)
        self._peak_b = 8.0
        self._peak_h = 4.0
        self.proc = subprocess.Popen(
            ["parec", "-d", "@DEFAULT_MONITOR@", "--format=s16le",
             f"--rate={RATE}", "--channels=1", "--latency-msec=15"],
            stdout=subprocess.PIPE,
        )
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        win = np.hanning(FFT).astype(np.float32)
        freqs = np.fft.rfftfreq(FFT, 1 / RATE)
        bass_mask = (freqs >= 25) & (freqs <= 150)
        high_mask = (freqs >= 4000) & (freqs <= 12000)
        nbytes = CHUNK * 2
        while True:
            data = self.proc.stdout.read(nbytes)
            if not data or len(data) < nbytes:
                break
            x = np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0
            self._buf = np.roll(self._buf, -CHUNK)
            self._buf[-CHUNK:] = x
            spec = np.abs(np.fft.rfft(self._buf * win))
            b = float(spec[bass_mask].mean())
            h = float(spec[high_mask].mean())

            # Controle de gain automatique : s'adapte au volume du PC.
            self._peak_b = max(b, self._peak_b * 0.9995, 8.0)
            self._peak_h = max(h, self._peak_h * 0.9995, 4.0)
            lb = min(1.0, (b / self._peak_b) * self.sens) ** 1.6
            lh = min(1.0, (h / self._peak_h) * self.sens) ** 1.3

            # Enveloppe : attaque quasi instantanee, retombee douce.
            self.bass += (lb - self.bass) * (0.85 if lb > self.bass else 0.10)
            self.high += (lh - self.high) * (0.60 if lh > self.high else 0.15)


def find_mouse(client):
    mice = client.get_devices_by_type(DeviceType.MOUSE)
    if mice:
        return mice[0]
    for d in client.devices:
        if "mouse" in d.name.lower():
            return d
    raise SystemExit("Souris introuvable dans OpenRGB. Verifie le serveur et les regles udev.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["white", "audio"])
    ap.add_argument("--period", type=float, default=5.0, help="duree d'un cycle de respiration (s)")
    ap.add_argument("--sens", type=float, default=1.0, help="sensibilite audio")
    args = ap.parse_args()

    client = OpenRGBClient(address="127.0.0.1", port=6742, name="mouse_rgb")
    mouse = find_mouse(client)
    mouse.set_mode("direct")
    print(f"Souris : {mouse.name} - Ctrl+C pour arreter")

    audio = AudioAnalyzer(args.sens) if args.mode == "audio" else None

    t0 = time.perf_counter()
    nxt = t0
    try:
        while True:
            t = time.perf_counter() - t0
            breath = 0.5 - 0.5 * math.cos(2 * math.pi * t / args.period)  # 0..1 sinusoidal

            if args.mode == "white":
                v = 0.06 + 0.94 * breath
                color = rgb(v, v, v)
            else:
                base = 0.12 + 0.30 * breath          # blanc doux qui respire
                w = base * (1 - audio.bass) + 0.25 * audio.high
                r = max(w, audio.bass)               # le rouge monte avec les basses
                color = rgb(r, w * (1 - audio.bass), w * (1 - audio.bass))

            mouse.set_color(color, fast=True)

            nxt += 1 / FPS
            delay = nxt - time.perf_counter()
            if delay > 0:
                time.sleep(delay)
            else:
                nxt = time.perf_counter()
    except KeyboardInterrupt:
        pass
    finally:
        mouse.set_color(RGBColor(255, 255, 255))
        client.disconnect()


if __name__ == "__main__":
    main()
