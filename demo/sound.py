"""Soundtrack for the explainer: a soft music bed plus effects synthesized from scratch (numpy), mixed onto the video.

Every effect is placed from the explainer's cue sheet, so sounds land on the exact frame of their animation.
No samples, no downloads: everything here is generated.
"""
import subprocess
import tempfile
import wave
from pathlib import Path

import numpy as np

SR = 44100
RNG = np.random.default_rng(7)


def _t(dur):
    return np.arange(int(SR * dur)) / SR


def _env(n, attack=0.005, decay=None, release=0.05):
    e = np.ones(n)
    a = max(1, int(SR * attack))
    e[:a] = np.linspace(0, 1, a)
    if decay:
        e *= np.exp(-np.arange(n) / (SR * decay))
    r = min(n, max(1, int(SR * release)))
    e[-r:] *= np.linspace(1, 0, r)
    return e


def _lowpass(x, k):
    k = max(1, int(k))
    return np.convolve(x, np.ones(k) / k, mode="same")


def sine(f, dur, amp=0.3, decay=None):
    t = _t(dur)
    return amp * np.sin(2 * np.pi * f * t) * _env(len(t), decay=decay)


def key():        # keyboard tap: short high-passed noise + a tiny click
    n = int(SR * 0.035)
    x = RNG.normal(0, 1, n)
    x = (x - _lowpass(x, 6)) * _env(n, 0.001, 0.008)
    c = np.zeros(n)
    tone = sine(1900 + RNG.uniform(-200, 200), 0.02, 0.05, 0.004)
    c[: len(tone)] = tone
    return 0.22 * x + c


def click():      # UI click
    a = sine(1250, 0.05, 1, 0.01)
    b = sine(2500, 0.05, 1, 0.006)
    return 0.3 * (a + 0.5 * b)


def tick():
    return sine(2200, 0.03, 0.18, 0.006)


def pop(i=0):     # soft pop, rising with the list
    f = 620 + 55 * i
    t = _t(0.12)
    sweep = np.sin(2 * np.pi * (f * t + 900 * t * t)) * _env(len(t), 0.002, 0.03)
    return 0.28 * sweep


def toggle(i=0):
    return 0.6 * np.concatenate([sine(900 + 60 * i, 0.04, 0.4, 0.01), sine(1350 + 60 * i, 0.07, 0.4, 0.02)])


def chime(base=880):
    out = sum(sine(base * m, 1.2, a, 0.35) for m, a in ((1, 0.22), (2, 0.08), (3, 0.04), (4.2, 0.02)))
    return out


def done(i=0):    # two-note "task done"
    notes = [523.25, 659.25, 783.99, 880.0, 987.77]
    f = notes[i % len(notes)]
    a = sine(f, 0.18, 0.22, 0.08)
    b = sine(f * 1.5, 0.35, 0.2, 0.15)
    return np.concatenate([a[: int(SR * 0.08)], b])


def buzz():       # "needs you": low, gentle, two pulses
    t = _t(0.16)
    pulse = (np.sign(np.sin(2 * np.pi * 150 * t)) * 0.5 + np.sin(2 * np.pi * 150 * t)) * _env(len(t), 0.005, 0.08)
    p = _lowpass(pulse, 12) * 0.18
    return np.concatenate([p, np.zeros(int(SR * 0.05)), p])


def whoosh(dur=0.45):
    n = int(SR * dur)
    x = RNG.normal(0, 1, n)
    env = np.sin(np.linspace(0, np.pi, n)) ** 2
    # a moving low-pass: the window shrinks over time so the noise "opens up"
    out = np.zeros(n)
    for j, k in enumerate(np.linspace(40, 6, 8)):
        a, b = j * n // 8, (j + 1) * n // 8
        out[a:b] = _lowpass(x, k)[a:b]
    return 0.5 * out * env


def swoosh(_=0):
    return 0.7 * whoosh(0.3)


