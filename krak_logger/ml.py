"""Ensemble model classes, model discovery and feature-name parsing.

The WeightedEnsemble classes must be importable wherever joblib pickles
reference them (historically the classes lived in the __main__ script);
register_pickle_classes() injects them there before joblib.load.
"""

import os
import sys

import numpy as np
from sklearn.base import BaseEstimator, ClassifierMixin

from .config import BASE_DIR, MODELS_DIR


class WeightedEnsemble(BaseEstimator, ClassifierMixin):
    def __init__(self, models, feature_lists, weights, classes):
        self.models       = models
        self.feature_lists = feature_lists
        self.weights      = weights
        self.classes_     = classes

    def fit(self, X, y): return self

    def predict_proba(self, X):
        result = np.zeros((len(X), len(self.classes_)))
        cls_list = list(self.classes_)
        for mdl, feats, w in zip(self.models, self.feature_lists, self.weights):
            Xf = X.copy()
            for m in [f for f in feats if f not in Xf.columns]:
                Xf[m] = 0.0
            Xf = Xf[feats]
            proba = mdl.predict_proba(Xf)
            mdl_cls = list(mdl.classes_)
            for i, cls in enumerate(cls_list):
                if cls in mdl_cls:
                    result[:, i] += proba[:, mdl_cls.index(cls)] * w
        return result

    def predict(self, X):
        probas = self.predict_proba(X)
        return self.classes_[np.argmax(probas, axis=1)]


class WeightedEnsemble_20260505(WeightedEnsemble):
    pass


class WeightedEnsemble_20260506(WeightedEnsemble):
    pass


def register_pickle_classes():
    """Expose the ensemble classes on __main__ so joblib pickles that
    reference __main__.WeightedEnsemble* unpickle correctly."""
    main_mod = sys.modules.get("__main__")
    if main_mod is not None:
        main_mod.WeightedEnsemble = WeightedEnsemble
        main_mod.WeightedEnsemble_20260505 = WeightedEnsemble
        main_mod.WeightedEnsemble_20260506 = WeightedEnsemble


# ---------------------------------------------------------------------------
# Model discovery
# ---------------------------------------------------------------------------

def discover_available_models():
    """Return {display name: model directory} from MODELS_DIR (with fallback)."""
    available = {}
    if os.path.exists(MODELS_DIR):
        for d in os.listdir(MODELS_DIR):
            full_path = os.path.join(MODELS_DIR, d)
            if os.path.isdir(full_path):
                available[d] = full_path

    if not available:
        available = {
            "Standalone Ensemble": os.path.join(BASE_DIR, "standalone_ensemble"),
            "Standalone Ensemble 20260505": os.path.join(BASE_DIR, "standalone_ensemble_20260505"),
            "Standalone Ensemble Noisy": os.path.join(BASE_DIR, "standalone_ensemble_noisy"),
        }
    return available


AVAILABLE_MODELS_DICT = discover_available_models()

# ---------------------------------------------------------------------------
# Feature-extraction parsing constants
# ---------------------------------------------------------------------------
KNOWN_FILTERS = [
    "nofilter",
    "antialiasing_32khz",
    "standard_bandpass_1000_25000",
    "band5k_0_5000",
    "band5k_5000_10000",
    "band5k_10000_15000",
    "band5k_15000_20000",
    "band5k_20000_25000",
    "acoustic_range",
    "mid_frequency",
    "high_frequency",
    "low_frequency",
    "ultrasonic",
]

KNOWN_CONFIGS = {
    "default", "high_res_temporal", "high_res_spectral",
    "speech_standard", "percussive_focus", "balanced",
    "Tresh_0_Prom_8e-2", "Tresh_1e-2_Prom_8e-2", "Tresh_2e-2_Prom_8e-2", "Tresh_2e-2_Prom_1e-1"
}

LFCC_ONLY_PREFIXES = (
    "lfcc_mean_", "lfcc_std_", "lfcc_max_", "lfcc_min_", "lfcc_range_",
    "lfcc_zcr_", "lfcc_entropy_", "lfcc_regularity_",
    "lfcc_deriv_std_", "lfcc_deriv_mean_abs_",
    "delta_lfcc_", "delta2_lfcc_",
)

MFCC_ONLY_PREFIXES = (
    "mfcc_mean_", "mfcc_std_", "mfcc_max_", "mfcc_min_", "mfcc_range_",
    "mfcc_zcr_", "mfcc_entropy_", "mfcc_regularity_",
    "mfcc_deriv_std_", "mfcc_deriv_mean_abs_",
    "delta_mfcc_", "delta2_mfcc_",
    "chroma_", "chroma_std_",
    "tonnetz_", "poly_",
)

ALL_SPECTRUM_PREFIXES = LFCC_ONLY_PREFIXES + MFCC_ONLY_PREFIXES

PEAK_DEPENDENT_FEATURES = {
    "num_peaks", "mean_peak_height", "std_peak_height",
    "max_peak_height", "min_peak_height", "peak_density",
    "mean_peak_width", "std_peak_width", "mean_peak_prominence",
    "std_peak_prominence", "mean_peak_interval", "std_peak_interval",
}


def parse_feature(feat_name: str):
    """Return (base_feature, filter_name, config_or_None)."""
    for filt in sorted(KNOWN_FILTERS, key=len, reverse=True):
        for cfg in KNOWN_CONFIGS:
            if feat_name.endswith(f"_{filt}_{cfg}"):
                base = feat_name[: -(len(filt) + len(cfg) + 2)]
                return base, filt, cfg
        if feat_name.endswith(f"_{filt}"):
            base = feat_name[: -(len(filt) + 1)]
            return base, filt, None
    return feat_name, None, None


def load_required_features(model_dir):
    import json
    json_path = os.path.join(model_dir, "elastic_net_model_selected_features.json")
    if os.path.exists(json_path):
        with open(json_path, "r") as fh:
            return json.load(fh)

    req_path = os.path.join(model_dir, "required_features.txt")
    with open(req_path, "r") as fh:
        return [ln.strip() for ln in fh if ln.strip()]
