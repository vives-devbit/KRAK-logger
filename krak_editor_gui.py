import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
import tkinter as tk
from tkinter import messagebox, ttk, simpledialog
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
import threading
import sounddevice as sd
import scipy.io.wavfile as wav
import dotenv
import datetime
import os
import io
import json
from datetime import datetime as dt
import re
from minio import Minio
from minio.error import S3Error
from minio.commonconfig import CopySource

BUCKET_NAME = "krak"
INDEX_FILE_NAME = "search_index.json"
loaded_df = None
current_sample_name = None
search_index = {}

# Playback tracking variables
playback_line = None
playback_active = False
playback_start_time = None
audio_duration = None
playback_timer = None

# Delay learning system
learned_delays = {}  # Dictionary to store learned delays per file
current_file_delay = None  # Current file's learned delay
delay_learning_active = False  # Whether we're learning delay for current file


def create_minio_client():
    """Create and return MinIO client"""
    dotenv.load_dotenv()
    endpoint = os.getenv("MINIO_ENDPOINT")

    # Parse endpoint URL properly
    secure = False
    if endpoint.startswith("https://"):
        endpoint = endpoint[8:]
        secure = True
    elif endpoint.startswith("http://"):
        endpoint = endpoint[7:]
        secure = False

    return Minio(
        endpoint,
        access_key=os.getenv("MINIO_ACCESS_KEY"),
        secret_key=os.getenv("MINIO_SECRET_KEY"),
        secure=secure,
        region=os.getenv("MINIO_REGION", "us-east-1")  # Default region
    )


def list_s3_files():
    try:
        minio_client = create_minio_client()
        objects = minio_client.list_objects(BUCKET_NAME)
        names = set()

        for obj in objects:
            if obj.object_name.endswith('.parquet'):
                base = os.path.splitext(os.path.basename(obj.object_name))[0]
                names.add(base)
        return sorted(names)
    except S3Error as e:
        print(f"MinIO Error listing files: {e}")
        return []
    except Exception as e:
        print(f"Error listing files: {e}")
        return []

def load_search_index():
    global search_index
    try:
        search_progress_var.set("Loading search index from server...")
        root.update_idletasks()

        minio_client = create_minio_client()

        # Download the search index
        response = minio_client.get_object(BUCKET_NAME, INDEX_FILE_NAME)
        index_data = json.loads(response.read().decode('utf-8'))

        # Convert to the format expected by the GUI (base_name -> metadata)
        search_index = {}
        for filename, metadata in index_data.get('files', {}).items():
            base_name = os.path.splitext(os.path.basename(filename))[0]
            # Remove _file_info for GUI compatibility, keep original metadata
            clean_metadata = {k: v for k, v in metadata.items() if k != '_file_info'}
            search_index[base_name] = clean_metadata

        index_info = index_data.get('index_info', {})
        created_at = index_info.get('created_at', 'Unknown')
        total_files = len(search_index)

        search_progress_var.set(f"Loaded index with {total_files} files (created: {created_at[:19]})")

        # Update dropdown values with available keys
        update_search_key_dropdowns()

        return True

    except S3Error as e:
        if "NoSuchKey" in str(e):
            messagebox.showerror("Index Error",
                f"Search index not found on server.\n\n"
                f"Please run 'python parquet_search_indexer.py' first to create the index.")
        else:
            messagebox.showerror("Index Error", f"MinIO Error loading search index: {e}")
        search_progress_var.set("Index loading failed")
        return False
    except json.JSONDecodeError as e:
        messagebox.showerror("Index Error", f"Invalid search index format: {e}")
        search_progress_var.set("Index format error")
        return False
    except Exception as e:
        messagebox.showerror("Index Error", f"Failed to load search index: {e}")
        search_progress_var.set("Index loading failed")
        return False


def update_search_index_on_server(filename, updated_metadata):
    """
    Update the search index on the server with new/updated metadata for a file

    Args:
        filename: Base filename (without .parquet extension)
        updated_metadata: Dictionary of metadata to update in the index

    Returns:
        bool: True if successful, False otherwise
    """
    try:
        minio_client = create_minio_client()
        parquet_filename = f"{filename}.parquet"

        # Download existing index
        try:
            response = minio_client.get_object(BUCKET_NAME, INDEX_FILE_NAME)
            index_data = json.loads(response.read().decode('utf-8'))
        except S3Error as e:
            if "NoSuchKey" in str(e):
                # Create new index if it doesn't exist
                index_data = {
                    "index_info": {
                        "created_at": datetime.datetime.now().isoformat(),
                        "total_files": 0,
                        "index_version": "1.0"
                    },
                    "files": {}
                }
            else:
                raise e

        # Get file info
        try:
            stat = minio_client.stat_object(BUCKET_NAME, parquet_filename)
            file_info = {
                'filename': parquet_filename,
                'size_bytes': stat.size,
                'last_modified': stat.last_modified.isoformat() if stat.last_modified else None,
                'rows': 'Unknown',  # We don't have access to the DataFrame here
                'columns': 'Unknown',
                'column_names': 'Unknown'
            }
        except:
            # If we can't get file stats, use basic info
            file_info = {
                'filename': parquet_filename,
                'size_bytes': 'Unknown',
                'last_modified': datetime.datetime.now().isoformat(),
                'rows': 'Unknown',
                'columns': 'Unknown',
                'column_names': 'Unknown'
            }

        # Update the index entry
        if parquet_filename in index_data['files']:
            # Update existing entry - preserve file info but update metadata
            existing_entry = index_data['files'][parquet_filename]
            existing_file_info = existing_entry.get('_file_info', file_info)

            # Merge updated metadata with existing metadata
            updated_entry = dict(updated_metadata)
            updated_entry['_file_info'] = existing_file_info
            index_data['files'][parquet_filename] = updated_entry
        else:
            # Create new entry
            file_metadata = dict(updated_metadata)
            file_metadata['_file_info'] = file_info
            index_data['files'][parquet_filename] = file_metadata

        # Update index info
        index_data['index_info']['total_files'] = len(index_data['files'])
        index_data['index_info']['last_updated'] = datetime.datetime.now().isoformat()

        # Upload updated index
        json_bytes = json.dumps(index_data, indent=2, ensure_ascii=False).encode('utf-8')
        json_buffer = io.BytesIO(json_bytes)

        minio_client.put_object(
            BUCKET_NAME,
            INDEX_FILE_NAME,
            json_buffer,
            len(json_bytes),
            content_type='application/json'
        )

        print(f"Successfully updated search index for {parquet_filename}")

        # Show success dialog
        messagebox.showinfo("Search Index Updated",
                           f"Successfully updated search index for {filename}!\n\n"
                           f"Total files in index: {index_data['index_info']['total_files']}\n\n"
                           f"The metadata changes are now searchable from all locations.")
        return True

    except Exception as e:
        error_msg = f"Failed to update search index: {str(e)}"
        print(error_msg)

        # Show error dialog but don't fail the operation
        messagebox.showwarning("Search Index Warning",
                              f"Metadata was saved successfully to {filename}.parquet,\n"
                              f"but the search index could not be updated.\n\n"
                              f"Error: {str(e)}\n\n"
                              f"The changes won't be searchable until the index is rebuilt.")
        return False


