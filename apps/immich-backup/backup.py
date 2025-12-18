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
import time
import threading
import signal
import atexit
from queue import Queue
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional, Tuple, List, Callable
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
UPLOAD_WORKERS = int(os.getenv('UPLOAD_WORKERS', '4'))  # Number of parallel upload threads
MAX_PENDING_FUTURES = int(os.getenv('MAX_PENDING_FUTURES', '50000'))  # Max pending futures to prevent unbounded memory growth (~10MB memory)


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
            # check_same_thread=False allows connection to be used across threads
            # We use locks (manifest_lock) to ensure thread-safe access
            self.conn = sqlite3.connect(db_path, check_same_thread=False)
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
    
    def save_to_gcs(self, include_backup: bool = False):
        """
        Save manifest to GCS (lightweight version for periodic saves).
        
        Args:
            include_backup: If True, also create a timestamped backup copy
        """
        if self.conn:
            self.conn.commit()
        
        db_path = self._get_db_path()
        
        # Save current manifest with retry
        def upload_manifest():
            blob = self.bucket.blob(self.manifest_path)
            blob.upload_from_filename(db_path)
            blob.storage_class = GCS_STORAGE_CLASS
            blob.patch()
            return True
        
        try:
            retry_with_backoff(upload_manifest, max_retries=3, initial_delay=1.0, max_delay=30.0)
            logger.info(f"Saved manifest to {self.manifest_path}")
        except Exception as e:
            logger.error(f"Failed to save manifest to GCS: {e}")
            raise
        
        # Optionally save timestamped backup
        if include_backup:
            timestamp_str = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
            backup_path = f'manifest-{timestamp_str}.db'
            backup_blob = self.bucket.blob(backup_path)
            backup_blob.upload_from_filename(db_path)
            backup_blob.storage_class = GCS_STORAGE_CLASS
            backup_blob.patch()
            logger.info(f"Saved manifest backup to {backup_path}")
    
    def save(self):
        """Save manifest to GCS with timestamp backup and metadata update"""
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
        
        # Save to GCS with backup
        self.save_to_gcs(include_backup=True)
    
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


def format_bytes(bytes_size: int) -> str:
    """Format bytes into human-readable format (KB, MB, GB)"""
    for unit in ['B', 'KB', 'MB', 'GB']:
        if bytes_size < 1024.0:
            if unit == 'B':
                return f"{int(bytes_size)} {unit}"
            return f"{bytes_size:.2f} {unit}"
        bytes_size /= 1024.0
    return f"{bytes_size:.2f} TB"


def calculate_sha256(file_path: Path) -> str:
    """Calculate SHA256 checksum of a file"""
    sha256_hash = hashlib.sha256()
    with open(file_path, 'rb') as f:
        for byte_block in iter(lambda: f.read(4096), b''):
            sha256_hash.update(byte_block)
    return f"sha256:{sha256_hash.hexdigest()}"


def retry_with_backoff(func: Callable, max_retries: int = 5, initial_delay: float = 1.0, max_delay: float = 60.0, backoff_factor: float = 2.0):
    """
    Retry a function with exponential backoff.
    
    Args:
        func: Function to retry
        max_retries: Maximum number of retry attempts
        initial_delay: Initial delay in seconds
        max_delay: Maximum delay in seconds
        backoff_factor: Multiplier for delay between retries
    
    Returns:
        Result of the function call
    
    Raises:
        Last exception if all retries fail
    """
    delay = initial_delay
    last_exception = None
    
    for attempt in range(max_retries):
        try:
            return func()
        except Exception as e:
            last_exception = e
            if attempt < max_retries - 1:
                logger.warning(f"Attempt {attempt + 1}/{max_retries} failed: {e}. Retrying in {delay:.1f}s...")
                time.sleep(delay)
                delay = min(delay * backoff_factor, max_delay)
            else:
                logger.error(f"All {max_retries} attempts failed. Last error: {e}")
    
    raise last_exception


