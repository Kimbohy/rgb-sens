#!/usr/bin/env python3
"""RGB audio-reactif pour la souris Acer OMW950 (Instant USB Gaming Mouse) via OpenRGB.

Un seul script, plusieurs modes qu'on parcourt avec un "bouton" :

  1. loop      Mode Loop du firmware : lumiere blanche qui tourne lentement
               (la souris fait elle-meme l'animation, le script ne touche plus a rien)
  2. pulse     Blanc doux + rouge sur les basses (kick), scintillement sur les aigus
  3. hue       La couleur suit la "hauteur" du son (graves=rouge ... aigus=violet)
  4. mixer     Basses=rouge, mediums=vert, aigus=bleu, avec flash sur les kicks

Dans les modes 2 a 4, quand le son s'arrete la souris attend un moment puis
revient doucement au blanc.

Prerequis : serveur OpenRGB lance (./OpenRGB.AppImage --server --noautoconnect),
pip install openrgb-python numpy, et `parec` (paquet pulseaudio-utils).

Aide pour le bouton de la souris :  python3 mouse_rgb.py --probe
"""
import argparse
import collections
import colorsys
import glob
import math
import os
import select
import signal
import subprocess
import sys
import threading
import time

import numpy as np

SAMPLE_RATE = 44100
HOP = 256                       # ~5.8 ms entre deux analyses
FFT_SIZE = 2048
BAND_EDGES = [30, 80, 200, 600, 2000, 5000, 14000]   # 6 bandes : sub, basses, bas-med, med, haut-med, aigus
NB = len(BAND_EDGES) - 1
TILT_DB = np.array([0.0, 2.0, 5.0, 8.0, 11.0, 14.0])   # compense le fait que les aigus sont plus faibles que les graves

MOUSE_VID = 0x30FA
MOUSE_PID = 0x1040
MOUSE_HID_ID = "0003:000030FA:00001040"


# --------------------------------------------------------------------------
# Petits outils mathematiques
# --------------------------------------------------------------------------
def clamp01(x):
    return 0.0 if x < 0.0 else 1.0 if x > 1.0 else x


def smoothstep(x):
    x = clamp01(x)
    return x * x * (3.0 - 2.0 * x)


def lerp(a, b, t):
    return a + (b - a) * t


SMOOTH = [1.0]      # multiplie tous les temps de reponse (--smooth) : >1 = plus doux, <1 = plus nerveux


def follow(cur, target, dt, tau_up, tau_down):
    """Rapproche cur de target, independamment du nombre d'images par seconde.
    tau_up : vitesse quand ca monte (attaque), tau_down : quand ca descend (relache)."""
    tau = (tau_up if target > cur else tau_down) * SMOOTH[0]
    return cur + (target - cur) * (1.0 - math.exp(-dt / tau))


class HoldRelease:
    """Monte vite, reste en place un moment (hold), puis redescend doucement.
    C'est ce qui donne le "delai avant de revenir au blanc"."""

    def __init__(self, attack, hold, release):
        self.attack, self.hold, self.release = attack, hold, release
        self.v = 0.0
        self.hold_until = 0.0

    def update(self, target, t, dt):
        if target > self.v + 0.01:
            self.v += (target - self.v) * (1.0 - math.exp(-dt / (self.attack * SMOOTH[0])))
            self.hold_until = t + self.hold
        elif t >= self.hold_until:
            self.v += (target - self.v) * (1.0 - math.exp(-dt / (self.release * SMOOTH[0])))
        return self.v


# --------------------------------------------------------------------------
# Analyse audio
# --------------------------------------------------------------------------
class Features:
    __slots__ = ("bands", "level", "kick", "snare", "hat")

    def __init__(self):
        self.bands = np.zeros(NB)
        self.level = 0.0     # volume global normalise 0..1
        self.kick = 0.0      # impulsion sur les attaques de basses
        self.snare = 0.0     # impulsion sur les attaques de mediums
        self.hat = 0.0       # impulsion sur les attaques d'aigus


