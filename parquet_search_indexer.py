#!/usr/bin/env python3
"""
KRAK Parquet Search Indexer

This program downloads all parquet files from MinIO, extracts their metadata,
and creates a searchable JSON index (search_index.json) that is uploaded back
to the server for centralized searching across multiple locations.

Usage:
    python parquet_search_indexer.py
"""

import os
import io
import sys
import json
import pandas as pd
import dotenv
from minio import Minio
from minio.error import S3Error
from datetime import datetime
from typing import Dict, List, Any, Optional


BUCKET_NAME = "krak"
INDEX_FILE_NAME = "search_index.json"


def create_minio_client():
    """Create and return MinIO client using environment variables"""
    dotenv.load_dotenv()
    endpoint = os.getenv("MINIO_ENDPOINT")

    if not endpoint:
        raise ValueError("MINIO_ENDPOINT environment variable not set")

    # Parse endpoint URL properly
    secure = False
    if endpoint.startswith("https://"):
        endpoint = endpoint[8:]
        secure = True
    elif endpoint.startswith("http://"):
        endpoint = endpoint[7:]
        secure = False

    access_key = os.getenv("MINIO_ACCESS_KEY")
    secret_key = os.getenv("MINIO_SECRET_KEY")

    if not access_key or not secret_key:
        raise ValueError("MINIO_ACCESS_KEY and MINIO_SECRET_KEY environment variables must be set")

    return Minio(
        endpoint,
        access_key=access_key,
        secret_key=secret_key,
        secure=secure,
        region=os.getenv("MINIO_REGION", "us-east-1")
    )


def get_all_parquet_files(minio_client) -> List[str]:
    """
    Get list of all parquet files in the bucket

    Args:
        minio_client: Initialized MinIO client

    Returns:
        List[str]: List of parquet file names
    """
    try:
        print("📋 Listing all parquet files in bucket...")
        objects = minio_client.list_objects(BUCKET_NAME, recursive=True)

        parquet_files = []
        for obj in objects:
            if obj.object_name.endswith('.parquet') and obj.object_name != INDEX_FILE_NAME:
                parquet_files.append(obj.object_name)

        print(f"✓ Found {len(parquet_files)} parquet files")
        return sorted(parquet_files)

    except S3Error as e:
        print(f"✗ MinIO Error listing files: {e}")
        return []
    except Exception as e:
        print(f"✗ Error listing files: {e}")
        return []


def extract_metadata_from_parquet(minio_client, object_name: str) -> Optional[Dict[str, Any]]:
    """
    Download and extract metadata from a parquet file

    Args:
        minio_client: Initialized MinIO client
        object_name: Name of the parquet file in MinIO

    Returns:
        Dict or None: Metadata dictionary if successful, None otherwise
    """
    try:
        # Download the parquet file
        response = minio_client.get_object(BUCKET_NAME, object_name)
        df = pd.read_parquet(io.BytesIO(response.read()))

        # Get file stats
        stat = minio_client.stat_object(BUCKET_NAME, object_name)

        # Extract metadata and add file info
        metadata = dict(df.attrs) if df.attrs else {}

        # Add file metadata
        metadata['_file_info'] = {
            'filename': object_name,
            'size_bytes': stat.size,
            'last_modified': stat.last_modified.isoformat() if stat.last_modified else None,
            'rows': len(df),
            'columns': len(df.columns),
            'column_names': list(df.columns)
        }

        return metadata

    except S3Error as e:
        print(f"✗ MinIO Error downloading {object_name}: {e}")
        return None
    except Exception as e:
        print(f"✗ Error processing {object_name}: {str(e)}")
        return None


def download_existing_index(minio_client) -> Dict[str, Any]:
    """
    Download existing search index if it exists

    Args:
        minio_client: Initialized MinIO client

    Returns:
        Dict: Existing index or empty structure
    """
    try:
        print(f"📥 Checking for existing {INDEX_FILE_NAME}...")
        response = minio_client.get_object(BUCKET_NAME, INDEX_FILE_NAME)
        existing_index = json.loads(response.read().decode('utf-8'))
        print(f"✓ Found existing index with {len(existing_index.get('files', {}))} entries")
        return existing_index

    except S3Error as e:
        if "NoSuchKey" in str(e):
            print(f"ℹ️  No existing index found - creating new one")
        else:
            print(f"⚠️  Error downloading existing index: {e}")
        return {}
    except Exception as e:
        print(f"⚠️  Error parsing existing index: {e}")
        return {}