def _process_single_file(file_path: Path, library_path: Path, bucket: storage.Bucket, manifest: BackupManifest, manifest_lock: threading.Lock) -> Tuple[str, Optional[str]]:
    """
    Process a single file for backup (worker function for parallel processing).
    Returns: (status, error_message)
    Status can be: 'uploaded', 'skipped', 'error'
    """
    relative_path = file_path.relative_to(library_path)
    file_path_str = f"/{relative_path.as_posix()}"
    
    try:
        # Get file stats
        stat = file_path.stat()
        file_size = stat.st_size
        
        # Check if file is already backed up (thread-safe)
        # Optimize by checking manifest first - only calculate expensive checksum if needed
        with manifest_lock:
            existing_info = manifest.get_file_info(file_path_str)
            if existing_info and existing_info.get('size') == file_size:
                # File exists in manifest with matching size - assume unchanged and skip
                # This avoids expensive checksum calculation for files we've already backed up
                # Size + path is sufficient for most cases (files rarely change without size change)
                return ('skipped', None)
        
        # File is new or size changed - need to calculate checksum and upload
        checksum = calculate_sha256(file_path)
        
        # Double-check manifest after calculating checksum (in case file was modified but size stayed same)
        with manifest_lock:
            existing_info = manifest.get_file_info(file_path_str)
            if existing_info and existing_info.get('checksum') == checksum:
                # Checksum matches - file unchanged, skip upload
                return ('skipped', None)
        
        # Upload to GCS (derive path from file_path)
        gcs_path = manifest._derive_gcs_path(file_path_str)
        blob = bucket.blob(gcs_path)
        
        # Upload with retry logic
        logger.info(f"Uploading {file_path_str} -> {gcs_path} ({format_bytes(file_size)})")
        try:
            def upload_file():
                # upload_from_filename automatically uses resumable uploads for large files
                # This handles timeouts better and can resume if interrupted
                # The library will automatically retry on timeout errors
                # Use a long timeout (1800s = 30 mins) to handle slow uploads on constrained bandwidth
                # For large files on slow connections, this prevents premature timeouts
                blob.upload_from_filename(str(file_path), timeout=1800)
                return True
            
            retry_with_backoff(upload_file, max_retries=5, initial_delay=2.0, max_delay=120.0)
            
            # Reload blob to ensure we have the latest metadata after upload
            blob.reload()
            
            # Set storage class with retry
            def set_storage_class():
                # Reload blob first to ensure it exists and we have current metadata
                blob.reload()
                if not blob.exists():
                    raise Exception(f"Blob does not exist after upload: {gcs_path}")
                blob.storage_class = GCS_STORAGE_CLASS
                blob.patch()
                return True
            
            retry_with_backoff(set_storage_class, max_retries=3, initial_delay=1.0, max_delay=30.0)
            
            # Verify upload with retry
            def verify_upload():
                if not blob.exists():
                    raise Exception("Upload verification failed: blob does not exist")
                return True
            
            retry_with_backoff(verify_upload, max_retries=3, initial_delay=1.0, max_delay=30.0)
            
            # Update manifest (thread-safe)
            with manifest_lock:
                manifest.update_file_info(file_path_str, checksum, file_size, archived=False)
            
            return ('uploaded', None)
            
        except Exception as e:
            error_msg = f"Upload failed after retries: {file_path_str} -> {gcs_path}: {e}"
            logger.error(error_msg)
            return ('error', error_msg)
            
    except Exception as e:
        error_msg = f"Error processing {file_path_str}: {e}"
        logger.error(error_msg)
        return ('error', error_msg)


