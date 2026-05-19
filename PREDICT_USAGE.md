# Chip Classification — Inference Guide

Two PaSST-based models trained on chip-cutting acoustic signals, combined into an ensemble.

## Models

| Model | Input | Accuracy (test set, n=31) |
|---|---|---|
| `best_model_passt_10s.pt` | Last 10s of recording | 77% |
| `best_model_passt_3s.pt` | Last 3s of recording | 65% |
| Ensemble (50/50 average) | Both | 77% (higher precision) |

**Classes:** `type_1` through `type_5`

---

## Signal Requirements

| Property | Value |
|---|---|
| Input sample rate | **65536 Hz** (resampled internally to 32 kHz) |
| Channel | AI0 (V) — air microphone |
| Minimum duration | 10 seconds recommended |

### Normalization

`predict.py` applies **peak normalization** automatically on every call:

```
signal_normalized = signal / max(abs(signal))
```

This makes predictions **invariant to microphone gain and sensitivity** — a louder or quieter mic will give the same result. No manual normalization needed before calling `predict()`.

---

## Usage

### 1. Install dependencies

```bash
cd /root/Krak_Repo/KRAK_DeepX
source venv/bin/activate
```

### 2. From the command line (single parquet file)

```bash
python predict.py --input /path/to/recording.parquet
python predict.py --input /path/to/recording.parquet --channel "AI0 (V)"
```

Output:
```json
{
  "prediction_10s": "type_2",
  "proba_10s": {"type_1": 0.04, "type_2": 0.81, "type_3": 0.07, "type_4": 0.05, "type_5": 0.03},
  "prediction_3s": "type_2",
  "proba_3s": {"type_1": 0.06, "type_2": 0.74, "type_3": 0.09, "type_4": 0.07, "type_5": 0.04},
  "prediction_ensemble": "type_2",
  "proba_ensemble": {"type_1": 0.05, "type_2": 0.77, "type_3": 0.08, "type_4": 0.06, "type_5": 0.04}
}
```

### 3. In a Python pipeline

```python
import numpy as np
from predict import load_models, predict

# Load once at startup (takes ~10s, loads both model checkpoints)
models = load_models()

# --- In your recording loop ---

# Option A: pass a raw numpy array (at 65536 Hz)
signal = my_daq.read_channel("AI0")          # 1D numpy array
result = predict(models, signal)

# Option B: load from parquet
import pyarrow.parquet as pq
pf = pq.read_table("recording.parquet")
signal = pf.to_pandas()["AI0 (V)"].to_numpy(dtype="float32")
result = predict(models, signal)

# Use the result
print(result["prediction_ensemble"])         # e.g. "type_3"
print(result["proba_ensemble"])              # probabilities for all 5 classes

# Or access individual models
print(result["prediction_10s"])
print(result["prediction_3s"])
```

### 4. Custom ensemble weights

If you want to weight the 10s model more heavily:

```python
result = predict(models, signal, w10=0.7, w3=0.3)
```

Weights are normalized automatically, so `w10=7, w3=3` gives the same result.

---

## Adding the Elastic Net to the Ensemble

Once the elastic net is trained on the same split (`data/split_manifest.csv`), add it:

```python
import joblib
import sys
sys.path.insert(0, "/path/to/standalone_ensemble_noisy")
from inference import load_ensemble, predict_new_data

# Load all models
deep_models = load_models()
elastic_net  = load_ensemble("/path/to/weighted_ensemble_model.joblib")

# Get probabilities from each
deep_result  = predict(deep_models, signal)
en_pred, en_proba = predict_new_data(elastic_net, features_df)  # your 145-feature df

# Combine (example: equal thirds)
import numpy as np
p_deep = np.array(list(deep_result["proba_ensemble"].values()))
p_en   = en_proba[0]  # shape (5,)
p_final = (p_deep + p_en) / 2

class_names = ["type_1", "type_2", "type_3", "type_4", "type_5"]
print("Final prediction:", class_names[np.argmax(p_final)])
```

---

## File Overview

```
KRAK_DeepX/
├── predict.py                  ← inference pipeline (this guide)
├── best_model_passt_10s.pt     ← trained 10s model checkpoint
├── best_model_passt_3s.pt      ← trained 3s model checkpoint
├── preprocess.py               ← parquet → WAV conversion (training only)
├── train.py                    ← model training script
├── ensemble.py                 ← batch evaluation of the ensemble on test set
├── data/
│   ├── split_manifest.csv      ← train/val/test file assignments (use for elastic net)
│   ├── train/, val/, test/     ← WAV files organized by class
└── results/
    ├── report_passt_10s.txt
    ├── report_ensemble_10s_3s.txt
    └── confusion_matrix_*.png
```
