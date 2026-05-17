"""
EMERGENCY: Restore Ahlam + Abdulkareem PAPERS ONLY after a
destructive reset_test_researchers --unregister run.

DEFAULT (papers-only) recovers:
  1. Authors links — by scanning every ResearchPaper.RawData_Log
     for an authorship whose author.id / author.orcid matches the
     known OpenAlex ID / ORCID.
  2. Researcher.OpenAlex_AuthorID + ORCID_ID + ResearchInterests
     (needed for future scrapes + Network feature to work)

DOES NOT touch (so they stay "scraped-only / unregistered"):
  * Users.Email, Users.FirstName/MiddleName/LastName, Users.FullName_Ar
  * Users.UserType, Users.AccountStatus
  * Department.HeadID

WITH --full it ALSO restores registration metadata + HoD role
(only use if you want the original login back).

WHY THIS WORKS
  reset_test_researchers deleted rows from the Authors table only.
  The ResearchPaper rows themselves stayed intact, INCLUDING the
  RawData_Log JSONB blob that lists every co-author with their
  OpenAlex ID. Matching on author.id rebuilds the links with 100%
  accuracy — no name disambiguation needed.

USAGE
  python restore_test_researchers.py                  # dry-run, papers only
  python restore_test_researchers.py --apply          # apply, papers only
  python restore_test_researchers.py --only ahlam --apply
  python restore_test_researchers.py --apply --full   # also restore login + HoD
"""
import os, sys, argparse, json
import django

if not os.environ.get("DJANGO_SETTINGS_MODULE"):
    os.environ["DJANGO_SETTINGS_MODULE"] = "litrix_backend.settings"
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
django.setup()

from django.db import connection, transaction

TARGETS = {
    'ahlam': {
        'user_id':            12,
        'litrix_id':          'Lit-000007',
        'scholar_id':         'lURTCEIAAAAJ',
        'openalex_author_id': 'A5037835330',
        'orcid_id':           '0000-0002-5923-5950',
        'email':              'ra002wan@gmail.com',
        'first_name':         'احلام',
        'middle_name':        'التهامي محمد',
        'last_name':          'النابلي',
        'full_name_ar':       'احلام التهامي محمد النابلي',
        'user_type':          'Researcher',
        'account_status':     'Active',
        'interests':          ['Data warehouse', 'NoSQL', 'Ontology'],
        'head_of_dept':       None,
    },
    'abdulkareem': {
        'user_id':            1,
        'litrix_id':          'Lit-000001',
        'scholar_id':         'JSQbyBgAAAAJ',
        'openalex_author_id': 'A5045695293',
        'orcid_id':           '0000-0003-3658-1284',
        'email':              'ra20awn@gmail.com',
        'first_name':         'abdulkareem',
        'middle_name':        'عوضه سعدي الحريري',
        'last_name':          'a',
        'full_name_ar':       'عبدالكريم الزهراني',
        'user_type':          'HoD',
        'account_status':     'Active',
        'interests':          ['Artificial Intelligence',
                               'Computational Intelligence',
                               'Ambient intelligence',
                               'Agent and Multi-agent'],
        'head_of_dept':       'النظم والشبكات',
    },
}


def find_paper_ids_for_openalex(cur, openalex_id):
    cur.execute(
        '''
        SELECT DISTINCT rp."PaperID"
        FROM "ResearchPaper" rp
        CROSS JOIN LATERAL jsonb_array_elements(
            CASE WHEN jsonb_typeof(rp."RawData_Log"->'authorships') = 'array'
                 THEN rp."RawData_Log"->'authorships'
                 ELSE '[]'::jsonb END
        ) AS ship
        WHERE REGEXP_REPLACE(COALESCE(ship->'author'->>'id', ''), '^.*/', '') = %s
        ''',
        [openalex_id],
    )
    return [row[0] for row in cur.fetchall()]


def find_paper_ids_for_orcid(cur, orcid):
    cur.execute(
        '''
        SELECT DISTINCT rp."PaperID"
        FROM "ResearchPaper" rp
        CROSS JOIN LATERAL jsonb_array_elements(
            CASE WHEN jsonb_typeof(rp."RawData_Log"->'authorships') = 'array'
                 THEN rp."RawData_Log"->'authorships'
                 ELSE '[]'::jsonb END
        ) AS ship
        WHERE REGEXP_REPLACE(COALESCE(ship->'author'->>'orcid', ''), '^.*/', '') = %s
        ''',
        [orcid],
    )
    return [row[0] for row in cur.fetchall()]