def get_available_search_keys():
    """
    Get all unique metadata keys from the search index

    Returns:
        list: Sorted list of unique metadata keys (excluding specified keys)
    """
    # Keys to exclude from dropdown lists
    excluded_keys = {
        "Distance (mm)", "H", "Measurement Parameters", "Probe", "Sample",
        "Sample Rate (Hz)", "Test_Author", "Test_Product", "Test_metadata",
        "Timestamp", "snelheid", "par1", "par2", "probe", "preload", "exp nr."
    }

    keys = set()

    # Check if we have a search index loaded
    if search_index:
        for metadata in search_index.values():
            for key in metadata.keys():
                if key != '_file_info' and key not in excluded_keys:  # Exclude internal file info and specified keys
                    keys.add(key)

    return sorted(list(keys))


def get_current_file_metadata_keys():
    """
    Get all metadata keys from the currently loaded file

    Returns:
        list: Sorted list of metadata keys from the current file
    """
    if loaded_df is None:
        return []

    return sorted(list(loaded_df.attrs.keys()))


def update_metadata_key_dropdowns():
    """
    Update the dropdown values for update metadata key combobox with keys from the current file
    """
    try:
        available_keys = get_current_file_metadata_keys()

        # Update only the update metadata key combobox
        update_metadata_key_entry['values'] = available_keys

        print(f"Updated update metadata key dropdown with {len(available_keys)} keys from current file: {available_keys}")

    except Exception as e:
        print(f"Error updating metadata key dropdowns: {e}")


def update_search_key_dropdowns():
    """
    Update the dropdown values for search key comboboxes with available keys from the index
    """
    try:
        available_keys = get_available_search_keys()

        # Update all search key comboboxes
        search_key1_entry['values'] = available_keys
        search_key2_entry['values'] = available_keys
        search_key3_entry['values'] = available_keys
        search_key4_entry['values'] = available_keys

        print(f"Updated search key dropdowns with {len(available_keys)} keys: {available_keys}")

    except Exception as e:
        print(f"Error updating search key dropdowns: {e}")


def refresh_file_list(filtered_files=None):
    file_listbox.delete(0, tk.END)
    if filtered_files is not None:
        files_to_show = filtered_files
    else:
        files_to_show = list_s3_files()

    for file in files_to_show:
        file_listbox.insert(tk.END, file)

def filter_files_by_name():
    """Filter files by filename search term"""
    search_term = filename_search_entry.get().strip().lower()

    if not search_term:
        refresh_file_list()
        return

    # Get all files from server
    all_files = list_s3_files()

    # Filter files that contain the search term
    filtered_files = [file for file in all_files if search_term in file.lower()]

    # Update the file list with filtered results
    refresh_file_list(filtered_files)

    # Update status
    status_msg = f"Found {len(filtered_files)} files containing '{search_term}'"
    if hasattr(search_progress_var, 'set'):
        search_progress_var.set(status_msg)
    print(status_msg)

def clear_filename_search():
    """Clear the filename search and show all files"""
    filename_search_entry.delete(0, tk.END)
    refresh_file_list()
    if hasattr(search_progress_var, 'set'):
        search_progress_var.set("Showing all files")

def load_sample():
    try:
        selection = file_listbox.curselection()
        if not selection:
            messagebox.showwarning("No Selection", "Please select a file from the list.")
            return

        base_name = file_listbox.get(selection[0])
        parquet_key = f"{base_name}.parquet"

        minio_client = create_minio_client()
        response = minio_client.get_object(BUCKET_NAME, parquet_key)
        df = pd.read_parquet(io.BytesIO(response.read()))

        # Update plot with loaded data
        update_plot(df["Time (s)"], [df["AI0 (V)"], df["AI1 (V)"]], title=base_name)

        metadata_text.delete("1.0", tk.END)
        for key, val in df.attrs.items():
            metadata_text.insert(tk.END, f"{key}: {val}\n")

        global loaded_df, current_sample_name
        loaded_df = df
        current_sample_name = base_name

        current_file_var.set(f"Loaded: {base_name}")
        playback_status_var.set(f"Ready to play: {base_name}.wav")
        status_var.set("Ready")

        # Update metadata key dropdowns with keys from the loaded file
        update_metadata_key_dropdowns()
    except S3Error as e:
        messagebox.showerror("Load Error", f"MinIO Error: {e}")
    except Exception as e:
        messagebox.showerror("Load Error", str(e))