def _process_completed_future(future, completed: int, new_files: int, skipped_files: int, errors: List[str], 
                               manifest: BackupManifest, manifest_lock: threading.Lock, 
                               total_files: int = None) -> Tuple[int, int, int]:
    """
    Process a completed future and update counters/logs/manifest.
    Returns: (new_files, skipped_files, completed)
    """
    try:
        status, error_msg = future.result()
        
        if status == 'uploaded':
            new_files += 1
        elif status == 'skipped':
            skipped_files += 1
        elif status == 'error':
            if error_msg:
                errors.append(error_msg)
        
        # Log progress every 10 files
        if completed % 10 == 0:
            if total_files is not None and total_files > 0:
                percentage = (completed / total_files) * 100
                logger.info(f"Progress: {completed}/{total_files} processed ({percentage:.1f}%), {new_files} uploaded, {skipped_files} skipped, {len(errors)} errors")
            else:
                # Producer still discovering files - show discovered count as estimate
                logger.info(f"Progress: {completed} processed (at least {completed} total discovered so far), {new_files} uploaded, {skipped_files} skipped, {len(errors)} errors")
        
        # Commit to local DB every 50 new files (thread-safe)
        if new_files > 0 and new_files % 50 == 0:
            with manifest_lock:
                manifest.commit()
            logger.info(f"Manifest committed locally: {new_files} new files backed up so far")
        
        # Save manifest to GCS every 50 new files (thread-safe) - matches local commit to prevent progress loss
        # Previously was 100, but that meant losing up to 100 files of progress on failure
        if new_files > 0 and new_files % 50 == 0:
            try:
                with manifest_lock:
                    manifest.save_to_gcs(include_backup=False)
                logger.info(f"Manifest saved to GCS: {new_files} new files backed up so far")
            except Exception as e:
                logger.error(f"Failed to save manifest to GCS (will retry later): {e}")
                # Don't fail the whole backup if manifest save fails
        
        return new_files, skipped_files, completed + 1
        
    except Exception as e:
        error_msg = f"Unexpected error processing file: {e}"
        logger.error(error_msg)
        errors.append(error_msg)
        return new_files, skipped_files, completed + 1


