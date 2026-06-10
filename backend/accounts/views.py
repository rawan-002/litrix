import json
import logging
import threading

import psycopg2
from django.contrib.auth.hashers import make_password, check_password
from django.db import connection, transaction
from rest_framework import status, permissions
from rest_framework.decorators import (
    api_view, permission_classes, throttle_classes,
)
from rest_framework.response import Response
from rest_framework.throttling import AnonRateThrottle
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework_simplejwt.views import TokenRefreshView

logger = logging.getLogger(__name__)


# ============================================================================
# Auth-endpoint throttles
# ============================================================================
# Why custom subclasses?
#   The DRF Scoped/Anon defaults read their rate from the global
#   DEFAULT_THROTTLE_RATES map in settings. We define dedicated classes
#   here (rather than @scope decorators) so the throttle is self-
#   contained — the view file declares its own rate-limit policy and
#   it cannot accidentally fall through to the global anon limit.
#
# The rates come from settings.DEFAULT_THROTTLE_RATES['auth_anon'] etc.
# Adjust there to tune; the view code never needs to change.
# ============================================================================
class AuthAnonThrottle(AnonRateThrottle):
    """5/min by default. Applied to login, register, password-reset, etc."""
    scope = 'auth_anon'

from .models import User
from .serializers import (
    RegisterSerializer, VerifyEmailSerializer, LoginSerializer,
    UserSerializer, PasswordResetSerializer, PasswordResetConfirmSerializer,
)
from .email_service import (
    create_verification, verify_token,
    send_verification_email, send_password_reset,
)


# ============================================================================
# Post-approval scraper kickoff
# ============================================================================
# After an admin approves a registration, the new researcher should have
# their publications fetched without requiring a second manual click in
# the Sync admin page. This helper queues a SyncJob and spins up the
# background thread that runs the scraper.
#
# Source priority:
#   1. Scholar  — richer per-paper signal (cited_by, journal, etc).
#   2. ORCID    — fallback if no Scholar_ID.
#   3. (none)   — silently skip; admin can sync manually later.
#
# The cooldown gate in _run_scraper / scholar.py naturally lets a first-
# time sync through because LastSyncedAt is NULL for a freshly created
# Researcher row.
# ============================================================================
def _kickoff_initial_sync(
    user_id: int,
    tenant_id: int,
    scholar_id: str | None,
    orcid_id: str | None,
    triggered_by_user_id: int,
):
    if scholar_id:
        source = 'scholar'
        args = [scholar_id, str(user_id)]
    elif orcid_id:
        source = 'orcid'
        args = ['--orcid', orcid_id, '--user', str(user_id)]
    else:
        return None  # Nothing to sync — user provided no academic IDs.

    with connection.cursor() as cur:
        # %s::jsonb is required because psycopg2 binds the dumped string
        # as plain TEXT, and Postgres won't implicitly cast TEXT → JSONB
        # inside an INSERT (you'd get 'column "Metadata" is of type jsonb
        # but expression is of type text'). The cast is the canonical fix.
        cur.execute(
            '''
            INSERT INTO "SyncJob" (
                "TenantID", "UserID", "TriggeredBy", "Source", "Status", "Metadata"
            )
            VALUES (%s, %s, %s, %s, 'queued', %s::jsonb)
            RETURNING "JobID"
            ''',
            [
                tenant_id, user_id, triggered_by_user_id, source,
                json.dumps({'origin': 'registration_approval', 'force': False}),
            ],
        )
        job_id = cur.fetchone()[0]

    # Lazy import — avoids circular dependency at module load time and
    # keeps sync_views as the single source of truth for _run_scraper.
    from .sync_views import _run_scraper
    threading.Thread(
        target=_run_scraper, args=(job_id, source, args), daemon=True,
    ).start()
    return job_id


def get_tokens(user):
    refresh = RefreshToken.for_user(user)
    refresh['email']     = user.email
    refresh['tenant_id'] = user.tenant_id
    refresh['role_id']   = user.role_id
    refresh['user_type'] = user.user_type
    return {
        'access':  str(refresh.access_token),
        'refresh': str(refresh),
    }


# ============================================================================
# Identity Verification Gate (registration-time)
# ============================================================================
# Why?
#   We may already have a Researcher record for this person from prior
#   scraping (papers + dept + rank are populated). When that person comes
#   to claim their account, the form values they enter must be reconciled
#   against the stored record. Mismatches surface as warnings so:
#     • Honest users catch a typo before submitting.
#     • Identity-claim attempts surface visibly to admins at approval time.
#   We do NOT block the submit — per the product spec, mismatches show as
#   a notification ("اشعار") and the admin makes the final call.
#
# Lookup priority: Scholar_ID > ORCID > Email. Whichever hits first wins,
# because Scholar_ID is the most specific academic identifier.
# ============================================================================
def _normalize(s):
    return (s or '').strip()


def _lookup_existing_for_registration(
    scholar_id: str | None,
    orcid_id:   str | None,
    email:      str | None,
    department_id: int | None    = None,
    academic_rank: str | None    = None,
    full_name_ar:  str | None    = None,
) -> dict:
    """
    Try to find an existing Users row that this registration is claiming
    to be, and report any mismatches against the form-supplied values.

    Returns:
        match_found     : bool
        matched_by      : 'scholar_id' | 'orcid_id' | 'email' | None
        stored          : { user_id, full_name_ar, department_id,
                            department_name, academic_rank, papers_count }
        mismatches      : [ { field, your_value, our_value, label } ]
    """
    sid   = _normalize(scholar_id)
    oid   = _normalize(orcid_id)
    em    = _normalize(email).lower()

    matched_by = None
    matched_user_id = None

    with connection.cursor() as cur:
        if sid:
            cur.execute(
                'SELECT "UserID" FROM "Users" WHERE "Scholar_ID" = %s LIMIT 1',
                [sid],
            )
            r = cur.fetchone()
            if r:
                matched_user_id, matched_by = r[0], 'scholar_id'

        if matched_user_id is None and oid:
            cur.execute(
                'SELECT "UserID" FROM "Users" WHERE "Orcid_ID" = %s LIMIT 1',
                [oid],
            )
            r = cur.fetchone()
            if r:
                matched_user_id, matched_by = r[0], 'orcid_id'

        if matched_user_id is None and em:
            cur.execute(
                'SELECT "UserID" FROM "Users" WHERE LOWER("Email") = %s LIMIT 1',
                [em],
            )
            r = cur.fetchone()
            if r:
                matched_user_id, matched_by = r[0], 'email'

        if matched_user_id is None:
            return {
                'match_found': False,
                'matched_by':  None,
                'stored':      None,
                'mismatches':  [],
            }

        # Pull the stored profile snapshot for comparison.
        # AccountStatus + IsActive let us distinguish a fully-claimed
        # account (someone else is already using it) from a
        # placeholder Users row created by an admin import / pre-scrape.
        #
        # Department lookup uses a LATERAL subquery that prefers the
        # current position but falls back to ANY recorded Works_In row
        # if IsCurrentPosition was never set (common when researchers
        # are imported by admin and the flag was missed). Without this
        # fallback, a stored department could silently show up as NULL
        # and miss the mismatch comparison entirely.
        cur.execute('''
            SELECT
                u."UserID",
                u."FullName_Ar",
                u."Email",
                u."AccountStatus",
                u."IsActive",
                u."EmailVerified",
                w."DepartmentID",
                d."DepartmentName",
                r."AcademicRank",
                (SELECT COUNT(*) FROM "Authors" a WHERE a."UserID" = u."UserID") AS papers
            FROM "Users" u
            LEFT JOIN "Researcher" r ON r."UserID" = u."UserID"
            LEFT JOIN LATERAL (
                SELECT wi."DepartmentID"
                FROM "Works_In" wi
                WHERE wi."UserID" = u."UserID"
                ORDER BY
                    wi."IsCurrentPosition" DESC NULLS LAST,
                    wi."StartDate"         DESC NULLS LAST
                LIMIT 1
            ) w ON TRUE
            LEFT JOIN "Department" d ON d."DepartmentID" = w."DepartmentID"
            WHERE u."UserID" = %s
        ''', [matched_user_id])
        row = cur.fetchone()

    is_active_user = bool(
        row[4]                          # IsActive
        and row[5]                      # EmailVerified
        and (row[3] or '').lower() == 'active'
    )

    stored = {
        'user_id':           row[0],
        'full_name_ar':      row[1],
        'email':             row[2],
        'account_status':    row[3],
        'is_active_user':    is_active_user,
        'department_id':     row[6],
        'department_name':   row[7],
        'academic_rank':     row[8],
        'papers_count':      row[9] or 0,
    }

    mismatches = []

    # ------------------------------------------------------------------
    # Critical signal: this Scholar/ORCID is already a fully-activated
    # account in the system. Almost always indicates a typo in the form
    # OR an identity-claim attempt. Surfaced as a HIGH-severity item
    # the UI can render in red instead of amber.
    # ------------------------------------------------------------------
    if is_active_user and matched_by in ('scholar_id', 'orcid_id'):
        mismatches.append({
            'field':      'identity_already_claimed',
            'label':      'Identity already in use',
            'severity':   'high',
            'your_value': matched_by,
            'our_value':  stored['email'],
        })

    # ------------------------------------------------------------------
    # Department mismatch.
    #
    # Three sub-cases:
    #   (a) Both sides have a value, and they differ → hard mismatch.
    #   (b) Stored side has a value, form side is empty → not a mismatch
    #       per se (user just hasn't picked yet); skip silently.
    #   (c) Stored side is empty BUT we have a Scholar_ID match with
    #       existing scraped papers → notify so the user knows we
    #       already track them; the admin will confirm department on
    #       approval. This is the "data-integrity hint" the SaaS spec
    #       requires for prior-scraped researchers.
    # ------------------------------------------------------------------
    if department_id and stored['department_id'] and \
       int(department_id) != int(stored['department_id']):
        mismatches.append({
            'field':      'department_id',
            'label':      'Department',
            'severity':   'high',
            'your_value': int(department_id),
            'our_value':  stored['department_id'],
            'our_label':  stored['department_name'],
        })
    elif (not stored['department_id']
          and stored['papers_count'] > 0
          and matched_by in ('scholar_id', 'orcid_id')):
        # Case (c): we have papers from prior scraping but no recorded
        # department. The user's chosen department needs admin review.
        mismatches.append({
            'field':      'department_unverified',
            'label':      'Department needs verification',
            'severity':   'info',
            'your_value': department_id,
            'our_value':  None,
            'note':       (
                f'This {matched_by} is already tracked in our system '
                f'with {stored["papers_count"]} stored papers, but no '
                f'department is on file. An administrator will verify '
                f'your chosen department on approval.'
            ),
        })

    # Academic rank mismatch — case-insensitive, trimmed compare.
    if _normalize(academic_rank) and _normalize(stored['academic_rank']):
        if _normalize(academic_rank).lower() != \
           _normalize(stored['academic_rank']).lower():
            mismatches.append({
                'field':      'academic_rank',
                'label':      'Academic Rank',
                'your_value': academic_rank,
                'our_value':  stored['academic_rank'],
            })

    # Name mismatch — exact (after trim). Names get normalized
    # differently across systems; only flag a HARD mismatch.
    if _normalize(full_name_ar) and _normalize(stored['full_name_ar']):
        if _normalize(full_name_ar) != _normalize(stored['full_name_ar']):
            mismatches.append({
                'field':      'full_name_ar',
                'label':      'Arabic Name',
                'your_value': full_name_ar,
                'our_value':  stored['full_name_ar'],
            })

    return {
        'match_found': True,
        'matched_by':  matched_by,
        'stored':      stored,
        'mismatches':  mismatches,
    }


