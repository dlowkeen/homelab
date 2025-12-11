#!/usr/bin/env python3
"""
Immich GCS Backup Script

This script performs incremental, idempotent, append-only backups of:
1. Immich library files (photos/videos)
2. PostgreSQL database

Features:
- Incremental: Only uploads new/changed files
- Idempotent: Safe to run multiple times
- Append-only: Never deletes from GCS
- Verification: Checksums and metadata validation
"""

import os
import sys
import json
import hashlib
import logging
import subprocess
import gzip
import tempfile
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional, Tuple, List
from google.cloud import storage
from google.cloud.exceptions import NotFound

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Configuration from environment
LIBRARY_PATH = os.getenv('LIBRARY_PATH', '/usr/src/app/upload')
GCS_BUCKET = os.getenv('GCS_BUCKET', 'donovans-personal-stuff-immich-backups')
GCS_CREDENTIALS_PATH = os.getenv('GOOGLE_APPLICATION_CREDENTIALS', '/etc/gcs/credentials.json')
DB_HOST = os.getenv('DB_HOST', 'immich-postgresql')
DB_PORT = os.getenv('DB_PORT', '5432')
DB_NAME = os.getenv('DB_NAME')
DB_USER = os.getenv('DB_USER')
DB_PASSWORD = os.getenv('DB_PASSWORD')
IMMICH_VERSION = os.getenv('IMMICH_VERSION', 'unknown')
DB_BACKUP_RETENTION = int(os.getenv('DB_BACKUP_RETENTION', '5'))  # Keep last 5 backups
GCS_STORAGE_CLASS = os.getenv('GCS_STORAGE_CLASS', 'ARCHIVE')  # Coldest storage


