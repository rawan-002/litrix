import os
import subprocess
import threading
from pathlib import Path
from django.db import connection
from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.response import Response


PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
SCRAPER_DIR = PROJECT_ROOT / 'scrapers'

# ============================================================================
# Sync cooldown — researchers who already have papers stored shouldn't be
# re-scraped on every whim. Hitting Scholar/OpenAlex repeatedly burns the
# SerpAPI quota and rarely yields new data within a single week.
#
# The gate is enforced in TWO places (defense-in-depth):
#   1. trigger_sync API — rejects with 409 unless force=true.
#   2. The scraper scripts themselves — exit early when invoked without
#      --force on a recently-synced researcher (protects against CLI
#      invocation bypassing the API).
# ============================================================================
SYNC_COOLDOWN_DAYS = 7


def check_sync_eligibility(user_id: int) -> dict:
    """
    Decide whether a researcher should be (re-)scraped.

    Returns a dict with:
        eligible        : bool   — True only when sync should proceed
        reason          : str    — human-readable code
        last_synced_at  : str|None
        papers_count    : int
        cooldown_until  : str|None  ISO datetime when cooldown expires
    """
    with connection.cursor() as cur:
        cur.execute('''
            SELECT
                r."LastSyncedAt",
                (SELECT COUNT(*) FROM "Authors" a WHERE a."UserID" = %s) AS papers
            FROM "Users" u
            LEFT JOIN "Researcher" r ON r."UserID" = u."UserID"
            WHERE u."UserID" = %s
        ''', [user_id, user_id])
        row = cur.fetchone()

    if not row:
        return {
            'eligible': False, 'reason': 'user_not_found',
            'last_synced_at': None, 'papers_count': 0,
            'cooldown_until': None,
        }

    last_synced, papers = row[0], row[1] or 0

    # Never synced AND no papers → first-time sync, always eligible.
    if last_synced is None and papers == 0:
        return {
            'eligible': True, 'reason': 'first_time',
            'last_synced_at': None, 'papers_count': 0,
            'cooldown_until': None,
        }

    # Has papers but no LastSyncedAt — defensive: papers were imported
    # by another path. Treat as "needs sync" but warn caller.
    if last_synced is None:
        return {
            'eligible': True, 'reason': 'unsynced_with_papers',
            'last_synced_at': None, 'papers_count': papers,
            'cooldown_until': None,
        }

    from datetime import timedelta
    from django.utils import timezone
    cooldown_until = last_synced + timedelta(days=SYNC_COOLDOWN_DAYS)
    in_cooldown = timezone.now() < cooldown_until

    return {
        'eligible': not in_cooldown,
        'reason': 'in_cooldown' if in_cooldown else 'cooldown_elapsed',
        'last_synced_at': last_synced.isoformat(),
        'papers_count': papers,
        'cooldown_until': cooldown_until.isoformat(),
    }


def _run_scraper(job_id, source, args):
    with connection.cursor() as cur:
        cur.execute(
            'UPDATE "SyncJob" SET "Status" = %s, "StartedAt" = NOW() WHERE "JobID" = %s',
            ['running', job_id],
        )

    script_map = {
        'scholar': str(SCRAPER_DIR / 'scholar.py'),
        'orcid':   str(SCRAPER_DIR / 'orcid.py'),
        'manual':  str(SCRAPER_DIR / 'manual.py'),
    }
    script = script_map.get(source)
    if not script:
        with connection.cursor() as cur:
            cur.execute(
                'UPDATE "SyncJob" SET "Status" = %s, "FinishedAt" = NOW(), '
                '"ErrorMessage" = %s WHERE "JobID" = %s',
                ['failed', f'Unknown source: {source}', job_id],
            )
        return

    cmd = ['python', script] + args
    env = os.environ.copy()
    env['PYTHONIOENCODING'] = 'utf-8'

    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True,
            encoding='utf-8', errors='replace',
            env=env, timeout=600, cwd=str(PROJECT_ROOT),
        )
        ok = result.returncode == 0
        with connection.cursor() as cur:
            cur.execute('''
                UPDATE "SyncJob"
                SET "Status" = %s,
                    "FinishedAt" = NOW(),
                    "ErrorMessage" = %s,
                    "Metadata" = jsonb_set(
                        COALESCE("Metadata", '{}'::jsonb),
                        '{stdout_tail}',
                        to_jsonb(%s::text)
                    )
                WHERE "JobID" = %s
            ''', [
                'completed' if ok else 'failed',
                None if ok else (result.stderr or '')[-500:],
                (result.stdout or '')[-2000:],
                job_id,
            ])
    except subprocess.TimeoutExpired:
        with connection.cursor() as cur:
            cur.execute(
                'UPDATE "SyncJob" SET "Status" = %s, "FinishedAt" = NOW(), '
                '"ErrorMessage" = %s WHERE "JobID" = %s',
                ['failed', 'Timeout (10min)', job_id],
            )
    except Exception as e:
        with connection.cursor() as cur:
            cur.execute(
                'UPDATE "SyncJob" SET "Status" = %s, "FinishedAt" = NOW(), '
                '"ErrorMessage" = %s WHERE "JobID" = %s',
                ['failed', str(e), job_id],
            )