@api_view(['POST'])
@permission_classes([permissions.AllowAny])
@throttle_classes([AuthAnonThrottle])
def registration_match(request):
    """
    Real-time check used by the registration form.
    Public on purpose — but we never leak a password hash, just enough
    profile info to reconcile against form values.

    Throttled because this endpoint is a tempting reconnaissance vector
    (it confirms whether a given Scholar_ID / ORCID is in the system).
    """
    d = request.data or {}
    result = _lookup_existing_for_registration(
        scholar_id    = d.get('scholar_id'),
        orcid_id      = d.get('orcid_id'),
        email         = d.get('email'),
        department_id = d.get('department_id'),
        academic_rank = d.get('academic_rank'),
        full_name_ar  = d.get('full_name_ar'),
    )
    return Response(result)


def audit(user_id, tenant_id, action, target_type=None, target_id=None, metadata=None, request=None):
    with connection.cursor() as cur:
        cur.execute('''
            INSERT INTO "AuditLog"
            ("TenantID", "UserID", "Action", "TargetType", "TargetID",
             "Metadata", "IpAddress", "UserAgent")
            VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s, %s)
        ''', [
            tenant_id, user_id, action, target_type, target_id,
            json.dumps(metadata or {}),
            request.META.get('REMOTE_ADDR') if request else None,
            request.META.get('HTTP_USER_AGENT', '')[:500] if request else None,
        ])


@api_view(['GET'])
@permission_classes([permissions.AllowAny])
def public_departments(request):
    with connection.cursor() as cur:
        cur.execute('''
            SELECT "DepartmentID", "DepartmentName"
            FROM "Department" WHERE "TenantID" = 1
            ORDER BY "DepartmentName"
        ''')
        rows = [{'department_id': r[0], 'department_name': r[1]} for r in cur.fetchall()]
    return Response({'departments': rows})


@api_view(['GET'])
@permission_classes([permissions.AllowAny])
def public_stats(request):
    """
    Headline counts for the public landing page. No auth required, no
    PII surfaced - only aggregates. Cached intent: cheap to compute,
    safe to expose, refreshed on every page load.
    """
    with connection.cursor() as cur:
        cur.execute("""
            SELECT
                (SELECT COUNT(*) FROM "Users"
                 WHERE "UserType" = 'Researcher')                AS researchers,
                (SELECT COUNT(*) FROM "ResearchPaper")           AS papers,
                (SELECT COUNT(DISTINCT "JournalID")
                 FROM "JournalRankings"
                 WHERE "Quartile" = 'Q1')                        AS q1_journals,
                (SELECT COUNT(*) FROM "Department"
                 WHERE "TenantID" = 1)                           AS departments,
                (SELECT COUNT(*) FROM "ResearchPaper"
                 WHERE "PubYear" = EXTRACT(YEAR FROM NOW())::int) AS papers_this_year
        """)
        r = cur.fetchone()
    return Response({
        'researchers':      r[0],
        'papers':           r[1],
        'q1_journals':      r[2],
        'departments':      r[3],
        'papers_this_year': r[4],
    })


