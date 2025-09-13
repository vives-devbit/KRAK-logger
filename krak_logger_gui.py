from HelpFunctions.lanxi import LanXI
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
import scipy.io.wavfile as wav
import tkinter as tk
from tkinter import messagebox, ttk, filedialog
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
import threading
import boto3
import dotenv
import datetime
import sounddevice as sd
import os
import io
import subprocess
from minio import Minio

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
excel_metadata_df = None  # For storing Excel metadata
excel_file_path = None  # For storing current Excel file path
loaded_excel_metadata = {}  # For storing currently loaded metadata from Excel
sort_by = "time"  # Default sort by time
sort_order = "desc"  # Default sort newest first

# IP of Lan-XI
dotenv.load_dotenv()
ip = os.getenv("BKDAQ_IP")
Lanxi = LanXI(ip)
Lanxi.setup_stream()
SAMPLE_RATE = Lanxi.sample_rate
NUM_SAMPLES = SAMPLE_RATE * DURATION

def select_excel_file():
    global excel_metadata_df, excel_file_path
    file_path = filedialog.askopenfilename(
        title="Select Excel Metadata File",
        filetypes=[("Excel files", "*.xlsx *.xls"), ("All files", "*.*")]
    )
    if file_path:
        try:
            excel_metadata_df = pd.read_excel(file_path)
            excel_file_path = file_path
            excel_file_label.config(text=f"Excel file: {os.path.basename(file_path)}")
            messagebox.showinfo("Success", f"Excel file loaded successfully.\nColumns: {list(excel_metadata_df.columns)}")
            # Clear any previously loaded metadata
            clear_excel_metadata_display()
        except Exception as e:
            messagebox.showerror("Error", f"Failed to load Excel file: {str(e)}")
            excel_metadata_df = None
            excel_file_path = None
            excel_file_label.config(text="No Excel file selected")

def load_metadata_from_excel():
    global loaded_excel_metadata
    if excel_metadata_df is None:
        messagebox.showwarning("No Excel File", "Please select an Excel file first.")
        return

    ref_number = ref_number_entry.get().strip()
    if not ref_number:
        messagebox.showwarning("Invalid Input", "Please enter a reference number.")
        return

    try:
        # Convert ref_number to appropriate type for comparison
        try:
            ref_num = int(ref_number)
        except ValueError:
            ref_num = ref_number

        # Find the row with matching reference number (first column)
        ref_col = excel_metadata_df.columns[0]
        matching_rows = excel_metadata_df[excel_metadata_df[ref_col] == ref_num]

        if matching_rows.empty:
            # Try string comparison if int comparison failed
            matching_rows = excel_metadata_df[excel_metadata_df[ref_col].astype(str) == str(ref_number)]

        if matching_rows.empty:
            messagebox.showwarning("Not Found", f"Reference number '{ref_number}' not found in Excel file.")
            return

        # Get the first matching row
        row = matching_rows.iloc[0]

        # Load metadata from all columns except the first (reference) column
        loaded_excel_metadata.clear()
        for col in excel_metadata_df.columns[1:]:
            value = row[col]
            if pd.notna(value):  # Only add non-empty values
                loaded_excel_metadata[col] = str(value)

        # Add automatic timestamp and date
        current_time = datetime.datetime.now()
        loaded_excel_metadata['Date'] = current_time.strftime("%Y-%m-%d")
        loaded_excel_metadata['Timestamp'] = current_time.strftime("%Y-%m-%d %H:%M:%S")

        # Display loaded metadata for verification
        display_excel_metadata()

    except Exception as e:
        messagebox.showerror("Error", f"Failed to load metadata: {str(e)}")

def display_excel_metadata():
    excel_metadata_text.delete("1.0", tk.END)
    for key, value in loaded_excel_metadata.items():
        excel_metadata_text.insert(tk.END, f"{key}: {value}\n")

def clear_excel_metadata_display():
    global loaded_excel_metadata
    loaded_excel_metadata.clear()
    excel_metadata_text.delete("1.0", tk.END)
    ref_number_entry.delete(0, tk.END)
    # Clear additional metadata entries
    for widget in additional_metadata_frame.winfo_children():
        widget.destroy()