def match_date_criteria(metadata_value, search_value):
    """Check if a date/timestamp matches search criteria.
    Supports formats like: YYYY-MM-DD, YYYY-MM, YYYY, or partial matches"""
    try:
        metadata_str = str(metadata_value).lower()
        search_str = search_value.lower()
        
        # Direct substring match for partial dates
        if search_str in metadata_str:
            return True
            
        # Try to parse as dates for more precise matching
        # Extract date patterns from metadata
        date_patterns = [
            r'\d{4}-\d{2}-\d{2}',  # YYYY-MM-DD
            r'\d{4}-\d{2}',       # YYYY-MM
            r'\d{4}'              # YYYY
        ]
        
        for pattern in date_patterns:
            matches = re.findall(pattern, metadata_str)
            if matches and any(search_str in match for match in matches):
                return True
                
        return False
    except:
        # Fallback to string comparison
        return search_value.lower() in str(metadata_value).lower()

def search_metadata():
    try:
        # Load fresh search index from server each time
        search_progress_var.set("Loading latest search index...")
        root.update_idletasks()

        if not load_search_index():
            messagebox.showerror("Search Error", "Failed to load search index from server.")
            search_progress_var.set("Search failed")
            return

        # Validate index is not empty after loading
        if not search_index:
            messagebox.showwarning("No Data", "No files found in search index. Cannot perform search.")
            search_progress_var.set("No data in index")
            return

        # Get search criteria
        key1 = search_key1_entry.get().strip()
        value1 = search_value1_entry.get().strip()
        key2 = search_key2_entry.get().strip()
        value2 = search_value2_entry.get().strip()
        key3 = search_key3_entry.get().strip()
        value3 = search_value3_entry.get().strip()
        key4 = search_key4_entry.get().strip()
        value4 = search_value4_entry.get().strip()

        # Date search criteria
        date_search = date_search_entry.get().strip()

        search_criteria = []
        if key1 and value1:
            search_criteria.append((key1, value1))
        if key2 and value2:
            search_criteria.append((key2, value2))
        if key3 and value3:
            search_criteria.append((key3, value3))
        if key4 and value4:
            search_criteria.append((key4, value4))

        if not search_criteria and not date_search:
            refresh_file_list()
            search_progress_var.set("Showing all files")
            return

        # Perform search with progress indication
        search_progress_var.set("Searching...")
        root.update_idletasks()

        matching_files = []
        total_files = len(search_index)
        processed = 0

        for filename, metadata in search_index.items():
            try:
                matches = True

                # Check regular key-value pairs
                for key, value in search_criteria:
                    if key not in metadata or str(metadata[key]).lower() != value.lower():
                        matches = False
                        break

                # Check date search if specified
                if matches and date_search:
                    date_match = False
                    # Search in common timestamp fields
                    timestamp_fields = ['Timestamp', 'timestamp', 'Date', 'date', 'created', 'Created']
                    for field in timestamp_fields:
                        if field in metadata:
                            if match_date_criteria(metadata[field], date_search):
                                date_match = True
                                break

                    # Also search filename for date patterns
                    if not date_match:
                        if match_date_criteria(filename, date_search):
                            date_match = True

                    if not date_match:
                        matches = False

                if matches:
                    matching_files.append(filename)

                # Update progress occasionally
                processed += 1
                if processed % 50 == 0:  # Update every 50 files
                    search_progress_var.set(f"Searching... {processed}/{total_files}")
                    root.update_idletasks()

            except Exception as e:
                print(f"Error processing file {filename}: {e}")
                continue

        # Update results
        refresh_file_list(sorted(matching_files))
        search_progress_var.set(f"Found {len(matching_files)} matching files")

    except Exception as e:
        messagebox.showerror("Search Error", f"An error occurred during search: {str(e)}")
        search_progress_var.set("Search failed")
        print(f"Search error: {e}")

def clear_search():
    search_key1_entry.set('')
    search_value1_entry.delete(0, tk.END)
    search_key2_entry.set('')
    search_value2_entry.delete(0, tk.END)
    search_key3_entry.set('')
    search_value3_entry.delete(0, tk.END)
    search_key4_entry.set('')
    search_value4_entry.delete(0, tk.END)
    date_search_entry.delete(0, tk.END)
    refresh_file_list()
    search_progress_var.set("Showing all files")