@api_view(['POST'])
@permission_classes([permissions.AllowAny])
@throttle_classes([AuthAnonThrottle])
def register(request):
    s = RegisterSerializer(data=request.data)
    s.is_valid(raise_exception=True)
    d = s.validated_data
    email = d['email'].lower()

    # ------------------------------------------------------------------
    # Optional invitation flow.
    #
    # If the request carries an `invite` token, we run the role-scoped
    # registration path:
    #   • token must exist, not be used, not be expired, not revoked
    #   • email must match the address the admin invited
    # On success, the resulting User is provisioned with the intended
    # role + UserType + (optional) department, and the regular admin-
    # approval queue is bypassed — the invite IS the approval.
    # ------------------------------------------------------------------
    invite_token = (request.data.get('invite') or '').strip()
    invite_payload = None
    if invite_token:
        from .invitation_views import consume_invitation
        invite_payload, reason = consume_invitation(invite_token, email)
        if invite_payload is None:
            return Response(
                {'error': 'invalid_invitation', 'reason': reason},
                status=status.HTTP_400_BAD_REQUEST,
            )

    # ------------------------------------------------------------------
    # Server-side identity verification gate.
    #
    # The frontend already runs the same lookup as the user types, but
    # that's a UX nicety, not a security boundary. A user can:
    #   • submit before the debounce fires
    #   • disable JS / hit the API directly
    #   • ignore the warning banner
    # We re-run the check here and HARD-BLOCK any high-severity issue:
    #   • The Scholar/ORCID is already linked to an active account.
    #   • The chosen department clearly disagrees with the stored one.
    # Lower-severity hints (rank/name/info) only warn; they don't block,
    # because legitimate updates (transfers, promotions) need a path
    # through. Admins still see them at approval time.
    #
    # Invitations skip this gate — the admin already vetted the
    # invitee when issuing the token.
    # ------------------------------------------------------------------
    if not invite_payload:
        verification = _lookup_existing_for_registration(
            scholar_id    = d.get('scholar_id'),
            orcid_id      = d.get('orcid_id'),
            email         = email,
            department_id = d.get('department_id'),
            academic_rank = d.get('academic_rank'),
            full_name_ar  = d.get('full_name_ar'),
        )
        blocking = [
            m for m in verification.get('mismatches', [])
            if m.get('severity') == 'high'
        ]
        if blocking:
            return Response(
                {
                    'error':         'identity_verification_failed',
                    'message':       (
                        'Your registration conflicts with our records. '
                        'Please review and correct, or contact your administrator.'
                    ),
                    'verification':  verification,
                    'mismatches':    blocking,
                },
                status=status.HTTP_409_CONFLICT,
            )

    pwd_hash = make_password(d['password'])
    metadata = {
        'full_name_ar': d.get('full_name_ar') or '',
        'full_name_en': d.get('full_name_en') or '',
        # Explicit three-part English name from the form (preferred over
        # splitting full_name_en, which can't tell middle from last).
        'first_name':   d.get('first_name') or '',
        'middle_name':  d.get('middle_name') or '',
        'last_name':    d.get('last_name') or '',
        'department_id': d.get('department_id'),
        'academic_rank': d.get('academic_rank') or '',
        'scholar_id':   d.get('scholar_id') or '',
        'orcid_id':     d.get('orcid_id') or '',
        'scopus_id':    d.get('scopus_id') or '',
        'password_hash': pwd_hash,
    }

    # ------------------------------------------------------------------
    # Invitation fast path: provision the User directly, skip the
    # awaiting-email-verification queue, and skip the admin-approval
    # step. The invite IS the approval (admin issued it explicitly).
    #
    # SECURITY GATE: even with a valid invitation, the researcher
    # cannot claim a profile under a different department than the
    # one already recorded. We re-run the mismatch check here and
    # reject HIGH-severity department mismatches before any account
    # is created. (The invite vetting only covers email + role, not
    # the identity claim against existing scraped data.)
    # ------------------------------------------------------------------
    if invite_payload:
        inv_verification = _lookup_existing_for_registration(
            scholar_id    = d.get('scholar_id'),
            orcid_id      = d.get('orcid_id'),
            email         = email,
            department_id = d.get('department_id'),
            academic_rank = d.get('academic_rank'),
            full_name_ar  = d.get('full_name_ar'),
        )
        dept_blocking = [
            m for m in inv_verification.get('mismatches', [])
            if m.get('severity') == 'high'
               and m.get('field') == 'department_id'
        ]
        if dept_blocking:
            return Response(
                {
                    'error':       'department_mismatch',
                    'message':     (
                        'The department you selected does not match '
                        'the one on record for this researcher. Please '
                        'pick the correct department or contact the '
                        'administrator.'
                    ),
                    'mismatches':  dept_blocking,
                },
                status=status.HTTP_409_CONFLICT,
            )
        return _provision_invited_user(
            request, d, metadata, pwd_hash, email, invite_payload,
        )

    with connection.cursor() as cur:
        # Coerce empty strings to NULL on the way into the DB so the
        # academic-ID UNIQUEs and FullName_Ar UNIQUE don't trip on
        # multiple "registered without scholar/orcid" rows. Approve
        # logic does the same — done at *both* ends of the pipeline.
        sid_val    = (metadata['scholar_id']  or '').strip() or None
        oid_val    = (metadata['orcid_id']    or '').strip() or None
        scopus_val = (metadata['scopus_id']   or '').strip() or None
        name_ar_v  = (metadata['full_name_ar'] or '').strip() or None
        name_en_v  = (metadata['full_name_en'] or '').strip() or None
        rank_val   = (metadata['academic_rank'] or '').strip() or None
        cur.execute('''
            INSERT INTO "RegistrationRequest"
            ("TenantID", "Email", "FullName_Ar", "FullName_En",
             "Scholar_ID", "Orcid_ID", "Scopus_ID",
             "DepartmentID", "AcademicRank", "PasswordHash", "Status")
            VALUES (1, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'awaiting_email_verification')
            RETURNING "RequestID"
        ''', [
            email, name_ar_v, name_en_v,
            sid_val, oid_val, scopus_val,
            metadata['department_id'], rank_val, pwd_hash,
        ])
        req_id = cur.fetchone()[0]

    token = create_verification(email, purpose='registration')
    # Surface the delivery result so the client can warn + offer a resend
    # instead of silently parking the user on "we sent a code" when the
    # email never actually went out.
    sent = send_verification_email(email, token)

    return Response({
        'message': 'Verification code sent to your email' if sent
                   else 'Account created, but we could not send the '
                        'verification email. Use “Resend code”.',
        'request_id': req_id,
        'email_sent': bool(sent),
    }, status=status.HTTP_201_CREATED)


def _provision_invited_user(request, d, metadata, pwd_hash, email, invite):
    """
    Direct provisioning path for invitation-based registration.

    Creates the Users + Researcher + Works_In rows in one atomic block
    using the role + department baked into the invitation, marks the
    invitation used, and kicks off the initial scrape if academic IDs
    are present. The caller still receives a 201 with the brand-new
    user_id.
    """
    sid    = (metadata['scholar_id']  or '').strip() or None
    oid    = (metadata['orcid_id']    or '').strip() or None
    scopus = (metadata['scopus_id']   or '').strip() or None
    name_ar = (metadata['full_name_ar'] or '').strip() or None
    name_en = (metadata['full_name_en'] or '').strip() or None
    rank    = (metadata['academic_rank'] or '').strip() or None

    # Prefer the explicit three-part English name the form now sends.
    first_name  = (metadata.get('first_name')  or '').strip() or None
    middle_name = (metadata.get('middle_name') or '').strip() or None
    last_name   = (metadata.get('last_name')   or '').strip() or None

    # "Users"."FirstName"/"LastName" are NOT NULL. If the explicit parts are
    # missing (older clients, Arabic-only signups, a single token), derive
    # non-empty values so we never send NULLs and 500 the registration:
    # English full name -> Arabic name -> email local part.
    if not (first_name and last_name):
        display = name_en or name_ar or email.split('@')[0]
        parts   = display.split()
        first_name = first_name or (parts[0] if parts else display)
        if not last_name:
            last_name = ' '.join(parts[1:]) if len(parts) > 1 else first_name

    # The invitation already pinned tenant + role + user_type + (optional)
    # department; the invitee can't override them via the form.
    tenant_id   = invite['tenant_id']
    role_id     = invite['role_id']
    user_type   = invite['user_type']
    invite_dept = invite['department_id']
    final_dept  = invite_dept or metadata.get('department_id')

    with transaction.atomic():
        with connection.cursor() as cur:
            # ----------------------------------------------------------
            # ATOMIC INVITATION CLAIM (race guard)
            # consume_invitation() validated the token earlier but did NOT
            # mark it used, so two concurrent registrations with the same
            # token could both reach here and provision two accounts. Claim
            # it FIRST with a conditional UPDATE: the `WHERE "UsedAt" IS NULL`
            # + row lock makes this the single serialization point — the
            # second request matches 0 rows and is rejected before anything
            # is created. UsedByUserID is filled in once we have the UserID;
            # if any later step fails, the whole atomic block rolls back and
            # the token reverts to unused.
            # ----------------------------------------------------------
            cur.execute(
                'UPDATE "Invitation" SET "UsedAt" = NOW() '
                'WHERE "InvitationID" = %s '
                '  AND "UsedAt" IS NULL AND "RevokedAt" IS NULL '
                'RETURNING "InvitationID"',
                [invite['invitation_id']],
            )
            if cur.fetchone() is None:
                return Response(
                    {'error': 'invalid_invitation', 'reason': 'already_used'},
                    status=status.HTTP_409_CONFLICT,
                )

            # ----------------------------------------------------------
            # CLAIM-PROFILE FLOW
            # Before INSERT-ing a brand-new Users row, check if there's
            # already a 'Pending' row with the same Scholar_ID or ORCID.
            # That row was created earlier by an admin import / cleanup
            # and is waiting for the real owner to register. We UPDATE
            # it in-place so the existing Authors links + Researcher
            # row keep their UserID - the new user inherits all their
            # papers automatically.
            # ----------------------------------------------------------
            claimed_user_id = None
            if sid:
                cur.execute(
                    'SELECT "UserID" FROM "Users" '
                    'WHERE "Scholar_ID" = %s AND "AccountStatus" = %s '
                    'LIMIT 1',
                    [sid, 'Pending'],
                )
                row = cur.fetchone()
                if row:
                    claimed_user_id = row[0]
            if claimed_user_id is None and oid:
                cur.execute(
                    'SELECT "UserID" FROM "Users" '
                    'WHERE "Orcid_ID" = %s AND "AccountStatus" = %s '
                    'LIMIT 1',
                    [oid, 'Pending'],
                )
                row = cur.fetchone()
                if row:
                    claimed_user_id = row[0]

            if claimed_user_id is not None:
                # Claim path: update the existing Pending row.
                cur.execute('''
                    UPDATE "Users"
                    SET "Email"         = %s,
                        "PasswordHash"  = %s,
                        "FullName_Ar"   = COALESCE(%s, "FullName_Ar"),
                        "FirstName"     = COALESCE(%s, "FirstName"),
                        "MiddleName"    = COALESCE(%s, "MiddleName"),
                        "LastName"      = COALESCE(%s, "LastName"),
                        "UserType"      = %s,
                        "AccountStatus" = 'Active',
                        "TenantID"      = %s,
                        "RoleID"        = %s,
                        "EmailVerified" = TRUE,
                        "IsActive"      = TRUE,
                        "Scopus_ID"     = COALESCE(%s, "Scopus_ID")
                    WHERE "UserID" = %s
                    RETURNING "UserID"
                ''', [
                    email, pwd_hash, name_ar, first_name, middle_name, last_name,
                    user_type, tenant_id, role_id, scopus,
                    claimed_user_id,
                ])
                new_user_id = cur.fetchone()[0]
            else:
                # Fresh INSERT path: no matching profile to claim.
                cur.execute('''
                    INSERT INTO "Users"
                      ("Email", "PasswordHash", "FullName_Ar", "FirstName", "MiddleName", "LastName",
                       "UserType", "AccountStatus", "TenantID", "RoleID",
                       "EmailVerified", "IsActive", "Scholar_ID", "Orcid_ID", "Scopus_ID",
                       "CreatedAt")
                    VALUES (%s, %s, %s, %s, %s, %s, %s, 'Active', %s, %s,
                            TRUE, TRUE, %s, %s, %s, NOW())
                    RETURNING "UserID"
                ''', [
                    email, pwd_hash, name_ar, first_name, middle_name, last_name,
                    user_type, tenant_id, role_id,
                    sid, oid, scopus,
                ])
                new_user_id = cur.fetchone()[0]

            # Researcher row exists for everyone — even Dean/HoD may
            # eventually have publications; the row is the join anchor.
            cur.execute('''
                INSERT INTO "Researcher" ("UserID", "AcademicRank", "LastSyncedAt")
                VALUES (%s, %s, NULL)
                ON CONFLICT ("UserID") DO UPDATE SET "AcademicRank" = COALESCE(
                    EXCLUDED."AcademicRank", "Researcher"."AcademicRank")
            ''', [new_user_id, rank])

            if final_dept:
                cur.execute('''
                    INSERT INTO "Works_In"
                      ("UserID", "DepartmentID", "StartDate", "IsCurrentPosition")
                    VALUES (%s, %s, CURRENT_DATE, TRUE)
                    ON CONFLICT DO NOTHING
                ''', [new_user_id, final_dept])

                # A HoD heads exactly one department — record the canonical
                # Department.HeadID link so role-scoped views (and exports)
                # can resolve "this user's department" without leaning on
                # Works_In. The invite explicitly designated them as head.
                if user_type == 'HoD':
                    cur.execute(
                        'UPDATE "Department" SET "HeadID" = %s '
                        'WHERE "DepartmentID" = %s',
                        [new_user_id, final_dept],
                    )

            # The invitation was already claimed (UsedAt set) at the top of
            # this transaction; now that we have the UserID, record who used
            # it. Same atomic block, so it commits/rolls back together.
            cur.execute(
                'UPDATE "Invitation" SET "UsedByUserID" = %s '
                'WHERE "InvitationID" = %s',
                [new_user_id, invite['invitation_id']],
            )

            # Welcome notification (English — matches the rest of the UI).
            cur.execute('''
                INSERT INTO "Notification"
                  ("TenantID", "UserID", "Type", "Title", "Message")
                VALUES (%s, %s, 'invitation_accepted', %s, %s)
            ''', [
                tenant_id, new_user_id, 'Welcome to Litrix',
                f'Your account has been created as {user_type}. You can now log in.',
            ])

    # Kick off initial publication sync if we have an academic ID. Same
    # helper used by the manual-approval path — keeps behavior unified.
    sync_job_id = None
    try:
        sync_job_id = _kickoff_initial_sync(
            user_id=new_user_id,
            tenant_id=tenant_id,
            scholar_id=sid,
            orcid_id=oid,
            triggered_by_user_id=invite.get('invited_by_user_id') or new_user_id,
        )
    except Exception:
        import traceback
        traceback.print_exc()

    audit(
        new_user_id, tenant_id,
        'registration.invited', 'Invitation', invite['invitation_id'],
        {'user_type': user_type, 'role_id': role_id, 'department_id': final_dept},
        request=request,
    )

    return Response({
        'message':     'Account created via invitation. You can now log in.',
        'user_id':     new_user_id,
        'user_type':   user_type,
        'sync_job_id': sync_job_id,
    }, status=status.HTTP_201_CREATED)


