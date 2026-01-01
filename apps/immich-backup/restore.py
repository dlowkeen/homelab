#!/usr/bin/env python3
"""
Immich Backup Restore Script

This script restores Immich backups from GCS to a local directory.
It downloads all library files and optionally restores the database.

Usage:
    python3 restore.py --restore-dir /path/to/restore [--restore-db] [--db-host localhost] [--db-name immich] [--db-user immich] [--db-password password]
"""

import os
import sys
import argparse
import logging
import sqlite3
import tempfile
import subprocess
import gzip
from pathlib import Path
from typing import Dict, Optional
from google.cloud import storage
from google.cloud.exceptions import NotFound

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Configuration from environment
GCS_BUCKET = os.getenv('GCS_BUCKET', 'donovans-personal-stuff-immich-backups')
GCS_CREDENTIALS_PATH = os.getenv('GOOGLE_APPLICATION_CREDENTIALS', '/etc/gcs/credentials.json')
MANIFEST_PATH = 'manifest.db'


def load_manifest(bucket: storage.Bucket) -> tuple[sqlite3.Connection, str]:
    """Load the backup manifest database from GCS, return connection and temp path"""
    logger.info(f"Loading manifest from GCS: {MANIFEST_PATH}")
    
    # Download manifest to temp file
    blob = bucket.blob(MANIFEST_PATH)
    if not blob.exists():
        raise FileNotFoundError(f"Manifest not found in GCS: {MANIFEST_PATH}")
    
    temp_db = tempfile.NamedTemporaryFile(delete=False, suffix='.db')
    temp_db_path = temp_db.name
    temp_db.close()
    
    blob.download_to_filename(temp_db_path)
    logger.info(f"Manifest downloaded to {temp_db_path}")
    
    # Open SQLite connection
    conn = sqlite3.connect(temp_db_path)
    conn.row_factory = sqlite3.Row
    
    return conn, temp_db_path


def get_all_files_from_manifest(conn: sqlite3.Connection) -> list[Dict]:
    """Get all files from manifest that are not archived"""
    cursor = conn.cursor()
    cursor.execute("""
        SELECT file_path, size, checksum 
        FROM files 
        WHERE archived = 0
        ORDER BY file_path
    """)
    
    files = []
    for row in cursor.fetchall():
        files.append({
            'file_path': row['file_path'],
            'size': row['size'],
            'checksum': row['checksum']
        })
    
    return files


def format_bytes(bytes_size: int) -> str:
    """Format bytes into human-readable format (KB, MB, GB)"""
    for unit in ['B', 'KB', 'MB', 'GB']:
        if bytes_size < 1024.0:
            if unit == 'B':
                return f"{int(bytes_size)} {unit}"
            return f"{bytes_size:.2f} {unit}"
        bytes_size /= 1024.0
    return f"{bytes_size:.2f} GB"


def download_file_from_gcs(bucket: storage.Bucket, gcs_path: str, local_path: Path, expected_size: int) -> bool:
    """Download a file from GCS to local path"""
    try:
        blob = bucket.blob(gcs_path)
        if not blob.exists():
            logger.error(f"File not found in GCS: {gcs_path}")
            return False
        
        # Create parent directories
        local_path.parent.mkdir(parents=True, exist_ok=True)
        
        # Download file
        blob.download_to_filename(str(local_path))
        
        # Verify size
        actual_size = local_path.stat().st_size
        if actual_size != expected_size:
            logger.warning(f"Size mismatch for {local_path}: expected {expected_size}, got {actual_size}")
            return False
        
        return True
    except Exception as e:
        logger.error(f"Failed to download {gcs_path}: {e}")
        return False


def restore_database(bucket: storage.Bucket, db_host: str, db_port: str, db_name: str, db_user: str, db_password: str) -> bool:
    """Restore the most recent database backup"""
    logger.info("Finding most recent database backup...")
    
    # List all database backups
    blobs = bucket.list_blobs(prefix='database/')
    db_backups = []
    for blob in blobs:
        if blob.name.endswith('.sql.gz'):
            db_backups.append(blob)
    
    if not db_backups:
        logger.error("No database backups found in GCS")
        return False
    
    # Sort by time created (most recent first)
    db_backups.sort(key=lambda x: x.time_created, reverse=True)
    latest_backup = db_backups[0]
    
    logger.info(f"Found latest database backup: {latest_backup.name} (created: {latest_backup.time_created})")
    
    # Download to temp file
    temp_db = tempfile.NamedTemporaryFile(delete=False, suffix='.sql.gz')
    temp_db_path = temp_db.name
    temp_db.close()
    
    logger.info(f"Downloading database backup ({format_bytes(latest_backup.size)})...")
    latest_backup.download_to_filename(temp_db_path)
    
    # Decompress and restore
    logger.info("Decompressing database backup...")
    decompressed_path = temp_db_path.replace('.gz', '')
    
    try:
        with gzip.open(temp_db_path, 'rb') as gz_file:
            with open(decompressed_path, 'wb') as sql_file:
                sql_file.write(gz_file.read())
        
        # Restore using psql
        logger.info(f"Restoring database to {db_host}:{db_port}/{db_name}...")
        env = os.environ.copy()
        env['PGPASSWORD'] = db_password
        
        restore_cmd = [
            'psql',
            '-h', db_host,
            '-p', db_port,
            '-U', db_user,
            '-d', db_name,
            '-f', decompressed_path
        ]
        
        result = subprocess.run(restore_cmd, env=env, capture_output=True, text=True)
        
        if result.returncode != 0:
            logger.error(f"Database restore failed: {result.stderr}")
            return False
        
        logger.info("Database restore completed successfully")
        return True
        
    finally:
        # Clean up temp files
        if os.path.exists(temp_db_path):
            os.unlink(temp_db_path)
        if os.path.exists(decompressed_path):
            os.unlink(decompressed_path)