def save_metadata_to_s3():
    global loaded_df, current_sample_name
    
    if loaded_df is None:
        messagebox.showwarning("No Sample Loaded", "Please load a sample first before adding metadata.")
        return
        
    key = metadata_key_entry.get().strip()
    value = metadata_value_entry.get().strip()
    
    if not key or not value:
        messagebox.showwarning("Invalid Input", "Please enter both key and value for metadata.")
        return
    
    metadata_key = key
    metadata_value = value
    sample_name = current_sample_name
    original_df = loaded_df.copy()
    
    def save_worker():
        backup_key = None
        old_key = None
        
        try:
            root.after(0, lambda: save_metadata_button.config(text="Saving data...", state="disabled"))
            
            updated_df = original_df.copy()
            updated_df.attrs[metadata_key] = metadata_value
            
            minio_client = create_minio_client()
            
            original_key = f"{sample_name}.parquet"
            backup_key = f"{sample_name}_backup_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.parquet"
            old_key = f"{sample_name}_old_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.parquet"
            
            try:
                minio_client.stat_object(BUCKET_NAME, original_key)
            except S3Error:
                raise Exception(f"Original file {original_key} not found in MinIO")
            
            root.after(0, lambda: save_metadata_button.config(text="Creating backup..."))
            parquet_buffer = io.BytesIO()
            updated_df.to_parquet(parquet_buffer, index=False)
            buffer_size = parquet_buffer.tell()
            parquet_buffer.seek(0)
            
            minio_client.put_object(BUCKET_NAME, backup_key, parquet_buffer, buffer_size)
            
            backup_obj = minio_client.stat_object(BUCKET_NAME, backup_key)
            if backup_obj.size != buffer_size:
                raise Exception("Backup file upload verification failed - size mismatch")
            
            root.after(0, lambda: save_metadata_button.config(text="Backing up original..."))
            minio_client.copy_object(
                BUCKET_NAME, old_key,
                CopySource(BUCKET_NAME, original_key)
            )
            
            minio_client.stat_object(BUCKET_NAME, old_key)
            
            root.after(0, lambda: save_metadata_button.config(text="Finalizing save..."))
            minio_client.copy_object(
                BUCKET_NAME, original_key,
                CopySource(BUCKET_NAME, backup_key)
            )
            
            final_obj = minio_client.stat_object(BUCKET_NAME, original_key)
            if final_obj.size != buffer_size:
                try:
                    minio_client.copy_object(
                        BUCKET_NAME, original_key,
                        CopySource(BUCKET_NAME, old_key)
                    )
                    raise Exception("Final file verification failed - rolled back to original")
                except Exception as rollback_error:
                    raise Exception(f"Final file verification failed and rollback failed: {rollback_error}")
            
            try:
                minio_client.remove_object(BUCKET_NAME, backup_key)
                minio_client.remove_object(BUCKET_NAME, old_key)
            except Exception as cleanup_error:
                print(f"Warning: Cleanup failed but data was saved successfully: {cleanup_error}")

            def success_update():
                global loaded_df
                loaded_df = updated_df
                metadata_text.delete("1.0", tk.END)
                for k, v in updated_df.attrs.items():
                    metadata_text.insert(tk.END, f"{k}: {v}\n")

                metadata_key_entry.delete(0, tk.END)
                metadata_value_entry.delete(0, tk.END)
                
                save_metadata_button.config(text="Save Metadata to MinIO", state="normal")

                # Update local search index if it exists
                if sample_name in search_index:
                    search_index[sample_name] = dict(updated_df.attrs)

                # Update search index on server
                try:
                    update_search_index_on_server(sample_name, dict(updated_df.attrs))
                except Exception as index_error:
                    print(f"Warning: Search index update failed: {index_error}")

                messagebox.showinfo("Success", f"Metadata '{metadata_key}' added and saved to MinIO safely.")
            
            root.after(0, success_update)
            
        except Exception as e:
            error_msg = str(e)
            
            if backup_key:
                try:
                    minio_client.remove_object(BUCKET_NAME, backup_key)
                except:
                    pass
            
            if old_key:
                try:
                    minio_client.remove_object(BUCKET_NAME, old_key)
                except:
                    pass
            
            def error_update():
                save_metadata_button.config(text="Save Metadata to MinIO", state="normal")
                messagebox.showerror("Save Error", f"Failed to save metadata safely: {error_msg}")
            
            root.after(0, error_update)
    
    threading.Thread(target=save_worker, daemon=True).start()

def update_metadata_in_s3():
    global loaded_df, current_sample_name
    
    if loaded_df is None:
        messagebox.showwarning("No Sample Loaded", "Please load a sample first before updating metadata.")
        return
        
    key = update_metadata_key_entry.get().strip()
    new_value = update_metadata_value_entry.get().strip()
    
    if not key:
        messagebox.showwarning("Invalid Input", "Please enter a key for the metadata to update.")
        return
    
    if not new_value:
        messagebox.showwarning("Invalid Input", "Please enter a new value for the metadata.")
        return
    
    if key not in loaded_df.attrs:
        messagebox.showwarning("Key Not Found", f"Metadata key '{key}' not found in current sample.")
        return
    
    if str(loaded_df.attrs[key]) == new_value:
        messagebox.showwarning("No Change", "New value is the same as current value.")
        return
    
    metadata_key = key
    metadata_value = new_value
    sample_name = current_sample_name
    original_df = loaded_df.copy()
    old_value = str(loaded_df.attrs[key])
    
    def update_worker():
        backup_key = None
        old_key = None
        
        try:
            root.after(0, lambda: update_metadata_button.config(text="Updating data...", state="disabled"))
            
            updated_df = original_df.copy()
            updated_df.attrs[metadata_key] = metadata_value
            
            minio_client = create_minio_client()
            
            original_key = f"{sample_name}.parquet"
            backup_key = f"{sample_name}_backup_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.parquet"
            old_key = f"{sample_name}_old_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.parquet"
            
            try:
                minio_client.stat_object(BUCKET_NAME, original_key)
            except S3Error:
                raise Exception(f"Original file {original_key} not found in MinIO")
            
            root.after(0, lambda: update_metadata_button.config(text="Creating backup..."))
            parquet_buffer = io.BytesIO()
            updated_df.to_parquet(parquet_buffer, index=False)
            buffer_size = parquet_buffer.tell()
            parquet_buffer.seek(0)
            
            minio_client.put_object(BUCKET_NAME, backup_key, parquet_buffer, buffer_size)
            
            backup_obj = minio_client.stat_object(BUCKET_NAME, backup_key)
            if backup_obj.size != buffer_size:
                raise Exception("Backup file upload verification failed - size mismatch")
            
            root.after(0, lambda: update_metadata_button.config(text="Backing up original..."))
            minio_client.copy_object(
                BUCKET_NAME, old_key,
                CopySource(BUCKET_NAME, original_key)
            )
            
            minio_client.stat_object(BUCKET_NAME, old_key)
            
            root.after(0, lambda: update_metadata_button.config(text="Finalizing update..."))
            minio_client.copy_object(
                BUCKET_NAME, original_key,
                CopySource(BUCKET_NAME, backup_key)
            )
            
            final_obj = minio_client.stat_object(BUCKET_NAME, original_key)
            if final_obj.size != buffer_size:
                try:
                    minio_client.copy_object(
                        BUCKET_NAME, original_key,
                        CopySource(BUCKET_NAME, old_key)
                    )
                    raise Exception("Final file verification failed - rolled back to original")
                except Exception as rollback_error:
                    raise Exception(f"Final file verification failed and rollback failed: {rollback_error}")
            
            try:
                minio_client.remove_object(BUCKET_NAME, backup_key)
                minio_client.remove_object(BUCKET_NAME, old_key)
            except Exception as cleanup_error:
                print(f"Warning: Cleanup failed but data was saved successfully: {cleanup_error}")

            def success_update():
                global loaded_df
                loaded_df = updated_df
                metadata_text.delete("1.0", tk.END)
                for k, v in updated_df.attrs.items():
                    metadata_text.insert(tk.END, f"{k}: {v}\n")

                update_metadata_key_entry.set('')
                update_metadata_value_entry.delete(0, tk.END)
                
                update_metadata_button.config(text="Update Metadata", state="normal")

                # Update local search index if it exists
                if sample_name in search_index:
                    search_index[sample_name] = dict(updated_df.attrs)

                # Update search index on server
                try:
                    update_search_index_on_server(sample_name, dict(updated_df.attrs))
                except Exception as index_error:
                    print(f"Warning: Search index update failed: {index_error}")

                messagebox.showinfo("Success", f"Metadata '{metadata_key}' updated from '{old_value}' to '{metadata_value}' and saved to MinIO.")
            
            root.after(0, success_update)
            
        except Exception as e:
            error_msg = str(e)
            
            if backup_key:
                try:
                    minio_client.remove_object(BUCKET_NAME, backup_key)
                except:
                    pass
            
            if old_key:
                try:
                    minio_client.remove_object(BUCKET_NAME, old_key)
                except:
                    pass
            
            def error_update():
                update_metadata_button.config(text="Update Metadata", state="normal")
                messagebox.showerror("Update Error", f"Failed to update metadata safely: {error_msg}")
            
            root.after(0, error_update)
    
    threading.Thread(target=update_worker, daemon=True).start()