def add_additional_metadata_field():
    frame = tk.Frame(additional_metadata_frame)
    frame.pack(fill=tk.X, padx=5, pady=2)

    tk.Label(frame, text="Key:").pack(side=tk.LEFT)
    key_entry = tk.Entry(frame, width=10)
    key_entry.pack(side=tk.LEFT, padx=(2, 5))

    tk.Label(frame, text="Value:").pack(side=tk.LEFT)
    value_entry = tk.Entry(frame, width=15)
    value_entry.pack(side=tk.LEFT, padx=(2, 5))

    remove_btn = tk.Button(frame, text="Remove", command=lambda: frame.destroy())
    remove_btn.pack(side=tk.LEFT, padx=(5, 0))






def get_all_metadata():
    """Combine Excel metadata with additional metadata fields"""
    all_metadata = loaded_excel_metadata.copy()

    # Add additional metadata from manual entry fields
    for frame in additional_metadata_frame.winfo_children():
        entries = [w for w in frame.winfo_children() if isinstance(w, tk.Entry)]
        if len(entries) >= 2:
            key = entries[0].get().strip()
            value = entries[1].get().strip()
            if key and value:
                all_metadata[key] = value

    return all_metadata

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

    # Check if we have Excel metadata with ID field
    if loaded_excel_metadata and 'ID' in loaded_excel_metadata:
        id_value = loaded_excel_metadata['ID']
        base_name = f"{timestamp}-({id_value})"
    else:
        # Fallback to timestamp if no Excel metadata
        base_name = f"recording_{timestamp}"

    wav_filename = f"{base_name}.wav"
    parquet_filename = f"{base_name}.parquet"
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
        # Get all metadata (Excel + additional)
        metadata = get_all_metadata()

        # Add traditional metadata fields
        metadata.update({
            "Sample Rate (Hz)": Lanxi.sample_rate,
        })

        # Add parameter entries
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

        # Apply metadata as tags to parquet file
        metadata = get_all_metadata()
        if metadata:
            # Apply tags to parquet file only
            parquet_success = sync_metadata_to_tags(os.path.basename(OUTPUT_PARQUET_FILE), metadata)

            if parquet_success:
                print("Successfully applied tags to parquet file")
            else:
                print("Failed to apply tags to parquet file")

        messagebox.showinfo("Upload Complete", f"Files uploaded to MinIO")

        # Get the uploaded file base name (without extension)
        uploaded_file_base = os.path.splitext(os.path.basename(OUTPUT_PARQUET_FILE))[0]

        # Refresh file list after upload
        refresh_file_list()

        # Auto-select the uploaded file
        select_uploaded_file(uploaded_file_base)

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



def list_s3_files(sort_by="name", sort_order="asc"):
    """List S3 files with sorting options

    Args:
        sort_by: "name" or "time"
        sort_order: "asc" (1-9) or "desc" (9-1)
    """
    dotenv.load_dotenv()
    s3_client = boto3.client(
        "s3",
        endpoint_url=os.getenv("MINIO_ENDPOINT"),
        aws_access_key_id=os.getenv("MINIO_ACCESS_KEY"),
        aws_secret_access_key=os.getenv("MINIO_SECRET_KEY")
    )
    response = s3_client.list_objects_v2(Bucket=BUCKET_NAME)

    if 'Contents' not in response:
        return []

    # Create a dictionary to store file info
    files_info = {}
    for obj in response['Contents']:
        if obj['Key'].endswith('.parquet'):  # Only process parquet files
            base_name = os.path.splitext(os.path.basename(obj['Key']))[0]
            if base_name not in files_info:
                files_info[base_name] = {
                    'name': base_name,
                    'last_modified': obj['LastModified']
                }

    # Sort based on criteria
    if sort_by == "time":
        sorted_files = sorted(files_info.values(),
                            key=lambda x: x['last_modified'],
                            reverse=(sort_order == "desc"))
    else:  # sort by name
        sorted_files = sorted(files_info.values(),
                            key=lambda x: x['name'],
                            reverse=(sort_order == "desc"))

    return [file_info['name'] for file_info in sorted_files]


