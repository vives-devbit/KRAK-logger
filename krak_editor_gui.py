import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
import tkinter as tk
from tkinter import messagebox, ttk
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
import threading
import sounddevice as sd
import scipy.io.wavfile as wav
import dotenv
import datetime
import os
import io
from datetime import datetime as dt
import re
from minio import Minio
from minio.error import S3Error
from minio.commonconfig import CopySource

BUCKET_NAME = "krak"
loaded_df = None
current_sample_name = None
metadata_cache = {}


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
    except S3Error as e:
        print(f"MinIO Error applying tags to {object_name}: {e}")
        return False
    except Exception as e:
        print(f"Failed to apply tags to {object_name}: {str(e)}")
        return False

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

def load_metadata_cache():
    global metadata_cache
    try:
        minio_client = create_minio_client()
        objects = minio_client.list_objects(BUCKET_NAME)

        parquet_files = [obj.object_name for obj in objects if obj.object_name.endswith('.parquet')]

        for i, parquet_key in enumerate(parquet_files):
            try:
                # Update progress
                search_progress_var.set(f"Loading metadata... {i+1}/{len(parquet_files)}")
                root.update_idletasks()

                response = minio_client.get_object(BUCKET_NAME, parquet_key)
                df = pd.read_parquet(io.BytesIO(response.read()))
                base_name = os.path.splitext(os.path.basename(parquet_key))[0]
                metadata_cache[base_name] = dict(df.attrs)
            except Exception as e:
                print(f"Error loading metadata for {parquet_key}: {e}")
                continue

        search_progress_var.set(f"Loaded metadata for {len(metadata_cache)} files")
        return True
    except S3Error as e:
        messagebox.showerror("Cache Error", f"MinIO Error loading metadata cache: {e}")
        return False
    except Exception as e:
        messagebox.showerror("Cache Error", f"Failed to load metadata cache: {e}")
        return False

def refresh_file_list(filtered_files=None):
    file_listbox.delete(0, tk.END)
    if filtered_files is not None:
        files_to_show = filtered_files
    else:
        files_to_show = list_s3_files()
    
    for file in files_to_show:
        file_listbox.insert(tk.END, file)

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
        status_var.set("Ready")
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
        # Check if cache exists and is not empty
        if not metadata_cache:
            # Show warning and ask user to load cache
            result = messagebox.askyesno(
                "Cache Not Loaded",
                "Search requires metadata cache to be loaded first.\n\n"
                "This may take some time depending on the number of files.\n\n"
                "Do you want to load the cache now?"
            )
            if not result:
                return

            # Show progress and load cache
            search_progress_var.set("Loading metadata cache...")
            root.update_idletasks()

            if not load_metadata_cache():
                messagebox.showerror("Cache Error", "Failed to load metadata cache. Search cancelled.")
                search_progress_var.set("Cache loading failed")
                return

        # Validate cache is not empty after loading
        if not metadata_cache:
            messagebox.showwarning("No Data", "No metadata found in cache. Cannot perform search.")
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
        total_files = len(metadata_cache)
        processed = 0

        for filename, metadata in metadata_cache.items():
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
    search_key1_entry.delete(0, tk.END)
    search_value1_entry.delete(0, tk.END)
    search_key2_entry.delete(0, tk.END)
    search_value2_entry.delete(0, tk.END)
    search_key3_entry.delete(0, tk.END)
    search_value3_entry.delete(0, tk.END)
    search_key4_entry.delete(0, tk.END)
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

            # Sync metadata to MinIO tags
            try:
                if sync_metadata_to_tags(original_key, dict(updated_df.attrs)):
                    print(f"Successfully synced metadata tags for {original_key}")
                else:
                    print(f"Failed to sync metadata tags for {original_key}")
            except Exception as tag_error:
                print(f"Warning: Tag sync failed but metadata was saved successfully: {tag_error}")

            def success_update():
                global loaded_df
                loaded_df = updated_df
                metadata_text.delete("1.0", tk.END)
                for k, v in updated_df.attrs.items():
                    metadata_text.insert(tk.END, f"{k}: {v}\n")
                    
                metadata_key_entry.delete(0, tk.END)
                metadata_value_entry.delete(0, tk.END)
                
                save_metadata_button.config(text="Save Metadata to MinIO", state="normal")
                
                # Update cache
                metadata_cache[sample_name] = dict(updated_df.attrs)
                
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

            # Sync metadata to MinIO tags
            try:
                if sync_metadata_to_tags(original_key, dict(updated_df.attrs)):
                    print(f"Successfully synced metadata tags for {original_key}")
                else:
                    print(f"Failed to sync metadata tags for {original_key}")
            except Exception as tag_error:
                print(f"Warning: Tag sync failed but metadata was saved successfully: {tag_error}")

            def success_update():
                global loaded_df
                loaded_df = updated_df
                metadata_text.delete("1.0", tk.END)
                for k, v in updated_df.attrs.items():
                    metadata_text.insert(tk.END, f"{k}: {v}\n")
                    
                update_metadata_key_entry.delete(0, tk.END)
                update_metadata_value_entry.delete(0, tk.END)
                
                update_metadata_button.config(text="Update Metadata", state="normal")
                
                # Update cache
                metadata_cache[sample_name] = dict(updated_df.attrs)
                
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