def rise(dur=2.0):  # rising sweep for filling bars
    t = _t(dur)
    f = 300 + 500 * (t / dur) ** 1.5
    phase = 2 * np.pi * np.cumsum(f) / SR
    return 0.08 * np.sin(phase) * np.linspace(0.3, 1, len(t)) * _env(len(t), 0.05, release=0.2)


def success():
    out = np.zeros(int(SR * 1.6))
    for k, f in enumerate([523.25, 659.25, 783.99, 1046.5]):
        s = sine(f, 1.4 - k * 0.08, 0.16, 0.5)
        o = int(SR * 0.09 * k)
        out[o:o + len(s)] += s
    return out


def final():
    out = np.zeros(int(SR * 2.6))
    for f in (261.63, 329.63, 392.0, 523.25):
        s = sine(f, 2.5, 0.12, 0.9)
        out[: len(s)] += s
    return out


def pad(duration):
    """A quiet, warm chord bed (Cmaj7 - Am7 - Fmaj7 - G6) with a soft pulse, for the whole video."""
    n = int(SR * duration)
    t = np.arange(n) / SR
    chords = [[261.63, 329.63, 392.0, 493.88], [220.0, 261.63, 329.63, 392.0], [174.61, 220.0, 261.63, 329.63],
              [196.0, 246.94, 293.66, 329.63]]
    bar = 4.0
    out = np.zeros(n)
    for ci in range(int(duration / bar) + 1):
        a, b = int(ci * bar * SR), min(n, int((ci + 1) * bar * SR + SR * 0.5))
        if a >= n:
            break
        seg = t[a:b] - t[a]
        env = np.minimum(1, seg / 0.8) * np.minimum(1, (seg[-1] - seg + 1e-9) / 0.6)
        for f in chords[ci % 4]:
            for det in (-0.6, 0.6):
                out[a:b] += 0.022 * np.sin(2 * np.pi * (f + det) * seg) * env
        # soft bass + pulse on the beat
        root = chords[ci % 4][0] / 2
        out[a:b] += 0.03 * np.sin(2 * np.pi * root * seg) * env
        for beat in range(4):
            o = a + int(beat * SR)
            k = sine(root * 2, 0.25, 0.018, 0.08)
            out[o:o + len(k)] += k[: max(0, min(len(k), n - o))]
    fade = np.minimum(1, t / 1.5) * np.minimum(1, (duration - t) / 2.5)
    return out * fade


FX = {"key": lambda a: key(), "click": lambda a: click(), "tick": lambda a: tick(), "pop": pop, "toggle": toggle,
      "chime": lambda a: chime(), "done": done, "buzz": lambda a: buzz(), "whoosh": lambda a: whoosh(), "swoosh": swoosh,
      "rise": lambda a: rise(a or 2.0), "success": lambda a: success(), "final": lambda a: final()}


def render(cue_list, duration):
    n = int(SR * (duration + 0.5))
    mix = np.zeros(n)
    for when, kind, arg in cue_list:
        if kind == "pad_start":
            continue
        s = FX[kind](arg)
        o = int(SR * when)
        if o >= n:
            continue
        mix[o:o + len(s)] += s[: n - o]
    mix[: int(SR * duration)] += pad(duration)
    mix = mix / max(1.0, np.abs(mix).max() / 0.89)        # leave headroom, never clip
    stereo = np.stack([mix, np.roll(mix, 30)], axis=1)     # a hair of width
    return (stereo * 32767).astype(np.int16)


def mux(video: Path, cue_list, duration):
    audio = render(cue_list, duration)
    with tempfile.TemporaryDirectory() as d:
        wav, tmp = Path(d) / "a.wav", Path(d) / "v.mp4"
        with wave.open(str(wav), "wb") as w:
            w.setnchannels(2)
            w.setsampwidth(2)
            w.setframerate(SR)
            w.writeframes(audio.tobytes())
        subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-i", str(video), "-i", str(wav), "-c:v", "copy", "-af", "loudnorm=I=-16:TP=-1.5:LRA=11",
                        "-c:a", "aac", "-b:a", "160k", "-shortest", "-movflags", "+faststart", str(tmp)], check=True)
        tmp.replace(video)