def refresh_index():
    def refresh_worker():
        try:
            root.after(0, lambda: refresh_cache_button.config(text="Refreshing Index...", state="disabled"))
            success = load_search_index()
            if success:
                root.after(0, lambda: refresh_file_list())
            root.after(0, lambda: refresh_cache_button.config(text="Refresh Index", state="normal"))
        except Exception as e:
            root.after(0, lambda: refresh_cache_button.config(text="Refresh Index", state="normal"))

    threading.Thread(target=refresh_worker, daemon=True).start()

def copy_selected_filename():
    """Copy the selected filename to clipboard"""
    selection = file_listbox.curselection()
    if selection:
        filename = file_listbox.get(selection[0])
        root.clipboard_clear()
        root.clipboard_append(filename)
        root.update()  # Required to make clipboard work
        status_var.set(f"Copied: {filename}")
    else:
        messagebox.showinfo("No Selection", "Please select a filename to copy.")

def copy_all_visible_filenames():
    """Copy all visible filenames to clipboard (one per line)"""
    all_files = [file_listbox.get(i) for i in range(file_listbox.size())]
    if all_files:
        filenames_text = '\n'.join(all_files)
        root.clipboard_clear()
        root.clipboard_append(filenames_text)
        root.update()
        status_var.set(f"Copied {len(all_files)} filenames")
    else:
        messagebox.showinfo("No Files", "No files available to copy.")

def show_context_menu(event):
    """Show right-click context menu"""
    try:
        # Select the item under cursor
        index = file_listbox.nearest(event.y)
        file_listbox.selection_clear(0, tk.END)
        file_listbox.selection_set(index)
        file_listbox.activate(index)

        # Show context menu
        context_menu.post(event.x_root, event.y_root)
    except:
        pass

def update_plot(time_axis, data, title="Loaded Data"):
    """Update the plot with new data"""
    global playback_line
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

    # Reset playback line reference since plot was cleared
    playback_line = None

    fig.canvas.draw()

def update_playback_line():
    """Update the playback line position based on current audio time"""
    global playback_line, playback_active, playback_start_time, audio_duration, playback_timer

    if not playback_active or playback_start_time is None or loaded_df is None:
        return

    try:
        # Calculate current playback position
        import time
        current_time = time.time()
        elapsed_time = current_time - playback_start_time

        # Use learned delay if available, otherwise use adaptive delay
        if current_file_delay is not None:
            # Use the learned delay for this specific file
            adaptive_delay = current_file_delay
        else:
            # Adaptive delay compensation based on audio complexity
            base_delay = 0.05  # 50ms base delay

            # Adaptive delay based on audio duration (longer files often have more delay)
            if audio_duration:
                if audio_duration > 10:  # Long files (>10s)
                    adaptive_delay = base_delay + 0.05  # +50ms
                elif audio_duration > 5:  # Medium files (5-10s)
                    adaptive_delay = base_delay + 0.03  # +30ms
                else:  # Short files (<5s)
                    adaptive_delay = base_delay + 0.01  # +10ms
            else:
                adaptive_delay = base_delay

        # Check if audio is still playing (sounddevice status)
        audio_finished = False
        try:
            if not sd.get_stream().active:
                audio_finished = True
        except:
            # If we can't check stream status, check if we've exceeded duration
            if audio_duration and elapsed_time > (audio_duration + 1.0):
                audio_finished = True

        if audio_finished:
            # Learn the delay if this is a learning session
            if delay_learning_active and audio_duration and current_sample_name:
                # Calculate what the delay should have been based on where the line was
                # when audio actually finished
                learned_delay = elapsed_time - audio_duration
                if 0.0 <= learned_delay <= 1.0:  # Reasonable delay range
                    learned_delays[current_sample_name] = learned_delay
                    print(f"Learned delay of {learned_delay:.3f}s for {current_sample_name}")
                    # Save learned delays to file
                    save_learned_delays()
                else:
                    print(f"Unreasonable delay {learned_delay:.3f}s detected, not saving")

            stop_playback_line()
            return

        playback_position = elapsed_time - adaptive_delay

        # Ensure position is within bounds
        if playback_position < 0:
            playback_position = 0
        elif audio_duration and playback_position > audio_duration:
            # Audio finished
            stop_playback_line()
            return

        # Get plot limits
        time_data = loaded_df["Time (s)"]
        max_time = time_data.max()
        min_time = time_data.min()

        # Only draw line if position is within data range
        if min_time <= playback_position <= max_time:
            # Remove old line efficiently
            if playback_line:
                try:
                    playback_line.remove()
                    playback_line = None
                except:
                    pass

            # Add new playback line
            playback_line = ax1.axvline(x=playback_position, color='red', linestyle='-', linewidth=2, alpha=0.8)

            # Optimize drawing - only update canvas every few frames to reduce CPU load
            # For complex plots, reduce update frequency
            update_interval = 50  # Default 50ms
            if audio_duration and audio_duration > 10:  # Long files
                update_interval = 100  # Slower updates for complex files
            elif len(loaded_df) > 50000:  # Large datasets
                update_interval = 75  # Moderate update rate

            fig.canvas.draw_idle()  # Use draw_idle for better performance

        # Schedule next update with adaptive timing
        if playback_active:
            playback_timer = root.after(update_interval, update_playback_line)

    except Exception as e:
        print(f"Error updating playback line: {e}")