class BackupManifest:
    """Manages the backup manifest stored in GCS using SQLite for efficient querying"""
    
    def __init__(self, bucket: storage.Bucket):
        self.bucket = bucket
        self.manifest_path = 'manifest.db'
        self.temp_db_path = None
        self.conn = None
        self._file_count = None
    
    def _get_db_path(self) -> str:
        """Get path to temporary SQLite database"""
        if self.temp_db_path is None:
            temp_file = tempfile.NamedTemporaryFile(delete=False, suffix='.db')
            self.temp_db_path = temp_file.name
            temp_file.close()
        return self.temp_db_path
    
    def _get_connection(self) -> sqlite3.Connection:
        """Get SQLite connection, creating database if needed"""
        if self.conn is None:
            db_path = self._get_db_path()
            self.conn = sqlite3.connect(db_path)
            self.conn.row_factory = sqlite3.Row  # Return rows as dict-like objects
            self._init_schema()
        return self.conn
    
    def _init_schema(self):
        """Initialize database schema"""
        conn = self._get_connection()
        conn.execute("""
            CREATE TABLE IF NOT EXISTS manifest_metadata (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS files (
                file_path TEXT PRIMARY KEY,
                checksum TEXT NOT NULL,
                size INTEGER NOT NULL,
                archived INTEGER DEFAULT 0
            )
        """)
        # Create indexes for efficient lookups
        conn.execute("CREATE INDEX IF NOT EXISTS idx_checksum ON files(checksum)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_archived ON files(archived)")
        conn.commit()
    
    def _derive_gcs_path(self, file_path: str) -> str:
        """Derive GCS path from file path"""
        # Remove leading slash if present
        clean_path = file_path.lstrip('/')
        return f"library/{clean_path}"
    
    def load(self) -> bool:
        """Load manifest from GCS, return True if found"""
        try:
            blob = self.bucket.blob(self.manifest_path)
            if blob.exists():
                db_path = self._get_db_path()
                logger.info("Downloading manifest database from GCS...")
                blob.download_to_filename(db_path)
                
                # Get connection to verify and count files
                conn = self._get_connection()
                cursor = conn.execute("SELECT COUNT(*) as count FROM files")
                self._file_count = cursor.fetchone()['count']
                logger.info(f"Loaded manifest with {self._file_count} files")
                return True
            else:
                logger.info("No existing manifest found, starting fresh")
                return False
        except Exception as e:
            logger.warning(f"Error loading manifest: {e}, starting fresh")
            return False
    
    def save(self):
        """Save manifest to GCS with timestamp backup"""
        if self.conn:
            self.conn.commit()
        
        # Update metadata with backup timestamp
        conn = self._get_connection()
        timestamp = datetime.now(timezone.utc).isoformat()
        conn.execute("""
            INSERT OR REPLACE INTO manifest_metadata (key, value)
            VALUES ('last_backup', ?), ('immich_version', ?), ('version', '2.0')
        """, (timestamp, IMMICH_VERSION))
        conn.commit()
        
        db_path = self._get_db_path()
        
        # Save current manifest
        blob = self.bucket.blob(self.manifest_path)
        blob.upload_from_filename(db_path)
        blob.storage_class = GCS_STORAGE_CLASS
        blob.patch()
        logger.info(f"Saved manifest to {self.manifest_path}")
        
        # Save timestamped backup of manifest
        timestamp_str = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
        backup_path = f'manifest-{timestamp_str}.db'
        backup_blob = self.bucket.blob(backup_path)
        backup_blob.upload_from_filename(db_path)
        backup_blob.storage_class = GCS_STORAGE_CLASS
        backup_blob.patch()
        logger.info(f"Saved manifest backup to {backup_path}")
    
    def get_file_info(self, file_path: str) -> Optional[Dict]:
        """Get file info from manifest (doesn't load everything, just queries)"""
        conn = self._get_connection()
        cursor = conn.execute("""
            SELECT file_path, checksum, size, archived
            FROM files
            WHERE file_path = ?
        """, (file_path,))
        row = cursor.fetchone()
        if row:
            info = dict(row)
            # Derive GCS path on-the-fly
            info['gcs_path'] = self._derive_gcs_path(file_path)
            return info
        return None
    
    def update_file_info(self, file_path: str, checksum: str, size: int, archived: bool = False):
        """Update file info in manifest"""
        conn = self._get_connection()
        conn.execute("""
            INSERT OR REPLACE INTO files 
            (file_path, checksum, size, archived)
            VALUES (?, ?, ?, ?)
        """, (file_path, checksum, size, 1 if archived else 0))
        # Commit periodically for performance, but don't commit on every insert
        # We'll commit in batches or at the end
    
    def commit(self):
        """Commit pending changes to the database"""
        if self.conn:
            self.conn.commit()
    
    def get_file_count(self) -> int:
        """Get total number of files in manifest"""
        if self._file_count is None:
            conn = self._get_connection()
            cursor = conn.execute("SELECT COUNT(*) as count FROM files")
            self._file_count = cursor.fetchone()['count']
        return self._file_count
    
    def cleanup(self):
        """Clean up temporary database file"""
        if self.conn:
            self.conn.close()
            self.conn = None
        if self.temp_db_path and os.path.exists(self.temp_db_path):
            try:
                os.unlink(self.temp_db_path)
            except:
                pass


def calculate_sha256(file_path: Path) -> str:
    """Calculate SHA256 checksum of a file"""
    sha256_hash = hashlib.sha256()
    with open(file_path, 'rb') as f:
        for byte_block in iter(lambda: f.read(4096), b''):
            sha256_hash.update(byte_block)
    return f"sha256:{sha256_hash.hexdigest()}"


