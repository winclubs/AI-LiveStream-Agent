"""Generate the golden reference for the mel filterbank + preemphasis tests.

This is a direct, independent transcription of librosa's Slaney mel filterbank
(librosa.filters.mel(htk=False, norm='slaney') used by Wav2Lip audio.py::_build_mel_basis).
It deliberately shares no code with Wav2LipMelExtractor so that comparing against it
is a real cross-check rather than a self-referential assertion.

Run:  python scripts/gen_mel_golden.py > server/tests/data/mel_golden.json
"""
import json
import sys
from pathlib import Path

import numpy as np

SR, N_FFT, N_MELS, FMIN, FMAX = 16000, 800, 80, 55.0, 7600.0


def hz_to_mel_slaney(f):
    """librosa.hz_to_mel, slaney scale (htk=False)."""
    f = np.asarray(f, dtype=np.float64)
    mels = f / (200.0 / 3.0)
    min_log_hz, min_log_mel = 1000.0, (1000.0 - 0.0) / (200.0 / 3.0)
    logstep = np.log(6.4) / 27.0
    return np.where(
        f >= min_log_hz,
        min_log_mel + np.log(np.maximum(f, 1e-10) / min_log_hz) / logstep,
        mels,
    )


def mel_to_hz_slaney(m):
    m = np.asarray(m, dtype=np.float64)
    freqs = m * (200.0 / 3.0)
    min_log_hz, min_log_mel = 1000.0, (1000.0 - 0.0) / (200.0 / 3.0)
    logstep = np.log(6.4) / 27.0
    return np.where(
        m >= min_log_mel,
        min_log_hz * np.exp(logstep * (m - min_log_mel)),
        freqs,
    )


def librosa_mel_filterbank():
    """Transcription of librosa.filters.mel(sr, n_fft, n_mels, fmin, fmax)
    with default norm='slaney', htk=False."""
    fftfreqs = np.fft.rfftfreq(N_FFT, 1.0 / SR)
    mel_f = mel_to_hz_slaney(np.linspace(hz_to_mel_slaney(FMIN), hz_to_mel_slaney(FMAX), N_MELS + 2))
    fdiff = np.diff(mel_f)
    ramps = np.subtract.outer(mel_f, fftfreqs)
    weights = np.zeros((N_MELS, len(fftfreqs)))
    for i in range(N_MELS):
        lower = -ramps[i] / fdiff[i]
        upper = ramps[i + 2] / fdiff[i + 1]
        weights[i] = np.maximum(0.0, np.minimum(lower, upper))
    # norm='slaney'
    weights *= (2.0 / (mel_f[2:N_MELS + 2] - mel_f[:N_MELS]))[:, None]
    return weights


def official_mel(pcm):
    """Transcription of Rudrabha/Wav2Lip audio.py::melspectrogram with hparams.py."""
    wav = np.asarray(pcm, dtype=np.float64)
    # preemphasis: signal.lfilter([1,-0.97],[1], wav)   (preemphasize=True)
    wav = np.append(wav[0], wav[1:] - 0.97 * wav[:-1])
    hop = 200
    pad = np.pad(wav, (N_FFT // 2, N_FFT // 2), mode="reflect")
    nf = 1 + (len(pad) - N_FFT) // hop
    idx = np.arange(N_FFT)[None, :] + hop * np.arange(nf)[:, None]
    D = np.abs(np.fft.rfft(pad[idx] * np.hanning(N_FFT), axis=1)).T
    mel = np.dot(librosa_mel_filterbank(), D)
    S = 20.0 * np.log10(np.maximum(1e-5, mel)) - 20.0          # _amp_to_db - ref_level_db
    S = np.clip(2 * 4.0 * ((S + 100.0) / 100.0) - 4.0, -4.0, 4.0)  # _normalize
    if S.shape[1] > 16:
        s = (S.shape[1] - 16) // 2
        S = S[:, s:s + 16]
    elif S.shape[1] < 16:
        pl = (16 - S.shape[1]) // 2
        S = np.pad(S, ((0, 0), (pl, 16 - S.shape[1] - pl)), mode="edge")
    return S


def vowel(formants, n=6400, f0=120.0):
    t = np.arange(n) / SR
    src = (np.sin(2 * np.pi * f0 * t) > 0).astype(np.float64) * 0.6
    y = src
    for fc, bw in zip(formants, (80.0, 100.0, 140.0, 160.0)):
        r = np.exp(-np.pi * bw / SR)
        th = 2 * np.pi * fc / SR
        y = np.convolve(y, [1.0, -2 * r * np.cos(th), r * r], mode="full")[:n]
    return y / np.max(np.abs(y))


def main():
    rng = np.random.default_rng(20261002)
    a = vowel((800.0, 1200.0))
    i = vowel((280.0, 2300.0, 3000.0))
    u = vowel((300.0, 870.0))
    cases = {
        "silence": np.zeros(6400),
        "near_silence": rng.normal(0, 1e-7, 6400),
        "vowel_a": a,
        "vowel_i": i,
        "vowel_u": u,
        "quiet_a": a * 0.02,
        "loud_a": a * 0.95,
        "noise": rng.normal(0, 0.2, 6400),
    }
    payload = {
        "params": {
            "sample_rate": SR, "n_fft": N_FFT, "hop_length": 200,
            "n_mels": N_MELS, "fmin": FMIN, "fmax": FMAX,
            "min_level_db": -100.0, "ref_level_db": 20.0,
            "max_abs_value": 4.0, "preemphasis": 0.97,
        },
        "mel_basis": np.round(librosa_mel_filterbank(), 8).tolist(),
        "mel_windows": {
            k: np.round(official_mel(v), 6).tolist() for k, v in cases.items()
        },
    }
    out = Path(__file__).resolve().parent.parent / "server" / "tests" / "data" / "mel_golden.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload), encoding="utf-8")
    print(f"wrote {out} ({out.stat().st_size} bytes)", file=sys.stderr)


if __name__ == "__main__":
    main()
