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
loaded_df = None  # For storing loaded sample data
current_sample_name = None  # For storing current sample name

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
        
        # Store the loaded dataframe globally for metadata updates
        global loaded_df, current_sample_name
        loaded_df = df
        current_sample_name = base_name
    except Exception as e:
        messagebox.showerror("Load Error", str(e))


def save_metadata_to_s3():
    global loaded_df, current_sample_name
    
    # Input validation on main thread
    if loaded_df is None:
        messagebox.showwarning("No Sample Loaded", "Please load a sample first before adding metadata.")
        return
        
    key = metadata_key_entry.get().strip()
    value = metadata_value_entry.get().strip()
    
    if not key or not value:
        messagebox.showwarning("Invalid Input", "Please enter both key and value for metadata.")
        return
    
    # Capture values to prevent race conditions
    metadata_key = key
    metadata_value = value
    sample_name = current_sample_name
    original_df = loaded_df.copy()
    
    def save_worker():
        backup_key = None
        old_key = None
        
        try:
            # Update button to show progress
            root.after(0, lambda: save_metadata_button.config(text="Saving data...", state="disabled"))
            
            # Create updated dataframe
            updated_df = original_df.copy()
            updated_df.attrs[metadata_key] = metadata_value
            
            # Prepare S3 client
            dotenv.load_dotenv()
            s3_client = boto3.client(
                "s3",
                endpoint_url=os.getenv("MINIO_ENDPOINT"),
                aws_access_key_id=os.getenv("MINIO_ACCESS_KEY"),
                aws_secret_access_key=os.getenv("MINIO_SECRET_KEY")
            )
            
            # Define file names
            original_key = f"{sample_name}.parquet"
            backup_key = f"{sample_name}_backup_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.parquet"
            old_key = f"{sample_name}_old_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.parquet"
            
            # Step 1: Verify original file exists
            try:
                s3_client.head_object(Bucket=BUCKET_NAME, Key=original_key)
            except Exception:
                raise Exception(f"Original file {original_key} not found in S3")
            
            # Step 2: Create backup with updated data and verify upload
            root.after(0, lambda: save_metadata_button.config(text="Creating backup..."))
            parquet_buffer = io.BytesIO()
            updated_df.to_parquet(parquet_buffer, index=False)
            buffer_size = parquet_buffer.tell()
            parquet_buffer.seek(0)
            
            s3_client.upload_fileobj(parquet_buffer, BUCKET_NAME, backup_key)
            
            # Verify backup upload
            backup_obj = s3_client.head_object(Bucket=BUCKET_NAME, Key=backup_key)
            if backup_obj['ContentLength'] != buffer_size:
                raise Exception("Backup file upload verification failed - size mismatch")
            
            # Step 3: Copy original to old version and verify
            root.after(0, lambda: save_metadata_button.config(text="Backing up original..."))
            s3_client.copy_object(
                Bucket=BUCKET_NAME,
                CopySource={'Bucket': BUCKET_NAME, 'Key': original_key},
                Key=old_key
            )
            
            # Verify old file copy
            s3_client.head_object(Bucket=BUCKET_NAME, Key=old_key)
            
            # Step 4: Replace original with backup and verify
            root.after(0, lambda: save_metadata_button.config(text="Finalizing save..."))
            s3_client.copy_object(
                Bucket=BUCKET_NAME,
                CopySource={'Bucket': BUCKET_NAME, 'Key': backup_key},
                Key=original_key
            )
            
            # Verify final file
            final_obj = s3_client.head_object(Bucket=BUCKET_NAME, Key=original_key)
            if final_obj['ContentLength'] != buffer_size:
                # Attempt rollback
                try:
                    s3_client.copy_object(
                        Bucket=BUCKET_NAME,
                        CopySource={'Bucket': BUCKET_NAME, 'Key': old_key},
                        Key=original_key
                    )
                    raise Exception("Final file verification failed - rolled back to original")
                except Exception as rollback_error:
                    raise Exception(f"Final file verification failed and rollback failed: {rollback_error}")
            
            # Step 5: Clean up only after successful verification
            try:
                s3_client.delete_object(Bucket=BUCKET_NAME, Key=backup_key)
                s3_client.delete_object(Bucket=BUCKET_NAME, Key=old_key)
            except Exception as cleanup_error:
                # Log but don't fail - the main operation succeeded
                print(f"Warning: Cleanup failed but data was saved successfully: {cleanup_error}")
            
            # Success - update UI on main thread
            def success_update():
                global loaded_df
                loaded_df = updated_df
                metadata_text.delete("1.0", tk.END)
                for k, v in updated_df.attrs.items():
                    metadata_text.insert(tk.END, f"{k}: {v}\n")
                    
                # Clear input fields
                metadata_key_entry.delete(0, tk.END)
                metadata_value_entry.delete(0, tk.END)
                
                # Restore button
                save_metadata_button.config(text="Save Metadata to S3", state="normal")
                
                messagebox.showinfo("Success", f"Metadata '{metadata_key}' added and saved to S3 safely.")
            
            root.after(0, success_update)
            
        except Exception as e:
            # Error handling with attempted cleanup
            error_msg = str(e)
            
            # Try to clean up any partial uploads
            if backup_key:
                try:
                    s3_client.delete_object(Bucket=BUCKET_NAME, Key=backup_key)
                except:
                    pass  # Ignore cleanup errors during error handling
            
            if old_key:
                try:
                    s3_client.delete_object(Bucket=BUCKET_NAME, Key=old_key)
                except:
                    pass  # Ignore cleanup errors during error handling
            
            def error_update():
                save_metadata_button.config(text="Save Metadata to S3", state="normal")
                messagebox.showerror("Save Error", f"Failed to save metadata safely: {error_msg}")
            
            root.after(0, error_update)
    
    # Start the safe save operation in background thread
    threading.Thread(target=save_worker, daemon=True).start()