@api_view(['POST'])
@permission_classes([permissions.AllowAny])
@throttle_classes([AuthAnonThrottle])
def verify_email(request):
    s = VerifyEmailSerializer(data=request.data)
    s.is_valid(raise_exception=True)
    email = s.validated_data['email'].lower()
    token = s.validated_data['token']

    ok, msg = verify_token(email, token, purpose='registration')
    if not ok:
        return Response({'error': msg}, status=status.HTTP_400_BAD_REQUEST)

    with connection.cursor() as cur:
        cur.execute('''
            UPDATE "RegistrationRequest"
            SET "Status" = 'pending'
            WHERE LOWER("Email") = LOWER(%s)
              AND "Status" = 'awaiting_email_verification'
            RETURNING "RequestID"
        ''', [email])
        row = cur.fetchone()
        if not row:
            return Response({'error': 'No pending registration found'},
                            status=status.HTTP_404_NOT_FOUND)
        req_id = row[0]

    return Response({
        'message': 'Email verified. Awaiting admin approval.',
        'request_id': req_id,
        'status': 'pending',
    })


@api_view(['POST'])
@permission_classes([permissions.AllowAny])
@throttle_classes([AuthAnonThrottle])
def resend_verification(request):
    """
    POST /api/auth/resend-verification/  { "email": "..." }

    Re-mint and re-send the registration verification code for an email
    that is still awaiting verification. Used when the first email never
    arrived (delivery failure, spam, lost). Returns the same opaque shape
    whether or not a matching pending request exists, so the endpoint
    can't be used to enumerate which emails have registered — except we
    do report `email_sent` so a legitimate user knows to retry.
    """
    email = (request.data.get('email') or '').strip().lower()
    if not email:
        return Response({'error': 'email is required'},
                        status=status.HTTP_400_BAD_REQUEST)

    with connection.cursor() as cur:
        cur.execute('''
            SELECT 1 FROM "RegistrationRequest"
            WHERE LOWER("Email") = LOWER(%s)
              AND "Status" = 'awaiting_email_verification'
            LIMIT 1
        ''', [email])
        pending = cur.fetchone() is not None

    sent = False
    if pending:
        token = create_verification(email, purpose='registration')
        sent = send_verification_email(email, token)

    # Opaque message (don't confirm/deny the email exists); still surface
    # whether the send succeeded so a real user can react.
    return Response({
        'message': 'If an unverified registration exists for this email, '
                   'a new code has been sent.',
        'email_sent': bool(sent),
    })


@api_view(['POST'])
@permission_classes([permissions.AllowAny])
@throttle_classes([AuthAnonThrottle])
def login(request):
    """
    Authenticate a user and return a JWT pair.

    Why does every failure return the SAME error?
        Returning distinct messages ("Invalid credentials" vs. "Email
        not verified" vs. "Account is inactive") is a username
        enumeration vector — an attacker scripts the endpoint with
        random emails and learns which ones are registered. We collapse
        all four failure modes into one generic 401. The audit log
        still records the specific reason internally so admins can
        debug, but the wire response is opaque.
    """
    s = LoginSerializer(data=request.data)
    s.is_valid(raise_exception=True)
    email = s.validated_data['email'].lower()
    password = s.validated_data['password']

    GENERIC_ERROR = {'error': 'Invalid credentials'}

    try:
        user = User.objects.get(email__iexact=email)
    except User.DoesNotExist:
        logger.info('login.fail email=%s reason=no_user', email)
        return Response(GENERIC_ERROR, status=status.HTTP_401_UNAUTHORIZED)

    if not user.password or not check_password(password, user.password):
        logger.info('login.fail email=%s reason=bad_password', email)
        return Response(GENERIC_ERROR, status=status.HTTP_401_UNAUTHORIZED)

    if not user.is_active:
        logger.info('login.fail email=%s reason=inactive', email)
        return Response(GENERIC_ERROR, status=status.HTTP_401_UNAUTHORIZED)

    if not user.email_verified:
        logger.info('login.fail email=%s reason=unverified', email)
        return Response(GENERIC_ERROR, status=status.HTTP_401_UNAUTHORIZED)

    with connection.cursor() as cur:
        cur.execute('UPDATE "Users" SET "LastLoginAt" = NOW() WHERE "UserID" = %s', [user.user_id])

    audit(user.user_id, user.tenant_id, 'auth.login', request=request)

    return Response({
        **get_tokens(user),
        'user': UserSerializer(user).data,
    })


@api_view(['POST'])
def logout(request):
    refresh_token = request.data.get('refresh')
    if refresh_token:
        try:
            RefreshToken(refresh_token).blacklist()
        except Exception:
            pass
    audit(request.user.user_id, request.user.tenant_id, 'auth.logout', request=request)
    return Response({'message': 'Logged out'})


