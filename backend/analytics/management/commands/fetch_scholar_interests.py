"""
Backfill Researcher.ResearchInterests from Google Scholar profile labels.

USAGE
-----
    # Refresh everyone with a Scholar_ID:
    python manage.py fetch_scholar_interests

    # Limit to N researchers per run (politeness to Scholar):
    python manage.py fetch_scholar_interests --limit 20

    # Just one researcher:
    python manage.py fetch_scholar_interests --scholar-id lURTCEIAAAAJ

    # Force re-fetch even if recently refreshed:
    python manage.py fetch_scholar_interests --force

    # Dry-run (preview without writing):
    python manage.py fetch_scholar_interests --dry-run

    # Rotate through FreeProxies when Scholar starts rate-limiting:
    python manage.py fetch_scholar_interests --use-proxy

WHY A MANAGEMENT COMMAND (not part of the scraper pipeline):
    The main scraper fires on registration approval and pulls *papers*.
    Interests are a separate, lightweight signal — we want to be able
    to refresh them on a different cadence (e.g. monthly) without
    re-scraping everything.

WHY scholarly (not raw requests):
    Google Scholar HTML is unstable. The `scholarly` library tracks
    the page structure and gives us a typed dict that includes
    `interests: List[str]`.

RATE-LIMITING:
    Scholar throttles aggressively. We:
      * sleep between requests (--sleep, default 2.5s),
      * adaptively double the sleep when 'Cannot Fetch' shows up,
      * abort after 3 consecutive rate-limit failures, and
      * support --use-proxy for FreeProxies rotation.
"""
import time
import json
from datetime import datetime, timezone

from django.core.management.base import BaseCommand
from django.db import connection