def main():
    parser = argparse.ArgumentParser(description='Restore Immich backup from GCS')
    parser.add_argument('--restore-dir', required=True, help='Directory to restore files to')
    parser.add_argument('--restore-db', action='store_true', help='Also restore database')
    parser.add_argument('--db-host', default='localhost', help='Database host (default: localhost)')
    parser.add_argument('--db-port', default='5432', help='Database port (default: 5432)')
    parser.add_argument('--db-name', default='immich', help='Database name (default: immich)')
    parser.add_argument('--db-user', default='immich', help='Database user (default: immich)')
    parser.add_argument('--db-password', help='Database password (or set PGPASSWORD env var)')
    parser.add_argument('--gcs-bucket', default=GCS_BUCKET, help=f'GCS bucket name (default: {GCS_BUCKET})')
    parser.add_argument('--skip-existing', action='store_true', help='Skip files that already exist locally')
    
    args = parser.parse_args()
    
    # Validate restore directory
    restore_dir = Path(args.restore_dir)
    if not restore_dir.exists():
        logger.info(f"Creating restore directory: {restore_dir}")
        restore_dir.mkdir(parents=True, exist_ok=True)
    
    if not restore_dir.is_dir():
        logger.error(f"Restore path is not a directory: {restore_dir}")
        sys.exit(1)
    
    # Initialize GCS client
    logger.info(f"Connecting to GCS bucket: {args.gcs_bucket}")
    client = storage.Client()
    bucket = client.bucket(args.gcs_bucket)
    
    if not bucket.exists():
        logger.error(f"Bucket does not exist: {args.gcs_bucket}")
        sys.exit(1)
    
    # Load manifest
    try:
        conn, temp_db_path = load_manifest(bucket)
    except Exception as e:
        logger.error(f"Failed to load manifest: {e}")
        sys.exit(1)
    
    try:
        # Get all files from manifest
        logger.info("Reading file list from manifest...")
        files = get_all_files_from_manifest(conn)
        total_files = len(files)
        total_size = sum(f['size'] for f in files)
        
        logger.info(f"Found {total_files} files to restore ({format_bytes(total_size)})")
        
        # Restore files
        restored = 0
        skipped = 0
        errors = 0
        
        for idx, file_info in enumerate(files, 1):
            file_path = file_info['file_path']
            file_size = file_info['size']
            gcs_path = f"library/{file_path}"
            
            # Local restore path
            local_path = restore_dir / file_path.lstrip('/')
            
            # Skip if exists and --skip-existing
            if args.skip_existing and local_path.exists():
                local_size = local_path.stat().st_size
                if local_size == file_size:
                    skipped += 1
                    if idx % 100 == 0:
                        logger.info(f"Progress: {idx}/{total_files} ({idx/total_files*100:.1f}%), {restored} restored, {skipped} skipped, {errors} errors")
                    continue
            
            # Download file
            if download_file_from_gcs(bucket, gcs_path, local_path, file_size):
                restored += 1
            else:
                errors += 1
            
            # Progress logging
            if idx % 100 == 0 or idx == total_files:
                logger.info(f"Progress: {idx}/{total_files} ({idx/total_files*100:.1f}%), {restored} restored, {skipped} skipped, {errors} errors")
        
        logger.info("=" * 60)
        logger.info("FILE RESTORE SUMMARY")
        logger.info("=" * 60)
        logger.info(f"Total files: {total_files}")
        logger.info(f"Restored: {restored}")
        logger.info(f"Skipped: {skipped}")
        logger.info(f"Errors: {errors}")
        logger.info(f"Restore directory: {restore_dir}")
        logger.info("=" * 60)
        
        # Restore database if requested
        if args.restore_db:
            logger.info("=" * 60)
            logger.info("RESTORING DATABASE")
            logger.info("=" * 60)
            
            db_password = args.db_password or os.getenv('PGPASSWORD')
            if not db_password:
                logger.error("Database password required (--db-password or PGPASSWORD env var)")
                sys.exit(1)
            
            if restore_database(bucket, args.db_host, args.db_port, args.db_name, args.db_user, db_password):
                logger.info("Database restore completed successfully")
            else:
                logger.error("Database restore failed")
                sys.exit(1)
        
        logger.info("Restore completed successfully!")
        
    finally:
        # Clean up
        conn.close()
        if os.path.exists(temp_db_path):
            os.unlink(temp_db_path)


if __name__ == '__main__':
    main()