@api_view(['GET', 'PATCH'])
def me(request):
    user = request.user
    if request.method == 'PATCH':
        editable = {'full_name_ar', 'scholar_id', 'orcid_id', 'scopus_id'}
        for field in editable:
            if field in request.data:
                setattr(user, field, request.data[field] or None)
        user.save(update_fields=list(editable))
        audit(user.user_id, user.tenant_id, 'profile.update', request=request)
    return Response(UserSerializer(user).data)


@api_view(['POST'])
def change_password(request):
    from django.contrib.auth.hashers import check_password, make_password
    old = request.data.get('old_password')
    new = request.data.get('new_password')
    if not old or not new:
        return Response({'error': 'Both passwords required'}, status=400)
    user = request.user
    if not check_password(old, user.password or ''):
        return Response({'error': 'Wrong current password'}, status=400)
    if len(new) < 8:
        return Response({'error': 'Password must be at least 8 characters'}, status=400)
    user.password = make_password(new)
    user.save(update_fields=['password'])
    audit(user.user_id, user.tenant_id, 'auth.change_password', request=request)
    return Response({'message': 'Password updated'})


@api_view(['POST'])
@permission_classes([permissions.AllowAny])
@throttle_classes([AuthAnonThrottle])
def password_reset_request(request):
    s = PasswordResetSerializer(data=request.data)
    s.is_valid(raise_exception=True)
    email = s.validated_data['email'].lower()

    user_exists = User.objects.filter(email__iexact=email).exists()
    if user_exists:
        token = create_verification(email, purpose='password_reset', ttl_hours=1)
        send_password_reset(email, token)

    return Response({'message': 'If the email exists, a reset code has been sent'})


@api_view(['POST'])
@permission_classes([permissions.AllowAny])
@throttle_classes([AuthAnonThrottle])
def password_reset_confirm(request):
    s = PasswordResetConfirmSerializer(data=request.data)
    s.is_valid(raise_exception=True)
    email = s.validated_data['email'].lower()
    token = s.validated_data['token']
    new_password = s.validated_data['new_password']

    ok, msg = verify_token(email, token, purpose='password_reset')
    if not ok:
        return Response({'error': msg}, status=status.HTTP_400_BAD_REQUEST)

    try:
        user = User.objects.get(email__iexact=email)
    except User.DoesNotExist:
        return Response({'error': 'User not found'}, status=status.HTTP_404_NOT_FOUND)

    user.password = make_password(new_password)
    user.save(update_fields=['password'])
    audit(user.user_id, user.tenant_id, 'auth.password_reset', request=request)

    return Response({'message': 'Password updated successfully'})


@api_view(['GET'])
def list_pending_registrations(request):
    if not request.user.has_litrix_perm('approve_registrations'):
        return Response({'error': 'Forbidden'}, status=status.HTTP_403_FORBIDDEN)

    where = '"Status" = %s'
    params = ['pending']
    if not request.user.has_litrix_perm('manage_users') and request.user.has_litrix_perm('approve_registrations'):
        # Scope HoD to the department they HEAD, not the one they work in.
        # A HoD can work in one department but be the head of another;
        # registration approval authority follows Department.HeadID.
        with connection.cursor() as cur:
            cur.execute(
                'SELECT "DepartmentID" FROM "Department" '
                'WHERE "HeadID" = %s LIMIT 1',
                [request.user.user_id],
            )
            r = cur.fetchone()
            if r and r[0]:
                where += ' AND "DepartmentID" = %s'
                params.append(r[0])

    with connection.cursor() as cur:
        # LEFT JOIN with Department so admins see the requested
        # department by name, not just an opaque ID. Done at the SQL
        # boundary instead of a frontend lookup so the list arrives
        # ready-to-render in a single round trip.
        cur.execute(f'''
            SELECT rr."RequestID", rr."Email",
                   rr."FullName_Ar", rr."FullName_En",
                   rr."Scholar_ID", rr."Orcid_ID", rr."Scopus_ID",
                   rr."DepartmentID",
                   d."DepartmentName" AS DepartmentName,
                   rr."AcademicRank", rr."Status", rr."CreatedAt"
            FROM "RegistrationRequest" rr
            LEFT JOIN "Department" d ON d."DepartmentID" = rr."DepartmentID"
            WHERE {where.replace('"Status"', 'rr."Status"').replace('"DepartmentID"', 'rr."DepartmentID"')}
            ORDER BY rr."CreatedAt" DESC
        ''', params)
        cols = [c[0] for c in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]

    # Per-row identity verification snapshot. Recomputed each call so the
    # admin sees the latest state (if Users were updated between submit
    # and approval, those changes are reflected).
    for r in rows:
        match = _lookup_existing_for_registration(
            scholar_id    = r.get('Scholar_ID'),
            orcid_id      = r.get('Orcid_ID'),
            email         = r.get('Email'),
            department_id = r.get('DepartmentID'),
            academic_rank = r.get('AcademicRank'),
            full_name_ar  = r.get('FullName_Ar'),
        )
        r['verification'] = {
            'match_found':       match['match_found'],
            'matched_by':        match['matched_by'],
            'has_mismatches':    bool(match['mismatches']),
            'mismatches':        match['mismatches'],
            'stored_papers':     (match.get('stored') or {}).get('papers_count', 0),
        }

    return Response({'requests': rows})


