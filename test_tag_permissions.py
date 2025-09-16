#!/usr/bin/env python3
"""
Quick test script to verify S3 tagging permissions are working
"""

import os
import dotenv
from minio import Minio
from minio.error import S3Error
from minio.datatypes import Tags

BUCKET_NAME = "krak"

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

    return Minio(
        endpoint,
        access_key=os.getenv("MINIO_ACCESS_KEY"),
        secret_key=os.getenv("MINIO_SECRET_KEY"),
        secure=secure,
        region=os.getenv("MINIO_REGION", "us-east-1")
    )


def test_tag_permissions():
    """Test both GetObjectTagging and PutObjectTagging permissions"""
    print("Testing S3 Tag Permissions")
    print("==========================")

    try:
        # Create MinIO client
        minio_client = create_minio_client()
        print("✓ MinIO client created successfully")

        # Test file (we know this exists)
        test_file = "test_tags.parquet"

        # Test 1: Can we READ tags?
        print(f"\n🔍 Testing GetObjectTagging permission on {test_file}...")
        try:
            existing_tags = minio_client.get_object_tags(BUCKET_NAME, test_file)
            print(f"✅ GET tags SUCCESS!")

            # Show existing tags
            if hasattr(existing_tags, 'tags'):
                tags_dict = existing_tags.tags
            else:
                tags_dict = dict(existing_tags) if existing_tags else {}

            if tags_dict:
                print(f"   Found {len(tags_dict)} existing tags:")
                for key, value in tags_dict.items():
                    print(f"     {key}: {value}")
            else:
                print("   No existing tags found")

        except S3Error as e:
            print(f"❌ GET tags FAILED: {e}")
            return False

        # Test 2: Can we SET tags?
        print(f"\n🏷️  Testing PutObjectTagging permission...")
        try:
            # Create test tags
            tags = Tags.new_object_tags()
            tags["test_permission"] = "success"
            tags["timestamp"] = "2025-09-15"

            # Apply tags
            minio_client.set_object_tags(BUCKET_NAME, test_file, tags)
            print(f"✅ SET tags SUCCESS!")

            # Verify by reading back
            print(f"🔍 Verifying tags were applied...")
            new_tags = minio_client.get_object_tags(BUCKET_NAME, test_file)

            if hasattr(new_tags, 'tags'):
                new_tags_dict = new_tags.tags
            else:
                new_tags_dict = dict(new_tags) if new_tags else {}

            if "test_permission" in new_tags_dict:
                print(f"✅ VERIFICATION SUCCESS! Tags were applied correctly.")
                print(f"   test_permission: {new_tags_dict['test_permission']}")
                return True
            else:
                print(f"❌ VERIFICATION FAILED! Tags were not applied correctly.")
                return False

        except S3Error as e:
            print(f"❌ SET tags FAILED: {e}")
            return False

    except Exception as e:
        print(f"❌ Test failed with error: {e}")
        return False

if __name__ == "__main__":
    success = test_tag_permissions()

    print(f"\n" + "="*50)
    if success:
        print("🎉 ALL TAG PERMISSIONS WORKING!")
        print("✅ You can now use the tag-based metadata system")
        print("\nRecommendation: Use tags instead of JSON index")
    else:
        print("❌ Tag permissions still not working")
        print("💡 Stick with JSON index system for now")