class AudioAnalyzer:
    """FFT glissante -> 6 bandes en dB, auto-normalisees, plus detection d'attaques.

    - normalisation automatique par bande (le volume du PC n'a pas d'importance)
    - les attaques = ce qui depasse la moyenne recente de la bande
    - snapshot() rend le maximum vu depuis le dernier appel, pour ne rater aucun kick
    """

    def __init__(self, sens=1.0):
        self.sens_db = 20.0 * math.log10(max(sens, 1e-3))
        self.win = np.hanning(FFT_SIZE).astype(np.float32)
        freqs = np.fft.rfftfreq(FFT_SIZE, 1.0 / SAMPLE_RATE)
        self.masks = [(freqs >= lo) & (freqs < hi) for lo, hi in zip(BAND_EDGES[:-1], BAND_EDGES[1:])]
        self.buf = np.zeros(FFT_SIZE, dtype=np.float32)
        self.dt = HOP / SAMPLE_RATE
        self.hi_db = np.full(NB, -45.0)       # reference "fort" par bande (sert aux attaques)
        self.g_hi = -35.0                     # reference "fort" globale (sert aux couleurs)
        self.level_hi = -35.0
        self.avg = np.zeros(3)                # moyennes lentes : kick, snare, hat
        self.lock = threading.Lock()
        self.cur = Features()
        self.acc = Features()
        self.proc = None
        self.alive = True

    # -- traitement d'un bloc de HOP echantillons ---------------------------
    def process(self, x):
        self.buf[:-HOP] = self.buf[HOP:]
        self.buf[-HOP:] = x
        spec = np.abs(np.fft.rfft(self.buf * self.win)) / (FFT_SIZE / 4.0)
        db = np.empty(NB)
        for i, m in enumerate(self.masks):
            e = math.sqrt(float(np.mean(spec[m] ** 2)))
            db[i] = 20.0 * math.log10(e + 1e-7) + self.sens_db

        # bandes "globales" : une seule reference pour tout le spectre (avec une compensation de la
        # pente naturelle de la musique), donc des basses seules donnent du rouge seul
        dbt = db + TILT_DB
        self.g_hi = max(float(dbt.max()), self.g_hi - 3.0 * self.dt, -42.0)
        bands = np.clip((dbt - (self.g_hi - 34.0)) / 34.0, 0.0, 1.0) ** 1.5
        # bandes "relatives" : normalisees chacune a leur propre niveau, pour reperer les attaques
        self.hi_db = np.maximum(np.maximum(db, self.hi_db - 3.0 * self.dt), -48.0)
        nbands = np.clip((db - (self.hi_db - 26.0)) / 26.0, 0.0, 1.0) ** 1.4

        rms = math.sqrt(float(np.mean(self.buf[-1024:] ** 2)))
        ldb = 20.0 * math.log10(rms + 1e-7) + self.sens_db
        self.level_hi = max(ldb, self.level_hi - 2.0 * self.dt, -40.0)
        level = clamp01((ldb - (self.level_hi - 30.0)) / 30.0) ** 1.3
        if ldb < -66.0:                       # silence : on coupe tout
            bands[:] = 0.0
            nbands[:] = 0.0
            level = 0.0

        agg = np.array([max(nbands[0], nbands[1]), max(nbands[2], nbands[3]), max(nbands[4], nbands[5])])
        tr = np.clip((agg - self.avg) / 0.30, 0.0, 1.0)
        self.avg += (agg - self.avg) * (self.dt / 0.30)

        with self.lock:
            self.cur.bands, self.cur.level = bands, level
            self.cur.kick, self.cur.snare, self.cur.hat = float(tr[0]), float(tr[1]), float(tr[2])
            a = self.acc
            a.bands = np.maximum(a.bands, bands)
            a.level = max(a.level, level)
            a.kick, a.snare, a.hat = max(a.kick, self.cur.kick), max(a.snare, self.cur.snare), max(a.hat, self.cur.hat)

    def snapshot(self):
        with self.lock:
            f = Features()
            f.bands, f.level = self.acc.bands.copy(), self.acc.level
            f.kick, f.snare, f.hat = self.acc.kick, self.acc.snare, self.acc.hat
            self.acc.bands = self.cur.bands.copy()
            self.acc.level = self.cur.level
            self.acc.kick = self.acc.snare = self.acc.hat = 0.0
        return f

    # -- lecture du son du PC ----------------------------------------------
    def start(self, source="@DEFAULT_MONITOR@"):
        self.proc = subprocess.Popen(
            ["parec", "-d", source, "--format=float32le", f"--rate={SAMPLE_RATE}",
             "--channels=1", "--latency-msec=10"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        n = HOP * 4
        while self.alive:
            data = self.proc.stdout.read(n)
            if not data or len(data) < n:
                print("[audio] flux audio interrompu (parec). Verifie la sortie son / --source.")
                break
            self.process(np.frombuffer(data, dtype=np.float32))

    def stop(self):
        self.alive = False
        if self.proc:
            self.proc.terminate()


# --------------------------------------------------------------------------
# Effets (tout est calcule en lumiere lineaire 0..1 ; le gamma est applique a la fin)
# --------------------------------------------------------------------------
def idle_white(t, level=0.0):
    """Le blanc de repos : doux, respire tres lentement, monte un peu avec le volume."""
    breath = 0.5 - 0.5 * math.cos(2 * math.pi * t / 7.0)
    return 0.20 + 0.07 * breath * (1.0 - level) + 0.30 * level


class WhiteBreath:
    """Secours si le mode Loop du firmware ne sait pas faire de blanc."""
    label = "blanc qui respire (secours)"

    def render(self, t, dt, f):
        breath = 0.5 - 0.5 * math.cos(2 * math.pi * t / 6.0)
        v = 0.05 + 0.95 * breath
        return (v, v, v)


class Pulse:
    label = "pulse : blanc + rouge sur les basses"

    def __init__(self, hold=1.0):
        self.live = 0.0
        self.slow = 0.0
        self.glow = HoldRelease(0.12, hold, 1.4)
        self.hat = 0.0
        self.lvl = 0.0

    def render(self, t, dt, f):
        bass = max(f.bands[0], f.bands[1])
        target = clamp01(0.55 * bass ** 1.2 + 0.85 * f.kick)
        self.live = follow(self.live, target, dt, 0.045, 0.20)      # pulsation sur chaque kick
        self.slow = follow(self.slow, bass, dt, 0.30, 0.60)         # niveau moyen des basses
        glow = self.glow.update(0.5 * self.slow, t, dt)             # teinte rosee qui s'attarde, puis retour au blanc
        a = max(self.live, glow)
        self.hat = follow(self.hat, f.hat, dt, 0.03, 0.14)
        self.lvl = follow(self.lvl, f.level, dt, 0.25, 0.9)

        w = idle_white(t, self.lvl * 0.5)
        m = smoothstep(a)
        red = 0.28 + 0.72 * a
        r = lerp(w, red, m)
        g = lerp(w, 0.0, m)
        b = lerp(w, 0.0, m)
        sh = 0.30 * self.hat * (1.0 - 0.6 * m)              # scintillement des aigus
        return (r + sh, g + sh * (1 - m), b + sh * (1 - m))


class Hue:
    label = "hue : la couleur suit la hauteur du son"

    def __init__(self, hold=1.0):
        self.hue = 0.0
        self.pres = HoldRelease(0.15, hold, 1.6)
        self.lvl = 0.0
        self.kick = 0.0

    def render(self, t, dt, f):
        w8 = f.bands ** 2
        tot = float(w8.sum())
        if tot > 0.08:                                       # centre de gravite du spectre
            c = float((np.arange(NB) * w8).sum() / tot) / (NB - 1)
            self.hue = follow(self.hue, 0.75 * c, dt, 0.14, 0.14)   # 0=rouge ... 0.75=violet
        self.lvl = follow(self.lvl, f.level, dt, 0.06, 0.40)
        self.kick = follow(self.kick, f.kick, dt, 0.04, 0.25)
        pres = smoothstep(self.pres.update(clamp01(f.level * 1.6), t, dt))
        v_music = clamp01(0.30 + 0.70 * self.lvl + 0.30 * self.kick)
        v = lerp(idle_white(t), v_music, pres)
        r, g, b = colorsys.hsv_to_rgb(self.hue, pres, v)
        return (r, g, b)


class Mixer:
    label = "mixer : basses=rouge, mediums=vert, aigus=bleu"

    def __init__(self, hold=1.0):
        self.env = [0.0, 0.0, 0.0]
        self.chroma = [1.0, 1.0, 1.0]
        self.peak = 0.0
        self.pres = HoldRelease(0.10, hold, 1.6)
        self.flash = 0.0

    def render(self, t, dt, f):
        tg = (
            clamp01(0.9 * max(f.bands[0], f.bands[1]) ** 1.2 + 0.6 * f.kick),
            clamp01(0.9 * max(f.bands[2], f.bands[3]) ** 1.2 + 0.5 * f.snare),
            clamp01(0.9 * max(f.bands[4], f.bands[5]) ** 1.2 + 0.5 * f.hat),
        )
        taus = ((0.040, 0.32), (0.045, 0.25), (0.030, 0.16))
        for i in range(3):
            self.env[i] = follow(self.env[i], tg[i], dt, *taus[i])
        peak = max(self.env)
        self.peak = follow(self.peak, peak, dt, 0.05, 0.5)
        if peak > 0.08:                                      # on retient la teinte tant qu'il y a du son
            k = 1.0 - math.exp(-dt / 0.06)
            for i in range(3):
                self.chroma[i] += (self.env[i] / peak - self.chroma[i]) * k
        self.flash = follow(self.flash, f.kick, dt, 0.025, 0.22)

        pres = smoothstep(self.pres.update(max(tg), t, dt))
        bright = 0.30 + 0.70 * self.peak
        colored = [c * bright for c in self.chroma]
        w = idle_white(t)
        out = [lerp(w, colored[i], pres) for i in range(3)]
        fl = 0.30 * self.flash * pres
        return (out[0] + fl, out[1] + fl, out[2] + fl)


EFFECTS = {"pulse": Pulse, "hue": Hue, "mixer": Mixer}


# --------------------------------------------------------------------------
# Sortie : gamma + dithering temporel (fondus sans paliers visibles)
# --------------------------------------------------------------------------
class Encoder:
    def __init__(self, gamma=2.2, dither=True, master=1.0):
        self.gamma, self.dither, self.master = gamma, dither, master
        self.err = [0.0, 0.0, 0.0]

    def encode(self, rgb):
        out = []
        for i, c in enumerate(rgb):
            v = clamp01(c * self.master) ** self.gamma * 255.0
            if self.dither:
                v += self.err[i]
                q = int(round(v))
                q = 0 if q < 0 else 255 if q > 255 else q
                self.err[i] = max(-1.0, min(1.0, v - q))
            else:
                q = int(round(v))
            out.append(q)
        return tuple(out)


# --------------------------------------------------------------------------
# Moteur : choisit l'effet, gere les fondus entre modes (sans materiel)
# --------------------------------------------------------------------------
class Engine:
    XFADE = 0.7

    def __init__(self, names, hold):
        self.names = names
        self.hold = hold
        self.idx = 0
        self.effect = None
        self.last = (0.2, 0.2, 0.2)
        self.xf_from = None
        self.xf_t0 = 0.0

    def make_effect(self, name):
        if name == "white":
            return WhiteBreath()
        return EFFECTS[name](hold=self.hold)

    def set_mode(self, idx, t):
        self.idx = idx % len(self.names)
        name = self.names[self.idx]
        self.effect = None if name == "loop" else self.make_effect(name)
        self.xf_from = self.last
        self.xf_t0 = t

    def step(self, t, dt, f):
        rgb = self.effect.render(t, dt, f)
        if self.xf_from is not None:
            k = smoothstep((t - self.xf_t0) / self.XFADE)
            rgb = tuple(lerp(a, b, k) for a, b in zip(self.xf_from, rgb))
            if k >= 1.0:
                self.xf_from = None
        self.last = tuple(clamp01(c) for c in rgb)
        return self.last


# --------------------------------------------------------------------------
# Declencheurs de changement de mode (bouton souris, signal, clavier)
# --------------------------------------------------------------------------
def hid_nodes():
    nodes = []
    for p in sorted(glob.glob("/sys/class/hidraw/hidraw*")):
        try:
            with open(os.path.join(p, "device", "uevent")) as fh:
                if MOUSE_HID_ID in fh.read().upper():
                    nodes.append("/dev/" + os.path.basename(p))
        except OSError:
            pass
    return nodes


def evdev_nodes():
    try:
        import evdev
    except ImportError:
        return None, []
    devs = []
    for path in evdev.list_devices():
        try:
            d = evdev.InputDevice(path)
        except OSError:
            continue
        if d.info.vendor == MOUSE_VID and d.info.product == MOUSE_PID:
            devs.append(d)
    return evdev, devs


class TriggerListener(threading.Thread):
    """Ecoute des evenements de la souris. Specs : 'evdev:BTN_EXTRA' ou 'hid:<hex>'."""

    def __init__(self, specs, callback):
        super().__init__(daemon=True)
        self.callback = callback
        self.evdev_codes = set()
        self.hid_prefixes = []
        for s in specs:
            kind, _, val = s.partition(":")
            if kind == "evdev":
                self.evdev_codes.add(val.strip())
            elif kind == "hid":
                self.hid_prefixes.append(bytes.fromhex(val.replace(" ", "")))
            else:
                print(f"[declencheur] format inconnu : {s}")
        self.fds = {}
        self.ev, devs = evdev_nodes()
        if self.evdev_codes:
            if self.ev is None:
                print("[declencheur] python-evdev manquant : pip install evdev")
            elif not devs:
                print("[declencheur] aucune interface d'entree de la souris lisible "
                      "(groupe 'input' ? voir les instructions)")
            for d in devs:
                self.fds[d.fd] = ("evdev", d)
        if self.hid_prefixes:
            for node in hid_nodes():
                try:
                    fd = os.open(node, os.O_RDONLY | os.O_NONBLOCK)
                    self.fds[fd] = ("hid", node)
                except OSError as e:
                    print(f"[declencheur] impossible d'ouvrir {node} : {e}")

    def run(self):
        if not self.fds:
            return
        while True:
            ready, _, _ = select.select(list(self.fds), [], [], 1.0)
            for fd in ready:
                kind, obj = self.fds[fd]
                try:
                    if kind == "evdev":
                        for e in obj.read():
                            if e.type == self.ev.ecodes.EV_KEY and e.value == 1:
                                names = self.ev.ecodes.bytype[self.ev.ecodes.EV_KEY].get(e.code, "")
                                names = names if isinstance(names, list) else [names]
                                if self.evdev_codes & set(names):
                                    self.callback()
                    else:
                        data = os.read(fd, 64)
                        if any(data.startswith(p) for p in self.hid_prefixes):
                            self.callback()
                except (BlockingIOError, OSError):
                    pass


def run_probe(seconds):
    """Montre tout ce que la souris envoie au PC quand on appuie sur le bouton LED."""
    ev, devs = evdev_nodes()
    nodes = hid_nodes()
    fds = {}
    print("Sources trouvees pour la souris :")
    if ev is None:
        print("  (python-evdev absent : pip install evdev)")
    for d in devs:
        keys = d.capabilities().get(ev.ecodes.EV_KEY, [])
        names = [ev.ecodes.bytype[ev.ecodes.EV_KEY].get(k, k) for k in keys]
        names = [n[0] if isinstance(n, list) else n for n in names]
        print(f"  evdev {d.path} '{d.name}' boutons: {', '.join(map(str, names)) or '-'}")
        fds[d.fd] = ("evdev", d)
    for node in nodes:
        try:
            fd = os.open(node, os.O_RDONLY | os.O_NONBLOCK)
            fds[fd] = ("hid", node)
            print(f"  hidraw {node}")
        except OSError as e:
            print(f"  hidraw {node} : acces refuse ({e})")
    if not fds:
        print("Rien de lisible. Ajoute-toi au groupe 'input' (sudo usermod -aG input $USER, puis reconnexion).")
        return
    print(f"\nNE BOUGE PAS la souris. Appuie 3 fois sur le bouton des LEDs pendant {seconds} s...\n")
    seen = collections.Counter()
    end = time.time() + seconds
    while time.time() < end:
        ready, _, _ = select.select(list(fds), [], [], 0.2)
        for fd in ready:
            kind, obj = fds[fd]
            try:
                if kind == "evdev":
                    for e in obj.read():
                        if e.type == ev.ecodes.EV_SYN:
                            continue
                        tname = ev.ecodes.EV[e.type]
                        cname = ev.ecodes.bytype.get(e.type, {}).get(e.code, e.code)
                        cname = cname[0] if isinstance(cname, list) else cname
                        print(f"  [evdev {obj.path}] {tname} {cname} valeur={e.value}")
                        if e.type == ev.ecodes.EV_KEY and e.value == 1:
                            seen[f"--trigger evdev:{cname}"] += 1
                else:
                    data = os.read(fd, 64)
                    print(f"  [{obj}] {data.hex(' ')}")
                    seen[f"--trigger hid:{data.hex()}"] += 1
            except (BlockingIOError, OSError):
                pass
    print()
    if seen:
        print("Declencheurs possibles (celui qui apparait ~3 fois est le bon) :")
        for k, n in seen.most_common():
            print(f"  {k}   (vu {n} fois)")
    else:
        print("Aucun evenement : le bouton LED est gere uniquement par le firmware de la souris.")
        print("Solutions : relance --probe en appuyant sur un autre bouton (ex. un bouton lateral),")
        print("ou utilise un raccourci clavier GNOME qui lance :  pkill -USR1 -f mouse_rgb.py")


# --------------------------------------------------------------------------
# Application (materiel OpenRGB)
# --------------------------------------------------------------------------
def connect(address, port, tries=15):
    from openrgb import OpenRGBClient
    last = None
    for _ in range(tries):
        try:
            return OpenRGBClient(address=address, port=port, name="mouse_rgb")
        except Exception as e:  # serveur pas encore pret
            last = e
            time.sleep(1.0)
    raise SystemExit(f"Impossible de joindre le serveur OpenRGB ({last}).\n"
                     "Lance d'abord :  ./OpenRGB.AppImage --server --noautoconnect")


def find_mouse(client):
    from openrgb.utils import DeviceType
    mice = client.get_devices_by_type(DeviceType.MOUSE)
    if mice:
        return mice[0]
    for d in client.devices:
        if "mouse" in d.name.lower():
            return d
    raise SystemExit("Souris introuvable dans OpenRGB (regles udev ? serveur lance avec les bons droits ?).")


def describe_modes(dev):
    from openrgb.utils import ModeFlags
    print(f"Appareil : {dev.name}  ({len(dev.leds)} LED, zones : {[z.name for z in dev.zones]})")
    for m in dev.modes:
        fl = [n for n in ("HAS_SPEED", "HAS_BRIGHTNESS", "HAS_MODE_SPECIFIC_COLOR", "HAS_PER_LED_COLOR",
                          "HAS_RANDOM_COLOR", "HAS_DIRECTION_LR", "HAS_DIRECTION_UD", "HAS_DIRECTION_HV")
              if getattr(ModeFlags, n) in m.flags]
        print(f"  - {m.name:<16} couleurs={m.color_mode.name:<14} vitesse={m.speed_min}..{m.speed_max} "
              f"luminosite={m.brightness_min}..{m.brightness_max} couleurs_min/max={m.colors_min}/{m.colors_max}")
        print(f"      flags: {', '.join(fl) or '-'}")


class App:
    def __init__(self, dev, args):
        from openrgb.utils import RGBColor, ModeFlags, ModeColors
        self.RGBColor, self.ModeFlags, self.ModeColors = RGBColor, ModeFlags, ModeColors
        self.dev, self.args = dev, args
        self.direct = self._mode("direct")
        self.loop = self._mode("loop")
        names = [n.strip() for n in args.modes.split(",") if n.strip()]
        self.loop_white = self.loop is not None and self._loop_can_be_white()
        if "loop" in names and not self.loop_white:
            why = "n'existe pas" if self.loop is None else "ne propose pas de couleur reglable (elle sera arc-en-ciel)"
            print(f"[attention] le mode Loop du firmware {why} : "
                  "remplace par un blanc qui respire. Verifie dans l'interface OpenRGB.")
            names = ["white" if n == "loop" else n for n in names]
        self.names = names
        self.engine = Engine(names, args.hold)
        self.enc = Encoder(dither=not args.no_dither, master=args.brightness)
        self.requests = collections.deque()
        self.reassert = []
        self.last_trigger = 0.0
        self.last_sent = None
        self.running = True

    def _mode(self, name):
        for m in self.dev.modes:
            if m.name.lower() == name:
                return m
        return None

    def _loop_can_be_white(self):
        m = self.loop
        return self.ModeFlags.HAS_MODE_SPECIFIC_COLOR in m.flags and (m.colors_max or 0) >= 1

    def request_next(self):
        now = time.time()
        if now - self.last_trigger > 0.3:
            self.last_trigger = now
            self.requests.append(("next", None))

    def request_set(self, i):
        self.requests.append(("set", i))

    def _apply_hw_mode(self, name):
        if name == "loop":
            m = self.loop
            if self.ModeFlags.HAS_SPEED in m.flags:
                m.speed = int(round(lerp(m.speed_min, m.speed_max, self.args.loop_speed)))
            if self.ModeFlags.HAS_BRIGHTNESS in m.flags and m.brightness_max is not None:
                m.brightness = m.brightness_max
            n = max(m.colors_min or 1, 1)
            m.colors = [self.RGBColor(255, 255, 255)] * min(n, m.colors_max)
            self.dev.set_mode(m)
        else:
            self.dev.set_mode(self.direct)

    def activate(self, idx, t):
        idx %= len(self.names)
        name = self.names[idx]
        self._apply_hw_mode(name)
        self.engine.set_mode(idx, t)
        self.last_sent = None
        print(f"Mode {idx + 1}/{len(self.names)} : {self.mode_label(name)}")
        # la souris change aussi de mode toute seule quand on appuie sur son bouton :
        # on reaffirme notre mode juste apres pour reprendre la main
        self.reassert = [time.time() + 0.25, time.time() + 0.8]

    def mode_label(self, name):
        if name == "loop":
            return "loop : lumiere blanche qui tourne (firmware)"
        return self.engine.make_effect(name).label

    def run(self, audio):
        t0 = time.perf_counter()
        self.activate(self.args.start - 1, 0.0)
        frame = 1.0 / self.args.fps
        nxt = time.perf_counter()
        prev = t0
        try:
            while self.running:
                now = time.perf_counter()
                t, dt = now - t0, max(now - prev, 1e-3)
                prev = now

                while self.requests:
                    kind, val = self.requests.popleft()
                    i = self.engine.idx + 1 if kind == "next" else val
                    self.activate(i, t)
                if self.reassert and time.time() >= self.reassert[0]:
                    self.reassert.pop(0)
                    self._apply_hw_mode(self.names[self.engine.idx])

                if self.engine.effect is None:           # mode firmware : rien a envoyer
                    time.sleep(0.05)
                    nxt = time.perf_counter()
                    continue

                feats = audio.snapshot() if audio else Features()
                rgb = self.engine.step(t, dt, feats)
                q = self.enc.encode(rgb)
                if q != self.last_sent:
                    self.dev.set_color(self.RGBColor(*q), fast=True)
                    self.last_sent = q

                nxt += frame
                delay = nxt - time.perf_counter()
                if delay > 0:
                    time.sleep(delay)
                else:
                    nxt = time.perf_counter()
        except KeyboardInterrupt:
            pass


def stdin_listener(app):
    def loop():
        for line in sys.stdin:
            s = line.strip().lower()
            if s == "q":
                app.running = False
                return
            if s.isdigit():
                app.request_set(int(s) - 1)
            else:
                app.request_next()
    threading.Thread(target=loop, daemon=True).start()


def main():
    ap = argparse.ArgumentParser(description="RGB audio-reactif pour la souris Acer OMW950")
    ap.add_argument("--modes", default="loop,pulse,hue,mixer", help="ordre des modes (loop,pulse,hue,mixer)")
    ap.add_argument("--start", type=int, default=1, help="numero du mode de depart")
    ap.add_argument("--trigger", action="append", default=[],
                    help="bouton qui change de mode : evdev:BTN_EXTRA ou hid:<hex> (plusieurs possibles)")
    ap.add_argument("--probe", nargs="?", const=15, type=int, metavar="SECONDES",
                    help="affiche ce que la souris envoie quand on appuie sur son bouton LED")
    ap.add_argument("--info", action="store_true", help="affiche les modes que la souris expose, puis quitte")
    ap.add_argument("--sens", type=float, default=1.0, help="sensibilite audio (0.5 - 2)")
    ap.add_argument("--hold", type=float, default=1.0, help="delai (s) avant le retour au blanc quand le son s'arrete")
    ap.add_argument("--smooth", type=float, default=1.0,
                    help="douceur des transitions : 1 = defaut, 1.5 = plus doux, 0.7 = plus nerveux")
    ap.add_argument("--loop-speed", type=float, default=0.0,
                    help="vitesse du mode Loop de 0 (le plus lent) a 1 ; inverse si c'est trop rapide")
    ap.add_argument("--brightness", type=float, default=1.0, help="luminosite globale 0..1")
    ap.add_argument("--no-dither", action="store_true", help="coupe le dithering (si ca scintille)")
    ap.add_argument("--fps", type=int, default=60)
    ap.add_argument("--source", default="@DEFAULT_MONITOR@", help="source audio PulseAudio/PipeWire")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=6742)
    args = ap.parse_args()

    SMOOTH[0] = max(args.smooth, 0.2)
    if args.probe is not None:
        run_probe(args.probe)
        return

    client = connect(args.host, args.port)
    dev = find_mouse(client)
    if args.info:
        describe_modes(dev)
        client.disconnect()
        return

    app = App(dev, args)
    needs_audio = any(n in EFFECTS for n in app.names)
    audio = None
    if needs_audio:
        audio = AudioAnalyzer(args.sens)
        audio.start(args.source)

    if args.trigger:
        TriggerListener(args.trigger, app.request_next).start()
    signal.signal(signal.SIGUSR1, lambda *_: app.request_next())
    if sys.stdin.isatty():
        stdin_listener(app)
        print("Entree = mode suivant, un chiffre = aller a ce mode, q = quitter, Ctrl+C aussi.")

    print(f"Souris : {dev.name}")
    app.run(audio)

    if audio:
        audio.stop()
    try:
        dev.set_mode(app.direct or 0)
        dev.set_color(app.RGBColor(255, 255, 255))
    finally:
        client.disconnect()


if __name__ == "__main__":
    main()