@api_view(['POST'])
def approve_registration(request, request_id):
    if not request.user.has_litrix_perm('approve_registrations'):
        return Response({'error': 'Forbidden'}, status=status.HTTP_403_FORBIDDEN)

    with transaction.atomic():
        with connection.cursor() as cur:
            cur.execute('''
                SELECT "Email", "FullName_Ar", "FullName_En", "Scholar_ID",
                       "Orcid_ID", "Scopus_ID", "DepartmentID", "AcademicRank",
                       "PasswordHash", "TenantID", "Status"
                FROM "RegistrationRequest"
                WHERE "RequestID" = %s
                FOR UPDATE
            ''', [request_id])
            row = cur.fetchone()
            if not row:
                return Response({'error': 'Not found'}, status=status.HTTP_404_NOT_FOUND)
            email, name_ar, name_en, sid, oid, scopus, dept, rank, pwd, tenant_id, st = row
            if st != 'pending':
                return Response({'error': f'Cannot approve from status: {st}'},
                                status=status.HTTP_400_BAD_REQUEST)

            # Empty-string → NULL coercion. The Users table carries UNIQUE
            # constraints on the academic-ID columns AND on FullName_Ar.
            # Postgres treats '' as a real value (only NULL is exempt
            # from UNIQUE), so two users with no Scholar_ID / no Arabic
            # name would both get '' and the second insert collides.
            # Normalising at the boundary keeps the DB clean and the
            # constraints useful.
            sid     = (sid     or '').strip() or None
            oid     = (oid     or '').strip() or None
            scopus  = (scopus  or '').strip() or None
            name_ar = (name_ar or '').strip() or None
            name_en = (name_en or '').strip() or None
            rank    = (rank    or '').strip() or None

            # Pre-split the English name into three parts. Done up here (not
            # inline in the INSERT) so we control the NULL-vs-'' behaviour on
            # each piece — empty string would still fight any FirstName /
            # LastName UNIQUE that gets added later. A three-part name maps
            # to First / Middle / Last; everything between first and last is
            # the middle name.
            if name_en:
                parts       = name_en.split()
                first_name  = parts[0]
                last_name   = parts[-1] if len(parts) > 1 else None
                middle_name = ' '.join(parts[1:-1]) if len(parts) > 2 else None
            else:
                first_name = middle_name = last_name = None

            cur.execute('''
                SELECT "RoleID" FROM "Role"
                WHERE "Name" = 'Researcher' AND "TenantID" = %s
            ''', [tenant_id])
            role_row = cur.fetchone()
            researcher_role_id = role_row[0] if role_row else None

            existing_user_id = None
            if sid:
                cur.execute('SELECT "UserID" FROM "Users" WHERE "Scholar_ID" = %s LIMIT 1', [sid])
                r = cur.fetchone()
                if r: existing_user_id = r[0]
            if not existing_user_id and oid:
                cur.execute('SELECT "UserID" FROM "Users" WHERE "Orcid_ID" = %s LIMIT 1', [oid])
                r = cur.fetchone()
                if r: existing_user_id = r[0]
            if not existing_user_id:
                cur.execute('SELECT "UserID" FROM "Users" WHERE LOWER("Email") = LOWER(%s) LIMIT 1', [email])
                r = cur.fetchone()
                if r: existing_user_id = r[0]

            if existing_user_id:
                cur.execute('''
                    UPDATE "Users"
                    SET "Email" = %s,
                        "PasswordHash" = %s,
                        "FullName_Ar" = COALESCE(%s, "FullName_Ar"),
                        "TenantID" = %s,
                        "RoleID" = COALESCE("RoleID", %s),
                        "UserType" = COALESCE("UserType", 'Researcher'),
                        "AccountStatus" = 'Active',
                        "EmailVerified" = TRUE,
                        "IsActive" = TRUE,
                        "Scholar_ID" = COALESCE("Scholar_ID", %s),
                        "Orcid_ID"   = COALESCE("Orcid_ID", %s),
                        "Scopus_ID"  = COALESCE("Scopus_ID", %s)
                    WHERE "UserID" = %s
                    RETURNING "UserID"
                ''', [email, pwd, name_ar, tenant_id, researcher_role_id,
                      sid, oid, scopus, existing_user_id])
                new_user_id = cur.fetchone()[0]
            else:
                cur.execute('''
                    INSERT INTO "Users"
                    ("Email", "PasswordHash", "FullName_Ar", "FirstName", "MiddleName", "LastName",
                     "UserType", "AccountStatus", "TenantID", "RoleID",
                     "EmailVerified", "IsActive", "Scholar_ID", "Orcid_ID", "Scopus_ID",
                     "CreatedAt")
                    VALUES (%s, %s, %s, %s, %s, %s, 'Researcher', 'Active', %s, %s,
                            TRUE, TRUE, %s, %s, %s, NOW())
                    RETURNING "UserID"
                ''', [
                    email, pwd, name_ar,
                    first_name, middle_name, last_name,
                    tenant_id, researcher_role_id,
                    sid, oid, scopus,
                ])
                new_user_id = cur.fetchone()[0]

            cur.execute('''
                INSERT INTO "Researcher" ("UserID", "LastSyncedAt")
                VALUES (%s, NULL)
                ON CONFLICT ("UserID") DO NOTHING
            ''', [new_user_id])

            if dept:
                cur.execute('''
                    INSERT INTO "Works_In" ("UserID", "DepartmentID", "StartDate", "IsCurrentPosition")
                    VALUES (%s, %s, CURRENT_DATE, TRUE)
                    ON CONFLICT DO NOTHING
                ''', [new_user_id, dept])

            cur.execute('''
                UPDATE "RegistrationRequest"
                SET "Status" = 'approved',
                    "ReviewedByUserID" = %s,
                    "ReviewedAt" = NOW()
                WHERE "RequestID" = %s
            ''', [request.user.user_id, request_id])

            cur.execute('''
                INSERT INTO "Notification" ("TenantID", "UserID", "Type", "Title", "Message")
                VALUES (%s, %s, 'registration_approved', %s, %s)
            ''', [tenant_id, new_user_id, 'Welcome to Litrix',
                  'Your registration has been approved. You can now log in.'])

    from .email_service import send_registration_approved
    send_registration_approved(email)
    audit(request.user.user_id, request.user.tenant_id,
          'registration.approve', 'RegistrationRequest', request_id, request=request)

    # Auto-kickoff the initial publication sync. Runs OUTSIDE the
    # transaction (we don't want a scraper failure to roll back the
    # approval itself) and only when the researcher provided an academic
    # ID we can actually scrape.
    #
    # Wrapped in try/except so any failure here logs a warning but
    # doesn't 500 the approve endpoint — the user is already approved
    # at this point (transaction committed). Admin can sync manually.
    sync_job_id = None
    sync_error   = None
    try:
        sync_job_id = _kickoff_initial_sync(
            user_id=new_user_id,
            tenant_id=tenant_id,
            scholar_id=sid,
            orcid_id=oid,
            triggered_by_user_id=request.user.user_id,
        )
    except Exception as e:
        import traceback
        traceback.print_exc()           # surface in Django console
        sync_error = str(e)

    return Response({
        'message':       'Registration approved',
        'user_id':       new_user_id,
        'sync_job_id':   sync_job_id,
        'sync_kicked':   sync_job_id is not None,
        'sync_source':   'scholar' if sid else ('orcid' if oid else None),
        'sync_error':    sync_error,    # null on success, traceback message on failure
    })


@api_view(['GET'])
def list_users(request):
    if not request.user.has_litrix_perm('manage_users'):
        return Response({'error': 'Forbidden'}, status=status.HTTP_403_FORBIDDEN)

    search = (request.GET.get('search') or '').strip()
    role_filter = request.GET.get('role')
    where = ['u."TenantID" = %s']
    params = [request.user.tenant_id]
    if search:
        where.append('(LOWER(u."Email") LIKE %s OR u."FullName_Ar" LIKE %s)')
        params.extend([f'%{search.lower()}%', f'%{search}%'])
    if role_filter:
        where.append('r."Name" = %s')
        params.append(role_filter)

    with connection.cursor() as cur:
        # Pull the current Department alongside the user so admins can
        # see "this person heads/works in <X>" before promoting them
        # to HoD. LATERAL prefers IsCurrentPosition but falls back to
        # any Works_In row, mirroring the registration-match lookup.
        cur.execute(f'''
            SELECT u."UserID", u."Email",
                   u."FullName_Ar", u."FirstName", u."MiddleName", u."LastName",
                   u."UserType",
                   u."AccountStatus", u."IsActive", u."EmailVerified",
                   u."Scholar_ID", u."Orcid_ID", u."Scopus_ID",
                   u."LastLoginAt", u."CreatedAt",
                   r."RoleID", r."Name" AS role_name,
                   w."DepartmentID", d."DepartmentName"
            FROM "Users" u
            LEFT JOIN "Role" r ON r."RoleID" = u."RoleID"
            LEFT JOIN LATERAL (
                SELECT wi."DepartmentID"
                FROM "Works_In" wi
                WHERE wi."UserID" = u."UserID"
                ORDER BY wi."IsCurrentPosition" DESC NULLS LAST,
                         wi."StartDate"         DESC NULLS LAST
                LIMIT 1
            ) w ON TRUE
            LEFT JOIN "Department" d ON d."DepartmentID" = w."DepartmentID"
            WHERE {" AND ".join(where)}
            ORDER BY u."CreatedAt" DESC
        ''', params)
        cols = [c[0] for c in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]

    return Response({'users': rows})


@api_view(['PATCH', 'DELETE'])
def update_user(request, user_id):
    if not request.user.has_litrix_perm('manage_users'):
        return Response({'error': 'Forbidden'}, status=status.HTTP_403_FORBIDDEN)

    if request.method == 'DELETE':
        return _delete_user(request, user_id)

    # ------------------------------------------------------------
    # Whitelist of editable Users columns. Anything outside this list
    # is silently ignored — protects sensitive cols (UserID, TenantID,
    # PasswordHash, Litrix_ID, Scholar_ID, CreatedAt) from being
    # mutated via this endpoint. To change those, build a dedicated
    # endpoint with stricter checks.
    # ------------------------------------------------------------
    EDITABLE = {
        # admin/role
        'role_id':         ('RoleID',         lambda v: v),
        'is_active':       ('IsActive',       lambda v: bool(v)),
        'user_type':       ('UserType',       lambda v: str(v).strip() or None),
        'account_status':  ('AccountStatus',  lambda v: str(v).strip() or None),
        # personal data
        'email':           ('Email',          lambda v: (str(v).strip().lower() or None)),
        'first_name':      ('FirstName',      lambda v: str(v).strip()),
        'middle_name':     ('MiddleName',     lambda v: str(v).strip() or None),
        'last_name':       ('LastName',       lambda v: str(v).strip()),
        'full_name_ar':    ('FullName_Ar',    lambda v: str(v).strip() or None),
    }

    fields, params = [], []
    for key, (column, cleaner) in EDITABLE.items():
        if key in request.data:
            fields.append(f'"{column}" = %s')
            params.append(cleaner(request.data[key]))

    if not fields:
        return Response({'error': 'Nothing to update'}, status=400)

    params.extend([user_id, request.user.tenant_id])
    with connection.cursor() as cur:
        cur.execute(f'''
            UPDATE "Users" SET {", ".join(fields)}
            WHERE "UserID" = %s AND "TenantID" = %s
            RETURNING "UserID"
        ''', params)
        if not cur.fetchone():
            return Response({'error': 'Not found'}, status=404)

    audit(request.user.user_id, request.user.tenant_id,
          'user.update', 'User', user_id, request.data, request=request)
    return Response({'message': 'Updated'})


