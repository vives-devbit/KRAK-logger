"""
Inference pipeline — 10s model, 3s model, and 50/50 ensemble.

Accepts raw audio at 65536 Hz (as numpy array or parquet file path).
Handles resampling, trimming, and normalization internally.

Usage (CLI):
    python predict.py --input /path/to/file.parquet
    python predict.py --input /path/to/file.parquet --channel "AI0 (V)"

Usage (as module):
    from predict import load_models, predict

    models = load_models()
    result = predict(models, signal_65k_hz)  # numpy array at 65536 Hz
    print(result)
"""

import argparse
import json
import os

import numpy as np
import scipy.signal
import torch
import torch.nn.functional as F
from deepaudiox import AudioClassifier

torch.set_num_threads(20)
torch.set_num_interop_threads(5)

# ── Config ───────────────────────────────────────────────────────────────────
SRC_SR      = 65536
TGT_SR      = 32_000
SAMPLES_10S = 10 * TGT_SR   # 320 000
SAMPLES_3S  =  3 * TGT_SR   #  96 000
CLASS_NAMES = {0: "type_1", 1: "type_2", 2: "type_3", 3: "type_4", 4: "type_5"}

MODEL_DIR = os.path.dirname(__file__)
CHECKPOINT_10S = os.path.join(MODEL_DIR, "best_model_passt_10s.pt")
CHECKPOINT_3S  = os.path.join(MODEL_DIR, "best_model_passt_3s.pt")
# ─────────────────────────────────────────────────────────────────────────────


def load_models(device: str = "cpu") -> dict:
    """Load both models once and return them ready for inference."""
    print("Loading 10s model...")
    m10 = AudioClassifier.from_checkpoint(CHECKPOINT_10S).to(device)
    m10.eval()
    print("Loading 3s model...")
    m3  = AudioClassifier.from_checkpoint(CHECKPOINT_3S).to(device)
    m3.eval()
    return {"10s": m10, "3s": m3, "device": device}


def preprocess(signal: np.ndarray, n_samples: int) -> torch.Tensor:
    """
    Resample from 65536 → 32000 Hz, take the last n_samples, normalize.
    Returns (1, n_samples) float32 tensor.
    """
    # Resample
    target_len = int(len(signal) * TGT_SR / SRC_SR)
    resampled = scipy.signal.resample(signal, target_len).astype(np.float32)

    # Trim to last n_samples (crescendo peak)
    if len(resampled) >= n_samples:
        trimmed = resampled[-n_samples:]
    else:
        pad = np.zeros(n_samples - len(resampled), dtype=np.float32)
        trimmed = np.concatenate([pad, resampled])

    # Peak normalize
    peak = np.max(np.abs(trimmed))
    if peak > 0:
        trimmed /= peak

    return torch.tensor(trimmed, dtype=torch.float32).unsqueeze(0)  # (1, samples)


def _get_proba(model, waveform: torch.Tensor, device: str) -> np.ndarray:
    with torch.no_grad():
        logits = model(waveform.to(device))
        return F.softmax(logits, dim=-1).squeeze(0).cpu().numpy()


def predict(models: dict, signal: np.ndarray, w10: float = 0.5, w3: float = 0.5) -> dict:
    """
    Run inference on a raw signal array (at 65536 Hz).

    Args:
        models:  output of load_models()
        signal:  1D numpy array at 65536 Hz
        w10:     weight for 10s model (will be normalised)
        w3:      weight for 3s model  (will be normalised)

    Returns dict with keys:
        prediction_10s, proba_10s,
        prediction_3s,  proba_3s,
        prediction_ensemble, proba_ensemble
    """
    device = models["device"]
    total  = w10 + w3
    w10, w3 = w10 / total, w3 / total

    wav10 = preprocess(signal, SAMPLES_10S)
    wav3  = preprocess(signal, SAMPLES_3S)

    p10 = _get_proba(models["10s"], wav10, device)
    p3  = _get_proba(models["3s"],  wav3,  device)
    p_ens = w10 * p10 + w3 * p3

    def fmt(proba):
        return {CLASS_NAMES[i]: round(float(proba[i]), 4) for i in range(len(proba))}

    return {
        "prediction_10s":       CLASS_NAMES[int(np.argmax(p10))],
        "proba_10s":            fmt(p10),
        "prediction_3s":        CLASS_NAMES[int(np.argmax(p3))],
        "proba_3s":             fmt(p3),
        "prediction_ensemble":  CLASS_NAMES[int(np.argmax(p_ens))],
        "proba_ensemble":       fmt(p_ens),
    }


# ── CLI ───────────────────────────────────────────────────────────────────────
def main():
    p = argparse.ArgumentParser()
    p.add_argument("--input",   required=True, help="Path to parquet file")
    p.add_argument("--channel", default="AI0 (V)", help="Channel column name")
    p.add_argument("--device",  default="cpu")
    args = p.parse_args()

    import pyarrow.parquet as pq
    pf     = pq.read_table(args.input)
    signal = pf.to_pandas()[args.channel].to_numpy(dtype=np.float32)

    models = load_models(device=args.device)
    result = predict(models, signal)

    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