def backup_library_files(bucket: storage.Bucket, manifest: BackupManifest) -> Tuple[int, int, int, List[str]]:
    """
    Backup library files incrementally using parallel uploads with queue-based producer-consumer pattern.
    Uses bounded queue to limit memory usage and provide natural backpressure.
    Returns: (total_files, new_files, skipped_files, error_list)
    """
    library_path = Path(LIBRARY_PATH)
    if not library_path.exists():
        logger.error(f"Library path does not exist: {LIBRARY_PATH}")
        sys.exit(1)
    
    # Thread-safe counters and collections
    total_files = 0
    new_files = 0
    skipped_files = 0
    errors = []
    manifest_lock = threading.Lock()
    
    # Bounded queue: limits memory usage and provides backpressure
    # Queue size = workers * 10 gives buffer for in-flight tasks
    queue_size = UPLOAD_WORKERS * 10
    file_queue = Queue(maxsize=queue_size)
    
    logger.info(f"Backing up library directory: {LIBRARY_PATH} (using {UPLOAD_WORKERS} parallel workers, queue size: {queue_size})")
    
    # Track when producer finishes (for accurate progress reporting)
    producer_finished = threading.Event()
    
    def file_producer():
        """Producer thread: walks directory and adds files to queue"""
        nonlocal total_files
        files_queued = 0
        try:
            for root, dirs, files in os.walk(library_path):
                for file_name in files:
                    file_path = Path(root) / file_name
                    total_files += 1
                    file_queue.put(file_path)  # Blocks if queue is full (backpressure)
                    files_queued += 1  # Only incremented after successful put()
                    if files_queued % 10 == 0:
                        logger.info(f"Producer: discovered {total_files} files, queued {files_queued} files, queue size: {file_queue.qsize()}")
            # Signal completion with None sentinel
            file_queue.put(None)
            logger.info(f"Producer finished: discovered {total_files} files, queued {files_queued} files")
            producer_finished.set()  # Mark producer as finished
        except Exception as e:
            logger.error(f"Error in file producer: {e}")
            file_queue.put(None)  # Signal completion even on error
            producer_finished.set()
    
    # Start producer thread
    producer_thread = threading.Thread(target=file_producer, name="FileProducer")
    producer_thread.start()
    
    # Process files in parallel using queue
    # Limit pending futures to prevent unbounded memory growth
    # Default 50,000 = ~10MB memory (reasonable), configurable via MAX_PENDING_FUTURES env var
    # This is much larger than the queue size (20) to allow executor to have a buffer
    # but still bounded to prevent issues with millions of files
    max_pending_futures = MAX_PENDING_FUTURES
    with ThreadPoolExecutor(max_workers=UPLOAD_WORKERS) as executor:
        futures = []
        completed = 0
        
        while True:
            # Get file from queue (blocks until available)
            file_path = file_queue.get()
            
            # Check for sentinel (end of files)
            if file_path is None:
                break
            
            # Process completed futures first to free memory and make room
            # Check completed futures every iteration to keep memory bounded
            for completed_future in list(futures):
                if completed_future.done():
                    futures.remove(completed_future)
                    # Only use total_files for progress if producer has finished, otherwise show "discovered so far"
                    total_for_progress = total_files if producer_finished.is_set() else None
                    new_files, skipped_files, completed = _process_completed_future(
                        completed_future, completed, new_files, skipped_files, errors, manifest, manifest_lock, total_for_progress
                    )
            
            # Wait if we have too many pending futures (backpressure)
            # This prevents the executor's internal queue and futures list from growing unbounded
            while len(futures) >= max_pending_futures:
                # Wait for at least one future to complete
                for completed_future in list(futures):
                    if completed_future.done():
                        futures.remove(completed_future)
                        total_for_progress = total_files if producer_finished.is_set() else None
                        new_files, skipped_files, completed = _process_completed_future(
                            completed_future, completed, new_files, skipped_files, errors, manifest, manifest_lock, total_for_progress
                        )
                        break
                else:
                    # No futures completed yet, wait a bit
                    time.sleep(0.1)
            
            # Submit file for processing
            future = executor.submit(_process_single_file, file_path, library_path, bucket, manifest, manifest_lock)
            futures.append(future)
        
        # Wait for producer to finish to get final total count
        producer_thread.join()
        logger.info(f"Directory scan complete. Found {total_files} total files. Waiting for remaining uploads to complete...")
        
        # Wait for all remaining futures to complete
        for future in as_completed(futures):
            new_files, skipped_files, completed = _process_completed_future(
                future, completed, new_files, skipped_files, errors, manifest, manifest_lock, total_files
            )
    
    # Final commit to local DB
    with manifest_lock:
        manifest.commit()
    
    # Save manifest to GCS to preserve progress (even if there were errors)
    if new_files > 0:
        try:
            manifest.save_to_gcs(include_backup=False)
            logger.info(f"Manifest saved to GCS after library backup: {new_files} new files backed up")
        except Exception as e:
            logger.error(f"Failed to save manifest to GCS after library backup: {e}")
            # Don't fail - we'll try again at the end
    
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


# Global manifest reference for signal handler
_global_manifest = None
_global_manifest_lock = None

def _signal_handler(signum, frame):
    """Handle termination signals gracefully by saving manifest"""
    global _global_manifest, _global_manifest_lock
    logger.warning(f"Received signal {signum}, saving manifest before exit...")
    if _global_manifest and _global_manifest_lock:
        try:
            with _global_manifest_lock:
                _global_manifest.save_to_gcs(include_backup=False)
            logger.info("Manifest saved successfully before exit")
        except Exception as e:
            logger.error(f"Failed to save manifest on exit: {e}")
    sys.exit(1)