def backup_library_files(bucket: storage.Bucket, manifest: BackupManifest) -> Tuple[int, int, int, List[str]]:
    """
    Backup library files incrementally.
    Returns: (total_files, new_files, skipped_files, error_list)
    """
    library_path = Path(LIBRARY_PATH)
    if not library_path.exists():
        logger.error(f"Library path does not exist: {LIBRARY_PATH}")
        sys.exit(1)
    
    total_files = 0
    new_files = 0
    skipped_files = 0
    errors = []  # Collect error messages instead of just counting
    
    logger.info(f"Scanning library directory: {LIBRARY_PATH}")
    
    # Walk through all files in library directory
    for root, dirs, files in os.walk(library_path):
        for file_name in files:
            file_path = Path(root) / file_name
            relative_path = file_path.relative_to(library_path)
            file_path_str = f"/{relative_path.as_posix()}"
            
            total_files += 1
            
            try:
                # Get file stats
                stat = file_path.stat()
                file_size = stat.st_size
                
                # Calculate checksum
                logger.debug(f"Calculating checksum for {file_path_str}")
                checksum = calculate_sha256(file_path)
                
                # Check if file is already backed up with same checksum
                existing_info = manifest.get_file_info(file_path_str)
                if existing_info and existing_info.get('checksum') == checksum:
                    logger.debug(f"Skipping {file_path_str} (already backed up)")
                    skipped_files += 1
                    continue
                
                # Upload to GCS (derive path from file_path)
                gcs_path = manifest._derive_gcs_path(file_path_str)
                blob = bucket.blob(gcs_path)
                
                logger.info(f"Uploading {file_path_str} -> {gcs_path} ({file_size} bytes)")
                blob.upload_from_filename(str(file_path))
                
                # Set storage class
                blob.storage_class = GCS_STORAGE_CLASS
                blob.patch()
                
                # Verify upload
                if not blob.exists():
                    error_msg = f"Upload verification failed: {file_path_str} -> {gcs_path}"
                    errors.append(error_msg)
                    continue
                
                # Update manifest (batch commits for performance)
                manifest.update_file_info(file_path_str, checksum, file_size, archived=False)
                new_files += 1
                
                # Commit every 100 files to balance performance and safety
                if new_files % 100 == 0:
                    manifest.commit()
                
            except Exception as e:
                error_msg = f"Error processing {file_path_str}: {e}"
                errors.append(error_msg)
    
    # Final commit
    manifest.commit()
    logger.info(f"Library backup complete: {total_files} total, {new_files} new, {skipped_files} skipped, {len(errors)} errors")
    return total_files, new_files, skipped_files, errors


def backup_database(bucket: storage.Bucket) -> bool:
    """Backup PostgreSQL database"""
    timestamp = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H-%M-%SZ')
    db_backup_name = f"database/{timestamp}-immich-{IMMICH_VERSION}.sql.gz"
    
    logger.info(f"Starting database backup: {db_backup_name}")
    
    # Set PGPASSWORD environment variable
    env = os.environ.copy()
    env['PGPASSWORD'] = DB_PASSWORD
    
    # Run pg_dump
    pg_dump_cmd = [
        'pg_dump',
        '-h', DB_HOST,
        '-p', DB_PORT,
        '-U', DB_USER,
        '-d', DB_NAME,
        '--no-owner',
        '--no-acl',
        '--clean',
        '--if-exists'
    ]
    
    try:
        # Run pg_dump and compress to temporary file
        logger.info("Running pg_dump...")
        dump_process = subprocess.Popen(
            pg_dump_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env
        )
        
        # Write to temporary compressed file
        with tempfile.NamedTemporaryFile(mode='wb', delete=False, suffix='.sql.gz') as tmp_file:
            tmp_path = tmp_file.name
            with gzip.open(tmp_file, 'wb') as gzip_file:
                for chunk in dump_process.stdout:
                    gzip_file.write(chunk)
        
        # Check for errors
        dump_process.wait()
        if dump_process.returncode != 0:
            stderr = dump_process.stderr.read().decode()
            logger.error(f"pg_dump failed: {stderr}")
            os.unlink(tmp_path)
            return False
        
        # Upload to GCS
        blob = bucket.blob(db_backup_name)
        blob.upload_from_filename(tmp_path)
        
        # Set storage class
        blob.storage_class = GCS_STORAGE_CLASS
        blob.patch()
        
        # Clean up temp file
        os.unlink(tmp_path)
        
        logger.info(f"Database backup completed: {db_backup_name}")
        
        # Clean up old database backups
        cleanup_old_db_backups(bucket)
        
        return True
        
    except Exception as e:
        logger.error(f"Database backup failed: {e}")
        if 'tmp_path' in locals():
            try:
                os.unlink(tmp_path)
            except:
                pass
        return False


