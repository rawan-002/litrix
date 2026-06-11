"""
Reset test researchers
======================
Wipes scraped + registration data for researchers so they revert to
the "scraped-only" state -- exactly like a researcher the scraper
discovered but who never created a Litrix account.

THREE LEVELS OF CLEANUP
-----------------------
default (--apply, no other flags)
    Wipes scraped research data only. Keeps account intact.

--unassign-hod
    Adds HoD demotion: clears Department.HeadID + demotes UserType.

--unregister  (IMPLIES --unassign-hod)
    Full unregister: removes login + personal data. Users row stays
    so FK references stay valid. After this, the account looks like
    a "scraped-only" stub.

ALWAYS KEPT
    Users row, Users.Litrix_ID, Users.Scholar_ID, Researcher row,
    Works_In, ResearchPaper.

AUDITLOG
    Tries to write a snapshot into AuditLog for rollback purposes.
    Auto-detects the table's column layout at startup. If the table
    doesn't exist or has an incompatible schema, the snapshot is
    skipped (with a warning) and the reset proceeds anyway.
    Pass --no-audit to skip the snapshot entirely.
"""
import os, sys, argparse, json
import django
from datetime import datetime, timezone

if not os.environ.get("DJANGO_SETTINGS_MODULE"):
    os.environ["DJANGO_SETTINGS_MODULE"] = "litrix_backend.settings"
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
django.setup()

from django.db import connection, transaction

DEFAULT_SCHOLAR_IDS = [
    "lURTCEIAAAAJ",   # Ahlam
    "JSQbyBgAAAAJ",   # Abdulkarim
]


# --------------------------------------------------------------------
# AuditLog schema introspection
# --------------------------------------------------------------------
# Different Litrix DBs in the wild have slightly different AuditLog
# layouts (some have EntityName, some don't, some use Details vs
# OldValue, etc.). Rather than hard-coding, we discover what columns
# exist and build the INSERT to match.

_AUDIT_INSERT_SQL = None  # cached after first call
_AUDIT_PARAM_FN   = None


def _discover_audit_schema():
    """Inspect AuditLog and decide how to insert. Returns (sql, fn) or (None, None)."""
    global _AUDIT_INSERT_SQL, _AUDIT_PARAM_FN
    if _AUDIT_INSERT_SQL is not None or _AUDIT_PARAM_FN is False:
        return _AUDIT_INSERT_SQL, _AUDIT_PARAM_FN

    with connection.cursor() as cur:
        cur.execute('''
            SELECT column_name, data_type
              FROM information_schema.columns
             WHERE table_name = 'AuditLog'
        ''')
        cols = {name: dtype for name, dtype in cur.fetchall()}

    if not cols:
        print("    [warn] AuditLog table not found — snapshots disabled.")
        _AUDIT_PARAM_FN = False
        return None, None

    # Required: a UserID column + an Action column + a timestamp.
    # Optional: EntityName, EntityID, OldValue/Details/Payload.
    have = lambda *names: next((n for n in names if n in cols), None)

    col_user      = have('UserID', 'userid', 'user_id')
    col_action    = have('Action', 'action')
    col_created   = have('CreatedAt', 'createdat', 'created_at', 'Timestamp', 'timestamp')
    col_entity_n  = have('EntityName', 'entityname', 'entity_name', 'TableName')
    col_entity_id = have('EntityID', 'entityid', 'entity_id', 'TargetID')
    col_payload   = have('OldValue', 'oldvalue', 'old_value',
                         'Details', 'details', 'Payload', 'payload',
                         'Data', 'data', 'Snapshot', 'snapshot')

    if not (col_user and col_action and col_created):
        print(f"    [warn] AuditLog missing required cols (have {list(cols)}) -- "
              f"snapshots disabled.")
        _AUDIT_PARAM_FN = False
        return None, None

    # Build the dynamic INSERT
    insert_cols, placeholders = [col_user, col_action, col_created], ['%s', '%s', '%s']
    if col_entity_n:
        insert_cols.append(col_entity_n);  placeholders.append('%s')
    if col_entity_id:
        insert_cols.append(col_entity_id); placeholders.append('%s')
    if col_payload:
        # Decide whether to cast — only if JSON/JSONB
        cast = '::jsonb' if cols[col_payload] in ('jsonb',) else (
               '::json' if cols[col_payload] in ('json',) else '')
        insert_cols.append(col_payload);   placeholders.append('%s' + cast)

    sql = (f'INSERT INTO "AuditLog" ('
           + ', '.join(f'"{c}"' for c in insert_cols)
           + f') VALUES ({", ".join(placeholders)})')

    def make_params(uid, action, payload_json):
        p = [uid, action, datetime.now(timezone.utc)]
        if col_entity_n:  p.append('Researcher')
        if col_entity_id: p.append(uid)
        if col_payload:   p.append(payload_json)
        return p

    print(f"    [info] AuditLog INSERT: cols = {insert_cols}")
    _AUDIT_INSERT_SQL = sql
    _AUDIT_PARAM_FN   = make_params
    return sql, make_params


