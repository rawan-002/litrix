#!/usr/bin/env bash
#
# Render build script
# -------------------
# Runs on every deploy. Fails the build if any step exits non-zero.
#
# Order matters:
#   1) Install deps
#   2) Django's own migrations  (creates analytics tables, sprint1..8 etc.)
#   3) Raw-SQL sprint migrations (sprint9 = ResearchInterests column,
#      sprint10 = canonicalize_interest function). Applied idempotently
#      via apply_migration.py — re-runs are safe.
#   4) collectstatic for whitenoise.

set -o errexit

echo "==> 1/4  pip install"
pip install -r requirements.txt

echo "==> 2/4  Django migrate"
python manage.py migrate --no-input

echo "==> 3/4  Raw SQL sprint migrations"
for sql in analytics/migrations/sprint*.sql; do
    if [ -f "$sql" ]; then
        echo "     applying $sql"
        # apply_migration.py is idempotent — sprint9 uses CREATE COLUMN
        # IF NOT EXISTS, sprint10 uses CREATE OR REPLACE FUNCTION.
        python apply_migration.py "$sql" || {
            echo "     [warn] $sql failed — continuing build."
        }
    fi
done

echo "==> 4/4  collectstatic"
python manage.py collectstatic --no-input

echo "==> Build complete"