def rename_file_in_s3():
    global loaded_df, current_sample_name
    
    # Input validation on main thread
    if loaded_df is None:
        messagebox.showwarning("No Sample Loaded", "Please load a sample first before renaming.")
        return
        
    new_name = rename_entry.get().strip()
    
    if not new_name:
        messagebox.showwarning("Invalid Input", "Please enter a new filename.")
        return
    
    # Remove file extension if provided
    if new_name.endswith('.parquet'):
        new_name = new_name[:-8]
    if new_name.endswith('.wav'):
        new_name = new_name[:-4]
        
    if new_name == current_sample_name:
        messagebox.showwarning("Invalid Input", "New filename must be different from current filename.")
        return
    
    # Capture values to prevent race conditions
    old_sample_name = current_sample_name
    new_sample_name = new_name
    original_df = loaded_df.copy()
    
    def rename_worker():
        backup_parquet_key = None
        backup_wav_key = None
        temp_parquet_key = None
        temp_wav_key = None
        
        try:
            # Update button to show progress
            root.after(0, lambda: rename_button.config(text="Renaming files...", state="disabled"))
            
            # Create updated dataframe with old filename in metadata
            updated_df = original_df.copy()
            updated_df.attrs['old_filename'] = old_sample_name
            
            # Prepare S3 client
            dotenv.load_dotenv()
            s3_client = boto3.client(
                "s3",
                endpoint_url=os.getenv("MINIO_ENDPOINT"),
                aws_access_key_id=os.getenv("MINIO_ACCESS_KEY"),
                aws_secret_access_key=os.getenv("MINIO_SECRET_KEY")
            )
            
            # Define file names
            old_parquet_key = f"{old_sample_name}.parquet"
            old_wav_key = f"{old_sample_name}.wav"
            new_parquet_key = f"{new_sample_name}.parquet"
            new_wav_key = f"{new_sample_name}.wav"
            
            backup_parquet_key = f"{old_sample_name}_backup_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.parquet"
            backup_wav_key = f"{old_sample_name}_backup_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.wav"
            temp_parquet_key = f"{new_sample_name}_temp_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.parquet"
            temp_wav_key = f"{new_sample_name}_temp_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.wav"
            
            # Step 1: Verify original files exist
            try:
                s3_client.head_object(Bucket=BUCKET_NAME, Key=old_parquet_key)
                parquet_exists = True
            except Exception:
                raise Exception(f"Original parquet file {old_parquet_key} not found in S3")
            
            try:
                s3_client.head_object(Bucket=BUCKET_NAME, Key=old_wav_key)
                wav_exists = True
            except Exception:
                wav_exists = False
                print(f"Warning: WAV file {old_wav_key} not found, will only rename parquet file")
            
            # Step 2: Check if target files already exist
            try:
                s3_client.head_object(Bucket=BUCKET_NAME, Key=new_parquet_key)
                raise Exception(f"Target file {new_parquet_key} already exists")
            except s3_client.exceptions.NoSuchKey:
                pass  # Good, target doesn't exist
            
            if wav_exists:
                try:
                    s3_client.head_object(Bucket=BUCKET_NAME, Key=new_wav_key)
                    raise Exception(f"Target file {new_wav_key} already exists")
                except s3_client.exceptions.NoSuchKey:
                    pass  # Good, target doesn't exist
            
            # Step 3: Create updated parquet file with old_filename metadata and upload to temp location
            root.after(0, lambda: rename_button.config(text="Creating updated parquet..."))
            parquet_buffer = io.BytesIO()
            updated_df.to_parquet(parquet_buffer, index=False)
            buffer_size = parquet_buffer.tell()
            parquet_buffer.seek(0)
            
            s3_client.upload_fileobj(parquet_buffer, BUCKET_NAME, temp_parquet_key)
            
            # Verify temp parquet upload
            temp_parquet_obj = s3_client.head_object(Bucket=BUCKET_NAME, Key=temp_parquet_key)
            if temp_parquet_obj['ContentLength'] != buffer_size:
                raise Exception("Temporary parquet file upload verification failed - size mismatch")
            
            # Step 4: Copy WAV file to temp location if it exists
            if wav_exists:
                root.after(0, lambda: rename_button.config(text="Copying WAV file..."))
                s3_client.copy_object(
                    Bucket=BUCKET_NAME,
                    CopySource={'Bucket': BUCKET_NAME, 'Key': old_wav_key},
                    Key=temp_wav_key
                )
                
                # Verify temp wav copy
                s3_client.head_object(Bucket=BUCKET_NAME, Key=temp_wav_key)
            
            # Step 5: Create backups of original files
            root.after(0, lambda: rename_button.config(text="Creating backups..."))
            s3_client.copy_object(
                Bucket=BUCKET_NAME,
                CopySource={'Bucket': BUCKET_NAME, 'Key': old_parquet_key},
                Key=backup_parquet_key
            )
            
            if wav_exists:
                s3_client.copy_object(
                    Bucket=BUCKET_NAME,
                    CopySource={'Bucket': BUCKET_NAME, 'Key': old_wav_key},
                    Key=backup_wav_key
                )
            
            # Verify backups
            s3_client.head_object(Bucket=BUCKET_NAME, Key=backup_parquet_key)
            if wav_exists:
                s3_client.head_object(Bucket=BUCKET_NAME, Key=backup_wav_key)
            
            # Step 6: Move temp files to final locations
            root.after(0, lambda: rename_button.config(text="Finalizing rename..."))
            s3_client.copy_object(
                Bucket=BUCKET_NAME,
                CopySource={'Bucket': BUCKET_NAME, 'Key': temp_parquet_key},
                Key=new_parquet_key
            )
            
            if wav_exists:
                s3_client.copy_object(
                    Bucket=BUCKET_NAME,
                    CopySource={'Bucket': BUCKET_NAME, 'Key': temp_wav_key},
                    Key=new_wav_key
                )
            
            # Verify final files
            final_parquet_obj = s3_client.head_object(Bucket=BUCKET_NAME, Key=new_parquet_key)
            if final_parquet_obj['ContentLength'] != buffer_size:
                raise Exception("Final parquet file verification failed - size mismatch")
            
            if wav_exists:
                s3_client.head_object(Bucket=BUCKET_NAME, Key=new_wav_key)
            
            # Step 7: Delete original files only after successful verification
            root.after(0, lambda: rename_button.config(text="Cleaning up..."))
            s3_client.delete_object(Bucket=BUCKET_NAME, Key=old_parquet_key)
            if wav_exists:
                s3_client.delete_object(Bucket=BUCKET_NAME, Key=old_wav_key)
            
            # Step 8: Clean up temporary and backup files
            try:
                s3_client.delete_object(Bucket=BUCKET_NAME, Key=temp_parquet_key)
                if wav_exists:
                    s3_client.delete_object(Bucket=BUCKET_NAME, Key=temp_wav_key)
                s3_client.delete_object(Bucket=BUCKET_NAME, Key=backup_parquet_key)
                if wav_exists:
                    s3_client.delete_object(Bucket=BUCKET_NAME, Key=backup_wav_key)
            except Exception as cleanup_error:
                print(f"Warning: Cleanup failed but rename was successful: {cleanup_error}")
            
            # Success - update UI on main thread
            def success_update():
                global loaded_df, current_sample_name
                loaded_df = updated_df
                current_sample_name = new_sample_name
                
                # Update metadata display
                metadata_text.delete("1.0", tk.END)
                for k, v in updated_df.attrs.items():
                    metadata_text.insert(tk.END, f"{k}: {v}\n")
                    
                # Clear input field
                rename_entry.delete(0, tk.END)
                
                # Update dropdown with new filename
                dropdown_menu['values'] = list_s3_files()
                dropdown_var.set(new_sample_name)
                
                # Restore button
                rename_button.config(text="Rename File", state="normal")
                
                messagebox.showinfo("Success", f"File renamed from '{old_sample_name}' to '{new_sample_name}' successfully.\nOld filename stored in metadata.")
            
            root.after(0, success_update)
            
        except Exception as e:
            # Error handling with attempted cleanup and rollback
            error_msg = str(e)
            
            # Try to clean up any temporary files
            cleanup_files = [temp_parquet_key, temp_wav_key, backup_parquet_key, backup_wav_key]
            for cleanup_file in cleanup_files:
                if cleanup_file:
                    try:
                        s3_client.delete_object(Bucket=BUCKET_NAME, Key=cleanup_file)
                    except:
                        pass  # Ignore cleanup errors during error handling
            
            def error_update():
                rename_button.config(text="Rename File", state="normal")
                messagebox.showerror("Rename Error", f"Failed to rename file safely: {error_msg}")
            
            root.after(0, error_update)
    
    # Start the safe rename operation in background thread
    threading.Thread(target=rename_worker, daemon=True).start()

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
dropdown_label.pack(anchor="e", pady=(10, 0))
dropdown_var = tk.StringVar()
dropdown_menu = ttk.Combobox(control_frame, textvariable=dropdown_var, values=list_s3_files())
dropdown_menu.pack(anchor="e")
load_button = tk.Button(control_frame, text="Load Sample", command=load_sample)
load_button.pack(anchor="e")


