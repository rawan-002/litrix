"""
DEPRECATED — one-shot backfill, no longer needed.

The scrapers in scrapers/ now persist the OpenAlex authorships[] array
into RawData_Log on every scrape, so the paper-detail modal can render
per-author affiliations + Al-Baha flag directly.

If you find rows where "RawData_Log"->'authorships' IS NULL, the
recipe to refill them is:
    for paper_id, doi in unfilled_rows:
        r = httpx.get(f'https://api.openalex.org/works/doi:{doi}')
        ships = (r.json() or {}).get('authorships') or []
        cur.execute(
            'UPDATE "ResearchPaper" '
            'SET "RawData_Log" = jsonb_set("RawData_Log", \\'{authorships}\\', %s::jsonb) '
            'WHERE "PaperID" = %s',
            [json.dumps(ships), paper_id],
        )
"""
raise SystemExit(
    'backfill_authorships.py is deprecated. See the docstring for the recipe.'
)