def create_search_index(file_metadata: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    """
    Create the search index structure

    Args:
        file_metadata: Dictionary mapping filenames to their metadata

    Returns:
        Dict: Complete search index structure
    """
    index = {
        "index_info": {
            "created_at": datetime.now().isoformat(),
            "total_files": len(file_metadata),
            "index_version": "1.0"
        },
        "files": file_metadata
    }

    return index


def upload_search_index(minio_client, index_data: Dict[str, Any]) -> bool:
    """
    Upload the search index to MinIO

    Args:
        minio_client: Initialized MinIO client
        index_data: The complete index data

    Returns:
        bool: True if successful, False otherwise
    """
    try:
        print(f"📤 Uploading {INDEX_FILE_NAME}...")

        # Convert to JSON bytes
        json_bytes = json.dumps(index_data, indent=2, ensure_ascii=False).encode('utf-8')
        json_buffer = io.BytesIO(json_bytes)

        # Upload to MinIO
        minio_client.put_object(
            BUCKET_NAME,
            INDEX_FILE_NAME,
            json_buffer,
            len(json_bytes),
            content_type='application/json'
        )

        print(f"✅ Successfully uploaded {INDEX_FILE_NAME} ({len(json_bytes)} bytes)")
        return True

    except S3Error as e:
        print(f"✗ MinIO Error uploading index: {e}")
        return False
    except Exception as e:
        print(f"✗ Error uploading index: {e}")
        return False


def search_files(index_data: Dict[str, Any], search_criteria: Dict[str, str]) -> List[str]:
    """
    Search files using the index

    Args:
        index_data: The search index
        search_criteria: Dictionary of key-value pairs to search for

    Returns:
        List[str]: List of matching filenames
    """
    matching_files = []

    for filename, metadata in index_data.get('files', {}).items():
        matches = True

        for search_key, search_value in search_criteria.items():
            # Convert search to lowercase for case-insensitive matching
            search_value_lower = str(search_value).lower()

            # Check if key exists and value matches (partial match)
            if search_key in metadata:
                metadata_value = str(metadata[search_key]).lower()
                if search_value_lower not in metadata_value:
                    matches = False
                    break
            else:
                matches = False
                break

        if matches:
            matching_files.append(filename)

    return matching_files


def build_index():
    """
    Main function to build the search index
    """
    print("KRAK Parquet Search Indexer")
    print("===========================")
    print(f"Timestamp: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Bucket: {BUCKET_NAME}")
    print()

    try:
        # Create MinIO client
        minio_client = create_minio_client()
        print("✓ MinIO client created successfully")

        # Get all parquet files
        parquet_files = get_all_parquet_files(minio_client)
        if not parquet_files:
            print("⚠️  No parquet files found")
            return False

        # Download existing index to preserve timestamps and avoid reprocessing
        existing_index = download_existing_index(minio_client)
        existing_files = existing_index.get('files', {})

        # Process all files
        print(f"\n🔄 Processing {len(parquet_files)} parquet files...")
        file_metadata = {}
        processed = 0
        skipped = 0

        for i, filename in enumerate(parquet_files, 1):
            print(f"[{i}/{len(parquet_files)}] Processing {filename}...")

            # Check if file already exists in index and hasn't changed
            if filename in existing_files:
                try:
                    # Get current file stats
                    stat = minio_client.stat_object(BUCKET_NAME, filename)
                    existing_modified = existing_files[filename].get('_file_info', {}).get('last_modified')
                    current_modified = stat.last_modified.isoformat() if stat.last_modified else None

                    if existing_modified == current_modified:
                        print(f"  ℹ️  Skipping {filename} (unchanged)")
                        file_metadata[filename] = existing_files[filename]
                        skipped += 1
                        continue
                except:
                    pass  # If we can't check, just reprocess

            # Extract metadata
            metadata = extract_metadata_from_parquet(minio_client, filename)
            if metadata:
                file_metadata[filename] = metadata
                processed += 1
                print(f"  ✓ Extracted {len(metadata)-1} metadata items") # -1 for _file_info
            else:
                print(f"  ✗ Failed to extract metadata")

        print(f"\n📊 Summary:")
        print(f"  • Processed: {processed} files")
        print(f"  • Skipped (unchanged): {skipped} files")
        print(f"  • Total in index: {len(file_metadata)} files")

        # Create search index
        index_data = create_search_index(file_metadata)

        # Upload index
        if upload_search_index(minio_client, index_data):
            print(f"\n✅ Index building completed successfully!")
            return True
        else:
            print(f"\n❌ Failed to upload search index")
            return False

    except Exception as e:
        print(f"\n❌ Unexpected error: {str(e)}")
        return False


def search_index():
    """
    Interactive search functionality
    """
    try:
        # Create MinIO client
        minio_client = create_minio_client()

        # Download search index
        print(f"📥 Downloading {INDEX_FILE_NAME}...")
        response = minio_client.get_object(BUCKET_NAME, INDEX_FILE_NAME)
        index_data = json.loads(response.read().decode('utf-8'))

        print(f"✓ Loaded index with {len(index_data.get('files', {}))} files")
        print(f"  Index created: {index_data.get('index_info', {}).get('created_at', 'Unknown')}")

        # Interactive search
        print("\n🔍 Search Interface")
        print("Enter search criteria (key=value), or 'quit' to exit")
        print("Example: Product=DoE or Moisture=L1")

        while True:
            query = input("\nSearch> ").strip()

            if query.lower() in ['quit', 'exit', 'q']:
                break

            if not query:
                continue

            try:
                # Parse search criteria
                search_criteria = {}
                for pair in query.split(','):
                    if '=' in pair:
                        key, value = pair.split('=', 1)
                        search_criteria[key.strip()] = value.strip()

                if not search_criteria:
                    print("Please use format: key=value")
                    continue

                # Search
                results = search_files(index_data, search_criteria)

                print(f"\n📋 Found {len(results)} matching files:")
                for filename in results:
                    metadata = index_data['files'][filename]
                    file_info = metadata.get('_file_info', {})
                    print(f"  • {filename}")
                    print(f"    Size: {file_info.get('size_bytes', 'Unknown')} bytes")
                    print(f"    Rows: {file_info.get('rows', 'Unknown')}")

                    # Show matching metadata
                    for key, value in search_criteria.items():
                        if key in metadata:
                            print(f"    {key}: {metadata[key]}")
                    print()

            except Exception as e:
                print(f"Search error: {e}")

    except Exception as e:
        print(f"Error accessing search index: {e}")


def main():
    """Main function"""
    if len(sys.argv) > 1 and sys.argv[1] == "search":
        search_index()
    else:
        success = build_index()
        sys.exit(0 if success else 1)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\n⚠️  Process interrupted by user")
        sys.exit(1)
    except Exception as e:
        print(f"\n❌ Fatal error: {str(e)}")
        sys.exit(1)