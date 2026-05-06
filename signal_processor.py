"""Signal processing class for trimming acoustic signals based on reference thresholds."""

import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
import os
from typing import Tuple, Dict, Any, Optional


class SignalProcessor:
    """Handles signal processing operations including threshold-based trimming."""

    def __init__(self, config: Dict[str, Any], filter_helper=None):
        self.config = config
        self.acoustic_column = config.get('ACOUSTIC_COLUMN', 'AI0 (V)')
        self.reference_column = config.get('REFERENCE_COLUMN', 'AI1 (V)')
        self.sampling_rate = config.get('SAMPLING_RATE', 65536)
        self.start_threshold = config.get('START_THRESHOLD', 0.1)
        self.start_extra_time_seconds = config.get('START_EXTRA_TIME_SECONDS', 0.0)
        self.stop_threshold = config.get('STOP_THRESHOLD', 0.4)
        self.hysteresis = config.get('HYSTERESIS', 0.2)
        self.extra_time_seconds = config.get('EXTRA_TIME_SECONDS', 0.250)
        self.min_duration_below_threshold = config.get('MIN_DURATION_BELOW_THRESHOLD', 0.25)
        self.required_metadata_fields = config.get('REQUIRED_METADATA_FIELDS',
                                                 ["Moisture", "Speed", "Orientation", "Distance"])
        # Peak force trimming parameters
        self.peak_force_time_before = config.get('PEAK_FORCE_TIME_BEFORE', 0.5)
        self.peak_force_time_after = config.get('PEAK_FORCE_TIME_AFTER', 0.5)
        # Threshold-to-peak trimming parameters
        self.threshold_to_peak_threshold = config.get('THRESHOLD_TO_PEAK_THRESHOLD', 0.1)
        self.threshold_to_peak_time_before = config.get('THRESHOLD_TO_PEAK_TIME_BEFORE', 0.1)
        self.threshold_to_peak_time_after = config.get('THRESHOLD_TO_PEAK_TIME_AFTER', 0.1)
        # Percentage-based threshold trimming parameters
        self.start_threshold_pct = config.get('START_THRESHOLD_PCT', 10.0)
        self.stop_threshold_pct = config.get('STOP_THRESHOLD_PCT', 40.0)
        self.hysteresis_pct = config.get('HYSTERESIS_PCT', 20.0)
        # Filter (not used locally but accepted so callers can pass filter_helper)
        self.filter_helper = filter_helper

    def extract_metadata(self, df: pd.DataFrame) -> Dict[str, Any]:
        metadata = {}
        for field in self.required_metadata_fields:
            metadata[field] = df.attrs.get(field, None)
        return metadata

    def _remove_dc(self, reference_signal: np.ndarray) -> np.ndarray:
        """Remove DC offset using the mean of the first 0.5 s."""
        half_second_samples = int(0.5 * self.sampling_rate)
        baseline_samples = min(half_second_samples, len(reference_signal))
        dc_offset = np.mean(reference_signal[:baseline_samples])
        return reference_signal - dc_offset

    def trim_by_threshold(self, acoustic_signal: np.ndarray, reference_signal: np.ndarray,
                          ai2_signal: Optional[np.ndarray] = None) -> Tuple[np.ndarray, int, int]:
        """Trim signals based on threshold crossing in reference signal."""
        reference_dc_removed = self._remove_dc(reference_signal)

        # Find first point where signal crosses start threshold (positive direction)
        start_indices = np.where(reference_dc_removed >= self.start_threshold)[0]
        if len(start_indices) == 0:
            return np.array([]), 0, 0
        start_idx = start_indices[0]

        # Apply start extra time (go back before the threshold crossing)
        extra_start_samples = int(self.start_extra_time_seconds * self.sampling_rate)
        start_idx = max(0, start_idx - extra_start_samples)

        # Find first point after start where signal goes above stop threshold + hysteresis
        hysteresis_threshold = self.stop_threshold + self.hysteresis
        above_hysteresis = np.where(reference_dc_removed[start_idx:] >= hysteresis_threshold)[0]

        if len(above_hysteresis) == 0:
            end_idx = len(reference_signal)
        else:
            hysteresis_idx = start_idx + above_hysteresis[0]
            below_stop = np.where(reference_dc_removed[hysteresis_idx:] < self.stop_threshold)[0]

            if len(below_stop) == 0:
                end_idx = len(reference_signal)
            else:
                min_duration_samples = int(self.min_duration_below_threshold * self.sampling_rate)
                stop_idx = None

                for below_idx in below_stop:
                    candidate_stop_idx = hysteresis_idx + below_idx
                    end_check_idx = min(len(reference_signal), candidate_stop_idx + min_duration_samples)
                    check_window = reference_dc_removed[candidate_stop_idx:end_check_idx]
                    if len(check_window) > 0 and np.all(check_window < self.stop_threshold):
                        stop_idx = candidate_stop_idx
                        break

                if stop_idx is None:
                    end_idx = len(reference_signal)
                else:
                    extra_samples = int(self.extra_time_seconds * self.sampling_rate)
                    end_idx = min(len(reference_signal), stop_idx + extra_samples)

        return acoustic_signal[start_idx:end_idx], start_idx, end_idx

    def trim_by_peak_force(self, acoustic_signal: np.ndarray, reference_signal: np.ndarray,
                           ai2_signal: Optional[np.ndarray] = None) -> Tuple[np.ndarray, int, int]:
        """Trim based on a window around the maximum peak in the reference signal (loadcell)."""
        reference_dc_removed = self._remove_dc(reference_signal)

        max_peak_idx = int(np.argmax(np.abs(reference_dc_removed)))

        samples_before = int(self.peak_force_time_before * self.sampling_rate)
        samples_after = int(self.peak_force_time_after * self.sampling_rate)

        start_idx = max(0, max_peak_idx - samples_before)
        end_idx = min(len(acoustic_signal), max_peak_idx + samples_after)

        return acoustic_signal[start_idx:end_idx], start_idx, end_idx

    def trim_by_threshold_to_peak(self, acoustic_signal: np.ndarray, reference_signal: np.ndarray,
                                  ai2_signal: Optional[np.ndarray] = None) -> Tuple[np.ndarray, int, int]:
        """Trim using threshold crossing as start and loadcell peak as end."""
        reference_dc_removed = self._remove_dc(reference_signal)

        abs_reference = np.abs(reference_dc_removed)
        max_value = np.max(abs_reference)
        if max_value <= 0:
            return np.array([]), 0, 0

        normalized_reference = abs_reference / max_value

        threshold_crossings = np.where(normalized_reference >= self.threshold_to_peak_threshold)[0]
        if len(threshold_crossings) == 0:
            return np.array([]), 0, 0

        threshold_idx = threshold_crossings[0]
        max_peak_idx = int(np.argmax(abs_reference))

        samples_before_threshold = int(self.threshold_to_peak_time_before * self.sampling_rate)
        samples_after_peak = int(self.threshold_to_peak_time_after * self.sampling_rate)

        start_idx = max(0, threshold_idx - samples_before_threshold)
        end_idx = min(len(acoustic_signal), max_peak_idx + samples_after_peak)

        if start_idx >= end_idx:
            return np.array([]), 0, 0

        return acoustic_signal[start_idx:end_idx], start_idx, end_idx

    def trim_by_threshold_pct(self, acoustic_signal: np.ndarray, reference_signal: np.ndarray,
                              ai2_signal: Optional[np.ndarray] = None) -> Tuple[np.ndarray, int, int]:
        """Trim using start/stop thresholds expressed as a percentage of the loadcell peak."""
        reference_dc_removed = self._remove_dc(reference_signal)
        max_value = np.max(reference_dc_removed)

        if max_value <= 0:
            return np.array([]), 0, 0

        orig_start = self.start_threshold
        orig_stop = self.stop_threshold
        orig_hysteresis = self.hysteresis
        self.start_threshold = (self.start_threshold_pct / 100.0) * max_value
        self.stop_threshold = (self.stop_threshold_pct / 100.0) * max_value
        self.hysteresis = (self.hysteresis_pct / 100.0) * max_value

        result = self.trim_by_threshold(acoustic_signal, reference_signal, ai2_signal)

        self.start_threshold = orig_start
        self.stop_threshold = orig_stop
        self.hysteresis = orig_hysteresis

        return result

    def remove_dc_offset(self, signal: np.ndarray) -> Tuple[np.ndarray, float]:
        half_second_samples = int(0.5 * self.sampling_rate)
        baseline_samples = min(half_second_samples, len(signal))
        dc_offset = np.mean(signal[:baseline_samples])
        return signal - dc_offset, dc_offset

    def plot_trimming(self, acoustic: np.ndarray, reference: np.ndarray, peak_idx: int,
                     start: int, end: int, filename: str, is_skipped: bool = False,
                     output_dir: str = "plots") -> None:
        time = np.arange(len(acoustic)) / self.sampling_rate
        ref_time = np.arange(len(reference)) / self.sampling_rate

        plt.figure(figsize=(14, 6))

        plt.subplot(2, 1, 1)
        plt.plot(time, acoustic, label="Acoustic Signal")
        plt.axvline(peak_idx / self.sampling_rate, color='r', linestyle='--', label="Peak Detected")
        if not is_skipped:
            plt.axvspan(start / self.sampling_rate, end / self.sampling_rate,
                       color='orange', alpha=0.3, label="Trim Window")
        plt.title("Acoustic Signal with Trim Region" + (" (SKIPPED)" if is_skipped else ""))
        plt.xlabel("Time [s]")
        plt.ylabel("Amplitude")
        plt.legend()

        plt.subplot(2, 1, 2)
        plt.plot(ref_time, reference, label="Reference Signal", color="purple")
        plt.axvline(peak_idx / self.sampling_rate, color='r', linestyle='--', label="Peak Detected")
        plt.title("Reference Signal (Trigger)" + (" - Below Threshold" if is_skipped else ""))
        plt.xlabel("Time [s]")
        plt.ylabel("Amplitude")
        plt.legend()

        plt.tight_layout()

        folder = os.path.join(output_dir, "skipped") if is_skipped else output_dir
        os.makedirs(folder, exist_ok=True)
        plt.savefig(f"{folder}/{filename}_trimming.png", dpi=150, bbox_inches='tight')
        plt.close()

    def process_dataframe(self, df: pd.DataFrame, should_plot: bool = False,
                         plot_filename: Optional[str] = None, output_dir: str = "plots") -> Tuple[pd.DataFrame, bool]:
        try:
            acoustic_signal = df[self.acoustic_column].values.astype(np.float32)
            reference_signal = df[self.reference_column].values.astype(np.float32)

            trimmed_signal, start_idx, end_idx = self.trim_by_threshold(acoustic_signal, reference_signal)

            if len(trimmed_signal) == 0:
                if should_plot and plot_filename:
                    self.plot_trimming(acoustic_signal, reference_signal, 0, 0, len(acoustic_signal),
                                     plot_filename, is_skipped=True, output_dir=output_dir)
                return None, True

            if should_plot and plot_filename:
                self.plot_trimming(acoustic_signal, reference_signal, 0, start_idx, end_idx,
                                 plot_filename, is_skipped=False, output_dir=output_dir)

            trimmed_df = pd.DataFrame({self.acoustic_column: trimmed_signal})
            metadata = self.extract_metadata(df)
            for key, value in metadata.items():
                trimmed_df.attrs[key] = value

            return trimmed_df, False

        except Exception as e:
            raise Exception(f"Error processing dataframe: {e}")
