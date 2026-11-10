"""Audio capture and analysis.

The analyzer turns raw samples into a few numbers that effects can use:

* ``bands``  six frequency bands (sub, bass, low-mid, mid, high-mid, treble), 0..1,
  all measured against one shared reference so that "only bass" really looks like bass.
* ``level``  overall loudness, 0..1.
* ``kick`` / ``snare`` / ``hat``  short impulses when a band suddenly rises above its
  recent average. Each band is normalised on its own for this, so a steady tone gives
  nothing and a drum hit gives a clean spike whatever the volume.

Everything is normalised automatically, so the volume of the PC does not matter.
"""

from __future__ import annotations

import logging
import math
import os
import select
import subprocess
import threading
import time
from dataclasses import dataclass, field

import numpy as np

log = logging.getLogger(__name__)

SAMPLE_RATE = 44_100
HOP = 256  # ~5.8 ms between two analyses
FFT_SIZE = 2_048
BAND_EDGES_HZ = (30, 80, 200, 600, 2_000, 5_000, 14_000)
BAND_NAMES = ("sub", "bass", "low-mid", "mid", "high-mid", "treble")
NUM_BANDS = len(BAND_NAMES)

# Music has less energy in the highs than in the lows; this compensates for it.
_TILT_DB = np.array([0.0, 2.0, 5.0, 8.0, 11.0, 14.0])

DEFAULT_SILENCE_DB = -66.0
_CAPTURE_TIMEOUT = 0.15  # no samples for this long -> treat as silence
_MAX_BACKLOG_BLOCKS = 48  # ~280 ms; beyond this we drop old audio to stay real time
_KEEP_BLOCKS = 4


class AudioError(RuntimeError):
    """The audio source could not be opened."""


@dataclass
class Features:
    bands: np.ndarray = field(default_factory=lambda: np.zeros(NUM_BANDS))
    level: float = 0.0
    kick: float = 0.0
    snare: float = 0.0
    hat: float = 0.0

    def copy(self) -> Features:
        return Features(self.bands.copy(), self.level, self.kick, self.snare, self.hat)


class AudioAnalyzer:
    """Sliding FFT -> :class:`Features`. Thread safe: feed from one thread, read from another."""

    def __init__(self, sensitivity: float = 1.0, silence_db: float = DEFAULT_SILENCE_DB):
        self._sens_db = 20.0 * math.log10(max(sensitivity, 1e-3))
        self._silence_db = silence_db
        self._window = np.hanning(FFT_SIZE).astype(np.float32)
        freqs = np.fft.rfftfreq(FFT_SIZE, 1.0 / SAMPLE_RATE)
        self._masks = [
            (freqs >= lo) & (freqs < hi)
            for lo, hi in zip(BAND_EDGES_HZ[:-1], BAND_EDGES_HZ[1:], strict=True)
        ]
        self._buf = np.zeros(FFT_SIZE, dtype=np.float32)
        self._dt = HOP / SAMPLE_RATE
        self._band_ref_db = np.full(NUM_BANDS, -45.0)  # "loud" reference per band (for hits)
        self._global_ref_db = -35.0  # "loud" reference for the whole spectrum (for colours)
        self._level_ref_db = -35.0
        self._avg = np.zeros(3)  # slow averages used to spot hits: bass, mids, highs
        self._lock = threading.Lock()
        self._cur = Features()
        self._acc = Features()

    # -- analysis ---------------------------------------------------------------------------
    def process(self, block: np.ndarray) -> None:
        """Analyse one block of ``HOP`` mono float32 samples in -1..1."""
        block = np.asarray(block, dtype=np.float32)
        if block.shape != (HOP,):
            raise ValueError(f"expected {HOP} samples, got shape {block.shape}")
        self._buf[:-HOP] = self._buf[HOP:]
        self._buf[-HOP:] = block

        spectrum = np.abs(np.fft.rfft(self._buf * self._window)) / (FFT_SIZE / 4.0)
        db = np.empty(NUM_BANDS)
        for i, mask in enumerate(self._masks):
            energy = math.sqrt(float(np.mean(spectrum[mask] ** 2)))
            db[i] = 20.0 * math.log10(energy + 1e-7) + self._sens_db

        # Shared reference: used for colours.
        tilted = db + _TILT_DB
        self._global_ref_db = max(float(tilted.max()), self._global_ref_db - 3.0 * self._dt, -42.0)
        bands = np.clip((tilted - (self._global_ref_db - 34.0)) / 34.0, 0.0, 1.0) ** 1.5

        # One reference per band: used to spot hits.
        self._band_ref_db = np.maximum(np.maximum(db, self._band_ref_db - 3.0 * self._dt), -48.0)
        per_band = np.clip((db - (self._band_ref_db - 26.0)) / 26.0, 0.0, 1.0) ** 1.4

        rms = math.sqrt(float(np.mean(self._buf[-1024:] ** 2)))
        level_db = 20.0 * math.log10(rms + 1e-7) + self._sens_db
        self._level_ref_db = max(level_db, self._level_ref_db - 2.0 * self._dt, -40.0)
        level = clamp01((level_db - (self._level_ref_db - 30.0)) / 30.0) ** 1.3

        if level_db < self._silence_db:  # nothing playing: cut everything
            bands[:] = 0.0
            per_band[:] = 0.0
            level = 0.0

        grouped = np.array(
            [
                max(per_band[0], per_band[1]),
                max(per_band[2], per_band[3]),
                max(per_band[4], per_band[5]),
            ]
        )
        hits = np.clip((grouped - self._avg) / 0.30, 0.0, 1.0)
        self._avg += (grouped - self._avg) * (self._dt / 0.30)

        with self._lock:
            self._cur = Features(bands, level, float(hits[0]), float(hits[1]), float(hits[2]))
            acc = self._acc
            acc.bands = np.maximum(acc.bands, bands)
            acc.level = max(acc.level, level)
            acc.kick = max(acc.kick, self._cur.kick)
            acc.snare = max(acc.snare, self._cur.snare)
            acc.hat = max(acc.hat, self._cur.hat)

    def snapshot(self) -> Features:
        """Peak values seen since the previous call (so no kick is missed between two frames)."""
        with self._lock:
            out = self._acc.copy()
            self._acc = Features(self._cur.bands.copy(), self._cur.level)
        return out

    def silence(self) -> None:
        """Forget the audio: used when the source stops sending samples."""
        with self._lock:
            self._buf[:] = 0.0
            self._avg[:] = 0.0
            self._cur = Features()
            self._acc = Features()