def refresh_file_list():
    """Refresh the file listbox with current S3 files"""
    global sort_by, sort_order
    file_listbox.delete(0, tk.END)
    s3_files = list_s3_files(sort_by, sort_order)
    for file in s3_files:
        file_listbox.insert(tk.END, file)

def change_sort_criteria():
    """Handle sort criteria changes and refresh the list"""
    global sort_by, sort_order
    sort_by = sort_by_var.get()
    sort_order = sort_order_var.get()
    refresh_file_list()

def select_uploaded_file(file_base_name):
    """Select and highlight the uploaded file in the listbox"""
    for i in range(file_listbox.size()):
        if file_listbox.get(i) == file_base_name:
            file_listbox.selection_clear(0, tk.END)
            file_listbox.selection_set(i)
            file_listbox.see(i)  # Scroll to make it visible
            dropdown_var.set(file_base_name)
            break




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






def create_minio_client():
    """Create and return MinIO client"""
    dotenv.load_dotenv()
    endpoint = os.getenv("MINIO_ENDPOINT")
    # Remove http:// or https:// from endpoint for MinIO client
    if endpoint.startswith("http://"):
        endpoint = endpoint[7:]
        secure = False
    elif endpoint.startswith("https://"):
        endpoint = endpoint[8:]
        secure = True
    else:
        secure = False

    return Minio(
        endpoint,
        access_key=os.getenv("MINIO_ACCESS_KEY"),
        secret_key=os.getenv("MINIO_SECRET_KEY"),
        secure=secure
    )

def sync_metadata_to_tags(object_name, metadata_dict):
    """Sync metadata dictionary to MinIO object tags"""
    try:
        minio_client = create_minio_client()

        # Convert metadata to tags format (MinIO tags are key-value pairs)
        tags = {}
        for key, value in metadata_dict.items():
            # MinIO tag keys and values must be strings, and have length restrictions
            tag_key = str(key).replace(' ', '_')[:128]  # Replace spaces and limit length
            tag_value = str(value)[:256]  # Limit tag value length
            tags[tag_key] = tag_value

        # Apply tags to the object
        minio_client.set_object_tags(BUCKET_NAME, object_name, tags)
        print(f"Applied {len(tags)} tags to {object_name}")
        return True
    except Exception as e:
        print(f"Failed to apply tags to {object_name}: {str(e)}")
        return False

def launch_editor():
    """Launch the krak_editor_gui.py application"""
    try:
        subprocess.Popen(["python", "krak_editor_gui.py"])
    except Exception as e:
        messagebox.showerror("Launch Error", f"Failed to launch editor: {str(e)}")

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
                
                # Update file list with new filename
                refresh_file_list()
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


parameter_frame = tk.Frame(control_frame)
parameter_frame.pack(anchor="e")

# Excel Metadata Section
excel_section = tk.LabelFrame(control_frame, text="Excel Metadata", padx=5, pady=5)
excel_section.pack(anchor="e", fill=tk.X, pady=(10, 5))

# Excel file selection
excel_file_frame = tk.Frame(excel_section)
excel_file_frame.pack(fill=tk.X, pady=2)
select_excel_btn = tk.Button(excel_file_frame, text="Select Excel File", command=select_excel_file)
select_excel_btn.pack(side=tk.LEFT)
excel_file_label = tk.Label(excel_file_frame, text="No Excel file selected", fg="gray")
excel_file_label.pack(side=tk.LEFT, padx=(10, 0))

# Reference number input
ref_frame = tk.Frame(excel_section)
ref_frame.pack(fill=tk.X, pady=2)
tk.Label(ref_frame, text="Reference Number:").pack(side=tk.LEFT)
ref_number_entry = tk.Entry(ref_frame, width=10)
ref_number_entry.pack(side=tk.LEFT, padx=(5, 5))
load_metadata_btn = tk.Button(ref_frame, text="Load Metadata", command=load_metadata_from_excel)
load_metadata_btn.pack(side=tk.LEFT)

# Excel metadata display
excel_metadata_text = tk.Text(excel_section, height=6, width=40)
excel_metadata_text.pack(fill=tk.X, pady=2)

# Additional metadata section
additional_section = tk.LabelFrame(control_frame, text="Additional Metadata", padx=5, pady=5)
additional_section.pack(anchor="e", fill=tk.X, pady=5)