def refresh_cache():
    def refresh_worker():
        try:
            root.after(0, lambda: refresh_cache_button.config(text="Refreshing...", state="disabled"))
            success = load_metadata_cache()
            if success:
                root.after(0, lambda: refresh_file_list())
            root.after(0, lambda: refresh_cache_button.config(text="Refresh Cache", state="normal"))
        except Exception as e:
            root.after(0, lambda: refresh_cache_button.config(text="Refresh Cache", state="normal"))
    
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
search_key1_entry = tk.Entry(search1_frame, width=15)
search_key1_entry.pack(side=tk.LEFT, padx=5)
tk.Label(search1_frame, text="Value 1:").pack(side=tk.LEFT)
search_value1_entry = tk.Entry(search1_frame, width=15)
search_value1_entry.pack(side=tk.LEFT, padx=5)

# Search criteria 2
search2_frame = tk.Frame(search_frame)
search2_frame.pack(fill=tk.X, pady=2)
tk.Label(search2_frame, text="Key 2:").pack(side=tk.LEFT)
search_key2_entry = tk.Entry(search2_frame, width=15)
search_key2_entry.pack(side=tk.LEFT, padx=5)
tk.Label(search2_frame, text="Value 2:").pack(side=tk.LEFT)
search_value2_entry = tk.Entry(search2_frame, width=15)
search_value2_entry.pack(side=tk.LEFT, padx=5)

# Search criteria 3
search3_frame = tk.Frame(search_frame)
search3_frame.pack(fill=tk.X, pady=2)
tk.Label(search3_frame, text="Key 3:").pack(side=tk.LEFT)
search_key3_entry = tk.Entry(search3_frame, width=15)
search_key3_entry.pack(side=tk.LEFT, padx=5)
tk.Label(search3_frame, text="Value 3:").pack(side=tk.LEFT)
search_value3_entry = tk.Entry(search3_frame, width=15)
search_value3_entry.pack(side=tk.LEFT, padx=5)

# Search criteria 4
search4_frame = tk.Frame(search_frame)
search4_frame.pack(fill=tk.X, pady=2)
tk.Label(search4_frame, text="Key 4:").pack(side=tk.LEFT)
search_key4_entry = tk.Entry(search4_frame, width=15)
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
refresh_cache_button = tk.Button(button_frame, text="Refresh Cache", command=refresh_cache)
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
update_metadata_key_entry = tk.Entry(update_metadata_key_frame, width=15)
update_metadata_key_entry.pack(side=tk.LEFT, padx=(2, 5))
tk.Label(update_metadata_key_frame, text="New Value:").pack(side=tk.LEFT)
update_metadata_value_entry = tk.Entry(update_metadata_key_frame, width=15)
update_metadata_value_entry.pack(side=tk.LEFT, padx=(2, 5))

update_metadata_button = tk.Button(metadata_frame, text="Update Metadata", command=update_metadata_in_s3)
update_metadata_button.pack(pady=(2, 0))

# Playback controls (top of center frame)
playback_frame = tk.LabelFrame(center_frame, text="Playback Controls", padx=10, pady=5)
playback_frame.pack(fill=tk.X, pady=(0, 10))

# Placeholder label for future playback functions
playback_placeholder = tk.Label(playback_frame, text="Playback functions will be added here",
                               font=('TkDefaultFont', 9), fg='gray')
playback_placeholder.pack(pady=10)

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


root.mainloop()