def stop_playback_line():
    """Stop the playback line updates"""
    global playback_line, playback_active, playback_timer

    playback_active = False

    # Cancel timer
    if playback_timer:
        root.after_cancel(playback_timer)
        playback_timer = None

    # Remove playback line
    if playback_line:
        try:
            playback_line.remove()
            fig.canvas.draw_idle()
        except:
            pass
        playback_line = None

def save_learned_delays():
    """Save learned delays to a file for persistence"""
    try:
        import json
        delay_file = "audio_delays.json"
        with open(delay_file, 'w') as f:
            json.dump(learned_delays, f, indent=2)
        print(f"Saved {len(learned_delays)} learned delays to {delay_file}")
    except Exception as e:
        print(f"Failed to save learned delays: {e}")

def load_learned_delays():
    """Load previously learned delays from file"""
    global learned_delays
    try:
        import json
        delay_file = "audio_delays.json"
        if os.path.exists(delay_file):
            with open(delay_file, 'r') as f:
                learned_delays = json.load(f)
            print(f"Loaded {len(learned_delays)} learned delays from {delay_file}")
        else:
            learned_delays = {}
    except Exception as e:
        print(f"Failed to load learned delays: {e}")
        learned_delays = {}

def play_wav_file():
    """Play the WAV file corresponding to the loaded parquet file"""
    try:
        # Check if a parquet file is loaded
        if loaded_df is None or not current_sample_name:
            messagebox.showwarning("No File Loaded",
                                 "Please load a parquet file first before trying to play audio.")
            return

        # Get the corresponding WAV file name
        wav_filename = f"{current_sample_name}.wav"

        # Download WAV file from MinIO to a temporary location
        minio_client = create_minio_client()

        # Check if WAV file exists in MinIO
        try:
            minio_client.stat_object(BUCKET_NAME, wav_filename)
        except S3Error:
            messagebox.showerror("WAV File Not Found",
                                f"No WAV file found for '{current_sample_name}'.\n\n"
                                f"Looking for: {wav_filename}")
            return

        # Download WAV file to temporary location
        import tempfile
        temp_wav_path = os.path.join(tempfile.gettempdir(), f"temp_{wav_filename}")

        minio_client.fget_object(BUCKET_NAME, wav_filename, temp_wav_path)

        # Load and play the WAV file
        rate, data = wav.read(temp_wav_path)

        # Calculate audio duration
        global audio_duration, playback_active, playback_start_time, current_file_delay, delay_learning_active
        audio_duration = len(data) / rate

        # Check if we have a learned delay for this file
        if current_sample_name in learned_delays:
            current_file_delay = learned_delays[current_sample_name]
            delay_learning_active = False
            print(f"Using learned delay of {current_file_delay:.3f}s for {current_sample_name}")
        else:
            current_file_delay = None
            delay_learning_active = True
            print(f"Learning delay for {current_sample_name} during this playback")

        # Configure audio settings for better performance
        # Use lower latency and larger buffer size for smoother playback
        sd.default.latency = 'low'
        sd.default.blocksize = 1024  # Smaller block size for lower latency

        # Start playback line tracking
        playback_active = True
        import time
        playback_start_time = time.time()

        # Play audio with optimized settings
        sd.play(data, rate, blocking=False)

        # Start updating playback line
        update_playback_line()

        # Clean up temporary file
        try:
            os.remove(temp_wav_path)
        except:
            pass  # Ignore cleanup errors

    except S3Error as e:
        messagebox.showerror("MinIO Error", f"Failed to access WAV file: {e}")
    except Exception as e:
        messagebox.showerror("Playback Error", f"Failed to play audio: {str(e)}")

def stop_audio():
    """Stop audio playback"""
    global playback_active
    try:
        sd.stop()
        stop_playback_line()
        messagebox.showinfo("Audio Stopped", "Audio playback stopped.")
    except Exception as e:
        messagebox.showerror("Stop Error", f"Failed to stop audio: {str(e)}")

# Load previously learned delays
load_learned_delays()

# Create the GUI
root = tk.Tk()
root.title("KRAK Metadata Editor")
root.geometry("1000x700")

# Create main frames
left_frame = tk.Frame(root, width=400)
left_frame.pack(side=tk.LEFT, fill=tk.Y, padx=10, pady=10)
left_frame.pack_propagate(False)  # Maintain fixed width

# Center frame for playback controls and plot
center_frame = tk.Frame(root)
center_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=10, pady=10)

# Search section (top of left frame)
search_frame = tk.LabelFrame(left_frame, text="Metadata Search", padx=10, pady=10)
search_frame.pack(fill=tk.X, pady=(0, 10))

# Search criteria 1
search1_frame = tk.Frame(search_frame)
search1_frame.pack(fill=tk.X, pady=2)
tk.Label(search1_frame, text="Key 1:").pack(side=tk.LEFT)
search_key1_entry = ttk.Combobox(search1_frame, width=13, values=[])
search_key1_entry.pack(side=tk.LEFT, padx=5)
tk.Label(search1_frame, text="Value 1:").pack(side=tk.LEFT)
search_value1_entry = tk.Entry(search1_frame, width=15)
search_value1_entry.pack(side=tk.LEFT, padx=5)

# Search criteria 2
search2_frame = tk.Frame(search_frame)
search2_frame.pack(fill=tk.X, pady=2)
tk.Label(search2_frame, text="Key 2:").pack(side=tk.LEFT)
search_key2_entry = ttk.Combobox(search2_frame, width=13, values=[])
search_key2_entry.pack(side=tk.LEFT, padx=5)
tk.Label(search2_frame, text="Value 2:").pack(side=tk.LEFT)
search_value2_entry = tk.Entry(search2_frame, width=15)
search_value2_entry.pack(side=tk.LEFT, padx=5)

