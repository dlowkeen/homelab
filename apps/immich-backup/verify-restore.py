#!/usr/bin/env python3
"""
Immich Backup Restore Verification Script

This script performs periodic verification of backup integrity by:
1. Downloading a random sample of files from GCS
2. Verifying checksums match the manifest
3. Logging results

This is designed to be run separately (weekly/monthly) to verify backups
are restorable without triggering excessive GCS costs.
"""

import os
import sys
import json
import hashlib
import logging
import random
import tempfile
import sqlite3
from pathlib import Path
from typing import Dict, List, Tuple
from google.cloud import storage
from google.cloud.exceptions import NotFound

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Configuration from environment
GCS_BUCKET = os.getenv('GCS_BUCKET')
GCS_CREDENTIALS_PATH = os.getenv('GOOGLE_APPLICATION_CREDENTIALS', '/etc/gcs/credentials.json')
VERIFY_SAMPLE_SIZE = int(os.getenv('VERIFY_SAMPLE_SIZE', '10'))  # Number of files to verify


def calculate_sha256(file_path: Path) -> str:
    """Calculate SHA256 checksum of a file"""
    sha256_hash = hashlib.sha256()
    with open(file_path, 'rb') as f:
        for byte_block in iter(lambda: f.read(4096), b''):
            sha256_hash.update(byte_block)
    return f"sha256:{sha256_hash.hexdigest()}"


def load_manifest(bucket: storage.Bucket) -> Tuple[sqlite3.Connection, str]:
    """Load the backup manifest database from GCS, return connection and temp path"""
    try:
        blob = bucket.blob('manifest.db')
        if not blob.exists():
            logger.error("Manifest not found in GCS")
            sys.exit(1)
        
        # Download to temp file
        with tempfile.NamedTemporaryFile(delete=False, suffix='.db') as tmp_file:
            tmp_path = tmp_file.name
        
        logger.info("Downloading manifest database from GCS...")
        blob.download_to_filename(tmp_path)
        
        # Connect to database
        conn = sqlite3.connect(tmp_path)
        conn.row_factory = sqlite3.Row
        
        # Get file count
        cursor = conn.execute("SELECT COUNT(*) as count FROM files")
        file_count = cursor.fetchone()['count']
        logger.info(f"Loaded manifest with {file_count} files")
        
        return conn, tmp_path
    except Exception as e:
        logger.error(f"Failed to load manifest: {e}")
        sys.exit(1)


def derive_gcs_path(file_path: str) -> str:
    """Derive GCS path from file path"""
    # Remove leading slash if present
    clean_path = file_path.lstrip('/')
    return f"library/{clean_path}"


def verify_file(bucket: storage.Bucket, file_info: sqlite3.Row, temp_dir: Path) -> Tuple[bool, str]:
    """
    Download and verify a single file.
    Returns: (success, message)
    """
    file_path = file_info['file_path']
    gcs_path = derive_gcs_path(file_path)
    expected_checksum = file_info['checksum']
    expected_size = file_info['size']
    
    if not expected_checksum:
        return False, "Missing required file info in manifest"
    
    try:
        blob = bucket.blob(gcs_path)
        if not blob.exists():
            return False, f"File does not exist in GCS: {gcs_path}"
        
        # Check size
        if blob.size != expected_size:
            return False, f"Size mismatch: expected {expected_size}, got {blob.size}"
        
        # Download to temp file
        temp_file = temp_dir / Path(gcs_path).name
        temp_file.parent.mkdir(parents=True, exist_ok=True)
        
        logger.info(f"Downloading {gcs_path} for verification...")
        blob.download_to_filename(str(temp_file))
        
        # Calculate checksum
        actual_checksum = calculate_sha256(temp_file)
        
        if actual_checksum != expected_checksum:
            return False, f"Checksum mismatch: expected {expected_checksum}, got {actual_checksum}"
        
        # Clean up temp file
        temp_file.unlink()
        
        return True, "Verification passed"
        
    except Exception as e:
        return False, f"Error during verification: {e}"


def main():
    """Main verification function"""
    logger.info("Starting Immich backup restore verification")
    
    # Validate required environment variables
    if not GCS_BUCKET:
        logger.error("GCS_BUCKET environment variable is required")
        sys.exit(1)
    
    # Initialize GCS client
    try:
        if os.path.exists(GCS_CREDENTIALS_PATH):
            client = storage.Client.from_service_account_json(GCS_CREDENTIALS_PATH)
        else:
            # Try default credentials
            client = storage.Client()
        bucket = client.bucket(GCS_BUCKET)
        logger.info(f"Connected to GCS bucket: {GCS_BUCKET}")
    except Exception as e:
        logger.error(f"Failed to connect to GCS: {e}")
        sys.exit(1)
    
    # Load manifest
    conn, tmp_path = load_manifest(bucket)
    
    try:
        # Get total file count
        cursor = conn.execute("SELECT COUNT(*) as count FROM files")
        total_count = cursor.fetchone()['count']
        
        if total_count == 0:
            logger.warning("No files in manifest to verify")
            sys.exit(0)
        
        # Select random sample using SQL (more efficient than loading all)
        sample_size = min(VERIFY_SAMPLE_SIZE, total_count)
        cursor = conn.execute("""
            SELECT file_path, checksum, size
            FROM files
            ORDER BY RANDOM()
            LIMIT ?
        """, (sample_size,))
        sample = cursor.fetchall()
    
    logger.info(f"Verifying {sample_size} random files from {total_count} total files")
    
    # Verify files
    verified_count = 0
    failed_count = 0
    failures = []
    
    with tempfile.TemporaryDirectory() as temp_dir:
        temp_path = Path(temp_dir)
        
        for file_info in sample:
            file_path = file_info['file_path']
            logger.info(f"Verifying: {file_path}")
            success, message = verify_file(bucket, file_info, temp_path)
            
            if success:
                verified_count += 1
                logger.info(f"  ✓ {file_path}: {message}")
            else:
                failed_count += 1
                logger.error(f"  ✗ {file_path}: {message}")
                failures.append({
                    'file': file_path,
                    'error': message
                })
    
    # Cleanup
    conn.close()
    try:
        os.unlink(tmp_path)
    except:
        pass
    
    # Summary
    logger.info("=" * 60)
    logger.info("VERIFICATION SUMMARY")
    logger.info("=" * 60)
    logger.info(f"Files verified: {verified_count}/{sample_size}")
    logger.info(f"Failures: {failed_count}")
    
    if failures:
        logger.error("Failed files:")
        for failure in failures:
            logger.error(f"  - {failure['file']}: {failure['error']}")
        sys.exit(1)
    else:
        logger.info("All sampled files verified successfully!")
        sys.exit(0)


if __name__ == '__main__':
    main()

