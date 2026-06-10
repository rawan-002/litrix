"""
Lightweight Python backup for the 7 researchers affected by scopus_attribution_fix.

=============================================================================
WHY THIS EXISTS
=============================================================================
The user is on Windows without pg_dump. Neon's Point-in-Time Recovery
already provides 7-day automatic restore, but a focused JSON snapshot of
the affected rows gives us:

  • A human-readable, diff-able record of exactly what existed BEFORE the fix.
  • A script-friendly format for ad-hoc spot-checks ("did UserID 89 lose paper X?").
  • Zero external dependencies — just psycopg2 (already installed).

Run BEFORE every --apply, even for a single researcher. The output file is
tagged with timestamp + UserID so reruns don't overwrite each other.

=============================================================================
USAGE
=============================================================================
    # Snapshot all 7 researchers (recommended before bulk --apply)
    python backend/scopus_backup_snapshot.py

    # Snapshot one researcher only (recommended for single-user testing)
    python backend/scopus_backup_snapshot.py --user-id 106

=============================================================================
"""

from __future__ import annotations

import argparse
import io
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import psycopg2
from psycopg2.extras import RealDictCursor
from dotenv import load_dotenv

try:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', line_buffering=True)
except Exception:
    pass

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")

# Must match the UserIDs hard-coded in scopus_attribution_fix.RESEARCHERS.
# Kept here independently so this script never has to import the other.
AFFECTED_USER_IDS = [6, 8, 81, 89, 92, 93, 106]

BACKUP_DIR = PROJECT_ROOT / "data" / "scopus_audit"


# Shared DB helper (single source — see litrix_db.py at repo root).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from litrix_db import db as db_connect


def snapshot_researcher(cur, user_id: int) -> dict:
    """Capture every row that scopus_attribution_fix would touch for this user.

    Tables included:
        • Users / Researcher (the metadata that we mutate via UPDATE)
        • Authors (the link table — heavy DELETE happens here)
        • ResearchPaper rows linked to this user via Authors
          (we DON'T delete these, but capture the state we'd UPDATE on overlap)
    """
    snapshot: dict = {"user_id": user_id}

    cur.execute(
        '''
        SELECT u."UserID", u."FullName_Ar", u."FirstName", u."MiddleName",
               u."LastName", u."Email", u."Scholar_ID", u."Litrix_ID",
               r."Scopus_ID", r."ORCID_ID", r."AcademicRank",
               r."OpenAlex_AuthorID", r."H_Index", r."LastSyncedAt",
               r."CitationsByYear"
        FROM "Users" u
        LEFT JOIN "Researcher" r ON r."UserID" = u."UserID"
        WHERE u."UserID" = %s
        ''',
        (user_id,),
    )
    snapshot["researcher_meta"] = dict(cur.fetchone() or {})

    cur.execute(
        '''
        SELECT a."AuthorLinkID", a."UserID", a."PaperID", a."AuthorOrder",
               a."IsCorrespondingAuthor", a."MappingConfidence",
               a."MappingCriteria", a."AuthorNameRaw", a."Is_Verified"
        FROM "Authors" a
        WHERE a."UserID" = %s
        ORDER BY a."AuthorLinkID"
        ''',
        (user_id,),
    )
    snapshot["authors_rows"] = [dict(r) for r in cur.fetchall()]

    cur.execute(
        '''
        SELECT rp."PaperID", rp."JournalID", rp."Title", rp."DOI",
               rp."PubYear", rp."Volume", rp."Issue", rp."Pages",
               rp."Source", rp."ScrapedAt", rp."IsVerified"
        FROM "ResearchPaper" rp
        WHERE rp."PaperID" IN (
            SELECT "PaperID" FROM "Authors" WHERE "UserID" = %s
        )
        ORDER BY rp."PaperID"
        ''',
        (user_id,),
    )
    snapshot["linked_papers"] = [dict(r) for r in cur.fetchall()]

    snapshot["counts"] = {
        "authors_rows": len(snapshot["authors_rows"]),
        "linked_papers": len(snapshot["linked_papers"]),
    }
    return snapshot


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--user-id", type=int, default=None,
                    help="Snapshot one UserID only. Default: all 7 affected.")
    args = ap.parse_args()

    user_ids = [args.user_id] if args.user_id else AFFECTED_USER_IDS
    if args.user_id and args.user_id not in AFFECTED_USER_IDS:
        print(f"WARNING: UserID {args.user_id} is NOT in the affected list. "
              f"Snapshotting anyway.")

    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    suffix = f"u{args.user_id}" if args.user_id else "all"
    out_path = BACKUP_DIR / f"snapshot_{suffix}_{timestamp}.json"

    print(f"Connecting to database...")
    conn = db_connect()
    try:
        all_snapshots = []
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            for uid in user_ids:
                print(f"  -> Snapshotting UserID {uid}...")
                snap = snapshot_researcher(cur, uid)
                all_snapshots.append(snap)
                print(f"     authors_rows={snap['counts']['authors_rows']:>4}, "
                      f"linked_papers={snap['counts']['linked_papers']:>4}")
    finally:
        conn.close()

    payload = {
        "schema_version": 1,
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "scope": "single_user" if args.user_id else "all_affected",
        "snapshots": all_snapshots,
    }
    out_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    size_kb = out_path.stat().st_size / 1024
    print(f"\nSnapshot saved:\n  {out_path}")
    print(f"  Size: {size_kb:.1f} KB")
    print(f"\nIf anything goes wrong, you can:")
    print(f"  1. Restore from Neon Point-in-Time Recovery (preferred), OR")
    print(f"  2. Use this JSON to manually re-insert affected rows.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