# Search criteria 3
search3_frame = tk.Frame(search_frame)
search3_frame.pack(fill=tk.X, pady=2)
tk.Label(search3_frame, text="Key 3:").pack(side=tk.LEFT)
search_key3_entry = ttk.Combobox(search3_frame, width=13, values=[])
search_key3_entry.pack(side=tk.LEFT, padx=5)
tk.Label(search3_frame, text="Value 3:").pack(side=tk.LEFT)
search_value3_entry = tk.Entry(search3_frame, width=15)
search_value3_entry.pack(side=tk.LEFT, padx=5)

# Search criteria 4
search4_frame = tk.Frame(search_frame)
search4_frame.pack(fill=tk.X, pady=2)
tk.Label(search4_frame, text="Key 4:").pack(side=tk.LEFT)
search_key4_entry = ttk.Combobox(search4_frame, width=13, values=[])
search_key4_entry.pack(side=tk.LEFT, padx=5)
tk.Label(search4_frame, text="Value 4:").pack(side=tk.LEFT)
search_value4_entry = tk.Entry(search4_frame, width=15)
search_value4_entry.pack(side=tk.LEFT, padx=5)

# Date search
date_search_frame = tk.Frame(search_frame)
date_search_frame.pack(fill=tk.X, pady=2)
tk.Label(date_search_frame, text="Date Search:").pack(side=tk.LEFT)
date_search_entry = tk.Entry(date_search_frame, width=25)
date_search_entry.pack(side=tk.LEFT, padx=5)
tk.Label(date_search_frame, text="(e.g., 2024, 2024-01, 2024-01-15)", font=('TkDefaultFont', 8)).pack(side=tk.LEFT, padx=5)

# Search buttons
button_frame = tk.Frame(search_frame)
button_frame.pack(fill=tk.X, pady=5)
search_button = tk.Button(button_frame, text="Search", command=search_metadata)
search_button.pack(side=tk.LEFT, padx=5)
clear_search_button = tk.Button(button_frame, text="Clear Search", command=clear_search)
clear_search_button.pack(side=tk.LEFT, padx=5)
refresh_cache_button = tk.Button(button_frame, text="Refresh Index", command=refresh_index)
refresh_cache_button.pack(side=tk.LEFT, padx=5)

# Progress display for search operations
search_progress_var = tk.StringVar()
search_progress_var.set("Ready")
search_progress_label = tk.Label(search_frame, textvariable=search_progress_var,
                                font=('TkDefaultFont', 9), fg='blue', relief=tk.SUNKEN, anchor=tk.W)
search_progress_label.pack(fill=tk.X, pady=(5, 0))

# File list section (upper part of left frame)
file_list_frame = tk.LabelFrame(left_frame, text="Files on Server", padx=5, pady=5)
file_list_frame.pack(fill=tk.X, pady=(5, 5))

# Filename search
filename_search_frame = tk.Frame(file_list_frame)
filename_search_frame.pack(fill=tk.X, pady=(0, 5))
tk.Label(filename_search_frame, text="Search filename:").pack(side=tk.LEFT)
filename_search_entry = tk.Entry(filename_search_frame, width=25)
filename_search_entry.pack(side=tk.LEFT, padx=5)
filename_search_button = tk.Button(filename_search_frame, text="Filter", command=lambda: filter_files_by_name())
filename_search_button.pack(side=tk.LEFT, padx=2)
filename_clear_button = tk.Button(filename_search_frame, text="Clear", command=lambda: clear_filename_search())
filename_clear_button.pack(side=tk.LEFT, padx=2)

# File listbox with scrollbar
listbox_frame = tk.Frame(file_list_frame)
listbox_frame.pack(fill=tk.BOTH, expand=True)

scrollbar = tk.Scrollbar(listbox_frame)
scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

file_listbox = tk.Listbox(listbox_frame, yscrollcommand=scrollbar.set, font=("Courier", 9), height=15)
file_listbox.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
scrollbar.config(command=file_listbox.yview)

# Create context menu for file listbox
context_menu = tk.Menu(root, tearoff=0)
context_menu.add_command(label="Copy Filename", command=copy_selected_filename)
context_menu.add_command(label="Copy All Visible Filenames", command=copy_all_visible_filenames)
context_menu.add_separator()
context_menu.add_command(label="Load Selected File", command=load_sample)

# Bind right-click to show context menu
file_listbox.bind("<Button-3>", show_context_menu)  # Right-click on Windows/Linux
file_listbox.bind("<Button-2>", show_context_menu)  # Middle-click as alternative

# Bind keyboard shortcuts
file_listbox.bind("<Control-c>", lambda e: copy_selected_filename())
file_listbox.bind("<Control-a>", lambda e: copy_all_visible_filenames())
file_listbox.bind("<Return>", lambda e: load_sample())  # Enter key to load
file_listbox.bind("<Double-Button-1>", lambda e: load_sample())  # Double-click to load

# Bind Enter key to filename search
filename_search_entry.bind("<Return>", lambda e: filter_files_by_name())
filename_search_entry.bind("<KeyRelease>", lambda e: filter_files_by_name() if len(filename_search_entry.get()) >= 1 else refresh_file_list())

# Load button
load_button = tk.Button(file_list_frame, text="Load Selected File", command=load_sample)
load_button.pack(pady=5)

# Current file status (under file list)
current_file_var = tk.StringVar()
current_file_var.set("No file loaded")
current_file_label = tk.Label(file_list_frame, textvariable=current_file_var, font=('TkDefaultFont', 9), fg='blue')
current_file_label.pack(pady=(5, 0))

# Metadata section (lower part of left frame)
metadata_frame = tk.LabelFrame(left_frame, text="Metadata", padx=5, pady=5)
metadata_frame.pack(fill=tk.BOTH, expand=True, pady=(5, 0))

metadata_text = tk.Text(metadata_frame, height=4, width=45)
metadata_text.pack(fill=tk.BOTH, expand=True, pady=(0, 5))

