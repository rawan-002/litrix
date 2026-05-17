"""
Quick verifier - prints current orphan count and how many deletion
audit entries exist.

USAGE:
    python verify_orphans.py
"""
import os, sys
import django

if not os.environ.get("DJANGO_SETTINGS_MODULE"):
    os.environ["DJANGO_SETTINGS_MODULE"] = "litrix_backend.settings"
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
django.setup()

from django.db import connection

with connection.cursor() as cur:
    cur.execute(
        'SELECT COUNT(*) FROM "ResearchPaper" rp '
        'WHERE NOT EXISTS ('
        '  SELECT 1 FROM "Authors" a WHERE a."PaperID" = rp."PaperID")'
    )
    print("Remaining orphans:", cur.fetchone()[0])

    cur.execute(
        'SELECT COALESCE(rp."Source", \'(null)\'), COUNT(*) '
        'FROM "ResearchPaper" rp '
        'WHERE NOT EXISTS ('
        '  SELECT 1 FROM "Authors" a WHERE a."PaperID" = rp."PaperID") '
        'GROUP BY rp."Source" ORDER BY 2 DESC'
    )
    print("\nBy Source:")
    for src, n in cur.fetchall():
        print(f"  {src:<12} {n}")

    cur.execute(
        'SELECT COUNT(*) FROM "AuditLog" '
        'WHERE "Action" = %s',
        ["paper.delete.orphan"],
    )
    print("\nAudit entries for paper.delete.orphan:", cur.fetchone()[0])

    cur.execute(
        'SELECT COUNT(*) FROM "ResearchPaper"'
    )
    print("Total ResearchPaper rows now:", cur.fetchone()[0])
