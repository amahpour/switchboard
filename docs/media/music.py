"""The video's music: an original, upbeat synth loop, generated here from scratch.

No samples and no third-party audio: every sound is synthesized below (sine, saw and
pulse oscillators, filtered noise), and the melody and chords are written out in this
file. It's covered by the repository's MIT license (see CREDITS.md).

    uv run --with numpy python docs/media/music.py --seconds 36 --out music.wav

Deterministic: the same arguments give the same file.
"""

from __future__ import annotations

import argparse
import wave

import numpy as np

SR = 44100
BPM = 120
BEAT = 60 / BPM            # 0.5 s
BAR = 4 * BEAT             # 2 s
STEP = BEAT / 4            # a 16th note

# I - V - vi - IV in C major, one chord per bar (root MIDI note, chord tones)
CHORDS = [(48, (60, 64, 67)), (43, (59, 62, 67)), (45, (57, 60, 64)), (41, (57, 60, 65))]
# the hook: (16th-note step within the 4-bar phrase, MIDI note, length in steps)
HOOK = [
    (0, 76, 2), (3, 79, 1), (4, 76, 2), (6, 81, 2), (10, 79, 4),
    (16, 74, 2), (19, 79, 1), (20, 74, 2), (22, 71, 2), (26, 74, 4),
    (32, 72, 2), (35, 76, 1), (36, 72, 2), (38, 81, 2), (42, 79, 2), (44, 76, 2),
    (48, 77, 2), (51, 81, 1), (52, 77, 2), (54, 79, 6),
]
# the answer phrase the second time round: same rhythm, it resolves up to the tonic
HOOK_B = HOOK[:16] + [(48, 77, 2), (51, 81, 1), (52, 79, 2), (54, 84, 6)]


def hz(m: float) -> float:
    return 440.0 * 2 ** ((m - 69) / 12)


def env(n: int, a: float, d: float, s: float = 0.0, r: float = 0.02) -> np.ndarray:
    """Attack/decay to sustain level, then a release at the end, over n samples."""
    t = np.arange(n) / SR
    e = np.where(t < a, t / max(a, 1e-6), s + (1 - s) * np.exp(-(t - a) / max(d, 1e-6)))
    rel = int(r * SR)
    if rel and n > rel:
        e[-rel:] *= np.linspace(1, 0, rel)
    return e


def saw(f: float, n: int, phase: float = 0.0) -> np.ndarray:
    t = np.arange(n) / SR
    return 2 * ((t * f + phase) % 1.0) - 1


def pulse(f: float, n: int, width: float = 0.25) -> np.ndarray:
    t = np.arange(n) / SR
    return np.where((t * f) % 1.0 < width, 1.0, -1.0)


def lowpass(x: np.ndarray, cutoff: float) -> np.ndarray:
    """A gentle low-pass (a 4th-order roll-off), applied in the frequency domain."""
    spec = np.fft.rfft(x)
    f = np.fft.rfftfreq(len(x), 1 / SR)
    spec *= 1 / (1 + (f / cutoff) ** 4)
    return np.fft.irfft(spec, len(x))


def add(buf: np.ndarray, start: float, sig: np.ndarray) -> None:
    i = int(start * SR)
    j = min(len(buf), i + len(sig))
    if i < len(buf):
        buf[i:j] += sig[: j - i]


