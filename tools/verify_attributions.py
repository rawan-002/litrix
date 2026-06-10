import os
import sys
import time
import io
from dotenv import load_dotenv
import psycopg2
from serpapi import GoogleSearch

try:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', line_buffering=True)
except Exception:
    pass

load_dotenv()
SERP_KEY = os.getenv("SERP_API_KEY")


# Shared DB helper (single source — see litrix_db.py at repo root).
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from litrix_db import db


def normalize_title(t):
    return ''.join(c for c in (t or '').lower() if c.isalnum() or c.isspace()).strip()


def fetch_scholar_titles(scholar_id):
    titles = set()
    for sort in [None, "pubdate"]:
        start = 0
        while True:
            try:
                params = {
                    "engine": "google_scholar_author",
                    "author_id": scholar_id,
                    "api_key": SERP_KEY,
                    "num": 100,
                    "start": start,
                }
                if sort: params["sort"] = sort
                r = GoogleSearch(params).get_dict()
            except Exception:
                break
            articles = r.get("articles") or []
            if not articles:
                break
            for a in articles:
                t = a.get("title") or ""
                if t: titles.add(normalize_title(t))
            if len(articles) < 100:
                break
            start += 100
            time.sleep(0.5)
    return titles


def main():
    conn = db()
    cur = conn.cursor()

    cur.execute('''
        SELECT u."UserID", u."FullName_Ar", u."Scholar_ID"
        FROM "Users" u
        WHERE u."Scholar_ID" IS NOT NULL AND u."Scholar_ID" <> ''
          AND u."UserType" = 'Researcher'
        ORDER BY u."UserID"
    ''')
    researchers = cur.fetchall()
    print(f"Verifying {len(researchers)} researchers\n")

    total_deleted = 0
    for uid, name, scholar_id in researchers:
        print(f"\nUID={uid}  {name}")
        verified = fetch_scholar_titles(scholar_id)
        print(f"  Scholar verified: {len(verified)} papers")

        cur.execute('''
            SELECT a."AuthorID", rp."PaperID", rp."Title"
            FROM "Authors" a
            JOIN "ResearchPaper" rp ON rp."PaperID" = a."PaperID"
            WHERE a."UserID" = %s
        ''', [uid])

        deleted = 0
        for author_id, paper_id, title in cur.fetchall():
            if normalize_title(title) not in verified:
                cur.execute('DELETE FROM "Authors" WHERE "AuthorID" = %s', [author_id])
                deleted += 1
        if deleted:
            print(f"  Deleted {deleted} wrong attributions")
            total_deleted += deleted
        conn.commit()

    print(f"\nDone — total deleted: {total_deleted}")
    cur.close()
    conn.close()


if __name__ == "__main__":
    main()
