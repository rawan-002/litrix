"""
DEPRECATED — one-shot backfill, no longer needed.

The scrapers in scrapers/ now populate Abstract on insert, so newly
ingested papers carry the field directly. This script existed to
backfill rows that were ingested before that change shipped.

If you find unfilled rows again, the equivalent SQL is:
    UPDATE "ResearchPaper"
       SET "Abstract" = ("RawData_Log"->>'abstract')
     WHERE "Abstract" IS NULL
       AND "RawData_Log"->>'abstract' IS NOT NULL;
"""
raise SystemExit(
    'backfill_abstracts.py is deprecated. See the docstring for the SQL equivalent.'
)