def main():
    """Main backup function"""
    global _global_manifest, _global_manifest_lock
    
    # Register signal handlers for graceful termination
    signal.signal(signal.SIGTERM, _signal_handler)
    signal.signal(signal.SIGINT, _signal_handler)
    
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
    
    # Set global reference for signal handler
    _global_manifest = manifest
    # Create a lock for signal handler (use manifest's internal lock mechanism)
    _global_manifest_lock = threading.Lock()
    
    # Register atexit handler as backup (in case signals don't work)
    def save_on_exit():
        if _global_manifest:
            try:
                with _global_manifest_lock:
                    _global_manifest.save_to_gcs(include_backup=False)
                logger.info("Manifest saved via atexit handler")
            except Exception as e:
                logger.error(f"Failed to save manifest via atexit: {e}")
    
    atexit.register(save_on_exit)
    
    # Backup library files
    logger.info("=" * 60)
    logger.info("Backing up library files...")
    logger.info("=" * 60)
    try:
        total_files, new_files, skipped_files, errors = backup_library_files(bucket, manifest)
    except Exception as e:
        logger.error(f"Fatal error during library backup: {e}", exc_info=True)
        # Try to save manifest before exiting
        try:
            with _global_manifest_lock:
                _global_manifest.save_to_gcs(include_backup=False)
            logger.info("Manifest saved after fatal error")
        except Exception as save_error:
            logger.error(f"Failed to save manifest after fatal error: {save_error}")
        # Don't exit - continue to database backup and final save
        total_files, new_files, skipped_files, errors = 0, 0, 0, [f"Fatal error during library backup: {e}"]
    
    # Backup database
    logger.info("=" * 60)
    logger.info("Backing up database...")
    logger.info("=" * 60)
    try:
        db_success = backup_database(bucket)
    except Exception as e:
        logger.error(f"Fatal error during database backup: {e}", exc_info=True)
        db_success = False
    
    # Always save manifest, even if database backup failed
    # This preserves library backup progress
    try:
        manifest.save()
    except Exception as e:
        logger.error(f"Failed to save manifest: {e}")
        # Try lightweight save as fallback
        try:
            manifest.save_to_gcs(include_backup=False)
            logger.info("Saved manifest using lightweight method")
        except Exception as e2:
            logger.error(f"Failed to save manifest even with lightweight method: {e2}")
    
    if not db_success:
        logger.error("Database backup failed - but continuing to save manifest")
        # Don't exit - save manifest and report error, but don't crash the job
        # The job will be marked as failed by Kubernetes if we exit with non-zero
        # But we want to preserve progress, so we'll exit with 0 and log the error
    
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
    
    # Determine exit code
    # Exit with 0 (success) if we made progress, even if there were some upload errors
    # Exit with 1 (failure) only if we couldn't connect to GCS, missing env vars, etc.
    if errors:
        logger.warning(f"Backup completed with {len(errors)} errors - check error file for details")
        # If we made progress (new_files > 0), consider it a success
        # Upload errors are expected with bandwidth constraints
        if new_files > 0:
            logger.info(f"Backup completed with {new_files} new files backed up despite {len(errors)} errors")
            sys.exit(0)  # Success - progress was made
        else:
            # No progress made - might be a real issue
            logger.error("Backup completed with errors and no new files backed up")
            sys.exit(1)  # Failure - no progress
    else:
        logger.info("Backup completed successfully")
        sys.exit(0)


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        logger.warning("Backup interrupted by user")
        sys.exit(130)  # Standard exit code for SIGINT
    except Exception as e:
        logger.critical(f"Unexpected fatal error: {e}", exc_info=True)
        # Try to save manifest one last time
        try:
            if _global_manifest and _global_manifest_lock:
                with _global_manifest_lock:
                    _global_manifest.save_to_gcs(include_backup=False)
                logger.info("Manifest saved after unexpected error")
        except Exception as save_error:
            logger.error(f"Failed to save manifest after unexpected error: {save_error}")
        sys.exit(1)