additional_metadata_frame = tk.Frame(additional_section)
additional_metadata_frame.pack(fill=tk.X)

add_field_btn = tk.Button(additional_section, text="Add Metadata Field", command=add_additional_metadata_field)
add_field_btn.pack(pady=2)

clear_metadata_btn = tk.Button(additional_section, text="Clear All Metadata", command=clear_excel_metadata_display)
clear_metadata_btn.pack(pady=2)


duration_label = tk.Label(control_frame, text="Duration (s):")
duration_label.pack(anchor="e")
duration_entry = tk.Entry(control_frame)
duration_entry.insert(0, "1")
duration_entry.pack(anchor="e")

# Recording section
recording_section = tk.LabelFrame(control_frame, text="Recording", padx=5, pady=5)
recording_section.pack(anchor="e", fill=tk.X, pady=(10, 5))

record_button = tk.Button(recording_section, text="Start Recording", command=start_recording)
record_button.pack(anchor="e")

upload_button = tk.Button(recording_section, text="Upload to MinIO", command=upload_to_minio)
upload_button.pack(anchor="e")

play_button = tk.Button(recording_section, text="Play Audio", command=play_audio)
play_button.pack(anchor="e")

# File list display for S3 files
file_section = tk.LabelFrame(control_frame, text="File Management", padx=5, pady=5)
file_section.pack(anchor="e", fill=tk.BOTH, expand=True, pady=(10, 5))

dropdown_label = tk.Label(file_section, text="Select sample from S3:")
dropdown_label.pack(anchor="e", pady=(5, 0))

# Sorting options frame
sort_frame = tk.Frame(file_section)
sort_frame.pack(anchor="e", pady=(5, 0))

tk.Label(sort_frame, text="Sort by:").pack(side=tk.LEFT, padx=(0, 5))
sort_by_var = tk.StringVar(value=sort_by)
sort_by_combo = ttk.Combobox(sort_frame, textvariable=sort_by_var, values=["name", "time"],
                             width=8, state="readonly")
sort_by_combo.pack(side=tk.LEFT, padx=(0, 10))
sort_by_combo.bind('<<ComboboxSelected>>', lambda e: change_sort_criteria())

tk.Label(sort_frame, text="Order:").pack(side=tk.LEFT, padx=(0, 5))
sort_order_var = tk.StringVar(value=sort_order)
sort_order_combo = ttk.Combobox(sort_frame, textvariable=sort_order_var,
                                values=["asc", "desc"], width=6, state="readonly")
sort_order_combo.pack(side=tk.LEFT)
sort_order_combo.bind('<<ComboboxSelected>>', lambda e: change_sort_criteria())

# Create a frame for the file list with scrollbar
file_list_frame = tk.Frame(file_section)
file_list_frame.pack(anchor="e", fill=tk.BOTH, pady=5)

# Add scrollbar for the listbox
scrollbar = tk.Scrollbar(file_list_frame)
scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

# Create a larger listbox for better filename visibility
dropdown_var = tk.StringVar()
file_listbox = tk.Listbox(file_list_frame, height=8, width=45, yscrollcommand=scrollbar.set, font=("Courier", 9))
file_listbox.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
scrollbar.config(command=file_listbox.yview)

# Populate the listbox with initial sorting
refresh_file_list()

# Bind selection event
def on_file_select(event):
    selection = file_listbox.curselection()
    if selection:
        selected_file = file_listbox.get(selection[0])
        dropdown_var.set(selected_file)

file_listbox.bind('<<ListboxSelect>>', on_file_select)

load_button = tk.Button(file_section, text="Load Sample", command=load_sample)
load_button.pack(anchor="e")

metadata_text = tk.Text(file_section, height=8, width=40)
metadata_text.pack(anchor="e")

editor_button = tk.Button(file_section, text="Editor", command=launch_editor)
editor_button.pack(anchor="e", pady=(10, 0))


fig, ax1 = plt.subplots()
ax2 = ax1.twinx()
canvas = FigureCanvasTkAgg(fig, master=root)
canvas.get_tk_widget().pack(side=tk.LEFT, expand=True, fill=tk.BOTH)

root.protocol("WM_DELETE_WINDOW", on_closing)
root.mainloop()
