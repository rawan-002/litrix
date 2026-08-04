-- Enforce, at the schema level, that no two Journals rows can share the
-- same ISSN once formatting differences (a leading "ISSN-" tag, dashes,
-- spaces) are normalized away.
--
-- Application-level normalization (normalize_issn() in
-- scopus_attribution_fix.py) already prevents new duplicates on the
-- Scopus import path, and fix_duplicate_journal_issn.py cleaned up the
-- 54 historical duplicate groups this formatting mismatch had already
-- produced. This index is the backstop: it makes the same class of bug
-- impossible to reintroduce from ANY future write path (a new scraper,
-- a manual INSERT, a different ingestion script), not just the one that
-- caused it this time.
--
-- Partial: only rows whose normalized ISSN is a valid 8-char code are
-- covered, so journals with no ISSN (or a garbage value) can still
-- coexist -- ISSN_Print is not itself unique/required.
CREATE UNIQUE INDEX IF NOT EXISTS uq_journals_normalized_issn_print
    ON "Journals" (
        (regexp_replace(regexp_replace(upper(COALESCE("ISSN_Print", '')),
                                        '^ISSN-?', ''), '[^A-Z0-9]', '', 'g'))
    )
    WHERE length(regexp_replace(regexp_replace(upper(COALESCE("ISSN_Print", '')),
                                                 '^ISSN-?', ''), '[^A-Z0-9]', '', 'g')) = 8;