def render(seconds: float) -> np.ndarray:
    n = int(seconds * SR) + SR
    rng = np.random.default_rng(7)
    drums, bass, pads, lead, arp = (np.zeros(n) for _ in range(5))
    duck = np.ones(n)
    bars = int(np.ceil(seconds / BAR))
    for b in range(bars):
        t0 = b * BAR
        root, tones = CHORDS[b % 4]
        intro = b < 2
        # drums: kick on 1 and 3 (and the "and" of 4), clap on 2 and 4, hats on 8ths
        if not intro:
            for beat in (0, 2, 3.5):
                k = int(0.35 * SR)
                t = np.arange(k) / SR
                f = 45 + 75 * np.exp(-t / 0.03)
                kick = np.sin(2 * np.pi * np.cumsum(f) / SR) * np.exp(-t / 0.12)
                add(drums, t0 + beat * BEAT, 0.9 * kick)
                d0 = int((t0 + beat * BEAT) * SR)
                dl = int(0.22 * SR)
                if d0 < n:
                    seg = min(dl, n - d0)
                    duck[d0:d0 + seg] = np.minimum(duck[d0:d0 + seg], 1 - 0.55 * np.exp(-np.arange(seg) / SR / 0.08))
            for beat in (1, 3):
                k = int(0.2 * SR)
                noise = rng.standard_normal(k)
                clap = (noise - np.concatenate(([0], noise[:-1]))) * np.exp(-np.arange(k) / SR / 0.06)
                add(drums, t0 + beat * BEAT, 0.28 * clap)
        for e8 in range(8):
            k = int(0.05 * SR)
            noise = rng.standard_normal(k)
            hat = (noise - np.concatenate(([0], noise[:-1]))) * np.exp(-np.arange(k) / SR / 0.012)
            add(drums, t0 + e8 * BEAT / 2, (0.10 if e8 % 2 else 0.06) * hat)
        # bass: 8th-note octave bounce on the chord root
        if not intro:
            for e8 in range(8):
                m = root + (12 if e8 % 2 else 0)
                k = int(BEAT / 2 * SR * 0.9)
                s = 0.6 * saw(hz(m), k) + 0.4 * pulse(hz(m), k, 0.5)
                add(bass, t0 + e8 * BEAT / 2, 0.32 * s * env(k, 0.005, 0.18, 0.35, 0.02))
        # pads: three detuned saws per chord tone, a slow swell
        k = int(BAR * SR)
        chord = sum(saw(hz(m) * d, k, ph) for m in tones for d, ph in ((0.997, 0.1), (1.0, 0.5), (1.004, 0.8)))
        add(pads, t0, 0.05 * chord * env(k, 0.25, 1.2, 0.6, 0.15))
        # arpeggio: 16ths over the chord, up an octave
        for s16 in range(16):
            m = tones[s16 % 3] + 12
            k = int(STEP * SR * 0.8)
            add(arp, t0 + s16 * STEP, 0.07 * pulse(hz(m), k, 0.5) * env(k, 0.002, 0.05, 0.0, 0.01))
    # the hook, over the full sections (from bar 2), alternating the two endings
    for phrase_start in range(2, bars - 1, 4):
        melody = HOOK if ((phrase_start - 2) // 4) % 2 == 0 else HOOK_B
        for step, m, length in melody:
            t = phrase_start * BAR + step * STEP
            if t >= seconds - 1.0:
                continue
            k = int(length * STEP * SR)
            f = hz(m)
            vib = 1 + 0.004 * np.sin(2 * np.pi * 5.5 * np.arange(k) / SR) * np.clip(np.arange(k) / SR / 0.2, 0, 1)
            s = pulse(f, k, 0.25) * 0.6 + saw(f * 1.002, k) * 0.4
            s = s * vib
            add(lead, t, 0.16 * s * env(k, 0.006, 0.25, 0.55, 0.03))
    pads = lowpass(pads, 1800) * duck
    bass = lowpass(bass, 900) * (0.6 + 0.4 * duck)
    lead = lowpass(lead, 5200)
    arp = lowpass(arp, 4000) * duck
    left = drums + bass + pads * 1.1 + lead * 0.85 + arp * 1.25
    right = drums + bass + pads * 0.9 + lead * 1.05 + arp * 0.75
    mix = np.stack([left, right], axis=1)[: int(seconds * SR)]
    mix = np.tanh(1.4 * mix) / np.tanh(1.4)
    fade_in, fade_out = int(0.3 * SR), int(2.0 * SR)
    mix[:fade_in] *= np.linspace(0, 1, fade_in)[:, None]
    mix[-fade_out:] *= np.linspace(1, 0, fade_out)[:, None] ** 1.5
    return 0.6 * mix / np.max(np.abs(mix))   # background level, with headroom for the AAC encoder


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--seconds", type=float, default=36.0)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    pcm = (render(args.seconds) * 32767).astype("<i2")
    with wave.open(args.out, "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm.tobytes())
    print(f"wrote {args.out}: {args.seconds:.1f} s")


if __name__ == "__main__":
    main()