@api_view(['POST'])
def trigger_sync(request):
    if not request.user.has_litrix_perm('trigger_sync'):
        return Response({'error': 'Forbidden'}, status=status.HTTP_403_FORBIDDEN)

    target_user_id = request.data.get('user_id')
    source = request.data.get('source', 'scholar')
    # Allow callers to bypass the cooldown explicitly. Accept any truthy
    # value so the frontend can send `true`, `1`, "true", etc.
    force = bool(request.data.get('force', False))

    if not target_user_id:
        return Response({'error': 'user_id required'}, status=400)

    with connection.cursor() as cur:
        cur.execute('''
            SELECT "Scholar_ID", "Orcid_ID", "FullName_Ar"
            FROM "Users" WHERE "UserID" = %s
        ''', [target_user_id])
        row = cur.fetchone()
        if not row:
            return Response({'error': 'User not found'}, status=404)
        scholar_id, orcid_id, name_ar = row

    if source == 'scholar' and not scholar_id:
        return Response({'error': 'User has no Scholar_ID'}, status=400)
    if source == 'orcid' and not orcid_id:
        return Response({'error': 'User has no ORCID'}, status=400)

    # Cooldown gate — protects the SerpAPI quota and avoids needless
    # re-processing of already-stored papers. Surfaces enough metadata
    # for the frontend to render a meaningful "Force re-sync" prompt.
    eligibility = check_sync_eligibility(int(target_user_id))
    if not eligibility['eligible'] and not force:
        return Response(
            {
                'error': 'in_cooldown',
                'message': (
                    f'Researcher was last synced recently and has '
                    f'{eligibility["papers_count"]} papers stored. '
                    f'Send force=true to override.'
                ),
                **eligibility,
            },
            status=status.HTTP_409_CONFLICT,
        )

    if source == 'scholar':
        args = [scholar_id, str(target_user_id)]
    elif source == 'orcid':
        args = ['--orcid', orcid_id, '--user', str(target_user_id)]
    else:
        args = ['--uid', str(target_user_id)]

    # Pass --force through to the scraper script. The scraper enforces
    # the same cooldown as a second line of defense; without --force it
    # will refuse to call SerpAPI even if the API layer somehow let it
    # through (e.g. CLI invocation, test scripts, etc).
    if force:
        args.append('--force')

    with connection.cursor() as cur:
        # Explicit %s::jsonb cast — see _kickoff_initial_sync in views.py
        # for the full reasoning. Postgres won't implicitly cast TEXT → JSONB.
        cur.execute('''
            INSERT INTO "SyncJob" (
                "TenantID", "UserID", "TriggeredBy", "Source", "Status", "Metadata"
            )
            VALUES (%s, %s, %s, %s, 'queued', %s::jsonb)
            RETURNING "JobID"
        ''', [
            request.user.tenant_id, target_user_id, request.user.user_id, source,
            # Audit trail — was this a forced sync, what was the prior state?
            __import__('json').dumps({
                'force': force,
                'prior_papers_count': eligibility['papers_count'],
                'prior_last_synced_at': eligibility['last_synced_at'],
            }),
        ])
        job_id = cur.fetchone()[0]

    threading.Thread(
        target=_run_scraper, args=(job_id, source, args), daemon=True,
    ).start()

    return Response({
        'job_id': job_id,
        'message': 'Sync started',
        'force': force,
        'prior_papers_count': eligibility['papers_count'],
    }, status=201)


