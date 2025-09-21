"""Signal processing class for trimming acoustic signals based on reference thresholds."""

import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
import os
from typing import Tuple, Dict, Any, Optional


class SignalProcessor:
    """Handles signal processing operations including threshold-based trimming."""

    def __init__(self, config: Dict[str, Any]):
        """Initialize with configuration parameters.

        Args:
            config: Dictionary containing processing parameters
        """
        self.config = config
        self.acoustic_column = config.get('ACOUSTIC_COLUMN', 'AI0 (V)')
        self.reference_column = config.get('REFERENCE_COLUMN', 'AI1 (V)')
        self.sampling_rate = config.get('SAMPLING_RATE', 65536)
        self.start_threshold = config.get('START_THRESHOLD', 0.1)
        self.stop_threshold = config.get('STOP_THRESHOLD', 0.4)
        self.hysteresis = config.get('HYSTERESIS', 0.2)
        self.extra_time_seconds = config.get('EXTRA_TIME_SECONDS', 0.250)
        self.min_duration_below_threshold = config.get('MIN_DURATION_BELOW_THRESHOLD', 0.25)
        self.required_metadata_fields = config.get('REQUIRED_METADATA_FIELDS',
                                                 ["Moisture", "Speed", "Orientation", "Distance"])

    def extract_metadata(self, df: pd.DataFrame) -> Dict[str, Any]:
        """Extract metadata from parquet file attributes.

        Args:
            df: Pandas dataframe with metadata in attrs

        Returns:
            dict: Dictionary with metadata values, None for missing fields
        """
        metadata = {}
        for field in self.required_metadata_fields:
            metadata[field] = df.attrs.get(field, None)
        return metadata

    def trim_by_threshold(self, acoustic_signal: np.ndarray, reference_signal: np.ndarray) -> Tuple[np.ndarray, int, int]:
        """Trim signals based on threshold crossing in reference signal.

        Args:
            acoustic_signal: Acoustic signal array
            reference_signal: Reference signal array (DC offset will be removed in this function)

        Returns:
            tuple: (trimmed_acoustic, start_idx, end_idx)
        """
        # Remove DC offset using mean of first 0.5 seconds (or entire signal if shorter)
        half_second_samples = int(0.5 * self.sampling_rate)
        baseline_samples = min(half_second_samples, len(reference_signal))
        dc_offset = np.mean(reference_signal[:baseline_samples])
        reference_dc_removed = reference_signal - dc_offset

        # Find first point where signal crosses start threshold (positive direction)
        start_indices = np.where(reference_dc_removed >= self.start_threshold)[0]
        if len(start_indices) == 0:
            # No crossing found, return empty signal
            return np.array([]), 0, 0
        start_idx = start_indices[0]

        # Find first point after start where signal goes above stop threshold + hysteresis
        hysteresis_threshold = self.stop_threshold + self.hysteresis
        above_hysteresis = np.where(reference_dc_removed[start_idx:] >= hysteresis_threshold)[0]

        if len(above_hysteresis) == 0:
            # Signal never goes above hysteresis threshold, use end of signal
            end_idx = len(reference_signal)
        else:
            # Found where it goes above hysteresis threshold, now find where it goes back below stop threshold
            hysteresis_idx = start_idx + above_hysteresis[0]
            below_stop = np.where(reference_dc_removed[hysteresis_idx:] < self.stop_threshold)[0]

            if len(below_stop) == 0:
                # Signal never goes back below stop threshold, use end of signal
                end_idx = len(reference_signal)
            else:
                # Find where signal stays below stop threshold for minimum duration
                min_duration_samples = int(self.min_duration_below_threshold * self.sampling_rate)
                stop_idx = None

                # Check each point where signal goes below threshold
                for below_idx in below_stop:
                    candidate_stop_idx = hysteresis_idx + below_idx

                    # Check if signal stays below threshold for minimum duration
                    end_check_idx = min(len(reference_signal), candidate_stop_idx + min_duration_samples)
                    check_window = reference_dc_removed[candidate_stop_idx:end_check_idx]

                    # If all samples in the window are below threshold, this is a valid stop
                    if len(check_window) > 0 and np.all(check_window < self.stop_threshold):
                        stop_idx = candidate_stop_idx
                        break

                if stop_idx is None:
                    # No valid stop found (signal doesn't stay below threshold long enough)
                    end_idx = len(reference_signal)
                else:
                    # Add extra time after stop condition is met
                    extra_samples = int(self.extra_time_seconds * self.sampling_rate)
                    end_idx = min(len(reference_signal), stop_idx + extra_samples)

        return acoustic_signal[start_idx:end_idx], start_idx, end_idx

    def remove_dc_offset(self, signal: np.ndarray) -> Tuple[np.ndarray, float]:
        """Remove DC offset from signal using baseline estimation.

        Args:
            signal: Input signal array

        Returns:
            tuple: (dc_compensated_signal, dc_offset_value)
        """
        # Remove DC offset using mean of first 0.5 seconds (or entire signal if shorter)
        half_second_samples = int(0.5 * self.sampling_rate)
        baseline_samples = min(half_second_samples, len(signal))
        dc_offset = np.mean(signal[:baseline_samples])
        signal_dc_removed = signal - dc_offset

        return signal_dc_removed, dc_offset

    def plot_trimming(self, acoustic: np.ndarray, reference: np.ndarray, peak_idx: int,
                     start: int, end: int, filename: str, is_skipped: bool = False,
                     output_dir: str = "plots") -> None:
        """Plot the full acoustic and reference signal with trimming overlay and save to file.

        Args:
            acoustic: Acoustic signal array
            reference: Reference signal array
            peak_idx: Peak detection index
            start: Start trimming index
            end: End trimming index
            filename: Base filename for saving
            is_skipped: Whether this signal was skipped
            output_dir: Directory to save plots
        """
        time = np.arange(len(acoustic)) / self.sampling_rate
        ref_time = np.arange(len(reference)) / self.sampling_rate

        plt.figure(figsize=(14, 6))

        plt.subplot(2, 1, 1)
        plt.plot(time, acoustic, label="Acoustic Signal")
        plt.axvline(peak_idx / self.sampling_rate, color='r', linestyle='--', label="Peak Detected")
        if not is_skipped:
            plt.axvspan(start / self.sampling_rate, end / self.sampling_rate,
                       color='orange', alpha=0.3, label="Trim Window")
        plt.title("Acoustic Signal with Trim Region" + (" (SKIPPED - Below Threshold)" if is_skipped else ""))
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

        # Save to appropriate folder
        folder = os.path.join(output_dir, "skipped") if is_skipped else output_dir
        os.makedirs(folder, exist_ok=True)
        plt.savefig(f"{folder}/{filename}_trimming.png", dpi=150, bbox_inches='tight')
        plt.close()

    def process_dataframe(self, df: pd.DataFrame, should_plot: bool = False,
                         plot_filename: Optional[str] = None, output_dir: str = "plots") -> Tuple[pd.DataFrame, bool]:
        """Process a single dataframe and return trimmed result.

        Args:
            df: Input dataframe with acoustic and reference signals
            should_plot: Whether to generate plots
            plot_filename: Base filename for plots (required if should_plot=True)
            output_dir: Directory for saving plots

        Returns:
            tuple: (trimmed_dataframe or None, was_skipped)
        """
        try:
            acoustic_signal = df[self.acoustic_column].values.astype(np.float32)
            reference_signal = df[self.reference_column].values.astype(np.float32)

            trimmed_signal, start_idx, end_idx = self.trim_by_threshold(acoustic_signal, reference_signal)

            # Check if trimmed signal is empty
            if len(trimmed_signal) == 0:
                if should_plot and plot_filename:
                    self.plot_trimming(acoustic_signal, reference_signal, 0, 0, len(acoustic_signal),
                                     plot_filename, is_skipped=True, output_dir=output_dir)
                return None, True

            if should_plot and plot_filename:
                self.plot_trimming(acoustic_signal, reference_signal, 0, start_idx, end_idx,
                                 plot_filename, is_skipped=False, output_dir=output_dir)

            # Create trimmed dataframe with the acoustic signal
            trimmed_df = pd.DataFrame({
                self.acoustic_column: trimmed_signal
            })

            # Extract and preserve metadata in the trimmed parquet file
            metadata = self.extract_metadata(df)
            for key, value in metadata.items():
                trimmed_df.attrs[key] = value

            return trimmed_df, False

        except Exception as e:
            raise Exception(f"Error processing dataframe: {e}")

    def signal_to_audio_buffer(self, signal: np.ndarray, normalize: bool = True) -> bytes:
        """Convert signal array to WAV audio buffer for playback.

        Args:
            signal: Signal array to convert to audio
            normalize: Whether to normalize signal amplitude for better audio playback

        Returns:
            bytes: WAV audio data as bytes for st.audio()
        """
        try:
            import soundfile as sf
            from io import BytesIO

            # Prepare signal for audio playback
            audio_signal = signal.copy().astype(np.float32)

            # Normalize signal if requested (recommended for audio playback)
            if normalize:
                # Avoid division by zero
                max_val = np.max(np.abs(audio_signal))
                if max_val > 0:
                    audio_signal = audio_signal / max_val * 0.8  # Scale to 80% to avoid clipping

            # Convert to audio buffer
            audio_buffer = BytesIO()
            sf.write(audio_buffer, audio_signal, samplerate=self.sampling_rate, format='WAV')
            audio_buffer.seek(0)

            return audio_buffer.read()

        except Exception as e:
            raise Exception(f"Error converting signal to audio: {e}")

    def create_audio_comparison(self, original_signal: np.ndarray, trimmed_signal: np.ndarray) -> Tuple[bytes, bytes]:
        """Create audio buffers for both original and trimmed signals for comparison.

        Args:
            original_signal: Original full signal
            trimmed_signal: Trimmed signal

        Returns:
            tuple: (original_audio_bytes, trimmed_audio_bytes)
        """
        try:
            original_audio = self.signal_to_audio_buffer(original_signal)
            trimmed_audio = self.signal_to_audio_buffer(trimmed_signal)
            return original_audio, trimmed_audio

        except Exception as e:
            raise Exception(f"Error creating audio comparison: {e}")