metadata_text = tk.Text(control_frame, height=10, width=40)
metadata_text.pack(anchor="e")

# Add metadata input section
add_metadata_label = tk.Label(control_frame, text="Add New Metadata:")
add_metadata_label.pack(anchor="e", pady=(10, 0))

# Key-value input for new metadata
metadata_key_frame = tk.Frame(control_frame)
metadata_key_frame.pack(anchor="e", fill=tk.X, pady=2)
tk.Label(metadata_key_frame, text="Key:").pack(side=tk.LEFT)
metadata_key_entry = tk.Entry(metadata_key_frame, width=15)
metadata_key_entry.pack(side=tk.LEFT, padx=(5, 0))

metadata_value_frame = tk.Frame(control_frame)
metadata_value_frame.pack(anchor="e", fill=tk.X, pady=2)
tk.Label(metadata_value_frame, text="Value:").pack(side=tk.LEFT)
metadata_value_entry = tk.Entry(metadata_value_frame, width=15)
metadata_value_entry.pack(side=tk.LEFT, padx=(5, 0))

save_metadata_button = tk.Button(control_frame, text="Save Metadata to S3", command=lambda: save_metadata_to_s3())
save_metadata_button.pack(anchor="e", pady=(5, 0))

# Add rename file section
rename_label = tk.Label(control_frame, text="Rename File:")
rename_label.pack(anchor="e", pady=(10, 0))

rename_frame = tk.Frame(control_frame)
rename_frame.pack(anchor="e", fill=tk.X, pady=2)
tk.Label(rename_frame, text="New Name:").pack(side=tk.LEFT)
rename_entry = tk.Entry(rename_frame, width=15)
rename_entry.pack(side=tk.LEFT, padx=(5, 0))

rename_button = tk.Button(control_frame, text="Rename File", command=lambda: rename_file_in_s3())
rename_button.pack(anchor="e", pady=(5, 0))

fig, ax1 = plt.subplots()
ax2 = ax1.twinx()
canvas = FigureCanvasTkAgg(fig, master=root)
canvas.get_tk_widget().pack(side=tk.LEFT, expand=True, fill=tk.BOTH)

root.protocol("WM_DELETE_WINDOW", on_closing)
root.mainloop()