class Command(BaseCommand):
    help = "Fetch Google Scholar profile labels into Researcher.ResearchInterests."

    def add_arguments(self, parser):
        parser.add_argument('--limit', type=int, default=None,
                            help='Max number of researchers to process.')
        parser.add_argument('--scholar-id', type=str, default=None,
                            help='Only process this single Scholar_ID.')
        parser.add_argument('--force', action='store_true',
                            help='Re-fetch even if updated in the last 30 days.')
        parser.add_argument('--dry-run', action='store_true',
                            help='Print what would be written without saving.')
        parser.add_argument('--sleep', type=float, default=2.5,
                            help='Seconds between Scholar requests.')
        parser.add_argument('--use-proxy', action='store_true',
                            help='Rotate through FreeProxies to dodge Scholar rate '
                                 'limits. Slower but survives heavy use.')

    def handle(self, *args, **opts):
        try:
            from scholarly import scholarly  # type: ignore
        except ImportError:
            self.stderr.write(self.style.ERROR(
                "scholarly is not installed. Run:\n"
                "    pip install scholarly"
            ))
            return

        # Optional proxy rotation — helps when Scholar starts returning
        # 'Cannot Fetch from Google Scholar' (= IP rate-limited).
        if opts['use_proxy']:
            try:
                from scholarly import ProxyGenerator  # type: ignore
                pg = ProxyGenerator()
                ok = pg.FreeProxies()
                if ok:
                    scholarly.use_proxy(pg)
                    self.stdout.write(self.style.SUCCESS(
                        "Proxy rotation enabled (FreeProxies)."))
                else:
                    self.stdout.write(self.style.WARNING(
                        "Could not initialise FreeProxies; continuing direct."))
            except Exception as e:
                self.stdout.write(self.style.WARNING(
                    f"Proxy setup failed: {e}. Continuing direct."))

        params = []
        sql = '''
            SELECT u."UserID", u."Scholar_ID",
                   COALESCE(u."FullName_Ar",
                            TRIM(CONCAT_WS(' ', u."FirstName", u."LastName"))) AS label,
                   r."ResearchInterestsUpdatedAt"
            FROM "Users" u
            JOIN "Researcher" r ON r."UserID" = u."UserID"
            WHERE u."Scholar_ID" IS NOT NULL
              AND TRIM(u."Scholar_ID") <> ''
        '''
        if opts['scholar_id']:
            sql += ' AND u."Scholar_ID" = %s'
            params.append(opts['scholar_id'])
        elif not opts['force']:
            sql += ''' AND (r."ResearchInterestsUpdatedAt" IS NULL
                            OR r."ResearchInterestsUpdatedAt" < NOW() - INTERVAL '30 days') '''

        sql += ' ORDER BY r."ResearchInterestsUpdatedAt" NULLS FIRST'
        if opts['limit']:
            sql += f' LIMIT {int(opts["limit"])}'

        with connection.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()

        if not rows:
            self.stdout.write("No researchers need refreshing.")
            return

        self.stdout.write(f"Processing {len(rows)} researcher(s)…")
        ok, empty, fail = 0, 0, 0

        # Adaptive backoff: if Scholar starts choking, slow down.
        # 3 consecutive 'Cannot Fetch' errors -> stop, save what we have.
        sleep_secs = opts['sleep']
        consecutive_fails = 0

        for user_id, scholar_id, label, last_updated in rows:
            try:
                self.stdout.write(f"  · {label}  (Scholar_ID={scholar_id}) … ",
                                  ending='')
                author = scholarly.search_author_id(scholar_id)
                try:
                    author = scholarly.fill(author, sections=['basics'])
                except Exception:
                    author = scholarly.fill(author)

                interests = author.get('interests') or []
                seen, cleaned = set(), []
                for raw in interests:
                    t = (raw or '').strip()
                    if not t:
                        continue
                    key = t.lower()
                    if key in seen:
                        continue
                    seen.add(key)
                    cleaned.append(t)

                if not cleaned:
                    self.stdout.write(self.style.WARNING("no interests on profile"))
                    empty += 1
                    if not opts['dry_run']:
                        self._save(user_id, None)
                    consecutive_fails = 0
                    time.sleep(sleep_secs)
                    continue

                self.stdout.write(self.style.SUCCESS(
                    f"{len(cleaned)} → {', '.join(cleaned[:3])}"
                    + (f" (+{len(cleaned)-3})" if len(cleaned) > 3 else "")
                ))
                ok += 1
                if not opts['dry_run']:
                    self._save(user_id, cleaned)
                consecutive_fails = 0

            except Exception as e:
                msg = str(e)
                self.stdout.write(self.style.ERROR(f"FAILED: {msg}"))
                fail += 1

                # Scholar's "Cannot Fetch" = IP rate-limited.
                if 'Cannot Fetch' in msg or '429' in msg or 'CAPTCHA' in msg:
                    consecutive_fails += 1
                    sleep_secs = min(sleep_secs * 2, 60)
                    self.stdout.write(self.style.WARNING(
                        f"  ↑ Scholar rate-limit detected — backing off to "
                        f"{sleep_secs:.0f}s between requests."
                    ))
                    if consecutive_fails >= 3:
                        self.stdout.write(self.style.ERROR(
                            "\n3 consecutive rate-limit failures — aborting.\n"
                            "Tips:\n"
                            "  • Wait 30-60 minutes for Scholar to release your IP.\n"
                            "  • Re-run with --use-proxy to rotate IPs.\n"
                            "  • Or use a VPN to change your IP."
                        ))
                        break

            time.sleep(sleep_secs)

        self.stdout.write(self.style.SUCCESS(
            f"\nDone. ok={ok}, empty={empty}, failed={fail}, "
            f"{'(dry-run — nothing written)' if opts['dry_run'] else 'saved.'}"
        ))

    @staticmethod
    def _save(user_id, interests_list):
        """Atomic UPDATE — never replaces with NULL unless explicit empty.

        Why the connection dance:
          Scholar requests can take >30s with backoff/retry, so our
          PostgreSQL connection often goes idle long enough for the
          server to close it (`server closed the connection unexpectedly`).
          We aggressively recycle the connection before each save so a
          stale handle never reaches `.execute()`.
        """
        try:
            connection.close_if_unusable_or_obsolete()
        except Exception:
            try:
                connection.close()
            except Exception:
                pass

        payload = json.dumps(interests_list) if interests_list else None

        last_exc = None
        for attempt in (1, 2):
            try:
                with connection.cursor() as cur:
                    cur.execute(
                        '''
                        UPDATE "Researcher"
                           SET "ResearchInterests"          = %s::jsonb,
                               "ResearchInterestsUpdatedAt" = %s
                         WHERE "UserID" = %s
                        ''',
                        [payload, datetime.now(timezone.utc), user_id],
                    )
                return
            except Exception as e:
                last_exc = e
                try:
                    connection.close()
                except Exception:
                    pass
                if attempt == 2:
                    raise last_exc