# --------------------------------------------------------------------

def find_targets(scholar_ids=None, litrix_ids=None):
    where, params = [], []
    if scholar_ids:
        where.append('u."Scholar_ID" = ANY(%s)')
        params.append(scholar_ids)
    if litrix_ids:
        where.append('u."Litrix_ID" = ANY(%s)')
        params.append(litrix_ids)
    if not where:
        return []

    sql = f'''
        SELECT u."UserID",
               u."Litrix_ID",
               u."Scholar_ID",
               u."UserType",
               u."Email",
               u."FirstName",
               u."MiddleName",
               u."LastName",
               u."FullName_Ar",
               u."AccountStatus",
               COALESCE(u."FullName_Ar",
                        TRIM(CONCAT_WS(' ', u."FirstName", u."LastName"))) AS label,
               r."OpenAlex_AuthorID",
               r."ORCID_ID",
               r."LastSyncedAt",
               r."ResearchInterestsUpdatedAt",
               jsonb_array_length(
                   CASE WHEN jsonb_typeof(r."ResearchInterests") = 'array'
                        THEN r."ResearchInterests" ELSE '[]'::jsonb END
               ) AS n_interests,
               (SELECT COUNT(*) FROM "Authors" a WHERE a."UserID" = u."UserID") AS n_authors,
               (SELECT COUNT(DISTINCT a."PaperID") FROM "Authors" a WHERE a."UserID" = u."UserID") AS n_papers,
               (SELECT string_agg(d."DepartmentName", ', ')
                  FROM "Department" d
                 WHERE d."HeadID" = u."UserID") AS heads_of_depts
        FROM "Users" u
        LEFT JOIN "Researcher" r ON r."UserID" = u."UserID"
        WHERE ({') OR ('.join(where)})
        ORDER BY u."Litrix_ID"
    '''
    with connection.cursor() as cur:
        cur.execute(sql, params)
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def snapshot_to_audit(target, unassigned_hod, unregistered, enabled=True):
    """
    Write a snapshot into AuditLog. Returns True on success, False on
    skip/failure. Uses its OWN savepoint so a snapshot failure can't
    abort the outer reset transaction.
    """
    if not enabled:
        return False
    sql, make_params = _discover_audit_schema()
    if sql is None:
        return False

    payload = {
        'user_id':            target['UserID'],
        'litrix_id':          target['Litrix_ID'],
        'scholar_id':         target['Scholar_ID'],
        'user_type':          target['UserType'],
        'account_status':     target['AccountStatus'],
        'email':              target['Email'],
        'first_name':         target['FirstName'],
        'middle_name':        target['MiddleName'],
        'last_name':          target['LastName'],
        'full_name_ar':       target['FullName_Ar'],
        'heads_of_depts':     target['heads_of_depts'],
        'openalex_author_id': target['OpenAlex_AuthorID'],
        'orcid_id':           target['ORCID_ID'],
        'last_synced_at':     target['LastSyncedAt'].isoformat()
                                if target['LastSyncedAt'] else None,
        'interests_updated':  target['ResearchInterestsUpdatedAt'].isoformat()
                                if target['ResearchInterestsUpdatedAt'] else None,
        'n_interests':        target['n_interests'],
        'n_author_links':     target['n_authors'],
        'n_distinct_papers':  target['n_papers'],
        'unassigned_hod':     unassigned_hod,
        'unregistered':       unregistered,
    }
    payload_json = json.dumps(payload, ensure_ascii=False)

    try:
        # Nested atomic = SAVEPOINT — if the INSERT fails we only roll
        # back THIS, not the whole reset.
        with transaction.atomic():
            with connection.cursor() as cur:
                cur.execute(sql, make_params(
                    target['UserID'], 'RESET_RESEARCHER', payload_json))
        return True
    except Exception as e:
        print(f"    [warn] AuditLog snapshot skipped: {e}")
        return False


