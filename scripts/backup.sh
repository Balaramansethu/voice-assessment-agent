#!/bin/bash
# PR-609: Create an encrypted backup of the PostgreSQL database.
# Backs up the dev interview database and writes to backups/ directory.
# Usage: ./scripts/backup.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"
BACKUP_DIR="$REPO_DIR/backups"

# Ensure backups directory exists
mkdir -p "$BACKUP_DIR"

# Generate a timestamped backup filename
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
BACKUP_FILE="$BACKUP_DIR/interview_backup_${TIMESTAMP}.sql.gz"

echo "Backing up PostgreSQL database to $BACKUP_FILE..."

# Run pg_dump on the postgres service container; pipe to gzip for compression.
# The database is named 'interview' on the local dev Postgres.
docker compose exec -T postgres pg_dump \
    -U interview \
    --no-password \
    interview | gzip > "$BACKUP_FILE"

if [ $? -eq 0 ]; then
    SIZE=$(du -h "$BACKUP_FILE" | cut -f1)
    echo "✓ Backup succeeded: $BACKUP_FILE ($SIZE)"
    exit 0
else
    echo "✗ Backup failed"
    rm -f "$BACKUP_FILE"
    exit 1
fi