class AudioCapture:
    """Feeds an :class:`AudioAnalyzer` with the sound the PC is playing (via ``parec``).

    PipeWire/PulseAudio stop delivering samples on a monitor source when nothing is playing.
    A blocking read would then freeze the last values forever, so we wait with a timeout and
    report silence instead.
    """

    def __init__(self, analyzer: AudioAnalyzer, source: str = "@DEFAULT_MONITOR@"):
        self.analyzer = analyzer
        self.source = source
        self._proc: subprocess.Popen | None = None
        self._thread: threading.Thread | None = None
        self._alive = False

    def start(self) -> None:
        try:
            self._proc = subprocess.Popen(
                [
                    "parec",
                    "-d",
                    self.source,
                    "--format=float32le",
                    f"--rate={SAMPLE_RATE}",
                    "--channels=1",
                    "--latency-msec=10",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                bufsize=0,
            )
        except FileNotFoundError as exc:
            raise AudioError(
                "`parec` not found. Install it with: sudo apt install pulseaudio-utils"
            ) from exc
        self._alive = True
        self._thread = threading.Thread(target=self._run, name="audio-capture", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._alive = False
        if self._proc is not None:
            self._proc.terminate()
            self._proc.wait(timeout=2)

    def _run(self) -> None:
        assert self._proc is not None and self._proc.stdout is not None
        fd = self._proc.stdout.fileno()
        block_bytes = HOP * 4
        pending = bytearray()
        last_data = time.monotonic()
        while self._alive:
            ready, _, _ = select.select([fd], [], [], 0.1)
            now = time.monotonic()
            if not ready:
                if now - last_data > _CAPTURE_TIMEOUT:
                    self.analyzer.silence()
                continue
            chunk = os.read(fd, 65_536)
            if not chunk:
                log.warning("Audio stream ended (parec exited). Check --source.")
                self.analyzer.silence()
                return
            last_data = now
            pending += chunk
            blocks = len(pending) // block_bytes
            if blocks > _MAX_BACKLOG_BLOCKS:  # we fell behind: skip ahead
                del pending[: (blocks - _KEEP_BLOCKS) * block_bytes]
            while len(pending) >= block_bytes:
                samples = np.frombuffer(bytes(pending[:block_bytes]), dtype=np.float32)
                del pending[:block_bytes]
                self.analyzer.process(samples)


def clamp01(x: float) -> float:
    return 0.0 if x < 0.0 else 1.0 if x > 1.0 else x