def reset_one(target, apply, unassign_hod, unregister, no_audit=False):
    uid = target['UserID']
    label = target['label']
    print()
    print(f"  - {label}  (UserID={uid}, Litrix={target['Litrix_ID']})")
    print(f"    will delete  : {target['n_authors']} Authors links -> "
          f"{target['n_papers']} distinct papers")
    print(f"    will clear   : ResearchInterests ({target['n_interests']} labels), "
          f"OpenAlex_AuthorID, ORCID_ID, LastSyncedAt, "
          f"CitationsByYear, ResearchInterestsUpdatedAt")
    if (unassign_hod or unregister) and target['heads_of_depts']:
        print(f"    will unassign: HoD of [{target['heads_of_depts']}] "
              f"-> Department.HeadID = NULL")
        if target['UserType'] == 'HoD':
            print(f"    will demote  : UserType 'HoD' -> 'Researcher'")
    if unregister:
        new_email = f"unregistered_{uid}@litrix.local"
        print(f"    will unregister:")
        print(f"      Email         '{target['Email']}' -> '{new_email}'")
        print(f"      FirstName     '{target['FirstName']}' -> ''")
        print(f"      MiddleName    '{target['MiddleName'] or ''}' -> NULL")
        print(f"      LastName      '{target['LastName']}' -> ''")
        print(f"      FullName_Ar   '{target['FullName_Ar'] or ''}' -> NULL")
        print(f"      AccountStatus '{target['AccountStatus']}' -> 'Pending'")
        print(f"      UserType      -> 'Pending'  (overrides --unassign-hod demote)")
        print(f"    + DELETE: RegistrationRequest, EmailVerification, "
              f"RefreshToken, Notification, Invitation (by old email)")

    if not apply:
        return

    # Snapshot FIRST in its own savepoint (won't break anything if it fails)
    did_unassign = bool((unassign_hod or unregister) and target['heads_of_depts'])
    snapshot_to_audit(target,
                      unassigned_hod=did_unassign,
                      unregistered=bool(unregister),
                      enabled=not no_audit)

    # Real reset in its own transaction
    with transaction.atomic():
        with connection.cursor() as cur:
            # 1) Authors links
            cur.execute('DELETE FROM "Authors" WHERE "UserID" = %s', [uid])

            # 2) Researcher scraped fields
            cur.execute(
                '''
                UPDATE "Researcher"
                   SET "ResearchInterests"          = NULL,
                       "ResearchInterestsUpdatedAt" = NULL,
                       "OpenAlex_AuthorID"          = NULL,
                       "ORCID_ID"                   = NULL,
                       "LastSyncedAt"               = NULL,
                       "CitationsByYear"            = NULL
                 WHERE "UserID" = %s
                ''',
                [uid],
            )

            # 3) HoD demotion
            if (unassign_hod or unregister) and target['heads_of_depts']:
                cur.execute(
                    'UPDATE "Department" SET "HeadID" = NULL WHERE "HeadID" = %s',
                    [uid],
                )
                if target['UserType'] == 'HoD' and not unregister:
                    cur.execute(
                        'UPDATE "Users" SET "UserType" = %s WHERE "UserID" = %s',
                        ['Researcher', uid],
                    )

            # 4) Full unregister
            if unregister:
                old_email = target['Email']
                new_email = f"unregistered_{uid}@litrix.local"
                cur.execute(
                    '''
                    UPDATE "Users"
                       SET "Email"         = %s,
                           "FirstName"     = '',
                           "MiddleName"    = NULL,
                           "LastName"      = '',
                           "FullName_Ar"   = NULL,
                           "UserType"      = 'Pending',
                           "AccountStatus" = 'Pending'
                     WHERE "UserID" = %s
                    ''',
                    [new_email, uid],
                )
                _try_savepoint(
                    'UPDATE "Users" SET "PasswordHash" = NULL WHERE "UserID" = %s', [uid])
                _try_savepoint(
                    'DELETE FROM "RegistrationRequest" WHERE "UserID" = %s', [uid])
                _try_savepoint(
                    'DELETE FROM "EmailVerification" WHERE "UserID" = %s', [uid])
                _try_savepoint(
                    'DELETE FROM "RefreshToken" WHERE "UserID" = %s', [uid])
                _try_savepoint(
                    'DELETE FROM "Notification" WHERE "UserID" = %s', [uid])
                if old_email:
                    _try_savepoint(
                        'DELETE FROM "Invitation" WHERE LOWER("TargetEmail") = LOWER(%s)',
                        [old_email])

    print(f"    [ok] reset complete.")