def _run_project_script(job_id, steps):
    """Background runner for repo-level maintenance jobs made of one or
    more sequential script steps.

    steps: list of (label, script_path, args, timeout) tuples. Steps run
    in order; a failed step aborts the chain and marks the job failed.
    Lifecycle mirrors _run_scraper (queued → running → completed/failed)
    with each step's stdout tail captured into Metadata.
    """
    with connection.cursor() as cur:
        cur.execute(
            'UPDATE "SyncJob" SET "Status" = %s, "StartedAt" = NOW() WHERE "JobID" = %s',
            ['running', job_id],
        )

    env = os.environ.copy()
    env['PYTHONIOENCODING'] = 'utf-8'

    tails = []
    status_final, error_msg = 'completed', None
    for label, script_path, args, timeout in steps:
        cmd = ['python', str(script_path)] + args
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True,
                encoding='utf-8', errors='replace',
                env=env, timeout=timeout, cwd=str(PROJECT_ROOT),
            )
            tails.append(f'=== {label} ===\n' + (result.stdout or '')[-1500:])
            if result.returncode != 0:
                status_final = 'failed'
                error_msg = f'[{label}] ' + (result.stderr or 'non-zero exit')[-400:]
                break
        except subprocess.TimeoutExpired:
            status_final = 'failed'
            error_msg = f'[{label}] Timeout ({timeout // 60}min)'
            break
        except Exception as e:
            status_final = 'failed'
            error_msg = f'[{label}] {e}'
            break

    with connection.cursor() as cur:
        cur.execute('''
            UPDATE "SyncJob"
            SET "Status" = %s,
                "FinishedAt" = NOW(),
                "ErrorMessage" = %s,
                "Metadata" = jsonb_set(
                    COALESCE("Metadata", '{}'::jsonb),
                    '{stdout_tail}',
                    to_jsonb(%s::text)
                )
            WHERE "JobID" = %s
        ''', [status_final, error_msg, '\n\n'.join(tails)[-3000:], job_id])


def _run_citation_refresh(job_id, args):
    """citations/refresh_hybrid.py — citation NUMBERS only on existing papers
    (never inserts papers, never touches Authors links)."""
    _run_project_script(job_id, [
        ('citation-refresh',
         PROJECT_ROOT / 'citations' / 'refresh_hybrid.py', args, 3600),
    ])


def _run_new_papers_scrape(job_id, args):
    """Two-step chain (user requirement — scrape MUST verify affiliation):

    1. scrapers/scholar_new_papers.py — NEW papers only: per researcher,
       fetches Scholar newest-first and stops at the first paper older
       than their latest stored PubYear. Existing papers are never
       re-downloaded or modified; cooldown (LastSyncedAt) untouched.
    2. backend/affiliation_verifier.py --apply --source Scholar — the
       multi-tier Al-Baha affiliation check (OpenAlex → Crossref → PDF →
       publisher HTML) runs over the still-unverified Scholar papers,
       which includes everything step 1 just inserted. Papers confirmed
       NOT Al-Baha get AffiliationVerified=FALSE and drop out of the
       dashboards; unresolvable ones stay NULL for manual review.
    """
    _run_project_script(job_id, [
        ('scrape-new-papers',
         PROJECT_ROOT / 'scrapers' / 'scholar_new_papers.py', args, 3600),
        ('affiliation-verify',
         PROJECT_ROOT / 'backend' / 'affiliation_verifier.py',
         ['--apply', '--source', 'Scholar'], 1800),
    ])