def restore_one(key, target, apply, full):
    uid       = target['user_id']
    label_ar  = target['full_name_ar']  # only for display in this report
    print()
    print(f"== {label_ar}  (UserID={uid}, Litrix={target['litrix_id']}) ==")

    with connection.cursor() as cur:
        # 1. Discover papers via OpenAlex ID + ORCID (union, dedupe)
        paper_ids_oa    = set(find_paper_ids_for_openalex(cur, target['openalex_author_id']))
        paper_ids_orcid = set(find_paper_ids_for_orcid(cur, target['orcid_id']))
        paper_ids       = paper_ids_oa | paper_ids_orcid

        if paper_ids:
            cur.execute(
                'SELECT "PaperID" FROM "Authors" WHERE "UserID" = %s AND "PaperID" = ANY(%s)',
                [uid, list(paper_ids)],
            )
            already = {row[0] for row in cur.fetchall()}
        else:
            already = set()
        missing = paper_ids - already

        print(f"  papers found via OpenAlex  : {len(paper_ids_oa)}")
        print(f"  papers found via ORCID     : {len(paper_ids_orcid)}")
        print(f"  union (distinct)           : {len(paper_ids)}")
        print(f"  already linked             : {len(already)}")
        print(f"  will create Authors rows   : {len(missing)}")
        print(f"  will restore Researcher    : OpenAlex={target['openalex_author_id']}, "
              f"ORCID={target['orcid_id']}, Interests={len(target['interests'])}")
        if full:
            print(f"  + FULL MODE: will ALSO restore Users (Email, Name, "
                  f"UserType={target['user_type']}, Status=Active)")
            if target['head_of_dept']:
                print(f"  + FULL MODE: will set Department '{target['head_of_dept']}'"
                      f".HeadID -> {uid}")
        else:
            print(f"  (papers-only mode: Users.Email/Name/UserType/Status untouched,")
            print(f"   Department.HeadID untouched -> they stay 'unregistered' stubs)")

        if not apply:
            return

        with transaction.atomic():
            # ---- Always: Researcher row (needed for Network + future scrapes)
            cur.execute(
                '''
                UPDATE "Researcher"
                   SET "OpenAlex_AuthorID" = %s,
                       "ORCID_ID"          = %s,
                       "ResearchInterests" = %s::jsonb,
                       "ResearchInterestsUpdatedAt" = NOW()
                 WHERE "UserID" = %s
                ''',
                [target['openalex_author_id'], target['orcid_id'],
                 json.dumps(target['interests'], ensure_ascii=False), uid],
            )

            # ---- Always: Authors links
            if missing:
                values = [(pid, uid) for pid in missing]
                args_str = ','.join(
                    cur.mogrify('(%s,%s)', v).decode('utf-8') for v in values
                )
                cur.execute(
                    f'INSERT INTO "Authors" ("PaperID", "UserID") VALUES {args_str}'
                )

            # ---- Only with --full: registration metadata + HoD role
            if full:
                cur.execute(
                    '''
                    UPDATE "Users"
                       SET "Email"         = %s,
                           "FirstName"     = %s,
                           "MiddleName"    = %s,
                           "LastName"      = %s,
                           "FullName_Ar"   = %s,
                           "UserType"      = %s,
                           "AccountStatus" = %s
                     WHERE "UserID" = %s
                    ''',
                    [target['email'], target['first_name'], target['middle_name'],
                     target['last_name'], target['full_name_ar'],
                     target['user_type'], target['account_status'], uid],
                )
                if target['head_of_dept']:
                    cur.execute(
                        'UPDATE "Department" SET "HeadID" = %s WHERE "DepartmentName" = %s',
                        [uid, target['head_of_dept']],
                    )

        print(f"  [ok] restored.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--only', choices=['ahlam', 'abdulkareem'], default=None)
    ap.add_argument('--apply', action='store_true')
    ap.add_argument('--yes', action='store_true')
    ap.add_argument('--full', action='store_true',
                    help='Also restore Users (Email, Name, UserType, Status) '
                         'and Department.HeadID. Default is papers-only.')
    args = ap.parse_args()

    keys = [args.only] if args.only else list(TARGETS.keys())

    print("=" * 70)
    print(f"RESTORE — {'FULL (papers + login + HoD)' if args.full else 'PAPERS ONLY'}")
    print("=" * 70)
    for k in keys:
        restore_one(k, TARGETS[k], apply=False, full=args.full)

    if not args.apply:
        print()
        print("DRY RUN — nothing was written. Re-run with --apply to commit.")
        return

    if not args.yes:
        print()
        prompt = ("Type 'yes' to restore FULL (papers + login + HoD): "
                  if args.full else
                  "Type 'yes' to restore PAPERS ONLY: ")
        if input(prompt).strip().lower() != 'yes':
            print("Aborted.")
            return

    print()
    print("=" * 70)
    print("Applying restore...")
    print("=" * 70)
    for k in keys:
        try:
            restore_one(k, TARGETS[k], apply=True, full=args.full)
        except Exception as e:
            print(f"  [FAIL] {e}")
    print()
    print("Done.")


if __name__ == "__main__":
    main()
