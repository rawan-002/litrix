"""
Link LIKELY_MATCH rows from missing_papers_diagnosed.csv
========================================================
The diagnose step already identified, with high confidence (≥0.85
similarity, often perfect), that these "missing" papers are actually
in the DB — they were just missed by NormalizedTitle exact match.

This script:
  1. Reads missing_papers_diagnosed.csv
  2. For each row whose verdict is LIKELY_MATCH:
       - Resolves the user_id from scholar_id
       - Inserts Authors(PaperID = best_match_paper_id, UserID)
         (ON CONFLICT DO NOTHING — idempotent)
  3. Prints a summary.

POSSIBLE_MATCH rows are NOT auto-linked — they need human review.
NOT_IN_DB rows are left alone (those are genuine new papers).

USAGE
  python link_likely_matches.py             # dry-run (default)
  python link_likely_matches.py --apply
  python link_likely_matches.py --apply --min-similarity 0.90
"""
import os, sys, argparse, csv
import django

if not os.environ.get("DJANGO_SETTINGS_MODULE"):
    os.environ["DJANGO_SETTINGS_MODULE"] = "litrix_backend.settings"
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
django.setup()

from django.db import connection, transaction


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", default="missing_papers_diagnosed.csv")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--min-similarity", type=float, default=0.85,
                    help="Only link rows with similarity >= this. "
                         "Default 0.85.")
    args = ap.parse_args()

    if not os.path.exists(args.csv):
        print(f"{args.csv} not found. Run diagnose_missing_papers.py first.")
        return

    # Read all rows
    with open(args.csv, encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))

    # Filter to LIKELY_MATCH above threshold
    candidates = [
        r for r in rows
        if float(r.get("similarity_score") or 0) >= args.min_similarity
        and r.get("best_match_paper_id")
    ]

    if not candidates:
        print(f"No rows above similarity {args.min_similarity}.")
        return

    # Resolve scholar_id -> user_id (cached)
    scholar_to_uid = {}
    with connection.cursor() as cur:
        unique_sids = list({r["scholar_id"] for r in candidates})
        cur.execute(
            'SELECT "Scholar_ID", "UserID" FROM "Users" '
            'WHERE "Scholar_ID" = ANY(%s)',
            [unique_sids],
        )
        for sid, uid in cur.fetchall():
            scholar_to_uid[sid] = uid

    print("=" * 70)
    print(f"Candidates with similarity >= {args.min_similarity}: {len(candidates)}")
    print(f"Mode: {'APPLY' if args.apply else 'DRY-RUN'}")
    print("=" * 70)

    # Group by user for nice report
    from collections import defaultdict
    by_user = defaultdict(list)
    for r in candidates:
        sid = r["scholar_id"]
        uid = scholar_to_uid.get(sid)
        if uid is None:
            print(f"  [WARN] No Users row for Scholar_ID={sid} — skip "
                  f"{r['missing_title'][:50]!r}")
            continue
        by_user[(sid, uid)].append(r)

    total_inserts = 0
    skipped_existing = 0

    for (sid, uid), papers in by_user.items():
        print()
        print(f"== Scholar_ID={sid}  UserID={uid}  ({len(papers)} candidates) ==")

        # Check which Authors rows already exist
        pids = [int(p["best_match_paper_id"]) for p in papers]
        with connection.cursor() as cur:
            cur.execute(
                'SELECT "PaperID" FROM "Authors" '
                'WHERE "UserID" = %s AND "PaperID" = ANY(%s)',
                [uid, pids],
            )
            existing = {row[0] for row in cur.fetchall()}

        new_pids = [pid for pid in pids if pid not in existing]
        skipped_existing += len(pids) - len(new_pids)

        print(f"   already linked : {len(pids) - len(new_pids)}")
        print(f"   will link      : {len(new_pids)}")
        if new_pids[:5]:
            for p in papers[:5]:
                pid = int(p["best_match_paper_id"])
                marker = "(new)" if pid in new_pids else "(exists)"
                print(f"     {marker} [{pid}] {p['missing_title'][:60]!r}")
            if len(papers) > 5:
                print(f"     ... and {len(papers) - 5} more")

        if args.apply and new_pids:
            with connection.cursor() as cur:
                values = [(pid, uid) for pid in new_pids]
                args_str = ",".join(
                    cur.mogrify("(%s,%s)", v).decode("utf-8") for v in values
                )
                cur.execute(
                    f'INSERT INTO "Authors" ("PaperID", "UserID") VALUES {args_str} '
                    f'ON CONFLICT DO NOTHING'
                )
            total_inserts += len(new_pids)

    print()
    print("=" * 70)
    if args.apply:
        print(f"Done. Inserted {total_inserts} Authors rows. "
              f"Skipped {skipped_existing} already-existing.")
    else:
        print("DRY RUN — re-run with --apply to commit.")


if __name__ == "__main__":
    main()
