"""
backup.py — Dumps the joindev schema and pushes it to a private GitHub repo.

Usage:
  python backup.py            # Runs the dump + upload
  python backup.py --dry-run  # Just prints what would happen
"""

import os
import sys
import gzip
import time
import base64
import logging
import subprocess
import requests
from datetime import datetime, timezone

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("joindev.backup")

# ---------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------
DATABASE_URL = os.getenv("DATABASE_URL")
GITHUB_TOKEN = os.getenv("BACKUP_GITHUB_TOKEN")
GITHUB_REPO = os.getenv("BACKUP_GITHUB_REPO")   # e.g. "XPchungis44/JoinDev-Backups"
GITHUB_BRANCH = os.getenv("BACKUP_GITHUB_BRANCH", "main")
GITHUB_PATH = "backups"                           # Folder in the repo

if not DATABASE_URL:
    log.error("DATABASE_URL not set")
    sys.exit(1)

if not GITHUB_TOKEN or not GITHUB_REPO:
    log.error("BACKUP_GITHUB_TOKEN and BACKUP_GITHUB_REPO must be set")
    sys.exit(1)


def build_dump_filename() -> str:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H%M%S")
    return f"joindev_{ts}.sql.gz"


def dump_database() -> bytes:
    """Runs pg_dump and returns the gzipped bytes."""
    log.info("Dumping database...")

    # pg_dump writes to stdout, we capture it
    cmd = [
        "pg_dump",
        "--schema=joindev",       # Only our schema
        "--no-owner",              # Skip ownership (portable)
        "--no-acl",                # Skip access privileges
        DATABASE_URL,
    ]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            check=True,
            timeout=120,
        )
    except subprocess.CalledProcessError as e:
        log.error(f"pg_dump failed: {e.stderr.decode()[:500]}")
        raise
    except FileNotFoundError:
        log.error("pg_dump not found. Install postgresql-client.")
        raise

    # Gzip the output
    gzipped = gzip.compress(result.stdout, compresslevel=6)
    log.info(f"Dump complete: {len(gzipped):,} bytes gzipped")
    return gzipped


def upload_to_github(filename: str, content: bytes) -> bool:
    """Pushes the backup file to GitHub via the Contents API."""
    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{GITHUB_PATH}/{filename}"
    headers = {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
    }

    payload = {
        "message": f"Backup {filename}",
        "content": base64.b64encode(content).decode(),
        "branch": GITHUB_BRANCH,
    }

    try:
        r = requests.put(url, json=payload, headers=headers, timeout=30)
        if r.status_code in (200, 201):
            log.info(f"Uploaded: {GITHUB_PATH}/{filename}")
            return True
        log.error(f"GitHub upload failed: {r.status_code} {r.text[:300]}")
        return False
    except Exception as e:
        log.error(f"GitHub upload error: {e}")
        return False


def cleanup_old_backups(keep: int = 14):
    """Deletes backups older than the N most recent ones."""
    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{GITHUB_PATH}"
    headers = {"Authorization": f"token {GITHUB_TOKEN}"}

    try:
        r = requests.get(url, headers=headers, timeout=15)
        if r.status_code != 200:
            log.warning(f"Could not list backups: {r.status_code}")
            return

        files = [f for f in r.json() if f["name"].startswith("joindev_")]
        files.sort(key=lambda f: f["name"], reverse=True)

        if len(files) <= keep:
            log.info(f"Only {len(files)} backups — nothing to delete")
            return

        for f in files[keep:]:
            del_url = f["url"]
            del_payload = {
                "message": f"Delete old backup {f['name']}",
                "sha": f["sha"],
                "branch": GITHUB_BRANCH,
            }
            dr = requests.delete(del_url, json=del_payload, headers=headers, timeout=15)
            if dr.status_code in (200, 204):
                log.info(f"Deleted old backup: {f['name']}")
            else:
                log.warning(f"Failed to delete {f['name']}: {dr.status_code}")
    except Exception as e:
        log.warning(f"Cleanup error: {e}")


# ---------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------
if __name__ == "__main__":
    dry_run = "--dry-run" in sys.argv

    filename = build_dump_filename()
    log.info(f"Backup target: {GITHUB_REPO}/{GITHUB_PATH}/{filename}")

    if dry_run:
        log.info("DRY RUN — no upload will happen")
        sys.exit(0)

    try:
        content = dump_database()
        if upload_to_github(filename, content):
            cleanup_old_backups(keep=14)
            log.info("✅ Backup complete")
        else:
            log.error("❌ Backup upload failed")
            sys.exit(1)
    except Exception as e:
        log.error(f"❌ Backup failed: {e}")
        sys.exit(1)