def _delete_user(request, user_id: int):
    """
    Hard-delete a researcher and everything that's *only* about them.
    Audit trail and shared content are preserved:
      • Their author->paper LINKS are deleted, but papers themselves
        stay (they may carry other authors).
      • Department headship, registration reviewers, audit log entries,
        and sync-job triggers are nulled out so history remains
        readable but no FK points at a vanished UserID.
      • Their own Notifications and SyncJobs are deleted outright.

    Two guards:
      1. Self-delete is blocked — admins can't accidentally lock
         themselves out.
      2. The whole cascade runs in `transaction.atomic()` so either
         every step lands or nothing does.
    """
    # Self-protection — admins shouldn't be able to delete themselves.
    if int(user_id) == int(request.user.user_id):
        return Response(
            {'error': 'You cannot delete your own account.'},
            status=status.HTTP_400_BAD_REQUEST,
        )

    # Snapshot the user before deletion — needed for the audit row and
    # for a meaningful 200 response.
    with connection.cursor() as cur:
        cur.execute(
            '''
            SELECT u."UserID", u."Email", u."FullName_Ar",
                   u."Litrix_ID", u."UserType",
                   (SELECT COUNT(*) FROM "Authors" a WHERE a."UserID" = u."UserID")
                       AS papers
            FROM "Users" u
            WHERE u."UserID" = %s AND u."TenantID" = %s
            ''',
            [user_id, request.user.tenant_id],
        )
        row = cur.fetchone()
        if not row:
            return Response({'error': 'Not found'}, status=status.HTTP_404_NOT_FOUND)
        snapshot = {
            'user_id':      row[0],
            'email':        row[1],
            'full_name_ar': row[2],
            'litrix_id':    row[3],
            'user_type':    row[4],
            'papers':       row[5] or 0,
        }

    # If this account has papers, a hard delete would orphan them (its
    # Authors links get removed). Instead we UN-REGISTER: strip the login +
    # registration artifacts but keep the researcher profile, their author
    # links, department, and every paper. Only truly footprint-less accounts
    # (no papers) are hard-deleted below.
    if snapshot['papers'] > 0:
        return _unregister_user(request, user_id, snapshot)

    def _table_exists(cur, name: str) -> bool:
        cur.execute(
            'SELECT 1 FROM information_schema.tables '
            'WHERE table_schema = current_schema() AND table_name = %s',
            [name],
        )
        return cur.fetchone() is not None

    with transaction.atomic():
        with connection.cursor() as cur:
            # ---- Phase 1: null out cross-table references --------------
            # Use the safe SET NULL pattern to keep history readable.
            cur.execute(
                'UPDATE "Department" SET "HeadID" = NULL WHERE "HeadID" = %s',
                [user_id],
            )
            cur.execute(
                'UPDATE "RegistrationRequest" SET "ReviewedByUserID" = NULL '
                'WHERE "ReviewedByUserID" = %s',
                [user_id],
            )
            cur.execute(
                'UPDATE "AuditLog" SET "UserID" = NULL WHERE "UserID" = %s',
                [user_id],
            )
            cur.execute(
                'UPDATE "SyncJob" SET "TriggeredBy" = NULL '
                'WHERE "TriggeredBy" = %s',
                [user_id],
            )

            if _table_exists(cur, 'Invitation'):
                cur.execute(
                    'UPDATE "Invitation" SET "InvitedByUserID" = NULL '
                    'WHERE "InvitedByUserID" = %s',
                    [user_id],
                )
                cur.execute(
                    'UPDATE "Invitation" SET "UsedByUserID" = NULL '
                    'WHERE "UsedByUserID" = %s',
                    [user_id],
                )

            # SimpleJWT outstanding refresh tokens hold a FK to Users.
            # Postgres won't drop the User while these rows exist. We
            # also have to delete the blacklist rows first because they
            # reference outstanding by id, not by user_id, so they
            # don't cascade automatically.
            if _table_exists(cur, 'token_blacklist_blacklistedtoken') \
                    and _table_exists(cur, 'token_blacklist_outstandingtoken'):
                cur.execute('''
                    DELETE FROM token_blacklist_blacklistedtoken
                    WHERE token_id IN (
                        SELECT id FROM token_blacklist_outstandingtoken
                        WHERE user_id = %s
                    )
                ''', [user_id])
            if _table_exists(cur, 'token_blacklist_outstandingtoken'):
                cur.execute(
                    'DELETE FROM token_blacklist_outstandingtoken '
                    'WHERE user_id = %s',
                    [user_id],
                )

            # ---- Phase 2: delete user-owned rows -----------------------
            cur.execute('DELETE FROM "SyncJob"      WHERE "UserID" = %s', [user_id])
            cur.execute('DELETE FROM "Notification" WHERE "UserID" = %s', [user_id])
            cur.execute('DELETE FROM "Authors"      WHERE "UserID" = %s', [user_id])
            cur.execute('DELETE FROM "Works_In"     WHERE "UserID" = %s', [user_id])
            cur.execute('DELETE FROM "Researcher"   WHERE "UserID" = %s', [user_id])

            # ---- Phase 3: the user row itself --------------------------
            cur.execute(
                'DELETE FROM "Users" WHERE "UserID" = %s AND "TenantID" = %s',
                [user_id, request.user.tenant_id],
            )

    audit(
        request.user.user_id,
        request.user.tenant_id,
        'user.delete',
        'User',
        user_id,
        snapshot,
        request=request,
    )
    return Response({'message': 'User deleted', 'deleted': snapshot})


def _unregister_user(request, user_id: int, snapshot: dict):
    """
    Soft-remove an account that HAS papers: strip the login + everything the
    registration created, but keep the scraped researcher profile, its author
    links, department membership, and every paper. A hard delete would drop
    the Authors links and orphan those papers, so this is used instead.

    Reverts the Users row to the unregistered scraped-researcher state
    (AccountStatus='Pending', no password, no email, UserType='Researcher')
    and clears registration side-effects: welcome notifications, the initial
    sync job, registration requests, consumed/issued invitations, any
    headship, and active JWT sessions. Idempotent and atomic.
    """
    tenant_id = request.user.tenant_id
    with transaction.atomic():
        with connection.cursor() as cur:
            cur.execute('SELECT "Email" FROM "Users" WHERE "UserID" = %s', [user_id])
            r = cur.fetchone()
            email = r[0] if r else None

            # Registration records tied to this email + invitations.
            #
            # We RE-OPEN the invitation (RevokedAt/UsedAt/UsedByUserID → NULL)
            # instead of revoking it, so the SAME invitation link works again
            # to register this researcher anew. Re-registration then reclaims
            # this very Users row through the Scholar_ID/ORCID claim-profile
            # path (keeping every paper). Revoking it — the old behaviour —
            # left the original link dead ("invalid_invitation · revoked")
            # whenever an un-registered account tried to sign up again.
            if email:
                cur.execute(
                    'DELETE FROM "RegistrationRequest" WHERE LOWER("Email") = LOWER(%s)',
                    [email],
                )
                cur.execute(
                    'UPDATE "Invitation" SET "RevokedAt" = NULL, "UsedAt" = NULL, '
                    '"UsedByUserID" = NULL WHERE LOWER("InvitedEmail") = LOWER(%s)',
                    [email],
                )
            cur.execute(
                'UPDATE "Invitation" SET "RevokedAt" = NULL, "UsedAt" = NULL, '
                '"UsedByUserID" = NULL WHERE "UsedByUserID" = %s',
                [user_id],
            )

            # Welcome notifications + the registration-triggered sync job.
            cur.execute(
                'DELETE FROM "Notification" WHERE "UserID" = %s '
                "AND \"Type\" IN ('registration_approved', 'invitation_accepted')",
                [user_id],
            )
            cur.execute('DELETE FROM "SyncJob" WHERE "UserID" = %s', [user_id])

            # Defensive: drop any headship the (possibly promoted) account held.
            cur.execute(
                'UPDATE "Department" SET "HeadID" = NULL WHERE "HeadID" = %s',
                [user_id],
            )

            # Invalidate active JWT sessions so the removed login can't keep
            # working until token expiry (mirrors the hard-delete cleanup).
            cur.execute(
                'SELECT 1 FROM information_schema.tables '
                'WHERE table_name = %s', ['token_blacklist_outstandingtoken'])
            if cur.fetchone():
                cur.execute(
                    'DELETE FROM token_blacklist_blacklistedtoken '
                    'WHERE token_id IN (SELECT id FROM token_blacklist_outstandingtoken '
                    'WHERE user_id = %s)', [user_id])
                cur.execute(
                    'DELETE FROM token_blacklist_outstandingtoken WHERE user_id = %s',
                    [user_id])

            # Revert the Users row to the unregistered scraped-researcher state.
            cur.execute(
                '''UPDATE "Users" SET
                       "PasswordHash"  = NULL,
                       "Email"         = NULL,
                       "AccountStatus" = 'Pending',
                       "EmailVerified" = FALSE,
                       "LastLoginAt"   = NULL,
                       "UserType"      = 'Researcher',
                       "RoleID"        = (SELECT "RoleID" FROM "Role"
                                          WHERE "Name" = 'Researcher' AND "TenantID" = %s)
                   WHERE "UserID" = %s''',
                [tenant_id, user_id],
            )

    audit(
        request.user.user_id, tenant_id,
        'user.unregister', 'User', user_id, snapshot, request=request,
    )
    return Response({
        'message': 'Account unregistered — the login was removed but the '
                   'researcher profile and all their papers were kept.',
        'unregistered': snapshot,
    })


