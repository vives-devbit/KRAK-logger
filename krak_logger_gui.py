from HelpFunctions.lanxi import LanXI
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
import scipy.io.wavfile as wav
import tkinter as tk
from tkinter import messagebox, ttk
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
import threading
import boto3
import dotenv
import datetime
import sounddevice as sd
import os
import io

# Global variables
recording = False
DURATION = 1  # Default duration (can be adjusted)
OUTPUT_WAV_FILE = "recorded_audio.wav"
OUTPUT_PARQUET_FILE = "recorded_data.parquet"
BUCKET_NAME = "krak" # Replace with your bucket name
parameter_entries = {}
TEMP_DIR = "temp_files"

# IP of Lan-XI
dotenv.load_dotenv()
ip = os.getenv("BKDAQ_IP")
Lanxi = LanXI(ip)
Lanxi.setup_stream()
SAMPLE_RATE = Lanxi.sample_rate
NUM_SAMPLES = SAMPLE_RATE * DURATION

def update_parameters():
    global parameter_entries
    for widget in parameter_frame.winfo_children():
        widget.destroy()

    parameters = measurement_entry.get().split(',')
    parameter_entries.clear()
    for param in parameters:
        param = param.strip()
        if param:
            frame = tk.Frame(parameter_frame)
            frame.pack(fill=tk.X, padx=5, pady=2)
            tk.Label(frame, text=param + ":").pack(side=tk.LEFT)
            entry = tk.Entry(frame)
            entry.pack(side=tk.LEFT, expand=True, fill=tk.X)
            parameter_entries[param] = entry


def generate_filenames():
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    product_name = product_entry.get().replace(" ", "_")
    wav_filename = f"{product_name}_{timestamp}.wav"
    parquet_filename = f"{product_name}_{timestamp}.parquet"
    return wav_filename, parquet_filename


def ensure_temp_dir():
    if not os.path.exists(TEMP_DIR):
        os.makedirs(TEMP_DIR)


def record_data():
    global recording, NUM_SAMPLES, DURATION, OUTPUT_WAV_FILE, OUTPUT_PARQUET_FILE
    try:
        DURATION = float(duration_entry.get())
    except ValueError:
        messagebox.showerror("Invalid Input", "Please enter a valid number for duration.")
        return

    OUTPUT_WAV_FILE, OUTPUT_PARQUET_FILE = generate_filenames()
    ensure_temp_dir()
    OUTPUT_WAV_FILE = os.path.join(TEMP_DIR, OUTPUT_WAV_FILE)
    OUTPUT_PARQUET_FILE = os.path.join(TEMP_DIR, OUTPUT_PARQUET_FILE)

    recording = True

    time_axis, data = Lanxi.SampleChannels(DURATION)

    update_plot(time_axis, data)

    global recorded_data, recorded_time_axis
    recorded_data = data
    recorded_time_axis = time_axis
    recording = False

    # Save WAV file for AI0 only
    max_voltage = 10
    audio_data = (data[0] / max_voltage * 32767).astype(np.int16)
    wav.write(OUTPUT_WAV_FILE, SAMPLE_RATE, audio_data)

    messagebox.showinfo("Recording Complete", "Recording finished and audio saved.")


def save_to_parquet():
    if recorded_data is not None:
        timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        metadata = {
            "Product": product_entry.get(),
            "Measurement Parameters": measurement_entry.get(),
            "Timestamp": timestamp,
            "Sample Rate (Hz)": Lanxi.sample_rate,
        }

        for param, entry in parameter_entries.items():
            metadata[param] = entry.get()

        df = pd.DataFrame({
            "Time (s)": recorded_time_axis,
            "AI0 (V)": recorded_data[0],
            "AI1 (V)": recorded_data[1]
        })

        df.attrs.update(metadata)
        df.to_parquet(OUTPUT_PARQUET_FILE, index=False)
        print(f"Data saved as {OUTPUT_PARQUET_FILE} with metadata")
        messagebox.showinfo("Save Complete", f"Data saved as {OUTPUT_PARQUET_FILE}")
    else:
        messagebox.showwarning("No Data", "No recorded data to save.")

def upload_to_minio():
    # check if file exists
    if not os.path.exists(OUTPUT_PARQUET_FILE) or not os.path.exists(OUTPUT_WAV_FILE):
        messagebox.showwarning("Files Not Found", "Saving the files locally.")
        save_to_parquet()
    try:
        dotenv.load_dotenv()

        s3_client = boto3.client(
            "s3",
            endpoint_url=os.getenv("MINIO_ENDPOINT"),
            aws_access_key_id=os.getenv("MINIO_ACCESS_KEY"),
            aws_secret_access_key=os.getenv("MINIO_SECRET_KEY")
        )

        s3_client.upload_file(OUTPUT_PARQUET_FILE, BUCKET_NAME, os.path.basename(OUTPUT_PARQUET_FILE))
        s3_client.upload_file(OUTPUT_WAV_FILE, BUCKET_NAME, os.path.basename(OUTPUT_WAV_FILE))
        print(f"Files uploaded to MinIO: {OUTPUT_PARQUET_FILE}, {OUTPUT_WAV_FILE}")
        messagebox.showinfo("Upload Complete", f"Files uploaded to MinIO")

        # Refresh dropdown menu after upload
        dropdown_menu['values'] = list_s3_files()

    except Exception as e:
        messagebox.showerror("Upload Failed", f"Error: {str(e)}")


def start_recording():
    if not recording:
        threading.Thread(target=record_data, daemon=True).start()