@api_view(['POST'])
def trigger_citation_refresh(request):
    """POST /api/auth/sync/refresh-citations/

    Body (all optional):
        scope       : 'missing' (default — only papers without citation data)
                      | 'all' (re-refresh every paper's citation numbers)
        serp_budget : int >= 0, default 0. Hard cap on PAID SerpAPI calls;
                      0 = OpenAlex only (free). Each call costs 1 credit.
        user_id     : restrict the refresh to one researcher's papers.

    Citation-numbers-only by design: the underlying script never inserts
    papers, never edits paper metadata, never touches Authors links, and
    never modifies Researcher.CitationsByYear (the dashboard's author-level
    graph stays Scholar-owned).
    """
    if not request.user.has_litrix_perm('trigger_sync'):
        return Response({'error': 'Forbidden'}, status=status.HTTP_403_FORBIDDEN)

    scope = request.data.get('scope', 'missing')
    if scope not in ('missing', 'all'):
        return Response({'error': "scope must be 'missing' or 'all'"}, status=400)

    try:
        serp_budget = max(0, int(request.data.get('serp_budget', 0)))
    except (TypeError, ValueError):
        return Response({'error': 'serp_budget must be an integer'}, status=400)
    # Server-side ceiling so a typo in the UI can't burn the whole quota.
    serp_budget = min(serp_budget, 500)

    user_id = request.data.get('user_id')

    # One refresh at a time — a second concurrent run would duplicate API
    # calls (and SerpAPI spend) for zero benefit.
    with connection.cursor() as cur:
        cur.execute('''
            SELECT "JobID" FROM "SyncJob"
            WHERE "Source" = 'citations' AND "Status" IN ('queued', 'running')
            LIMIT 1
        ''')
        running = cur.fetchone()
    if running:
        return Response(
            {'error': 'already_running',
             'message': f'Citation refresh job #{running[0]} is still running.'},
            status=status.HTTP_409_CONFLICT,
        )

    args = ['--apply']
    if scope == 'all':
        args.append('--all')
    if serp_budget > 0:
        args += ['--serp-budget', str(serp_budget)]
    else:
        args.append('--no-serp')
    if user_id:
        args += ['--user', str(int(user_id))]

    with connection.cursor() as cur:
        cur.execute('''
            INSERT INTO "SyncJob" (
                "TenantID", "UserID", "TriggeredBy", "Source", "Status", "Metadata"
            )
            VALUES (%s, %s, %s, 'citations', 'queued', %s::jsonb)
            RETURNING "JobID"
        ''', [
            request.user.tenant_id,
            int(user_id) if user_id else None,
            request.user.user_id,
            __import__('json').dumps({
                'scope': scope,
                'serp_budget': serp_budget,
            }),
        ])
        job_id = cur.fetchone()[0]

    threading.Thread(
        target=_run_citation_refresh, args=(job_id, args), daemon=True,
    ).start()

    return Response({
        'job_id': job_id,
        'message': 'Citation refresh started',
        'scope': scope,
        'serp_budget': serp_budget,
    }, status=201)


@api_view(['POST'])
def trigger_new_papers_scrape(request):
    """POST /api/auth/sync/scrape-new/

    Incremental scrape — NEW papers only (user requirement):
    for every researcher with a Scholar_ID, look up the year of their most
    recent stored paper, fetch their Scholar profile newest-first, and stop
    the moment an older paper appears. Only genuinely-new papers are
    inserted; existing papers are never re-downloaded or modified.

    Body (optional):
        serp_budget : int, default 100 — hard cap on SerpAPI calls.
                      A researcher with no new papers costs exactly 1 call.
        user_id     : restrict to one researcher.
    """
    if not request.user.has_litrix_perm('trigger_sync'):
        return Response({'error': 'Forbidden'}, status=status.HTTP_403_FORBIDDEN)

    try:
        serp_budget = max(1, int(request.data.get('serp_budget', 100)))
    except (TypeError, ValueError):
        return Response({'error': 'serp_budget must be an integer'}, status=400)
    serp_budget = min(serp_budget, 500)

    user_id = request.data.get('user_id')

    # One incremental scrape at a time — concurrent runs would race on the
    # same researchers and double the SerpAPI spend.
    with connection.cursor() as cur:
        cur.execute('''
            SELECT "JobID" FROM "SyncJob"
            WHERE "Source" = 'scrape_new' AND "Status" IN ('queued', 'running')
            LIMIT 1
        ''')
        running = cur.fetchone()
    if running:
        return Response(
            {'error': 'already_running',
             'message': f'New-papers scrape job #{running[0]} is still running.'},
            status=status.HTTP_409_CONFLICT,
        )

    args = ['--apply', '--budget', str(serp_budget)]
    if user_id:
        args += ['--user', str(int(user_id))]

    with connection.cursor() as cur:
        cur.execute('''
            INSERT INTO "SyncJob" (
                "TenantID", "UserID", "TriggeredBy", "Source", "Status", "Metadata"
            )
            VALUES (%s, %s, %s, 'scrape_new', 'queued', %s::jsonb)
            RETURNING "JobID"
        ''', [
            request.user.tenant_id,
            int(user_id) if user_id else None,
            request.user.user_id,
            __import__('json').dumps({'serp_budget': serp_budget}),
        ])
        job_id = cur.fetchone()[0]

    threading.Thread(
        target=_run_new_papers_scrape, args=(job_id, args), daemon=True,
    ).start()

    return Response({
        'job_id': job_id,
        'message': 'New-papers scrape started',
        'serp_budget': serp_budget,
    }, status=201)


