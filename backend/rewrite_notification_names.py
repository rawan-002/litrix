"""Rewrite Arabic researcher names to English in EXISTING notifications.

my_reports_views.py used to bake the Arabic name into campaign notification
Title/Message ("Report received from <arabic>"). The code is fixed for new
notifications; this backfills the old ones. Each campaign.submission_received
notification's Metadata carries researcher_id, so we look up that researcher's
English name (ScholarDisplayName -> First+Last) and string-replace their Arabic
name in Title + Message.

Idempotent + guarded: only rows still containing the Arabic name AND where an
English name exists are touched. Dry-run by default; --commit to write.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from litrix_db import db, setup_utf8_stdout

setup_utf8_stdout()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--commit', action='store_true')
    args = ap.parse_args()
    conn = db(); cur = conn.cursor()

    # Notifications that embed a researcher's name and know who via Metadata.
    cur.execute('''
        SELECT n."NotificationID", n."Title", n."Message",
               (n."Metadata"->>'researcher_id')::int AS rid
        FROM "Notification" n
        WHERE n."Type" = 'campaign.submission_received'
          AND n."Metadata" ? 'researcher_id'
    ''')
    rows = cur.fetchall()

    # Map researcher_id -> (arabic, english)
    ids = list({r[3] for r in rows if r[3]})
    names = {}
    if ids:
        cur.execute('''
            SELECT u."UserID", u."FullName_Ar",
                   COALESCE(NULLIF(u."ScholarDisplayName", ''),
                            NULLIF(TRIM(CONCAT_WS(' ', u."FirstName", u."LastName")), '')) AS en
            FROM "Users" u WHERE u."UserID" = ANY(%s)
        ''', [ids])
        for uid, ar, en in cur.fetchall():
            names[uid] = (ar, en)

    planned = []
    for nid, title, msg, rid in rows:
        ar, en = names.get(rid, (None, None))
        if not ar or not en or en == ar:
            continue
        # only English chars in en -> a real English name
        if not any(c.isascii() and c.isalpha() for c in en):
            continue
        new_title = (title or '').replace(ar, en)
        new_msg = (msg or '').replace(ar, en)
        if new_title != title or new_msg != msg:
            planned.append((nid, new_title, new_msg, ar, en))

    print('Notifications to rewrite: %d of %d' % (len(planned), len(rows)))
    for nid, nt, nm, ar, en in planned[:20]:
        print('  #%s: %s -> %s' % (nid, ar, en))
    if len(planned) > 20:
        print('  ... +%d more' % (len(planned) - 20))

    if not args.commit:
        print('\nDRY-RUN. Re-run with --commit to apply.')
        return
    for nid, nt, nm, ar, en in planned:
        cur.execute('UPDATE "Notification" SET "Title"=%s, "Message"=%s WHERE "NotificationID"=%s',
                    [nt, nm, nid])
    conn.commit()
    print('\nCOMMITTED: %d notifications rewritten.' % len(planned))


if __name__ == '__main__':
    main()