def _try_savepoint(sql, params):
    """Execute inside a SAVEPOINT so missing-table/column errors don't
    abort the outer transaction."""
    try:
        with transaction.atomic():
            with connection.cursor() as cur:
                cur.execute(sql, params)
    except Exception:
        pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--scholar-ids', type=str, default=None)
    ap.add_argument('--litrix-ids', type=str, default=None)
    ap.add_argument('--apply', action='store_true')
    ap.add_argument('--yes', action='store_true')
    ap.add_argument('--unassign-hod', action='store_true')
    ap.add_argument('--unregister', action='store_true',
                    help='Full unregister; implies --unassign-hod.')
    ap.add_argument('--no-audit', action='store_true',
                    help='Skip writing AuditLog snapshots.')
    args = ap.parse_args()

    scholar_ids = (args.scholar_ids.split(',') if args.scholar_ids
                   else (None if args.litrix_ids else DEFAULT_SCHOLAR_IDS))
    litrix_ids  = args.litrix_ids.split(',') if args.litrix_ids else None

    targets = find_targets(scholar_ids=scholar_ids, litrix_ids=litrix_ids)
    if not targets:
        print("No matching researchers found.")
        return

    print("=" * 70)
    print(f"Targets ({len(targets)}):")
    print("=" * 70)
    has_hod = False
    for t in targets:
        print(f"  - {t['label']}  (Litrix={t['Litrix_ID']}, "
              f"Scholar={t['Scholar_ID']})")
        print(f"      Role          : {t['UserType']}")
        print(f"      Email         : {t['Email']}")
        print(f"      AccountStatus : {t['AccountStatus']}")
        if t['heads_of_depts']:
            has_hod = True
            print(f"      [HoD]         : {t['heads_of_depts']}")
        print(f"      Authors links : {t['n_authors']} "
              f"({t['n_papers']} distinct papers)")
        print(f"      Interests     : {t['n_interests']} labels")
        print(f"      OpenAlex ID   : {t['OpenAlex_AuthorID'] or '-'}")
        print(f"      ORCID         : {t['ORCID_ID'] or '-'}")
        print(f"      Last synced   : {t['LastSyncedAt'] or '-'}")

    if has_hod and (args.unregister or args.unassign_hod):
        print()
        print("HoD UNASSIGN: Department.HeadID will be cleared.")

    if args.unregister:
        print()
        print("UNREGISTER MODE: Email, Name, Password, Status cleared;")
        print("                 RegistrationRequest + Tokens + Notifications + ")
        print("                 Invitation deleted. Users row + IDs preserved.")

    if not args.apply:
        print()
        print("DRY RUN -- nothing will be written. Re-run with --apply to commit.")
        return

    if not args.yes:
        print()
        prompt = ("Type 'yes' to confirm UNREGISTER + reset: "
                  if args.unregister
                  else "Type 'yes' to confirm reset: ")
        if input(prompt).strip().lower() != 'yes':
            print("Aborted.")
            return

    print()
    print("=" * 70)
    print("Applying reset...")
    print("=" * 70)
    for t in targets:
        try:
            reset_one(t, apply=True,
                      unassign_hod=args.unassign_hod or args.unregister,
                      unregister=args.unregister,
                      no_audit=args.no_audit)
        except Exception as e:
            print(f"    [FAIL] {e}")
    print()
    print("Done.")


if __name__ == "__main__":
    main()
