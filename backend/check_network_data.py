"""Diagnose: what co-author data exists for a given researcher?"""
import os, sys, django
if not os.environ.get("DJANGO_SETTINGS_MODULE"):
    os.environ["DJANGO_SETTINGS_MODULE"] = "litrix_backend.settings"
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
django.setup()
from django.db import connection

LITRIX_ID = sys.argv[1] if len(sys.argv) > 1 else "Lit-000012"   # احلام

with connection.cursor() as cur:
    # Resolve user
    cur.execute("""
        SELECT "UserID", "FullName_Ar"
        FROM "Users"
        WHERE "Litrix_ID" ~* '^lit-[0-9]+$'
          AND CAST(SUBSTRING("Litrix_ID" FROM 5) AS INTEGER) = %s
    """, [int(LITRIX_ID.replace('Lit-', '').lstrip('0') or '0')])
    row = cur.fetchone()
    if not row:
        print(f"User not found for {LITRIX_ID}")
        sys.exit(1)
    user_id, name = row
    print(f"User: {name}  (UserID={user_id})")

    # Total papers
    cur.execute('SELECT COUNT(*) FROM "Authors" WHERE "UserID" = %s', [user_id])
    print(f"Papers (Authors links): {cur.fetchone()[0]}")

    # Internal co-authors (other Users on same papers)
    cur.execute("""
        SELECT COUNT(DISTINCT a2."UserID")
        FROM "Authors" a1
        JOIN "Authors" a2 ON a2."PaperID" = a1."PaperID"
                          AND a2."UserID" <> a1."UserID"
        WHERE a1."UserID" = %s
    """, [user_id])
    print(f"Internal co-authors: {cur.fetchone()[0]}")

    # ExternalAuthors entries
    cur.execute("""
        SELECT COUNT(*)
        FROM "Authors" a
        JOIN "ExternalAuthors" ea ON ea."PaperID" = a."PaperID"
        WHERE a."UserID" = %s
    """, [user_id])
    print(f"ExternalAuthors rows for her papers: {cur.fetchone()[0]}")

    # Papers with RawData_Log->'authorships' containing other names
    cur.execute("""
        SELECT COUNT(*)
        FROM "Authors" a
        JOIN "ResearchPaper" rp ON rp."PaperID" = a."PaperID"
        WHERE a."UserID" = %s
          AND rp."RawData_Log" ? 'authorships'
          AND jsonb_array_length(rp."RawData_Log"->'authorships') > 1
    """, [user_id])
    print(f"Papers with authorships array (1+ entries): {cur.fetchone()[0]}")

    # Sample authorships from one paper
    cur.execute("""
        SELECT rp."PaperID", LEFT(rp."Title", 60),
               jsonb_array_length(rp."RawData_Log"->'authorships') AS n_authors
        FROM "Authors" a
        JOIN "ResearchPaper" rp ON rp."PaperID" = a."PaperID"
        WHERE a."UserID" = %s
          AND rp."RawData_Log" ? 'authorships'
        ORDER BY rp."PaperID" DESC
        LIMIT 3
    """, [user_id])
    print("\nSample papers with authorships:")
    for pid, title, n in cur.fetchall():
        print(f"  PaperID={pid}  n_authors={n}  title: {title}")