# Add metadata section
add_metadata_label = tk.Label(metadata_frame, text="Add New Metadata:")
add_metadata_label.pack(pady=(5, 0))

metadata_key_frame = tk.Frame(metadata_frame)
metadata_key_frame.pack(fill=tk.X, pady=2)
tk.Label(metadata_key_frame, text="Key:").pack(side=tk.LEFT)
metadata_key_entry = tk.Entry(metadata_key_frame, width=15)
metadata_key_entry.pack(side=tk.LEFT, padx=(2, 5))
tk.Label(metadata_key_frame, text="Value:").pack(side=tk.LEFT)
metadata_value_entry = tk.Entry(metadata_key_frame, width=15)
metadata_value_entry.pack(side=tk.LEFT, padx=(2, 5))

save_metadata_button = tk.Button(metadata_frame, text="Save Metadata to MinIO", command=save_metadata_to_s3)
save_metadata_button.pack(pady=(2, 0))

# Update metadata section
update_metadata_label = tk.Label(metadata_frame, text="Update Existing Metadata:")
update_metadata_label.pack(pady=(5, 0))

update_metadata_key_frame = tk.Frame(metadata_frame)
update_metadata_key_frame.pack(fill=tk.X, pady=2)
tk.Label(update_metadata_key_frame, text="Key:").pack(side=tk.LEFT)
update_metadata_key_entry = ttk.Combobox(update_metadata_key_frame, width=15, values=[])
update_metadata_key_entry.pack(side=tk.LEFT, padx=(2, 5))
tk.Label(update_metadata_key_frame, text="New Value:").pack(side=tk.LEFT)
update_metadata_value_entry = tk.Entry(update_metadata_key_frame, width=15)
update_metadata_value_entry.pack(side=tk.LEFT, padx=(2, 5))

update_metadata_button = tk.Button(metadata_frame, text="Update Metadata", command=update_metadata_in_s3)
update_metadata_button.pack(pady=(2, 0))

# Playback controls (top of center frame)
playback_frame = tk.LabelFrame(center_frame, text="Playback Controls", padx=10, pady=5)
playback_frame.pack(fill=tk.X, pady=(0, 10))

# Button frame for playback controls
playback_button_frame = tk.Frame(playback_frame)
playback_button_frame.pack(pady=10)

# Play button
play_button = tk.Button(playback_button_frame, text="Play WAV File",
                       command=play_wav_file, bg='lightgreen', width=12)
play_button.pack(side=tk.LEFT, padx=5)

# Stop button
stop_button = tk.Button(playback_button_frame, text="Stop Audio",
                       command=stop_audio, bg='lightcoral', width=12)
stop_button.pack(side=tk.LEFT, padx=5)

# Manual delay calibration button
def calibrate_delay():
    """Manual delay calibration dialog"""
    if not current_sample_name:
        messagebox.showwarning("No File", "Please load a file first.")
        return

    current_delay = learned_delays.get(current_sample_name, 0.0)
    delay_str = tk.simpledialog.askstring(
        "Audio Delay Calibration",
        f"Current delay for '{current_sample_name}': {current_delay:.3f}s\n\n"
        "Enter new delay in seconds (0.0 to 1.0):",
        initialvalue=f"{current_delay:.3f}"
    )

    if delay_str is not None:
        try:
            new_delay = float(delay_str)
            if 0.0 <= new_delay <= 1.0:
                learned_delays[current_sample_name] = new_delay
                save_learned_delays()
                messagebox.showinfo("Calibration",
                                  f"Delay set to {new_delay:.3f}s for '{current_sample_name}'")
            else:
                messagebox.showerror("Invalid Delay", "Delay must be between 0.0 and 1.0 seconds")
        except ValueError:
            messagebox.showerror("Invalid Input", "Please enter a valid number")

calibrate_button = tk.Button(playback_button_frame, text="Calibrate Delay",
                           command=calibrate_delay, bg='lightyellow', width=12)
calibrate_button.pack(side=tk.LEFT, padx=5)

# Status label for playback
playback_status_var = tk.StringVar()
playback_status_var.set("Load a file to enable playback")
playback_status_label = tk.Label(playback_frame, textvariable=playback_status_var,
                                font=('TkDefaultFont', 9), fg='gray')
playback_status_label.pack(pady=(0, 5))

# Keep status_var and progress_var for compatibility but don't display them
status_var = tk.StringVar()
status_var.set("Ready")
progress_var = tk.StringVar()
progress_var.set("")

# Add plot to center frame (below playback controls)
fig, ax1 = plt.subplots(figsize=(8, 6))
ax2 = ax1.twinx()
canvas = FigureCanvasTkAgg(fig, master=center_frame)
canvas.get_tk_widget().pack(fill=tk.BOTH, expand=True)

# Initialize file list
refresh_file_list()

def on_closing():
    """Clean shutdown when window is closed"""
    try:
        # Stop any audio playback
        sd.stop()
        print("Audio playback stopped")
    except Exception as e:
        print(f"Error stopping audio: {e}")

    try:
        # Stop playback line updates
        stop_playback_line()
        print("Playback line stopped")
    except Exception as e:
        print(f"Error stopping playback line: {e}")

    try:
        # Save learned delays before closing
        save_learned_delays()
        print("Learned delays saved")
    except Exception as e:
        print(f"Error saving delays: {e}")

    try:
        # Stop matplotlib animations/timers
        plt.close('all')
        print("Matplotlib plots closed")
    except Exception as e:
        print(f"Error closing plots: {e}")

    # Attempt to stop all non-main threads gracefully
    for thread in threading.enumerate():
        if thread is not threading.main_thread():
            try:
                print(f"Waiting for thread to finish: {thread.name}")
                thread.join(timeout=2)
            except Exception as e:
                print(f"Error joining thread {thread.name}: {e}")

    print("Cleanup complete, closing editor")
    root.quit()  # Stop the mainloop
    root.destroy()  # Destroy the window

# Bind the cleanup function to window close event
root.protocol("WM_DELETE_WINDOW", on_closing)

root.mainloop()