"""Audio plumbing: whatever the capture device gives us, in and out.

Everything downstream (VAD, ASR, embeddings) is fixed at **16 kHz mono float32**,
because that is what FunASR and the CAM++ speaker model expect. Capture devices are
not: they commonly run at 44.1 kHz or 48 kHz, in stereo. So conversion is not
optional, it happens on the critical path of every chunk.

Two conversion strategies exist here and the difference matters:

* ``audioop`` (stdlib) -- fast, but deliberately *not* used for downsampling. It
  drops samples instead of low-pass filtering, so 48k -> 16k folds everything
  above 8 kHz back into the speech band as aliasing. Speech still transcribes,
  which is exactly what makes this failure mode dangerous: it sounds fine and
  quietly costs accuracy.
* ``scipy.signal.resample_poly`` -- polyphase FIR, properly anti-aliased. 48 -> 16
  reduces to a clean decimate-by-3. This is the default.

The numpy linear-interpolation path exists only as a last resort when scipy is
absent, and is marked as such so a caller can tell which one ran.
"""

from __future__ import annotations

import wave
from pathlib import Path

import numpy as np

TARGET_SR = 16000
SAMPLE_WIDTH = 2  # 16-bit PCM, on the wire and on disk

try:  # pragma: no cover - exercised by whichever branch the env has
    from scipy.signal import resample_poly

    HAVE_SCIPY = True
except Exception:  # noqa: BLE001
    HAVE_SCIPY = False


# ── reading ─────────────────────────────────────────────────────────────

def read_wav(path: str | Path, target_sr: int = TARGET_SR) -> tuple[np.ndarray, int]:
    """Read any PCM WAV as float32 mono at ``target_sr``.

    Multi-channel input is mixed down by averaging rather than by taking the
    first channel. On a phone lying flat on a meeting table the two capsules
    really do hear different things, and discarding one throws away speech.
    """
    with wave.open(str(path), "rb") as w:
        sr = w.getframerate()
        ch = w.getnchannels()
        sw = w.getsampwidth()
        raw = w.readframes(w.getnframes())

    audio = pcm_bytes_to_float(raw, sw)
    if ch > 1:
        usable = (len(audio) // ch) * ch
        audio = audio[:usable].reshape(-1, ch).mean(axis=1)
    if sr != target_sr:
        audio = resample(audio, sr, target_sr)
    return np.ascontiguousarray(audio, dtype=np.float32), target_sr


def pcm_bytes_to_float(raw: bytes, sampwidth: int = 2) -> np.ndarray:
    """Raw little-endian PCM bytes -> float32 in [-1, 1)."""
    if sampwidth == 2:
        return np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    if sampwidth == 1:
        # 8-bit WAV is *unsigned* by spec; centre it.
        return (np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
    if sampwidth == 4:
        return np.frombuffer(raw, dtype="<i4").astype(np.float32) / 2147483648.0
    raise ValueError(f"unsupported sample width: {sampwidth}")


def float_to_pcm_bytes(audio: np.ndarray) -> bytes:
    """float32 -> 16-bit little-endian PCM bytes.

    Clipped rather than scaled: a loud passage should flatten at full scale, not
    quietly drag the whole recording down by a normalisation factor.
    """
    clipped = np.clip(audio, -1.0, 1.0)
    return (clipped * 32767.0).astype("<i2").tobytes()


# ── resampling ──────────────────────────────────────────────────────────

def resample(audio: np.ndarray, sr_in: int, sr_out: int = TARGET_SR) -> np.ndarray:
    """Anti-aliased resample of a float32 mono signal."""
    if sr_in == sr_out or audio.size == 0:
        return audio.astype(np.float32, copy=False)

    if HAVE_SCIPY:
        from math import gcd

        g = gcd(int(sr_in), int(sr_out))
        up, down = int(sr_out) // g, int(sr_in) // g
        return resample_poly(audio.astype(np.float64), up, down).astype(np.float32)

    # Fallback: linear interpolation. Kept only so the module still functions in a
    # bare environment; quality is worse than the polyphase path above.
    n_out = int(round(len(audio) * float(sr_out) / float(sr_in)))
    x_in = np.linspace(0.0, 1.0, num=len(audio), endpoint=False)
    x_out = np.linspace(0.0, 1.0, num=n_out, endpoint=False)
    return np.interp(x_out, x_in, audio).astype(np.float32)


def resample_used() -> str:
    """Which resampler is active -- surfaced in health output, not guessed at."""
    return "scipy.resample_poly" if HAVE_SCIPY else "numpy.interp (fallback)"


# ── writing ─────────────────────────────────────────────────────────────

def write_wav(path: str | Path, audio: np.ndarray, sr: int = TARGET_SR) -> Path:
    """Write float32 mono audio as a 16-bit PCM WAV."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(p), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(SAMPLE_WIDTH)
        w.setframerate(sr)
        w.writeframes(float_to_pcm_bytes(audio))
    return p


# ── small helpers ───────────────────────────────────────────────────────

def rms_dbfs(audio: np.ndarray) -> float:
    """Level in dBFS; -inf-safe so it can be logged on a silent chunk."""
    if audio.size == 0:
        return float("-inf")
    r = float(np.sqrt(np.mean(np.square(audio.astype(np.float64)))))
    return 20.0 * np.log10(r) if r > 0 else float("-inf")


def frames(n_samples: int, sr: int = TARGET_SR) -> float:
    return n_samples / float(sr)


def samples(seconds: float, sr: int = TARGET_SR) -> int:
    return int(round(seconds * sr))