@api_view(['GET'])
def list_sync_jobs(request):
    if not request.user.has_litrix_perm('trigger_sync'):
        return Response({'error': 'Forbidden'}, status=status.HTTP_403_FORBIDDEN)

    user_filter = request.GET.get('user_id')
    where = ['sj."TenantID" = %s']
    params = [request.user.tenant_id]
    if user_filter:
        where.append('sj."UserID" = %s')
        params.append(int(user_filter))

    with connection.cursor() as cur:
        # Surface enough name signals so the frontend can fall back
        # gracefully when FullName_Ar is missing (researcher imported
        # with English-only metadata, etc.). Display order on the
        # client: Arabic name → English first+last → Email → User #ID.
        cur.execute(f'''
            SELECT sj."JobID", sj."Source", sj."Status",
                   sj."StartedAt", sj."FinishedAt", sj."ErrorMessage",
                   sj."CreatedAt",
                   u."UserID", u."FullName_Ar", u."Email",
                   u."FirstName", u."LastName",
                   tu."FullName_Ar" AS triggered_by_name
            FROM "SyncJob" sj
            LEFT JOIN "Users" u ON u."UserID" = sj."UserID"
            LEFT JOIN "Users" tu ON tu."UserID" = sj."TriggeredBy"
            WHERE {" AND ".join(where)}
            ORDER BY sj."CreatedAt" DESC LIMIT 50
        ''', params)
        cols = [c[0] for c in cur.description]
        jobs = [dict(zip(cols, r)) for r in cur.fetchall()]

    return Response({'jobs': jobs})


@api_view(['GET'])
def list_syncable_researchers(request):
    if not request.user.has_litrix_perm('trigger_sync'):
        return Response({'error': 'Forbidden'}, status=status.HTTP_403_FORBIDDEN)

    # We compute cooldown fields right here in SQL so the frontend can
    # disable the Sync button and offer a Force option without an extra
    # round trip per researcher.
    with connection.cursor() as cur:
        cur.execute('''
            SELECT u."UserID", u."Litrix_ID", u."FullName_Ar", u."Email",
                   u."Scholar_ID", u."Orcid_ID",
                   r."LastSyncedAt",
                   COUNT(a."PaperID") AS papers,
                   (
                     r."LastSyncedAt" IS NOT NULL
                     AND r."LastSyncedAt" + (%s || ' days')::interval > NOW()
                   ) AS is_in_cooldown,
                   CASE
                     WHEN r."LastSyncedAt" IS NOT NULL
                     THEN r."LastSyncedAt" + (%s || ' days')::interval
                     ELSE NULL
                   END AS cooldown_until
            FROM "Users" u
            LEFT JOIN "Researcher" r ON r."UserID" = u."UserID"
            LEFT JOIN "Authors" a ON a."UserID" = u."UserID"
            WHERE u."TenantID" = %s
              AND (u."Scholar_ID" IS NOT NULL OR u."Orcid_ID" IS NOT NULL)
            GROUP BY u."UserID", u."Litrix_ID", u."FullName_Ar", u."Email",
                     u."Scholar_ID", u."Orcid_ID", r."LastSyncedAt"
            ORDER BY r."LastSyncedAt" ASC NULLS FIRST
        ''', [SYNC_COOLDOWN_DAYS, SYNC_COOLDOWN_DAYS, request.user.tenant_id])
        cols = [c[0] for c in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]

    return Response({
        'researchers': rows,
        'cooldown_days': SYNC_COOLDOWN_DAYS,
    })