@api_view(['GET'])
def list_notifications(request):
    only_unread = request.GET.get('unread') == 'true'
    where = ['"UserID" = %s']
    params = [request.user.user_id]
    if only_unread:
        where.append('"IsRead" = FALSE')

    with connection.cursor() as cur:
        cur.execute(f'''
            SELECT "NotificationID", "Type", "Title", "Message",
                   "Metadata", "IsRead", "CreatedAt", "ReadAt"
            FROM "Notification"
            WHERE {" AND ".join(where)}
            ORDER BY "CreatedAt" DESC LIMIT 100
        ''', params)
        cols = [c[0] for c in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]
        cur.execute(
            'SELECT COUNT(*) FROM "Notification" WHERE "UserID" = %s AND "IsRead" = FALSE',
            [request.user.user_id],
        )
        unread_count = cur.fetchone()[0]
    return Response({'notifications': rows, 'unread_count': unread_count})


@api_view(['POST'])
def mark_notification_read(request, notif_id):
    with connection.cursor() as cur:
        cur.execute('''
            UPDATE "Notification" SET "IsRead" = TRUE, "ReadAt" = NOW()
            WHERE "NotificationID" = %s AND "UserID" = %s AND "IsRead" = FALSE
        ''', [notif_id, request.user.user_id])
    return Response({'message': 'OK'})


@api_view(['POST'])
def mark_all_read(request):
    with connection.cursor() as cur:
        cur.execute('''
            UPDATE "Notification" SET "IsRead" = TRUE, "ReadAt" = NOW()
            WHERE "UserID" = %s AND "IsRead" = FALSE
        ''', [request.user.user_id])
    return Response({'message': 'All marked read'})


@api_view(['GET'])
def list_audit_log(request):
    if not request.user.has_litrix_perm('view_audit_log'):
        return Response({'error': 'Forbidden'}, status=status.HTTP_403_FORBIDDEN)

    action_filter = request.GET.get('action')
    user_filter = request.GET.get('user_id')
    where = ['al."TenantID" = %s']
    params = [request.user.tenant_id]
    if action_filter:
        where.append('al."Action" LIKE %s')
        params.append(f'{action_filter}%')
    if user_filter:
        where.append('al."UserID" = %s')
        params.append(int(user_filter))

    with connection.cursor() as cur:
        cur.execute(f'''
            SELECT al."LogID", al."Action", al."TargetType", al."TargetID",
                   al."Metadata", al."IpAddress", al."CreatedAt",
                   u."Email", u."FullName_Ar"
            FROM "AuditLog" al
            LEFT JOIN "Users" u ON u."UserID" = al."UserID"
            WHERE {" AND ".join(where)}
            ORDER BY al."CreatedAt" DESC LIMIT 200
        ''', params)
        cols = [c[0] for c in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    return Response({'logs': rows})


@api_view(['GET'])
def list_roles(request):
    if not request.user.has_litrix_perm('manage_users') and not request.user.has_litrix_perm('manage_roles'):
        return Response({'error': 'Forbidden'}, status=status.HTTP_403_FORBIDDEN)
    with connection.cursor() as cur:
        cur.execute('''
            SELECT "RoleID", "Name", "Description", "IsSystem"
            FROM "Role" WHERE "TenantID" = %s ORDER BY "RoleID"
        ''', [request.user.tenant_id])
        cols = [c[0] for c in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    return Response({'roles': rows})


@api_view(['GET'])
def list_permissions(request):
    if not request.user.has_litrix_perm('manage_roles'):
        return Response({'error': 'Forbidden'}, status=status.HTTP_403_FORBIDDEN)
    with connection.cursor() as cur:
        cur.execute('SELECT "PermissionID", "Code", "Description", "Category" FROM "Permission" ORDER BY "Category", "Code"')
        cols = [c[0] for c in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    return Response({'permissions': rows})


@api_view(['GET'])
def get_role_permissions(request, role_id):
    if not request.user.has_litrix_perm('manage_roles'):
        return Response({'error': 'Forbidden'}, status=status.HTTP_403_FORBIDDEN)
    with connection.cursor() as cur:
        cur.execute('''
            SELECT p."PermissionID" FROM "RolePermission" rp
            JOIN "Permission" p ON p."PermissionID" = rp."PermissionID"
            WHERE rp."RoleID" = %s
        ''', [role_id])
        ids = [r[0] for r in cur.fetchall()]
    return Response({'permission_ids': ids})


@api_view(['PUT'])
def set_role_permissions(request, role_id):
    if not request.user.has_litrix_perm('manage_roles'):
        return Response({'error': 'Forbidden'}, status=status.HTTP_403_FORBIDDEN)

    permission_ids = request.data.get('permission_ids') or []
    with connection.cursor() as cur:
        cur.execute('''
            SELECT 1 FROM "Role" WHERE "RoleID" = %s AND "TenantID" = %s
        ''', [role_id, request.user.tenant_id])
        if not cur.fetchone():
            return Response({'error': 'Role not found'}, status=404)

        cur.execute('DELETE FROM "RolePermission" WHERE "RoleID" = %s', [role_id])
        for pid in permission_ids:
            cur.execute(
                'INSERT INTO "RolePermission" ("RoleID", "PermissionID") VALUES (%s, %s) ON CONFLICT DO NOTHING',
                [role_id, pid],
            )

    audit(request.user.user_id, request.user.tenant_id,
          'role.update_permissions', 'Role', role_id,
          {'count': len(permission_ids)}, request=request)
    return Response({'message': 'Updated', 'count': len(permission_ids)})


@api_view(['POST'])
def create_role(request):
    if not request.user.has_litrix_perm('manage_roles'):
        return Response({'error': 'Forbidden'}, status=status.HTTP_403_FORBIDDEN)

    name = (request.data.get('name') or '').strip()
    description = (request.data.get('description') or '').strip()
    if not name:
        return Response({'error': 'Name required'}, status=400)

    try:
        with connection.cursor() as cur:
            cur.execute('''
                INSERT INTO "Role" ("TenantID", "Name", "Description", "IsSystem")
                VALUES (%s, %s, %s, FALSE) RETURNING "RoleID"
            ''', [request.user.tenant_id, name, description])
            role_id = cur.fetchone()[0]
    except psycopg2.IntegrityError:
        return Response({'error': 'Role name already exists'}, status=400)

    audit(request.user.user_id, request.user.tenant_id,
          'role.create', 'Role', role_id, {'name': name}, request=request)
    return Response({'role_id': role_id, 'name': name}, status=201)


@api_view(['DELETE'])
def delete_role(request, role_id):
    if not request.user.has_litrix_perm('manage_roles'):
        return Response({'error': 'Forbidden'}, status=status.HTTP_403_FORBIDDEN)
    with connection.cursor() as cur:
        cur.execute('SELECT "IsSystem" FROM "Role" WHERE "RoleID" = %s AND "TenantID" = %s',
                    [role_id, request.user.tenant_id])
        row = cur.fetchone()
        if not row:
            return Response({'error': 'Not found'}, status=404)
        if row[0]:
            return Response({'error': 'Cannot delete system role'}, status=400)
        cur.execute('UPDATE "Users" SET "RoleID" = NULL WHERE "RoleID" = %s', [role_id])
        cur.execute('DELETE FROM "Role" WHERE "RoleID" = %s', [role_id])
    audit(request.user.user_id, request.user.tenant_id,
          'role.delete', 'Role', role_id, request=request)
    return Response({'message': 'Deleted'})


@api_view(['POST'])
def reject_registration(request, request_id):
    if not request.user.has_litrix_perm('approve_registrations'):
        return Response({'error': 'Forbidden'}, status=status.HTTP_403_FORBIDDEN)

    reason = (request.data.get('reason') or '').strip()
    with connection.cursor() as cur:
        cur.execute('''
            UPDATE "RegistrationRequest"
            SET "Status" = 'rejected',
                "RejectionReason" = %s,
                "ReviewedByUserID" = %s,
                "ReviewedAt" = NOW()
            WHERE "RequestID" = %s AND "Status" = 'pending'
            RETURNING "Email", "TenantID"
        ''', [reason, request.user.user_id, request_id])
        row = cur.fetchone()

    if not row:
        return Response({'error': 'Not found or not pending'},
                        status=status.HTTP_404_NOT_FOUND)

    from .email_service import send_registration_rejected
    send_registration_rejected(row[0], reason)
    audit(request.user.user_id, request.user.tenant_id,
          'registration.reject', 'RegistrationRequest', request_id,
          {'reason': reason}, request=request)

    return Response({'message': 'Registration rejected'})
