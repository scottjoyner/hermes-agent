#!/usr/bin/env python3
"""
NAS Auto-Ingest Watcher
Monitors NAS directories for new files and triggers ingestion pipeline.
"""

import os
import sys
import time
import hashlib
import logging
from pathlib import Path
from datetime import datetime

# Configuration
NAS_PATH = "/media/scott/NAS/fileserver"
WATCH_DIRS = ["bodycam", "dashcam", "audio"]
LOG_FILE = "/var/log/nas-watcher.log"
STATE_FILE = "/tmp/nas-watcher-state.json"

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)


def compute_file_hash(filepath):
    """Compute SHA256 hash of a file."""
    sha256_hash = hashlib.sha256()
    with open(filepath, "rb") as f:
        for byte_block in iter(lambda: f.read(4096), b""):
            sha256_hash.update(byte_block)
    return sha256_hash.hexdigest()


def get_file_metadata(filepath):
    """Get file metadata."""
    stat = os.stat(filepath)
    return {
        "size": stat.st_size,
        "modified": datetime.fromtimestamp(stat.st_mtime).isoformat(),
        "created": datetime.fromtimestamp(stat.st_ctime).isoformat(),
        "hash": compute_file_hash(filepath)
    }


def process_new_file(filepath):
    """Process a new file for ingestion."""
    logger.info(f"Processing new file: {filepath}")
    
    # Get file metadata
    metadata = get_file_metadata(filepath)
    
    # Determine file type and appropriate processing
    ext = Path(filepath).suffix.lower()
    if ext in ['.mp4', '.mov', '.avi']:
        logger.info(f"Video file detected: {filepath}")
        # Trigger video processing pipeline
    elif ext in ['.mp3', '.wav', '.flac']:
        logger.info(f"Audio file detected: {filepath}")
        # Trigger audio processing pipeline
    elif ext in ['.jpg', '.jpeg', '.png']:
        logger.info(f"Image file detected: {filepath}")
        # Trigger image processing pipeline
    else:
        logger.info(f"Unknown file type: {ext}")
    
    # Log metadata for neo4j indexing
    logger.info(f"File metadata: {metadata}")
    
    # Trigger Neo4j ingestion
    trigger_neo4j_ingest(filepath, metadata)
    
    # Trigger Nextcloud sync
    trigger_nextcloud_sync(filepath)
    
    logger.info(f"File processing complete: {filepath}")


def trigger_neo4j_ingest(filepath, metadata):
    """Trigger Neo4j ingestion pipeline."""
    logger.info(f"Triggering Neo4j ingestion for: {filepath}")
    # In a real implementation, this would call the Neo4j ingestion API
    # or write to a queue for processing by the ingestion service


def trigger_nextcloud_sync(filepath):
    """Trigger Nextcloud sync."""
    logger.info(f"Triggering Nextcloud sync for: {filepath}")
    # In a real implementation, this would use Nextcloud API to sync files


def get_existing_hashes():
    """Get existing file hashes from state file."""
    try:
        with open(STATE_FILE, 'r') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state):
    """Save file state to state file."""
    with open(STATE_FILE, 'w') as f:
        json.dump(state, f)


def main():
    """Main watcher loop."""
    logger.info("Starting NAS Auto-Ingest Watcher")
    logger.info(f"Watching directories: {[os.path.join(NAS_PATH, d) for d in WATCH_DIRS]}")
    
    state = get_existing_hashes()
    
    while True:
        try:
            for watch_dir in WATCH_DIRS:
                watch_path = os.path.join(NAS_PATH, watch_dir)
                if not os.path.exists(watch_path):
                    logger.warning(f"Watch directory does not exist: {watch_path}")
                    continue
                
                for root, dirs, files in os.walk(watch_path):
                    for filename in files:
                        filepath = os.path.join(root, filename)
                        file_hash = compute_file_hash(filepath)
                        
                        if filepath not in state or state[filepath].get('hash') != file_hash:
                            logger.info(f"New or modified file detected: {filepath}")
                            process_new_file(filepath)
                            
                            # Update state
                            state[filepath] = {
                                'hash': file_hash,
                                'size': os.path.getsize(filepath),
                                'modified': datetime.fromtimestamp(
                                    os.path.getmtime(filepath)
                                ).isoformat()
                            }
            
            # Save state
            save_state(state)
            
            # Wait before next check
            time.sleep(60)
            
        except Exception as e:
            logger.error(f"Error in watcher loop: {e}")
            time.sleep(60)


if __name__ == "__main__":
    main()
