"""MinIO storage: client creation, file listing and search-index updates."""

import datetime
import io
import json
import os
from tkinter import messagebox

from minio import Minio
from minio.error import S3Error

from .config import BUCKET_NAME, INDEX_FILE_NAME, load_env


def create_minio_client():
    """Create and return MinIO client"""
    load_env()
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


def list_s3_files(sort_by="name", sort_order="asc"):
    """List MinIO files with sorting options

    Args:
        sort_by: "name" or "time"
        sort_order: "asc" (1-9) or "desc" (9-1)
    """
    try:
        minio_client = create_minio_client()
        objects = minio_client.list_objects(BUCKET_NAME)

        files_info = {}
        for obj in objects:
            if obj.object_name.endswith('.parquet'):  # Only process parquet files
                base_name = os.path.splitext(os.path.basename(obj.object_name))[0]
                if base_name not in files_info:
                    files_info[base_name] = {
                        'name': base_name,
                        'last_modified': obj.last_modified
                    }

        if sort_by == "time":
            sorted_files = sorted(files_info.values(),
                                  key=lambda x: x['last_modified'],
                                  reverse=(sort_order == "desc"))
        else:  # sort by name
            sorted_files = sorted(files_info.values(),
                                  key=lambda x: x['name'],
                                  reverse=(sort_order == "desc"))

        return [file_info['name'] for file_info in sorted_files]

    except S3Error as e:
        print(f"MinIO Error listing files: {e}")
        return []
    except Exception as e:
        print(f"Error listing files: {e}")
        return []


def update_search_index_on_server(parquet_filename, metadata_dict, dataframe):
    """
    Update the search index on the server with new metadata for an uploaded file

    Args:
        parquet_filename: Full filename (with .parquet extension)
        metadata_dict: Dictionary of metadata
        dataframe: The pandas DataFrame for file info

    Returns:
        bool: True if successful, False otherwise
    """
    try:
        minio_client = create_minio_client()

        # Download existing index or create new one
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

        # Get file statistics
        try:
            stat = minio_client.stat_object(BUCKET_NAME, parquet_filename)
            file_info = {
                'filename': parquet_filename,
                'size_bytes': stat.size,
                'last_modified': stat.last_modified.isoformat() if stat.last_modified else None,
                'rows': len(dataframe),
                'columns': len(dataframe.columns),
                'column_names': list(dataframe.columns)
            }
        except Exception as file_error:
            print(f"Warning: Could not get file stats: {file_error}")
            file_info = {
                'filename': parquet_filename,
                'size_bytes': 'Unknown',
                'last_modified': datetime.datetime.now().isoformat(),
                'rows': len(dataframe) if dataframe is not None else 'Unknown',
                'columns': len(dataframe.columns) if dataframe is not None else 'Unknown',
                'column_names': list(dataframe.columns) if dataframe is not None else 'Unknown'
            }

        # Prepare the file metadata
        file_metadata = dict(metadata_dict) if metadata_dict else {}
        file_metadata['_file_info'] = file_info

        # Update the index
        index_data['files'][parquet_filename] = file_metadata
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
        return True

    except Exception as e:
        error_msg = f"Failed to update search index: {str(e)}"
        print(error_msg)

        messagebox.showerror("Search Index Error",
                             f"Could not update search index for {parquet_filename}.\n\n"
                             f"Error: {str(e)}\n\n"
                             f"The file was uploaded successfully, but won't be searchable until the index is updated.")
        return False
