"""
Shared DB + console helpers for Litrix standalone scripts (single source).

Every scrapers/ citations/ classification/ tools/ backend/ script that talks to
the domain DB via psycopg2 used to redefine its OWN identical `db()` (and its
own UTF-8 stdout wrapper). That is ~30 lines copy-pasted ~20 times, and it
meant connection behaviour (keepalives, timeouts) could silently drift between
scripts. This module is the one place that logic lives.

USAGE (from a script one level under the repo root, e.g. tools/foo.py):

    import os as _o, sys as _s
    _s.path.insert(0, _o.path.dirname(_o.path.dirname(_o.path.abspath(__file__))))
    from litrix_db import db, setup_utf8_stdout

    setup_utf8_stdout()
    conn = db()

DB switch (identical to the Django backend): DATABASE_URL set -> production
(Neon); empty -> local Postgres via discrete DB_* vars.
"""
import io
import os
import sys

import psycopg2

# Load a repo-root .env if python-dotenv is available, so scripts pick up
# DATABASE_URL the same way whether or not the caller exported it.
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

# Neon drops idle SSL connections; these keepalives keep long scrape/classify
# runs from dying mid-way.
_KEEPALIVE = dict(
    keepalives=1, keepalives_idle=30,
    keepalives_interval=10, keepalives_count=5,
)


def db():
    """Canonical psycopg2 connection.

    DATABASE_URL (prod/Neon) when set, otherwise discrete DB_* vars
    (local Postgres). The single environment switch shared with Django.
    """
    url = os.getenv('DATABASE_URL')
    if url:
        return psycopg2.connect(url, connect_timeout=20, **_KEEPALIVE)
    return psycopg2.connect(
        host=os.getenv('DB_HOST', 'localhost'),
        port=os.getenv('DB_PORT', '5432'),
        dbname=os.getenv('DB_NAME', 'LitrixDB'),
        user=os.getenv('DB_USER', 'postgres'),
        password=os.getenv('DB_PASSWORD', ''),
        connect_timeout=20, **_KEEPALIVE,
    )


def setup_utf8_stdout():
    """Wrap stdout/stderr in UTF-8 so Arabic text + box-drawing characters
    don't crash on a Windows cp1252 console. No-op if already UTF-8."""
    for _name in ('stdout', 'stderr'):
        s = getattr(sys, _name, None)
        enc = (getattr(s, 'encoding', '') or '')
        if s is not None and enc.lower() != 'utf-8' and hasattr(s, 'buffer'):
            setattr(sys, _name, io.TextIOWrapper(
                s.buffer, encoding='utf-8', errors='replace', line_buffering=True))
