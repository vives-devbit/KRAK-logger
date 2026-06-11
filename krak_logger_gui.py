"""KRAK Logger GUI entry point.

The application lives in the krak_logger package; this script only starts it
(kept so `python krak_logger_gui.py` and the PyInstaller spec keep working).
"""

# Older joblib model pickles reference WeightedEnsemble* on __main__; keep the
# names importable here so they unpickle when this script is the entry point.
from krak_logger.ml import (  # noqa: F401
    WeightedEnsemble,
    WeightedEnsemble_20260505,
    WeightedEnsemble_20260506,
)

from krak_logger.app import main

if __name__ == "__main__":
    main()
