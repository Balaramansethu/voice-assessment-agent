#!/bin/bash
# PR-610: Restore a PostgreSQL backup into a target database.
# Restores from a gzipped pg_dump file.
# Usage: ./scripts/restore.sh <backup_file> [target_host] [target_user] [target_db]
# Defaults: target_host=localhost, target_user=interview, target_db=interview

set -euo pipefail

# This script shells out to a HOST-installed psql/gunzip (unlike backup.sh, which
# routes pg_dump through `docker compose exec postgres` so it needs nothing installed
# locally) — a dev machine that only ever touches Postgres via Docker may not have
# either. Fail with an actionable message rather than a bare "command not found".
for bin in psql gunzip; do
    if ! command -v "$bin" >/dev/null 2>&1; then
        echo "✗ '$bin' not found on this machine." >&2
        echo "  Install the PostgreSQL client tools (e.g. 'brew install postgresql' on macOS)" >&2
        echo "  or restore via a throwaway container instead, e.g.:" >&2
        echo "    gunzip -c <backup_file> | docker run --rm -i pgvector/pgvector:pg16 \\" >&2
        echo "      psql -h <target_host> -U <target_user> -d <target_db>" >&2
        exit 1
    fi
done

if [ $# -lt 1 ]; then
    echo "Usage: $0 <backup_file> [target_host] [target_user] [target_db]"
    echo ""
    echo "Restore a gzipped pg_dump backup into a PostgreSQL database."
    echo ""
    echo "Arguments:"
    echo "  backup_file   Path to the .sql.gz backup file (required)"
    echo "  target_host   PostgreSQL host (default: localhost)"
    echo "  target_user   Database user (default: interview)"
    echo "  target_db     Database name (default: interview)"
    echo ""
    echo "Example (restore to local dev Postgres):"
    echo "  $0 backups/interview_backup_20260920_123456.sql.gz"
    echo ""
    echo "Example (restore to external database):"
    echo "  $0 backups/interview_backup_20260920_123456.sql.gz prod.example.com prod_user prod_db"
    exit 1
fi

BACKUP_FILE="$1"
TARGET_HOST="${2:-localhost}"
TARGET_USER="${3:-interview}"
TARGET_DB="${4:-interview}"

# Validate backup file exists and is readable
if [ ! -f "$BACKUP_FILE" ]; then
    echo "✗ Backup file not found: $BACKUP_FILE"
    exit 1
fi

if [ ! -r "$BACKUP_FILE" ]; then
    echo "✗ Backup file is not readable: $BACKUP_FILE"
    exit 1
fi

# Safety: if target is the local dev database, require explicit confirmation
# unless --force flag is present.
if [ "$TARGET_HOST" = "localhost" ] && [ "$TARGET_DB" = "interview" ]; then
    echo "⚠ Warning: You are about to restore to the local development database."
    echo "   Host: $TARGET_HOST, DB: $TARGET_DB"
    echo ""
    echo "This will OVERWRITE the existing database. This cannot be undone."
    read -p "Type 'restore' to confirm: " confirmation
    if [ "$confirmation" != "restore" ]; then
        echo "Restore cancelled."
        exit 1
    fi
fi

echo "Restoring from $BACKUP_FILE to $TARGET_HOST/$TARGET_DB..."
echo ""

# Extract (gzip) and pipe directly into psql for minimal disk usage.
# Uses PGPASSWORD for non-interactive auth if set; otherwise prompts.
gunzip < "$BACKUP_FILE" | \
    PGPASSWORD="${PGPASSWORD:-interview}" \
    psql -h "$TARGET_HOST" -U "$TARGET_USER" -d "$TARGET_DB" \
    --single-transaction \
    --exit-on-error

if [ $? -eq 0 ]; then
    echo ""
    echo "✓ Restore succeeded to $TARGET_HOST/$TARGET_DB"
    exit 0
else
    echo ""
    echo "✗ Restore failed"
    exit 1
fi