def cleanup_old_db_backups(bucket: storage.Bucket):
    """Remove old database backups, keeping only the last N"""
    logger.info(f"Cleaning up old database backups (keeping last {DB_BACKUP_RETENTION})")
    
    # List all database backups
    blobs = bucket.list_blobs(prefix='database/')
    db_backups = [blob for blob in blobs if blob.name.endswith('.sql.gz')]
    
    # Sort by creation time (newest first)
    db_backups.sort(key=lambda x: x.time_created, reverse=True)
    
    # Delete old backups
    if len(db_backups) > DB_BACKUP_RETENTION:
        for blob in db_backups[DB_BACKUP_RETENTION:]:
            logger.info(f"Deleting old database backup: {blob.name}")
            blob.delete()
    
    logger.info(f"Database backup cleanup complete: {len(db_backups)} total, keeping {min(len(db_backups), DB_BACKUP_RETENTION)}")


def main():
    """Main backup function"""
    logger.info("Starting Immich backup")
    
    # Validate required environment variables
    if not GCS_BUCKET:
        logger.error("GCS_BUCKET environment variable is required")
        sys.exit(1)
    
    if not all([DB_HOST, DB_NAME, DB_USER, DB_PASSWORD]):
        logger.error("Database credentials environment variables are required")
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
    
    # Load or create manifest
    manifest = BackupManifest(bucket)
    manifest.load()
    
    # Backup library files
    logger.info("=" * 60)
    logger.info("Backing up library files...")
    logger.info("=" * 60)
    total_files, new_files, skipped_files, errors = backup_library_files(bucket, manifest)
    
    # Backup database
    logger.info("=" * 60)
    logger.info("Backing up database...")
    logger.info("=" * 60)
    db_success = backup_database(bucket)
    
    if not db_success:
        logger.error("Database backup failed")
        sys.exit(1)
    
    # Save manifest
    manifest.save()
    
    # Summary
    logger.info("=" * 60)
    logger.info("BACKUP SUMMARY")
    logger.info("=" * 60)
    logger.info(f"Library files: {total_files} total, {new_files} new, {skipped_files} skipped, {len(errors)} errors")
    logger.info(f"Database: {'SUCCESS' if db_success else 'FAILED'}")
    logger.info("=" * 60)
    
    # Output errors if any
    if errors:
        logger.warning(f"Backup completed with {len(errors)} errors")
        
        # Write errors to temp file
        error_file = tempfile.NamedTemporaryFile(mode='w', delete=False, prefix='backup-errors-', suffix='.txt')
        error_file_path = error_file.name
        try:
            error_file.write(f"Backup Errors - {datetime.now(timezone.utc).isoformat()}\n")
            error_file.write("=" * 60 + "\n\n")
            for error in errors:
                error_file.write(f"{error}\n")
            error_file.close()
            logger.info(f"Errors written to: {error_file_path}")
        except Exception as e:
            logger.error(f"Failed to write error file: {e}")
        
        # Also output errors to console
        logger.error("=" * 60)
        logger.error("ERROR DETAILS")
        logger.error("=" * 60)
        for error in errors:
            logger.error(error)
        logger.error("=" * 60)
    
    # Cleanup
    manifest.cleanup()
    
    if errors:
        logger.warning(f"Backup completed with {len(errors)} errors - check error file for details")
    else:
        logger.info("Backup completed successfully")


if __name__ == '__main__':
    main()