def play_audio():
    try:
        rate, data = wav.read(OUTPUT_WAV_FILE)
        sd.play(data, rate)
        sd.wait()
    except Exception as e:
        messagebox.showerror("Playback Error", f"Unable to play audio: {str(e)}")


def update_plot(time_axis, data, title="Recorded Data"):
    ax1.clear()
    ax2.clear()
    ax1.plot(time_axis, data[0], 'b-', label="AI0")
    ax2.plot(time_axis, data[1], 'r-', label="AI1")
    ax1.set_xlabel("Time (s)")
    ax1.set_ylabel("AI0 Voltage (V)", color="b")
    ax2.set_ylabel("AI1 Voltage (V)", color="r")
    ax1.tick_params(axis="y", labelcolor="b")
    ax2.tick_params(axis="y", labelcolor="r")
    ax1.set_title(title)
    fig.canvas.draw()


def list_s3_files():
    dotenv.load_dotenv()
    s3_client = boto3.client(
        "s3",
        endpoint_url=os.getenv("MINIO_ENDPOINT"),
        aws_access_key_id=os.getenv("MINIO_ACCESS_KEY"),
        aws_secret_access_key=os.getenv("MINIO_SECRET_KEY")
    )
    response = s3_client.list_objects_v2(Bucket=BUCKET_NAME)
    names = set()
    if 'Contents' in response:
        for obj in response['Contents']:
            base = os.path.splitext(os.path.basename(obj['Key']))[0]
            names.add(base)
    return sorted(names)


def load_sample():
    try:
        base_name = dropdown_var.get()
        parquet_key = f"{base_name}.parquet"
        dotenv.load_dotenv()
        s3_client = boto3.client(
            "s3",
            endpoint_url=os.getenv("MINIO_ENDPOINT"),
            aws_access_key_id=os.getenv("MINIO_ACCESS_KEY"),
            aws_secret_access_key=os.getenv("MINIO_SECRET_KEY")
        )
        response = s3_client.get_object(Bucket=BUCKET_NAME, Key=parquet_key)
        df = pd.read_parquet(io.BytesIO(response['Body'].read()))
        update_plot(df["Time (s)"], [df["AI0 (V)"], df["AI1 (V)"]], title=base_name)
        metadata_text.delete("1.0", tk.END)
        for key, val in df.attrs.items():
            metadata_text.insert(tk.END, f"{key}: {val}\n")
    except Exception as e:
        messagebox.showerror("Load Error", str(e))

def on_closing():
    try:
        Lanxi.close_stream()
    except Exception as e:
        print(f"Error closing LAN-XI stream: {e}")
    # Attempt to stop all non-main threads gracefully
    for thread in threading.enumerate():
        if thread is not threading.main_thread():
            try:
                # If your threads are daemon, they will exit with the main program.
                # If not, you may need to implement a stop mechanism for your threads.
                # Here, we just print their names for awareness.
                print(f"Waiting for thread to finish: {thread.name}")
                thread.join(timeout=1)
            except Exception as e:
                print(f"Error joining thread {thread.name}: {e}")
    root.destroy()

# Create the GUI
root = tk.Tk()
root.title("HBK LAN-XI 3676 Recorder")

# Metadata Controls
control_frame = tk.Frame(root)
control_frame.pack(side=tk.RIGHT, fill=tk.Y, padx=10, pady=10)

tk.Label(control_frame, text="Product:").pack(anchor="e")
product_entry = tk.Entry(control_frame)
product_entry.pack(anchor="e")

tk.Label(control_frame, text="Measurement Parameters:").pack(anchor="e")
measurement_entry = tk.Entry(control_frame)
measurement_entry.pack(anchor="e")
measurement_entry.bind("<KeyRelease>", lambda event: update_parameters())

parameter_frame = tk.Frame(control_frame)
parameter_frame.pack(anchor="e")

duration_label = tk.Label(control_frame, text="Duration (s):")
duration_label.pack(anchor="e")
duration_entry = tk.Entry(control_frame)
duration_entry.insert(0, "1")
duration_entry.pack(anchor="e")

record_button = tk.Button(control_frame, text="Start Recording", command=start_recording)
record_button.pack(anchor="e")

save_parquet_button = tk.Button(control_frame, text="Save to Parquet", command=save_to_parquet)
save_parquet_button.pack(anchor="e")

upload_button = tk.Button(control_frame, text="Upload to MinIO", command=upload_to_minio)
upload_button.pack(anchor="e")

play_button = tk.Button(control_frame, text="Play Audio", command=play_audio)
play_button.pack(anchor="e")

# Dropdown to list S3 files
# add a label in front of the dropdown menu that says "Select sample from storage:"
dropdown_label = tk.Label(control_frame, text="Select sample from S3:")
dropdown_label.pack(anchor="e")
dropdown_var = tk.StringVar()
dropdown_menu = ttk.Combobox(control_frame, textvariable=dropdown_var, values=list_s3_files())
dropdown_menu.pack(anchor="e")
load_button = tk.Button(control_frame, text="Load Sample", command=load_sample)
load_button.pack(anchor="e")

metadata_text = tk.Text(control_frame, height=10, width=40)
metadata_text.pack(anchor="e")

fig, ax1 = plt.subplots()
ax2 = ax1.twinx()
canvas = FigureCanvasTkAgg(fig, master=root)
canvas.get_tk_widget().pack(side=tk.LEFT, expand=True, fill=tk.BOTH)

root.protocol("WM_DELETE_WINDOW", on_closing)
root.mainloop()
